"""Upstash retrieval over this environment's fixed reference namespaces; no serving reads of MySQL vector blobs.

One index per environment (REVEAL_VECTOR_ENVIRONMENT): `<env>-factors` holds one vector per factor
(id = factor key KPN.TRAIT:NNNNNNN::FactorN) and `<env>-contexts` one vector per exact DisMech
context text (id = sha256 hex of its UTF-8 text). The reference release publisher fills them with
the reference tables.

The provider supplies candidates; ids the served factor table does not hold are skipped. Exact
cosines are evaluated from the fetched vectors of the selected factors, retaining each context's
real similarity. Query text is embedded with the configured embedding service (EMBEDDING_*
settings); the dimensions come from the published release manifest.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import base64
import math
import re
from typing import Protocol

import httpx
import numpy as np

from .eaggl_embeddings import QueryVectorCache, validate_vectors
from .embedding_client import DEFAULT_MODEL, DEFAULT_SERVICE_URL, get_embeddings
from .repository import canonical, digest
from .runtime_config import setting

POLICY_VERSION = 'upstash-cosine-reference-release-v1'
MAX_CANDIDATES = 1000
MAX_FETCH = 4096
FETCH_BATCH = 128
ENVIRONMENT_RE = re.compile(r'[a-z][a-z0-9_-]{0,24}')
# Corpus kind -> namespace suffix of `<env>-<suffix>` (the reference release publishes only these two).
NAMESPACES = {'factors': 'factors', 'contexts': 'contexts'}


class VectorUnavailable(RuntimeError):
    """Semantic retrieval cannot safely return a scientific match."""


class RetrievalIndex(Protocol):
    factors: list
    dimensions: int
    candidate_limit: int
    def query_vectors(self, texts, *, embedder=get_embeddings): ...
    def candidates(self, vectors, top_k, *, exclude=()): ...
    def fetch_vectors(self, factor_ids): ...


def environment(name=None):
    """The vector environment (REVEAL_VECTOR_ENVIRONMENT, default local) that names the namespaces."""
    name = name or setting('REVEAL_VECTOR_ENVIRONMENT', 'local')
    if not ENVIRONMENT_RE.fullmatch(name): raise VectorUnavailable('Invalid vector environment')
    return name


def namespace(kind, environment_name=None):
    """`<env>-factors` or `<env>-contexts`."""
    return environment(environment_name) + '-' + NAMESPACES[kind]


def embedding_config():
    """Query embedding settings from the runtime configuration (never from a stored run)."""
    return {'model': setting('EMBEDDING_MODEL') or DEFAULT_MODEL, 'provider': setting('EMBEDDING_PROVIDER') or 'huggingface',
            'service_url': (setting('EMBEDDING_SERVICE_URL') or DEFAULT_SERVICE_URL).rstrip('/')}


def text_sha256(text):
    """Context vector id: sha256 hex of the exact UTF-8 text."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


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


def embedding_space(embedding):
    """Run id of a release's embedding space (its manifest `embedding` block). Vectors are a function of the model and the
    text, so anchors bound under different releases of one space still share one run: evidence_package
    frozen_semantic_association compares the run ids of a binding and of its suggestion."""
    return digest({key: (embedding or {}).get(key) for key in ('model', 'model_revision', 'provider', 'dimensions')})


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


class Candidates(list):
    """One query's served candidates; `returned` counts every provider hit, skipped unknown ids included."""
    def __init__(self, rows, returned):
        super().__init__(rows)
        self.returned = returned


