"""Signed Workflow v1 HTTP endpoints and bounded after-commit dispatch repair.

Only RDS is scanned by the managed reconciliation request. No Redis read,
Streams consumer, heartbeat service, or application background worker is used.
"""
import asyncio
import json
import logging
import os
from urllib.parse import urlparse
from fastapi import Request
from fastapi.responses import JSONResponse
from . import jobs, workflow_state as state
from .repository import DatabaseBusy, FenceBusy, now, digest
from .workflow_execution import WorkflowExecution, infrastructure_error

PATH = '/internal/workflows/research-v1'
CONTROL_PATH = '/internal/workflows/control-v1'
RECONCILE_PATH = '/internal/workflows/reconcile-v1'
CLEANUP_PATH = '/internal/workflows/cleanup-v1'
log = logging.getLogger('reveal.workflow')


def config():
    required = ('QSTASH_TOKEN', 'QSTASH_CURRENT_SIGNING_KEY', 'QSTASH_NEXT_SIGNING_KEY', 'REVEAL_WORKFLOW_URL')
    missing = [key for key in required if not os.getenv(key)]
    if missing: raise ValueError('Workflow configuration missing: ' + ', '.join(missing))
    url = os.environ['REVEAL_WORKFLOW_URL'].rstrip('/')
    parsed = urlparse(url)
    local = os.getenv('REVEAL_ENVIRONMENT', 'development') in ('development', 'test')
    if parsed.scheme != 'https' and not (local and parsed.hostname in ('localhost', '127.0.0.1', 'host.docker.internal')):
        raise ValueError('Workflow callback must use HTTPS outside isolated local development')
    if not parsed.path.endswith(PATH) or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('REVEAL_WORKFLOW_URL must end with ' + PATH)
    return url


def client():
    from qstash import AsyncQStash
    from .workflow_transport import WorkflowHttp
    result = AsyncQStash(os.environ['QSTASH_TOKEN'], base_url=os.getenv('QSTASH_URL'), retry=False)
    result.http = WorkflowHttp(result.http)
    return result


def steps_per_run():
    value = int(os.getenv('REVEAL_WORKFLOW_STEPS_PER_RUN', '6'))
    if value < 1: raise ValueError('REVEAL_WORKFLOW_STEPS_PER_RUN must be positive')
    return value


def payload_for(intent):
    # Legacy intents use a fixed default; never change a replay's step order
    # when a deployment changes the configured size of future runs.
    return {key: intent[key] for key in ('job_id', 'namespace', 'generation')} | {
        'index': intent.get('index', 0), 'steps_per_run': intent.get('steps_per_run', 6)}


def queue_generation(tx, owner, execution):
    identity = execution['job_id']; generation = execution['generation']
    execution.update(fence=None, lease_until=None, step=None, run_id=None,
                     disposition='ready', updated_at=now(), expected_at=state.after(180))
    tx.put('execution', identity, owner, execution)
    queue = tx.get('queue', identity)['data']; queue.update(token=None, lease_until=None)
    tx.put('queue', identity, owner, queue)
    intent = {'job_id': identity, 'namespace': execution['namespace'], 'generation': generation,
        'index': execution['phase_index'], 'steps_per_run': steps_per_run(),
        'dispatch_id': digest([identity, generation]), 'run_id': 'reveal-' + digest([identity, generation])[:40],
        'attempts': 0, 'published_at': None, 'created_at': now(), 'next_attempt_at': now()}
    tx.put('workflow_dispatch', identity, owner, intent)
    return intent


