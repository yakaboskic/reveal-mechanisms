#!/usr/bin/env python3
"""Inventory DisMech discussions and resolve knowledge-gap attachments.

Uses DisMech's own reference grammar; writes only to this project's output.
Knowledge-gap records remain source questions, never generated scientific claims.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import yaml

LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
GAP_KINDS = {'KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH'}


def walk(value, pointer=''):
    yield pointer, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, pointer + '/' + str(key).replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk(child, pointer + '/' + str(i))


def extract(source, output):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source / 'src'))
    from dismech.entity_refs import parse_entity_ref, resolve_entity_ref, entity_ref_index, canonical_kind
    output.mkdir(parents=True, exist_ok=True)
    files = sorted((source / 'kb').rglob('*.yaml'))
    by_stem = defaultdict(list)
    for file in files:
        by_stem[file.stem].append(file)
    rows, attachments, hashes, errors = [], [], [], []

    def resolve(file, document, reference):
        out = {'source_reference': reference}
        parsed = parse_entity_ref(reference)
        if parsed is None:
            return {**out, 'resolution': 'invalid_syntax'}
        target_file, target_document = file, document
        if parsed.file:
            requested = parsed.file.removesuffix('.yaml')
            choices = [p for p in files if str(p.relative_to(source / 'kb').with_suffix('')) == requested]
            if not choices:
                choices = by_stem.get(requested, [])
            if len(choices) != 1:
                return {**out, 'resolution': 'ambiguous_file' if choices else 'missing_file'}
            target_file = choices[0]
            target_document = yaml.load(target_file.read_bytes(), Loader=LOADER)
        local_ref = parsed.kind + '#' + parsed.name
        exists = resolve_entity_ref(target_document, local_ref)
        if exists is not True:
            return {**out, 'resolution': 'missing_target' if exists is False else 'unsupported_kind'}
        relative = str(target_file.relative_to(source))
        document_id = 'dismech:' + str(target_file.relative_to(source / 'kb').with_suffix(''))
        out.update(target_document_id=document_id, target_source_file=relative, target_kind=canonical_kind(parsed.kind))
        if not parsed.name:
            pointer = '' if parsed.kind == 'disease' else '/' + canonical_kind(parsed.kind)
            return {**out, 'resolution': 'whole_document' if not pointer else 'whole_section',
                    'target_id': document_id + ('#' + pointer if pointer else ''), 'target_pointer': pointer,
                    'section_has_content': bool(target_document.get(canonical_kind(parsed.kind))) if pointer else True}
        matches = entity_ref_index(target_document).get(local_ref, [])
        # Multiple source items with the same key need review, never pick one silently.
        unique = {id(item): item for item in matches}
        if len(unique) != 1:
            return {**out, 'resolution': 'ambiguous_target', 'match_count': len(unique)}
        item = next(iter(unique.values()))
        pointer = next(ptr for ptr, value in walk(target_document) if value is item)
        return {**out, 'resolution': 'resolved', 'target_pointer': pointer,
                'target_id': document_id + ('#' + pointer if pointer else ''),
                'target_label': item.get('name') or parsed.name}

    for n, file in enumerate(files, 1):
        raw = file.read_bytes()
        relative = str(file.relative_to(source))
        hashes.append({'path': relative, 'sha256': hashlib.sha256(raw).hexdigest()})
        try:
            doc = yaml.load(raw, Loader=LOADER)
            if not isinstance(doc, dict):
                raise ValueError('Expected mapping')
            document_id = 'dismech:' + str(file.relative_to(source / 'kb').with_suffix(''))
            for pointer, value in walk(doc):
                if not pointer.endswith('/discussions') or not isinstance(value, list):
                    continue
                for i, discussion in enumerate(value):
                    if not isinstance(discussion, dict):
                        raise ValueError(f'Invalid discussion at {pointer}/{i}')
                    locator = f'{pointer}/{i}'
                    local_id = discussion.get('discussion_id')
                    record_id = document_id + '#discussion:' + (local_id or locator)
                    row = {'id': record_id, 'discussion_id': local_id, 'source_file': relative,
                           'source_pointer': locator, 'document_id': document_id,
                           'document_name': doc.get('name', file.stem), 'disease_term': doc.get('disease_term'),
                           'source_kind': file.relative_to(source / 'kb').parts[0],
                           'kind': discussion.get('kind'), 'status': discussion.get('status'),
                           'is_gap': discussion.get('kind') in GAP_KINDS,
                           'has_stable_source_id': bool(local_id), 'raw': discussion}
                    rows.append(row)
                    if row['is_gap']:
                        for index, ref in enumerate(discussion.get('attaches_to') or []):
                            attachments.append({'gap_id': record_id, 'attachment_index': index,
                                                **resolve(file, doc, ref)})
        except Exception as exc:
            errors.append({'source_file': relative, 'error': str(exc)})
        if n % 700 == 0:
            print(f'Knowledge gaps: inspected {n}/{len(files)} YAML files', flush=True)
    gaps = [r for r in rows if r['is_gap']]
    exports = {'discussions.jsonl.gz': rows, 'knowledge-gaps.jsonl.gz': gaps, 'gap-attachments.jsonl.gz': attachments}
    for filename, records in exports.items():
        with gzip.open(output / filename, 'wt', encoding='utf-8') as stream:
            for row in records:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    fixture = next((r for r in gaps if r['discussion_id'] == 'gap_aip_ahr_branch_tumor_growth_causality'), None)
    if fixture:
        (output / 'aip-gap-example.json').write_text(json.dumps({'gap': fixture, 'attachments': [a for a in attachments if a['gap_id'] == fixture['id']]}, indent=2, ensure_ascii=False, default=str) + '\n')
    manifest = {'retrieved_at': datetime.now(timezone.utc).isoformat(),
                'source_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                'yaml_files': len(files), 'discussions': len(rows), 'knowledge_gaps': len(gaps),
                'gap_kinds': sorted(GAP_KINDS), 'discussions_by_kind': dict(Counter(r['kind'] for r in rows)),
                'gaps_by_kind': dict(Counter(r['kind'] for r in gaps)),
                'gaps_by_status': dict(Counter(r['status'] for r in gaps)),
                'gaps_missing_source_id': sum(not r['has_stable_source_id'] for r in gaps),
                'attachments': len(attachments), 'attachment_resolution': dict(Counter(a['resolution'] for a in attachments)),
                'complete': not errors, 'errors': errors,
                'files': {name: {'rows': len(records), 'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest()} for name, records in exports.items()},
                'resolver_source': 'src/dismech/entity_refs.py',
                'resolver_sha256': hashlib.sha256((source / 'src/dismech/entity_refs.py').read_bytes()).hexdigest(),
                'scope_note': 'All KB YAML discussions; gap kinds follow DisMech discussions exporter; no source statuses changed'}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'source-files.json').write_text(json.dumps(hashes, indent=2) + '\n')
    (output / 'SOURCE-LICENSE.txt').write_text((source / 'LICENSE').read_text())
    print(json.dumps(manifest), flush=True)
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path.home() / 'src/research/dismech')
    parser.add_argument('--output', type=Path, default=Path('data/dismech-gaps/2026-09-24'))
    args = parser.parse_args()
    extract(args.source.resolve(), args.output)
