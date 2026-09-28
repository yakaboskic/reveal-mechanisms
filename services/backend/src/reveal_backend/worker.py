"""Aurora-backed worker. All scientific writes are fenced and backend validated."""
import asyncio
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import signal
import socket
import time
from .agent_execution import ExecutionRequest, MAX_EMIT_BATCH_EVENTS, MAX_EMIT_BATCH_BYTES, emit_batch_size
from .auth import Problem, owned
from .acceptance import assemble_account, object_envelope, release_root, LOCK, mint, validate_paragraph_document
from .evidence_package import DapperRuntime, canonical_json, decode, require, sha256
from .evidence_schema import validate_package_shape, load_generated_schema
from .evidence_collector import collect_package
from .evidence_database import geneset_resolver
from .evidence_budget import fit_input_budget
from .repository import Repository, now, uid, digest
from .runtime_config import ROOT, setting, artifacts_root
from . import jobs

log=logging.getLogger('reveal.worker')

def activity(kind,state='started',source='worker',**values):
    return {'kind':kind,'state':state,'source':source,'call_id':None,'tool_name':None,'selected_kg':None,
        'display_arguments':None,'output_excerpt':None,'artifact_sha256':None,'duration_ms':None,'counts':None,**values}

def public_activity(job,kind,payload):
    """Map observable transport data; the public event contract is unchanged."""
    message=str(payload.get('message') or payload.get('text') or kind.replace('_',' '))[:16000]
    if kind=='stage': job['stage']=payload['stage']; detail=activity('preparation',payload.get('state','started'))
    elif kind in ('tool_call','tool_result'):
        detail=activity(kind,payload.get('state') or ('failed' if kind=='tool_result' and payload.get('status') in ('error','failed') else 'completed' if kind=='tool_result' else 'started'),'harness',
            call_id=payload.get('call_id'),tool_name=payload.get('tool_name') or payload.get('tool'),selected_kg=payload.get('selected_graph'),
            display_arguments=str(payload.get('display_arguments',''))[:8000] or None,output_excerpt=str(payload.get('output_excerpt',''))[:16000] or None)
    elif kind in ('agent_started','agent_message','message','agent_completed'):
        detail=activity('agent_message','completed' if kind=='agent_completed' else 'started','harness',
            **({'message_delta':True} if payload.get('delta') is True else {}))
    elif kind=='warning':
        detail=None; job['warnings'].append(message)
    else: return None
    return ('warning' if kind=='warning' else 'activity',message,detail)

def persist_activity_batch(repository,job_id,token,events):
    """Atomically deliver bounded individual events, preserving replay identity."""
    if len(events)>MAX_EMIT_BATCH_EVENTS or emit_batch_size(events)>MAX_EMIT_BATCH_BYTES:
        raise ValueError('Observable event batch exceeds its bounded transport limit')
    if not events: return
    deliveries=[digest([job_id,payload.get('remote_stream_id'),payload['remote_sequence']])
        if payload.get('remote_sequence') is not None else None for _,payload in events]
    with repository.transaction() as tx:
        pair=jobs.fenced(tx,job_id,token)
        if not pair: raise RuntimeError('Attempt lease lost')
        current,_=pair; owner=current['owner_user_id']
        seen=set(tx.get_many('remote_event',list(dict.fromkeys(identity for identity in deliveries if identity))))
        records=[]; changed=False
        for (kind,payload),delivery in zip(events,deliveries):
            if delivery:
                if delivery in seen: continue
                seen.add(delivery)
                records.append(('remote_event',delivery,owner,{'job_id':job_id,'sequence':payload['remote_sequence']}))
            mapped=public_activity(current,kind,payload)
            if mapped is None: continue
            item=jobs.event_record(current,*mapped)
            records.append(('event',job_id+':'+item['id'].zfill(12),owner,item)); changed=True
        tx.insert_many(records)
        if changed: tx.update_existing('job',job_id,owner,current)

def assert_artifact(path,directory):
    path=Path(path).resolve(); require(path.is_relative_to(directory.resolve()) and path.is_file(),'Execution output escapes attempt directory')
    return path

def restore_dispatch_input(root,snapshot):
    path=assert_artifact(root/snapshot['path'],root)
    raw=path.read_bytes(); require(sha256(raw)==snapshot['sha256'],'Frozen dispatch input checksum changed')
    return path,decode(raw)

