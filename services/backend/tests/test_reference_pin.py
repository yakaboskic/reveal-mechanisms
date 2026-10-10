import sqlite3

import pytest

from reveal_backend import reference_pin as rp

RELEASE = 'a' * 64


class Cursor:
    def __init__(self, connection): self.connection, self.rows = connection, []
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, sql, parameters=()):
        self.connection.sql.append(sql)
        if self.connection.error: raise self.connection.error
        self.rows = list(self.connection.rows)
    def fetchall(self): return self.rows


class Connection:
    def __init__(self, rows=(), error=None): self.rows, self.error, self.sql = rows, error, []
    def cursor(self): return Cursor(self)


def test_the_published_release_is_read_from_the_environment_table():
    connection = Connection([(RELEASE, '2026-10-10 12:00:00', '{"release_id": "%s"}' % RELEASE)])
    release = rp.pin_release(connection, RELEASE, 'reveal_workflow_qa')
    assert release['manifest'] == {'release_id': RELEASE} and connection.sql == ['SELECT release_id,published_at,manifest FROM reveal_workflow_qa_ref_release']
    assert rp.is_release(connection, RELEASE, 'reveal_workflow_qa') and not rp.is_release(connection, 'b' * 64, 'reveal_workflow_qa')


def test_a_replaced_or_unpublished_release_is_refused():
    with pytest.raises(rp.ReleaseUnavailable) as raised: rp.pin_release(Connection([('c' * 64, None, '{}')]), RELEASE, 'reveal')
    assert (raised.value.pinned, raised.value.current) == (RELEASE, 'c' * 64)
    problem = rp.superseded_problem(raised.value)
    assert (problem.status, problem.code, problem.extra['reason']) == (409, 'SOURCE_UNAVAILABLE', rp.SUPERSEDED)
    missing = Exception(rp.NO_TABLE, "Table 'reveal_ref_release' doesn't exist")
    assert rp.current_release(Connection(error=missing), 'reveal') is None
    with pytest.raises(rp.ReleaseUnavailable): rp.pin_release(Connection(error=missing), RELEASE, 'reveal')
    with pytest.raises(sqlite3.OperationalError): rp.current_release(Connection(error=sqlite3.OperationalError('locked')), 'reveal')
