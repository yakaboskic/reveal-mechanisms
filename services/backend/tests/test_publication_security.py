"""Offline adversarial publication boundaries; synthetic accepted records, no live publish."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import jwt

from reveal_backend import app as api, publication
from reveal_backend.acceptance import object_envelope
from reveal_backend.auth import Problem
from reveal_backend.citations import register, revise
from reveal_backend.repository import Repository, digest, uid

ACCOUNT = 'dapper:ScientificAccount.' + 'a' * 32
SECOND = 'dapper:ScientificAccount.' + 'b' * 32
CLAIM = 'dapper:Claim.' + 'c' * 32
PRIVATE = 'dapper:Claim.' + 'd' * 32
FILE = 'dapper:File.' + 'f' * 32
PRIVATE_FILE = 'dapper:File.' + 'e' * 32
GAP = 'dapper:KnowledgeGap.' + 'g' * 32
PARAGRAPH = 'dapper:Paragraph.' + 'p' * 32
NEXT_PARAGRAPH = 'dapper:Paragraph.' + 'q' * 32
STAMP = '2026-09-28T12:00:00Z'


class PublicationSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = Repository(str(self.root / 'publication.sqlite')); self.repo.migrate()
        for context in (patch.object(api, 'repo', self.repo),
                        patch.object(api, 'artifacts_root', return_value=self.root.resolve()),
                        patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 40,
                            'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE': 'reveal-api'})):
            context.start(); self.addCleanup(context.stop)
        self.client = TestClient(api.app)
        self.owner = self.principal('anonymous'); self.other = self.principal('registered')
        self.job_id, self.request_id, self.draft_id = uid(), uid(), uid()
        self.attribution = {'user_id': self.owner, 'person_id': None, 'principal_kind': 'anonymous',
            'display_name': 'Original anonymous author', 'orcid': None, 'orcid_authenticated': False, 'observed_at': STAMP}
        self.source_bytes = b'{"publicly_cited_row":7}'
        self.private_bytes = b'{"unrelated_private_capture":31}'
        self.source_sha = hashlib.sha256(self.source_bytes).hexdigest()
        self.private_sha = hashlib.sha256(self.private_bytes).hexdigest()
        source = {'id': FILE, 'filename': 'cited.json', 'mime_type': 'application/json', 'sha256': self.source_sha, 'size_in_bytes': len(self.source_bytes)}
        private = {'id': PRIVATE_FILE, 'filename': 'unrelated.json', 'mime_type': 'application/json', 'sha256': self.private_sha, 'size_in_bytes': len(self.private_bytes)}
        self.document = {
            'scientific_accounts': [{'id': ACCOUNT, 'question': GAP, 'component_claims': [CLAIM], 'closing_remarks': 'A bounded test conclusion.'}],
            'knowledge_gaps': [{'id': GAP, 'text': 'A synthetic access-control question.'}],
            'claims': [{'id': CLAIM, 'statement': 'A synthetic cited observation.', 'was_derived_from': [FILE]},
                       {'id': PRIVATE, 'statement': 'Unrelated private claim.', 'was_derived_from': [PRIVATE_FILE]}],
            'files': [source, private],
            'paragraphs': [{'id': PARAGRAPH, 'text': 'Observed.', 'citations': [
                {'target_id': CLAIM, 'citation_metadata_revision': 1, 'start': 0, 'end': 8, 'exact_text': 'Observed'}]}]}
        self.access = {item['id']: {'file': item, 'download_url': '/v1/artifacts/' + item['sha256'],
            'expires_at': None, 'availability': 'available', 'verification': 'checksum_verified'} for item in (source, private)}
        with self.repo.transaction() as tx:
            self.metadata = register(tx, self.owner, self.document, self.attribution, STAMP)
            for file, data in ((source, self.source_bytes), (private, self.private_bytes)):
                path = self.root / file['filename']; path.write_bytes(data)
                tx.put('artifact', digest([self.owner, file['sha256']]), self.owner,
                    {'sha256': file['sha256'], 'path': str(path), 'file': file})
            self.seed_document(tx, self.owner, self.document, ACCOUNT, PARAGRAPH)
            tx.put('job', self.job_id, self.owner, {'id': self.job_id, 'research_request_id': self.request_id,
                'status': 'succeeded', 'kind': 'analysis', 'created_at': STAMP})
            tx.put('request', self.request_id, self.owner, {'id': self.request_id, 'attribution': self.attribution, 'private_context': 'PRIVATE_CONTEXT'})
            tx.put('draft', self.draft_id, self.owner, {'id': self.draft_id, 'version': 1, 'private_context': 'PRIVATE_DRAFT'})
            tx.put('evidence', self.job_id, self.owner, {'private_context': 'FULL_PRIVATE_PACKAGE'})

    def principal(self, kind):
        identity = uid()
        with self.repo.transaction() as tx:
            tx.put('principal', identity, identity, {'retired': False, 'me': {
                'user_id': identity, 'principal_kind': kind, 'display_name': kind,
                'workspace_expires_at': None}})
        return identity

    def headers(self, owner, **claims):
        kind = 'anonymous' if owner == self.owner else 'registered'
        token = jwt.encode({'sub': owner, 'principal_kind': kind, 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
            'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid(), **claims}, 's' * 40, algorithm='HS256')
        return {'Authorization': 'Bearer ' + token, 'Idempotency-Key': uid()}

    def seed_document(self, tx, owner, document, account_id, paragraph_id=None):
        """Use the real provenance projector and registry, without paid acceptance."""
        checksum = digest(document)
        tx.put('scientific_document', digest([owner, checksum]), owner,
            {'sha256': checksum, 'document': document, 'citation_metadata': self.metadata, 'artifact_access': self.access})
        for group, rows in document.items():
            for node in rows:
                identity = node['id']; kind = 'account' if group == 'scientific_accounts' else 'paragraph' if group == 'paragraphs' else 'object'
                result = object_envelope(document, identity, self.metadata, self.access, max_depth=50, max_nodes=100)
                data = {'result': result}
                if kind == 'paragraph': data['account_id'] = account_id
                if kind == 'account':
                    result['research_statement'] = {'status': 'succeeded' if paragraph_id else 'not_requested', 'job_id': self.job_id if paragraph_id else None, 'paragraph_id': paragraph_id}
                    summary = {'account': node, 'knowledge_gap': document['knowledge_gaps'][0], 'claim_count': len(node['component_claims']),
                        'created_at': STAMP, 'job_id': self.job_id, 'research_statement': result['research_statement']}
                    data['summary'] = summary
                    tx.put('account_membership', digest([owner, identity]), owner, {'account_id': identity, 'summary': summary})
                tx.put(kind, digest([owner, identity]), owner, data)
                tx.put('object_document', digest([owner, identity]), owner, {'object_id': identity, 'sha256': checksum})

    def publish(self, identity=ACCOUNT, *, owner=None, visibility='public', version=0):
        owner = owner or self.owner
        response = self.client.post('/v1/accounts/' + identity + '/publication',
            json={'visibility': visibility, 'expected_version': version}, headers=self.headers(owner))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def get(self, path, *, owner=None, status=200):
        response = self.client.get(path, headers=self.headers(owner) if owner else {})
        self.assertEqual(response.status_code, status, response.text)
        return response

    def test_snapshot_excludes_unrelated_nodes_captures_and_execution_state(self):
        self.publish()
        with self.repo.read_transaction() as tx:
            row, snapshot = publication.find(tx, ACCOUNT, 'account')
            ids = {node['id'] for rows in snapshot['document'].values() for node in rows}
            self.assertEqual(ids, {ACCOUNT, CLAIM, FILE, GAP, PARAGRAPH})
            self.assertEqual(set(snapshot['artifact_records']), {self.source_sha})
            self.assertEqual(set(snapshot['artifacts']), {FILE})
            self.assertNotIn(PRIVATE, row['data']['object_ids'])
            for identity, kind in ((PRIVATE, 'object'), (PRIVATE, 'citation'), (self.private_sha, 'artifact')):
                with self.subTest(kind=kind), self.assertRaises(Problem): publication.find(tx, identity, kind)
            self.assertIsNone(snapshot['summary']['job_id'])
            self.assertIsNone(snapshot['summary']['research_statement']['job_id'])
        result = self.get('/v1/accounts/' + ACCOUNT).json()
        self.assertFalse(result['publication']['can_manage'])
        encoded = json.dumps(result)
        for private in (PRIVATE, PRIVATE_FILE, self.job_id, self.request_id, self.draft_id, str(self.root), 'PRIVATE_CONTEXT', 'FULL_PRIVATE_PACKAGE'):
            self.assertNotIn(private, encoded)
        self.assertEqual(self.get('/v1/artifacts/' + self.source_sha).content, self.source_bytes)
        for path in ('/v1/claims/' + PRIVATE, '/v1/artifacts/' + self.private_sha, '/v1/citations/' + PRIVATE): self.get(path, status=404)
        for path in ('/v1/jobs/' + self.job_id, '/v1/jobs/' + self.job_id + '/events',
                     '/v1/jobs/' + self.job_id + '/evidence-package', '/v1/drafts/' + self.draft_id,
                     '/v1/research-requests/' + self.request_id):
            self.get(path, status=401); self.get(path, owner=self.other, status=404)

    def test_public_access_never_downgrades_invalid_expired_or_retired_credentials(self):
        self.publish()
        paths = ['/v1/accounts/' + ACCOUNT, '/v1/accounts/' + ACCOUNT + '/publication', '/v1/claims/' + CLAIM,
            '/v1/paragraphs/' + PARAGRAPH, '/v1/citations/' + CLAIM, '/v1/artifacts/' + self.source_sha,
            '/v1/paragraphs/' + PARAGRAPH + '/export?format=markdown']
        invalid = [{'Authorization': 'Bearer invalid'}, {'Authorization': ''}, self.headers(self.other, exp=int(time.time()) - 1)]
        with self.repo.transaction() as tx:
            principal = tx.get('principal', self.other)['data']; principal['retired'] = True
            tx.put('principal', self.other, self.other, principal)
        invalid.append(self.headers(self.other))
        for path in paths:
            self.get(path)
            for headers in invalid:
                with self.subTest(path=path, credential=headers.get('Authorization', '')[:12]):
                    self.assertEqual(self.client.get(path, headers=headers).status_code, 401)

    def test_public_read_does_not_create_grants_or_allow_mutations(self):
        self.publish()
        with self.repo.read_transaction() as tx:
            before = {kind: tx.list(kind) for kind in ('grant', 'publication', 'account', 'paragraph', 'job', 'draft')}
        for owner in (None, self.other):
            self.get('/v1/accounts/' + ACCOUNT, owner=owner)
            self.get('/v1/citations/' + CLAIM, owner=owner)
            headers = self.headers(owner) if owner else {}
            requests = [('/v1/accounts/' + ACCOUNT + '/publication', {'visibility': 'private', 'expected_version': 1}),
                        ('/v1/jobs', {'kind': 'paragraph', 'account_id': ACCOUNT}),
                        ('/v1/jobs/' + self.job_id + '/cancel', None)]
            for path, body in requests:
                response = self.client.post(path, json=body, headers=headers)
                self.assertEqual(response.status_code, 404 if owner else 401, response.text)
            response = self.client.patch('/v1/drafts/' + self.draft_id, json={'expected_version': 1, 'composer': {
                'source_gap': None, 'eaggl_anchors': [], 'dismissed_source_ids': [], 'mechanism_subquery': '', 'model': 'cfde-inc-v2', 'selected_kgs': []}}, headers=headers)
            self.assertEqual(response.status_code, 404 if owner else 401, response.text)
        with self.repo.read_transaction() as tx:
            self.assertEqual(before, {kind: tx.list(kind) for kind in before})

    def test_exact_citation_revisions_and_exports_remain_frozen_until_republish(self):
        self.publish()
        original = self.get('/v1/citations/' + CLAIM + '?revision=1').json()
        exported = self.get('/v1/paragraphs/' + PARAGRAPH + '/export?format=markdown').json()
        with self.repo.transaction() as tx:
            revised = revise(tx, self.owner, CLAIM, 1, {'language': 'fr'})
        self.assertEqual(revised['metadata_revision'], 2)
        self.assertEqual(self.get('/v1/citations/' + CLAIM).json(), original)
        self.get('/v1/citations/' + CLAIM + '?revision=2', status=404)
        self.assertEqual(self.get('/v1/paragraphs/' + PARAGRAPH + '/export?format=markdown').json(), exported)
        self.assertEqual(exported['citation_targets'], [{'target_id': CLAIM, 'citation_metadata_revision': 1}])
        updated = deepcopy(self.document); updated['paragraphs'][0]['id'] = NEXT_PARAGRAPH
        updated['paragraphs'][0]['citations'][0]['citation_metadata_revision'] = 2
        with self.repo.transaction() as tx: self.seed_document(tx, self.owner, updated, ACCOUNT, NEXT_PARAGRAPH)
        self.assertTrue(self.get('/v1/accounts/' + ACCOUNT + '/publication', owner=self.owner).json()['has_unpublished_changes'])
        self.get('/v1/paragraphs/' + NEXT_PARAGRAPH, status=404)
        self.publish(version=1)
        self.assertEqual(self.get('/v1/citations/' + CLAIM + '?revision=2').json(), revised)
        self.get('/v1/paragraphs/' + PARAGRAPH, status=404)
        self.get('/v1/paragraphs/' + NEXT_PARAGRAPH)

    def test_revoking_one_publication_preserves_only_independently_shared_closure(self):
        self.publish()
        second = deepcopy(self.document); second['scientific_accounts'][0]['id'] = SECOND; second.pop('paragraphs')
        with self.repo.transaction() as tx: self.seed_document(tx, self.owner, second, SECOND)
        self.publish(SECOND)
        cursor = self.get('/v1/accounts/' + ACCOUNT + '?max_nodes=2').json()['coverage']['next_cursor']
        self.assertTrue(cursor)
        self.publish(visibility='private', version=1)
        self.get('/v1/accounts/' + ACCOUNT, status=404)
        self.get('/v1/accounts/' + ACCOUNT + '?cursor=' + cursor, status=404)
        self.get('/v1/paragraphs/' + PARAGRAPH, status=404)
        self.get('/v1/accounts/' + SECOND); self.get('/v1/claims/' + CLAIM); self.get('/v1/artifacts/' + self.source_sha)
        self.publish(SECOND, visibility='private', version=1)
        for path in ('/v1/claims/' + CLAIM, '/v1/citations/' + CLAIM, '/v1/artifacts/' + self.source_sha): self.get(path, status=404)
        self.get('/v1/accounts/' + ACCOUNT, owner=self.owner)

    def test_transfer_rekeys_publication_management_and_preserves_original_attribution(self):
        self.publish()
        before = self.get('/v1/accounts/' + ACCOUNT).json()
        with self.repo.transaction() as tx:
            tx.transfer(self.owner, self.other)
            self.assertIsNone(tx.get('publication', digest([self.owner, ACCOUNT])))
            row, snapshot = publication.find(tx, ACCOUNT, 'account')
            self.assertEqual(row['owner'], self.other)
            self.assertEqual(snapshot['summary']['attribution'], self.attribution)
        after = self.get('/v1/accounts/' + ACCOUNT).json()
        self.assertEqual(before['document'], after['document'])
        self.assertFalse(self.get('/v1/accounts/' + ACCOUNT + '/publication', owner=self.owner).json()['can_manage'])
        self.assertTrue(self.get('/v1/accounts/' + ACCOUNT + '/publication', owner=self.other).json()['can_manage'])
        denied = self.client.post('/v1/accounts/' + ACCOUNT + '/publication',
            json={'visibility': 'private', 'expected_version': 1}, headers=self.headers(self.owner))
        self.assertEqual(denied.status_code, 404, denied.text)
        self.publish(owner=self.other, visibility='private', version=1)
        self.get('/v1/accounts/' + ACCOUNT, status=404)
        self.get('/v1/accounts/' + ACCOUNT, owner=self.other)

    def test_partial_account_without_full_document_fails_closed_without_snapshot(self):
        with self.repo.transaction() as tx:
            key = digest([self.owner, ACCOUNT]); tx.remove('object_document', key)
            account = tx.get('account', key)['data']; account['result']['coverage']['complete'] = False
            tx.put('account', key, self.owner, account)
        response = self.client.post('/v1/accounts/' + ACCOUNT + '/publication',
            json={'visibility': 'public', 'expected_version': 0}, headers=self.headers(self.owner))
        self.assertEqual(response.status_code, 409, response.text)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('publication'), []); self.assertEqual(tx.list('publication_snapshot'), [])

    def test_missing_exact_citation_revision_rolls_back_publication_atomically(self):
        with self.repo.transaction() as tx: tx.remove('citation', CLAIM + ':1')
        response = self.client.post('/v1/accounts/' + ACCOUNT + '/publication',
            json={'visibility': 'public', 'expected_version': 0}, headers=self.headers(self.owner))
        self.assertEqual(response.status_code, 404, response.text)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('publication'), []); self.assertEqual(tx.list('publication_snapshot'), [])

    def test_snapshot_reader_never_delegates_private_queries_or_writes(self):
        self.publish()
        with self.repo.read_transaction() as tx: _, snapshot = publication.find(tx, ACCOUNT, 'account')
        reader = publication.SnapshotReader(snapshot)
        for kind in ('object', 'account', 'grant', 'artifact', 'job', 'draft', 'request'):
            self.assertIsNone(reader.get(kind, ACCOUNT)); self.assertEqual(reader.list(kind), [])
            with self.assertRaises(RuntimeError): reader.put(kind, ACCOUNT, self.owner, {})
        self.assertIsNone(reader.get('citation', PRIVATE + ':1'))
        self.assertIsNone(reader.get('citation', CLAIM + ':2'))
        reader.put('citation_rendering', 'deterministic-cache-only', self.owner, {})


if __name__ == '__main__':
    unittest.main()
