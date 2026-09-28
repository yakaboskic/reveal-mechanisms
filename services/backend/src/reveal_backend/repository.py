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
import sqlite3
from uuid import uuid4
from .runtime_config import ROOT, mysql_connection

def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
def uid(): return str(uuid4())
def canonical(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()

class Conflict(Exception): pass

class Transaction:
    def __init__(self, connection, sqlite=False):
        self.connection, self.sqlite = connection, sqlite
    def execute(self, sql, params=()):
        cursor = self.connection.cursor()
        cursor.execute(sql.replace('%s', '?') if self.sqlite else sql, params)
        return cursor
    def get(self, kind, identity):
        row = self.execute('SELECT owner_id,version,payload FROM reveal_records WHERE kind=%s AND id=%s', (kind, identity)).fetchone()
        if row is None: return None
        return {'owner': row[0], 'version': row[1], 'data': json.loads(row[2])}
    def get_many(self, kind, identities):
        if not identities: return {}
        rows = self.execute('SELECT id,owner_id,version,payload FROM reveal_records WHERE kind=%s AND id IN ('+
            ','.join(['%s'] * len(identities))+')', (kind, *identities)).fetchall()
        return {row[0]: {'owner': row[1], 'version': row[2], 'data': json.loads(row[3])} for row in rows}
    def insert_many(self, records):
        """Insert new records in one SQL statement; conflicts roll back the batch."""
        if not records: return
        values = []
        for kind, identity, owner, data in records:
            values.extend((kind, identity, owner, 1, canonical(data), now()))
        self.execute('INSERT INTO reveal_records(kind,id,owner_id,version,payload,updated_at) VALUES '+
            ','.join(['(%s,%s,%s,%s,%s,%s)'] * len(records)), tuple(values))
    def update_existing(self, kind, identity, owner, data):
        """Update a row already read under the transaction's exclusive fence."""
        cursor = self.execute('UPDATE reveal_records SET owner_id=%s,version=version+1,payload=%s,updated_at=%s WHERE kind=%s AND id=%s',
            (owner, canonical(data), now(), kind, identity))
        if cursor.rowcount != 1: raise Conflict('Expected existing record')
    def list(self, kind, owner=None):
        sql, args = 'SELECT id,owner_id,version,payload FROM reveal_records WHERE kind=%s', [kind]
        if owner is not None: sql += ' AND owner_id=%s'; args.append(owner)
        return [{'id': r[0], 'owner': r[1], 'version': r[2], 'data': json.loads(r[3])}
                for r in self.execute(sql + ' ORDER BY updated_at DESC,id', args).fetchall()]
    def put(self, kind, identity, owner, data, expected=None):
        old = self.get(kind, identity)
        if expected is not None and (old is None or old['version'] != expected): raise Conflict('Version conflict')
        version = old['version'] + 1 if old else 1
        if old:
            self.execute('UPDATE reveal_records SET owner_id=%s,version=%s,payload=%s,updated_at=%s WHERE kind=%s AND id=%s',
                         (owner, version, canonical(data), now(), kind, identity))
        else:
            self.execute('INSERT INTO reveal_records(kind,id,owner_id,version,payload,updated_at) VALUES (%s,%s,%s,%s,%s,%s)',
                         (kind, identity, owner, version, canonical(data), now()))
        return version
    def remove(self, kind, identity):
        self.execute('DELETE FROM reveal_records WHERE kind=%s AND id=%s', (kind, identity))
    def transfer(self, source, target):
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
        self.execute('UPDATE reveal_records SET owner_id=%s WHERE owner_id=%s AND kind NOT IN (%s,%s)', (target, source, 'principal', 'identity'))

class Repository:
    def __init__(self, sqlite_path=None): self.sqlite_path = sqlite_path
    def connect(self):
        if self.sqlite_path:
            connection = sqlite3.connect(self.sqlite_path, timeout=30)
            return connection
        return mysql_connection()
    @contextmanager
    def read_transaction(self):
        """Consistent authorized reads without taking the application write mutex."""
        connection = self.connect()
        try:
            tx = Transaction(connection, bool(self.sqlite_path))
            if self.sqlite_path:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
            else:
                tx.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                tx.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            yield tx
            connection.commit()
        except BaseException:
            connection.rollback(); raise
        finally: connection.close()
    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            tx = Transaction(connection, bool(self.sqlite_path))
            if self.sqlite_path: connection.execute('BEGIN IMMEDIATE')
            else: tx.execute('SELECT revision FROM reveal_transaction_lock WHERE id=1 FOR UPDATE').fetchone()
            yield tx
            connection.commit()
        except BaseException:
            connection.rollback(); raise
        finally: connection.close()
    def migrate(self):
        connection = self.connect()
        try:
            if self.sqlite_path:
                connection.execute('CREATE TABLE IF NOT EXISTS reveal_records(kind TEXT NOT NULL,id TEXT NOT NULL,owner_id TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(kind,id))')
            else:
                sql = '\n'.join(line for line in (ROOT / 'schema/migrations/005_application.sql').read_text().splitlines() if not line.startswith('--'))
                with connection.cursor() as cursor:
                    for statement in sql.split(';'):
                        if statement.strip(): cursor.execute(statement)
            connection.commit()
        finally: connection.close()
    def readiness(self):
        with self.read_transaction() as tx:
            tx.execute('SELECT COUNT(*) FROM reveal_records').fetchone()
            if self.sqlite_path: return {'database': 'sqlite-test', 'tls': False}
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
