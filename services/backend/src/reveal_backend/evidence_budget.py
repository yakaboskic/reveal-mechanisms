"""Freeze a measured, bounded dispatch package from an unchanged source capture."""
from copy import deepcopy
import fcntl
import math
from pathlib import Path
import tempfile

import httpx

from .evidence_package import DapperRuntime, build_package, canonical_json, decode, load_build_input, require, sha256
from .evidence_schema import load_generated_schema, validate_package_shape
from .runtime_config import ROOT, setting


def count_tokens(data: bytes, model: str) -> int:
    key = setting('ANTHROPIC_API_KEY')
    require(bool(key), 'ANTHROPIC_API_KEY is required for measured input budget')
    result = httpx.post('https://api.anthropic.com/v1/messages/count_tokens',
                       headers={'x-api-key': key, 'anthropic-version': '2023-06-01'},
                       json={'model': model, 'messages': [{'role': 'user', 'content': data.decode()}]}, timeout=60)
    require(result.status_code == 200, 'Anthropic input token measurement failed')
    count = result.json().get('input_tokens')
    require(type(count) is int and count > 0, 'Invalid model token count')
    return count


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.dispatch-', delete=False) as output:
        temporary = Path(output.name)
        output.write(canonical_json(value))
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fit_input_budget(package_path, mode, budget):
    """Return (immutable chosen path, package, measurements) before paid execution.

    Preserve all selected anchors and exact captured sources. If needed, retain
    fewer existing ranked candidates and their GeneSet projections, rebuilding
    through the original deterministic validator. Raw captures are never edited,
    rerequested, or summarized by a model. The package reports every omission.
    """
    package_path = Path(package_path).resolve()
    # The worker lease fences database publication, but an expired worker can
    # still finish its collection thread. Serialize the complete filesystem
    # freeze so another process reuses the winner's measured input unchanged.
    # Separate opens also serialize threads; the OS releases flock on exit.
    lock_path = package_path.parent.parent / '.dispatch-input.lock'
    with lock_path.open('a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _fit_input_budget_locked(package_path, mode, budget)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _fit_input_budget_locked(package_path, mode, budget):
    root = package_path.parent.parent
    require(mode in ('box', 'deterministic'), 'Unknown evidence execution mode')
    require(type(budget) is int and budget > 0, 'Evidence token budget must be positive')
    original_data = package_path.read_bytes()
    original = decode(original_data)
    model = setting('REVEAL_CLAUDE_MODEL', 'claude-sonnet-4-6')
    binding = {'original_sha256': sha256(original_data), 'mode': mode, 'model': model, 'budget': budget}
    cache = root / 'dispatch-input.json'

    def cached():
        saved = decode(cache.read_bytes())
        require(saved['binding'] == binding, 'Frozen dispatch input belongs to another package, model or budget')
        chosen = (root / saved['path']).resolve()
        require(chosen.is_relative_to(root), 'Frozen dispatch path escapes source capture')
        raw = chosen.read_bytes()
        require(sha256(raw) == saved['sha256'], 'Frozen dispatch input checksum changed')
        if mode == 'box':
            require(saved['measurement']['enforced'] and saved['measurement']['count'] <= budget,
                    'Frozen dispatch input exceeds its measured budget')
        return chosen, decode(raw), saved['measurement']

    if cache.exists():
        return cached()
    measurement = {'method': 'anthropic-count-tokens' if mode == 'box' else 'development-byte-upper-bound',
                   'budget': budget, 'enforced': mode == 'box', 'model': model, 'attempts': []}
    chosen, data, package, built = package_path, original_data, original, None
    spec = blobs = runtime = None
    ranking = sorted(original['pigean']['candidates'],
                     key=lambda identity: (-original['pigean']['candidates'][identity]['candidate']['aggregate_score'], identity))
    anchors = original['selection']['eaggl_mechanism_ids']
    candidate_count = len(ranking)
    for _ in range(10):
        count = count_tokens(data, model) if mode == 'box' else len(data)
        measurement['attempts'].append({'input_tokens': count, 'package_sha256': sha256(data),
                                        'retained_nodes': package['coverage']['retained_nodes'],
                                        'retained_edges': package['coverage']['retained_edges']})
        if mode == 'deterministic' or count <= budget:
            measurement.update(count=count, reduced=built is not None,
                               original_package_sha256=binding['original_sha256'], package_sha256=sha256(data))
            validate_package_shape(package, load_generated_schema(ROOT / 'schema/evidence-package.schema.json'))
            if built is not None:
                chosen = root / 'dispatch/package/evidence-package.json'
                built.write(chosen.parent)
            atomic_json(cache, {'format': 'reveal.dispatch-input/1', 'binding': binding,
                                'path': str(chosen.relative_to(root)), 'sha256': sha256(data),
                                'measurement': measurement})
            return chosen, package, measurement
        if candidate_count == 0:
            atomic_json(root / 'dispatch-budget-failure.json', measurement)
            require(False, f'Even the selected anchors and source context exceed the evidence token budget ({count} > {budget})')
        if spec is None:
            spec, blobs = load_build_input(root / 'build-input.json', root)
            runtime = DapperRuntime(ROOT / 'data/dapper/2026-09-24-v8')
        # Account for fixed provenance/context overhead; measured verification,
        # rather than this estimate, decides whether a package may be dispatched.
        candidate_count = max(0, min(candidate_count - 1, math.floor(candidate_count * budget / count * 0.65)))
        retained = set(anchors) | set(ranking[:candidate_count])
        reduced = deepcopy(spec)
        reduced['policy'].update(retain_node_ids=sorted(retained), max_nodes=len(retained),
                                 max_edges=min(spec['policy']['max_edges'], max(1, len(retained) * 4)))
        kept_sets = {identity: row for identity, row in spec['gene_sets'].items() if identity in retained}
        kept_ids = {row['dapper_id'] for row in kept_sets.values()}
        reduced['gene_sets'] = kept_sets
        reduced['dapper_context']['gene_sets'] = [node for node in spec['dapper_context'].get('gene_sets', []) if node['id'] in kept_ids]
        built = build_package(reduced, blobs, runtime)
        data, package = built.files['evidence-package.json'], built.package
    atomic_json(root / 'dispatch-budget-failure.json', measurement)
    require(False, 'Could not fit captured evidence within the measured input budget')
