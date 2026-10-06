"""Sanitized TTD relevance, exact attachment provenance and offline identity replay."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.catalog import Catalog
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.gene_identity import GeneCrosswalk, MAPPING_REVISION, SOURCE_ROOT, load_crosswalk, resolve_pinned_gene
from reveal_backend.mapping_identity import interpret_mapping, interpreted_mappings, normalize_disease_id, POLICY_VERSION
from reveal_backend.research_seed import prepare_research_seed
import test_research_data as seed_fixtures


class MappingTests(unittest.TestCase):
    def mapping(self, **changes):
        return {'target_id': 'MONDO:0018053', 'mapping_predicate': 'skos:exactMatch',
                'mapping_justification': 'xref', 'source': 'pinned-registry.tsv', 'confidence': '0.8500', **changes}

    def test_raw_mappings_preserved_and_policy_rejects_inheritance_and_lexical_identity(self):
        for justification in ('inherited', 'lexical_match', 'ambiguous', ''):
            mapping = self.mapping(mapping_justification=justification); before = deepcopy(mapping)
            self.assertFalse(interpret_mapping(mapping)['identity_eligible'])
            self.assertEqual(mapping, before)
        result = interpret_mapping(self.mapping())
        self.assertTrue(result['identity_eligible']); self.assertEqual(result['policy_version'], POLICY_VERSION)
        self.assertEqual(result['parsed_confidence'], '0.8500')
        self.assertFalse(interpret_mapping(self.mapping(mapping_predicate='skos:broadMatch'))['identity_eligible'])
        ambiguous = interpreted_mappings([self.mapping(), self.mapping(target_id='MONDO:0000001')])
        self.assertTrue(all(not item['identity_eligible'] and item['reason'] == 'ambiguous_identity_targets' for item in ambiguous))

    def test_supported_identifier_normalization_is_not_arbitrary_uri_matching(self):
        for value in ('MONDO:0018053', 'http://purl.obolibrary.org/obo/MONDO_0018053', 'https://identifiers.org/mondo:0018053'):
            self.assertEqual(normalize_disease_id(value), 'MONDO:0018053')
        self.assertIsNone(normalize_disease_id('https://untrusted.test/MONDO_0018053'))

    def test_official_registry_uris_normalize_without_changing_mapping_assertions(self):
        registries = {'www.ebi.ac.uk/efo/EFO_0000618': 'EFO:0000618',
                      'id.nlm.nih.gov/mesh/D012140': 'MESH:D012140',
                      'omim.org/entry/601675': 'OMIM:601675'}
        self.assertEqual(POLICY_VERSION, 'reveal.trait-identity-eligibility/2')
        for path, expected in registries.items():
            for scheme in ('http', 'https'):
                uri = scheme + '://' + path
                with self.subTest(uri=uri):
                    self.assertEqual(normalize_disease_id(uri), expected)
                    rows = [self.mapping(target_id=uri), self.mapping(target_id=expected)]
                    before = deepcopy(rows)
                    self.assertTrue(all(row['identity_eligible'] for row in interpreted_mappings(rows)))
                    self.assertEqual(rows, before)
                    for changes, reason in (({'mapping_justification': 'inherited'}, 'inherited_mapping'),
                                            ({'mapping_justification': 'lexical_match'}, 'unsupported_or_lexical_justification'),
                                            ({'mapping_predicate': 'skos:broadMatch'}, 'non_identity_predicate'),
                                            ({'mapping_predicate': 'skos:relatedMatch'}, 'non_identity_predicate'),
                                            ({'source': None}, 'missing_source')):
                        raw = self.mapping(target_id=uri, **changes); original = deepcopy(raw)
                        result = interpret_mapping(raw)
                        self.assertFalse(result['identity_eligible'])
                        self.assertEqual(result['reason'], reason)
                        self.assertEqual(raw, original)

    def test_registry_uri_normalization_requires_exact_host_path_and_http_scheme(self):
        for uri in ('https://www.ebi.ac.uk/efo/EFO_0000618',
                    'https://id.nlm.nih.gov/mesh/D012140', 'https://omim.org/entry/601675'):
            scheme, tail = uri.split('://'); host, path = tail.split('/', 1)
            for invalid in (f'ftp://{tail}', f'file://{tail}', f'{scheme}://{host}.untrusted.test/{path}',
                            f'{scheme}://untrusted.test/{tail}', f'{scheme}://user@{tail}',
                            f'{scheme}://{host}:443/{path}', f'{scheme}://{host}//{path}',
                            uri + '/', uri + '?source=registry', uri + '#entry',
                            f'{scheme}://{host}/{path.replace("/", "%2F")}'):
                with self.subTest(uri=invalid): self.assertIsNone(normalize_disease_id(invalid))

    def test_disease_priority_matches_official_registry_uris_and_keeps_ambiguity_ineligible(self):
        for uri, curie, other in (('https://www.ebi.ac.uk/efo/EFO_0000618', 'EFO:0000618', 'EFO:0000001'),
                                 ('https://id.nlm.nih.gov/mesh/D012140', 'MESH:D012140', 'MESH:D000001'),
                                 ('https://omim.org/entry/601675', 'OMIM:601675', 'OMIM:100000')):
            for disease, target in ((uri, curie), (curie, uri)):
                with self.subTest(disease=disease, target=target):
                    catalog = Catalog(); catalog.loaded = True
                    catalog.factors = {'supported': {'source_id': 'supported', 'kpn_trait': {
                        'id': 'trait:fixture', 'ontology_mappings': [self.mapping(target_id=target)]}},
                        'ambiguous': {'source_id': 'ambiguous', 'kpn_trait': {'id': 'trait:other',
                            'ontology_mappings': [self.mapping(target_id=target), self.mapping(target_id=other)]}}}
                    gap = {'object': {'id': 'gap:fixture', 'about_entities': [disease]}}
                    selected = catalog.disease_factors(gap, 5, ())
                    self.assertEqual([item['record']['source_id'] for item in selected], ['supported'])
                    self.assertEqual(selected[0]['retrieval']['normalized_target_id'], curie)
                    self.assertEqual(selected[0]['retrieval']['policy_version'], POLICY_VERSION)

    def test_ttd_disease_priority_is_bounded_deterministic_and_respects_kept_dismissed(self):
        catalog = Catalog(); catalog.loaded = True
        def factor(identity, mapping): return {'source_id': identity, 'reference_generation_id': 'a'*64,
            'kpn_trait': {'id': 'KPN.TRAIT:0005855', 'ontology_mappings': [mapping]}}
        catalog.factors = {name: factor(name, self.mapping()) for name in ('ttd:2', 'ttd:1', 'manual', 'dismissed')}
        catalog.factors['other'] = factor('other', self.mapping(mapping_justification='inherited'))
        gap = {'object': {'id': 'gap:ttd', 'about_entities': ['http://purl.obolibrary.org/obo/MONDO_0018053']}}
        before = deepcopy(catalog.factors)
        selected = catalog.disease_factors(gap, 2, {'manual', 'dismissed'})
        self.assertEqual([x['record']['source_id'] for x in selected], ['ttd:1', 'ttd:2'])
        self.assertTrue(all(x['retrieval']['reference_generation_id'] == 'a'*64 for x in selected))
        self.assertEqual(catalog.disease_factors(gap, 0, ()), [])
        self.assertEqual(catalog.factors, before)


class CrosswalkTests(unittest.TestCase):
    def test_authoritative_snapshot_exact_replay_species_alias_and_withdrawal(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('resolution must be offline')):
            resolved = resolve_pinned_gene('ERCC2', taxon='NCBITaxon:9606', source_import='import:fixture', source_index=17)
            self.assertEqual(resolved['status'], 'mapped')
            self.assertIn('HGNC:3434', resolved['verified_identity'])
            self.assertEqual(resolved['source_local']['source_index'], 17)
            self.assertEqual(resolved['mapping_revision'], MAPPING_REVISION)
            self.assertEqual(resolved, resolve_pinned_gene('ERCC2', taxon='9606', mapping_revision=MAPPING_REVISION,
                                                         source_import='import:fixture', source_index=17))
            self.assertEqual(resolve_pinned_gene('ERCC2')['status'], 'needs_taxon')
            self.assertEqual(resolve_pinned_gene('ERCC2', taxon='NCBITaxon:10090')['status'], 'unsupported_taxon')
            self.assertEqual(resolve_pinned_gene('A1S9T', taxon='9606')['status'], 'obsolete')
            self.assertEqual(resolve_pinned_gene('p53', taxon='9606')['status'], 'alias')
            ambiguous = resolve_pinned_gene('BTF2', taxon='9606')
            self.assertEqual(ambiguous['status'], 'ambiguous'); self.assertEqual(ambiguous['verified_identity'], [])
            self.assertEqual(resolve_pinned_gene('NOT_A_GENE', taxon='9606')['status'], 'unmapped')
            with self.assertRaises(ValueError): resolve_pinned_gene('ERCC2', taxon='9606', mapping_revision='b'*64)

    def test_ambiguous_alias_candidates_do_not_become_identity(self):
        records = [{'symbol': name, 'identifiers': ['HGNC:'+str(index)], 'aliases': ['SHARED'], 'status': 'approved'}
                   for index, name in enumerate(('A', 'B'))]
        crosswalk = GeneCrosswalk({'mapping_revision': 'r', 'source_release': 'synthetic', 'taxon': 'NCBITaxon:9606'}, records)
        ambiguous = crosswalk.resolve('SHARED', taxon='9606')
        self.assertEqual(ambiguous['status'], 'ambiguous'); self.assertEqual(ambiguous['verified_identity'], [])
        self.assertEqual(len(ambiguous['candidates']), 2)

    def test_modified_pinned_original_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'manifest.json').write_bytes((SOURCE_ROOT/'manifest.json').read_bytes())
            (root/'hgnc_complete_set.tsv.gz').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checksum changed'): load_crosswalk(root)


class SeedRelevanceTests(unittest.TestCase):
    def test_exact_attachment_source_and_retrieval_audit_remain_available_with_bounded_first_view(self):
        dapper, frozen, binding = seed_fixtures.SeedTests().inputs()
        record = {'id': 'dismech:ttd#/pathophysiology/0', 'name': 'Fixture mechanism',
            'raw': {'description': 'Long source excerpt. '*500, 'notes': 'Candidate interpretation, not established.',
                    'evidence': [{'reference': 'PMID:123456', 'supports': 'SUPPORT'}]}}
        reference = {'source_id': record['id'], 'source_revision': 'f'*64}
        frozen['linked_dismech_context'] = [reference]
        binding['pinned_dismech_context'] = [{**reference, 'source_detail': {'source_file': 'kb/TTD.yaml',
            'source_pointer': '/pathophysiology/0', 'raw': record, 'payload_sha256': sha256(canonical_json(record))}}]
        binding['retrieval'] = {'factor': {'hit': {'reason': 'Selected disease', 'retrieval': {'vectors': [0.125]*20000}}}}
        before = deepcopy((frozen, binding))
        built = prepare_research_seed(frozen, binding, dapper=dapper, project_root=seed_fixtures.ROOT)
        attachment = built.package['dismech']['mechanisms'][record['id']]
        self.assertTrue(attachment['truncated']); self.assertEqual(len(attachment['description_excerpt']), 4000)
        source = built.package['source_artifacts'][attachment['source_ref']['artifact_id']]
        self.assertEqual(built.files[source['path']], canonical_json(record))
        self.assertIn(source['dapper_file_id'], built.package['eligible_source_ids'])
        self.assertEqual(attachment['evidence_refs'][0]['source_ref']['pointer'], '/raw/evidence/0')
        selection = built.files[built.package['source_artifacts']['frozen-selection']['path']]
        audit = built.files[built.package['source_artifacts']['frozen-selection-audit']['path']]
        self.assertLess(len(selection), 5000); self.assertGreater(len(audit), 100000)
        self.assertNotIn(b'vectors', selection); self.assertIn(b'vectors', audit)
        self.assertEqual((frozen, binding), before)
        self.assertEqual(json.loads(audit)['retrieval'], binding['retrieval'])

    def test_historical_reference_only_seed_is_not_silently_rehydrated(self):
        dapper, frozen, binding = seed_fixtures.SeedTests().inputs()
        frozen['linked_dismech_context'] = [{'source_id': 'dismech:old', 'source_revision': 'a'*64}]
        built = prepare_research_seed(frozen, binding, dapper=dapper, project_root=seed_fixtures.ROOT)
        self.assertEqual(built.package['dismech']['mechanisms'], {})
