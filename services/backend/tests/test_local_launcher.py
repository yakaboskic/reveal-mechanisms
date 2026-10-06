"""Anonymous launch and device OAuth handoff without provider/model calls."""
import contextlib
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).parents[1] / 'agent-runtime' / 'reveal_local_launcher.py'
SPEC = importlib.util.spec_from_file_location('reveal_local_launcher', SOURCE)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class Store:
    def __init__(self): self.values = {}; self.writes = 0
    def get(self, key): return self.values.get(key)
    def put(self, key, token):
        assert key not in self.values
        self.writes += 1; self.values[key] = token
    def delete(self, key): self.values.pop(key, None)


class Transport:
    def __init__(self, scope):
        self.scope = scope; self.calls = []; self.approved = False; self.denied = False
        self.rotation = 0; self.bad_scope = False; self.revoke_fails = False
    def tokens(self):
        self.rotation += 1
        return {'access_token': 'rvlm_'+str(self.rotation)*43, 'refresh_token': 'rvlrt_'+str(self.rotation)*43,
                'expires_in': 900, 'scope': 'research:read research:write', 'token_type': 'Bearer',
                'grant_id': 'grant', 'local_work_id': 'foreign' if self.bad_scope else self.scope['local_work_id'],
                'package_sha256': self.scope['package_sha256'], 'mcp_url': self.scope['mcp_url']}
    def http(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        if url == self.scope['device_authorization_url']:
            return {'device_code': 'private-device-'+'x'*40, 'user_code': 'ABCD-EFGH', 'expires_in': 600,
                    'interval': 5, 'verification_uri': 'https://app.example/research/connect'}
        if url == self.scope['token_url']:
            if kwargs['body']['grant_type'] == 'refresh_token': return self.tokens()
            if self.denied: raise launcher.HTTPFailure(400, 'access_denied')
            if not self.approved: raise launcher.HTTPFailure(400, 'authorization_pending')
            return self.tokens()
        if url == self.scope['revocation_url']:
            if self.revoke_fails: raise launcher.SetupError('network unavailable')
            return None
        raise AssertionError('Unexpected URL')
    def post(self, url, body, token=None, protocol=None):
        self.calls.append((url, deepcopy(body), token))
        if body['method'] == 'initialize':
            return {'id': body['id'], 'result': {'protocolVersion': '2025-11-25'}}
        if body['method'] == 'notifications/initialized': return None
        if body['method'] == 'tools/list':
            tools = [{'name': 'find_claims', 'description': 'Public claims', 'inputSchema': {'type': 'object'},
                      'securitySchemes': [{'type': 'noauth'}]},
                     {'name': 'get_factor', 'description': 'Public factors', 'securitySchemes': [{'type': 'noauth'}],
                      'inputSchema': {'type': 'object', 'properties': {'reference_generation_id': {'type': 'string'}}}},
                     {'name': 'validate_submission', 'inputSchema': {'type': 'object'},
                      'securitySchemes': [{'type': 'oauth2', 'scopes': ['research:write']}]}]
            return {'id': body['id'], 'result': {'tools': tools}}
        if body['method'] == 'tools/call':
            return {'id': body['id'], 'result': launcher.tool_result({'received': body['params']['arguments']})}
        raise AssertionError('Unexpected MCP call')


class LocalLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scope = {'schema_version': 1, 'setup_version': 'reveal.local-setup/2', 'client': 'codex',
            'local_work_id': 'work-123', 'research_request_id': 'request-123', 'reference_generation_id': 'a'*64,
            'package_sha256': launcher.sha256(b'{"frozen":true}\n'), 'mcp_url': 'https://reveal.example/mcp',
            'device_authorization_url': 'https://reveal.example/oauth/device_authorization',
            'token_url': 'https://reveal.example/oauth/token', 'revocation_url': 'https://reveal.example/oauth/revoke',
            'return_url': 'https://app.example/local-runs/work-123'}
        files = {'start.py': SOURCE.read_bytes(), 'input/evidence-package.json': b'{"frozen":true}\n',
            'input/manifest.json': b'{}', 'input/sources/row.json': b'{"source":"immutable"}',
            'RESEARCH.md': b'Private research question, not an argv value.', 'AGENTS.md': b'Instructions', 'CLAUDE.md': b'Instructions',
            '.gitignore': b'setup.json\n.reveal-setup/\n', '.codex/config.toml': b'[mcp_servers.reveal_work_123]\ncommand="python3"\nargs=["start.py","--mcp"]\n',
            '.mcp.json': json.dumps({'mcpServers': {'reveal_work_123': {'type': 'stdio', 'command': 'python3', 'args': ['start.py', '--mcp']}}}).encode()}
        files['services/backend/src/reveal_backend/evidence_reader.py'] = (SOURCE.parent.parent/'src/reveal_backend/evidence_reader.py').read_bytes()
        for name, raw in files.items():
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        (self.root/'output').mkdir(); (self.root/'output/account.json').write_bytes(b'{"keep":"findings"}')
        self.manifest = {**self.scope, 'files': [{'path': name, 'sha256': launcher.sha256(raw), 'size_bytes': len(raw)} for name, raw in files.items()]}
        self.write_manifest(); self.store = Store(); self.transport = Transport(self.scope); self.children = []
        self.clock = 1000
        self.connection = launcher.Connection(self.root, self.scope, store=self.store, http=self.transport.http, clock=lambda: self.clock)
    def write_manifest(self): (self.root/'setup-manifest.json').write_text(json.dumps(self.manifest))
    def runner(self, argv, **kwargs): self.children.append((argv, kwargs)); return subprocess.CompletedProcess(argv, 0)
    def launch(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return launcher.launch(self.root, store=self.store, post=self.transport.post, http=self.transport.http,
                                   runner=self.runner, which=lambda name: '/installed/'+name, **kwargs)
    def connected(self):
        self.connection.begin(); self.clock += 6; self.transport.approved = True
        self.assertEqual(self.connection.status()['state'], 'connected')
    def assert_no_secrets(self, output=''):
        raw_files = [p.read_bytes() for p in self.root.rglob('*') if p.is_file()]
        for raw in self.store.values.values():
            for key, value in json.loads(raw).items():
                if key in ('access_token', 'refresh_token', 'device_code'):
                    self.assertNotIn(value, output)
                    self.assertTrue(all(value.encode() not in content for content in raw_files))

    def test_anonymous_both_clients_launch_without_network_keychain_or_credential_environment(self):
        for client in ('codex', 'claude'):
            with patch.object(launcher, 'credential_store', side_effect=AssertionError('must not open store')):
                with patch.dict(os.environ, {'REVEAL_MCP_TOKEN': 'old-secret', 'OPENAI_API_KEY': 'provider-login'}):
                    self.assertEqual(self.launch(client=client), 0)
            argv, options = self.children[-1]
            self.assertNotIn('REVEAL_MCP_TOKEN', options['env'])
            self.assertEqual(options['env']['OPENAI_API_KEY'], 'provider-login')
            self.assertNotIn('old-secret', ' '.join(argv)); self.assertNotIn('Private research question', ' '.join(argv))
            self.assertEqual(options['cwd'], str(self.root)); self.assertNotIn('shell', options)
            self.assertIn('connect_reveal for private research, validation or contribution', argv[-1])
            for prohibited in ('--full-auto', '--dangerously-skip-permissions', '--model', '--sandbox', '--ask-for-approval'):
                self.assertNotIn(prohibited, argv)
        self.assertEqual(self.store.writes, 0); self.assertEqual(self.transport.calls, [])
        self.assertEqual((self.root/'output/account.json').read_bytes(), b'{"keep":"findings"}')

    def test_check_only_needs_no_agent_cli_or_authentication(self):
        with patch.object(launcher, 'credential_store', side_effect=AssertionError('store')):
            self.assertEqual(launcher.launch(self.root, check_only=True, post=self.transport.post, which=lambda _: None), 0)
        self.assertEqual(self.children, []); self.assertTrue(all(len(call) == 3 and call[2] is None for call in self.transport.calls))

    def test_offline_keeps_local_reader_and_disables_remote_calls(self):
        self.launch(client='codex', offline=True)
        self.assertNotIn('mcp_servers.reveal_work_123.enabled=false', self.children[-1][0])
        self.assertEqual(self.children[-1][1]['env']['REVEAL_LOCAL_OFFLINE'], '1')
        self.assertIn(launcher.OFFLINE_INSTRUCTIONS, self.children[-1][0][-1])
        self.assertNotIn('connect_reveal', self.children[-1][0][-1])
        self.launch(client='claude', offline=True)
        config = json.loads((self.root/'.reveal-setup/agent-mcp.json').read_text())
        self.assertEqual(config['mcpServers']['reveal_work_123']['env']['REVEAL_LOCAL_OFFLINE'], '1')
        self.assertIn(launcher.OFFLINE_INSTRUCTIONS, self.children[-1][0][-1])
        self.assertNotIn('connect_reveal', self.children[-1][0][-1])
        self.assertNotIn('--strict-mcp-config', self.children[-1][0]); self.assertEqual(self.transport.calls, [])

    def test_device_flow_exposes_only_link_code_then_stores_tokens_securely(self):
        pending = self.connection.begin(); again = self.connection.begin()
        self.assertEqual(again, pending); self.assertEqual(len(self.transport.calls), 1)
        self.assertIn('user_code=ABCD-EFGH', pending['verification_uri_complete'])
        self.assertNotIn('device_code', pending); self.assert_no_secrets(json.dumps(pending))
        self.assertEqual(self.connection.status()['state'], 'authorization_pending'); self.assertEqual(len(self.transport.calls), 1)
        self.clock += 6
        self.assertEqual(self.connection.status()['state'], 'authorization_pending')
        self.clock += 6; self.transport.approved = True
        self.assertEqual(self.connection.status()['state'], 'connected')
        self.assertTrue(self.connection.token().startswith('rvlm_')); self.assert_no_secrets()
        self.assertEqual(len(self.store.values), 1)

    def test_refresh_rotates_secure_secret_and_disconnect_preserves_files(self):
        self.connected(); first = self.connection.token(); self.clock += 880
        second = self.connection.token(); self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.values), 1); self.assert_no_secrets()
        result = self.connection.disconnect()
        self.assertTrue(result['revoked']); self.assertIsNone(self.connection.token()); self.assertEqual(self.store.values, {})
        self.assertTrue((self.root/'output/account.json').exists())

    def test_failed_remote_revoke_still_disables_local_use_and_can_retry(self):
        self.connected(); self.transport.revoke_fails = True
        self.assertFalse(self.connection.disconnect()['revoked']); self.assertIsNone(self.connection.token())
        self.transport.revoke_fails = False
        self.assertTrue(self.connection.disconnect()['revoked']); self.assertEqual(self.store.values, {})

    def test_repeated_connect_reuses_connection_and_never_orphans_prior_authority(self):
        self.connected(); previous = self.connection.token(); before = len(self.transport.calls)
        self.assertEqual(self.connection.begin()['state'], 'connected')
        self.assertEqual(len(self.transport.calls), before)
        self.transport.revoke_fails = True; self.connection.disconnect()
        with self.assertRaisesRegex(launcher.SetupError, 'network'):
            self.connection.begin()
        self.assertEqual(sum(call[0] == self.scope['device_authorization_url'] for call in self.transport.calls), 1)
        self.transport.revoke_fails = False
        self.assertEqual(self.connection.begin()['state'], 'authorization_pending')
        self.assertTrue(all(previous not in raw for raw in self.store.values.values()))
        self.assertEqual(self.transport.calls[-2][0], self.scope['revocation_url'])

    def test_crash_after_saving_tokens_recovers_without_polling_consumed_device(self):
        self.connection.begin(); self.clock += 6; self.transport.approved = True
        original = self.connection.discard
        with patch.object(self.connection, 'discard', side_effect=launcher.SetupError('simulated crash')):
            with self.assertRaisesRegex(launcher.SetupError, 'crash'): self.connection.status()
        before = len(self.transport.calls)
        self.assertEqual(self.connection.status()['state'], 'connected')
        self.assertEqual(len(self.transport.calls), before)

    def test_sdk_metadata_auth_schemes_allow_public_bridge_reads(self):
        bridge = launcher.Bridge(self.root, self.scope, connection=Mock(), post=self.transport.post)
        bridge.catalog = {'find_claims': {'name': 'find_claims', '_meta': {'securitySchemes': [{'type': 'noauth'}]}, 'inputSchema': {}}}
        bridge.connection.token.side_effect = AssertionError('Must remain anonymous')
        self.assertFalse(bridge.call('find_claims', {})['isError'])

    def test_denied_and_expired_device_requests_do_not_create_credentials(self):
        self.connection.begin(); self.clock += 6; self.transport.denied = True
        self.assertEqual(self.connection.status()['state'], 'access_denied'); self.assertIsNone(self.connection.token())
        self.connection.begin(); self.clock += 601
        self.assertEqual(self.connection.status()['state'], 'expired'); self.assertEqual(self.store.values, {})

    def test_wrong_work_tokens_are_rejected_before_persistence(self):
        self.connection.begin(); self.clock += 6; self.transport.approved = True; self.transport.bad_scope = True
        with self.assertRaisesRegex(launcher.SetupError, 'mismatched'): self.connection.status()
        self.assertIsNone(self.connection.token()); self.assert_no_secrets()

    def test_public_bridge_fills_pinned_generation_and_never_opens_credentials(self):
        connection = Mock(); connection.token.side_effect = AssertionError('Public query touched credentials')
        bridge = launcher.Bridge(self.root, self.scope, connection=connection, post=self.transport.post)
        tools = bridge.tools()['tools']; self.assertTrue(any(t['name'] == 'connect_reveal' for t in tools))
        result = bridge.call('get_factor', {'arguments': {'factor_id': 'f1'}})
        self.assertEqual(result['structuredContent']['received']['reference_generation_id'], 'a'*64)
        self.assertEqual(self.transport.calls[-1][2], None)

    def test_graph_queries_do_not_inject_reference_generation_and_private_reads_use_oauth(self):
        connection = Mock(); connection.token.return_value = 'rvlm_fixture'
        bridge = launcher.Bridge(self.root, self.scope, connection=connection, post=self.transport.post)
        bridge.protocol = '2025-11-25'
        bridge.catalog = {'sparql_query': {'name': 'sparql_query',
            '_meta': {'securitySchemes': [{'type': 'noauth'}, {'type': 'oauth2'}]},
            'inputSchema': {'type': 'object', 'properties': {'graph': {'type': 'string'}, 'query': {'type': 'string'}}}}}
        arguments = {'graph': 'prokn', 'query': 'SELECT ?s WHERE { GRAPH <urn:fixture> { ?s ?p ?o } }'}
        result = bridge.call('sparql_query', arguments)
        self.assertEqual(result['structuredContent']['received'], arguments)
        self.assertIsNone(self.transport.calls[-1][2]); connection.token.assert_not_called()
        private = {**arguments, 'research_request_id': self.scope['research_request_id'], 'idempotency_key': 'query'}
        self.assertEqual(bridge.call('sparql_query', private)['structuredContent']['received'], private)
        connection.token.assert_called_once()
        self.assertEqual(self.transport.calls[-1][2], 'rvlm_fixture')

    def test_protected_bridge_requires_explicit_signin_and_transfers_no_credentials_to_agent(self):
        bridge = launcher.Bridge(self.root, self.scope, connection=self.connection, post=self.transport.post)
        with self.assertRaisesRegex(launcher.SetupError, 'connect_reveal'): bridge.call('validate_submission', {})
        self.connected()
        result = bridge.call('validate_submission', {'local_work_id': 'work-123'})
        self.assert_no_secrets(json.dumps(result)); self.assertTrue(self.transport.calls[-1][2].startswith('rvlm_'))

    def test_upload_helper_restricts_paths_and_checks_server_destinations(self):
        self.connected(); bridge = launcher.Bridge(self.root, self.scope, connection=self.connection, post=self.transport.post)
        for path in ('../outside', '/tmp/outside', 'input/evidence-package.json', 'output/../start.py'):
            with self.subTest(path=path), self.assertRaises(launcher.SetupError):
                bridge.upload({'purpose': 'account', 'paths': [path], 'idempotency_key': 'upload'})
        bridge.unpack = Mock(return_value={'uploads': [{'upload_id': 'upload', 'url': 'https://other.example/steal'}]})
        with self.assertRaisesRegex(launcher.SetupError, 'destination'):
            bridge.upload({'purpose': 'account', 'paths': ['output/account.json'], 'idempotency_key': 'upload'})

    def test_stdio_protocol_keeps_logs_and_secrets_out_of_stdout(self):
        bridge = launcher.Bridge(self.root, self.scope, connection=self.connection, post=self.transport.post)
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'validate_submission', 'arguments': {}}}]
        source = io.StringIO('\n'.join(map(json.dumps, messages))+'\n'); output = io.StringIO()
        launcher.serve(self.root, self.scope, source=source, destination=output, bridge=bridge)
        responses = list(map(json.loads, output.getvalue().splitlines()))
        self.assertEqual([r['id'] for r in responses], [1, 2, 3]); self.assertTrue(responses[-1]['result']['isError'])
        self.assertIn('connect_reveal for private research, validation or contribution', responses[0]['result']['instructions'])
        self.assertIn('connect_reveal', responses[-1]['result']['content'][0]['text']); self.assert_no_secrets(output.getvalue())

    def test_offline_stdio_never_contacts_network(self):
        raw = b'{"items":[{"loading":0.123456789012345678901234567890}]}'
        package = {'source_artifacts': {'fixture': {'path': 'sources/row.json',
            'sha256': launcher.sha256(raw), 'size_bytes': len(raw), 'format': 'json'}}}
        package_raw = json.dumps(package).encode()
        self.scope['package_sha256'] = launcher.sha256(package_raw)
        (self.root/'input/evidence-package.json').write_bytes(package_raw)
        (self.root/'input/sources/row.json').write_bytes(raw)
        (self.root/'input/manifest.json').write_text(json.dumps({'package_sha256': launcher.sha256(package_raw)}))
        bridge = launcher.Bridge(self.root, self.scope, connection=self.connection,
            post=Mock(side_effect=AssertionError('network')), http=Mock(side_effect=AssertionError('network')))
        messages = [{'jsonrpc': '2.0', 'id': 0, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'read_evidence',
                'arguments': {'artifact_id': 'fixture', 'sha256': launcher.sha256(raw), 'pointer': '/items/0/loading'}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'get_factor', 'arguments': {}}}]
        source = io.StringIO('\n'.join(map(json.dumps, messages))+'\n'); output = io.StringIO()
        with patch('socket.socket', side_effect=AssertionError('network')):
            launcher.serve(self.root, self.scope, source=source, destination=output, bridge=bridge, offline=True)
        responses = list(map(json.loads, output.getvalue().splitlines()))
        self.assertEqual(responses[0]['result']['instructions'], launcher.OFFLINE_INSTRUCTIONS)
        self.assertNotIn('connect_reveal', responses[0]['result']['instructions'])
        self.assertEqual([tool['name'] for tool in responses[1]['result']['tools']], ['read_evidence'])
        self.assertEqual(responses[2]['result']['structuredContent']['content_json'], '0.123456789012345678901234567890')
        self.assertTrue(responses[3]['result']['isError'])
        self.assertIn('read_evidence for downloaded evidence', responses[3]['result']['content'][0]['text'])
        bridge.post.assert_not_called(); bridge.http.assert_not_called()

    def test_explicit_offline_flag_is_respected_by_stdio_entrypoint(self):
        with patch.object(launcher, 'verify_bundle', return_value=self.scope), patch.object(launcher, 'serve', return_value=0) as serve:
            self.assertEqual(launcher.main(['--mcp', '--offline']), 0)
        self.assertTrue(serve.call_args.kwargs['offline'])

    def test_offline_flag_and_environment_reject_authentication_and_downloads_before_access(self):
        forbidden = AssertionError('Offline mode must not access network, credentials or browser')
        cases = [(['--login'], 'sign-in and logout'), (['--logout'], 'sign-in and logout'),
                 (['--download-public-capture', 'capture'], 'Downloading evidence'),
                 (['--materialize-evidence', 'output/selection.json'], 'Downloading evidence')]
        with patch.object(launcher, '__file__', str(self.root/'start.py')), \
             patch('socket.socket', side_effect=forbidden), \
             patch('socket.create_connection', side_effect=forbidden), \
             patch('socket.getaddrinfo', side_effect=forbidden), \
             patch.object(launcher, 'credential_store', side_effect=forbidden) as store, \
             patch.object(launcher.webbrowser, 'open', side_effect=forbidden) as browser, \
             patch.object(launcher, 'request_http', side_effect=forbidden) as http:
            for inherited in (False, True):
                for arguments, expected in cases:
                    with self.subTest(inherited=inherited, arguments=arguments), \
                         patch.dict(os.environ, {'REVEAL_LOCAL_OFFLINE': '1' if inherited else '0'}), \
                         contextlib.redirect_stderr(io.StringIO()) as errors:
                        flags = arguments if inherited else ['--offline', *arguments]
                        self.assertEqual(launcher.main(flags), 1)
                        self.assertIn(expected, errors.getvalue())
                        self.assertIn('REVEAL_LOCAL_OFFLINE', errors.getvalue())
            store.assert_not_called(); browser.assert_not_called(); http.assert_not_called()

    def test_inherited_offline_mode_applies_to_checks_client_launch_and_stdio(self):
        forbidden = AssertionError('Inherited offline mode must not access network or credentials')
        with patch.object(launcher, '__file__', str(self.root/'start.py')), \
             patch.dict(os.environ, {'REVEAL_LOCAL_OFFLINE': '1'}), \
             patch('socket.socket', side_effect=forbidden), \
             patch('socket.create_connection', side_effect=forbidden), \
             patch('socket.getaddrinfo', side_effect=forbidden), \
             patch.object(launcher, 'credential_store', side_effect=forbidden), \
             patch.object(launcher.webbrowser, 'open', side_effect=forbidden), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--check-only']), 0)
            with patch.object(launcher, 'launch', return_value=0) as launch:
                self.assertEqual(launcher.main(['codex']), 0)
                self.assertTrue(launch.call_args.kwargs['offline'])
            with patch.object(launcher, 'serve', return_value=0) as serve:
                self.assertEqual(launcher.main(['--mcp']), 0)
                self.assertTrue(serve.call_args.kwargs['offline'])

    def test_direct_offline_launch_rejects_connection_actions(self):
        for action in ('login', 'logout'):
            with self.subTest(action=action), self.assertRaisesRegex(launcher.SetupError, 'Offline mode'):
                self.launch(offline=True, **{action: True})
        self.assertEqual(self.transport.calls, []); self.assertEqual(self.store.writes, 0)

    def test_integrity_and_unsafe_manifest_paths_fail_before_launch(self):
        (self.root/'input/sources/row.json').write_text('changed')
        with self.assertRaisesRegex(launcher.SetupError, 'integrity'): self.launch()
        self.assertEqual(self.children, []); self.assertEqual(self.transport.calls, [])
        for path in ('../outside', '/tmp/outside', 'input/./row.json', 'input\\row.json'):
            with self.subTest(path=path), self.assertRaises(launcher.SetupError): launcher.safe_path(self.root, path)

    def test_symlinks_and_cross_scope_store_keys_are_rejected(self):
        target = self.root/'target'; target.write_text('never touch')
        (self.root/'.reveal-setup').symlink_to(target)
        with self.assertRaises(launcher.SetupError): self.connection.begin()
        (self.root/'.reveal-setup').unlink()
        with launcher.locked_state(self.root):
            launcher.write_state(self.root, {'scope': self.scope, 'tokens': 'different-work-key'})
        with self.assertRaisesRegex(launcher.SetupError, 'scope'): self.connection.token()
        self.assertEqual(target.read_text(), 'never touch')

    def test_credentials_non_tls_and_cross_origin_oauth_are_rejected(self):
        for url in ('https://user:secret@host/mcp', 'http://remote.example/mcp', 'https://host/mcp?token=secret'):
            with self.subTest(url=url), self.assertRaises(launcher.SetupError): launcher.checked_url(url)
        with self.assertRaisesRegex(launcher.SetupError, 'same Reveal origin'):
            launcher.scope_metadata({**self.scope, 'token_url': 'https://foreign.example/oauth/token'})

    def test_linux_secrets_use_stdin_and_never_arguments(self):
        with patch.object(launcher.shutil, 'which', return_value='/usr/bin/secret-tool'):
            store = launcher.LinuxSecretStore()
        with patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
            store.put('one-entry', 'secret-in-stdin')
        self.assertEqual(run.call_args.kwargs['input'], b'secret-in-stdin')
        self.assertNotIn('secret-in-stdin', ' '.join(run.call_args.args[0]))

    def test_real_http_redirect_does_not_forward_secret_or_print_response_body(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                hits.append(self.path)
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', '/stolen'); self.end_headers()
                else:
                    self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"sensitive-response"}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            base = 'http://127.0.0.1:'+str(server.server_port)
            with self.assertRaisesRegex(launcher.SetupError, 'redirected'): launcher.post_json(base+'/redirect', {}, 'private-bearer')
            with self.assertRaises(launcher.HTTPFailure) as error: launcher.post_json(base+'/error', {}, 'private-bearer')
            self.assertNotIn('sensitive', str(error.exception)); self.assertEqual(hits, ['/redirect', '/error'])
        finally: server.shutdown(); server.server_close(); thread.join()


