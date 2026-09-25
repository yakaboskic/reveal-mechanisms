import io
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import numpy as np

from reveal_backend import embedding_client as client


def response(vectors, **changes):
    body = {"embeddings": vectors, "count": len(vectors), "dimensions": len(vectors[0])}
    body.update(changes)
    return io.BytesIO(json.dumps(body).encode())


class EmbeddingClientTests(unittest.TestCase):
    def test_default_model_and_header_contract(self):
        with patch.dict(os.environ, {"EMBEDDING_SERVICE_API_KEY": "test-key"}, clear=True), \
             patch.object(client.urllib.request, 'urlopen', return_value=response([[1, 2]])) as send:
            result = client.get_embeddings(['mechanism'])
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, client.DEFAULT_SERVICE_URL + '/embed')
        self.assertEqual(request.get_header('X-api-key'), 'test-key')
        self.assertEqual(json.loads(request.data), {'texts': ['mechanism'], 'model': client.DEFAULT_MODEL, 'provider': 'huggingface'})
        self.assertEqual(result.dtype, np.float32)
        self.assertEqual(result.shape, (1, 2))

    def test_empty_and_missing_key_do_not_send(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(client.urllib.request, 'urlopen') as send:
            self.assertEqual(client.get_embeddings([]).shape, (0, 0))
            with self.assertRaisesRegex(ValueError, 'API_KEY'):
                client.get_embeddings(['a'])
            send.assert_not_called()

    def test_batch_limit_and_parameters(self):
        with patch.object(client.urllib.request, 'urlopen') as send:
            for kwargs in ({'batch_size': 0}, {'batch_size': 101}, {'max_workers': 0},
                           {'log_every': 0}, {'max_retries': -1}, {'timeout': 0}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    client.get_embeddings(['a'], api_key='test', **kwargs)
            send.assert_not_called()

    def test_service_batch_boundary_and_order(self):
        sizes = []
        def send(request, **kwargs):
            texts = json.loads(request.data)['texts']
            sizes.append(len(texts))
            return response([[int(text), 1] for text in texts])
        with patch.object(client.urllib.request, 'urlopen', side_effect=send):
            result = client.get_embeddings([str(i) for i in range(201)], api_key='test')
        self.assertEqual(sizes, [100, 100, 1])
        np.testing.assert_array_equal(result[:, 0], np.arange(201))

    def test_parallel_completion_preserves_input_order(self):
        second_finished = threading.Event()
        def send(request, **kwargs):
            text = json.loads(request.data)['texts'][0]
            if text == '0':
                self.assertTrue(second_finished.wait(2))
            else:
                second_finished.set()
            return response([[int(text), 1]])
        with patch.object(client.urllib.request, 'urlopen', side_effect=send):
            result = client.get_embeddings(['0', '1'], api_key='test', batch_size=1, max_workers=2)
        np.testing.assert_array_equal(result[:, 0], [0, 1])

    def test_transient_retry_and_auth_failure(self):
        transient = HTTPError(client.DEFAULT_SERVICE_URL, 503, 'busy', {}, io.BytesIO(b''))
        with patch.object(client.urllib.request, 'urlopen', side_effect=[transient, response([[1]])]) as send, \
             patch.object(client.time, 'sleep') as sleep:
            client.get_embeddings(['a'], api_key='test', max_retries=1)
            self.assertEqual(send.call_count, 2)
            sleep.assert_called_once()
        unauthorized = HTTPError(client.DEFAULT_SERVICE_URL, 401, 'unauthorized', {}, io.BytesIO(b''))
        with patch.object(client.urllib.request, 'urlopen', side_effect=unauthorized) as send, \
             patch.object(client.time, 'sleep') as sleep:
            with self.assertRaises(HTTPError):
                client.get_embeddings(['a'], api_key='test')
            self.assertEqual(send.call_count, 1)
            sleep.assert_not_called()

    def test_bad_response_cannot_enter_vector_corpus(self):
        bodies = [
            {'embeddings': [[1, 2]], 'dimensions': 2, 'count': 2},
            {'embeddings': [[1]], 'dimensions': 2, 'count': 1},
            {'embeddings': [[float('nan')]], 'dimensions': 1, 'count': 1},
            {'embeddings': [['1']], 'dimensions': 1, 'count': 1},
            {'embeddings': [[1e100]], 'dimensions': 1, 'count': 1},
        ]
        for body in bodies:
            with self.subTest(body=body), patch.object(client.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(body).encode())):
                with self.assertRaises(ValueError):
                    client.get_embeddings(['a'], api_key='test')

    def test_inconsistent_batch_dimensions_fail(self):
        with patch.object(client.urllib.request, 'urlopen', side_effect=[response([[1]]), response([[1, 2]])]):
            with self.assertRaises(ValueError):
                client.get_embeddings(['a', 'b'], api_key='test', batch_size=1)

    def test_throttle_respects_small_worker_limit_and_can_back_off(self):
        throttle = client._Throttle(2, 10)
        self.assertEqual(throttle.target, 2)
        with patch.object(client.time, 'monotonic', return_value=100):
            throttle.on_reject(0, 429)
        self.assertEqual(throttle.target, 1)

    def test_cosine_dimensions_and_one_row_matrices(self):
        self.assertAlmostEqual(client.cosine_similarity(np.array([1, 0]), np.array([1, 0])), 1)
        self.assertEqual(client.cosine_similarity(np.array([[1, 0]]), np.array([[1, 0]])).shape, (1, 1))
        self.assertEqual(client.cosine_similarity(np.array([[1, 0]]), np.array([1, 0])).shape, (1,))
        with self.assertRaises(ValueError):
            client.cosine_similarity(np.array([1, 0]), np.array([1]))


if __name__ == '__main__':
    unittest.main()
