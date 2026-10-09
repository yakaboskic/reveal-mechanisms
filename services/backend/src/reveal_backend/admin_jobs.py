"""Admin-only execution history and bounded, allowlisted saved diagnostics."""
import json
import os
import re
from uuid import UUID

from .auth import Problem
from .runtime_config import artifacts_root
from .telemetry import job_timings, records

MAX_DIAGNOSTIC_BYTES = 128 * 1024
MAX_DIAGNOSTICS = 20
SECRET_FIELD = re.compile(r'^(?:authorization|cookie|password|secret|token|api[_-]?key|access[_-]?token|refresh[_-]?token)$', re.I)


def redact(value):
    """Diagnostics can contain provider exceptions; never return credentials."""
    secrets = [v for k, v in os.environ.items() if len(v) >= 8 and re.search(r'(?:PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)$', k)]
    def clean(item):
        if isinstance(item, dict): return {k: '[redacted]' if SECRET_FIELD.fullmatch(k) else clean(v) for k, v in item.items()}
        if isinstance(item, list): return [clean(v) for v in item]
        if not isinstance(item, str): return item
        for secret in secrets: item = item.replace(secret, '[redacted]')
        item = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [redacted]', item)
        item = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)\s*[=:]\s*)[^\s&,;]+', r'\1[redacted]', item)
        return re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[redacted]@', item)
    return clean(value)


def saved_diagnostics(job_id, attempts):
    base = artifacts_root()
    root = base / job_id
    # Explicit filenames only. Never allow an arbitrary file path from a browser
    # or follow a symlink into another job (or outside the artifact volume).
    if root.is_symlink() or not root.resolve().is_relative_to(base): return [], False
    candidates = [root / ('evidence/' + name) for name in ('dispatch-budget-failure.json', 'collection-error.json')]
    candidates.append(root / 'token-budget.json')  # Workflow preparation writes its measurement at the workspace root.
    for attempt in reversed(attempts):
        directory = root / f"attempt-{attempt['attempt']}"
        candidates.extend(directory / name for name in ('failure.json', 'token-budget.json', 'paragraph-grounding.json', 'output/runtime.json'))
        if directory.is_dir() and not directory.is_symlink():
            candidates.extend(sorted(p for p in directory.iterdir() if re.fullmatch(r'(?:validation|grounding)-\d+\.json', p.name)))
    result = []
    for path in candidates:
        relative = path.relative_to(root)
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != base and parent.is_relative_to(base)): continue
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()): continue
        if len(result) == MAX_DIAGNOSTICS: return result, True
        try:
            with path.open('rb') as stream: raw = stream.read(MAX_DIAGNOSTIC_BYTES + 1)
            if len(raw) > MAX_DIAGNOSTIC_BYTES:
                result.append({'path': str(relative), 'available': False, 'reason': 'This diagnostic exceeds the 128 KiB preview limit.'})
                continue
            value = json.loads(raw)
            result.append({'path': str(relative), 'available': True, 'data': redact(value)})
        except (OSError, ValueError):
            result.append({'path': str(relative), 'available': False, 'reason': 'This diagnostic is incomplete or unreadable.'})
    return result, False


def s3_diagnostics(workspace, attempts):
    """Read only allowlisted diagnostic objects, outside the database transaction."""
    from .artifact_store import store, StorageUnavailable
    if not workspace: return [], False
    storage = store()
    try:
        manifest = json.loads(storage.get(workspace))
        if manifest.get('format') != 'reveal.workspace/1': raise ValueError('Invalid checkpoint')
        allowed = {item['attempt'] for item in attempts}
        candidates = []
        for item in manifest['files']:
            name = item['path']
            match = re.fullmatch(r'attempt-([1-9][0-9]*)/(?:failure|token-budget|paragraph-grounding|(?:validation|grounding)-[0-9]+|output/runtime)\.json', name)
            if name in ('evidence/dispatch-budget-failure.json', 'evidence/collection-error.json', 'token-budget.json') or match and int(match[1]) in allowed:
                candidates.append(item)
        result = []
        for item in candidates[:MAX_DIAGNOSTICS]:
            if item['storage']['size_bytes'] > MAX_DIAGNOSTIC_BYTES:
                result.append({'path': item['path'], 'available': False, 'reason': 'This diagnostic exceeds the 128 KiB preview limit.'})
                continue
            try:
                value = json.loads(storage.get(item['storage']))
                result.append({'path': item['path'], 'available': True, 'data': redact(value)})
            except (StorageUnavailable, ValueError):
                result.append({'path': item['path'], 'available': False, 'reason': 'This diagnostic is unavailable or unreadable.'})
        return result, len(candidates) > MAX_DIAGNOSTICS
    except (StorageUnavailable, ValueError, KeyError, TypeError):
        return [{'path': 'checkpoint', 'available': False, 'reason': 'The retained diagnostics are currently unavailable.'}], False


