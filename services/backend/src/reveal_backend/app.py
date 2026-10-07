"""REVEAL public contract and separately authenticated gateway operations."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
import os
import base64
import hmac
import re
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, Request, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse, Response, RedirectResponse
from starlette.concurrency import run_in_threadpool
from jsonschema import Draft202012Validator
from .auth import Problem, decode_assertion, owned, require_owned, principal, principal_with, publication_principal, service_authority
from .catalog import GENERATION_TTL_SECONDS, Catalog
from .repository import DatabaseBusy, Repository, now, uid, digest
from .runtime_config import ROOT, artifacts_root
from .service_routing import mount_service
from . import jobs
from .account_discovery import visible_accounts, counts_by_gap, counted_gap, gap_snapshot, by_reference_state, resolved_mechanism_count
from . import publication
from . import analysis_outcomes
from . import reference_generation
from . import votes
from . import user_inputs

CONTRACT = json.loads((ROOT/'api/openapi.json').read_text())
GATEWAY = json.loads((ROOT/'schema/gateway.schema.json').read_text())
# The catalog polls the active reference generation on its own thread, never inside a request's transaction.
repo, catalog = Repository(), Catalog(poll_seconds=GENERATION_TTL_SECONDS)

def validate_query(request:Request):
    template=getattr(request.scope.get('route'),'path','')
    operation=CONTRACT['paths'].get(template,{}).get(request.method.lower(),{})
    for parameter in operation.get('parameters',[]):
        if parameter['in']!='query': continue
        value=request.query_params.get(parameter['name'])
        if value is None:
            if parameter.get('required'): raise Problem(422,'INVALID_QUERY','Missing query parameter: '+parameter['name'])
            continue
        schema=parameter.get('schema',{})
        if schema.get('type')=='integer':
            try: value=int(value)
            except ValueError: raise Problem(422,'INVALID_QUERY','Invalid integer parameter: '+parameter['name'])
        if list(Draft202012Validator(schema).iter_errors(value)):
            raise Problem(422,'INVALID_QUERY','Invalid query parameter: '+parameter['name'])

app = FastAPI(title='REVEAL Mechanisms', version='0.2.0', docs_url='/docs',dependencies=[Depends(validate_query)])
app.openapi = lambda: CONTRACT

@app.middleware('http')
async def publication_cache_policy(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path.removeprefix(request.scope.get('root_path', ''))
    if path.startswith(('/v1/drafts', '/v1/admin/', '/v1/accounts', '/v1/claims', '/v1/objects', '/v1/gene-sets',
            '/v1/paragraphs', '/v1/citations', '/v1/artifacts', '/v1/knowledge-gaps', '/v1/analysis-outcomes', '/v1/leaderboard')):
        # Visibility is revocable and workspace responses vary by principal.
        response.headers['Cache-Control'] = 'private, no-store'
        vary = [part.strip() for part in response.headers.get('Vary', '').split(',') if part.strip()]
        if 'authorization' not in {part.lower() for part in vary}: vary.append('Authorization')
        response.headers['Vary'] = ', '.join(vary)
    return response

@app.middleware('http')
async def measure_request(request: Request, call_next):
    import time
    from .runtime_metrics import begin_request, end_request, log_request, observe
    started = time.perf_counter()
    status = 500
    cost, token = begin_request()
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # Route template only: raw paths carry ids. Database cost stops at response headers, like the timing.
        route = request.method+' '+getattr(request.scope.get('route'), 'path', 'unmatched')
        elapsed = (time.perf_counter()-started)*1000
        end_request(token)
        observe('http', route, elapsed, status >= 500, extra=cost)
        log_request(route, status, elapsed, cost)

def validate(value, name, gateway=False):
    schema = GATEWAY if gateway else CONTRACT
    ref = '#/$defs/'+name if gateway else '#/components/schemas/'+name
    errors = list(Draft202012Validator({'$ref': ref, **schema}).iter_errors(value))
    if errors: raise Problem(422, 'INVALID_REQUEST', errors[0].message[:400])

@app.exception_handler(Problem)
async def problem_handler(request, exc):
    return JSONResponse({'type': 'urn:reveal:problem:'+exc.code.lower(), 'title': exc.code.replace('_',' ').title(),
        'status': exc.status, 'code': exc.code, 'detail': exc.detail, 'request_id': uid(), 'retryable': exc.status in (429,503), **exc.extra}, status_code=exc.status, media_type='application/problem+json')

@app.exception_handler(DatabaseBusy)
async def database_busy(request, exc):
    import logging
    logging.getLogger('reveal').warning('Database busy: %s', exc)  # fixed text: pool or writer admission
    return await problem_handler(request, Problem(503, 'SERVICE_UNAVAILABLE', 'The service is temporarily unavailable; retry shortly.'))

@app.exception_handler(Exception)
async def internal_error(request, exc):
    import logging
    logging.getLogger('reveal').error('Unhandled request failure (%s)', type(exc).__name__)
    return await problem_handler(request, Problem(503, 'SERVICE_UNAVAILABLE', 'The service is temporarily unavailable; retry shortly.'))

def page(items, owner='', limit=50, cursor=None, scope='', *, snapshot_items=None, seed=None):
    limit=max(1,min(limit,100)); snapshot=digest([owner,scope,items if snapshot_items is None else snapshot_items]); offset=0
    secret=os.getenv('REVEAL_GATEWAY_SECRET','').encode()
    if cursor:
        try:
            encoded,signature=cursor.split('.')
            if not hmac.compare_digest(hmac.new(secret,encoded.encode(),hashlib.sha256).hexdigest(),signature): raise ValueError()
            value=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
            if value['owner']!=owner or value['scope']!=scope or value['snapshot']!=snapshot: raise ValueError()
            if seed is not None and value.get('seed')!=seed: raise ValueError()
            offset=value['offset']
            if type(offset) is not int or offset<0: raise ValueError()
        except (ValueError,KeyError,TypeError): raise Problem(409,'CURSOR_EXPIRED','This collection changed or the cursor belongs to another query; reload its first page.')
    has_more=offset+limit<len(items); next_cursor=None
    if has_more:
        payload={'owner':owner,'scope':scope,'snapshot':snapshot,'offset':offset+limit}
        if seed is not None: payload['seed']=seed
        encoded=base64.urlsafe_b64encode(json.dumps(payload,separators=(',',':')).encode()).decode().rstrip('=')
        next_cursor=encoded+'.'+hmac.new(secret,encoded.encode(),hashlib.sha256).hexdigest()
    return {'items':items[offset:offset+limit],'page':{'next_cursor':next_cursor,'has_more':has_more,'snapshot_id':snapshot}}

def browse_seed(cursor):
    if not cursor: return uid()
    try:
        encoded,signature=cursor.split('.')
        secret=os.getenv('REVEAL_GATEWAY_SECRET','').encode()
        if not hmac.compare_digest(hmac.new(secret,encoded.encode(),hashlib.sha256).hexdigest(),signature): raise ValueError()
        seed=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))['seed']
        if not isinstance(seed,str) or not re.fullmatch('[a-f0-9-]{36}',seed): raise ValueError()
        return seed
    except (ValueError,KeyError,TypeError): raise Problem(409,'CURSOR_EXPIRED','Reload the first page to start a new browse session.')

def idempotent(tx, owner, route, key, body, action, *, staged=False):
    """staged: action returns (response, new rows), inserted with the idempotency row in one statement."""
    if not key or len(key)>200: raise Problem(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Supply an Idempotency-Key of at most 200 characters.')
    identity = digest([owner, route, key]); checksum = digest(body)
    existing = tx.get('idempotency', identity)
    if existing:
        if existing['data']['checksum'] != checksum: raise Problem(409, 'IDEMPOTENCY_CONFLICT', 'This retry key was already used for different input.')
        return existing['data']['response']
    result, rows = action() if staged else (action(), [])
    # Read as absent under this fence, so it is inserted unread; a duplicate fails the whole transaction.
    tx.insert_many([*rows, ('idempotency', identity, owner, {'checksum': checksum, 'response': result})])
    return result

def principal_for(tx, authorization, route, key, keys=()):
    """principal() plus, in the same statement, this route's idempotency row and the rows it reads next. The
    identity map serves those later reads; each row is still authorized where it is used (owned())."""
    exact=[item for item in keys if isinstance(item[1],str)]
    return principal_with(tx, authorization, lambda owner: [('idempotency', digest([owner, route, key])), *exact])[0]

def fresh_principal(kind, profile=None):
    identity = uid(); profile = profile or {}
    me = {'user_id': identity, 'principal_kind': kind, 'display_name': profile.get('display_name'), 'email': profile.get('email'),
        'email_verified': profile.get('email_verified'), 'orcid': profile.get('orcid'), 'orcid_authenticated': profile.get('orcid_authenticated',False),
        'person': None, 'workspace_expires_at': (datetime.now(timezone.utc)+timedelta(days=30)).isoformat().replace('+00:00','Z') if kind=='anonymous' else None}
    return me

def current_binding(binding):
    """A frozen catalog binding belongs to the served reference generation.

    Legacy mode (no active generation) keeps today's behaviour: saved selections retain
    the exact run bindings first saved with them, so every binding is current. Catalogs
    without generations (test doubles) accept every binding; in active mode a binding
    whose generation cannot be derived is never current.
    """
    served=getattr(catalog,'reference_generation_id',None)
    if served is None or not getattr(catalog,'active_generation',None): return True
    try: return reference_generation.generation_of_binding(binding)==served
    except reference_generation.ReferenceError: return False

def superseded_source(source_id):
    """A factor id the active (non-legacy) generation no longer serves."""
    return bool(getattr(catalog,'active_generation',None) and reference_generation.model_of_source_id(source_id)
        and source_id not in catalog.factors)

def superseded(detail,status=409,**extra):
    return Problem(status,'REFERENCE_GENERATION_SUPERSEDED',detail,**extra)

def reload_gate(tx):
    if reference_generation.read_gate(tx):
        raise Problem(503,'REFERENCE_RELOAD_IN_PROGRESS','Reference data is being reloaded. Retry shortly.')

def preload_catalog(composer=None):
    """Cold-load the catalog before a write transaction opens: the cold load reads the active
    generation through the application pool, which must never nest inside a pooled write
    transaction. A loaded catalog does no I/O. Failures surface later with their usual precedence."""
    if composer is not None and not (composer.get('source_gap') or composer.get('eaggl_anchors')): return
    load=getattr(catalog,'load',None)
    if not load: return
    try: load()
    except Exception: pass

def freeze_draft_bindings(tx,draft_id,owner,composer,*,new=False):
    """An unchanged selection retains the exact run bindings first saved with it,
    while its reference generation is still served; otherwise it is re-validated.
    new: the draft is created in this transaction, so there is no prior binding to read and the caller inserts
    the returned binding with it."""
    gap=catalog.selected(composer['source_gap']) if composer['source_gap'] else None
    if composer['eaggl_anchors'] and hasattr(catalog,'load'): catalog.load()
    previous=None if new else tx.get('draft_binding',draft_id)
    previous=previous['data'].get('selections',{}) if previous else {}
    selections={}; gate_checked=False; suggestions={}   # automatic anchors share one suggestion: read and parse it once
    for selection in composer['eaggl_anchors']:
        reference=selection['reference']; native=reference['source_id']; old=previous.get(native)
        stale=bool(old) and not current_binding(old['binding'])
        if old and old['reference']==reference and not stale:
            selections[native]=old
        else:
            # Anchor writes wait for a reload to finish; unchanged anchors stay editable.
            if not gate_checked: reload_gate(tx); gate_checked=True
            try:
                catalog.validate_composer(dict(composer,eaggl_anchors=[selection]))
            except Problem as error:
                if error.status==409 and error.code=='SOURCE_REVISION_CHANGED' and (stale or superseded_source(native)):
                    raise superseded('This anchor belongs to a superseded reference generation; select current factors.') from None
                raise
            selections[native]={'reference':reference,'record':catalog.factors[native],'binding':catalog.bindings[native]}
        identity=selection.get('suggestion_id')
        if identity and identity not in suggestions: suggestions[identity]=tx.get('suggestion',identity)
        suggestion=suggestions.get(identity) if identity else None
        if suggestion and native in suggestion['data']['hits']:
            selections[native]['retrieval']={k:v for k,v in suggestion['data'].items() if k!='hits'}|{'hit':suggestion['data']['hits'][native]}
    if len(selections)!=len(composer['eaggl_anchors']): raise Problem(422,'DUPLICATE_ANCHOR','Select each native mechanism once.')
    frozen={'dismech_import_id':catalog.dismech_import if gap else None,'source_gap':gap,'selections':selections}
    if not new: tx.put('draft_binding',draft_id,owner,frozen)
    return frozen

@app.get('/health')
@app.get('/healthz')
def health(): return {'status':'ok'}

@app.get('/internal/v1/admin/telemetry')
def admin_telemetry(request: Request):
    service_authority(request.headers.get('authorization'))
    decode_assertion(request.headers.get('x-reveal-admin-assertion', ''), 'admin_telemetry')
    from .telemetry import snapshot
    from fastapi.encoders import jsonable_encoder
    return JSONResponse(jsonable_encoder(snapshot(repo)), headers={'Cache-Control': 'private, no-store'})

def admin_database_authority(request: Request):
    service_authority(request.headers.get('authorization'))
    decode_assertion(request.headers.get('x-reveal-admin-assertion', ''), 'admin_telemetry')

@app.get('/internal/v1/admin/jobs/{job_id}', dependencies=[Depends(admin_database_authority)])
def admin_job(job_id: str, before: str | None = Query(None, max_length=12), limit: int = Query(100, ge=1, le=100)):
    from .admin_jobs import job_detail
    return JSONResponse(job_detail(repo, job_id, before=before, limit=limit), headers={'Cache-Control': 'private, no-store'})

@app.get('/internal/v1/admin/tables/{table}', dependencies=[Depends(admin_database_authority)])
def admin_table(table: str, limit: int = Query(25, ge=1, le=50), cursor: str | None = Query(None, max_length=8192),
                column: str | None = Query(None, max_length=128), operator: str = 'contains', q: str = Query('', max_length=256)):
    from .admin_database import inspect_table
    return JSONResponse(inspect_table(repo, table, limit=limit, cursor=cursor, column=column, operator=operator, q=q),
                        headers={'Cache-Control': 'private, no-store'})

@app.get('/internal/v1/admin/tables/{table}/row', dependencies=[Depends(admin_database_authority)])
def admin_row(table: str, key: str = Query(..., max_length=4096)):
    from .admin_database import inspect_row
    return JSONResponse(inspect_row(repo, table, key), headers={'Cache-Control': 'private, no-store'})

@app.get('/internal/v1/admin/tables/{table}/cell', dependencies=[Depends(admin_database_authority)])
def admin_cell(table: str, key: str = Query(..., max_length=4096), column: str = Query(..., max_length=128),
               offset: int = Query(0, ge=0, le=100_000_000), digest: str | None = Query(None, max_length=64)):
    from .admin_database import inspect_cell
    return JSONResponse(inspect_cell(repo, table, key, column, offset, digest), headers={'Cache-Control': 'private, no-store'})

@app.get('/readyz')
@app.get('/health/ready')
def ready():
    from .api_keys import configuration as api_key_configuration
    from .admin_read_keys import configuration as admin_key_configuration
    api_key_configuration()
    admin_key_configuration()
    from . import readiness
    monitor = readiness.running()
    if monitor: database, sources = monitor.snapshot()  # computed off the request path; no I/O here
    else:
        database = repo.readiness(); sources = catalog.readiness()
        from .artifact_store import s3_enabled, store
        if s3_enabled(): store().check()
    # Health probes must never generate recurring Redis commands. Notifications
    # are observed from subscriber state; authoritative API writes survive a
    # notification outage and retain their durable publication intent.
    from .redis_notifications import configuration
    return {'status': 'ready', **database, 'sources': sources,
        'execution_mode': os.getenv('REVEAL_EXECUTION_MODE','box'), 'job_transport':jobs.transport(),
        'notifications':{'transport':configuration()[0], 'delivery':'pubsub', 'polling':False}}

def admin_scientific_list(request, kind, visibility, limit, cursor):
    from . import admin_read_keys, admin_science
    key_id = admin_read_keys.authenticate(request.headers.get('authorization'))
    cursor_owner = digest([key_id, os.getenv('REVEAL_ADMIN_READ_API_KEY_SHA256', '')])
    with repo.read_transaction() as tx:
        items, revisions = admin_science.collection(tx, kind, visibility)
        result = page(items, cursor_owner, limit, cursor, 'admin-science:'+kind+':'+visibility, snapshot_items=[items, revisions])
        if kind == 'account': admin_science.attribution(tx, result['items'])
    admin_science.audit(key_id, 'list_'+kind, len(result['items']))
    return result


def admin_scientific_detail(request, kind, identity):
    from . import admin_read_keys, admin_science
    key_id = admin_read_keys.authenticate(request.headers.get('authorization'))
    with repo.read_transaction() as tx:
        result = admin_science.detail(tx, kind, identity)
    admin_science.audit(key_id, 'get_'+kind, 1)
    return result


@app.get('/v1/admin/accounts')
def admin_accounts(request: Request, visibility: str = 'private', limit: int = Query(50, ge=1, le=100), cursor: str | None = None):
    return admin_scientific_list(request, 'account', visibility, limit, cursor)


@app.get('/v1/admin/accounts/{record_id}')
def admin_account(record_id: str, request: Request):
    return admin_scientific_detail(request, 'account', record_id)


@app.get('/v1/admin/explorations')
def admin_explorations(request: Request, visibility: str = 'private', limit: int = Query(50, ge=1, le=100), cursor: str | None = None):
    return admin_scientific_list(request, 'analysis_outcome', visibility, limit, cursor)


@app.get('/v1/admin/explorations/{record_id}')
def admin_exploration(record_id: str, request: Request):
    return admin_scientific_detail(request, 'analysis_outcome', record_id)


# Async handlers read the request body on the event loop and run validation, catalog loads and every
# transaction in the threadpool: one blocking statement here would stall every other request in the process.
@app.post('/internal/v1/principals/anonymous', status_code=201)
async def provision(request: Request):
    service_authority(request.headers.get('authorization')); body = await request.json()
    def run():
        validate(body,'AnonymousProvisionInput',True)
        with repo.transaction() as tx:
            def create():
                cutoff=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat().replace('+00:00','Z')
                count=sum(1 for r in tx.list('principal') if r['data'].get('created_at','')>=cutoff and r['data']['me']['principal_kind']=='anonymous')
                if count>=int(os.getenv('REVEAL_ANONYMOUS_PROVISIONS_PER_HOUR','100')):
                    raise Problem(429,'ANONYMOUS_QUOTA_EXCEEDED','Anonymous workspace creation is temporarily rate limited.')
                me = fresh_principal('anonymous'); tx.put('principal', me['user_id'], me['user_id'], {'me':me,'retired':False,'created_at':now()})
                return {k:me[k] for k in ('user_id','principal_kind','workspace_expires_at')}
            return idempotent(tx,'gateway','anonymous',request.headers.get('idempotency-key'),body,create)
    return await run_in_threadpool(run)

def resolve_identity(tx, body, preferred=None):
    identity = digest([body['issuer'],body['subject']]); existing = tx.get('identity',identity)
    if existing: return existing['data']['user_id']
    me = fresh_principal('registered',body)
    if preferred: me['user_id'] = preferred
    tx.put('principal',me['user_id'],me['user_id'],{'me':me,'retired':False})
    tx.put('identity',identity,me['user_id'],{'user_id':me['user_id'],'issuer':body['issuer'],'subject':body['subject']})
    return me['user_id']

@app.post('/internal/v1/principals/resolve')
async def resolve(request: Request):
    service_authority(request.headers.get('authorization')); body = await request.json()
    def run():
        validate(body,'VerifiedIdentityInput',True)
        proof=request.headers.get('x-reveal-anonymous-session')
        source=decode_assertion(proof,'anonymous_session') if proof else None
        with repo.transaction() as tx:
            preferred=None
            if source:
                row=tx.get('principal',source.get('sub',''))
                if not row or row['data']['retired'] or row['data']['me']['principal_kind']!='anonymous' or row['data']['me']['workspace_expires_at']<=now():
                    raise Problem(401,'INVALID_IDENTITY_PROOF','The source workspace proof is no longer active.')
                preferred=source['sub']
            return {'user_id':resolve_identity(tx,body,preferred),'principal_kind':'registered'}
    return await run_in_threadpool(run)

@app.post('/internal/v1/principals/claim')
async def claim_workspace(request: Request):
    service_authority(request.headers.get('authorization')); body = await request.json()
    def run():
        validate(body,'WorkspaceClaimInput',True)
        source = decode_assertion(body['anonymous_session_assertion'],'anonymous_session')
        target = decode_assertion(body['verified_login_assertion'],'verified_identity')
        profile = target.get('verified_identity',{}); validate(profile,'VerifiedIdentityInput',True)
        with repo.transaction() as tx:
            def perform():
                row = tx.get('principal',source.get('sub',''))
                if not row or row['data']['retired'] or row['data']['me']['principal_kind']!='anonymous' or row['data']['me']['workspace_expires_at']<=now():
                    raise Problem(409,'PRINCIPAL_ALREADY_CLAIMED','The source workspace is no longer claimable.')
                target_id = resolve_identity(tx,profile,source['sub']); transferred = target_id != source['sub']
                if transferred:
                    tx.transfer(source['sub'],target_id); row['data']['retired']=True
                    tx.put('principal',source['sub'],source['sub'],row['data'])
                result={'mode':'transferred' if transferred else 'upgraded','user_id':target_id,'principal_kind':'registered','source_retired':transferred,'historical_attribution_unchanged':True}
                tx.put('transfer',uid(),target_id,{'source':source['sub'],'target':target_id,'occurred_at':now(),'result':result})
                return result
            # Do not retain raw signed proofs in audit/idempotency payloads.
            safe={'source':source.get('sub'),'target':digest([profile['issuer'],profile['subject']]),'consent':body['consent']}
            return idempotent(tx,'gateway','claim',request.headers.get('idempotency-key'),safe,perform)
    return await run_in_threadpool(run)

# Workspace reads never take the global write fence: one consistent snapshot authorizes and reads, so they
# neither queue behind writers nor serialize against each other. /v1/me is exactly one principal row.
@app.get('/v1/me')
def me(request: Request):
    with repo.single_read() as tx: return principal(tx,request.headers.get('authorization'))

def filter_gaps(items,kind,status,disease_id):
    if disease_id and ':' in disease_id and not disease_id.startswith(('http:','https:','urn:')):
        try: disease_id=catalog.runtime.resolver({}).expand(disease_id)
        except ValueError: raise Problem(422,'INVALID_QUERY','Unknown disease ontology prefix.')
    return [x for x in items if (not kind or x['object']['gap_kind']==kind) and
        (not status or (x['source']['status'] or 'UNSPECIFIED')==status) and
        (not disease_id or disease_id in x['object'].get('about_entities',[]))]

@app.get('/v1/knowledge-gaps/search')
def search_gaps(request:Request,q: str='', limit: int=20, mode: str='fuzzy',kind:str|None=None,status:str|None=None,cursor:str|None=None,source:str='dismech',disease_id:str|None=None,scope:str='public'):
    if mode not in ('lexical','fuzzy'): raise Problem(503,'SEARCH_MODE_UNAVAILABLE','Knowledge-gap discovery currently supports lexical and fuzzy modes; no semantic gap index is configured.')
    catalog.load(); items=catalog.search_gaps(q,len(catalog.gaps),mode)
    allowed={g['object']['id'] for g in filter_gaps([x['gap'] for x in items],kind,status,disease_id)}
    items=[x for x in items if x['gap']['object']['id'] in allowed]
    owner,viewer,accounts,values=gap_discovery(request,scope,[item['gap'] for item in items])
    counts=counts_by_gap(accounts); observed=now()
    items=[{**item,'gap':{**counted_gap(item['gap'],counts,owner,observed),'votes':values[('gap',item['gap']['object']['id'])]}} for item in items]
    return {**page(items,viewer or owner,limit,cursor,digest([q,mode,kind,status,disease_id,scope]),snapshot_items=gap_snapshot(items,getattr(catalog,'dismech_import',None))),'search':catalog.provenance(q,mode)}

def optional_identity(tx,request):
    authorization=request.headers.get('authorization')
    return principal(tx,authorization)['user_id'] if authorization is not None else None

def discovery_scope(scope):
    if scope not in ('public','workspace'): raise Problem(422,'INVALID_QUERY','Choose public or workspace discovery.')

def discovery(tx,request,scope='public', *, attribution=False):
    """One principal read: owner, registered vote viewer, the accounts the scope may count and, for public
    scope, the publication rows they came from (for account_summaries)."""
    authorization=request.headers.get('authorization')
    me=principal(tx,authorization) if authorization is not None else None
    owner=me['user_id'] if me else None
    viewer=owner if me and me['principal_kind']=='registered' else None
    if scope=='public':
        published=publication.published(tx)
        return '',viewer,publication.public_accounts(tx,published),published
    if owner is None: raise Problem(401,'SESSION_EXPIRED','Continue with a registered or anonymous session to browse your workspace.')
    return owner,viewer,visible_accounts(tx,owner,attribution=attribution),None

def gap_discovery(request,scope,gaps):
    """Counts and every gap's vote state from one read snapshot, after scope validation."""
    discovery_scope(scope)
    with repo.read_transaction() as tx:
        owner,viewer,accounts,_=discovery(tx,request,scope)
        return owner,viewer,accounts,votes.states(tx,[('gap',gap['object']['id']) for gap in gaps],viewer)

