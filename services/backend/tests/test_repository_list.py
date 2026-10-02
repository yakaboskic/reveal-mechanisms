"""Record listing preserves ordering without asking MySQL to sort JSON payloads."""
import json
import sqlite3
import unittest

from reveal_backend.repository import Transaction


class RepositoryListTests(unittest.TestCase):
    def test_order_owner_prefix_and_row_shape_match_existing_sql_semantics(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        prefix = 'reveal_reload_rehearsal'
        for table in ('reveal_records', prefix + '_records'):
            connection.execute(f'CREATE TABLE {table} (kind TEXT, id TEXT, owner_id TEXT, '
                               'version INTEGER, payload TEXT, updated_at TEXT)')
        rows = [
            ('account', 'z', 'owner-a', 4, {'label': 'old'}, '2026-10-01T01:00:00Z'),
            ('account', 'b', 'owner-a', 2, {'label': 'tied b'}, '2026-10-02T01:00:00Z'),
            ('account', 'A', 'owner-a', 7, {'label': 'tied A'}, '2026-10-02T01:00:00Z'),
            ('account', 'a', 'owner-a', 3, {'label': 'tied a'}, '2026-10-02T01:00:00Z'),
            ('account', 'new', 'owner-b', 1, {'label': 'other owner'}, '2026-10-03T01:00:00Z'),
            ('account', 'empty-owner', '', 5, {'label': 'empty owner'}, '2026-10-04T01:00:00Z'),
            ('draft', 'draft', 'owner-a', 1, {'label': 'other kind'}, '2026-10-05T01:00:00Z'),
        ]
        connection.executemany(f'INSERT INTO {prefix}_records VALUES (?,?,?,?,?,?)',
                               [(kind, identity, owner, version, json.dumps(data), updated)
                                for kind, identity, owner, version, data, updated in rows])
        connection.execute('INSERT INTO reveal_records VALUES (?,?,?,?,?,?)',
                           ('account', 'wrong-prefix', 'owner-a', 99, '{}', '2027-01-01T00:00:00Z'))
        tx = Transaction(connection, sqlite=True, table_prefix=prefix)
        for owner in (None, 'owner-a', 'owner-b', '', 'missing'):
            with self.subTest(owner=owner):
                sql = f'SELECT id,owner_id,version,payload FROM {prefix}_records WHERE kind=?'
                args = ['account']
                if owner is not None:
                    sql += ' AND owner_id=?'
                    args.append(owner)
                expected = [{'id': identity, 'owner': row_owner, 'version': version, 'data': json.loads(payload)}
                            for identity, row_owner, version, payload in
                            connection.execute(sql + ' ORDER BY updated_at DESC,id', args).fetchall()]
                self.assertEqual(tx.list('account', owner), expected)
        self.assertEqual([row['id'] for row in tx.list('account', 'owner-a')], ['A', 'a', 'b', 'z'])
        self.assertEqual(tx.list('missing'), [])

    def test_mysql_fetches_large_payloads_without_database_sort(self):
        payload = json.dumps({'document': 'x' * (2 * 1024 * 1024), 'nested': {'kept': True}})
        # PyMySQL fetchall returns tuples; server row order is deliberately shuffled.
        rows = (
            ('b', 'owner-a', 2, payload, '2026-10-02T00:00:00Z'),
            ('old', 'owner-a', 1, '{"old":true}', '2026-10-01T00:00:00Z'),
            ('a', 'owner-a', 9, '{"new":true}', '2026-10-02T00:00:00Z'),
        )
        calls = []

        class Cursor:
            def execute(self, sql, params):
                if 'ORDER BY' in sql.upper():
                    raise AssertionError('Large JSON rows must not pass through MySQL filesort')
                calls.append((sql, list(params)))

            def fetchall(self):
                return rows

        class Connection:
            def cursor(self):
                return Cursor()

        result = Transaction(Connection(), table_prefix='reveal_reload_rehearsal').list('account', 'owner-a')
        self.assertEqual(calls, [('SELECT id,owner_id,version,payload,updated_at FROM '
                                 'reveal_reload_rehearsal_records WHERE kind=%s AND owner_id=%s',
                                 ['account', 'owner-a'])])
        self.assertEqual([row['id'] for row in result], ['a', 'b', 'old'])
        self.assertEqual(result[1], {'id': 'b', 'owner': 'owner-a', 'version': 2, 'data': json.loads(payload)})
        self.assertTrue(all(set(row) == {'id', 'owner', 'version', 'data'} for row in result))


if __name__ == '__main__':
    unittest.main()
