"""One general agent job's content for admins: what was asked, the limits it ran under, and what came back.

The 15-second overview and the job detail report sizes only (telemetry.py, admin_jobs.py). This read is the one
place an admin sees the prompt, system prompt, question, claims, output schema and response. It has its own
assertion purpose (admin_content), names the admin, and writes one admin_content_read record per read before
anything is returned; a read that cannot be recorded returns nothing. The record holds who, which job and when,
never the content.

Never returned: the queue lease token, the remote handle, who an account credits (the queue's attribution), the
account's evidence package, and the job result's account document.
"""
import json
import logging
import re
from uuid import UUID

from .admin_jobs import redact
from .auth import Problem
from .box_agent import LIMITS, RESPONSE_CAPTURE_PATH, RESPONSE_FORMAT, TRIPLE_SLOTS
from .box_upload import capture_file_limit
from .jobs import TERMINAL
from .repository import now, uid
from .runtime_config import artifacts_root

ACCESS_FORMAT = 'reveal.admin-content-read/1'
ACCESS_OWNER = 'admin-console'
# The response record of attempt N, relative to the job's workspace.
RESPONSE_PATH = 'attempt-{}/output/' + RESPONSE_CAPTURE_PATH
MAX_RESPONSE_BYTES = capture_file_limit(RESPONSE_CAPTURE_PATH)
INPUT_SETTINGS = ('format', 'prompt', 'system_prompt', 'tools', 'graphs', 'network', 'allowed_domains', 'effort', 'model',
                  'deliverable', 'claims_repository')
# Claude Code's result subtypes and the runner's reasons for each limit (box_remote.provider_failure_reason,
# box_remote.deadline_reason, job_failures.authoring_failure).
TURNS_REASON = re.compile(r'reached its \d+-turn (?:execution )?limit')
DEADLINE_REASON = re.compile(r'reached its \d+-second execution limit')


def frozen_input(inputs):
    """The frozen input by allowlist: every setting and limit, the question and claims of an account job, and the
    output schema. The account's evidence package is not a request setting and is left out."""
    value = {key: inputs.get(key) for key in INPUT_SETTINGS}
    value['output_schema'] = inputs.get('output_schema')
    limits = inputs.get('limits')
    value['limits'] = {key: limits.get(key) for key in LIMITS} if isinstance(limits, dict) else None
    account = inputs.get('account')
    if isinstance(account, dict):
        claims = account.get('claims') if isinstance(account.get('claims'), list) else []
        # Inputs frozen before claims became Propositions hold each claim as its statement alone.
        claims = [{'statement': claim} if isinstance(claim, str) else claim for claim in claims]
        value['account'] = {'question': account.get('question'), 'attribute_requester': account.get('attribute_requester'),
                            'claims': [{key: claim.get(key) for key in ('statement', 'proposition_id', *TRIPLE_SLOTS) if key in claim}
                                       for claim in claims if isinstance(claim, dict)]}
    else:
        value['account'] = None
    return value


def number(value):
    return value if type(value) in (int, float) else None


def requested_limits(queue, frozen, policy, accounting):
    """{limit: (requested, basis, differs_from_current_default)}.

    Jobs queued from this change on record which limits the request named (queue.requested_limits). For earlier
    jobs nothing stored tells a requested value from a default: requested is None, and the frozen value is only
    compared with today's default, which may itself have changed since the job was queued."""
    recorded = queue.get('requested_limits')
    result = {}
    for key in LIMITS:
        if isinstance(recorded, list):
            result[key] = (key in recorded, 'recorded', None)
            continue
        default = policy[key]['ceiling'] if accounting and key in ('timeout_seconds', 'max_turns') else policy[key]['default']
        value = frozen.get(key) if isinstance(frozen, dict) else None
        result[key] = (None, 'not_recorded', None if value is None else value != default)
    return result