@app.get('/v1/knowledge-gaps')
def list_gaps(request:Request,limit: int=20,cursor:str|None=None,kind:str|None=None,status:str|None=None,source:str='dismech',disease_id:str|None=None,scope:str='public',sort:str='accounts'):
    if sort not in ('accounts','votes'): raise Problem(422,'INVALID_QUERY','Choose accounts or votes sorting.')
    catalog.load(); items=filter_gaps([x['gap'] for x in catalog.search_gaps('',len(catalog.gaps))],kind,status,disease_id)
    owner,viewer,accounts,values=gap_discovery(request,scope,items)
    counts=counts_by_gap(accounts); observed=now(); seed=browse_seed(cursor)
    items=[{**counted_gap(gap,counts,owner,observed),'votes':values[('gap',gap['object']['id'])]} for gap in items]
    items.sort(key=lambda gap:((-gap['votes']['score'],-resolved_mechanism_count(gap),-gap['scientific_accounts']['count'])
        if sort=='votes' else (-gap['scientific_accounts']['count'],-resolved_mechanism_count(gap)))+
        (digest([seed,gap['object']['id']]),gap['source']['source_id'],gap['object']['id']))
    return page(items,viewer or owner,limit,cursor,digest(['gaps',kind,status,disease_id,scope,sort]),snapshot_items=gap_snapshot(items,getattr(catalog,'dismech_import',None)),seed=seed)

