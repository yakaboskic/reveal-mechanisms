"""Exercise startup rollback and process ownership without touching real services."""
import importlib.util
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
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

    def test_long_operation_reports_elapsed_and_stops_after_completion(self):
        output = io.StringIO(); reported = threading.Event()
        def status():
            reported.set()
            return 'api: running, starting; worker: created'
        with redirect_stdout(output):
            with m.Progress('Starting containers', status=status, interval=.01):
                self.assertTrue(reported.wait(1))
                time.sleep(.02)
            finished = output.getvalue()
            time.sleep(.03)
        self.assertIn('elapsed — api: running, starting; worker: created', finished)
        self.assertIn('Starting containers: done (', finished)
        self.assertEqual(output.getvalue(), finished)

    def test_browser_url_uses_selected_port_and_preserves_explicit_override(self):
        with patch.dict(m.os.environ, {'REVEAL_FRONTEND_PORT': '3100', 'REVEAL_PUBLIC_WEB_URL': ''}, clear=True):
            self.assertEqual(m.environment()['REVEAL_PUBLIC_WEB_URL'], 'http://localhost:3100')
        with patch.dict(m.os.environ, {'REVEAL_FRONTEND_PORT': '3100',
                'REVEAL_PUBLIC_WEB_URL': 'https://public.example.org'}, clear=True):
            self.assertEqual(m.environment()['REVEAL_PUBLIC_WEB_URL'], 'https://public.example.org')

    def test_status_outputs_only_allowed_container_fields(self):
        rows = [{'Service': 'api', 'State': 'running', 'Health': 'starting', 'Command': 'secret-command'},
                {'Service': 'worker', 'State': 'created', 'Health': ''},
                {'Service': 'unrelated', 'State': 'secret-value'}]
        for stdout in (json.dumps(rows), '\n'.join(json.dumps(row) for row in rows)):
            result = subprocess.CompletedProcess([], 0, stdout=stdout, stderr='secret-stderr')
            with patch.object(m.subprocess, 'run', return_value=result):
                self.assertEqual(m.container_progress({}), 'api: running, starting; worker: created')

    def test_timeout_retains_redacted_partial_output_with_private_permissions(self):
        failure = subprocess.TimeoutExpired(['docker'], 20, output=b'partial secret-value', stderr=b'waiting secret-value')
        with patch.object(m.subprocess, 'run', side_effect=failure):
            with self.assertRaisesRegex(m.StartupError, 'exceeded 20s; partial output saved'):
                m.run(['docker'], {'SERVICE_API_KEY': 'secret-value'}, timeout=20)
        log = m.LOGS / 'startup-error.log'
        self.assertEqual(log.read_text(), 'partial [REDACTED]\nwaiting [REDACTED]')
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_readiness_reports_failures_without_marking_phase_complete(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(m, 'health_status', return_value=(False, 'HTTP 503; waiting for readiness')), patch.object(m.time, 'sleep'):
            with self.assertRaisesRegex(m.StartupError, 'HTTP 503'):
                m.wait_ready('http://localhost/health', .002, 'API and Aurora')
        self.assertIn('Waiting for API and Aurora: failed', output.getvalue())
        self.assertNotIn(': done', output.getvalue())

    def test_unwritable_diagnostics_preserve_command_failure(self):
        failure = subprocess.TimeoutExpired(['docker'], 20, output=b'private output')
        with patch.object(m.subprocess, 'run', side_effect=failure), patch.object(m, 'diagnostic_log', side_effect=PermissionError('no access')):
            with self.assertRaisesRegex(m.StartupError, 'docker exceeded 20s; partial output could not be saved'):
                m.run(['docker'], {}, timeout=20)

    def test_status_probe_failure_cannot_replace_main_command_diagnostics(self):
        with patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['docker'], 3)), patch.object(m, 'diagnostic_log') as log:
            with self.assertRaises(subprocess.TimeoutExpired): m.container_progress({})
            log.assert_not_called()

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
