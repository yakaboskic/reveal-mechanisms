"""Bounded, process-local latency summaries; no SQL text, parameters or URLs."""
from collections import deque
from threading import Lock
import os
import time

_lock = Lock()
_started = time.time()
_samples = {}


def observe(category, name, duration_ms, failed=False):
    with _lock:
        key = (category, name)
        if key not in _samples:
            if len(_samples) >= 200: key = (category, 'other')
            _samples.setdefault(key, {'count': 0, 'errors': 0, 'total_ms': 0, 'recent': deque(maxlen=500)})
        item = _samples[key]
        item['count'] += 1; item['errors'] += int(failed); item['total_ms'] += duration_ms
        item['recent'].append(duration_ms)


def metrics():
    with _lock:
        rows = []
        for (category, name), item in sorted(_samples.items()):
            values = sorted(item['recent'])
            rows.append({'category': category, 'name': name, 'count': item['count'], 'errors': item['errors'],
                         'mean_ms': round(item['total_ms']/item['count'], 2),
                         'p95_ms': round(values[max(0, (95*len(values)+99)//100-1)], 2)})
    return {'pid': os.getpid(), 'uptime_seconds': round(time.time()-_started), 'rows': rows,
            'scope': 'This API process since startup; p95 uses the latest 500 samples per operation. HTTP timing ends at response headers, not the end of event streams.'}
