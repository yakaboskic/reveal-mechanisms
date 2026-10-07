"""Shared, explicit MCP tool contracts and authorization-aware dispatch."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .auth import Problem, owned
from .repository import digest, now, uid
from .research_work import authenticate, idempotent, operation_counts, public_base, MAX_ARTIFACT_BYTES
from . import user_inputs

STRING = {'type': 'string', 'minLength': 1, 'maxLength': 1000}
IDS = {'type': 'array', 'items': STRING, 'maxItems': 100, 'uniqueItems': True}
OBJECT = {'type': 'object'}
KEY = {'type': 'string', 'minLength': 1, 'maxLength': 200}
WORK = {'local_work_id': STRING}
REQUEST = {'research_request_id': STRING}


def schema(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


def private_definitions():
    result = []
    def add(name, description, props, required, mutation=False):
        result.append({'name': name, 'description': description, 'inputSchema': schema(props, required),
            'annotations': {'readOnlyHint': not mutation, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}})
    add('get_local_work', 'Get the frozen draft, current work state, connection scope and submission results.', WORK, ['local_work_id'])
    add('get_research_package', 'Download the small immutable seed and authoring-kit manifest. Large files have separate authorized downloads.', WORK, ['local_work_id'])
    add('get_artifact_download', 'Get an authenticated download descriptor for one authorized immutable artifact.', {**WORK, 'artifact_id': STRING}, ['local_work_id', 'artifact_id'])
    from .evidence_reader import TOOL_DEFINITION
    reader = TOOL_DEFINITION['inputSchema']
    add('read_evidence', TOOL_DEFINITION['description'], {**WORK, **reader['properties']},
        ['local_work_id', *reader.get('required', [])])
    # Keep mode-dependent bounds from the standalone/offline reader contract.
    result[-1]['inputSchema'] = {**deepcopy(reader), **result[-1]['inputSchema']}
    from .research_graphs import definitions as graph_definitions
    add('list_knowledge_graphs', 'Discover the connected knowledge graphs selected for this frozen research request.', REQUEST, ['research_request_id'])
    for tool in graph_definitions():
        fields = tool['inputSchema']
        add(tool['name'], tool['description'] + ' Authenticated calls retain a work-scoped receipt; poll get_operation.',
            {**REQUEST, **fields['properties'], 'idempotency_key': KEY},
            ['research_request_id', *fields.get('required', []), 'idempotency_key'], True)
    add('list_data_operations', 'Discover data operations over the pinned app import and the two small/sigma=2 phenotype exceptions.', REQUEST, ['research_request_id'])
    add('describe_data_operation', 'Inspect arguments, metrics and coverage of a registered operation.', {**REQUEST, 'operation_id': STRING}, ['research_request_id', 'operation_id'])
    add('query_data', 'Capture a bounded query using an explicit operation ID; poll get_operation for its authoritative receipt. No automatic external fallback.',
        {**REQUEST, 'operation_id': STRING, 'arguments': OBJECT, 'idempotency_key': KEY}, ['research_request_id', 'operation_id', 'arguments', 'idempotency_key'], True)
    from .research_data import OPERATIONS, SmallModelBioIndex
    for name, fields in OPERATIONS.items():
        props = {key: STRING for key in fields}
        props.update(limit={'type': 'integer', 'minimum': 1, 'maximum': 500}, cursor=STRING)
        add(name, 'Query loaded reference data using the pinned generation. Capture returns an operation ID; inspect its receipt and coverage before citing.',
            {**REQUEST, 'arguments': schema(props, []), 'idempotency_key': KEY}, ['research_request_id', 'arguments', 'idempotency_key'], True)
    for name in SmallModelBioIndex.INDEXES:
        add(name, 'Explicit gated BioIndex phenotype observation, fixed model small and sigma 2. Pass only phenotype_id in arguments, then inspect returned gene or GeneSet rows. Availability is separate from imported app data; this call does not verify deployment access.',
            {**REQUEST, 'arguments': SmallModelBioIndex.arguments_schema(), 'idempotency_key': KEY}, ['research_request_id', 'arguments', 'idempotency_key'], True)
    for name in ('find_propositions', 'find_claims', 'find_scientific_accounts'):
        add(name, 'Search owner-accepted and currently public science. Exact matches and lexical candidates are distinct; inspect scope before reuse.',
            {**REQUEST, 'query': {'type': 'string', 'maxLength': 1000}, 'filters': OBJECT,
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10000}}, ['research_request_id'])
    add('get_scientific_object', 'Inspect an exact source-qualified scientific observation and its authorized dependencies.',
        {**REQUEST, 'selection': OBJECT}, ['research_request_id', 'selection'])
    add('reuse_scientific_objects', 'Pin unchanged objects and their complete authorized scientific dependencies. Preserve original authorship and citations.',
        {**REQUEST, 'selections': {'type': 'array', 'items': OBJECT, 'minItems': 1, 'maxItems': 30}, 'idempotency_key': KEY}, ['research_request_id', 'selections', 'idempotency_key'], True)
    add('prepare_artifact_upload', 'Prepare bounded account/evidence uploads. Transfer bytes to each returned URL using the same research bearer credential.',
        {**WORK, 'purpose': {'enum': ['account', 'evidence']}, 'files': {'type': 'array', 'minItems': 1, 'maxItems': 3,
            'items': schema({'filename': STRING, 'sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'size_bytes': {'type': 'integer', 'minimum': 1, 'maximum': MAX_ARTIFACT_BYTES}}, ['filename', 'sha256', 'size_bytes'])}, 'idempotency_key': KEY}, ['local_work_id', 'purpose', 'files', 'idempotency_key'], True)
    add('complete_artifact_upload', 'Verify uploaded bytes and freeze immutable artifact IDs. Staged bytes are never accepted as scientific evidence directly.',
        {**WORK, 'upload_ids': IDS, 'idempotency_key': KEY}, ['local_work_id', 'upload_ids', 'idempotency_key'], True)
    add('import_evidence', 'Import independently collected or locally derived evidence with explicit unverified provenance before citing it.',
        {**WORK, 'artifact_ids': IDS, 'metadata': OBJECT, 'receipt_ids': IDS, 'import_ids': IDS, 'reuse_receipt_ids': IDS, 'idempotency_key': KEY}, ['local_work_id', 'artifact_ids', 'metadata', 'idempotency_key'], True)
    add('get_evidence_import', 'Get a completed evidence import and source records.', {**WORK, 'import_id': STRING}, ['local_work_id', 'import_id'])
    add('get_evidence_result', 'Replay a captured query receipt without querying its source again.', {**REQUEST, 'receipt_id': STRING}, ['research_request_id', 'receipt_id'])
    add('export_evidence_context', 'Start a durable evidence export; immediately returns operation_id. Poll get_operation for a compact manifest, then download missing files by verified checksum. No scientific source is re-queried.',
        {**WORK, 'receipt_ids': IDS, 'import_ids': IDS, 'reuse_receipt_ids': IDS, 'idempotency_key': KEY}, ['local_work_id', 'idempotency_key'], True)
    add('attach_public_captures', 'After signing in, attach exact server-verified public captures to this research work. No data is re-queried; captures must match the frozen generation or selected external graph and be unexpired.',
        {**WORK, 'capture_ids': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'minItems': 1, 'maxItems': 10, 'uniqueItems': True}, 'idempotency_key': KEY}, ['local_work_id', 'capture_ids', 'idempotency_key'], True)
    for name, description in (
        ('validate_submission', 'Validate uploaded account drafts and selected evidence. Retains private validation results and normalized artifacts; does not accept accounts or publish.'),
        ('submit_accounts', 'Validate and accept a bounded batch of new accounts and/or exact-question existing-account selections into private research. Does not publish.')):
        add(name, description,
            {**WORK, 'package_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}, 'account_artifact_ids': IDS,
             'receipt_ids': IDS, 'import_ids': IDS, 'reuse_receipt_ids': IDS, 'existing_account_ids': IDS,
             'client_runtime': OBJECT, 'idempotency_key': KEY}, ['local_work_id', 'package_sha256', 'idempotency_key'], True)
    for name, key in (('get_operation', 'operation_id'), ('get_submission', 'submission_id')):
        add(name, 'Poll durable operation state; polling resumes pending work after an API restart.', {**WORK, key: STRING}, ['local_work_id', key])
    add('retry_operation', 'Retry failed infrastructure processing against the same immutable inputs; rejected science needs a repaired new submission.',
        {**WORK, 'operation_id': STRING, 'idempotency_key': KEY}, ['local_work_id', 'operation_id', 'idempotency_key'], True)
    return result


def public_call(name, arguments):
    from .research_public import definitions as public_definitions
    return (name in {tool['name'] for tool in public_definitions()} and isinstance(arguments, dict)
            and 'research_request_id' not in arguments and 'local_work_id' not in arguments)


def definitions():
    """One catalog with explicit anonymous and owner-bound argument variants."""
    from .research_public import definitions as public_definitions
    from .research_graphs import OPERATIONS as GRAPH_OPERATIONS
    result = {tool['name']: tool for tool in private_definitions()}
    private_descriptions = {name: tool['description'] for name, tool in result.items()}
    for public in public_definitions():
        name = public['name']
        if name in result:
            private = result[name]
            old_props, new_props = private['inputSchema']['properties'], public['inputSchema']['properties']
            merged_props = {**old_props, **new_props}
            for key in old_props.keys() & new_props.keys():
                if old_props[key] != new_props[key]: merged_props[key] = {'anyOf': [old_props[key], new_props[key]]}
            private['inputSchema'] = {'type': 'object', 'properties': merged_props,
                'anyOf': [public['inputSchema'], private['inputSchema']]}
            if name in GRAPH_OPERATIONS:
                suffix = ' With research_request_id, use only this research request\'s selected graphs; supply idempotency_key for a retained work receipt.'
            elif name == 'list_knowledge_graphs':
                suffix = ' With research_request_id, show this frozen research request\'s selected graphs.'
            elif 'reference_generation_id' in public['inputSchema']['properties']:
                suffix = ' Supply research_request_id instead of reference_generation_id for authenticated owner-scoped research and retained work receipts.'
            else:
                suffix = ' With an authenticated research_request_id, also search your own accepted and shared research.'
            private['description'] = public['description'] + suffix
        else:
            result[name] = public
        result[name]['securitySchemes'] = [{'type': 'noauth'}, {'type': 'oauth2', 'scopes': ['research:read', 'research:write']}]
    for tool in result.values():
        if 'securitySchemes' not in tool:
            scopes = ['research:read'] + ([] if tool['annotations']['readOnlyHint'] else ['research:write'])
            tool['securitySchemes'] = [{'type': 'oauth2', 'scopes': scopes}]
            tool['description'] += ' Requires Reveal sign-in; results remain private and are not published automatically.'
        tool['_meta'] = {'securitySchemes': tool['securitySchemes']}
        if tool['name'] in private_descriptions:
            tool['_meta']['reveal/hostedDescription'] = private_descriptions[tool['name']]
    return list(result.values())


def first_authentication(tx, authorization, *, registered=False):
    """A read's only authentication: the transport no longer pre-checks reads, so a failure here is tagged
    for it to answer with the HTTP challenge (status, JSON body, WWW-Authenticate) it used to send."""
    try:
        authority = authenticate(tx, authorization)
        if registered:
            from .research_oauth import require_registered_local
            require_registered_local(authority)
        return authority
    except Problem as error:
        error.mcp_challenge = True
        raise


def check_child(tx, kind, identity, authority):
    value = owned(tx, kind, identity, authority['owner'])['data']
    if value.get('local_work_id') != authority['work']['id']:
        raise Problem(404, 'NOT_FOUND', 'This artifact or operation is not part of the authorized research work.')
    return value


def resolve_reader_artifact(tx, authority, identity, checksum):
    """Resolve only this work's retained inventory; names never become paths."""
    owner, work = authority['owner'], authority['work']
    direct = tx.get('research_artifact', identity)
    if direct:
        return check_child(tx, 'research_artifact', identity, authority)
    candidates = []
    if identity == 'package:'+checksum:
        candidates = [row['data'] for row in tx.list('research_artifact', owner)
            if row['data']['local_work_id'] == work['id'] and row['data']['sha256'] == checksum
            and row['data']['filename'] == 'evidence-package.json'
            and row['data']['purpose'] in ('seed', 'validation-context')]
    else:
        package = owned(tx, 'research_package', work['package_id'], owner)['data']
        source = package['package'].get('source_artifacts', {}).get(identity)
        if source and source['sha256'] == checksum:
            candidates += [owned(tx, 'research_artifact', item['id'], owner)['data']
                for item in package['artifacts'] if item['filename'] == source['path'] and item['sha256'] == checksum]
        for kind in ('evidence_receipt', 'evidence_import'):
            for row in tx.list(kind, owner):
                record = row['data']
                if record.get('local_work_id') != work['id'] or record.get('research_request_id') != work['research_request_id']: continue
                context = record.get('context', {}); source = context.get('source_artifacts', {}).get(identity)
                if source and source['sha256'] == checksum:
                    artifact_id = context.get('artifact_ids', {}).get(source['path'])
                    if artifact_id: candidates.append(check_child(tx, 'research_artifact', artifact_id, authority))
        if identity == 'reuse-'+checksum:
            candidates += [row['data'] for row in tx.list('research_artifact', owner)
                if row['data']['local_work_id'] == work['id'] and row['data']['sha256'] == checksum
                and row['data']['filename'] == 'reuse/'+checksum]
    if not candidates: raise Problem(404, 'NOT_FOUND', 'Artifact is absent from the authorized evidence inventory.')
    from .research_execution import authorize_artifact
    for candidate in sorted(candidates, key=lambda item: item['id']):
        if candidate['local_work_id'] != work['id']: continue
        authorize_artifact(tx, owner, candidate)
        return candidate
    raise Problem(404, 'NOT_FOUND', 'Artifact is absent from the authorized evidence inventory.')


