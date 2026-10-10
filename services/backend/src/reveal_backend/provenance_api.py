"""Authorized account provenance representation, stable trace pages and immutable pins.

GET /v1/accounts/{id}/provenance (trace) runs in three phases and writes no file:
  1a  under one application read transaction: identity, the authorized source, its artifact records, the cursor secret;
  1b  with no application lease: capture bytes, then the reference reads under the reference lease alone
      (provenance_reference.expand; only the legacy-generation path opens an application read for its supplements);
  2   no database lease: the portal's bottom-line GWAS graph of every reached trait, within PORTAL_BUDGET_SECONDS
      (a failure is coverage external_provenance_unavailable for that trait, never an error);
  3   a short read transaction re-checks identity, publication or ownership and the withheld ids (a change is 409
      CURSOR_EXPIRED), then collect, the withheld filter, the snapshot and the page.
Phases 1 and 3 run in the threadpool, at most TRACE_SLOTS at once per process.
"""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import base64
import hashlib
import hmac
import json
import os
import weakref

from .auth import Problem
from .repository import digest

from .account_provenance import TRACING_POLICY_VERSION as POLICY_VERSION
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PORTAL_BUDGET_SECONDS = 15
TRACE_SLOTS = 2
_SLOTS = weakref.WeakKeyDictionary()  # running loop -> its Semaphore(TRACE_SLOTS)


def artifact_records(tx, source):
    """Only retained bytes for visible Files; public reads never look up an owner."""
    files = {node.get('sha256'): node for node in source['document'].get('files', [])
             if node.get('sha256') and node.get('id')}
    if source['public'] is not None:
        records = source['snapshot'].get('artifact_records', {})
    else:
        owner = source['user']
        rows = tx.get_many('artifact', [digest([owner, checksum]) for checksum in files])
        records = {row['data'].get('sha256'): row['data'] for row in rows.values() if row['owner'] == owner}
    return {checksum: deepcopy(record) for checksum, record in records.items()
            if checksum in files and record.get('sha256') == checksum
            and record.get('file', {}).get('id') == files[checksum]['id']}


def _decode_cursor(cursor, secret):
    try:
        encoded, signature = cursor.split('.')
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature): raise ValueError()
        result = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if not isinstance(result, dict): raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError):
        raise Problem(409, 'CURSOR_EXPIRED', 'Reload the first provenance page; this cursor is invalid.') from None


def _secret(secret=None):
    secret = os.getenv('REVEAL_GATEWAY_SECRET', '').encode() if secret is None else secret
    if len(secret) < 32:
        raise Problem(503, 'SERVICE_UNAVAILABLE', 'Configure the provenance cursor signing secret before serving this endpoint.')
    return secret


