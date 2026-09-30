"""Captured bytes unblock science; deletion remains independently durable."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt

from reveal_backend import jobs, workflow_state as state, durable_review
from reveal_backend.box_adapter import CAPTURE_MARKER, capture_binding, atomic_capture_marker, BoxTransportError
from reveal_backend.box_mcp import Ledger
from reveal_backend.evidence_package import sha256
from reveal_backend.repository import Repository, Transaction
from reveal_backend.review_retry import enqueue_review, prepare_source
from reveal_backend.workflow_execution import WorkflowExecution
from reveal_backend.workflow_routes import dispatch_cleanup, cleanup_pending, mount_workflow, CLEANUP_PATH, CONTROL_PATH
from test_durable_workflow import MemoryStore

KEY='cleanup-test-signing-key-32-bytes'
BASE='https://workflow.invalid'


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo=Repository(Path(self.temp.name)/'db.sqlite'); self.repo.migrate()
        self.env=patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT':'workflow','REVEAL_JOB_NAMESPACE':'cleanup-test',
            'REVEAL_ENVIRONMENT':'test','QSTASH_TOKEN':'fake','QSTASH_CURRENT_SIGNING_KEY':KEY,
            'QSTASH_NEXT_SIGNING_KEY':KEY,'REVEAL_WORKFLOW_URL':BASE+'/internal/workflows/research-v1'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.store=MemoryStore(); self.adapter=Mock()
        self.adapter.capture_once=AsyncMock(side_effect=self.capture)
        self.adapter.delete_once=AsyncMock(side_effect=lambda box:dict(box,phase='deleted'))
        self.engine=WorkflowExecution(self.repo,storage=self.store,adapter=self.adapter)

    async def capture(self,request,handle):
        request.output_dir.mkdir(parents=True,exist_ok=True)
        (request.output_dir/'output').mkdir(exist_ok=True)
        content=b'{"segments":[]}'
        (request.output_dir/'output/paragraph.json').write_bytes(content)
        marker={'format':'reveal.box-capture/1','binding':capture_binding(request,handle),
            'state':{'status':'succeeded'},'cleanup_complete':False,
            'files':{'output/paragraph.json':{'sha256':sha256(content),'size_bytes':len(content)}}}
        atomic_capture_marker(request.output_dir/CAPTURE_MARKER,marker)
        return dict(handle,phase='captured')

    def seed(self):
        with self.repo.transaction() as tx:
            job=jobs.enqueue(tx,'owner','paragraph',account_id='account:original')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'input.json').write_bytes(b'{}'); ref=self.store.snapshot(root)
        with self.repo.transaction() as tx:
            row=tx.get('execution',job['id']); value=row['data']
            value.update(phase='capture',workspace=ref,capacity_reserved=True,
                box={'box_id':'box-'+job['id'],'job_id':job['id'],'attempt':1,'phase':'terminal','state':{'status':'succeeded'}})
            tx.put('execution',job['id'],row['owner'],value)
            queue=tx.get('queue',job['id'])['data']; queue.update(workspace=ref,dispatch_input={
                'path':'input.json','sha256':sha256(b'{}'),'kind':'paragraph','mode':'box','model':'original-model'})
            tx.put('queue',job['id'],'owner',queue)
        return job,{'job_id':job['id'],'namespace':'cleanup-test','generation':1}

    def rows(self,job):
        with self.repo.read_transaction() as tx:
            return {kind:tx.get(kind,job['id'])['data'] for kind in ('job','execution','queue')}

    async def handed_off(self):
        job,payload=self.seed(); result=await self.engine.step(payload,0)
        return job,payload,result,{'cleanup_id':result['cleanup_id'],'namespace':'cleanup-test'}

    async def test_capture_commits_intent_and_advances_without_delete_or_faking_marker(self):
        job,payload,result,cleanup=await self.handed_off()
        self.assertEqual(result['phase'],'validate'); self.adapter.delete_once.assert_not_awaited()
        rows=self.rows(job)
        with self.repo.read_transaction() as tx:
            intent=tx.get('workflow_cleanup',cleanup['cleanup_id'])['data']
        self.assertEqual(intent['workspace'],rows['execution']['workspace'])
        self.assertEqual(intent['capture_sha256'],rows['queue']['review_capture']['capture_sha256'])
        self.assertTrue(rows['execution']['capacity_reserved']); self.assertFalse(rows['execution']['cleanup_complete'])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); self.store.restore(intent['workspace'],root)
            request,_=self.engine.request(rows['job'],rows['queue'],rows['execution'],root)
            marker=self.engine.verified_capture(payload,rows['execution'],request)
            self.assertFalse(marker['cleanup_complete'])
            path=request.output_dir/CAPTURE_MARKER; value=json.loads(path.read_bytes()); value['cleanup_complete']=True
            atomic_capture_marker(path,value)
            with self.assertRaisesRegex(ValueError,'binding differs'):
                self.engine.verified_capture(payload,rows['execution'],request)
            with self.assertRaisesRegex(ValueError,'binding differs'):
                await self.engine.validate(payload,'unused',rows['job'],rows['queue'],
                    {**rows['execution'],'validated_paths':['already-validated.json']},root,request,{})
        self.assertEqual(await self.engine.step(payload,0),result)
        self.assertEqual(self.adapter.capture_once.await_count,1)

    async def test_direct_capture_skips_temp_directory_restore_and_local_request(self):
        job,payload=self.seed(); rows=self.rows(job)
        with self.repo.transaction() as tx:
            value=rows['execution']; value['box']['capture_protocol']='s3-v1'
            tx.put('execution',job['id'],'owner',value)
            queue=rows['queue']; queue['dispatch_input']['selected_graphs']=[]
            tx.put('queue',job['id'],'owner',queue)
        captured_box=dict(value['box'],phase='captured')
        self.adapter.capture_to_store=AsyncMock(return_value={'box':captured_box,
            'workspace':{'immutable':'captured-reference'},'capture_sha256':'a'*64})
        with (patch('reveal_backend.workflow_execution.tempfile.TemporaryDirectory',side_effect=AssertionError('No capture scratch')),
              patch.object(self.engine,'request',side_effect=AssertionError('No local input materialization'))):
            result=await self.engine.step(payload,0)
        self.assertEqual(result['phase'],'validate'); self.adapter.capture_once.assert_not_awaited()
        binding,handle,storage,reference=self.adapter.capture_to_store.call_args.args
        self.assertEqual(binding,{'job_id':job['id'],'attempt':1,'kind':'paragraph','box_id':value['box']['box_id'],
            'selected_graphs':[],'input_sha256':sha256(b'{}')})
        self.assertIs(storage,self.store); self.assertEqual(reference,rows['execution']['workspace'])
        with self.repo.read_transaction() as tx:
            intent=tx.get('workflow_cleanup',result['cleanup_id'])['data']
        self.assertEqual(intent['workspace'],{'immutable':'captured-reference'})
        self.assertEqual(self.rows(job)['queue']['review_capture']['capture_sha256'],'a'*64)

    async def test_capture_transaction_failure_does_not_leave_cleanup_or_review_source(self):
        job,payload=self.seed(); original=Transaction.put; failed=False
        def put(tx,kind,identity,owner,data,expected=None):
            nonlocal failed
            if kind=='queue' and data.get('review_capture') and not failed:
                failed=True; raise OSError('lost capture commit')
            return original(tx,kind,identity,owner,data,expected)
        with patch.object(Transaction,'put',put),self.assertRaises(OSError): await self.engine.step(payload,0)
        rows=self.rows(job)
        self.assertFalse(rows['execution']['capture_complete']); self.assertNotIn('review_capture',rows['queue'])
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('workflow_cleanup'),[])
        self.adapter.delete_once.assert_not_awaited()

    async def test_cancel_after_handoff_finishes_without_paid_work_or_waiting_for_cleanup(self):
        job,payload,result,cleanup=await self.handed_off()
        with self.repo.transaction() as tx: jobs.cancel(tx,tx.get('job',job['id'])['data'])
        with patch.object(self.engine,'review',AsyncMock(side_effect=AssertionError('no paid review'))):
            terminal=await self.engine.step(payload,result['index'])
        self.assertTrue(terminal['done']); self.assertEqual(self.rows(job)['job']['status'],'cancelled')
        self.assertTrue(self.rows(job)['execution']['capacity_reserved'])
        self.adapter.delete_once.assert_not_awaited()
        await self.engine.cleanup(cleanup)
        self.assertFalse(self.rows(job)['execution']['capacity_reserved'])

    async def test_terminal_review_retry_preserves_capacity_and_late_ack_does_not_reset_main_fence(self):
        job,payload,result,cleanup=await self.handed_off()
        _,execution,_=state.acquire(self.repo,payload,result['index']); token=execution['fence']
        jobs.finish(self.repo,job['id'],token,'failed',failure={'code':'REVIEW_UNAVAILABLE','retryable':True,'message':'No verdict'})
        state.complete(self.repo,payload,token,next_phase='complete',done=True)
        with patch('reveal_backend.artifact_store.s3_enabled',return_value=True),self.repo.transaction() as tx:
            current=tx.get('job',job['id'])['data']; enqueue_review(tx,current,current['last_event_id'])
        current=self.rows(job); self.assertEqual(current['execution']['generation'],2)
        self.assertEqual(current['execution']['cleanup_id'],cleanup['cleanup_id'])
        self.assertTrue(current['execution']['capacity_reserved']); self.assertFalse(current['execution']['cleanup_complete'])
        _,active,_=state.acquire(self.repo,{**payload,'generation':2},0)
        await self.engine.cleanup(cleanup)
        after=self.rows(job)
        self.assertEqual(after['execution']['generation'],2); self.assertEqual(after['execution']['phase'],'validate')
        self.assertEqual(after['execution']['fence'],active['fence']); self.assertEqual(after['job']['status'],'queued')
        self.assertFalse(after['execution']['capacity_reserved']); self.assertTrue(after['execution']['cleanup_complete'])
        self.assertEqual(after['queue']['review_source']['capture_sha256'],current['queue']['review_source']['capture_sha256'])
        await self.engine.cleanup(cleanup); self.assertEqual(self.adapter.delete_once.await_count,1)

    async def test_cleanup_failure_survives_terminal_job_and_holds_capacity_until_ack(self):
        job,payload,result,cleanup=await self.handed_off()
        with self.repo.transaction() as tx:
            current=tx.get('job',job['id'])['data']; current.update(status='failed',failure={'code':'VALIDATION_FAILED'})
            tx.put('job',job['id'],'owner',current)
        await self.engine.step(payload,result['index'])
        self.adapter.delete_once.side_effect=BoxTransportError('provider temporarily unavailable')
        with self.assertRaises(BoxTransportError): await self.engine.cleanup(cleanup)
        other,other_payload=self.seed()
        with self.repo.transaction() as tx:
            value=tx.get('execution',other['id'])['data']; value.update(phase='create',capacity_reserved=False)
            tx.put('execution',other['id'],'owner',value)
        _,active,_=state.acquire(self.repo,other_payload,0)
        with patch.dict('os.environ',{'REVEAL_MAX_ACTIVE_BOXES':'1'}),self.assertRaises(state.StepBusy):
            state.reserve_box(self.repo,other_payload,active['fence'])
        self.adapter.delete_once.side_effect=lambda box:dict(box,phase='deleted')
        await self.engine.cleanup(cleanup)
        with patch.dict('os.environ',{'REVEAL_MAX_ACTIVE_BOXES':'1'}): state.reserve_box(self.repo,other_payload,active['fence'])
        self.assertEqual(self.rows(job)['job']['status'],'failed')

    async def test_lost_delete_ack_and_owner_transfer_do_not_restore_old_owner(self):
        job,payload,result,cleanup=await self.handed_off()
        intent=state.acquire_cleanup(self.repo,cleanup)
        # Provider deletion succeeded, then the process died before DB ack.
        handle=await self.adapter.delete_once(intent['box'])
        with self.repo.transaction() as tx:
            tx.transfer('owner','new-owner')
            row=tx.get('workflow_cleanup',cleanup['cleanup_id']); value=row['data']; value['lease_until']=''
            tx.put('workflow_cleanup',cleanup['cleanup_id'],row['owner'],value)
        await self.engine.cleanup(cleanup)
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('workflow_cleanup',cleanup['cleanup_id']))
            self.assertEqual(tx.get('workflow_cleanup_completed',cleanup['cleanup_id'])['owner'],'new-owner')
            self.assertEqual(tx.get('execution',job['id'])['owner'],'new-owner')
        self.assertEqual(self.adapter.delete_once.await_count,2)

    async def test_cleanup_dispatch_repairs_publish_gap_and_filters_future_leased_other_namespace(self):
        job,payload,result,cleanup=await self.handed_off()
        with self.repo.transaction() as tx:
            template=tx.get('workflow_cleanup',cleanup['cleanup_id'])['data']
            for index in range(30):
                future=deepcopy(template); future.update(next_attempt_at=state.after(500),namespace='cleanup-test')
                tx.put('workflow_cleanup','future-'+str(index),'owner',future)
                leased=deepcopy(template); leased.update(lease_until=state.after(500))
                tx.put('workflow_cleanup','leased-'+str(index),'owner',leased)
            wrong=deepcopy(template); wrong['namespace']='other'
            tx.put('workflow_cleanup','other','owner',wrong)
        self.assertEqual([key for key,_ in cleanup_pending(self.repo,1)],[cleanup['cleanup_id']])
        client=Mock(); client.message.publish_json=AsyncMock(side_effect=[TimeoutError(),{'messageId':'accepted'}])
        self.assertEqual((await dispatch_cleanup(self.repo,qstash=client))['failed'],1)
        self.assertEqual((await dispatch_cleanup(self.repo,qstash=client))['delivered'],1)
        first,second=client.message.publish_json.call_args_list
        self.assertEqual(first.kwargs['deduplication_id'],second.kwargs['deduplication_id'])
        self.assertEqual(second.kwargs['body'],cleanup)
        self.assertEqual(await dispatch_cleanup(self.repo,qstash=client),{'delivered':0,'failed':0})
        with self.repo.transaction() as tx:
            row=tx.get('workflow_cleanup',cleanup['cleanup_id']); row['data']['next_attempt_at']=''
            tx.put('workflow_cleanup',cleanup['cleanup_id'],'owner',row['data'])
        client.message.publish_json.side_effect=None
        self.assertEqual((await dispatch_cleanup(self.repo,qstash=client))['delivered'],1)
        self.assertNotEqual(client.message.publish_json.call_args.kwargs['deduplication_id'],first.kwargs['deduplication_id'])

    def signed(self,payload,path=CLEANUP_PATH):
        body=json.dumps(payload)
        token=jwt.encode({'iss':'Upstash','sub':BASE+path,'exp':int(time.time())+60,'nbf':int(time.time())-1,
            'body':base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest()).decode().rstrip('=')},KEY,algorithm='HS256')
        return body,{'Upstash-Signature':token,'Content-Type':'application/json'}

    async def test_signed_cleanup_route_rejects_wrong_route_namespace_and_unsigned_body(self):
        job,payload,result,cleanup=await self.handed_off()
        app=FastAPI(); mounted=mount_workflow(app,self.repo); mounted.adapter=self.adapter
        mounted.storage=Mock(); mounted.storage.restore.side_effect=AssertionError('Cleanup must not restore files')
        with TestClient(app) as client:
            self.assertEqual(client.post(CLEANUP_PATH,json=cleanup).status_code,401)
            body,headers=self.signed(cleanup,CONTROL_PATH)
            self.assertEqual(client.post(CLEANUP_PATH,content=body,headers=headers).status_code,401)
            body,headers=self.signed([])
            self.assertEqual(client.post(CLEANUP_PATH,content=body,headers=headers).status_code,401)
            body,headers=self.signed({**cleanup,'namespace':'other'})
            self.assertEqual(client.post(CLEANUP_PATH,content=body,headers=headers).json(),{'stale':True})
            self.adapter.delete_once.assert_not_awaited()
            body,headers=self.signed(cleanup)
            self.assertEqual(client.post(CLEANUP_PATH,content=body,headers=headers).json(),{'deleted':True})
            self.assertEqual(client.post(CLEANUP_PATH,content=body,headers=headers).json(),{'deleted':True})
        self.assertEqual(self.adapter.delete_once.await_count,1)

    async def test_review_retry_rejects_changed_cleanup_binding(self):
        job,payload,result,cleanup=await self.handed_off()
        rows=self.rows(job)
        with patch('reveal_backend.artifact_store.s3_enabled',return_value=True),self.repo.transaction() as tx:
            changed=deepcopy(rows['queue']); changed['review_capture']['capture_sha256']='0'*64
            with self.assertRaisesRegex(Exception,'saved authoring output'):
                prepare_source(tx,rows['job'],changed)

    def stored_review(self):
        from reveal_backend.artifact_store import S3Store
        from test_artifact_checkpoints import CountingS3
        job,payload=self.seed(); storage=S3Store('reveal-test-artifacts',client=CountingS3())
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); (root/'review').mkdir()
            ledger=Ledger(root/'ledger','test',1); ledger.freeze()
            review=durable_review.initial('analysis',{'claims':[{'id':'claim:1'}]},
                {'selection':{'knowledge_gap_id':'gap:1'},'dismech':{'observation':'Only association was measured'},
                 'external_evidence':{'selected_graphs':[]}},ledger_path=root/'ledger/manifest.json',budget=1)
            (root/'review/1-0.json').write_text(json.dumps(review))
            (root/'unchanged-source.bin').write_bytes(b'captured source not needed by review call or tools')
            reference=storage.snapshot(root)
        sibling=next(item['storage'] for item in storage.workspace_manifest(reference)['files'] if item['path']=='unchanged-source.bin')
        with self.repo.transaction() as tx:
            value=tx.get('execution',job['id'])['data']
            value.update(phase='review_call',workspace=reference,review_checkpoint='review/1-0.json',
                review_index=0,validated_paths=['validated/account-0.json'],capacity_reserved=False)
            tx.put('execution',job['id'],'owner',value)
            queue=tx.get('queue',job['id'])['data']; queue['workspace']=reference
            tx.put('queue',job['id'],'owner',queue)
        return job,payload,storage,sibling

    async def test_review_call_and_tools_update_only_json_without_any_scratch_or_sibling_download(self):
        job,payload,storage,sibling=self.stored_review()
        engine=WorkflowExecution(self.repo,storage=storage)
        response=Mock(status_code=200)
        response.json.return_value={'stop_reason':'tool_use','content':[{'type':'tool_use','id':'read-1',
            'name':'read_evidence','input':{'pointer':'/package/dismech/observation'}}],
            'usage':{'input_tokens':1000,'output_tokens':100}}
        with (patch.dict('os.environ',{'ANTHROPIC_API_KEY':'fake'}),
              patch('reveal_backend.scientific_grounding.httpx.post',return_value=response) as paid,
              patch('reveal_backend.workflow_execution.tempfile.TemporaryDirectory',side_effect=AssertionError('No review scratch')),
              patch.object(engine,'request',side_effect=AssertionError('No review materialization')),
              patch.object(storage,'restore',side_effect=AssertionError('No full restore')),
              patch.object(storage,'snapshot',side_effect=AssertionError('No full snapshot')),
              patch.object(storage,'get',wraps=storage.get) as get):
            first=await engine.step(payload,0)
            self.assertEqual(first['phase'],'review_tools')
            second=await engine.step(payload,1)
            self.assertEqual(second['phase'],'review_call')
            paid.assert_called_once()
            self.assertNotIn(sibling['key'],[call.args[0]['key'] for call in get.call_args_list])
        execution=self.rows(job)['execution']
        review=json.loads(storage.read_workspace_file(execution['workspace'],'review/1-0.json'))
        self.assertEqual(len(review['session']['calls']),1); self.assertEqual(len(review['reads']),1)
        self.assertIsNone(review['pending']); self.assertIsNone(review['response'])
        unchanged=next(item['storage'] for item in storage.workspace_manifest(execution['workspace'])['files'] if item['path']=='unchanged-source.bin')
        self.assertEqual(unchanged,sibling)

    async def test_metadata_review_keeps_paid_reservation_on_lost_response_and_never_repeats_it(self):
        job,payload,storage,_=self.stored_review(); engine=WorkflowExecution(self.repo,storage=storage)
        with patch.dict('os.environ',{'ANTHROPIC_API_KEY':'fake'}),patch('reveal_backend.scientific_grounding.httpx.post',side_effect=TimeoutError()) as paid:
            result=await engine.step(payload,0)
            self.assertTrue(result['done']); self.assertEqual(await engine.step(payload,0),result)
            paid.assert_called_once()
        execution=self.rows(job)['execution']
        review=json.loads(storage.read_workspace_file(execution['workspace'],'review/1-0.json'))
        self.assertIsNotNone(review['pending']); self.assertEqual(review['failure']['error_type'],'TimeoutError')
        self.assertEqual(self.rows(job)['job']['failure']['code'],'REVIEW_UNAVAILABLE')


if __name__=='__main__': unittest.main()
