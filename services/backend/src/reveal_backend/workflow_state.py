"""RDS-owned workflow state, transactional dispatch, short leases and fences.

This module never connects to Redis. Scheduler history is advisory: only this
record can authorize a phase, paid creation, validation, or outcome commit.
"""
from datetime import datetime, timedelta, timezone
import os
from .repository import now, uid, digest

VERSION = 1

class StaleExecution(RuntimeError): pass
class StepBusy(RuntimeError): pass
class RecoveryRequired(RuntimeError): pass


def after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def create(tx, job, queue):
    """(Re)dispatch a job's execution, over whatever execution it already has (review retries)."""
    old = tx.get('execution', job['id'])
    rows = build(job, queue, old['data'] if old else {})
    for row in rows: tx.put(*row)
    return rows[0][3]


def build(job, queue, previous):
    """The execution and workflow_dispatch rows for a job whose prior execution is `previous`; no I/O."""
    reviewing = bool(queue.get('review_source'))
    source = queue.get('review_source') or {}
    authoring_attempt = source.get('attempt', previous.get('authoring_attempt', 1)) if reviewing else 1
    captured_box = previous.get('box')
    if reviewing and not captured_box:
        captured_box = {'box_id': source['box_id'], 'job_id': job['id'], 'attempt': authoring_attempt,
                        'phase': 'deleted', 'cursor': 0, 'timings': {}}
    execution = {
        'job_id': job['id'], 'version': VERSION, 'namespace': queue['namespace'],
        'generation': previous.get('generation', 0) + 1, 'run_id': None,
        'authoring_attempt': authoring_attempt,
        'review_attempt': previous.get('review_attempt', 0) + 1,
        'phase': 'validate' if reviewing else 'prepare', 'phase_index': 0,
        'fence': None, 'lease_until': None, 'step': None,
        'workspace': queue.get('workspace') if reviewing else None,
        'box': captured_box if reviewing else None,
        'cleanup_complete': previous.get('cleanup_complete', False) if reviewing and source.get('cleanup_id') else reviewing,
        'capture_complete': reviewing,
        'capacity_reserved': previous.get('capacity_reserved', False) if reviewing and source.get('cleanup_id') else False,
        'created_at': now(), 'updated_at': now(), 'expected_at': after(60),
        'disposition': 'ready', 'cancel_requested': False,
        'review_checkpoint': None, 'review_index': 0,
        # Revalidate saved output against the current deterministic gates.
        'validated_paths': None,
    }
    if reviewing and source.get('cleanup_id'):
        execution.update(cleanup_id=source['cleanup_id'], capture_sha256=source['capture_sha256'])
    queue['attempt'] = execution['authoring_attempt']
    return [('execution', job['id'], job['owner_user_id'], execution),
            ('workflow_dispatch', job['id'], job['owner_user_id'], {
        'job_id': job['id'], 'namespace': execution['namespace'], 'generation': execution['generation'],
        'dispatch_id': queue['dispatch_id'], 'run_id': 'reveal-' + digest([job['id'], execution['generation']])[:40],
        'published_at': None, 'attempts': 0, 'created_at': now(), 'next_attempt_at': now(),
    })]


def check(execution, payload):
    from .jobs import namespace
    if (execution['namespace'] != namespace() or execution['version'] != VERSION or execution['generation'] != payload.get('generation')
            or execution['namespace'] != payload.get('namespace') or execution['job_id'] != payload.get('job_id')):
        raise StaleExecution('Workflow identity, namespace or generation is stale')


def owned(tx, payload, token, *, terminal=False):
    row = tx.get('execution', payload['job_id'])
    if not row: raise StaleExecution('Execution is unavailable')
    state = row['data']; check(state, payload)
    if state.get('fence') != token or not state.get('lease_until') or state['lease_until'] <= now():
        raise StaleExecution('Workflow step fence was lost')
    return row['owner'], state


def stored_bootstrap(execution):
    descriptor = (execution.get('dispatch_input') or {}).get('bootstrap')
    return (isinstance(descriptor, dict) and descriptor.get('format') == 'reveal.box-bootstrap/1'
            and isinstance(descriptor.get('bundle'), dict) and descriptor['bundle'].get('store') == 's3')


