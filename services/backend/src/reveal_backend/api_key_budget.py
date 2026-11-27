"""Spending budgets on API keys minted in the admin console (docs/api-keys.md, "Budgets").

A key may carry a budget: a dollar limit per calendar month (UTC) or in total. Every job a minted key starts is
listed in that key's usage record with the most its next agent run may cost (its reserve). A key's spend is:

  - for a finished job, what its runs cost (job_metrics charged_usd, which adds up every run, retries included);
    a finished job whose sandbox was created but whose run was never recorded (cancelled or lost mid-run) is
    charged its reserve, and one that never dispatched costs nothing;
  - for a job still in progress, what its runs have cost so far plus its reserve, since another run may follow;
  - for an analysis, also what the statement jobs written from its accounts cost.

A new job is refused (402 API_KEY_BUDGET_EXHAUSTED) when that spend plus the new job's reserve would pass the
limit, so the key never starts work its budget cannot cover. A run already in progress is never stopped: each run
is bounded by its own cap, which the reserve already counted. A run that reports no cost is charged its cap.
"""
from datetime import datetime, timezone
import math

from .auth import Problem
from .repository import Conflict, now

USAGE_KIND, OWNER, FORMAT = 'api_key_usage', 'admin-console', 'reveal.api-key-usage/1'
PERIODS = ('month', 'total')
MAX_LIMIT_USD = 100_000
MAX_JOBS = 20_000


class BudgetError(ValueError):
    """A budget the console can correct; the message is shown as sent."""


def parse(value):
    """A budget from a request: None (no budget) or {'limit_usd', 'period'}."""
    if value is None: return None
    if not isinstance(value, dict) or set(value) != {'limit_usd', 'period'}:
        raise BudgetError('A budget is {"limit_usd": <dollars>, "period": "month" or "total"}, or null for none.')
    limit, period = value['limit_usd'], value['period']
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or not 0 < limit <= MAX_LIMIT_USD:
        raise BudgetError(f'The budget must be more than $0 and at most ${MAX_LIMIT_USD:,}.')
    if period not in PERIODS:
        raise BudgetError('The budget period must be "month" (each calendar month, UTC) or "total".')
    return {'limit_usd': round(float(limit), 2), 'period': period}


def period_start(period, moment=None):
    """The first instant this period counts from, as stored timestamps sort; None for a total budget."""
    if period != 'month': return None
    moment = moment or datetime.now(timezone.utc)
    # No 'Z': stored stamps carry fractional seconds, and '.' sorts before 'Z', so a job in the month's first second
    # would otherwise fall into the month before.
    return moment.strftime('%Y-%m-01T00:00:00')


def ledger(tx, key_id):
    row = tx.get(USAGE_KIND, key_id)
    data = row['data'] if row and isinstance(row.get('data'), dict) and row['data'].get('format') == FORMAT else None
    return (data or {'format': FORMAT, 'key_id': key_id, 'jobs': {}}), (row['version'] if row else None)


def reserve(kind, rows):
    """The most one more agent run of this new job may cost: an agent job's frozen budget, else its kind's cap."""
    from .agent_execution import agent_budget_usd
    if kind == 'agent':
        queue = next((data for record_kind, _, _, data in rows if record_kind == 'queue'), {})
        limit = (queue.get('inputs') or {}).get('limits', {}).get('max_budget_usd')
        if isinstance(limit, (int, float)) and limit > 0: return float(limit)
    return agent_budget_usd(kind)


def charged(metrics):
    """What a job's recorded runs cost: the running total, else (metrics from before it existed) the last run."""
    data = metrics['data'] if metrics else {}
    if isinstance(data.get('charged_usd'), (int, float)): return float(data['charged_usd'])
    agent = data.get('agent') if isinstance(data.get('agent'), dict) else None
    return run_charge(agent) if agent else 0.0


def recorded(metrics):
    data = metrics['data'] if metrics else {}
    return isinstance(data.get('charged_usd'), (int, float)) or isinstance(data.get('agent'), dict)


def dispatched(execution):
    """Whether this job's sandbox was ever asked for: a box handle, or an intent to create or launch one."""
    data = execution['data'] if execution else {}
    return bool(data.get('box') or data.get('creation_intent') or data.get('launch_intent'))


def run_charge(agent):
    """One run's cost: as the provider reported it, else estimated from its tokens, else its whole cap."""
    for field in ('cost_usd', 'estimated_cost_usd', 'budget_usd'):
        value = agent.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return float(value)
    return 0.0


