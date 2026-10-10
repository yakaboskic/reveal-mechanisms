"""KPN reference evidence from the reference release tables is accepted by the unchanged builder, schema and worker checks."""
from copy import deepcopy
import gzip
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import reference_evidence as reference
from reveal_backend.box_adapter import dispatchable_capture
from reveal_backend.evidence_package import (DapperRuntime, EvidenceBuildError, build_package, canonical_json, decode,
                                            load_build_input, sha256)
from reveal_backend.evidence_schema import load_generated_schema, validate_package_shape
from reveal_backend.reference_generation import KPN_MODEL, LEGACY_MODEL, factor_key, mechanism_node, public_id
from reveal_backend.scientific_account_lint import cfde_source_files

ROOT = Path(__file__).resolve().parents[3]
GAP = 'cad_pgsxc_reverse_causation'
GAP_DAPPER_ID = 'dapper:KnowledgeGap.zNV20nhHamt-a4CeAktQQPoAivOJe6xk'  # collect_package's id for the same gap
RELEASE = sha256(b'reference-evidence test release')
CAD, T2D = 'KPN.TRAIT:0000398', 'KPN.TRAIT:0000319'
TRAITS = {CAD: ('CADinT2D', 'Coronary artery disease in type 2 diabetes', 'cardiovascular'), T2D: ('T2D', 'Type 2 diabetes', 'metabolic')}
GENES = ['SHH', 'TCF7L2', 'SIX2', 'GLI3', 'FGF13', 'EYA2', 'CTNNB1', 'WNT4', 'APOB', 'LPA', 'PCSK9', 'INS']
# (kpn trait, FactorN, label, {gene: loading})
FACTORS = [(CAD, 'Factor1', 'Hedgehog signalling', {'SHH': .9, 'TCF7L2': .6, 'SIX2': .4, 'GLI3': .3, 'FGF13': .2}),
           (CAD, 'Factor2', 'Wnt signalling', {'SHH': .5, 'WNT4': .7, 'CTNNB1': .4}),
           (CAD, 'Factor3', 'Lipoprotein', {'APOB': .8, 'LPA': .6}),
           (T2D, 'Factor1', 'Insulin secretion', {'TCF7L2': .95, 'INS': .8, 'SHH': .1}),
           (T2D, 'Factor2', 'Cholesterol', {'PCSK9': .5, 'GLI3': .2})]
ANCHOR, SECOND, SIBLING = public_id(CAD, 'Factor1'), public_id(T2D, 'Factor1'), public_id(CAD, 'Factor2')
SCHEMA = load_generated_schema(ROOT / 'schema/evidence-package.schema.json')
CFDE_YAML = Path('/humgen/diabetes/users/chase/data/dig-s3/gene_sets/cfde/2026-09-28/GTEx/genesets/adipose_tissue/models/HZ1/'
                 'extractor/age50_20/GeneSetCollection.oqDFu8MGOkOqvQI3blZG_jIviRwR4qfv.yaml')


class SQLiteMySQL:
    """DB-API stand-in for pymysql: %s placeholders and context-manager cursors over SQLite."""
    def __init__(self, path):
        self.db = sqlite3.connect(path); self.db.execute('PRAGMA foreign_keys=ON')
        self.statements, self.closed, self.rollbacks = [], False, 0

    def cursor(self): return Cursor(self)
    def commit(self): self.db.commit()
    def rollback(self): self.rollbacks += 1; self.db.rollback()
    def close(self): self.closed = True; self.db.close()


class Cursor:
    def __init__(self, connection): self.connection, self.cursor = connection, connection.db.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, sql, values=()):
        self.connection.statements.append(sql)
        return self.cursor.execute(sql.replace('%s', '?'), values)
    def executemany(self, sql, rows): return self.cursor.executemany(sql.replace('%s', '?'), rows)
    def fetchall(self): return self.cursor.fetchall()
    def fetchone(self): return self.cursor.fetchone()


# The flat per-environment reference tables (reveal_ref_*, contract columns), spelled for SQLite.
REFERENCE_DDL = '''
CREATE TABLE reveal_ref_release(release_id CHAR(64) PRIMARY KEY, published_at TEXT NOT NULL, manifest TEXT NOT NULL);
CREATE TABLE reveal_ref_traits(kpn_trait_id TEXT PRIMARY KEY, legacy_phenotype_id TEXT NOT NULL UNIQUE, phenotype_name TEXT NOT NULL,
  gwas_source_category TEXT NOT NULL, trait_group TEXT, legacy_trait_group TEXT, trait_type TEXT, description TEXT,
  is_dichotomous INTEGER, is_complex INTEGER, n_factors INTEGER NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE reveal_ref_factors(factor_key TEXT PRIMARY KEY, public_id TEXT NOT NULL UNIQUE, eaggl_factor_id TEXT NOT NULL UNIQUE,
  kpn_trait_id TEXT NOT NULL REFERENCES reveal_ref_traits(kpn_trait_id), factor_number INTEGER NOT NULL, label TEXT NOT NULL,
  input_sha256 TEXT NOT NULL, source_revision TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE reveal_ref_factor_genes(factor_key TEXT NOT NULL REFERENCES reveal_ref_factors(factor_key), gene TEXT NOT NULL,
  loading REAL NOT NULL, PRIMARY KEY(factor_key, gene));
CREATE TABLE reveal_ref_collections(collection_id TEXT PRIMARY KEY, cfde_label TEXT NOT NULL UNIQUE, library TEXT NOT NULL,
  n_sets INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE reveal_ref_gene_sets(gene_set_id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES reveal_ref_collections(collection_id),
  gene_set_name TEXT NOT NULL, library TEXT NOT NULL, n_genes INTEGER NOT NULL, n_genes_in_eaggl_universe INTEGER NOT NULL,
  legacy_source_key TEXT, metadata TEXT NOT NULL);
CREATE TABLE reveal_ref_projections(factor_key TEXT NOT NULL REFERENCES reveal_ref_factors(factor_key),
  gene_set_id TEXT NOT NULL REFERENCES reveal_ref_gene_sets(gene_set_id), library TEXT NOT NULL, joint_loading REAL NOT NULL,
  marginal_loading REAL NOT NULL, joint_loading_text TEXT NOT NULL, marginal_loading_text TEXT NOT NULL, joint_rank INTEGER NOT NULL,
  marginal_rank INTEGER NOT NULL, is_joint_top_factor INTEGER NOT NULL, PRIMARY KEY(factor_key, gene_set_id));
'''
TABLES = ('release', 'traits', 'factors', 'factor_genes', 'collections', 'gene_sets', 'projections')