def validate_execution_ledger(result,request,expected_model=None):
    require(result.runtime_manifest_path is not None and result.ledger_manifest_path is not None,'Execution lacks trusted runtime or tool ledger')
    runtime_path=assert_artifact(result.runtime_manifest_path,request.output_dir)
    runtime=decode(runtime_path.read_bytes())
    require(runtime.get('job_id')==request.job_id,'Runtime belongs to another job')
    require(runtime.get('attempt')==request.attempt,'Runtime belongs to another attempt')
    require(runtime.get('input_sha256')==sha256(request.input_path.read_bytes()),'Execution used different input bytes')
    lock=decode(LOCK.read_bytes())
    require(runtime.get('dapper',{}).get('commit')==lock['commit'],'Execution used a different DAPPER release')
    require(runtime.get('model')==(expected_model or setting('REVEAL_CLAUDE_MODEL','claude-sonnet-4-6')),'Execution model differs from frozen input model')
    ledger_path=assert_artifact(result.ledger_manifest_path,request.output_dir); ledger=decode(ledger_path.read_bytes())
    require(ledger.get('format')=='reveal.tool-ledger/1' and ledger.get('job_id')==request.job_id and ledger.get('attempt')==request.attempt and ledger.get('complete') is True,'Tool ledger is incomplete or belongs to another execution')
    sequences=set()
    for call in ledger['calls']:
        require(call['sequence'] not in sequences,'Duplicate ledger sequence'); sequences.add(call['sequence'])
        require(call.get('selected_graph') is None or call['selected_graph'] in request.selected_graphs,'Execution queried an unselected graph')
        require(call.get('status') in ('completed','empty','failed','denied','interrupted'),'Tool call has no final result')
        for field in ('request','response','upstream_request'):
            artifact=call.get(field)
            if field in ('request','response'): require(artifact is not None,'Tool call is missing complete request/result capture')
            if artifact:
                path=assert_artifact(ledger_path.parent/artifact['path'],ledger_path.parent)
                data=path.read_bytes(); require(sha256(data)==artifact['sha256'] and len(data)==artifact['size_bytes'],'Tool ledger artifact checksum mismatch')
        if call.get('status')=='denied':
            # Legacy root proxy classified this precise draft-schema feedback as
            # denied. It performs no retrieval or mutation; real policy denials
            # remain fatal. New proxies record this as failed validation.
            response=decode((ledger_path.parent/call['response']['path']).read_bytes())
            legacy_feedback=call.get('tool')=='write_account_draft' and response=={'content':[{'text':'Draft must contain one ScientificAccount','type':'text'}],'isError':True}
            require(legacy_feedback,'Execution attempted a prohibited tool action')

def enrichment_status(result,selected,mode):
    if mode=='deterministic': return [{'graph':g,'status':'skipped','source_claim_ids':[],'detail':'Development execution does not query external graphs.'} for g in selected]
    ledger=decode(Path(result.ledger_manifest_path).read_bytes())
    items=[]
    for graph in selected:
        calls=[c for c in ledger['calls'] if c.get('selected_graph')==graph and c.get('tool')=='query_graph']
        if not calls: status,detail='skipped','The agent did not query this selected graph.'
        elif any(c['status'] in ('failed','interrupted','denied') for c in calls): status,detail='unavailable','At least one selected graph query failed; inspect the execution ledger.'
        elif all(c['status']=='empty' for c in calls): status,detail='no_match','All completed graph queries returned explicit empty results.'
        else: status,detail='matched','The graph returned results; returned results are not automatically accepted scientific claims.'
        items.append({'graph':graph,'status':status,'source_claim_ids':[],'detail':detail})
    return items

