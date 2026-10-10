"""Generation-pinned reference hydration for an already authorized account graph.

Only the caller's retained artifact descriptors confer authority. This module never
looks up an owner's research jobs, active reference selections, or remote URLs.
"""
from collections import defaultdict, deque
from copy import deepcopy
from decimal import Decimal
import json

from .auth import Problem
from .evidence_package import canonical_json, sha256
from .reference_generation import GENERATION_RE, KPN_MODEL, LEGACY_MODEL, mechanism_node, parse_public_id

BATCH = 250
MAX_NODES = 10_000
MAX_EDGES = 50_000
GENERATION_COLUMNS = ('generation_id', 'model', 'status', 'eaggl_import_id', 'legacy_mapping_run_id',
                      'legacy_gene_set_import_id', 'manifest')
PROJECTION_COLUMNS = ('factor_key', 'gene_set_id', 'joint_loading', 'marginal_loading',
                      'joint_rank', 'marginal_rank')
NODE_GROUPS = ('gene_sets', 'mechanisms', 'activities', 'files', 'datasets', 'organizations',
               'persons', 'software', 'c2m2_files')
STRIP_GENE_SET = ('in_gene_set_collection', 'in_gmt_file', 'gmt_entry', 'has_embedding')


def _limit():
    raise Problem(422, 'PROVENANCE_LIMIT_EXCEEDED', 'Scientific provenance exceeds the processing bound; no rankings were returned.')


def _json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _value(value):
    if isinstance(value, bytes): return value.decode()
    if isinstance(value, Decimal): return float(value)
    return value


def _rows(connection, sql, params, columns):
    with connection.cursor() as cursor:
        cursor.execute(sql + ' LIMIT ' + str(MAX_NODES + 1), tuple(params))
        values = cursor.fetchall()
    if len(values) > MAX_NODES: _limit()
    rows = []
    for row in values:
        if len(row) != len(columns): raise ValueError('Unexpected provenance reference row shape')
        item = {key: _value(value) for key, value in zip(columns, row)}
        for key in ('metadata', 'payload', 'provenance', 'manifest', 'mapping'):
            if key in item: item[key] = _json(item[key])
        rows.append(item)
    return sorted(rows, key=canonical_json)


def _batched(connection, sql, prefix, identities, columns):
    result = []
    identities = sorted(set(identities))
    for offset in range(0, len(identities), BATCH):
        batch = identities[offset:offset + BATCH]
        result.extend(_rows(connection, sql.format(marks=','.join(['%s'] * len(batch))),
                            [*prefix, *batch], columns))
        if len(result) > MAX_NODES: _limit()
    return result


def _read_capture(record):
    # Source records use the same immutable storage shape as publication/reuse.
    from .research_execution import _read_record
    return _read_record(record)


def _compute_mechanism(item, source):
    """Derive identity only from exact captured, typed source fields."""
    from .acceptance import public_runtime
    if source['model'] == KPN_MODEL:
        fit = parse_public_id(item['public_id'])
        trait = item['trait_metadata']
        if (fit['factor_key'] != item['factor_key'] or fit['kpn_trait_id'] != item['kpn_trait_id']
                or trait['kpn_trait_id'] != item['kpn_trait_id']):
            raise ValueError('Captured factor identifiers disagree')
        node = mechanism_node(item['public_id'], trait['phenotype_name'], item['kpn_trait_id'], fit['factor'],
            item['label'], identity_version=source.get('mechanism_identity_version', 1),
            eaggl_import_id=source['eaggl_import_id'])
    elif source['model'] == LEGACY_MODEL:
        parts = item['public_id'].split(':')
        raw = _json(item['mapping'])['raw']
        if len(parts) != 5 or not isinstance(raw['label'], str): raise ValueError('Missing legacy mapping')
        node = {'name': f'{parts[2]} mechanism {parts[4]}',
                'description': f'EAGGL mechanism {item["public_id"]}. Source label: {raw["label"]}.'}
    else:
        raise ValueError('Unknown captured model')
    runtime = public_runtime()
    node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
    return node


