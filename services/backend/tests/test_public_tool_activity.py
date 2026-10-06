"""Observable tool projections remain useful, bounded, and credential-safe."""
import json
import unittest

from reveal_backend.box_stream import ClaudeStream
from reveal_backend.public_tool_activity import (ARGUMENTS, ARGUMENT_BYTES, PREVIEW_BYTES, display_arguments, durable_operation,
                                                 result_preview, tool_call_payload, tool_result_payload)
from reveal_backend.worker import public_activity


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode() + b'\n'


class PublicToolActivityTests(unittest.TestCase):
    def test_hosted_catalog_has_explicit_safe_argument_projection(self):
        from reveal_backend.box_research import ALLOWED_TOOLS
        from reveal_backend.research_tools import private_definitions
        self.assertFalse(ALLOWED_TOOLS - ARGUMENTS.keys())
        hidden = {'local_work_id', 'research_request_id', 'idempotency_key'}
        for tool in private_definitions():
            if tool['name'] in ALLOWED_TOOLS:
                with self.subTest(tool=tool['name']):
                    self.assertEqual(set(ARGUMENTS[tool['name']]),
                        set(tool['inputSchema']['properties']) - hidden)
        inputs = {'arguments': {'factor_id': 'pinned-factor', 'kind': 'genes', 'limit': 25,
                               'api_key': 'NEVER SHOW', 'thinking': 'PRIVATE THOUGHT'},
                  'research_request_id': 'PRIVATE SCOPE', 'idempotency_key': 'PRIVATE RETRY',
                  'authorization': 'PRIVATE TOKEN'}
        shown = display_arguments('mcp__reveal__get_factor_loadings', inputs)
        self.assertEqual(json.loads(shown)['arguments']['factor_id'], 'pinned-factor')
        for private in ('NEVER SHOW', 'PRIVATE THOUGHT', 'PRIVATE SCOPE', 'PRIVATE RETRY', 'PRIVATE TOKEN'):
            self.assertNotIn(private, shown)
        self.assertEqual(json.loads(display_arguments('mcp__reveal__get_operation',
            {'operation_id': 'operation-1'})), {'operation_id': 'operation-1'})

    def test_pending_operation_is_identified_without_claiming_scientific_completion(self):
        value = {'operation_id': 'operation-1', 'state': 'received', 'detail': 'PRIVATE UNSTRUCTURED DETAIL'}
        for result in ({'structuredContent': value}, {'content': json.dumps(value)},
                       {'content': [{'type': 'text', 'text': json.dumps(value)}]}):
            with self.subTest(result=result):
                projected = tool_result_payload('call-1', 'mcp__reveal__get_factor_loadings', {}, result)
                self.assertIn('scientific result is not ready', projected['message'])
                self.assertIn('operation-1', projected['message'])
                self.assertNotIn('PRIVATE', projected['message'])
                self.assertEqual(durable_operation(result), {'operation_id': 'operation-1', 'state': 'received'})
        self.assertIsNone(durable_operation({'content': 'unstructured output'}))
        self.assertIsNone(durable_operation({'structuredContent': {'operation_id': 'unsafe\ntext', 'state': 'running'}}))
        completed = tool_result_payload('call-2', 'mcp__reveal__get_operation', {},
            {'structuredContent': {'operation_id': 'operation-1', 'state': 'succeeded'}})
        self.assertNotIn('not ready', completed['message'])
        for secret in ('configured-secret-value', 'sk-ant-fixture-secret-0123456789abcdef'):
            result = {'structuredContent': {'operation_id': secret, 'state': 'running'}}
            projected = tool_result_payload('call-3', 'mcp__reveal__get_operation', {}, result, (secret,))
            self.assertNotIn(secret, json.dumps(projected))
            self.assertIsNone(durable_operation(result, (secret,)))
        unrelated = tool_result_payload('read-1', 'Read', {}, {'structuredContent': value})
        self.assertNotIn('scientific result', unrelated['message'])

    def test_read_arguments_result_correlation_and_observed_duration(self):
        clock = iter((1.0, 2.234))
        parser = ClaudeStream(clock=lambda: next(clock))
        events = parser.feed(encoded({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'read-1', 'name': 'Read', 'input': {'file_path': '/reveal/workspace/reveal/input/evidence-index.json', 'offset': 10, 'limit': 50}}
        ]}}))
        events += parser.feed(encoded({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'read-1', 'content': '10\tExact visible source row.'}
        ]}}))
        call, result = [value for _, value in events]
        self.assertEqual(call['call_id'], result['call_id'])
        self.assertEqual(call['tool_name'], 'Read')
        self.assertEqual(json.loads(call['display_arguments'])['offset'], 10)
        self.assertIn('Exact visible source row.', result['output_excerpt'])
        self.assertEqual(result['duration_ms'], 1234)
        self.assertNotIn('Using an authorized tool', json.dumps(events))

    def test_graph_selectors_and_errors_are_visible_without_internal_ledger_duplicates(self):
        args = {'graph': 'prokn', 'contains': 'source-derived term', 'limit': 5}
        call = tool_call_payload('query-1', 'mcp__reveal__query_graph', args)
        result = tool_result_payload('query-1', 'mcp__reveal__query_graph', args,
            {'is_error': True, 'content': [{'type': 'text', 'text': 'The endpoint timed out.'}]})
        self.assertEqual(call['selected_graph'], 'prokn')
        self.assertEqual(json.loads(call['display_arguments']), args)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['output_excerpt'], 'The endpoint timed out.')
        self.assertEqual(result['call_id'], call['call_id'])
        empty = tool_result_payload('query-2', 'mcp__reveal__query_graph', args, {'content': []})
        self.assertEqual(empty['status'], 'completed')
        self.assertIn('empty result', empty['output_excerpt'])

    def test_write_edit_and_draft_arguments_omit_full_authored_content(self):
        private = 'Do not publish this large document or copied credential.'
        cases = [('Write', {'file_path': '/reveal/output/account-1.json', 'content': private}),
                 ('Edit', {'file_path': '/reveal/output/account-1.json', 'old_string': private, 'new_string': private + '!'}),
                 ('mcp__reveal__write_account_draft', {'filename': 'account-1.json',
                    'document': {'claims': [{'statement': private}], 'scientific_accounts': [{}], 'thinking': private}})]
        for name, args in cases:
            with self.subTest(name=name):
                value = display_arguments(name, args)
                self.assertNotIn(private, value)
                self.assertIn('account-1.json', value)
        self.assertEqual(json.loads(display_arguments(cases[-1][0], cases[-1][1]))['node_counts'], {'claims': 1, 'scientific_accounts': 1})

    def test_structured_secret_keys_bearer_values_and_private_fields_are_omitted(self):
        result = {'content': [{'type': 'thinking', 'thinking': 'PRIVATE BLOCK', 'text': 'HIDDEN BLOCK TEXT'},
            {'type': 'text', 'text': json.dumps({'rows': [{'name': 'Public result', 'api_key': 'API VALUE',
             'access_token': 'ACCESS VALUE', 'Authorization': 'Bearer AUTH_VALUE', 'password': 'PASS VALUE'}],
             'thinking': 'PRIVATE THINKING', 'analysis': {'text': 'PRIVATE ANALYSIS'}, 'signature': 'PRIVATE SIGNATURE'})}]}
        public = result_preview(result, {})
        self.assertIn('Public result', public)
        for value in ('PRIVATE BLOCK', 'HIDDEN BLOCK TEXT', 'API VALUE', 'ACCESS VALUE', 'AUTH_VALUE', 'PASS VALUE',
                      'PRIVATE THINKING', 'PRIVATE ANALYSIS', 'PRIVATE SIGNATURE'):
            self.assertNotIn(value, public)
        text = 'Authorization: Bearer sample-auth-token.abc\napi_key="literal-secret"\nVisible error.'
        projected = result_preview({'content': text}, {})
        self.assertIn('Visible error', projected)
        self.assertNotIn('sample-auth-token', projected)
        self.assertNotIn('literal-secret', projected)

    def test_exact_secrets_redacted_before_byte_truncation_and_unicode_bounds(self):
        secret = 'configured-secret-do-not-display'
        args = {'pattern': '🧬' * 510 + secret + ' end', 'path': '/reveal/workspace/reveal/input'}
        projected = display_arguments('Grep', args, (secret,))
        self.assertLessEqual(len(projected.encode()), ARGUMENT_BYTES)
        self.assertNotIn(secret[:15], projected)
        preview = result_preview({'content': 'β' * 2040 + secret + ' end'}, {}, (secret,))
        self.assertLessEqual(len(preview.encode()), PREVIEW_BYTES)
        self.assertNotIn(secret[:15], preview)
        self.assertIn('[truncated]', preview)

    def test_sensitive_file_outputs_and_unknown_legacy_shapes_have_safe_fallbacks(self):
        for path in ('/proc/self/environ', '/reveal/credentials.json', '/reveal/workspace/reveal/.env', '/reveal/state/claude-stream.jsonl'):
            with self.subTest(path=path):
                self.assertNotIn('PRIVATE', result_preview({'content': 'PRIVATE'}, {'file_path': path}))
        self.assertEqual(tool_result_payload('old', None, None, None)['output_excerpt'], 'Tool result details unavailable.')
        self.assertIn('unavailable', display_arguments('Unknown', {'command': 'PRIVATE'}))
        self.assertNotIn('PRIVATE', display_arguments('Unknown', {'command': 'PRIVATE'}))
        # Non-scalar biomedical "type" fields are ordinary data, not private blocks.
        value = result_preview({'structuredContent': {'type': ['Gene', 'Protein'], 'rows': []}}, {})
        self.assertIn('Protein', value)

    def test_only_public_text_emitted_and_split_credentials_redacted_across_unicode_deltas(self):
        secret = 'sk-ant-private-testing-credential'
        text = 'Observable β ' + secret + ' 🧬 complete.'
        for split in range(1, len(secret) + 1):
            parser = ClaudeStream(secrets=(secret,))
            events = parser.feed(encoded({'type': 'stream_event', 'event': {'delta': {'type': 'thinking_delta', 'thinking': 'PRIVATE THOUGHTS'}}}))
            for offset in range(0, len(text), split):
                events += parser.feed(encoded({'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': text[offset:offset + split]}}}))
            events += parser.feed(encoded({'type': 'result', 'is_error': False}))
            public = ''.join(payload['text'] for kind, payload in events if kind == 'agent_message')
            self.assertNotIn(secret, public)
            self.assertNotIn('PRIVATE THOUGHTS', json.dumps(events))
            self.assertIn('Observable β', public)
            self.assertIn('🧬 complete.', public)

    def test_worker_distinguishes_setup_lifecycle_narrative_and_tool_result(self):
        job = {'kind': 'analysis', 'stage': 'starting_agent', 'warnings': []}
        setup = public_activity(job, 'stage', {'stage': 'starting_agent', 'source': 'harness', 'message': 'Preparing runtime.'})
        self.assertEqual(setup[2]['kind'], 'preparation')
        self.assertEqual(setup[2]['source'], 'harness')
        start = public_activity(job, 'agent_started', {'message': 'Claude started.'})
        self.assertEqual(start[2]['kind'], 'preparation')
        self.assertEqual(job['stage'], 'authoring_account')
        narrative = public_activity(job, 'agent_message', {'text': 'Checking the selected evidence.', 'delta': True})
        self.assertEqual(narrative[2]['kind'], 'agent_message')
        self.assertTrue(narrative[2]['message_delta'])
        result = public_activity(job, 'tool_result', {'call_id': 'read-1', 'tool_name': 'Read', 'status': 'completed',
            'display_arguments': '{"offset":10}', 'output_excerpt': 'Exact public row', 'artifact_sha256': 'a' * 64, 'duration_ms': 12})
        self.assertEqual(result[2]['call_id'], 'read-1')
        self.assertEqual(result[2]['output_excerpt'], 'Exact public row')
        self.assertEqual(result[2]['artifact_sha256'], 'a' * 64)
        self.assertEqual(result[2]['duration_ms'], 12)
        legacy = public_activity(job, 'tool_call', {'tool': 'Read', 'call_id': 'old'})
        self.assertEqual(legacy[2]['tool_name'], 'Read')
        self.assertIsNone(legacy[2]['display_arguments'])
        self.assertIsNone(legacy[2]['output_excerpt'])


if __name__ == '__main__':
    unittest.main()
