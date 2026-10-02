"""The generated API contract (api/openapi.json) accepts what the reference-reload code writes.

Pins docs/reference-reload.md §5 against drift in either direction: archive stamps, frozen
factor snapshots and KPN factor records built by the backend's own helpers must validate,
and the model enums, reference_state filter, reference-factor route and reference error codes
must match the backend. Regenerate the contract with scripts/build_openapi.py after changing them.
"""
from datetime import datetime
import hashlib
import json
import unittest

from jsonschema import Draft202012Validator

from reveal_backend import analysis_outcomes as outcomes
from reveal_backend import reference_archive as archive
from reveal_backend import reference_generation as reference
from reveal_backend.account_discovery import REFERENCE_STATES
from reveal_backend.catalog import Catalog, archived_factor
from reveal_backend.repository import canonical, digest
from reveal_backend.runtime_config import ROOT

CONTRACT = json.loads((ROOT / 'api/openapi.json').read_text())
SCHEMAS = CONTRACT['components']['schemas']
MAPPING = 'a' * 64
LEGACY, KPN, KPN2 = reference.legacy_generation_id(MAPPING), 'b' * 64, 'c' * 64
LEGACY_ID = 'factor:portal:T2D:cfde-inc-v2:Factor1'
KPN_TRAIT = 'KPN.TRAIT:0000398'
KPN_ID = reference.public_id(KPN_TRAIT, 'Factor2')
MECHANISM = 'dapper:Mechanism.' + '1' * 32
GAP = {'id': 'dapper:KnowledgeGap.' + '3' * 32, 'source_id': 'dismech:gap-1', 'source_revision': '4' * 64}
JOB, REQUEST, OUTCOME = '44444444-4444-4444-8444-444444444444', '33333333-3333-4333-8333-333333333333', '66666666-6666-4666-8666-666666666666'
ACCOUNT = 'dapper:ScientificAccount.' + '5' * 32


def errors(value, name):
    """Validation messages of `value` against a contract component, resolved as app.validate() does."""
    return [error.message for error in Draft202012Validator({'$ref': '#/components/schemas/' + name, **CONTRACT}).iter_errors(value)]


def operation(path, method='get'):
    return CONTRACT['paths'][path][method]


class Runtime:
    """Deterministic stand-in for the pinned DAPPER runtime (content-addressed ids)."""
    schema = 'pinned-schema'
    def compute_id(self, node, kind, schema): return f'dapper:{kind}.' + digest(node)[:32]
    def file(self, name, data, mime):
        node = {'filename': name, 'mime_type': mime, 'sha256': hashlib.sha256(data).hexdigest(), 'size_in_bytes': len(data)}
        return {**node, 'id': self.compute_id(node, 'File', self.schema)}


class Cursor:
    def __init__(self, answer): self.answer, self.rows = answer, []
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, sql, params=()): self.rows = list(self.answer(sql, tuple(params)))
    def fetchall(self): return self.rows


class Connection:
    def __init__(self, answer): self.answer = answer
    def cursor(self): return Cursor(self.answer)


def legacy_answer(sql, params):
    if 'FROM eaggl_cfde_factor_links' in sql:
        yield (LEGACY_ID, 7, 'e' * 64, canonical({'raw': {'label': 'Insulin secretion'}}), 'T2D::Factor1', 'T2D', 'Insulin secretion program',
               canonical({'factor': 'Factor1', 'gene_score': 3.5}))
    elif 'FROM eaggl_cfde_gene_set_links' in sql:
        yield (1, 'GO__insulin_secretion', 'gene_set:GO__insulin_secretion', 'dapper:GeneSet.' + 'a' * 32)
        yield (2, 'HALLMARK__glycolysis___KEGG__beta_cell', 'gene_set:x', None)  # an unresolved legacy alias
    elif 'eaggl_gene_loadings' in sql: yield ('INS', 0.9)
    else: raise AssertionError(sql)


