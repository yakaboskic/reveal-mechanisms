#!/usr/bin/env python3
"""One-time migration from reference generations to per-environment reference releases.

Run from the repository root with the backend environment, in this order:

  export-vectors --cache FILE    Read-only on MySQL: copy the served EAGGL factor-label vectors and DisMech
                                 context vectors, each hash-verified, into the release build's SQLite cache.
  freeze-legacy [--apply]        Freeze every legacy (cfde-inc-v2) factor of the served mapping run into
                                 archived_reference_factors; rows already there are never rewritten.
  archive-prod [--apply --backup-dir DIR]
                                 Only when prod (prefix `reveal`) was never cut over: back up reveal_records,
                                 point prod at the KPN generation and run the archive pass of the old cutover.
                                 Run it with the new code deployed: legacy-mode code follows the pointer.
  cleanup [--apply]              After local, QA and prod run the new code: delete the old Upstash namespaces,
                                 bookkeeping records and shared reference tables. Dry run by default.

Nothing in MySQL or Upstash changes without --apply. Each command prints one JSON result on stdout
(progress goes to stderr); a refusal prints {"ok": false, "refused": ...} and exits 2. Secrets are read
only from the environment (the repository .env via python-dotenv) and are never printed.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))

from reveal_backend import reference_archive as archive
from reveal_backend import reference_generation as rg
from reveal_backend.repository import Repository, canonical

# The served state this migration retires (data/reference-reload/2026-10-02-load).
KPN_GENERATION = 'ec3364ae1dfdeeead0110dd8129a16aa03dbd90b11400108e274f8d9ded92c8a'
LEGACY_GENERATION = '16e78f6a1282320367b4e6507ad828fc71bdb38ad06e0e0a7d384026f4414e19'
MAPPING_RUN = '272cfa19d093257863d7e7134776229dbc9b9b018d974e6b311285686833ec31'
EAGGL_RUN = 'd4c0300978778453c847c7c3447a316e55b1b0cfe469c647f148ecc1a77f57d7'
DISMECH_RUN = 'dd922e3b7b405f68626bd1679129764de605fd9e278244cca8de54b304d47824'
PROD_PREFIX = 'reveal'
PROD_NOTIFICATION_NAMESPACE = 'reveal-prod'  # deploy/dig/service.yaml
DAPPER_SNAPSHOT = ROOT / 'data/dapper/2026-09-24-v8'  # the runtime the catalog mints Mechanism ids with
RUN_RE = re.compile(r'[a-f0-9]{64}')
PAGE_ROWS = 1000
BACKUP_PAGE_ROWS = 500
DELETE_BATCH = 10000

# Vector cache shared with the release build: one row per exact input text and kind ('factor_label' or 'context'),
# so a text that is both a factor label and a context keeps a row of each kind.
CACHE_SCHEMA = ('CREATE TABLE IF NOT EXISTS vectors(input_sha256 TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, '
                'dimensions INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(input_sha256, kind))',
                'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
META_KEYS = ('model', 'model_revision', 'provider', 'dimensions')
FACTOR_VECTORS = ('SELECT input_sha256,input_text,vector,vector_sha256 FROM eaggl_name_embeddings '
                  'WHERE run_id=%s AND input_sha256>%s ORDER BY input_sha256 LIMIT %s')
CONTEXT_VECTORS = ('SELECT input_sha256,input_text,vector,vector_sha256 FROM dismech_embedding_vectors '
                   'WHERE run_id=%s AND input_sha256>%s ORDER BY input_sha256 LIMIT %s')

# cleanup: environments that serve releases, and what goes.
ENVIRONMENTS = {'local': 'reveal_workflow_local', 'qa': 'reveal_workflow_qa', 'prod': PROD_PREFIX}
RELEASE_NAMESPACE_KINDS = ('factors', 'contexts', 'gene-sets', 'collections')
SERVED_NAMESPACE_KINDS = ('factors', 'contexts')  # what serving reads (vector_retrieval); cleanup requires these
OLD_NAMESPACE_RE = re.compile(r'(?:local|qa|prod|rehearsal|compose)-(?:f|c|eaggl-factor|dismech-context|cfde-geneset|cfde-collection)-')
RELEASE_NAMESPACE_RE = re.compile(r'[a-z][a-z0-9_-]*-(?:' + '|'.join(RELEASE_NAMESPACE_KINDS) + ')')
RECORD_TABLES = ('reveal_records', 'reveal_workflow_qa_records', 'reveal_workflow_local_records', 'reveal_compose_records')
RECORD_KINDS = ('vector_snapshot', 'vector_batch', 'vector_active', 'vector_archive', 'vector_dispatch', 'vector_failure',
                'vector_import_request', 'vector_inventory', 'vector_quality', 'reference_active', 'reference_control',
                'reference_archive_run', 'reference_reload')
# Drop order: children before the tables their foreign keys name.
SHARED_TABLES = ('reference_vectors', 'factor_gene_set_projections', 'cfde_gene_sets', 'cfde_gene_set_collections',
                 'reference_factors', 'kpn_traits', 'vector_bindings', 'embedding_spaces', 'reference_generations',
                 'eaggl_cfde_gene_set_links', 'eaggl_cfde_factor_links', 'eaggl_cfde_link_runs', 'cfde_gene_set_aliases',
                 'gene_set_imports', 'dapper_objects', 'dismech_embedding_inputs', 'dismech_embedding_vectors',
                 'dismech_embedding_runs', 'eaggl_name_embeddings', 'eaggl_embedding_runs', 'eaggl_graph_edges',
                 'eaggl_graph_nodes', 'eaggl_gene_loadings', 'eaggl_factors', 'eaggl_genes', 'eaggl_imports')
RETIRED_PREFIXES = ('reveal_compose', 'reveal_reload_rehearsal')  # every <prefix>_* table is dropped after SHARED_TABLES
TABLE_RE = re.compile(r'[a-z][a-z0-9_]{0,63}')


def _dismech_source_tables():
    names = set(re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', (ROOT / 'schema/migrations/003_dismech.sql').read_text()))
    return names | {'dismech_imports', 'dismech_documents', 'dismech_mechanisms', 'dismech_causal_edges', 'dismech_hypotheses',
                    'dismech_ontology_terms', 'dismech_vocabulary', 'dismech_discussions', 'dismech_gap_attachments'}


# Never dropped or deleted from: frozen snapshots, DisMech sources and the records of the serving environments
# (whose RECORD_KINDS rows alone are deleted). <prefix>_ref_* release tables are protected by RELEASE_TABLE_RE.
PROTECTED_TABLES = frozenset({'archived_reference_factors', *_dismech_source_tables(),
                              *(f'{prefix}_{suffix}' for prefix in ENVIRONMENTS.values() for suffix in ('records', 'transaction_lock'))})
RELEASE_TABLE_RE = re.compile(r'reveal(?:_[a-z0-9_]+)?_ref_[a-z0-9_]*')


class Refused(RuntimeError):
    """A protection refused the command; nothing further was changed."""


# --------------------------------------------------------------------------------------
# Side effects (tests replace these)


def connect():
    from reveal_backend.runtime_config import mysql_connection
    return mysql_connection()


def repository(prefix):
    return Repository(table_prefix=prefix)


def vector_client(*, write):
    import httpx
    from upstash_vector import Index
    url, key = os.environ.get('UPSTASH_VECTOR_REST_URL', ''), 'UPSTASH_VECTOR_WRITE_TOKEN' if write else 'UPSTASH_VECTOR_REST_TOKEN'
    if not url.startswith('https://') or not os.environ.get(key): raise Refused(f'Configure UPSTASH_VECTOR_REST_URL and {key}')
    index = Index(url=url, token=os.environ[key], retries=1, retry_interval=.2, allow_telemetry=False)
    index._client.timeout = httpx.Timeout(60, connect=10)  # SDK 0.8.0 otherwise waits up to 600 s
    return index


def dapper_runtime():
    from reveal_backend.evidence_package import DapperRuntime
    try: return DapperRuntime(DAPPER_SNAPSHOT)
    except (OSError, ValueError) as error:
        raise Refused(f'The DAPPER runtime {DAPPER_SNAPSHOT} is unavailable ({type(error).__name__}); Mechanism ids cannot be minted') from error


# --------------------------------------------------------------------------------------
# Helpers


def read_only(connection):
    """Run this session's statements in one read-only transaction."""
    with connection.cursor() as cursor:
        cursor.execute('SET SESSION TRANSACTION READ ONLY')
        cursor.execute('START TRANSACTION READ ONLY')


