"""Reference guards over the served reference release, and reads of stored archive stamps.

The rule: an anchor is current iff its factor id is served by this environment's current factor
table (the catalog's factors; reveal_ref_factors for retry-review). Unserved anchors are rejected
(409 REFERENCE_GENERATION_SUPERSEDED), unserved factor reads are 410 with their frozen snapshot,
and there is no reload gate, no generation pointer and no stamping at creation. Archive stamps
already stored on records (by the retired cutover) keep rendering, filtering and publishing as before.
"""
import asyncio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import jwt

from reveal_backend import analysis_outcomes, app as api, jobs, publication, worker
from reveal_backend import reference_generation as reference
from reveal_backend.auth import Problem
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json
from reveal_backend.repository import Repository, digest, now, uid
from reveal_backend.runtime_config import ROOT


def example(name, response=False):
    value = json.loads((ROOT / 'api/examples' / name).read_text())
    return next(iter(value['responses']['200']['examples'].values())) if response else value['request']['body']


GAP = example('getKnowledgeGap.request.json', True)
GAP_ID = GAP['object']['id']
COMPOSER = example('createDraft.question_and_anchor.json')['composer']
SUGGEST = example('suggestMechanisms.dismech_context.json')
ACCOUNT = example('getAccount.request.json', True)

R1, R2 = 'a' * 64, 'b' * 64
LEGACY_GENERATION = reference.legacy_generation_id('c' * 64)
LEGACY_ID = 'factor:portal:T2D:cfde-inc-v2:Factor1'
KPN_ID = reference.public_id('KPN.TRAIT:0000398', 'Factor1')
OTHER_ID = reference.public_id('KPN.TRAIT:0000398', 'Factor2')
LEGACY_BINDING = {'eaggl_factor_id': 'T2D::Factor1', 'eaggl_import_id': 'f' * 64, 'embedding_run_id': 'e' * 64,
                  'mapping_run_id': 'c' * 64, 'gene_set_import_id': 'd' * 64, 'cfde_node_id': LEGACY_ID}


def binding(source_id, release):
    key = reference.parse_public_id(source_id)['factor_key']
    return {'eaggl_factor_id': 'T2D::' + key.split('::')[1], 'factor_key': key, 'kpn_trait_id': 'KPN.TRAIT:0000398',
            'eaggl_import_id': None, 'embedding_run_id': release, 'mapping_run_id': release, 'gene_set_import_id': release,
            'reference_generation_id': release, 'model': reference.KPN_MODEL, 'cfde_node_id': source_id}


def factor(source_id, letter):
    return {'source': 'eaggl', 'source_id': source_id, 'source_revision': letter * 64, 'object_class': 'Mechanism',
            'object': {'id': 'dapper:Mechanism.' + letter * 32, 'name': 'T2D mechanism Factor1', 'description': source_id},
            'cfde_anchor': {'node_id': source_id, 'node_type': 'factor', 'label': 'Label ' + letter, 'subtitle': 'T2D (Factor1)'}}


def source_ref(record):
    return {'source': 'eaggl', 'source_id': record['source_id'], 'source_revision': record['source_revision'],
            'dapper_id': record['object']['id']}


def selection(record):
    return {'reference': source_ref(record), 'origin': 'manual', 'suggestion_id': None}


class Release:
    """Catalog double: the factors of the served reference release (the catalog attributes the app reads)."""
    model = reference.KPN_MODEL
    LETTERS = {KPN_ID: '2', OTHER_ID: '3'}

    def __init__(self):
        self.gaps = {GAP_ID: GAP}; self.mechanisms = {}; self.dismech_import = 'dismech-import'
        self.archived, self.snapshots = {}, {}
        self.serve(R1, KPN_ID, OTHER_ID)

    def serve(self, release, *identities):
        """Publish a release: the catalog's run fields name it and it serves exactly `identities`."""
        self.release_id = self.reference_generation_id = self.embedding_run = self.mapping_run = release
        self.factors = {identity: factor(identity, self.LETTERS[identity]) for identity in identities}
        self.bindings = {identity: binding(identity, release) for identity in identities}

    def load(self): pass

    def gap(self, identity):
        if identity not in self.gaps: raise Problem(404, 'GAP_NOT_FOUND', 'This source gap is unavailable.')
        return self.gaps[identity]

    def selected(self, chosen): return self.gap(chosen['id'])

    def validate_composer(self, composer, submit=False):
        for chosen in composer['eaggl_anchors']:
            ref = chosen['reference']; record = self.factors.get(ref['source_id'])
            if not record or record['source_revision'] != ref['source_revision'] or record['object']['id'] != ref['dapper_id']:
                raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The selected mechanism binding is unavailable; select it again.')
        return self.selected(composer['source_gap']) if composer['source_gap'] else None

    def archived_for_source(self, source_id): return self.archived.get(source_id)
    def archived_reference_factor(self, archive_id): return self.snapshots.get(archive_id)
    def dismech_catalog(self): return {}
    def suggest_factors(self, *args, **kwargs): return []
    def context_embedding_provenance(self, *args): return {'dismech_embedding_run_id': self.release_id}
    def provenance(self, *args): return {}


