"""Offline release/bootstrap tests and genuine upstream linter failure cases."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_evidence_package as fixtures
from reveal_backend.dapper_release import clone_release, prepare_agent_workspace, verify_release
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, decode, sha256
from reveal_backend.scientific_account_lint import AccountValidationError, cfde_source_files, draft_id_collisions, lint_scientific_account, validate_scientific_account
from reveal_backend.runtime_config import CURRENT_DAPPER_SNAPSHOT

ROOT = fixtures.ROOT


class ReferenceSourceLineageTests(unittest.TestCase):
    def test_sql_sources_require_verified_generation_and_capture_provenance(self):
        generation = 'a' * 64
        package = {'pigean': {'model': 'eaggl-capped-v1', 'mechanisms': {'factor': {'fit': {'upstream_build': generation}}}},
                   'source_artifacts': {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def add(key, *, kind='mysql', names=('eaggl_gene_loadings',), **changes):
                body = {'format': 'reveal.reference-evidence.' + ('mysql-capture/1' if kind == 'mysql' else 'derived-capture/1'),
                        'generation_id': generation, 'model': 'eaggl-capped-v1',
                        'source': {'kind': kind, 'tables' if kind == 'mysql' else 'derived_from': list(names)}, 'data': []}
                body.update(changes)
                raw = canonical_json(body); (root / key).write_bytes(raw)
                package['source_artifacts'][key] = {'path': key, 'format': 'json', 'sha256': sha256(raw),
                    'origin': f"{kind}:{'+'.join(names)}?generation_id={generation}", 'dapper_file_id': 'file:' + key}
            add('loading')
            add('connections', kind='mysql-derived', names=('loading',))
            add('contextual', kind='mysql-derived', names=('connections',))
            add('other-generation', generation_id='b' * 64)
            add('other-model', model='other-model')
            add('dismech', names=('dismech_documents',))
            add('generation-metadata', names=('reference_generations',))
            add('instructions', format='reveal.instructions/1')
            add('mixed-derived', kind='mysql-derived', names=('loading', 'instructions'))
            add('missing-derived', kind='mysql-derived', names=('missing',))
            add('cyclic-derived', kind='mysql-derived', names=('cyclic-derived',))
            add('wrong-origin')
            package['source_artifacts']['wrong-origin']['origin'] = 'mysql:other?generation_id=' + generation
            self.assertEqual(cfde_source_files(package, root / 'package.json'),
                             {'file:loading', 'file:connections', 'file:contextual'})
            package['pigean']['model'] = 'cfde-inc-v2'
            self.assertEqual(cfde_source_files(package, root / 'package.json'), set())
            package['pigean']['model'] = 'eaggl-capped-v1'
            (root / 'loading').write_text('{}')
            with self.assertRaisesRegex(EvidenceBuildError, 'checksum changed'):
                cfde_source_files(package, root / 'package.json')

    def test_release_captures_bind_to_the_release_tables_and_id(self):
        release = 'a' * 64
        package = {'pigean': {'model': 'eaggl-capped-v1', 'mechanisms': {'factor': {'fit': {'upstream_build': release}}}},
                   'source_artifacts': {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def add(key, *, kind='mysql', names=('reveal_ref_factor_genes',), field='release_id', version='2', **changes):
                body = {'format': f"reveal.reference-evidence.{'mysql-capture' if kind == 'mysql' else 'derived-capture'}/{version}",
                        field: release, 'model': 'eaggl-capped-v1',
                        'source': {'kind': kind, 'tables' if kind == 'mysql' else 'derived_from': list(names)}, 'data': [], **changes}
                raw = canonical_json(body); (root / key).write_bytes(raw)
                package['source_artifacts'][key] = {'path': key, 'format': 'json', 'sha256': sha256(raw),
                    'origin': f"{kind}:{'+'.join(names)}?{field}={release}", 'dapper_file_id': 'file:' + key}
            add('release', names=('reveal_ref_release',))
            add('betas', names=('reveal_ref_trait_gene_sets', 'reveal_ref_phenotypes'))
            add('links', names=('reveal_ref_factor_trait_links',))
            add('derived', kind='mysql-derived', names=('release', 'betas'))
            add('generation-keyed', field='generation_id')
            add('generation-tables', names=('eaggl_gene_loadings',))
            add('dapper-tables', names=('reveal_ref_dapper_nodes',))
            add('other-release', release_id='b' * 64)
            self.assertEqual(cfde_source_files(package, root / 'package.json'),
                             {'file:release', 'file:betas', 'file:links', 'file:derived'})


class DraftIdCollisionTests(unittest.TestCase):
    def test_unminted_ids_inside_longer_node_ids_collide_like_dapper_references(self):
        digest = 'A' * 31 + '-'
        base = 'urn:reveal:local:w:d:'
        ids = [base + 'claim-12', base + 'claim-1', base + 'claim-10', base + 'claim-2', base + 'evidence-01', base + 'evidence-10',
               'dapper:GeneSet.' + digest, 'urn:x:gene-dapper:GeneSet.' + digest, 'urn:x:gap:claim', 'urn:x:gap', None, 7]
        self.assertEqual(draft_id_collisions(ids), {'urn:x:gap': ['urn:x:gap:claim'],
                                                    base + 'claim-1': [base + 'claim-10', base + 'claim-12']})
        # One width per kind, minted ids and repeated ids never collide.
        self.assertEqual(draft_id_collisions([base + f'claim-{n:02}' for n in range(1, 31)] + [base + 'claim-01']), {})
        self.assertEqual(draft_id_collisions({'dapper:Claim.' + digest: 1, 'urn:x:about-dapper:Claim.' + digest: 2}), {})
        for name in ('authoring-skeleton.json', 'authoring-examples.json'):
            data = json.loads((ROOT / 'services/backend/agent-runtime' / name).read_bytes())
            for document in data.get('documents', [data]):
                self.assertEqual(draft_id_collisions(node['id'] for rows in document.values() if isinstance(rows, list)
                                                     for node in rows if isinstance(node, dict) and 'id' in node), {})


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
        shutil.copytree(CURRENT_DAPPER_SNAPSHOT / 'snapshot/schema', cls.origin / 'schema')
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

    def test_reference_sql_capture_passes_draft_lint_and_final_assembly(self):
        from reveal_backend import acceptance
        from test_reference_evidence import ReferenceEvidenceTests
        ReferenceEvidenceTests.setUpClass()
        try:
            package = ReferenceEvidenceTests.single.package
            package_path = ReferenceEvidenceTests.root / 'single/package/evidence-package.json'
            mechanism_id = next(iter(package['pigean']['mechanisms'].values()))['dapper_id']
            mechanism = next(node for node in package['dapper_context']['mechanisms'] if node['id'] == mechanism_id)
            trusted = {node['id']: (group, node) for group, rows in package['dapper_context'].items()
                       if isinstance(rows, list) for node in rows if isinstance(node, dict) and 'id' in node}
            keys = [next(key for key in package['source_artifacts'] if key.startswith('gene-factor-')), 'connections-gene']
            for key in keys:
                with self.subTest(source=key):
                    source = package['source_artifacts'][key]
                    capture = decode((package_path.parent / source['path']).read_bytes())
                    document = deepcopy(self.draft)
                    document['prefixes'] = package['prefixes']
                    document['knowledge_gaps'] = [deepcopy(trusted[package['selection']['knowledge_gap_id']][1])]
                    document['scientific_accounts'][0]['question'] = package['selection']['knowledge_gap_id']
                    document['mechanisms'] = [deepcopy(mechanism)]
                    document['propositions'][0]['object_entity'] = mechanism_id
                    document['files'] = [deepcopy(trusted[source['dapper_file_id']][1])]
                    document['used_edges'][0]['object'] = source['dapper_file_id']
                    evidence = document['evidence_items'][0]
                    evidence['was_derived_from'] = [source['dapper_file_id']]
                    locator, observation = ('/data/0', capture['data'][0]) if 'data' in capture else ('/response/candidates/0', capture['response']['candidates'][0])
                    evidence.update(context=f'Captured observation `{locator}`.', snippet=json.dumps(observation))
                    with patch.object(acceptance, 'release_root', return_value=self.release), patch.object(acceptance, 'LOCK', self.lock):
                        document = acceptance.hydrate_inputs(document, trusted)
                        raw = self.root / 'sql-account.json'; raw.write_bytes(canonical_json(document))
                        report = lint_scientific_account(raw, dapper_root=self.release, release_lock=self.lock,
                                                         evidence_package=package_path, mode='draft')
                        self.assertTrue(report['valid'], report)
                        _, final = acceptance.assemble_account(raw, package_path, self.root / 'sql-assembled.json',
                            {'user_id': 'sql-owner', 'principal_kind': 'anonymous'}, {'id': 'sql-job'}, 1, 'box')
                    self.assertTrue(final['valid'], final)
        finally:
            ReferenceEvidenceTests.tearDownClass()

    def test_distinct_claims_share_source_without_collapsing_assessments(self):
        # Real captured CFDE rows support separate scoped involvement drafts.
        # This checks structure/source fidelity, not biological acceptance.
        document = deepcopy(self.draft)
        source = self.package['source_artifacts']['gene-factor-50fbafcddac8.attempt-1.body']
        rows = decode((self.package_path.parent / source['path']).read_bytes())['data'][:2]
        document['propositions'], document['claims'], document['evidence_items'] = [], [], []
        for index, row in enumerate(rows):
            proposition, claim, evidence = (deepcopy(self.draft[group][0])
                for group in ('propositions', 'claims', 'evidence_items'))
            proposition.update(id=f'urn:test:proposition-{index}', subject_entity='urn:cfde:gene:'+row['gene'],
                statement=f"{row['gene']} is a candidate for involvement in the selected mechanism within the retained trait model.")
            claim.update(id=f'urn:test:claim-{index}', proposition=proposition['id'],
                has_evidence=[f'urn:test:evidence-{index}'],
                statement=f"The retained loading for {row['gene']} motivates a scoped involvement hypothesis; causal direction is unresolved.")
            evidence.update(id=f'urn:test:evidence-{index}', target_proposition=proposition['id'],
                context=f'Captured gene-factor source row `/data/{index}`.', snippet=json.dumps(row),
                explanation=f"{row['gene']}'s observed loading informs this scoped hypothesis, without establishing regulatory direction.")
            for group, node in (('propositions', proposition), ('claims', claim), ('evidence_items', evidence)):
                document[group].append(node)
        account = document['scientific_accounts'][0]
        account.update(component_claims=[claim['id'] for claim in document['claims']],
            context='Two distinct involvement assessments in a shared trait model address candidate selection; neither establishes the causal direction in the selected question.',
            closing_remarks='The separate involvement assessments identify candidates within the retained model. Their causal roles remain unresolved.')
        self.assertTrue(self.lint(document, mode='draft')['valid'])
        path = self.root / 'multi-claim.json'; path.write_bytes(canonical_json(document)); self.mint(path)
        minted = decode(path.read_bytes())
        self.assertTrue(self.lint(minted)['valid'])
        self.assertEqual(minted['scientific_accounts'][0]['component_claims'], [claim['id'] for claim in minted['claims']])
        self.assertEqual(len({claim['proposition'] for claim in minted['claims']}), 2)
        self.assertEqual(minted['evidence_items'][0]['was_derived_from'], minted['evidence_items'][1]['was_derived_from'])
        # A valid first Claim cannot cover a second Claim's missing lineage or
        # a shared EvidenceItem targeting the wrong Proposition.
        for defect in ('target', 'lineage'):
            broken = deepcopy(document)
            if defect == 'target': broken['claims'][1]['has_evidence'] = [broken['evidence_items'][0]['id']]
            else: broken['evidence_items'][1].pop('was_derived_from')
            findings = {item['check'] for item in self.lint(broken, mode='draft')['findings']}
            self.assertIn('evidence-target' if defect == 'target' else 'source-ancestry', findings)

    def test_assembly_does_not_turn_prose_mentions_into_orphan_nodes(self):
        from reveal_backend import acceptance
        document=deepcopy(self.draft)
        mechanism=document.pop('mechanisms')[0]
        document['propositions'][0]['object_entity']='urn:cfde:trait:lymphocyte-count'
        # Reproduce the failed job: a valid account names an input ID only in
        # narrative context. Final assembly must not invent a graph dependency.
        document['scientific_accounts'][0]['context']='Considered mechanism ('+mechanism['id']+').'
        self.assertTrue(self.lint(document,mode='draft')['valid'])
        raw=self.root/'prose-mention.json'; raw.write_bytes(canonical_json(document))
        with patch.object(acceptance,'release_root',return_value=self.release), patch.object(acceptance,'LOCK',self.lock):
            assembled,report=acceptance.assemble_account(raw,self.package_path,self.root/'prose-assembled.json',
                {'user_id':'test-owner','principal_kind':'anonymous'}, {'id':'prose-hydration'},1,'box')
        self.assertTrue(report['valid'],report)
        self.assertNotIn('mechanisms',assembled)
        self.assertEqual(assembled['scientific_accounts'][0]['context'],document['scientific_accounts'][0]['context'])

    def test_hydration_follows_declared_links_and_transitive_inputs_only(self):
        from reveal_backend import acceptance
        document=deepcopy(self.draft)
        trusted={node['id']:(group,node) for group,rows in self.package['dapper_context'].items()
                 if isinstance(rows,list) for node in rows if isinstance(node,dict) and 'id' in node}
        mechanism=document.pop('mechanisms')[0]
        source=document.pop('files')[0]
        # A literal equal to a known ID is still a literal, not an implicit link.
        gap=document['knowledge_gaps'][0]
        literal_only={'scientific_accounts':[{'id':'urn:test:account','context':gap['id']}]}
        expected_literal=deepcopy(literal_only)
        with patch.object(acceptance,'release_root',return_value=self.release), patch.object(acceptance,'LOCK',self.lock):
            hydrated=acceptance.hydrate_inputs(document,trusted)
            self.assertEqual(acceptance.hydrate_inputs(literal_only,trusted),expected_literal)
        self.assertIn(mechanism,hydrated['mechanisms'])
        self.assertIn(source,hydrated['files'])
        self.assertTrue(all(node==trusted[node['id']][1] for group in ('mechanisms','files') for node in hydrated[group]))

    def test_source_fidelity_findings_are_identical_in_draft_and_final(self):
        source_id = self.draft['files'][0]['id']
        artifact = next(value for value in self.package['source_artifacts'].values() if value['dapper_file_id'] == source_id)
        row = decode((self.package_path.parent / artifact['path']).read_bytes())['data'][0]
        for snippet, context, expected in (
                ('result_key: "derived-field", normalized_score: 0.5795', 'Source /data/0.', 'evidence-snippet'),
                (json.dumps(row), 'Source /data/999.', 'source-locator'),
                (json.dumps(row), 'Source /data/0.', None)):
            with self.subTest(expected=expected):
                document = deepcopy(self.draft)
                document['evidence_items'][0].update(snippet=snippet, context=context)
                path = self.root / 'source-parity.json'; path.write_bytes(canonical_json(document)); self.mint(path)
                document = decode(path.read_bytes())
                draft = self.lint(document, mode='draft')
                final = self.lint(document, mode='final')
                self.assertEqual(draft['findings'], final['findings'])
                self.assertEqual(draft['valid'], expected is None, draft)
                if expected:
                    self.assertIn(expected, {item['check'] for item in draft['findings']})

    def test_literature_account_job_drafts_lints_assembles_and_takes_its_deposit_form(self):
        """A scientific_account agent job end to end, offline: the agent's tools (draft, lint, with the quote check),
        the backend's gate on the pinned release, and the deposit form minted with DAPPER 0.2.1-a1."""
        import io, os
        from types import SimpleNamespace
        from reveal_backend import acceptance, account_deposit, agent_account, box_remote
        from reveal_backend.box_literature import LiteratureClient
        from reveal_backend.box_mcp import Ledger
        result = 'Hepatocyte-specific deletion of Lepr reduced insulin-stimulated AKT phosphorylation by 42% in liver.'
        article = ('<?xml version="1.0"?><article><front><article-meta><article-id pub-id-type="pmc">12345</article-id>'
                   '<article-id pub-id-type="pmid">99999</article-id><title-group><article-title>Leptin receptor loss and hepatic insulin '
                   'signalling</article-title></title-group></article-meta></front><body><sec><title>Results</title><p>' + result + '</p>'
                   '<p>Glucose production rose. Later, glucose production rose again in knockout mice.</p></sec></body></article>').encode()
        class Response(io.BytesIO):
            status = 200
        http = SimpleNamespace(open=lambda request, timeout: Response(article))
        claim_text = 'Loss of hepatocyte LEPR reduces hepatic insulin signalling.'
        account = agent_account.build('Does loss of LEPR signalling cause hepatic insulin resistance?', [claim_text])
        gap_id, proposition_id = account['package']['selection']['knowledge_gap_id'], account['claims'][0]['proposition_id']
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); state = base / 'state'; output = base / 'output'
            state.mkdir(); output.mkdir()
            lock = base / 'bundle/services/backend/agent-runtime/dapper-release.json'
            lock.parent.mkdir(parents=True); lock.write_bytes(self.lock.read_bytes())
            package_path = base / 'evidence-package.json'; package_path.write_bytes(canonical_json(account['package']))
            attribution = {'organizations': [{'id': 'urn:reveal:runtime:executor', 'name': 'REVEAL Mechanisms runtime executor'}],
                           'activities': [{'id': 'urn:reveal:runtime:execution:job:1', 'name': 'Claude Code research execution',
                                           'command': 'reveal Box scientific-account authoring', 'software_name': 'Claude Code'}]}
            (state / 'runtime.json').write_bytes(canonical_json({'dapper_root': str(self.release), 'evidence_package': str(package_path),
                                                                  'deliverable': 'scientific_account', 'draft_attribution': attribution}))
            ledger = Ledger(state / 'ledger', 'account-job', 1)
            settings = {'tools': ['Read'], 'graphs': [], 'literature': True,
                        'limits': {'tool_calls': 30, 'literature_searches': 3, 'literature_search_results': 5, 'literature_papers': 4, 'literature_characters': 48_000}}
            def draft(snippet):
                return {'files': [capture['file']],
                        'evidence_items': [{'id': 'urn:draft:evidence-1', 'target_proposition': proposition_id, 'direction': 'SUPPORTS',
                                            'snippet': snippet, 'explanation': 'The paper reports this directly in mouse liver.',
                                            'context': 'Source locator: ' + capture['source_locator'] + '. Mouse liver.',
                                            'evidence_source': 'Europe PMC open-access full text (PMC12345)', 'reference': 'PMID:99999',
                                            'was_derived_from': [capture['file']['id']]}],
                        'claims': [{'id': 'urn:draft:claim-1', 'proposition': proposition_id, 'direction': 'SUPPORTS', 'status': 'proposed',
                                    'statement': 'One mouse study supports the requester\'s claim.', 'has_evidence': ['urn:draft:evidence-1']}],
                        'scientific_accounts': [{'id': 'urn:draft:account-1', 'name': 'LEPR and hepatic insulin signalling', 'question': gap_id,
                                                 'context': 'One open-access paper found through Europe PMC.', 'component_claims': ['urn:draft:claim-1'],
                                                 'closing_remarks': 'A single mouse study; no human data were read.'}]}
            with patch.object(box_remote, 'BASE', base), patch.object(box_remote, 'STATE', state), patch.object(box_remote, 'OUTPUT', output), \
                    patch.object(box_remote.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())):
                tools = box_remote.agent_tools([], ledger, settings, literature_client=lambda **options: LiteratureClient(opener=http, **options),
                                               lint=lambda filename: box_remote.lint_tool(filename, ledger), write_draft=box_remote.write_draft_tool)
                read = tools.call('read_paper', {'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text'})
                self.assertFalse(read.get('isError'), read)
                capture = json.loads(read['content'][-1]['text'])
                # A quote that is verbatim (so the pinned linter accepts it) but occurs twice in the paper: only the quote
                # check can tell the agent, and it does so during the run.
                saved = tools.call('write_account_draft', {'filename': 'account-1.json', 'document': draft('lucose production rose')})
                self.assertFalse(saved.get('isError'), saved)
                feedback = json.loads(tools.call('lint_account', {'filename': 'account-1.json'})['content'][0]['text'])
                errors = [(item['check'], item['where']) for item in feedback['findings'] if item['severity'] == 'error']
                self.assertEqual(errors, [('literature-quote', 'urn:draft:evidence-1')])
                self.assertFalse(feedback['valid'])
                self.assertIn('occurs 2 times', next(item['message'] for item in feedback['findings'] if item['check'] == 'literature-quote'))
                # The trusted Proposition and KnowledgeGap were hydrated from the package; the agent wrote neither.
                stored = json.loads((output / 'account-1.json').read_bytes())
                self.assertEqual([node['id'] for node in stored['knowledge_gaps']], [gap_id])
                self.assertEqual(stored['propositions'], [{'id': proposition_id, 'statement': claim_text}])
                tools.call('write_account_draft', {'filename': 'account-1.json', 'document': draft(result)})
                clean = json.loads(tools.call('lint_account', {'filename': 'account-1.json'})['content'][0]['text'])
                self.assertTrue(clean['valid'], clean.get('findings'))
                # Trying to reword the requester's claim is refused.
                reworded = draft(result); reworded['propositions'] = [{'id': proposition_id, 'statement': 'A reworded claim.'}]
                refused = tools.call('write_account_draft', {'filename': 'account-1.json', 'document': reworded})
                self.assertTrue(refused.get('isError')); self.assertIn('trusted source object was changed', refused['content'][0]['text'])
                self.assertEqual(json.loads((output / 'account-1.json').read_bytes())['propositions'], [{'id': proposition_id, 'statement': claim_text}])
            ledger.freeze()
            manifest = state / 'ledger/manifest.json'
            requester = {'user_id': '11111111-1111-4111-8111-111111111111', 'principal_kind': 'registered', 'display_name': 'Test Requester',
                         'orcid': None, 'orcid_authenticated': False}
            with patch.object(acceptance, 'release_root', return_value=self.release), patch.object(acceptance, 'LOCK', self.lock):
                assembled, report = acceptance.assemble_account(output / 'account-1.json', package_path, base / 'assembled.json',
                                                                requester, {'id': 'account-job'}, 1, 'box', manifest)
            self.assertTrue(report['valid'], report.get('findings'))
            self.assertEqual([node['name'] for node in assembled['persons']], [assembled['persons'][0]['name']])
            self.assertIn('Test Requester', assembled['persons'][0]['name'])
            calls = json.loads(manifest.read_bytes())['calls']
            document, summary = account_deposit.deposit_document(assembled, state / 'ledger', calls)
            self.assertNotIn('persons', document); self.assertNotIn('Test Requester', json.dumps(document))   # not named unless asked
            located = document['value_locators'][0]
            text = account_deposit.paper_text_bytes(state / 'ledger', calls, summary['papers'][0]['text_sha256'])
            self.assertEqual(text[located['byte_start']:located['byte_start'] + located['byte_length']].decode(), result)
            self.assertEqual(document['scientific_accounts'][0]['question'], gap_id)                 # the question keeps its id
            self.assertEqual(document['claims'][0]['proposition'], proposition_id)                   # and so does the requester's claim
            self.assertEqual(summary['account_id'], document['scientific_accounts'][0]['id'])
            self.assertNotEqual(summary['account_id'], assembled['scientific_accounts'][0]['id'])    # the deposit id is the only public one
            self.assertNotIn('urn:reveal', json.dumps(document)); self.assertNotIn('Later, glucose', json.dumps(document))
            # The workflow's gate is those two steps taken once, leaving under the workspace all that commit reads back.
            from reveal_backend.evidence_package import EvidenceBuildError
            work = base / 'work'; work.mkdir()
            captured = SimpleNamespace(account_paths=(output / 'account-1.json',), ledger_manifest_path=manifest)
            with patch.object(acceptance, 'release_root', return_value=self.release), patch.object(acceptance, 'LOCK', self.lock):
                record = agent_account.accept(work, {'id': 'account-job'}, 1, account, requester, captured, output)
            data, texts = agent_account.stored(work, record)
            kept = json.loads(data)
            self.assertEqual(record['account_id'], kept['scientific_accounts'][0]['id'])
            self.assertEqual(record['requested_claims'], [{'statement': claim_text, 'assessed': True, 'claim_ids': [kept['claims'][0]['id']]}])
            self.assertEqual(list(texts), [kept['files'][[node['id'] for node in kept['files']].index(kept['value_locators'][0]['file'])]['sha256']])
            self.assertTrue((work / 'attempt-1/validation-1.json').is_file())
            shown = agent_account.public(record, {'status': 'not_configured'})
            self.assertEqual((shown['claims'], shown['value_locators'], shown['deposit']), (1, 1, {'status': 'not_configured'}))
            self.assertNotIn('path', shown['papers'][0]); self.assertNotIn('Test Requester', json.dumps(shown))
            # The requester is in the stored account only when the job asked to be named.
            self.assertNotIn('Test Requester', data.decode()); self.assertNotIn(requester['user_id'], data.decode())
            named_work = base / 'named'; named_work.mkdir()
            with patch.object(acceptance, 'release_root', return_value=self.release), patch.object(acceptance, 'LOCK', self.lock):
                named = agent_account.accept(named_work, {'id': 'account-job'}, 1, {**account, 'attribute_requester': True}, requester, captured, output)
            named_document = json.loads(agent_account.stored(named_work, named)[0])
            self.assertIn('Test Requester', named_document['persons'][0]['name']); self.assertEqual(named['objects'], record['objects'])
            self.assertEqual(kept['scientific_accounts'][0]['was_attributed_to'], [kept['organizations'][0]['id']])
            self.assertEqual(named_document['scientific_accounts'][0]['was_attributed_to'], [named_document['persons'][0]['id']])
            # A claim the requester asked about and the agent did not assess is reported, not invented.
            other = agent_account.requested_claims([*account['claims'], {'statement': 'An unassessed claim.', 'proposition_id': 'dapper:Proposition.x'}], kept)
            self.assertEqual(other[1], {'statement': 'An unassessed claim.', 'assessed': False, 'claim_ids': []})
            # Bytes that changed after the check are refused at commit.
            saved_bytes = (work / record['document']['path']).read_bytes()
            (work / record['document']['path']).write_bytes(b'{}')
            with self.assertRaisesRegex(EvidenceBuildError, 'changed after validation'): agent_account.stored(work, record)
            (work / record['document']['path']).write_bytes(saved_bytes)
            # An account the pinned linter accepts but whose quote has no single place in the paper fails the gate as a
            # validation failure (it could only get here past the in-sandbox lint by defect or tampering).
            tampered = json.loads((output / 'account-1.json').read_bytes())
            tampered['evidence_items'][0]['snippet'] = 'lucose production rose'
            (output / 'account-1.json').write_bytes(canonical_json(tampered))
            with patch.object(acceptance, 'release_root', return_value=self.release), patch.object(acceptance, 'LOCK', self.lock), \
                    self.assertRaisesRegex(EvidenceBuildError, 'could not be put in its deposit form.*occurs 2 times'):
                agent_account.accept(base / 'work', {'id': 'account-job'}, 1, account, requester, captured, output)

    def test_agent_tool_and_worker_assembly_use_same_source_gate(self):
        from reveal_backend import acceptance, box_remote
        from reveal_backend.authoring_structure import _normalized_finding
        from reveal_backend.box_mcp import Ledger
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); state = base / 'state'; output = base / 'output'
            state.mkdir(); output.mkdir()
            lock = base / 'bundle/services/backend/agent-runtime/dapper-release.json'
            lock.parent.mkdir(parents=True); lock.write_bytes(self.lock.read_bytes())
            (state / 'runtime.json').write_bytes(canonical_json({'dapper_root': str(self.release), 'evidence_package': str(self.package_path)}))
            ledger = Ledger(state / 'ledger', 'parity-job', 1)
            captured_call = ledger.start('query_graph', {'graph': 'prokn'}, 'prokn')
            ledger.finish(captured_call, {'content': [{'type': 'text', 'text': 'Captured auxiliary observation'}]}, 'completed')
            capture = captured_call['response']
            document = deepcopy(self.draft)
            document['files'].append({'id': 'urn:test:external', 'filename': 'capture.json', 'mime_type': 'application/json',
                                      'sha256': capture['sha256'], 'size_in_bytes': capture['size_bytes']})
            document['used_edges'].append({'subject': 'urn:test:activity', 'predicate': 'prov:used', 'object': 'urn:test:external'})
            document['evidence_items'][0]['snippet'] = 'result_key: "derived-field", normalized_score: 0.5795'
            raw = output / 'account-1.json'; raw.write_bytes(canonical_json(document))
            authoritative = []
            def capture_report(*args, **kwargs):
                report = lint_scientific_account(*args, **kwargs)
                authoritative.append(report)
                return report
            with patch.object(box_remote, 'BASE', base), patch.object(box_remote, 'STATE', state), patch.object(box_remote, 'OUTPUT', output), \
                    patch('reveal_backend.scientific_account_lint.lint_scientific_account', side_effect=capture_report):
                feedback = box_remote.lint_tool(raw.name, ledger)
            self.assertTrue(feedback['isError'])
            self.assertFalse(ledger.frozen)
            draft_report = json.loads(feedback['content'][0]['text'])
            self.assertEqual(len(authoritative), 1)
            descriptor = draft_report['report']
            self.assertTrue(descriptor['retained'])
            retained_bytes = Path(descriptor['path']).read_bytes()
            self.assertEqual(sha256(retained_bytes), descriptor['sha256'])
            self.assertEqual(len(retained_bytes), descriptor['size_bytes'])
            retained = decode(retained_bytes)
            self.assertTrue(retained['findings_complete'])
            self.assertEqual(retained['counts'], authoritative[0]['counts'])
            self.assertEqual(retained['finding_count'], len(authoritative[0]['findings']))
            self.assertEqual(retained['findings'], [_normalized_finding(item) for item in authoritative[0]['findings']])
            self.assertEqual(retained['advisories'], [_normalized_finding(item) for item in authoritative[0]['advisories']])
            self.assertEqual(draft_report['findings'], retained['findings'])
            self.assertEqual(draft_report['findings_omitted'], 0)
            ledger.freeze()
            with patch.object(acceptance, 'release_root', return_value=self.release), patch.object(acceptance, 'LOCK', self.lock):
                with self.assertRaises(AccountValidationError) as failure:
                    acceptance.assemble_account(raw, self.package_path, base / 'assembled.json',
                        {'user_id': 'parity-owner', 'principal_kind': 'anonymous'}, {'id': 'parity-job'}, 1, 'deterministic', state / 'ledger/manifest.json')
            source_errors = lambda report: [item for item in report['findings'] if item['check'].startswith(('source-', 'evidence-snippet'))]
            draft_errors = source_errors(retained)
            final_errors = [_normalized_finding(item) for item in source_errors(failure.exception.report)]
            self.assertEqual(len(draft_errors), 1)
            self.assertEqual(len(final_errors), 1)
            # Trusted assembly mints a new EvidenceItem identity. Both reports
            # must locate that same offending item, preserving all other
            # finding fields after the deliberate text normalization.
            assembled = decode((base / 'assembled.json').read_bytes())
            self.assertEqual(draft_errors[0]['where'], document['evidence_items'][0]['id'])
            self.assertEqual(final_errors[0]['where'], assembled['evidence_items'][0]['id'])
            self.assertEqual(assembled['evidence_items'][0]['snippet'], document['evidence_items'][0]['snippet'])
            self.assertEqual(draft_errors, [dict(final_errors[0], where=document['evidence_items'][0]['id'])])
            self.assertEqual(draft_errors[0]['check'], 'evidence-snippet')
            self.assertEqual(draft_errors[0]['severity'], 'error')
            self.assertEqual(draft_errors[0]['rule'], 'evidence-snippet')
            self.assertEqual(draft_errors[0]['message'],
                'Replace snippet with a verbatim excerpt of repair.source_excerpt, the cited source text at this EvidenceItem locator; put summaries in explanation.')
            # The draft tool and final assembly report the same cited source text to copy.
            self.assertIn('source_excerpt', draft_errors[0]['repair'])
            self.assertNotIn('source-file', {item['check'] for item in retained['findings']})
            # A real source quotation must pass both paths with the same live
            # external capture, rather than merely making both paths reject.
            source_id = document['evidence_items'][0]['was_derived_from'][0]
            artifact = next(value for value in self.package['source_artifacts'].values() if value['dapper_file_id'] == source_id)
            row = decode((self.package_path.parent / artifact['path']).read_bytes())['data'][0]
            document['evidence_items'][0]['snippet'] = json.dumps(row)
            raw.write_bytes(canonical_json(document))
            with patch.object(box_remote, 'BASE', base), patch.object(box_remote, 'STATE', state), patch.object(box_remote, 'OUTPUT', output):
                feedback = box_remote.lint_tool(raw.name, ledger)
            self.assertFalse(feedback['isError'], feedback)
            with patch.object(acceptance, 'release_root', return_value=self.release), patch.object(acceptance, 'LOCK', self.lock):
                _, report = acceptance.assemble_account(raw, self.package_path, base / 'repaired.json',
                    {'user_id': 'parity-owner', 'principal_kind': 'anonymous'}, {'id': 'parity-job'}, 1, 'deterministic', state / 'ledger/manifest.json')
            self.assertTrue(report['valid'], report)

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
        self.assertIn('source-ancestry', {f['check'] for f in result['findings']})

    def test_uncaptured_paper_lineage_does_not_qualify_as_evidence(self):
        document = deepcopy(self.draft)
        paper_id = 'urn:test:captured-paper-response'
        document['files'].append({'id': paper_id, 'filename': 'paper-response.json',
                                 'mime_type': 'application/json', 'sha256': 'a' * 64, 'size_in_bytes': 120})
        document['evidence_items'][0]['was_derived_from'] = [paper_id]
        document['evidence_items'][0]['context'] = 'Captured paper abstract at /structuredContent/data/text.'
        path = self.root / 'paper-only.json'; path.write_bytes(canonical_json(document)); self.mint(path)
        result = self.lint(decode(path.read_bytes()))
        self.assertIn('source-ancestry', {f['check'] for f in result['findings']})
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
        for module in ('scientific_account_lint.py', 'source_validation.py', 'claim_suggestions.py'):
            relative = Path('services/backend/src/reveal_backend') / module
            self.assertEqual((workspace / 'reveal' / relative).read_bytes(), (ROOT / relative).read_bytes())
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
