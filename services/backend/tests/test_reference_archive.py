"""Archive-on-reload (docs/reference-reload.md §3-§4): classification, stamp placement,
idempotency and history, anchored-draft drops, anchor_display backfill, job cancellation
and frozen factor snapshots. SQLite repositories and fake DB-API cursors only."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import analysis_outcomes as outcomes, jobs
from reveal_backend import reference_archive as archive
from reveal_backend import reference_generation as reference
from reveal_backend.repository import Repository, canonical, digest, uid

MAPPING = 'a' * 64
LEGACY = reference.legacy_generation_id(MAPPING)
KPN, KPN2 = 'b' * 64, 'c' * 64
LEGACY_ID = 'factor:portal:T2D:cfde-inc-v2:Factor1'
KPN_ID = reference.public_id('KPN.TRAIT:0000398', 'Factor2')
MECHANISM, KPN_MECHANISM = 'dapper:Mechanism.' + '1' * 32, 'dapper:Mechanism.' + '2' * 32
GAP = {'id': 'dapper:KnowledgeGap.' + '3' * 32, 'source_id': 'dismech:gap-1', 'source_revision': '4' * 64}
CATALOG_GAP = {'object': {'id': GAP['id'], 'text': 'Why does secretion fail?'},
               'source': {'source': 'dismech', 'source_id': GAP['source_id'], 'source_revision': GAP['source_revision']}}
LEGACY_BINDING = {'eaggl_factor_id': 'T2D::Factor1', 'eaggl_import_id': 'e' * 64, 'embedding_run_id': 'f' * 64,
                  'mapping_run_id': MAPPING, 'gene_set_import_id': 'd' * 64, 'cfde_node_id': LEGACY_ID,
                  'cfde_payload': {'label': 'Insulin secretion'}}
KPN_BINDING = {'eaggl_factor_id': 'T2D::Factor2', 'eaggl_import_id': 'e' * 64, 'embedding_run_id': 'f' * 64,
               'reference_generation_id': KPN, 'model': reference.KPN_MODEL, 'factor_key': 'KPN.TRAIT:0000398::Factor2',
               'kpn_trait_id': 'KPN.TRAIT:0000398', 'mapping_run_id': KPN, 'gene_set_import_id': KPN, 'cfde_node_id': KPN_ID}
LEGACY_ANCHOR = (LEGACY_ID, MECHANISM, LEGACY_BINDING, 'Insulin secretion', 'T2D (Factor1)',
                 {'id': MECHANISM, 'name': 'T2D mechanism Factor1',
                  'description': f'EAGGL mechanism {LEGACY_ID}. Source label: Insulin secretion.'})
KPN_ANCHOR = (KPN_ID, KPN_MECHANISM, KPN_BINDING, 'Beta cell stress', 'Type 2 diabetes (Factor2)',
              {'id': KPN_MECHANISM, **reference.mechanism_node(KPN_ID, 'Type 2 diabetes', 'KPN.TRAIT:0000398', 'Factor2', 'Beta cell stress')})
T1, T2 = '2026-10-01T00:00:00Z', '2027-01-01T00:00:00Z'
SOURCE = Path(__file__).resolve().parents[1] / 'src/reveal_backend'


def reference_of(anchor):
    native, mechanism = anchor[0], anchor[1]
    return {'source': 'eaggl', 'source_id': native, 'source_revision': '5' * 64, 'dapper_id': mechanism}


def selection_of(anchor):
    native, _, binding, label, subtitle, node = anchor
    return {'reference': reference_of(anchor), 'binding': deepcopy(binding),
            'record': {'source_id': native, 'source_revision': '5' * 64, 'object': deepcopy(node),
                       'cfde_anchor': {'node_id': native, 'node_type': 'factor', 'label': label, 'subtitle': subtitle}}}


class Repo(unittest.TestCase):
    """A SQLite application repository seeded like a real prefix."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name) / 'app.sqlite'), table_prefix='reveal_reload_rehearsal'); self.repo.migrate()
        for context in (patch.dict(os.environ, {'REVEAL_JOB_TRANSPORT': 'database', 'REVEAL_JOB_NAMESPACE': 'reveal'}),
                        patch('reveal_backend.redis_notifications.publish')):
            context.start(); self.addCleanup(context.stop)
        self.owner, self.other = self.principal(), self.principal()

    def principal(self):
        identity = uid()
        with self.repo.transaction() as tx:
            tx.put('principal', identity, identity, {'retired': False, 'me': {'user_id': identity, 'principal_kind': 'registered'}})
        return identity

    def put(self, kind, identity, owner, data):
        with self.repo.transaction() as tx: tx.put(kind, identity, owner, data)

    def get(self, kind, identity):
        with self.repo.read_transaction() as tx: return tx.get(kind, identity)

    def all_rows(self):
        with self.repo.read_transaction() as tx:
            return {(kind, identity): (version, payload) for kind, identity, version, payload in
                    tx.execute('SELECT kind,id,version,payload FROM reveal_records').fetchall()}

    def activate(self, generation=KPN, previous=None):
        with self.repo.transaction() as tx: reference.write_active(tx, generation, reference.KPN_MODEL, expected_previous=previous)

    def draft(self, owner, anchors=(), *, updated_at='2026-09-01T00:00:00Z'):
        identity = uid()
        composer = {'source_gap': deepcopy(GAP), 'eaggl_anchors': [{'reference': reference_of(a), 'origin': 'automatic'} for a in anchors],
                    'dismissed_source_ids': [], 'mechanism_subquery': '', 'model': 'cfde-inc-v2', 'selected_kgs': []}
        saved = {'dismech_import_id': 'import', 'source_gap': deepcopy(CATALOG_GAP), 'selections': {a[0]: selection_of(a) for a in anchors}}
        with self.repo.transaction() as tx:
            tx.put('draft', identity, owner, {'id': identity, 'owner_user_id': owner, 'version': 3, 'composer': composer,
                                              'created_at': updated_at, 'updated_at': updated_at})
            tx.put('draft_binding', identity, owner, saved)
        return identity, composer, saved

    def submit(self, owner, drafted, anchors, *, display=True, status='succeeded', result=None, collected=True):
        draft_id, composer, saved = drafted
        identity = uid()
        binding = {'dismech_import_id': 'import', 'source_gap': deepcopy(CATALOG_GAP), 'anchors': [deepcopy(a[2]) for a in anchors],
                   'retrieval': {a[0]: None for a in anchors}}
        if display: binding['anchor_display'] = outcomes.anchor_display(composer, binding, saved)
        with self.repo.transaction() as tx:
            tx.put('request', identity, owner, {'id': identity, 'owner_user_id': owner, 'source_draft_id': draft_id, 'source_draft_version': 3,
                'composer': deepcopy(composer), 'question_id': GAP['id'],
                'document': {'knowledge_gaps': [CATALOG_GAP['object']], 'mechanisms': [deepcopy(a[5]) for a in anchors]},
                'attribution': {'user_id': owner}, 'submitted_at': '2026-09-02T00:00:00Z'})
            tx.put('request_binding', identity, owner, binding)
            job = jobs.enqueue(tx, owner, 'analysis', request_id=identity, inputs={'kind': 'analysis'})
            if status != 'queued':
                job.update(status=status, stage='complete' if status in jobs.TERMINAL else 'running', result=result)
                tx.put('job', job['id'], owner, job)
            if collected:
                queue = tx.get('queue', job['id'])['data']; queue['dispatch_input'] = {'kind': 'analysis', 'sha256': '9' * 64}
                tx.put('queue', job['id'], owner, queue)
        return identity, job

    def account(self, owner, job_id, letter, anchors, *, publish=True):
        account_id = 'dapper:ScientificAccount.' + letter * 32
        summary = {'account': {'id': account_id, 'question': GAP['id'], 'component_claims': []}, 'knowledge_gap': CATALOG_GAP['object'],
                   'claim_count': 0, 'created_at': '2026-09-03T00:00:00Z', 'job_id': job_id,
                   'research_statement': {'status': 'queued', 'job_id': uid(), 'paragraph_id': None}}
        document = {'scientific_accounts': [summary['account']], 'mechanisms': [deepcopy(a[5]) for a in anchors]}
        with self.repo.transaction() as tx:
            tx.put('account', digest([owner, account_id]), owner, {'result': {'root_id': account_id, 'document': document,
                'research_statement': summary['research_statement']}, 'summary': summary})
            tx.put('account_membership', digest([owner, account_id]), owner, {'account_id': account_id, 'summary': summary})
            if publish:
                public = dict(deepcopy(summary), job_id=None, research_statement={'status': 'not_requested', 'job_id': None, 'paragraph_id': None})
                snapshot = uid()
                tx.put('publication_snapshot', snapshot, owner, {'account_id': account_id, 'paragraph_id': None, 'document': document,
                    'citation_metadata': [], 'artifacts': {}, 'artifact_records': {}, 'summary': public})
                tx.put('publication', digest([owner, account_id]), owner, {'account_id': account_id, 'visibility': 'public', 'version': 1,
                    'published_at': '2026-09-04T00:00:00Z', 'updated_at': '2026-09-04T00:00:00Z', 'snapshot_id': snapshot, 'paragraph_id': None,
                    'object_ids': [], 'citation_keys': [], 'artifact_sha256': [], 'summary': public})
        return account_id

    def outcome(self, owner, job_id, anchors, *, publish=True):
        identity = uid()
        record = {'outcome': 'insufficient_evidence', 'summary': 'Scoped', 'reason': 'Missing link', 'knowledge_gap': CATALOG_GAP['object'],
                  'source_gap': deepcopy(GAP), 'selected_kgs': [], 'created_at': '2026-09-05T00:00:00Z', 'attribution': None,
                  'anchors': [{'source_id': a[0], 'mechanism_id': a[1], 'name': a[3], 'trait': 'T2D', 'origin': 'automatic'} for a in anchors],
                  'scope_note': 'scoped', 'record_format': 'structured', 'job_id': job_id, 'id': identity,
                  'provenance': {'evidence_package_sha256': '8' * 64, 'coverage': {'complete': True},
                                 'source_bindings': [{'source_id': a[0], 'source_revision': '5' * 64, 'embedding_run_id': a[2]['embedding_run_id'],
                                                      'mapping_run_id': a[2]['mapping_run_id']} for a in anchors]}}
        listed = {key: deepcopy(record[key]) for key in ('id', 'outcome', 'summary', 'knowledge_gap', 'anchors', 'created_at', 'attribution')}
        with self.repo.transaction() as tx:
            tx.put('analysis_outcome', identity, owner, {'record': record, 'artifacts': {}})
            tx.put('outcome_summary', identity, owner, listed)
            tx.put('analysis_outcome_by_job', job_id, owner, {'id': identity})
            if publish:
                snapshot = uid(); public = dict(deepcopy(record), job_id=None)
                tx.put('outcome_snapshot', snapshot, owner, {'record': public, 'artifacts': {}})
                tx.put('outcome_publication', identity, owner, {'visibility': 'public', 'version': 1, 'published_at': '2026-09-06T00:00:00Z',
                    'updated_at': '2026-09-06T00:00:00Z', 'snapshot_id': snapshot, 'summary': dict(listed), 'artifact_sha256': []})
        return identity


