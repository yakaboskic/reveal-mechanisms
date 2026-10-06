"""Transactional queue, ordered event log, leases, cancellation and acceptance."""
from datetime import datetime, timezone, timedelta
import os
from .repository import now, uid
from .auth import Problem, owned

TERMINAL = {'succeeded', 'failed', 'cancelled', 'insufficient_evidence'}

def transport():
    value = os.getenv('REVEAL_JOB_TRANSPORT', 'database')
    if value not in ('database', 'redis', 'workflow'): raise ValueError('Invalid job transport')
    return value

def namespace():
    import re
    value = os.getenv('REVEAL_JOB_NAMESPACE', 'reveal')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', value): raise ValueError('Invalid job namespace')
    return value

def lease_duration():
    value = int(os.getenv('REVEAL_JOB_LEASE_SECONDS', '90'))
    if not 6 <= value <= 900: raise ValueError('Invalid lease duration')
    return value

def dispatch(tx, job, queue):
    """The durable outbox shares the job's transaction; Redis is never called here."""
    queue.update(transport=transport(), namespace=namespace(), dispatch_id=uid())
    if queue['transport'] == 'workflow':
        from .workflow_state import create
        create(tx, job, queue)
    if queue['transport'] == 'redis':
        tx.put('dispatch', job['id'], job['owner_user_id'], {
            'job_id': job['id'], 'namespace': queue['namespace'], 'dispatch_id': queue['dispatch_id'],
            'schema': 1, 'message_id': None, 'published_at': None})
    tx.put('queue', job['id'], job['owner_user_id'], queue)

def update_paragraph_state(tx,job):
    if job['kind']!='paragraph': return
    from .repository import digest
    owner=job['owner_user_id']; key=digest([owner,job['input_account_id']]); account=tx.get('account',key)
    if not account: return
    result=job.get('result') or {}
    state={'status':job['status'],'job_id':job['id'],'paragraph_id':result.get('paragraph_id')}
    account['data']['result']['research_statement']=state; account['data']['summary']['research_statement']=state
    tx.put('account',key,owner,account['data'])
    member=tx.get('account_membership',key)
    if member:
        member['data']['summary']['research_statement']=state; tx.put('account_membership',key,owner,member['data'])

def event_record(job, event_type, message, detail=None):
    job['last_event_id'] = str(int(job['last_event_id']) + 1)
    item = {'id': job['last_event_id'], 'job_id': job['id'], 'occurred_at': now(), 'event_type': event_type,
        'status': job['status'], 'stage': job['stage'], 'message': message, 'result': job['result'], 'detail': detail}
    job['updated_at'] = now()
    return item

def event(tx, job, event_type, message, detail=None):
    item = event_record(job, event_type, message, detail)
    tx.put('event', job['id']+':'+item['id'].zfill(12), job['owner_user_id'], item)
    tx.put('job', job['id'], job['owner_user_id'], job)
    if job['status'] in TERMINAL:
        from .research_hosted import release
        release(tx, job)
    return item

def enqueue(tx, owner, kind, request_id=None, account_id=None, inputs=None):
    identity = uid(); timestamp = now()
    job = {'id': identity, 'kind': kind, 'owner_user_id': owner, 'status': 'queued', 'stage': 'queued',
        'research_request_id': request_id, 'input_account_id': account_id, 'created_at': timestamp, 'updated_at': timestamp,
        'completed_at': None, 'result': None, 'failure': None, 'warnings': [], 'last_event_id': '0',
        'links': {'self': '/v1/jobs/'+identity, 'events': '/v1/jobs/'+identity+'/events', 'cancel': '/v1/jobs/'+identity+'/cancel'}}
    dispatch(tx, job, {'attempt': 0, 'lease_until': None, 'token': None, 'remote_handle': None, 'inputs': inputs or {}})
    update_paragraph_state(tx,job)
    event(tx, job, 'status', 'Queued for evidence preparation.' if kind == 'analysis' else 'Research statement queued.')
    return job

def cancel(tx, job):
    if job['status'] in TERMINAL: return job
    if job['status']=='cancel_requested': return job
    queue=tx.get('queue',job['id'])
    execution=tx.get('execution',job['id']) if queue and queue['data'].get('transport')=='workflow' else None
    if execution:
        state=execution['data']; state['cancel_requested']=True
        tx.put('execution',job['id'],job['owner_user_id'],state)
        tx.put('workflow_control',job['id'],job['owner_user_id'],{
            'job_id':job['id'],'generation':state['generation'],'namespace':state['namespace'],
            'published_at':None,'created_at':now()})
    active=queue and (queue['data'].get('token') or queue['data'].get('remote_handle') or
                     execution and execution['data'].get('capacity_reserved'))
    job['status'] = 'cancel_requested' if active else 'cancelled'
    job['completed_at'] = None if active else now()
    update_paragraph_state(tx,job)
    event(tx, job, 'status', 'Stopping the current execution.' if active else 'Stopped by the workspace owner.')
    return job

