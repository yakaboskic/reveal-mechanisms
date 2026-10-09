"""Committed workspace change log and push-only SSE delivery.

Events and their outbox row commit inside the writer's RDS transaction fence. A
background publisher then PUBLISHes and deletes the outbox row without the fence.
Redis is a wakeup only: replay always reads authorized durable records, never
message payloads.
"""
import asyncio
import atexit
import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import random
import threading
import time

from .auth import Problem, principal, credential_expiry
from .mysql_pool import DatabaseBusy
from .repository import now, uid, digest, canonical
from .runtime_metrics import measure
from . import redis_notifications

log = logging.getLogger(__name__)
COLLECTIONS = {
    'draft': ['drafts', 'gaps'], 'exploration': ['gaps', 'explorations'],
    'request': ['requests', 'gaps'], 'job': ['jobs', 'gaps'],
    'local_work': ['jobs'], 'research_operation': ['jobs'],
    'lightning_audit': ['audits'],
    'account': ['accounts', 'gaps'], 'account_membership': ['accounts', 'gaps'],
    'paragraph': ['accounts'], 'publication': ['accounts', 'gaps'],
    'analysis_outcome': ['explorations', 'gaps'], 'outcome_summary': ['explorations', 'gaps'],
    'outcome_publication': ['explorations', 'gaps'],
    'principal': ['identity'], 'grant': ['accounts', 'gaps', 'explorations'],
    'vector_active': ['catalog', 'gaps', 'accounts'],
    'vote': ['gaps', 'accounts'],
}
TYPES = {'draft':'draft.changed', 'exploration':'exploration.updated', 'job':'job.updated',
    'lightning_audit':'lightning_audit.updated',
    'account':'scientific_account.updated', 'account_membership':'scientific_account.updated',
    'paragraph':'scientific_account.updated', 'publication':'publication.changed',
    'outcome_publication':'publication.changed', 'principal':'identity.changed',
    'vector_active':'catalog.updated'}


def tracked(kind):
    return kind in COLLECTIONS or kind == 'event'


def track(tx, kind, identity, owner, data, old=None, operation='upsert', revision=1):
    if not tracked(kind): return
    if kind == 'event':
        tx.notification_jobs.add(data['job_id'])
        return
    if old and old['data'] == data and old['owner'] == owner: return
    if kind == 'lightning_audit' and old and old['owner'] == owner and old['data'].get('public') == data.get('public'):
        return  # Retaining internal model/source bytes does not change the workspace list.
    if kind == 'job' and old:
        # Detailed agent/tool events update last_event_id and timestamps, but
        # must not repeatedly re-fetch every workspace list.
        keys = ('status', 'stage', 'result', 'failure', 'warnings')
        if all(old['data'].get(key) == data.get(key) for key in keys) and old['owner'] == owner: return
    if kind in ('local_work', 'research_operation'):
        # Research Runs includes local work and its submissions. Data retrieval,
        # credentials and lease/activity heartbeats do not change that list.
        if kind == 'local_work':
            if data.get('job_id'): return  # Hosted runs already emit job events.
            keys = ('state', 'package_id', 'package_sha256', 'last_error', 'expires_at', 'closed_at')
        else:
            if data.get('kind') not in ('validate', 'submit'): return
            keys = ('state', 'result', 'error', 'report', 'account_ids', 'reused_account_ids',
                'validation_only', 'completed_at')
        if old and old['owner'] == owner and all(old['data'].get(key) == data.get(key) for key in keys): return
        if kind == 'research_operation':
            work = tx.get('local_work', data.get('local_work_id'))
            if not work or work['owner'] != owner or work['data'].get('job_id'): return
    owners = {owner}
    if old: owners.add(old['owner'])
    for audience in owners:
        if not audience: continue
        entity_id = data.get('account_id') or data.get('id') or (data.get('result') or {}).get('root_id') or identity
        tx.workspace_changes[(audience, kind, identity)] = {'event_type':TYPES.get(kind, 'workspace.changed'),
            'entity_id':entity_id, 'entity_revision':revision, 'operation':operation if audience == owner else 'remove',
            'collections':COLLECTIONS[kind]}
    if kind in ('publication', 'outcome_publication', 'vector_active', 'vote'):
        # Public notifications contain no owner or private scientific identifiers. A vector_active
        # change (a reference cutover) is named 'reference' so open composers recheck their anchors
        # only then, not on every publish or vote.
        prior = tx.workspace_changes.get(('public', 'catalog', 'catalog')) or {}
        reference = kind == 'vector_active' or prior.get('entity_id') == 'reference'
        tx.workspace_changes[('public', 'catalog', 'catalog')] = {'event_type':'catalog.updated',
            'entity_id':'reference' if reference else 'catalog', 'entity_revision':revision, 'operation':'invalidate',
            'collections':['catalog', 'accounts', 'gaps', 'explorations']}


