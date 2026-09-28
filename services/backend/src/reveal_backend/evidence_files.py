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


def record_path(namespace, location, kind='record', number=0):
    key = json.dumps([namespace, location, kind, number], ensure_ascii=False, separators=(',', ':')).encode()
    return 'input/evidence-records/' + sha256(key) + '.json'


def display_label(value):
    if not isinstance(value, dict):
        return {}
    for field in ('name', 'display_name', 'filename', 'id'):
        label = value.get(field)
        if type(label) is str:
            return {'label': label[:160], **({'label_truncated': True} if len(label) > 160 else {})}
    return {}


class _Files:
    def __init__(self, page_size):
        self.files = {}
        self.page_size = page_size
        self.links = {}
        self.force_collections = set()

    def write(self, path, value):
        data = readable(value)
        require(len(data) <= MAX_RECORD_BYTES, 'Evidence reader record exceeds its byte bound')
        previous = self.files.get(path)
        require(previous is None or previous == data, 'Conflicting evidence reader record')
        self.files[path] = data
        return path

    def pages(self, entries, namespace, location, origin):
        if not entries:
            return {'page_count': 0, 'first_page': None}
        groups, pending = [], []
        for entry in entries:
            trial = pending + [entry]
            # Reserve the real next-page path when checking the byte bound.
            probe = {'format': PAGE_FORMAT, **origin, 'pointer': location, 'entries': trial,
                     'next_page': record_path(namespace, location, 'page', len(groups) + 1)}
            if pending and (len(trial) > self.page_size or len(readable(probe)) > MAX_RECORD_BYTES):
                groups.append(pending)
                pending = [entry]
            else:
                pending = trial
        if pending:
            groups.append(pending)
        for index, group in enumerate(groups):
            self.write(record_path(namespace, location, 'page', index),
                       {'format': PAGE_FORMAT, **origin, 'pointer': location, 'entries': group,
                        'next_page': record_path(namespace, location, 'page', index + 1)
                                     if index + 1 < len(groups) else None})
        return {'page_count': len(groups), 'first_page': record_path(namespace, location, 'page')}

    def store(self, value, namespace, location, origin):
        path = record_path(namespace, location)
        if path in self.files:
            return path
        envelope = {'format': RECORD_FORMAT, **origin, 'pointer': location, 'value': value}
        links = self.links.get((namespace, location))
        if links:
            envelope['links'] = links
        force = (namespace, location) in self.force_collections
        if not force and len(readable(envelope)) <= MAX_RECORD_BYTES:
            return self.write(path, envelope)
        entries = []
        if isinstance(value, dict):
            kind = 'object'
            for key in sorted(value):
                entries.append({'key': key, 'path': self.store(value[key], namespace, child_pointer(location, key), origin),
                                **display_label(value[key])})
        elif isinstance(value, list):
            kind = 'array'
            for key, item in enumerate(value):
                entries.append({'key': key, 'path': self.store(item, namespace, child_pointer(location, key), origin),
                                **display_label(item)})
        elif type(value) is str:
            kind = 'string'
            # At most 4096 UTF-8 payload bytes per exact character chunk.
            for key, offset in enumerate(range(0, len(value), 1024)):
                chunk_path = record_path(namespace, location, 'text', key)
                self.write(chunk_path, {'format': RECORD_FORMAT, **origin, 'pointer': location,
                                       'text_chunk': key, 'value': value[offset:offset + 1024]})
                entries.append({'key': key, 'path': chunk_path})
        else:
            require(False, 'Oversized scalar cannot be represented by the evidence reader')
        record = {'format': RECORD_FORMAT, **origin, 'pointer': location, 'kind': kind,
                  'count': len(value), **self.pages(entries, namespace, location, origin)}
        if kind == 'string':
            record.update(chunk_count=len(entries), instruction='Concatenate chunk values in numeric key order without separators; each chunk refers to this same source string pointer.')
        if links:
            record['links'] = links
        return self.write(path, record)


