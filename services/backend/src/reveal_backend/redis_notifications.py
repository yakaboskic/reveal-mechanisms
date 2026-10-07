"""Push-only Redis notifications. RDS owns ordering, authorization and replay.

REST subscriptions block on incoming SSE frames; RESP subscriptions block in
listen(). Neither sends GET/XREAD polling, keepalive PINGs, or idle commands.
One provider subscription is shared by all interested local browser streams.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
import re
import threading
from urllib.parse import quote, urlsplit
import weakref

import httpx

log = logging.getLogger(__name__)
_hubs = weakref.WeakSet()
_hubs_lock = threading.Lock()
_client_lock = threading.Lock()
_client = (None, None)


def active_hubs():
    with _hubs_lock: return list(_hubs)


def namespace():
    value = os.getenv('REVEAL_NOTIFICATION_NAMESPACE') or os.getenv('REVEAL_APPLICATION_TABLE_PREFIX', 'reveal')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value):
        raise ValueError('Invalid notification namespace')
    return value


def channel(scope):
    return namespace() + ':notify:' + hashlib.sha256(scope.encode()).hexdigest()


def configuration():
    resp = os.getenv('REVEAL_NOTIFICATION_REDIS_URL', '')
    rest = os.getenv('REVEAL_NOTIFICATION_REDIS_REST_URL') or os.getenv('UPSTASH_REDIS_REST_URL', '')
    token = os.getenv('REVEAL_NOTIFICATION_REDIS_REST_TOKEN') or os.getenv('UPSTASH_REDIS_REST_TOKEN', '')
    if resp:
        if urlsplit(resp).scheme not in ('rediss', 'redis'):
            raise ValueError('Invalid notification Redis URL')
        return 'resp', resp, ''
    if rest or token:
        if not token or urlsplit(rest).scheme != 'https' or not urlsplit(rest).netloc:
            raise ValueError('Notification REST configuration requires HTTPS and a token')
        return 'rest', rest.rstrip('/'), token
    return 'local', '', ''


def _publisher_client(mode, endpoint, token):
    """One thread-safe client per process and configuration. httpx keeps an idle connection for only 5 s by
    default, shorter than the gap between most writes, so every publish paid a new TLS handshake."""
    global _client
    key = (os.getpid(), mode, endpoint, hashlib.sha256(token.encode()).hexdigest())
    with _client_lock:
        current, client = _client
        if current == key: return client
        if client is not None and current[0] == os.getpid():
            try: client.close()
            except Exception: pass  # a publish still using it fails and stays in the outbox
        if mode == 'rest':
            client = httpx.Client(base_url=endpoint, headers={'Authorization': 'Bearer '+token}, timeout=3,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=55))
        elif mode == 'resp':
            import redis
            client = redis.Redis.from_url(endpoint, socket_timeout=3, socket_connect_timeout=3, health_check_interval=0)
        else: client = None
        _client = (key, client)
        return client


def publish(channels):
    """Bounded post-commit publication, one command per changed audience/job."""
    channels = list(dict.fromkeys(channels))
    if not channels: return
    mode, endpoint, token = configuration()
    client = _publisher_client(mode, endpoint, token)
    if mode == 'rest':
        body = [['PUBLISH', name, 'changed'] for name in channels]
        try: response = client.post('/pipeline', json=body)
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
            response = client.post('/pipeline', json=body)  # once, for a kept-alive socket the provider closed
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, list) or len(result) != len(channels) or any('error' in item for item in result):
            raise RuntimeError('Redis publication was not acknowledged')
    elif mode == 'resp':
        pipeline = client.pipeline(transaction=False)
        for name in channels: pipeline.publish(name, 'changed')
        pipeline.execute()
    else:
        # Isolated tests/development can run in one process without Redis.
        for hub in active_hubs():
            for name in channels: hub.loop.call_soon_threadsafe(hub.wake, name, 'changed')


class Subscription:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=1)

    def wake(self, reason):
        # A notification is only a wakeup, so coalescing cannot lose events:
        # every wakeup replays the durable log after the consumer's cursor.
        if self.queue.full():
            self.queue.get_nowait()
            reason = 'resync'
        self.queue.put_nowait(reason)

    async def wait(self, timeout):
        # This timeout controls HTTP keepalives only, never a Redis command.
        return await asyncio.wait_for(self.queue.get(), timeout)

    def discard_pending(self):
        """Drop a queued wake that the caller's next durable read subsumes; keep a disconnect signal."""
        try: reason = self.queue.get_nowait()
        except asyncio.QueueEmpty: return
        if reason == 'disconnected': self.queue.put_nowait(reason)