def kpn_answer(sql, params):
    if 'FROM reference_factors f JOIN kpn_traits' in sql:
        yield (KPN_ID, KPN_TRAIT + '::Factor2', 'T2D::Factor2', KPN_TRAIT, 'Beta cell stress', 'e' * 64,
               canonical({'kpn': {'phenotype_name': 'Type 2 diabetes'}}), 'Type 2 diabetes', 'T2D')
    elif 'eaggl_gene_loadings' in sql: yield ('PDX1', 0.7)
    elif 'FROM factor_gene_set_projections' in sql:
        yield (1, 'dapper:GeneSet.' + 'b' * 32, 'Insulin secretion', 'GO_BP', 'dapper:GeneSetCollection.' + 'c' * 32, None, 0.12, 0.34)
    else: raise AssertionError(sql)


LEGACY_GENERATION = {'generation_id': LEGACY, 'kind': reference.LEGACY_KIND, 'model': reference.LEGACY_MODEL,
                     'legacy_mapping_run_id': MAPPING, 'manifest': {'format': 'reveal.reference-generation/1'}}
KPN_GENERATION = {'generation_id': KPN, 'kind': reference.KPN_KIND, 'model': reference.KPN_MODEL, 'eaggl_import_id': 'e' * 64,
                  'manifest': {'format': 'reveal.reference-generation/1'}}


def stamp(**previous):
    """A stamp as the archive pass writes it for an account built on the legacy generation."""
    binding = {'eaggl_factor_id': 'T2D::Factor1', 'mapping_run_id': MAPPING, 'cfde_node_id': LEGACY_ID}
    reference_ = {'source': 'eaggl', 'source_id': LEGACY_ID, 'source_revision': '5' * 64, 'dapper_id': MECHANISM}
    request_binding = {'anchors': [binding], 'anchor_display': {LEGACY_ID: {'reference': reference_, 'label': 'Insulin secretion', 'subtitle': 'T2D (Factor1)'}}}
    composer = {'eaggl_anchors': [{'reference': reference_, 'origin': 'manual', 'suggestion_id': None}]}
    anchors = archive.anchors_for_stamp(request_binding, composer=composer, generation_id=LEGACY,
        scientific_document={'mechanisms': [{'id': MECHANISM, 'name': 'T2D mechanism Factor1', 'description': 'EAGGL mechanism.'}]})
    return reference.build_stamp(LEGACY, KPN, reference={'model': reference.LEGACY_MODEL, 'anchors': anchors}, gap=dict(GAP),
        analysis={'job_id': JOB, 'request_id': REQUEST, 'evidence_package_sha256': '6' * 64, 'account_id': ACCOUNT}, at='2026-10-01T09:00:00Z', **previous)


class ArchiveStampContractTests(unittest.TestCase):
    def test_stamps_written_by_the_backend_validate(self):
        first = stamp()
        self.assertEqual(errors(first, 'ReferenceArchive'), [])
        self.assertEqual(errors(reference.public_stamp(first), 'ReferenceArchive'), [])
        second = reference.build_stamp(None, KPN2, reference=None, gap=None, analysis={}, previous=first, at='2027-01-01T00:00:00Z')
        self.assertEqual((len(second['history']), errors(second, 'ReferenceArchive')), (2, []))
        # Stamps from a request without a resolvable gap or display text still validate.
        bare = reference.build_stamp(LEGACY, KPN, reference={'model': reference.KPN_MODEL, 'anchors': [dict(first['reference']['anchors'][0],
            mechanism_id=None, factor_id=None, trait=None, label=None, name=None, origin=None)]}, gap=None, analysis={}, at='2026-10-01T09:00:00Z')
        self.assertEqual(errors(bare, 'ReferenceArchive'), [])

    def test_stamp_schema_is_closed(self):
        self.assertTrue(errors(dict(stamp(), extra=True), 'ReferenceArchive'))
        self.assertTrue(errors(dict(stamp(), status='current'), 'ReferenceArchive'))
        self.assertTrue(errors(dict(stamp(), history=[]), 'ReferenceArchive'))
        self.assertTrue(errors({key: value for key, value in stamp().items() if key != 'analysis'}, 'ReferenceArchive'))

    def test_archive_is_optional_on_every_archived_record(self):
        for name in ('AccountSummary', 'AccountResult', 'AnalysisOutcome', 'AnalysisOutcomeSummary', 'ResearchRequest'):
            self.assertEqual(SCHEMAS[name]['properties']['archive'], {'$ref': '#/components/schemas/ReferenceArchive'}, name)
            self.assertNotIn('archive', SCHEMAS[name]['required'], name)

    def test_outcome_summary_keys_are_contract_keys(self):
        record = {key: None for key in ('id', 'outcome', 'summary', 'knowledge_gap', 'anchors', 'created_at', 'attribution')}
        properties = SCHEMAS['AnalysisOutcomeSummary']['properties']
        self.assertLessEqual(set(outcomes.summary(record)), set(properties))
        self.assertIn('archive', outcomes.summary(dict(record, archive=stamp())))
        self.assertLessEqual(set(outcomes.summary(dict(record, archive=stamp()))), set(properties))


