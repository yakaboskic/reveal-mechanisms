"""Check the actual collector output against the LinkML-derived wire contract."""
from copy import deepcopy
import json
import unittest

import test_evidence_package as fixtures
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, decode, sha256
from reveal_backend.evidence_schema import generate_schema, validate_package_shape


class EvidenceSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.EvidencePackageTests.setUpClass()
        cls.addClassCleanup(fixtures.EvidencePackageTests.tearDownClass)
        cls.package = fixtures.EvidencePackageTests.built.package
        cls.schema = generate_schema(fixtures.ROOT / 'schema/evidence-package.yaml')

    def validate(self, package):
        validate_package_shape(package, self.schema)

    def test_collector_json_and_yaml_validate_without_mutation(self):
        original = deepcopy(self.package)
        self.validate(self.package)
        self.validate(decode(fixtures.EvidencePackageTests.built.files['evidence-package.yaml'], 'yaml'))
        self.assertEqual(self.package, original)

    def test_generated_schema_matches_canonical_linkml(self):
        generated = json.loads((fixtures.ROOT / 'schema/evidence-package.schema.json').read_bytes())
        self.assertEqual(sha256(canonical_json(self.schema)), sha256(canonical_json(generated)))

    def test_required_sections_version_and_unknown_application_fields(self):
        mutations = [lambda p: p.pop('pigean'),
                     lambda p: p.update(package_version='reveal.evidence-package/0.1-draft'),
                     lambda p: p.update(unmodeled_field=True),
                     lambda p: p['selection'].update(eaggl_mechanism_ids=[]),
                     lambda p: p['selection'].update(eaggl_mechanism_ids=['Factor1']),
                     lambda p: p['policy'].update(max_nodes=-1),
                     lambda p: p['readiness'].update(agent_dispatch_validated='yes'),
                     lambda p: p['coverage']['queries'].update(gene='made_up')]
        for mutate in mutations:
            package = deepcopy(self.package); mutate(package)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(EvidenceBuildError): self.validate(package)

    def test_bad_loadings_metrics_hashes_and_pointers(self):
        for invalid in ['high', {'score': 0.5}, float('nan')]:
            package = deepcopy(self.package)
            package['pigean']['mechanisms'][fixtures.FACTOR]['gene_loadings']['items']['gene:SHH']['factor_value'] = invalid
            with self.subTest(value=invalid), self.assertRaises(EvidenceBuildError): self.validate(package)
        package = deepcopy(self.package)
        package['builder']['source_sha256'] = 'not-a-checksum'
        with self.assertRaises(EvidenceBuildError): self.validate(package)
        package = deepcopy(self.package)
        package['dismech']['knowledge_gap']['source_ref']['pointer'] = '/bad/~2'
        with self.assertRaises(EvidenceBuildError): self.validate(package)
        package = deepcopy(self.package)
        metrics = next(iter(package['pigean']['traits'].values()))['gene_associations']['items']['gene:SHH']['observations'][0]['reported_metrics']
        metrics['causal_probability'] = 0.9
        with self.assertRaises(EvidenceBuildError): self.validate(package)

    def test_dapper_objects_are_typed_and_closed(self):
        package = deepcopy(self.package)
        package['dapper_context']['knowledge_gaps'][0]['invented_field'] = 'no'
        with self.assertRaises(EvidenceBuildError): self.validate(package)
        package = deepcopy(self.package)
        package['dapper_context']['gene_sets'][0]['n_genes'] = 'many'
        with self.assertRaises(EvidenceBuildError): self.validate(package)

    def test_upstream_records_are_extensible_with_typed_bindings(self):
        package = deepcopy(self.package)
        mechanism = next(iter(package['dismech']['mechanisms'].values()))
        mechanism['future_curated_field'] = [{'upstream_detail': True}]
        package['dismech']['other_context'][0]['record'] = [{'whole_section_item': 'retained'}]
        self.validate(package)
        mechanism['dapper_id'] = 42
        with self.assertRaises(EvidenceBuildError): self.validate(package)
        package = deepcopy(self.package)
        package['dismech']['other_context'][0]['record'] = 'not a structured source record'
        with self.assertRaises(EvidenceBuildError): self.validate(package)

    def test_initial_package_cannot_include_external_assertions(self):
        package = deepcopy(self.package)
        package['external_evidence']['assertions'] = [{'fabricated': True}]
        with self.assertRaises(EvidenceBuildError): self.validate(package)

    def test_unresolved_attachment_nulls_and_missing_query_states(self):
        package = deepcopy(self.package)
        package['dismech']['knowledge_gap']['attachments'].append(
            {'attachment_index': 4, 'gap_id': fixtures.GAP, 'source_reference': 'broken', 'resolution': 'invalid_syntax'})
        package['coverage']['queries']['factor'] = 'not_captured'
        package['policy']['retain_node_ids'] = None
        self.validate(package)


if __name__ == '__main__':
    unittest.main()
