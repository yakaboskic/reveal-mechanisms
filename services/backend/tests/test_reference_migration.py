"""One-time reference migration script (scripts/reference_migration.py): vector cache export, legacy
factor freeze, prod archive pass and cleanup.

No network or MySQL: shared tables are a scripted fake DB-API connection, application records are
SQLite repositories, Upstash is a fake client and reference_archive.capture_factors is a fake.
"""
import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import reference_migration as cli

from reveal_backend import redis_notifications
from reveal_backend import reference_archive as archive
from reveal_backend import reference_generation as rg
from reveal_backend.repository import Repository, canonical, digest

L, G = cli.LEGACY_GENERATION, cli.KPN_GENERATION
SOURCES = ['factor:portal:T2D:cfde-inc-v2:Factor1', 'factor:portal:T2D:cfde-inc-v2:Factor2', 'factor:portal:BMI:cfde-inc-v2:Factor1']


@pytest.fixture(autouse=True)
def quiet_notifications(monkeypatch):
    monkeypatch.setattr(redis_notifications, 'publish', lambda channels: None)


# --------------------------------------------------------------------------------------
# Fake shared-table connection (the test_reference_reload pattern)


class FakeCursor:
    def __init__(self, db): self.db, self.rows, self.rowcount, self.description = db, [], 0, None
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, sql, params=()):
        sql = ' '.join(sql.split()); self.db.log.append((sql, tuple(params or ())))
        result = self.db.answer(sql, tuple(params or ()))
        if isinstance(result, int): self.rows, self.rowcount = [], result
        else: self.rows = list(result or []); self.rowcount = len(self.rows)
    def executemany(self, sql, rows):
        rows = [tuple(row) for row in rows]; sql = ' '.join(sql.split())
        self.db.log.append((sql, rows)); self.db.answer('MANY ' + sql, rows); self.rowcount = len(rows)
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return list(self.rows)


class FakeDB:
    """Scripted DB-API connection: reference_generations rows are fixed, everything else is a rule or []."""
    def __init__(self, rules=(), generations=(), log=None):
        self.log = [] if log is None else log
        self.commits = self.rollbacks = 0; self.closed = False; self.rules = list(rules)
        self.generations = {row['generation_id']: row for row in generations}
    def cursor(self): return FakeCursor(self)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True
    def statements(self, pattern): return [(sql, params) for sql, params in self.log if isinstance(sql, str) and re.search(pattern, sql)]
    def answer(self, sql, params):
        for pattern, value in self.rules:
            if re.search(pattern, sql): return value(sql, params) if callable(value) else value
        if 'FROM reference_generations WHERE generation_id=%s' in sql:
            row = self.generations.get(params[0])
            return [tuple(json.dumps(row[c]) if c in ('manifest', 'cold_export_ref') and row[c] is not None else row[c]
                          for c in rg.GENERATION_COLUMNS)] if row else []
        return []


def writes(db):
    return [sql for sql, _ in db.log if isinstance(sql, str) and re.match(r'(MANY )?(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER)', sql)]


def read_only_first(db):
    return [sql for sql, _ in db.log[:2]] == ['SET SESSION TRANSACTION READ ONLY', 'START TRANSACTION READ ONLY']


# --------------------------------------------------------------------------------------
# export-vectors


def sha(data): return hashlib.sha256(data).hexdigest()


def vector_row(text, values):
    blob = np.asarray(values, dtype='<f4').tobytes()
    return (sha(text.encode('utf-8')), text, blob, sha(blob))


def mysql_json(value):
    """A JSON column as MySQL returns it: keys reordered (length first) and spaced."""
    return json.dumps(dict(sorted(value.items(), key=lambda item: (len(item[0]), item[0]))), separators=(', ', ': '))


DIMS = 4
EAGGL_CONFIG = {'import_id': 'a' * 64, 'model': 'pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb', 'model_revision': 'unspecified',
                'provider': 'huggingface', 'service_url': 'https://embed.invalid', 'template': 'factor-label-v1', 'dtype': 'float32-le',
                'normalization': 'none', 'metric': 'cosine'}
EAGGL_RUN = sha(canonical(EAGGL_CONFIG).encode())
DISMECH_CONFIG = {'version': 'dismech-embeddings-v1', 'dismech_import_id': '4' * 64, 'eaggl_embedding_run_id': EAGGL_RUN,
                  'eaggl_config': EAGGL_CONFIG, 'dimensions': DIMS, 'input_inventory_sha256': '5' * 64}
