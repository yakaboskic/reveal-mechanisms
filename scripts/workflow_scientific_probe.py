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
from urllib.parse import quote, urlsplit
import hashlib
import math
import re
import sys
import httpx
import jwt
from dotenv import dotenv_values

TERMINAL={'succeeded','failed','cancelled','insufficient_evidence'}
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/backend/src'))
from reveal_backend.runtime_config import CURRENT_DAPPER_SNAPSHOT
from reveal_backend.evidence_package import canonical_json
MAX_ARTIFACT_BYTES=8_000_000
MAX_TOTAL_BYTES=64_000_000


def canonical(value):
    return canonical_json(value)


def write_private(path, raw):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_bytes(raw); temporary.chmod(0o600); temporary.replace(path)


def model_from_records(items, selected=None):
    models={item['record'].get('model') or item['record']['source_id'].split(':')[3] for item in items}
    if len(models)!=1 or not models <= {'cfde-inc-v2','eaggl-capped-v1'}:
        raise RuntimeError('Cannot identify one eligible active reference model from the API')
    active=models.pop()
    if selected and selected!=active: raise RuntimeError('Requested reference model differs from the active API records')
    return active


def budget_observation(env, maximum):
    per_execution=float(env.get('REVEAL_AGENT_MAX_BUDGET_USD') or '3')
    if not math.isfinite(per_execution) or per_execution<=0 or not math.isfinite(maximum) or maximum<=0:
        raise RuntimeError('Invalid configured scientific budget')
    result={'model':env.get('REVEAL_CLAUDE_MODEL') or 'claude-sonnet-4-6',
        'per_execution_usd':per_execution,'maximum_executions':2,'maximum_model_budget_usd':2*per_execution,
        'timeout_seconds_per_execution':int(env.get('REVEAL_AGENT_TIMEOUT_SECONDS') or '900'),
        'max_turns_per_execution':int(env.get('REVEAL_AGENT_MAX_TURNS') or '100'),
        'note':'Configuration observation, not measured spend; one analysis plus at most one automatic paragraph. Box/storage charges excluded.'}
    if result['maximum_model_budget_usd']>maximum:
        raise RuntimeError('Configured analysis plus paragraph budget exceeds --max-run-budget-usd; lower runtime limits before creating a job')
    return result


def relationship_report(document, verified_files):
    from reveal_backend.relationship_provenance import reachable_entities
    groups={node['id']:group for group,rows in document.items() if isinstance(rows,list)
            for node in rows if isinstance(node,dict) and isinstance(node.get('id'),str)}
    nodes={node['id']:node for rows in document.values() if isinstance(rows,list)
           for node in rows if isinstance(node,dict) and isinstance(node.get('id'),str)}
    families={'gene–GeneSet':[],'gene–Mechanism':[],'GeneSet–Mechanism':[]}
    gene_identities={}
    def kind(identity):
        if groups.get(identity)=='gene_sets': return 'GeneSet'
        if groups.get(identity)=='mechanisms': return 'Mechanism'
        local=re.fullmatch(r'urn:reveal:eaggl-gene:([^:\s]+):([0-9]+)',identity) if isinstance(identity,str) else None
        if local:
            gene_identities[identity]={'scope':'source_local','source_import':local.group(1),
                'gene_index':int(local.group(2)),'taxon':None,'cross_dataset_equivalence':'not_inferred'}
            return 'gene'
        if isinstance(identity,str) and (identity.startswith(('NCBIGene:','ENSEMBL:','HGNC:')) or
                any(part in identity for part in ('/ncbigene/','/ensembl/','/hgnc/'))):
            gene_identities[identity]={'scope':'external_identifier_namespace','cross_dataset_equivalence':'not_inferred'}
            return 'gene'
        return None
    evidence=[]
    for item in document.get('evidence_items',[]):
        refs=reachable_entities(document,item['id'])
        files=sorted(ref for ref in refs if groups.get(ref)=='files')
        evidence.append({'id':item['id'],'gene_sets':sorted(ref for ref in refs if groups.get(ref)=='gene_sets'),
            'mechanisms':sorted(ref for ref in refs if groups.get(ref)=='mechanisms'),
            'source_claims':item.get('source_claims',[]),'files':files,
            'verified_file_ids':[ref for ref in files if nodes[ref].get('sha256') in verified_files],
            'source_locator':item.get('source_locator')})
    structured=[]
    for proposition in document.get('propositions',[]):
        if not all(proposition.get(field) for field in ('subject_entity','relation','object_entity')): continue
        structured.append(proposition['id'])
        ends={kind(proposition['subject_entity']),kind(proposition['object_entity'])}
        for family in families:
            if ends==set(family.split('–')): families[family].append(proposition['id'])
    return {'structured_propositions':structured,'relationship_families':families,'evidence_provenance':evidence,
        'gene_identity_scopes':gene_identities,
        'note':'Observed authored structure and exact dependency traversal; this is not a scientific truth score, and empty relationship categories are permitted.'}


