"""Source-based collection fixtures and deterministic replay/validation invariants."""
from copy import deepcopy
import gzip
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from reveal_backend.evidence_collector import CaptureStore, HttpCaptureClient, collect_package, parse_factor
from reveal_backend.evidence_package import (DapperRuntime, EvidenceBuildError, build_package, canonical_json,
                                            decode, load_build_input, pointer, sha256)

ROOT = Path(__file__).resolve().parents[3]
FACTOR = 'factor:portal:CADinT2D:cfde-inc-v2:Factor1'
GAP = 'cad_pgsxc_reverse_causation'


class FixtureClient:
    """Test-only transport: real captured rows, simulated empty factor expansion."""
    def __init__(self, store, two_anchors=False):
        self.store, self.calls, self.two_anchors = store, [], two_anchors

    def request(self, name, url, payload=None):
        self.calls.append((url, deepcopy(payload)))
        if '/catalog?' in url:
            capture = decode((ROOT / 'data/interactive/2026-09-24/cad-in-t2d-factor-gene.json').read_bytes())
            body = {'items': capture['request']['anchor_items']}
        elif url.endswith('/connections'):
            target = payload['target_type']
            body = {'candidates': [], 'graph': {'nodes': [], 'edges': []}, 'candidate_count': 0} if target == 'factor' else decode(
                (ROOT / f'data/interactive/2026-09-24/cad-in-t2d-factor-{target}.json').read_bytes())['response']
        elif url.endswith('/contextual-edges'):
            all_edges = decode((ROOT / 'data/interactive/2026-09-25/cad-in-t2d-contextual-edges.json').read_bytes())['response']['edges']
            body = {'edges': [e for e in all_edges if {e['source'], e['target']} <= set(payload['node_ids'])]}
        else:
            index = urlparse(url).path.rsplit('/', 1)[1]
            query = parse_qs(urlparse(url).query)['q'][0].split(',')
            if index == 'pigean-factor':
                row = decode((ROOT / 'data/cfde/cad-in-t2d/pigean-gene-factor.response.json').read_bytes())['data'][0]
                body = {'index': index, 'q': query, 'data': [{k: row[k] for k in ['factor', 'label', 'phenotype', 'gene_set_size', 'trait_group']}]}
            else:
                kind = 'gene-set' if 'gene-set' in index else 'gene'
                body = decode((ROOT / f'data/cfde/cad-in-t2d/pigean-{kind}-factor.response.json').read_bytes())
                body['index'], body['q'] = index, query
        if self.two_anchors:
            # Synthetic second mechanism sharing candidates; never written as real scientific data.
            second = FACTOR.rsplit(':', 1)[0] + ':Factor2'
            def second_edges(edges):
                copied = json.loads(json.dumps(edges).replace(FACTOR, second))
                for edge in copied: edge['id'] = sha256((edge['id'] + '-second').encode())[:16]
                return copied
            if '/catalog?' in url:
                item = deepcopy(body['items'][0]); item['node_id'] = second; body['items'].append(item)
            elif url.endswith('/connections'):
                for candidate in body['candidates']:
                    candidate['edges'] += second_edges(candidate['edges'])
                body['graph']['edges'] += second_edges(body['graph']['edges'])
            elif url.endswith('/contextual-edges'):
                body['edges'] += second_edges(body['edges'])
            elif index == 'pigean-factor':
                row = deepcopy(body['data'][0]); row['factor'] = 'Factor2'; body['data'].append(row)
            elif index.endswith('-factor') and query[-1] == 'Factor2':
                for row in body['data']: row['factor'] = 'Factor2'
        body_bytes = canonical_json(body)
        key = name + '.attempt-1'
        wrapper = {'url': url, 'method': 'POST' if payload is not None else 'GET', 'request': payload,
                   'retrieved_at': '2026-09-25T12:00:00+00:00', 'status': 200,
                   'body_sha256': sha256(body_bytes), 'body_bytes': len(body_bytes), 'response': body}
        self.store.add(key + '.body', body_bytes, 'json', origin=url)
        self.store.add(key, canonical_json(wrapper), 'json', origin=url)
        return key, wrapper


class EvidencePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.runtime = DapperRuntime(ROOT / 'data/dapper/2026-09-24-v8')
        source = cls.root / 'dismech'
        (source / 'kb/disorders').mkdir(parents=True)
        (source / 'src/dismech/schema').mkdir(parents=True)
        shutil.copyfile(ROOT / 'docs/examples/evidence-package-cad/sources/Coronary_Artery_Disease.yaml', source / 'kb/disorders/Coronary_Artery_Disease.yaml')
        (source / 'src/dismech/schema/dismech.yaml').write_text(
            'prefixes:\n  GO: http://purl.obolibrary.org/obo/GO_\n  ECTO: http://purl.obolibrary.org/obo/ECTO_\n')
        # Small test import with the same catalog projection, for all captured candidate sets.
        imported = cls.root / 'genesets'; imported.mkdir()
        activity_bytes = (ROOT / 'data/cfde-genesets/2026-09-24/activity.json').read_bytes()
        activity = decode(activity_bytes); (imported / 'activity.json').write_bytes(activity_bytes)
        rows = decode((ROOT / 'data/cfde/cad-in-t2d/pigean-gene-set-factor.response.json').read_bytes())['data']
        with gzip.open(imported / 'records.jsonl.gz', 'wt') as stream:
            for row in rows:
                key = row['gene_set']; alias = 'gene_set:' + key
                node = {'name': key, 'member_type': 'gene', 'term': key, 'alternate_identifier': [alias],
                        'was_generated_by': activity['id'], 'was_derived_from': ['https://cfde-dev.hugeampkpnbi.org/api/bio/keys/pigean-gene-set/2']}
                node['id'] = cls.runtime.compute_id(node, 'GeneSet', cls.runtime.schema)
                stream.write(json.dumps({'model': 'cfde-inc-v2', 'node_id': alias, 'gene_set': node}) + '\n')
        (imported / 'manifest.json').write_bytes(canonical_json({'complete': True, 'model': 'cfde-inc-v2',
             'expected_rows': len(rows), 'encoded_rows': len(rows), 'import_id': 'test-import',
             'records_sha256': sha256((imported / 'records.jsonl.gz').read_bytes()), 'activity_sha256': sha256(activity_bytes)}))
        clients = []
        def factory(store):
            client = FixtureClient(store); clients.append(client); return client
        cls.built = collect_package(gap_id=GAP, factor_ids=[FACTOR], output=cls.root / 'capture', dapper=cls.runtime,
                                   project_root=ROOT, dismech_source=source,
                                   dismech_index=ROOT / 'data/dismech-gaps/2026-09-24', geneset_import=imported,
                                   limit=8, client_factory=factory)
        cls.calls = clients[0].calls
        cls.spec, cls.blobs = load_build_input(cls.root / 'capture/build-input.json', cls.root / 'capture')
        cls.source, cls.imported = source, imported

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def build(self, spec=None, blobs=None):
        return build_package(spec or self.spec, blobs or self.blobs, self.runtime)

    def test_collector_calls_all_sources_and_freezes_same_seed_set(self):
        self.assertEqual(len(self.calls), 11)
        expansions = [p for u, p in self.calls if u.endswith('/connections')]
        self.assertEqual({p['target_type'] for p in expansions}, {'gene', 'gene_set', 'trait', 'factor'})
        for request in expansions:
            self.assertEqual([n['node_id'] for n in request['anchor_items']], [FACTOR])
            self.assertEqual(request['exclude_node_ids'], [FACTOR])
            self.assertEqual(request['context'], '')
        self.assertTrue(any('pigean-gene-phenotype?' in u for u, _ in self.calls))
        self.assertTrue(any('pigean-gene-set-phenotype?' in u for u, _ in self.calls))

    def test_real_values_contextual_dedup_and_no_invented_similarity(self):
        packet = self.built.package
        shh = packet['pigean']['mechanisms'][FACTOR]['gene_loadings']['items']['gene:SHH']
        self.assertEqual(shh['factor_value'], .5042)
        self.assertEqual(shh['interactive_observations'][0]['raw_score'], .5041999816894531)
        self.assertEqual(packet['coverage']['retained_edges'], 16)
        self.assertEqual(packet['pigean']['contextual_relationships']['new_unique_edges'], 0)
        for edge in packet['pigean']['graph']['edges']: self.assertGreaterEqual(len(edge['source_refs']), 3)
        for mechanism in packet['dismech']['mechanisms'].values():
            self.assertIsNone(mechanism['associated_eaggl_mechanisms'][FACTOR]['semantic_similarity'])
        self.assertEqual(packet['dismech']['knowledge_gap']['dapper_id'], 'dapper:KnowledgeGap.zNV20nhHamt-a4CeAktQQPoAivOJe6xk')

    def test_multiple_anchors_preserve_shared_gene_observations(self):
        clients = []
        def factory(store):
            client = FixtureClient(store, two_anchors=True); clients.append(client); return client
        second = FACTOR.rsplit(':', 1)[0] + ':Factor2'
        packet = collect_package(gap_id=GAP, factor_ids=[second, FACTOR], output=self.root / 'two-anchors', dapper=self.runtime,
                                 project_root=ROOT, dismech_source=self.source, dismech_index=ROOT / 'data/dismech-gaps/2026-09-24',
                                 geneset_import=self.imported, limit=8, client_factory=factory).package
        self.assertEqual(len(clients[0].calls), 13)
        self.assertEqual(packet['coverage']['retained_nodes'], 18)
        self.assertEqual(packet['coverage']['retained_edges'], 32)
        for factor in [FACTOR, second]:
            self.assertIn('gene:SHH', packet['pigean']['mechanisms'][factor]['gene_loadings']['items'])
        observations = packet['pigean']['traits']['trait:portal:CADinT2D']['gene_associations']['items']['gene:SHH']['observations']
        self.assertEqual(len(observations), 3)  # Two mechanism queries and one trait query, never overwritten.
        for url, request in clients[0].calls:
            if url.endswith('/connections'):
                self.assertEqual([n['node_id'] for n in request['anchor_items']], [FACTOR, second])

    def test_replay_bytes_identical_with_reordered_inputs_and_relocation(self):
        spec = deepcopy(self.spec)
        spec['artifacts'] = dict(reversed(list(spec['artifacts'].items())))
        spec['captures']['bioindex'].reverse()
        for value in spec['dapper_context'].values():
            if isinstance(value, list): value.reverse()
        for descriptor in spec['artifacts'].values(): descriptor['path'] = 'ignored-transport-location'
        original = deepcopy(spec)
        with patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network')):
            rebuilt = self.build(spec)
        self.assertEqual(self.built.files, rebuilt.files)
        self.assertEqual(spec, original)
        destination = self.root / 'replayed'
        rebuilt.write(destination); rebuilt.write(destination)
        self.assertEqual((destination / 'evidence-package.json').read_bytes(), self.built.files['evidence-package.json'])
        (destination / 'unexpected').write_text('conflict')
        with self.assertRaisesRegex(EvidenceBuildError, 'different content'): rebuilt.write(destination)

    def test_changed_source_bytes_rejected_before_output(self):
        blobs = dict(self.blobs); key = next(iter(blobs)); blobs[key] += b' '
        with self.assertRaisesRegex(EvidenceBuildError, 'checksum mismatch'): self.build(blobs=blobs)

    def test_mismatched_dapper_identity_and_missing_dependency(self):
        spec = deepcopy(self.spec); spec['dapper_context']['knowledge_gaps'][0]['text'] = 'changed question'
        with self.assertRaisesRegex(EvidenceBuildError, 'identity mismatch'): self.build(spec)
        spec = deepcopy(self.spec); spec['dapper_context']['activities'] = []
        with self.assertRaisesRegex(EvidenceBuildError, 'Missing DAPPER dependency'): self.build(spec)

    def test_unknown_prefix_and_seed_scope_rejected(self):
        spec = deepcopy(self.spec); spec['prefixes']['dapper'] = 'https://example.org/incorrect/'
        with self.assertRaisesRegex(EvidenceBuildError, 'Invalid prefix map'): self.build(spec)
        spec = deepcopy(self.spec); spec['selection']['eaggl_mechanism_ids'] = []
        with self.assertRaisesRegex(EvidenceBuildError, 'nonempty'): self.build(spec)
        spec = deepcopy(self.spec); spec['selection']['eaggl_mechanism_ids'] *= 2
        with self.assertRaisesRegex(EvidenceBuildError, 'Duplicate'): self.build(spec)

    def test_empty_vs_missing_query_and_explicit_partial_policy(self):
        self.assertEqual(self.built.package['coverage']['queries']['factor'], 'empty')
        spec = deepcopy(self.spec); spec['captures']['connections']['factor'] = {'status': 'not_captured'}
        with self.assertRaisesRegex(EvidenceBuildError, 'Required query unavailable'): self.build(spec)
        spec['policy']['allow_incomplete_capture'] = True
        packet = self.build(spec).package
        self.assertFalse(packet['readiness']['input_capture_complete'])
        self.assertIn('factor:not_captured', packet['readiness']['capture_blockers'])

    def test_budget_clipping_preserves_anchors_and_records_omissions(self):
        spec = deepcopy(self.spec); spec['policy']['retain_node_ids'] = None; spec['policy']['max_nodes'] = 4; spec['policy']['max_edges'] = 2
        result = self.build(spec).package
        self.assertEqual(result['coverage']['retained_nodes'], 4)
        self.assertEqual(result['coverage']['retained_edges'], 2)
        self.assertIn(FACTOR, result['pigean']['graph']['node_ids'])
        self.assertEqual(len(result['coverage']['omitted_node_ids']), 13)
        spec['policy']['max_package_bytes'] = 100
        with self.assertRaisesRegex(EvidenceBuildError, 'byte budget'): self.build(spec)

    def test_conflicting_duplicate_edge_and_wrong_query_model_rejected(self):
        for mutation, message in [('edge', 'Conflicting duplicate edge'), ('model', 'target/model mismatch')]:
            spec, blobs = deepcopy(self.spec), dict(self.blobs)
            key = spec['captures']['connections']['gene']['artifact_id']; payload = decode(blobs[key])
            if mutation == 'edge':
                payload['response']['graph']['edges'][0]['raw_score'] = .9
                blobs[key + '.body'] = canonical_json(payload['response'])
                payload['body_sha256'] = spec['artifacts'][key + '.body']['sha256'] = sha256(blobs[key + '.body'])
                payload['body_bytes'] = len(blobs[key + '.body'])
            else: payload['request']['model'] = 'different-model'
            blobs[key] = canonical_json(payload); spec['artifacts'][key]['sha256'] = sha256(blobs[key])
            with self.subTest(mutation=mutation), self.assertRaisesRegex(EvidenceBuildError, message): self.build(spec, blobs)

    def test_http_projection_must_match_exact_saved_body(self):
        spec, blobs = deepcopy(self.spec), dict(self.blobs)
        key = spec['captures']['connections']['gene']['artifact_id']; payload = decode(blobs[key])
        payload['response']['graph']['edges'][0]['raw_score'] = .9
        blobs[key] = canonical_json(payload); spec['artifacts'][key]['sha256'] = sha256(blobs[key])
        with self.assertRaisesRegex(EvidenceBuildError, 'differs from exact body'): self.build(spec, blobs)

    def test_missing_bioindex_query_is_not_complete(self):
        spec = deepcopy(self.spec); spec['captures']['bioindex'].pop()
        with self.assertRaisesRegex(EvidenceBuildError, 'Required BioIndex query unavailable'): self.build(spec)
        spec['policy']['allow_incomplete_capture'] = True
        self.assertFalse(self.build(spec).package['readiness']['input_capture_complete'])

    def test_json_yaml_duplicate_keys_nan_and_bad_pointers(self):
        for data, format in [(b'{"a":1,"a":2}', 'json'), (b'a: 1\na: 2\n', 'yaml'), (b'{"a":NaN}', 'json')]:
            with self.assertRaises(EvidenceBuildError): decode(data, format)
        self.assertEqual(pointer({'a/b': {'~key': [7]}}, '/a~1b/~0key/0'), 7)
        for p in ['/a/-1', '/a/00', '/a/2', '/a/~2']:
            with self.assertRaises(EvidenceBuildError): pointer({'a': [1]}, p)
        with self.assertRaisesRegex(EvidenceBuildError, 'full interactive factor ID'): parse_factor('CAD::Factor1', 'cfde-inc-v2')

    def test_builder_pin_and_missing_geneset_mapping_fail(self):
        spec = deepcopy(self.spec); spec['authoring']['assembly_builder']['source_sha256'] = '0' * 64
        with self.assertRaisesRegex(EvidenceBuildError, 'Builder code pin mismatch'): self.build(spec)
        spec = deepcopy(self.spec); spec['gene_sets'] = {}
        with self.assertRaisesRegex(EvidenceBuildError, 'Missing GeneSet binding'): self.build(spec)

    def test_transport_captures_retries_and_exact_bodies(self):
        class Response(io.BytesIO):
            def __init__(self, status, body):
                super().__init__(body); self.status = status; self.headers = {'Content-Type': 'application/json'}
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(Path(directory) / 'capture'); client = HttpCaptureClient(store)
            bodies = [b'{"error":"try again"}', b'{ "items" : [] }']
            with patch('reveal_backend.evidence_collector.urlopen', side_effect=[Response(503, bodies[0]), Response(200, bodies[1])]), patch('reveal_backend.evidence_collector.time.sleep'):
                key, result = client.request('retry', 'https://example.org/read', {'target_type': 'gene'})
            self.assertEqual(key, 'retry.attempt-2')
            self.assertEqual(result['request'], {'target_type': 'gene'})
            self.assertEqual(store.blobs['retry.attempt-2.body'], bodies[1])
            self.assertEqual(decode(store.blobs['retry.attempt-1'])['status'], 503)

    def test_multicolons_and_uri_expansion(self):
        resolver = self.runtime.resolver(self.built.package['prefixes'])
        self.assertEqual(resolver.expand(FACTOR), 'urn:cfde:factor:portal:CADinT2D:cfde-inc-v2:Factor1')
        self.assertEqual(resolver.expand('GO:0006809'), 'http://purl.obolibrary.org/obo/GO_0006809')
        with self.assertRaises(ValueError): resolver.expand('UNKNOWN:1')


if __name__ == '__main__':
    unittest.main()