def listing_scope(parts,reference_state):
    """Cursor scope; the default `all` keeps the pre-archive scope digest."""
    return digest(parts if reference_state=='all' else [*parts,reference_state])

@app.get('/v1/knowledge-gaps/{gap_id}/accounts')
def gap_accounts(gap_id:str,request:Request,limit:int=20,cursor:str|None=None,source_revision:str|None=None,scope:str='public',reference_state:str='all'):
    gap=catalog.gap(gap_id)
    if source_revision and source_revision!=gap['source']['source_revision']: raise Problem(409,'SOURCE_REVISION_CHANGED','The exact requested source revision is unavailable.')
    discovery_scope(scope)
    with repo.read_transaction() as tx:
        owner,viewer,accounts,published=discovery(tx,request,scope,attribution=True)
        items=by_reference_state([item for item in accounts if item['account']['question']==gap['object']['id']],reference_state)
        items=votes.account_summaries(tx,items,viewer,published=published)
    return page(items,viewer or owner,limit,cursor,listing_scope(['gap-accounts',gap['object']['id'],source_revision,scope],reference_state))

@app.get('/v1/knowledge-gaps/{gap_id}/outcomes')
def gap_outcomes(gap_id:str,request:Request,limit:int=20,cursor:str|None=None,source_revision:str|None=None,scope:str='public',reference_state:str='all'):
    if scope not in ('public','workspace'): raise Problem(422,'INVALID_QUERY','Choose public or workspace discovery.')
    gap=catalog.gap(gap_id)
    if source_revision and source_revision!=gap['source']['source_revision']: raise Problem(409,'SOURCE_REVISION_CHANGED','The exact requested source revision is unavailable.')
    with repo.read_transaction() as tx:
        user=optional_identity(tx,request)
        if scope=='workspace' and user is None: raise Problem(401,'SESSION_EXPIRED','A workspace session is required.')
        items=by_reference_state(analysis_outcomes.listing(tx,gap['object']['id'],user,scope),reference_state)
    return page(items,user if scope=='workspace' else '',limit,cursor,listing_scope(['gap-outcomes',gap['object']['id'],source_revision,scope],reference_state))

def read_vote(kind,identity,request):
    preload_catalog()
    with repo.read_transaction() as tx:
        viewer=votes.viewer(tx,request.headers.get('authorization'))
        canonical,_=votes.target(tx,catalog,kind,identity)
        return votes.states(tx,[(kind,canonical)],viewer)[(kind,canonical)]

async def write_vote(kind,identity,request):
    body=await request.json(); validate(body,'VoteInput')
    def save():
        preload_catalog()
        with repo.transaction() as tx:
            user=votes.voter(tx,request.headers.get('authorization'))
            canonical,gap_id=votes.target(tx,catalog,kind,identity)
            return idempotent(tx,user,'vote:'+kind+':'+canonical,request.headers.get('idempotency-key'),body,
                lambda:votes.change(tx,kind,canonical,gap_id,user,body['vote']))
    return await asyncio.to_thread(save)

@app.get('/v1/knowledge-gaps/{gap_id}/vote')
def get_gap_vote(gap_id:str,request:Request): return read_vote('gap',gap_id,request)

@app.post('/v1/knowledge-gaps/{gap_id}/vote')
async def set_gap_vote(gap_id:str,request:Request): return await write_vote('gap',gap_id,request)

@app.get('/v1/accounts/{account_id}/vote')
def get_account_vote(account_id:str,request:Request): return read_vote('account',account_id,request)

@app.post('/v1/accounts/{account_id}/vote')
async def set_account_vote(account_id:str,request:Request): return await write_vote('account',account_id,request)

