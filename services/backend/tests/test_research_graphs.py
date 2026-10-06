"""Bounded external graph captures, with no live upstream or model calls."""
from copy import deepcopy
import io
import json
import threading
import unittest

from reveal_backend.auth import Problem
from reveal_backend.box_mcp import ENDPOINT, GRAPHS
from reveal_backend.evidence_package import canonical_json
from reveal_backend.research_data import CAPTURE_FORMAT
from reveal_backend.research_graphs import (GraphQueryService, _GraphMCPClient, _NoRedirect,
    catalog, definitions, validate)

ROW = {'s': {'type': 'uri', 'value': 'urn:gene:1'}, 'p': {'type': 'uri', 'value': 'urn:relation'},
       'o': {'type': 'literal', 'value': 'quoted β name', 'xml:lang': 'en'}}
ARGUMENTS = {'graph': 'prokn', 'subject': 'urn:gene:1', 'limit': 2}


class FakeDapper:
    def file(self, name, raw, media):
        import hashlib
        checksum = hashlib.sha256(raw).hexdigest()
        return {'id': 'dapper:File.'+checksum, 'filename': name, 'sha256': checksum, 'size_in_bytes': len(raw)}


class GraphCaptureTests(unittest.TestCase):
    def service(self, result, **kwargs):
        self.calls = []
        calls = self.calls
        class Client:
            server_info = {'name': 'upstream', 'version': 'protocol-server-version'}
            def call(self, tool, arguments):
                calls.append((tool, deepcopy(arguments)))
                return deepcopy(result)
        return GraphQueryService(client_factory=Client, **kwargs)

    def test_catalog_validation_and_selected_scope(self):
        self.assertEqual({x['name'] for x in definitions()}, {'get_schema', 'describe_kg', 'query_graph', 'sparql_query'})
        self.assertEqual(catalog(['prokn'])['graphs'], [{'id': 'prokn', 'named_graph': GRAPHS['prokn']}])
        self.assertEqual(catalog([])['graphs'], [])
        self.assertEqual(validate('query_graph', ARGUMENTS, ['prokn']), ARGUMENTS)
        for args in ({**ARGUMENTS, 'graph': 'other'}, {**ARGUMENTS, 'query': 'SELECT * {}'},
                     {**ARGUMENTS, 'endpoint': 'https://evil.test'}, {**ARGUMENTS, 'limit': True},
                     {**ARGUMENTS, 'limit': 101}, {'graph': 'prokn'},
                     {'graph': 'prokn', 'contains': 'gene'},
                     {**ARGUMENTS, 'subject': 'urn:x> } SERVICE <https://evil.test'}):
            with self.subTest(args=args), self.assertRaises(Problem): validate('query_graph', args)
        with self.assertRaises(Problem) as denied: validate('query_graph', ARGUMENTS, [])
        self.assertEqual(denied.exception.code, 'GRAPH_NOT_SELECTED')

    def test_triple_capture_retains_generated_query_terms_source_and_original_envelope(self):
        original = {'content': [], 'structuredContent': {'head': {'vars': ['s', 'p', 'o']}, 'results': {'bindings': [ROW]}}}
        service = self.service(original)
        capture = service.query('query_graph', ARGUMENTS, generation_id='a'*64, selected_graphs=['prokn'])
        self.assertEqual(capture.result['status'], 'complete')
        self.assertEqual(capture.result['items'], [ROW])
        self.assertEqual(capture.extra_files['mcp-tool-result.json'], canonical_json(original))
        self.assertEqual(self.calls[0][0], 'sparql_query')
        self.assertIn('GRAPH <'+GRAPHS['prokn']+'>', self.calls[0][1]['query'])
        self.assertTrue(self.calls[0][1]['query'].endswith('LIMIT 2'))
        self.assertEqual(capture.source['upstream_request']['arguments'], self.calls[0][1])
        self.assertEqual(capture.source['origin'], ENDPOINT)
        self.assertIsNone(capture.source['generation_id'])
        self.assertIsNone(capture.source['upstream_release'])
        envelope = json.loads(capture.raw)
        self.assertEqual(envelope['format'], CAPTURE_FORMAT)
        self.assertEqual(envelope['source_mode'], 'external_kg')
        materialized = capture.materialize(FakeDapper())
        self.assertEqual(len(materialized['eligible_source_ids']), 2)
        self.assertEqual(materialized['cfde_source_ids'], [])
        self.assertEqual(materialized['source_ref']['pointer'], '/result/items')

    def test_text_wrapped_rows_and_exact_literal_are_supported(self):
        original = {'content': [{'type': 'text', 'text': json.dumps({'rows': [ROW], 'row_count': 1})}]}
        service = self.service(original)
        args = {'graph': 'biomarkerkg', 'predicate': 'urn:label', 'literal': 'a "quoted" β name', 'limit': 1}
        capture = service.query('query_graph', args)
        self.assertEqual(capture.result['status'], 'partial')
        self.assertTrue(capture.result['truncated'])
        self.assertIn(json.dumps(args['literal']), self.calls[0][1]['query'])

    def test_custom_select_retains_projected_and_aggregate_bindings_without_inventing_triples(self):
        bindings = [{'gene': {'type': 'uri', 'value': 'urn:gene:1'},
                     'n': {'type': 'literal', 'datatype': 'http://www.w3.org/2001/XMLSchema#integer', 'value': '2'},
                     'score': 0.5}]
        original = {'structuredContent': {'results': {'bindings': bindings}}, 'content': []}
        args = {'graph': 'prokn', 'query': 'SELECT ?gene (COUNT(?object) AS ?n) WHERE { GRAPH <'+GRAPHS['prokn']+'> { ?gene ?predicate ?object } } GROUP BY ?gene', 'limit': 3}
        capture = self.service(original).query('sparql_query', args, selected_graphs=['prokn'])
        self.assertEqual(capture.result['items'], bindings)
        self.assertEqual(capture.result['query_form'], 'select_bindings')
        self.assertEqual(capture.result['status'], 'complete')
        self.assertNotIn('s', capture.result['items'][0])
        self.assertEqual(json.loads(capture.raw)['arguments']['query'], args['query'])
        self.assertIn('LIMIT', capture.source['upstream_request']['arguments']['query'].upper())
        self.assertEqual(capture.extra_files['mcp-tool-result.json'], canonical_json(original))
        self.assertTrue(capture.materialize(FakeDapper())['eligible_source_ids'])
        malformed = self.service({'structuredContent': {'rows': [{'gene': {'unexpected': 'object'}}]}})
        self.assertEqual(malformed.query('sparql_query', args).result['status'], 'source_unavailable')

    def test_invalid_custom_select_stays_correctable_and_never_calls_upstream(self):
        service = self.service({'structuredContent': {'rows': [ROW]}})
        with self.assertRaises(Problem) as failure:
            service.query('sparql_query', {'graph': 'prokn', 'query': 'SELECT * WHERE { ?s ?p ?o }'})
        self.assertEqual(failure.exception.code, 'INVALID_SPARQL')
        self.assertEqual(self.calls, [])

    def test_metadata_empty_errors_malformed_and_oversize_are_not_evidence(self):
        cases = [('get_schema', {'content': [{'type': 'text', 'text': 'Graph schema'}]}, 'metadata'),
                 ('describe_kg', {'structuredContent': {'description': 'scope'}}, 'metadata'),
                 ('query_graph', {'structuredContent': {'rows': []}}, 'empty'),
                 ('query_graph', {'isError': True, 'structuredContent': {'rows': [ROW]}}, 'source_unavailable'),
                 ('query_graph', {'content': [{'type': 'text', 'text': '{"error":"HTTP429","rows":[]}'}]}, 'source_unavailable'),
                 ('query_graph', {'structuredContent': {'rows': [{'s': 'urn:gene'}]}}, 'source_unavailable'),
                 ('query_graph', {'structuredContent': {'rows': [ROW]*3}}, 'source_unavailable'),
                 ('query_graph', {'structuredContent': {'rows': [ROW]}, 'content': [{'type': 'text', 'text': '{"rows":[]}'}]}, 'source_unavailable')]
        for operation, original, status in cases:
            with self.subTest(operation=operation, original=original):
                args = ARGUMENTS if operation == 'query_graph' else {'graph': 'prokn'}
                capture = self.service(original).query(operation, args)
                self.assertEqual(capture.result['status'], status)
                self.assertEqual(capture.materialize(FakeDapper())['eligible_source_ids'], [])
                self.assertEqual(capture.extra_files['mcp-tool-result.json'], canonical_json(original))
        huge = self.service({'content': [{'type': 'text', 'text': 'x'*6000}]}, max_bytes=4096).query('get_schema', {'graph': 'prokn'})
        self.assertEqual(huge.result['status'], 'source_unavailable')
        self.assertEqual(huge.extra_files, {})
        self.assertLessEqual(len(huge.raw), 4096)

    def test_timeout_late_result_never_mutates_capture_or_releases_capacity_early(self):
        started = threading.Event(); release = threading.Event(); finished = threading.Event()
        slots = threading.BoundedSemaphore(1)
        class Client:
            server_info = {}
            def call(self, *args):
                started.set(); release.wait(1); finished.set()
                return {'structuredContent': {'rows': [ROW]}}
        service = GraphQueryService(Client, timeout=.02, read_slots=slots)
        first = service.query('query_graph', ARGUMENTS)
        self.assertTrue(started.is_set())
        self.assertEqual(first.result['status'], 'source_unavailable')
        second = service.query('query_graph', ARGUMENTS)
        self.assertEqual(second.result['status'], 'source_unavailable')
        before = first.raw
        release.set(); self.assertTrue(finished.wait(1))
        self.assertEqual(first.raw, before)
        self.assertEqual(first.extra_files, {})

    def test_exceptions_do_not_expose_private_error_details(self):
        class Client:
            def call(self, *args): raise OSError('Authorization: Bearer TOPSECRET internal/file')
        capture = GraphQueryService(Client).query('query_graph', ARGUMENTS)
        self.assertNotIn(b'TOPSECRET', capture.raw)
        self.assertNotIn(b'internal/file', capture.raw)


