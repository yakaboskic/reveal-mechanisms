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
from .repository import now, digest
from .workflow_execution import WorkflowExecution

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


async def dispatch_pending(repository, limit=25, *, qstash=None, job_id=None):
    delivered = failed = 0
    if jobs.transport() != 'workflow': return {'delivered': 0, 'failed': 0}
    config()
    for identity, intent in await asyncio.to_thread(pending, repository, 'workflow_dispatch', min(limit, 100), job_id):
        if intent.get('published_at') or intent.get('next_attempt_at', '') > now(): continue
        try:
            await trigger(intent, qstash=qstash)
            await asyncio.to_thread(acknowledge_dispatch, repository, identity, intent)
            delivered += 1
        except Exception as exc:
            failed += 1
            await asyncio.to_thread(retry_dispatch, repository, identity, intent, type(exc).__name__)
            log.warning('Workflow dispatch remains pending: %s', type(exc).__name__)
    for identity, intent in await asyncio.to_thread(pending, repository, 'workflow_control', min(limit, 100), job_id):
        if intent.get('published_at'): continue
        try:
            async with asyncio.timeout(15):
                await (qstash or client()).message.publish_json(url=config().removesuffix(PATH) + CONTROL_PATH,
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


async def dispatch_job(repository, job_id):
    return await dispatch_pending(repository, limit=1, job_id=job_id)


def reconcile_stale(repository, limit=25):
    recovered = 0
    from .user_inputs import cleanup
    with repository.transaction() as tx: cleanup(tx)
    # Read indexed execution pages ordered by oldest update. Completed records
    # move to an archive kind at completion in a future compaction migration;
    # SQL JSON filtering keeps terminal records from starving active work now.
    with repository.read_transaction() as tx:
        expr = "json_extract(payload,'$.disposition')" if tx.sqlite else "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.disposition'))"
        namespace_expr = "json_extract(payload,'$.namespace')" if tx.sqlite else "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.namespace'))"
        rows = tx.execute('SELECT id FROM reveal_records WHERE kind=%s AND ' + namespace_expr + '=%s AND ' + expr +
            ' NOT IN (%s,%s) ORDER BY updated_at,id LIMIT %s', ('execution', jobs.namespace(), 'complete', 'recovery_required', min(limit, 100))).fetchall()
    for (identity,) in rows:
        with repository.transaction() as tx:
            row = tx.get('execution', identity); execution = row['data']
            if execution.get('namespace') != jobs.namespace(): continue
            if (execution.get('lease_until') or '') > now() or execution.get('expected_at', '') > now(): continue
            if tx.get('workflow_dispatch', identity): continue
            # A missing scheduler delivery is not evidence that a phase failed.
            # Only a retry explicitly recorded by the execution engine spends
            # the failure budget; planned handoffs spend neither counter.
            step_failure = execution.get('retry_cause') == 'step_failure'
            recoveries = execution.get('recoveries', 0) + int(step_failure)
            if step_failure and recoveries > int(os.getenv('REVEAL_WORKFLOW_MAX_RECOVERIES', '3')):
                state.require_recovery(tx, row['owner'], execution, 'Managed recovery budget exhausted')
                continue
            execution.update(generation=execution['generation'] + 1, recoveries=recoveries,
                delivery_recoveries=execution.get('delivery_recoveries', 0) + int(not step_failure),
                retry_cause=None)
            queue_generation(tx, row['owner'], execution)
            recovered += 1
    return recovered


def mount_workflow(app, repository):
    """Mount once. Missing signatures fail startup when workflow routing is on."""
    if jobs.transport() != 'workflow': return
    from qstash import Receiver
    from upstash_workflow.fastapi import Serve
    url = config(); receiver = Receiver(os.environ['QSTASH_CURRENT_SIGNING_KEY'], os.environ['QSTASH_NEXT_SIGNING_KEY'])
    engine = WorkflowExecution(repository)

    async def failure(context, status, body, headers):
        payload = context.request_payload
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
        recovered = await asyncio.to_thread(reconcile_stale, repository)
        delivery = await dispatch_pending(repository)
        cleanup = await dispatch_cleanup(repository)
        from .vector_workflow import dispatch_pending as dispatch_vectors
        vector_delivery = await dispatch_vectors(repository)
        from .workspace_events import reconcile_notifications
        notifications = await asyncio.to_thread(reconcile_notifications, repository)
        return {'recovered': recovered, **delivery, 'cleanup':cleanup, 'vector_delivery': vector_delivery, 'notifications': notifications}

    @app.post(CLEANUP_PATH, include_in_schema=False)
    async def cleanup(request: Request):
        payload = await authorized(request)
        if payload is None: return JSONResponse({'error':'Invalid workflow signature'}, status_code=401)
        try: return await engine.cleanup(payload)
        except state.StaleExecution: return {'stale':True}
        except state.StepBusy: return JSONResponse({'busy':True},status_code=503)
    return engine
