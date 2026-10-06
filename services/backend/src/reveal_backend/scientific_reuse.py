"""Exact scientific reuse with current, source-bound read authority.

Search is an authorized projection of accepted documents, not an authority to
copy them. Receipts retain exact payloads while each use rechecks their source.
No operation in this module creates the legacy indefinite ``grant`` records.
"""
from copy import deepcopy
from pathlib import Path
import re

from .auth import Problem
from .repository import digest, now, uid
from .evidence_package import canonical_json, sha256

GROUPS = {'proposition': 'propositions', 'claim': 'claims', 'account': 'scientific_accounts',
          'Proposition': 'propositions', 'Claim': 'claims', 'ScientificAccount': 'scientific_accounts'}
MAX_NODES = 10000
_UNREAD = object()


def _legacy_sql_source(tx, owner, package, key, seen=frozenset()):
    """Recheck historical SQL capture bytes, including derived input captures."""
    from .evidence_package import decode
    if key in seen:
        return False
    artifacts = package.get('source_artifacts', {})
    source = artifacts.get(key, {})
    if source.get('format') != 'json' or not str(source.get('origin', '')).startswith(('mysql:', 'mysql-derived:')):
        return False
    row = tx.get('artifact', digest([owner, source.get('sha256')]))
    if not row or row['owner'] != owner or row['data'].get('file', {}).get('id') != source.get('dapper_file_id'):
        return False
    record = row['data']
    try:
        if record.get('storage'):
            from .artifact_store import store
            raw = store().get(record['storage'])
        else:
            raw = Path(record['path']).read_bytes()
        if sha256(raw) != source['sha256'] or len(raw) != record['file']['size_in_bytes']:
            return False
        capture = decode(raw)
    except (OSError, KeyError, ValueError):
        return False
    generations = {node.get('fit', {}).get('upstream_build') for node in package.get('pigean', {}).get('mechanisms', {}).values()}
    if package.get('pigean', {}).get('model') != 'eaggl-capped-v1' or len(generations) != 1:
        return False
    generation = next(iter(generations))
    provenance = capture.get('source', {}); kind = provenance.get('kind')
    names = provenance.get('tables' if kind == 'mysql' else 'derived_from')
    if (capture.get('generation_id') != generation or capture.get('model') != 'eaggl-capped-v1'
            or not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names)
            or source['origin'] != f"{kind}:{'+'.join(names)}?generation_id={generation}"):
        return False
    if kind == 'mysql' and capture.get('format') == 'reveal.reference-evidence.mysql-capture/1':
        return set(names) <= {'reference_factors', 'kpn_traits', 'eaggl_factors', 'eaggl_genes', 'eaggl_gene_loadings',
                             'factor_gene_set_projections', 'cfde_gene_sets', 'cfde_gene_set_collections'}
    if kind == 'mysql-derived' and capture.get('format') == 'reveal.reference-evidence.derived-capture/1':
        return all(_legacy_sql_source(tx, owner, package, item, seen | {key}) for item in names)
    return False


def _fail(code='NOT_FOUND', detail='The requested scientific resource is unavailable.', status=404):
    raise Problem(status, code, detail)


def _nodes(document):
    result = {}
    for group, rows in document.items():
        if not isinstance(rows, list):
            continue
        for node in rows:
            if not isinstance(node, dict) or not isinstance(node.get('id'), str):
                continue
            if node['id'] in result and result[node['id']] != (group, node):
                _fail('REUSE_PAYLOAD_CONFLICT', 'A source document contains conflicting observations.', 409)
            result[node['id']] = (group, node)
    return result


def _public_sources(tx):
    """Only explicit, currently published snapshots; never scan private rows."""
    from .publication import published
    for publication in published(tx):
        snapshot = tx.get('publication_snapshot', publication['data']['snapshot_id'])
        if not snapshot or snapshot['owner'] != publication['owner']:
            continue
        data = snapshot['data']
        yield {'source_kind': 'publication_snapshot', 'source_id': publication['data']['snapshot_id'],
               'owner': publication['owner'], 'publication_id': publication['id'],
               'publication_version': publication['data']['version'], 'document': data['document'],
               'citation_metadata': data.get('citation_metadata', []), 'artifact_access': data.get('artifacts', {}),
               'artifact_records': data.get('artifact_records', {}), 'allowed_ids': None}