class ClassificationTests(unittest.TestCase):
    def test_every_kind_the_backend_writes_is_classified(self):
        written = set()
        for path in SOURCE.glob('*.py'):
            text = path.read_text()
            written |= set(re.findall(r"\b(?:put|update_existing|remove|insert_many)\(\s*\[?\(?'([a-z_]+)'", text))
            # Batched rows are appended as (kind, id, owner, data) tuples before insert_many.
            written |= set(re.findall(r"\brecords\.append\(\(\s*'([a-z_]+)'\s*,", text))
        written |= {reference.ACTIVE_KIND, reference.CONTROL_KIND, reference.ARCHIVE_RUN_KIND, reference.RELOAD_KIND}
        self.assertTrue({'account', 'suggestion', 'workspace_cursor', 'remote_event', 'vector_batch'} <= written)
        self.assertEqual(sorted(kind for kind in written if archive.classify(kind) is None), [])
        self.assertLessEqual(set(archive.KIND_ACTIONS.values()), {'archive', 'keep', 'drop_anchored_draft', 'cancel_nonterminal', 'purge_at_retire'})

    def test_contract_classification(self):
        for kind in ('account', 'account_membership', 'publication', 'publication_snapshot', 'analysis_outcome', 'outcome_snapshot',
                     'outcome_summary', 'outcome_publication', 'request'):
            self.assertEqual(archive.classify(kind), 'archive')
        for kind in ('request_binding', 'evidence', 'exploration', 'workspace_event', 'workspace_cursor', 'object', 'grant', 'citation',
                     'principal', 'identity', 'citation_actor', 'reference_active', 'idempotency', 'upload', 'fixture_seed',
                     # Written by votes.py on main: ballots (owner = voter) and tallies (owner = system), keyed by gap or account.
                     'vote', 'vote_total'):
            self.assertEqual(archive.classify(kind), 'keep')
        self.assertEqual((archive.classify('draft'), archive.classify('draft_binding'), archive.classify('job')),
                         ('drop_anchored_draft', 'drop_anchored_draft', 'cancel_nonterminal'))
        self.assertEqual({archive.classify(k) for k in ('suggestion', 'vector_snapshot', 'vector_batch')}, {'purge_at_retire'})
        self.assertIsNone(archive.classify('mystery'))