def ownership_changed(tx, source, target):
    for owner in (source, target):
        tx.workspace_changes[(owner, 'principal', owner)] = {'event_type':'identity.changed',
            'entity_id':owner, 'entity_revision':0, 'operation':'resync',
            'collections':['identity', 'gaps', 'accounts', 'explorations', 'drafts', 'jobs', 'requests', 'audits']}


def prepare_commit(tx):
    """Called under the write fence immediately before the authoritative commit. However many changes, it reads
    every scope's cursor in one SELECT, updates each existing cursor once and inserts all events, new cursors and
    the outbox row in one statement."""
    timestamp = now()
    changes = sorted(tx.workspace_changes.items())
    cursors = {}
    for (owner, _, _), _ in changes:
        scope = 'public' if owner == 'public' else 'workspace:'+owner
        cursors.setdefault(scope, {'owner':owner, 'id':digest(scope)})
    found = tx.get_many('workspace_cursor', [cursor['id'] for cursor in cursors.values()]) if cursors else {}
    for cursor in cursors.values():
        state = found.get(cursor['id'])
        cursor.update(new=state is None, sequence=state['data']['sequence'] if state else 0,
            oldest=state['data'].get('oldest', 1) if state else 1)
    records = []
    for (owner, kind, identity), mutation in changes:
        scope = 'public' if owner == 'public' else 'workspace:'+owner
        cursor = cursors[scope]; cursor['sequence'] += 1; sequence = cursor['sequence']
        envelope = {'schema_version':1, 'event_id':cursor['id']+':'+str(sequence), 'cursor':str(sequence),
            'scope':'public' if owner == 'public' else 'workspace', 'committed_at':timestamp, **mutation}
        records.append(('workspace_event', cursor['id']+':'+str(sequence).zfill(20), owner, envelope))
    for cursor in cursors.values():
        state = {'sequence':cursor['sequence'], 'oldest':cursor['oldest']}
        if cursor['new']: records.append(('workspace_cursor', cursor['id'], cursor['owner'], state))
        else: tx.update_existing('workspace_cursor', cursor['id'], cursor['owner'], state)
    channels = {redis_notifications.channel(scope) for scope in cursors}
    channels.update(redis_notifications.channel('job:'+job_id) for job_id in tx.notification_jobs)
    pending = [{'id':uid(), 'channels':sorted(channels)}] if channels else []
    for item in pending:
        records.append(('notification_outbox', item['id'], 'system', {'channels':item['channels'], 'created_at':timestamp}))
    # The cursors advance under this fence, so every key is new; an event key that exists after all keeps put()'s overwrite.
    for offset in range(0, len(records), 100): tx.insert_new(records[offset:offset+100], ('workspace_event',))
    return pending


def deliver(repository, pending):
    """PUBLISH, then delete the acknowledged outbox rows outside the write fence. A failure of either leaves the
    rows for reconcile_notifications; nothing is retried here and no SQL is replayed."""
    if not pending: return 0
    try:
        with measure('notification', 'PUBLISH'):
            redis_notifications.publish([name for item in pending for name in item['channels']])
    except Exception as error:
        # Do not reveal provider URLs/tokens or turn a committed save into 503.
        log.warning('Committed notifications remain pending (%s)', type(error).__name__)
        return 0
    try: repository.discard_notifications([item['id'] for item in pending])
    except Exception as error:
        log.warning('Published notifications await reconciliation (%s)', type(error).__name__)
    return len(pending)