def _sources(tx, owner):
    from .account_discovery import visible_accounts
    accepted = {item['account']['id'] for item in visible_accounts(tx, owner)}
    if owner:
        for row in tx.list('scientific_document', owner):
            data = row['data']; document = data.get('document', {})
            if not any(node.get('id') in accepted for node in document.get('scientific_accounts', [])):
                continue
            yield {'source_kind': 'owned_document', 'source_id': data.get('sha256', sha256(canonical_json(document))),
                   'owner': owner, 'document': document, 'citation_metadata': data.get('citation_metadata', []),
                   'artifact_access': data.get('artifact_access', {}), 'allowed_ids': None}
    yield from _public_sources(tx)
    if not owner:
        return
    # Explicit shares are distinct from auto-created citation grants. The
    # source owner must issue a revision-bound closure grant through trusted UI.
    for row in tx.list('scientific_share'):
        data = row['data']
        if (data.get('beneficiary') != owner or data.get('revoked_at') or
                (data.get('expires_at') and data['expires_at'] <= now())):
            continue
        document = tx.get('scientific_document', digest([row['owner'], data.get('document_sha256')]))
        if not document or document['owner'] != row['owner']:
            continue
        value = document['data']
        yield {'source_kind': 'shared_document', 'source_id': row['id'], 'owner': row['owner'],
               'document': value['document'], 'citation_metadata': value.get('citation_metadata', []),
               'artifact_access': value.get('artifact_access', {}),
               'allowed_ids': set(data.get('object_ids', []))}


def _selection(source, identity, node):
    return {'object_id': identity, 'payload_sha256': sha256(canonical_json(node)),
            'source_kind': source['source_kind'], 'source_id': source['source_id'],
            **({'publication_version': source['publication_version']} if 'publication_version' in source else {})}


def _resolve(tx, owner, selection):
    required = ('object_id', 'payload_sha256', 'source_kind', 'source_id')
    if not isinstance(selection, dict) or any(not isinstance(selection.get(key), str) for key in required):
        _fail('INVALID_REUSE_SELECTION', 'Select an exact object, payload and source observation.', 422)
    for source in _sources(tx, owner):
        if any(source[key] != selection[key] for key in ('source_kind', 'source_id')):
            continue
        node = _nodes(source['document']).get(selection['object_id'])
        if not node or (source['allowed_ids'] is not None and selection['object_id'] not in source['allowed_ids']):
            continue
        if sha256(canonical_json(node[1])) != selection['payload_sha256']:
            _fail('REUSE_PAYLOAD_CONFLICT', 'The exact selected payload is unavailable.', 409)
        if ('publication_version' in selection and
                source.get('publication_version') != selection['publication_version']):
            _fail('REUSE_AUTHORITY_UNAVAILABLE', 'The selected publication changed.', 409)
        return source, node
    _fail('REUSE_AUTHORITY_UNAVAILABLE', 'The selected source is no longer authorized.', 409)


