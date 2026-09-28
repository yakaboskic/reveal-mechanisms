"""Offline file-reader regressions: no tokenizer, provider, Box, or network."""
from copy import deepcopy
from decimal import Decimal
import json
import unittest

from reveal_backend.evidence_files import (INDEX_PATH, MAX_RECORD_BYTES, build_evidence_files,
                                           child_pointer)
from reveal_backend.evidence_package import canonical_json, pointer, sha256


def read(files, path):
    return json.loads(files[path], parse_float=Decimal)


def entries(files, record):
    page = record['first_page']
    seen = set()
    values = []
    while page is not None:
        if page in seen:
            raise AssertionError('Reader page loop')
        seen.add(page)
        value = read(files, page)
        values.extend(value['entries'])
        page = value['next_page']
    return values


def reconstruct(files, path):
    record = read(files, path)
    if 'value' in record:
        return record['value']
    children = entries(files, record)
    if record['kind'] == 'object':
        return {entry['key']: reconstruct(files, entry['path']) for entry in children}
    if record['kind'] == 'array':
        assert [entry['key'] for entry in children] == list(range(record['count']))
        return [reconstruct(files, entry['path']) for entry in children]
    assert record['kind'] == 'string'
    assert [entry['key'] for entry in children] == list(range(record['chunk_count']))
    value = ''.join(reconstruct(files, entry['path']) for entry in children)
    assert len(value) == record['count']
    return value


def fixture():
    identity = 'factor:trait/~special:Factor1'
    source = b'{"data":[{"gene":"TEST","factor_value":0.1234567890123456789012345,"exponent":1.2300e-400,"integer":900719925474099312345}],"empty":[]}'
    digest = sha256(source)
    package = {
        'selection': {'knowledge_gap_id': 'dapper:KnowledgeGap.test', 'eaggl_mechanism_ids': [identity]},
        'dismech': {'knowledge_gap': {'prompt': 'Which measured relationship is supported?',
                                     'source_ref': {'artifact_id': 'capture/one~two', 'pointer': '/data/0'}},
                    'mechanisms': {'source:/record~1': {'name': 'Exact linked context', 'description': 'Context only.'}}},
        'pigean': {'mechanisms': {identity: {'dapper_id': 'dapper:Mechanism.test', 'display_name': 'Selected factor',
            'gene_loadings': {'status': 'omitted', 'query_status': 'ok_limit_reached', 'items': {}},
            'trait_loadings': {'status': 'empty', 'query_status': 'empty', 'items': {}}}},
            'candidates': {f'gene:{n:03}': {'aggregate_score': n / 1000, 'source_ref': {'artifact_id': 'capture/one~two', 'pointer': '/data/0'}} for n in range(55)},
            'traits': {}},
        'coverage': {'omitted_node_ids': ['gene:omitted'], 'upstream_total': None},
        'source_artifacts': {'capture/one~two': {'sha256': digest, 'format': 'json',
            'path': f'sources/{digest}.json', 'filename': 'exact-source.json', 'dapper_file_id': 'dapper:File.test'}},
        'dapper_context': {'files': [{'id': 'dapper:File.test', 'sha256': digest, 'size_in_bytes': len(source)}]},
    }
    return package, {'capture/one~two': source}