def needs_scratch(execution):
    return execution['phase'] not in ('create', 'observe', 'launch', 'complete', 'review_init', 'review_call', 'review_tools') and not (
        execution['phase'] == 'bootstrap' and stored_bootstrap(execution)) and not (
        execution['phase'] == 'cleanup' and execution.get('abandoned')) and not (
        execution['phase'] == 'capture' and (execution.get('box') or {}).get('capture_protocol') == 's3-v1')


def cleanup_record(tx, identity):
    return tx.get('workflow_cleanup', identity) or tx.get('workflow_cleanup_completed', identity)


def cleanup_matches(value, execution):
    box = execution.get('box') or {}
    return (value.get('namespace') == execution['namespace'] and value.get('job_id') == execution['job_id']
            and value.get('authoring_attempt') == execution['authoring_attempt']
            and value.get('box_id') == box.get('box_id')
            and value.get('capture_sha256') == execution.get('capture_sha256')
            and bool(value.get('workspace')) and bool(execution.get('capture_complete')))


def has_cleanup_handoff(tx, execution):
    identity = execution.get('cleanup_id')
    row = cleanup_record(tx, identity) if identity else None
    return bool(row and cleanup_matches(row['data'], execution))


def capture_handed_off(repository, payload):
    with repository.read_transaction() as tx:
        row = tx.get('execution', payload['job_id'])
        if not row: raise StaleExecution('Execution is unavailable')
        check(row['data'], payload)
        return has_cleanup_handoff(tx, row['data'])


def enqueue_cleanup(tx, owner, execution, workspace, handle, capture_sha256):
    """Transfer deletion to an independent durable intent in the capture commit."""
    if (not isinstance(capture_sha256,str) or len(capture_sha256)!=64
            or any(character not in '0123456789abcdef' for character in capture_sha256)):
        raise StaleExecution('Cleanup requires a verified capture checksum')
    identity = digest(['box-cleanup-v1', execution['namespace'], execution['job_id'],
                       execution['authoring_attempt'], handle['box_id']])
    value = {'cleanup_id': identity, 'namespace': execution['namespace'], 'job_id': execution['job_id'],
        'authoring_attempt': execution['authoring_attempt'], 'box_id': handle['box_id'],
        'box': handle, 'workspace': workspace, 'capture_sha256': capture_sha256,
        'created_at': now(), 'next_attempt_at': now(), 'lease_until': None, 'token': None,
        'attempts': 0, 'dispatch_generation': 0, 'status': 'pending'}
    existing = cleanup_record(tx, identity)
    if existing:
        for key in ('namespace', 'job_id', 'authoring_attempt', 'box_id', 'capture_sha256', 'workspace'):
            if existing['data'][key] != value[key]: raise StaleExecution('Cleanup capture binding changed')
    else:
        tx.put('workflow_cleanup', identity, owner, value)
    return identity


def cleanup_owned(tx, payload):
    from .jobs import namespace
    if payload.get('namespace') != namespace(): raise StaleExecution('Cleanup namespace differs')
    row = cleanup_record(tx, payload.get('cleanup_id', ''))
    if not row or row['data']['namespace'] != namespace(): raise StaleExecution('Cleanup is unavailable')
    value = row['data']
    if value['box'].get('box_id') != value['box_id']: raise StaleExecution('Cleanup Box binding differs')
    expected = digest(['box-cleanup-v1', value['namespace'], value['job_id'], value['authoring_attempt'], value['box_id']])
    if expected != payload['cleanup_id']: raise StaleExecution('Cleanup identity differs')
    return row['owner'], value


def acquire_cleanup(repository, payload):
    with repository.transaction() as tx:
        owner, value = cleanup_owned(tx, payload)
        if value['status'] == 'deleted': return value
        if (value.get('lease_until') or '') > now(): raise StepBusy('Cleanup already owns its Box')
        value.update(token=uid(), lease_until=after(180), attempts=value['attempts'] + 1, status='deleting')
        tx.put('workflow_cleanup', payload['cleanup_id'], owner, value)
        return value


