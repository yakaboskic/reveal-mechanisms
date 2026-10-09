"""Audit continuation uses the existing research lifecycle, without paid execution."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient

from reveal_backend import app as api, reference_archive, reference_generation as rg, research_hosted
from reveal_backend.auth import Problem
from reveal_backend.lightning_continuation import continue_audit
from reveal_backend.repository import Repository, digest, now, uid
from reveal_backend.research_work import ResearchWorkService
import test_research_data as fixtures


class LightningContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Repository(str(Path(self.temp.name) / 'audit.sqlite')); self.repo.migrate()
        env = patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 40, 'REVEAL_LIGHTNING_ENABLED': 'true',
            'REVEAL_JOB_TRANSPORT': 'database', 'REVEAL_JOB_RUNNER': 'legacy',
            'REVEAL_ARTIFACT_STORE': 'filesystem', 'REVEAL_ARTIFACTS_DIR': self.temp.name,
            'REVEAL_NOTIFICATION_REDIS_URL': '', 'REVEAL_NOTIFICATION_REDIS_REST_URL': '',
            'UPSTASH_REDIS_REST_URL': '', 'UPSTASH_REDIS_REST_TOKEN': ''})
        env.start(); self.addCleanup(env.stop)
        self.owner = 'alice'; self.other = 'bob'; self.audit_id = uid()
        self.dapper, self.frozen, self.binding = fixtures.SeedTests().inputs()
        self.frozen.update(owner_user_id=self.owner, source_draft_id='deleted-draft', source_draft_version=2,
            submitted_at=now(), user_inputs={'format': 'reveal.user-inputs/1', 'research_direction': 'Original direction',
                'context': 'Original context', 'hypotheses': '', 'uploads': []}, attribution={'user_id': self.owner})
        self.audit = {'control_owner': self.owner, 'attempt_id': 'attempt-1', 'deadline_at': '2999-01-01T00:00:00Z',
            'frozen': deepcopy(self.frozen), 'binding': deepcopy(self.binding),
            'source_state': {'exact': [1, 2, 3]}, 'model_payload': {'messages': [{'role': 'user', 'content': 'Evidence'}]},
            'response': {'assessment': 'partial', 'summary': 'Preliminary direction'},
            'public': {'id': self.audit_id, 'kind': 'lightning_audit', 'status': 'succeeded',
                'research_request_id': self.frozen['id'], 'created_at': now(), 'updated_at': now(),
                'continuation_expires_at': '2999-01-01T00:00:00Z', 'reference_generation_id': fixtures.GEN,
                'continuations': []}}
        with self.repo.transaction() as tx:
            for owner in (self.owner, self.other):
                tx.put('principal', owner, owner, {'retired': False, 'me': {'user_id': owner,
                    'principal_kind': 'registered', 'workspace_expires_at': None}})
            tx.put('request', self.frozen['id'], self.owner, self.frozen)
            tx.put('request_binding', self.frozen['id'], self.owner, self.binding)
            tx.put('research_pin', self.frozen['id'], self.owner, {'id': self.frozen['id'], 'generation_id': fixtures.GEN,
                'state': 'active', 'expires_at': '2999-01-01T00:00:00Z'})
            tx.put('lightning_audit', self.audit_id, self.owner, self.audit)

    def auth(self, owner=None):
        return 'Bearer ' + jwt.encode({'sub': owner or self.owner, 'principal_kind': 'registered',
            'iss': 'reveal-nextjs', 'aud': 'reveal-api', 'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid()},
            's' * 40, algorithm='HS256')

    @staticmethod
    def gate(tx):
        if (rg.read_gate(tx) or {}).get('closed'):
            raise Problem(503, 'REFERENCE_RELOAD_IN_PROGRESS', 'Reference writes paused')

    def create(self, mode='local', key=None, direction='Investigate the scoped relationship', owner=None):
        return continue_audit(self.repo, self.auth(owner), self.audit_id,
            {'mode': mode, 'research_direction': direction}, key or uid(), reload_gate=self.gate,
            check_job_quota=api.check_job_quota, analysis_job_rows=api.analysis_job_rows)

    def row(self, kind, identity):
        with self.repo.read_transaction() as tx: return tx.get(kind, identity)

    def test_superseded_frozen_selection_needs_no_draft_or_active_catalog(self):
        with self.repo.transaction() as tx:
            tx.put(rg.ACTIVE_KIND, rg.ACTIVE_ID, 'catalog', {'generation_id': fixtures.OTHER})
            original = deepcopy(self.frozen); original['archive'] = {'status': 'archived'}
            tx.put('request', original['id'], self.owner, original)
        result = self.create()
        child = self.row('request', result['research_request_id'])['data']
        self.assertNotEqual(child['id'], self.frozen['id'])
        self.assertEqual(child['user_inputs'], self.frozen['user_inputs'])
        self.assertEqual(child['composer'], self.frozen['composer'])
        self.assertEqual(child['lightning_audit_id'], self.audit_id)
        self.assertNotIn('archive', child)
        self.assertEqual(self.row('request_binding', child['id'])['data'], self.binding)
        self.assertEqual(self.row('research_pin', child['id'])['data']['generation_id'], fixtures.GEN)
        self.assertEqual(self.row('request', self.frozen['id'])['data']['archive']['status'], 'archived')

    def test_delivery_replay_and_changed_body_have_one_child(self):
        first = self.create(key='same-key')
        self.assertEqual(self.create(key='same-key'), first)
        with self.assertRaises(Problem) as caught: self.create(key='same-key', direction='Changed direction')
        self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.row('lightning_audit', self.audit_id)['data']['public']['continuations'], [first])

    def test_children_have_independent_pins_and_online_dispatch_intent(self):
        local = self.create(); online = self.create('online')
        with self.repo.transaction() as tx:
            work = tx.get('local_work', local['id'])['data']
            work['state'] = 'closed'
            operation = tx.get('research_operation', work['preparation_operation_id'])['data']
            operation['state'] = 'succeeded'
            tx.put('research_operation', operation['id'], self.owner, operation)
            tx.put('local_work', work['id'], self.owner, work)
            ResearchWorkService.release_pin_if_idle(tx, work)
        self.assertEqual(self.row('research_pin', local['research_request_id'])['data']['state'], 'released')
        for request in (self.frozen['id'], online['research_request_id']):
            self.assertEqual(self.row('research_pin', request)['data']['state'], 'active')
        self.assertEqual(self.row('job', online['id'])['data']['research_request_id'], online['research_request_id'])
        self.assertIsNotNone(self.row('queue', online['id']))

    def test_online_quota_and_reload_gate_leave_no_partial_children(self):
        self.create('online'); self.create('online')
        with self.assertRaises(Problem) as caught: self.create('online')
        self.assertEqual(caught.exception.code, 'JOB_QUOTA_EXCEEDED')
        with self.repo.transaction() as tx: rg.set_gate(tx, True, reason='reference_purge')
        with self.assertRaises(Problem) as caught: self.create()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(self.row('lightning_audit', self.audit_id)['data']['public']['continuations']), 2)

    def test_closed_pin_and_expired_window_refuse_new_continuation(self):
        with self.repo.transaction() as tx:
            pin = tx.get('research_pin', self.frozen['id'])['data']; pin['state'] = 'released'
            tx.put('research_pin', self.frozen['id'], self.owner, pin)
        with self.assertRaises(Problem) as caught: self.create()
        self.assertEqual(caught.exception.code, 'SOURCE_UNAVAILABLE')
        with self.repo.transaction() as tx:
            audit = tx.get('lightning_audit', self.audit_id)['data']
            audit['public']['continuation_expires_at'] = '2000-01-01T00:00:00Z'
            tx.put('lightning_audit', self.audit_id, self.owner, audit)
        with self.assertRaises(Problem) as caught: self.create()
        self.assertEqual(caught.exception.code, 'AUDIT_CONTINUATION_EXPIRED')

    def test_owner_transfer_keeps_completed_context_but_fences_pending_audit(self):
        self.create(key='owned-key')
        pending = deepcopy(self.audit); pending['public']['id'] = 'pending'; pending['public']['status'] = 'assessing'
        with self.repo.transaction() as tx:
            tx.put('lightning_audit', 'pending', self.owner, pending)
            tx.transfer(self.owner, self.other)
        with self.assertRaises(Problem) as caught: self.create()
        self.assertEqual(caught.exception.status, 404)
        completed = self.row('lightning_audit', self.audit_id)
        self.assertEqual(completed['owner'], self.other)
        self.assertEqual(completed['data']['frozen']['attribution']['user_id'], self.owner)
        self.assertEqual(completed['data']['control_owner'], self.other)
        self.assertEqual(self.row('lightning_audit', 'pending')['data']['public']['status'], 'interrupted')
        result = self.create(owner=self.other)
        self.assertEqual(self.row('request', result['research_request_id'])['owner'], self.other)
        self.assertEqual(self.row('request', result['research_request_id'])['data']['attribution']['user_id'], self.other)

    def test_audit_artifacts_are_exact_shared_seed_context_never_scientific_evidence(self):
        local = self.create(); online = self.create('online')
        self.assertNotIn('artifacts', self.row('request', local['research_request_id'])['data']['lightning_context'])
        service = ResearchWorkService(self.repo)
        local_result = service.prepare({'owner_user_id': self.owner, 'local_work_id': local['id'],
            'research_request_id': local['research_request_id']})
        hosted_path, hosted = research_hosted.collect(self.repo, self.row('job', online['id'])['data'], Path(self.temp.name) / 'hosted')
        self.assertTrue(hosted_path.is_file())
        for package in (local_result['package'], hosted):
            self.assertEqual(package['user_inputs'], self.frozen['user_inputs'])
            self.assertEqual(package['cfde_source_ids'], [])
            for name in ('source_state', 'model_payload', 'response'):
                source = package['source_artifacts']['lightning-' + name]
                self.assertTrue(source['private'])
                self.assertEqual(source['sha256'], digest(self.audit[name]))
                self.assertNotIn(source['dapper_file_id'], package['eligible_source_ids'])
        self.assertEqual(local_result['package']['lightning_audit'], hosted['lightning_audit'])

    def test_changed_retained_audit_bytes_cannot_be_used_by_a_child(self):
        child = self.create()
        with self.repo.transaction() as tx:
            audit = tx.get('lightning_audit', self.audit_id)['data']
            audit['response']['summary'] = 'Mutated after continuation'
            tx.put('lightning_audit', self.audit_id, self.owner, audit)
        with self.assertRaises(Problem) as caught:
            ResearchWorkService(self.repo).prepare({'owner_user_id': self.owner, 'local_work_id': child['id'],
                'research_request_id': child['research_request_id']})
        self.assertEqual(caught.exception.code, 'AUDIT_CONTEXT_UNAVAILABLE')

    def test_hosted_audit_guidance_is_bound_to_the_retained_seed_on_replay(self):
        from reveal_backend.agent_execution import ExecutionRequest
        from reveal_backend.box_adapter import make_bundle
        from reveal_backend.dispatch_view import (pinned_claim_structure_sha256, pinned_contract_sha256,
            pinned_skeleton_sha256, research_prompt, validate_file_input, FILE_INPUT_FILENAME)
        from reveal_backend.evidence_budget import fit_input_budget
        from reveal_backend.evidence_package import EvidenceBuildError, decode
        from reveal_backend.runtime_config import ROOT
        child = self.create('online')
        with patch.dict(os.environ, {'REVEAL_PUBLIC_API_URL': 'https://reveal.example.test'}):
            path, package = research_hosted.collect(self.repo, self.row('job', child['id'])['data'],
                Path(self.temp.name) / 'hosted')
        prepared = fit_input_budget(path, 'box', 24000)
        self.assertEqual(fit_input_budget(path, 'box', 24000), prepared)
        metadata = decode((path.parent.parent / FILE_INPUT_FILENAME).read_bytes())
        arguments = {'progressive': True, 'contract_sha256': pinned_contract_sha256(package),
            'skeleton_sha256': pinned_skeleton_sha256(package),
            'claim_structure_sha256': pinned_claim_structure_sha256(package)}
        plain = research_prompt([], **arguments)
        self.assertNotIn('lightning_audit', plain)
        with self.assertRaises(EvidenceBuildError): validate_file_input(path.read_bytes(), metadata, plain)
        audit_prompt = research_prompt([], **arguments, lightning_audit=True)
        self.assertIn('not eligible scientific evidence', audit_prompt)
        validate_file_input(path.read_bytes(), metadata, audit_prompt)
        request = ExecutionRequest(child['id'], 1, 'research', path, Path(self.temp.name) / 'output')
        self.assertTrue(make_bundle(ROOT, request))

    def test_child_can_prepare_after_parent_window_closes_and_workspace_is_claimed(self):
        child = self.create()
        with self.repo.transaction() as tx:
            audit = tx.get('lightning_audit', self.audit_id)['data']
            audit['public']['continuation_expires_at'] = '2000-01-01T00:00:00Z'
            audit['pin_released'] = True
            tx.put('lightning_audit', self.audit_id, self.owner, audit)
            pin = tx.get('research_pin', self.frozen['id'])['data']; pin['state'] = 'released'
            tx.put('research_pin', self.frozen['id'], self.owner, pin)
            tx.transfer(self.owner, self.other)
        service = ResearchWorkService(self.repo)
        with self.assertRaises(Problem) as caught:
            service.prepare({'owner_user_id': self.owner, 'local_work_id': child['id'],
                'research_request_id': child['research_request_id']})
        self.assertEqual(caught.exception.status, 404)
        ready = service.prepare({'owner_user_id': self.other, 'local_work_id': child['id'],
            'research_request_id': child['research_request_id']})
        self.assertEqual(ready['package']['lightning_audit']['audit_id'], self.audit_id)
        self.assertEqual(ready['package']['source_artifacts']['lightning-response']['sha256'], digest(self.audit['response']))
        self.assertEqual(self.row('research_pin', child['research_request_id'])['data']['state'], 'active')

    def test_audit_changes_emit_only_the_audits_collection(self):
        with self.repo.read_transaction() as tx:
            changes = [r['data'] for r in tx.list('workspace_event', self.owner) if r['data'].get('entity_id') == self.audit_id]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['collections'], ['audits'])
        self.assertEqual(changes[0]['event_type'], 'lightning_audit.updated')
        self.assertTrue(all(reference_archive.classify(kind) == reference_archive.KEEP
            for kind in ('lightning_audit', 'lightning_audit_idempotency', 'lightning_continuation')))

    def test_disabled_feature_blocks_new_children_but_preserves_records(self):
        previous = self.create(key='prior-child')
        with patch.dict(os.environ, {'REVEAL_LIGHTNING_ENABLED': 'false'}), self.assertRaises(Problem) as caught:
            self.create()
        self.assertEqual(caught.exception.status, 503)
        with patch.dict(os.environ, {'REVEAL_LIGHTNING_ENABLED': 'false'}):
            self.assertEqual(self.create(key='prior-child'), previous)
        self.assertEqual(self.row('lightning_audit', self.audit_id)['data']['public']['continuations'], [previous])

    def test_http_routes_keep_history_private_and_continue_after_response(self):
        from reveal_backend import lightning_audits
        headers = {'Authorization': self.auth(), 'Idempotency-Key': uid()}
        with patch.object(api, 'repo', self.repo), patch.object(ResearchWorkService, 'kick') as kick:
            client = TestClient(api.app)
            listing = client.get('/v1/lightning-audits?limit=1', headers=headers)
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertEqual(listing.json()['items'][0]['id'], self.audit_id)
            self.assertTrue(listing.json()['page']['snapshot_id'])
            self.assertEqual(listing.headers['cache-control'], 'private, no-store')
            self.assertIn('Authorization', listing.headers['vary'])
            with patch.dict(os.environ, {'REVEAL_LIGHTNING_ENABLED': 'false'}):
                saved = client.get('/v1/lightning-audits/' + self.audit_id, headers=headers)
                self.assertEqual(saved.status_code, 200, saved.text)
            inaccessible = client.get('/v1/lightning-audits/' + self.audit_id, headers={'Authorization': self.auth(self.other)})
            self.assertEqual(inaccessible.status_code, 404)
            invalid = client.get('/v1/lightning-audits/' + self.audit_id + '?wait=21', headers=headers)
            self.assertEqual(invalid.status_code, 422)
            continued = client.post('/v1/lightning-audits/' + self.audit_id + '/continue', headers=headers,
                json={'mode': 'local', 'research_direction': 'Check the original observation'})
            self.assertEqual(continued.status_code, 202, continued.text)
            self.assertEqual(continued.headers['location'], '/v1/local-work/' + continued.json()['id'])
            self.assertEqual(continued.headers['retry-after'], '2')
            kick.assert_called_once_with(continued.json()['id'])
            with patch.object(lightning_audits, 'start', return_value=self.audit['public']):
                created = client.post('/v1/lightning-audits', headers=headers, json={'draft_id': 'draft', 'draft_version': 1})
            self.assertEqual(created.status_code, 202, created.text)
            self.assertEqual(created.headers['location'], '/v1/lightning-audits/' + self.audit_id)
            self.assertEqual(created.headers['retry-after'], '2')


if __name__ == '__main__': unittest.main()
