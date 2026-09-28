"""Actual collector fixture → adapter → trusted validator → persistence → Paragraph."""
import asyncio
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import AsyncMock, patch
from reveal_backend.repository import Repository, uid, now, digest
from reveal_backend import jobs
from reveal_backend.worker import Worker
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.auth import owned
from reveal_backend.acceptance import validate_observations,replace_authored_attribution,validate_new_files
from reveal_backend.evidence_package import sha256
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.citations import export
from reveal_backend.app import validate
import test_evidence_package as fixtures

class WorkerStreamMappingTests(unittest.TestCase):
    def test_persistence_boundary_is_durable_and_refuses_stale_or_cancelled_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Repository(str(Path(temp)/'application.db')); repo.migrate(); owner=uid()
            with repo.transaction() as tx: job=jobs.enqueue(tx,owner,'analysis',request_id=uid())
            job,queue=jobs.claim(repo,'test-worker'); worker=Worker(repo)
            self.assertFalse(asyncio.run(worker.begin_persistence(job,'stale','Saving results.')))
            self.assertTrue(asyncio.run(worker.begin_persistence(job,queue['token'],'Saving results.')))
            with repo.read_transaction() as tx:
                stored=tx.get('job',job['id'])['data']
                self.assertEqual(stored['stage'],'persisting')
                event=tx.get('event',job['id']+':'+stored['last_event_id'].zfill(12))['data']
                self.assertEqual(event['stage'],'persisting'); validate(event,'JobEvent')
                self.assertEqual(tx.list('account',owner),[])
            with repo.transaction() as tx: jobs.cancel(tx,tx.get('job',job['id'])['data'])
            self.assertFalse(asyncio.run(worker.begin_persistence(job,queue['token'],'Must not announce saving.')))
            with repo.read_transaction() as tx:
                self.assertEqual(sum(row['data']['message']=='Saving results.' for row in tx.list('event',owner)),1)
                self.assertFalse(any(row['data']['message']=='Must not announce saving.' for row in tx.list('event',owner)))

    def test_paragraph_immutable_content_rejection_never_announces_persistence(self):
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as temp:
            account={'id':'dapper:ScientificAccount.'+'a'*32,'closing_remarks':'Exact accepted content'}
            inputs={'account_document':{'scientific_accounts':[account]}}
            job={'id':uid(),'input_account_id':account['id']}
            worker=Worker(); worker.begin_persistence=AsyncMock(return_value=True)
            def changed(document,path):
                result=deepcopy(document); result['scientific_accounts'][0]['closing_remarks']='Changed accepted content'
                return result
            with patch('reveal_backend.box_paragraph.assemble_paragraph',return_value={'text':'Statement','citations':[]}), \
                    patch('reveal_backend.worker.release_root',return_value=Path(temp)), \
                    patch('reveal_backend.worker.mint',side_effect=changed), \
                    patch('reveal_backend.worker.validate_paragraph_document',return_value={'errors':[],'warnings':[]}):
                with self.assertRaisesRegex(EvidenceBuildError,'changed accepted scientific content'):
                    asyncio.run(worker.accept_paragraph(job,'token',{},inputs,Path(temp)))
            worker.begin_persistence.assert_not_awaited()

    def test_actual_box_text_deltas_and_tool_names_survive_persisted_events(self):
        import json
        from reveal_backend.box_stream import ClaudeStream
        wire=[
            {'type':'stream_event','event':{'delta':{'type':'thinking_delta','thinking':'private reasoning'}}},
            {'type':'stream_event','event':{'delta':{'type':'text_delta','text':'First '}}},
            {'type':'stream_event','event':{'delta':{'type':'text_delta','text':'sentence.'}}},
            {'type':'assistant','message':{'content':[{'type':'tool_use','id':'read-1','name':'Read','input':{'file_path':'source.json'}}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'read-1','content':'Source unavailable','is_error':True}]}},
            {'type':'assistant','message':{'content':[{'type':'tool_use','id':'read-2','name':'Glob','input':{'pattern':'*.json'}}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'read-2','content':'source.json'}]}},
            {'type':'result','is_error':True}]
        parser=ClaudeStream(); events=parser.feed(('\n'.join(json.dumps(row) for row in wire)+'\n').encode())+parser.finish()
        class Adapter:
            async def execute(self,request,emit,cancelled,checkpoint):
                for sequence,(kind,payload) in enumerate(events,1):
                    forwarded={**payload,'remote_stream_id':'actual-parser-shape','remote_sequence':sequence}
                    await emit(kind,forwarded); await emit(kind,forwarded)
                return ExecutionResult('failed',request.output_dir,reason='Intentional transport fixture failure')
        with tempfile.TemporaryDirectory() as temp:
            repository=Repository(str(Path(temp)/'app.sqlite')); repository.migrate(); owner=uid(); identity='dapper:ScientificAccount.'+'a'*32
            with repository.transaction() as tx:
                tx.put('account',digest([owner,identity]),owner,{'result':{'document':{},'citation_metadata':[]},'summary':{}})
                job=jobs.enqueue(tx,owner,'paragraph',account_id=identity)
            with patch.dict('os.environ',{'REVEAL_EXECUTION_MODE':'deterministic','REVEAL_ARTIFACTS_DIR':temp}):
                asyncio.run(Worker(repository,Adapter()).process(*jobs.claim(repository,'stream-test')))
            with repository.transaction() as tx:
                delivered=sorted([row['data'] for row in tx.list('event',owner)],key=lambda row:int(row['id']))
            deltas=[row for row in delivered if row.get('detail') and row['detail'].get('message_delta')]
            self.assertEqual([row['message'] for row in deltas],['First ','sentence.'])
            tools=[row for row in delivered if row.get('detail') and row['detail']['kind'] in ('tool_call','tool_result')]
            self.assertEqual([row['detail']['tool_name'] for row in tools],['Read','Read','Glob','Glob'])
            self.assertEqual([row['detail']['call_id'] for row in tools],['read-1','read-1','read-2','read-2'])
            self.assertEqual([row['detail']['state'] for row in tools],['started','failed','started','completed'])
            self.assertTrue(all('message_delta' not in row['detail'] for row in tools))
            self.assertNotIn('private reasoning',json.dumps(delivered))
            self.assertEqual(len(deltas),2)  # duplicate remote deliveries remain idempotent
            for row in delivered: validate(row,'JobEvent')

    def test_slow_stream_writes_do_not_starve_lease_heartbeats(self):
        import time
        sleep=asyncio.sleep; real_event=jobs.event; real_heartbeat=jobs.heartbeat
        heartbeats=[]; during=[]
        async def fast_lease_sleep(seconds): await sleep(0.002 if seconds==20 else seconds)
        def slow_event(tx,job,event_type,message,detail=None):
            if detail and detail['kind']=='agent_message': time.sleep(0.03)
            return real_event(tx,job,event_type,message,detail)
        def heartbeat(*args,**kwargs):
            result=real_heartbeat(*args,**kwargs)
            if result: heartbeats.append(time.monotonic())
            return result
        class SlowRepository(Repository):
            def connect(self):
                # Model remote connection latency as well as transactional writes.
                # SQLite's writer busy-retry policy differs from Aurora row locks.
                time.sleep(0.03)
                return super().connect()
        class Adapter:
            async def execute(self,request,emit,cancelled,checkpoint):
                start=len(heartbeats)
                for index in range(12): await emit('agent_message',{'text':str(index),'delta':True})
                during.extend([start,len(heartbeats)])
                return ExecutionResult('failed',request.output_dir,reason='Intentional transport fixture failure')
        with tempfile.TemporaryDirectory() as temp:
            repository=SlowRepository(str(Path(temp)/'app.sqlite')); repository.migrate(); owner=uid(); identity='dapper:ScientificAccount.'+'a'*32
            with repository.transaction() as tx:
                tx.put('account',digest([owner,identity]),owner,{'result':{'document':{},'citation_metadata':[]},'summary':{}})
                jobs.enqueue(tx,owner,'paragraph',account_id=identity)
            with patch.dict('os.environ',{'REVEAL_EXECUTION_MODE':'deterministic','REVEAL_ARTIFACTS_DIR':temp}),patch('reveal_backend.worker.asyncio.sleep',new=fast_lease_sleep),patch.object(jobs,'event',side_effect=slow_event),patch.object(jobs,'heartbeat',side_effect=heartbeat):
                asyncio.run(Worker(repository,Adapter()).process(*jobs.claim(repository,'stream-test')))
            self.assertEqual(len(during),2)
            self.assertGreater(during[1],during[0],'Lease renewals must complete during slow streamed event writes, not only before/after the batch')

