"""Anonymous reads retain exact bytes without private-work authority or leakage."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import research_public as public, research_execution, scientific_reuse as reuse, user_inputs
from reveal_backend.auth import Problem
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.repository import Repository
from reveal_backend.research_work import ResearchWorkService
from reveal_backend.research_data import CAPTURE_FORMAT, QueryCapture
import test_research_data as data_fixture
import test_scientific_reuse as science_fixture


class PublicCaptureTests(unittest.TestCase):
    def setUp(self):
        self.reference = data_fixture.ReferenceQueryTests()
        self.reference.setUp(); self.addCleanup(self.reference.doCleanups)
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = Repository(str(self.root/'application.sqlite')); self.repo.migrate()
        self.service = ResearchWorkService(self.repo, data_service=self.reference.service)
        for context in (patch.dict(os.environ, {'REVEAL_PUBLIC_API_URL': 'https://reveal.example',
                            'REVEAL_SMALL_PHENOTYPE_VERIFIED': 'false', 'REVEAL_PUBLIC_READS_PER_MINUTE': '600',
                            'REVEAL_PUBLIC_CAPTURE_MAX_BYTES': '256000000', 'REVEAL_PUBLIC_CAPTURE_MAX_RECORDS': '500'}),
                        patch('reveal_backend.user_inputs.artifacts_root', return_value=self.root/'artifacts'),
                        patch('reveal_backend.user_inputs.s3_enabled', return_value=False),
                        # Public factor capture now resolves a trusted Mechanism;
                        # use the real pinned identity runtime for both node types.
                        patch('reveal_backend.acceptance.public_runtime', return_value=data_fixture.SeedTests.runtime())):
            context.start(); self.addCleanup(context.stop)

    def query(self, name='get_factor', arguments=None, generation=data_fixture.GEN):
        return public.public_dispatch(self.service, name, {'reference_generation_id': generation,
            'arguments': arguments if arguments is not None else {'factor_id': data_fixture.FACTOR}}, rate_key='remote-fixture')

    def work(self, generation=data_fixture.GEN, owner='researcher', identity='work'):
        work = {'id': identity, 'research_request_id': identity+'-request', 'reference_generation_id': generation,
                'package_id': identity+'-package', 'state': 'ready', 'expires_at': '2099-01-01T00:00:00Z'}
        with self.repo.transaction() as tx:
            tx.put('principal', owner, owner, {'me': {'user_id': owner, 'workspace_expires_at': None}})
            tx.put('local_work', identity, owner, work)
            tx.put('request', work['research_request_id'], owner, {'question_id': 'dapper:KnowledgeGap.gap'})
            tx.put('research_package', work['package_id'], owner, {'artifacts': [], 'package': {
                'selection': {'knowledge_gap_id': 'dapper:KnowledgeGap.gap'}, 'dapper_context': {}, 'source_artifacts': {}}})
        return {'owner': owner, 'work': work}

    def attach(self, authority, capture_ids, key='attach'):
        prepared = public.prepare_capture_attachments(self.service, capture_ids, authority=authority)
        with self.repo.transaction() as tx:
            return public.attach_capture(self.service, tx, authority, capture_ids, key, prepared=prepared)

    def expire(self, identity):
        with self.repo.transaction() as tx:
            row = tx.get('public_capture', identity)
            row['data']['expires_at'] = '2020-01-01T00:00:00Z'
            tx.put('public_capture', identity, row['owner'], row['data'])

    def test_anonymous_generation_query_is_portable_cached_and_creates_no_private_work(self):
        result = self.query()
        self.assertEqual(result['result']['items'][0]['label'], 'retained')
        self.assertEqual(result['reference_generation_id'], data_fixture.GEN)
        self.assertEqual(result['source']['generation_id'], data_fixture.GEN)
        self.assertTrue(result['dapper_context']['files'])
        with patch.object(self.reference.service, 'query', side_effect=AssertionError('Do not requery a retained response')):
            self.assertEqual(result, self.query())
            replay = public.public_dispatch(self.service, 'get_public_capture', {'capture_id': result['capture_id']})
            self.assertEqual(replay, result)
        with self.repo.read_transaction() as tx:
            for kind in ('principal', 'local_work', 'request', 'research_pin', 'research_operation', 'evidence_receipt', 'research_artifact'):
                self.assertEqual(tx.list(kind), [], kind)
            self.assertEqual(len(tx.list('public_capture', public.OWNER)), 1)
        serialized = json.dumps(result)
        for private in ('"storage"', '"key"', '"owner_user_id"', str(self.root)):
            self.assertNotIn(private, serialized)
        artifact = next(item for item in result['artifacts'] if item['sha256'] == result['raw_sha256'])
        raw, descriptor = public.capture_artifact(self.service, result['capture_id'], artifact['sha256'])
        self.assertEqual(sha256(raw), result['raw_sha256'])
        self.assertEqual(json.loads(raw)['result'], result['result'])
        self.assertEqual(descriptor['authentication'], 'none')
        self.assertNotIn('storage', descriptor)

    def test_authenticated_attachment_preserves_exact_bytes_without_live_source_and_loads_normal_context(self):
        result = self.query(); authority = self.work()
        with sqlite3.connect(self.reference.path) as db:
            db.execute('DELETE FROM reference_generations WHERE generation_id=?', (data_fixture.GEN,))
        with patch.object(self.reference.service, 'query', side_effect=AssertionError('No changed-source requery')):
            attached = self.attach(authority, [result['capture_id']])
            self.assertEqual(self.attach(authority, [result['capture_id']]), attached)
        receipt_id = attached['receipts'][0]['receipt_id']
        package, files, _ = research_execution.load_context(self.service, authority['owner'], 'work', {'receipt_ids': [receipt_id]})
        self.assertEqual(package['dapper_context'], result['dapper_context'])
        self.assertEqual({sha256(raw) for raw in files.values()}, {item['sha256'] for item in result['artifacts']})
        self.assertEqual(package['validation_context']['eligible_source_ids'], [result['dapper_file_id']])
        self.assertEqual(package['validation_context']['cfde_source_ids'], [result['dapper_file_id']])
        self.expire(result['capture_id'])
        self.assertEqual(self.attach(authority, [result['capture_id']], 'later-idempotency-key'), attached)
        package_after, files_after, _ = research_execution.load_context(self.service, authority['owner'], 'work', {'receipt_ids': [receipt_id]})
        self.assertEqual(files_after, files)
        self.assertEqual(package_after, package)
        with self.assertRaises(Problem) as failure:
            public.public_dispatch(self.service, 'get_public_capture', {'capture_id': result['capture_id']})
        self.assertEqual(failure.exception.code, 'PUBLIC_CAPTURE_EXPIRED')

    def test_attachment_enforces_generation_owner_and_atomic_batch(self):
        result = self.query(); other = self.query(generation=data_fixture.OTHER)
        authority = self.work()
        with self.assertRaises(Problem) as failure:
            self.attach(authority, [result['capture_id'], other['capture_id']])
        self.assertEqual(failure.exception.code, 'CAPTURE_GENERATION_MISMATCH')
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('evidence_receipt'), [])
            self.assertEqual(tx.list('research_artifact'), [])
        with self.assertRaises(Problem):
            self.attach({**authority, 'owner': 'foreign'}, [result['capture_id']])
        self.expire(result['capture_id'])
        with self.assertRaises(Problem) as failure:
            self.attach(authority, [result['capture_id']])
        self.assertEqual(failure.exception.status, 410)

    def test_client_hash_or_tampered_manifest_cannot_become_evidence(self):
        authority = self.work()
        with self.assertRaises(Problem) as failure:
            self.attach(authority, ['f' * 64])
        self.assertEqual(failure.exception.status, 404)
        result = self.query()
        with self.repo.transaction() as tx:
            row = tx.get('public_capture', result['capture_id'])
            row['data']['manifest']['context']['eligible_source_ids'] = ['dapper:File.fake']
            tx.put('public_capture', result['capture_id'], row['owner'], row['data'])
        with self.assertRaises(Problem) as failure:
            self.attach(authority, [result['capture_id']])
        self.assertEqual(failure.exception.code, 'PUBLIC_CAPTURE_INTEGRITY')

    def test_changed_storage_bytes_are_rejected_before_attachment_or_download(self):
        result = self.query(); authority = self.work()
        with self.repo.read_transaction() as tx:
            record = tx.get('public_capture', result['capture_id'])['data']
        reference = next(iter(record['storage'].values()))
        Path(reference['key']).write_bytes(b'tampered bytes')
        for action in (lambda: self.attach(authority, [result['capture_id']]),
                       lambda: public.capture_artifact(self.service, result['capture_id'], reference['sha256'])):
            with self.assertRaises(Problem) as failure:
                action()
            self.assertEqual(failure.exception.code, 'PUBLIC_CAPTURE_INTEGRITY')

    def test_missing_and_partial_data_retain_coverage_without_fabricating_evidence(self):
        empty = self.query('search_genes', {'q': 'NO_SUCH_GENE'})
        partial = self.query('get_factor_loadings', {'factor_id': data_fixture.FACTOR, 'limit': 1})
        self.assertEqual(empty['result']['status'], 'empty')
        self.assertEqual(partial['result']['status'], 'partial')
        self.assertTrue(partial['result']['next_cursor'])
        attached = self.attach(self.work(), [empty['capture_id'], partial['capture_id']])
        with self.repo.read_transaction() as tx:
            first = tx.get('evidence_receipt', attached['receipts'][0]['receipt_id'])['data']
            second = tx.get('evidence_receipt', attached['receipts'][1]['receipt_id'])['data']
        self.assertEqual(first['context']['eligible_source_ids'], [])
        self.assertEqual(second['context']['eligible_source_ids'], [partial['dapper_file_id']])
        self.assertTrue(second['result']['truncated'])

    def test_phenotype_calls_preserve_unverified_deployment_gate_and_do_not_fetch(self):
        with patch.object(public.SmallModelBioIndex, '_fetch', side_effect=AssertionError('No upstream access without verified gate')):
            result = self.query('get_pigean_gene_phenotype', {'phenotype_id': 'T2D'})
        self.assertEqual(result['source_mode'], 'bioindex_small_phenotype')
        self.assertEqual(result['source']['model'], 'small')
        self.assertEqual(result['source']['sigma'], 2)
        self.assertIsNone(result['source']['generation_id'])
        self.assertEqual(result['result']['status'], 'source_unavailable')
        with self.assertRaises(Problem):
            self.query('get_pigean_gene_phenotype', {'phenotype_id': 'T2D', 'model': 'large'})

    def test_storage_quota_is_reserved_before_blob_io_and_expiry_cannot_bypass_it(self):
        with patch.dict(os.environ, {'REVEAL_PUBLIC_CAPTURE_MAX_BYTES': '1'}):
            with patch.object(user_inputs, 'retain', side_effect=AssertionError('Quota must precede storage')):
                with self.assertRaises(Problem) as failure:
                    self.query()
            self.assertEqual(failure.exception.code, 'PUBLIC_CAPTURE_QUOTA')
        first = self.query(); self.expire(first['capture_id'])
        with patch.dict(os.environ, {'REVEAL_PUBLIC_CAPTURE_MAX_RECORDS': '1'}):
            with self.assertRaises(Problem) as failure:
                self.query('search_genes', {'q': 'GENE'})
            self.assertEqual(failure.exception.code, 'PUBLIC_CAPTURE_QUOTA')

    def test_anonymous_global_rate_limit_is_durable_across_service_instances(self):
        with patch.dict(os.environ, {'REVEAL_PUBLIC_READS_PER_MINUTE': '1'}):
            self.query()
            second = ResearchWorkService(self.repo, data_service=self.reference.service)
            with self.assertRaises(Problem) as failure:
                public.public_dispatch(second, 'list_data_operations', {'reference_generation_id': data_fixture.GEN})
            self.assertEqual(failure.exception.code, 'PUBLIC_RESEARCH_RATE_LIMIT')

    def test_prepared_attachment_is_rechecked_under_commit_fence(self):
        result = self.query(); authority = self.work()
        prepared = public.prepare_capture_attachments(self.service, [result['capture_id']])
        self.expire(result['capture_id'])
        with self.repo.transaction() as tx:
            with self.assertRaises(Problem) as failure:
                public.attach_capture(self.service, tx, authority, [result['capture_id']], 'key', prepared=prepared)
            self.assertEqual(failure.exception.code, 'PUBLIC_CAPTURE_EXPIRED')


class PublicGraphCaptureTests(unittest.TestCase):
    def setUp(self):
        self.base = PublicCaptureTests(); self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.repo, self.service = self.base.repo, self.base.service
        self.status = 'complete'; self.observation = 1
        self.original_envelope = {'structuredContent': {'results': {'bindings': [
            {'s': {'type': 'uri', 'value': 'urn:gene:SHH'},
             'p': {'type': 'uri', 'value': 'urn:related-to'},
             'o': {'type': 'literal', 'value': 'exact upstream observation', 'xml:lang': 'en'}}]}}}
        self.adapter_class = public.research_graphs.GraphQueryService
        reader = patch.object(public.research_graphs, 'GraphQueryService')
        self.reader = reader.start(); self.addCleanup(reader.stop)
        self.reader.return_value.query.side_effect = self.answer

    def answer(self, operation, arguments, *, generation_id=None):
        self.assertIsNone(generation_id)
        source = {'origin': 'https://apps.okn.us/okn-mcp-dev/mcp', 'graph_id': arguments['graph'],
            'generation_id': None, 'upstream_release': None, 'observed_at': str(self.observation),
            'upstream_request': {'query': 'exact bounded fixture SELECT'}}
        eligible = self.status in ('complete', 'partial')
        result = {'status': self.status, 'items': self.original_envelope['structuredContent']['results']['bindings'] if eligible else [],
            'complete': self.status == 'complete', 'truncated': self.status == 'partial'}
        raw = canonical_json({'format': CAPTURE_FORMAT, 'reader_version': public.research_graphs.VERSION,
            'source_mode': 'external_kg', 'source': source, 'operation': operation, 'arguments': arguments, 'result': result})
        return QueryCapture(result, raw, 'external_kg', source,
                            {'mcp-tool-result.json': canonical_json(self.original_envelope)})

    def query(self, name='query_graph', graph='prokn', **arguments):
        if name == 'query_graph' and not arguments:
            arguments = {'subject': 'urn:gene:SHH', 'limit': 10}
        return public.public_dispatch(self.service, name, {'graph': graph, **arguments}, rate_key='graph-fixture')

    def work(self, selected, generation=data_fixture.GEN, identity='graph-work'):
        authority = self.base.work(generation=generation, identity=identity)
        with self.repo.transaction() as tx:
            row = tx.get('request', authority['work']['research_request_id'])
            row['data']['composer'] = {'selected_kgs': selected}
            tx.put('request', authority['work']['research_request_id'], row['owner'], row['data'])
        return authority

    def test_public_catalog_and_schema_share_graph_adapter_without_private_or_imported_scope(self):
        catalog = public.public_dispatch(self.service, 'list_knowledge_graphs', {})
        self.assertEqual(catalog, public.research_graphs.catalog())
        self.assertEqual(catalog['source_mode'], 'external_kg')
        self.assertEqual({graph['id'] for graph in catalog['graphs']}, {'prokn', 'biomarkerkg'})
        tools = {tool['name']: tool for tool in public.definitions()}
        for name, operation in public.research_graphs.OPERATIONS.items():
            self.assertEqual(tools[name]['inputSchema'], operation['inputSchema'])
            self.assertTrue(tools[name]['annotations']['openWorldHint'])
        self.reader.assert_not_called()
        for args in ({'graph': 'private-unconnected'}, {'graph': 'prokn', 'reference_generation_id': data_fixture.GEN}):
            with self.assertRaises(Problem): public.public_dispatch(self.service, 'get_schema', args)
        self.reader.assert_not_called()

    def test_anonymous_graph_capture_retains_envelope_without_work_or_generation_lookup(self):
        with patch.object(self.base.reference.service, 'query', side_effect=AssertionError('No imported query')):
            with patch.object(self.base.reference.service, 'catalog', side_effect=AssertionError('No imported generation required')):
                result = self.query()
        self.assertIsNone(result['reference_generation_id'])
        self.assertEqual(result['source_mode'], 'external_kg')
        self.assertEqual(result['source']['graph_id'], 'prokn')
        self.assertIsNone(result['source']['generation_id'])
        raw = [public.capture_artifact(self.service, result['capture_id'], item['sha256'])[0] for item in result['artifacts']]
        self.assertIn(canonical_json(self.original_envelope), raw)
        self.assertIn('selected in the frozen research request', result['attachment_policy'])
        serialized = json.dumps(result)
        for private in ('"storage"', '"owner_user_id"', '"local_work_id"', str(self.base.root)):
            self.assertNotIn(private, serialized)
        with self.repo.read_transaction() as tx:
            for kind in ('principal', 'local_work', 'request', 'research_access', 'research_pin', 'research_operation', 'evidence_receipt'):
                self.assertEqual(tx.list(kind), [])

    def test_real_graph_adapter_capture_materializes_through_public_retention(self):
        calls = []; envelope = self.original_envelope
        class Client:
            server_info = {'name': 'fixture-graph', 'version': 'fixture-release'}
            def call(self, name, arguments):
                calls.append((name, arguments)); return deepcopy(envelope)
        self.reader.return_value = self.adapter_class(client_factory=Client)
        result = self.query()
        self.assertEqual(result['result']['status'], 'complete')
        self.assertEqual(result['result']['items'], envelope['structuredContent']['results']['bindings'])
        self.assertEqual(calls[0][0], 'sparql_query')
        self.assertEqual(result['source']['upstream_request']['arguments'], calls[0][1])
        self.assertIsNone(result['source']['generation_id'])
        attached = self.base.attach(self.work(['prokn']), [result['capture_id']])
        with self.repo.read_transaction() as tx:
            receipt = tx.get('evidence_receipt', attached['receipts'][0]['receipt_id'])['data']
            self.assertEqual(receipt['source'], result['source'])
            self.assertTrue(receipt['context']['eligible_source_ids'])

    def test_selected_graph_attaches_exact_bytes_across_imported_generations_without_requery(self):
        result = self.query()
        authority = self.work(['prokn'], generation=data_fixture.OTHER)
        with patch.object(self.reader.return_value, 'query', side_effect=AssertionError('Never requery an attached observation')):
            attached = self.base.attach(authority, [result['capture_id']])
        receipt_id = attached['receipts'][0]['receipt_id']
        package, files, _ = research_execution.load_context(self.service, authority['owner'], authority['work']['id'], {'receipt_ids': [receipt_id]})
        self.assertIn(canonical_json(self.original_envelope), files.values())
        self.assertEqual(package['validation_context']['cfde_source_ids'], [])
        self.assertEqual(set(package['validation_context']['eligible_source_ids']), {file['id'] for file in result['dapper_context']['files']})
        with self.repo.read_transaction() as tx:
            receipt = tx.get('evidence_receipt', receipt_id)['data']
            self.assertEqual(receipt['source'], result['source'])
            self.assertEqual(receipt['source_mode'], 'external_kg')
        self.base.expire(result['capture_id'])
        self.assertEqual(self.base.attach(authority, [result['capture_id']], 'retained-replay'), attached)

    def test_unselected_graph_rejects_entire_attachment_batch_using_frozen_request(self):
        selected = self.query(graph='prokn'); unselected = self.query(graph='biomarkerkg')
        authority = self.work(['prokn'])
        authority['work']['composer'] = {'selected_kgs': ['prokn', 'biomarkerkg']}
        with self.assertRaises(Problem) as failure:
            self.base.attach(authority, [selected['capture_id'], unselected['capture_id']])
        self.assertEqual(failure.exception.code, 'GRAPH_NOT_SELECTED')
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('evidence_receipt'), [])
            self.assertEqual(tx.list('research_artifact'), [])
        no_graphs = self.work([], identity='no-graphs')
        with self.assertRaises(Problem): self.base.attach(no_graphs, [selected['capture_id']])

    def test_schema_diagnostics_errors_and_empty_queries_are_not_evidence(self):
        authority = self.work(['prokn']); captures = []
        for name, status, args in [('get_schema', 'metadata', {}), ('describe_kg', 'metadata', {}),
                ('query_graph', 'empty', {'subject': 'urn:empty'}),
                ('query_graph', 'source_unavailable', {'subject': 'urn:unavailable'})]:
            self.status = status
            capture = self.query(name, **args); captures.append(capture['capture_id'])
            self.assertEqual(capture['result']['status'], status)
        attached = self.base.attach(authority, captures)
        with self.repo.read_transaction() as tx:
            for item in attached['receipts']:
                context = tx.get('evidence_receipt', item['receipt_id'])['data']['context']
                self.assertEqual(context['eligible_source_ids'], [])
                self.assertEqual(context['cfde_source_ids'], [])

    def test_live_graph_query_cache_is_short_but_exact_capture_remains_replayable(self):
        first = self.query()
        self.assertEqual(self.query(), first)
        self.assertEqual(self.reader.return_value.query.call_count, 1)
        with self.repo.transaction() as tx:
            caches = tx.list('public_capture_cache', public.OWNER)
            self.assertEqual(len(caches), 1)
            cache = caches[0]
            from datetime import datetime, timezone
            remaining = (datetime.fromisoformat(cache['data']['expires_at'].replace('Z', '+00:00'))-datetime.now(timezone.utc)).total_seconds()
            self.assertLessEqual(remaining, 300); self.assertGreater(remaining, 290)
            cache['data']['expires_at'] = '2020-01-01T00:00:00Z'
            tx.put('public_capture_cache', cache['id'], cache['owner'], cache['data'])
        self.observation = 2
        second = self.query()
        self.assertNotEqual(first['capture_id'], second['capture_id'])
        self.assertEqual(self.reader.return_value.query.call_count, 2)
        self.assertEqual(public.public_dispatch(self.service, 'get_public_capture', {'capture_id': first['capture_id']}), first)


class PublicScienceTests(unittest.TestCase):
    def setUp(self):
        self.science = science_fixture.ScientificReuseTests()
        self.science.setUp(); self.addCleanup(self.science.doCleanups)
        self.repo = self.science.repo

    def test_public_search_never_scans_private_documents_shares_or_dependencies(self):
        with self.repo.transaction() as tx:
            original = tx.list
            seen = []
            def listing(kind, owner=None):
                seen.append(kind)
                self.assertEqual(kind, 'publication')
                return original(kind, owner)
            with patch.object(tx, 'list', side_effect=listing):
                self.assertEqual(reuse.public_search(tx, 'claim')['total'], 0)
            self.science.publish(tx)
            with patch.object(tx, 'list', side_effect=listing):
                result = reuse.public_search(tx, 'claim')
                self.assertEqual(result['total'], 1)
                selected = result['items'][0]
                detail = reuse.public_get_object(tx, selected)
            self.assertEqual(detail['dapper_context']['claims'], self.science.doc['claims'])
            self.assertNotIn('scientific_accounts', detail['dapper_context'])
            self.assertEqual(detail['scope'], 'public')
            self.assertEqual(detail['artifacts'][0]['file']['id'], science_fixture.FILE)
            self.assertNotIn('path', json.dumps(detail))
            self.assertNotIn('storage', json.dumps(detail))
            self.assertEqual(set(seen), {'publication'})

    def test_public_selection_requires_exact_payload_and_current_publication(self):
        with self.repo.transaction() as tx:
            self.science.publish(tx)
            selected = reuse.public_search(tx, 'claim')['items'][0]
            for changed in ({**selected, 'payload_sha256': '0' * 64}, {**selected, 'publication_version': 2},
                            {**selected, 'source_kind': 'owned_document'}):
                with self.assertRaises(Problem):
                    reuse.public_get_object(tx, changed)
            tx.put('publication', 'publication', 'author', {'visibility': 'private', 'version': 2, 'snapshot_id': None})
            self.assertEqual(reuse.public_search(tx, 'claim')['total'], 0)
            with self.assertRaises(Problem):
                reuse.public_get_object(tx, selected)

    def test_anonymous_selection_can_be_reused_only_through_authenticated_receipt(self):
        with self.repo.transaction() as tx:
            self.science.publish(tx)
            selection = reuse.public_search(tx, 'claim')['items'][0]
            receipt = reuse.create_receipt(tx, 'reader', 'request', [selection], 'reuse')
            self.assertEqual(receipt['selections'][0]['selection']['payload_sha256'], selection['payload_sha256'])
            self.assertEqual(receipt['selections'][0]['eligible_source_ids'], [science_fixture.FILE])
            self.assertEqual(tx.list('grant', 'reader'), [])
            tx.put('publication', 'publication', 'author', {'visibility': 'private', 'version': 2, 'snapshot_id': None})
            with self.assertRaises(Problem) as failure:
                reuse.resolve_receipts(tx, 'reader', 'request', [receipt['id']])
            self.assertEqual(failure.exception.code, 'REUSE_AUTHORITY_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
