"""Hand-curated CFDE collection lineage for one reference generation (data/cfde-provenance/).

The served KPN generation stores each collection's Datasets and Organizations in ``payload.provenance``
but not the edges that follow ``gene_sets:`` in its source documents, so a GeneSet's generating Activity
reaches no Dataset. A curation file names, per library, which stored Datasets that Activity used
(``data_input`` or ``metadata_input``). ``plan`` derives the edges and proves they keep evidence packages
exact; ``apply`` adds them as ``payload.provenance.used_edges`` beside a ``payload.lineage_curation``
marker after writing a backup; ``rollback`` restores that backup. Everything is a dry run by default and
only adds edges between nodes the collection already stores.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path

from .evidence_package import canonical_json, sha256

FORMAT = 'reveal.curated-collection-lineage/1'
MARKER_FORMAT = 'reveal.lineage-curation/1'
KPN_MODEL = 'eaggl-capped-v1'
ROLES = ('data_input', 'metadata_input')
LOCK = 'reveal:reference-reload'
PREFIXES = {'local': 'reveal_workflow_local', 'qa': 'reveal_workflow_qa', 'prod': 'reveal'}
ORG_FIELDS = ('publisher', 'was_attributed_to', 'has_creator', 'has_contributor', 'funded_by')


class LineageRefused(ValueError):
    """The curation, the stored rows or the plan disagree; nothing was written."""


def _require(condition, message):
    if not condition: raise LineageRefused(message)


def _json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _digest(value):
    return sha256(canonical_json(value))


def _refs(value):
    values = value if isinstance(value, list) else [value]
    return [item for item in values if isinstance(item, str)]


def load_curation(path):
    """(curation, sha256 of its bytes) for a well-formed curation file."""
    raw = Path(path).read_bytes()
    curation = json.loads(raw)
    _require(curation.get('format') == FORMAT, 'Unsupported curation format')
    _require(isinstance(curation.get('curation_id'), str) and curation['curation_id'], 'Curation id is required')
    _require(isinstance(curation.get('generation_id'), str) and len(curation['generation_id']) == 64, 'Curation must pin one generation')
    libraries = curation.get('libraries')
    _require(isinstance(libraries, dict) and libraries, 'Curation lists no libraries')
    for library, entry in libraries.items():
        _require(isinstance(entry, dict), f'Malformed library {library}')
        for item in entry.get('inputs', []):
            _require(item.get('edge_role') in ROLES, f'Unsupported edge role in {library}')
            _require(str(item.get('dataset_id', '')).startswith('dapper:Dataset.') and item.get('name'), f'Malformed input in {library}')
        for item in entry.get('excluded', []):
            _require(str(item.get('dataset_id', '')).startswith('dapper:Dataset.') and item.get('name'), f'Malformed exclusion in {library}')
        _require(entry.get('inputs') or entry.get('unresolved_reason'), f'{library} needs inputs or an unresolved_reason')
    return curation, hashlib.sha256(raw).hexdigest()


def _cursor_rows(connection, sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return cursor.fetchall()


def read_state(connection, generation_id):
    """The generation row, its collections, each collection's generating Activities and one representative
    GeneSet row per (collection, Activity). Reads only; GeneSet members are fetched for representatives alone."""
    rows = _cursor_rows(connection, 'SELECT generation_id,model,status,eaggl_import_id,manifest FROM reference_generations'
                        ' WHERE generation_id=%s', [generation_id])
    _require(len(rows) == 1, 'Reference generation is unavailable')
    generation = dict(zip(('generation_id', 'model', 'status', 'eaggl_import_id', 'manifest'), rows[0]))
    generation['manifest'] = _json(generation['manifest'])
    _require(generation['model'] == KPN_MODEL, 'Only a KPN reference generation can be curated')
    _require(generation['status'] in ('complete', 'superseded'), 'Reference generation is not complete')
    collections = {}
    for collection_id, label, library, payload in _cursor_rows(connection,
            'SELECT collection_id,cfde_label,library,payload FROM cfde_gene_set_collections WHERE generation_id=%s', [generation_id]):
        collections[collection_id] = {'collection_id': collection_id, 'label': label, 'library': library, 'payload': _json(payload)}
    _require(collections, 'Reference generation stores no collections')
    activities, representatives = defaultdict(set), {}
    for gene_set_id, collection_id, activity in _cursor_rows(connection,
            "SELECT gene_set_id,collection_id,JSON_UNQUOTE(JSON_EXTRACT(metadata,'$.dapper_gene_set.was_generated_by'))"
            ' FROM cfde_gene_sets WHERE generation_id=%s ORDER BY gene_set_id', [generation_id]):
        _require(collection_id in collections, f'GeneSet {gene_set_id} names an unknown collection')
        _require(isinstance(activity, str) and activity, f'GeneSet {gene_set_id} has no generating Activity')
        activities[collection_id].add(activity)
        representatives.setdefault((collection_id, activity), gene_set_id)
    chosen = sorted(representatives.values())
    full = {}
    for offset in range(0, len(chosen), 250):
        batch = chosen[offset:offset + 250]
        for gene_set_id, collection_id, metadata in _cursor_rows(connection,
                'SELECT gene_set_id,collection_id,metadata FROM cfde_gene_sets WHERE generation_id=%s AND gene_set_id IN ('
                + ','.join(['%s'] * len(batch)) + ')', [generation_id, *batch]):
            full[gene_set_id] = {'gene_set_id': gene_set_id, 'collection_id': collection_id, 'metadata': _json(metadata)}
    return {'generation': generation, 'collections': collections, 'activities': {key: sorted(value) for key, value in activities.items()},
            'representatives': {key: full[value] for key, value in representatives.items()}}


def _marker(curation, curation_sha, edges):
    return {'format': MARKER_FORMAT, 'curation_id': curation['curation_id'], 'curation_sha256': curation_sha, 'used_edges': edges}


def build_plan(curation, curation_sha, state):
    """Per collection: add (with exact edges and before/after hashes), unchanged, or skip. Refuses anything unclassified."""
    generation_id = state['generation']['generation_id']
    _require(curation['generation_id'] == generation_id, 'Curation pins a different generation')
    libraries, entries, used_inputs = curation['libraries'], [], set()
    for collection_id, collection in sorted(state['collections'].items()):
        library, payload = collection['library'], collection['payload']
        _require(isinstance(payload, dict) and isinstance(payload.get('provenance'), dict), f'{collection_id} stores no provenance')
        _require(library in libraries, f'Library {library} is not curated')
        entry = libraries[library]
        provenance = payload['provenance']
        stored_activities = {node.get('id') for node in provenance.get('activities') or [] if isinstance(node, dict)}
        generating = state['activities'].get(collection_id, [])
        _require(generating, f'{collection_id} has no gene sets')
        for activity in generating:
            _require(activity in stored_activities, f'{collection_id}: generating Activity {activity} is not stored')
        inputs = {item['dataset_id']: item for item in entry.get('inputs', [])}
        excluded = {item['dataset_id']: item for item in entry.get('excluded', [])}
        present = []
        for dataset in provenance.get('datasets') or []:
            identity = dataset.get('id')
            known = inputs.get(identity) or excluded.get(identity)
            _require(known is not None, f'{collection_id}: unclassified upstream dataset {identity}')
            _require(known['name'] == dataset.get('name'), f'{collection_id}: dataset {identity} name differs from the curation')
            if identity in inputs: present.append(inputs[identity])
        before = _digest(payload)
        base = {'collection_id': collection_id, 'label': collection['label'], 'library': library, 'before_sha256': before}
        if not present:
            entries.append({**base, 'action': 'skip', 'reason': entry.get('unresolved_reason') or 'no curated input in this collection',
                            'after_sha256': before})
            continue
        edges = sorted(({'subject': activity, 'predicate': 'prov:used', 'object': item['dataset_id'], 'edge_role': item['edge_role']}
                        for activity in generating for item in present), key=canonical_json)
        used_inputs.update(item['dataset_id'] for item in present)
        existing = provenance.get('used_edges')
        marker = _marker(curation, curation_sha, edges)
        if existing is not None:
            _require(existing == edges and payload.get('lineage_curation') == marker,
                     f'{collection_id} already has different used_edges; refusing to overwrite')
            entries.append({**base, 'action': 'unchanged', 'edges': edges, 'after_sha256': before})
            continue
        after = deepcopy(payload)
        after['provenance']['used_edges'] = edges
        after['lineage_curation'] = marker
        entries.append({**base, 'action': 'add', 'edges': edges, 'after_sha256': _digest(after), 'payload': after})
    for library, entry in libraries.items():
        for item in entry.get('inputs', []):
            _require(item['dataset_id'] in used_inputs, f'Curated input {item["dataset_id"]} ({library}) is stored in no collection')
    summary = defaultdict(lambda: defaultdict(int))
    for entry in entries:
        summary[entry['library']][entry['action']] += 1
        summary[entry['library']]['edges'] += len(entry.get('edges', []))
    identity = [{key: entry.get(key) for key in ('collection_id', 'action', 'before_sha256', 'after_sha256', 'edges')} for entry in entries]
    return {'format': 'reveal.collection-lineage-plan/1', 'generation_id': generation_id, 'curation_id': curation['curation_id'],
            'curation_sha256': curation_sha, 'collections': entries,
            'summary': {library: dict(counts) for library, counts in sorted(summary.items())},
            'plan_sha256': _digest({'generation_id': generation_id, 'curation_sha256': curation_sha, 'collections': identity})}


def verify(plan, state, runtime=None):
    """Gates: edges validate as DAPPER Used; evidence packages stay exact and gain the inputs and their organizations;
    shared nodes are identical across collections; the trace keeps each input Dataset and its organizations."""
    from .provenance_reference import STRIP_GENE_SET, _relevant_graph
    from .provenance_schema import validate_provenance_edges
    from .reference_evidence import _class_groups, _resolve_gene_set
    if runtime is None:
        from .acceptance import public_runtime
        runtime = public_runtime()
    class_groups = _class_groups(runtime)
    report = {'validated_collections': 0, 'representatives_checked': 0, 'shared_nodes_checked': 0}
    observed = defaultdict(dict)
    for entry in plan['collections']:
        if entry['action'] != 'add': continue
        collection = state['collections'][entry['collection_id']]
        old, new = collection['payload'], entry['payload']
        try: validate_provenance_edges(new['provenance'])
        except ValueError as error: raise LineageRefused(f'{entry["collection_id"]}: {error}') from None
        pool = {node['id']: node for group, rows in new['provenance'].items() if isinstance(rows, list)
                and not group.endswith('_edges') for node in rows if isinstance(node, dict) and isinstance(node.get('id'), str)}
        inputs = {edge['object'] for edge in entry['edges']}
        organizations = {ref for dataset in inputs for field in ORG_FIELDS for ref in _refs(pool[dataset].get(field))}
        _require(all(ref in pool for ref in organizations), f'{entry["collection_id"]}: an input names an unstored organization')
        for identity in {edge['subject'] for edge in entry['edges']} | inputs | organizations:
            observed[identity][entry['collection_id']] = canonical_json(pool[identity])
        for (collection_id, activity), row in state['representatives'].items():
            if collection_id != entry['collection_id']: continue
            before = _resolve_gene_set(runtime, row, old, {}, class_groups)
            after = _resolve_gene_set(runtime, row, new, {}, class_groups)
            _require(before[3] is None, f'{row["gene_set_id"]} is not exact before the patch: {before[3]}')
            _require(after[3] is None, f'{row["gene_set_id"]} would no longer be exact: {after[3]}')
            old_ids = {item['id'] for item in before[1]}; new_ids = {item['id'] for item in after[1]}
            reached = {edge['object'] for edge in entry['edges'] if edge['subject'] == activity}
            reached_orgs = {ref for dataset in reached for field in ORG_FIELDS for ref in _refs(pool[dataset].get(field))}
            _require(old_ids <= new_ids and reached | reached_orgs <= new_ids,
                     f'{row["gene_set_id"]}: evidence dependencies do not gain the curated inputs')
            node = {key: value for key, value in row['metadata']['dapper_gene_set'].items() if key not in STRIP_GENE_SET}
            kept = _relevant_graph({**deepcopy(new['provenance']), 'gene_sets': [node]}, {row['gene_set_id']}, set())
            kept_ids = {item['id'] for group, rows in kept.items() if not group.endswith('_edges') for item in rows}
            _require(reached | reached_orgs <= kept_ids, f'{row["gene_set_id"]}: the trace would not reach the curated inputs')
            report['representatives_checked'] += 1
        report['validated_collections'] += 1
    for identity, copies in observed.items():
        _require(len(set(copies.values())) == 1, f'{identity} differs between collections')
        report['shared_nodes_checked'] += 1
    return report


def active_generations(connection):
    """{environment: active reference generation id or None} from each application prefix's reference_active record."""
    result = {}
    for environment, prefix in PREFIXES.items():
        try:
            rows = _cursor_rows(connection, f"SELECT payload FROM {prefix}_records WHERE kind='reference_active' AND id='active'")
        except Exception:
            result[environment] = 'unreadable'; continue
        result[environment] = (_json(rows[0][0]) or {}).get('generation_id') if rows else None
    return result


