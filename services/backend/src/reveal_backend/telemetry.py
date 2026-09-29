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


def records(tx, kind, fields, identities=None):
    """Project allowlisted JSON paths inside SQL, before large payloads cross the wire."""
    projection = ','.join("'"+field+"',JSON_EXTRACT(payload,'$."+field+"')" for field in fields)
    sql = 'SELECT id,owner_id,JSON_OBJECT('+projection+') FROM reveal_records WHERE kind=%s'
    args = [kind]
    if identities is not None:
        if not identities: return {}
        sql += ' AND id IN ('+','.join(['%s']*len(identities))+')'
        args.extend(identities)
    sql += ' ORDER BY updated_at DESC,id DESC LIMIT %s'
    args.append(len(identities) if identities is not None else 100)
    return {r[0]: {'owner': r[1], 'data': json.loads(r[2])} for r in tx.execute(sql, args).fetchall()}


def job_timings(tx, identities=None):
    rows = records(tx, 'job', ('kind', 'status', 'stage', 'created_at', 'completed_at', 'updated_at', 'failure.code'), identities)
    queues = records(tx, 'queue', ('attempt', 'worker_id', 'lease_until', 'recoveries', 'remote_handle'), list(rows))
    attempt_ids = {f'{identity}:1' for identity in rows}
    attempt_ids.update(f"{identity}:{row['data'].get('attempt')}" for identity, row in queues.items())
    attempts = records(tx, 'attempt', ('started_at',), sorted(attempt_ids))
    job_items = []
    for identity, row in rows.items():
        owner = row['owner']; job = row['data']; queue = queues.get(identity, {}).get('data', {})
        attempt = attempts.get(f"{identity}:{queue.get('attempt')}", {}).get('data', {})
        first_start = attempts.get(f'{identity}:1', {}).get('data', {}).get('started_at')
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
            'attempt': queue.get('attempt', 0), 'worker': queue.get('worker_id'),
            'lease_until': queue.get('lease_until'), 'recoveries': queue.get('recoveries') or 0,
            'lease_expired': job['status'] not in TERMINAL and bool(queue.get('lease_until')) and queue['lease_until'] < now(),
            'box_id': handle.get('box_id'), 'box_phase': handle.get('phase'),
            'box_seconds': seconds(handle.get('created_at'), phases.get('deleted_at') or end),
            'setup_seconds': seconds(handle.get('created_at'), phases.get('prepared_at')) if phases.get('prepared_at') else None,
            'agent_seconds': seconds(phases.get('running_at'), phases.get('terminal_at') or end),
            'capture_seconds': seconds(phases.get('terminal_at'), phases.get('captured_at')) if phases.get('captured_at') else None,
            'cleanup_seconds': seconds(phases.get('captured_at'), phases.get('deleted_at')) if phases.get('deleted_at') else None,
            'failure_code': job.get('failure.code'),
        })
    return job_items


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
    return {'generated_at': now(), 'query_ms': round((time.perf_counter()-started)*1000, 1), 'runtime': metrics(),
            'database': 'sqlite-test' if repository.sqlite_path else 'aurora-mysql',
            'counts': counts, 'recent': recent, 'statuses': statuses, 'jobs': job_items, 'events': events,
            'tables': tables, 'imports': imports, 'limits': {'jobs': 100, 'events': 100, 'recent': 100, 'imports_per_table': 10}}
