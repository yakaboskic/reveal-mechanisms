"""Private, frozen, one-completion research audits, independent of research jobs.

Only explicit creation dispatches inference. Reads, reconciliation and ambiguous
delivery never retry a potentially billed provider request.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import threading
from time import monotonic

import httpx

from .auth import Problem, owned, principal_with
from .cfde_assessment import Wakeups
from . import cfde_assessment_state, lightning_payload, reference_generation, user_inputs
from .repository import canonical, digest, now, uid

KIND = 'lightning_audit'
IDEMPOTENCY_KIND = 'lightning_audit_idempotency'
PENDING = ('preparing', 'assessing')
DEADLINE_SECONDS = 120
MAX_REQUEST_BYTES = 100_000
MAX_RESPONSE_BYTES = 128_000
MAX_ERROR_BYTES = 4096
MAX_OUTPUT_TOKENS = 3000
MAX_WAIT_SECONDS = 20
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='lightning-audit')
_slots = threading.BoundedSemaphore(4)
_running = set()
_running_lock = threading.Lock()
wakeups = Wakeups()


def enabled():
    return os.getenv('REVEAL_LIGHTNING_ENABLED', '').lower() in ('1', 'true', 'yes')


def require_enabled():
    if not enabled(): raise Problem(503, 'LIGHTNING_DISABLED', 'Lightning audits are not enabled on this server.')


def provider_key():
    key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not key: raise Problem(503, 'LIGHTNING_UNAVAILABLE', 'Lightning audits are not configured on this server.')
    return key


class ProviderProblem(Problem):
    """Sanitized public failure with bounded operational fields retained privately."""
    def __init__(self, status, code, detail, diagnostics):
        super().__init__(status, code, detail)
        self.diagnostics = diagnostics


async def _provider_diagnostics(response):
    diagnostics = {'http_status': response.status_code}
    request_id = response.headers.get('request-id', '')
    if re.fullmatch(r'req_[A-Za-z0-9_-]{1,120}', request_id): diagnostics['request_id'] = request_id
    # Error messages can echo source text or credentials. Never retain them.
    raw = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=1024):
        if len(raw) + len(chunk) > MAX_ERROR_BYTES: return diagnostics
        raw.extend(chunk)
    try: error_type = json.loads(raw).get('error', {}).get('type')
    except (ValueError, TypeError, AttributeError): return diagnostics
    if error_type in ('invalid_request_error', 'authentication_error', 'permission_error', 'not_found_error',
            'request_too_large', 'rate_limit_error', 'api_error', 'overloaded_error'):
        diagnostics['error_type'] = error_type
    return diagnostics


def serialize_request(payload):
    """Keep schema property order: the provider generates fields in this order."""
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def call_provider(payload, *, deadline):
    """One HTTP attempt, bounded bytes/time, with no tools, redirects or retries."""
    # The audit executor is synchronous. One private event loop lets cancellation
    # bound the entire response stream, not merely each socket read separately.
    return asyncio.run(_provider_request(payload, deadline=deadline))


async def _provider_request(payload, *, deadline):
    key = provider_key()
    remaining = deadline - monotonic()
    if remaining <= 0: raise Problem(504, 'LIGHTNING_TIMEOUT', 'The audit deadline expired.')
    try:
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(timeout=httpx.Timeout(remaining, connect=min(5, remaining)), follow_redirects=False,
                    headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'}) as client:
                async with client.stream('POST', 'https://api.anthropic.com/v1/messages', content=serialize_request(payload).encode()) as response:
                    if response.status_code != 200:
                        raise ProviderProblem(503, 'LIGHTNING_PROVIDER_UNAVAILABLE',
                            'The model service could not complete this audit. Start a new audit to try again.',
                            await _provider_diagnostics(response))
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES: raise ValueError()
        if monotonic() >= deadline: raise Problem(504, 'LIGHTNING_TIMEOUT', 'The audit deadline expired.')
        value = json.loads(raw)
        if not isinstance(value, dict): raise ValueError()
        return value
    except (TimeoutError, httpx.TimeoutException):
        raise ProviderProblem(504, 'LIGHTNING_TIMEOUT', 'The model request timed out. Start a new audit to try again.',
            {'category': 'timeout'}) from None
    except httpx.HTTPError:
        raise ProviderProblem(503, 'LIGHTNING_PROVIDER_UNAVAILABLE',
            'The model service could not complete this audit. Start a new audit to try again.',
            {'category': 'transport'}) from None
    except (ValueError, TypeError):
        raise Problem(503, 'LIGHTNING_RESPONSE_INVALID', 'The model returned an invalid or oversized response.') from None


def _error(code, detail, retryable=True):
    return {'code': code, 'detail': detail, 'retryable': retryable}


def _projection(data):
    value = deepcopy(data['public'])
    if value['status'] in PENDING and data['deadline_at'] <= now():
        value.update(status='interrupted', completed_at=data['deadline_at'], error=_error('LIGHTNING_INTERRUPTED',
            'This audit did not finish before its deadline. Start a new audit to try again.'))
    return value


def _alive(tx, owner):
    row = tx.get('principal', owner)
    me = row['data'].get('me', {}) if row else {}
    return bool(row and not row['data'].get('retired') and
        (not me.get('workspace_expires_at') or me['workspace_expires_at'] > now()))


def _index(owner, key):
    return digest([owner, 'lightning-audit', key])


def _replay(tx, owner, key, request_hash):
    row = tx.get(IDEMPOTENCY_KIND, _index(owner, key))
    if not row: return None
    if row['owner'] != owner or row['data']['request_sha256'] != request_hash:
        raise Problem(409, 'IDEMPOTENCY_CONFLICT', 'This key was already used for different Lightning audit inputs.')
    return _projection(owned(tx, KIND, row['data']['audit_id'], owner)['data'])


def _draft(tx, owner, body):
    draft = user_inputs.available(owned(tx, 'draft', body['draft_id'], owner)['data'])
    if draft['version'] != body['draft_version']:
        raise Problem(409, 'VERSION_CONFLICT', 'Save the current draft before starting an audit.', current_version=draft['version'])
    if not draft['composer'].get('source_gap') or not draft['composer'].get('eaggl_anchors'):
        raise Problem(422, 'ANCHOR_REQUIRED', 'Select a knowledge gap and at least one mechanism.')
    return draft


def start(repo, catalog, authorization, body, key, *, freeze, reload_gate):
    if not isinstance(body, dict) or set(body) != {'draft_id', 'draft_version'} or \
            not isinstance(body['draft_id'], str) or type(body['draft_version']) is not int or body['draft_version'] < 1:
        raise Problem(422, 'INVALID_REQUEST', 'Supply the draft ID and saved draft version.')
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise Problem(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Supply an Idempotency-Key of 8–128 characters.')
    request_hash = digest(body)
    keys = lambda owner: [(IDEMPOTENCY_KIND, _index(owner, key)), ('draft', body['draft_id']),
        ('draft_binding', body['draft_id']), (reference_generation.CONTROL_KIND, reference_generation.CONTROL_ID)]
    with repo.read_transaction() as tx:
        actor, _ = principal_with(tx, authorization, keys)
        owner = actor['user_id']
        replay = _replay(tx, owner, key, request_hash)
        if replay: return replay
        draft = _draft(tx, owner, body)
        reload_gate(tx)
        uploads = [('upload', identity) for identity in draft['composer'].get('upload_ids', [])]
        tx.get_records(uploads)
        user_inputs.resolve(tx, owner, draft['composer'])
    require_enabled()
    provider_key()
    # freeze() calls catalog.selected(). Warm its immutable snapshot before
    # entering the fence, just as the normal job admission path does.
    if hasattr(catalog, 'load'): catalog.load()
    reserved = _slots.acquire(blocking=False)
    if not reserved: raise Problem(429, 'LIGHTNING_BUSY', 'Lightning audits are busy. Try again shortly.')
    identity, attempt = uid(), uid()
    try:
        with repo.transaction() as tx:
            actor, _ = principal_with(tx, authorization, [*keys(owner), *uploads])
            owner = actor['user_id']
            replay = _replay(tx, owner, key, request_hash)
            if replay: return replay
            _draft(tx, owner, body)
            reload_gate(tx)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace('+00:00', 'Z')
            created = "JSON_EXTRACT(payload,'$.public.created_at')"
            if not tx.sqlite: created = 'JSON_UNQUOTE(' + created + ')'
            count = tx.execute("SELECT COUNT(*) FROM reveal_records WHERE kind=%s AND owner_id=%s AND " + created + ">=%s",
                (KIND, owner, cutoff)).fetchone()[0]
            if count >= (20 if actor['principal_kind'] == 'anonymous' else 100):
                raise Problem(429, 'LIGHTNING_QUOTA', 'This workspace has reached its daily Lightning audit allowance.')
            frozen, binding, rows = freeze(tx, actor, body, retrieval_mode='progressive', write=False)
            generation = reference_generation.generation_of_anchors(binding['anchors'])
            if not generation: raise Problem(503, 'SOURCE_NOT_READY', 'A retained reference generation is required.')
            timestamp = now()
            operation_deadline = (datetime.now(timezone.utc) + timedelta(seconds=DEADLINE_SECONDS)).isoformat().replace('+00:00', 'Z')
            continuation_deadline = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat().replace('+00:00', 'Z')
            if actor.get('workspace_expires_at'): continuation_deadline = min(continuation_deadline, actor['workspace_expires_at'])
            question = frozen['document']['knowledge_gaps'][0]
            public = {'id': identity, 'kind': KIND, 'status': 'preparing', 'research_request_id': frozen['id'],
                'source_draft_id': body['draft_id'], 'source_draft_version': body['draft_version'],
                'question': {'id': question['id'], 'text': question['text']}, 'created_at': timestamp, 'updated_at': timestamp,
                'completed_at': None, 'continuation_expires_at': continuation_deadline, 'reference_generation_id': generation,
                'result': None, 'coverage': None, 'evidence_references': [],
                'provenance': {'model': os.getenv('REVEAL_CLAUDE_MODEL', 'claude-sonnet-4-6'),
                               'prompt_version': lightning_payload.PROMPT_VERSION},
                'usage': None, 'error': None, 'continuations': []}
            data = {'public': public, 'control_owner': owner, 'attempt_id': attempt, 'deadline_at': operation_deadline,
                'frozen': deepcopy(frozen), 'binding': deepcopy(binding)}
            tx.insert_many([*rows, (KIND, identity, owner, data),
                (IDEMPOTENCY_KIND, _index(owner, key), owner, {'request_sha256': request_hash, 'audit_id': identity}),
                ('research_pin', frozen['id'], owner, {'id': frozen['id'], 'research_request_id': frozen['id'],
                    'generation_id': generation, 'state': 'active', 'expires_at': continuation_deadline})])
        try: _pool.submit(_run, repo, catalog, owner, identity, attempt)
        except Exception:
            _update(repo, owner, identity, attempt, status='failed', error=_error('LIGHTNING_DISPATCH_FAILED',
                'The audit could not start. Start a new audit to try again.'))
            return get(repo, authorization, identity)
        reserved = False
        return public
    finally:
        if reserved: _slots.release()


def get(repo, authorization, identity):
    with repo.read_transaction() as tx:
        actor, _ = principal_with(tx, authorization, [(KIND, identity)])
        return _projection(owned(tx, KIND, identity, actor['user_id'])['data'])


async def wait_get(repo, authorization, identity, wait=0):
    if type(wait) is not int or not 0 <= wait <= MAX_WAIT_SECONDS:
        raise Problem(422, 'INVALID_REQUEST', 'wait must be between 0 and 20 seconds.')
    seen = wakeups.mark()
    value = await asyncio.to_thread(get, repo, authorization, identity)
    if not wait or value['status'] not in PENDING: return value
    deadline = datetime.fromisoformat(value['created_at'].replace('Z', '+00:00')) + timedelta(seconds=DEADLINE_SECONDS)
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    await wakeups.wait(identity, seen, max(0, min(wait, remaining + .05)))
    return await asyncio.to_thread(get, repo, authorization, identity)


def listing(repo, authorization, *, paginate=None, limit=50, cursor=None):
    with repo.read_transaction() as tx:
        actor, _ = principal_with(tx, authorization, [])
        # History never hydrates frozen inputs or provider artifacts.
        rows = tx.execute("SELECT JSON_EXTRACT(payload,'$.public'),JSON_EXTRACT(payload,'$.deadline_at') "
            "FROM reveal_records WHERE kind=%s AND owner_id=%s", (KIND, actor['user_id'])).fetchall()
        def deadline(value):
            return json.loads(value) if isinstance(value, str) and value.startswith('"') else value
        items = [_projection({'public': json.loads(row[0]) if isinstance(row[0], str) else row[0],
                              'deadline_at': deadline(row[1])}) for row in rows]
    items.sort(key=lambda item: (item['created_at'], item['id']), reverse=True)
    if paginate: return paginate(items, actor['user_id'], limit, cursor, 'lightning-audits')
    return {'items': items, 'page': {'next_cursor': None, 'has_more': False, 'snapshot_id': digest(items)}}


def _update(repo, owner, identity, attempt, *, status, error=None, public_fields=None, expected_status=None, **stored):
    """Every worker write is fenced by live ownership, attempt, status and deadline."""
    with repo.transaction() as tx:
        tx.get_records([(KIND, identity), ('principal', owner)])
        row = tx.get(KIND, identity)
        if not row or row['owner'] != owner or not _alive(tx, owner): return False
        data = row['data']; public = data['public']
        if data['control_owner'] != owner or data['attempt_id'] != attempt or public['status'] not in PENDING or data['deadline_at'] <= now(): return False
        if expected_status is not None and public['status'] != expected_status: return False
        public.update(status=status, updated_at=now(), **(public_fields or {}))
        if status not in PENDING: public.update(completed_at=now(), error=error)
        data.update(stored)
        tx.put(KIND, identity, owner, data)
    wakeups.notify(identity)
    return True


def _parse_response(response, references):
    if response.get('stop_reason') == 'refusal':
        raise Problem(503, 'LIGHTNING_REFUSED', 'The model could not assess this evidence package.')
    if response.get('stop_reason') != 'end_turn':
        raise Problem(503, 'LIGHTNING_INCOMPLETE', 'The model did not finish a complete audit. Start a new audit to try again.')
    try:
        content = response['content']
        if not isinstance(content, list) or any(block.get('type') != 'text' for block in content): raise ValueError()
        value = json.loads(''.join(block['text'] for block in content))
        usage = response['usage']
        if any(type(usage.get(key)) is not int or usage[key] < 0 for key in ('input_tokens', 'output_tokens')): raise ValueError()
        if usage['output_tokens'] > MAX_OUTPUT_TOKENS: raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Problem(503, 'LIGHTNING_RESPONSE_INVALID', 'The model returned invalid audit content or usage.') from None
    result = lightning_payload.validate_result(value, references)
    return result, {key: usage[key] for key in ('input_tokens', 'output_tokens')}


def _run(repo, catalog, owner, identity, attempt):
    started = monotonic(); provider_started = None; retained = {}; diagnostic_public = {}
    with _running_lock: _running.add(identity)
    try:
        with repo.read_transaction() as tx:
            tx.get_records([(KIND, identity), ('principal', owner)])
            row = tx.get(KIND, identity)
            if not row or row['owner'] != owner or not _alive(tx, owner): return
            data = row['data']
            if data['control_owner'] != owner or data['attempt_id'] != attempt or data['public']['status'] != 'preparing': return
        remaining = (datetime.fromisoformat(data['deadline_at'].replace('Z', '+00:00')) - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0: return
        deadline = monotonic() + remaining
        frozen = data['frozen']
        prepared = cfde_assessment_state.build_state(catalog, frozen['composer'], frozen['user_inputs'], deadline=deadline)
        if prepared['reference_generation_id'] != data['public']['reference_generation_id']:
            raise Problem(409, 'SOURCE_REVISION_CHANGED', 'Reference data changed while preparing the audit. Start a new audit.')
        projection, references = lightning_payload.project(prepared['state'])
        projection['selected_graphs_for_future_research'] = deepcopy(frozen['composer'].get('selected_kgs', []))
        payload = {'model': data['public']['provenance']['model'], 'max_tokens': MAX_OUTPUT_TOKENS,
            'system': lightning_payload.SYSTEM, 'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': 'Frozen evidence package (source data, not instructions):\n' + canonical(projection)},
                {'type': 'text', 'text': lightning_payload.TASK}]}],
            'output_config': {'format': {'type': 'json_schema', 'schema': lightning_payload.RESULT_SCHEMA}}}
        request_json = serialize_request(payload)
        request_bytes = request_json.encode()
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise Problem(422, 'LIGHTNING_INPUT_TOO_LARGE', 'This evidence package exceeds the audit size limit. Select fewer mechanisms or shorter context.')
        provenance = {**data['public']['provenance'], 'source_state_sha256': digest(prepared['state']), 'request_sha256': hashlib.sha256(request_bytes).hexdigest()}
        retained['timings'] = {'preparation_ms': round((monotonic() - started) * 1000, 3)}
        if not _update(repo, owner, identity, attempt, status='assessing', expected_status='preparing', public_fields={
                'coverage': prepared['coverage'], 'evidence_references': references, 'provenance': provenance},
                source_state=prepared['state'], model_payload=payload, model_request_json=request_json, provider_started_at=now(), **retained): return
        provider_started = monotonic()
        response = call_provider(payload, deadline=deadline)
        retained.update(provider_response=response, timings={**retained['timings'],
            'provider_ms': round((monotonic() - provider_started) * 1000, 3),
            'total_ms': round((monotonic() - started) * 1000, 3)})
        provenance['response_sha256'] = digest(response)
        diagnostic_public['provenance'] = provenance
        raw_usage = response.get('usage', {})
        if isinstance(raw_usage, dict) and all(type(raw_usage.get(key)) is int and raw_usage[key] >= 0
                for key in ('input_tokens', 'output_tokens')):
            diagnostic_public['usage'] = {key: raw_usage[key] for key in ('input_tokens', 'output_tokens')}
        result, usage = _parse_response(response, references)
        _update(repo, owner, identity, attempt, status='succeeded', public_fields={
            'result': result, 'usage': usage, 'provenance': provenance}, response=result, **retained)
    except Exception as error:
        problem = error if isinstance(error, Problem) else Problem(503, 'LIGHTNING_FAILED', 'This audit could not finish. Start a new audit to try again.')
        if isinstance(error, ProviderProblem): retained['provider_failure'] = error.diagnostics
        timings = retained.setdefault('timings', {})
        timings['total_ms'] = round((monotonic() - started) * 1000, 3)
        if provider_started is not None: timings.setdefault('provider_ms', round((monotonic() - provider_started) * 1000, 3))
        else: timings.setdefault('preparation_ms', timings['total_ms'])
        try: _update(repo, owner, identity, attempt, status='failed', public_fields=diagnostic_public,
            error=_error(problem.code, problem.detail[:400], problem.status >= 500), **retained)
        except Exception: pass  # The persisted deadline exposes interrupted work after storage loss.
    finally:
        with _running_lock: _running.discard(identity)
        _slots.release()


def reconcile(repo):
    """Finalize abandoned receipts and explicitly release expired audit pins.

    Do not hold a read lease while waiting for the write fence. A live local
    assembler retains its pin; other processes get a source-query grace period.
    """
    with repo.read_transaction() as tx:
        # Thirty-second maintenance reads only compact control fields, never
        # every completed audit's evidence package/model payload.
        compact = "JSON_OBJECT('status',JSON_EXTRACT(payload,'$.public.status'),'deadline_at',JSON_EXTRACT(payload,'$.deadline_at')," \
            "'continuation_expires_at',JSON_EXTRACT(payload,'$.public.continuation_expires_at'))"
        records = tx.execute('SELECT id,owner_id,' + compact + ' FROM reveal_records WHERE kind=%s AND '
            "COALESCE(JSON_EXTRACT(payload,'$.pin_released'),false)=false", (KIND,)).fetchall()
        rows = [{'id': row[0], 'owner': row[1], 'data': json.loads(row[2]) if isinstance(row[2], str) else row[2]} for row in records]
        tx.get_records([('principal', row['owner']) for row in rows])
        candidates = [row['id'] for row in rows if
            (row['data']['status'] in PENDING and row['data']['deadline_at'] <= now()) or
            row['data']['status'] in ('failed', 'interrupted') or
            row['data']['continuation_expires_at'] <= now() or not _alive(tx, row['owner'])]
    changed = 0
    for identity in candidates:
        with repo.transaction(nowait=True) as tx:
            row = tx.get(KIND, identity)
            if not row: continue
            data = row['data']; public = data['public']; owner = row['owner']
            tx.get_records([('principal', owner), ('research_pin', public['research_request_id'])])
            alive = _alive(tx, owner)
            pending = public['status'] in PENDING
            if pending and (data['deadline_at'] <= now() or not alive):
                public.update(status='interrupted', updated_at=now(), completed_at=now(),
                    error=_error('LIGHTNING_INTERRUPTED', 'This audit was interrupted. Start a new audit to try again.'))
                tx.put(KIND, identity, owner, data); changed += 1
            with _running_lock: active = identity in _running
            grace = (datetime.fromisoformat(data['deadline_at'].replace('Z', '+00:00')) + timedelta(seconds=30)).isoformat().replace('+00:00', 'Z')
            release = public['continuation_expires_at'] <= now() or not alive or public['status'] in ('failed', 'interrupted')
            # assessing means source reads already ended. A crashed preparing worker
            # is safe once its bounded source socket deadline and grace have passed.
            source_done = bool(data.get('provider_started_at')) or grace <= now()
            if release and not active and source_done:
                pin = tx.get('research_pin', public['research_request_id'])
                if pin and pin['owner'] == owner and pin['data'].get('state') != 'released':
                    pin['data'].update(state='released', released_at=now())
                    tx.put('research_pin', public['research_request_id'], owner, pin['data']); changed += 1
                data['pin_released'] = True
                tx.put(KIND, identity, owner, data)
        wakeups.notify(identity)
    return changed
