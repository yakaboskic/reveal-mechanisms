"""Aurora-backed worker. All scientific writes are fenced and backend validated."""
import asyncio
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import signal
import shutil
import socket
import time
from .agent_execution import ExecutionRequest, MAX_EMIT_BATCH_EVENTS, MAX_EMIT_BATCH_BYTES, agent_budget_usd, emit_batch_size
from .auth import Problem, owned
from .acceptance import assemble_account, claim_structure_record, object_envelope, object_projection, with_citations, release_root, LOCK, mint, prewarm, validate_paragraph_document
from .evidence_package import DapperRuntime, canonical_json, decode, require, sha256
from .evidence_schema import validate_package_shape, load_generated_schema
from .evidence_collector import collect_package
from .evidence_database import geneset_resolver
from .evidence_budget import fit_input_budget
from .reference_generation import KPN_MODEL, LEGACY_MODEL, MODELS, generation_of_anchors
from .repository import Repository, now, uid, digest
from .runtime_config import ROOT, CURRENT_DAPPER_SNAPSHOT, setting, artifacts_root, mysql_connection
from . import jobs
from .artifact_store import s3_enabled, store as artifact_store, retained_file, StorageUnavailable

log=logging.getLogger('reveal.worker')

def activity(kind,state='started',source='worker',**values):
    return {'kind':kind,'state':state,'source':source,'call_id':None,'tool_name':None,'selected_kg':None,
        'display_arguments':None,'output_excerpt':None,'artifact_sha256':None,'duration_ms':None,'counts':None,**values}

def public_activity(job,kind,payload):
    """Map observable transport data; the public event contract is unchanged."""
    message=str(payload.get('message') or payload.get('text') or kind.replace('_',' '))[:16000]
    if kind=='stage':
        job['stage']=payload['stage']
        detail=activity('preparation',payload.get('state','started'),'harness' if payload.get('source')=='harness' else 'worker')
    elif kind in ('tool_call','tool_result'):
        detail=activity(kind,payload.get('state') or ('failed' if kind=='tool_result' and payload.get('status') in ('error','failed') else 'completed' if kind=='tool_result' else 'started'),'harness',
            call_id=payload.get('call_id'),tool_name=payload.get('tool_name') or payload.get('tool'),selected_kg=payload.get('selected_graph'),
            display_arguments=payload['display_arguments'][:8000] if isinstance(payload.get('display_arguments'),str) else None,
            output_excerpt=payload['output_excerpt'][:16000] if isinstance(payload.get('output_excerpt'),str) else None,
            artifact_sha256=payload.get('artifact_sha256'),
            duration_ms=payload['duration_ms'] if type(payload.get('duration_ms')) in (int,float) and payload['duration_ms']>=0 else None)
    elif kind in ('agent_started','agent_completed'):
        if kind=='agent_started':
            job['stage']=payload.get('stage') or ('authoring_paragraph' if job.get('kind')=='paragraph' else 'authoring_account')
            detail=activity('preparation','completed','harness')
        else:
            # A provider result ends authoring, not the job. Output still needs
            # durable capture and deterministic validation before acceptance.
            job['stage']=(('authoring_paragraph' if job.get('kind')=='paragraph' else 'authoring_account')
                          if payload.get('status')=='failed' else 'collecting_output')
            # This is a completion notice, not the start of a timed capture
            # operation. The job remains running in its collection stage.
            detail=activity('preparation','failed' if payload.get('status')=='failed' else 'completed','harness')
    elif kind in ('agent_message','message'):
        detail=activity('agent_message','started','harness',
            **({'message_delta':True} if payload.get('delta') is True else {}))
    elif kind=='warning':
        if message in job['warnings']: return None
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

def read_preparation_inputs(repository,job):
    """Read immutable inputs from one snapshot without the application write lock."""
    with repository.read_transaction() as tx:
        if job['kind']=='analysis':
            identity=job['research_request_id']
            records=tx.get_records((('request',identity),('request_binding',identity)))
            return records[('request',identity)]['data'],records[('request_binding',identity)]['data']
        owner=tx.get('job',job['id'])['owner']
        stored=owned(tx,'account',job['input_account_id'],owner)['data']
        # Paragraph assembly re-mints every identity in the account document, so it needs the exact accepted
        # document. The stored projection writes non-DAPPER CURIEs (HGNC.SYMBOL:, obo:) as full IRIs, which
        # re-keys their propositions, claims and the account itself. Accounts without the index keep the projection.
        reference=tx.get('object_document',digest([owner,job['input_account_id']]))
        exact=tx.get('scientific_document',digest([owner,reference['data']['sha256']])) if reference and reference['owner']==owner else None
        if exact and exact['owner']==owner:
            stored={**stored,'result':{**stored['result'],'document':exact['data']['document']}}
        return stored

