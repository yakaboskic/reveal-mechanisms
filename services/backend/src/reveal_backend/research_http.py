"""Browser lifecycle and sessionless MCP with anonymous public reads and OAuth."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp import types

from .auth import Problem, owned, principal, principal_with, require_owned
from .repository import now
from .research_work import ResearchWorkService, authenticate, bearer_shaped, idempotent, issue_grant, public_base, TERMINAL
from .research_tools import definitions, dispatch, check_child, public_call
from . import user_inputs
from . import research_setup

MCP_CHALLENGE = 'reveal.mcp_challenge'   # ASGI scope key: a read tool's authentication failure, answered as HTTP

def register(app, repository, *, freeze, preload, reload_gate, service_factory=ResearchWorkService):
    from . import research_oauth
    research_oauth.register(app, repository)
    def service(): return service_factory(repository())
    def local_row(tx, owner, work_id, *row):
        """Authorize the local_work row (prefetched in this transaction when given); hosted work is 404."""
        row = require_owned(tx, 'local_work', work_id, owner, row[0]) if row else owned(tx, 'local_work', work_id, owner)
        if row['data'].get('job_id'): raise Problem(404, 'NOT_FOUND', 'Local research unavailable.')
        return row
    def local_work(tx, owner, work_id): return local_row(tx, owner, work_id)['data']

    @app.post('/v1/local-work', status_code=201)
    async def create(request: Request):
        try: body = await request.json()
        except ValueError: raise Problem(422, 'INVALID_REQUEST', 'Supply a JSON object with draft_id and draft_version.')
        if (not isinstance(body, dict) or set(body) != {'draft_id', 'draft_version'} or not isinstance(body['draft_id'], str)
                or not body['draft_id'] or type(body['draft_version']) is not int or body['draft_version'] < 1):
            raise Problem(422, 'INVALID_REQUEST', 'Supply draft_id and integer draft_version.')
        await asyncio.to_thread(preload)
        def run():
            runner = service()
            with runner.repo.transaction() as tx:
                identity = principal(tx, request.headers.get('authorization')); reload_gate(tx)
                result = runner.create(tx, identity, body, request.headers.get('idempotency-key'), freeze)
            runner.kick(result['id']); return result
        return await asyncio.to_thread(run)

    @app.get('/v1/local-work')
    def listing(request: Request):
        with repository().read_transaction() as tx:
            owner = principal(tx, request.headers.get('authorization'))['user_id']
            # Owner-filtered rows; views() reads every work's children in a fixed number of statements.
            return {'items': service().views(tx, owner, [r for r in tx.list('local_work', owner) if not r['data'].get('job_id')])}

    @app.get('/v1/local-work/{work_id}')
    def get(work_id: str, request: Request):
        runner = service(); loaded = {}
        with runner.repo.read_transaction() as tx:
            me, rows = principal_with(tx, request.headers.get('authorization'), (('local_work', work_id),))
            row = local_row(tx, me['user_id'], work_id, rows.get(('local_work', work_id)))
            result = runner.view(tx, me['user_id'], work_id, work_row=row, loaded=loaded)
        # Recovery is scheduled from this snapshot's operations, never a second read.
        runner.resume_pending(loaded['operations']); return result

    @app.get('/v1/local-work/{work_id}/package')
    def package(work_id: str, request: Request):
        runner = service()
        with runner.repo.read_transaction() as tx:
            owner = principal(tx, request.headers.get('authorization'))['user_id']
            local_work(tx, owner, work_id)
            return runner.package(tx, owner, work_id)

    @app.post('/v1/local-work/{work_id}/setup-kit')
    async def setup_kit(work_id: str, request: Request):
        try: body = await request.json()
        except ValueError: raise Problem(422, 'INVALID_REQUEST', 'Choose codex or claude_code.')
        if not isinstance(body, dict) or set(body) != {'client'} or body['client'] not in ('codex', 'claude_code'):
            raise Problem(422, 'INVALID_REQUEST', 'Choose codex or claude_code.')
        raw = await asyncio.to_thread(research_setup.build_setup, repository(),
            request.headers.get('authorization'), work_id, body['client'])
        return Response(raw, media_type='application/zip', headers={'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': "attachment; filename*=UTF-8''"+quote('reveal-'+work_id+'.zip', safe='')})

    @app.delete('/v1/local-work/{work_id}/setup-tickets', status_code=204)
    def revoke_setup_tickets(work_id: str, request: Request):
        with repository().transaction() as tx:
            owner = principal(tx, request.headers.get('authorization'))['user_id']
            research_setup.revoke_tickets(tx, owner, work_id)
        return Response(status_code=204, headers={'Cache-Control': 'private, no-store'})

    @app.post('/v1/research-setup/exchange')
    async def exchange_setup(request: Request):
        # Bound this unauthenticated credential exchange before decoding JSON.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096: raise Problem(413, 'REQUEST_TOO_LARGE', 'The setup exchange request is too large.')
        try: body = json.loads(raw)
        except (ValueError, UnicodeError): raise Problem(422, 'INVALID_REQUEST', 'Supply a ticket and token_sha256.')
        if not isinstance(body, dict) or set(body) != {'ticket', 'token_sha256'}:
            raise Problem(422, 'INVALID_REQUEST', 'Supply a ticket and token_sha256.')
        def run():
            with repository().transaction() as tx:
                return research_setup.exchange(tx, body['ticket'], body['token_sha256'], request.headers.get('authorization'))
        result = await asyncio.to_thread(run)
        return JSONResponse(result, headers={'Cache-Control': 'private, no-store'})

    @app.post('/v1/local-work/{work_id}/grants', status_code=201)
    def grant(work_id: str, request: Request):
        with repository().transaction() as tx:
            owner = principal(tx, request.headers.get('authorization'))['user_id']
            local_work(tx, owner, work_id)
            result = issue_grant(tx, owner, work_id, request.headers.get('idempotency-key'))
        return JSONResponse(result, status_code=201, headers={'Cache-Control': 'no-store'})

    @app.delete('/v1/local-work/{work_id}/grants/{grant_id}', status_code=204)
    def revoke(work_id: str, grant_id: str, request: Request):
        with repository().transaction() as tx:
            owner = principal(tx, request.headers.get('authorization'))['user_id']
            local_work(tx, owner, work_id)
            matches = [r for r in tx.list('research_access', owner) if r['data']['local_work_id'] == work_id and r['data']['grant_id'] == grant_id]
            if not matches: raise Problem(404, 'NOT_FOUND', 'Connection unavailable.')
            for row in matches:
                row['data']['revoked_at'] = now(); tx.put('research_access', row['id'], owner, row['data'])
            research_oauth.revoke_oauth_grant(tx, owner, grant_id)
        return Response(status_code=204)

    @app.post('/v1/local-work/{work_id}/close')
    def close(work_id: str, request: Request):
        runner = service()
        with runner.repo.transaction() as tx:
            owner = principal(tx, request.headers.get('authorization'))['user_id']
            row = local_row(tx, owner, work_id); work = row['data']
            def action():
                work.update(state='closed', closed_at=now(), last_action='closed'); tx.put('local_work', work_id, owner, work)
                # Closing blocks new work, but already-authorized bounded operations
                # can finish unless their grant is explicitly revoked.
                runner.release_pin_if_idle(tx, work)
                return {'id': work_id, 'state': 'closed'}
            idempotent(tx, owner, 'close:'+work_id, request.headers.get('idempotency-key'), {}, action)
            # Replays repeat the mutation result, never stale borrowed evidence.
            return runner.view(tx, owner, work_id, work_row=row)

    @app.put('/v1/research-uploads/{upload_id}/content')
    async def upload(upload_id: str, request: Request):
        runner = service(); authorization = request.headers.get('authorization')
        def check():
            with runner.repo.read_transaction() as tx:
                authority = authenticate(tx, authorization, write=True)
                research_oauth.require_registered_local(authority)
                if authority['grant']['kind'] != 'local': raise Problem(403, 'HOSTED_TOOL_SCOPE', 'This connection cannot upload.')
                value = check_child(tx, 'research_upload', upload_id, authority)
                if value['expires_at'] <= now(): raise Problem(410, 'UPLOAD_EXPIRED', 'Prepare another upload.')
                return value
        value = await asyncio.to_thread(check)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > value['size_bytes']: raise Problem(413, 'UPLOAD_TOO_LARGE', 'Upload exceeds its declared size.')
        if len(data) != value['size_bytes'] or hashlib.sha256(data).hexdigest() != value['sha256']:
            raise Problem(422, 'UPLOAD_CHECKSUM_MISMATCH', 'Upload size or SHA-256 differs from the declaration.')
        storage = await asyncio.to_thread(user_inputs.retain, bytes(data), user_inputs.TYPES[Path(value['filename']).suffix.lower()])
        def commit():
            with runner.repo.transaction() as tx:
                authority = authenticate(tx, authorization, write=True)
                research_oauth.require_registered_local(authority)
                current = check_child(tx, 'research_upload', upload_id, authority)
                if current['expires_at'] <= now(): raise Problem(410, 'UPLOAD_EXPIRED', 'Prepare another upload.')
                current['storage'] = storage; tx.put('research_upload', upload_id, authority['owner'], current)
        await asyncio.to_thread(commit)
        return {'upload_id': upload_id, 'sha256': value['sha256'], 'size_bytes': len(data)}

    @app.get('/v1/research-artifacts/{artifact_id}/content')
    def download(artifact_id: str, request: Request):
        with repository().read_transaction() as tx:
            authority = authenticate(tx, request.headers.get('authorization'))
            research_oauth.require_registered_local(authority)
            artifact = check_child(tx, 'research_artifact', artifact_id, authority)
            from .research_execution import authorize_artifact
            authorize_artifact(tx, authority['owner'], artifact)
        from .research_execution import read_artifact_bytes
        raw = read_artifact_bytes(artifact)
        # Dependencies may be withdrawn while immutable bytes are read.
        with repository().read_transaction() as tx:
            current = authenticate(tx, request.headers.get('authorization'))
            artifact = check_child(tx, 'research_artifact', artifact_id, current)
            authorize_artifact(tx, current['owner'], artifact)
        return Response(raw, media_type=artifact['storage'].get('content_type', 'application/octet-stream'),
            headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                'Content-Disposition': "attachment; filename*=UTF-8''" + quote(Path(artifact['filename']).name, safe='')})

    @app.get('/v1/public-research/captures/{capture_id}/artifacts/{artifact_sha256}')
    def public_artifact(capture_id: str, artifact_sha256: str, request: Request):
        from .research_public import capture_artifact
        raw, descriptor = capture_artifact(service(), capture_id, artifact_sha256,
            rate_key=request.client.host if request.client else 'unknown')
        return Response(raw, media_type='application/json', headers={'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': "attachment; filename*=UTF-8''"+quote(Path(descriptor.get('path', 'capture.json')).name, safe='')})

    tool_definitions = definitions()
    async def list_tools(context, params):
        return types.ListToolsResult(tools=[types.Tool(**tool) for tool in tool_definitions])

    async def call_tool(context, params):
        runner = service(); authorization = context.request.headers.get('authorization')
        try:
            args = params.arguments or {}
            result = await asyncio.to_thread(dispatch, runner, authorization, params.name, args,
                rate_key=context.request.client.host if context.request.client else 'unknown',
                on_operation=runner.resume_operation)
            return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(result))], structuredContent=result)
        except Problem as error:
            if getattr(error, 'mcp_challenge', False) and getattr(context, 'request', None) is not None:
                context.request.scope[MCP_CHALLENGE] = error
            result = {'code': error.code, 'detail': error.detail, **error.extra}
            metadata = ({'mcp/www_authenticate': [research_oauth.challenge(write=True,
                error='insufficient_scope' if error.status == 403 else 'invalid_token')]}
                if error.status in (401, 403) else None)
            return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(result))],
                structuredContent=result, isError=True, _meta=metadata)

    server = Server('reveal', version='1.0.0', on_list_tools=list_tools, on_call_tool=call_tool,
        get_tool_input_schema=lambda name: next((t['inputSchema'] for t in tool_definitions if t['name'] == name), None))
    parsed = urlsplit(public_base())
    def make_transport():
        return server.streamable_http_app(json_response=True, stateless_http=True,
            transport_security=TransportSecuritySettings(allowed_hosts=[parsed.netloc, 'localhost:*', '127.0.0.1:*', '[::1]:*'],
                allowed_origins=[parsed.scheme+'://'+parsed.netloc]), max_request_body_size=2_000_000)
    transport = make_transport()

    class Authorized:
        def __init__(self, downstream): self.downstream = downstream
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            try:
                authorization = request.headers.get('authorization')
                message = None
                downstream_receive = receive
                if request.method == 'POST':
                    raw = bytearray()
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > 2_000_000:
                            raise Problem(413, 'REQUEST_TOO_LARGE', 'MCP request exceeds its size limit.')
                    try: message = json.loads(raw)
                    except (ValueError, UnicodeError): pass  # SDK owns malformed JSON errors.
                    consumed = False
                    async def replay():
                        nonlocal consumed
                        if not consumed:
                            consumed = True
                            return {'type': 'http.request', 'body': bytes(raw), 'more_body': False}
                        return await receive()
                    downstream_receive = replay
                protected = False
                write = False
                call = isinstance(message, dict) and message.get('method') == 'tools/call'
                if call:
                    params = message.get('params')
                    if isinstance(params, dict) and isinstance(params.get('name'), str):
                        tool = next((t for t in tool_definitions if t['name'] == params['name']), None)
                        protected = tool is not None and not public_call(params['name'], params.get('arguments') or {})
                        write = protected and not tool['annotations']['readOnlyHint']
                if protected and not bearer_shaped(authorization):
                    raise Problem(401, 'MCP_AUTH_REQUIRED', 'Connect using a Reveal research credential.')
                # Handshakes keep their HTTP 401 for a bad bearer. Writes are pre-checked outside the global
                # fence, so an invalid credential never takes it. A read tool's dispatch authenticates once,
                # inside its own snapshot, and its failure comes back here as the same HTTP challenge.
                if authorization and (write or not call):
                    def check():
                        with repository().read_transaction() as tx:
                            authority = authenticate(tx, authorization)
                            if write: research_oauth.check_grant_scope(tx, authority['owner'], authority['grant'],
                                write=True, me=authority['me'], family=authority['oauth_family'])
                            if protected: research_oauth.require_registered_local(authority)
                    await asyncio.to_thread(check)
            except Problem as error:
                response = JSONResponse({'code': error.code, 'detail': error.detail}, status_code=error.status,
                    headers={'WWW-Authenticate': research_oauth.challenge(write=locals().get('write', False),
                        error='insufficient_scope' if error.status == 403 else 'invalid_token'), 'Cache-Control': 'no-store'})
                return await response(scope, receive, send)
            challenge = None
            async def private_send(message):
                nonlocal challenge
                if message['type'] == 'http.response.start':
                    # JSON-response mode starts the reply only after the tool returned, so call_tool has
                    # already recorded any authentication failure in the scope.
                    failure = scope.get(MCP_CHALLENGE)
                    if failure is None:
                        message['headers'].append((b'cache-control', b'private, no-store'))
                    else:
                        challenge = json.dumps({'code': failure.code, 'detail': failure.detail}, ensure_ascii=False, separators=(',', ':')).encode()
                        message = {'type': 'http.response.start', 'status': failure.status, 'headers': [
                            (b'content-type', b'application/json'), (b'content-length', str(len(challenge)).encode()),
                            (b'www-authenticate', research_oauth.challenge(
                                error='insufficient_scope' if failure.status == 403 else 'invalid_token').encode()),
                            (b'cache-control', b'no-store')]}
                elif message['type'] == 'http.response.body' and challenge is not None:
                    if message.get('more_body'): return
                    message = {'type': 'http.response.body', 'body': challenge, 'more_body': False}
                await send(message)
            await self.downstream(scope, downstream_receive, private_send)
    registered = list(transport.routes)
    for route in registered:
        route.app = Authorized(route.app); app.router.routes.append(route)
    previous = app.router.lifespan_context
    async def recover():
        import logging
        while True:
            await asyncio.sleep(30)
            try: await asyncio.to_thread(service().reconcile)
            except Exception:
                logging.getLogger(__name__).warning('Research operation recovery deferred', exc_info=False)
    @asynccontextmanager
    async def lifespan(application):
        # The SDK's manager has a single-run lifecycle. A new app lifecycle
        # needs a fresh manager even though the durable work remains unchanged.
        active_transport = make_transport()
        for route, active in zip(registered, active_transport.routes):
            route.app = Authorized(active.app)
        async with previous(application), active_transport.router.lifespan_context(active_transport):
            recovery = asyncio.create_task(recover())
            try: yield
            finally:
                recovery.cancel()
                try: await recovery
                except asyncio.CancelledError: pass
    app.router.lifespan_context = lifespan
