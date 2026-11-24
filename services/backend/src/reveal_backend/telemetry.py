"""Read-only, bounded admin projections. Never expose record payloads or credentials. The one personal
projection is owner_directory: the name and email a sign-in supplied, so an admin can see who an owner is."""
from datetime import datetime
import json
import re
import time
from .runtime_config import ROOT
from .repository import now
from .jobs import TERMINAL
from .auth import Problem


def seconds(start, end=None):
    if start is None: return None
    def stamp(value):
        if isinstance(value, (int, float)): return value
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    try: return round(max(0, (stamp(end) if end else time.time()) - stamp(start)), 3)
    except (ValueError, TypeError): return None


def records(tx, kind, fields, identities=None, limit=100):
    """Project allowlisted JSON paths inside SQL, before large payloads cross the wire."""
    projection = ','.join("'"+field+"',JSON_EXTRACT(payload,'$."+field+"')" for field in fields)
    sql = 'SELECT id,owner_id,JSON_OBJECT('+projection+') FROM reveal_records WHERE kind=%s'
    args = [kind]
    if identities is not None:
        if not identities: return {}
        sql += ' AND id IN ('+','.join(['%s']*len(identities))+')'
        args.extend(identities)
    sql += ' ORDER BY updated_at DESC,id DESC LIMIT %s'
    args.append(len(identities) if identities is not None else limit)
    return {r[0]: {'owner': r[1], 'data': json.loads(r[2])} for r in tx.execute(sql, args).fetchall()}


# job_metrics payload path -> flattened job-table field.
METRIC_FIELDS = {'agent.cost_usd': 'cost_usd', 'agent.estimated_cost_usd': 'estimated_cost_usd',
                 'agent.cost_source': 'cost_source', 'agent.budget_usd': 'budget_usd',
                 'agent.budget_used': 'budget_used', 'agent.turns': 'turns', 'agent.tokens': 'tokens',
                 'agent.model': 'model', 'agent.status': 'agent_status', 'agent.subtype': 'agent_subtype',
                 'agent.lint_checks': 'lint_checks', 'agent.draft_writes': 'draft_writes', 'agent.tool_calls': 'tool_calls',
                 'agent.tool_failures': 'tool_failures', 'agent.turn_limit': 'turn_limit',
                 'agent.time_limit_seconds': 'time_limit_seconds',
                 # Responses the provider's safety classifiers stopped, by category, and the models that answered.
                 'agent.refusals': 'refusals', 'agent.refusal_categories': 'refusal_categories',
                 'agent.ended_on_refusal': 'ended_on_refusal', 'agent.models': 'models', 'error.phase': 'error_phase',
                 'error.error_type': 'error_type', 'parent_job_id': 'parent_job_id'}


def job_metrics(tx, identities):
    """Cost and failure telemetry of these jobs, flattened for the job tables. Unknown values are null."""
    rows = records(tx, 'job_metrics', tuple(METRIC_FIELDS), identities)
    empty = dict.fromkeys(METRIC_FIELDS.values())
    return {identity: {**empty, **{METRIC_FIELDS[path]: value for path, value in rows[identity]['data'].items()}}
            if identity in rows else dict(empty) for identity in identities}


def deliverable_of(kind, queue):
    """What an agent job was asked to return: 'answer' or 'scientific_account'. None for every other kind of job, and
    for an agent job whose queue record is gone."""
    if kind != 'agent': return None
    value = queue.get('inputs.deliverable')
    return value if isinstance(value, str) else None


