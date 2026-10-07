"""Aurora round trips per hot route stay within budget (SQLite + pinned protocol constants).

BUDGET holds the current counts, so a regression fails here. When a change removes round trips, run
`pytest -s tests/test_round_trip_budget.py` (each test prints its Budget) and lower the matching entry to the
new count in the same commit. Never raise an entry or relax a lease-kind assertion without review: a read
that becomes a write costs the same trips but holds the global write fence.
"""
import os
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from round_trips import END, OPEN, RELEASE, count_round_trips
from reveal_backend import redis_notifications, workspace_events
from reveal_backend.mysql_database import application_session_unchanged, reset_application_session
from reveal_backend.mysql_pool import Pool
from reveal_backend.repository import Repository
from reveal_backend.research_work import ResearchWorkService, deadline
from reveal_backend.workflow_routes import dispatch_job, sweep
import test_cfde_assessment as cfde
from test_cfde_assessment import case  # noqa: F401 (pytest fixture)
import test_account_discovery as account_discovery
import test_acceptance_batch as acceptance_batch
import test_application as application
import test_durable_workflow as durable
import test_event_loop_offload as offload
import test_mysql_pool as wire
import test_publication as publication
import test_research_http as research_http
import test_readiness_monitor as readiness_monitor
import test_research_polling as research_polling

BUDGET = {'local_work_poll': 5, 'me': 2, 'readyz': 2, 'draft_patch': 8, 'mcp_get_operation': 5, 'job_create': 8, 'draft_create': 7, 'account_acceptance': 12,
          'mcp_query_enqueue': 13, 'query_operation': 17, 'workspace_list': 4, 'workspace_detail': 4,
          'reconcile_idle': 4, 'job_dispatch': 11, 'observe_tick': 11, 'observe_tick_events': 13, 'gap_list': 5, 'gap_search': 5, 'gap_detail': 5,
          'citation_render': 5, 'readyz_monitored': 0, 'readiness_tick': 2, 'suggest': 2,
          'cfde_start': 8, 'cfde_start_locked': 4, 'cfde_reuse_locked': 3, 'cfde_poll': 3, 'cfde_worker': 17}


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


class SuggestionBudget(unittest.TestCase):
    setUp = application.ApplicationTests.setUp

    def test_suggestion_audit_row_is_one_unfenced_append_committed_before_the_response(self):
        import json
        from pathlib import Path
        from reveal_backend import app as api
        root = Path(__file__).resolve().parents[3]
        body = json.loads((root / 'api/examples/suggestMechanisms.dismech_context.json').read_text())['request']['body']
        gap = next(iter(json.loads((root / 'api/examples/getKnowledgeGap.request.json').read_text())['responses']['200']['examples'].values()))
        class Source:
            mechanisms = {}; embedding_run = 'embedding'; mapping_run = 'mapping'
            def selected(self, reference): return gap
            def suggest_factors(self, *args, **kwargs): return []
            def context_embedding_provenance(self, *args): return {'dismech_embedding_run_id': 'context-run'}
            def provenance(self, *args): return {}
        with patch.object(api, 'catalog', Source()), count_round_trips() as budget, \
                patch.object(self.repo, 'transaction', side_effect=AssertionError('suggest took the write fence')):
            response = self.client.post('/v1/mechanisms/suggest', json=body)
        self.assertEqual(response.status_code, 200, response.text); print('\nsuggest', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects, budget.locked_trips()), (['append'], 0, 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['suggest'], budget)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('suggestion', response.json()['suggestion_id'])['owner'], 'catalog')


class ReadinessBudget(unittest.TestCase):
    setUp = readiness_monitor.ReadinessMonitorTests.setUp
    activate = readiness_monitor.ReadinessMonitorTests.activate
    tick = readiness_monitor.ReadinessMonitorTests.tick

    def test_monitored_probe_reads_nothing_and_each_tick_is_one_single_read(self):
        with count_round_trips() as budget: self.tick()   # every 5 s: database, reference_active and vector_active
        print('\nreadiness tick', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['single'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['readiness_tick'], budget)
        with count_round_trips() as budget: response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 200, response.text); print('\n/readyz monitored', budget)
        self.assertLessEqual(budget.trips(), BUDGET['readyz_monitored'], budget)


