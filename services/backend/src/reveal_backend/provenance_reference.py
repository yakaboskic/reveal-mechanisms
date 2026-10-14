"""Generation- and release-pinned reference hydration for an already authorized account graph.

Only the caller's retained artifact descriptors, or an object's exact content-addressed identity in the
published release (identity binding, below), confer authority. This module never looks up an owner's
research jobs, active reference selections, or remote URLs.

Trusted captures bind opaque ids to the reference they were read from: research
captures (their source names a reference generation, or a release by release_id with
source_kind 'reference_release') and the reference-release captures of Box evidence
packages (reveal.reference-evidence.mysql-capture/2: reference-release, reference-factors,
candidate-factors), checksum-verified and their Mechanisms re-minted from the captured
rows. A release is read from the <prefix>_ref_* tables after reference_pin's ref_release
read on the same connection: ref_factors, ref_projections (every per-library row), ref_gene_sets
and ref_collections through reference_lineage (collection_lineage, lineage_closure). Gene sets
the document reaches get their full node; gene sets reached only by a projection get their
name, collection_id and generating activity. A capture pinned to a release that is no longer
published is the coverage issue reference_release_superseded: that work is hydrated only from
capture bytes and archived snapshots (archived(mechanism_id)), never from the current tables.
Every reached KPN trait (a bound Mechanism's fitted trait, or one a proposition cites) gets a
`traits` record named from ref_phenotypes / ref_traits; metadata['traits'] carries its
bottom-line GWAS artifact (metadata.bottom_line) for the trace's portal phase. Missing tables
(MySQL 1146: reload-era tables after cleanup) are coverage issues, not errors.

Identity binding (binding_origin document_identity): a GeneSet or Mechanism the document references
without a capture binding it, and without lineage of its own, binds to the published release by its
content-addressed id alone: a GeneSet whose id is a ref_gene_sets row, a Mechanism whose id equals the
node minted (reference_generation.mechanism_node, as the catalog and reference_evidence mint it) from
that release's ref_factors + ref_traits row of the public factor id its description names. Hosted (Box)
accounts whose packages hold no reference capture trace this way. Captures keep precedence; a capture
and an identity binding that disagree are ambiguous; names and prose never bind, and a superseded
release is never matched.

Curated lineage edges are labelled provenance_source.kind curated_lineage: a release collection's
payload.curated_provenance (lap/config/source_datasets.yaml; its curation_id is that source path) and a
reload-era collection's payload.lineage_curation (scripts/apply_collection_lineage.py).
"""
from collections import OrderedDict, defaultdict, deque
from copy import deepcopy
from decimal import Decimal
import json
import re
import threading

from .auth import Problem
from .evidence_package import canonical_json, sha256
from .reference_generation import GENERATION_RE, KPN_MODEL, LEGACY_MODEL, PUBLIC_ID_RE, mechanism_node, parse_public_id
from .reference_pin import NO_TABLE, SUPERSEDED, current_release
from .repository import application_prefix, application_sql

BATCH = 250
MAX_NODES = 10_000
MAX_EDGES = 50_000
GENERATION_COLUMNS = ('generation_id', 'model', 'status', 'eaggl_import_id', 'legacy_mapping_run_id',
                      'legacy_gene_set_import_id', 'manifest')
PROJECTION_COLUMNS = ('factor_key', 'gene_set_id', 'joint_loading', 'marginal_loading',
                      'joint_rank', 'marginal_rank')
NODE_GROUPS = ('gene_sets', 'mechanisms', 'activities', 'files', 'datasets', 'organizations',
               'persons', 'software', 'c2m2_files', 'traits')