def persist_dispatch_input(repository,job_id,token,snapshot,package=None):
    """Commit collected evidence and its exact dispatch checkpoint together."""
    with repository.transaction() as tx:
        pair=jobs.fenced(tx,job_id,token)
        if not pair: return False
        current,queue=pair; owner=current['owner_user_id']
        if package is not None:
            tx.put('evidence',job_id,owner,{'job_id':job_id,'package_sha256':snapshot['sha256'],'package':package})
        queue['dispatch_input']=snapshot
        if snapshot.get('workspace'): queue['workspace']=snapshot['workspace']
        tx.update_existing('queue',job_id,owner,queue)
        return True

def prepare_source_artifacts(package_path,job_id):
    """Verify immutable source files before entering the acceptance write fence."""
    package=decode(package_path.read_bytes()); records=[]; access={}
    files={file['id']:file for file in package['dapper_context'].get('files',[])}
    base=setting('NEXTAUTH_URL','http://localhost:3000').rstrip('/')+'/api/backend/v1/artifacts/'
    for source in package['source_artifacts'].values():
        file=files.get(source['dapper_file_id'])
        if not file: continue
        path=assert_artifact(package_path.parent/source['path'],package_path.parent)
        data=path.read_bytes(); require(sha256(data)==source['sha256'],'Source artifact changed before persistence')
        record = {'sha256':source['sha256'],'file':file,**retained_file(path,source['sha256']),'job_id':job_id}
        context = package.get('validation_context')
        if context:
            record['research_source'] = {**source, 'eligible_evidence': file['id'] in context['eligible_source_ids'],
                'cfde_evidence': file['id'] in context['cfde_source_ids']}
        records.append(record)
        access[file['id']]={'file':file,'download_url':base+source['sha256'],'expires_at':None,
                            'availability':'available','verification':'checksum_verified'}
    return records,access

def persist_source_artifacts(tx,owner,artifacts):
    """Batch new rows while preserving each prior put's duplicate/version semantics."""
    records=[(digest([owner,record['sha256']]),record) for record in artifacts]
    identities=list(dict.fromkeys(identity for identity,_ in records)); known=set()
    for offset in range(0,len(identities),100):
        known.update(tx.get_many('artifact',identities[offset:offset+100]))
    pending=[]
    def flush():
        if pending: tx.insert_many(pending); pending.clear()
    for identity,record in records:
        if identity in known:
            # A duplicate can refer to an unflushed new row. Flush before updating
            # so every occurrence increments its version and the last value wins.
            flush(); tx.update_existing('artifact',identity,owner,record)
        else:
            pending.append(('artifact',identity,owner,record)); known.add(identity)
            if len(pending)==100: flush()
    flush()

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
        for field in ('request','response','upstream_request','upstream_response'):
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

def collect_reference_package(**kwargs):
    """KPN-generation collector (reveal_backend.reference_evidence), imported on first use."""
    from .reference_evidence import collect_reference_package as collect_reference
    return collect_reference(**kwargs)

def anchor_model(anchors):
    """Reference model of frozen anchor bindings; legacy bindings carry no model field."""
    models={anchor.get('model') or LEGACY_MODEL for anchor in anchors}
    require(len(models)==1 and models <= set(MODELS),'Selected anchors must share one known reference model')
    return models.pop()

def check_user_inputs(package,frozen):
    """Recovery must use the same user text and immutable objects as submission."""
    expected=frozen.get('user_inputs')
    actual=package.get('user_inputs')
    if expected is None:
        require(actual is None,'Unexpected researcher inputs in a historical request')
        return
    require(isinstance(actual,dict),'Frozen researcher inputs are missing')
    for key in ('format','research_direction','context','hypotheses'):
        require(actual.get(key)==expected.get(key),'Frozen researcher text changed')
    require(len(actual.get('uploads',[]))==len(expected['uploads']),'Frozen researcher attachments changed')
    for captured,submitted in zip(actual.get('uploads',[]),expected['uploads']):
        if package.get('retrieval_mode') == 'progressive':
            require(captured.get('id') == submitted['id'] and captured.get('sha256') == submitted['sha256'], 'Frozen research attachment changed')
            continue
        require({key:captured.get(key) for key in submitted}==submitted,'Frozen researcher attachment changed')


