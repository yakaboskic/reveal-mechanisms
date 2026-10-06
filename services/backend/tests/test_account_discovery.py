"""Scoped gap popularity and account discovery without private-work leakage."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import jwt

from reveal_backend import app as api
from reveal_backend.auth import Problem
from reveal_backend.catalog import Catalog
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.runtime_config import ROOT


class AccountDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name) / 'app.sqlite')); self.repo.migrate()
        fixture = json.loads((ROOT / 'services/frontend/src/lib/fixtures/contract.json').read_text())
        self.base_account = fixture['account']['document']['scientific_accounts'][0]
        self.catalog = Catalog(); self.catalog.loaded = True
        self.catalog.dismech_import = 'source-snapshot'
        self.catalog.gaps = {}; self.catalog.by_source = {}
        for letter in 'abc':
            gap = deepcopy(fixture['gaps']['items'][0])
            gap['object']['id'] = 'dapper:KnowledgeGap.' + letter * 32
            gap['source']['source_id'] = 'dismech:' + letter
            # Same source prose deliberately proves identity, not text, joins.
            self.catalog.gaps[gap['object']['id']] = gap
            self.catalog.by_source[gap['source']['source_id']] = gap
        self.gaps = list(self.catalog.gaps.values())
        self.search_path = '/v1/knowledge-gaps/search?' + urlencode({'q': self.gaps[0]['object']['text'].split()[0]})
        for context in (patch.object(api, 'repo', self.repo), patch.object(api, 'catalog', self.catalog),
                        patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 40,
                            'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE': 'reveal-api'})):
            context.start(); self.addCleanup(context.stop)
        self.client = TestClient(api.app)
        self.owner, self.other = self.principal(), self.principal()

    def principal(self):
        identity = uid()
        with self.repo.transaction() as tx:
            tx.put('principal', identity, identity, {'retired': False, 'me': {
                'user_id': identity, 'principal_kind': 'registered', 'display_name': 'Current profile',
                'workspace_expires_at': None}})
        return identity

    def headers(self, owner):
        current = int(time.time())
        token = jwt.encode({'sub': owner, 'principal_kind': 'registered', 'iss': 'reveal-nextjs',
            'aud': 'reveal-api', 'iat': current, 'exp': current + 120, 'jti': uid()}, 's' * 40, algorithm='HS256')
        return {'Authorization': 'Bearer ' + token}

    def accepted(self, owner, gap, letter, *, date='2026-09-28T10:00:00Z', author=None):
        account = deepcopy(self.base_account)
        account.update(id='dapper:ScientificAccount.' + letter * 32, question=gap['object']['id'])
        job_id, request_id = uid(), uid()
        summary = {'account': account, 'knowledge_gap': gap['object'], 'claim_count': len(account['component_claims']),
            'created_at': date, 'job_id': job_id, 'research_statement': {'status': 'succeeded', 'job_id': uid(), 'paragraph_id': None}}
        original = {'user_id': author or owner, 'person_id': None, 'principal_kind': 'anonymous',
            'display_name': 'Original proposer', 'orcid': None, 'orcid_authenticated': False, 'observed_at': date}
        with self.repo.transaction() as tx:
            tx.put('account_membership', digest([owner, account['id']]), owner, {'account_id': account['id'], 'summary': summary})
            tx.put('job', job_id, owner, {'research_request_id': request_id})
            tx.put('request', request_id, owner, {'attribution': original})
        return summary

    def get(self, path, owner=None):
        if owner and path.startswith('/v1/knowledge-gaps') and 'scope=' not in path:
            path += ('&' if '?' in path else '?') + 'scope=workspace'
        response = self.client.get(path, headers=self.headers(owner) if owner else {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_distinct_accepted_accounts_rank_exact_gaps_and_never_private_others(self):
        a, b, c = self.gaps
        saved = self.accepted(self.owner, b, 'd'); self.accepted(self.owner, b, 'e'); self.accepted(self.owner, a, 'f')
        for letter in 'ghij': self.accepted(self.other, c, letter)
        with self.repo.transaction() as tx:
            tx.put('account_membership', 'duplicate-delivery', self.owner, {'account_id': saved['account']['id'], 'summary': saved})
            tx.put('job', uid(), self.owner, {'kind': 'analysis', 'status': 'failed', 'gap_id': c['object']['id']})
            tx.put('job', uid(), self.owner, {'kind': 'paragraph', 'status': 'succeeded', 'input_account_id': saved['account']['id']})
        result = self.get('/v1/knowledge-gaps', self.owner)
        self.assertEqual([item['source']['source_id'] for item in result['items']], ['dismech:b', 'dismech:a', 'dismech:c'])
        self.assertEqual([item['scientific_accounts']['count'] for item in result['items']], [2, 1, 0])
        self.assertTrue(all(item['scientific_accounts']['scope'] == 'owner_exact_gap' for item in result['items']))
        self.assertTrue(all(item['scientific_accounts']['ranking'] == 'account_count' for item in result['items']))
        self.assertEqual(len(self.get('/v1/accounts', self.owner)['items']), 3)
        public = self.get('/v1/knowledge-gaps')
        self.assertEqual([item['scientific_accounts']['count'] for item in public['items']], [0, 0, 0])
        self.assertEqual(self.get('/v1/knowledge-gaps/' + b['object']['id'] + '/accounts')['items'], [])

    @staticmethod
    def mechanism_attachment(letter, **changes):
        target={'source':'dismech','source_id':'dismech:mechanism-'+letter,'source_revision':'a'*64,
                'dapper_id':'dapper:Mechanism.'+letter*32}
        attachment={'source_reference':letter,'target_kind':'pathophysiology','resolution':'resolved',
                    'target':target,'label':'Mechanism '+letter}
        attachment.update(changes)
        return attachment

    def test_mechanism_tiebreaker_counts_distinct_resolved_dismech_identities(self):
        valid=self.mechanism_attachment('a')
        alias=deepcopy(valid); alias['target']['source_id']='dismech:another-source-alias'
        nonmechanism=deepcopy(valid); nonmechanism['target']['dapper_id']='dapper:GeneSet.'+'b'*32
        foreign=deepcopy(valid); foreign['target']['source']='eaggl'; foreign['target']['source_id']='factor:other'
        unbound=deepcopy(valid); unbound['target']['source_id']='another:source'
        attachments=[valid,deepcopy(valid),alias,nonmechanism,foreign,unbound,
            self.mechanism_attachment('b',resolution='unresolved'), self.mechanism_attachment('c',target=None),
            # The resolved target identity, not a display kind label, establishes a Mechanism.
            self.mechanism_attachment('d',target_kind='other')]
        self.assertEqual(api.resolved_mechanism_count({'attachments':attachments}),2)
        self.assertEqual(api.resolved_mechanism_count({'attachments':[]}),0)

    def test_account_ranking_prefers_more_mechanisms_only_after_account_count(self):
        a,b,c=self.gaps
        a['attachments']=[self.mechanism_attachment('a')]
        b['attachments']=[self.mechanism_attachment(letter) for letter in 'ab']
        c['attachments']=[self.mechanism_attachment(letter) for letter in 'abc']
        self.accepted(self.owner,a,'d'); self.accepted(self.owner,b,'e')
        result=self.get('/v1/knowledge-gaps?sort=accounts',self.owner)
        self.assertEqual([item['object']['id'] for item in result['items']], [b['object']['id'],a['object']['id'],c['object']['id']])
        self.accepted(self.owner,a,'f')
        result=self.get('/v1/knowledge-gaps?sort=accounts',self.owner)
        self.assertEqual([item['object']['id'] for item in result['items']], [a['object']['id'],b['object']['id'],c['object']['id']])

    def test_vote_ranking_prefers_mechanisms_before_accounts_after_vote_score(self):
        a,b,c=self.gaps
        for gap,letters in zip(self.gaps,('a','ab','abc')):
            gap['attachments']=[self.mechanism_attachment(letter) for letter in letters]
        self.accepted(self.owner,a,'d'); self.accepted(self.owner,a,'e'); self.accepted(self.owner,b,'f')
        with self.repo.transaction() as tx:
            for gap in (a,b): api.votes.change(tx,'gap',gap['object']['id'],gap['object']['id'],self.owner,1)
        result=self.get('/v1/knowledge-gaps?sort=votes',self.owner)
        self.assertEqual([item['object']['id'] for item in result['items']], [b['object']['id'],a['object']['id'],c['object']['id']])
        with self.repo.transaction() as tx:
            for owner in (self.owner,self.other): api.votes.change(tx,'gap',c['object']['id'],c['object']['id'],owner,1)
        result=self.get('/v1/knowledge-gaps?sort=votes',self.owner)
        self.assertEqual([item['object']['id'] for item in result['items']], [c['object']['id'],b['object']['id'],a['object']['id']])
        # Account count remains the next tie break when votes and mechanism counts match.
        b['attachments']=deepcopy(a['attachments'])
        result=self.get('/v1/knowledge-gaps?sort=votes',self.owner)
        self.assertEqual([item['object']['id'] for item in result['items']], [c['object']['id'],a['object']['id'],b['object']['id']])

    def test_mechanism_ties_keep_seeded_pages_stable_and_attachment_changes_expire_cursor(self):
        for gap in self.gaps: gap['attachments']=[self.mechanism_attachment('a')]
        with patch.object(api,'browse_seed',return_value='11111111-1111-4111-8111-111111111111'):
            for sort in ('accounts','votes'):
                with self.subTest(sort=sort):
                    route='/v1/knowledge-gaps?sort='+sort
                    complete=self.get(route,self.owner)
                    first=self.get(route+'&limit=1',self.owner)
                    cursor=first['page']['next_cursor']
                    next_path=route+'&limit=2&scope=workspace&cursor='+cursor
                    second=self.get(next_path,self.owner)
                    self.assertEqual([item['object']['id'] for item in first['items']+second['items']],
                                     [item['object']['id'] for item in complete['items']])
                    self.gaps[2]['attachments'].append(self.mechanism_attachment('b'))
                    self.assertEqual(self.client.get(next_path,headers=self.headers(self.owner)).status_code,409)
                    self.gaps[2]['attachments'].pop()

    def test_archived_accounts_are_counted_apart_and_never_rank(self):
        from reveal_backend.account_discovery import counted_gap, counts_by_gap
        from reveal_backend.reference_generation import ARCHIVE_STATUS
        a, b, _ = self.gaps
        summary = lambda gap, archived=False: {'account': {'question': gap['object']['id']},
                                               **({'archive': {'status': ARCHIVE_STATUS}} if archived else {})}
        counts = counts_by_gap([summary(a), summary(a, True), summary(a, True), summary(b, True)])
        first, second = (counted_gap(gap, counts, '', 'now')['scientific_accounts'] for gap in (a, b))
        self.assertEqual((first['count'], first['archived_count']), (1, 2))
        self.assertEqual((second['count'], second['archived_count']), (0, 1))
        self.assertNotIn('archived_count', counted_gap(self.gaps[2], counts, '', 'now')['scientific_accounts'])

    def test_search_relevance_order_is_unchanged_but_counts_and_exact_gap_update(self):
        gap = self.gaps[1]; self.accepted(self.owner, gap, 'd')
        result = self.get(self.search_path, self.owner)
        self.assertEqual([item['gap']['source']['source_id'] for item in result['items']], ['dismech:a', 'dismech:b', 'dismech:c'])
        self.assertEqual([item['gap']['scientific_accounts']['count'] for item in result['items']], [0, 1, 0])
        self.assertEqual(self.get('/v1/knowledge-gaps/' + gap['object']['id'], self.owner)['scientific_accounts']['count'], 1)
        self.assertEqual(gap['scientific_accounts']['count'], 0, 'Never mutate the shared public source cache')
        self.accepted(self.owner, gap, 'e')
        self.assertEqual(self.get('/v1/knowledge-gaps/' + gap['object']['id'], self.owner)['scientific_accounts']['count'], 2)

    def test_pagination_ignores_observation_time_but_binds_owner_and_count_snapshot(self):
        with patch.object(api, 'now', return_value='2026-09-28T10:00:00Z'):
            first = self.get('/v1/knowledge-gaps?limit=1', self.owner)
        path = '/v1/knowledge-gaps?limit=1&scope=workspace&cursor=' + first['page']['next_cursor']
        with patch.object(api, 'now', return_value='2026-09-28T10:00:05Z'):
            second = self.get(path, self.owner)
        self.assertNotEqual(second['items'][0]['source']['source_id'], first['items'][0]['source']['source_id'])
        self.assertEqual(second['items'][0]['scientific_accounts']['as_of'], '2026-09-28T10:00:05Z')
        self.assertEqual(self.client.get(path, headers=self.headers(self.other)).status_code, 409)
        self.accepted(self.owner, self.gaps[2], 'd')
        self.assertEqual(self.client.get(path, headers=self.headers(self.owner)).status_code, 409)

    def test_gap_account_pagination_is_newest_first_and_preserves_original_attribution(self):
        gap = self.gaps[0]
        first = self.accepted(self.owner, gap, 'd', author=self.other)
        last = self.accepted(self.owner, gap, 'e', date='2026-09-28T11:00:00Z')
        self.accepted(self.other, gap, 'f')
        path = '/v1/knowledge-gaps/' + gap['object']['id'] + '/accounts?limit=1'
        result = self.get(path, self.owner)
        self.assertEqual(result['items'][0]['account']['id'], last['account']['id'])
        page = self.get(path + '&cursor=' + result['page']['next_cursor'], self.owner)
        self.assertEqual(page['items'][0]['account']['id'], first['account']['id'])
        self.assertEqual(page['items'][0]['attribution']['user_id'], self.other)
        self.assertEqual(page['items'][0]['attribution']['display_name'], 'Original proposer')
        self.assertFalse(page['page']['has_more'])
        self.assertEqual(len(self.get('/v1/accounts?gap_id=' + gap['object']['id'], self.owner)['items']), 2)

    def test_transfer_changes_access_not_historical_proposer(self):
        gap = self.gaps[0]; self.accepted(self.owner, gap, 'd')
        with self.repo.transaction() as tx: tx.transfer(self.owner, self.other)
        path = '/v1/knowledge-gaps/' + gap['object']['id'] + '/accounts'
        self.assertEqual(self.get(path, self.owner)['items'], [])
        result = self.get(path, self.other)
        self.assertEqual(result['items'][0]['attribution']['user_id'], self.owner)

    def test_attribution_never_exposes_unrelated_profile_fields(self):
        gap = self.gaps[0]; summary = self.accepted(self.owner, gap, 'd')
        with self.repo.transaction() as tx:
            request_id = tx.get('job', summary['job_id'])['data']['research_request_id']
            request = tx.get('request', request_id)['data']
            request['attribution']['email'] = 'private@example.invalid'
            request['attribution']['identity_token'] = 'must-not-be-disclosed'
            tx.put('request', request_id, self.owner, request)
        result = self.get('/v1/knowledge-gaps/' + gap['object']['id'] + '/accounts', self.owner)
        self.assertNotIn('email', result['items'][0]['attribution'])
        self.assertNotIn('identity_token', result['items'][0]['attribution'])

    def test_exact_digest_not_same_text_native_alias_or_changed_source_revision(self):
        gap = self.gaps[0]; self.accepted(self.owner, gap, 'd')
        other = self.gaps[1]
        self.assertEqual(gap['object']['text'], other['object']['text'])
        self.assertEqual(self.get('/v1/knowledge-gaps/' + other['object']['id'] + '/accounts', self.owner)['items'], [])
        path = '/v1/knowledge-gaps/' + gap['object']['id'] + '/accounts?source_revision=' + '0' * 64
        self.assertEqual(self.client.get(path, headers=self.headers(self.owner)).status_code, 409)

    def test_missing_original_attribution_is_null_and_inconsistent_membership_not_counted(self):
        gap = self.gaps[0]; summary = self.accepted(self.owner, gap, 'd')
        with self.repo.transaction() as tx:
            tx.remove('job', summary['job_id'])
            invalid = deepcopy(summary); invalid['account']['question'] = self.gaps[1]['object']['id']
            tx.put('account_membership', 'inconsistent', self.owner, {'account_id': invalid['account']['id'], 'summary': invalid})
        result = self.get('/v1/knowledge-gaps/' + gap['object']['id'] + '/accounts', self.owner)
        self.assertEqual(len(result['items']), 1); self.assertIsNone(result['items'][0]['attribution'])
        self.assertEqual(self.get('/v1/knowledge-gaps/' + self.gaps[1]['object']['id'], self.owner)['scientific_accounts']['count'], 0)

    def test_invalid_supplied_credentials_never_downgrade_to_public_and_readers_do_not_lock(self):
        paths = ['/v1/knowledge-gaps', self.search_path, '/v1/knowledge-gaps/' + self.gaps[0]['object']['id'],
            '/v1/knowledge-gaps/' + self.gaps[0]['object']['id'] + '/accounts']
        for path in paths:
            self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer invalid'}).status_code, 401)
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('No write mutex for discovery')):
            for path in paths: self.get(path, self.owner)