def data_capabilities(service, authorization, name, arguments):
    """Resolve the actual pinned source without holding an app transaction."""
    with service.repo.read_transaction() as tx:
        authority=first_authentication(tx,authorization,registered=True)
        work=authority['work']
        if arguments['research_request_id'] != work['research_request_id']:
            raise Problem(404,'NOT_FOUND','The requested research context is unavailable.')
        generation=work['reference_generation_id']
    from .research_data import ReferenceQueryService, SmallModelBioIndex
    from .runtime_config import reference_mysql_connection
    reader=service.data_service or ReferenceQueryService(reference_mysql_connection)
    try: catalog=reader.catalog(generation)
    except Problem: raise
    except Exception:
        raise Problem(503,'SOURCE_UNAVAILABLE','The pinned reference capability metadata is unavailable.') from None
    # Revocation or ownership changes while metadata is read cannot authorize a
    # response from a now-invalid credential.
    with service.repo.read_transaction() as tx:
        current=authenticate(tx,authorization)
        if current['work']['id'] != work['id'] or current['work']['reference_generation_id'] != generation:
            raise Problem(409,'RESEARCH_SCOPE_MISMATCH','The research context changed during the metadata read.')
    catalog['phenotype_source']=SmallModelBioIndex.descriptor()
    if name=='describe_data_operation':
        if arguments['operation_id'] in SmallModelBioIndex.INDEXES:
            return {**SmallModelBioIndex.descriptor(),'name':arguments['operation_id']}
        found=next((item for item in catalog['operations'] if item['name']==arguments['operation_id']),None)
        if found is None: raise Problem(404,'OPERATION_UNAVAILABLE','Unknown operation for this source registry.')
        return {**found,'generation':catalog['generation']}
    return catalog


