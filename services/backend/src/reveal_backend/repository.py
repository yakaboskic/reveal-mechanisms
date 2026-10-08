"""Small transactional application repository; Aurora is the production store.

One indexed row lock serializes short application transactions, including claims,
leases, idempotency, ownership transfers and acceptance/outbox. Network/agent work
never runs inside this lock. Only two writes run outside it: deleting
notification_outbox rows after their Redis wakeup was published
(discard_notifications), and appending one new immutable row of an untracked kind
that no fenced decision reads before its id is returned (append). SQLite is
available only to isolated unit tests.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from uuid import uuid4
from . import runtime_metrics as metrics
from .mysql_database import SESSION_LOCK_WAIT_SECONDS, application_session_unchanged
from .mysql_pool import DatabaseBusy
from .runtime_config import ROOT, mysql_connection, application_mysql_connection

def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
def uid(): return str(uuid4())
def canonical(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()

class Conflict(Exception): pass

class FenceBusy(DatabaseBusy):
    """transaction(nowait=True) found the write fence held, in this process or another; nothing was written."""

SELECT_ROW = 'SELECT owner_id,version,payload FROM reveal_records WHERE kind=%s AND id=%s'
FENCE = 'SELECT revision FROM reveal_transaction_lock WHERE id=1 FOR UPDATE'
LOCK_NOWAIT = 3572   # ER_LOCK_NOWAIT: FOR UPDATE NOWAIT found the row locked
LOCK_WAIT_TIMEOUT = 1205   # ER_LOCK_WAIT_TIMEOUT: queued on the fence past innodb_lock_wait_timeout
_READ = re.compile(r'\s*\(*\s*(SELECT|SHOW)\b', re.I)
_PLAIN_SELECT = re.compile(r'\s*\(*\s*SELECT\b', re.I)
_NOT_PLAIN = re.compile(r'@|\b(FOR\s+UPDATE|FOR\s+SHARE|LOCK\s+IN\s+SHARE\s+MODE|INTO|GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS|'
                        r'IS_USED_LOCK|SLEEP|LAST_INSERT_ID)\b', re.I)
_MISS = object()
_TEXT_BUDGET = 8 << 20  # payload characters one transaction may remember; beyond it rows keep only owner/version

def _exact(identity):
    # ids are ascii_bin (PAD SPACE): a trailing space or a non-ASCII literal can match a different stored id.
    return isinstance(identity, str) and identity.isascii() and not identity.endswith(' ')

def application_prefix(value=None):
    value = value or os.getenv('REVEAL_APPLICATION_TABLE_PREFIX', 'reveal')
    if not re.fullmatch(r'reveal(?:_[a-z][a-z0-9_]{0,30})?', value):
        raise ValueError('Invalid application table prefix')
    return value

def application_sql(sql, prefix):
    return re.sub(r'\breveal_(records|transaction_lock)\b', lambda match: prefix+'_'+match[1], sql)

class Transaction:
    """Rows read in this transaction are remembered by (kind, id). Under REPEATABLE READ a re-read returns the
    same bytes, so get/put/update_existing/remove skip it. Rows this transaction wrote keep only owner and
    version (MySQL JSON need not round-trip our text), and any SQL that is not a plain read and was not sent by
    a keyed helper forgets everything. The map lives on this object, never on the pooled connection."""
    def __init__(self, connection, sqlite=False, table_prefix='reveal'):
        self.connection, self.sqlite, self.table_prefix = connection, sqlite, table_prefix
        self.workspace_changes, self.notification_jobs = {}, set()
        self._rows, self._keyed, self._held = {}, False, 0
    def execute(self, sql, params=()):
        if self._rows and not self._keyed and not _READ.match(sql): self._rows.clear()
        cursor = self.connection.cursor()
        started = time.perf_counter()
        failed = False
        try:
            sql = application_sql(sql, self.table_prefix)
            cursor.execute(sql.replace('%s', '?') if self.sqlite else sql, params)
        except BaseException:
            failed = True
            raise
        finally:
            name = sql.split()[0].upper()
            if name == 'SELECT' and sql.endswith((' FOR UPDATE', ' FOR UPDATE NOWAIT')): name = 'FENCE'
            metrics.observe('database', name, (time.perf_counter()-started)*1000, failed)
        return cursor
    def _known(self, kind, identity):
        return self._rows.get((kind, identity), _MISS) if _exact(identity) else _MISS
    def absent(self, kind, identity):
        """True when this transaction read (kind, identity) and found no row, so it can be inserted unread."""
        return self._known(kind, identity) is None
    def _learn(self, kind, identity, owner=None, version=None, text=None, absent=False):
        if not _exact(identity): return
        if absent: self._rows[(kind, identity)] = None; return
        if text is not None:
            if self._held + len(text) > _TEXT_BUDGET: text = None
            else: self._held += len(text)
        self._rows[(kind, identity)] = (owner, version, text)
    def _write(self, sql, params, identities):
        # A keyed write keeps the map; one with an inexact id may change another key's row, so it clears it.
        self._keyed = all(_exact(identity) for identity in identities)
        try: return self.execute(sql, params)
        finally: self._keyed = False
    def get(self, kind, identity):
        entry = self._known(kind, identity)
        if entry is None: return None
        if entry is not _MISS and entry[2] is not None:
            return {'owner': entry[0], 'version': entry[1], 'data': json.loads(entry[2])}
        row = self.execute(SELECT_ROW, (kind, identity)).fetchone()
        if row is None: self._learn(kind, identity, absent=True); return None
        self._learn(kind, identity, row[0], row[1], row[2])
        return {'owner': row[0], 'version': row[1], 'data': json.loads(row[2])}
    def _old(self, kind, identity, data):
        """The row a write replaces; data only when track() diffs it."""
        entry = self._known(kind, identity)
        if entry is _MISS and not data:
            row = self.execute('SELECT owner_id,version FROM reveal_records WHERE kind=%s AND id=%s', (kind, identity)).fetchone()
            if row is None: self._learn(kind, identity, absent=True); return None
            self._learn(kind, identity, row[0], row[1]); return {'owner': row[0], 'version': row[1], 'data': None}
        if entry is None: return None
        if entry is _MISS or (data and entry[2] is None): return self.get(kind, identity)
        return {'owner': entry[0], 'version': entry[1], 'data': json.loads(entry[2]) if data else None}
    def get_many(self, kind, identities):
        if not identities: return {}
        rows = self.execute('SELECT id,owner_id,version,payload FROM reveal_records WHERE kind=%s AND id IN ('+
            ','.join(['%s'] * len(identities))+')', (kind, *identities)).fetchall()
        result = {}
        for row in rows:
            self._learn(kind, row[0], row[1], row[2], row[3])
            result[row[0]] = {'owner': row[1], 'version': row[2], 'data': json.loads(row[3])}
        for identity in identities:
            if identity not in result: self._learn(kind, identity, absent=True)
        return result
    def get_records(self, keys, *, bare=()):
        """Fetch exact heterogeneous record keys without widening access. Rows of a kind in bare come back with
        owner and version only (data None), for existence checks that need no payload."""
        keys = list(dict.fromkeys(keys)); result = {}
        column = 'CASE WHEN kind IN (' + ','.join(['%s'] * len(bare)) + ') THEN NULL ELSE payload END' if bare else 'payload'
        for offset in range(0, len(keys), 250):
            batch = keys[offset:offset + 250]
            rows = self.execute('SELECT kind,id,owner_id,version,' + column + ' FROM reveal_records WHERE (kind,id) IN (' +
                ','.join(['(%s,%s)'] * len(batch)) + ')', (*bare, *(value for key in batch for value in key))).fetchall()
            for row in rows:
                self._learn(row[0], row[1], row[2], row[3], row[4])
                result[(row[0], row[1])] = {'owner': row[2], 'version': row[3], 'data': None if row[4] is None else json.loads(row[4])}
            for key in batch:
                if key not in result: self._learn(*key, absent=True)
        return result
    def exists(self, kind, identity):
        """Whether the row exists, from this transaction's reads and writes when known; never reads its payload."""
        entry = self._known(kind, identity)
        if entry is not _MISS: return entry is not None
        return self._old(kind, identity, False) is not None
    def insert_many(self, records):
        """Insert new records in one SQL statement; conflicts roll back the batch."""
        if not records: return
        values = []
        for kind, identity, owner, data in records:
            values.extend((kind, identity, owner, 1, canonical(data), now()))
        self._write('INSERT INTO reveal_records(kind,id,owner_id,version,payload,updated_at) VALUES '+
            ','.join(['(%s,%s,%s,%s,%s,%s)'] * len(records)), tuple(values), [record[1] for record in records])
        from .workspace_events import track
        for kind, identity, owner, data in records:
            self._learn(kind, identity, owner, 1)
            track(self, kind, identity, owner, data)
    def insert(self, kind, identity, owner, data, *, replace=False):
        """Insert a key known to be new with no pre-read. replace=True puts instead if it exists after all."""
        self.insert_new([(kind, identity, owner, data)], (kind,) if replace else ())
    def insert_new(self, records, replace=()):
        """Insert keys known to be new in one statement with no pre-read. If one exists after all, the failed
        statement changed nothing; rows of a kind in replace are then put() one by one, others inserted alone."""
        try: self.insert_many(records)
        except Exception as error:
            import pymysql
            if not replace or not isinstance(error, (sqlite3.IntegrityError, pymysql.err.IntegrityError)): raise
            for kind, identity, owner, data in records:
                if kind in replace: self.put(kind, identity, owner, data)
                else: self.insert_many([(kind, identity, owner, data)])
    def apply_puts(self, writes, fresh=(), *, rows=250, size=4 << 20):
        """The final state of sequential put()s in one pass. Each (kind, id, owner, data, count) is `count` puts of
        one key whose last data is `data`; a tracked kind repeats identical data. Owners, payloads, versions and
        workspace events are the puts'. Keys this transaction read as absent, and `fresh` rows (new uuids), go in
        multi-row INSERTs; rows read with an unchanged payload get one version bump per count; each changed row one
        UPDATE; a key this transaction has not read is put() as before."""
        from .workspace_events import tracked, track
        inserts, bumps = [(kind, identity, owner, data, 1) for kind, identity, owner, data in fresh], {}
        for kind, identity, owner, data, count in writes:
            entry = self._known(kind, identity)
            if entry is None: inserts.append((kind, identity, owner, data, count)); continue
            if entry is _MISS or (entry[2] is None and tracked(kind)):
                for _ in range(count): self.put(kind, identity, owner, data)
                continue
            old = json.loads(entry[2]) if entry[2] is not None else _MISS
            if old == data and entry[0] == owner:
                bumps.setdefault(count, []).append((kind, identity, owner, entry[1])); continue
            cursor = self._write('UPDATE reveal_records SET owner_id=%s,version=%s,payload=%s,updated_at=%s WHERE kind=%s AND id=%s AND version=%s',
                                 (owner, entry[1] + count, canonical(data), now(), kind, identity, entry[1]), [identity])
            if cursor.rowcount != 1: raise Conflict('Record changed outside the write fence')
            self._learn(kind, identity, owner, entry[1] + count)
            track(self, kind, identity, owner, data, None if old is _MISS else {'owner': entry[0], 'version': entry[1], 'data': old},
                  revision=entry[1] + 1)
        for count, unchanged in bumps.items():
            for offset in range(0, len(unchanged), 250):
                part = unchanged[offset:offset + 250]
                cursor = self._write('UPDATE reveal_records SET version=version+%s,updated_at=%s WHERE (kind,id) IN (' +
                    ','.join(['(%s,%s)'] * len(part)) + ')', (count, now(), *(value for row in part for value in row[:2])),
                    [row[1] for row in part])
                if cursor.rowcount != len(part): raise Conflict('Record changed outside the write fence')
                for kind, identity, owner, version in part: self._learn(kind, identity, owner, version + count)
        batch, held = [], 0
        def flush():
            if not batch: return
            self._write('INSERT INTO reveal_records(kind,id,owner_id,version,payload,updated_at) VALUES '+
                ','.join(['(%s,%s,%s,%s,%s,%s)'] * len(batch)), tuple(value for row in batch for value in row), [row[1] for row in batch])
            batch.clear()
        for kind, identity, owner, data, count in inserts:
            text = canonical(data)
            if batch and (len(batch) >= rows or held + len(text) > size): flush(); held = 0
            batch.append((kind, identity, owner, count, text, now())); held += len(text)
        flush()
        for kind, identity, owner, data, count in inserts:
            self._learn(kind, identity, owner, count)
            track(self, kind, identity, owner, data)
    def update_existing(self, kind, identity, owner, data):
        """Update a row already read under the transaction's exclusive fence."""
        from .workspace_events import tracked, track
        old = self._old(kind, identity, True) if tracked(kind) else None
        entry = self._known(kind, identity)
        known = old['version'] if old else entry[1] if entry is not _MISS and entry is not None else None
        sql = 'UPDATE reveal_records SET owner_id=%s,version=version+1,payload=%s,updated_at=%s WHERE kind=%s AND id=%s'
        params = (owner, canonical(data), now(), kind, identity)
        cursor = self._write(sql + ' AND version=%s', params + (known,), [identity]) if known is not None else \
            self._write(sql, params, [identity])
        if cursor.rowcount != 1: raise Conflict('Expected existing record')
        if known is not None: self._learn(kind, identity, owner, known + 1)
        elif _exact(identity): self._rows.pop((kind, identity), None)
        track(self, kind, identity, owner, data, old, revision=old['version']+1 if old else 1)
    def list(self, kind, owner=None):
        sql, args = 'SELECT id,owner_id,version,payload,updated_at FROM reveal_records WHERE kind=%s', [kind]
        if owner is not None: sql += ' AND owner_id=%s'; args.append(owner)
        # MySQL filesort can exhaust its sort buffer on large JSON payloads.
        # These rows are already fetched in full; sort their references here,
        # preserving the stored timestamp ordering and ascending ID tie-break.
        rows = sorted(self.execute(sql, args).fetchall(), key=lambda row: row[0])
        rows.sort(key=lambda row: row[4], reverse=True)
        for r in rows: self._learn(kind, r[0], r[1], r[2], r[3])
        return [{'id': r[0], 'owner': r[1], 'version': r[2], 'data': json.loads(r[3])}
                for r in rows]
    def active_box_count(self, namespace):
        """Count live reservations under the write fence without loading JSON rows."""
        def value(alias, field):
            expr = "JSON_EXTRACT(" + alias + ".payload,'$." + field + "')"
            return "CAST(" + expr + " AS TEXT)" if self.sqlite else "NULLIF(JSON_UNQUOTE(" + expr + "),'null')"
        def yes(alias, field): return value(alias, field) + " IN ('true','1')"
        same = ' AND '.join(value('c', field) + '=' + value('e', target) for field, target in (
            ('namespace', 'namespace'), ('job_id', 'job_id'), ('authoring_attempt', 'authoring_attempt'), ('box_id', 'box.box_id')))
        captured = ("COALESCE(" + value('c', 'abandoned') + ",'false') NOT IN ('true','1') AND "
                    + yes('e', 'capture_complete') + ' AND ' + value('c', 'capture_sha256') + '=' + value('e', 'capture_sha256')
                    + ' AND ' + value('c', 'workspace') + " IS NOT NULL AND " + value('c', 'workspace') + " NOT IN ('','{}','false')")
        abandoned = (yes('c', 'abandoned') + ' AND ' + yes('e', 'cleanup_abandoned')
                     + ' AND ' + value('c', 'recovery_generation') + '=' + value('e', 'recovery_generation'))
        sql = ("SELECT SUM(active) FROM (SELECT COUNT(*) AS active FROM reveal_records e WHERE e.kind='execution' AND "
            + value('e', 'namespace') + '=%s AND ' + yes('e', 'capacity_reserved')
            + " AND NOT EXISTS (SELECT 1 FROM reveal_records c WHERE c.kind IN ('workflow_cleanup','workflow_cleanup_completed')"
            + ' AND c.id=' + value('e', 'cleanup_id') + ' AND ' + same + ' AND ((' + captured + ') OR (' + abandoned + ')))'
            + " UNION ALL SELECT COUNT(*) AS active FROM reveal_records q WHERE q.kind='queue' AND COALESCE("
            + value('q', 'namespace') + ",'reveal')=%s AND COALESCE(" + value('q', 'transport') + ",'')<>'workflow'"
            + ' AND ' + value('q', 'remote_handle.box_id') + ' IS NOT NULL AND COALESCE('
            + value('q', 'remote_handle.phase') + ",'')<>'deleted') reservations")
        return int(self.execute(sql, (namespace, namespace)).fetchone()[0])

    def put(self, kind, identity, owner, data, expected=None):
        from .workspace_events import tracked, track
        old = self._old(kind, identity, tracked(kind))
        if expected is not None and (old is None or old['version'] != expected): raise Conflict('Version conflict')
        version = old['version'] + 1 if old else 1
        if old:
            cursor = self._write('UPDATE reveal_records SET owner_id=%s,version=%s,payload=%s,updated_at=%s WHERE kind=%s AND id=%s AND version=%s',
                                 (owner, version, canonical(data), now(), kind, identity, old['version']), [identity])
            if cursor.rowcount != 1: raise Conflict('Record changed outside the write fence')
        else:
            self._write('INSERT INTO reveal_records(kind,id,owner_id,version,payload,updated_at) VALUES (%s,%s,%s,%s,%s,%s)',
                        (kind, identity, owner, version, canonical(data), now()), [identity])
        self._learn(kind, identity, owner, version)
        track(self, kind, identity, owner, data, old, revision=version)
        return version
    def remove(self, kind, identity):
        from .workspace_events import tracked, track
        old = self._old(kind, identity, True) if tracked(kind) else None
        if old: track(self, kind, identity, old['owner'], old['data'], operation='remove', revision=old['version']+1)
        self._write('DELETE FROM reveal_records WHERE kind=%s AND id=%s', (kind, identity), [identity])
        self._learn(kind, identity, absent=True)
    def transfer(self, source, target):
        from .workspace_events import ownership_changed
        ownership_changed(self, source, target)
        from .research_ownership import transfer_workspace
        transfer_workspace(self, source, target)
        for kind in ('object','account','paragraph','grant','account_membership','publication','exploration','outbox','artifact','object_document','scientific_document','object_observation'):
            for row in self.list(kind,source):
                data=row['data']
                identity=(data.get('result',data).get('root_id') if kind in ('object','account','paragraph') else
                    data['target_id'] if kind=='grant' else data['sha256'] if kind in ('artifact','scientific_document') else data['object_id'] if kind in ('object_document','object_observation') else data['account_id'] if kind in ('account_membership','outbox','publication') else data['knowledge_gap']['id'])
                destination=digest([target,identity,'default-paragraph']) if kind=='outbox' else digest([target,identity])
                if kind=='object_observation':
                    from .evidence_package import canonical_json,sha256
                    destination=digest([target,identity,sha256(canonical_json(data['payload']))])
                if not self.get(kind,destination): self.put(kind,destination,target,data)
                self.remove(kind,row['id'])
        # Shared forecast versions/indexes are service authority, never personal
        # workspace state, even if a malformed row has a user owner.
        excluded = ('principal', 'identity', 'workspace_event', 'workspace_cursor',
                    'cfde_assessment_shared', 'cfde_assessment_shared_cache')
        self.execute('UPDATE reveal_records SET owner_id=%s WHERE owner_id=%s AND kind NOT IN (' +
                     ','.join(['%s'] * len(excluded)) + ')', (target, source, *excluded))

