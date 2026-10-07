"""The container entrypoint ends event streams at shutdown, then waits only for real in-flight requests."""
import asyncio
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import httpx
import uvicorn

from reveal_backend import redis_notifications as notifications, serve, workspace_events as events

ROOT = Path(__file__).resolve().parents[3]


class ServeConfigTests(unittest.TestCase):
    def test_backstop_stays_below_the_compose_and_ecs_stop_windows(self):
        with patch.dict(os.environ, {'PORT': '', 'REVEAL_GRACEFUL_SHUTDOWN_SECONDS': ''}):
            config = serve.config()
        self.assertEqual((config.app, config.host, config.port, config.timeout_graceful_shutdown),
                         ('reveal_backend.app:app', '0.0.0.0', 8000, 110))
        with patch.dict(os.environ, {'PORT': '9001', 'REVEAL_GRACEFUL_SHUTDOWN_SECONDS': '20'}):
            config = serve.config()
        self.assertEqual((config.port, config.timeout_graceful_shutdown), (9001, 20))
        for value in ('0', '3601'):
            with patch.dict(os.environ, {'REVEAL_GRACEFUL_SHUTDOWN_SECONDS': value}), self.assertRaises(ValueError): serve.config()
        dockerfile = (ROOT / 'services/backend/Dockerfile').read_text()
        self.assertIn('CMD ["python", "-m", "reveal_backend.serve"]', dockerfile)
        self.assertNotIn('uvicorn', dockerfile)   # the bare CLI would wait on every open stream until SIGKILL
        self.assertIn('stop_grace_period: 120s', (ROOT / 'deploy/compose.yaml').read_text())
        self.assertIn('stop_timeout_seconds: 120', (ROOT / 'deploy/dig/service.yaml').read_text())

    def test_startup_failure_exits_like_the_cli(self):
        class Never(serve.Server):
            def run(self, sockets=None): self.started = False
        with patch.object(serve, 'Server', Never), self.assertRaises(SystemExit) as exited: serve.main()
        self.assertEqual(exited.exception.code, 3)


class ServeShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_closes_streams_before_uvicorn_waits_for_connections(self):
        order = []
        async def close(): order.append('streams')
        async def shutdown(self, sockets=None): order.append('uvicorn')
        with patch.object(notifications, 'close_streams', close), patch.object(uvicorn.Server, 'shutdown', shutdown):
            await serve.Server(uvicorn.Config(FastAPI())).shutdown()
        self.assertEqual(order, ['streams', 'uvicorn'])   # uvicorn is pinned at 0.34.2; guards an upgrade

    async def test_open_stream_ends_at_shutdown_while_an_in_flight_request_finishes(self):
        app = FastAPI()
        def read(*args): return {'items': [], 'terminal': False}
        @app.get('/stream')
        async def stream():
            initial = {'items': [{'id': '1', 'event_type': 'status'}], 'terminal': False}
            return StreamingResponse(events.job_event_stream(None, None, 'job', 'proof', 0, 100, read, initial),
                                     media_type='text/event-stream')
        @app.get('/slow')
        def slow(): time.sleep(1); return {'done': True}
        with patch.object(notifications, '_closing', False), \
                patch.object(notifications, 'configuration', return_value=('local', '', '')), \
                patch.object(events, 'stream_deadline', return_value=time.monotonic() + 60):
            server = serve.Server(uvicorn.Config(app, host='127.0.0.1', port=0, timeout_graceful_shutdown=110,
                                                 log_level='warning', lifespan='off'))
            serving = asyncio.ensure_future(server.serve())
            while not server.started: await asyncio.sleep(.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            async with httpx.AsyncClient(base_url='http://127.0.0.1:%d' % port, timeout=10) as client:
                async with client.stream('GET', '/stream') as response:
                    lines = response.aiter_lines()
                    self.assertEqual(await anext(lines), 'id: 1')
                    pending = asyncio.ensure_future(client.get('/slow'))
                    await asyncio.sleep(.3)
                    started = time.monotonic(); server.should_exit = True
                    async for _ in lines: pass   # ends instead of running out its 60 s window
                    self.assertLess(time.monotonic() - started, 1)
                self.assertEqual((await pending).json(), {'done': True})   # real work is not cut short
            await asyncio.wait_for(serving, 5)
        self.assertLess(time.monotonic() - started, 3)


if __name__ == '__main__': unittest.main()
