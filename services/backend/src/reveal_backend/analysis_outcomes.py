"""Durable scoped explorations, separate from accepted scientific accounts.

An outcome reports why a particular captured investigation stopped. It is not a
validated scientific claim or evidence that a relationship is globally absent.
"""
from copy import deepcopy
from pathlib import Path
import re
from .auth import Problem, owned
from .repository import digest, now, uid
from .evidence_package import canonical_json, decode, require, sha256
from .runtime_config import setting
from .artifact_store import retained_file

SCOPE_NOTE = ('This is a report of the selected investigation and captured evidence, not a ScientificAccount or a globally established absence of a relationship. Unavailable or unqueried sources do not establish a negative finding.')
READ_TOOLS = {'query_graph', 'describe_kg', 'get_schema', 'list_types', 'list_predicates', 'search_papers', 'read_paper'}


def anchor_display(composer,binding,draft_binding):
    """Copy display text only from the exact saved source/import selection."""
    anchors={item['cfde_node_id']:item for item in binding['anchors'] if item.get('cfde_node_id')}; displays={}
    for selected in composer['eaggl_anchors']:
        reference=selected['reference']; native=reference['source_id']
        if not all(reference.get(key) for key in ('source_revision','dapper_id')): continue
        saved=draft_binding.get('selections',{}).get(native,{})
        record=saved.get('record',{}); display=record.get('cfde_anchor',{})
        if (saved.get('reference')!=reference or saved.get('binding')!=anchors.get(native)
                or record.get('source_id')!=native or record.get('source_revision')!=reference['source_revision']
                or record.get('object',{}).get('id')!=reference['dapper_id'] or display.get('node_id')!=native):
            continue
        label=display.get('label'); subtitle=display.get('subtitle')
        if not isinstance(label,str) or not label.strip(): continue
        displays[native]={'reference':deepcopy(reference),'label':label,
            'subtitle':subtitle if isinstance(subtitle,str) and subtitle.strip() else None}
    return displays


def pointer(value, path):
    require(isinstance(path,str) and (path == '' or path.startswith('/')), 'Invalid outcome evidence pointer')
    if path == '': return value
    for encoded in path[1:].split('/'):
        require(re.search(r'~(?![01])',encoded) is None,'Invalid outcome evidence pointer escape')
        key=encoded.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            require(key=='0' or (key.isdecimal() and not key.startswith('0')), 'Invalid evidence array index')
            index=int(key); require(index<len(value),'Outcome evidence pointer is absent'); value=value[index]
        else:
            require(isinstance(value,dict) and key in value,'Outcome evidence pointer is absent'); value=value[key]
    return value