def _observed_sets(capture):
    # Search results and retrieval candidates are deliberately not scientific-use
    # roots. A precise query observation can bind an already referenced GeneSet.
    operation = capture.get('operation')
    if operation not in ('get_gene_set', 'get_gene_set_members', 'get_gene_gene_sets', 'get_factor_loadings'):
        return set()
    args = capture.get('arguments') or {}
    if operation == 'get_factor_loadings' and args.get('kind', 'gene') != 'gene_set': return set()
    result = {item['gene_set_id'] for item in capture.get('result', {}).get('items', [])
              if isinstance(item, dict) and isinstance(item.get('gene_set_id'), str)}
    if operation in ('get_gene_set', 'get_gene_set_members') and isinstance(args.get('gene_set_id'), str):
        result.add(args['gene_set_id'])
    return result



def _relevant_graph(graph, roots, blocked):
    """Keep recorded source dependencies of these exact sets, not sibling sets."""
    from .account_provenance import FIELDS, ORG_ROLES, _relation, _refs
    nodes = {node['id']: (group, node) for group, rows in graph.items() if isinstance(rows, list)
             and not group.endswith('_edges') for node in rows if isinstance(node, dict) and 'id' in node}
    adj, parents = defaultdict(list), defaultdict(list)
    for identity, (group, node) in nodes.items():
        for field in FIELDS:
            for target in _refs(node.get(field)):
                adj[identity].append((field, target))
                if field in ('has_file', 'had_member') and group == 'datasets': parents[target].append(identity)
    for group, rows in graph.items():
        if not group.endswith('_edges') or not isinstance(rows, list): continue
        for row in rows:
            if not isinstance(row, dict): continue
            subject, target = row.get('subject'), row.get('object')
            relation = _relation(row.get('predicate'), group)
            if not isinstance(subject, str) or not isinstance(target, str) or not relation: continue
            adj[subject].append((relation, target))
            if relation in ('has_file', 'had_member') and nodes.get(subject, (None,))[0] == 'datasets':
                parents[target].append(subject)
    visited, states = set(), set()
    pending = deque((identity, False, 0) for identity in roots)
    while pending:
        identity, membership, depth = pending.popleft()
        if identity in blocked or (identity, membership) in states: continue
        if depth > 128: _limit()
        visited.add(identity); states.add((identity, membership))
        if len(visited) > MAX_NODES: _limit()
        group = nodes.get(identity, (None,))[0]
        allowed = {'was_generated_by'} | set(ORG_ROLES) if membership else {'was_derived_from', 'was_generated_by'}
        if group == 'activities': allowed |= {'used'} | set(ORG_ROLES)
        if group == 'datasets': allowed |= set(ORG_ROLES)
        for relation, target in adj.get(identity, ()):
            if relation in allowed: pending.append((target, False, depth + 1))
        if group in ('files', 'c2m2_files'):
            for parent in parents.get(identity, ()):
                pending.append((parent, True, depth + 1))
    result = {}
    for group, rows in graph.items():
        if not isinstance(rows, list): continue
        if group.endswith('_edges'):
            kept = [row for row in rows if isinstance(row, dict)
                    and row.get('subject') in visited and row.get('object') in visited]
        else:
            kept = [row for row in rows if isinstance(row, dict) and row.get('id') in visited]
        if kept: result[group] = kept
    return result


