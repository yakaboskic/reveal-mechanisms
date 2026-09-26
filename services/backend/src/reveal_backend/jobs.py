"""Transactional queue, ordered event log, leases, cancellation and acceptance."""
from datetime import datetime, timezone, timedelta
from .repository import now, uid
from .auth import Problem, owned

TERMINAL = {'succeeded', 'failed', 'cancelled', 'insufficient_evidence'}

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
    return item

def enqueue(tx, owner, kind, request_id=None, account_id=None, inputs=None):
    identity = uid(); timestamp = now()
    job = {'id': identity, 'kind': kind, 'owner_user_id': owner, 'status': 'queued', 'stage': 'queued',
        'research_request_id': request_id, 'input_account_id': account_id, 'created_at': timestamp, 'updated_at': timestamp,
        'completed_at': None, 'result': None, 'failure': None, 'warnings': [], 'last_event_id': '0',
        'links': {'self': '/v1/jobs/'+identity, 'events': '/v1/jobs/'+identity+'/events', 'cancel': '/v1/jobs/'+identity+'/cancel'}}
    tx.put('queue', identity, owner, {'attempt': 0, 'lease_until': None, 'token': None, 'remote_handle': None, 'inputs': inputs or {}})
    update_paragraph_state(tx,job)
    event(tx, job, 'status', 'Queued for evidence preparation.' if kind == 'analysis' else 'Research statement queued.')
    return job

def cancel(tx, job):
    if job['status'] in TERMINAL: return job
    if job['status']=='cancel_requested': return job
    queue=tx.get('queue',job['id'])
    active=queue and (queue['data'].get('token') or queue['data'].get('remote_handle'))
    job['status'] = 'cancel_requested' if active else 'cancelled'
    job['completed_at'] = None if active else now()
    update_paragraph_state(tx,job)
    event(tx, job, 'status', 'Stopping the current execution.' if active else 'Stopped by the workspace owner.')
    return job

def claim(repository, worker_id, lease_seconds=90):
    with repository.transaction() as tx:
        candidates = list(reversed(tx.list('job')))
        for row in candidates:
            job = row['data']; job['owner_user_id'] = row['owner']
            if job['status'] in TERMINAL: continue
            queue_row = tx.get('queue', job['id']); queue = queue_row['data']
            if queue['lease_until'] and queue['lease_until'] > now(): continue
            if not queue.get('remote_handle'): queue['attempt'] += 1
            queue['token'] = uid(); queue['worker_id'] = worker_id
            queue['lease_until'] = (datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat().replace('+00:00','Z')
            tx.put('queue', job['id'], row['owner'], queue)
            tx.put('attempt', f"{job['id']}:{queue['attempt']}", row['owner'], dict(queue, started_at=now()))
            if job['status']!='cancel_requested': job['status'] = 'running'
            update_paragraph_state(tx,job)
            event(tx, job, 'status', 'Worker resumed the persisted attempt.' if queue['remote_handle'] else 'Preparing the selected evidence.')
            return job, queue
    return None

def fenced(tx, job_id, token):
    row = tx.get('job', job_id); queue = tx.get('queue', job_id)
    if not row or not queue or queue['data']['token'] != token or queue['data']['lease_until'] <= now() or row['data']['status'] in TERMINAL:
        return None
    job = row['data']; job['owner_user_id'] = row['owner']
    return job, queue['data']

def heartbeat(repository, job_id, token, seconds=90):
    with repository.transaction() as tx:
        pair = fenced(tx, job_id, token)
        if not pair: return False
        job, queue = pair
        queue['lease_until'] = (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat().replace('+00:00','Z')
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