class StampPlacementTests(unittest.TestCase):
    STAMP = reference.build_stamp(LEGACY, KPN, reference={'model': reference.LEGACY_MODEL, 'anchors': []}, gap=GAP,
                                  analysis={'job_id': 'job', 'request_id': 'request', 'evidence_package_sha256': '8' * 64,
                                            'account_id': 'account', 'outcome_id': None}, at=T1)

    def test_each_kind_carries_the_stamp_in_its_place(self):
        payloads = {'account': {'result': {'root_id': 'x'}, 'summary': {'job_id': 'job'}},
                    'account_membership': {'account_id': 'x', 'summary': {}}, 'publication': {'version': 2, 'summary': {'job_id': None}},
                    'publication_snapshot': {'summary': {}, 'document': {}}, 'analysis_outcome': {'record': {'provenance': {'a': 1}}, 'artifacts': {}},
                    'outcome_snapshot': {'record': {'provenance': {}}}, 'outcome_summary': {'id': 'o'},
                    'outcome_publication': {'version': 3, 'summary': {'id': 'o'}}, 'request': {'composer': {}}}
        places = {'account': ('summary',), 'account_membership': ('summary',), 'publication': ('summary',), 'publication_snapshot': ('summary',),
                  'analysis_outcome': ('record',), 'outcome_snapshot': ('record',), 'outcome_summary': (), 'outcome_publication': ('summary',), 'request': ()}
        for kind, payload in payloads.items():
            original = deepcopy(payload)
            stamped = archive.stamp_payload(kind, payload, self.STAMP)
            self.assertEqual(payload, original, kind)  # never mutates its input
            target = stamped
            for key in places[kind]: target = target[key]
            expected = reference.public_stamp(self.STAMP) if kind in archive.PUBLIC_KINDS else self.STAMP
            self.assertEqual(target['archive'], expected, kind)
            self.assertEqual(archive.stamp_of(kind, stamped), expected)
            self.assertTrue(reference.is_archived(target))
            if kind in archive.PUBLIC_KINDS: self.assertEqual((target['archive']['analysis']['job_id'], target['archive']['analysis']['request_id']), (None, None))
            del target['archive']
            self.assertEqual(stamped, original, kind)  # nothing else changed: result, provenance, versions
        self.assertEqual(archive.stamp_payload('publication', {'version': 1, 'summary': None}, self.STAMP), {'version': 1, 'summary': None})
        self.assertEqual(archive.stamp_payload('outcome_publication', {'summary': None}, self.STAMP), {'summary': None})
        self.assertIsNone(archive.stamp_of('publication', {'summary': None}))
        with self.assertRaises(reference.ReferenceError): archive.stamp_payload('object', {}, self.STAMP)


class AnchorTests(unittest.TestCase):
    def test_request_binding_display_and_composer(self):
        binding = {'anchors': [LEGACY_BINDING], 'anchor_display': {LEGACY_ID: {'reference': reference_of(LEGACY_ANCHOR),
                                                                             'label': 'Insulin secretion', 'subtitle': 'T2D (Factor1)'}}}
        composer = {'eaggl_anchors': [{'reference': reference_of(LEGACY_ANCHOR), 'origin': 'manual'}]}
        anchors = archive.anchors_for_stamp(binding, scientific_document={'mechanisms': [LEGACY_ANCHOR[5]]}, composer=composer, generation_id=LEGACY)
        self.assertEqual(anchors, [{'source_id': LEGACY_ID, 'mechanism_id': MECHANISM, 'factor_id': 'T2D::Factor1', 'trait': 'T2D',
            'kpn_trait_id': None, 'label': 'Insulin secretion', 'name': 'T2D mechanism Factor1', 'origin': 'manual',
            'archived_reference_factor_id': reference.archive_id(LEGACY, LEGACY_ID)}])
        self.assertEqual(tuple(anchors[0]), archive.ANCHOR_FIELDS)

    def test_draft_selection_fallback_only_for_the_same_frozen_binding(self):
        saved = {'selections': {KPN_ID: selection_of(KPN_ANCHOR)}}
        anchor, = archive.anchors_for_stamp({'anchors': [KPN_BINDING]}, draft_binding=saved, generation_id=KPN)
        self.assertEqual((anchor['label'], anchor['trait'], anchor['kpn_trait_id'], anchor['mechanism_id'], anchor['name']),
                         ('Beta cell stress', 'Type 2 diabetes', 'KPN.TRAIT:0000398', KPN_MECHANISM, KPN_ANCHOR[5]['name']))
        self.assertEqual(anchor['archived_reference_factor_id'], reference.archive_id(KPN, KPN_ID))
        changed = deepcopy(saved); changed['selections'][KPN_ID]['binding']['embedding_run_id'] = 'other'
        anchor, = archive.anchors_for_stamp({'anchors': [KPN_BINDING]}, draft_binding=changed, generation_id=KPN)
        self.assertEqual((anchor['label'], anchor['mechanism_id'], anchor['trait']), (None, None, 'T2D'))

    def test_mechanism_nodes_of_a_scientific_document_row(self):
        row = {'sha256': 'x', 'document': {'mechanisms': [KPN_ANCHOR[5], LEGACY_ANCHOR[5], {'id': 'dapper:Mechanism.dismech', 'name': 'Other',
                                                                                          'description': 'A DisMech mechanism.'}]}}
        anchors = archive.anchors_for_stamp(None, scientific_document=row, generation_id=LEGACY)
        self.assertEqual([(a['source_id'], a['label'], a['kpn_trait_id'], a['mechanism_id']) for a in anchors],
                         [(KPN_ID, 'Beta cell stress', 'KPN.TRAIT:0000398', KPN_MECHANISM), (LEGACY_ID, 'Insulin secretion', None, MECHANISM)])
        self.assertEqual(anchors[1]['factor_id'], 'T2D::Factor1')
        self.assertEqual(archive.anchors_for_stamp(None, generation_id=LEGACY), [])