@app.get('/v1/knowledge-gaps/{gap_id:path}')
def get_gap(gap_id: str,request:Request,source_revision:str|None=None,scope:str='public'):
    gap=catalog.gap(gap_id)
    if source_revision and source_revision!=gap['source']['source_revision']: raise Problem(409,'SOURCE_REVISION_CHANGED','The exact requested source revision is unavailable.')
    owner,_,accounts,values=gap_discovery(request,scope,[gap])
    return {**counted_gap(gap,counts_by_gap(accounts),owner,now()),'votes':values[('gap',gap['object']['id'])]}

@app.get('/v1/mechanisms/search')
def search_mechanisms(q: str='', mode: str='hybrid', limit: int=20,source:str='all',model:str='cfde-inc-v2',cursor:str|None=None):
    catalog.load()
    if mode=='semantic' and source!='eaggl': raise Problem(503,'SEARCH_MODE_UNAVAILABLE','Pure semantic search requires the EAGGL embedding corpus; use lexical, fuzzy or hybrid for DisMech.')
    semantic=source!='dismech' and mode in ('semantic','hybrid')
    index=pinned_index() if semantic else {}
    # Items are MechanismHit {record, ranking}: per-hit retrieval provenance (query vectors, up to 1,000 candidate
    # hits each) stays server-side, so it is neither built nor returned.
    items=[{'record':item['record'],'ranking':item['ranking']} for item in catalog.search_factors(q,mode,len(catalog.factors),
        provenance=False,**({'_index':index['index']} if index else {}))] if source in ('eaggl','all') else []
    if source in ('dismech','all'):
        from difflib import SequenceMatcher
        contexts=[]
        for record in catalog.dismech_catalog().values():
            text=(record['object']['name']+' '+record['object']['description']).casefold()
            score=sum(word in text for word in q.casefold().split())/max(1,len(q.split()))
            if mode=='fuzzy' and not score:
                score=max((SequenceMatcher(None,q.casefold(),word).ratio() for word in text.split()),default=0)
                if score<0.7: score=0
            if score: contexts.append({'record':record,'ranking':{'value':score,'metric':'fuzzy_similarity' if mode=='fuzzy' else 'lexical_rank','rank':1}})
        contexts.sort(key=lambda item:(-item['ranking']['value'],item['record']['source_id']))
        for rank,item in enumerate(contexts,1): item['ranking']['rank']=rank
        if mode=='hybrid':
            for item in items+contexts: item['ranking'].update(value=1/(60+item['ranking']['rank']),metric='reciprocal_rank_fusion')
        items+=contexts; items.sort(key=lambda item:(-item['ranking']['value'],item['record']['source_id']))
        for rank,item in enumerate(items,1): item['ranking']['rank']=rank
    provenance=catalog.provenance(q,mode,semantic,**index)
    if source=='all': provenance['corpus_snapshot']=digest([catalog.dismech_import,catalog.mapping_run])
    # Order, ranking and exact record identities, never the per-item retrieval provenance (up to 1,000 candidate hits each).
    snapshot=['mechanism-snapshot-v2',provenance,getattr(catalog,'reference_generation_id',None),[[item['record'].get('source'),item['record']['source_id'],
        item['record'].get('source_revision'),(item['record'].get('object') or {}).get('id'),item['ranking']] for item in items]]
    return {**page(items,limit=limit,cursor=cursor,scope=digest(['mechanisms',q,mode,source,model]),snapshot_items=snapshot),'search':provenance}

def pinned_index():
    """{'index': the served semantic index}, resolved once per request so its hits, context inputs and provenance
    share one snapshot; {} for catalogs without one (test stand-ins)."""
    resolve=getattr(catalog,'retrieval_index',None)
    index=resolve() if resolve else None
    return {'index':index} if index is not None else {}

@app.post('/v1/mechanisms/suggest')
async def suggest(request: Request):
    body=await request.json()
    # Source preparation and durable audit writes can both touch Aurora. Keep
    # the entire synchronous path off the event loop, including cached results.
    return await asyncio.to_thread(build_suggestions,body)

def build_suggestions(body):
    validate(body,'SuggestInput'); gap=catalog.selected(body['source_gap'])
    # Manual anchors must come from the served generation, so a draft never mixes generations.
    if any(superseded_source(anchor['source_id']) for anchor in body['manual_eaggl_anchors']):
        raise superseded('A kept anchor belongs to a superseded reference generation; remove it and select current factors.')
    exclude=set(body['dismissed_source_ids']) | {s['source_id'] for s in body['manual_eaggl_anchors']}
    contexts=[(a['target']['source_id'],catalog.mechanisms[a['target']['source_id']]['object']['description']) for a in gap['attachments'] if a['target'] and a['target']['source_id'] in catalog.mechanisms]
    query=body.get('subquery') or ' '.join(text for _,text in contexts) or gap['object']['text']
    if body.get('subquery'): contexts=[('mechanism_subquery',body['subquery'])]
    if not contexts: contexts=[(gap['object']['id'],gap['object']['text'])]
    remaining=max(0,5-len(body['manual_eaggl_anchors']))
    precomputed=not bool(body.get('subquery'))
    disease_candidates=getattr(catalog,'disease_factors',lambda *_: [])(gap,remaining,exclude)
    excluded=exclude | {item['record']['source_id'] for item in disease_candidates}
    index=pinned_index()  # the response's search provenance always needs it, so no request resolves it more than once
    items=disease_candidates+catalog.suggest_factors(contexts,body.get('mode','semantic'),remaining-len(disease_candidates),excluded,precomputed=precomputed,**index)
    for rank,item in enumerate(items,1):
        item['ranking']['rank']=rank
        item.setdefault('reason','Similarity to selected source context; inspect for relevance, not biological support.')
    context_provenance=catalog.context_embedding_provenance(contexts,**index) if precomputed else {'context_embedding_origin':'user_subquery'}
    suggestion_id=uid()
    # A new uuid-keyed audit row, committed before its id is returned; only a later fenced draft write reads it.
    repo.append('suggestion',suggestion_id,'catalog',{'mode':body.get('mode','semantic'),'embedding_run_id':catalog.embedding_run,'mapping_run_id':catalog.mapping_run,
        **context_provenance,'hits':{x['record']['source_id']:{'ranking':x['ranking'],'matched_context_ids':x['contexts'],'reason':x['reason'],
            **({'retrieval':x['retrieval']} if 'retrieval' in x else {}),
            **({'context_similarities':x['context_similarities']} if 'context_similarities' in x else {})} for x in items}})
    return {'suggestion_id':suggestion_id,'automatic_anchors':[{'factor':x['record'],'ranking':x['ranking'],'matched_context_ids':x['contexts'],'reason':x['reason']} for x in items],
        'automatic_target_count':5,'search':catalog.provenance(query,body.get('mode','semantic'),True,**index),'limitations':['Retrieval similarity is not evidence of biological support.']}

def archived_factor(source_id):
    """(superseded, frozen snapshot|None) for a factor id the served generation does not serve.

    Legacy mode has no superseded generation, so unknown ids stay 404 as before.
    A same-model id with no captured snapshot is simply unknown.
    """
    if not superseded_source(source_id): return False,None
    lookup=getattr(catalog,'archived_for_source',None); archived=lookup(source_id) if lookup else None
    return bool(archived) or reference_generation.model_of_source_id(source_id)!=getattr(catalog,'model',None),archived

@app.get('/v1/reference-factors/{archive_id}')
def reference_factor(archive_id:str):
    """Frozen snapshot of a factor referenced by archived work (public reference data)."""
    lookup=getattr(catalog,'archived_reference_factor',None)
    record=lookup(archive_id) if lookup and re.fullmatch('[a-f0-9]{64}',archive_id) else None
    if not record: raise Problem(404,'NOT_FOUND','The archived reference factor is unavailable.')
    return record

@app.get('/v1/factors/{source_id}')
def get_factor_detail(source_id:str,generation_id:str|None=None,source_revision:str|None=None):
    from .factor_details import factor_detail
    return factor_detail(catalog,source_id,generation_id=generation_id,source_revision=source_revision)

@app.get('/v1/factor-loadings')
def get_factor_loadings(source_id:str,kind:str='gene',metric:str='joint',sort:str='loading',q:str='',limit:int=200,offset:int=0,
                        generation_id:str|None=None,source_revision:str|None=None,gnomad_import_id:str|None=None):
    from .factor_details import factor_loadings
    return factor_loadings(catalog,source_id,kind=kind,metric=metric,sort=sort,q=q,limit=limit,offset=offset,
                           generation_id=generation_id,source_revision=source_revision,gnomad_import_id=gnomad_import_id)

@app.get('/v1/catalog/gene-sets/{gene_set_id}')
def get_catalog_gene_set(gene_set_id:str,generation_id:str|None=None):
    from .factor_details import gene_set_detail
    return gene_set_detail(catalog,gene_set_id,generation_id=generation_id)

@app.get('/v1/mechanisms/{source_id:path}')
def get_mechanism(source_id:str,source_revision:str|None=None):
    catalog.load(); record=catalog.factors.get(source_id) or catalog.mechanisms.get(source_id)
    if not record and source_id.startswith('dismech:'): record=catalog.dismech_catalog().get(source_id)
    if not record:
        gone,archived=archived_factor(source_id)
        if gone: raise superseded('This factor belongs to a superseded reference generation.',410,archived_reference_factor=archived)
        raise Problem(404,'NOT_FOUND','The mapped mechanism is unavailable.')
    if source_revision and source_revision!=record['source_revision']: raise Problem(409,'SOURCE_REVISION_CHANGED','The exact requested source revision is unavailable.')
    return record

@app.get('/v1/drafts')
def list_drafts(request:Request,limit:int=50,cursor:str|None=None):
    # A pure read: saved() already hides every temporary editor, expired or not. Expiry deletes and exploration
    # repointing run under the write fence in managed reconciliation (workflow_routes.sweep).
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; rows=tx.list('draft',user)
    return page([dict(r['data'],owner_user_id=user) for r in rows if user_inputs.saved(r['data'])],user,limit,cursor,'drafts')

