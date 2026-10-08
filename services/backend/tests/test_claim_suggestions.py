"""Advisory claim-structure suggestions: classification, checks, lint and hosted wiring, storage and the report.

Synthetic identifiers only; suggestions never change lint validity, errors or warnings.
"""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import research_seed
from reveal_backend.acceptance import claim_structure_record
from reveal_backend.authoring_structure import _normalized_finding, diagnostic_response
from reveal_backend.claim_suggestions import BIOLINK, FAMILIES, GAP_RELATION, LEGACY, OBO, PROV, claim_structure, factor_traits, mentions
from reveal_backend.evidence_package import canonical_json, decode

ROOT = Path(__file__).resolve().parents[3]
GAP = 'dapper:KnowledgeGap.' + 'G' * 32
FACTOR = 'dapper:Mechanism.' + 'F' * 32
SET = 'dapper:GeneSet.' + 'S' * 32
FILE = 'dapper:File.' + 'C' * 32
TRAIT = 'KPN.TRAIT:0000398'
INS, C10, C10_UPPER = 'HGNC.SYMBOL:INS', 'HGNC.SYMBOL:C10orf71', 'HGNC.SYMBOL:C10ORF71'
FAMILY_CHECKS = {'atomic-result-kind', 'atomic-triple', 'atomic-orientation', 'atomic-endpoints', 'atomic-predicate', 'atomic-score',
                 'atomic-capture', 'atomic-single-fact', 'atomic-value-in-statement'}