class Publisher:
    """One daemon thread per process delivers committed wakeups after the writer returns. Whatever is queued when
    it starts a batch shares one PUBLISH pipeline and one DELETE per repository. A full queue, a failure or process
    exit leaves outbox rows that reconcile_notifications republishes, so delivery stays at-least-once."""
    def __init__(self, limit=10000):
        self.limit = limit
        self._after_fork()

    def _after_fork(self):
        self.lock, self.pid, self.queue, self.thread = threading.Lock(), None, None, None

    def submit(self, repository, pending):
        with self.lock:
            if self.pid != os.getpid(): self.pid, self.queue, self.thread = os.getpid(), queue.Queue(self.limit), None
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, args=(self.queue,), name='reveal-notifications', daemon=True)
                self.thread.start()
            work = self.queue
        try: work.put_nowait((repository, pending)); return True
        except queue.Full:
            log.warning('Notification queue is full; reconciliation will deliver'); return False

    def _run(self, work):
        while True:
            batch = [work.get()]
            while len(batch) < 500:
                try: batch.append(work.get_nowait())
                except queue.Empty: break
            groups = {}
            for repository, pending in batch:
                groups.setdefault((repository.sqlite_path, repository.table_prefix), (repository, []))[1].extend(pending)
            try:
                for repository, pending in groups.values(): deliver(repository, pending)
            finally:
                for _ in batch: work.task_done()

    def backlog(self):
        work = self.queue if self.pid == os.getpid() else None
        return work.qsize() if work is not None else 0

    def flush(self, timeout=2.0):
        """Wait until everything submitted so far was delivered or left for reconciliation; False on timeout."""
        work = self.queue if self.pid == os.getpid() else None
        if work is None: return True
        deadline = time.monotonic()+timeout
        with work.all_tasks_done:
            while work.unfinished_tasks:
                remaining = deadline-time.monotonic()
                if remaining <= 0: return False
                work.all_tasks_done.wait(remaining)
        return True


publisher = Publisher()
if hasattr(os, 'register_at_fork'): os.register_at_fork(after_in_child=publisher._after_fork)
atexit.register(publisher.flush)


def publish_committed(repository, pending):
    """After COMMIT and the lease release. The background publisher delivers for Aurora; SQLite repositories and
    REVEAL_NOTIFICATION_DELIVERY=inline deliver before returning. Returns how many were delivered or queued."""
    if not pending: return 0
    mode = os.getenv('REVEAL_NOTIFICATION_DELIVERY') or ('inline' if repository.sqlite_path else 'background')
    if mode == 'inline': return deliver(repository, pending)
    return len(pending) if publisher.submit(repository, pending) else 0


def reconcile_notifications(repository, limit=100):
    """Invoke from managed reconciliation; reads RDS, PUBLISHes to Redis, then deletes what was published."""
    with repository.read_transaction() as tx:
        rows = tx.execute('SELECT id,payload FROM reveal_records WHERE kind=%s ORDER BY updated_at,id LIMIT %s',
            ('notification_outbox', min(1000, max(1, limit)))).fetchall()
        pending = [{'id':row[0], **json.loads(row[1])} for row in rows]
    return deliver(repository, pending)