def claim(repository, worker_id, lease_seconds=None, *, job_id=None, dispatch_id=None):
    if transport() == 'workflow': return None  # Legacy consumers never acquire workflow jobs.
    lease_seconds = lease_seconds or lease_duration()
    with repository.transaction() as tx:
        control=tx.get('worker_control',namespace())
        if control and control['data'].get('draining'): return None
        if job_id:
            row = tx.get('job', job_id)
            candidates = [row] if row else []
        else:
            candidates = list(reversed(tx.list('job')))
        maximum = int(os.getenv('REVEAL_MAX_RUNNING_JOBS', '2' if transport() == 'redis' else '0'))
        if maximum < 0: raise ValueError('Invalid running job limit')
        if maximum:
            running = [row for row in tx.list('queue') if row['data'].get('lease_until')
                       and row['data']['lease_until'] > now() and row['data'].get('namespace', 'reveal') == namespace()]
            states = tx.get_many('job', [row['id'] for row in running])
            if sum(states.get(row['id'], {}).get('data', {}).get('status') not in TERMINAL for row in running) >= maximum:
                return None
        for row in candidates:
            job = row['data']; job['owner_user_id'] = row['owner']
            if job['status'] in TERMINAL: continue
            queue_row = tx.get('queue', job['id']); queue = queue_row['data']
            if queue.get('transport', 'database') != transport() or queue.get('namespace', 'reveal') != namespace(): continue
            if dispatch_id and queue.get('dispatch_id') != dispatch_id: continue
            if queue.get('held'): continue
            if queue['lease_until'] and queue['lease_until'] > now(): continue
            new_attempt = queue.pop('new_attempt', False)
            if not queue.get('remote_handle') and (not queue.get('workspace') or new_attempt): queue['attempt'] += 1
            queue['token'] = uid(); queue['worker_id'] = worker_id
            queue['lease_until'] = (datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat().replace('+00:00','Z')
            tx.put('queue', job['id'], row['owner'], queue)
            attempt_id = f"{job['id']}:{queue['attempt']}"
            previous_attempt = tx.get('attempt', attempt_id)
            started_at = previous_attempt['data'].get('started_at', now()) if previous_attempt else now()
            tx.put('attempt', attempt_id, row['owner'], dict(queue, started_at=started_at))
            if job['status']!='cancel_requested': job['status'] = 'running'
            update_paragraph_state(tx,job)
            event(tx, job, 'status', 'Reviewing the saved output; research execution is already complete.' if queue.get('review_source') else
                  'Worker resumed the persisted attempt.' if queue['remote_handle'] else 'Preparing the selected evidence.')
            return job, queue
    return None

def fenced(tx, job_id, token):
    row = tx.get('job', job_id); queue = tx.get('queue', job_id)
    if not row or not queue or queue['data']['token'] != token or queue['data']['lease_until'] <= now() or row['data']['status'] in TERMINAL:
        return None
    if queue['data'].get('transport') == 'workflow':
        execution=tx.get('execution',job_id)
        if not execution or execution['data'].get('fence') != token or execution['data'].get('lease_until','') <= now(): return None
    job = row['data']; job['owner_user_id'] = row['owner']
    return job, queue['data']

def heartbeat(repository, job_id, token, seconds=None):
    with repository.transaction() as tx:
        pair = fenced(tx, job_id, token)
        if not pair: return False
        job, queue = pair
        queue['lease_until'] = (datetime.now(timezone.utc)+timedelta(seconds=seconds or lease_duration())).isoformat().replace('+00:00','Z')
        tx.put('queue', job_id, job['owner_user_id'], queue)
        return True

def finish(repository, job_id, token, status, result=None, failure=None):
    with repository.transaction() as tx:
        pair = fenced(tx, job_id, token)
        if not pair: return False
        job, queue = pair
        if job['status']=='cancel_requested' and status!='cancelled': return False
        job.update(status=status, result=result, failure=failure, completed_at=now())
        if status == 'succeeded': job['stage'] = 'complete'
        update_paragraph_state(tx,job)
        event(tx, job, 'failure' if failure else 'result' if result else 'status', failure['message'] if failure else 'Gap analysis complete.' if result else 'No account was accepted.')
        return True
