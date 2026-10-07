"""Aurora round trips per hot route stay within budget (SQLite + pinned protocol constants).

BUDGET holds the current counts, so a regression fails here. When a change removes round trips, run
`pytest -s tests/test_round_trip_budget.py` (each test prints its Budget) and lower the matching entry to the
new count in the same commit. Never raise an entry or relax a lease-kind assertion without review: a read
that becomes a write costs the same trips but holds the global write fence.
"""
import os
import threading
import unittest
from unittest.mock import patch

from round_trips import END, OPEN, RELEASE, count_round_trips
from reveal_backend import redis_notifications, workspace_events
from reveal_backend.mysql_database import application_session_unchanged, reset_application_session
from reveal_backend.mysql_pool import Pool
from reveal_backend.repository import Repository
from reveal_backend.research_work import ResearchWorkService, deadline
from reveal_backend.workflow_routes import sweep
import test_application as application
import test_durable_workflow as durable
import test_mysql_pool as wire
import test_research_http as research_http
import test_research_polling as research_polling

BUDGET = {'local_work_poll': 5, 'me': 2, 'readyz': 2, 'draft_patch': 10, 'mcp_get_operation': 5,
          'mcp_query_enqueue': 13, 'query_operation': 17, 'workspace_list': 4, 'workspace_detail': 4,
          'reconcile_idle': 4}


class LocalWorkPollBudget(unittest.TestCase):
    setUp = research_http.ResearchHTTPTests.setUp
    make_app = research_http.ResearchHTTPTests.make_app
    headers = research_http.ResearchHTTPTests.headers
    create = research_http.ResearchHTTPTests.create
    grant = research_http.ResearchHTTPTests.grant

    def poll(self, work, scheduled=None):
        with patch.object(research_http.InlinePreparation, 'kick', side_effect=AssertionError('poll re-read its work')), \
             patch.object(research_http.InlinePreparation, 'resume_operation', scheduled or (lambda *a, **k: None)), \
             count_round_trips() as budget:
            response = self.client.get('/v1/local-work/' + work['id'], headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return budget

    def test_poll_is_one_read_lease_within_budget_and_constant_in_children(self):
        work = self.create()
        empty = self.poll(work); print('\npoll', empty)
        self.assertEqual(empty.kinds(), ['read'], empty)   # never the global write fence, never a second lease
        self.assertEqual((empty.unleased, empty.connects), (0, 0), empty)
        self.assertLessEqual(empty.trips(), BUDGET['local_work_poll'], empty)
        service = ResearchWorkService(self.repo)
        grants = [self.grant(work) for _ in range(3)]
        with self.repo.transaction() as tx:
            for index in range(3):
                service.enqueue(tx, self.owner, work, 'validate', {'operation_id': 'v%d' % index}, grants[0]['grant_id'])
        busy = self.poll(work)
        self.assertEqual(busy.leases, empty.leases, busy)  # no per-submission or per-grant queries
        with self.repo.transaction() as tx:
            for index in range(3):
                service.enqueue(tx, self.owner, work, 'submit', {'receipt_ids': ['r%d' % index, 'shared'],
                    'import_ids': ['i%d' % index]}, grants[0]['grant_id'])
        cited = self.poll(work)   # every cited evidence row arrives in one read, however many submissions cite it
        self.assertEqual(cited.leases, [['read', empty.leases[0][1] + 1]], cited)

    def test_poll_resumes_pending_operations_from_its_own_snapshot(self):
        work = self.create(); service = ResearchWorkService(self.repo); scheduled = []
        with self.repo.transaction() as tx:
            received = service.enqueue(tx, self.owner, work, 'query', {}, None)
            expired, live, done = (service.enqueue(tx, self.owner, work, kind, {}, None) for kind in ('export', 'import', 'validate'))
            for operation, state, lease in ((expired, 'running', deadline(-60)), (live, 'running', deadline(600)), (done, 'succeeded', None)):
                operation.update(state=state, lease_until=lease, lease_token=operation['id'] + '-lease')
                tx.put('research_operation', operation['id'], self.owner, operation)
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('poll took the write fence')):
            budget = self.poll(work, lambda runner, identity, lease_token=None: scheduled.append((identity, lease_token)))
        self.assertEqual(budget.kinds(), ['read'], budget)
        self.assertEqual(sorted(scheduled), sorted([(received['id'], None), (expired['id'], expired['id'] + '-lease')]))