def encode_cursor(owner, positions):
    payload = base64.urlsafe_b64encode(canonical([redis_notifications.namespace(), owner, positions]).encode()).decode().rstrip('=')
    signature = hmac.new(os.getenv('REVEAL_GATEWAY_SECRET', '').encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload+'.'+signature


def decode_cursor(owner, value):
    if not value: return {'workspace':0, 'public':0}
    try:
        payload, signature = value.split('.')
        expected = hmac.new(os.getenv('REVEAL_GATEWAY_SECRET', '').encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected): raise ValueError()
        namespace, audience, positions = json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        if namespace != redis_notifications.namespace() or audience != owner: raise ValueError()
        if set(positions) != {'workspace', 'public'} or any(type(n) is not int or n < 0 for n in positions.values()): raise ValueError()
        return positions
    except (ValueError, TypeError, KeyError):
        raise Problem(400, 'INVALID_CURSOR', 'The workspace cursor is invalid for this session.') from None


def replay(repository, authorization, positions, limit=500):
    with repository.read_transaction() as tx:
        owner = principal(tx, authorization)['user_id']
        events, highwater, expired = [], {}, False
        for label, scope, audience in [('workspace', 'workspace:'+owner, owner), ('public', 'public', 'public')]:
            state_id = digest(scope)
            state = tx.get('workspace_cursor', state_id)
            state = state['data'] if state else {'sequence':0, 'oldest':1}
            after = positions[label]
            highwater[label] = state['sequence']
            if after > state['sequence'] or after < state.get('oldest',1)-1:
                expired = True
            rows = tx.execute('SELECT payload FROM reveal_records WHERE kind=%s AND owner_id=%s AND id>%s AND id<=%s ORDER BY id LIMIT %s',
                ('workspace_event', audience, state_id+':'+str(after).zfill(20), state_id+':'+str(state['sequence']).zfill(20), limit+1)).fetchall()
            if len(rows) > limit: expired = True
            decoded = [json.loads(row[0]) for row in rows[:limit]]
            if len(rows) <= limit and (len(decoded) != state['sequence']-after or
                    any(int(event['cursor']) != after+index+1 for index, event in enumerate(decoded))):
                expired = True  # retention/removal gaps require a fresh snapshot
            events.extend(decoded)
        return owner, events, highwater, expired


def sse(event, payload, cursor=None):
    return (f'id: {cursor}\n' if cursor else '')+f'event: {event}\ndata: {json.dumps(payload, separators=(",", ":"))}\n\n'


def stream_deadline(authorization, workspace_expires_at=None):
    expiry = credential_expiry(authorization)
    lifetime = min(240, max(1, int(os.getenv('REVEAL_SSE_WINDOW_SECONDS', '240'))))
    if expiry is not None: lifetime = min(lifetime, max(0, expiry-time.time()))
    if workspace_expires_at:
        from datetime import datetime
        lifetime = min(lifetime, max(0, datetime.fromisoformat(workspace_expires_at.replace('Z', '+00:00')).timestamp()-time.time()))
    return time.monotonic()+lifetime


async def busy_pause(deadline):
    """A read found the pool busy (DatabaseBusy: no pooled session, so no statement ran): wait 0.5-2 s, never past the
    stream's deadline, then read the same positions again on a fresh snapshot."""
    await asyncio.sleep(min(random.uniform(.5, 2.), max(0., deadline-time.monotonic())))


async def workspace_response(repository, request, after=None):
    from fastapi.responses import StreamingResponse
    authorization = request.headers.get('authorization')
    if redis_notifications.closing(): raise Problem(503, 'SERVICE_UNAVAILABLE', 'The service is restarting; reconnect shortly.')
    def identify():
        with repository.read_transaction() as tx: return principal(tx, authorization)
    identity = await asyncio.to_thread(identify); owner = identity['user_id']
    header = request.headers.get('last-event-id')
    if header and after and header != after: raise Problem(400, 'INVALID_CURSOR', 'Last-Event-ID and after must agree.')
    positions = decode_cursor(owner, header or after)
    deadline = stream_deadline(authorization, identity.get('workspace_expires_at'))

    async def generate():
        nonlocal positions
        try:
            async with redis_notifications.hub().subscribe(['workspace:'+owner, 'public']) as subscription:
                # SUBSCRIBE acknowledgment precedes the consistent RDS snapshot.
                # Notifications queued while reading it trigger another replay.
                # Shutdown ends the stream like its deadline: the client renews with its cursor.
                while time.monotonic() < deadline and not redis_notifications.closing():
                    try:
                        current_owner, events, highwater, expired = await asyncio.to_thread(replay, repository, authorization, positions)
                        if current_owner != owner:
                            yield sse('access_revoked', {'schema_version':1}); return
                    except DatabaseBusy:
                        # A busy pool keeps the stream open: positions stay where they were.
                        if redis_notifications.closing() or time.monotonic() >= deadline: return
                        yield ': heartbeat\n\n'
                        await busy_pause(deadline)
                        continue
                    except Problem:
                        yield sse('access_revoked', {'schema_version':1}); return
                    if expired:
                        positions = highwater
                        yield sse('resync_required', {'schema_version':1, 'reason':'cursor_expired'}, encode_cursor(owner, positions))
                    else:
                        for event in events:
                            positions[event['scope']] = int(event['cursor'])
                            yield sse('workspace_change', event, encode_cursor(owner, positions))
                    yield sse('ready', {'schema_version':1}, encode_cursor(owner, positions))
                    while time.monotonic() < deadline and not redis_notifications.closing():
                        try: reason = await subscription.wait(min(15, max(.01, deadline-time.monotonic())))
                        except asyncio.TimeoutError:
                            # HTTP keepalive only: zero RDS reads/Redis commands.
                            yield ': heartbeat\n\n'
                            continue
                        if redis_notifications.closing(): return
                        if reason == 'disconnected':
                            yield sse('connection_degraded', {'schema_version':1})
                            continue
                        break
        except asyncio.TimeoutError:   # the subscription timed out; a busy database is handled above
            yield sse('connection_degraded', {'schema_version':1})
    return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control':'private, no-store', 'X-Accel-Buffering':'no'})


async def job_event_stream(repository, request, job_id, authorization, cursor, limit, read_events, initial=None):
    """Retains the public JobEvent envelope; only changes its wakeup source. `initial` is the handler's authorizing
    read: it is streamed first, and a finished job never subscribes."""
    deadline = stream_deadline(authorization)
    if initial is not None:
        for item in initial['items']:
            cursor = int(item['id'])
            yield sse(item['event_type'], item, item['id'])
        if initial['terminal']: return
    if redis_notifications.closing(): return
    try:
        async with redis_notifications.hub().subscribe(['job:'+job_id]) as subscription:
            # The first read catches up on commits between `initial`'s snapshot and the SUBSCRIBE acknowledgment.
            while time.monotonic() < deadline and not redis_notifications.closing():
                try: data = await asyncio.to_thread(read_events, job_id, authorization, cursor, limit)
                except DatabaseBusy:
                    # A busy pool keeps the stream open; the cursor does not move.
                    if redis_notifications.closing() or time.monotonic() >= deadline: return
                    yield ': heartbeat\n\n'
                    await busy_pause(deadline)
                    continue
                except Problem: return
                for item in data['items']:
                    cursor = int(item['id'])
                    yield sse(item['event_type'], item, item['id'])
                if data['terminal']: return
                if len(data['items']) >= limit: continue
                while time.monotonic() < deadline and not redis_notifications.closing():
                    try: await subscription.wait(min(15, max(.01, deadline-time.monotonic())))
                    except asyncio.TimeoutError:
                        yield ': heartbeat\n\n'; continue
                    if redis_notifications.closing(): return
                    break
    except asyncio.TimeoutError:
        return  # the subscription timed out; the client renews with its durable cursor


def register(app, repository_provider):
    from fastapi import Request
    @app.get('/v1/me/workspace/events')
    async def workspace_events(request: Request, after: str | None = None):
        return await workspace_response(repository_provider(), request, after)


def notification_health(repository):
    with repository.read_transaction() as tx:
        row = tx.execute('SELECT COUNT(*),MIN(updated_at) FROM reveal_records WHERE kind=%s', ('notification_outbox',)).fetchone()
    bridges = [bridge.health() for bridge in redis_notifications.active_hubs()]
    return {'transport':redis_notifications.configuration()[0], 'outbox_pending':row[0], 'oldest_pending_at':row[1],
        'publish_queue':publisher.backlog(),
        'subscriptions':sum(item['subscriptions'] for item in bridges),
        'connected':sum(item['connected'] for item in bridges),
        'connections':sum(item['connections'] for item in bridges),
        'reconnects':sum(item['reconnects'] for item in bridges),
        'failures':sum(item['failures'] for item in bridges)}
