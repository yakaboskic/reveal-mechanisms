"""Explicit, resumable DisMech embeddings in the pinned EAGGL vector space.

Automatic suggestions consume this immutable import. Only this explicit offline
embedding step and novel user text contact the embedding service.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile
from uuid import uuid4

import numpy as np

from .dismech_import import canonical, digest, require
from .eaggl_embeddings import decode_vector, validate_vectors, vector_hash
from .embedding_client import get_embeddings
from .mysql_database import insert_batch

MIGRATION = Path(__file__).resolve().parents[4] / 'schema/migrations/006_dismech_embeddings.sql'
VERSION = 'dismech-embeddings-v1'
TEMPLATES = {'mechanism': 'dismech-description-v1', 'knowledge_gap': 'dismech-gap-text-v1'}
CALIBRATION_MIN_COSINE = 0.999999
GENERATION_FORMAT = 'reveal.embedding-generation/1'


def context_input(source_id, source_kind, source_revision, text):
    require(source_kind in TEMPLATES and isinstance(text, str) and bool(text.strip()), 'Invalid source embedding text')
    return {'source_id': source_id, 'source_kind': source_kind, 'source_revision': source_revision,
            'template': TEMPLATES[source_kind], 'input_sha256': digest(text), 'input_text': text}


def source_inputs(export):
    mechanisms = {row['id']: row for row in export.rows['mechanisms']}
    linked = {row['gap_id'] for row in export.rows['gap_attachments'] if row.get('target_id') in mechanisms}
    rows = [context_input(row['id'], 'mechanism', export.source_files['source'][row['source_file']],
                          row.get('description') or row['name']) for row in mechanisms.values()]
    rows.extend(context_input(row['id'], 'knowledge_gap', export.source_files['gaps'][row['source_file']],
                              row['raw']['prompt']) for row in export.rows['knowledge_gaps'] if row['id'] not in linked)
    return sorted(rows, key=lambda row: row['source_id'])


def validate_target(target):
    config = target['config']
    require(digest(canonical(config)) == target['run_id'], 'EAGGL embedding configuration/hash mismatch')
    require(config.get('template') == 'factor-label-v1' and config.get('dtype') == 'float32-le'
            and config.get('normalization') == 'none' and config.get('metric') == 'cosine', 'Unsupported target embedding space')
    require(all(config.get(key) for key in ('model', 'model_revision', 'provider', 'service_url', 'import_id')),
            'Incomplete target model configuration')
    require(type(target['dimensions']) is int and target['dimensions'] > 0, 'Invalid target dimensions')
    probes = target.get('calibration')
    require(isinstance(probes, list) and bool(probes), 'Target metadata requires stored EAGGL calibration vectors; use read_target_run')
    for probe in probes:
        require(digest(probe['input_text']) == probe['input_sha256'], 'Calibration input hash mismatch')
        vector = validate_vectors([probe['vector']], 1, target['dimensions'])[0]
        require(vector_hash(vector.tobytes()) == probe['vector_sha256'], 'Calibration vector hash mismatch')


def read_target_run(connection, run_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT config,dimensions FROM eaggl_embedding_runs WHERE run_id=%s AND status='complete'", (run_id,))
        row = cursor.fetchone()
        require(row is not None, 'Select a completed EAGGL embedding run')
        target = {'run_id': run_id, 'config': json.loads(row[0]), 'dimensions': row[1]}
        cursor.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM eaggl_name_embeddings WHERE run_id=%s ORDER BY input_sha256 LIMIT 3', (run_id,))
        target['calibration'] = [{'input_sha256': sha, 'input_text': text, 'vector': decode_vector(blob, checksum, row[1]).tolist(),
                                  'vector_sha256': checksum} for sha, text, blob, checksum in cursor.fetchall()]
    validate_target(target)
    return target


def prepare_capture(output, export, target_run):
    validate_target(target_run)
    target = {key: deepcopy(target_run[key]) for key in ('run_id', 'config', 'dimensions', 'calibration')}
    inputs = source_inputs(export)
    raw = ''.join(canonical(row) + '\n' for row in inputs).encode('utf-8')
    config = {'version': VERSION, 'dismech_import_id': export.import_id, 'eaggl_embedding_run_id': target['run_id'],
              'eaggl_config': target['config'], 'templates': TEMPLATES, 'dimensions': target['dimensions'],
              'input_inventory_sha256': digest(raw), 'calibration_vectors_sha256': digest(canonical(target['calibration']))}
    manifest = {'run_id': digest(canonical(config)), 'config': config, 'dimensions': target['dimensions'],
                'expected_bindings': len(inputs), 'expected_vectors': len({row['input_sha256'] for row in inputs}), 'target_run': target}
    output = Path(output)
    if output.exists():
        existing, _ = open_capture(output)
        require(existing == manifest, 'Existing DisMech embedding capture conflicts with requested source/model')
        return existing
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.dismech-embeddings-', dir=output.parent))
    try:
        (temporary / 'inputs.jsonl').write_bytes(raw)
        (temporary / 'manifest.json').write_text(canonical(manifest) + '\n')
        temporary.rename(output)
    finally:
        if temporary.exists(): shutil.rmtree(temporary)
    return manifest


def open_capture(output):
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_bytes())
    config = manifest['config']
    validate_target(manifest['target_run'])
    require(config.get('version') == VERSION and config.get('templates') == TEMPLATES, 'Unsupported DisMech text templates')
    require(digest(canonical(config)) == manifest['run_id'], 'DisMech embedding run identity mismatch')
    target = manifest['target_run']
    require(config['eaggl_embedding_run_id'] == target['run_id'] and config['eaggl_config'] == target['config']
            and config['dimensions'] == target['dimensions'] == manifest['dimensions'], 'Target space differs from capture')
    require(config['calibration_vectors_sha256'] == digest(canonical(target['calibration'])), 'Calibration manifest mismatch')
    raw = (output / 'inputs.jsonl').read_bytes()
    require(digest(raw) == config['input_inventory_sha256'], 'Source input inventory hash mismatch')
    inputs = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
    require(len(inputs) == manifest['expected_bindings'] and len({row['source_id'] for row in inputs}) == len(inputs), 'Source binding coverage mismatch')
    require(inputs == sorted(inputs, key=lambda row: row['source_id']), 'Source bindings must be sorted')
    for row in inputs:
        require(row == context_input(row['source_id'], row['source_kind'], row['source_revision'], row['input_text']), 'Source input projection mismatch')
        require(len(row['source_revision']) == 64 and all(c in '0123456789abcdef' for c in row['source_revision']), 'Invalid source revision')
    require(len({row['input_sha256'] for row in inputs}) == manifest['expected_vectors'], 'Unique text coverage mismatch')
    return manifest, inputs


def _cache(output):
    connection = sqlite3.connect(Path(output) / 'embeddings.sqlite3')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS vectors (input_sha256 TEXT PRIMARY KEY,input_text TEXT NOT NULL,vector BLOB NOT NULL,vector_sha256 TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS generation_sessions (session_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,metadata TEXT NOT NULL,metadata_sha256 TEXT NOT NULL,started_at TEXT,finished_at TEXT,recovered_at TEXT,status TEXT NOT NULL,before_calibration TEXT,after_calibration TEXT,error_type TEXT)')
    connection.execute('CREATE TABLE IF NOT EXISTS generation_vectors (input_sha256 TEXT PRIMARY KEY,vector_sha256 TEXT NOT NULL,session_id TEXT NOT NULL,FOREIGN KEY(input_sha256) REFERENCES vectors(input_sha256),FOREIGN KEY(session_id) REFERENCES generation_sessions(session_id))')
    return connection


def _time():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _generation_metadata(value):
    require(isinstance(value, dict) and isinstance(value.get('backend'), str) and bool(value['backend'].strip()),
            'Generation metadata requires an explicit backend')
    require(len(canonical(value).encode('utf-8')) <= 65536, 'Generation metadata is too large')
    return deepcopy(value)


def _generation_evidence(connection, manifest, rows, complete=False):
    """Verify compact session inventories against exact persisted vector bytes."""
    assignments = connection.execute('SELECT input_sha256,vector_sha256,session_id FROM generation_vectors ORDER BY input_sha256').fetchall()
    expected = {row[0]: row[3] for row in rows}
    require(len(assignments) == len(expected) and {row[0]: row[1] for row in assignments} == expected,
            'Generation session assignments do not cover the exact persisted vectors')
    grouped = {}
    for sha, vector_sha, session_id in assignments:
        grouped.setdefault(session_id, []).append([sha, vector_sha])
    sessions = []
    for row in connection.execute('SELECT session_id,run_id,metadata,metadata_sha256,started_at,finished_at,recovered_at,status,before_calibration,after_calibration,error_type FROM generation_sessions ORDER BY session_id'):
        identity, run_id, metadata, metadata_sha, started, finished, recovered, status, before, after, error_type = row
        require(run_id == manifest['run_id'] and digest(metadata) == metadata_sha, 'Generation session run/metadata checksum mismatch')
        metadata = _generation_metadata(json.loads(metadata))
        before, after = json.loads(before) if before else None, json.loads(after) if after else None
        inventory = grouped.pop(identity, [])
        require(status in ('running', 'generating', 'complete', 'partial', 'interrupted', 'failed', 'quarantined', 'legacy'), 'Invalid generation session status')
        if complete:
            require(status not in ('running', 'generating', 'quarantined'), 'Generation session is unfinished or quarantined')
        if status == 'legacy':
            require(metadata['backend'] == 'legacy_unrecorded' and before is None and after is None,
                    'Legacy session must not invent original calibration evidence')
        else:
            if inventory: require(before is not None, 'Generated vectors lack session calibration')
            for calibration in (before, after):
                if calibration is not None:
                    validate_calibration_evidence({'before': calibration, 'after': calibration}, manifest['target_run'])
            if status in ('complete', 'partial'):
                require(before is not None and after is not None, 'Finished generation session lacks calibration evidence')
        sessions.append({'session_id': identity, 'metadata': metadata, 'metadata_sha256': metadata_sha,
            'started_at': started, 'finished_at': finished, 'recovered_at': recovered, 'status': status,
            'before_calibration': before, 'after_calibration': after, 'error_type': error_type,
            'vector_count': len(inventory), 'vector_inventory_sha256': digest(canonical(inventory))})
    require(not grouped, 'Vector assignment references an unknown generation session')
    return {'format': GENERATION_FORMAT, 'target_eaggl_embedding_run_id': manifest['target_run']['run_id'],
            'vector_count': len(assignments), 'assignment_sha256': digest(canonical(assignments)), 'sessions': sessions}


def _start_generation(connection, manifest, rows, metadata, legacy_attestation, *, start=True):
    marker = connection.execute("SELECT value FROM metadata WHERE key='generation_format'").fetchone()
    with connection:
        if marker is None:
            require(connection.execute('SELECT COUNT(*) FROM generation_sessions').fetchone()[0] == 0
                    and connection.execute('SELECT COUNT(*) FROM generation_vectors').fetchone()[0] == 0,
                    'Unmarked generation evidence cannot be adopted')
            if rows:
                legacy = {'backend': 'legacy_unrecorded', 'limitation': 'Original execution session and per-session calibration were not recorded.',
                          'retrospective_attestation': legacy_attestation}
                raw = canonical(legacy); identity = str(uuid4())
                connection.execute('INSERT INTO generation_sessions(session_id,run_id,metadata,metadata_sha256,recovered_at,status) VALUES (?,?,?,?,?,?)',
                    (identity, manifest['run_id'], raw, digest(raw), _time(), 'legacy'))
                connection.executemany('INSERT INTO generation_vectors VALUES (?,?,?)', [(row[0], row[3], identity) for row in rows])
            connection.execute('INSERT INTO metadata VALUES (?,?)', ('generation_format', GENERATION_FORMAT))
        else:
            require(marker[0] == GENERATION_FORMAT, 'Unsupported generation evidence format')
        # The capture CLI holds an exclusive file lock. Open sessions here are
        # prior processes that exited without recording their final outcome.
        connection.execute("UPDATE generation_sessions SET status='interrupted',recovered_at=?,error_type='UncleanInterruption' WHERE status IN ('running','generating')", (_time(),))
        if not start: return None
        identity = str(uuid4()); raw = canonical(metadata)
        connection.execute('INSERT INTO generation_sessions(session_id,run_id,metadata,metadata_sha256,started_at,status) VALUES (?,?,?,?,?,?)',
            (identity, manifest['run_id'], raw, digest(raw), _time(), 'running'))
    return identity


def read_local_vectors(output, require_complete=True):
    manifest, inputs = open_capture(output)
    path = Path(output) / 'embeddings.sqlite3'
    require(path.exists(), 'Embedding cache is not prepared; run embed explicitly')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        metadata = dict(connection.execute('SELECT key,value FROM metadata'))
        require(metadata.get('run_id') == manifest['run_id'], 'Local embedding cache run mismatch')
        rows = connection.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM vectors ORDER BY input_sha256').fetchall()
        texts = {row['input_sha256']: row['input_text'] for row in inputs}
        for sha, text, blob, checksum in rows:
            require(texts.get(sha) == text and digest(text) == sha, 'Embedding input text/hash mismatch')
            decode_vector(blob, checksum, manifest['dimensions'])
        generation = None
        if 'generation_format' in metadata:
            require(metadata['generation_format'] == GENERATION_FORMAT, 'Unsupported generation evidence format')
            generation = _generation_evidence(connection, manifest, rows, complete=require_complete)
        if require_complete:
            require(metadata.get('status') == 'complete' and len(rows) == len(texts), 'Embedding run is not complete')
            require('calibration' in metadata, 'Completed run lacks calibration evidence')
            calibration = json.loads(metadata['calibration'])
            validate_calibration_evidence(calibration, manifest['target_run'], expected_vectors=manifest['expected_vectors'])
            require(calibration.get('generation') == generation, 'Completed generation evidence differs from local session inventory')
        return manifest, inputs, rows
    finally:
        connection.close()


def validate_calibration_evidence(report, target, *, expected_vectors=None):
    require(isinstance(report, dict), 'Invalid calibration evidence')
    for phase in ('before', 'after'):
        result = report.get(phase)
        require(isinstance(result, dict) and result.get('eaggl_embedding_run_id') == target['run_id']
                and result.get('minimum_cosine') == CALIBRATION_MIN_COSINE, 'Calibration evidence does not match target run/threshold')
        probes = result.get('probes')
        require(isinstance(probes, list) and len(probes) == len(target['calibration']), 'Calibration evidence probe coverage differs')
        for actual, expected in zip(probes, target['calibration']):
            require(isinstance(actual, dict) and actual.get('input_sha256') == expected['input_sha256']
                    and actual.get('stored_vector_sha256') == expected['vector_sha256'], 'Calibration evidence probe identity differs')
            score = actual.get('cosine_similarity')
            require(type(score) in (int, float) and np.isfinite(score) and CALIBRATION_MIN_COSINE <= score <= 1,
                    'Calibration evidence does not establish compatible vectors')
    if 'generation' in report:
        generation = report['generation']
        require(isinstance(generation, dict) and generation.get('format') == GENERATION_FORMAT
                and generation.get('target_eaggl_embedding_run_id') == target['run_id'], 'Invalid generation evidence target/format')
        sessions = generation.get('sessions')
        require(isinstance(sessions, list) and type(generation.get('vector_count')) is int
                and all(isinstance(s, dict) and type(s.get('vector_count')) is int and s['vector_count'] >= 0 for s in sessions)
                and sum(s['vector_count'] for s in sessions) == generation['vector_count'], 'Invalid generation evidence vector counts')
        require(expected_vectors is None or generation['vector_count'] == expected_vectors,
                'Generation evidence does not cover the expected vector inventory')
        def is_hash(value):
            return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
        require(is_hash(generation.get('assignment_sha256')), 'Invalid generation assignment checksum')
        identities = set()
        for session in sessions:
            identity = session.get('session_id')
            require(isinstance(identity, str) and bool(identity) and identity not in identities,
                    'Invalid or duplicate generation session identity')
            identities.add(identity)
            metadata = _generation_metadata(session.get('metadata'))
            require(session.get('metadata_sha256') == digest(canonical(metadata)), 'Generation metadata checksum mismatch')
            require(is_hash(session.get('vector_inventory_sha256')), 'Invalid generation session inventory checksum')
            status = session.get('status')
            require(status in ('complete', 'partial', 'interrupted', 'failed', 'legacy'), 'Invalid completed generation session status')
            before, after = session.get('before_calibration'), session.get('after_calibration')
            if status == 'legacy':
                require(metadata['backend'] == 'legacy_unrecorded' and before is None and after is None,
                        'Legacy session must not invent original calibration evidence')
            else:
                if session['vector_count']:
                    require(before is not None, 'Generated vectors lack session calibration')
                if status in ('complete', 'partial'):
                    require(before is not None and after is not None, 'Finished generation session lacks calibration evidence')
                for result in (before, after):
                    if result is not None:
                        validate_calibration_evidence({'before': result, 'after': result}, target)


def calibrate(target_run, *, embedder=get_embeddings, max_retries=3, timeout=120):
    validate_target(target_run)
    config, probes = target_run['config'], target_run['calibration']
    fresh = validate_vectors(embedder([p['input_text'] for p in probes], model=config['model'], provider=config['provider'],
        service_url=config['service_url'], max_workers=1, max_retries=max_retries, timeout=timeout), len(probes), target_run['dimensions']).astype(np.float64)
    stored = np.asarray([p['vector'] for p in probes], dtype=np.float64)
    similarities = np.sum(fresh * stored, axis=1) / (np.linalg.norm(fresh, axis=1) * np.linalg.norm(stored, axis=1))
    require(bool(np.all(similarities >= CALIBRATION_MIN_COSINE)), 'Embedding service no longer matches the pinned EAGGL vector space')
    return {'eaggl_embedding_run_id': target_run['run_id'], 'minimum_cosine': CALIBRATION_MIN_COSINE,
            'probes': [{'input_sha256': p['input_sha256'], 'stored_vector_sha256': p['vector_sha256'],
                        'cosine_similarity': float(np.clip(score, -1, 1))} for p, score in zip(probes, similarities)],
            'limitation': 'Sample compatibility check; the service does not expose an immutable model revision pin.'}


def embed_capture(output, *, batch_size=32, max_workers=2, max_retries=3, timeout=120, max_batches=None,
                  embedder=get_embeddings, generation_metadata=None, legacy_generation_attestation=None):
    require(type(batch_size) is int and 1 <= batch_size <= 100 and type(max_workers) is int and 1 <= max_workers <= 4,
            'Embedding batch size must be 1..100 and workers 1..4')
    require(max_batches is None or type(max_batches) is int and max_batches > 0, 'Maximum batches must be positive')
    manifest, inputs = open_capture(output)
    if generation_metadata is None:
        target = manifest['target_run']['config']
        generation_metadata = {'backend': 'remote_embedding_service' if embedder is get_embeddings else 'custom_embedder_unrecorded',
            'model': target['model'], 'declared_model_revision': target['model_revision']}
        if embedder is get_embeddings:
            generation_metadata.update(provider=target['provider'], service_url=target['service_url'])
    generation_metadata = _generation_metadata(generation_metadata)
    require(legacy_generation_attestation is None or isinstance(legacy_generation_attestation, dict)
            and len(canonical(legacy_generation_attestation).encode('utf-8')) <= 65536, 'Invalid legacy generation attestation')
    connection = _cache(output)
    session_id = None
    try:
        existing = dict(connection.execute('SELECT key,value FROM metadata'))
        require(not existing or existing.get('run_id') == manifest['run_id'], 'Local embedding run conflicts with capture')
        require(existing.get('status') != 'quarantined', 'Capture is quarantined after failed final calibration; prepare a fresh output directory after restoring model compatibility')
        if not existing:
            with connection:
                connection.executemany('INSERT INTO metadata VALUES (?,?)', [('run_id', manifest['run_id']), ('status', 'embedding')])
        _, _, rows = read_local_vectors(output, require_complete=False)
        texts = {row['input_sha256']: row['input_text'] for row in inputs}
        # Similar-length batches reduce transformer padding. Work ordering is
        # separate from immutable source/run identity and persisted primary keys.
        missing = sorted(set(texts) - {row[0] for row in rows}, key=lambda key: (len(texts[key]), key))
        if not missing and existing.get('status') == 'complete':
            read_local_vectors(output)
            if 'generation_format' not in existing and legacy_generation_attestation is not None:
                # Explicitly attesting an old completed capture needs no model
                # call and must not invent a new local generation session.
                _start_generation(connection, manifest, rows, generation_metadata, legacy_generation_attestation, start=False)
                calibration = json.loads(existing['calibration'])
                calibration['generation'] = _generation_evidence(connection, manifest, rows, complete=True)
                with connection:
                    connection.execute("UPDATE metadata SET value=? WHERE key='calibration'", (canonical(calibration),))
                read_local_vectors(output)
            return {**manifest, 'status': 'complete', 'embedded_now': 0}
        target = manifest['target_run']; config = target['config']
        session_id = _start_generation(connection, manifest, rows, generation_metadata, legacy_generation_attestation)
        before = calibrate(target, embedder=embedder, max_retries=max_retries, timeout=timeout)
        with connection:
            connection.execute("UPDATE metadata SET value='embedding' WHERE key='status'")
            connection.execute("UPDATE generation_sessions SET before_calibration=?,status='generating' WHERE session_id=?", (canonical(before), session_id))
        chunk_size = batch_size * max_workers
        embedded_now = 0
        for start in range(0, len(missing), chunk_size):
            if max_batches is not None and start // chunk_size >= max_batches: break
            keys = missing[start:start + chunk_size]
            vectors = validate_vectors(embedder([texts[key] for key in keys], model=config['model'], provider=config['provider'],
                service_url=config['service_url'], batch_size=batch_size, max_workers=max_workers,
                max_retries=max_retries, timeout=timeout), len(keys), manifest['dimensions'])
            records = [(key, texts[key], vector.tobytes(), vector_hash(vector.tobytes())) for key, vector in zip(keys, vectors)]
            with connection:
                connection.executemany('INSERT INTO vectors VALUES (?,?,?,?)', records)
                connection.executemany('INSERT INTO generation_vectors VALUES (?,?,?)', [(row[0], row[3], session_id) for row in records])
            embedded_now += len(keys)
            logging.info('Embedded DisMech texts %d/%d', len(rows) + start + len(keys), len(texts))
        try:
            after = calibrate(target, embedder=embedder, max_retries=max_retries, timeout=timeout)
        except ValueError:
            # Known model drift can invalidate every freshly generated row.
            # A later healthy deployment must not authorize these old bytes.
            with connection:
                connection.execute("UPDATE metadata SET value='quarantined' WHERE key='status'")
                connection.execute("UPDATE generation_sessions SET status='quarantined',finished_at=?,error_type='CalibrationFailure' WHERE session_id=?", (_time(), session_id))
            raise
        with connection:
            complete = embedded_now == len(missing)
            connection.execute('UPDATE generation_sessions SET status=?,finished_at=?,after_calibration=? WHERE session_id=?',
                ('complete' if complete else 'partial', _time(), canonical(after), session_id))
            all_rows = connection.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM vectors ORDER BY input_sha256').fetchall()
            calibration = {'before': before, 'after': after, 'generation': _generation_evidence(connection, manifest, all_rows, complete=complete)}
            connection.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('calibration', canonical(calibration)))
            connection.execute("UPDATE metadata SET value=? WHERE key='status'", ('complete' if complete else 'embedding',))
        read_local_vectors(output, require_complete=complete)
        return {**manifest, 'status': 'complete' if complete else 'embedding', 'embedded_now': embedded_now,
                'remaining_vectors': len(missing) - embedded_now, 'calibration': calibration}
    except BaseException as error:
        if session_id is not None:
            with connection:
                connection.execute("UPDATE generation_sessions SET status='interrupted',finished_at=?,error_type=? WHERE session_id=? AND status IN ('running','generating')",
                    (_time(), type(error).__name__, session_id))
        raise
    finally:
        connection.close()


def migrate(connection):
    sql = '\n'.join(line for line in MIGRATION.read_text().splitlines() if not line.lstrip().startswith('--'))
    with connection.cursor() as cursor:
        for statement in sql.split(';'):
            if statement.strip(): cursor.execute(statement)
    connection.commit()


def _database_inputs(connection, import_id):
    """Independent exact source projection, used only by explicit load/verify."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT status FROM dismech_imports WHERE import_id=%s', (import_id,))
        require(cursor.fetchone() == ('complete',), 'DisMech source import is not complete')
        # Aurora can choose a document-first nested loop through the secondary
        # mechanism FK index, causing scattered reads of every long text row.
        # Scan each import's clustered primary range once; join only small IDs
        # in memory and retain the same exact native source/revision checks.
        cursor.execute('SELECT id_sha256,source_sha256 FROM dismech_documents FORCE INDEX(PRIMARY) WHERE import_id=%s', (import_id,))
        documents = dict(cursor.fetchall())

        def source_revision(document):
            require(document in documents, 'Source context references a missing document revision')
            return documents[document]

        def binding(identity, kind, document, sha):
            require(isinstance(sha, str) and len(sha) == 64, 'Source context text cannot be hashed')
            return {'source_id': identity, 'source_kind': kind, 'source_revision': source_revision(document),
                    'template': TEMPLATES[kind], 'input_sha256': sha}

        # Hash the exact UTF-8 text on Aurora; no normalization or collation
        # comparison may turn whitespace into an absent description.
        cursor.execute("SELECT source_id,SHA2(CASE WHEN description IS NULL OR OCTET_LENGTH(description)=0 THEN name ELSE description END,256),document_sha256 FROM dismech_mechanisms FORCE INDEX(PRIMARY) WHERE import_id=%s", (import_id,))
        rows = [binding(identity, 'mechanism', document, sha)
                for identity, sha, document in cursor.fetchall()]
        cursor.execute('SELECT gap_sha256 FROM dismech_gap_attachments FORCE INDEX(PRIMARY) WHERE import_id=%s AND target_mechanism_sha256 IS NOT NULL', (import_id,))
        attached = {row[0] for row in cursor.fetchall()}
        cursor.execute('SELECT id_sha256,source_id,SHA2(prompt,256),document_sha256 FROM dismech_discussions FORCE INDEX(PRIMARY) WHERE import_id=%s AND is_gap=1', (import_id,))
        rows.extend(binding(identity, 'knowledge_gap', document, sha)
                    for gap, identity, sha, document in cursor.fetchall() if gap not in attached)
    return sorted(rows, key=lambda row: row['source_id'])


