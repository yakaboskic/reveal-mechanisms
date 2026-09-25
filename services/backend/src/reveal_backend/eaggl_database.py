"""Append-only, transactionally resumable Aurora/MySQL projection of a capture."""
from __future__ import annotations

from itertools import islice
import json
import logging
from pathlib import Path

from .eaggl_bundle import canonical, open_capture, text_hash
from .eaggl_embeddings import read_embeddings

from .mysql_database import DEFAULT_HOST, connect, insert_batch, validate_database
MIGRATION = Path(__file__).resolve().parents[4] / 'schema/migrations/002_eaggl_factors.sql'


def source_stages(manifest, factors, genes, matrix, graph):
    identity = manifest['import_id']
    yield 'genes', 'eaggl_genes', ('import_id', 'gene_index', 'symbol'), (
        (identity, i, gene) for i, gene in enumerate(genes))
    yield 'factors', 'eaggl_factors', ('import_id', 'factor_index', 'factor_id', 'factor_id_sha256',
        'trait', 'label', 'input_sha256', 'metadata'), (
        (identity, f['index'], f['factor_id'], text_hash(f['factor_id']), f['trait'], f['label'],
         f['input_sha256'], canonical(f['metadata'])) for f in factors)

    def loadings():
        for i in range(matrix.shape[0]):
            for j in range(matrix.indptr[i], matrix.indptr[i + 1]):
                yield identity, i, int(matrix.indices[j]), float(matrix.data[j])
    yield 'loadings', 'eaggl_gene_loadings', ('import_id', 'factor_index', 'gene_index', 'loading'), loadings()
    nodes = graph['nodes'] if graph else []
    node_indices = {n['id']: i for i, n in enumerate(nodes)}
    factor_indices = {f['factor_id']: f['index'] for f in factors}
    yield 'graph_nodes', 'eaggl_graph_nodes', ('import_id', 'node_index', 'node_id', 'factor_index', 'payload'), (
        (identity, i, n['id'], factor_indices[n['factor_id']] if n['kind'] == 'factor' else None, canonical(n))
        for i, n in enumerate(nodes))
    yield 'graph_edges', 'eaggl_graph_edges', ('import_id', 'parent_index', 'child_index'), (
        (identity, i, node_indices[child]) for i, n in enumerate(nodes) for child in n.get('children', []))


def count(cursor, table, key, identity):
    cursor.execute(f'SELECT COUNT(*) FROM {table} WHERE {key}=%s', (identity,))
    return cursor.fetchone()[0]


