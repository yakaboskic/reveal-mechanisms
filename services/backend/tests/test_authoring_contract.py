"""Real locked validator and shared local/hosted authoring contract regression."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import shutil
import unittest
from copy import deepcopy
from unittest.mock import patch
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

    def test_minimal_authorable_skeleton_passes_locked_lint_after_trusted_hydration(self):
        if self.release is None: self.skipTest('Set REVEAL_TEST_DAPPER_RELEASE to a checkout containing the locked release')
        from reveal_backend.acceptance import hydrate_inputs
        bundle=json.loads((ROOT/'services/backend/agent-runtime/authoring-examples.json').read_bytes())
        original=bundle['documents'][0]
        doc=json.loads((ROOT/'services/backend/agent-runtime/authoring-skeleton.json').read_bytes())
        self.assertEqual(set(doc), {'prefixes','scientific_accounts','propositions','claims','evidence_items','claim_scores'})
        self.assertEqual((len(doc['scientific_accounts']),len(doc['claims']),len(doc['claim_scores'])),(1,6,5))
        for group,rows in doc.items():
            for node in rows if isinstance(rows,list) else ():
                self.assertFalse({'was_generated_by','was_attributed_to','source_ref','source_locator','subject_proposition','required_question'} & set(node))
        evidence={item['id']:item for item in doc['evidence_items']}
        for claim in doc['claims']:
            self.assertEqual([evidence[ref]['target_proposition'] for ref in claim['has_evidence']],[claim['proposition']])
            self.assertEqual(claim['direction'],'SUPPORTS'); self.assertEqual(evidence[claim['has_evidence'][0]]['direction'],'SUPPORTS')
        # Only the isolated harness supplies actual fixture provenance and
        # exact source dependency bodies; agents never author these fields.
        for group in ('scientific_accounts','claims'):
            for node in doc[group]:
                for field in ('was_generated_by','was_attributed_to'):
                    node[field]=deepcopy(original[group][0][field])
        # The server declares the prefixes an authored draft uses; drop the skeleton's copy to exercise that.
        doc.pop('prefixes')
        trusted={node['id']:(group,node) for group in ('knowledge_gaps','gene_sets','files','activities','organizations','mechanisms') for node in original[group]}
        with patch('reveal_backend.acceptance.release_root',return_value=self.release):
            hydrated=hydrate_inputs(doc,trusted,[('used_edges',edge) for edge in original['used_edges']],
                                    {**original['prefixes'],'unused':'https://example.org/unused/'})
        self.assertEqual(hydrated['gene_sets'],original['gene_sets'])
        self.assertEqual(hydrated['files'],original['files'])
        self.assertEqual(hydrated['mechanisms'],original['mechanisms'])
        self.assertEqual(hydrated['prefixes'],original['prefixes'])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); path=root/'account.json'; path.write_bytes(canonical_json(hydrated))
            sources={}
            for file in hydrated['files']:
                raw=canonical_json(bundle['synthetic_sources'][file['filename']]); (root/file['filename']).write_bytes(raw)
                sources[file['filename']]={'path':file['filename'],'sha256':sha256(raw),'size_bytes':len(raw),'format':'json','dapper_file_id':file['id']}
            package={'package_version':'reveal.evidence-package/0.2-draft','dapper_pin':{'snapshot_sha256':json.loads(LOCK.read_bytes())['compatible_input_snapshots'][0]},
                'selection':{'knowledge_gap_id':hydrated['knowledge_gaps'][0]['id']},
                'dapper_context':{key:hydrated[key] for key in ('files','knowledge_gaps','gene_sets','mechanisms')},
                'source_artifacts':sources,'pigean':{'model':'synthetic','mechanisms':{}},
                'validation_context':{'format':'reveal.validation-context/1','eligible_source_ids':[f['id'] for f in hydrated['files']]}}
            package_path=root/'evidence-package.json';package_path.write_bytes(canonical_json(package))
            result=lint_scientific_account(path,dapper_root=self.release,release_lock=LOCK,evidence_package=package_path,mode='draft',strict=True)
            self.assertTrue(result['valid'],result)
            self.assertEqual(result['claim_structure']['conformance_rate'],1.0)

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

    def test_complete_synthetic_examples_pass_full_lint_and_trusted_acceptance(self):
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
                result=lint_scientific_account(path,dapper_root=self.release,release_lock=LOCK,evidence_package=package_path,mode='draft',strict=True)
                self.assertTrue(result['valid'],result)
                # One conformant atomic Claim per family and one coherent gap-relevance synthesis: no structure suggestion.
                summary=result['claim_structure']
                self.assertEqual({family:value['count'] for family,value in summary['families'].items()},dict.fromkeys(summary['families'],1))
                self.assertEqual((summary['atomic'],summary['synthesis'],summary['other'],summary['issues']),
                                 ({'count':5,'conformant':5},{'count':1,'coherent':1,'gap_relevance':1},0,{}))
                self.assertEqual([a['check'] for a in result['advisories'] if a['severity']=='suggestion'],['claim-structure-summary'])
                props={p['id']:p for p in doc['propositions']}; claims={c['id']:c for c in doc['claims']}
                gene_set=doc['gene_sets'][0]['id'];source=doc['files'][0]['id']
                for item in doc['evidence_items']:
                    prop=props[item['target_proposition']]
                    if gene_set in (prop.get('subject_entity'),prop.get('object_entity')):
                        if item.get('source_claims'):
                            # A synthesis reaches the GeneSet through the atomic Claims it cites.
                            cited=[props[claims[ref]['proposition']] for ref in item['source_claims']]
                            self.assertTrue(any(gene_set in (p.get('subject_entity'),p.get('object_entity')) for p in cited))
                        else:
                            self.assertIn(gene_set,item['was_derived_from']);self.assertIn(source,item['was_derived_from'])
                self.assertTrue(any(e.get('source_claims') for e in doc['evidence_items']))
                # Trusted assembly mints final identities and the final gate accepts the account.
                from reveal_backend import acceptance
                with patch.object(acceptance,'release_root',return_value=self.release):
                    accepted,final=acceptance.assemble_account(path,package_path,root/'accepted.json',
                        {'user_id':'example-owner','principal_kind':'anonymous'},{'id':'example-job'},1,'deterministic')
                self.assertTrue(final['valid'],final)
                self.assertEqual(final['mode'],'final')
                self.assertEqual(final['claim_structure']['conformance_rate'],1.0)
