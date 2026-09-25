"""Validated, immutable DisMech source projections for Aurora/MySQL.

No embedding calls or DAPPER transformation. All exported source fields survive
in payload JSON; searchable columns and foreign keys are an additional projection.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import gzip
import hashlib
from itertools import islice
import json
import logging
from pathlib import Path

from .mysql_database import insert_batch

MIGRATION = Path(__file__).resolve().parents[4] / 'schema/migrations/003_dismech.sql'
VERSION = 'dismech-import-v1'
GAP_KINDS = {'KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH'}
FILES = {
    'documents': 'entities.jsonl.gz', 'mechanisms': 'mechanisms.jsonl.gz',
    'causal_edges': 'causal-edges.jsonl.gz', 'hypotheses': 'hypotheses.jsonl.gz',
    'ontology_terms': 'ontology-terms.jsonl.gz', 'vocabulary': 'vocabulary.jsonl.gz',
    'discussions': 'discussions.jsonl.gz', 'knowledge_gaps': 'knowledge-gaps.jsonl.gz',
    'gap_attachments': 'gap-attachments.jsonl.gz',
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_bytes())


def source_index(rows):
    result = {}
    for row in rows:
        path, sha = row['path'], row['sha256']
        require(path not in result, f'Duplicate source file: {path}')
        require(len(sha) == 64 and all(c in '0123456789abcdef' for c in sha), f'Invalid source hash: {path}')
        result[path] = sha
    return result


def unique(rows, key, label):
    result = {}
    for row in rows:
        identity = key(row)
        require(identity not in result, f'Duplicate {label}: {identity}')
        result[identity] = row
    return result


def locator(row):
    return row['document_id'] + '#' + row['json_pointer']


@dataclass
class Export:
    manifest: dict
    rows: dict
    source_files: dict
    schema_vocabularies: dict

    @property
    def import_id(self):
        return self.manifest['import_id']


def open_export(source, gaps):
    """Fully validate both snapshots before any connection or database write."""
    source, gaps = Path(source), Path(gaps)
    manifests = {name: read_json(root / 'manifest.json') for name, root in [('source', source), ('gaps', gaps)]}
    for name, manifest in manifests.items():
        require(manifest.get('complete') is True and not manifest.get('errors'), f'{name}: incomplete extraction')
        require(isinstance(manifest.get('source_commit'), str) and 1 <= len(manifest['source_commit']) <= 64,
                f'{name}: missing/invalid source commit')
    require(manifests['source']['source_commit'] == manifests['gaps']['source_commit'], 'Snapshot commits differ')
    inventories = {name: source_index(read_json(root / 'source-files.json'))
                   for name, root in [('source', source), ('gaps', gaps)]}
    kb_files = {path: sha for path, sha in inventories['source'].items() if path.startswith('kb/')}
    require(kb_files == inventories['gaps'], 'Snapshot KB source-file hashes differ')
    require(len(kb_files) == manifests['source']['yaml_files'] == manifests['gaps']['yaml_files'],
            'Source-file count differs from manifest')
    schema_vocab = read_json(source / 'schema-vocabularies.json')
    rows, artifacts = {}, {}
    for stage, filename in FILES.items():
        group = 'gaps' if stage in ('discussions', 'knowledge_gaps', 'gap_attachments') else 'source'
        path = (gaps if group == 'gaps' else source) / filename
        metadata = manifests[group]['files'][filename]
        raw = path.read_bytes()
        require(digest(raw) == metadata['sha256'], f'{filename}: checksum mismatch')
        records = [json.loads(line) for line in gzip.decompress(raw).decode('utf-8').splitlines()]
        require(all(isinstance(row, dict) for row in records), f'{filename}: expected JSON objects')
        require(len(records) == metadata['rows'], f'{filename}: row count mismatch')
        rows[stage] = records
        artifacts[stage] = metadata
    docs = unique(rows['documents'], lambda r: r['id'], 'document')
    require(len(docs) == len(kb_files), 'Document/source-file count mismatch')
    require({r['source_file'] for r in docs.values()} == set(kb_files), 'Document source files do not cover KB')
    for row in docs.values():
        expected_id = 'dismech:' + row['source_file'][3:].removesuffix('.yaml')
        require(row['id'] == expected_id, 'Document ID/source file mismatch')
    mechanisms = unique(rows['mechanisms'], lambda r: r['id'], 'mechanism')
    unique(rows['causal_edges'], lambda r: r['id'], 'causal edge')
    unique(rows['hypotheses'], locator, 'hypothesis')
    unique(rows['ontology_terms'], lambda r: r['id'], 'ontology term')
    unique(rows['vocabulary'], lambda r: r['id'], 'vocabulary entry')
    discussions = unique(rows['discussions'], lambda r: r['id'], 'discussion')
    gap_rows = unique(rows['knowledge_gaps'], lambda r: r['id'], 'knowledge gap')
    for stage in ('mechanisms', 'causal_edges', 'hypotheses', 'discussions'):
        for row in rows[stage]:
            doc = docs.get(row['document_id'])
            require(doc is not None and doc['source_file'] == row['source_file'], f'{stage}: missing/mismatched document')
            if stage in ('mechanisms', 'causal_edges'):
                require(row['id'] == locator(row), f'{stage}: ID/pointer mismatch')
    for row in rows['causal_edges']:
        parent = mechanisms.get(row['source_id'])
        require(parent is not None and parent['document_id'] == row['document_id'], 'Causal edge has invalid parent')
    for row in discussions.values():
        raw = row['raw']
        require(isinstance(row['is_gap'], bool) and row['is_gap'] == (row['kind'] in GAP_KINDS), 'Inconsistent gap kind')
        require(row['kind'] == raw.get('kind') and row['status'] == raw.get('status'), 'Discussion kind/status mismatch')
        require(row['discussion_id'] == raw.get('discussion_id'), 'Discussion ID mismatch')
        require(row['has_stable_source_id'] == bool(row['discussion_id']), 'Discussion stability flag mismatch')
        require(row['id'] == row['document_id'] + '#discussion:' + (row['discussion_id'] or row['source_pointer']),
                'Discussion source ID mismatch')
        require(isinstance(raw.get('prompt'), str) and bool(raw['prompt'].strip()), 'Discussion prompt is missing')
    require(gap_rows == {key: row for key, row in discussions.items() if row['is_gap']},
            'Knowledge gaps are not the exact gap subset of discussions')
    attachments = unique(rows['gap_attachments'], lambda r: (r['gap_id'], r['attachment_index']), 'attachment')
    expected_refs = {(row['id'], i): ref for row in gap_rows.values()
                     for i, ref in enumerate(row['raw'].get('attaches_to') or [])}
    require(set(attachments) == set(expected_refs), 'Attachment coverage differs from gap source references')
    for key, row in attachments.items():
        require(type(row['attachment_index']) is int and row['attachment_index'] >= 0, 'Invalid attachment index')
        require(row['source_reference'] == expected_refs[key], 'Attachment reference mismatch')
        target_doc = row.get('target_document_id')
        if target_doc is not None:
            require(target_doc in docs and docs[target_doc]['source_file'] == row['target_source_file'],
                    'Attachment target document is missing/mismatched')
        if row['resolution'] in ('resolved', 'whole_section', 'whole_document'):
            require(target_doc is not None and isinstance(row.get('target_pointer'), str), 'Resolved attachment lacks a target')
            require(row.get('target_id') == target_doc + ('#' + row['target_pointer'] if row['target_pointer'] else ''),
                    'Attachment target ID/pointer mismatch')
        else:
            require(not row.get('target_id'), 'Unresolved attachment must not invent a target')
    # Verify occurrence provenance too; some references point to sections that are
    # retained only in payloads, not extracted as individual mechanism records.
    for stage in ('ontology_terms', 'vocabulary'):
        for row in rows[stage]:
            for occurrence in row['occurrences']:
                require(occurrence['source_file'] in kb_files, f'{stage}: unknown occurrence source')
                if occurrence.get('mechanism_id'):
                    require(occurrence['mechanism_id'] in mechanisms, 'Unknown occurrence mechanism')
                if occurrence.get('document_id'):
                    require(occurrence['document_id'] in docs, 'Unknown occurrence document')
    counts = {key: len(value) for key, value in rows.items()}
    require(counts['knowledge_gaps'] == manifests['gaps']['knowledge_gaps'], 'Gap manifest count mismatch')
    identity = {'version': VERSION, 'source_commit': manifests['source']['source_commit'], 'artifacts': artifacts,
                'source_files_sha256': digest(canonical(inventories)), 'schema_vocabularies_sha256': digest(canonical(schema_vocab))}
    manifest = {'import_id': digest(canonical(identity)), 'identity': identity, 'counts': counts,
                'gaps_by_kind': dict(Counter(r['kind'] for r in gap_rows.values())),
                'attachment_resolution': dict(Counter(r['resolution'] for r in attachments.values())),
                'exports': manifests}
    export = Export(manifest, rows, inventories, schema_vocab)
    # Encode every projected row now, detecting invalid JSON and oversize SQL text
    # before DDL or partial inserts. LONGTEXT is used for free-form long passages.
    for stage in stages(export):
        for row in stage.rows:
            for column, value in zip(stage.columns, row):
                if isinstance(value, str) and column not in stage.json_columns:
                    limit = 64 if column in ('kind', 'status', 'target_kind', 'resolution', 'target_resolution') else 65535
                    if column in ('prompt', 'description'):
                        limit = 2**32 - 1
                    size = len(value) if limit == 64 else len(value.encode('utf-8'))
                    require(size <= limit, f'{stage.name}.{column}: value exceeds column capacity')
    return export


@dataclass
class Stage:
    name: str
    columns: tuple
    rows: list
    key_columns: tuple = ('id_sha256',)
    json_columns: tuple = ('payload',)

    @property
    def table(self):
        return 'dismech_' + self.name


def stages(export):
    identity, rows = export.import_id, export.rows
    def stage(name, columns, values, keys=('id_sha256',)):
        return Stage(name, ('import_id', *columns, 'payload'),
                     [(identity, *projected, canonical(row)) for row, projected in values], keys)
    yield stage('documents', ('id_sha256', 'source_id', 'name', 'kind', 'source_file', 'source_sha256'),
        ((r, (digest(r['id']), r['id'], r['name'], r['kind'], r['source_file'], export.source_files['source'][r['source_file']])) for r in rows['documents']))
    yield stage('mechanisms', ('id_sha256', 'document_sha256', 'source_id', 'source_pointer', 'name', 'description'),
        ((r, (digest(r['id']), digest(r['document_id']), r['id'], r['json_pointer'], r['name'], r['description'])) for r in rows['mechanisms']))
    yield stage('causal_edges', ('id_sha256', 'mechanism_sha256', 'source_id', 'target_resolution'),
        ((r, (digest(r['id']), digest(r['source_id']), r['id'], r['target_resolution'])) for r in rows['causal_edges']))
    yield stage('hypotheses', ('id_sha256', 'document_sha256', 'source_id', 'source_pointer'),
        ((r, (digest(locator(r)), digest(r['document_id']), locator(r), r['json_pointer'])) for r in rows['hypotheses']))
    yield stage('ontology_terms', ('id_sha256', 'source_id'),
        ((r, (digest(r['id']), r['id'])) for r in rows['ontology_terms']))
    yield stage('vocabulary', ('id_sha256', 'source_id', 'kind', 'label', 'term_id'),
        ((r, (digest(r['id']), r['id'], r['kind'], r['label'], r['term_id'])) for r in rows['vocabulary']))
    yield stage('discussions', ('id_sha256', 'document_sha256', 'source_id', 'discussion_id', 'source_pointer', 'kind', 'status', 'is_gap', 'has_stable_source_id', 'prompt'),
        ((r, (digest(r['id']), digest(r['document_id']), r['id'], r['discussion_id'], r['source_pointer'], r['kind'], r['status'], r['is_gap'], r['has_stable_source_id'], r['raw']['prompt'])) for r in rows['discussions']))
    mechanism_ids = {r['id'] for r in rows['mechanisms']}
    yield stage('gap_attachments', ('gap_sha256', 'attachment_index', 'source_reference', 'resolution', 'target_document_sha256', 'target_mechanism_sha256', 'target_id', 'target_pointer', 'target_kind'),
        ((r, (digest(r['gap_id']), r['attachment_index'], r['source_reference'], r['resolution'],
              digest(r['target_document_id']) if r.get('target_document_id') else None,
              digest(r['target_id']) if r['resolution'] == 'resolved' and r.get('target_id') in mechanism_ids else None,
              r.get('target_id'), r.get('target_pointer'), r.get('target_kind'))) for r in rows['gap_attachments']),
        ('gap_sha256', 'attachment_index'))


def verify_stage(cursor, stage, identity, limit=None):
    """Compare all projected columns and full payloads, including resumed prefixes."""
    expected = stage.rows if limit is None else stage.rows[:limit]
    key_indices = [stage.columns.index(key) for key in stage.key_columns]
    json_indices = [stage.columns.index(key) for key in stage.json_columns]
    by_key = {tuple(row[i] for i in key_indices): row for row in expected}
    cursor.execute(f"SELECT {','.join(stage.columns)} FROM {stage.table} WHERE import_id=%s", (identity,))
    seen = set()
    while batch := cursor.fetchmany(500):
        for db_row in batch:
            key = tuple(db_row[i] for i in key_indices)
            require(key in by_key and key not in seen, f'{stage.name}: unexpected database row')
            wanted, actual = list(by_key[key]), list(db_row)
            for i in json_indices:
                wanted[i], actual[i] = json.loads(wanted[i]), json.loads(actual[i])
            require(actual == wanted, f'{stage.name}: database read-back differs from export')
            seen.add(key)
    require(len(seen) == len(expected), f'{stage.name}: database row count differs from checkpoint/export')


def verify_export(connection, export):
    with connection.cursor() as cursor:
        cursor.execute('SELECT manifest,source_files,schema_vocabularies,status FROM dismech_imports WHERE import_id=%s', (export.import_id,))
        row = cursor.fetchone()
        require(row is not None, 'Import is not present in database')
        require(json.loads(row[0])['identity'] == export.manifest['identity'], 'Import identity differs')
        require(json.loads(row[1]) == export.source_files and json.loads(row[2]) == export.schema_vocabularies,
                'Stored provenance differs from export')
        for stage in stages(export):
            verify_stage(cursor, stage, export.import_id)
        status = row[3]
    return {'import_id': export.import_id, 'status': status, 'verified': True, 'counts': export.manifest['counts']}


def load_export(connection, export, *, batch_size=500, migration=MIGRATION):
    require(batch_size > 0, 'Batch size must be positive')
    locked = False
    with connection.cursor() as cursor:
        cursor.execute('SELECT DATABASE()')
        lock_name = 'dismech:' + digest(cursor.fetchone()[0] + ':' + export.import_id)[:56]
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT GET_LOCK(%s, 0)', (lock_name,))
            locked = cursor.fetchone()[0] == 1
            require(locked, 'Another loader holds this import lock')
            cursor.execute("SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')")
            sql = '\n'.join(line for line in Path(migration).read_text().splitlines() if not line.lstrip().startswith('--'))
            for statement in sql.split(';'):
                if statement.strip():
                    cursor.execute(statement)
            cursor.execute('SELECT manifest,progress,source_files,schema_vocabularies FROM dismech_imports WHERE import_id=%s', (export.import_id,))
            old = cursor.fetchone()
            if old:
                require(json.loads(old[0])['identity'] == export.manifest['identity'], 'Existing import identity conflicts')
                require(json.loads(old[2]) == export.source_files and json.loads(old[3]) == export.schema_vocabularies,
                        'Existing import provenance conflicts')
                progress = json.loads(old[1])
            else:
                progress = {}
                cursor.execute('INSERT INTO dismech_imports (import_id,source_commit,status,manifest,source_files,schema_vocabularies,progress) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    (export.import_id, export.manifest['identity']['source_commit'], 'loading', canonical(export.manifest),
                     canonical(export.source_files), canonical(export.schema_vocabularies), '{}'))
                connection.commit()
            for stage in stages(export):
                loaded = progress.get(stage.name, 0)
                require(type(loaded) is int and 0 <= loaded <= len(stage.rows), f'{stage.name}: invalid checkpoint')
                verify_stage(cursor, stage, export.import_id, loaded)
                iterator = iter(stage.rows[loaded:])
                while batch := list(islice(iterator, batch_size)):
                    insert_batch(cursor, stage.table, stage.columns, batch)
                    loaded += len(batch)
                    progress[stage.name] = loaded
                    cursor.execute('UPDATE dismech_imports SET progress=%s WHERE import_id=%s', (canonical(progress), export.import_id))
                    connection.commit()
                    logging.info('Loaded %s %d/%d', stage.name, loaded, len(stage.rows))
                verify_stage(cursor, stage, export.import_id)
            cursor.execute("UPDATE dismech_imports SET status='complete' WHERE import_id=%s", (export.import_id,))
            connection.commit()
        return {'import_id': export.import_id, 'status': 'complete', 'verified': True, 'counts': export.manifest['counts']}
    except BaseException:
        connection.rollback()
        raise
    finally:
        if locked:
            with connection.cursor() as cursor:
                cursor.execute('SELECT RELEASE_LOCK(%s)', (lock_name,))
