"""The deposit form of an agent job's Scientific Account: literature evidence located by ValueLocator in a paper
text identified by checksum, REVEAL's capture Files dropped, one mint with the vendored DAPPER 0.2.1-a1."""
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from reveal_backend import account_deposit as deposit
from reveal_backend.box_literature import LiteratureClient
from reveal_backend.box_mcp import Ledger, ScopedTools
from reveal_backend.runtime_config import ROOT

RESULT = 'Hepatocyte-specific deletion of Lepr reduced insulin-stimulated AKT phosphorylation by 42% in liver.'
CLAMP = 'Glucose production was not suppressed during the clamp in knockout mice.'
UNQUOTED = 'Mice were housed at 22 °C on a twelve-hour light cycle.'
ARTICLE = ('''<?xml version="1.0"?><article><front><article-meta><article-id pub-id-type="pmc">12345</article-id>
<article-id pub-id-type="pmid">99999</article-id><article-id pub-id-type="doi">10.1/fixture</article-id>
<title-group><article-title>Leptin receptor loss and hepatic insulin signalling</article-title></title-group></article-meta></front>
<body><sec><title>Results</title><p>''' + RESULT + '''</p><p>''' + CLAMP + '''</p><p>''' + UNQUOTED + '''</p></sec></body></article>''').encode()
ABSTRACT = 'We show that Lepr deletion in hepatocytes impairs insulin action in mice fed a high-fat diet for eight weeks.'
RECORD = json.dumps({'hitCount': 1, 'resultList': {'result': [dict(
    id='99999', source='MED', pmid='99999', pmcid='PMC12345', doi='10.1/fixture', title='Leptin receptor loss and hepatic insulin signalling.',
    abstractText=ABSTRACT)]}}).encode()


class Response(io.BytesIO):
    status = 200


class HTTP:
    def __init__(self, raw): self.raw = raw
    def open(self, request, timeout): return Response(self.raw)


def evidence(identity, capture, snippet, proposition='urn:t:proposition', **extra):
    return {'id': identity, 'target_proposition': proposition, 'direction': 'SUPPORTS', 'snippet': snippet,
            'context': 'Source locator: ' + capture['source_locator'] + '. Mouse liver.',
            'explanation': 'The paper reports this directly.', 'was_derived_from': [capture['file']['id']], **extra}


class AccountDepositTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.http = HTTP(ARTICLE)
        self.ledger = Ledger(self.root, 'deposit-job', 1)
        self.tools = ScopedTools(('prokn',), self.ledger, soft_budget=True, max_calls=40, max_searches=3, max_papers=5, max_characters=100_000,
                                 literature=LiteratureClient(opener=self.http, guidance=True))
        self.full = self.capture({'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text'})

    def capture(self, arguments):
        result = self.tools.call('read_paper', arguments)
        self.assertFalse(result.get('isError'), result)
        return json.loads(result['content'][-1]['text'])

    def calls(self):
        return self.ledger.entries

    def account(self, items, files):
        """An accepted account as acceptance.assemble_account returns it (ids here are placeholders: the deposit
        mint recomputes every id from content)."""
        return {'prefixes': {'dapper': 'https://example.invalid/'},
                'knowledge_gaps': [{'id': 'urn:t:gap', 'text': 'Does loss of LEPR signalling cause hepatic insulin resistance?',
                                    'gap_description': 'Caller-supplied question.'}],
                'persons': [{'id': 'urn:t:person', 'name': 'Test Researcher'}],
                'activities': [{'id': 'urn:t:activity', 'name': 'REVEAL Claude Code account construction',
                                'command': 'reveal-worker box deposit-job --attempt 1', 'generated_at_time': '2026-11-12T00:00:00Z'}],
                'organizations': [],
                'files': files,
                'propositions': [{'id': 'urn:t:proposition', 'statement': 'Loss of hepatocyte LEPR reduces hepatic insulin signalling.'}],
                'evidence_items': items,
                'claims': [{'id': 'urn:t:claim', 'proposition': 'urn:t:proposition', 'direction': 'SUPPORTS', 'status': 'proposed',
                            'has_evidence': [item['id'] for item in items], 'was_generated_by': 'urn:t:activity',
                            'was_attributed_to': ['urn:t:person']}],
                'scientific_accounts': [{'id': 'urn:t:account', 'name': 'Literature account', 'question': 'urn:t:gap',
                                         'context': 'Built from captured paper reads.', 'component_claims': ['urn:t:claim'],
                                         'closing_remarks': 'One mouse study.', 'was_generated_by': 'urn:t:activity',
                                         'was_attributed_to': ['urn:t:person']}],
                'used_edges': [{'subject': 'urn:t:activity', 'predicate': 'prov:used', 'object': file['id']} for file in files]}

    def test_literature_evidence_becomes_a_verifiable_locator_and_the_capture_file_leaves(self):
        source = self.account([evidence('urn:t:evidence', self.full, RESULT, reference='PMID:99999')], [self.full['file']])
        untouched = deepcopy(source)
        document, summary = deposit.deposit_document(source, self.root, self.calls())
        self.assertEqual(source, untouched)                                   # the accepted document is not modified
        self.assertNotIn('prefixes', document); self.assertNotIn('persons', document)
        item, located = document['evidence_items'][0], document['value_locators'][0]
        files = {node['filename']: node for node in document['files']}
        text, upstream = files['PMC12345.fulltext.txt'], files['PMC12345.europepmc.xml']
        self.assertEqual(set(files), {'PMC12345.fulltext.txt', 'PMC12345.europepmc.xml'})   # the capture File is gone
        # The DAPPER contract: the File's sha256 names the bytes, and file[start : start + length] decodes to value_text.
        data = deposit.paper_text_bytes(self.root, self.calls(), text['sha256'])
        self.assertEqual((hashlib.sha256(data).hexdigest(), len(data)), (text['sha256'], text['size_in_bytes']))
        self.assertEqual(data[located['byte_start']:located['byte_start'] + located['byte_length']].decode(), located['value_text'])
        self.assertEqual((located['value_text'], located['file'], located['media_type']), (RESULT, text['id'], 'text/plain'))
        self.assertTrue(located['selector'].startswith('paragraph=1; section=Results; chars='))
        self.assertEqual((item['snippet'], item['value_locators'], item['was_derived_from']), (RESULT, [located['id']], [text['id']]))
        self.assertEqual(item['context'], 'Source locator: ' + located['selector'] + '. Mouse liver.')
        publication = document['publications'][0]
        self.assertEqual({k: publication[k] for k in ('title', 'pmid', 'doi', 'citation')},
                         {'title': 'Leptin receptor loss and hepatic insulin signalling', 'pmid': '99999', 'doi': 'doi:10.1/fixture', 'citation': 'PMCID:PMC12345'})
        self.assertEqual((item['reference'], item['reference_title']), (publication['id'], publication['title']))
        # The text stays with REVEAL: no location, and nothing of the paper beyond the quote is in the document.
        self.assertNotIn('location', text); self.assertTrue(upstream['location'].endswith('/PMC12345/fullTextXML'))
        self.assertEqual(upstream['sha256'], hashlib.sha256(ARTICLE).hexdigest())
        serialized = json.dumps(document)
        for absent in (CLAMP, UNQUOTED, 'urn:reveal', 'ledger_sequence', self.full['file']['sha256'], 'urn:t:'):
            self.assertNotIn(absent, serialized)
        activity, account = document['activities'][0]['id'], document['scientific_accounts'][0]
        self.assertEqual(document['used_edges'], [{'subject': activity, 'predicate': 'prov:used', 'object': text['id']}])
        self.assertEqual(document['was_derived_from_edges'], [{'subject': text['id'], 'predicate': 'prov:wasDerivedFrom', 'object': upstream['id']}])
        self.assertEqual(summary, {'account_id': account['id'], 'dapper_version': '0.2.1-a1', 'objects': 11, 'edges': 2, 'claims': 1,
                                   'value_locators': 1, 'papers': [{'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text',
                                       'reference': 'PMID:99999', 'text_sha256': text['sha256'], 'text_bytes': text['size_in_bytes'],
                                       'media_type': 'text/plain', 'filename': 'PMC12345.fulltext.txt', 'evidence_items': 1, 'file_id': text['id']}]})
        self.assertIsNone(deposit.paper_text_bytes(self.root, self.calls(), '0' * 64))
        # Every id is a DAPPER-ID-1 address, every reference resolves, and the same input mints the same account.
        ids = {node['id'] for key, rows in document.items() if not key.endswith('_edges') for node in rows}
        self.assertTrue(all(identity.startswith('dapper:') and len(identity.split('.')[1]) == 32 for identity in ids))
        self.assertEqual(deposit.deposit_document(source, self.root, self.calls())[0], document)

    def test_the_requester_is_named_only_when_the_job_asked_for_it(self):
        source = self.account([evidence('urn:t:evidence', self.full, RESULT)], [self.full['file']])
        source['was_attributed_to_edges'] = [{'subject': 'urn:t:claim', 'predicate': 'prov:wasAttributedTo', 'object': 'urn:t:person'}]
        plain, summary = deposit.deposit_document(source, self.root, self.calls())
        serialized = json.dumps(plain)
        # DAPPER requires an agent on a Claim: unnamed, that is the service, one Organization shared by every such account.
        service, = plain['organizations']
        self.assertEqual({k: v for k, v in service.items() if k != 'id'}, deposit.SERVICE_AGENT); self.assertTrue(service['id'].startswith('dapper:Organization.'))
        self.assertNotIn('persons', plain); self.assertNotIn('Test Researcher', serialized); self.assertNotIn('urn:', serialized)
        self.assertEqual((plain['claims'][0]['was_attributed_to'], plain['scientific_accounts'][0]['was_attributed_to']), ([service['id']], [service['id']]))
        self.assertEqual(plain['was_attributed_to_edges'][0]['object'], service['id'])
        named, with_person = deposit.deposit_document(source, self.root, self.calls(), attribute_requester=True)
        person = named['persons'][0]
        self.assertEqual(person['name'], 'Test Researcher'); self.assertEqual(with_person['objects'], summary['objects']); self.assertNotIn('organizations', named)
        self.assertEqual((named['claims'][0]['was_attributed_to'], named['scientific_accounts'][0]['was_attributed_to']), ([person['id']], [person['id']]))
        self.assertEqual(named['was_attributed_to_edges'][0]['object'], person['id'])
        self.assertNotEqual(named['scientific_accounts'][0]['id'], plain['scientific_accounts'][0]['id'])

    def test_ids_of_untouched_nodes_are_the_ones_the_pinned_release_mints(self):
        """0.2.0 (the pinned release that gates the account) and 0.2.1-a1 (which mints the deposit) share one identity
        scheme, so a node the rewrite does not touch keeps its id across the two."""
        document, _ = deposit.deposit_document(self.account([evidence('urn:t:evidence', self.full, RESULT)], [self.full['file']]),
                                               self.root, self.calls(), attribute_requester=True)
        program = ("import json,sys\nfrom pathlib import Path\nschema=Path(sys.argv[1])\nsys.path[:0]=[str(schema/'identity'),str(schema)]\n"
                   "from dapper_identity import compute_id,load_schema\nview=load_schema(schema/'dapper.yaml')\n"
                   "print(json.dumps([compute_id({k:v for k,v in node.items() if k!='id'},cls,view) for cls,node in json.loads(sys.stdin.read())]))")
        nodes = [('KnowledgeGap', document['knowledge_gaps'][0]), ('Proposition', document['propositions'][0]), ('Person', document['persons'][0])]
        pinned = json.loads(subprocess.run([sys.executable, '-c', program, str(ROOT / 'data/dapper/0.2.0/snapshot/schema')],
                                           input=json.dumps(nodes), capture_output=True, text=True, check=True).stdout)
        self.assertEqual(pinned, [node['id'] for _, node in nodes])

    def test_several_reads_of_one_paper_share_a_text_file_and_a_publication(self):
        paged = self.capture({'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text', 'offset': 0, 'limit': 12000})
        self.http.raw = RECORD
        abstract = self.capture({'source': 'MED', 'id': '99999'})
        items = [evidence('urn:t:e1', self.full, RESULT), evidence('urn:t:e2', paged, CLAMP),
                 evidence('urn:t:e3', paged, '  ' + RESULT.replace(' ', '\n', 2) + ' '),          # the same span, quoted with other whitespace
                 evidence('urn:t:e4', abstract, 'Lepr deletion in hepatocytes impairs insulin action')]
        document, summary = deposit.deposit_document(self.account(items, [self.full['file'], paged['file'], abstract['file']]),
                                                     self.root, self.calls())
        names = sorted(node['filename'] for node in document['files'])
        self.assertEqual(names, ['99999.abstract.html', '99999.europepmc.json', 'PMC12345.europepmc.xml', 'PMC12345.fulltext.txt'])
        # e3 differs from e1 only in whitespace: identical once located, so content addressing makes them one node.
        self.assertEqual((len(document['value_locators']), len(document['evidence_items'])), (3, 3))
        self.assertEqual(len(document['publications']), 1)                    # PMID and PMC id are one paper
        self.assertEqual({item['reference'] for item in document['evidence_items']}, {document['publications'][0]['id']})
        self.assertEqual({item['reference_title'] for item in document['evidence_items']}, {'Leptin receptor loss and hepatic insulin signalling'})
        self.assertEqual([(paper['id'], paper['section'], paper['evidence_items']) for paper in summary['papers']],
                         [('99999', 'abstract', 1), ('PMC12345', 'full_text', 3)])
        self.assertEqual(len(document['used_edges']), 2); self.assertEqual(len(document['was_derived_from_edges']), 2)
        html = next(node for node in document['value_locators'] if node['media_type'] == 'text/html')
        self.assertTrue(html['selector'].startswith('abstract; chars='))

    def test_evidence_from_anything_but_a_paper_read_is_refused_so_no_internal_capture_is_deposited(self):
        graph = self.ledger.start('query_graph', {'graph': 'prokn'}, 'prokn')
        self.ledger.finish(graph, {'content': [{'type': 'text', 'text': 'Captured graph observation'}]}, 'completed')
        graph_file = {'id': 'urn:t:graph-file', 'filename': 'graph.json', 'mime_type': 'application/json',
                      'sha256': graph['response']['sha256'], 'size_in_bytes': graph['response']['size_bytes']}
        items = [evidence('urn:t:e1', self.full, RESULT),
                 {'id': 'urn:t:e2', 'target_proposition': 'urn:t:proposition', 'direction': 'SUPPORTS', 'context': 'graph row',
                  'explanation': 'A graph observation.', 'was_derived_from': ['urn:t:graph-file']}]
        with self.assertRaisesRegex(deposit.DepositError, 'urn:t:e2: This EvidenceItem cites the result of query_graph'):
            deposit.deposit_document(self.account(items, [self.full['file'], graph_file]), self.root, self.calls())
        # A capture File that nothing cites is dropped, not deposited; one that some other object still cites is refused.
        document, _ = deposit.deposit_document(self.account(items[:1], [self.full['file'], graph_file]), self.root, self.calls())
        self.assertNotIn('graph.json', [node['filename'] for node in document['files']])
        self.assertNotIn(graph['response']['sha256'], json.dumps(document))
        cited = self.account(items[:1], [self.full['file'], graph_file])
        cited['claims'][0]['source_files'] = ['urn:t:graph-file']
        with self.assertRaisesRegex(deposit.DepositError, 'still cites REVEAL tool captures'):
            deposit.deposit_document(cited, self.root, self.calls())

    def test_an_account_that_cannot_be_put_in_deposit_form_is_refused_with_the_object_named(self):
        for change, fragment in (
                ({'snippet': 'Lepr deletion had no effect on AKT phosphorylation in liver tissue.'}, 'urn:t:evidence: The snippet is not verbatim text'),
                ({'snippet': None}, 'urn:t:evidence: Evidence from a paper needs a snippet'),
                ({'explanation': ''}, 'urn:t:evidence: Evidence quoted from a paper needs explanation'),
                ({'target_proposition': None}, 'urn:t:evidence: Evidence quoted from a paper needs target_proposition')):
            item = {**evidence('urn:t:evidence', self.full, RESULT), **change}
            with self.assertRaises(deposit.DepositError) as refused:
                deposit.deposit_document(self.account([item], [self.full['file']]), self.root, self.calls())
            self.assertIn(fragment, str(refused.exception))
        self.http.raw = RECORD
        abstract = self.capture({'source': 'MED', 'id': '99999'})
        both = {**evidence('urn:t:evidence', self.full, RESULT), 'was_derived_from': [self.full['file']['id'], abstract['file']['id']]}
        with self.assertRaises(deposit.DepositError) as two:
            deposit.deposit_document(self.account([both], [self.full['file'], abstract['file']]), self.root, self.calls())
        self.assertIn('cites reads of 2 papers', str(two.exception))
        account = self.account([evidence('urn:t:evidence', self.full, RESULT)], [self.full['file']])
        with self.assertRaises(deposit.DepositError) as plural:
            deposit.deposit_document({**account, 'scientific_accounts': account['scientific_accounts'] * 2}, self.root, self.calls())
        self.assertIn('exactly one ScientificAccount', str(plural.exception))

    def test_mint_refuses_unknown_groups_and_references_the_document_does_not_contain(self):
        node = {'id': 'urn:t:p', 'statement': 'A proposition.'}
        minted, mapping = deposit.mint({'propositions': [node], 'claims': []})
        self.assertEqual(list(minted), ['propositions']); self.assertEqual(mapping['urn:t:p'], minted['propositions'][0]['id'])
        with self.assertRaises(deposit.DepositError) as unknown:
            deposit.mint({'propositions': [node], 'proposition': [node]})
        self.assertIn('not DAPPER DAPPER-ID-1 groups: proposition', str(unknown.exception))
        claim = {'id': 'urn:t:c', 'proposition': 'dapper:Proposition.' + 'A' * 32, 'was_generated_by': 'urn:t:a', 'was_attributed_to': ['urn:t:x']}
        with self.assertRaises(deposit.DepositError) as dangling:
            deposit.mint({'claims': [claim]})
        self.assertIn('references objects it does not contain', str(dangling.exception))

    def test_the_vendored_dapper_is_the_recorded_commit_and_reproduces_its_own_vectors(self):
        vendor = json.loads((deposit.VENDOR / 'VENDOR.json').read_text())
        self.assertEqual((vendor['version'], vendor['commit'][:7]), ('0.2.1-a1', 'ac1a4d7'))
        for name, digest in vendor['files'].items():
            self.assertEqual(hashlib.sha256((deposit.VENDOR / name).read_bytes()).hexdigest(), digest, name)
        vectors = json.loads((deposit.VENDOR / 'schema/identity/test_vectors.json').read_text())['vectors']
        groups = {'Activity': 'activities', 'File': 'files', 'Proposition': 'propositions'}
        checked = 0
        for vector in vectors:
            if vector['class'] in groups and 'dapper:' not in json.dumps(vector['instance']):
                minted, _ = deposit.mint({groups[vector['class']]: [{'id': 'urn:t:v', **vector['instance']}]})
                self.assertEqual(minted[groups[vector['class']]][0]['id'], vector['expected_id'], vector['name'])
                checked += 1
        self.assertGreater(checked, 0)


if __name__ == '__main__':
    unittest.main()
