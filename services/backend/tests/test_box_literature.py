"""Offline HTTP fixtures exercise actual capture tools; no model or source calls."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from reveal_backend import box_remote
from reveal_backend.acceptance import ledger_sources, validate_new_files
from reveal_backend.agent_execution import ExecutionRequest
from reveal_backend.box_adapter import make_bundle
from reveal_backend.box_literature import BASE, LiteratureClient, LiteratureInputError, NoRedirect, request_spec
from reveal_backend.box_mcp import DraftValidationError, Ledger, PolicyError, ScopedTools
from reveal_backend.worker import validate_execution_ledger
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.public_tool_activity import display_arguments
from reveal_backend.research_outcome import FORMAT, validate_insufficient_outcome
from reveal_backend.runtime_config import ROOT


def canonical_text(ledger, index):
    return (ledger.root / ledger.entries[index]['response']['path']).read_text()


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


def paper_response(text='Observed β response in the studied tissue.', **fields):
    return encoded({'hitCount': 1, 'resultList': {'result': [dict(
        id='12345', source='MED', pmid='12345', pmcid='PMC12345', title='Fixture paper',
        abstractText=text, **fields)]}})


class Response(io.BytesIO):
    def __init__(self, raw, status=200):
        super().__init__(raw)
        self.status = status


class FixtureHTTP:
    def __init__(self, raw, status=200):
        self.raw, self.status, self.requests = raw, status, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return Response(self.raw, self.status)


class LiteratureTransportTests(unittest.TestCase):
    def test_fixed_origin_exact_ids_windows_and_no_redirect(self):
        query = 'β cell AND TITLE:"insulin"'
        spec = request_spec('search_papers', {'query': query, 'limit': 2})
        self.assertEqual(urlsplit(spec['url']).netloc, 'www.ebi.ac.uk')
        self.assertEqual(parse_qs(urlsplit(spec['url']).query)['query'], [query])
        bad = [('search_papers', {'query': 'valid', 'url': 'https://other.test'}),
               ('search_papers', {'query': 'a\nprivate'}), ('search_papers', {'query': 'valid', 'limit': True}),
               ('read_paper', {'source': 'PMC', 'id': '../../secrets'}),
               ('read_paper', {'source': 'MED', 'id': '123', 'section': 'full_text'}),
               ('read_paper', {'source': 'MED', 'id': '123', 'offset': -1}),
               ('read_paper', {'source': 'MED', 'id': '123', 'limit': 12001})]
        for tool, arguments in bad:
            with self.subTest(arguments=arguments), self.assertRaises(LiteratureInputError):
                request_spec(tool, arguments)
        with self.assertRaises(HTTPError):
            NoRedirect().redirect_request(Request(BASE), io.BytesIO(), 302, 'redirect', {}, 'https://other.test')

    def test_abstract_unicode_offsets_and_exact_raw_capture(self):
        text = 'β cell response; exact abstract text.'
        raw = paper_response(text, commentCorrectionList={'commentCorrection': [{'type': 'RetractionIn'}]})
        http = FixtureHTTP(raw)
        result = LiteratureClient(opener=http).call('read_paper', {'source': 'MED', 'id': '12345', 'offset': 2, 'limit': 9})
        view = result['structuredContent']
        self.assertFalse(result['isError'])
        self.assertEqual(view['data']['text'], text[2:11])
        self.assertEqual((view['scope'], view['data']['offset'], view['data']['next_offset']), ('abstract', 2, 11))
        self.assertEqual(view['data']['paper']['commentCorrectionList']['commentCorrection'][0]['type'], 'RetractionIn')
        self.assertEqual(view['data']['text_kind'], 'abstract_html')
        self.assertEqual(view['upstream_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(view['upstream_capture_complete'])
        self.assertEqual(result['_raw_response'], raw)
        request, timeout = http.requests[0]
        self.assertEqual(timeout, 20)
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)['query'], ['EXT_ID:12345 AND SRC:MED'])

    def test_search_is_discovery_only_and_read_requires_exact_returned_identity(self):
        raw = paper_response()
        search = LiteratureClient(opener=FixtureHTTP(raw)).call('search_papers', {'query': 'fixture'})
        self.assertEqual(search['structuredContent']['scope'], 'discovery')
        self.assertNotIn('abstractText', search['structuredContent']['data']['records'][0])
        wrong = LiteratureClient(opener=FixtureHTTP(raw)).call('read_paper', {'source': 'MED', 'id': '999'})
        self.assertTrue(wrong['isError'])
        self.assertEqual(wrong['_raw_response'], raw)
        self.assertEqual(wrong['structuredContent']['data'], {})

    def test_xml_standard_dtd_allowed_entity_expansion_rejected_and_id_checked(self):
        raw = b'''<?xml version="1.0"?><!DOCTYPE article SYSTEM "https://not-fetched.test/DTD">
          <article><front><article-meta><article-id pub-id-type="pmc">12345</article-id>
          <article-id pub-id-type="doi">10.1/fixture</article-id><title-group><article-title>Fixture title</article-title></title-group>
          </article-meta></front><body><sec><title>Results</title><p>Exact reported result.</p></sec></body></article>'''
        arguments = {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text', 'limit': 12}
        result = LiteratureClient(opener=FixtureHTTP(raw)).call('read_paper', arguments)
        self.assertFalse(result['isError'])
        self.assertEqual(result['_raw_response'], raw)
        view = result['structuredContent']
        self.assertEqual((view['scope'], view['data']['text_kind']), ('full_text', 'oa_xml_text'))
        self.assertEqual(view['data']['paper']['doi'], '10.1/fixture')
        self.assertEqual(view['data']['next_offset'], 12)
        for invalid in (raw.replace(b'12345', b'44444'), b'<!DOCTYPE article [<!ENTITY e "bad">]><article>&e;</article>'):
            bad = LiteratureClient(opener=FixtureHTTP(invalid)).call('read_paper', arguments)
            self.assertTrue(bad['isError'])
            self.assertEqual(bad['_raw_response'], invalid)

    def test_malformed_http_failures_and_size_limits_retain_diagnostic_bytes(self):
        cases = [(b'upstream unavailable', 429), (b'not found', 404), (b'not JSON', 200),
                 (encoded({'resultList': []}), 200), (encoded({'resultList': {'result': [42]}}), 200)]
        for raw, status in cases:
            with self.subTest(raw=raw):
                result = LiteratureClient(opener=FixtureHTTP(raw, status)).call('read_paper', {'source': 'MED', 'id': '12345'})
                self.assertTrue(result['isError'])
                self.assertEqual(result['_raw_response'], raw)
                self.assertEqual(result['structuredContent']['http_status'], status)
                self.assertNotIn('row_count', result['structuredContent'])
        with patch('reveal_backend.box_literature.MAX_RESPONSE_BYTES', 8):
            result = LiteratureClient(opener=FixtureHTTP(b'01234567890123')).call('read_paper', {'source': 'MED', 'id': '12345'})
        self.assertTrue(result['isError'])
        self.assertEqual(result['_raw_response'], b'012345678')
        self.assertFalse(result['structuredContent']['upstream_capture_complete'])


class CapturedPaperToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.http = FixtureHTTP(paper_response())
        self.ledger = Ledger(self.root, 'offline-job', 1)
        self.tools = ScopedTools((), self.ledger, literature=LiteratureClient(opener=self.http))

    def test_real_scoped_registration_independent_of_selected_graphs(self):
        self.assertEqual({x['name'] for x in self.tools.definitions()}, {'search_papers', 'read_paper'})
        self.assertEqual(ScopedTools((), Ledger(self.root / 'paragraph', 'paragraph', 1)).definitions(), [])
        denied = self.tools.call('query_graph', {'graph': 'prokn', 'subject': 'urn:exact'})
        self.assertTrue(denied['isError'])
        self.assertEqual(self.http.requests, [])
        self.assertEqual(self.ledger.entries[-1]['status'], 'denied')

    def test_fresh_bundle_contains_real_tool_and_outcome_validator_modules(self):
        package = self.root / 'paragraph-input.json'; package.write_bytes(encoded({'format': 'reveal.paragraph-input/1'}))
        request = ExecutionRequest(job_id='bundle-test', attempt=1, kind='paragraph',
                                   input_path=package, output_dir=self.root / 'output')
        with tarfile.open(fileobj=io.BytesIO(make_bundle(ROOT, request)), mode='r:gz') as bundle:
            for name in ('box_literature.py', 'research_outcome.py'):
                source = 'services/backend/src/reveal_backend/' + name
                self.assertEqual(bundle.extractfile('bundle/' + source).read(), (ROOT / source).read_bytes())
            self.assertFalse(any(name.endswith('/.env') for name in bundle.getnames()))

    def test_complete_ledger_and_exact_file_promotion_reject_search_and_tamper(self):
        search = self.tools.call('search_papers', {'query': 'fixture', 'limit': 1})
        read = self.tools.call('read_paper', {'source': 'MED', 'id': '12345'})
        manifest = self.ledger.freeze()
        entry = manifest['calls'][1]
        self.assertEqual(entry['status'], 'completed')
        self.assertIsNone(entry['selected_graph'])
        raw_descriptor = entry['upstream_response']
        self.assertEqual((self.root / raw_descriptor['path']).read_bytes(), self.http.raw)
        self.assertEqual(raw_descriptor['sha256'], hashlib.sha256(self.http.raw).hexdigest())
        self.assertNotIn('_raw_response', json.dumps(read))
        request = json.loads((self.root / entry['upstream_request']['path']).read_bytes())
        self.assertEqual(request['method'], 'GET')
        self.assertTrue(request['url'].startswith(BASE + '/search?'))
        capture = json.loads(read['content'][-1]['text'])
        self.assertEqual(capture['source_locator'], 'ledger_sequence=2;pointer=/structuredContent/data/text')
        sources = ledger_sources(self.root / 'manifest.json')
        self.assertEqual(set(sources), {entry['response']['sha256']})
        validate_new_files({'files': [capture['file']]}, {}, sources)
        search_file = json.loads(search['content'][-1]['text'])['file']
        with self.assertRaises(EvidenceBuildError): validate_new_files({'files': [search_file]}, {}, sources)
        (self.root / entry['response']['path']).write_bytes(b'changed')
        with self.assertRaises(EvidenceBuildError): ledger_sources(self.root / 'manifest.json')

    def test_empty_and_failed_papers_retained_but_not_promoted(self):
        self.http.raw = paper_response('')
        self.tools.call('read_paper', {'source': 'MED', 'id': '12345'})
        self.http.status = 429
        self.tools.call('read_paper', {'source': 'MED', 'id': '12345'})
        self.assertEqual([row['status'] for row in self.ledger.freeze()['calls']], ['empty', 'failed'])
        self.assertEqual(ledger_sources(self.root / 'manifest.json'), {})
        self.assertTrue(all('upstream_response' in row for row in self.ledger.entries))

    def test_bounded_calls_and_argument_validation_do_not_add_requests(self):
        for _ in range(4): self.tools.call('search_papers', {'query': 'fixture'})
        for _ in range(5): self.tools.call('read_paper', {'source': 'MED', 'id': '12345'})
        self.tools.call('read_paper', {'source': 'MED', 'id': '12345', 'url': 'https://other.test'})
        self.assertEqual(len(self.http.requests), 7)
        self.assertEqual(self.ledger.entries[3]['status'], 'failed')
        self.assertEqual(self.ledger.entries[8]['status'], 'failed')
        self.assertEqual(self.ledger.entries[9]['status'], 'failed')
        self.assertTrue(self.ledger.freeze()['complete'])

    def agent_tools(self, name='agent', **limits):
        ledger = Ledger(self.root / name, 'agent-job', 1)
        bounds = {'max_calls': 30, 'max_searches': 3, 'max_papers': 2, 'max_characters': 60, **limits}
        return ledger, ScopedTools((), ledger, soft_budget=True, literature=LiteratureClient(opener=self.http, max_results=9, guidance=True), **bounds)

    def test_research_tool_descriptions_and_allowances_are_unchanged(self):
        described = {tool['name']: tool for tool in self.tools.definitions()}
        self.assertIn('(maximum three searches per attempt)', described['search_papers']['description'])
        self.assertIn('(maximum four reads)', described['read_paper']['description'])
        self.assertEqual(described['search_papers']['inputSchema']['properties']['limit']['maximum'], 5)
        read = self.tools.call('read_paper', {'source': 'MED', 'id': '12345'})
        self.assertNotIn('full_text', read['structuredContent']['data'])
        self.assertNotIn('run_budget', json.loads(read['content'][-1]['text']))
        self.assertTrue(self.tools.call('search_papers', {'query': 'fixture', 'limit': 6})['isError'])
        refused = self.tools.call('read_paper', {'source': 'MED', 'id': '12345', 'section': 'full_text'})
        self.assertEqual(refused['content'][0]['text'], 'Open-access full text requires the exact PMC identifier from a paper record')
        # Four reads, then the fixed refusal; the third call above was refused before it counted.
        for _ in range(3): self.assertFalse(self.tools.call('read_paper', {'source': 'MED', 'id': '12345'})['isError'])
        fifth = self.tools.call('read_paper', {'source': 'MED', 'id': '12345'})
        self.assertEqual(fifth['content'][0]['text'], 'Bounded literature read budget exhausted; report the remaining limitation')

    def test_agent_job_tools_name_their_own_limits_and_how_to_reach_full_text(self):
        _, tools = self.agent_tools(max_searches=25, max_papers=40, max_characters=500_000)
        described = {tool['name']: tool for tool in tools.definitions()}
        self.assertIn('up to 25 searches this run, up to 9 results each', described['search_papers']['description'])
        self.assertEqual(described['search_papers']['inputSchema']['properties']['limit']['maximum'], 9)
        read = described['read_paper']['description']
        for phrase in ('section defaults to "abstract"', 'source "PMC"', 'section "full_text"', '40 distinct papers', '500,000 characters'):
            self.assertIn(phrase, read)
        self.assertNotIn('maximum four reads', read)
        self.http.raw = paper_response(isOpenAccess='Y')
        search = tools.call('search_papers', {'query': 'fixture', 'limit': 9})
        self.assertFalse(search['isError'])
        self.assertIn('section "full_text"', search['structuredContent']['data']['usage'])
        self.assertEqual(tools.call('read_paper', {'source': 'MED', 'id': '12345'})['structuredContent']['data']['full_text'],
                         {'available': True, 'read_paper': {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text'}})
        self.http.raw = paper_response(isOpenAccess='N')
        closed = tools.call('read_paper', {'source': 'MED', 'id': '12345'})['structuredContent']['data']['full_text']
        self.assertEqual((closed['available'], closed['pmcid']), (False, 'PMC12345'))
        refused = tools.call('read_paper', {'source': 'MED', 'id': '12345', 'section': 'full_text'})
        self.assertIn('the pmcid that record returns', refused['content'][0]['text'])
        self.assertTrue(tools.call('search_papers', {'query': 'fixture', 'limit': 10})['isError'])

    def test_agent_job_paging_one_paper_uses_characters_not_papers(self):
        text = 'A' * 25 + 'B' * 25 + 'C' * 25
        self.http.raw = paper_response(text)
        ledger, tools = self.agent_tools()
        pages = [tools.call('read_paper', {'source': 'MED', 'id': '12345', 'offset': offset, 'limit': 25}) for offset in (0, 25)]
        self.assertEqual([page['structuredContent']['data']['text'] for page in pages], ['A' * 25, 'B' * 25])
        budget = json.loads(pages[1]['content'][-1]['text'])['run_budget']
        self.assertEqual(budget, {'tool_calls': {'used': 2, 'allowed': 30}, 'paper_searches': {'used': 0, 'allowed': 3},
                                  'papers': {'used': 1, 'allowed': 2}, 'paper_characters': {'used': 50, 'allowed': 60}})
        # Ten characters remain: the third page is cut to them, and the next read is refused without a request.
        last = tools.call('read_paper', {'source': 'MED', 'id': '12345', 'offset': 50, 'limit': 25})
        self.assertEqual((last['structuredContent']['data']['text'], last['structuredContent']['data']['next_offset']), ('C' * 10, 60))
        requests = len(self.http.requests)
        spent = tools.call('read_paper', {'source': 'MED', 'id': '12345', 'offset': 60})
        self.assertTrue(spent['isError'])
        self.assertIn('60 characters of paper text for this run are used', spent['content'][0]['text'])
        self.assertEqual(len(self.http.requests), requests)
        self.assertEqual([entry['status'] for entry in ledger.entries], ['completed', 'completed', 'completed', 'failed'])

    def test_agent_job_paper_budget_counts_distinct_papers_and_returns_unused_allowance(self):
        ledger, tools = self.agent_tools(max_characters=10_000)
        self.http.raw = paper_response('Short abstract.')
        first = tools.call('read_paper', {'source': 'MED', 'id': '12345'})
        self.assertEqual(json.loads(first['content'][-1]['text'])['run_budget']['paper_characters']['used'], len('Short abstract.'))
        # The abstract named PMC12345, so its full text is the same paper.
        self.http.raw = b'''<article><front><article-meta><article-id pub-id-type="pmc">12345</article-id>
          <article-id pub-id-type="pmid">12345</article-id></article-meta></front><body><p>Exact reported result.</p></body></article>'''
        full = tools.call('read_paper', {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text'})
        self.assertFalse(full['isError'])
        self.assertEqual(json.loads(full['content'][-1]['text'])['run_budget']['papers'], {'used': 1, 'allowed': 2})
        # The other direction: full text first names the PMID, so its abstract is the same paper.
        _, reverse = self.agent_tools('reverse', max_papers=1, max_characters=10_000)
        self.assertFalse(reverse.call('read_paper', {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text'})['isError'])
        self.http.raw = paper_response('Short abstract.')
        self.assertFalse(reverse.call('read_paper', {'source': 'MED', 'id': '12345'})['isError'])
        self.assertEqual(reverse.budget()['papers'], {'used': 1, 'allowed': 1})
        # A failed read gives its slot back; a second real paper takes the last one; a third is refused.
        self.http.status = 404
        self.assertTrue(tools.call('read_paper', {'source': 'PMC', 'id': 'PMC777', 'section': 'full_text'})['isError'])
        self.assertEqual(tools.budget()['papers']['used'], 1)
        self.http.status = 200
        self.http.raw = encoded({'hitCount': 1, 'resultList': {'result': [dict(id='222', source='MED', pmid='222', abstractText='Second.')]}})
        self.assertFalse(tools.call('read_paper', {'source': 'MED', 'id': '222'})['isError'])
        requests = len(self.http.requests)
        third = tools.call('read_paper', {'source': 'MED', 'id': '333'})
        self.assertIn('All 2 papers for this run have been opened', third['content'][0]['text'])
        self.assertEqual(len(self.http.requests), requests)
        self.assertFalse(tools.call('read_paper', {'source': 'MED', 'id': '222'})['isError'])
        self.assertEqual(tools.budget()['paper_characters']['used'], len('Short abstract.') + len('Exact reported result.') + 2 * len('Second.'))
        for _ in range(4): tools.call('search_papers', {'query': 'fixture'})
        self.assertIn('All 3 paper searches for this run are used', json.loads(canonical_text(ledger, -1))['content'][0]['text'])

    def test_agent_job_overlapping_reads_of_one_new_paper_share_a_slot_whichever_returns_text(self):
        _, tools = self.agent_tools(max_papers=1, max_characters=10_000)
        # Two pages of one unopened paper are in flight together; the first to finish returns nothing.
        first = tools.reserve_paper({'source': 'MED', 'id': '12345', 'offset': 9000})
        second = tools.reserve_paper({'source': 'MED', 'id': '12345'})
        self.assertEqual(tools.budget()['papers']['used'], 1)
        with self.assertRaises(LiteratureInputError): tools.reserve_paper({'source': 'MED', 'id': '222'})
        tools.settle_paper(first, {'isError': True})
        self.assertEqual(tools.budget()['papers']['used'], 1)
        tools.settle_paper(second, {'structuredContent': {'data': {'text': 'Read.', 'paper': {'pmcid': 'PMC12345'}}}})
        self.assertEqual((tools.budget()['papers']['used'], tools.budget()['paper_characters']['used']), (1, 5))
        self.assertFalse(tools.reserve_paper({'source': 'PMC', 'id': 'PMC12345'})['opening'])
        # The other order: the first read returns text while the second is still in flight. That is still one paper,
        # so a different paper can take the second slot; and its PMC id, read at the same time, is the same paper.
        _, pair = self.agent_tools('pair', max_papers=2, max_characters=100_000)  # each read in flight holds 8,000
        early, late = [pair.reserve_paper({'source': 'MED', 'id': '12345'}) for _ in range(2)]
        full = pair.reserve_paper({'source': 'PMC', 'id': 'PMC12345'})
        self.assertEqual(pair.budget()['papers']['used'], 2)
        pair.settle_paper(early, {'structuredContent': {'data': {'text': 'Read.', 'paper': {'pmcid': 'PMC12345'}}}})
        self.assertEqual(pair.budget()['papers']['used'], 1)
        other = pair.reserve_paper({'source': 'MED', 'id': '222'})
        self.assertTrue(other['opening'])
        with self.assertRaises(LiteratureInputError): pair.reserve_paper({'source': 'MED', 'id': '333'})
        for reservation in (late, full): pair.settle_paper(reservation, {'structuredContent': {'data': {'text': 'More.', 'paper': {}}}})
        pair.settle_paper(other, {'structuredContent': {'data': {'text': 'Second.', 'paper': {}}}})
        self.assertEqual((pair.budget()['papers']['used'], pair.opening, pair.budget()['paper_characters']['used']), (2, {}, 5 + 5 + 5 + 7))
        # Both fail: the slot is free again and nothing is charged.
        _, empty = self.agent_tools('empty', max_papers=1, max_characters=10_000)
        for reservation in [empty.reserve_paper({'source': 'MED', 'id': '12345'}) for _ in range(2)]: empty.settle_paper(reservation, None)
        self.assertEqual((empty.budget()['papers']['used'], empty.budget()['paper_characters']['used'], empty.opening), (0, 0, {}))
        self.assertTrue(empty.reserve_paper({'source': 'MED', 'id': '222'})['opening'])

    def test_agent_job_spent_tool_calls_are_an_error_the_run_survives(self):
        ledger, tools = self.agent_tools(max_calls=2, max_papers=5, max_characters=10_000)
        for _ in range(2): self.assertFalse(tools.call('read_paper', {'source': 'MED', 'id': '12345'})['isError'])
        requests = len(self.http.requests)
        spent = tools.call('search_papers', {'query': 'fixture'})
        self.assertTrue(spent['isError'])
        self.assertIn('All 2 REVEAL tool calls for this run are used', spent['content'][0]['text'])
        self.assertEqual(len(self.http.requests), requests)
        self.assertEqual([entry['status'] for entry in ledger.entries], ['completed', 'completed', 'failed'])
        # The ledger check that fails a job on a denied call accepts this run.
        output = self.root / 'agent'
        package = self.root / 'agent-input.json'; package.write_bytes(b'{}')
        (output / 'manifest.json').write_bytes(json.dumps(ledger.freeze()).encode())
        (output / 'runtime.json').write_bytes(encoded({'job_id': 'agent-job', 'attempt': 1, 'model': 'claude-test',
                                                       'input_sha256': hashlib.sha256(b'{}').hexdigest()}))
        request = ExecutionRequest(job_id='agent-job', attempt=1, kind='agent', input_path=package, output_dir=output)
        result = SimpleNamespace(runtime_manifest_path=output / 'runtime.json', ledger_manifest_path=output / 'manifest.json')
        validate_execution_ledger(result, request, 'claude-test')
        # A research run keeps the fatal denial.
        strict_ledger = Ledger(self.root / 'strict', 'job', 1)
        strict = ScopedTools((), strict_ledger, max_calls=1, literature=LiteratureClient(opener=self.http))
        strict.call('read_paper', {'source': 'MED', 'id': '12345'}); strict.call('read_paper', {'source': 'MED', 'id': '12345'})
        self.assertEqual([entry['status'] for entry in strict_ledger.entries], ['completed', 'denied'])

    def test_literature_limits_must_be_given_together_and_positive(self):
        for bounds in ({'max_papers': 2}, {'max_characters': 100}, {'max_papers': 0, 'max_characters': 100}, {'max_searches': 0}):
            with self.assertRaises(ValueError): ScopedTools((), self.ledger, **bounds)

    def test_credentials_not_sent_and_raw_body_redacted_before_durable_capture(self):
        secret = 'fixture-sensitive-key'
        self.ledger.secrets = (secret,)
        self.tools.call('search_papers', {'query': secret})
        self.assertEqual(self.http.requests, [])
        self.assertEqual(self.ledger.entries[-1]['status'], 'denied')
        self.http.raw = paper_response('Source unexpectedly included ' + secret)
        result = self.tools.call('read_paper', {'source': 'MED', 'id': '12345'})
        self.ledger.freeze()
        self.assertEqual(self.ledger.entries[-1]['upstream_response']['credential_redactions'], 1)
        for path in self.root.rglob('*'):
            if path.is_file(): self.assertNotIn(secret.encode(), path.read_bytes())
        # Public projection has the same configured-secret boundary.
        self.assertNotIn(secret, display_arguments('mcp__reveal__search_papers', {'query': secret}, (secret,)))
        self.assertEqual(json.loads(display_arguments('mcp__reveal__read_paper', {'source': 'MED', 'id': '12345', 'offset': 8}))['offset'], 8)


class OutputAndOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(); self.work = self.root / 'work'; self.work.mkdir()
        self.output = self.root / 'output'
        self.user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        self.enterContext(patch.object(box_remote, 'OUTPUT', self.output))
        self.enterContext(patch.object(box_remote.pwd, 'getpwnam', return_value=self.user))

    def test_relative_output_alias_prevents_mkdir_under_readonly_workspace(self):
        original = self.work / 'input.json'; original.write_bytes(b'exact evidence')
        box_remote.prepare_writable_output(self.work)
        self.assertTrue((self.work / 'output').is_symlink())
        self.assertEqual((self.work / 'output').resolve(), self.output)
        original.chmod(0o444); self.work.chmod(0o555)
        self.addCleanup(self.work.chmod, 0o755)
        real_run = subprocess.run
        observed = []
        def local_uid_probe(command, **kwargs):
            observed.append(command)
            self.assertEqual(command[:6], ['setpriv', '--reuid', str(os.getuid()), '--regid', str(os.getgid()), '--clear-groups'])
            # Host macOS lacks Linux setpriv: execute the same probe as current
            # unprivileged UID against actual readonly files and writable alias.
            return real_run(command[7:], **kwargs)
        with patch.object(box_remote.subprocess, 'run', side_effect=local_uid_probe):
            box_remote.verify_writable_output(self.work)
        self.assertEqual(len(observed), 1)
        self.assertFalse((self.output / '.trusted-write-probe').exists())
        self.assertEqual(original.read_bytes(), b'exact evidence')
        self.assertEqual(original.stat().st_mode & 0o777, 0o444)
        (self.work / 'output' / 'note.txt').write_text('A bounded author note')
        self.assertTrue((self.output / 'note.txt').is_file())

    def test_alias_conflict_and_permission_preflight_fail_before_authoring(self):
        (self.work / 'output').mkdir()
        with self.assertRaises(ValueError): box_remote.prepare_writable_output(self.work)
        with patch.object(box_remote.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'before agent start'):
                box_remote.verify_writable_output(self.work)

    def test_structured_outcome_write_and_legacy_roundtrip_without_fake_summary(self):
        box_remote.prepare_writable_output(self.work)
        value = {'format': FORMAT, 'status': 'insufficient_evidence', 'reason': 'No observed temporal ordering.',
                 'summary': 'The selected evidence leaves causal direction unresolved.',
                 'missing_evidence': ['Temporal or perturbational observations'],
                 'evidence_refs': [{'source': 'package', 'pointer': '/selection', 'ledger_sequence': None}]}
        tools = ScopedTools((), Ledger(self.root / 'ledger', 'job', 1), write_outcome=box_remote.write_outcome_tool)
        self.assertEqual(tools.definitions()[0]['name'], 'write_outcome')
        result = tools.call('write_outcome', {'outcome': value})
        self.assertFalse(result.get('isError', False))
        saved = json.loads((self.output / 'outcome.json').read_bytes())
        self.assertEqual(saved['reason'], value['reason'])
        self.assertEqual(validate_insufficient_outcome(saved), saved)
        self.assertEqual(tools.ledger.entries[0]['status'], 'completed')
        legacy = {'status': 'insufficient_evidence', 'reason': 'Exact original reason.', 'knowledge_gap_id': 'exact-gap'}
        box_remote.write_outcome_tool(legacy)
        saved = json.loads((self.output / 'outcome.json').read_bytes())
        self.assertNotIn('format', saved)
        self.assertNotIn('summary', saved)
        self.assertEqual(validate_insufficient_outcome(saved)['knowledge_gap_id'], 'exact-gap')

    def test_invalid_outcomes_and_symlink_cannot_write(self):
        base = {'format': FORMAT, 'status': 'insufficient_evidence', 'reason': 'Supported limitation'}
        invalid = [dict(base, summary='x' * 1201), dict(base, extra='untrusted'), dict(base, reason=' '),
                   dict(base, next_steps=['x'] * 21), dict(base, knowledge_gap_id='legacy-only'),
                   dict(base, evidence_refs=[{'source': 'package', 'pointer': '/bad~2escape', 'ledger_sequence': None}]),
                   dict(base, evidence_refs=[{'source': 'tool_response', 'pointer': '/data', 'ledger_sequence': True}]),
                   dict(base, evidence_refs=[{'source': 'package', 'pointer': '/data', 'ledger_sequence': 1}])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError): validate_insufficient_outcome(value)
        box_remote.prepare_writable_output(self.work)
        protected = self.root / 'original.json'; protected.write_bytes(b'original')
        (self.output / 'outcome.json').symlink_to(protected)
        with self.assertRaises(PolicyError): box_remote.write_outcome_tool(base)
        self.assertEqual(protected.read_bytes(), b'original')
        with self.assertRaises(DraftValidationError): box_remote.write_outcome_tool(invalid[0])


if __name__ == '__main__':
    unittest.main()