def batched(tx, kind, ids, size=500):
    """get_many in bounded IN lists: a total budget's ledger can list thousands of jobs."""
    rows = {}
    for start in range(0, len(ids), size): rows.update(tx.get_many(kind, ids[start:start + size]))
    return rows


def usage(tx, key, owner, moment=None):
    """This key's spend in its budget period: {'spent_usd', 'in_progress_usd', 'jobs', 'period_start'}.
    spent_usd includes what in-progress jobs may still cost (in_progress_usd is that part)."""
    from .jobs import TERMINAL
    budget = key.get('budget') or {}
    start = period_start(budget.get('period'), moment)
    data, _ = ledger(tx, key['key_id'])
    entries = {job_id: entry for job_id, entry in data['jobs'].items() if not start or entry.get('created_at', '') >= start}
    ids = sorted(entries)
    jobs, metrics = batched(tx, 'job', ids), batched(tx, 'job_metrics', ids)
    # A job row that is gone (its workspace was removed) is finished: it keeps only what was recorded.
    running = {job_id for job_id in ids if job_id in jobs and jobs[job_id]['data'].get('status') not in TERMINAL}
    unrecorded = [job_id for job_id in ids if job_id in jobs and job_id not in running and not recorded(metrics.get(job_id))]
    executions = batched(tx, 'execution', unrecorded)
    spent = held = 0.0
    for job_id in ids:
        cost, hold = charged(metrics.get(job_id)), entries[job_id].get('reserve_usd', 0.0)
        if job_id in running: held += hold; cost += hold
        elif job_id in unrecorded and dispatched(executions.get(job_id)): cost = hold
        spent += cost
    if any(entry.get('kind') == 'analysis' for entry in entries.values()):
        # Statement jobs an analysis's accounts schedule are the workspace's, not the key's: count them by their parent.
        parents = {job_id for job_id, entry in entries.items() if entry.get('kind') == 'analysis'}
        for row in tx.list('job_metrics', owner):
            if row['data'].get('parent_job_id') in parents and row['id'] not in entries: spent += charged(row)
    return {'spent_usd': round(spent, 4), 'in_progress_usd': round(held, 4), 'jobs': len(ids), 'period_start': start}


def status(tx, key, owner, moment=None):
    """usage() with the budget beside it: remaining_usd is None for a key with no budget."""
    result = usage(tx, key, owner, moment)
    budget = key.get('budget')
    result['limit_usd'] = budget['limit_usd'] if budget else None
    result['period'] = budget['period'] if budget else None
    result['remaining_usd'] = round(max(0.0, budget['limit_usd'] - result['spent_usd']), 4) if budget else None
    return result


def admit(tx, key, owner, job, rows):
    """Charge a new job to the minted key that starts it: refuse it if the key's budget cannot cover its reserve,
    otherwise list it in the key's usage. Runs in the job's own transaction, so a refused job writes nothing."""
    amount = reserve(job['kind'], rows)
    budget = key.get('budget')
    if budget:
        state = status(tx, key, owner)
        if state['spent_usd'] + amount > budget['limit_usd'] + 1e-9:
            remaining = state['remaining_usd']
            when = 'this month' if budget['period'] == 'month' else 'in total'
            detail = (f'This API key has ${remaining:.2f} of its ${budget["limit_usd"]:.2f} budget left {when}, and this job may cost '
                      f'up to ${amount:.2f}.' + (' Ask for a lower limits.max_budget_usd, or' if job['kind'] == 'agent' and remaining > 0 else '')
                      + ' Ask an admin to raise the budget' + (', or wait for next month.' if budget['period'] == 'month' else '.'))
            raise Problem(402, 'API_KEY_BUDGET_EXHAUSTED', detail, remaining_usd=remaining, limit_usd=budget['limit_usd'],
                          period=budget['period'], job_reserve_usd=amount)
    data, version = ledger(tx, key['key_id'])
    if len(data['jobs']) >= MAX_JOBS:
        raise Problem(429, 'API_KEY_JOB_LIMIT', 'This API key has started as many jobs as one key may. Mint another key.')
    data = {**data, 'jobs': {**data['jobs'], job['id']: {'kind': job['kind'], 'created_at': job.get('created_at') or now(),
                                                          'reserve_usd': amount}}}
    try: tx.put(USAGE_KIND, key['key_id'], OWNER, data, expected=version)
    except Conflict: raise Problem(409, 'API_KEY_BUSY', 'Another job from this key was being started. Retry.') from None
