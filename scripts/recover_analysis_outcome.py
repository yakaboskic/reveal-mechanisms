#!/usr/bin/env python3
"""Explicit, idempotent recovery of a saved insufficient-evidence outcome.

Uses only existing trusted local captures and frozen database records. No agent,
provider, token count or source retrieval calls. Dry-run unless --apply is given.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/backend/src'))
from reveal_backend import analysis_outcomes, jobs
from reveal_backend.agent_execution import ExecutionRequest
from reveal_backend.box_adapter import CAPTURE_MARKER, read_capture_marker, captured_result
from reveal_backend.evidence_package import decode, require
from reveal_backend.repository import Repository, digest
from reveal_backend.runtime_config import artifacts_root
from reveal_backend.worker import restore_dispatch_input


def recover(repository,job_id,*,apply=False,root=None):
    with repository.read_transaction() as tx:
        row=tx.get('job',job_id); require(row is not None,'Job not found')
        job=row['data']; job['owner_user_id']=row['owner']
        require(job['kind']=='analysis' and job['status']=='insufficient_evidence','Only terminal insufficient-evidence analyses can be recovered')
        keys=(('request',job['research_request_id']),('request_binding',job['research_request_id']),('queue',job_id))
        saved=tx.get_records(keys)
        require(all(key in saved and saved[key]['owner']==row['owner'] for key in keys),'Frozen job inputs are not owned consistently')
        frozen=saved[keys[0]]['data']; binding=deepcopy(saved[keys[1]]['data']); queue=saved[keys[2]]['data']
        draft=tx.get('draft_binding',frozen['source_draft_id']) if frozen.get('source_draft_id') else None
        if draft and draft['owner']==row['owner']:
            recovered=analysis_outcomes.anchor_display(frozen['composer'],binding,draft['data'])
            binding['anchor_display']={**recovered,**binding.get('anchor_display',{})}
    snapshot=queue['dispatch_input']; require(snapshot['kind']=='analysis' and snapshot['mode']=='box','Historical import requires a captured live analysis')
    directory=(root or artifacts_root())/job_id
    package_path,_=restore_dispatch_input(directory,snapshot)
    output=directory/('attempt-'+str(queue['attempt']))/'output'
    marker=decode((output/CAPTURE_MARKER).read_bytes())
    handle={'box_id':marker['binding']['box_id']}
    request=ExecutionRequest(job_id=job_id,attempt=queue['attempt'],kind='research',input_path=package_path,
        output_dir=output,selected_graphs=tuple(frozen['composer']['selected_kgs']))
    marker=read_capture_marker(request,handle); require(marker is not None,'Trusted capture marker missing')
    result=captured_result(request,handle,marker)
    prepared=analysis_outcomes.prepare(job,frozen,binding,package_path,result,attempt=queue['attempt'],mode='box',expected_model=snapshot['model'])
    identity=None
    if apply:
        with repository.transaction() as tx:
            current=tx.get('job',job_id); require(current and current['data']['status']=='insufficient_evidence','Job changed during recovery')
            # A transfer can change current ownership, but cannot rewrite the
            # exact saved inquiry or selected package underneath this import.
            require(tx.get('queue',job_id)['data']['dispatch_input']==snapshot,'Frozen dispatch changed during recovery')
            require(digest(tx.get('request',job['research_request_id'])['data'])==digest(frozen),'Frozen request changed during recovery')
            active=current['data']; active['owner_user_id']=current['owner']
            identity=analysis_outcomes.save(tx,active,prepared)
            expected=analysis_outcomes.result(identity,prepared)
            if active.get('result')!=expected:
                active.update(result=expected,stage='complete')
                jobs.event(tx,active,'result','Recovered the saved scoped exploration; no new research was run.')
    return {'applied':apply,'outcome_id':identity,'evidence_package_sha256':prepared['record']['provenance']['evidence_package_sha256'],
        'outcome_sha256':prepared['record']['provenance']['outcome_sha256'],'record_format':prepared['record']['record_format'],
        'anchors':[{'name':a['name'],'trait':a['trait']} for a in prepared['record']['anchors']],
        'source_artifact_count':len(prepared['artifacts']),'created_at':prepared['record']['created_at']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id',required=True); parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    print(json.dumps(recover(Repository(),args.job_id,apply=args.apply),indent=2))

if __name__=='__main__': main()