def _closure(source, root):
    from .acceptance import _reference_fields, release_root, LOCK
    from .dapper_release import verify_release
    runtime = release_root(); release = verify_release(runtime, LOCK)
    fields = _reference_fields(str(runtime.resolve()), release['lock_sha256'])
    document = source['document']; nodes = _nodes(document)
    selected = set(); pending = [root]; edges = []
    while pending:
        identity = pending.pop()
        if identity in selected:
            continue
        if identity not in nodes or (source['allowed_ids'] is not None and identity not in source['allowed_ids']):
            _fail('REUSE_DEPENDENCY_UNAVAILABLE', 'An exact required scientific dependency is unavailable.', 409)
        selected.add(identity)
        if len(selected) > MAX_NODES:
            _fail('REUSE_DEPENDENCY_UNAVAILABLE', 'The required dependency closure exceeds its bound.', 409)
        group, node = nodes[identity]
        for name in fields.get(group, []):
            if name == 'asserted_in':  # publication back-reference, not scientific support
                continue
            value = node.get(name, [])
            for target in value if isinstance(value, list) else [value]:
                if not isinstance(target, str):
                    continue
                if target in nodes:
                    # A claim's back-reference must not add another account root.
                    if nodes[target][0] == 'scientific_accounts' and target != root:
                        _fail('REUSE_SCHEMA_INCOMPATIBLE', 'This closure requires another account root.', 409)
                    pending.append(target)
                elif re.fullmatch(r'dapper:[A-Za-z]+\.[A-Za-z0-9_-]+', target):
                    _fail('REUSE_DEPENDENCY_UNAVAILABLE', 'A required scientific object is missing.', 409)
        for edge_group, rows in document.items():
            if not edge_group.endswith('_edges') or not isinstance(rows, list):
                continue
            for edge in rows:
                if edge.get('subject') != identity or edge_group == 'asserted_in_edges':
                    continue
                edges.append((edge_group, edge))
                if edge.get('object') in nodes:
                    pending.append(edge['object'])
    result = {'prefixes': deepcopy(document['prefixes'])} if 'prefixes' in document else {}
    for identity in sorted(selected):
        group, node = nodes[identity]
        result.setdefault(group, []).append(deepcopy(node))
    for group, edge in edges:
        rows = result.setdefault(group, [])
        if edge not in rows:
            rows.append(deepcopy(edge))
    return result


def search(tx, owner, kind, query='', filters=None, limit=20, offset=0, *, _public_only=False):
    group = GROUPS.get(kind, kind)
    if group not in GROUPS.values() or not isinstance(query, str) or len(query) > 1000:
        _fail('INVALID_QUERY', 'Choose Proposition, Claim or ScientificAccount search.', 422)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        _fail('INVALID_QUERY', 'Use bounded search pagination.', 422)
    filters = filters or {}
    allowed_filters = {'object_id', 'proposition', 'question', 'subject_entity', 'object_entity',
                       'relation', 'proposition_kind', 'direction', 'status'}
    if set(filters) - allowed_filters:
        _fail('INVALID_QUERY', 'Unsupported scientific search filter.', 422)
    terms = query.casefold().split(); results = []; seen = set()
    for source in _public_sources(tx) if _public_only else _sources(tx, owner):
        for node in source['document'].get(group, []):
            identity = node.get('id')
            if source['allowed_ids'] is not None and identity not in source['allowed_ids']:
                continue
            if any(node.get('id' if key == 'object_id' else key) != value for key, value in filters.items()):
                continue
            text = ' '.join(str(node.get(field, '')) for field in ('name', 'statement', 'text', 'closing_remarks', 'context')).casefold()
            if not all(term in text for term in terms):
                continue
            # A recipient-owned document can contain revocable borrowed nodes.
            if not _public_only:
                try:
                    authorize_object(tx, owner, identity)
                except Problem:
                    continue
            selection = _selection(source, identity, node)
            marker = (selection['source_kind'], selection['source_id'], identity, selection['payload_sha256'])
            if marker in seen:
                continue
            seen.add(marker)
            results.append({**selection, 'kind': group, 'object': deepcopy(node),
                'match_type': 'exact_id' if filters.get('object_id') else 'structured_candidate' if filters else 'lexical_candidate',
                'scope': 'owner' if source['source_kind'] == 'owned_document' else 'public' if source['source_kind'] == 'publication_snapshot' else 'shared'})
    results.sort(key=lambda item: (item['object_id'], item['payload_sha256'], item['source_kind'], item['source_id']))
    return {'items': results[offset:offset + limit], 'total': len(results),
            'next_offset': offset + limit if offset + limit < len(results) else None,
            'index_as_of': now(), 'availability': 'complete_authorized_scan', 'search_mode': 'lexical_structured'}


def public_search(tx, kind, query='', filters=None, limit=20, offset=0):
    """Public matches never depend on an owner, shares, or private grants."""
    return search(tx, None, kind, query, filters, limit, offset, _public_only=True)