@app.post('/v1/drafts',status_code=201)
async def create_draft(request:Request):
    body=await request.json()
    def run():
        validate(body,'DraftCreate'); preload_catalog(body['composer'])
        key=request.headers.get('idempotency-key')
        with repo.transaction() as tx:
            reads=[('draft',body.get('source_draft_id'))]+([(reference_generation.CONTROL_KIND,reference_generation.CONTROL_ID)] if body['composer'].get('eaggl_anchors') else [])
            user=principal_for(tx,request.headers.get('authorization'),'draft',key,reads)['user_id']
            def create():
                draft={'id':uid(),'owner_user_id':user,'version':1,'composer':body['composer'],'created_at':now(),'updated_at':now()}
                lifecycle=body.get('lifecycle','saved')
                draft.update(lifecycle=lifecycle,expires_at=user_inputs.expiration() if lifecycle=='temporary' else None)
                if body.get('source_draft_id'):
                    source=user_inputs.available(owned(tx,'draft',body['source_draft_id'],user)['data'])
                    if not user_inputs.saved(source): raise Problem(422,'SOURCE_DRAFT_NOT_SAVED','Only a saved draft can supply editor lineage.')
                    source_version=body.get('source_draft_version',source['version'])
                    if source_version>source['version']:
                        raise Problem(409,'SOURCE_DRAFT_VERSION_CONFLICT','The supplied source draft revision does not exist.',current_version=source['version'])
                    draft.update(source_draft_id=source['id'],source_draft_version=source_version)
                user_inputs.resolve(tx,user,body['composer'])
                if 'name' in body: draft['name']=body['name'].strip()
                binding=freeze_draft_bindings(tx,draft['id'],user,body['composer'],new=True)
                return draft,[('draft_binding',draft['id'],user,binding),('draft',draft['id'],user,draft)]   # a fresh uuid
            return idempotent(tx,user,'draft',key,body,create,staged=True)
    return await run_in_threadpool(run)

@app.get('/v1/drafts/{draft_id}')
def get_draft(draft_id:str,request:Request):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; row=owned(tx,'draft',draft_id,user)
    return dict(user_inputs.available(row['data']),owner_user_id=user)

@app.post('/v1/drafts/{draft_id}/cfde-assessments', status_code=202)
async def create_cfde_assessment(draft_id: str, request: Request):
    from . import cfde_assessment
    from starlette.concurrency import run_in_threadpool
    body = await request.json(); validate(body, 'CfdeAssessmentInput')
    result = await run_in_threadpool(cfde_assessment.start, repo, catalog,
        request.headers.get('authorization'), draft_id, body, request.headers.get('idempotency-key'))
    return JSONResponse(result, status_code=202, headers={
        'Location': request.url.path.rstrip('/')+'/'+result['id'], 'Retry-After': '2'})

@app.get('/v1/drafts/{draft_id}/cfde-assessments/{assessment_id}')
async def get_cfde_assessment(draft_id: str, assessment_id: str, request: Request, wait: str = '0'):
    """wait=N (0-20 s, optional) long-polls a pending receipt instead of polling it every 2 s."""
    from . import cfde_assessment
    if not (wait.isascii() and wait.isdigit() and int(wait) <= cfde_assessment.MAX_WAIT_SECONDS):
        raise Problem(422,'INVALID_QUERY','Invalid query parameter: wait')
    return await cfde_assessment.poll(repo, catalog, request.headers.get('authorization'), draft_id, assessment_id, int(wait))

@app.patch('/v1/drafts/{draft_id}')
async def patch_draft(draft_id:str,request:Request):
    body=await request.json()
    def run():
        validate(body,'DraftPatch')
        if 'composer' in body: preload_catalog(body['composer'])
        key=request.headers.get('idempotency-key')
        with repo.transaction() as tx:
            reads=[('draft',draft_id)]+([('draft_binding',draft_id),(reference_generation.CONTROL_KIND,reference_generation.CONTROL_ID)] if 'composer' in body else [])
            user=principal_for(tx,request.headers.get('authorization'),'draft:'+draft_id,key,reads)['user_id']
            def update():
                row=owned(tx,'draft',draft_id,user); draft=user_inputs.available(row['data'])
                if draft['version']!=body['expected_version']: raise Problem(409,'VERSION_CONFLICT','This draft changed in another tab.',current_version=draft['version'])
                if 'composer' in body:
                    user_inputs.resolve(tx,user,body['composer'])
                    freeze_draft_bindings(tx,draft_id,user,body['composer'])
                    draft['composer']=body['composer']
                if 'name' in body: draft['name']=body['name'].strip()
                if body.get('lifecycle')=='saved':
                    if not draft.get('name'): raise Problem(422,'DRAFT_NAME_REQUIRED','Name the draft before saving it.')
                    draft.update(lifecycle='saved',expires_at=None,saved_at=now())
                elif not user_inputs.saved(draft): draft['expires_at']=user_inputs.expiration()
                draft.update(version=draft['version']+1,updated_at=now(),owner_user_id=user)
                tx.put('draft',draft_id,user,draft,expected=row['version']); return draft
            return idempotent(tx,user,'draft:'+draft_id,key,body,update)
    return await run_in_threadpool(run)

@app.delete('/v1/drafts/{draft_id}')
async def delete_draft(draft_id:str,request:Request):
    body=await request.json()
    def run():
        validate(body,'DraftDelete')
        with repo.transaction() as tx:
            user=principal(tx,request.headers.get('authorization'))['user_id']
            def remove():
                draft=owned(tx,'draft',draft_id,user)['data']
                if draft['version']!=body['expected_version']: raise Problem(409,'VERSION_CONFLICT','This draft changed in another tab. Refresh before deleting it.',current_version=draft['version'])
                tx.remove('draft',draft_id); tx.remove('draft_binding',draft_id)
                # Editable state may be removed; submitted evidence and results remain immutable.
                remaining=[r['data'] for r in tx.list('draft',user) if user_inputs.saved(r['data'])]
                remaining.sort(key=lambda d:(d['updated_at'],d['id']),reverse=True)
                for row in tx.list('exploration',user):
                    exploration=row['data']
                    if exploration.get('draft_id')!=draft_id: continue
                    gap_id=exploration['source_gap']['id']
                    exploration['draft_id']=next((d['id'] for d in remaining if (d['composer'].get('source_gap') or {}).get('id')==gap_id),None)
                    tx.put('exploration',row['id'],user,exploration,expected=row['version'])
                return {'id':draft_id,'deleted':True}
            return idempotent(tx,user,'delete-draft:'+draft_id,request.headers.get('idempotency-key'),body,remove)
    return await run_in_threadpool(run)

@app.post('/v1/uploads',status_code=201)
async def initiate_upload(request:Request):
    body=await request.json()
    def run():
        validate(body,'UploadCreate')
        with repo.transaction() as tx:
            user=principal(tx,request.headers.get('authorization'))['user_id']
            result=idempotent(tx,user,'uploads',request.headers.get('idempotency-key'),body,lambda:user_inputs.initiate(tx,user,body))
            current=owned(tx,'upload',result['upload']['id'],user)['data']
            return {'upload':user_inputs.public_upload(current),'transfer':user_inputs.transfer(current)}
    return await run_in_threadpool(run)

@app.get('/v1/uploads')
def list_uploads(request:Request,draft_id:str):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; owned(tx,'draft',draft_id,user)
        return {'items':[user_inputs.public_upload(row['data']) for row in tx.list('upload',user)
            if row['data']['draft_id']==draft_id and row['data']['status']!='removed']}

@app.get('/v1/uploads/{upload_id}')
def get_upload(upload_id:str,request:Request):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']
        return user_inputs.public_upload(owned(tx,'upload',upload_id,user)['data'])

@app.post('/v1/uploads/{upload_id}/content')
async def upload_content(upload_id:str,request:Request):
    if user_inputs.s3_enabled(): raise Problem(404,'NOT_FOUND','Local upload transport is disabled.')
    raw=bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw)>user_inputs.MAX_FILE_BYTES*4//3+100: raise Problem(413,'UPLOAD_TOO_LARGE','Upload exceeds 8 MB.')
    def run():
        try: body=json.loads(raw)
        except ValueError: raise Problem(422,'INVALID_UPLOAD','Invalid upload body.') from None
        with repo.transaction() as tx:
            user=principal(tx,request.headers.get('authorization'))['user_id']
            return user_inputs.stage_local(tx,user,upload_id,body.get('content_base64'))
    return await run_in_threadpool(run)

@app.post('/v1/uploads/{upload_id}/complete')
def complete_upload(upload_id:str,request:Request):
    # Each transaction opens and closes in this one threadpool call; extraction runs between them, outside both.
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']
        before=owned(tx,'upload',upload_id,user); value=before['data']
    failure=None
    try: completed=user_inputs.complete(value)
    except Problem as error:
        failure=error; completed={**value,'status':'failed','error':error.detail}
    with repo.transaction() as tx:
        row=owned(tx,'upload',upload_id,user)
        if row['data']['status']=='ready': return user_inputs.public_upload(row['data'])
        if row['version']!=before['version']: raise Problem(409,'UPLOAD_CHANGED','The upload changed while processing.')
        tx.put('upload',upload_id,user,completed,expected=before['version'])
    if failure: raise failure
    return user_inputs.public_upload(completed)

@app.delete('/v1/uploads/{upload_id}')
def remove_upload(upload_id:str,request:Request):
    with repo.transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; row=owned(tx,'upload',upload_id,user)
        if user_inputs.referenced(tx,upload_id): raise Problem(409,'UPLOAD_IN_USE','Remove the attachment from saved inputs first. Submitted inputs are retained.')
        value={**row['data'],'status':'removed'}; tx.put('upload',upload_id,user,value)
        return user_inputs.public_upload(value)

@app.get('/v1/uploads/{upload_id}/download')
def download_upload(upload_id:str,request:Request):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; value=owned(tx,'upload',upload_id,user)['data']
        if value['status']!='ready': raise Problem(404,'NOT_FOUND','Upload is unavailable.')
    data=user_inputs.read(value['storage'])
    return Response(data,media_type=value['media_type'],headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(value['filename'],safe=''),
        'X-Content-Type-Options':'nosniff','Cache-Control':'private, no-store'})

@app.get('/v1/research-requests')
def list_requests(request:Request,limit:int=50,cursor:str|None=None):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; rows=tx.list('request',user)
    return page([dict(r['data'],owner_user_id=user) for r in rows],user,limit,cursor,'requests')

@app.get('/v1/research-requests/{request_id}')
def get_request(request_id:str,request:Request):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']
        return dict(owned(tx,'request',request_id,user)['data'],owner_user_id=user)