def continue_run(repository, payload, index):
    """Fence a completed segment and commit its next dispatch exactly once."""
    identity = digest(['workflow-handoff-v1', payload['namespace'], payload['job_id'], payload['generation'], index])
    with repository.transaction() as tx:
        # One read of every row the handoff decides on or replaces; the rest of the fence is writes.
        tx.get_records([('workflow_handoff', identity), ('execution', payload['job_id']),
                        ('queue', payload['job_id']), ('workflow_dispatch', payload['job_id'])])
        prior = tx.get('workflow_handoff', identity)
        if prior: return prior['data']['result']
        row = tx.get('execution', payload['job_id'])
        if not row: raise state.StaleExecution('Execution is unavailable')
        execution = row['data']; state.check(execution, payload)
        if execution['phase_index'] != index or execution['disposition'] != 'ready':
            raise state.StaleExecution('Workflow continuation checkpoint changed')
        if (execution.get('lease_until') or '') > now():
            raise state.StepBusy('Workflow phase still owns its lease')
        execution.update(generation=execution['generation'] + 1,
                         handoffs=execution.get('handoffs', 0) + 1)
        intent = queue_generation(tx, row['owner'], execution)
        result = {'generation': intent['generation'], 'index': index}
        tx.put('workflow_handoff', identity, row['owner'], {
            'job_id': payload['job_id'], 'namespace': payload['namespace'],
            'generation': payload['generation'], 'result': result, 'created_at': now()})
        return result


def segment_limit(context, payload):
    if 'steps_per_run' in payload: return payload['steps_per_run']
    # Upgrade legacy runs by first replaying every already-recorded phase.
    # Otherwise their seventh phase would be mistaken for the new handoff.
    # This reads only the authenticated, normalized history, never live state.
    completed = sum(step.step_type == 'Run' and step.step_name.startswith('phase-') for step in context._steps)
    return max(6, completed)


async def continue_job(repository, payload, index):
    result = await asyncio.to_thread(continue_run, repository, payload, index)
    # The dispatch intent remains durable if publishing fails, including a
    # process exit after this transaction but before this SDK step is saved.
    await dispatch_job(repository, payload['job_id'])
    return result


async def trigger(intent, *, qstash=None):
    """The pinned SDK's own first-invocation protocol, with a stable run ID.

    publish_json forwards control headers and therefore cannot initialize an
    actual Workflow run in qstash-py 3; use the SDK protocol builder instead.
    """
    from upstash_workflow.workflow_requests import _get_first_invocation_batch_body
    url = config()
    batch = _get_first_invocation_batch_body(intent['run_id'], url, {}, payload_for(intent), retries=5,
                                            workflow_failure_url=url)
    batch[0]['headers']['Upstash-Deduplication-Id'] = intent['dispatch_id']
    batch[0]['headers']['Upstash-Timeout'] = os.getenv('REVEAL_WORKFLOW_DELIVERY_TIMEOUT', '420s')
    async with asyncio.timeout(15):
        return await (qstash or client()).http.request(path='/v2/batch', method='POST',
            headers={'Content-Type': 'application/json'}, body=json.dumps(batch))


def pending(repository, kind, limit, job_id=None):
    # kind equality uses the existing primary key; bounded pages prevent an
    # unbounded write lock even when scheduler delivery was unavailable.
    with repository.read_transaction() as tx:
        namespace_expr = "json_extract(payload,'$.namespace')" if tx.sqlite else "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.namespace'))"
        base = 'SELECT id,payload FROM reveal_records WHERE kind=%s AND ' + namespace_expr + '=%s'
        rows = (tx.execute(base+' AND id=%s', (kind, jobs.namespace(), job_id)).fetchall()
                if job_id else tx.execute(base+' ORDER BY updated_at,id LIMIT %s',
                          (kind, jobs.namespace(), limit)).fetchall())
    return [(row[0], json.loads(row[1])) for row in rows]


def pending_for_job(repository, job_id, kinds):
    """One job's intents of these kinds in one statement: primary-key point lookups."""
    with repository.read_transaction() as tx:
        namespace_expr = "json_extract(payload,'$.namespace')" if tx.sqlite else "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.namespace'))"
        rows = tx.execute('SELECT kind,id,payload FROM reveal_records WHERE kind IN (' + ','.join(['%s'] * len(kinds)) +
                          ') AND id=%s AND ' + namespace_expr + '=%s', (*kinds, job_id, jobs.namespace())).fetchall()
    found = {kind: [] for kind in kinds}
    for kind, identity, payload in rows: found[kind].append((identity, json.loads(payload)))
    return found


