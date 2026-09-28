"""Transport tests use fake Box/MCP instances; they are not live scientific evidence."""
import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import os

from reveal_backend.agent_execution import ExecutionRequest, MAX_EMIT_BATCH_BYTES, MAX_EMIT_BATCH_EVENTS, emit_batch_size
from reveal_backend.box_adapter import BoxExecutionAdapter, BoxConfigurationError, BoxTransportError, CAPTURE_MARKER, make_bundle, public_event_batches
from reveal_backend.box_mcp import DraftValidationError, Ledger, ScopedTools, PolicyError, query_arguments
from reveal_backend.box_paragraph import validate_paragraph_segments
from reveal_backend.box_stream import ClaudeStream, SecretFilter, StreamProtocolError
from reveal_backend import box_remote


def line(value): return json.dumps(value).encode() + b'\n'


class StreamTests(unittest.TestCase):
    def test_credentials_redacted_across_every_chunk_boundary(self):
        secret='sk-ant-testing-only-123456'
        raw=('before '+secret+' after').encode()
        for chunk_size in range(1,len(secret)+2):
            redactor=SecretFilter((secret,)); chunks=[]
            for offset in range(0,len(raw),chunk_size): chunks.append(redactor.feed(raw[offset:offset+chunk_size]))
            chunks.append(redactor.feed(b'',final=True))
            self.assertEqual(b''.join(chunks),b'before [REDACTED_CREDENTIAL] after')
    def test_incremental_unicode_and_no_private_reasoning(self):
        parser = ClaudeStream()
        raw = b''.join(line(x) for x in [
            {'type': 'stream_event', 'event': {'delta': {'type': 'thinking_delta', 'thinking': 'private reasoning'}}},
            {'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': 'Observable β'}}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Observable β'}, {'type': 'thinking', 'thinking': 'private reasoning'}]}},
            {'type': 'result', 'is_error': False}])
        events = []
        for byte in raw: events.extend(parser.feed(bytes([byte])))
        events.extend(parser.finish())
        self.assertEqual([x[1]['text'] for x in events if x[0] == 'agent_message'], ['Observable β'])
        self.assertNotIn('private reasoning', json.dumps(events))

    def test_malformed_and_truncated_stream_fail(self):
        with self.assertRaises(StreamProtocolError): ClaudeStream().feed(b'no JSON\n')
        with self.assertRaises(StreamProtocolError): ClaudeStream().finish()
        with self.assertRaises(StreamProtocolError): ClaudeStream().feed(b'[]\n')
        with self.assertRaises(StreamProtocolError): ClaudeStream(max_line_bytes=3).feed(b'xxxx')

    def test_tool_errors_and_empty_results_retained(self):
        parser = ClaudeStream()
        parser.feed(line({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'one', 'name': 'Read', 'input': {'path': 'x'}}]}}))
        events = parser.feed(line({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'one', 'content': [], 'is_error': True}]}}))
        self.assertEqual(events[0][1]['status'], 'error')
        self.assertEqual(parser.tools['one']['result']['content'], [])

    def test_orphan_and_duplicate_tool_calls_fail(self):
        with self.assertRaises(StreamProtocolError):
            ClaudeStream().feed(line({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'missing'}]}}))
        parser = ClaudeStream()
        event = line({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'same', 'name': 'Read'}]}})
        parser.feed(event)
        with self.assertRaises(StreamProtocolError): parser.feed(event)
        result = line({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'same','content':[]}]}})
        parser.feed(result)
        with self.assertRaises(StreamProtocolError): parser.feed(result)

    def test_terminal_error_and_post_terminal_fail(self):
        parser = ClaudeStream()
        self.assertEqual(parser.feed(line({'type': 'result', 'is_error': True}))[0][1]['status'], 'failed')
        with self.assertRaises(StreamProtocolError): parser.feed(line({'type': 'assistant'}))