class NotificationHub:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.listeners = {}
        self.tasks = {}
        self.ready = {}
        self.connected = set()
        self.reconnects = 0
        self.failures = 0
        with _hubs_lock: _hubs.add(self)

    def wake(self, name, reason='changed'):
        for listener in tuple(self.listeners.get(name, ())): listener.wake(reason)

    @asynccontextmanager
    async def subscribe(self, scopes):
        names = [channel(scope) for scope in dict.fromkeys(scopes)]
        subscription = Subscription()
        try:
            for name in names:
                self.listeners.setdefault(name, set()).add(subscription)
                if name not in self.tasks:
                    self.ready[name] = asyncio.Event()
                    self.tasks[name] = asyncio.create_task(self._consume(name))
            await asyncio.wait_for(asyncio.gather(*(self.ready[name].wait() for name in names)), 5)
            # Callers read the durable log right after this yields. That read starts after every SUBSCRIBE ack, so
            # it covers any wake queued so far, including _subscribed's own 'replay' for a new channel.
            subscription.discard_pending()
            yield subscription
        finally:
            stopped = []
            for name in names:
                self.listeners.get(name, set()).discard(subscription)
                if not self.listeners.get(name):
                    self.listeners.pop(name, None)
                    task = self.tasks.pop(name, None)
                    if task: task.cancel(); stopped.append(task)
                    self.ready.pop(name, None)
                    self.connected.discard(name)
            if stopped: await asyncio.gather(*stopped, return_exceptions=True)

    def _subscribed(self, name):
        self.connected.add(name)
        self.ready[name].set()
        self.wake(name, 'replay')

    async def _consume(self, name):
        delay = .5
        while True:
            try:
                mode, endpoint, token = configuration()  # reread rotated configuration on reconnect
                if mode == 'local':
                    self._subscribed(name)
                    await asyncio.Future()
                elif mode == 'rest':
                    timeout = httpx.Timeout(connect=5, read=None, write=5, pool=5)
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        async with client.stream('POST', endpoint+'/subscribe/'+quote(name, safe=''),
                                headers={'Authorization':'Bearer '+token, 'Accept':'text/event-stream'}) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if not line.startswith('data:'): continue
                                value = line[5:].strip()
                                if value.startswith('subscribe,'):
                                    self._subscribed(name); delay = .5
                                elif value.startswith('message,'):
                                    self.wake(name)
                else:
                    import redis.asyncio as redis
                    client = redis.Redis.from_url(endpoint, socket_timeout=None, socket_connect_timeout=5, health_check_interval=0)
                    try:
                        async with client.pubsub() as subscription:
                            await subscription.subscribe(name)
                            async for message in subscription.listen():
                                if message['type'] == 'subscribe':
                                    self._subscribed(name); delay = .5
                                elif message['type'] == 'message': self.wake(name)
                    finally: await client.aclose()
                raise ConnectionError('Notification stream closed')
            except asyncio.CancelledError: raise
            except Exception:
                self.failures += 1
                self.connected.discard(name)
                self.wake(name, 'disconnected')
                # Retry only after a failed/closed transport; idle consumers
                # remain blocked on pushed bytes for the entire connection.
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)
                self.reconnects += 1

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear(); self.listeners.clear(); self.connected.clear()

    def health(self):
        return {'transport': configuration()[0], 'subscriptions': len(self.tasks),
            'connected': len(self.connected), 'connections': len({id(x) for group in self.listeners.values() for x in group}),
            'reconnects': self.reconnects, 'failures': self.failures}


def hub():
    loop = asyncio.get_running_loop()
    for existing in active_hubs():
        if existing.loop is loop: return existing
    return NotificationHub()