class LauncherTLSTests(unittest.TestCase):
    def context(self, roots=0):
        # A real TLS client context retains its required certificate and hostname
        # checks; only the trust-store I/O is replaced by this isolated fixture.
        class Context(launcher.ssl.SSLContext):
            def __new__(cls): return super().__new__(cls, launcher.ssl.PROTOCOL_TLS_CLIENT)
            def __init__(self): self.roots = roots; self.loaded = []
            def cert_store_stats(self): return {'x509_ca': self.roots}
            def load_verify_locations(self, *, cafile):
                self.loaded.append(cafile); self.roots = 1
        return Context()

    def test_existing_default_trust_is_not_replaced(self):
        context = self.context(roots=2)
        with patch.object(launcher.ssl, 'create_default_context', return_value=context), \
             patch.object(launcher.sys, 'platform', 'darwin'), patch.dict(os.environ, {}, clear=True):
            self.assertIs(launcher.https_context(), context)
        self.assertEqual(context.loaded, [])
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, launcher.ssl.CERT_REQUIRED)

    def test_empty_macos_default_store_loads_system_bundle_and_keeps_verification(self):
        context = self.context()
        with patch.object(launcher.ssl, 'create_default_context', return_value=context), \
             patch.object(launcher.sys, 'platform', 'darwin'), patch.dict(os.environ, {}, clear=True):
            self.assertIs(launcher.https_context(), context)
        self.assertEqual(context.loaded, ['/etc/ssl/cert.pem'])
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, launcher.ssl.CERT_REQUIRED)

    def test_explicit_certificate_configuration_is_never_augmented(self):
        for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR'):
            for value in ('/user/selected/trust', ''):
                context = self.context()
                with self.subTest(key=key, value=value), \
                     patch.object(launcher.ssl, 'create_default_context', return_value=context), \
                     patch.object(launcher.sys, 'platform', 'darwin'), patch.dict(os.environ, {key: value}, clear=True):
                    self.assertIs(launcher.https_context(), context)
                self.assertEqual(context.loaded, [])
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, launcher.ssl.CERT_REQUIRED)

    def test_non_macos_trust_is_unchanged_including_lazy_certificate_directories(self):
        context = self.context()
        with patch.object(launcher.ssl, 'create_default_context', return_value=context), \
             patch.object(launcher.sys, 'platform', 'linux'), patch.dict(os.environ, {}, clear=True):
            self.assertIs(launcher.https_context(), context)
        self.assertEqual(context.loaded, [])

    def test_missing_or_empty_system_certificates_fail_before_network(self):
        for failure in (FileNotFoundError('private system detail'), None):
            context = self.context()
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(launcher.ssl, 'create_default_context', return_value=context), \
                 patch.object(launcher.sys, 'platform', 'darwin'), patch.dict(os.environ, {}, clear=True), \
                 patch.object(context, 'load_verify_locations', side_effect=failure), \
                 patch.object(launcher, 'build_opener') as opener:
                with self.assertRaisesRegex(launcher.SetupError, 'SSL_CERT_FILE') as error:
                    launcher.request_http('https://reveal.example/mcp')
                self.assertNotIn('private system detail', str(error.exception))
                opener.assert_not_called()
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, launcher.ssl.CERT_REQUIRED)

    def test_certificate_verification_failure_is_actionable_without_exposing_details(self):
        failure = launcher.ssl.SSLCertVerificationError('private TLS details')
        for wrapped in (failure, launcher.URLError(failure)):
            context = self.context(roots=1)
            with self.subTest(wrapped=type(wrapped).__name__), \
                 patch.object(launcher, 'https_context', return_value=context), \
                 patch.object(launcher, 'build_opener') as opener:
                opener.return_value.open.side_effect = wrapped
                with self.assertRaisesRegex(launcher.SetupError, 'Certificate verification remains required') as error:
                    launcher.request_http('https://reveal.example/mcp', token='private-bearer')
                self.assertIn('SSL_CERT_FILE', str(error.exception))
                self.assertNotIn('private', str(error.exception))
                handlers = opener.call_args.args
                self.assertTrue(any(isinstance(handler, launcher.NoRedirect) for handler in handlers))
                tls = next(handler for handler in handlers if isinstance(handler, launcher.HTTPSHandler))
                self.assertIs(tls._context, context)


@unittest.skipUnless(os.environ.get('REVEAL_TEST_NATIVE_KEYCHAIN') == '1' and launcher.sys.platform == 'darwin',
                     'Opt-in native Keychain smoke; ordinary tests never access real credentials')
class NativeKeychainSmokeTests(unittest.TestCase):
    def test_new_entry_round_trip_and_delete(self):
        store = launcher.MacKeychain()
        key = 'launcher-smoke-' + launcher.secrets.token_hex(24)
        token = 'rvlm_' + launcher.secrets.token_urlsafe(32)
        self.assertTrue(store.get(key) is None, 'New random test entry must not exist')
        created = False
        try:
            store.put(key, token); created = True
            self.assertTrue(store.get(key) == token, 'Native Keychain round trip failed')
        finally:
            if created:
                store.delete(key)
        self.assertTrue(store.get(key) is None, 'Native Keychain test entry was not deleted')


if __name__ == '__main__':
    unittest.main()
