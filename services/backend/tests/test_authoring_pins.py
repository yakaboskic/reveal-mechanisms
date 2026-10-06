"""Frozen v2 contract bytes win over later deployment prose; no remote clone."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.dapper_release import prepare_agent_workspace
from reveal_backend.dispatch_view import file_input_manifest, pinned_contract_sha256, research_prompt, validate_file_input
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
            ROOT/'services/backend/agent-skills/construct-scientific-account/SKILL.md': b'A later deployment skill.\n',
            ROOT/'services/backend/agent-runtime/authoring-schema-excerpt.yaml': b'changed: schema\n',
            ROOT/'services/backend/agent-runtime/authoring-examples.json': b'{"changed":true}\n'}
        return patch.object(Path, 'read_bytes', lambda path: changed.get(path.resolve(), original(path)))

    def test_frozen_contract_hash_generates_same_prompt_after_runtime_prose_changes(self):
        raw = self.package.read_bytes()
        manifest = file_input_manifest(raw)
        contract = pinned_contract_sha256(self.built.package)
        with self.changed_runtime():
            self.assertEqual(file_input_manifest(raw), manifest)
            prompt = research_prompt([], progressive=True, contract_sha256=contract)
            validate_file_input(raw, manifest, prompt)
            self.assertIn(contract, prompt)

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
