"""Per-job cost and failure telemetry for operators.

One `job_metrics` record per job, keyed by job id. It holds numbers copied from
the Box runner's terminal runtime record and the exception type and bounded,
redacted message of a failed phase, never scientific content, prompts, tool
arguments or credentials. Writes are best effort: telemetry never changes a job's
outcome. Admin telemetry reads these rows; public job responses do not.
"""
import logging
import math

from .box_timing import estimated_cost_usd
from .repository import digest, now

log = logging.getLogger(__name__)
KIND = 'job_metrics'
FORMAT = 'reveal.job-metrics/1'
MESSAGE_LIMIT = 500


def number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return int(value) if integer and float(value).is_integer() else (None if integer else value)


def text(value, limit=300):
    return value[:limit] if isinstance(value, str) and value else None


def agent_metrics(runtime):
    """Allowlisted projection of a captured `runtime.json`; unknown values stay null, never zero."""
    runtime = runtime if isinstance(runtime, dict) else {}
    completion = runtime.get('completion') if isinstance(runtime.get('completion'), dict) else {}
    reported = completion.get('provider_reported') if isinstance(completion.get('provider_reported'), dict) else {}
    usage = reported.get('usage') if isinstance(reported.get('usage'), dict) else {}
    limits = runtime.get('execution_limits') if isinstance(runtime.get('execution_limits'), dict) else {}
    timing = completion.get('timing') if isinstance(completion.get('timing'), dict) else {}
    tools = timing.get('tools') if isinstance(timing.get('tools'), dict) else {}
    authoring = completion.get('authoring_timing') if isinstance(completion.get('authoring_timing'), dict) else {}
    calls = [c for c in authoring.get('calls', []) if isinstance(c, dict)] if isinstance(authoring.get('calls'), list) else []
    cost = number(completion.get('cost_usd'))
    observed = timing.get('usage') if isinstance(timing.get('usage'), dict) else None
    # Priced from streamed token counts; the only figure for runs stopped before the provider reported a total.
    estimate = estimated_cost_usd(runtime.get('model'), observed) if observed and observed.get('messages') else None
    budget = number(completion.get('max_budget_usd')) or number(limits.get('max_budget_usd'))
    spent = cost if cost is not None else estimate
    api_ms = number(reported.get('duration_api_ms'))
    # Provider totals first; a run stopped before reporting falls back to the tokens observed in its stream.
    counted = usage or (observed if estimate is not None else {})
    tokens = {name: number(counted.get(field), integer=True) for name, field in (
        ('input', 'input_tokens'), ('output', 'output_tokens'),
        ('cache_write', 'cache_creation_input_tokens'), ('cache_read', 'cache_read_input_tokens'))}
    return {
        'model': text(runtime.get('model'), 80),
        'status': text(completion.get('status'), 40),
        'subtype': text(completion.get('provider_result_subtype'), 60),
        'reason': text(completion.get('reason')),
        'cost_usd': cost,
        'estimated_cost_usd': estimate,
        'cost_source': 'provider' if cost is not None else 'estimated' if estimate is not None else 'unreported',
        'budget_usd': budget,
        'budget_used': round(spent / budget, 4) if spent is not None and budget else None,
        'turns': number(completion.get('turns_used'), integer=True),
        'turn_limit': number(completion.get('turn_limit'), integer=True) or number(limits.get('max_turns'), integer=True),
        'elapsed_seconds': number(completion.get('elapsed_seconds')),
        'time_limit_seconds': number(completion.get('time_limit_seconds')) or number(limits.get('timeout_seconds')),
        'api_seconds': round(api_ms / 1000, 3) if api_ms is not None else None,
        'tokens': tokens if any(v is not None for v in tokens.values()) else None,
        'tool_calls': number(tools.get('completed'), integer=True),
        'tool_failures': number(tools.get('failed'), integer=True),
        'lint_checks': sum(c.get('tool') == 'lint_account' for c in calls) if calls else None,
        'draft_writes': sum(c.get('tool') == 'write_account_draft' for c in calls) if calls else None,
    }


def failure_metrics(phase, exc):
    from .admin_jobs import redact
    return {'phase': text(phase, 40), 'error_type': type(exc).__name__,
            'message': redact(str(exc))[:MESSAGE_LIMIT] or None, 'recorded_at': now()}


def record(repository, job, *, agent=None, error=None):
    """Merge this job's metrics. Returns False instead of raising: telemetry never fails a job."""
    try:
        with repository.transaction() as tx:
            stored = tx.get('job', job['id'])
            if not stored: return False
            owner, current = stored['owner'], stored['data']
            existing = tx.get(KIND, job['id'])
            data = existing['data'] if existing else {'format': FORMAT, 'job_id': job['id']}
            data.update(kind=current.get('kind'), created_at=current.get('created_at'), recorded_at=now())
            if current.get('kind') == 'paragraph' and current.get('input_account_id') and 'parent_job_id' not in data:
                account = tx.get('account', digest([owner, current['input_account_id']]))
                parent = account['data'].get('summary', {}).get('job_id') if account and account['owner'] == owner else None
                data['parent_job_id'] = parent if isinstance(parent, str) else None
            if agent is not None: data['agent'] = agent
            if error is not None: data['error'] = error
            tx.put(KIND, job['id'], owner, data)
        return True
    except Exception as exc:  # A metrics write must never become the job's failure.
        log.warning('Job metrics were not recorded for %s (%s)', job.get('id'), type(exc).__name__)
        return False