def job_timings(tx, identities=None):
    rows = records(tx, 'job', ('kind', 'status', 'stage', 'created_at', 'completed_at', 'updated_at', 'failure.code'), identities)
    queues = records(tx, 'queue', ('attempt', 'worker_id', 'lease_until', 'recoveries', 'remote_handle',
                                   'inputs.deliverable'), list(rows))
    # Durable workflow jobs keep their start, attempt and recovery state on the execution, not on legacy attempt rows.
    executions = records(tx, 'execution', ('created_at', 'authoring_attempt', 'recoveries', 'delivery_recoveries', 'handoffs'), list(rows))
    metrics = job_metrics(tx, list(rows))
    attempt_ids = {f'{identity}:1' for identity in rows}
    attempt_ids.update(f"{identity}:{row['data'].get('attempt')}" for identity, row in queues.items())
    attempts = records(tx, 'attempt', ('started_at',), sorted(attempt_ids))
    job_items = []
    for identity, row in rows.items():
        owner = row['owner']; job = row['data']; queue = queues.get(identity, {}).get('data', {})
        execution = executions.get(identity, {}).get('data', {})
        attempt = attempts.get(f"{identity}:{queue.get('attempt')}", {}).get('data', {})
        if not attempt and execution.get('created_at'): attempt = {'started_at': execution['created_at']}
        first_start = attempts.get(f'{identity}:1', {}).get('data', {}).get('started_at') or execution.get('created_at')
        handle = queue.get('remote_handle') or attempt.get('remote_handle') or {}
        end = job.get('completed_at')
        phases = handle.get('timings', {})
        job_items.append({
            'id': identity, 'owner': owner, 'kind': job['kind'], 'deliverable': deliverable_of(job['kind'], queue),
            'status': job['status'], 'stage': job['stage'],
            'created_at': job.get('created_at'), 'completed_at': end, 'updated_at': job.get('updated_at'),
            'started_at': first_start, 'attempt_started_at': attempt.get('started_at'),
            'wall_seconds': seconds(job.get('created_at'), end),
            'queue_seconds': seconds(job.get('created_at'), first_start or end) if first_start or not queue.get('attempt') else None,
            'attempt_seconds': seconds(attempt.get('started_at'), end),
            'attempt': queue.get('attempt') or execution.get('authoring_attempt') or 0, 'worker': queue.get('worker_id'),
            'lease_until': queue.get('lease_until'), 'recoveries': queue.get('recoveries') or execution.get('recoveries') or 0,
            'delivery_recoveries': execution.get('delivery_recoveries') or 0, 'handoffs': execution.get('handoffs') or 0,
            'lease_expired': job['status'] not in TERMINAL and bool(queue.get('lease_until')) and queue['lease_until'] < now(),
            'box_id': handle.get('box_id'), 'box_phase': handle.get('phase'),
            'box_seconds': seconds(handle.get('created_at'), phases.get('deleted_at') or end),
            'setup_seconds': seconds(handle.get('created_at'), phases.get('prepared_at')) if phases.get('prepared_at') else None,
            'agent_seconds': seconds(phases.get('running_at'), phases.get('terminal_at') or end),
            'capture_seconds': seconds(phases.get('terminal_at'), phases.get('captured_at')) if phases.get('captured_at') else None,
            'cleanup_seconds': seconds(phases.get('captured_at'), phases.get('deleted_at')) if phases.get('deleted_at') else None,
            'failure_code': job.get('failure.code'),
            **metrics[identity],
        })
    return job_items


AGENT_JOB_WINDOW = 100
# How an agent job was asked to run, by allowlisted path of the queue record's frozen input. That record also holds
# the prompt, system prompt, question, claims, evidence package, lease token and (for an account) who is credited.
# None of those is read here: text is reported as a length and lists of content as a count, both taken in SQL.
AGENT_INPUT_FIELDS = {'inputs.format': 'input_format', 'inputs.model': 'model', 'inputs.effort': 'effort',
                      'inputs.tools': 'tools', 'inputs.graphs': 'graphs', 'inputs.network': 'network', 'inputs.allowed_domains': 'allowed_domains',
                      'inputs.claims_repository': 'claims_repository', 'inputs.limits': 'limits',
                      'inputs.account.attribute_requester': 'attribute_requester'}
AGENT_SWITCHES = ('claims_repository', 'attribute_requester')
# What came back, from the job record. Never the response text, the account document or a claim.
AGENT_RESULT_FIELDS = {'result.response_truncated': 'response_truncated', 'result.account.account_id': 'account_id',
                       'result.account.objects': 'objects', 'result.account.claims': 'claims',
                       'result.account.value_locators': 'quotes', 'result.account.lint_warnings': 'lint_warnings',
                       'result.account.dapper_version': 'dapper_version',
                       'result.account.deposit.status': 'deposit_status', 'result.account.deposit.deposit_id': 'deposit_id',
                       'result.account.deposit.reason': 'deposit_reason',
                       'result.account.deposit.http_status': 'deposit_http_status'}
