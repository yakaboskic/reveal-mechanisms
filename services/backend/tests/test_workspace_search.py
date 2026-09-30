"""Workspace search covers all saved summaries and binds pagination to its query."""
from copy import deepcopy
import unittest
from urllib.parse import urlencode

from reveal_backend import app as api, publication
from reveal_backend.repository import digest, uid
import test_account_discovery as discovery


class WorkspaceSearchTests(unittest.TestCase):
    setUp = discovery.AccountDiscoveryTests.setUp
    principal = discovery.AccountDiscoveryTests.principal
    headers = discovery.AccountDiscoveryTests.headers
    accepted = discovery.AccountDiscoveryTests.accepted

    def request(self, route, owner=None, **query):
        return self.client.get(route + '?' + urlencode(query), headers=self.headers(owner or self.owner))

    def account(self, letter, title, *, owner=None, date='2026-09-28T10:00:00Z', gap=None):
        owner = owner or self.owner
        item = self.accepted(owner, gap or self.gaps[0], letter, date=date)
        item['account']['name'] = title
        item['account']['closing_remarks'] = 'Distinctive saved synthesis'
        with self.repo.transaction() as tx:
            tx.put('account_membership', digest([owner, item['account']['id']]), owner,
                   {'account_id': item['account']['id'], 'summary': item})
        return item

    def outcome(self, summary, *, owner=None, date='2026-09-28T10:00:00Z'):
        owner = owner or self.owner
        item = deepcopy(api.CONTRACT['paths']['/v1/analysis-outcomes']['get']['responses']['200']
                        ['content']['application/json']['examples']['saved']['value']['items'][0])
        item.update(id=uid(), summary=summary, created_at=date)
        item.pop('publication')
        item['anchors'][0].update(name='Stored mechanism', trait='Rare metabolic trait')
        with self.repo.transaction() as tx: tx.put('outcome_summary', item['id'], owner, item)
        return item

    def test_account_search_reaches_older_pages_and_is_literal_and_owner_scoped(self):
        target = self.account('d', 'Straße account', date='2020-01-01T00:00:00Z')
        for letter in 'efg': self.account(letter, 'Recent unrelated account')
        self.account('h', 'Straße account', owner=self.other)
        first = self.request('/v1/accounts', limit=2).json()
        self.assertNotIn(target['account']['id'], [item['account']['id'] for item in first['items']])
        for query in (' STRASSE  synthesis ', target['account']['id'], 'Original proposer'):
            response = self.request('/v1/accounts', q=query)
            self.assertEqual(response.status_code, 200, response.text)
            api.validate(response.json(), 'AccountList')
            identities = [item['account']['id'] for item in response.json()['items']]
            self.assertIn(target['account']['id'], identities)
            self.assertNotIn('dapper:ScientificAccount.'+'h'*32, identities)
        self.assertEqual(self.request('/v1/accounts', q='%_').json()['items'], [])
        self.assertEqual(self.request('/v1/accounts', q='Straße', gap_id=self.gaps[1]['object']['id']).json()['items'], [])

    def test_account_cursor_binds_normalized_query_even_when_both_queries_match(self):
        for letter in 'de': self.account(letter, 'Alpha beta')
        first = self.request('/v1/accounts', q=' ALPHA ', limit=1).json()
        cursor = first['page']['next_cursor']
        second = self.request('/v1/accounts', q='alpha', limit=1, cursor=cursor)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first['items'][0]['account']['id'], second.json()['items'][0]['account']['id'])
        for query in ('beta', ''):
            self.assertEqual(self.request('/v1/accounts', q=query, cursor=cursor).status_code, 409)
        self.assertEqual(self.request('/v1/accounts', owner=self.other, q='alpha', cursor=cursor).status_code, 409)

    def test_account_publication_is_current_owner_scoped_and_invalidates_old_pages(self):
        first = self.account('d', 'Shared title')
        second = self.account('e', 'Shared title')
        initial = self.request('/v1/accounts', limit=1).json()
        self.assertEqual(initial['items'][0]['publication']['visibility'], 'private')
        account_id = first['account']['id']
        record = {'visibility':'public','version':1,'published_at':'2026-09-30T00:00:00Z',
                  'updated_at':'2026-09-30T00:00:00Z','paragraph_id':None,'snapshot_id':'frozen',
                  'account_id':account_id,'summary':dict(first,job_id=None)}
        with self.repo.transaction() as tx:
            tx.put('publication', digest([self.other, second['account']['id']]), self.other, record)
            tx.put('publication', digest([self.owner, account_id]), self.owner, record)
        items = self.request('/v1/accounts').json()['items']
        controls = {item['account']['id']:item['publication'] for item in items}
        self.assertEqual(controls[account_id]['visibility'], 'public')
        self.assertTrue(controls[account_id]['can_manage'])
        self.assertEqual(controls[second['account']['id']]['visibility'], 'private')
        self.assertEqual(self.request('/v1/accounts', cursor=initial['page']['next_cursor']).status_code, 409)
        with self.repo.read_transaction() as tx:
            public = publication.public_accounts(tx)
        self.assertTrue(all(item['publication']['visibility']=='public' and not item['publication']['can_manage'] for item in public))
        with self.repo.transaction() as tx:
            key = digest([self.owner, account_id])
            member = tx.get('account_membership', key)['data']
            member['summary']['research_statement']['paragraph_id'] = 'new-private-paragraph'
            tx.put('account_membership', key, self.owner, member)
        changed = self.request('/v1/accounts', q=account_id).json()['items'][0]
        self.assertTrue(changed['publication']['has_unpublished_changes'])
        with self.repo.transaction() as tx:
            record.update(visibility='private', version=2)
            tx.put('publication', key, self.owner, record)
        unpublished = self.request('/v1/accounts', q=account_id).json()['items'][0]
        self.assertEqual(unpublished['publication']['visibility'], 'private')
        self.assertEqual(unpublished['publication']['version'], 2)
        self.assertFalse(unpublished['publication']['has_unpublished_changes'])

    def test_outcome_search_reaches_old_anchor_and_gap_summaries_without_private_details(self):
        old = self.outcome('Unusual captured exploration', date='2020-01-01T00:00:00Z')
        for index in range(3): self.outcome('Unrelated recent exploration '+str(index))
        foreign = self.outcome('Unusual captured exploration', owner=self.other)
        first = self.request('/v1/analysis-outcomes', limit=2).json()
        self.assertNotIn(old['id'], [item['id'] for item in first['items']])
        for query in ('UNUSUAL   metabolic', old['id'], old['knowledge_gap']['id']):
            response = self.request('/v1/analysis-outcomes', q=query)
            self.assertEqual(response.status_code, 200, response.text)
            api.validate(response.json(), 'AnalysisOutcomeList')
            items = response.json()['items']
            self.assertIn(old['id'], [item['id'] for item in items])
            self.assertNotIn(foreign['id'], [item['id'] for item in items])
            self.assertTrue(all('provenance' not in item and 'job_id' not in item for item in items))
            self.assertTrue(all(item['publication']['visibility']=='private' for item in items))

    def test_outcome_pagination_query_and_publication_changes_are_fenced(self):
        first = self.outcome('Alpha beta'); self.outcome('Alpha beta')
        page = self.request('/v1/analysis-outcomes', q='alpha', limit=1).json()
        cursor = page['page']['next_cursor']
        self.assertEqual(self.request('/v1/analysis-outcomes', q=' ALPHA ', cursor=cursor).status_code, 200)
        self.assertEqual(self.request('/v1/analysis-outcomes', q='beta', cursor=cursor).status_code, 409)
        with self.repo.transaction() as tx:
            tx.put('outcome_publication', first['id'], self.owner,
                {'visibility':'public','version':1,'published_at':'2026-09-30T00:00:00Z','updated_at':'2026-09-30T00:00:00Z'})
        self.assertEqual(self.request('/v1/analysis-outcomes', q='alpha', cursor=cursor).status_code, 409)
        updated = self.request('/v1/analysis-outcomes', q='alpha').json()['items']
        self.assertEqual(next(item for item in updated if item['id']==first['id'])['publication']['visibility'], 'public')

    def test_search_is_bounded_and_requires_current_identity(self):
        for route in ('/v1/accounts','/v1/analysis-outcomes'):
            self.assertEqual(self.client.get(route+'?q=anything').status_code, 401)
            self.assertEqual(self.request(route, q='a'*201).status_code, 422)
            self.assertEqual(self.request(route, q='a'*200).status_code, 200)
