"""Durable local research and scoped execution authority, independent of MCP sessions.

The database is authoritative. Short operations can be reclaimed on another API
replica after a lease expires; status reads resume pending work after a restart.
No paid worker/Box execution is created by this module.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from urllib.parse import urlsplit

from .auth import Problem, owned, require_owned
from .repository import FenceBusy, canonical, digest, now, uid
from .runtime_config import ROOT, CURRENT_DAPPER_SNAPSHOT, setting
from .runtime_metrics import measure
from . import user_inputs

POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='reveal-research')
RETAINERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix='reveal-seed-retention')
_scheduled_operations = set()
_scheduled_operations_lock = threading.Lock()
TERMINAL = {'succeeded', 'accepted', 'rejected', 'failed', 'cancelled'}
# Kinds that run authorize_commit in their own first read and again in the fence that commits their result.
SELF_AUTHORIZING = {'query', 'import', 'export'}
MAX_ARTIFACT_BYTES = 8_000_000


def json_text(tx, path):
    """SQL text of one top-level payload field; path is a code constant, never input."""
    field = "JSON_EXTRACT(payload, '$." + path + "')"
    return field if tx.sqlite else 'JSON_UNQUOTE(' + field + ')'


def _ordered(rows, identity, updated):
    """list() order: updated_at descending, then id ascending."""
    rows = sorted(rows, key=lambda row: row[identity])
    rows.sort(key=lambda row: row[updated], reverse=True)
    return rows


def records_where(tx, kind, owner, field, value):
    """This owner's rows of one kind whose payload field equals value, in list() order."""
    rows = tx.execute('SELECT id,owner_id,version,payload,updated_at FROM reveal_records '
        'WHERE kind=%s AND owner_id=%s AND '+json_text(tx, field)+'=%s', (kind, owner, value)).fetchall()
    return [{'id': row[0], 'owner': row[1], 'version': row[2], 'data': json.loads(row[3])} for row in _ordered(rows, 0, 4)]


def work_records(tx, kind, owner, work_id):
    """Read this work's children without transferring an owner's other captures.

    Export/query records can contain large evidence contexts. Filtering them in
    Python made every status poll download those unrelated payloads from MySQL.
    Ownership and the frozen local-work boundary both remain in the SQL filter.
    """
    return records_where(tx, kind, owner, 'local_work_id', work_id)


def work_children(tx, owner, work_ids, *, operations=('validate', 'submit')):
    """research_operation and research_access rows of several works in one owner-scoped read.

    Returns {kind: {work_id: rows}}, each list in work_records() order. operations limits
    research_operation rows to those kinds in SQL; None keeps every kind (pending detection).
    """
    work, kind = json_text(tx, 'local_work_id'), json_text(tx, 'kind')
    grouped = {'research_operation': {}, 'research_access': {}}; rows = []; ids = list(dict.fromkeys(work_ids))
    for offset in range(0, len(ids), 250):
        batch = ids[offset:offset + 250]
        sql = ('SELECT kind,id,owner_id,version,payload,updated_at FROM reveal_records WHERE kind IN (%s,%s) '
               'AND owner_id=%s AND '+work+' IN ('+','.join(['%s'] * len(batch))+')')
        args = ['research_operation', 'research_access', owner, *batch]
        if operations:
            sql += ' AND (kind=%s OR '+kind+' IN ('+','.join(['%s'] * len(operations))+'))'
            args += ['research_access', *operations]
        rows += tx.execute(sql, args).fetchall()
    for row in _ordered(rows, 1, 5):
        data = json.loads(row[4])
        grouped[row[0]].setdefault(data.get('local_work_id'), []).append(
            {'id': row[1], 'owner': row[2], 'version': row[3], 'data': data})
    return grouped


def operation_counts(tx, owner, work_id):
    """(all, pending) research_operation counts of one work, without transferring their payloads."""
    row = tx.execute('SELECT COUNT(*),COALESCE(SUM(CASE WHEN '+json_text(tx, 'state')+' IN (%s,%s) THEN 1 ELSE 0 END),0) '
        'FROM reveal_records WHERE kind=%s AND owner_id=%s AND '+json_text(tx, 'local_work_id')+'=%s',
        ('received', 'running', 'research_operation', owner, work_id)).fetchone()
    return int(row[0]), int(row[1])


def artifact_sizes(tx, owner, work_id):
    """{sha256: size_bytes} of a work's retained artifacts, without transferring their descriptors."""
    rows = tx.execute('SELECT '+json_text(tx, 'sha256')+','+json_text(tx, 'size_bytes')+' FROM reveal_records '
        'WHERE kind=%s AND owner_id=%s AND '+json_text(tx, 'local_work_id')+'=%s', ('research_artifact', owner, work_id)).fetchall()
    return {row[0]: int(row[1]) for row in rows}


def unsettled_operations(tx, owner, work_id):
    """How many of one work's research_operations are not terminal, without transferring their payloads."""
    state = json_text(tx, 'state')
    return int(tx.execute('SELECT COUNT(*) FROM reveal_records WHERE kind=%s AND owner_id=%s AND '+json_text(tx, 'local_work_id')+
        '=%s AND ('+state+' IS NULL OR '+state+' NOT IN ('+','.join(['%s'] * len(TERMINAL))+'))',
        ('research_operation', owner, work_id, *sorted(TERMINAL))).fetchone()[0])


