"""Protected operation polling avoids write locks and unrelated recovery work."""
from concurrent.futures import Future
from unittest import TestCase
from unittest.mock import Mock, patch

from reveal_backend.auth import Problem
from reveal_backend.research_work import ResearchWorkService, deadline
from reveal_backend import research_tools, research_work
import test_research_http as fixture


class ResearchPollingTests(TestCase):
    make_app = fixture.ResearchHTTPTests.make_app
    headers = fixture.ResearchHTTPTests.headers
    create = fixture.ResearchHTTPTests.create
    grant = fixture.ResearchHTTPTests.grant
    rpc = fixture.ResearchHTTPTests.rpc
    tool = fixture.ResearchHTTPTests.tool

    def setUp(self):
        fixture.ResearchHTTPTests.setUp(self)
        self.work = self.create()
        self.grant_value = self.grant(self.work)
        self.authorization = 'Bearer '+self.grant_value['token']
        self.service = ResearchWorkService(self.repo)
        with self.repo.transaction() as tx:
            self.operation = self.service.enqueue(tx, self.owner, self.work, 'query',
                {'operation_id': 'get_factor', 'arguments': {}}, self.grant_value['grant_id'])
        self.arguments = {'local_work_id': self.work['id'], 'operation_id': self.operation['id']}

    def poll(self, **options):
        return research_tools.dispatch(self.service, self.authorization, 'get_operation', self.arguments, **options)

    def test_poll_uses_one_read_transaction_and_never_acquires_write_mutex(self):
        scheduled = Mock()
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('Read acquired write lock')), \
                patch.object(self.repo, 'read_transaction', wraps=self.repo.read_transaction) as reads, \
                patch.object(research_tools, 'authenticate', wraps=research_tools.authenticate) as auth:
            result = self.poll(on_operation=scheduled)
        self.assertEqual(result['state'], 'received')
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(auth.call_count, 1)
        scheduled.assert_called_once_with(self.operation['id'])

    def test_poll_resumes_received_or_expired_leases_only(self):
        for state, lease, expected in [('received', None, True), ('running', deadline(-1), True),
                ('running', deadline(60), False), ('succeeded', None, False), ('failed', None, False)]:
            with self.subTest(state=state, lease=lease):
                with self.repo.transaction() as tx:
                    operation = tx.get('research_operation', self.operation['id'])['data']
                    operation.update(state=state, lease_until=lease)
                    tx.put('research_operation', operation['id'], self.owner, operation)
                scheduled = Mock()
                self.poll(on_operation=scheduled)
                self.assertEqual(scheduled.call_count, int(expected))

    def test_wrong_work_and_revoked_grant_never_schedule_recovery(self):
        scheduled = Mock()
        self.arguments['local_work_id'] = 'different-work'
        with self.assertRaises(Problem): self.poll(on_operation=scheduled)
        self.arguments['local_work_id'] = self.work['id']
        with self.repo.transaction() as tx:
            rows = tx.list('research_access', self.owner)
            row = next(row for row in rows if row['data']['grant_id'] == self.grant_value['grant_id'])
            row['data']['revoked_at'] = '2026-01-01T00:00:00Z'
            tx.put('research_access', row['id'], self.owner, row['data'])
        with self.assertRaises(Problem): self.poll(on_operation=scheduled)
        scheduled.assert_not_called()

    def test_transport_does_not_reauthenticate_or_scan_work_after_poll(self):
        with patch.object(fixture.InlinePreparation, 'kick', side_effect=AssertionError('Poll scanned work')), \
                patch.object(fixture.InlinePreparation, 'resume_operation') as scheduled, \
                patch.object(self.repo, 'read_transaction', wraps=self.repo.read_transaction) as reads, \
                patch('reveal_backend.research_http.authenticate', wraps=research_work.authenticate) as transport_auth, \
                patch.object(research_tools, 'authenticate', wraps=research_work.authenticate) as dispatch_auth:
            result = self.tool(self.grant_value, 'get_operation', self.arguments)
        self.assertFalse(result.get('isError'))
        # A read tool authenticates once, inside the snapshot that serves it.
        self.assertEqual((transport_auth.call_count, dispatch_auth.call_count, reads.call_count), (0, 1, 1))
        scheduled.assert_called_once_with(self.operation['id'])

    def test_read_tool_authentication_failure_is_the_transport_challenge(self):
        with self.repo.transaction() as tx:
            row = next(r for r in tx.list('research_access', self.owner) if r['data']['grant_id'] == self.grant_value['grant_id'])
            row['data']['expires_at'] = '2000-01-01T00:00:00Z'
            tx.put('research_access', row['id'], self.owner, row['data'])
        modern = {'io.modelcontextprotocol/protocolVersion': '2026-07-28', 'io.modelcontextprotocol/clientCapabilities': {}}
        def legacy(token): return self.rpc(token, 'tools/call', {'name': 'get_operation', 'arguments': self.arguments})
        def stateless(token):   # the SDK's modern per-request path builds its Request from the same scope
            return self.client.post('/mcp', headers={'Authorization': 'Bearer '+token, 'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': 'tools/call', 'Mcp-Name': 'get_operation'},
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                      'params': {'name': 'get_operation', 'arguments': self.arguments, '_meta': modern}})
        for send, (token, code) in [(send, case) for send in (legacy, stateless) for case in
                ((self.grant_value['token'], 'MCP_GRANT_EXPIRED'), ('rvlm_unknown', 'MCP_AUTH_REQUIRED'))]:
            with self.subTest(path=send.__name__, code=code):
                response = send(token)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.json(), {'code': code, 'detail': response.json()['detail']})
                self.assertIn('resource_metadata="', response.headers['www-authenticate'])
                self.assertIn('error="invalid_token"', response.headers['www-authenticate'])
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertEqual(int(response.headers['content-length']), len(response.content))

    def test_write_tool_is_prechecked_outside_the_write_fence(self):
        args = {'research_request_id': self.work['research_request_id'], 'arguments': {'factor_id': 'fixture'},
                'idempotency_key': 'denied'}
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('Invalid credential took the fence')):
            response = self.rpc('rvlm_unknown', 'tools/call', {'name': 'get_factor', 'arguments': args})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertIn('research:write', response.headers['www-authenticate'])
        with patch('reveal_backend.research_http.authenticate', wraps=research_work.authenticate) as transport_auth:
            result = self.tool(self.grant_value, 'get_factor', {**args, 'idempotency_key': 'accepted'})
        self.assertFalse(result.get('isError'), result)
        self.assertEqual(transport_auth.call_count, 1)

    def test_recovery_queue_deduplicates_repeated_polls_across_service_instances(self):
        future = Future()
        with patch.object(research_work.POOL, 'submit', return_value=future) as submit:
            self.service.resume_operation(self.operation['id'])
            ResearchWorkService(self.repo).resume_operation(self.operation['id'])
            self.assertEqual(submit.call_count, 1)
            future.set_result(None)
            self.service.resume_operation(self.operation['id'])
            self.assertEqual(submit.call_count, 2)

    def test_expired_lease_can_recover_while_older_future_is_still_hung(self):
        older, recovery = Future(), Future()
        with patch.object(research_work.POOL, 'submit', side_effect=[older, recovery]) as submit:
            try:
                self.service.resume_operation(self.operation['id'])
                self.service.resume_operation(self.operation['id'], lease_token='expired-attempt-1')
                self.service.resume_operation(self.operation['id'], lease_token='expired-attempt-1')
                self.assertEqual(submit.call_count, 2)
                self.assertFalse(older.done())
                self.assertFalse(recovery.done())
            finally:
                older.set_result(None)
                recovery.set_result(None)
