"""Checksum-bound, additive recovery of historical collection provenance.

Scientific rows and generation manifests are never rewritten. Only the catalog-owned
registry and content-addressed artifact are added, after verifying the original source.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
import re

from . import artifact_store
from .artifact_store import StorageUnavailable
from .reference_generation import CATALOG_OWNER
from .repository import canonical, digest, now
from .runtime_config import artifacts_root

KIND = 'provenance_supplement'
EXTRACTOR_VERSION = 'reveal.collection-provenance/1'
FORMAT = 'reveal.provenance-supplement/1'
MAX_GRAPH_BYTES = 32 << 20


class RecoveryRefused(ValueError):
    pass


def supplement_id(generation_id, collection_id, source_document_sha256):
    return digest({'generation_id': generation_id, 'collection_id': collection_id,
        'source_document_sha256': source_document_sha256, 'extractor_version': EXTRACTOR_VERSION})


def _storage_read(storage):
    try:
        if storage.get('store') == 's3': return artifact_store.store().get(storage)
        path = Path(storage['key']).resolve()
        base = (artifacts_root() / 'provenance-supplements').resolve()
        if (storage.get('store') != 'filesystem' or path.parent != base or
                path.name != storage['sha256'] or not 0 <= storage['size_bytes'] <= MAX_GRAPH_BYTES):
            raise StorageUnavailable('Invalid provenance supplement storage reference')
        with path.open('rb') as stream: data = stream.read(storage['size_bytes'] + 1)
        if len(data) != storage['size_bytes'] or hashlib.sha256(data).hexdigest() != storage['sha256']:
            raise StorageUnavailable('Provenance supplement checksum mismatch')
        return data
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise StorageUnavailable('Provenance supplement artifact is unavailable') from error


def _storage_write(data):
    if artifact_store.s3_enabled(): return artifact_store.store().put(data, 'application/json')
    sha = hashlib.sha256(data).hexdigest()
    path = artifacts_root() / 'provenance-supplements' / sha
    path.parent.mkdir(parents=True, exist_ok=True)
    ref = {'store': 'filesystem', 'key': str(path), 'sha256': sha, 'size_bytes': len(data), 'content_type': 'application/json'}
    temporary = None
    try:
        # Finish and sync bytes before atomically publishing the content address.
        # link() is exclusive: another writer can win, but can never be replaced.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.recover-', delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        except FileExistsError:
            if _storage_read(ref) != data: raise RecoveryRefused('Conflicting immutable provenance artifact')
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
    return ref


def load_supplement(tx, generation_id, collection_id, source_document_sha256):
    """Return {graph, registry} or None; unavailable registered bytes are an error."""
    identity = supplement_id(generation_id, collection_id, source_document_sha256)
    row = tx.get(KIND, identity)
    if row is None: return None
    value = row['data']
    expected = {'id': identity, 'format': FORMAT, 'generation_id': generation_id, 'collection_id': collection_id,
        'source_document_sha256': source_document_sha256, 'extractor_version': EXTRACTOR_VERSION}
    if row['owner'] != CATALOG_OWNER or any(value.get(k) != v for k, v in expected.items()):
        raise StorageUnavailable('Invalid provenance supplement registry')
    if not isinstance(value.get('storage'), dict) or value.get('graph_sha256') != value['storage'].get('sha256'):
        raise StorageUnavailable('Invalid provenance supplement checksum binding')
    data = _storage_read(value['storage'])
    try: graph = json.loads(data)
    except (ValueError, UnicodeError) as error: raise StorageUnavailable('Invalid provenance supplement graph') from error
    if not isinstance(graph, dict): raise StorageUnavailable('Invalid provenance supplement graph')
    return {'graph': graph, 'registry': value}


def _source_binding(tx, generation_id, collection_id, sha):
    result = tx.execute('SELECT manifest FROM reference_generations WHERE generation_id=%s', (generation_id,)).fetchone()
    if result is None: raise RecoveryRefused('Reference generation is unavailable')
    manifest = json.loads(result[0]) if isinstance(result[0], (str, bytes)) else result[0]
    result = tx.execute('SELECT cfde_label,payload FROM cfde_gene_set_collections WHERE generation_id=%s AND collection_id=%s',
                        (generation_id, collection_id)).fetchone()
    if result is None: raise RecoveryRefused('Stored collection is unavailable')
    label, payload = result
    payload = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    if not isinstance(payload, dict) or payload.get('document_sha256') != sha:
        raise RecoveryRefused('Original source checksum differs from stored collection')
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('collections'), dict)
            or manifest['collections'].get(label) != sha):
        raise RecoveryRefused('Original source checksum differs from generation manifest')
    return digest({'manifest': manifest, 'collection': payload, 'label': label})


def recover_collection_provenance(repository, generation_id, source_path, *, apply=False):
    """Default dry run; apply adds one immutable supplement and never changes source rows."""
    from .reference_reload import collection_header, _sha256_file
    from .provenance_schema import PROVENANCE_GROUPS, validate_provenance_edges
    if not isinstance(generation_id, str) or not re.fullmatch(r'[a-f0-9]{64}', generation_id):
        raise RecoveryRefused('Invalid reference generation id')
    path = Path(source_path)
    source_sha = _sha256_file(path)
    header, collection = collection_header(path)
    collection_id = collection.get('id')
    if not isinstance(collection_id, str) or not re.fullmatch(r'dapper:GeneSetCollection\.[A-Za-z0-9_-]{32}', collection_id):
        raise RecoveryRefused('Invalid source collection id')
    if _sha256_file(path) != source_sha: raise RecoveryRefused('Original source changed during extraction')
    graph = {group: header[group] for group in PROVENANCE_GROUPS if header.get(group) is not None}
    validate_provenance_edges(graph)
    data = canonical(graph).encode('utf-8')
    if len(data) > MAX_GRAPH_BYTES: raise RecoveryRefused('Recovered provenance exceeds the artifact bound')
    graph_sha = hashlib.sha256(data).hexdigest()
    identity = supplement_id(generation_id, collection_id, source_sha)
    with repository.read_transaction() as tx:
        binding = _source_binding(tx, generation_id, collection_id, source_sha)
        existing = load_supplement(tx, generation_id, collection_id, source_sha)
    if existing and existing['registry']['graph_sha256'] != graph_sha:
        raise RecoveryRefused('Conflicting registered provenance supplement')
    result = {'id': identity, 'generation_id': generation_id, 'collection_id': collection_id,
        'source_document_sha256': source_sha, 'extractor_version': EXTRACTOR_VERSION,
        'graph_sha256': graph_sha, 'counts': {group: len(rows) for group, rows in graph.items() if group != 'prefixes'},
        'applied': False, 'reused': bool(existing)}
    if not apply or existing: return {**result, 'applied': bool(apply and existing)}
    # Artifact network I/O stays outside the short catalog write transaction. A failed
    # registration can leave only an unreferenced, content-addressed artifact.
    storage = _storage_write(data)
    with repository.transaction() as tx:
        if _source_binding(tx, generation_id, collection_id, source_sha) != binding:
            raise RecoveryRefused('Reference source observations changed during recovery')
        prior = tx.get(KIND, identity)
        if prior:
            if prior['owner'] != CATALOG_OWNER or prior['data'].get('graph_sha256') != graph_sha:
                raise RecoveryRefused('Conflicting registered provenance supplement')
        else:
            tx.insert(KIND, identity, CATALOG_OWNER, {**result, 'format': FORMAT,
                'storage': storage, 'registered_at': now(), 'applied': True})
    return {**result, 'applied': True, 'reused': bool(prior)}
