"""/readyz from memory: a per-process monitor checks readiness off the request path.

Every GENERATION_TTL_SECONDS one statement reads the application database and both active pointers
(reference_active, vector_active); the catalog's poller reuses that read. Sources (reference tables,
the verified Vector snapshot and provider, the artifact store) are re-verified in their own thread
whenever the pointers change, at least every READINESS_VERIFICATION_TTL_SECONDS, and at the next tick
after a failed verification. A probe does no I/O. Readiness fails closed: a failed read or verification
answers 503 until the next success, and so does a monitor whose last read is older than POINTER_MAX_AGE
(hung or stopped). Nothing here takes the write fence, writes, or retries a failed read inside a tick.
"""
import logging
import os
import threading
from time import monotonic

from .auth import Problem
from .catalog import GENERATION_TTL_SECONDS, READINESS_VERIFICATION_TTL_SECONDS, active_pointers, readiness_binding, vector_backend

LOGGER = logging.getLogger(__name__)
POINTER_MAX_AGE = 3 * GENERATION_TTL_SECONDS
VERIFY_MAX_AGE = 3 * READINESS_VERIFICATION_TTL_SECONDS
NEW_BINDING_GRACE = 2 * READINESS_VERIFICATION_TTL_SECONDS  # a cutover keeps the last verified sources this long
_monitor = None


def _answer(error):
    """What a probe raises for a stored failure: a new copy of a Problem (the stored one is shared by every probe),
    otherwise the API's generic retryable 503, as an unchecked database or storage error was before."""
    if isinstance(error, Problem): return Problem(error.status, error.code, error.detail, **error.extra)
    return Problem(503, 'SERVICE_UNAVAILABLE', 'The service is temporarily unavailable; retry shortly.')


class ReadinessMonitor:
    def __init__(self, repo, catalog, *, interval=GENERATION_TTL_SECONDS, clock=monotonic):
        # repo/catalog are callables, so a probe always reports on the objects the routes serve.
        self.repo, self.catalog, self.interval, self.clock = repo, catalog, interval, clock
        self.lock, self.stopped, self.thread, self.pid = threading.Lock(), threading.Event(), None, os.getpid()
        self.current = None    # (read at, database facts, binding, binding first seen at, error)
        self.verified = None   # (binding, verified at or failed attempt at, sources, error)
        self.verifying = False

    def tick(self):
        repo, catalog = self.repo(), self.catalog()
        facts = {}
        try:
            def inspect(tx):
                facts['database'] = ({'database': 'sqlite-test', 'tls': False} if repo.sqlite_path else
                    {'database': 'aurora-mysql', 'tls': True} if getattr(tx.connection, 'reveal_verified_tls', False) else None)
            generation, snapshot = active_pointers(repo, vector_backend(), inspect=inspect)
            database = facts['database'] or repo.readiness()  # sessions not opened by mysql_database.connect
            binding = readiness_binding(generation, snapshot)
        except Exception as error:
            with self.lock: previous, self.current = self.current, (self.clock(), None, None, None, error)
            if previous is None or previous[4] is None: LOGGER.warning('Readiness check failed (%s)', type(error).__name__)
            return
        observe = getattr(catalog, 'observe_pointers', None)
        if observe: observe(generation, snapshot)
        now = self.clock()
        with self.lock:
            seen = self.current[3] if self.current and self.current[2] == binding else now
            self.current = (now, database, binding, seen, None)
            verified = self.verified
            # a pass holds for the TTL; a failure (stamped when it was attempted) is retried at the next tick
            due = not self.verifying and (verified is None or verified[0] != binding or now - verified[1] >= (
                READINESS_VERIFICATION_TTL_SECONDS if verified[3] is None else self.interval / 2))
            if due: self.verifying = True
        if due: threading.Thread(target=self.verify, args=(catalog, binding, now), name='readiness-verify', daemon=True).start()

    def verify(self, catalog, binding, attempted):
        try:
            sources = catalog.verify_binding(binding)
            from .artifact_store import s3_enabled, store
            if s3_enabled(): store().check()
            outcome = (binding, self.clock(), sources, None)
        except Exception as error:
            outcome = (binding, attempted, None, error)
        with self.lock: previous, self.verified, self.verifying = self.verified, outcome, False
        if outcome[3] is not None and (previous is None or previous[3] is None):
            LOGGER.warning('Source verification failed (%s)', type(outcome[3]).__name__)

    def snapshot(self):
        """(database facts, sources) from memory, or raise why readiness is not confirmed. No I/O."""
        now = self.clock()
        with self.lock: current, verified = self.current, self.verified
        if current is None or now - current[0] > POINTER_MAX_AGE:
            raise Problem(503, 'SOURCE_NOT_READY', 'Readiness has not been confirmed recently.')
        if current[4] is not None: raise _answer(current[4])
        if verified is not None and verified[0] != current[2] and (verified[3] is not None or now - current[3] > NEW_BINDING_GRACE):
            verified = None  # the active pointers changed and their sources are not verified yet
        if verified is None: raise Problem(503, 'SOURCE_NOT_READY', 'Source verification is pending.')
        if now - verified[1] > VERIFY_MAX_AGE: raise Problem(503, 'SOURCE_NOT_READY', 'Source verification is overdue.')
        if verified[3] is not None: raise _answer(verified[3])
        return dict(current[1]), dict(verified[2])

    def run(self):
        while not self.stopped.is_set():
            try: self.tick()
            except Exception: LOGGER.warning('Readiness monitor tick failed', exc_info=True)
            self.stopped.wait(self.interval)

    def start(self):
        self.thread = threading.Thread(target=self.run, name='readiness-monitor', daemon=True); self.thread.start()
        return self

    def stop(self, timeout=None):
        self.stopped.set()
        if self.thread and timeout: self.thread.join(timeout)


def start(repo, catalog, **options):
    """Start this process's monitor (the API lifespan); probes read it through running()."""
    global _monitor
    if _monitor is not None and _monitor.pid == os.getpid(): _monitor.stop()
    _monitor = ReadinessMonitor(repo, catalog, **options).start()
    return _monitor


def stop(monitor):
    global _monitor
    monitor.stop()
    if _monitor is monitor: _monitor = None


def running():
    """The monitor of this process, or None (tests and tools without the API lifespan check synchronously)."""
    monitor = _monitor
    return monitor if monitor is not None and monitor.pid == os.getpid() and not monitor.stopped.is_set() else None