async def dispatch_pending(repository, limit=25, *, qstash=None, job_id=None, control=True):
    """control=False skips the cancellation read where none can exist (a new job, a review retry). One job's
    intents are read in one statement; the bulk path keeps reading controls after its dispatches. At most one
    QStash client is built per call, never shared across event loops."""
    delivered = failed = 0
    if jobs.transport() != 'workflow': return {'delivered': 0, 'failed': 0}
    config()
    shared = qstash
    def provider():
        nonlocal shared
        if shared is None: shared = client()
        return shared
    controls = None
    if job_id:
        found = await asyncio.to_thread(pending_for_job, repository, job_id,
                                        ('workflow_dispatch', 'workflow_control') if control else ('workflow_dispatch',))
        dispatches, controls = found['workflow_dispatch'], found.get('workflow_control', [])
    else: dispatches = await asyncio.to_thread(pending, repository, 'workflow_dispatch', min(limit, 100))
    for identity, intent in dispatches:
        if intent.get('published_at') or intent.get('next_attempt_at', '') > now(): continue
        try:
            await trigger(intent, qstash=provider())
            await asyncio.to_thread(acknowledge_dispatch, repository, identity, intent)
            delivered += 1
        except Exception as exc:
            failed += 1
            await asyncio.to_thread(retry_dispatch, repository, identity, intent, type(exc).__name__)
            log.warning('Workflow dispatch remains pending: %s', type(exc).__name__)
    if controls is None: controls = await asyncio.to_thread(pending, repository, 'workflow_control', min(limit, 100))
    for identity, intent in controls:
        if intent.get('published_at'): continue
        try:
            async with asyncio.timeout(15):
                await provider().message.publish_json(url=config().removesuffix(PATH) + CONTROL_PATH,
                    body=payload_for(intent), deduplication_id=digest(['control', identity, intent['generation']]), retries=5)
            await asyncio.to_thread(acknowledge_control, repository, identity, intent)
        except Exception as exc:
            failed += 1; log.warning('Workflow cancellation delivery remains pending: %s', type(exc).__name__)
    return {'delivered': delivered, 'failed': failed}


def cleanup_pending(repository, limit, identity=None):
    with repository.read_transaction() as tx:
        expr = "json_extract(payload,'$.namespace')" if tx.sqlite else "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.namespace'))"
        due = "json_extract(payload,'$.next_attempt_at')" if tx.sqlite else "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.next_attempt_at'))"
        lease = "json_extract(payload,'$.lease_until')" if tx.sqlite else "NULLIF(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.lease_until')),'null')"
        sql = ('SELECT id,payload FROM reveal_records WHERE kind=%s AND '+expr+'=%s AND '+due+'<=%s'
               + ' AND ('+lease+' IS NULL OR '+lease+'<=%s)')
        args = ['workflow_cleanup', jobs.namespace(), now(), now()]
        if identity: sql += ' AND id=%s'; args.append(identity)
        rows = tx.execute(sql+' ORDER BY updated_at,id LIMIT %s', (*args, min(limit,100))).fetchall()
    return [(row[0], json.loads(row[1])) for row in rows]


def cleanup_published(repository, identity, observed):
    with repository.transaction() as tx:
        row = tx.get('workflow_cleanup', identity)
        if row and row['data']['dispatch_generation'] == observed['dispatch_generation']:
            value = row['data']; value.update(dispatch_generation=value['dispatch_generation']+1,
                published_at=now(), next_attempt_at=state.after(300))
            tx.put('workflow_cleanup', identity, row['owner'], value)


async def dispatch_cleanup(repository, identity=None, *, qstash=None, limit=25):
    delivered = failed = 0
    if jobs.transport() != 'workflow': return {'delivered':0,'failed':0}
    url = config().removesuffix(PATH) + CLEANUP_PATH
    for key, intent in await asyncio.to_thread(cleanup_pending, repository, limit, identity):
        if (intent.get('lease_until') or '') > now(): continue
        try:
            async with asyncio.timeout(15):
                await (qstash or client()).message.publish_json(url=url,
                    body={'cleanup_id':key,'namespace':intent['namespace']}, retries=5,
                    deduplication_id=digest(['cleanup',key,intent['dispatch_generation']]))
            await asyncio.to_thread(cleanup_published, repository, key, intent)
            delivered += 1
        except Exception as exc:
            failed += 1
            log.warning('Box cleanup delivery remains pending: %s', type(exc).__name__)
    return {'delivered':delivered,'failed':failed}


