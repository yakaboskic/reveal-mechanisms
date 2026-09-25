import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from reveal_backend import eaggl_cfde_links as links

ROOT = Path(__file__).resolve().parents[3]


def source(trait='Trait', number=1, index=0):
    return {'factor_index': index, 'factor_id': f'{trait}::Factor{number}', 'trait': trait,
            'metadata': {'factor': f'Factor{number}', 'factor_number': str(number), 'label': 'EAGGL wording',
                         'top_genes': 'A,B,C', 'top_gene_sets': 'legacy-only'}}


def target(trait='Trait', number=1):
    return {'id': f'cfde:cfde-inc-v2:{trait}:Factor{number}', 'phenotype_key': trait,
            'raw': {'factor': f'Factor{number}', 'gene_set_size': 'cfde-inc-v2', 'phenotype': trait,
                    'trait_group': 'portal', 'label': 'Different CFDE wording', 'top_genes': 'X;Y;Z',
                    'top_gene_sets': 'set-A;not-loaded'}}


class MatchingTests(unittest.TestCase):
    def test_labels_genes_and_gene_sets_do_not_gate_identifier_match(self):
        matched, missing = links.match_factors([source()], [target()], 'cfde-inc-v2')
        self.assertFalse(missing)
        self.assertEqual(matched[0]['cfde_node_id'], 'factor:portal:Trait:cfde-inc-v2:Factor1')
        self.assertEqual(matched[0]['gene_sets'], ['set-A', 'not-loaded'])

    def test_missing_trait_and_number_have_separate_reasons(self):
        matched, missing = links.match_factors([source(number=2), source(trait='Other', index=1)], [target()], 'cfde-inc-v2')
        self.assertFalse(matched)
        self.assertEqual([r['reason'] for r in missing], ['factor_number_not_in_cfde_catalog', 'trait_not_in_cfde_catalog'])

    def test_trait_matching_is_case_sensitive_and_model_scoped(self):
        matched, _ = links.match_factors([source(trait='trait')], [target()], 'cfde-inc-v2')
        self.assertFalse(matched)
        with self.assertRaisesRegex(ValueError, 'model mismatch'):
            links.match_factors([source()], [target()], 'cfde')

    def test_ambiguous_target_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Ambiguous CFDE'):
            links.match_factors([source()], [target(), target()], 'cfde-inc-v2')

    def test_factor_number_metadata_must_be_consistent(self):
        row = source(); row['metadata']['factor_number'] = '2'
        with self.assertRaisesRegex(ValueError, 'disagree'):
            links.match_factors([row], [target()], 'cfde-inc-v2')

    def test_no_label_based_fallback(self):
        row = source(number=2)
        row['metadata']['label'] = target()['raw']['label']
        matched, missing = links.match_factors([row], [target()], 'cfde-inc-v2')
        self.assertFalse(matched); self.assertEqual(len(missing), 1)

    def test_invalid_catalog_hash(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            (p / 'manifest.json').write_text(json.dumps({'complete': True, 'sha256': 'bad', 'factor_records': 0}))
            (p / 'factors.jsonl.gz').write_bytes(gzip.compress(b''))
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                links.read_catalog(p)


@unittest.skipUnless(os.getenv('REVEAL_TEST_MYSQL_PORT'), 'Disposable MySQL port not configured')
class MappingMySQLTests(unittest.TestCase):
    def setUp(self):
        import pymysql
        self.c = pymysql.connect(host='127.0.0.1', port=int(os.environ['REVEAL_TEST_MYSQL_PORT']), user='root',
            password=os.getenv('REVEAL_TEST_MYSQL_PASSWORD', 'local-test-only'), database='cyaka_dismech_test',
            charset='utf8mb4', autocommit=False)
        self.addCleanup(self.c.close)
        self.source_id, self.genes_id = links.digest(str(uuid.uuid4())), links.digest(str(uuid.uuid4()))
        dapper_id = 'test:GeneSet.' + uuid.uuid4().hex
        with self.c.cursor() as q:
            for filename in ['001_gene_set_inventory.sql', '002_eaggl_factors.sql']:
                sql = '\n'.join(line for line in (ROOT / 'schema/migrations' / filename).read_text().splitlines() if not line.lstrip().startswith('--'))
                for statement in sql.split(';'):
                    if statement.strip(): q.execute(statement)
            q.execute("INSERT INTO eaggl_imports (import_id,source_namespace,source_version,status,manifest,progress) VALUES (%s,'test','1','complete','{}','{}')", (self.source_id,))
            for row in [source(), source(number=2, index=1), source(trait='Other', index=2)]:
                q.execute('INSERT INTO eaggl_factors (import_id,factor_index,factor_id,factor_id_sha256,trait,label,input_sha256,metadata) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                    (self.source_id, row['factor_index'], row['factor_id'], links.digest(row['factor_id']), row['trait'],
                     row['metadata']['label'], links.digest(row['metadata']['label']), links.canonical(row['metadata'])))
            q.execute("INSERT INTO gene_set_imports (import_id,model,status,loaded_rows,expected_rows,manifest) VALUES (%s,'cfde-inc-v2','complete',1,1,'{}')", (self.genes_id,))
            q.execute("INSERT INTO dapper_objects (id,class_name,identity_profile,payload_sha256,payload) VALUES (%s,'GeneSet','test',%s,'{}')", (dapper_id, links.digest('{}')))
            q.execute("INSERT INTO cfde_gene_set_aliases (import_id,node_id_sha256,model,source_key,node_id,dapper_id,provenance) VALUES (%s,%s,'cfde-inc-v2','set-A','gene_set:set-A',%s,'{}')", (self.genes_id, links.digest('gene_set:set-A'), dapper_id))
        self.c.commit()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        raw = gzip.compress((links.canonical(target()) + '\n').encode(), mtime=0)
        (directory / 'factors.jsonl.gz').write_bytes(raw)
        (directory / 'manifest.json').write_text(links.canonical({'complete': True, 'sha256': links.digest(raw), 'factor_records': 1, 'model': 'cfde-inc-v2'}))
        self.plan = links.prepare_mapping(self.c, directory, eaggl_import_id=self.source_id, gene_set_import_id=self.genes_id)

    def test_load_replay_and_lookup_resolves_existing_dapper_object(self):
        result = links.load_mapping(self.c, self.plan, batch_size=1)
        self.assertEqual(result, links.load_mapping(self.c, self.plan))
        row = links.lookup_factor(self.c, 'Trait::Factor1', run_id=result['run_id'])
        self.assertEqual(row['status'], 'matched')
        self.assertEqual(row['gene_sets'][0]['status'], 'resolved')
        self.assertTrue(row['gene_sets'][0]['dapper_id'].startswith('test:GeneSet.'))
        self.assertEqual(row['gene_sets'][1]['status'], 'not_in_gene_set_catalog')
        missing = links.lookup_factor(self.c, 'Trait::Factor2', run_id=result['run_id'])
        self.assertEqual(missing['reason'], 'factor_number_not_in_cfde_catalog')

    def test_failure_rolls_back_entire_mapping(self):
        insert = links.insert_batch
        def fail(cursor, table, columns, rows):
            insert(cursor, table, columns, rows)
            if table == 'eaggl_cfde_gene_set_links': raise ConnectionError('interrupted')
        with patch.object(links, 'insert_batch', side_effect=fail), self.assertRaises(ConnectionError):
            links.load_mapping(self.c, self.plan, batch_size=1)
        with self.c.cursor() as q:
            q.execute('SELECT COUNT(*) FROM eaggl_cfde_link_runs WHERE run_id=%s', (self.plan['manifest']['run_id'],))
            self.assertEqual(q.fetchone()[0], 0)
        self.assertEqual(links.load_mapping(self.c, self.plan)['status'], 'complete')

    def test_stored_target_corruption_is_detected(self):
        result = links.load_mapping(self.c, self.plan)
        with self.c.cursor() as q:
            q.execute("UPDATE eaggl_cfde_factor_links SET cfde_node_id='changed' WHERE run_id=%s", (result['run_id'],))
        self.c.commit()
        with self.assertRaisesRegex(ValueError, 'read-back differs'):
            links.load_mapping(self.c, self.plan)


if __name__ == '__main__':
    unittest.main()