class McpBudget(unittest.TestCase):
    setUp = research_polling.ResearchPollingTests.setUp
    make_app = research_http.ResearchHTTPTests.make_app
    headers = research_http.ResearchHTTPTests.headers
    create = research_http.ResearchHTTPTests.create
    grant = research_http.ResearchHTTPTests.grant
    rpc = research_http.ResearchHTTPTests.rpc
    tool = research_http.ResearchHTTPTests.tool

    def test_query_tool_call_takes_the_fence_once_without_reading_operation_payloads(self):
        args = {'research_request_id': self.work['research_request_id'], 'arguments': {'factor_id': 'fixture'},
                'idempotency_key': 'budget'}
        with patch.object(research_http.InlinePreparation, 'resume_operation', lambda *a, **k: None), \
                count_round_trips() as budget:
            result = self.tool(self.grant_value, 'get_factor', args)
        self.assertFalse(result.get('isError'), result); print('\nmcp query enqueue', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['read', 'write'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['mcp_query_enqueue'], budget)

    def test_query_operation_commits_its_records_in_one_fence(self):
        identity = research_http.ResearchHTTPTests.queued(self, self.grant_value, self.work, 'budget')
        service = ResearchWorkService(self.repo, data_service=research_http.ResearchHTTPTests.Pages())
        with count_round_trips() as budget: service.run_operation(identity)   # two captured files
        print('\nquery operation', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['write', 'read', 'write'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['query_operation'], budget)

    def test_read_tool_call_is_one_read_lease(self):
        with patch.object(research_http.InlinePreparation, 'resume_operation', lambda *a, **k: None), \
                count_round_trips() as budget:
            result = self.tool(self.grant_value, 'get_operation', self.arguments)
        self.assertFalse(result.get('isError'), result); print('\nmcp get_operation', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['read'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['mcp_get_operation'], budget)


class AccountBudget(unittest.TestCase):
    setUp = application.ApplicationTests.setUp
    provision = application.ApplicationTests.provision
    token = application.ApplicationTests.token

    def test_me_within_budget(self):
        user = self.provision()
        with count_round_trips() as budget:
            response = self.client.get('/v1/me', headers={'Authorization': 'Bearer ' + self.token(user)})
        self.assertEqual(response.status_code, 200, response.text); print('\n/v1/me', budget)
        self.assertEqual(budget.kinds(), ['single'], budget)  # one principal row, never the global write fence
        self.assertEqual((budget.unleased, budget.connects, budget.locked_trips()), (0, 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['me'], budget)

    def test_workspace_reads_are_one_snapshot_off_the_fence(self):
        user = self.provision(); headers = {'Authorization': 'Bearer ' + self.token(user)}
        draft = self.client.post('/v1/drafts', json={'composer': application.COMPOSER},
                                 headers={**headers, 'Idempotency-Key': 'budget'}).json()
        routes = (('/v1/drafts', 'workspace_list'), ('/v1/jobs', 'workspace_list'), ('/v1/research-requests', 'workspace_list'),
                  ('/v1/me/explorations', 'workspace_list'), ('/v1/drafts/' + draft['id'], 'workspace_detail'))
        for route, name in routes:
            with self.subTest(route=route), count_round_trips() as budget:
                response = self.client.get(route, headers=headers)
                self.assertEqual(response.status_code, 200, response.text); print('\n' + route, budget)
                self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['read'], 0, 0), budget)
                self.assertLessEqual(budget.trips(), BUDGET[name], budget)

    def test_readyz_database_check_is_one_single_read(self):
        class Sources:
            def readiness(self): return {'reference': 'stub'}
        with patch('reveal_backend.app.catalog', Sources()), count_round_trips() as budget:
            response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 200, response.text); print('\n/readyz', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['single'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['readyz'], budget)


class TrackedWriteBudget(unittest.TestCase):
    setUp = application.ApplicationTests.setUp
    provision = application.ApplicationTests.provision
    token = application.ApplicationTests.token
    headers = application.ApplicationTests.headers
    draft = application.ApplicationTests.draft

    def test_tracked_write_is_one_fenced_lease_and_publishes_after_the_response(self):
        user = self.provision(); draft = self.draft(user)
        release, published = threading.Event(), []
        def publish(channels): release.wait(5); published.append(channels)
        with patch.dict(os.environ, {'REVEAL_NOTIFICATION_DELIVERY': 'background'}), \
                patch.object(redis_notifications, 'publish', side_effect=publish):
            with count_round_trips() as budget:
                response = self.client.patch('/v1/drafts/' + draft['id'], headers=self.headers(user),
                                             json={'expected_version': draft['version'], 'name': 'Renamed'})
            self.assertEqual((response.status_code, published), (200, []), response.text); print('\ndraft patch', budget)
            release.set(); self.assertTrue(workspace_events.publisher.flush(5))
        self.assertEqual(budget.kinds(), ['write'], budget)   # no second fenced transaction deletes the outbox row
        self.assertEqual((budget.unleased, budget.connects), (0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['draft_patch'], budget)
        self.assertEqual(len(published), 1)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('notification_outbox'), [])


class WorkflowBudget(unittest.IsolatedAsyncioTestCase):
    setUp = durable.WorkflowTests.setUp
    new = durable.WorkflowTests.new

    async def test_idle_reconciliation_is_one_snapshot_off_the_fence(self):
        self.new()   # a job awaiting delivery is the dispatcher's, not a recovery candidate
        with count_round_trips() as budget: self.assertEqual(sweep(self.repo), (0, False))
        print('\nreconcile sweep', budget)
        self.assertEqual((budget.leases, budget.unleased, budget.connects, budget.locked_trips()), ([['read', 2]], 0, 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['reconcile_idle'], budget)


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
