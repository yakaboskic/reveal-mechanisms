"""Locating a quoted passage in a paper the agent read: exact byte spans in text rebuilt from the captured upstream
document, only inside what was served, one paragraph, unambiguous. Offline fixtures; no model or source calls."""
import hashlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from reveal_backend import literature_locator as locator
from reveal_backend.box_literature import LiteratureClient
from reveal_backend.box_mcp import Ledger, ScopedTools

RESULT = 'Hepatocyte-specific deletion of Lepr reduced insulin-stimulated AKT phosphorylation by 42% in liver (β-actin normalised).'
ARTICLE = ('''<?xml version="1.0"?><article><front><article-meta><article-id pub-id-type="pmc">12345</article-id>
<article-id pub-id-type="pmid">99999</article-id><article-id pub-id-type="doi">10.1/fixture</article-id>
<title-group><article-title>Leptin receptor loss and hepatic insulin signalling</article-title></title-group></article-meta></front>
<body><sec><title>Introduction</title><p>Leptin acts on the liver — directly and through the brain. Glucose production rose.</p></sec>
<sec><title>Results</title><p>''' + RESULT + '''</p>
<p>Glucose production rose. It was not suppressed during the clamp in <italic>knockout</italic> mice, unlike controls.</p>
<p>Mice were housed at 22 °C.</p></sec></body></article>''').encode()
ABSTRACT = 'We show that <i>Lepr</i> deletion in hepatocytes impairs insulin action in mice fed a high-fat diet for eight weeks.'


class Response(io.BytesIO):
    def __init__(self, raw, status=200):
        super().__init__(raw); self.status = status


class HTTP:
    def __init__(self, raw): self.raw = raw
    def open(self, request, timeout): return Response(self.raw)


def abstract_response(text=ABSTRACT):
    return json.dumps({'hitCount': 1, 'resultList': {'result': [dict(
        id='99999', source='MED', pmid='99999', pmcid='PMC12345', doi='10.1/fixture', title='Leptin receptor loss and hepatic insulin signalling.',
        isOpenAccess='Y', abstractText=text)]}}, ensure_ascii=False).encode()


class LocatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.http = HTTP(ARTICLE)
        self.ledger = Ledger(self.root, 'locator-job', 1)
        self.tools = ScopedTools((), self.ledger, soft_budget=True, max_calls=40, max_searches=3, max_papers=5, max_characters=100_000,
                                 literature=LiteratureClient(opener=self.http, guidance=True))

    def read(self, **arguments):
        result = self.tools.call('read_paper', {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text', **arguments})
        self.assertFalse(result.get('isError'), result)
        return result

    def papers(self):
        return locator.paper_texts(self.root, self.ledger.entries)

    def test_a_quote_is_located_by_exact_bytes_in_the_rebuilt_text(self):
        served = self.read()
        paper = self.papers()[('PMC', 'PMC12345', 'full_text')]
        self.assertEqual(paper['text'], served['structuredContent']['data']['text'])   # byte for byte what the agent saw
        self.assertEqual(paper['served'], [(0, len(paper['text']))])
        span = locator.locate(paper, RESULT)
        data = locator.text_bytes(paper)
        # The DAPPER contract: file[byte_start : byte_start + byte_length] decodes to value_text.
        self.assertEqual(data[span['byte_start']:span['byte_start'] + span['byte_length']].decode('utf-8'), span['value_text'])
        self.assertEqual(span['value_text'], RESULT)
        # Multi-byte characters before and inside the quote: bytes and characters differ.
        self.assertGreater(span['byte_start'], span['character_start']); self.assertGreater(span['byte_length'], len(RESULT))
        self.assertEqual(span['selector'], f"paragraph=2; section=Results; chars={span['character_start']}-{span['character_end']}")
        self.assertEqual(span['media_type'], 'text/plain')
        # Whitespace may differ from the source; the located text is the source's.
        loose = locator.locate(paper, '  not suppressed during the\n clamp in knockout   mice, unlike controls. ')
        self.assertEqual(loose['value_text'], 'not suppressed during the clamp in knockout mice, unlike controls.')
        self.assertTrue(loose['selector'].startswith('paragraph=3; section=Results'))
        self.assertEqual(locator.locate(paper, 'Leptin receptor loss and hepatic insulin signalling')['selector'].split(';')[0], 'heading')

    def test_quotes_that_cannot_be_trusted_are_refused_with_what_to_change(self):
        self.read()
        paper = self.papers()[('PMC', 'PMC12345', 'full_text')]
        for quote, fragment in (
                ('Hepatocyte-specific deletion of Lepr increased insulin-stimulated AKT phosphorylation', 'not verbatim text of this paper'),
                ('Glucose production rose.', 'occurs 2 times'),
                ('unlike controls. Mice were housed at 22 °C.', 'across two paragraphs'),
                ('42% in liver', 'too short to locate'),
                (RESULT + ' ' + 'and more ' * 60, 'too long'),
                ('', 'needs a snippet'), (None, 'needs a snippet'), ('   \n ', 'needs a snippet')):
            with self.assertRaises(locator.LocatorError) as refused:
                locator.locate(paper, quote)
            self.assertIn(fragment, str(refused.exception), quote)

    def test_only_text_the_agent_was_served_can_be_quoted(self):
        # A per-job character budget cut the read short: what was served comes from the response, not the request.
        tight_ledger = Ledger(self.root / 'tight', 'locator-job', 1)
        tight = ScopedTools((), tight_ledger, soft_budget=True, max_calls=10, max_searches=1, max_papers=2, max_characters=160,
                            literature=LiteratureClient(opener=self.http, guidance=True))
        first = tight.call('read_paper', {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text', 'limit': 12000})
        self.assertEqual(len(first['structuredContent']['data']['text']), 160)
        paper = locator.paper_texts(self.root / 'tight', tight_ledger.entries)[('PMC', 'PMC12345', 'full_text')]
        self.assertEqual(paper['served'], [(0, 160)])
        self.assertEqual(json.loads((self.root / 'tight' / tight_ledger.entries[0]['request']['path']).read_bytes())['arguments']['limit'], 12000)
        with self.assertRaises(locator.LocatorError) as unread:
            locator.locate(paper, RESULT)
        self.assertIn('you were not served', str(unread.exception))
        # Paging through the paper extends what may be quoted; adjacent windows merge, so a quote may span them.
        total = first['structuredContent']['data']['total_characters']
        for offset in range(0, total, 150): self.read(offset=offset, limit=150)
        paged = self.papers()[('PMC', 'PMC12345', 'full_text')]
        self.assertEqual(paged['served'], [(0, total)]); self.assertEqual(paged['sequences'], list(range(1, len(paged['sequences']) + 1)))
        self.assertGreater(len(paged['sequences']), 2)
        self.assertEqual(locator.locate(paged, RESULT)['value_text'], RESULT)
        # A quote that occurs twice in the paper is ambiguous whether or not the second occurrence was served yet: the
        # verdict must be the same at an early lint and after the run has read on.
        repeated = 'Glucose production rose.'
        with self.assertRaises(locator.LocatorError):
            locator.locate(paged, repeated)
        once_ledger = Ledger(self.root / 'once', 'locator-job', 1)
        once = ScopedTools((), once_ledger, soft_budget=True, max_calls=10, max_searches=1, max_papers=2, max_characters=10_000,
                           literature=LiteratureClient(opener=self.http, guidance=True))
        once.call('read_paper', {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text', 'limit': paged['text'].index(repeated) + len(repeated) + 3})
        partial = locator.paper_texts(self.root / 'once', once_ledger.entries)[('PMC', 'PMC12345', 'full_text')]
        with self.assertRaisesRegex(locator.LocatorError, 'occurs 2 times in this paper'):
            locator.locate(partial, repeated)
        self.assertEqual(locator.locate(partial, 'Leptin acts on the liver — directly and through the brain.')['selector'].split(';')[0], 'paragraph=1')

    def test_abstracts_keep_the_markup_the_agent_saw(self):
        self.http.raw = abstract_response()
        served = self.tools.call('read_paper', {'source': 'MED', 'id': '99999'})
        self.assertFalse(served.get('isError'), served)
        paper = self.papers()[('MED', '99999', 'abstract')]
        self.assertEqual((paper['kind'], paper['text']), ('abstract_html', ABSTRACT))
        span = locator.locate(paper, '<i>Lepr</i> deletion in hepatocytes impairs insulin action')
        self.assertEqual((span['media_type'], span['selector']), ('text/html', f"abstract; chars={span['character_start']}-{span['character_end']}"))
        with self.assertRaises(locator.LocatorError):
            locator.locate(paper, 'Lepr deletion in hepatocytes impairs insulin action')   # the tags are part of the text
        self.assertEqual(locator.publication(paper), {'title': 'Leptin receptor loss and hepatic insulin signalling.', 'pmid': '99999',
                                                      'doi': 'doi:10.1/fixture', 'citation': 'PMCID:PMC12345'})
        self.assertEqual(locator.reference(paper), 'PMID:99999')

    def test_file_records_identify_the_text_by_checksum_and_never_carry_it(self):
        capture = json.loads(self.read()['content'][-1]['text'])
        self.http.raw = abstract_response()
        self.tools.call('read_paper', {'source': 'MED', 'id': '99999'})
        papers = self.papers()
        paper = locator.paper_for_capture(papers, capture['file']['sha256'])
        self.assertEqual(paper['key'], ('PMC', 'PMC12345', 'full_text'))
        self.assertIsNone(locator.paper_for_capture(papers, '0' * 64))
        text = locator.text_file(paper)
        self.assertEqual(text['sha256'], hashlib.sha256(paper['text'].encode()).hexdigest())
        self.assertEqual((text['filename'], text['mime_type'], text['size_in_bytes']), ('PMC12345.fulltext.txt', 'text/plain', len(paper['text'].encode())))
        self.assertNotIn('location', text)
        self.assertIn(locator.EXTRACTION, text['description']); self.assertNotIn('Hepatocyte', json.dumps(text))
        upstream = locator.upstream_file(paper)
        self.assertEqual(upstream['sha256'], hashlib.sha256(ARTICLE).hexdigest())
        self.assertEqual((upstream['mime_type'], upstream['size_in_bytes']), ('application/xml', len(ARTICLE)))
        self.assertTrue(upstream['location'].endswith('/PMC12345/fullTextXML'))
        self.assertEqual(text['description'].count(upstream['sha256']), 1)
        self.assertEqual(locator.publication(paper), {'title': 'Leptin receptor loss and hepatic insulin signalling', 'pmid': '99999',
                                                      'doi': 'doi:10.1/fixture', 'citation': 'PMCID:PMC12345'})
        self.assertEqual(locator.text_file(papers[('MED', '99999', 'abstract')])['filename'], '99999.abstract.html')

    def test_failed_searched_truncated_and_tampered_reads_yield_no_text(self):
        self.tools.call('search_papers', {'query': 'leptin'})
        self.http.raw = b'<article><front><article-meta><article-id pub-id-type="pmc">777</article-id></article-meta></front></article>'
        self.assertTrue(self.tools.call('read_paper', {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text'})['isError'])
        self.assertEqual(self.papers(), {})
        self.http.raw = ARTICLE
        self.read()
        entry = self.ledger.entries[-1]
        # A capture that was cut at the byte limit cannot be rebuilt, so it yields nothing to quote.
        response = json.loads((self.root / entry['response']['path']).read_bytes())
        response['structuredContent']['upstream_capture_complete'] = False
        truncated = json.dumps(response).encode()
        path = self.root / 'truncated.json'; path.write_bytes(truncated)
        incomplete = {**entry, 'response': {'path': 'truncated.json', 'sha256': hashlib.sha256(truncated).hexdigest(), 'size_bytes': len(truncated)}}
        self.assertEqual(locator.paper_texts(self.root, [incomplete]), {})
        # Changed bytes under a recorded checksum are refused, as is a read that is not what its upstream yields.
        (self.root / entry['upstream_response']['path']).write_bytes(ARTICLE.replace(b'42%', b'24%'))
        with self.assertRaises(locator.LocatorError):
            self.papers()
        altered = ARTICLE.replace(b'42%', b'24%')
        forged = {**entry, 'upstream_response': {'path': 'forged.xml', 'sha256': hashlib.sha256(altered).hexdigest(), 'size_bytes': len(altered)}}
        (self.root / 'forged.xml').write_bytes(altered)
        # A read that is not what its upstream yields serves nothing, so nothing can be quoted from it; the rest of the
        # run is unaffected (a source may change between two reads of one paper).
        unusable = locator.paper_texts(self.root, [forged])
        self.assertIsNone(locator.paper_for_capture(unusable, forged['response']['sha256']))
        self.assertEqual([paper['served'] for paper in unusable.values()], [[]])

    def test_one_rule_decides_literature_evidence_for_the_draft_lint_and_the_deposit(self):
        self.read()
        papers, reads = self.papers(), locator.captures(self.ledger.entries)
        capture = self.ledger.entries[-1]['response']['sha256']
        files = {'urn:file:read': {'id': 'urn:file:read', 'sha256': capture}, 'urn:file:graph': {'id': 'urn:file:graph', 'sha256': 'f' * 64},
                 'urn:file:lost': {'id': 'urn:file:lost', 'sha256': 'e' * 64}}
        item = {'id': 'urn:draft:evidence-1', 'was_derived_from': ['urn:file:read'], 'snippet': RESULT,
                'target_proposition': 'urn:draft:proposition-1', 'explanation': 'Loss of the receptor lowers insulin signalling.'}
        paper, span = locator.evidence_quote(item, files, papers, reads)
        self.assertEqual((paper['key'], span['value_text']), (('PMC', 'PMC12345', 'full_text'), RESULT))
        # A File that is no capture of this run is not this rule's business (the pinned linter decides on it).
        self.assertIsNone(locator.evidence_quote({**item, 'was_derived_from': ['urn:file:graph']}, files, papers, reads))
        # A read whose text cannot be rebuilt (an empty read, a truncated capture) is refused, not skipped: skipped
        # here, the item would pass the drafting lint and reach the deposit with no place in any paper.
        with self.assertRaisesRegex(locator.LocatorError, 'returned no text REVEAL can rebuild'):
            locator.evidence_quote({**item, 'was_derived_from': ['urn:file:lost']}, files, papers, {**reads, 'e' * 64: 'read_paper'})
        with self.assertRaisesRegex(locator.LocatorError, 'cites the result of query_graph'):
            locator.evidence_quote({**item, 'was_derived_from': ['urn:file:graph']}, files, papers, {**reads, 'f' * 64: 'query_graph'})
        with self.assertRaisesRegex(locator.LocatorError, 'needs explanation'):
            locator.evidence_quote({**item, 'explanation': ' '}, files, papers, reads)
        with self.assertRaisesRegex(locator.LocatorError, 'not verbatim'):
            locator.evidence_quote({**item, 'snippet': 'Leptin receptor deletion abolished all hepatic signalling.'}, files, papers, reads)

    def test_a_quote_accepted_early_is_still_accepted_after_the_run_reads_on(self):
        """The lint sees the ledger as it is when called; the backend sees the finished one. The verdict must not turn."""
        self.read(limit=160)                                                    # the title and the first paragraph only
        early = self.papers()[('PMC', 'PMC12345', 'full_text')]
        unique = 'Leptin acts on the liver — directly and through the brain.'
        repeated = 'Glucose production rose.'                                   # again in the Results, not yet served
        self.assertEqual(locator.locate(early, unique)['value_text'], unique)
        with self.assertRaisesRegex(locator.LocatorError, 'occurs 2 times in this paper'): locator.locate(early, repeated)
        with self.assertRaisesRegex(locator.LocatorError, 'not served'): locator.locate(early, RESULT)
        self.read(offset=160)
        later = self.papers()[('PMC', 'PMC12345', 'full_text')]
        self.assertEqual(locator.locate(later, unique), locator.locate(early, unique))
        with self.assertRaisesRegex(locator.LocatorError, 'occurs 2 times in this paper'): locator.locate(later, repeated)
        self.assertEqual(locator.locate(later, RESULT)['value_text'], RESULT)   # refusals may lift; acceptances never fall

    def test_an_account_may_quote_only_a_small_part_of_one_paper_and_the_whole_document_is_judged_at_once(self):
        self.read()
        papers, captured = self.papers(), locator.captures(self.ledger.entries)
        capture = self.ledger.entries[-1]['response']['sha256']
        def document(snippets):
            return {'files': [{'id': 'urn:file:read', 'sha256': capture}],
                    'evidence_items': [{'id': f'urn:draft:e{index}', 'was_derived_from': ['urn:file:read'], 'snippet': snippet,
                                        'target_proposition': 'urn:draft:p', 'explanation': 'Reported directly.'}
                                       for index, snippet in enumerate(snippets, 1)]}
        located, problems = locator.document_quotes(document([RESULT, 'Mice were housed at 22 °C.', 'not in the paper at all, anywhere']), papers, captured)
        self.assertEqual([index for index, _, _ in located], [0, 1])
        self.assertEqual([where for where, _ in problems], ['urn:draft:e3']); self.assertIn('not verbatim', problems[0][1])
        with unittest.mock.patch.object(locator, 'MAX_PAPER_QUOTE_CHARACTERS', 130):
            _, over = locator.document_quotes(document([RESULT, 'Mice were housed at 22 °C.']), papers, captured)
            # The same passage quoted twice counts once.
            _, twice = locator.document_quotes(document([RESULT, RESULT]), papers, captured)
        self.assertEqual([where for where, _ in over], ['urn:draft:e2']); self.assertIn('the most is 130', over[0][1])
        self.assertEqual(twice, [])

    def test_a_short_text_such_as_an_abstract_cannot_be_quoted_whole(self):
        self.http.raw = abstract_response('A. ' + ABSTRACT + ' ' + 'B. ' + ABSTRACT.replace('hepatocytes', 'adipocytes') + ' C. '
                                          + ABSTRACT.replace('hepatocytes', 'myocytes') + ' D. ' + ABSTRACT.replace('hepatocytes', 'neurons'))
        served = self.tools.call('read_paper', {'source': 'MED', 'id': '99999'})
        papers, captured = self.papers(), locator.captures(self.ledger.entries)
        text = served['structuredContent']['data']['text']
        parts = [text[start:start + 115] for start in (0, 120, 240, 360)]
        def document(snippets):
            return {'files': [{'id': 'urn:file:read', 'sha256': self.ledger.entries[-1]['response']['sha256']}],
                    'evidence_items': [{'id': f'urn:draft:e{index}', 'was_derived_from': ['urn:file:read'], 'snippet': snippet,
                                        'target_proposition': 'urn:draft:p', 'explanation': 'Reported directly.'}
                                       for index, snippet in enumerate(snippets, 1)]}
        self.assertEqual(locator.document_quotes(document(parts[:1]), papers, captured)[1], [])   # one full quote is always allowed
        _, problems = locator.document_quotes(document(parts), papers, captured)
        self.assertEqual(len(problems), 1); self.assertIn(f'of one {len(text):,}-character text; the most is 400', problems[0][1])

    def test_a_passage_longer_in_the_paper_than_its_quote_is_still_capped(self):
        self.http.raw = ARTICLE.replace(b'reduced insulin-stimulated', b'reduced' + b' ' * 500 + b'insulin-stimulated')
        self.read()
        with self.assertRaisesRegex(locator.LocatorError, 'characters in the paper, over the 400 allowed'):
            locator.locate(self.papers()[('PMC', 'PMC12345', 'full_text')], RESULT)


if __name__ == '__main__':
    unittest.main()