def collect(job,frozen,binding,budgets,directory):
    package_path=directory/'package/evidence-package.json'
    if not package_path.exists():
        completed=sorted(directory.parent.glob(directory.name+'-recovery-*/package/evidence-package.json'))
        if completed: package_path=completed[0]
    if package_path.exists():
        package=decode(package_path.read_bytes())
    else:
        # Keep incomplete captures for diagnosis; a crash before a complete
        # package must not make every recovered preparation fail on mkdir.
        if directory.exists() and any(directory.iterdir()):
            directory=directory.parent/(directory.name+'-recovery-'+uid())
            package_path=directory/'package/evidence-package.json'
        runtime=DapperRuntime(ROOT/'data/dapper/2026-09-24-v8')
        requested_limit=budgets.get('candidates_per_type',100)
        runs={a.get('embedding_run_id') for a in binding['anchors'] if a.get('embedding_run_id')}
        retrieval=binding.get('retrieval',{})
        metadata={'origins':{a['reference']['source_id']:a['origin'] for a in frozen['composer']['eaggl_anchors']},
            'dismissed_eaggl_ids':frozen['composer']['dismissed_source_ids'],
            'semantic_retrieval':{'status':'computed' if any(r and r.get('mode') in ('semantic','hybrid') for r in retrieval.values()) else 'not_computed',
                'embedding_run_id':next(iter(runs)) if len(runs)==1 else None},'frozen_binding':binding,
            'collection_budget':{'requested_max_candidates_per_type':requested_limit,'effective_candidates_per_type':requested_limit,
                'max_nodes':budgets.get('max_nodes',250),'max_edges':budgets.get('max_edges',1000),
                'policy':'Configured source retrieval and graph bounds; complete captured evidence is supplied as files for bounded on-demand reads.'}}
        if len(runs)!=1: metadata['semantic_retrieval']['status']='not_computed'
        built=collect_package(gap_id=frozen['composer']['source_gap']['source_id'],factor_ids=[a['cfde_node_id'] for a in binding['anchors']],
            output=directory,dapper=runtime,project_root=ROOT,dismech_source=Path(setting('REVEAL_DISMECH_SOURCE',str(ROOT.parent/'dismech'))),
            dismech_index=ROOT/'data/dismech-gaps/2026-09-24',geneset_import=ROOT/'data/cfde-genesets/2026-09-24',
            geneset_resolver=geneset_resolver(binding['anchors'][0]['gene_set_import_id']),
            selected_graphs=frozen['composer']['selected_kgs'],max_accounts=budgets.get('max_accounts',3),selection_metadata=metadata,
            limit=requested_limit,max_nodes=budgets.get('max_nodes',250),max_edges=budgets.get('max_edges',1000))
        package=built.package
    validate_package_shape(package,load_generated_schema(ROOT/'schema/evidence-package.schema.json'))
    gap=next(g for g in package['dapper_context']['knowledge_gaps'] if g['id']==frozen['question_id'])
    require(gap==binding['source_gap']['object'],'API-selected gap differs from collected source payload')
    require(package['dismech']['source_revision']['source_sha256']==frozen['composer']['source_gap']['source_revision'],'Collected DisMech revision differs from frozen request')
    require(set(package['selection']['eaggl_mechanism_ids'])=={a['cfde_node_id'] for a in binding['anchors']},'Collector changed native selected anchors')
    require(package['external_evidence']['selected_graphs']==frozen['composer']['selected_kgs'],'Collector changed selected graphs')
    return package_path,package