def package(biolink=True, traits=True):
    mechanism = {'id': FACTOR, 'name': 'T2D mechanism Factor8'}
    if traits: mechanism['description'] = 'EAGGL mechanism factor:kpn:0000398:eaggl-capped-v1:Factor8. KPN trait KPN.TRAIT:0000398 (T2D). Source label: x.'
    return {'selection': {'knowledge_gap_id': GAP}, 'source_artifacts': {'capture': {'dapper_file_id': FILE}},
            'dapper_context': {'knowledge_gaps': [{'id': GAP}], 'mechanisms': [mechanism], 'files': [{'id': FILE}],
                               'gene_sets': [{'id': SET, 'members': [C10, INS]}],
                               'prefixes': {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:', **({'biolink': BIOLINK} if biolink else {})}}}


class Account:
    """A synthetic account document built one Claim at a time."""
    def __init__(self):
        self.doc = {'scientific_accounts': [{'id': 'urn:test:account', 'question': GAP, 'component_claims': []}],
                    'propositions': [], 'claims': [], 'evidence_items': [], 'claim_scores': []}

    def claim(self, name, statement, triple=None, kind='RESULT', scores=(), derived=(FILE,), cites=()):
        proposition = {'id': f'urn:test:{name}:proposition', 'statement': statement, 'proposition_kind': kind}
        proposition.update((key, value) for key, value in zip(('subject_entity', 'relation', 'object_entity'), triple or ()) if value)
        evidence = {'id': f'urn:test:{name}:evidence', 'target_proposition': proposition['id'], 'direction': 'SUPPORTS',
                    'context': 'Captured row /result/items/0.', 'explanation': 'Synthetic observation.'}
        if derived: evidence['was_derived_from'] = list(derived)
        if cites: evidence['source_claims'] = ['urn:test:' + cited for cited in cites]
        claim = {'id': 'urn:test:' + name, 'proposition': proposition['id'], 'statement': statement, 'has_evidence': [evidence['id']]}
        for index, (metric, score_kind, value) in enumerate(scores):
            score = {'id': f'urn:test:{name}:score-{index}', 'metric': metric, 'score_kind': score_kind, 'value': value, 'interpretation': 'Exact.'}
            self.doc['claim_scores'].append(score); claim.setdefault('has_score', []).append(score['id'])
        self.doc['propositions'].append(proposition); self.doc['evidence_items'].append(evidence); self.doc['claims'].append(claim)
        self.doc['scientific_accounts'][0]['component_claims'].append(claim['id'])
        return self

    # One conformant atomic Claim per family; overrides change single fields.
    def factor_gene(self, name='factor-gene', gene=C10_UPPER, **changes):
        return self.claim(name, **{'statement': 'Gene C10ORF71 has loading 0.2502 on factor Factor8.', 'triple': (gene, 'obo:RO_0002610', FACTOR),
                                   'scores': [('loading', 'LOADING', 0.2502)], **changes})

    def phenotype_gene(self, name='phenotype-gene', gene=C10, trait=TRAIT, **changes):
        return self.claim(name, **{'statement': 'Gene C10orf71 is associated with T2D in PIGEAN (combined -0.429).',
                                   'triple': (gene, 'biolink:genetically_associated_with', trait),
                                   'scores': [('combined', 'SCORE', -0.429), ('log_bf', 'SCORE', -1.2)], **changes})

    def factor_gene_set(self, name='factor-gene-set', **changes):
        return self.claim(name, **{'statement': 'Gene set S has joint loading 0.198 on factor Factor8.', 'triple': (SET, 'obo:RO_0002610', FACTOR),
                                   'scores': [('joint_loading', 'LOADING', 0.198)], 'derived': (FILE, SET), **changes})

    def gene_gene_set(self, name='gene-gene-set', gene=C10, **changes):
        return self.claim(name, **{'statement': 'Gene set S has member C10orf71.', 'triple': (SET, 'prov:hadMember', gene), 'derived': (FILE, SET), **changes})

    def gene_set_trait(self, name='gene-set-trait', **changes):
        return self.claim(name, **{'statement': 'Gene set S is associated with T2D (beta_uncorrected 1.62).', 'triple': (SET, 'obo:RO_0002610', TRAIT),
                                   'scores': [('beta_uncorrected', 'EFFECT_ESTIMATE', 1.62)], 'derived': (FILE, SET), **changes})

    def gap(self, cites, name='gap', subject=C10, obj=GAP, statement="C10orf71 links Factor8, T2D and the gap within the factor's fit."):
        return self.claim(name, statement, (subject, GAP_RELATION, obj), kind='BIOLOGICAL_INTERPRETATION', derived=(), cites=cites)

    def structure(self, context=None, use_package=True):
        return claim_structure(self.doc, context if context is not None else package() if use_package else None)


def checks(result, claim=None):
    """Suggested check ids, optionally only those naming one Claim."""
    return {item['check'] for item in result['suggestions'] if claim is None or 'urn:test:' + claim in item.get('claims', [])}


def counted(result):
    summary = result['summary']
    found = {family: value['count'] for family, value in summary['families'].items() if value['count']}
    if summary['synthesis']['count']: found['synthesis'] = summary['synthesis']['count']
    if summary['other']: found['other'] = summary['other']
    return found


class ClassificationTests(unittest.TestCase):
    def test_standard_families_and_a_gap_synthesis_conform(self):
        account = Account().factor_gene().phenotype_gene().factor_gene_set().gene_gene_set().gene_set_trait()
        result = account.gap(['factor-gene', 'gene-gene-set', 'phenotype-gene', 'factor-gene-set']).structure()
        summary = result['summary']
        self.assertEqual({family: value for family, value in summary['families'].items()},
                         {family: {'count': 1, 'conformant': 1} for family in summary['families']})
        self.assertEqual(summary['synthesis'], {'count': 1, 'coherent': 1, 'gap_relevance': 1})
        self.assertEqual((summary['other'], summary['claims'], summary['conformance_rate'], summary['issues']), (0, 6, 1.0, {}))
        self.assertEqual([item['check'] for item in result['suggestions']], ['claim-structure-summary'])
        self.assertTrue(all(item['severity'] == 'suggestion' for item in result['suggestions']))

    def test_full_iris_are_recommended_forms_and_legacy_relations_are_accepted_synonyms(self):
        full = {'factor_gene': (C10, OBO + 'RO_0002610', FACTOR), 'gene_gene_set': (SET, PROV + 'hadMember', C10),
                'phenotype_gene': (C10, BIOLINK + 'genetically_associated_with', TRAIT)}
        for family, triple in full.items():
            with self.subTest(family=family):
                result = getattr(Account(), family)(triple=triple).structure()
                self.assertEqual(result['summary']['families'][family], {'count': 1, 'conformant': 1})
        legacy = [('member-of', (C10, LEGACY + 'member-of', SET), 'gene_gene_set', {'atomic-orientation', 'atomic-predicate'}),
                  ('has-member', (SET, LEGACY + 'has-member', C10), 'gene_gene_set', {'atomic-predicate'}),
                  ('has-loading-on', (C10_UPPER, LEGACY + 'has-loading-on', FACTOR), 'factor_gene', {'atomic-predicate'}),
                  ('observed-loading', (C10_UPPER, LEGACY + 'observed-loading', FACTOR), 'factor_gene', {'atomic-predicate'}),
                  ('has-joint-loading-on', (SET, LEGACY + 'has-joint-loading-on', FACTOR), 'factor_gene_set', {'atomic-predicate'}),
                  ('observed-projection', (SET, LEGACY + 'observed-projection', FACTOR), 'factor_gene_set', {'atomic-predicate'}),
                  ('unlisted legacy', (C10, LEGACY + 'associated-with', TRAIT), 'phenotype_gene', {'atomic-predicate'}),
                  ('RO CURIE', (SET, 'RO:0002610', TRAIT), 'gene_set_trait', {'atomic-predicate'})]
        for label, triple, family, expected in legacy:
            with self.subTest(relation=label):
                result = getattr(Account(), family)(name='atom', triple=triple).structure()
                self.assertEqual(counted(result), {family: 1})
                self.assertEqual(checks(result, 'atom'), expected)
        result = Account().gene_gene_set(name='atom', triple=(C10, LEGACY + 'member-of', SET)).structure()
        predicate = next(item for item in result['suggestions'] if item['check'] == 'atomic-predicate')
        self.assertIn('prov:hadMember', predicate['repair'])

    def test_metric_and_legacy_relation_fallbacks_and_other_claims(self):
        cases = [('loading', dict(triple=None), 'factor_gene', 'factor_gene'),
                 ('joint loading', dict(triple=None), 'factor_gene_set', 'factor_gene_set'),
                 ('combined', dict(triple=None), 'phenotype_gene', 'phenotype_gene'),
                 ('beta', dict(triple=None, scores=[('beta', 'EFFECT_ESTIMATE', 1.62)]), 'gene_set_trait', 'gene_set_trait'),
                 ('unrecognized gene, loading', dict(triple=('urn:cfde:gene:SHH', 'obo:RO_0002610', FACTOR)), 'factor_gene', 'factor_gene'),
                 ('involved-in with loading', dict(triple=(C10, LEGACY + 'involved-in', FACTOR)), 'factor_gene', 'factor_gene'),
                 ('legacy member-of, unrecognized gene', dict(triple=('urn:cfde:gene:SHH', LEGACY + 'member-of', SET)), 'gene_gene_set', 'gene_gene_set')]
        for label, changes, builder, family in cases:
            with self.subTest(case=label):
                self.assertEqual(counted(getattr(Account(), builder)(**changes).structure()), {family: 1})
        others = [('KG predicate', (INS, 'biolink:interacts_with', 'HGNC.SYMBOL:GCK')),
                  ('KG disease', (INS, 'biolink:genetically_associated_with', 'MONDO:0005148')),
                  ('involvement without score', (C10, 'obo:RO_0002331', FACTOR)),
                  ('standard predicate, unrecognized endpoints', ('urn:cfde:gene:SHH', 'obo:RO_0002610', FACTOR)),
                  ('text only', None)]
        for label, triple in others:
            with self.subTest(case=label):
                self.assertEqual(counted(Account().claim('kg', 'A literature or KG statement.', triple, kind='BIOLOGICAL_INTERPRETATION').structure()),
                                 {'other': 1})
        result = Account().factor_gene().claim('narrative', 'Uses the loading.', kind='BIOLOGICAL_INTERPRETATION', derived=(), cites=['factor-gene']).structure()
        self.assertEqual(counted(result), {'factor_gene': 1, 'synthesis': 1})


class SuggestionTests(unittest.TestCase):
    def atom_checks(self, **changes):
        result = Account().factor_gene_set(name='atom', **changes).structure()
        self.assertEqual(result['summary']['families']['factor_gene_set']['conformant'], 0 if checks(result, 'atom') else 1)
        return checks(result, 'atom') & FAMILY_CHECKS

    def test_each_atomic_check(self):
        cases = [('atomic-result-kind', dict(kind='BIOLOGICAL_INTERPRETATION')),
                 ('atomic-triple', dict(triple=(SET, None, FACTOR))),
                 ('atomic-orientation', dict(triple=(FACTOR, 'obo:RO_0002610', SET))),
                 ('atomic-endpoints', dict(triple=('urn:cfde:gene_set:S', 'obo:RO_0002610', FACTOR), derived=(FILE, SET))),
                 ('atomic-endpoints', dict(triple=('dapper:GeneSet.' + 'U' * 32, 'obo:RO_0002610', FACTOR), derived=(FILE, 'dapper:GeneSet.' + 'U' * 32))),
                 ('atomic-predicate', dict(triple=(SET, LEGACY + 'observed-projection', FACTOR))),
                 ('atomic-score', dict(scores=[])),
                 ('atomic-score', dict(scores=[('joint_loading', 'SCORE', 0.198)])),
                 ('atomic-capture', dict(derived=(SET,))),
                 ('atomic-capture', dict(derived=(FILE,))),
                 ('atomic-single-fact', dict(scores=[('joint_loading', 'LOADING', 0.198), ('beta', 'EFFECT_ESTIMATE', 1.62)])),
                 ('atomic-single-fact', dict(scores=[('joint_loading', 'LOADING', 0.198), ('joint_loading', 'LOADING', 0.5)])),
                 ('atomic-value-in-statement', dict(statement='Gene set S has a joint loading on factor Factor8.'))]
        self.assertEqual(self.atom_checks(), set())
        for check, changes in cases:
            with self.subTest(check=check, changes=changes):
                self.assertEqual(self.atom_checks(**changes), {check})
        # Draft documents may cite a captured File by its package id; an untrusted package keeps the endpoint check quiet.
        result = Account().factor_gene_set(name='atom', triple=('dapper:GeneSet.' + 'U' * 32, 'obo:RO_0002610', FACTOR),
                                           derived=('urn:capture', 'dapper:GeneSet.' + 'U' * 32)).structure({'source_artifacts': {'c': {'dapper_file_id': 'urn:capture'}}})
        self.assertEqual(checks(result, 'atom') & FAMILY_CHECKS, set())

    def test_value_mentions_tolerate_rounding_but_not_coarse_numbers(self):
        self.assertTrue(mentions('loading 0.504 on Factor8', 0.5042))
        self.assertTrue(mentions('combined score -0.429', -0.429))
        self.assertTrue(mentions('beta 1.62', 1.6213))
        self.assertFalse(mentions('loading 0.5', 0.5042))
        self.assertFalse(mentions('rank 1 of 3', 0.6))
        self.assertFalse(mentions('no value', True))
        self.assertFalse(mentions('an overflowing 1e400 and 9' * 3, 0.25))

    def test_synthesis_paths_fold_gene_symbols_and_reject_disconnected_atoms(self):
        result = Account().factor_gene(gene=C10_UPPER).gene_gene_set(gene=C10).phenotype_gene(gene=C10).gap(
            ['factor-gene', 'gene-gene-set', 'phenotype-gene']).structure()
        self.assertEqual(result['summary']['synthesis'], {'count': 1, 'coherent': 1, 'gap_relevance': 1})
        self.assertFalse(checks(result, 'gap'))
        result = Account().factor_gene(gene=INS).gene_gene_set(gene=C10).gap(['factor-gene', 'gene-gene-set'], subject=INS).structure()
        self.assertEqual(result['summary']['synthesis']['coherent'], 0)
        self.assertEqual(checks(result, 'gap'), {'synthesis-path'})
        result = Account().factor_gene(gene=INS).factor_gene(name='second', gene=C10).gap(['factor-gene', 'second'], subject=INS).structure()
        self.assertEqual(checks(result, 'gap'), {'synthesis-families'})
        # The synthesis subject itself must lie on the cited path.
        result = Account().factor_gene(gene=INS).gene_gene_set(gene=INS).gap(['factor-gene', 'gene-gene-set'], subject='HGNC.SYMBOL:GCK').structure()
        self.assertEqual(checks(result, 'gap'), {'synthesis-path'})
        # A cited synthesis contributes its own atoms.
        account = Account().factor_gene().gene_gene_set().phenotype_gene()
        account.claim('involved', 'Gene set S is involved in Factor8.', (SET, LEGACY + 'involved-in', FACTOR), kind='BIOLOGICAL_INTERPRETATION',
                      derived=(), cites=['factor-gene', 'gene-gene-set'])
        result = account.gap(['involved', 'phenotype-gene']).structure()
        self.assertEqual(result['summary']['synthesis'], {'count': 2, 'coherent': 2, 'gap_relevance': 1})
        self.assertEqual(checks(result, 'involved'), {'synthesis-predicate'})
        self.assertFalse(checks(result, 'gap'))

    def test_gap_relevance_target_missing_and_text_only_forms(self):
        atoms = ['factor-gene', 'gene-gene-set']
        result = Account().factor_gene().gene_gene_set().gap(atoms, obj='dapper:KnowledgeGap.' + 'O' * 32).structure()
        self.assertEqual(checks(result, 'gap'), {'gap-target'})
        self.assertEqual(result['summary']['synthesis']['coherent'], 1)
        result = Account().factor_gene().gene_gene_set().structure()
        missing = next(item for item in result['suggestions'] if item['check'] == 'gap-relevance-missing')
        self.assertEqual(missing['where'], 'urn:test:account')
        result = Account().factor_gene().gene_gene_set().claim('text', 'Text-only gap relevance.', kind='BIOLOGICAL_INTERPRETATION', derived=(),
                                                               cites=atoms).structure()
        self.assertEqual(result['summary']['synthesis']['gap_relevance'], 1)
        self.assertNotIn('gap-relevance-missing', checks(result))
        # Without a package the selected gap is the account question.
        result = Account().factor_gene().gene_gene_set().gap(atoms, obj='dapper:KnowledgeGap.' + 'O' * 32).structure(use_package=False)
        self.assertIn('gap-target', checks(result, 'gap'))

    def test_within_fit_note_only_for_the_factors_own_trait(self):
        def gap_checks(statement, trait=TRAIT, context=None):
            account = Account().factor_gene().phenotype_gene(trait=trait)
            return checks(account.gap(['factor-gene', 'phenotype-gene'], statement=statement).structure(context), 'gap')
        self.assertEqual(gap_checks('C10orf71 links Factor8 and T2D to the gap.'), {'synthesis-within-fit'})
        self.assertEqual(gap_checks("C10orf71 links Factor8 and T2D to the gap within the factor's fit."), set())
        self.assertEqual(gap_checks('C10orf71 links Factor8 and another trait.', trait='KPN.TRAIT:0000001'), set())
        self.assertEqual(gap_checks('C10orf71 links Factor8 and T2D to the gap.', context=package(traits=False)), set())
        fitted = package(traits=False); fitted['pigean'] = {'mechanisms': {'anchor': {'dapper_id': FACTOR, 'fit': {'trait_group': 'kpn', 'phenotype': '0000398'}}}}
        self.assertEqual(gap_checks('C10orf71 links Factor8 and T2D to the gap.', context=fitted), {'synthesis-within-fit'})

    def test_gap_hints_grouping_value_free_messages_and_declared_biolink(self):
        account = Account()
        for index in range(10):
            account.factor_gene(name=f'atom-{index}', statement='PRIVATE_STATEMENT_TEXT without its value.')
        result = account.structure()
        self.assertEqual(result['suggestions'][0]['check'], 'claim-structure-summary')
        grouped = next(item for item in result['suggestions'] if item['check'] == 'atomic-value-in-statement')
        self.assertEqual((grouped['count'], len(grouped['claims']), grouped['family'], grouped['where']), (10, 8, 'factor_gene', 'urn:test:atom-0'))
        hint = next(item for item in result['suggestions'] if item['check'] == 'family-missing')
        self.assertIn('phenotype-gene', hint['message']); self.assertIn('get_pigean_gene_phenotype', hint['repair'])
        self.assertIn('get_pigean_gene_set_phenotype', hint['repair']); self.assertNotIn('factor-gene,', hint['message'])
        self.assertNotIn('PRIVATE_STATEMENT_TEXT', json.dumps(result))
        self.assertEqual(result['summary']['issues'], {'atomic-value-in-statement': 10, 'gap-relevance-missing': 1, 'family-missing': 1})
        for declared, expected in ((True, 'relation biolink:genetically_associated_with'), (False, 'relation ' + BIOLINK + 'genetically_associated_with')):
            result = Account().phenotype_gene(name='atom', triple=(C10, LEGACY + 'associated-with', TRAIT)).structure(package(biolink=declared))
            self.assertIn(expected, next(item for item in result['suggestions'] if item['check'] == 'atomic-predicate')['repair'])

    def test_trusted_claims_are_cited_but_not_counted_and_malformed_input_is_tolerated(self):
        account = Account().factor_gene().gene_gene_set().gap(['factor-gene', 'gene-gene-set'])
        context = package(); context['dapper_context']['claims'] = [account.doc['claims'][0]]
        result = account.structure(context)
        self.assertEqual(result['summary']['families']['factor_gene']['count'], 0)
        self.assertEqual(result['summary']['synthesis']['coherent'], 1)
        for document in (None, {}, {'claims': 'bad'}, {'claims': [{'id': 'x', 'proposition': ['p'], 'has_score': [{}], 'has_evidence': 'e'}],
                         'claim_scores': [{'id': 's', 'metric': ['loading']}], 'propositions': [{'id': ['p'], 'relation': {}}]},
                         {'claims': [{'id': 'x', 'proposition': 'p', 'has_evidence': ['e']}], 'evidence_items': [{'id': 'e', 'source_claims': ['x', 'x', 5]}],
                          'propositions': [{'id': 'p', 'subject_entity': ['HGNC:1'], 'relation': 7, 'object_entity': None}]}):
            with self.subTest(document=document):
                result = claim_structure(document, {'dapper_context': [], 'pigean': {'mechanisms': {'a': {'fit': {'trait_group': 'kpn', 'phenotype': '0000001'}, 'dapper_id': ['x']}}}})
                self.assertEqual(result['summary']['format'], 'reveal.claim-structure/1')

    def test_legacy_fixtures_produce_suggestions_only(self):
        bubble = json.loads((ROOT / 'data/fixtures/bubble-account-v1/scientific-account.json').read_bytes())
        result = claim_structure(bubble)
        self.assertEqual(counted(result), {'gene_gene_set': 2, 'other': 10})
        self.assertEqual(checks(result) - {'claim-structure-summary', 'gap-relevance-missing', 'family-missing'},
                         {'atomic-result-kind', 'atomic-endpoints', 'atomic-predicate', 'atomic-capture'})
        for item in result['suggestions']:
            self.assertEqual(item['severity'], 'suggestion')
            normalized = _normalized_finding(item)
            self.assertEqual((normalized['message'], normalized['rule']), (item['message'], item['check']))
            self.assertEqual(normalized.get('repair'), item.get('repair'))


    def test_shipped_examples_and_skeleton_follow_the_structure_exactly(self):
        runtime = ROOT / 'services/backend/agent-runtime'
        example = json.loads((runtime / 'authoring-examples.json').read_bytes())['documents'][0]
        skeleton = json.loads((runtime / 'authoring-skeleton.json').read_bytes())
        context = {'selection': {'knowledge_gap_id': example['knowledge_gaps'][0]['id']},
                   'dapper_context': {group: example[group] for group in ('files', 'knowledge_gaps', 'gene_sets', 'mechanisms', 'prefixes')}}
        for document, package in ((example, None), (example, context), (skeleton, None)):
            result = claim_structure(document, package)
            self.assertEqual(counted(result), {**dict.fromkeys(FAMILIES, 1), 'synthesis': 1})
            self.assertEqual(result['summary']['conformance_rate'], 1.0)
            self.assertEqual(result['summary']['synthesis'], {'count': 1, 'coherent': 1, 'gap_relevance': 1})
            self.assertEqual(checks(result), {'claim-structure-summary'})
        # The example factor's own trait is on the path, and the synthesis says so.
        self.assertEqual(factor_traits(example, context['dapper_context'], context), {example['mechanisms'][0]['id']: '0000000'})


class HostedDiagnosticsTests(unittest.TestCase):
    def test_hosted_report_keeps_claim_structure_and_suggestion_repairs(self):
        structure = Account().factor_gene(statement='Loading without the value.').structure()
        report = {'valid': True, 'counts': {'errors': 0, 'warnings': 0}, 'findings': [],
                  'advisories': [{'severity': 'advisory', 'check': 'cfde-grounding-missing', 'where': 'x', 'message': 'PRIVATE'},
                                 *structure['suggestions']], 'claim_structure': structure['summary']}
        with tempfile.TemporaryDirectory() as directory:
            value = diagnostic_response(report, output=Path(directory), filename='account-1.json')['structuredContent']
            stored = json.loads(Path(value['report']['path']).read_bytes())
        self.assertEqual(value['claim_structure'], structure['summary']); self.assertEqual(stored['claim_structure'], structure['summary'])
        suggestion = next(item for item in value['advisories'] if item['check'] == 'atomic-value-in-statement')
        self.assertEqual((suggestion['severity'], suggestion['family'], suggestion['claims']), ('suggestion', 'factor_gene', ['urn:test:factor-gene']))
        self.assertIn('State the exact value', suggestion['repair'])
        self.assertEqual(stored['advisories'], [_normalized_finding(item) for item in report['advisories']])
        self.assertNotIn('PRIVATE', json.dumps(value))


class BundleTests(unittest.TestCase):
    def test_every_linter_bundle_ships_the_module(self):
        self.assertIn('services/backend/src/reveal_backend/claim_suggestions.py', research_seed.KIT_IMPLEMENTATION)
        for module in ('box_adapter.py', 'dapper_release.py'):
            self.assertIn('claim_suggestions.py', (ROOT / 'services/backend/src/reveal_backend' / module).read_text())
        # Standard library only: the isolated linter subprocess imports it without the backend's dependencies.
        source = (ROOT / 'services/backend/src/reveal_backend/claim_suggestions.py').read_text()
        self.assertEqual({line for line in source.splitlines() if line.startswith(('import ', 'from '))}, {'import re', 'import string'})


RUNNER = '''import json, sys
sys.path.insert(0, sys.argv[1])
from reveal_backend import claim_suggestions, scientific_account_lint
if sys.argv[2] == 'off': claim_suggestions.claim_structure = lambda document, package=None: {'summary': None, 'suggestions': []}
if sys.argv[2] == 'raise':
    def fail(document, package=None): raise RuntimeError('synthetic suggestion failure')
    claim_suggestions.claim_structure = fail
print(json.dumps(scientific_account_lint._lint(*sys.argv[3:8], strict=sys.argv[8] == 'strict')))
'''


class LintIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_scientific_account_lint as science
        cls.science = science.ScientificAccountLintTests
        cls.science.setUpClass(); cls.addClassCleanup(cls.science.doClassCleanups)
        cls.temp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        draft = deepcopy(cls.science.draft); mechanism = draft['mechanisms'][0]['id']; source = draft['files'][0]['id']
        def atom(name, triple, statement, metric, kind, value):
            proposition = {'id': f'urn:test:{name}:p', 'statement': statement, 'proposition_kind': 'RESULT',
                           **dict(zip(('subject_entity', 'relation', 'object_entity'), triple))}
            evidence = {'id': f'urn:test:{name}:e', 'target_proposition': proposition['id'], 'direction': 'SUPPORTS',
                        'context': 'Captured gene-factor row /data/0.', 'explanation': 'Exact captured value.', 'was_derived_from': [source]}
            score = {'id': f'urn:test:{name}:s', 'metric': metric, 'score_kind': kind, 'value': value, 'interpretation': 'Exact captured value.'}
            # No draft ID may be contained in another: trusted minting reads an embedded ID as a reference.
            claim = {'id': f'urn:test:{name}:c', 'proposition': proposition['id'], 'statement': statement, 'direction': 'SUPPORTS', 'status': 'proposed',
                     'has_evidence': [evidence['id']], 'has_score': [score['id']], 'was_generated_by': 'urn:test:activity', 'was_attributed_to': ['urn:test:person']}
            for group, node in (('propositions', proposition), ('evidence_items', evidence), ('claim_scores', score), ('claims', claim)):
                draft.setdefault(group, []).append(node)
            draft['scientific_accounts'][0]['component_claims'].append(claim['id'])
        atom('factor-gene', ('HGNC:10848', 'obo:RO_0002610', mechanism), 'Gene SHH (HGNC:10848) has loading 0.5042 on factor Factor1.',
             'factor_value', 'LOADING', 0.5042)
        atom('phenotype-gene', ('HGNC:10848', BIOLINK + 'genetically_associated_with', 'KPN.TRAIT:0000001'),
             'Gene SHH is associated with CADinT2D in PIGEAN (combined 3.86).', 'combined', 'SCORE', 3.86)
        proposition = {'id': 'urn:test:gap:p', 'statement': 'SHH links Factor1 and CADinT2D to the selected gap.', 'proposition_kind': 'BIOLOGICAL_INTERPRETATION'}
        evidence = {'id': 'urn:test:gap:e', 'target_proposition': proposition['id'], 'direction': 'SUPPORTS', 'context': 'Both atomic SHH observations.',
                    'explanation': 'One shared gene path.', 'source_claims': ['urn:test:factor-gene:c', 'urn:test:phenotype-gene:c']}
        claim = {'id': 'urn:test:gap:c', 'proposition': proposition['id'], 'statement': 'Gap relevance.', 'direction': 'SUPPORTS', 'status': 'proposed',
                 'has_evidence': [evidence['id']], 'was_generated_by': 'urn:test:activity', 'was_attributed_to': ['urn:test:person']}
        draft['propositions'].append(proposition); draft['evidence_items'].append(evidence); draft['claims'].append(claim)
        draft['scientific_accounts'][0]['component_claims'].append(claim['id'])
        cls.structured = draft

    def lint(self, document, mode='draft', strict=False):
        from reveal_backend.scientific_account_lint import lint_scientific_account
        path = self.root / 'account.json'; path.write_bytes(canonical_json(document))
        return lint_scientific_account(path, dapper_root=self.science.release, release_lock=self.science.lock,
                                       evidence_package=self.science.package_path, mode=mode, strict=strict)

    def isolated(self, document, variant, mode, strict):
        path = self.root / 'isolated.json'; path.write_bytes(canonical_json(document))
        process = subprocess.run([sys.executable, '-I', '-B', '-c', RUNNER, str(ROOT / 'services/backend/src'), variant, str(path),
                                  str(self.science.release), str(self.science.lock), str(self.science.package_path), mode,
                                  'strict' if strict else 'normal'], capture_output=True, text=True, timeout=180)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_local_lint_reports_claim_structure_and_suggestions_only_as_advisories(self):
        report = self.lint(self.structured)
        self.assertTrue(report['valid'], report['findings'])
        summary = report['claim_structure']
        self.assertEqual((summary['atomic'], summary['synthesis']), ({'count': 2, 'conformant': 2}, {'count': 1, 'coherent': 1, 'gap_relevance': 1}))
        self.assertEqual(summary['other'], 1)
        legacy = self.lint(self.science.valid, mode='final')
        self.assertTrue(legacy['valid'], legacy['findings'])
        self.assertEqual((legacy['claim_structure']['other'], legacy['claim_structure']['atomic']['count']), (1, 0))
        for value in (report, legacy):
            self.assertEqual(value['advisories'][[item['check'] for item in value['advisories']].index('claim-structure-summary')]['severity'], 'suggestion')
            self.assertFalse([item for item in value['findings'] if item['severity'] == 'suggestion'])
            self.assertEqual(value['counts']['errors'], 0)

    def test_suggestions_never_change_validity_errors_or_warnings(self):
        broken = deepcopy(self.science.valid); broken['scientific_accounts'][0]['closing_remarks'] = ' '
        for document, mode, strict in ((self.structured, 'draft', False), (self.science.valid, 'final', True), (broken, 'final', False),
                                       (self.structured, 'final', False)):
            with self.subTest(mode=mode, strict=strict, valid=document is not broken):
                normal, off, failed = (self.isolated(document, variant, mode, strict) for variant in ('on', 'off', 'raise'))
                for other in (off, failed):
                    self.assertEqual([normal[key] for key in ('valid', 'findings', 'counts')], [other[key] for key in ('valid', 'findings', 'counts')])
                self.assertIn('claim_structure', normal); self.assertNotIn('claim_structure', off)
                self.assertEqual(failed['claim_structure'], {'format': 'reveal.claim-structure/1', 'unavailable': 'RuntimeError'})
                self.assertEqual([item for item in normal['advisories'] if item['severity'] != 'suggestion'], off['advisories'])
                self.assertEqual(failed['advisories'], off['advisories'])

    def test_hosted_lint_tool_returns_the_same_claim_structure(self):
        from reveal_backend import box_remote
        from reveal_backend.box_mcp import Ledger
        base = self.root / 'hosted'; state = base / 'state'; output = base / 'output'
        state.mkdir(parents=True); output.mkdir()
        lock = base / 'bundle/services/backend/agent-runtime/dapper-release.json'
        lock.parent.mkdir(parents=True); lock.write_bytes(self.science.lock.read_bytes())
        (state / 'runtime.json').write_bytes(canonical_json({'dapper_root': str(self.science.release), 'evidence_package': str(self.science.package_path)}))
        (output / 'account-1.json').write_bytes(canonical_json(self.structured))
        with patch.object(box_remote, 'BASE', base), patch.object(box_remote, 'STATE', state), patch.object(box_remote, 'OUTPUT', output):
            feedback = box_remote.lint_tool('account-1.json', Ledger(state / 'ledger', 'claim-structure-job', 1))
        self.assertFalse(feedback['isError'], feedback)
        value = feedback['structuredContent']
        self.assertEqual(value['claim_structure'], self.lint(self.structured)['claim_structure'])
        retained = decode(Path(value['report']['path']).read_bytes())
        self.assertEqual(retained['claim_structure'], value['claim_structure'])
        self.assertIn('family-missing', {item['check'] for item in retained['advisories'] if item['severity'] == 'suggestion'})


    def test_server_declared_prefixes_resolve_recommended_triples_through_draft_final_and_projection(self):
        """biolink, obo:RO_0002610 and prov:hadMember triples with no authored prefix map: lint, accept, project."""
        import shutil
        from types import SimpleNamespace
        from reveal_backend import acceptance, box_remote
        from reveal_backend.box_mcp import Ledger
        science = self.science; base = self.root / 'prefixes'
        declared = deepcopy(science.package)
        for prefixes in (declared['prefixes'], declared['dapper_context']['prefixes']): prefixes.update(research_seed.CLAIM_PREFIXES)
        for artifact in declared['source_artifacts'].values():
            target = base / 'package' / artifact['path']; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(science.package_path.parent / artifact['path'], target)
        package_path = base / 'package/evidence-package.json'; package_path.write_bytes(canonical_json(declared))
        draft = deepcopy(science.draft); draft.pop('prefixes')
        mechanism, source = draft['mechanisms'][0]['id'], draft['files'][0]['id']
        gene_set = declared['dapper_context']['gene_sets'][0]['id']
        for group in ('propositions', 'claims', 'evidence_items'): draft[group] = []
        draft['claim_scores'] = []; draft['scientific_accounts'][0]['component_claims'] = []
        def atom(name, triple, statement, scores=(), derived=(source,)):
            proposition = {'id': f'urn:test:{name}:p', 'statement': statement, 'proposition_kind': 'RESULT',
                           **dict(zip(('subject_entity', 'relation', 'object_entity'), triple))}
            evidence = {'id': f'urn:test:{name}:e', 'target_proposition': proposition['id'], 'direction': 'SUPPORTS',
                        'context': 'Captured gene-factor row /data/0.', 'explanation': 'Exact captured row.', 'was_derived_from': list(derived)}
            # No draft ID may be a prefix of another: trusted minting reads an embedded ID as a reference.
            claim = {'id': f'urn:test:{name}:c', 'proposition': proposition['id'], 'statement': statement, 'direction': 'SUPPORTS',
                     'status': 'proposed', 'has_evidence': [evidence['id']], 'was_generated_by': 'urn:test:activity',
                     'was_attributed_to': ['urn:test:person']}
            for index, (metric, kind, value) in enumerate(scores):
                draft['claim_scores'].append({'id': f'urn:test:{name}:s{index}', 'metric': metric, 'score_kind': kind, 'value': value,
                                              'interpretation': 'Exact captured value.'})
                claim.setdefault('has_score', []).append(f'urn:test:{name}:s{index}')
            for group, node in (('propositions', proposition), ('evidence_items', evidence), ('claims', claim)): draft[group].append(node)
            draft['scientific_accounts'][0]['component_claims'].append(claim['id'])
        atom('phenotype-gene', ('HGNC.SYMBOL:SHH', 'biolink:genetically_associated_with', 'KPN.TRAIT:0000001'),
             'Gene SHH is associated with CADinT2D in PIGEAN (combined 3.86).', [('combined', 'SCORE', 3.86), ('log_bf', 'SCORE', 2.14)])
        atom('factor-gene', ('HGNC.SYMBOL:SHH', 'obo:RO_0002610', mechanism), 'Gene SHH has loading 0.5042 on factor Factor1.',
             [('factor_value', 'LOADING', 0.5042)])
        atom('gene-gene-set', (gene_set, 'prov:hadMember', 'HGNC.SYMBOL:SHH'), 'Gene set S has member SHH.', derived=(source, gene_set))
        # Hosted draft path: the writer declares the server's prefixes before preflight and after hydration.
        state, output = base / 'state', base / 'output'; state.mkdir(); output.mkdir()
        lock = base / 'bundle/services/backend/agent-runtime/dapper-release.json'
        lock.parent.mkdir(parents=True); lock.write_bytes(science.lock.read_bytes())
        def write(filename, package):
            (state / 'runtime.json').write_bytes(canonical_json({'dapper_root': str(science.release), 'evidence_package': str(package)}))
            with patch.multiple(box_remote, BASE=base, STATE=state, OUTPUT=output, RESEARCH=None), \
                    patch.object(box_remote.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())):
                return box_remote.write_draft_tool(filename, deepcopy(draft))
        # Without the server declaration (an older package), the authored CURIEs do not resolve.
        self.assertTrue(write('account-2.json', science.package_path)['isError'])
        self.assertFalse((output / 'account-2.json').exists())
        result = write('account-1.json', package_path)
        self.assertFalse(result.get('isError'), result)
        written = decode((output / 'account-1.json').read_bytes())
        self.assertEqual({name: written['prefixes'][name] for name in research_seed.CLAIM_PREFIXES}, research_seed.CLAIM_PREFIXES)
        with patch.multiple(box_remote, BASE=base, STATE=state, OUTPUT=output):
            feedback = box_remote.lint_tool('account-1.json', Ledger(state / 'ledger', 'prefix-job', 1))
        self.assertFalse(feedback['isError'], feedback)
        families = feedback['structuredContent']['claim_structure']['families']
        self.assertEqual({name for name, value in families.items() if value['conformant']}, {'phenotype_gene', 'factor_gene', 'gene_gene_set'})
        # Trusted acceptance of the raw authored draft (still without prefixes) and the final gate.
        raw = base / 'raw.json'; raw.write_bytes(canonical_json(draft))
        with patch.object(acceptance, 'release_root', return_value=science.release), patch.object(acceptance, 'LOCK', science.lock):
            accepted, report = acceptance.assemble_account(raw, package_path, base / 'accepted.json',
                {'user_id': 'prefix-owner', 'principal_kind': 'anonymous'}, {'id': 'prefix-job'}, 1, 'box')
        self.assertTrue(report['valid'], report)
        self.assertEqual((report['mode'], report['counts']['errors'], report['claim_structure']['atomic']), ('final', 0, {'count': 3, 'conformant': 3}))
        self.assertEqual({name: accepted['prefixes'][name] for name in research_seed.CLAIM_PREFIXES}, research_seed.CLAIM_PREFIXES)
        # The public projection expands every server-declared CURIE before dropping the prefix map.
        expanded = {}
        for claim in accepted['claims']:
            projection, _ = acceptance.object_projection(accepted, claim['id'])
            self.assertNotIn('prefixes', projection['document'])
            for proposition in projection['document']['propositions']: expanded[proposition['relation']] = proposition
        self.assertEqual(set(expanded), {BIOLINK + 'genetically_associated_with', OBO + 'RO_0002610', PROV + 'hadMember'})
        self.assertEqual(expanded[BIOLINK + 'genetically_associated_with']['subject_entity'], 'https://identifiers.org/hgnc.symbol:SHH')


class AcceptanceStorageTests(unittest.TestCase):
    SUMMARY = {'format': 'reveal.claim-structure/1', 'atomic': {'count': 2, 'conformant': 1}}

    def test_record_field_only_from_a_report_summary(self):
        self.assertEqual(claim_structure_record({'claim_structure': self.SUMMARY}), {'claim_structure': self.SUMMARY})
        for report in (None, {}, {'claim_structure': None}, {'claim_structure': 'x'}):
            self.assertEqual(claim_structure_record(report), {})

    def accounts(self, repo):
        with repo.read_transaction() as tx: return [row['data'] for row in tx.list('account')]

    def test_worker_acceptance_stores_the_summary_on_the_account_record(self):
        import test_acceptance_batch as batch
        from reveal_backend import worker as W
        case = batch.AcceptanceBatchTests('test_fresh_owner'); case.setUp(); self.addCleanup(case.doCleanups)
        original = W.Worker.accept_accounts
        async def with_summary(worker, job, token, accepted, *args):
            return await original(worker, job, token, [(doc, {**report, 'claim_structure': self.SUMMARY}, path) for doc, report, path in accepted], *args)
        with patch.object(W.Worker, 'accept_accounts', with_summary):
            case.accept(case.repo, W.Worker, batch.OWNER, [case.document])
        [account] = self.accounts(case.repo)
        self.assertEqual(account['claim_structure'], self.SUMMARY)
        self.assertNotIn('claim_structure', account['summary'])

    def test_submission_commit_stores_the_summary_on_the_account_record(self):
        import test_acceptance_batch as batch
        from reveal_backend import research_execution
        case = batch.SubmissionBatchTests('test_submission_rows_match_sequential_puts'); case.setUp(); self.addCleanup(case.doCleanups)
        document = case.document; file = document['files'][0]
        operation = {'owner_user_id': batch.OWNER, 'local_work_id': 'work', 'arguments': {}, 'research_request_id': 'request'}
        prepared = {'reused': {'receipt_ids': []}, 'account_ids': [document['scientific_accounts'][0]['id']], 'reused_account_ids': [],
                    'source_records': [{'sha256': file['sha256'], 'file': file, 'storage': {'sha256': file['sha256']}}],
                    'validated': [{'document': document, 'storage': {'sha256': 'document'}, 'report': {'valid': True, 'claim_structure': self.SUMMARY}}],
                    'evidence_manifest_sha256': 'manifest', 'report': {}, 'existing_accounts': []}
        with batch.Deterministic(), case.repo.transaction() as tx: research_execution.commit_accounts(None, tx, operation, prepared)
        [account] = self.accounts(case.repo)
        self.assertEqual(account['claim_structure'], self.SUMMARY)
        self.assertNotIn('claim_structure', account['summary'])


class ReportScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('claim_structure_report', ROOT / 'scripts/claim_structure_report.py')
        cls.script = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.script)

    def test_local_files_and_read_only_database_rows(self):
        from reveal_backend.repository import Repository
        structured = Account().factor_gene().gene_gene_set().gap(['factor-gene', 'gene-gene-set']).doc
        structured['scientific_accounts'][0]['id'] = 'dapper:ScientificAccount.' + 'A' * 32
        draft, retry, paragraph = deepcopy(structured), deepcopy(structured), deepcopy(structured)
        draft['scientific_accounts'][0]['id'] = 'urn:reveal:tmp:account-1'
        retry['activities'] = [{'id': 'dapper:Activity.' + 'R' * 32}]   # a retried attempt mints the same account again
        paragraph.update(paragraphs=[{'id': 'dapper:Paragraph.' + 'P' * 32}], activities=[{'id': 'dapper:Activity.' + 'P' * 32}])
        legacy = Account().claim('kg', 'A KG statement.', kind='BIOLOGICAL_INTERPRETATION').doc
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ('job/attempt-1/output/output', 'job/attempt-2', 'failed/attempt-1/output'): (root / folder).mkdir(parents=True)
            (root / 'job/attempt-1/accepted-1.json').write_bytes(canonical_json(structured))
            (root / 'job/attempt-1/output/output/account-1.json').write_bytes(canonical_json(draft))   # its minted copy is accepted-1
            (root / 'job/attempt-1/validation-1.json').write_bytes(canonical_json({'valid': True}))
            (root / 'job/attempt-2/accepted-1.json').write_bytes(canonical_json(retry))
            (root / 'paragraph/attempt-1').mkdir(parents=True)
            (root / 'paragraph/attempt-1/accepted-paragraph.json').write_bytes(canonical_json(paragraph))
            (root / 'failed/attempt-1/output/account-1.json').write_bytes(canonical_json(legacy))   # never accepted: the draft counts
            (root / 'failed/attempt-1/output/account-2.json').write_bytes(b'not json')
            documents = list(self.script.local_documents([root], package()))
            self.assertEqual(sorted(str(Path(source).relative_to(root)) for source, _, _ in documents),
                             ['failed/attempt-1/output/account-1.json', 'job/attempt-1/accepted-1.json', 'job/attempt-2/accepted-1.json'])
            self.assertEqual([Path(source).name for source, _, _ in self.script.local_documents([root / 'job/attempt-1/output', root / 'job/attempt-1'])],
                             ['accepted-1.json'])
            self.assertEqual(list(self.script.local_documents([root / 'paragraph/attempt-1/accepted-paragraph.json'])), [])
            result = self.script.report(documents)
            total = result['total']
            self.assertEqual([Path(row['source']).relative_to(root).parts[0] for row in result['documents']], ['failed', 'job'])
            self.assertEqual((total['documents'], total['atomic'], total['synthesis'], total['other']),
                             (2, {'count': 2, 'conformant': 2}, {'count': 1, 'coherent': 1, 'gap_relevance': 1}, 1))
            self.assertEqual(total['conformance_rate'], 1.0)
            self.assertIn('gene-gene set', self.script.render(result))
            repository = Repository(str(root / 'records.sqlite')); repository.migrate()
            with repository.transaction() as tx:
                tx.put('scientific_document', 'x-not-an-account', 'owner', {'document': {'claims': []}})
                tx.put('scientific_document', 'zz-paragraph', 'owner', {'document': paragraph})
                tx.put('scientific_document', 'one', 'owner', {'document': structured}); tx.put('scientific_document', 'two', 'owner', {'document': legacy})
                tx.put('account', 'unrelated', 'owner', {'document': legacy})
            rows = list(self.script.database_documents(repository, 5))
            self.assertEqual(sorted(source for source, _, _ in rows), ['scientific_document:one', 'scientific_document:two'])
            self.assertEqual(self.script.report(rows)['total']['synthesis']['count'], 1)
            self.assertEqual(self.script.report(rows + documents)['total']['documents'], 2)   # the same accounts count once across sources
            self.assertEqual(len(list(self.script.database_documents(repository, 1))), 1)
            with patch('sys.stdout') as stdout:
                self.assertEqual(self.script.main([str(root), '--json']), 0)
            printed = json.loads(''.join(call.args[0] for call in stdout.write.call_args_list))
            self.assertEqual((printed['total']['documents'], printed['total']['synthesis']['count']), (2, 1))

if __name__ == '__main__':
    unittest.main()
