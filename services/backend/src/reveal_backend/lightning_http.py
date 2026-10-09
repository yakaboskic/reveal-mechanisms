"""Private Lightning audit routes and bounded lifecycle maintenance."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Request
from fastapi.responses import JSONResponse

from .auth import Problem
from .research_work import ResearchWorkService
from .lightning_continuation import continue_audit


def register(app, repository, catalog, *, freeze, reload_gate, check_job_quota,
             analysis_job_rows, deliver_after_response, paginate=None):
    from . import lightning_audits as audits

    @app.post('/v1/lightning-audits', status_code=202)
    async def create(request: Request):
        try: body = await request.json()
        except ValueError: raise Problem(422, 'INVALID_REQUEST', 'Supply draft_id and draft_version.') from None
        result = await asyncio.to_thread(audits.start, repository(), catalog(), request.headers.get('authorization'),
            body, request.headers.get('idempotency-key'), freeze=freeze, reload_gate=reload_gate)
        return JSONResponse(result, status_code=202, headers={'Location': '/v1/lightning-audits/' + result['id'], 'Retry-After': '2'})

    @app.get('/v1/lightning-audits')
    def listing(request: Request, limit: int = 50, cursor: str | None = None):
        if not 1 <= limit <= 100: raise Problem(422, 'INVALID_QUERY', 'Supply a limit from 1 to 100.')
        return audits.listing(repository(), request.headers.get('authorization'), paginate=paginate, limit=limit, cursor=cursor)

    @app.get('/v1/lightning-audits/{audit_id}')
    async def detail(audit_id: str, request: Request, wait: str = '0', after_revision: str | None = None):
        if not (wait.isascii() and wait.isdigit() and int(wait) <= 20):
            raise Problem(422, 'INVALID_QUERY', 'Supply wait as an integer from 0 to 20 seconds.')
        if after_revision is not None and not (after_revision.isascii() and after_revision.isdigit() and len(after_revision) <= 12):
            raise Problem(422, 'INVALID_QUERY', 'Supply after_revision as a nonnegative integer.')
        return await audits.wait_get(repository(), request.headers.get('authorization'), audit_id, int(wait),
            int(after_revision) if after_revision is not None else None)

    @app.post('/v1/lightning-audits/{audit_id}/continue', status_code=202)
    async def continuation(audit_id: str, request: Request, background: BackgroundTasks):
        try: body = await request.json()
        except ValueError: raise Problem(422, 'INVALID_REQUEST', 'Supply a mode and research_direction.') from None
        result = await asyncio.to_thread(continue_audit, repository(), request.headers.get('authorization'), audit_id,
            body, request.headers.get('idempotency-key'), reload_gate=reload_gate, check_job_quota=check_job_quota,
            analysis_job_rows=analysis_job_rows)
        if result['mode'] == 'online': deliver_after_response(background, result['id'], control=False)
        else: background.add_task(ResearchWorkService(repository()).kick, result['id'])
        location = ('/v1/jobs/' if result['mode'] == 'online' else '/v1/local-work/') + result['id']
        return JSONResponse(result, status_code=202, headers={'Location': location, 'Retry-After': '2'})

    previous = app.router.lifespan_context

    async def recover():
        while True:
            try: await asyncio.to_thread(audits.reconcile, repository())
            except Exception: logging.getLogger(__name__).warning('Lightning audit reconciliation deferred', exc_info=False)
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application) as state:
            recovery = asyncio.create_task(recover())
            try: yield state
            finally:
                recovery.cancel()
                try: await recovery
                except asyncio.CancelledError: pass

    app.router.lifespan_context = lifespan
