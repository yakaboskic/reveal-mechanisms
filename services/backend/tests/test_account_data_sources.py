"""Stored account data sources: traced once at acceptance, served with the account, frozen with its publication."""
from copy import deepcopy
import time
import unittest
from unittest.mock import patch

from reveal_backend import account_data_sources as sources, app as api
from reveal_backend.artifact_store import StorageUnavailable
from reveal_backend.auth import Problem
from reveal_backend.mysql_pool import DatabaseBusy
from reveal_backend.repository import digest
import test_provenance_api as provenance_tests

identifier = provenance_tests.identifier
ORG = identifier('Organization', 'o')


class AccountDataSourcesTests(unittest.TestCase):
    principal = provenance_tests.ProvenanceApiTests.principal
    headers = provenance_tests.ProvenanceApiTests.headers
    seed = provenance_tests.ProvenanceApiTests.seed
    request = provenance_tests.ProvenanceApiTests.request
    publish = provenance_tests.ProvenanceApiTests.publish
    install_document = provenance_tests.ProvenanceApiTests.install_document

    def setUp(self):
        provenance_tests.ProvenanceApiTests.setUp(self)

    def traced(self, document=None, **options):
        document = document or self.document
        return sources.compute(self.repo, self.owner, self.account_id, document, digest(document), {}, **options)

    def store(self, record):
        with self.repo.transaction() as tx: tx.put(sources.KIND, sources.owner_key(self.owner, self.account_id), self.owner, record)

    def served(self, owner=None):
        response = self.request('get', self.route, owner)
        self.assertEqual(response.status_code, 200, response.text)
        # The minimal fixture account is not a full DAPPER document; the served data sources must match their contract.
        api.validate(response.json()['data_sources'], 'AccountDataSources')
        return response.json()['data_sources']

    def test_acceptance_trace_equals_the_stored_source_trace_and_is_served_with_the_account(self):
        record = self.traced()
        self.assertEqual((record['status'], record['account_id'], record['document_sha256']), ('complete', self.account_id, digest(self.document)))
        for letter, claim in zip('abc', record['claims']):
            self.assertEqual(claim['claim_id'], identifier('Claim', letter))
            (item,) = claim['datasets']
            self.assertEqual(item['dataset_id'], identifier('Dataset', letter))
            self.assertEqual(item['via_ids'], sorted([identifier('GeneSet', letter), identifier('Dataset', letter)]))
            self.assertEqual(item['routes'], ['evidence_derivation', 'proposition_reference'])
            self.assertEqual(item['organizations'], [{'organization_id': ORG, 'roles': ['publisher']}])
            self.assertEqual(item['curation_ids'], [])
        self.assertEqual([row['name'] for row in record['datasets']], ['Dataset a', 'Dataset b', 'Dataset c'])
        self.assertEqual(record['organizations'], [{'id': ORG, 'name': 'Dataset publisher'}])
        with self.repo.read_transaction() as tx:
            source = api.scientific_source(tx, self.account_id, self.owner, 'account', None, verify_payload=True)
            exact = sources.trace_source(tx, self.account_id, source, source['document_sha256'])
        self.assertEqual({key: value for key, value in exact.items() if key != 'traced_at'},
                         {key: value for key, value in record.items() if key != 'traced_at'})
        self.assertEqual(self.served(self.owner), {'status': 'not_traced'})
        self.store(record)
        self.assertEqual(self.served(self.owner), {key: record[key] for key in sources.SERVED if key in record})

    def test_a_changed_document_policy_format_or_payload_is_not_traced(self):
        record = self.traced()
        for change in ({'document_sha256': 'f' * 64}, {'policy_version': 'account-provenance-v0'},
                       {'format': 'reveal.account-data-sources/0'}, {'account_payload_sha256': 'e' * 64}):
            self.store({**record, **change})
            self.assertEqual(self.served(self.owner), {'status': 'not_traced'}, change)

    def test_publication_freezes_the_traced_record_for_public_readers(self):
        self.store(self.traced())
        response = self.publish(); self.assertEqual(response.status_code, 200, response.text)
        public = self.served()
        self.assertEqual((public['status'], len(public['claims'])), ('complete', 3))
        # A later re-trace of the owner's account never rewrites what the public snapshot froze.
        self.store({**self.traced(), 'status': 'incomplete'})
        self.assertEqual(self.served()['status'], 'complete')
        self.assertEqual(self.served(self.owner)['status'], 'incomplete')

    def test_a_publication_without_a_matching_trace_serves_not_traced(self):
        self.store({**self.traced(), 'account_payload_sha256': 'e' * 64})
        response = self.publish(); self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.served(), {'status': 'not_traced'})

    def test_tracing_failures_are_recorded_and_never_raise(self):
        for error, code in ((StorageUnavailable('down'), 'STORAGE_UNAVAILABLE'), (Problem(422, 'PROVENANCE_LIMIT_EXCEEDED', 'bounded'), 'PROVENANCE_LIMIT_EXCEEDED'),
                            (DatabaseBusy('busy'), 'DATABASE_BUSY'), (RuntimeError('REVEAL_MYSQL_PASSWORD is required'), 'TRACE_FAILED')):
            with self.subTest(code=code), patch('reveal_backend.provenance_api.trace', side_effect=error):
                record = self.traced()
                self.assertEqual((record['status'], record['error']), ('unavailable', {'code': code}))
                self.assertNotIn('claims', record)
        with patch('reveal_backend.provenance_api.trace', side_effect=lambda *args, **kwargs: time.sleep(1)):
            self.assertEqual(self.traced(deadline=0.05)['error'], {'code': 'TRACE_TIMEOUT'})
        self.assertEqual(sources.compute(self.repo, self.owner, identifier('ScientificAccount', 'z'), self.document, 'd' * 64, {})['error'],
                         {'code': 'TRACE_FAILED'})
        self.store(self.traced())
        with patch('reveal_backend.provenance_api.trace', side_effect=StorageUnavailable('down')):
            self.store(self.traced())
        self.assertEqual(self.served(self.owner), {'status': 'unavailable', 'traced_at': self.served(self.owner)['traced_at'],
                                                   'error': {'code': 'STORAGE_UNAVAILABLE'}})

    def test_input_roles_curation_and_only_public_locations_are_kept(self):
        document = deepcopy(self.document); activity = identifier('Activity', 'x')
        gene_set, dataset, other = document['gene_sets'][0], document['datasets'][0], document['datasets'][1]
        gene_set.pop('was_derived_from'); gene_set['was_generated_by'] = activity
        document['activities'] = [{'id': activity, 'name': 'Convert the library'}]
        document['used_edges'] = [{'subject': activity, 'predicate': 'prov:used', 'object': dataset['id'], 'edge_role': 'metadata_input',
                                   'provenance_source': {'kind': 'curated_lineage', 'curation_id': 'test-curation'}}]
        dataset.update(location='s3://private-bucket/prefix/', was_attributed_to=[ORG], description='Mapping table.')
        other['location'] = 'https://example.org/data'
        record = self.traced(document)
        first = record['claims'][0]['datasets'][0]
        self.assertIn('metadata_input', first['input_roles'])
        self.assertEqual(first['curation_ids'], ['test-curation'])
        self.assertEqual(first['organizations'], [{'organization_id': ORG, 'roles': ['attribution', 'publisher']}])
        described = {row['id']: row for row in record['datasets']}
        self.assertEqual(described[dataset['id']], {'id': dataset['id'], 'name': 'Dataset a', 'description': 'Mapping table.'})
        self.assertEqual(described[other['id']]['location'], 'https://example.org/data')

    def test_a_kpn_mechanism_reaches_curated_reference_datasets_through_its_projections(self):
        # The account names a KPN Mechanism only: identity binding loads its retained projections, and the projected
        # gene set's stored collection lineage, labelled as curated, reaches the dataset and its publisher.
        from types import SimpleNamespace
        import json
        import sqlite3
        import test_research_data as references
        from reveal_backend import provenance_reference
        reference = SimpleNamespace(addCleanup=self.addCleanup)
        references.ReferenceQueryTests.setUp(reference)
        capture = reference.service.query('get_factor', {'factor_id': references.FACTOR}, generation_id=references.GEN)
        mechanism = provenance_reference._compute_mechanism(capture.result['items'][0], capture.source)
        activity, dataset, org = identifier('Activity', 'x'), identifier('Dataset', 'x'), identifier('Organization', 'x')
        used = [{'subject': activity, 'predicate': 'prov:used', 'object': dataset, 'edge_role': 'data_input'}]
        graph = {'activities': [{'id': activity, 'name': 'extraction'}], 'used_edges': used,
                 'datasets': [{'id': dataset, 'name': 'source', 'publisher': org, 'location': 'https://example.org/source'}],
                 'organizations': [{'id': org, 'name': 'publisher', 'ror': 'ror:05a0ya142'}]}
        marker = {'format': 'reveal.lineage-curation/1', 'curation_id': 'test-curation', 'curation_sha256': 'c' * 64, 'used_edges': used}
        with sqlite3.connect(reference.path) as c:
            c.execute('UPDATE cfde_gene_sets SET metadata=? WHERE generation_id=?', (json.dumps({'dapper_gene_set':
                {'id': references.SET, 'name': 'stored set', 'members': ['HGNC.SYMBOL:GENE_A'], 'was_generated_by': activity}}), references.GEN))
            c.execute('UPDATE cfde_gene_set_collections SET payload=? WHERE generation_id=?',
                      (json.dumps({'provenance': graph, 'document_sha256': 'd' * 64, 'lineage_curation': marker}), references.GEN))
        claim, prop = identifier('Claim', 'm'), identifier('Proposition', 'm')
        document = {'scientific_accounts': [dict(self.base_account, id=self.account_id, component_claims=[claim], conclusion_claims=[claim])],
                    'claims': [{'id': claim, 'proposition': prop, 'statement': 'A mechanism claim', 'direction': 'SUPPORTS', 'status': 'proposed'}],
                    'propositions': [{'id': prop, 'subject_entity': mechanism['id'], 'statement': 'The factor is involved.', 'proposition_kind': 'RESULT'}],
                    'mechanisms': [mechanism]}
        connect = lambda *args, **kwargs: references.Connection(reference.path, reference.queries)
        with patch('reveal_backend.runtime_config.reference_mysql_connection', side_effect=connect):
            record = self.traced(document)
            self.install_document(document)
            with self.repo.read_transaction() as tx:
                source = api.scientific_source(tx, self.account_id, self.owner, 'account', None, verify_payload=True)
                exact = sources.trace_source(tx, self.account_id, source, source['document_sha256'])
        self.assertEqual(record['reference_generation_ids'], [references.GEN])
        (connection,) = record['claims'][0]['datasets']
        self.assertEqual({key: value for key, value in connection.items() if key != 'trace_count'},
            {'dataset_id': dataset, 'input_roles': ['data_input'], 'routes': ['factor_projection', 'proposition_reference'],
             'via_ids': [mechanism['id']], 'organizations': [{'organization_id': org, 'roles': ['publisher']}], 'curation_ids': ['test-curation']})
        self.assertEqual(record['datasets'], [{'id': dataset, 'name': 'source', 'location': 'https://example.org/source'}])
        self.assertEqual(record['organizations'], [{'id': org, 'name': 'publisher', 'ror': 'ror:05a0ya142'}])
        self.assertEqual(record['via'], [{'id': mechanism['id'], 'name': mechanism['name'], 'kind': 'mechanism'}])
        self.assertEqual({key: value for key, value in exact.items() if key != 'traced_at'},
                         {key: value for key, value in record.items() if key != 'traced_at'})

    def test_withheld_dependencies_drop_only_the_connections_they_carry(self):
        self.store(self.traced())
        withheld = {identifier('GeneSet', 'a'), identifier('Dataset', 'a'), identifier('Claim', 'b')}
        with self.repo.read_transaction() as tx:
            source = api.scientific_source(tx, self.account_id, self.owner, 'account', None)
            view = sources.response(tx, self.account_id, {**source, 'withheld_dependencies': True, 'withheld_ids': withheld})
        self.assertEqual(view['status'], 'incomplete')
        self.assertEqual([claim['claim_id'] for claim in view['claims']], [identifier('Claim', 'a'), identifier('Claim', 'c')])
        self.assertEqual(view['claims'][0]['datasets'], [])
        self.assertEqual([row['id'] for row in view['datasets']], [identifier('Dataset', 'c')])
        self.assertTrue(withheld.isdisjoint(row['id'] for row in view['via']))


class ContractTests(unittest.TestCase):
    def test_data_sources_are_an_optional_closed_part_of_the_account_result(self):
        import test_reference_contract as contract
        result = contract.SCHEMAS['AccountResult']
        self.assertEqual(result['properties']['data_sources'], {'$ref': '#/components/schemas/AccountDataSources'})
        self.assertNotIn('data_sources', result['required'])
        self.assertEqual(contract.errors({'status': 'not_traced'}, 'AccountDataSources'), [])
        self.assertTrue(contract.errors({'status': 'not_traced', 'owner': 'x'}, 'AccountDataSources'))
        self.assertTrue(contract.errors({'status': 'pending'}, 'AccountDataSources'))
        stored = sources.stored(identifier('ScientificAccount', 'a'), 'd' * 64, 'p' * 64, {'status': 'unavailable', 'error': {'code': 'TRACE_TIMEOUT'}})
        self.assertEqual(contract.errors({key: stored[key] for key in sources.SERVED if key in stored}, 'AccountDataSources'), [])


if __name__ == '__main__':
    unittest.main()
