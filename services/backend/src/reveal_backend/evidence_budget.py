"""Freeze complete evidence files for on-demand reading inside the Box.

The historical public function name remains for worker/recovery compatibility.
A stored file is not an inline prompt: no provider token-count call or candidate
pruning is performed for new captures. Source collection and bundle byte limits
continue to apply independently of runtime cost, duration and tool limits.
"""
import fcntl
from pathlib import Path
import tempfile

from .evidence_package import canonical_json, decode, require, sha256
from .dispatch_view import (FILE_INPUT_FILENAME, file_input_manifest, legacy_research_prompt,
                            research_prompt, validate_dispatch_budget, validate_file_input)
from .evidence_schema import load_generated_schema, validate_package_shape
from .runtime_config import ROOT, setting


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
    """Return the full immutable file and its preparation record without I/O to a model.

    Existing frozen captures keep their exact bytes and historical measurement;
    new inputs use the complete package, including every retained candidate.
    The legacy budget argument is recorded for request compatibility, not used
    to treat file bytes as initial model context.
    """
    package_path = Path(package_path).resolve()
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

    def contained(relative):
        path = (root / relative).resolve()
        require(path.is_relative_to(root), 'Frozen dispatch path escapes source capture')
        return path

    if cache.exists():
        saved = decode(cache.read_bytes())
        require(saved['format'] in ('reveal.dispatch-input/1', 'reveal.dispatch-input/2'), 'Unknown frozen input format')
        require(saved['binding'] == binding, 'Frozen dispatch input belongs to another package, model or request')
        chosen = contained(saved['path'])
        raw = chosen.read_bytes()
        require(sha256(raw) == saved['sha256'], 'Frozen dispatch input checksum changed')
        package = decode(raw)
        if saved['format'] == 'reveal.dispatch-input/2':
            reference = saved['file_input']
            metadata = contained(reference['path']).read_bytes()
            require(sha256(metadata) == reference['sha256'], 'Frozen evidence input manifest changed')
            from .dispatch_view import pinned_contract_sha256, pinned_skeleton_sha256
            validate_file_input(raw, decode(metadata), research_prompt(package['external_evidence']['selected_graphs'], validation_feedback, progressive=package.get('retrieval_mode') == 'progressive',
                                contract_sha256=pinned_contract_sha256(package), skeleton_sha256=pinned_skeleton_sha256(package)))
            return chosen, package, saved['measurement']
        # Honor older jobs' exact captures without relabeling their token counts
        # as measurements of the new file reader or recollecting source evidence.
        if mode == 'box':
            require(saved['measurement']['enforced'] and saved['measurement']['count'] <= budget,
                    'Legacy frozen dispatch input exceeded its measured budget')
        if 'dispatch_view' in saved:
            reference = saved['dispatch_view']
            view = contained(reference['path']).read_bytes()
            metadata = contained(reference['budget_path']).read_bytes()
            require(sha256(view) == reference['sha256'] and sha256(metadata) == reference['budget_sha256'],
                    'Legacy frozen dispatch view or measurement changed')
            validate_dispatch_budget(raw, view, decode(metadata),
                legacy_research_prompt(package['external_evidence']['selected_graphs'], validation_feedback), model)
            require(decode(metadata)['measurement'] == saved['measurement'], 'Legacy frozen dispatch measurements differ')
        else:
            require(not validation_feedback, 'Legacy frozen input needs fresh preparation for review feedback')
        return chosen, package, {**saved['measurement'], 'scope': 'legacy frozen input; file-reader context was not measured'}

    if original.get('retrieval_mode') == 'progressive':
        from .research_seed import validate_seed_shape
        validate_seed_shape(original)
    else:
        validate_package_shape(original, load_generated_schema(ROOT / 'schema/evidence-package.schema.json'))
    metadata = file_input_manifest(original_data, validation_feedback)
    measurement = {'method': 'file-backed-evidence', 'enforced': False,
                   'scope': 'artifact storage; evidence read on demand', 'model': model,
                   'requested_evidence_tokens': budget, 'package_bytes': len(original_data),
                   'package_sha256': sha256(original_data), 'reduced': False}
    # Publish outside the builder-owned exact file set. Repeating after an
    # interrupted manifest write preserves every original package/source byte.
    atomic_json(root / FILE_INPUT_FILENAME, metadata)
    atomic_json(cache, {'format': 'reveal.dispatch-input/2', 'binding': binding,
                       'path': str(package_path.relative_to(root)), 'sha256': sha256(original_data),
                       'file_input': {'path': FILE_INPUT_FILENAME, 'sha256': sha256(canonical_json(metadata))},
                       'measurement': measurement})
    return package_path, original, measurement
