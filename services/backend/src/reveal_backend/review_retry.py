"""Legacy review retries validate and save a checksum-verified authoring capture."""
from copy import deepcopy

from .agent_execution import ExecutionRequest
from .auth import Problem
from .box_adapter import CAPTURE_MARKER, captured_result, read_capture_marker
from .evidence_package import require, sha256
from .runtime_config import artifacts_root

REVIEW_FAILURES = {'REVIEW_UNAVAILABLE', 'REVIEW_BUDGET_EXCEEDED'}


def replay_capture(request, source):
    marker_path = request.output_dir / CAPTURE_MARKER
    require(sha256(marker_path.read_bytes()) == source['capture_sha256'], 'Saved authoring capture changed')
    handle = {'box_id': source['box_id']}
    marker = read_capture_marker(request, handle)
    require(marker and marker['cleanup_complete'] and marker['state']['status'] == 'succeeded',
            'Saved-output validation requires a completed, cleaned-up authoring capture')
    result = captured_result(request, handle, marker)
    require(bool(result.account_paths) if request.kind == 'research' else result.paragraph_path is not None,
            'Saved authoring output is unavailable')
    return result


def prepare_source(tx, job, queue):
    """Authorize from database state; never accept caller-supplied file paths."""
    snapshot = queue.get('dispatch_input')
    try:
        require(snapshot and snapshot['mode'] == 'box' and snapshot['kind'] == job['kind'], 'Missing frozen dispatch')
        from .artifact_store import s3_enabled
        if s3_enabled():
            # Verify bytes after worker restore, outside the API write lock.
            source=deepcopy(queue.get('review_source') or queue.get('review_capture'))
            require(queue.get('workspace') and source and source.get('capture_sha256'), 'Missing durable review capture')
            if source.get('cleanup_id'):
                from .workflow_state import cleanup_record
                row=cleanup_record(tx,source['cleanup_id'])
                require(queue.get('transport')=='workflow' and row and row['owner']==job['owner_user_id'], 'Missing workflow cleanup capture')
                value=row['data']
                require(value['namespace']==queue['namespace'] and value['job_id']==job['id'] and
                    value['authoring_attempt']==source['attempt'] and value['box_id']==source['box_id'] and
                    value['capture_sha256']==source['capture_sha256'] and value.get('workspace'), 'Workflow cleanup capture binding differs')
            return source
        root = (artifacts_root() / job['id']).resolve()
        path = (root / snapshot['path']).resolve()
        require(path.is_relative_to(root) and sha256(path.read_bytes()) == snapshot['sha256'], 'Saved input changed')
        source = deepcopy(queue.get('review_source'))
        attempt = source['attempt'] if source else queue['attempt']
        require(type(attempt) is int and attempt > 0, 'Invalid source attempt')
        output = root / f'attempt-{attempt}' / 'output'
        require(output.resolve().is_relative_to(root), 'Saved output escapes job')
        if source is None:
            handle = queue.get('remote_handle') or {}
            source = {'attempt': attempt, 'box_id': handle['box_id'],
                      'capture_sha256': sha256((output / CAPTURE_MARKER).read_bytes())}
        selected = ()
        if job['kind'] == 'analysis':
            from .auth import owned
            frozen = owned(tx, 'request', job['research_request_id'], job['owner_user_id'])['data']
            selected = tuple(frozen['composer']['selected_kgs'])
        request = ExecutionRequest(job_id=job['id'], attempt=attempt,
            kind='research' if job['kind'] == 'analysis' else 'paragraph',
            input_path=path, output_dir=output, selected_graphs=selected)
        replay_capture(request, source)
        return source
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise Problem(409, 'REVIEW_CAPTURE_UNAVAILABLE',
            'The saved authoring output could not be verified. Validation was not started; no research agent was launched.') from exc


def require_current_reference(tx, job):
    """Accepting saved output would mint accounts from the job's frozen reference data.

    Blocked while a reference reload holds the gate, and for analysis jobs whose
    request was frozen on a superseded generation. Legacy mode (no active
    generation record) keeps today's behaviour; paragraph jobs need no reference data.
    """
    if job['kind'] != 'analysis': return
    from .reference_generation import ReferenceError, generation_of_anchors, read_gate
    if read_gate(tx):
        raise Problem(503, 'REFERENCE_RELOAD_IN_PROGRESS', 'Reference data is being reloaded. Retry validation shortly.')
    from .analysis_outcomes import active_reference_generation
    active = active_reference_generation(tx)
    if not active: return
    row = tx.get('request_binding', job.get('research_request_id') or '')
    binding = row['data'] if row and row['owner'] == job['owner_user_id'] else {}
    try: generation = generation_of_anchors(binding.get('anchors'))
    except ReferenceError: generation = None
    # Fail closed: a binding whose generation cannot be shown current is not retried.
    if generation != active:
        raise Problem(409, 'REFERENCE_GENERATION_SUPERSEDED',
            'This analysis used a superseded reference generation. Start a new analysis on this gap with current factors.')


def enqueue_review(tx, job, expected_event_id):
    from . import jobs
    if job['status'] != 'failed' or (job.get('failure') or {}).get('code') not in REVIEW_FAILURES:
        raise Problem(409, 'REVIEW_RETRY_UNAVAILABLE', 'Only output retained after an incomplete legacy review can be validated and saved through this action.')
    if job['last_event_id'] != expected_event_id:
        raise Problem(409, 'JOB_CHANGED', 'This job changed. Refresh its status before validating saved output.')
    require_current_reference(tx, job)
    row = tx.get('queue', job['id'])
    if not row or row['owner'] != job['owner_user_id']:
        raise Problem(409, 'REVIEW_CAPTURE_UNAVAILABLE', 'The saved execution is unavailable.')
    queue = row['data']
    source = prepare_source(tx, job, queue)
    queue.update(review_source=source, remote_handle=None, token=None, lease_until=None, new_attempt=True)
    jobs.dispatch(tx,job,queue)
    job.update(status='queued', stage='validating', failure=None, result=None, completed_at=None)
    jobs.update_paragraph_state(tx, job)
    jobs.event(tx, job, 'status', 'Validation queued using saved output. No new research or AI review will run.')
    return job
