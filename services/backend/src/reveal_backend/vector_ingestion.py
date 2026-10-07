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
import threading

import numpy as np

from . import artifact_store
from .dismech_embeddings import (_database_inputs, load_context_vectors, read_target_run, validate_target)
from .eaggl_bundle import text_hash
from .eaggl_embeddings import database_search_index, decode_vector
from .reference_generation import (KPN_KIND, KPN_MODEL, ReferenceError, get_generation, legacy_generation_id,
    parse_factor_key, public_id)
from .repository import Repository, canonical, digest, now
from .runtime_config import ROOT, mysql_connection, setting
from .vector_retrieval import (ID_SCHEME, KINDS, NAMESPACE_KEYS, NAMESPACE_RE, POLICY_VERSION, REFERENCE_SOURCE_KINDS,
    ROUNDTRIP_ATOL, ROUNDTRIP_RTOL, VectorUnavailable, UpstashFactorIndex, batch_kind, client_from_environment,
    embedding_space, metadata, normalized, snapshot_kinds, value, vector_checksum, cosine_score)

ENVIRONMENT_RE = re.compile(r'[a-z][a-z0-9_-]{0,24}')
# Reference-generation namespaces are {env}-{prefix}-{snapshot_id[:24]}; legacy ones {env}-f|c-{snapshot_id[:48]}.
NAMESPACE_PREFIXES = {'factors': 'eaggl-factor', 'contexts': 'dismech-context', 'gene_sets': 'cfde-geneset', 'collections': 'cfde-collection'}
VECTOR_ID_RE = re.compile(r'[A-Za-z0-9._:-]{1,128}')
DAPPER_ID_RES = {'cfde_gene_set': re.compile(r'dapper:GeneSet\.[A-Za-z0-9_-]{32}'),
                 'cfde_collection': re.compile(r'dapper:GeneSetCollection\.[A-Za-z0-9_-]{32}')}
# Manifest rows of gene sets/collections; their full bindings are in the export batches.
COMPACT_KEYS = ('id', 'batch', 'original_vector_sha256', 'roundtrip_sha256')
# Serving summaries of snapshot rows by (table prefix, environment, snapshot id): ((version, updated_at), summary).
_summaries, _summaries_lock = {}, threading.Lock()


def environment(name=None):
    name = name or setting('REVEAL_VECTOR_ENVIRONMENT', 'local')
    if not ENVIRONMENT_RE.fullmatch(name): raise ValueError('Invalid vector environment')
    return name


def generation_namespaces(identity, scope=None):
    """The four namespaces of a reference-generation snapshot in one vector environment."""
    scope = environment(scope)
    return {NAMESPACE_KEYS[kind]: f'{scope}-{prefix}-{identity[:24]}' for kind, prefix in NAMESPACE_PREFIXES.items()}


def context_vector_id(source_kind, source_id):
    """Deterministic DisMech vector id; source ids reach 143 bytes and contain '/', '#' and ':'."""
    return f'dismech:{source_kind}:{hashlib.sha256(source_id.encode()).hexdigest()[:32]}'


def source_vector_id(kind, binding):
    """Vector id under id_scheme source-id-v1: the factor key, a hashed DisMech id, or the DAPPER id."""
    if kind == 'factors': return binding['factor_id']
    if kind == 'contexts': return context_vector_id(binding['source_kind'], binding['source_id'])
    return binding['source_id']


