"""Public OAuth clients never supply identity or ownership; consent does."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt

from reveal_backend import research_oauth as oauth
from reveal_backend.auth import Problem
from reveal_backend.repository import Repository, uid
from reveal_backend.research_work import authenticate, authorize_commit, deadline, issue_grant, ResearchWorkService

BASE = 'http://127.0.0.1:18000'
RESOURCE = BASE+'/mcp'
CALLBACK = 'http://127.0.0.1:4444/callback?existing=1'


class ResearchOAuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name)/'app.sqlite')); self.repo.migrate()
        env = patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's'*40, 'REVEAL_PUBLIC_API_URL': BASE,
            'REVEAL_PUBLIC_WEB_URL': 'http://localhost:3000', 'REVEAL_NOTIFICATION_REDIS_URL': '',
            'REVEAL_NOTIFICATION_REDIS_REST_URL': '', 'UPSTASH_REDIS_REST_URL': '', 'UPSTASH_REDIS_REST_TOKEN': ''})
        env.start(); self.addCleanup(env.stop)
        with self.repo.transaction() as tx:
            for owner, kind in (('alice', 'registered'), ('bob', 'registered'), ('anon', 'anonymous')):
                tx.put('principal', owner, owner, {'retired': False, 'me': {'user_id': owner,
                    'principal_kind': kind, 'workspace_expires_at': None}})
        app = FastAPI(); oauth.register(app, lambda: self.repo)
        self.client = TestClient(app, base_url=BASE)
        self.work = self.make_work('alice'); self.other_work = self.make_work('bob'); self.anon_work = self.make_work('anon')

    def make_work(self, owner):
        identity = uid(); request = uid()
        value = {'id': identity, 'owner_user_id': owner, 'state': 'ready', 'package_id': uid(), 'package_sha256': 'a'*64,
            'expires_at': deadline(86400), 'research_request_id': request}
        with self.repo.transaction() as tx:
            tx.put('local_work', identity, owner, value)
            tx.put('request', request, owner, {'id': request})
        return value

    def browser(self, owner='alice', kind=None):
        kind = kind or ('anonymous' if owner == 'anon' else 'registered')
        token = jwt.encode({'sub': owner, 'principal_kind': kind, 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
            'iat': int(time.time()), 'exp': int(time.time())+120, 'jti': uid()}, 's'*40, algorithm='HS256')
        return {'Authorization': 'Bearer '+token}

    def register(self, **changes):
        response = self.client.post('/oauth/register', json={'client_name': 'Test agent',
            'redirect_uris': [CALLBACK], 'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'], 'token_endpoint_auth_method': 'none', **changes})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def request_code(self, client=None, scope=None, **changes):
        client = client or self.register()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params = {'client_id': client['client_id'], 'redirect_uri': CALLBACK, 'response_type': 'code',
            'resource': RESOURCE, 'code_challenge': challenge, 'code_challenge_method': 'S256', 'state': 'client-state', **changes}
        if scope is not None: params['scope'] = scope
        response = self.client.get('/oauth/authorize', params=params, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        request = parse_qs(urlsplit(response.headers['location']).query)['request_id'][0]
        return client, verifier, request

    def approve(self, request, work=None, owner='alice', approved=True):
        return self.client.post('/v1/research-oauth/consent', headers=self.browser(owner),
            json={'request_id': request, 'local_work_id': (work or self.work)['id'], 'approve': approved})

    def code_flow(self, scope=None):
        client, verifier, request = self.request_code(scope=scope)
        consent = self.approve(request)
        self.assertEqual(consent.status_code, 200, consent.text)
        query = parse_qs(urlsplit(consent.json()['redirect_url']).query)
        self.assertEqual(query['state'], ['client-state']); self.assertEqual(query['iss'], [BASE])
        self.assertEqual(query['existing'], ['1'])
        params = {'client_id': client['client_id'], 'grant_type': 'authorization_code', 'resource': RESOURCE,
            'code': query['code'][0], 'code_verifier': verifier, 'redirect_uri': CALLBACK}
        response = self.client.post('/oauth/token', data=params)
        self.assertEqual(response.status_code, 200, response.text)
        return client, params, response.json()

    def device(self, work=None, **changes):
        response = self.client.post('/oauth/device_authorization', data={'client_id': oauth.LAUNCHER_CLIENT,
            'resource': RESOURCE, 'local_work_id': (work or self.work)['id'], **changes})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def device_params(self, device):
        return {'client_id': oauth.LAUNCHER_CLIENT, 'grant_type': oauth.DEVICE_GRANT,
            'resource': RESOURCE, 'device_code': device['device_code']}

    def test_metadata_dcr_and_resource_challenge_are_standard_public_client_contracts(self):
        meta = self.client.get('/.well-known/oauth-authorization-server').json()
        self.assertEqual(meta['issuer'], BASE)
        self.assertEqual(meta['code_challenge_methods_supported'], ['S256'])
        self.assertIn(oauth.DEVICE_GRANT, meta['grant_types_supported'])
        protected = self.client.get('/.well-known/oauth-protected-resource/mcp').json()
        self.assertEqual(protected['resource'], RESOURCE)
        self.assertIn('resource_metadata="'+BASE, oauth.challenge(write=True))
        self.assertIn('research:write', oauth.challenge(write=True))
        client = self.register()
        self.assertNotIn('client_secret', client)
        self.assertEqual(client['token_endpoint_auth_method'], 'none')
        preflight = self.client.options('/oauth/token')
        self.assertEqual(preflight.status_code, 204)
        self.assertNotIn('access-control-allow-credentials', preflight.headers)

    def test_exact_redirect_pkce_and_resource_validation_prevent_redirect_or_audience_substitution(self):
        client = self.register()
        base = {'client_id': client['client_id'], 'redirect_uri': CALLBACK, 'response_type': 'code',
            'resource': RESOURCE, 'code_challenge': 'x'*43, 'code_challenge_method': 'S256'}
        for changes in ({'redirect_uri': CALLBACK+'evil'}, {'resource': 'https://other.example/mcp'},
                {'code_challenge_method': 'plain'}, {'response_type': 'token'}, {'code_challenge': 'short'}):
            with self.subTest(changes=changes):
                response = self.client.get('/oauth/authorize', params={**base, **changes}, follow_redirects=False)
                self.assertEqual(response.status_code, 400); self.assertNotIn('location', response.headers)
        for redirect in ('http://remote.example/callback', 'https://good.example/cb#fragment',
                'https://user:pass@example.org/cb', 'javascript:alert(1)', CALLBACK+'&code=preselected'):
            response = self.client.post('/oauth/register', json={'redirect_uris': [redirect]})
            self.assertEqual(response.status_code, 400, response.text)
        duplicate = self.client.get('/oauth/authorize?client_id=a&client_id=b')
        self.assertEqual(duplicate.status_code, 400)

    def test_consent_requires_registered_browser_and_owned_open_ready_work(self):
        client, verifier, request = self.request_code()
        endpoint = '/v1/research-oauth/consent'
        self.assertEqual(self.client.get(endpoint, params={'request_id': request}).status_code, 401)
        anon = self.client.get(endpoint, params={'request_id': request}, headers=self.browser('anon'))
        self.assertEqual(anon.status_code, 403); self.assertEqual(anon.json()['code'], 'SIGN_IN_REQUIRED')
        self.assertEqual(self.approve(request, self.anon_work, 'anon').status_code, 403)
        info = self.client.get(endpoint, params={'request_id': request}, headers=self.browser()).json()
        self.assertEqual(info['scopes'], list(oauth.SCOPES)); self.assertTrue(info['requires_registered'])
        self.assertEqual(self.approve(request, self.other_work).status_code, 404)
        for update in ({'state': 'preparing'}, {'state': 'closed'}, {'state': 'ready', 'job_id': 'hosted'}):
            with self.repo.transaction() as tx:
                work = tx.get('local_work', self.work['id'])['data']; work.update(update)
                tx.put('local_work', work['id'], 'alice', work)
            self.assertIn(self.approve(request).status_code, (404, 409))
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('research_access'), [])

    def test_authorization_code_binds_client_pkce_state_work_and_hashed_tokens(self):
        client, params, tokens = self.code_flow()
        self.assertLessEqual(tokens['expires_in'], oauth.ACCESS_TTL)
        self.assertGreater(tokens['expires_in'], oauth.ACCESS_TTL-5)
        self.assertEqual(tokens['local_work_id'], self.work['id']); self.assertEqual(tokens['resource'], RESOURCE)
        with self.repo.read_transaction() as tx:
            authority = authenticate(tx, 'Bearer '+tokens['access_token'], write=True)
            self.assertEqual(authority['owner'], 'alice')
            self.assertEqual(authority['grant']['scopes'], list(oauth.SCOPES))
            retained = json.dumps(tx.list('research_access')+tx.list('research_oauth_refresh')+tx.list('research_oauth_code'))
            for secret in (tokens['access_token'], tokens['refresh_token'], params['code']): self.assertNotIn(secret, retained)
        self.assertTrue(tokens['access_token'].startswith('rvlm_'))

    def test_oauth_authentication_reads_principal_and_family_once(self):
        from round_trips import count_round_trips
        _, _, tokens = self.code_flow()
        with count_round_trips() as budget:
            with self.repo.read_transaction() as tx:
                authority = authenticate(tx, 'Bearer '+tokens['access_token'])
                # The transport's write-scope check reuses the same rows instead of re-reading them.
                oauth.check_grant_scope(tx, authority['owner'], authority['grant'], write=True,
                                        me=authority['me'], family=authority['oauth_family'])
        self.assertEqual(budget.leases, [['read', 2]], budget)   # research_access, then principal+work+request+family
        with self.repo.transaction() as tx:
            identity = authority['grant']['oauth_family_id']; family = tx.get('research_oauth_family', identity)
            family['data']['revoked_at'] = deadline(0); tx.put('research_oauth_family', identity, family['owner'], family['data'])
        with self.repo.read_transaction() as tx, self.assertRaises(Problem) as revoked:
            authenticate(tx, 'Bearer '+tokens['access_token'])
        self.assertEqual(revoked.exception.code, 'MCP_GRANT_EXPIRED')

    def test_pkce_wrong_client_and_invalid_verifier_do_not_mint_or_consume_code(self):
        client, verifier, request = self.request_code()
        response = self.approve(request); code = parse_qs(urlsplit(response.json()['redirect_url']).query)['code'][0]
        params = {'client_id': client['client_id'], 'grant_type': 'authorization_code', 'resource': RESOURCE,
            'redirect_uri': CALLBACK, 'code': code, 'code_verifier': verifier}
        other = self.register()
        for changed in ({'client_id': other['client_id']}, {'redirect_uri': CALLBACK+'x'}, {'code_verifier': 'x'*43}):
            denied = self.client.post('/oauth/token', data={**params, **changed})
            self.assertEqual(denied.status_code, 400); self.assertEqual(denied.json()['error'], 'invalid_grant')
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('research_oauth_code', oauth.sha(code))['data']['attempts'], 1)
            self.assertEqual(tx.list('research_access'), [])
        self.assertEqual(self.client.post('/oauth/token', data=params).status_code, 200)

    def test_pkce_attempt_limit_and_expired_code_do_not_mint_credentials(self):
        client, verifier, request = self.request_code()
        callback = self.approve(request).json()['redirect_url']
        code = parse_qs(urlsplit(callback).query)['code'][0]
        params = {'client_id': client['client_id'], 'grant_type': 'authorization_code', 'resource': RESOURCE,
            'code': code, 'code_verifier': verifier, 'redirect_uri': CALLBACK}
        for _ in range(5):
            self.assertEqual(self.client.post('/oauth/token', data={**params, 'code_verifier': 'z'*43}).json()['error'], 'invalid_grant')
        self.assertEqual(self.client.post('/oauth/token', data=params).json()['error'], 'invalid_grant')
        with self.repo.transaction() as tx:
            row = tx.get('research_oauth_code', oauth.sha(code)); row['data'].update(attempts=0, expires_at='2000-01-01T00:00:00Z')
            tx.put('research_oauth_code', oauth.sha(code), 'alice', row['data'])
        self.assertEqual(self.client.post('/oauth/token', data=params).json()['error'], 'invalid_grant')
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('research_access'), [])

    def test_device_pending_slow_down_denial_expiry_and_success_with_owned_hint(self):
        device = self.device(); params = self.device_params(device)
        with patch.object(oauth.time, 'time', return_value=1000):
            pending = self.client.post('/oauth/token', data=params)
            self.assertEqual(pending.json()['error'], 'authorization_pending')
            fast = self.client.post('/oauth/token', data=params)
            self.assertEqual(fast.json()['error'], 'slow_down')
        info = self.client.get('/v1/research-oauth/consent', params={'user_code': device['user_code']}, headers=self.browser()).json()
        self.assertEqual(info['requested_local_work_id'], self.work['id'])
        self.assertEqual(self.approve(info['request_id'], self.other_work).status_code, 404)
        own_other = self.make_work('alice')
        self.assertEqual(self.approve(info['request_id'], own_other).status_code, 403)
        self.assertEqual(self.approve(info['request_id']).json()['approved'], True)
        with patch.object(oauth.time, 'time', return_value=1010):
            granted = self.client.post('/oauth/token', data=params)
        self.assertEqual(granted.status_code, 200, granted.text)
        self.assertEqual(self.client.post('/oauth/token', data=params).json()['error'], 'invalid_grant')
        with self.repo.read_transaction() as tx:
            self.assertEqual(authenticate(tx, 'Bearer '+granted.json()['access_token'])['owner'], 'alice')
            serialized = json.dumps(tx.list('research_oauth_device')+tx.list('research_oauth_user_code'))
            self.assertNotIn(device['device_code'], serialized); self.assertNotIn(device['user_code'], serialized)

    def test_denied_or_expired_device_and_wrong_client_cannot_exchange(self):
        device = self.device()
        info = self.client.get('/v1/research-oauth/consent', params={'user_code': device['user_code']}, headers=self.browser()).json()
        self.assertFalse(self.approve(info['request_id'], approved=False).json()['approved'])
        denied = self.client.post('/oauth/token', data=self.device_params(device))
        self.assertEqual(denied.json()['error'], 'access_denied')
        expired = self.device()
        with self.repo.transaction() as tx:
            row = tx.get('research_oauth_device', oauth.sha(expired['device_code']))
            row['data']['expires_at'] = '2000-01-01T00:00:00Z'
            tx.put('research_oauth_device', oauth.sha(expired['device_code']), row['owner'], row['data'])
        self.assertEqual(self.client.post('/oauth/token', data=self.device_params(expired)).json()['error'], 'expired_token')
        client = self.register(grant_types=[oauth.DEVICE_GRANT, 'refresh_token'], response_types=[])
        self.assertEqual(self.client.post('/oauth/token', data={**self.device_params(device), 'client_id': client['client_id']}).json()['error'], 'invalid_grant')

    def test_existing_identity_upgrade_allows_own_work_without_possession_based_claim(self):
        device = self.device(self.anon_work)
        self.assertEqual(self.client.get('/v1/research-oauth/consent', params={'user_code': device['user_code']},
            headers=self.browser('anon')).status_code, 403)
        # The real provider/gateway claim flow performs this promotion; OAuth
        # cannot modify the principal or infer ownership from the device code.
        with self.repo.transaction() as tx:
            row = tx.get('principal', 'anon'); row['data']['me']['principal_kind'] = 'registered'
            tx.put('principal', 'anon', 'anon', row['data'])
        info = self.client.get('/v1/research-oauth/consent', params={'user_code': device['user_code']},
            headers=self.browser('anon', 'registered')).json()
        approved = self.client.post('/v1/research-oauth/consent', headers=self.browser('anon', 'registered'),
            json={'request_id': info['request_id'], 'local_work_id': self.anon_work['id'], 'approve': True})
        self.assertEqual(approved.status_code, 200, approved.text)
        token = self.client.post('/oauth/token', data=self.device_params(device)).json()['access_token']
        with self.repo.read_transaction() as tx:
            self.assertEqual(authenticate(tx, 'Bearer '+token, write=True)['owner'], 'anon')
            self.assertEqual(tx.get('local_work', self.anon_work['id'])['owner'], 'anon')

    def test_refresh_rotation_scope_narrowing_replay_revokes_entire_family_durably(self):
        client, _, first = self.code_flow()
        params = {'client_id': client['client_id'], 'grant_type': 'refresh_token', 'resource': RESOURCE,
            'refresh_token': first['refresh_token'], 'scope': 'research:read'}
        response = self.client.post('/oauth/token', data=params)
        self.assertEqual(response.status_code, 200, response.text); second = response.json()
        self.assertNotEqual(first['refresh_token'], second['refresh_token'])
        self.assertEqual(first['grant_id'], second['grant_id'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(authenticate(tx, 'Bearer '+second['access_token'])['owner'], 'alice')
            with self.assertRaises(Problem) as denied: authenticate(tx, 'Bearer '+second['access_token'], write=True)
            self.assertEqual(denied.exception.code, 'INSUFFICIENT_SCOPE')
            self.assertEqual(len(ResearchWorkService(self.repo).view(tx, 'alice', self.work['id'])['grants']), 1)
        escalation = self.client.post('/oauth/token', data={**params, 'refresh_token': second['refresh_token'], 'scope': ' '.join(oauth.SCOPES)})
        self.assertEqual(escalation.json()['error'], 'invalid_scope')
        replay = self.client.post('/oauth/token', data=params)
        self.assertEqual(replay.json()['error'], 'invalid_grant')
        with self.repo.read_transaction() as tx:
            for access in (first['access_token'], second['access_token']):
                with self.assertRaises(Problem): authenticate(tx, 'Bearer '+access)
            family = tx.get('research_oauth_family', first['grant_id'])['data']
            self.assertEqual(family['revocation_reason'], 'refresh_token_replayed')
        self.assertEqual(self.client.post('/oauth/token', data={**params, 'refresh_token': second['refresh_token']}).json()['error'], 'invalid_grant')

    def test_simultaneous_refresh_detects_replay_and_revokes_the_winning_descendant(self):
        client, _, first = self.code_flow()
        params = {'client_id': client['client_id'], 'grant_type': 'refresh_token', 'resource': RESOURCE,
            'refresh_token': first['refresh_token']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.client.post('/oauth/token', data=params), range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 400])
        winner = next(response.json() for response in responses if response.status_code == 200)
        with self.repo.read_transaction() as tx:
            with self.assertRaises(Problem): authenticate(tx, 'Bearer '+winner['access_token'])
            self.assertEqual(tx.get('research_oauth_family', first['grant_id'])['data']['revocation_reason'], 'refresh_token_replayed')

    def test_refresh_checks_client_resource_work_lifetime_and_current_registered_identity(self):
        client, _, tokens = self.code_flow(); other = self.register()
        params = {'client_id': client['client_id'], 'grant_type': 'refresh_token', 'resource': RESOURCE,
            'refresh_token': tokens['refresh_token']}
        for changes, code in (({'client_id': other['client_id']}, 'invalid_grant'),
                ({'resource': 'https://another.example/mcp'}, 'invalid_target')):
            self.assertEqual(self.client.post('/oauth/token', data={**params, **changes}).json()['error'], code)
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('research_oauth_refresh', oauth.sha(tokens['refresh_token']))['data']['used_at'])
        with self.repo.transaction() as tx:
            value = tx.get('local_work', self.work['id'])['data']; value['expires_at'] = '2000-01-01T00:00:00Z'
            tx.put('local_work', self.work['id'], 'alice', value)
        self.assertEqual(self.client.post('/oauth/token', data=params).json()['error'], 'invalid_grant')
        with self.repo.transaction() as tx:
            value = tx.get('local_work', self.work['id'])['data']; value['expires_at'] = deadline(86400)
            tx.put('local_work', self.work['id'], 'alice', value)
            row = tx.get('principal', 'alice'); row['data']['me']['principal_kind'] = 'anonymous'
            tx.put('principal', 'alice', 'alice', row['data'])
        self.assertEqual(self.client.post('/oauth/token', data=params).json()['error'], 'invalid_grant')
        with self.repo.read_transaction() as tx:
            with self.assertRaises(Problem): authenticate(tx, 'Bearer '+tokens['access_token'])

    def test_code_replay_revocation_commits_and_browser_revoke_blocks_refresh(self):
        client, params, granted = self.code_flow()
        replay = self.client.post('/oauth/token', data=params)
        self.assertEqual(replay.json()['error'], 'invalid_grant')
        with self.repo.read_transaction() as tx:
            with self.assertRaises(Problem): authenticate(tx, 'Bearer '+granted['access_token'])
        client, _, granted = self.code_flow()
        with self.repo.transaction() as tx: oauth.revoke_oauth_grant(tx, 'alice', granted['grant_id'])
        refreshed = self.client.post('/oauth/token', data={'client_id': client['client_id'], 'grant_type': 'refresh_token',
            'resource': RESOURCE, 'refresh_token': granted['refresh_token']})
        self.assertEqual(refreshed.json()['error'], 'invalid_grant')

    def test_public_client_revocation_is_bound_and_does_not_disclose_unknown_tokens(self):
        client, _, granted = self.code_flow(); other = self.register()
        response = self.client.post('/oauth/revoke', data={'client_id': other['client_id'], 'token': granted['refresh_token']})
        self.assertEqual(response.status_code, 200)
        with self.repo.read_transaction() as tx: authenticate(tx, 'Bearer '+granted['access_token'])
        self.assertEqual(self.client.post('/oauth/revoke', data={'client_id': client['client_id'], 'token': 'unknown'}).status_code, 200)
        self.assertEqual(self.client.post('/oauth/revoke', data={'client_id': client['client_id'], 'token': granted['access_token']}).status_code, 200)
        with self.repo.read_transaction() as tx:
            with self.assertRaises(Problem): authenticate(tx, 'Bearer '+granted['access_token'])

    def test_transfer_and_retirement_block_refresh_and_pending_authorizations(self):
        for change in ('transfer', 'retire'):
            with self.subTest(change=change):
                client, _, tokens = self.code_flow()
                with self.repo.transaction() as tx:
                    if change == 'transfer': tx.transfer('alice', 'bob')
                    else:
                        row = tx.get('principal', 'alice'); row['data']['retired'] = True
                        tx.put('principal', 'alice', 'alice', row['data'])
                response = self.client.post('/oauth/token', data={'client_id': client['client_id'], 'grant_type': 'refresh_token',
                    'resource': RESOURCE, 'refresh_token': tokens['refresh_token']})
                self.assertEqual(response.json()['error'], 'invalid_grant')
                with self.repo.read_transaction() as tx:
                    with self.assertRaises(Problem): authenticate(tx, 'Bearer '+tokens['access_token'])
                with self.repo.transaction() as tx:
                    if change == 'transfer': tx.transfer('bob', 'alice')
                    else:
                        row = tx.get('principal', 'alice'); row['data']['retired'] = False
                        tx.put('principal', 'alice', 'alice', row['data'])

    def test_legacy_anonymous_local_cannot_write_but_hosted_mode_remains_unchanged(self):
        with self.repo.transaction() as tx:
            with self.assertRaises(Problem) as denied:
                issue_grant(tx, 'anon', self.anon_work['id'], 'legacy')
            self.assertEqual(denied.exception.code, 'SIGN_IN_REQUIRED')
            grant = {'token': 'rvlm_historical-anonymous', 'grant_id': uid(), 'kind': 'local',
                'local_work_id': self.anon_work['id'], 'research_request_id': self.anon_work['research_request_id'],
                'issued_principal_kind': 'anonymous', 'expires_at': deadline(86400), 'revoked_at': None}
            tx.put('research_access', hashlib.sha256(grant['token'].encode()).hexdigest(), 'anon',
                {key: value for key, value in grant.items() if key != 'token'})
        with self.repo.read_transaction() as tx:
            self.assertEqual(authenticate(tx, 'Bearer '+grant['token'])['principal_kind'], 'anonymous')
            with self.assertRaises(Problem) as denied: authenticate(tx, 'Bearer '+grant['token'], write=True)
            self.assertEqual(denied.exception.code, 'SIGN_IN_REQUIRED')
        operation = {'owner_user_id': 'anon', 'local_work_id': self.anon_work['id'],
            'research_request_id': self.anon_work['research_request_id'], 'grant_id': grant['grant_id']}
        with self.repo.read_transaction() as tx:
            with self.assertRaises(Problem): authorize_commit(tx, operation)
        hosted = self.make_work('anon')
        with self.repo.transaction() as tx:
            hosted['job_id'] = 'online'; tx.put('local_work', hosted['id'], 'anon', hosted)
            tx.put('job', 'online', 'anon', {'id': 'online', 'status': 'running'})
            tx.put('queue', 'online', 'anon', {'attempt': 1})
            grant = issue_grant(tx, 'anon', hosted['id'], 'hosted-attempt', kind='hosted', execution_id=1)
        with self.repo.read_transaction() as tx:
            self.assertEqual(authenticate(tx, 'Bearer '+grant['token'], write=True)['grant']['kind'], 'hosted')
            authorize_commit(tx, {'owner_user_id': 'anon', 'local_work_id': hosted['id'],
                'research_request_id': hosted['research_request_id'], 'grant_id': grant['grant_id']})

    def test_identity_promotion_does_not_upgrade_historical_local_credentials(self):
        historical = []
        with self.repo.transaction() as tx:
            for marker in (None, 'anonymous'):
                token = 'rvlm_' + secrets.token_urlsafe(32)
                grant = {'grant_id': uid(), 'kind': 'local', 'local_work_id': self.anon_work['id'],
                    'research_request_id': self.anon_work['research_request_id'],
                    'expires_at': deadline(86400), 'revoked_at': None}
                if marker is not None: grant['issued_principal_kind'] = marker
                tx.put('research_access', hashlib.sha256(token.encode()).hexdigest(), 'anon', grant)
                historical.append((token, grant))
            principal = tx.get('principal', 'anon')['data']
            principal['me']['principal_kind'] = 'registered'
            tx.put('principal', 'anon', 'anon', principal)
        for token, grant in historical:
            with self.subTest(marker=grant.get('issued_principal_kind')), self.repo.read_transaction() as tx:
                authority = authenticate(tx, 'Bearer '+token)
                for check in (lambda: oauth.require_registered_local(authority),
                              lambda: authenticate(tx, 'Bearer '+token, write=True),
                              lambda: authorize_commit(tx, {'owner_user_id': 'anon',
                                  'local_work_id': self.anon_work['id'],
                                  'research_request_id': self.anon_work['research_request_id'],
                                  'grant_id': grant['grant_id']})):
                    with self.assertRaises(Problem) as denied: check()
                    self.assertEqual(denied.exception.code, 'REGISTERED_CONSENT_REQUIRED')
        with self.repo.transaction() as tx:
            fresh = issue_grant(tx, 'anon', self.anon_work['id'], 'fresh-registered-consent')
        with self.repo.read_transaction() as tx:
            authority = authenticate(tx, 'Bearer '+fresh['token'], write=True)
            self.assertEqual(authority['grant']['issued_principal_kind'], 'registered')
            oauth.require_registered_local(authority)

    def test_public_client_form_ambiguity_unknown_scope_and_rate_limit_fail_closed(self):
        self.assertEqual(self.client.post('/oauth/token', content='client_id=a&client_id=b',
            headers={'Content-Type': 'application/x-www-form-urlencoded'}).json()['error'], 'invalid_request')
        self.assertEqual(self.client.post('/oauth/token', json={}).json()['error'], 'invalid_request')
        self.assertEqual(self.client.post('/oauth/device_authorization', data={'client_id': oauth.LAUNCHER_CLIENT,
            'resource': RESOURCE, 'scope': 'admin'}).json()['error'], 'invalid_scope')
        client = self.register()
        for _ in range(9): self.register()
        blocked = self.client.post('/oauth/register', json={'redirect_uris': [CALLBACK]})
        self.assertEqual(blocked.status_code, 429)
        denied = self.client.get('/v1/research-oauth/consent', params={'user_code': 'BAD1-CODE'}, headers=self.browser())
        self.assertEqual(denied.status_code, 404)
        with self.repo.read_transaction() as tx:
            self.assertTrue(any(row['data']['count'] >= 10 for row in tx.list('research_oauth_rate')))
