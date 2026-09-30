"""Snapshot-pinned Upstash retrieval; no serving reads of MySQL vector blobs.

The provider supplies candidates. Exact cosines are evaluated only for their
bounded native-factor aliases, retaining each context's real similarity.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import hashlib
import base64
import math
import re
from typing import Protocol

import httpx
import numpy as np

from .eaggl_embeddings import QueryVectorCache, validate_vectors
from .embedding_client import get_embeddings
from .repository import canonical, digest
from .runtime_config import setting

POLICY_VERSION = 'upstash-cosine-alias-expansion-v1'
ROUNDTRIP_ATOL = 2e-6
ROUNDTRIP_RTOL = 2e-5
MAX_CANDIDATES = 1000
MAX_FETCH_ALIASES = 4096


class VectorUnavailable(RuntimeError):
    """Semantic retrieval cannot safely return a scientific match."""


class RetrievalIndex(Protocol):
    run: dict
    factors: list
    candidate_limit: int
    def query_vectors(self, texts, *, embedder=get_embeddings): ...
    def candidates(self, vectors, top_k, *, exclude=()): ...
    def fetch_vectors(self, factor_ids): ...


def normalized(vectors, dimensions):
    validate_vectors(vectors, len(vectors), dimensions)
    result = np.asarray(vectors, dtype=np.float64)
    return result / np.linalg.norm(result, axis=1, keepdims=True)


def value(row, name, default=None):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def vector_checksum(vector):
    return hashlib.sha256(np.asarray(vector, dtype='<f8').tobytes()).hexdigest()


def query_vector_provenance(vectors):
    return {'query_vector_checksums': [vector_checksum(vector) for vector in vectors],
            'query_vectors': np.asarray(vectors).tolist(), 'query_vector_encoding': 'float64-le/base64',
            'query_vectors_base64': [base64.b64encode(np.asarray(vector, dtype='<f8').tobytes()).decode('ascii') for vector in vectors]}


def cosine_score(score):
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -1e-6 <= score <= 1 + 1e-6:
        raise VectorUnavailable('Invalid cosine score from Vector')
    return float(np.clip(2 * score - 1, -1, 1))


def client_from_environment(*, write=False):
    from upstash_vector import Index
    url = setting('UPSTASH_VECTOR_REST_URL', '')
    token = setting('UPSTASH_VECTOR_WRITE_TOKEN') if write else setting('UPSTASH_VECTOR_REST_TOKEN')
    if not url.startswith('https://') or not token:
        raise VectorUnavailable('Configure separate Upstash Vector URL and serving/write credentials')
    index = Index(url=url, token=token, retries=1, retry_interval=.2, allow_telemetry=False)
    # SDK 0.8.0 hardcodes a 600-second timeout. Pin the SDK and replace its
    # transport timeout; HTTP workflow/application requests must remain bounded.
    index._client.timeout = httpx.Timeout(20, connect=5)
    return index


def embedding_space(run, context_config):
    return digest({'run_id': run['run_id'], 'config': run['config'], 'dimensions': run['dimensions'],
                   'calibration': [{**probe, 'vector': np.asarray(probe['vector'], dtype='<f4').tolist()} for probe in run['calibration']], 'context_templates': context_config['templates'],
                   'query_normalization': 'l2-float64', 'metric': 'cosine'})


def metadata(snapshot, row):
    return {**row['binding'], 'snapshot_id': snapshot['snapshot_id'], 'embedding_space': snapshot['embedding_space'],
            'original_vector_sha256': row['original_vector_sha256']}


def verify_record(snapshot, expected, record, *, vector=True):
    if record is None or value(record, 'id') != expected['id'] or value(record, 'metadata') != metadata(snapshot, expected):
        raise VectorUnavailable('Vector record does not match the frozen source binding')
    if not vector:
        return None
    raw = value(record, 'vector')
    try:
        validate_vectors([raw], 1, snapshot['run']['dimensions'])
    except (TypeError, ValueError) as error:
        raise VectorUnavailable('Vector readback has invalid dimensions or values') from error
    # Upstash returns raw vector float32 values. The import gate checks original
    # numeric tolerance and records a provider readback checksum separately.
    if vector_checksum(raw) != expected['roundtrip_sha256']:
        raise VectorUnavailable('Vector readback differs from the verified immutable snapshot')
    return normalized([raw], snapshot['run']['dimensions'])[0]


class StoredContextVectors(Mapping):
    """Fetch only selected contexts; membership tests perform no network I/O."""
    def __init__(self, index):
        self.index = index
    def __len__(self): return len(self.index.context_by_id)
    def __iter__(self): return iter(self.index.context_by_id)
    def __contains__(self, identity): return identity in self.index.context_by_id
    def __getitem__(self, identity): return self.index.context_vectors([identity])[0]


class UpstashFactorIndex:
    candidate_limit = MAX_CANDIDATES

    def __init__(self, snapshot, *, client=None):
        self.snapshot, self.run = snapshot, snapshot['run']
        if (snapshot.get('status') != 'complete' or snapshot.get('policy_version') != POLICY_VERSION
                or snapshot.get('embedding_space') != embedding_space(self.run, snapshot['context_config'])):
            raise VectorUnavailable('Vector snapshot is incomplete or incompatible')
        for key in ('factor_namespace', 'context_namespace'):
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', snapshot.get(key, '')):
                raise VectorUnavailable('An explicit Vector snapshot namespace is required')
        self.client = client if client is not None else client_from_environment()
        self.factors = [row['binding'] for row in snapshot['factors']]
        self.by_id = {row['binding']['factor_id']: row for row in snapshot['factors']}
        self.by_vector_id = {row['id']: row for row in snapshot['factors']}
        self.context_by_id = {row['binding']['source_id']: row for row in snapshot['contexts']}
        self.query_cache = QueryVectorCache()
        self.vector_cache = QueryVectorCache(max_entries=256)
        self.text_bindings = {}
        for identity, row in self.by_id.items():
            self.text_bindings[row['binding']['input_sha256']] = ('factor', identity, row['binding']['input_text'])
        for identity, row in self.context_by_id.items():
            self.text_bindings[row['binding']['input_sha256']] = ('context', identity, row['binding']['input_text'])
        self.contexts = {'run_id': snapshot['context_run_id'], 'config': snapshot['context_config'],
            'bindings': {identity: {key: row['binding'][key] for key in ('source_id', 'source_kind', 'source_revision', 'template', 'input_sha256', 'input_text')}
                         for identity, row in self.context_by_id.items()}, 'vectors': StoredContextVectors(self)}

    def check(self):
        try:
            info = self.client.info()
            if value(info, 'dimension') != self.run['dimensions'] or str(value(info, 'similarity_function')).upper() != 'COSINE':
                raise VectorUnavailable('Vector index dimensions or metric are incompatible')
            namespaces = value(info, 'namespaces', {})
            for kind in ('factor', 'context'):
                record = namespaces.get(self.snapshot[kind + '_namespace'])
                expected = len(self.snapshot['factors' if kind == 'factor' else 'contexts'])
                if record is None or value(record, 'vector_count') != expected or value(record, 'pending_vector_count', 0):
                    raise VectorUnavailable('Vector namespace coverage is not ready')
        except VectorUnavailable:
            raise
        except Exception as error:
            raise VectorUnavailable('Upstash Vector readiness check failed') from error

    def _fetch(self, rows, namespace):
        if not rows: return np.empty((0, self.run['dimensions']))
        if len(rows) > MAX_FETCH_ALIASES:
            raise VectorUnavailable('Candidate alias coverage exceeds the bounded retrieval policy')
        results = []
        try:
            for start in range(0, len(rows), 128):
                batch = rows[start:start + 128]
                fetched = self.client.fetch(ids=[row['id'] for row in batch], namespace=namespace,
                                            include_vectors=True, include_metadata=True)
                if len(fetched) != len(batch):
                    raise VectorUnavailable('Vector fetch returned incomplete coverage')
                results.extend(verify_record(self.snapshot, row, record) for row, record in zip(batch, fetched))
            return np.stack(results)
        except VectorUnavailable:
            raise
        except Exception as error:
            raise VectorUnavailable('Upstash Vector fetch is unavailable') from error

    def fetch_vectors(self, factor_ids):
        return self._fetch([self.by_id[identity] for identity in factor_ids], self.snapshot['factor_namespace'])

    def context_vectors(self, identities):
        return self._fetch([self.context_by_id[identity] for identity in identities], self.snapshot['context_namespace'])

    def query_vectors(self, texts, *, embedder=get_embeddings):
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError('Queries cannot be blank')
        config = self.run['config']
        def fetch(missing):
            found, novel = {}, []
            for text in missing:
                sha = hashlib.sha256(text.encode()).hexdigest()
                known = self.text_bindings.get(sha)
                if known and known[2] == text:
                    found[text] = (self.fetch_vectors([known[1]]) if known[0] == 'factor' else self.context_vectors([known[1]]))[0]
                else: novel.append(text)
            if novel:
                raw = embedder(novel, model=config['model'], provider=config['provider'],
                    service_url=config['service_url'], max_workers=1, max_retries=2, timeout=30)
                validate_vectors(raw, len(novel), self.run['dimensions'])
                vectors = normalized(raw, self.run['dimensions'])
                found.update(zip(novel, vectors))
            return np.stack([found[text] for text in missing])
        return self.query_cache.get(texts, self.snapshot['embedding_space'], fetch)

    def candidates(self, vectors, top_k, *, exclude=()):
        if not 1 <= top_k <= self.candidate_limit:
            raise ValueError('Candidate depth exceeds the bounded retrieval policy')
        vectors = normalized(vectors, self.run['dimensions'])
        # snapshot_id is a locally generated digest, never a raw query/filter.
        snapshot_id = self.snapshot['snapshot_id']
        if not re.fullmatch('[a-f0-9]{64}', snapshot_id):
            raise VectorUnavailable('Invalid snapshot identity')
        filter_expression = "snapshot_id = '" + snapshot_id + "'"
        try:
            # Upstash caps the aggregate reads of query_many at 1,000,
            # not merely top_k for each individual query.
            batch_size = max(1, min(32, 1000 // top_k))
            batches = []
            for start in range(0, len(vectors), batch_size):
                batches.extend(self.client.query_many(queries=[{'vector': vector.tolist(), 'top_k': top_k,
                    'include_metadata': True, 'filter': filter_expression} for vector in vectors[start:start + batch_size]],
                    namespace=self.snapshot['factor_namespace']))
            if len(batches) != len(vectors): raise VectorUnavailable('Vector batch query returned incomplete coverage')
            result = []
            for batch in batches:
                if not batch and self.factors: raise VectorUnavailable('Vector returned no candidates for a nonempty verified snapshot')
                rows = []
                for record in batch:
                    expected = self.by_vector_id.get(value(record, 'id'))
                    if expected is None: raise VectorUnavailable('Vector query returned an unknown source binding')
                    verify_record(self.snapshot, expected, record, vector=False)
                    score = value(record, 'score')
                    rows.append({'factor_id': expected['binding']['factor_id'], 'cosine_similarity': cosine_score(score),
                                 'provider_score': score, 'vector_id': expected['id']})
                result.append(rows)
            return result
        except VectorUnavailable: raise
        except Exception as error: raise VectorUnavailable('Upstash Vector candidate retrieval is unavailable') from error

    def provenance(self):
        return {key: self.snapshot[key] for key in ('snapshot_id', 'embedding_space', 'factor_namespace', 'context_namespace', 'policy_version', 'export_ref')} | {
            'provider': 'upstash-vector', 'metric': 'COSINE', 'score_conversion': '2 * provider_score - 1',
            'score_conversion_version': 1, 'aggregation': 'maximum_per_context_over_all_native_aliases',
            'candidate_limit': self.candidate_limit, 'mapping_run_id': self.snapshot.get('mapping_run'),
            'embedding_run_id': self.run['run_id'], 'dismech_embedding_run_id': self.snapshot['context_run_id'],
            'dismech_import_id': self.snapshot.get('dismech_import'), 'roundtrip_tolerance': {'atol': ROUNDTRIP_ATOL, 'rtol': ROUNDTRIP_RTOL}}


def retrieve_native(index, factor_legacy, vectors, limit, exclude=()):
    """Shared exact/reference and ANN retrieval contract, with bounded expansion."""
    if not limit or not len(vectors): return []
    vectors = np.asarray(vectors, dtype=np.float64)
    validate_vectors(vectors, len(vectors), index.run['dimensions'])
    if not np.allclose(np.linalg.norm(vectors, axis=1), 1):
        raise ValueError('Query vectors must be normalized in the selected space')
    exclude = set(exclude)
    aliases = defaultdict(list)
    for factor in index.factors:
        record = factor_legacy.get(factor['factor_id'])
        if record and record['source_id'] not in exclude:
            aliases[record['source_id']].append(factor['factor_id'])
    if not aliases: return []
    target = min(limit, len(aliases))
    ceiling = min(len(index.factors), index.candidate_limit)
    depth = min(ceiling, max(32, target * 4))
    batches = []
    while depth:
        batches = index.candidates(vectors, depth, exclude=exclude)
        per_context = [{factor_legacy[row['factor_id']]['source_id'] for row in batch
                        if row['factor_id'] in factor_legacy and factor_legacy[row['factor_id']]['source_id'] in aliases}
                       for batch in batches]
        if all(len(found) >= target for found in per_context) or depth == ceiling or all(len(batch) < depth for batch in batches): break
        depth = min(ceiling, depth * 2)
    selected = set().union(*per_context)
    # Fetch every alias of selected native identities, including aliases absent
    # from ANN results. This also supplies non-winning context similarities.
    factor_ids = [identity for native in sorted(selected) for identity in aliases[native]]
    matrix = index.fetch_vectors(factor_ids)
    scores = matrix @ np.asarray(vectors).T
    grouped = {}
    for identity, row in zip(factor_ids, scores):
        native = factor_legacy[identity]['source_id']
        grouped[native] = np.maximum(grouped[native], row) if native in grouped else row.copy()
    result = []
    for native, scores in grouped.items():
        cosine = np.clip(scores, -1, 1)
        item = {'record': factor_legacy[aliases[native][0]], 'scores': cosine, 'value': float(cosine.max())}
        if hasattr(index, 'provenance'):
            item['retrieval'] = {**index.provenance(), 'candidate_depth': depth, 'candidate_count': len(selected),
                **query_vector_provenance(vectors),
                'candidate_hits': [[dict(row) for row in batch] for batch in batches],
                'aliases': [{'id': index.by_id[identity]['id'], 'original_vector_sha256': index.by_id[identity]['original_vector_sha256']}
                            for identity in aliases[native]]}
        result.append(item)
    return sorted(result, key=lambda item: (-item['value'], item['record']['source_id']))[:target]