def public_get_object(tx, selection):
    """Inspect one current public observation and its exact published closure."""
    if (not isinstance(selection, dict) or selection.get('source_kind') != 'publication_snapshot'
            or any(not isinstance(selection.get(key), str) for key in ('object_id', 'payload_sha256', 'source_id'))
            or type(selection.get('publication_version')) is not int):
        _fail('INVALID_REUSE_SELECTION', 'Select an exact current public observation and publication version.', 422)
    for source in _public_sources(tx):
        if source['source_id'] != selection['source_id'] or source['publication_version'] != selection['publication_version']:
            continue
        found = _nodes(source['document']).get(selection['object_id'])
        if not found:
            break
        group, node = found
        if sha256(canonical_json(node)) != selection['payload_sha256']:
            _fail('REUSE_PAYLOAD_CONFLICT', 'The exact selected public payload is unavailable.', 409)
        document = _closure(source, node['id']); ids = set(_nodes(document))
        from .research_work import public_base
        artifacts = []
        for file in document.get('files', []):
            retained = source['artifact_records'].get(file.get('sha256'), {})
            available = retained.get('file') == file
            artifacts.append({'file': deepcopy(file), 'availability': 'available' if available else 'unavailable',
                'download_url': public_base()+'/v1/artifacts/'+file['sha256'] if available else None,
                'authentication': 'none; availability follows current public publication'})
        return {**_selection(source, node['id'], node), 'kind': group, 'scope': 'public',
            'object': deepcopy(node), 'dapper_context': document, 'closure_sha256': sha256(canonical_json(document)),
            'citation_metadata': [deepcopy(item) for item in source['citation_metadata'] if item['target_id'] in ids],
            'artifacts': artifacts,
            'reuse_policy': 'Reading is not a reuse grant. Authenticate and select this exact observation for research reuse.'}
    _fail('REUSE_AUTHORITY_UNAVAILABLE', 'The selected public observation is no longer available.', 409)


def get_object(tx, owner, selection):
    source, (group, node) = _resolve(tx, owner, selection)
    authorize_object(tx, owner, node['id'])
    closure = _closure(source, node['id'])
    authorize_document(tx, owner, closure)
    return {**_selection(source, node['id'], node), 'kind': group, 'object': deepcopy(node),
            'dapper_context': closure,
            'citation_metadata': [deepcopy(item) for item in source['citation_metadata'] if item['target_id'] == node['id']]}