DISMECH_RUN = sha(canonical(DISMECH_CONFIG).encode())
SHARED = 'Lipid metabolism'
FACTORS = [vector_row('Insulin secretion', (1, 0, 0, 0)), vector_row('Beta cell stress', (0, 1, 0, 0)), vector_row(SHARED, (0, 0, 1, 0))]
CONTEXTS = [vector_row('How does insulin resistance arise in hepatocytes?', (.5, .5, 0, 0)), vector_row(SHARED, (0, 0, 1, 0)),
            vector_row('Hepatic lipid accumulation impairs insulin signalling', (0, 0, .5, .5))]


def paged(rows):
    def answer(sql, params):
        _, last, limit = params
        return [row for row in sorted(rows) if row[0] > last][:limit]
    return answer


def export_db(*, factors=FACTORS, contexts=CONTEXTS, eaggl=None, dismech=None):
    eaggl = eaggl or (mysql_json(EAGGL_CONFIG), DIMS, len(factors), len(factors), 'complete')
    dismech = dismech or ('4' * 64, EAGGL_RUN, mysql_json(DISMECH_CONFIG), DIMS, len(contexts), len(contexts), 'complete')
    return FakeDB(rules=[
        (r'FROM eaggl_embedding_runs WHERE run_id=%s', lambda sql, params: [eaggl] if params[0] == EAGGL_RUN else []),
        (r'FROM dismech_embedding_runs WHERE run_id=%s', lambda sql, params: [dismech] if params[0] == DISMECH_RUN else []),
        (r'FROM eaggl_name_embeddings', paged(factors)),
        (r'FROM dismech_embedding_vectors', paged(contexts))])


def export(db, cache):
    return cli.export_vectors(db, cache, eaggl_run_id=EAGGL_RUN, dismech_run_id=DISMECH_RUN)


def cache_rows(cache):
    """(meta, {(input_sha256, kind): (text, dimensions, vector)})."""
    with contextlib.closing(sqlite3.connect(cache)) as connection:
        meta = dict(connection.execute('SELECT key,value FROM meta'))
        rows = {(row[0], row[1]): (row[2], row[3], bytes(row[4]))
                for row in connection.execute('SELECT input_sha256,kind,text,dimensions,vector FROM vectors')}
    return meta, rows


def test_export_vectors_writes_the_verified_cache_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'PAGE_ROWS', 2)  # several keyset pages per run
    db, cache = export_db(), tmp_path / 'release' / 'vectors.sqlite3'
    result = export(db, cache)
    assert read_only_first(db) and not writes(db) and (db.commits, db.rollbacks) == (0, 1)
    pages = db.statements(r'FROM eaggl_name_embeddings')
    assert [params for _, params in pages] == [(EAGGL_RUN, '', 2), (EAGGL_RUN, sorted(FACTORS)[1][0], 2)]
    # The shared text keeps one row per kind: 3 factor labels + 3 contexts.
    assert {key: result[key] for key in ('factor_labels', 'contexts', 'shared_texts', 'inserted', 'unchanged', 'cache_vectors')} == {
        'factor_labels': 3, 'contexts': 3, 'shared_texts': 1, 'inserted': 6, 'unchanged': 0, 'cache_vectors': {'context': 3, 'factor_label': 3}}
    assert result['dismech_import_id'] == '4' * 64
    meta, rows = cache_rows(cache)
    assert meta == {'model': EAGGL_CONFIG['model'], 'model_revision': 'unspecified', 'provider': 'huggingface', 'dimensions': '4'}
    assert result['meta'] == meta
    assert rows == {**{(row[0], 'factor_label'): (row[1], DIMS, row[2]) for row in FACTORS},
                    **{(row[0], 'context'): (row[1], DIMS, row[2]) for row in CONTEXTS}}
    assert (sha(SHARED.encode()), 'factor_label') in rows and (sha(SHARED.encode()), 'context') in rows
    assert np.frombuffer(rows[CONTEXTS[0][0], 'context'][2], dtype='<f4').tolist() == [.5, .5, 0, 0]
    # Extending the cache with the same vectors is a no-op.
    again = export(export_db(), cache)
    assert (again['inserted'], again['unchanged']) == (0, 6) and cache_rows(cache) == (meta, rows)


def test_export_vectors_keeps_each_kinds_own_vector_for_a_shared_text(tmp_path):
    contexts = CONTEXTS[:1] + [vector_row(SHARED, (0, 0, 0, 1))] + CONTEXTS[2:]  # same text, another run's vector
    cache = tmp_path / 'vectors.sqlite3'
    result = export(export_db(contexts=contexts), cache)
    _, rows = cache_rows(cache)
    assert FACTORS[2][0] == contexts[1][0] and result['shared_texts'] == 1 and result['inserted'] == 6
    assert rows[FACTORS[2][0], 'factor_label'] == (SHARED, DIMS, FACTORS[2][2]) and rows[contexts[1][0], 'context'] == (SHARED, DIMS, contexts[1][2])
    assert FACTORS[2][2] != contexts[1][2]