async def download_artifact(client, headers, checksum):
    async def read(response):
        if response.status_code!=200: raise RuntimeError('Authorized artifact download failed (HTTP '+str(response.status_code)+')')
        raw=bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw)>MAX_ARTIFACT_BYTES: raise RuntimeError('Artifact download exceeds probe byte bound')
        return bytes(raw)
    async with client.stream('GET','/v1/artifacts/'+checksum,headers=headers) as response:
        if not response.is_redirect: return await read(response)
        destination=response.headers['location']; parsed=urlsplit(destination)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError('Unsafe artifact download destination')
    # Never forward a Reveal bearer to the storage service or save its signed URL.
    async with httpx.AsyncClient(timeout=60,follow_redirects=False) as storage:
        try:
            async with storage.stream('GET',destination) as response: return await read(response)
        except httpx.HTTPError as error:
            raise RuntimeError('Storage download transport failed: '+type(error).__name__) from None


async def capture_paragraph_results(analysis, *, wait_job, request, auth, report, record, release_lock_sha256):
    """Observe only the paragraph jobs already returned by the analysis."""
    observations=[]
    for index,identity in enumerate((analysis.get('result') or {}).get('paragraph_job_ids',[]),1):
        final=await wait_job(identity)
        result_path=f'paragraph-{index}/result.json'
        report(result_path,final)
        observed={'job_id':identity,'status':final['status'],'result':final.get('result'),
            'failure':final.get('failure'),'result_report':result_path,'projection_verified':False}
        if final['status']=='succeeded':
            paragraph_id=(final.get('result') or {}).get('paragraph_id')
            if not isinstance(paragraph_id,str) or not paragraph_id:
                observed['projection_error']='Successful paragraph job omitted paragraph_id'
            else:
                observed['paragraph_id']=paragraph_id
                projection_path=f'paragraph-{index}/projection.json'
                try:
                    projection=await request('GET','/v1/paragraphs/'+quote(paragraph_id,safe=''),headers=auth())
                    report(projection_path,projection)
                    observed['projection_report']=projection_path
                    if projection.get('root_id')!=paragraph_id:
                        raise RuntimeError('Paragraph projection root differs from the completed job')
                    if projection.get('schema',{}).get('dependency_snapshot_sha256')!=release_lock_sha256:
                        raise RuntimeError('Paragraph projection uses an unexpected DAPPER release lock')
                    observed['projection_verified']=True
                    observed['coverage']=projection.get('coverage')
                except (RuntimeError,httpx.HTTPError) as error:
                    observed['projection_error']=str(error)
        record(identity,observed)
        observations.append(observed)
    return observations


def workflow_summary(analysis, paragraphs):
    complete=(analysis['status']=='succeeded' and bool(paragraphs) and
              all(item['status']=='succeeded' and item['projection_verified'] for item in paragraphs))
    if complete: status='succeeded'
    elif analysis['status']!='succeeded': status=analysis['status']
    elif not paragraphs: status='analysis_only'
    else: status='paragraph_incomplete'
    return {'analysis_status':analysis['status'],'job_id':analysis['id'],'result':analysis.get('result'),
        'failure':analysis.get('failure'),'paragraphs':paragraphs,'full_flow_status':status,
        'full_flow_succeeded':complete,'exit_code':0 if complete else 1}