def finish_cleanup(repository, payload, token, handle=None, *, error=None):
    with repository.transaction() as tx:
        owner, value = cleanup_owned(tx, payload)
        if value['status'] == 'deleted': return value
        if value.get('token') != token or (value.get('lease_until') or '') <= now():
            raise StaleExecution('Cleanup lease changed')
        value.update(token=None, lease_until=None)
        if error:
            value.update(status='pending', next_attempt_at=after(30), last_error=error)
            tx.put('workflow_cleanup', payload['cleanup_id'], owner, value)
            return value
        if not handle or handle.get('box_id') != value['box_id'] or handle.get('phase') != 'deleted':
            raise StaleExecution('Cleanup did not acknowledge its assigned Box')
        value.update(status='deleted', box=handle, deleted_at=now())
        tx.put('workflow_cleanup_completed', payload['cleanup_id'], owner, value)
        tx.remove('workflow_cleanup', payload['cleanup_id'])
        # Main phase/fence/generation may have advanced or already terminated.
        # Update only the matching remote allocation; never restore stale state.
        row = tx.get('execution', value['job_id'])
        if row and cleanup_matches(value, row['data']) and row['data'].get('cleanup_id') == payload['cleanup_id']:
            execution = row['data']; execution.update(box=handle, cleanup_complete=True, capacity_reserved=False)
            tx.put('execution', value['job_id'], row['owner'], execution)
            queue = tx.get('queue', value['job_id'])
            if queue and (queue['data'].get('remote_handle') or {}).get('box_id') == value['box_id']:
                data = queue['data']; data['remote_handle'] = handle
                tx.put('queue', value['job_id'], queue['owner'], data)
        return value


def acquire(repository, payload, index, *, lease_seconds=600):
    """Acquire exactly the persisted next phase, or replay its committed result."""
    job, _, execution, replay = acquire_step(repository, payload, index, lease_seconds=lease_seconds)
    return job, execution, replay


def acquire_step(repository, payload, index, *, lease_seconds=600):
    """acquire() that also returns the queue row it leased: one read, then the execution and queue updates. The
    rows are the step's read-only snapshot; every later write re-reads its rows under the fence."""
    key = digest([payload['job_id'], payload['generation'], index])
    with repository.transaction() as tx:
        tx.get_records([('execution', payload['job_id']), ('workflow_step', key), ('job', payload['job_id']), ('queue', payload['job_id'])])
        row = tx.get('execution', payload['job_id'])
        if not row: raise StaleExecution('Execution is unavailable')
        state = row['data']; check(state, payload)
        cached = tx.get('workflow_step', key)
        if cached: return None, None, None, cached['data']['result']
        if state['disposition'] == 'recovery_required': raise RecoveryRequired('Execution needs explicit operator recovery')
        if index != state['phase_index']: raise StaleExecution('Workflow phase index is stale')
        if state.get('lease_until') and state['lease_until'] > now(): raise StepBusy('Another request owns this step')
        # Separate short-running preparation and validation limits from the
        # durable active-Box cap. All replicas share these RDS reservations.
        groups = {'prepare': 'preparation', 'bootstrap': 'preparation', 'validate': 'review',
                  'commit': 'review', 'capture': 'capture'}
        group = groups.get(state['phase']); deferred = False
        if group or needs_scratch(state):
            active = [item['data'] for item in tx.list('execution')
                      if (item['data'].get('lease_until') or '') > now()
                      and item['data']['namespace'] == state['namespace']]
        if needs_scratch(state):
            scratch_cap = int(os.getenv('REVEAL_MAX_SCRATCH_STEPS', '2'))
            if scratch_cap < 1: raise ValueError('Workflow scratch concurrency must be positive')
            deferred = sum(needs_scratch(item) for item in active) >= scratch_cap
        if group:
            cap = int(os.getenv({'preparation': 'REVEAL_MAX_PREPARATION_STEPS', 'review': 'REVEAL_MAX_REVIEW_STEPS', 'capture': 'REVEAL_MAX_CAPTURE_STEPS'}[group], '2'))
            if cap < 1: raise ValueError('Workflow stage concurrency must be positive')
            running = sum(groups.get(item['phase']) == group for item in active)
            deferred = deferred or running >= cap
        job = tx.get('job', payload['job_id'])['data']; job['owner_user_id'] = row['owner']
        token = uid()
        state.update(fence=token, lease_until=after(lease_seconds), step=key, updated_at=now())
        queue = tx.get('queue', payload['job_id'])['data']
        queue.update(token=token, lease_until=state['lease_until'], worker_id='workflow', attempt=state['authoring_attempt'])
        tx.put('execution', payload['job_id'], row['owner'], state)
        tx.put('queue', payload['job_id'], row['owner'], queue)
        return job, dict(queue), dict(state, deferred=deferred), None


def save(repository, payload, token, **updates):
    with repository.transaction() as tx:
        owner, state = owned(tx, payload, token)
        state.update(updates, updated_at=now())
        tx.put('execution', payload['job_id'], owner, state)
    return state