class GraphPolicyTests(unittest.TestCase):
    def test_ledger_redacts_before_durable_artifacts_and_annotates(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); ledger=Ledger(root,'job',1,secrets=('test-secret-value',))
            entry=ledger.start('Read',{'unexpected':'test-secret-value'},None)
            ledger.finish(entry,{'content':'test-secret-value'},'completed');ledger.freeze()
            self.assertEqual(entry['request']['credential_redactions'],1)
            self.assertEqual(entry['response']['credential_redactions'],1)
            for path in root.rglob('*'):
                if path.is_file(): self.assertNotIn(b'test-secret-value',path.read_bytes())
    def test_named_graph_query_is_bounded(self):
        args = query_arguments({'graph': 'prokn', 'subject': 'https://example.org/gene', 'limit': 3}, ('prokn',))
        self.assertIn('GRAPH <https://purl.org/okn/frink/kg/prokn>', args['query'])
        self.assertTrue(args['query'].endswith('LIMIT 3'))

    def test_injection_unselected_and_unbounded_denied(self):
        bad = [{'graph': 'prokn', 'query': 'SELECT * WHERE {?s ?p ?o}'},
               {'graph': 'other', 'subject': 'https://example.org/x'},
               {'graph': 'prokn', 'subject': 'https://example.org/x> } SERVICE <https://evil'},
               {'graph': 'prokn'}, {'graph': 'prokn', 'contains': 'x', 'limit': 999},
               {'graph': 'prokn', 'contains': 'gene', 'limit': True}]
        for args in bad:
            with self.assertRaises(PolicyError): query_arguments(args, ('prokn',))

    def test_complete_ledger_includes_empty_error_denied_and_interrupted(self):
        class Client:
            def call(self, name, args):
                if name == 'get_schema': raise TimeoutError()
                return {'structuredContent': {'row_count': 0, 'rows': []}, 'content': []}
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), 'job', 1)
            tools = ScopedTools(('prokn',), ledger, client=Client())
            tools.call('query_graph', {'graph': 'prokn', 'predicate': 'http://www.w3.org/2000/01/rdf-schema#label', 'contains': 'gene'})
            tools.call('get_schema', {'graph': 'prokn'})
            tools.call('query_graph', {'graph': 'unselected', 'contains': 'gene'})
            ledger.start('Read', {'path': 'some-output'}, None)
            manifest = ledger.freeze()
            self.assertEqual([x['status'] for x in manifest['calls']], ['empty', 'failed', 'denied', 'interrupted'])
            self.assertTrue(manifest['complete'])
            self.assertEqual(len((Path(temp) / 'events.jsonl').read_text().splitlines()), 8)
            for call in manifest['calls']:
                self.assertTrue((Path(temp) / call['request']['path']).exists())
                self.assertTrue((Path(temp) / call['response']['path']).exists())

    def test_call_budget_and_tools_allowlist(self):
        with tempfile.TemporaryDirectory() as temp:
            tools = ScopedTools((), Ledger(Path(temp), 'job', 1), max_calls=1)
            self.assertEqual(tools.definitions(), [])
            self.assertTrue(tools.call('publish_transcript', {})['isError'])
            self.assertTrue(tools.call('list_kgs', {})['isError'])

    def test_draft_schema_feedback_is_failed_not_policy_denied(self):
        def write_draft(*args): raise DraftValidationError('Expected scientific_accounts array')
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), 'job', 1)
            result = ScopedTools((), ledger, write_draft=write_draft).call('write_account_draft', {'filename':'account-1.json','document':{}})
            self.assertEqual(ledger.entries[0]['status'], 'failed')
            self.assertEqual(result['content'][0]['text'], 'Expected scientific_accounts array')

    def test_text_wrapped_empty_response_is_empty(self):
        class Client:
            def call(self, *args): return {'content': [{'type':'text','text':'{"rows":[],"row_count":0}'}], 'isError':False}
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), 'job', 1)
            ScopedTools(('prokn',), ledger, client=Client()).call('query_graph', {'graph':'prokn','predicate':'http://www.w3.org/2000/01/rdf-schema#label','contains':'gene'})
            self.assertEqual(ledger.entries[0]['status'], 'empty')
            self.assertEqual(len(ledger.entries[0]['locators']), 1)

    def test_capture_descriptor_matches_original_response_bytes(self):
        upstream = {'content': [{'type': 'text', 'text': '{"row_count":1,"rows":[{"s":"urn:test:gene","p":"urn:test:relation","o":"urn:test:target"}]}'}]}
        class Client:
            def call(self, *args): return upstream
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), 'job', 1)
            result = ScopedTools(('prokn',), ledger, client=Client()).call('query_graph', {'graph':'prokn','predicate':'http://www.w3.org/2000/01/rdf-schema#label','contains':'gene'})
            capture = json.loads(result['content'][-1]['text'])
            original = (Path(temp) / ledger.entries[0]['response']['path']).read_bytes()
            self.assertEqual(json.loads(original), upstream)
            self.assertEqual(len(upstream['content']), 1)
            self.assertEqual(capture['file']['sha256'], __import__('hashlib').sha256(original).hexdigest())
            self.assertEqual(capture['file']['size_in_bytes'], len(original))
            self.assertEqual(capture['status'], 'completed')
            self.assertEqual(capture['source_locator'], 'ledger_sequence=1;pointer=/content')


