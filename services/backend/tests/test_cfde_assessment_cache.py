"""Public-only cache identity, isolation and version fencing; no provider calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import cfde_assessment_cache as cache
from reveal_backend.repository import Repository, digest, uid

GENERATION = 'a' * 64
STAMP = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def iso(stamp): return stamp.isoformat().replace('+00:00', 'Z')


def composer():
    return {'source_gap': {'id': 'dapper:KnowledgeGap.' + 'b' * 32, 'source_id': 'dismech:gap', 'source_revision': 'c' * 64},
        'eaggl_anchors': [{'reference': {'source': 'eaggl', 'source_id': 'factor:kpn:0000398:eaggl-capped-v1:Factor1',
            'source_revision': 'd' * 64, 'dapper_id': 'dapper:Mechanism.' + 'e' * 32},
            'origin': 'automatic', 'suggestion_id': uid()}], 'model': 'eaggl-capped-v1', 'selected_kgs': ['prokn'],
        'research_direction': '', 'context': '', 'hypotheses': '', 'upload_ids': [],
        'mechanism_subquery': 'insulin', 'dismissed_source_ids': ['unselected']}


def key(value=None, generation=GENERATION, **kwargs):
    return cache.shared_key(composer() if value is None else value, generation,
                            model=kwargs.get('model', 'jev-1.13.0'), rubric=kwargs.get('rubric', 'cfde-support-v1'))


def public(status='preparing', identity=None, updated=STAMP):
    return {'id': identity or uid(), 'draft_id': uid(), 'draft_version': 1, 'owner_user_id': 'private-owner',
        'model': 'jev-1.13.0', 'rubric_version': 'cfde-support-v1', 'created_at': iso(STAMP),
        'status': status, 'updated_at': iso(updated), 'expires_at': iso(STAMP + timedelta(minutes=2)),
        'reference_generation_id': GENERATION,
        'result': {'verdict': 'yes', 'probability_yes': .56, 'probability_no': .43, 'confidence': .5,
            'main_blocker': 'weak_relevance', 'relationship_support': {'gene_gene_set': .2,
                'gene_mechanism': .4, 'gene_set_mechanism': .6}, 'calibration': 'not_calibrated'} if status == 'succeeded' else None,
        'coverage': {'factor_count': 1, 'gene_loading_count': 50, 'gene_set_loading_count': 50,
            'unique_gene_set_count': 50, 'missing': [], 'truncations': [], 'complete': True} if status == 'succeeded' else None,
        'error': {'code': 'CFDE_ASSESSMENT_FAILED', 'detail': 'A bounded failure.', 'retryable': True}
            if status in ('failed', 'interrupted') else None}


class SharedKeyTests(unittest.TestCase):
    def test_empty_or_whitespace_inputs_share_but_private_inputs_do_not(self):
        value = composer(); expected = key(value)
        self.assertRegex(expected, r'^[a-f0-9]{64}$')
        for field in ('research_direction', 'context', 'hypotheses'):
            candidate = deepcopy(value); candidate[field] = ' \t\n'
            self.assertEqual(key(candidate), expected)
            for text in ('private note', None, 42, {'secret': 'value'}):
                candidate[field] = text
                self.assertIsNone(key(candidate))
        for uploads in ([uid()], None, {}, ''):
            candidate = deepcopy(value); candidate['upload_ids'] = uploads
            self.assertIsNone(key(candidate))

    def test_generation_and_scientific_revisions_invalidate(self):
        value = composer(); expected = key(value)
        self.assertNotEqual(key(value, 'f' * 64), expected)
        for generation in (None, '', 'unknown', 'A' * 64): self.assertIsNone(key(value, generation))
        changes = [('source_gap', 'source_revision', 'f' * 64), ('source_gap', 'source_id', 'dismech:different'),
                   ('source_gap', 'id', 'dapper:KnowledgeGap.' + 'f' * 32)]
        for group, field, replacement in changes:
            candidate = deepcopy(value); candidate[group][field] = replacement
            self.assertNotEqual(key(candidate), expected)
        for field, replacement in [('source_revision', 'f' * 64), ('source_id', 'factor:different'),
                                   ('dapper_id', 'dapper:Mechanism.' + 'f' * 32)]:
            candidate = deepcopy(value); candidate['eaggl_anchors'][0]['reference'][field] = replacement
            self.assertNotEqual(key(candidate), expected)
        self.assertNotEqual(key(value, model='jev-next'), expected)
        self.assertNotEqual(key(value, rubric='next-rubric'), expected)
        candidate = deepcopy(value); candidate['model'] = 'cfde-inc-v2'
        self.assertNotEqual(key(candidate), expected)
        candidate = deepcopy(value); candidate['selected_kgs'] = []
        self.assertNotEqual(key(candidate), expected)

    def test_selection_metadata_and_search_text_do_not_change_model_identity(self):
        value = composer(); expected = key(value)
        value['eaggl_anchors'][0].update(origin='manual', suggestion_id=uid())
        value['mechanism_subquery'] = 'different search'; value['dismissed_source_ids'] = ['different']
        self.assertEqual(key(value), expected)

    def test_anchor_order_remains_part_of_identity(self):
        value = composer(); second = deepcopy(value['eaggl_anchors'][0])
        second['reference']['source_id'] += '2'; second['reference']['dapper_id'] = 'dapper:Mechanism.' + 'f' * 32
        value['eaggl_anchors'].append(second); expected = key(value)
        value['eaggl_anchors'].reverse()
        self.assertNotEqual(key(value), expected)

    def test_untyped_or_poisoned_scientific_references_are_not_eligible(self):
        value = composer()
        for group in ('source_gap', 'reference'):
            candidate = deepcopy(value)
            target = candidate['source_gap'] if group == 'source_gap' else candidate['eaggl_anchors'][0]['reference']
            target['owner_context'] = 'private material'
            self.assertIsNone(key(candidate))
        candidate = deepcopy(value); candidate['eaggl_anchors'].append(deepcopy(candidate['eaggl_anchors'][0]))
        self.assertIsNone(key(candidate))
        candidate = deepcopy(value); candidate['eaggl_anchors'][0]['reference']['source_revision'] = 'unpinned'
        self.assertIsNone(key(candidate))
        candidate = deepcopy(value); candidate['selected_kgs'] = ['unknown']
        self.assertIsNone(key(candidate))


class SharedRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'cache.sqlite'); self.repo.migrate()
        self.clock = patch.object(cache, 'now', return_value=iso(STAMP)); self.clock.start(); self.addCleanup(self.clock.stop)
        self.key = key()

    def create(self, value):
        with self.repo.transaction() as tx: return cache.create_shared(tx, self.key, value)

    def read(self):
        with self.repo.read_transaction() as tx: return cache.read_shared(tx, self.key)

    def projection(self, reference):
        with self.repo.read_transaction() as tx: return cache.projection_fields(tx, {'shared_ref': reference})

    def test_stored_projection_has_no_owner_draft_request_or_private_fields(self):
        value = public('succeeded'); value['request'] = {'private_prompt': 'secret'}
        value['result']['provider_headers'] = {'Authorization': 'secret'}
        value['result']['relationship_support']['owner_user_id'] = 'private-owner'
        value['coverage']['private_context'] = 'secret'
        value['coverage']['truncations'] = [{'source': 'public-source', 'included_chars': 2, 'total_chars': 3,
                                            'storage': '/private/path'}]
        ref = self.create(value)
        result = self.read()
        self.assertEqual(result['leader'], value['id'])
        self.assertEqual(set(result['public']), set(cache.PUBLIC_FIELDS))
        self.assertNotIn('secret', str(result)); self.assertNotIn('private-owner', str(result)); self.assertNotIn('/private/path', str(result))
        with self.repo.read_transaction() as tx:
            row = tx.get(cache.KIND, digest([self.key, value['id']]))
            self.assertEqual(row['owner'], cache.OWNER)
            self.assertEqual(set(row['data']), {'key', 'leader', 'public'})
        self.assertEqual(self.projection(ref), result['public'])

    def test_pending_expiry_is_a_miss_but_historical_projection_remains(self):
        value = public(); ref = self.create(value)
        self.assertEqual(self.read()['public']['status'], 'preparing')
        with patch.object(cache, 'now', return_value=value['expires_at']):
            self.assertIsNone(self.read())
            self.assertEqual(self.projection(ref)['status'], 'preparing')

    def test_success_reuses_only_for_seven_days(self):
        value = public('succeeded', updated=STAMP - timedelta(days=7) + timedelta(seconds=1))
        ref = self.create(value); self.assertIsNotNone(self.read())
        with patch.object(cache, 'now', return_value=iso(STAMP + timedelta(seconds=1))): self.assertIsNone(self.read())
        self.assertEqual(self.projection(ref)['result']['probability_yes'], .56)

    def test_future_updated_at_does_not_extend_cache_lifetime(self):
        self.create(public('succeeded', updated=STAMP + timedelta(days=7)))
        self.assertIsNone(self.read())

    def test_failed_and_interrupted_results_are_never_reused(self):
        for status in ('failed', 'interrupted'):
            ref = self.create(public(status)); self.assertIsNone(self.read())
            self.assertEqual(self.projection(ref)['status'], status)

    def test_leader_fencing_keeps_followers_and_newer_index_is_immutable_to_old_worker(self):
        first = public(); first_ref = self.create(first)
        second = public(); second_ref = self.create(second)
        completed = public('succeeded', identity=first['id'])
        with self.repo.transaction() as tx:
            self.assertTrue(cache.publish_shared(tx, {'shared_ref': first_ref, 'public': completed}, first['id']))
            self.assertFalse(cache.publish_shared(tx, {'shared_ref': first_ref, 'public': completed}, second['id']))
        self.assertEqual(self.read()['leader'], second['id'])
        self.assertEqual(self.projection(first_ref)['status'], 'succeeded')
        self.assertEqual(self.projection(second_ref)['status'], 'preparing')
        # A delayed repeat create cannot put the first leader back in the index.
        self.assertEqual(self.create(first), first_ref)
        self.assertEqual(self.read()['leader'], second['id'])
        self.assertEqual(self.projection(first_ref)['status'], 'succeeded')

    def test_owner_or_version_metadata_poisoning_is_a_miss(self):
        value = public('succeeded'); ref = self.create(value); identity = digest([self.key, value['id']])
        with self.repo.transaction() as tx:
            row = tx.get(cache.KIND, identity)
            tx.put(cache.KIND, identity, 'a-user', row['data'])
        self.assertIsNone(self.read()); self.assertEqual(self.projection(ref), {})
        with self.repo.transaction() as tx:
            row['data']['key'] = 'f' * 64
            tx.put(cache.KIND, identity, cache.OWNER, row['data'])
        self.assertIsNone(self.read()); self.assertEqual(self.projection(ref), {})

    def test_index_owner_cannot_redirect_reuse(self):
        value = public('succeeded'); self.create(value)
        with self.repo.transaction() as tx: tx.put(cache.INDEX_KIND, self.key, 'a-user', {'leader': value['id']})
        self.assertIsNone(self.read())

    def test_invalid_nested_payloads_fail_closed(self):
        value = public('succeeded'); ref = self.create(value); identity = digest([self.key, value['id']])
        with self.repo.transaction() as tx:
            row = tx.get(cache.KIND, identity)
            row['data']['public']['result']['probability_yes'] = 'user-provided text'
            tx.put(cache.KIND, identity, cache.OWNER, row['data'])
        self.assertIsNone(self.read()); self.assertEqual(self.projection(ref), {})

    def test_cross_generation_publication_rejected(self):
        value = public(); ref = self.create(value)
        changed = public('succeeded', identity=value['id']); changed['reference_generation_id'] = 'f' * 64
        with self.repo.transaction() as tx:
            self.assertFalse(cache.publish_shared(tx, {'shared_ref': ref, 'public': changed}, value['id']))
        self.assertEqual(self.read()['public']['status'], 'preparing')

    def test_read_result_is_a_copy_and_bad_refs_do_not_read_arbitrary_rows(self):
        value = public('succeeded'); self.create(value)
        result = self.read(); result['public']['result']['probability_yes'] = 0
        self.assertEqual(self.read()['public']['result']['probability_yes'], .56)
        for reference in ({}, {'key': self.key, 'leader': 'not-a-uuid'}, {'key': None, 'leader': value['id']}):
            self.assertEqual(self.projection(reference), {})


if __name__ == '__main__': unittest.main()
