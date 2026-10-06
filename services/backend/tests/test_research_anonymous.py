"""Real stateless MCP/OAuth transport and exact public-to-private evidence flow."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx2
import jwt
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import test_research_data as data_fixture
import test_research_execution as execution_fixture
import test_scientific_reuse as science_fixture
from reveal_backend import research_execution, research_oauth as oauth
from reveal_backend.auth import Problem
from reveal_backend.evidence_package import canonical_json
from reveal_backend.research_http import register
from reveal_backend.research_work import ResearchWorkService, deadline
from reveal_backend.repository import uid

BASE = 'http://127.0.0.1:18000'
RESOURCE = BASE + '/mcp'


class ResearchAnonymousTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        execution_fixture.ResearchExecutionTests.setUpClass()
        cls.addClassCleanup(execution_fixture.ResearchExecutionTests.doClassCleanups)

    def setUp(self):
        self.execution = execution_fixture.ResearchExecutionTests()
        self.execution.setUp(); self.addCleanup(self.execution.doCleanups)
        self.data = data_fixture.ReferenceQueryTests()
        self.data.setUp(); self.addCleanup(self.data.doCleanups)
        self.repo = self.execution.repo; self.owner = self.execution.owner
        self.work = {**self.execution.work, 'expires_at': deadline(86400),
                     'reference_generation_id': data_fixture.GEN}
        with self.repo.transaction() as tx:
            tx.put('local_work', self.work['id'], self.owner, self.work)
            for owner, kind in ((self.owner, 'registered'), ('other', 'registered'), ('anonymous', 'anonymous')):
                tx.put('principal', owner, owner, {'retired': False, 'me': {'user_id': owner,
                    'principal_kind': kind, 'workspace_expires_at': None}})
        env = patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's'*40,
            'REVEAL_PUBLIC_API_URL': BASE, 'REVEAL_PUBLIC_WEB_URL': 'http://localhost:3000',
            'REVEAL_NOTIFICATION_REDIS_URL': '', 'REVEAL_NOTIFICATION_REDIS_REST_URL': '',
            'UPSTASH_REDIS_REST_URL': '', 'UPSTASH_REDIS_REST_TOKEN': '',
            'REVEAL_PUBLIC_READS_PER_MINUTE': '10000', 'REVEAL_PUBLIC_CLIENT_READS_PER_MINUTE': '1000'})
        env.start(); self.addCleanup(env.stop)
        self.service = ResearchWorkService(self.repo, data_service=self.data.service)
        # Durable processing is invoked explicitly below, without agent execution.
        self.service.kick = lambda work_id: None
        self.service.resume_operation = lambda operation_id, **options: None
        self.app = self.make_app()
        self.client = TestClient(self.app, base_url=BASE)
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)

    def make_app(self):
        app = FastAPI()
        @app.exception_handler(Problem)
        async def problem(request, error):
            return JSONResponse({'code': error.code, 'detail': error.detail, **error.extra}, status_code=error.status)
        def no_new_work(*args):
            raise AssertionError('This test uses a frozen fixture, never a hosted agent.')
        register(app, lambda: self.repo, freeze=no_new_work, preload=lambda: None,
                 reload_gate=lambda tx: None, service_factory=lambda repo: self.service)
        return app

    def browser(self, owner=None):
        owner = owner or self.owner
        token = jwt.encode({'sub': owner, 'principal_kind': 'anonymous' if owner == 'anonymous' else 'registered',
            'iss': 'reveal-nextjs', 'aud': 'reveal-api', 'iat': int(time.time()),
            'exp': int(time.time())+120, 'jti': uid()}, 's'*40, algorithm='HS256')
        return {'Authorization': 'Bearer '+token}

    def rpc(self, method, params=None, token=None):
        headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'}
        if token is not None: headers['Authorization'] = 'Bearer '+token
        return self.client.post('/mcp', headers=headers,
            json={'jsonrpc': '2.0', 'id': 1, 'method': method, **({'params': params} if params is not None else {})})

    def call(self, name, arguments, token=None, *, error=False):
        response = self.rpc('tools/call', {'name': name, 'arguments': arguments}, token)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()['result']
        self.assertEqual(bool(result.get('isError')), error, result)
        return result['structuredContent']

    def public_capture(self):
        return self.call('get_factor', {'reference_generation_id': data_fixture.GEN,
                                      'arguments': {'factor_id': data_fixture.FACTOR}})

    def device_tokens(self, scope=None):
        params = {'client_id': oauth.LAUNCHER_CLIENT, 'resource': RESOURCE, 'local_work_id': self.work['id']}
        if scope is not None: params['scope'] = scope
        device = self.client.post('/oauth/device_authorization', data=params)
        self.assertEqual(device.status_code, 200, device.text)
        consent = self.client.get('/v1/research-oauth/consent',
            params={'user_code': device.json()['user_code']}, headers=self.browser())
        self.assertEqual(consent.status_code, 200, consent.text)
        approval = self.client.post('/v1/research-oauth/consent', headers=self.browser(),
            json={'request_id': consent.json()['request_id'], 'local_work_id': self.work['id'], 'approve': True})
        self.assertEqual(approval.status_code, 200, approval.text)
        response = self.client.post('/oauth/token', data={'client_id': oauth.LAUNCHER_CLIENT,
            'resource': RESOURCE, 'grant_type': oauth.DEVICE_GRANT, 'device_code': device.json()['device_code']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['local_work_id'], self.work['id'])
        return response.json()

    def test_official_client_initializes_and_reads_without_identity_or_session(self):
        app = self.make_app()
        async def verify():
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                    async with streamable_http_client(RESOURCE, http_client=http, terminate_on_close=False) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=10) as session:
                            initialized = await session.initialize()
                            self.assertEqual(initialized.server_info.name, 'reveal')
                            catalog = await session.list_tools()
                            tools = {tool.name: tool for tool in catalog.tools}
                            self.assertIn('attach_public_captures', tools)
                            self.assertIn('find_claims', tools)
                            result = await session.call_tool('list_data_operations', {'reference_generation_id': data_fixture.GEN})
                            self.assertFalse(result.is_error)
                            self.assertEqual(result.structured_content['generation']['generation_id'], data_fixture.GEN)
                            result = await session.call_tool('get_factor', {'reference_generation_id': data_fixture.GEN,
                                'arguments': {'factor_id': data_fixture.FACTOR}})
                            self.assertFalse(result.is_error)
                            self.assertEqual(result.structured_content['result']['items'][0]['label'], 'retained')
                            self.assertIsNone(http.headers.get('Authorization'))
        asyncio.run(verify())
        with self.repo.read_transaction() as tx:
            for kind in ('research_access', 'research_operation', 'research_pin', 'evidence_receipt'):
                self.assertEqual(tx.list(kind), [])

    def test_catalog_describes_public_and_protected_auth_and_private_calls_challenge(self):
        response = self.rpc('tools/list')
        self.assertEqual(response.status_code, 200, response.text)
        catalog = {tool['name']: tool for tool in response.json()['result']['tools']}
        self.assertIn({'type': 'noauth'}, catalog['find_claims']['_meta']['securitySchemes'])
        self.assertNotIn({'type': 'noauth'}, catalog['submit_accounts']['_meta']['securitySchemes'])
        for name, args in (
            ('get_local_work', {'local_work_id': self.work['id']}),
            ('find_claims', {'research_request_id': self.work['research_request_id']}),
            ('validate_submission', {'local_work_id': self.work['id'], 'package_sha256': self.work['package_sha256'], 'idempotency_key': 'private'}),
            ('submit_accounts', {'local_work_id': self.work['id'], 'package_sha256': self.work['package_sha256'], 'idempotency_key': 'submit'})):
            with self.subTest(name=name):
                response = self.rpc('tools/call', {'name': name, 'arguments': args})
                self.assertEqual(response.status_code, 401, response.text)
                challenge = response.headers['www-authenticate']
                self.assertIn('resource_metadata="'+BASE+'/.well-known/oauth-protected-resource/mcp"', challenge)
                if name in ('validate_submission', 'submit_accounts'): self.assertIn('research:write', challenge)
        metadata = self.client.get('/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json()['resource'], RESOURCE)
        self.assertEqual(metadata.json()['authorization_servers'], [BASE])

    def test_invalid_bearer_never_falls_back_to_anonymous_even_for_public_tools(self):
        for method, params in (('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'test', 'version': '1'}}), ('tools/list', None),
                ('tools/call', {'name': 'find_claims', 'arguments': {}}),
                ('tools/call', {'name': 'get_factor', 'arguments': {'reference_generation_id': data_fixture.GEN,
                    'arguments': {'factor_id': data_fixture.FACTOR}}})):
            with self.subTest(method=method, params=params):
                response = self.rpc(method, params, 'rvlm_invalid')
                self.assertEqual(response.status_code, 401, response.text)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('public_capture'), [])

    def test_legacy_anonymous_grant_cannot_read_private_context_or_write(self):
        work = {**self.work, 'id': 'anonymous-work', 'research_request_id': 'anonymous-request', 'owner_user_id': 'anonymous'}
        token = 'rvlm_'+'a'*43
        with self.repo.transaction() as tx:
            tx.put('local_work', work['id'], 'anonymous', work)
            tx.put('request', work['research_request_id'], 'anonymous', {'id': work['research_request_id']})
            # A historical credential predates the registered-consent boundary;
            # current issuance correctly refuses anonymous principals entirely.
            tx.put('research_access', hashlib.sha256(token.encode()).hexdigest(), 'anonymous', {
                'grant_id': 'historical-anonymous', 'kind': 'local', 'local_work_id': work['id'],
                'research_request_id': work['research_request_id'], 'expires_at': deadline(3600),
                'revoked_at': None})
        for name, args in (('get_local_work', {'local_work_id': work['id']}),
                ('find_claims', {'research_request_id': work['research_request_id']}),
                ('attach_public_captures', {'local_work_id': work['id'], 'capture_ids': ['a'*64], 'idempotency_key': 'no'})):
            response = self.rpc('tools/call', {'name': name, 'arguments': args}, token)
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()['code'], 'SIGN_IN_REQUIRED')
        self.assertEqual(self.call('find_claims', {})['total'], 0)

    def test_anonymous_science_uses_only_current_public_snapshots_and_exact_closure(self):
        fixture = science_fixture.ScientificReuseTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        with self.repo.transaction() as tx:
            fixture.seed(tx, self.owner, fixture.doc)
            private = deepcopy(fixture.doc)
            private['claims'][0].update(id='dapper:Claim.private', statement='secret unpublished discovery')
            private['scientific_accounts'][0]['component_claims'] = ['dapper:Claim.private']
            fixture.seed(tx, 'other', private)
        self.assertEqual(self.call('find_claims', {})['total'], 0)
        tokens = self.device_tokens()
        owned = self.call('find_claims', {'research_request_id': self.work['research_request_id']}, tokens['access_token'])
        self.assertEqual(owned['total'], 1)
        with self.repo.transaction() as tx: fixture.publish(tx)
        selection = self.call('find_claims', {})['items'][0]
        self.assertEqual(selection['source_kind'], 'publication_snapshot')
        result = self.call('get_scientific_object', {'selection': selection})
        self.assertEqual(result['dapper_context']['claims'], fixture.doc['claims'])
        self.assertIn(fixture.doc['files'][0], result['dapper_context']['files'])
        self.assertNotIn('secret unpublished', json.dumps(result))
        self.assertNotIn(fixture.record['path'], json.dumps(result))
        forged = {**selection, 'source_kind': 'owned_document', 'source_id': owned['items'][0]['source_id']}
        self.call('get_scientific_object', {'selection': forged}, error=True)
        with self.repo.transaction() as tx:
            tx.put('publication', 'publication', 'author', {'visibility': 'private', 'version': 2, 'snapshot_id': None})
        self.assertEqual(self.call('find_claims', {})['total'], 0)
        self.call('get_scientific_object', {'selection': selection}, error=True)

    def test_device_oauth_attaches_exact_public_bytes_then_validates_without_paid_work(self):
        capture = self.public_capture()
        descriptor = capture['artifacts'][0]
        downloaded = self.client.get(descriptor['download_url'])
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), descriptor['sha256'])
        tokens = self.device_tokens(); token = tokens['access_token']
        args = {'local_work_id': self.work['id'], 'capture_ids': [capture['capture_id']], 'idempotency_key': 'attach'}
        with patch.object(self.data.service, 'query', side_effect=AssertionError('Attachment must not query changed source data')):
            attached = self.call('attach_public_captures', args, token)
            self.assertEqual(self.call('attach_public_captures', args, token), attached)
        receipt_id = attached['receipts'][0]['receipt_id']
        receipt = self.call('get_evidence_result', {'research_request_id': self.work['research_request_id'], 'receipt_id': receipt_id}, token)
        self.assertEqual(receipt['sha256'], descriptor['sha256'])
        context, source_bytes, _ = research_execution.load_context(self.service, self.owner, self.work['id'], {'receipt_ids': [receipt_id]})
        self.assertIn(downloaded.content, source_bytes.values())
        self.assertIn(receipt['context']['dapper_file_id'], context['validation_context']['eligible_source_ids'])
        draft, import_id = self.execution.imported_draft()
        raw = canonical_json(draft)
        staged = self.call('prepare_artifact_upload', {'local_work_id': self.work['id'], 'purpose': 'account',
            'files': [{'filename': 'account.json', 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}],
            'idempotency_key': 'stage'}, token)['uploads'][0]
        upload = self.client.put(staged['url'], headers={'Authorization': 'Bearer '+token}, content=raw)
        self.assertEqual(upload.status_code, 200, upload.text)
        completed = self.call('complete_artifact_upload', {'local_work_id': self.work['id'],
            'upload_ids': [staged['upload_id']], 'idempotency_key': 'complete'}, token)
        validation = self.call('validate_submission', {'local_work_id': self.work['id'],
            'package_sha256': self.work['package_sha256'], 'account_artifact_ids': [completed['artifacts'][0]['id']],
            'receipt_ids': [receipt_id], 'import_ids': [import_id], 'idempotency_key': 'validate'}, token)
        self.service.run_operation(validation['operation_id'])
        result = self.call('get_operation', {'local_work_id': self.work['id'], 'operation_id': validation['operation_id']}, token)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertTrue(result['report']['valid'], result)
        with self.repo.read_transaction() as tx:
            for kind in ('job', 'queue', 'paragraph', 'account', 'scientific_document'):
                self.assertEqual(tx.list(kind), [])

    def test_read_only_oauth_allows_context_and_rejects_mutations_before_any_write(self):
        token = self.device_tokens('research:read')['access_token']
        self.assertEqual(self.call('get_local_work', {'local_work_id': self.work['id']}, token)['id'], self.work['id'])
        for name, args in (('attach_public_captures', {'local_work_id': self.work['id'], 'capture_ids': ['a'*64], 'idempotency_key': 'attach'}),
                ('validate_submission', {'local_work_id': self.work['id'], 'package_sha256': self.work['package_sha256'], 'idempotency_key': 'validate'}),
                ('get_factor', {'research_request_id': self.work['research_request_id'], 'arguments': {'factor_id': data_fixture.FACTOR}, 'idempotency_key': 'query'})):
            response = self.rpc('tools/call', {'name': name, 'arguments': args}, token)
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()['code'], 'INSUFFICIENT_SCOPE')
        with self.repo.read_transaction() as tx:
            for kind in ('evidence_receipt', 'research_operation', 'research_upload'):
                self.assertEqual(tx.list(kind), [])

    def test_browser_revocation_blocks_oauth_access_and_refresh_family(self):
        tokens = self.device_tokens()
        response = self.client.delete('/v1/local-work/'+self.work['id']+'/grants/'+tokens['grant_id'], headers=self.browser())
        self.assertEqual(response.status_code, 204, response.text)
        private = self.rpc('tools/call', {'name': 'get_local_work', 'arguments': {'local_work_id': self.work['id']}}, tokens['access_token'])
        self.assertEqual(private.status_code, 401, private.text)
        public = self.rpc('tools/call', {'name': 'find_claims', 'arguments': {}}, tokens['access_token'])
        self.assertEqual(public.status_code, 401, public.text)
        refresh = self.client.post('/oauth/token', data={'client_id': oauth.LAUNCHER_CLIENT,
            'resource': RESOURCE, 'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']})
        self.assertEqual(refresh.status_code, 400, refresh.text)
        self.assertEqual(refresh.json()['error'], 'invalid_grant')
        self.assertEqual(self.call('find_claims', {})['total'], 0)

    def test_connected_graph_select_is_anonymous_then_attached_without_requery(self):
        from reveal_backend.research_graphs import GRAPHS, GraphQueryService
        calls = []
        row = {'gene': {'type': 'uri', 'value': 'urn:gene:1'}, 'label': {'type': 'literal', 'value': 'example'}}
        class Client:
            server_info = {'name': 'fixture-graph', 'version': '1'}
            def call(self, name, arguments):
                calls.append((name, deepcopy(arguments)))
                return {'content': [], 'structuredContent': {'results': {'bindings': [row]}}}
        self.service.graph_service = GraphQueryService(client_factory=Client)
        with self.repo.transaction() as tx:
            request = tx.get('request', self.work['research_request_id'])['data']
            request['composer']['selected_kgs'] = ['prokn']
            tx.put('request', self.work['research_request_id'], self.owner, request)
        graphs = self.call('list_knowledge_graphs', {})
        self.assertEqual({item['id'] for item in graphs['graphs']}, set(GRAPHS))
        query = 'SELECT ?gene ?label WHERE { GRAPH <'+GRAPHS['prokn']+'> { ?gene <urn:label> ?label } }'
        capture = self.call('sparql_query', {'graph': 'prokn', 'query': query, 'limit': 2})
        self.assertEqual(capture['result']['items'], [row])
        self.assertIsNone(capture['reference_generation_id'])
        self.assertEqual(capture['arguments']['query'], query)
        self.assertTrue(calls[0][1]['query'].endswith('LIMIT 2'))
        artifact = next(item for item in capture['artifacts'] if item['sha256'] == capture['raw_sha256'])
        downloaded = self.client.get(artifact['download_url'])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), artifact['sha256'])
        token = self.device_tokens()['access_token']
        with patch.object(self.service.graph_service, 'query', side_effect=AssertionError('Do not requery graph')):
            attached = self.call('attach_public_captures', {'local_work_id': self.work['id'],
                'capture_ids': [capture['capture_id']], 'idempotency_key': 'graph-attach'}, token)
        receipt = attached['receipts'][0]['receipt_id']
        context, files, _ = research_execution.load_context(self.service, self.owner, self.work['id'], {'receipt_ids': [receipt]})
        self.assertIn(downloaded.content, files.values())
        self.assertIn(capture['dapper_file_id'], context['validation_context']['eligible_source_ids'])
        self.assertNotIn(capture['dapper_file_id'], context['validation_context']['cfde_source_ids'])
        draft = deepcopy(self.execution.science.draft)
        draft['evidence_items'][0].update(was_derived_from=[capture['dapper_file_id']],
            context='The exact SELECT binding at /result/items/0/label/value.', snippet='example')
        uploaded = self.service.retain(self.owner, self.work['id'], canonical_json(draft), 'graph-account.json', purpose='account')
        validation = self.call('validate_submission', {'local_work_id': self.work['id'],
            'package_sha256': self.work['package_sha256'], 'account_artifact_ids': [uploaded['id']],
            'receipt_ids': [receipt], 'idempotency_key': 'validate-graph'}, token)
        self.service.run_operation(validation['operation_id'])
        outcome = self.call('get_submission', {'local_work_id': self.work['id'],
            'submission_id': validation['operation_id']}, token)
        self.assertEqual(outcome['state'], 'succeeded', outcome)
        with self.repo.read_transaction() as tx:
            for kind in ('account', 'scientific_document', 'job', 'queue'): self.assertEqual(tx.list(kind), [])

    def test_private_graph_receipts_obey_selected_graph_scope_and_idempotency(self):
        from reveal_backend.research_graphs import GraphQueryService
        calls = []
        class Client:
            server_info = {'name': 'fixture-graph'}
            def call(self, name, arguments):
                calls.append((name, deepcopy(arguments)))
                return {'structuredContent': {'rows': [{'s': 'urn:s', 'p': 'urn:p', 'o': 'urn:o'}]}}
        self.service.graph_service = GraphQueryService(client_factory=Client)
        with self.repo.transaction() as tx:
            request = tx.get('request', self.work['research_request_id'])['data']
            request['composer']['selected_kgs'] = ['prokn']
            tx.put('request', self.work['research_request_id'], self.owner, request)
        token = self.device_tokens()['access_token']
        request_id = self.work['research_request_id']
        selected = self.call('list_knowledge_graphs', {'research_request_id': request_id}, token)
        self.assertEqual([item['id'] for item in selected['graphs']], ['prokn'])
        args = {'research_request_id': request_id, 'graph': 'prokn', 'subject': 'urn:s', 'idempotency_key': 'kg-read'}
        result = self.call('query_graph', args, token)
        self.assertEqual(self.call('query_graph', args, token), result)
        self.service.run_operation(result['operation_id'])
        operation = self.call('get_operation', {'local_work_id': self.work['id'], 'operation_id': result['operation_id']}, token)
        self.assertEqual(operation['state'], 'succeeded', operation)
        self.assertEqual(operation['result']['source_mode'], 'external_kg')
        self.assertEqual(len(calls), 1)
        denied = self.call('query_graph', {**args, 'graph': 'biomarkerkg', 'idempotency_key': 'unselected'}, token, error=True)
        self.assertEqual(denied['code'], 'GRAPH_NOT_SELECTED')
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('research_operation', self.owner)), 1)
            self.assertEqual(len(tx.list('evidence_receipt', self.owner)), 1)
            for kind in ('job', 'queue'): self.assertEqual(tx.list(kind), [])


if __name__ == '__main__':
    unittest.main()