def _resolved_context(tx, owner, selection):
    source, (_, node) = _resolve(tx, owner, selection)
    document = _closure(source, node['id']); ids = set(_nodes(document))
    authorize_document(tx, owner, document)
    inherited = []; inherited_ids = set()
    for identity in ids:
        dependency = tx.get('scientific_dependencies', digest([owner, identity]))
        if dependency and dependency['owner'] == owner and dependency['data'].get('borrowed'):
            inherited_ids.add(identity)
            for binding in dependency['data'].get('bindings', []):
                if binding not in inherited:
                    inherited.append(deepcopy(binding))
    metadata = [deepcopy(item) for item in source['citation_metadata'] if item['target_id'] in ids]
    artifacts = {identity: deepcopy(value) for identity, value in source['artifact_access'].items() if identity in ids}
    # Artifact records identify exact retained bytes; URL strings never become
    # storage authority. The caller materializes authorized references itself.
    records = {key: deepcopy(value) for key, value in source.get('artifact_records', {}).items()
               if value.get('file', {}).get('id') in ids}
    eligible = []; cfde = []
    for file in document.get('files', []):
        if file['sha256'] not in records:
            record = tx.get('artifact', digest([source['owner'], file['sha256']]))
            if record and record['owner'] == source['owner'] and record['data'].get('file') == file:
                records[file['sha256']] = deepcopy(record['data'])
        if file['sha256'] not in records:
            _fail('REUSE_DEPENDENCY_UNAVAILABLE', 'The exact required source bytes are unavailable.', 409)
        record = records[file['sha256']]
        source_descriptor = record.get('research_source')
        is_eligible = record.get('eligible_evidence') is True or (source_descriptor or {}).get('eligible_evidence') is True
        is_cfde = (record.get('cfde') is True or record.get('cfde_evidence') is True
                   or (source_descriptor or {}).get('cfde_evidence') is True)
        if source_descriptor is None:
            # Legacy captures are classified only against their retained trusted
            # input package. A filename, URL supplied by the borrower, or mere
            # presence in an accepted document does not establish eligibility.
            evidence = tx.get('evidence', record.get('job_id')) if record.get('job_id') else None
            package = evidence['data'].get('package', {}) if evidence and evidence['owner'] == source['owner'] else {}
            package_artifacts = package.get('source_artifacts', {})
            matched = [(key, item) for key, item in package_artifacts.items()
                       if item.get('dapper_file_id') == file['id'] and item.get('sha256') == file['sha256']]
            if len(matched) == 1:
                key, source_descriptor = matched[0]
                authoring = package.get('authoring', {})
                instructions = [authoring.get('skill', {}), authoring.get('contract', {}), *authoring.get('references', [])]
                blocked = key in {item.get('artifact_id') for item in instructions}
                origin = source_descriptor.get('origin', '')
                is_cfde = isinstance(origin, str) and origin.startswith(('https://dev.cfdeknowledge.org/api/',
                    'https://cfde-dev.hugeampkpnbi.org/api/'))
                is_cfde = is_cfde or _legacy_sql_source(tx, source['owner'], package, key)
                validation = package.get('validation_context', {})
                is_cfde = is_cfde or file['id'] in validation.get('cfde_source_ids', [])
                uploads = package.get('user_inputs', {}).get('uploads', [])
                is_eligible = is_cfde or file['id'] in validation.get('eligible_source_ids', []) or any(
                    file['id'] in (item.get('original_file_id'), item.get('extraction_file_id')) for item in uploads)
                is_eligible = is_eligible and not blocked
                is_cfde = is_cfde and not blocked
        if not isinstance(source_descriptor, dict):
            _fail('REUSE_DEPENDENCY_UNAVAILABLE', 'Historical source eligibility metadata must be restored before reuse.', 409)
        if source_descriptor.get('sha256') != file['sha256'] or source_descriptor.get('dapper_file_id') != file['id']:
            _fail('REUSE_PAYLOAD_CONFLICT', 'Retained source metadata does not match the exact File.', 409)
        record['research_source'] = deepcopy(source_descriptor)
        record['eligible_evidence'] = is_eligible
        record['cfde'] = is_cfde and is_eligible
        if is_eligible:
            eligible.append(file['id'])
        if is_cfde and is_eligible:
            cfde.append(file['id'])
    return {'dapper_context': document, 'citation_metadata': metadata, 'artifact_access': artifacts,
            'artifact_records': records, 'borrowed_ids': sorted(ids if source['owner'] != owner else inherited_ids),
            'dependency_bindings': inherited,
            'eligible_source_ids': sorted(eligible), 'cfde_source_ids': sorted(cfde),
            'selection': _selection(source, node['id'], node), 'source_owner': source['owner']}


def create_receipt(tx, owner, research_request_id, selections, idempotency_key):
    request = tx.get('request', research_request_id)
    if not request or request['owner'] != owner:
        _fail()
    if not isinstance(selections, list) or not 1 <= len(selections) <= 30 or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
        _fail('INVALID_REUSE_SELECTION', 'Choose one to thirty objects and an idempotency key.', 422)
    body_hash = digest(selections); key = digest([owner, research_request_id, 'scientific-reuse', idempotency_key])
    prior = tx.get('reuse_idempotency', key)
    if prior:
        if prior['data']['body_hash'] != body_hash:
            _fail('IDEMPOTENCY_CONFLICT', 'The reuse key was already used for another selection.', 409)
        receipt = tx.get('reuse_receipt', prior['data']['receipt_id'])
        resolve_receipts(tx, owner, research_request_id, [prior['data']['receipt_id']])
        return deepcopy(receipt['data'])
    resolved = []
    gap = (request['data'].get('question_id') or request['data'].get('selection', {}).get('knowledge_gap_id')
           or request['data'].get('knowledge_gap', {}).get('id'))
    for selection in selections:
        context = _resolved_context(tx, owner, selection)
        purpose = selection.get('purpose', 'source_claim')
        if purpose not in ('proposition', 'component_claim', 'source_claim', 'existing_account'):
            _fail('INVALID_REUSE_SELECTION', 'Unknown reuse purpose.', 422)
        root = _nodes(context['dapper_context'])[selection['object_id']]
        expected = 'scientific_accounts' if purpose == 'existing_account' else 'propositions' if purpose == 'proposition' else 'claims'
        if root[0] != expected:
            _fail('INVALID_REUSE_SELECTION', 'Reuse purpose does not match the selected object class.', 422)
        if purpose == 'existing_account' and root[1].get('question') != gap:
            _fail('REUSE_QUESTION_MISMATCH', 'The existing account must answer this exact selected question.', 409)
        context['purpose'] = purpose
        context['closure_sha256'] = sha256(canonical_json(context['dapper_context']))
        context['citation_sha256'] = sha256(canonical_json(context['citation_metadata']))
        resolved.append(context)
    receipt = {'id': uid(), 'research_request_id': research_request_id, 'created_at': now(),
               'format': 'reveal.scientific-reuse/1', 'selections': resolved}
    tx.put('reuse_receipt', receipt['id'], owner, receipt)
    tx.put('reuse_idempotency', key, owner, {'body_hash': body_hash, 'receipt_id': receipt['id']})
    return deepcopy(receipt)


