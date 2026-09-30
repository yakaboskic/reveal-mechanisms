"""Mount the API below the path forwarded intact by a shared load balancer."""
from contextlib import asynccontextmanager
import re

from fastapi import FastAPI


def mount_service(application: FastAPI, prefix: str) -> FastAPI:
    if not prefix:
        return application
    prefix = prefix.rstrip('/')
    if not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)+', prefix):
        raise ValueError('SERVICE_PATH_PREFIX must be an absolute path of nonempty URL segments.')

    @asynccontextmanager
    async def lifespan(_):
        async with application.router.lifespan_context(application) as state:
            yield state

    # An actual mount retains the contract's /v1/... route templates for query
    # validation while setting root_path for Swagger, ReDoc and URL redirects.
    mounted = FastAPI(docs_url=None, redoc_url=None,
                      openapi_url=prefix + '/openapi.json', lifespan=lifespan)
    mounted.openapi = lambda: {**application.openapi(), 'servers': [{'url': prefix}]}
    mounted.mount(prefix, application)
    return mounted
