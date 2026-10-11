"""Hand-curated collection lineage: guarded plan/apply/rollback and the traces it enables."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from reveal_backend import collection_lineage as lineage
from reveal_backend import provenance_reference as reference
from reveal_backend.account_provenance import collect
from reveal_backend.acceptance import public_runtime

GEN = 'a' * 64
OTHER = 'b' * 64
IMPORT = 'c' * 64


class Cursor:
    def __init__(self, connection): self.cursor = connection.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, sql, args=()): self.cursor.execute(sql.replace('%s', '?'), tuple(args))
    def fetchall(self): return self.cursor.fetchall()
    @property
    def rowcount(self): return self.cursor.rowcount


class Connection:
    """sqlite stand-in for the MySQL calls the module makes (JSON_UNQUOTE, GET_LOCK, rowcount, commit)."""
    def __init__(self, path, lock=1):
        self.c = sqlite3.connect(path)
        self.c.create_function('JSON_UNQUOTE', 1, lambda value: value)
        self.c.create_function('GET_LOCK', 2, lambda name, wait: lock)
        self.c.create_function('RELEASE_LOCK', 1, lambda name: 1)
    def cursor(self): return Cursor(self.c)
    def commit(self): self.c.commit()
    def rollback(self): self.c.rollback()
    def close(self): self.c.close()


class EmptyTx:
    def get(self, *args): return None


class CollectionLineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.path, self.curation_path, self.backups = str(root / 'refs.sqlite'), root / 'lineage.json', root / 'backups'
        self.runtime = runtime = public_runtime()
        def node(cls, **fields): return {**fields, 'id': runtime.compute_id(fields, cls, runtime.schema)}
        self.broad, self.gtex_org = node('Organization', name='Broad Institute'), node('Organization', name='GTEx Consortium')
        self.ncbi, self.ismms = node('Organization', name='NCBI'), node('Organization', name='ISMMS')
        self.gtex = node('Dataset', name='GTEx V8 bulk RNA-seq', publisher=self.broad['id'], was_attributed_to=[self.gtex_org['id']])
        self.gene_info = node('Dataset', name='NCBI gene_info', publisher=self.ncbi['id'])
        self.lincs = node('Dataset', name='LINCS L1000 signatures', publisher=self.ismms['id'])
        libraries = {name: node('Dataset', name='dig-gene-set-filtered/' + name) for name in ('GTEx', 'LINCS_L1000', 'LIGER')}
        self.libraries = libraries
        prefixes = {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:'}
        self.collections, self.sets = {}, {}
        for collection_id, library, datasets, organizations in [
                ('gtex-1', 'GTEx', [libraries['GTEx'], self.gtex, self.gene_info], [self.broad, self.gtex_org, self.ncbi]),
                ('gtex-2', 'GTEx', [libraries['GTEx'], self.gtex, self.gene_info], [self.broad, self.gtex_org, self.ncbi]),
                ('lincs-1', 'LINCS_L1000', [libraries['LINCS_L1000'], self.lincs], [self.ismms]),
                ('liger-1', 'LIGER', [libraries['LIGER']], None)]:
            activity = node('Activity', name='convert ' + collection_id)
            gene_set = node('GeneSet', name='set ' + collection_id, member_type='gene', members=['HGNC.SYMBOL:UBA1'],
                            was_generated_by=activity['id'])
            self.sets[collection_id] = gene_set
            self.collections[collection_id] = (library, {'collection': {'id': 'dapper:GeneSetCollection.' + collection_id},
                'index': {'label': collection_id}, 'document_sha256': 'd' * 64,
                'provenance': {'prefixes': prefixes, 'organizations': organizations, 'datasets': datasets,
                               'files': [], 'activities': [activity]}})
        self.curation = {'format': lineage.FORMAT, 'curation_id': 'curation-test', 'generation_id': GEN, 'policy': 'test',
            'libraries': {
                'GTEx': {'excluded': [{'dataset_id': libraries['GTEx']['id'], 'name': libraries['GTEx']['name'], 'reason': 'intermediate'}],
                         'inputs': [{'dataset_id': self.gtex['id'], 'name': self.gtex['name'], 'edge_role': 'data_input'},
                                    {'dataset_id': self.gene_info['id'], 'name': self.gene_info['name'], 'edge_role': 'metadata_input'}]},
                'LINCS_L1000': {'excluded': [{'dataset_id': libraries['LINCS_L1000']['id'], 'name': libraries['LINCS_L1000']['name'], 'reason': 'intermediate'}],
                         'inputs': [{'dataset_id': self.lincs['id'], 'name': self.lincs['name'], 'edge_role': 'data_input'}]},
                'LIGER': {'excluded': [{'dataset_id': libraries['LIGER']['id'], 'name': libraries['LIGER']['name'], 'reason': 'intermediate'}],
                          'inputs': [], 'unresolved_reason': 'no upstream recorded'}}}
        self.write_curation()
        with sqlite3.connect(self.path) as c:
            c.executescript('''
CREATE TABLE reference_generations(generation_id TEXT,kind TEXT,model TEXT,status TEXT,eaggl_import_id TEXT,legacy_mapping_run_id TEXT,legacy_gene_set_import_id TEXT,manifest TEXT);
CREATE TABLE cfde_gene_set_collections(generation_id TEXT,collection_id TEXT,cfde_label TEXT,library TEXT,payload TEXT);
CREATE TABLE cfde_gene_sets(generation_id TEXT,gene_set_id TEXT,collection_id TEXT,metadata TEXT);
CREATE TABLE reveal_records(kind TEXT,id TEXT,payload TEXT);
CREATE TABLE reveal_workflow_qa_records(kind TEXT,id TEXT,payload TEXT);
CREATE TABLE reveal_workflow_local_records(kind TEXT,id TEXT,payload TEXT);''')
            for generation, status in [(GEN, 'complete'), (OTHER, 'superseded')]:
                c.execute('INSERT INTO reference_generations VALUES(?,?,?,?,?,?,?,?)',
                    (generation, 'kpn-eaggl-capped', 'eaggl-capped-v1', status, IMPORT, None, None, '{}'))
            for collection_id, (library, payload) in self.collections.items():
                c.execute('INSERT INTO cfde_gene_set_collections VALUES(?,?,?,?,?)', (GEN, collection_id, collection_id, library, json.dumps(payload)))
                gene_set = self.sets[collection_id]
                c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?)', (GEN, gene_set['id'], collection_id, json.dumps({'dapper_gene_set': gene_set})))
            c.execute('INSERT INTO cfde_gene_set_collections VALUES(?,?,?,?,?)', (OTHER, 'gtex-1', 'gtex-1', 'GTEx', '{"provenance":{}}'))
            for table in ('reveal_workflow_qa_records', 'reveal_workflow_local_records'):
                c.execute(f'INSERT INTO {table} VALUES(?,?,?)', ('reference_active', 'active', json.dumps({'generation_id': GEN})))

    def write_curation(self):
        self.curation_path.write_text(json.dumps(self.curation))

    def connection(self, **kwargs):
        connection = Connection(self.path, **kwargs); self.addCleanup(connection.close)
        return connection

    def dump(self):
        with sqlite3.connect(self.path) as c: return list(c.iterdump())

    def payload(self, collection_id, generation=GEN):
        with sqlite3.connect(self.path) as c:
            return json.loads(c.execute('SELECT payload FROM cfde_gene_set_collections WHERE generation_id=? AND collection_id=?',
                                        (generation, collection_id)).fetchone()[0])

    def plan(self):
        return lineage.plan_from(self.connection(), self.curation_path, self.runtime)[0]

    def apply(self, plan=None, **kwargs):
        plan = plan or self.plan()
        return lineage.apply(self.connection(), self.curation_path, expect_plan=plan['plan_sha256'], backup_dir=self.backups,
                             runtime=self.runtime, **kwargs)

    def test_plan_reads_only_and_classifies_every_collection(self):
        before = self.dump()
        plan = self.plan()
        self.assertEqual(self.dump(), before)
        self.assertEqual(plan['summary'], {'GTEx': {'add': 2, 'edges': 4}, 'LIGER': {'skip': 1, 'edges': 0}, 'LINCS_L1000': {'add': 1, 'edges': 1}})
        self.assertEqual(plan['verification'], {'validated_collections': 3, 'representatives_checked': 3, 'shared_nodes_checked': 10})
        self.assertEqual(plan['active_generations'], {'local': GEN, 'qa': GEN, 'prod': None})
        gtex = next(entry for entry in plan['collections'] if entry['collection_id'] == 'gtex-1')
        activity = self.collections['gtex-1'][1]['provenance']['activities'][0]['id']
        self.assertEqual(sorted(gtex['edges'], key=json.dumps), sorted([
            {'subject': activity, 'predicate': 'prov:used', 'object': self.gtex['id'], 'edge_role': 'data_input'},
            {'subject': activity, 'predicate': 'prov:used', 'object': self.gene_info['id'], 'edge_role': 'metadata_input'}],
            key=json.dumps))
        self.assertEqual(lineage.public(plan)['collections'][0].keys() & {'payload'}, set())

    def test_apply_backs_up_first_then_adds_edges_and_is_idempotent(self):
        result = self.apply()
        self.assertEqual(result['applied'], 3)
        with gzip.open(result['backup'], 'rb') as handle: backup = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual({row['collection_id'] for row in backup}, {'gtex-1', 'gtex-2', 'lincs-1'})
        self.assertTrue(all(row['payload'] == self.collections[row['collection_id']][1] for row in backup))
        patched = self.payload('gtex-1')
        original = self.collections['gtex-1'][1]
        self.assertEqual(len(patched['provenance']['used_edges']), 2)
        self.assertEqual(patched['lineage_curation']['curation_id'], 'curation-test')
        self.assertEqual({k: v for k, v in patched.items() if k != 'lineage_curation'},
                         {**original, 'provenance': {**original['provenance'], 'used_edges': patched['provenance']['used_edges']}})
        self.assertEqual(self.payload('liger-1'), self.collections['liger-1'][1])
        self.assertEqual(self.payload('gtex-1', OTHER), {'provenance': {}})
        plan = self.plan()
        self.assertEqual({entry['action'] for entry in plan['collections']}, {'unchanged', 'skip'})
        before = self.dump()
        again = self.apply(plan)
        self.assertEqual((again['applied'], again['backup']), (0, None))
        self.assertEqual(self.dump(), before)

    def test_refusals_leave_the_database_unchanged(self):
        before = self.dump()
        def refused(message, mutate=None, curation=None, **kwargs):
            with self.subTest(message=message):
                self.setUp()
                if mutate:
                    with sqlite3.connect(self.path) as c: mutate(c)
                if curation:
                    curation(self.curation); self.write_curation()
                snapshot = self.dump()
                with self.assertRaises(lineage.LineageRefused) as raised:
                    plan = kwargs.pop('plan', None)
                    self.apply(plan, **kwargs) if plan or kwargs else self.plan()
                self.assertIn(message, str(raised.exception))
                self.assertEqual(self.dump(), snapshot)
        def payload_update(collection_id, change):
            def mutate(c):
                payload = json.loads(c.execute('SELECT payload FROM cfde_gene_set_collections WHERE generation_id=? AND collection_id=?',
                                               (GEN, collection_id)).fetchone()[0])
                change(payload)
                c.execute('UPDATE cfde_gene_set_collections SET payload=? WHERE generation_id=? AND collection_id=?',
                          (json.dumps(payload), GEN, collection_id))
            return mutate
        unknown = {'id': 'dapper:Dataset.' + 'u' * 32, 'name': 'unknown'}
        refused('unclassified upstream dataset', payload_update('gtex-1', lambda p: p['provenance']['datasets'].append(unknown)))
        refused('name differs', payload_update('gtex-1', lambda p: p['provenance']['datasets'][1].update(name='renamed')))
        refused('is not stored', payload_update('lincs-1', lambda p: p['provenance'].update(activities=[])))
        refused('already has different used_edges', payload_update('gtex-1', lambda p: p['provenance'].update(used_edges=[])))
        refused('has no gene sets', curation=lambda c: c.update(generation_id=OTHER))  # OTHER stores no curated collection
        refused('is not curated', curation=lambda c: c['libraries'].pop('LINCS_L1000'))
        refused('is stored in no collection', curation=lambda c: c['libraries']['GTEx']['inputs'].append(
            {'dataset_id': 'dapper:Dataset.' + 's' * 32, 'name': 'stale', 'edge_role': 'data_input'}))
        refused('Unsupported edge role', curation=lambda c: c['libraries']['GTEx']['inputs'][0].update(edge_role='data_output'))
        refused('not complete', lambda c: c.execute("UPDATE reference_generations SET status='loading' WHERE generation_id=?", (GEN,)))
        refused('Only a KPN', lambda c: c.execute("UPDATE reference_generations SET model='cfde-inc-v2' WHERE generation_id=?", (GEN,)))
        refused('unstored organization', payload_update('gtex-1', lambda p: p['provenance'].update(
            organizations=[org for org in p['provenance']['organizations'] if org['name'] != 'NCBI'])))
        # A changed shared Dataset no longer matches its content-addressed id, so evidence exactness refuses it first.
        refused('would no longer be exact', payload_update('gtex-2', lambda p: p['provenance']['datasets'][1].update(description='variant')))
        refused('The plan changed', plan={'plan_sha256': '0' * 64})
        refused('Production serves this generation',
                lambda c: c.execute('INSERT INTO reveal_records VALUES(?,?,?)', ('reference_active', 'active', json.dumps({'generation_id': GEN}))),
                plan=None, allow_production=False)
        self.setUp()
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO reveal_records VALUES(?,?,?)', ('reference_active', 'active', json.dumps({'generation_id': GEN})))
        self.assertEqual(self.apply(allow_production=True)['applied'], 3)
        self.setUp()
        with self.assertRaises(lineage.LineageRefused):
            lineage.apply(self.connection(lock=0), self.curation_path, expect_plan=self.plan()['plan_sha256'],
                          backup_dir=self.backups, runtime=self.runtime)

    def test_rollback_restores_exactly_and_refuses_drift(self):
        result = self.apply()
        report = lineage.rollback(self.connection(), result['backup'])
        self.assertEqual((report['restore'], report['applied']), (3, False))
        self.assertNotEqual(self.payload('gtex-1'), self.collections['gtex-1'][1])
        self.assertEqual(lineage.rollback(self.connection(), result['backup'], apply=True)['applied'], True)
        for collection_id, (library, payload) in self.collections.items():
            self.assertEqual(self.payload(collection_id), payload)
        self.assertEqual(lineage.rollback(self.connection(), result['backup'], apply=True)['already_restored'], 3)
        result = self.apply()
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE cfde_gene_set_collections SET payload=? WHERE generation_id=? AND collection_id=?',
                      (json.dumps({'provenance': {'datasets': []}}), GEN, 'lincs-1'))
        with self.assertRaises(lineage.LineageRefused):
            lineage.rollback(self.connection(), result['backup'], apply=True)

    def test_curated_collections_trace_referenced_gene_sets_to_datasets_and_organizations(self):
        self.apply()
        account, claim, proposition = 'dapper:ScientificAccount.a', 'dapper:Claim.c', 'dapper:Proposition.p'
        def trace(collection_id):
            document = {'scientific_accounts': [{'id': account, 'component_claims': [claim], 'conclusion_claims': [claim]}],
                'claims': [{'id': claim, 'proposition': proposition}],
                'propositions': [{'id': proposition, 'subject_entity': self.sets[collection_id]['id']}]}
            enriched, metadata = reference.expand(document, {}, tx=EmptyTx(), connection_factory=lambda: Connection(self.path))
            enriched['_provenance_coverage'] = metadata
            return collect(enriched, account)
        result = trace('gtex-1')
        self.assertEqual({row['dataset_id'] for row in result['dataset_reuse']}, {self.gtex['id'], self.gene_info['id']})
        self.assertEqual({row['organization_id'] for row in result['organization_reuse']},
                         {self.broad['id'], self.gtex_org['id'], self.ncbi['id']})
        roles = {row['dataset_id']: row['input_roles'] for row in result['traces']}
        self.assertEqual(roles[self.gtex['id']], ['data_input'])
        used = [edge for edge in result['edges'] if edge['relation'] == 'used']
        self.assertTrue(used and all(edge['provenance_source'][0]['kind'] == 'curated_lineage' for edge in used))
        liger = trace('liger-1')
        self.assertEqual(liger['dataset_reuse'], [])
        self.assertTrue(any(row['code'] == 'MISSING_LINEAGE' for row in liger['coverage']['issues']))


if __name__ == '__main__': unittest.main()