def open_operations(tx):
    """Every unsettled research_operation in one kind-range read; settled payloads never cross the wire."""
    state = json_text(tx, 'state')
    rows = tx.execute('SELECT id,owner_id,payload FROM reveal_records WHERE kind=%s AND ('+state+' IS NULL OR '+state+
        ' NOT IN ('+','.join(['%s'] * len(TERMINAL))+'))', ('research_operation', *sorted(TERMINAL))).fetchall()
    return [{'id': row[0], 'owner': row[1], 'data': json.loads(row[2])} for row in sorted(rows)]


def needs_close(work, principal):
    """An open (or closed but unstamped) local work past its lifetime or whose principal is gone."""
    if work['state'] == 'closed' and work.get('closed_at'): return False
    return work['expires_at'] <= now() or not principal or bool(principal['data'].get('retired'))


def pending_operations(rows):
    """(id, lease_token) of received operations and running ones whose lease expired, to resume."""
    stamp = now(); pending = []
    for row in rows:
        state = row['data']['state']
        if state == 'received': pending.append((row['id'], None))
        elif state == 'running' and (row['data'].get('lease_until') or '') <= stamp:
            pending.append((row['id'], row['data'].get('lease_token')))
    return pending


def deadline(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def public_base():
    workflow = setting('REVEAL_WORKFLOW_URL', '')
    fallback = workflow.split('/internal/workflows/', 1)[0] if '/internal/workflows/' in workflow else 'http://127.0.0.1:18000'
    value = (setting('REVEAL_PUBLIC_API_URL') or fallback).rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise Problem(503, 'MCP_NOT_CONFIGURED', 'Configure a canonical REVEAL_PUBLIC_API_URL.')
    if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise Problem(503, 'MCP_NOT_CONFIGURED', 'Remote MCP access requires HTTPS.')
    return value


def valid_principal(tx, owner):
    return principal_record(tx.get('principal', owner))


def principal_record(row):
    if not row or row['data'].get('retired'):
        raise Problem(401, 'SESSION_EXPIRED', 'This workspace is no longer active.')
    me = row['data']['me']
    if me.get('workspace_expires_at') and me['workspace_expires_at'] <= now():
        raise Problem(401, 'SESSION_EXPIRED', 'This workspace has expired.')
    return me


def idempotent(tx, owner, operation, key, body, action):
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise Problem(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Supply an idempotency_key of 1–200 characters.')
    identity = digest([owner, 'research', operation, key]); checksum = digest(body)
    old = tx.get('research_idempotency', identity)
    if old:
        if old['data']['checksum'] != checksum:
            raise Problem(409, 'IDEMPOTENCY_CONFLICT', 'This retry key was used for different input.')
        return deepcopy(old['data']['result'])
    result = action()
    tx.put('research_idempotency', identity, owner, {'checksum': checksum, 'result': result})
    return result


def prompt(work_id):
    return (f'Use Reveal MCP to get_local_work with local_work_id={work_id}. Download the research seed '
        'and pinned authoring kit with get_research_package and get_artifact_download, and verify checksums. '
        'When present, read lightning_audit for the edited research direction and preliminary assessment; '
        'its model conclusions are planning context and cannot establish scientific evidence. '
        'Search existing scientific accounts, Propositions and Claims before authoring new science. '
        'Use the loaded-reference data tools on demand; only the two explicitly offered small-model '
        'phenotype tools may query BioIndex. Preserve receipt IDs, coverage, source identities and prior authorship. '
        'Import independently collected scientific evidence before citing it. Seek relevant CFDE grounding, '
        'but supported accounts without it are allowed. Export the selected evidence context, author one '
        'ScientificAccount per document, validate_submission, repair errors and submit_accounts. '
        'Return the submission ID and account links. Do not publish automatically.')


def connection_instructions(work_id):
    endpoint = public_base() + '/mcp'
    return {'mcp_url': endpoint, 'prompt': prompt(work_id), 'instructions': {
        'codex': 'Add an HTTP MCP server named reveal with url ' + endpoint +
            ' and bearer_token_env_var="REVEAL_MCP_TOKEN". Set the shown credential in that environment variable before starting Codex. '
            'Allow local file reads/writes and authenticated downloads for this workspace. Never paste the credential into the research prompt.',
        'claude_code': 'Add an HTTP MCP server named reveal with URL ' + endpoint +
            ' and Authorization header Bearer ${REVEAL_MCP_TOKEN}. Set the shown credential in that environment variable before starting Claude Code. '
            'Allow local file reads/writes and authenticated downloads for this workspace. Keep the credential out of committed configuration.'}}


def issue_grant(tx, owner, work_id, key, *, kind='local', execution_id=None):
    me = valid_principal(tx, owner)
    work = owned(tx, 'local_work', work_id, owner)['data']
    if kind not in ('local','hosted') or (kind == 'hosted') != bool(work.get('job_id')):
        raise Problem(403, 'RESEARCH_SCOPE_MISMATCH', 'Research credentials must match the execution mode.')
    if kind == 'hosted' and execution_id is None:
        raise Problem(403, 'EXECUTION_EXPIRED', 'Hosted research requires an execution attempt.')
    if kind == 'local' and me.get('principal_kind') != 'registered':
        raise Problem(403, 'SIGN_IN_REQUIRED', 'Sign in to Reveal before authorizing local research.')
    if work['state'] == 'closed': raise Problem(409, 'WORK_CLOSED', 'This local work is closed.')
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise Problem(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Supply an Idempotency-Key.')
    issue_id = digest([owner, work_id, 'grant', key])
    if tx.get('research_grant_issue', issue_id):
        raise Problem(409, 'GRANT_ALREADY_ISSUED', 'Credentials are shown once. Revoke the previous grant and reconnect with a new retry key.')
    active = [r for r in tx.list('research_access', owner) if r['data']['local_work_id'] == work_id
              and not r['data'].get('revoked_at') and r['data']['expires_at'] > now()]
    if kind == 'hosted':
        for row in active:
            row['data']['revoked_at'] = now(); tx.put('research_access', row['id'], owner, row['data'])
    else:
        connections = {row['data']['grant_id'] for row in active}
        connections.update(row['id'] for row in tx.list('research_oauth_family', owner)
            if row['data']['local_work_id'] == work_id and not row['data'].get('revoked_at') and row['data']['expires_at'] > now())
        if len(connections) >= 5: raise Problem(429, 'GRANT_LIMIT', 'Revoke an old connection before adding another.')
    token = 'rvlm_' + secrets.token_urlsafe(32)
    expiry = min(deadline(min(7 * 86400, int(setting('REVEAL_LOCAL_GRANT_TTL_SECONDS', str(7 * 86400))))),
                 me.get('workspace_expires_at') or '9999', work['expires_at'])
    value = {'grant_id': uid(), 'local_work_id': work_id, 'research_request_id': work['research_request_id'],
        'expires_at': expiry, 'created_at': now(), 'revoked_at': None, 'kind': kind, 'execution_id': execution_id,
        'issued_principal_kind': me.get('principal_kind')}
    tx.put('research_access', hashlib.sha256(token.encode()).hexdigest(), owner, value)
    tx.put('research_grant_issue', issue_id, owner, {'grant_id': value['grant_id']})
    return {**connection_instructions(work_id), 'grant_id': value['grant_id'], 'token': token,
            'expires_at': expiry, 'local_work_id': work_id}


def bearer_shaped(authorization):
    """A research credential's shape, checked without the database."""
    return isinstance(authorization, str) and authorization.startswith('Bearer rvlm_')


def authenticate(tx, authorization, *, write=False, delegated=False):
    if not bearer_shaped(authorization):
        raise Problem(401, 'MCP_AUTH_REQUIRED', 'Connect using a Reveal research credential.')
    row = tx.get('research_access', hashlib.sha256(authorization[7:].encode()).hexdigest())
    if not row: raise Problem(401, 'MCP_AUTH_REQUIRED', 'The research credential is invalid.')
    grant = row['data']; owner = row['owner']; family_id = grant.get('oauth_family_id')
    keys = [('principal', owner), ('local_work', grant['local_work_id']), ('request', grant['research_request_id'])]
    if family_id: keys.append(('research_oauth_family', family_id))
    records = tx.get_records(keys)
    me = principal_record(records.get(('principal', owner)))
    if grant.get('revoked_at') or (not delegated and grant['expires_at'] <= now()):
        raise Problem(401, 'MCP_GRANT_EXPIRED', 'The research credential expired or was revoked; reconnect in Reveal.')
    from .research_oauth import check_grant_scope, require_registered_local
    family = records.get(('research_oauth_family', family_id)) if family_id else None
    check_grant_scope(tx, owner, grant, write=write, me=me, family=family)
    if write: require_registered_local({'grant': grant, 'principal_kind': me.get('principal_kind')})
    work = require_owned(tx, 'local_work', grant['local_work_id'], owner,
        records.get(('local_work', grant['local_work_id'])))['data']
    if ((grant['kind'] == 'hosted') != bool(work.get('job_id'))
            or work['research_request_id'] != grant['research_request_id']):
        raise Problem(403, 'RESEARCH_SCOPE_MISMATCH', 'The research credential does not match its execution context.')
    request = require_owned(tx, 'request', grant['research_request_id'], owner,
        records.get(('request', grant['research_request_id'])))['data']
    if write and (work['state'] == 'closed' or work['expires_at'] <= now()):
        raise Problem(409, 'WORK_CLOSED', 'This research work is closed or its data lifetime expired.')
    if grant['kind'] == 'hosted':
        execution = tx.get_records((('job', work['job_id']), ('queue', work['job_id'])))
        job = require_owned(tx, 'job', work['job_id'], owner, execution.get(('job', work['job_id'])))['data']
        queue = require_owned(tx, 'queue', work['job_id'], owner, execution.get(('queue', work['job_id'])))['data']
        if job['status'] in ('succeeded', 'failed', 'cancelled', 'insufficient_evidence', 'cancel_requested') or str(queue.get('attempt')) != str(grant['execution_id']):
            raise Problem(403, 'EXECUTION_EXPIRED', 'This hosted execution no longer has research authority.')
    return {'owner': owner, 'principal_kind': me.get('principal_kind'), 'grant': grant, 'work': work, 'request': request,
            'me': me, 'oauth_family': family}


def authorize_commit(tx, operation, *, extra=()):
    """Recheck an operation's authority in this transaction, with one grant read and one batched record read.

    Returns the authorized work and request, and the batched rows: extra exact keys arrive in the same read
    but are NOT authorized; pass each through require_owned.
    """
    owner = operation['owner_user_id']; grant = None
    if operation.get('grant_id'):
        # The first matching row in list() order, as before, without reading the owner's other grants.
        grant = next((r['data'] for r in records_where(tx, 'research_access', owner, 'grant_id', operation['grant_id'])
                      if r['data']['grant_id'] == operation['grant_id']), None)
    family_id = grant.get('oauth_family_id') if grant else None
    keys = [('principal', owner), ('local_work', operation['local_work_id']), ('request', operation['research_request_id']), *extra]
    if family_id: keys.append(('research_oauth_family', family_id))
    records = tx.get_records(keys)
    me = principal_record(records.get(('principal', owner)))
    work = require_owned(tx, 'local_work', operation['local_work_id'], owner, records.get(('local_work', operation['local_work_id'])))['data']
    request = require_owned(tx, 'request', operation['research_request_id'], owner,
        records.get(('request', operation['research_request_id'])))['data']
    if operation.get('grant_id'):
        if not grant or grant.get('revoked_at'):
            raise Problem(403, 'AUTHORITY_REVOKED', 'The connection was revoked before this operation committed.')
        from .research_oauth import check_grant_scope, require_registered_local
        check_grant_scope(tx, owner, grant, me=me, family=records.get(('research_oauth_family', family_id)) if family_id else None)
        require_registered_local({'grant': grant, 'principal_kind': me.get('principal_kind')})
        if (grant['local_work_id'] != work['id'] or grant['research_request_id'] != operation['research_request_id']
                or work['research_request_id'] != operation['research_request_id']
                or (grant['kind'] == 'hosted') != bool(work.get('job_id'))):
            raise Problem(403, 'RESEARCH_SCOPE_MISMATCH', 'The operation authority does not match its execution context.')
        if grant['kind'] == 'hosted':
            execution = tx.get_records((('job', work['job_id']), ('queue', work['job_id'])))
            job = require_owned(tx, 'job', work['job_id'], owner, execution.get(('job', work['job_id'])))['data']
            queue = require_owned(tx, 'queue', work['job_id'], owner, execution.get(('queue', work['job_id'])))['data']
            if job['status'] in ('succeeded', 'failed', 'cancelled', 'insufficient_evidence', 'cancel_requested') or str(queue['attempt']) != str(grant['execution_id']):
                raise Problem(403, 'EXECUTION_EXPIRED', 'The hosted execution is no longer active.')
    return {'work': work, 'request': request, 'records': records}


class ResearchWorkService:
    def __init__(self, repository, *, prepare_seed=None, data_service=None, graph_service=None):
        self.repo = repository
        self.prepare_seed = prepare_seed
        self.data_service = data_service
        self.graph_service = graph_service

    def create(self, tx, identity, body, key, freeze):
        owner = identity['user_id']
        def action():
            self.check_create_quota(tx, owner)
            frozen, binding = freeze(tx, identity, body)
            work, rows = self.frozen_rows(identity, frozen, binding)
            tx.insert_many(rows)
            return self.view(tx, owner, work['id'])
        return idempotent(tx, owner, 'create-local', key, body, action)

    @staticmethod
    def check_create_quota(tx, owner):
        if len([r for r in tx.list('local_work', owner) if r['data']['state'] != 'closed']) >= 20:
            raise Problem(429, 'LOCAL_WORK_LIMIT', 'Close an earlier local research run first.')

    @staticmethod
    def frozen_rows(identity, frozen, binding):
        """Stage local work for a newly frozen request, also used by Lightning continuation."""
        from .reference_generation import generation_of_anchors
        owner = identity['user_id']; generation = generation_of_anchors(binding['anchors'])
        work_id = uid(); created = now(); operation_id = uid()
        work = {'id': work_id, 'owner_user_id': owner, 'research_request_id': frozen['id'],
            'state': 'preparing', 'created_at': created, 'last_activity': created,
            'last_action': 'created', 'expires_at': min(deadline(30 * 86400), identity.get('workspace_expires_at') or '9999'),
            'reference_generation_id': generation, 'package_id': None, 'package_sha256': None, 'last_error': None,
            'preparation_operation_id': operation_id}
        operation = {'id': operation_id, 'owner_user_id': owner, 'local_work_id': work_id,
            'research_request_id': frozen['id'], 'grant_id': None, 'kind': 'prepare', 'arguments': {},
            'state': 'received', 'created_at': created, 'validation_only': False,
            'account_ids': [], 'reused_account_ids': [], 'attempt': 0}
        return work, [('local_work', work_id, owner, work),
            ('research_pin', frozen['id'], owner, {'id': frozen['id'], 'research_request_id': frozen['id'],
                'generation_id': generation, 'state': 'active', 'expires_at': work['expires_at']}),
            ('research_operation', operation_id, owner, operation)]

    def view(self, tx, owner, work_id, *, work_row=None, loaded=None):
        """work_row, when given, must already be authorized for owner in this transaction. loaded, when
        given, receives every research_operation row of the work, so a poll resumes from the same snapshot."""
        return self.views(tx, owner, [{**(work_row or owned(tx, 'local_work', work_id, owner)), 'id': work_id}], loaded=loaded)[0]

    def views(self, tx, owner, rows, *, loaded=None):
        """Views of this owner's authorized local_work rows with a fixed number of reads, whatever their count."""
        if not rows: return []
        ids = [row['id'] for row in rows]
        requested = sorted({row['data']['research_request_id'] for row in rows})
        requests = tx.get_many('request', requested) if len(requested) > 1 else None
        children = work_children(tx, owner, ids, operations=None if loaded is not None else ('validate', 'submit'))
        operations, grants = children['research_operation'], children['research_access']
        submissions = {work_id: [r['data'] for r in operations.get(work_id, [])
            if r['data']['kind'] in ('validate', 'submit')] for work_id in ids}
        # Warm this snapshot's evidence rows in one read; each submission is still authorized row by row.
        evidence = sorted({(kind, identity) for values in submissions.values() for value in values
            for field, kind in (('import_ids', 'evidence_import'), ('receipt_ids', 'evidence_receipt'))
            for identity in (value.get('arguments') or {}).get(field, []) if isinstance(identity, str)})
        if evidence: tx.get_records(evidence)
        families = tx.get_many('research_oauth_family', sorted({row['data']['oauth_family_id']
            for work_id in ids for row in grants.get(work_id, []) if row['data'].get('oauth_family_id')}))
        resolved = {}; result = []
        for row in rows:
            work_id = row['id']
            work = deepcopy(row['data'])
            work.pop('owner_user_id', None)
            request_id = work['research_request_id']
            request = (owned(tx, 'request', request_id, owner) if requests is None
                else require_owned(tx, 'request', request_id, owner, requests.get(request_id)))
            work['request'] = deepcopy(request['data'])
            for upload in (work['request'].get('user_inputs') or {}).get('uploads', []):
                upload.pop('storage', None)
                if isinstance(upload.get('extraction'), dict):
                    upload['extraction'].pop('storage', None); upload['extraction'].pop('content', None)
            work['submissions'] = [self.operation_view(value, tx=tx, owner=owner, resolved=resolved)
                for value in submissions[work_id]]
            connections = {}
            for grant_row in grants.get(work_id, []):
                grant = grant_row['data']
                if grant['local_work_id'] != work_id or grant['kind'] != 'local': continue
                family = families.get(grant.get('oauth_family_id'))
                if family and family['owner'] == owner:
                    # Show the renewable connection lifetime, so dormant OAuth
                    # authorization remains visible and revocable after access expires.
                    grant = {**grant, **{key: family['data'].get(key) for key in ('expires_at', 'created_at', 'revoked_at')}}
                prior = connections.get(grant['grant_id'])
                if not prior or grant['expires_at'] > prior['expires_at']:
                    connections[grant['grant_id']] = {k: grant.get(k) for k in ('grant_id', 'expires_at', 'revoked_at', 'created_at')}
            work['grants'] = list(connections.values())
            work.update(connection_instructions(work_id))
            result.append(work)
        if loaded is not None: loaded['operations'] = [r for work_id in ids for r in operations.get(work_id, [])]
        return result

    @staticmethod
    def release_pin_if_idle(tx, work):
        """Release a closed work's generation pin once every operation settled; a released pin stays as it was."""
        if work['state'] != 'closed': return
        pin = tx.get('research_pin', work['research_request_id'])
        if not pin or pin['data'].get('state') == 'released': return
        if unsettled_operations(tx, work['owner_user_id'], work['id']): return
        pin['data'].update(state='released', released_at=now())
        tx.put('research_pin', work['research_request_id'], pin['owner'], pin['data'])

    @staticmethod
    def operation_view(operation, *, tx, owner, strict=False, resolved=None):
        result = {k: deepcopy(v) for k, v in operation.items() if k in (
            'id', 'kind', 'state', 'created_at', 'completed_at', 'result', 'error', 'report',
            'account_ids', 'reused_account_ids', 'validation_only', 'local_work_id')}
        from .research_execution import authorize_operation_result
        try:
            authorize_operation_result(tx, owner, operation, resolved=resolved)
        except Problem as error:
            if strict: raise
            for field in ('result', 'report', 'error', 'reused_account_ids'): result.pop(field, None)
            result['error'] = {'code': 'REUSE_AUTHORITY_UNAVAILABLE',
                'detail': 'Borrowed source context is no longer available. Accepted account status is unchanged.'}
        return result

    def enqueue(self, tx, owner, work, kind, arguments, grant_id):
        operation = {'id': uid(), 'owner_user_id': owner, 'local_work_id': work['id'],
            'research_request_id': work['research_request_id'], 'grant_id': grant_id, 'kind': kind,
            'arguments': deepcopy(arguments), 'state': 'received', 'created_at': now(),
            'validation_only': kind == 'validate', 'account_ids': [], 'reused_account_ids': [], 'attempt': 0}
        tx.insert('research_operation', operation['id'], owner, operation)   # a fresh id: no pre-read
        return operation

    def kick(self, work_id):
        with self.repo.read_transaction() as tx:
            work = tx.get('local_work', work_id)
            if not work: return
            pending = pending_operations(work_records(tx, 'research_operation', work['owner'], work_id))
        for identity, token in pending: self.resume_operation(identity, lease_token=token)

    def resume_pending(self, rows):
        """Schedule pending operations among rows already read, e.g. by view(loaded=...)."""
        for identity, token in pending_operations(rows): self.resume_operation(identity, lease_token=token)

    def resume_operation(self, operation_id, *, lease_token=None):
        """Schedule an already-authorized operation without delaying its reply.

        The durable lease and commit authorization remain authoritative. This
        process-local guard only prevents repeated polls filling the worker
        queue with copies of the same operation while a replica is busy.
        """
        # An expired durable lease gets a distinct key: a hung older Future
        # must not prevent recovery in this process, while repeated polls of
        # the same expired lease still schedule at most one replacement.
        key = (self.repo.sqlite_path, self.repo.table_prefix, operation_id, lease_token)
        with _scheduled_operations_lock:
            if key in _scheduled_operations: return
            _scheduled_operations.add(key)
        def finished(_):
            with _scheduled_operations_lock: _scheduled_operations.discard(key)
        try: future = POOL.submit(self.run_operation, operation_id)
        except BaseException:
            finished(None)
            raise
        future.add_done_callback(finished)

    def reconcile(self):
        """Recover short operations, close expired local work and release idle generation pins.

        One snapshot finds what must change. The write fence is taken only for those rows, without waiting
        behind another writer (a busy fence defers them to the next cycle), and each is re-read and decided
        again under it. Recovery is scheduled from the same snapshot's unsettled operations.
        """
        with self.repo.read_transaction() as tx:
            works = tx.list('local_work')
            if not works: return
            local = [row for row in works if not row['data'].get('job_id')]   # hosted lifecycle owns its work/pin
            keys = [('principal', row['owner']) for row in local
                    if not (row['data']['state'] == 'closed' and row['data'].get('closed_at'))]
            keys += [('research_pin', row['data']['research_request_id']) for row in local if row['data']['state'] == 'closed']
            rows = tx.get_records(keys) if keys else {}
            operations = open_operations(tx)
        busy = {(op['owner'], op['data'].get('local_work_id')) for op in operations}
        candidates = []
        for row in local:
            work = row['data']
            pin = rows.get(('research_pin', work['research_request_id'])) if work['state'] == 'closed' else None
            if (needs_close(work, rows.get(('principal', row['owner'])))
                    or (pin and pin['data'].get('state') != 'released' and (work['owner_user_id'], work['id']) not in busy)):
                candidates.append(row['id'])
        if candidates:
            try:
                with self.repo.transaction(nowait=True) as tx:
                    current = tx.get_many('local_work', candidates)
                    principals = tx.get_many('principal', sorted({row['owner'] for row in current.values()}))
                    for identity in candidates:
                        row = current.get(identity)
                        if not row or row['data'].get('job_id'): continue
                        work = row['data']
                        if needs_close(work, principals.get(row['owner'])):
                            work.update(state='closed', closed_at=work.get('closed_at') or now())
                            tx.put('local_work', identity, row['owner'], work)
                        self.release_pin_if_idle(tx, work)
            except FenceBusy: pass
        # kick()'s match: the operation's owner and work are the local_work row's.
        owned_by = {(row['owner'], row['id']) for row in works}
        self.resume_pending([op for op in operations if (op['owner'], op['data'].get('local_work_id')) in owned_by])

    def retain(self, owner, work_id, data, filename, *, purpose='source', metadata=None):
        if len(data) > 32_000_000: raise Problem(413, 'ARTIFACT_TOO_LARGE', 'A research artifact exceeds its limit.')
        checksum = hashlib.sha256(data).hexdigest()
        identity = digest([work_id, purpose, checksum, filename, metadata or {}])
        with measure('research_artifact', 'authorization_read'), self.repo.read_transaction() as tx:
            owned(tx, 'local_work', work_id, owner)
            existing = tx.get('research_artifact', identity)
            if existing:
                if existing['owner'] != owner: raise Problem(404, 'NOT_FOUND', 'Artifact is unavailable.')
                return existing['data']
            matching = next((r['data'] for r in work_records(tx, 'research_artifact', owner, work_id)
                if r['data']['sha256'] == checksum
                and not r['data'].get('retained_record')), None)
        with measure('research_artifact', 'store'):
            storage = matching['storage'] if matching else user_inputs.retain(data,
                user_inputs.TYPES.get(Path(filename).suffix, 'application/octet-stream'))
        value = {'id': identity, 'local_work_id': work_id,
            'filename': filename, 'sha256': checksum, 'size_bytes': len(data), 'storage': storage,
            'purpose': purpose, 'metadata': metadata or {}, 'created_at': now()}
        with measure('research_artifact', 'commit'), self.repo.transaction() as tx:
            owned(tx, 'local_work', work_id, owner)
            existing = tx.get('research_artifact', identity)
            if existing: return existing['data']
            blobs = {r['data']['sha256']: r['data']['size_bytes']
                for r in work_records(tx, 'research_artifact', owner, work_id)}
            if checksum not in blobs and sum(blobs.values()) + len(data) > 128_000_000:
                raise Problem(429, 'RESEARCH_STORAGE_LIMIT', 'This research work reached its retained artifact budget.')
            tx.put('research_artifact', identity, owner, value)
        return value

    def stage(self, owner, work_id, data, filename, *, storage, purpose='source', metadata=None):
        """retain() without the database: the same id and descriptor, uploading the blob only when storage
        ({sha256: ref} of this work's retained blobs, extended in place) lacks it. commit_staged() writes it."""
        if not isinstance(data, bytes) or len(data) > 32_000_000:
            raise Problem(413, 'ARTIFACT_TOO_LARGE', 'A research artifact exceeds its limit.')
        checksum = hashlib.sha256(data).hexdigest()
        if checksum not in storage:
            with measure('research_artifact', 'store'):
                storage[checksum] = user_inputs.retain(data, user_inputs.TYPES.get(Path(filename).suffix, 'application/octet-stream'))
        return {'id': digest([work_id, purpose, checksum, filename, metadata or {}]), 'local_work_id': work_id,
            'filename': filename, 'sha256': checksum, 'size_bytes': len(data), 'storage': storage[checksum],
            'purpose': purpose, 'metadata': metadata or {}, 'created_at': now()}

    @staticmethod
    def retained_storage(tx, owner, work_id):
        """{sha256: storage} of a work's own retained blobs, so staging reuses them as retain() does."""
        storage = {}
        for row in work_records(tx, 'research_artifact', owner, work_id):
            if not row['data'].get('retained_record') and row['data'].get('storage'):
                storage.setdefault(row['data']['sha256'], row['data']['storage'])
        return storage

    @staticmethod
    def commit_staged(tx, owner, work_id, staged):
        """Rows to insert for staged artifacts ({id: value}) under the caller's fence, as retain() commits them:
        existing ids are kept, and new blobs must fit the work's combined retained budget."""
        if not staged: return []
        existing = tx.get_many('research_artifact', sorted(staged))
        if any(row['owner'] != owner for row in existing.values()):
            raise Problem(404, 'NOT_FOUND', 'Artifact is unavailable.')
        new = {identity: value for identity, value in staged.items() if identity not in existing}
        if not new: return []
        sizes = artifact_sizes(tx, owner, work_id)
        added = {value['sha256']: value['size_bytes'] for value in new.values() if value['sha256'] not in sizes}
        if added and sum(sizes.values()) + sum(added.values()) > 128_000_000:
            raise Problem(429, 'RESEARCH_STORAGE_LIMIT', 'This research work reached its retained artifact budget.')
        return [('research_artifact', identity, owner, value) for identity, value in new.items()]

    def retain_seed(self, owner, work_id, files):
        """Retain a bounded seed with two transactions, not two per artifact.

        Only immutable blob transfers run concurrently, outside the database
        lock. The final transaction rechecks ownership, existing identities and
        the combined quota before committing the complete descriptor batch.
        Retries retain the original artifact IDs and reuse verified blob refs.
        """
        if not 1 <= len(files) <= 256:
            raise Problem(413, 'ARTIFACT_TOO_LARGE', 'The research seed exceeds its artifact count limit.')
        values, contents = {}, {}
        for filename, data in files.items():
            if isinstance(data, Path): data = data.read_bytes()
            if isinstance(data, str): data = data.encode()
            if not isinstance(data, bytes) or len(data) > 32_000_000:
                raise Problem(413, 'ARTIFACT_TOO_LARGE', 'A research artifact exceeds its limit.')
            checksum = hashlib.sha256(data).hexdigest()
            identity = digest([work_id, 'seed', checksum, filename, {}])
            values[identity] = {'id': identity, 'local_work_id': work_id, 'filename': filename,
                'sha256': checksum, 'size_bytes': len(data), 'purpose': 'seed', 'metadata': {}}
            contents.setdefault(checksum, (data, user_inputs.TYPES.get(Path(filename).suffix,
                'application/octet-stream')))

        def inspect(tx):
            owned(tx, 'local_work', work_id, owner)
            existing = tx.get_many('research_artifact', list(values))
            if any(row['owner'] != owner for row in existing.values()):
                raise Problem(404, 'NOT_FOUND', 'Artifact is unavailable.')
            retained = work_records(tx, 'research_artifact', owner, work_id)
            sizes = {row['data']['sha256']: row['data']['size_bytes'] for row in retained}
            sizes.update({value['sha256']: value['size_bytes'] for value in values.values()})
            if sum(sizes.values()) > 128_000_000:
                raise Problem(429, 'RESEARCH_STORAGE_LIMIT', 'This research work reached its retained artifact budget.')
            storage = {row['data']['sha256']: row['data']['storage'] for row in retained
                if not row['data'].get('retained_record')}
            return existing, storage

        with self.repo.read_transaction() as tx:
            existing, storage = inspect(tx)
        if len(existing) == len(values):
            return [existing[identity]['data'] for identity in values]
        pending = {checksum: RETAINERS.submit(user_inputs.retain, *content)
            for checksum, content in contents.items() if checksum not in storage}
        try:
            for checksum, future in pending.items(): storage[checksum] = future.result()
        finally:
            for future in pending.values(): future.cancel()
            wait(pending.values())
        with self.repo.transaction() as tx:
            existing, current_storage = inspect(tx)
            storage.update(current_storage)
            additions = []
            for identity, value in values.items():
                if identity in existing:
                    values[identity] = existing[identity]['data']
                else:
                    value.update(storage=storage[value['sha256']], created_at=now())
                    additions.append(('research_artifact', identity, owner, value))
            tx.insert_many(additions)
        return list(values.values())

    @staticmethod
    def artifact_view(value):
        return {k: deepcopy(value[k]) for k in ('id', 'filename', 'sha256', 'size_bytes', 'purpose')}

    def package(self, tx, owner, work_id):
        work = owned(tx, 'local_work', work_id, owner)['data']
        if work['state'] == 'preparing':
            return {'state': 'preparing', 'operation_id': work['preparation_operation_id']}
        if not work['package_id']:
            raise Problem(409, 'PACKAGE_NOT_READY', 'The research seed is not ready.', error=work.get('last_error'))
        row = owned(tx, 'research_package', work['package_id'], owner)['data']
        return {'package_id': work['package_id'], 'package_sha256': work['package_sha256'],
            'package': row['package'], 'manifest': row['manifest'], 'artifacts': row['artifacts'],
            'authoring_instructions': prompt(work_id)}

    def prepare(self, operation):
        owner, work_id = operation['owner_user_id'], operation['local_work_id']
        with self.repo.read_transaction() as tx:
            frozen = owned(tx, 'request', operation['research_request_id'], owner)['data']
            if frozen.get('lightning_audit_id'):
                from .lightning_continuation import hydrate_context
                frozen = hydrate_context(tx, owner, frozen)
            binding = owned(tx, 'request_binding', frozen['id'], owner)['data']
            work = owned(tx, 'local_work', work_id, owner)['data']
            queue = owned(tx, 'queue', work['job_id'], owner)['data'] if work.get('job_id') else None
            max_accounts = (queue or {}).get('inputs', {}).get('budgets', {}).get('max_accounts', 3)
        from .evidence_package import DapperRuntime, canonical_json
        from .research_seed import prepare_research_seed
        builder = self.prepare_seed or prepare_research_seed
        with tempfile.TemporaryDirectory(prefix='reveal-seed-') as directory:
            built = builder(frozen, binding, dapper=DapperRuntime(CURRENT_DAPPER_SNAPSHOT),
                            project_root=ROOT, output=Path(directory), max_accounts=max_accounts)
            built.package['research_context'] = {'local_work_id': work_id, 'research_request_id': frozen['id'], 'mcp_url': public_base()+'/mcp'}
            raw = canonical_json(built.package)
            built.files['evidence-package.json'] = raw
            from .evidence_files import build_evidence_index
            built.files['evidence-index.json'] = build_evidence_index(raw)
            built.manifest['files']['evidence-index.json'] = hashlib.sha256(built.files['evidence-index.json']).hexdigest()
            built.manifest['package_sha256'] = hashlib.sha256(raw).hexdigest()
            built.manifest['files']['evidence-package.json'] = hashlib.sha256(raw).hexdigest()
            built.files['manifest.json'] = canonical_json(built.manifest)
            retained = self.retain_seed(owner, work_id, built.files)
            artifacts = [self.artifact_view(value) for value in retained]
            value = next(value for value in retained if value['filename'] == 'evidence-package.json')
            result = {'id': digest([work_id, value['sha256']]), 'package': built.package,
                'sha256': value['sha256'], 'manifest': built.manifest, 'artifacts': artifacts}
        return result

    def settle(self, tx, operation, owner, current, result):
        """Record a finished operation's result inside the caller's fence; current is its row read there."""
        if operation['kind'] == 'prepare':
            work = owned(tx, 'local_work', operation['local_work_id'], owner)['data']
            tx.put('research_package', result['id'], owner, result)
            work.update(package_id=result['id'], package_sha256=result['sha256'], last_activity=now(), last_error=None)
            if work['state'] != 'closed': work['state'] = 'ready'
            tx.put('local_work', work['id'], owner, work)
            result = {'package_id': result['id'], 'package_sha256': result['sha256']}
        elif operation['kind'] == 'submit':
            result = self.commit_accounts(tx, current, result)
        current.update(state='accepted' if operation['kind'] == 'submit' else 'succeeded', result=result, completed_at=now())
        for key in ('report', 'account_ids', 'reused_account_ids'):
            if key in result: current[key] = result[key]
        tx.put('research_operation', operation['id'], owner, current)
        self.release_pin_if_idle(tx, owned(tx, 'local_work', operation['local_work_id'], owner)['data'])

    def run_operation(self, operation_id):
        token = uid()
        with self.repo.transaction() as tx:
            row = tx.get('research_operation', operation_id)
            if not row: return
            operation = row['data']
            if operation['state'] in TERMINAL: return
            if operation['state'] == 'running' and operation.get('lease_until', '') > now(): return
            operation.update(state='running', lease_token=token, lease_until=deadline(900), attempt=operation['attempt'] + 1)
            tx.put('research_operation', operation_id, row['owner'], operation)
        settled = []
        def finalize(tx, current, result):
            # Query, import and export commit their records, this result and the pin release in one fence.
            self.settle(tx, operation, row['owner'], current, result); settled.append(True)
        try:
            if operation['kind'] not in SELF_AUTHORIZING:
                with self.repo.read_transaction() as tx: authorize_commit(tx, operation)
            if operation['kind'] == 'prepare': result = self.prepare(operation)
            elif operation['kind'] == 'query': result = self.query(operation, finalize=finalize)
            elif operation['kind'] == 'import': result = self.import_sources(operation, finalize=finalize)
            elif operation['kind'] == 'export':
                from .research_execution import export_context
                result = export_context(self, operation, finalize=finalize)
            elif operation['kind'] in ('validate', 'submit'): result = self.validate(operation)
            else: raise Problem(422, 'INVALID_OPERATION', 'Unknown research operation.')
            if settled: return
            with self.repo.transaction() as tx:
                current = tx.get('research_operation', operation_id)['data']
                if current.get('lease_token') != token: return
                authorize_commit(tx, current)
                if operation['kind'] == 'export':
                    from .research_execution import authorize_context_selection
                    authorize_context_selection(tx, row['owner'], operation['local_work_id'], operation['research_request_id'], operation['arguments'])
                self.settle(tx, operation, row['owner'], current, result)
        except Exception as error:
            from .scientific_account_lint import AccountValidationError
            scientific_error = isinstance(error, AccountValidationError)
            detail = error.detail if isinstance(error, Problem) else str(error)[:1000]
            # No captured response bodies, credentials or private backend paths in public errors.
            if not isinstance(error, (Problem, AccountValidationError, ValueError)):
                detail = 'The operation failed; retry after checking backend diagnostics.'
                import logging
                logging.getLogger(__name__).exception('Research operation %s failed', operation_id)
            with self.repo.transaction() as tx:
                current = tx.get('research_operation', operation_id)['data']
                if current.get('lease_token') != token: return
                code = error.code if isinstance(error, Problem) else 'VALIDATION_REJECTED' if scientific_error else 'OPERATION_FAILED'
                current.update(state='rejected' if scientific_error else 'failed', completed_at=now(),
                    error={'code': code, 'detail': detail})
                if scientific_error: current['report'] = error.report
                tx.put('research_operation', operation_id, row['owner'], current)
                self.release_pin_if_idle(tx, owned(tx, 'local_work', operation['local_work_id'], row['owner'])['data'])
                if operation['kind'] == 'prepare':
                    work = owned(tx, 'local_work', operation['local_work_id'], row['owner'])['data']
                    if work['state'] != 'closed': work['state'] = 'preparation_failed'
                    work['last_error'] = current['error']; tx.put('local_work', work['id'], row['owner'], work)

    def query(self, operation, *, finalize=None):
        from .research_execution import capture_query
        return capture_query(self, operation, finalize=finalize)

    def import_sources(self, operation, *, finalize=None):
        from .research_execution import import_sources
        return import_sources(self, operation, finalize=finalize)

    def validate(self, operation):
        from .research_execution import validate_submission
        return validate_submission(self, operation)

    def commit_accounts(self, tx, operation, result):
        from .research_execution import commit_accounts
        return commit_accounts(self, tx, operation, result)
