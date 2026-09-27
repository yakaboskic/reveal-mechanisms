"""Bundle alignment, provenance, resumability, service wiring and retrieval invariants."""
import copy
import csv
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy import sparse

from reveal_backend import eaggl_bundle as bundle
from reveal_backend import eaggl_embeddings as embeddings
from reveal_backend import eaggl_database as database


class CaptureFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.data = self.source / 'data'
        self.data.mkdir(parents=True)
        self.output = self.root / 'capture'
        self.rows = [
            {'factor_id': 'A::Factor1', 'trait': 'A', 'factor': 'Factor1', 'label': 'T cell activation'},
            {'factor_id': 'B::Factor1', 'trait': 'B', 'factor': 'Factor1', 'label': 'T cell activation'},
            {'factor_id': 'C::Factor2', 'trait': 'C', 'factor': 'Factor2', 'label': 'Glucose regulation'}]
        self.tsv('factor_metadata.tsv', list(self.rows[0]), list(reversed(self.rows)))
        self.tsv('factor_ids.tsv', ['factor_id'], [{'factor_id': r['factor_id']} for r in self.rows])
        self.tsv('genes.tsv', ['gene'], [{'gene': g} for g in ['HLA-A', 'INS']])
        self.matrix = sparse.csr_matrix(np.array([[1, 0], [np.nextafter(np.float32(0), np.float32(1)), .2], [0, .5]]))
        sparse.save_npz(self.data / 'capped_factor_gene_loadings.npz', self.matrix)
        self.graph = {'root': 'root', 'nodes': [
            {'id': f'f{i}', 'kind': 'factor', 'factor_id': r['factor_id'], 'factor_index': i,
             'parents': ['root'], 'children': [], 'label': r['label']} for i, r in enumerate(self.rows)] + [
            {'id': 'root', 'kind': 'root', 'parents': [], 'children': ['f0', 'f1', 'f2'], 'label': 'Atlas'}]}
        self.write_graph()

    def tsv(self, name, fields, rows):
        with (self.data / name).open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter='\t')
            writer.writeheader(); writer.writerows(rows)

    def write_graph(self):
        (self.source / 'index.html').write_text('<script>const DATA=' + json.dumps(self.graph) + ';const byId=new Map();</script>')

    def prepare(self, **kwargs):
        return bundle.prepare(self.output, bundle=self.source, source_version='legacy-test', **kwargs)

    @staticmethod
    def embedder(texts, **kwargs):
        return np.array([[1., 0., 0.] if t == 'T cell activation' else [.6, .8, 0.] for t in texts])

    def embedded(self):
        self.manifest = self.prepare()
        return embeddings.embed_capture(self.output, embedder=self.embedder)