def dispatch(service, authorization, name, arguments, *, rate_key=None, on_operation=None):
    tools = {t['name']: t for t in definitions()}
    if name not in tools: raise Problem(404, 'UNKNOWN_TOOL', 'Unknown research tool.')
    errors = list(Draft202012Validator(tools[name]['inputSchema']).iter_errors(arguments))
    if errors: raise Problem(422, 'INVALID_ARGUMENTS', errors[0].message[:500])
    if public_call(name, arguments):
        # No owner lookup or durable private work is needed for public research.
        # Supplied invalid credentials must never silently downgrade access.
        if authorization:
            with service.repo.read_transaction() as tx: first_authentication(tx, authorization)
        from .research_public import public_dispatch
        return public_dispatch(service, name, arguments, rate_key=rate_key)
    from .research_oauth import require_registered_local
    if name in ('list_data_operations','describe_data_operation'):
        return data_capabilities(service,authorization,name,arguments)
    if name == 'read_evidence':
        from .research_execution import authorize_artifact, read_artifact_bytes
        from .evidence_reader import read_artifact
        with service.repo.read_transaction() as tx:
            authority = first_authentication(tx, authorization, registered=True)
            if arguments['local_work_id'] != authority['work']['id']:
                raise Problem(404, 'NOT_FOUND', 'The requested research context is unavailable.')
            artifact = resolve_reader_artifact(tx, authority, arguments['artifact_id'], arguments['sha256'])
            authorize_artifact(tx, authority['owner'], artifact)
        try:
            result = read_artifact(read_artifact_bytes(artifact), {**artifact, 'artifact_id': arguments['artifact_id']},
                **{key: value for key, value in arguments.items() if key != 'local_work_id'})
        except ValueError as error:
            raise Problem(422, 'EVIDENCE_READ_INVALID', str(error)) from None
        with service.repo.read_transaction() as tx:
            current = authenticate(tx, authorization)
            artifact = check_child(tx, 'research_artifact', artifact['id'], current)
            authorize_artifact(tx, current['owner'], artifact)
        return result
    mutation = not tools[name]['annotations']['readOnlyHint']
    prepared_captures = None
    if name == 'attach_public_captures':
        # Authenticate before touching retained bytes; recheck at commit below.
        with service.repo.read_transaction() as tx:
            authority = authenticate(tx, authorization, write=True)
            if arguments['local_work_id'] != authority['work']['id']:
                raise Problem(404, 'NOT_FOUND', 'The requested research context is unavailable.')
        from .research_public import prepare_capture_attachments
        prepared_captures = prepare_capture_attachments(service, arguments['capture_ids'], authority=authority)
    transaction = service.repo.transaction if mutation else service.repo.read_transaction
    with transaction() as tx:
        if mutation:   # the transport pre-checked this write outside the fence; recheck under it
            authority = authenticate(tx, authorization, write=True)
            require_registered_local(authority)
        else: authority = first_authentication(tx, authorization, registered=True)
        owner, work = authority['owner'], authority['work']
        if arguments.get('local_work_id', work['id']) != work['id'] or arguments.get('research_request_id', work['research_request_id']) != work['research_request_id']:
            raise Problem(404, 'NOT_FOUND', 'The requested research context is unavailable.')
        if authority['grant']['kind'] == 'hosted' and name in ('submit_accounts', 'prepare_artifact_upload', 'complete_artifact_upload', 'import_evidence'):
            raise Problem(403, 'HOSTED_TOOL_SCOPE', 'Hosted results must pass through the trusted output capture and worker acceptance.')
        if name in ('get_local_work', 'get_research_package'):
            if on_operation and work['state'] == 'preparing':
                on_operation(work['preparation_operation_id'])
            return service.view(tx, owner, work['id']) if name == 'get_local_work' else service.package(tx, owner, work['id'])
        if name == 'list_knowledge_graphs':
            from .research_graphs import catalog
            request = owned(tx, 'request', work['research_request_id'], owner)['data']
            return catalog(selected_graphs=request.get('composer', {}).get('selected_kgs', []))
        if name == 'get_artifact_download':
            artifact = check_child(tx, 'research_artifact', arguments['artifact_id'], authority)
            from .research_execution import authorize_artifact
            authorize_artifact(tx, owner, artifact)
            return {**service.artifact_view(artifact), 'url': public_base()+'/v1/research-artifacts/'+artifact['id']+'/content',
                'method': 'GET', 'authentication': 'Use the same Authorization bearer credential as MCP; never include it in the URL.'}
        if name in ('get_operation', 'get_submission'):
            identity = arguments.get('operation_id') or arguments['submission_id']
            operation = check_child(tx, 'research_operation', identity, authority)
            result = service.operation_view(operation, tx=tx, owner=owner, strict=True)
            if on_operation and operation['state'] == 'received': on_operation(identity)
            elif on_operation and operation['state'] == 'running' and operation.get('lease_until', '') <= now():
                on_operation(identity, lease_token=operation.get('lease_token'))
            return result
        if name == 'get_evidence_result':
            return check_child(tx, 'evidence_receipt', arguments['receipt_id'], authority)
        if name == 'get_evidence_import':
            imported = check_child(tx, 'evidence_import', arguments['import_id'], authority)
            from .research_execution import authorize_import
            authorize_import(tx, owner, imported)
            return imported
        if name == 'attach_public_captures':
            from .research_public import attach_capture
            return attach_capture(service, tx, authority, arguments['capture_ids'], arguments['idempotency_key'], prepared=prepared_captures)
        if name.startswith('find_') or name in ('get_scientific_object', 'reuse_scientific_objects'):
            from . import scientific_reuse
            if name == 'get_scientific_object': return scientific_reuse.get_object(tx, owner, arguments['selection'])
            if name == 'reuse_scientific_objects':
                receipt = scientific_reuse.create_receipt(tx, owner, work['research_request_id'], arguments['selections'], arguments['idempotency_key'])
                return {'format': 'reveal.scientific-reuse/1', 'reuse_receipt_id': receipt['id'], 'id': receipt['id'],
                    'selections': [{'selection': context['selection'], 'purpose': context['purpose'],
                        'closure_sha256': context['closure_sha256']} for context in receipt['selections']]}
            return scientific_reuse.search(tx, owner, {'find_propositions': 'proposition', 'find_claims': 'claim', 'find_scientific_accounts': 'account'}[name],
                arguments.get('query', ''), filters=arguments.get('filters'), limit=arguments.get('limit', 20), offset=arguments.get('offset', 0))
        key = arguments['idempotency_key']; body = {k: v for k, v in arguments.items() if k != 'idempotency_key'}
        def action():
            if name == 'prepare_artifact_upload':
                pending = [r for r in tx.list('research_upload', owner) if r['data']['state'] == 'pending' and r['data']['expires_at'] > now()]
                if len(pending) + len(arguments['files']) > 10:
                    raise Problem(429, 'UPLOAD_LIMIT', 'Complete existing transfers before preparing more.')
                total = sum(r['data']['size_bytes'] for r in tx.list('research_artifact', owner) if r['data']['local_work_id'] == work['id'])
                total += sum(r['data']['size_bytes'] for r in pending if r['data']['local_work_id'] == work['id'])
                if total + sum(f['size_bytes'] for f in arguments['files']) > 128_000_000:
                    raise Problem(429, 'RESEARCH_STORAGE_LIMIT', 'This research work reached its retained artifact budget.')
                from .research_work import deadline
                result = []
                for file in arguments['files']:
                    filename = file['filename']
                    if Path(filename).name != filename or '\\' in filename or any(ord(c) < 32 for c in filename):
                        raise Problem(422, 'INVALID_UPLOAD', 'Use a plain filename.')
                    if Path(filename).suffix.lower() not in user_inputs.TYPES:
                        raise Problem(422, 'UNSUPPORTED_UPLOAD', 'Unsupported account/evidence format.')
                    if arguments['purpose'] == 'account' and (file['size_bytes'] > 4_000_000 or Path(filename).suffix.lower() not in ('.json', '.yaml', '.yml')):
                        raise Problem(422, 'INVALID_ACCOUNT_UPLOAD', 'Accounts must be JSON/YAML documents of at most 4 MB.')
                    upload = {**file, 'id': uid(), 'local_work_id': work['id'], 'state': 'pending', 'purpose': arguments['purpose'],
                        'created_at': now(), 'expires_at': deadline(3600)}
                    tx.put('research_upload', upload['id'], owner, upload)
                    result.append({'upload_id': upload['id'], 'url': public_base()+'/v1/research-uploads/'+upload['id']+'/content',
                        'method': 'PUT', 'encoding': 'raw', 'authentication': 'Same bearer credential as MCP.'})
                return {'uploads': result}
            if name == 'complete_artifact_upload':
                result = []
                for identity in arguments['upload_ids']:
                    upload = check_child(tx, 'research_upload', identity, authority)
                    if not upload.get('storage'): raise Problem(409, 'UPLOAD_INCOMPLETE', 'Transfer the bytes before completing this upload.')
                    # The transfer endpoint verifies size/hash before immutable retention.
                    artifact = {k: upload[k] for k in ('filename', 'sha256', 'size_bytes', 'purpose', 'storage', 'local_work_id', 'created_at')}
                    artifact.update(id=identity, metadata={})
                    tx.put('research_artifact', identity, owner, artifact)
                    upload['state'] = 'complete'; tx.put('research_upload', identity, owner, upload)
                    result.append(service.artifact_view(artifact))
                return {'artifacts': result}
            if name == 'retry_operation':
                operation = check_child(tx, 'research_operation', arguments['operation_id'], authority)
                if operation['state'] != 'failed': raise Problem(409, 'RETRY_NOT_AVAILABLE', 'Only failed infrastructure operations can retry the same input.')
                if operation_counts(tx, owner, work['id'])[1] >= 8:
                    raise Problem(429, 'RESEARCH_BUSY', 'Wait for pending operations to finish.')
                operation.update(state='received', error=None, grant_id=authority['grant']['grant_id'], owner_user_id=owner)
                operation.pop('lease_token', None); operation.pop('lease_until', None)
                tx.put('research_operation', operation['id'], owner, operation)
                return {'operation_id': operation['id'], 'state': 'received'}
            if name in ('validate_submission', 'submit_accounts'):
                if arguments['package_sha256'] != work['package_sha256']:
                    raise Problem(409, 'PACKAGE_MISMATCH', 'Use the exact frozen package hash.')
                count = len(arguments.get('account_artifact_ids', [])) + len(arguments.get('existing_account_ids', []))
                if not 1 <= count <= 3: raise Problem(422, 'ACCOUNT_LIMIT', 'Submit one to three new or reused accounts.')
                for identity in arguments.get('account_artifact_ids', []):
                    artifact = check_child(tx, 'research_artifact', identity, authority)
                    if artifact['purpose'] != 'account': raise Problem(422, 'INVALID_ARTIFACT', 'Expected an account upload.')
                kind = 'validate' if name == 'validate_submission' else 'submit'
            elif name == 'export_evidence_context':
                kind = 'export'
            elif name == 'import_evidence':
                if not arguments['artifact_ids']: raise Problem(422, 'IMPORT_EMPTY', 'Select evidence artifacts.')
                for identity in arguments['artifact_ids']:
                    if check_child(tx, 'research_artifact', identity, authority)['purpose'] != 'evidence':
                        raise Problem(422, 'INVALID_ARTIFACT', 'Expected an evidence upload.')
                kind = 'import'
            else:
                kind = 'query'
                body['operation_id'] = arguments.get('operation_id', name)
                from .research_graphs import OPERATIONS as GRAPH_OPERATIONS, validate as validate_graph
                if body['operation_id'] in GRAPH_OPERATIONS:
                    if name != 'query_data':
                        body['arguments'] = {key: value for key, value in arguments.items()
                            if key not in ('research_request_id', 'idempotency_key')}
                        normalized = {key: body[key] for key in ('research_request_id', 'operation_id', 'arguments')}
                        body.clear(); body.update(normalized)
                    request = owned(tx, 'request', work['research_request_id'], owner)['data']
                    validate_graph(body['operation_id'], body['arguments'],
                        selected_graphs=request.get('composer', {}).get('selected_kgs', []))
            # Counted in SQL: stored results never cross the wire under the write fence.
            total, pending = operation_counts(tx, owner, work['id'])
            if total >= 300: raise Problem(429, 'RESEARCH_BUDGET_EXCEEDED', 'This work reached its operation budget.')
            if pending >= 8: raise Problem(429, 'RESEARCH_BUSY', 'Wait for pending operations to finish.')
            operation = service.enqueue(tx, owner, work, kind, body, authority['grant']['grant_id'])
            work.update(last_activity=now(), last_action=name); tx.put('local_work', work['id'], owner, work)
            return {'operation_id': operation['id'], 'submission_id': operation['id'] if kind in ('validate', 'submit') else None, 'state': 'received'}
        result = idempotent(tx, owner, work['id']+':'+name, key, body, action)
    if on_operation and result.get('operation_id'): on_operation(result['operation_id'])
    return result