def paginate(result, snapshot, authority, query, *, secret=None):
    """Paginate traces only, preserving complete-account annotations and metrics."""
    secret = _secret(secret)
    try:
        limit = int(query.get('limit', '100'))
        if not 1 <= limit <= 250: raise ValueError()
    except (ValueError, TypeError):
        raise Problem(422, 'INVALID_QUERY', 'limit must be an integer between 1 and 250.') from None
    binding = digest([authority, snapshot, result])
    offset = 0
    cursor = query.get('cursor')
    if cursor:
        state = _decode_cursor(cursor, secret)
        if (state.get('snapshot') != binding or type(state.get('offset')) is not int
                or state['offset'] < 0 or type(state.get('limit')) is not int
                or not 1 <= state['limit'] <= 250
                or ('limit' in query and limit != state['limit'])):
            raise Problem(409, 'CURSOR_EXPIRED', 'Permissions, provenance, or page settings changed; reload the first page.')
        offset, limit = state['offset'], state['limit']
    count = len(result['traces'])
    if offset > count:
        raise Problem(409, 'CURSOR_EXPIRED', 'The requested provenance page is unavailable.')
    response = deepcopy(result)
    response['traces'] = result['traces'][offset:offset + limit]
    trace_ids = {trace['id'] for trace in response['traces']}
    wrappers = [response['account']]
    for group in ('claims', 'propositions', 'evidence_items'):
        wrappers.extend(response[group])
    for wrapper in wrappers:
        annotation = wrapper['provenance']
        annotation['trace_ids'] = [identity for identity in annotation['trace_ids'] if identity in trace_ids]
        annotation['page_trace_count'] = len(annotation['trace_ids'])
    # Keep all explored graph branches: witness paths are not a license to discard
    # an alternative recorded route. Graph caps bound the shared representation.
    next_offset = offset + len(response['traces'])
    next_cursor = None
    if next_offset < count:
        state = {'snapshot': binding, 'offset': next_offset, 'limit': limit}
        encoded = base64.urlsafe_b64encode(json.dumps(state, separators=(',', ':')).encode()).decode().rstrip('=')
        next_cursor = encoded + '.' + hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    response['snapshot'] = {**snapshot, 'id': binding}
    response['page'] = {'limit': limit, 'offset': offset, 'total': count, 'next_cursor': next_cursor}
    if len(json.dumps(response, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
        raise Problem(422, 'PROVENANCE_LIMIT_EXCEEDED', 'The provenance response exceeds the 8 MiB limit; no rankings were returned.')
    return response


def _fence(source):
    """What phase 3 re-checks: the reader, the publication or ownership state and the withheld dependencies."""
    publication, record = source['public'], source['publication_record']
    return digest([source['user'], publication and [publication['id'], publication.get('owner'), publication['data'].get('version')],
                   record and [record.get('owner'), record['data'].get('version')], source['root_payload'], digest(source['document']),
                   sorted(source.get('withheld_ids', ())), bool(source['withheld_dependencies'])])


class _SupplementReads:
    """load_supplement's tx, for the legacy-generation path only: one application read transaction opened at its first
    lookup (inside the reference lease, the order that cannot starve the pool: reference reads hold at most half of it)
    and closed when expansion ends. A release needs no supplement and never opens it."""
    def __init__(self, repo): self.repo, self.stack, self.tx = repo, ExitStack(), None
    def get(self, kind, identity):
        if self.tx is None: self.tx = self.stack.enter_context(self.repo.read_transaction())
        return self.tx.get(kind, identity)


def _phase1(repo, account_id, resolve, archived):
    from . import provenance_reference
    from .artifact_store import StorageUnavailable
    with repo.read_transaction() as tx:
        source = resolve(tx)
        if 'legacy_result' in source or not source.get('full_document_available', True):
            raise Problem(409, 'PROVENANCE_SOURCE_UNAVAILABLE', 'The complete scientific account source is unavailable.')
        records, fence = artifact_records(tx, source), _fence(source)
    _secret()
    supplements = _SupplementReads(repo)
    try:
        with supplements.stack:
            expanded, metadata = provenance_reference.expand(source['document'], records, tx=supplements,
                blocked_ids=source.get('withheld_ids', ()), account_id=account_id, archived=archived)
    except StorageUnavailable as error:
        raise Problem(503, 'SERVICE_UNAVAILABLE', 'Retained provenance storage is temporarily unavailable.') from error
    return source, fence, expanded, metadata


async def _gwas(traits):
    """{trait id: provenance_portal.Outcome} of every trait whose GWAS route names a portal artifact, all within
    PORTAL_BUDGET_SECONDS of the first request."""
    from . import provenance_portal
    wanted = {identity: info['gwas_artifact']['artifact_id'] for identity, info in traits.items()
              if isinstance(info.get('gwas_artifact'), dict) and isinstance(info['gwas_artifact'].get('artifact_id'), str)}
    artifacts = sorted(set(wanted.values()))
    deadline = asyncio.get_running_loop().time() + PORTAL_BUDGET_SECONDS
    outcomes = await asyncio.gather(*(provenance_portal.bottom_line(artifact, timeout=deadline - asyncio.get_running_loop().time())
                                      for artifact in artifacts))
    found = dict(zip(artifacts, outcomes))
    return {identity: found[artifact] for identity, artifact in wanted.items()}


def _phase3(repo, account_id, resolve, fence, original, expanded, metadata, outcomes, query):
    from .account_provenance import collect
    from .provenance_portal import trait_lineage
    with repo.read_transaction() as tx:
        try: source = resolve(tx)
        except Problem as error:
            if error.status != 404: raise
            source = None
        if source is None or 'legacy_result' in source or _fence(source) != fence:
            raise Problem(409, 'CURSOR_EXPIRED', 'Permissions or sources changed while the provenance was read; reload the first page.')
    blocked = set(metadata.get('blocked_reference_ids', ()))
    issues, observations, gaps = list(metadata.get('issues', [])), list(metadata.get('source_observations', [])), {}
    for identity, info in sorted(metadata.get('traits', {}).items()):
        outcome = outcomes.get(identity)
        if outcome is None:
            if info.get('gwas_reason'): gaps.setdefault(info['gwas_reason'], []).append(identity)
        elif outcome.status == 'ok':
            for group, rows in trait_lineage(identity, outcome).items():
                expanded.setdefault(group, []).extend(rows)
            observations.append({'kind': 'portal_bottom_line', 'key': outcome.portal_url, 'sha256': outcome.sha256})
        else:
            issues.append({'code': 'external_provenance_unavailable', 'object_id': identity})
    if blocked:
        for group, values in expanded.items():
            if isinstance(values, list):
                expanded[group] = [value for value in values if not isinstance(value, dict) or
                                   (value.get('id') not in blocked and value.get('subject') not in blocked
                                    and value.get('object') not in blocked)]
    expanded['_provenance_coverage'] = {key: metadata.get(key, []) for key in
        ('retained_projection_coverage', 'recovery_status', 'blocked_reference_ids')}
    expanded['_provenance_coverage'].update(issues=issues, upstream_gaps=[
        {'route': 'trait_gwas', 'code': code, 'object_ids': sorted(ids)} for code, ids in sorted(gaps.items())])
    result = collect(expanded, account_id)
    if source['withheld_dependencies']:
        withheld = source.get('withheld_ids', set())
        result['nodes'] = [node for node in result['nodes'] if node['id'] not in withheld]
        result['edges'] = [edge for edge in result['edges']
                           if edge['subject'] not in withheld and edge['object'] not in withheld]
        result['coverage']['status'] = 'incomplete'
        result['coverage']['counts_are_lower_bounds'] = True
        result['coverage']['issues'].append({'code': 'withheld_dependencies'})
        result['coverage']['lineage_resolution']['status'] = 'incomplete'
        result['coverage']['lineage_resolution']['issue_count'] = len(result['coverage']['issues'])
        # Do not disclose which revocable borrowed objects were withheld.
        result['account']['provenance']['resolution'] = 'incomplete'
    publication = source['public']
    publication_version = (publication or source['publication_record'] or {}).get('data', {}).get('version', 0)
    authority = (['publication', publication['id'], publication_version] if publication is not None
                 else ['owner', source['user']])
    snapshot = {'policy_version': POLICY_VERSION, 'account_id': account_id,
                'account_payload_sha256': source['root_payload'], 'document_sha256': digest(original),
                'publication_version': publication_version,
                'reference_generation_ids': sorted(metadata.get('reference_generation_ids', [])),
                'supplement_ids': sorted(metadata.get('supplement_ids', [])),
                'source_observations': sorted(observations, key=lambda item: (item['kind'], item['key'], item['sha256']))}
    return paginate(result, snapshot, authority, query)


def _slots():
    loop = asyncio.get_running_loop()
    slots = _SLOTS.get(loop)
    if slots is None: slots = _SLOTS[loop] = asyncio.Semaphore(TRACE_SLOTS)
    return slots


async def trace(repo, account_id, resolve, query, *, archived=None):
    """The account trace. resolve(tx) returns the authorized scientific source (the route's identity and
    scientific_source); archived(mechanism_id) is the catalog's archived-snapshot lookup."""
    from starlette.concurrency import run_in_threadpool
    async with _slots():
        source, fence, expanded, metadata = await run_in_threadpool(_phase1, repo, account_id, resolve, archived)
    outcomes = await _gwas(metadata.get('traits', {}))
    async with _slots():
        return await run_in_threadpool(_phase3, repo, account_id, resolve, fence, source['document'], expanded, metadata, outcomes, query)