class VectorRegistry:
    def __init__(self, repo=None, *, environment_name=None):
        self.repo = repo or Repository()
        self.scope = environment(environment_name)
        self._snapshots = {}
    def active_identity_in(self, tx):
        """The active snapshot id read in the caller's transaction (e.g. with reference_active), or None."""
        row = tx.get('vector_active', self.scope)
        return row['data']['snapshot_id'] if row else None
    def active_identity(self, *, required=True):
        with self.repo.single_read() as tx: identity = self.active_identity_in(tx)
        if not identity and required: raise VectorUnavailable('No verified active Vector snapshot in this environment')
        return identity
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
    def serving(self, identity, *, summary=False, context_ids=None):
        """Read only serving fields, projecting large import-only arrays on the DB.

        Counts come from the same immutable verified row as the run pins. Full
        manifests remain available through get() for import, audit and export.
        """
        fields = ('snapshot_id', 'status', 'environment', 'run', 'mapping_run', 'geneset_import',
            'dismech_import', 'context_run_id', 'context_config', 'policy_version', 'embedding_space',
            'id_scheme', 'reference_generation_id', 'gene_set_embedding_space', 'export_ref',
            *NAMESPACE_KEYS.values(), *(('factors', 'contexts') if not summary else ()))
        pairs = [f"'{key}',JSON_EXTRACT(payload,'$.{key}')" for key in fields]
        counts = ','.join(f"'{kind}',JSON_LENGTH(JSON_EXTRACT(payload,'$.{kind}'))" for kind in KINDS)
        key = stamp = None
        with self.repo.read_transaction() as tx:
            if summary:
                # A summary projects the whole 31-53 MB row; re-project only when the row changed (every write bumps its
                # version; a deleted and recreated row has a new updated_at). The key is read before the content.
                key = (getattr(self.repo, 'table_prefix', None), self.scope, identity)
                stamp = tx.execute('SELECT version,updated_at FROM reveal_records WHERE kind=%s AND id=%s',
                                   ('vector_snapshot', identity)).fetchone()
                stamp = tuple(stamp) if stamp else None
                with _summaries_lock: cached = _summaries.get(key)
                if stamp and cached and cached[0] == stamp: return deepcopy(cached[1])
            # SQLite's JSON extension spells the array-length function differently.
            if tx.sqlite: counts = counts.replace('JSON_LENGTH(', 'JSON_ARRAY_LENGTH(')
            parameters = []
            if context_ids is not None and not summary:
                # Contexts of unrelated diseases dominate the full manifest.
                # Select exact required source IDs in SQL, retaining full rows.
                selected = json.dumps(sorted(set(context_ids)), separators=(',', ':'))
                if tx.sqlite:
                    contexts = "(SELECT JSON_GROUP_ARRAY(JSON(c.value)) FROM JSON_EACH(payload,'$.contexts') c WHERE JSON_EXTRACT(c.value,'$.binding.source_id') IN (SELECT value FROM JSON_EACH(%s)))"
                else:
                    contexts = "(SELECT JSON_ARRAYAGG(c.item) FROM JSON_TABLE(payload,'$.contexts[*]' COLUMNS(item JSON PATH '$')) c WHERE BINARY JSON_UNQUOTE(JSON_EXTRACT(c.item,'$.binding.source_id')) IN (SELECT BINARY JSON_UNQUOTE(chosen.item) FROM JSON_TABLE(%s,'$[*]' COLUMNS(item JSON PATH '$')) chosen))"
                pairs[pairs.index("'contexts',JSON_EXTRACT(payload,'$.contexts')")] = "'contexts',COALESCE("+contexts+",JSON_ARRAY())"
                parameters.append(selected)
            pairs.extend([f"'_serving_counts',JSON_OBJECT({counts})",
                "'_serving_verified',JSON_EXTRACT(payload,'$.verification.passed')"])
            row = tx.execute('SELECT JSON_OBJECT('+','.join(pairs)+') FROM reveal_records WHERE kind=%s AND id=%s',
                (*parameters, 'vector_snapshot', identity)).fetchone()
        state = json.loads(row[0]) if row else None
        if (not state or state['snapshot_id'] != identity or state['status'] != 'complete'
                or state['environment'] != self.scope or state['_serving_verified'] != True):
            raise VectorUnavailable('No verified Vector snapshot in this environment')
        state = {name: value for name, value in state.items() if value is not None}
        state['_serving_counts'] = {name: value for name, value in state['_serving_counts'].items() if value is not None}
        if stamp:
            with _summaries_lock:
                _summaries.pop(key, None); _summaries[key] = (stamp, deepcopy(state))
                while len(_summaries) > 16: _summaries.pop(next(iter(_summaries)))
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
            kinds = snapshot_kinds(state)
            if ('id_scheme' in state or len(kinds) > 2) and (state.get('id_scheme') != ID_SCHEME or kinds != list(KINDS)
                                                             or not all(state.get(kind) for kind in KINDS)):
                raise ValueError('A reference-generation snapshot needs every corpus kind')
            for kind in kinds:
                for record in state[kind]:
                    checksums = state['verified_batches'][record['batch']]
                    if record['id'] not in checksums: raise ValueError('Incomplete binding coverage')
                    record['roundtrip_sha256'] = checksums[record['id']]
            state.update(status='complete', verification=report, verified_at=now())
            tx.put('vector_snapshot', identity, 'catalog', state)
    def activate(self, identity, expected_previous=None):
        with self.repo.transaction() as tx: self.activate_in(tx, identity, expected_previous)
    def activate_in(self, tx, identity, expected_previous=None):
        """Compare-and-swap vector_active inside the caller's transaction (reference_reload
        switches vector_active and reference_active together)."""
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
    context_run, context_config, context_records, context_vectors = _export_contexts(connection, dismech_import, target)
    with connection.cursor() as cursor:
        cursor.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM eaggl_name_embeddings WHERE run_id=%s', (eaggl_run,))
        factor_rows = {row[0]: row for row in cursor.fetchall()}
    vectors, records = {}, {'factors': [], 'contexts': context_records}
    for factor in exact.factors:
        if factor['factor_id'] not in native: continue
        sha, text, blob, checksum = factor_rows[factor['input_sha256']]
        binding = {**factor, 'input_text': text, 'source_kind': 'eaggl_factor', 'native_id': native[factor['factor_id']],
            'source_revision': revisions[factor['factor_id']], 'import_id': eaggl_import, 'mapping_run': mapping, 'embedding_run': eaggl_run,
            'template': target['config']['template']}
        records['factors'].append({'binding': binding, 'original_vector_sha256': checksum})
        vectors[('factors', factor['factor_id'])] = decode_vector(blob, checksum, target['dimensions']).tolist()
    vectors.update((('contexts', source), vector) for source, vector in context_vectors.items())
    config = {'version': 1, 'environment': environment(), 'run': target, 'mapping_run': mapping,
        'geneset_import': geneset_import, 'dismech_import': dismech_import, 'context_run_id': context_run,
        'context_config': context_config, 'policy_version': POLICY_VERSION,
        'embedding_space': embedding_space(target, context_config), **records}
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


