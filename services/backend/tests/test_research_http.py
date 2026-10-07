"""Local HTTP lifecycle and the official sessionless MCP client, without source DB or paid agents."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx2
import jwt
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from reveal_backend.auth import Problem
from reveal_backend.repository import Repository,now,uid
from reveal_backend.research_http import register
from reveal_backend.research_work import ResearchWorkService, issue_grant
from reveal_backend.research_seed import prepare_research_seed
from reveal_backend.research_data import QueryCapture
from reveal_backend.research_tools import dispatch
from reveal_backend.evidence_package import canonical_json
from reveal_backend import reference_generation as rg
import test_research_data as seed_fixture

BASE='http://127.0.0.1:18000'


class InlinePreparation(ResearchWorkService):
    """Run only seed preparation inline; other operations remain durable work."""
    def kick(self,work_id):
        with self.repo.read_transaction() as tx:
            pending=[r['id'] for r in tx.list('research_operation')
                     if r['data']['local_work_id']==work_id and r['data']['kind']=='prepare' and r['data']['state']=='received']
        for identity in pending: self.run_operation(identity)

    def resume_operation(self, operation_id, *, lease_token=None):
        with self.repo.read_transaction() as tx:
            row = tx.get('research_operation', operation_id)
        if row and row['data']['kind'] == 'prepare': self.run_operation(operation_id)


class ResearchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo=Repository(str(Path(self.temp.name)/'app.sqlite')); self.repo.migrate()
        self.env=patch.dict(os.environ,{'REVEAL_GATEWAY_SECRET':'s'*40,'REVEAL_PUBLIC_API_URL':BASE,
            'REVEAL_ARTIFACT_STORE':'filesystem','REVEAL_ARTIFACTS_DIR':self.temp.name,
            'REVEAL_NOTIFICATION_REDIS_URL':'','REVEAL_NOTIFICATION_REDIS_REST_URL':'',
            'UPSTASH_REDIS_REST_URL':'','UPSTASH_REDIS_REST_TOKEN':''})
        self.env.start(); self.addCleanup(self.env.stop)
        self.owner,self.other=uid(),uid()
        with self.repo.transaction() as tx:
            for identity in (self.owner,self.other):
                tx.put('principal',identity,identity,{'me':{'user_id':identity,'principal_kind':'registered',
                    'workspace_expires_at':None,'display_name':'Fixture scientist','orcid':None,'orcid_authenticated':False},'retired':False})
        self.app=self.make_app()
        self.client=TestClient(self.app,base_url=BASE)
        self.client.__enter__(); self.addCleanup(self.client.__exit__,None,None,None)

    def make_app(self):
        app=FastAPI()
        @app.exception_handler(Problem)
        async def problem(request,error):
            return JSONResponse({'code':error.code,'detail':error.detail,**error.extra},status_code=error.status)
        def freeze(tx,identity,body):
            if body['draft_version']!=1: raise Problem(409,'VERSION_CONFLICT','Wrong frozen version')
            _,frozen,binding=seed_fixture.SeedTests().inputs()
            frozen.update(id=uid(),owner_user_id=identity['user_id'],source_draft_id=body['draft_id'],source_draft_version=body['draft_version'],submitted_at=now())
            tx.put('request',frozen['id'],identity['user_id'],frozen)
            tx.put('request_binding',frozen['id'],identity['user_id'],binding)
            return frozen,binding
        def gate(tx):
            if rg.read_gate(tx): raise Problem(503,'REFERENCE_RELOAD_IN_PROGRESS','Reference writes paused')
        register(app,lambda:self.repo,freeze=freeze,preload=lambda:None,reload_gate=gate,service_factory=InlinePreparation)
        return app

    def headers(self,owner=None,key=None):
        token=jwt.encode({'sub':owner or self.owner,'principal_kind':'registered','iss':'reveal-nextjs','aud':'reveal-api',
            'iat':int(time.time()),'exp':int(time.time())+120,'jti':uid()},'s'*40,algorithm='HS256')
        return {'Authorization':'Bearer '+token,'Idempotency-Key':key or uid()}

    def create(self,owner=None):
        response=self.client.post('/v1/local-work',headers=self.headers(owner),json={'draft_id':uid(),'draft_version':1})
        self.assertEqual(response.status_code,201,response.text)
        work=response.json()
        ready=self.client.get('/v1/local-work/'+work['id'],headers=self.headers(owner))
        self.assertEqual(ready.status_code,200,ready.text)
        self.assertEqual(ready.json()['state'],'ready',ready.text)
        return ready.json()

    def grant(self,work,owner=None):
        response=self.client.post('/v1/local-work/'+work['id']+'/grants',headers=self.headers(owner),json={})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def rpc(self,token,method,params=None,identity=1):
        message={'jsonrpc':'2.0','id':identity,'method':method}
        if params is not None: message['params']=params
        return self.client.post('/mcp',headers={'Authorization':'Bearer '+token,
            'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'},json=message)

    def tool(self,grant,name,args):
        response=self.rpc(grant['token'],'tools/call',{'name':name,'arguments':args})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['result']

    def test_browser_lifecycle_idempotency_scope_and_no_hosted_job(self):
        body={'draft_id':uid(),'draft_version':1}; headers=self.headers(key='same-create')
        first=self.client.post('/v1/local-work',headers=headers,json=body)
        self.assertEqual(first.status_code,201,first.text)
        second=self.client.post('/v1/local-work',headers=headers,json=body)
        self.assertEqual(second.json(),first.json())
        conflict=self.client.post('/v1/local-work',headers=headers,json={**body,'draft_version':2})
        self.assertEqual(conflict.status_code,409)
        work=self.client.get('/v1/local-work/'+first.json()['id'],headers=self.headers()).json()
        self.assertEqual(work['state'],'ready')
        self.assertEqual(self.client.get('/v1/local-work/'+work['id'],headers=self.headers(self.other)).status_code,404)
        self.assertEqual(self.client.get('/v1/local-work',headers=self.headers(self.other)).json()['items'],[])
        package=self.client.get('/v1/local-work/'+work['id']+'/package',headers=self.headers()).json()
        self.assertEqual(package['package']['retrieval_mode'],'progressive')
        self.assertTrue(package['artifacts'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('job'),[])
            self.assertEqual(tx.get('research_pin',work['research_request_id'])['data']['state'],'active')
        closed=self.client.post('/v1/local-work/'+work['id']+'/close',headers=self.headers(),json={})
        self.assertEqual(closed.json()['state'],'closed')
        with self.repo.read_transaction() as tx: self.assertEqual(tx.get('research_pin',work['research_request_id'])['data']['state'],'released')

    def legacy_view(self,tx,owner,work_id):
        """The per-work view() before batching: the reference for listing equivalence."""
        from reveal_backend.auth import owned
        from reveal_backend.research_work import connection_instructions,work_records
        work=deepcopy(owned(tx,'local_work',work_id,owner)['data']); work.pop('owner_user_id',None)
        work['request']=deepcopy(owned(tx,'request',work['research_request_id'],owner)['data'])
        for upload in (work['request'].get('user_inputs') or {}).get('uploads',[]):
            upload.pop('storage',None)
            if isinstance(upload.get('extraction'),dict): upload['extraction'].pop('storage',None); upload['extraction'].pop('content',None)
        work['submissions']=[ResearchWorkService.operation_view(r['data'],tx=tx,owner=owner)
            for r in work_records(tx,'research_operation',owner,work_id) if r['data']['kind'] in ('validate','submit')]
        connections={}; grants=work_records(tx,'research_access',owner,work_id)
        families=tx.get_many('research_oauth_family',sorted({r['data']['oauth_family_id'] for r in grants if r['data'].get('oauth_family_id')}))
        for row in grants:
            grant=row['data']
            if grant['kind']!='local': continue
            family=families.get(grant.get('oauth_family_id'))
            if family and family['owner']==owner:
                grant={**grant,**{key:family['data'].get(key) for key in ('expires_at','created_at','revoked_at')}}
            prior=connections.get(grant['grant_id'])
            if not prior or grant['expires_at']>prior['expires_at']:
                connections[grant['grant_id']]={k:grant.get(k) for k in ('grant_id','expires_at','revoked_at','created_at')}
        work['grants']=list(connections.values()); work.update(connection_instructions(work_id))
        return work

    def seed_children(self,work,grant_id,evidence):
        service=ResearchWorkService(self.repo)
        with self.repo.transaction() as tx:
            stored=tx.get('local_work',work['id'])['data']
            for kind,arguments in (('query',{}),('export',{'receipt_ids':[evidence['receipt']]}),
                    ('validate',{'receipt_ids':[evidence['receipt']]}),
                    ('submit',{'receipt_ids':[evidence['receipt']],'import_ids':[evidence['import']]})):
                operation=service.enqueue(tx,self.owner,stored,kind,arguments,grant_id)
                operation.update(state='succeeded',result={'borrowed':'excerpt'},report={'valid':True},reused_account_ids=['dapper:Account.borrowed'])
                tx.put('research_operation',operation['id'],self.owner,operation)

    def test_listing_batches_children_and_matches_each_work_view(self):
        from round_trips import count_round_trips
        works=[self.create() for _ in range(3)]; first=works[0]
        local=[self.grant(work) for work in works]
        evidence={'receipt':'receipt-a','import':'import-a'}
        with self.repo.transaction() as tx:
            scope={'local_work_id':first['id'],'research_request_id':first['research_request_id']}
            tx.put('evidence_receipt','receipt-a',self.owner,{'id':'receipt-a',**scope,'context':{}})
            # A withdrawn reuse receipt behind the import must strip borrowed results, never fail the listing.
            tx.put('evidence_import','import-a',self.owner,{'id':'import-a',**scope,'context':{},'reuse_receipt_ids':['withdrawn']})
            key=hashlib.sha256(b'oauth-token').hexdigest()
            tx.put('research_access',key,self.owner,{'grant_id':'oauth-grant','local_work_id':first['id'],
                'research_request_id':first['research_request_id'],'expires_at':'2000-01-01T00:00:00Z','created_at':now(),
                'revoked_at':None,'kind':'local','oauth_family_id':'family-a'})
            tx.put('research_oauth_family','family-a',self.owner,{'local_work_id':first['id'],'expires_at':'2999-01-01T00:00:00Z','created_at':now(),'revoked_at':None})
            hosted=dict(tx.get('local_work',works[2]['id'])['data'],id='hosted-work',job_id='hosted-work')
            tx.put('local_work','hosted-work',self.owner,hosted)
        for work,grant in zip(works,local): self.seed_children(work,grant['grant_id'],evidence)
        other=self.create(self.other)
        def listing():
            with count_round_trips() as budget:
                response=self.client.get('/v1/local-work',headers=self.headers())
            self.assertEqual(response.status_code,200,response.text)
            return response.json()['items'],budget
        items,budget=listing()
        with self.repo.read_transaction() as tx:
            rows=[r for r in tx.list('local_work',self.owner) if not r['data'].get('job_id')]
            expected=[self.legacy_view(tx,self.owner,row['id']) for row in rows]
        self.assertEqual(json.dumps(items,sort_keys=True),json.dumps(expected,sort_keys=True))
        self.assertEqual([item['id'] for item in items],[row['id'] for row in rows])
        self.assertNotIn('hosted-work',[item['id'] for item in items])
        view={item['id']:item for item in items}[first['id']]
        cited=[item for item in view['submissions'] if item['kind']=='submit'][0]
        self.assertEqual(cited['error']['code'],'REUSE_AUTHORITY_UNAVAILABLE')
        for field in ('result','report','reused_account_ids'): self.assertNotIn(field,cited)
        self.assertEqual([item['kind'] for item in view['submissions']].count('validate'),1)
        self.assertEqual({grant['grant_id'] for grant in view['grants']},{local[0]['grant_id'],'oauth-grant'})
        self.assertEqual([g for g in view['grants'] if g['grant_id']=='oauth-grant'][0]['expires_at'],'2999-01-01T00:00:00Z')
        self.assertEqual([item['id'] for item in self.client.get('/v1/local-work',headers=self.headers(self.other)).json()['items']],[other['id']])
        more=[self.create() for _ in range(2)]
        for work in more: self.seed_children(work,self.grant(work)['grant_id'],evidence)
        grown,larger=listing()
        self.assertEqual(len(grown),len(items)+2)
        self.assertEqual(larger.leases,budget.leases,larger)   # statements do not grow with the number of works

    def test_poll_rejects_an_expired_session_before_revealing_whether_work_exists(self):
        work=self.create()
        with self.repo.transaction() as tx:
            row=tx.get('principal',self.other); row['data']['retired']=True; tx.put('principal',self.other,self.other,row['data'])
        for path in ('/v1/local-work/'+work['id'],'/v1/local-work/absent-work'):
            self.assertEqual(self.client.get(path,headers=self.headers(self.other)).status_code,401)
        self.assertEqual(self.client.get('/v1/local-work/absent-work',headers=self.headers()).status_code,404)
        self.assertEqual(self.client.get('/v1/local-work/'+work['id'],headers={'Authorization':'Bearer '}).status_code,401)

    def test_grant_once_and_revocation_blocks_transport(self):
        work=self.create(); path='/v1/local-work/'+work['id']+'/grants'; headers=self.headers()
        first=self.client.post(path,headers=headers,json={})
        self.assertEqual(first.status_code,201)
        self.assertEqual(first.headers['cache-control'],'no-store')
        self.assertEqual(self.client.post(path,headers=headers,json={}).status_code,409)
        grant=first.json()
        listing=self.rpc(grant['token'],'tools/list')
        self.assertEqual(listing.status_code,200,listing.text)
        self.assertIn('private, no-store',listing.headers['cache-control'])
        visible=self.client.get('/v1/local-work/'+work['id'],headers=self.headers()).json()
        self.assertNotIn(grant['token'],json.dumps(visible))
        revoked=self.client.delete(path+'/'+grant['grant_id'],headers=self.headers())
        self.assertEqual(revoked.status_code,204)
        self.assertEqual(revoked.content,b'')
        self.assertEqual(self.rpc(grant['token'],'tools/list').status_code,401)

    def test_close_replay_refreshes_source_authority_instead_of_cached_results(self):
        work=self.create(); runner=ResearchWorkService(self.repo)
        with self.repo.transaction() as tx:
            operation=runner.enqueue(tx,self.owner,tx.get('local_work',work['id'])['data'],'submit',{},None)
            operation.update(state='accepted',result={'borrowed_excerpt':'previously-visible-evidence'},report={'detail':'borrowed'})
            tx.put('research_operation',operation['id'],self.owner,operation)
        headers=self.headers(key='repeat-close'); path='/v1/local-work/'+work['id']+'/close'
        first=self.client.post(path,headers=headers,json={})
        self.assertEqual(first.status_code,200,first.text)
        self.assertIn('previously-visible-evidence',first.text)
        with patch('reveal_backend.research_execution.authorize_operation_result',side_effect=
                Problem(409,'REUSE_AUTHORITY_UNAVAILABLE','The source was withdrawn.')):
            second=self.client.post(path,headers=headers,json={})
        self.assertEqual(second.status_code,200,second.text)
        self.assertNotIn('previously-visible-evidence',second.text)
        self.assertEqual(second.json()['submissions'][0]['error']['code'],'REUSE_AUTHORITY_UNAVAILABLE')

    def test_reconnect_retry_uses_current_grant_and_preserves_immutable_arguments(self):
        work=self.create(); old=self.grant(work)
        result=self.tool(old,'get_factor',{'research_request_id':work['research_request_id'],
            'arguments':{'factor_id':'fixture'},'idempotency_key':'before-reconnect'})['structuredContent']
        identity=result['operation_id']
        with self.repo.transaction() as tx:
            operation=tx.get('research_operation',identity)['data']; arguments=deepcopy(operation['arguments'])
            operation.update(state='failed',lease_token='obsolete-worker',lease_until='2999-01-01T00:00:00Z')
            tx.put('research_operation',identity,self.owner,operation)
        revoked=self.client.delete('/v1/local-work/'+work['id']+'/grants/'+old['grant_id'],headers=self.headers())
        self.assertEqual(revoked.status_code,204)
        args={'local_work_id':work['id'],'operation_id':identity,'idempotency_key':'retry-current-grant'}
        denied=self.rpc(old['token'],'tools/call',{'name':'retry_operation','arguments':args})
        self.assertEqual(denied.status_code,401)
        fresh=self.grant(work); accepted=self.tool(fresh,'retry_operation',args)
        self.assertFalse(accepted.get('isError'),accepted)
        with self.repo.read_transaction() as tx:
            operation=tx.get('research_operation',identity)['data']
            self.assertEqual(operation['grant_id'],fresh['grant_id']); self.assertEqual(operation['arguments'],arguments)
            self.assertEqual(operation['state'],'received'); self.assertNotIn('lease_token',operation)
        service=ResearchWorkService(self.repo)
        with patch.object(service,'query',return_value={'captured':True}): service.run_operation(identity)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('research_operation',identity)['data']['state'],'succeeded')

    def test_context_child_scope_tamper_and_private_artifact_download(self):
        first,second=self.create(),self.create()
        grant=self.grant(first)
        denied=self.tool(grant,'get_local_work',{'local_work_id':second['id']})
        self.assertTrue(denied['isError']); self.assertEqual(denied['structuredContent']['code'],'NOT_FOUND')
        package=self.tool(grant,'get_research_package',{'local_work_id':first['id']})['structuredContent']
        artifact=package['artifacts'][0]
        descriptor=self.tool(grant,'get_artifact_download',{'local_work_id':first['id'],'artifact_id':artifact['id']})['structuredContent']
        download=self.client.get(descriptor['url'],headers={'Authorization':'Bearer '+grant['token']})
        self.assertEqual(download.status_code,200,download.text)
        self.assertEqual(hashlib.sha256(download.content).hexdigest(),artifact['sha256'])
        other_grant=self.grant(second)
        self.assertEqual(self.client.get(descriptor['url'],headers={'Authorization':'Bearer '+other_grant['token']}).status_code,404)
        bad=self.tool(grant,'get_factor',{'research_request_id':second['research_request_id'],'arguments':{'factor_id':'x'},'idempotency_key':'x'})
        self.assertEqual(bad['structuredContent']['code'],'NOT_FOUND')

    def test_expiry_close_and_reload_gate_prevent_new_actions(self):
        work=self.create(); grant=self.grant(work)
        with self.repo.transaction() as tx:
            rg.set_gate(tx,True,reason='fixture reload')
        self.assertEqual(self.client.post('/v1/local-work',headers=self.headers(),json={'draft_id':uid(),'draft_version':1}).status_code,503)
        closed=self.client.post('/v1/local-work/'+work['id']+'/close',headers=self.headers(),json={})
        self.assertEqual(closed.status_code,200)
        query=self.tool(grant,'get_factor',{'research_request_id':work['research_request_id'],'arguments':{'factor_id':'x'},'idempotency_key':'after-close'})
        self.assertTrue(query['isError']); self.assertEqual(query['structuredContent']['code'],'WORK_CLOSED')
        with self.repo.transaction() as tx:
            key=hashlib.sha256(grant['token'].encode()).hexdigest(); row=tx.get('research_access',key)
            row['data']['expires_at']='2000-01-01T00:00:00Z'; tx.put('research_access',key,row['owner'],row['data'])
        self.assertEqual(self.rpc(grant['token'],'tools/list').status_code,401)

    def test_official_streamable_client_reconnects_without_session_state(self):
        work=self.create(); grant=self.grant(work); fresh_app=self.make_app()
        async def verify():
            async with fresh_app.router.lifespan_context(fresh_app):
                for _ in range(2):
                    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fresh_app),
                            headers={'Authorization':'Bearer '+grant['token']}) as http:
                        async with streamable_http_client(BASE+'/mcp',http_client=http,terminate_on_close=False) as (read,write):
                            async with ClientSession(read,write,read_timeout_seconds=10) as session:
                                initialized=await session.initialize()
                                self.assertEqual(initialized.server_info.name,'reveal')
                                tools=await session.list_tools()
                                self.assertIn('get_factor_loadings',{tool.name for tool in tools.tools})
                                result=await session.call_tool('get_local_work',{'local_work_id':work['id']})
                                self.assertFalse(result.is_error)
                                self.assertEqual(result.structured_content['id'],work['id'])
        asyncio.run(verify())

    def test_same_app_can_restart_its_transport_lifecycle(self):
        work = self.create(); grant = self.grant(work); app = self.make_app()
        for _ in range(2):
            with TestClient(app, base_url=BASE) as client:
                response = client.post('/mcp', headers={'Authorization': 'Bearer '+grant['token'],
                    'Accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('get_local_work', {t['name'] for t in response.json()['result']['tools']})

    def test_pending_quota_and_grant_limit_are_enforced_before_side_effects(self):
        work=self.create(); grant=self.grant(work)
        for number in range(8):
            result=self.tool(grant,'get_factor',{'research_request_id':work['research_request_id'],
                'arguments':{'factor_id':'fixture'},'idempotency_key':'pending-'+str(number)})
            self.assertFalse(result.get('isError'),result)
        denied=self.tool(grant,'get_factor',{'research_request_id':work['research_request_id'],
            'arguments':{'factor_id':'fixture'},'idempotency_key':'over-pending-limit'})
        self.assertEqual(denied['structuredContent']['code'],'RESEARCH_BUSY')
        with self.repo.transaction() as tx:
            stored=tx.get('local_work',work['id'])['data']
            failed=ResearchWorkService(self.repo).enqueue(tx,self.owner,stored,'query',{},grant['grant_id'])
            failed['state']='failed'; tx.put('research_operation',failed['id'],self.owner,failed)
        retry=self.tool(grant,'retry_operation',{'local_work_id':work['id'],'operation_id':failed['id'],'idempotency_key':'retry-busy'})
        self.assertEqual(retry['structuredContent']['code'],'RESEARCH_BUSY')
        for _ in range(4): self.grant(work)
        self.assertEqual(self.client.post('/v1/local-work/'+work['id']+'/grants',headers=self.headers(),json={}).status_code,429)
        with self.repo.read_transaction() as tx:
            self.assertEqual(sum(row['data']['state']=='received' for row in tx.list('research_operation')),8)
            self.assertEqual(len(tx.list('research_access')),5)
            self.assertEqual(tx.list('evidence_receipt'),[])

    def test_expired_lease_reclaimed_by_new_service_and_stale_result_cannot_commit(self):
        work=self.create(); grant=self.grant(work)
        result=self.tool(grant,'get_factor',{'research_request_id':work['research_request_id'],
            'arguments':{'factor_id':'fixture'},'idempotency_key':'recovery'})['structuredContent']
        operation_id=result['operation_id']; calls=[]
        test=self
        class Captures:
            def __init__(self,marker,reclaim=None): self.marker,self.reclaim=marker,reclaim
            def query(self,name,arguments,*,generation_id):
                calls.append(self.marker)
                if self.reclaim: self.reclaim()
                source={'origin':'fixture:retained-reference','generation_id':generation_id}
                value={'status':'complete','items':[{'marker':self.marker}]}
                raw=canonical_json({'operation':name,'arguments':arguments,'source':source,'result':value})
                return QueryCapture(value,raw,'imported_reference',source)
        replacement=ResearchWorkService(self.repo,data_service=Captures('replacement'))
        def reclaim():
            with test.repo.transaction() as tx:
                row=tx.get('research_operation',operation_id)
                row['data']['lease_until']='2000-01-01T00:00:00Z'
                tx.put('research_operation',operation_id,row['owner'],row['data'])
            replacement.run_operation(operation_id)
        original=ResearchWorkService(self.repo,data_service=Captures('stale-original',reclaim))
        # Closing retains the generation while accepted operations are pending.
        self.client.post('/v1/local-work/'+work['id']+'/close',headers=self.headers(),json={})
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('research_pin',work['research_request_id'])['data']['state'],'active')
        original.run_operation(operation_id)
        replacement.run_operation(operation_id) # Terminal retries are no-ops.
        with self.repo.read_transaction() as tx:
            operation=tx.get('research_operation',operation_id)['data']
            receipt=tx.get('evidence_receipt',operation_id)['data']
            self.assertEqual(operation['attempt'],2)
            self.assertEqual(operation['state'],'succeeded')
            self.assertEqual(receipt['result']['items'],[{'marker':'replacement'}])
            self.assertEqual(len(tx.list('evidence_receipt')),1)
            self.assertEqual(tx.get('research_pin',work['research_request_id'])['data']['state'],'released')
        self.assertEqual(calls,['stale-original','replacement'])

    class Pages:
        """A reference capture with one exact upstream page beside its normalized result."""
        def __init__(self,during=None): self.during=during
        def query(self,name,arguments,*,generation_id):
            if self.during: self.during()
            source={'origin':'fixture:retained-reference','generation_id':generation_id}
            value={'status':'complete','items':[{'factor':arguments.get('factor_id')}]}
            raw=canonical_json({'operation':name,'arguments':arguments,'source':source,'result':value})
            return QueryCapture(value,raw,'imported_reference',source,extra_files={'page-1':canonical_json({'page':1,'factor':arguments.get('factor_id')})})

    def queued(self,grant,work,key,factor='fixture'):
        return self.tool(grant,'get_factor',{'research_request_id':work['research_request_id'],
            'arguments':{'factor_id':factor},'idempotency_key':key})['structuredContent']['operation_id']

    def test_query_commits_artifacts_receipt_and_result_in_one_fence(self):
        from round_trips import count_round_trips
        from reveal_backend.repository import digest
        work=self.create(); grant=self.grant(work); identity=self.queued(grant,work,'one-fence')
        service=ResearchWorkService(self.repo,data_service=self.Pages())
        with count_round_trips() as budget: service.run_operation(identity)
        # The lease, one authorized read before the query, then one fence for artifacts, receipt and result.
        self.assertEqual(budget.kinds(),['write','read','write'],budget)
        with self.repo.read_transaction() as tx:
            operation=tx.get('research_operation',identity)['data']; receipt=tx.get('evidence_receipt',identity)['data']
            self.assertEqual(operation['state'],'succeeded',operation)
            self.assertEqual(operation['result']['receipt_id'],identity)
            self.assertEqual(sorted(operation['result']['artifacts']),sorted(receipt['context']['artifact_ids']))
            self.assertEqual(len(receipt['context']['artifact_ids']),2)
            for relative,artifact_id in receipt['context']['artifact_ids'].items():
                artifact=tx.get('research_artifact',artifact_id)['data']
                # The same ids retain() assigns, so receipts and their hashes are unchanged.
                self.assertEqual(artifact_id,digest([work['id'],'source',artifact['sha256'],relative,
                    {'research_request_id':work['research_request_id']}]))
                self.assertEqual((artifact['filename'],artifact['purpose'],artifact['local_work_id']),(relative,'source',work['id']))
        again=self.queued(grant,work,'one-fence-again')
        with patch('reveal_backend.user_inputs.retain',side_effect=AssertionError('Retained blobs are reused, never uploaded again')):
            ResearchWorkService(self.repo,data_service=self.Pages()).run_operation(again)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('research_operation',again)['data']['state'],'succeeded')
            first=tx.get('evidence_receipt',identity)['data']['context']['artifact_ids']
            self.assertEqual(tx.get('evidence_receipt',again)['data']['context']['artifact_ids'],first)

    def test_revocation_or_quota_failure_before_the_commit_writes_nothing(self):
        work=self.create(); grant=self.grant(work)
        def revoke():
            with self.repo.transaction() as tx:
                key=hashlib.sha256(grant['token'].encode()).hexdigest(); row=tx.get('research_access',key)
                row['data']['revoked_at']=now(); tx.put('research_access',key,row['owner'],row['data'])
        def artifacts():
            with self.repo.read_transaction() as tx: return {r['id'] for r in tx.list('research_artifact',self.owner)}
        full=self.queued(grant,work,'over-budget')
        with self.repo.transaction() as tx:
            tx.put('research_artifact','budget-filler',self.owner,{'id':'budget-filler','local_work_id':work['id'],
                'filename':'filler.bin','sha256':'f'*64,'size_bytes':127_999_990,'purpose':'source','metadata':{},
                'storage':{'store':'filesystem','key':'filler'}})
        revoked=self.queued(grant,work,'revoked')
        for identity,during,code in ((full,None,'RESEARCH_STORAGE_LIMIT'),(revoked,revoke,'AUTHORITY_REVOKED')):
            with self.subTest(code=code):
                before=artifacts()
                ResearchWorkService(self.repo,data_service=self.Pages(during)).run_operation(identity)
                with self.repo.read_transaction() as tx:
                    operation=tx.get('research_operation',identity)['data']
                    self.assertEqual((operation['state'],operation['error']['code']),('failed',code))
                    self.assertIsNone(tx.get('evidence_receipt',identity))
                self.assertEqual(artifacts(),before)   # no file of a two-file capture commits alone

    def test_local_browser_routes_cannot_convert_or_close_hosted_execution(self):
        work=self.create(); grant=self.grant(work)
        with self.repo.transaction() as tx:
            row=tx.get('local_work',work['id']); row['data']['job_id']=work['id']
            tx.put('local_work',work['id'],self.owner,row['data'])
            with self.assertRaises(Problem) as error: issue_grant(tx,self.owner,work['id'],'hosted-bypass')
            self.assertEqual(error.exception.code,'RESEARCH_SCOPE_MISMATCH')
        root='/v1/local-work/'+work['id']
        for method,path in [('get',root),('get',root+'/package'),('post',root+'/grants'),
                            ('post',root+'/close'),('delete',root+'/grants/'+grant['grant_id'])]:
            response=getattr(self.client,method)(path,headers=self.headers())
            self.assertEqual(response.status_code,404,(method,path,response.text))
        self.assertEqual(self.client.get('/v1/local-work',headers=self.headers()).json()['items'],[])
        self.assertEqual(self.rpc(grant['token'],'tools/list').status_code,403)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('local_work',work['id'])['data']['state'],'ready')
            self.assertEqual(len(tx.list('research_access')),1)
            self.assertEqual(tx.list('research_access')[0]['data']['revoked_at'],None)

    def test_invalid_creation_body_does_not_freeze_a_request(self):
        for raw in ('{',json.dumps({'draft_id':'x','draft_version':0}),json.dumps({'draft_id':'x','draft_version':True})):
            response=self.client.post('/v1/local-work',headers={**self.headers(),'Content-Type':'application/json'},content=raw)
            self.assertEqual(response.status_code,422,response.text)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('request'),[])

    def test_reuse_receipt_response_never_exposes_internal_source_locations(self):
        work=self.create(); grant=self.grant(work)
        selection={'object_id':'dapper:Claim.fixture','source_kind':'owned_document','source_id':'document','payload_sha256':'a'*64}
        internal={'id':'receipt','selections':[{'selection':selection,'purpose':'source_claim','closure_sha256':'b'*64,
            'artifact_records':{'file':{'path':'/private/scientific-source','storage':{'key':'private-bucket-key'}}},
            'dapper_context':{'files':[{'id':'dapper:File.fixture'}]}}]}
        with patch('reveal_backend.scientific_reuse.create_receipt',return_value=internal):
            result=self.tool(grant,'reuse_scientific_objects',{'research_request_id':work['research_request_id'],
                'selections':[selection],'idempotency_key':'reuse'})['structuredContent']
        self.assertEqual(result['selections'],[{'selection':selection,'purpose':'source_claim','closure_sha256':'b'*64}])
        for secret in ('artifact_records','storage','/private/scientific-source','private-bucket-key','dapper_context'):
            self.assertNotIn(secret,json.dumps(result))

    def test_capabilities_read_pinned_source_only_after_auth_and_outside_app_transaction(self):
        work=self.create(); grant=self.grant(work)
        source=seed_fixture.ReferenceQueryTests(); source.setUp(); self.addCleanup(source.doCleanups)
        legacy='1'*64
        with sqlite3.connect(source.path) as connection:
            connection.execute('INSERT INTO reference_generations VALUES(?,?,?,?,?,?,?,?)',
                (legacy,'legacy-cfde-inc-v2','cfde-inc-v2','superseded',seed_fixture.IMP,'2'*64,'3'*64,'{}'))
        active=0; calls=[]; read_transaction=self.repo.read_transaction; source_catalog=source.service.catalog
        @contextmanager
        def tracked_read():
            nonlocal active
            active+=1
            try:
                with read_transaction() as tx: yield tx
            finally: active-=1
        def catalog(generation):
            self.assertEqual(active,0,'Source SQL must not hold an app transaction')
            calls.append(generation)
            return source_catalog(generation)
        runner=ResearchWorkService(self.repo,data_service=source.service)
        with patch.object(self.repo,'read_transaction',tracked_read), patch.object(source.service,'catalog',side_effect=catalog):
            with self.assertRaises(Problem):
                dispatch(runner,'Bearer invalid','list_data_operations',{'research_request_id':work['research_request_id']})
            with self.assertRaises(Problem):
                dispatch(runner,'Bearer '+grant['token'],'list_data_operations',{'research_request_id':'another-request'})
            self.assertEqual(calls,[])
            for generation,model,availability in [(seed_fixture.GEN,'eaggl-capped-v1','supported'),(legacy,'cfde-inc-v2','not_stored')]:
                with self.repo.transaction() as tx:
                    row=tx.get('local_work',work['id']); row['data']['reference_generation_id']=generation
                    tx.put('local_work',work['id'],self.owner,row['data'])
                args={'research_request_id':work['research_request_id']}
                result=dispatch(runner,'Bearer '+grant['token'],'list_data_operations',args)
                self.assertEqual(result['generation']['model'],model)
                self.assertEqual(result['generation']['generation_id'],generation)
                detail=dispatch(runner,'Bearer '+grant['token'],'describe_data_operation',{**args,'operation_id':'get_gene_set_factors'})
                self.assertEqual(detail['availability'],availability)
                self.assertEqual(detail['generation']['model'],model)
        self.assertEqual(calls,[seed_fixture.GEN,seed_fixture.GEN,legacy,legacy])


if __name__=='__main__': unittest.main()
