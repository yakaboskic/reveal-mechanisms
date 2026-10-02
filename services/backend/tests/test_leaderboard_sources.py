"""Evidence credit is narrower than graph reachability or dataset membership."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from reveal_backend.leaderboard_sources import collect


def document():
    return {
        'scientific_accounts': [{'id': 'account', 'component_claims': ['claim']}],
        'claims': [{'id': 'claim', 'proposition': 'proposition', 'direction': 'DISPUTES', 'has_evidence': ['evidence']}],
        'propositions': [{'id': 'proposition', 'statement': 'A test proposition.'}],
        'evidence_items': [{'id': 'evidence', 'target_proposition': 'proposition', 'direction': 'SUPPORTS', 'was_derived_from': ['file']}],
        'datasets': [{'id': 'dataset', 'name': 'Recorded dataset', 'has_file': ['file', 'unused-file']}],
        'files': [{'id': 'file'}, {'id': 'unused-file'}],
    }


class LeaderboardSourcesTests(unittest.TestCase):
    def test_explicit_evidence_direction_and_reached_file_not_all_members(self):
        source = document(); saved = deepcopy(source)
        self.assertEqual(collect(source, 'account'), {'uses': [{
            'dataset_id': 'dataset', 'dataset_label': 'Recorded dataset', 'claim_id': 'claim',
            'evidence_id': 'evidence', 'direction': 'SUPPORTS', 'file_ids': ['file']}], 'excluded_paths': 0})
        self.assertEqual(source, saved)

    def test_dataset_only_path_does_not_count_any_of_its_files(self):
        source = document(); source['evidence_items'][0]['was_derived_from'] = ['dataset']
        self.assertEqual(collect(source, 'account')['uses'][0]['file_ids'], [])

    def test_all_explicit_directions_preserved_and_duplicate_paths_deduplicated(self):
        source = document(); source['scientific_accounts'][0]['component_claims'] *= 2
        source['claims'][0]['has_evidence'] = []
        source['evidence_items'] = []
        for direction in ('SUPPORTS', 'DISPUTES', 'MIXED', 'NEUTRAL', 'UNKNOWN'):
            source['claims'][0]['has_evidence'].extend([direction, direction])
            source['evidence_items'].append({'id': direction, 'target_proposition': 'proposition',
                'direction': direction, 'was_derived_from': ['file', 'dataset', 'file']})
        uses = collect(source, 'account')['uses']
        self.assertEqual(len(uses), 5)
        self.assertEqual({item['direction'] for item in uses}, {'SUPPORTS', 'DISPUTES', 'MIXED', 'NEUTRAL', 'UNKNOWN'})
        self.assertTrue(all(item['file_ids'] == ['file'] for item in uses))

    def test_evidence_must_be_attached_target_exact_loaded_proposition_and_have_direction(self):
        for change in ({'target_proposition': 'other'}, {'target_proposition': None},
                       {'direction': None}, {'direction': 'supports'}, {'direction': ['SUPPORTS']}):
            source = document(); source['evidence_items'][0].update(change)
            with self.subTest(change=change):
                self.assertEqual(collect(source, 'account')['uses'], [])
                self.assertEqual(collect(source, 'account')['excluded_paths'], 1)
        source = document(); source['claims'][0]['has_evidence'] = []
        self.assertEqual(collect(source, 'account')['uses'], [])
        source = document(); source['propositions'] = []
        self.assertEqual(collect(source, 'account')['uses'], [])

    def test_edge_forms_including_c2m2_files_and_declared_predicate_defaults(self):
        source = document(); source['claims'][0].pop('has_evidence')
        source['evidence_items'][0].pop('was_derived_from'); source['datasets'][0].pop('has_file')
        source['files'] = []; source['c2m2_files'] = [{'id': 'file'}]
        source['has_evidence_edges'] = [{'subject': 'claim', 'object': 'evidence'}]
        source['was_derived_from_edges'] = [{'subject': 'evidence', 'predicate': 'http://www.w3.org/ns/prov#wasDerivedFrom', 'object': 'file'}]
        source['has_file_edges'] = [{'subject': 'dataset', 'predicate': 'https://broadinstitute.github.io/dapper/ns#hasFile', 'object': 'file'}]
        self.assertEqual(collect(source, 'account')['uses'][0]['file_ids'], ['file'])
        source['has_evidence_edges'][0]['predicate'] = 'prov:used'
        self.assertEqual(collect(source, 'account')['uses'], [])

    def test_source_claim_chain_keeps_root_interpretation_without_multiplying_directions(self):
        source = document(); source['evidence_items'][0].pop('was_derived_from')
        source['evidence_items'][0]['source_claims'] = ['source-claim']
        source['claims'].append({'id': 'source-claim', 'proposition': 'source-proposition', 'direction': 'DISPUTES', 'has_evidence': ['source-evidence']})
        source['propositions'].append({'id': 'source-proposition'})
        source['evidence_items'].append({'id': 'source-evidence', 'target_proposition': 'source-proposition', 'direction': 'DISPUTES', 'was_derived_from': ['file']})
        self.assertEqual([(row['evidence_id'], row['direction']) for row in collect(source, 'account')['uses']], [('evidence', 'SUPPORTS')])
        source['evidence_items'][1]['target_proposition'] = 'proposition'
        self.assertEqual(collect(source, 'account')['uses'], [])
        source['claims'][1]['was_derived_from'] = ['file']
        self.assertEqual(collect(source, 'account')['uses'][0]['evidence_id'], 'evidence')

    def test_computational_inputs_and_publication_references_do_not_become_evidence(self):
        source = document(); source['evidence_items'][0].pop('was_derived_from')
        source['claims'][0]['was_derived_from'] = ['file']
        source['evidence_items'][0].update(was_generated_by='activity', reference='file')
        source['activities'] = [{'id': 'activity', 'was_derived_from': ['dataset']}]
        source['used_edges'] = [{'subject': 'activity', 'predicate': 'prov:used', 'object': 'file'}]
        source['was_generated_by_edges'] = [{'subject': 'evidence', 'predicate': 'prov:wasGeneratedBy', 'object': 'activity'}]
        self.assertEqual(collect(source, 'account')['uses'], [])
        source['evidence_items'][0]['was_derived_from'] = ['activity']
        self.assertEqual(collect(source, 'account')['uses'], [])

    def test_recorded_derivations_reach_upstream_sources_without_expanding_members(self):
        source = document(); source['files'][0]['was_derived_from'] = ['upstream-file']
        source['files'].append({'id': 'upstream-file'})
        source['datasets'].append({'id': 'upstream', 'has_file': ['upstream-file']})
        uses = collect(source, 'account')['uses']
        self.assertEqual({row['dataset_id']: row['file_ids'] for row in uses}, {'dataset': ['file'], 'upstream': ['upstream-file']})
        # Membership alone does not establish that this particular file used
        # everything its parent dataset was derived from.
        source['files'][0].pop('was_derived_from'); source['datasets'][0]['was_derived_from'] = ['upstream']
        self.assertEqual([row['dataset_id'] for row in collect(source, 'account')['uses']], ['dataset'])
        source['evidence_items'][0]['was_derived_from'] = ['dataset']
        self.assertEqual({row['dataset_id']: row['file_ids'] for row in collect(source, 'account')['uses']}, {'dataset': [], 'upstream': []})

    def test_shared_file_membership_counts_each_canonical_dataset_without_name_merging(self):
        source = document(); source['datasets'].append({'id': 'another-dataset', 'name': 'Recorded dataset', 'has_file': ['file']})
        uses = collect(source, 'account')['uses']
        self.assertEqual([row['dataset_id'] for row in uses], ['another-dataset', 'dataset'])
        self.assertTrue(all(row['file_ids'] == ['file'] for row in uses))

    def test_ungrouped_file_and_drs_access_alias_never_invent_dataset_membership(self):
        source = document(); source['datasets'][0]['has_file'] = []
        source['datasets'][0]['has_drs_object'] = ['drs']
        source['files'][0]['drs_representation'] = 'drs'
        source['drs_objects'] = [{'id': 'drs'}]
        self.assertEqual(collect(source, 'account')['uses'], [])
        source['evidence_items'][0]['was_derived_from'] = ['drs']
        self.assertEqual(collect(source, 'account')['uses'], [])

    def test_missing_conflicting_and_bounded_paths_do_not_fabricate_credit(self):
        source = document(); source['files'] = []
        result = collect(source, 'account')
        self.assertEqual(result['uses'], []); self.assertGreater(result['excluded_paths'], 0)
        source = document(); source['files'].append({'id': 'file', 'filename': 'conflicting'})
        self.assertEqual(collect(source, 'account')['uses'], [])
        source = document()
        with patch('reveal_backend.leaderboard_sources._MAX_DEPTH', 0):
            self.assertEqual(collect(source, 'account'), {'uses': [], 'excluded_paths': 1})
        with patch('reveal_backend.leaderboard_sources._MAX_VISITS', 1):
            self.assertEqual(collect(source, 'account'), {'uses': [], 'excluded_paths': 1})
        self.assertEqual(collect({'document': source, 'coverage': {'complete': False}}, 'account')['uses'], [])

    def test_conflicting_interpretations_and_incomplete_source_claims_are_excluded(self):
        source = document()
        source['evidence_items'].append({**source['evidence_items'][0], 'direction': 'DISPUTES'})
        result = collect(source, 'account')
        self.assertEqual(result['uses'], []); self.assertEqual(result['excluded_paths'], 1)
        source = document(); source['evidence_items'][0].pop('was_derived_from')
        source['evidence_items'][0]['source_claims'] = ['source-claim']
        source['claims'].append({'id': 'source-claim', 'proposition': 'missing-proposition', 'was_derived_from': ['file']})
        result = collect(source, 'account')
        self.assertEqual(result['uses'], []); self.assertEqual(result['excluded_paths'], 1)
        source['propositions'].append({'id': 'missing-proposition'})
        self.assertEqual(collect(source, 'account')['uses'][0]['file_ids'], ['file'])

    def test_cycles_terminate_and_do_not_discard_independent_explicit_source(self):
        source = document(); source['evidence_items'][0]['was_derived_from'] = ['loop', 'file']
        source['gene_sets'] = [{'id': 'loop', 'was_derived_from': ['loop']}]
        result = collect(source, 'account')
        self.assertEqual(result['uses'][0]['file_ids'], ['file'])
        self.assertEqual(result['excluded_paths'], 1)

    def test_canonical_fixture_follows_evidence_sources_not_the_bubble_projection(self):
        root = Path(__file__).resolve().parents[3]
        source = json.loads((root / 'data/fixtures/bubble-account-v1/account-envelope.json').read_text())['document']
        account = source['scientific_accounts'][0]['id']
        result = collect(source, account)
        self.assertEqual(result['excluded_paths'], 0)
        self.assertEqual(len({row['claim_id'] for row in result['uses']}), 12)
        self.assertEqual(len({row['dataset_id'] for row in result['uses']}), 4)
        reached = {identity for row in result['uses'] for identity in row['file_ids']}
        named = {row['filename'] for row in source['files'] if row['id'] in reached}
        self.assertEqual(named, {'pigean-gene-factor.response.json', 'pigean-gene-set-factor.response.json',
                                'dismech-gap.source.json', 'illustrative-kg-assertions.json',
                                'Coronary_Artery_Disease.yaml'})
        self.assertTrue({'gene-metrics.json', 'gene-set-metrics.json', 'evidence-inventory.json'}.isdisjoint(named))
        reordered = {group: list(reversed(rows)) if isinstance(rows, list) else rows for group, rows in source.items()}
        self.assertEqual(collect(reordered, account), result)


if __name__ == '__main__':
    unittest.main()