class CutoverTests(Repo):
    """A legacy prefix cut over to KPN, then to KPN2."""

    def setUp(self):
        super().setUp()
        owner = self.owner
        self.anchored = self.draft(owner, [LEGACY_ANCHOR], updated_at='2026-09-01T00:00:00Z')
        self.gap_only = self.draft(owner, [], updated_at='2026-08-01T00:00:00Z')
        self.put('exploration', digest([owner, GAP['id']]), owner, {'source_gap': deepcopy(GAP), 'knowledge_gap': CATALOG_GAP['object'],
                 'draft_id': self.anchored[0], 'last_explored_at': '2026-09-01T00:00:00Z'})
        self.account_id = 'dapper:ScientificAccount.' + 'a' * 32
        self.r1, self.j1 = self.submit(owner, self.anchored, [LEGACY_ANCHOR], display=False, result={'kind': 'analysis',
            'account_ids': [self.account_id], 'paragraph_job_ids': [], 'evidence_package_sha256': '7' * 64})
        self.account(owner, self.j1['id'], 'a', [LEGACY_ANCHOR])
        # Community votes (votes.py on main) on the published account: kept untouched, never stamped.
        self.put('vote_total', digest(['account', self.account_id]), 'system', {'target_kind': 'account', 'target_id': self.account_id,
                 'gap_id': GAP['id'], 'upvotes': 1, 'downvotes': 0})
        self.put('vote', digest([self.other, 'account', self.account_id]), self.other, {'target_kind': 'account',
                 'target_id': self.account_id, 'gap_id': GAP['id'], 'vote': 1, 'updated_at': T1})
        self.r2, self.j2 =self.submit(owner, self.anchored, [LEGACY_ANCHOR], status='insufficient_evidence')
        self.outcome_id = self.outcome(owner, self.j2['id'], [LEGACY_ANCHOR])
        self.r3, self.j3 = self.submit(owner, self.anchored, [LEGACY_ANCHOR], status='queued', collected=False)
        self.r4, self.j4 = self.submit(owner, self.anchored, [LEGACY_ANCHOR], status='running', collected=True)
        with self.repo.transaction() as tx:
            self.j5 = jobs.enqueue(tx, owner, 'paragraph', account_id=self.account_id, inputs={'kind': 'paragraph'})
        # An account whose job row is gone: only a legacy cutover can archive it, from its frozen Mechanism nodes.
        self.orphan_id = self.account(owner, 'missing-job', 'b', [LEGACY_ANCHOR], publish=False)
        # Another owner: an unpublished publication (null summary) and an anchored draft.
        self.put('publication', digest([self.other, 'x']), self.other, {'account_id': 'x', 'visibility': 'private', 'version': 2, 'summary': None})
        self.other_draft = self.draft(self.other, [LEGACY_ANCHOR])

    def stamp(self, kind, identity):
        return archive.stamp_of(kind, self.get(kind, identity)['data'])

    def run_cutover(self, **options):
        self.activate()
        return archive.archive_prefix(self.repo, LEGACY, KPN, apply=True, at=T1, **options)

    def test_plan_is_read_only_deterministic_and_fails_closed(self):
        before = self.all_rows()
        plan = archive.plan_prefix(self.repo, KPN, from_generation=LEGACY)
        self.assertEqual(plan, archive.plan_prefix(self.repo, KPN, from_generation=LEGACY))
        self.assertEqual(self.all_rows(), before)
        self.assertTrue(plan['ok']); self.assertEqual(plan['unknown_kinds'], []); self.assertTrue(plan['legacy_mode'])
        self.assertEqual(plan['counts_by_kind']['request'], 4); self.assertEqual(plan['actions']['draft'], 'drop_anchored_draft')
        self.assertEqual((plan['actions']['vote'], plan['actions']['vote_total']), ('keep', 'keep'))
        self.assertEqual({item['id'] for item in plan['archive_candidates']['account']},
                         {digest([self.owner, self.account_id]), digest([self.owner, self.orphan_id])})
        self.assertEqual(len(plan['archive_candidates']['request']), 4)
        self.assertNotIn('publication', {kind for kind, items in plan['archive_candidates'].items()
                                         if any(item['owner'] == self.other for item in items)})
        self.assertEqual({item['id'] for item in plan['anchored_drafts']}, {self.anchored[0], self.other_draft[0]})
        self.assertNotIn('explorations_repointed', plan)  # explorations are kept untouched (docs §4.2)
        self.assertEqual(plan['anchor_display_backfill'], {'missing': 1, 'recoverable': 1, 'unrecoverable': []})
        actions = {item['id']: item['action'] for item in plan['nonterminal_jobs']}
        self.assertEqual(actions, {self.j3['id']: 'cancel', self.j4['id']: 'wait', self.j5['id']: 'continue'})
        self.assertEqual({item['kind'] for item in plan['unresolved'] if item['archived']}, {'account', 'account_membership'})
        self.put('mystery_kind', 'x', 'system', {})
        plan = archive.plan_prefix(self.repo, KPN)
        self.assertEqual((plan['unknown_kinds'], plan['ok']), (['mystery_kind'], False))

    def test_dry_run_changes_nothing_and_apply_requires_activation(self):
        before = self.all_rows()
        report = archive.archive_prefix(self.repo, LEGACY, KPN, apply=False, at=T1)
        self.assertEqual(self.all_rows(), before)
        self.assertEqual(report['counts']['account'], 2); self.assertEqual(len(report['dropped_drafts']), 2)
        with self.assertRaisesRegex(reference.ReferenceError, 'Activate'):
            archive.archive_prefix(self.repo, LEGACY, KPN, apply=True)
        with self.assertRaises(reference.ReferenceError): archive.archive_prefix(self.repo, LEGACY, LEGACY, apply=False)
        self.assertEqual(self.all_rows(), before)

    def test_cutover_keeps_uploads_and_fixture_receipts_with_frozen_input_refs(self):
        upload_id, receipt_id = uid(), uid()
        upload = {'id': upload_id, 'draft_id': self.anchored[0], 'filename': 'private-notes.txt',
                  'status': 'ready', 'storage': {'store': 's3', 'key': 'local/immutable-input',
                    'version_id': 'captured-version', 'sha256': 'f' * 64},
                  'extraction': {'text': 'Private observation'}, 'expires_at': T2}
        receipt = {'format': 'reveal.fixture-seed-receipt/1', 'account_id': self.account_id,
                   'content_sha256': 'e' * 64, 'scientific_acceptance': 'not_reviewed'}
        self.put('upload', upload_id, self.owner, upload)
        self.put('fixture_seed', receipt_id, self.owner, receipt)
        with self.repo.transaction() as tx:
            request = tx.get('request', self.r1)['data']
            request['user_inputs'] = {'format': 'reveal.user-inputs/1', 'context': 'Private context',
                                      'research_direction': '', 'hypotheses': '', 'uploads': [deepcopy(upload)]}
            tx.put('request', self.r1, self.owner, request)
        before = self.all_rows()
        plan = archive.plan_prefix(self.repo, KPN, from_generation=LEGACY)
        self.assertTrue(plan['ok']); self.assertEqual(plan['unknown_kinds'], [])
        self.assertEqual({kind: plan['actions'][kind] for kind in ('upload', 'fixture_seed')},
                         {'upload': 'keep', 'fixture_seed': 'keep'})
        self.assertEqual(self.all_rows(), before)
        self.run_cutover()
        after = self.all_rows()
        for key in (('upload', upload_id), ('fixture_seed', receipt_id)):
            self.assertEqual(after[key], before[key])
        self.assertIsNone(self.get('draft', self.anchored[0]))
        self.assertEqual(self.get('request', self.r1)['data']['user_inputs'], request['user_inputs'])
        self.assertIsNotNone(self.stamp('request', self.r1))

    def test_cutover_stamps_every_archived_kind_in_place(self):
        before = self.all_rows()
        report = self.run_cutover()
        owner, key = self.owner, digest([self.owner, self.account_id])
        stamp = self.stamp('account', key)
        anchor = {'source_id': LEGACY_ID, 'mechanism_id': MECHANISM, 'factor_id': 'T2D::Factor1', 'trait': 'T2D', 'kpn_trait_id': None,
                  'label': 'Insulin secretion', 'name': 'T2D mechanism Factor1', 'origin': 'automatic',
                  'archived_reference_factor_id': reference.archive_id(LEGACY, LEGACY_ID)}
        self.assertEqual(stamp, {'status': 'archived', 'reason': 'reference_generation_superseded', 'archived_at': T1,
            'from_reference_generation': LEGACY, 'to_reference_generation': KPN,
            'history': [{'from_reference_generation': LEGACY, 'to_reference_generation': KPN, 'archived_at': T1}],
            'reference': {'model': 'cfde-inc-v2', 'anchors': [anchor]}, 'gap': GAP,
            'analysis': {'job_id': self.j1['id'], 'request_id': self.r1, 'evidence_package_sha256': '7' * 64,
                         'account_id': self.account_id, 'outcome_id': None}})
        public = reference.public_stamp(stamp)
        # Only the stamp is added; result (root_id) and logical versions stay; row versions bump once.
        after = self.get('account', key)['data']; old = before[('account', key)]
        self.assertEqual(after['result'], json.loads(old[1])['result'])
        self.assertEqual(self.get('account', key)['version'], old[0] + 1)
        self.assertEqual(self.stamp('account_membership', key), stamp)
        publication = self.get('publication', key)
        self.assertEqual((publication['data']['summary']['archive'], publication['data']['version'], publication['version']),
                         (public, 1, before[('publication', key)][0] + 1))
        self.assertEqual(self.stamp('publication_snapshot', publication['data']['snapshot_id']), public)
        outcome = self.get('analysis_outcome', self.outcome_id)['data']['record']
        self.assertEqual(outcome['archive']['analysis'], {'job_id': self.j2['id'], 'request_id': self.r2, 'evidence_package_sha256': '8' * 64,
                                                          'account_id': None, 'outcome_id': self.outcome_id})
        self.assertEqual(outcome['provenance'], json.loads(before[('analysis_outcome', self.outcome_id)][1])['record']['provenance'])
        self.assertEqual(self.get('outcome_summary', self.outcome_id)['data']['archive'], outcome['archive'])
        published = self.get('outcome_publication', self.outcome_id)['data']
        self.assertEqual((published['summary']['archive'], published['version']), (reference.public_stamp(outcome['archive']), 1))
        self.assertEqual(self.stamp('outcome_snapshot', published['snapshot_id']), reference.public_stamp(outcome['archive']))
        request = self.get('request', self.r1)['data']['archive']
        self.assertEqual((request['analysis']['job_id'], request['analysis']['account_id']), (self.j1['id'], self.account_id))
        self.assertEqual(self.get('request', self.r2)['data']['archive']['analysis']['outcome_id'], self.outcome_id)
        # The request binding gained its display from the saved selection before the draft was dropped.
        binding = self.get('request_binding', self.r1)['data']
        self.assertEqual(binding['anchor_display'][LEGACY_ID]['label'], 'Insulin secretion')
        self.assertEqual({k: v for k, v in binding.items() if k != 'anchor_display'},
                         {k: v for k, v in json.loads(before[('request_binding', self.r1)][1]).items()})
        # Anchored drafts are dropped with their bindings, gap-only drafts stay; explorations are kept untouched
        # (their row order is the workspace's recently-explored order, and the workspace falls back past a dropped draft).
        for owner_draft in (self.anchored[0], self.other_draft[0]):
            self.assertIsNone(self.get('draft', owner_draft)); self.assertIsNone(self.get('draft_binding', owner_draft))
        self.assertIsNotNone(self.get('draft', self.gap_only[0]))
        self.assertEqual(self.get('exploration', digest([owner, GAP['id']]))['data']['draft_id'], self.anchored[0])
        # Unresolvable rows are still archived on a legacy cutover, from their frozen Mechanism nodes.
        orphan = self.stamp('account', digest([owner, self.orphan_id]))
        self.assertEqual((orphan['from_reference_generation'], orphan['reference']['anchors'][0]['label'], orphan['analysis']['job_id']),
                         (LEGACY, 'Insulin secretion', 'missing-job'))
        self.assertEqual(orphan['gap'], {'id': GAP['id'], 'source_id': None, 'source_revision': None})
        self.assertEqual({(item['kind'], item['reason'], item['archived']) for item in report['unresolved']},
                         {('account', 'job_missing', True), ('account_membership', 'job_missing', True)})
        # Kept kinds are untouched; the unpublished publication has no summary to stamp.
        for kind in ('job', 'queue', 'event', 'evidence', 'principal', 'analysis_outcome_by_job', 'exploration', 'vote', 'vote_total'):
            self.assertEqual({k: v for k, v in self.all_rows().items() if k[0] == kind}, {k: v for k, v in before.items() if k[0] == kind}, kind)
        self.assertEqual(self.get('publication', digest([self.other, 'x']))['version'], 1)
        self.assertEqual(report['counts'], {'account': 2, 'account_membership': 2, 'analysis_outcome': 1, 'outcome_publication': 1,
                                            'outcome_snapshot': 1, 'outcome_summary': 1, 'publication': 1, 'publication_snapshot': 1, 'request': 4})
        self.assertEqual((report['skipped'], report['anchor_display_backfilled']), ({'publication': 1}, 1))
        self.assertNotIn('explorations_repointed', report)
        # Ledger and workspace notifications.
        with self.repo.read_transaction() as tx:
            ledger = tx.get(reference.ARCHIVE_RUN_KIND, digest(['reveal_reload_rehearsal', LEGACY, KPN]))
            cursors = tx.list('workspace_cursor'); events = tx.list('workspace_event')
        self.assertEqual((ledger['owner'], ledger['data']['counts'], ledger['data']['runs']), ('catalog', report['counts'], 1))
        self.assertEqual({item['id'] for item in ledger['data']['dropped_drafts']}, {self.anchored[0], self.other_draft[0]})
        self.assertEqual(len(ledger['data']['unresolved']), 2); self.assertIsNotNone(ledger['data']['completed_at'])
        self.assertTrue(cursors and any(event['data']['event_type'] == 'scientific_account.updated' for event in events))

    def test_rerun_is_a_no_op(self):
        self.run_cutover(); before = self.all_rows()
        report = archive.archive_prefix(self.repo, LEGACY, KPN, apply=True, at=T2)
        self.assertEqual((report['counts'], report['dropped_drafts'], report['anchor_display_backfilled']), ({}, [], 0))
        self.assertEqual(report['already_archived']['account'], 2)
        ledger = digest(['reveal_reload_rehearsal', LEGACY, KPN])
        changed = {key for key, value in self.all_rows().items() if before.get(key) != value}
        self.assertEqual(changed, {(reference.ARCHIVE_RUN_KIND, ledger)})
        self.assertEqual(self.get(reference.ARCHIVE_RUN_KIND, ledger)['data']['runs'], 2)

    def test_second_reload_appends_history_and_archives_the_first_kpn_generation(self):
        kpn_draft = self.draft(self.owner, [KPN_ANCHOR])
        request, job = self.submit(self.owner, kpn_draft, [KPN_ANCHOR], result={'kind': 'analysis', 'account_ids': ['k'], 'evidence_package_sha256': '6' * 64})
        current_id = self.account(self.owner, job['id'], 'c', [KPN_ANCHOR])
        report = self.run_cutover()
        self.assertEqual(report['current']['account'], 1); self.assertIsNone(self.stamp('account', digest([self.owner, current_id])))
        self.assertIsNotNone(self.get('draft', kpn_draft[0]))
        first = self.stamp('account', digest([self.owner, self.account_id]))
        late_orphan = self.account(self.owner, 'gone', 'd', [KPN_ANCHOR], publish=False)
        self.activate(KPN2, previous=KPN)
        report = archive.archive_prefix(self.repo, KPN, KPN2, apply=True, at=T2)
        second = self.stamp('account', digest([self.owner, self.account_id]))
        self.assertEqual({key: value for key, value in second.items() if key not in ('to_reference_generation', 'history')},
                         {key: value for key, value in first.items() if key not in ('to_reference_generation', 'history')})
        self.assertEqual((second['to_reference_generation'], second['archived_at'], second['from_reference_generation']), (KPN2, T1, LEGACY))
        self.assertEqual(second['history'], first['history'] + [{'from_reference_generation': KPN, 'to_reference_generation': KPN2, 'archived_at': T2}])
        self.assertEqual(self.stamp('publication', digest([self.owner, self.account_id])), reference.public_stamp(second))
        fresh = self.stamp('account', digest([self.owner, current_id]))
        self.assertEqual((fresh['from_reference_generation'], fresh['to_reference_generation'], fresh['reference']['model']), (KPN, KPN2, reference.KPN_MODEL))
        self.assertEqual({key: fresh['reference']['anchors'][0][key] for key in ('source_id', 'kpn_trait_id', 'label', 'archived_reference_factor_id')},
                         {'source_id': KPN_ID, 'kpn_trait_id': 'KPN.TRAIT:0000398', 'label': 'Beta cell stress',
                          'archived_reference_factor_id': reference.archive_id(KPN, KPN_ID)})
        self.assertIsNone(self.get('draft', kpn_draft[0]))
        self.assertEqual(report['restamped']['account'], 2); self.assertEqual(report['counts']['account'], 3)
        # Not a legacy cutover: an unresolvable row is reported, never guessed.
        self.assertIsNone(self.stamp('account', digest([self.owner, late_orphan])))
        self.assertIn(('account', digest([self.owner, late_orphan]), False),
                      {(item['kind'], item['id'], item['archived']) for item in report['unresolved']})

    def test_backfill_anchor_display(self):
        dry = archive.backfill_anchor_display(self.repo, apply=False)
        self.assertEqual((dry['missing'], dry['backfilled']), (1, 1)); self.assertNotIn('anchor_display', self.get('request_binding', self.r1)['data'])
        applied = archive.backfill_anchor_display(self.repo, apply=True)
        self.assertEqual(applied['backfilled'], 1)
        binding = self.get('request_binding', self.r1)['data']
        self.assertEqual(binding['anchor_display'], outcomes.anchor_display(self.anchored[1], dict(binding, anchor_display={}), self.anchored[2]))
        self.assertEqual(archive.backfill_anchor_display(self.repo, apply=True)['missing'], 0)
        # Without the exact saved selection nothing is invented.
        request, _ = self.submit(self.owner, self.draft(self.owner, [LEGACY_ANCHOR]), [LEGACY_ANCHOR], display=False)
        with self.repo.transaction() as tx:
            saved = tx.get('draft_binding', self.get('request', request)['data']['source_draft_id'])
            saved['data']['selections'][LEGACY_ID]['binding']['embedding_run_id'] = 'changed'
            tx.put('draft_binding', self.get('request', request)['data']['source_draft_id'], self.owner, saved['data'])
        report = archive.backfill_anchor_display(self.repo, apply=True)
        self.assertEqual(report['unrecoverable'], [{'id': request, 'owner': self.owner, 'missing': [LEGACY_ID]}])

    def test_cancel_nonterminal_jobs(self):
        self.assertEqual(archive.cancel_nonterminal_jobs(self.repo, LEGACY, apply=False), [self.j3['id']])
        self.assertEqual(self.get('job', self.j3['id'])['data']['status'], 'queued')
        self.assertEqual(archive.cancel_nonterminal_jobs(self.repo, KPN, apply=False), [])
        self.assertEqual(archive.cancel_nonterminal_jobs(self.repo, LEGACY, apply=True), [self.j3['id']])
        self.assertEqual([self.get('job', job['id'])['data']['status'] for job in (self.j3, self.j4, self.j5)], ['cancelled', 'running', 'queued'])
        self.assertEqual(archive.cancel_nonterminal_jobs(self.repo, LEGACY, apply=True), [])

    def test_referenced_sources(self):
        self.draft(self.owner, [KPN_ANCHOR])
        self.assertEqual(archive.referenced_sources(self.repo), {LEGACY: {LEGACY_ID}, KPN: {KPN_ID}})


