#!/usr/bin/env python3
"""One explicit bounded scientific workflow, followed through pushed job events.

Requires an isolated development/test namespace. Never increases configured
provider budgets. State saves ids/cursors only; reruns resume the existing job.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from urllib.parse import quote
import hashlib
import httpx
import jwt
from dotenv import dotenv_values

TERMINAL={'succeeded','failed','cancelled','insufficient_evidence'}


async def main(args):
    env={**dotenv_values(args.env),**{k:v for k,v in os.environ.items() if k.startswith('REVEAL_')}}
    if env.get('REVEAL_ENVIRONMENT') not in ('development','test') or env.get('REVEAL_JOB_NAMESPACE')=='reveal':
        raise RuntimeError('Scientific probe requires an explicitly isolated development/test namespace')
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    state=json.loads(output.read_text()) if output.exists() else {'namespace':env['REVEAL_JOB_NAMESPACE'],'created_at':time.time(),'events':{}}
    if state['namespace']!=env['REVEAL_JOB_NAMESPACE']: raise RuntimeError('Probe state belongs to another namespace')
    def save(): output.write_text(json.dumps(state,indent=2))
    def auth():
        now=int(time.time())
        token=jwt.encode({'sub':state['user_id'],'principal_kind':'anonymous',
            'iss':env.get('REVEAL_GATEWAY_ISSUER','reveal-nextjs'),'aud':env.get('REVEAL_GATEWAY_AUDIENCE','reveal-api'),
            'iat':now,'exp':now+240,'jti':str(uuid4())},env['REVEAL_GATEWAY_SECRET'],algorithm='HS256')
        return {'Authorization':'Bearer '+token}
    async with httpx.AsyncClient(base_url=args.base.rstrip('/'),timeout=180,follow_redirects=False) as client:
        async def request(method,path,**kwargs):
            response=await client.request(method,path,**kwargs)
            if response.status_code>=400:
                raise RuntimeError(f'{method} {path} failed ({response.status_code}): '+response.text[:1200])
            return response.json()
        if not state.get('user_id'):
            me=await request('POST','/internal/v1/principals/anonymous',json={},headers={
                'Authorization':'Bearer '+env['REVEAL_GATEWAY_SERVICE_TOKEN'],'Idempotency-Key':'workflow-probe-'+str(uuid4())})
            state['user_id']=me['user_id']; save()
        if not state.get('draft'):
            found=await request('GET','/v1/knowledge-gaps/search',params={'q':args.query,'limit':10,'mode':'fuzzy'})
            if not found['items']: raise RuntimeError('No source gap matched probe query')
            gap=next((item['gap'] for item in found['items'] if 'reverse_causation' in item['gap']['source']['source_id']),found['items'][0]['gap'])
            source={'id':gap['object']['id'],'source_id':gap['source']['source_id'],'source_revision':gap['source']['source_revision']}
            suggestions=await request('POST','/v1/mechanisms/suggest',json={'source_gap':source,'manual_eaggl_anchors':[],
                'dismissed_source_ids':[],'subquery':'','mode':'semantic','model':'cfde-inc-v2'})
            if not suggestions['automatic_anchors']: raise RuntimeError('No compatible Vector suggestions')
            factor=suggestions['automatic_anchors'][0]['factor']
            composer={'source_gap':source,'eaggl_anchors':[{'reference':{'source':'eaggl','source_id':factor['source_id'],
                'source_revision':factor['source_revision'],'dapper_id':factor['object']['id']},'origin':'automatic',
                'suggestion_id':suggestions['suggestion_id']}], 'dismissed_source_ids':[],'mechanism_subquery':'',
                'model':'cfde-inc-v2','selected_kgs':['biomarkerkg']}
            state['draft']=await request('POST','/v1/drafts',json={'name':'Durable Workflow scientific pilot','composer':composer},
                headers={**auth(),'Idempotency-Key':'workflow-probe-draft-'+str(uuid4())})
            state['source_gap']=source; state['vector_provenance']=suggestions.get('search'); save()
        if args.prepare_only:
            print(json.dumps({'prepared':True,'namespace':state['namespace'],'report':str(output)}),flush=True)
            return
        if not state.get('job'):
            draft=state['draft']; key=state.setdefault('job_idempotency_key',str(uuid4())); save()
            state['job']=await request('POST','/v1/jobs',json={'kind':'analysis','draft_id':draft['id'],'draft_version':draft['version']},
                                      headers={**auth(),'Idempotency-Key':key})
            save(); print(json.dumps({'submitted_job':state['job']['id'],'namespace':state['namespace']}),flush=True)
        async def wait_job(identity):
            snapshot=await request('GET','/v1/jobs/'+identity,headers=auth())
            if snapshot['status'] in TERMINAL:
                state.setdefault('completed_jobs',{})[identity]=snapshot; save(); return snapshot
            cursor=state['events'].get(identity,{}).get('cursor','0')
            deadline=time.monotonic()+args.max_seconds
            while time.monotonic()<deadline:
                # Streams renew when their short-lived identity assertion expires.
                # This reconnect performs replay once, never recurring status reads.
                async with client.stream('GET','/v1/jobs/'+identity+'/events',headers={**auth(),'Accept':'text/event-stream',
                    'Last-Event-ID':str(cursor)},timeout=httpx.Timeout(270,connect=30)) as response:
                    response.raise_for_status(); data=[]
                    async for line in response.aiter_lines():
                        if line.startswith('data:'): data.append(line[5:].lstrip())
                        if not line and data:
                            event=json.loads('\n'.join(data)); data=[]
                            if 'id' not in event: continue
                            cursor=event['id']; state['events'][identity]={'cursor':cursor,'last':event}; save()
                            if event['event_type'] in ('status','result','failure') or event['stage']!=state.get('last_stage'):
                                print(json.dumps({'job_id':identity,'event_id':cursor,'status':event['status'],'stage':event['stage'],
                                                  'message':event['message']}),flush=True)
                            state['last_stage']=event['stage']
                            if event['status'] in TERMINAL:
                                result=await request('GET','/v1/jobs/'+identity,headers=auth())
                                state.setdefault('completed_jobs',{})[identity]=result; save(); return result
            raise TimeoutError('Scientific probe wall-clock limit reached; durable workflow remains recoverable')
        final=await wait_job(state['job']['id'])
        if final['status']=='succeeded':
            for account in (final.get('result') or {}).get('account_ids',[]):
                detail=await request('GET','/v1/accounts/'+quote(account,safe=''),headers=auth())
                state.setdefault('accounts',{})[account]={'root_id':detail['root_id'],'artifact_count':len(detail.get('artifacts',[]))}; save()
                if detail.get('artifacts'):
                    artifact=detail['artifacts'][0]['file']; checksum=artifact['sha256']
                    response=await client.get('/v1/artifacts/'+checksum,headers=auth())
                    if response.is_redirect:
                        async with httpx.AsyncClient(timeout=60) as download:
                            response=await download.get(response.headers['location'])
                    response.raise_for_status()
                    if hashlib.sha256(response.content).hexdigest()!=checksum: raise RuntimeError('Authorized download checksum mismatch')
                    state['artifact_download_verified']=checksum; save()
            for identity in (final.get('result') or {}).get('paragraph_job_ids',[]): await wait_job(identity)
        elif final['status']=='insufficient_evidence':
            outcome=await request('GET','/v1/jobs/'+final['id']+'/outcome',headers=auth())
            state['outcome_verified']=bool(outcome); save()
        print(json.dumps({'analysis_status':final['status'],'job_id':final['id'],'result':final.get('result'),
                          'failure':final.get('failure'),'report':str(output)}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',default='.runtime/workflow/backend.env')
    parser.add_argument('--base',default='http://127.0.0.1:18001')
    parser.add_argument('--output',default='.runtime/workflow/scientific-probe.json')
    parser.add_argument('--query',default='coronary artery disease')
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--max-seconds',type=int,default=2400)
    asyncio.run(main(parser.parse_args()))