class Worker:
    def __init__(self,repository=None,adapter=None):
        self.repository=repository or Repository(); self.adapter=adapter; self.stopping=False
        self.worker_id=socket.gethostname()+':'+uid()
    async def process(self,job,queue):
        token=queue['token']; mode=queue.get('dispatch_input',{}).get('mode') or ('box' if queue.get('remote_handle') else setting('REVEAL_EXECUTION_MODE','box'))
        require(mode in ('box','deterministic'),'REVEAL_EXECUTION_MODE must be box or deterministic')
        require(mode!='deterministic' or setting('REVEAL_ENVIRONMENT','development') in ('development','test'),'Deterministic execution is restricted to development/test environments')
        root=artifacts_root()/job['id']; directory=root/('attempt-'+str(queue['attempt'])); directory.mkdir(parents=True,exist_ok=True)
        lost=False
        def cancellation_requested():
            with self.repository.transaction() as tx:
                pair=jobs.fenced(tx,job['id'],token)
                return pair is None or pair[0]['status']=='cancel_requested'
        async def cancelled():
            if self.stopping or lost: return True
            try:
                return await asyncio.to_thread(cancellation_requested)
            except Exception as exc:
                from .box_adapter import BoxTransportError
                raise BoxTransportError('Cancellation state unavailable; retain the existing execution') from exc
        async def lease_loop():
            nonlocal lost
            while True:
                await asyncio.sleep(20)
                if not await asyncio.to_thread(jobs.heartbeat,self.repository,job['id'],token): lost=True; return
        def persist_checkpoint(handle):
            with self.repository.transaction() as tx:
                pair=jobs.fenced(tx,job['id'],token)
                if not pair: raise RuntimeError('Attempt lease lost')
                current,q=pair; q['remote_handle']=handle
                tx.put('queue',job['id'],current['owner_user_id'],q)
        async def checkpoint(handle):
            try:
                await asyncio.to_thread(persist_checkpoint,handle)
            except Exception as exc:
                from .box_adapter import BoxTransportError
                raise BoxTransportError('Remote cursor checkpoint interrupted; replay the durable cursor') from exc
        repository=self.repository
        class EventEmitter:
            async def __call__(self,kind,payload):
                await self.emit_batch(((kind,payload),))
            async def emit_batch(self,events):
                try:
                    await asyncio.to_thread(persist_activity_batch,repository,job['id'],token,events)
                except ValueError:
                    raise
                except Exception as exc:
                    from .box_adapter import BoxTransportError
                    raise BoxTransportError('Observable event persistence interrupted; replay the durable cursor') from exc
        emit=EventEmitter()
        lease=asyncio.create_task(lease_loop())
        phase='evidence_preparation'
        try:
            if await cancelled() and not queue.get('remote_handle'):
                jobs.finish(self.repository,job['id'],token,'cancelled'); return
            if mode=='deterministic': await emit('warning',{'message':'DEVELOPMENT SIMULATION — not a live scientific result.'})
            with self.repository.transaction() as tx:
                if job['kind']=='analysis':
                    frozen=tx.get('request',job['research_request_id'])['data']; binding=tx.get('request_binding',job['research_request_id'])['data']
                else:
                    owner=tx.get('job',job['id'])['owner']
                    stored=owned(tx,'account',job['input_account_id'],owner)['data']; document=stored['result']['document']
            snapshot=queue.get('dispatch_input')
            if queue.get('remote_handle') and not snapshot:
                # Backward-compatible recovery of already launched attempts:
                # locate a frozen manifest, never recollect or choose new bytes.
                candidates=[]
                if job['kind']=='analysis':
                    for manifest in root.glob('evidence*/dispatch-input.json'):
                        saved=decode(manifest.read_bytes())
                        if saved['binding']['mode']=='box': candidates.append((manifest.parent/saved['path'],saved['sha256'],saved['binding']['model']))
                else:
                    previous=directory/'paragraph-input.json'
                    if previous.exists(): candidates.append((previous,sha256(previous.read_bytes()),setting('REVEAL_CLAUDE_MODEL','claude-sonnet-4-6')))
                require(len(candidates)==1,'Remote recovery requires one exact frozen input; recollection is forbidden')
                previous,checksum,model=candidates[0]
                snapshot={'path':str(previous.resolve().relative_to(root.resolve())),'sha256':checksum,'mode':'box','model':model,'kind':job['kind']}
            if snapshot:
                require(snapshot['kind']==job['kind'],'Frozen dispatch kind changed')
                input_path,restored=restore_dispatch_input(root,snapshot)
                if job['kind']=='analysis': package=restored; selected=tuple(frozen['composer']['selected_kgs'])
                else: paragraph_input=restored; selected=()
            elif job['kind']=='analysis':
                await emit('stage',{'stage':'preparing_evidence','message':'Collecting the frozen DisMech question and native CFDE mechanism evidence.'})
                input_path,package=await asyncio.to_thread(collect,job,frozen,binding,queue['inputs'].get('budgets',{}),root/'evidence')
                input_path,package,measurement=await asyncio.to_thread(fit_input_budget,input_path,mode,queue['inputs'].get('budgets',{}).get('evidence_tokens',24000))
                (directory/'token-budget.json').write_bytes(canonical_json(measurement))
                with self.repository.transaction() as tx:
                    pair=jobs.fenced(tx,job['id'],token)
                    if not pair: return
                    tx.put('evidence',job['id'],pair[0]['owner_user_id'],{'job_id':job['id'],'package_sha256':sha256(input_path.read_bytes()),'package':package})
                selected=tuple(frozen['composer']['selected_kgs'])
            else:
                from .citations import register
                with self.repository.transaction() as tx:
                    owner=tx.get('job',job['id'])['owner']
                    metadata=stored['result']['citation_metadata']
                allowed=[{'target_id':m['target_id'],'citation_metadata_revision':m['metadata_revision']} for m in metadata]
                # Claims first makes deterministic paragraph cite an assessed target.
                allowed.sort(key=lambda x:0 if x['target_id'].startswith('dapper:Claim.') else 1)
                paragraph_input={'format':'reveal.paragraph-input/1','account_document':document,'account_id':job['input_account_id'],'allowed_citations':allowed}
                input_path=directory/'paragraph-input.json'; input_path.write_bytes(canonical_json(paragraph_input)); selected=()
            if snapshot is None:
                snapshot={'path':str(input_path.resolve().relative_to(root.resolve())),'sha256':sha256(input_path.read_bytes()),
                    'mode':mode,'model':setting('REVEAL_CLAUDE_MODEL','claude-sonnet-4-6'),'kind':job['kind']}
            with self.repository.transaction() as tx:
                pair=jobs.fenced(tx,job['id'],token)
                if not pair: return
                current,q=pair; q['dispatch_input']=snapshot; tx.put('queue',job['id'],current['owner_user_id'],q)
            if await cancelled() and not queue.get('remote_handle'):
                jobs.finish(self.repository,job['id'],token,'cancelled'); return
            phase='agent_execution'
            await emit('stage',{'stage':'starting_agent' if job['kind']=='analysis' else 'authoring_paragraph','message':'Starting the configured execution adapter.'})
            adapter=self.adapter
            if adapter is None:
                if mode=='deterministic':
                    from .deterministic_adapter import DeterministicAdapter
                    adapter=DeterministicAdapter()
                else:
                    from .box_adapter import BoxExecutionAdapter
                    adapter=BoxExecutionAdapter(ROOT,environ={**os.environ,'REVEAL_CLAUDE_MODEL':snapshot['model']})
            request=ExecutionRequest(job_id=job['id'],attempt=queue['attempt'],kind='research' if job['kind']=='analysis' else 'paragraph',input_path=input_path,
                output_dir=directory/'output',selected_graphs=selected,timeout_seconds=int(setting('REVEAL_AGENT_TIMEOUT_SECONDS','900')),
                max_budget_usd=float(setting('REVEAL_AGENT_MAX_BUDGET_USD','3')),max_turns=int(setting('REVEAL_AGENT_MAX_TURNS','40')),remote_handle=queue.get('remote_handle'))
            result=await adapter.execute(request,emit,cancelled,checkpoint)
            if await cancelled():
                jobs.finish(self.repository,job['id'],token,'cancelled'); return
            if result.status!='succeeded':
                failure={'code':'AGENT_EXECUTION_FAILED','message':result.reason or 'The execution did not complete.','retryable':True} if result.status=='failed' else None
                jobs.finish(self.repository,job['id'],token,result.status,failure=failure); return
            phase='scientific_validation'
            if mode=='box': validate_execution_ledger(result,request,snapshot['model'])
            await emit('stage',{'stage':'validating','message':'Validating scientific identities, source fidelity and provenance.'})
            if job['kind']=='analysis':
                require(0<len(result.account_paths)<=queue['inputs'].get('budgets',{}).get('max_accounts',3),'Execution returned an invalid account count')
                accepted=[]
                for index,path in enumerate(result.account_paths):
                    raw=assert_artifact(path,request.output_dir); final_path=directory/f'accepted-{index+1}.json'
                    doc,report=await asyncio.to_thread(assemble_account,raw,input_path,final_path,frozen['attribution'],job,queue['attempt'],mode,
                        result.ledger_manifest_path if mode=='box' else None)
                    if mode=='box':
                        from .scientific_grounding import review_account
                        grounding=await asyncio.to_thread(review_account,doc,package,result.ledger_manifest_path,
                            model=snapshot['model'],api_key=setting('ANTHROPIC_API_KEY'),
                            max_budget_usd=float(setting('REVEAL_GROUNDING_MAX_BUDGET_USD','0.30')))
                        (directory/f'grounding-{index+1}.json').write_bytes(canonical_json(grounding))
                        require(grounding['accepted'],'Independent source-grounding review rejected unsupported or overstated scientific content')
                    (directory/f'validation-{index+1}.json').write_bytes(canonical_json(report)); accepted.append((doc,report,final_path))
                await self.accept_accounts(job,token,accepted,frozen,input_path,result,directory,mode)
            else:
                require(result.paragraph_path is not None,'Paragraph execution returned no segments')
                raw=assert_artifact(result.paragraph_path,request.output_dir)
                segments=decode(raw.read_bytes())
                if mode=='box':
                    from .scientific_grounding import review_paragraph
                    grounding=await asyncio.to_thread(review_paragraph,segments,paragraph_input,
                        model=snapshot['model'],api_key=setting('ANTHROPIC_API_KEY'),
                        max_budget_usd=float(setting('REVEAL_GROUNDING_MAX_BUDGET_USD','0.30')))
                    (directory/'paragraph-grounding.json').write_bytes(canonical_json(grounding))
                    require(grounding['accepted'],'Independent source-grounding review rejected unsupported or overstated paragraph content')
                await self.accept_paragraph(job,token,segments,paragraph_input,directory)
            if await cancelled(): jobs.finish(self.repository,job['id'],token,'cancelled')
        except Exception as exc:
            from .box_adapter import BoxTransportError
            if isinstance(exc,BoxTransportError):
                with self.repository.transaction() as tx:
                    pair=jobs.fenced(tx,job['id'],token)
                    if pair and pair[1].get('remote_handle'):
                        current,q=pair; q['recoveries']=q.get('recoveries',0)+1
                        from datetime import datetime,timedelta,timezone
                        q['lease_until']=(datetime.now(timezone.utc)+timedelta(seconds=min(60,5*q['recoveries']))).isoformat().replace('+00:00','Z')
                        tx.put('queue',job['id'],current['owner_user_id'],q)
                        jobs.event(tx,current,'warning','Reconnecting to the existing remote execution for result capture or cleanup; no new paid attempt was launched.')
                        return
            # Scientific exceptions are retained as bounded local diagnostics;
            # API errors do not echo third-party headers/URLs/credentials.
            diagnostic={'phase':phase,'error_type':type(exc).__name__,'message':str(exc)[:3000] if not isinstance(exc,OSError) else 'Operating system error'}
            (directory/'failure.json').write_bytes(canonical_json(diagnostic))
            if phase=='evidence_preparation':
                code,message='EVIDENCE_PREPARATION_FAILED','The source evidence could not be prepared.'
            elif phase=='scientific_validation' and isinstance(exc,ValueError):
                code,message='VALIDATION_FAILED','The attempt failed scientific validation.'
            else:
                code,message='WORKER_FAILED','The attempt failed during execution.'
            jobs.finish(self.repository,job['id'],token,'failed',failure={'code':code,
                'message':message+' The saved draft and existing accounts are preserved.','retryable':True})
            log.error('Job %s failed during %s (%s)',job['id'],phase,type(exc).__name__)
        finally:
            lease.cancel()
            try: await lease
            except asyncio.CancelledError: pass

    async def accept_accounts(self,job,token,accepted,frozen,package_path,result,directory,mode):
        from .citations import register
        with self.repository.transaction() as tx:
            pair=jobs.fenced(tx,job['id'],token)
            if not pair or pair[0]['status']=='cancel_requested': return
            current,_=pair; owner=current['owner_user_id']; accounts=[]; paragraphs=[]; manifest_accounts=[]
            package=decode(package_path.read_bytes()); artifact_access={}
            files={f['id']:f for f in package['dapper_context'].get('files',[])}
            for source in package['source_artifacts'].values():
                file=files.get(source['dapper_file_id'])
                if not file: continue
                path=assert_artifact(package_path.parent/source['path'],package_path.parent)
                data=path.read_bytes(); require(sha256(data)==source['sha256'],'Source artifact changed before persistence')
                tx.put('artifact',digest([owner,source['sha256']]),owner,{'sha256':source['sha256'],'file':file,'path':str(path),'job_id':job['id']})
                url=setting('NEXTAUTH_URL','http://localhost:3000').rstrip('/')+'/api/backend/v1/artifacts/'+source['sha256']
                artifact_access[file['id']]={'file':file,'download_url':url,'expires_at':None,'availability':'available','verification':'checksum_verified'}
            for doc,report,path in accepted:
                if mode=='box':
                    from .acceptance import ledger_sources
                    captured=ledger_sources(result.ledger_manifest_path)
                    for file in doc.get('files',[]):
                        source=captured.get(file.get('sha256'))
                        if source:
                            tx.put('artifact',digest([owner,file['sha256']]),owner,{'sha256':file['sha256'],'file':file,'path':str(source['path']),'job_id':job['id']})
                            url=setting('NEXTAUTH_URL','http://localhost:3000').rstrip('/')+'/api/backend/v1/artifacts/'+file['sha256']
                            artifact_access[file['id']]={'file':file,'download_url':url,'expires_at':None,'availability':'available','verification':'checksum_verified'}
                runtime=decode(Path(result.runtime_manifest_path).read_bytes()) if result.runtime_manifest_path else {'model_id':None,'harness_version':None}
                runtime['model_id']=runtime.get('model_id') or runtime.get('model')
                actor=doc['scientific_accounts'][0].get('was_attributed_to',[None])[0]
                metadata=register(tx,owner,doc,{**frozen['attribution'],'person_id':actor},now(),runtime=runtime)
                account=doc['scientific_accounts'][0]; identity=account['id']; gap=next(g for g in doc['knowledge_gaps'] if g['id']==account['question'])
                outbox_key=digest([owner,identity,'default-paragraph']); previous=tx.get('outbox',outbox_key)
                if previous:
                    paragraph=tx.get('job',previous['data']['job_id'])['data']
                    old_account=owned(tx,'account',identity,owner)['data']
                    state=old_account['result']['research_statement']
                else:
                    paragraph=jobs.enqueue(tx,owner,'paragraph',account_id=identity,inputs={'kind':'paragraph','account_id':identity})
                    state={'status':'queued','job_id':paragraph['id'],'paragraph_id':None}
                envelope=object_envelope(doc,identity,metadata,artifact_access); envelope['research_statement']=state
                summary={'account':account,'knowledge_gap':gap,'claim_count':len(account['component_claims']),'created_at':now(),'job_id':job['id'],'research_statement':state}
                if not previous:
                    tx.put('account',digest([owner,identity]),owner,{'result':envelope,'summary':summary})
                    tx.put('account_membership',digest([owner,identity]),owner,{'account_id':identity,'summary':summary})
                document_sha=sha256(path.read_bytes())
                tx.put('scientific_document',digest([owner,document_sha]),owner,{'sha256':document_sha,'document':doc,'job_id':job['id'],'observed_at':now(),
                    'citation_metadata':metadata,'artifact_access':artifact_access})
                for rows in doc.values():
                    if isinstance(rows,list):
                        for node in rows:
                            if isinstance(node,dict) and str(node.get('id','')).startswith('dapper:'):
                                tx.put('grant',digest([owner,node['id']]),owner,{'target_id':node['id']})
                                projection=object_envelope(doc,node['id'],metadata,artifact_access)
                                tx.put('object_observation',digest([owner,node['id'],sha256(canonical_json(node))]),owner,{'object_id':node['id'],'payload':node,'document_sha256':document_sha})
                                if not tx.get('object',digest([owner,node['id']])):
                                    tx.put('object',digest([owner,node['id']]),owner,projection)
                                    tx.put('object_document',digest([owner,node['id']]),owner,{'object_id':node['id'],'sha256':document_sha})
                if not previous: tx.put('outbox',outbox_key,owner,{'account_id':identity,'job_id':paragraph['id'],'dispatched':True})
                accounts.append(identity); paragraphs.append(paragraph['id'])
                manifest_accounts.append({'path':str(path.resolve().relative_to(directory.resolve())),'sha256':sha256(path.read_bytes()),'account_id':identity,'lint_report_sha256':sha256(canonical_json(report))})
            public={'kind':'analysis','request_id':job['research_request_id'],'account_ids':accounts,'enrichment':enrichment_status(result,frozen['composer']['selected_kgs'],mode),
                'paragraph_job_ids':paragraphs,'evidence_package_sha256':sha256(package_path.read_bytes())}
            current.update(status='succeeded',stage='complete',result=public,completed_at=now())
            jobs.event(tx,current,'result','Gap analysis complete.' if mode=='box' else 'Development simulation complete — not a scientific result.')
            manifest={'format':'reveal.agent-output/1','job_id':job['id'],'attempt':pair[1]['attempt'],'status':'succeeded','input_package_sha256':public['evidence_package_sha256'],
                'runtime_manifest_sha256':sha256(Path(result.runtime_manifest_path).read_bytes()) if result.runtime_manifest_path else digest({'mode':mode}),
                'ledger_manifest_sha256':sha256(Path(result.ledger_manifest_path).read_bytes()) if result.ledger_manifest_path else None,'accounts':manifest_accounts,'reason':None}
            (directory/'worker-output.json').write_bytes(canonical_json(manifest))

    async def accept_paragraph(self,job,token,raw,inputs,directory):
        from .box_paragraph import assemble_paragraph
        assembled=assemble_paragraph(raw,inputs,dapper_root=release_root(),release_lock=LOCK)
        document=deepcopy(inputs['account_document'])
        activity_id='urn:reveal:paragraph-execution:'+job['id']
        document.setdefault('activities',[]).append({'id':activity_id,'name':'REVEAL cited research statement generation',
            'command':'reveal-worker paragraph','software_name':'REVEAL Mechanisms worker','software_version':'0.2.0','generated_at_time':now()})
        document.setdefault('used_edges',[]).append({'subject':activity_id,'predicate':'prov:used','object':job['input_account_id']})
        paragraph={'id':'urn:reveal:paragraph:'+job['id'],'scientific_account':job['input_account_id'],'language':'en','access_level':'controlled',**assembled,
            'was_derived_from':[job['input_account_id']],'was_generated_by':activity_id,
            'was_attributed_to':document['scientific_accounts'][0].get('was_attributed_to',[])}
        document.setdefault('paragraphs',[]).append(paragraph)
        path=directory/'accepted-paragraph.json'; document=await asyncio.to_thread(mint,document,path)
        report=await asyncio.to_thread(validate_paragraph_document,path)
        (directory/'paragraph-validation.json').write_bytes(canonical_json(report))
        before={n['id']:n for rows in inputs['account_document'].values() if isinstance(rows,list) for n in rows if isinstance(n,dict) and 'id' in n}
        after={n['id']:n for rows in document.values() if isinstance(rows,list) for n in rows if isinstance(n,dict) and 'id' in n}
        require(all(after.get(identity)==node for identity,node in before.items()),'Paragraph assembly changed accepted scientific content')
        paragraph=document['paragraphs'][-1]
        with self.repository.transaction() as tx:
            pair=jobs.fenced(tx,job['id'],token)
            if not pair or pair[0]['status']=='cancel_requested': return
            current,_=pair; owner=current['owner_user_id']; stored=owned(tx,'account',job['input_account_id'],owner)['data']
            # Exact registry revision is required; never substitute latest.
            for occurrence in paragraph['citations']: owned(tx,'citation',occurrence['target_id']+':'+str(occurrence['citation_metadata_revision']),owner)
            envelope=object_envelope(document,paragraph['id'],stored['result']['citation_metadata'])
            document_sha=sha256(path.read_bytes())
            tx.put('scientific_document',digest([owner,document_sha]),owner,{'sha256':document_sha,'document':document,'job_id':job['id'],'observed_at':now(),
                'citation_metadata':stored['result']['citation_metadata'],'artifact_access':{a['file']['id']:a for a in stored['result']['artifacts']}})
            tx.put('object_document',digest([owner,paragraph['id']]),owner,{'object_id':paragraph['id'],'sha256':document_sha})
            tx.put('paragraph',digest([owner,paragraph['id']]),owner,{'result':envelope,'account_id':job['input_account_id']}); tx.put('object',digest([owner,paragraph['id']]),owner,envelope)
            tx.put('grant',digest([owner,paragraph['id']]),owner,{'target_id':paragraph['id']})
            state={'status':'succeeded','job_id':job['id'],'paragraph_id':paragraph['id']}
            stored['result']['research_statement']=state; stored['summary']['research_statement']=state
            tx.put('account',digest([owner,job['input_account_id']]),owner,stored)
            member=tx.get('account_membership',digest([owner,job['input_account_id']]))
            if member:
                member['data']['summary']['research_statement']=state; tx.put('account_membership',member['id'] if 'id' in member else digest([owner,job['input_account_id']]),owner,member['data'])
            current.update(status='succeeded',stage='complete',completed_at=now(),result={'kind':'paragraph','account_id':job['input_account_id'],'paragraph_id':paragraph['id']})
            jobs.event(tx,current,'result','Cited research statement complete.')

    async def run(self):
        while not self.stopping:
            try:
                claimed=await asyncio.to_thread(jobs.claim,self.repository,self.worker_id)
                if claimed: await self.process(*claimed)
                else: await asyncio.sleep(1)
            except Exception as exc:
                log.error('Worker loop unavailable (%s)',type(exc).__name__); await asyncio.sleep(3)

def main():
    logging.basicConfig(level=logging.INFO)
    worker=Worker()
    def stop(*_): worker.stopping=True
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    asyncio.run(worker.run())

if __name__=='__main__': main()