def complete(repository, payload, token, *, next_phase, sleep=0, done=False, observation=None, **updates):
    """Finish the step in one fenced transaction: one read, its records in one INSERT, then execution and queue.
    observation (an observe step's Box inspection) commits its cursor and events here too."""
    jid = payload['job_id']
    with repository.transaction() as tx:
        tx.get_records([('execution', jid), ('queue', jid), *([('job', jid)] if done else []),
                        *(observation.keys() if observation else [])])
        owner, state = owned(tx, payload, token)
        if done:
            from . import jobs
            # Cancellation can win between a phase's read and jobs.finish.
            # Resolve it in this final transaction before retiring scheduling.
            if updates.get('capacity_reserved', state.get('capacity_reserved')) and not has_cleanup_handoff(tx, state):
                raise StaleExecution('Cannot complete while Box cleanup is unresolved')
            current = tx.get('job', payload['job_id'])['data']; current['owner_user_id'] = owner
            if current['status'] == 'cancel_requested':
                current.update(status='cancelled', completed_at=now(), result=None, failure=None)
                jobs.update_paragraph_state(tx, current)
                jobs.event(tx, current, 'status', 'Stopped by the workspace owner.')
            if current['status'] not in jobs.TERMINAL:
                raise StaleExecution('An execution cannot finish before its authoritative job outcome')
        records = observation.apply(tx, owner, state) if observation else []
        result = {'phase': next_phase, 'index': state['phase_index'] + 1, 'sleep': sleep, 'done': done}
        if state['phase'] == 'capture' and state.get('cleanup_id'):
            result['cleanup_id'] = state['cleanup_id']
        # acquire proved the step key absent under this fence; an existing one is overwritten as put() did.
        records.append(('workflow_step', state['step'], owner, {'job_id': payload['job_id'], 'generation': payload['generation'],
            'phase': state['phase'], 'index': state['phase_index'], 'result': result, 'completed_at': now()}))
        tx.insert_new(records, ('workflow_step',))
        state.update(updates, phase=next_phase, phase_index=result['index'], fence=None, lease_until=None,
            step=None, updated_at=now(), expected_at=after(sleep + 120), disposition='complete' if done else 'ready')
        tx.put('execution', payload['job_id'], owner, state)
        queue = tx.get('queue', payload['job_id'])['data']; queue.update(token=None, lease_until=None)
        if observation: queue['remote_handle'] = observation.handle
        tx.put('queue', payload['job_id'], owner, queue)
        return result


def release(repository, payload, token, *, recovery=False, reason=None):
    with repository.transaction() as tx:
        owner, state = owned(tx, payload, token)
        state.update(fence=None, lease_until=None, updated_at=now(), expected_at=after(60),
            disposition='recovery_required' if recovery else 'retry', diagnostic=reason)
        tx.put('execution', payload['job_id'], owner, state)
        queue=tx.get('queue', payload['job_id'])['data']; queue.update(token=None, lease_until=None)
        tx.put('queue', payload['job_id'], owner, queue)


def reserve_box(repository, payload, token):
    with repository.transaction() as tx:
        owner, state = owned(tx, payload, token)
        if state.get('capacity_reserved'): return state
        maximum = int(os.getenv('REVEAL_MAX_ACTIVE_BOXES', '2'))
        if maximum < 1: raise ValueError('REVEAL_MAX_ACTIVE_BOXES must be positive')
        pending_cleanup = [r for r in tx.list('workflow_cleanup') if r['data']['namespace'] == state['namespace']]
        active = len(pending_cleanup)
        active += sum(bool(r['data'].get('capacity_reserved')) and not has_cleanup_handoff(tx, r['data'])
                      for r in tx.list('execution') if r['data']['namespace'] == state['namespace'])
        # Include legacy assignments through a deliberate migration overlap.
        active += sum(bool(r['data'].get('remote_handle')) and r['data']['remote_handle'].get('phase') != 'deleted'
                      for r in tx.list('queue') if r['data'].get('transport') != 'workflow'
                      and r['data'].get('namespace', 'reveal') == state['namespace'])
        if active >= maximum: raise StepBusy('Active Box capacity is reserved')
        state.update(capacity_reserved=True, creation_intent=uid(), updated_at=now())
        tx.put('execution', payload['job_id'], owner, state)
        return state