def stop_reason(job, agent, record, rows):
    """Why the run ended, in one line, and the limit it hit (or None)."""
    status, failure = job.get('status'), job.get('failure') or {}
    code, message = failure.get('code'), failure.get('message') or ''
    subtype = agent.get('subtype') or (record or {}).get('subtype')
    reason = ' '.join(text for text in (message, agent.get('reason')) if isinstance(text, str))
    def by(key):
        requested = rows[key]['requested']
        return 'request' if requested else 'default' if requested is False else 'unknown'
    def who(key):
        return {'request': ', which the request set.', 'default': ', the default it was given.',
                'unknown': '. Whether the request set it is not recorded for this job.'}[by(key)]
    if status not in TERMINAL:
        return {'limit': None, 'by': None, 'text': f"Not finished: {str(status).replace('_', ' ')}."}
    if code == 'AGENT_OUTPUT_TOKENS_EXCEEDED':
        cap = rows['max_output_tokens']['frozen']
        return {'limit': 'max_output_tokens', 'by': by('max_output_tokens'),
                'text': f'A response exceeded max_output_tokens ({cap:,})' + who('max_output_tokens')}
    if subtype == 'error_max_budget_usd' or code == 'AUTHORING_BUDGET_EXCEEDED':
        row = rows['max_budget_usd']
        applied, frozen = row['applied'], row['frozen']
        if applied is not None and frozen is not None and applied < frozen:
            # The dispatch clamp lowered the job's budget to the admin ceiling.
            return {'limit': 'max_budget_usd', 'by': 'admin_ceiling',
                    'text': f'Hit the admin budget ceiling (${applied:.2f}); the job was frozen with ${frozen:.2f}.'}
        budget = applied if applied is not None else frozen
        return {'limit': 'max_budget_usd', 'by': by('max_budget_usd'),
                'text': f'Hit its budget (${budget:.2f})' + who('max_budget_usd')}
    if subtype == 'error_max_turns' or TURNS_REASON.search(reason):
        turns = rows['max_turns']['applied'] or rows['max_turns']['frozen']
        return {'limit': 'max_turns', 'by': by('max_turns'), 'text': f'Stopped at its own max_turns ({turns})' + who('max_turns')}
    elapsed, limit = agent.get('elapsed_seconds'), rows['timeout_seconds']['applied'] or rows['timeout_seconds']['frozen']
    if DEADLINE_REASON.search(reason) or (status == 'failed' and subtype is None and number(elapsed) is not None
                                          and number(limit) is not None and elapsed >= limit - 1):
        return {'limit': 'timeout_seconds', 'by': by('timeout_seconds'), 'text': f'Timed out at {limit} s' + who('timeout_seconds')}
    if status == 'succeeded':
        return {'limit': None, 'by': None, 'text': 'Finished normally.'}
    if status == 'cancelled':
        return {'limit': None, 'by': None, 'text': 'Cancelled.'}
    detail = f'{code}: {message}' if code and message else code or message or 'no failure was recorded'
    return {'limit': None, 'by': None, 'text': f"Ended {str(status).replace('_', ' ')} ({detail}). No limit was identified as the cause."}


def limits_table(queue, frozen, policy, accounting, agent, result, record, failure):
    """One row per limit: requested, frozen, ceiling (at queue time when recorded, else today's), what the run was
    given (applied: the dispatch clamp may lower a frozen budget) and what it used."""
    asked = requested_limits(queue, frozen, policy, accounting)
    ceilings = queue.get('limit_ceilings')
    recorded_ceilings = isinstance(ceilings, dict)
    record = record or {}
    def first(*values): return next((value for value in map(number, values) if value is not None), None)
    turns = first(agent.get('turns'), result.get('turns'), record.get('num_turns'))
    cost = first(agent.get('cost_usd'), result.get('cost_usd'), record.get('cost_usd'))
    estimated = number(agent.get('estimated_cost_usd')) if cost is None else None
    seconds = first(agent.get('elapsed_seconds'), result.get('duration_seconds'))
    used = {'timeout_seconds': seconds, 'max_turns': turns, 'max_budget_usd': cost if cost is not None else estimated,
            # Claude Code counts every tool call, built-in ones included; the limit counts REVEAL's tools only.
            'tool_calls': None, 'max_output_tokens': None}
    applied = {'timeout_seconds': number(agent.get('time_limit_seconds')), 'max_turns': number(agent.get('turn_limit')),
               'max_budget_usd': first(((failure or {}).get('budget') or {}).get('limit_usd'), agent.get('budget_usd')),
               'tool_calls': None, 'max_output_tokens': None}
    rows = {}
    for key in LIMITS:
        requested, basis, differs = asked[key]
        ceiling = ceilings.get(key) if recorded_ceilings else policy[key]['ceiling']
        rows[key] = {'key': key, 'requested': requested, 'requested_basis': basis, 'differs_from_current_default': differs,
                     'frozen': frozen.get(key) if isinstance(frozen, dict) else None,
                     'ceiling': ceiling, 'ceiling_basis': 'queue_time' if recorded_ceilings else 'current',
                     'applied': applied[key], 'used': used[key], 'hit': False}
    rows['max_budget_usd']['used_estimated'] = cost is None and estimated is not None
    rows['tool_calls']['all_tool_calls'] = number(agent.get('tool_calls'))
    return rows