class ParagraphTests(unittest.TestCase):
    def setUp(self):
        self.input = {'format': 'reveal.paragraph-input/1', 'account_id':'account', 'account_document': {'claims': [{'id': 'claim'}], 'scientific_accounts':[{'id':'account','component_claims':['claim']}]},
                      'allowed_citations': [{'target_id': 'claim', 'citation_metadata_revision': 2}]}
        self.output = {'format': 'reveal.paragraph-output/1', 'segments': [{'text': 'β has an association.', 'citations': [{'target_id': 'claim', 'citation_metadata_revision': 2}]}]}
    def test_exact_allowed_revision(self):
        self.assertEqual(validate_paragraph_segments(self.output, self.input), self.output['segments'])
        self.output['segments'][0]['citations'][0]['citation_metadata_revision'] = 3
        with self.assertRaises(ValueError): validate_paragraph_segments(self.output, self.input)
    def test_no_claim_and_invented_fields_fail(self):
        self.output['segments'][0]['citations'] = []
        with self.assertRaises(ValueError): validate_paragraph_segments(self.output, self.input)
        self.output['accepted'] = True
        with self.assertRaises(ValueError): validate_paragraph_segments(self.output, self.input)


class DraftHydrationTests(unittest.TestCase):
    def test_exact_sources_and_transitive_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); state=root/'state'; state.mkdir(); output=root/'output'; output.mkdir()
            trusted={'knowledge_gaps':[{'id':'gap','text':'Exact imported gap','was_generated_by':'source-act'}],
                     'activities':[{'id':'source-act','name':'Original import'}]}
            package=root/'input.json'; package.write_text(json.dumps({'dapper_context':trusted}))
            (state/'runtime.json').write_text(json.dumps({'evidence_package':str(package)}))
            with patch.multiple(box_remote,STATE=state,OUTPUT=output), patch.object(box_remote.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid())):
                box_remote.write_draft_tool('account-1.json',{'scientific_accounts':[{'id':'temporary-account','question':'gap'}]})
                doc=json.loads((output/'account-1.json').read_text())
                self.assertEqual(doc['knowledge_gaps'],trusted['knowledge_gaps'])
                self.assertEqual(doc['activities'],trusted['activities'])
                with self.assertRaises(PolicyError):
                    box_remote.write_draft_tool('account-2.json',{'scientific_accounts':[{'id':'temporary-account','question':'gap'}],'knowledge_gaps':[{'id':'gap','text':'Changed'}]})


