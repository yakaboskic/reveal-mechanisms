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

from reveal_backend.evidence_collector import CaptureStore, HttpCaptureClient, add_source_prefixes, collect_package, parse_factor
from reveal_backend.evidence_package import (DapperRuntime, EvidenceBuildError, build_package, canonical_json,
                                            decode, frozen_semantic_association, load_build_input, pointer, sha256)
from reveal_backend.evidence_schema import load_generated_schema, validate_package_shape

ROOT = Path(__file__).resolve().parents[3]
FACTOR = 'factor:portal:CADinT2D:cfde-inc-v2:Factor1'
GAP = 'cad_pgsxc_reverse_causation'


def semantic_metadata(context_id='dismech:context', revision='source-revision', text='Exact mechanism description'):
    return {'origins': {FACTOR: 'automatic'}, 'dismissed_eaggl_ids': [],
        'semantic_retrieval': {'status': 'computed', 'embedding_run_id': 'frozen-embedding'},
        'frozen_binding': {'dismech_import_id': 'dismech-import',
            'anchors': [{'cfde_node_id': FACTOR, 'embedding_run_id': 'frozen-embedding', 'mapping_run_id': 'mapping'}],
            'retrieval': {FACTOR: {'mode': 'semantic', 'embedding_run_id': 'frozen-embedding', 'mapping_run_id': 'mapping',
                'dismech_import_id': 'dismech-import', 'dismech_embedding_run_id': 'dismech-run',
                'context_embedding_inputs': [{'source_id': context_id, 'source_kind': 'mechanism',
                    'source_revision': revision, 'template': 'dismech-description-v1', 'input_sha256': sha256(text.encode())}],
                'hit': {'matched_context_ids': [context_id],
                    'ranking': {'metric': 'cosine_similarity', 'value': .4372766973724816, 'rank': 5},
                    'context_similarities': {context_id: .4372766973724816}}}}}}


