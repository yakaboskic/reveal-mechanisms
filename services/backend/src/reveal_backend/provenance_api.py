"""Authorized account provenance representation, stable trace pages and immutable pins."""
from copy import deepcopy
import base64
import hashlib
import hmac
import json
import os

from .auth import Problem
from .repository import digest

from .account_provenance import TRACING_POLICY_VERSION as POLICY_VERSION
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


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


def paginate(result, snapshot, authority, query, *, secret=None):
    """Paginate traces only, preserving complete-account annotations and metrics."""
    secret = os.getenv('REVEAL_GATEWAY_SECRET', '').encode() if secret is None else secret
    if len(secret) < 32:
        raise Problem(503, 'SERVICE_UNAVAILABLE', 'Configure the provenance cursor signing secret before serving this endpoint.')
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


def read(tx, account_id, source, query):
    from .account_provenance import collect
    from .provenance_reference import expand
    if 'legacy_result' in source or not source.get('full_document_available', True):
        raise Problem(409, 'PROVENANCE_SOURCE_UNAVAILABLE', 'The complete scientific account source is unavailable.')
    original = source['document']
    from .artifact_store import StorageUnavailable
    try:
        expanded, metadata = expand(original, artifact_records(tx, source), tx=tx,
                                    blocked_ids=source.get('withheld_ids', ()), account_id=account_id)
    except StorageUnavailable as error:
        raise Problem(503, 'SERVICE_UNAVAILABLE', 'Retained provenance storage is temporarily unavailable.') from error
    expanded = deepcopy(expanded)
    blocked = set(metadata.get('blocked_reference_ids', ()))
    if blocked:
        for group, values in expanded.items():
            if isinstance(values, list):
                expanded[group] = [value for value in values if not isinstance(value, dict) or
                                   (value.get('id') not in blocked and value.get('subject') not in blocked
                                    and value.get('object') not in blocked)]
    expanded['_provenance_coverage'] = {key: metadata.get(key, []) for key in
        ('issues', 'retained_projection_coverage', 'recovery_status', 'blocked_reference_ids')}
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
                'source_observations': metadata.get('source_observations', [])}
    return paginate(result, snapshot, authority, query)