class SourceMetricTests(unittest.TestCase):
    def test_guessed_actors_and_runtime_are_removed_but_source_nodes_unchanged(self):
        original={'id':'source-actor','name':'Source person'}
        doc={'persons':[original,{'id':'guessed','name':'Invented scientist','orcid':'bad'}],
            'organizations':[{'id':'guessed-org','name':'Invented lab'}], 'activities':[{'id':'guessed-run','command':'guessed'}],
            'claims':[{'id':'claim','was_attributed_to':['guessed','guessed-org'],'was_generated_by':'guessed-run'}]}
        replace_authored_attribution(doc,{'source-actor':original},{'id':'actual','name':'Actual operator'},{'id':'actual-run','command':'trusted'})
        self.assertEqual(doc['persons'],[original,{'id':'actual','name':'Actual operator'}]); self.assertEqual(doc['organizations'],[])
        self.assertEqual(doc['claims'][0]['was_generated_by'],'actual-run'); self.assertNotIn('Invented',str(doc))

    def test_new_evidence_file_requires_exact_capture_hash_and_size(self):
        doc={'files':[{'id':'new-file','sha256':'a'*64,'size_in_bytes':12}]}
        with self.assertRaises(EvidenceBuildError): validate_new_files(doc,{}, {})
        with self.assertRaises(EvidenceBuildError): validate_new_files(doc,{}, {'a'*64:{'size_bytes':13}})
        validate_new_files(doc,{}, {'a'*64:{'size_bytes':12}})
    def test_same_numeric_value_at_wrong_row_is_rejected(self):
        doc={'claims':[{'id':'c','has_score':['s'],'has_evidence':['e']}],
             'claim_scores':[{'id':'s','metric':'factor_value','value':0.5,'score_kind':'LOADING'}],
             'evidence_items':[{'id':'e','context':'source /data/1','was_derived_from':['f']}]}
        source={'f':{'data':[{'factor_value':0.5},{'other_metric':0.5,'factor_value':0.7}]}}
        with self.assertRaises(EvidenceBuildError): validate_observations(doc,source)
        doc['evidence_items'][0]['context']='source /data/0'; validate_observations(doc,source)
        doc['claim_scores'][0]['score_kind']='PROBABILITY'
        with self.assertRaises(EvidenceBuildError): validate_observations(doc,source)

