"""Public discovery searches frozen publication summaries, never workspace edits."""
from copy import deepcopy
import unittest
from urllib.parse import urlencode

from reveal_backend import app as api
from reveal_backend.repository import digest, uid
import test_publication as published


class PublicAccountDiscoveryTests(unittest.TestCase):
    setUp = published.PublicationTests.setUp
    principal = published.PublicationTests.principal
    headers = published.PublicationTests.headers
    seed = published.PublicationTests.seed
    request = published.PublicationTests.request
    publish = published.PublicationTests.publish

    def browse(self, owner=None, **query):
        return self.request('get', '/v1/accounts?' + urlencode({'scope': 'public', **query}), owner)

    def clone_public_summary(self, letter, name, date, score=0):
        """Model additional already-frozen publications for collection tests."""
        identity = 'dapper:ScientificAccount.' + letter * 32
        with self.repo.transaction() as tx:
            row = deepcopy(tx.get('publication', digest([self.owner, self.account_id]))['data'])
            row.update(account_id=identity, published_at=date, updated_at=date)
            row['summary']['account'].update(id=identity, name=name)
            tx.put('publication', digest([self.owner, identity]), self.owner, row)
            tx.put('vote_total', digest(['account', identity]), 'system', {
                'target_kind': 'account', 'target_id': identity, 'gap_id': self.gap['object']['id'],
                'upvotes': max(0, score), 'downvotes': max(0, -score)})
        return identity

    def test_public_is_explicit_and_never_falls_back_to_owner_or_private_edits(self):
        self.assertEqual(self.client.get('/v1/accounts').status_code, 401)
        self.assertEqual(len(self.request('get', '/v1/accounts', self.owner).json()['items']), 1)
        for owner in (None, self.owner, self.other):
            self.assertEqual(self.browse(owner).json()['items'], [])
        self.assertEqual(self.publish().status_code, 200)
        # Later workspace edits do not become searchable or visible publicly.
        with self.repo.transaction() as tx:
            key = digest([self.owner, self.account_id]); row = tx.get('account_membership', key)['data']
            row['summary']['account']['name'] = 'Unpublished secret title'
            row['summary']['account']['closing_remarks'] = 'Confidential new conclusion'
            row['summary']['research_statement']['job_id'] = uid()
            tx.put('account_membership', key, self.owner, row)
        for owner in (None, self.owner, self.other):
            result = self.browse(owner)
            self.assertEqual(result.status_code, 200, result.text)
            api.validate(result.json(), 'AccountList')
            item = result.json()['items'][0]
            self.assertNotIn('secret', result.text)
            self.assertNotIn('Confidential', result.text)
            self.assertIsNone(item['job_id']); self.assertIsNone(item['research_statement']['job_id'])
            self.assertEqual(item['publication']['visibility'], 'public')
            self.assertFalse(item['publication']['can_manage'])
            self.assertEqual(self.browse(owner, q='unpublished secret').json()['items'], [])
        invalid = self.client.get('/v1/accounts?scope=public', headers={'Authorization': 'Bearer invalid'})
        self.assertEqual(invalid.status_code, 401)

    def test_search_before_pagination_and_actual_votes_or_publication_recency(self):
        self.assertEqual(self.publish().status_code, 200)
        older = self.clone_public_summary('b', 'Rare target alpha', '2020-01-01T00:00:00Z', 4)
        newer = self.clone_public_summary('c', 'Other beta', '2030-01-01T00:00:00Z', 1)
        tied = self.clone_public_summary('d', 'Other gamma', '2030-01-01T00:00:00Z', 1)
        recent = self.browse(sort='recent', limit=1).json()
        self.assertEqual(recent['items'][0]['account']['id'], newer)
        votes = self.browse(sort='votes', limit=1).json()
        self.assertEqual(votes['items'][0]['account']['id'], older)
        matched = self.browse(q=' RARE   ALPHA ', sort='recent', limit=1).json()
        self.assertEqual(matched['items'][0]['account']['id'], older)
        self.assertFalse(matched['page']['has_more'])
        second = self.browse(sort='recent', limit=1, cursor=recent['page']['next_cursor']).json()
        self.assertEqual(second['items'][0]['account']['id'], tied)
        self.assertEqual(self.browse(q='%_').json()['items'], [])
        # Independently publishing the same account must not duplicate it.
        self.seed(self.other); self.assertEqual(self.publish(self.other).status_code, 200)
        self.assertEqual(len(self.browse().json()['items']), 4)

    def test_cursors_bind_scope_query_sort_viewer_votes_and_publication(self):
        self.assertEqual(self.publish().status_code, 200)
        other = self.clone_public_summary('b', 'Second published account', '2020-01-01T00:00:00Z')
        cursor = self.browse(self.owner, limit=1, sort='votes').json()['page']['next_cursor']
        for owner, changes in ((self.other, {}), (None, {}), (self.owner, {'scope': 'workspace'}),
                               (self.owner, {'q': 'account'}), (self.owner, {'sort': 'recent'})):
            result = self.browse(owner, **{'limit': 1, 'sort': 'votes', 'cursor': cursor, **changes})
            self.assertEqual(result.status_code, 409, result.text)
        with self.repo.transaction() as tx:
            key = digest(['account', other]); row = tx.get('vote_total', key)['data']
            row['upvotes'] = 2; tx.put('vote_total', key, 'system', row)
        self.assertEqual(self.browse(self.owner, limit=1, sort='votes', cursor=cursor).status_code, 409)
        fresh = self.browse(self.owner, limit=1).json()['page']['next_cursor']
        self.assertEqual(self.publish(visibility='private', version=1).status_code, 200)
        self.assertEqual(self.browse(self.owner, limit=1, cursor=fresh).status_code, 409)
        self.assertNotIn(self.account_id, [item['account']['id'] for item in self.browse().json()['items']])
        for query in ({'scope': 'all'}, {'sort': 'confidence'}, {'q': 'x' * 201}):
            self.assertEqual(self.browse(**query).status_code, 422)


if __name__ == '__main__': unittest.main()