@app.post('/v1/jobs',status_code=202)
async def create_job(request:Request,background:BackgroundTasks):
    body=await request.json(); validate(body,'JobCreate')
    result = await asyncio.to_thread(create_job_transaction,body,request.headers.get('authorization'),request.headers.get('idempotency-key'))
    deliver_after_response(background,result['id'],control=False)   # a new job has no cancellation intent
    return result

def deliver_after_response(background,job_id,*,control):
    """The committed intent is the delivery guarantee (managed reconciliation repairs it), so the reply never waits
    for Workflow. Background tasks start only after the response body is sent, and graceful shutdown awaits them."""
    if jobs.transport()=='workflow': background.add_task(deliver_workflow_intents,job_id,control=control)

async def deliver_workflow_intents(job_id,*,control=True):
    from .workflow_routes import dispatch_job
    try:
        async with asyncio.timeout(30):
            await dispatch_job(repo, job_id, control=control)
    except Exception as error:
        # The job and dispatch intent already committed. Managed reconciliation
        # repairs delivery; never misrepresent a committed submission as failed.
        import logging
        logging.getLogger('reveal.workflow').warning('Dispatch deferred to reconciliation (%s)', type(error).__name__)

def freeze_research_request(tx, identity, body, *, retrieval_mode=None, write=True):
    """Freeze one scientific request for either hosted or local execution. write=False returns its two new rows
    as well instead of writing them, so the caller inserts them with the rest of its batch; retrieval_mode is
    frozen into the request before its only write."""
    user = identity['user_id']
    draft=user_inputs.available(owned(tx,'draft',body['draft_id'],user)['data'])
    if draft['version']!=body['draft_version']: raise Problem(409,'VERSION_CONFLICT','Save the current draft before submitting.',current_version=draft['version'])
    composer=draft['composer']
    if not composer['source_gap'] or not composer['eaggl_anchors']: raise Problem(422,'ANCHOR_REQUIRED','Select a source question and at least one mechanism anchor.')
    catalog.selected(composer['source_gap'])
    saved=owned(tx,'draft_binding',draft['id'],user)['data']; gap=saved['source_gap']
    if not all(current_binding(saved['selections'][s['reference']['source_id']]['binding']) for s in composer['eaggl_anchors']):
        raise superseded('This draft uses factors from a superseded reference generation; start a new analysis on this gap with current factors.')
    contexts=[a['target'] for a in gap['attachments'] if a['target']]
    # Freeze selected attachment bodies once. Old requests retain their original references.
    pinned_context=[]
    for reference in contexts[:10]:
        record=getattr(catalog,'mechanisms',{}).get(reference['source_id'])
        if record and record['source_revision']==reference['source_revision']:
            pinned_context.append(deepcopy(record))
    document={'knowledge_gaps':[gap['object']], 'mechanisms':[saved['selections'][s['reference']['source_id']]['record']['object'] for s in composer['eaggl_anchors']]}
    frozen={'id':uid(),'owner_user_id':user,'source_draft_id':draft['id'],'source_draft_version':draft['version'],'composer':composer,'question_id':gap['object']['id'],
        'document':document,'attribution':{'user_id':user,'person_id':None,'display_name':identity['display_name'],'orcid':identity['orcid'],'orcid_authenticated':identity['orcid_authenticated'],'observed_at':now(),'principal_kind':identity['principal_kind']},
        'submitted_at':now(),'linked_dismech_context':contexts,'user_inputs':user_inputs.resolve(tx,user,composer)}
    if draft.get('source_draft_id'):
        frozen.update(originating_saved_draft_id=draft['source_draft_id'],originating_saved_draft_version=draft['source_draft_version'])
    if retrieval_mode: frozen['retrieval_mode']=retrieval_mode
    request_binding={'dismech_import_id':saved['dismech_import_id'],'source_gap':gap,
        'pinned_dismech_context':pinned_context,
        'anchors':[saved['selections'][s['reference']['source_id']]['binding'] for s in composer['eaggl_anchors']],
        'retrieval':{s['reference']['source_id']:saved['selections'][s['reference']['source_id']].get('retrieval') for s in composer['eaggl_anchors']}}
    request_binding['anchor_display']=analysis_outcomes.anchor_display(composer,request_binding,saved)
    rows=[('request',frozen['id'],user,frozen),('request_binding',frozen['id'],user,request_binding)]   # a fresh uuid
    if not write: return frozen, request_binding, rows
    for row in rows: tx.put(*row)
    return frozen, request_binding


def create_job_transaction(body,authorization,idempotency_key):
    analysis=body.get('kind')=='analysis'
    if analysis: preload_catalog()
    with repo.transaction() as tx:
        reads=[(reference_generation.CONTROL_KIND,reference_generation.CONTROL_ID),('draft',body.get('draft_id')),('draft_binding',body.get('draft_id'))] if analysis else []
        identity=principal_for(tx,authorization,'job',idempotency_key,reads); user=identity['user_id']
        def create():
            mine=tx.list('job',user)
            active=[r for r in mine if r['data']['status'] not in jobs.TERMINAL]
            maximum=int(os.getenv('REVEAL_MAX_ACTIVE_JOBS','2'))
            if len(active)>=maximum: raise Problem(429,'ANONYMOUS_QUOTA_EXCEEDED' if identity['principal_kind']=='anonymous' else 'JOB_QUOTA_EXCEEDED','Wait for an active job to finish or stop it.')
            if identity['principal_kind']=='anonymous' and body['kind']=='analysis':
                cutoff=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat().replace('+00:00','Z')
                today=[r for r in mine if r['data']['kind']=='analysis' and r['data']['created_at']>=cutoff]
                if len(today)>=int(os.getenv('REVEAL_ANONYMOUS_ANALYSES_PER_DAY','5')): raise Problem(429,'ANONYMOUS_QUOTA_EXCEEDED','This anonymous workspace has reached its daily analysis allowance.')
            if body['kind']=='paragraph':
                # Research statements need no reference data: allowed on archived accounts and during reloads.
                account=owned(tx,'account',body['account_id'],user)
                job,rows=jobs.new(user,'paragraph',account_id=body['account_id'],inputs=body)
                jobs.update_paragraph_state(tx,job)
                return job,rows
            reload_gate(tx)
            # The request is frozen as progressive and written once, with every other new row, in one statement.
            frozen,binding,rows=freeze_research_request(tx,identity,body,retrieval_mode='progressive',write=False)
            job,queued=jobs.new(user,'analysis',request_id=frozen['id'],inputs=body)
            from . import research_hosted
            return job,rows+queued+research_hosted.rows(job,frozen,binding)
        return idempotent(tx,user,'job',idempotency_key,body,create,staged=True)

@app.get('/v1/jobs')
def list_jobs(request:Request,limit:int=20,cursor:str|None=None,kind:str|None=None,status:str|None=None,research_request_id:str|None=None):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; rows=tx.list('job',user)
    items=[dict(r['data'],owner_user_id=user) for r in rows if (not kind or r['data']['kind']==kind) and (not status or r['data']['status']==status) and (not research_request_id or r['data']['research_request_id']==research_request_id)]
    items.sort(key=lambda x:x['id']); items.sort(key=lambda x:x['created_at'],reverse=True)
    return page(items,user,limit,cursor,digest(['jobs',kind,status,research_request_id]))

@app.get('/v1/jobs/{job_id}')
def get_job(job_id:str,request:Request):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; return dict(owned(tx,'job',job_id,user)['data'],owner_user_id=user)

@app.post('/v1/jobs/{job_id}/cancel')
async def cancel_job(job_id:str,request:Request,background:BackgroundTasks):
    result = await asyncio.to_thread(cancel_job_transaction, job_id, request.headers.get('authorization'))
    deliver_after_response(background,job_id,control=True)
    return result

def cancel_job_transaction(job_id, authorization):
    with repo.transaction() as tx:
        user=principal(tx,authorization)['user_id']; job=dict(owned(tx,'job',job_id,user)['data'],owner_user_id=user)
        return jobs.cancel(tx,job)

@app.post('/v1/jobs/{job_id}/retry-review',status_code=202)
async def retry_job_review(job_id:str,request:Request,background:BackgroundTasks):
    body=await request.json(); validate(body,'ReviewRetryInput')
    result = await asyncio.to_thread(retry_review_transaction,job_id,body,request.headers.get('authorization'),request.headers.get('idempotency-key'))
    deliver_after_response(background,job_id,control=False)   # only cancellation writes workflow_control
    return result

def retry_review_transaction(job_id,body,authorization,idempotency_key):
    from .review_retry import enqueue_review
    with repo.transaction() as tx:
        identity=principal(tx,authorization); user=identity['user_id']
        job=dict(owned(tx,'job',job_id,user)['data'],owner_user_id=user)
        def retry():
            active=[r for r in tx.list('job',user) if r['data']['status'] not in jobs.TERMINAL]
            if len(active)>=int(os.getenv('REVEAL_MAX_ACTIVE_JOBS','2')):
                raise Problem(429,'JOB_QUOTA_EXCEEDED','Wait for an active job to finish or stop it.')
            return enqueue_review(tx,job,body['expected_last_event_id'])
        return idempotent(tx,user,'/v1/jobs/'+job_id+'/retry-review',idempotency_key,body,retry)

def read_events(job_id,authorization,after,limit=100):
    with repo.read_transaction() as tx:
        user=principal(tx,authorization)['user_id']; job=owned(tx,'job',job_id,user)['data']
        # Event keys encode the job and padded numeric sequence; use the primary
        # key range instead of scanning this owner's complete event history.
        rows=tx.execute('SELECT payload FROM reveal_records WHERE kind=%s AND owner_id=%s AND id>%s AND id<%s ORDER BY id LIMIT %s',
            ('event',user,job_id+':'+str(after).zfill(12),job_id+';',limit+1)).fetchall()
        items=[json.loads(row[0]) for row in rows]
        return {'items':items[:limit],'next_after':items[:limit][-1]['id'] if items else str(after),'terminal':job['status'] in jobs.TERMINAL and len(items)<=limit}

