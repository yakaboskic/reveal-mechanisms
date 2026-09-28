"""Bounded, process-local leases for application MySQL transactions only.

Importers/migrations use direct connections. A connection is never shared across
borrowers, and only a fully reset, initialized session can return to the pool.
"""
from collections import deque
from dataclasses import dataclass
import os
import threading
import time


@dataclass(eq=False)
class _Entry:
    connection: object
    born: float
    idle_since: float
    pid: int


def _close(connection, *, inherited=False):
    try:
        # Sending QUIT on an inherited socket could terminate the parent's
        # session. PyMySQL's destructor uses this local descriptor-only close.
        if inherited: connection._force_close()
        else: connection.close()
    except Exception:
        pass


class Pool:
    def __init__(self, factory, reset, *, maximum=4, wait_seconds=5, idle_seconds=60,
                 lifetime_seconds=300, clock=time.monotonic):
        if maximum < 1 or min(wait_seconds, idle_seconds, lifetime_seconds) <= 0:
            raise ValueError('Pool bounds must be positive')
        self.factory, self.reset = factory, reset
        self.maximum, self.wait_seconds = maximum, wait_seconds
        self.idle_seconds, self.lifetime_seconds = idle_seconds, lifetime_seconds
        self.clock = clock; self.pid = os.getpid()
        self.condition = threading.Condition(); self.idle = deque(); self.entries = set(); self.creating = 0
        self.closed = False

    def _process(self):
        if self.pid == os.getpid(): return
        # The child has one thread; never acquire a possibly inherited locked
        # Condition. Discard every inherited descriptor, including active ones.
        for entry in self.entries: _close(entry.connection, inherited=True)
        self.condition = threading.Condition(); self.idle = deque(); self.entries = set(); self.creating = 0
        self.pid = os.getpid(); self.closed = False

    def acquire(self):
        self._process(); deadline = self.clock() + self.wait_seconds
        while True:
            stale = []
            with self.condition:
                if self.closed: raise RuntimeError('Connection pool is closed')
                while self.idle:
                    entry = self.idle.pop(); stamp = self.clock()
                    if stamp - entry.idle_since >= self.idle_seconds or stamp - entry.born >= self.lifetime_seconds:
                        self.entries.remove(entry); stale.append(entry)
                    else:
                        # Reset on release also checked the transport. No ping
                        # here: transaction SQL fails closed if it died idle.
                        selected = entry; break
                else: selected = None
                if selected is None and len(self.entries) + self.creating < self.maximum:
                    self.creating += 1; create = True
                else: create = False
                if selected is None and not create:
                    remaining = deadline - self.clock()
                    if remaining <= 0: raise TimeoutError('Application database connection pool is busy')
                    self.condition.wait(remaining)
            for entry in stale: _close(entry.connection)
            if selected is not None: return Lease(self, selected)
            if create:
                try:
                    connection = self.factory(); stamp = self.clock()
                    entry = _Entry(connection, stamp, stamp, self.pid)
                except BaseException:
                    with self.condition: self.creating -= 1; self.condition.notify()
                    raise
                with self.condition:
                    self.creating -= 1
                    if self.closed:
                        _close(connection); self.condition.notify(); raise RuntimeError('Connection pool is closed')
                    self.entries.add(entry); self.condition.notify()
                return Lease(self, entry)

    def release(self, entry, reusable):
        if entry.pid != os.getpid():
            _close(entry.connection, inherited=True); return
        interruption = None
        if reusable and not self.closed and self.clock() - entry.born < self.lifetime_seconds:
            try: self.reset(entry.connection)
            except BaseException as error:
                reusable = False
                if not isinstance(error, Exception): interruption = error
        else: reusable = False
        with self.condition:
            discard = not reusable or self.closed
            if not discard:
                entry.idle_since = self.clock(); self.idle.append(entry)
            else:
                self.entries.discard(entry)
            self.condition.notify()
        # Once published to idle, a new lease may already own the connection.
        # A later pool.close() cannot change this release's disposal decision.
        if discard: _close(entry.connection)
        if interruption is not None: raise interruption

    def close(self):
        self._process()
        with self.condition:
            self.closed = True; idle = list(self.idle); self.idle.clear()
            for entry in idle: self.entries.discard(entry)
            self.condition.notify_all()
        for entry in idle: _close(entry.connection)


class Lease:
    def __init__(self, pool, entry):
        self._pool, self._entry = pool, entry
        self._closed = False; self._settled = False; self._broken = False; self._cursors = []

    def _check(self):
        if self._closed or self._entry.pid != os.getpid(): raise RuntimeError('Database lease is no longer active')

    def cursor(self, *args, **kwargs):
        self._check(); cursor = _Cursor(self, self._entry.connection.cursor(*args, **kwargs))
        self._cursors.append(cursor); return cursor

    def commit(self):
        self._check()
        try: result = self._entry.connection.commit()
        except BaseException:
            self._broken = True; raise
        self._settled = True; return result

    def rollback(self):
        self._check()
        try: result = self._entry.connection.rollback()
        except BaseException:
            self._broken = True; raise
        self._settled = True; return result

    def close(self):
        if self._closed: return
        try:
            if self._entry.pid == os.getpid():
                for cursor in self._cursors:
                    try: cursor.close()
                    except BaseException as error:
                        self._broken = True
                        if not isinstance(error, Exception): raise
        finally:
            self._closed = True
            self._pool.release(self._entry, self._settled and not self._broken)

    def ping(self, reconnect=False):
        self._check()
        if reconnect: raise ValueError('Pooled sessions never reconnect in place')
        try: return self._entry.connection.ping(reconnect=False)
        except BaseException:
            self._broken = True; raise

    def __getattr__(self, name):
        if name == 'connect': raise AttributeError('Pooled sessions never reconnect in place')
        self._check(); value = getattr(self._entry.connection, name)
        if not callable(value): return value
        def call(*args, **kwargs):
            self._check(); return value(*args, **kwargs)
        return call

    def __enter__(self): self._check(); return self
    def __exit__(self, *args): self.close()


class _Cursor:
    def __init__(self, lease, cursor): self.lease, self.cursor, self.closed = lease, cursor, False
    def _check(self):
        self.lease._check()
        if self.closed: raise RuntimeError('Database cursor is closed')
    def __getattr__(self, name):
        if name == 'connection':
            self._check(); return self.lease
        self._check(); value = getattr(self.cursor, name)
        if not callable(value): return value
        def call(*args, **kwargs):
            self._check(); return value(*args, **kwargs)
        return call
    def close(self):
        if self.closed: return
        self._check(); self.cursor.close(); self.closed = True
    def __enter__(self): self._check(); return self
    def __exit__(self, *args): self.close()
    def __iter__(self): return self
    def __next__(self): self._check(); return next(self.cursor)