def query(connection, sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return list(cursor.fetchall())


def _json(value): return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _sha256(data): return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()


def _batches(values, size=500):
    values = list(values)
    for start in range(0, len(values), size): yield values[start:start + size]


def _placeholders(values): return ','.join(['%s'] * len(values))


def _tables(connection):
    return {row[0] for row in query(connection, 'SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')}


def legacy_source_ids(connection, mapping_run=MAPPING_RUN):
    """Public source ids of every legacy factor: the CFDE node id of each factor link of the mapping run
    (the catalog's legacy mode serves exactly these, and reference_archive._legacy_factors resolves them)."""
    ids = [row[0] for row in query(connection, 'SELECT cfde_node_id FROM eaggl_cfde_factor_links WHERE run_id=%s ORDER BY factor_index',
                                   (mapping_run,))]
    if not ids: raise Refused(f'Mapping run {mapping_run[:12]} has no factor links')
    malformed = [source for source in ids if rg.model_of_source_id(source) != rg.LEGACY_MODEL]
    if malformed: raise Refused(f'{len(malformed)} factor links are not legacy factor ids, e.g. {malformed[:3]}')
    if len(set(ids)) != len(ids): raise Refused(f'Mapping run {mapping_run[:12]} links a factor id twice')
    return ids


def archived_snapshots(connection, archive_ids):
    """{archive_id: snapshot_sha256} of the given ids already in archived_reference_factors."""
    stored = {}
    for batch in _batches(sorted(set(archive_ids))):
        stored.update(query(connection, 'SELECT archive_id,snapshot_sha256 FROM archived_reference_factors WHERE archive_id IN ('
                            + _placeholders(batch) + ')', batch))
    return stored


# --------------------------------------------------------------------------------------
# export-vectors: served vectors -> SQLite vector cache (read-only on MySQL)


def _pages(connection, sql, params):
    """Every row of a query keyset-paged on its first column: `... >%s ORDER BY <first> LIMIT %s`."""
    last = ''
    while True:
        rows = query(connection, sql, (*params, last, PAGE_ROWS))
        yield from rows
        if len(rows) < PAGE_ROWS: return
        last = rows[-1][0]


def _verified(rows, kind, dimensions, run_id):
    """(input_sha256, kind, text, dimensions, vector) rows; refuses unless each text and vector matches its stored sha256."""
    verified = []
    for sha, text, vector, vector_sha in rows:
        vector = bytes(vector)
        if not isinstance(text, str) or _sha256(text.encode('utf-8')) != sha:
            raise Refused(f'{kind} {sha} of run {run_id[:12]}: the sha256 of its text differs from input_sha256')
        if len(vector) != 4 * dimensions or _sha256(vector) != vector_sha:
            raise Refused(f'{kind} {sha} of run {run_id[:12]}: its vector is not {dimensions} float32 values matching vector_sha256')
        verified.append((sha, kind, text, dimensions, vector))
    return verified


def eaggl_run(connection, run_id):
    rows = query(connection, 'SELECT config,dimensions,expected_rows,loaded_rows,status FROM eaggl_embedding_runs WHERE run_id=%s', (run_id,))
    if not rows: raise Refused(f'Unknown EAGGL embedding run {run_id}')
    config, dimensions, expected, loaded, status = rows[0]
    config = _json(config) or {}
    if status != 'complete' or loaded != expected: raise Refused(f'EAGGL embedding run {run_id[:12]} is not complete')
    if _sha256(canonical(config).encode('utf-8')) != run_id: raise Refused(f'EAGGL embedding run {run_id[:12]}: its config does not hash to its id')
    if config.get('dtype') != 'float32-le' or not all(config.get(key) for key in ('model', 'model_revision', 'provider')):
        raise Refused(f'EAGGL embedding run {run_id[:12]} lacks float32-le vectors or its model, revision or provider')
    if config.get('dimensions') not in (None, dimensions): raise Refused(f'EAGGL embedding run {run_id[:12]}: config and row dimensions differ')
    return {'run_id': run_id, 'config': config, 'dimensions': int(dimensions), 'expected': int(expected)}


def dismech_run(connection, run_id, eaggl):
    rows = query(connection, 'SELECT dismech_import_id,eaggl_embedding_run_id,config,dimensions,expected_vectors,loaded_vectors,status '
                             'FROM dismech_embedding_runs WHERE run_id=%s', (run_id,))
    if not rows: raise Refused(f'Unknown DisMech embedding run {run_id}')
    import_id, target, config, dimensions, expected, loaded, status = rows[0]
    config = _json(config) or {}
    if status != 'complete' or loaded != expected: raise Refused(f'DisMech embedding run {run_id[:12]} is not complete')
    if _sha256(canonical(config).encode('utf-8')) != run_id: raise Refused(f'DisMech embedding run {run_id[:12]}: its config does not hash to its id')
    # Context and factor vectors must share one embedding space: the cache holds one model and dimension.
    if target != eaggl['run_id'] or config.get('eaggl_embedding_run_id') != eaggl['run_id'] or dimensions != eaggl['dimensions']:
        raise Refused(f"DisMech embedding run {run_id[:12]} is not in the space of EAGGL embedding run {eaggl['run_id'][:12]}")
    return {'run_id': run_id, 'dismech_import_id': import_id, 'expected': int(expected)}


def _cache_signature(connection):
    """(column, declared type, not null, primary-key position) of each cache table; [] for an absent table."""
    return {table: [(row[1], row[2].upper(), row[3], row[5]) for row in connection.execute(f'PRAGMA table_info({table})')]
            for table in ('vectors', 'meta')}


def _contract_signature():
    with contextlib.closing(sqlite3.connect(':memory:')) as connection:
        for statement in CACHE_SCHEMA: connection.execute(statement)
        return _cache_signature(connection)


CACHE_SIGNATURE = _contract_signature()


def write_cache(path, rows, meta):
    """Add rows to the cache in one transaction. An identical stored (input_sha256, kind) vector is a no-op; different bytes refuse."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        # Never add tables to another SQLite file (e.g. a DisMech capture's embeddings.sqlite3) or adopt another layout,
        # such as the earlier cache keyed by input_sha256 alone.
        for table, found in _cache_signature(connection).items():
            if found and found != CACHE_SIGNATURE[table]:
                raise Refused(f'{path} is not a vector cache of this contract: table {table} has columns {found}')
        for statement in CACHE_SCHEMA: connection.execute(statement)
        connection.execute('BEGIN IMMEDIATE')
        try:
            stored = dict(connection.execute('SELECT key,value FROM meta'))
            differ = sorted(key for key in META_KEYS if key in stored and stored[key] != meta[key])
            if differ: raise Refused(f'{path} holds vectors of another embedding space ({differ} differ)')
            connection.executemany('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)', [(key, meta[key]) for key in META_KEYS])
            inserted = unchanged = 0
            for batch in _batches(rows):
                hashes = sorted({row[0] for row in batch})
                existing = {(sha, kind): bytes(vector) for sha, kind, vector in connection.execute(
                    'SELECT input_sha256,kind,vector FROM vectors WHERE input_sha256 IN (' + ','.join('?' * len(hashes)) + ')', hashes)}
                conflicts = [f'{row[0]}/{row[1]}' for row in batch if existing.get(row[:2], row[4]) != row[4]]
                if conflicts: raise Refused(f'{path} holds a different vector for {len(conflicts)} (input_sha256, kind) rows, e.g. {conflicts[:3]}')
                new = [row for row in batch if row[:2] not in existing]
                connection.executemany('INSERT INTO vectors(input_sha256,kind,text,dimensions,vector) VALUES (?,?,?,?,?)', new)
                inserted += len(new); unchanged += len(batch) - len(new)
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        total = dict(connection.execute('SELECT kind,COUNT(*) FROM vectors GROUP BY kind'))
    finally: connection.close()
    return {'inserted': inserted, 'unchanged': unchanged, 'cache_vectors': dict(sorted(total.items()))}


def export_vectors(connection, cache, *, eaggl_run_id=EAGGL_RUN, dismech_run_id=DISMECH_RUN):
    """Every factor-label vector of the EAGGL run and context vector of the DisMech run into the cache."""
    read_only(connection)
    try:
        run = eaggl_run(connection, eaggl_run_id)
        context_run = dismech_run(connection, dismech_run_id, run)
        factors = _verified(_pages(connection, FACTOR_VECTORS, (eaggl_run_id,)), 'factor_label', run['dimensions'], eaggl_run_id)
        if len(factors) != run['expected']: raise Refused(f"EAGGL embedding run {eaggl_run_id[:12]} has {len(factors)} vectors, not {run['expected']}")
        logging.info('Verified %d factor label vectors', len(factors))
        contexts = _verified(_pages(connection, CONTEXT_VECTORS, (dismech_run_id,)), 'context', run['dimensions'], dismech_run_id)
        if len(contexts) != context_run['expected']:
            raise Refused(f"DisMech embedding run {dismech_run_id[:12]} has {len(contexts)} vectors, not {context_run['expected']}")
        logging.info('Verified %d context vectors', len(contexts))
    finally: connection.rollback()  # ends the read-only transaction
    # Each kind keeps its own row and vector; a text present under both kinds is only counted.
    shared = len({row[0] for row in factors} & {row[0] for row in contexts})
    config = run['config']
    meta = {'model': config['model'], 'model_revision': config['model_revision'], 'provider': config['provider'],
            'dimensions': str(run['dimensions'])}
    written = write_cache(cache, factors + contexts, meta)
    return {'cache': str(Path(cache).resolve()), 'meta': meta, 'eaggl_embedding_run_id': eaggl_run_id,
            'dismech_embedding_run_id': dismech_run_id, 'dismech_import_id': context_run['dismech_import_id'],
            'factor_labels': len(factors), 'contexts': len(contexts), 'shared_texts': shared, **written}


# --------------------------------------------------------------------------------------
# freeze-legacy: every legacy factor -> archived_reference_factors


def freeze_legacy(connection, *, apply, runtime):
    if not apply: read_only(connection)
    generation = rg.get_generation(connection, LEGACY_GENERATION)
    if (not generation or generation['kind'] != rg.LEGACY_KIND or generation['legacy_mapping_run_id'] != MAPPING_RUN
            or rg.legacy_generation_id(MAPPING_RUN) != LEGACY_GENERATION):
        raise Refused(f'{LEGACY_GENERATION[:12]} is not the registered legacy generation of mapping run {MAPPING_RUN[:12]}')
    sources = legacy_source_ids(connection)
    rows = archive.capture_factors(connection, generation, sources, runtime=runtime, strict=True)
    stored = archived_snapshots(connection, [row['archive_id'] for row in rows])
    new = [row for row in rows if row['archive_id'] not in stored]
    # Archives are never overwritten; a stored snapshot that differs from a fresh capture is reported only.
    differ = sorted(row['archive_id'] for row in rows if stored.get(row['archive_id']) not in (None, row['snapshot_sha256']))
    report = {'apply': apply, 'generation_id': LEGACY_GENERATION, 'mapping_run_id': MAPPING_RUN, 'legacy_factors': len(sources),
              'captured': len(rows), 'already_archived': len(rows) - len(new),
              'stored_snapshot_differs': {'count': len(differ), 'archive_ids': differ[:10]}}
    if apply: report['written'] = archive.write_archived_factors(connection, new) if new else 0
    else:
        connection.rollback()
        report['would_write'] = len(new)
    return report


# --------------------------------------------------------------------------------------
# archive-prod: back up reveal_records, point prod at the KPN generation, archive pass


def _cell(value):
    if isinstance(value, (bytes, bytearray, memoryview)): return {'$base64': base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value, (datetime, date)): return value.isoformat()
    if isinstance(value, Decimal): return str(value)
    return value


def backup_records(repo, directory):
    """Every <prefix>_records row, as of one read snapshot, into a new private JSONL.gz file (paged by primary key)."""
    directory = Path(directory); directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    table = f'{repo.table_prefix}_records'
    path = directory / f"{table}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}.jsonl.gz"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(descriptor, 0o600)
    rows = 0
    try:
        with os.fdopen(descriptor, 'wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as out, \
                repo.read_transaction() as tx:
            last = None
            while True:
                if last is None: cursor = tx.execute('SELECT * FROM reveal_records ORDER BY kind,id LIMIT %s', (BACKUP_PAGE_ROWS,))
                else: cursor = tx.execute('SELECT * FROM reveal_records WHERE kind>%s OR (kind=%s AND id>%s) ORDER BY kind,id LIMIT %s',
                                          (last[0], last[0], last[1], BACKUP_PAGE_ROWS))
                columns = [column[0] for column in cursor.description]
                page = cursor.fetchall()
                for row in page: out.write((canonical(dict(zip(columns, map(_cell, row)))) + '\n').encode('utf-8'))
                rows += len(page)
                if len(page) < BACKUP_PAGE_ROWS: break
                last = (page[-1][columns.index('kind')], page[-1][columns.index('id')])
            total = tx.execute('SELECT COUNT(*) FROM reveal_records').fetchone()[0]
            if total != rows: raise Refused(f'The {table} backup holds {rows} rows, not {total}')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    logging.info('Backed up %d rows of %s', rows, table)
    return {'path': str(path.resolve()), 'table': table, 'rows': rows, 'sha256': _sha256_file(path), 'bytes': path.stat().st_size}


def archive_prod(repo, *, apply, backup_dir=None):
    """Archive prod's legacy work as the old cutover archived local's and QA's. It runs once the new code serves prod, so
    only legacy-generation work is archived (only_from): work bound to a reference release stays current."""
    with repo.read_transaction() as tx: active = rg.read_active(tx)
    if active and active.get('vector_snapshot_id'):  # only the old reload's activation names a vector snapshot
        raise Refused(f"{repo.table_prefix} was cut over by the reference reload (active on {active.get('generation_id', '')[:12]}): "
                      'its work is already archived')
    if active and active.get('generation_id') != KPN_GENERATION:
        raise Refused(f"{repo.table_prefix} is active on {active.get('generation_id')}, not {KPN_GENERATION[:12]}: archive-prod is only for a "
                      'prefix that was never cut over')
    report = {'apply': apply, 'prefix': repo.table_prefix, 'from_generation': LEGACY_GENERATION, 'to_generation': KPN_GENERATION,
              'reference_active': active, 'legacy_jobs': archive.nonterminal_analysis_jobs(repo, LEGACY_GENERATION)}
    if not apply:
        report['archive'] = archive.archive_prefix(repo, LEGACY_GENERATION, KPN_GENERATION, apply=False, only_from=LEGACY_GENERATION)
        return report
    if not backup_dir: raise Refused('--backup-dir is required')
    report['backup'] = backup_records(repo, backup_dir)
    # As the old apply drained: the new worker cannot collect a legacy job, so uncollected ones are cancelled. A collected
    # one can still write an account, so the pass waits for it: run archive-prod again once it has finished.
    report['cancelled_jobs'] = archive.cancel_nonterminal_jobs(repo, LEGACY_GENERATION, apply=True)
    running = [job['id'] for job in archive.nonterminal_analysis_jobs(repo, LEGACY_GENERATION)]
    if running:
        raise Refused(f'{len(running)} legacy analysis jobs are still running ({", ".join(running[:5])}): run archive-prod again once they '
                      'have finished')
    with repo.transaction() as tx:
        # write_active is a no-op when the prefix is already active on the generation.
        report['reference_active'] = rg.write_active(tx, KPN_GENERATION, rg.KPN_MODEL, expected_previous=None)
    # As the old apply ran its archive pass: in legacy mode, rows without a derivable generation assume the legacy one.
    report['archive'] = archive.archive_prefix(repo, LEGACY_GENERATION, KPN_GENERATION, apply=True, only_from=LEGACY_GENERATION)
    return report


# --------------------------------------------------------------------------------------
# cleanup: old namespaces, bookkeeping records and shared tables


def protected_table(name): return name in PROTECTED_TABLES or bool(RELEASE_TABLE_RE.fullmatch(name))


def guard(verb, table):
    """Refuse a cleanup statement that would hit a protected table, or anything outside the cleanup's tables."""
    if not isinstance(table, str) or not TABLE_RE.fullmatch(table): raise Refused(f'Invalid table name {table!r}')
    if verb == 'delete' and table in RECORD_TABLES: return  # only RECORD_KINDS rows, see delete_sql
    retired = table.startswith(tuple(prefix + '_' for prefix in RETIRED_PREFIXES))
    if verb != 'drop' or protected_table(table) or not (table in SHARED_TABLES or retired):
        raise Refused(f'Refusing to {verb} protected table {table}')


def guard_namespace(name):
    if not isinstance(name, str) or RELEASE_NAMESPACE_RE.fullmatch(name) or not OLD_NAMESPACE_RE.match(name):
        raise Refused(f'Refusing to delete Vector namespace {name!r}')


def delete_sql(table):
    guard('delete', table)
    return f'DELETE FROM `{table}` WHERE kind IN ({_placeholders(RECORD_KINDS)}) LIMIT {int(DELETE_BATCH)}'


def drop_sql(table):
    guard('drop', table)
    return f'DROP TABLE `{table}`'


def readiness(connection, tables, namespaces):
    """Blockers: legacy factors not yet frozen, or an environment without a published release."""
    blockers, legacy = [], {'skipped': 'eaggl_cfde_factor_links is gone'}
    if 'eaggl_cfde_factor_links' in tables:
        try: sources = legacy_source_ids(connection)
        except Refused as error:
            legacy = {'error': str(error)}; blockers.append(f'Legacy factors cannot be checked: {error}')
        else:
            expected = {rg.archive_id(LEGACY_GENERATION, source) for source in sources}
            present = ({row[0] for row in query(connection, 'SELECT archive_id FROM archived_reference_factors WHERE generation_id=%s',
                                                (LEGACY_GENERATION,))} if 'archived_reference_factors' in tables else set())
            legacy = {'expected': len(expected), 'missing': len(expected - present)}
            if expected - present: blockers.append(f'{len(expected - present)} legacy factors are not frozen: run freeze-legacy --apply first')
    environments = {}
    for environment, prefix in ENVIRONMENTS.items():
        released = sorted(table for table in tables if table.startswith(prefix + '_ref_') and not table.endswith(('__new', '__old')))
        # Published = the swap happened: <prefix>_ref_release holds the release row (leftover __new tables don't count).
        release = (query(connection, f'SELECT release_id FROM `{prefix}_ref_release` LIMIT 1') if f'{prefix}_ref_release' in tables else [])
        present = {f'{environment}-{kind}': f'{environment}-{kind}' in namespaces for kind in RELEASE_NAMESPACE_KINDS}
        environments[environment] = {'prefix': prefix, 'release_id': release[0][0] if release else None, 'release_tables': released,
                                     'namespaces': present}
        if not release or not all(present[f'{environment}-{kind}'] for kind in SERVED_NAMESPACE_KINDS):
            blockers.append(f'{environment} has no published release ({prefix}_ref_* tables, {environment}-factors and {environment}-contexts)')
    return {'legacy_factors': legacy, 'environments': environments}, blockers


def cleanup_plan(connection, client):
    tables, namespaces = _tables(connection), sorted(client.list_namespaces())
    doomed = [name for name in namespaces if OLD_NAMESPACE_RE.match(name) and not RELEASE_NAMESPACE_RE.fullmatch(name)]
    for name in doomed: guard_namespace(name)
    records = {}
    for table in RECORD_TABLES:
        if table not in tables: continue
        counts = query(connection, f'SELECT kind,COUNT(*) FROM `{table}` WHERE kind IN ({_placeholders(RECORD_KINDS)}) GROUP BY kind', RECORD_KINDS)
        records[table] = {kind: int(count) for kind, count in sorted(counts)}
    drops = [table for table in SHARED_TABLES if table in tables]
    drops += sorted(table for table in tables if table.startswith(tuple(prefix + '_' for prefix in RETIRED_PREFIXES)))
    statements = [delete_sql(table) for table in records] + [drop_sql(table) for table in drops]
    ready, blockers = readiness(connection, tables, set(namespaces))
    return {'namespaces': {'delete': doomed, 'keep': [name for name in namespaces if name not in doomed]}, 'records': records,
            'record_kinds': list(RECORD_KINDS), 'tables': drops, 'statements': statements, 'readiness': ready, 'blockers': blockers}


def cleanup(connection, client, *, apply):
    if not apply: read_only(connection)
    plan = cleanup_plan(connection, client)
    if not apply:
        connection.rollback()
        return {'apply': False, **plan}
    connection.commit()
    if plan['blockers']: raise Refused(f"Not ready for cleanup: {plan['blockers']}")
    done = {'namespaces_deleted': [], 'records_deleted': {}, 'tables_dropped': []}
    for name in plan['namespaces']['delete']:
        guard_namespace(name)
        client.delete_namespace(name)
        done['namespaces_deleted'].append(name)
        logging.info('Deleted Vector namespace %s', name)
    for table in plan['records']:
        sql, total = delete_sql(table), 0
        while True:
            with connection.cursor() as cursor:
                cursor.execute(sql, RECORD_KINDS)
                count = max(cursor.rowcount or 0, 0)
            connection.commit(); total += count
            if count < DELETE_BATCH: break
        done['records_deleted'][table] = total
        logging.info('Deleted %d bookkeeping records from %s', total, table)
    # Fail fast instead of queueing behind a stale reader (the default waits a year); a re-run resumes.
    if plan['tables']:
        with connection.cursor() as cursor: cursor.execute('SET SESSION lock_wait_timeout=60')
    for table in plan['tables']:
        with connection.cursor() as cursor: cursor.execute(drop_sql(table))
        done['tables_dropped'].append(table)
        logging.info('Dropped %s', table)
    return {'apply': True, **plan, **done}


# --------------------------------------------------------------------------------------
# CLI


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    c = commands.add_parser('export-vectors', help='Read-only: served factor-label and context vectors into the SQLite vector cache')
    c.add_argument('--cache', type=Path, required=True)
    c.add_argument('--eaggl-run', default=EAGGL_RUN, help='EAGGL embedding run of the factor labels')
    c.add_argument('--dismech-run', default=DISMECH_RUN, help='DisMech embedding run of the contexts')
    c = commands.add_parser('freeze-legacy', help='Freeze every legacy factor into archived_reference_factors (dry run unless --apply)')
    c.add_argument('--apply', action='store_true')
    c = commands.add_parser('archive-prod', help='Prod never cut over: backup, pointer, archive pass (dry run unless --apply)')
    c.add_argument('--apply', action='store_true')
    c.add_argument('--backup-dir', type=Path, help='Required with --apply: directory of the private reveal_records backup')
    c = commands.add_parser('cleanup', help='After every environment runs the new code: list (default) or --apply the removal')
    c.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'export-vectors' and not (RUN_RE.fullmatch(args.eaggl_run) and RUN_RE.fullmatch(args.dismech_run)):
        parser.error('embedding run ids are 64 lowercase hex characters')
    if args.command == 'archive-prod' and args.apply and not args.backup_dir:
        parser.error('archive-prod --apply requires --backup-dir')
    return args


def run(args):
    if args.command == 'archive-prod':
        # Pin this process to prod, as the old cutover pinned its target: notifications of rewritten rows reach prod's channels.
        os.environ.update(REVEAL_APPLICATION_TABLE_PREFIX=PROD_PREFIX, REVEAL_NOTIFICATION_NAMESPACE=PROD_NOTIFICATION_NAMESPACE)
        return archive_prod(repository(PROD_PREFIX), apply=args.apply, backup_dir=args.backup_dir)
    client = vector_client(write=args.apply) if args.command == 'cleanup' else None
    runtime = dapper_runtime() if args.command == 'freeze-legacy' else None
    connection = connect()
    try:
        if args.command == 'export-vectors':
            return export_vectors(connection, args.cache, eaggl_run_id=args.eaggl_run, dismech_run_id=args.dismech_run)
        if args.command == 'freeze-legacy': return freeze_legacy(connection, apply=args.apply, runtime=runtime)
        return cleanup(connection, client, apply=args.apply)
    finally: connection.close()


def main(argv=None):
    # Read only this project's configuration; preserve shell overrides and literal dollar signs.
    load_dotenv(ROOT / '.env', override=False, interpolate=False)
    args = arguments(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stderr)
    try: result, code = run(args), 0
    except (Refused, rg.ReferenceError) as error: result, code = {'ok': False, 'refused': str(error)}, 2
    print(json.dumps({'command': args.command, **result}, indent=2, ensure_ascii=False, default=str), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
