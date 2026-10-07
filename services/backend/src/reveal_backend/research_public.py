"""Anonymous scientific reads and bounded, verifiable portable query captures.

Public captures contain only registered scientific-reader output. They never
contain researcher inputs, private work IDs, or scientific reuse closures.
The capture ID commits to exact source bytes and their materialized context;
attachment trusts a verified server record, never a client-supplied checksum.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time

from jsonschema import Draft202012Validator

from .auth import Problem, owned
from .evidence_package import canonical_json, sha256
from .repository import digest, now, uid
from .research_data import OPERATIONS, READER_VERSION, CAPTURE_FORMAT, ReferenceQueryService, SmallModelBioIndex
from .research_work import idempotent, public_base, valid_principal
from .runtime_config import reference_mysql_connection, setting
from . import user_inputs, research_graphs

OWNER = 'public-research'
FORMAT = 'reveal.public-reference-capture/1'
SHA = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
GENERATION = {'reference_generation_id': SHA}
MAX_CAPTURE_BYTES = 8_000_000
MAX_CONTEXT_BYTES = 2_000_000
MAX_FILES = 16


def _object(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def definitions():
    tools = []
    text = {'type': 'string', 'minLength': 1, 'maxLength': 1000}
    def add(name, description, properties, required):
        tools.append({'name': name, 'description': description, 'inputSchema': _object(properties, required),
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True,
                            'openWorldHint': name in SmallModelBioIndex.INDEXES or name in research_graphs.OPERATIONS}})
    add('list_data_operations', 'Discover loaded data operations for an exact public reference generation.', GENERATION, GENERATION)
    add('describe_data_operation', 'Describe coverage and query arguments for an approved reference operation.',
        {**GENERATION, 'operation_id': text}, ('reference_generation_id', 'operation_id'))
    add('query_data', 'Read an approved public reference operation and retain a bounded, exact portable capture. Never falls back to another generation or source.',
        {**GENERATION, 'operation_id': {'enum': sorted([*OPERATIONS, *SmallModelBioIndex.INDEXES])}, 'arguments': {'type': 'object'}},
        ('reference_generation_id', 'operation_id', 'arguments'))
    for name, fields in OPERATIONS.items():
        arguments = {key: {'type': 'string', 'maxLength': 250} for key in fields}
        arguments.update(limit={'type': 'integer', 'minimum': 1, 'maximum': 500}, cursor=text)
        add(name, 'Read loaded data from the exact generation. The response includes coverage and a retained capture; authenticate to attach it to research.',
            {**GENERATION, 'arguments': _object(arguments)}, ('reference_generation_id', 'arguments'))
    for name in SmallModelBioIndex.INDEXES:
        add(name, 'Explicit gated BioIndex observation, literal model small and sigma 2; distinct from loaded data. Pass only phenotype_id in arguments, then inspect returned gene or GeneSet rows. No deployment verification is triggered by this read.',
            {**GENERATION, 'arguments': SmallModelBioIndex.arguments_schema()}, ('reference_generation_id', 'arguments'))
    add('list_knowledge_graphs', 'List the connected public knowledge graphs and their bounded read operations. External observations have their own capture time and source version, independent of imported CFDE generations.', {}, ())
    for name, operation in research_graphs.OPERATIONS.items():
        tools.append({'name': name, 'description': operation['description'] +
            ' Read anonymously; attaching the exact capture later requires that this graph was selected in the frozen research request.',
            'inputSchema': deepcopy(operation['inputSchema']),
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}})
    for name in ('find_propositions', 'find_claims', 'find_scientific_accounts'):
        add(name, 'Search only current public publication snapshots. Reading does not grant future reuse rights.',
            {'query': {'type': 'string', 'maxLength': 1000}, 'filters': {'type': 'object'},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
             'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10000}}, ())
    add('get_scientific_object', 'Get an exact current-public observation with its published dependency closure and citations.',
        {'selection': {'type': 'object'}}, ['selection'])
    add('get_public_capture', 'Replay retained public reference bytes and context without re-querying the source. Expiry is explicit.',
        {'capture_id': SHA}, ['capture_id'])
    return tools


def _bounded_setting(name, default, minimum, maximum):
    try:
        value = int(setting(name, str(default)))
    except (ValueError, TypeError):
        raise Problem(503, 'PUBLIC_RESEARCH_NOT_CONFIGURED', 'Public research limits are invalid.') from None
    return max(minimum, min(maximum, value))


def _rate_limit(service, rate_key):
    """Durable fixed-slot counters bound both traffic and counter cardinality."""
    minute = int(time.time() // 60)
    budgets = [('global', _bounded_setting('REVEAL_PUBLIC_READS_PER_MINUTE', 600, 1, 10000))]
    if rate_key is not None:
        # The HTTP adapter supplies a trusted remote-address key; never store it.
        slot = int(hashlib.sha256(str(rate_key).encode()).hexdigest(), 16) % 256
        budgets.append(('slot-' + str(slot), _bounded_setting('REVEAL_PUBLIC_CLIENT_READS_PER_MINUTE', 60, 1, 1000)))
    with service.repo.transaction() as tx:
        for key, limit in budgets:
            row = tx.get('public_read_budget', key)
            count = row['data']['count'] if row and row['data']['minute'] == minute else 0
            if count >= limit:
                raise Problem(429, 'PUBLIC_RESEARCH_RATE_LIMIT', 'Public research read limit reached; retry after a minute.')
            tx.put('public_read_budget', key, OWNER, {'minute': minute, 'count': count + 1})


def _reader(service):
    return service.data_service or ReferenceQueryService(reference_mysql_connection,
        cursor_secret=setting('REVEAL_RESEARCH_CURSOR_SECRET', setting('REVEAL_GATEWAY_SECRET', '')))


def _catalog(service, generation):
    try:
        result = _reader(service).catalog(generation)
    except Problem:
        raise
    except Exception:
        raise Problem(503, 'SOURCE_UNAVAILABLE', 'The requested public reference generation is unavailable.') from None
    result['phenotype_source'] = SmallModelBioIndex.descriptor()
    return result


def _expire(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def _record(tx, identity):
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
        raise Problem(422, 'INVALID_CAPTURE', 'Use an exact public capture identifier.')
    row = tx.get('public_capture', identity)
    if not row or row['owner'] != OWNER:
        raise Problem(404, 'PUBLIC_CAPTURE_NOT_FOUND', 'The retained public capture is unavailable.')
    value = row['data']
    if value.get('state') != 'ready':
        raise Problem(409, 'PUBLIC_CAPTURE_PENDING', 'The public capture is still being retained; retry shortly.')
    if value['expires_at'] <= now():
        raise Problem(410, 'PUBLIC_CAPTURE_EXPIRED', 'The public capture retention period ended. Existing authenticated attachments remain retained.')
    if value.get('id') != identity or sha256(canonical_json(value.get('manifest'))) != identity:
        raise Problem(409, 'PUBLIC_CAPTURE_INTEGRITY', 'The retained public capture manifest failed verification.')
    return deepcopy(value)


def _verify(record):
    manifest = record['manifest']; files = {}
    if (manifest.get('format') != FORMAT or not isinstance(manifest.get('files'), list)
            or not 1 <= len(manifest['files']) <= MAX_FILES):
        raise Problem(409, 'PUBLIC_CAPTURE_INTEGRITY', 'The retained public capture format is invalid.')
    try:
        for item in manifest['files']:
            path = item['path']
            if (not isinstance(path, str) or not path.startswith('sources/') or '\\' in path or '..' in path.split('/')
                    or path in files or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_CAPTURE_BYTES):
                raise ValueError()
            reference = record['storage'][path]
            if reference['sha256'] != item['sha256'] or reference['size_bytes'] != item['size_bytes']:
                raise ValueError()
            raw = user_inputs.read(reference)
            if sha256(raw) != item['sha256'] or len(raw) != item['size_bytes']:
                raise ValueError()
            files[path] = raw
        if sum(map(len, files.values())) > MAX_CAPTURE_BYTES:
            raise ValueError()
        matching = [raw for raw in files.values() if sha256(raw) == manifest['raw_sha256']]
        if len(matching) != 1:
            raise ValueError()
        capture = json.loads(matching[0])
        if (capture['format'] != CAPTURE_FORMAT or capture['operation'] != manifest['operation']
                or capture['source_mode'] != manifest['source_mode']):
            raise ValueError()
        if (manifest['source_mode'] == 'imported_reference'
                and capture['source'].get('generation_id') != manifest['reference_generation_id']):
            raise ValueError()
        if manifest['source_mode'] == 'external_kg':
            if (manifest['reference_generation_id'] is not None or capture['source'].get('generation_id') is not None
                    or capture['operation'] not in research_graphs.OPERATIONS
                    or capture['source'].get('graph_id') != manifest['arguments'].get('graph')
                    or capture['source'].get('graph_id') != capture['arguments'].get('graph')):
                raise ValueError()
            research_graphs.validate(capture['operation'], manifest['arguments'])
        for artifact in manifest['context']['source_artifacts'].values():
            raw = files[artifact['path']]
            if artifact['sha256'] != sha256(raw) or artifact['size_bytes'] != len(raw):
                raise ValueError()
        return capture, files
    except Exception:
        raise Problem(409, 'PUBLIC_CAPTURE_INTEGRITY', 'Retained public evidence bytes failed verification.') from None


def _descriptor(record, item):
    return {**deepcopy(item), 'download_url': public_base()+'/v1/public-research/captures/'+record['id']+'/artifacts/'+item['sha256'],
        'authentication': 'none', 'expires_at': record['expires_at']}


def _response(record):
    capture, _ = _verify(record)
    manifest = record['manifest']; context = manifest['context']
    return {'format': FORMAT, 'capture_id': record['id'], 'expires_at': record['expires_at'],
        'reference_generation_id': manifest['reference_generation_id'], 'operation': manifest['operation'],
        'arguments': deepcopy(capture['arguments']), 'reader_version': capture.get('reader_version'),
        'source_mode': capture['source_mode'], 'source': deepcopy(capture['source']), 'result': deepcopy(capture['result']),
        'raw_sha256': manifest['raw_sha256'], 'metric_definitions': deepcopy(capture.get('metric_definitions', {})),
        'dapper_context': deepcopy(context['dapper_context']), 'source_artifacts': deepcopy(context['source_artifacts']),
        'source_ref': deepcopy(context.get('source_ref')), 'dapper_file_id': context.get('dapper_file_id'),
        'object_resolution': deepcopy(context.get('object_resolution', [])),
        'artifacts': [_descriptor(record, item) for item in manifest['files']],
        'attachment_policy': ('Authenticate and attach before expires_at. This external graph must have been selected in the frozen research request. The exact retained observation is used without re-querying; it is not an imported CFDE generation.'
            if manifest['source_mode'] == 'external_kg' else
            'Authenticate and attach before expires_at. Exact captured bytes remain usable for the same frozen generation even if source tables are later unavailable; no source is re-queried.')}


def _capture(service, generation, operation, arguments):
    graph_query = operation in research_graphs.OPERATIONS
    if operation not in OPERATIONS and operation not in SmallModelBioIndex.INDEXES and not graph_query:
        raise Problem(422, 'INVALID_QUERY', 'Unknown public reference operation.')
    if not isinstance(arguments, dict) or len(canonical_json(arguments)) > 16_000:
        raise Problem(422, 'INVALID_QUERY', 'Use bounded public query arguments.')
    gate = setting('REVEAL_SMALL_PHENOTYPE_VERIFIED', 'false').lower() == 'true'
    if graph_query:
        if generation is not None:
            raise Problem(422, 'INVALID_QUERY', 'External knowledge graphs do not use imported reference generations.')
        research_graphs.validate(operation, arguments)
        query_key = digest([FORMAT, research_graphs.VERSION, None, operation, arguments])
    else:
        query_key = digest([FORMAT, READER_VERSION, SmallModelBioIndex.VERSION, gate, generation, operation, arguments])
    cached_record = None
    with service.repo.read_transaction() as tx:
        cached = tx.get('public_capture_cache', query_key)
        if cached and cached['owner'] == OWNER and cached['data']['expires_at'] > now():
            try:
                cached_record = _record(tx, cached['data']['capture_id'])
            except Problem as error:
                if error.code not in ('PUBLIC_CAPTURE_EXPIRED', 'PUBLIC_CAPTURE_NOT_FOUND'):
                    raise
    if cached_record is not None:
        return _response(cached_record)
    if graph_query:
        reader = service.graph_service or research_graphs.GraphQueryService()
    else:
        # The phenotype exception retains the existing frozen-generation binding,
        # while its scientific source explicitly remains external.
        _catalog(service, generation)
        reader = SmallModelBioIndex(verified=gate) if operation in SmallModelBioIndex.INDEXES else _reader(service)
    try:
        captured = reader.query(operation, arguments, generation_id=generation)
    except Problem:
        raise
    except Exception:
        raise Problem(503, 'SOURCE_UNAVAILABLE', 'The requested public scientific source is unavailable.') from None
    from .acceptance import public_runtime
    context = captured.materialize(public_runtime())
    files = context.pop('files')
    if not 1 <= len(files) <= MAX_FILES or sum(map(len, files.values())) > MAX_CAPTURE_BYTES or len(canonical_json(context)) > MAX_CONTEXT_BYTES:
        raise Problem(413, 'PUBLIC_CAPTURE_TOO_LARGE', 'The public reference capture exceeds its retention bounds.')
    manifest = {'format': FORMAT, 'reference_generation_id': generation, 'operation': operation,
        'arguments': deepcopy(arguments), 'source_mode': captured.source_mode, 'raw_sha256': sha256(captured.raw),
        'context': context, 'files': [{'path': path, 'sha256': sha256(raw), 'size_bytes': len(raw), 'media_type': 'application/json'}
            for path, raw in sorted(files.items())]}
    identity = sha256(canonical_json(manifest))
    retained_bytes = sum(map(len, files.values()))
    ttl = _bounded_setting('REVEAL_PUBLIC_CAPTURE_TTL_SECONDS', 7 * 86400, 60, 30 * 86400)
    # All instances use the repository write fence for quota reservations and
    # deduplication. Blob I/O happens outside it; failures keep their reservation
    # so retries cannot evade the durable storage cap with orphan uploads.
    # Expired records stay accounted for because shared CAS bytes may also be
    # referenced by accepted work. No unsafe global blob deletion happens here.
    lease = uid(); ready = None
    with service.repo.transaction() as tx:
        existing = tx.get('public_capture', identity)
        rows = tx.list('public_capture', OWNER)
        added_bytes = 0 if existing else retained_bytes
        if (sum(r['data']['size_bytes'] for r in rows) + added_bytes > _bounded_setting('REVEAL_PUBLIC_CAPTURE_MAX_BYTES', 256_000_000, 1, 2_000_000_000)
                or len(rows) + (0 if existing else 1) > _bounded_setting('REVEAL_PUBLIC_CAPTURE_MAX_RECORDS', 500, 1, 10000)):
            raise Problem(429, 'PUBLIC_CAPTURE_QUOTA', 'Public capture retention is full. Existing captures can still be read and attached.')
        if existing and existing['owner'] != OWNER:
            raise Problem(409, 'PUBLIC_CAPTURE_INTEGRITY', 'Capture identity belongs to another record scope.')
        if existing and existing['data'].get('state') == 'ready' and existing['data']['expires_at'] > now():
            ready = _record(tx, identity)
        else:
            if existing and existing['data'].get('state') == 'preparing' and existing['data'].get('lease_until', '') > now():
                raise Problem(409, 'PUBLIC_CAPTURE_PENDING', 'An identical capture is being retained; retry shortly.')
            record = {'id': identity, 'manifest': manifest, 'created_at': now(), 'expires_at': _expire(ttl),
                'state': 'preparing', 'lease_token': lease, 'lease_until': _expire(120), 'size_bytes': retained_bytes, 'storage': {}}
            tx.put('public_capture', identity, OWNER, record)
    if ready is not None:
        return _response(ready)
    storage = {path: user_inputs.retain(raw, 'application/json') for path, raw in files.items()}
    with service.repo.transaction() as tx:
        existing = tx.get('public_capture', identity)
        if not existing or existing['owner'] != OWNER or existing['data'].get('lease_token') != lease:
            raise Problem(409, 'PUBLIC_CAPTURE_PENDING', 'Another request resumed this capture; retry its read.')
        record = existing['data']
        record.update(state='ready', storage=storage)
        record.pop('lease_token', None); record.pop('lease_until', None)
        tx.put('public_capture', identity, OWNER, record)
        cache_expiry = min(record['expires_at'], _expire(300)) if graph_query or operation in SmallModelBioIndex.INDEXES else record['expires_at']
        tx.put('public_capture_cache', query_key, OWNER, {'capture_id': identity, 'expires_at': cache_expiry})
    return _response(record)


def public_dispatch(service, name, arguments, *, rate_key=None):
    tool = next((item for item in definitions() if item['name'] == name), None)
    if tool is None:
        raise Problem(404, 'UNKNOWN_TOOL', 'Unknown public research tool.')
    errors = list(Draft202012Validator(tool['inputSchema']).iter_errors(arguments))
    if errors:
        raise Problem(422, 'INVALID_ARGUMENTS', errors[0].message[:500])
    _rate_limit(service, rate_key)
    if name == 'list_knowledge_graphs':
        return research_graphs.catalog()
    if name in research_graphs.OPERATIONS:
        return _capture(service, None, name, arguments)
    if name in ('list_data_operations', 'describe_data_operation'):
        catalog = _catalog(service, arguments['reference_generation_id'])
        if name == 'list_data_operations':
            return catalog
        operation = arguments['operation_id']
        if operation in SmallModelBioIndex.INDEXES:
            return {**SmallModelBioIndex.descriptor(), 'name': operation}
        found = next((item for item in catalog['operations'] if item['name'] == operation), None)
        if found is None:
            raise Problem(404, 'OPERATION_UNAVAILABLE', 'Unknown operation for this public reference generation.')
        return {**found, 'generation': catalog['generation']}
    if name.startswith('find_') or name == 'get_scientific_object':
        from . import scientific_reuse
        with service.repo.read_transaction() as tx:
            if name == 'get_scientific_object':
                return scientific_reuse.public_get_object(tx, arguments['selection'])
            return scientific_reuse.public_search(tx,
                {'find_propositions': 'proposition', 'find_claims': 'claim', 'find_scientific_accounts': 'account'}[name],
                arguments.get('query', ''), arguments.get('filters'), arguments.get('limit', 20), arguments.get('offset', 0))
    if name == 'get_public_capture':
        with service.repo.read_transaction() as tx:
            record = _record(tx, arguments['capture_id'])
        return _response(record)
    return _capture(service, arguments['reference_generation_id'], arguments.get('operation_id', name), arguments['arguments'])


def capture_artifact(service, capture_id, artifact_sha256, *, rate_key=None):
    """HTTP adapter returns these bytes directly, never a storage reference."""
    _rate_limit(service, rate_key)
    with service.repo.read_transaction() as tx:
        record = _record(tx, capture_id)
    _, files = _verify(record)
    item = next((item for item in record['manifest']['files'] if item['sha256'] == artifact_sha256), None)
    if item is None:
        raise Problem(404, 'PUBLIC_CAPTURE_ARTIFACT_NOT_FOUND', 'The artifact is not part of this public capture.')
    return files[item['path']], _descriptor(record, item)


def _capture_ids(capture_ids):
    if (not isinstance(capture_ids, list) or not 1 <= len(capture_ids) <= 10
            or any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value) for value in capture_ids)
            or len(set(capture_ids)) != len(capture_ids)):
        raise Problem(422, 'INVALID_CAPTURE', 'Attach one to ten distinct exact public captures.')


def prepare_capture_attachments(service, capture_ids, *, authority=None):
    """Trusted in-process preparation; never accept this result from a client."""
    _capture_ids(capture_ids)
    records = {}
    with service.repo.read_transaction() as tx:
        for identity in capture_ids:
            if authority:
                receipt_id = digest([authority['owner'], authority['work']['id'], 'public-capture', identity])
                previous = tx.get('evidence_receipt', receipt_id)
                if previous and previous['owner'] == authority['owner']:
                    continue  # An existing authenticated attachment outlives public TTL.
            records[identity] = _record(tx, identity)
    return {identity: {'record': record, 'verified': _verify(record)} for identity, record in records.items()}


def attach_capture(service, tx, authority, capture_ids, idempotency_key, *, prepared=None):
    """Called inside the authenticated dispatcher's current write transaction.

    Imported captures must match the frozen generation. External graph captures
    instead require the exact graph to be selected in the frozen request. Neither
    path re-queries a source or reinterprets its retained scientific provenance.
    """
    owner = authority['owner']; work_id = authority['work']['id']
    valid_principal(tx, owner)
    work = owned(tx, 'local_work', work_id, owner)['data']
    request = owned(tx, 'request', work['research_request_id'], owner)['data']
    if work['state'] != 'ready' or work['expires_at'] <= now():
        raise Problem(409, 'WORK_CLOSED', 'The research work is not ready for evidence attachment.')
    _capture_ids(capture_ids)
    def action():
        new_receipts = []; artifacts = {}; receipts = []
        for identity in capture_ids:
            receipt_id = digest([owner, work_id, 'public-capture', identity])
            existing = tx.get('evidence_receipt', receipt_id)
            if existing:
                value = owned(tx, 'evidence_receipt', receipt_id, owner)['data']
                if value.get('local_work_id') != work_id or value.get('public_capture_id') != identity:
                    raise Problem(409, 'PUBLIC_CAPTURE_INTEGRITY', 'An existing capture attachment has different scope.')
                receipts.append({'capture_id': identity, 'receipt_id': receipt_id}); continue
            record = _record(tx, identity); manifest = record['manifest']
            if manifest['source_mode'] != 'external_kg' and manifest['reference_generation_id'] != work['reference_generation_id']:
                raise Problem(409, 'CAPTURE_GENERATION_MISMATCH', 'The public capture belongs to a different frozen reference generation.')
            saved = (prepared or {}).get(identity)
            if not saved or saved.get('record') != record:
                raise Problem(409, 'PUBLIC_CAPTURE_CHANGED', 'Prepare and verify these capture attachments before committing them.')
            capture, files = saved['verified']
            if manifest['source_mode'] == 'external_kg':
                selected = request.get('composer', {}).get('selected_kgs', [])
                if not isinstance(selected, list) or capture['source'].get('graph_id') not in selected:
                    raise Problem(403, 'GRAPH_NOT_SELECTED', 'This external graph was not selected in the frozen research request.')
            context = deepcopy(manifest['context']); context['artifact_ids'] = {}
            for path, raw in files.items():
                artifact_id = digest([work_id, 'source', sha256(raw), path])
                artifact = {'id': artifact_id, 'local_work_id': work_id, 'filename': path, 'sha256': sha256(raw),
                    'size_bytes': len(raw), 'storage': deepcopy(record['storage'][path]), 'purpose': 'source',
                    'metadata': {'research_request_id': work['research_request_id'], 'public_capture_id': identity}, 'created_at': now()}
                context['artifact_ids'][path] = artifact_id
                prior = tx.get('research_artifact', artifact_id)
                if prior:
                    if prior['owner'] != owner or any(prior['data'].get(key) != artifact[key] for key in ('local_work_id', 'sha256', 'size_bytes', 'filename', 'purpose')):
                        raise Problem(409, 'PUBLIC_CAPTURE_INTEGRITY', 'An existing source artifact has different scope.')
                else:
                    artifacts[artifact_id] = artifact
            new_receipts.append({'id': receipt_id, 'public_capture_id': identity, 'local_work_id': work_id,
                'research_request_id': work['research_request_id'], 'operation': capture['operation'],
                'arguments': deepcopy(capture['arguments']), 'source_mode': capture['source_mode'],
                'source': deepcopy(capture['source']), 'result': deepcopy(capture['result']), 'context': context,
                'created_at': now(), 'sha256': manifest['raw_sha256']})
            receipts.append({'capture_id': identity, 'receipt_id': receipt_id})
        used = sum(row['data']['size_bytes'] for row in tx.list('research_artifact', owner) if row['data']['local_work_id'] == work_id)
        if used + sum(item['size_bytes'] for item in artifacts.values()) > 128_000_000:
            raise Problem(429, 'RESEARCH_STORAGE_LIMIT', 'This research work reached its retained artifact budget.')
        for artifact_id, artifact in artifacts.items():
            tx.put('research_artifact', artifact_id, owner, artifact)
        for receipt in new_receipts:
            tx.put('evidence_receipt', receipt['id'], owner, receipt)
        return {'local_work_id': work_id, 'receipts': receipts}
    return idempotent(tx, owner, work_id+':attach-public-captures', idempotency_key, {'capture_ids': capture_ids}, action)