def prepare(job,frozen,binding,package_path,result,*,attempt,mode,expected_model,capture_sha256=None):
    """Validate captured bytes and create an immutable, explicitly scoped report."""
    from .agent_execution import ExecutionRequest
    from .worker import assert_artifact, validate_execution_ledger, prepare_source_artifacts
    from .research_outcome import validate_insufficient_outcome
    package_path=Path(package_path); package_bytes=package_path.read_bytes(); package=decode(package_bytes)
    require(job['kind']=='analysis' and result.status=='insufficient_evidence','Only completed scoped investigations can become analysis outcomes')
    request=ExecutionRequest(job_id=job['id'],attempt=attempt,kind='research',input_path=package_path,
        output_dir=Path(result.output_dir),selected_graphs=tuple(frozen['composer']['selected_kgs']))
    require(result.outcome_path is not None,'Insufficient evidence result lacks its captured outcome artifact')
    path=assert_artifact(result.outcome_path,request.output_dir)
    if mode=='box':
        from .box_adapter import read_capture_marker, CAPTURE_MARKER
        require(result.remote_handle is not None,'Captured outcome lacks its trusted execution binding')
        marker=read_capture_marker(request,result.remote_handle)
        require(marker and marker['state']['status']=='insufficient_evidence' and
            (marker['cleanup_complete'] or capture_sha256 and sha256((request.output_dir/CAPTURE_MARKER).read_bytes())==capture_sha256),
            'Outcome capture is not a completed insufficient-evidence execution')
        relative=str(path.relative_to(request.output_dir.resolve()))
        require(relative in marker['files'],'Outcome was not included in the trusted result capture')
        validate_execution_ledger(result,request,expected_model)
    outcome_bytes=path.read_bytes(); raw=decode(outcome_bytes); outcome=validate_insufficient_outcome(raw)
    gap_id=frozen['question_id']
    require(raw.get('knowledge_gap_id',gap_id)==gap_id,'Outcome reports another knowledge gap')
    require(package['selection']['knowledge_gap_id']==gap_id,'Captured evidence belongs to another gap')
    gap=next((g for g in package['dapper_context']['knowledge_gaps'] if g['id']==gap_id),None)
    require(gap is not None and gap==binding['source_gap']['object'],'Captured gap differs from the frozen source observation')
    require(package['dismech']['source_revision']['source_sha256']==frozen['composer']['source_gap']['source_revision'],'Outcome source revision differs from selected source')
    require(set(package['selection']['eaggl_mechanism_ids'])=={a['cfde_node_id'] for a in binding['anchors']},'Outcome changed the selected native anchors')
    require(package['external_evidence']['selected_graphs']==frozen['composer']['selected_kgs'],'Outcome changed selected external graphs')
    runtime=decode(Path(result.runtime_manifest_path).read_bytes()) if result.runtime_manifest_path else {}
    ledger=decode(Path(result.ledger_manifest_path).read_bytes()) if result.ledger_manifest_path else {'calls':[]}
    anchors=[]; versions=[]
    frozen_mechanisms={node['id']:node for node in frozen.get('document',{}).get('mechanisms',[])}
    bound={a['cfde_node_id']:a for a in binding['anchors']}
    for selected in frozen['composer']['eaggl_anchors']:
        reference=selected['reference']; item=bound[reference['source_id']]
        mechanism=package['pigean']['mechanisms'][item['cfde_node_id']]
        display=binding.get('anchor_display',{}).get(reference['source_id'],{})
        if display.get('reference')!=reference: display={}
        trait=re.sub(r'\s*\(Factor\d+\)\s*$','',display.get('subtitle') or '',flags=re.IGNORECASE).strip()
        anchors.append({'source_id':reference['source_id'],'mechanism_id':reference['dapper_id'],
            'name':display.get('label') or frozen_mechanisms.get(reference['dapper_id'],{}).get('name') or item.get('object',{}).get('name') or mechanism['display_name'],
            'trait':trait or mechanism.get('fit',{}).get('phenotype'),'origin':selected['origin']})
        versions.append({'source_id':reference['source_id'],'source_revision':reference['source_revision'],
            'embedding_run_id':item.get('embedding_run_id'),'mapping_run_id':item.get('mapping_run_id')})
    source_artifacts,access=prepare_source_artifacts(package_path,job['id'])
    records={item['sha256']:item for item in source_artifacts}; references=[]
    calls={call['sequence']:call for call in ledger['calls']}
    for ref in outcome.get('evidence_refs',[]):
        if ref['source']=='package':
            require(ref.get('ledger_sequence') is None,'Package evidence cannot name a tool sequence')
            pointer(package,ref['pointer']); checksum=sha256(package_bytes)
        else:
            call=calls.get(ref.get('ledger_sequence'))
            require(call and call['tool'] in READ_TOOLS and call['status'] in ('completed','empty'),'Outcome evidence must reference a completed trusted read')
            artifact=call['response']; target=assert_artifact(Path(result.ledger_manifest_path).parent/artifact['path'],Path(result.ledger_manifest_path).parent)
            data=target.read_bytes(); require(sha256(data)==artifact['sha256'] and len(data)==artifact['size_bytes'],'Outcome tool source changed')
            pointer(decode(data),ref['pointer']); checksum=artifact['sha256']
        url=None
        if ref['source']=='tool_response':
            records[checksum]={'sha256':checksum,**retained_file(target,checksum),'job_id':job['id'],
                'file':{'filename':'tool-response-'+str(call['sequence'])+'.json','mime_type':'application/json'}}
            url=setting('NEXTAUTH_URL','http://localhost:3000').rstrip('/')+'/api/backend/v1/artifacts/'+checksum
        references.append({**ref,'ledger_sequence':ref.get('ledger_sequence'),'artifact_sha256':checksum,'download_url':url})
    fields=('user_id','person_id','principal_kind','display_name','orcid','orcid_authenticated','observed_at')
    attribution=frozen.get('attribution')
    attribution={key:deepcopy(attribution[key]) for key in fields} if attribution and all(key in attribution for key in fields) else None
    record={'outcome':'insufficient_evidence','summary':outcome.get('summary') or 'The available evidence did not support a scientific account for the selected question.','reason':outcome['reason'],
        'explored_topics':outcome.get('explored_topics',[]),'missing_evidence':outcome.get('missing_evidence',[]),
        'limitations':outcome.get('limitations',[]),'next_steps':outcome.get('next_steps',[]),
        'knowledge_gap':gap,'source_gap':deepcopy(frozen['composer']['source_gap']),'anchors':anchors,
        'selected_kgs':list(frozen['composer']['selected_kgs']),
        'created_at':job.get('completed_at') or runtime.get('completed_at') or now(),'attribution':attribution,
        'scope_note':SCOPE_NOTE,'record_format':'structured' if raw.get('format') else 'legacy','job_id':job['id'],
        'provenance':{'evidence_package_sha256':sha256(package_bytes),'outcome_sha256':sha256(outcome_bytes),
            'runtime_sha256':sha256(Path(result.runtime_manifest_path).read_bytes()) if result.runtime_manifest_path else None,
            'ledger_sha256':sha256(Path(result.ledger_manifest_path).read_bytes()) if result.ledger_manifest_path else None,
            'execution_mode':mode,'source_bindings':versions,'coverage':deepcopy(package['coverage']),
            'evidence_refs':references,'source_artifacts':list(access.values()),
            'graph_queries':[{'sequence':c['sequence'],'graph':c['selected_graph'],'status':c['status'],
                'request_sha256':c['request']['sha256'],'response_sha256':c['response']['sha256']}
                for c in ledger['calls'] if c['tool']=='query_graph']}}
    return {'record':record,'artifacts':records}