@unittest.skipUnless((fixtures.ROOT/'.runtime/dapper/.git').exists(),'Trusted DAPPER checkout requires explicit one-time setup')
class WorkerJourneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.EvidencePackageTests.setUpClass()
        cls.addClassCleanup(fixtures.EvidencePackageTests.tearDownClass)
    def test_accepted_account_auto_paragraph_and_complete_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Repository(str(Path(temp)/'application.db')); repo.migrate(); user=uid()
            package=fixtures.EvidencePackageTests.built.package
            source=fixtures.EvidencePackageTests.root/'capture/package/evidence-package.json'
            frozen={'id':uid(),'question_id':package['selection']['knowledge_gap_id'],'composer':{'selected_kgs':[]},
                'attribution':{'user_id':user,'principal_kind':'anonymous','display_name':None,'orcid':None,'orcid_authenticated':False}}
            with repo.transaction() as tx:
                tx.put('request',frozen['id'],user,frozen); tx.put('request_binding',frozen['id'],user,{})
                job=jobs.enqueue(tx,user,'analysis',request_id=frozen['id'],inputs={'budgets':{'max_accounts':1}})
            capture=Path(temp)/job['id']/'evidence'; shutil.copytree(source.parent.parent,capture)
            source=capture/'package/evidence-package.json'
            with patch.dict('os.environ',{'REVEAL_EXECUTION_MODE':'deterministic','REVEAL_ARTIFACTS_DIR':temp}), patch('reveal_backend.worker.collect',return_value=(source,package)):
                worker=Worker(repo); asyncio.run(worker.process(*jobs.claim(repo,'test-worker')))
                with repo.transaction() as tx:
                    result=tx.get('job',job['id'])['data']
                    if result['status']!='succeeded':
                        failures=list(Path(temp).rglob('failure.json'))
                        self.fail(str(result)+' '+str([p.read_text() for p in failures]))
                    self.assertIn('DEVELOPMENT SIMULATION',result['warnings'][0])
                    validate(result,'Job')
                    analysis_events=sorted((row['data'] for row in tx.list('event',user) if row['data']['job_id']==job['id']),key=lambda item:int(item['id']))
                    self.assertEqual(sum(item['stage']=='persisting' for item in analysis_events),1)
                    self.assertEqual([item['stage'] for item in analysis_events[-2:]],['persisting','complete'])
                    self.assertTrue(any(item['stage']=='validating' for item in analysis_events[:-2]))
                    account_id=result['result']['account_ids'][0]; paragraph_job=result['result']['paragraph_job_ids'][0]
                    import json
                    manifest_path=Path(temp)/job['id']/'attempt-1/worker-output.json'; manifest=json.loads(manifest_path.read_text())
                    for entry in manifest['accounts']:
                        published=(manifest_path.parent/entry['path']).resolve()
                        self.assertTrue(published.is_relative_to(manifest_path.parent.resolve()))
                        self.assertEqual(sha256(published.read_bytes()),entry['sha256'])
                asyncio.run(worker.process(*jobs.claim(repo,'test-worker')))
                with repo.transaction() as tx:
                    result=tx.get('job',paragraph_job)['data']
                    if result['status']!='succeeded':
                        self.fail(str(result)+' '+str([p.read_text() for p in Path(temp).rglob('failure.json')]))
                    paragraph_id=result['result']['paragraph_id']
                    paragraph_events=sorted((row['data'] for row in tx.list('event',user) if row['data']['job_id']==paragraph_job),key=lambda item:int(item['id']))
                    self.assertEqual(sum(item['stage']=='persisting' for item in paragraph_events),1)
                    self.assertEqual([item['stage'] for item in paragraph_events[-2:]],['persisting','complete'])
                    self.assertTrue(any(item['stage']=='validating' for item in paragraph_events[:-2]))
                    account=owned(tx,'account',account_id,user)['data']
                    validate(account['result'],'AccountResult')
                    self.assertEqual(account['result']['research_statement']['paragraph_id'],paragraph_id)
                    exports=[export(tx,user,paragraph_id,format) for format in ('markdown','latex','bibtex','rich-text')]
                    self.assertTrue(all(e['citation_targets']==exports[0]['citation_targets'] for e in exports))
                    for item in exports: validate(item,'ParagraphExport')
                    self.assertIn('Development simulation',exports[0]['content'])
                    with self.assertRaises(Exception): owned(tx,'object',package['selection']['knowledge_gap_id'],uid())

    def test_recovered_cancellation_captures_then_deletes_existing_box_without_publish(self):
        from test_box_execution import FakeAdapter,FakeFactory
        import json,time
        with tempfile.TemporaryDirectory() as temp:
            repo=Repository(str(Path(temp)/'application.db')); repo.migrate(); user=uid()
            package=fixtures.EvidencePackageTests.built.package
            source=fixtures.EvidencePackageTests.root/'capture/package/evidence-package.json'
            frozen={'id':uid(),'question_id':package['selection']['knowledge_gap_id'],'composer':{'selected_kgs':[]},
                'attribution':{'user_id':user,'principal_kind':'anonymous','display_name':None,'orcid':None,'orcid_authenticated':False}}
            with repo.transaction() as tx:
                tx.put('request',frozen['id'],user,frozen); tx.put('request_binding',frozen['id'],user,{})
                job=jobs.enqueue(tx,user,'analysis',request_id=frozen['id'])
            source=Path(temp)/job['id']/'evidence/package/evidence-package.json'; source.parent.mkdir(parents=True)
            source.write_bytes(fixtures.canonical_json(package))
            _,first=jobs.claim(repo,'old-worker')
            with repo.transaction() as tx:
                queue=tx.get('queue',job['id'])['data']; queue['lease_until']='2000-01-01T00:00:00Z'
                queue['remote_handle']={'job_id':job['id'],'attempt':first['attempt'],'box_id':'test-box','cursor':1,'phase':'running','created_at':time.time()}
                queue['dispatch_input']={'path':'evidence/package/evidence-package.json','sha256':sha256(source.read_bytes()),'kind':'analysis','mode':'box','model':'claude-sonnet-4-6'}
                tx.put('queue',job['id'],user,queue); jobs.cancel(tx,tx.get('job',job['id'])['data'])
            claimed=jobs.claim(repo,'recovered-worker'); self.assertEqual(claimed[0]['status'],'cancel_requested')
            adapter=FakeAdapter(fixtures.ROOT,environ={'ANTHROPIC_API_KEY':'secret-a','UPSTASH_BOX_API_KEY':'secret-b'},box_factory=FakeFactory,poll_interval=0)
            adapter.calls=[]; FakeFactory.created=FakeFactory.retrieved=0; original=adapter.remote
            async def remote(box,action,*args):
                if action=='poll':
                    adapter.calls.append(action)
                    return json.dumps({'state':{'status':'cancelled'},'events':[],'cursor':1,'has_more':False})
                if action=='collect': self.assertFalse(box.deleted)
                return await original(box,action,*args)
            adapter.remote=remote
            with patch.dict('os.environ',{'REVEAL_EXECUTION_MODE':'box','REVEAL_ARTIFACTS_DIR':temp}), patch('reveal_backend.worker.collect',side_effect=AssertionError('Recovery must not collect again')),patch('reveal_backend.worker.fit_input_budget',side_effect=AssertionError('Recovery must reuse frozen measured bytes')):
                asyncio.run(Worker(repo,adapter).process(*claimed))
            self.assertEqual(adapter.calls,['cancel','poll','collect']); self.assertTrue(FakeFactory.instance.deleted)
            self.assertEqual(FakeFactory.created,0); self.assertEqual(FakeFactory.retrieved,1)
            with repo.transaction() as tx:
                self.assertEqual(tx.get('job',job['id'])['data']['status'],'cancelled')
                self.assertEqual(tx.get('queue',job['id'])['data']['remote_handle']['phase'],'deleted')
                self.assertEqual(tx.list('account'),[])

if __name__=='__main__': unittest.main()
