"""Captured research inputs, immutable validation contexts and atomic results."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from .auth import Problem, owned
from .repository import digest, now
from .evidence_package import canonical_json, sha256
from .runtime_config import ROOT, artifacts_root, mysql_connection, setting
from .research_work import authorize_commit, public_base
from .runtime_metrics import measure
from . import user_inputs


def authorize_artifact(tx, owner, artifact):
    from .scientific_reuse import resolve_receipts
    metadata = artifact.get('metadata', {})
    if metadata.get('reuse_receipt_ids'):
        resolve_receipts(tx, owner, metadata['research_request_id'], metadata['reuse_receipt_ids'])


def authorize_import(tx, owner, imported):
    from .scientific_reuse import resolve_receipts
    if imported.get('reuse_receipt_ids'):
        resolve_receipts(tx, owner, imported['research_request_id'], imported['reuse_receipt_ids'])


def authorize_context_selection(tx, owner, work_id, request_id, arguments, *, resolved=None):
    """Recheck the complete inherited authorization behind a retained response.

    resolved memoizes resolve_receipts (read-only and deterministic for one snapshot), including its
    failure, per receipt set; share it only within one transaction.
    """
    from .scientific_reuse import resolve_receipts
    identities = set(arguments.get('reuse_receipt_ids', []))
    for field, kind in (('import_ids', 'evidence_import'), ('receipt_ids', 'evidence_receipt')):
        for identity in set(arguments.get(field, [])):
            imported = owned(tx, kind, identity, owner)['data']
            if imported['local_work_id'] != work_id or imported['research_request_id'] != request_id:
                raise Problem(404, 'NOT_FOUND', 'The evidence belongs to different research work.')
            identities.update(imported.get('reuse_receipt_ids', []))
    if not identities: return
    if resolved is None:
        resolve_receipts(tx, owner, request_id, sorted(identities)); return
    key = (request_id, tuple(sorted(identities)))
    if key not in resolved:
        try: resolve_receipts(tx, owner, request_id, list(key[1])); resolved[key] = None
        except Problem as error: resolved[key] = error
    if resolved[key] is not None: raise resolved[key]


def authorize_operation_result(tx, owner, operation, *, resolved=None):
    authorize_context_selection(tx, owner, operation['local_work_id'], operation['research_request_id'],
        operation.get('arguments', {}), resolved=resolved)
    if operation['kind'] == 'import':
        row = tx.get('evidence_import', operation['id'])
        if row:
            if row['owner'] != owner: raise Problem(404, 'NOT_FOUND', 'The evidence import is unavailable.')
            authorize_import(tx, owner, row['data'])


def _read_record(record):
    if record.get('storage'):
        ref = record['storage']
        if ref.get('store') == 'filesystem': return user_inputs.read(ref)
        from .artifact_store import store
        return store().get(ref)
    path = Path(record.get('path', '')).resolve()
    if not path.is_relative_to(artifacts_root().resolve()):
        raise Problem(409, 'REUSE_DEPENDENCY_UNAVAILABLE', 'Retained evidence is unavailable.')
    data = path.read_bytes()
    if sha256(data) != record['sha256']:
        raise Problem(409, 'REUSE_PAYLOAD_CONFLICT', 'Retained evidence checksum differs.')
    return data


def _record(file, ref, work_id, source=None):
    retained = {'storage': ref} if ref['store'] == 's3' else {'path': ref['key']}
    return {'sha256': file['sha256'], 'file': deepcopy(file), **retained,
            'job_id': work_id, 'research_source': deepcopy(source or {})}


def read_artifact_bytes(artifact):
    raw = _read_record(artifact['retained_record']) if artifact.get('retained_record') else user_inputs.read(artifact['storage'])
    if sha256(raw) != artifact['sha256'] or len(raw) != artifact['size_bytes']:
        raise Problem(409, 'SOURCE_UNAVAILABLE', 'Retained evidence bytes changed.')
    return raw


def retain_context(service, operation, context):
    context = deepcopy(context); files = context.pop('files', {})
    context['artifact_ids'] = {}
    for relative, data in files.items():
        if not isinstance(data, bytes): raise ValueError('Capture file must contain bytes')
        artifact = service.retain(operation['owner_user_id'], operation['local_work_id'], data, relative,
            metadata={'research_request_id': operation['research_request_id']})
        context['artifact_ids'][relative] = artifact['id']
    return context


def capture_query(service, operation):
    from .acceptance import public_runtime
    from .research_data import ReferenceQueryService, SmallModelBioIndex
    args = operation['arguments']; owner = operation['owner_user_id']
    with measure('research_query', 'context_read'), service.repo.read_transaction() as tx:
        work = owned(tx, 'local_work', operation['local_work_id'], owner)['data']
        previous = tx.get('evidence_receipt', operation['id'])
        request = owned(tx, 'request', work['research_request_id'], owner)['data']
    def response(receipt):
        context = receipt['context']
        return {'receipt_id': receipt['id'], 'source_mode': receipt['source_mode'], 'source': receipt['source'],
            'result': receipt['result'], 'dapper_context': context['dapper_context'],
            'source_ref': context.get('source_ref'), 'object_resolution': context.get('object_resolution', []),
            'reference_objects': context.get('reference_objects', []), 'artifacts': context['artifact_ids']}
    if previous: return response(previous['data'])
    query_service = service.data_service or ReferenceQueryService(mysql_connection,
        cursor_secret=setting('REVEAL_RESEARCH_CURSOR_SECRET', setting('REVEAL_GATEWAY_SECRET', '')))
    name = args['operation_id']
    if name in SmallModelBioIndex.INDEXES:
        query_service = SmallModelBioIndex(verified=setting('REVEAL_SMALL_PHENOTYPE_VERIFIED', 'false').lower() == 'true')
    from .research_graphs import OPERATIONS as GRAPH_OPERATIONS, GraphQueryService
    with measure('research_query', 'source'):
        if name in GRAPH_OPERATIONS:
            query_service = service.graph_service or GraphQueryService()
            capture = query_service.query(name, args.get('arguments', {}),
                selected_graphs=request.get('composer', {}).get('selected_kgs', []))
        else:
            capture = query_service.query(name, args.get('arguments', {}), generation_id=work['reference_generation_id'])
    with measure('research_query', 'materialization'):
        context = capture.materialize(public_runtime())
    with measure('research_query', 'retention'):
        context = retain_context(service, operation, context)
    receipt = {'id': operation['id'], 'local_work_id': work['id'], 'research_request_id': work['research_request_id'],
        'operation': name, 'arguments': args.get('arguments', {}), 'source_mode': capture.source_mode,
        'source': capture.source, 'result': capture.result, 'context': context, 'created_at': now(),
        'sha256': sha256(capture.raw)}
    with measure('research_query', 'receipt_commit'), service.repo.transaction() as tx:
        authorize_commit(tx, operation)
        current = owned(tx, 'research_operation', operation['id'], owner)['data']
        if current.get('lease_token') != operation.get('lease_token') or current['state'] != 'running':
            raise Problem(409, 'STALE_OPERATION', 'Another worker resumed this research operation.')
        previous = tx.get('evidence_receipt', operation['id'])
        if previous: return response(previous['data'])
        tx.put('evidence_receipt', receipt['id'], owner, receipt)
    return response(receipt)


def load_context(service, owner, work_id, arguments, *, artifact_records=None, timings=None):
    """Resolve receipt scope/authority, then materialize exact immutable bytes."""
    started = time.monotonic()
    contexts = []; files = {}; retained = []; reuse_ids = set(arguments.get('reuse_receipt_ids', []))
    from .scientific_reuse import resolve_receipts
    with service.repo.read_transaction() as tx:
        work = owned(tx, 'local_work', work_id, owner)['data']
        if not work['package_id']: raise Problem(409, 'PACKAGE_NOT_READY', 'The seed is not ready.')
        seed_record = owned(tx, 'research_package', work['package_id'], owner)['data']
        source_paths = {source['path'] for source in seed_record['package'].get('source_artifacts', {}).values()}
        for artifact in seed_record['artifacts']:
            if artifact['filename'] not in source_paths: continue
            value = owned(tx, 'research_artifact', artifact['id'], owner)['data']
            if value['local_work_id'] != work_id: raise Problem(404, 'NOT_FOUND', 'Seed artifact is outside this work.')
            retained.append(value)
        for field, kind in (('receipt_ids', 'evidence_receipt'), ('import_ids', 'evidence_import')):
            for identity in sorted(set(arguments.get(field, []))):
                value = owned(tx, kind, identity, owner)['data']
                if value['local_work_id'] != work_id or value['research_request_id'] != work['research_request_id']:
                    raise Problem(404, 'NOT_FOUND', 'The evidence belongs to different research work.')
                context = deepcopy(value['context']); contexts.append(context)
                reuse_ids.update(value.get('reuse_receipt_ids', []))
                for relative, artifact_id in context['artifact_ids'].items():
                    artifact = owned(tx, 'research_artifact', artifact_id, owner)['data']
                    if artifact['local_work_id'] != work_id: raise Problem(404, 'NOT_FOUND', 'Evidence artifact is outside this work.')
                    authorize_artifact(tx, owner, artifact)
                    retained.append({**artifact, 'filename': relative})
        reused = resolve_receipts(tx, owner, work['research_request_id'], sorted(reuse_ids))
        reused['receipt_ids'] = sorted(reuse_ids)
    if timings is not None: timings['selection_authorization_lookup_ms'] = round((time.monotonic()-started)*1000, 3)
    started = time.monotonic()
    # Source bytes are immutable and content-addressed. Read each blob once,
    # with bounded concurrency even when several selected receipts share it.
    unique = {artifact['sha256']: artifact for artifact in retained}
    def read_verified(item):
        checksum, artifact = item
        raw = read_artifact_bytes(artifact)
        if sha256(raw) != checksum or len(raw) != artifact['size_bytes']:
            raise Problem(409, 'SOURCE_UNAVAILABLE', 'Retained evidence bytes changed.')
        return checksum, raw
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='reveal-closure') as pool:
        blobs = dict(pool.map(read_verified, unique.items()))
    for artifact in retained:
        raw = blobs[artifact['sha256']]
        relative = artifact['filename']
        if relative in files and files[relative] != raw: raise ValueError('Conflicting context bytes')
        files[relative] = raw
        if artifact_records is not None: artifact_records[relative] = artifact
    for context in reused['contexts']:
        context = deepcopy(context)
        context.setdefault('source_artifacts', {})
        for checksum, record in context['artifact_records'].items():
            raw = blobs.get(checksum)
            if raw is None:
                raw = _read_record(record)
                blobs[checksum] = raw
            if sha256(raw) != checksum: raise ValueError('Reused source checksum changed')
            relative = 'reuse/' + checksum
            source = deepcopy(record.get('research_source') or {})
            source.update(path=relative, sha256=checksum, size_bytes=len(raw), dapper_file_id=record['file']['id'])
            source.setdefault('format', 'json' if record['file'].get('mime_type') == 'application/json' else 'binary')
            source.setdefault('origin', 'reveal:retained-scientific-source')
            context['source_artifacts']['reuse-'+checksum] = source
            files[relative] = raw
            if artifact_records is not None:
                artifact_records[relative] = {'reused_record': record}
        contexts.append(context)
    if timings is not None: timings['reads_ms'] = round((time.monotonic()-started)*1000, 3)
    started = time.monotonic()
    from .acceptance import build_validation_context
    package = build_validation_context(seed_record['package'], contexts=contexts)
    package['validation_context'].update(receipt_ids=sorted(set(arguments.get('receipt_ids', []))),
        import_ids=sorted(set(arguments.get('import_ids', []))), reuse_receipt_ids=sorted(reuse_ids))
    for source in package['source_artifacts'].values():
        raw = files.get(source['path'])
        if raw is None or sha256(raw) != source['sha256']:
            raise Problem(409, 'SOURCE_UNAVAILABLE', 'A required retained source is unavailable.')
    if timings is not None: timings['merge_hash_ms'] = round((time.monotonic()-started)*1000, 3)
    return package, files, reused


def write_context(directory, package, files):
    directory = Path(directory).resolve()
    for relative, raw in files.items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or path == directory:
            raise ValueError('Unsafe evidence context path')
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    target = directory/'evidence-package.json'; target.write_bytes(canonical_json(package))
    return target


def import_sources(service, operation):
    from .evidence_imports import materialize_import
    args = operation['arguments']; owner = operation['owner_user_id']; work_id = operation['local_work_id']
    def response(record):
        return {'import_id': record['id'], 'sources': record.get('sources', []), 'context': record['context'], 'verification': 'user_supplied'}
    with service.repo.read_transaction() as tx:
        authorize_commit(tx, operation)
        previous = tx.get('evidence_import', operation['id'])
        if previous:
            if previous['owner'] != owner: raise Problem(404, 'NOT_FOUND', 'The evidence import is unavailable.')
            authorize_import(tx, owner, previous['data'])
            return response(previous['data'])
    package, _, inherited_reuse = load_context(service, owner, work_id, args)
    contexts = []; imported = []
    for number, artifact_id in enumerate(args['artifact_ids']):
        with service.repo.read_transaction() as tx:
            artifact = owned(tx, 'research_artifact', artifact_id, owner)['data']
            if artifact['local_work_id'] != work_id or artifact['purpose'] != 'evidence':
                raise Problem(404, 'NOT_FOUND', 'This evidence upload is unavailable.')
        context = materialize_import(artifact['filename'], user_inputs.read(artifact['storage']), args.get('metadata'),
            inputs=[{'dapper_context': package['dapper_context']}], import_id=operation['id']+'-'+str(number))
        context = retain_context(service, operation, context); contexts.append(context)
        imported.append({'artifact_id': artifact_id, 'source_ids': context['eligible_source_ids']})
    from .acceptance import build_validation_context
    combined = build_validation_context(package, contexts=contexts)
    # Retain only this import's additions; caller inputs remain explicit receipts.
    merged = {'dapper_context': {}, 'source_artifacts': {}, 'artifact_ids': {},
        'eligible_source_ids': [], 'cfde_source_ids': [], 'user_inputs': {'uploads': []}}
    for context in contexts:
        for group, nodes in context['dapper_context'].items(): merged['dapper_context'].setdefault(group, []).extend(nodes)
        merged['source_artifacts'].update(context['source_artifacts']); merged['artifact_ids'].update(context['artifact_ids'])
        merged['eligible_source_ids'].extend(context['eligible_source_ids'])
        merged['user_inputs']['uploads'].extend(context.get('user_inputs', {}).get('uploads', []))
    # Derived inputs must travel with the imported result, preserving original receipts.
    if args.get('metadata', {}).get('origin') == 'locally_derived':
        merged['dapper_context'] = combined['dapper_context']
        merged['source_artifacts'] = combined['source_artifacts']
        merged['eligible_source_ids'] = combined['validation_context']['eligible_source_ids']
        merged['cfde_source_ids'] = combined['validation_context']['cfde_source_ids']
        _, input_files, _ = load_context(service, owner, work_id, args)
        for relative, raw in input_files.items():
            artifact = service.retain(owner, work_id, raw, relative, metadata={
                'research_request_id': operation['research_request_id'], 'reuse_receipt_ids': inherited_reuse['receipt_ids']})
            merged['artifact_ids'][relative] = artifact['id']
    record = {'id': operation['id'], 'local_work_id': work_id, 'research_request_id': operation['research_request_id'],
        'context': merged, 'declared_metadata': args.get('metadata', {}), 'created_at': now(), 'status': 'completed',
        'reuse_receipt_ids': inherited_reuse['receipt_ids'] if args.get('metadata', {}).get('origin') == 'locally_derived' else [],
        'sources': imported}
    with service.repo.transaction() as tx:
        authorize_commit(tx, operation)
        current = owned(tx, 'research_operation', operation['id'], owner)['data']
        if (not operation.get('lease_token') or current.get('lease_token') != operation['lease_token']
                or current['state'] != 'running'):
            raise Problem(409, 'STALE_OPERATION', 'Another worker resumed this research operation.')
        previous = tx.get('evidence_import', operation['id'])
        if previous:
            if previous['owner'] != owner: raise Problem(404, 'NOT_FOUND', 'The evidence import is unavailable.')
            authorize_import(tx, owner, previous['data'])
            return response(previous['data'])
        authorize_import(tx, owner, record)
        tx.put('evidence_import', record['id'], owner, record)
    return response(record)


def export_context(service, operation):
    """Build one durable closure without retaining copies of immutable seed blobs."""
    owner, work_id = operation['owner_user_id'], operation['local_work_id']
    arguments = operation['arguments']; timings = {}; records = {}
    with service.repo.read_transaction() as tx:
        authorize_commit(tx, operation)
        work = owned(tx, 'local_work', work_id, owner)['data']
    package, files, reused = load_context(service, owner, work_id, arguments,
        artifact_records=records, timings=timings)
    metadata = {'research_request_id': work['research_request_id'], 'reuse_receipt_ids': reused['receipt_ids']}
    artifacts = []; started = time.monotonic()
    for source in package['source_artifacts'].values():
        filename = source['path']; original = records.get(filename)
        if original and original.get('id'):
            artifact = original
        else:
            # Borrowed sources retain their original storage handle and dependency
            # authority. A content blob is never copied merely to export it.
            record = original['reused_record']
            storage = record.get('storage') or {'store': 'filesystem', 'key': record['path'],
                'sha256': source['sha256'], 'size_bytes': source['size_bytes']}
            artifact = {'id': digest([work_id, 'closure-source', filename, source['sha256'], metadata]),
                'local_work_id': work_id, 'filename': filename, 'sha256': source['sha256'],
                'size_bytes': source['size_bytes'], 'storage': storage, 'purpose': 'validation-context',
                'metadata': metadata, 'retained_record': deepcopy(record), 'created_at': now()}
            with service.repo.transaction() as tx:
                authorize_commit(tx, operation)
                authorize_context_selection(tx, owner, work_id, work['research_request_id'], arguments)
                tx.put('research_artifact', artifact['id'], owner, artifact)
        descriptor = service.artifact_view(artifact)
        descriptor.update(artifact_id=artifact['id'], filename=filename, path=filename,
            format=source.get('format', 'binary'), dapper_file_id=source['dapper_file_id'])
        artifacts.append(descriptor)
    raw = canonical_json(package)
    artifact = service.retain(owner, work_id, raw, 'evidence-package.json', purpose='validation-context', metadata=metadata)
    package_descriptor = {**service.artifact_view(artifact), 'artifact_id': artifact['id'],
        'path': 'evidence-package.json', 'format': 'json'}
    artifacts.append(package_descriptor)
    artifacts = sorted({item['path']: item for item in artifacts}.values(), key=lambda item: item['path'])
    timings['retention_ms'] = round((time.monotonic()-started)*1000, 3); started = time.monotonic()
    context = package['validation_context']; context_hash = sha256(canonical_json(context))
    manifest = {'format': 'reveal.context-manifest/2', 'seed_sha256': work['package_sha256'],
        'package_sha256': sha256(raw), 'context_sha256': context_hash, 'files': artifacts, 'package': package_descriptor}
    result = {'format': 'reveal.validation-context-export/2', 'package_sha256': sha256(raw),
        'seed_sha256': work['package_sha256'], 'context_sha256': context_hash,
        'receipt_ids': context['receipt_ids'], 'import_ids': context['import_ids'],
        'reuse_receipt_ids': context['reuse_receipt_ids'], 'artifacts': artifacts,
        'package_artifact': package_descriptor, 'manifest': manifest, 'phase_timings_ms': timings}
    timings['response_construction_ms'] = round((time.monotonic()-started)*1000, 3)
    return result


def validate_submission(service, operation):
    from .acceptance import assemble_account
    from .scientific_account_lint import validate_scientific_account
    args = operation['arguments']; owner = operation['owner_user_id']; work_id = operation['local_work_id']
    with service.repo.read_transaction() as tx:
        work = owned(tx, 'local_work', work_id, owner)['data']
        frozen = owned(tx, 'request', operation['research_request_id'], owner)['data']
        if args['package_sha256'] != work['package_sha256']: raise Problem(409, 'PACKAGE_MISMATCH', 'Seed hash changed.')
        uploads = [owned(tx, 'research_artifact', identity, owner)['data'] for identity in args.get('account_artifact_ids', [])]
    package, files, reused = load_context(service, owner, work_id, args)
    selected_existing = args.get('existing_account_ids', [])
    available_existing = {item['account_id']: item for item in reused['existing_accounts']}
    if not set(selected_existing) <= set(available_existing):
        raise Problem(422, 'REUSE_SELECTION_REQUIRED', 'Select existing accounts through exact-question reuse receipts.')
    validated = []; reports = []
    with tempfile.TemporaryDirectory(prefix='reveal-validate-') as temporary:
        root = Path(temporary); package_path = write_context(root/'package', package, files)
        for index, upload in enumerate(uploads):
            if upload['local_work_id'] != work_id or upload['purpose'] != 'account': raise Problem(404, 'NOT_FOUND', 'Account upload unavailable.')
            source = root/('input-'+str(index)+Path(upload['filename']).suffix)
            source.write_bytes(user_inputs.read(upload['storage']))
            target = root/('accepted-'+str(index)+'.json')
            document, report = assemble_account(source, package_path, target, frozen['attribution'],
                {'id': operation['id'], 'research_request_id': frozen['id']}, operation['attempt'], 'local')
            ref = user_inputs.retain(target.read_bytes(), 'application/json')
            validated.append({'document': document, 'storage': ref, 'report': report})
            reports.append(report)
        for identity in selected_existing:
            context = next(c for c in reused['contexts'] if c['selection']['object_id'] == identity)
            document = context['dapper_context']
            account = next(a for a in document['scientific_accounts'] if a['id'] == identity)
            if account['question'] != frozen['question_id']: raise Problem(422, 'QUESTION_MISMATCH', 'Existing account answers another question.')
            path = root/('reused-'+digest(identity)+'.json'); path.write_bytes(canonical_json(document))
            from .acceptance import release_root, LOCK
            reports.append(validate_scientific_account(path, dapper_root=release_root(), release_lock=LOCK, evidence_package=package_path))
    source_records = []
    source_files = {f['id']: f for f in package['dapper_context'].get('files', [])}
    for source in package['source_artifacts'].values():
        raw = files[source['path']]; node = source_files[source['dapper_file_id']]
        ref = user_inputs.retain(raw, node.get('mime_type', 'application/octet-stream'))
        eligibility = {**source, 'eligible_evidence': node['id'] in package['validation_context']['eligible_source_ids'],
            'cfde_evidence': node['id'] in package['validation_context']['cfde_source_ids']}
        source_records.append(_record(node, ref, work_id, eligibility))
    payload = {'report': {'valid': True, 'documents': reports, 'advisories': [a for r in reports for a in r.get('advisories', [])]},
        'validated': validated, 'existing_accounts': [available_existing[i] for i in selected_existing],
        'source_records': source_records, 'evidence_manifest_sha256': sha256(canonical_json(package)),
        'reused': reused, 'account_ids': [v['document']['scientific_accounts'][0]['id'] for v in validated],
        'reused_account_ids': selected_existing}
    if operation['kind'] == 'validate':
        return {k: payload[k] for k in ('report', 'account_ids', 'reused_account_ids', 'evidence_manifest_sha256')}
    return payload


def commit_accounts(service, tx, operation, prepared):
    from .acceptance import object_envelope
    from .citations import register
    from .scientific_reuse import record_dependencies
    from .analysis_outcomes import creation_stamp, stamp_gap, stamped
    owner = operation['owner_user_id']; work_id = operation['local_work_id']; args = operation['arguments']
    frozen = owned(tx, 'request', operation['research_request_id'], owner)['data']
    reused = record_dependencies(tx, owner, frozen['id'], prepared['reused']['receipt_ids'],
        prepared['account_ids'] + prepared['reused_account_ids'])
    retained_ids = {n['id'] for c in reused['contexts'] for rows in c['dapper_context'].values() if isinstance(rows, list)
                    for n in rows if isinstance(n, dict) and 'id' in n}
    borrowed_ids = set(reused['borrowed_ids']); access = {}
    for record in prepared['source_records']:
        file = record['file']
        tx.put('artifact', digest([owner, record['sha256']]), owner, record)
        access[file['id']] = {'file': file, 'download_url': setting('NEXTAUTH_URL', 'http://localhost:3000').rstrip('/')+'/api/backend/v1/artifacts/'+record['sha256'],
            'expires_at': None, 'availability': 'available', 'verification': 'checksum_verified'}
    for accepted in prepared['validated']:
        doc = accepted['document']; account = doc['scientific_accounts'][0]; identity = account['id']
        actor = account.get('was_attributed_to', [None])[0]
        metadata = register(tx, owner, doc, {**frozen['attribution'], 'person_id': actor}, now(),
            runtime={'model_id': None, 'harness_version': None}, retained_citation_metadata=reused['citation_metadata'], retained_object_ids=retained_ids)
        state = {'status': 'not_requested', 'job_id': None, 'paragraph_id': None}
        gap = next(g for g in doc['knowledge_gaps'] if g['id'] == account['question'])
        previous = tx.get('account', digest([owner, identity]))
        if not previous:
            envelope = object_envelope(doc, identity, metadata, access); envelope['research_statement'] = state
            summary = {'account': account, 'knowledge_gap': gap, 'claim_count': len(account['component_claims']),
                'created_at': now(), 'job_id': work_id, 'local_work_id': work_id, 'execution_mode': 'local', 'research_statement': state}
            stamp = creation_stamp(tx, owner, frozen['id'], gap=stamp_gap(frozen['composer'].get('source_gap'), frozen.get('question_id')),
                scientific_document=doc, analysis={'job_id': work_id, 'request_id': frozen['id'],
                    'evidence_package_sha256': prepared['evidence_manifest_sha256'], 'account_id': identity})
            tx.put('account', digest([owner, identity]), owner, stamped('account', {'result': envelope, 'summary': deepcopy(summary)}, stamp))
            tx.put('account_membership', digest([owner, identity]), owner, stamped('account_membership', {'account_id': identity, 'summary': summary}, stamp))
        document_sha = accepted['storage']['sha256']
        tx.put('scientific_document', digest([owner, document_sha]), owner, {'sha256': document_sha, 'document': doc,
            'job_id': work_id, 'local_work_id': work_id, 'observed_at': now(), 'citation_metadata': metadata, 'artifact_access': access,
            'evidence_manifest_sha256': prepared['evidence_manifest_sha256']})
        for rows in doc.values():
            if not isinstance(rows, list): continue
            for node in rows:
                if not isinstance(node, dict) or not str(node.get('id', '')).startswith('dapper:'): continue
                if node['id'] not in borrowed_ids:
                    tx.put('grant', digest([owner, node['id']]), owner, {'target_id': node['id']})
                tx.put('object_observation', digest([owner, node['id'], sha256(canonical_json(node))]), owner,
                    {'object_id': node['id'], 'payload': node, 'document_sha256': document_sha})
                if not tx.get('object', digest([owner, node['id']])):
                    tx.put('object', digest([owner, node['id']]), owner, object_envelope(doc, node['id'], metadata, access))
                    tx.put('object_document', digest([owner, node['id']]), owner, {'object_id': node['id'], 'sha256': document_sha})
    return {k: prepared[k] for k in ('report', 'account_ids', 'reused_account_ids', 'existing_accounts', 'evidence_manifest_sha256')}
