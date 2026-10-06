"""Bounded independent-evidence ingestion, separate from reference readers.

The caller authorizes finalized upload bytes and pins input contexts. No URLs
are fetched, and uploaded code is never executed.
"""
from copy import deepcopy
from pathlib import Path
import re

from .auth import Problem
from .evidence_package import canonical_json, sha256
from .repository import now, uid
from . import user_inputs


def materialize_import(filename, data, metadata=None, *, dapper=None, inputs=(), import_id=None):
    """Return a trusted context and relative byte map for a completed upload."""
    if dapper is None:
        from .acceptance import public_runtime
        dapper = public_runtime()
    metadata = deepcopy(metadata or {})
    if not isinstance(data, bytes) or not data or len(data) > user_inputs.MAX_FILE_BYTES:
        raise Problem(413, 'IMPORT_TOO_LARGE', 'Evidence imports require one to 8,000,000 bytes.')
    if (not isinstance(filename, str) or Path(filename).name != filename or '\\' in filename or
            any(ord(char) < 32 for char in filename)):
        raise Problem(422, 'INVALID_IMPORT', 'Use a plain evidence filename.')
    media = user_inputs.TYPES.get(Path(filename).suffix.lower())
    if not media:
        raise Problem(422, 'UNSUPPORTED_IMPORT', 'This evidence file format is unsupported.')
    if len(canonical_json(metadata)) > 32000:
        raise Problem(422, 'INVALID_IMPORT', 'Declared evidence metadata exceeds its bound.')
    origin = metadata.get('origin', 'externally_collected')
    if origin not in ('externally_collected', 'locally_derived'):
        raise Problem(422, 'INVALID_IMPORT', 'Choose externally_collected or locally_derived evidence.')
    try:
        extracted = user_inputs.extract(data, filename)
    except ValueError as error:
        raise Problem(422, 'IMPORT_EXTRACTION_FAILED', str(error)) from None
    identity = import_id or uid(); observed = now()
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity):
        raise Problem(422, 'INVALID_IMPORT', 'Invalid import identity.')
    input_nodes = {node['id']: node for context in inputs for rows in context.get('dapper_context', {}).values()
                   if isinstance(rows, list) for node in rows if isinstance(node, dict) and 'id' in node}
    original = dapper.file(filename, data, media)
    document = {'files': [original], 'activities': [], 'used_edges': [],
                'was_generated_by_edges': [], 'was_derived_from_edges': []}
    if origin == 'locally_derived':
        input_ids = metadata.get('input_ids')
        method = metadata.get('method')
        if (not isinstance(input_ids, list) or not input_ids or not all(isinstance(item, str) and item in input_nodes for item in input_ids)
                or not isinstance(method, str) or not method.strip()):
            raise Problem(422, 'IMPORT_DERIVATION_REQUIRED', 'A local derivation requires retained input IDs and its declared method.')
        scripts = metadata.get('script_sha256', [])
        if not isinstance(scripts, list) or not all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) for value in scripts):
            raise Problem(422, 'INVALID_IMPORT', 'Script hashes must be SHA-256 values.')
        activity = {'name': 'Unverified local derivation', 'description': method,
                    'command': canonical_json({'declared_method': method, 'software': metadata.get('software'),
                        'parameters': metadata.get('parameters'), 'script_sha256': scripts, 'import_id': identity}).decode().strip()}
        activity['id'] = dapper.compute_id(activity, 'Activity', dapper.schema)
        document['activities'].append(activity)
        document['used_edges'].extend({'subject': activity['id'], 'predicate': 'prov:used', 'object': item} for item in sorted(set(input_ids)))
        document['was_generated_by_edges'].append({'subject': original['id'], 'predicate': 'prov:wasGeneratedBy', 'object': activity['id']})
        document['was_derived_from_edges'].extend({'subject': original['id'], 'predicate': 'prov:wasDerivedFrom', 'object': item}
                                                  for item in sorted(set(input_ids)))
    ingestion = {'name': 'REVEAL independent evidence extraction', 'command': 'reveal-import '+identity,
                 'software_name': extracted['extractor']['name'], 'software_version': extracted['extractor']['version'],
                 'generated_at_time': observed}
    ingestion['id'] = dapper.compute_id(ingestion, 'Activity', dapper.schema)
    document['activities'].append(ingestion)
    document['used_edges'].append({'subject': ingestion['id'], 'predicate': 'prov:used', 'object': original['id']})
    raw = canonical_json(extracted)
    derived = dapper.file('extraction.json', raw, 'application/json')
    document['files'].append(derived)
    document['was_generated_by_edges'].append({'subject': derived['id'], 'predicate': 'prov:wasGeneratedBy', 'object': ingestion['id']})
    document['was_derived_from_edges'].append({'subject': derived['id'], 'predicate': 'prov:wasDerivedFrom', 'object': original['id']})
    originals = f'imports/{identity}/{sha256(data)}{Path(filename).suffix.lower()}'
    extraction = f'imports/{identity}/{sha256(raw)}.json'
    artifacts = {
        'import:'+identity+':original': {'path': originals, 'filename': filename, 'format': 'binary',
            'origin': 'reveal-import:'+identity, 'sha256': sha256(data), 'size_bytes': len(data), 'dapper_file_id': original['id']},
        'import:'+identity+':extraction': {'path': extraction, 'filename': 'extraction.json', 'format': 'json',
            'origin': 'reveal-extraction:'+identity, 'sha256': sha256(raw), 'size_bytes': len(raw), 'dapper_file_id': derived['id']}}
    return {'format': 'reveal.evidence-import/1', 'id': identity, 'status': 'completed', 'received_at': observed,
            'origin': origin, 'declared_metadata': metadata, 'verification': 'integrity_checked_execution_unverified',
            'dapper_context': document, 'source_artifacts': artifacts,
            'eligible_source_ids': [original['id'], derived['id']], 'cfde_source_ids': [],
            'files': {originals: data, extraction: raw}, 'user_inputs': {'uploads': [
                {'id': identity, 'sha256': sha256(data), 'original_file_id': original['id'],
                 'extraction_file_id': derived['id']}]}}
