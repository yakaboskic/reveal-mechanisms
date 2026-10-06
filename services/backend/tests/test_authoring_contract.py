"""Real locked validator and shared local/hosted authoring contract regression."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import shutil
import unittest
import yaml
from reveal_backend.authoring_contract import schema_excerpt, pinned_schema, WANTED
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.scientific_account_lint import lint_scientific_account
from reveal_backend.dispatch_view import research_prompt

ROOT=Path(__file__).resolve().parents[3]
RELEASE=Path(os.environ.get('REVEAL_TEST_DAPPER_RELEASE', ROOT.parent/'dapper'))
LOCK=ROOT/'services/backend/agent-runtime/dapper-release.json'

class AuthoringContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = None
        if RELEASE.exists():
            lock=json.loads(LOCK.read_bytes())
            available = subprocess.run(['git','-C',str(RELEASE),'cat-file','-e',lock['commit']+'^{commit}'],
                                       capture_output=True)
            if available.returncode:
                return
            # Isolate local transport metadata; never edit the developer's checkout.
            cls.directory = tempfile.TemporaryDirectory()
            cls.addClassCleanup(cls.directory.cleanup)
            cls.release = Path(cls.directory.name)/'locked-release'
            subprocess.run(['git','clone','--quiet','--no-hardlinks',str(RELEASE),str(cls.release)],check=True)
            subprocess.run(['git','-C',str(cls.release),'remote','set-url','origin',lock['repository']],check=True)
            subprocess.run(['git','-C',str(cls.release),'checkout','--quiet',lock['commit']],check=True)

    def test_shared_contract_and_mode_truth(self):
        contract=(ROOT/'docs/authoring-contract.md').read_text()
        for progressive in (False,True):
            self.assertIn('docs/authoring-contract.md',research_prompt([],progressive=progressive))
            self.assertIn(sha256(contract.encode()),research_prompt([],progressive=progressive))
        for skill in ('read-evidence-package','construct-scientific-account'):
            self.assertIn('docs/authoring-contract.md',(ROOT/'services/backend/agent-skills'/skill/'SKILL.md').read_text())
        self.assertIn('retains private validation results',contract)
        self.assertIn('both TTD factors',contract)
        self.assertIn('partial top-60',contract)
        self.assertIn('perturbation target',contract)
        self.assertNotIn('evidence-records/',contract)

    def test_schema_matches_actual_locked_release_and_has_transitive_definitions(self):
        if self.release is None: self.skipTest('Set REVEAL_TEST_DAPPER_RELEASE to a checkout containing the locked release')
        raw=pinned_schema(ROOT)
        self.assertEqual(raw,schema_excerpt(self.release,LOCK))
        excerpt=yaml.safe_load(raw)
        self.assertTrue(WANTED<=set(excerpt['classes']))
        self.assertIn('ProvenancedResource',excerpt['classes'])
        self.assertIn('was_derived_from',excerpt['classes']['ProvenancedResource']['attributes'])
        self.assertIn('ClaimScoreKindEnum',excerpt['enums'])
        upstream={kind:{} for kind in ('classes','slots','types','enums')}
        for path in ('schema/dapper.yaml','schema/claims.yaml'):
            source=yaml.safe_load((self.release/path).read_bytes())
            for kind in upstream: upstream[kind].update(source.get(kind,{}) or {})
        dependency=yaml.safe_load((ROOT/'services/backend/agent-runtime/linkml-types-1.11.1.yaml').read_bytes())
        upstream['types'].update(dependency['types'])
        for kind in upstream:
            for name,definition in excerpt[kind].items():
                self.assertEqual(definition,upstream[kind][name],kind+':'+name)
        self.assertEqual(excerpt['default_range'],'string');self.assertEqual(excerpt['default_prefix'],'dapper')
        self.assertIn('uriorcurie',excerpt['types']);self.assertIn('float',excerpt['types'])
        known=set().union(*(excerpt[kind] for kind in upstream))
        def references(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    if key in ('is_a','range','typeof') and isinstance(child,str):self.assertIn(child,known)
                    if key in ('slots','mixins') and isinstance(child,list):
                        for name in child:self.assertIn(name,known)
                    references(child)
            elif isinstance(value,list):
                for child in value:references(child)
        for kind in upstream:references(excerpt[kind])
        pinned=json.loads((ROOT/'services/backend/agent-runtime/authoring-schema-dependencies.json').read_bytes())
        self.assertEqual(excerpt['external_dependencies'],pinned['dependencies'])
        for item in pinned['dependencies'].values():
            self.assertEqual(sha256((ROOT/'services/backend/agent-runtime'/item['path']).read_bytes()),item['sha256'])

    def test_complete_synthetic_examples_pass_actual_locked_draft_validator(self):
        if self.release is None: self.skipTest('Set REVEAL_TEST_DAPPER_RELEASE to a checkout containing the locked release')
        bundle=json.loads((ROOT/'services/backend/agent-runtime/authoring-examples.json').read_bytes())
        self.assertFalse(bundle['eligible_scientific_evidence'])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for index,doc in enumerate(bundle['documents']):
                path=root/'account.json';path.write_bytes(canonical_json(doc))
                sources={}
                for file in doc['files']:
                    raw=canonical_json(bundle['synthetic_sources'][file['filename']]);(root/file['filename']).write_bytes(raw)
                    sources[file['filename']]={'path':file['filename'],'sha256':sha256(raw),'size_bytes':len(raw),'format':'json','dapper_file_id':file['id']}
                context={key:doc[key] for key in ('files','knowledge_gaps','gene_sets','mechanisms')}
                package={'package_version':'reveal.evidence-package/0.2-draft','dapper_pin':{'snapshot_sha256':json.loads(LOCK.read_bytes())['compatible_input_snapshots'][0]},
                    'selection':{'knowledge_gap_id':doc['knowledge_gaps'][0]['id']},'dapper_context':context,'source_artifacts':sources,
                    'pigean':{'model':'synthetic','mechanisms':{}},
                    # Only this isolated validator fixture declares synthetic eligibility.
                    # Production seeds capture examples as instructions, never eligible evidence.
                    'validation_context':{'format':'reveal.validation-context/1','eligible_source_ids':[f['id'] for f in doc['files']]}}
                package_path=root/'evidence-package.json';package_path.write_bytes(canonical_json(package))
                result=lint_scientific_account(path,dapper_root=self.release,release_lock=LOCK,evidence_package=package_path,mode='draft')
                self.assertTrue(result['valid'],result)
                props={p['id']:p for p in doc['propositions']}; claims={c['id']:c for c in doc['claims']}
                gene_set=doc['gene_sets'][0]['id'];source=doc['files'][0]['id']
                for item in doc['evidence_items']:
                    prop=props[item['target_proposition']]
                    if gene_set in (prop.get('subject_entity'),prop.get('object_entity')):
                        self.assertIn(gene_set,item['was_derived_from']);self.assertIn(source,item['was_derived_from'])
                self.assertTrue(any(e.get('source_claims') for e in doc['evidence_items']))
