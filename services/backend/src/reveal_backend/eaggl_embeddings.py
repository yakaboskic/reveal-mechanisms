"""Resumable label embeddings and exact, on-demand EAGGL semantic retrieval."""
from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
import sqlite3
import threading
import time

import numpy as np

from .eaggl_bundle import TEMPLATE, canonical, open_capture, text_hash
from .embedding_client import DEFAULT_MODEL, DEFAULT_SERVICE_URL, get_embeddings


def validate_vectors(vectors, count, dimensions=None):
    raw = np.asarray(vectors)
    if raw.dtype.kind not in 'fiu':
        raise ValueError('Embedding values must be numeric')
    with np.errstate(over='ignore', invalid='ignore'):
        matrix = raw.astype('<f4')
    if matrix.ndim != 2 or matrix.shape[0] != count or matrix.shape[1] < 1:
        raise ValueError('Embedding shape does not match labels')
    if dimensions is not None and matrix.shape[1] != dimensions:
        raise ValueError('Embedding dimension changed; use a new model revision')
    if not np.isfinite(matrix).all() or np.any(np.linalg.norm(matrix.astype(np.float64), axis=1) == 0):
        raise ValueError('Embeddings must be finite and nonzero')
    return matrix


def cache_connection(output, *, readonly=False):
    path = Path(output).resolve() / 'embeddings.sqlite3'
    if readonly:
        return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection = sqlite3.connect(path)
    connection.executescript('''
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, import_id TEXT NOT NULL, config TEXT NOT NULL,
          dimensions INTEGER, expected INTEGER NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS vectors (
          run_id TEXT NOT NULL, input_sha256 TEXT NOT NULL, input_text TEXT NOT NULL,
          vector BLOB NOT NULL, vector_sha256 TEXT NOT NULL,
          PRIMARY KEY(run_id, input_sha256));
    ''')
    return connection


def vector_hash(blob):
    import hashlib
    return hashlib.sha256(blob).hexdigest()


def decode_vector(blob, checksum, dimensions):
    if len(blob) != dimensions * 4 or vector_hash(blob) != checksum:
        raise ValueError('Embedding byte length/checksum mismatch')
    vector = np.frombuffer(blob, dtype='<f4')
    return validate_vectors(vector[None, :], 1, dimensions)[0]


def embed_capture(output, *, model=None, model_revision='unspecified', service_url=None,
                  provider='huggingface', batch_size=100, max_retries=3, timeout=120,
                  embedder=get_embeddings):
    manifest, factors, _, _, _ = open_capture(output)
    model = model if model is not None else os.getenv('EMBEDDING_MODEL', DEFAULT_MODEL)
    service_url = (service_url if service_url is not None else os.getenv('EMBEDDING_SERVICE_URL', DEFAULT_SERVICE_URL)).rstrip('/')
    if not model.strip() or not model_revision.strip() or not provider.strip() or not service_url.startswith('https://'):
        raise ValueError('Model, revision, provider and HTTPS service URL are required')
    if not 1 <= batch_size <= 100:
        raise ValueError('Embedding batch size must be between 1 and 100')
    config = {'import_id': manifest['import_id'], 'model': model, 'model_revision': model_revision,
              'service_url': service_url, 'provider': provider, 'template': TEMPLATE,
              'dtype': 'float32-le', 'normalization': 'none', 'metric': 'cosine'}
    run_id = text_hash(canonical(config))
    names = {f['input_sha256']: f['input_text'] for f in factors}
    connection = cache_connection(output)
    try:
        connection.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?)',
                           (run_id, manifest['import_id'], canonical(config), None, len(names), 'embedding'))
        connection.commit()
        dimensions = connection.execute('SELECT dimensions FROM runs WHERE run_id=?', (run_id,)).fetchone()[0]
        existing = set()
        for key, text, blob, checksum in connection.execute(
                'SELECT input_sha256,input_text,vector,vector_sha256 FROM vectors WHERE run_id=?', (run_id,)):
            if names.get(key) != text or dimensions is None:
                raise ValueError('Embedding cache text/config mismatch')
            decode_vector(blob, checksum, dimensions); existing.add(key)
        missing = sorted(set(names) - existing)
        for start in range(0, len(missing), batch_size):
            keys = missing[start:start + batch_size]
            matrix = validate_vectors(embedder([names[key] for key in keys], model=model,
                service_url=service_url, provider=provider, batch_size=batch_size,
                max_workers=1, max_retries=max_retries, timeout=timeout), len(keys), dimensions)
            dimensions = matrix.shape[1]
            with connection:
                for key, vector in zip(keys, matrix):
                    blob = vector.tobytes()
                    connection.execute('INSERT INTO vectors VALUES (?,?,?,?,?)',
                                       (run_id, key, names[key], blob, vector_hash(blob)))
                connection.execute('UPDATE runs SET dimensions=? WHERE run_id=?', (dimensions, run_id))
            logging.info('Embedded %d/%d unique labels', len(existing) + start + len(keys), len(names))
        with connection:
            connection.execute("UPDATE runs SET status='complete' WHERE run_id=?", (run_id,))
    finally:
        connection.close()
    return {'run_id': run_id, 'dimensions': dimensions, 'unique_labels': len(names), 'config': config}


