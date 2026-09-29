"""Reconcile committed jobs into Redis, including complete broker loss."""
import logging
import signal
import time

from . import jobs
from .job_transport import RedisTransport
from .repository import Repository, now
from .runtime_config import setting

log = logging.getLogger('reveal.dispatcher')


def reconcile(repository, transport):
    transport.ensure_group()
    with repository.read_transaction() as tx:
        intents = [row['data'] for row in tx.list('dispatch') if row['data']['namespace'] == jobs.namespace()]
        states = tx.get_many('job', [item['job_id'] for item in intents])
        queues = tx.get_many('queue', [item['job_id'] for item in intents])
    count = 0
    for intent in intents:
        row, queue = states.get(intent['job_id']), queues.get(intent['job_id'])
        if not row or not queue or row['data']['status'] in jobs.TERMINAL: continue
        if queue['data'].get('held') or queue['data'].get('dispatch_id') != intent['dispatch_id']: continue
        if transport.exists(intent.get('message_id')): continue
        identity = transport.publish(intent)
        with repository.transaction() as tx:
            current = tx.get('dispatch', intent['job_id'])
            if current and current['data']['dispatch_id'] == intent['dispatch_id']:
                tx.put('dispatch', intent['job_id'], current['owner'],
                    dict(current['data'], message_id=identity, published_at=now()))
        count += 1
    with repository.transaction() as tx:
        tx.put('runtime', 'dispatcher:' + jobs.namespace(), 'system', {'namespace': jobs.namespace(), 'heartbeat_at': now(), 'published': count})
    return count


def main():
    logging.basicConfig(level=logging.INFO)
    if jobs.transport() != 'redis': raise RuntimeError('Dispatcher requires Redis transport')
    repository, transport = Repository(), RedisTransport()
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            reconcile(repository, transport)
        except Exception as exc:
            log.error('Dispatch unavailable (%s); jobs remain in the database', type(exc).__name__)
        time.sleep(float(setting('REVEAL_DISPATCH_INTERVAL_SECONDS', '3')))


if __name__ == '__main__': main()
