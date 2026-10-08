"""Independent evidence and progressive contexts exercise the real pinned lint."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_scientific_account_lint as fixtures
from reveal_backend import acceptance, evidence_imports, user_inputs
from reveal_backend.auth import Problem
from reveal_backend.evidence_package import canonical_json, decode, EvidenceBuildError, sha256
from reveal_backend.scientific_account_lint import AccountValidationError, lint_scientific_account
from reveal_backend.source_validation import observation_findings


class ProgressiveAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.science = fixtures.ScientificAccountLintTests
        cls.science.setUpClass()
        cls.addClassCleanup(cls.science.doClassCleanups)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Existing package files remain under their verified fixture location.
        self.package = deepcopy(self.science.package)
        for artifact in self.package['source_artifacts'].values():
            source = self.science.package_path.parent / artifact['path']
            destination = self.root / artifact['path']; destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())

    def imported(self, filename='paper.txt', data=b'The independently collected observation is explicit.', metadata=None, inputs=()):
        context = evidence_imports.materialize_import(filename, data, metadata,
            dapper=acceptance.public_runtime(), inputs=inputs, import_id='test-import')
        for relative, raw in context['files'].items():
            target = self.root / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
        return context

    def test_independent_evidence_validates_in_strict_mode_with_advisory(self):
        imported = self.imported()
        package = acceptance.build_validation_context(self.package, contexts=[imported])
        package_path = self.root/'context.json'; package_path.write_bytes(canonical_json(package))
        draft = deepcopy(self.science.draft)
        draft['evidence_items'][0].update(was_derived_from=[imported['dapper_context']['files'][1]['id']],
            context='Independent source /segments/0/text.', snippet='The independently collected observation is explicit.')
        source = self.root/'draft.json'; source.write_bytes(canonical_json(draft))
        with patch.object(acceptance, 'release_root', return_value=self.science.release), patch.object(acceptance, 'LOCK', self.science.lock):
            try:
                document, report = acceptance.assemble_account(source, package_path, self.root/'accepted.json',
                    {'user_id':'owner', 'principal_kind':'anonymous'}, {'id':'local-work'}, 1, 'local')
            except AccountValidationError as error:
                self.fail(error.report)
        strict = lint_scientific_account(self.root/'accepted.json', dapper_root=self.science.release,
            release_lock=self.science.lock, evidence_package=package_path, mode='final', strict=True)
        self.assertTrue(strict['valid'], strict)
        # Claim-structure suggestions are separate optional advice (test_claim_suggestions.py).
        self.assertEqual([item['check'] for item in strict['advisories'] if item['severity'] != 'suggestion'], ['cfde-grounding-missing'])
        self.assertNotIn('cfde-grounding-missing', {item['check'] for item in strict['findings']})
        original = imported['dapper_context']['activities'][0]
        self.assertIn(original, document['activities'])
        self.assertTrue(any(node['name']=='REVEAL local account submission' for node in document['activities']))

    def test_local_derivation_requires_retained_inputs_and_keeps_distinct_activities(self):
        with self.assertRaises(Problem) as failure:
            self.imported(metadata={'origin':'locally_derived', 'method':'Analyze data', 'input_ids':['unknown']})
        self.assertEqual(failure.exception.code, 'IMPORT_DERIVATION_REQUIRED')
        file = self.package['dapper_context']['files'][0]
        context = self.imported(metadata={'origin':'locally_derived', 'method':'An unverified local transform',
            'input_ids':[file['id']], 'software':'Local script', 'parameters':{'threshold':0.5}}, inputs=[self.package])
        activities = context['dapper_context']['activities']
        self.assertEqual(len(activities), 2)
        self.assertNotEqual(activities[0]['id'], activities[1]['id'])
        self.assertNotIn('generated_at_time', activities[0])
        self.assertIn('Unverified', activities[0]['name'])

    def test_pinned_dismech_only_evidence_passes_local_lint_and_both_acceptance_modes(self):
        from test_research_data import SeedTests, ROOT
        from reveal_backend.research_seed import prepare_research_seed
        dapper, frozen, binding = SeedTests().inputs()
        record = {'id': 'dismech:fixture#/pathophysiology/0', 'name': 'Fixture mechanism',
                  'raw': {'description': 'A pinned independent mechanistic observation.'}}
        reference = {'source_id': record['id'], 'source_revision': 'f' * 64}
        frozen['linked_dismech_context'] = [reference]
        binding['pinned_dismech_context'] = [{**reference, 'source_detail': {
            'source_file': 'kb/Fixture.yaml', 'source_pointer': '/pathophysiology/0',
            'raw': record, 'payload_sha256': sha256(canonical_json(record))}}]
        built = prepare_research_seed(frozen, binding, dapper=dapper, project_root=ROOT)
        for relative, raw in built.files.items():
            path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        seed = built.package
        source_id = seed['source_artifacts']['dismech-attachment-0']['dapper_file_id']
        self.assertEqual(seed['authoring']['contract']['path'], 'docs/authoring-contract.md')
        self.assertIn('docs/evidence-package.md', [item['path'] for item in seed['authoring']['references']])
        draft = deepcopy(self.science.draft)
        draft['prefixes'] = seed['prefixes']
        draft['knowledge_gaps'] = deepcopy(seed['dapper_context']['knowledge_gaps'])
        draft['scientific_accounts'][0]['question'] = seed['selection']['knowledge_gap_id']
        draft['mechanisms'] = deepcopy(seed['dapper_context']['mechanisms'])
        draft['propositions'][0]['object_entity'] = draft['mechanisms'][0]['id']
        draft['files'] = [deepcopy(node) for node in seed['dapper_context']['files'] if node['id'] == source_id]
        draft['used_edges'][0]['object'] = source_id
        draft['evidence_items'][0].update(was_derived_from=[source_id],
            context='Pinned DisMech attachment record.', snippet=record['raw']['description'])
        path = self.root / 'draft.json'; path.write_bytes(canonical_json(draft))
        seed_path = self.root / 'evidence-package.json'
        local = lint_scientific_account(path, dapper_root=self.science.release,
            release_lock=self.science.lock, evidence_package=seed_path, mode='draft', strict=True)
        self.assertTrue(local['valid'], local)
        self.assertIn('cfde-grounding-missing', {row['check'] for row in local['advisories']})
        closure = acceptance.build_validation_context(seed)
        self.assertEqual(closure['validation_context']['eligible_source_ids'], [source_id])
        # Repeated closures, including an empty export selection, keep seed eligibility.
        self.assertEqual(acceptance.build_validation_context(closure)['validation_context']['eligible_source_ids'], [source_id])
        closure_path = self.root / 'context.json'; closure_path.write_bytes(canonical_json(closure))
        with patch.object(acceptance, 'release_root', return_value=self.science.release), patch.object(acceptance, 'LOCK', self.science.lock):
            for execution in ('local', 'hosted'):
                with self.subTest(execution=execution):
                    _, report = acceptance.assemble_account(path, closure_path, self.root / (execution + '.json'),
                        {'user_id': 'fixture-owner', 'principal_kind': 'anonymous'},
                        {'id': 'fixture-' + execution}, 1, execution)
                    self.assertTrue(report['valid'], report)

    def test_context_conflict_and_authoring_files_cannot_be_promoted(self):
        altered = deepcopy(self.package['dapper_context']['knowledge_gaps'][0]); altered['text']='Edited source'
        with self.assertRaises(EvidenceBuildError):
            acceptance.build_validation_context(self.package, contexts=[{'dapper_context':{'knowledge_gaps':[altered]}}])
        from reveal_backend.scientific_account_lint import eligible_source_files
        instruction = self.package['authoring']['skill']['artifact_id']
        file_id = self.package['source_artifacts'][instruction]['dapper_file_id']
        context = acceptance.build_validation_context(self.package, contexts=[{'eligible_source_ids':[file_id]}])
        eligible, _ = eligible_source_files(context, self.science.package_path)
        self.assertNotIn(file_id, eligible)
        # The complete authoring kit includes schema/examples/implementation
        # artifacts in addition to the legacy instruction list.
        context['eligible_source_ids'] = [file_id]
        context['authoring'] = {}
        context['authoring_kit'] = {'files': [{'artifact_id': instruction}]}
        eligible, _ = eligible_source_files(context, self.science.package_path)
        self.assertNotIn(file_id, eligible)

    def test_declared_derivation_edges_preserve_cfde_lineage_without_activity_inference(self):
        source_id=self.package['source_artifacts']['gene-factor-50fbafcddac8.attempt-1.body']['dapper_file_id']
        imported=self.imported(metadata={'origin':'locally_derived','input_ids':[source_id],
            'method':'A declared local interpretation of the retained loading.'},inputs=[self.package])
        package=acceptance.build_validation_context(self.package,contexts=[imported])
        package_path=self.root/'context.json'; package_path.write_bytes(canonical_json(package))
        draft=deepcopy(self.science.draft)
        draft['evidence_items'][0].update(was_derived_from=[imported['dapper_context']['files'][1]['id']],
            context='Declared local result at /segments/0/text.',snippet='The independently collected observation is explicit.')
        source=self.root/'draft.json'; source.write_bytes(canonical_json(draft))
        with patch.object(acceptance,'release_root',return_value=self.science.release), patch.object(acceptance,'LOCK',self.science.lock):
            _,report=acceptance.assemble_account(source,package_path,self.root/'accepted.json',
                {'user_id':'owner','principal_kind':'anonymous'},{'id':'derived-work'},1,'local')
        self.assertEqual([item for item in report['advisories'] if item['severity']!='suggestion'],[])

    def test_csv_json_rows_keep_exact_numeric_locators(self):
        extraction = user_inputs.parse_document(b'gene,beta\nABC,0.125\nDEF,-0.25\n','result.csv')
        document = {'claims':[{'id':'claim','has_evidence':['evidence'],'has_score':['score']}],
            'claim_scores':[{'id':'score','metric':'beta','value':0.125,'score_kind':'EFFECT_ESTIMATE'}],
            'evidence_items':[{'id':'evidence','was_derived_from':['file'],'context':'Exact row /data/0.'}]}
        self.assertEqual(observation_findings(document, {'file':extraction}), [])
        document['evidence_items'][0]['context']='Wrong row /data/1.'
        self.assertIn('source-metric', {item['check'] for item in observation_findings(document, {'file':extraction})})
        with self.assertRaises(ValueError):
            user_inputs.parse_document(b'gene,gene\nA,B\n','ambiguous.csv')


if __name__ == '__main__':
    unittest.main()
