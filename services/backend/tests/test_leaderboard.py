"""Public population, immutable attribution, exact scoring and matching records."""
from copy import deepcopy
from urllib.parse import quote, urlencode
import unittest

from reveal_backend import app as api, leaderboard
from reveal_backend.repository import digest, uid
import test_account_discovery as discovery


def scientific(kind, value): return 'dapper:' + kind + '.' + digest(value)[:32]


class LeaderboardTests(unittest.TestCase):
    setUp = discovery.AccountDiscoveryTests.setUp
    principal = discovery.AccountDiscoveryTests.principal
    headers = discovery.AccountDiscoveryTests.headers

    def publish_account(self, name, author=None, *, owner=None, gap='gap-one', identity=None,
                        direction='SUPPORTS', document=None, fixture=False, anonymous=False):
        owner = owner or self.owner; identity = identity or scientific('ScientificAccount', name)
        gap_id = scientific('KnowledgeGap', gap); claim = scientific('Claim', name); prop = scientific('Proposition', name)
        evidence = scientific('EvidenceItem', name); dataset = scientific('Dataset', 'shared-data')
        file = scientific('File', 'used-file'); unused = scientific('File', 'unused-file')
        account = {'id': identity, 'name': name, 'question': gap_id, 'component_claims': [claim]}
        graph = document or {'scientific_accounts': [account], 'knowledge_gaps': [{'id': gap_id, 'name': gap}],
            'claims': [{'id': claim, 'name': name + ' claim', 'proposition': prop, 'has_evidence': [evidence]}],
            'propositions': [{'id': prop, 'statement': 'Illustrative test proposition'}],
            'evidence_items': [{'id': evidence, 'target_proposition': prop, 'direction': direction, 'was_derived_from': [file]}],
            'datasets': [{'id': dataset, 'name': 'Shared dataset', 'has_file': [file, unused]}],
            'files': [{'id': file, 'name': 'Reached source'}, {'id': unused, 'name': 'Unvisited source'}]}
        account = next(node for node in graph['scientific_accounts'] if node['id'] == identity)
        gap_node = next(node for node in graph['knowledge_gaps'] if node['id'] == account['question'])
        attribution = None if author is None else {'user_id': author, 'principal_kind': 'anonymous' if anonymous else 'registered',
            'display_name': 'Researcher ' + author[:6], 'orcid': '0000-0000-0000-0001', 'orcid_authenticated': False,
            'observed_at': '2026-10-01T00:00:00Z', 'email': 'private@example.org'}
        summary = {'account': account, 'knowledge_gap': gap_node, 'attribution': attribution,
            'created_at': '2026-10-01T00:00:00Z', 'job_id': 'private-job-never-return', 'claim_count': len(account['component_claims'])}
        if fixture: summary['fixture_origin'] = {'kind': 'canonical_fixture'}
        snapshot_id = uid(); frozen = {'account_id': identity, 'summary': summary, 'document': graph}
        with self.repo.transaction() as tx:
            tx.put('publication_snapshot', snapshot_id, owner, frozen)
            tx.put('publication', digest([owner, identity]), owner, {'account_id': identity, 'visibility': 'public',
                'version': 1, 'published_at': '2026-10-01T00:00:00Z', 'snapshot_id': snapshot_id, 'summary': summary})
        return identity

    def exploration(self, name, author, *, gap='gap-three', kind='registered', fixture=False):
        identity = uid(); snapshot_id = uid()
        record = {'id': identity, 'outcome': 'insufficient_evidence', 'summary': name,
            'knowledge_gap': {'id': scientific('KnowledgeGap', gap), 'name': gap},
            'attribution': {'user_id': author, 'principal_kind': kind, 'display_name': 'Exploration researcher',
                'orcid': None, 'orcid_authenticated': False, 'observed_at': '2026-10-01T00:00:00Z'}}
        if fixture: record['test_origin'] = True
        with self.repo.transaction() as tx:
            tx.put('outcome_snapshot', snapshot_id, self.owner, {'record': record})
            tx.put('outcome_publication', identity, self.owner, {'visibility': 'public', 'snapshot_id': snapshot_id})
        return identity

    def ballot(self, account, voter, value):
        from reveal_backend import votes
        with self.repo.transaction() as tx:
            row = next(pub for pub in tx.list('publication') if pub['data']['account_id'] == account)
            votes.change(tx, 'account', account, row['data']['summary']['account']['question'], voter, value)

    def browse(self, **query):
        response = self.client.get('/v1/leaderboard?' + urlencode(query))
        self.assertEqual(response.status_code, 200, response.text)
        api.validate(response.json(), 'LeaderboardList')
        return response.json()

    def detail(self, view, identity, metric, **query):
        response = self.client.get('/v1/leaderboard/' + view + '/' + quote(identity, safe='') + '/records?' + urlencode({'metric': metric, **query}))
        self.assertEqual(response.status_code, 200, response.text)
        api.validate(response.json(), 'LeaderboardRecords')
        return response.json()

    def seed_demo(self):
        """Isolated SQLite browser fixture only; never called by application code."""
        third = self.principal(); voter = self.principal()
        a = self.publish_account('Lipid regulation', self.owner)
        b = self.publish_account('Endothelial signalling', self.owner, gap='gap-two', direction='DISPUTES')
        c = self.publish_account('Inflammatory response', self.other)
        self.exploration('Evidence search with explicit limitations', third)
        self.ballot(a, self.owner, 1); self.ballot(a, voter, 1); self.ballot(a, third, 1)
        self.ballot(b, voter, 1); self.ballot(c, voter, -1)
        return a, b, c, third, voter

    def test_private_rows_and_fixtures_do_not_create_live_contributions(self):
        self.accepted = discovery.AccountDiscoveryTests.accepted.__get__(self)
        self.accepted(self.owner, self.gaps[0], 'z')
        self.publish_account('Fixture', self.owner, fixture=True)
        self.exploration('Demo exploration', self.other, fixture=True)
        result = self.browse()
        self.assertEqual(result['items'], []); self.assertEqual(result['cohort_size'], 0)
        self.assertEqual(result['voting_participants'], 0)
        self.assertEqual(result['exclusions']['fixture_accounts'], 1)
        self.assertEqual(self.browse(view='accounts')['items'], [])
        self.assertEqual(self.browse(view='datasets')['items'], [])
        text = str(result)
        for secret in ('Current profile', 'private@example.org', self.owner, self.other, 'private-job'):
            self.assertNotIn(secret, text)

    def test_full_population_percentiles_own_votes_and_public_drilldown_totals(self):
        a, b, c, third, voter = self.seed_demo()
        result = self.browse(limit=1)
        self.assertEqual(result['cohort_size'], 3); self.assertEqual(result['voting_participants'], 3)
        first = result['items'][0]
        self.assertEqual(first['id'], leaderboard.contributor_key(self.owner))
        self.assertEqual(first['metrics']['account_count'], 2)
        self.assertEqual(first['metrics']['net_votes'], 3)
        self.assertAlmostEqual(first['metrics']['overall_score'], 83.3333333333)
        self.assertAlmostEqual(first['components']['votes'], 83.3333333333)
        detail = self.detail('researchers', first['id'], 'votes')
        self.assertEqual(sum(item['value'] for item in detail['items']), 3)
        for metric, key in [('accounts', 'account_count'), ('gaps', 'account_gap_count'), ('explored', 'explored_gap_count')]:
            self.assertEqual(self.detail('researchers', first['id'], metric)['total'], first['metrics'][key])
        second = self.browse(limit=1, cursor=result['page']['next_cursor'])['items'][0]
        self.assertEqual(second['metrics']['net_votes'], -1); self.assertEqual(second['components']['votes'], 0)
        accounts = self.browse(view='accounts', sort='votes')['items']
        self.assertEqual(accounts[0]['metrics']['net_votes'], 3)  # Account keeps its author's own vote.
        self.assertEqual(accounts[0]['attribution']['id'], first['id'])
        self.assertIsNone(accounts[0]['attribution']['orcid'])  # Merely claimed ORCID is not exposed as verified.
        self.assertEqual(self.browse(view='researchers', evidence='supporting')['evidence'], 'all')

    def test_sole_contributor_positive_components_fifty_and_zero_vote_zero(self):
        self.publish_account('Only account', self.owner)
        item = self.browse()['items'][0]
        self.assertEqual(item['components'], {'accounts': 50., 'votes': 0., 'gaps': 50.})
        self.assertEqual(item['metrics']['overall_score'], 30.)
        self.assertEqual(item['rank'], 1)

    def test_explorations_count_distinct_gaps_and_zero_scores_share_rank(self):
        self.exploration('First search', self.owner); self.exploration('Repeated search', self.owner)
        self.exploration('Other researcher', self.other)
        result = self.browse()
        self.assertEqual([item['rank'] for item in result['items']], [1, 1])
        self.assertTrue(all(item['metrics']['overall_score'] == 0 for item in result['items']))
        self.assertTrue(all(item['metrics']['explored_gap_count'] == 1 for item in result['items']))
        self.assertEqual(self.detail('researchers', result['items'][0]['id'], 'explored')['total'], 1)

    def test_copies_deduplicate_transfer_preserves_actor_and_conflicts_uncredit(self):
        account = self.publish_account('Copied account', self.owner)
        self.publish_account('Copied account', self.owner, owner=self.other)
        self.assertEqual(self.browse()['items'][0]['metrics']['account_count'], 1)
        self.assertEqual(len(self.browse(view='accounts')['items']), 1)
        self.assertEqual(self.browse(view='datasets')['items'][0]['metrics']['account_count'], 1)
        self.publish_account('Copied account', self.other, owner=self.other)
        result = self.browse()
        self.assertEqual(result['items'], [])
        self.assertEqual(result['exclusions']['conflicting_accounts'], 1)
        self.assertIsNone(self.browse(view='accounts')['items'][0]['attribution'])
        self.assertEqual(self.browse(view='datasets')['items'][0]['metrics']['researcher_count'], 0)
        with self.repo.transaction() as tx:
            copy = tx.get('publication', digest([self.other, account]))['data']; copy['visibility'] = 'private'
            tx.put('publication', digest([self.other, account]), self.other, copy)
        self.assertEqual(self.browse()['items'][0]['id'], leaderboard.contributor_key(self.owner))

    def test_anonymous_missing_and_fixture_copies_never_earn_researcher_credit(self):
        a = self.publish_account('Anonymous account', self.owner, anonymous=True)
        self.publish_account('Missing actor')
        self.assertEqual(self.browse()['items'], [])
        self.assertEqual(len(self.browse(view='accounts')['items']), 2)
        self.publish_account('Anonymous account', self.owner, owner=self.other, fixture=True)
        self.assertNotIn(a, [item['id'] for item in self.browse(view='accounts')['items']])

    def test_dataset_dedup_filter_directions_and_only_reached_files(self):
        a = self.publish_account('Support account', self.owner)
        b = self.publish_account('Dispute account', self.other, direction='DISPUTES', gap='gap-two')
        self.publish_account('Support account', self.owner, owner=self.other)
        item = self.browse(view='datasets')['items'][0]
        self.assertEqual(item['metrics']['account_count'], 2); self.assertEqual(item['metrics']['claim_count'], 2)
        self.assertEqual(item['metrics']['account_gap_count'], 2); self.assertEqual(item['metrics']['researcher_count'], 2)
        self.assertEqual(item['directions']['SUPPORTS'], 1); self.assertEqual(item['directions']['DISPUTES'], 1)
        for metric, key in [('accounts', 'account_count'), ('claims', 'claim_count'), ('gaps', 'account_gap_count'), ('researchers', 'researcher_count')]:
            detail = self.detail('datasets', item['id'], metric)
            self.assertEqual(detail['total'], item['metrics'][key])
            self.assertTrue(all(record['evidence_ids'] for record in detail['items']))
        files = self.detail('datasets', item['id'], 'files')['items']
        self.assertEqual(len(files), 1); self.assertEqual(files[0]['label'], 'Reached source')
        self.assertTrue(files[0]['url'].startswith('/id/'))
        supporting = self.browse(view='datasets', evidence='supporting')['items'][0]
        self.assertEqual(supporting['metrics']['account_count'], 1); self.assertEqual(supporting['directions']['DISPUTES'], 0)
        self.assertEqual(self.detail('datasets', item['id'], 'accounts', evidence='supporting')['items'][0]['id'], a)

    def test_ties_keep_shared_rank_and_stable_page_order(self):
        for name in ('Beta', 'Alpha', 'Gamma'): self.publish_account(name)
        first = self.browse(view='accounts', limit=1)
        second = self.browse(view='accounts', limit=1, cursor=first['page']['next_cursor'])
        self.assertEqual(first['items'][0]['rank'], 1); self.assertEqual(second['items'][0]['rank'], 1)
        self.assertLess(first['items'][0]['id'], second['items'][0]['id'])

    def test_shared_claims_direction_overlap_and_uncredited_dataset_adoption(self):
        first = self.publish_account('Shared claim origin', self.owner)
        with self.repo.transaction() as tx:
            pub = tx.get('publication', digest([self.owner, first]))['data']
            graph = deepcopy(tx.get('publication_snapshot', pub['snapshot_id'])['data']['document'])
        other = scientific('ScientificAccount', 'second use')
        graph['scientific_accounts'][0].update(id=other, name='Second use of same claim')
        opposite = deepcopy(graph['evidence_items'][0]); opposite.update(id=scientific('EvidenceItem', 'opposite'), direction='DISPUTES')
        graph['claims'][0]['has_evidence'].append(opposite['id']); graph['evidence_items'].append(opposite)
        self.publish_account('Second use', identity=other, document=graph)
        item = self.browse(view='datasets')['items'][0]
        self.assertEqual(item['metrics']['account_count'], 2)
        self.assertEqual(item['metrics']['claim_count'], 1)
        self.assertEqual(item['metrics']['researcher_count'], 1)
        self.assertEqual(item['directions']['SUPPORTS'], 1); self.assertEqual(item['directions']['DISPUTES'], 1)
        detail = self.detail('datasets', item['id'], 'claims')
        self.assertEqual(detail['total'], 1)
        self.assertEqual(set(detail['items'][0]['account_ids']), {first, other})
        supporting = self.browse(view='datasets', evidence='supporting')['items'][0]
        self.assertEqual(supporting['metrics']['claim_count'], 1)
        self.assertEqual(supporting['metrics']['account_count'], 2)

    def test_invalid_snapshot_owner_root_and_missing_evidence_never_infer_eligibility(self):
        first = self.publish_account('Missing evidence', self.owner)
        with self.repo.transaction() as tx:
            pub = tx.get('publication', digest([self.owner, first]))['data']
            frozen = tx.get('publication_snapshot', pub['snapshot_id'])['data']
            frozen['document']['propositions'] = []
            tx.put('publication_snapshot', pub['snapshot_id'], self.owner, frozen)
        self.assertEqual(self.browse(view='datasets')['items'], [])
        self.assertEqual(self.browse()['exclusions']['excluded_evidence_paths'], 1)
        with self.repo.transaction() as tx:
            tx.put('publication_snapshot', pub['snapshot_id'], self.other, frozen)
        self.assertEqual(self.browse(view='accounts')['items'], [])
        self.assertEqual(self.browse()['exclusions']['invalid_public_accounts'], 1)

    def test_cursor_invalidation_publication_vote_query_and_detail(self):
        a = self.publish_account('First', self.owner); b = self.publish_account('Second', self.other)
        initial = self.browse(view='accounts', limit=1)
        cursor = initial['page']['next_cursor']
        # Observation time changes between reads do not expire the same population.
        self.browse(view='accounts', limit=1, cursor=cursor)
        wrong = self.client.get('/v1/leaderboard?' + urlencode({'view': 'researchers', 'cursor': cursor}))
        self.assertEqual(wrong.status_code, 409)
        dataset = self.browse(view='datasets')['items'][0]['id']
        detail = self.detail('datasets', dataset, 'accounts', limit=1)
        self.ballot(a, self.other, 1)
        self.assertEqual(self.client.get('/v1/leaderboard?' + urlencode({'view': 'accounts', 'cursor': cursor})).status_code, 409)
        detail_url = '/v1/leaderboard/datasets/' + quote(dataset, safe='') + '/records?'
        self.assertEqual(self.client.get(detail_url + urlencode({'metric': 'accounts', 'cursor': detail['page']['next_cursor']})).status_code, 409)
        fresh = self.browse(view='accounts', limit=1)['page']['next_cursor']
        with self.repo.transaction() as tx:
            pub = tx.get('publication', digest([self.owner, a]))['data']; pub['visibility'] = 'private'
            tx.put('publication', digest([self.owner, a]), self.owner, pub)
        self.assertEqual(self.client.get('/v1/leaderboard?' + urlencode({'view': 'accounts', 'cursor': fresh})).status_code, 409)
        self.assertEqual(self.client.get('/v1/leaderboard/accounts/' + a + '/records').status_code, 404)

    def test_queries_auth_cache_and_public_display_do_not_leak_private_actor_ids(self):
        self.seed_demo()
        response = self.client.get('/v1/leaderboard')
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertIn('Authorization', response.headers['vary'])
        for secret in (self.owner, self.other, 'private@example.org', 'private-job', 'internal_id', 'Current profile'):
            self.assertNotIn(secret, response.text)
        for query in ({'view': 'other'}, {'view': 'accounts', 'sort': 'recent'}, {'view': 'datasets', 'sort': 'overall'}, {'evidence': 'inferred'}, {'limit': 0}):
            self.assertEqual(self.client.get('/v1/leaderboard?' + urlencode(query)).status_code, 422, query)
        invalid = self.client.get('/v1/leaderboard', headers={'Authorization': 'Bearer invalid'})
        self.assertEqual(invalid.status_code, 401)
        result = self.client.get('/v1/leaderboard/researchers/missing/records?metric=files')
        self.assertEqual(result.status_code, 422)
        for view in ('researchers', 'accounts', 'datasets'):
            result = self.client.get('/v1/leaderboard/' + view + '/missing/records')
            self.assertEqual(result.status_code, 404)


if __name__ == '__main__': unittest.main()