class GapDiscoveryBudget(unittest.TestCase):
    setUp = account_discovery.AccountDiscoveryTests.setUp
    principal = account_discovery.AccountDiscoveryTests.principal
    headers = account_discovery.AccountDiscoveryTests.headers

    def test_gap_discovery_is_one_snapshot_whatever_the_catalog_size(self):
        from copy import deepcopy
        template = self.gaps[0]
        for index in range(600):   # 1,200 vote keys for a registered viewer: five exact-key batches before
            gap = deepcopy(template); gap['object']['id'] = 'dapper:KnowledgeGap.%032x' % index
            gap['source']['source_id'] = 'dismech:%03d' % index; self.catalog.gaps[gap['object']['id']] = gap
        query = template['object']['text'].split()[0]
        routes = (('/v1/knowledge-gaps?limit=20', 'gap_list'), ('/v1/knowledge-gaps?limit=20&sort=votes', 'gap_list'),
                  ('/v1/knowledge-gaps/search?q=' + query, 'gap_search'), ('/v1/knowledge-gaps/' + template['object']['id'], 'gap_detail'))
        for route, name in routes:
            for headers in (self.headers(self.owner), {}):
                with self.subTest(route=route, signed=bool(headers)), count_round_trips() as budget:
                    response = self.client.get(route, headers=headers)
                    self.assertEqual(response.status_code, 200, response.text); print('\n' + route, budget)
                    self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['read'], 0, 0), budget)
                    self.assertLessEqual(budget.trips(), BUDGET[name] - (0 if headers else 1), budget)   # no principal read


class CitationRenderBudget(unittest.TestCase):
    setUp = publication.PublicationTests.setUp
    seed = publication.PublicationTests.seed
    principal = publication.PublicationTests.principal
    headers = publication.PublicationTests.headers
    request = publication.PublicationTests.request
    publish = publication.PublicationTests.publish
    loop_guard = offload.RuntimeOffloadTests.loop_guard

    def render(self, owner=None):
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('rendering took the write fence')), \
                self.loop_guard() as on_loop, count_round_trips() as budget:
            response = self.request('post', '/v1/citations/render', owner, json={'paragraph_id': self.paragraph_id, 'style': 'mla', 'locale': 'en-US'})
        self.assertEqual(response.status_code, 200, response.text); print('\ncitation render', budget)
        self.assertEqual(on_loop, [])   # the read lease and the CSL engine never run on the event loop
        return response.json(), budget

    def test_render_is_one_read_snapshot_whatever_the_citation_count(self):
        owned, budget = self.render(self.owner)
        cited = {item['target_id'] for item in owned['bibliography']}
        self.assertGreater(len(cited), 1)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects, budget.locked_trips()), (['read'], 0, 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['citation_render'], budget)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('citation_rendering'), [])
        self.assertEqual(self.publish().status_code, 200)
        public, budget = self.render()   # the published snapshot, for a reader without a session
        self.assertEqual(budget.kinds(), ['read'], budget)
        self.assertEqual(public['bibliography'], owned['bibliography'])


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