def save(tx,job,prepared):
    """Called under the job fence, or an explicit terminal-outcome recovery lock."""
    owner=job['owner_user_id']; previous=tx.get('analysis_outcome_by_job',job['id'])
    if previous:
        row=owned(tx,'analysis_outcome',previous['data']['id'],owner)
        require(row['data']['record']['provenance']==prepared['record']['provenance'],'A saved exploration cannot be replaced by different captured provenance')
        return row['data']['record']['id']
    from .worker import persist_source_artifacts
    persist_source_artifacts(tx,owner,list(prepared['artifacts'].values()))
    identity=uid(); record={**deepcopy(prepared['record']),'id':identity}
    tx.put('analysis_outcome',identity,owner,{'record':record,'artifacts':prepared['artifacts']})
    tx.put('outcome_summary',identity,owner,summary(record))
    tx.put('analysis_outcome_by_job',job['id'],owner,{'id':identity})
    return identity


def result(identity,prepared):
    return {'kind':'analysis_outcome','outcome_id':identity,'evidence_package_sha256':prepared['record']['provenance']['evidence_package_sha256']}


def summary(record):
    return {key:deepcopy(record[key]) for key in ('id','outcome','summary','knowledge_gap','anchors','created_at','attribution')}


def publication_state(row,can_manage=False):
    data=row['data'] if row else {}
    return {'visibility':data.get('visibility','private'),'version':data.get('version',0),'published_at':data.get('published_at'),
        'updated_at':data.get('updated_at'),'can_manage':can_manage,'has_unpublished_changes':False}


def state(tx,identity,*,can_manage=False):
    return publication_state(tx.get('outcome_publication',identity),can_manage)


def published(tx,identity=None):
    rows=[tx.get('outcome_publication',identity)] if identity else tx.list('outcome_publication')
    for row in rows:
        if not row or row['data'].get('visibility')!='public': continue
        snapshot=tx.get('outcome_snapshot',row['data']['snapshot_id'])
        if snapshot and snapshot['owner']==row['owner']: yield row,snapshot['data']


def get(tx,identity,owner=None):
    row=tx.get('analysis_outcome',identity) if owner else None
    if row and row['owner']==owner: record=deepcopy(row['data']['record']); manage=True
    else:
        match=next(published(tx,identity),None)
        if match is None: raise Problem(404,'NOT_FOUND','The explored analysis is unavailable.')
        record=deepcopy(match[1]['record']); manage=False
    record['publication']=state(tx,identity,can_manage=manage)
    return record


def listing(tx,gap_id=None,owner=None,scope='public'):
    if scope=='public':
        records=[]
        for row in tx.list('outcome_publication'):
            metadata=row['data']; item=metadata.get('summary')
            if metadata.get('visibility')=='public' and item and (gap_id is None or item['knowledge_gap']['id']==gap_id):
                records.append({**deepcopy(item),'publication':publication_state(row)})
    else:
        if owner is None: raise Problem(401,'SESSION_EXPIRED','A workspace session is required.')
        rows=[row for row in tx.list('outcome_summary',owner) if gap_id is None or row['data']['knowledge_gap']['id']==gap_id]
        publications=tx.get_many('outcome_publication',[row['id'] for row in rows])
        records=[{**deepcopy(row['data']),'publication':publication_state(publications.get(row['id']),True)} for row in rows]
    return sorted(records,key=lambda row:(row['created_at'],row['id']),reverse=True)


def change(tx,identity,owner,visibility,expected_version):
    row=owned(tx,'analysis_outcome',identity,owner)
    current=state(tx,identity,can_manage=True)
    if current['version']!=expected_version: raise Problem(409,'PUBLICATION_VERSION_CONFLICT','Publication changed; refresh before choosing again.',current_version=current['version'])
    stamp=now(); metadata={'visibility':visibility,'version':current['version']+1,'published_at':None,'updated_at':stamp,'snapshot_id':None,'summary':None,'artifact_sha256':[]}
    if visibility=='public':
        snapshot=deepcopy(row['data']); snapshot['record']['job_id']=None
        snapshot_id=uid(); tx.put('outcome_snapshot',snapshot_id,owner,snapshot)
        metadata.update(snapshot_id=snapshot_id,published_at=current['published_at'] or stamp,summary=summary(snapshot['record']),artifact_sha256=sorted(snapshot['artifacts']))
    tx.put('outcome_publication',identity,owner,metadata)
    return state(tx,identity,can_manage=True)


def published_artifact(tx,checksum):
    for row in tx.list('outcome_publication'):
        if row['data'].get('visibility')=='public' and checksum in row['data'].get('artifact_sha256',[]):
            snapshot=tx.get('outcome_snapshot',row['data']['snapshot_id'])
            if snapshot and snapshot['owner']==row['owner']: return snapshot['data']['artifacts'][checksum]
    raise Problem(404,'NOT_FOUND','The source artifact is unavailable.')
