"""Scoped exploration durability, recovery and explicit publication boundaries."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from reveal_backend import analysis_outcomes as outcomes, app as api, jobs
from reveal_backend.agent_execution import ExecutionRequest, ExecutionResult
from reveal_backend.box_adapter import atomic_capture_marker, capture_binding, BoxTransportError
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.repository import Transaction, digest, now, uid
from reveal_backend.runtime_config import ROOT
from reveal_backend.worker import Worker
import test_account_discovery as discovery


class AnalysisOutcomeTests(unittest.TestCase):
    principal=discovery.AccountDiscoveryTests.principal
    headers=discovery.AccountDiscoveryTests.headers
    def setUp(self):
        discovery.AccountDiscoveryTests.setUp(self)
        self.root=Path(self.repo.sqlite_path).parent
        self.request_id=uid()
        with self.repo.transaction() as tx:
            self.job=jobs.enqueue(tx,self.owner,'analysis',request_id=self.request_id)
        self.job,self.queue=jobs.claim(self.repo,'outcome-test')
        target=self.root/self.job['id']/'evidence/package'
        shutil.copytree(ROOT/'data/evidence-packages/cad-builder-v1',target)
        self.package_path=target/'evidence-package.json'; self.package=json.loads(self.package_path.read_bytes())
        self.gap=deepcopy(self.fixture_gap()); self.gap['object']=self.package['dapper_context']['knowledge_gaps'][0]
        self.catalog.gaps={self.gap['object']['id']:self.gap}; self.catalog.by_source={self.gap['source']['source_id']:self.gap}
        selected=[]; bound=[]; named=[]
        for index,native in enumerate(self.package['selection']['eaggl_mechanism_ids']):
            mechanism=self.package['pigean']['mechanisms'][native]
            reference={'source':'eaggl','source_id':native,'dapper_id':mechanism['dapper_id'],'source_revision':'e'*64}
            selected.append({'reference':reference,'origin':'automatic','suggestion_id':uid()})
            bound.append({'cfde_node_id':native,'reference_generation_id':digest('fixture'),'embedding_run_id':'embedding-run','mapping_run_id':'mapping-run'})
            named.append({'id':mechanism['dapper_id'],'name':'Frozen human mechanism '+str(index)})
        source={'source_id':self.gap['source']['source_id'],'id':self.gap['object']['id'],
            'source_revision':self.package['dismech']['source_revision']['source_sha256']}
        self.gap['source']['source_revision']=source['source_revision']
        self.frozen={'question_id':self.gap['object']['id'],'document':{'mechanisms':named},'composer':{'source_gap':source,
            'selected_kgs':self.package['external_evidence']['selected_graphs'],'eaggl_anchors':selected},
            'attribution':{'user_id':self.owner,'person_id':None,'principal_kind':'registered','display_name':'Original investigator',
                'orcid':None,'orcid_authenticated':False,'observed_at':now(),'email':'private@example.invalid'}}
        self.binding={'source_gap':{'object':self.gap['object']},'anchors':bound}
        self.output=self.root/self.job['id']/'attempt-1/output'; (self.output/'output').mkdir(parents=True)
        self.outcome_path=self.output/'output/outcome.json'
        self.raw={'status':'insufficient_evidence','knowledge_gap_id':self.gap['object']['id'],'reason':'The inspected captured scope did not supply the missing link. No global absence is established.'}
        self.outcome_path.write_bytes(canonical_json(self.raw))
        self.execution=ExecutionResult('insufficient_evidence',self.output,outcome_path=self.outcome_path,reason=self.raw['reason'])
        self.snapshot={'kind':'analysis','mode':'deterministic','model':'claude-sonnet-4-6','path':'evidence/package/evidence-package.json','sha256':sha256(self.package_path.read_bytes())}
        with self.repo.transaction() as tx:
            tx.put('request',self.request_id,self.owner,self.frozen); tx.put('request_binding',self.request_id,self.owner,self.binding)
            queue=tx.get('queue',self.job['id'])['data']; queue['dispatch_input']=self.snapshot
            tx.put('queue',self.job['id'],self.owner,queue); self.queue=queue
        self.environment=patch.dict('os.environ',{'REVEAL_ARTIFACTS_DIR':str(self.root),'REVEAL_ENVIRONMENT':'test','REVEAL_EXECUTION_MODE':'deterministic'})
        self.environment.start(); self.addCleanup(self.environment.stop)
        context=patch.object(api,'artifacts_root',return_value=self.root.resolve()); context.start(); self.addCleanup(context.stop)
    def fixture_gap(self): return json.loads((ROOT/'services/frontend/src/lib/fixtures/contract.json').read_text())['gaps']['items'][0]
    def prepare(self): return outcomes.prepare(self.job,self.frozen,self.binding,self.package_path,self.execution,attempt=1,mode='deterministic',expected_model='claude-sonnet-4-6')
    def save(self):
        prepared=self.prepare()
        with self.repo.transaction() as tx: identity=outcomes.save(tx,self.job,prepared)
        return identity,prepared
    def control(self,identity,*,owner=None,visibility='public',version=0,key=None):
        return self.client.post('/v1/analysis-outcomes/'+identity+'/publication',headers={**self.headers(owner or self.owner),'Idempotency-Key':key or uid()},json={'visibility':visibility,'expected_version':version})
    def get(self,path,owner=None): return self.client.get(path,headers=self.headers(owner) if owner else {})

    def display_binding(self):
        saved={'selections':{}}
        for selected,anchor in zip(self.frozen['composer']['eaggl_anchors'],self.binding['anchors']):
            reference=selected['reference']; native=reference['source_id']
            saved['selections'][native]={'reference':deepcopy(reference),'binding':deepcopy(anchor),
                'record':{'source_id':native,'source_revision':reference['source_revision'],'object':{'id':reference['dapper_id']},
                    'cfde_anchor':{'node_id':native,'label':'Lipoprotein transport','subtitle':'CAD (Factor1)'}}}
        return saved

    def test_exact_saved_display_is_frozen_and_incompatible_selection_never_relabels_outcome(self):
        saved=self.display_binding(); original=deepcopy(self.binding)
        display=outcomes.anchor_display(self.frozen['composer'],self.binding,saved)
        self.binding['anchor_display']=display
        self.assertEqual(self.prepare()['record']['anchors'][0]['name'],'Lipoprotein transport')
        self.assertEqual(self.prepare()['record']['anchors'][0]['trait'],'CAD')
        native=self.frozen['composer']['eaggl_anchors'][0]['reference']['source_id']
        display[native]['subtitle']='T2D (age 50) (factor12) '
        self.assertEqual(self.prepare()['record']['anchors'][0]['trait'],'T2D (age 50)')
        for field in ('source_id','source_revision','dapper_id'):
            changed=deepcopy(saved); changed['selections'][native]['reference'][field]='changed'
            self.assertNotIn(native,outcomes.anchor_display(self.frozen['composer'],original,changed))
        for field in ('embedding_run_id','mapping_run_id','eaggl_import_id'):
            changed=deepcopy(saved); changed['selections'][native]['binding'][field]='changed'
            self.assertNotIn(native,outcomes.anchor_display(self.frozen['composer'],original,changed))
        changed=deepcopy(saved); changed['selections'][native]['record']['source_revision']='changed'
        self.assertNotIn(native,outcomes.anchor_display(self.frozen['composer'],original,changed))
        self.binding=original
        self.assertEqual(self.prepare()['record']['anchors'][0]['name'],'Frozen human mechanism 0')

    def test_submission_freezes_display_from_exact_saved_selection(self):
        draft_id=uid(); saved=self.display_binding()
        saved.update(dismech_import_id='source-import',source_gap=self.gap)
        with self.repo.transaction() as tx:
            principal=tx.get('principal',self.owner)['data']; principal['me'].update(orcid=None,orcid_authenticated=False)
            tx.put('principal',self.owner,self.owner,principal)
            tx.put('draft',draft_id,self.owner,{'id':draft_id,'version':1,'composer':self.frozen['composer']})
            tx.put('draft_binding',draft_id,self.owner,saved)
        response=self.client.post('/v1/jobs',headers={**self.headers(self.owner),'Idempotency-Key':uid()},json={'kind':'analysis','draft_id':draft_id,'draft_version':1})
        self.assertEqual(response.status_code,202,response.text)
        with self.repo.read_transaction() as tx:
            binding=tx.get('request_binding',response.json()['research_request_id'])['data']
            self.assertEqual(binding['anchor_display'],outcomes.anchor_display(self.frozen['composer'],self.binding,saved))

    def test_legacy_reason_scope_names_and_source_versions_preserved_without_invented_details(self):
        before=self.outcome_path.read_bytes(); prepared=self.prepare(); record=prepared['record']
        self.assertEqual(record['reason'],self.raw['reason']); self.assertEqual(record['record_format'],'legacy')
        self.assertEqual(record['missing_evidence'],[]); self.assertEqual(record['next_steps'],[])
        self.assertEqual(record['anchors'][0]['name'],'Frozen human mechanism 0')
        self.assertEqual(record['provenance']['source_bindings'][0]['embedding_run_id'],'embedding-run')
        self.assertNotIn('email',record['attribution']); self.assertEqual(self.outcome_path.read_bytes(),before)
        self.assertEqual(record['provenance']['outcome_sha256'],sha256(before))
        self.assertEqual(len(prepared['artifacts']),len({item['sha256'] for item in self.package['source_artifacts'].values()}))
        changed=dict(self.raw,knowledge_gap_id='dapper:KnowledgeGap.'+'z'*32); self.outcome_path.write_bytes(canonical_json(changed))
        with self.assertRaisesRegex(ValueError,'another knowledge gap'): self.prepare()

    def test_worker_persists_terminal_result_atomically_and_never_creates_account_or_outbox(self):
        execution=self.execution
        class Adapter:
            async def execute(self,*args): return execution
        with patch('httpx.post',side_effect=AssertionError('No external calls')):
            asyncio.run(Worker(self.repo,Adapter()).process(self.job,self.queue))
        with self.repo.read_transaction() as tx:
            job=tx.get('job',self.job['id'])['data']; self.assertEqual(job['status'],'insufficient_evidence')
            self.assertEqual(job['stage'],'complete'); self.assertEqual(job['result']['kind'],'analysis_outcome')
            self.assertEqual(len(tx.list('analysis_outcome')),1); self.assertEqual(tx.list('account'),[]); self.assertEqual(tx.list('outbox'),[])
            api.validate(job,'Job')
            for event in tx.list('event'): api.validate(event['data'],'JobEvent')
        response=self.get('/v1/jobs/'+self.job['id']+'/outcome',self.owner)
        self.assertEqual(response.status_code,200,response.text); api.validate(response.json(),'AnalysisOutcome')
        self.assertFalse(Worker(self.repo).accept_outcome(self.job,self.queue['token'],self.prepare()))

    def test_workspace_lists_saved_outcomes_across_gaps_with_owner_bound_pagination(self):
        first, prepared = self.save()
        newer = deepcopy(prepared)
        newer['record']['created_at'] = '2099-01-01T00:00:00Z'
        newer['record']['knowledge_gap']['id'] = 'dapper:KnowledgeGap.' + 'x' * 32
        with self.repo.transaction() as tx:
            second = outcomes.save(tx, dict(self.job, id=uid()), newer)
            foreign = outcomes.save(tx, dict(self.job, id=uid(), owner_user_id=self.other), prepared)
        self.assertEqual(self.control(foreign, owner=self.other).status_code, 200)
        route = '/v1/analysis-outcomes'
        self.assertEqual(self.get(route).status_code, 401)
        first_page = self.get(route + '?limit=1', self.owner).json()
        api.validate(first_page, 'AnalysisOutcomeList')
        self.assertEqual([item['id'] for item in first_page['items']], [second])
        self.assertEqual(first_page['items'][0]['publication']['visibility'], 'private')
        self.assertTrue(first_page['items'][0]['publication']['can_manage'])
        self.assertNotIn('provenance', first_page['items'][0])
        cursor = first_page['page']['next_cursor']
        second_page = self.get(route + '?limit=1&cursor=' + cursor, self.owner).json()
        self.assertEqual([item['id'] for item in second_page['items']], [first])
        self.assertFalse(second_page['page']['has_more'])
        self.assertEqual(self.get(route + '?cursor=' + cursor, self.other).status_code, 409)
        self.assertEqual([item['id'] for item in self.get(route, self.other).json()['items']], [foreign])
        # Claiming transfers private summaries too; it does not rewrite authorship.
        with self.repo.transaction() as tx: tx.transfer(self.owner, self.other)
        self.assertEqual(self.get(route, self.owner).json()['items'], [])
        moved = self.get(route, self.other).json()['items']
        self.assertEqual({item['id'] for item in moved}, {first, second, foreign})
        self.assertEqual(next(item for item in moved if item['id'] == first)['attribution']['user_id'], self.owner)

    def test_cancelled_or_stale_attempt_cannot_save_outcome(self):
        prepared=self.prepare()
        self.assertFalse(Worker(self.repo).accept_outcome(self.job,'wrong-token',prepared))
        with self.repo.transaction() as tx: jobs.cancel(tx,tx.get('job',self.job['id'])['data'])
        self.assertFalse(Worker(self.repo).accept_outcome(self.job,self.queue['token'],prepared))
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('analysis_outcome'),[])

    def test_publication_scopes_counts_private_operations_and_exact_source_artifacts(self):
        identity,prepared=self.save(); route='/v1/analysis-outcomes/'+identity
        self.assertEqual(self.get(route).status_code,404); self.assertEqual(self.get(route,self.other).status_code,404)
        self.assertEqual(self.control(identity,owner=self.other).status_code,404)
        key=uid(); first=self.control(identity,key=key); self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(self.control(identity,key=key).json(),first.json())
        self.assertEqual(self.control(identity).status_code,409)
        public=self.get(route); self.assertEqual(public.status_code,200,public.text); api.validate(public.json(),'AnalysisOutcome')
        self.assertIsNone(public.json()['job_id']); self.assertFalse(public.json()['publication']['can_manage'])
        self.assertEqual(public.headers['cache-control'],'private, no-store')
        checksum=next(iter(prepared['artifacts'])); artifact=prepared['artifacts'][checksum]
        download=self.get('/v1/artifacts/'+checksum); self.assertEqual(download.status_code,200,download.text[:100])
        self.assertEqual(download.content,Path(artifact['path']).read_bytes())
        self.assertEqual(self.get('/v1/artifacts/'+prepared['record']['provenance']['evidence_package_sha256']).status_code,404)
        self.assertEqual(self.get('/v1/jobs/'+self.job['id']+'/outcome').status_code,401)
        self.assertEqual(self.get('/v1/jobs/'+self.job['id']+'/outcome',self.other).status_code,404)
        for path in (route,route+'/publication','/v1/artifacts/'+checksum):
            self.assertEqual(self.client.get(path,headers={'Authorization':'Bearer invalid'}).status_code,401)
        gap='/v1/knowledge-gaps/'+self.gap['object']['id']
        listing=self.get(gap+'/outcomes').json(); self.assertEqual(len(listing['items']),1); api.validate(listing,'AnalysisOutcomeList')
        self.assertNotIn('provenance',listing['items'][0]); self.assertNotIn('reason',listing['items'][0])
        self.assertEqual(self.get(gap).json()['scientific_accounts']['count'],0)
        self.assertEqual(self.get('/v1/objects/'+self.gap['object']['id']).status_code,404)
        self.assertEqual(self.control(identity,visibility='private',version=1).status_code,200)
        self.assertEqual(self.get(route).status_code,404); self.assertEqual(self.get('/v1/artifacts/'+checksum).status_code,404)
        self.assertEqual(self.get('/v1/artifacts/'+checksum,self.owner).status_code,200)

    def test_transfer_preserves_original_author_and_moves_management(self):
        identity,_=self.save(); self.assertEqual(self.control(identity).status_code,200)
        with self.repo.transaction() as tx: tx.transfer(self.owner,self.other)
        route='/v1/analysis-outcomes/'+identity
        self.assertFalse(self.get(route,self.owner).json()['publication']['can_manage'])
        self.assertTrue(self.get(route,self.other).json()['publication']['can_manage'])
        self.assertEqual(self.get(route,self.other).json()['attribution']['user_id'],self.owner)
        self.assertEqual(self.control(identity,visibility='private',version=1).status_code,404)
        self.assertEqual(self.control(identity,owner=self.other,visibility='private',version=1).status_code,200)
        self.assertEqual(self.get('/v1/jobs/'+self.job['id']+'/outcome',self.other).status_code,200)

    def test_compact_listing_never_loads_full_records_and_binds_scope_cursor(self):
        identity,_=self.save(); self.assertEqual(self.control(identity).status_code,200)
        path='/v1/knowledge-gaps/'+self.gap['object']['id']+'/outcomes'
        original=Transaction.get
        def guarded(tx,kind,*args):
            if kind in ('analysis_outcome','outcome_snapshot'): raise AssertionError('List fetched large outcome detail')
            return original(tx,kind,*args)
        with patch.object(Transaction,'get',guarded):
            self.assertEqual(self.get(path).status_code,200)
            self.assertEqual(self.get(path+'?scope=workspace',self.owner).status_code,200)
        self.assertEqual(self.get(path+'?scope=workspace').status_code,401)
        self.assertEqual(self.get(path+'?source_revision='+'0'*64).status_code,409)
        self.assertEqual(self.get(path+'?scope=workspace',self.other).json()['items'],[])

    def test_evidence_references_fail_closed_for_absent_or_model_authored_sources(self):
        structured={'format':'reveal.insufficient-evidence/1','status':'insufficient_evidence','reason':'Scoped evidence report.',
            'evidence_refs':[{'source':'package','pointer':'/pigean/mechanisms','ledger_sequence':None}]}
        self.outcome_path.write_bytes(canonical_json(structured)); record=self.prepare()['record']
        self.assertEqual(record['record_format'],'structured'); self.assertIsNone(record['provenance']['evidence_refs'][0]['download_url'])
        structured['evidence_refs'][0]['pointer']='/does-not-exist'; self.outcome_path.write_bytes(canonical_json(structured))
        with self.assertRaisesRegex(ValueError,'pointer is absent'): self.prepare()
        structured['evidence_refs']=[{'source':'tool_response','pointer':'','ledger_sequence':1}]
        self.outcome_path.write_bytes(canonical_json(structured))
        with self.assertRaisesRegex(ValueError,'completed trusted read'): self.prepare()

    def captured(self):
        ledger=self.output/'ledger/manifest.json'; ledger.parent.mkdir()
        ledger.write_bytes(canonical_json({'format':'reveal.tool-ledger/1','job_id':self.job['id'],'attempt':1,'complete':True,'calls':[]}))
        lock=json.loads((ROOT/'services/backend/agent-runtime/dapper-release.json').read_text())
        runtime=self.output/'runtime.json'; runtime.write_bytes(canonical_json({'job_id':self.job['id'],'attempt':1,
            'input_sha256':self.snapshot['sha256'],'dapper':{'commit':lock['commit']},'model':self.snapshot['model'],'completed_at':now()}))
        request=ExecutionRequest(job_id=self.job['id'],attempt=1,kind='research',input_path=self.package_path,output_dir=self.output,
            selected_graphs=tuple(self.frozen['composer']['selected_kgs']))
        handle={'box_id':'offline-box-no-network'}
        marker={'format':'reveal.box-capture/1','binding':capture_binding(request,handle),'state':{'status':'insufficient_evidence'},'cleanup_complete':True,
            'files':{str(path.relative_to(self.output)):{'sha256':sha256(path.read_bytes()),'size_bytes':path.stat().st_size} for path in (ledger,runtime,self.outcome_path)}}
        atomic_capture_marker(self.output/'.box-capture-complete.json',marker)
        self.execution=replace(self.execution,runtime_manifest_path=runtime,ledger_manifest_path=ledger,remote_handle=handle)
        return self.execution

    def test_explicit_historical_recovery_is_idempotent_and_checks_capture_before_any_write(self):
        self.captured(); self.snapshot['mode']='box'
        self.frozen['source_draft_id']=uid(); original=deepcopy(self.binding)
        with self.repo.transaction() as tx:
            tx.put('request',self.request_id,self.owner,self.frozen)
            tx.put('draft_binding',self.frozen['source_draft_id'],self.owner,self.display_binding())
            queue=tx.get('queue',self.job['id'])['data']; queue['dispatch_input']=self.snapshot; tx.put('queue',self.job['id'],self.owner,queue)
        jobs.finish(self.repo,self.job['id'],self.queue['token'],'insufficient_evidence')
        spec=importlib.util.spec_from_file_location('recovery',ROOT/'scripts/recover_analysis_outcome.py'); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with patch('httpx.post',side_effect=AssertionError('Recovery must not run research')):
            dry=module.recover(self.repo,self.job['id'],root=self.root); self.assertFalse(dry['applied'])
            self.assertEqual(dry['anchors'][0],{'name':'Lipoprotein transport','trait':'CAD'})
            with self.repo.read_transaction() as tx: self.assertEqual(tx.list('analysis_outcome'),[])
            first=module.recover(self.repo,self.job['id'],root=self.root,apply=True)
            second=module.recover(self.repo,self.job['id'],root=self.root,apply=True)
        self.assertEqual(first['outcome_id'],second['outcome_id'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('request_binding',self.request_id)['data'],original)
            self.assertEqual(len(tx.list('analysis_outcome')),1)
            job=tx.get('job',self.job['id'])['data']; api.validate(job,'Job')
            self.assertEqual(job['completed_at'],first['created_at'])
        self.outcome_path.write_text('{}')
        with self.assertRaises(BoxTransportError): module.recover(self.repo,self.job['id'],root=self.root,apply=True)

    def test_missing_outcome_file_is_never_promoted_from_only_reason_text(self):
        self.execution=replace(self.execution,outcome_path=None)
        with self.assertRaisesRegex(ValueError,'captured outcome artifact'): self.prepare()

if __name__=='__main__': unittest.main()
