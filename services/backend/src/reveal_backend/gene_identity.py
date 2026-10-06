"""Offline, content-pinned HGNC identity crosswalk with explicit taxon scope.

This maps symbols only when the caller supplies organism context; it never
retrofits old observations or treats symbol spelling as evidence of species.
The complete and withdrawn source files are retained, not fetched at query time.
"""
from collections import defaultdict
from copy import deepcopy
import csv
from functools import lru_cache
import gzip
import hashlib
import io
import json
from pathlib import Path

MAPPING_REVISION = 'ab3d238c6ec7fee150e8199064a41fd2ef814165170a2429229706acbf8aafbb'
SOURCE_ROOT = Path(__file__).resolve().parents[4] / 'data/gene-identity/hgnc-2026-10-06'
HUMAN = 'NCBITaxon:9606'


def _sha(raw): return hashlib.sha256(raw).hexdigest()


def _canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def crosswalk_capability():
    try:
        _pinned_crosswalk()
        status = 'available'
    except FileNotFoundError: status = 'not_installed'
    except (ValueError, OSError, KeyError): status = 'source_integrity_failed'
    return {'status': status,
            'mapping_revision': MAPPING_REVISION, 'provider': 'HGNC', 'taxa': [HUMAN],
            'requires_explicit_taxon': True, 'source_backed': True,
            'limitations': 'Human only. Source-local EAGGL organism is not inferred; other taxa remain unsupported. Aliases and obsolete symbols are not automatically treated as approved identity.'}


class GeneCrosswalk:
    def __init__(self, manifest, records):
        self.manifest = deepcopy(manifest)
        self.by_symbol, self.by_alias, self.by_id = defaultdict(list), defaultdict(list), defaultdict(list)
        for record in records:
            self.by_symbol[record['symbol']].append(record)
            for alias in record.get('aliases', []): self.by_alias[alias].append(record)
            for identity in record['identifiers']: self.by_id[identity].append(record)

    def resolve(self, symbol, *, taxon=None, mapping_revision=None, source_import=None, source_index=None):
        revision = self.manifest['mapping_revision']
        if mapping_revision is not None and mapping_revision != revision:
            raise ValueError('Gene mapping revision is unavailable; no current-release substitution is allowed')
        # Equivalent explicit taxonomy forms are normalized without inferring taxon.
        if taxon in (9606, '9606', 'http://purl.obolibrary.org/obo/NCBITaxon_9606',
                     'https://purl.obolibrary.org/obo/NCBITaxon_9606'): taxon = HUMAN
        result = {'mapping_revision': revision, 'source_release': self.manifest['source_release'],
            'source_local': {'symbol': symbol, 'source_import': source_import, 'source_index': source_index},
            'taxon': taxon, 'candidates': []}
        if taxon is None: return {**result, 'status': 'needs_taxon'}
        if taxon != self.manifest['taxon']: return {**result, 'status': 'unsupported_taxon'}
        matches = self.by_id.get(symbol) or self.by_symbol.get(symbol)
        method = 'exact_identifier' if symbol in self.by_id else 'exact_symbol'
        if not matches:
            matches = self.by_alias.get(symbol, [])
            method = 'alias_or_previous_symbol'
        unique = {record['identifiers'][0]: record for record in matches}
        candidates = [{**deepcopy(record), 'mapping_method': method} for _, record in sorted(unique.items())]
        result['candidates'] = candidates
        result['status'] = ('unmapped' if not candidates else 'ambiguous' if len(candidates) > 1 else
            'obsolete' if candidates[0]['status'] != 'approved' else
            'alias' if method == 'alias_or_previous_symbol' else 'mapped')
        result['verified_identity'] = candidates[0]['identifiers'] if result['status'] == 'mapped' else []
        return result


def load_crosswalk(root=SOURCE_ROOT):
    root = Path(root).resolve(); manifest = json.loads((root / 'manifest.json').read_bytes())
    unsigned = {key: value for key, value in manifest.items() if key != 'mapping_revision'}
    if _sha(_canonical(unsigned)) != manifest['mapping_revision']: raise ValueError('Gene crosswalk manifest checksum changed')
    if manifest['mapping_revision'] != MAPPING_REVISION: raise ValueError('Unrecognized gene mapping revision')
    records = []
    for entry in manifest['files']:
        path = (root / entry['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file(): raise ValueError('Invalid crosswalk source path')
        packed = path.read_bytes()
        if len(packed) != entry['size_bytes'] or _sha(packed) != entry['sha256']: raise ValueError('Gene crosswalk source checksum changed')
        raw = gzip.decompress(packed)
        if len(raw) != entry['source_size_bytes'] or _sha(raw) != entry['source_sha256']: raise ValueError('Gene crosswalk original bytes changed')
        for index, row in enumerate(csv.DictReader(io.StringIO(raw.decode('utf-8')), delimiter='\t'), 2):
            withdrawn = 'HGNC_ID' in row
            symbol = row['WITHDRAWN_SYMBOL'] if withdrawn else row['symbol']
            identity = row['HGNC_ID'] if withdrawn else row['hgnc_id']
            identifiers = [identity]
            if not withdrawn:
                if row.get('entrez_id'): identifiers.append('NCBIGene:'+row['entrez_id'])
                if row.get('ensembl_gene_id'): identifiers.append('ENSEMBL:'+row['ensembl_gene_id'])
            records.append({'symbol': symbol, 'taxon': manifest['taxon'], 'identifiers': identifiers,
                'aliases': [] if withdrawn else sorted(set(filter(None, (row.get('alias_symbol', '')+'|'+row.get('prev_symbol', '')).split('|')))),
                'status': 'obsolete' if withdrawn else 'approved', 'source_release': manifest['source_release'],
                'source_locator': {'url': entry['source_url'], 'source_sha256': entry['source_sha256'],
                    'format': 'tsv', 'record_number': index-1, 'hgnc_id': identity},
                **({'withdrawn_status': row['STATUS'], 'replacement_assertion': row.get('MERGED_INTO_REPORT(S) (i.e HGNC_ID|SYMBOL|STATUS)', '')} if withdrawn else {})})
    return GeneCrosswalk(manifest, records)


@lru_cache(maxsize=1)
def _pinned_crosswalk(): return load_crosswalk()


def resolve_pinned_gene(symbol, *, taxon=None, mapping_revision=None, source_import=None, source_index=None):
    return _pinned_crosswalk().resolve(symbol, taxon=taxon, mapping_revision=mapping_revision,
                                      source_import=source_import, source_index=source_index)
