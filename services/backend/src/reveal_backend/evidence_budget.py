"""Freeze a measured, bounded dispatch package from an unchanged source capture."""
from copy import deepcopy
import fcntl
import math
from pathlib import Path
import tempfile

import httpx

from .evidence_package import DapperRuntime, build_package, canonical_json, decode, load_build_input, require, sha256
from .dispatch_view import (BUDGET_FILENAME, BUDGET_SCOPE, VIEW_FILENAME, VIEW_FORMAT, dispatch_view,
                            measured_input, research_prompt, validate_dispatch_budget)
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
    atomic_bytes(path, canonical_json(value))


def atomic_bytes(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.dispatch-', delete=False) as output:
        temporary = Path(output.name)
        output.write(value)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fit_input_budget(package_path, mode, budget, validation_feedback=()):
    """Return (immutable chosen path, package, measurements) before paid execution.

    Measure the exact initial reading view plus shared research instructions.
    The canonical package and all source files remain complete. If needed, retain
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
            return _fit_input_budget_locked(package_path, mode, budget, validation_feedback)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _fit_input_budget_locked(package_path, mode, budget, validation_feedback=()):
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
        if 'dispatch_view' in saved:
            view_binding = saved['dispatch_view']
            view_path = (root / view_binding['path']).resolve()
            budget_path = (root / view_binding['budget_path']).resolve()
            require(view_path.is_relative_to(root) and budget_path.is_relative_to(root),
                    'Frozen dispatch sidecar path escapes source capture')
            require(sha256(view_path.read_bytes()) == view_binding['sha256'] and
                    sha256(budget_path.read_bytes()) == view_binding['budget_sha256'],
                    'Frozen dispatch view or measurement changed')
            prompt = research_prompt(decode(raw)['external_evidence']['selected_graphs'], validation_feedback)
            validate_dispatch_budget(raw, view_path.read_bytes(), decode(budget_path.read_bytes()), prompt, model)
            require(decode(budget_path.read_bytes())['measurement'] == saved['measurement'],
                    'Frozen dispatch measurements differ')
        elif mode == 'box':
            # Historical captures measured only the canonical package, not the
            # new initial prompt/view. Preserve their frozen bytes and describe
            # that scope honestly; do not relabel the old count as a view count.
            require(not validation_feedback, 'Legacy frozen input needs fresh preparation to budget review feedback')
            return chosen, decode(raw), {**saved['measurement'],
                'scope': 'legacy canonical package only; research prompt and dispatch view were not measured'}
        return chosen, decode(raw), saved['measurement']

    if cache.exists():
        return cached()
    measurement = {'method': 'anthropic-count-tokens' if mode == 'box' else 'development-byte-upper-bound',
                   'budget': budget, 'enforced': mode == 'box', 'model': model, 'attempts': []}
    if mode == 'box':
        measurement.update(scope=BUDGET_SCOPE, view_format=VIEW_FORMAT)
    chosen, data, package, built = package_path, original_data, original, None
    spec = blobs = runtime = None
    ranking = sorted(original['pigean']['candidates'],
                     key=lambda identity: (-original['pigean']['candidates'][identity]['candidate']['aggregate_score'], identity))
    anchors = original['selection']['eaggl_mechanism_ids']
    candidate_count = len(ranking)
    for _ in range(10):
        initial = measured_input(data, validation_feedback) if mode == 'box' else data
        count = count_tokens(initial, model) if mode == 'box' else len(data)
        measurement['attempts'].append({'input_tokens': count, 'package_sha256': sha256(data),
                                        'input_sha256': sha256(initial),
                                        'retained_nodes': package['coverage']['retained_nodes'],
                                        'retained_edges': package['coverage']['retained_edges']})
        if mode == 'deterministic' or count <= budget:
            measurement.update(count=count, reduced=built is not None,
                               original_package_sha256=binding['original_sha256'], package_sha256=sha256(data),
                               input_sha256=sha256(initial))
            validate_package_shape(package, load_generated_schema(ROOT / 'schema/evidence-package.schema.json'))
            if built is not None:
                chosen = root / 'dispatch/package/evidence-package.json'
                built.write(chosen.parent)
            frozen = {'format': 'reveal.dispatch-input/1', 'binding': binding,
                                'path': str(chosen.relative_to(root)), 'sha256': sha256(data),
                                'measurement': measurement}
            if mode == 'box':
                view = dispatch_view(data)
                prompt = research_prompt(package['external_evidence']['selected_graphs'], validation_feedback)
                view_budget = {'format': 'reveal.dispatch-budget/1', 'view_format': VIEW_FORMAT,
                               'package_sha256': sha256(data), 'view_sha256': sha256(view),
                               'prompt_sha256': sha256(prompt.encode()), 'measurement': measurement}
                validate_dispatch_budget(data, view, view_budget, prompt, model)
                # Keep sidecars outside the exact-file-set builder directory.
                # A crash before manifest publication can then replay the
                # unchanged built package and publish its sidecars again.
                atomic_json(root / BUDGET_FILENAME, view_budget)
                atomic_bytes(root / VIEW_FILENAME, view)
                frozen['dispatch_view'] = {'format': VIEW_FORMAT, 'sha256': sha256(view),
                                          'path': VIEW_FILENAME, 'budget_path': BUDGET_FILENAME,
                                          'budget_sha256': sha256(canonical_json(view_budget))}
            atomic_json(cache, frozen)
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