class BundleTests(CaptureFixture):
    def test_real_axis_join_sparse_values_and_graph_mapping(self):
        result = self.prepare()
        manifest, factors, genes, matrix, graph = bundle.open_capture(self.output)
        self.assertEqual([f['factor_id'] for f in factors], [r['factor_id'] for r in self.rows])
        np.testing.assert_array_equal(matrix.toarray(), self.matrix.toarray())
        self.assertGreater(matrix[1, 0], 0)
        self.assertEqual(result['counts'], dict(factors=3, genes=2, loadings=4, traits=3,
                                               unique_labels=2, graph_nodes=4, graph_edges=3))
        stages = {stage: list(rows) for stage, _, _, rows in database.source_stages(manifest, factors, genes, matrix, graph)}
        self.assertEqual(stages['graph_edges'], [(manifest['import_id'], 3, i) for i in range(3)])

    def test_tsv_fallback_and_optional_graph(self):
        path = self.root / 'matrix.tsv.gz'
        with gzip.open(path, 'wt') as handle:
            handle.write('factor_id\tHLA-A\tINS\nA::Factor1\t1\t0\nB::Factor1\t0.2\t0.1\nC::Factor2\t0\t0.5\n')
        bundle.prepare(self.output, metadata=self.data / 'factor_metadata.tsv', loadings=path, source_version='v1')
        manifest, _, _, matrix, graph = bundle.open_capture(self.output)
        self.assertIsNone(graph)
        self.assertEqual(manifest['counts']['loadings'], 4)
        self.assertEqual(matrix[1, 1], .1)
        with self.assertRaisesRegex(ValueError, 'order differs'):
            bundle.read_matrix(path, ['C::Factor2', 'B::Factor1', 'A::Factor1'], ['HLA-A', 'INS'])

    def test_blank_labels_rejected(self):
        self.rows[0]['label'] = ' '
        self.tsv('factor_metadata.tsv', list(self.rows[0]), self.rows)
        with self.assertRaisesRegex(ValueError, 'Missing label'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_duplicate_metadata_and_axes_rejected(self):
        self.tsv('factor_metadata.tsv', list(self.rows[0]), self.rows + [self.rows[0]])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            bundle.read_matrix(self.data / 'capped_factor_gene_loadings.npz', ['a', 'a', 'b'], ['A', 'B'])

    def test_npz_requires_axes_and_capped_finite_values(self):
        path = self.data / 'capped_factor_gene_loadings.npz'
        with self.assertRaisesRegex(ValueError, 'requires'):
            bundle.read_matrix(path)
        for bad in [float('nan'), float('inf'), -0.1, 1.01]:
            self.matrix.data[0] = bad
            sparse.save_npz(path, self.matrix)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.prepare()

    def test_matrix_metadata_scope_mismatch_rejected(self):
        self.rows[0]['factor_id'] = 'different'
        self.tsv('factor_metadata.tsv', list(self.rows[0]), self.rows)
        with self.assertRaisesRegex(ValueError, 'exactly match'):
            self.prepare()

    def test_graph_dangling_cycle_and_factor_alignment_rejected(self):
        original = copy.deepcopy(self.graph)
        for mutation in ['dangling', 'cycle', 'index', 'missing-parent']:
            self.graph = copy.deepcopy(original)
            if mutation == 'dangling':
                self.graph['nodes'][-1]['children'].append('absent')
            elif mutation == 'cycle':
                self.graph['nodes'][-1]['children'].append('root')
            elif mutation == 'index':
                self.graph['nodes'][0]['factor_index'] = 1
            else:
                self.graph['nodes'][0]['parents'] = []
            self.write_graph()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.prepare()

    def test_checksum_corruption_and_capture_mutation_rejected(self):
        (self.source / 'SHA256SUMS.txt').write_text('0' * 64 + '  data/factor_metadata.tsv\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.prepare()
        (self.source / 'SHA256SUMS.txt').unlink()
        self.prepare()
        (self.output / 'genes.json').write_text('["changed"]')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            bundle.open_capture(self.output)

    def test_identity_is_path_independent_but_version_specific(self):
        first = self.prepare()
        second = bundle.prepare(self.root / 'second', bundle=self.source, source_version='legacy-test')
        third = bundle.prepare(self.root / 'third', bundle=self.source, source_version='another-version')
        self.assertEqual(first['import_id'], second['import_id'])
        self.assertEqual(first['artifacts'], second['artifacts'])
        self.assertNotEqual(first['import_id'], third['import_id'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.prepare()


class EmbeddingTests(CaptureFixture):
    def test_duplicate_names_embedded_once_and_replay_makes_no_calls(self):
        self.prepare()
        with patch.object(embeddings, 'get_embeddings', side_effect=AssertionError('unexpected network')):
            calls = []
            def embed(texts, **kwargs):
                calls.extend(texts); return self.embedder(texts)
            run = embeddings.embed_capture(self.output, embedder=embed, batch_size=1)
            again = embeddings.embed_capture(self.output, embedder=lambda *a, **k: self.fail('re-embedded'))
        self.assertEqual(len(calls), 2)
        self.assertEqual(run['run_id'], again['run_id'])
        index = embeddings.local_search_index(self.output)
        results = index.search(factor_id='A::Factor1', top_k=2)
        self.assertEqual(results[0]['factor_id'], 'B::Factor1')
        self.assertAlmostEqual(results[0]['cosine_similarity'], 1.)
        self.assertAlmostEqual(results[1]['cosine_similarity'], .6, places=6)
        self.assertEqual(index.search(factor_id='A::Factor1', trait='C')[0]['factor_id'], 'C::Factor2')

    def test_interrupted_embedding_resumes_only_unfinished_names(self):
        self.prepare()
        calls = []
        def broken(texts, **kwargs):
            calls.extend(texts)
            if len(calls) == 2:
                raise TimeoutError('interrupted')
            return self.embedder(texts)
        with self.assertRaises(TimeoutError):
            embeddings.embed_capture(self.output, embedder=broken, batch_size=1)
        with self.assertRaisesRegex(ValueError, 'complete'):
            embeddings.local_search_index(self.output)
        resumed = []
        def finish(texts, **kwargs):
            resumed.extend(texts); return self.embedder(texts)
        embeddings.embed_capture(self.output, embedder=finish, batch_size=1)
        self.assertEqual(resumed, calls[1:])

    def test_zero_nonfinite_and_dimension_drift_fail_before_complete(self):
        for vectors in [[[0, 0]], [[float('nan'), 1]], [['1', '2']], [[1, 2], [3, 4]]]:
            with self.subTest(vectors=vectors), self.assertRaises(ValueError):
                embeddings.validate_vectors(vectors, 1)
        self.prepare()
        calls = []
        def drift(texts, **kwargs):
            calls.append(texts)
            return [[1, 2]] if len(calls) == 1 else [[1, 2, 3]]
        with self.assertRaisesRegex(ValueError, 'dimension changed'):
            embeddings.embed_capture(self.output, embedder=drift, batch_size=1)

    def test_provider_model_revision_template_and_endpoint_scoping(self):
        self.embedded()
        changed = embeddings.embed_capture(self.output, embedder=self.embedder, model_revision='revision-2')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            embeddings.local_search_index(self.output)
        index = embeddings.local_search_index(self.output, changed['run_id'])
        self.assertEqual(index.run['config']['model_revision'], 'revision-2')
        calls = []
        def query_embedder(texts, **kwargs):
            calls.append(kwargs); return [[1, 0, 0]]
        index.search(query='immunity', embedder=query_embedder)
        self.assertEqual(calls[0]['model'], index.run['config']['model'])
        self.assertEqual(calls[0]['service_url'], index.run['config']['service_url'])

    def test_corrupt_embedding_bytes_are_rejected(self):
        self.embedded()
        with sqlite3.connect(self.output / 'embeddings.sqlite3') as connection:
            connection.execute("UPDATE vectors SET vector=x'00000000'")
        with self.assertRaisesRegex(ValueError, 'checksum'):
            embeddings.local_search_index(self.output)

    def test_uses_real_embedding_client_wire_contract(self):
        self.prepare()
        requests = []
        class Response:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.data).encode()
        def request(req, **kwargs):
            payload = json.loads(req.data)
            requests.append((req.full_url, payload, req.get_header('X-api-key')))
            return Response({'count': len(payload['texts']), 'dimensions': 3,
                             'embeddings': self.embedder(payload['texts']).tolist()})
        with patch.dict('os.environ', {'EMBEDDING_SERVICE_API_KEY': 'unit-test-key'}), \
             patch('reveal_backend.embedding_client.urllib.request.urlopen', side_effect=request):
            embeddings.embed_capture(self.output, service_url='https://example.invalid', model='test-model')
        self.assertEqual(requests[0][0], 'https://example.invalid/embed')
        self.assertEqual(requests[0][1]['provider'], 'huggingface')
        self.assertEqual(requests[0][1]['model'], 'test-model')
        self.assertEqual(requests[0][2], 'unit-test-key')
        self.assertEqual(set(requests[0][1]['texts']), {r['label'] for r in self.rows})


class SQLiteMySQLAdapter:
    """Exercise loader transactions/constraints locally, not MySQL engine compatibility.

    Only DDL spelling and MySQL control statements are translated. Real inserts,
    selects, commits and rollback run against SQLite, including foreign keys.
    """
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA foreign_keys=ON')
        import hashlib
        def raw(value): return value.encode('utf-8') if isinstance(value,str) else bytes(value)
        self.db.create_function('SHA2',2,lambda value,bits: hashlib.sha256(raw(value)).hexdigest() if value is not None and bits==256 else None)
        self.db.create_function('OCTET_LENGTH',1,lambda value: len(raw(value)) if value is not None else None)
        self.fail_after_insert = None
        self.lock_available = True
        self.lock_released = False

    def commit(self): self.db.commit()
    def rollback(self): self.db.rollback()
    def close(self): self.db.close()
    def cursor(self): return SQLiteCursor(self)


class SQLiteCursor:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.db.cursor()
        self.synthetic = None

    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()

    def execute(self, sql, values=()):
        import re
        self.synthetic = None
        if 'GET_LOCK(' in sql:
            self.synthetic = [(int(self.connection.lock_available),)]; return
        if 'RELEASE_LOCK(' in sql:
            self.connection.lock_released = True
            self.synthetic = [(1,)]; return
        if sql == 'SHOW WARNINGS':
            self.synthetic = []; return
        if sql.startswith('SET SESSION sql_mode'):
            self.synthetic = []; return
        if 'CREATE TABLE' in sql:
            sql = re.sub(r'CHARACTER SET \w+', '', sql)
            sql = re.sub(r'COLLATE \w+', '', sql)
            sql = re.sub(r'\bUNSIGNED\b', '', sql)
            sql = re.sub(r'UNIQUE KEY \w+\s*\(', 'UNIQUE (', sql)
            sql = re.sub(r'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4', '', sql)
        sql = re.sub(r'\s+FORCE INDEX\s*\(PRIMARY\)', '', sql)
        return self.cursor.execute(sql.replace('%s', '?'), values)

    def executemany(self, sql, rows):
        self.synthetic = None
        self.cursor.executemany(sql.replace('%s', '?'), rows)
        if self.connection.fail_after_insert and self.connection.fail_after_insert(sql, rows):
            raise ConnectionError('simulated disconnect after insert, before checkpoint')

    def fetchone(self):
        return self.synthetic[0] if self.synthetic is not None else self.cursor.fetchone()

    def fetchall(self):
        return self.synthetic if self.synthetic is not None else self.cursor.fetchall()


class DatabaseTests(CaptureFixture):
    def connect(self):
        connection = SQLiteMySQLAdapter(self.root / 'test.sqlite3')
        self.addCleanup(connection.close)
        return connection

    def test_resume_after_uncommitted_insert_and_completed_replay(self):
        embedded = self.embedded()
        connection = self.connect()
        calls = []
        def fail(sql, rows):
            if 'INSERT INTO eaggl_gene_loadings ' in sql:
                calls.append(sql)
                return len(calls) == 2
            return False
        connection.fail_after_insert = fail
        with self.assertRaises(ConnectionError):
            database.load_capture(connection, self.output, batch_size=2)
        self.assertTrue(connection.lock_released)
        self.assertEqual(connection.db.execute('SELECT COUNT(*) FROM eaggl_gene_loadings').fetchone()[0], 2)
        progress, status = connection.db.execute('SELECT progress,status FROM eaggl_imports').fetchone()
        self.assertEqual(json.loads(progress)['loadings'], 2)
        self.assertEqual(status, 'loading')
        connection.fail_after_insert = None
        result = database.load_capture(connection, self.output, batch_size=2)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(connection.db.execute('SELECT COUNT(*) FROM eaggl_gene_loadings').fetchone()[0], 4)
        self.assertEqual(connection.db.execute('SELECT loading FROM eaggl_gene_loadings WHERE factor_index=1 AND gene_index=0').fetchone()[0], self.matrix[1, 0])
        replay = database.load_capture(connection, self.output, batch_size=2)
        self.assertEqual(result, replay)
        index = embeddings.database_search_index(connection, self.manifest['import_id'], embedded['run_id'])
        self.assertEqual(index.search(factor_id='A::Factor1')[0]['factor_id'], 'B::Factor1')

    def test_embedding_load_failure_is_not_searchable_and_resumes(self):
        embedded = self.embedded()
        connection = self.connect()
        connection.fail_after_insert = lambda sql, rows: 'INSERT INTO eaggl_name_embeddings ' in sql
        with self.assertRaises(ConnectionError):
            database.load_capture(connection, self.output, batch_size=1)
        self.assertEqual(connection.db.execute('SELECT COUNT(*) FROM eaggl_name_embeddings').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, 'Embedding run is not complete'):
            embeddings.database_search_index(connection, self.manifest['import_id'], embedded['run_id'])
        connection.fail_after_insert = None
        database.load_capture(connection, self.output, batch_size=1)
        self.assertEqual(connection.db.execute('SELECT COUNT(*) FROM eaggl_name_embeddings').fetchone()[0], 2)

    def test_checkpoint_corruption_and_embedding_corruption_fail_closed(self):
        self.embedded()
        connection = self.connect()
        database.load_capture(connection, self.output)
        connection.db.execute('DELETE FROM eaggl_gene_loadings WHERE factor_index=0')
        connection.commit()
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            database.load_capture(connection, self.output)

    def test_lock_prevents_simultaneous_importers(self):
        self.embedded()
        connection = self.connect()
        connection.lock_available = False
        with self.assertRaisesRegex(RuntimeError, 'Another loader'):
            database.load_capture(connection, self.output)
        self.assertFalse(connection.lock_released)

    def test_database_name_scope(self):
        database.validate_database('cyaka_reveal_mechanisms')
        for bad in ['other', 'cyaka_', 'cyakaXanything', 'cyaka_x`', 'cyaka_a;DROP TABLE X']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                database.validate_database(bad)


if __name__ == '__main__':
    unittest.main()