class UpstashFactorIndex:
    """`<env>-factors` (query and fetch) and `<env>-contexts` (fetch) for one served reference release."""
    candidate_limit = MAX_CANDIDATES

    def __init__(self, factor_ids, *, dimensions, release_id, embedding_run=None, environment_name=None, client=None):
        if type(dimensions) is not int or dimensions < 1:
            raise VectorUnavailable('The reference release does not name its embedding dimensions')
        self.environment = environment(environment_name)
        self.factor_namespace, self.context_namespace = namespace('factors', self.environment), namespace('contexts', self.environment)
        self.dimensions, self.release_id, self.embedding_run = dimensions, release_id, embedding_run or release_id
        self.factors = [{'factor_id': identity} for identity in factor_ids]
        self.served = frozenset(factor_ids)
        self.client = client if client is not None else client_from_environment()
        self.query_cache, self.context_cache = QueryVectorCache(), QueryVectorCache(max_entries=8192)
        # Factor key -> the publisher's vector checksum (fetched metadata), recorded in retrieval provenance.
        self.vector_sha256 = {}

    def check(self):
        """Readiness: the index is reachable, its dimension and cosine metric match, and the factor namespace is non-empty."""
        try:
            info = self.client.info()
            if value(info, 'dimension') != self.dimensions or str(value(info, 'similarity_function')).upper() != 'COSINE':
                raise VectorUnavailable('Vector index dimensions or metric are incompatible with the reference release')
            record = (value(info, 'namespaces', {}) or {}).get(self.factor_namespace)
            if record is None or not value(record, 'vector_count', 0):
                raise VectorUnavailable('The factor vector namespace is empty')
        except VectorUnavailable:
            raise
        except Exception as error:
            raise VectorUnavailable('Upstash Vector readiness check failed') from error

    def _fetch(self, namespace_name, identities):
        """{id: (normalized vector, metadata)} of the requested ids; ids the provider does not hold are absent."""
        identities = list(dict.fromkeys(identities))
        if len(identities) > MAX_FETCH:
            raise VectorUnavailable('Vector fetch exceeds the bounded retrieval policy')
        result = {}
        try:
            for start in range(0, len(identities), FETCH_BATCH):
                batch = identities[start:start + FETCH_BATCH]
                fetched = self.client.fetch(ids=batch, namespace=namespace_name, include_vectors=True, include_metadata=True)
                if len(fetched) != len(batch):
                    raise VectorUnavailable('Vector fetch returned incomplete coverage')
                for identity, record in zip(batch, fetched):
                    if record is None or value(record, 'id') != identity: continue
                    try: vector = normalized([value(record, 'vector')], self.dimensions)[0]
                    except (TypeError, ValueError) as error:
                        raise VectorUnavailable('Vector readback has invalid dimensions or values') from error
                    vector.setflags(write=False)
                    result[identity] = (vector, value(record, 'metadata') or {})
            return result
        except VectorUnavailable:
            raise
        except Exception as error:
            raise VectorUnavailable('Upstash Vector fetch is unavailable') from error

    def fetch_vectors(self, factor_ids):
        """{factor key: normalized vector} of the found factors; a factor without a vector is absent."""
        found = self._fetch(self.factor_namespace, [identity for identity in factor_ids if identity in self.served])
        for identity, (_, metadata) in found.items():
            if isinstance(metadata, dict) and metadata.get('vector_sha256'): self.vector_sha256[identity] = metadata['vector_sha256']
        return {identity: vector for identity, (vector, _) in found.items()}

    def query_vectors(self, texts, *, embedder=get_embeddings):
        """Live query embeddings in the configured space, cached in process."""
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError('Queries cannot be blank')
        config = embedding_config()
        def fetch(missing):
            raw = embedder(missing, model=config['model'], provider=config['provider'], service_url=config['service_url'],
                           max_workers=1, max_retries=2, timeout=30)
            validate_vectors(raw, len(missing), self.dimensions)
            return normalized(raw, self.dimensions)
        return self.query_cache.get(texts, ('query', canonical(config), self.dimensions), fetch)

    def context_vectors(self, texts, *, embedder=get_embeddings):
        """Exact DisMech context texts: `<env>-contexts` by sha256(text); a miss is embedded live. Cached in process."""
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError('Context texts cannot be blank')
        def fetch(missing):
            stored = self._fetch(self.context_namespace, [text_sha256(text) for text in missing])
            found = {text: stored[text_sha256(text)][0] for text in missing if text_sha256(text) in stored}
            novel = [text for text in missing if text not in found]
            if novel: found.update(zip(novel, self.query_vectors(novel, embedder=embedder)))
            return np.stack([found[text] for text in missing])
        return self.context_cache.get(texts, ('context', self.context_namespace, self.release_id, canonical(embedding_config()), self.dimensions), fetch)

    def candidates(self, vectors, top_k, *, exclude=()):
        if not 1 <= top_k <= self.candidate_limit:
            raise ValueError('Candidate depth exceeds the bounded retrieval policy')
        vectors = normalized(vectors, self.dimensions)
        try:
            # Upstash caps the aggregate reads of query_many at 1,000,
            # not merely top_k for each individual query.
            batch_size = max(1, min(32, 1000 // top_k))
            batches = []
            for start in range(0, len(vectors), batch_size):
                batches.extend(self.client.query_many(queries=[{'vector': vector.tolist(), 'top_k': top_k}
                    for vector in vectors[start:start + batch_size]], namespace=self.factor_namespace))
            if len(batches) != len(vectors): raise VectorUnavailable('Vector batch query returned incomplete coverage')
            result = []
            for batch in batches:
                rows = []
                for record in batch:
                    identity = value(record, 'id')
                    if identity not in self.served: continue  # Not served by this release's factor table.
                    score = value(record, 'score')
                    rows.append({'factor_id': identity, 'cosine_similarity': cosine_score(score), 'provider_score': score, 'vector_id': identity})
                result.append(Candidates(rows, len(batch)))
            return result
        except VectorUnavailable: raise
        except Exception as error: raise VectorUnavailable('Upstash Vector candidate retrieval is unavailable') from error

    def provenance(self):
        config = embedding_config()
        return {'provider': 'upstash-vector', 'metric': 'COSINE', 'score_conversion': '2 * provider_score - 1',
                'score_conversion_version': 1, 'aggregation': 'maximum_per_context_over_all_native_aliases',
                'candidate_limit': self.candidate_limit, 'policy_version': POLICY_VERSION, 'release_id': self.release_id,
                'factor_namespace': self.factor_namespace, 'context_namespace': self.context_namespace,
                'query_embedding': {'model': config['model'], 'provider': config['provider'], 'dimensions': self.dimensions},
                'mapping_run_id': self.embedding_run, 'embedding_run_id': self.embedding_run, 'dismech_embedding_run_id': self.embedding_run}


def retrieve_native(index, factor_legacy, vectors, limit, exclude=()):
    """ANN candidates expanded until each context finds `limit` native factors, then exact cosines."""
    if not limit or not len(vectors): return []
    vectors = np.asarray(vectors, dtype=np.float64)
    validate_vectors(vectors, len(vectors), index.dimensions)
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
    while True:
        batches = index.candidates(vectors, depth, exclude=exclude)
        per_context = [{factor_legacy[row['factor_id']]['source_id'] for row in batch
                        if row['factor_id'] in factor_legacy and factor_legacy[row['factor_id']]['source_id'] in aliases}
                       for batch in batches]
        returned = [getattr(batch, 'returned', len(batch)) for batch in batches]
        # Ids the release does not serve take provider slots: allow the full bounded depth.
        if any(count > len(batch) for count, batch in zip(returned, batches)): ceiling = index.candidate_limit
        if all(len(found) >= target for found in per_context) or depth >= ceiling or all(count < depth for count in returned): break
        depth = min(ceiling, depth * 2)
    selected = set().union(*per_context)
    # Fetch every alias of the selected native identities, including aliases absent
    # from ANN results. This also supplies non-winning context similarities.
    factor_ids = [identity for native in sorted(selected) for identity in aliases[native]]
    fetched = index.fetch_vectors(factor_ids)
    grouped = {}
    for identity in factor_ids:
        if identity not in fetched: continue
        native, row = factor_legacy[identity]['source_id'], np.asarray(fetched[identity]) @ vectors.T
        grouped[native] = np.maximum(grouped[native], row) if native in grouped else row.copy()
    result = []
    for native, scores in grouped.items():
        cosine = np.clip(scores, -1, 1)
        item = {'record': factor_legacy[aliases[native][0]], 'scores': cosine, 'value': float(cosine.max())}
        if hasattr(index, 'provenance'):
            item['retrieval'] = {**index.provenance(), 'candidate_depth': depth, 'candidate_count': len(selected),
                **query_vector_provenance(vectors),
                'candidate_hits': [[dict(row) for row in batch] for batch in batches],
                'aliases': [{'id': identity, 'vector_sha256': getattr(index, 'vector_sha256', {}).get(identity)}
                            for identity in aliases[native] if identity in fetched]}
        result.append(item)
    return sorted(result, key=lambda item: (-item['value'], item['record']['source_id']))[:target]