# The deposit screen's verdict, from job_metrics (deposit_screen): which check withheld the account, the classifier's
# score and threshold, and where the text came from (a source file and lines, a ledger call, an account field).
AGENT_SCREEN_FIELDS = {'screen.outcome': 'screen_outcome', 'screen.check': 'screen_check', 'screen.score': 'screen_score',
                       'screen.threshold': 'screen_threshold', 'screen.location': 'screen_location',
                       'screen.classifier': 'screen_classifier', 'screen.windows': 'screen_windows',
                       'screen.texts': 'screen_texts', 'screen.seconds': 'screen_seconds'}
# (record kind, output field, JSON path, what is measured). 'text' is a string's length in characters, 'items' a
# list's length, 'triples' the claims that came with a subject. A missing or null value is None, never 0.
AGENT_SIZES = (('queue', 'prompt_characters', 'inputs.prompt', 'text'),
               ('queue', 'system_prompt_characters', 'inputs.system_prompt', 'text'),
               ('queue', 'question_characters', 'inputs.account.question', 'text'),
               ('queue', 'claims_requested', 'inputs.account.claims', 'items'),
               ('queue', 'claims_with_triple', 'inputs.account.claims', 'triples'),
               ('job', 'response_characters', 'result.response', 'text'),
               ('job', 'papers', 'result.account.papers', 'items'),
               ('job', 'files', 'result.files', 'items'),
               # Structured output: only how large the schema and the returned object are, never either one.
               ('queue', 'output_schema_characters', 'inputs.output_schema', 'json'),
               ('job', 'structured_output_characters', 'result.structured_output', 'json'))


def measure(tx, path, what):
    """SQL for one size of a JSON value in payload, in the connection's dialect. The value itself is never selected."""
    path = "'$." + path + "'"
    if tx.sqlite:
        if what == 'text': return f"CASE WHEN json_type(payload,{path})='text' THEN length(json_extract(payload,{path})) END"
        if what == 'items': return f"CASE WHEN json_type(payload,{path})='array' THEN json_array_length(payload,{path}) END"
        if what == 'json': return f"CASE WHEN json_type(payload,{path}) IN ('object','array') THEN length(json_extract(payload,{path})) END"
        return (f"CASE WHEN json_type(payload,{path})='array' THEN (SELECT COUNT(*) FROM json_each(payload,{path}) "
                "WHERE type='object' AND json_type(value,'$.subject_entity')='text') END")
    value = f"JSON_EXTRACT(payload,{path})"
    if what == 'text': return f"IF(JSON_TYPE({value})='STRING',CHAR_LENGTH(JSON_UNQUOTE({value})),NULL)"
    if what == 'items': return f"IF(JSON_TYPE({value})='ARRAY',JSON_LENGTH({value}),NULL)"
    if what == 'json': return f"IF(JSON_TYPE({value}) IN ('OBJECT','ARRAY'),CHAR_LENGTH(CAST({value} AS CHAR)),NULL)"
    subjects = "JSON_EXTRACT(payload,'$." + path[3:-1] + "[*].subject_entity')"
    return f"IF(JSON_TYPE({value})='ARRAY',COALESCE(JSON_LENGTH({subjects}),0),NULL)"


def sizes(tx, kind, identities):
    """{id: {field: size}} for the AGENT_SIZES of one record kind."""
    wanted = [(field, path, what) for record, field, path, what in AGENT_SIZES if record == kind]
    if not identities or not wanted: return {}
    sql = ('SELECT id,' + ','.join(measure(tx, path, what) for _, path, what in wanted) + ' FROM reveal_records WHERE kind=%s AND id IN ('
           + ','.join(['%s'] * len(identities)) + ')')
    return {row[0]: {field: int(value) if value is not None else None for (field, _, _), value in zip(wanted, row[1:])}
            for row in tx.execute(sql, [kind, *identities]).fetchall()}


