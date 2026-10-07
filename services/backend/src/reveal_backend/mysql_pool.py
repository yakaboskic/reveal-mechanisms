"""Bounded, process-local leases for application MySQL transactions only.

Importers/migrations use direct connections. A connection is never shared across
borrowers. It returns to idle only after a full reset, or when the OK packet of its
final COMMIT/ROLLBACK proves no open transaction and every statement on the lease
was session-neutral; anything else is reset or discarded.
"""
from collections import deque
from dataclasses import dataclass
import os
import re
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


# PyMySQL never enables CLIENT_MULTI_STATEMENTS, so one execute() is one statement. Anything not provably
# session-neutral (SET, USE, SHOW, CALL, a comment first, @variables, user locks, temporary tables, COMMIT
# through a cursor...) dirties the lease, which then gets the full reset.
_NEUTRAL = re.compile(r'\s*\(*\s*(SELECT|WITH|INSERT|UPDATE|DELETE|START\s+TRANSACTION)\b', re.I)
_STATEFUL = re.compile(r'@|\b(GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS|IS_USED_LOCK|LAST_INSERT_ID|TEMPORARY|'
                       r'OUTFILE|DUMPFILE|SQL_CALC_FOUND_ROWS)\b', re.I)
_FETCH = frozenset(('fetchone', 'fetchmany', 'fetchall'))


def session_neutral(sql):
    if isinstance(sql, (bytes, bytearray)): sql = bytes(sql).decode('utf-8', 'replace')
    return isinstance(sql, str) and bool(_NEUTRAL.match(sql)) and not _STATEFUL.search(sql)


class Pool:
    """unchanged(connection) proves a clean lease reusable with no round trip; otherwise release resets."""
    def __init__(self, factory, reset, *, unchanged=None, maximum=4, wait_seconds=5, idle_seconds=60,
                 lifetime_seconds=300, clock=time.monotonic):
        if maximum < 1 or min(wait_seconds, idle_seconds, lifetime_seconds) <= 0:
            raise ValueError('Pool bounds must be positive')
        self.factory, self.reset, self.unchanged = factory, reset, unchanged
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
                        # The release reset or COMMIT/ROLLBACK OK packet checked the
                        # transport. Transaction SQL fails closed if it died idle.
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

    def release(self, entry, reusable, clean=False):
        if entry.pid != os.getpid():
            _close(entry.connection, inherited=True); return
        interruption = None
        if reusable and not self.closed and self.clock() - entry.born < self.lifetime_seconds:
            try:
                try: skip = bool(clean and self.unchanged is not None and self.unchanged(entry.connection))
                except Exception: skip = False
                if not skip: self.reset(entry.connection)
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
        # _quiescent: the last operation was a successful COMMIT/ROLLBACK. _dirty (sticky): something with
        # an unknown session effect ran, so release must fully reset.
        self._quiescent = False; self._dirty = False

    def _check(self):
        if self._closed or self._entry.pid != os.getpid(): raise RuntimeError('Database lease is no longer active')

    def _statement(self, sql):
        self._quiescent = False
        if not session_neutral(sql): self._dirty = True

    def cursor(self, *args, **kwargs):
        self._check()
        if args or kwargs: self._dirty = True  # unbuffered or custom cursor classes
        cursor = _Cursor(self, self._entry.connection.cursor(*args, **kwargs))
        self._cursors.append(cursor); return cursor

    def commit(self):
        self._check(); self._quiescent = False
        try: result = self._entry.connection.commit()
        except BaseException:
            self._broken = True; raise
        self._settled = self._quiescent = True; return result

    def rollback(self):
        self._check(); self._quiescent = False
        try: result = self._entry.connection.rollback()
        except BaseException:
            self._broken = True; raise
        self._settled = self._quiescent = True; return result

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
            self._pool.release(self._entry, self._settled and not self._broken,
                               clean=self._quiescent and not self._dirty)

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
            # select_db, query, autocommit, set_character_set...: unknown session effect.
            self._check(); self._dirty = True; self._quiescent = False; return value(*args, **kwargs)
        return call

    def __enter__(self): self._check(); return self
    def __exit__(self, *args): self.close()


class _Cursor:
    def __init__(self, lease, cursor): self.lease, self.cursor, self.closed = lease, cursor, False
    def _check(self):
        self.lease._check()
        if self.closed: raise RuntimeError('Database cursor is closed')
    def _call(self, method, args, kwargs):
        try: return method(*args, **kwargs)
        except Exception:
            self.lease._dirty = True; raise
        except BaseException:
            # An interrupted exchange can leave a reply unread; a later OK would desynchronize the next borrower.
            self.lease._broken = True; raise
    def execute(self, query, *args, **kwargs):
        self._check(); self.lease._statement(query); return self._call(self.cursor.execute, (query, *args), kwargs)
    def executemany(self, query, *args, **kwargs):
        self._check(); self.lease._statement(query); return self._call(self.cursor.executemany, (query, *args), kwargs)
    def __getattr__(self, name):
        if name == 'connection':
            self._check(); return self.lease
        self._check(); value = getattr(self.cursor, name)
        if not callable(value): return value
        def call(*args, **kwargs):
            self._check()
            if name not in _FETCH: self.lease._dirty = True  # callproc, nextset, scroll...
            return self._call(value, args, kwargs)
        return call
    def close(self):
        if self.closed: return
        self._check(); self.cursor.close(); self.closed = True
    def __enter__(self): self._check(); return self
    def __exit__(self, *args): self.close()
    def __iter__(self): return self
    def __next__(self): self._check(); return next(self.cursor)
