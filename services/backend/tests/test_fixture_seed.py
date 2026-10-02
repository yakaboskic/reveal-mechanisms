"""Canonical fixture integrity and complete local seeding through normal read APIs."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient
import jwt

from reveal_backend import app as api
from reveal_backend.account_discovery import visible_accounts
from reveal_backend.catalog import Catalog
from reveal_backend.fixture_seed import apply_seed, dry_run, load_fixture, verify_seed, LOCAL_PREFIX
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.runtime_config import ROOT


class MemoryS3:
    """Content-addressed immutable storage double; no AWS calls."""
    def __init__(self):
        self.contents = {}; self.uploads = 0

    def put(self, data, content_type):
        checksum = hashlib.sha256(data).hexdigest()
        if checksum not in self.contents:
            self.contents[checksum] = data; self.uploads += 1
        return {'store': 's3', 'bucket': 'fixture-test-only', 'key': 'local/artifacts/sha256/' + checksum[:2] + '/' + checksum,
                'version_id': 'version-' + checksum[:12], 'sha256': checksum, 'size_bytes': len(data), 'content_type': content_type}

    def get(self, reference):
        return self.contents[reference['sha256']]

    def download_url(self, reference, filename, content_type):
        self.get(reference)
        return 'https://fixture-test-only.invalid/' + reference['key'] + '?versionId=' + reference['version_id']


class FixtureSeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_fixture(ROOT / 'data/fixtures/bubble-account-v1')
        contract = json.loads((ROOT / 'services/frontend/src/lib/fixtures/contract.json').read_text())
        cls.gap = deepcopy(next(gap for gap in contract['gaps']['items'] if gap['object']['id'] == cls.fixture['manifest']['selected_gap']['id']))
        cls.gap['source'].update({key: cls.fixture['manifest']['selected_gap'][key] for key in ('source_id', 'source_revision')})

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.repo = Repository(str(Path(temp.name) / 'app.sqlite'), table_prefix=LOCAL_PREFIX); self.repo.migrate()
        self.owner, self.other = uid(), uid()
        with self.repo.transaction() as tx:
            for owner in (self.owner, self.other):
                tx.put('principal', owner, owner, {'retired': False, 'me': {'user_id': owner,
                    'principal_kind': 'registered', 'display_name': 'Fixture test owner', 'workspace_expires_at': None}})
        self.catalog = Catalog(); self.catalog.loaded = True; self.catalog.dismech_import = 'fixture'
        self.catalog.gaps = {self.gap['object']['id']: deepcopy(self.gap)}
        self.catalog.by_source = {self.gap['source']['source_id']: self.catalog.gaps[self.gap['object']['id']]}
        self.storage = MemoryS3()
        for context in (patch.object(api, 'repo', self.repo), patch.object(api, 'catalog', self.catalog),
                        patch('reveal_backend.artifact_store.store', return_value=self.storage),
                        patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 40, 'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs',
                            'REVEAL_GATEWAY_AUDIENCE': 'reveal-api', 'REVEAL_ARTIFACT_STORE': 's3'})):
            context.start(); self.addCleanup(context.stop)
        self.client = TestClient(api.app)

    def headers(self, owner):
        current = int(time.time())
        return {'Authorization': 'Bearer ' + jwt.encode({'sub': owner, 'principal_kind': 'registered', 'iss': 'reveal-nextjs',
            'aud': 'reveal-api', 'iat': current, 'exp': current + 120, 'jti': uid()}, 's' * 40, algorithm='HS256')}

    def seed(self, owner=None):
        return apply_seed(self.repo, owner or self.owner, self.fixture, self.catalog, self.storage)

    def snapshot(self):
        with self.repo.read_transaction() as tx:
            return tx.execute('SELECT kind,id,owner_id,version,payload FROM reveal_records ORDER BY kind,id').fetchall()

    def test_fixture_is_complete_and_datasets_are_claim_specific(self):
        doc = self.fixture['document']; manifest = self.fixture['manifest']
        self.assertEqual(len(doc['claims']), 12)
        self.assertGreaterEqual(len(doc['datasets']), 3); self.assertGreaterEqual(len(doc['files']), 6)
        self.assertEqual(manifest['scientific_acceptance'], 'not_reviewed')
        self.assertEqual(manifest['selected_gap']['source_revision'], manifest['source_binding']['source_file_sha256'])
        source_bytes = (self.fixture['root'] / 'sources' / Path(manifest['source_binding']['source_file']).name).read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), manifest['selected_gap']['source_revision'])
        self.assertFalse(self.fixture['validation']['scientific_grounding_evaluated'])
        datasets = {node['id']: node for node in doc['datasets']}
        evidence = {node['id']: node for node in doc['evidence_items']}
        per_claim = [{source for item in claim['has_evidence'] for source in evidence[item]['was_derived_from'] if source in datasets} for claim in doc['claims']]
        self.assertTrue(all(per_claim)); self.assertGreater(max(map(len, per_claim)), 1)
        self.assertTrue(any(sum(identity in sources for sources in per_claim) > 1 for identity in datasets))
        self.assertTrue(any(node['direction'] == 'NEUTRAL' for node in evidence.values()))
        self.assertEqual({item['id'] for item in manifest['files']}, {ref for node in datasets.values() for ref in node['has_file']})

    def test_dry_run_has_no_writes_and_refuses_shared_target_or_wrong_gap(self):
        before = self.snapshot()
        result = dry_run(self.repo, self.owner, self.fixture, self.catalog)
        self.assertEqual(result['writes'], 0); self.assertEqual(self.storage.uploads, 0); self.assertEqual(before, self.snapshot())
        self.repo.table_prefix = 'reveal'
        with self.assertRaisesRegex(ValueError, 'isolated'): self.seed()
        self.repo.table_prefix = LOCAL_PREFIX
        self.catalog.gaps[self.gap['object']['id']]['source']['source_revision'] = '0' * 64
        with self.assertRaises(Exception): self.seed()
        self.assertEqual(self.storage.uploads, 0); self.assertEqual(before, self.snapshot())

    def test_complete_seed_reads_through_account_claim_dataset_file_and_citation_apis(self):
        receipt = self.seed(); manifest = self.fixture['manifest']; owner_headers = self.headers(self.owner)
        account_id = manifest['account_id']
        response = self.client.get('/v1/accounts/' + quote(account_id, safe=''), headers=owner_headers)
        self.assertEqual(response.status_code, 200, response.text)
        account = response.json()
        self.assertEqual(account['research_statement']['status'], 'not_requested')
        self.assertEqual(len(account['document']['claims']), 12)
        self.assertEqual(len(account['document']['datasets']), 4)
        for group, route in [('claims', 'claims'), ('datasets', 'objects'), ('files', 'objects')]:
            for node in self.fixture['document'][group]:
                result = self.client.get('/v1/' + route + '/' + quote(node['id'], safe=''), headers=owner_headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['root_id'], node['id'])
        for source in manifest['files']:
            download = self.client.get('/v1/artifacts/' + source['sha256'], headers=owner_headers, follow_redirects=False)
            self.assertEqual(download.status_code, 307, download.text)
            self.assertIn('versionId=', download.headers['location'])
        for claim in self.fixture['document']['claims']:
            citation = self.client.get('/v1/citations/' + quote(claim['id'], safe=''), headers=owner_headers)
            self.assertEqual(citation.status_code, 200, citation.text)
            self.assertIn('unreviewed', citation.json()['repository'])
        for owner in (None, self.other):
            denied = self.client.get('/v1/accounts/' + quote(account_id, safe=''), headers=self.headers(owner) if owner else {})
            self.assertEqual(denied.status_code, 404, denied.text)
        listing = self.client.get('/v1/accounts', headers=owner_headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual([item['account']['id'] for item in listing.json()['items']], [account_id])
        gap_accounts = self.client.get('/v1/knowledge-gaps/' + quote(manifest['selected_gap']['id'], safe='') + '/accounts?scope=workspace', headers=owner_headers)
        self.assertEqual(gap_accounts.status_code, 200, gap_accounts.text)
        self.assertEqual(gap_accounts.json()['items'][0]['job_id'], None)
        with self.repo.read_transaction() as tx:
            for kind in ('job', 'request', 'outbox', 'publication', 'draft'):
                self.assertEqual(tx.list(kind), [], kind)
            self.assertEqual(visible_accounts(tx, self.owner, attribution=True)[0]['attribution'], None)
        self.assertTrue(verify_seed(self.repo, self.owner, self.fixture, self.catalog, self.storage)['verified'])
        self.assertTrue(receipt['inserted_records'])

    def test_repeat_and_second_owner_do_not_duplicate_uploads_or_overwrite_shared_records(self):
        self.seed(); before = self.snapshot(); first_uploads = self.storage.uploads
        replay = self.seed(); self.assertTrue(replay['verified'])
        self.assertEqual(before, self.snapshot()); self.assertEqual(first_uploads, self.storage.uploads)
        second = self.seed(self.other)
        self.assertEqual(first_uploads, self.storage.uploads)
        self.assertTrue(any(item['kind'] == 'citation' for item in second['reused_records']))
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('account_membership', self.owner)), 1)
            self.assertEqual(len(tx.list('account_membership', self.other)), 1)
            for item in before:
                if item[0] == 'citation':
                    current = tx.get(item[0], item[1]); self.assertEqual(current['version'], item[3]); self.assertEqual(current['data'], json.loads(item[4]))

    def test_unrelated_account_is_never_overwritten_and_storage_failure_never_commits(self):
        key = digest([self.owner, self.fixture['manifest']['account_id']])
        with self.repo.transaction() as tx: tx.put('account', key, self.owner, {'unrelated': True})
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'overwrite'): self.seed()
        self.assertEqual(before, self.snapshot()); self.assertEqual(self.storage.uploads, 0)
        with self.repo.transaction() as tx: tx.remove('account', key)
        before = self.snapshot()
        with patch.object(self.storage, 'get', return_value=b'corrupt'):
            with self.assertRaisesRegex(ValueError, 'readback'): self.seed()
        self.assertEqual(before, self.snapshot())

    def test_verification_detects_missing_record_and_corrupt_retained_bytes(self):
        receipt = self.seed()
        checksum = self.fixture['manifest']['files'][0]['sha256']
        self.storage.contents[checksum] = b'corrupt'
        with self.assertRaisesRegex(ValueError, 'checksum'): verify_seed(self.repo, self.owner, self.fixture, self.catalog, self.storage)
        with self.repo.transaction() as tx:
            tx.remove('grant', next(item['id'] for item in receipt['inserted_records'] if item['kind'] == 'grant'))
        with self.assertRaisesRegex(ValueError, 'missing'): verify_seed(self.repo, self.owner, self.fixture, self.catalog, self.storage)

    def test_fixture_and_normal_account_discovery_handle_null_and_real_job_ids(self):
        self.seed()
        with self.repo.transaction() as tx:
            summary = deepcopy(tx.list('account_membership', self.owner)[0]['data']['summary'])
            summary['account']['id'] = 'dapper:ScientificAccount.' + 'a' * 32
            summary['job_id'] = uid(); summary.pop('fixture_origin')
            tx.put('account_membership', digest([self.owner, summary['account']['id']]), self.owner,
                   {'account_id': summary['account']['id'], 'summary': summary})
        with self.repo.read_transaction() as tx:
            records = visible_accounts(tx, self.owner, attribution=True)
            self.assertEqual(len(records), 2)
            self.assertTrue(any(record['job_id'] is None for record in records))
            self.assertTrue(all(record['attribution'] is None for record in records))

    def test_missing_or_retired_owner_is_rejected_before_retention(self):
        with self.assertRaisesRegex(ValueError, 'unavailable'): self.seed(uid())
        with self.repo.transaction() as tx:
            principal = tx.get('principal', self.owner)['data']; principal['retired'] = True
            tx.put('principal', self.owner, self.owner, principal)
        with self.assertRaisesRegex(ValueError, 'unavailable'): self.seed()
        self.assertEqual(self.storage.uploads, 0)


if __name__ == '__main__': unittest.main()