def agent_runs(tx, identities):
    """{job id: how the agent job was asked to run and what it returned}. Settings, limits, sizes and outcomes only."""
    identities = list(identities)
    queues = records(tx, 'queue', ('inputs.deliverable', *AGENT_INPUT_FIELDS), identities)
    results = records(tx, 'job', tuple(AGENT_RESULT_FIELDS), identities)
    screens = records(tx, 'job_metrics', tuple(AGENT_SCREEN_FIELDS), identities)
    queue_sizes, job_sizes = sizes(tx, 'queue', identities), sizes(tx, 'job', identities)
    runs = {}
    for identity in identities:
        queue = queues.get(identity, {}).get('data', {})
        asked = {field: queue.get(path) for path, field in AGENT_INPUT_FIELDS.items()}
        # SQLite reports a JSON boolean as 0 or 1 and MySQL as true or false; a setting the input never had stays None.
        for field in AGENT_SWITCHES:
            if asked[field] is not None: asked[field] = bool(asked[field])
        for field in ('tools', 'graphs', 'allowed_domains'):
            if not isinstance(asked[field], list): asked[field] = None
            else: asked[field] = [item for item in asked[field] if isinstance(item, str)][:50]
        if not isinstance(asked['limits'], dict): asked['limits'] = None
        result = {field: results.get(identity, {}).get('data', {}).get(path) for path, field in AGENT_RESULT_FIELDS.items()}
        result.update({field: screens.get(identity, {}).get('data', {}).get(path) for path, field in AGENT_SCREEN_FIELDS.items()})
        if result['response_truncated'] is not None: result['response_truncated'] = bool(result['response_truncated'])
        empty = {field: None for record, field, _, _ in AGENT_SIZES}
        runs[identity] = {'deliverable': deliverable_of('agent', queue), **asked,
                          **{**empty, **queue_sizes.get(identity, {}), **job_sizes.get(identity, {})}, 'result': result}
        # The sizes that describe the result belong with it.
        for field in ('response_characters', 'papers', 'files', 'structured_output_characters'):
            runs[identity]['result'][field] = runs[identity].pop(field)
    return runs


# Each source file an account job saved, for the admin job detail only (never the job list): what the agent wrote about
# it (path, URL, the command it recorded and the Bash call REVEAL saw run) and what REVEAL measured (size, sha256, media
# type), with how many quotes cite it. Metadata, not the file or its text. Caps bound what one job can put on the page.
AGENT_SOURCE_FIELDS = {'path': 300, 'url': 2_000, 'command': 2_000, 'observed_command': 8_000, 'derived_from': 300,
                       'sha256': 64, 'media_type': 100, 'size_bytes': None, 'evidence_items': None}
MAX_AGENT_SOURCES = 100


def account_sources(tx, identity):
    """{'items': [...], 'total': n} for the saved sources the job result's account lists, each with AGENT_SOURCE_FIELDS
    only; None when the job returned no account."""
    papers = records(tx, 'job', ('result.account.papers',), [identity]).get(identity, {}).get('data', {}).get('result.account.papers')
    if not isinstance(papers, list): return None
    def value(paper, key, cap):
        item = paper.get(key)
        if cap is None: return item if isinstance(item, int) and not isinstance(item, bool) else None
        return item[:cap] if isinstance(item, str) else None
    items = [{key: value(paper, key, cap) for key, cap in AGENT_SOURCE_FIELDS.items()}
             for paper in papers[:MAX_AGENT_SOURCES] if isinstance(paper, dict)]
    return {'items': items, 'total': len(papers)}