class ReferenceFactorContractTests(unittest.TestCase):
    def served(self, connection, generation, source, runtime):
        row, = archive.capture_factors(connection, generation, [source], runtime=runtime)
        # As GET /v1/reference-factors/{archive_id} serves it from archived_reference_factors.
        return archived_factor((row['archive_id'], row['generation_id'], row['source_id'], canonical(row['snapshot']), datetime(2026, 10, 1, 8, 30)))

    def test_captured_snapshots_validate(self):
        for connection, generation, source in [(Connection(legacy_answer), LEGACY_GENERATION, LEGACY_ID), (Connection(kpn_answer), KPN_GENERATION, KPN_ID)]:
            with self.subTest(model=generation['model']):
                served = self.served(connection, generation, source, Runtime())
                self.assertEqual(errors(served, 'ArchivedReferenceFactor'), [])
                self.assertEqual(served['captured_at'], '2026-10-01T08:30:00Z')
                self.assertTrue(errors(dict(served, unexpected=1), 'ArchivedReferenceFactor'))

    def test_snapshot_without_a_minted_mechanism_validates(self):
        class Unminted(Runtime):
            def compute_id(self, node, kind, schema): return None
        served = self.served(Connection(legacy_answer), LEGACY_GENERATION, LEGACY_ID, Unminted())
        self.assertIsNone(served['mechanism']['id'])
        self.assertEqual(errors(served, 'ArchivedReferenceFactor'), [])

    def test_route_is_public_reference_data(self):
        route = operation('/v1/reference-factors/{archive_id}')
        self.assertEqual(route['operationId'], 'getArchivedReferenceFactor')
        self.assertIn({}, route['security'])
        self.assertEqual(route['responses']['200']['content']['application/json']['schema'], {'$ref': '#/components/schemas/ArchivedReferenceFactor'})
        self.assertIn('404', route['responses'])


