"""RDS-owned workflow state, transactional dispatch, short leases and fences.

This module never connects to Redis. Scheduler history is advisory: only this
record can authorize a phase, paid creation, review, or outcome commit.
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
    old = tx.get('execution', job['id'])
    previous = old['data'] if old else {}
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
        'cleanup_complete': reviewing, 'capture_complete': reviewing, 'capacity_reserved': False,
        'created_at': now(), 'updated_at': now(), 'expected_at': after(60),
        'disposition': 'ready', 'cancel_requested': False,
        'review_checkpoint': None, 'review_index': 0,
        'validated_paths': previous.get('validated_paths') if reviewing else None,
    }
    queue['attempt'] = execution['authoring_attempt']
    tx.put('execution', job['id'], job['owner_user_id'], execution)
    tx.put('workflow_dispatch', job['id'], job['owner_user_id'], {
        'job_id': job['id'], 'namespace': execution['namespace'], 'generation': execution['generation'],
        'dispatch_id': queue['dispatch_id'], 'run_id': 'reveal-' + digest([job['id'], execution['generation']])[:40],
        'published_at': None, 'attempts': 0, 'created_at': now(), 'next_attempt_at': now(),
    })
    return execution


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


def needs_scratch(execution):
    return execution['phase'] not in ('create', 'observe', 'launch', 'complete') and not (
        execution['phase'] == 'cleanup' and execution.get('abandoned'))


def acquire(repository, payload, index, *, lease_seconds=600):
    """Acquire exactly the persisted next phase, or replay its committed result."""
    key = digest([payload['job_id'], payload['generation'], index])
    with repository.transaction() as tx:
        row = tx.get('execution', payload['job_id'])
        if not row: raise StaleExecution('Execution is unavailable')
        state = row['data']; check(state, payload)
        cached = tx.get('workflow_step', key)
        if cached: return None, None, cached['data']['result']
        if state['disposition'] == 'recovery_required': raise RecoveryRequired('Execution needs explicit operator recovery')
        if index != state['phase_index']: raise StaleExecution('Workflow phase index is stale')
        if state.get('lease_until') and state['lease_until'] > now(): raise StepBusy('Another request owns this step')
        # Separate short-running preparation and validation limits from the
        # durable active-Box cap. All replicas share these RDS reservations.
        groups = {'prepare': 'preparation', 'bootstrap': 'preparation', 'validate': 'review',
                  'review_init': 'review', 'review_call': 'review', 'review_tools': 'review', 'commit': 'review', 'capture': 'capture'}
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
        return job, dict(state, deferred=deferred), None


def save(repository, payload, token, **updates):
    with repository.transaction() as tx:
        owner, state = owned(tx, payload, token)
        state.update(updates, updated_at=now())
        tx.put('execution', payload['job_id'], owner, state)
    return state


def complete(repository, payload, token, *, next_phase, sleep=0, done=False, **updates):
    with repository.transaction() as tx:
        owner, state = owned(tx, payload, token)
        if done:
            from . import jobs
            # Cancellation can win between a phase's read and jobs.finish.
            # Resolve it in this final transaction before retiring scheduling.
            if updates.get('capacity_reserved', state.get('capacity_reserved')):
                raise StaleExecution('Cannot complete while Box cleanup is unresolved')
            current = tx.get('job', payload['job_id'])['data']; current['owner_user_id'] = owner
            if current['status'] == 'cancel_requested':
                current.update(status='cancelled', completed_at=now(), result=None, failure=None)
                jobs.update_paragraph_state(tx, current)
                jobs.event(tx, current, 'status', 'Stopped by the workspace owner.')
            if current['status'] not in jobs.TERMINAL:
                raise StaleExecution('An execution cannot finish before its authoritative job outcome')
        result = {'phase': next_phase, 'index': state['phase_index'] + 1, 'sleep': sleep, 'done': done}
        tx.put('workflow_step', state['step'], owner, {'job_id': payload['job_id'], 'generation': payload['generation'],
            'phase': state['phase'], 'index': state['phase_index'], 'result': result, 'completed_at': now()})
        state.update(updates, phase=next_phase, phase_index=result['index'], fence=None, lease_until=None,
            step=None, updated_at=now(), expected_at=after(sleep + 120), disposition='complete' if done else 'ready')
        tx.put('execution', payload['job_id'], owner, state)
        queue = tx.get('queue', payload['job_id'])['data']; queue.update(token=None, lease_until=None)
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
        active = sum(bool(r['data'].get('capacity_reserved')) for r in tx.list('execution')
                     if r['data']['namespace'] == state['namespace'])
        # Include legacy assignments through a deliberate migration overlap.
        active += sum(bool(r['data'].get('remote_handle')) and r['data']['remote_handle'].get('phase') != 'deleted'
                      for r in tx.list('queue') if r['data'].get('transport') != 'workflow'
                      and r['data'].get('namespace', 'reveal') == state['namespace'])
        if active >= maximum: raise StepBusy('Active Box capacity is reserved')
        state.update(capacity_reserved=True, creation_intent=uid(), updated_at=now())
        tx.put('execution', payload['job_id'], owner, state)
        return state
