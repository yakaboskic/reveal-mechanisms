"""The deposit form of an agent job's Scientific Account: each quote located by ValueLocator in the file the agent
saved, each File with REVEAL's checksum and the URL it came from, each Activity with the command REVEAL saw run, one
mint with the vendored DAPPER 0.2.1-a1."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from reveal_backend import account_deposit as deposit
from reveal_backend import source_evidence
from reveal_backend.box_mcp import Ledger
from reveal_backend.runtime_config import ROOT
import test_source_evidence as run


class AccountDepositTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'output'
        self.ledger = Ledger(self.root / 'ledger', 'deposit-job', 1)
        run.save_sources(self.output, self.ledger)

    def deposit(self, source, **options):
        return deposit.deposit_document(source, self.output, self.ledger.entries, self.ledger.root, **options)

    def account(self, **change):
        """An accepted account as acceptance.assemble_account returns it: the agent's record of its sources with
        REVEAL's measurements, attributed to the run (ids here are placeholders: the deposit mint recomputes every id
        from content)."""
        draft = run.draft('urn:t:proposition', 'urn:t:gap', **change)
        problems, binding = source_evidence.check(draft, self.output, self.ledger.entries, self.ledger.root)
        self.assertEqual(problems, [])
        document = source_evidence.bind(draft, binding)
        document.update({'prefixes': {'dapper': 'https://example.invalid/'},
                         'knowledge_gaps': [{'id': 'urn:t:gap', 'text': 'Does loss of LEPR signalling cause hepatic insulin resistance?',
                                             'gap_description': 'Caller-supplied question.'}],
                         'persons': [{'id': 'urn:t:person', 'name': 'Test Researcher'}], 'organizations': [],
                         'propositions': [{'id': 'urn:t:proposition', 'statement': 'Loss of hepatocyte LEPR reduces hepatic insulin signalling.'}]})
        document['activities'].append({'id': 'urn:t:activity', 'name': 'REVEAL Claude Code account construction',
                                       'command': 'reveal-worker box deposit-job --attempt 1', 'generated_at_time': '2026-11-12T00:00:00Z'})
        for node in (*document['claims'], *document['scientific_accounts']):
            node.update(was_generated_by='urn:t:activity', was_attributed_to=['urn:t:person'])
        return document

    def test_each_quote_becomes_a_verifiable_locator_in_the_file_the_agent_saved(self):
        source = self.account()
        untouched = deepcopy(source)
        document, summary = self.deposit(source)
        self.assertEqual(source, untouched)                                   # the accepted document is not modified
        self.assertNotIn('prefixes', document); self.assertNotIn('persons', document)
        files = {node['filename']: node for node in document['files']}
        self.assertEqual(set(files), {path for path, _ in run.FILES.values()})
        # The DAPPER contract: the File's sha256 names the bytes, and file[start : start + length] decodes to value_text.
        for locator in document['value_locators']:
            node = next(node for node in document['files'] if node['id'] == locator['file'])
            data = (self.output / node['filename']).read_bytes()
            self.assertEqual((hashlib.sha256(data).hexdigest(), len(data)), (node['sha256'], node['size_in_bytes']))
            self.assertEqual(data[locator['byte_start']:locator['byte_start'] + locator['byte_length']].decode(), locator['value_text'])
        xml = next(locator for locator in document['value_locators'] if locator['file'] == files['sources/PMC3491803.xml']['id'])
        self.assertEqual((xml['value_text'], xml['selector'], xml['media_type']), (run.XML_QUOTE, 'line=8', 'application/xml'))
        items = {item['snippet']: item for item in document['evidence_items']}
        self.assertEqual(items[run.XML_QUOTE]['value_locators'], [xml['id']])
        self.assertEqual(items[run.XML_QUOTE]['was_derived_from'], [files['sources/PMC3491803.xml']['id']])
        self.assertEqual(items[run.XML_QUOTE]['context'], 'Mouse liver, hepatocyte-specific knockout; Results')   # as the agent wrote it
        # Each paper is a Publication named by the identifiers its URL carries; the converted text names the PDF's.
        publications = {node['id']: {k: v for k, v in node.items() if k != 'id'} for node in document['publications']}
        self.assertEqual(sorted(publications.values(), key=json.dumps), [{'citation': 'PMCID:PMC3491803'}, {'doi': 'doi:10.1234/jmet.2012.5'}])
        self.assertEqual({publications[item['reference']].popitem()[1] for item in document['evidence_items']},
                         {'PMCID:PMC3491803', 'doi:10.1234/jmet.2012.5'})
        # The record of how: the Files say where the bytes came from, the Activities what ran.
        self.assertEqual(files['sources/PMC3491803.xml']['location'], run.XML_URL); self.assertNotIn('location', files['sources/jmet-2012.txt'])
        activities = {node.get('command'): node for node in document['activities']}
        self.assertEqual([activities[command]['observed_command'] for command in (run.FETCH_XML, run.FETCH_PDF, run.CONVERT)],
                         [run.RAN['xml'], run.RAN['pdf'], run.RAN['txt']])
        self.assertNotIn('observed_command', activities['reveal-worker box deposit-job --attempt 1'])
        made = {(edge['subject'], edge['object']) for edge in document['was_generated_by_edges']}
        self.assertEqual(made, {(files[path]['id'], activities[command]['id']) for path, command in
                                (('sources/PMC3491803.xml', run.FETCH_XML), ('sources/jmet-2012.pdf', run.FETCH_PDF), ('sources/jmet-2012.txt', run.CONVERT))})
        self.assertEqual(document['was_derived_from_edges'], [{'subject': files['sources/jmet-2012.txt']['id'], 'predicate': 'prov:wasDerivedFrom',
                                                               'object': files['sources/jmet-2012.pdf']['id']}])
        run_activity = activities['reveal-worker box deposit-job --attempt 1']['id']
        used = {(edge['subject'], edge['object']) for edge in document['used_edges']}
        self.assertEqual(used, {(activities[run.CONVERT]['id'], files['sources/jmet-2012.pdf']['id']),
                                (run_activity, files['sources/PMC3491803.xml']['id']), (run_activity, files['sources/jmet-2012.txt']['id'])})
        # The paper itself never leaves REVEAL: nothing of a file beyond its quotes is in the document.
        serialized = json.dumps(document)
        for absent in ('Glucose production rose', 'Discussion', 'urn:reveal', 'urn:t:', 'urn:draft'):
            self.assertNotIn(absent, serialized)
        account = document['scientific_accounts'][0]
        self.assertEqual({key: summary[key] for key in ('account_id', 'dapper_version', 'claims', 'value_locators')},
                         {'account_id': account['id'], 'dapper_version': '0.2.1-a1', 'claims': 1, 'value_locators': 2})
        self.assertEqual([(paper['path'], paper['url'], paper['derived_from'], paper['evidence_items'], paper['media_type']) for paper in summary['papers']],
                         [('sources/PMC3491803.xml', run.XML_URL, None, 1, 'application/xml'),
                          ('sources/jmet-2012.pdf', run.PDF_URL, None, 0, 'application/pdf'),
                          ('sources/jmet-2012.txt', run.PDF_URL, 'sources/jmet-2012.pdf', 1, 'text/plain')])
        self.assertEqual({paper['path']: paper['file_id'] for paper in summary['papers']}, {path: node['id'] for path, node in files.items()})
        self.assertEqual(deposit.source_bytes(self.output, summary['papers']), {hashlib.sha256(data).hexdigest(): data for _, data in run.FILES.values()})
        # Every id is a DAPPER-ID-1 address, every reference resolves, and the same input mints the same account.
        ids = {node['id'] for key, rows in document.items() if not key.endswith('_edges') for node in rows}
        self.assertTrue(all(identity.startswith('dapper:') and len(identity.split('.')[1]) == 32 for identity in ids))
        self.assertEqual(self.deposit(source)[0], document)

    def test_the_requester_is_named_only_when_the_job_asked_for_it(self):
        source = self.account()
        source['was_attributed_to_edges'] = [{'subject': 'urn:draft:claim-1', 'predicate': 'prov:wasAttributedTo', 'object': 'urn:t:person'}]
        plain, summary = self.deposit(source)
        serialized = json.dumps(plain)
        # DAPPER requires an agent on a Claim: unnamed, that is the service, one Organization shared by every such account.
        service, = plain['organizations']
        self.assertEqual({k: v for k, v in service.items() if k != 'id'}, deposit.SERVICE_AGENT); self.assertTrue(service['id'].startswith('dapper:Organization.'))
        self.assertNotIn('persons', plain); self.assertNotIn('Test Researcher', serialized); self.assertNotIn('urn:', serialized)
        self.assertEqual((plain['claims'][0]['was_attributed_to'], plain['scientific_accounts'][0]['was_attributed_to']), ([service['id']], [service['id']]))
        self.assertEqual(plain['was_attributed_to_edges'][0]['object'], service['id'])
        named, with_person = self.deposit(source, attribute_requester=True)
        person = named['persons'][0]
        self.assertEqual(person['name'], 'Test Researcher'); self.assertEqual(with_person['objects'], summary['objects']); self.assertNotIn('organizations', named)
        self.assertEqual((named['claims'][0]['was_attributed_to'], named['scientific_accounts'][0]['was_attributed_to']), ([person['id']], [person['id']]))
        self.assertEqual(named['was_attributed_to_edges'][0]['object'], person['id'])
        self.assertNotEqual(named['scientific_accounts'][0]['id'], plain['scientific_accounts'][0]['id'])

    def test_ids_of_untouched_nodes_are_the_ones_the_pinned_release_mints(self):
        """0.2.0 (the pinned release that gates the account) and 0.2.1-a1 (which mints the deposit) share one identity
        scheme, so a node the rewrite does not touch keeps its id across the two."""
        document, _ = self.deposit(self.account(), attribute_requester=True)
        program = ("import json,sys\nfrom pathlib import Path\nschema=Path(sys.argv[1])\nsys.path[:0]=[str(schema/'identity'),str(schema)]\n"
                   "from dapper_identity import compute_id,load_schema\nview=load_schema(schema/'dapper.yaml')\n"
                   "print(json.dumps([compute_id({k:v for k,v in node.items() if k!='id'},cls,view) for cls,node in json.loads(sys.stdin.read())]))")
        files = {node['filename']: node for node in document['files']}
        nodes = [('KnowledgeGap', document['knowledge_gaps'][0]), ('Proposition', document['propositions'][0]), ('Person', document['persons'][0]),
                 ('File', files['sources/PMC3491803.xml'])]
        pinned = json.loads(subprocess.run([sys.executable, '-c', program, str(ROOT / 'data/dapper/0.2.0/snapshot/schema')],
                                           input=json.dumps(nodes), capture_output=True, text=True, check=True).stdout)
        self.assertEqual(pinned, [node['id'] for _, node in nodes])

    def test_two_quotes_of_one_span_are_one_locator(self):
        source = self.account()
        # The same span quoted again with other whitespace: identical once located, so content addressing merges them.
        again = {**source['evidence_items'][0], 'id': 'urn:draft:evidence-3', 'snippet': '  ' + run.XML_QUOTE.replace(' ', '\n', 2) + ' '}
        source['evidence_items'].append(again); source['claims'][0]['has_evidence'].append('urn:draft:evidence-3')
        document, summary = self.deposit(source)
        self.assertEqual((len(document['value_locators']), len(document['evidence_items'])), (2, 2))
        self.assertEqual([paper['evidence_items'] for paper in summary['papers']], [2, 0, 1])

    def test_an_account_whose_record_does_not_hold_is_refused_with_the_object_named(self):
        changed = self.account()
        changed['evidence_items'][1]['snippet'] = 'Lepr deletion had no effect on AKT phosphorylation in liver tissue.'
        with self.assertRaisesRegex(deposit.DepositError, 'urn:draft:evidence-2: The snippet is not verbatim text of sources/jmet-2012.txt'):
            self.deposit(changed)
        # A File whose measurements differ from the bytes (the accepted document was edited, or the file was).
        edited = self.account()
        edited['files'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(deposit.DepositError, 'urn:draft:file-xml: the File does not carry what REVEAL measured'):
            self.deposit(edited)
        accepted = self.account()
        (self.output / 'sources/PMC3491803.xml').write_bytes(run.XML.replace(b'42%', b'24%'))
        with self.assertRaisesRegex(deposit.DepositError, 'urn:draft:evidence-1: The snippet is not verbatim text'):
            self.deposit(accepted)
        account = self.account(xml_snippet='Glucose production rose. Later, glucose production rose again in knockout mice.')
        with self.assertRaises(deposit.DepositError) as plural:
            self.deposit({**account, 'scientific_accounts': account['scientific_accounts'] * 2})
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