def latest_attempt(tx, job_id):
    """The job's newest attempt number, or None: legacy attempt rows, or the durable execution's authoring attempt. Only
    this attempt's response is read, so the answer shown is the one the stop reason describes."""
    values = tx.execute("SELECT JSON_EXTRACT(payload,'$.attempt') FROM reveal_records WHERE kind=%s AND id>%s AND id<%s",
                        ('attempt', job_id + ':', job_id + ';')).fetchall()
    attempts = {int(row[0]) for row in values if row[0] is not None and re.fullmatch(r'[1-9][0-9]{0,5}', str(row[0]))}
    execution = tx.get('execution', job_id)
    current = execution['data'].get('authoring_attempt') if execution else None
    if type(current) is int and 0 < current <= 100000: attempts.add(current)
    return max(attempts, default=None)


def checked_record(raw, job_id, attempt):
    record = json.loads(raw)
    if not (isinstance(record, dict) and record.get('format') == RESPONSE_FORMAT and record.get('job_id') == job_id
            and record.get('attempt') == attempt):
        raise ValueError('The response record belongs to another execution')
    return record


def local_response(job_id, attempt):
    """(record, None) from the artifact volume, or (None, why it is unavailable). Explicit names only; never a symlink."""
    base = artifacts_root()
    root = base / job_id
    if root.is_symlink() or not root.resolve().is_relative_to(base): return None, 'unavailable'
    if attempt is None: return None, 'not_captured'
    path = root / RESPONSE_PATH.format(attempt)
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != base and parent.is_relative_to(base)): return None, 'unavailable'
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()): return None, 'not_captured'
    try:
        with path.open('rb') as stream: raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES: return None, 'too_large'
        return checked_record(raw, job_id, attempt), None
    except (OSError, ValueError):
        return None, 'unreadable'


def s3_response(workspace, job_id, attempt):
    """(record, None) from the job's workspace checkpoint, or (None, why it is unavailable)."""
    from .artifact_store import store, StorageUnavailable
    if not workspace or attempt is None: return None, 'not_captured'
    storage = store()
    try:
        manifest = json.loads(storage.get(workspace))
        if manifest.get('format') != 'reveal.workspace/1': raise ValueError('Invalid checkpoint')
        item = next((item for item in manifest['files'] if item['path'] == RESPONSE_PATH.format(attempt)), None)
        if item is None: return None, 'not_captured'
        if item['storage']['size_bytes'] > MAX_RESPONSE_BYTES: return None, 'too_large'
        return checked_record(storage.get(item['storage']), job_id, attempt), None
    except (StorageUnavailable, ValueError, KeyError, TypeError):
        return None, 'unavailable'


def response_view(record, why, result):
    """The full answer from the captured response record, else the job result's copy (cut at 50,000 characters)."""
    files = [{'path': item.get('path'), 'size_bytes': item.get('size_bytes'), 'sha256': item.get('sha256')}
             for item in (result.get('files') or []) if isinstance(item, dict)]
    if record is not None:
        text = record.get('response') if isinstance(record.get('response'), str) else None
        structured = record.get('structured_output')
        view = {'source': 'captured', 'text': text, 'truncated': False, 'status': record.get('status'), 'subtype': record.get('subtype')}
    elif isinstance(result.get('response'), str):
        text, structured = result['response'], result.get('structured_output')
        view = {'source': 'job_result', 'text': text, 'truncated': bool(result.get('response_truncated')), 'status': None, 'subtype': None}
    else:
        text = structured = None
        view = {'source': None, 'text': None, 'truncated': False, 'status': None, 'subtype': None}
    # The agent wrote this text inside the sandbox: credentials are redacted as in every saved diagnostic.
    view.update(attempt=record.get('attempt') if record is not None else None, text=redact(text) if text is not None else None, characters=len(text) if text is not None else None,
                structured_output=redact(structured) if structured is not None else None, files=files,
                captured_unavailable=why if record is None else None)
    return view