@pytest.mark.parametrize('table,row,message', [
    ('factors', (FACTORS[0][0], 'Insulin secretion ', *FACTORS[0][2:]), 'sha256 of its text'),
    ('contexts', (*CONTEXTS[0][:2], np.asarray((.5, .5, 0, 1), dtype='<f4').tobytes(), CONTEXTS[0][3]), 'vector_sha256'),
    ('contexts', (*CONTEXTS[0][:2], b'\0' * 12, sha(b'\0' * 12)), '4 float32 values')])
def test_export_vectors_refuses_any_hash_mismatch_before_touching_the_cache(tmp_path, table, row, message):
    rows = {'factors': list(FACTORS), 'contexts': list(CONTEXTS)}
    rows[table][0] = row
    db, cache = export_db(**rows), tmp_path / 'vectors.sqlite3'
    with pytest.raises(cli.Refused, match=message): export(db, cache)
    assert not cache.exists() and read_only_first(db) and not writes(db) and db.rollbacks == 1


def test_export_vectors_refuses_runs_that_are_incomplete_or_in_another_space(tmp_path):
    cache = tmp_path / 'vectors.sqlite3'
    cases = [(export_db(eaggl=(mysql_json(EAGGL_CONFIG), DIMS, 3, 2, 'loading')), 'is not complete'),
             (export_db(eaggl=(mysql_json(dict(EAGGL_CONFIG, model='other')), DIMS, 3, 3, 'complete')), 'does not hash to its id'),
             (export_db(dismech=('4' * 64, 'f' * 64, mysql_json(DISMECH_CONFIG), DIMS, 3, 3, 'complete')), 'not in the space'),
             (export_db(dismech=('4' * 64, EAGGL_RUN, mysql_json(DISMECH_CONFIG), 8, 3, 3, 'complete')), 'not in the space'),
             (export_db(eaggl=(mysql_json(EAGGL_CONFIG), DIMS, 4, 4, 'complete')), 'has 3 vectors, not 4')]
    for db, message in cases:
        with pytest.raises(cli.Refused, match=message): export(db, cache)
        assert not cache.exists() and not writes(db)
    with pytest.raises(cli.Refused, match='Unknown EAGGL'):
        cli.export_vectors(export_db(), cache, eaggl_run_id='0' * 64, dismech_run_id=DISMECH_RUN)