def build_evidence_files(package_bytes, page_size=20, source_bytes=None):
    """Return path→bytes for a small index and bounded lossless reading files.

    Paths are relative to the agent working directory. ``source_bytes`` optionally
    supplies all declared artifacts, keyed by artifact_id, for hash-verified
    parsed views of minified JSON/YAML. Text artifacts retain their raw files.
    Every value remains retrievable through the root record and linked pages.
    """
    require(type(page_size) is int and 1 <= page_size <= 100, 'Invalid evidence reader page size')
    package = decode(package_bytes)
    package_sha = sha256(package_bytes)
    namespace = 'package:' + package_sha
    origin = {'package_sha256': package_sha}
    files = _Files(page_size)
    # These are entry catalogues, even if a small instance would fit in one file.
    catalogue_locations = ('', '/source_artifacts', '/dapper_context/files', '/pigean/candidates',
                           '/pigean/mechanisms', '/pigean/traits', '/dismech/mechanisms')
    files.force_collections.update((namespace, location) for location in catalogue_locations)
    artifacts = package.get('source_artifacts', {})
    require(isinstance(artifacts, dict), 'Source artifacts must be an indexed mapping')
    if source_bytes is not None:
        require(set(source_bytes) == set(artifacts), 'Reader source bytes differ from declared artifacts')
    parsed_sources = 0
    for identity, artifact in artifacts.items():
        links = {'raw_path': 'input/' + artifact['path']}
        if source_bytes is not None:
            raw = source_bytes[identity]
            require(sha256(raw) == artifact['sha256'], 'Evidence reader source checksum mismatch')
            if artifact['format'] in ('json', 'yaml'):
                source_namespace = 'source:' + artifact['sha256'] + ':' + artifact['format']
                source_origin = {'artifact_sha256': artifact['sha256'], 'source_format': artifact['format']}
                parsed = parse_source(raw, artifact['format'])
                links['parsed_record_path'] = files.store(parsed, source_namespace, '', source_origin)
                parsed_sources += 1
        files.links[(namespace, child_pointer('/source_artifacts', identity))] = links

    root_path = files.store(package, namespace, '', origin)

    def catalogue(location):
        try:
            value = pointer(package, location)
        except (KeyError, IndexError, ValueError):
            return None
        entry = {'path': files.store(value, namespace, location, origin)}
        if isinstance(value, (dict, list)):
            entry['count'] = len(value)
        return entry

    gap = package.get('dismech', {}).get('knowledge_gap', {})
    selection = package.get('selection', {})
    selected_gap = {'id': selection.get('knowledge_gap_id'), 'record_path': catalogue('/dismech/knowledge_gap')}
    if selected_gap['record_path']:
        selected_gap['record_path'] = selected_gap['record_path']['path']
    if isinstance(gap.get('prompt'), str):
        if len(gap['prompt']) <= 600:
            selected_gap['prompt'] = gap['prompt']
        else:
            selected_gap.update(prompt_preview=gap['prompt'][:600], prompt_truncated=True)
    anchors = []
    for identity in selection.get('eaggl_mechanism_ids', []):
        location = child_pointer('/pigean/mechanisms', identity)
        value = pointer(package, location)
        row = {'id': identity, 'dapper_id': value['dapper_id'],
               'record_path': files.store(value, namespace, location, origin)}
        if isinstance(value.get('display_name'), str):
            row.update(name=value['display_name'][:160], name_truncated=len(value['display_name']) > 160)
        anchors.append(row)
    sections = {key: catalogue(child_pointer('', key)) for key in sorted(package)}
    catalogues = {name: catalogue(location) for name, location in {
        'candidates': '/pigean/candidates', 'mechanisms': '/pigean/mechanisms', 'traits': '/pigean/traits',
        'dismech_mechanisms': '/dismech/mechanisms', 'source_artifacts': '/source_artifacts',
        'dapper_files': '/dapper_context/files'}.items()}
    index = {'format': 'reveal.evidence-index/1',
             'canonical_package': {'path': 'input/evidence-package.json', 'sha256': package_sha, 'size_bytes': len(package_bytes)},
             'root_record_path': root_path, 'selected_gap': selected_gap, 'anchors': anchors,
             'sections': sections, 'catalogues': catalogues,
             'source_views': {'provided': source_bytes is not None, 'parsed_artifact_count': parsed_sources},
             'reader_limits': {'record_bytes': MAX_RECORD_BYTES, 'page_entries': page_size},
             'instruction': 'Read only relevant records. A value is exact source data. Larger values use kind/count/first_page; follow entries by key and next_page only as needed. Catalogue counts describe captured membership, not biological absence. Source-artifact records link unchanged raw files and optional parsed views with exact source pointers. Display labels and previews are navigation only.'}
    files.write(INDEX_PATH, index)
    return files.files
