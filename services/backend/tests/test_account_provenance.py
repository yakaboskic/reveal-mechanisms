"""Scientific provenance incidence is independent of graph path multiplicity."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from reveal_backend import account_provenance as provenance
from reveal_backend.auth import Problem


def document():
    return {
        'scientific_accounts': [{'id': 'account', 'component_claims': ['claim'], 'conclusion_claims': ['claim']}],
        'claims': [{'id': 'claim', 'proposition': 'prop', 'has_evidence': ['evidence']}],
        'propositions': [{'id': 'prop', 'subject_entity': 'set', 'object_entity': 'HGNC.SYMBOL:UBA1'}],
        'evidence_items': [{'id': 'evidence', 'target_proposition': 'prop', 'direction': 'SUPPORTS', 'was_derived_from': ['set']}],
        'gene_sets': [{'id': 'set', 'members': ['HGNC.SYMBOL:UBA1'], 'was_generated_by': 'activity'}],
        'activities': [{'id': 'activity'}],
        'used_edges': [{'subject': 'activity', 'predicate': 'prov:used', 'object': 'dataset', 'input_role': 'data'}],
        'datasets': [{'id': 'dataset', 'name': 'Original experiment', 'was_attributed_to': ['organization']}],
        'organizations': [{'id': 'organization', 'name': 'The lab'}],
    }


class AccountProvenanceTests(unittest.TestCase):
    def test_incidence_over_union_of_claims_and_original_payloads_unchanged(self):
        source = document(); original = deepcopy(source)
        result = provenance.collect(source, 'account')
        self.assertEqual(source, original)
        self.assertEqual(result['account']['record'], original['scientific_accounts'][0])
        self.assertEqual(result['coverage']['claim_count'], 1)
        self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
        self.assertEqual(result['dataset_reuse'][0]['classification_claim_counts'], {
            'proposition_reference': 1, 'evidence_derivation': 1, 'inherited_source_claim': 0, 'factor_projection': 0})
        self.assertEqual(result['organization_reuse'][0]['claim_dataset_count'], 1)
        self.assertEqual(result['organization_reuse'][0]['roles']['attribution']['claim_count'], 1)
        self.assertEqual(result['coverage']['status'], 'complete')
        self.assertNotIn('members', next(node for node in result['nodes'] if node['id'] == 'set'))
        self.assertEqual(len(result['traces']), 2)
        self.assertNotIn('direction', next(row for row in result['traces'] if row['origin_id'] == 'prop'))
        self.assertEqual(next(row for row in result['traces'] if row['origin_id'] == 'evidence')['direction'], 'SUPPORTS')

    def test_proposition_only_and_evidence_only(self):
        for keep_proposition in (False, True):
            source = document()
            if keep_proposition:
                source['claims'][0]['has_evidence'] = []
            else:
                source['propositions'][0].pop('subject_entity')
            result = provenance.collect(source, 'account')
            self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
            self.assertEqual(len(result['traces']), 1)
            self.assertEqual(result['traces'][0]['origin_id'], 'prop' if keep_proposition else 'evidence')

    def test_claim_11_multiple_inherited_routes_and_nested_claims(self):
        source = document()
        source['scientific_accounts'][0]['component_claims'] = ['claim-11']
        source['scientific_accounts'][0]['conclusion_claims'] = ['claim-11']
        source['claims'].extend([
            {'id': 'claim-11', 'proposition': 'prop-11', 'has_evidence': ['evidence-11']},
            {'id': 'intermediate-claim', 'proposition': 'intermediate-prop', 'has_evidence': ['intermediate-evidence']},
        ])
        source['propositions'].extend([{'id': 'prop-11'}, {'id': 'intermediate-prop'}])
        source['evidence_items'].extend([
            {'id': 'evidence-11', 'target_proposition': 'prop-11', 'direction': 'DISPUTES', 'source_claims': ['claim', 'intermediate-claim']},
            {'id': 'intermediate-evidence', 'target_proposition': 'intermediate-prop', 'direction': 'SUPPORTS', 'source_claims': ['claim']},
        ])
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'][0]['claim_ids'], ['claim-11'])
        self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
        self.assertEqual(result['dataset_reuse'][0]['classification_claim_counts']['inherited_source_claim'], 1)
        self.assertTrue(all(row['direction'] == 'DISPUTES' for row in result['traces']))
        self.assertEqual([row['record']['id'] for row in result['claims']], ['claim-11'])

    def test_cycle_safe_and_preserves_shared_branches_without_enumerating_paths(self):
        source = document()
        source['gene_sets'][0]['was_derived_from'] = ['set-2', 'set-3']
        source['gene_sets'].extend([
            {'id': 'set-2', 'was_derived_from': ['set', 'dataset']},
            {'id': 'set-3', 'was_derived_from': ['set-2']},
        ])
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
        triples = {(row['subject'], row['object']) for row in result['edges']}
        self.assertTrue({('set', 'set-2'), ('set', 'set-3'), ('set-3', 'set-2'), ('set-2', 'set')} <= triples)
        self.assertLess(len(result['traces']), 8)

    def test_file_membership_does_not_inherit_parent_lineage_or_siblings(self):
        source = document()
        source['gene_sets'][0] = {'id': 'set', 'was_derived_from': ['file']}
        source['files'] = [{'id': 'file'}, {'id': 'sibling', 'was_derived_from': ['sibling-origin']}]
        source['datasets'][0].update(has_file=['file', 'sibling'], was_derived_from=['parent-origin'])
        source['datasets'].extend([{'id': 'parent-origin'}, {'id': 'sibling-origin'}])
        result = provenance.collect(source, 'account')
        self.assertEqual([row['dataset_id'] for row in result['dataset_reuse']], ['dataset'])
        self.assertNotIn('sibling', {row['id'] for row in result['nodes']})
        for trace in result['traces']:
            self.assertEqual(trace['path_steps'][-1]['direction'], 'reverse')

    def test_direct_dataset_and_file_membership_have_independent_traversal_states(self):
        source = document()
        source['gene_sets'][0] = {'id': 'set', 'was_derived_from': ['file', 'dataset']}
        source['files'] = [{'id': 'file'}]
        source['datasets'][0].update(has_file=['file'], was_derived_from=['parent-origin'])
        source['datasets'].append({'id': 'parent-origin'})
        result = provenance.collect(source, 'account')
        self.assertEqual({row['dataset_id'] for row in result['dataset_reuse']}, {'dataset', 'parent-origin'})

    def test_retained_joint_and_marginal_factor_projections_are_labeled(self):
        source = document()
        source['propositions'][0]['subject_entity'] = 'mechanism'
        source['claims'][0]['has_evidence'] = []
        source['mechanisms'] = [{'id': 'mechanism'}]
        source['gene_sets'].append({'id': 'marginal-set', 'was_derived_from': ['marginal-dataset']})
        source['datasets'].append({'id': 'marginal-dataset'})
        source['factor_projection_edges'] = [
            {'subject': 'mechanism', 'predicate': 'reveal:factorProjection', 'object': 'set',
             'reference_generation_id': 'old-generation', 'joint_loading': 0.4, 'marginal_loading': 0.9},
            {'subject': 'mechanism', 'predicate': 'reveal:factorProjection', 'object': 'marginal-set',
             'reference_generation_id': 'old-generation', 'joint_loading': None, 'marginal_loading': 0.2},
        ]
        result = provenance.collect(source, 'account')
        self.assertEqual({row['dataset_id'] for row in result['dataset_reuse']}, {'dataset', 'marginal-dataset'})
        self.assertTrue(all('factor_projection' in row['route_classifications'] for row in result['traces']))
        self.assertEqual(result['coverage']['retained_projection_coverage'][0]['retained_projection_count'], 2)
        self.assertFalse(result['coverage']['retained_projection_coverage'][0]['construction_lineage_complete'])

    def test_missing_factor_binding_never_falls_back_to_unrelated_context(self):
        source = document(); source['propositions'][0]['subject_entity'] = 'mechanism'
        source['claims'][0]['has_evidence'] = []; source['mechanisms'] = [{'id': 'mechanism'}]
        source['_provenance_coverage'] = {'retained_projection_coverage': [
            {'mechanism_id': 'mechanism', 'known_factor_binding': True, 'status': 'unresolved'}]}
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'], [])
        self.assertIn('MISSING_FACTOR_PROJECTIONS', {row['code'] for row in result['coverage']['issues']})
        self.assertTrue(result['coverage']['counts_are_lower_bounds'])

    def test_generic_mechanism_native_lineage_needs_no_factor_projection(self):
        for known_factor in (False, True):
            source = document(); source['propositions'][0]['subject_entity'] = 'mechanism'
            source['claims'][0]['has_evidence'] = []
            source['mechanisms'] = [{'id': 'mechanism', 'was_derived_from': ['dataset']}]
            source['_provenance_coverage'] = {'retained_projection_coverage': [
                {'mechanism_id': 'mechanism', 'known_factor_binding': known_factor, 'status': 'unresolved'}]}
            result = provenance.collect(source, 'account')
            self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
            self.assertEqual(result['coverage']['status'], 'incomplete' if known_factor else 'complete')
            codes = {row['code'] for row in result['coverage']['issues']}
            self.assertEqual('MISSING_FACTOR_PROJECTIONS' in codes, known_factor)
            self.assertEqual(result['propositions'][0]['provenance']['resolution'],
                             'incomplete' if known_factor else 'complete')

    def test_generic_mechanism_without_any_lineage_reports_generic_gap(self):
        source = document(); source['propositions'][0]['subject_entity'] = 'mechanism'
        source['claims'][0]['has_evidence'] = []; source['mechanisms'] = [{'id': 'mechanism'}]
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'], [])
        self.assertEqual({row['code'] for row in result['coverage']['issues']}, {'MISSING_LINEAGE'})
        self.assertEqual(result['coverage']['retained_projection_coverage'], [])
        self.assertTrue(result['coverage']['counts_are_lower_bounds'])

    def test_mapping_reference_inputs_and_organization_roles(self):
        source = document()
        source['datasets'].extend([{'id': 'mapping', 'publisher': 'organization'}, {'id': 'reference', 'has_creator': ['organization']}])
        source['used_edges'].extend([
            {'subject': 'activity', 'predicate': 'prov:used', 'object': 'mapping', 'role': 'mapping'},
            {'subject': 'activity', 'predicate': 'prov:used', 'object': 'reference', 'input_role': 'reference'},
        ])
        source['datasets'][0]['was_generated_by'] = 'generation'
        source['activities'].append({'id': 'generation', 'was_associated_with': ['organization']})
        result = provenance.collect(source, 'account')
        roles = {row['dataset_id']: row['input_roles'] for row in result['dataset_reuse']}
        self.assertEqual(roles, {'dataset': ['data'], 'mapping': ['mapping'], 'reference': ['reference']})
        organization = result['organization_reuse'][0]
        self.assertEqual(organization['claim_count'], 1)
        self.assertEqual(organization['dataset_count'], 3)
        self.assertEqual(organization['claim_dataset_count'], 3)
        self.assertEqual(set(organization['roles']), {'creator', 'publisher', 'attribution', 'generation_agent'})

    def test_unknown_input_role_not_inferred_from_names(self):
        source = document(); source['used_edges'][0].pop('input_role')
        source['datasets'][0]['name'] = 'Reference gene mapping'
        self.assertEqual(provenance.collect(source, 'account')['dataset_reuse'][0]['input_roles'], ['unknown'])

    def test_funder_award_does_not_invent_organization_from_free_text(self):
        source = document(); source['datasets'][0].pop('was_attributed_to')
        source['datasets'][0]['funded_by'] = ['award']
        source['awards'] = [{'id': 'award', 'funder_name': 'The lab'}]
        self.assertEqual(provenance.collect(source, 'account')['organization_reuse'], [])

    def test_authoring_inputs_gap_and_free_text_are_not_seeds(self):
        source = document(); source['propositions'] = [{'id': 'prop', 'statement': 'set mechanism dataset'}]
        source['claims'][0]['has_evidence'] = []
        source['scientific_accounts'][0].update(was_generated_by='activity', question='gap')
        source['knowledge_gaps'] = [{'id': 'gap', 'was_derived_from': ['dataset']}]
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'], [])
        self.assertEqual(provenance.eligible_reference_ids(source, 'account'), set())

    def test_account_context_annotations_receive_no_claim_credit(self):
        source = document(); source['claims'][0]['has_evidence'] = []; source['propositions'] = [{'id': 'prop'}]
        source['scientific_accounts'][0]['was_derived_from'] = ['set']
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'], [])
        self.assertEqual(result['organization_reuse'], [])
        self.assertEqual(result['account']['provenance']['dataset_ids'], ['dataset'])
        self.assertTrue(all(row['claim_id'] is None for row in result['traces']))

    def test_mechanistic_model_follows_causal_steps(self):
        source = document(); source['propositions'][0] = {'id': 'prop', 'mechanistic_model': 'model'}
        source['claims'][0]['has_evidence'] = []
        source['mechanistic_models'] = [{'id': 'model', 'has_causal_step': ['step']}]
        source['causal_steps'] = [{'id': 'step', 'via_mechanism': 'mechanism'}]
        source['mechanisms'] = [{'id': 'mechanism'}]
        source['factor_projection_edges'] = [{'subject': 'mechanism', 'object': 'set'}]
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
        self.assertIn('mechanism', provenance.eligible_reference_ids(source, 'account'))

    def test_causal_step_literal_intermediates_never_supply_scientific_references(self):
        for text in ('dataset', 'set', 'ordinary scientific description'):
            source = document(); source['propositions'][0] = {'id': 'prop', 'mechanistic_model': 'model'}
            source['claims'][0]['has_evidence'] = []
            source['mechanistic_models'] = [{'id': 'model', 'has_causal_step': ['step']}]
            source['causal_steps'] = [{'id': 'step', 'intermediate_mechanisms': [text]}]
            result = provenance.collect(source, 'account')
            self.assertEqual(result['dataset_reuse'], [])
            self.assertEqual(result['traces'], [])
            self.assertNotIn(text, {row['id'] for row in result['nodes']})
            self.assertEqual(result['coverage']['issues'], [])

    def test_conflicting_and_missing_observations_are_unresolved(self):
        for conflict in (True, False):
            source = document()
            if conflict: source['gene_sets'].append({'id': 'set', 'name': 'Different observation'})
            else: source['gene_sets'] = []
            result = provenance.collect(source, 'account')
            self.assertEqual(result['dataset_reuse'], [])
            self.assertTrue(result['coverage']['counts_are_lower_bounds'])
            self.assertEqual(result['evidence_items'][0]['provenance']['resolution'], 'incomplete')
            self.assertNotIn('set', json.dumps(result['coverage']['issues']))

    def test_blocked_reference_ids_cannot_be_rehydrated(self):
        source = document(); source['_provenance_coverage'] = {'blocked_reference_ids': ['set']}
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'], [])
        self.assertNotIn('set', provenance.eligible_reference_ids(source, 'account'))

    def test_invalid_evidence_target_and_invalid_source_claim_receive_no_credit(self):
        for mutation in ('target', 'source'):
            source = document(); source['propositions'][0].pop('subject_entity')
            if mutation == 'target': source['evidence_items'][0]['target_proposition'] = 'wrong'
            else:
                source['evidence_items'][0].pop('was_derived_from')
                source['evidence_items'][0]['source_claims'] = ['dataset']
            result = provenance.collect(source, 'account')
            self.assertEqual(result['dataset_reuse'], [])
            self.assertTrue(result['coverage']['counts_are_lower_bounds'])

    def test_duplicate_edges_and_input_order_do_not_change_witnesses(self):
        source = document(); source['was_derived_from_edges'] = [
            {'subject': 'set', 'predicate': 'prov:wasDerivedFrom', 'object': 'dataset'}] * 2
        source['gene_sets'][0]['was_derived_from'] = ['dataset']
        result = provenance.collect(source, 'account')
        reversed_source = {group: list(reversed(rows)) if isinstance(rows, list) else rows for group, rows in reversed(list(source.items()))}
        self.assertEqual(result, provenance.collect(reversed_source, 'account'))
        self.assertEqual(len([edge for edge in result['edges'] if edge['subject'] == 'set' and edge['object'] == 'dataset']), 1)

    def test_supplement_metadata_and_coverage_are_preserved(self):
        source = document(); annotation = {'supplement_sha256': 'a' * 64, 'reference_generation_id': 'old'}
        source['used_edges'][0]['provenance_source'] = annotation
        source['_provenance_coverage'] = {'issues': [{'code': 'ORIGINAL_UNAVAILABLE'}],
                                          'recovery_status': [{'status': 'unavailable'}]}
        result = provenance.collect(source, 'account')
        edge = next(row for row in result['edges'] if row['relation'] == 'used')
        self.assertEqual(edge['provenance_source'], [annotation])
        self.assertEqual(result['coverage']['recovery_status'], [{'status': 'unavailable'}])
        self.assertEqual(result['coverage']['status'], 'incomplete')

    def test_enrichment_issues_on_upstream_reference_mark_origin_wrappers_incomplete(self):
        source = document()
        source['_provenance_coverage'] = {'issues': [{'code': 'supplement_incomplete', 'object_id': 'activity'}]}
        result = provenance.collect(source, 'account')
        for group in ('claims', 'propositions', 'evidence_items'):
            self.assertEqual(result[group][0]['provenance']['resolution'], 'incomplete')
        self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)

    def test_dapper_edge_role_preserved_exactly(self):
        source = document(); source['used_edges'][0].pop('input_role')
        source['used_edges'][0]['edge_role'] = 'data_input'
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'][0]['input_roles'], ['data_input'])
        self.assertEqual(next(edge for edge in result['edges'] if edge['relation'] == 'used')['input_role'], 'data_input')

    def test_unsupported_predicate_namespace_or_mismatched_group_is_not_lineage(self):
        for predicate in ('untrusted:used', 'https://untrusted.example/used', 'prov:wasDerivedFrom'):
            source = document(); source['used_edges'][0]['predicate'] = predicate
            result = provenance.collect(source, 'account')
            self.assertEqual(result['dataset_reuse'], [])
            self.assertTrue(result['coverage']['counts_are_lower_bounds'])
        source = document(); source['used_edges'][0]['predicate'] = 'http://www.w3.org/ns/prov#used'
        self.assertEqual(provenance.collect(source, 'account')['dataset_reuse'][0]['claim_count'], 1)

    def test_pure_cycles_incomplete_but_converging_paths_not_cycles(self):
        source = document(); source['gene_sets'][0] = {'id': 'set', 'was_derived_from': ['set-2']}
        source['gene_sets'].append({'id': 'set-2', 'was_derived_from': ['set']})
        result = provenance.collect(source, 'account')
        self.assertEqual(result['dataset_reuse'], [])
        self.assertIn('CYCLIC_LINEAGE', {row['code'] for row in result['coverage']['issues']})
        source['gene_sets'][0]['was_derived_from'].append('dataset')
        source['gene_sets'][1]['was_derived_from'] = ['dataset']
        result = provenance.collect(source, 'account')
        self.assertNotIn('CYCLIC_LINEAGE', {row['code'] for row in result['coverage']['issues']})

    def test_invalid_evidence_is_annotated_without_traces(self):
        source = document(); source['evidence_items'][0]['target_proposition'] = 'wrong'
        result = provenance.collect(source, 'account')
        self.assertEqual(result['evidence_items'][0]['record'], source['evidence_items'][0])
        self.assertEqual(result['evidence_items'][0]['provenance']['resolution'], 'incomplete')
        self.assertEqual(result['evidence_items'][0]['provenance']['trace_count'], 0)

    def test_nodes_edges_and_depth_bounds_fail_without_rankings(self):
        for constant, bound in (('MAX_NODES', 2), ('MAX_EDGES', 2), ('MAX_DEPTH', 1), ('MAX_STATES', 1)):
            with self.subTest(constant=constant), patch.object(provenance, constant, bound):
                with self.assertRaises(Problem) as caught:
                    provenance.collect(document(), 'account')
                self.assertEqual(caught.exception.status, 422)
                self.assertEqual(caught.exception.code, 'PROVENANCE_LIMIT_EXCEEDED')

    def test_all_trace_paths_are_auditable_and_shared_edges_have_endpoints(self):
        result = provenance.collect(document(), 'account')
        edges = {edge['id']: edge for edge in result['edges']}; nodes = {node['id'] for node in result['nodes']}
        for edge in edges.values():
            self.assertIn(edge['subject'], nodes); self.assertIn(edge['object'], nodes)
        for trace in result['traces']:
            current = trace['origin_id']
            for step in trace['path_steps']:
                edge = edges[step['edge_id']]
                start, end = (edge['subject'], edge['object']) if step['direction'] == 'forward' else (edge['object'], edge['subject'])
                self.assertEqual(start, current); current = end
            self.assertEqual(current, trace['dataset_id'])


if __name__ == '__main__':
    unittest.main()