def test_export_vectors_never_overwrites_the_cache(tmp_path):
    cache, other_bytes = tmp_path / 'vectors.sqlite3', b'\1' * 16
    with contextlib.closing(sqlite3.connect(cache)) as connection, connection:
        for statement in cli.CACHE_SCHEMA: connection.execute(statement)
        connection.execute('INSERT INTO vectors VALUES (?,?,?,?,?)', (FACTORS[1][0], 'factor_label', FACTORS[1][1], DIMS, other_bytes))
    with pytest.raises(cli.Refused, match=r'different vector for 1 \(input_sha256, kind\) rows'): export(export_db(), cache)
    meta, rows = cache_rows(cache)
    assert meta == {} and rows == {(FACTORS[1][0], 'factor_label'): (FACTORS[1][1], DIMS, other_bytes)}  # rolled back entirely
    # Rows are keyed per kind: the same text stored under another kind is no conflict, and stays untouched.
    with contextlib.closing(sqlite3.connect(cache)) as connection, connection: connection.execute("UPDATE vectors SET kind='context'")
    result = export(export_db(), cache)
    _, rows = cache_rows(cache)
    assert (result['inserted'], result['unchanged'], len(rows)) == (6, 0, 7)
    assert rows[FACTORS[1][0], 'context'] == (FACTORS[1][1], DIMS, other_bytes)
    assert rows[FACTORS[1][0], 'factor_label'] == (FACTORS[1][1], DIMS, FACTORS[1][2])
    with contextlib.closing(sqlite3.connect(cache)) as connection, connection: connection.execute("UPDATE meta SET value='other/model' WHERE key='model'")
    with pytest.raises(cli.Refused, match=r"another embedding space \(\['model'\]"): export(export_db(), cache)
    # Never adds tables to another SQLite file (a DisMech capture's embeddings.sqlite3) or adopts the earlier cache
    # layout keyed by input_sha256 alone, whose column names are the same.
    for name, create in (('embeddings.sqlite3', 'CREATE TABLE vectors (input_sha256 TEXT PRIMARY KEY,input_text TEXT NOT NULL,'
                                                'vector BLOB NOT NULL,vector_sha256 TEXT NOT NULL)'),
                         ('single-key.sqlite3', 'CREATE TABLE vectors(input_sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, '
                                                'dimensions INTEGER NOT NULL, vector BLOB NOT NULL)')):
        other = tmp_path / name
        with contextlib.closing(sqlite3.connect(other)) as connection, connection: connection.execute(create)
        with pytest.raises(cli.Refused, match='is not a vector cache of this contract'): export(export_db(), other)
        with contextlib.closing(sqlite3.connect(other)) as connection:
            assert [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ['vectors']
            assert connection.execute('SELECT COUNT(*) FROM vectors').fetchone() == (0,)


# --------------------------------------------------------------------------------------
# freeze-legacy

LEGACY_ROW = {'generation_id': L, 'kind': rg.LEGACY_KIND, 'model': rg.LEGACY_MODEL, 'status': 'complete', 'eaggl_import_id': 'a' * 64,
              'eaggl_embedding_run_id': cli.EAGGL_RUN, 'dismech_import_id': '4' * 64, 'legacy_mapping_run_id': cli.MAPPING_RUN,
              'legacy_gene_set_import_id': 'd' * 64, 'manifest': {'format': 'reveal.reference-generation/1', 'legacy_mapping_run_id': cli.MAPPING_RUN},
              'cold_export_ref': None}


def snapshot_row(source, label='Insulin secretion'):
    trait, factor = source.split(':')[2], source.split(':')[4]
    return archive._archived_row({'format': archive.SNAPSHOT_FORMAT, 'generation_id': L, 'model': rg.LEGACY_MODEL, 'source_id': source,
        'factor_id': f'{trait}::{factor}', 'trait': trait, 'kpn_trait_id': None, 'label': label,
        'mechanism': {'id': None, 'name': f'{trait} mechanism {factor}', 'description': f'EAGGL mechanism {source}. Source label: {label}.'},
        'metadata': {}, 'top_genes': [], 'top_gene_sets': [], 'generation_manifest_sha256': digest(LEGACY_ROW['manifest'])})


def freeze_db(stored, *, sources=SOURCES, generations=(LEGACY_ROW,)):
    def present(sql, params): return [(identity, stored[identity]) for identity in params if identity in stored]
    return FakeDB(generations=generations, rules=[
        (r'^SELECT cfde_node_id FROM eaggl_cfde_factor_links WHERE run_id=%s', lambda sql, params: [(s,) for s in sources] if params == (cli.MAPPING_RUN,) else []),
        (r'^SELECT archive_id,snapshot_sha256 FROM archived_reference_factors WHERE archive_id IN', present)])


@pytest.fixture
def capture(monkeypatch):
    calls = []
    def fake(connection, generation, source_ids, *, runtime=None, strict=False):
        calls.append({'generation': generation, 'source_ids': list(source_ids), 'runtime': runtime, 'strict': strict})
        return [snapshot_row(source) for source in sorted(source_ids)]
    monkeypatch.setattr(archive, 'capture_factors', fake)
    return calls


def test_freeze_legacy_captures_every_legacy_factor_and_skips_archived_rows(capture):
    runtime = object()
    present = snapshot_row(SOURCES[0])
    db = freeze_db({present['archive_id']: present['snapshot_sha256']})
    dry = cli.freeze_legacy(db, apply=False, runtime=runtime)
    assert read_only_first(db) and not writes(db) and db.rollbacks == 1
    assert capture == [{'generation': LEGACY_ROW, 'source_ids': SOURCES, 'runtime': runtime, 'strict': True}]
    assert {key: dry[key] for key in ('legacy_factors', 'captured', 'already_archived', 'would_write')} == {
        'legacy_factors': 3, 'captured': 3, 'already_archived': 1, 'would_write': 2}
    assert dry['stored_snapshot_differs'] == {'count': 0, 'archive_ids': []} and 'written' not in dry

    db = freeze_db({present['archive_id']: present['snapshot_sha256']})
    result = cli.freeze_legacy(db, apply=True, runtime=runtime)
    assert db.log[0][0] != 'SET SESSION TRANSACTION READ ONLY' and capture[-1]['source_ids'] == SOURCES
    (sql, inserted), = [(sql, rows) for sql, rows in db.log if sql.startswith('INSERT INTO archived_reference_factors')]
    assert sorted(row[0] for row in inserted) == sorted(rg.archive_id(L, source) for source in SOURCES[1:])
    assert (result['written'], result['already_archived'], db.commits) == (2, 1, 1)


def test_freeze_legacy_never_rewrites_a_stored_snapshot(capture):
    stored = {snapshot_row(source)['archive_id']: snapshot_row(source)['snapshot_sha256'] for source in SOURCES}
    stored[rg.archive_id(L, SOURCES[2])] = '0' * 64  # captured earlier, e.g. with another runtime
    db = freeze_db(stored)
    result = cli.freeze_legacy(db, apply=True, runtime=None)
    assert not writes(db) and result['written'] == 0 and result['already_archived'] == 3
    assert result['stored_snapshot_differs'] == {'count': 1, 'archive_ids': [rg.archive_id(L, SOURCES[2])]}


def test_freeze_legacy_refuses_anything_but_the_legacy_generation(capture):
    for generations in ((), (dict(LEGACY_ROW, kind=rg.KPN_KIND),), (dict(LEGACY_ROW, legacy_mapping_run_id='e' * 64),)):
        with pytest.raises(cli.Refused, match='not the registered legacy generation'):
            cli.freeze_legacy(freeze_db({}, generations=generations), apply=True, runtime=None)
    with pytest.raises(cli.Refused, match='not legacy factor ids'):
        cli.freeze_legacy(freeze_db({}, sources=SOURCES + [rg.public_id('KPN.TRAIT:0000398', 'Factor1')]), apply=True, runtime=None)
    with pytest.raises(cli.Refused, match='no factor links'):
        cli.freeze_legacy(freeze_db({}, sources=[]), apply=True, runtime=None)
    assert capture == []


# --------------------------------------------------------------------------------------
# archive-prod

BINDING = {'eaggl_factor_id': 'T2D::Factor1', 'eaggl_import_id': 'a' * 64, 'embedding_run_id': cli.EAGGL_RUN, 'mapping_run_id': cli.MAPPING_RUN,
           'gene_set_import_id': 'd' * 64, 'cfde_node_id': SOURCES[0], 'cfde_payload': {'label': 'Insulin secretion'}}


@pytest.fixture
def prod(tmp_path):
    repo = Repository(str(tmp_path / 'app.sqlite'), table_prefix=cli.PROD_PREFIX); repo.migrate()
    with repo.transaction() as tx:  # a legacy-mode analysis request: its binding names only the mapping run
        tx.put('request', 'request-1', 'user-1', {'id': 'request-1', 'owner_user_id': 'user-1', 'question_id': 'dapper:KnowledgeGap.' + '3' * 32,
                                                   'submitted_at': '2026-09-02T00:00:00Z'})
        tx.put('request_binding', 'request-1', 'user-1', {'anchors': [BINDING],
               'anchor_display': {SOURCES[0]: {'label': 'Insulin secretion', 'subtitle': 'T2D (Factor1)'}}})
    return repo


def all_rows(repo):
    with repo.read_transaction() as tx:
        cursor = tx.execute('SELECT * FROM reveal_records ORDER BY kind,id')
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


@pytest.fixture
def archive_calls(monkeypatch):
    calls, real = [], archive.archive_prefix
    def recording(repo, from_generation, to_generation, **options):
        calls.append((repo.table_prefix, from_generation, to_generation, options))
        return real(repo, from_generation, to_generation, **options)
    monkeypatch.setattr(archive, 'archive_prefix', recording)
    return calls


def test_archive_prod_backs_up_first_then_points_at_the_kpn_generation_and_archives(prod, archive_calls, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'BACKUP_PAGE_ROWS', 2)  # several keyset pages
    before, backups = all_rows(prod), tmp_path / 'backups'
    real_write = rg.write_active
    def write_active(tx, *args, **options):
        # The backup is complete and private before the first write.
        (path,) = backups.iterdir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(gzip.decompress(path.read_bytes()).splitlines()) == len(before)
        assert not archive_calls
        return real_write(tx, *args, **options)
    monkeypatch.setattr(rg, 'write_active', write_active)
    result = cli.archive_prod(prod, apply=True, backup_dir=backups)
    (path,) = backups.iterdir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.name.startswith('reveal_records.') and path.name.endswith('.jsonl.gz')
    backup = [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines()]
    assert backup == before and not any(row['kind'] == rg.ACTIVE_KIND for row in backup)
    assert result['backup'] == {'path': str(path.resolve()), 'table': 'reveal_records', 'rows': len(before),
                                'sha256': sha(path.read_bytes()), 'bytes': path.stat().st_size}
    with prod.read_transaction() as tx:
        active, request = rg.read_active(tx), tx.get('request', 'request-1')['data']
    assert (active['generation_id'], active['model'], active['previous_generation_id']) == (G, rg.KPN_MODEL, None)
    assert archive_calls == [('reveal', L, G, {'apply': True})]  # the old apply's call: legacy mode is auto-detected
    assert result['archive']['counts'] == {'request': 1} and result['archive']['legacy_fallback'] is True
    stamp = request['archive']
    assert (stamp['from_reference_generation'], stamp['to_reference_generation']) == (L, G)
    assert stamp['reference']['anchors'][0]['archived_reference_factor_id'] == rg.archive_id(L, SOURCES[0])
    # A re-run (the prefix is already active on G) takes another backup and archives nothing new.
    monkeypatch.setattr(rg, 'write_active', real_write)
    again = cli.archive_prod(prod, apply=True, backup_dir=backups)
    assert len(list(backups.iterdir())) == 2 and again['archive']['counts'] == {} and again['archive']['already_archived'] == {'request': 1}


def test_archive_prod_refuses_a_prefix_active_on_another_generation(prod, archive_calls, tmp_path):
    with prod.transaction() as tx: rg.write_active(tx, 'f' * 64, rg.KPN_MODEL, expected_previous=None)
    before = all_rows(prod)
    for apply in (True, False):
        with pytest.raises(cli.Refused, match='only for a prefix that was never cut over'):
            cli.archive_prod(prod, apply=apply, backup_dir=tmp_path / 'backups')
    assert not (tmp_path / 'backups').exists() and not archive_calls and all_rows(prod) == before


def test_archive_prod_dry_run_writes_nothing(prod, archive_calls, tmp_path):
    before = all_rows(prod)
    result = cli.archive_prod(prod, apply=False)
    assert archive_calls == [('reveal', L, G, {'apply': False})] and result['reference_active'] is None and 'backup' not in result
    assert result['archive']['counts'] == {'request': 1} and all_rows(prod) == before
    with pytest.raises(cli.Refused, match='--backup-dir'): cli.archive_prod(prod, apply=True)
    assert all_rows(prod) == before


# --------------------------------------------------------------------------------------
# cleanup

PROTECTED = {'archived_reference_factors', 'dismech_imports', 'dismech_documents', 'dismech_mechanisms', 'dismech_gap_attachments',
             'reveal_records', 'reveal_transaction_lock', 'reveal_workflow_qa_records', 'reveal_workflow_qa_transaction_lock',
             'reveal_workflow_local_records', 'reveal_workflow_local_transaction_lock',
             'reveal_ref_factors', 'reveal_workflow_qa_ref_factors', 'reveal_workflow_local_ref_factors'}
RETIRED = ['reveal_compose_records', 'reveal_compose_transaction_lock', 'reveal_reload_rehearsal_records',
           'reveal_reload_rehearsal_transaction_lock']
OLD_NAMESPACES = ['compose-cfde-collection-' + 'e' * 24, 'local-c-' + 'a' * 48, 'local-f-' + 'a' * 48, 'prod-eaggl-factor-' + 'b' * 24,
                  'qa-dismech-context-' + 'c' * 24, 'rehearsal-cfde-geneset-' + 'd' * 24]
RELEASE_NAMESPACES = [f'{environment}-{kind}' for environment in ('local', 'qa', 'prod') for kind in cli.RELEASE_NAMESPACE_KINDS]
OTHER_NAMESPACES = ['', 'scratch', 'staging-f-' + 'f' * 48]
RECORDS = {'reveal_records': 3, 'reveal_workflow_qa_records': 2, 'reveal_workflow_local_records': 0, 'reveal_compose_records': 1}


class Upstash:
    def __init__(self, names, log): self.names, self.log = list(names), log
    def list_namespaces(self): return list(self.names)
    def delete_namespace(self, name): self.log.append(('delete_namespace', name)); self.names.remove(name)


def cleanup_env(*, tables=(), drop_tables=(), namespaces=OLD_NAMESPACES + RELEASE_NAMESPACES + OTHER_NAMESPACES, frozen=SOURCES):
    tables = (set(cli.SHARED_TABLES) | PROTECTED | set(RETIRED) | set(tables)) - set(drop_tables)
    remaining = dict(RECORDS)
    def counts(sql, params):
        total = remaining[re.search(r'FROM `(\w+)`', sql)[1]]
        return ([('reference_active', 1)] if total else []) + ([('vector_snapshot', total - 1)] if total > 1 else [])
    def delete(sql, params):
        table = re.search(r'FROM `(\w+)`', sql)[1]
        count = min(remaining[table], cli.DELETE_BATCH); remaining[table] -= count
        return count
    log = []
    db = FakeDB(log=log, rules=[
        (r'^SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE\(\)', lambda sql, params: [(t,) for t in sorted(tables)]),
        (r'^SELECT kind,COUNT\(\*\) FROM `\w+` WHERE kind IN', counts),
        (r'^SELECT cfde_node_id FROM eaggl_cfde_factor_links WHERE run_id=%s', [(source,) for source in SOURCES]),
        (r'^SELECT archive_id FROM archived_reference_factors WHERE generation_id=%s', [(rg.archive_id(L, source),) for source in frozen]),
        (r'^DELETE FROM', delete), (r'^DROP TABLE', 0)])
    return db, Upstash(namespaces, log)


def destructive(log):
    return [entry for entry in log if entry[0] == 'delete_namespace' or re.match(r'(DELETE|DROP|INSERT|UPDATE|ALTER|CREATE)', entry[0])]


def test_cleanup_dry_run_lists_exactly_what_would_go_without_executing(monkeypatch):
    db, client = cleanup_env()
    result = cli.cleanup(db, client, apply=False)
    assert read_only_first(db) and not destructive(db.log) and db.rollbacks == 1 and db.commits == 0
    assert result['namespaces'] == {'delete': OLD_NAMESPACES, 'keep': sorted(RELEASE_NAMESPACES + OTHER_NAMESPACES)}
    assert result['records'] == {'reveal_records': {'reference_active': 1, 'vector_snapshot': 2},
                                 'reveal_workflow_qa_records': {'reference_active': 1, 'vector_snapshot': 1},
                                 'reveal_workflow_local_records': {}, 'reveal_compose_records': {'reference_active': 1}}
    assert result['tables'] == list(cli.SHARED_TABLES) + RETIRED and result['record_kinds'] == list(cli.RECORD_KINDS)
    assert result['statements'][0] == ('DELETE FROM `reveal_records` WHERE kind IN (' + ','.join(['%s'] * 13) + ') LIMIT 10000')
    assert result['statements'][4:] == [f'DROP TABLE `{table}`' for table in list(cli.SHARED_TABLES) + RETIRED]
    assert result['blockers'] == [] and result['readiness']['legacy_factors'] == {'expected': 3, 'missing': 0}
    assert result['readiness']['environments']['prod'] == {'prefix': 'reveal', 'release_tables': ['reveal_ref_factors'],
                                                           'namespaces': {f'prod-{kind}': True for kind in cli.RELEASE_NAMESPACE_KINDS}}


def test_cleanup_apply_runs_namespaces_then_records_then_tables_and_spares_protected(monkeypatch):
    monkeypatch.setattr(cli, 'DELETE_BATCH', 2)
    db, client = cleanup_env()
    result = cli.cleanup(db, client, apply=True)
    steps = destructive(db.log)
    kinds = ['namespace' if entry[0] == 'delete_namespace' else entry[0].split()[0].lower() for entry in steps]
    assert kinds == ['namespace'] * 6 + ['delete'] * 6 + ['drop'] * (len(cli.SHARED_TABLES) + len(RETIRED))
    assert [entry[1] for entry in steps[:6]] == OLD_NAMESPACES and sorted(client.names) == sorted(RELEASE_NAMESPACES + OTHER_NAMESPACES)
    deletes = [(re.search(r'`(\w+)`', sql)[1], params) for sql, params in steps[6:12]]
    # Batches until one deletes fewer than DELETE_BATCH rows: 3 -> 2+1, 2 -> 2+0, 0 -> 0, 1 -> 1.
    assert [table for table, _ in deletes] == ['reveal_records'] * 2 + ['reveal_workflow_qa_records'] * 2 + ['reveal_workflow_local_records', 'reveal_compose_records']
    assert all(params == cli.RECORD_KINDS for _, params in deletes)
    assert all(sql.endswith('LIMIT 2') and 'WHERE kind IN' in sql for sql, _ in steps[6:12])
    dropped = [re.fullmatch(r'DROP TABLE `(\w+)`', sql)[1] for sql, _ in steps[12:]]
    assert dropped == list(cli.SHARED_TABLES) + RETIRED and not set(dropped) & PROTECTED
    assert result['records_deleted'] == RECORDS and result['tables_dropped'] == dropped and result['namespaces_deleted'] == OLD_NAMESPACES
    assert db.commits == 1 + 6  # after planning, then per delete batch


def test_cleanup_refuses_until_every_environment_published_and_legacy_factors_are_frozen():
    cases = [(dict(namespaces=OLD_NAMESPACES + [name for name in RELEASE_NAMESPACES if name != 'prod-factors']), 'prod has no published release'),
             (dict(namespaces=OLD_NAMESPACES + [name for name in RELEASE_NAMESPACES if name != 'local-contexts']), 'local has no published release'),
             (dict(drop_tables=['reveal_workflow_qa_ref_factors']), 'qa has no published release'),
             (dict(frozen=SOURCES[:2]), '1 legacy factors are not frozen')]
    for options, message in cases:
        db, client = cleanup_env(**options)
        assert any(message in blocker for blocker in cli.cleanup(db, client, apply=False)['blockers'])
        db, client = cleanup_env(**options)
        with pytest.raises(cli.Refused, match=message): cli.cleanup(db, client, apply=True)
        assert not destructive(db.log)
    # Serving reads only <env>-factors and <env>-contexts: other release namespaces are reported, never required.
    db, client = cleanup_env(namespaces=OLD_NAMESPACES + [name for name in RELEASE_NAMESPACES if name != 'qa-collections'])
    plan = cli.cleanup(db, client, apply=False)
    assert plan['blockers'] == [] and plan['readiness']['environments']['qa']['namespaces']['qa-collections'] is False
    # Once eaggl_cfde_factor_links is gone (a resumed cleanup) the freeze can no longer be checked or lost.
    db, client = cleanup_env(drop_tables=['eaggl_cfde_factor_links'], frozen=[])
    assert cli.cleanup(db, client, apply=False)['readiness']['legacy_factors'] == {'skipped': 'eaggl_cfde_factor_links is gone'}


def test_cleanup_refuses_a_plan_that_would_touch_a_protected_table():
    db, client = cleanup_env(tables=['reveal_compose_ref_factors'])  # a release table under a retired prefix
    with pytest.raises(cli.Refused, match='Refusing to drop protected table reveal_compose_ref_factors'):
        cli.cleanup(db, client, apply=True)
    assert not destructive(db.log)


def test_guards_protect_tables_and_release_namespaces():
    for table in PROTECTED | {'reveal_compose_ref_factors', 'dismech_hypotheses', 'unrelated_table'}:
        with pytest.raises(cli.Refused): cli.guard('drop', table)
    for table in PROTECTED - {'reveal_records', 'reveal_workflow_qa_records', 'reveal_workflow_local_records'} | {'reference_vectors'}:
        with pytest.raises(cli.Refused): cli.guard('delete', table)
    for table in ('reveal_records', 'reveal_workflow_qa_records', 'reveal_workflow_local_records', 'reveal_compose_records'):
        cli.guard('delete', table)
    for table in list(cli.SHARED_TABLES) + RETIRED: cli.guard('drop', table)
    with pytest.raises(cli.Refused, match='Invalid table'): cli.guard('drop', 'reference_vectors`; DROP TABLE x')
    for name in RELEASE_NAMESPACES + OTHER_NAMESPACES + ['dev-factors', 'local-factors-x']:
        with pytest.raises(cli.Refused): cli.guard_namespace(name)
    for name in OLD_NAMESPACES: cli.guard_namespace(name)


# --------------------------------------------------------------------------------------
# CLI


def test_arguments_require_backup_dir_for_apply_and_valid_run_ids():
    for argv in (['archive-prod', '--apply'], ['export-vectors'], ['export-vectors', '--cache', 'x', '--eaggl-run', 'abc'],
                 ['export-vectors', '--cache', 'x', '--dismech-run', 'D' * 64], ['cleanup', '--force']):
        with pytest.raises(SystemExit) as failure, contextlib.redirect_stderr(io.StringIO()): cli.arguments(argv)
        assert failure.value.code == 2
    args = cli.arguments(['export-vectors', '--cache', 'x'])
    assert (args.eaggl_run, args.dismech_run) == (cli.EAGGL_RUN, cli.DISMECH_RUN)
    assert cli.arguments(['archive-prod', '--apply', '--backup-dir', 'b']).backup_dir == Path('b')
    assert cli.arguments(['cleanup']).apply is False and cli.arguments(['freeze-legacy']).apply is False


def run_main(monkeypatch, argv):
    monkeypatch.setattr(cli, 'load_dotenv', lambda *args, **kwargs: None)  # never read the repository .env
    out = io.StringIO()
    with contextlib.redirect_stdout(out): code = cli.main(argv)
    return code, json.loads(out.getvalue())


def test_main_prints_one_json_result_and_refusals_exit_2(monkeypatch, capture):
    db = freeze_db({}, generations=())
    monkeypatch.setattr(cli, 'connect', lambda: db)
    monkeypatch.setattr(cli, 'dapper_runtime', lambda: None)
    code, result = run_main(monkeypatch, ['freeze-legacy', '--apply'])
    assert code == 2 and result['command'] == 'freeze-legacy' and result['ok'] is False and 'legacy generation' in result['refused']
    assert db.closed


def test_main_archive_prod_pins_the_process_to_prod(monkeypatch, prod):
    for key in ('REVEAL_APPLICATION_TABLE_PREFIX', 'REVEAL_NOTIFICATION_NAMESPACE'): monkeypatch.setenv(key, 'reveal-local')
    monkeypatch.setattr(cli, 'repository', lambda prefix: prod if prefix == 'reveal' else pytest.fail(prefix))
    monkeypatch.setattr(cli, 'connect', lambda: pytest.fail('archive-prod uses only the prod repository'))
    code, result = run_main(monkeypatch, ['archive-prod'])
    assert code == 0 and result['command'] == 'archive-prod' and result['apply'] is False and result['archive']['counts'] == {'request': 1}
    assert (os.environ['REVEAL_APPLICATION_TABLE_PREFIX'], os.environ['REVEAL_NOTIFICATION_NAMESPACE']) == ('reveal', 'reveal-prod')
