"""Gap search returns exactly what the per-word SequenceMatcher loop returned, without running it for every word."""
from difflib import SequenceMatcher
import gzip
import json
import random
import unittest
from unittest.mock import patch

from reveal_backend import fuzzy
from reveal_backend.catalog import Catalog
from reveal_backend.runtime_config import ROOT

GAPS = ROOT / 'data/dismech-gaps/2026-09-24/knowledge-gaps.jsonl.gz'


def reference(gaps, query, limit=20, mode='fuzzy'):
    """Catalog.search_gaps before the accelerator, verbatim."""
    words = query.casefold().split()
    scored = []
    for gap in gaps.values():
        text = (gap['object']['text']+' '+gap['source']['disease_label']).casefold()
        score = sum(w in text for w in words)/max(1,len(words)) if words else 1
        if words and not score and mode=='fuzzy':
            score = max((SequenceMatcher(None, query.casefold(), word).ratio() for word in text.split()), default=0)
            if score < 0.7: score = 0
        if score: scored.append((score, gap))
    scored.sort(key=lambda r: (-r[0], r[1]['source']['source_id']))
    return [{'gap': g, 'ranking': {'value': s, 'metric': 'fuzzy_similarity' if mode=='fuzzy' else 'lexical_rank', 'rank': i+1}} for i,(s,g) in enumerate(scored[:limit])]


def typo(rng, word):
    if len(word) < 3: return word
    i = rng.randrange(len(word)); kind = rng.randrange(4)
    if kind == 0: return word[:i] + word[i+1:]
    if kind == 1: return word[:i] + rng.choice('aeiounrst') + word[i+1:]
    if kind == 2 and i < len(word) - 1: return word[:i] + word[i+1] + word[i] + word[i+2:]
    return word[:i] + rng.choice('aeiou') + word[i:]


class GapSearchParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [json.loads(line) for line in gzip.open(GAPS, 'rt')]
        rng = random.Random(33)
        rows = rng.sample(rows, 250)
        cls.gaps = {}
        for index, row in enumerate(rows):
            raw = row['raw'] if isinstance(row['raw'], dict) else {}
            cls.gaps['dapper:KnowledgeGap.%032x' % index] = {'object': {'text': raw.get('prompt') or row['discussion_id'], 'id': 'dapper:KnowledgeGap.%032x' % index},
                'source': {'source_id': row['id'], 'disease_label': row['document_name']}}
        vocabulary = sorted({word for gap in cls.gaps.values() for word in gap['object']['text'].split()})
        cls.queries = ['insulin', 'insulin resistance', 'insulni', 'fibrsis', 'mitochondrial dysfunction', 'mitochondrail',
                       'zebrafish', 'autophagy lysosome', 'ins', 'in', 'a', '', '   ', ' insulin ', 'ß', 'İstanbul', 'STRASSE',
                       'Ünïcödé', 'x' * 250, 'insulin ' * 30, 'coronary artery disease', 'transcription RNA', '🧬 gene']
        cls.queries += [' '.join(typo(rng, rng.choice(vocabulary)) for _ in range(rng.randint(1, 3))) for _ in range(70)]

    def catalog(self):
        catalog = Catalog(); catalog.loaded = True; catalog.gaps = dict(self.gaps)
        return catalog

    def test_results_and_scores_are_identical_to_the_reference_loop(self):
        catalog = self.catalog()
        for mode in ('fuzzy', 'lexical'):
            for query in self.queries:
                with self.subTest(mode=mode, query=query[:40]):
                    expected = reference(catalog.gaps, query, len(catalog.gaps), mode)
                    actual = catalog.search_gaps(query, len(catalog.gaps), mode)
                    self.assertEqual([(x['ranking'], x['gap']['source']['source_id']) for x in actual],
                                     [(x['ranking'], x['gap']['source']['source_id']) for x in expected])
                    self.assertTrue(all(a['gap'] is e['gap'] for a, e in zip(actual, expected)))

    def test_ratio_matches_sequence_matcher_for_every_query_and_word(self):
        words = sorted({word for gap in self.gaps.values() for word in gap['object']['text'].casefold().split()})[:2000]
        index = fuzzy.FuzzyWords([' '.join(words)])
        for query in self.queries[:40]:
            score = index.scorer(query)(0)
            best = max((SequenceMatcher(None, query.casefold(), word).ratio() for word in words), default=0)
            self.assertEqual(score, best if best >= 0.7 else 0, query)

    def test_matcher_runs_only_for_words_that_can_reach_the_threshold(self):
        catalog = self.catalog(); calls = []; original = SequenceMatcher.ratio
        def counted(matcher): calls.append(1); return original(matcher)
        with patch.object(SequenceMatcher, 'ratio', counted): catalog.search_gaps('mitochondrial dysfunction', 20)
        words = {word for gap in self.gaps.values() for word in (gap['object']['text']+' '+gap['source']['disease_label']).casefold().split()}
        self.assertLess(len(calls), len(words) / 20)   # the loop it replaces scored every word of every gap

    def test_index_follows_the_served_gaps(self):
        catalog = self.catalog(); catalog.search_gaps('insulni', 50)
        built = catalog.gap_fuzzy; self.assertIsNotNone(built)
        self.assertIs(catalog.search_gaps('fibrsis', 50) and catalog.gap_fuzzy, built)   # reused across queries
        gap = next(iter(catalog.gaps.values()))
        catalog.gaps[gap['object']['id']] = dict(gap, object=dict(gap['object'], text='insulni pathway'))
        self.assertEqual(catalog.search_gaps('insulni', 50)[0]['gap']['object']['text'], 'insulni pathway')
        self.assertIsNot(catalog.gap_fuzzy, built)
        catalog.gaps = {}
        self.assertEqual(catalog.search_gaps('insulni', 50), [])


if __name__ == '__main__': unittest.main()
