"""Committed workspace change log and push-only SSE delivery.

Events/outbox share the application's RDS transaction fence. Redis is a wakeup
only: replay always reads authorized durable records, never message payloads.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time

from .auth import Problem, principal, credential_expiry
from .repository import now, uid, digest, canonical
from . import redis_notifications

log = logging.getLogger(__name__)
COLLECTIONS = {
    'draft': ['drafts', 'gaps'], 'exploration': ['gaps', 'explorations'],
    'request': ['requests', 'gaps'], 'job': ['jobs', 'gaps'],
    'account': ['accounts', 'gaps'], 'account_membership': ['accounts', 'gaps'],
    'paragraph': ['accounts'], 'publication': ['accounts', 'gaps'],
    'analysis_outcome': ['explorations', 'gaps'], 'outcome_summary': ['explorations', 'gaps'],
    'outcome_publication': ['explorations', 'gaps'],
    'principal': ['identity'], 'grant': ['accounts', 'gaps', 'explorations'],
    'vector_active': ['catalog', 'gaps', 'accounts'],
}
TYPES = {'draft':'draft.changed', 'exploration':'exploration.updated', 'job':'job.updated',
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
    if kind == 'job' and old:
        # Detailed agent/tool events update last_event_id and timestamps, but
        # must not repeatedly re-fetch every workspace list.
        keys = ('status', 'stage', 'result', 'failure', 'warnings')
        if all(old['data'].get(key) == data.get(key) for key in keys) and old['owner'] == owner: return
    owners = {owner}
    if old: owners.add(old['owner'])
    for audience in owners:
        if not audience: continue
        entity_id = data.get('account_id') or data.get('id') or (data.get('result') or {}).get('root_id') or identity
        tx.workspace_changes[(audience, kind, identity)] = {'event_type':TYPES.get(kind, 'workspace.changed'),
            'entity_id':entity_id, 'entity_revision':revision, 'operation':operation if audience == owner else 'remove',
            'collections':COLLECTIONS[kind]}
    if kind in ('publication', 'outcome_publication', 'vector_active'):
        # Public notifications contain no owner or private scientific identifiers.
        tx.workspace_changes[('public', 'catalog', 'catalog')] = {'event_type':'catalog.updated',
            'entity_id':'catalog', 'entity_revision':revision, 'operation':'invalidate',
            'collections':['catalog', 'accounts', 'gaps', 'explorations']}


def ownership_changed(tx, source, target):
    for owner in (source, target):
        tx.workspace_changes[(owner, 'principal', owner)] = {'event_type':'identity.changed',
            'entity_id':owner, 'entity_revision':0, 'operation':'resync',
            'collections':['identity', 'gaps', 'accounts', 'explorations', 'drafts', 'jobs', 'requests']}


def prepare_commit(tx):
    """Called under the write fence immediately before the authoritative commit."""
    timestamp = now()
    channels = set()
    for (owner, kind, identity), mutation in sorted(tx.workspace_changes.items()):
        scope = 'public' if owner == 'public' else 'workspace:'+owner
        state_id = digest(scope)
        state = tx.get('workspace_cursor', state_id)
        sequence = (state['data']['sequence'] if state else 0) + 1
        tx.put('workspace_cursor', state_id, owner, {'sequence':sequence, 'oldest':(state['data'].get('oldest', 1) if state else 1)})
        envelope = {'schema_version':1, 'event_id':state_id+':'+str(sequence), 'cursor':str(sequence),
            'scope':'public' if owner == 'public' else 'workspace', 'committed_at':timestamp, **mutation}
        tx.put('workspace_event', state_id+':'+str(sequence).zfill(20), owner, envelope)
        channels.add(redis_notifications.channel(scope))
    channels.update(redis_notifications.channel('job:'+job_id) for job_id in tx.notification_jobs)
    if not channels: return []
    identity = uid()
    tx.put('notification_outbox', identity, 'system', {'channels':sorted(channels), 'created_at':timestamp})
    return [{'id':identity, 'channels':sorted(channels)}]


def publish_committed(repository, pending):
    if not pending: return 0
    try:
        redis_notifications.publish([name for item in pending for name in item['channels']])
        with repository.transaction() as tx:
            for item in pending: tx.remove('notification_outbox', item['id'])
        return len(pending)
    except Exception as error:
        # Do not reveal provider URLs/tokens or turn a committed save into 503.
        log.warning('Committed notifications remain pending (%s)', type(error).__name__)
        return 0


def reconcile_notifications(repository, limit=100):
    """Invoke from managed reconciliation; reads RDS, only PUBLISHes to Redis."""
    with repository.read_transaction() as tx:
        rows = tx.execute('SELECT id,payload FROM reveal_records WHERE kind=%s ORDER BY updated_at,id LIMIT %s',
            ('notification_outbox', min(1000, max(1, limit)))).fetchall()
        pending = [{'id':row[0], **json.loads(row[1])} for row in rows]
    return publish_committed(repository, pending)


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


async def workspace_response(repository, request, after=None):
    from fastapi.responses import StreamingResponse
    authorization = request.headers.get('authorization')
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
                while time.monotonic() < deadline:
                    try:
                        current_owner, events, highwater, expired = await asyncio.to_thread(replay, repository, authorization, positions)
                        if current_owner != owner:
                            yield sse('access_revoked', {'schema_version':1}); return
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
                    while time.monotonic() < deadline:
                        try: reason = await subscription.wait(min(15, max(.01, deadline-time.monotonic())))
                        except asyncio.TimeoutError:
                            # HTTP keepalive only: zero RDS reads/Redis commands.
                            yield ': heartbeat\n\n'
                            continue
                        if reason == 'disconnected':
                            yield sse('connection_degraded', {'schema_version':1})
                            continue
                        break
        except asyncio.TimeoutError:
            yield sse('connection_degraded', {'schema_version':1})
    return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control':'private, no-store', 'X-Accel-Buffering':'no'})


async def job_event_stream(repository, request, job_id, authorization, cursor, limit, read_events):
    """Retains the public JobEvent envelope; only changes its wakeup source."""
    deadline = stream_deadline(authorization)
    try:
        async with redis_notifications.hub().subscribe(['job:'+job_id]) as subscription:
            while time.monotonic() < deadline:
                try: data = await asyncio.to_thread(read_events, job_id, authorization, cursor, limit)
                except Problem: return
                for item in data['items']:
                    cursor = int(item['id'])
                    yield sse(item['event_type'], item, item['id'])
                if data['terminal']: return
                if len(data['items']) >= limit: continue
                while time.monotonic() < deadline:
                    try: await subscription.wait(min(15, max(.01, deadline-time.monotonic())))
                    except asyncio.TimeoutError:
                        yield ': heartbeat\n\n'; continue
                    break
    except asyncio.TimeoutError:
        return  # client renews with its durable cursor


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
        'subscriptions':sum(item['subscriptions'] for item in bridges),
        'connected':sum(item['connected'] for item in bridges),
        'connections':sum(item['connections'] for item in bridges),
        'reconnects':sum(item['reconnects'] for item in bridges),
        'failures':sum(item['failures'] for item in bridges)}