def expand(document, artifact_records, *, tx, connection_factory=None, blocked_ids=(), account_id=None):
    """Return (enriched copy, observations) without mutating any scientific record.

    ``artifact_records`` is keyed by checksum and must contain ONLY records which
    the HTTP source resolver already authorized. ``blocked_ids`` are revoked
    dependencies and may never be restored by catalog or recovery hydration.
    """
    from .account_provenance import eligible_reference_ids
    from .provenance_supplements import load_supplement
    from .runtime_config import reference_mysql_connection

    enriched = deepcopy(document)
    blocked = set(blocked_ids)
    eligible = eligible_reference_ids(document, account_id=account_id) - blocked
    wanted_mechanisms = {value for value in eligible if value.startswith('dapper:Mechanism.')}
    wanted_sets = {value for value in eligible if value.startswith('dapper:GeneSet.')}
    files = {node['id']: node for node in document.get('files', []) if isinstance(node, dict) and 'id' in node}
    metadata = {'reference_generation_ids': [], 'supplement_ids': [], 'source_observations': [], 'issues': [],
                'retained_projection_coverage': [], 'recovery_status': [], 'blocked_reference_ids': []}
    issues, observations, conflicts = {}, {}, set()
    bindings, set_generations = defaultdict(dict), defaultdict(set)
    capture_sources, candidate_nodes = {}, {}
    captured_graphs = []
    node_pool = {node['id']: node for group, rows in document.items() if isinstance(rows, list)
                 for node in rows if isinstance(node, dict) and isinstance(node.get('id'), str)}

    def issue(code, identity=None):
        if identity in blocked: return
        entry = {'code': code}
        if identity is not None: entry['object_id'] = identity
        issues[(code, identity or '')] = entry

    def observe(kind, key, value):
        # Store only hashes and public scientific coordinates, never descriptors,
        # storage locations, query text or the source owner's private metadata.
        if isinstance(value, list): value = sorted(value, key=canonical_json)
        item = {'kind': kind, 'key': key, 'sha256': sha256(canonical_json(value))}
        observations[(kind, key, item['sha256'])] = item

    def merge(graph, source):
        from .account_provenance import FIELDS, ORG_ROLES, _refs
        field_edges = defaultdict(list)
        for group, rows in graph.items():
            if not isinstance(rows, list) or (group not in NODE_GROUPS and not group.endswith('_edges')): continue
            for row in rows:
                if not isinstance(row, dict): continue
                if group.endswith('_edges'):
                    if row.get('subject') in blocked or row.get('object') in blocked: continue
                    value = deepcopy(row)
                    value.setdefault('provenance_source', source)
                else:
                    identity = row.get('id')
                    if not isinstance(identity, str) or identity in blocked: continue
                    value = deepcopy(row)
                    previous = node_pool.get(identity)
                    if previous is not None and previous != value:
                        conflicts.add(identity); issue('conflicting_reference_observation', identity)
                        continue
                    node_pool[identity] = value
                    for field in {'was_derived_from', 'was_generated_by', 'used', 'has_file', 'had_member'} | set(ORG_ROLES):
                        for target_id in _refs(value.get(field)):
                            if target_id not in blocked:
                                field_edges[field + '_edges'].append({'subject': identity, 'predicate': FIELDS[field],
                                    'object': target_id, 'source_field': field, 'provenance_source': source})
                target = enriched.setdefault(group, [])
                if value not in target: target.append(value)
        for group, rows in field_edges.items():
            target = enriched.setdefault(group, [])
            for row in rows:
                if row not in target: target.append(row)
        if len(node_pool) > MAX_NODES or sum(len(rows) for group, rows in enriched.items()
                if group.endswith('_edges') and isinstance(rows, list)) > MAX_EDGES: _limit()

    def inherit_set_bindings(graph, generation):
        # Upstream sets acquire the exact source generation from an allowed
        # recorded derivation, never from an active catalog or a name match.
        from .account_provenance import _refs, _relation
        for group, rows in graph.items():
            if not isinstance(rows, list): continue
            for node in rows:
                if not isinstance(node, dict): continue
                if group.endswith('_edges'):
                    identities = [node.get('object')] if _relation(node.get('predicate'), group) in (
                        'was_derived_from', 'was_generated_by', 'used') else []
                else:
                    identities = [target for field in ('was_derived_from', 'was_generated_by', 'used')
                                  for target in _refs(node.get(field))]
                for identity in identities:
                    if not isinstance(identity, str) or not identity.startswith('dapper:GeneSet.') or identity in blocked:
                        continue
                    set_generations[identity].add(generation)
                    if len(set_generations[identity]) > 1:
                        conflicts.add(identity); issue('ambiguous_reference_binding', identity)

    # Only verified capture envelopes may bind opaque scientific IDs to a model
    # generation. Names, prose and current catalog selections never do so.
    for checksum, record in sorted(artifact_records.items()):
        descriptor = record.get('research_source') or {}
        source = descriptor.get('research_capture')
        if not isinstance(source, dict) or descriptor.get('source_mode') != 'imported_reference': continue
        file = record.get('file') or {}
        file_id = file.get('id')
        if file_id in blocked: continue
        # An authorized snapshot can contain captures that bind an eligible
        # Mechanism even when that capture File is not cited by a claim.
        if not wanted_mechanisms and not wanted_sets: continue
        if (record.get('sha256') != checksum or descriptor.get('sha256') != checksum
                or descriptor.get('dapper_file_id') != file_id or file.get('sha256') != checksum
                or (file_id in files and files[file_id] != file)):
            issue('invalid_capture_binding', file_id if file_id in eligible else None); continue
        raw = _read_capture(record)  # Storage failures are not empty scientific results.
        if sha256(raw) != checksum:
            raise Problem(409, 'SOURCE_UNAVAILABLE', 'Retained scientific capture bytes changed.')
        try:
            capture = json.loads(raw)
            if (capture.get('source') != source or capture.get('source_mode') != 'imported_reference'
                    or not GENERATION_RE.fullmatch(source.get('generation_id', ''))):
                raise ValueError('Capture source binding differs')
            if capture.get('result', {}).get('status') not in ('complete', 'partial'): continue
        except (ValueError, TypeError, AttributeError):
            issue('invalid_capture_binding', file_id if file_id in eligible else None); continue
        generation = source['generation_id']
        touched = False
        if capture.get('operation') == 'get_factor':
            for item in capture.get('result', {}).get('items', []):
                try: node = _compute_mechanism(item, source)
                except (ValueError, KeyError, TypeError):
                    issue('invalid_factor_capture', file_id if file_id in eligible else None); continue
                identity = node['id']
                if identity in blocked or identity not in wanted_mechanisms: continue
                wanted_mechanisms.add(identity)
                item = deepcopy(item)
                if 'mapping' in item: item['mapping'] = _json(item['mapping'])
                binding = {'generation_id': generation, 'source': source, 'item': item, 'node': node}
                key = (generation, item.get('public_id'), item.get('eaggl_factor_id'))
                prior = bindings[identity].get(key)
                if prior is not None and prior != binding: conflicts.add(identity)
                bindings[identity][key] = binding
                candidate_nodes[identity] = node
                touched = True
        for identity in _observed_sets(capture):
            if identity in blocked or identity not in wanted_sets: continue
            if not identity.startswith('dapper:GeneSet.'): continue
            wanted_sets.add(identity); set_generations[identity].add(generation); touched = True
        if capture.get('operation') == 'get_gene_set':
            # Exact captured definitions remain usable when their reference SQL
            # generation has retired. Only already-used GeneSets seed this graph;
            # a cited query File never makes every result row a scientific use.
            from .acceptance import public_runtime
            groups = {cls: group for group, cls in public_runtime().groups.items()}
            for item in capture.get('result', {}).get('items', []):
                if not isinstance(item, dict): continue
                if isinstance(item.get('metadata'), dict):
                    node = item['metadata'].get('dapper_gene_set')
                    collection = item.get('payload') or {}
                    graph = deepcopy(collection.get('provenance') or {}) if isinstance(collection, dict) else {}
                    dependencies = item['metadata'].get('dapper_dependencies') or []
                else:
                    node = item.get('payload')
                    graph = deepcopy(item.get('provenance') or {})
                    dependencies = []
                if not isinstance(node, dict) or node.get('id') not in wanted_sets or node['id'] in blocked:
                    continue
                identity = node['id']
                if identity not in _observed_sets(capture) or not isinstance(graph, dict):
                    issue('invalid_gene_set_capture', identity); continue
                node = {key: value for key, value in node.items() if key not in STRIP_GENE_SET}
                graph.setdefault('gene_sets', []).append(node)
                for dependency in dependencies:
                    if not isinstance(dependency, dict): continue
                    cls = dependency.get('id', '').split(':')[-1].split('.')[0]
                    if cls in groups: graph.setdefault(groups[cls], []).append(dependency)
                captured_graphs.append((identity, generation, checksum, graph))
        if touched:
            capture_sources.setdefault(generation, []).append(source)
            observe('research_capture', checksum, capture)

    chosen = {}
    for identity in sorted(wanted_mechanisms):
        candidates = list(bindings[identity].values())
        if len(candidates) != 1 or identity in conflicts:
            if candidates:
                issue('ambiguous_reference_binding', identity); conflicts.add(identity)
            metadata['retained_projection_coverage'].append({'mechanism_id': identity, 'status': 'unresolved',
                'known_factor_binding': bool(bindings[identity])})
        else:
            chosen[identity] = candidates[0]
    generations = sorted(set(capture_sources))
    if not generations:
        metadata['issues'] = list(issues.values())
        metadata['source_observations'] = list(observations.values())
        metadata['blocked_reference_ids'] = sorted(conflicts)
        return enriched, metadata

    for identity, contexts in set_generations.items():
        if len(contexts) > 1:
            conflicts.add(identity); issue('ambiguous_reference_binding', identity)
    for identity, generation, checksum, graph in captured_graphs:
        if identity in conflicts: continue
        selected = _relevant_graph(graph, {identity}, blocked)
        inherit_set_bindings(selected, generation)
        merge(selected, {'kind': 'research_capture', 'sha256': checksum, 'reference_generation_id': generation})

    connection = (connection_factory or reference_mysql_connection)()
    try:
        generation_rows = _batched(connection, 'SELECT ' + ','.join(GENERATION_COLUMNS) +
            ' FROM reference_generations WHERE generation_id IN ({marks})', [], generations, GENERATION_COLUMNS)
        generation_map = defaultdict(list)
        for row in generation_rows: generation_map[row['generation_id']].append(row)
        valid = {}
        for generation in generations:
            rows = generation_map[generation]
            observe('reference_generation', generation, rows)
            if len(rows) != 1 or rows[0]['status'] not in ('complete', 'superseded'):
                issue('reference_generation_unavailable'); continue
            gen = rows[0]
            sources = capture_sources[generation]
            if any(source.get('model') != gen['model'] or source.get('eaggl_import_id') != gen['eaggl_import_id']
                   or source.get('generation_manifest_sha256') != sha256(canonical_json(gen['manifest']))
                   or (gen['model'] == KPN_MODEL and source.get('mechanism_identity_version', 1) !=
                       (gen['manifest'] or {}).get('mechanism_identity_version', 1))
                   or (gen['model'] == LEGACY_MODEL and (source.get('mapping_run_id') != gen['legacy_mapping_run_id']
                       or source.get('gene_set_import_id') != gen['legacy_gene_set_import_id'])) for source in sources):
                issue('reference_generation_conflict'); continue
            valid[generation] = gen
        projections = []
        for generation, gen in sorted(valid.items()):
            current = {identity: binding for identity, binding in chosen.items() if binding['generation_id'] == generation}
            if gen['model'] == KPN_MODEL:
                keys = {binding['item']['factor_key'] for binding in current.values()}
                columns = ('factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'label')
                rows = _batched(connection, 'SELECT ' + ','.join(columns) +
                    ' FROM reference_factors WHERE generation_id=%s AND factor_key IN ({marks})', [generation], keys, columns)
                observe('reference_factors', generation, rows)
                by_key = defaultdict(list)
                for row in rows: by_key[row['factor_key']].append(row)
                exact = {}
                for identity, binding in current.items():
                    item = binding['item']; matches = by_key[item['factor_key']]
                    if len(matches) != 1 or any(matches[0][key] != item.get(key) for key in columns):
                        issue('factor_binding_conflict', identity); conflicts.add(identity); continue
                    exact[item['factor_key']] = identity
                projection_rows = _batched(connection, 'SELECT ' + ','.join(PROJECTION_COLUMNS) +
                    ' FROM factor_gene_set_projections WHERE generation_id=%s AND scope=%s AND factor_key IN ({marks})',
                    [generation, 'per_trait'], exact, PROJECTION_COLUMNS)
                observe('factor_gene_set_projections', generation, projection_rows)
                for row in projection_rows:
                    identity = exact[row['factor_key']]
                    projections.append((generation, identity, row['gene_set_id'], row))
                counts = defaultdict(int)
                for row in projection_rows: counts[row['factor_key']] += 1
                for key, identity in exact.items():
                    metadata['retained_projection_coverage'].append({'mechanism_id': identity,
                        'reference_generation_id': generation, 'status': 'retained_rows_loaded', 'known_factor_binding': True,
                        'retained_rows': counts[key], 'model_construction_provenance': 'not_established'})
            elif gen['model'] == LEGACY_MODEL:
                columns = ('factor_index', 'eaggl_factor_id', 'public_id', 'mapping')
                rows = _batched(connection, 'SELECT e.factor_index,e.factor_id,l.cfde_node_id,l.payload FROM eaggl_factors e'
                    ' JOIN eaggl_cfde_factor_links l ON l.run_id=%s AND l.factor_index=e.factor_index'
                    ' WHERE e.import_id=%s AND l.cfde_node_id IN ({marks})',
                    [gen['legacy_mapping_run_id'], gen['eaggl_import_id']],
                    [binding['item']['public_id'] for binding in current.values()], columns)
                observe('legacy_factor_bindings', generation, rows)
                exact = {}
                for identity, binding in current.items():
                    item = binding['item']; matches = [row for row in rows if row['public_id'] == item['public_id']]
                    if len(matches) != 1 or any(matches[0][key] != item.get(key) for key in columns):
                        issue('factor_binding_conflict', identity); conflicts.add(identity); continue
                    exact[matches[0]['factor_index']] = identity
                projection_rows = _batched(connection, 'SELECT l.factor_index,l.node_id,l.source_key,l.gene_set_rank,a.dapper_id'
                    ' FROM eaggl_cfde_gene_set_links l LEFT JOIN cfde_gene_set_aliases a'
                    ' ON a.import_id=l.gene_set_import_id AND a.node_id_sha256=l.resolved_alias_sha256'
                    ' WHERE l.run_id=%s AND l.factor_index IN ({marks})', [gen['legacy_mapping_run_id']], exact,
                    ('factor_index', 'node_id', 'source_key', 'gene_set_rank', 'dapper_id'))
                observe('legacy_gene_set_links', generation, projection_rows)
                counts = defaultdict(int)
                for row in projection_rows:
                    identity = exact[row['factor_index']]; counts[row['factor_index']] += 1
                    if row['dapper_id']: projections.append((generation, identity, row['dapper_id'], row))
                    else: issue('legacy_gene_set_binding_unavailable', identity)
                for index, identity in exact.items():
                    metadata['retained_projection_coverage'].append({'mechanism_id': identity,
                        'reference_generation_id': generation, 'status': 'retained_legacy_links_loaded', 'known_factor_binding': True,
                        'retained_rows': counts[index], 'model_construction_provenance': 'not_established'})
        for generation, identity, gene_set, row in projections:
            if gene_set not in blocked: set_generations[gene_set].add(generation)
        for identity, contexts in set_generations.items():
            if len(contexts) > 1:
                conflicts.add(identity); issue('ambiguous_reference_binding', identity)
        loaded = defaultdict(set)

        for depth in range(129):
            progressed = False
            for generation, gen in sorted(valid.items()):
                sets = [identity for identity, contexts in set_generations.items()
                        if contexts == {generation} and identity not in conflicts and identity not in blocked
                                and identity not in loaded[generation]]
                if not sets: continue
                progressed = True
                loaded[generation].update(sets)
                if sum(map(len, loaded.values())) > MAX_NODES: _limit()
                if gen['model'] == KPN_MODEL:
                    rows = _batched(connection, 'SELECT gene_set_id,collection_id,metadata FROM cfde_gene_sets'
                        ' WHERE generation_id=%s AND gene_set_id IN ({marks})', [generation], sets,
                        ('gene_set_id', 'collection_id', 'metadata'))
                    observe('gene_sets', generation, rows)
                    collections = _batched(connection, 'SELECT collection_id,payload FROM cfde_gene_set_collections'
                        ' WHERE generation_id=%s AND collection_id IN ({marks})', [generation],
                        [row['collection_id'] for row in rows], ('collection_id', 'payload'))
                    observe('gene_set_collections', generation, collections)
                    collection_map = {row['collection_id']: row['payload'] for row in collections}
                    found = set()
                    graphs, roots = {}, defaultdict(set)
                    for collection_id, payload in sorted(collection_map.items()):
                        source = {'kind': 'reference_collection', 'reference_generation_id': generation,
                                  'collection_id': collection_id}
                        graph = deepcopy(payload.get('provenance') or {})
                        for group, edges in graph.items():
                            if group.endswith('_edges') and isinstance(edges, list):
                                for edge in edges:
                                    if isinstance(edge, dict): edge['provenance_source'] = source
                        checksum = payload.get('document_sha256')
                        supplement = load_supplement(tx, generation, collection_id, checksum) if checksum else None
                        recovery = {'reference_generation_id': generation, 'collection_id': collection_id,
                                    'status': 'supplement_loaded' if supplement else 'no_registered_supplement'}
                        if supplement:
                            registry = supplement['registry']; supplement_id = registry['id']
                            metadata['supplement_ids'].append(supplement_id)
                            observe('provenance_supplement', supplement_id,
                                {key: value for key, value in registry.items() if key != 'storage'})
                            for group, values in supplement['graph'].items():
                                if not isinstance(values, list): continue
                                for value in values:
                                    value = deepcopy(value)
                                    if group.endswith('_edges'):
                                        value['provenance_source'] = {'kind': 'recovery_supplement', 'supplement_id': supplement_id,
                                            'reference_generation_id': generation, 'collection_id': collection_id}
                                    if value not in graph.setdefault(group, []): graph[group].append(value)
                        metadata['recovery_status'].append(recovery)
                        graphs[collection_id] = graph
                    from .acceptance import public_runtime
                    groups = {cls: group for group, cls in public_runtime().groups.items()}
                    for row in rows:
                        identity = row['gene_set_id']; found.add(identity)
                        node = (row['metadata'] or {}).get('dapper_gene_set')
                        if not isinstance(node, dict) or node.get('id') != identity:
                            issue('gene_set_definition_unavailable', identity); continue
                        node = {key: value for key, value in node.items() if key not in STRIP_GENE_SET}
                        collection_id = row['collection_id']
                        if collection_id not in graphs:
                            issue('collection_provenance_unavailable', identity)
                        graph = graphs.setdefault(collection_id, {})
                        graph.setdefault('gene_sets', []).append(node)
                        roots[collection_id].add(identity)
                        for dependency in (row['metadata'] or {}).get('dapper_dependencies') or []:
                            cls = dependency.get('id', '').split(':')[-1].split('.')[0]
                            if cls in groups: graph.setdefault(groups[cls], []).append(dependency)
                    for identity in sorted(set(sets) - found): issue('gene_set_definition_unavailable', identity)
                    for collection_id, graph in sorted(graphs.items()):
                        selected = _relevant_graph(graph, roots[collection_id], blocked)
                        inherit_set_bindings(selected, generation)
                        merge(selected, {'kind': 'reference_collection', 'reference_generation_id': generation, 'collection_id': collection_id})
                elif gen['model'] == LEGACY_MODEL:
                    rows = _batched(connection, 'SELECT a.dapper_id,o.payload,a.provenance FROM cfde_gene_set_aliases a'
                        ' JOIN dapper_objects o ON o.id=a.dapper_id WHERE a.import_id=%s AND a.dapper_id IN ({marks})',
                        [gen['legacy_gene_set_import_id']], sets, ('dapper_id', 'payload', 'provenance'))
                    observe('legacy_gene_sets', generation, rows)
                    found = set()
                    for row in rows:
                        identity = row['dapper_id']; found.add(identity)
                        if not isinstance(row['payload'], dict) or row['payload'].get('id') != identity:
                            issue('gene_set_definition_unavailable', identity); continue
                        source = {'kind': 'legacy_reference', 'reference_generation_id': generation}
                        graph = deepcopy(row['provenance'] or {})
                        graph.setdefault('gene_sets', []).append(row['payload'])
                        selected = _relevant_graph(graph, {identity}, blocked)
                        inherit_set_bindings(selected, generation)
                        merge(selected, source)
                    for identity in sorted(set(sets) - found): issue('gene_set_definition_unavailable', identity)
            if not progressed: break
        else:
            _limit()
        for generation, identity, gene_set, row in projections:
            if identity in conflicts or gene_set in conflicts or gene_set in blocked: continue
            merge({'mechanisms': [candidate_nodes[identity]]}, {'kind': 'research_capture'})
            merge({'factor_projection_edges': [{'subject': identity, 'predicate': 'reveal:factorProjection',
                'object': gene_set, 'reference_generation_id': generation, **row}]},
                {'kind': 'retained_projection', 'reference_generation_id': generation})
        covered = {row['mechanism_id'] for row in metadata['retained_projection_coverage']}
        for identity in sorted(wanted_mechanisms - covered):
            metadata['retained_projection_coverage'].append({'mechanism_id': identity, 'status': 'unresolved',
                'known_factor_binding': bool(bindings[identity])})
        metadata['retained_projection_coverage'].sort(key=lambda row: row['mechanism_id'])
        metadata['reference_generation_ids'] = generations
    finally:
        try: connection.rollback()
        finally: connection.close()
    metadata['issues'] = [issues[key] for key in sorted(issues)]
    metadata['source_observations'] = [observations[key] for key in sorted(observations)]
    metadata['supplement_ids'] = sorted(set(metadata['supplement_ids']))
    metadata['recovery_status'] = sorted({canonical_json(row): row for row in metadata['recovery_status']}.values(),
                                         key=canonical_json)
    metadata['blocked_reference_ids'] = sorted(conflicts)
    return enriched, metadata