class EvidenceFilesTests(unittest.TestCase):
    def setUp(self):
        self.package, self.sources = fixture()
        self.raw = canonical_json(self.package)

    def build(self, **kwargs):
        return build_evidence_files(self.raw, source_bytes=self.sources, **kwargs)

    def source_record(self, files):
        index = read(files, INDEX_PATH)
        catalogue = read(files, index['catalogues']['source_artifacts']['path'])
        item = entries(files, catalogue)[0]
        return read(files, item['path'])

    def test_entire_package_roundtrips_without_pruning_or_changing_coverage(self):
        before = deepcopy(self.package)
        files = self.build(page_size=7)
        index = read(files, INDEX_PATH)
        self.assertEqual(reconstruct(files, index['root_record_path']), json.loads(self.raw, parse_float=Decimal))
        self.assertEqual(self.package, before)
        self.assertEqual(index['catalogues']['candidates']['count'], 55)
        self.assertEqual(index['anchors'][0]['id'], self.package['selection']['eaggl_mechanism_ids'][0])
        anchor = reconstruct(files, index['anchors'][0]['record_path'])
        self.assertEqual(anchor['gene_loadings'], {'status': 'omitted', 'query_status': 'ok_limit_reached', 'items': {}})
        self.assertEqual(anchor['trait_loadings'], {'status': 'empty', 'query_status': 'empty', 'items': {}})
        self.assertEqual(index['canonical_package']['sha256'], sha256(self.raw))

    def test_source_numeric_tokens_keep_precision_and_link_original_locator(self):
        files = self.build()
        descriptor = self.source_record(files)
        source_root = descriptor['links']['parsed_record_path']
        parsed = reconstruct(files, source_root)
        expected = json.loads(self.sources['capture/one~two'], parse_float=Decimal)
        self.assertEqual(parsed, expected)
        self.assertEqual(parsed['data'][0]['integer'], 900719925474099312345)
        self.assertEqual(parsed['data'][0]['exponent'], Decimal('1.2300e-400'))
        self.assertIn(b'0.1234567890123456789012345', files[source_root])
        self.assertIn(b'1.2300e-400', files[source_root])
        self.assertEqual(read(files, source_root)['artifact_sha256'], descriptor['value']['sha256'])
        self.assertEqual(descriptor['links']['raw_path'], 'input/' + descriptor['value']['path'])
        self.assertEqual(descriptor['pointer'], '/source_artifacts/capture~1one~0two')

    def test_large_minified_sources_have_bounded_exact_row_reads(self):
        source = ('{"data":[' + ','.join('{"i":%d,"value":0.123456789012345678901}' % n for n in range(900)) + ']}').encode()
        self.sources['capture/one~two'] = source
        self.package['source_artifacts']['capture/one~two']['sha256'] = sha256(source)
        self.raw = canonical_json(self.package)
        files = self.build(page_size=13)
        root = self.source_record(files)['links']['parsed_record_path']
        parsed = reconstruct(files, root)
        self.assertEqual(parsed, json.loads(source, parse_float=Decimal))
        row_records = [read(files, path) for path in files if path != INDEX_PATH and read(files, path).get('pointer') == '/data/731']
        self.assertEqual(len(row_records), 1)
        self.assertEqual(row_records[0]['value']['i'], 731)
        self.assertEqual(row_records[0]['value']['value'], Decimal('0.123456789012345678901'))
        self.assertTrue(all(len(data) <= MAX_RECORD_BYTES for data in files.values()))
        self.assertTrue(all(len(read(files, path)['entries']) <= 13 for path in files if read(files, path).get('format') == 'reveal.evidence-page/1'))

    def test_long_unicode_text_and_many_pages_are_lossless_and_bounded(self):
        long_text = 'Exact 🧬 β sentence.\n' * 2500
        self.package['dismech']['knowledge_gap']['prompt'] = long_text
        self.package['pigean']['candidates'] = {f'gene:{n}': {'text': 'Specific observation ' + str(n)} for n in range(450)}
        self.raw = canonical_json(self.package)
        files = self.build(page_size=3)
        index = read(files, INDEX_PATH)
        self.assertTrue(index['selected_gap']['prompt_truncated'])
        self.assertNotIn('prompt', index['selected_gap'])
        self.assertEqual(reconstruct(files, index['selected_gap']['record_path'])['prompt'], long_text)
        self.assertEqual(reconstruct(files, index['root_record_path']), json.loads(self.raw, parse_float=Decimal))
        self.assertTrue(all(len(data) <= MAX_RECORD_BYTES for data in files.values()))
        catalog = read(files, index['catalogues']['candidates']['path'])
        self.assertEqual(catalog['page_count'], 150)
        self.assertNotIn('pages', catalog)

    def test_exact_yaml_decimal_and_large_integer_are_not_float_coerced(self):
        raw = b'factor_value: 0.1234567890123456789012345\ninteger: 900719925474099312345\nempty: []\n'
        self.sources['capture/one~two'] = raw
        self.package['source_artifacts']['capture/one~two'].update(format='yaml', sha256=sha256(raw))
        self.raw = canonical_json(self.package)
        files = self.build()
        parsed = reconstruct(files, self.source_record(files)['links']['parsed_record_path'])
        self.assertEqual(parsed['factor_value'], Decimal('0.1234567890123456789012345'))
        self.assertEqual(parsed['integer'], 900719925474099312345)

    def test_source_checksum_missing_extra_or_ambiguous_source_rejected(self):
        with self.assertRaises(ValueError):
            build_evidence_files(self.raw, source_bytes={'capture/one~two': b'changed'})
        with self.assertRaises(ValueError):
            build_evidence_files(self.raw, source_bytes={})
        with self.assertRaises(ValueError):
            build_evidence_files(self.raw, source_bytes={**self.sources, 'extra': b'{}'})
        for bad in (b'{"same":1,"same":2}', b'{"score":NaN}'):
            with self.subTest(bad=bad):
                self.package['source_artifacts']['capture/one~two']['sha256'] = sha256(bad)
                with self.assertRaises(ValueError):
                    build_evidence_files(canonical_json(self.package), source_bytes={'capture/one~two': bad})

    def test_deterministic_paths_escape_source_keys_and_never_escape_input_directory(self):
        files = self.build()
        self.assertEqual(files, self.build())
        self.assertTrue(all(path.startswith('input/') and '..' not in path.split('/') for path in files))
        identity = self.package['selection']['eaggl_mechanism_ids'][0]
        anchor = read(files, read(files, INDEX_PATH)['anchors'][0]['record_path'])
        self.assertEqual(anchor['pointer'], child_pointer('/pigean/mechanisms', identity))
        self.assertEqual(anchor['value'], pointer(self.package, anchor['pointer']))

    def test_metadata_only_build_does_not_invent_source_views(self):
        files = build_evidence_files(self.raw)
        descriptor = self.source_record(files)
        self.assertNotIn('parsed_record_path', descriptor['links'])
        self.assertFalse(read(files, INDEX_PATH)['source_views']['provided'])
        for bad in (True, 0, 101):
            with self.assertRaises(ValueError): build_evidence_files(self.raw, page_size=bad)