def _authorize_context(tx, owner, context, seen=frozenset()):
    # A currently authorized identical observation may replace a withdrawn
    # publication. Require its entire exact closure and pinned citation set.
    selection = context['selection']
    for binding in context.get('dependency_bindings', []):
        resolve_receipts(tx, owner, binding['research_request_id'], binding['receipt_ids'], _seen=seen)
    candidates = [selection]
    for source in _sources(tx, owner):
        node = _nodes(source['document']).get(selection['object_id'])
        if node and sha256(canonical_json(node[1])) == selection['payload_sha256']:
            candidate = _selection(source, selection['object_id'], node[1])
            if candidate not in candidates:
                candidates.append(candidate)
    for candidate in candidates:
        try:
            source, _ = _resolve(tx, owner, candidate)
            closure = _closure(source, candidate['object_id']); ids = set(_nodes(closure))
            metadata = [item for item in source['citation_metadata'] if item['target_id'] in ids]
            if (sha256(canonical_json(closure)) == context['closure_sha256'] and
                    sha256(canonical_json(metadata)) == context['citation_sha256']):
                # A copied recipient-owned document cannot authorize itself.
                if source['source_kind'] == 'owned_document' and source['owner'] == owner and not _same_owner(tx, context.get('source_owner'), owner):
                    continue
                return candidate
        except Problem:
            continue
    _fail('REUSE_AUTHORITY_UNAVAILABLE', 'Required borrowed scientific evidence is no longer authorized.', 409)


def _same_owner(tx, original, current):
    """Follow explicit workspace transfers, never infer ownership from a copy."""
    seen = set()
    while original != current:
        if not original or original in seen or len(seen) >= 20: return False
        seen.add(original)
        row = tx.get('scientific_owner_transition', digest(original))
        if not row or row['data'].get('source') != original: return False
        original = row['data']['target']
    return True


def resolve_receipts(tx, owner, research_request_id, receipt_ids, *, _seen=frozenset()):
    contexts = []; existing = []; borrowed = set(); replacements = []
    for identity in receipt_ids:
        if identity in _seen or len(_seen) > 100:
            _fail('REUSE_DEPENDENCY_UNAVAILABLE', 'Cyclic or excessive reuse authorization dependencies.', 409)
        row = tx.get('reuse_receipt', identity)
        if not row or row['owner'] != owner or row['data']['research_request_id'] != research_request_id:
            _fail()
        for context in row['data']['selections']:
            authority = _authorize_context(tx, owner, context, _seen | {identity})
            if authority != context['selection']:
                replacements.append({'receipt_id': identity, 'selection': context['selection'], 'authorization': authority})
            contexts.append(deepcopy(context)); borrowed.update(context['borrowed_ids'])
            if context['purpose'] == 'existing_account':
                existing.append({'account_id': context['selection']['object_id'], 'reuse_receipt_id': identity,
                                 'selection': deepcopy(context['selection']), 'status': 'reused_existing'})
    return {'contexts': contexts, 'existing_accounts': existing, 'borrowed_ids': sorted(borrowed),
            'citation_metadata': [item for context in contexts for item in context['citation_metadata']],
            'replacement_authorizations': replacements}


