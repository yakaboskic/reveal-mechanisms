"""Owner-bound downloads and retry-safe setup exchange, without local agents."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import secrets
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from reveal_backend import research_setup as setup
from reveal_backend.auth import Problem
from reveal_backend.repository import Repository
from reveal_backend.research_work import authenticate
from reveal_backend import user_inputs
import test_research_http as http_fixture


class ResearchSetupTests(unittest.TestCase):
    make_app = http_fixture.ResearchHTTPTests.make_app
    headers = http_fixture.ResearchHTTPTests.headers
    create = http_fixture.ResearchHTTPTests.create
    grant = http_fixture.ResearchHTTPTests.grant

    def setUp(self):
        http_fixture.ResearchHTTPTests.setUp(self)
        with setup._cache_lock: setup._cache.clear()
        self.launcher = b'#!/usr/bin/env python3\nprint("fixture launcher")\n'
        read = Path.read_bytes
        launcher = self.launcher
        def launcher_read(path):
            return launcher if path.name == 'reveal_local_launcher.py' else read(path)
        patched = patch.object(Path, 'read_bytes', launcher_read)
        patched.start(); self.addCleanup(patched.stop)

    def download(self, work, client='codex', owner=None):
        response = self.client.post('/v1/local-work/'+work['id']+'/setup-kit',
            headers=self.headers(owner), json={'client': client})
        self.assertEqual(response.status_code, 200, response.text[:300] if response.status_code != 200 else '')
        self.assertEqual(response.headers['content-type'], 'application/zip')
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertIn('attachment;', response.headers['content-disposition'])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            prefix = 'reveal-'+work['id']+'/'
            self.assertTrue(all(name.startswith(prefix) for name in archive.namelist()))
            files = {name[len(prefix):]: archive.read(name) for name in archive.namelist()}
        return files, json.loads(files['setup-manifest.json'])

    def legacy_download(self, work, client='codex', owner=None):
        # Fixture for tickets issued before credential-free setup v2. New
        # downloads never create tickets; the redemption endpoint remains safe.
        files, manifest = self.download(work, client, owner)
        secret = 'rvls_'+secrets.token_urlsafe(32)
        envelope = {**manifest, 'ticket': secret, 'ticket_expires_at': work['expires_at']}
        with self.repo.transaction() as tx:
            tx.put('research_setup_ticket', hashlib.sha256(secret.encode()).hexdigest(), owner or self.owner,
                {'issued_owner_user_id': owner or self.owner, 'issued_principal_kind': 'registered', 'local_work_id': work['id'],
                 'research_request_id': work['research_request_id'], 'package_sha256': work['package_sha256'],
                 'created_at': '2026-01-01T00:00:00Z', 'expires_at': work['expires_at'],
                 'revoked_at': None, 'connection': manifest})
        return files, envelope

    def redeem(self, envelope, token=None, previous=None):
        token = token or 'rvlm_'+secrets.token_urlsafe(32)
        response = self.client.post('/v1/research-setup/exchange',
            json={'ticket': envelope['ticket'], 'token_sha256': hashlib.sha256(token.encode()).hexdigest()},
            headers={'Authorization': 'Bearer '+previous} if previous else {})
        return token, response

    def test_anonymous_or_unknown_legacy_ticket_cannot_gain_authority_after_signin(self):
        work = self.create(); _, envelope = self.legacy_download(work)
        with self.repo.transaction() as tx:
            key = hashlib.sha256(envelope['ticket'].encode()).hexdigest()
            row = tx.get('research_setup_ticket', key); row['data'].pop('issued_principal_kind')
            tx.put('research_setup_ticket', key, row['owner'], row['data'])
        self.assertEqual(self.redeem(envelope)[1].status_code, 403)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('research_access'), [])

    def test_both_clients_download_exact_retained_inputs_and_original_authoring_paths(self):
        work = self.create()
        with self.repo.read_transaction() as tx:
            stored = tx.get('research_package', work['package_id'])['data']
            artifacts = {row['data']['filename']: row['data'] for row in tx.list('research_artifact')}
        for client in ('codex', 'claude_code'):
            with self.subTest(client=client):
                files, envelope = self.download(work, client)
                manifest = json.loads(files['setup-manifest.json'])
                self.assertEqual(envelope['client'], client)
                self.assertEqual(envelope['setup_version'], setup.SETUP_VERSION)
                self.assertEqual(envelope['package_sha256'], work['package_sha256'])
                self.assertEqual(envelope['return_url'], 'http://localhost:3000/local-runs/'+work['id'])
                for key in ('client', 'local_work_id', 'research_request_id', 'reference_generation_id', 'package_sha256', 'mcp_url', 'token_url', 'return_url'):
                    self.assertEqual(manifest[key], envelope[key])
                for path, record in artifacts.items():
                    self.assertEqual(files['input/'+path], user_inputs.read(record['storage']))
                for entry in stored['package']['authoring_kit']['files']:
                    source = stored['package']['source_artifacts'][entry['artifact_id']]
                    self.assertEqual(files[entry['path']], files['input/'+source['path']])
                skeleton=next(entry for entry in stored['package']['authoring']['references'] if entry['path']=='input/package-sections/authoring-skeleton.json')
                self.assertEqual(hashlib.sha256(files[skeleton['path']]).hexdigest(),skeleton['sha256'])
                listed = {item['path']: item for item in manifest['files']}
                self.assertEqual(set(listed), set(files)-{'setup-manifest.json'})
                for path, entry in listed.items():
                    self.assertEqual(entry['sha256'], hashlib.sha256(files[path]).hexdigest())
                    self.assertEqual(entry['size_bytes'], len(files[path]))
                self.assertEqual(files['start.py'], self.launcher)
                self.assertIn(b'--mcp', files['.codex/config.toml'])
                self.assertIn(b'--mcp', files['.mcp.json'])
                self.assertNotIn(b'REVEAL_MCP_TOKEN', files['.mcp.json'])
                self.assertNotIn('setup.json', files)
                self.assertNotIn('ticket', envelope)
                self.assertIn(b'setup.json', files['.gitignore'])
                self.assertIn(b'.reveal-setup/', files['.gitignore'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('job'), [])
            self.assertEqual(tx.list('research_access'), [])
            self.assertEqual(tx.list('research_setup_ticket'), [])
        for raw in setup._cache.values():
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                self.assertFalse(any(name.endswith('/setup.json') for name in archive.namelist()))

    def test_download_scope_missing_auth_foreign_owner_hosted_and_not_ready(self):
        work = self.create(); path = '/v1/local-work/'+work['id']+'/setup-kit'
        self.assertEqual(self.client.post(path, json={'client': 'codex'}).status_code, 401)
        self.assertEqual(self.client.post(path, headers=self.headers(self.other), json={'client': 'codex'}).status_code, 404)
        for update, expected in (({'job_id': 'hosted-job'}, 404), ({'state': 'preparing'}, 409), ({'state': 'closed'}, 409)):
            with self.repo.transaction() as tx:
                value = tx.get('local_work', work['id'])['data']; value.pop('job_id', None); value.update(update)
                tx.put('local_work', work['id'], self.owner, value)
            self.assertEqual(self.client.post(path, headers=self.headers(), json={'client': 'codex'}).status_code, expected)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('research_setup_ticket'), [])

    def test_same_hash_lost_ack_replay_returns_one_grant_and_never_stores_raw_secrets(self):
        work = self.create(); _, envelope = self.legacy_download(work)
        token, first = self.redeem(envelope)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers['cache-control'], 'private, no-store')
        self.assertEqual(first.json()['package_sha256'], work['package_sha256'])
        other_replica = Repository(self.repo.sqlite_path)
        with other_replica.transaction() as tx:
            replay = setup.exchange(tx, envelope['ticket'], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(first.json(), replay)
        with self.repo.transaction() as tx:
            key = hashlib.sha256(envelope['ticket'].encode()).hexdigest()
            row = tx.get('research_setup_ticket', key); row['data']['expires_at'] = '2000-01-01T00:00:00Z'
            tx.put('research_setup_ticket', key, self.owner, row['data'])
        self.assertEqual(self.redeem(envelope, token)[1].json(), first.json())
        self.assertEqual(self.redeem(envelope)[1].status_code, 409)
        with self.repo.read_transaction() as tx:
            self.assertEqual(authenticate(tx, 'Bearer '+token)['work']['id'], work['id'])
            self.assertEqual(len(tx.list('research_access')), 1)
            persisted = json.dumps(tx.list('research_setup_ticket')+tx.list('research_access'))
            self.assertNotIn(token, persisted); self.assertNotIn(envelope['ticket'], persisted)

    def test_simultaneous_same_hash_exchange_has_one_atomic_grant(self):
        work = self.create(); _, envelope = self.legacy_download(work)
        token_hash = hashlib.sha256(b'rvlm_concurrent-local-secret').hexdigest()
        def redeem(_):
            with Repository(self.repo.sqlite_path).transaction() as tx:
                return setup.exchange(tx, envelope['ticket'], token_hash)
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(redeem, range(2)))
        self.assertEqual(results[0], results[1])
        with self.repo.read_transaction() as tx: self.assertEqual(len(tx.list('research_access')), 1)

    def test_expired_revoked_closed_retired_transferred_and_changed_package_tickets_fail(self):
        for reason in ('expired', 'revoked', 'closed', 'work_expired', 'retired', 'transferred', 'package'):
            with self.subTest(reason=reason):
                work = self.create(); _, envelope = self.legacy_download(work)
                ticket_id = hashlib.sha256(envelope['ticket'].encode()).hexdigest()
                with self.repo.transaction() as tx:
                    if reason in ('expired', 'revoked'):
                        row = tx.get('research_setup_ticket', ticket_id)
                        row['data'].update({'expires_at': '2000-01-01T00:00:00Z'} if reason == 'expired' else {'revoked_at': 'now'})
                        tx.put('research_setup_ticket', ticket_id, self.owner, row['data'])
                    elif reason == 'retired':
                        row = tx.get('principal', self.owner); row['data']['retired'] = True
                        tx.put('principal', self.owner, self.owner, row['data'])
                    elif reason == 'transferred': tx.transfer(self.owner, self.other)
                    else:
                        row = tx.get('local_work', work['id'])
                        row['data'].update({'state': 'closed'} if reason == 'closed' else
                            {'expires_at': '2000-01-01T00:00:00Z'} if reason == 'work_expired' else {'package_sha256': '0'*64})
                        tx.put('local_work', work['id'], self.owner, row['data'])
                response = self.redeem(envelope)[1]
                self.assertEqual(response.status_code, 410 if reason == 'expired' else
                    401 if reason in ('revoked', 'retired', 'transferred') else 409, response.text)
                with self.repo.transaction() as tx:
                    row = tx.get('principal', self.owner); row['data']['retired'] = False
                    tx.put('principal', self.owner, self.owner, row['data'])

    def test_explicit_ticket_revocation_preserves_consumed_grants_and_other_work(self):
        work = self.create(); other = self.create()
        _, consumed = self.legacy_download(work); token, granted = self.redeem(consumed)
        self.assertEqual(granted.status_code, 200)
        _, pending = self.legacy_download(work); _, unrelated = self.legacy_download(other)
        path = '/v1/local-work/'+work['id']+'/setup-tickets'
        self.assertEqual(self.client.delete(path, headers=self.headers(self.other)).status_code, 404)
        revoked = self.client.delete(path, headers=self.headers())
        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(self.redeem(pending)[1].status_code, 401)
        self.assertEqual(self.redeem(unrelated)[1].status_code, 200)
        self.assertEqual(self.redeem(consumed, token)[1].json(), granted.json())
        self.client.delete('/v1/local-work/'+work['id']+'/grants/'+granted.json()['grant_id'], headers=self.headers())
        self.assertEqual(self.redeem(consumed, token)[1].status_code, 401)

    def test_transferring_work_back_cannot_resurrect_a_setup_ticket(self):
        work = self.create(); _, envelope = self.legacy_download(work)
        with self.repo.transaction() as tx: tx.transfer(self.owner, self.other)
        with self.repo.transaction() as tx: tx.transfer(self.other, self.owner)
        response = self.redeem(envelope)[1]
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()['code'], 'SETUP_TICKET_REVOKED')
        with self.repo.read_transaction() as tx:
            row = tx.get('research_setup_ticket', hashlib.sha256(envelope['ticket'].encode()).hexdigest())
            self.assertEqual(row['data']['revocation_reason'], 'workspace_transferred')
            self.assertEqual(tx.list('research_access'), [])

    def test_reconnect_rotates_only_supplied_grant_and_replays_after_lost_ack(self):
        work = self.create(); old = self.grant(work); others = [self.grant(work) for _ in range(4)]
        _, envelope = self.legacy_download(work)
        self.assertEqual(self.redeem(envelope)[1].status_code, 429)
        token, response = self.redeem(envelope, previous=old['token'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.redeem(envelope, token, old['token'])[1].json(), response.json())
        with self.repo.read_transaction() as tx:
            with self.assertRaises(Problem): authenticate(tx, 'Bearer '+old['token'])
            self.assertEqual(authenticate(tx, 'Bearer '+token)['work']['id'], work['id'])
            for grant in others: self.assertEqual(authenticate(tx, 'Bearer '+grant['token'])['work']['id'], work['id'])

    def test_reconnect_foreign_connection_never_consumes_or_revokes(self):
        work = self.create(); other = self.create(self.other); unrelated = self.grant(other, self.other)
        _, envelope = self.legacy_download(work)
        self.assertEqual(self.redeem(envelope, previous=unrelated['token'])[1].status_code, 403)
        with self.repo.read_transaction() as tx:
            self.assertEqual(authenticate(tx, 'Bearer '+unrelated['token'])['work']['id'], other['id'])
        self.assertEqual(self.redeem(envelope)[1].status_code, 200)

    def test_fresh_ticket_can_replace_expired_or_revoked_same_scope_connection(self):
        for reason in ('expired', 'revoked'):
            work = self.create(); old = self.grant(work)
            with self.repo.transaction() as tx:
                key = hashlib.sha256(old['token'].encode()).hexdigest(); row = tx.get('research_access', key)
                row['data'].update({'expires_at': '2000-01-01T00:00:00Z'} if reason == 'expired' else {'revoked_at': 'now'})
                tx.put('research_access', key, self.owner, row['data'])
            _, envelope = self.legacy_download(work)
            token, response = self.redeem(envelope, previous=old['token'])
            self.assertEqual(response.status_code, 200, response.text)
            with self.repo.read_transaction() as tx:
                self.assertEqual(authenticate(tx, 'Bearer '+token)['work']['id'], work['id'])

    def test_workspace_generation_does_not_write_global_or_server_files(self):
        work = self.create()
        with (patch.object(Path, 'write_bytes', side_effect=AssertionError('Unexpected filesystem write')),
                patch.object(Path, 'write_text', side_effect=AssertionError('Unexpected filesystem write'))):
            self.download(work)

    def test_generated_bundle_runs_actual_launcher_and_anonymous_mcp_for_both_clients(self):
        spec = importlib.util.spec_from_file_location('setup_integration_launcher',
            setup.ROOT/'services/backend/agent-runtime/reveal_local_launcher.py')
        launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
        class MemoryStore:
            def __init__(self): self.values = {}
            def get(self, key): return self.values.get(key)
            def put(self, key, value): self.values[key] = value
        def post(url, body, token=None, protocol=None):
            headers = {'Accept': 'application/json, text/event-stream'}
            if token: headers['Authorization'] = 'Bearer '+token
            if protocol: headers['MCP-Protocol-Version'] = protocol
            response = self.client.post(url, headers=headers, json=body)
            self.assertLess(response.status_code, 300, response.text)
            return response.json() if response.content else {}
        for client in ('codex', 'claude_code'):
            work = self.create(); files, envelope = self.download(work, client)
            folder = Path(self.temp.name)/('bundle-'+client); folder.mkdir()
            for name, raw in files.items():
                path = folder/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
            self.assertEqual(launcher.verify_bundle(folder)['package_sha256'], work['package_sha256'])
            launches = []
            def run(args, **kwargs):
                launches.append((args, kwargs)); return SimpleNamespace(returncode=0)
            store = MemoryStore()
            self.assertEqual(launcher.launch(folder, store=store, post=post, runner=run,
                which=lambda name: '/fixture/'+name), 0)
            self.assertEqual(len(launches), 1)
            args, options = launches[0]
            self.assertEqual(args[0], '/fixture/'+('claude' if client == 'claude_code' else 'codex'))
            self.assertEqual(options['cwd'], str(folder.resolve()))
            self.assertFalse((folder/'setup.json').exists())
            self.assertNotIn('REVEAL_MCP_TOKEN', options['env'])
            with self.repo.read_transaction() as tx:
                self.assertEqual(tx.list('research_access'), [])
                self.assertEqual(tx.list('job'), [])
            self.assertEqual(launcher.launch(folder, check_only=True, store=store, post=post, runner=run,
                which=lambda name: '/fixture/'+name), 0)
            self.assertEqual(len(launches), 1)

    def test_hash_validation_and_token_collision_do_not_consume_ticket(self):
        work = self.create(); _, envelope = self.legacy_download(work)
        for digest in ('bad', 'A'*64, None, 123):
            response = self.client.post('/v1/research-setup/exchange', json={'ticket': envelope['ticket'], 'token_sha256': digest})
            self.assertEqual(response.status_code, 422)
        existing = self.grant(work)
        self.assertEqual(self.redeem(envelope, existing['token'])[1].status_code, 409)
        self.assertEqual(self.redeem(envelope)[1].status_code, 200)
        large = self.client.post('/v1/research-setup/exchange', content=b'x'*4097)
        self.assertEqual(large.status_code, 413)

    def test_corrupt_artifact_or_unsafe_path_fails_before_ticket_issuance(self):
        for unsafe in (False, True):
            work = self.create()
            with self.repo.transaction() as tx:
                package = tx.get('research_package', work['package_id'])['data']
                item = package['artifacts'][0]; row = tx.get('research_artifact', item['id'])
                if unsafe:
                    item['filename'] = row['data']['filename'] = '../escape'
                    tx.put('research_artifact', item['id'], self.owner, row['data'])
                    tx.put('research_package', work['package_id'], self.owner, package)
                else:
                    row['data']['storage']['sha256'] = 'f'*64
                    tx.put('research_artifact', item['id'], self.owner, row['data'])
            response = self.client.post('/v1/local-work/'+work['id']+'/setup-kit', headers=self.headers(), json={'client': 'codex'})
            self.assertEqual(response.status_code, 409, response.text)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('research_setup_ticket'), [])

    def test_close_during_artifact_reads_rechecks_authority_before_issuing_ticket(self):
        work = self.create()
        original = setup._archive
        def close(*args):
            result = original(*args)
            with self.repo.transaction() as tx:
                value = tx.get('local_work', work['id'])['data']; value['state'] = 'closed'
                tx.put('local_work', work['id'], self.owner, value)
            return result
        with patch.object(setup, '_archive', side_effect=close):
            response = self.client.post('/v1/local-work/'+work['id']+'/setup-kit', headers=self.headers(), json={'client': 'codex'})
        self.assertEqual(response.status_code, 409)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('research_setup_ticket'), [])

    def test_artifact_reads_are_bounded_parallel_and_cached_without_tickets(self):
        work = self.create(); lock = threading.Lock(); counts = {'active': 0, 'peak': 0}
        original = user_inputs.read
        def read(ref):
            with lock:
                counts['active'] += 1; counts['peak'] = max(counts['peak'], counts['active'])
            try:
                time.sleep(.002)
                return original(ref)
            finally:
                with lock: counts['active'] -= 1
        with patch.object(user_inputs, 'read', side_effect=read) as reads:
            first, a = self.download(work)
            call_count = reads.call_count
            second, b = self.download(work)
            self.assertEqual(reads.call_count, call_count)
        self.assertGreater(counts['peak'], 1); self.assertLessEqual(counts['peak'], 4)
        self.assertEqual(a, b)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('research_setup_ticket'), [])
        self.assertEqual({k:v for k,v in first.items() if k != 'setup.json'}, {k:v for k,v in second.items() if k != 'setup.json'})

    def test_portable_paths_and_canonical_web_urls(self):
        for path in ('../secret', '/absolute', 'C:/secret', 'a\\b', 'a//b', 'a/./b', 'CON.txt', 'a/NUL', 'a/end.'):
            with self.subTest(path=path), self.assertRaises(Problem): setup.safe_path(path)
        self.assertEqual(setup.safe_path('docs/research.md'), 'docs/research.md')
        with patch.dict('os.environ', {'REVEAL_PUBLIC_API_URL': 'https://api.example.org',
                'REVEAL_PUBLIC_WEB_URL': '', 'REVEAL_CANONICAL_URL': '', 'NEXTAUTH_URL': ''}):
            with self.assertRaises(Problem): setup.urls('work')
        with patch.dict('os.environ', {'REVEAL_PUBLIC_WEB_URL': 'https://reveal.example.org'}):
            self.assertEqual(setup.urls('work')['return_url'], 'https://reveal.example.org/local-runs/work')
