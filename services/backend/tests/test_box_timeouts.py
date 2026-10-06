"""Bounded KG reads, immutable failure captures, and explicit deadline outcomes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rdflib import Dataset, URIRef, Literal
from rdflib.plugins.sparql.parser import parseQuery

from reveal_backend import box_remote
from reveal_backend.box_mcp import (Ledger, ScopedTools, QueryValidationError,
                                  canonical, query_arguments)

LABEL = 'http://www.w3.org/2000/01/rdf-schema#label'
EMPTY = {'content': [{'type': 'text', 'text': '{"rows":[],"row_count":0}'}]}


class KGTimeoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = Ledger(self.root, 'offline', 1)

    def test_literal_lookup_is_exact_escaped_and_confined_to_selected_graph(self):
        value = 'A "quoted" β symbol\\name'
        query = query_arguments({'graph': 'prokn', 'predicate': LABEL, 'literal': value, 'limit': 3}, ('prokn',))['query']
        parseQuery(query)
        self.assertIn(json.dumps(value), query)
        self.assertIn('GRAPH <https://purl.org/okn/frink/kg/prokn>', query)
        self.assertNotIn('CONTAINS', query)
        self.assertTrue(query.endswith('LIMIT 3'))
        for arguments in ({'literal': 'symbol'}, {'literal': 'symbol', 'predicate': LABEL, 'object': 'urn:other'},
                          {'literal': 'symbol', 'predicate': LABEL, 'contains': 'symbol'}):
            with self.subTest(arguments=arguments), self.assertRaises(QueryValidationError):
                query_arguments({'graph': 'prokn', **arguments}, ('prokn',))

    def test_unbound_scan_returns_repairable_feedback_without_upstream_call(self):
        client = Mock()
        result = ScopedTools(('prokn',), self.ledger, client=client).call('query_graph', {'graph': 'prokn', 'contains': 'symbol'})
        self.assertTrue(result['isError']); client.call.assert_not_called()
        self.assertEqual(self.ledger.entries[0]['status'], 'failed')
        self.assertIn('predicate', result['content'][0]['text'])

    def test_curie_query_can_be_corrected_without_poisoning_execution_ledger(self):
        from reveal_backend.agent_execution import ExecutionRequest, ExecutionResult
        from reveal_backend.acceptance import LOCK
        from reveal_backend.evidence_package import sha256
        from reveal_backend.worker import validate_execution_ledger

        client = Mock(server_info={}); client.call.return_value = deepcopy(EMPTY)
        tools = ScopedTools(('biomarkerkg',), self.ledger, client=client)
        # Actual query shape from QA job 374ef39d, previously classified denied.
        arguments = {'graph': 'biomarkerkg', 'limit': 10, 'object': 'MONDO:0010017',
                     'predicate': 'http://purl.obolibrary.org/obo/OBCI_1000002'}
        feedback = tools.call('query_graph', arguments)
        self.assertTrue(feedback['isError']); client.call.assert_not_called()
        self.assertEqual(self.ledger.entries[0]['status'], 'failed')
        self.assertIn('expand CURIEs', feedback['content'][0]['text'])
        tools.call('query_graph', {**arguments, 'object': 'http://purl.obolibrary.org/obo/MONDO_0010017'})
        self.assertEqual(self.ledger.entries[1]['status'], 'empty')
        self.ledger.freeze()

        package = self.root/'input.json'; package.write_bytes(b'{}')
        runtime = self.root/'runtime.json'
        runtime.write_bytes(canonical({'job_id': 'offline', 'attempt': 1,
            'input_sha256': sha256(package.read_bytes()), 'model': 'test-model',
            'dapper': {'commit': json.loads(LOCK.read_bytes())['commit']}}))
        request = ExecutionRequest('offline', 1, 'research', package, self.root,
                                   selected_graphs=('biomarkerkg',))
        result = ExecutionResult('insufficient_evidence', self.root,
            runtime_manifest_path=runtime, ledger_manifest_path=self.root/'manifest.json')
        validate_execution_ledger(result, request, 'test-model')

    def test_query_typos_fail_but_policy_violations_stay_denied(self):
        client = Mock()
        tools = ScopedTools(('prokn',), self.ledger, client=client)
        repairable = [
            {'subject': 'relative/path'}, {'subject': 'https://example.org/has space'},
            {'subject': 123}, {'predicate': LABEL, 'contains': 'x'},
            {'predicate': LABEL, 'contains': 'gene', 'object': 'urn:gene'},
        ]
        denied = [
            {'subject': 'https://example.org/x> } SERVICE <https://evil'},
            {'query': 'SELECT * WHERE {?s ?p ?o}'}, {'graph': 'unselected', 'subject': 'urn:gene'},
        ]
        for arguments, status in [(value, 'failed') for value in repairable] + [(value, 'denied') for value in denied]:
            with self.subTest(arguments=arguments):
                result = tools.call('query_graph', {'graph': 'prokn', **arguments})
                self.assertTrue(result['isError'])
                self.assertEqual(self.ledger.entries[-1]['status'], status)
        client.call.assert_not_called()

    def test_predicate_scoped_contains_keeps_object_unbound(self):
        dataset = Dataset()
        graph = dataset.graph(URIRef('https://purl.org/okn/frink/kg/prokn'))
        graph.add((URIRef('urn:gene:1'), URIRef(LABEL), Literal('SYMBOL alpha')))
        graph.add((URIRef('urn:gene:2'), URIRef(LABEL), Literal('Other')))
        dataset.graph(URIRef('urn:unselected')).add((URIRef('urn:gene:3'), URIRef(LABEL), Literal('SYMBOL hidden')))
        for scope in ({'predicate': LABEL}, {'subject': 'urn:gene:1'}):
            query = query_arguments({'graph': 'prokn', **scope, 'contains': 'SYMBOL'}, ('prokn',))['query']
            parseQuery(query)
            self.assertIn('CONTAINS(LCASE(STR(?o)), "symbol")', query)
            self.assertNotIn('AS ?o)', query)
            self.assertEqual([(str(row.s), str(row.o)) for row in dataset.query(query)], [('urn:gene:1', 'SYMBOL alpha')])

    def test_embedded_upstream_error_is_failure_with_original_bytes_retained(self):
        for field in ('content', 'structuredContent'):
            original = ({'content': [{'type': 'text', 'text': '{"error":"HTTP 429 after 2 attempts: Operation timed out","row_count":0}'}]}
                        if field == 'content' else {'content': [], 'structuredContent': {'error': 'Upstream timeout', 'row_count': 0}})
            client = Mock(server_info={'version': 'test'}); client.call.return_value = deepcopy(original)
            result = ScopedTools(('prokn',), self.ledger, client=client).call('query_graph', {'graph': 'prokn', 'subject': 'urn:test:gene'})
            entry = self.ledger.entries[-1]
            self.assertEqual(entry['status'], 'failed'); self.assertTrue(result['isError'])
            self.assertEqual((self.root / entry['upstream_response']['path']).read_bytes(), canonical(original))
            self.assertEqual(json.loads(result['content'][-1]['text'])['status'], 'failed')

    def test_slow_graph_does_not_block_other_graph_or_authoring_tool(self):
        started, release = threading.Event(), threading.Event()
        class Client:
            def call(self, tool, args):
                if args.get('shortname') == 'prokn':
                    started.set(); release.wait(2)
                return deepcopy(EMPTY)
        tools = ScopedTools(('prokn', 'biomarkerkg'), self.ledger, client=Client(), read_timeout=.8,
                            lint=lambda filename: {'content': [], 'valid': True})
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(tools.call, 'get_schema', {'graph': 'prokn'})
            self.assertTrue(started.wait(.5))
            before = time.monotonic()
            fast = tools.call('get_schema', {'graph': 'biomarkerkg'})
            lint = tools.call('lint_account', {'filename': 'account-1.json'})
            elapsed = time.monotonic() - before
            release.set(); slow.result(1)
        self.assertLess(elapsed, .4)
        self.assertFalse(fast.get('isError', False)); self.assertTrue(lint['valid'])
        self.assertEqual(len(self.ledger.entries), 3)

    def test_hard_wall_deadline_capacity_and_late_response_cannot_mutate_capture(self):
        release, ended = threading.Event(), threading.Event()
        class Client:
            calls = 0
            def call(self, *args):
                self.calls += 1
                release.wait(2); ended.set()
                return {'structuredContent': {'error': 'Late error'}, 'content': []}
        client = Client(); tools = ScopedTools(('prokn',), self.ledger, client=client, read_timeout=.04, max_parallel_reads=1)
        before = time.monotonic()
        result = tools.call('get_schema', {'graph': 'prokn'})
        self.assertLess(time.monotonic() - before, .4)
        self.assertTrue(result['isError']); self.assertEqual(self.ledger.entries[0]['status'], 'failed')
        busy = tools.call('get_schema', {'graph': 'prokn'})
        self.assertTrue(busy['isError']); self.assertEqual(client.calls, 1)
        self.ledger.freeze(); snapshot = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        release.set(); self.assertTrue(ended.wait(.5)); time.sleep(.01)
        self.assertEqual(snapshot, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_freeze_during_pending_read_keeps_interrupted_outcome_immutable(self):
        started, release = threading.Event(), threading.Event()
        class Client:
            server_info = {'version': 'late'}
            def call(self, *args):
                started.set(); release.wait(2)
                return {'structuredContent': {'error': 'Late upstream error'}, 'content': []}
        tools = ScopedTools(('prokn',), self.ledger, client=Client(), read_timeout=.8)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(tools.call, 'get_schema', {'graph': 'prokn'})
            self.assertTrue(started.wait(.5)); self.ledger.freeze()
            snapshot = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            release.set(); result = pending.result(1)
        self.assertEqual(self.ledger.entries[0]['status'], 'interrupted')
        self.assertTrue(result['isError'])
        self.assertEqual(snapshot, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_default_upstream_sessions_are_independent(self):
        created = []
        class Client:
            def __init__(self, **kwargs):
                self.server_info = {'session': len(created)}; created.append(self)
            def call(self, *args): return deepcopy(EMPTY)
        with patch('reveal_backend.box_mcp.MCPClient', Client):
            tools = ScopedTools(('prokn', 'biomarkerkg'), self.ledger)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda graph: tools.call('get_schema', {'graph': graph}), ('prokn', 'biomarkerkg')))
        self.assertEqual(len(created), 2)
        self.assertEqual({entry['source_version']['session'] for entry in self.ledger.entries}, {0, 1})

    def test_concurrent_call_budget_is_reserved_once_and_cannot_overspend(self):
        client = Mock(server_info={}); client.call.return_value = deepcopy(EMPTY)
        tools = ScopedTools(('prokn',), self.ledger, client=client, max_calls=2)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: tools.call('get_schema', {'graph': 'prokn'}), range(5)))
        self.assertEqual(client.call.call_count, 2)
        self.assertEqual(sum(entry['status'] == 'denied' for entry in self.ledger.entries), 3)
        self.assertEqual(len({entry['sequence'] for entry in self.ledger.entries}), 5)


class DraftAndRuntimeTests(unittest.TestCase):
    def test_timeout_reason_tracks_observable_polling_without_guessing_authoring(self):
        request = {'kind': 'research', 'timeout_seconds': 900}
        tool = {'tool_name': 'mcp__reveal__get_operation'}
        waiting = box_remote.observable_activity('tool_call', tool, {'input': {'operation_id': 'operation-1'}})
        self.assertIn('waiting for a durable evidence operation', box_remote.deadline_reason(request, waiting))
        result = {'structuredContent': {'operation_id': 'operation-1', 'state': 'running', 'detail': 'PRIVATE'}}
        waiting = box_remote.observable_activity('tool_result', tool, {'result': result})
        self.assertEqual(waiting['operation'], {'operation_id': 'operation-1', 'state': 'running'})
        self.assertNotIn('PRIVATE', json.dumps(waiting))
        self.assertIn('waiting for a durable evidence operation', box_remote.deadline_reason(request, waiting))
        result['structuredContent']['state'] = 'succeeded'
        done = box_remote.observable_activity('tool_result', tool, {'result': result})
        self.assertNotIn('waiting', box_remote.deadline_reason(request, done))
        self.assertIn('during research', box_remote.deadline_reason(request, done))
        lint = {'kind': 'tool_call', 'tool_name': 'mcp__reveal__lint_account'}
        self.assertIn('checking the draft account and sources', box_remote.deadline_reason(request, lint))
        self.assertIn('writing the research statement', box_remote.deadline_reason({'kind': 'paragraph', 'timeout_seconds': 900}))

    def test_prose_mentions_do_not_hydrate_unreachable_nodes_but_exact_links_do(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); state = root / 'state'; state.mkdir(); output = root / 'output'; output.mkdir()
            trusted = {'knowledge_gaps': [{'id': 'urn:gap', 'was_generated_by': 'urn:import'}],
                       'activities': [{'id': 'urn:import'}], 'mechanisms': [{'id': 'urn:mechanism', 'name': 'Source mechanism'}]}
            package = root / 'package.json'; package.write_text(json.dumps({'dapper_context': trusted}))
            (state / 'runtime.json').write_text(json.dumps({'evidence_package': str(package)}))
            draft = {'scientific_accounts': [{'id': 'urn:new', 'question': 'urn:gap', 'context': 'A prose mention of urn:mechanism.'}]}
            with patch.multiple(box_remote, STATE=state, OUTPUT=output), patch.object(box_remote.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())):
                box_remote.write_draft_tool('account-1.json', deepcopy(draft))
                first = json.loads((output / 'account-1.json').read_text())
                self.assertNotIn('mechanisms', first)
                self.assertEqual(first['knowledge_gaps'], trusted['knowledge_gaps'])
                self.assertEqual(first['activities'], trusted['activities'])
                draft['propositions'] = [{'id': 'urn:proposition', 'subject_entity': 'urn:mechanism'}]
                box_remote.write_draft_tool('account-2.json', draft)
                self.assertEqual(json.loads((output / 'account-2.json').read_text())['mechanisms'], trusted['mechanisms'])

    def test_deadline_exit_records_clear_reason_and_unavailable_usage(self):
        for returncode in (124, 137):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); state = root / 'state'; output = root / 'output'
                request = {'job_id': 'offline', 'attempt': 1, 'kind': 'research', 'selected_graphs': [],
                           'claude_version': 'test', 'model': 'test', 'max_turns': 3, 'max_budget_usd': .1, 'timeout_seconds': 900}
                (root / 'request.json').write_text(json.dumps(request))
                (root / 'credentials.json').write_text('{"ANTHROPIC_API_KEY":"offline-placeholder"}')
                runtime = {'dapper_root': str(root / 'dapper')}
                process = SimpleNamespace(stdin=io.BytesIO(), stdout=object(), stderr=object(),
                                          pid=999999, returncode=returncode, poll=lambda: returncode)
                selector = Mock(); selector.get_map.return_value = {}
                user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(root))
                with patch.multiple(box_remote, BASE=root, STATE=state, OUTPUT=output, SECRETS=()), \
                     patch.object(box_remote.os, 'getuid', return_value=0), \
                     patch.object(box_remote.pwd, 'getpwnam', return_value=user), \
                     patch.object(box_remote, 'setup', return_value=(root, runtime, 'Offline prompt')), \
                     patch.object(box_remote, 'serve', return_value=Mock()), \
                     patch.object(box_remote.subprocess, 'run', return_value=SimpleNamespace(stdout='test version')), \
                     patch.object(box_remote.subprocess, 'Popen', return_value=process), \
                     patch.object(box_remote.selectors, 'DefaultSelector', return_value=selector):
                    box_remote.main()
                status = json.loads((state / 'status.json').read_text())
                completion = json.loads((state / 'runtime.json').read_text())['completion']
                self.assertEqual(status['status'], 'failed')
                self.assertIn('900-second execution limit' if returncode == 124 else 'before its execution deadline', status['reason'])
                self.assertNotIn('trusted agent execution failed', status['reason'])
                self.assertEqual(completion['usage_status'], 'unavailable'); self.assertIsNone(completion['cost_usd'])
                self.assertEqual(completion['process_returncode'], returncode)
                self.assertTrue(json.loads((state / 'ledger/manifest.json').read_text())['complete'])


    def test_turn_limit_remains_failure_even_if_agent_left_an_outcome_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); state = root / 'state'; output = root / 'output'; output.mkdir()
            request = {'job_id': 'offline', 'attempt': 1, 'kind': 'research', 'selected_graphs': [],
                       'claude_version': 'test', 'model': 'test', 'max_turns': 40, 'max_budget_usd': 3, 'timeout_seconds': 900}
            (root / 'request.json').write_text(json.dumps(request))
            (root / 'credentials.json').write_text('{"ANTHROPIC_API_KEY":"offline-placeholder"}')
            (output / 'outcome.json').write_text('{"status":"insufficient_evidence","reason":"Unfinished draft"}')
            runtime = {'dapper_root': str(root / 'dapper')}
            process = SimpleNamespace(stdin=io.BytesIO(), stdout=object(), stderr=object(),
                                      pid=999999, returncode=1, poll=lambda: 1)
            parser = SimpleNamespace(result={'is_error': True, 'subtype': 'error_max_turns', 'num_turns': 40, 'total_cost_usd': 1.26},
                                     runtime={}, finish=lambda: [])
            selector = Mock(); selector.get_map.return_value = {}
            user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(root))
            with patch.multiple(box_remote, BASE=root, STATE=state, OUTPUT=output, SECRETS=()), \
                 patch.object(box_remote.os, 'getuid', return_value=0), \
                 patch.object(box_remote.pwd, 'getpwnam', return_value=user), \
                 patch.object(box_remote, 'setup', return_value=(root, runtime, 'Offline prompt')), \
                 patch.object(box_remote, 'serve', return_value=Mock()), \
                 patch.object(box_remote, 'ClaudeStream', return_value=parser), \
                 patch.object(box_remote.subprocess, 'run', return_value=SimpleNamespace(stdout='test version')), \
                 patch.object(box_remote.subprocess, 'Popen', return_value=process), \
                 patch.object(box_remote.selectors, 'DefaultSelector', return_value=selector):
                box_remote.main()
            status = json.loads((state / 'status.json').read_text())
            completion = json.loads((state / 'runtime.json').read_text())['completion']
            self.assertEqual(status['status'], 'failed')
            self.assertIn('40-turn execution limit', status['reason'])
            self.assertIn('Retry', status['reason'])
            self.assertEqual(completion['provider_result_subtype'], 'error_max_turns')
            self.assertEqual(completion['turns_used'], 40)
            self.assertEqual(completion['cost_usd'], 1.26)


if __name__ == '__main__':
    unittest.main()
