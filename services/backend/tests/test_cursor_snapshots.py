"""Cursor snapshots bind what a page's order and content depend on, never by copying or hashing catalog payloads."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from reveal_backend import app as api
from reveal_backend.account_discovery import counted_gap, counts_by_gap, gap_snapshot
from reveal_backend.repository import canonical, digest
from reveal_backend.runtime_config import ROOT

FIXTURE = json.loads((ROOT / 'services/frontend/src/lib/fixtures/contract.json').read_text())


def gaps(count=3):
    result = []
    for index in range(count):
        gap = deepcopy(FIXTURE['gaps']['items'][0]); gap['object']['id'] = 'dapper:KnowledgeGap.%032x' % index
        gap['source']['source_id'] = 'dismech:%03d' % index
        gap['source_detail'] = {'source_file': 'kb/x.yaml', 'payload_sha256': '%064x' % index, 'raw': {'prompt': 'x' * 50_000}}
        result.append(gap)
    return result


def listed(items, observed='2026-10-07T00:00:00Z', counts=((), ())):
    from collections import Counter
    return [{**counted_gap(gap, (Counter(counts[0]), Counter(counts[1])), '', observed),
             'votes': {'upvotes': 0, 'downvotes': 0, 'score': 0, 'user_vote': None}} for gap in items]


class GapSnapshotTests(unittest.TestCase):
    def test_binds_order_membership_counts_votes_and_ranking_but_not_observation_time(self):
        base = listed(gaps()); before = deepcopy(base)
        snapshot = digest(gap_snapshot(base, 'import'))
        self.assertEqual(base, before, 'the snapshot never mutates the page')
        self.assertEqual(digest(gap_snapshot(listed(gaps(), observed='2026-10-07T00:00:05Z'), 'import')), snapshot)
        voted = deepcopy(base); voted[1]['votes'] = {'upvotes': 1, 'downvotes': 0, 'score': 1, 'user_vote': 1}
        mechanism = deepcopy(base); mechanism[2]['attachments'].append({'resolution': 'resolved', 'target': {'source': 'dismech',
            'source_id': 'dismech:m', 'dapper_id': 'dapper:Mechanism.' + 'm' * 32}})
        searched = [{'gap': gap, 'ranking': {'value': 1, 'metric': 'lexical_rank', 'rank': rank}} for rank, gap in enumerate(base, 1)]
        reranked = deepcopy(searched); reranked[0]['ranking']['value'] = 0.5
        variants = {'count': listed(gaps(), counts=([base[0]['object']['id']], ())), 'archived': listed(gaps(), counts=((), [base[0]['object']['id']])),
                    'vote': voted, 'order': base[::-1], 'membership': base[:2], 'mechanisms': mechanism,
                    'revision': deepcopy(base), 'corpus': base}
        variants['revision'][0]['source']['source_revision'] = 'f' * 64
        for name, items in variants.items():
            with self.subTest(name=name):
                self.assertNotEqual(digest(gap_snapshot(items, 'other-import' if name == 'corpus' else 'import')), snapshot)
        self.assertNotEqual(digest(gap_snapshot(searched, 'import')), digest(gap_snapshot(reranked, 'import')))
        self.assertNotEqual(digest(gap_snapshot(searched, 'import')), snapshot)

    def test_never_copies_or_hashes_catalog_payloads(self):
        items = listed(gaps(200))
        compact = canonical(gap_snapshot(items, 'import'))
        self.assertNotIn('x' * 100, compact)
        self.assertLess(len(compact), len(canonical(items)) / 50)
        with patch('reveal_backend.account_discovery.deepcopy', side_effect=AssertionError('copied a page')):
            gap_snapshot(items, 'import')


class MechanismSnapshotTests(unittest.TestCase):
    def setUp(self):
        class Catalog:
            loaded = True; dismech_import = 'dismech-import'; mapping_run = 'mapping'; reference_generation_id = 'generation'
            factors = {'a': 1, 'b': 2, 'c': 3}
            def load(self): pass
            def provenance(self, query, mode, semantic=False): return {'query': query, 'mode': mode, 'corpus_snapshot': 'generation'}
            def search_factors(self, query, mode, limit): return deepcopy(self.items)
        self.catalog = Catalog()
        self.catalog.items = [{'record': {'source': 'eaggl', 'source_id': 'factor:' + letter, 'source_revision': letter * 64,
                                          'object': {'id': 'dapper:Mechanism.' + letter * 32}},
                               'ranking': {'value': 1 / (60 + rank), 'metric': 'reciprocal_rank_fusion', 'rank': rank},
                               'retrieval': {'candidate_hits': [[{'id': 'hit-%d' % hit, 'score': hit / 1000} for hit in range(1000)]],
                                             'query_vector': [0.5] * 768}}
                              for rank, letter in enumerate('abc', 1)]
        for context in (patch.object(api, 'catalog', self.catalog), patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 40})):
            context.start(); self.addCleanup(context.stop)
        self.client = TestClient(api.app)

    def search(self, cursor=None):
        return self.client.get('/v1/mechanisms/search', params={'q': 'insulin', 'source': 'eaggl', 'limit': 1, **({'cursor': cursor} if cursor else {})})

    def test_cursor_binds_ranking_not_retrieval_provenance(self):
        first = self.search(); self.assertEqual(first.status_code, 200, first.text)
        cursor = first.json()['page']['next_cursor']
        self.assertEqual(first.json()['items'][0]['retrieval'], self.catalog.items[0]['retrieval'], 'responses keep their provenance')
        for item in self.catalog.items: item['retrieval']['candidate_hits'] = [[{'id': 'other', 'score': 0.1}]]
        second = self.search(cursor)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()['items'][0]['record']['source_id'], 'factor:b')
        self.assertEqual(second.json()['page']['snapshot_id'], first.json()['page']['snapshot_id'])
        self.catalog.items[1]['ranking']['value'] = 0.5
        self.assertEqual(self.search(cursor).status_code, 409)
        self.catalog.items[1]['ranking']['value'] = 1 / 62; self.catalog.reference_generation_id = 'next-generation'
        self.assertEqual(self.search(cursor).status_code, 409)


if __name__ == '__main__': unittest.main()