def collect(job,frozen,binding,budgets,directory,repository=None):
    if frozen.get('retrieval_mode') == 'progressive':
        from .research_hosted import collect as collect_seed
        return collect_seed(repository or Repository(), job, directory)
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
        runtime=DapperRuntime(CURRENT_DAPPER_SNAPSHOT)
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
        sources=dict(gap_id=frozen['composer']['source_gap']['source_id'],factor_ids=[a['cfde_node_id'] for a in binding['anchors']],
            output=directory,dapper=runtime,project_root=ROOT,dismech_source=Path(setting('REVEAL_DISMECH_SOURCE',str(ROOT.parent/'dismech'))),
            dismech_index=ROOT/'data/dismech-gaps/2026-09-24',
            selected_graphs=frozen['composer']['selected_kgs'],max_accounts=budgets.get('max_accounts',3),selection_metadata=metadata,
            limit=requested_limit,max_nodes=budgets.get('max_nodes',250),max_edges=budgets.get('max_edges',1000),
            user_inputs=frozen.get('user_inputs'))
        model=anchor_model(binding['anchors'])
        if model==KPN_MODEL:
            # KPN generations capture the same evidence set from the reference tables in MySQL.
            built=collect_reference_package(**sources,generation_id=generation_of_anchors(binding['anchors']),connection_factory=mysql_connection)
        else:
            built=collect_package(**sources,model=model,geneset_import=ROOT/'data/cfde-genesets/2026-09-24',
                geneset_resolver=geneset_resolver(binding['anchors'][0]['gene_set_import_id']))

        package=built.package
    validate_package_shape(package,load_generated_schema(ROOT/'schema/evidence-package.schema.json'))
    gap=next(g for g in package['dapper_context']['knowledge_gaps'] if g['id']==frozen['question_id'])
    require(gap==binding['source_gap']['object'],'API-selected gap differs from collected source payload')
    require(package['dismech']['source_revision']['source_sha256']==frozen['composer']['source_gap']['source_revision'],'Collected DisMech revision differs from frozen request')
    require(set(package['selection']['eaggl_mechanism_ids'])=={a['cfde_node_id'] for a in binding['anchors']},'Collector changed native selected anchors')
    require(package['external_evidence']['selected_graphs']==frozen['composer']['selected_kgs'],'Collector changed selected graphs')
    check_user_inputs(package,frozen)
    return package_path,package

