"""Application publication controls, frozen reads, ownership and stable discovery."""
from copy import deepcopy
import json
from urllib.parse import urlencode
import unittest
from unittest.mock import patch

from reveal_backend import app as api, publication
from reveal_backend.repository import digest, uid
from reveal_backend.runtime_config import ROOT
import test_account_discovery as discovery


class PublicationTests(unittest.TestCase):
    principal = discovery.AccountDiscoveryTests.principal
    headers = discovery.AccountDiscoveryTests.headers
    def setUp(self):
        discovery.AccountDiscoveryTests.setUp(self)
        self.fixture = json.loads((ROOT / 'services/frontend/src/lib/fixtures/contract.json').read_text())
        self.gap = deepcopy(self.fixture['gaps']['items'][0])
        self.catalog.gaps = {self.gap['object']['id']: self.gap}
        self.catalog.by_source = {self.gap['source']['source_id']: self.gap}
        self.account_id = self.fixture['account']['root_id']
        self.paragraph_id = self.fixture['paragraph']['root_id']
        self.claim_id = self.fixture['claim']['root_id']
        self.route = '/v1/accounts/' + self.account_id
        self.control = self.route + '/publication'
        self.seed(self.owner)

    def seed(self, owner, *, paragraph=True):
        envelope = deepcopy(self.fixture['account'])
        envelope.pop('publication', None)
        envelope['research_statement'] = {'status': 'succeeded' if paragraph else 'not_requested',
            'paragraph_id': self.paragraph_id if paragraph else None, 'job_id': uid() if paragraph else None}
        account = envelope['document']['scientific_accounts'][0]
        job_id, request_id = uid(), uid()
        summary = {'account': account, 'knowledge_gap': self.gap['object'], 'claim_count': len(account['component_claims']),
            'created_at': '2026-09-28T10:00:00Z', 'job_id': job_id, 'research_statement': envelope['research_statement']}
        self.job_id = job_id
        attribution = {'user_id': owner, 'person_id': None, 'principal_kind': 'anonymous', 'display_name': 'Original proposer',
            'orcid': None, 'orcid_authenticated': False, 'observed_at': summary['created_at']}
        with self.repo.transaction() as tx:
            key = digest([owner, self.account_id])
            tx.put('account', key, owner, {'result': envelope, 'summary': summary})
            tx.put('account_membership', key, owner, {'account_id': self.account_id, 'summary': summary})
            tx.put('job', job_id, owner, {'research_request_id': request_id})
            tx.put('request', request_id, owner, {'attribution': attribution, 'private_request_marker': 'never-public'})
            if paragraph:
                tx.put('paragraph', digest([owner, self.paragraph_id]), owner,
                    {'account_id': self.account_id, 'result': deepcopy(self.fixture['paragraph'])})
            for metadata in self.fixture['account']['citation_metadata']:
                tx.put('citation', f"{metadata['target_id']}:{metadata['metadata_revision']}", owner, metadata)
                tx.put('grant', digest([owner, metadata['target_id']]), owner, {'target_id': metadata['target_id']})
        return summary

    def request(self, method, path, owner=None, **kwargs):
        headers = kwargs.pop('headers', {})
        if owner: headers.update(self.headers(owner))
        return getattr(self.client, method)(path, headers=headers, **kwargs)

    def publish(self, owner=None, *, visibility='public', version=0, key=None):
        return self.request('post', self.control, owner or self.owner,
            headers={'Idempotency-Key': key or uid()}, json={'visibility': visibility, 'expected_version': version})

    def test_explicit_owner_control_and_idempotency_leave_content_immutable(self):
        before = self.request('get', self.route, self.owner).json()
        state = self.request('get', self.control, self.owner)
        self.assertEqual(state.status_code, 200, state.text)
        self.assertEqual(state.json()['visibility'], 'private'); self.assertTrue(state.json()['can_manage'])
        self.assertEqual(self.client.get(self.route).status_code, 404)
        self.assertEqual(self.publish(self.other).status_code, 404)
        key = uid(); first = self.publish(key=key)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.publish(key=key).json(), first.json())
        self.assertEqual(self.publish(key=key, visibility='private', version=1).json()['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.publish(version=0).json()['code'], 'PUBLICATION_VERSION_CONFLICT')
        self.assertEqual(self.publish(self.other, version=1).status_code, 404)
        after = self.request('get', self.route, self.owner).json()
        self.assertEqual(before['document'], after['document']); self.assertEqual(before['payloads'], after['payloads'])
        self.assertTrue(after['publication']['can_manage'])
        self.assertFalse(self.client.get(self.control).json()['can_manage'])

    def test_published_science_exact_citations_and_exports_without_private_jobs(self):
        response = self.publish(); self.assertEqual(response.status_code, 200, response.text)
        account = self.client.get(self.route)
        self.assertEqual(account.status_code, 200, account.text)
        self.assertEqual(account.headers['cache-control'], 'private, no-store')
        self.assertIn('Authorization', account.headers['vary'])
        self.assertFalse(account.json()['publication']['can_manage'])
        self.assertIsNone(account.json()['research_statement']['job_id'])
        for path in ['/v1/claims/' + self.claim_id, '/v1/objects/' + self.gap['object']['id'],
                '/v1/paragraphs/' + self.paragraph_id, '/v1/citations/' + self.claim_id + '?revision=1']:
            result = self.client.get(path); self.assertEqual(result.status_code, 200, result.text)
        record = self.client.get('/v1/citations/' + self.claim_id + '?revision=1').json()
        original = next(row for row in self.fixture['account']['citation_metadata'] if row['target_id'] == self.claim_id)
        self.assertEqual(record, original, 'Publication must not rewrite immutable citation metadata')
        self.assertEqual(self.client.get('/v1/citations/' + self.claim_id + '?revision=2').status_code, 404)
        for format in ('markdown', 'latex', 'bibtex', 'rich-text'):
            result = self.client.get('/v1/paragraphs/' + self.paragraph_id + '/export?format=' + format)
            self.assertEqual(result.status_code, 200, result.text)
        result = self.client.post('/v1/citations/render', json={'paragraph_id': self.paragraph_id, 'style': 'apa', 'locale': 'en-US'})
        self.assertEqual(result.status_code, 200, result.text)
        for suffix in ('', '/events', '/evidence-package'):
            self.assertEqual(self.client.get('/v1/jobs/' + self.job_id + suffix).status_code, 401)
            self.assertEqual(self.request('get', '/v1/jobs/' + self.job_id + suffix, self.other).status_code, 404)
        result = self.client.get('/v1/knowledge-gaps/' + self.gap['object']['id'] + '/accounts').json()
        self.assertIsNone(result['items'][0]['job_id']); self.assertIsNone(result['items'][0]['research_statement']['job_id'])
        self.assertEqual(result['items'][0]['attribution']['user_id'], self.owner)

    def test_invalid_supplied_auth_never_downgrades_on_any_public_route(self):
        self.assertEqual(self.publish().status_code, 200)
        for path in [self.route, self.control, '/v1/claims/' + self.claim_id,
                '/v1/citations/' + self.claim_id, '/v1/paragraphs/' + self.paragraph_id,
                '/v1/paragraphs/' + self.paragraph_id + '/export?format=markdown', '/v1/knowledge-gaps']:
            response = self.client.get(path, headers={'Authorization': 'Bearer invalid'})
            self.assertEqual(response.status_code, 401, path)
        response = self.client.post('/v1/citations/render', headers={'Authorization': 'Bearer invalid'},
            json={'paragraph_id': self.paragraph_id, 'style': 'apa', 'locale': 'en-US'})
        self.assertEqual(response.status_code, 401)

    def test_independent_publications_deduplicate_and_last_unpublish_revokes(self):
        self.seed(self.other)
        self.assertEqual(self.publish().status_code, 200); self.assertEqual(self.publish(self.other).status_code, 200)
        def count(scope='public', owner=None):
            response = self.request('get', '/v1/knowledge-gaps?scope=' + scope, owner)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()['items'][0]['scientific_accounts']['count']
        self.assertEqual(count(), 1); self.assertEqual(count('workspace', self.owner), 1)
        self.assertEqual(self.client.get('/v1/knowledge-gaps?scope=workspace').status_code, 401)
        self.assertEqual(self.publish(visibility='private', version=1).status_code, 200)
        self.assertEqual(count(), 1); self.assertEqual(self.client.get(self.route).status_code, 200)
        self.assertEqual(self.publish(self.other, visibility='private', version=1).status_code, 200)
        self.assertEqual(count(), 0)
        for path in [self.route, self.control, '/v1/claims/' + self.claim_id, '/v1/paragraphs/' + self.paragraph_id,
                '/v1/citations/' + self.claim_id, '/v1/paragraphs/' + self.paragraph_id + '/export?format=markdown']:
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.request('get', self.route, self.owner).status_code, 200)

    def test_future_paragraph_requires_explicit_update_and_transfer_preserves_authorship(self):
        with self.repo.transaction() as tx:
            key = digest([self.owner, self.account_id]); account = tx.get('account', key)['data']
            account['result']['research_statement'] = {'status': 'not_requested', 'job_id': None, 'paragraph_id': None}
            tx.put('account', key, self.owner, account)
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.client.get('/v1/paragraphs/' + self.paragraph_id).status_code, 404)
        self.seed(self.owner)
        self.assertTrue(self.request('get', self.control, self.owner).json()['has_unpublished_changes'])
        self.assertEqual(self.client.get('/v1/paragraphs/' + self.paragraph_id).status_code, 404)
        self.assertEqual(self.publish(version=1).status_code, 200)
        self.assertEqual(self.client.get('/v1/paragraphs/' + self.paragraph_id).status_code, 200)
        with self.repo.transaction() as tx: tx.transfer(self.owner, self.other)
        self.assertFalse(self.request('get', self.control, self.owner).json()['can_manage'])
        self.assertTrue(self.request('get', self.control, self.other).json()['can_manage'])
        self.assertEqual(self.publish(self.owner, visibility='private', version=2).status_code, 404)
        listing = self.client.get('/v1/knowledge-gaps/' + self.gap['object']['id'] + '/accounts').json()
        self.assertEqual(listing['items'][0]['attribution']['user_id'], self.owner)
        self.assertEqual(self.publish(self.other, visibility='private', version=2).status_code, 200)

    def test_anonymous_workspace_owner_must_sign_in_to_publish(self):
        with self.repo.transaction() as tx:
            row = tx.get('principal', self.owner)['data']; row['me']['principal_kind'] = 'anonymous'
            row['me']['workspace_expires_at'] = '2099-01-01T00:00:00Z'; tx.put('principal', self.owner, self.owner, row)
        import jwt
        token = jwt.decode(self.headers(self.owner)['Authorization'][7:], options={'verify_signature': False})
        token['principal_kind'] = 'anonymous'
        response = self.client.post(self.control, headers={'Authorization': 'Bearer ' + jwt.encode(token, 's'*40, algorithm='HS256'),
            'Idempotency-Key': uid()}, json={'visibility': 'public', 'expected_version': 0})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()['code'], 'SIGN_IN_REQUIRED')

    def test_random_ties_remain_stable_across_pages_and_expire_on_count_changes(self):
        for letter in 'abcdefgh':
            gap = deepcopy(self.gap); gap['object']['id'] = 'dapper:KnowledgeGap.' + letter * 32
            gap['source']['source_id'] = 'dismech:' + letter; self.catalog.gaps[gap['object']['id']] = gap
        def browse(seed):
            ids = []; cursor = None
            with patch.object(api, 'uid', return_value=seed):
                while True:
                    response = self.client.get('/v1/knowledge-gaps?' + urlencode({'limit': 2, **({'cursor': cursor} if cursor else {})}))
                    self.assertEqual(response.status_code, 200, response.text); result = response.json()
                    ids.extend(item['object']['id'] for item in result['items']); cursor = result['page']['next_cursor']
                    if not cursor: break
            return ids
        first = browse('11111111-1111-4111-8111-111111111111')
        self.assertEqual(len(first), len(set(first))); self.assertEqual(len(first), len(self.catalog.gaps))
        self.assertEqual(first, browse('11111111-1111-4111-8111-111111111111'))
        self.assertNotEqual(first, browse('22222222-2222-4222-8222-222222222222'))
        page = self.client.get('/v1/knowledge-gaps?limit=2').json(); cursor = page['page']['next_cursor']
        self.assertEqual(self.request('get', '/v1/knowledge-gaps?limit=2&scope=workspace&cursor=' + cursor, self.owner).status_code, 409)
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.client.get('/v1/knowledge-gaps?limit=2&cursor=' + cursor).status_code, 409)
        self.assertEqual(self.client.get('/v1/knowledge-gaps').json()['items'][0]['object']['id'], self.gap['object']['id'])


if __name__ == '__main__': unittest.main()
