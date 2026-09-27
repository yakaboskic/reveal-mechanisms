"""Optional, offline SentenceTransformers runner for an explicit DisMech import.

Torch and SentenceTransformers remain optional tools in an isolated environment.
The fixed remote reference bundle is operator-attested, not proof of origin by
itself. Its explicitly pinned digest prevents a later mixed resume from silently
calibrating this implementation against its own newly generated vectors.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sqlite3

import numpy as np

from .dismech_embeddings import CALIBRATION_MIN_COSINE, calibrate, open_capture
from .dismech_import import canonical, digest, require
from .eaggl_embeddings import validate_vectors, vector_hash


def read_references(output, reference_path, reference_sha256, attestation):
    """Validate frozen references without creating or updating capture files."""
    output = Path(output)
    manifest, inputs = open_capture(output)
    raw = Path(reference_path).read_bytes()
    require(digest(raw) == reference_sha256, 'Fixed remote reference bundle checksum mismatch')
    bundle = json.loads(raw)
    require(bundle.get('run_id') == manifest['run_id']
            and bundle.get('capture_manifest_sha256') == digest((output / 'manifest.json').read_bytes())
            and bundle.get('target_eaggl_run_id') == manifest['target_run']['run_id']
            and bundle.get('dimensions') == manifest['dimensions'], 'Remote reference bundle belongs to another capture/space')
    config = manifest['target_run']['config']
    origin = bundle.get('origin', {})
    require(isinstance(attestation, dict) and attestation.get('execution_backend') == 'remote_embedding_service'
            and attestation.get('model') == config['model'] and attestation.get('service_url') == config['service_url']
            and origin.get('kind') == 'retrospective_remote_attestation'
            and origin.get('attestation') == attestation, 'Explicit matching remote generation attestation required')
    references = bundle.get('references')
    texts = {row['input_sha256']: row['input_text'] for row in inputs}
    require(isinstance(references, list) and bool(references) and len(references) >= min(32, len(texts)),
            'Remote reference bundle requires at least 32 distinct contexts (or the entire smaller inventory)')
    seen = set()
    connection = sqlite3.connect((output / 'embeddings.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    try:
        stored_run = connection.execute("SELECT value FROM metadata WHERE key='run_id'").fetchone()
        require(stored_run == (manifest['run_id'],), 'Remote reference cache belongs to another run')
        for reference in references:
            sha, text = reference['input_sha256'], reference['input_text']
            require(sha not in seen and digest(text) == sha and texts.get(sha) == text,
                    'Remote reference input is duplicate, changed, or absent from source inventory')
            seen.add(sha)
            vector = validate_vectors([reference['vector']], 1, manifest['dimensions'])[0]
            require(vector_hash(vector.tobytes()) == reference['vector_sha256'], 'Remote reference vector checksum mismatch')
            row = connection.execute('SELECT input_text,vector,vector_sha256 FROM vectors WHERE input_sha256=?', (sha,)).fetchone()
            require(row == (text, vector.tobytes(), reference['vector_sha256']),
                    'Remote reference differs from the preserved checkpoint')
    finally:
        connection.close()
    return manifest, references


def _snapshot_files(snapshot):
    files = []
    for path in sorted(snapshot.rglob('*')):
        if not path.is_file():
            continue
        checksum = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                checksum.update(chunk)
        files.append({'path': path.relative_to(snapshot).as_posix(), 'bytes': path.stat().st_size,
                      'sha256': checksum.hexdigest()})
    require(bool(files), 'Pinned model snapshot is empty')
    return files


def _load_model(model_id, revision, cache_dir, device):
    require(re.fullmatch(r'[0-9a-f]{40}', revision) is not None, 'Local model revision must be an exact HF commit')
    require(device in ('mps', 'cpu'), 'Local device must be explicitly mps or cpu')
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', model_id) is not None, 'Expected a public HF model repository')
    snapshot = Path(cache_dir).resolve() / ('models--' + model_id.replace('/', '--')) / 'snapshots' / revision
    require(snapshot.is_dir(), 'Exact model snapshot is not cached; the local runner never downloads models')
    files = _snapshot_files(snapshot)
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    import torch
    from sentence_transformers import SentenceTransformer

    require(device != 'mps' or torch.backends.mps.is_available(), 'Requested MPS device is unavailable')
    model = SentenceTransformer(str(snapshot), device=device, token=False,
                                trust_remote_code=False, local_files_only=True)
    model.float()
    model.eval()
    require(next(model.parameters()).dtype == torch.float32, 'Local model must use float32')
    require(str(model.device).split(':')[0] == device, 'Local model loaded on an unexpected device')
    tokenizer = model.tokenizer
    pooling = [module.get_config_dict() for module in model if type(module).__name__ == 'Pooling']
    metadata = {'backend': 'local_sentence_transformers', 'model': model_id, 'resolved_hf_commit': revision,
        'device': str(model.device), 'dtype': str(next(model.parameters()).dtype),
        'packages': {name: importlib.metadata.version(name) for name in
                     ('torch', 'sentence-transformers', 'transformers', 'huggingface-hub', 'numpy', 'tokenizers')},
        'encoding': {'normalize_embeddings': False, 'precision': 'float32', 'convert_to_numpy': True,
                     'maximum_sequence_length': model.max_seq_length, 'truncation': 'longest_first',
                     'tokenizer_class': type(tokenizer).__name__, 'tokenizer_model_max_length': tokenizer.model_max_length,
                     'tokenizer_truncation_side': tokenizer.truncation_side, 'pooling': pooling},
        'snapshot_files': files, 'snapshot_inventory_sha256': digest(canonical(files)),
        'network': 'offline; cached exact commit; implicit HF credentials disabled'}
    return model, metadata


class LocalEmbedder:
    """A validated callable with the same keyword contract as get_embeddings."""

    def __init__(self, model, target, metadata):
        self.model = model
        self.target = target
        self.generation_metadata = metadata

    def __call__(self, texts, *, model, provider, service_url, batch_size=32, max_workers=1, **_):
        config = self.target['config']
        require(model == config['model'] and provider == config['provider'] and service_url == config['service_url'],
                'Local embedding request differs from the pinned compatibility target')
        require(max_workers == 1, 'Local embedding uses one inference worker')
        require(isinstance(texts, list) and all(isinstance(text, str) and text.strip() for text in texts),
                'Local inputs must be nonempty exact text strings')
        vectors = self.model.encode(texts, batch_size=batch_size, normalize_embeddings=False,
                                    precision='float32', convert_to_numpy=True, show_progress_bar=False)
        return validate_vectors(vectors, len(texts), self.target['dimensions'])


def prepare_local_embedder(output, *, reference_path, reference_sha256, attestation, revision, cache_dir, device):
    """Finish all validation/calibration before the caller enters embed_capture."""
    manifest, references = read_references(output, reference_path, reference_sha256, attestation)
    target = manifest['target_run']
    require(target['config']['provider'] == 'huggingface', 'Local runner requires a Hugging Face target')
    model, metadata = _load_model(target['config']['model'], revision, cache_dir, device)
    runner = LocalEmbedder(model, target, metadata)
    eaggl = calibrate(target, embedder=runner, max_retries=0)
    config = target['config']
    fresh = runner([reference['input_text'] for reference in references], model=config['model'],
                   provider=config['provider'], service_url=config['service_url']).astype(np.float64)
    stored = np.asarray([reference['vector'] for reference in references], dtype=np.float64)
    similarities = np.sum(fresh * stored, axis=1) / (np.linalg.norm(fresh, axis=1) * np.linalg.norm(stored, axis=1))
    require(bool(np.all(similarities >= CALIBRATION_MIN_COSINE)), 'Local inference differs from frozen remote DisMech vectors')
    metadata.update(target_eaggl_run_id=target['run_id'], reference_bundle_sha256=reference_sha256,
        retrospective_attestation_sha256=digest(canonical(attestation)),
        calibration={'eaggl': eaggl, 'dismech': {'count': len(references),
            'minimum_required_cosine': CALIBRATION_MIN_COSINE, 'minimum_observed_cosine': float(np.min(similarities)),
            'input_sha256': [reference['input_sha256'] for reference in references],
            'limitation': 'Fixed public references have retrospective remote attribution; calibration is a sample compatibility check.'}})
    return runner
