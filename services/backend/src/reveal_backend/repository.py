"""Small transactional application repository; Aurora is the production store.

One indexed row lock serializes short application transactions, including claims,
leases, idempotency, ownership transfers and acceptance/outbox. Network/agent work
never runs inside this lock. SQLite is available only to isolated unit tests.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import time
from uuid import uuid4
from .runtime_config import ROOT, mysql_connection, application_mysql_connection

def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
def uid(): return str(uuid4())
def canonical(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()

class Conflict(Exception): pass

SELECT_ROW = 'SELECT owner_id,version,payload FROM reveal_records WHERE kind=%s AND id=%s'
_READ = re.compile(r'\s*\(*\s*(SELECT|SHOW)\b', re.I)
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
            from .runtime_metrics import observe
            observe('database', sql.split()[0].upper(), (time.perf_counter()-started)*1000, failed)
        return cursor
    def _known(self, kind, identity):
        return self._rows.get((kind, identity), _MISS) if _exact(identity) else _MISS
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
    def get_records(self, keys):
        """Fetch exact heterogeneous record keys without widening access."""
        keys = list(dict.fromkeys(keys)); result = {}
        for offset in range(0, len(keys), 250):
            batch = keys[offset:offset + 250]
            rows = self.execute('SELECT kind,id,owner_id,version,payload FROM reveal_records WHERE (kind,id) IN (' +
                ','.join(['(%s,%s)'] * len(batch)) + ')', tuple(value for key in batch for value in key)).fetchall()
            for row in rows:
                self._learn(row[0], row[1], row[2], row[3], row[4])
                result[(row[0], row[1])] = {'owner': row[2], 'version': row[3], 'data': json.loads(row[4])}
            for key in batch:
                if key not in result: self._learn(*key, absent=True)
        return result
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
        try: self.insert_many([(kind, identity, owner, data)])
        except Exception as error:
            import pymysql
            if not replace or not isinstance(error, (sqlite3.IntegrityError, pymysql.err.IntegrityError)): raise
            self.put(kind, identity, owner, data)
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
            connection.commit()
        except BaseException:
            try: connection.rollback()
            except Exception: pass  # Retain the original query/commit error.
            raise
        finally: connection.close()
    @contextmanager
    def transaction(self):
        connection = self.connect()
        pending = []
        try:
            tx = Transaction(connection, bool(self.sqlite_path), self.table_prefix)
            if self.sqlite_path: connection.execute('BEGIN IMMEDIATE')
            else: tx.execute('SELECT revision FROM reveal_transaction_lock WHERE id=1 FOR UPDATE').fetchone()
            yield tx
            from .workspace_events import prepare_commit
            pending = prepare_commit(tx)
            connection.commit()
        except BaseException:
            try: connection.rollback()
            except Exception: pass
            raise
        finally: connection.close()
        from .workspace_events import publish_committed
        publish_committed(self, pending)
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
        with self.read_transaction() as tx:
            tx.execute('SELECT 1 FROM reveal_records LIMIT 1').fetchone()
            if self.sqlite_path: return {'database': 'sqlite-test', 'tls': False}
            if getattr(tx.connection, 'reveal_verified_tls', False):
                return {'database': 'aurora-mysql', 'tls': True}
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
