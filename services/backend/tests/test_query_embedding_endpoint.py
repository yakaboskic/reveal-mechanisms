"""Local and QA transport aliases must preserve the frozen scientific embedding space."""
from copy import deepcopy
import hashlib

import numpy as np
import pytest

from reveal_backend.eaggl_bundle import text_hash
from reveal_backend.eaggl_embeddings import FactorSearchIndex, vector_hash
from reveal_backend.embedding_client import query_embedding_service_url
from reveal_backend.vector_retrieval import UpstashFactorIndex
from test_vector_retrieval import fixture


OVERRIDE = 'REVEAL_QUERY_EMBEDDING_SERVICE_URL'
PINNED = 'https://pinned.example.invalid'
REPLACEMENT = 'https://replacement.example.invalid'


@pytest.fixture(autouse=True)
def clean_query_environment(monkeypatch):
    monkeypatch.delenv(OVERRIDE, raising=False)
    monkeypatch.delenv('REVEAL_APPLICATION_TABLE_PREFIX', raising=False)


def test_query_transport_requires_explicit_opt_in_and_exact_environment_prefix(monkeypatch):
    monkeypatch.setenv('EMBEDDING_SERVICE_URL', REPLACEMENT)
    assert query_embedding_service_url(PINNED) == PINNED
    monkeypatch.setenv(OVERRIDE, REPLACEMENT + '/')
    for prefix in ('', 'reveal', 'reveal_workflow_prod', 'reveal_compose', 'reveal_reload_rehearsal',
                   'reveal_workflow_local_copy', 'reveal_workflow_qa_copy'):
        monkeypatch.setenv('REVEAL_APPLICATION_TABLE_PREFIX', prefix)
        with pytest.raises(ValueError, match='allowed only for reveal_workflow_local') as failure:
            query_embedding_service_url(PINNED)
        assert REPLACEMENT not in str(failure.value)
    for prefix in ('reveal_workflow_local', 'reveal_workflow_qa'):
        monkeypatch.setenv('REVEAL_APPLICATION_TABLE_PREFIX', prefix)
        assert query_embedding_service_url(PINNED) == REPLACEMENT


@pytest.mark.parametrize('invalid', [
    'http://untrusted.example.invalid', 'https://', 'https:///missing-host', 'not-a-url',
    'https://user:private@host.invalid', 'https://@host.invalid', 'https://host.invalid?token=private',
    'https://host.invalid#private', 'https://host.invalid?', 'https://host.invalid#',
    'https://host.invalid:bad', 'https://host.invalid:70000',
    'https://host.invalid:0', 'https://host.invalid/ private', 'https://[invalid',
    'https://bad\\host.invalid', 'https://host..invalid', 'https://host.invalid/\x01private',
])
def test_invalid_query_override_fails_without_echoing_endpoint(monkeypatch, invalid):
    monkeypatch.setenv('REVEAL_APPLICATION_TABLE_PREFIX', 'reveal_workflow_local')
    monkeypatch.setenv(OVERRIDE, invalid)
    with pytest.raises(ValueError, match='must be an HTTPS base URL') as failure:
        query_embedding_service_url(PINNED)
    assert invalid not in str(failure.value) and 'private' not in str(failure.value)


def query_index(kind):
    if kind == 'upstash':
        snapshot, provider, _ = fixture()
        index = UpstashFactorIndex(snapshot, client=provider)
        # The provider test fixture uses illustrative hashes; pin this exact label for reuse.
        index.text_bindings[hashlib.sha256(b'a').hexdigest()] = ('factor', 'a', 'a')
        return index
    blob = np.asarray([1., 0.], dtype='<f4').tobytes()
    run = {'run_id': 'frozen-run', 'dimensions': 2, 'config': {
        'model': 'frozen-model', 'provider': 'frozen-provider', 'service_url': PINNED}}
    return FactorSearchIndex([{'factor_id': 'a', 'input_sha256': text_hash('a')}], run,
                             [(text_hash('a'), 'a', blob, vector_hash(blob))])


@pytest.mark.parametrize('prefix', ['reveal_workflow_local', 'reveal_workflow_qa'])
@pytest.mark.parametrize('kind', ['upstash', 'exact'])
def test_both_query_paths_preserve_pins_and_separate_transport_caches(monkeypatch, kind, prefix):
    index = query_index(kind)
    frozen_run = deepcopy(index.run)
    frozen_snapshot = deepcopy(getattr(index, 'snapshot', None))
    calls = []
    def embed(texts, **kwargs):
        calls.append((texts, kwargs))
        vector = [0., 1.] if kwargs['service_url'] == REPLACEMENT else [1., 0.]
        return np.asarray([vector for _ in texts])
    first = index.query_vectors(['new query'], embedder=embed)
    index.query_vectors(['new query'], embedder=embed)
    assert len(calls) == 1
    monkeypatch.setenv('REVEAL_APPLICATION_TABLE_PREFIX', prefix)
    monkeypatch.setenv(OVERRIDE, REPLACEMENT)
    second = index.query_vectors(['new query'], embedder=embed)
    assert len(calls) == 2  # A transport change cannot reuse a result fetched at the old endpoint.
    assert not np.array_equal(first, second)
    assert calls[1][1] == {'model': frozen_run['config']['model'], 'provider': frozen_run['config']['provider'],
                           'service_url': REPLACEMENT, 'max_workers': 1, 'max_retries': 2, 'timeout': 30}
    index.query_vectors(['new query'], embedder=embed)
    np.testing.assert_array_equal(index.query_vectors(['a'], embedder=embed), [[1., 0.]])
    assert len(calls) == 2  # Exact imported vectors remain reusable without invoking the service.
    monkeypatch.delenv(OVERRIDE)
    np.testing.assert_array_equal(index.query_vectors(['new query'], embedder=embed), first)
    assert len(calls) == 2
    assert index.run == frozen_run and getattr(index, 'snapshot', None) == frozen_snapshot


@pytest.mark.parametrize('prefix', ['reveal_workflow_local', 'reveal_workflow_qa'])
@pytest.mark.parametrize('kind', ['upstash', 'exact'])
def test_query_override_keeps_dimension_and_finite_checks(monkeypatch, kind, prefix):
    monkeypatch.setenv('REVEAL_APPLICATION_TABLE_PREFIX', prefix)
    monkeypatch.setenv(OVERRIDE, REPLACEMENT)
    index = query_index(kind)
    for invalid in ([[1., 2., 3.]], [[float('nan'), 1.]], [[0., 0.]]):
        with pytest.raises(ValueError): index.query_vectors(['new query'], embedder=lambda *a, **kw: np.asarray(invalid))
        assert not index.query_cache.values and not index.query_cache.pending