# --------------------------------------------------------------------------------------
# archived_reference_factors (fake DB-API)


class FakeCursor:
    def __init__(self, connection): self.connection, self.rows = connection, []
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, sql, params=()):
        params = tuple(params); self.connection.log.append((sql, params))
        self.rows = list(self.connection.answer(sql, params))
    def executemany(self, sql, rows):
        self.connection.log.append((sql, 'many')); self.connection.inserted.extend(rows)
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, answer): self.answer, self.log, self.inserted, self.commits, self.rollbacks = answer, [], [], 0, 0
    def cursor(self): return FakeCursor(self)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


class FakeRuntime:
    schema = 'pinned-schema'
    def compute_id(self, node, kind, schema):
        assert schema == self.schema
        return f'dapper:{kind}.' + digest(node)[:32]


LEGACY_GENERATION = {'generation_id': LEGACY, 'kind': reference.LEGACY_KIND, 'model': reference.LEGACY_MODEL, 'status': 'complete',
                     'eaggl_import_id': 'e' * 64, 'legacy_mapping_run_id': MAPPING, 'legacy_gene_set_import_id': 'd' * 64,
                     'manifest': {'format': 'reveal.reference-generation/1', 'legacy_mapping_run_id': MAPPING}}
KPN_GENERATION = {'generation_id': KPN, 'kind': reference.KPN_KIND, 'model': reference.KPN_MODEL, 'status': 'complete',
                  'eaggl_import_id': 'e' * 64, 'manifest': {'format': 'reveal.reference-generation/1', 'factors': 4037}}