class KpnFactorContractTests(unittest.TestCase):
    def test_catalog_kpn_record_validates_as_eaggl_factor(self):
        catalog = Catalog()
        catalog.reference_generation_id, catalog.eaggl_import, catalog.embedding_run = KPN, 'e' * 64, 'f' * 64
        catalog.factors, catalog.factor_legacy, catalog.bindings = {}, {}, {}
        metadata = {'label': 'Beta cell stress', 'kpn': {'phenotype_name': 'Type 2 diabetes'}}
        row = {'public_id': KPN_ID, 'factor_key': KPN_TRAIT + '::Factor2', 'eaggl_factor_id': 'T2D::Factor2', 'kpn_trait_id': KPN_TRAIT,
               'factor': 'Factor2', 'label': 'Beta cell stress', 'phenotype_name': 'Type 2 diabetes', 'metadata': metadata,
               'source_revision': digest(metadata), 'legacy_phenotype_id': 'T2D', 'trait_group': None, 'trait_type': 'phenotype'}
        catalog.kpn_factors(Runtime(), [row])
        record = catalog.factors[KPN_ID]
        self.assertEqual((record['model'], record['reference_generation_id']), (reference.KPN_MODEL, KPN))
        self.assertEqual(errors(record, 'EagglFactor'), [])
        self.assertEqual(errors(record, 'MechanismRecord'), [])
        self.assertEqual(errors(dict(record, kpn_trait=None), 'EagglFactor'), [])
        self.assertTrue(errors(dict(record, model='eaggl-capped-v2'), 'EagglFactor'))

    def test_model_enums_match_the_backend(self):
        models = list(reference.MODELS)
        self.assertEqual(SCHEMAS['EagglFactor']['properties']['model']['enum'], models)
        for name in ('Composer', 'SuggestInput'): self.assertEqual(SCHEMAS[name]['properties']['model']['enum'], models, name)
        search, = [p for p in operation('/v1/mechanisms/search')['parameters'] if p['name'] == 'model']
        self.assertEqual(search['schema']['enum'], models)
        self.assertNotIn('default', search['schema'])  # results always come from the active generation

    def test_stored_and_new_composers_validate(self):
        composer = {'source_gap': None, 'eaggl_anchors': [], 'dismissed_source_ids': [], 'mechanism_subquery': '', 'selected_kgs': []}
        for model in reference.MODELS: self.assertEqual(errors(dict(composer, model=model), 'Composer'), [], model)
        self.assertTrue(errors(dict(composer, model='other'), 'Composer'))


class ReferenceSurfaceContractTests(unittest.TestCase):
    LISTINGS = [('/v1/accounts', 'listAccounts'), ('/v1/analysis-outcomes', 'listAnalysisOutcomes'),
                ('/v1/knowledge-gaps/{gap_id}/accounts', 'listKnowledgeGapAccounts'), ('/v1/knowledge-gaps/{gap_id}/outcomes', 'listKnowledgeGapOutcomes')]

    def test_listings_take_reference_state(self):
        for path, identity in self.LISTINGS:
            route = operation(path)
            self.assertEqual(route['operationId'], identity)
            parameter, = [p for p in route['parameters'] if p['name'] == 'reference_state']
            self.assertEqual((parameter['in'], parameter['required'], parameter['schema']['enum'], parameter['schema']['default']),
                             ('query', False, list(REFERENCE_STATES), 'all'), path)

    def problem(self, path, method, status, code):
        examples = operation(path, method)['responses'][str(status)]['content']['application/problem+json']['examples']
        value = examples[code.lower()]['value']
        self.assertEqual((value['status'], value['code'], value['retryable']), (status, code, status == 503))
        self.assertEqual(errors(value, 'Problem'), [])
        return value

    def test_reference_error_codes_are_documented(self):
        for path, method in [('/v1/drafts', 'post'), ('/v1/drafts/{draft_id}', 'patch'), ('/v1/jobs', 'post'), ('/v1/jobs/{job_id}/retry-review', 'post')]:
            self.problem(path, method, 409, 'REFERENCE_GENERATION_SUPERSEDED')
            self.problem(path, method, 503, 'REFERENCE_RELOAD_IN_PROGRESS')
        self.problem('/v1/mechanisms/suggest', 'post', 409, 'REFERENCE_GENERATION_SUPERSEDED')
        gone = self.problem('/v1/mechanisms/{source_id}', 'get', 410, 'REFERENCE_GENERATION_SUPERSEDED')
        self.assertEqual(errors(gone['archived_reference_factor'], 'ArchivedReferenceFactor'), [])
        self.assertEqual(errors(dict(gone, archived_reference_factor=None), 'Problem'), [])

    def test_job_status_has_no_archived_state(self):
        self.assertNotIn('archived', SCHEMAS['Job']['properties']['status']['enum'])


if __name__ == '__main__':
    unittest.main()
