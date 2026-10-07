"""Reference-generation guards, creation-time archive stamps and archive-aware reads.

Covers docs/reference-reload.md §5 and §8: superseded selections are rejected
(409), the reload gate holds analysis writes (503), late finishers are born
archived, listings filter by reference_state, and superseded factors are 410.
Legacy mode (no reference_active record) keeps today's behaviour.
"""
import asyncio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import importlib.util
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
from reveal_backend.evidence_package import canonical_json
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

MAPPING = 'a' * 64
LEGACY = reference.legacy_generation_id(MAPPING)
KPN = 'c' * 64
LEGACY_ID = 'factor:portal:T2D:cfde-inc-v2:Factor1'
KPN_ID = reference.public_id('KPN.TRAIT:0000398', 'Factor1')
LEGACY_BINDING = {'eaggl_factor_id': 'T2D::Factor1', 'eaggl_import_id': 'f' * 64, 'embedding_run_id': 'e' * 64,
                  'mapping_run_id': MAPPING, 'gene_set_import_id': 'd' * 64, 'cfde_node_id': LEGACY_ID}
KPN_BINDING = {'eaggl_factor_id': 'T2D::Factor1', 'reference_generation_id': KPN, 'model': reference.KPN_MODEL,
               'factor_key': 'KPN.TRAIT:0000398::Factor1', 'kpn_trait_id': 'KPN.TRAIT:0000398',
               'mapping_run_id': KPN, 'gene_set_import_id': KPN, 'cfde_node_id': KPN_ID}


def factor(source_id, letter):
    return {'source': 'eaggl', 'source_id': source_id, 'source_revision': letter * 64, 'object_class': 'Mechanism',
            'object': {'id': 'dapper:Mechanism.' + letter * 32, 'name': 'T2D mechanism Factor1', 'description': source_id},
            'cfde_anchor': {'node_id': source_id, 'node_type': 'factor', 'label': 'Label ' + letter, 'subtitle': 'T2D (Factor1)'}}


def source_ref(record):
    return {'source': 'eaggl', 'source_id': record['source_id'], 'source_revision': record['source_revision'],
            'dapper_id': record['object']['id']}


def selection(record):
    return {'reference': source_ref(record), 'origin': 'manual', 'suggestion_id': None}


class Generations:
    """Catalog double exposing the reference-generation attributes of docs §8."""
    embedding_run = 'embedding'; mapping_run = MAPPING

    def __init__(self):
        self.gaps = {GAP_ID: GAP}; self.mechanisms = {}; self.dismech_import = 'dismech-import'
        self.archived, self.snapshots = {}, {}
        self.serve_legacy()

    def serve_legacy(self):
        self.active_generation = None; self.reference_generation_id = LEGACY; self.model = reference.LEGACY_MODEL
        self.factors = {LEGACY_ID: factor(LEGACY_ID, '1')}; self.bindings = {LEGACY_ID: deepcopy(LEGACY_BINDING)}

    def serve_kpn(self):
        self.active_generation = KPN; self.reference_generation_id = KPN; self.model = reference.KPN_MODEL
        self.factors = {KPN_ID: factor(KPN_ID, '2')}; self.bindings = {KPN_ID: deepcopy(KPN_BINDING)}

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
    def context_embedding_provenance(self, *args): return {'dismech_embedding_run_id': 'context-run'}
    def provenance(self, *args): return {}


def contract_archive():
    """Stand-in for reveal_backend.reference_archive that follows the §4.2 placement contract."""
    module = types.ModuleType('reveal_backend.reference_archive'); module.calls = []
    places = {'account': 'summary', 'account_membership': 'summary', 'publication': 'summary', 'publication_snapshot': 'summary',
              'analysis_outcome': 'record', 'outcome_snapshot': 'record', 'outcome_summary': None,
              'outcome_publication': 'summary', 'request': None}
    public = {'publication', 'publication_snapshot', 'outcome_publication', 'outcome_snapshot'}

    def anchors_for_stamp(request_binding, *, draft_binding=None, scientific_document=None, composer=None, generation_id):
        module.calls.append({'generation_id': generation_id, 'scientific_document': scientific_document, 'composer': composer})
        return [{'source_id': anchor['cfde_node_id'], 'factor_id': anchor.get('eaggl_factor_id'),
                 'archived_reference_factor_id': reference.archive_id(generation_id, anchor['cfde_node_id'])}
                for anchor in request_binding['anchors']]

    def stamp_payload(kind, payload, stamp):
        payload = deepcopy(payload); target = payload[places[kind]] if places[kind] else payload
        target['archive'] = reference.public_stamp(stamp) if kind in public else stamp
        return payload

    module.anchors_for_stamp, module.stamp_payload = anchors_for_stamp, stamp_payload
    return module


class ReferenceGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = Repository(str(self.root / 'app.sqlite')); self.repo.migrate()
        self.catalog = Generations(); self.archive = contract_archive()
        # Replace only this key: restoring all of sys.modules would evict modules imported lazily meanwhile.
        name = 'reveal_backend.reference_archive'; previous = sys.modules.get(name); sys.modules[name] = self.archive
        self.addCleanup(lambda: sys.modules.__setitem__(name, previous) if previous is not None else sys.modules.pop(name, None))
        for context in (patch.object(api, 'repo', self.repo), patch.object(api, 'catalog', self.catalog),
                        patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 40, 'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs',
                                                'REVEAL_GATEWAY_AUDIENCE': 'reveal-api', 'REVEAL_ARTIFACTS_DIR': str(self.root)})):
            context.start(); self.addCleanup(context.stop)
        os.environ.pop('REVEAL_REFERENCE_GENERATION_ID', None)  # restored by patch.dict
        self.client = TestClient(api.app); self.owner = self.principal()

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

    def activate(self, generation=KPN):
        with self.repo.transaction() as tx:
            reference.write_active(tx, generation, reference.KPN_MODEL, expected_previous=None)

    def gate(self, closed):
        with self.repo.transaction() as tx: reference.set_gate(tx, closed, reason='test reload')

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

    # ------------------------------------------------------------------ guards
    def test_legacy_mode_freezes_and_submits_exactly_as_before(self):
        legacy = self.catalog.factors[LEGACY_ID]
        draft = self.create_draft(legacy)
        # Unchanged anchors of the served generation are retained with their first-saved runs.
        self.catalog.bindings[LEGACY_ID]['embedding_run_id'] = 'changed-embedding'
        patched = self.patch_draft(draft, {'expected_version': 1, 'composer': self.composer(legacy, mechanism_subquery='edited')})
        self.assertEqual(patched.status_code, 200, patched.text)
        response = self.submit(patched.json()); self.assertEqual(response.status_code, 202, response.text)
        with self.repo.read_transaction() as tx:
            binding = tx.get('request_binding', response.json()['research_request_id'])['data']
            self.assertEqual(binding['anchors'], [LEGACY_BINDING])
            self.assertIsNone(tx.get(reference.ACTIVE_KIND, reference.ACTIVE_ID))
        self.code(self.client.get('/v1/mechanisms/factor:portal:T2D:cfde-inc-v2:Factor9'), 404, 'NOT_FOUND')
        # Legacy mode never rejects manual anchors by generation.
        body = dict(deepcopy(SUGGEST), manual_eaggl_anchors=[source_ref(factor('factor:portal:X:cfde-inc-v2:Factor2', '9'))])
        self.assertEqual(self.client.post('/v1/mechanisms/suggest', json=body).status_code, 200)

    def test_catalog_loads_before_write_transactions_open(self):
        # A cold catalog load reads reference_active through the application pool: it must never nest
        # inside a pooled transaction (draft saves, submits and vote writes hold the global write lock).
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
        with tracking():
            draft = self.create_draft(self.catalog.factors[LEGACY_ID]); first = len(loads)
            patched = self.patch_draft(draft, {'expected_version': 1, 'composer': self.composer(self.catalog.factors[LEGACY_ID], mechanism_subquery='x')})
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

    def test_superseded_draft_submission_is_409_and_reload_gate_is_503(self):
        draft = self.create_draft(self.catalog.factors[LEGACY_ID])
        self.catalog.serve_kpn(); self.activate()
        body = self.code(self.submit(draft), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        self.assertFalse(body['retryable'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('job', self.owner), []); self.assertEqual(tx.list('request', self.owner), [])
        current = self.create_draft(self.catalog.factors[KPN_ID])
        account_id = 'dapper:ScientificAccount.' + 'a' * 32
        with self.repo.transaction() as tx:
            tx.put('account', digest([self.owner, account_id]), self.owner, {'result': {'document': {}}, 'summary': {}})
        self.gate(True)
        self.assertTrue(self.code(self.submit(current), 503, 'REFERENCE_RELOAD_IN_PROGRESS')['retryable'])
        paragraph = self.client.post('/v1/jobs', json={'kind': 'paragraph', 'account_id': account_id}, headers=self.headers())
        self.assertEqual(paragraph.status_code, 202, paragraph.text)
        self.gate(False)
        response = self.submit(current); self.assertEqual(response.status_code, 202, response.text)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('request_binding', response.json()['research_request_id'])['data']['anchors'], [KPN_BINDING])

    def test_draft_writes_revalidate_superseded_anchors_and_wait_for_the_gate(self):
        legacy, draft = self.catalog.factors[LEGACY_ID], self.create_draft(self.catalog.factors[LEGACY_ID])
        self.catalog.serve_kpn(); self.activate(); kpn = self.catalog.factors[KPN_ID]
        renamed = self.patch_draft(draft, {'expected_version': 1, 'name': 'Renamed'})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        stale = self.patch_draft(draft, {'expected_version': 2, 'composer': self.composer(legacy, mechanism_subquery='edited')})
        self.code(stale, 409, 'REFERENCE_GENERATION_SUPERSEDED')
        gap_only = self.patch_draft(draft, {'expected_version': 2, 'composer': self.composer()})
        self.assertEqual(gap_only.status_code, 200, gap_only.text)
        self.gate(True)
        self.code(self.patch_draft(draft, {'expected_version': 3, 'composer': self.composer(kpn)}), 503, 'REFERENCE_RELOAD_IN_PROGRESS')
        self.create_draft(kpn, status=503)
        self.create_draft()  # a gap-only draft ("new analysis with current factors") needs no reference data
        self.gate(False)
        current = self.patch_draft(draft, {'expected_version': 3, 'composer': self.composer(kpn)})
        self.assertEqual(current.status_code, 200, current.text)
        self.gate(True)
        edited = self.patch_draft(draft, {'expected_version': 4, 'composer': self.composer(kpn, mechanism_subquery='still editable')})
        self.assertEqual(edited.status_code, 200, edited.text)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('draft_binding', draft['id'])['data']['selections'][KPN_ID]['binding'], KPN_BINDING)

    def test_suggest_rejects_manual_anchors_outside_the_active_generation(self):
        self.catalog.serve_kpn()
        body = dict(deepcopy(SUGGEST), manual_eaggl_anchors=[source_ref(factor(LEGACY_ID, '1'))])
        self.code(self.client.post('/v1/mechanisms/suggest', json=body), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        body['manual_eaggl_anchors'] = [source_ref(self.catalog.factors[KPN_ID])]
        response = self.client.post('/v1/mechanisms/suggest', json=body)
        self.assertEqual(response.status_code, 200, response.text)

    def test_retry_review_rejects_superseded_generation_and_waits_for_gate(self):
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
        legacy = failed([LEGACY_BINDING])
        # Legacy mode: the guard passes; the (absent) saved capture is what refuses.
        self.code(retry(legacy), 409, 'REVIEW_CAPTURE_UNAVAILABLE')
        self.gate(True)
        self.code(retry(legacy), 503, 'REFERENCE_RELOAD_IN_PROGRESS')
        paragraph = failed([], 'paragraph', 'dapper:ScientificAccount.' + 'b' * 32)
        self.code(retry(paragraph), 409, 'REVIEW_CAPTURE_UNAVAILABLE')
        self.gate(False); self.activate()
        self.code(retry(legacy), 409, 'REFERENCE_GENERATION_SUPERSEDED')
        self.code(retry(failed([KPN_BINDING])), 409, 'REVIEW_CAPTURE_UNAVAILABLE')

    def test_generation_pin_overrides_the_active_record(self):
        with self.repo.read_transaction() as tx: self.assertIsNone(analysis_outcomes.active_reference_generation(tx))
        self.activate()
        with self.repo.read_transaction() as tx: self.assertEqual(analysis_outcomes.active_reference_generation(tx), KPN)
        with patch.dict(os.environ, {'REVEAL_REFERENCE_GENERATION_ID': 'e' * 64}), self.repo.read_transaction() as tx:
            self.assertEqual(analysis_outcomes.active_reference_generation(tx), 'e' * 64)

    # ------------------------------------------------------------------ reads
    def test_superseded_mechanism_is_gone_with_its_frozen_snapshot(self):
        self.catalog.serve_kpn()
        snapshot = {'format': 'reveal.archived-reference-factor/1', 'archive_id': reference.archive_id(LEGACY, LEGACY_ID),
                    'generation_id': LEGACY, 'source_id': LEGACY_ID, 'top_genes': [{'symbol': 'TCF7L2', 'loading': 0.5}]}
        self.catalog.archived[LEGACY_ID] = snapshot; self.catalog.snapshots[snapshot['archive_id']] = snapshot
        body = self.code(self.client.get('/v1/mechanisms/' + LEGACY_ID), 410, 'REFERENCE_GENERATION_SUPERSEDED')
        self.assertEqual(body['archived_reference_factor'], snapshot); self.assertFalse(body['retryable'])
        uncaptured = self.code(self.client.get('/v1/mechanisms/factor:portal:T2D:cfde-inc-v2:Factor7'), 410, 'REFERENCE_GENERATION_SUPERSEDED')
        self.assertIsNone(uncaptured['archived_reference_factor'])
        self.code(self.client.get('/v1/mechanisms/' + reference.public_id('KPN.TRAIT:0000001', 'Factor3')), 404, 'NOT_FOUND')
        self.assertEqual(self.client.get('/v1/mechanisms/' + KPN_ID).json(), self.catalog.factors[KPN_ID])
        self.code(self.client.get('/v1/mechanisms/' + KPN_ID, params={'source_revision': '0' * 64}), 409, 'SOURCE_REVISION_CHANGED')
        # Public reference data: no session needed; unknown and malformed ids are 404.
        response = self.client.get('/v1/reference-factors/' + snapshot['archive_id'])
        self.assertEqual(response.status_code, 200, response.text); self.assertEqual(response.json(), snapshot)
        self.code(self.client.get('/v1/reference-factors/' + '0' * 64), 404, 'NOT_FOUND')
        self.code(self.client.get('/v1/reference-factors/not-an-archive-id'), 404, 'NOT_FOUND')

    def test_account_detail_returns_archive_for_owner_and_public_snapshot(self):
        identity = ACCOUNT['root_id']; checksum = digest(ACCOUNT['document'])
        stamp = reference.build_stamp(LEGACY, KPN, reference={'model': reference.LEGACY_MODEL, 'anchors': []}, gap=None,
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

    # ------------------------------------------------------------------ stamping at creation
    def outcome(self, job, *, created_at=None):
        record = {'outcome': 'insufficient_evidence', 'summary': 'Scoped', 'reason': 'Missing link', 'knowledge_gap': GAP['object'],
                  'source_gap': deepcopy(COMPOSER['source_gap']), 'anchors': [{'source_id': LEGACY_ID, 'name': 'Frozen'}],
                  'created_at': created_at or now(), 'attribution': None, 'job_id': job['id'],
                  'provenance': {'evidence_package_sha256': '9' * 64, 'source_bindings': []}}
        prepared = {'record': record, 'artifacts': {}}
        with self.repo.transaction() as tx: return analysis_outcomes.save(tx, job, prepared), prepared

    def test_late_outcome_is_born_archived_and_published_without_private_ids(self):
        current, _ = self.outcome(self.job_with_binding([LEGACY_BINDING]), created_at='2026-09-01T00:00:00Z')
        self.activate()
        same, _ = self.outcome(self.job_with_binding([KPN_BINDING]), created_at='2026-09-02T00:00:00Z')
        document = {'knowledge_gaps': [GAP['object']], 'mechanisms': [factor(LEGACY_ID, '1')['object']]}
        job = self.job_with_binding([LEGACY_BINDING], document=document)
        late, prepared = self.outcome(job, created_at='2026-09-03T00:00:00Z')
        # The request's frozen catalog Mechanism nodes and composer origins feed the stamp anchors.
        self.assertEqual((self.archive.calls[-1]['scientific_document'], self.archive.calls[-1]['composer']['eaggl_anchors'][0]['origin']),
                         (document, 'automatic'))
        with self.repo.read_transaction() as tx:
            for identity in (current, same):
                self.assertNotIn('archive', tx.get('analysis_outcome', identity)['data']['record'])
                self.assertNotIn('archive', tx.get('outcome_summary', identity)['data'])
            record = tx.get('analysis_outcome', late)['data']['record']
            stamp = record['archive']
            self.assertEqual(tx.get('outcome_summary', late)['data']['archive'], stamp)
        self.assertEqual((stamp['status'], stamp['reason']), ('archived', 'reference_generation_superseded'))
        self.assertEqual((stamp['from_reference_generation'], stamp['to_reference_generation']), (LEGACY, KPN))
        self.assertEqual(stamp['analysis'], {'job_id': job['id'], 'request_id': job['research_request_id'], 'evidence_package_sha256': '9' * 64,
                                             'account_id': None, 'outcome_id': late})
        self.assertEqual(stamp['gap'], {'id': GAP_ID, 'source_id': COMPOSER['source_gap']['source_id'],
                                        'source_revision': COMPOSER['source_gap']['source_revision']})
        self.assertEqual(stamp['reference']['model'], reference.LEGACY_MODEL)
        self.assertEqual(stamp['reference']['anchors'][0]['archived_reference_factor_id'], reference.archive_id(LEGACY, LEGACY_ID))
        self.assertEqual(record['provenance'], prepared['record']['provenance'])  # stamp sits outside provenance
        with self.repo.transaction() as tx: self.assertEqual(analysis_outcomes.save(tx, job, prepared), late)  # replay
        with self.repo.transaction() as tx: analysis_outcomes.change(tx, late, self.owner, 'public', 0)
        with self.repo.read_transaction() as tx:
            published = tx.get('outcome_publication', late)['data']
            shared = tx.get('outcome_snapshot', published['snapshot_id'])['data']['record']['archive']
        self.assertEqual(shared, reference.public_stamp(stamp)); self.assertEqual(published['summary']['archive'], shared)
        self.assertIsNone(shared['analysis']['request_id']); self.assertEqual(shared['analysis']['outcome_id'], late)
        self.assertEqual(self.client.get('/v1/analysis-outcomes/' + late).json()['archive'], shared)
        self.assertEqual(self.client.get('/v1/analysis-outcomes/' + late, headers=self.headers()).json()['archive'], stamp)
        # Listings: all (current first, newest first within each group), current, archived.
        listed = lambda state=None: [item['id'] for item in self.client.get('/v1/analysis-outcomes', headers=self.headers(),
            params={'reference_state': state} if state else {}).json()['items']]
        self.assertEqual(listed(), [same, current, late])
        self.assertEqual(listed('current'), [same, current]); self.assertEqual(listed('archived'), [late])
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

    def test_late_account_is_born_archived_listed_after_current_work_and_not_counted(self):
        accept = self.accept_account
        _, current, account, membership = accept([LEGACY_BINDING], 'c')
        self.assertNotIn('archive', account['summary']); self.assertNotIn('archive', membership['summary'])
        self.activate()
        job, late, account, membership = accept([LEGACY_BINDING], 'd')
        stamp = account['summary']['archive']
        self.assertEqual(membership['summary']['archive'], stamp); self.assertNotIn('archive', account['result'])
        self.assertEqual((stamp['from_reference_generation'], stamp['to_reference_generation']), (LEGACY, KPN))
        self.assertEqual((stamp['analysis']['account_id'], stamp['analysis']['job_id']), (late, job['id']))
        self.assertEqual(self.archive.calls[-1]['scientific_document']['scientific_accounts'][0]['id'], late)
        listed = lambda state=None: [item['account']['id'] for item in self.client.get('/v1/accounts', headers=self.headers(),
            params={'reference_state': state} if state else {}).json()['items']]
        self.assertEqual(listed(), [current, late])  # newest first would put the archived account first
        self.assertEqual(listed('current'), [current]); self.assertEqual(listed('archived'), [late])
        gap = self.client.get('/v1/knowledge-gaps/' + GAP_ID, params={'scope': 'workspace'}, headers=self.headers()).json()
        # Current work alone ranks the gap; archived work is counted apart for the 'all' listing.
        self.assertEqual((gap['scientific_accounts']['count'], gap['scientific_accounts']['archived_count']), (1, 1))
        api.validate(gap, 'GapRecord')
        public_gap = self.client.get('/v1/knowledge-gaps/' + GAP_ID).json()['scientific_accounts']
        self.assertEqual(public_gap['count'], 0); self.assertNotIn('archived_count', public_gap)
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
    def test_worker_selects_the_collector_by_anchor_model(self):
        class Collected(Exception): pass
        frozen = {'question_id': GAP_ID, 'composer': {'source_gap': deepcopy(COMPOSER['source_gap']), 'dismissed_source_ids': [],
                  'selected_kgs': ['prokn'], 'eaggl_anchors': []}}
        def run(binding, name):
            frozen['composer']['eaggl_anchors'] = [{'reference': {'source_id': anchor['cfde_node_id']}, 'origin': 'manual'} for anchor in binding]
            with patch.object(worker, 'collect_reference_package', side_effect=Collected) as kpn, \
                    patch.object(worker, 'collect_package', side_effect=Collected) as legacy, \
                    patch.object(worker, 'geneset_resolver', return_value='resolver') as resolver, \
                    patch.object(worker, 'DapperRuntime', return_value='runtime'):
                with self.assertRaises(Collected):
                    worker.collect({}, frozen, {'anchors': binding, 'retrieval': {}},
                                   {'candidates_per_type': 7, 'max_nodes': 81, 'max_edges': 191, 'max_accounts': 2}, self.root / name)
            return kpn, legacy, resolver
        kpn, legacy, resolver = run([KPN_BINDING], 'kpn')
        legacy.assert_not_called(); resolver.assert_not_called()
        call = kpn.call_args.kwargs
        self.assertEqual((call['generation_id'], call['connection_factory']), (KPN, worker.mysql_connection))
        self.assertEqual((call['factor_ids'], call['dapper'], call['limit'], call['max_nodes'], call['max_edges'], call['max_accounts']),
                         ([KPN_ID], 'runtime', 7, 81, 191, 2))
        self.assertEqual((call['gap_id'], call['selected_graphs'], call['project_root']),
                         (COMPOSER['source_gap']['source_id'], ['prokn'], ROOT))
        self.assertEqual(call['dismech_index'], ROOT / 'data/dismech-gaps/2026-09-24')
        self.assertEqual(set(call['selection_metadata']['origins']), {KPN_ID})
        self.assertFalse({'model', 'geneset_import', 'geneset_resolver'} & set(call))
        kpn, legacy, resolver = run([LEGACY_BINDING], 'legacy')
        kpn.assert_not_called(); resolver.assert_called_once_with(LEGACY_BINDING['gene_set_import_id'])
        call = legacy.call_args.kwargs
        self.assertEqual((call['model'], call['geneset_resolver'], call['factor_ids']), (reference.LEGACY_MODEL, 'resolver', [LEGACY_ID]))
        self.assertEqual(call['geneset_import'], ROOT / 'data/cfde-genesets/2026-09-24')
        with self.assertRaisesRegex(ValueError, 'one known reference model'):
            run([LEGACY_BINDING, KPN_BINDING], 'mixed')


@unittest.skipUnless(importlib.util.find_spec('reveal_backend.reference_archive'), 'reference_archive is not installed yet')
class ReferenceArchiveIntegrationTests(unittest.TestCase):
    """With the real reference_archive module, late outcomes carry the §4.2 stamp outside provenance."""
    setUp = ReferenceGuardTests.setUp
    principal, activate, job_with_binding, outcome = (ReferenceGuardTests.principal, ReferenceGuardTests.activate,
                                                      ReferenceGuardTests.job_with_binding, ReferenceGuardTests.outcome)

    def test_real_module_stamps_late_outcome(self):
        sys.modules.pop('reveal_backend.reference_archive', None)  # use the installed module, not the stand-in
        self.activate()
        identity, prepared = self.outcome(self.job_with_binding([LEGACY_BINDING]))
        with self.repo.read_transaction() as tx:
            record = tx.get('analysis_outcome', identity)['data']['record']
            summary = tx.get('outcome_summary', identity)['data']
        self.assertTrue(reference.is_archived(record)); self.assertEqual(summary['archive'], record['archive'])
        self.assertEqual(record['archive']['to_reference_generation'], KPN)
        self.assertEqual(record['provenance'], prepared['record']['provenance'])

    accept_account = ReferenceGuardTests.accept_account

    def test_real_module_stamps_late_account_and_membership(self):
        sys.modules.pop('reveal_backend.reference_archive', None)
        self.activate()
        job, identity, account, membership = self.accept_account([LEGACY_BINDING], 'e')
        self.assertTrue(reference.is_archived(account['summary'])); self.assertEqual(membership['summary']['archive'], account['summary']['archive'])
        stamp = account['summary']['archive']
        self.assertEqual((stamp['from_reference_generation'], stamp['to_reference_generation'], stamp['analysis']['account_id']), (LEGACY, KPN, identity))
        self.assertEqual([anchor['source_id'] for anchor in stamp['reference']['anchors']], [LEGACY_ID])
        self.assertNotIn('archive', account['result'])


if __name__ == '__main__': unittest.main()
