"""Explicit operator inspection/recovery of exhausted or ambiguous workflows.

This is a backend CLI requiring application DB and provider credentials, never a
browser API. Recovery preserves frozen evidence, authoring identity, paid-call
reservations, capture and cleanup obligations. It cannot reset a review budget.
"""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from . import jobs, workflow_state as state
from .repository import Repository, digest, now

# Why the previous attempt was retried or held: reset with the recovery budget when an operator resumes.
RETRY_MARKERS = ('retry_cause', 'infrastructure_since', 'scheduler_failure_at', 'scheduler_failure_status', 'sweep_hold')


def inspect_execution(repository, identity):
    with repository.read_transaction() as tx:
        row=tx.get('execution',identity)
        if not row or row['data']['namespace'] != jobs.namespace(): raise ValueError('Execution not found in this environment')
        value=row['data']; job=tx.get('job',identity)['data']
    keys=('job_id','namespace','generation','phase','phase_index','disposition','created_at','updated_at','expected_at',
          'capacity_reserved','creation_intent','launch_intent','capture_complete','cleanup_complete','cleanup_id',
          'cleanup_abandoned','failure_code','review_attempt','recoveries','delivery_recoveries','handoffs','diagnostic',
          'retry_cause','infrastructure_since','scheduler_failure_at','scheduler_failure_status','sweep_hold')
    return {**{key:value.get(key) for key in keys}, 'job_status':job['status'],
            'box_id':(value.get('box') or {}).get('box_id'), 'box_phase':(value.get('box') or {}).get('phase')}


def resume(repository, identity, expected_generation, *, recovered_box=None):
    """Fence the previous generation before scheduling a deliberate recovery."""
    with repository.transaction() as tx:
        row=tx.get('execution',identity)
        if not row or row['data']['namespace'] != jobs.namespace(): raise ValueError('Execution not found in this environment')
        execution=row['data']
        if execution['generation'] != expected_generation: raise ValueError('Execution generation changed; inspect before recovery')
        if execution.get('cleanup_abandoned'):
            raise ValueError('Box cleanup owns this abandoned execution; retry cleanup rather than resuming its phases')
        if execution['disposition'] not in ('recovery_required','retry'): raise ValueError('Execution is not awaiting recovery')
        if (execution.get('lease_until') or '') > now(): raise ValueError('An active step still owns this execution')
        previous={key:execution.get(key) for key in ('generation','phase','phase_index','disposition','diagnostic',
            'failure_code','recovery_required_at','recovery_generation','recovery_phase','recovery_phase_index',
            'recoveries',*RETRY_MARKERS)}
        if execution.get('creation_intent') and not execution.get('box'):
            if not recovered_box: raise ValueError('Ambiguous Box creation requires a verified existing Box identity; automated creation is forbidden')
            execution['box']={'box_id':recovered_box,'job_id':identity,'attempt':execution['authoring_attempt'],
                'cursor':0,'phase':'created','created_at':datetime.fromisoformat(execution['created_at'].replace('Z','+00:00')).timestamp(),'timings':{}}
            execution['phase']='bootstrap'
        elif recovered_box:
            if execution.get('box',{}).get('box_id') != recovered_box: raise ValueError('Cannot replace an assigned Box')
        job=tx.get('job',identity)['data']
        cleanup_only=bool(recovered_box and execution.get('recovery_required_at') and job['status'] in jobs.TERMINAL)
        if cleanup_only:
            execution.pop('recovery_required_at')
            state.require_recovery(tx,row['owner'],execution,'Operator resolved ambiguous allocation',
                failure_code=execution.get('failure_code','WORKFLOW_ALLOCATION_AMBIGUOUS'))
            generation=execution['generation']
        else:
            from .workflow_routes import steps_per_run
            generation=execution['generation']+1
            # A fresh budget forgets why the last attempt was retried (kept in the audit's previous_execution):
            # a stale cause would make the next delivery stall spend the new budget as a step failure.
            execution.update(generation=generation,run_id=None,recoveries=0,disposition='ready',diagnostic=None,
                fence=None,lease_until=None,step=None,updated_at=now(),expected_at=state.after(180),
                **dict.fromkeys(RETRY_MARKERS))
            tx.put('execution',identity,row['owner'],execution)
            queue=tx.get('queue',identity)['data']; queue.update(token=None,lease_until=None,remote_handle=execution.get('box'))
            tx.put('queue',identity,row['owner'],queue)
            tx.put('workflow_dispatch',identity,row['owner'],{'job_id':identity,'namespace':execution['namespace'],
                'generation':generation,'index':execution['phase_index'],'steps_per_run':steps_per_run(),
                'dispatch_id':digest([identity,generation]),
                'run_id':'reveal-'+digest([identity,generation])[:40],'attempts':0,'published_at':None,
                'created_at':now(),'next_attempt_at':now()})
        tx.put('workflow_recovery_audit',digest([identity,generation]),row['owner'],{
            'job_id':identity,'previous_generation':expected_generation,'generation':generation,
            'recovered_box_id':recovered_box,'cleanup_only':cleanup_only,'previous_execution':previous,'operator_recovery_at':now()})
    return inspect_execution(repository,identity)


async def verify_box(identity, attempt, box_id):
    from upstash_box import AsyncBox
    box=await AsyncBox.get(box_id,api_key=os.environ['UPSTASH_BOX_API_KEY'])
    try:
        expected={'reveal','job-'+hashlib.sha256(identity.encode()).hexdigest()[:16],'attempt-'+str(attempt)}
        if not expected.issubset(set(await box.labels.list())): raise ValueError('Box labels do not match this authoring attempt')
    finally: await box.aclose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('inspect','resume'))
    parser.add_argument('--job-id',required=True)
    parser.add_argument('--expected-generation',type=int)
    parser.add_argument('--box-id',help='Existing provider Box selected after ambiguous allocation investigation; never creates one')
    args=parser.parse_args()
    from .runtime_config import ROOT
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    repository=Repository()
    if args.action=='inspect': print(json.dumps(inspect_execution(repository,args.job_id),indent=2)); return
    if args.expected_generation is None: parser.error('resume requires --expected-generation')
    if args.box_id:
        with repository.read_transaction() as tx: attempt=tx.get('execution',args.job_id)['data']['authoring_attempt']
        asyncio.run(verify_box(args.job_id,attempt,args.box_id))
    result=resume(repository,args.job_id,args.expected_generation,recovered_box=args.box_id)
    from .workflow_routes import dispatch_job, dispatch_cleanup
    result['dispatch']=asyncio.run(dispatch_cleanup(repository,result['cleanup_id']) if result.get('cleanup_abandoned')
                                   else dispatch_job(repository,args.job_id))
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
