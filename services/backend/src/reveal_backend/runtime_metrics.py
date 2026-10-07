"""Bounded, process-local latency summaries; no SQL text, parameters or URLs."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
import json
import logging
import os
import sys
import time

_lock = Lock()
_started = time.time()
_samples = {}
# Per-request database cost. A mutable holder set by the HTTP middleware is visible to sync endpoints (AnyIO
# copies the context into its worker thread) and async ones; work in other threads is simply not attributed.
_request = ContextVar('reveal_request_database', default=None)
REQUEST_FIELDS = ('statements', 'db_ms', 'pool_wait_ms', 'lock_wait_ms', 'connects', 'resets')
_WAITS = frozenset(('ACQUIRE', 'WRITER_WAIT'))        # in-process queueing, not database time
_SPANS = frozenset(('RESET_SKIPPED', 'LOCK_HOLD'))    # counters and spans that overlap other rows
_NOT_STATEMENTS = frozenset(('CONNECT', 'RESET', 'PING'))


@contextmanager
def measure(category, name):
    """Time one fixed-name phase, including failures, without recording inputs."""
    started = time.perf_counter()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        observe(category, name, (time.perf_counter()-started)*1000, failed)


def _attribute(cost, name, duration_ms):
    if name in _WAITS: cost['pool_wait_ms'] += duration_ms; return
    if name in _SPANS: return
    cost['db_ms'] += duration_ms
    if name == 'CONNECT': cost['connects'] += 1
    elif name == 'RESET': cost['resets'] += 1
    if name not in _NOT_STATEMENTS: cost['statements'] += 1   # SQL, FENCE, COMMIT and ROLLBACK
    if name == 'FENCE': cost['lock_wait_ms'] += duration_ms


def observe(category, name, duration_ms, failed=False, extra=None):
    """extra: additive numeric fields reported as mean_<field> on the row."""
    with _lock:
        key = (category, name)
        if key not in _samples:
            if len(_samples) >= 200: key = (category, 'other')
            _samples.setdefault(key, {'count': 0, 'errors': 0, 'total_ms': 0, 'recent': deque(maxlen=500)})
        item = _samples[key]
        item['count'] += 1; item['errors'] += int(failed); item['total_ms'] += duration_ms
        item['recent'].append(duration_ms)
        if extra:
            totals = item.setdefault('extra', {})
            for field, value in extra.items(): totals[field] = totals.get(field, 0) + value
        if category == 'database':
            cost = _request.get()
            if cost is not None: _attribute(cost, name, duration_ms)


def begin_request():
    cost = dict.fromkeys(REQUEST_FIELDS, 0)
    return cost, _request.set(cost)


def end_request(token): _request.reset(token)


class _Stdout(logging.StreamHandler):
    """Resolves sys.stdout per record, so replaced streams (test capture, reloaders) keep working."""
    def emit(self, record):
        self.stream = sys.stdout; super().emit(record)


_access = logging.getLogger('reveal.access')
if not _access.handlers:
    _handler = _Stdout(sys.stdout); _handler.setFormatter(logging.Formatter('%(message)s'))
    _access.addHandler(_handler); _access.setLevel(logging.INFO); _access.propagate = False


def log_request(route, status, duration_ms, cost):
    """One JSON line per request: the route template only, never the raw path, query, ids or parameters.
    Uvicorn's default logging config installs no root handler, so this logger owns its stdout handler."""
    if os.getenv('REVEAL_ACCESS_LOG', '1') == '0': return
    line = {'event': 'http', 'route': route, 'status': status, 'ms': round(duration_ms, 1)}
    for field in REQUEST_FIELDS: line[field] = round(cost[field], 1) if field.endswith('_ms') else cost[field]
    _access.info(json.dumps(line, separators=(',', ':')))


def metrics():
    with _lock:
        rows = []
        for (category, name), item in sorted(_samples.items()):
            values = sorted(item['recent'])
            row = {'category': category, 'name': name, 'count': item['count'], 'errors': item['errors'],
                   'mean_ms': round(item['total_ms']/item['count'], 2),
                   'p95_ms': round(values[max(0, (95*len(values)+99)//100-1)], 2)}
            for field, total in item.get('extra', {}).items(): row['mean_'+field] = round(total/item['count'], 2)
            rows.append(row)
    return {'pid': os.getpid(), 'uptime_seconds': round(time.time()-_started), 'rows': rows,
            'scope': 'This API process since startup; p95 uses the latest 500 samples per operation. HTTP timing ends at response headers, not the end of event streams. '
                     'database rows: ACQUIRE and WRITER_WAIT are in-process queueing; FENCE is the global-lock wait plus one round trip; '
                     'LOCK_HOLD runs from fence grant to COMMIT/ROLLBACK; RESET_SKIPPED counts clean releases.'}