async def main(args):
    env={**dotenv_values(args.env,interpolate=False),**{k:v for k,v in os.environ.items() if k.startswith('REVEAL_')}}
    if (env.get('REVEAL_ENVIRONMENT') not in ('development','test') or
        env.get('REVEAL_JOB_NAMESPACE') in (None,'','reveal') or
        env.get('REVEAL_APPLICATION_TABLE_PREFIX') in (None,'','reveal')):
        raise RuntimeError('Scientific probe requires an explicitly isolated development/test namespace')
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    state=json.loads(output.read_text()) if output.exists() else {'namespace':env['REVEAL_JOB_NAMESPACE'],'created_at':time.time(),'events':{}}
    if state['namespace']!=env['REVEAL_JOB_NAMESPACE']: raise RuntimeError('Probe state belongs to another namespace')
    reports=output.parent/(output.stem+'-reports')
    def save(): write_private(output,json.dumps(state,indent=2).encode()+b'\n')
    def report(name,value): write_private(reports/name,canonical(value))
    if not state.get('job'):
        state['budget_observation']=budget_observation(env,args.max_run_budget_usd)
        state['requested_job_budgets']={'max_accounts':1,'mcp_calls':20,'evidence_tokens':24000}
        save()
    print(json.dumps({'budget_observation':state.get('budget_observation'),
                      'resuming_existing_job':bool(state.get('job')),'report':str(output)}),flush=True)
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
            key=state.setdefault('principal_idempotency_key','workflow-probe-'+str(uuid4())); save()
            me=await request('POST','/internal/v1/principals/anonymous',json={},headers={
                'Authorization':'Bearer '+env['REVEAL_GATEWAY_SERVICE_TOKEN'],'Idempotency-Key':key})
            state['user_id']=me['user_id']; save()
        if not state.get('draft'):
            if not state.get('draft_input'):
                factors=await request('GET','/v1/mechanisms/search',params={'q':args.factor_query or args.query,
                    'source':'eaggl','mode':'lexical','limit':5})
                model=model_from_records(factors['items'],args.model)
                state['reference_model']=model; report('reference-model.json',factors)
                found=await request('GET','/v1/knowledge-gaps/search',params={'q':args.query,'limit':10,'mode':'fuzzy'})
                if not found['items']: raise RuntimeError('No source gap matched probe query')
                gap=next((item['gap'] for item in found['items'] if 'reverse_causation' in item['gap']['source']['source_id']),found['items'][0]['gap'])
                source={'id':gap['object']['id'],'source_id':gap['source']['source_id'],'source_revision':gap['source']['source_revision']}
                suggestions=await request('POST','/v1/mechanisms/suggest',json={'source_gap':source,'manual_eaggl_anchors':[],
                    'dismissed_source_ids':[],'subquery':'','mode':'semantic','model':model})
                if not suggestions['automatic_anchors']: raise RuntimeError('No compatible Vector suggestions')
                factor=suggestions['automatic_anchors'][0]['factor']
                if model_from_records([{'record':factor}])!=model: raise RuntimeError('Suggested factor model changed')
                report('suggestions.json',suggestions)
                composer={'source_gap':source,'eaggl_anchors':[{'reference':{'source':'eaggl','source_id':factor['source_id'],
                    'source_revision':factor['source_revision'],'dapper_id':factor['object']['id']},'origin':'automatic',
                    'suggestion_id':suggestions['suggestion_id']}], 'dismissed_source_ids':[],'mechanism_subquery':'',
                    'model':model,'selected_kgs':['biomarkerkg']}
                state['draft_input']={'name':'Durable Workflow scientific pilot','composer':composer}
                state['source_gap']=source; state['vector_provenance']=suggestions.get('search'); save()
            key=state.setdefault('draft_idempotency_key','workflow-probe-draft-'+str(uuid4())); save()
            state['draft']=await request('POST','/v1/drafts',json=state['draft_input'],
                headers={**auth(),'Idempotency-Key':key})
            save()
        if args.prepare_only:
            print(json.dumps({'prepared':True,'namespace':state['namespace'],'report':str(output)}),flush=True)
            return 0
        if not state.get('job'):
            draft=state['draft']; key=state.setdefault('job_idempotency_key',str(uuid4())); save()
            state['job']=await request('POST','/v1/jobs',json={'kind':'analysis','draft_id':draft['id'],'draft_version':draft['version'],
                                      'budgets':state['requested_job_budgets']},
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
        paragraphs=[]
        report('analysis-result.json',final)
        # Fetch the server-retained input independently of success. It records
        # the actual release/authoring contract used by this execution.
        evidence_response=await client.get('/v1/jobs/'+final['id']+'/evidence-package',headers=auth())
        if evidence_response.status_code==200:
            evidence=evidence_response.json(); report('evidence-package-response.json',evidence)
            package=evidence['package']; expected=json.loads((ROOT/'services/backend/agent-runtime/dapper-release.json').read_bytes())
            kit=package.get('authoring_kit',{})
            package_sha=hashlib.sha256(canonical(package)).hexdigest()
            lock_sha=hashlib.sha256((ROOT/'services/backend/agent-runtime/dapper-release.json').read_bytes()).hexdigest()
            if package_sha!=evidence['package_sha256']: raise RuntimeError('Retained evidence package checksum mismatch')
            if (kit.get('release_tag')!=expected['tag'] or kit.get('release_commit')!=expected['commit'] or
                kit.get('release_lock_sha256')!=lock_sha or
                kit.get('kit_sha256')!=hashlib.sha256(canonical(kit.get('files'))).hexdigest()):
                raise RuntimeError('Hosted authoring contract does not match this checkout release')
            if package['dapper_pin']['snapshot_sha256'] not in expected['compatible_input_snapshots']:
                raise RuntimeError('Hosted evidence snapshot is not approved by the release')
            current=json.loads((CURRENT_DAPPER_SNAPSHOT/'snapshot.json').read_bytes())
            if state.get('requested_job_budgets') and package['dapper_pin']['snapshot_sha256']!=current['snapshot_sha256']:
                raise RuntimeError('Newly generated hosted evidence did not use the current DAPPER snapshot')
            if state.get('requested_job_budgets') and package['authoring']['max_accounts']!=state['requested_job_budgets']['max_accounts']:
                raise RuntimeError('Hosted authoring limit differs from submitted account budget')
            state['contract_verified']={'tag':expected['tag'],'commit':expected['commit'],'release_lock_sha256':lock_sha,
                'evidence_package_sha256':package_sha,'dapper_pin':package['dapper_pin'],'kit_sha256':kit['kit_sha256'],
                'max_accounts':package['authoring']['max_accounts']}; save()
        else:
            state['contract_verification_unavailable']={'http_status':evidence_response.status_code}; save()
            if final['status']=='succeeded': raise RuntimeError('Successful job lacks its retained evidence package for verification')
        if final['status']=='succeeded':
            total_bytes=0; verified=set()
            accounts=[*(final.get('result') or {}).get('account_ids',[]),*(final.get('result') or {}).get('reused_account_ids',[])]
            for account_index,account in enumerate(accounts,1):
                document={}; artifacts={}; pending=[('/v1/accounts/'+quote(account,safe=''),{})]; seen=set(); missing=set()
                for page_number in range(200):
                    if not pending: break
                    path,params=pending.pop(0); marker=(path,json.dumps(params,sort_keys=True))
                    if marker in seen: continue
                    seen.add(marker)
                    detail=await request('GET',path,params=params,headers=auth())
                    if detail.get('schema',{}).get('dependency_snapshot_sha256')!=lock_sha:
                        raise RuntimeError('Account projection uses an unexpected DAPPER release lock')
                    report(f'account-{account_index}/page-{page_number}.json',detail)
                    for group,rows in detail.get('document',{}).items():
                        if not isinstance(rows,list): continue
                        by_id={node.get('id') or hashlib.sha256(canonical(node)).hexdigest():node for node in document.get(group,[])}
                        for node in rows:
                            key=node.get('id') or hashlib.sha256(canonical(node)).hexdigest()
                            if key in by_id and by_id[key]!=node: raise RuntimeError('Account payload changed during provenance traversal')
                            by_id[key]=node
                        document[group]=list(by_id.values())
                    for item in detail.get('artifacts',[]): artifacts[item['file']['id']]=item
                    coverage=detail.get('coverage',{}); cursor=coverage.get('next_cursor')
                    if cursor: pending.insert(0,(path,{'cursor':cursor}))
                    else: missing.update(coverage.get('missing_ids',[]))
                    known={node.get('id') for rows in document.values() for node in rows}
                    for identity in sorted(missing-known):
                        target='/v1/objects/'+quote(identity,safe='')
                        if (target,'{}') not in seen and (target,{}) not in pending: pending.append((target,{}))
                known={node.get('id') for rows in document.values() for node in rows}
                report(f'account-{account_index}/document.json',document)
                unavailable=[]
                for artifact in artifacts.values():
                    file=artifact['file']; checksum=file.get('sha256')
                    if artifact.get('availability')!='available' or not checksum:
                        unavailable.append(file['id']); continue
                    if checksum in verified: continue
                    target=reports/'artifacts'/checksum
                    if target.exists():
                        if target.stat().st_size>MAX_ARTIFACT_BYTES: raise RuntimeError('Cached artifact exceeds size bound')
                        raw=target.read_bytes()
                    else:
                        raw=await download_artifact(client,auth(),checksum)
                    if hashlib.sha256(raw).hexdigest()!=checksum: raise RuntimeError('Authorized download checksum mismatch')
                    if file.get('size_in_bytes') is not None and len(raw)!=file['size_in_bytes']: raise RuntimeError('Artifact size mismatch')
                    total_bytes+=len(raw)
                    if total_bytes>MAX_TOTAL_BYTES: raise RuntimeError('Probe artifact download aggregate bound exceeded')
                    write_private(target,raw); verified.add(checksum)
                findings=relationship_report(document,verified)
                findings.update(coverage_complete=not pending and not (missing-known),missing_ids=sorted(missing-known),
                    unavailable_file_ids=unavailable,verified_artifact_count=len(verified))
                report(f'account-{account_index}/relationship-provenance.json',findings)
                state.setdefault('accounts',{})[account]={'root_id':account,'artifact_count':len(artifacts),
                    'relationship_report':f'account-{account_index}/relationship-provenance.json',
                    'relationship_counts':{key:len(value) for key,value in findings['relationship_families'].items()},
                    'coverage_complete':findings['coverage_complete'],'unavailable_file_ids':unavailable}; save()
            state['verified_artifact_sha256']=sorted(verified); save()
            def record_paragraph(identity,observation):
                state.setdefault('paragraphs',{})[identity]=observation; save()
            paragraphs=await capture_paragraph_results(final,wait_job=wait_job,request=request,auth=auth,
                report=report,record=record_paragraph,release_lock_sha256=lock_sha)
        elif final['status']=='insufficient_evidence':
            outcome=await request('GET','/v1/jobs/'+final['id']+'/outcome',headers=auth())
            report('insufficient-evidence-outcome.json',outcome)
            state['outcome_verified']=bool(outcome); save()
        summary={**workflow_summary(final,paragraphs),'report':str(output)}
        state['workflow_summary']=summary; save(); report('workflow-summary.json',summary)
        print(json.dumps(summary),flush=True)
        return summary['exit_code']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',default='.runtime/workflow/backend.env')
    parser.add_argument('--base',default='http://127.0.0.1:18001')
    parser.add_argument('--output',default='.runtime/workflow/scientific-probe.json')
    parser.add_argument('--query',default='coronary artery disease')
    parser.add_argument('--factor-query',help='Lexical factor query for active-model discovery; defaults to --query')
    parser.add_argument('--model',choices=('cfde-inc-v2','eaggl-capped-v1'),help='Optional reference model; must match active API records')
    parser.add_argument('--max-run-budget-usd',type=float,default=10,help='Refuse submission if configured analysis plus one paragraph model caps exceed this amount; excludes Box/storage')
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--max-seconds',type=int,default=2400)
    sys.exit(asyncio.run(main(parser.parse_args())))