def read_embeddings(output, factors, import_id, run_id=None):
    connection = cache_connection(output, readonly=True)
    try:
        runs = connection.execute("SELECT run_id,config,dimensions,expected FROM runs WHERE import_id=? AND status='complete'"
                                  + (' AND run_id=?' if run_id else ''),
                                  (import_id, run_id) if run_id else (import_id,)).fetchall()
        if len(runs) != 1:
            raise ValueError('Select exactly one complete embedding run with --run-id (run embed first)')
        run_id, config_json, dimensions, expected = runs[0]
        config = json.loads(config_json)
        if text_hash(canonical(config)) != run_id or config['import_id'] != import_id or config['template'] != TEMPLATE:
            raise ValueError('Embedding run configuration mismatch')
        names = {f['input_sha256']: f['input_text'] for f in factors}
        rows = connection.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM vectors WHERE run_id=? ORDER BY input_sha256',
                                  (run_id,)).fetchall()
        if expected != len(names) or {r[0] for r in rows} != set(names):
            raise ValueError('Embedding coverage does not match the factor corpus')
        for key, text, blob, checksum in rows:
            if names[key] != text or text_hash(text) != key:
                raise ValueError('Embedding text/hash mismatch')
            decode_vector(blob, checksum, dimensions)
        return {'run_id': run_id, 'config': config, 'dimensions': dimensions, 'expected': expected}, rows
    finally:
        connection.close()


class QueryVectorCache:
    """Bounded process-local vectors, with one request for overlapping misses.

    Scope includes the immutable embedding run and full configuration. Only
    validated vectors are published; failures reach current waiters and are
    removed so subsequent requests can retry. No query text is persisted.
    """
    def __init__(self, max_entries=512, ttl_seconds=3600):
        self.max_entries, self.ttl_seconds = max_entries, ttl_seconds
        self.values = OrderedDict()
        self.pending = {}
        self.lock = threading.Lock()

    def get(self, texts, scope, fetch):
        keys = [(scope, text_hash(text)) for text in texts]
        available, waiting, owned = {}, {}, {}
        with self.lock:
            current = time.monotonic()
            for key, text in zip(keys, texts):
                cached = self.values.get(key)
                if cached is not None and current - cached[0] >= self.ttl_seconds:
                    del self.values[key]
                    cached = None
                if cached is not None:
                    self.values.move_to_end(key)
                    available[key] = cached[1]
                else:
                    future = self.pending.get(key)
                    if future is None:
                        future = self.pending[key] = Future()
                        owned[key] = (text, future)
                    waiting[key] = future
        if owned:
            try:
                vectors = fetch([text for text, _ in owned.values()])
                completed = []
                with self.lock:
                    current = time.monotonic()
                    for (key, (_, future)), vector in zip(owned.items(), vectors):
                        vector.setflags(write=False)
                        self.values[key] = (current, vector)
                        self.pending.pop(key)
                        completed.append((future, vector))
                    while len(self.values) > self.max_entries:
                        self.values.popitem(last=False)
                for future, vector in completed:
                    future.set_result(vector)
            except BaseException as error:
                with self.lock:
                    for key in owned:
                        self.pending.pop(key, None)
                for _, future in owned.values():
                    if not future.done():
                        future.set_exception(error)
                raise
        # Stack preserves duplicate occurrences and context order without
        # exposing mutable cached vectors to callers.
        return np.stack([available[key] if key in available else waiting[key].result() for key in keys])