@app.get('/v1/jobs/{job_id}/events')
async def get_events(job_id:str,request:Request,after:str='0',limit:int=100):
    if request.headers.get('last-event-id') and 'after' in request.query_params and request.headers['last-event-id']!=after:
        raise Problem(400,'INVALID_CURSOR','Last-Event-ID and after must agree.')
    try: cursor=int(request.headers.get('last-event-id') or after)
    except ValueError: raise Problem(400,'INVALID_CURSOR','Event cursor must be numeric.')
    if cursor<0: raise Problem(400,'INVALID_CURSOR','Event cursor must be nonnegative.')
    stream='text/event-stream' in request.headers.get('accept','')
    from .redis_notifications import closing
    if stream and closing(): raise Problem(503,'SERVICE_UNAVAILABLE','The service is restarting; reconnect shortly.')
    authorization=request.headers.get('authorization'); initial=await asyncio.to_thread(read_events,job_id,authorization,cursor,limit)
    if not stream: return initial
    from .workspace_events import job_event_stream
    return StreamingResponse(job_event_stream(repo, request, job_id, authorization, cursor, limit, read_events, initial),
        media_type='text/event-stream', headers={'Cache-Control':'private, no-store','X-Accel-Buffering':'no'})

@app.get('/v1/me/explorations')
def explorations(request:Request,limit:int=50,cursor:str|None=None):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']; rows=tx.list('exploration',user)
    return page([r['data'] for r in rows],user,limit,cursor,'explorations')

@app.post('/v1/me/explorations')
async def record_exploration(request:Request):
    body=await request.json(); validate(body,'ExplorationInput')
    def record():
        # catalog.selected may reload the catalog after a reference cutover: keep it off the event loop.
        gap=catalog.selected(body['source_gap'])
        with repo.transaction() as tx:
            user=principal(tx,request.headers.get('authorization'))['user_id']
            if body.get('draft_id'): owned(tx,'draft',body['draft_id'],user)
            row={'source_gap':body['source_gap'],'knowledge_gap':gap['object'],'last_explored_at':now(),'draft_id':body.get('draft_id'),
                'scientific_accounts':dict(gap['scientific_accounts'],scope='owner_exact_gap')}
            tx.put('exploration',digest([user,gap['object']['id']]),user,row); return row
    return await asyncio.to_thread(record)

@app.get('/v1/accounts')
def accounts(request:Request,limit:int=20,cursor:str|None=None,gap_id:str|None=None,q:str=Query('',max_length=200),scope:str='workspace',sort:str='recent',reference_state:str='all'):
    from .workspace_search import normalize, filter_summaries
    if scope not in ('public','workspace'): raise Problem(422,'INVALID_QUERY','Choose public or workspace discovery.')
    if sort not in ('recent','votes'): raise Problem(422,'INVALID_QUERY','Choose recent or votes sorting.')
    query=normalize(q)
    with repo.read_transaction() as tx:
        user,viewer,items,published=discovery(tx,request,scope,attribution=True)
        items=filter_summaries([item for item in items if not gap_id or item['account']['question']==gap_id],query,'account')
        items=votes.account_summaries(tx,items,viewer,published=published)
        # Keep workspace ordering unchanged; public recency is the actual
        # publication date, with deterministic identity ties across pages.
        items.sort(key=lambda item:item['account']['id'])
        items.sort(key=lambda item:(item['publication'].get('published_at') or item['created_at']) if scope=='public' else item['created_at'],reverse=True)
        if sort=='votes': items.sort(key=lambda item:(item.get('votes') or {}).get('score',0),reverse=True)
        # Current work leads the default combined list; each reference group
        # retains the requested vote/recency ordering, before pagination.
        items=by_reference_state(items,reference_state)
        return page(items,(viewer or '') if scope=='public' else user,limit,cursor,listing_scope(['accounts',gap_id,query,scope,sort],reference_state))

@app.get('/v1/leaderboard')
def community_leaderboard(request:Request,view:str='researchers',sort:str|None=None,evidence:str='all',limit:int=20,cursor:str|None=None):
    from . import leaderboard
    sort=leaderboard.options(view,sort,evidence)
    if view!='datasets': evidence='all'
    with repo.read_transaction() as tx:
        optional_identity(tx,request)  # Invalid supplied credentials never downgrade to public.
        projection=leaderboard.build(tx,evidence)
    items=leaderboard.ranking(projection,view,sort)
    result=page(items,limit=limit,cursor=cursor,scope=digest(['leaderboard',view,sort,evidence]),snapshot_items=projection['revision'])
    return {**result,**projection['metadata'],'view':view,'sort':sort,'evidence':evidence}

@app.get('/v1/leaderboard/{view}/{entry_id}/records')
def leaderboard_records(view:str,entry_id:str,request:Request,metric:str='accounts',evidence:str='all',limit:int=20,cursor:str|None=None):
    from . import leaderboard
    leaderboard.options(view,evidence=evidence,metric=metric)
    if view!='datasets': evidence='all'
    with repo.read_transaction() as tx:
        optional_identity(tx,request)
        projection=leaderboard.build(tx,evidence)
    entry,items=leaderboard.details(projection,view,entry_id,metric)
    result=page(items,limit=limit,cursor=cursor,scope=digest(['leaderboard-records',view,entry_id,metric,evidence]),snapshot_items=projection['revision'])
    return {**result,**projection['metadata'],'view':view,'id':entry_id,'metric':metric,'evidence':evidence,'entry':entry,'total':len(items)}

@app.get('/v1/accounts/{dapper_id}/publication')
def account_publication(dapper_id:str,request:Request):
    with repo.read_transaction() as tx:
        user=optional_identity(tx,request)
        if user and tx.get('account',digest([user,dapper_id])):
            owned(tx,'account',dapper_id,user)
            return publication.state(tx,user,dapper_id,can_manage=True)
        row,_=publication.find(tx,dapper_id,'account')
        return publication.state(tx,row['owner'],dapper_id)

@app.post('/v1/accounts/{dapper_id}/publication')
async def update_publication(dapper_id:str,request:Request):
    body=await request.json(); validate(body,'PublicationInput')
    def save():
        with repo.transaction() as tx:
            user=publication_principal(tx,request.headers.get('authorization'),body['visibility'])['user_id']
            return idempotent(tx,user,'publication:'+dapper_id,request.headers.get('idempotency-key'),body,
                lambda:publication.change(tx,user,dapper_id,body['visibility'],body['expected_version']))
    return await asyncio.to_thread(save)

@app.get('/v1/analysis-outcomes')
def workspace_outcomes(request:Request,limit:int=20,cursor:str|None=None,q:str=Query('',max_length=200),reference_state:str='all'):
    from .workspace_search import normalize, filter_summaries
    query=normalize(q)
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']
        items=by_reference_state(analysis_outcomes.listing(tx,owner=user,scope='workspace'),reference_state)
    return page(filter_summaries(items,query,'outcome'),user,limit,cursor,listing_scope(['workspace-outcomes',query],reference_state))

@app.get('/v1/jobs/{job_id}/outcome')
def job_outcome(job_id:str,request:Request):
    with repo.read_transaction() as tx:
        user=principal(tx,request.headers.get('authorization'))['user_id']
        owned(tx,'job',job_id,user)
        reference=owned(tx,'analysis_outcome_by_job',job_id,user)
        return analysis_outcomes.get(tx,reference['data']['id'],user)

@app.get('/v1/analysis-outcomes/{outcome_id}')
def analysis_outcome(outcome_id:str,request:Request):
    with repo.read_transaction() as tx:
        return analysis_outcomes.get(tx,outcome_id,optional_identity(tx,request))

@app.get('/v1/analysis-outcomes/{outcome_id}/publication')
def outcome_publication(outcome_id:str,request:Request):
    with repo.read_transaction() as tx:
        return analysis_outcomes.get(tx,outcome_id,optional_identity(tx,request))['publication']

@app.post('/v1/analysis-outcomes/{outcome_id}/publication')
async def update_outcome_publication(outcome_id:str,request:Request):
    body=await request.json(); validate(body,'PublicationInput')
    def save():
        with repo.transaction() as tx:
            user=publication_principal(tx,request.headers.get('authorization'),body['visibility'])['user_id']
            return idempotent(tx,user,'outcome-publication:'+outcome_id,request.headers.get('idempotency-key'),body,
                lambda:analysis_outcomes.change(tx,outcome_id,user,body['visibility'],body['expected_version']))
    return await asyncio.to_thread(save)