def median(values):
    values = sorted(value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool))
    return values[len(values) // 2] if values else None


def agent_jobs(tx, limit=AGENT_JOB_WINDOW):
    """The latest general agent jobs with how each was asked to run, and what was asked for by type.

    The job list above holds every kind of job, so agent jobs drop out of it as research runs accumulate; this
    one selects them by kind."""
    identities = [row[0] for row in tx.execute(
        "SELECT id FROM reveal_records WHERE kind=%s AND JSON_EXTRACT(payload,'$.kind')=%s ORDER BY updated_at DESC,id DESC LIMIT %s",
        ('job', 'agent', limit)).fetchall()]
    runs = agent_runs(tx, identities)
    items = [{**job, 'agent': runs[job['id']]} for job in job_timings(tx, identities)] if identities else []
    types = {}
    for job in items:
        run = job['agent']; name = run['deliverable'] or 'unknown'
        entry = types.setdefault(name, {'deliverable': name, 'jobs': 0, 'succeeded': 0, 'failed': 0, 'cancelled': 0, 'active': 0,
                                        'deposited': 0, 'deposit_not_made': 0, 'withheld_unsafe': 0, 'withheld_by_check': {},
                                        'spend_usd': 0.0, 'owners': set(),
                                        'costs': [], 'turns': [], 'seconds': [], 'latest_at': None})
        entry['jobs'] += 1; entry['owners'].add(job['owner'])
        status = job['status']
        entry[status if status in ('succeeded', 'cancelled') else 'failed' if status in TERMINAL else 'active'] += 1
        deposit = run['result']['deposit_status']
        if deposit == 'deposited': entry['deposited'] += 1
        # Withheld by the deposit screen, counted by the check that withheld it: the rate the threshold is tuned from.
        elif deposit == 'withheld_unsafe':
            entry['withheld_unsafe'] += 1
            check = run['result']['screen_check'] or run['result']['deposit_reason'] or 'unknown'
            entry['withheld_by_check'][check] = entry['withheld_by_check'].get(check, 0) + 1
        # An account that was accepted and returned but is not in the claims repository: the deposit failed or no
        # repository is configured. The job itself still reads "succeeded".
        elif status == 'succeeded' and name == 'scientific_account': entry['deposit_not_made'] += 1
        cost = job['cost_usd'] if isinstance(job['cost_usd'], (int, float)) else job['estimated_cost_usd']
        if isinstance(cost, (int, float)): entry['spend_usd'] += cost
        if status == 'succeeded':
            entry['costs'].append(job['cost_usd']); entry['turns'].append(job['turns']); entry['seconds'].append(job['wall_seconds'])
        if isinstance(job['created_at'], str) and (entry['latest_at'] is None or job['created_at'] > entry['latest_at']):
            entry['latest_at'] = job['created_at']
    summary = [{**{key: value for key, value in entry.items() if key not in ('owners', 'costs', 'turns', 'seconds')},
                'spend_usd': round(entry['spend_usd'], 4), 'owners': len(entry['owners']),
                'typical_cost_usd': median(entry['costs']), 'typical_turns': median(entry['turns']),
                'typical_seconds': median(entry['seconds'])}
               for entry in sorted(types.values(), key=lambda entry: -entry['jobs'])]
    return {'window_limit': limit, 'jobs': items, 'types': summary}


COST_WINDOW = 1000


OWNER_WINDOW = 500
INTERNAL_ISSUERS = {'urn:reveal:client:': 'client', 'urn:reveal:application:': 'application'}
SIGN_IN = {'https://accounts.google.com': 'Google', 'https://orcid.org': 'ORCID', 'https://sandbox.orcid.org': 'ORCID'}


def key_owners(environ=None):
    """The workspace ids this deployment has an API key configured for; none when the configuration is unusable."""
    from . import api_keys
    try: return {user_id for _, user_id in api_keys.keys(environ)}
    except Problem: return set()


def owner_directory(tx, owners):
    """Who each of these owner ids is: {owner id: {'kind', 'name', 'email', 'sign_in', 'api_key'}}.

    kind is 'registered' (a person who signed in), 'api_key' (a workspace that exists only to hold an operator-issued
    key: named when an operator registered it under a name, otherwise nameless), 'client' or 'application' (a partner
    frontend's shared identity, named <client>/<subject>), or 'anonymous' (a browser workspace nobody signed in to:
    there is no name to show). name and email are the profile the sign-in supplied and may be null. api_key says
    whether this deployment has a key configured for the workspace, whatever its kind. An id that is no principal,
    such as a system owner, is left out. Only these allowlisted profile fields are read, by exact key."""
    owners = sorted({owner for owner in owners if isinstance(owner, str) and owner})[:OWNER_WINDOW]
    if not owners: return {}
    principals = records(tx, 'principal', ('me.principal_kind', 'me.display_name', 'me.email'), owners)
    marks = ','.join(['%s'] * len(owners))
    projection = "JSON_OBJECT('issuer',JSON_EXTRACT(payload,'$.issuer'),'subject',JSON_EXTRACT(payload,'$.subject'))"
    issued = {}
    for owner, identity in tx.execute('SELECT owner_id,' + projection + ' FROM reveal_records WHERE kind=%s AND owner_id IN ('
                                      + marks + ') ORDER BY id', ('identity', *owners)).fetchall():
        issued.setdefault(owner, []).append(json.loads(identity))
    def text(value): return value.strip() if isinstance(value, str) and value.strip() else None
    keyed = key_owners()
    directory = {}
    for owner, row in principals.items():
        profile = row['data']
        entry = {'kind': 'anonymous' if profile.get('me.principal_kind') == 'anonymous' else 'registered',
                 'name': text(profile.get('me.display_name')), 'email': text(profile.get('me.email')), 'sign_in': None,
                 'api_key': owner in keyed}
        # A key issued without registering its workspace leaves an anonymous principal: say it is a key, not a guest.
        if entry['api_key'] and entry['kind'] == 'anonymous': entry['kind'] = 'api_key'
        for identity in issued.get(owner, ()):
            issuer, subject = text(identity.get('issuer')), text(identity.get('subject'))
            if not issuer: continue
            shared = next((prefix for prefix in INTERNAL_ISSUERS if issuer.startswith(prefix)), None)
            if shared and subject:
                entry.update(kind=INTERNAL_ISSUERS[shared], name=entry['name'] or issuer.removeprefix(shared) + '/' + subject)
            elif issuer == 'urn:reveal:operator-api-key': entry['kind'] = 'api_key'
            elif issuer in SIGN_IN: entry['sign_in'] = SIGN_IN[issuer]
        directory[owner] = entry
    return directory


def cost_summary(tx, current=None):
    """Spend and failure aggregates over the latest jobs that have recorded metrics.

    Spend is the provider-reported agent cost per run. A run stopped before the provider reported
    a total (timeout, cancellation) counts its estimate from streamed token usage when one was
    recorded, and is otherwise counted as unreported, never as $0.
    """
    metrics = records(tx, 'job_metrics', ('kind', 'created_at', 'parent_job_id', 'agent.cost_usd', 'agent.estimated_cost_usd', 'agent.cost_source',
                                          'agent.budget_used', 'agent.subtype', 'error.error_type', 'error.phase'), limit=COST_WINDOW)
    jobs = records(tx, 'job', ('status', 'failure.code', 'created_at'), list(metrics))
    current = time.time() if current is None else current
    def age(value):
        try: return (current - datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()) / 86400
        except (AttributeError, ValueError): return None
    def bucket(): return {'spend_usd': 0.0, 'jobs': 0, 'estimated': 0, 'unreported': 0, 'failed': 0}
    totals = {name: bucket() for name in ('day', 'week', 'month', 'window')}
    daily, owners, failures = {}, {}, {}
    # Successful runs' provider-reported costs, per kind with a summary: research analyses and general agent jobs.
    succeeded, exceeded, near = {'analysis': [], 'agent': []}, 0, 0
    for identity, row in metrics.items():
        data = row['data']; job = jobs.get(identity, {}).get('data', {})
        created = job.get('created_at') or data.get('created_at'); days = age(created)
        reported, estimate = (value if isinstance(value, (int, float)) else None
                              for value in (data.get('agent.cost_usd'), data.get('agent.estimated_cost_usd')))
        cost = reported if reported is not None else estimate
        status, code = job.get('status'), job.get('failure.code')
        estimated = reported is None and estimate is not None
        unreported = data.get('agent.cost_source') is not None and cost is None
        for name, limit in (('day', 1), ('week', 7), ('month', 30), ('window', None)):
            if limit is None or days is not None and days <= limit:
                target = totals[name]; target['jobs'] += 1; target['spend_usd'] += cost or 0
                target['estimated'] += estimated; target['unreported'] += unreported; target['failed'] += status == 'failed'
        if days is not None and days <= 30 and isinstance(created, str):
            day = daily.setdefault(created[:10], {'date': created[:10], 'analysis_usd': 0.0, 'paragraph_usd': 0.0, 'agent_usd': 0.0,
                                                  'jobs': 0, 'failed': 0})
            day['jobs'] += 1; day['failed'] += status == 'failed'
            day[{'paragraph': 'paragraph_usd', 'agent': 'agent_usd'}.get(data.get('kind'), 'analysis_usd')] += cost or 0
        owner = owners.setdefault(row['owner'], {'owner': row['owner'], **bucket()})
        owner['jobs'] += 1; owner['spend_usd'] += cost or 0; owner['estimated'] += estimated
        owner['unreported'] += unreported; owner['failed'] += status == 'failed'
        if status == 'failed':
            entry = failures.setdefault(code or 'UNKNOWN', {'code': code or 'UNKNOWN', 'jobs': 0, 'spend_usd': 0.0, 'causes': {}})
            entry['jobs'] += 1; entry['spend_usd'] += cost or 0
            # A run that ended normally ('success') is not why a later phase failed.
            subtype = data.get('agent.subtype') if data.get('agent.subtype') != 'success' else None
            cause = ' · '.join(filter(None, (data.get('error.error_type'), data.get('error.phase')))) or subtype
            if cause: entry['causes'][cause] = entry['causes'].get(cause, 0) + 1
        if data.get('agent.subtype') == 'error_max_budget_usd' or code == 'AUTHORING_BUDGET_EXCEEDED': exceeded += 1
        elif isinstance(data.get('agent.budget_used'), (int, float)) and data['agent.budget_used'] >= .8: near += 1
        if data.get('kind') in succeeded and status == 'succeeded' and reported is not None: succeeded[data['kind']].append(reported)
    def distribution(values):
        values = sorted(values)
        def quantile(q): return round(values[min(len(values) - 1, int(q * len(values)))], 4) if values else None
        return {'succeeded': len(values), 'p50_usd': quantile(.5), 'p90_usd': quantile(.9), 'max_usd': values[-1] if values else None,
                'mean_usd': round(sum(values) / len(values), 4) if values else None}
    rounded = lambda items: [{**item, 'spend_usd': round(item['spend_usd'], 4)} for item in items]
    return {'window_jobs': len(metrics), 'window_limit': COST_WINDOW,
            'totals': {name: {**value, 'spend_usd': round(value['spend_usd'], 4)} for name, value in totals.items()},
            'daily': [{**d, **{key: round(d[key], 4) for key in ('analysis_usd', 'paragraph_usd', 'agent_usd')}}
                      for d in sorted(daily.values(), key=lambda d: d['date'])],
            'research': distribution(succeeded['analysis']), 'agents': distribution(succeeded['agent']),
            'owners': rounded(sorted(owners.values(), key=lambda o: -o['spend_usd'])[:20]),
            'failures': rounded(sorted(failures.values(), key=lambda f: -f['jobs'])),
            'budget': {'exceeded': exceeded, 'near_limit': near}}


def snapshot(repository):
    started = time.perf_counter()
    with repository.read_transaction(utc=True) as tx:
        counts = [{'kind': r[0], 'count': r[1], 'updated_at': r[2]} for r in tx.execute(
            'SELECT kind,COUNT(*),MAX(updated_at) FROM reveal_records GROUP BY kind ORDER BY kind').fetchall()]
        recent = [dict(zip(('kind', 'id', 'owner', 'version', 'updated_at'), r)) for r in tx.execute(
            'SELECT kind,id,owner_id,version,updated_at FROM reveal_records ORDER BY updated_at DESC,kind DESC,id DESC LIMIT 100').fetchall()]
        status_sql = "SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status')),COUNT(*) FROM reveal_records WHERE kind='job' GROUP BY 1"
        if tx.sqlite: status_sql = status_sql.replace("JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status'))", "json_extract(payload,'$.status')")
        statuses = [{'status': r[0], 'count': r[1]} for r in tx.execute(status_sql).fetchall()]
        kind_sql = "SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.kind')),COUNT(*) FROM reveal_records WHERE kind='job' GROUP BY 1"
        if tx.sqlite: kind_sql = kind_sql.replace("JSON_UNQUOTE(JSON_EXTRACT(payload,'$.kind'))", "json_extract(payload,'$.kind')")
        job_kinds = sorted(({'kind': r[0], 'count': r[1]} for r in tx.execute(kind_sql).fetchall()), key=lambda k: -k['count'])
        job_items = job_timings(tx)
        agents = agent_jobs(tx)
        costs = cost_summary(tx)
        owners = owner_directory(tx, [row['owner'] for group in (recent, job_items, agents['jobs'], costs['owners']) for row in group])
        # Only event envelope fields; messages/tool arguments may contain private scientific input.
        events = [row['data'] for row in records(tx, 'event', ('id', 'job_id', 'occurred_at', 'event_type', 'status', 'stage')).values()]
        tables, imports = [], []
        schema = (ROOT / 'schema/prisma/schema.prisma').read_text()
        mapped = re.findall(r'@@map\("([a-z_]+)"\)', schema)
        if not tx.sqlite:
            metadata = {r[0]: r for r in tx.execute('SELECT TABLE_NAME,TABLE_ROWS,DATA_LENGTH,INDEX_LENGTH FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()').fetchall()}
            for name in mapped:
                row = metadata.get(name)
                tables.append({'name': name, 'present': row is not None, 'estimated_rows': int(row[1] or 0) if row else None,
                               'bytes': int((row[2] or 0)+(row[3] or 0)) if row else None})
            column_rows = tx.execute('SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()').fetchall()
            queries = []
            for name in ('dismech_imports', 'eaggl_imports', 'gene_set_imports', 'eaggl_embedding_runs', 'dismech_embedding_runs', 'eaggl_cfde_link_runs'):
                if name not in metadata: continue
                # Identifiers come exclusively from these fixed allowlists.
                available = {r[1] for r in column_rows if r[0] == name}
                columns = [c for c in ('import_id', 'run_id', 'status', 'created_at', 'updated_at', 'expected_rows', 'loaded_rows', 'expected_vectors', 'loaded_vectors', 'expected_bindings', 'loaded_bindings', 'dimensions') if c in available]
                if not columns: continue
                order = 'created_at' if 'created_at' in columns else columns[0]
                projection = ','.join("'"+c+"',"+c for c in columns)
                queries.append("(SELECT JSON_OBJECT('table','"+name+"',"+projection+") FROM `"+name+'` ORDER BY '+order+' DESC LIMIT 10)')
            if queries:
                imports = [json.loads(r[0]) for r in tx.execute(' UNION ALL '.join(queries)).fetchall()]
        else:
            tables = [{'name': name, 'present': name == 'reveal_records', 'estimated_rows': None, 'bytes': None} for name in mapped]
    from .runtime_metrics import metrics
    from .workspace_events import notification_health
    return {'generated_at': now(), 'query_ms': round((time.perf_counter()-started)*1000, 1), 'runtime': metrics(),
            'notifications':notification_health(repository),
            'database': 'sqlite-test' if repository.sqlite_path else 'aurora-mysql',
            'counts': counts, 'recent': recent, 'statuses': statuses, 'job_kinds': job_kinds, 'jobs': job_items,
            'agent_jobs': agents, 'costs': costs, 'owners': owners, 'events': events,
            'tables': tables, 'imports': imports,
            'limits': {'jobs': 100, 'agent_jobs': AGENT_JOB_WINDOW, 'events': 100, 'recent': 100, 'imports_per_table': 10,
                       'owners': OWNER_WINDOW}}