class FrozenSemanticAssociationTests(unittest.TestCase):
    def project(self, metadata, context='dismech:context', revision='source-revision', text='Exact mechanism description'):
        return frozen_semantic_association(metadata, FACTOR, context, revision, text)

    def test_legacy_single_context_score_is_recoverable_but_multiple_contexts_are_not(self):
        metadata = semantic_metadata()
        retrieval = metadata['frozen_binding']['retrieval'][FACTOR]
        del retrieval['hit']['context_similarities']
        self.assertEqual(self.project(metadata)['semantic_similarity'], .4372766973724816)
        retrieval['context_embedding_inputs'].append(dict(retrieval['context_embedding_inputs'][0], source_id='dismech:other'))
        self.assertEqual(self.project(metadata)['status'], 'not_computed')

    def test_distinct_pairs_and_hybrid_scores_are_not_replaced_by_the_ranking(self):
        metadata = semantic_metadata()
        retrieval = metadata['frozen_binding']['retrieval'][FACTOR]
        retrieval['context_embedding_inputs'].append(dict(retrieval['context_embedding_inputs'][0], source_id='dismech:other'))
        retrieval['hit']['context_similarities']['dismech:other'] = -.25
        for mode in ('semantic', 'hybrid'):
            retrieval['mode'] = mode
            retrieval['hit']['ranking'] = {'metric': 'reciprocal_rank_fusion' if mode == 'hybrid' else 'cosine_similarity',
                                         'value': .99, 'rank': 1}
            with self.subTest(mode=mode):
                self.assertEqual(self.project(metadata)['semantic_similarity'], .4372766973724816)
                self.assertEqual(self.project(metadata, 'dismech:other')['semantic_similarity'], -.25)
        del retrieval['hit']['context_similarities']
        retrieval['context_embedding_inputs'] = retrieval['context_embedding_inputs'][:1]
        self.assertEqual(self.project(metadata)['status'], 'not_computed')

    def test_zero_is_computed_and_missing_pair_is_not(self):
        metadata = semantic_metadata()
        scores = metadata['frozen_binding']['retrieval'][FACTOR]['hit']['context_similarities']
        scores['dismech:context'] = 0.
        self.assertEqual(self.project(metadata)['status'], 'computed')
        self.assertEqual(self.project(metadata)['semantic_similarity'], 0.)
        scores.clear()
        self.assertIsNone(self.project(metadata)['semantic_similarity'])

    def test_wrong_context_source_template_text_or_run_never_becomes_a_pair(self):
        cases = [('source_id', 'mechanism_subquery'), ('source_kind', 'knowledge_gap'),
                 ('source_revision', 'changed'), ('template', 'changed'), ('input_sha256', 'changed')]
        for key, value in cases:
            metadata = semantic_metadata()
            metadata['frozen_binding']['retrieval'][FACTOR]['context_embedding_inputs'][0][key] = value
            with self.subTest(key=key):
                self.assertEqual(self.project(metadata)['status'], 'not_computed')
        for key in ('embedding_run_id', 'dismech_import_id', 'mapping_run_id', 'dismech_embedding_run_id'):
            metadata = semantic_metadata()
            metadata['frozen_binding']['retrieval'][FACTOR][key] = None
            with self.subTest(key=key):
                self.assertEqual(self.project(metadata)['status'], 'not_computed')

    def test_selection_origin_is_preserved_without_inventing_measurements(self):
        self.assertEqual(self.project(None)['association_basis'], 'user_supplied_anchor')
        metadata = semantic_metadata()
        metadata['frozen_binding']['retrieval'][FACTOR] = None
        self.assertEqual(self.project(metadata), {'status': 'not_computed', 'semantic_similarity': None,
                                               'association_basis': 'automatic_selection'})
        metadata['origins'][FACTOR] = 'manual'
        self.assertEqual(self.project(metadata)['association_basis'], 'user_supplied_anchor')
        metadata = semantic_metadata()
        metadata['frozen_binding']['retrieval'][FACTOR]['mode'] = 'lexical'
        self.assertEqual(self.project(metadata)['status'], 'not_computed')

    def test_invalid_captured_cosines_are_rejected(self):
        for score in (None, True, float('nan'), float('inf'), 1.01, -1.01):
            metadata = semantic_metadata()
            metadata['frozen_binding']['retrieval'][FACTOR]['hit']['context_similarities']['dismech:context'] = score
            with self.subTest(score=score), self.assertRaises(EvidenceBuildError):
                self.project(metadata)


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

    def test_real_t2d_lowercase_hgnc_collects_with_exact_source_payloads(self):
        # Portable fixture assembled from committed, exact T2D raw records.
        # Its reconstructed source bytes have their own checksum, not the live
        # YAML checksum. CFDE transport is the existing explicit test fixture.
        mechanisms = decode((ROOT/'data/dismech/t2d-mechanisms.json').read_bytes())
        index = ROOT/'data/dismech-gaps/2026-09-24'
        with gzip.open(index/'knowledge-gaps.jsonl.gz','rt') as stream:
            gaps = [json.loads(line) for line in stream if 'dismech:disorders/Type_2_Diabetes_Mellitus' in line]
        gaps = [row for row in gaps if row['document_id']=='dismech:disorders/Type_2_Diabetes_Mellitus']
        chosen = next(row for row in gaps if row['discussion_id']=='gap_t2d_beta_cell_dedifferentiation_reversibility')
        with gzip.open(index/'gap-attachments.jsonl.gz','rt') as stream:
            attachments = [json.loads(line) for line in stream if 'dismech:disorders/Type_2_Diabetes_Mellitus' in line]
        attachments = [row for row in attachments if row['gap_id']==chosen['id']]
        document = {'pathophysiology':[row['raw'] for row in sorted(mechanisms,key=lambda row:int(row['json_pointer'].rsplit('/',1)[1]))],
                    'discussions':[row['raw'] for row in sorted(gaps,key=lambda row:int(row['source_pointer'].rsplit('/',1)[1]))]}
        source_bytes = canonical_json(document)
        self.assertIn(b'hgnc:',source_bytes)
        directory = self.root/'t2d-prefix'; source=directory/'source'; frozen=directory/'index'
        source_path=source/chosen['source_file']; source_path.parent.mkdir(parents=True); source_path.write_bytes(source_bytes)
        (source/'src/dismech/schema').mkdir(parents=True)
        (source/'src/dismech/schema/dismech.yaml').write_bytes(canonical_json({'prefixes':{
            'HGNC':'https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/',
            **{name:f'http://purl.obolibrary.org/obo/{name}_' for name in ('GO','ECTO','NCBITaxon','NCIT','UBERON')}}}))
        frozen.mkdir(parents=True)
        manifest=decode((index/'manifest.json').read_bytes())
        for filename,rows in [('knowledge-gaps.jsonl.gz',gaps),('gap-attachments.jsonl.gz',attachments)]:
            with gzip.open(frozen/filename,'wt') as stream:
                for row in rows: stream.write(json.dumps(row)+'\n')
            manifest['files'][filename]['sha256']=sha256((frozen/filename).read_bytes())
        (frozen/'manifest.json').write_bytes(canonical_json(manifest))
        (frozen/'source-files.json').write_bytes(canonical_json([{'path':chosen['source_file'],'sha256':sha256(source_bytes)}]))
        built=collect_package(gap_id=chosen['id'],factor_ids=[FACTOR],output=directory/'capture',dapper=self.runtime,
            project_root=ROOT,dismech_source=source,dismech_index=frozen,geneset_import=self.imported,
            limit=8,client_factory=FixtureClient)
        packet=built.package
        self.assertEqual(packet['selection']['knowledge_gap_id'],'dapper:KnowledgeGap.m6gVKa2vVfnyJR8191TfE6T0BNlUFy4p')
        self.assertEqual(packet['prefixes']['hgnc'],str(self.runtime.schema.namespaces()['HGNC']))
        resolver=self.runtime.resolver(packet['prefixes'])
        self.assertEqual(resolver.expand('hgnc:11892'),resolver.expand('HGNC:11892'))
        validate_package_shape(packet,load_generated_schema(ROOT/'schema/evidence-package.schema.json'))
        self.runtime.validate(packet['dapper_context'])
        captured=next(artifact for artifact in packet['source_artifacts'].values() if artifact['filename']=='Type_2_Diabetes_Mellitus.yaml')
        self.assertEqual(captured['sha256'],sha256(source_bytes))
        self.assertEqual(source_path.read_bytes(),source_bytes)
        self.assertEqual((directory/'capture/package'/captured['path']).read_bytes(),source_bytes)
        source_names={row['raw']['name'] for row in mechanisms}
        source_nodes=[node for node in packet['dapper_context']['mechanisms'] if node['name'] in source_names]
        self.assertEqual(len(source_nodes),3)
        for node in source_nodes:
            original=next(row for row in mechanisms if row['raw']['name']==node['name'])
            expected={'name':original['raw']['name'],'description':original['raw'].get('description',original['raw']['name'])}
            self.assertEqual(node['id'],self.runtime.compute_id(expected,'Mechanism',self.runtime.schema))

    def test_hgnc_alias_does_not_allow_unknown_or_conflicting_prefixes(self):
        namespaces={'HGNC':'http://identifiers.org/hgnc/'}
        raw={'genes':[{'term':{'id':'hgnc:11892'}}]}; before=deepcopy(raw); prefixes={}
        add_source_prefixes(raw,namespaces,prefixes)
        self.assertEqual(raw,before)
        self.assertEqual(prefixes,{'hgnc':namespaces['HGNC']})
        for unknown in ('hGnC:11892','unknown:11892'):
            with self.subTest(unknown=unknown),self.assertRaisesRegex(EvidenceBuildError,'Unknown DisMech CURIE prefix'):
                add_source_prefixes({'id':unknown},namespaces,{})
        with self.assertRaisesRegex(EvidenceBuildError,'Conflicting DisMech CURIE alias'):
            add_source_prefixes(raw,{**namespaces,'hgnc':'https://example.invalid/'},{})

    def test_frozen_browser_selection_provenance_survives_collection(self):
        dismissed=FACTOR.rsplit(':',1)[0]+':Factor2'
        context_id, mechanism = next(iter(self.built.package['dismech']['mechanisms'].items()))
        source_revision = self.built.package['dismech']['source_revision']['source_sha256']
        metadata = semantic_metadata(context_id, source_revision, mechanism['description'])
        metadata['dismissed_eaggl_ids'] = [dismissed]
        built=collect_package(gap_id=GAP,factor_ids=[FACTOR],output=self.root/'browser-selection',dapper=self.runtime,
            project_root=ROOT,dismech_source=self.source,dismech_index=ROOT/'data/dismech-gaps/2026-09-24',
            geneset_import=self.imported,limit=8,client_factory=FixtureClient,selection_metadata=metadata)
        self.assertEqual(built.package['selection']['origins'],{FACTOR:'automatic'})
        self.assertEqual(built.package['selection']['dismissed_eaggl_ids'],[dismissed])
        self.assertEqual(built.package['selection']['semantic_retrieval'],metadata['semantic_retrieval'])
        captured=built.package['source_artifacts']['selection-provenance']
        self.assertEqual(decode((self.root/'browser-selection/package'/captured['path']).read_bytes()),metadata)
        self.assertEqual(built.package['dismech']['mechanisms'][context_id]['associated_eaggl_mechanisms'][FACTOR],
            {'status':'computed','semantic_similarity':.4372766973724816,'association_basis':'semantic_retrieval'})
        validate_package_shape(built.package,load_generated_schema(ROOT/'schema/evidence-package.schema.json'))
        spec, blobs = load_build_input(self.root/'browser-selection/build-input.json', self.root/'browser-selection')
        replay = build_package(spec, blobs, self.runtime)
        self.assertEqual(replay.package, built.package)
        spec['dismech']['mechanisms'][context_id]['associated_eaggl_mechanisms'][FACTOR]['semantic_similarity'] = .99
        with self.assertRaisesRegex(EvidenceBuildError, 'differs from frozen provenance'):
            build_package(spec, blobs, self.runtime)

    def test_database_unmapped_candidate_is_explicitly_omitted_without_disabling_anchor(self):
        with gzip.open(self.imported/'records.jsonl.gz','rt') as stream: rows=[json.loads(line) for line in stream]
        missing=rows[0]['node_id']
        def resolver(wanted,model):
            return ((self.imported/'manifest.json').read_bytes(),decode((self.imported/'activity.json').read_bytes()),
                    [row for row in rows if row['node_id'] in wanted and row['node_id']!=missing])
        built=collect_package(gap_id=GAP,factor_ids=[FACTOR],output=self.root/'partial-database-aliases',dapper=self.runtime,
            project_root=ROOT,dismech_source=self.source,dismech_index=ROOT/'data/dismech-gaps/2026-09-24',
            geneset_import=self.imported,geneset_resolver=resolver,limit=8,client_factory=FixtureClient)
        self.assertIn(FACTOR,built.package['selection']['eaggl_mechanism_ids'])
        self.assertIn(missing,built.package['coverage']['omitted_node_ids'])
        self.assertNotIn(missing,built.package['pigean']['graph']['node_ids'])
        self.assertIn('geneset-alias-resolution',built.package['source_artifacts'])

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

    def test_all_pruned_observations_are_omitted_not_empty_source_results(self):
        spec = deepcopy(self.spec)
        spec['policy']['retain_node_ids'] = [FACTOR]
        spec['policy']['max_nodes'] = 1
        result = self.build(spec).package
        mechanism = result['pigean']['mechanisms'][FACTOR]
        for kind in ('gene', 'gene_set'):
            collection = mechanism[kind + '_loadings']
            self.assertEqual(collection['items'], {})
            self.assertEqual(collection['status'], 'omitted')
            self.assertEqual(collection['query_status'], self.built.package['coverage']['queries'][kind])
            trait = result['pigean']['traits'][mechanism['fit']['trait_id']]
            self.assertEqual(trait[kind + '_associations']['status'], 'omitted')
        self.assertEqual(result['coverage']['queries']['factor'], 'empty')
        self.assertEqual(result['source_artifacts'], self.built.package['source_artifacts'])
        validate_package_shape(result, load_generated_schema(ROOT/'schema/evidence-package.schema.json'))

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
