"""Exercise startup rollback and process ownership without touching real services."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dev_stack', Path(__file__).parents[1] / 'dev_stack.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class StackLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(m, 'ROOT', self.root), patch.object(m, 'RUNTIME', self.root),
                        patch.object(m, 'LOGS', self.root / 'logs'), patch.object(m, 'STATE', self.root / 'state.json')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_closed_connections_do_not_block_restart_but_listeners_do(self):
        with m.socket.socket() as server:
            server.setsockopt(m.socket.SOL_SOCKET, m.socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            server.listen(1)
            self.assertFalse(m.port_available(port))
            with m.socket.create_connection(('127.0.0.1', port)) as client:
                connection, _ = server.accept()
                connection.close()
                self.assertEqual(client.recv(1), b'')
        self.assertTrue(m.port_available(port))

    def test_dotenv_is_data_and_process_values_win(self):
        (self.root / '.env').write_text("PASSWORD='literal $HOME $(echo bad) # value'\nEMPTY=''\nPORT=3 # comment\n")
        with patch.dict(m.os.environ, {'PORT': '4'}, clear=True):
            env = m.environment()
        self.assertEqual(env['PASSWORD'], 'literal $HOME $(echo bad) # value')
        self.assertEqual(env['EMPTY'], '')
        self.assertEqual(env['PORT'], '4')

    def test_foreign_process_state_is_rejected(self):
        m.STATE.write_text(json.dumps({'project': 'foreign', 'root': str(self.root)}))
        with self.assertRaises(m.StartupError):
            m.load_state()

    def test_reused_pid_cannot_be_killed(self):
        state = {'frontend_pid': 5555, 'frontend_started': 'old'}
        with patch.object(m.os, 'getpgid', return_value=5555), patch.object(m, 'start_signature', return_value='new'), patch.object(m.os, 'killpg') as kill:
            m.stop_frontend(state)
            kill.assert_not_called()

    def test_readiness_failure_cleans_only_services_started_here(self):
        env = {'REVEAL_EXECUTION_MODE': 'deterministic', 'REVEAL_FRONTEND_PORT': '3000',
               'REVEAL_API_PORT': '8000', 'REVEAL_API_URL': 'http://127.0.0.1:8000'}
        calls = []
        def compose(env, *args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout='api\n' if args[0] == 'ps' else '', stderr='')
        with patch.object(m, 'preflight'), patch.object(m, 'compose', side_effect=compose), patch.object(m, 'port_available', return_value=True), patch.object(m, 'wait_ready', side_effect=m.StartupError('not ready')):
            with self.assertRaises(m.StartupError):
                m.up(env)
        self.assertIn(('stop', '--timeout', '120', 'worker'), calls)
        self.assertIn(('rm', '-f', 'worker'), calls)
        self.assertFalse(m.STATE.exists())

    def test_down_is_repeatable_and_never_removes_volumes(self):
        with patch.object(m, 'compose') as compose, patch.object(m.shutil, 'which', return_value='/usr/bin/docker'):
            m.down({})
            m.down({})
        args = [c.args[1:] for c in compose.call_args_list]
        self.assertEqual(args, [('stop', '--timeout', '120', 'api', 'worker'), ('rm', '-f', 'api', 'worker')] * 2)

    def test_diagnostic_failure_does_not_mask_failure_or_skip_cleanup(self):
        env = {'REVEAL_EXECUTION_MODE': 'deterministic', 'REVEAL_FRONTEND_PORT': '3000',
               'REVEAL_API_PORT': '8000', 'REVEAL_API_URL': 'http://127.0.0.1:8000'}
        calls = []
        def compose(env, *args, **kwargs):
            calls.append(args)
            if args[0] in ('logs', 'stop'):
                raise m.StartupError('Docker unavailable')
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        with patch.object(m, 'preflight'), patch.object(m, 'compose', side_effect=compose), patch.object(m, 'port_available', return_value=True), patch.object(m, 'wait_ready', side_effect=m.StartupError('original readiness failure')):
            with self.assertRaisesRegex(m.StartupError, 'original readiness failure'):
                m.up(env)
        self.assertIn(('stop', '--timeout', '120', 'api', 'worker'), calls)
        self.assertIn(('rm', '-f', 'api', 'worker'), calls)
        self.assertFalse(m.STATE.exists())


if __name__ == '__main__':
    unittest.main()
