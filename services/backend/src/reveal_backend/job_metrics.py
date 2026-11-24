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


def refusal_metrics(responses):
    """How often the provider's safety classifiers stopped a response in this run, and which models answered.
    Runs recorded before the runner counted this have every field null, never zero."""
    if not isinstance(responses, dict):
        return {'responses': None, 'refusals': None, 'refusal_categories': None, 'ended_on_refusal': None, 'models': None}
    categories = responses.get('refusal_categories') if isinstance(responses.get('refusal_categories'), dict) else {}
    models = responses.get('models') if isinstance(responses.get('models'), list) else []
    return {'responses': number(responses.get('ended'), integer=True),
            'refusals': number(responses.get('refused'), integer=True),
            'refusal_categories': {name: count for key, value in list(categories.items())[:16]
                                   if (name := text(key, 80)) and (count := number(value, integer=True))},
            'ended_on_refusal': responses.get('last_stop_reason') == 'refusal',
            'models': [name for model in models[:8] if (name := text(model, 80))]}


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
        **refusal_metrics(timing.get('responses')),
        'lint_checks': sum(c.get('tool') == 'lint_account' for c in calls) if calls else None,
        'draft_writes': sum(c.get('tool') == 'write_account_draft' for c in calls) if calls else None,
    }


def failure_metrics(phase, exc):
    from .admin_jobs import redact
    return {'phase': text(phase, 40), 'error_type': type(exc).__name__,
            'message': redact(str(exc))[:MESSAGE_LIMIT] or None, 'recorded_at': now()}


# The deposit screen's verdict (deposit_screen.screen): which check withheld the account, the classifier's score and
# threshold, and where the text was. Never the text itself.
SCREEN_FIELDS = ('outcome', 'check', 'location', 'score', 'threshold', 'classifier', 'windows', 'texts', 'quoted_commands', 'seconds')


def screen_metrics(verdict):
    verdict = verdict if isinstance(verdict, dict) else {}
    return {'outcome': text(verdict.get('outcome'), 20), 'check': text(verdict.get('check'), 40),
            'location': text(verdict.get('location')), 'score': number(verdict.get('score')),
            'threshold': number(verdict.get('threshold')), 'classifier': text(verdict.get('classifier'), 80),
            'windows': number(verdict.get('windows'), integer=True), 'texts': number(verdict.get('texts'), integer=True),
            'seconds': number(verdict.get('seconds'))}


# A scientific_account job's deposit outcome (claims_deposit.send): status, why it was not made, the receipt id and its
# counts. `inconsistent` is a receipt whose objects the repository holds otherwise than sent.
def deposit_metrics(deposit):
    deposit = deposit if isinstance(deposit, dict) else {}
    return {'status': text(deposit.get('status'), 40), 'reason': text(deposit.get('reason'), 40),
            'deposit_id': text(deposit.get('deposit_id'), 64), 'http_status': number(deposit.get('http_status'), integer=True),
            'objects': number(deposit.get('objects'), integer=True),
            'stored_differs': number(deposit.get('stored_differs'), integer=True),
            'unresolved_references': number(deposit.get('unresolved_references'), integer=True)}


# The last time the job's account could not be served back from the claims repository (claims_read): whether the
# repository was unreachable or answered with something other than the deposited account, and which check failed.
def serve_metrics(outcome, reason=None):
    return {'outcome': text(outcome, 20), 'reason': text(reason, 40), 'recorded_at': now()}


def record(repository, job, *, agent=None, error=None, screen=None, deposit=None, serve=None):
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
            if screen is not None: data['screen'] = screen_metrics(screen)
            if deposit is not None: data['deposit'] = deposit_metrics(deposit)
            if serve is not None: data['serve'] = serve
            tx.put(KIND, job['id'], owner, data)
        return True
    except Exception as exc:  # A metrics write must never become the job's failure.
        log.warning('Job metrics were not recorded for %s (%s)', job.get('id'), type(exc).__name__)
        return False
