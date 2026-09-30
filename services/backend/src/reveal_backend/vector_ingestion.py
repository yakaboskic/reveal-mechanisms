"""Explicit immutable Vector imports, with bounded replayable batch operations.

Only this importer reads corpus vector blobs. API startup never imports or
embeds a corpus. Registry updates use the application's transaction fence.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import gzip
import base64
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
from pathlib import Path
import re

import numpy as np

from . import artifact_store
from .dismech_embeddings import (_database_inputs, load_context_vectors, read_target_run, validate_target)
from .eaggl_embeddings import database_search_index
from .repository import Repository, canonical, digest, now
from .runtime_config import ROOT, mysql_connection, setting
from .vector_retrieval import (POLICY_VERSION, ROUNDTRIP_ATOL, ROUNDTRIP_RTOL, VectorUnavailable,
    UpstashFactorIndex, client_from_environment, embedding_space, metadata, normalized, value, vector_checksum, cosine_score)


def environment(name=None):
    name = name or setting('REVEAL_VECTOR_ENVIRONMENT', 'local')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,24}', name): raise ValueError('Invalid vector environment')
    return name


class VectorRegistry:
    def __init__(self, repo=None, *, environment_name=None):
        self.repo = repo or Repository()
        self.scope = environment(environment_name)
        self._snapshots = {}
    def active_identity(self):
        with self.repo.read_transaction() as tx:
            row = tx.get('vector_active', self.scope)
        if not row: raise VectorUnavailable('No verified active Vector snapshot in this environment')
        return row['data']['snapshot_id']
    def active(self):
        with self.repo.read_transaction() as tx:
            active = tx.get('vector_active', self.scope)
            row = tx.get('vector_snapshot', active['data']['snapshot_id']) if active else None
        if not row or row['data']['status'] != 'complete' or row['data']['environment'] != self.scope:
            raise VectorUnavailable('No verified active Vector snapshot in this environment')
        return row['data']
    def get(self, identity, *, progress=True):
        with self.repo.read_transaction() as tx:
            projection = "json_extract(payload,'$.status')" if tx.sqlite else "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status'))"
            row = tx.execute('SELECT ' + projection + ' FROM reveal_records WHERE kind=%s AND id=%s', ('vector_snapshot', identity)).fetchone()
            if not row: raise VectorUnavailable('Unknown Vector import')
            cached = self._snapshots.get(identity)
            if cached is None or cached['status'] != row[0]:
                cached = tx.get('vector_snapshot', identity)['data']
                self._snapshots[identity] = cached
                while len(self._snapshots) > 4: self._snapshots.pop(next(iter(self._snapshots)))
            if cached['environment'] != self.scope: raise VectorUnavailable('Unknown Vector import')
            state = deepcopy(cached)
            if progress and state['status'] != 'complete':
                batches = tx.get_many('vector_batch', [digest([identity, key]) for key in state['batches']])
                for result in batches.values(): state['verified_batches'][result['data']['batch']] = result['data']['checksums']
        return state
    def batch_done(self, identity, key):
        with self.repo.read_transaction() as tx:
            return tx.get('vector_batch', digest([identity, key])) is not None
    def register(self, manifest):
        if manifest['status'] != 'loading' or manifest['environment'] != self.scope or manifest.get('verified_batches'):
            raise ValueError('Register an unverified loading snapshot in the selected environment')
        with self.repo.transaction() as tx:
            old = tx.get('vector_snapshot', manifest['snapshot_id'])
            if old:
                if old['data']['export_ref'] != manifest['export_ref'] or old['data']['environment'] != manifest['environment']:
                    raise ValueError('Snapshot identity conflicts with immutable export')
            else: tx.put('vector_snapshot', manifest['snapshot_id'], 'catalog', manifest)
    def save_batch(self, identity, key, checksums):
        state = self.get(identity, progress=False)
        if state['status'] == 'complete': return
        if set(checksums) != set(state['batches'][key]): raise ValueError('Incomplete verified batch')
        with self.repo.transaction() as tx:
            record_id = digest([identity, key])
            old = tx.get('vector_batch', record_id)
            data = {'snapshot_id': identity, 'batch': key, 'checksums': checksums}
            if old is not None and old['data'] != data: raise ValueError('Verified batch readback changed')
            if old is None: tx.put('vector_batch', record_id, 'catalog', data)
    def complete(self, identity, report):
        state = self.get(identity)
        with self.repo.transaction() as tx:
            if state['environment'] != self.scope: raise ValueError('Vector environment mismatch')
            if set(state['verified_batches']) != set(state['batches']): raise ValueError('Incomplete import cannot be activated')
            for kind in ('factors', 'contexts'):
                for record in state[kind]:
                    checksums = state['verified_batches'][record['batch']]
                    if record['id'] not in checksums: raise ValueError('Incomplete binding coverage')
                    record['roundtrip_sha256'] = checksums[record['id']]
            state.update(status='complete', verification=report, verified_at=now())
            tx.put('vector_snapshot', identity, 'catalog', state)
    def activate(self, identity, expected_previous=None):
        with self.repo.transaction() as tx:
            row = tx.get('vector_snapshot', identity)
            if not row or row['data']['status'] != 'complete' or row['data']['environment'] != self.scope:
                raise ValueError('Only a verified snapshot can be activated')
            state = row['data']
            if not state.get('verification', {}).get('passed'): raise ValueError('Retrieval quality gate did not pass')
            old = tx.get('vector_active', self.scope)
            previous = old['data']['snapshot_id'] if old else None
            if previous == identity: return
            if previous != expected_previous: raise ValueError('Active snapshot changed; inspect before activation')
            tx.put('vector_active', self.scope, 'catalog', {'snapshot_id': identity, 'previous_snapshot_id': previous, 'activated_at': now()})
            # Event integration can publish this same transaction's durable
            # catalog change using the repository mutation event hook.


def build_export(connection, *, batch_size=100):
    if not 1 <= batch_size <= 200: raise ValueError('Vector import batch size must be between 1 and 200')
    with connection.cursor() as cursor:
        cursor.execute("SELECT run_id,eaggl_import_id,gene_set_import_id FROM eaggl_cfde_link_runs WHERE status='complete'")
        mappings = [row for row in cursor.fetchall() if not setting('REVEAL_MAPPING_RUN_ID') or row[0] == setting('REVEAL_MAPPING_RUN_ID')]
        if len(mappings) != 1: raise ValueError('Select one complete EAGGL mapping')
        mapping, eaggl_import, geneset_import = mappings[0]
        cursor.execute("SELECT run_id FROM eaggl_embedding_runs WHERE import_id=%s AND status='complete'", (eaggl_import,))
        runs = [row[0] for row in cursor.fetchall() if not setting('REVEAL_EMBEDDING_RUN_ID') or row[0] == setting('REVEAL_EMBEDDING_RUN_ID')]
        if len(runs) != 1: raise ValueError('Select one EAGGL embedding run')
        eaggl_run = runs[0]
        cursor.execute("SELECT import_id FROM dismech_imports WHERE status='complete'")
        imports = [row[0] for row in cursor.fetchall() if not setting('REVEAL_DISMECH_IMPORT_ID') or row[0] == setting('REVEAL_DISMECH_IMPORT_ID')]
        if len(imports) != 1: raise ValueError('Select one complete DisMech import')
        dismech_import = imports[0]
        cursor.execute('SELECT f.factor_id,l.cfde_node_id,l.payload FROM eaggl_cfde_factor_links l JOIN eaggl_factors f ON f.import_id=l.eaggl_import_id AND f.factor_index=l.factor_index WHERE l.run_id=%s', (mapping,))
        factor_links = cursor.fetchall()
        native = {row[0]: row[1] for row in factor_links}
        from .evidence_package import canonical_json, sha256
        revisions = {row[0]: sha256(canonical_json(json.loads(row[2])['raw'])) for row in factor_links}
    exact = database_search_index(connection, eaggl_import, eaggl_run)
    target = read_target_run(connection, eaggl_run)
    # Independently check every context binding against current native source
    # IDs/revisions/text. The legacy validator also checks calibration evidence.
    bindings = _database_inputs(connection, dismech_import)
    with connection.cursor() as cursor:
        cursor.execute("SELECT run_id FROM dismech_embedding_runs WHERE dismech_import_id=%s AND eaggl_embedding_run_id=%s AND status='complete'", (dismech_import, eaggl_run))
        context_runs = [row[0] for row in cursor.fetchall() if not setting('REVEAL_DISMECH_EMBEDDING_RUN_ID') or row[0] == setting('REVEAL_DISMECH_EMBEDDING_RUN_ID')]
        if len(context_runs) != 1: raise ValueError('Select one compatible completed context run')
        context_run = context_runs[0]
        cursor.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM dismech_embedding_vectors WHERE run_id=%s', (context_run,))
        context_rows = {row[0]: row for row in cursor.fetchall()}
    inputs = [dict(row, input_text=context_rows[row['input_sha256']][1]) for row in bindings]
    contexts = load_context_vectors(connection, dismech_import, target, inputs, run_id=context_run)
    with connection.cursor() as cursor:
        cursor.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM eaggl_name_embeddings WHERE run_id=%s', (eaggl_run,))
        factor_rows = {row[0]: row for row in cursor.fetchall()}
    from .eaggl_embeddings import decode_vector
    vectors, records = {}, {'factors': [], 'contexts': []}
    for factor in exact.factors:
        if factor['factor_id'] not in native: continue
        sha, text, blob, checksum = factor_rows[factor['input_sha256']]
        binding = {**factor, 'input_text': text, 'source_kind': 'eaggl_factor', 'native_id': native[factor['factor_id']],
            'source_revision': revisions[factor['factor_id']], 'import_id': eaggl_import, 'mapping_run': mapping, 'embedding_run': eaggl_run,
            'template': target['config']['template']}
        records['factors'].append({'binding': binding, 'original_vector_sha256': checksum})
        vectors[('factors', factor['factor_id'])] = decode_vector(blob, checksum, target['dimensions']).tolist()
    for row in inputs:
        _, text, blob, checksum = context_rows[row['input_sha256']]
        records['contexts'].append({'binding': {**row, 'import_id': dismech_import, 'embedding_run': context_run}, 'original_vector_sha256': checksum})
        vectors[('contexts', row['source_id'])] = decode_vector(blob, checksum, target['dimensions']).tolist()
    config = {'version': 1, 'environment': environment(), 'run': target, 'mapping_run': mapping,
        'geneset_import': geneset_import, 'dismech_import': dismech_import, 'context_run_id': context_run,
        'context_config': contexts['config'], 'policy_version': POLICY_VERSION,
        'embedding_space': embedding_space(target, contexts['config']), **records}
    identity = digest(config)
    manifest = {**config, 'snapshot_id': identity, 'factor_namespace': environment() + '-f-' + identity[:48],
        'context_namespace': environment() + '-c-' + identity[:48], 'status': 'loading', 'verified_batches': {}, 'batches': {}}
    exported = []
    for kind in ('factors', 'contexts'):
        for position, record in enumerate(manifest[kind]):
            source = record['binding']['factor_id' if kind == 'factors' else 'source_id']
            record.update(id=digest([identity, kind, record['binding']]), batch=kind + ':' + str(position // batch_size))
            record['roundtrip_sha256'] = vector_checksum(vectors[(kind, source)])
            manifest['batches'].setdefault(record['batch'], []).append(record['id'])
            exported.append({'kind': kind, **record, 'vector': vectors[(kind, source)]})
    manifest['quality_probes'] = quality_baseline(manifest, exported)
    return manifest, exported


def quality_baseline(manifest, exported, probe_count=12):
    factors = [row for row in exported if row['kind'] == 'factors']
    contexts = [row for row in exported if row['kind'] == 'contexts']
    if not factors or not contexts: raise ValueError('Both factor and context corpus are required')
    matrix = normalized([row['vector'] for row in factors], manifest['run']['dimensions'])
    probes = [rows[position] for rows in (factors, contexts)
              for position in np.linspace(0, len(rows) - 1, min(probe_count, len(rows)), dtype=int)]
    result = []
    for row in probes:
        query = normalized([row['vector']], manifest['run']['dimensions'])[0]
        scores = matrix @ query
        order = sorted(range(len(factors)), key=lambda i: (-scores[i], factors[i]['id']))
        k = min(10, len(factors)); threshold = scores[order[k - 1]] - 1e-6
        result.append({'id': row['id'], 'query_vector': query.tolist(), 'query_checksum': vector_checksum(query),
            'query_vector_base64': base64.b64encode(np.asarray(query, dtype='<f8').tobytes()).decode('ascii'),
            'top1_score': float(scores[order[0]]), 'k': k,
            'eligible_ids': [factors[i]['id'] for i in order if scores[i] >= threshold]})
    return result


def save_export(manifest, exported, directory=None):
    """Save independently recoverable bounded vector batches plus their manifest."""
    directory = Path(directory or ROOT / '.runtime/vector-exports')
    def put(data):
        data = gzip.compress(data, mtime=0)
        if artifact_store.s3_enabled(): return artifact_store.store().put(data, 'application/gzip')
        if environment() != 'local': raise ValueError('Durable Vector exports require S3 outside local development')
        directory.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256(data).hexdigest()
        path = directory / (sha + '.json')
        if path.exists() and path.read_bytes() != data: raise ValueError('Immutable export checksum conflict')
        path.write_bytes(data)
        return {'store': 'filesystem', 'path': str(path.resolve()), 'sha256': sha}
    batches = {}
    for row in exported: batches.setdefault(row['batch'], []).append(row)
    def save_batch(item):
        key, rows = item
        return key, put((canonical(rows) + '\n').encode())
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = dict(pool.map(save_batch, batches.items()))
    data = (canonical({'manifest': manifest, 'batch_refs': refs}) + '\n').encode()
    return {**manifest, 'export_ref': put(data)}


def _export_object(ref, scope):
    if ref['store'] == 's3':
        storage = artifact_store.store(); storage.validate(ref)
        cache_dir = setting('REVEAL_VECTOR_IMPORT_CACHE_DIR')
        cached = Path(cache_dir) / ref['sha256'] if cache_dir else None
        if cached is not None and cached.exists(): raw = cached.read_bytes()
        else:
            raw = storage.get(ref)
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(raw)
    elif ref['store'] == 'filesystem' and scope == 'local': raw = Path(ref['path']).read_bytes()
    else: raise ValueError('Invalid export storage')
    if hashlib.sha256(raw).hexdigest() != ref['sha256']: raise ValueError('Immutable export checksum mismatch')
    if raw.startswith(b'\x1f\x8b'): raw = gzip.decompress(raw)
    return json.loads(raw)


@lru_cache(maxsize=4)
def _export_manifest(ref_json, scope):
    # Bounded immutable metadata only; corpus vectors never enter this cache.
    return _export_object(json.loads(ref_json), scope)


def read_export(manifest, batch=None):
    data = _export_manifest(canonical(manifest['export_ref']), manifest['environment'])
    if data['manifest']['snapshot_id'] != manifest['snapshot_id']: raise ValueError('Export belongs to a different snapshot')
    keys = [batch] if batch is not None else list(data['batch_refs'])
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = pool.map(lambda key: _export_object(data['batch_refs'][key], manifest['environment']), keys)
        return [row for rows in batches for row in rows]


def import_batch(registry, identity, key, *, client=None):
    """One bounded idempotent step; incomplete visibility is retried by caller."""
    manifest = registry.get(identity, progress=False)
    if manifest['status'] == 'complete' or key in manifest['verified_batches'] or registry.batch_done(identity, key): return {'batch': key, 'verified': True}
    if key not in manifest['batches']: raise ValueError('Unknown import batch')
    client = client or client_from_environment(write=True)
    rows = read_export(manifest, key)
    namespace = manifest['factor_namespace' if key.startswith('factors:') else 'context_namespace']
    client.upsert(vectors=[{'id': row['id'], 'vector': row['vector'], 'metadata': metadata(manifest, row)} for row in rows], namespace=namespace)
    results = client.fetch(ids=[row['id'] for row in rows], namespace=namespace, include_vectors=True, include_metadata=True)
    if len(results) != len(rows): raise VectorUnavailable('Imported batch is not fully visible')
    checksums = {}
    for expected, actual in zip(rows, results):
        if actual is None or value(actual, 'id') != expected['id'] or value(actual, 'metadata') != metadata(manifest, expected):
            raise VectorUnavailable('Imported vector source binding did not round trip')
        raw = value(actual, 'vector')
        if np.shape(raw) != np.shape(expected['vector']) or not np.allclose(raw, expected['vector'], rtol=ROUNDTRIP_RTOL, atol=ROUNDTRIP_ATOL):
            raise VectorUnavailable('Imported vector readback exceeds numeric tolerance')
        checksums[expected['id']] = vector_checksum(raw)
    registry.save_batch(identity, key, checksums)
    return {'batch': key, 'verified': True, 'count': len(rows)}


def verify_snapshot(registry, identity, *, client=None, probe_count=12):
    """Coverage plus deterministic exact-baseline recall gates; never sleeps."""
    manifest = registry.get(identity)
    if set(manifest['verified_batches']) != set(manifest['batches']): raise ValueError('Incomplete import cannot pass verification')
    client = client or client_from_environment(write=True)
    candidate = deepcopy(manifest)
    candidate['status'] = 'complete'
    for kind in ('factors', 'contexts'):
        for row in candidate[kind]: row['roundtrip_sha256'] = manifest['verified_batches'][row['batch']][row['id']]
    index = UpstashFactorIndex(candidate, client=client)
    index.check()
    # Exact ID inventory is checked with range, not inferred from counts.
    for kind, namespace in [('factors', candidate['factor_namespace']), ('contexts', candidate['context_namespace'])]:
        seen, cursor = set(), ''
        while True:
            page = client.range(cursor=cursor, limit=200, include_metadata=True, namespace=namespace)
            for row in value(page, 'vectors', []):
                identity_row = value(row, 'id')
                if identity_row in seen: raise VectorUnavailable('Duplicate vector inventory entry')
                seen.add(identity_row)
            cursor = value(page, 'next_cursor', '')
            if not cursor or cursor == '0': break
            if len(seen) > len(candidate[kind]): raise VectorUnavailable('Vector inventory contains unexpected bindings')
        if seen != {row['id'] for row in candidate[kind]}: raise VectorUnavailable('Vector ID inventory differs from immutable export')
    exported = read_export(manifest)
    factors = [row for row in exported if row['kind'] == 'factors']
    contexts = [row for row in exported if row['kind'] == 'contexts']
    if not factors or not contexts: raise ValueError('Both factor and context corpus are required')
    probes = [rows[position] for rows in (factors, contexts)
              for position in np.linspace(0, len(rows) - 1, min(probe_count, len(rows)), dtype=int)]
    matrix = normalized([row['vector'] for row in factors], candidate['run']['dimensions'])
    queries = normalized([row['vector'] for row in probes], candidate['run']['dimensions'])
    depth = min(len(factors), 100)
    returned = index.candidates(queries, depth)
    recall, errors, agreement = [], [], []
    ids = [row['binding']['factor_id'] for row in factors]
    by_id = dict(zip(ids, range(len(ids))))
    for query, batch in zip(queries, returned):
        exact = matrix @ query
        order = sorted(range(len(ids)), key=lambda i: (-exact[i], ids[i]))
        k = min(10, len(ids))
        observed = {row['factor_id'] for row in batch}
        # Equal vectors may have more valid ties than k; count any tied result.
        threshold = exact[order[k - 1]] - 1e-6
        valid = {ids[i] for i in order if exact[i] >= threshold}
        recall.append(min(k, len(observed & valid)) / k)
        for row in batch:
            errors.append(abs(exact[by_id[row['factor_id']]] - row['cosine_similarity']))
        agreement.append(bool(batch) and bool(exact[by_id[batch[0]['factor_id']]] >= exact[order[0]] - 1e-5))
    report = {'passed': min(recall) >= .9 and min(agreement) and bool(max(errors, default=1) < 5e-4),
        'probe_count': len(probes), 'candidate_depth': depth, 'minimum_recall_at_10': min(recall),
        'top1_agreement': sum(agreement) / len(agreement), 'maximum_cosine_error': float(max(errors, default=1)),
        'record_count': {'factors': len(factors), 'contexts': len(contexts)}, 'policy_version': POLICY_VERSION}
    if not report['passed']: raise VectorUnavailable('Vector quality gate failed: ' + canonical(report))
    registry.complete(identity, report)
    return report


def main():
    import argparse
    from dotenv import load_dotenv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--workflow', action='store_true', help='Enroll durable signed Workflow import instead of operator-run batches')
    parser.add_argument('--previous-snapshot')
    parser.add_argument('--snapshot', help='Resume an existing immutable import')
    parser.add_argument('--clone-from', help='Copy a verified snapshot into this configured environment')
    parser.add_argument('--source-environment', default='local')
    parser.add_argument('--source-table-prefix', default='reveal_workflow_local')
    parser.add_argument('--batch-size', type=int, default=100)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    registry = VectorRegistry()
    if args.snapshot and args.clone_from: parser.error('Choose resume or clone, not both')
    if args.snapshot:
        manifest = registry.get(args.snapshot)
    elif args.clone_from:
        source_registry = VectorRegistry(Repository(table_prefix=args.source_table_prefix), environment_name=args.source_environment)
        source = source_registry.get(args.clone_from)
        with source_registry.repo.read_transaction() as tx: archive = tx.get('vector_archive', args.clone_from)
        manifest = clone_export(source, unmapped_archive=archive['data'] if archive else source.get('unmapped_archive'))
        registry.register(manifest)
    else:
        connection = mysql_connection()
        try:
            manifest, rows = build_export(connection, batch_size=args.batch_size)
            if artifact_store.s3_enabled(): manifest['unmapped_archive'] = archive_unmapped(connection, manifest)
        finally: connection.close()
        manifest = save_export(manifest, rows)
        registry.register(manifest)
    print(canonical({'snapshot_id': manifest['snapshot_id'], 'batches': len(manifest['batches']), 'environment': environment()}), flush=True)
    if args.workflow:
        import asyncio
        from .vector_workflow import enroll, dispatch_pending
        enroll(registry, manifest['snapshot_id'], activate=args.activate, expected_previous=args.previous_snapshot)
        print(canonical(asyncio.run(dispatch_pending(registry.repo))), flush=True)
        return
    client = client_from_environment(write=True)
    _export_manifest(canonical(manifest['export_ref']), manifest['environment'])
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(lambda key: import_batch(registry, manifest['snapshot_id'], key, client=client), manifest['batches']):
            print(canonical(result), flush=True)
    print(canonical(verify_snapshot(registry, manifest['snapshot_id'], client=client)), flush=True)
    if args.activate:
        registry.activate(manifest['snapshot_id'], args.previous_snapshot)
        print('Activated verified snapshot ' + manifest['snapshot_id'], flush=True)




def archive_unmapped(connection, manifest):
    """Retain non-serving EAGGL bindings too, without making them candidates."""
    from .eaggl_embeddings import decode_vector
    target = read_target_run(connection, manifest['run']['run_id'])
    retained_run = deepcopy(manifest['run'])
    for probe in retained_run['calibration']: probe['vector'] = np.asarray(probe['vector'], dtype='<f4').tolist()
    if target != retained_run: raise ValueError('Original EAGGL run changed before archival')
    with connection.cursor() as cursor:
        cursor.execute('SELECT f.factor_id,f.label,f.trait,f.input_sha256,v.input_text,v.vector,v.vector_sha256 '
            'FROM eaggl_factors f JOIN eaggl_name_embeddings v ON v.run_id=%s AND v.input_sha256=f.input_sha256 '
            'WHERE f.import_id=%s AND NOT EXISTS (SELECT 1 FROM eaggl_cfde_factor_links l '
            'WHERE l.run_id=%s AND l.eaggl_import_id=f.import_id AND l.factor_index=f.factor_index) ORDER BY f.factor_id',
            (target['run_id'], target['config']['import_id'], manifest['mapping_run']))
        records = cursor.fetchall()
    rows = []
    for factor_id, label, trait, sha, text, blob, checksum in records:
        if hashlib.sha256(text.encode()).hexdigest() != sha or label.strip() != text: raise ValueError('Unmapped source text binding changed')
        rows.append({'factor_id': factor_id, 'label': label, 'trait': trait, 'input_sha256': sha, 'input_text': text,
            'original_vector_sha256': checksum, 'vector': decode_vector(blob, checksum, target['dimensions']).tolist()})
    if not artifact_store.s3_enabled(): raise ValueError('Full corpus archive requires immutable S3 storage')
    def put(batch): return artifact_store.store().put(gzip.compress((canonical(batch) + '\n').encode(), mtime=0), 'application/gzip')
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(put, [rows[i:i+100] for i in range(0, len(rows), 100)]))
    archive = {'format': 'reveal.unmapped-vector-archive/1', 'run': target, 'mapping_run': manifest['mapping_run'],
        'source_import': target['config']['import_id'], 'binding_count': len(rows),
        'unique_vector_count': len({row['input_sha256'] for row in rows}), 'batch_refs': refs}
    return {'binding_count': len(rows), 'export_ref': put(archive)}


def clone_export(source, *, unmapped_archive=None):
    """Rebind a verified immutable corpus to an independent environment.

    Streams bounded source batches from S3, never MySQL or an embedder. Call
    with the destination environment and S3 prefix already configured, and a
    read-prefix grant for the retained source export.
    """
    if source['status'] != 'complete' or not source.get('verification', {}).get('passed'):
        raise ValueError('Only a verified snapshot can seed another environment')
    if source['environment'] == environment(): raise ValueError('Clone requires a different environment')
    identity = digest(['vector-clone-v1', source['snapshot_id'], environment(), POLICY_VERSION])
    manifest = deepcopy(source)
    for key in ('verification', 'verified_at', 'export_ref'): manifest.pop(key, None)
    manifest.update(snapshot_id=identity, environment=environment(), status='loading', verified_batches={}, batches={},
        factor_namespace=environment() + '-f-' + identity[:48], context_namespace=environment() + '-c-' + identity[:48],
        derived_from={'snapshot_id': source['snapshot_id'], 'export_ref': source['export_ref']})
    if unmapped_archive:
        archive = _export_object(unmapped_archive['export_ref'], source['environment'])
        def copy_archive_batch(ref):
            rows = _export_object(ref, source['environment'])
            return artifact_store.store().put(gzip.compress((canonical(rows)+'\n').encode(), mtime=0), 'application/gzip')
        with ThreadPoolExecutor(max_workers=4) as pool:
            archive['batch_refs'] = list(pool.map(copy_archive_batch, archive['batch_refs']))
        archive['derived_from'] = unmapped_archive['export_ref']
        archive_ref = artifact_store.store().put(gzip.compress((canonical(archive)+'\n').encode(), mtime=0), 'application/gzip')
        manifest['unmapped_archive'] = {'binding_count': unmapped_archive['binding_count'], 'export_ref': archive_ref}
    replacements = {}
    for kind in ('factors', 'contexts'):
        for row in manifest[kind]:
            old_id = row['id']; row['id'] = digest([identity, kind, row['binding']]); replacements[old_id] = row
            manifest['batches'].setdefault(row['batch'], []).append(row['id'])
    source_export = _export_manifest(canonical(source['export_ref']), source['environment'])
    # The exact gate needs all serving factors and representative contexts;
    # it does not retain a full context-vector matrix in memory.
    chosen = {source['contexts'][i]['id'] for i in np.linspace(0, len(source['contexts'])-1, min(12,len(source['contexts'])), dtype=int)}
    def copy_batch(key):
        original = _export_object(source_export['batch_refs'][key], source['environment'])
        batch = [{'kind': row['kind'], **replacements[row['id']], 'vector': row['vector']} for row in original]
        reference = artifact_store.store().put(gzip.compress((canonical(batch)+'\n').encode(), mtime=0), 'application/gzip')
        baseline = [new for old, new in zip(original, batch) if old['kind'] == 'factors' or old['id'] in chosen]
        return key, reference, baseline
    refs, baseline = {}, []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, ref, rows in pool.map(copy_batch, source['batches']):
            refs[key] = ref; baseline.extend(rows)
    manifest['quality_probes'] = quality_baseline(manifest, baseline)
    root = gzip.compress((canonical({'manifest': manifest, 'batch_refs': refs})+'\n').encode(), mtime=0)
    return {**manifest, 'export_ref': artifact_store.store().put(root, 'application/gzip')}


if __name__ == '__main__': main()