def acknowledge_dispatch(repository, identity, intent):
    with repository.transaction() as tx:
        row = tx.get('workflow_dispatch', identity)
        if row and row['data']['dispatch_id'] == intent['dispatch_id']:
            value = row['data']; value.update(published_at=now(), attempts=value['attempts'] + 1)
            tx.put('workflow_delivery', intent['dispatch_id'], row['owner'], value)
            tx.remove('workflow_dispatch', identity)
            execution = tx.get('execution', identity)
            if execution and execution['data']['generation'] == intent['generation']:
                data = execution['data']; data.update(run_id=intent['run_id'], expected_at=state.after(180))
                tx.put('execution', identity, row['owner'], data)


def retry_dispatch(repository, identity, intent, error):
    with repository.transaction() as tx:
        row = tx.get('workflow_dispatch', identity)
        if row and row['data']['dispatch_id'] == intent['dispatch_id']:
            value = row['data']; value.update(attempts=value['attempts'] + 1, next_attempt_at=state.after(30), last_error=error)
            tx.put('workflow_dispatch', identity, row['owner'], value)


def acknowledge_control(repository, identity, intent):
    with repository.transaction() as tx:
        row = tx.get('workflow_control', identity)
        if row and row['data']['generation'] == intent['generation']: tx.remove('workflow_control', identity)


async def dispatch_job(repository, job_id, *, control=True):
    return await dispatch_pending(repository, limit=1, job_id=job_id, control=control)


# A stale execution whose recovery cannot be decided waits this long before the sweep decides it again.
SWEEP_HOLD_SECONDS = 900


def sweep(repository, limit=25):
    """Expire research inputs and re-fence stale executions; returns (recovered, deferred, held).

    One read snapshot finds the candidates: expired editors and uploads by id only, and executions whose lease
    and expected time have both passed (a superset of what the in-lock checks accept, so nothing due is missed).
    The write fence is taken only when there is a candidate, never waiting behind another writer: a held fence
    defers that work to the next tick. Every candidate is re-read and decided again under the fence.

    Each execution is decided in its own transaction. A busy, lost or failing database still defers or fails the
    whole tick. Any other failure concerns that row alone (a cleanup binding that changed, a malformed record):
    its transaction rolled back, so its reservation, Box, cleanup obligation, job and disposition are exactly as
    they were. It is logged, reported in held for an operator, and backed off for SWEEP_HOLD_SECONDS, after which
    the sweep decides it again; the other rows continue.
    """
    from .user_inputs import cleanup, cleanup_candidates
    recovered, deferred, held = 0, False, []
    # Completed records move to an archive kind at completion in a future compaction migration;
    # SQL JSON filtering keeps terminal and in-progress records from starving stale work now.
    with repository.read_transaction() as tx:
        candidates = cleanup_candidates(tx)
        text = ((lambda name: "json_extract(payload,'$." + name + "')") if tx.sqlite else
                (lambda name: "JSON_UNQUOTE(JSON_EXTRACT(payload,'$." + name + "'))"))
        due = (lambda name: 'COALESCE(' + (text(name) if tx.sqlite else 'NULLIF(' + text(name) + ",'null')") + ",'')<=%s")
        rows = tx.execute('SELECT e.id FROM reveal_records e WHERE e.kind=%s AND ' + text('namespace') + '=%s AND ' + text('disposition') +
            ' NOT IN (%s,%s) AND ' + due('lease_until') + ' AND ' + due('expected_at') + ' AND NOT EXISTS (SELECT 1 FROM reveal_records d'
            ' WHERE d.kind=%s AND d.id=e.id) ORDER BY e.updated_at,e.id LIMIT %s',
            ('execution', jobs.namespace(), 'complete', 'recovery_required', now(), now(), 'workflow_dispatch', min(limit, 100))).fetchall()
    if any(candidates):
        try:
            with repository.transaction(nowait=True) as tx: cleanup(tx, candidates=candidates)
        except FenceBusy: deferred = True
    if rows:
        # Configuration errors fail the tick; they must not be mistaken for one row's failure.
        budget = int(os.getenv('REVEAL_WORKFLOW_MAX_RECOVERIES', '3')); steps_per_run()
    for (identity,) in rows:
        generation = None
        try:
            with repository.transaction(nowait=True) as tx:
                row = tx.get('execution', identity)
                if not row: continue
                execution = row['data']
                if execution.get('namespace') != jobs.namespace(): continue
                if (execution.get('lease_until') or '') > now() or execution.get('expected_at', '') > now(): continue
                if tx.get('workflow_dispatch', identity): continue
                generation = execution['generation']; execution.pop('sweep_hold', None)
                # Explicit generation fencing permits recovery without two owners.
                # Effect intents/capture/review reservations survive unchanged.
                # A missing scheduler delivery is not evidence that a phase failed.
                # Only a retry explicitly recorded by the execution engine spends
                # the failure budget; planned handoffs spend neither counter.
                step_failure = execution.get('retry_cause') == 'step_failure'
                recoveries = execution.get('recoveries', 0) + int(step_failure)
                if step_failure and recoveries > budget:
                    # Fails the job and hands any known Box to durable cleanup in this same fenced transaction.
                    state.require_recovery(tx, row['owner'], execution, 'Managed recovery budget exhausted')
                    continue
                execution.update(generation=execution['generation'] + 1, recoveries=recoveries,
                    delivery_recoveries=execution.get('delivery_recoveries', 0) + int(not step_failure),
                    retry_cause=None)
                queue_generation(tx, row['owner'], execution)
                recovered += 1
        except FenceBusy:
            deferred = True; break
        except state.StepBusy:
            continue   # a phase took the lease after the probe; the next tick decides it
        except Exception as exc:
            if infrastructure_error(exc): raise   # nothing of this row was written; the tick fails or defers
            detail = str(exc)[:200] if isinstance(exc, state.StaleExecution) else None
            log.error('Reconciliation cannot recover execution %s (%s%s); operator inspection required',
                      identity, type(exc).__name__, ': ' + detail if detail else '')
            entry = {'job_id': identity, 'error': type(exc).__name__, 'detail': detail, 'held_until': None}
            held.append(entry)
            try: entry['held_until'] = hold_stale(repository, identity, generation, entry)
            except FenceBusy:
                deferred = True; break
            except Exception as hold_error:
                if infrastructure_error(hold_error): raise
                log.error('Reconciliation could not back off execution %s (%s); it is decided again next tick',
                          identity, type(hold_error).__name__)
    return recovered, deferred, held