class SingleRead(Transaction):
    """Exactly one plain SELECT: no locking read, user variable or lock, INTO or second statement."""
    _used = False
    def execute(self, sql, params=()):
        if self._used: raise RuntimeError('single_read() runs one statement; use read_transaction()')
        if not isinstance(sql, str) or not _PLAIN_SELECT.match(sql) or _NOT_PLAIN.search(sql):
            raise RuntimeError('single_read() accepts only one plain SELECT')
        self._used = True
        return super().execute(sql, params)

# Admission to the global fence: at most WRITERS fenced transactions per table prefix per process hold a pooled
# session (one with the fence, one queued on it); the rest wait here, not on a pooled session, for at most the
# session lock wait, so a queued writer never fails sooner than it would have on FOR UPDATE. The database row
# lock stays the only cross-process authority.
WRITERS = 2
_writer_gates, _writer_lock = {}, threading.Lock()

def _after_fork():
    global _writer_gates, _writer_lock
    _writer_gates, _writer_lock = {}, threading.Lock()

if hasattr(os, 'register_at_fork'): os.register_at_fork(after_in_child=_after_fork)

def writer_gate(prefix):
    with _writer_lock:
        gate = _writer_gates.get(prefix)
        if gate is None: gate = _writer_gates[prefix] = threading.BoundedSemaphore(WRITERS)
        return gate

