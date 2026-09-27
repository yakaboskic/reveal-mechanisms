"""Offline local-runner integrity tests; no Torch, network, GPU, or database."""
from copy import deepcopy
import json
from unittest.mock import Mock, patch

import numpy as np

from reveal_backend import dismech_embeddings as embeddings
from reveal_backend import local_embeddings as local
from reveal_backend.dismech_import import canonical, digest
from test_dismech_embeddings import CaptureFixture, embed, target_run


class LocalRunnerTests(CaptureFixture):
    def setUp(self):
        super().setUp()
        self.target = target_run(model='public/model', provider='huggingface')
        self.complete()
        manifest, _, rows = embeddings.read_local_vectors(self.output)
        self.attestation = {'execution_backend': 'remote_embedding_service',
                            'model': 'public/model', 'service_url': self.target['config']['service_url']}
        self.bundle = {'run_id': manifest['run_id'],
            'capture_manifest_sha256': digest((self.output / 'manifest.json').read_bytes()),
            'target_eaggl_run_id': self.target['run_id'], 'dimensions': 2,
            'origin': {'kind': 'retrospective_remote_attestation', 'attestation': self.attestation},
            'references': [{'input_sha256': sha, 'input_text': text,
                            'vector': np.frombuffer(blob, dtype='<f4').tolist(), 'vector_sha256': checksum}
                           for sha, text, blob, checksum in rows]}
        self.reference = self.root / 'fixed-reference.json'

    def write_reference(self, bundle=None):
        raw = (canonical(bundle or self.bundle) + '\n').encode()
        self.reference.write_bytes(raw)
        return digest(raw)

    def prepare_local(self, checksum=None):
        return local.prepare_local_embedder(self.output, reference_path=self.reference,
            reference_sha256=checksum or self.write_reference(), attestation=self.attestation,
            revision='a' * 40, cache_dir=self.root, device='mps')

    def test_valid_fixed_calibration_records_metadata_without_writing_capture(self):
        before = (self.output / 'embeddings.sqlite3').read_bytes()
        model = Mock()
        model.encode.side_effect = embed
        with patch.object(local, '_load_model', return_value=(model, {'backend': 'local_sentence_transformers'})):
            runner = self.prepare_local()
        self.assertEqual(runner.generation_metadata['calibration']['dismech']['count'], 2)
        self.assertGreaterEqual(runner.generation_metadata['calibration']['dismech']['minimum_observed_cosine'], .999999)
        self.assertEqual((self.output / 'embeddings.sqlite3').read_bytes(), before)
        self.assertEqual(model.encode.call_args.kwargs['normalize_embeddings'], False)
        self.assertEqual(model.encode.call_args.kwargs['precision'], 'float32')

    def test_changed_reference_identity_or_vectors_fail_before_model_load(self):
        changes = [lambda b: b.update(run_id='wrong'),
                   lambda b: b.update(capture_manifest_sha256='wrong'),
                   lambda b: b.update(target_eaggl_run_id='wrong'),
                   lambda b: b.update(dimensions=3),
                   lambda b: b.update(references=[]),
                   lambda b: b['references'][0].update(input_text='changed'),
                   lambda b: b['references'][0].update(vector=[9., 9.]),
                   lambda b: b['references'][0].update(vector_sha256='wrong'),
                   lambda b: b['origin'].update(kind='local'),
                   lambda b: b['references'].append(b['references'][0])]
        for change in changes:
            with self.subTest(change=change), patch.object(local, '_load_model') as loader:
                bundle = deepcopy(self.bundle)
                change(bundle)
                checksum = self.write_reference(bundle)
                with self.assertRaises(ValueError):
                    self.prepare_local(checksum)
                loader.assert_not_called()

    def test_fixed_digest_rejects_self_consistent_reference_replacement(self):
        checksum = self.write_reference()
        replacement = deepcopy(self.bundle)
        replacement['references'][0]['vector'] = [9., 9.]
        replacement['references'][0]['vector_sha256'] = digest(np.asarray([9., 9.], dtype='<f4').tobytes())
        self.write_reference(replacement)
        with patch.object(local, '_load_model') as loader, self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.prepare_local(checksum)
        loader.assert_not_called()

    def test_rehashed_reference_still_must_match_source_inventory_and_checkpoint(self):
        for mode in ('text', 'vector'):
            replacement = deepcopy(self.bundle)
            if mode == 'text':
                replacement['references'][0].update(input_text='absent', input_sha256=digest('absent'))
            else:
                replacement['references'][0].update(vector=[9., 9.],
                    vector_sha256=digest(np.asarray([9., 9.], dtype='<f4').tobytes()))
            checksum = self.write_reference(replacement)
            with self.subTest(mode=mode), patch.object(local, '_load_model') as loader, self.assertRaises(ValueError):
                self.prepare_local(checksum)
            loader.assert_not_called()

    def test_drift_invalid_shapes_zero_nonfinite_fail_without_capture_changes(self):
        before = (self.output / 'embeddings.sqlite3').read_bytes()
        for result in ([[1., 1.], [1., 1.]], [[0., 0.], [0., 0.]],
                       [[float('nan'), 1.], [1., 1.]], [[1., 2., 3.], [1., 2., 3.]], [[1., 2.]]):
            model = Mock()
            model.encode.return_value = result
            with self.subTest(result=result), patch.object(local, '_load_model', return_value=(model, {})):
                with self.assertRaises(ValueError):
                    self.prepare_local()
            self.assertEqual((self.output / 'embeddings.sqlite3').read_bytes(), before)

    def test_request_space_and_worker_mismatch_rejected_before_inference(self):
        model = Mock()
        runner = local.LocalEmbedder(model, self.target, {})
        arguments = {'model': 'public/model', 'provider': 'huggingface',
                     'service_url': self.target['config']['service_url']}
        for override in ({'model': 'other'}, {'provider': 'other'}, {'service_url': 'other'}, {'max_workers': 2}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                runner(['exact unchanged text'], **(arguments | override))
        model.encode.assert_not_called()

    def test_context_drift_rejected_even_when_eaggl_labels_match(self):
        before = (self.output / 'embeddings.sqlite3').read_bytes()
        model = Mock()
        def infer(texts, **kwargs):
            values = embed(texts)
            if texts == [reference['input_text'] for reference in self.bundle['references']]:
                values[0] = [-1., -2.]
            return values
        model.encode.side_effect = infer
        with patch.object(local, '_load_model', return_value=(model, {})), \
             self.assertRaisesRegex(ValueError, 'frozen remote DisMech'):
            self.prepare_local()
        self.assertEqual(model.encode.call_count, 2)
        self.assertEqual((self.output / 'embeddings.sqlite3').read_bytes(), before)

    def test_local_snapshot_requires_explicit_commit_and_existing_cache_without_imports(self):
        for revision in ('main', 'a' * 40):
            with self.assertRaises(ValueError):
                local._load_model('public/model', revision, self.root, 'mps')