def hold_stale(repository, identity, generation, entry):
    """Back an undecidable stale execution off the sweep and record why, for workflow_admin inspect.

    Only its expected time and a sweep_hold marker change. Its reservation, Box, cleanup obligation, job and
    disposition stay as they were, so nothing is released or abandoned; the sweep (or an operator) decides it
    again after the hold. A row that changed since the failed attempt is left for the next tick instead."""
    if generation is None: return None
    with repository.transaction(nowait=True) as tx:
        row = tx.get('execution', identity)
        if not row or row['data'].get('generation') != generation: return None
        execution = row['data']
        if ((execution.get('lease_until') or '') > now() or execution.get('expected_at', '') > now()
                or execution.get('disposition') in ('complete', 'recovery_required') or tx.get('workflow_dispatch', identity)):
            return None
        until = state.after(SWEEP_HOLD_SECONDS)
        execution.update(expected_at=until, updated_at=now(), sweep_hold={
            'at': now(), 'until': until, 'error': entry['error'], 'detail': entry['detail']})
        tx.put('execution', identity, row['owner'], execution)
        return until


def reconcile_stale(repository, limit=25):
    return sweep(repository, limit)[0]


def mount_workflow(app, repository):
    """Mount once. Missing signatures fail startup when workflow routing is on."""
    if jobs.transport() != 'workflow': return
    from qstash import Receiver
    from upstash_workflow.fastapi import Serve
    url = config(); receiver = Receiver(os.environ['QSTASH_CURRENT_SIGNING_KEY'], os.environ['QSTASH_NEXT_SIGNING_KEY'])
    engine = WorkflowExecution(repository)

    def mark_retry(payload, status):
        with repository.transaction() as tx:
            row = tx.get('execution', payload['job_id'])
            if not row: return
            execution = row['data']
            try: state.check(execution, payload)
            except state.StaleExecution: return
            if execution['disposition'] in ('complete', 'recovery_required'): return
            # Late scheduler failures cannot revoke a live phase lease. The
            # reconciler handles it after expiry and distinguishes deliveries
            # from phase exceptions using the engine's durable retry cause.
            execution.update(expected_at=now(), scheduler_failure_at=now(), scheduler_failure_status=status)
            if (execution.get('lease_until') or '') <= now():
                if (execution.get('retry_cause') == 'step_failure'
                        and execution.get('recoveries', 0) >= int(os.getenv('REVEAL_WORKFLOW_MAX_RECOVERIES', '3'))):
                    state.require_recovery(tx, row['owner'], execution, 'Managed recovery budget exhausted')
                    return
                execution['disposition'] = 'retry'
            tx.put('execution', payload['job_id'], row['owner'], execution)

    async def failure(context, status, body, headers):
        await asyncio.to_thread(mark_retry, context.request_payload, status)

    @Serve(app).post(PATH, qstash_client=client(), receiver=receiver, url=url, retries=5, failure_function=failure)
    async def research(context):
        from .workflow_compat import normalize_history
        normalize_history(context)
        payload = context.request_payload
        # Deterministic control flow: all mutable reads/effects live in steps.
        index = payload.get('index', 0)
        for _ in range(segment_limit(context, payload)):
            current_index = index
            result = await context.run('phase-' + str(index), lambda: engine.step(payload, current_index))
            if result.get('cleanup_id'):
                cleanup_id = result['cleanup_id']
                await context.run('dispatch-cleanup-' + str(index), lambda: dispatch_cleanup(repository, cleanup_id))
            if result['done']:
                await context.run('dispatch-followups', lambda: dispatch_pending(repository))
                return
            index = result['index']
            if result.get('sleep'): await context.sleep('wait-' + str(index), result['sleep'])
        await context.run('continue-' + str(index), lambda: continue_job(repository, payload, index))

    async def authorized(request):
        body = (await request.body()).decode()
        suffix = next((path for path in (CONTROL_PATH, RECONCILE_PATH, CLEANUP_PATH) if request.url.path.endswith(path)), None)
        if suffix is None: return None
        try:
            receiver.verify(signature=request.headers.get('upstash-signature', ''), body=body,
                            url=url.removesuffix(PATH) + suffix)
        except Exception: return None
        try:
            value = json.loads(body or '{}')
            return value if isinstance(value, dict) else None
        except ValueError: return None

    @app.post(CONTROL_PATH, include_in_schema=False)
    async def control(request: Request):
        payload = await authorized(request)
        if payload is None: return JSONResponse({'error': 'Invalid workflow signature'}, status_code=401)
        try: return await engine.cancel(payload)
        except state.StaleExecution: return {'stale': True}

    @app.post(RECONCILE_PATH, include_in_schema=False)
    async def reconcile(request: Request):
        payload = await authorized(request)
        if payload is None: return JSONResponse({'error': 'Invalid workflow signature'}, status_code=401)
        # A busy database is not a failed tick: answer 200 so the scheduler does not retry; the next tick resumes.
        try:
            recovered, deferred, held = await asyncio.to_thread(sweep, repository)
            delivery = await dispatch_pending(repository)
            cleanup = await dispatch_cleanup(repository)
            from .workspace_events import reconcile_notifications
            notifications = await asyncio.to_thread(reconcile_notifications, repository)
        except DatabaseBusy:
            log.warning('Reconciliation deferred: the application database is busy')
            return {'status': 'deferred'}
        # A held row never fails the tick: it is reported here, logged, and decided again after its hold.
        return {'status': 'deferred' if deferred else 'ok', 'recovered': recovered, **delivery, 'cleanup':cleanup,
                'notifications': notifications, 'needs_operator': held}

    @app.post(CLEANUP_PATH, include_in_schema=False)
    async def cleanup(request: Request):
        payload = await authorized(request)
        if payload is None: return JSONResponse({'error':'Invalid workflow signature'}, status_code=401)
        try: return await engine.cleanup(payload)
        except state.StaleExecution: return {'stale':True}
        except state.StepBusy: return JSONResponse({'busy':True},status_code=503)
    return engine