def scientific(identity,request,kind='object'):
    with repo.read_transaction() as tx:
        user=optional_identity(tx,request); public=None; reference=None; publication_record=None
        if kind not in ('account','object','paragraph'):
            if user is None: raise Problem(401,'SESSION_EXPIRED','Continue with a registered or anonymous session.')
            row=owned(tx,kind,identity,user)['data']
        else:
            try:
                if not user: raise Problem(404,'NOT_FOUND','Scientific resource unavailable.')
                key=digest([user,identity])
                keys=[(kind,key),('object_document',key),('scientific_dependencies',key)]
                if kind=='account': keys.append(('publication',key))
                records=tx.get_records(keys)
                row=require_owned(tx,kind,identity,user,records.get((kind,key)),
                    dependency=records.get(('scientific_dependencies',key)))['data']
                reference=records.get(('object_document',key))
                publication_record=records.get(('publication',key))
            except Problem as error:
                if error.status!=404: raise
                public,snapshot=publication.find(tx,identity,kind)
                from .acceptance import object_envelope
                row=object_envelope(snapshot['document'],identity,snapshot['citation_metadata'],snapshot['artifacts'])
                if kind=='account': row['research_statement']=snapshot['summary']['research_statement']
        # Outdated-reference state lives beside the immutable result: summary.archive.
        archive=((snapshot['summary'] if public is not None else row.get('summary')) or {}).get('archive') if kind=='account' else None
        result=row.get('result',row)
        if 'document' not in result: return result
        checksum=request.query_params.get('payload_sha256')
        if checksum and not any(p['object_id']==identity and p['payload_sha256']==checksum for p in result['payloads']):
            raise Problem(404,'PAYLOAD_NOT_FOUND','The exact requested payload observation is unavailable.')
        from .acceptance import object_envelope
        document=result['document']; metadata=result['citation_metadata']; artifacts={a['file']['id']:a for a in result['artifacts']}
        if public is None and kind not in ('account','object','paragraph'):
            reference=tx.get('object_document',digest([user,identity]))
        if reference and reference['owner'] != user: reference=None
        document_sha=reference['data']['sha256'] if reference else None
        if public is None and document_sha is None:
            # Rows accepted before the direct document index retain their exact
            # source observation and full immutable document under this owner.
            observations=[r['data'] for r in tx.list('object_observation',user) if r['data']['object_id']==identity]
            if observations: document_sha=observations[-1]['document_sha256']
        if document_sha:
            stored=tx.get('scientific_document',digest([user,document_sha]))
            if stored and stored['owner']==user:
                document=stored['data']['document']; metadata=stored['data'].get('citation_metadata',metadata); artifacts=stored['data'].get('artifact_access',artifacts)
        withheld_dependencies = False
        if public is not None:
            document=snapshot['document']; metadata=snapshot['citation_metadata']; artifacts=snapshot['artifacts']
        elif user:
            from .scientific_reuse import readable_document
            before_visibility = document
            document = readable_document(tx, user, document)
            withheld_dependencies = document != before_visibility
            visible = {n['id'] for rows in document.values() if isinstance(rows, list)
                for n in rows if isinstance(n, dict) and 'id' in n}
            metadata = [item for item in metadata if item['target_id'] in visible]
            artifacts = {key: item for key, item in artifacts.items() if key in visible}
        root_payload=next(p['payload_sha256'] for p in result['payloads'] if p['object_id']==identity)
        binding={'owner':user if public is None else 'publication:'+public['id']+':'+str(public['data']['version']),
            'kind':kind,'root':identity,'payload':root_payload,'document':digest(document)}
        depth=int(request.query_params.get('max_depth','5')); maximum=int(request.query_params.get('max_nodes','250')); offset=0
        secret=os.getenv('REVEAL_GATEWAY_SECRET','').encode(); cursor=request.query_params.get('cursor')
        if cursor:
            try:
                encoded,signature=cursor.split('.')
                if not hmac.compare_digest(hmac.new(secret,encoded.encode(),hashlib.sha256).hexdigest(),signature): raise ValueError()
                state=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
                if any(state.get(key)!=value for key,value in binding.items()): raise ValueError()
                if ('max_depth' in request.query_params and depth!=state['depth']) or ('max_nodes' in request.query_params and maximum!=state['maximum']): raise ValueError()
                depth,maximum,offset=state['depth'],state['maximum'],state['offset']
                if not (type(offset) is int and offset>=0 and 0<=depth<=5 and 1<=maximum<=250): raise ValueError()
            except (ValueError,KeyError,TypeError): raise Problem(409,'CURSOR_EXPIRED','The provenance cursor belongs to another owner, filter or immutable payload; reload its first page.')
        def continuation(next_offset):
            state={**binding,'depth':depth,'maximum':maximum,'offset':next_offset}
            encoded=base64.urlsafe_b64encode(json.dumps(state,separators=(',',':')).encode()).decode().rstrip('=')
            return encoded+'.'+hmac.new(secret,encoded.encode(),hashlib.sha256).hexdigest()
        clipped=object_envelope(document,identity,metadata,artifacts,max_depth=depth,max_nodes=maximum,offset=offset,continuation=continuation)
        if withheld_dependencies:
            # Withdrawal is incomplete provenance, not an empty complete graph.
            # Never add newly unauthorized identities to the public missing list.
            clipped['coverage']['complete'] = False
        if 'research_statement' in result: clipped['research_statement']=result['research_statement']
        if 'fixture_origin' in result: clipped['fixture_origin']=result['fixture_origin']
        if kind=='account': clipped['publication']=publication.state(tx,public['owner'] if public else user,identity,
            can_manage=public is None,record=public if public else publication_record,account_result=result)
        if archive: clipped['archive']=archive
        return clipped

@app.get('/v1/accounts/{dapper_id}')
def account(dapper_id:str,request:Request): return scientific(dapper_id,request,'account')

@app.get('/v1/claims/{dapper_id}')
@app.get('/v1/objects/{dapper_id}')
@app.get('/v1/gene-sets/{dapper_id}')
def object_result(dapper_id:str,request:Request): return scientific(dapper_id,request)

@app.get('/v1/paragraphs/{dapper_id}')
def paragraph_result(dapper_id:str,request:Request): return scientific(dapper_id,request,'paragraph')

@app.get('/v1/citations/{dapper_id}')
def citation(dapper_id:str,request:Request,revision:int|None=None,format:str='native',locale:str='en-US'):
    from .citations import representation
    with repo.read_transaction() as tx:
        user=optional_identity(tx,request)
        try:
            if not user: raise Problem(404,'NOT_FOUND','Citation unavailable.')
            payload,media=representation(tx,user,dapper_id,format,revision,locale)
        except Problem as error:
            if error.status!=404: raise
            _,snapshot=publication.find(tx,dapper_id,'citation',revision)
            reader=publication.SnapshotReader(snapshot)
            payload,media=representation(reader,reader.user,dapper_id,format,revision,locale)
        accept=request.headers.get('accept','*/*')
        if '*/*' not in accept and media not in accept and 'application/json' not in accept: raise Problem(406,'NOT_ACCEPTABLE','The requested citation format does not match Accept.')
        return JSONResponse(payload,media_type=media) if isinstance(payload,(dict,list)) else Response(payload,media_type=media)

@app.get('/v1/jobs/{job_id}/evidence-package')
def evidence(job_id:str,request:Request): return scientific(job_id,request,'evidence')

@app.get('/v1/artifacts/{sha256}')
def artifact_bytes(sha256:str,request:Request):
    if not re.fullmatch('[a-f0-9]{64}',sha256): raise Problem(404,'NOT_FOUND','The source artifact is unavailable.')
    with repo.read_transaction() as tx:
        user=optional_identity(tx,request)
        try:
            if not user: raise Problem(404,'NOT_FOUND','Artifact unavailable.')
            row=owned(tx,'artifact',digest([user,sha256]),user)['data']
        except Problem as error:
            if error.status!=404: raise
            try:
                _,snapshot=publication.find(tx,sha256,'artifact'); row=snapshot['artifact_records'][sha256]
            except Problem as error:
                if error.status!=404: raise
                row=analysis_outcomes.published_artifact(tx,sha256)
    from .artifact_store import s3_enabled, store, StorageUnavailable
    if row.get('storage'):
        if row['storage'].get('sha256')!=sha256: raise Problem(503,'ARTIFACT_CHECKSUM_MISMATCH','Artifact identity differs.')
        try:
            url=store().download_url(row['storage'],row['file'].get('filename','source-artifact'),row['file'].get('mime_type','application/octet-stream'))
        except StorageUnavailable:
            raise Problem(503,'ARTIFACT_UNAVAILABLE','The retained source bytes are currently unavailable.')
        return RedirectResponse(url,status_code=307,headers={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer'})
    if s3_enabled(): raise Problem(404,'ARTIFACT_UNAVAILABLE','This legacy artifact has not been migrated to object storage.')
    path=Path(row['path']).resolve()
    if not path.is_relative_to(artifacts_root()): raise Problem(404,'NOT_FOUND','The source artifact is unavailable.')
    try: data=path.read_bytes()
    except (FileNotFoundError,PermissionError): raise Problem(404,'ARTIFACT_UNAVAILABLE','The retained source bytes are currently unavailable.')
    if hashlib.sha256(data).hexdigest()!=sha256: raise Problem(503,'ARTIFACT_CHECKSUM_MISMATCH','The source artifact failed checksum verification.')
    filename=quote(row['file'].get('filename','source-artifact'),safe='')
    return Response(data,media_type=row['file'].get('mime_type','application/octet-stream'),headers={'Content-Disposition':"attachment; filename*=UTF-8''"+filename,'X-Content-Type-Options':'nosniff','Cache-Control':'private, no-store'})

@app.get('/v1/paragraphs/{dapper_id}/export')
def export_paragraph(dapper_id:str,request:Request,format:str='markdown'):
    from .citations import export
    with repo.read_transaction() as tx:
        user=optional_identity(tx,request)
        try:
            if not user: raise Problem(404,'NOT_FOUND','Paragraph unavailable.')
            return export(tx,user,dapper_id,format)
        except Problem as error:
            if error.status!=404: raise
            _,snapshot=publication.find(tx,dapper_id,'paragraph'); reader=publication.SnapshotReader(snapshot)
            return export(reader,reader.user,dapper_id,format)

@app.post('/v1/citations/render')
async def render_citations(request:Request):
    from .citations import load_paragraph, render_paragraph
    body=await request.json()
    def run():
        # A read snapshot, never the write fence: rendering persists nothing.
        validate(body,'CitationRenderInput'); identity=body['paragraph_id']
        with repo.read_transaction() as tx:
            user=optional_identity(tx,request)
            try:
                if not user: raise Problem(404,'NOT_FOUND','Paragraph unavailable.')
                paragraph,records=load_paragraph(tx,user,identity)
            except Problem as error:
                if error.status!=404: raise
                _,snapshot=publication.find(tx,identity,'paragraph'); reader=publication.SnapshotReader(snapshot)
                paragraph,records=load_paragraph(reader,reader.user,identity)
        # The CSL engine runs after the pooled connection is returned.
        return render_paragraph(paragraph,records,identity,body.get('style','apa'),body.get('locale','en-US'))
    return await run_in_threadpool(run)


from .workspace_events import register as register_workspace_events
register_workspace_events(app, lambda: repo)
from .research_http import register as register_research
register_research(app, lambda: repo, freeze=freeze_research_request, preload=preload_catalog, reload_gate=reload_gate)
from .workflow_routes import mount_workflow
mount_workflow(app, repo)
from .vector_workflow import mount_vector_workflow
if jobs.transport() == 'workflow':
    mount_vector_workflow(app, repo)

_routes_lifespan = app.router.lifespan_context

@asynccontextmanager
async def background_lifespan(application):
    """The API process warms the catalog (REVEAL_CATALOG_WARMUP) and runs the readiness monitor
    (REVEAL_READINESS_MONITOR) off the request path; both default on, and tests turn them off."""
    from . import readiness
    async with _routes_lifespan(application) as state:
        warmup = getattr(catalog, 'start_warmup', None)
        if warmup and os.getenv('REVEAL_CATALOG_WARMUP', '1') != '0': warmup()
        monitor = readiness.start(lambda: repo, lambda: catalog) if os.getenv('REVEAL_READINESS_MONITOR', '1') != '0' else None
        try: yield state
        finally:
            if monitor: readiness.stop(monitor)

app.router.lifespan_context = background_lifespan

app = mount_service(app, os.getenv('SERVICE_PATH_PREFIX', ''))