class ResearchPromptTests(unittest.TestCase):
    def prepared_prompt(self, selected_graphs, feedback=(), frozen_input=False, tamper_input=False):
        """Exercise normal setup without a network clone or a model invocation."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); state = root / 'state'; state.mkdir()
            work = root / 'workspace/reveal'; (work / 'input').mkdir(parents=True)
            package = work / 'input/evidence-package.json'
            package.write_text(json.dumps({'selection': {}, 'pigean': {}, 'source_artifacts': {},
                'dapper_context': {}, 'external_evidence': {'selected_graphs': list(selected_graphs)}}))
            schema = root / 'workspace/dapper/schema'; schema.mkdir(parents=True)
            for name in ('dapper.yaml', 'claims.yaml'): (schema / name).write_text('{}')
            runtime = {'working_directory': str(work), 'evidence_package': str(package), 'dapper_root': str(schema.parent)}
            request = {'kind': 'research', 'job_id': 'arbitrary-job', 'attempt': 1,
                'selected_graphs': list(selected_graphs), 'model': 'test-model', 'claude_version': 'test-version',
                'timeout_seconds': 60, 'max_budget_usd': 1, 'max_turns': 10, 'input_sha256': 'frozen-input'}
            if feedback: request['validation_feedback'] = list(feedback)
            if frozen_input:
                from reveal_backend.dispatch_view import file_input_manifest
                (root / 'input').mkdir()
                manifest = file_input_manifest(package.read_bytes(), feedback)
                if tamper_input: manifest['package']['sha256'] = '0' * 64
                (root / 'input/evidence-input.json').write_text(json.dumps(manifest))
            with patch.multiple(box_remote, BASE=root, STATE=state, OUTPUT=root / 'output'), patch.object(box_remote, 'protect'), \
                    patch.object(box_remote, 'verify_writable_output'), \
                    patch.object(box_remote.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())), \
                    patch('reveal_backend.dapper_release.prepare_agent_workspace', return_value=runtime):
                _, manifest, prompt = box_remote.setup(request)
            from reveal_backend.dispatch_view import research_prompt
            self.assertEqual(prompt, research_prompt(selected_graphs, feedback))
            index = work / 'input/evidence-index.json'
            self.assertEqual(manifest['evidence_reader']['index_sha256'], __import__('hashlib').sha256(index.read_bytes()).hexdigest())
            self.assertEqual(manifest['research_prompt_sha256'], __import__('hashlib').sha256(prompt.encode()).hexdigest())
            self.assertFalse((work / 'input/dispatch-view.json').exists())
            self.assertFalse((work / 'input/package-sections/source_artifacts.json').exists())
            self.assertNotIn('dispatch_budget', manifest)
            return prompt, manifest

    def test_normal_runner_without_feedback_checks_every_scientific_field(self):
        prompt, manifest = self.prepared_prompt(())
        self.assertNotIn('Trusted independent review feedback', prompt)
        self.assertEqual(manifest['validation_feedback_sha256'], __import__('hashlib').sha256(b'[]').hexdigest())
        self.assertIn('no evidence distinguishing upstream causation, downstream readout, reverse causation, or pleiotropy', prompt)
        self.assertIn('even weak, limited, suggestive, or consistent-with', prompt)
        self.assertIn('does not repair an unsupported directional assertion elsewhere', prompt)
        for field in ('Proposition statement/scope', 'Claim statement and assessment',
                      'EvidenceItem explanation/context/snippet', 'ScientificAccount closing_remarks'):
            self.assertIn(field, prompt)
        self.assertIn('association-only biological interpretation', prompt)
        self.assertIn('causal question explicitly unresolved', prompt)
        self.assertIn('future tests, not observed support', prompt)
        self.assertIn('Keep public narration brief', prompt)

    def test_selected_graph_instructions_match_actual_tool_availability(self):
        for graphs in ((), ('prokn',), ('biomarkerkg',), ('prokn', 'biomarkerkg')):
            with self.subTest(graphs=graphs), tempfile.TemporaryDirectory() as temp:
                prompt, _ = self.prepared_prompt(graphs)
                definitions = {tool['name']: tool for tool in ScopedTools(graphs, Ledger(Path(temp), 'job', 1)).definitions()}
                if graphs:
                    self.assertIn('enabled graphs are exactly ' + json.dumps(sorted(graphs)), prompt)
                    for tool in ('get_schema', 'query_graph'):
                        self.assertIn('mcp__reveal__' + tool, prompt)
                        self.assertEqual(definitions[tool]['inputSchema']['properties']['graph']['enum'], list(graphs))
                    self.assertIn('limit=10 or less', prompt)
                    self.assertIn('schema read alone is not a biological evidence search', prompt)
                    self.assertIn('Do not force an irrelevant query', prompt)
                    self.assertIn('empty or failed searches are not biological absence', prompt)
                else:
                    self.assertEqual(definitions, {})
                    self.assertIn('No external graphs are selected', prompt)
                    self.assertNotIn('mcp__reveal__query_graph', prompt)

    def test_review_feedback_supplements_baseline_instead_of_enabling_it(self):
        prompt, _ = self.prepared_prompt(('prokn',), ('Prior source-specific review feedback.',))
        self.assertIn(box_remote.research_authoring_requirements(('prokn',)), prompt)
        self.assertIn('Prior source-specific review feedback.', prompt)

    def test_remote_honors_frozen_file_input_including_feedback(self):
        _, manifest = self.prepared_prompt(('prokn',), ('Previously rejected directional assertion.',), frozen_input=True)
        self.assertEqual(manifest['file_input']['format'], 'reveal.file-backed-evidence/1')
        self.assertEqual(manifest['file_input']['package']['sha256'], manifest['evidence_reader']['package_sha256'])

    def test_remote_rejects_changed_input_binding_before_model_execution(self):
        with self.assertRaises(ValueError):
            self.prepared_prompt((), frozen_input=True, tamper_input=True)


class DeliveryBatchTests(unittest.TestCase):
    def test_utf8_byte_bound_exact_sequence_and_redaction_on_every_event(self):
        secret = 'credential-for-this-test'
        events = [{'type': 'agent_message', 'payload': {'text': '\U0001f9ec' * 16000 + secret, 'delta': True}} for _ in range(11)]
        chunks = list(public_event_batches(events, 7, 'stream', (value for value in (secret,))))
        self.assertGreater(len(chunks), 1)
        flattened = [event for chunk in chunks for event in chunk]
        self.assertEqual([payload['remote_sequence'] for _, payload in flattened], list(range(8, 19)))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_EMIT_BATCH_EVENTS)
            self.assertLessEqual(emit_batch_size(chunk), MAX_EMIT_BATCH_BYTES)
            for kind, payload in chunk:
                self.assertEqual(kind, 'agent_message')
                self.assertEqual(payload['remote_stream_id'], 'stream')
                self.assertEqual(payload['text'], '\U0001f9ec' * 16000 + '[REDACTED]')
                self.assertTrue(payload['delta'])

    def test_count_bound_and_oversized_singleton_are_not_clipped(self):
        events = [{'type': 'tool_call', 'payload': {'call_id': str(i)}} for i in range(45)]
        self.assertEqual([len(chunk) for chunk in public_event_batches(events, 0, 's', ())], [20, 20, 5])
        events = [{'type': 'agent_message', 'payload': {'text': 'x' * MAX_EMIT_BATCH_BYTES}}]
        with self.assertRaises(BoxTransportError): list(public_event_batches(events, 0, 's', ()))


class FakeBox:
    id = 'test-box'
    deleted = False
    closed = False
    async def delete(self): self.deleted = True
    async def aclose(self): self.closed = True


class FakeFactory:
    created = 0
    retrieved = 0
    instance = None
    @classmethod
    async def create(cls, **kwargs):
        cls.created += 1; cls.instance = FakeBox(); return cls.instance
    @classmethod
    async def get(cls, *args, **kwargs):
        cls.retrieved += 1; cls.instance = FakeBox(); return cls.instance


class FakeAdapter(BoxExecutionAdapter):
    calls = []
    async def prepare(self, *args): pass
    async def command(self, box, command): self.calls.append(command); return ''
    async def remote(self, box, action, *args):
        self.calls.append(action)
        if action == 'poll':
            if args[0] == 0:
                return json.dumps({'state': {'status': 'running'}, 'events': [{'type': 'agent_message', 'payload': {'text': 'Visible'}}], 'cursor': 1, 'has_more': False})
            return json.dumps({'state': {'status': 'succeeded'}, 'events': [], 'cursor': 1, 'has_more': False})
        if action == 'collect': return json.dumps({'files': {'output/account-1.json': base64.b64encode(b'{"draft":true}').decode(), 'runtime.json': base64.b64encode(b'{}').decode()}})
        return '{}'


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.input = self.root / 'paragraph.json'
        self.input.write_text(json.dumps({'format': 'reveal.paragraph-input/1'}))
        self.request = ExecutionRequest('job', 1, 'paragraph', self.input, self.root / 'out')
        self.events, self.checkpoints = [], []
        async def emit(kind, payload): self.events.append((kind,payload))
        async def checkpoint(handle): self.checkpoints.append(handle.copy())
        async def cancelled(): return False
        self.emit, self.checkpoint, self.cancelled = emit, checkpoint, cancelled
        FakeFactory.created = FakeFactory.retrieved = 0
        self.adapter = FakeAdapter(Path(__file__).resolve().parents[3], environ={'ANTHROPIC_API_KEY': 'secret-a', 'UPSTASH_BOX_API_KEY': 'secret-b'}, box_factory=FakeFactory, poll_interval=0)
        self.adapter.calls = []
    async def asyncTearDown(self): self.temp.cleanup()
    async def test_incremental_completion_and_cleanup(self):
        result = await self.adapter.execute(self.request, self.emit, self.cancelled, self.checkpoint)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(FakeFactory.created, 1)
        self.assertTrue(FakeFactory.instance.deleted)
        self.assertTrue(any(x[0] == 'agent_message' for x in self.events))
        self.assertEqual(self.checkpoints[-1]['phase'], 'deleted')
    async def test_recovery_reuses_handle_without_duplicate_launch(self):
        request = replace(self.request, remote_handle={'job_id':'job','attempt':1,'box_id':'test-box','cursor':1,'phase':'running','created_at':__import__('time').time()})
        await self.adapter.execute(request, self.emit, self.cancelled, self.checkpoint)
        self.assertEqual(FakeFactory.created, 0)
        self.assertEqual(FakeFactory.retrieved, 1)
        self.assertNotIn('nohup', ' '.join(self.adapter.calls))

    async def test_batch_durability_checkpoints_and_cancellation_between_chunks(self):
        request = replace(self.request, remote_handle={'job_id':'job','attempt':1,'box_id':'test-box','cursor':0,'phase':'running','created_at':__import__('time').time()})
        available = [{'type':'agent_message','payload':{'text':str(i),'delta':True}} for i in range(45)]
        chunks, ordering = [], []
        original = self.adapter.remote
        async def remote(box, action, *args):
            ordering.append(action)
            if action == 'poll':
                return json.dumps({'state':{'status':'succeeded'},'events':available[args[0]:], 'cursor':45,'has_more':False})
            if action == 'collect': self.assertEqual(len(self.events), 45)
            return await original(box, action, *args)
        self.adapter.remote = remote
        class Sink:
            async def __call__(sink, kind, payload): await self.emit(kind, payload)
            async def emit_batch(sink, events):
                chunks.append(len(events)); ordering.append('committed-' + str(events[-1][1]['remote_sequence']))
                self.events.extend(events)
        async def checkpoint(handle):
            self.assertEqual(handle['cursor'], self.events[-1][1]['remote_sequence'])
            ordering.append('checkpoint-' + str(handle['cursor']))
            await self.checkpoint(handle)
        async def cancelled(): return bool(chunks)
        result = await self.adapter.execute(request, Sink(), cancelled, checkpoint)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(chunks, [20, 20, 5])
        self.assertEqual([h['cursor'] for h in self.checkpoints if h['phase'] == 'running'], [20,40,45])
        self.assertLess(ordering.index('committed-20'), ordering.index('checkpoint-20'))
        self.assertLess(ordering.index('checkpoint-20'), ordering.index('cancel'))
        self.assertLess(ordering.index('cancel'), ordering.index('committed-40'))
        self.assertEqual([p['remote_sequence'] for _,p in self.events], list(range(1,46)))
        self.assertEqual({p['remote_stream_id'] for _,p in self.events}, {'test-box:job:1'})
        self.assertTrue(FakeFactory.instance.deleted)

    async def test_failed_batch_retains_box_and_resumes_from_last_durable_chunk(self):
        request = replace(self.request, remote_handle={'job_id':'job','attempt':1,'box_id':'test-box','cursor':0,'phase':'running','created_at':__import__('time').time()})
        available = [{'type':'agent_message','payload':{'text':str(i)}} for i in range(45)]
        original = self.adapter.remote
        async def remote(box, action, *args):
            if action == 'poll':
                self.adapter.calls.append('poll-' + str(args[0]))
                return json.dumps({'state':{'status':'succeeded'},'events':available[args[0]:], 'cursor':45,'has_more':False})
            return await original(box, action, *args)
        self.adapter.remote = remote
        class Sink:
            fail = True
            async def __call__(sink, kind, payload): await self.emit(kind, payload)
            async def emit_batch(sink, events):
                if sink.fail and events[0][1]['remote_sequence'] == 21:
                    raise BoxTransportError('Database interrupted before durable acknowledgement')
                self.events.extend(events)
        sink = Sink()
        with self.assertRaises(BoxTransportError):
            await self.adapter.execute(request, sink, self.cancelled, self.checkpoint)
        self.assertEqual(self.checkpoints[-1]['cursor'], 20)
        self.assertFalse(FakeFactory.instance.deleted)
        self.assertNotIn('collect', self.adapter.calls)
        sink.fail = False
        result = await self.adapter.execute(replace(request, remote_handle=self.checkpoints[-1]), sink, self.cancelled, self.checkpoint)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual([p['remote_sequence'] for _,p in self.events], list(range(1,46)))
        self.assertEqual(self.adapter.calls, ['poll-0','poll-20','collect'])
        self.assertEqual(FakeFactory.created, 0)
        self.assertTrue(FakeFactory.instance.deleted)

    async def test_terminal_backlog_drains_after_local_deadline_before_capture(self):
        request = replace(self.request, remote_handle={'job_id':'job','attempt':1,'box_id':'test-box','cursor':0,'phase':'running',
            'created_at':__import__('time').time()-self.request.timeout_seconds-600})
        original = self.adapter.remote
        async def remote(box, action, *args):
            if action == 'poll':
                self.adapter.calls.append(action)
                first = args[0] == 0
                return json.dumps({'state': {'status': 'succeeded'},
                    'events': [{'type':'agent_message','payload':{'text':'Retained final text','delta':True}}] if first else [],
                    'cursor':1,'has_more':first})
            if action == 'collect':
                self.assertEqual(self.events[0][1]['text'], 'Retained final text')
                self.assertFalse(box.deleted)
            return await original(box, action, *args)
        self.adapter.remote = remote
        result = await self.adapter.execute(request, self.emit, self.cancelled, self.checkpoint)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(self.adapter.calls, ['poll', 'poll', 'collect'])
        self.assertEqual(self.checkpoints[-1]['phase'], 'deleted')
        self.assertEqual(FakeFactory.created, 0)
    async def test_cancel_before_launch(self):
        async def cancelled(): return True
        result = await self.adapter.execute(self.request, self.emit, cancelled, self.checkpoint)
        self.assertEqual(result.status, 'cancelled')
        self.assertTrue(FakeFactory.instance.deleted)
    async def test_resumed_cancelled_job_captures_ledger_before_delete(self):
        request = replace(self.request, remote_handle={'job_id':'job','attempt':1,'box_id':'test-box','cursor':1,'phase':'running','created_at':__import__('time').time()})
        async def cancelled(): return True
        original = self.adapter.remote
        async def remote(box, action, *args):
            if action == 'poll':
                self.adapter.calls.append(action)
                return json.dumps({'state': {'status': 'cancelled'}, 'events': [], 'cursor': 1, 'has_more': False})
            if action == 'collect': self.assertFalse(box.deleted)
            return await original(box, action, *args)
        self.adapter.remote = remote
        result = await self.adapter.execute(request, self.emit, cancelled, self.checkpoint)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(self.adapter.calls, ['cancel', 'poll', 'collect'])
        self.assertTrue((request.output_dir / 'runtime.json').exists())
        self.assertTrue(FakeFactory.instance.deleted)
        self.assertEqual(self.checkpoints[-1]['phase'], 'deleted')
        self.assertEqual(FakeFactory.created, 0)
    async def test_attempt_fencing(self):
        request = replace(self.request, remote_handle={'job_id':'other','attempt':1})
        with self.assertRaises(BoxConfigurationError): await self.adapter.execute(request, self.emit, self.cancelled, self.checkpoint)
    async def test_missing_credentials_prevent_box_creation(self):
        self.adapter.environ = {}
        with self.assertRaises(BoxConfigurationError): await self.adapter.execute(self.request, self.emit, self.cancelled, self.checkpoint)
        self.assertEqual(FakeFactory.created, 0)

    async def test_active_cancellation_reaches_remote_marker(self):
        count = 0
        async def cancelled():
            nonlocal count
            count += 1
            return count > 1
        await self.adapter.execute(self.request, self.emit, cancelled, self.checkpoint)
        self.assertIn('cancel', self.adapter.calls)

    async def test_capture_failure_preserves_box_for_recovery(self):
        original = self.adapter.remote
        async def remote(box, action, *args):
            if action == 'collect': raise OSError('connection interrupted')
            return await original(box, action, *args)
        self.adapter.remote = remote
        with self.assertRaises(OSError): await self.adapter.execute(self.request, self.emit, self.cancelled, self.checkpoint)
        self.assertFalse(FakeFactory.instance.deleted)
        self.assertTrue(FakeFactory.instance.closed)
        self.assertEqual(self.checkpoints[-1]['phase'], 'running')

    async def test_delete_failure_remains_recoverable_after_durable_capture(self):
        async def failed_delete(box): raise OSError('delete transport interrupted')
        with patch.object(FakeBox, 'delete', failed_delete):
            with self.assertRaises(BoxTransportError):
                await self.adapter.execute(self.request, self.emit, self.cancelled, self.checkpoint)
        self.assertTrue((self.request.output_dir / 'runtime.json').exists())
        self.assertTrue(FakeFactory.instance.closed)
        self.assertEqual(self.checkpoints[-1]['phase'], 'running')
        self.assertTrue(any(p.get('code') == 'box_cleanup_pending' for _,p in self.events))

    async def test_deleted_box_with_failed_checkpoint_resumes_local_capture(self):
        async def checkpoint(handle):
            if handle['phase'] == 'deleted': raise OSError('database unavailable after deletion')
            await self.checkpoint(handle)
        with self.assertRaises(BoxTransportError):
            await self.adapter.execute(self.request, self.emit, self.cancelled, checkpoint)
        self.assertTrue(FakeFactory.instance.deleted)
        self.assertTrue(json.loads((self.request.output_dir / CAPTURE_MARKER).read_text())['cleanup_complete'])
        saved = self.checkpoints[-1].copy()
        self.assertEqual(saved['phase'], 'running')
        calls = list(self.adapter.calls)
        resumed = replace(self.request, remote_handle=saved)
        result = await self.adapter.execute(resumed, self.emit, self.cancelled, self.checkpoint)
        self.assertEqual(result.status, 'succeeded')
        self.assertTrue(result.account_paths[0].is_file())
        self.assertEqual(FakeFactory.created, 1)
        self.assertEqual(FakeFactory.retrieved, 0)
        self.assertEqual(self.adapter.calls, calls)
        self.assertEqual(self.checkpoints[-1]['phase'], 'deleted')

    async def test_crash_after_deleted_checkpoint_reuses_verified_capture(self):
        first = await self.adapter.execute(self.request, self.emit, self.cancelled, self.checkpoint)
        resumed = replace(self.request, remote_handle=self.checkpoints[-1])
        result = await self.adapter.execute(resumed, self.emit, self.cancelled, self.checkpoint)
        self.assertEqual(result.account_paths, first.account_paths)
        self.assertEqual(result.status, first.status)
        self.assertEqual(FakeFactory.retrieved, 0)
        # A valid cleanup marker cannot hide changed scientific output bytes.
        result.account_paths[0].write_text('{"changed":true}')
        with self.assertRaises(BoxTransportError):
            await self.adapter.execute(resumed, self.emit, self.cancelled, self.checkpoint)

    async def test_capture_recovery_requires_sdk_404_not_arbitrary_get_failure(self):
        async def failed_delete(box): raise OSError('deletion acknowledgement lost')
        with patch.object(FakeBox, 'delete', failed_delete):
            with self.assertRaises(BoxTransportError):
                await self.adapter.execute(self.request, self.emit, self.cancelled, self.checkpoint)
        resumed = replace(self.request, remote_handle=self.checkpoints[-1].copy())
        class SDKBoxError(Exception):
            def __init__(self, code): self.status_code = code
        class OtherError(Exception):
            status_code = 404
        async def unrelated_get(*args, **kwargs): raise OtherError()
        with patch.object(FakeFactory, 'get', unrelated_get):
            with self.assertRaises(BoxTransportError):
                await self.adapter.execute(resumed, self.emit, self.cancelled, self.checkpoint)
        self.assertFalse(json.loads((self.request.output_dir / CAPTURE_MARKER).read_text())['cleanup_complete'])
        async def missing_get(*args, **kwargs): raise SDKBoxError(404)
        with patch.dict('sys.modules', {'upstash_box.errors': SimpleNamespace(BoxError=SDKBoxError)}), patch.object(FakeFactory, 'get', missing_get):
            result = await self.adapter.execute(resumed, self.emit, self.cancelled, self.checkpoint)
        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(self.checkpoints[-1]['phase'], 'deleted')
        self.assertEqual(FakeFactory.created, 1)
        self.assertTrue(json.loads((self.request.output_dir / CAPTURE_MARKER).read_text())['cleanup_complete'])

    async def test_local_capture_cannot_be_reused_with_different_frozen_input(self):
        await self.adapter.execute(self.request, self.emit, self.cancelled, self.checkpoint)
        resumed = replace(self.request, remote_handle=self.checkpoints[-1])
        self.input.write_text('{"format":"changed"}')
        with self.assertRaises(BoxTransportError):
            await self.adapter.execute(resumed, self.emit, self.cancelled, self.checkpoint)

    async def test_interrupted_preparation_cleans_up_without_relaunch(self):
        request = replace(self.request, remote_handle={'job_id':'job','attempt':1,'box_id':'test-box','cursor':0,'phase':'created','created_at':__import__('time').time()})
        result = await self.adapter.execute(request, self.emit, self.cancelled, self.checkpoint)
        self.assertEqual(result.status, 'failed')
        self.assertTrue(FakeFactory.instance.deleted)
        self.assertNotIn('nohup', ' '.join(self.adapter.calls))


if __name__ == '__main__': unittest.main()