class Repository:
    def __init__(self, sqlite_path=None, table_prefix=None):
        self.sqlite_path = sqlite_path
        self.table_prefix = application_prefix(table_prefix)
    def connect(self):
        if self.sqlite_path:
            connection = sqlite3.connect(self.sqlite_path, timeout=30)
            return connection
        return application_mysql_connection()
    @contextmanager
    def read_transaction(self, *, utc=False):
        """Consistent authorized reads without taking the application write mutex."""
        connection = self.connect()
        try:
            tx = Transaction(connection, bool(self.sqlite_path), self.table_prefix)
            if self.sqlite_path:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
            else:
                # Admin readers serialize native TIMESTAMP columns as UTC;
                # their display timezone is chosen by the browser.
                if not getattr(connection, 'reveal_session_defaults', False):
                    if utc: tx.execute("SET time_zone = '+00:00'")
                    tx.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                tx.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            yield tx
            with metrics.measure('database', 'COMMIT'): connection.commit()
        except BaseException:
            try:
                with metrics.measure('database', 'ROLLBACK'): connection.rollback()
            except Exception: pass  # Retain the original query/commit error.
            raise
        finally: connection.close()
    @contextmanager
    def single_read(self):
        """One plain SELECT with no START: InnoDB's statement-level consistent read, ended by ROLLBACK, so a clean
        pooled lease costs 2 round trips. Only for reads that are genuinely one statement, never an
        authorization+data pair: those need read_transaction()'s single snapshot."""
        connection = self.connect()
        try:
            tx = SingleRead(connection, bool(self.sqlite_path), self.table_prefix)
            if self.sqlite_path:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
            elif not application_session_unchanged(connection):
                # Unpooled, unproven or open-transaction session: chosen before any SQL is sent, never a retry.
                if not getattr(connection, 'reveal_session_defaults', False):
                    Transaction.execute(tx, 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                Transaction.execute(tx, 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            yield tx
            # Ends the implicit read view; nothing can persist, and the lease settles for a clean release.
            with metrics.measure('database', 'ROLLBACK'): connection.rollback()
        except BaseException:
            try:
                with metrics.measure('database', 'ROLLBACK'): connection.rollback()
            except Exception: pass
            raise
        finally: connection.close()
    @contextmanager
    def transaction(self, *, nowait=False):
        """The global write fence. nowait (background sweeps) raises FenceBusy at once instead of queueing
        behind another writer, in this process or any other. A writer that queued on the fence past the session's
        lock wait gets DatabaseBusy: the body never ran, so the caller's retry (idempotency key) is safe."""
        gate = None
        if not self.sqlite_path:
            gate = writer_gate(self.table_prefix); started = time.perf_counter()
            admitted = gate.acquire(blocking=False) if nowait else gate.acquire(timeout=SESSION_LOCK_WAIT_SECONDS)
            metrics.observe('database', 'WRITER_WAIT', (time.perf_counter()-started)*1000, not admitted)
            if not admitted: raise (FenceBusy if nowait else DatabaseBusy)('Application database writers are busy')
        pending = []
        try:
            connection = self.connect()
            granted = None
            try:
                tx = Transaction(connection, bool(self.sqlite_path), self.table_prefix)
                if self.sqlite_path: connection.execute('BEGIN IMMEDIATE')
                else:
                    try: tx.execute(FENCE + ' NOWAIT' if nowait else FENCE).fetchone()
                    except Exception as error:
                        code = getattr(error, 'args', (None,))[:1]
                        if nowait and code == (LOCK_NOWAIT,):
                            raise FenceBusy('The application write fence is held') from None
                        if code == (LOCK_WAIT_TIMEOUT,): raise DatabaseBusy('Write fence lock wait timed out') from error
                        raise
                    granted = time.perf_counter()
                yield tx
                from .workspace_events import prepare_commit
                pending = prepare_commit(tx)
                with metrics.measure('database', 'COMMIT'): connection.commit()
            except BaseException:
                try:
                    with metrics.measure('database', 'ROLLBACK'): connection.rollback()
                except Exception: pass
                raise
            finally:
                if granted is not None: metrics.observe('database', 'LOCK_HOLD', (time.perf_counter()-granted)*1000)
                connection.close()
        finally:
            if gate is not None: gate.release()
        from .workspace_events import publish_committed
        publish_committed(self, pending)   # queues the wakeup; SQLite delivers it inline
    def append(self, kind, identity, owner, data):
        """Insert one new immutable row of an untracked kind without the write fence, committed on return.

        For uuid-keyed audit rows (suggestion) that no fenced decision reads until the caller hands their id
        out: no outbox or workspace event, no read-modify-write, and a duplicate key fails as an INSERT does."""
        from .workspace_events import tracked
        if tracked(kind): raise ValueError(kind + ' is tracked; write it in transaction()')
        with self._append_lease() as tx:
            tx.execute('INSERT INTO reveal_records(kind,id,owner_id,version,payload,updated_at) VALUES (%s,%s,%s,1,%s,%s)',
                       (kind, identity, owner, canonical(data), now()))
    @contextmanager
    def _append_lease(self):
        connection = self.connect()
        try:
            yield Transaction(connection, bool(self.sqlite_path), self.table_prefix)
            with metrics.measure('database', 'COMMIT'): connection.commit()
        except BaseException:
            try:
                with metrics.measure('database', 'ROLLBACK'): connection.rollback()
            except Exception: pass
            raise
        finally: connection.close()
    def discard_notifications(self, ids):
        """Delete published notification_outbox rows without the write fence. They are inserted once under the
        fence and never read-modify-written, and a delete that is lost or repeated only repeats a wakeup. READ
        COMMITTED keeps an id that reconciliation already deleted from taking a gap lock that would stall a
        fenced INSERT; the SET costs the lease its reset on release."""
        ids = list(dict.fromkeys(ids))
        if not ids: return 0
        connection = self.connect()
        try:
            tx = Transaction(connection, bool(self.sqlite_path), self.table_prefix)
            if not self.sqlite_path: tx.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            deleted = 0
            for offset in range(0, len(ids), 500):
                part = ids[offset:offset + 500]
                deleted += tx.execute('DELETE FROM reveal_records WHERE kind=%s AND id IN (' + ','.join(['%s'] * len(part)) + ')',
                                      ('notification_outbox', *part)).rowcount
            with metrics.measure('database', 'COMMIT'): connection.commit()
            return deleted
        except BaseException:
            try:
                with metrics.measure('database', 'ROLLBACK'): connection.rollback()
            except Exception: pass
            raise
        finally: connection.close()
    def migrate(self):
        connection = self.connect() if self.sqlite_path else mysql_connection()
        try:
            if self.sqlite_path:
                connection.execute(application_sql('CREATE TABLE IF NOT EXISTS reveal_records(kind TEXT NOT NULL,id TEXT NOT NULL,owner_id TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(kind,id))', self.table_prefix))
            else:
                sql = '\n'.join(line for line in (ROOT / 'schema/migrations/005_application.sql').read_text().splitlines() if not line.startswith('--'))
                with connection.cursor() as cursor:
                    for statement in sql.split(';'):
                        if statement.strip(): cursor.execute(application_sql(statement, self.table_prefix))
            connection.commit()
        finally: connection.close()
    def readiness(self):
        with self.single_read() as tx:
            tx.execute('SELECT 1 FROM reveal_records LIMIT 1').fetchone()
            verified = getattr(tx.connection, 'reveal_verified_tls', False)
        if self.sqlite_path: return {'database': 'sqlite-test', 'tls': False}
        if verified: return {'database': 'aurora-mysql', 'tls': True}
        with self.read_transaction() as tx:  # only sessions not opened by mysql_database.connect
            row = tx.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'").fetchone()
        return {'database': 'aurora-mysql', 'tls': bool(row and row[1])}

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Explicit one-time additive application migration')
    parser.add_argument('--apply', action='store_true', required=True)
    parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    Repository().migrate()
    print('Application migration 005 applied; imported scientific tables unchanged.')

if __name__ == '__main__': main()
