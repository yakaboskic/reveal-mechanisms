"""Synthetic relationship fixtures; no researcher account or scientific network."""
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import test_research_data as queries
import test_scientific_account_lint as science
from reveal_backend import acceptance
from reveal_backend.evidence_package import canonical_json, EvidenceBuildError
from reveal_backend.relationship_provenance import relationship_advisories, reachable_entities
from reveal_backend.scientific_account_lint import AccountValidationError, lint_scientific_account
from reveal_backend.source_validation import observation_findings


class RelationshipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.science = science.ScientificAccountLintTests
        cls.science.setUpClass(); cls.addClassCleanup(cls.science.doClassCleanups)

    def setUp(self):
        self.sql = queries.ReferenceQueryTests(); self.sql.setUp(); self.addCleanup(self.sql.doCleanups)
        self.runtime = queries.SeedTests.runtime()
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.seed = deepcopy(self.science.package)
        self.seed['reference_generation_id'] = queries.GEN
        for artifact in self.seed['source_artifacts'].values():
            target = self.root / artifact['path']; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.science.package_path.parent / artifact['path']).read_bytes())
        upstream_raw = b'SYNTHETIC\tGENE_A\n'
        self.upstream = self.runtime.file('synthetic-upstream.gmt', upstream_raw, 'text/plain')
        self.gene = 'urn:reveal:eaggl-gene:'+queries.IMP+':0'
        self.gene_set = {'name':'Synthetic perturbation signature', 'member_type':'gene', 'members':[self.gene],
                         'was_derived_from':[self.upstream['id']]}
        self.gene_set['id'] = self.runtime.compute_id(self.gene_set, 'GeneSet', self.runtime.schema)
        with sqlite3.connect(self.sql.path) as c:
            c.execute('UPDATE cfde_gene_sets SET gene_set_id=?,metadata=?', (self.gene_set['id'], json.dumps({
                'dapper_gene_set':self.gene_set, 'dapper_dependencies':[self.upstream]})))
            c.execute('UPDATE factor_gene_set_projections SET gene_set_id=?', (self.gene_set['id'],))
        upstream_path = 'sources/synthetic-upstream.gmt'
        (self.root / upstream_path).write_bytes(upstream_raw)
        self.contexts = [{'dapper_context':{'files':[self.upstream]}, 'source_artifacts':{'synthetic-upstream':{
            'path':upstream_path, 'sha256':self.upstream['sha256'], 'size_bytes':len(upstream_raw),
            'format':'text', 'origin':'fixture:synthetic-catalog', 'dapper_file_id':self.upstream['id']}},
            'files':{upstream_path:upstream_raw}}]
        self.factor_context = self.capture('get_factor', {'factor_id':queries.FACTOR})
        self.mechanism = self.factor_context['dapper_context']['mechanisms'][0]
        self.definition = self.capture('get_gene_set', {'gene_set_id':self.gene_set['id']})

    def capture(self, operation, arguments):
        context = self.sql.query(operation, arguments).materialize(self.runtime)
        self.contexts.append(context)
        for relative, raw in context['files'].items():
            target = self.root / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
        return context

    def document(self, family):
        doc = deepcopy(self.science.draft)
        doc.update(mechanisms=[], files=[], used_edges=[])
        if family == 'membership':
            context = self.capture('get_gene_set_members', {'gene_set_id':self.gene_set['id']})
            subject, relation, obj = self.gene, 'urn:reveal:relation:member-of', self.gene_set['id']
        elif family == 'gene-factor':
            context = self.capture('get_factor_loadings', {'factor_id':queries.FACTOR, 'q':'GENE_A'})
            subject, relation, obj = self.gene, 'urn:reveal:relation:observed-loading', self.mechanism['id']
        else:
            context = self.capture('get_factor_loadings', {'factor_id':queries.FACTOR, 'kind':'gene_set'})
            subject, relation, obj = self.gene_set['id'], 'urn:reveal:relation:observed-projection', self.mechanism['id']
        doc['propositions'][0].update(subject_entity=subject, relation=relation, object_entity=obj,
            statement='Synthetic fixture assesses the exact observed relationship in one pinned import.')
        doc['evidence_items'][0].update(was_derived_from=[context['dapper_file_id']] +
            ([self.gene_set['id']] if family != 'gene-factor' else []), context='Exact synthetic row /result/items/0.')
        if family != 'membership':
            doc['claim_scores'] = [{'id':'urn:test:score', 'metric':'loading' if family == 'gene-factor' else 'joint_loading',
                'value':0.8 if family == 'gene-factor' else 0.4, 'score_kind':'LOADING',
                'interpretation':'Exact stored loading in the synthetic fitted model; not a probability.'}]
            doc['claims'][0]['has_score'] = ['urn:test:score']
        return doc, context

    def package(self):
        package = acceptance.build_validation_context(self.seed, contexts=self.contexts)
        path = self.root / 'context.json'; path.write_bytes(canonical_json(package))
        return package, path

    def assemble(self, doc):
        self.assembly_count = getattr(self, 'assembly_count', 0) + 1
        package, path = self.package()
        raw = self.root / 'draft.json'; raw.write_bytes(canonical_json(doc))
        with patch.object(acceptance, 'release_root', return_value=self.science.release), patch.object(acceptance, 'LOCK', self.science.lock):
            try:
                return acceptance.assemble_account(raw, path, self.root/'assembled.json',
                    {'user_id':'isolated-fixture', 'principal_kind':'anonymous'}, {'id':'fixture-work-'+str(self.assembly_count)}, 1, 'local')
            except AccountValidationError as error:
                self.fail(error.report)

    def test_three_relationship_families_hydrate_exact_objects_and_source_paths(self):
        for family in ('membership', 'gene-factor', 'set-factor'):
            with self.subTest(family=family):
                doc, context = self.document(family)
                built, report = self.assemble(doc)
                self.assertTrue(report['valid'], report)
                evidence = built['evidence_items'][0]
                reached = reachable_entities(built, evidence['id'])
                self.assertIn(context['dapper_file_id'], reached)
                self.assertEqual(evidence['context'], 'Exact synthetic row /result/items/0.')
                if family != 'gene-factor':
                    self.assertIn(self.gene_set['id'], reached)
                    self.assertIn(self.gene_set, built['gene_sets'])
                    self.assertIn(self.upstream['id'], reached)
                    self.assertIn(self.upstream, built['files'])
                self.assertNotIn('geneset-provenance-missing', {item['check'] for item in report['advisories']})
        self.assertEqual(self.definition['object_resolution'][0]['construction_provenance_status'], 'not_loaded')

    def test_source_claim_reuse_retains_gene_set_relationship_and_exact_observation(self):
        doc, context = self.document('set-factor')
        # Mark the first assessment trusted, as an authorized reuse service does.
        first, _ = self.assemble(doc)
        self.contexts.append({'dapper_context':{key:rows for key,rows in first.items() if key in
            ('propositions','claims','evidence_items','claim_scores','persons','activities')}})
        reused = first['claims'][0]
        doc = deepcopy(self.science.draft)
        doc.update(mechanisms=[], files=[], used_edges=[])
        doc['propositions'][0] = {'id':'urn:test:synthesis', 'statement':'A scoped narrative synthesis of an existing assessment.',
                                   'proposition_kind':'BIOLOGICAL_INTERPRETATION'}
        doc['claims'][0]['proposition'] = 'urn:test:synthesis'
        doc['evidence_items'][0] = {'id':'urn:test:evidence', 'target_proposition':'urn:test:synthesis',
            'direction':'SUPPORTS', 'context':'The exact existing source Claim is reused.',
            'explanation':'The source assessment supplies the observed relationship.', 'source_claims':[reused['id']]}
        built, report = self.assemble(doc)
        self.assertTrue(report['valid'], report)
        evidence = next(item for item in built['evidence_items'] if item.get('source_claims'))
        reached = reachable_entities(built, evidence['id'])
        self.assertIn(self.gene_set['id'], reached)
        self.assertIn(self.upstream['id'], reached)
        self.assertIn(context['dapper_file_id'], reached)
        self.assertIn(reused, built['claims'])

    def test_prose_only_and_unrelated_set_do_not_supply_missing_structural_path(self):
        doc, _ = self.document('set-factor')
        proposition = doc['propositions'][0]
        for field in ('subject_entity', 'relation', 'object_entity'): proposition.pop(field)
        proposition['statement'] = 'Synthetic perturbation signature projects on the fitted mechanism.'
        doc['evidence_items'][0]['was_derived_from'] = doc['evidence_items'][0]['was_derived_from'][:1]
        other = {'name':'Unrelated synthetic set', 'member_type':'gene', 'members':['urn:test:other-gene']}
        other['id'] = self.runtime.compute_id(other, 'GeneSet', self.runtime.schema)
        self.contexts.append({'dapper_context':{'gene_sets':[other]}})
        doc['evidence_items'][0]['was_derived_from'].append(other['id'])
        package, path = self.package()
        advisories = relationship_advisories(doc, package, path)
        self.assertEqual({row['check'] for row in advisories}, {'geneset-provenance-missing','entity-reference-missing'})
        built, report = self.assemble(doc)
        strict = lint_scientific_account(self.root/'assembled.json', dapper_root=self.science.release,
            release_lock=self.science.lock, evidence_package=path, mode='final', strict=True)
        self.assertTrue(strict['valid'], strict)
        self.assertTrue(strict['advisories'])

    def test_coloading_perturbation_target_and_partial_absence_receive_scope_feedback(self):
        doc, _ = self.document('set-factor')
        doc['propositions'][0].update(subject_entity=self.gene, object_entity=self.gene_set['id'],
            relation='urn:reveal:relation:member-of')
        package, path = self.package()
        rows = relationship_advisories(doc, package, path)
        self.assertIn('relationship-support-missing', {row['check'] for row in rows})
        self.assertIn('perturbation target', next(row['message'] for row in rows if row['check']=='relationship-support-missing'))
        doc['propositions'][0]['subject_entity'] = 'urn:test:perturbation-target-not-a-signature-member'
        self.assertIn('membership-identity-unverified', {row['check'] for row in relationship_advisories(doc, package, path)})
        partial = self.capture('get_factor_loadings', {'factor_id':queries.FACTOR, 'limit':1})
        doc['evidence_items'][0]['was_derived_from'] = [partial['dapper_file_id']]
        doc['propositions'][0]['statement'] = 'The gene is absent from both factors.'
        package, path = self.package()
        self.assertIn('query-scope-overclaim', {row['check'] for row in relationship_advisories(doc, package, path)})
        doc['propositions'][0]['statement'] = 'The gene is absent from the inspected top-1 window on Factor1.'
        self.assertNotIn('query-scope-overclaim', {row['check'] for row in relationship_advisories(doc, package, path)})
        empty = self.capture('get_factor_loadings', {'factor_id':queries.FACTOR, 'q':'MISSING'})
        doc['evidence_items'][0]['was_derived_from'] = [empty['dapper_file_id']]
        doc['propositions'][0]['statement'] = 'There is no stored loading on either factor.'
        package, path = self.package()
        self.assertIn('query-scope-overclaim', {row['check'] for row in relationship_advisories(doc, package, path)})
        doc['propositions'][0]['statement'] = 'There is no stored loading in this targeted Factor1 query.'
        self.assertNotIn('query-scope-overclaim', {row['check'] for row in relationship_advisories(doc, package, path)})

    def test_changed_trusted_dependency_and_generation_fail_closed(self):
        doc, _ = self.document('membership')
        doc['gene_sets'] = [deepcopy(self.gene_set)]
        doc['gene_sets'][0]['members'] = ['urn:test:unsupported-substitution']
        with self.assertRaisesRegex(EvidenceBuildError, 'altered a trusted'):
            self.assemble(doc)
        changed = deepcopy(self.definition)
        next(iter(changed['source_artifacts'].values()))['research_capture']['generation_id'] = queries.OTHER
        with self.assertRaisesRegex(EvidenceBuildError, 'different frozen reference generation'):
            acceptance.build_validation_context(self.seed, contexts=[changed])

    def test_reused_claim_score_follows_upstream_evidence_and_preserves_loading_kind(self):
        document = {'claims':[{'id':'prior','has_evidence':['prior-evidence']},
                             {'id':'claim','has_evidence':['evidence'],'has_score':['score']}],
            'evidence_items':[{'id':'evidence','source_claims':['prior']},
                              {'id':'prior-evidence','was_derived_from':['file'],'context':'/result/items/0'}],
            'claim_scores':[{'id':'score','metric':'loading','value':0.8,'score_kind':'LOADING'}]}
        observed = {'file':{'result':{'items':[{'loading':0.8}]}}}
        self.assertEqual(observation_findings(document, observed), [])
        document['claim_scores'][0]['score_kind'] = 'PROBABILITY'
        self.assertIn('source-metric-kind', {row['check'] for row in observation_findings(document, observed)})


if __name__ == '__main__': unittest.main()