class FactorSearchIndex:
    """Offline exact reference, selected only by explicit legacy configuration."""

    candidate_limit = 1000000

    def __init__(self, factors, run, rows):
        self.factors, self.run = factors, run
        rows = list(rows)
        if any(text_hash(row[1]) != row[0] for row in rows):
            raise ValueError('Imported embedding text/hash mismatch')
        by_hash = {r[0]: decode_vector(r[2], r[3], run['dimensions']) for r in rows}
        matrix = np.stack([by_hash[f['input_sha256']] for f in factors]).astype(np.float64)
        self.matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        self.by_id = {f['factor_id']: i for i, f in enumerate(factors)}
        self.query_cache = QueryVectorCache()
        self.imported_scope = (run['run_id'], text_hash(canonical(run['config'])), run['dimensions'])
        self.imported_vectors = {}
        for sha, text, _, _ in rows:
            vector = by_hash[sha].astype(np.float64)
            vector /= np.linalg.norm(vector)
            vector.setflags(write=False)
            self.imported_vectors[sha] = (text, vector)

    def query_vectors(self, texts, *, embedder=get_embeddings):
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError('Queries cannot be blank')
        config = self.run['config']
        dimensions = self.matrix.shape[1]
        scope = (self.run['run_id'], text_hash(canonical(config)), dimensions)

        def fetch(missing):
            raw = embedder(missing, model=config['model'], provider=config['provider'],
                service_url=config['service_url'], max_workers=1, max_retries=2, timeout=30)
            validate_vectors(raw, len(missing), dimensions)
            # Validation must not reduce the precision used by per-context
            # suggestion scoring (custom embedders may return float64).
            vectors = np.asarray(raw, dtype=np.float64)
            return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

        imported = {}
        if scope == self.imported_scope:
            for text in texts:
                candidate = self.imported_vectors.get(text_hash(text))
                if candidate is not None and candidate[0] == text:
                    imported[text] = candidate[1]
        missing = [text for text in texts if text not in imported]
        if missing:
            resolved = self.query_cache.get(missing, scope, fetch)
            imported.update(zip(missing, resolved))
        # Return an independent matrix while preserving every occurrence and
        # its original position, including mixed imported and novel text.
        return np.stack([imported[text] for text in texts])

    def candidates(self, vectors, top_k, *, exclude=()):
        return [self.search(query='stored query', query_vector=vector, top_k=top_k) for vector in vectors]

    def fetch_vectors(self, factor_ids):
        return self.matrix[[self.by_id[identity] for identity in factor_ids]].copy()

    def search(self, *, query=None, factor_id=None, top_k=10, trait=None, embedder=get_embeddings, query_vector=None):
        if (query is None) == (factor_id is None) or top_k < 1:
            raise ValueError('Choose one query or factor ID and a positive top_k')
        if factor_id is not None:
            vector = self.matrix[self.by_id[factor_id]]
        else:
            if not query.strip():
                raise ValueError('Query cannot be blank')
            if query_vector is None:
                vector = self.query_vectors([query.strip()], embedder=embedder)[0]
            else:
                vector = np.asarray(query_vector, dtype=np.float64)
                if vector.shape != (self.matrix.shape[1],) or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1):
                    raise ValueError('Imported query vector must be finite and normalized in the target space')
        scores = self.matrix @ vector
        eligible = [i for i, f in enumerate(self.factors)
                    if f['factor_id'] != factor_id and (trait is None or f['trait'] == trait)]
        ranked = sorted(eligible, key=lambda i: (-scores[i], self.factors[i]['factor_id']))[:top_k]
        return [{'factor_id': self.factors[i]['factor_id'], 'label': self.factors[i]['label'],
                 'trait': self.factors[i]['trait'], 'cosine_similarity': float(np.clip(scores[i], -1, 1)),
                 'import_id': self.run['config']['import_id'], 'embedding_run_id': self.run['run_id']}
                for i in ranked]


def local_search_index(output, run_id=None):
    manifest, factors, _, _, _ = open_capture(output)
    run, rows = read_embeddings(output, factors, manifest['import_id'], run_id)
    return FactorSearchIndex(factors, run, rows)


def database_search_index(connection, import_id, run_id):
    """Load only a completed source snapshot and completed compatible embedding run."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT status FROM eaggl_imports WHERE import_id=%s", (import_id,))
        if cursor.fetchone() != ('complete',):
            raise ValueError('Source import is not complete')
        cursor.execute("SELECT config,dimensions,expected_rows FROM eaggl_embedding_runs WHERE run_id=%s AND import_id=%s AND status='complete'",
                       (run_id, import_id))
        row = cursor.fetchone()
        if row is None:
            raise ValueError('Embedding run is not complete for this import')
        config, dimensions, expected = row
        run = {'run_id': run_id, 'config': json.loads(config), 'dimensions': dimensions, 'expected': expected}
        if (text_hash(canonical(run['config'])) != run_id or run['config']['import_id'] != import_id
                or run['config']['template'] != TEMPLATE):
            raise ValueError('Embedding run configuration mismatch')
        cursor.execute('SELECT factor_id,label,trait,input_sha256 FROM eaggl_factors WHERE import_id=%s ORDER BY factor_index', (import_id,))
        factors = [dict(zip(('factor_id', 'label', 'trait', 'input_sha256'), r)) for r in cursor.fetchall()]
        cursor.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM eaggl_name_embeddings WHERE run_id=%s', (run_id,))
        rows = cursor.fetchall()
        if len(rows) != expected or {r[0] for r in rows} != {f['input_sha256'] for f in factors}:
            raise ValueError('Incomplete embedding coverage')
        names = {f['input_sha256']: f['label'].strip() for f in factors}
        if any(text_hash(r[1]) != r[0] or names[r[0]] != r[1] for r in rows):
            raise ValueError('Embedding text/hash mismatch')
        run['calibration'] = [{'input_sha256': sha, 'input_text': text,
            'vector': decode_vector(blob, checksum, dimensions).tolist(), 'vector_sha256': checksum}
            for sha, text, blob, checksum in sorted(rows, key=lambda row: row[0])[:3]]
    return FactorSearchIndex(factors, run, rows)