class SubmissionBudget(unittest.TestCase):
    setUp = application.ApplicationTests.setUp
    provision = application.ApplicationTests.provision
    token = application.ApplicationTests.token
    headers = application.ApplicationTests.headers
    submission = application.ApplicationTests.submission

    def test_job_submission_is_one_fence_with_one_read_and_one_insert(self):
        # The principal, idempotency, reload gate, draft and binding in one read; every new row in one INSERT.
        user, draft = self.submission()
        with patch.dict(os.environ, {'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'test'}), \
                patch('reveal_backend.app.deliver_after_response'), patch.object(workspace_events, 'publish_committed'), \
                count_round_trips() as budget:   # delivery runs after the response (TrackedWriteBudget)
            response = self.client.post('/v1/jobs', json={'kind': 'analysis', 'draft_id': draft['id'], 'draft_version': draft['version']},
                                        headers=self.headers(user))
        self.assertEqual(response.status_code, 202, response.text); print('\njob create', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['write'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['job_create'], budget)

    def test_draft_creation_inserts_the_draft_binding_and_retry_key_together(self):
        user, draft = self.submission()
        with patch.object(workspace_events, 'publish_committed'), count_round_trips() as budget:
            response = self.client.post('/v1/drafts', json={'composer': draft['composer']}, headers=self.headers(user))
        self.assertEqual(response.status_code, 201, response.text); print('\ndraft create', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['write'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['draft_create'], budget)


class AcceptanceBudget(unittest.TestCase):
    setUp = acceptance_batch.AcceptanceBatchTests.setUp
    claimed = acceptance_batch.AcceptanceBatchTests.claimed
    accept = acceptance_batch.AcceptanceBatchTests.accept

    def test_account_acceptance_is_one_fence_with_a_few_batched_statements(self):
        # The 63-node fixture account: one read of every row it can touch, multi-row INSERTs, the result event.
        claimed = self.claimed(self.repo, acceptance_batch.OWNER)
        with count_round_trips() as budget:
            self.accept(self.repo, acceptance_batch.W.Worker, acceptance_batch.OWNER, [self.document], claimed=claimed)
        print('\naccount acceptance', budget)   # the read is the helper's own status check afterwards
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['write', 'read'], 0, 0), budget)
        self.assertLessEqual(budget.leases[0][1] + OPEN['write'] + END + RELEASE, BUDGET['account_acceptance'], budget)


class WorkflowBudget(unittest.IsolatedAsyncioTestCase):
    setUp = durable.WorkflowTests.setUp
    new = durable.WorkflowTests.new
    observing = durable.WorkflowTests.observing

    async def test_observe_tick_is_one_batched_acquire_and_one_commit(self):
        # Every ~5 s per running job: acquire (one read, two updates), then the completion with the observation.
        job, payload, handle, adapter, engine = self.observing()
        with patch.object(workspace_events, 'publish_committed'), count_round_trips() as budget:
            self.assertEqual((await engine.step(payload, 0))['phase'], 'observe')
        print('\nobserve tick', budget)
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['write', 'write'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['observe_tick'], budget)
        events = [('tool_call', {'remote_stream_id': 's', 'remote_sequence': n, 'tool_name': 't', 'call_id': str(n)}) for n in range(3)]
        adapter.inspect_once.return_value = ({**handle, 'cursor': 3}, events, False)
        with patch.object(workspace_events, 'publish_committed'), count_round_trips() as budget:
            await engine.step(payload, 1)
        print('\nobserve tick with events', budget)
        self.assertEqual(budget.kinds(), ['write', 'write'], budget)
        self.assertLessEqual(budget.trips(), BUDGET['observe_tick_events'], budget)

    async def test_idle_reconciliation_is_one_snapshot_off_the_fence(self):
        self.new()   # a job awaiting delivery is the dispatcher's, not a recovery candidate
        with count_round_trips() as budget: self.assertEqual(sweep(self.repo), (0, False))
        print('\nreconcile sweep', budget)
        self.assertEqual((budget.leases, budget.unleased, budget.connects, budget.locked_trips()), ([['read', 2]], 0, 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['reconcile_idle'], budget)

    async def test_job_dispatch_reads_once_and_fences_once(self):
        job, _ = self.new(); fake = Mock(); fake.http.request = AsyncMock(return_value=[{'messageId': 'run'}])
        with patch('reveal_backend.workflow_routes.client', return_value=fake), count_round_trips() as budget:
            self.assertEqual(await dispatch_job(self.repo, job['id'], control=False), {'delivered': 1, 'failed': 0})
        print('\njob dispatch', budget)
        self.assertEqual((budget.kinds(), budget.leases[0], budget.unleased, budget.connects), (['read', 'write'], ['read', 1], 0, 0), budget)
        self.assertLessEqual(budget.trips(), BUDGET['job_dispatch'], budget)


def test_cfde_assessment_admission_is_one_snapshot_then_one_short_fence(case):
    # Auto-fired 1.5 s after each settled edit: the snapshot decides and rejects, the fence re-decides and writes.
    with count_round_trips() as budget: first = cfde.start(case).json()   # a private miss starts an attempt
    print('\ncfde start', budget)
    assert (budget.leases, budget.unleased, budget.connects) == ([['read', 1], ['write', 3]], 0, 0), budget
    assert budget.trips() <= BUDGET['cfde_start'] and budget.locked_trips() <= BUDGET['cfde_start_locked'], budget
    with count_round_trips() as budget: again = cfde.start(case, headers=case.headers(case.owner)).json()
    print('\ncfde reuse', budget)   # unchanged inputs, new key: the pending receipt, one INSERT under the fence
    assert again['id'] == first['id'] and budget.kinds() == ['read', 'write'], budget
    assert budget.trips() <= BUDGET['cfde_start'] and budget.locked_trips() <= BUDGET['cfde_reuse_locked'], budget
    with count_round_trips() as budget: response = case.client.get(case.route + '/' + first['id'], headers=case.headers(case.owner))
    print('\ncfde poll', budget)
    assert response.status_code == 200 and (budget.leases, budget.unleased) == ([['read', 1]], 0), budget
    assert budget.trips() <= BUDGET['cfde_poll'], budget
    with count_round_trips() as budget: case.queue.run()
    print('\ncfde worker', budget)   # one read, then preparing, assessing and succeeded: each one batch plus its writes
    assert budget.kinds() == ['read', 'write', 'write', 'write'] and budget.trips() <= BUDGET['cfde_worker'], budget


def test_cfde_shared_receipts_poll_with_one_more_read(case):
    case.body['composer'].pop('context'); first = cfde.start(case).json()
    with count_round_trips() as budget: case.client.get(case.route + '/' + first['id'], headers=case.headers(case.owner))
    assert (budget.leases, budget.unleased) == ([['read', 2]], 0) and budget.trips() <= BUDGET['cfde_poll'] + 1, budget


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
        for method, kind in (('read_transaction', 'read'), ('transaction', 'write'), ('single_read', 'single'), ('_append_lease', 'append')):
            with self.subTest(method=method):
                self.assertEqual(self.lease_trips(method), 1 + OPEN[kind] + END + RELEASE)


if __name__ == '__main__': unittest.main()