def legacy_answer(sql, params):
    if 'FROM eaggl_cfde_factor_links' in sql:
        assert params[0] == MAPPING and 'cfde_node_sha256 IN' in sql
        if digest(LEGACY_ID) in params[1:]:
            yield (LEGACY_ID, 7, 'e' * 64, canonical({'phenotype_key': 'T2D', 'raw': {'label': 'Insulin secretion', 'factor': 'Factor1'}}),
                   'T2D::Factor1', 'T2D', 'Insulin secretion program', canonical({'factor': 'Factor1', 'gene_score': 3.5}))
    elif 'FROM eaggl_cfde_gene_set_links' in sql:
        assert params == (MAPPING, 7) and 'cfde_gene_set_aliases' in sql
        yield (1, 'GO__insulin_secretion', 'gene_set:GO__insulin_secretion', 'dapper:GeneSet.' + 'a' * 32)
        yield (2, 'HALLMARK__glycolysis___KEGG__beta_cell', 'gene_set:x', None)
    elif 'eaggl_gene_loadings' in sql:
        assert params == ('e' * 64, 7) and 'LIMIT 50' in sql and 'ORDER BY l.loading DESC' in sql
        yield ('INS', 0.9); yield ('GCK', 0.5)
    else: raise AssertionError(sql)