def refusing_module(name, *functions):
    """A stand-in module whose functions fail the test if the app ever calls them."""
    module = types.ModuleType(name)
    def refuse(function):
        def call(*args, **kwargs): raise AssertionError(function + ' must not be called')
        return call
    for function in functions: setattr(module, function, refuse(function))
    return module


class ReferenceGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = Repository(str(self.root / 'app.sqlite')); self.repo.migrate()
        self.catalog = Release()
        # No stamping and no generation pointers: the archive pass is never imported, and the
        # pointer/gate/generation helpers are never called.
        name = 'reveal_backend.reference_archive'; previous = sys.modules.get(name)
        sys.modules[name] = refusing_module(name, 'anchors_for_stamp', 'stamp_payload', 'capture_factors')
        self.addCleanup(lambda: sys.modules.__setitem__(name, previous) if previous is not None else sys.modules.pop(name, None))
        for function in ('read_active', 'read_gate', 'generation_of_binding', 'generation_of_anchors'):
            context = patch.object(reference, function, side_effect=AssertionError(function + ' must not be called'))
            context.start(); self.addCleanup(context.stop)
        for context in (patch.object(api, 'repo', self.repo), patch.object(api, 'catalog', self.catalog),
                        patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 40, 'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs',
                                                'REVEAL_GATEWAY_AUDIENCE': 'reveal-api', 'REVEAL_ARTIFACTS_DIR': str(self.root),
                                                # Retired pins: never read.
                                                'REVEAL_REFERENCE_GENERATION_ID': 'f' * 64})):
            context.start(); self.addCleanup(context.stop)
        self.client = TestClient(api.app); self.owner = self.principal()
        with self.repo.transaction() as tx:
            # Retired records of the old cutover: a closed reload gate and a pointer to another generation.
            tx.put(reference.CONTROL_KIND, reference.CONTROL_ID, reference.CATALOG_OWNER, {'closed': True, 'reason': 'retired gate'})
            tx.put(reference.ACTIVE_KIND, reference.ACTIVE_ID, reference.CATALOG_OWNER, {'generation_id': 'e' * 64, 'model': reference.KPN_MODEL})

    # ------------------------------------------------------------------ helpers
    def principal(self):
        identity = uid()
        with self.repo.transaction() as tx:
            tx.put('principal', identity, identity, {'retired': False, 'me': {
                'user_id': identity, 'principal_kind': 'registered', 'display_name': 'Researcher', 'orcid': None,
                'orcid_authenticated': False, 'workspace_expires_at': None}})
        return identity

    def headers(self, owner=None):
        current = int(time.time())
        token = jwt.encode({'sub': owner or self.owner, 'principal_kind': 'registered', 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
                            'iat': current, 'exp': current + 120, 'jti': uid()}, 's' * 40, algorithm='HS256')
        return {'Authorization': 'Bearer ' + token, 'Idempotency-Key': uid()}

    def composer(self, *records, **changes):
        return dict(deepcopy(COMPOSER), eaggl_anchors=[selection(record) for record in records], **changes)

    def create_draft(self, *records, status=201):
        response = self.client.post('/v1/drafts', json={'composer': self.composer(*records)}, headers=self.headers())
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def patch_draft(self, draft, body):
        return self.client.patch('/v1/drafts/' + draft['id'], json=body, headers=self.headers())

    def submit(self, draft):
        return self.client.post('/v1/jobs', json={'kind': 'analysis', 'draft_id': draft['id'], 'draft_version': draft['version']},
                                headers=self.headers())

    def code(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text); self.assertEqual(response.json()['code'], code)
        return response.json()

    def job_with_binding(self, anchors, kind='analysis', document=None):
        request_id = uid(); composer = {'selected_kgs': [], 'eaggl_anchors': [
            {'reference': {'source_id': anchor['cfde_node_id']}, 'origin': 'automatic'} for anchor in anchors]}
        with self.repo.transaction() as tx:
            tx.put('request', request_id, self.owner, {'composer': composer, 'question_id': GAP_ID, **({'document': document} if document else {})})
            tx.put('request_binding', request_id, self.owner, {'anchors': deepcopy(anchors),
                   'anchor_display': {anchor['cfde_node_id']: {'label': 'Frozen label'} for anchor in anchors}})
            return jobs.enqueue(tx, self.owner, kind, request_id=request_id)

    def served_table(self, *identities):
        """This environment's reveal_ref_factors table (in the application database, as in Aurora)."""
        with self.repo.transaction() as tx:
            tx.execute('CREATE TABLE IF NOT EXISTS reveal_ref_factors(factor_key TEXT PRIMARY KEY, public_id TEXT, eaggl_factor_id TEXT, '
                       'kpn_trait_id TEXT, factor_number INTEGER, label TEXT, input_sha256 TEXT, source_revision TEXT, metadata TEXT)')
            tx.execute('DELETE FROM reveal_ref_factors')
            for identity in identities:
                parsed = reference.parse_public_id(identity)
                tx.execute('INSERT INTO reveal_ref_factors VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)', (parsed['factor_key'], identity, 'T2D::' + parsed['factor'],
                           parsed['kpn_trait_id'], int(parsed['factor'][6:]), 'label', '0' * 64, '1' * 64, '{}'))

    # ------------------------------------------------------------------ guards
    def test_served_anchors_freeze_and_submit_with_their_first_saved_runs(self):
        kpn = self.catalog.factors[KPN_ID]
        draft = self.create_draft(kpn)
        # A later release still serves the factor: the unchanged anchor keeps its first-saved binding.
        self.catalog.serve(R2, KPN_ID, OTHER_ID)
        patched = self.patch_draft(draft, {'expected_version': 1, 'composer': self.composer(kpn, mechanism_subquery='edited')})
        self.assertEqual(patched.status_code, 200, patched.text)
        response = self.submit(patched.json()); self.assertEqual(response.status_code, 202, response.text)
        with self.repo.read_transaction() as tx:
            frozen = tx.get('request_binding', response.json()['research_request_id'])['data']
            self.assertEqual(frozen['anchors'], [binding(KPN_ID, R1)])
            self.assertEqual(tx.get(reference.ACTIVE_KIND, reference.ACTIVE_ID)['data']['generation_id'], 'e' * 64)  # Never read or moved.
        # A newly added anchor binds the current release.
        other = self.catalog.factors[OTHER_ID]
        patched = self.patch_draft(patched.json(), {'expected_version': 2, 'composer': self.composer(kpn, other)})
        self.assertEqual(patched.status_code, 200, patched.text)
        with self.repo.read_transaction() as tx:
            selections = tx.get('draft_binding', draft['id'])['data']['selections']
        self.assertEqual((selections[KPN_ID]['binding'], selections[OTHER_ID]['binding']), (binding(KPN_ID, R1), binding(OTHER_ID, R2)))

    def test_catalog_loads_before_write_transactions_open(self):
        # A cold catalog load does network I/O: it must never run inside a pooled write
        # transaction (draft saves, submits and vote writes hold the global write lock).
        depth, loads = [], []
        transaction, read_transaction = self.repo.transaction, self.repo.read_transaction
        def tracked(opener):
            @contextmanager
            def opened(*args, **kwargs):
                depth.append(1)
                try:
                    with opener(*args, **kwargs) as tx: yield tx
                finally: depth.pop()
            return opened
        def tracking():
            stack = ExitStack()
            stack.enter_context(patch.object(self.repo, 'transaction', tracked(transaction)))
            stack.enter_context(patch.object(self.repo, 'read_transaction', tracked(read_transaction)))
            return stack
        self.catalog.load = lambda: loads.append(bool(depth))
        kpn = self.catalog.factors[KPN_ID]
        with tracking():
            draft = self.create_draft(kpn); first = len(loads)
            patched = self.patch_draft(draft, {'expected_version': 1, 'composer': self.composer(kpn, mechanism_subquery='x')})
            self.assertEqual(patched.status_code, 200, patched.text); second = len(loads)
            self.assertEqual(self.submit(patched.json()).status_code, 202)
        self.assertEqual([loads[0], loads[first], loads[second]], [False, False, False])
        loads.clear()
        with tracking():  # A rename needs no catalog.
            self.assertEqual(self.patch_draft(patched.json(), {'expected_version': 2, 'name': 'Renamed'}).status_code, 200)
        self.assertEqual(loads, [])
        # Votes resolve their gap through the catalog inside the transaction, so they load it first too.
        account_id = 'dapper:ScientificAccount.' + 'b' * 32
        with self.repo.transaction() as tx:
            tx.put('publication_snapshot', 'snapshot', self.owner, {'account_id': account_id, 'document': {
                'scientific_accounts': [{'id': account_id, 'question': GAP_ID}]}})
            tx.put('publication', 'publication', self.owner, {'visibility': 'public', 'snapshot_id': 'snapshot',
                   'published_at': now(), 'account_id': account_id})
        for path in ('/v1/knowledge-gaps/' + GAP_ID + '/vote', '/v1/accounts/' + account_id + '/vote'):
            for method in ('get', 'post'):
                loads.clear()
                with tracking():
                    response = self.client.request(method, path, headers=self.headers(),
                                                   **({'json': {'vote': 1}} if method == 'post' else {}))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(loads, [False], (method, path))

    def test_unserved_draft_submission_is_409_and_there_is_no_reload_gate(self):
        draft = self.create_draft(self.catalog.factors[KPN_ID])
        self.catalog.serve(R2, OTHER_ID)  # The new release no longer serves the draft's factor.
        body = self.code(self.submit(draft), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        self.assertFalse(body['retryable'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('job', self.owner), []); self.assertEqual(tx.list('request', self.owner), [])
        # The retired gate record is closed, yet nothing waits for it.
        current = self.create_draft(self.catalog.factors[OTHER_ID])
        response = self.submit(current); self.assertEqual(response.status_code, 202, response.text)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('request_binding', response.json()['research_request_id'])['data']['anchors'], [binding(OTHER_ID, R2)])
        account_id = 'dapper:ScientificAccount.' + 'a' * 32
        with self.repo.transaction() as tx:
            tx.put('account', digest([self.owner, account_id]), self.owner, {'result': {'document': {}}, 'summary': {}})
        paragraph = self.client.post('/v1/jobs', json={'kind': 'paragraph', 'account_id': account_id}, headers=self.headers())
        self.assertEqual(paragraph.status_code, 202, paragraph.text)

    def test_legacy_anchor_drafts_are_superseded(self):
        legacy = factor(LEGACY_ID, '1')
        with self.repo.transaction() as tx:
            draft = {'id': uid(), 'owner_user_id': self.owner, 'version': 1, 'composer': self.composer(legacy), 'created_at': now(),
                     'updated_at': now(), 'lifecycle': 'saved', 'expires_at': None, 'name': 'Legacy draft'}
            tx.put('draft', draft['id'], self.owner, draft)
            tx.put('draft_binding', draft['id'], self.owner, {'dismech_import_id': 'dismech-import', 'source_gap': GAP,
                   'selections': {LEGACY_ID: {'reference': source_ref(legacy), 'record': legacy, 'binding': LEGACY_BINDING}}})
        self.code(self.submit(draft), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        self.code(self.patch_draft(draft, {'expected_version': 1, 'composer': self.composer(legacy, mechanism_subquery='edited')}),
                  409, 'REFERENCE_GENERATION_SUPERSEDED')

    def test_draft_writes_revalidate_unserved_anchors(self):
        kpn, draft = self.catalog.factors[KPN_ID], self.create_draft(self.catalog.factors[KPN_ID])
        self.catalog.serve(R2, OTHER_ID)
        other = self.catalog.factors[OTHER_ID]
        renamed = self.patch_draft(draft, {'expected_version': 1, 'name': 'Renamed'})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        stale = self.patch_draft(draft, {'expected_version': 2, 'composer': self.composer(kpn, mechanism_subquery='edited')})
        self.code(stale, 409, 'REFERENCE_GENERATION_SUPERSEDED')
        gap_only = self.patch_draft(draft, {'expected_version': 2, 'composer': self.composer()})
        self.assertEqual(gap_only.status_code, 200, gap_only.text)
        current = self.patch_draft(draft, {'expected_version': 3, 'composer': self.composer(other)})
        self.assertEqual(current.status_code, 200, current.text)
        edited = self.patch_draft(draft, {'expected_version': 4, 'composer': self.composer(other, mechanism_subquery='still editable')})
        self.assertEqual(edited.status_code, 200, edited.text)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('draft_binding', draft['id'])['data']['selections'][OTHER_ID]['binding'], binding(OTHER_ID, R2))
        # A served factor whose content changed is reselected, not superseded.
        self.catalog.factors[OTHER_ID] = factor(OTHER_ID, '9')
        changed = self.patch_draft(draft, {'expected_version': 5, 'composer': self.composer(other, mechanism_subquery='again')})
        self.assertEqual(changed.status_code, 200, changed.text)  # Unchanged selections keep their frozen record.
        reselected = self.patch_draft(draft, {'expected_version': 6, 'composer': self.composer(factor(OTHER_ID, '3') | {'source_revision': '4' * 64})})
        self.code(reselected, 409, 'SOURCE_REVISION_CHANGED')

    def test_suggest_rejects_unserved_manual_anchors(self):
        body = dict(deepcopy(SUGGEST), manual_eaggl_anchors=[source_ref(factor(LEGACY_ID, '1'))])
        self.code(self.client.post('/v1/mechanisms/suggest', json=body), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        self.catalog.serve(R2, OTHER_ID)
        body['manual_eaggl_anchors'] = [source_ref(factor(KPN_ID, '2'))]
        self.code(self.client.post('/v1/mechanisms/suggest', json=body), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        body['manual_eaggl_anchors'] = [source_ref(self.catalog.factors[OTHER_ID])]
        response = self.client.post('/v1/mechanisms/suggest', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        with self.repo.read_transaction() as tx:
            suggestion = tx.get('suggestion', response.json()['suggestion_id'])['data']
        # Suggestion records carry the release in their run fields.
        self.assertEqual((suggestion['embedding_run_id'], suggestion['mapping_run_id']), (R2, R2))

    def test_retry_review_blocks_only_anchors_the_reference_factor_table_does_not_serve(self):
        def failed(anchors, kind='analysis', account_id=None):
            if kind == 'analysis': job = self.job_with_binding(anchors)
            else:
                with self.repo.transaction() as tx:
                    tx.put('account', digest([self.owner, account_id]), self.owner, {'result': {'document': {}}, 'summary': {}})
                    job = jobs.enqueue(tx, self.owner, 'paragraph', account_id=account_id)
            _, queue = jobs.claim(self.repo, 'guard-test', job_id=job['id'])
            jobs.finish(self.repo, job['id'], queue['token'], 'failed',
                        failure={'code': 'REVIEW_UNAVAILABLE', 'message': 'No verdict', 'retryable': True})
            with self.repo.read_transaction() as tx: return tx.get('job', job['id'])['data']
        retry = lambda job: self.client.post('/v1/jobs/' + job['id'] + '/retry-review',
                                             json={'expected_last_event_id': job['last_event_id']}, headers=self.headers())
        legacy, current, older = failed([LEGACY_BINDING]), failed([binding(KPN_ID, R1)]), failed([binding(KPN_ID, 'e' * 64), binding(OTHER_ID, R1)])
        self.code(retry(current), 503, 'SOURCE_NOT_READY')  # No reference factor table yet.
        self.served_table(KPN_ID, OTHER_ID)
        self.code(retry(legacy), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        # The guard passes for served factors, whichever release froze them; the (absent) saved capture is what refuses.
        self.code(retry(current), 409, 'REVIEW_CAPTURE_UNAVAILABLE')
        self.code(retry(older), 409, 'REVIEW_CAPTURE_UNAVAILABLE')
        paragraph = failed([], 'paragraph', 'dapper:ScientificAccount.' + 'b' * 32)
        self.code(retry(paragraph), 409, 'REVIEW_CAPTURE_UNAVAILABLE')
        self.served_table(KPN_ID)
        self.code(retry(older), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        self.code(retry(current), 409, 'REVIEW_CAPTURE_UNAVAILABLE')
        self.code(retry(failed([{**binding(KPN_ID, R1), 'cfde_node_id': None}])), 409, 'REFERENCE_GENERATION_SUPERSEDED')

    # ------------------------------------------------------------------ reads
    def test_unserved_factor_is_gone_with_its_frozen_snapshot(self):
        legacy = {'format': 'reveal.archived-reference-factor/1', 'archive_id': reference.archive_id(LEGACY_GENERATION, LEGACY_ID),
                  'generation_id': LEGACY_GENERATION, 'source_id': LEGACY_ID, 'top_genes': [{'symbol': 'TCF7L2', 'loading': 0.5}]}
        dropped = {'format': 'reveal.archived-reference-factor/1', 'archive_id': reference.archive_id(R1, OTHER_ID),
                   'generation_id': R1, 'source_id': OTHER_ID, 'top_genes': [{'symbol': 'INS', 'loading': 0.7}]}
        for snapshot in (legacy, dropped):
            self.catalog.archived[snapshot['source_id']] = snapshot; self.catalog.snapshots[snapshot['archive_id']] = snapshot
        self.catalog.serve(R2, KPN_ID)
        for identity, snapshot in ((LEGACY_ID, legacy), (OTHER_ID, dropped)):
            body = self.code(self.client.get('/v1/mechanisms/' + identity), 410, 'REFERENCE_GENERATION_SUPERSEDED')
            self.assertEqual(body['archived_reference_factor'], snapshot); self.assertFalse(body['retryable'])
        uncaptured = self.code(self.client.get('/v1/mechanisms/factor:portal:T2D:cfde-inc-v2:Factor7'), 410, 'REFERENCE_GENERATION_SUPERSEDED')
        self.assertIsNone(uncaptured['archived_reference_factor'])
        self.code(self.client.get('/v1/mechanisms/' + reference.public_id('KPN.TRAIT:0000001', 'Factor3')), 404, 'NOT_FOUND')
        self.assertEqual(self.client.get('/v1/mechanisms/' + KPN_ID).json(), self.catalog.factors[KPN_ID])
        self.code(self.client.get('/v1/mechanisms/' + KPN_ID, params={'source_revision': '0' * 64}), 409, 'SOURCE_REVISION_CHANGED')
        # Public reference data: no session needed; unknown and malformed ids are 404.
        response = self.client.get('/v1/reference-factors/' + legacy['archive_id'])
        self.assertEqual(response.status_code, 200, response.text); self.assertEqual(response.json(), legacy)
        self.code(self.client.get('/v1/reference-factors/' + '0' * 64), 404, 'NOT_FOUND')
        self.code(self.client.get('/v1/reference-factors/not-an-archive-id'), 404, 'NOT_FOUND')

    def test_account_detail_returns_stored_archive_for_owner_and_public_snapshot(self):
        identity = ACCOUNT['root_id']; checksum = digest(ACCOUNT['document'])
        stamp = reference.build_stamp(LEGACY_GENERATION, R1, reference={'model': reference.LEGACY_MODEL, 'anchors': []}, gap=None,
                                      analysis={'job_id': 'job', 'request_id': 'request', 'account_id': identity})
        publisher = self.principal()
        with self.repo.transaction() as tx:
            for owner, summary in ((self.owner, {'archive': stamp}), (publisher, {})):
                tx.put('account', digest([owner, identity]), owner, {'result': ACCOUNT, 'summary': summary})
                tx.put('object_document', digest([owner, identity]), owner, {'object_id': identity, 'sha256': checksum})
                tx.put('scientific_document', digest([owner, checksum]), owner, {'document': ACCOUNT['document']})
        detail = self.client.get('/v1/accounts/' + identity, headers=self.headers())
        self.assertEqual(detail.status_code, 200, detail.text); self.assertEqual(detail.json()['archive'], stamp)
        self.assertNotIn('archive', self.client.get('/v1/accounts/' + identity, headers=self.headers(publisher)).json())
        with self.repo.transaction() as tx:
            snapshot_id = uid()
            summary = {'research_statement': {'status': 'not_requested', 'job_id': None, 'paragraph_id': None},
                       'archive': reference.public_stamp(stamp)}
            tx.put('publication_snapshot', snapshot_id, publisher, {'account_id': identity, 'document': ACCOUNT['document'],
                   'citation_metadata': ACCOUNT['citation_metadata'], 'summary': summary,
                   'artifacts': {item['file']['id']: item for item in ACCOUNT['artifacts']}, 'artifact_records': {}})
            tx.put('publication', digest([publisher, identity]), publisher, {'account_id': identity, 'visibility': 'public', 'version': 1,
                   'published_at': now(), 'updated_at': now(), 'snapshot_id': snapshot_id, 'object_ids': [identity],
                   'citation_keys': [], 'artifact_sha256': [], 'summary': summary})
        public = self.client.get('/v1/accounts/' + identity)
        self.assertEqual(public.status_code, 200, public.text)
        self.assertEqual(public.json()['archive'], reference.public_stamp(stamp))
        self.assertIsNone(public.json()['archive']['analysis']['job_id'])

    # ------------------------------------------------------------------ no stamping at creation; stored stamps keep rendering
    def outcome(self, job, *, created_at=None):
        record = {'outcome': 'insufficient_evidence', 'summary': 'Scoped', 'reason': 'Missing link', 'knowledge_gap': GAP['object'],
                  'source_gap': deepcopy(COMPOSER['source_gap']), 'anchors': [{'source_id': LEGACY_ID, 'name': 'Frozen'}],
                  'created_at': created_at or now(), 'attribution': None, 'job_id': job['id'],
                  'provenance': {'evidence_package_sha256': '9' * 64, 'source_bindings': []}}
        prepared = {'record': record, 'artifacts': {}}
        with self.repo.transaction() as tx: return analysis_outcomes.save(tx, job, prepared), prepared

    def test_outcomes_are_never_stamped_at_creation_and_stored_stamps_keep_rendering(self):
        current, _ = self.outcome(self.job_with_binding([binding(KPN_ID, R1)]), created_at='2026-09-01T00:00:00Z')
        late_job = self.job_with_binding([LEGACY_BINDING])  # Collected on reference data the release no longer serves.
        late, prepared = self.outcome(late_job, created_at='2026-09-03T00:00:00Z')
        with self.repo.read_transaction() as tx:
            for identity in (current, late):
                self.assertNotIn('archive', tx.get('analysis_outcome', identity)['data']['record'])
                self.assertNotIn('archive', tx.get('outcome_summary', identity)['data'])
        with self.repo.transaction() as tx: self.assertEqual(analysis_outcomes.save(tx, late_job, prepared), late)  # replay
        # An outcome the retired cutover stamped: the stamp sits in record.archive and the summary.
        stored, _ = self.outcome(self.job_with_binding([LEGACY_BINDING]), created_at='2026-09-02T00:00:00Z')
        stamp = reference.build_stamp(LEGACY_GENERATION, R1, reference={'model': reference.LEGACY_MODEL, 'anchors': []},
            gap={'id': GAP_ID, 'source_id': COMPOSER['source_gap']['source_id'], 'source_revision': COMPOSER['source_gap']['source_revision']},
            analysis={'job_id': uid(), 'request_id': uid(), 'evidence_package_sha256': '9' * 64, 'outcome_id': stored})
        with self.repo.transaction() as tx:
            row = tx.get('analysis_outcome', stored)['data']; row['record']['archive'] = stamp
            tx.put('analysis_outcome', stored, self.owner, row)
            summary = tx.get('outcome_summary', stored)['data']; summary['archive'] = stamp
            tx.put('outcome_summary', stored, self.owner, summary)
        with self.repo.transaction() as tx: analysis_outcomes.change(tx, stored, self.owner, 'public', 0)
        with self.repo.read_transaction() as tx:
            published = tx.get('outcome_publication', stored)['data']
            shared = tx.get('outcome_snapshot', published['snapshot_id'])['data']['record']['archive']
        self.assertEqual(shared, reference.public_stamp(stamp)); self.assertEqual(published['summary']['archive'], shared)
        self.assertIsNone(shared['analysis']['request_id']); self.assertEqual(shared['analysis']['outcome_id'], stored)
        self.assertEqual(self.client.get('/v1/analysis-outcomes/' + stored).json()['archive'], shared)
        self.assertEqual(self.client.get('/v1/analysis-outcomes/' + stored, headers=self.headers()).json()['archive'], stamp)
        # Listings: all (current first, newest first within each group), current, archived.
        listed = lambda state=None: [item['id'] for item in self.client.get('/v1/analysis-outcomes', headers=self.headers(),
            params={'reference_state': state} if state else {}).json()['items']]
        self.assertEqual(listed(), [late, current, stored])
        self.assertEqual(listed('current'), [late, current]); self.assertEqual(listed('archived'), [stored])
        self.code(self.client.get('/v1/analysis-outcomes', params={'reference_state': 'outdated'}, headers=self.headers()), 422, 'INVALID_QUERY')
        path = '/v1/knowledge-gaps/' + GAP_ID + '/outcomes'
        self.assertEqual([item['archive'] for item in self.client.get(path).json()['items']], [shared])
        self.assertEqual(self.client.get(path, params={'reference_state': 'current'}).json()['items'], [])

    def accept_account(self, anchors, letter):
        """Worker.accept_accounts for one account, with citation registration and envelopes stubbed."""
        job = self.job_with_binding(anchors)
        job, queue = jobs.claim(self.repo, 'accept-test', job_id=job['id'])
        directory = self.root / job['id'] / 'attempt-1'; directory.mkdir(parents=True)
        package = directory / 'evidence-package.json'
        package.write_bytes(canonical_json({'dapper_context': {'files': []}, 'source_artifacts': {}}))
        identity = 'dapper:ScientificAccount.' + letter * 32
        doc = {'scientific_accounts': [{'id': identity, 'question': GAP_ID, 'component_claims': []}], 'knowledge_gaps': [GAP['object']]}
        path = directory / 'accepted-1.json'; path.write_bytes(canonical_json(doc))
        frozen = {'question_id': GAP_ID, 'composer': {'selected_kgs': [], 'source_gap': deepcopy(COMPOSER['source_gap'])},
                  'attribution': {'user_id': self.owner}}
        projection = lambda document, root, *args, **kwargs: ({'root_id': root, 'document': {}}, {root})
        with patch('reveal_backend.citations.register', return_value=[]), patch.object(worker, 'object_projection', side_effect=projection):
            asyncio.run(worker.Worker(self.repo).accept_accounts(job, queue['token'], [(doc, {'valid': True}, path)], frozen, package,
                        SimpleNamespace(runtime_manifest_path=None, ledger_manifest_path=None), directory, 'deterministic'))
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'succeeded')
            return job, identity, tx.get('account', digest([self.owner, identity]))['data'], \
                tx.get('account_membership', digest([self.owner, identity]))['data']

    def test_accounts_are_never_stamped_at_creation_and_stored_stamps_keep_counting_apart(self):
        _, current, account, membership = self.accept_account([binding(KPN_ID, R1)], 'c')
        self.assertNotIn('archive', account['summary']); self.assertNotIn('archive', membership['summary'])
        job, late, account, membership = self.accept_account([LEGACY_BINDING], 'd')
        self.assertNotIn('archive', account['summary']); self.assertNotIn('archive', membership['summary'])
        # The account the retired cutover stamped.
        stamp = reference.build_stamp(LEGACY_GENERATION, R1, reference={'model': reference.LEGACY_MODEL, 'anchors': []}, gap=None,
                                      analysis={'job_id': job['id'], 'request_id': job['research_request_id'], 'account_id': late})
        with self.repo.transaction() as tx:
            for kind in ('account', 'account_membership'):
                row = tx.get(kind, digest([self.owner, late]))['data']; row['summary']['archive'] = stamp
                tx.put(kind, digest([self.owner, late]), self.owner, row)
        listed = lambda state=None: [item['account']['id'] for item in self.client.get('/v1/accounts', headers=self.headers(),
            params={'reference_state': state} if state else {}).json()['items']]
        self.assertEqual(listed(), [current, late])  # newest first would put the archived account first
        self.assertEqual(listed('current'), [current]); self.assertEqual(listed('archived'), [late])
        gap = self.client.get('/v1/knowledge-gaps/' + GAP_ID, params={'scope': 'workspace'}, headers=self.headers()).json()
        # Current work alone ranks the gap; archived work is counted apart for the 'all' listing.
        self.assertEqual((gap['scientific_accounts']['count'], gap['scientific_accounts']['archived_count']), (1, 1))
        api.validate(gap, 'GapRecord')
        path = '/v1/knowledge-gaps/' + GAP_ID + '/accounts'
        items = self.client.get(path, params={'scope': 'workspace', 'reference_state': 'archived'}, headers=self.headers()).json()['items']
        self.assertEqual([item['account']['id'] for item in items], [late])
        # Publishing an archived account is allowed; the public copy drops private job/request ids.
        closure = {'coverage': {'complete': True}, 'document': {}, 'citation_metadata': [], 'artifacts': []}
        with patch.object(publication, 'full_document', return_value=({}, [], {})), \
                patch('reveal_backend.acceptance.object_envelope', return_value=closure), self.repo.transaction() as tx:
            publication.change(tx, self.owner, late, 'public', 0)
        public = self.client.get(path).json()['items']
        self.assertEqual([item['archive'] for item in public], [reference.public_stamp(stamp)])
        public_gap = self.client.get('/v1/knowledge-gaps/' + GAP_ID).json()['scientific_accounts']
        self.assertEqual((public_gap['count'], public_gap['archived_count']), (0, 1))
        self.assertIsNone(public[0]['archive']['analysis']['job_id']); self.assertIsNone(public[0]['job_id'])
        self.assertEqual(self.client.get(path, params={'reference_state': 'current'}).json()['items'], [])

    # ------------------------------------------------------------------ research collection
    def test_worker_collects_kpn_anchors_from_the_current_release_and_rejects_legacy_anchors(self):
        class Collected(Exception): pass
        frozen = {'question_id': GAP_ID, 'composer': {'source_gap': deepcopy(COMPOSER['source_gap']), 'dismissed_source_ids': [],
                  'selected_kgs': ['prokn'], 'eaggl_anchors': []}}
        budgets = {'candidates_per_type': 7, 'max_nodes': 81, 'max_edges': 191, 'max_accounts': 2}
        def collect(anchors, name):
            frozen['composer']['eaggl_anchors'] = [{'reference': {'source_id': anchor['cfde_node_id']}, 'origin': 'manual'} for anchor in anchors]
            return worker.collect({}, frozen, {'anchors': anchors, 'retrieval': {}}, budgets, self.root / name)
        with patch.object(worker, 'collect_reference_package', side_effect=Collected) as collector, \
                patch.object(worker, 'DapperRuntime', return_value='runtime'):
            # A binding frozen on an earlier release is collected from the current release.
            with self.assertRaises(Collected): collect([binding(KPN_ID, 'e' * 64)], 'kpn')
            with self.assertRaisesRegex(EvidenceBuildError, 'Legacy cfde-inc-v2 anchors cannot be collected'): collect([LEGACY_BINDING], 'legacy')
            with self.assertRaisesRegex(EvidenceBuildError, 'one known reference model'): collect([LEGACY_BINDING, binding(KPN_ID, R1)], 'mixed')
        self.assertEqual(collector.call_count, 1)
        call = collector.call_args.kwargs
        self.assertEqual(call['connection_factory'], worker.mysql_connection)
        self.assertFalse({'generation_id', 'release_id', 'model', 'geneset_import', 'geneset_resolver'} & set(call))
        self.assertEqual((call['factor_ids'], call['dapper'], call['limit'], call['max_nodes'], call['max_edges'], call['max_accounts']),
                         ([KPN_ID], 'runtime', 7, 81, 191, 2))
        self.assertEqual((call['gap_id'], call['selected_graphs'], call['project_root']),
                         (COMPOSER['source_gap']['source_id'], ['prokn'], ROOT))
        self.assertEqual(call['dismech_index'], ROOT / 'data/dismech-gaps/2026-09-24')
        self.assertEqual(set(call['selection_metadata']['origins']), {KPN_ID})
        self.assertEqual(call['selection_metadata']['semantic_retrieval']['embedding_run_id'], 'e' * 64)
        self.assertFalse(hasattr(worker, 'collect_package') or hasattr(worker, 'geneset_resolver'))

if __name__ == '__main__': unittest.main()
