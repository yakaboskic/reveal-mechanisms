"""The claims repository tools of an agent run. Offline; the opener is injected. query_claims: the lookups it can
send, what comes back, that every call is captured and that its answer is not a file an account can quote.
run_collection_query: a collection's queries, a dimension's values and a run, with the id of the Claim behind each
row. And the rule for the one object an account may name without containing it: a repository Claim in an
EvidenceItem's source_claims, resolved in the repository."""
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
ROW_CLAIM = 'dapper:Claim.gM7GXq3cP0aWm2r5T8uYb1dE4fH6jK9L'
QUERY = {'collection': 'pigean', 'release': {'id': 'dapper:ResultCollection.' + 'r' * 32, 'version': '2026.11.2'}, 'name': 'top-genes-with-sets',
         'description': 'Top genes for a trait by combined score, each with its strongest gene sets.',
         'params': {'trait': {'type': 'string', 'required': True, 'description': 'Trait as a KPN.TRAIT id or a legacy key such as IBD.', 'dimension': 'trait',
                              'values': {'count': 1444, 'sample': [{'value': 'ADFather', 'label': 'ADFather', 'datasets': 1}], 'all': '/v1/collections/pigean/dimensions/trait'}},
                    'topGenes': {'type': 'integer', 'default': 20, 'min': 0, 'max': 200}},
         'returns': {'rows': {'kind': 'gene_phenotype', 'metrics': {'combined': {'description': 'Combined gene-trait support.', 'score_kind': 'SCORE'}},
                              'claims': {'measurement': {'relation': 'biolink:associated_with', 'description': 'Measurement: carries the scores.'},
                                         'tier-combined': {'description': 'Evidence tier.'}}}},
         'claims': ['measurement', 'tier-combined'], 'plan': [{'anchor': {}}], 'run': {'rest': {}}}


