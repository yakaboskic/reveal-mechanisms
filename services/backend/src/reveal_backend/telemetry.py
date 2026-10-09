"""Read-only, bounded admin projections. Never expose record payloads or credentials."""
from datetime import datetime
import json
import re
import time
from .runtime_config import ROOT
from .repository import now
from .jobs import TERMINAL


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
                 'agent.lint_checks': 'lint_checks', 'error.phase': 'error_phase', 'error.error_type': 'error_type',
                 'parent_job_id': 'parent_job_id'}


def job_metrics(tx, identities):
    """Cost and failure telemetry of these jobs, flattened for the job tables. Unknown values are null."""
    rows = records(tx, 'job_metrics', tuple(METRIC_FIELDS), identities)
    empty = dict.fromkeys(METRIC_FIELDS.values())
    return {identity: {**empty, **{METRIC_FIELDS[path]: value for path, value in rows[identity]['data'].items()}}
            if identity in rows else dict(empty) for identity in identities}


def job_timings(tx, identities=None):
    rows = records(tx, 'job', ('kind', 'status', 'stage', 'created_at', 'completed_at', 'updated_at', 'failure.code'), identities)
    queues = records(tx, 'queue', ('attempt', 'worker_id', 'lease_until', 'recoveries', 'remote_handle'), list(rows))
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
            'id': identity, 'owner': owner, 'kind': job['kind'], 'status': job['status'], 'stage': job['stage'],
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


COST_WINDOW = 1000


def owner_labels(tx):
    """Name only client principals (e.g. a partner frontend's shared demo identity); people stay opaque ids."""
    labels = {}
    for row in records(tx, 'identity', ('issuer', 'subject', 'user_id'), limit=COST_WINDOW).values():
        issuer, subject, user = (row['data'].get(name) for name in ('issuer', 'subject', 'user_id'))
        if isinstance(issuer, str) and issuer.startswith('urn:reveal:client:') and isinstance(subject, str) and user:
            labels[user] = issuer.removeprefix('urn:reveal:client:') + '/' + subject
    return labels


def cost_summary(tx, current=None):
    """Spend and failure aggregates over the latest jobs that have recorded metrics.

    Spend is the provider-reported agent cost per run. A run stopped before the provider reported
    a total (timeout, cancellation) counts its estimate from streamed token usage when one was
    recorded, and is otherwise counted as unreported, never as $0.
    """
    metrics = records(tx, 'job_metrics', ('kind', 'created_at', 'parent_job_id', 'agent.cost_usd', 'agent.estimated_cost_usd', 'agent.cost_source',
                                          'agent.budget_used', 'agent.subtype', 'error.error_type', 'error.phase'), limit=COST_WINDOW)
    jobs = records(tx, 'job', ('status', 'failure.code', 'created_at'), list(metrics))
    labels = owner_labels(tx)
    current = time.time() if current is None else current
    def age(value):
        try: return (current - datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()) / 86400
        except (AttributeError, ValueError): return None
    def bucket(): return {'spend_usd': 0.0, 'jobs': 0, 'estimated': 0, 'unreported': 0, 'failed': 0}
    totals = {name: bucket() for name in ('day', 'week', 'month', 'window')}
    daily, owners, failures = {}, {}, {}
    research, exceeded, near = [], 0, 0
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
            day = daily.setdefault(created[:10], {'date': created[:10], 'analysis_usd': 0.0, 'paragraph_usd': 0.0, 'jobs': 0, 'failed': 0})
            day['jobs'] += 1; day['failed'] += status == 'failed'
            day['paragraph_usd' if data.get('kind') == 'paragraph' else 'analysis_usd'] += cost or 0
        owner = owners.setdefault(row['owner'], {'owner': row['owner'], 'label': labels.get(row['owner']), **bucket()})
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
        if data.get('kind') == 'analysis' and status == 'succeeded' and reported is not None: research.append(reported)
    research.sort()
    def quantile(q): return round(research[min(len(research) - 1, int(q * len(research)))], 4) if research else None
    rounded = lambda items: [{**item, 'spend_usd': round(item['spend_usd'], 4)} for item in items]
    return {'window_jobs': len(metrics), 'window_limit': COST_WINDOW,
            'totals': {name: {**value, 'spend_usd': round(value['spend_usd'], 4)} for name, value in totals.items()},
            'daily': [{**d, 'analysis_usd': round(d['analysis_usd'], 4), 'paragraph_usd': round(d['paragraph_usd'], 4)} for d in sorted(daily.values(), key=lambda d: d['date'])],
            'research': {'succeeded': len(research), 'p50_usd': quantile(.5), 'p90_usd': quantile(.9),
                         'max_usd': research[-1] if research else None,
                         'mean_usd': round(sum(research) / len(research), 4) if research else None},
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
        job_items = job_timings(tx)
        costs = cost_summary(tx)
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
            'counts': counts, 'recent': recent, 'statuses': statuses, 'jobs': job_items, 'costs': costs, 'events': events,
            'tables': tables, 'imports': imports, 'limits': {'jobs': 100, 'events': 100, 'recent': 100, 'imports_per_table': 10}}
