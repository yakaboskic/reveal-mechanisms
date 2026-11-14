"""The query_claims tool: read-only lookups in the claims repository from inside an agent run. Offline; the opener is
injected. Checks the queries it can send, what comes back, that every call is captured, and that nothing it returns
can be cited as a source."""
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from reveal_backend import box_agent, box_claims
from reveal_backend.box_adapter import egress_policy
from reveal_backend.box_claims import ClaimsClient, ClaimsInputError
from reveal_backend.box_mcp import Ledger, ScopedTools

PROPOSITION = 'dapper:Proposition.Ff8qcu-J5lhxnew01n3HWgnWNoTInJYQ'
CLAIM = 'dapper:Claim.2UUvJHMk7n06xIqMLuXz6MEs8HlaF5J8'
URL = 'https://claims.example.org'


class Reply(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): return False


class Repository:
    """Answers like the claims repository's /v1/objects routes."""
    def __init__(self): self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        parsed = urllib.parse.urlsplit(request.full_url); query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == '/v1/objects/search':
            objects = [{'id': PROPOSITION, 'class_name': 'Proposition', 'label': 'Loss of hepatocyte LEPR reduces hepatic insulin signalling.',
                        'times_seen': 2, 'resolver_url': '/v1/objects/' + PROPOSITION}]
            if query.get('q') == ['nothing here']: objects = []
            return Reply(json.dumps({'total': 3 if objects else 0, 'limit': int(query['limit'][0]), 'offset': int(query['offset'][0]),
                                     'objects': objects}).encode())
        if parsed.path == '/v1/objects/' + CLAIM:
            return Reply(json.dumps({'id': CLAIM, 'class_name': 'Claim', 'direction': 'SUPPORTS', 'proposition': PROPOSITION}).encode())
        raise urllib.error.HTTPError(request.full_url, 404, 'missing', {}, io.BytesIO(b'{"detail":"no deposited object"}'))


class ClaimsToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = Repository()
        self.ledger = Ledger(self.root, 'claims-job', 1)
        self.tools = ScopedTools((), self.ledger, soft_budget=True, max_calls=3, claims=ClaimsClient(URL, opener=self.repository))

    def data(self, **arguments):
        result = self.tools.call('query_claims', arguments)
        self.assertFalse(result.get('isError'), result)
        return result['structuredContent']['data'], result

    def test_search_references_and_fetch_are_read_only_keyless_and_captured(self):
        found, result = self.data(q='hepatic insulin', class_name='Proposition', limit=1)
        self.assertEqual(found, {'count': 1, 'total': 3, 'offset': 0, 'next_offset': 1, 'objects': [
            {'id': PROPOSITION, 'class_name': 'Proposition', 'label': 'Loss of hepatocyte LEPR reduces hepatic insulin signalling.'}]})
        self.data(references=PROPOSITION)
        one, _ = self.data(id=CLAIM)
        self.assertEqual(one, {'found': True, 'count': 1, 'object': {'id': CLAIM, 'class_name': 'Claim', 'direction': 'SUPPORTS', 'proposition': PROPOSITION}})
        sent = [urllib.parse.urlsplit(request.full_url) for request in self.repository.requests]
        self.assertEqual([(part.netloc, part.path) for part in sent], [('claims.example.org', '/v1/objects/search')] * 2 + [('claims.example.org', '/v1/objects/' + CLAIM)])
        self.assertEqual(urllib.parse.parse_qs(sent[1].query), {'references': [PROPOSITION], 'limit': ['10'], 'offset': ['0']})
        for request in self.repository.requests:
            self.assertEqual(request.get_method(), 'GET'); self.assertNotIn('X-api-key', request.headers); self.assertNotIn('Authorization', request.headers)
        # Every call is in the ledger, and none of them yields a capture File: a lookup is not a source an account can cite.
        self.assertEqual([(entry['tool'], entry['status']) for entry in self.ledger.entries], [('query_claims', 'completed')] * 3)
        self.assertEqual(len(result['content']), 1); self.assertNotIn('reveal.tool-capture', json.dumps(result))
        # An account that tried to cite one is told so by the same rule the deposit applies.
        from reveal_backend import literature_locator as locator
        captured = locator.captures(self.ledger.entries)
        self.assertEqual(set(captured.values()), {'query_claims'})
        sha256 = self.ledger.entries[0]['response']['sha256']
        item = {'id': 'urn:draft:e1', 'was_derived_from': ['urn:file:lookup'], 'snippet': 'x' * 30, 'target_proposition': 'p', 'explanation': 'e'}
        with self.assertRaisesRegex(locator.LocatorError, 'cites the result of query_claims'):
            locator.evidence_quote(item, {'urn:file:lookup': {'id': 'urn:file:lookup', 'sha256': sha256}}, {}, captured)

    def test_nothing_found_is_empty_not_an_error_and_a_spent_budget_ends_lookups(self):
        none, _ = self.data(q='nothing here')
        self.assertEqual((none['count'], none['total']), (0, 0)); self.assertEqual(self.ledger.entries[-1]['status'], 'empty')
        missing, _ = self.data(id='dapper:Claim.' + 'z' * 32)
        self.assertEqual(missing, {'found': False, 'id': 'dapper:Claim.' + 'z' * 32, 'count': 0})
        self.data(q='hepatic insulin')
        spent = self.tools.call('query_claims', {'q': 'hepatic insulin'})
        self.assertTrue(spent['isError']); self.assertIn('All 3 REVEAL tool calls', spent['content'][0]['text'])

    def test_queries_the_repository_cannot_be_asked_are_refused_with_what_to_change(self):
        for arguments, message in (({}, 'Give q'), ({'q': 'x'}, '2 to 200 characters'), ({'id': 'Claim.abc'}, 'must be a dapper id'),
                                   ({'id': CLAIM, 'q': 'also'}, 'id alone'), ({'q': 'insulin', 'limit': 500}, 'limit must be'),
                                   ({'q': 'insulin', 'class_name': 'Secret'}, 'class_name must be'), ({'q': 'insulin', 'deposited_by': 'key_1'}, 'takes q')):
            with self.assertRaisesRegex(ClaimsInputError, message): box_claims.request_path(arguments)
        refused = self.tools.call('query_claims', {'references': 'https://attacker.example/x'})
        self.assertTrue(refused['isError']); self.assertIn('must be a dapper id', refused['content'][0]['text'])
        self.assertEqual(self.repository.requests, [])
        # Without a configured repository there is no such tool.
        plain = ScopedTools((), Ledger(self.root / 'plain', 'claims-job', 1), soft_budget=True)
        self.assertNotIn('query_claims', [tool['name'] for tool in plain.definitions()])
        self.assertIn('query_claims', [tool['name'] for tool in self.tools.definitions()])
        self.assertTrue(plain.call('query_claims', {'q': 'insulin'})['isError'])

    def test_only_an_https_address_with_a_plain_host_reaches_a_sandbox(self):
        self.assertEqual(box_claims.base_url('https://claims.example.org/'), 'https://claims.example.org')
        self.assertEqual(box_claims.base_url(' https://claims.example.org/api '), 'https://claims.example.org/api')
        for bad in ('http://claims.example.org', 'https://user:pw@claims.example.org', 'https://claims.example.org?x=1', 'https://localhost',
                    'https://10.0.0.1', '', None, 'claims.example.org', 'https://claims.example.org:99999', 'https://[claims.example.org'):
            self.assertIsNone(box_claims.base_url(bad), bad)
        with self.assertRaises(ValueError): ClaimsClient('http://claims.example.org')

    def test_the_address_is_frozen_with_the_bootstrap_only_for_a_job_that_asked_and_opens_only_that_host(self):
        answer = box_agent.freeze_input({'kind': 'agent', 'prompt': 'p'}, budget_cap=5)
        asked = box_agent.freeze_input({'kind': 'agent', 'prompt': 'p', 'claims_repository': True}, budget_cap=5)
        self.assertEqual((answer['claims_repository'], asked['claims_repository']), (False, True))
        with self.assertRaisesRegex(ValueError, 'claims_repository must be true or false'):
            box_agent.freeze_input({'kind': 'agent', 'prompt': 'p', 'claims_repository': 'yes'}, budget_cap=5)
        self.assertNotIn('claims_url', box_agent.bootstrap_settings(answer, URL))
        self.assertNotIn('claims_url', box_agent.bootstrap_settings(asked, None))
        self.assertNotIn('claims_url', box_agent.bootstrap_settings(asked, 'http://insecure.example.org'))
        settings = box_agent.bootstrap_settings(asked, URL + '/')
        self.assertEqual(settings['claims_url'], URL); box_agent.validate_bootstrap_settings(settings)
        with self.assertRaises(ValueError): box_agent.validate_bootstrap_settings({**settings, 'claims_url': 'http://claims.example.org'})
        config = {'kind': 'agent', 'selected_graphs': [], 'agent': settings}
        self.assertEqual(egress_policy(config), {'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'claims.example.org']})
        self.assertEqual(egress_policy({**config, 'agent': box_agent.bootstrap_settings(answer, URL)}),
                         {'mode': 'custom', 'allowed_domains': ['api.anthropic.com']})


if __name__ == '__main__':
    unittest.main()
