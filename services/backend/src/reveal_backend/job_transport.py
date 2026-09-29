"""Redis delivery is disposable; RDS owns dispatch intent and execution leases."""
from . import jobs
from .runtime_config import setting


class RedisTransport:
    def __init__(self, client=None):
        import redis
        self.client = client or redis.Redis.from_url(setting('REVEAL_REDIS_URL', 'redis://redis:6379/0'),
            decode_responses=True, socket_connect_timeout=3, socket_timeout=5)
        self.stream = jobs.namespace() + ':jobs'
        self.group = jobs.namespace() + ':workers'
        self.reclaim_cursor = '0-0'

    def ensure_group(self):
        from redis.exceptions import ResponseError
        try:
            self.client.xgroup_create(self.stream, self.group, id='0-0', mkstream=True)
        except ResponseError as exc:
            if 'BUSYGROUP' not in str(exc): raise

    def publish(self, item):
        return self.client.xadd(self.stream, {key: str(item[key]) for key in ('job_id', 'dispatch_id', 'namespace', 'schema')})

    def exists(self, identity):
        return bool(identity and self.client.xrange(self.stream, min=identity, max=identity, count=1))

    def receive(self, consumer):
        self.ensure_group()
        idle = max(1000, int(setting('REVEAL_REDIS_RECLAIM_MS', str(jobs.lease_duration() * 1000))))
        pending = self.client.xautoclaim(self.stream, self.group, consumer, idle, self.reclaim_cursor, count=1)
        self.reclaim_cursor = pending[0]
        if pending[1]: return pending[1][0]
        messages = self.client.xreadgroup(self.group, consumer, {self.stream: '>'}, count=1, block=1000)
        return messages[0][1][0] if messages else None

    def acknowledge(self, identity):
        # One consumer group by design. Terminal history remains in RDS.
        pipe = self.client.pipeline(transaction=True)
        pipe.xack(self.stream, self.group, identity)
        pipe.xdel(self.stream, identity)
        pipe.execute()

    def disposition(self, repository, message):
        """Ignore stale generations/terminal jobs, without trusting transport data."""
        if message.get('namespace') != jobs.namespace() or message.get('schema') != '1': return 'discard'
        with repository.read_transaction() as tx:
            row, queue = tx.get('job', message.get('job_id', '')), tx.get('queue', message.get('job_id', ''))
            if not row or not queue: return 'discard'
            if (row['data']['status'] in jobs.TERMINAL or queue['data'].get('dispatch_id') != message.get('dispatch_id')
                    or queue['data'].get('namespace') != jobs.namespace()): return 'discard'
        return 'eligible'
