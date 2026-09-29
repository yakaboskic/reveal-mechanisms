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
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.public_tool_activity import display_arguments
from reveal_backend.research_outcome import FORMAT, validate_insufficient_outcome
from reveal_backend.runtime_config import ROOT


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