def insert(db, table, rows):
    for row in rows:
        db.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" * len(row))})',
                   [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()])


class ReferenceEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        from reveal_backend.runtime_config import CURRENT_DAPPER_SNAPSHOT
        cls.runtime = DapperRuntime(CURRENT_DAPPER_SNAPSHOT)
        cls.source, cls.index = cls.dismech_fixture(cls.root)
        cls.database = cls.root / 'reference.sqlite3'
        cls.gene_sets = cls.write_database(cls.database)
        cls.single = cls.run_collector('single', [ANCHOR], [])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def dismech_fixture(root):
        """A DisMech checkout with the committed CAD document and a tiny index of its gaps."""
        source = root / 'dismech'
        (source / 'kb/disorders').mkdir(parents=True); (source / 'src/dismech/schema').mkdir(parents=True)
        shutil.copyfile(ROOT / 'docs/examples/evidence-package-cad/sources/Coronary_Artery_Disease.yaml',
                        source / 'kb/disorders/Coronary_Artery_Disease.yaml')
        (source / 'src/dismech/schema/dismech.yaml').write_text(
            'prefixes:\n  GO: http://purl.obolibrary.org/obo/GO_\n  ECTO: http://purl.obolibrary.org/obo/ECTO_\n')
        real, index = ROOT / 'data/dismech-gaps/2026-09-24', root / 'dismech-index'
        index.mkdir()
        with gzip.open(real / 'knowledge-gaps.jsonl.gz', 'rt') as stream:
            gaps = [json.loads(line) for line in stream if 'Coronary_Artery_Disease' in line]
        document = next(row['document_id'] for row in gaps if row['discussion_id'] == GAP)
        gaps = [row for row in gaps if row['document_id'] == document]
        ids = {row['id'] for row in gaps}
        with gzip.open(real / 'gap-attachments.jsonl.gz', 'rt') as stream:
            attachments = [row for row in map(json.loads, stream) if row['gap_id'] in ids]
        manifest = decode((real / 'manifest.json').read_bytes())
        for filename, rows in [('knowledge-gaps.jsonl.gz', gaps), ('gap-attachments.jsonl.gz', attachments)]:
            (index / filename).write_bytes(gzip.compress(''.join(json.dumps(row) + '\n' for row in rows).encode(), mtime=0))
            manifest['files'][filename]['sha256'] = sha256((index / filename).read_bytes())
        (index / 'manifest.json').write_bytes(canonical_json(manifest))
        files = {row['source_file'] for row in gaps}
        inventory = [row for row in decode((real / 'source-files.json').read_bytes()) if row['path'] in files]
        (index / 'source-files.json').write_bytes(canonical_json(inventory))
        return source, index

    @classmethod
    def write_database(cls, path):
        runtime = cls.runtime
        activity = {'name': 'dig-gene-set-extractors convert rna_deg_multi', 'activity_type': 'geneset_extraction',
                    'software_name': 'dig-gene-set-extractors', 'code_version': 'c40de4c2585d3dace3d3343f0d5fc733cbd55b6a',
                    'generated_at_time': '2026-09-28T14:49:48+00:00'}
        activity['id'] = runtime.compute_id(activity, 'Activity', runtime.schema)
        collection = 'dapper:GeneSetCollection.' + sha256(b'collection')[:32]

        def gene_set(name, members):
            node = {'name': name, 'alternate_identifier': [name], 'member_type': 'gene', 'members': ['HGNC.SYMBOL:' + m for m in members],
                    'n_genes': len(members), 'was_generated_by': activity['id'], 'term_prefix': 'GTEx', 'organism': 'human',
                    'genome_build': 'hg38', 'assay': 'bulk', 'data_type': 'rna_seq'}
            node['id'] = runtime.compute_id(node, 'GeneSet', runtime.schema)
            # Non-identity slots exactly as the CFDE collection YAML writes them.
            return {**node, 'in_gene_set_collection': [collection], 'in_gmt_file': 'dapper:File.' + sha256(name.encode())[:32],
                    'gmt_entry': node['id'], 'has_embedding': ['dapper:Embedding.' + sha256(name.encode())[32:]]}
        exact_a, exact_d = gene_set('GTEx_aging_Test_up', ['SHH', 'TCF7L2']), gene_set('GTEx_aging_Test_dn', ['GLI3', 'WNT4', 'CTNNB1'])
        altered = gene_set('GTEx_aging_Altered', ['APOB']); altered['members'] = ['HGNC.SYMBOL:LPA']  # stored bytes no longer match the id
        alias_only = 'dapper:GeneSet.' + sha256(b'alias only')[:32]
        sets = {'A': (exact_a['id'], exact_a['name'], {'dapper_gene_set': exact_a}),
                'B': (alias_only, 'LINCS_L1000_Chem_Pert_Test_dn', {'lap': {'gmt_row': 7}}),
                'C': (altered['id'], altered['name'], {'dapper_gene_set': altered}),
                'D': (exact_d['id'], exact_d['name'], {'dapper_gene_set': exact_d})}
        db = sqlite3.connect(path); db.execute('PRAGMA foreign_keys=ON')
        db.executescript(REFERENCE_DDL)
        insert(db, 'reveal_ref_release', [dict(release_id=RELEASE, published_at='2026-10-05T12:00:00Z',
            manifest={'format': 'test', 'embedding': {'model': 'm', 'model_revision': 'r', 'provider': 'p', 'dimensions': 2}})])
        insert(db, 'reveal_ref_traits', [dict(kpn_trait_id=t, legacy_phenotype_id=legacy, phenotype_name=name, gwas_source_category='KPN',
                                              trait_group=group, trait_type='phenotype', n_factors=sum(f[0] == t for f in FACTORS), metadata={'kpn_release': 'v0.0.2'})
                                         for t, (legacy, name, group) in TRAITS.items()])
        insert(db, 'reveal_ref_factors', [dict(factor_key=factor_key(t, f), public_id=public_id(t, f), eaggl_factor_id=f'{TRAITS[t][0]}::{f}',
                                               kpn_trait_id=t, factor_number=int(f[6:]), label=label, input_sha256=sha256(label.encode()),
                                               source_revision=sha256(f.encode()), metadata={'label': label, 'top_genes': ','.join(loads)})
                                          for t, f, label, loads in FACTORS])
        insert(db, 'reveal_ref_factor_genes', [dict(factor_key=factor_key(t, f), gene=g, loading=v) for t, f, _, loads in FACTORS for g, v in loads.items()])
        # The collection payload layout {collection, index, document_sha256, provenance}; only `provenance` is read.
        unrelated = dict(activity, name='dig-gene-set-extractors prepare_deg_long', activity_type='geneset_preparation'); del unrelated['id']
        unrelated['id'] = runtime.compute_id(unrelated, 'Activity', runtime.schema)
        insert(db, 'reveal_ref_collections', [dict(collection_id=collection, cfde_label='GTEx__test__HZ1', library='GTEx', n_sets=4,
            payload={'collection': {'id': collection, 'name': 'GTEx test collection', 'n_sets': 4}, 'index': {'label': 'GTEx__test__HZ1'},
                     'document_sha256': sha256(b'document'),
                     'provenance': {'prefixes': {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:', 'humgen': 'file:///humgen/'},
                                    'organizations': [], 'datasets': [], 'files': [], 'activities': [unrelated, activity]}})])
        insert(db, 'reveal_ref_gene_sets', [dict(gene_set_id=gid, collection_id=collection, gene_set_name=name, library='GTEx',
                                                 n_genes=3, n_genes_in_eaggl_universe=2, metadata=metadata) for gid, name, metadata in sets.values()])
        projections = {(CAD, 'Factor1'): {'A': (.9, .8), 'B': (.5, .6), 'C': (.3, .2), 'D': (0., .4)}, (CAD, 'Factor2'): {'A': (.4, .5), 'D': (.6, .7)},
                       (T2D, 'Factor1'): {'B': (.7, .7), 'C': (.2, .1)}, (T2D, 'Factor2'): {'D': (.3, .3)}}
        rows = []
        for (t, f), values in projections.items():
            joint = sorted(values, key=lambda k: -values[k][0]); marginal = sorted(values, key=lambda k: -values[k][1])
            # Ranks are per library; every fixture gene set is a GTEx set.
            rows += [dict(factor_key=factor_key(t, f), gene_set_id=sets[k][0], library='GTEx', joint_loading=j, marginal_loading=m,
                          joint_loading_text=f'{j:.4g}', marginal_loading_text=f'{m:.4g}', joint_rank=joint.index(k) + 1, marginal_rank=marginal.index(k) + 1,
                          is_joint_top_factor=int(k == 'A')) for k, (j, m) in values.items()]
        insert(db, 'reveal_ref_projections', rows)
        db.commit(); db.close()
        cls.activity = activity
        return {key: value[0] for key, value in sets.items()}

    @classmethod
    def run_collector(cls, name, factor_ids, connections, database=None, **overrides):
        def connect():
            connection = SQLiteMySQL(database or cls.database); connections.append(connection); return connection
        arguments = dict(gap_id=GAP, factor_ids=factor_ids, output=cls.root / name, dapper=cls.runtime, project_root=ROOT,
                         dismech_source=cls.source, dismech_index=cls.index, connection_factory=connect,
                         selected_graphs=['biomarkerkg'], limit=20)
        arguments.update(overrides)
        with patch('reveal_backend.evidence_collector.urlopen', side_effect=AssertionError('Unexpected network')):
            return reference.collect_reference_package(**arguments)

    def collect(self, name, factor_ids, **overrides):
        self.connections = []
        return self.run_collector(name, factor_ids, self.connections, **overrides)

    def built(self):
        return self.single

    def artifact(self, package, key, directory='single'):
        return decode((self.root / directory / 'package' / package['source_artifacts'][key]['path']).read_bytes())

    def test_package_passes_builder_schema_and_worker_checks(self):
        built = self.built(); package = built.package
        validate_package_shape(package, SCHEMA)
        validate_package_shape(decode(built.files['evidence-package.yaml'], 'yaml'), SCHEMA)
        # MySQL holds no PIGEAN trait-level phenotype associations: explicit blockers, never stand-in rows.
        self.assertFalse(package['readiness']['input_capture_complete'])
        self.assertEqual(package['readiness']['capture_blockers'], ['bioindex:trait:trait:kpn:0000398:gene:not_captured',
                                                                    'bioindex:trait:trait:kpn:0000398:gene_set:not_captured'])
        self.assertEqual(package['policy']['allow_incomplete_capture'], True)
        # box_adapter dispatches a KPN package whose only gaps are those trait queries, and nothing else incomplete.
        self.assertTrue(dispatchable_capture(package))
        other = deepcopy(package); other['readiness']['capture_blockers'].append('bioindex:factor:' + ANCHOR + ':gene:not_captured')
        self.assertFalse(dispatchable_capture(other))
        legacy = deepcopy(package); legacy['pigean']['model'] = LEGACY_MODEL
        self.assertFalse(dispatchable_capture(legacy))
        empty = deepcopy(package); empty['readiness']['capture_blockers'] = []
        self.assertFalse(dispatchable_capture(empty))
        # worker.collect: the gap object, DisMech revision, public anchor ids and selected graphs.
        self.assertEqual(package['selection']['knowledge_gap_id'], GAP_DAPPER_ID)
        gap = next(g for g in package['dapper_context']['knowledge_gaps'] if g['id'] == GAP_DAPPER_ID)
        self.assertEqual(self.runtime.compute_id({k: v for k, v in gap.items() if k != 'id'}, 'KnowledgeGap', self.runtime.schema), gap['id'])
        inventory = {row['path']: row['sha256'] for row in decode((self.index / 'source-files.json').read_bytes())}
        self.assertIn(package['dismech']['source_revision']['source_sha256'], inventory.values())
        self.assertEqual(package['selection']['eaggl_mechanism_ids'], [ANCHOR])
        self.assertEqual(package['external_evidence']['selected_graphs'], ['biomarkerkg'])
        self.assertEqual(set(package['coverage']['queries'].values()), {'ok'})
        self.assertEqual(len(package['coverage']['bioindex_queries']), 2)  # factor scope, gene and gene set; no trait-scope query

    def supplied_inputs(self):
        from reveal_backend import user_inputs
        data = b'First observation\nSecond observation'
        extracted = user_inputs.parse_document(data, 'notes.txt')
        directory = self.root / 'user-inputs'; directory.mkdir(exist_ok=True)

        def retain(raw, media):
            path = directory / sha256(raw); path.write_bytes(raw)
            return {'store': 'filesystem', 'key': str(path), 'sha256': sha256(raw), 'size_bytes': len(raw), 'content_type': media}

        original = retain(data, 'text/plain'); extraction = retain(canonical_json(extracted), 'application/json')
        return {'format': 'reveal.user-inputs/1', 'research_direction': 'Test the temporal direction',
                'context': 'Private preliminary observations', 'hypotheses': 'Exposure precedes the disease',
                'uploads': [{'id': 'test-notes', 'filename': 'notes.txt', 'media_type': 'text/plain',
                             'size_bytes': len(data), 'sha256': sha256(data), 'storage': original,
                             'extraction': {'storage': extraction, 'format': extracted['format'],
                                            'original_sha256': sha256(data), 'segment_count': len(extracted['segments'])}}]}

    def test_private_inputs_reach_kpn_package_replay_and_bundle(self):
        from reveal_backend.agent_execution import ExecutionRequest
        from reveal_backend.box_adapter import make_bundle
        from reveal_backend.evidence_files import build_evidence_files
        from reveal_backend.scientific_grounding import review_evidence
        from reveal_backend.scientific_review_reader import EvidenceReader
        supplied = self.supplied_inputs(); frozen = deepcopy(supplied)
        with patch('reveal_backend.user_inputs.artifacts_root', return_value=self.root):
            built = self.collect('private-inputs', [ANCHOR], user_inputs=supplied)
        self.assertEqual(supplied, frozen)  # the frozen request cannot acquire capture metadata
        package = built.package; validate_package_shape(package, SCHEMA)
        self.assertEqual(package['pigean']['model'], KPN_MODEL)
        upload = package['user_inputs']['uploads'][0]
        self.assertEqual(upload['content']['segments'][0], {'locator': 'line:1', 'text': 'First observation'})
        for field, storage in [('original_artifact_id', frozen['uploads'][0]['storage']),
                               ('extraction_artifact_id', frozen['uploads'][0]['extraction']['storage'])]:
            source = package['source_artifacts'][upload[field]]
            self.assertTrue(source['private'])
            self.assertEqual(source['sha256'], storage['sha256'])
            self.assertEqual(built.files[source['path']], Path(storage['key']).read_bytes())
        spec, blobs = load_build_input(self.root / 'private-inputs/build-input.json', self.root / 'private-inputs')
        self.assertEqual(build_package(spec, blobs, self.runtime).files, built.files)
        request = ExecutionRequest(job_id='private-kpn-test', attempt=1, kind='research',
            input_path=self.root / 'private-inputs/package/evidence-package.json', output_dir=self.root / 'bundle-output',
            selected_graphs=tuple(package['external_evidence']['selected_graphs']))
        with tarfile.open(fileobj=io.BytesIO(make_bundle(ROOT, request)), mode='r:gz') as bundle:
            for field in ('original_artifact_id', 'extraction_artifact_id'):
                source = package['source_artifacts'][upload[field]]
                self.assertEqual(sha256(bundle.extractfile('input/' + source['path']).read()), source['sha256'])
        reading = build_evidence_files(request.input_path.read_bytes())
        self.assertIn('user_inputs', json.loads(reading['input/evidence-index.json'])['sections'])
        ledger = self.root / 'private-inputs/ledger.json'
        ledger.write_bytes(canonical_json({'format': 'reveal.tool-ledger/1', 'complete': True, 'calls': []}))
        reviewer = EvidenceReader(review_evidence(package, ledger))
        self.assertEqual(reviewer.read('/package/user_inputs/uploads/0/content/segments/0')['value']['text'], 'First observation')

    def test_worker_kpn_forwards_frozen_inputs_and_checks_recovery(self):
        from reveal_backend import worker
        package = self.built().package
        gap = next(g for g in package['dapper_context']['knowledge_gaps'] if g['id'] == GAP_DAPPER_ID)
        frozen = {'question_id': GAP_DAPPER_ID, 'user_inputs': self.supplied_inputs(), 'composer': {
            'source_gap': {'source_id': GAP, 'source_revision': package['dismech']['source_revision']['source_sha256']},
            'eaggl_anchors': [{'reference': {'source_id': ANCHOR}, 'origin': 'user_supplied'}],
            'dismissed_source_ids': [], 'selected_kgs': ['biomarkerkg']}}
        binding = {'source_gap': {'object': gap}, 'anchors': [{'cfde_node_id': ANCHOR, 'model': KPN_MODEL,
                                                              'reference_generation_id': sha256(b'an earlier release')}]}
        captured = []

        def collect_reference(**kwargs):
            captured.append(kwargs)
            return reference.collect_reference_package(**{**kwargs, 'dismech_index': self.index,
                'dismech_source': self.source, 'connection_factory': lambda: SQLiteMySQL(self.database)})

        directory = self.root / 'worker-private-inputs'
        with patch.object(worker, 'collect_reference_package', side_effect=collect_reference), \
                patch.object(worker, 'DapperRuntime', return_value=self.runtime), \
                patch('reveal_backend.user_inputs.artifacts_root', return_value=self.root):
            path, collected = worker.collect({}, frozen, binding, {'candidates_per_type': 20}, directory)
        self.assertEqual(captured[0]['user_inputs'], frozen['user_inputs'])
        # The frozen binding's release is not a collection pin: evidence comes from the current release.
        self.assertFalse({'generation_id', 'release_id'} & set(captured[0]))
        self.assertEqual({node['fit']['upstream_build'] for node in collected['pigean']['mechanisms'].values()}, {RELEASE})
        self.assertEqual(collected['user_inputs']['context'], frozen['user_inputs']['context'])
        with patch.object(worker, 'collect_reference_package', side_effect=AssertionError('Recovery must use its capture')), \
                patch('reveal_backend.user_inputs.read', side_effect=AssertionError('Recovery must not reread mutable storage')):
            self.assertEqual(worker.collect({}, frozen, binding, {}, directory), (path, collected))
            modified = deepcopy(frozen); modified['user_inputs']['context'] = 'Replacement text'
            with self.assertRaisesRegex(EvidenceBuildError, 'researcher text changed'):
                worker.collect({}, modified, binding, {}, directory)
            modified = deepcopy(frozen); modified['user_inputs']['uploads'][0]['storage']['sha256'] = 'b' * 64
            with self.assertRaisesRegex(EvidenceBuildError, 'researcher attachment changed'):
                worker.collect({}, modified, binding, {}, directory)
            historical = deepcopy(frozen); historical.pop('user_inputs')
            with self.assertRaisesRegex(EvidenceBuildError, 'Unexpected researcher inputs'):
                worker.collect({}, historical, binding, {}, directory)

    def test_kpn_request_with_private_inputs_cannot_be_published(self):
        from reveal_backend import publication
        from reveal_backend.auth import Problem
        from reveal_backend.repository import Repository, digest
        repository = Repository(str(self.root / 'private-publication.sqlite')); repository.migrate()
        owner, account = 'private-owner', 'dapper:ScientificAccount.private-inputs-test'
        with repository.transaction() as tx:
            tx.put('request', 'private-request', owner, {'user_inputs': self.supplied_inputs()})
            tx.put('request_binding', 'private-request', owner, {'anchors': [{'model': KPN_MODEL,
                'reference_generation_id': RELEASE, 'cfde_node_id': ANCHOR}]})
            tx.put('job', 'private-job', owner, {'research_request_id': 'private-request'})
            tx.put('account', digest([owner, account]), owner, {'result': {}, 'summary': {'job_id': 'private-job'}})
            with self.assertRaises(Problem) as error:
                publication.change(tx, owner, account, 'public', 0)
            self.assertEqual(error.exception.code, 'PRIVATE_RESEARCH_INPUTS')
            self.assertEqual(tx.list('publication'), [])
            self.assertEqual(tx.list('publication_snapshot'), [])

    def test_reads_are_select_only_and_close_the_connection(self):
        self.collect('read-only', [ANCHOR])
        connection = self.connections[0]
        self.assertTrue(connection.statements)
        self.assertTrue(all(sql.startswith('SELECT ') for sql in connection.statements))
        self.assertTrue(connection.closed); self.assertEqual(connection.rollbacks, 1)

    def test_reads_use_the_environment_prefix_and_record_contract_tables(self):
        database = self.root / 'prefixed.sqlite3'; shutil.copyfile(self.database, database)
        db = sqlite3.connect(database)
        for table in TABLES: db.execute(f'ALTER TABLE reveal_ref_{table} RENAME TO reveal_workflow_qa_ref_{table}')
        db.commit(); db.close()
        with patch.dict('os.environ', {'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal_workflow_qa'}):
            package = self.collect('prefixed', [ANCHOR], database=database).package
        statements = self.connections[0].statements
        self.assertTrue(statements and all('reveal_workflow_qa_ref_' in sql and 'reveal_ref_' not in sql for sql in statements))
        captured = self.artifact(package, 'reference-factors', 'prefixed')
        self.assertEqual(captured['source']['tables'], ['reveal_ref_factors', 'reveal_ref_traits'])
        self.assertTrue(all('FROM reveal_ref_factors' in statement['sql'] for statement in captured['source']['statements']))
        # One release reads the same captured bytes whatever prefix serves it.
        self.assertEqual(package['source_artifacts']['reference-factors']['sha256'], self.single.package['source_artifacts']['reference-factors']['sha256'])

    def test_mechanisms_use_public_ids_and_catalog_mechanism_nodes(self):
        package = self.built().package
        mechanism = package['pigean']['mechanisms'][ANCHOR]
        self.assertEqual(mechanism['fit'], {'trait_group': 'kpn', 'phenotype': '0000398', 'model': KPN_MODEL, 'factor': 'Factor1',
                                            'trait_id': 'trait:kpn:0000398', 'upstream_build': RELEASE})
        node = mechanism_node(ANCHOR, TRAITS[CAD][1], CAD, 'Factor1', 'Hedgehog signalling')
        self.assertEqual(mechanism['dapper_id'], self.runtime.compute_id(node, 'Mechanism', self.runtime.schema))
        self.assertEqual(mechanism['display_name'], 'Coronary artery disease in type 2 diabetes mechanism Factor1')
        self.assertEqual(package['pigean']['model'], KPN_MODEL)
        row = self.artifact(package, mechanism['source_ref']['artifact_id'])['data'][int(mechanism['source_ref']['pointer'].rsplit('/', 1)[1])]
        self.assertEqual((row['public_id'], row['factor_key'], row['eaggl_factor_id']), (ANCHOR, factor_key(CAD, 'Factor1'), 'CADinT2D::Factor1'))
        self.assertEqual(self.runtime.resolver(package['prefixes']).expand(ANCHOR), 'urn:cfde:factor:kpn:0000398:eaggl-capped-v1:Factor1')

    def test_loadings_candidates_and_trait_observations_are_mysql_values(self):
        package = self.built().package; pigean = package['pigean']
        genes = pigean['mechanisms'][ANCHOR]['gene_loadings']
        self.assertEqual(genes['status'], 'ok')
        self.assertEqual({k: v['factor_value'] for k, v in genes['items'].items()}, {'gene:' + g: v for g, v in FACTORS[0][3].items()})
        self.assertTrue(all(v['reported_values_agree_within_1e_6'] for v in genes['items'].values()))
        sets = pigean['mechanisms'][ANCHOR]['gene_set_loadings']['items']
        self.assertEqual(sets['gene_set:' + self.gene_sets['A']]['factor_value'], .9)
        self.assertEqual(sets['gene_set:' + self.gene_sets['D']]['interactive_observations'][0]['raw_score'], 0.)
        self.assertEqual(list(pigean['mechanisms'][ANCHOR]['trait_loadings']['items']), ['trait:kpn:0000398'])
        factors = {k: c for k, c in pigean['candidates'].items() if c['node']['node_type'] == 'factor'}
        self.assertEqual(set(factors), {SECOND, SIBLING, public_id(T2D, 'Factor2')})
        # min(anchor, factor) over the gene candidates, divided by the anchor's candidate-gene loading.
        self.assertAlmostEqual(factors[SECOND]['candidate']['aggregate_score'], (.6 + .1) / 2.4)
        self.assertEqual(pigean['candidates']['trait:kpn:0000398']['node']['label'], TRAITS[CAD][1])
        trait = pigean['traits']['trait:kpn:0000398']
        observations = trait['gene_associations']['items']['gene:SHH']['observations']
        # Only the anchor's own factor-scope observation, with no PIGEAN trait metrics (MySQL holds none; never zero).
        self.assertEqual([(o['reported_metrics'], o['ascertained_via_mechanism']) for o in observations], [({}, ANCHOR)])
        # Other factors' loadings are never presented as phenotype-query trait associations.
        self.assertFalse([o for t in pigean['traits'].values() for kind in ('gene_associations', 'gene_set_associations')
                          for item in t[kind]['items'].values() for o in item['observations'] if o.get('source_scope') == 'phenotype_query'])
        self.assertFalse([key for key in package['source_artifacts'] if key.startswith(('gene-trait-', 'gene_set-trait-', 'trait-factors-'))])
        self.assertGreater(pigean['contextual_relationships']['new_unique_edges'], 0)
        families = {edge['family'] for edge in pigean['graph']['edges']}
        self.assertEqual(families, {'factor_gene_direct', 'factor_gene_set_direct', 'factor_trait_direct', 'factor_factor_shared_genes'})

    def test_gene_sets_bind_aliases_and_carry_exact_cfde_dapper_objects(self):
        package = self.built().package
        context = {node['id']: node for node in package['dapper_context']['gene_sets']}
        resolution = {row['gene_set_id']: row for row in self.artifact(package, 'gene-set-resolution')['gene_sets']}
        for key in 'AD':
            gene_set_id = self.gene_sets[key]; binding = package['entities']['gene_sets']['gene_set:' + gene_set_id]
            alias = context[binding['dapper_id']]
            self.assertEqual(alias['alternate_identifier'], ['gene_set:' + gene_set_id])
            self.assertEqual(alias['was_derived_from'], [gene_set_id])
            exact = context[gene_set_id]
            self.assertEqual(self.runtime.compute_id({k: v for k, v in exact.items() if k != 'id'}, 'GeneSet', self.runtime.schema), gene_set_id)
            self.assertFalse({'in_gene_set_collection', 'in_gmt_file', 'gmt_entry', 'has_embedding'} & set(exact))
            self.assertEqual((binding['membership_status'], binding['construction_provenance_status']), ('loaded', 'generating_activity_loaded'))
            self.assertEqual(binding['import_id'], RELEASE)
            self.assertEqual(resolution[gene_set_id]['status'], 'exact_dapper_gene_set')
        self.assertEqual([a['id'] for a in package['dapper_context']['activities']], [self.activity['id']])
        self.assertEqual(package['prefixes']['HGNC.SYMBOL'], 'https://identifiers.org/hgnc.symbol:')
        self.assertNotIn('humgen', package['prefixes'])
        for key, reason in [('B', 'not stored'), ('C', 'DAPPER validation failed')]:
            binding = package['entities']['gene_sets']['gene_set:' + self.gene_sets[key]]
            self.assertEqual(binding['membership_status'], 'not_loaded')
            self.assertNotIn('was_derived_from', context[binding['dapper_id']])
            self.assertNotIn(self.gene_sets[key], context)
            self.assertIn(reason, resolution[self.gene_sets[key]]['reason'])
        collections = self.artifact(package, 'gene-set-collections')['data']
        self.assertEqual(set(collections[0]), {'collection_id', 'cfde_label', 'library', 'n_sets', 'provenance'})
        self.assertEqual(len(collections[0]['provenance']['activities']), 2)  # only the referenced Activity joins the package

    def test_captures_record_sql_origin_and_release(self):
        package = self.built().package
        mysql = {k: v for k, v in package['source_artifacts'].items() if isinstance(v.get('origin'), str) and v['origin'].startswith('mysql')}
        self.assertEqual({k for k in mysql if not k.startswith(('gene-factor-', 'gene_set-factor-', 'gene-trait-', 'gene_set-trait-', 'trait-factors-', 'connections-'))},
                         {'reference-release', 'reference-factors', 'factor-gene-overlap', 'candidate-factors',
                          'gene-set-payloads', 'gene-set-collections', 'gene-set-resolution', 'contextual', 'contextual-gene-set-projections'})
        for key, descriptor in mysql.items():
            self.assertTrue(descriptor['origin'].endswith('?release_id=' + RELEASE), key)
            capture = self.artifact(package, key)
            self.assertEqual((capture['release_id'], capture['model']), (RELEASE, KPN_MODEL))
            self.assertNotIn('generation_id', capture)
            if descriptor['origin'].startswith('mysql:'):
                self.assertEqual((capture['source']['kind'], capture['format']), ('mysql', 'reveal.reference-evidence.mysql-capture/2'))
                self.assertEqual(descriptor['origin'].split('?')[0], 'mysql:' + '+'.join(capture['source']['tables']))
                self.assertTrue(all(table.startswith('reveal_ref_') for table in capture['source']['tables']))
                self.assertTrue(all(s['sql'].startswith('SELECT ') for s in capture['source']['statements']))
            else:
                self.assertEqual((capture['source']['kind'], capture['format']), ('mysql-derived', 'reveal.reference-evidence.derived-capture/2'))
                self.assertTrue(set(capture['source']['derived_from']) <= set(package['source_artifacts']))
        release = self.artifact(package, 'reference-release')['data']
        self.assertEqual([(row['release_id'], row['published_at'], row['manifest']['embedding']['dimensions']) for row in release],
                         [(RELEASE, '2026-10-05T12:00:00Z', 2)])
        # The lint recognizes every captured reference observation of the release.
        recognized = cfde_source_files(package, self.root / 'single/package/evidence-package.json')
        self.assertEqual({package['source_artifacts'][key]['dapper_file_id'] for key in mysql} - recognized, set())
        connections = self.artifact(package, 'connections-gene')
        self.assertNotIn('url', connections)
        self.assertEqual((connections['method'], connections['status'], connections['request']['model']), ('POST', 200, KPN_MODEL))
        self.assertEqual(package['source_artifacts']['collector-source']['filename'], 'reference_evidence.py')
        rows = self.artifact(package, 'gene-factor-' + sha256(canonical_json(ANCHOR))[:12])
        self.assertEqual((rows['index'], rows['q'], rows['limit']), ('pigean-gene-factor', ['0000398', KPN_MODEL, 'Factor1'], None))
        self.assertEqual(rows['data'][0], {'phenotype': '0000398', 'trait_group': 'kpn', 'gene_set_size': KPN_MODEL, 'factor': 'Factor1',
                                           'gene': 'SHH', 'factor_value': .9})
        projections = self.artifact(package, 'gene_set-factor-' + sha256(canonical_json(ANCHOR))[:12])
        self.assertEqual(projections['scope']['projection_scope'], 'per_library')
        self.assertEqual({row['library'] for row in projections['data']}, {'GTEx'})

    def test_replay_from_build_input_is_byte_identical(self):
        built = self.built()
        spec, blobs = load_build_input(self.root / 'single/build-input.json', self.root / 'single')
        with patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network')):
            self.assertEqual(build_package(spec, blobs, self.runtime).files, built.files)
        missing = deepcopy(spec); missing['captures']['connections']['gene'] = {'status': 'not_captured'}
        with self.assertRaises(EvidenceBuildError): build_package(missing, blobs, self.runtime)
        # The incomplete-capture policy exists only for the trait-scope queries MySQL cannot answer.
        strict = deepcopy(spec); strict['policy']['allow_incomplete_capture'] = False
        with self.assertRaisesRegex(EvidenceBuildError, 'Required BioIndex query unavailable: trait/trait:kpn:0000398/gene'):
            build_package(strict, blobs, self.runtime)

    def test_multiple_anchors_across_traits(self):
        package = self.collect('two-traits', [SECOND, ANCHOR]).package
        validate_package_shape(package, SCHEMA)
        self.assertEqual(package['selection']['eaggl_mechanism_ids'], sorted([ANCHOR, SECOND]))
        self.assertEqual(set(package['pigean']['traits']), {'trait:kpn:0000398', 'trait:kpn:0000319'})
        shh = package['pigean']['candidates']['gene:SHH']['candidate']
        self.assertEqual((shh['support_anchor_count'], shh['anchor_count']), (2, 2))
        self.assertAlmostEqual(shh['aggregate_score'], (.9 + .1) / 2)
        self.assertEqual(package['pigean']['candidates']['trait:kpn:0000319']['candidate']['aggregate_score'], .5)
        self.assertNotIn(SECOND, package['pigean']['candidates'])
        observations = package['pigean']['traits']['trait:kpn:0000398']['gene_associations']['items']['gene:SHH']['observations']
        self.assertEqual([o.get('ascertained_via_mechanism', 'trait') for o in observations], [ANCHOR])
        self.assertEqual(package['readiness']['capture_blockers'], [f'bioindex:trait:trait:kpn:{number}:{kind}:not_captured'
                                                                    for number in ('0000319', '0000398') for kind in ('gene', 'gene_set')])
        self.assertTrue(dispatchable_capture(package))

    def test_byte_budget_drops_lowest_ranked_candidates_and_keeps_anchors(self):
        full = self.built()
        with patch.object(reference, 'MAX_PACKAGE_BYTES', len(full.files['evidence-package.json']) - 100):
            package = self.collect('budget', [ANCHOR]).package
        self.assertIn(ANCHOR, package['pigean']['graph']['node_ids'])
        self.assertLess(package['coverage']['retained_nodes'], full.package['coverage']['retained_nodes'])
        self.assertTrue(package['coverage']['omitted_node_ids'])
        with patch.object(reference, 'MAX_PACKAGE_BYTES', 1000), self.assertRaisesRegex(EvidenceBuildError, 'byte budget'):
            self.collect('budget-too-small', [ANCHOR])

    def test_rejections_before_or_during_reads(self):
        with self.assertRaisesRegex(EvidenceBuildError, 'KPN factor public id'):
            self.collect('legacy-id', ['factor:portal:CADinT2D:cfde-inc-v2:Factor1'])
        with self.assertRaisesRegex(EvidenceBuildError, 'not in the current reference release'):
            self.collect('unknown-factor', [public_id(CAD, 'Factor9')])
        self.assertTrue(self.connections[0].closed)
        self.assertIn('not in the current reference release', decode((self.root / 'unknown-factor/collection-error.json').read_bytes())['error'])
        with self.assertRaisesRegex(EvidenceBuildError, 'Invalid reference release id'):
            self.collect('bad-release', [ANCHOR], release_id='legacy')
        with self.assertRaisesRegex(EvidenceBuildError, 'published reference release is ' + RELEASE):
            self.collect('other-release', [ANCHOR], release_id='f' * 64)
        self.assertEqual(self.collect('same-release', [ANCHOR], release_id=RELEASE).package['pigean']['mechanisms'][ANCHOR]['fit']['upstream_build'], RELEASE)
        for name, change, message in [('unpublished', 'DELETE FROM reveal_ref_release', 'No reference release is published'),
                                      ('missing', 'DELETE FROM reveal_ref_projections', None)]:
            database = self.root / f'{name}.sqlite3'; shutil.copyfile(self.database, database)
            db = sqlite3.connect(database); db.execute(change); db.commit(); db.close()
            if message:
                with self.subTest(name=name), self.assertRaisesRegex(EvidenceBuildError, message):
                    self.collect('status-' + name, [ANCHOR], database=database)
            else:
                package = self.collect('status-' + name, [ANCHOR], database=database).package
                self.assertEqual(package['coverage']['queries']['gene_set'], 'empty')
                self.assertEqual(package['pigean']['mechanisms'][ANCHOR]['gene_set_loadings']['status'], 'empty')
                validate_package_shape(package, SCHEMA)

    @unittest.skipUnless(CFDE_YAML.is_file(), 'CFDE gene-set snapshot is not mounted')
    def test_real_cfde_collection_gene_set_reproduces_its_dapper_id(self):
        import yaml
        document = yaml.safe_load(CFDE_YAML.read_text())
        header = {key: document.get(key, []) for key in ('organizations', 'datasets', 'files', 'activities')}
        prefixes = self.single.package['prefixes']
        for node in document['gene_sets']:
            row = {'gene_set_id': node['id'], 'metadata': {'dapper_gene_set': json.loads(json.dumps(node))}}
            exact, dependencies, used, reason = reference._resolve_gene_set(
                self.runtime, row, {'provenance': {'prefixes': document['prefixes'], **header}}, prefixes, reference._class_groups(self.runtime))
            self.assertIsNone(reason)
            self.assertEqual(exact['id'], node['id'])
            self.assertEqual(self.runtime.compute_id({k: v for k, v in exact.items() if k != 'id'}, 'GeneSet', self.runtime.schema), node['id'])
            self.assertEqual([item['id'] for item in dependencies], [node['was_generated_by']])
            self.assertEqual(used, {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:'})

    def test_collected_package_retains_shared_anonymous_provenance_edges(self):
        database = self.root / 'with-provenance.sqlite3'
        shutil.copyfile(self.database, database)
        dataset = {'name': 'raw measurements'}
        dataset['id'] = self.runtime.compute_id(dataset, 'Dataset', self.runtime.schema)
        edge = {'subject': self.activity['id'], 'predicate': 'prov:used', 'object': dataset['id'], 'edge_role': 'data_input'}
        with sqlite3.connect(database) as db:
            payload = json.loads(db.execute('SELECT payload FROM reveal_ref_collections').fetchone()[0])
            payload['provenance'].update(datasets=[dataset], used_edges=[edge])
            db.execute('UPDATE reveal_ref_collections SET payload=?', (json.dumps(payload),))
        package = self.collect('preserved-edges', [ANCHOR], database=database).package
        self.assertEqual(package['dapper_context']['used_edges'], [edge])
        self.assertEqual(package['dapper_context']['datasets'], [dataset])
        self.runtime.validate(package['dapper_context'])
        validate_package_shape(package, SCHEMA)

    def test_selection_provenance_is_frozen_like_collect_package(self):
        metadata = {'origins': {ANCHOR: 'automatic'}, 'dismissed_eaggl_ids': [SIBLING],
                    'semantic_retrieval': {'status': 'not_computed', 'embedding_run_id': None}, 'frozen_binding': {'anchors': []}}
        package = self.collect('selection', [ANCHOR], selection_metadata=metadata).package
        self.assertEqual(package['selection']['origins'], {ANCHOR: 'automatic'})
        self.assertEqual(package['selection']['dismissed_eaggl_ids'], [SIBLING])
        self.assertEqual(self.artifact(package, 'selection-provenance', 'selection'), metadata)
        with self.assertRaisesRegex(EvidenceBuildError, 'Selection provenance differs'):
            self.collect('selection-mismatch', [ANCHOR], selection_metadata={**deepcopy(metadata), 'origins': {SECOND: 'manual'}})


if __name__ == '__main__':
    unittest.main()
