"""Offline release/bootstrap tests and genuine upstream linter failure cases."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_evidence_package as fixtures
from reveal_backend.dapper_release import clone_release, prepare_agent_workspace, verify_release
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, decode, sha256
from reveal_backend.scientific_account_lint import AccountValidationError, lint_scientific_account, validate_scientific_account

ROOT = fixtures.ROOT


class ScientificAccountLintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        fixtures.EvidencePackageTests.setUpClass(); cls.addClassCleanup(fixtures.EvidencePackageTests.tearDownClass)
        cls.package = fixtures.EvidencePackageTests.built.package
        cls.package_path = fixtures.EvidencePackageTests.root / 'capture/package/evidence-package.json'
        # A local test release uses the vendored schema; no network or sibling checkout is required.
        cls.origin = cls.root / 'origin'
        shutil.copytree(ROOT / 'data/dapper/2026-09-24-v8/snapshot/schema', cls.origin / 'schema')
        for cache in cls.origin.rglob('__pycache__'): shutil.rmtree(cache)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(cls.origin), *args], text=True, stderr=subprocess.DEVNULL).strip()
        git('init'); git('add', 'schema')
        git('-c', 'user.name=REVEAL test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Local linter test fixture')
        git('-c', 'user.name=REVEAL test', '-c', 'user.email=test@example.invalid', 'tag', '-a', 'test-a1', '-m', 'Local test release')
        files = {str(p.relative_to(cls.origin)): sha256(p.read_bytes()) for p in (cls.origin / 'schema').rglob('*')
                 if p.is_file() and p.suffix in ('.py', '.yaml', '.yml', '.json')}
        lock = {'lock_version': 'reveal.dapper-release/1', 'repository': str(cls.origin), 'tag': 'test-a1',
                'tag_object': git('rev-parse', 'refs/tags/test-a1'), 'commit': git('rev-parse', 'HEAD'), 'files': files,
                'compatible_input_snapshots': [cls.package['dapper_pin']['snapshot_sha256']]}
        cls.lock = cls.root / 'release.json'; cls.lock.write_bytes(canonical_json(lock))
        cls.release = cls.root / 'runtime'; clone_release(cls.release, cls.lock)
        gap = cls.package['dapper_context']['knowledge_gaps'][0]
        mech_id = cls.package['pigean']['mechanisms'][fixtures.FACTOR]['dapper_id']
        mech = next(n for n in cls.package['dapper_context']['mechanisms'] if n['id'] == mech_id)
        source_id = cls.package['source_artifacts']['gene-factor-50fbafcddac8.attempt-1.body']['dapper_file_id']
        source = next(n for n in cls.package['dapper_context']['files'] if n['id'] == source_id)
        cls.draft = {'prefixes': cls.package['prefixes'], 'knowledge_gaps': [gap], 'mechanisms': [mech], 'files': [source],
            'persons': [{'id': 'urn:test:person', 'given_name': 'Test', 'family_name': 'Author'}],
            'activities': [{'id': 'urn:test:activity', 'name': 'Offline validation fixture', 'command': 'test-fixture'}],
            'propositions': [{'id': 'urn:test:proposition', 'statement': 'Test interpretation of SHH involvement in the selected mechanism.',
                              'proposition_kind': 'BIOLOGICAL_INTERPRETATION', 'subject_entity': 'urn:cfde:gene:SHH',
                              'relation': 'urn:test:involved-in', 'object_entity': mech_id}],
            'evidence_items': [{'id': 'urn:test:evidence', 'target_proposition': 'urn:test:proposition', 'direction': 'SUPPORTS',
                                'context': 'Test fixture: source /data/0 in the captured gene-factor response.',
                                'explanation': 'The observed loading informs this test interpretation.', 'was_derived_from': [source_id]}],
            'claims': [{'id': 'urn:test:claim', 'proposition': 'urn:test:proposition', 'statement': 'Test assessment using the captured loading.',
                        'direction': 'SUPPORTS', 'status': 'proposed', 'has_evidence': ['urn:test:evidence'],
                        'was_generated_by': 'urn:test:activity', 'was_attributed_to': ['urn:test:person']}],
            'scientific_accounts': [{'id': 'urn:test:account', 'name': 'Offline account validation fixture', 'question': gap['id'],
                                     'context': 'Offline test of account structure with one source-backed interpretation.',
                                     'component_claims': ['urn:test:claim'], 'closing_remarks': 'This test account models a partial interpretation, not a resolved causal gap.',
                                     'was_generated_by': 'urn:test:activity', 'was_attributed_to': ['urn:test:person']}],
            'used_edges': [{'subject': 'urn:test:activity', 'predicate': 'prov:used', 'object': source_id}]}
        cls.document = cls.root / 'account.json'; cls.document.write_bytes(canonical_json(cls.draft))
        cls.mint(cls.document)
        cls.valid = decode(cls.document.read_bytes())

    @classmethod
    def mint(cls, path):
        program = """import json,sys