def _check_database_sources(connection, manifest, inputs):
    require(read_target_run(connection, manifest['config']['eaggl_embedding_run_id']) == manifest['target_run'],
            'Database EAGGL vector space/calibration differs from capture')
    expected = [{key:value for key,value in row.items() if key != 'input_text'} for row in inputs]
    require(_database_inputs(connection, manifest['config']['dismech_import_id']) == expected,
            'Database DisMech source IDs/revisions/text coverage differ from capture')


def _stages(inputs, vectors):
    yield 'vectors', 'dismech_embedding_vectors', ('input_sha256', 'input_text', 'vector', 'vector_sha256'), vectors
    bindings = sorted((digest(row['source_id']), row['source_id'], row['source_kind'], row['source_revision'], row['template'], row['input_sha256']) for row in inputs)
    yield 'bindings', 'dismech_embedding_inputs', ('source_id_sha256', 'source_id', 'source_kind', 'source_revision', 'template', 'input_sha256'), bindings


def _stage_readback(cursor, stage, table, columns, run_id):
    if stage == 'vectors':
        # Compute hashes from stored bytes independently of the checksum column.
        # Full vector downloads are reserved for actual search/preload consumers.
        projection = 'input_sha256,SHA2(input_text,256),OCTET_LENGTH(vector),SHA2(vector,256),vector_sha256'
    else:
        projection = ','.join(columns)
    cursor.execute(f'SELECT {projection} FROM {table} WHERE run_id=%s ORDER BY {columns[0]}', (run_id,))
    return list(cursor.fetchall())


