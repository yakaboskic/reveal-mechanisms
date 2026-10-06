"""Behavioral tests for credentials, ownership, retries, leases and cancellation."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import jwt
from fastapi.testclient import TestClient
from reveal_backend import app as api
from reveal_backend import jobs
from reveal_backend.repository import Repository, Transaction, now, uid, digest
from reveal_backend.auth import Problem

COMPOSER={'source_gap':None,'eaggl_anchors':[],'dismissed_source_ids':[],'mechanism_subquery':'','model':'cfde-inc-v2','selected_kgs':[]}

class NoSources:
    def validate_composer(self,composer,submit=False):
        if submit: raise Problem(422,'ANCHOR_REQUIRED','Select an anchor.')

class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo=Repository(str(Path(self.temp.name)/'app.sqlite')); self.repo.migrate()
        self.environment=patch.dict(os.environ,{'REVEAL_GATEWAY_SECRET':'s'*40,'REVEAL_GATEWAY_SERVICE_TOKEN':'t'*40,'REVEAL_GATEWAY_ISSUER':'reveal-nextjs','REVEAL_GATEWAY_AUDIENCE':'reveal-api'})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.repo_patch=patch.object(api,'repo',self.repo); self.repo_patch.start(); self.addCleanup(self.repo_patch.stop)
        self.catalog_patch=patch.object(api,'catalog',NoSources()); self.catalog_patch.start(); self.addCleanup(self.catalog_patch.stop)
        self.client=TestClient(api.app)
    def provision(self):
        result=self.client.post('/internal/v1/principals/anonymous',json={},headers={'Authorization':'Bearer '+'t'*40,'Idempotency-Key':uid()})
        self.assertEqual(result.status_code,201,result.text)
        return result.json()['user_id']
    def token(self,user,kind='anonymous',**extra):
        claims={'sub':user,'principal_kind':kind,'iss':'reveal-nextjs','aud':'reveal-api','iat':int(time.time()),'exp':int(time.time())+120,'jti':uid(),**extra}
        return jwt.encode(claims,'s'*40,algorithm='HS256')
    def headers(self,user,**extra): return {'Authorization':'Bearer '+self.token(user),'Idempotency-Key':uid(),**extra}
    def draft(self,user):
        result=self.client.post('/v1/drafts',json={'composer':COMPOSER},headers=self.headers(user)); self.assertEqual(result.status_code,201,result.text); return result.json()
    def test_ownership_and_missing_forged_expired_assertions(self):
        user,other=self.provision(),self.provision(); draft=self.draft(user)
        self.assertEqual(self.client.get('/v1/drafts/'+draft['id'],headers=self.headers(other)).status_code,404)
        self.assertEqual(self.client.get('/v1/me').status_code,401)
        self.assertEqual(self.client.get('/v1/me',headers={'Authorization':'Bearer '+self.token(user,exp=int(time.time())-10)}).status_code,401)
        forged=jwt.encode({'sub':user,'exp':int(time.time())+10},'x'*40,algorithm='HS256')
        self.assertEqual(self.client.get('/v1/me',headers={'Authorization':'Bearer '+forged}).status_code,401)
        self.assertEqual(self.client.post('/internal/v1/principals/anonymous',json={},headers=self.headers(user)).status_code,403)
    def test_idempotent_autosave_and_two_tab_conflict(self):
        user=self.provision(); draft=self.draft(user); headers=self.headers(user)
        body={'expected_version':1,'composer':dict(COMPOSER,mechanism_subquery='new')}
        first=self.client.patch('/v1/drafts/'+draft['id'],json=body,headers=headers)
        again=self.client.patch('/v1/drafts/'+draft['id'],json=body,headers=headers)
        self.assertEqual(first.status_code,200,first.text); self.assertEqual(first.json(),again.json())
        conflict=self.client.patch('/v1/drafts/'+draft['id'],json=body,headers=self.headers(user))
        self.assertEqual(conflict.status_code,409); self.assertEqual(conflict.json()['current_version'],2)
        changed=dict(body,composer=dict(COMPOSER,mechanism_subquery='different'))
        self.assertEqual(self.client.patch('/v1/drafts/'+draft['id'],json=changed,headers=headers).json()['code'],'IDEMPOTENCY_CONFLICT')
    def test_empty_draft_never_dispatches(self):
        user=self.provision(); draft=self.draft(user)
        response=self.client.post('/v1/jobs',json={'kind':'analysis','draft_id':draft['id'],'draft_version':1},headers=self.headers(user))
        self.assertEqual(response.status_code,422)
        with self.repo.transaction() as tx: self.assertEqual(tx.list('job'),[])

    def test_named_draft_rename_preserves_composer_and_autosave_preserves_name(self):
        user,other=self.provision(),self.provision()
        created=self.client.post('/v1/drafts',json={'composer':COMPOSER,'name':'  Modifier hypothesis  '},headers=self.headers(user))
        self.assertEqual(created.status_code,201,created.text); draft=created.json(); route='/v1/drafts/'+draft['id']
        self.assertEqual(draft['name'],'Modifier hypothesis')
        body={'expected_version':1,'name':'Revised hypothesis'}; headers=self.headers(user)
        self.assertEqual(self.client.patch(route,json=body,headers=self.headers(other)).status_code,404)
        with patch.object(api,'freeze_draft_bindings',side_effect=AssertionError('Rename must not re-resolve saved scientific sources')):
            renamed=self.client.patch(route,json=body,headers=headers)
            self.assertEqual(renamed.status_code,200,renamed.text)
            self.assertEqual(self.client.patch(route,json=body,headers=headers).json(),renamed.json())
        self.assertEqual(renamed.json()['composer'],COMPOSER)
        self.assertEqual(renamed.json()['version'],2)
        self.assertEqual(self.client.patch(route,json=body,headers=self.headers(user)).status_code,409)
        saved=self.client.patch(route,json={'expected_version':2,'composer':dict(COMPOSER,mechanism_subquery='BMPR2')},headers=self.headers(user))
        self.assertEqual(saved.status_code,200,saved.text); self.assertEqual(saved.json()['name'],'Revised hypothesis')
        for invalid in ({'expected_version':3},{'expected_version':3,'name':'   '},{'expected_version':3,'name':'x'*121}):
            self.assertEqual(self.client.patch(route,json=invalid,headers=self.headers(user)).status_code,422)
        self.assertEqual(self.client.get('/v1/drafts',headers=self.headers(user)).json()['items'][0]['name'],'Revised hypothesis')

    def test_draft_delete_checks_owner_version_and_replays_acknowledgment(self):
        user,other=self.provision(),self.provision(); draft=self.draft(user); route='/v1/drafts/'+draft['id']
        delete=lambda body,headers:self.client.request('DELETE',route,json=body,headers=headers)
        self.assertEqual(delete({'expected_version':1},self.headers(other)).status_code,404)
        self.assertEqual(delete({'expected_version':2},self.headers(user)).status_code,409)
        self.assertEqual(delete({},self.headers(user)).status_code,422)
        headers=self.headers(user); result=delete({'expected_version':1},headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(delete({'expected_version':1},headers).json(),result.json())
        self.assertEqual(self.client.get(route,headers=self.headers(user)).status_code,404)
        self.assertEqual(self.client.patch(route,json={'expected_version':1,'composer':COMPOSER},headers=self.headers(user)).status_code,404)
        self.assertEqual(self.client.get('/v1/drafts',headers=self.headers(user)).json()['items'],[])
        with self.repo.transaction() as tx: self.assertIsNone(tx.get('draft_binding',draft['id']))

    def test_delete_preserves_active_frozen_research_and_results(self):
        for status in ('queued','running','cancel_requested'):
            user=self.provision(); draft=self.draft(user); request_id=uid(); account_id=uid(); outcome_id=uid()
            with self.repo.transaction() as tx:
                tx.put('request',request_id,user,{'id':request_id,'source_draft_id':draft['id'],'composer':COMPOSER})
                tx.put('request_binding',request_id,user,{'preserved':'evidence'})
                tx.put('account',account_id,user,{'preserved':'account'})
                tx.put('analysis_outcome',outcome_id,user,{'preserved':'exploration'})
                job=jobs.enqueue(tx,user,'analysis',request_id=request_id)
                job['status']=status; tx.put('job',job['id'],user,job)
            result=self.client.request('DELETE','/v1/drafts/'+draft['id'],json={'expected_version':1},headers=self.headers(user))
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(self.client.get('/v1/research-requests/'+request_id,headers=self.headers(user)).json()['composer'],COMPOSER)
            with self.repo.transaction() as tx:
                for kind,identity in [('request',request_id),('request_binding',request_id),('job',job['id']),('account',account_id),('analysis_outcome',outcome_id)]:
                    self.assertIsNotNone(tx.get(kind,identity),kind)
                self.assertEqual(tx.get('job',job['id'])['data']['status'],status)

    def test_deleted_draft_repoints_gap_then_clears_last_draft_without_removing_gap(self):
        user=self.provision(); first,second=self.draft(user),self.draft(user); gap_id='dapper:KnowledgeGap.'+'g'*32
        with self.repo.transaction() as tx:
            for draft in (first,second):
                draft['composer']['source_gap']={'id':gap_id}; tx.put('draft',draft['id'],user,draft)
            tx.put('exploration','gap-visit',user,{'source_gap':{'id':gap_id},'draft_id':first['id'],'knowledge_gap':{'id':gap_id},'last_explored_at':now()})
        for draft,expected in ((first,second['id']),(second,None)):
            result=self.client.request('DELETE','/v1/drafts/'+draft['id'],json={'expected_version':1},headers=self.headers(user))
            self.assertEqual(result.status_code,200,result.text)
            visits=self.client.get('/v1/me/explorations',headers=self.headers(user)).json()['items']
            self.assertEqual(len(visits),1); self.assertEqual(visits[0]['draft_id'],expected)
    def test_cancel_blocks_stale_result_and_cross_owner_cancel(self):
        user,other=self.provision(),self.provision()
        with self.repo.transaction() as tx: job=jobs.enqueue(tx,user,'analysis',request_id=uid())
        claimed,queue=jobs.claim(self.repo,'worker-1')
        self.assertEqual(self.client.post('/v1/jobs/'+job['id']+'/cancel',headers=self.headers(other)).status_code,404)
        self.assertEqual(self.client.post('/v1/jobs/'+job['id']+'/cancel',headers=self.headers(user)).json()['status'],'cancel_requested')
        self.assertFalse(jobs.finish(self.repo,job['id'],queue['token'],'succeeded',result={'bogus':True}))
        self.assertTrue(jobs.finish(self.repo,job['id'],queue['token'],'cancelled'))
        self.assertIsNone(jobs.claim(self.repo,'worker-2'))
    def test_expired_lease_recovers_remote_handle_and_fences_previous_worker(self):
        user=self.provision()
        with self.repo.transaction() as tx: job=jobs.enqueue(tx,user,'analysis',request_id=uid())
        _,first=jobs.claim(self.repo,'worker-1')
        self.assertIsNone(jobs.claim(self.repo,'worker-2'))
        with self.repo.transaction() as tx:
            queue=tx.get('queue',job['id'])['data']; queue['lease_until']='2000-01-01T00:00:00Z'; queue['remote_handle']={'box_id':'existing-paid-box'}
            tx.put('queue',job['id'],user,queue)
        _,second=jobs.claim(self.repo,'worker-2')
        self.assertEqual(second['remote_handle'],{'box_id':'existing-paid-box'})
        self.assertEqual(second['attempt'],first['attempt'])
        self.assertFalse(jobs.heartbeat(self.repo,job['id'],first['token']))
        self.assertFalse(jobs.finish(self.repo,job['id'],first['token'],'succeeded',result={}))
        self.assertTrue(jobs.heartbeat(self.repo,job['id'],second['token']))
    def test_sse_replay_uses_persisted_sequence(self):
        user=self.provision()
        with self.repo.transaction() as tx:
            job=jobs.enqueue(tx,user,'analysis',request_id=uid()); jobs.cancel(tx,job)
        data=self.client.get('/v1/jobs/'+job['id']+'/events?after=1',headers=self.headers(user)).json()
        self.assertEqual([e['id'] for e in data['items']],['2']); self.assertTrue(data['terminal'])
        stream=self.client.get('/v1/jobs/'+job['id']+'/events',headers=self.headers(user,Accept='text/event-stream',**{'Last-Event-ID':'1'}))
        self.assertIn('id: 2',stream.text); self.assertNotIn('id: 1',stream.text)

    def test_cancelled_paragraph_updates_account_without_deleting_conclusions(self):
        user=self.provision(); account_id='dapper:ScientificAccount.'+'a'*32; key=digest([user,account_id])
        with self.repo.transaction() as tx:
            tx.put('account',key,user,{'result':{'document':{'scientific_accounts':[{'id':account_id,'closing_remarks':'Preserved'}]}},'summary':{}})
            tx.put('account_membership',key,user,{'account_id':account_id,'summary':{}})
            job=jobs.enqueue(tx,user,'paragraph',account_id=account_id); jobs.cancel(tx,job)
            account=tx.get('account',key)['data']
            self.assertEqual(account['result']['research_statement']['status'],'cancelled')
            self.assertEqual(account['result']['document']['scientific_accounts'][0]['closing_remarks'],'Preserved')
    def test_anonymous_upgrade_retains_owned_draft_and_attribution(self):
        user=self.provision(); draft=self.draft(user)
        identity={'issuer':'https://accounts.google.com','subject':'subject-1','display_name':'Researcher','email':None,'email_verified':False,'orcid':None,'orcid_authenticated':False}
        anon=self.token(user,purpose='anonymous_session'); login=self.token('',purpose='verified_identity',verified_identity=identity)
        response=self.client.post('/internal/v1/principals/claim',json={'anonymous_session_assertion':anon,'verified_login_assertion':login,'consent':True},headers={'Authorization':'Bearer '+'t'*40,'Idempotency-Key':uid()})
        self.assertEqual(response.status_code,200,response.text); self.assertEqual(response.json()['user_id'],user)
        self.assertEqual(self.client.get('/v1/drafts/'+draft['id'],headers=self.headers(user)).status_code,401)
        result=self.client.get('/v1/drafts/'+draft['id'],headers={'Authorization':'Bearer '+self.token(user,'registered')})
        self.assertEqual(result.status_code,200)

    def test_transferred_artifact_rekeys_access_and_rejects_old_owner(self):
        source,target=self.provision(),self.provision(); data=b'exact captured response'; checksum=__import__('hashlib').sha256(data).hexdigest()
        path=Path(self.temp.name)/'source.json'; path.write_bytes(data)
        with self.repo.transaction() as tx:
            tx.put('artifact',digest([source,checksum]),source,{'sha256':checksum,'path':str(path),'file':{'filename':'source.json','mime_type':'application/json'}})
            gap_id='dapper:KnowledgeGap.'+'g'*32
            tx.put('exploration',digest([source,gap_id]),source,{'source_gap':{'source_id':'dismech:gap','object_id':gap_id,'source_revision':'revision'},'knowledge_gap':{'id':gap_id}})
            tx.transfer(source,target)
            self.assertIsNone(tx.get('artifact',digest([source,checksum])))
            self.assertIsNone(tx.get('exploration',digest([source,gap_id])))
            self.assertEqual(tx.get('exploration',digest([target,gap_id]))['owner'],target)
        with patch.object(api,'artifacts_root',return_value=Path(self.temp.name).resolve()):
            self.assertEqual(self.client.get('/v1/artifacts/'+checksum,headers=self.headers(source)).status_code,404)
            response=self.client.get('/v1/artifacts/'+checksum,headers=self.headers(target))
            self.assertEqual(response.status_code,200,response.text); self.assertEqual(response.content,data)
            path.write_bytes(b'mutated')
            self.assertEqual(self.client.get('/v1/artifacts/'+checksum,headers=self.headers(target)).status_code,503)

    def test_event_pagination_and_cursor_conflict(self):
        user=self.provision()
        with self.repo.transaction() as tx:
            job=jobs.enqueue(tx,user,'analysis',request_id=uid()); jobs.cancel(tx,job)
        first=self.client.get('/v1/jobs/'+job['id']+'/events?limit=1',headers=self.headers(user)).json()
        self.assertEqual(len(first['items']),1); self.assertFalse(first['terminal'])
        last=self.client.get('/v1/jobs/'+job['id']+'/events?after='+first['next_after'],headers=self.headers(user)).json()
        self.assertTrue(last['terminal']); self.assertEqual(last['items'][0]['id'],'2')
        self.assertEqual(self.client.get('/v1/jobs/'+job['id']+'/events?after=1',headers=self.headers(user,**{'Last-Event-ID':'2'})).status_code,400)

    def test_indexed_event_pages_preserve_numeric_replay_and_owner_isolation(self):
        user,other=self.provision(),self.provision()
        with self.repo.transaction() as tx:
            job=jobs.enqueue(tx,user,'analysis',request_id=uid())
            unrelated=jobs.enqueue(tx,user,'analysis',request_id=uid())
            private=jobs.enqueue(tx,other,'analysis',request_id=uid())
            for number in range(12): jobs.event(tx,job,'activity','Chunk '+str(number))
            jobs.cancel(tx,job)
        route='/v1/jobs/'+job['id']+'/events'
        # A list fallback would read every event for the workspace. This path
        # must use its bounded key-range query for every replay page.
        with patch.object(Transaction,'list',side_effect=AssertionError('Unexpected full history scan')),patch.object(self.repo,'transaction',side_effect=AssertionError('Read took write mutex')):
            first=self.client.get(route+'?after=8&limit=3',headers=self.headers(user))
            self.assertEqual(first.status_code,200,first.text)
            self.assertEqual([row['id'] for row in first.json()['items']],['9','10','11'])
            self.assertFalse(first.json()['terminal'])
            last=self.client.get(route+'?after='+first.json()['next_after']+'&limit=3',headers=self.headers(user)).json()
            self.assertEqual([row['id'] for row in last['items']],['12','13','14'])
            self.assertTrue(last['terminal'])
            replay=self.client.get(route+'?after=8&limit=3',headers=self.headers(user)).json()
            self.assertEqual(replay,first.json())
            self.assertEqual(self.client.get(route,headers=self.headers(other)).status_code,404)
            self.assertEqual(self.client.get('/v1/jobs/'+private['id']+'/events',headers=self.headers(user)).status_code,404)
            self.assertEqual(self.client.get('/v1/jobs/'+job['id'],headers=self.headers(user)).status_code,200)
            self.assertEqual(self.repo.readiness()['database'],'sqlite-test')

    def test_provenance_pages_bind_owner_filters_and_immutable_document(self):
        root=Path(__file__).resolve().parents[3]
        result=next(iter(json.loads((root/'api/examples/getAccount.request.json').read_text())['responses']['200']['examples'].values()))
        user,other=self.provision(),self.provision(); identity=result['root_id']; checksum=digest(result['document'])
        with self.repo.transaction() as tx:
            tx.put('account',digest([user,identity]),user,{'result':result,'summary':{}})
            tx.put('object_document',digest([user,identity]),user,{'object_id':identity,'sha256':checksum})
            tx.put('scientific_document',digest([user,checksum]),user,{'document':result['document']})
        route='/v1/accounts/'+identity
        first=self.client.get(route+'?max_nodes=2',headers=self.headers(user)); self.assertEqual(first.status_code,200,first.text)
        cursor=first.json()['coverage']['next_cursor']; self.assertTrue(cursor)
        self.assertEqual(self.client.get(route,params={'cursor':cursor},headers=self.headers(other)).status_code,404)
        self.assertEqual(self.client.get(route,params={'cursor':cursor,'max_nodes':3},headers=self.headers(user)).status_code,409)
        self.assertEqual(self.client.get(route,params={'cursor':cursor+'x'},headers=self.headers(user)).status_code,409)
        seen={p['object_id'] for p in first.json()['payloads']}; count=0
        while cursor:
            response=self.client.get(route,params={'cursor':cursor},headers=self.headers(user)); self.assertEqual(response.status_code,200,response.text)
            value=response.json(); api.validate(value,'AccountResult')
            ids={p['object_id'] for p in value['payloads']}; self.assertEqual(ids&seen,{identity}); self.assertLessEqual(len(ids),2)
            seen|=ids; cursor=value['coverage']['next_cursor']; count+=1; self.assertLess(count,100)
        whole=self.client.get(route,headers=self.headers(user)).json()
        self.assertEqual(seen,{p['object_id'] for p in whole['payloads']})
        cursor=first.json()['coverage']['next_cursor']
        with self.repo.transaction() as tx:
            changed=deepcopy(result['document']); changed['scientific_accounts'][0]['closing_remarks']='Changed observation'
            tx.put('scientific_document',digest([user,checksum]),user,{'document':changed})
        self.assertEqual(self.client.get(route,params={'cursor':cursor},headers=self.headers(user)).status_code,409)

    def test_public_suggestion_audit_has_nonnullable_system_owner(self):
        root=Path(__file__).resolve().parents[3]
        body=json.loads((root/'api/examples/suggestMechanisms.dismech_context.json').read_text())['request']['body']
        gap=next(iter(json.loads((root/'api/examples/getKnowledgeGap.request.json').read_text())['responses']['200']['examples'].values()))
        class Source:
            mechanisms={}; embedding_run='embedding'; mapping_run='mapping'
            def selected(self,reference): return gap
            def suggest_factors(self,*args,**kwargs): return []
            def context_embedding_provenance(self,*args): return {'dismech_embedding_run_id':'context-run'}
            def provenance(self,*args): return {'query':'','mode':'semantic','corpus_snapshot':'mapping','embedding_model':'model','embedding_revision':'embedding','template_version':'test','score_aggregation':'maximum_per_context'}
        with patch.object(api,'catalog',Source()):
            response=self.client.post('/v1/mechanisms/suggest',json=body)
        self.assertEqual(response.status_code,200,response.text)
        with self.repo.transaction() as tx:
            row=tx.get('suggestion',response.json()['suggestion_id']); self.assertEqual(row['owner'],'catalog')

    def test_mapping_switch_preserves_saved_selection_runs(self):
        root=Path(__file__).resolve().parents[3]
        composer=json.loads((root/'api/examples/createDraft.question_and_anchor.json').read_text())['request']['body']['composer']
        gap=next(iter(json.loads((root/'api/examples/getKnowledgeGap.request.json').read_text())['responses']['200']['examples'].values()))
        factor=next(iter(json.loads((root/'api/examples/getMechanism.request.json').read_text())['responses']['200']['examples'].values()))
        class Source:
            dismech_import='original-dismech'
            factors={factor['source_id']:factor}
            bindings={factor['source_id']:{'reference_generation_id':digest('fixture'),'mapping_run_id':'original-mapping','embedding_run_id':'original-embedding','eaggl_import_id':'original-source','cfde_node_id':factor['source_id']}}
            def selected(self,reference): return gap
            def validate_composer(self,composer,submit=False): return gap
        source=Source(); user=self.provision()
        with patch.object(api,'catalog',source):
            response=self.client.post('/v1/drafts',json={'composer':composer},headers=self.headers(user)); self.assertEqual(response.status_code,201,response.text)
            draft=response.json()
            source.bindings={factor['source_id']:{'mapping_run_id':'different-mapping'}}
            response=self.client.post('/v1/jobs',json={'kind':'analysis','draft_id':draft['id'],'draft_version':1},headers=self.headers(user))
            self.assertEqual(response.status_code,202,response.text)
            with self.repo.transaction() as tx:
                binding=tx.get('request_binding',response.json()['research_request_id'])['data']
                self.assertEqual(binding['anchors'][0]['mapping_run_id'],'original-mapping')
                self.assertEqual(binding['anchors'][0]['embedding_run_id'],'original-embedding')

    def test_pairwise_suggestion_scores_survive_draft_and_request_freezing(self):
        root=Path(__file__).resolve().parents[3]
        body=json.loads((root/'api/examples/suggestMechanisms.dismech_context.json').read_text())['request']['body']
        composer=json.loads((root/'api/examples/createDraft.question_and_anchor.json').read_text())['request']['body']['composer']
        gap=next(iter(json.loads((root/'api/examples/getKnowledgeGap.request.json').read_text())['responses']['200']['examples'].values()))
        factor=next(iter(json.loads((root/'api/examples/getMechanism.request.json').read_text())['responses']['200']['examples'].values()))
        pair_scores={'dismech:first':.6, 'dismech:second':-.2}
        class Source:
            mechanisms={}; dismech_import='dismech'; embedding_run='embedding'; mapping_run='mapping'
            factors={factor['source_id']:factor}
            bindings={factor['source_id']:{'reference_generation_id':digest('fixture'),'cfde_node_id':factor['source_id'], 'embedding_run_id':'embedding', 'mapping_run_id':'mapping'}}
            def selected(self,reference): return gap
            def validate_composer(self,composer,submit=False): return gap
            def suggest_factors(self,*args,**kwargs):
                return [{'record':factor, 'ranking':{'value':.6,'rank':1,'metric':'cosine_similarity'},
                         'contexts':['dismech:first'], 'context_similarities':pair_scores}]
            def context_embedding_provenance(self,*args): return {'dismech_embedding_run_id':'context-run'}
            def provenance(self,*args): return {}
        user=self.provision()
        with patch.object(api,'catalog',Source()):
            response=self.client.post('/v1/mechanisms/suggest',json=body)
            self.assertEqual(response.status_code,200,response.text)
            composer['eaggl_anchors'][0].update(origin='automatic',suggestion_id=response.json()['suggestion_id'])
            response=self.client.post('/v1/drafts',json={'composer':composer},headers=self.headers(user))
            self.assertEqual(response.status_code,201,response.text)
            draft=response.json()
            response=self.client.post('/v1/jobs',json={'kind':'analysis','draft_id':draft['id'],'draft_version':draft['version']},headers=self.headers(user))
            self.assertEqual(response.status_code,202,response.text)
        with self.repo.transaction() as tx:
            binding=tx.get('request_binding',response.json()['research_request_id'])['data']
        self.assertEqual(binding['retrieval'][factor['source_id']]['hit']['context_similarities'],pair_scores)

if __name__=='__main__': unittest.main()
