"""Aurora round trips per hot route stay within budget (SQLite + pinned protocol constants).

BUDGET holds the current counts, so a regression fails here. When a change removes round trips, run
`pytest -s tests/test_round_trip_budget.py` (each test prints its Budget) and lower the matching entry to the
new count in the same commit. Never raise an entry or relax a lease-kind assertion without review: a read
that becomes a write costs the same trips but holds the global write fence.
"""
import unittest
from unittest.mock import patch

from round_trips import END, OPEN, RELEASE, count_round_trips
from reveal_backend.mysql_database import application_session_unchanged, reset_application_session
from reveal_backend.mysql_pool import Pool
from reveal_backend.repository import Repository
from reveal_backend.research_work import ResearchWorkService
import test_application as application
import test_mysql_pool as wire
import test_research_http as research_http

BUDGET = {'local_work_poll': 11, 'me': 3, 'readyz': 2}


class LocalWorkPollBudget(unittest.TestCase):
    setUp = research_http.ResearchHTTPTests.setUp
    make_app = research_http.ResearchHTTPTests.make_app
    headers = research_http.ResearchHTTPTests.headers
    create = research_http.ResearchHTTPTests.create
    grant = research_http.ResearchHTTPTests.grant

    def poll(self, work):
        with patch.object(research_http.InlinePreparation, 'kick', ResearchWorkService.kick), \
             patch.object(research_http.InlinePreparation, 'resume_operation', lambda *a, **k: None), \
             count_round_trips() as budget:
            response = self.client.get('/v1/local-work/' + work['id'], headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return budget

    def test_poll_is_two_read_leases_within_budget_and_constant_in_children(self):
        work = self.create()
        empty = self.poll(work); print('\npoll', empty)
        self.assertEqual(empty.kinds(), ['read', 'read'], empty)   # never the global write fence
        self.assertEqual((empty.unleased, empty.connects), (0, 0), empty)
        self.assertLessEqual(empty.trips(), BUDGET['local_work_poll'], empty)
        service = ResearchWorkService(self.repo)
        grants = [self.grant(work) for _ in range(3)]
        with self.repo.transaction() as tx:
            for index in range(3):
                service.enqueue(tx, self.owner, work, 'validate', {'operation_id': 'v%d' % index}, grants[0]['grant_id'])
        busy = self.poll(work)
        self.assertEqual(busy.leases, empty.leases, busy)  # no per-submission or per-grant queries


class AccountBudget(unittest.TestCase):
    setUp = application.ApplicationTests.setUp
    provision = application.ApplicationTests.provision
    token = application.ApplicationTests.token

    def test_me_within_budget(self):
        user = self.provision()
        with count_round_trips() as budget:
            response = self.client.get('/v1/me', headers={'Authorization': 'Bearer ' + self.token(user)})
        self.assertEqual(response.status_code, 200, response.text); print('\n/v1/me', budget)
        self.assertEqual(budget.kinds(), ['write'], budget)  # known debt (F07/F28): flip to ['read'] or ['single']
        self.assertEqual((budget.unleased, budget.connects, budget.locked_trips()), (0, 0, 2), budget)
        self.assertLessEqual(budget.trips(), BUDGET['me'], budget)

    def test_readyz_database_check_is_one_single_read(self):
        class Sources:
            def readiness(self): return {'reference': 'stub'}
        with patch('reveal_backend.app.catalog', Sources()), count_round_trips() as budget:
            response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 200, response.text); print('\n/readyz', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['single'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['readyz'], budget)


class WireConstantsTests(unittest.TestCase):
    """OPEN/END/RELEASE match what the real Pool + Repository send on a warm, clean pooled session."""
    def lease_trips(self, method):
        created = []
        def factory():
            connection = wire.StatusConnection(); created.append(connection); return connection
        pool = Pool(factory, lambda c: reset_application_session(c, 'cyaka_expected'), unchanged=application_session_unchanged)
        self.addCleanup(pool.close); repo = Repository(); repo.connect = pool.acquire
        with pool.acquire() as warm: warm.commit()
        raw = created[0]; raw.trips = 0
        with getattr(repo, method)() as tx: tx.execute('SELECT 1')
        self.assertEqual((len(created), raw.reset_count), (1, 0))   # the warm session was reused, never reset
        return raw.trips

    def test_lease_overhead_constants_match_the_pool(self):
        for method, kind in (('read_transaction', 'read'), ('transaction', 'write'), ('single_read', 'single')):
            with self.subTest(method=method):
                self.assertEqual(self.lease_trips(method), 1 + OPEN[kind] + END + RELEASE)


if __name__ == '__main__': unittest.main()
