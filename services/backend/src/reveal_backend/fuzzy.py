"""Exact accelerator for the catalog's fuzzy word match.

For a document's casefolded search text, the fuzzy score is the best
SequenceMatcher(None, query, word).ratio() over its words, counted only at THRESHOLD or above. FuzzyWords
computes exactly that, bit for bit: each distinct word is scored at most once per query, and words whose
real_quick_ratio or quick_ratio bound (the same 2.0*matches/length expression difflib uses, so the bound is
exact in floating point) is below THRESHOLD never reach the quadratic matcher.
"""
from collections import Counter
from difflib import SequenceMatcher

THRESHOLD = 0.7


class FuzzyWords:
    def __init__(self, texts):
        self.texts = texts
        self.words = [tuple(dict.fromkeys(text.split())) for text in texts]
        self.counts = {}
        for words in self.words:
            for word in words:
                if word not in self.counts: self.counts[word] = tuple(Counter(word).items())

    def scorer(self, query):
        """score(i) for document i, identical to max(ratio(query, word)) set to 0 below THRESHOLD."""
        q = query.casefold(); lq = len(q); characters = Counter(q); memo = {}
        matcher = SequenceMatcher(None, q, '')   # seq1=query, seq2=word, as before: autojunk applies to the word
        def ratio(word):
            value = memo.get(word)
            if value is None:
                lw = len(word); length = lq + lw; value = 0
                if 2.0*min(lq, lw)/length >= THRESHOLD:   # real_quick_ratio()
                    common = 0
                    for character, count in self.counts[word]:
                        available = characters.get(character)
                        if available: common += count if count < available else available
                    if 2.0*common/length >= THRESHOLD:     # quick_ratio()
                        matcher.set_seq2(word); value = matcher.ratio()
                        if value < THRESHOLD: value = 0
                memo[word] = value
            return value
        return lambda index: max(map(ratio, self.words[index]), default=0)