def kpn_answer(sql, params):
    if 'FROM reference_factors f JOIN kpn_traits' in sql:
        assert params[0] == KPN
        if KPN_ID in params[1:]:
            yield (KPN_ID, 'KPN.TRAIT:0000398::Factor2', 'T2D::Factor2', 'KPN.TRAIT:0000398', 'Beta cell stress', 'e' * 64,
                   canonical({'kpn': {'phenotype_name': 'Type 2 diabetes'}, 'loading_l2': 1.2}), 'Type 2 diabetes', 'T2D')
    elif 'eaggl_gene_loadings' in sql:
        assert params == ('e' * 64, hashlib.sha256(b'T2D::Factor2').hexdigest(), digest('T2D::Factor2'), 'T2D::Factor2')
        yield ('PDX1', 0.7)
    elif 'FROM factor_gene_set_projections' in sql:
        assert params == (KPN, 'per_trait', 'KPN.TRAIT:0000398::Factor2', 50) and 'JOIN cfde_gene_sets' in sql
        yield (1, 'dapper:GeneSet.' + 'b' * 32, 'Insulin secretion', 'GO_BP', 'dapper:GeneSetCollection.' + 'c' * 32, 'GO:0030073', 0.12, 0.34)
    else: raise AssertionError(sql)


class CaptureTests(unittest.TestCase):
    def test_legacy_snapshot(self):
        runtime = FakeRuntime()
        row, = archive.capture_factors(FakeConnection(legacy_answer), LEGACY_GENERATION, [LEGACY_ID, LEGACY_ID], runtime=runtime)
        node = {'name': 'T2D mechanism Factor1', 'description': f'EAGGL mechanism {LEGACY_ID}. Source label: Insulin secretion.'}
        snapshot = {'format': 'reveal.archived-reference-factor/1', 'generation_id': LEGACY, 'model': 'cfde-inc-v2', 'source_id': LEGACY_ID,
            'factor_id': 'T2D::Factor1', 'trait': 'T2D', 'kpn_trait_id': None, 'label': 'Insulin secretion program',
            'mechanism': {'id': runtime.compute_id(node, 'Mechanism', runtime.schema), **node},
            'metadata': {'factor': 'Factor1', 'gene_score': 3.5},
            'top_genes': [{'symbol': 'INS', 'loading': 0.9}, {'symbol': 'GCK', 'loading': 0.5}],
            'top_gene_sets': [
                {'rank': 1, 'gene_set_id': 'dapper:GeneSet.' + 'a' * 32, 'name': 'GO / insulin secretion', 'library': 'GO', 'collection_id': None,
                 'source_key': 'GO__insulin_secretion', 'joint_loading': None, 'marginal_loading': None, 'score': None},
                {'rank': 2, 'gene_set_id': None, 'name': 'HALLMARK / glycolysis | KEGG / beta cell', 'library': None, 'collection_id': None,
                 'source_key': 'HALLMARK__glycolysis___KEGG__beta_cell', 'joint_loading': None, 'marginal_loading': None, 'score': None}],
            'generation_manifest_sha256': digest(LEGACY_GENERATION['manifest'])}
        self.assertEqual(row, {'archive_id': reference.archive_id(LEGACY, LEGACY_ID), 'generation_id': LEGACY, 'source_id': LEGACY_ID,
            'source_id_sha256': hashlib.sha256(LEGACY_ID.encode()).hexdigest(), 'model': 'cfde-inc-v2', 'factor_id': 'T2D::Factor1',
            'trait': 'T2D', 'kpn_trait_id': None, 'label': 'Insulin secretion program', 'snapshot': snapshot, 'snapshot_sha256': digest(snapshot)})
        self.assertEqual(row['snapshot_sha256'], hashlib.sha256(canonical(snapshot).encode()).hexdigest())

    def test_kpn_snapshot(self):
        runtime = FakeRuntime()
        row, = archive.capture_factors(FakeConnection(kpn_answer), KPN_GENERATION, {KPN_ID}, runtime=runtime)
        node = reference.mechanism_node(KPN_ID, 'Type 2 diabetes', 'KPN.TRAIT:0000398', 'Factor2', 'Beta cell stress')
        snapshot = row['snapshot']
        self.assertEqual(snapshot['mechanism'], {'id': runtime.compute_id(node, 'Mechanism', runtime.schema), **node})
        self.assertEqual({key: snapshot[key] for key in ('model', 'factor_id', 'trait', 'kpn_trait_id', 'label', 'top_genes')},
                         {'model': reference.KPN_MODEL, 'factor_id': 'T2D::Factor2', 'trait': 'T2D', 'kpn_trait_id': 'KPN.TRAIT:0000398',
                          'label': 'Beta cell stress', 'top_genes': [{'symbol': 'PDX1', 'loading': 0.7}]})
        self.assertEqual(snapshot['top_gene_sets'], [{'rank': 1, 'gene_set_id': 'dapper:GeneSet.' + 'b' * 32, 'name': 'Insulin secretion',
            'library': 'GO_BP', 'collection_id': 'dapper:GeneSetCollection.' + 'c' * 32, 'source_key': 'GO:0030073',
            'joint_loading': 0.12, 'marginal_loading': 0.34, 'score': None}])
        self.assertEqual(snapshot['metadata']['kpn']['phenotype_name'], 'Type 2 diabetes')
        self.assertEqual((row['archive_id'], row['kpn_trait_id']), (reference.archive_id(KPN, KPN_ID), 'KPN.TRAIT:0000398'))

    def test_missing_factors_are_skipped_or_fail_closed_when_strict(self):
        wanted = [LEGACY_ID, 'factor:portal:BMI:cfde-inc-v2:Factor9']
        rows = archive.capture_factors(FakeConnection(legacy_answer), LEGACY_GENERATION, wanted, runtime=FakeRuntime())
        self.assertEqual([row['source_id'] for row in rows], [LEGACY_ID])
        with self.assertRaisesRegex(reference.ReferenceError, 'absent'):
            archive.capture_factors(FakeConnection(legacy_answer), LEGACY_GENERATION, wanted, runtime=FakeRuntime(), strict=True)
        with patch.object(archive, '_default_runtime', return_value=None):
            row, = archive.capture_factors(FakeConnection(legacy_answer), LEGACY_GENERATION, [LEGACY_ID])
        self.assertIsNone(row['snapshot']['mechanism']['id']); self.assertEqual(row['snapshot_sha256'], digest(row['snapshot']))
        self.assertEqual(archive.capture_factors(FakeConnection(legacy_answer), LEGACY_GENERATION, [], runtime=FakeRuntime()), [])
        with self.assertRaises(reference.ReferenceError):
            archive.capture_factors(FakeConnection(legacy_answer), dict(LEGACY_GENERATION, kind='other'), [LEGACY_ID], runtime=FakeRuntime())

    def test_write_is_insert_if_absent_and_never_overwrites(self):
        row, = archive.capture_factors(FakeConnection(legacy_answer), LEGACY_GENERATION, [LEGACY_ID], runtime=FakeRuntime())
        stored = {}

        def answer(sql, params):
            assert sql.startswith('SELECT archive_id,snapshot_sha256 FROM archived_reference_factors')
            return [(identity, stored[identity]) for identity in params if identity in stored]
        connection = FakeConnection(answer)
        self.assertEqual(archive.write_archived_factors(connection, [row, row]), 1)
        inserted, = connection.inserted
        self.assertEqual(dict(zip(archive.ARCHIVED_COLUMNS, inserted)), {**row, 'snapshot': canonical(row['snapshot'])})
        self.assertIn('INSERT INTO archived_reference_factors (archive_id,generation_id,source_id,source_id_sha256,model,factor_id,trait,'
                      'kpn_trait_id,label,snapshot,snapshot_sha256)', connection.log[-1][0])
        self.assertEqual(connection.commits, 1)
        stored[row['archive_id']] = row['snapshot_sha256']
        again = FakeConnection(answer)
        self.assertEqual(archive.write_archived_factors(again, [row]), 0); self.assertEqual(again.inserted, [])
        stored[row['archive_id']] = '0' * 64
        conflict = FakeConnection(answer)
        with self.assertRaisesRegex(reference.ReferenceError, 'differs'): archive.write_archived_factors(conflict, [row])
        self.assertEqual((conflict.inserted, conflict.rollbacks, conflict.commits), ([], 1, 0))
        tampered = dict(row, snapshot=dict(row['snapshot'], label='edited'))
        with self.assertRaisesRegex(reference.ReferenceError, 'does not match'): archive.write_archived_factors(FakeConnection(answer), [tampered])
        self.assertEqual(archive.write_archived_factors(FakeConnection(answer), []), 0)


if __name__ == '__main__': unittest.main()
