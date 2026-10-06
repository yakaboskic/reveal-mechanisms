"""Lossless, bounded local reading files derived from immutable evidence.

No network, tokenization, model, filesystem writes, or scientific reinterpretation.
The canonical package and raw artifact bytes remain the authorities. Every
derived record carries its exact pointer into one of those immutable sources.
"""
from __future__ import annotations

import json
from decimal import Decimal

import yaml

from .evidence_package import StrictLoader, decode, pointer, require, sha256

INDEX_PATH = 'input/evidence-index.json'
MAX_RECORD_BYTES = 12 * 1024
RECORD_FORMAT = 'reveal.evidence-record/1'
PAGE_FORMAT = 'reveal.evidence-page/1'


class _Number(str):
    """An exact source JSON number token, emitted as a number, never a string."""


def readable(value):
    def render(item, level=0):
        if isinstance(item, _Number):
            return str(item)
        if isinstance(item, dict):
            if not item:
                return '{}'
            return '{\n' + ',\n'.join(' ' * (level + 1) + json.dumps(key, ensure_ascii=False) + ': ' +
                                      render(item[key], level + 1) for key in sorted(item)) + '\n' + ' ' * level + '}'
        if isinstance(item, list):
            if not item:
                return '[]'
            return '[\n' + ',\n'.join(' ' * (level + 1) + render(child, level + 1) for child in item) + '\n' + ' ' * level + ']'
        return json.dumps(item, ensure_ascii=False, allow_nan=False)
    return (render(value) + '\n').encode()


def parse_source(raw, format):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate source object key')
            result[key] = value
        return result
    if format == 'json':
        return json.loads(raw, object_pairs_hook=pairs, parse_int=_Number, parse_float=_Number,
                          parse_constant=lambda _: require(False, 'Nonfinite source number'))
    class Loader(StrictLoader):
        pass
    def exact_float(loader, node):
        number = Decimal(loader.construct_scalar(node).replace('_', ''))
        require(number.is_finite(), 'Nonfinite source number')
        return _Number(str(number))
    Loader.add_constructor('tag:yaml.org,2002:float', exact_float)
    return yaml.load(raw, Loader=Loader)


def child_pointer(parent, key):
    return parent + '/' + str(key).replace('~', '~0').replace('/', '~1')


def build_evidence_index(package_bytes, source_bytes=None):
    """Small navigation inventory; never expands arrays, vectors or records."""
    from .evidence_reader import READER_VERSION, MAX_RESPONSE_BYTES, MAX_ITEMS, MAX_TEXT_CHARACTERS
    package = decode(package_bytes)
    package_sha = sha256(package_bytes)
    identity = 'package:' + package_sha
    artifacts = package.get('source_artifacts', {})
    if source_bytes is not None:
        require(set(source_bytes) == set(artifacts), 'Reader sources differ from declared artifacts')
        for key, descriptor in artifacts.items():
            require(sha256(source_bytes[key]) == descriptor['sha256'] and
                    len(source_bytes[key]) == descriptor['size_bytes'], 'Evidence reader source checksum mismatch')
    def selection(location):
        return {'artifact_id': identity, 'sha256': package_sha, 'pointer': location}
    anchors = []
    for key in package.get('selection', {}).get('eaggl_mechanism_ids', []):
        node = package.get('pigean', {}).get('mechanisms', {}).get(key, {})
        anchors.append({'id': key, 'dapper_id': node.get('dapper_id'),
                        'read': selection(child_pointer('/pigean/mechanisms', key))})
    return readable({'format': 'reveal.evidence-index/2', 'reader_version': READER_VERSION,
        'canonical_package': {'artifact_id': identity, 'path': 'input/evidence-package.json',
                              'sha256': package_sha, 'size_bytes': len(package_bytes)},
        'selected_gap': {'id': package.get('selection', {}).get('knowledge_gap_id'),
                         'read': selection('/dismech/knowledge_gap')},
        'anchors': anchors,
        'sections': {key: selection(child_pointer('', key)) for key in sorted(package)
                     if key not in ('source_artifacts', 'dapper_context')},
        'source_inventory': {key: {k: value[k] for k in ('path', 'sha256', 'size_bytes', 'format', 'dapper_file_id') if k in value}
                             for key, value in artifacts.items()},
        'trusted_objects': selection('/dapper_context'),
        'reader': {'tool': 'read_evidence', 'response_bytes': MAX_RESPONSE_BYTES, 'max_items': MAX_ITEMS,
                   'max_text_characters': MAX_TEXT_CHARACTERS, 'default_items': 20,
                   'exact_numbers': 'content_json preserves original numeric tokens'},
        'instruction': 'Read selected JSON Pointers or bounded text ranges with read_evidence. Inspection paging does not alter scientific coverage. Original artifacts are authoritative. Embeddings and retrieval diagnostics are audit data; read only on explicit need. No recursive record files are generated.'})


def build_evidence_files(package_bytes, page_size=20, source_bytes=None):
    """Compatibility API for callers; new workspaces contain only the small index."""
    return {INDEX_PATH: build_evidence_index(package_bytes, source_bytes)}