def _export_contexts(connection, dismech_import, target):
    """DisMech context records and vectors of the context run bound to one EAGGL embedding run."""
    # Independently check every context binding against current native source
    # IDs/revisions/text. The legacy validator also checks calibration evidence.
    bindings = _database_inputs(connection, dismech_import)
    with connection.cursor() as cursor:
        cursor.execute("SELECT run_id FROM dismech_embedding_runs WHERE dismech_import_id=%s AND eaggl_embedding_run_id=%s AND status='complete'", (dismech_import, target['run_id']))
        context_runs = [row[0] for row in cursor.fetchall() if not setting('REVEAL_DISMECH_EMBEDDING_RUN_ID') or row[0] == setting('REVEAL_DISMECH_EMBEDDING_RUN_ID')]
        if len(context_runs) != 1: raise ValueError('Select one compatible completed context run')
        context_run = context_runs[0]
        cursor.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM dismech_embedding_vectors WHERE run_id=%s', (context_run,))
        context_rows = {row[0]: row for row in cursor.fetchall()}
    inputs = [dict(row, input_text=context_rows[row['input_sha256']][1]) for row in bindings]
    contexts = load_context_vectors(connection, dismech_import, target, inputs, run_id=context_run)
    records, vectors = [], {}
    for row in inputs:
        _, text, blob, checksum = context_rows[row['input_sha256']]
        records.append({'binding': {**row, 'import_id': dismech_import, 'embedding_run': context_run}, 'original_vector_sha256': checksum})
        vectors[row['source_id']] = decode_vector(blob, checksum, target['dimensions']).tolist()
    return context_run, contexts['config'], records, vectors


