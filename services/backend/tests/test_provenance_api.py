"""Account provenance authorization, immutable source selection and stable trace pages."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from reveal_backend import app as api
from reveal_backend.auth import Problem
from reveal_backend.provenance_api import paginate, artifact_records
from reveal_backend.repository import digest
from reveal_backend.evidence_package import canonical_json, sha256
import test_publication as publication_tests


def identifier(kind, character):
    return 'dapper:' + kind + '.' + character * 32


class ProvenanceApiTests(unittest.TestCase):
    principal = publication_tests.PublicationTests.principal
    headers = publication_tests.PublicationTests.headers
    seed = publication_tests.PublicationTests.seed
    request = publication_tests.PublicationTests.request
    publish = publication_tests.PublicationTests.publish

    def setUp(self):
        publication_tests.PublicationTests.setUp(self)
        self.provenance_route = self.route + '/provenance'
        self.document = {'scientific_accounts': [dict(self.base_account, id=self.account_id)],
                         'claims': [], 'propositions': [], 'evidence_items': [],
                         'gene_sets': [], 'datasets': [], 'organizations': []}
        org = identifier('Organization', 'o')
        self.document['organizations'] = [{'id': org, 'name': 'Dataset publisher'}]
        for n, letter in enumerate('abc'):
            claim, prop, ev, gs, ds = [identifier(kind, letter) for kind in
                                      ('Claim', 'Proposition', 'EvidenceItem', 'GeneSet', 'Dataset')]
            self.document['claims'].append({'id': claim, 'proposition': prop, 'has_evidence': [ev],
                                            'statement': 'A scientific claim', 'direction': 'SUPPORTS', 'status': 'proposed'})
            self.document['propositions'].append({'id': prop, 'subject_entity': gs,
                'relation': 'http://www.w3.org/ns/prov#wasDerivedFrom', 'object_entity': ds,
                'statement': 'A source dataset is used.', 'proposition_kind': 'RESULT'})
            self.document['evidence_items'].append({'id': ev, 'target_proposition': prop,
                'direction': 'SUPPORTS', 'was_derived_from': [gs]})
            self.document['gene_sets'].append({'id': gs, 'name': 'Source gene set', 'was_derived_from': [ds]})
            self.document['datasets'].append({'id': ds, 'name': 'Dataset ' + letter, 'publisher': org})
        self.document['scientific_accounts'][0]['component_claims'] = [row['id'] for row in self.document['claims']]
        self.document['scientific_accounts'][0]['conclusion_claims'] = [self.document['claims'][-1]['id']]
        self.install_document(self.document)

    def install_document(self, document, *, clipped=False):
        account = document['scientific_accounts'][0]
        envelope = {'root_id': self.account_id, 'document': deepcopy(document), 'citation_metadata': [], 'artifacts': [],
                    'payloads': [{'object_id': node['id'], 'payload_sha256': sha256(canonical_json(node))}
                        for rows in document.values() for node in rows if node.get('id')]}
        if clipped:
            envelope['document'] = {'scientific_accounts': [account]}
            envelope['coverage'] = {'complete': False}
        with self.repo.transaction() as tx:
            key = digest([self.owner, self.account_id])
            row = tx.get('account', key)['data']
            row['result'] = envelope
            row['summary']['account'] = account
            row['summary']['research_statement'] = {'status': 'not_requested', 'job_id': None, 'paragraph_id': None}
            tx.put('account', key, self.owner, row)
            tx.put('account_membership', key, self.owner, {'account_id': self.account_id, 'summary': row['summary']})
            sha = digest(document)
            tx.put('object_document', key, self.owner, {'sha256': sha})
            tx.put('scientific_document', digest([self.owner, sha]), self.owner,
                   {'document': deepcopy(document), 'citation_metadata': [], 'artifact_access': {}})

    def test_owner_only_until_published_and_withdrawal_revokes(self):
        self.assertEqual(self.client.get(self.provenance_route).status_code, 404)
        self.assertEqual(self.request('get', self.provenance_route, self.other).status_code, 404)
        result = self.request('get', self.provenance_route, self.owner)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers['cache-control'], 'private, no-store')
        self.assertIn('Authorization', result.headers['vary'])
        self.assertEqual(len(result.json()['dataset_reuse']), 3)
        published = self.publish()
        self.assertEqual(published.status_code, 200, published.text)
        public = self.client.get(self.provenance_route)
        self.assertEqual(public.status_code, 200, public.text)
        self.assertNotIn(self.owner, public.text)
        self.assertNotIn(self.job_id, public.text)
        self.assertEqual(public.json()['dataset_reuse'], result.json()['dataset_reuse'])
        self.assertEqual(self.client.get(self.provenance_route, headers={'Authorization': 'Bearer invalid'}).status_code, 401)
        self.assertEqual(self.publish(visibility='private', version=1).status_code, 200)
        self.assertEqual(self.client.get(self.provenance_route).status_code, 404)

    def test_reads_full_document_and_keeps_original_records(self):
        self.install_document(self.document, clipped=True)
        result = self.request('get', self.provenance_route, self.owner)
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()
        self.assertEqual(value['account']['record'], self.document['scientific_accounts'][0])
        self.assertEqual([row['record'] for row in value['propositions']], self.document['propositions'])
        self.assertEqual(len(value['dataset_reuse']), 3)
        checksum = value['snapshot']['account_payload_sha256']
        self.assertEqual(self.request('get', self.provenance_route + '?payload_sha256=' + checksum, self.owner).status_code, 200)
        self.assertEqual(self.request('get', self.provenance_route + '?payload_sha256=' + '0' * 64, self.owner).status_code, 404)
        with self.repo.read_transaction() as tx:
            stored = tx.get('scientific_document', digest([self.owner, digest(self.document)]))['data']['document']
        self.assertEqual(stored, self.document)

    def test_loaded_source_must_match_the_selected_payload_observation(self):
        original_payload = sha256(canonical_json(self.document['scientific_accounts'][0]))
        changed = deepcopy(self.document)
        changed['scientific_accounts'][0]['has_embedding'] = [identifier('Embedding', 'z')]
        sha = digest(changed)
        with self.repo.transaction() as tx:
            tx.put('object_document', digest([self.owner, self.account_id]), self.owner, {'sha256': sha})
            tx.put('scientific_document', digest([self.owner, sha]), self.owner,
                   {'document': changed, 'citation_metadata': [], 'artifact_access': {}})
        response = self.request('get', self.provenance_route, self.owner,
                                params={'payload_sha256': original_payload})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()['code'], 'PAYLOAD_NOT_FOUND')
        response = self.request('get', self.provenance_route, self.owner)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn('dataset_reuse', response.json())

    def test_trace_pagination_keeps_totals_and_invalidates_on_changes(self):
        first = self.request('get', self.provenance_route + '?limit=1', self.owner)
        self.assertEqual(first.status_code, 200, first.text)
        value = first.json()
        self.assertEqual(len(value['traces']), 1)
        cursor = value['page']['next_cursor']
        self.assertTrue(cursor)
        second = self.request('get', self.provenance_route, self.owner, params={'cursor': cursor})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotEqual(second.json()['traces'][0]['id'], value['traces'][0]['id'])
        self.assertEqual(second.json()['dataset_reuse'], value['dataset_reuse'])
        self.assertEqual(second.json()['organization_reuse'], value['organization_reuse'])
        self.assertEqual(self.request('get', self.provenance_route, self.owner,
            params={'cursor': cursor, 'limit': 2}).status_code, 409)
        self.assertEqual(self.request('get', self.provenance_route, self.owner,
            params={'cursor': cursor + 'x'}).status_code, 409)
        changed = deepcopy(self.document)
        changed['datasets'][0]['name'] = 'Changed observed source'
        self.install_document(changed)
        self.assertEqual(self.request('get', self.provenance_route, self.owner, params={'cursor': cursor}).status_code, 409)

    def test_invalid_limits_and_response_budget_have_no_rankings(self):
        for limit in ('0', '251', 'abc'):
            self.assertEqual(self.request('get', self.provenance_route, self.owner, params={'limit': limit}).status_code, 422)
        with patch('reveal_backend.provenance_api.MAX_RESPONSE_BYTES', 1):
            response = self.request('get', self.provenance_route, self.owner)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['code'], 'PROVENANCE_LIMIT_EXCEEDED')
        self.assertNotIn('dataset_reuse', response.json())

    def test_storage_failure_does_not_become_zero_counts(self):
        from reveal_backend.artifact_store import StorageUnavailable
        with patch('reveal_backend.provenance_reference.expand', side_effect=StorageUnavailable('offline')):
            response = self.request('get', self.provenance_route, self.owner)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn('dataset_reuse', response.json())

    def test_missing_complete_source_is_not_a_truncated_ranking(self):
        self.install_document(self.document, clipped=True)
        with self.repo.transaction() as tx:
            key = digest([self.owner, self.account_id])
            tx.put('object_document', key, self.owner, {'sha256': '0' * 64})
        response = self.request('get', self.provenance_route, self.owner)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['code'], 'PROVENANCE_SOURCE_UNAVAILABLE')
        self.assertNotIn('dataset_reuse', response.json())

    def test_response_conforms_to_generated_contract(self):
        self.install_document(deepcopy(self.fixture['account']['document']))
        response = self.request('get', self.provenance_route, self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        api.validate(response.json(), 'AccountProvenance')

    def test_cursor_secret_must_be_configured(self):
        import os
        with patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': ''}):
            with self.assertRaises(Problem) as caught:
                paginate({}, {}, [], {})
        self.assertEqual(caught.exception.status, 503)

    def test_public_artifact_sources_do_not_read_publisher_private_rows(self):
        file = {'id': identifier('File', 'f'), 'sha256': 'a' * 64}
        trusted = {'sha256': 'a' * 64, 'file': file, 'research_source': {}}
        class NoPrivateReads:
            def get_many(self, *args): raise AssertionError('public read entered private artifact lookup')
        source = {'document': {'files': [file]}, 'public': {'id': 'publication'},
                  'snapshot': {'artifact_records': {'a' * 64: trusted, 'b' * 64: {'private': True}}}}
        self.assertEqual(artifact_records(NoPrivateReads(), source), {'a' * 64: trusted})

    def test_revoked_dependency_is_not_rehydrated_or_identified_in_diagnostics(self):
        removed = self.document['gene_sets'][0]['id']
        def visible(tx, owner, document):
            value = deepcopy(document)
            value['gene_sets'] = value['gene_sets'][1:]
            return value
        from reveal_backend import provenance_reference
        with patch('reveal_backend.scientific_reuse.readable_document', side_effect=visible), \
             patch('reveal_backend.provenance_reference.expand', wraps=provenance_reference.expand) as expand:
            response = self.request('get', self.provenance_route, self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(expand.call_args.kwargs['blocked_ids'], {removed})
        result = response.json()
        self.assertTrue(result['coverage']['counts_are_lower_bounds'])
        self.assertNotIn(removed, json.dumps(result['coverage']['issues']))
        self.assertFalse(any(node['id'] == removed for node in result['nodes']))


TRAIT = 'KPN.TRAIT:0000398'
RELEASE = 'e' * 64
ARTIFACT = 'dig-open-bottom-line-analysis-stg_bottom-line_Mixed_T2D.sumstats.tsv.gz'


class Leases:
    """The application connections a Repository has open (sqlite connections behind a counting proxy)."""
    def __init__(self, repo):
        self.open, connect = 0, repo.connect
        leases = self
        class Proxy:
            def __init__(self, connection): self._connection = connection
            def __getattr__(self, name): return getattr(self._connection, name)
            def close(self):
                leases.open -= 1; return self._connection.close()
        def counted():
            self.open += 1; return Proxy(connect())
        self.patch = patch.object(repo, 'connect', counted)


class TracePhaseTests(unittest.TestCase):
    """The route in three phases: no lease while the portal is awaited, a portal outage is coverage, a change is 409."""
    principal, headers, seed = ProvenanceApiTests.principal, ProvenanceApiTests.headers, ProvenanceApiTests.seed
    request, publish, install_document = ProvenanceApiTests.request, ProvenanceApiTests.publish, ProvenanceApiTests.install_document

    def setUp(self):
        ProvenanceApiTests.setUp(self)
        self.document['propositions'][0]['object_entity'] = TRAIT
        self.install_document(self.document)
        self.leases = Leases(self.repo); self.leases.patch.start(); self.addCleanup(self.leases.patch.stop)
        from test_provenance_portal import bottom_line_document
        self.bottom_line, (self.published, *_) = bottom_line_document()
        from reveal_backend import provenance_portal
        provenance_portal._CACHE.clear(); self.addCleanup(provenance_portal._CACHE.clear)
        def expand(document, records, *, tx, blocked_ids=(), account_id=None, archived=None):
            self.assertEqual(self.leases.open, 0)  # phase 1b: no application lease is held
            expanded = deepcopy(document)
            expanded['traits'] = [{'id': TRAIT, 'name': 'Type 2 diabetes', 'kpn_trait_id': TRAIT,
                                   'href': 'https://portal.test/artifact_provenance/bottom_line/T2D'}]
            return expanded, {'reference_generation_ids': [RELEASE], 'issues': [], 'source_observations': [], 'supplement_ids': [],
                              'retained_projection_coverage': [], 'recovery_status': [], 'blocked_reference_ids': [],
                              'traits': {TRAIT: {'kpn_trait_id': TRAIT, 'portal_trait_id': 'T2D', 'gwas_reason': None,
                                                 'gwas_artifact': {'ancestry_code': 'Mixed', 'ancestry_name': 'Mixed', 'artifact_id': ARTIFACT}}}}
        self.expand = expand
        expanding = patch('reveal_backend.provenance_reference.expand', side_effect=expand); expanding.start(); self.addCleanup(expanding.stop)

    def portal(self, handler):
        import httpx
        from reveal_backend import provenance_portal
        async def handle(request):
            self.assertEqual(self.leases.open, 0)  # phase 2: no database lease while the portal is awaited
            return handler(request)
        return [patch.dict(os.environ, {provenance_portal.PORTAL_ENV: 'https://portal.test/artifact_provenance'}),
                patch.object(provenance_portal, '_new_client', lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))]

    def trace(self, handler, *extra):
        contexts = self.portal(handler) + list(extra)
        for context in contexts: context.start()
        try: return self.request('get', self.provenance_route, self.owner)
        finally:
            for context in reversed(contexts): context.stop()

    def test_the_trait_gwas_route_reaches_the_portal_dataset(self):
        import httpx
        response = self.trace(lambda request: httpx.Response(200, json=self.bottom_line))
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        gwas = next(row for row in value['dataset_reuse'] if row['dataset_id'] == self.published)
        self.assertEqual((gwas['claim_count'], gwas['classification_claim_counts']['trait_gwas']), (1, 1))
        nodes = {node['id']: node for node in value['nodes']}
        self.assertEqual(nodes[self.published]['href'], 'https://portal.test/artifact_provenance/artifact/' + ARTIFACT)
        self.assertEqual(nodes[TRAIT]['kind'], 'trait')
        self.assertEqual(value['coverage']['status'], 'complete')
        self.assertEqual(value['coverage']['upstream_gaps'], [{'route': 'trait_gwas', 'code': 'no_upstream_organizations', 'object_ids': [self.published]}])
        portal = [item for item in value['snapshot']['source_observations'] if item['kind'] == 'portal_bottom_line']
        self.assertEqual([item['key'] for item in portal], ['https://portal.test/artifact_provenance/get_provenance?id=' + ARTIFACT])
        self.assertEqual(value['snapshot']['policy_version'], 'account-provenance-v2')
        self.assertEqual(value['snapshot']['reference_generation_ids'], [RELEASE])
        self.assertEqual(len([edge for edge in value['edges'] if edge['relation'] == 'portal_transitive_input']), 2)

    def test_a_mechanism_route_response_conforms_to_the_generated_contract(self):
        import httpx
        document = deepcopy(self.fixture['account']['document'])
        mechanisms = [node['id'] for node in document.get('mechanisms', [])]
        self.assertTrue(mechanisms)
        self.install_document(document)
        def fitted(document, records, **kwargs):
            expanded, metadata = self.expand(document, records, **kwargs)
            expanded['fitted_on_trait_edges'] = [{'subject': identity, 'predicate': 'reveal:fittedOnTrait', 'object': TRAIT} for identity in mechanisms]
            return expanded, metadata
        with patch('reveal_backend.provenance_reference.expand', side_effect=fitted):
            response = self.trace(lambda request: httpx.Response(200, json=self.bottom_line))
        self.assertEqual(response.status_code, 200, response.text)
        api.validate(response.json(), 'AccountProvenance')
        self.assertTrue(any(row['dataset_id'] == self.published and row['classification_claim_counts']['trait_gwas']
                            for row in response.json()['dataset_reuse']))

    def test_a_portal_outage_is_an_incomplete_trace_not_an_error(self):
        import httpx
        def down(request): raise httpx.ConnectError('unreachable', request=request)
        for unset in (False, True):
            with self.subTest(portal_disabled=unset):
                extra = [patch.dict(os.environ, {'REVEAL_PROVENANCE_PORTAL_URL': ''})] if unset else []
                response = self.trace(down, *extra)
                self.assertEqual(response.status_code, 200, response.text)
                value = response.json()
                self.assertEqual(value['coverage']['status'], 'incomplete')
                self.assertIn({'code': 'external_provenance_unavailable', 'object_id': TRAIT}, value['coverage']['issues'])
                self.assertEqual(len(value['dataset_reuse']), 3)  # the other routes are unaffected
                self.assertEqual(value['propositions'][0]['provenance']['resolution'], 'incomplete')
                self.assertFalse(any(item['kind'] == 'portal_bottom_line' for item in value['snapshot']['source_observations']))

    def test_changes_while_the_portal_is_read_expire_the_trace(self):
        import httpx
        removed = self.document['gene_sets'][1]['id']
        revoked = []
        def visible(tx, owner, document):
            value = deepcopy(document)
            if revoked: value['gene_sets'] = [node for node in value['gene_sets'] if node['id'] != removed]
            return value
        def revoke(request):
            revoked.append(True)
            return httpx.Response(200, json=self.bottom_line)
        response = self.trace(revoke, patch('reveal_backend.scientific_reuse.readable_document', side_effect=visible))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['code'], 'CURSOR_EXPIRED')
        self.assertNotIn('dataset_reuse', response.json())
        changed = deepcopy(self.document); changed['datasets'][0]['name'] = 'Changed during the portal read'
        from reveal_backend import provenance_portal
        provenance_portal._CACHE.clear()  # the graph above was cached: read the portal again
        def replace(request):
            self.install_document(changed)
            return httpx.Response(200, json=self.bottom_line)
        response = self.trace(replace)
        self.assertEqual(response.status_code, 409, response.text)

    def test_the_trace_and_its_portal_fetch_write_no_file(self):
        import builtins, tempfile, httpx
        writes, real_open, real_os_open = [], builtins.open, os.open
        def guarded(file, mode='r', *args, **kwargs):
            if any(flag in mode for flag in 'wax+'): writes.append(file); raise AssertionError(f'write to {file}')
            return real_open(file, mode, *args, **kwargs)
        def guarded_os(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC): writes.append(path); raise AssertionError(f'write to {path}')
            return real_os_open(path, flags, *args, **kwargs)
        def refused(*args, **kwargs): writes.append('tempfile'); raise AssertionError('temporary file')
        guards = [patch('builtins.open', guarded), patch('io.open', guarded), patch('os.open', guarded_os)] + [
            patch.object(tempfile, name, refused) for name in ('NamedTemporaryFile', 'TemporaryFile', 'SpooledTemporaryFile', 'mkstemp', 'mkdtemp')]
        from pathlib import Path
        with patch('builtins.open', guarded), patch('io.open', guarded), self.assertRaises(AssertionError):
            (Path(self.repo.sqlite_path).parent / 'probe').write_text('the guards see pathlib writes')
        writes.clear()
        response = self.trace(lambda request: httpx.Response(200, json=self.bottom_line), *guards)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(writes, [])
        self.assertTrue(any(row['classification_claim_counts']['trait_gwas'] for row in response.json()['dataset_reuse']))