def job_run(repository, job_id, admin, environ=None):
    """GET /internal/v1/admin/jobs/{id}/run. admin: the verified admin email the access record names."""
    from . import agent_limits
    from .job_metrics import KIND as METRICS
    try:
        if str(UUID(job_id)) != job_id: raise ValueError()
    except ValueError: raise Problem(404, 'JOB_NOT_FOUND', 'This job does not exist.') from None
    with repository.read_transaction(utc=True) as tx:
        row = tx.get('job', job_id)
        if not row: raise Problem(404, 'JOB_NOT_FOUND', 'This job does not exist.')
        job = row['data']
        queue_row = tx.get('queue', job_id)
        if job.get('kind') != 'agent' or not queue_row or not isinstance(queue_row['data'].get('inputs'), dict):
            raise Problem(404, 'NOT_AN_AGENT_JOB', 'Only general agent jobs have a request to show.')
        queue = queue_row['data']
        metrics = tx.get(METRICS, job_id)
        agent = metrics['data'].get('agent') if metrics and isinstance(metrics['data'].get('agent'), dict) else {}
        policy = agent_limits.policy(tx, environ)
        attempt = latest_attempt(tx, job_id)
        execution = tx.get('execution', job_id)
        from .artifact_store import s3_enabled
        remote = s3_enabled()
        workspace = ((execution['data'].get('workspace') if execution else None) or queue.get('workspace')) if remote else None
    # Storage reads stay outside the database transaction.
    record, why = s3_response(workspace, job_id, attempt) if remote else local_response(job_id, attempt)
    inputs = frozen_input(queue['inputs'])
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    failure = redact(job.get('failure')) if isinstance(job.get('failure'), dict) else None
    accounting = inputs['deliverable'] == 'scientific_account'
    rows = limits_table(queue, inputs['limits'], policy, accounting, agent, result, record, failure)
    stop = stop_reason({**job, 'failure': failure}, agent, record, rows)
    if stop['limit']: rows[stop['limit']]['hit'] = True
    # Whether one response reached the output cap: known once the run has ended.
    rows['max_output_tokens']['reached'] = stop['limit'] == 'max_output_tokens' if job.get('status') in TERMINAL else None
    view = {'job': {'id': job_id, 'owner': row['owner'], 'status': job.get('status'), 'stage': job.get('stage'),
                    'created_at': job.get('created_at'), 'updated_at': job.get('updated_at'), 'completed_at': job.get('completed_at'),
                    'failure': {'code': failure.get('code'), 'message': failure.get('message')} if failure else None},
            'input': inputs, 'limits': [rows[key] for key in LIMITS], 'stop': stop,
            'run': {'subtype': agent.get('subtype') or (record or {}).get('subtype'), 'status': agent.get('status'),
                    'reason': redact(agent.get('reason')) if isinstance(agent.get('reason'), str) else None,
                    'tool_calls': number(agent.get('tool_calls')), 'tool_failures': number(agent.get('tool_failures'))},
            'response': response_view(record, why, result)}
    read_at = now()
    try:
        # Recorded before the content leaves: a read that cannot be recorded is refused.
        repository.append('admin_content_read', uid(), ACCESS_OWNER,
                          {'format': ACCESS_FORMAT, 'admin': admin, 'job_id': job_id, 'route': 'jobs/run', 'read_at': read_at})
    except Exception:
        logging.getLogger('uvicorn.error').warning('admin_content_read not recorded job_id=%s', job_id)
        raise Problem(503, 'ACCESS_LOG_UNAVAILABLE', 'This read could not be recorded, so nothing was returned. Retry shortly.') from None
    logging.getLogger('uvicorn.error').info('admin_content_read job_id=%s', job_id)
    return {**view, 'read': {'by': admin, 'at': read_at}}