def build_generation_export(connection, generation_id, *, batch_size=100):
    """Immutable snapshot export of one KPN reference generation (docs/reference-reload.md §6).

    Keeps build_export's manifest format, policy and factor/context embedding space, with
    id_scheme source-id-v1: vector ids are source ids (factor key, hashed DisMech id, DAPPER
    id), namespaces are {env}-<kind>-{snapshot_id[:24]}, and CFDE gene sets/collections are
    two more kinds whose vectors belong to the reference_vectors space (gene_set_embedding_space).
    Factors come from reference_factors with their EAGGL label vectors; contexts are the
    DisMech run bound to the generation's EAGGL embedding run, exactly as in build_export.
    """
    if not 1 <= batch_size <= 200: raise ValueError('Vector import batch size must be between 1 and 200')
    generation = get_generation(connection, generation_id)
    if not generation or generation['kind'] != KPN_KIND or generation['model'] != KPN_MODEL:
        raise ValueError('Select a loaded KPN reference generation')
    if generation['status'] not in ('complete', 'superseded'): raise ValueError('Reference generation is not completely loaded')
    eaggl_run, dismech_import = generation['eaggl_embedding_run_id'], generation['dismech_import_id']
    if not eaggl_run or not dismech_import: raise ValueError('Reference generation lacks its EAGGL embedding run or DisMech import')
    target = read_target_run(connection, eaggl_run)
    eaggl_import = target['config']['import_id']
    if generation['eaggl_import_id'] != eaggl_import: raise ValueError('Reference generation EAGGL import differs from its embedding run')
    with connection.cursor() as cursor:
        cursor.execute('SELECT status FROM eaggl_imports WHERE import_id=%s', (eaggl_import,))
        if cursor.fetchone() != ('complete',): raise ValueError('Source EAGGL import is not complete')
        cursor.execute('SELECT factor_id,trait,input_sha256 FROM eaggl_factors WHERE import_id=%s', (eaggl_import,))
        eaggl = {row[0]: row for row in cursor.fetchall()}
        cursor.execute('SELECT factor_key,public_id,eaggl_factor_id,kpn_trait_id,label,eaggl_import_id,input_sha256,source_revision '
                       'FROM reference_factors WHERE generation_id=%s', (generation_id,))
        factor_rows = cursor.fetchall()
        cursor.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM eaggl_name_embeddings WHERE run_id=%s', (eaggl_run,))
        embeddings = {row[0]: row for row in cursor.fetchall()}
    context_run, context_config, context_records, context_vectors = _export_contexts(connection, dismech_import, target)
    vectors, records = {}, {kind: [] for kind in KINDS}
    records['contexts'] = context_records
    def order(row):
        try: parsed = parse_factor_key(row[0])
        except ReferenceError as error: raise ValueError(f'Invalid reference factor key: {row[0]!r}') from error
        return parsed['kpn_trait_id'], int(parsed['factor'][6:]), row[0]
    for key, public, eaggl_factor, trait_id, label, row_import, sha, revision in sorted(factor_rows, key=order):
        parsed = parse_factor_key(key)
        if (parsed['kpn_trait_id'] != trait_id or public != public_id(trait_id, parsed['factor'])
                or not eaggl_factor.endswith('::' + parsed['factor'])):
            raise ValueError(f'Inconsistent reference factor identifiers: {key!r}')
        if row_import not in (None, eaggl_import): raise ValueError('Reference factor belongs to a different EAGGL import')
        source = eaggl.get(eaggl_factor)
        if source is None or source[2] != sha: raise ValueError(f'Reference factor {key} is not bound to its EAGGL factor label')
        if sha not in embeddings: raise ValueError(f'Reference factor {key} has no EAGGL label embedding')
        _, text, blob, checksum = embeddings[sha]
        if text_hash(text) != sha or label.strip() != text: raise ValueError('Reference factor text/embedding mismatch')
        binding = {'factor_id': key, 'label': label, 'trait': source[1], 'input_sha256': sha, 'input_text': text,
            'source_kind': 'eaggl_factor', 'native_id': public, 'source_revision': revision, 'import_id': eaggl_import,
            'mapping_run': generation_id, 'embedding_run': eaggl_run, 'template': target['config']['template'],
            'reference_generation_id': generation_id, 'kpn_trait_id': trait_id, 'eaggl_factor_id': eaggl_factor}
        records['factors'].append({'binding': binding, 'original_vector_sha256': checksum})
        vectors[('factors', key)] = decode_vector(blob, checksum, target['dimensions']).tolist()
    vectors.update((('contexts', source), vector) for source, vector in context_vectors.items())
    space_id = _export_gene_sets(connection, generation_id, target['dimensions'], records, vectors)
    if not all(records[kind] for kind in KINDS): raise ValueError('A reference-generation snapshot needs every corpus kind')
    # mapping_run/geneset_import carry the generation id, as KPN catalog bindings do.
    config = {'version': 1, 'environment': environment(), 'run': target, 'mapping_run': generation_id,
        'geneset_import': generation_id, 'dismech_import': dismech_import, 'context_run_id': context_run,
        'context_config': context_config, 'policy_version': POLICY_VERSION,
        'embedding_space': embedding_space(target, context_config), 'id_scheme': ID_SCHEME,
        'reference_generation_id': generation_id, 'reference_model': KPN_MODEL, 'gene_set_embedding_space': space_id, **records}
    identity = digest(config)
    manifest = {**config, 'snapshot_id': identity, **generation_namespaces(identity),
        'status': 'loading', 'verified_batches': {}, 'batches': {}}
    exported = []
    for kind in KINDS:
        seen = set()
        for position, record in enumerate(manifest[kind]):
            source = record['binding']['factor_id' if kind == 'factors' else 'source_id']
            record.update(id=source_vector_id(kind, record['binding']), batch=kind + ':' + str(position // batch_size))
            if not VECTOR_ID_RE.fullmatch(record['id']) or record['id'] in seen: raise ValueError(f'Invalid or duplicate vector id {record["id"]!r}')
            seen.add(record['id'])
            record['roundtrip_sha256'] = vector_checksum(vectors[(kind, source)])
            manifest['batches'].setdefault(record['batch'], []).append(record['id'])
            exported.append({'kind': kind, **record, 'vector': vectors[(kind, source)]})
    # ~44k gene sets would dominate the single vector_snapshot record: the manifest keeps
    # compact rows (id == DAPPER source id); bindings live in the export batches and metadata.
    for kind in REFERENCE_SOURCE_KINDS:
        manifest[kind] = [{key: record[key] for key in COMPACT_KEYS} for record in manifest[kind]]
    manifest['quality_probes'] = quality_baseline(manifest, exported)
    return manifest, exported


def _export_gene_sets(connection, generation_id, dimensions, records, vectors):
    """CFDE gene-set and collection records and vectors of a generation; returns their embedding space."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT collection_id,cfde_label,library,n_sets FROM cfde_gene_set_collections WHERE generation_id=%s', (generation_id,))
        collections = {row[0]: row for row in cursor.fetchall()}
        cursor.execute('SELECT gene_set_id,collection_id,gene_set_name,library,n_genes FROM cfde_gene_sets WHERE generation_id=%s', (generation_id,))
        gene_sets = {row[0]: row for row in cursor.fetchall()}
        cursor.execute('SELECT source_kind,source_id,space_id,input_sha256,input_text,vector,vector_sha256 FROM reference_vectors WHERE generation_id=%s', (generation_id,))
        rows = sorted(cursor.fetchall(), key=lambda row: (row[0], row[1]))
        spaces = {row[2] for row in rows}
        if len(spaces) != 1: raise ValueError('Gene-set and collection vectors must exist in exactly one recorded embedding space')
        space_id = next(iter(spaces))
        cursor.execute('SELECT dimensions,metric FROM embedding_spaces WHERE space_id=%s', (space_id,))
        space = cursor.fetchone()
    if space is None or int(space[0]) != dimensions or str(space[1]).lower() != 'cosine':
        raise ValueError('Gene-set embedding space is incompatible with the served Vector index')
    sources = {'cfde_gene_set': ('gene_sets', gene_sets), 'cfde_collection': ('collections', collections)}
    for source_kind, source, _, sha, text, blob, checksum in rows:
        if source_kind not in sources or source not in sources[source_kind][1] or not DAPPER_ID_RES[source_kind].fullmatch(source):
            raise ValueError(f'Reference vector {source_kind}:{source} has no CFDE record')
        if text_hash(text) != sha: raise ValueError(f'Reference vector {source} input text/hash mismatch')
        kind, row = sources[source_kind][0], sources[source_kind][1][source]
        if kind == 'gene_sets':
            _, collection, name, library, n_genes = row
            if collection not in collections: raise ValueError(f'Gene set {source} references an unknown collection')
            binding = {'source_kind': source_kind, 'source_id': source, 'name': name, 'collection_id': collection,
                       'library': library, 'n_genes': int(n_genes), 'input_sha256': sha, 'generation_id': generation_id}
        else:
            _, label, library, n_sets = row
            binding = {'source_kind': source_kind, 'source_id': source, 'label': label, 'library': library,
                       'n_sets': int(n_sets), 'input_sha256': sha, 'generation_id': generation_id}
        records[kind].append({'binding': binding, 'original_vector_sha256': checksum})
        vectors[(kind, source)] = decode_vector(blob, checksum, dimensions).tolist()
    if ({row['binding']['source_id'] for row in records['gene_sets']} != set(gene_sets)
            or {row['binding']['source_id'] for row in records['collections']} != set(collections)):
        raise ValueError('Reference vectors do not cover every CFDE gene set and collection')
    return space_id


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


def read_export(manifest, batch=None, *, kinds=None):
    data = _export_manifest(canonical(manifest['export_ref']), manifest['environment'])
    if data['manifest']['snapshot_id'] != manifest['snapshot_id']: raise ValueError('Export belongs to a different snapshot')
    keys = [batch] if batch is not None else [key for key in data['batch_refs'] if kinds is None or key.split(':', 1)[0] in kinds]
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = pool.map(lambda key: _export_object(data['batch_refs'][key], manifest['environment']), keys)
        return [row for rows in batches for row in rows]


def import_batch(registry, identity, key, *, client=None):
    """One bounded idempotent step; incomplete visibility is retried by caller."""
    manifest = registry.get(identity, progress=False)
    if manifest['status'] == 'complete' or key in manifest['verified_batches'] or registry.batch_done(identity, key): return {'batch': key, 'verified': True}
    if key not in manifest['batches'] or batch_kind(key) not in snapshot_kinds(manifest): raise ValueError('Unknown import batch')
    client = client or client_from_environment(write=True)
    rows = read_export(manifest, key)
    namespace = manifest[NAMESPACE_KEYS[batch_kind(key)]]
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
    kinds = snapshot_kinds(candidate)
    for kind in kinds:
        for row in candidate[kind]: row['roundtrip_sha256'] = manifest['verified_batches'][row['batch']][row['id']]
    index = UpstashFactorIndex(candidate, client=client)
    index.check()
    # Exact ID inventory of every namespace is checked with range, not inferred from counts.
    for kind in kinds:
        namespace = candidate[NAMESPACE_KEYS[kind]]
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
    # The retrieval quality gate covers factors and contexts only; gene-set and collection
    # vectors were verified by readback at import and by the inventory above.
    exported = read_export(manifest, kinds=('factors', 'contexts'))
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
        'record_count': {'factors': len(factors), 'contexts': len(contexts)} | {kind: len(candidate[kind]) for kind in kinds[2:]},
        'policy_version': POLICY_VERSION}
    if not report['passed']: raise VectorUnavailable('Vector quality gate failed: ' + canonical(report))
    registry.complete(identity, report)
    return report


BINDING_COLUMNS = ('environment', 'namespace', 'vector_id', 'snapshot_id', 'generation_id', 'source_kind', 'source_id',
                   'source_id_sha256', 'vector_sha256')


def binding_rows(manifest):
    """vector_bindings rows (docs/reference-reload.md §3) for every vector of a snapshot, as BINDING_COLUMNS tuples.

    source_kind is the binding's (eaggl_factor, mechanism/knowledge_gap, cfde_gene_set, cfde_collection);
    source_id is the MySQL key (factor key or legacy EAGGL factor id, DisMech source id, DAPPER id, which is
    also the vector id of the compact gene-set/collection rows); vector_sha256 is the sha256 of the original
    float32 blob in MySQL.
    """
    generation = manifest.get('reference_generation_id') or legacy_generation_id(manifest.get('mapping_run'))
    rows = []
    for kind in snapshot_kinds(manifest):
        namespace = manifest[NAMESPACE_KEYS[kind]]
        if not NAMESPACE_RE.fullmatch(namespace or ''): raise ValueError('Invalid Vector snapshot namespace')
        for record in manifest[kind]:
            if kind in REFERENCE_SOURCE_KINDS: source_kind, source = REFERENCE_SOURCE_KINDS[kind], record['id']
            else: source_kind, source = record['binding']['source_kind'], record['binding']['factor_id' if kind == 'factors' else 'source_id']
            rows.append((manifest['environment'], namespace, record['id'], manifest['snapshot_id'], generation,
                         source_kind, source, hashlib.sha256(source.encode()).hexdigest(), record['original_vector_sha256']))
    return rows


def record_vector_bindings(connection, manifest, *, batch_size=500):
    """Record every uploaded vector of a verified snapshot in vector_bindings (INSERT IGNORE, idempotent).

    Returns the number of newly inserted rows. Fails when the stored rows of this environment
    and snapshot then differ from the manifest (a conflicting earlier row was kept).
    """
    if manifest.get('status') != 'complete' or not manifest.get('verification', {}).get('passed'):
        raise ValueError('Record vector bindings only for a verified snapshot')
    if not ENVIRONMENT_RE.fullmatch(manifest.get('environment') or ''): raise ValueError('Invalid vector environment')
    rows = binding_rows(manifest)
    inserted = 0
    with connection.cursor() as cursor:
        for start in range(0, len(rows), batch_size):
            cursor.executemany('INSERT IGNORE INTO vector_bindings(' + ','.join(BINDING_COLUMNS) + ') VALUES (' +
                               ','.join(['%s'] * len(BINDING_COLUMNS)) + ')', rows[start:start + batch_size])
            inserted += max(cursor.rowcount or 0, 0)
        cursor.execute('SELECT ' + ','.join(BINDING_COLUMNS) + ' FROM vector_bindings WHERE environment=%s AND snapshot_id=%s',
                       (manifest['environment'], manifest['snapshot_id']))
        stored = {tuple(row) for row in cursor.fetchall()}
    connection.commit()
    if stored != set(rows): raise ValueError('vector_bindings rows conflict with the verified snapshot')
    return inserted


def list_namespaces(client, *, environment=None):
    """Upstash namespaces with their vector counts, optionally only those of one environment ('{env}-')."""
    if environment is not None and not ENVIRONMENT_RE.fullmatch(environment or ''): raise ValueError('Invalid vector environment')
    info = client.info()
    counts = {name: value(record, 'vector_count', 0) for name, record in (value(info, 'namespaces', {}) or {}).items()}
    if hasattr(client, 'list_namespaces'):
        for name in client.list_namespaces(): counts.setdefault(name, 0)
    return {name: count for name, count in sorted(counts.items()) if environment is None or name.startswith(environment + '-')}


def deletable_namespace(name, environment):
    """True only for a snapshot namespace of exactly this environment, in the legacy or generation layout."""
    return bool(isinstance(environment, str) and ENVIRONMENT_RE.fullmatch(environment) and isinstance(name, str)
                and NAMESPACE_RE.fullmatch(name) and re.fullmatch(re.escape(environment) + r'-(?:[fc]-[a-f0-9]{48}|(?:'
                + '|'.join(NAMESPACE_PREFIXES.values()) + r')-[a-f0-9]{24})', name))


def delete_namespace(client, name, *, environment, protected=()):
    """Delete one snapshot namespace of this environment; refuses anything else. False when already absent."""
    if not deletable_namespace(name, environment):
        raise ValueError(f'Refusing to delete Vector namespace {name!r}: not a snapshot namespace of environment {environment!r}')
    if name in set(protected): raise ValueError(f'Refusing to delete protected Vector namespace {name!r}')
    if name not in list_namespaces(client, environment=environment): return False
    client.delete_namespace(name)
    return True


def delete_vector_bindings(connection, name, *, environment):
    """Remove the vector_bindings rows of one deleted snapshot namespace; returns the row count."""
    if not deletable_namespace(name, environment): raise ValueError(f'Refusing to delete bindings of Vector namespace {name!r}')
    with connection.cursor() as cursor:
        cursor.execute('DELETE FROM vector_bindings WHERE environment=%s AND namespace=%s', (environment, name))
        count = cursor.rowcount
    connection.commit()
    return count


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
    parser.add_argument('--generation', help='Export this KPN reference generation (docs/reference-reload.md §6) instead of the legacy mapping')
    args = parser.parse_args()
    load_dotenv(args.env_file)
    registry = VectorRegistry()
    if args.snapshot and args.clone_from: parser.error('Choose resume or clone, not both')
    if args.generation and (args.snapshot or args.clone_from): parser.error('Choose one of resume, clone or generation')
    if args.generation and args.activate: parser.error('Activate reference-generation snapshots with `python -m reveal_backend.reference_reload apply`')
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
            if args.generation:
                manifest, rows = build_generation_export(connection, args.generation, batch_size=args.batch_size)
            else:
                manifest, rows = build_export(connection, batch_size=args.batch_size)
                if artifact_store.s3_enabled(): manifest['unmapped_archive'] = archive_unmapped(connection, manifest)
        finally: connection.close()
        manifest = save_export(manifest, rows)
        registry.register(manifest)
    if args.activate and 'id_scheme' in manifest:
        # vector_active and reference_active must switch in one transaction (reference_reload apply).
        parser.error('Activate reference-generation snapshots with `python -m reveal_backend.reference_reload apply`')
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
    if 'id_scheme' in manifest:
        connection = mysql_connection()
        try: print(canonical({'vector_bindings_inserted': record_vector_bindings(connection, registry.get(manifest['snapshot_id']))}), flush=True)
        finally: connection.close()
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
    # Source-id snapshots keep their deterministic vector ids and get this environment's generation namespaces.
    source_ids = source.get('id_scheme') == ID_SCHEME
    namespaces = generation_namespaces(identity) if source_ids else {
        'factor_namespace': environment() + '-f-' + identity[:48], 'context_namespace': environment() + '-c-' + identity[:48]}
    manifest.update(snapshot_id=identity, environment=environment(), status='loading', verified_batches={}, batches={},
        **namespaces, derived_from={'snapshot_id': source['snapshot_id'], 'export_ref': source['export_ref']})
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
    for kind in snapshot_kinds(manifest):
        for row in manifest[kind]:
            old_id = row['id']
            if not source_ids: row['id'] = digest([identity, kind, row['binding']])
            replacements[(kind, old_id)] = row
            manifest['batches'].setdefault(row['batch'], []).append(row['id'])
    source_export = _export_manifest(canonical(source['export_ref']), source['environment'])
    # The exact gate needs all serving factors and representative contexts;
    # it does not retain a full context-vector matrix in memory.
    chosen = {source['contexts'][i]['id'] for i in np.linspace(0, len(source['contexts'])-1, min(12,len(source['contexts'])), dtype=int)}
    def copy_batch(key):
        original = _export_object(source_export['batch_refs'][key], source['environment'])
        # Manifest rows override the source rows; compact gene-set rows keep their exported binding.
        batch = [{**row, **replacements[(row['kind'], row['id'])], 'vector': row['vector']} for row in original]
        reference = artifact_store.store().put(gzip.compress((canonical(batch)+'\n').encode(), mtime=0), 'application/gzip')
        baseline = [new for old, new in zip(original, batch) if old['kind'] == 'factors' or (old['kind'] == 'contexts' and old['id'] in chosen)]
        return key, reference, baseline
    refs, baseline = {}, []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, ref, rows in pool.map(copy_batch, source['batches']):
            refs[key] = ref; baseline.extend(rows)
    manifest['quality_probes'] = quality_baseline(manifest, baseline)
    root = gzip.compress((canonical({'manifest': manifest, 'batch_refs': refs})+'\n').encode(), mtime=0)
    return {**manifest, 'export_ref': artifact_store.store().put(root, 'application/gzip')}


if __name__ == '__main__': main()