def record_dependencies(tx, owner, research_request_id, receipt_ids, account_ids):
    resolved = resolve_receipts(tx, owner, research_request_id, receipt_ids)
    for item in resolved['replacement_authorizations']:
        tx.put('reuse_authorization', digest([owner, item]), owner, {**item, 'observed_at': now()})
    for identity in {*resolved['borrowed_ids'], *account_ids}:
        key = digest([owner, identity]); previous = tx.get('scientific_dependencies', key)
        bindings = deepcopy(previous['data'].get('bindings', [])) if previous else []
        binding = {'research_request_id': research_request_id, 'receipt_ids': sorted(set(receipt_ids))}
        if binding not in bindings:
            bindings.append(binding)
        tx.put('scientific_dependencies', key, owner, {'object_id': identity, 'bindings': bindings,
               'borrowed': identity in resolved['borrowed_ids']})
    return resolved


def authorize_object(tx, owner, identity, *, dependency=_UNREAD):
    row = tx.get('scientific_dependencies', digest([owner, identity])) if dependency is _UNREAD else dependency
    if not row or row['owner'] != owner or not row['data'].get('borrowed'):
        return
    for binding in row['data']['bindings']:
        resolve_receipts(tx, owner, binding['research_request_id'], binding['receipt_ids'])


def retained_citations(tx, owner, identity):
    """Return currently authorized pinned metadata, never a global latest revision."""
    row = tx.get('scientific_dependencies', digest([owner, identity]))
    if not row or row['owner'] != owner or not row['data'].get('borrowed'):
        return None
    records = []
    for binding in row['data']['bindings']:
        resolved = resolve_receipts(tx, owner, binding['research_request_id'], binding['receipt_ids'])
        for record in resolved['citation_metadata']:
            if record['target_id'] == identity and record not in records:
                records.append(record)
    return records


def authorize_document(tx, owner, document):
    records = tx.get_many('scientific_dependencies', [digest([owner, identity]) for identity in _nodes(document)])
    checked = set()
    for row in records.values():
        if row['owner'] != owner or not row['data'].get('borrowed'):
            continue
        for binding in row['data']['bindings']:
            key = digest(binding)
            if key not in checked:
                resolve_receipts(tx, owner, binding['research_request_id'], binding['receipt_ids'])
                checked.add(key)


def readable_document(tx, owner, document):
    """Retain the author's account while withholding withdrawn borrowed nodes.

    This is for ordinary provenance display only. Reuse/export/commit must use
    authorize_document and require the entire selected closure to be available.
    """
    unavailable = set(); checked = {}
    records = tx.get_many('scientific_dependencies', [digest([owner, identity]) for identity in _nodes(document)])
    for row in records.values():
        if row['owner'] != owner or not row['data'].get('borrowed'):
            continue
        for binding in row['data']['bindings']:
            key = digest(binding)
            if key not in checked:
                try:
                    resolve_receipts(tx, owner, binding['research_request_id'], binding['receipt_ids'])
                    checked[key] = True
                except Problem as error:
                    if error.code not in ('REUSE_AUTHORITY_UNAVAILABLE', 'REUSE_DEPENDENCY_UNAVAILABLE'):
                        raise
                    checked[key] = False
            if not checked[key]:
                unavailable.add(row['data']['object_id'])
    return {group: [deepcopy(node) for node in rows if isinstance(node, dict)
                   and node.get('id') not in unavailable and node.get('subject') not in unavailable
                   and node.get('object') not in unavailable] if isinstance(rows, list) else deepcopy(rows)
            for group, rows in document.items()}


def authorize_publication(tx, owner, account_id):
    row = tx.get('scientific_dependencies', digest([owner, account_id]))
    if row and row['owner'] == owner:
        for binding in row['data']['bindings']:
            resolve_receipts(tx, owner, binding['research_request_id'], binding['receipt_ids'])
