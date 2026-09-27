"""Safety boundaries for the explicit DisMech embedding command line."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'import_dismech_embeddings.py'
spec = importlib.util.spec_from_file_location('dismech_embeddings_cli', SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class EmbeddingCliTests(unittest.TestCase):
    def test_mutations_require_explicit_command_and_apply(self):
        for argv in (['migrate'], ['load'], ['verify', '--apply'], ['embed', '--apply'],
                     ['prepare'], ['embed', '--batch-size', '101'], ['embed', '--max-workers', '5'],
                     ['embed', '--max-batches', '0'], ['verify', '--max-batches', '1']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    cli.arguments(argv)
                self.assertEqual(failure.exception.code, 2)
        self.assertTrue(cli.arguments(['migrate', '--apply']).apply)
        self.assertTrue(cli.arguments(['load', '--apply']).apply)
        self.assertEqual(cli.arguments(['embed', '--max-batches', '1']).max_batches, 1)

    def test_capture_lock_prevents_concurrent_commands_and_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'capture'
            with cli.capture_lock(output):
                with self.assertRaisesRegex(RuntimeError, 'Another embedding command'):
                    with cli.capture_lock(output):
                        self.fail('A second command acquired the same capture')
            with cli.capture_lock(output) as locked:
                self.assertEqual(locked, output.resolve())

    def test_local_inference_requires_complete_explicit_options(self):
        local_args = ['--local-device', 'mps', '--local-revision', 'a' * 40,
                      '--local-cache-dir', '/tmp/cache', '--local-calibration-reference', '/tmp/references.json',
                      '--local-reference-sha256', 'b' * 64, '--legacy-generation-attestation', '/tmp/attestation.json']
        for argv in (['embed', '--local-device', 'mps'], ['verify'] + local_args,
                     ['embed'] + local_args, ['verify', '--legacy-generation-attestation', '/tmp/attestation.json']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.arguments(argv)
        self.assertEqual(cli.arguments(['embed', '--max-workers', '1'] + local_args).local_device, 'mps')

    def test_failed_local_preflight_never_enters_embed_capture(self):
        import reveal_backend.dismech_embeddings as embeddings
        import reveal_backend.local_embeddings as local
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attestation = root / 'attestation.json'
            attestation.write_text('{}')
            argv = ['embed', '--output', str(root / 'capture'), '--max-workers', '1',
                    '--local-device', 'mps', '--local-revision', 'a' * 40, '--local-cache-dir', temporary,
                    '--local-calibration-reference', str(root / 'reference.json'), '--local-reference-sha256', 'b' * 64,
                    '--legacy-generation-attestation', str(attestation)]
            with patch.object(cli, 'load_dotenv'), patch.object(local, 'prepare_local_embedder', side_effect=ValueError('drift')), \
                 patch.object(embeddings, 'embed_capture') as capture, self.assertRaisesRegex(ValueError, 'drift'):
                cli.main(argv)
            capture.assert_not_called()

    def test_verify_never_migrates_loads_or_embeds(self):
        import reveal_backend
        import reveal_backend.mysql_database as database

        module = types.ModuleType('reveal_backend.dismech_embeddings')
        module.verify_capture = Mock(return_value={'verified': True})
        module.migrate = Mock()
        module.load_capture = Mock()
        module.embed_capture = Mock()
        connection = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'capture'
            with patch.dict(sys.modules, {'reveal_backend.dismech_embeddings': module}), \
                 patch.object(reveal_backend, 'dismech_embeddings', module, create=True), \
                 patch.object(database, 'connect', return_value=connection), \
                 patch.object(cli, 'load_dotenv'), contextlib.redirect_stdout(io.StringIO()):
                cli.main(['verify', '--output', str(output)])
            module.verify_capture.assert_called_once_with(connection, output.resolve())
        module.migrate.assert_not_called()
        module.load_capture.assert_not_called()
        module.embed_capture.assert_not_called()
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