def job_detail(repository, job_id, *, before=None, limit=100):
    try:
        if str(UUID(job_id)) != job_id: raise ValueError()
    except ValueError: raise Problem(404, 'JOB_NOT_FOUND', 'This job does not exist.') from None
    if not 1 <= limit <= 100 or before is not None and not re.fullmatch(r'[1-9][0-9]{0,11}', before):
        raise Problem(422, 'INVALID_CURSOR', 'Choose a positive event cursor and 1–100 events.')
    with repository.read_transaction(utc=True) as tx:
        timings = job_timings(tx, [job_id])
        if not timings: raise Problem(404, 'JOB_NOT_FOUND', 'This job does not exist.')
        metadata = records(tx, 'job', ('failure', 'warnings', 'research_request_id', 'input_account_id'), [job_id])[job_id]['data']
        job = {**timings[0], **redact(metadata)}
        # The padded event sequence is part of the indexed primary key. Load the
        # tail first so the reason for failure is visible even on very long runs.
        upper = job_id+':'+before.zfill(12) if before else job_id+';'
        values = tx.execute('SELECT payload FROM reveal_records WHERE kind=%s AND id>%s AND id<%s ORDER BY id DESC LIMIT %s',
                            ('event', job_id+':', upper, limit+1)).fetchall()
        events = [redact(json.loads(row[0])) for row in values[:limit]]
        events.reverse()
        next_before = events[0]['id'] if len(values) > limit else None
        # Project only operational fields, never queue inputs or lease tokens.
        projection = ','.join("'"+field+"',JSON_EXTRACT(payload,'$."+field+"')" for field in ('attempt', 'started_at', 'worker_id', 'remote_handle.box_id'))
        values = tx.execute('SELECT JSON_OBJECT('+projection+') FROM reveal_records WHERE kind=%s AND id>%s AND id<%s ORDER BY updated_at DESC,id DESC LIMIT 51',
                            ('attempt', job_id+':', job_id+';')).fetchall()
        attempts = [json.loads(row[0]) for row in values[:50]]
        attempts = sorted((a for a in attempts if type(a.get('attempt')) is int and 0 < a['attempt'] <= 100000), key=lambda a: a['attempt'])
        execution = tx.get('execution', job_id)
        execution = execution['data'] if execution else {}
        # Durable workflow jobs record their attempt on the execution rather than as legacy attempt rows.
        current = execution.get('authoring_attempt')
        allowed = attempts or ([{'attempt': current}] if type(current) is int and 0 < current <= 100000 else [])
        metrics = tx.get('job_metrics', job_id)
        from .artifact_store import s3_enabled
        remote = s3_enabled()
        queue = tx.get('queue', job_id) if remote else None
        workspace = execution.get('workspace') or (queue['data'].get('workspace') if queue else None) if remote else None
    diagnostics, diagnostics_limited = s3_diagnostics(workspace, allowed) if remote else saved_diagnostics(job_id, allowed)
    return {'job': job, 'events': events, 'next_before': next_before, 'attempts': attempts,
            'attempts_limited': len(values) > 50, 'diagnostics': diagnostics, 'diagnostics_limited': diagnostics_limited,
            'metrics': redact(metrics['data']) if metrics else None}
