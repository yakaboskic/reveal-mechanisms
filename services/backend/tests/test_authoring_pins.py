"""Frozen v2 contract bytes win over later deployment prose; no remote clone."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.dapper_release import prepare_agent_workspace
from reveal_backend.dispatch_view import (CLAIM_STRUCTURE_PATH, KIT_V2, KIT_V3, file_input_manifest, pinned_claim_structure_sha256,
                                          pinned_contract_sha256, pinned_skeleton_sha256, research_prompt, validate_file_input)
from reveal_backend.authoring_contract import SKELETON_PATH
from reveal_backend.evidence_package import canonical_json, EvidenceBuildError, sha256
from reveal_backend.research_seed import prepare_research_seed
from reveal_backend.runtime_config import ROOT
import test_research_data as research_fixtures


class AuthoringPinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        runtime, frozen, binding = research_fixtures.SeedTests().inputs()
        self.built = prepare_research_seed(frozen, binding, dapper=runtime, project_root=ROOT,
                                           output=self.root/'seed')
        self.package = self.root/'seed/evidence-package.json'
        self.lock = ROOT/'services/backend/agent-runtime/dapper-release.json'

    def changed_runtime(self):
        original = Path.read_bytes
        changed = {ROOT/'docs/authoring-contract.md': b'A later deployment changed the scientific instructions.\n',
            ROOT/CLAIM_STRUCTURE_PATH: b'A later deployment changed the claim structure.\n',
            ROOT/'services/backend/agent-skills/construct-scientific-account/SKILL.md': b'A later deployment skill.\n',
            ROOT/'services/backend/agent-runtime/authoring-schema-excerpt.yaml': b'changed: schema\n',
            ROOT/'services/backend/agent-runtime/authoring-skeleton.json': b'{"changed":true}\n',
            ROOT/'services/backend/agent-runtime/authoring-examples.json': b'{"changed":true}\n'}
        return patch.object(Path, 'read_bytes', lambda path: changed.get(path.resolve(), original(path)))

    def test_frozen_contract_hash_generates_same_prompt_after_runtime_prose_changes(self):
        raw = self.package.read_bytes()
        manifest = file_input_manifest(raw)
        contract = pinned_contract_sha256(self.built.package)
        with self.changed_runtime():
            self.assertEqual(file_input_manifest(raw), manifest)
            prompt = research_prompt([], progressive=True, contract_sha256=contract, skeleton_sha256=pinned_skeleton_sha256(self.built.package),
                                     claim_structure_sha256=pinned_claim_structure_sha256(self.built.package))
            validate_file_input(raw, manifest, prompt)
            self.assertIn(contract, prompt)
            self.assertIn(pinned_claim_structure_sha256(self.built.package), prompt)

    def test_bootstrap_uses_exact_pinned_docs_skills_schema_and_examples(self):
        with self.changed_runtime(), patch('reveal_backend.dapper_release.clone_release', return_value={'commit':'isolated-fixture'}), \
                patch('reveal_backend.dapper_release.version', return_value='1.11.1'):
            runtime = prepare_agent_workspace(self.root/'workspace', ROOT, self.package, self.lock)
        work = Path(runtime['working_directory'])
        for entry in self.built.package['authoring_kit']['files']:
            if entry['path'].endswith('.md') or entry['path'].startswith('input/package-sections/'):
                source = self.built.package['source_artifacts'][entry['artifact_id']]
                self.assertEqual((work/entry['path']).read_bytes(), self.built.files[source['path']])
        self.assertEqual(runtime['authoring_instructions']['source'], 'frozen authoring kit')
        self.assertEqual(runtime['authoring_instructions']['updates_from_package'], [])
        pinned_skill = (work/'services/backend/agent-skills/construct-scientific-account/SKILL.md').read_text()
        self.assertEqual((work/'.claude/skills/construct-scientific-account/SKILL.md').read_text(),
                         pinned_skill.replace('../../../../docs/', '../../../docs/'))
        self.assertEqual(sha256((work/'docs/authoring-contract.md').read_bytes()), pinned_contract_sha256(self.built.package))
        self.assertEqual(sha256((work/SKELETON_PATH).read_bytes()),pinned_skeleton_sha256(self.built.package))

    def test_new_skeleton_is_instruction_only_and_prompt_requires_early_draft(self):
        import json
        from jsonschema import Draft202012Validator
        from reveal_backend.research_seed import validate_seed_shape
        validate_seed_shape(self.built.package,self.package.parent)
        schema=json.loads((ROOT/'schema/evidence-package.schema.json').read_bytes())
        authoring_schema=schema['$defs']['EPAuthoring']
        self.assertEqual(set(self.built.package['authoring']),set(authoring_schema['properties']))
        # Seeds use the existing research-seed builder pin rather than the
        # eager package's evidence-builder pin. Validate the new reference
        # against the unchanged canonical wire type, plus seed shape above.
        Draft202012Validator({'$defs':schema['$defs'],**authoring_schema['properties']['references']}).validate(self.built.package['authoring']['references'])
        pin=pinned_skeleton_sha256(self.built.package)
        source=self.built.package['source_artifacts']['authoring-skeleton']
        self.assertNotIn(source['dapper_file_id'],self.built.package['eligible_source_ids'])
        self.assertNotIn(source['dapper_file_id'],self.built.package['cfde_source_ids'])
        for progressive in (False,True):
            prompt=research_prompt([],progressive=progressive,skeleton_sha256=pin)
            self.assertIn(SKELETON_PATH,prompt);self.assertIn(pin,prompt)
            self.assertIn('first third',prompt);self.assertIn('"target_proposition"',prompt)
            self.assertIn('"proposition"',prompt);self.assertIn('"direction"',prompt)
            self.assertIn('"question"',prompt);self.assertIn('"was_derived_from"',prompt)
            self.assertNotIn('SKILL.md and relevant pinned schema only as needed',prompt)

    def test_historical_kit_does_not_receive_current_skeleton(self):
        import json
        package=self.built.package
        package['authoring']['references']=[entry for entry in package['authoring']['references'] if entry['path']!=SKELETON_PATH]
        package['authoring_kit']['files']=[entry for entry in package['authoring_kit']['files'] if entry['path']!=SKELETON_PATH]
        package['authoring_kit']['kit_sha256']=sha256(canonical_json(package['authoring_kit']['files']))
        self.package.write_bytes(canonical_json(package))
        self.assertIsNone(pinned_skeleton_sha256(package))
        with patch('reveal_backend.dapper_release.clone_release',return_value={'commit':'isolated-fixture'}),patch('reveal_backend.dapper_release.version',return_value='1.11.1'):
            runtime=prepare_agent_workspace(self.root/'historical-workspace',ROOT,self.package,self.lock)
        work=Path(runtime['working_directory'])
        self.assertFalse((work/SKELETON_PATH).exists())
        self.assertFalse((work/'services/backend/agent-runtime/authoring-skeleton.json').exists())

    def test_skeleton_reference_and_source_tampering_fail_closed(self):
        from copy import deepcopy
        for mode in ('reference','source','missing'):
            package=deepcopy(self.built.package)
            if mode=='reference':
                next(entry for entry in package['authoring']['references'] if entry['path']==SKELETON_PATH)['sha256']='f'*64
            elif mode=='source':package['source_artifacts']['authoring-skeleton']['sha256']='f'*64
            else:
                package['authoring_kit']['files']=[entry for entry in package['authoring_kit']['files'] if entry['path']!=SKELETON_PATH]
                package['authoring_kit']['kit_sha256']=sha256(canonical_json(package['authoring_kit']['files']))
            with self.subTest(mode=mode),self.assertRaises(EvidenceBuildError):pinned_skeleton_sha256(package)

    def test_historical_prompts_without_skeleton_pin_retain_exact_bytes(self):
        hashes={(False,False):'58e7a4f06df1901ea01c905776dc0a207e013613e00ddc97c9a04ae77fede4c7',
                (False,True):'2acd3f123a275648a880bdb7a89e040f6945676c4e468381cc6e6f121a673c8d',
                (True,False):'6eaf70b13d533b701122d1e5d5344fd78b3879967dc32e0e6c4053b242a24916',
                (True,True):'bf52608778488a74de11f9d014744f30594f46213e2bc355267ed87f47f108e0'}
        for (progressive,graph),expected in hashes.items():
            prompt=research_prompt(['prokn'] if graph else [],progressive=progressive,
                contract_sha256='f82a9c846c6f2681851ed9fa41f7845619d5a261dde88d46bf77460a6285c50c')
            self.assertEqual(sha256(prompt.encode()),expected)

    def frozen_kit(self, version, *, drop_claim_structure):
        """The built seed rewritten as an older (v2) or damaged frozen kit, published to its package path."""
        package = self.built.package
        if drop_claim_structure:
            for entries in (package['authoring']['references'], package['authoring_kit']['files']):
                entries[:] = [entry for entry in entries if entry['path'] != CLAIM_STRUCTURE_PATH]
        package['authoring_kit'].update(version=version, kit_sha256=sha256(canonical_json(package['authoring_kit']['files'])))
        self.package.write_bytes(canonical_json(package))
        return package

    def workspace(self, name):
        with self.changed_runtime(), patch('reveal_backend.dapper_release.clone_release', return_value={'commit':'isolated-fixture'}), \
                patch('reveal_backend.dapper_release.version', return_value='1.11.1'):
            return Path(prepare_agent_workspace(self.root/name, ROOT, self.package, self.lock)['working_directory'])

    def test_new_seed_pins_claim_structure_reference_prefixes_and_prompt(self):
        from reveal_backend.research_seed import CLAIM_PREFIXES
        package = self.built.package
        self.assertEqual(package['authoring_kit']['version'], KIT_V3)
        pin = pinned_claim_structure_sha256(package)
        self.assertEqual(pin, sha256((ROOT/CLAIM_STRUCTURE_PATH).read_bytes()))
        self.assertIn(CLAIM_STRUCTURE_PATH, [entry['path'] for entry in package['authoring']['references']])
        for prefixes in (package['prefixes'], package['dapper_context']['prefixes']):
            self.assertEqual({name: prefixes[name] for name in CLAIM_PREFIXES}, CLAIM_PREFIXES)
        skeleton = pinned_skeleton_sha256(package)
        for progressive in (False, True):
            prompt = research_prompt([], progressive=progressive, skeleton_sha256=skeleton, claim_structure_sha256=pin)
            self.assertIn(CLAIM_STRUCTURE_PATH + ' (SHA-256 ' + pin + ')', prompt)
            self.assertIn('atomic RESULT Claim: a Proposition with subject_entity, relation and object_entity', prompt)
            self.assertIn('through EvidenceItem.source_claims', prompt)
            self.assertIn('has_score referencing claim_scores', prompt)
            self.assertIn('put the exact locator in EvidenceItem.context', prompt)
            self.assertIn('Soft targets, not quotas', prompt)
            self.assertNotIn('separate source-result Claims are optional', prompt)
            self.assertNotIn('a Claim per row', prompt)
        work = self.workspace('v3-workspace')
        source = package['source_artifacts'][next(entry['artifact_id'] for entry in package['authoring_kit']['files'] if entry['path'] == CLAIM_STRUCTURE_PATH)]
        self.assertEqual((work/CLAIM_STRUCTURE_PATH).read_bytes(), self.built.files[source['path']])

    def test_v2_kit_still_installs_without_the_reference_and_keeps_exact_prompt_bytes(self):
        package = self.frozen_kit(KIT_V2, drop_claim_structure=True)
        self.assertIsNone(pinned_claim_structure_sha256(package))
        self.assertIsNotNone(pinned_skeleton_sha256(package))
        work = self.workspace('v2-workspace')
        self.assertFalse((work/CLAIM_STRUCTURE_PATH).exists())
        self.assertTrue((work/'docs/authoring-contract.md').exists())
        raw = self.package.read_bytes(); manifest = file_input_manifest(raw)
        validate_file_input(raw, manifest, research_prompt([], progressive=True, contract_sha256=pinned_contract_sha256(package),
                                                           skeleton_sha256=pinned_skeleton_sha256(package)))
        # v2 prompts with a skeleton pin, as generated before the claim-structure guidance existed.
        hashes={(False,False):'c878ac73c7f84c777cb2c0fdc6eb6aafd7a55ca5a0b6475fb4adcd74122ca972',
                (False,True):'6d1ab17f1476a4d77942b335ebc7f6760b8ce10aa4f17b15e6e2c4fa42016f18',
                (True,False):'df7bec02e93589293e79d1f06a027258daaed814fd3f7d02b374f3f131e7a727',
                (True,True):'564af0d94267c0c7c876324af51ee480b2ac0c66c4d363a9482adbd9494a0912'}
        for (progressive,graph),expected in hashes.items():
            prompt=research_prompt(['prokn'] if graph else [],progressive=progressive,skeleton_sha256='e'*64,
                contract_sha256='f82a9c846c6f2681851ed9fa41f7845619d5a261dde88d46bf77460a6285c50c')
            self.assertEqual(sha256(prompt.encode()),expected)

    def test_v3_kit_without_or_with_a_tampered_reference_fails_closed(self):
        from copy import deepcopy
        tampered = deepcopy(self.built.package)
        entry = next(item for item in tampered['authoring_kit']['files'] if item['path'] == CLAIM_STRUCTURE_PATH)
        tampered['source_artifacts'][entry['artifact_id']]['sha256'] = 'f'*64
        with self.assertRaisesRegex(EvidenceBuildError, 'source differs'): pinned_claim_structure_sha256(tampered)
        package = self.frozen_kit(KIT_V3, drop_claim_structure=True)
        with self.assertRaisesRegex(EvidenceBuildError, 'absent or ambiguous'): pinned_claim_structure_sha256(package)
        with self.assertRaisesRegex(EvidenceBuildError, 'missing required file: ' + CLAIM_STRUCTURE_PATH):
            self.workspace('broken-workspace')

    def test_tampered_contract_source_binding_fails_closed(self):
        package = self.built.package
        entry = next(item for item in package['authoring_kit']['files'] if item['path']=='docs/authoring-contract.md')
        entry['sha256'] = 'f'*64
        package['authoring_kit']['kit_sha256'] = sha256(canonical_json(package['authoring_kit']['files']))
        with self.assertRaisesRegex(EvidenceBuildError, 'source differs'):
            pinned_contract_sha256(package)

    def test_current_modes_allow_independent_evidence(self):
        for progressive in (False, True):
            prompt = research_prompt([], progressive=progressive)
            self.assertNotIn('required CFDE lineage', prompt)
            self.assertNotIn('cannot substitute for that requirement', prompt)
            self.assertIn('advisory', prompt)

    def test_hosted_reader_response_preserves_numeric_and_quote_tokens(self):
        import json
        from reveal_backend.box_mcp import Ledger, ScopedTools
        from reveal_backend.evidence_reader import read_artifact
        raw = b'{"result":{"items":[{"loading":0.10000000000000000000009,"tiny":1.2300e-40,"label":"quoted \\"word\\""}]}}'
        # Build valid quoted JSON while keeping the number lexemes untouched.
        raw = raw.replace(b'\\\\"', b'\\"')
        descriptor = {'artifact_id':'exact', 'sha256':sha256(raw), 'size_bytes':len(raw)}
        args = {'artifact_id':'exact', 'sha256':sha256(raw), 'pointer':'/result/items/0'}
        expected = read_artifact(raw, descriptor, **args)
        tools = ScopedTools((), Ledger(self.root/'ledger', 'fixture', 1),
                            read_evidence=lambda **arguments: read_artifact(raw, descriptor, **arguments))
        response = tools.call('read_evidence', args)
        self.assertEqual(response['structuredContent'], expected)
        self.assertEqual(json.loads(response['content'][0]['text']), expected)
        self.assertIn('0.10000000000000000000009', expected['content_json'])
        self.assertIn('1.2300e-40', expected['content_json'])
        self.assertEqual(tools.tool_calls, 0)
        from reveal_backend.source_validation import observation_findings
        from reveal_backend.evidence_reader import parse
        doc = {'evidence_items':[{'id':'evidence', 'was_derived_from':['source'],
                                 'context':'Exact source /result/items/0.', 'snippet':expected['content_json']}]}
        self.assertEqual(observation_findings(doc, {'source':json.loads(raw)}, exact_observed={'source':parse(raw)}), [])
        doc['evidence_items'][0]['snippet'] = expected['content_json'].replace('0.10000000000000000000009', '0.1')
        self.assertIn('evidence-snippet', {row['check'] for row in observation_findings(doc, {'source':json.loads(raw)}, exact_observed={'source':parse(raw)})})


if __name__ == '__main__': unittest.main()