class Worker:
    def __init__(self,repository=None,adapter=None):
        self.repository=repository or Repository(); self.adapter=adapter; self.stopping=False
        self.worker_id=socket.gethostname()+':'+uid()
        self.draining=False; self.current_job=None
    async def process(self,job,queue):
        try:
            if job['kind']=='deployment_probe':
                from .deployment import run_probe
                return await run_probe(self,job,queue)
            await self._process(job,queue)
        finally:
            if s3_enabled():
                root=artifacts_root()/job['id']
                if root.resolve().is_relative_to(artifacts_root()): shutil.rmtree(root,ignore_errors=True)

    def save_workspace(self,job,token,root):
        if not s3_enabled(): return
        reference=artifact_store().snapshot(root)
        review_capture=None
        from .box_adapter import CAPTURE_MARKER
        for path in sorted(root.glob('attempt-*/output/'+CAPTURE_MARKER)):
            marker=decode(path.read_bytes())
            if marker.get('cleanup_complete') and marker.get('state',{}).get('status')=='succeeded':
                binding=marker['binding']
                if binding['job_id']==job['id'] and (review_capture is None or binding['attempt']>review_capture['attempt']):
                    review_capture={'attempt':binding['attempt'],'box_id':binding['box_id'],'capture_sha256':sha256(path.read_bytes())}
        with self.repository.transaction() as tx:
            pair=jobs.fenced(tx,job['id'],token)
            if not pair: raise StorageUnavailable('Checkpoint lease was lost')
            current,queue=pair
            queue['workspace']=reference
            if review_capture: queue['review_capture']=review_capture
            tx.put('queue',job['id'],current['owner_user_id'],queue)

    async def _process(self,job,queue):
        token=queue['token']; mode=queue.get('dispatch_input',{}).get('mode') or ('box' if queue.get('remote_handle') else setting('REVEAL_EXECUTION_MODE','box'))
        require(mode in ('box','deterministic'),'REVEAL_EXECUTION_MODE must be box or deterministic')
        require(mode!='deterministic' or setting('REVEAL_ENVIRONMENT','development') in ('development','test'),'Deterministic execution is restricted to development/test environments')
        root=artifacts_root()/job['id']; directory=root/('attempt-'+str(queue['attempt'])); directory.mkdir(parents=True,exist_ok=True)
        lost=False
        durable_capture_handle=None
        def cancellation_requested():
            with self.repository.read_transaction() as tx:
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
                await asyncio.sleep(min(20,jobs.lease_duration()/3))
                if not await asyncio.to_thread(jobs.heartbeat,self.repository,job['id'],token): lost=True; return
        def persist_checkpoint(handle):
            nonlocal durable_capture_handle
            if s3_enabled() and handle.get('phase') in ('captured','deleted'):
                self.save_workspace(job,token,root)
            with self.repository.transaction() as tx:
                pair=jobs.fenced(tx,job['id'],token)
                if not pair: raise RuntimeError('Attempt lease lost')
                current,q=pair; q['remote_handle']=handle
                tx.put('queue',job['id'],current['owner_user_id'],q)
            if s3_enabled() and handle.get('phase')=='deleted':
                durable_capture_handle=deepcopy(handle)
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
            if s3_enabled() and queue.get('workspace'):
                await asyncio.to_thread(artifact_store().restore,queue['workspace'],root)
                directory.mkdir(parents=True,exist_ok=True)
            if await cancelled() and not queue.get('remote_handle'):
                jobs.finish(self.repository,job['id'],token,'cancelled'); return
            if mode=='deterministic': await emit('warning',{'message':'DEVELOPMENT SIMULATION — not a live scientific result.'})
            inputs=await asyncio.to_thread(read_preparation_inputs,self.repository,job)
            if job['kind']=='analysis': frozen,binding=inputs
            else: stored=inputs; document=stored['result']['document']
            prepared_package=None
            snapshot=queue.get('dispatch_input')
            review_source=queue.get('review_source')
            if review_source:
                require(snapshot and mode=='box','Saved-output retry requires the original frozen Box input')
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
                if job['kind']=='analysis':
                    package=restored; selected=tuple(frozen['composer']['selected_kgs'])
                    check_user_inputs(package,frozen)
                else: paragraph_input=restored; selected=()
            elif job['kind']=='analysis':
                await emit('stage',{'stage':'preparing_evidence','message':'Collecting the frozen DisMech question and native CFDE mechanism evidence.'})
                input_path,package=await asyncio.to_thread(collect,job,frozen,binding,queue['inputs'].get('budgets',{}),root/'evidence',**({'repository': self.repository} if frozen.get('retrieval_mode') == 'progressive' else {}))
                input_path,package,measurement=await asyncio.to_thread(fit_input_budget,input_path,mode,queue['inputs'].get('budgets',{}).get('evidence_tokens',24000))
                (directory/'token-budget.json').write_bytes(canonical_json(measurement))
                prepared_package=package
                selected=tuple(frozen['composer']['selected_kgs'])
            else:
                metadata=stored['result']['citation_metadata']
                allowed=[{'target_id':m['target_id'],'citation_metadata_revision':m['metadata_revision']} for m in metadata]
                # Claims first makes deterministic paragraph cite an assessed target.
                allowed.sort(key=lambda x:0 if x['target_id'].startswith('dapper:Claim.') else 1)
                paragraph_input={'format':'reveal.paragraph-input/1','account_document':document,'account_id':job['input_account_id'],'allowed_citations':allowed}
                input_path=directory/'paragraph-input.json'; input_path.write_bytes(canonical_json(paragraph_input)); selected=()
            if snapshot is None:
                snapshot={'path':str(input_path.resolve().relative_to(root.resolve())),'sha256':sha256(input_path.read_bytes()),
                    'mode':mode,'model':setting('REVEAL_CLAUDE_MODEL','claude-sonnet-4-6'),'kind':job['kind']}
            if s3_enabled():
                snapshot=dict(snapshot,workspace=await asyncio.to_thread(artifact_store().snapshot,root))
            if not await asyncio.to_thread(persist_dispatch_input,self.repository,job['id'],token,snapshot,prepared_package): return
            if await cancelled() and not queue.get('remote_handle'):
                jobs.finish(self.repository,job['id'],token,'cancelled'); return
            execution_attempt=review_source['attempt'] if review_source else queue['attempt']
            research_access = None
            if job['kind'] == 'analysis' and package.get('retrieval_mode') == 'progressive' and not review_source:
                from .research_hosted import access
                research_access = await asyncio.to_thread(access, self.repository, job, queue['attempt'])
            request=ExecutionRequest(job_id=job['id'],attempt=execution_attempt,kind='research' if job['kind']=='analysis' else 'paragraph',input_path=input_path,
                output_dir=root/f'attempt-{execution_attempt}'/'output',selected_graphs=selected,timeout_seconds=int(setting('REVEAL_AGENT_TIMEOUT_SECONDS','1800')),
                max_budget_usd=agent_budget_usd('research' if job['kind']=='analysis' else 'paragraph'),max_turns=int(setting('REVEAL_AGENT_MAX_TURNS','100')),remote_handle=queue.get('remote_handle'),research_access=research_access)
            if review_source:
                phase='scientific_validation'
                from .review_retry import replay_capture
                await emit('stage',{'stage':'validating','message':'Validating saved research output before saving. The research agent will not run again.'})
                result=await asyncio.to_thread(replay_capture,request,review_source)
            else:
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
                if mode=='box': prewarm()
                result=await adapter.execute(request,emit,cancelled,checkpoint)
            # The Box deleted checkpoint already saved the exact final capture
            # and its cleanup marker. Other adapters/replays still need a save.
            if durable_capture_handle is None or result.remote_handle!=durable_capture_handle:
                await asyncio.to_thread(self.save_workspace,job,token,root)
            if await cancelled():
                jobs.finish(self.repository,job['id'],token,'cancelled'); return
            if result.status=='insufficient_evidence' and job['kind']=='analysis':
                phase='scientific_validation'
                from .analysis_outcomes import prepare
                await emit('stage',{'stage':'validating','message':'Checking the captured evidence and reasons no scientific account could be supported.'})
                prepared=await asyncio.to_thread(prepare,job,frozen,binding,input_path,result,
                    attempt=queue['attempt'],mode=mode,expected_model=snapshot['model'])
                if await self.begin_persistence(job,token,'Saving the scoped exploration and captured reasons; no scientific account was accepted.'):
                    await asyncio.to_thread(self.accept_outcome,job,token,prepared)
                return
            if result.status!='succeeded':
                from .job_failures import authoring_failure
                failure=authoring_failure(result,request) if result.status=='failed' else None
                jobs.finish(self.repository,job['id'],token,result.status,failure=failure); return
            phase='scientific_validation'
            await emit('stage',{'stage':'validating','message':'Validating scientific identities, source fidelity and provenance.'})
            if mode=='box': await asyncio.to_thread(validate_execution_ledger,result,request,snapshot['model'])
            if job['kind']=='analysis':
                from .research_hosted import captured_context
                validation_path, _, existing = await asyncio.to_thread(captured_context, self.repository, job, result, input_path, directory/'research-context')
                require(0<len(result.account_paths)+len(existing)<=queue['inputs'].get('budgets',{}).get('max_accounts',3),'Execution returned an invalid account count')
                accepted=[]
                for index,path in enumerate(result.account_paths):
                    raw=assert_artifact(path,request.output_dir); final_path=directory/f'accepted-{index+1}.json'
                    from .scientific_account_lint import AccountValidationError
                    try:
                        doc,report=await asyncio.to_thread(assemble_account,raw,validation_path,final_path,frozen['attribution'],job,execution_attempt,mode,
                            result.ledger_manifest_path if mode=='box' else None)
                    except AccountValidationError as exc:
                        (directory/f'validation-{index+1}.json').write_bytes(canonical_json(exc.report))
                        raise
                    (directory/f'validation-{index+1}.json').write_bytes(canonical_json(report))
                    accepted.append((doc,report,final_path))
                if await self.begin_persistence(job,token,'Saving validated scientific accounts and their source provenance.'):
                    await self.accept_accounts(job,token,accepted,frozen,validation_path,result,directory,mode)
            else:
                require(result.paragraph_path is not None,'Paragraph execution returned no segments')
                raw=assert_artifact(result.paragraph_path,request.output_dir)
                segments=decode(raw.read_bytes())
                await self.accept_paragraph(job,token,segments,paragraph_input,directory)
            if await cancelled(): jobs.finish(self.repository,job['id'],token,'cancelled')
        except Exception as exc:
            from .box_adapter import BoxTransportError
            if isinstance(exc,(BoxTransportError,StorageUnavailable)):
                with self.repository.transaction() as tx:
                    pair=jobs.fenced(tx,job['id'],token)
                    if pair and (pair[1].get('remote_handle') or isinstance(exc,StorageUnavailable)):
                        current,q=pair; q['recoveries']=q.get('recoveries',0)+1
                        from datetime import datetime,timedelta,timezone
                        q['lease_until']=(datetime.now(timezone.utc)+timedelta(seconds=min(60,5*q['recoveries']))).isoformat().replace('+00:00','Z')
                        tx.put('queue',job['id'],current['owner_user_id'],q)
                        jobs.event(tx,current,'warning','Durable storage is unavailable; the attempt will resume from its last checkpoint.' if isinstance(exc,StorageUnavailable) else
                            'Reconnecting to the existing remote execution for result capture or cleanup; no new paid attempt was launched.')
                        return
            # Scientific exceptions are retained as bounded local diagnostics;
            # API errors do not echo third-party headers/URLs/credentials.
            diagnostic={'phase':phase,'error_type':type(exc).__name__,'message':str(exc)[:3000] if not isinstance(exc,OSError) else 'Operating system error'}
            (directory/'failure.json').write_bytes(canonical_json(diagnostic))
            await asyncio.to_thread(self.save_workspace,job,token,root)
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

    async def begin_persistence(self,job,token,message):
        """Publish the saving boundary before persistence can block event reads."""
        def transition():
            with self.repository.transaction() as tx:
                pair=jobs.fenced(tx,job['id'],token)
                if not pair or pair[0]['status']=='cancel_requested': return False
                current,_=pair; current['stage']='persisting'
                jobs.event(tx,current,'activity',message,activity('preparation'))
                return True
        return await asyncio.to_thread(transition)

    def accept_outcome(self,job,token,prepared):
        from . import analysis_outcomes
        self.save_workspace(job,token,artifacts_root()/job['id'])
        with self.repository.transaction() as tx:
            pair=jobs.fenced(tx,job['id'],token)
            if not pair or pair[0]['status']=='cancel_requested': return False
            current,_=pair
            identity=analysis_outcomes.save(tx,current,prepared)
            current.update(status='insufficient_evidence',stage='complete',completed_at=now(),
                result=analysis_outcomes.result(identity,prepared),failure=None)
            jobs.event(tx,current,'result','Exploration saved with its scoped limitations; no scientific account was accepted.')
            return True

    async def accept_accounts(self,job,token,accepted,frozen,package_path,result,directory,mode):
        from .citations import register
        source_artifacts,artifact_access=await asyncio.to_thread(prepare_source_artifacts,package_path,job['id'])
        captured={}
        if mode=='box':
            from .acceptance import ledger_sources
            captured=await asyncio.to_thread(ledger_sources,result.ledger_manifest_path)
            for checksum,source in captured.items():
                source['retained']=await asyncio.to_thread(retained_file,source['path'],checksum)
        await asyncio.to_thread(self.save_workspace,job,token,artifacts_root()/job['id'])
        from .analysis_outcomes import creation_stamp, stamp_gap, stamped
        from .reference_generation import ACTIVE_KIND, ACTIVE_ID
        from .scientific_writes import AcceptanceWrites, acceptance_keys, dapper_nodes
        from .workflow_execution import drain_on_cancel
        base=setting('NEXTAUTH_URL','http://localhost:3000').rstrip('/')+'/api/backend/v1/artifacts/'
        def persist():
            # One fenced transaction in one worker thread: the event loop keeps serving while it holds the fence.
            evidence_sha256=sha256(package_path.read_bytes())
            package = decode(package_path.read_bytes())
            validation = package.get('validation_context', {})
            # Envelopes depend on the fence only through citation metadata: project every node now, with each
            # document's artifact access as of that document (box captures are added document by document).
            documents=[doc for doc,_,_ in accepted]; snapshots=[]; access=dict(artifact_access)
            for doc in documents:
                if mode=='box':
                    for file in doc.get('files',[]):
                        if captured.get(file.get('sha256')):
                            access[file['id']]={'file':file,'download_url':base+file['sha256'],'expires_at':None,'availability':'available','verification':'checksum_verified'}
                snapshots.append(dict(access))
            projections=[{identity:object_projection(doc,identity,snapshot) for identity in dict.fromkeys(
                [*(node['id'] for node in dapper_nodes(doc)),doc['scientific_accounts'][0]['id']])} for doc,snapshot in zip(documents,snapshots)]
            shas=[sha256(path.read_bytes()) for _,_,path in accepted]
            owner=job.get('owner_user_id')   # the fenced job row decides; a changed owner only costs the reads below
            keys=[('job',job['id']),('queue',job['id']),('execution',job['id']),(ACTIVE_KIND,ACTIVE_ID),('local_work',job['id']),
                ('research_pin',job.get('research_request_id')),*acceptance_keys(owner,documents),
                *(('scientific_document',digest([owner,sha])) for sha in shas)]
            for doc in documents:
                identity=doc['scientific_accounts'][0]['id']
                keys+=[('outbox',digest([owner,identity,'default-paragraph'])),('account',digest([owner,identity])),('account_membership',digest([owner,identity]))]
                if mode=='box': keys+=[('artifact',digest([owner,file['sha256']])) for file in doc.get('files',[]) if captured.get(file.get('sha256'))]
            with self.repository.transaction() as tx:
                tx.get_records([key for key in keys if isinstance(key[1],str)],bare=('object',))   # one read for the whole commit
                pair=jobs.fenced(tx,job['id'],token)
                if not pair or pair[0]['status']=='cancel_requested': return
                current,_=pair; owner=current['owner_user_id']; accounts=[]; paragraphs=[]; manifest_accounts=[]
                writes=AcceptanceWrites(tx,owner)
                reused = {'contexts': [], 'borrowed_ids': [], 'citation_metadata': []}
                existing = validation.get('existing_account_ids', [])
                if validation:
                    from .scientific_reuse import record_dependencies
                    reused = record_dependencies(tx, owner, frozen['id'], validation.get('reuse_receipt_ids', []),
                        [doc['scientific_accounts'][0]['id'] for doc, _, _ in accepted] + existing)
                    require(set(existing) <= {a['account_id'] for a in reused['existing_accounts']}, 'Reused result authority changed')
                retained_ids = {n['id'] for c in reused['contexts'] for rows in c['dapper_context'].values() if isinstance(rows, list)
                    for n in rows if isinstance(n, dict) and 'id' in n}
                borrowed_ids = set(reused['borrowed_ids'])
                persist_source_artifacts(tx,owner,source_artifacts)
                for (doc,report,path),projection,snapshot,document_sha in zip(accepted,projections,snapshots,shas):
                    if mode=='box':
                        for file in doc.get('files',[]):
                            source=captured.get(file.get('sha256'))
                            if source: writes.put('artifact',digest([owner,file['sha256']]),owner,{'sha256':file['sha256'],'file':file,**source['retained'],'job_id':job['id']})
                    runtime=decode(Path(result.runtime_manifest_path).read_bytes()) if result.runtime_manifest_path else {'model_id':None,'harness_version':None}
                    runtime['model_id']=runtime.get('model_id') or runtime.get('model')
                    actor=doc['scientific_accounts'][0].get('was_attributed_to',[None])[0]
                    metadata=register(tx,owner,doc,{**frozen['attribution'],'person_id':actor},now(),runtime=runtime,
                        retained_citation_metadata=reused['citation_metadata'], retained_object_ids=retained_ids, writes=writes)
                    account=doc['scientific_accounts'][0]; identity=account['id']; gap=next(g for g in doc['knowledge_gaps'] if g['id']==account['question'])
                    outbox_key=digest([owner,identity,'default-paragraph'])
                    # A second document of an account accepted earlier in this commit reads its rows as written.
                    if writes.planned('outbox',outbox_key): writes.flush()
                    previous=tx.get('outbox',outbox_key)
                    if previous:
                        paragraph=tx.get('job',previous['data']['job_id'])['data']
                        old_account=owned(tx,'account',identity,owner)['data']
                        state=old_account['result']['research_statement']
                    else:
                        paragraph,rows=jobs.new(owner,'paragraph',account_id=identity,inputs={'kind':'paragraph','account_id':identity})
                        writes.insert(rows); jobs.update_paragraph_state(tx,paragraph)
                        state={'status':'queued','job_id':paragraph['id'],'paragraph_id':None}
                    envelope=with_citations(projection[identity],metadata); envelope['research_statement']=state
                    summary={'account':account,'knowledge_gap':gap,'claim_count':len(account['component_claims']),'created_at':now(),'job_id':job['id'],'research_statement':state}
                    if not previous:
                        # A job that finishes after its reference generation was superseded is born archived.
                        stamp=creation_stamp(tx,owner,job['research_request_id'],gap=stamp_gap(frozen['composer'].get('source_gap'),frozen.get('question_id')),scientific_document=doc,
                            analysis={'job_id':job['id'],'request_id':job['research_request_id'],'evidence_package_sha256':evidence_sha256,'account_id':identity})
                        writes.put('account',digest([owner,identity]),owner,stamped('account',{'result':envelope,'summary':deepcopy(summary),**claim_structure_record(report)},stamp))
                        writes.put('account_membership',digest([owner,identity]),owner,stamped('account_membership',{'account_id':identity,'summary':summary},stamp))
                    writes.put('scientific_document',digest([owner,document_sha]),owner,{'sha256':document_sha,'document':doc,'job_id':job['id'],'observed_at':now(),
                        'citation_metadata':metadata,'artifact_access':snapshot})
                    writes.document(doc,document_sha,metadata,projection.__getitem__,borrowed_ids)
                    if not previous: writes.put('outbox',outbox_key,owner,{'account_id':identity,'job_id':paragraph['id'],'dispatched':True})
                    accounts.append(identity); paragraphs.append(paragraph['id'])
                    manifest_accounts.append({'path':str(path.resolve().relative_to(directory.resolve())),'sha256':document_sha,'account_id':identity,'lint_report_sha256':sha256(canonical_json(report))})
                writes.flush()
                public={'kind':'analysis','request_id':job['research_request_id'],'account_ids':accounts,'enrichment':enrichment_status(result,frozen['composer']['selected_kgs'],mode),
                    'paragraph_job_ids':paragraphs,'evidence_package_sha256':evidence_sha256}
                if validation: public.update(reused_account_ids=existing, seed_sha256=validation['seed_sha256'])
                current.update(status='succeeded',stage='complete',result=public,completed_at=now())
                jobs.event(tx,current,'result','Gap analysis complete.' if mode=='box' else 'Development simulation complete — not a scientific result.')
                manifest={'format':'reveal.agent-output/1','job_id':job['id'],'attempt':pair[1]['attempt'],'status':'succeeded','input_package_sha256':public['evidence_package_sha256'],
                    'runtime_manifest_sha256':sha256(Path(result.runtime_manifest_path).read_bytes()) if result.runtime_manifest_path else digest({'mode':mode}),
                    'ledger_manifest_sha256':sha256(Path(result.ledger_manifest_path).read_bytes()) if result.ledger_manifest_path else None,'accounts':manifest_accounts,'reason':None}
                # S3 mode already has the exact output checkpoint and RDS result.
                if not s3_enabled(): (directory/'worker-output.json').write_bytes(canonical_json(manifest))
        await drain_on_cancel(asyncio.to_thread(persist))

    async def accept_paragraph(self,job,token,raw,inputs,directory):
        from .box_paragraph import assemble_paragraph
        assembled=await asyncio.to_thread(assemble_paragraph,raw,inputs,dapper_root=release_root(),release_lock=LOCK)
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
        await asyncio.to_thread(self.save_workspace,job,token,artifacts_root()/job['id'])
        if not await self.begin_persistence(job,token,'Saving the validated research statement and its exact citations.'): return
        from .workflow_execution import drain_on_cancel
        def persist():
            # The envelope depends on the fence only through citation metadata: project it before taking the fence.
            projection=object_projection(document,paragraph['id']); document_sha=sha256(path.read_bytes())
            with self.repository.transaction() as tx:
                pair=jobs.fenced(tx,job['id'],token)
                if not pair or pair[0]['status']=='cancel_requested': return
                current,_=pair; owner=current['owner_user_id']; stored=owned(tx,'account',job['input_account_id'],owner)['data']
                # Exact registry revision is required; never substitute latest.
                for occurrence in paragraph['citations']: owned(tx,'citation',occurrence['target_id']+':'+str(occurrence['citation_metadata_revision']),owner)
                envelope=with_citations(projection,stored['result']['citation_metadata'])
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
        await drain_on_cancel(asyncio.to_thread(persist))

    async def run(self):
        from .job_transport import RedisTransport
        transport=RedisTransport() if jobs.transport()=='redis' else None
        def status():
            with self.repository.transaction() as tx:
                control=tx.get('worker_control',jobs.namespace())
                if control: self.draining=bool(control['data'].get('draining'))
                tx.put('runtime',self.worker_id,'system',{'namespace':jobs.namespace(),'heartbeat_at':now(),
                    'job_id':self.current_job,'draining':self.draining,'pid':os.getpid()})
        async def monitor():
            while not self.stopping:
                try: await asyncio.to_thread(status)
                except Exception as exc: log.error('Worker heartbeat unavailable (%s)',type(exc).__name__)
                await asyncio.sleep(3)
        monitor_task=asyncio.create_task(monitor())
        try:
            while not self.stopping:
                message=None
                try:
                    if self.draining:
                        await asyncio.sleep(1); continue
                    if transport:
                        message=await asyncio.to_thread(transport.receive,self.worker_id)
                        if not message: continue
                        identity,envelope=message
                        if await asyncio.to_thread(transport.disposition,self.repository,envelope)=='discard':
                            await asyncio.to_thread(transport.acknowledge,identity); continue
                        claimed=await asyncio.to_thread(jobs.claim,self.repository,self.worker_id,
                            job_id=envelope.get('job_id'),dispatch_id=envelope.get('dispatch_id'))
                    else:
                        claimed=await asyncio.to_thread(jobs.claim,self.repository,self.worker_id)
                    if claimed:
                        self.current_job=claimed[0]['id']
                        await asyncio.to_thread(status)
                        await self.process(*claimed)
                        self.current_job=None
                    else: await asyncio.sleep(1)
                    if message and await asyncio.to_thread(transport.disposition,self.repository,envelope)=='discard':
                        await asyncio.to_thread(transport.acknowledge,identity)
                except Exception as exc:
                    self.current_job=None
                    log.error('Worker loop unavailable (%s)',type(exc).__name__); await asyncio.sleep(3)
        finally:
            monitor_task.cancel()
            try: await monitor_task
            except asyncio.CancelledError: pass

def main():
    logging.basicConfig(level=logging.INFO)
    worker=Worker()
    def stop(*_): worker.stopping=True
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    asyncio.run(worker.run())

if __name__=='__main__': main()
