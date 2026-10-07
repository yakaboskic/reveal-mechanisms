"""Container entrypoint: uvicorn whose shutdown first ends open event streams.

Otherwise each SSE stream runs out its window of up to 240 s while uvicorn waits
for connections to close, and the API refuses connections until SIGKILL. Real
in-flight requests, including workflow steps of up to 360 s that the deploy drain
windows were sized for, still finish; REVEAL_GRACEFUL_SHUTDOWN_SECONDS (default
110, under the 120 s compose and ECS stop windows) bounds the whole wait.
"""
import os
import sys

import uvicorn

from . import redis_notifications


class Server(uvicorn.Server):
    async def shutdown(self, sockets=None):
        await redis_notifications.close_streams()
        await super().shutdown(sockets=sockets)


def config():
    graceful = int(os.getenv('REVEAL_GRACEFUL_SHUTDOWN_SECONDS') or '110')
    if not 1 <= graceful <= 3600: raise ValueError('REVEAL_GRACEFUL_SHUTDOWN_SECONDS must be between 1 and 3600')
    return uvicorn.Config('reveal_backend.app:app', host='0.0.0.0', port=int(os.getenv('PORT') or '8000'),
                          timeout_graceful_shutdown=graceful)


def main():
    server = Server(config())
    try: server.run()
    except KeyboardInterrupt: pass
    if not server.started: sys.exit(3)  # uvicorn's STARTUP_FAILURE, as with the CLI


if __name__ == '__main__': main()
