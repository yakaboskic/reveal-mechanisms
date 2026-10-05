"""Query text is embedded with the configured embedding service; there is no separate query-endpoint override."""
import numpy as np
import pytest

from reveal_backend import embedding_client
from reveal_backend.eaggl_bundle import text_hash
from reveal_backend.eaggl_embeddings import FactorSearchIndex, vector_hash
from reveal_backend.vector_retrieval import UpstashFactorIndex, embedding_config

PINNED = 'https://pinned.example.invalid'
RUNTIME = 'https://runtime.example.invalid'
SETTINGS = ('EMBEDDING_SERVICE_URL', 'EMBEDDING_MODEL', 'EMBEDDING_PROVIDER', 'REVEAL_QUERY_EMBEDDING_SERVICE_URL',
            'REVEAL_APPLICATION_TABLE_PREFIX')


@pytest.fixture(autouse=True)
def clean_query_environment(monkeypatch):
    for name in SETTINGS: monkeypatch.delenv(name, raising=False)


def release_index():
    return UpstashFactorIndex(['KPN.TRAIT:0000001::Factor1'], dimensions=2, release_id='a' * 64, environment_name='local', client=object())


def recorder(vector=(3., 4.)):
    calls = []
    def embed(texts, **kwargs):
        calls.append((texts, kwargs))
        return np.asarray([list(vector) for _ in texts])
    return calls, embed


def test_default_service_and_no_query_override():
    assert embedding_client.DEFAULT_SERVICE_URL == 'https://embedding-service-848707719401.us-east1.run.app'
    assert not hasattr(embedding_client, 'query_embedding_service_url')
    assert embedding_config() == {'model': embedding_client.DEFAULT_MODEL, 'provider': 'huggingface',
                                  'service_url': embedding_client.DEFAULT_SERVICE_URL}


def test_release_index_embeds_with_runtime_configuration_and_caches_per_configuration(monkeypatch):
    index = release_index()
    calls, embed = recorder()
    monkeypatch.setenv('REVEAL_APPLICATION_TABLE_PREFIX', 'reveal_workflow_qa')
    monkeypatch.setenv('REVEAL_QUERY_EMBEDDING_SERVICE_URL', 'https://ignored.example.invalid')
    monkeypatch.setenv('EMBEDDING_SERVICE_URL', RUNTIME + '/')
    monkeypatch.setenv('EMBEDDING_MODEL', 'runtime-model')
    monkeypatch.setenv('EMBEDDING_PROVIDER', 'runtime-provider')
    np.testing.assert_allclose(index.query_vectors(['new query'], embedder=embed), [[.6, .8]])
    index.query_vectors(['new query'], embedder=embed)
    assert calls == [(['new query'], {'model': 'runtime-model', 'provider': 'runtime-provider', 'service_url': RUNTIME,
                                      'max_workers': 1, 'max_retries': 2, 'timeout': 30})]
    for name, value in (('EMBEDDING_MODEL', 'other-model'), ('EMBEDDING_PROVIDER', 'other-provider'), ('EMBEDDING_SERVICE_URL', PINNED)):
        monkeypatch.setenv(name, value)
        index.query_vectors(['new query'], embedder=embed)
    # A configuration change never reuses a vector embedded under another configuration.
    assert [call[1]['model'] for call in calls] == ['runtime-model', 'other-model', 'other-model', 'other-model']
    assert calls[-1][1]['service_url'] == PINNED and calls[-1][1]['provider'] == 'other-provider'


def test_release_dimensions_and_finite_checks_never_cache_bad_vectors():
    index = release_index()
    for invalid in ([[1., 2., 3.]], [[float('nan'), 1.]], [[0., 0.]], [[1., 0.], [0., 1.]]):
        with pytest.raises(ValueError): index.query_vectors(['new query'], embedder=lambda *a, **kw: np.asarray(invalid))
        assert not index.query_cache.values and not index.query_cache.pending


def test_exact_index_embeds_with_its_stored_run_configuration(monkeypatch):
    monkeypatch.setenv('REVEAL_APPLICATION_TABLE_PREFIX', 'reveal_workflow_local')
    monkeypatch.setenv('REVEAL_QUERY_EMBEDDING_SERVICE_URL', 'https://ignored.example.invalid')
    blob = np.asarray([1., 0.], dtype='<f4').tobytes()
    run = {'run_id': 'frozen-run', 'dimensions': 2, 'config': {'model': 'frozen-model', 'provider': 'frozen-provider', 'service_url': PINNED}}
    index = FactorSearchIndex([{'factor_id': 'a', 'input_sha256': text_hash('a')}], run, [(text_hash('a'), 'a', blob, vector_hash(blob))])
    calls, embed = recorder((0., 1.))
    np.testing.assert_array_equal(index.query_vectors(['new query'], embedder=embed), [[0., 1.]])
    np.testing.assert_array_equal(index.query_vectors(['a'], embedder=embed), [[1., 0.]])  # Imported vectors need no service call.
    assert calls == [(['new query'], {'model': 'frozen-model', 'provider': 'frozen-provider', 'service_url': PINNED,
                                      'max_workers': 1, 'max_retries': 2, 'timeout': 30})]