def load_capture(connection, output, *, run_id=None, batch_size=5000, migration=MIGRATION):
    if batch_size < 1:
        raise ValueError('Batch size must be positive')
    manifest, factors, genes, matrix, graph = open_capture(output)
    run, vectors = read_embeddings(output, factors, manifest['import_id'], run_id)
    if len(manifest['namespace']) > 128 or len(manifest['source_version']) > 255:
        raise ValueError('Source namespace/version exceeds database column length')
    import_id = manifest['import_id']
    lock_name = 'eaggl:' + import_id[:56]
    locked = False
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT GET_LOCK(%s, 0)', (lock_name,))
            locked = cursor.fetchone()[0] == 1
            if not locked:
                raise RuntimeError('Another loader holds this import lock; retry after it finishes')
            # executemany may split statements. Strict mode makes truncation in
            # any sub-statement fail, even before the final SHOW WARNINGS check.
            cursor.execute("SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')")
            sql = '\n'.join(line for line in Path(migration).read_text().splitlines()
                            if not line.lstrip().startswith('--'))
            for statement in sql.split(';'):
                if statement.strip():
                    cursor.execute(statement)
            cursor.execute('SELECT manifest,progress,status FROM eaggl_imports WHERE import_id=%s', (import_id,))
            old = cursor.fetchone()
            if old:
                old_manifest = json.loads(old[0])
                # A capture relocated to another machine may have different source paths only.
                for field in ('source_files',):
                    old_manifest.pop(field, None)
                comparable = {k: v for k, v in manifest.items() if k != 'source_files'}
                if old_manifest != comparable:
                    raise ValueError('Existing import manifest conflicts with this capture')
                progress = json.loads(old[1])
            else:
                progress = {}
                cursor.execute('INSERT INTO eaggl_imports (import_id,source_namespace,source_version,status,manifest,progress) VALUES (%s,%s,%s,%s,%s,%s)',
                    (import_id, manifest['namespace'], manifest['source_version'], 'loading', canonical(manifest), '{}'))
                connection.commit()
            for stage, table, columns, rows in source_stages(manifest, factors, genes, matrix, graph):
                loaded = progress.get(stage, 0)
                expected = manifest['counts'][stage]
                if not 0 <= loaded <= expected or count(cursor, table, 'import_id', import_id) != loaded:
                    raise ValueError(f'{stage}: database count differs from committed checkpoint')
                if loaded == expected:
                    continue
                iterator = islice(rows, loaded, None)
                while batch := list(islice(iterator, batch_size)):
                    insert_batch(cursor, table, columns, batch)
                    loaded += len(batch)
                    progress[stage] = loaded
                    cursor.execute('UPDATE eaggl_imports SET progress=%s WHERE import_id=%s', (canonical(progress), import_id))
                    connection.commit()
                    logging.info('Loaded %s %d/%d', stage, loaded, expected)
                if loaded != expected or count(cursor, table, 'import_id', import_id) != expected:
                    raise ValueError(f'{stage}: final count mismatch')
            cursor.execute("UPDATE eaggl_imports SET status='complete' WHERE import_id=%s", (import_id,))
            connection.commit()
            cursor.execute('SELECT config,dimensions,expected_rows,loaded_rows FROM eaggl_embedding_runs WHERE run_id=%s', (run['run_id'],))
            old_run = cursor.fetchone()
            if old_run:
                if (json.loads(old_run[0]), old_run[1], old_run[2]) != (run['config'], run['dimensions'], run['expected']):
                    raise ValueError('Existing embedding run conflicts with local run')
                loaded = old_run[3]
            else:
                loaded = 0
                cursor.execute('INSERT INTO eaggl_embedding_runs (run_id,import_id,config,dimensions,expected_rows,status) VALUES (%s,%s,%s,%s,%s,%s)',
                    (run['run_id'], import_id, canonical(run['config']), run['dimensions'], run['expected'], 'loading'))
                connection.commit()
            if not 0 <= loaded <= run['expected'] or count(cursor, 'eaggl_name_embeddings', 'run_id', run['run_id']) != loaded:
                raise ValueError('Embedding checkpoint count mismatch')
            for start in range(loaded, len(vectors), batch_size):
                batch = [(run['run_id'], *row) for row in vectors[start:start + batch_size]]
                insert_batch(cursor, 'eaggl_name_embeddings', ('run_id', 'input_sha256', 'input_text', 'vector', 'vector_sha256'), batch)
                loaded += len(batch)
                cursor.execute('UPDATE eaggl_embedding_runs SET loaded_rows=%s WHERE run_id=%s', (loaded, run['run_id']))
                connection.commit()
            if count(cursor, 'eaggl_name_embeddings', 'run_id', run['run_id']) != run['expected']:
                raise ValueError('Final embedding count mismatch')
            # Read every vector back before making the run eligible for search.
            cursor.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM eaggl_name_embeddings WHERE run_id=%s ORDER BY input_sha256', (run['run_id'],))
            if list(cursor.fetchall()) != vectors:
                raise ValueError('Embedding read-back differs from capture')
            cursor.execute("UPDATE eaggl_embedding_runs SET status='complete' WHERE run_id=%s", (run['run_id'],))
            connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        if locked:
            with connection.cursor() as cursor:
                cursor.execute('SELECT RELEASE_LOCK(%s)', (lock_name,))
    return {'import_id': import_id, 'run_id': run['run_id'], 'counts': manifest['counts'],
            'dimensions': run['dimensions'], 'status': 'complete'}