def _expected_readback(stage, rows):
    if stage == 'vectors':
        return [(sha,digest(text),len(blob),digest(blob),checksum) for sha,text,blob,checksum in rows]
    return rows


def _local_calibration(output):
    connection = sqlite3.connect((Path(output) / 'embeddings.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='calibration'").fetchone()
        require(row is not None, 'Completed vectors require calibration evidence')
        return json.loads(row[0])
    finally:
        connection.close()


def _read_run(cursor, run_id):
    cursor.execute('SELECT config,dimensions,expected_bindings,expected_vectors,loaded_bindings,loaded_vectors,calibration,status FROM dismech_embedding_runs WHERE run_id=%s', (run_id,))
    return cursor.fetchone()


def _check_run(row, manifest, calibration, *, complete=False):
    require(row is not None, 'DisMech embedding run is not loaded')
    stored = json.loads(row[6])
    # MySQL's binary JSON renderer can round an IEEE-754 cosine by one ULP.
    # Validate both reports, then permit only a two-ULP presentation difference
    # in defined calibration cosine fields. All metadata/hashes remain exact.
    for report in (stored, calibration):
        validate_calibration_evidence(report, manifest['target_run'], expected_vectors=manifest['expected_vectors'])
    comparable = deepcopy(stored)

    def align_cosines(actual, expected):
        if not isinstance(actual, dict) or not isinstance(expected, dict): return
        for left, right in zip(actual.get('probes', []), expected.get('probes', [])):
            a, b = left['cosine_similarity'], right['cosine_similarity']
            if math.nextafter(math.nextafter(b,-math.inf),-math.inf) <= a <= math.nextafter(math.nextafter(b,math.inf),math.inf):
                left['cosine_similarity'] = b

    for phase in ('before','after'):
        align_cosines(comparable[phase], calibration[phase])
    for left, right in zip(comparable.get('generation', {}).get('sessions', []),
                           calibration.get('generation', {}).get('sessions', [])):
        for phase in ('before_calibration','after_calibration'):
            align_cosines(left.get(phase), right.get(phase))
    require((json.loads(row[0]), row[1], row[2], row[3], comparable) ==
            (manifest['config'], manifest['dimensions'], manifest['expected_bindings'], manifest['expected_vectors'], calibration),
            'Stored DisMech embedding run conflicts with capture')
    if complete:
        require(row[7] == 'complete' and row[4] == row[2] and row[5] == row[3], 'Stored DisMech embedding run is not complete')


def load_capture(connection, output, *, batch_size=500):
    require(type(batch_size) is int and batch_size > 0, 'Database batch size must be positive')
    manifest, inputs, vectors = read_local_vectors(output)
    _check_database_sources(connection, manifest, inputs)
    calibration = _local_calibration(output)
    identity = manifest['run_id']
    lock_name = 'dismech-embedding:' + identity[:45]
    locked = False
    failure = None
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT GET_LOCK(%s,0)', (lock_name,))
            locked = cursor.fetchone()[0] == 1
            require(locked, 'Another DisMech embedding loader holds this run lock')
            cursor.execute("SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')")
            old = _read_run(cursor, identity)
            if old is None:
                cursor.execute('INSERT INTO dismech_embedding_runs (run_id,dismech_import_id,eaggl_embedding_run_id,config,dimensions,expected_bindings,expected_vectors,calibration,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (identity, manifest['config']['dismech_import_id'], manifest['config']['eaggl_embedding_run_id'], canonical(manifest['config']),
                     manifest['dimensions'], manifest['expected_bindings'], manifest['expected_vectors'], canonical(calibration), 'loading'))
                connection.commit()
                old = _read_run(cursor, identity)
            _check_run(old, manifest, calibration)
            for stage, table, columns, expected in _stages(inputs, vectors):
                loaded = old[5] if stage == 'vectors' else old[4]
                verification = _expected_readback(stage, expected)
                actual = _stage_readback(cursor, stage, table, columns, identity)
                require(len(actual) == loaded and actual == verification[:loaded], f'{stage}: stored rows differ from committed capture prefix')
                for start in range(loaded, len(expected), batch_size):
                    batch = expected[start:start + batch_size]
                    insert_batch(cursor, table, ('run_id', *columns), [(identity, *row) for row in batch])
                    loaded += len(batch)
                    cursor.execute(f'UPDATE dismech_embedding_runs SET loaded_{stage}=%s WHERE run_id=%s', (loaded, identity))
                    connection.commit()
                    logging.info('Loaded DisMech embedding %s %d/%d', stage, loaded, len(expected))
                require(_stage_readback(cursor, stage, table, columns, identity) == verification, f'{stage}: read-back differs from capture')
            cursor.execute("UPDATE dismech_embedding_runs SET status='complete' WHERE run_id=%s", (identity,))
            connection.commit()
    except BaseException as error:
        failure = error
        try:
            connection.rollback()
        except Exception as cleanup_error:
            logging.warning('Embedding rollback failed (%s); preserving original %s',
                            type(cleanup_error).__name__, type(error).__name__)
        raise
    finally:
        if locked:
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT RELEASE_LOCK(%s)', (lock_name,))
            except Exception as cleanup_error:
                if failure is None:
                    raise
                logging.warning('Embedding lock cleanup failed (%s); preserving original %s',
                                type(cleanup_error).__name__, type(failure).__name__)
    return {'run_id': identity, 'status': 'complete', 'bindings': len(inputs), 'vectors': len(vectors), 'dimensions': manifest['dimensions']}


def verify_capture(connection, output):
    manifest, inputs, vectors = read_local_vectors(output)
    _check_database_sources(connection, manifest, inputs)
    calibration = _local_calibration(output)
    with connection.cursor() as cursor:
        _check_run(_read_run(cursor, manifest['run_id']), manifest, calibration, complete=True)
        for stage, table, columns, expected in _stages(inputs, vectors):
            require(_stage_readback(cursor, stage, table, columns, manifest['run_id']) == _expected_readback(stage, expected),
                    f'{stage}: read-back differs from capture')
    return {'run_id': manifest['run_id'], 'verified': True, 'bindings': len(inputs), 'vectors': len(vectors), 'dimensions': manifest['dimensions']}


def load_context_vectors(connection, dismech_import_id, eaggl_run, inputs, *, run_id=None):
    """Preload only the current gap-context subset during API readiness.

    No embedding calls or writes. Every selected binding is checked against the
    exact native source record and source-file revision before vector reuse.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT run_id,config,dimensions,expected_bindings,expected_vectors,loaded_bindings,loaded_vectors,calibration FROM dismech_embedding_runs WHERE dismech_import_id=%s AND eaggl_embedding_run_id=%s AND status='complete'", (dismech_import_id, eaggl_run['run_id']))
        rows = [row for row in cursor.fetchall() if not run_id or row[0] == run_id]
        require(len(rows) == 1, 'Select one completed compatible DisMech embedding run')
        identity, config_json, dimensions, expected_bindings, expected_vectors, loaded_bindings, loaded_vectors, calibration = rows[0]
        config = json.loads(config_json)
        require(digest(canonical(config)) == identity and config['version'] == VERSION and config['templates'] == TEMPLATES
                and config['dismech_import_id'] == dismech_import_id and config['eaggl_embedding_run_id'] == eaggl_run['run_id']
                and config['eaggl_config'] == eaggl_run['config'] and dimensions == config['dimensions'] == eaggl_run['dimensions'],
                'Stored DisMech vectors are incompatible with selected sources or EAGGL space')
        require(expected_bindings == loaded_bindings and expected_vectors == loaded_vectors and len(inputs) <= expected_bindings,
                'Stored DisMech embedding coverage is incomplete')
        require(config['calibration_vectors_sha256'] == digest(canonical(eaggl_run['calibration'])), 'Stored calibration probes differ from EAGGL run')
        validate_calibration_evidence(json.loads(calibration), eaggl_run, expected_vectors=expected_vectors)
        expected = {row['source_id']: row for row in inputs}
        require(len(expected) == len(inputs), 'Duplicate source context binding')
        if not expected: return {'run_id': identity, 'config': config, 'bindings': {}, 'vectors': {}}
        hashes = [digest(source_id) for source_id in expected]
        cursor.execute('SELECT i.source_id,i.source_kind,i.source_revision,i.template,i.input_sha256,v.input_text,v.vector,v.vector_sha256 FROM dismech_embedding_inputs i JOIN dismech_embedding_vectors v ON v.run_id=i.run_id AND v.input_sha256=i.input_sha256 WHERE i.run_id=%s AND i.source_id_sha256 IN (' + ','.join(['%s'] * len(hashes)) + ')', (identity, *hashes))
        records = cursor.fetchall()
    bindings, vectors = {}, {}
    for source_id, kind, revision, template, sha, text, blob, checksum in records:
        row = {'source_id': source_id, 'source_kind': kind, 'source_revision': revision, 'template': template, 'input_sha256': sha, 'input_text': text}
        require(expected.get(source_id) == row and digest(text) == sha, 'Stored context text/source revision differs from selected source')
        vector = decode_vector(blob, checksum, dimensions).astype(np.float64)
        vector /= np.linalg.norm(vector)
        vector.setflags(write=False)
        bindings[source_id] = row
        vectors[source_id] = vector
    require(set(bindings) == set(expected), 'Stored DisMech context vectors do not cover current gaps')
    return {'run_id': identity, 'config': config, 'bindings': bindings, 'vectors': vectors}