def _lock(connection):
    rows = _cursor_rows(connection, 'SELECT GET_LOCK(%s,0)', [LOCK])
    _require(rows and rows[0][0] == 1, 'Another reference writer holds the reload lock')


def _unlock(connection):
    try: _cursor_rows(connection, 'SELECT RELEASE_LOCK(%s)', [LOCK])
    except Exception: pass


def plan_from(connection, curation_path, runtime=None):
    curation, curation_sha = load_curation(curation_path)
    state = read_state(connection, curation['generation_id'])
    plan = build_plan(curation, curation_sha, state)
    plan['verification'] = verify(plan, state, runtime)
    plan['active_generations'] = active_generations(connection)
    return plan, state


def public(plan):
    """The plan without full payloads, for printing and run logs."""
    return {**{key: value for key, value in plan.items() if key != 'collections'},
            'collections': [{key: value for key, value in entry.items() if key != 'payload'} for entry in plan['collections']]}


def apply(connection, curation_path, *, expect_plan, backup_dir, allow_production=False, runtime=None):
    """Write the planned edges after a backup, in one transaction, then read them back."""
    _lock(connection)
    try:
        plan, state = plan_from(connection, curation_path, runtime)
        _require(plan['plan_sha256'] == expect_plan, 'The plan changed since it was reviewed; re-run plan')
        if plan['active_generations'].get('prod') == plan['generation_id']:
            _require(allow_production, 'Production serves this generation; pass --allow-production to patch it')
        additions = [entry for entry in plan['collections'] if entry['action'] == 'add']
        backup = None
        if additions:
            directory = Path(backup_dir) / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            directory.mkdir(parents=True, exist_ok=False)
            backup = directory / 'payloads.jsonl.gz'
            with gzip.open(backup, 'wb') as handle:
                for entry in additions:
                    handle.write(canonical_json({'generation_id': plan['generation_id'], 'collection_id': entry['collection_id'],
                        'payload': state['collections'][entry['collection_id']]['payload'],
                        'payload_sha256': entry['before_sha256'], 'after_sha256': entry['after_sha256']}).rstrip(b'\n') + b'\n')
                handle.flush(); os.fsync(handle.fileno())
            (directory / 'plan.json').write_text(json.dumps(public(plan), indent=2, sort_keys=True) + '\n')
            try:
                for entry in additions:
                    rows = _cursor_rows(connection, 'SELECT payload FROM cfde_gene_set_collections WHERE generation_id=%s AND collection_id=%s',
                                        [plan['generation_id'], entry['collection_id']])
                    _require(len(rows) == 1 and _digest(_json(rows[0][0])) == entry['before_sha256'],
                             f'{entry["collection_id"]} changed after planning')
                    with connection.cursor() as cursor:
                        cursor.execute('UPDATE cfde_gene_set_collections SET payload=%s WHERE generation_id=%s AND collection_id=%s',
                                       (json.dumps(entry['payload'], ensure_ascii=False, sort_keys=True), plan['generation_id'], entry['collection_id']))
                        _require(cursor.rowcount == 1, f'{entry["collection_id"]} was not updated exactly once')
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        for entry in additions:
            rows = _cursor_rows(connection, 'SELECT payload FROM cfde_gene_set_collections WHERE generation_id=%s AND collection_id=%s',
                                [plan['generation_id'], entry['collection_id']])
            _require(_digest(_json(rows[0][0])) == entry['after_sha256'], f'{entry["collection_id"]} read back differently')
        return {**public(plan), 'applied': len(additions),
                'backup': str(backup) if backup else None,
                'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest() if backup else None}
    finally:
        _unlock(connection)


def rollback(connection, backup_path, *, apply=False):
    """Restore backed-up payloads where the row still holds exactly the curated version; dry run by default."""
    with gzip.open(backup_path, 'rb') as handle: rows = [json.loads(line) for line in handle if line.strip()]
    _require(rows, 'Backup is empty')
    plan = []
    for row in rows:
        _require(_digest(row['payload']) == row['payload_sha256'], f'Backup row {row["collection_id"]} is corrupt')
        current = _cursor_rows(connection, 'SELECT payload FROM cfde_gene_set_collections WHERE generation_id=%s AND collection_id=%s',
                               [row['generation_id'], row['collection_id']])
        _require(len(current) == 1, f'{row["collection_id"]} is no longer stored')
        sha = _digest(_json(current[0][0]))
        if sha == row['payload_sha256']: plan.append((row, 'already_restored'))
        else:
            _require(sha == row['after_sha256'], f'{row["collection_id"]} changed since the patch; refusing to overwrite')
            plan.append((row, 'restore'))
    result = {'restore': sum(action == 'restore' for _, action in plan), 'already_restored': sum(action != 'restore' for _, action in plan),
              'applied': False}
    if not apply or not result['restore']: return result
    _lock(connection)
    try:
        try:
            for row, action in plan:
                if action != 'restore': continue
                with connection.cursor() as cursor:
                    cursor.execute('UPDATE cfde_gene_set_collections SET payload=%s WHERE generation_id=%s AND collection_id=%s',
                                   (json.dumps(row['payload'], ensure_ascii=False, sort_keys=True), row['generation_id'], row['collection_id']))
                    _require(cursor.rowcount == 1, f'{row["collection_id"]} was not restored exactly once')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    finally:
        _unlock(connection)
    for row, action in plan:
        current = _cursor_rows(connection, 'SELECT payload FROM cfde_gene_set_collections WHERE generation_id=%s AND collection_id=%s',
                               [row['generation_id'], row['collection_id']])
        _require(_digest(_json(current[0][0])) == row['payload_sha256'], f'{row["collection_id"]} read back differently')
    return {**result, 'applied': True}