class TransportTests(unittest.TestCase):
    def test_fixed_origin_no_private_headers_session_is_not_capture_identity(self):
        requests = []
        class Response(io.BytesIO):
            def __init__(self, data, url=ENDPOINT):
                super().__init__(data); self.url = url
                self.headers = {'Content-Type': 'application/json', 'Mcp-Session-Id': 'private-upstream-session'}
            def geturl(self): return self.url
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                body = json.loads(request.data)
                result = {'serverInfo': {'name': 'upstream'}} if body['method'] == 'initialize' else {'content': [], 'structuredContent': {'rows': [ROW]}}
                return Response(canonical_json({'jsonrpc': '2.0', 'id': body['id'], 'result': result}) if 'id' in body else b'')
        capture = GraphQueryService(lambda: _GraphMCPClient(timeout=1, max_bytes=10000, opener=Opener())).query('query_graph', ARGUMENTS)
        self.assertEqual(capture.result['status'], 'complete')
        self.assertTrue(all(request.full_url == ENDPOINT for request in requests))
        self.assertTrue(all('authorization' not in {key.lower() for key, _ in request.header_items()} for request in requests))
        self.assertNotIn(b'private-upstream-session', capture.raw)
        with self.assertRaises(ValueError): _NoRedirect().redirect_request(None, None, 302, 'moved', {}, 'https://evil.test')

    def test_changed_destination_identity_and_oversized_wire_fail_closed(self):
        for failure in ('destination', 'identity', 'size'):
            with self.subTest(failure=failure):
                class Response(io.BytesIO):
                    headers = {'Content-Type': 'application/json'}
                    def geturl(self): return 'https://evil.test' if failure == 'destination' else ENDPOINT
                class Opener:
                    def open(self, request, timeout):
                        return Response(b'x'*5000 if failure == 'size' else canonical_json({'id': -1, 'result': {}}))
                client = _GraphMCPClient(timeout=1, max_bytes=4096, opener=Opener())
                with self.assertRaises(ValueError): client.rpc('initialize')