from pathlib import Path
schema=Path(sys.argv[1])/'schema'
sys.path[:0]=[str(schema/'identity'),str(schema)]
from dapper_identity import assign_ids,load_schema
p=Path(sys.argv[2]); doc=json.loads(p.read_bytes())
assign_ids(doc,load_schema(schema/'dapper.yaml'))
p.write_text(json.dumps(doc))
"""
        subprocess.run([sys.executable, '-I', '-B', '-c', program, str(cls.release), str(path)], check=True, capture_output=True)

    def lint(self, document=None, mode='final', strict=False):
        path = self.root / 'current.json'; path.write_bytes(canonical_json(document if document is not None else self.valid))
        return lint_scientific_account(path, dapper_root=self.release, release_lock=self.lock,
                                       evidence_package=self.package_path, mode=mode, strict=strict)

    def test_valid_final_and_backend_share_the_linter(self):
        report = self.lint()
        self.assertTrue(report['valid'], report)
        self.assertFalse(report['scientific_grounding_evaluated'])
        result = validate_scientific_account(self.document, dapper_root=self.release, release_lock=self.lock,
                                             evidence_package=self.package_path)
        self.assertEqual(result['findings'], report['findings'])
        self.assertEqual(result['dapper_release']['commit'], decode(self.lock.read_bytes())['commit'])

    def test_draft_temporary_ids_are_not_final_ids(self):
        draft = self.lint(self.draft, mode='draft')
        self.assertTrue(draft['valid'], draft)
        final = self.lint(self.draft)
        self.assertFalse(final['valid'])
        self.assertIn('final-identity', {f['check'] for f in final['findings']})

    def test_upstream_rejects_unknown_fields_stale_ids_and_multiple_accounts(self):
        for case in ['field', 'digest', 'count']:
            document = deepcopy(self.valid)
            if case == 'field': document['claims'][0]['invented_field'] = 1
            elif case == 'digest': document['claims'][0]['statement'] = 'Changed without minting'
            else: document['scientific_accounts'].append(deepcopy(document['scientific_accounts'][0]))
            with self.subTest(case=case):
                result = self.lint(document); self.assertFalse(result['valid'], result)
                self.assertFalse(result.get('operational_error'), result)

    def test_reveal_rejects_wrong_gap_modified_trusted_input_and_no_synthesis(self):
        for case, check in [('gap', 'selected-gap'), ('trusted', 'trusted-input'), ('synthesis', 'account-synthesis')]:
            document = deepcopy(self.valid)
            if case == 'gap': document['scientific_accounts'][0]['question'] = document['mechanisms'][0]['id']
            elif case == 'trusted': document['knowledge_gaps'][0]['text'] = 'Changed source gap'
            else: document['scientific_accounts'][0]['closing_remarks'] = ' '
            with self.subTest(case=case):
                result = self.lint(document)
                self.assertIn(check, {f['check'] for f in result['findings']})

    def test_activity_input_alone_is_not_claim_evidence(self):
        document = deepcopy(self.valid); document['evidence_items'][0]['was_derived_from'] = []
        result = self.lint(document)
        self.assertIn('cfde-ancestry', {f['check'] for f in result['findings']})

    def test_paper_only_lineage_does_not_replace_captured_cfde_evidence(self):
        document = deepcopy(self.draft)
        paper_id = 'urn:test:captured-paper-response'
        document['files'].append({'id': paper_id, 'filename': 'paper-response.json',
                                 'mime_type': 'application/json', 'sha256': 'a' * 64, 'size_in_bytes': 120})
        document['evidence_items'][0]['was_derived_from'] = [paper_id]
        document['evidence_items'][0]['context'] = 'Captured paper abstract at /structuredContent/data/text.'
        path = self.root / 'paper-only.json'; path.write_bytes(canonical_json(document)); self.mint(path)
        result = self.lint(decode(path.read_bytes()))
        self.assertIn('cfde-ancestry', {f['check'] for f in result['findings']})
        self.assertFalse(result['valid'])

    def test_missing_or_mistargeted_evidence_is_rejected(self):
        document = deepcopy(self.valid); document['claims'][0]['has_evidence'] = []
        result = self.lint(document)
        self.assertIn('claim-evidence', {f['check'] for f in result['findings']})
        document = deepcopy(self.valid); document['evidence_items'][0]['target_proposition'] = document['knowledge_gaps'][0]['id']
        result = self.lint(document)
        self.assertIn('evidence-target', {f['check'] for f in result['findings']})

    def test_backend_rejects_invalid_account_and_retains_report(self):
        path = self.root / 'unminted.json'; path.write_bytes(canonical_json(self.draft))
        with self.assertRaises(AccountValidationError) as context:
            validate_scientific_account(path, dapper_root=self.release, release_lock=self.lock, evidence_package=self.package_path)
        self.assertFalse(context.exception.report['valid'])

    def test_changed_release_lock_fails_before_upstream_import(self):
        bad = decode(self.lock.read_bytes()); bad['commit'] = '0' * 40
        path = self.root / 'wrong-lock.json'; path.write_bytes(canonical_json(bad))
        result = lint_scientific_account(self.document, dapper_root=self.release, release_lock=path, evidence_package=self.package_path)
        self.assertTrue(result['operational_error']); self.assertFalse(result['valid'])

    def test_bootstrap_clones_bundles_and_rejects_reuse(self):
        workspace = self.root / 'agent'
        runtime = prepare_agent_workspace(workspace, ROOT, self.package_path, self.lock)
        self.assertTrue((workspace / 'dapper/.git').is_dir())
        self.assertEqual(verify_release(workspace / 'dapper', self.lock)['commit'], runtime['dapper']['commit'])
        script = workspace / 'reveal/scripts/lint_scientific_account.py'
        run = subprocess.run([sys.executable, str(script), str(self.document), '--dapper-root', runtime['dapper_root'],
                              '--evidence-package', runtime['evidence_package'], '--mode', 'final'], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        self.assertTrue(decode(run.stdout.encode())['valid'])
        skill = (workspace / 'reveal/.claude/skills/construct-scientific-account/SKILL.md').read_text()
        self.assertIn('../../../docs/scientific-account-linting.md', skill)
        with self.assertRaisesRegex(EvidenceBuildError, 'workspace already exists'):
            prepare_agent_workspace(workspace, ROOT, self.package_path, self.lock)

    def test_bootstrap_failure_leaves_no_ready_runtime(self):
        bad = decode(self.lock.read_bytes()); bad['files']['schema/dapper.yaml'] = '0' * 64
        lock = self.root / 'bad-checksum.json'; lock.write_bytes(canonical_json(bad))
        workspace = self.root / 'failed-agent'
        with self.assertRaises(EvidenceBuildError): prepare_agent_workspace(workspace, ROOT, self.package_path, lock)
        self.assertFalse((workspace / 'runtime.json').exists())


if __name__ == '__main__':
    unittest.main()