def row(gene, index):
    """One served row as the repository returns it: the claim envelope carries whole DAPPER documents."""
    claim = ROW_CLAIM if index == 0 else 'dapper:Claim.' + gene.ljust(32, 'x')
    proposition = {'id': 'dapper:Proposition.' + gene.ljust(32, 'p'), 'statement': f'The gene {gene} is associated with Inflammatory bowel disease.'}
    envelope = {'id': claim, 'resolverUrl': '/v1/objects/' + claim, 'claim': {'id': claim, 'direction': 'SUPPORTS', 'proposition': proposition['id']},
                'proposition': proposition, 'triple': {'subject': 'HGNC.SYMBOL:' + gene, 'predicate': 'biolink:associated_with', 'object': 'KPN.TRAIT:0000505'},
                'scores': [{'metric': 'combined', 'value': 9.5 - index, 'scoreKind': 'SCORE', 'interpretation': 'x' * 500}],
                'evidence': [{'item': {'id': 'dapper:EvidenceItem.' + 'e' * 32}, 'valueLocators': [{'value_text': '9.5'}]}], 'timesDeposited': 3}
    joined = [{'edge': {'kind': 'member_of', 'claims': {}}, 'row': {'kind': 'gene_set_phenotype', 'subject': {'id': f'set-{n}', 'value': f'set_{n}'},
                                                                   'metrics': {'beta': 0.1 * n}}} for n in range(12)]
    return {'kind': 'gene_phenotype', 'dataset': {'id': 'dapper:Dataset.' + 'd' * 32, 'dimensions': {'trait': 'IBD', 'library': 'mouse_msigdb'}},
            'subject': {'id': 'HGNC.SYMBOL:' + gene, 'value': gene, 'label': gene}, 'object': {'id': 'KPN.TRAIT:0000505', 'label': 'Inflammatory bowel disease'},
            'metrics': {'combined': 9.5 - index, 'log_bf': 5.2, 'prior': 0.3}, 'columns': {'trait_group': 'digestive'}, 'claims': {'measurement': envelope},
            'sets': joined}


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
        if parsed.path == '/v1/queries':
            return Reply(json.dumps({'guide': {'summary': 'long guide text'}, 'collections': [], 'queries': [QUERY]}).encode())
        if parsed.path == '/v1/collections/pigean/dimensions/trait':
            return Reply(json.dumps({'collection': 'pigean', 'dimension': 'trait', 'count': 1, 'values': [
                {'value': 'KPN.TRAIT:0000505', 'label': 'Inflammatory bowel disease', 'datasets': 1, 'concept_id': 'EFO:0003767',
                 'xrefs': [{'target_id': 'MESH:D015212'}] * 40}]}).encode())
        if parsed.path == '/v1/collections/pigean/queries/top-genes-with-sets:run':
            sent = json.loads(request.data)
            if sent['variables'].get('trait') == 'nope':
                raise urllib.error.HTTPError(request.full_url, 400, 'bad', {}, io.BytesIO(b'{"detail":"trait has no value nope; see /v1/collections/pigean/dimensions/trait"}'))
            rows = [row(gene, index) for index, gene in enumerate(('NOD2', 'ATG16L1', 'IL23R')[:sent['variables'].get('topGenes', 3)])]
            return Reply(json.dumps({'collection': 'pigean', 'query': 'top-genes-with-sets', 'release': {'id': 'dapper:ResultCollection.' + 'r' * 32, 'version': '2026.11.2'},
                                     'variables': sent['variables'], 'rows': rows, 'warnings': []}).encode())
        if parsed.path == '/v1/objects:batchGet':
            ids = json.loads(request.data)['ids']
            held = {identity: {'id': identity, 'class_name': identity[7:].split('.')[0]} for identity in ids if identity in (CLAIM, ROW_CLAIM, PROPOSITION)}
            return Reply(json.dumps({'objects': held, 'missing': [identity for identity in ids if identity not in held]}).encode())
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
        # An account that tried to cite one is told so by the same rule the gate and the deposit apply: an account
        # cites only files the agent saved under output/sources/ with a recorded command.
        from reveal_backend import source_evidence
        response = self.ledger.entries[0]['response']
        document = {'files': [{'id': 'urn:file:lookup', 'filename': response['path'], 'sha256': response['sha256']}],
                    'evidence_items': [{'id': 'urn:draft:e1', 'was_derived_from': ['urn:file:lookup'], 'snippet': 'x' * 30,
                                        'target_proposition': 'p', 'explanation': 'e'}]}
        problems, binding = source_evidence.check(document, self.ledger.root, self.ledger.entries, self.ledger.root)
        self.assertEqual([where for where, _ in problems], ['urn:file:lookup'])
        self.assertIn('under sources/', problems[0][1])
        self.assertEqual((binding['files'], binding['quotes']), ({}, []))

    def test_a_text_search_can_name_its_class_and_fields_and_a_slow_one_says_what_to_do(self):
        self.data(q='hepatic insulin', class_name='Proposition', fields=['name', 'statement'])
        sent = urllib.parse.parse_qs(urllib.parse.urlsplit(self.repository.requests[-1].full_url).query)
        self.assertEqual(sent, {'q': ['hepatic insulin'], 'class_name': ['Proposition'], 'fields': ['statement,name'], 'limit': ['10'], 'offset': ['0']})
        for arguments, message in (({'fields': ['statement']}, 'fields goes with q'), ({'q': 'insulin', 'fields': []}, 'fields goes with q'),
                                   ({'q': 'insulin', 'fields': ['payload']}, 'fields goes with q'), ({'q': 'insulin', 'fields': 'statement'}, 'fields goes with q'),
                                   ({'references': PROPOSITION, 'fields': ['statement']}, 'fields goes with q')):
            with self.assertRaisesRegex(ClaimsInputError, message): box_claims.request_path(arguments)
        described = next(tool for tool in self.tools.definitions() if tool['name'] == 'query_claims')
        self.assertIn('run_collection_query', described['description']); self.assertIn('class_name', described['description'])
        self.assertEqual(described['inputSchema']['properties']['fields']['items']['enum'], ['statement', 'name', 'description'])

        class Slow:
            def open(self, request, timeout): raise TimeoutError('timed out')
        class Refusing:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 400, 'bad', {}, io.BytesIO(json.dumps({'detail': 'This text search matches too many objects to count and order in time. Add a more specific word.'}).encode()))
        slow = ScopedTools((), Ledger(self.root / 'slow', 'claims-job', 1), soft_budget=True, max_calls=5, claims=ClaimsClient(URL, opener=Slow()))
        late = slow.call('query_claims', {'q': 'gene'})
        self.assertTrue(late['isError'])
        for expected in ('did not answer within 20 seconds', 'not the same as nothing found', 'name a class_name', 'run_collection_query'):
            self.assertIn(expected, late['content'][0]['text'])
        self.assertNotIn('evidence service unavailable', late['content'][0]['text'])
        by_id = slow.call('query_claims', {'id': CLAIM})
        self.assertIn('did not answer within 20 seconds', by_id['content'][0]['text']); self.assertNotIn('Narrow the search', by_id['content'][0]['text'])
        broad = ScopedTools((), Ledger(self.root / 'broad', 'claims-job', 1), soft_budget=True, max_calls=5, claims=ClaimsClient(URL, opener=Refusing()))
        self.assertIn('matches too many objects', broad.call('query_claims', {'q': 'gene'})['content'][0]['text'])

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


class CollectionQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repository = Repository()
        self.ledger = Ledger(Path(temporary.name), 'claims-job', 1)
        self.client = ClaimsClient(URL, opener=self.repository)
        self.tools = ScopedTools((), self.ledger, soft_budget=True, max_calls=5, claims=self.client)

    def data(self, **arguments):
        result = self.tools.call('run_collection_query', arguments)
        self.assertFalse(result.get('isError'), result)
        return result['structuredContent']['data']

    def test_the_queries_a_dimension_and_a_run_come_back_compact_with_the_claim_behind_each_row(self):
        self.assertEqual([tool['name'] for tool in self.tools.definitions() if 'claims repository' in tool['description']],
                         ['query_claims', 'run_collection_query'])
        listed = self.data()
        self.assertEqual(listed, {'count': 1, 'queries': [{
            'collection': 'pigean', 'query': 'top-genes-with-sets', 'description': QUERY['description'],
            'params': {'trait': {'type': 'string', 'required': True, 'description': 'Trait as a KPN.TRAIT id or a legacy key such as IBD.',
                                 'dimension': 'trait', 'examples': ['ADFather']},
                       'topGenes': {'type': 'integer', 'default': 20, 'min': 0, 'max': 200}},
            'claims': {'measurement': 'Measurement: carries the scores.', 'tier-combined': 'Evidence tier.'},
            'metrics': {'combined': 'Combined gene-trait support.'}}]})
        self.assertEqual(self.data(collection='pigean', dimension='trait', search='bowel'), {
            'collection': 'pigean', 'dimension': 'trait', 'count': 1,
            'values': [{'value': 'KPN.TRAIT:0000505', 'label': 'Inflammatory bowel disease', 'datasets': 1, 'concept_id': 'EFO:0003767'}]})
        ran = self.data(collection='pigean', query='top-genes-with-sets', variables={'trait': 'IBD', 'topGenes': 2}, claims=['measurement'])
        self.assertEqual((ran['count'], ran['variables']), (2, {'trait': 'IBD', 'topGenes': 2}))
        first = ran['rows'][0]
        self.assertEqual(first['claims'], {'measurement': {
            'id': ROW_CLAIM, 'statement': 'The gene NOD2 is associated with Inflammatory bowel disease.', 'direction': 'SUPPORTS',
            'triple': {'subject': 'HGNC.SYMBOL:NOD2', 'predicate': 'biolink:associated_with', 'object': 'KPN.TRAIT:0000505'},
            'scores': [{'metric': 'combined', 'value': 9.5, 'scoreKind': 'SCORE'}]}})
        self.assertEqual((first['subject'], first['metrics'], first['dimensions']),
                         ({'id': 'HGNC.SYMBOL:NOD2', 'value': 'NOD2', 'label': 'NOD2'}, {'combined': 9.5, 'log_bf': 5.2, 'prior': 0.3},
                          {'trait': 'IBD', 'library': 'mouse_msigdb'}))
        # Joined rows are shown up to a bound, and the documents an envelope carries are left out.
        self.assertEqual((len(first['sets']), first['sets_more']), (box_claims.MAX_JOINED, 12 - box_claims.MAX_JOINED))
        self.assertNotIn('valueLocators', json.dumps(ran)); self.assertNotIn('x' * 100, json.dumps(ran))
        # What was sent: reads and one POST of the run, to the repository only, with no key.
        sent = [(request.get_method(), urllib.parse.urlsplit(request.full_url).path) for request in self.repository.requests]
        self.assertEqual(sent, [('GET', '/v1/queries'), ('GET', '/v1/collections/pigean/dimensions/trait'),
                                ('POST', '/v1/collections/pigean/queries/top-genes-with-sets:run')])
        self.assertEqual(json.loads(self.repository.requests[-1].data), {'variables': {'trait': 'IBD', 'topGenes': 2}, 'claims': ['measurement']})
        for request in self.repository.requests:
            self.assertEqual(urllib.parse.urlsplit(request.full_url).netloc, 'claims.example.org')
            self.assertNotIn('X-api-key', request.headers); self.assertNotIn('Authorization', request.headers)
        self.assertEqual([(entry['tool'], entry['status']) for entry in self.ledger.entries], [('run_collection_query', 'completed')] * 3)

    def test_a_long_answer_shows_fewer_rows_and_is_never_refused_after_the_run(self):
        long = {'collection': 'pigean', 'query': 'top-genes-with-sets', 'variables': {}, 'warnings': [],
                'rows': [row('GENE%03d' % number, number + 1) for number in range(200)]}
        for item in long['rows']:
            item['claims']['measurement']['proposition']['statement'] = 'A long statement. ' * 16
        shown = box_claims._result(long)
        self.assertLessEqual(len(json.dumps(shown)), box_claims.MAX_RUN_CHARACTERS)
        self.assertEqual((shown['count'], shown['more_rows']), (200, 200 - len(shown['rows'])))
        self.assertTrue(1 < len(shown['rows']) < box_claims.MAX_ROWS)
        self.assertEqual([item['subject']['value'] for item in shown['rows'][:2]], ['GENE000', 'GENE001'])
        self.assertIn(f"Only the first {len(shown['rows'])} rows are shown", shown['note'])

    def test_a_run_is_bounded_and_a_refusal_says_what_to_change(self):
        for arguments, message in (
                ({'collection': 'pigean', 'query': 'top-genes-with-sets', 'variables': {'trait': 'IBD'}}, 'claims must name 1 to 3'),
                ({'collection': 'pigean', 'query': 'top-genes-with-sets', 'claims': []}, 'claims must name 1 to 3'),
                ({'collection': 'pigean', 'query': 'top-genes-with-sets', 'claims': ['a', 'b', 'c', 'd']}, 'claims must name 1 to 3'),
                ({'collection': 'pigean', 'query': 'top-genes-with-sets', 'claims': ['measurement'], 'variables': {'topGenes': 500}}, 'at most 50'),
                ({'collection': 'pigean', 'query': 'top-genes-with-sets', 'claims': ['measurement'], 'variables': {'trait': ['IBD']}}, 'must be a number'),
                ({'collection': '../objects', 'query': 'x', 'claims': ['measurement']}, 'collection must be a name'),
                ({'collection': 'pigean', 'query': 'q/../../deposits', 'claims': ['measurement']}, 'query must be a name'),
                ({'collection': 'pigean', 'query': 'x', 'claims': ['measurement'], 'release': 'old'}, 'takes collection'),
                ({'collection': 'pigean', 'dimension': 'trait', 'query': 'x', 'claims': ['m'], 'search': 'bowel'}, 'Run a query with'),
                ({'dimension': 'trait'}, 'collection must be a name'), ({'search': 'bowel'}, 'Give query')):
            with self.assertRaisesRegex(ClaimsInputError, message): box_claims.collection_request(arguments)
        self.assertEqual(self.repository.requests, [])
        # The repository's own reason for a refusal reaches the agent; a name it does not know is said plainly.
        refused = self.tools.call('run_collection_query', {'collection': 'pigean', 'query': 'top-genes-with-sets', 'variables': {'trait': 'nope'}, 'claims': ['measurement']})
        self.assertTrue(refused['isError']); self.assertIn('trait has no value nope', refused['content'][0]['text'])
        unknown = self.tools.call('run_collection_query', {'collection': 'pigean', 'query': 'no-such-query', 'claims': ['measurement']})
        self.assertTrue(unknown['isError']); self.assertIn('no such collection, query or dimension', unknown['content'][0]['text'])

    def test_an_account_may_name_a_repository_claim_only_in_source_claims_and_only_one_the_repository_holds(self):
        def draft(**change):
            return {'evidence_items': [{'id': 'urn:draft:e1', 'source_claims': [ROW_CLAIM], 'target_proposition': 'urn:draft:p1'},
                                       {'id': 'urn:draft:e2', 'source_claims': ['urn:draft:c0'], 'target_proposition': 'urn:draft:p1'}],
                    'claims': [{'id': 'urn:draft:c0', 'proposition': 'urn:draft:p0'}, {'id': 'urn:draft:c1', 'has_evidence': ['urn:draft:e1', 'urn:draft:e2']}],
                    'scientific_accounts': [{'id': 'urn:draft:a1', 'component_claims': ['urn:draft:c1']}], **change}
        # A Claim of the draft's own in source_claims is an ordinary reference; the repository's is resolved there.
        self.assertEqual(box_claims.repository_references(draft()), ({ROW_CLAIM: ['urn:draft:e1']}, {}))
        self.assertEqual(box_claims.verify_references(draft(), self.client.resolve), ([], [ROW_CLAIM]))
        self.assertEqual(self.repository.requests[-1].get_method(), 'POST')
        self.assertEqual(urllib.parse.urlsplit(self.repository.requests[-1].full_url).path, '/v1/objects:batchGet')
        self.assertEqual(self.client.resolve([ROW_CLAIM, PROPOSITION, 'dapper:Claim.' + 'z' * 32, 'not an id']), {ROW_CLAIM: 'Claim', PROPOSITION: 'Proposition'})
        # An id the repository does not hold, one that is not a Claim, and one also named where a member is expected.
        invented = 'dapper:Claim.' + 'z' * 32
        problems, verified = box_claims.verify_references(draft(evidence_items=[{'id': 'urn:draft:e1', 'source_claims': [invented, ROW_CLAIM]}]), self.client.resolve)
        self.assertEqual(verified, [ROW_CLAIM]); self.assertEqual([where for where, _ in problems], ['urn:draft:e1'])
        self.assertIn('holds no claim ' + invented, problems[0][1])
        member = draft(); member['scientific_accounts'][0]['component_claims'].append(ROW_CLAIM)
        problems, verified = box_claims.verify_references(member, self.client.resolve)
        self.assertEqual((verified, [where for where, _ in problems]), ([], ['urn:draft:a1.component_claims']))
        self.assertIn("Name it only in an EvidenceItem's source_claims", problems[0][1])
        # A Proposition id is not a Claim id, so it is never taken for a repository reference: the linter reports it.
        self.assertEqual(box_claims.repository_references(draft(evidence_items=[{'id': 'urn:draft:e1', 'source_claims': [PROPOSITION]}])), ({}, {}))
        # No repository to ask, or one that cannot be reached: nothing is verified, and each id says why.
        for resolve, expected in ((None, 'no claims repository to check it against'),
                                  (lambda ids: (_ for _ in ()).throw(box_claims.ClaimsUnavailable('The claims repository could not be reached')), 'could not be checked')):
            problems, verified = box_claims.verify_references(draft(), resolve)
            self.assertEqual(verified, []); self.assertIn(expected, problems[0][1])
        # At the gate an unreachable repository is raised for the workflow to retry, never turned into a refusal; a
        # repository that answers 5xx is unavailable, and one that answers 4xx has refused the query.
        class Later(OSError): pass
        def down(ids): raise box_claims.ClaimsUnavailable('The claims repository could not be reached')
        with self.assertRaisesRegex(Later, 'could not be reached'): box_claims.verify_references(draft(), down, Later)
        self.assertEqual(box_claims.verify_references(draft(), lambda ids: {}, Later)[1], [])
        class Failing:
            def __init__(self, status): self.status = status
            def open(self, request, timeout): raise urllib.error.HTTPError(request.full_url, self.status, 'x', {}, io.BytesIO(b'{"detail":"why"}'))
        for status, kind in ((503, box_claims.ClaimsUnavailable), (429, box_claims.ClaimsUnavailable)):
            with self.assertRaises(kind): ClaimsClient(URL, opener=Failing(status)).resolve([ROW_CLAIM])
        with self.assertRaises(ClaimsInputError) as refusal: ClaimsClient(URL, opener=Failing(400)).resolve([ROW_CLAIM])
        self.assertNotIsInstance(refusal.exception, box_claims.ClaimsUnavailable); self.assertIn('why', str(refusal.exception))
        # The backend's own address rule may allow a repository on this machine; a sandbox's never does.
        self.assertEqual(ClaimsClient('http://127.0.0.1:8001', checked=True).url, 'http://127.0.0.1:8001')
        with self.assertRaises(ValueError): ClaimsClient('http://127.0.0.1:8001')