STRIP_GENE_SET = ('in_gene_set_collection', 'in_gmt_file', 'gmt_entry', 'has_embedding')
RELEASE = 'reference_release'  # a valid generation that is the published release (its model in `valid`)
RELEASE_FACTOR_COLUMNS = ('factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'label')
RELEASE_PROJECTION_COLUMNS = ('factor_key', 'gene_set_id', 'library', 'joint_loading', 'marginal_loading', 'joint_rank', 'marginal_rank')
# A Box package's reference-release captures (reference_evidence): filename -> the tables its source names.
BOX_FORMAT = 'reveal.reference-evidence.mysql-capture/2'
BOX_CAPTURES = {'reference-release.json': ['reveal_ref_release'], 'reference-factors.json': ['reveal_ref_factors', 'reveal_ref_traits'],
                'candidate-factors.json': ['reveal_ref_factors', 'reveal_ref_traits']}
LINEAGE_CACHE_BYTES = 128 << 20
KPN_PAGE = 'https://broadinstitute.github.io/kpn-data-models/kpn.trait/{}/'
IDENTITY = 'document_identity'
CURATED = 'curated_lineage'
LINEAGE_FIELDS = ('was_derived_from', 'was_generated_by')
# A KPN Mechanism node names its public factor id (reference_generation.mechanism_node).
KPN_DESCRIPTION = re.compile(r'EAGGL mechanism (' + PUBLIC_ID_RE.pattern + r')\. KPN trait ')
IDENTITY_COLUMNS = (*RELEASE_FACTOR_COLUMNS, 'phenotype_name')
IDENTITY_FACTORS = ('SELECT ' + ','.join('f.' + column for column in RELEASE_FACTOR_COLUMNS) + ',t.phenotype_name FROM reveal_ref_factors f'
                    ' JOIN reveal_ref_traits t ON t.kpn_trait_id=f.kpn_trait_id WHERE f.public_id IN ({marks})')
IDENTITY_SETS = 'SELECT gene_set_id FROM reveal_ref_gene_sets WHERE gene_set_id IN ({marks})'


def _limit():
    raise Problem(422, 'PROVENANCE_LIMIT_EXCEEDED', 'Scientific provenance exceeds the processing bound; no rankings were returned.')


def _missing(error):
    return bool(getattr(error, 'args', None)) and error.args[0] == NO_TABLE


def _json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _json_value(value):
    """A JSON_EXTRACT result: JSON text (MySQL), or a bare string (SQLite) for a string value."""
    if isinstance(value, bytes): value = value.decode()
    if not isinstance(value, str): return value
    try: return json.loads(value)
    except ValueError: return value


def _value(value):
    if isinstance(value, bytes): return value.decode()
    if isinstance(value, Decimal): return float(value)
    return value


def _ref(sql):
    """A reveal_ref_* statement for this environment's application prefix."""
    return application_sql(sql, application_prefix())


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


class _LineageCache:
    """Collection lineage of the published release by (release_id, collection_id, document_sha256): collection_lineage and
    its reference_lineage index, shared read-only by every trace (never copied: merge copies what it keeps). Bounded by the
    payload JSON bytes read (LINEAGE_CACHE_BYTES), least recently used first; emptied when another release is read. Only a
    caller that has just read that release from ref_release on its connection may use it."""
    def __init__(self, limit=LINEAGE_CACHE_BYTES):
        self.limit, self.items, self.size, self.release, self.lock = limit, OrderedDict(), 0, None, threading.Lock()

    def _release(self, release_id):
        if release_id != self.release: self.items.clear(); self.size = 0; self.release = release_id

    def get(self, release_id, collection_id, document_sha256):
        """(lineage, index, curated) or None; curated is _curated(payload)."""
        with self.lock:
            self._release(release_id)
            entry = self.items.get((collection_id, document_sha256))
            if entry: self.items.move_to_end((collection_id, document_sha256))
            return entry and entry[:3]

    def put(self, release_id, collection_id, document_sha256, lineage, index, curated, size):
        with self.lock:
            self._release(release_id)
            prior = self.items.pop((collection_id, document_sha256), None)
            if prior: self.size -= prior[-1]
            if size > self.limit: return
            self.items[(collection_id, document_sha256)] = (lineage, index, curated, size); self.size += size
            while self.size > self.limit: self.size -= self.items.popitem(last=False)[1][-1]


_LINEAGE = _LineageCache()


def _read_capture(record):
    # Source records use the same immutable storage shape as publication/reuse.
    from .research_execution import _read_record
    return _read_record(record)


def _compute_mechanism(item, source, runtime=None):
    """Derive identity only from exact captured (or, for an identity binding, published release) typed source fields."""
    from .acceptance import public_runtime
    if source['model'] == KPN_MODEL:
        fit = parse_public_id(item['public_id'])
        trait = item['trait_metadata']
        if (fit['factor_key'] != item['factor_key'] or fit['kpn_trait_id'] != item['kpn_trait_id']
                or trait['kpn_trait_id'] != item['kpn_trait_id']):
            raise ValueError('Captured factor identifiers disagree')
        node = mechanism_node(item['public_id'], trait['phenotype_name'], item['kpn_trait_id'], fit['factor'],
            item['label'], identity_version=source.get('mechanism_identity_version', 1),
            eaggl_import_id=source.get('eaggl_import_id'))
    elif source['model'] == LEGACY_MODEL:
        parts = item['public_id'].split(':')
        raw = _json(item['mapping'])['raw']
        if len(parts) != 5 or not isinstance(raw['label'], str): raise ValueError('Missing legacy mapping')
        node = {'name': f'{parts[2]} mechanism {parts[4]}',
                'description': f'EAGGL mechanism {item["public_id"]}. Source label: {raw["label"]}.'}
    else:
        raise ValueError('Unknown captured model')
    runtime = runtime or public_runtime()
    node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
    return node


def _native_lineage(document):
    """IDs whose own recorded lineage the document already carries; identity binding never looks those up."""
    from .account_provenance import _refs, _relation
    result = set()
    for group, rows in document.items():
        if not isinstance(rows, list): continue
        for row in rows:
            if not isinstance(row, dict): continue
            if group.endswith('_edges'):
                if (group == 'factor_projection_edges' or _relation(row.get('predicate'), group) in LINEAGE_FIELDS) \
                        and isinstance(row.get('subject'), str):
                    result.add(row['subject'])
            elif isinstance(row.get('id'), str) and any(_refs(row.get(field)) for field in LINEAGE_FIELDS):
                result.add(row['id'])
    return result


def _identity_source(release):
    """The binding source the published release itself establishes for an identity binding (as a Box capture's)."""
    manifest = release['manifest'] or {}
    return {'binding_origin': IDENTITY, 'source_kind': RELEASE, 'release_id': release['release_id'],
            'generation_id': release['release_id'], 'model': KPN_MODEL, 'generation_manifest_sha256': sha256(canonical_json(manifest)),
            'mechanism_identity_version': manifest.get('mechanism_identity_version', 1), 'eaggl_import_id': manifest.get('eaggl_import_id')}


def _edge_keys(groups):
    return {(group, edge.get('subject'), edge.get('predicate'), edge.get('object')) for group, edges in (groups or {}).items()
            if group.endswith('_edges') and isinstance(edges, list) for edge in edges if isinstance(edge, dict)}


def _curated(payload):
    """(edge keys, provenance_source) of the edges a release collection's payload.curated_provenance adds: those the
    producer's provenance lacks (collection_lineage keeps the producer's edge on a collision); (frozenset(), None) without."""
    curated = payload.get('curated_provenance') if isinstance(payload, dict) else None
    if not isinstance(curated, dict): return frozenset(), None
    keys = frozenset(_edge_keys(curated.get('groups')) - _edge_keys(payload.get('provenance')))
    return keys, ({'kind': CURATED, 'curation_id': curated.get('source'), 'curation_sha256': curated.get('source_sha256')} if keys else None)


def _label_released(graph, curated, location):
    """A copy of a release closure whose curated edges carry their curated_lineage provenance_source (closures are shared)."""
    keys, source = curated or (frozenset(), None)
    if not keys: return graph
    labelled = dict(graph)
    for group, edges in graph.items():
        if not group.endswith('_edges') or not isinstance(edges, list): continue
        labelled[group] = [{**edge, 'provenance_source': {**source, **location}}
                           if isinstance(edge, dict) and (group, edge.get('subject'), edge.get('predicate'), edge.get('object')) in keys
                           else edge for edge in edges]
    return labelled


def _label_curated(graph, payload, location):
    """Tag the used edges a reload-era lineage curation added (payload.lineage_curation, apply_collection_lineage.py) so
    traces disclose the hard-coded step. Returns the curation id, or None for an uncurated collection."""
    marker = payload.get('lineage_curation') if isinstance(payload, dict) else None
    if not isinstance(marker, dict): return None
    curated = {canonical_json(edge) for edge in marker.get('used_edges') or [] if isinstance(edge, dict)}
    source = {'kind': CURATED, 'curation_id': marker.get('curation_id'), 'curation_sha256': marker.get('curation_sha256'), **location}
    for edge in graph.get('used_edges') or []:
        if isinstance(edge, dict) and canonical_json({k: v for k, v in edge.items() if k != 'provenance_source'}) in curated:
            edge['provenance_source'] = dict(source)
    return marker.get('curation_id')


def _released(source):
    """Whether a capture source names a reference release (not a reload-era generation)."""
    return source.get('source_kind') == RELEASE or source.get('kind') == 'box_capture'


def _box_rows(raw, tables):
    """(release_id, rows) of a Box package's reference-release capture whose source names these tables."""
    capture = json.loads(raw)
    release_id = capture.get('release_id')
    if (capture.get('format') != BOX_FORMAT or capture.get('model') != KPN_MODEL or not isinstance(release_id, str)
            or not GENERATION_RE.fullmatch(release_id) or (capture.get('source') or {}).get('tables') != tables
            or not isinstance(capture.get('data'), list) or not all(isinstance(row, dict) for row in capture['data'])):
        raise ValueError('Not a reference-release capture')
    return release_id, capture['data']


def _observed_sets(capture):
    # Search results and retrieval candidates are deliberately not scientific-use
    # roots. A precise query observation can bind an already referenced GeneSet.
    operation = capture.get('operation')
    if operation not in ('get_gene_set', 'get_gene_set_members', 'get_gene_gene_sets', 'get_factor_loadings',
                         'get_trait_gene_sets', 'get_gene_set_traits'):
        return set()
    args = capture.get('arguments') or {}
    if operation == 'get_factor_loadings' and args.get('kind', 'gene') != 'gene_set': return set()
    result = {item['gene_set_id'] for item in capture.get('result', {}).get('items', [])
              if isinstance(item, dict) and isinstance(item.get('gene_set_id'), str)}
    if operation in ('get_gene_set', 'get_gene_set_members', 'get_gene_set_traits') and isinstance(args.get('gene_set_id'), str):
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


def _closure(node, lineage, index):
    """lineage_closure of one gene set; a collapsed group's kept used edge observes collapsed_inputs (a used relation the
    activity states as a node field becomes such an edge record)."""
    from .reference_lineage import lineage_closure
    closure = lineage_closure(node, lineage, index=index)
    graph = dict(closure['provenance'])
    for item in closure['collapsed']:
        edges = graph.get('used_edges') or []
        kept = [edge for edge in edges if edge.get('subject') == item['activity'] and edge.get('object') == item['representative']]
        observed = {'subject': item['activity'], 'predicate': 'prov:used', 'object': item['representative']}
        graph['used_edges'] = ([edge for edge in edges if edge not in kept] +
                               [{**edge, 'collapsed_inputs': item['collapsed_inputs']} for edge in kept or [observed]])
    return graph


def expand(document, artifact_records, *, tx, connection_factory=None, blocked_ids=(), account_id=None, archived=None):
    """Return (enriched copy, observations) without mutating any scientific record.

    ``artifact_records`` is keyed by checksum and must contain ONLY records which
    the HTTP source resolver already authorized. ``blocked_ids`` are revoked
    dependencies and may never be restored by catalog or recovery hydration.
    ``tx`` serves registered recovery supplements to the legacy-generation path only.
    ``archived(mechanism_id)`` (the catalog's archive lookup) is called only after the
    reference connection is closed, for Mechanisms of a superseded release.
    """
    from .account_provenance import eligible_reference_ids, kpn_trait
    from .provenance_portal import bottom_line_href
    from .provenance_supplements import load_supplement
    from .runtime_config import reference_mysql_connection

    enriched = deepcopy(document)
    blocked = set(blocked_ids)
    eligible = eligible_reference_ids(document, account_id=account_id) - blocked
    wanted_mechanisms = {value for value in eligible if value.startswith('dapper:Mechanism.')}
    wanted_sets = {value for value in eligible if value.startswith('dapper:GeneSet.')}
    wanted_traits = {value for value in eligible if kpn_trait(value)}
    cited_sets = set(wanted_sets)
    files = {node['id']: node for node in document.get('files', []) if isinstance(node, dict) and 'id' in node}
    metadata = {'reference_generation_ids': [], 'supplement_ids': [], 'source_observations': [], 'issues': [],
                'retained_projection_coverage': [], 'recovery_status': [], 'blocked_reference_ids': [], 'traits': {}}
    issues, observations, conflicts = {}, {}, set()
    bindings, set_generations = defaultdict(dict), defaultdict(set)
    capture_sources, candidate_nodes = {}, {}
    captured_graphs, box_manifests, box_rows = [], {}, []
    node_pool = {node['id']: node for group, rows in document.items() if isinstance(rows, list)
                 for node in rows if isinstance(node, dict) and isinstance(node.get('id'), str)}
    merged = defaultdict(set)  # canonical JSON of each row merge added, by group

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

    def append(group, value):
        target = enriched.setdefault(group, [])
        if not merged[group] and target: merged[group].update(canonical_json(row) for row in target)
        key = canonical_json(value)
        if key not in merged[group]: merged[group].add(key); target.append(value)

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
                append(group, value)
        for group, rows in field_edges.items():
            for row in rows: append(group, row)
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

    def bind(identity, generation, source, item, node):
        wanted_mechanisms.add(identity)
        binding = {'generation_id': generation, 'source': source, 'item': item, 'node': node}
        key = (generation, item.get('public_id'), item.get('eaggl_factor_id'))
        prior = bindings[identity].get(key)
        if prior is not None and prior != binding: conflicts.add(identity)
        bindings[identity][key] = binding
        candidate_nodes[identity] = node

    def fitted(identity, kpn, name, generation):
        # A bound Mechanism's fitted KPN trait (a node named as the release or the capture names it).
        traits.setdefault(kpn, {'id': kpn, 'name': name, 'kpn_trait_id': kpn, 'href': KPN_PAGE.format(kpn[-7:])})
        merge({'mechanisms': [candidate_nodes[identity]],
               'fitted_on_trait_edges': [{'subject': identity, 'predicate': 'reveal:fittedOnTrait', 'object': kpn}]},
              {'kind': 'reference_binding', 'reference_generation_id': generation})

    traits, trait_info = {}, {}

    # Only verified capture envelopes may bind opaque scientific IDs to a model
    # generation. Names, prose and current catalog selections never do so.
    for checksum, record in sorted(artifact_records.items()):
        descriptor = record.get('research_source') or {}
        source = descriptor.get('research_capture')
        file = record.get('file') or {}
        file_id = file.get('id')
        box = not isinstance(source, dict) and file.get('filename') in BOX_CAPTURES
        if not box and (not isinstance(source, dict) or descriptor.get('source_mode') != 'imported_reference'): continue
        if file_id in blocked: continue
        # An authorized snapshot can contain captures that bind an eligible
        # Mechanism even when that capture File is not cited by a claim.
        if not wanted_mechanisms and not wanted_sets: continue
        if (record.get('sha256') != checksum or file.get('sha256') != checksum or (file_id in files and files[file_id] != file)
                or not box and (descriptor.get('sha256') != checksum or descriptor.get('dapper_file_id') != file_id)):
            issue('invalid_capture_binding', file_id if file_id in eligible else None); continue
        raw = _read_capture(record)  # Storage failures are not empty scientific results.
        if sha256(raw) != checksum:
            raise Problem(409, 'SOURCE_UNAVAILABLE', 'Retained scientific capture bytes changed.')
        if box:
            try: release_id, rows = _box_rows(raw, BOX_CAPTURES[file['filename']])
            except (ValueError, TypeError, AttributeError):
                issue('invalid_capture_binding', file_id if file_id in eligible else None); continue
            if file['filename'] == 'reference-release.json': box_manifests.setdefault(release_id, []).append((checksum, rows))
            else: box_rows.append((release_id, checksum, file_id, rows))
            continue
        try:
            capture = json.loads(raw)
            if (capture.get('source') != source or capture.get('source_mode') != 'imported_reference'
                    or not GENERATION_RE.fullmatch(source.get('generation_id', ''))
                    or _released(source) and source.get('release_id') != source['generation_id']):
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
                item = deepcopy(item)
                if 'mapping' in item: item['mapping'] = _json(item['mapping'])
                bind(identity, generation, source, item, node)
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
                    _label_curated(graph, collection, {'reference_generation_id': generation})
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

    # A Box package's factors are minted as its builder minted them: from the captured rows and the captured release
    # manifest (reference_evidence._Evidence.anchors).
    for release_id, checksum, file_id, rows in box_rows:
        captured = box_manifests.get(release_id, [])
        manifests = [_json(rows_[0].get('manifest')) for _, rows_ in captured if len(rows_) == 1 and rows_[0].get('release_id') == release_id]
        if len(manifests) != 1 or not isinstance(manifests[0], dict):
            issue('invalid_capture_binding', file_id if file_id in eligible else None); continue
        manifest = manifests[0]
        source = {'kind': 'box_capture', 'source_kind': RELEASE, 'release_id': release_id, 'generation_id': release_id,
                  'model': KPN_MODEL, 'generation_manifest_sha256': sha256(canonical_json(manifest)),
                  'mechanism_identity_version': manifest.get('mechanism_identity_version', 1), 'eaggl_import_id': manifest.get('eaggl_import_id')}
        touched = False
        for row in rows:
            item = {key: row.get(key) for key in RELEASE_FACTOR_COLUMNS}
            item['trait_metadata'] = {'kpn_trait_id': row.get('kpn_trait_id'), 'phenotype_name': row.get('phenotype_name')}
            try: node = _compute_mechanism(item, source)
            except (ValueError, KeyError, TypeError):
                issue('invalid_factor_capture', file_id if file_id in eligible else None); continue
            if node['id'] in blocked or node['id'] not in wanted_mechanisms: continue
            bind(node['id'], release_id, source, item, node); touched = True
        if touched:
            capture_sources.setdefault(release_id, []).append(source)
            observe('box_capture', checksum, rows)
            for manifest_checksum, manifest_rows in captured: observe('box_capture', manifest_checksum, manifest_rows)

    # Identity binding candidates (module docstring): referenced, bound by no capture, without lineage of their own. A
    # Mechanism node must name its public factor id; a GeneSet is looked up only if no projection binds it either.
    native = _native_lineage(document)
    identity_lookup, publics, identity_refs = {}, defaultdict(set), []
    for identity in sorted(wanted_mechanisms):
        if bindings[identity] or identity in conflicts or identity in native or identity not in node_pool: continue
        match = KPN_DESCRIPTION.match(str(node_pool[identity].get('description') or ''))
        if match: publics[match.group(1)].add(identity)
        else: identity_lookup[identity] = 'not_applicable'
    pending_sets = {identity for identity in wanted_sets if not set_generations.get(identity) and identity not in native}

    def choose():
        chosen = {}
        for identity in sorted(wanted_mechanisms):
            candidates = list(bindings[identity].values())
            if len(candidates) != 1 or identity in conflicts:
                if candidates:
                    issue('ambiguous_reference_binding', identity); conflicts.add(identity)
                metadata['retained_projection_coverage'].append({'mechanism_id': identity, 'status': 'unresolved',
                    'known_factor_binding': bool(bindings[identity]),
                    **({'identity_lookup': identity_lookup[identity]} if identity in identity_lookup else {})})
            else:
                chosen[identity] = candidates[0]
        return chosen

    if not capture_sources and not wanted_traits and not publics and not pending_sets:
        choose()
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

    superseded, current = set(), None
    connection = (connection_factory or reference_mysql_connection)()
    try:
        released = {generation for generation, sources in capture_sources.items() if any(_released(source) for source in sources)}
        # A release reader's first statement: the publisher's RENAME waits on it (reference_pin).
        release = current_release(connection) if released or wanted_traits or publics or pending_sets else None
        # Work captured from a release that is no longer published is hydrated only from capture bytes and archived snapshots:
        # such an account binds nothing by identity, so none of it is read from the current tables.
        stale = any(not release or generation != release['release_id'] for generation in released)
        if stale: pending_sets = set()
        if publics and (release is None or stale):
            for identity in set().union(*publics.values()):
                identity_lookup[identity] = 'superseded_release' if stale and release else 'no_published_release'
        elif publics:
            # A Mechanism binds when the node minted from the published release's factor and trait rows has its id.
            source = _identity_source(release)
            try: rows = _batched(connection, _ref(IDENTITY_FACTORS), [], publics, IDENTITY_COLUMNS)
            except Exception as error:
                if not _missing(error): raise
                issue('reference_tables_unavailable'); rows = []
            from .acceptance import public_runtime
            runtime, found = public_runtime(), defaultdict(list)
            for row in rows:
                item = {key: row[key] for key in RELEASE_FACTOR_COLUMNS}
                item['trait_metadata'] = {'kpn_trait_id': row['kpn_trait_id'], 'phenotype_name': row['phenotype_name']}
                try: node = _compute_mechanism(item, source, runtime)
                except (ValueError, KeyError, TypeError): continue
                if node['id'] in publics.get(row['public_id'], ()): found[node['id']].append((item, row))
            for identity in sorted(set().union(*publics.values())):
                matches = found.get(identity, [])
                if len(matches) != 1:
                    identity_lookup[identity] = 'ambiguous' if matches else 'no_match'
                    if matches: issue('ambiguous_identity_binding', identity)
                    continue
                item, row = matches[0]; generation = release['release_id']
                identity_lookup[identity] = 'matched'
                # The document's own node: merging it again is a no-op.
                bindings[identity][(generation, item['public_id'], item['eaggl_factor_id'])] = {'generation_id': generation,
                    'source': source, 'item': item, 'node': deepcopy(node_pool[identity]), 'binding_origin': IDENTITY}
                candidate_nodes[identity] = deepcopy(node_pool[identity])
                capture_sources.setdefault(generation, []).append(source); released.add(generation)
                identity_refs.append({'reference_id': identity, 'kind': 'mechanism', **row})
        chosen = choose()
        generations = sorted(set(capture_sources))
        valid = {}
        legacy = [generation for generation in generations if generation not in released]
        try:
            generation_rows = _batched(connection, 'SELECT ' + ','.join(GENERATION_COLUMNS) +
                ' FROM reference_generations WHERE generation_id IN ({marks})', [], legacy, GENERATION_COLUMNS) if legacy else []
        except Exception as error:
            if not _missing(error): raise
            issue('reference_tables_unavailable'); generation_rows = []
        generation_map = defaultdict(list)
        for row in generation_rows: generation_map[row['generation_id']].append(row)
        for generation in generations:
            sources = capture_sources[generation]
            if generation in released:
                observe('reference_release', generation, [{'release_id': release['release_id'], 'manifest': release['manifest']}] if release else [])
                if any(not _released(source) for source in sources):
                    issue('reference_generation_conflict'); continue
                if not release or release['release_id'] != generation:
                    superseded.add(generation); issue(SUPERSEDED); continue
                manifest = release['manifest']
                if any(source.get('model') != KPN_MODEL or source.get('eaggl_import_id') != manifest.get('eaggl_import_id')
                       or source.get('generation_manifest_sha256', sha256(canonical_json(manifest))) != sha256(canonical_json(manifest))
                       or source.get('mechanism_identity_version', 1) != manifest.get('mechanism_identity_version', 1) for source in sources):
                    issue('reference_generation_conflict'); continue
                valid[generation] = {'model': RELEASE, 'release': release}
                continue
            rows = generation_map[generation]
            observe('reference_generation', generation, rows)
            if len(rows) != 1 or rows[0]['status'] not in ('complete', 'superseded'):
                issue('reference_generation_unavailable'); continue
            gen = rows[0]
            if any(source.get('model') != gen['model'] or source.get('eaggl_import_id') != gen['eaggl_import_id']
                   or source.get('generation_manifest_sha256') != sha256(canonical_json(gen['manifest']))
                   or (gen['model'] == KPN_MODEL and source.get('mechanism_identity_version', 1) !=
                       (gen['manifest'] or {}).get('mechanism_identity_version', 1))
                   or (gen['model'] == LEGACY_MODEL and (source.get('mapping_run_id') != gen['legacy_mapping_run_id']
                       or source.get('gene_set_import_id') != gen['legacy_gene_set_import_id'])) for source in sources):
                issue('reference_generation_conflict'); continue
            valid[generation] = gen
        # Cited traits are read from the published release unless the account's work is pinned only to a superseded one: work
        # bound to the published release keeps its fitted traits, their names and GWAS routes, beside superseded work.
        if release and (not superseded or release['release_id'] in valid): current = release['release_id']
        projections = []
        for generation, gen in sorted(valid.items()):
            current_bindings = {identity: binding for identity, binding in chosen.items() if binding['generation_id'] == generation}
            try:
                if gen['model'] == RELEASE:
                    keys = {binding['item']['factor_key'] for binding in current_bindings.values()}
                    rows = _batched(connection, _ref('SELECT ' + ','.join(RELEASE_FACTOR_COLUMNS) +
                        ' FROM reveal_ref_factors WHERE factor_key IN ({marks})'), [], keys, RELEASE_FACTOR_COLUMNS)
                    observe('ref_factors', generation, rows)
                    by_key = defaultdict(list)
                    for row in rows: by_key[row['factor_key']].append(row)
                    exact = {}
                    for identity, binding in current_bindings.items():
                        item = binding['item']; matches = by_key[item['factor_key']]
                        if len(matches) != 1 or any(matches[0][key] != item.get(key) for key in RELEASE_FACTOR_COLUMNS):
                            issue('factor_binding_conflict', identity); conflicts.add(identity); continue
                        exact[item['factor_key']] = identity
                    projection_rows = _batched(connection, _ref('SELECT ' + ','.join(RELEASE_PROJECTION_COLUMNS) +
                        ' FROM reveal_ref_projections WHERE factor_key IN ({marks})'), [], exact, RELEASE_PROJECTION_COLUMNS)
                    observe('ref_projections', generation, projection_rows)
                    counts = defaultdict(int)
                    for row in projection_rows:
                        projections.append((generation, exact[row['factor_key']], row['gene_set_id'], row)); counts[row['factor_key']] += 1
                    for key, identity in exact.items():
                        origin = current_bindings[identity].get('binding_origin')
                        metadata['retained_projection_coverage'].append({'mechanism_id': identity,
                            'reference_generation_id': generation, 'status': 'retained_rows_loaded', 'known_factor_binding': True,
                            'retained_rows': counts[key], 'model_construction_provenance': 'not_established',
                            **({'binding_origin': origin} if origin else {})})
                        fitted(identity, current_bindings[identity]['item']['kpn_trait_id'], None, generation)
                elif gen['model'] == KPN_MODEL:
                    keys = {binding['item']['factor_key'] for binding in current_bindings.values()}
                    columns = ('factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'label')
                    rows = _batched(connection, 'SELECT ' + ','.join(columns) +
                        ' FROM reference_factors WHERE generation_id=%s AND factor_key IN ({marks})', [generation], keys, columns)
                    observe('reference_factors', generation, rows)
                    by_key = defaultdict(list)
                    for row in rows: by_key[row['factor_key']].append(row)
                    exact = {}
                    for identity, binding in current_bindings.items():
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
                        [binding['item']['public_id'] for binding in current_bindings.values()], columns)
                    observe('legacy_factor_bindings', generation, rows)
                    exact = {}
                    for identity, binding in current_bindings.items():
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
            except Exception as error:
                if not _missing(error): raise
                issue('reference_tables_unavailable'); valid.pop(generation)
        for generation, identity, gene_set, row in projections:
            if gene_set not in blocked: set_generations[gene_set].add(generation)
        # Referenced GeneSets still unbound after projections bind to the published release when its ref_gene_sets holds them;
        # a release whose bindings failed validation is never revived.
        unbound = sorted(identity for identity in pending_sets if not set_generations.get(identity) and identity not in blocked)
        if unbound and release and (release['release_id'] in valid or release['release_id'] not in generations):
            generation = release['release_id']
            try: hits = {row['gene_set_id'] for row in _batched(connection, _ref(IDENTITY_SETS), [], unbound, ('gene_set_id',))}
            except Exception as error:
                if not _missing(error): raise
                issue('reference_tables_unavailable'); hits = set()
            if hits and generation not in valid:
                valid[generation] = {'model': RELEASE, 'release': release}; generations.append(generation); generations.sort()
                observe('reference_release', generation, [{'release_id': generation, 'manifest': release['manifest']}])
                current = generation
            for identity in sorted(hits):
                set_generations[identity].add(generation)
                identity_refs.append({'reference_id': identity, 'kind': 'gene_set'})
        if identity_refs and release and release['release_id'] in valid: observe(IDENTITY, release['release_id'], identity_refs)
        for identity, contexts in set_generations.items():
            if len(contexts) > 1:
                conflicts.add(identity); issue('ambiguous_reference_binding', identity)
        loaded = defaultdict(set)
        closures = {}  # (collection, its generating activities) -> merged: one closure serves a collection's sibling sets

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
                try:
                    if gen['model'] == RELEASE:
                        # Gene sets the document reaches get their full node (conflicts with its own are detected); a gene set
                        # reached only by a projection gets its name, collection and generating activity, never its members.
                        full = _batched(connection, _ref('SELECT gene_set_id,collection_id,metadata FROM reveal_ref_gene_sets'
                            ' WHERE gene_set_id IN ({marks})'), [], [identity for identity in sets if identity in cited_sets],
                            ('gene_set_id', 'collection_id', 'metadata'))
                        light = _batched(connection, _ref("SELECT gene_set_id,collection_id,gene_set_name,"
                            "JSON_EXTRACT(metadata,'$.dapper_gene_set.was_generated_by'),JSON_EXTRACT(metadata,'$.dapper_gene_set.was_derived_from')"
                            ' FROM reveal_ref_gene_sets WHERE gene_set_id IN ({marks})'), [], [identity for identity in sets if identity not in cited_sets],
                            ('gene_set_id', 'collection_id', 'gene_set_name', 'was_generated_by', 'was_derived_from'))
                        observe('ref_gene_sets', generation, full + light)
                        found, nodes = set(), {}
                        for row in full:
                            node = (row['metadata'] or {}).get('dapper_gene_set')
                            if not isinstance(node, dict) or node.get('id') != row['gene_set_id']:
                                issue('gene_set_definition_unavailable', row['gene_set_id']); continue
                            nodes[row['gene_set_id']] = (row['collection_id'], {key: value for key, value in node.items() if key not in STRIP_GENE_SET},
                                                         (row['metadata'] or {}).get('dapper_dependencies') or [], True)
                        for row in light:
                            node = {'id': row['gene_set_id'], 'name': row['gene_set_name'], 'collection_id': row['collection_id'],
                                    **{key: _json_value(row[key]) for key in ('was_generated_by', 'was_derived_from') if row[key] is not None}}
                            nodes[row['gene_set_id']] = (row['collection_id'], node, [], row['gene_set_id'] not in node_pool)
                        found.update(row['gene_set_id'] for row in full + light)
                        for identity in sorted(set(sets) - found): issue('gene_set_definition_unavailable', identity)
                        collections = sorted({collection for collection, _, _, _ in nodes.values()})
                        shas = {row['collection_id']: _json_value(row['document_sha256']) for row in _batched(connection, _ref(
                            "SELECT collection_id,JSON_EXTRACT(payload,'$.document_sha256') FROM reveal_ref_collections"
                            ' WHERE collection_id IN ({marks})'), [], collections, ('collection_id', 'document_sha256'))}
                        observe('ref_collections', generation, [[collection, sha] for collection, sha in shas.items()])
                        lineages = {collection: _LINEAGE.get(generation, collection, sha) for collection, sha in shas.items()}
                        missing = [collection for collection, cached in lineages.items() if cached is None]
                        if missing:
                            from .reference_lineage import collection_lineage, lineage_index
                            for row in _batched(connection, _ref('SELECT collection_id,payload AS raw FROM reveal_ref_collections'
                                    ' WHERE collection_id IN ({marks})'), [], missing, ('collection_id', 'raw')):
                                raw = row['raw'] if isinstance(row['raw'], str) else json.dumps(row['raw'])
                                payload = json.loads(raw)
                                lineage = collection_lineage(payload)
                                lineages[row['collection_id']] = (lineage, lineage_index(lineage), _curated(payload))
                                del payload
                                _LINEAGE.put(generation, row['collection_id'], shas[row['collection_id']], *lineages[row['collection_id']], len(raw))
                        from .acceptance import public_runtime
                        groups = {cls: group for group, cls in public_runtime().groups.items()}
                        for identity, (collection, node, dependencies, add) in sorted(nodes.items()):
                            source = {'kind': RELEASE, 'reference_generation_id': generation, 'collection_id': collection}
                            if not lineages.get(collection):
                                issue('collection_provenance_unavailable', identity)
                                if add: merge({'gene_sets': [node]}, source)
                                continue
                            lineage, index, curated = lineages[collection]
                            if add: merge({'gene_sets': [node]}, source)
                            # Sibling sets generated by the same activities share one closure; a set the lineage itself
                            # relates (was_generated_by edges) has its own. Curated edges are labelled curated_lineage.
                            from .account_provenance import _refs
                            own = bool(index.targets('was_generated_by', identity))
                            key = (collection, tuple(sorted(_refs(node.get('was_generated_by')))), identity if own else None)
                            if key not in closures:
                                closures[key] = _label_released(_closure({'was_generated_by': list(key[1]), **({'id': identity} if own else {})},
                                    lineage, index), curated, {'reference_generation_id': generation, 'collection_id': collection})
                                inherit_set_bindings(closures[key], generation)
                                merge(closures[key], source)
                            extra = defaultdict(list)
                            for dependency in dependencies:
                                cls = dependency.get('id', '').split(':')[-1].split('.')[0] if isinstance(dependency, dict) else None
                                if cls in groups: extra[groups[cls]].append(dependency)
                            extra['gene_sets'] = [node]
                            inherit_set_bindings(extra, generation)
                            extra.pop('gene_sets')
                            if extra: merge(extra, source)
                    elif gen['model'] == KPN_MODEL:
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
                            curation = _label_curated(graph, payload, {'reference_generation_id': generation, 'collection_id': collection_id})
                            checksum = payload.get('document_sha256')
                            supplement = load_supplement(tx, generation, collection_id, checksum) if checksum else None
                            recovery = {'reference_generation_id': generation, 'collection_id': collection_id,
                                        'status': 'supplement_loaded' if supplement else 'no_registered_supplement',
                                        **({'curated_lineage': curation} if curation else {})}
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
                except Exception as error:
                    if not _missing(error): raise
                    issue('reference_tables_unavailable')
            if not progressed: break
        else:
            _limit()
        for generation, identity, gene_set, row in projections:
            if identity in conflicts or gene_set in conflicts or gene_set in blocked: continue
            origin = chosen[identity].get('binding_origin')
            merge({'mechanisms': [candidate_nodes[identity]]}, {'kind': origin or 'research_capture'})
            merge({'factor_projection_edges': [{'subject': identity, 'predicate': 'reveal:factorProjection',
                'object': gene_set, 'reference_generation_id': generation, **row}]},
                {'kind': 'retained_projection', 'reference_generation_id': generation, **({'binding_origin': origin} if origin else {})})
        kpns = {kpn_trait(identity) for identity in wanted_traits} | set(traits)
        if current and kpns:
            try:
                trait_rows = _batched(connection, _ref('SELECT kpn_trait_id,phenotype_name,metadata FROM reveal_ref_traits'
                    ' WHERE kpn_trait_id IN ({marks})'), [], kpns, ('kpn_trait_id', 'phenotype_name', 'metadata'))
                phenotype_rows = _batched(connection, _ref('SELECT kpn_trait_id,display_name,metadata FROM reveal_ref_phenotypes'
                    ' WHERE kpn_trait_id IN ({marks})'), [], kpns, ('kpn_trait_id', 'display_name', 'metadata'))
            except Exception as error:
                if not _missing(error): raise
                issue('reference_tables_unavailable'); trait_rows = phenotype_rows = []
            observe('ref_traits', current, trait_rows); observe('ref_phenotypes', current, phenotype_rows)
            atlas = {row['kpn_trait_id']: row for row in trait_rows}
            phenotypes = {row['kpn_trait_id']: row for row in phenotype_rows}
            for identity in sorted(wanted_traits | set(traits)):
                kpn = kpn_trait(identity)
                row, phenotype = atlas.get(kpn), phenotypes.get(kpn)
                if not row and not phenotype:
                    traits.pop(identity, None); issue('trait_unavailable', identity); continue
                bottom = ((row or phenotype)['metadata'] or {}).get('bottom_line') or {}
                portal = bottom.get('portal_trait_id')
                traits[identity] = {'id': identity, 'name': phenotype['display_name'] if phenotype else row['phenotype_name'],
                                    'kpn_trait_id': kpn, 'href': bottom_line_href(portal) if portal else KPN_PAGE.format(kpn[-7:])}
                trait_info[identity] = {'kpn_trait_id': kpn, 'portal_trait_id': portal, 'gwas_artifact': bottom.get('gwas_artifact'),
                                        'gwas_reason': bottom.get('gwas_reason') or (None if bottom.get('gwas_artifact') else 'not_on_portal')}
        covered = {row['mechanism_id'] for row in metadata['retained_projection_coverage']}
        for identity in sorted(wanted_mechanisms - covered):
            if chosen.get(identity, {}).get('generation_id') in superseded: continue
            metadata['retained_projection_coverage'].append({'mechanism_id': identity, 'status': 'unresolved',
                'known_factor_binding': bool(bindings[identity]),
                **({'binding_origin': chosen[identity]['binding_origin']} if chosen.get(identity, {}).get('binding_origin') else {})})
        metadata['reference_generation_ids'] = sorted(set(generations) | ({current} if current and kpns else set()))
    finally:
        try: connection.rollback()
        finally: connection.close()
    # Superseded release: the frozen snapshot the environment archived for the Mechanism (its top gene sets per library),
    # and the trait its capture names. Never the current tables.
    for identity, binding in sorted(chosen.items()):
        if binding['generation_id'] not in superseded or identity in conflicts: continue
        item, generation = binding['item'], binding['generation_id']
        snapshot = archived(identity) if archived else None
        if (isinstance(snapshot, dict) and snapshot.get('generation_id') == generation and snapshot.get('source_id') == item.get('public_id')
                and (snapshot.get('mechanism') or {}).get('id') == identity and isinstance(snapshot.get('top_gene_sets'), list)):
            observe('archived_reference_factor', identity, snapshot)
            rows = [row for row in snapshot['top_gene_sets'] if isinstance(row, dict) and isinstance(row.get('gene_set_id'), str)
                    and row['gene_set_id'] not in blocked]
            merge({'mechanisms': [candidate_nodes[identity]], 'factor_projection_edges': [
                {'subject': identity, 'predicate': 'reveal:factorProjection', 'object': row['gene_set_id'],
                 'reference_generation_id': generation, 'library': row.get('library'), 'joint_rank': row.get('rank'),
                 'joint_loading': row.get('joint_loading'), 'marginal_loading': row.get('marginal_loading')} for row in rows]},
                {'kind': 'archived_snapshot', 'reference_generation_id': generation})
            metadata['retained_projection_coverage'].append({'mechanism_id': identity, 'reference_generation_id': generation,
                'status': 'archived_snapshot_loaded', 'known_factor_binding': True, 'retained_rows': len(rows),
                'model_construction_provenance': 'not_established'})
        else:
            metadata['retained_projection_coverage'].append({'mechanism_id': identity, 'reference_generation_id': generation,
                'status': 'unresolved', 'known_factor_binding': True})
        fitted(identity, item['kpn_trait_id'], (item.get('trait_metadata') or {}).get('phenotype_name'), generation)
    metadata['retained_projection_coverage'].sort(key=lambda row: row['mechanism_id'])
    if not current:  # nothing published, or superseded work: cited traits only as a capture names them
        for identity in sorted(wanted_traits - set(traits)): issue('trait_unavailable', identity)
    for identity, record in sorted(traits.items()):
        if record['name'] is None: record.pop('name')
        merge({'traits': [record]}, {'kind': RELEASE})
    metadata['traits'] = {identity: trait_info[identity] for identity in sorted(trait_info) if identity in traits}
    metadata['issues'] = [issues[key] for key in sorted(issues)]
    metadata['source_observations'] = [observations[key] for key in sorted(observations)]
    metadata['supplement_ids'] = sorted(set(metadata['supplement_ids']))
    metadata['recovery_status'] = sorted({canonical_json(row): row for row in metadata['recovery_status']}.values(),
                                         key=canonical_json)
    metadata['blocked_reference_ids'] = sorted(conflicts)
    return enriched, metadata
