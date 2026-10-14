"""Bounded research reads of an immutable imported reference generation or of the published reference release.

No active-catalog selection or external fallback is used here. Callers authorize
the request, retain its generation and persist each QueryCapture before exposing
it to an agent. This module owns the scientific bytes and their File identities.

A pin is either the environment's published release (read first on each query's connection, reference_pin; its
reveal_ref_* tables, reader RELEASE_READER_VERSION) or a reload-era generation (reference_generations and the migration-008
tables, reader READER_VERSION). A release pin that a later publish replaced is refused (reference_pin.superseded_problem).
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .auth import Problem
from .evidence_package import canonical_json, decode, require, sha256
from .factor_details import GENE_COVERAGE, SET_COVERAGE, LEGACY_COVERAGE, collection_facts, extracted
from .reference_generation import KPN_MODEL, LEGACY_MODEL, GENERATION_RE, mechanism_node, parse_public_id
from .reference_lineage import lineage_closure
from .reference_pin import NO_TABLE, ReleaseUnavailable, current_release, superseded_problem
from .repository import application_prefix, application_sql

READER_VERSION = 'reveal.reference-query/2'
RELEASE_READER_VERSION = 'reveal.reference-query/3'  # captures of a reference release (source_kind reference_release)
READER_VERSIONS = (READER_VERSION, RELEASE_READER_VERSION)
RELEASE = 'reference_release'
CAPTURE_FORMAT = 'reveal.reference-query.capture/1'
MAX_CAPTURE_BYTES = 1_000_000
PAGE_FIELDS = {'limit', 'cursor'}
OPERATIONS = {
    'search_factors': {'q'}, 'get_factor': {'factor_id'},
    'get_factor_loadings': {'factor_id', 'kind', 'metric', 'q'},
    'search_genes': {'q'}, 'resolve_gene': {'gene', 'taxon', 'mapping_revision'}, 'get_gene_factors': {'gene'},
    'get_gene_gene_sets': {'gene', 'factor_id'},
    'search_gene_sets': {'q'}, 'get_gene_set': {'gene_set_id'},
    'get_gene_set_members': {'gene_set_id'}, 'get_gene_set_factors': {'gene_set_id'},
    'search_traits': {'q'}, 'get_trait': {'trait_id'},
    'get_connections': {'factor_id'}, 'get_imported_graph': {'node_id'},
    'get_trait_gene_sets': {'trait_id', 'library', 'q'}, 'get_gene_set_traits': {'gene_set_id'},
    'get_factor_trait_links': {'factor_id', 'scope'}, 'get_trait_factor_links': {'trait_id', 'scope'},
}
# Stored only in reference releases (not_stored for reload-era generations); releases store no EAGGL graph.
RELEASE_OPERATIONS = ('get_trait_gene_sets', 'get_gene_set_traits', 'get_factor_trait_links', 'get_trait_factor_links')
GRAPH_OPERATIONS = ('get_connections', 'get_imported_graph')
METRICS = {
    'loading': 'Stored nonzero EAGGL gene weight; absent rows are not zero.',
    'joint_loading': 'Retained gene-set joint projection (top 50 per trait in a reload-era generation, per library in a release), not a phenotype association.',
    'marginal_loading': 'Retained gene-set marginal projection (top 50 per trait in a reload-era generation, per library in a release), not a phenotype association.',
    'joint_rank': 'Rank in the source joint projections (per trait in a reload-era generation, within the library in a release), before retained-subset filtering.',
    'marginal_rank': 'Rank in the source marginal projections (per trait in a reload-era generation, within the library in a release), before retained-subset filtering.',
    'beta_uncorrected': 'Release PIGEAN betas-mode gene-set effect on the trait before PIGEAN\'s correction (one fit per CFDE library, no outer Gibbs); not the BioIndex small-model value.',
    'beta': 'Release PIGEAN betas-mode corrected gene-set effect on the trait (one fit per CFDE library); not the BioIndex small-model value.',
    'avg_postp': 'Release PIGEAN betas-mode average posterior probability that the gene set is associated with the trait; not the BioIndex small-model value.',
    'library_rank': 'Rank of the gene set within its library for the trait, by beta_uncorrected.',
    'nnls_loading': 'eaggl trait linkage: the phenotype\'s support projected onto the fitted trait\'s factors by fixed-W NNLS; descriptive, no p-value.',
    'phewas_beta': 'eaggl factor-PheWAS: OLS coefficient of the phenotype\'s hit genes on the factor\'s gene loadings, adjusted for the fitted trait\'s direct support.',
    'phewas_p_onesided': 'eaggl factor-PheWAS one-sided p-value (robust HC3 standard errors) for enrichment of the phenotype in the factor.',
    'phewas_q': 'Benjamini-Hochberg q-value of phewas_p_onesided over every factor-phenotype test of the release; links are kept at q <= 0.05.',
}
# Operation-specific notes appended to the shared loaded-data tool descriptions.
DESCRIPTIONS = {
    'get_gene_gene_sets': ' Lists CFDE gene sets whose stored membership contains the gene (symbol matched without case; member and'
        ' source_pointer give the exact stored identifier). With factor_id (recommended), only that factor\'s retained projections (per'
        ' trait in a reload-era generation, per library in a release), with their joint and marginal loadings. Without it every stored'
        ' gene set\'s membership is scanned, within a time budget: past it the result is unavailable (scan_budget_exceeded), not empty.',
    'get_trait_gene_sets': ' Lists the PIGEAN gene-set betas of a KPN trait by beta_uncorrected (library keeps one CFDE library; q filters'
        ' the gene-set name). Reference releases only; these are not the BioIndex small-model values.',
    'get_gene_set_traits': ' Lists the KPN traits with a PIGEAN beta for a gene set, by beta_uncorrected. Reference releases only.',
    'get_factor_trait_links': ' Lists the phenotypes a factor is linked to (eaggl trait linkage and factor-PheWAS, q <= 0.05), by phewas_q;'
        ' scope atlas keeps the atlas traits. Reference releases only.',
    'get_trait_factor_links': ' Lists the factors linked to a KPN trait, its own factors flagged is_own_trait, by phewas_q. Reference releases only.',
}
MEMBERSHIP_COVERAGE = ('Exact stored CFDE gene-set membership of this generation, matched on the gene symbol without case; member is the'
    ' stored identifier at source_pointer in the GeneSet definition. Read the trusted GeneSet with get_gene_set before citing membership.')
# Reference release coverage (reveal_ref_*): projections are ranked within each library, and only the gene sets the release
# references (a factor projection or a trait beta) are stored.
RELEASE_GENE_COVERAGE = 'All stored nonzero EAGGL gene loadings of this release. Genes absent from a factor are not assigned a zero loading.'
RELEASE_SET_COVERAGE = ('Retained projections: per library, the gene sets in the top 50 by joint or marginal loading (ranks are within the'
    ' library). Search covers this retained subset, not every gene set in the source library.')
RELEASE_MEMBERSHIP_COVERAGE = ('Exact stored CFDE gene-set membership, matched on the gene symbol without case, of the gene sets this release'
    ' references (a factor projection or a trait beta); a gene set not listed may still contain the gene (absent is not non-member). member is'
    ' the stored identifier at source_pointer in the GeneSet definition. Read the trusted GeneSet with get_gene_set before citing membership.')
RELEASE_GENE_SET_COVERAGE = ('The gene sets this release references (a factor projection or a trait beta); other gene sets of the CFDE'
    ' libraries are not stored.')
RELEASE_GENE_SEARCH_COVERAGE = ('Loaded genes only: symbols with a stored nonzero loading on a factor of this release (genes of the EAGGL'
    ' universe without one are not listed); external identifier equivalence and organism are not inferred.')
RELEASE_TRAIT_COVERAGE = ('Release trait metadata: the atlas traits (is_atlas) and the other KPN phenotypes of the factor-trait linkage.'
    ' Gene-set betas are read with get_trait_gene_sets, factor links with get_trait_factor_links.')
BETA_COVERAGE = ('PIGEAN betas mode (no outer Gibbs) on each atlas trait\'s PIGEAN gene statistics, one fit per CFDE library (LINCS not fitted);'
    ' rows are the gene sets PIGEAN kept. An absent (trait, gene set) pair is not a zero beta, and some kept rows are exactly 0.'
    ' Not the BioIndex small-model values.')
LINK_COVERAGE = ('eaggl trait linkage (nnls_loading) and factor-PheWAS (phewas_*), kept at Benjamini-Hochberg q <= 0.05 over every'
    ' factor-phenotype test of the release. A phenotype absent here is unlinked at this threshold, not a negative result.')
NO_GRAPH = 'A reference release stores no EAGGL factor hierarchy graph.'
# get_gene_gene_sets without factor_id reads every stored gene set's members (about 1 GB of JSON in a release): MySQL stops
# the statement at the budget (an optimizer hint; a comment to SQLite) with ER_QUERY_TIMEOUT, an unavailable result.
SCAN_BUDGET_MS, SCAN_TIMEOUT = 4000, 3024
SCAN_HINT = f'/*+ MAX_EXECUTION_TIME({SCAN_BUDGET_MS}) */ '
SCAN_BUDGET_COVERAGE = ('The membership scan of every stored gene set stopped at its time budget, so nothing was observed; this is'
    ' not a biological negative. Pass factor_id to search one factor\'s retained projections.')
RELEASE_ONLY = 'Gene-set betas and factor-trait links are stored only in reference releases.'
# An optional release file absent at publish leaves its table empty (the stored manifest's absent_files).
RELEASE_FILES = {'get_trait_gene_sets': 'trait_gene_sets.tsv.gz', 'get_gene_set_traits': 'trait_gene_sets.tsv.gz',
                 'get_factor_trait_links': 'factor_trait_links.tsv.gz', 'get_trait_factor_links': 'factor_trait_links.tsv.gz'}


KPN_FACTOR_COLUMNS = ('factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'label', 'source_revision', 'metadata', 'factor_index')
# A KPN factor by any of its ids: three unique-key lookups, then the import row by factor_lookup (SHA-256 of the
# id, as the importer writes it). The OR form below scans every factor of the generation and of the import.
KPN_FACTOR_LOOKUP = ('SELECT f.factor_key,f.public_id,f.eaggl_factor_id,f.kpn_trait_id,f.label,f.source_revision,f.metadata,e.factor_index'
    ' FROM (SELECT factor_key FROM reference_factors WHERE generation_id=%s AND public_id IN ({marks})'
    ' UNION SELECT factor_key FROM reference_factors WHERE generation_id=%s AND factor_key IN ({marks})'
    ' UNION SELECT factor_key FROM reference_factors WHERE generation_id=%s AND eaggl_factor_id IN ({marks})) k'
    ' JOIN reference_factors f ON f.generation_id=%s AND f.factor_key=k.factor_key'
    ' LEFT JOIN eaggl_factors e ON e.import_id=%s AND e.factor_id_sha256=SHA2(f.eaggl_factor_id,256) AND e.factor_id=f.eaggl_factor_id')
KPN_FACTOR_TEXT_JOIN = ('SELECT f.factor_key,f.public_id,f.eaggl_factor_id,f.kpn_trait_id,f.label,f.source_revision,f.metadata,e.factor_index'
    ' FROM reference_factors f JOIN eaggl_factors e ON e.import_id=%s AND e.factor_id=f.eaggl_factor_id'
    ' WHERE f.generation_id=%s AND (f.public_id=%s OR f.factor_key=%s OR f.eaggl_factor_id=%s)')
KPN_FACTOR_TABLES = ['reference_factors', 'eaggl_factors']
GENE_LOADING_TABLES, SET_LOADING_TABLES = ['eaggl_gene_loadings', 'eaggl_genes'], ['factor_gene_set_projections', 'cfde_gene_sets']
GENE_ORDER, SET_ORDER = 'l.loading DESC,g.symbol,g.gene_index', 'p.joint_loading DESC,s.gene_set_id'
RELEASE_SET_ORDER = 'p.joint_loading DESC,p.gene_set_id'  # ties by the projection key (factor_key, gene_set_id)
SET_COLUMNS = ('gene_set_id', 'name', 'library', 'joint_loading', 'marginal_loading', 'joint_loading_text',
               'marginal_loading_text', 'joint_rank', 'marginal_rank', 'is_joint_top_factor')
# A release factor by any of its three unique keys. Release statements name the contract tables (application_sql prefixes them).
RELEASE_FACTOR_COLUMNS = ('factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'factor_number', 'label', 'source_revision', 'metadata')
RELEASE_FACTOR_LOOKUP = ('SELECT ' + ','.join('f.' + c for c in RELEASE_FACTOR_COLUMNS) +
    ' FROM (SELECT factor_key FROM reveal_ref_factors WHERE public_id IN ({marks}) UNION SELECT factor_key FROM reveal_ref_factors'
    ' WHERE factor_key IN ({marks}) UNION SELECT factor_key FROM reveal_ref_factors WHERE eaggl_factor_id IN ({marks})) k'
    ' JOIN reveal_ref_factors f ON f.factor_key=k.factor_key ORDER BY f.factor_key')
RELEASE_GENE_TABLES, RELEASE_SET_TABLES = ['reveal_ref_factor_genes'], ['reveal_ref_projections', 'reveal_ref_gene_sets']
RELEASE_GENE_ORDER = 'loading DESC,gene'
SET_SELECT = 's.gene_set_id,s.gene_set_name,s.library,' + ','.join('p.' + c for c in SET_COLUMNS[3:])
TRAIT_COLUMNS = ('kpn_trait_id', 'legacy_phenotype_id', 'phenotype_name', 'trait_group', 'trait_type', 'description', 'metadata')
# Atlas traits, then the linkage's other KPN phenotypes (is_atlas 0), in one statement.
RELEASE_TRAITS = ('SELECT ' + ','.join(TRAIT_COLUMNS) + ',1 FROM reveal_ref_traits WHERE {traits} UNION ALL SELECT kpn_trait_id,'
    'legacy_phenotype_id,display_name,NULL,NULL,NULL,metadata,0 FROM reveal_ref_phenotypes WHERE is_atlas=0 AND kpn_trait_id IS NOT NULL AND {phenotypes}')
BETA_COLUMNS = ('kpn_trait_id', 'trait_name', 'gene_set_id', 'gene_set_name', 'library', 'collection_id', 'n_genes', 'beta_uncorrected',
                'beta', 'avg_postp', 'library_rank')
BETA_SQL = ('SELECT t.kpn_trait_id,r.phenotype_name,t.gene_set_id,s.gene_set_name,t.library,s.collection_id,s.n_genes,t.beta_uncorrected,'
    't.beta,t.avg_postp,t.library_rank FROM reveal_ref_trait_gene_sets t JOIN reveal_ref_gene_sets s ON s.gene_set_id=t.gene_set_id'
    ' JOIN reveal_ref_traits r ON r.kpn_trait_id=t.kpn_trait_id WHERE ')
BETA_TABLES = ['reveal_ref_trait_gene_sets', 'reveal_ref_gene_sets', 'reveal_ref_traits']
LINK_COLUMNS = ('source_id', 'factor_label', 'fitted_kpn_trait_id', 'fitted_trait_name', 'phenotype', 'phenotype_kpn_trait_id', 'phenotype_name',
                'is_own_trait', 'is_atlas_trait', 'nnls_loading', 'phewas_beta', 'phewas_p_onesided', 'phewas_q')
LINK_SQL = ('SELECT f.public_id,f.label,f.kpn_trait_id,r.phenotype_name,l.phenotype,l.phenotype_kpn_trait_id,COALESCE(p.display_name,l.phenotype),'
    'l.is_own_trait,l.is_atlas_trait,l.nnls_loading,l.phewas_beta,l.phewas_p_onesided,l.phewas_q FROM reveal_ref_factor_trait_links l'
    ' JOIN reveal_ref_factors f ON f.factor_key=l.factor_key JOIN reveal_ref_traits r ON r.kpn_trait_id=f.kpn_trait_id'
    ' LEFT JOIN reveal_ref_phenotypes p ON p.phenotype=l.phenotype WHERE ')
LINK_TABLES = ['reveal_ref_factor_trait_links', 'reveal_ref_factors', 'reveal_ref_traits', 'reveal_ref_phenotypes']
LINK_SUMMARY = ('n_phenotypes', 'n_linked', 'n_linked_kpn_traits', 'n_linked_atlas_traits', 'own_trait_q')


def _json(value):
    return decode(value.encode() if isinstance(value, str) else value) if isinstance(value, (str, bytes)) else deepcopy(value)


def _value(value):
    if isinstance(value, Decimal): return float(value)
    if isinstance(value, bytes): return value.decode()
    if hasattr(value, 'isoformat'): return value.isoformat()
    return value


def _text(value, name, maximum=250):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise Problem(422, 'INVALID_QUERY', f'{name} must be a nonempty string of at most {maximum} characters.')
    return value


def _trait(value):
    trait = _text(value, 'trait_id')
    return 'KPN.TRAIT:' + trait.rsplit(':', 1)[1] if re.fullmatch(r'trait:kpn:[0-9]{7}', trait) else trait


IDENTIFIERS = ('factor_id', 'trait_id', 'gene_set_id')


def _foreign(arguments):
    """Whether an identifier argument is not ASCII: no ascii_bin id column holds one, and MySQL refuses the comparison (1267),
    so the identifier is not found, without a statement."""
    return any(isinstance(arguments.get(key), str) and not arguments[key].isascii() for key in IDENTIFIERS)


def _timed_out(error):
    """MAX_EXECUTION_TIME stopped the statement (MySQL ER_QUERY_TIMEOUT)."""
    return bool(getattr(error, 'args', None)) and error.args[0] == SCAN_TIMEOUT


def _no_table(error):
    """A reload-era table cleanup dropped (MySQL 1146): the generation is no longer readable, a conflict rather than an error."""
    return bool(getattr(error, 'args', None)) and error.args[0] == NO_TABLE


def _unretained():
    return Problem(409, 'SOURCE_UNAVAILABLE', 'The pinned reference generation is not retained for research queries.')


def _release_pin(release):
    """A published release as query()'s pin: its generation_id is the release id (the bindings' convention), its model KPN."""
    manifest = release['manifest'] if isinstance(release['manifest'], dict) else {}
    return {'generation_id': release['release_id'], 'release_id': release['release_id'], 'kind': RELEASE, 'source_kind': RELEASE,
            'model': KPN_MODEL, 'status': 'published', 'published_at': release['published_at'],
            'eaggl_import_id': manifest.get('eaggl_import_id'), 'legacy_mapping_run_id': None, 'legacy_gene_set_import_id': None,
            'manifest': manifest}


@dataclass(frozen=True)
class QueryCapture:
    result: dict
    raw: bytes
    source_mode: str
    source: dict
    # Exact upstream pages are retained separately from normalized observations.
    extra_files: dict[str, bytes] = field(default_factory=dict)

    def materialize(self, dapper):
        """Return package-compatible artifacts, exact bytes and canonical Files.

        The outer capture is eligible evidence only if its explicit outcome says
        observations were returned. Capability/error responses are never evidence.
        """
        identity = sha256(self.raw)
        key = 'research-' + identity
        name = key + '.json'
        path = 'sources/' + name
        node = dapper.file(name, self.raw, 'application/json')
        source = {'path': path, 'sha256': identity, 'size_bytes': len(self.raw), 'format': 'json',
                  'media_type': 'application/json', 'dapper_file_id': node['id'],
                  'origin': self.source['origin'], 'source_mode': self.source_mode,
                  'research_capture': deepcopy(self.source),
                  'eligible_evidence': self.result.get('status') in ('complete', 'partial') and bool(self.result.get('items'))}
        artifacts, files, nodes = {key: source}, {path: self.raw}, [node]
        for extra_name, raw in sorted(self.extra_files.items()):
            extra_key = 'research-raw-' + sha256(raw)
            extra_path = 'sources/' + extra_key + '.json'
            extra_node = dapper.file(extra_key + '.json', raw, 'application/json')
            artifacts[extra_key] = {'path': extra_path, 'sha256': sha256(raw), 'size_bytes': len(raw),
                'format': 'json', 'media_type': 'application/json', 'dapper_file_id': extra_node['id'],
                'origin': self.source['origin'], 'source_mode': self.source_mode,
                'capture_segment': extra_name, 'eligible_evidence': source['eligible_evidence']}
            files[extra_path] = raw
            if extra_node['id'] not in {n['id'] for n in nodes}: nodes.append(extra_node)
        eligible = [n['id'] for n in nodes] if source['eligible_evidence'] else []
        context = {'files': nodes}
        resolutions = []
        # Only a full authoritative definition can introduce a source GeneSet.
        # A membership page never mints a truncated set or invents dependencies.
        envelope = decode(self.raw)
        require(envelope.get('result') == self.result and envelope.get('source') == self.source,
                'Capture metadata differs from its exact retained bytes')
        if self.source_mode == 'imported_reference' and envelope.get('operation') == 'get_factor':
            for item in self.result.get('items', []):
                resolution = {'id': item.get('public_id') or item.get('eaggl_factor_id'),
                    'source_key': item.get('eaggl_factor_id'), 'reference_generation_id': self.source.get('generation_id'),
                    'eaggl_import_id': self.source.get('eaggl_import_id'), 'status': 'unavailable'}
                if self.source.get('model') == KPN_MODEL and item.get('trait_metadata'):
                    fit = parse_public_id(item['public_id'])
                    require(fit['factor_key'] == item['factor_key'] and fit['kpn_trait_id'] == item['kpn_trait_id'],
                            'Captured factor identifiers disagree')
                    trait = item['trait_metadata']
                    require(trait['kpn_trait_id'] == item['kpn_trait_id'], 'Captured factor trait differs')
                    exact = mechanism_node(item['public_id'], trait['phenotype_name'], item['kpn_trait_id'],
                        fit['factor'], item['label'], identity_version=self.source.get('mechanism_identity_version', 1),
                        eaggl_import_id=self.source['eaggl_import_id'])
                elif self.source.get('model') == LEGACY_MODEL and item.get('public_id'):
                    parts = item['public_id'].split(':')
                    raw = (item.get('mapping') or {}).get('raw')
                    exact = ({'name': f'{parts[2]} mechanism {parts[4]}',
                        'description': f'EAGGL mechanism {item["public_id"]}. Source label: {raw["label"]}.'}
                        if len(parts) == 5 and isinstance(raw, dict) and isinstance(raw.get('label'), str) else None)
                else:
                    exact = None
                if exact:
                    exact['id'] = dapper.compute_id(exact, 'Mechanism', dapper.schema)
                    context.setdefault('mechanisms', []).append(exact)
                    resolution.update(status='ready', dapper_id=exact['id'], object=exact,
                        source_ref={'artifact_id': key, 'pointer': '/result/items/0'},
                        identity_version=self.source.get('mechanism_identity_version', 1))
                else:
                    resolution['reason'] = 'Exact pinned trait metadata or legacy source mapping is unavailable.'
                resolutions.append(resolution)
        if self.source_mode == 'imported_reference' and envelope.get('operation') == 'get_gene_set':
            from .reference_evidence import _resolve_gene_set, _class_groups, _class_of
            groups = _class_groups(dapper)
            for item in self.result.get('items', []):
                if 'metadata' in item:
                    row, collection = item, item.get('payload')
                else:
                    node = item.get('payload') or {}
                    row = {'gene_set_id': node.get('id'), 'metadata': {'dapper_gene_set': node}}
                    collection = {'provenance': item.get('provenance') or {}}
                edges = {}
                exact, dependencies, prefixes, reason = _resolve_gene_set(dapper, row, collection, {}, groups, edge_groups=edges)
                resolutions.append({'id': row.get('gene_set_id'), 'status': 'ready' if exact else 'unavailable',
                    'dapper_id': exact['id'] if exact else None, 'reason': reason,
                    'reference_generation_id': self.source['generation_id'],
                    'source_ref': {'artifact_id': key, 'pointer': '/result/items/0/' +
                        ('metadata/dapper_gene_set' if 'metadata' in item else 'payload')},
                    'membership_status': 'loaded' if exact and 'members' in exact else 'not_loaded',
                    'construction_provenance_status': 'generating_activity_loaded' if any(
                        _class_of(dependency['id']) == 'Activity' for dependency in dependencies) else 'not_loaded',
                    'dependency_ids': sorted(dependency['id'] for dependency in dependencies),
                    'provenance_note': 'Only retained upstream provenance is included; missing membership or construction history is not inferred.'})
                if exact:
                    context.setdefault('prefixes', {}).update(prefixes)
                    context.setdefault('gene_sets', []).append(exact)
                    for group, rows in edges.items():
                        target = context.setdefault(group, [])
                        target.extend(edge for edge in rows if edge not in target)
                    for dependency in dependencies:
                        target = context.setdefault(groups[_class_of(dependency['id'])], [])
                        if dependency['id'] not in {node['id'] for node in target}: target.append(dependency)
        return {'source_artifacts': artifacts, 'dapper_context': context, 'files': files,
                'eligible_source_ids': eligible,
                'cfde_source_ids': eligible if self.source_mode == 'imported_reference' else [],
                'object_resolution': resolutions, 'reference_objects': resolutions,
                'source_ref': {'artifact_id': key, 'pointer': '/result/items'}, 'dapper_file_id': source['dapper_file_id']}


class ReferenceQueryService:
    def __init__(self, connection_factory, *, cursor_secret=None, max_capture_bytes=MAX_CAPTURE_BYTES, single_snapshot=False, prefix=None):
        self.connection_factory = connection_factory
        self.cursor_secret = cursor_secret.encode() if isinstance(cursor_secret, str) else cursor_secret
        self.max_capture_bytes = max_capture_bytes
        self.prefix = prefix  # the environment's table prefix (application_prefix) of its reveal_ref_* tables
        # single_snapshot: every read of this instance shares one read view (an assessment build), so a generation row
        # or factor lookup it already made is reused, as a copy, instead of read again.
        self._memo = {} if single_snapshot else None

    def _remembered(self, key, read):
        if self._memo is None: return read()
        if key not in self._memo: self._memo[key] = read()
        return deepcopy(self._memo[key])

    @staticmethod
    def _rows(connection, sql, parameters, columns):
        with connection.cursor() as cursor:
            cursor.execute(sql, tuple(parameters))
            rows = cursor.fetchall()
        result = []
        for row in rows:
            if len(row) != len(columns): raise ValueError('Unexpected imported reference row shape')
            item = {k: _value(v) for k, v in zip(columns, row)}
            for key in ('metadata', 'manifest', 'payload', 'provenance'):
                if key in item: item[key] = _json(item[key]) if item[key] is not None else None
            result.append(item)
        return result

    def _published(self, connection):
        """The environment's published release ({release_id, published_at, manifest}) or None: read on connection, so a query
        reads it first and every later statement of its transaction is of that release."""
        return self._remembered(('release',), lambda: current_release(connection, self.prefix))

    def _pin(self, connection, identity):
        """The published release (_release_pin) when identity is it, else the retained reload-era generation."""
        if not isinstance(identity, str) or not GENERATION_RE.fullmatch(identity):
            raise Problem(422, 'INVALID_QUERY', 'An exact reference generation is required.')
        published = self._published(connection)
        if published and published['release_id'] == identity: return _release_pin(published)
        return self._generation(connection, identity, published)

    def release(self, connection, release_id):
        """The published release as a pin; superseded_problem when release_id is not it (a later publish replaced it)."""
        published = self._published(connection)
        if not published or published['release_id'] != release_id:
            raise superseded_problem(ReleaseUnavailable(release_id, published and published['release_id']))
        return _release_pin(published)

    def _generation(self, connection, identity, published=None):
        """A retained reload-era generation. Without one (or without the reload tables, MySQL 1146), a pin while a release is
        published is a superseded release (superseded_problem); otherwise the generation is not retained."""
        if not isinstance(identity, str) or not GENERATION_RE.fullmatch(identity):
            raise Problem(422, 'INVALID_QUERY', 'An exact reference generation is required.')
        columns = ('generation_id', 'kind', 'model', 'status', 'eaggl_import_id', 'legacy_mapping_run_id', 'legacy_gene_set_import_id', 'manifest')
        try:
            rows = self._remembered(('generation', identity), lambda: self._rows(connection,
                'SELECT ' + ','.join(columns) + ' FROM reference_generations WHERE generation_id=%s', [identity], columns))
        except Exception as error:
            if not _no_table(error): raise
            rows = []
        if not rows and published: raise superseded_problem(ReleaseUnavailable(identity, published['release_id']))
        if not rows or rows[0]['status'] not in ('complete', 'superseded'): raise _unretained()
        return rows[0]

    def _release_factors(self, connection, identities):
        """reveal_ref_factors rows matching any of identities (public id, factor key or EAGGL factor id: three unique keys)."""
        return self._rows(connection, application_sql(RELEASE_FACTOR_LOOKUP.format(marks=','.join(['%s'] * len(identities))),
                                                      application_prefix(self.prefix)), list(identities) * 3, RELEASE_FACTOR_COLUMNS)

    def _kpn_factors(self, connection, generation, import_id, identities):
        """reference_factors rows matching any of identities, each with its import factor_index. Rows of an import
        not keyed by SHA-256(factor_id) are read again with the exact text join, per identity."""
        found = self._rows(connection, KPN_FACTOR_LOOKUP.format(marks=','.join(['%s'] * len(identities))),
                           [generation, *identities] * 3 + [generation, import_id], KPN_FACTOR_COLUMNS)
        if all(row['factor_index'] is not None for row in found): return found
        return [row for identity in identities for row in self._rows(connection, KPN_FACTOR_TEXT_JOIN,
                [import_id, generation, identity, identity, identity], KPN_FACTOR_COLUMNS)]

    def _bounded(self, result, limit):
        """query()'s row and byte budget, in place: at most limit items (the rest flagged truncated), and the status."""
        items = result.get('items', [])
        has_more = result.pop('_more', False)
        if len(items) > limit: items, has_more = items[:limit], True
        result['items'] = items
        # Bound serialized bytes as well as row count. Oversize singular
        # authoritative objects are unavailable; never truncate their identity.
        while len(canonical_json(result)) > self.max_capture_bytes - 20_000 and items:
            items.pop(); has_more = True
        if has_more and not items:
            result.update(status='unavailable', reason='record_exceeds_capture_budget'); has_more = False
        result.setdefault('status', 'partial' if has_more else 'complete' if items else 'empty')
        return items, has_more

    def factor_tops(self, connection, generation, identities, limit):
        """What query('get_factor') and query('get_factor_loadings', kind=gene|gene_set, metric='joint', limit=limit)
        return for each KPN identity, read in three statements for every identity instead of nine each: the
        factors, then the top genes and the top gene sets of all of them (one bounded branch per factor). For each
        identity: its factor items, or the Problem query() would raise, then per kind the result's items, status,
        truncated, import_coverage and origin. Call it only inside the caller's single read view. A release pin (_release_pin)
        reads its reveal_ref_* tables, as query() does for it."""
        for identity in identities: _text(identity, 'factor_id')
        gen, imp, release = generation['generation_id'], generation['eaggl_import_id'], generation.get('source_kind') == RELEASE
        lookup = (lambda values: self._release_factors(connection, values)) if release else \
            (lambda values: self._kpn_factors(connection, gen, imp, values))
        # A non-ASCII identity matches no ascii_bin key (_foreign): it is not found, and never sent to MySQL (1267).
        plain = [identity for identity in identities if identity.isascii()]
        rows = lookup(plain) if plain else []
        factors, found = {}, []
        for identity in identities:
            matches = [row for row in rows if identity in (row['public_id'], row['factor_key'], row['eaggl_factor_id'])]
            if not matches and identity.isascii():  # an id the exact SQL comparison may still match: the single lookup decides
                matches = lookup([identity])
            if len(matches) > 1:
                factors[identity] = Problem(409, 'AMBIGUOUS_IDENTITY', 'The imported factor identifier is ambiguous.'); continue
            result = {'items': deepcopy(matches[:1]), 'import_coverage': 'Exact imported factor metadata. Crosswalks describe routing correspondence, not scientific equivalence.'}
            factors[identity] = self._bounded(result, 1)[0]
            if factors[identity]: found.append((identity, factors[identity][0]))
        tops = {}
        specs = ((('gene', RELEASE_GENE_TABLES, RELEASE_GENE_COVERAGE, RELEASE_GENE_ORDER, ('symbol', 'loading'),
                   'SELECT %s AS anchor,gene AS symbol,loading FROM reveal_ref_factor_genes WHERE factor_key=%s', 'loading DESC,symbol'),
                  ('gene_set', RELEASE_SET_TABLES, RELEASE_SET_COVERAGE, RELEASE_SET_ORDER, SET_COLUMNS, 'SELECT %s AS anchor,' + SET_SELECT +
                   ' FROM reveal_ref_projections p JOIN reveal_ref_gene_sets s ON s.gene_set_id=p.gene_set_id WHERE p.factor_key=%s',
                   'joint_loading DESC,gene_set_id')) if release else (
                ('gene', GENE_LOADING_TABLES, GENE_COVERAGE, GENE_ORDER, ('symbol', 'gene_index', 'loading'),
                 'SELECT %s AS anchor,g.symbol,g.gene_index,l.loading FROM eaggl_gene_loadings l JOIN eaggl_genes g'
                 ' ON g.import_id=l.import_id AND g.gene_index=l.gene_index WHERE l.import_id=%s AND l.factor_index=%s', 'loading DESC,symbol,gene_index'),
                ('gene_set', SET_LOADING_TABLES, SET_COVERAGE, SET_ORDER, SET_COLUMNS,
                 'SELECT %s AS anchor,s.gene_set_id,s.gene_set_name,s.library,p.joint_loading,p.marginal_loading,p.joint_loading_text,'
                 'p.marginal_loading_text,p.joint_rank,p.marginal_rank,p.is_joint_top_factor FROM factor_gene_set_projections p'
                 ' JOIN cfde_gene_sets s ON s.generation_id=p.generation_id AND s.gene_set_id=p.gene_set_id'
                 ' WHERE p.generation_id=%s AND p.scope=%s AND p.factor_key=%s', 'joint_loading DESC,gene_set_id')))
        for kind, tables, coverage, order, columns, branch, final in specs:
            if not found: break
            # query() filters LOWER(name) LIKE '%%' for an empty search: true for every NOT NULL symbol and set name.
            sql = ' UNION ALL '.join('SELECT * FROM (' + branch + ' ORDER BY ' + order + ' LIMIT %s) a' + str(position)
                                     for position in range(len(found))) + ' ORDER BY anchor,' + final
            parameters = [value for position, (_, item) in enumerate(found) for value in
                          ((position, item['factor_key']) if release else (position, imp, item['factor_index']) if kind == 'gene'
                           else (position, gen, 'per_trait', item['factor_key'])) + (limit + 1,)]
            grouped = [[] for _ in found]
            for row in self._rows(connection, application_sql(sql, application_prefix(self.prefix)), parameters, ('anchor', *columns)):
                grouped[row.pop('anchor')].append(row)
            for (identity, _), items in zip(found, grouped):
                if release and kind == 'gene': items = [{'symbol': row['symbol'], 'gene_index': None, 'loading': row['loading']} for row in items]
                result = {'items': items, 'import_coverage': coverage, 'ordering': order, 'total_rows': None}
                items, truncated = self._bounded(result, limit)
                tops[(identity, kind)] = {'items': items, 'status': result['status'], 'truncated': truncated, 'import_coverage': coverage,
                    'origin': 'mysql:' + '+'.join(['reveal_ref_release', 'reveal_ref_factors', *tables] if release else
                                                  ['reference_generations', *KPN_FACTOR_TABLES, *tables]) + '?generation_id=' + gen}
        return factors, tops

    def catalog(self, generation_id):
        connection = self.connection_factory()
        try:
            generation = self._pin(connection, generation_id)
            # Catalogs expose scientific source identity, never backend manifest
            # paths or operator-only import configuration.
            public = {key: value for key, value in generation.items() if key != 'manifest'}
            public['generation_manifest_sha256'] = sha256(canonical_json(generation['manifest']))
            if generation.get('source_kind') == RELEASE: public['absent_files'] = sorted(generation['manifest'].get('absent_files') or [])
            return capability_catalog(public, source_kind=generation.get('source_kind'))
        finally: connection.close()

    def phenotype_trait(self, generation_id, phenotype):
        """The pinned KPN trait whose kpn_trait_id or legacy (BioIndex) phenotype id is phenotype, or None. A release also
        resolves the linkage's other KPN phenotypes that have a legacy phenotype id."""
        connection = self.connection_factory()
        try:
            generation = self._pin(connection, generation_id)
            if generation['model'] != KPN_MODEL: return None
            columns = ('kpn_trait_id', 'legacy_phenotype_id', 'phenotype_name', 'gwas_source_category')
            # A non-ASCII phenotype is in no ascii_bin column (MySQL refuses the comparison, 1267): only a linkage phenotype's
            # legacy id (utf8mb4) can hold it.
            plain = isinstance(phenotype, str) and phenotype.isascii()
            if generation.get('source_kind') == RELEASE:
                phenotypes = ('SELECT kpn_trait_id,legacy_phenotype_id,display_name,category FROM reveal_ref_phenotypes'
                              ' WHERE is_atlas=0 AND kpn_trait_id IS NOT NULL AND legacy_phenotype_id IS NOT NULL AND {}')
                if plain:  # the OR parenthesized: an atlas trait's phenotype row (is_atlas 1) is never a second match
                    select = ('SELECT kpn_trait_id,legacy_phenotype_id,phenotype_name,gwas_source_category FROM reveal_ref_traits WHERE {} UNION '
                              + phenotypes).format('kpn_trait_id=%s OR legacy_phenotype_id=%s', '(kpn_trait_id=%s OR legacy_phenotype_id=%s)')
                else: select = phenotypes.format('legacy_phenotype_id=%s')
                rows = self._rows(connection, application_sql(select, application_prefix(self.prefix)), [phenotype] * (4 if plain else 1), columns)
            elif not plain: return None  # kpn_traits keeps both ids in ascii_bin columns
            else:
                select = 'SELECT ' + ','.join(columns) + ' FROM kpn_traits WHERE generation_id=%s AND '
                try: rows = self._rows(connection, select + 'kpn_trait_id=%s UNION ' + select + 'legacy_phenotype_id=%s',
                                       [generation_id, phenotype, generation_id, phenotype], columns)
                except Exception as error:
                    if not _no_table(error): raise
                    raise _unretained() from None
            if len(rows) > 1: raise Problem(409, 'AMBIGUOUS_IDENTITY', 'The phenotype identifier is ambiguous in the pinned generation.')
            return rows[0] if rows else None
        finally:
            try: connection.rollback()
            except AttributeError: pass
            finally: connection.close()

    def _cursor(self, binding, position=None, value=None):
        if not self.cursor_secret:
            raise Problem(503, 'SOURCE_NOT_READY', 'A durable cursor signing secret is required for research pagination.')
        if value is not None:
            try:
                if not isinstance(value, str) or len(value) > 2000: raise ValueError()
                encoded, signature = value.split('.')
                expected = hmac.new(self.cursor_secret, encoded.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected): raise ValueError()
                payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
                if payload['binding'] != binding or type(payload['position']) is not int or payload['position'] < 0: raise ValueError()
                return payload['position']
            except (ValueError, KeyError, TypeError):
                raise Problem(422, 'INVALID_CURSOR', 'The cursor belongs to a different pinned research query.') from None
        encoded = base64.urlsafe_b64encode(canonical_json({'binding': binding, 'position': position})).decode().rstrip('=')
        return encoded + '.' + hmac.new(self.cursor_secret, encoded.encode(), hashlib.sha256).hexdigest()

    def query(self, operation, arguments, *, generation_id):
        if operation not in OPERATIONS or not isinstance(arguments, dict) or set(arguments) - OPERATIONS[operation] - PAGE_FIELDS:
            raise Problem(422, 'INVALID_QUERY', 'Unknown reference operation or arguments.')
        args = deepcopy(arguments)
        limit = args.pop('limit', 25); cursor = args.pop('cursor', None)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise Problem(422, 'INVALID_QUERY', 'Use a page limit between 1 and 500.')
        for key, value in args.items():
            if key == 'q' and value == '': continue
            _text(value, key)
        if cursor is not None and not self.cursor_secret: self._cursor(None, value=cursor)  # no signing secret: refused before any read
        connection = self.connection_factory()
        try:
            generation = self._pin(connection, generation_id)  # first statement: the published release
            release = generation.get('source_kind') == RELEASE
            version = RELEASE_READER_VERSION if release else READER_VERSION
            binding = sha256(canonical_json([version, generation_id, operation, args, limit]))
            offset = self._cursor(binding, value=cursor) if cursor is not None else 0
            try: result, tables = (self._release if release else self._execute)(connection, generation, operation, args, limit, offset)
            except Exception as error:
                if release or not _no_table(error): raise
                raise _unretained() from None  # its reference_generations row outlived the tables it reads
            items, has_more = self._bounded(result, limit)
            result.update(returned_rows=len(items), limit=limit, offset=offset, truncated=has_more,
                          next_cursor=self._cursor(binding, offset + len(items)) if has_more else None)
            source = {'origin': 'mysql:' + '+'.join(tables) + '?generation_id=' + generation_id,
                      'kind': 'mysql', 'tables': tables, 'generation_id': generation_id,
                      'model': generation['model'], 'eaggl_import_id': generation['eaggl_import_id'],
                      'mapping_run_id': generation['legacy_mapping_run_id'],
                      'gene_set_import_id': generation['legacy_gene_set_import_id'],
                      'generation_manifest_sha256': sha256(canonical_json(generation['manifest']))}
            if generation['model'] == KPN_MODEL:
                # A release mints as the catalog does: its manifest's identity version (1 unless it opts in) and EAGGL import.
                source['mechanism_identity_version'] = generation['manifest'].get('mechanism_identity_version', 1)
            if release: source.update(release_id=generation_id, source_kind=RELEASE)
            envelope = {'format': CAPTURE_FORMAT, 'reader_version': version,
                        'source_mode': 'imported_reference', 'source': source,
                        'operation': operation, 'arguments': {**args, 'limit': limit, 'offset': offset},
                        'metric_definitions': METRICS, 'result': result}
            raw = canonical_json(envelope)
            if len(raw) > self.max_capture_bytes: raise Problem(413, 'QUERY_TOO_LARGE', 'The reference capture exceeds its byte budget.')
            return QueryCapture(result, raw, 'imported_reference', source)
        finally:
            try: connection.rollback()
            except AttributeError: pass
            finally: connection.close()

    def _release(self, c, g, op, a, limit, offset):
        """_execute's operations on the published release (reveal_ref_*): ranks are per library, genes have no import index,
        only referenced gene sets are stored, and the betas, factor-trait links and phenotypes are read; there is no graph."""
        release, prefix, tables = g['generation_id'], application_prefix(self.prefix), ['reveal_ref_release']
        def rows(sql, params, columns, involved):
            for table in involved:
                if table not in tables: tables.append(table)
            return self._rows(c, application_sql(sql, prefix), params, columns)
        def page(sql, params, columns, involved, coverage, order):
            data = rows(sql + ' ORDER BY ' + order + ' LIMIT %s OFFSET %s', [*params, limit + 1, offset], columns, involved)
            return {'items': data, 'import_coverage': coverage, 'ordering': order, 'total_rows': None}, tables
        def missing(reason='The requested records are not stored in this release.'):
            return {'status': 'not_stored', 'items': [], 'reason': reason, 'import_coverage': 'No stored observation; this is not a biological negative.'}, tables
        def factor(identity):
            _text(identity, 'factor_id')
            if 'reveal_ref_factors' not in tables: tables.append('reveal_ref_factors')
            found = self._remembered(('factor', release, identity), lambda: self._release_factors(c, [identity]))
            if len(found) > 1: raise Problem(409, 'AMBIGUOUS_IDENTITY', 'The imported factor identifier is ambiguous.')
            return found[0] if found else None
        def scope():
            value = a.get('scope', 'all')
            if value not in ('atlas', 'all'): raise Problem(422, 'INVALID_QUERY', 'Use scope atlas or all.')
            return value
        search = a.get('q', '').lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
        like = '%' + search + '%'
        if op in GRAPH_OPERATIONS: return missing(NO_GRAPH)
        if RELEASE_FILES.get(op) in (g['manifest'].get('absent_files') or ()):
            return missing(f'This release was published without {RELEASE_FILES[op]}.')
        if _foreign(a): return missing()
        if op in ('get_factor', 'get_factor_loadings', 'get_factor_trait_links'):
            item = factor(a.get('factor_id'))
            if not item: return missing()
            if op == 'get_factor':
                traits = rows('SELECT ' + ','.join(TRAIT_COLUMNS) + ' FROM reveal_ref_traits WHERE kpn_trait_id=%s', [item['kpn_trait_id']],
                              TRAIT_COLUMNS, ['reveal_ref_traits'])
                item['trait_metadata'] = traits[0] if traits else None
                return {'items': [item], 'import_coverage': 'Exact imported factor metadata. Crosswalks describe routing correspondence, not scientific equivalence.'}, tables
            if op == 'get_factor_trait_links':
                kept = scope()
                result, tables = page(LINK_SQL + 'l.factor_key=%s' + (' AND l.is_atlas_trait=1' if kept == 'atlas' else ''), [item['factor_key']],
                                      LINK_COLUMNS, LINK_TABLES, LINK_COVERAGE, 'l.phewas_q,l.phenotype')
                summary = (item.get('metadata') or {}).get('trait_links')
                result.update(factor={'factor_id': item['public_id'], 'factor_key': item['factor_key'], 'label': item['label']}, scope=kept,
                              summary={key: summary.get(key) for key in LINK_SUMMARY} if isinstance(summary, dict) else None)
                for row in result['items']:
                    row.update(phenotype_kpn_trait_id=row['phenotype_kpn_trait_id'] or None, is_own_trait=bool(row['is_own_trait']),
                               is_atlas_trait=bool(row['is_atlas_trait']))
                return result, tables
            kind, metric = a.get('kind', 'gene'), a.get('metric', 'joint')
            if kind not in ('gene', 'gene_set') or metric not in ('joint', 'marginal'):
                raise Problem(422, 'INVALID_QUERY', 'Use gene/gene_set and joint/marginal.')
            if kind == 'gene':
                result, tables = page("SELECT gene,loading FROM reveal_ref_factor_genes WHERE factor_key=%s AND LOWER(gene) LIKE %s ESCAPE '!'",
                    [item['factor_key'], like], ('symbol', 'loading'), RELEASE_GENE_TABLES, RELEASE_GENE_COVERAGE, RELEASE_GENE_ORDER)
                result['items'] = [{'symbol': row['symbol'], 'gene_index': None, 'loading': row['loading']} for row in result['items']]
                return result, tables
            order = 'p.marginal_loading' if metric == 'marginal' else 'p.joint_loading'
            return page('SELECT ' + SET_SELECT + ' FROM reveal_ref_projections p JOIN reveal_ref_gene_sets s ON s.gene_set_id=p.gene_set_id'
                " WHERE p.factor_key=%s AND LOWER(s.gene_set_name) LIKE %s ESCAPE '!'", [item['factor_key'], like], SET_COLUMNS,
                RELEASE_SET_TABLES, RELEASE_SET_COVERAGE, order + ' DESC,p.gene_set_id')
        if op == 'search_factors':
            # eaggl_factor_id is ascii_bin: a non-ASCII search can match only the label (MySQL refuses the comparison, 1267).
            ids = (" OR LOWER(eaggl_factor_id) LIKE %s ESCAPE '!'", [like]) if search.isascii() else ('', [])
            return page('SELECT public_id,factor_key,eaggl_factor_id,kpn_trait_id,label,source_revision FROM reveal_ref_factors'
                " WHERE LOWER(label) LIKE %s ESCAPE '!'" + ids[0], [like, *ids[1]],
                ('public_id', 'factor_key', 'eaggl_factor_id', 'kpn_trait_id', 'label', 'source_revision'), ['reveal_ref_factors'],
                'All factors stored in this release.', 'public_id')
        if op in ('search_genes', 'resolve_gene'):
            # The release stores each factor's nonzero loadings by symbol: its genes are the distinct loaded symbols.
            clause, value = ("LOWER(gene) LIKE %s ESCAPE '!'", like) if op == 'search_genes' else ('gene=%s', _text(a.get('gene'), 'gene'))
            result, tables = page('SELECT DISTINCT gene FROM reveal_ref_factor_genes WHERE ' + clause, [value], ('symbol',),
                                  RELEASE_GENE_TABLES, RELEASE_GENE_SEARCH_COVERAGE, 'gene')
            result['items'] = [{'gene_index': None, 'symbol': row['symbol'], 'namespace': 'EAGGL symbol', 'external_identity_mapping': 'not_resolved',
                                'organism': None, 'source_local_id': f'urn:reveal:release-gene:{release}:{row["symbol"]}'} for row in result['items']]
            if op == 'resolve_gene':
                from .gene_identity import resolve_pinned_gene
                for item in result['items']:
                    try:
                        item['identity_mapping'] = resolve_pinned_gene(item['symbol'], taxon=a.get('taxon'),
                            mapping_revision=a.get('mapping_revision'), source_import=release, source_index=None)
                    except (ValueError, OSError) as exc:
                        raise Problem(409, 'MAPPING_REVISION_UNAVAILABLE', str(exc)) from None
                    item['external_identity_mapping'] = item['identity_mapping']['status']
                    item['organism_context'] = {'taxon': a.get('taxon'), 'basis': 'explicit_query_parameter' if a.get('taxon') else 'unknown'}
            return result, tables
        if op == 'get_gene_factors':
            return page('SELECT f.eaggl_factor_id,f.public_id,f.kpn_trait_id,t.legacy_phenotype_id,f.label,g.loading FROM reveal_ref_factor_genes g'
                ' JOIN reveal_ref_factors f ON f.factor_key=g.factor_key JOIN reveal_ref_traits t ON t.kpn_trait_id=f.kpn_trait_id WHERE g.gene=%s',
                [_text(a.get('gene'), 'gene')], ('factor_id', 'public_id', 'kpn_trait_id', 'trait', 'label', 'loading'),
                ['reveal_ref_factor_genes', 'reveal_ref_factors', 'reveal_ref_traits'], RELEASE_GENE_COVERAGE, 'g.loading DESC,g.factor_key')
        if op == 'get_gene_gene_sets':
            gene = _text(a.get('gene'), 'gene')
            symbol = gene[12:] if gene[:12].upper() == 'HGNC.SYMBOL:' else gene
            if not symbol or any(c in symbol for c in ':"\\'):
                raise Problem(422, 'INVALID_QUERY', 'Use a gene symbol or an HGNC.SYMBOL CURIE.')
            # Portable prefilter on the stored members' JSON text (a member string ending in :<symbol>), checked exactly below.
            members = "JSON_EXTRACT(s.metadata,'$.dapper_gene_set.members')"
            pattern = '%' + (':' + symbol + '"').lower().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            columns, select, scoped = ('gene_set_id', 'name', 'library', 'n_genes'), 's.gene_set_id,s.gene_set_name,s.library,s.n_genes,', {}
            if 'factor_id' in a:
                item = factor(a['factor_id'])
                if not item: return missing()
                columns += SET_COLUMNS[3:]
                sql = ('SELECT ' + select + ''.join('p.' + column + ',' for column in SET_COLUMNS[3:]) + members + ' FROM reveal_ref_projections p'
                       ' JOIN reveal_ref_gene_sets s ON s.gene_set_id=p.gene_set_id WHERE p.factor_key=%s AND ')
                params, involved = [item['factor_key']], RELEASE_SET_TABLES
                coverage, order = RELEASE_MEMBERSHIP_COVERAGE + ' ' + RELEASE_SET_COVERAGE, RELEASE_SET_ORDER
                scoped = {'factor': {'factor_id': item['public_id'], 'factor_key': item['factor_key'], 'label': item['label']}}
            else:  # every stored gene set's members: bounded in time (SCAN_HINT)
                sql, params, involved = 'SELECT ' + SCAN_HINT + select + members + ' FROM reveal_ref_gene_sets s WHERE ', [], ['reveal_ref_gene_sets']
                coverage, order = RELEASE_MEMBERSHIP_COVERAGE, 's.n_genes,s.gene_set_id'
            try:
                result, tables = page(sql + 'LOWER(' + members + ") LIKE %s ESCAPE '!'", [*params, pattern], (*columns, 'members'), involved,
                                      coverage, order)
            except Exception as error:
                if scoped or not _timed_out(error): raise
                return {'status': 'unavailable', 'items': [], 'reason': 'scan_budget_exceeded', 'gene': symbol,
                        'import_coverage': SCAN_BUDGET_COVERAGE}, tables
            found = []
            for row in result['items']:
                stored = _json(row.pop('members')) or []
                index = next((i for i, member in enumerate(stored) if isinstance(member, str)
                              and member.rsplit(':', 1)[-1].lower() == symbol.lower()), None)
                if index is not None: found.append({**row, 'member': stored[index], 'source_pointer': '/members/' + str(index)})
            result.update(items=found, gene=symbol, _more=len(result['items']) > limit, **scoped)
            return result, tables
        if op in ('search_traits', 'get_trait'):
            if op == 'search_traits':
                where, params = ("LOWER(phenotype_name) LIKE %s ESCAPE '!'", "LOWER(display_name) LIKE %s ESCAPE '!'"), [like, like]
            else:
                trait = _trait(a.get('trait_id'))
                where, params = ('kpn_trait_id=%s', 'kpn_trait_id=%s'), [trait, trait]
            result, tables = page(RELEASE_TRAITS.format(traits=where[0], phenotypes=where[1]), params, (*TRAIT_COLUMNS, 'is_atlas'),
                                  ['reveal_ref_traits', 'reveal_ref_phenotypes'], RELEASE_TRAIT_COVERAGE, 'kpn_trait_id')
            for row in result['items']: row['is_atlas'] = bool(row['is_atlas'])
            return result, tables
        if op in ('get_trait_gene_sets', 'get_gene_set_traits'):
            if op == 'get_gene_set_traits':
                identity = _text(a.get('gene_set_id'), 'gene_set_id')
                result, tables = page(BETA_SQL + 't.gene_set_id=%s', [identity], BETA_COLUMNS, BETA_TABLES, BETA_COVERAGE,
                                      't.beta_uncorrected DESC,t.library_rank,t.kpn_trait_id')
                result['gene_set_id'] = identity
                return result, tables
            trait, clauses, params = _trait(a.get('trait_id')), ['t.kpn_trait_id=%s'], []
            params.append(trait)
            if 'library' in a: clauses.append('t.library=%s'); params.append(_text(a['library'], 'library', 64))
            clauses.append("LOWER(s.gene_set_name) LIKE %s ESCAPE '!'"); params.append(like)
            result, tables = page(BETA_SQL + ' AND '.join(clauses), params, BETA_COLUMNS, BETA_TABLES, BETA_COVERAGE,
                                  't.beta_uncorrected DESC,t.library_rank,t.gene_set_id')
            result.update(trait_id=trait, library=a.get('library'))
            return result, tables
        if op == 'get_trait_factor_links':
            trait, kept = _trait(a.get('trait_id')), scope()
            result, tables = page(LINK_SQL + 'l.phenotype_kpn_trait_id=%s' + (' AND l.is_atlas_trait=1' if kept == 'atlas' else ''), [trait],
                                  LINK_COLUMNS, LINK_TABLES, LINK_COVERAGE, 'l.phewas_q,l.factor_key')
            for row in result['items']:
                row.update(is_own_trait=bool(row['is_own_trait']), is_atlas_trait=bool(row['is_atlas_trait']))
            result.update(trait_id=trait, scope=kept)
            return result, tables
        if op in ('search_gene_sets', 'get_gene_set', 'get_gene_set_members'):
            if op == 'search_gene_sets':
                return page("SELECT gene_set_id,gene_set_name,library,n_genes,n_genes_in_eaggl_universe FROM reveal_ref_gene_sets WHERE LOWER(gene_set_name) LIKE %s ESCAPE '!'",
                    [like], ('gene_set_id', 'name', 'library', 'n_genes', 'n_genes_in_eaggl_universe'), ['reveal_ref_gene_sets'],
                    RELEASE_GENE_SET_COVERAGE, 'gene_set_id')
            identity = _text(a.get('gene_set_id'), 'gene_set_id')
            if op == 'get_gene_set':
                # The collection's payload is read only when its parsed lineage is not cached (factor_details.collection_facts).
                found = rows('SELECT s.gene_set_id,s.gene_set_name,s.library,s.n_genes,s.n_genes_in_eaggl_universe,s.metadata,s.collection_id,'
                    "c.portal_collection_id,JSON_EXTRACT(c.payload,'$.document_sha256') FROM reveal_ref_gene_sets s"
                    ' JOIN reveal_ref_collections c ON c.collection_id=s.collection_id WHERE s.gene_set_id=%s', [identity],
                    ('gene_set_id', 'name', 'library', 'n_genes', 'n_genes_in_eaggl_universe', 'metadata', 'collection_id', 'portal_collection_id',
                     'document_sha256'), ['reveal_ref_gene_sets', 'reveal_ref_collections'])
            else:
                found = rows('SELECT gene_set_id,metadata FROM reveal_ref_gene_sets WHERE gene_set_id=%s', [identity], ('gene_set_id', 'metadata'),
                             ['reveal_ref_gene_sets'])
            if not found: return missing()
            item = found[0]; node = (item['metadata'] or {}).get('dapper_gene_set')
            if node and node.get('id') != identity: raise Problem(503, 'SOURCE_NOT_READY', 'Stored gene-set object identity differs from its key.')
            if op == 'get_gene_set':
                # Only this gene set's lineage closure (producer plus curated provenance, wide fan-ins collapsed), never the
                # whole collection: a GlyGen HZ2 collection alone exceeds the capture budget. Copied: the cached lineage is shared.
                collection = item.pop('collection_id')
                facts = collection_facts(release, collection, extracted(item.pop('document_sha256')), lambda: rows(
                    'SELECT payload FROM reveal_ref_collections WHERE collection_id=%s', [collection], ('raw',), ['reveal_ref_collections'])[0]['raw'])
                closure = deepcopy(lineage_closure(node or {}, facts['lineage'], index=facts['index']))
                item['payload'] = {'collection_id': collection, 'document_sha256': facts['document_sha256'],
                                   'portal_collection_id': item.pop('portal_collection_id'), **closure}
                return {'items': [item], 'import_coverage': 'Exact release definition and the provenance closure of its construction (inputs'
                        ' sharing their onward lineage are represented by one; see collapsed); missing membership is explicit.'}, tables
            if not node or not isinstance(node.get('members'), list): return missing('Exact gene-set membership is not stored.')
            members = node['members']; selected = members[offset:offset + limit + 1]
            return {'items': [{'member': member, 'source_pointer': '/members/' + str(offset + i)} for i, member in enumerate(selected)],
                    'total_rows': len(members), 'gene_set_id': identity, 'source_object_sha256': sha256(canonical_json(node)),
                    'ordering': 'source member order', 'import_coverage': 'A page of the exact source membership; never a newly minted truncated GeneSet.'}, tables
        if op == 'get_gene_set_factors':
            return page('SELECT f.public_id,p.factor_key,' + ','.join('p.' + c for c in SET_COLUMNS[3:9]) + ',p.library FROM reveal_ref_projections p'
                ' JOIN reveal_ref_factors f ON f.factor_key=p.factor_key WHERE p.gene_set_id=%s', [_text(a.get('gene_set_id'), 'gene_set_id')],
                ('factor_id', 'factor_key', *SET_COLUMNS[3:9], 'library'), ['reveal_ref_projections', 'reveal_ref_factors'], RELEASE_SET_COVERAGE,
                'p.joint_loading DESC,p.factor_key')
        raise Problem(422, 'INVALID_QUERY', 'Unknown imported reference operation.')

    def _execute(self, c, g, op, a, limit, offset):
        gen, imp, kpn = g['generation_id'], g['eaggl_import_id'], g['model'] == KPN_MODEL
        tables = ['reference_generations']
        if g['model'] not in (KPN_MODEL, LEGACY_MODEL):
            return {'status':'not_supported_for_model','items':[],
                    'reason':'No imported reader is registered for this model.'},tables
        def rows(sql, params, columns, involved):
            for table in involved:
                if table not in tables: tables.append(table)
            return self._rows(c, sql, params, columns)
        def page(sql, params, columns, involved, coverage, order):
            data = rows(sql + ' ORDER BY ' + order + ' LIMIT %s OFFSET %s', [*params, limit + 1, offset], columns, involved)
            return {'items': data, 'import_coverage': coverage, 'ordering': order, 'total_rows': None}, tables
        def missing(reason='The requested records are not stored in this import.'):
            return {'status': 'not_stored', 'items': [], 'reason': reason, 'import_coverage': 'No stored observation; this is not a biological negative.'}, tables
        if op in RELEASE_OPERATIONS: return missing(RELEASE_ONLY)
        # Migration 008 keys are ascii_bin too; a legacy import's factor ids are utf8mb4, its gene sets ascii dapper ids.
        if _foreign(a if kpn else {'gene_set_id': a.get('gene_set_id')}): return missing()
        def factor(identity):
            _text(identity, 'factor_id')
            if kpn:
                for table in KPN_FACTOR_TABLES:
                    if table not in tables: tables.append(table)
                found = self._remembered(('factor', gen, identity), lambda: self._kpn_factors(c, gen, imp, [identity]))
            else:
                columns = ('factor_index', 'eaggl_factor_id', 'trait', 'label', 'source_revision', 'metadata', 'public_id', 'mapping')
                found = self._remembered(('factor', gen, identity), lambda: rows('SELECT e.factor_index,e.factor_id,e.trait,e.label,e.input_sha256,e.metadata,l.cfde_node_id,l.payload'
                    ' FROM eaggl_factors e LEFT JOIN eaggl_cfde_factor_links l ON l.run_id=%s AND l.factor_index=e.factor_index'
                    ' WHERE e.import_id=%s AND (e.factor_id=%s OR l.cfde_node_id=%s)',
                    [g['legacy_mapping_run_id'], imp, identity, identity], columns, ['eaggl_factors', 'eaggl_cfde_factor_links']))
                for table in ('eaggl_factors', 'eaggl_cfde_factor_links'):
                    if table not in tables: tables.append(table)
            if len(found) > 1: raise Problem(409, 'AMBIGUOUS_IDENTITY', 'The imported factor identifier is ambiguous.')
            return found[0] if found else None
        search = a.get('q', '').lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
        like = '%' + search + '%'
        if op in ('get_factor', 'get_factor_loadings', 'get_connections'):
            item = factor(a.get('factor_id'))
            if not item: return missing()
            if op == 'get_factor':
                if kpn:
                    columns = ('kpn_trait_id', 'legacy_phenotype_id', 'phenotype_name', 'trait_group', 'trait_type', 'description', 'metadata')
                    traits = rows('SELECT ' + ','.join(columns) + ' FROM kpn_traits WHERE generation_id=%s AND kpn_trait_id=%s',
                        [gen, item['kpn_trait_id']], columns, ['kpn_traits'])
                    if len(traits) > 1: raise Problem(409, 'AMBIGUOUS_IDENTITY', 'The pinned factor trait is ambiguous.')
                    item['trait_metadata'] = traits[0] if traits else None
                return {'items': [item], 'import_coverage': 'Exact imported factor metadata. Crosswalks describe routing correspondence, not scientific equivalence.'}, tables
            if op == 'get_connections':
                op = 'get_imported_graph'
                a = {'factor_index': item['factor_index']}
            else:
                kind, metric = a.get('kind', 'gene'), a.get('metric', 'joint')
                if kind not in ('gene', 'gene_set') or metric not in ('joint', 'marginal'):
                    raise Problem(422, 'INVALID_QUERY', 'Use gene/gene_set and joint/marginal.')
                if kind == 'gene':
                    return page('SELECT g.symbol,g.gene_index,l.loading FROM eaggl_gene_loadings l JOIN eaggl_genes g'
                        ' ON g.import_id=l.import_id AND g.gene_index=l.gene_index WHERE l.import_id=%s AND l.factor_index=%s'
                        " AND LOWER(g.symbol) LIKE %s ESCAPE '!'", [imp, item['factor_index'], like],
                        ('symbol', 'gene_index', 'loading'), ['eaggl_gene_loadings', 'eaggl_genes'], GENE_COVERAGE,
                        'l.loading DESC,g.symbol,g.gene_index')
                if kpn:
                    order = 'p.marginal_loading' if metric == 'marginal' else 'p.joint_loading'
                    return page('SELECT s.gene_set_id,s.gene_set_name,s.library,p.joint_loading,p.marginal_loading,'
                        'p.joint_loading_text,p.marginal_loading_text,p.joint_rank,p.marginal_rank,p.is_joint_top_factor'
                        ' FROM factor_gene_set_projections p JOIN cfde_gene_sets s ON s.generation_id=p.generation_id AND s.gene_set_id=p.gene_set_id'
                        " WHERE p.generation_id=%s AND p.scope=%s AND p.factor_key=%s AND LOWER(s.gene_set_name) LIKE %s ESCAPE '!'",
                        [gen, 'per_trait', item['factor_key'], like], ('gene_set_id','name','library','joint_loading','marginal_loading',
                        'joint_loading_text','marginal_loading_text','joint_rank','marginal_rank','is_joint_top_factor'),
                        ['factor_gene_set_projections', 'cfde_gene_sets'], SET_COVERAGE, order + ' DESC,s.gene_set_id')
                return page('SELECT node_id,source_key,gene_set_rank FROM eaggl_cfde_gene_set_links'
                    " WHERE run_id=%s AND factor_index=%s AND LOWER(source_key) LIKE %s ESCAPE '!'",
                    [g['legacy_mapping_run_id'],item['factor_index'],like], ('node_id','source_key','rank'),
                    ['eaggl_cfde_gene_set_links'], LEGACY_COVERAGE, 'gene_set_rank,node_id')
        if op == 'search_factors':
            if kpn:
                ids = (" OR LOWER(eaggl_factor_id) LIKE %s ESCAPE '!'", [like]) if search.isascii() else ('', [])  # ascii_bin, as in _release
                return page('SELECT public_id,factor_key,eaggl_factor_id,kpn_trait_id,label,source_revision FROM reference_factors'
                    " WHERE generation_id=%s AND (LOWER(label) LIKE %s ESCAPE '!'" + ids[0] + ')',
                    [gen,like,*ids[1]], ('public_id','factor_key','eaggl_factor_id','kpn_trait_id','label','source_revision'),
                    ['reference_factors'], 'All factors stored in this generation.', 'public_id')
            return page('SELECT factor_id,trait,label,input_sha256 FROM eaggl_factors'
                " WHERE import_id=%s AND (LOWER(label) LIKE %s ESCAPE '!' OR LOWER(factor_id) LIKE %s ESCAPE '!')",
                [imp,like,like],('factor_id','trait','label','source_revision'), ['eaggl_factors'], 'All factors stored in this import.', 'factor_index')
        if op in ('search_genes', 'resolve_gene'):
            clause, value = ("LOWER(symbol) LIKE %s ESCAPE '!'", like) if op == 'search_genes' else ('symbol=%s', _text(a.get('gene'),'gene'))
            result, tables = page('SELECT gene_index,symbol FROM eaggl_genes WHERE import_id=%s AND ' + clause,
                [imp,value], ('gene_index','symbol'), ['eaggl_genes'], 'Imported EAGGL symbols; external identifier equivalence and organism are not inferred.', 'symbol,gene_index')
            for item in result['items']:
                item.update(namespace='EAGGL symbol', external_identity_mapping='not_resolved', organism=None,
                    source_local_id=f'urn:reveal:eaggl-gene:{imp}:{item["gene_index"]}')
                if op == 'resolve_gene':
                    from .gene_identity import resolve_pinned_gene
                    try:
                        item['identity_mapping'] = resolve_pinned_gene(item['symbol'], taxon=a.get('taxon'),
                            mapping_revision=a.get('mapping_revision'), source_import=imp, source_index=item['gene_index'])
                    except (ValueError, OSError) as exc:
                        raise Problem(409, 'MAPPING_REVISION_UNAVAILABLE', str(exc)) from None
                    item['external_identity_mapping'] = item['identity_mapping']['status']
                    item['organism_context'] = {'taxon': a.get('taxon'), 'basis': 'explicit_query_parameter' if a.get('taxon') else 'unknown'}
            return result,tables
        if op == 'get_gene_factors':
            gene = _text(a.get('gene'), 'gene')
            # Gene first, whatever the optimizer guesses for the symbol filter: driven from eaggl_factors the plan
            # reads every loading of the import (2.4M rows). A hint is a comment to SQLite and to older servers.
            return page('SELECT /*+ JOIN_ORDER(g, l, f) */ f.factor_id,f.trait,f.label,l.loading FROM eaggl_genes g JOIN eaggl_gene_loadings l'
                ' ON l.import_id=g.import_id AND l.gene_index=g.gene_index JOIN eaggl_factors f'
                ' ON f.import_id=l.import_id AND f.factor_index=l.factor_index WHERE g.import_id=%s AND g.symbol=%s',
                [imp,gene], ('factor_id','trait','label','loading'), ['eaggl_genes','eaggl_gene_loadings','eaggl_factors'],
                GENE_COVERAGE, 'l.loading DESC,f.factor_index,g.gene_index')
        if op == 'get_gene_gene_sets':
            if not kpn: return missing('This model stores ranked gene-set links, not CFDE definitions with exact membership.')
            gene = _text(a.get('gene'), 'gene')
            symbol = gene[12:] if gene[:12].upper() == 'HGNC.SYMBOL:' else gene
            if not symbol or any(c in symbol for c in ':"\\'):
                raise Problem(422, 'INVALID_QUERY', 'Use a gene symbol or an HGNC.SYMBOL CURIE.')
            # Portable prefilter on the stored members' JSON text (a member string ending in :<symbol>), checked exactly below.
            members = "JSON_EXTRACT(s.metadata,'$.dapper_gene_set.members')"
            pattern = '%' + (':' + symbol + '"').lower().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            columns, select = ('gene_set_id', 'name', 'library', 'n_genes'), 'SELECT s.gene_set_id,s.gene_set_name,s.library,s.n_genes,'
            scope = {}
            if 'factor_id' in a:
                item = factor(a['factor_id'])
                if not item: return missing()
                projection = SET_COLUMNS[3:]
                columns += projection
                sql = (select + ''.join('p.' + column + ',' for column in projection) + members + ' FROM factor_gene_set_projections p'
                       ' JOIN cfde_gene_sets s ON s.generation_id=p.generation_id AND s.gene_set_id=p.gene_set_id'
                       ' WHERE p.generation_id=%s AND p.scope=%s AND p.factor_key=%s AND ')
                params, involved = [gen, 'per_trait', item['factor_key']], ['factor_gene_set_projections', 'cfde_gene_sets']
                coverage, order = MEMBERSHIP_COVERAGE + ' ' + SET_COVERAGE, SET_ORDER
                scope = {'factor': {'factor_id': item['public_id'], 'factor_key': item['factor_key'], 'label': item['label']}}
            else:  # every gene set of the generation: bounded in time, as in _release
                sql = select.replace('SELECT ', 'SELECT ' + SCAN_HINT, 1) + members + ' FROM cfde_gene_sets s WHERE s.generation_id=%s AND '
                params, involved, coverage, order = [gen], ['cfde_gene_sets'], MEMBERSHIP_COVERAGE, 's.n_genes,s.gene_set_id'
            try:
                result, tables = page(sql + 'LOWER(' + members + ") LIKE %s ESCAPE '!'", [*params, pattern],
                                      (*columns, 'members'), involved, coverage, order)
            except Exception as error:
                if scope or not _timed_out(error): raise
                return {'status': 'unavailable', 'items': [], 'reason': 'scan_budget_exceeded', 'gene': symbol,
                        'import_coverage': SCAN_BUDGET_COVERAGE}, tables
            found = []
            for row in result['items']:
                stored = _json(row.pop('members')) or []
                index = next((i for i, member in enumerate(stored) if isinstance(member, str)
                              and member.rsplit(':', 1)[-1].lower() == symbol.lower()), None)
                if index is not None: found.append({**row, 'member': stored[index], 'source_pointer': '/members/' + str(index)})
            result.update(items=found, gene=symbol, _more=len(result['items']) > limit, **scope)
            return result, tables
        if op in ('search_traits','get_trait'):
            if not kpn: return missing('This imported model has factor trait labels but no KPN trait catalog.')
            columns = ('kpn_trait_id','legacy_phenotype_id','phenotype_name','trait_group','trait_type','description','metadata')
            trait = _text(a.get('trait_id'),'trait_id') if op == 'get_trait' else None
            if trait and re.fullmatch(r'trait:kpn:[0-9]{7}',trait): trait='KPN.TRAIT:'+trait.rsplit(':',1)[1]
            clause, params = ("LOWER(phenotype_name) LIKE %s ESCAPE '!'",[like]) if op == 'search_traits' else ('kpn_trait_id=%s',[trait])
            return page('SELECT '+','.join(columns)+' FROM kpn_traits WHERE generation_id=%s AND '+clause,
                [gen,*params], columns,['kpn_traits'],'Imported trait metadata; no phenotype association scores are stored.','kpn_trait_id')
        if op in ('search_gene_sets','get_gene_set','get_gene_set_members'):
            if not kpn:
                if op == 'search_gene_sets':
                    return page('SELECT a.dapper_id,a.source_key,a.node_id FROM cfde_gene_set_aliases a'
                        " WHERE a.import_id=%s AND LOWER(a.source_key) LIKE %s ESCAPE '!'", [g['legacy_gene_set_import_id'],like],
                        ('gene_set_id','source_key','node_id'),['cfde_gene_set_aliases'],'Imported source aliases.','a.node_id_sha256')
                identity = _text(a.get('gene_set_id'),'gene_set_id')
                found = rows('SELECT o.payload,a.provenance,a.source_key FROM cfde_gene_set_aliases a JOIN dapper_objects o ON o.id=a.dapper_id'
                    ' WHERE a.import_id=%s AND a.dapper_id=%s ORDER BY a.node_id_sha256 LIMIT 1',
                    [g['legacy_gene_set_import_id'],identity],('payload','provenance','source_key'),['cfde_gene_set_aliases','dapper_objects'])
                if not found: return missing()
                item = found[0]; node = item['payload']
            else:
                if op == 'search_gene_sets':
                    return page('SELECT gene_set_id,gene_set_name,library,n_genes,n_genes_in_eaggl_universe FROM cfde_gene_sets'
                        " WHERE generation_id=%s AND LOWER(gene_set_name) LIKE %s ESCAPE '!'", [gen,like],
                        ('gene_set_id','name','library','n_genes','n_genes_in_eaggl_universe'),['cfde_gene_sets'],
                        'All imported gene-set definitions; factor projections retain a smaller subset.','gene_set_id')
                identity = _text(a.get('gene_set_id'),'gene_set_id')
                found = rows('SELECT s.gene_set_id,s.gene_set_name,s.library,s.n_genes,s.n_genes_in_eaggl_universe,s.metadata,c.payload'
                    ' FROM cfde_gene_sets s JOIN cfde_gene_set_collections c ON c.generation_id=s.generation_id AND c.collection_id=s.collection_id'
                    ' WHERE s.generation_id=%s AND s.gene_set_id=%s', [gen,identity],
                    ('gene_set_id','name','library','n_genes','n_genes_in_eaggl_universe','metadata','payload'),['cfde_gene_sets','cfde_gene_set_collections'])
                if not found: return missing()
                item = found[0]; node = item['metadata'].get('dapper_gene_set')
            if node and node.get('id') != identity: raise Problem(503,'SOURCE_NOT_READY','Stored gene-set object identity differs from its key.')
            if op == 'get_gene_set':
                return {'items':[item], 'import_coverage':'Exact imported definition and stored provenance; missing membership is explicit.'},tables
            if not node or not isinstance(node.get('members'),list): return missing('Exact gene-set membership is not stored.')
            members = node['members']; selected = members[offset:offset+limit+1]
            return {'items':[{'member':member,'source_pointer':'/members/'+str(offset+i)} for i,member in enumerate(selected)],
                    'total_rows':len(members),'gene_set_id':identity,'source_object_sha256':sha256(canonical_json(node)),
                    'ordering':'source member order','import_coverage':'A page of the exact source membership; never a newly minted truncated GeneSet.'},tables
        if op == 'get_gene_set_factors':
            if not kpn: return missing('This model has ranked links, not numeric gene-set projections.')
            identity = _text(a.get('gene_set_id'),'gene_set_id')
            return page('SELECT f.public_id,p.factor_key,p.joint_loading,p.marginal_loading,p.joint_loading_text,p.marginal_loading_text,p.joint_rank,p.marginal_rank'
                ' FROM factor_gene_set_projections p JOIN reference_factors f ON f.generation_id=p.generation_id AND f.factor_key=p.factor_key'
                ' WHERE p.generation_id=%s AND p.scope=%s AND p.gene_set_id=%s', [gen,'per_trait',identity],
                ('factor_id','factor_key','joint_loading','marginal_loading','joint_loading_text','marginal_loading_text','joint_rank','marginal_rank'),
                ['factor_gene_set_projections','reference_factors'],SET_COVERAGE,'p.joint_loading DESC,p.factor_key')
        if op == 'get_imported_graph':
            selector, value = ('n.factor_index',a['factor_index']) if 'factor_index' in a else ('n.node_id',_text(a.get('node_id'),'node_id'))
            nodes = rows('SELECT n.node_index,n.node_id,n.payload FROM eaggl_graph_nodes n WHERE n.import_id=%s AND '+selector+'=%s',
                [imp,value],('node_index','node_id','payload'),['eaggl_graph_nodes'])
            if not nodes: return missing('This import has no stored graph node for the requested identity.')
            if len(nodes)>1: raise Problem(409,'AMBIGUOUS_IDENTITY','Multiple imported graph nodes match this factor.')
            result,tables = page('SELECT p.node_id,c.node_id,p.payload,c.payload FROM eaggl_graph_edges e'
                ' JOIN eaggl_graph_nodes p ON p.import_id=e.import_id AND p.node_index=e.parent_index'
                ' JOIN eaggl_graph_nodes c ON c.import_id=e.import_id AND c.node_index=e.child_index'
                ' WHERE e.import_id=%s AND (e.parent_index=%s OR e.child_index=%s)', [imp,nodes[0]['node_index'],nodes[0]['node_index']],
                ('parent_id','child_id','parent_payload','child_payload'),['eaggl_graph_edges','eaggl_graph_nodes'],
                'Stored EAGGL hierarchy edges only; no inferred interaction or causal relationship.','e.parent_index,e.child_index')
            for item in result['items']:
                item['parent_payload']=_json(item['parent_payload']); item['child_payload']=_json(item['child_payload'])
            result['node']=nodes[0]; return result,tables
        raise Problem(422,'INVALID_QUERY','Unknown imported reference operation.')


def phenotype_gate():
    """The deployment gate for the BioIndex small-model readers (REVEAL_SMALL_PHENOTYPE_VERIFIED)."""
    from .runtime_config import setting
    return setting('REVEAL_SMALL_PHENOTYPE_VERIFIED', 'false').lower() == 'true'


def capability_catalog(generation=None, *, phenotype_verified=None, source_kind=None):
    """The data operations of a pin. source_kind 'reference_release' (also read from generation['source_kind']) is a release:
    no EAGGL graph, its betas and factor-trait links (unless absent at publish); other pins are reload-era generations."""
    from .gene_identity import crosswalk_capability
    if phenotype_verified is None: phenotype_verified = phenotype_gate()
    model=(generation or {}).get('model')
    release=(source_kind or (generation or {}).get('source_kind'))==RELEASE
    absent=set((generation or {}).get('absent_files') or ())
    gene_coverage,set_coverage=(RELEASE_GENE_COVERAGE,RELEASE_SET_COVERAGE) if release else (GENE_COVERAGE,SET_COVERAGE)
    operations=[]
    for name,fields in OPERATIONS.items():
        availability='supported' if model in (KPN_MODEL,LEGACY_MODEL) else 'not_supported_for_model' if model else 'requires_generation_metadata'
        reason=None
        if model==LEGACY_MODEL and name in ('search_traits','get_trait','get_gene_set_factors','get_gene_gene_sets'):
            availability='not_stored'
            reason=('This model stores factor trait labels, not a KPN trait catalog.' if name in ('search_traits','get_trait')
                    else 'This model stores ranked factor-to-gene-set links, not numeric gene-set projections.' if name=='get_gene_set_factors'
                    else 'This model stores ranked gene-set links, not CFDE definitions with exact membership.')
        if release and name in GRAPH_OPERATIONS: availability,reason='not_stored',NO_GRAPH
        if name in RELEASE_OPERATIONS and not release: availability,reason='not_stored',RELEASE_ONLY
        if release and RELEASE_FILES.get(name) in absent: availability,reason='not_stored',f'This release was published without {RELEASE_FILES[name]}.'
        descriptor={'name':name,'arguments':sorted(fields | PAGE_FIELDS),'availability':availability}
        if reason: descriptor['reason']=reason
        if name=='get_factor_loadings':
            descriptor['semantics']={'gene':gene_coverage,'gene_set':set_coverage if model==KPN_MODEL else LEGACY_COVERAGE if model==LEGACY_MODEL else 'requires_generation_metadata'}
        if name=='get_gene_gene_sets':
            descriptor['semantics']={'membership':RELEASE_MEMBERSHIP_COVERAGE if release else MEMBERSHIP_COVERAGE,'factor_id':set_coverage}
        if name in ('get_trait_gene_sets','get_gene_set_traits'): descriptor['semantics']={'betas':BETA_COVERAGE}
        if name in ('get_factor_trait_links','get_trait_factor_links'): descriptor['semantics']={'links':LINK_COVERAGE}
        operations.append(descriptor)
    coverage={'gene_loadings':gene_coverage,'gene_set_projections':set_coverage if model==KPN_MODEL else 'not_stored' if model==LEGACY_MODEL else 'requires_generation_metadata',
              'legacy_gene_set_links':LEGACY_COVERAGE if model==LEGACY_MODEL else 'not_applicable' if model==KPN_MODEL else 'requires_generation_metadata',
              'phenotype_associations':'gene_set_betas' if release and 'trait_gene_sets.tsv.gz' not in absent else 'not_stored'}
    if release:
        coverage.update(gene_set_betas=BETA_COVERAGE if 'trait_gene_sets.tsv.gz' not in absent else 'not_stored',
                        factor_trait_links=LINK_COVERAGE if 'factor_trait_links.tsv.gz' not in absent else 'not_stored',
                        gene_set_membership=RELEASE_MEMBERSHIP_COVERAGE, imported_graph='not_stored')
    return {'version':RELEASE_READER_VERSION if release else READER_VERSION,'source_mode':'imported_reference',
        'generation':deepcopy(generation),'operations':operations,
        'coverage':coverage,
        'availability_note':'Supported means a reader exists for the retained model; each query reports actual imported coverage and missing rows.',
        'external_fallback':False,'gene_identity_crosswalk':crosswalk_capability(),
        'small_model_phenotype':SmallModelBioIndex.descriptor(verified=phenotype_verified)}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class _OverBudget(ValueError):
    """An upstream page larger than the remaining byte budget: a bound, not a scope violation."""


GENE_SYMBOL = r'(?:HGNC\.SYMBOL:)?[A-Za-z0-9][A-Za-z0-9._@-]{0,63}'
# kpn_traits.gwas_source_category -> the pigean-gene trait_group key; other categories are refused.
TRAIT_GROUPS = {'KPN': 'portal', 'rare_v2': 'rare_v2'}
KPN_TRAIT = re.compile(r'KPN\.TRAIT:[0-9]{7}')


class SmallModelBioIndex:
    """The only external reference exception. Never follows redirects/fallbacks."""
    HOST = 'https://bioindex.hugeamp.org'
    VERSION = 'reveal.bioindex-small-phenotype/2'
    # The host's CDN answers HTTP 403 to urllib's default Python-urllib agent.
    USER_AGENT = 'REVEAL-BioIndexSmallPhenotype/2'
    INDEXES = {'get_pigean_gene_phenotype':'pigean-gene-phenotype',
               'get_pigean_gene_set_phenotype':'pigean-gene-set-phenotype'}
    GENE_INDEX = 'pigean-gene'
    SIGNATURES = {'pigean-gene-phenotype':['phenotype','sigma','gene_set_size'],
                  'pigean-gene-set-phenotype':['phenotype','sigma','gene_set_size'],
                  'pigean-gene':['trait_group','gene','sigma','gene_set_size']}
    DEFAULT_LIMIT, MAX_LIMIT, MAX_GENES, MAX_RETRY_AFTER = 100, 500, 5, 2
    NOTES = {'get_pigean_gene_phenotype': 'Pass phenotype_id (a BioIndex phenotype such as T2D, or a pinned KPN.TRAIT id) for its top'
                 ' PIGEAN gene rows; genes for each gene\'s phenotype rows across the KPN trait groups; or both for that phenotype\'s row'
                 ' per gene. limit caps rows per upstream query (default 100).',
             'get_pigean_gene_set_phenotype': 'Pass phenotype_id and an optional limit (default 100), then inspect returned GeneSet rows.'}

    @classmethod
    def arguments_schema(cls, operation='get_pigean_gene_phenotype'):
        """One advertised contract per fixed phenotype reader."""
        properties = {'phenotype_id': {'type': 'string', 'minLength': 1, 'maxLength': 200,
            'pattern': r'^(?=.*\S)[^,\x00-\x1f]+(?![\s\S])',
            'description': 'Exact BioIndex phenotype (for example T2D) or a KPN.TRAIT identifier of the pinned generation.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': cls.MAX_LIMIT,
                'description': f'Rows per upstream query, default {cls.DEFAULT_LIMIT}; a capped result is reported as partial.'}}
        schema = {'type': 'object', 'properties': properties, 'required': ['phenotype_id'], 'additionalProperties': False}
        if operation == 'get_pigean_gene_phenotype':
            properties['genes'] = {'type': 'array', 'minItems': 1, 'maxItems': cls.MAX_GENES, 'uniqueItems': True,
                'items': {'type': 'string', 'pattern': '^' + GENE_SYMBOL + r'(?![\s\S])'},
                'description': 'Per-gene mode (pigean-gene). With phenotype_id, only that phenotype\'s row for each gene; without it, each gene\'s phenotypes.'}
            schema.update(required=[], anyOf=[{'required': ['phenotype_id']}, {'required': ['genes']}])
        return schema

    def __init__(self, *, fetch=None, verified=False, traits=None, max_bytes=3_000_000, max_pages=3, timeout=20, page_timeout=10,
                 retries=2, sleep=None, clock=None):
        self.fetch = fetch or self._fetch
        self.verified = verified
        # traits(generation_id, phenotype) -> the pinned kpn_traits row, for KPN.TRAIT ids and the per-gene trait group.
        self.traits = traits
        self.max_bytes, self.max_pages, self.timeout, self.page_timeout = max_bytes, max_pages, timeout, page_timeout
        self.retries, self.sleep = retries, sleep or time.sleep
        self.clock = clock or (lambda:datetime.now(timezone.utc).isoformat())

    @classmethod
    def descriptor(cls, *, verified=None, operation=None):
        if verified is None: verified = phenotype_gate()
        return {'host':cls.HOST,'model':'small','sigma':2,'signature':cls.SIGNATURES['pigean-gene-phenotype'],
                'gene_index':cls.GENE_INDEX,'gene_signature':cls.SIGNATURES[cls.GENE_INDEX],'trait_groups':dict(TRAIT_GROUPS),
                'adapter_version':cls.VERSION,'operations':cls.INDEXES,
                'arguments':cls.arguments_schema(operation) if operation else {name:cls.arguments_schema(name) for name in cls.INDEXES},
                'limits':{'default_rows':cls.DEFAULT_LIMIT,'max_rows':cls.MAX_LIMIT,'max_genes':cls.MAX_GENES},
                'availability':'supported' if verified else 'requires_verified_deployment_access',
                'deployment_verified':bool(verified), 'coverage':'not_queried',
                'availability_note':'Deployment capability does not establish source coverage or a successful scientific query. An unavailable source is an outage, never an absence.',
                'source_mode':'bioindex_small_phenotype'}

    @staticmethod
    def _fetch(url, max_bytes, timeout):
        headers = {'Accept':'application/json','User-Agent':SmallModelBioIndex.USER_AGENT}
        with build_opener(_NoRedirect()).open(Request(url, headers=headers),timeout=timeout) as response:
            data = response.read(max_bytes+1)
        if len(data)>max_bytes: raise _OverBudget('Source response exceeds byte budget')
        return data

    def _wait(self, error, attempt, deadline):
        """Seconds to wait before retrying error (429, 5xx, timeout or reset), or None when it is final."""
        if attempt >= self.retries: return None
        wait = 0.5 * 2 ** attempt
        if isinstance(error, HTTPError):
            if error.code != 429 and error.code < 500: return None
            headers = getattr(error, 'headers', None)
            after = headers.get('Retry-After') if headers is not None else None
            if after is not None:
                try: wait = float(after)
                except ValueError: return None
                if not 0 <= wait <= self.MAX_RETRY_AFTER: return None
        elif not isinstance(error.reason if isinstance(error, URLError) else error, (TimeoutError, ConnectionError)):
            return None
        return wait if time.monotonic() + wait < deadline else None

    def _get(self, url, budget, deadline):
        """One upstream page within the per-page timeout, retried within the query deadline."""
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError('Query latency budget exhausted')
            try:
                return self.fetch(url, budget, min(self.page_timeout, remaining))
            except OSError as error:   # HTTPError, URLError, timeouts and resets
                wait = self._wait(error, attempt, deadline)
                if wait is None:
                    error.reveal_attempts = attempt + 1
                    raise
            self.sleep(wait); attempt += 1

    def verify(self):
        """Administrative metadata probe; never exposed as an agent operation."""
        deadline = time.monotonic() + self.timeout
        get = lambda path: decode(self._get(self.HOST + path, self.max_bytes, deadline))
        catalog = get('/api/bio/indexes')
        if not all(any(row.get('index')==index and row.get('query',{}).get('keys')==keys for row in catalog.get('data',[]))
                   for index, keys in self.SIGNATURES.items()):
            raise Problem(503,'SOURCE_UNAVAILABLE','The small-model deployment query signature has not been verified.')
        for index, keys in self.SIGNATURES.items():
            # The keys route takes the index's own key count: 3 for the phenotype indexes, 4 for pigean-gene.
            if ['small'] not in get('/api/bio/keys/'+index+'/'+str(len(keys))+'?columns=gene_set_size').get('keys',[]):
                raise Problem(503,'SOURCE_UNAVAILABLE','The deployment does not advertise literal small.')
        groups = get('/api/bio/keys/'+self.GENE_INDEX+'/'+str(len(self.SIGNATURES[self.GENE_INDEX]))+'?columns=trait_group').get('keys',[])
        if any([group] not in groups for group in TRAIT_GROUPS.values()):
            raise Problem(503,'SOURCE_UNAVAILABLE','The deployment does not advertise every mapped trait group.')
        self.verified=True
        return catalog

    def query(self, operation, arguments, *, generation_id=None):
        allowed = set(self.arguments_schema(operation)['properties']) if operation in self.INDEXES else set()
        if not allowed or not isinstance(arguments,dict) or set(arguments)-allowed:
            raise Problem(422,'INVALID_QUERY','Only the fixed small-model phenotype operations and their arguments are permitted.')
        phenotype, genes, limit = arguments.get('phenotype_id'), arguments.get('genes'), arguments.get('limit', self.DEFAULT_LIMIT)
        if phenotype is not None:
            phenotype=_text(phenotype,'phenotype_id',200)
            if ',' in phenotype: raise Problem(422,'INVALID_QUERY','A phenotype identifier cannot add positional query keys.')
        if type(limit) is not int or not 1 <= limit <= self.MAX_LIMIT:
            raise Problem(422,'INVALID_QUERY',f'Use a limit between 1 and {self.MAX_LIMIT}.')
        if genes is not None:
            if (not isinstance(genes,list) or not 1 <= len(genes) <= self.MAX_GENES
                    or any(not isinstance(gene,str) or not re.fullmatch(GENE_SYMBOL,gene) for gene in genes)):
                raise Problem(422,'INVALID_QUERY',f'Use one to {self.MAX_GENES} gene symbols.')
            genes=[gene.rsplit(':',1)[-1] for gene in genes]
            if len({gene.upper() for gene in genes})!=len(genes): raise Problem(422,'INVALID_QUERY','Gene symbols repeat.')
        elif phenotype is None: raise Problem(422,'INVALID_QUERY','Pass phenotype_id, genes or both.')
        index=self.GENE_INDEX if genes else self.INDEXES[operation]
        source={'origin':self.HOST+'/api/bio/query/'+index,'host':self.HOST,'index':index,'model':'small','sigma':2,
                'phenotype':phenotype,'adapter_version':self.VERSION,'observed_at':self.clock(),
                'upstream_release':None,'generation_id':None,
                'bounds':{'bytes':self.max_bytes,'pages':self.max_pages,'rows':limit,'seconds':self.timeout,
                          'page_seconds':self.page_timeout,'retries':self.retries}}
        result={'items':[],'status':'source_unavailable','import_coverage':'Separate small-model observation; not part of the imported reference generation.',
                'snapshot_consistency':'not_guaranteed','returned_rows':0,'truncated':False,'next_cursor':None,'limit':limit}
        pages={}; page_meta=[]; reason=None; queries=[]
        if not self.verified: reason='Deployment access and the small-model index signatures have not been verified.'
        else:
            trait=self.traits(generation_id,phenotype) if phenotype is not None and self.traits and generation_id else None
            if phenotype is not None and not trait and (genes or KPN_TRAIT.fullmatch(phenotype)):
                raise Problem(422,'INVALID_QUERY','Per-gene lookups and KPN.TRAIT identifiers need a phenotype in the pinned generation\'s KPN trait catalog.')
            if trait:
                group=TRAIT_GROUPS.get(trait['gwas_source_category'])
                if genes and not group:
                    raise Problem(422,'INVALID_QUERY','No BioIndex trait group is registered for source category '+str(trait['gwas_source_category'])+'.')
                phenotype=source['phenotype']=trait['legacy_phenotype_id']
                source['trait']={**trait,'trait_group':group,'reference_generation_id':generation_id}
            if genes:
                groups=[TRAIT_GROUPS[trait['gwas_source_category']]] if phenotype is not None else sorted(set(TRAIT_GROUPS.values()))
                source.update(genes=genes,trait_groups=groups)
                requests=[(gene,group,[group,gene,'2','small']) for gene in genes for group in groups]
            else: requests=[(None,None,[phenotype,'2','small'])]
            deadline=time.monotonic()+self.timeout; consumed=0; truncated=False

            def check(row,gene,group):
                if not isinstance(row,dict): raise ValueError('Invalid source row')
                if row.get('gene_set_size','small')!='small' or str(row.get('sigma',2)) not in ('2','2.0'):
                    raise ValueError('Source model or sigma escaped query scope')
                if gene is None:
                    if row.get('phenotype',phenotype)!=phenotype: raise ValueError('Source phenotype escaped query scope')
                elif str(row.get('gene',gene)).upper()!=gene.upper() or row.get('trait_group',group)!=group:
                    raise ValueError('Source gene or trait group escaped query scope')

            def read(keys,upstream_limit,gene,group):
                # Bounded pages of one positional query. This is an explicit result cap, never advertised as upstream page
                # size or complete retrieval; unread byte progress remains partial.
                nonlocal consumed
                url=source['origin']+'?'+urlencode({'q':','.join(keys),**({'limit':upstream_limit} if upstream_limit else {})})
                rows=[]; seen=set(); partial=True
                for _ in range(self.max_pages):
                    if consumed>=self.max_bytes: raise _OverBudget('Source byte budget exhausted')
                    raw=self._get(url,self.max_bytes-consumed,deadline)
                    if not isinstance(raw,bytes) or len(raw)>self.max_bytes-consumed: raise _OverBudget('Source response exceeds byte budget')
                    page=decode(raw)
                    if not isinstance(page,dict): raise ValueError('Invalid BioIndex response envelope')
                    data=page.get('data')
                    if not isinstance(data,list): raise ValueError('Invalid BioIndex response rows')
                    if page.get('index',index)!=index: raise ValueError('Source index escaped query scope')
                    query_keys = page.get('q')
                    if query_keys is not None:
                        if isinstance(query_keys,str): query_keys=query_keys.split(',')
                        if not isinstance(query_keys,list) or [str(v) for v in query_keys] != keys:
                            raise ValueError('Source positional query escaped its pinned scope')
                    for row in data: check(row,gene,group)
                    pages['page-'+str(len(pages))+'.json']=raw; consumed+=len(raw)
                    rows.extend(data)
                    progress=page.get('progress') or {}
                    if not isinstance(progress,dict): raise ValueError('Invalid source progress')
                    continuation=page.get('continuation')
                    finished = (type(progress.get('bytes_read')) is int and type(progress.get('bytes_total')) is int
                                and 0 <= progress['bytes_read'] == progress['bytes_total'])
                    partial=bool(continuation) or not finished
                    page_meta.append({'sha256':sha256(raw),'size_bytes':len(raw),'rows':len(data),'progress':progress,
                                      'restricted':page.get('restricted'),'page':page.get('page'),'q':keys})
                    if not continuation or (upstream_limit and len(rows)>=upstream_limit): break
                    if not isinstance(continuation,str) or len(continuation)>10000 or continuation in seen:
                        raise ValueError('Invalid or repeated upstream continuation')
                    seen.add(continuation)
                    url=self.HOST+'/api/bio/cont?'+urlencode({'token':continuation})
                return rows,partial

            try:
                for gene,group,keys in requests:
                    entry={'gene':gene,'trait_group':group,'status':'unavailable','returned_rows':0}
                    if gene: queries.append(entry)
                    # One phenotype's row per gene needs that gene's whole exposure; otherwise the upstream limit applies.
                    rows,partial=read(keys,None if gene and phenotype is not None else limit,gene,group)
                    if gene and phenotype is not None: rows=[row for row in rows if row.get('phenotype')==phenotype]
                    if len(rows)>limit: rows,partial=rows[:limit],True
                    result['items'].extend(rows); truncated=truncated or partial
                    entry.update(status='partial' if partial else 'complete',returned_rows=len(rows))
                result.update(status='partial' if truncated else 'complete' if result['items'] else 'empty',truncated=truncated)
            except (OSError,_OverBudget) as error:
                result.update(status='partial' if result['items'] else 'source_unavailable',truncated=bool(result['items']))
                if isinstance(error,_OverBudget): reason=str(error)+'; unread rows were not observed.'
                else:
                    attempts=getattr(error,'reveal_attempts',1)
                    reason=('The configured small-model source was unavailable ('+('HTTP '+str(error.code) if isinstance(error,HTTPError)
                            else type(error).__name__)+(f' after {attempts} attempts' if attempts>1 else '')+
                            '). This is an outage, not evidence of absence.')
            except (ValueError,TypeError,KeyError) as error:
                # A scope mismatch invalidates the whole query; earlier valid
                # pages stay diagnostic only and cannot become eligible evidence.
                result.update(items=[],status='source_unavailable',truncated=False)
                for entry in queries: entry.update(status='unavailable',returned_rows=0)
                reason=str(error)
        if reason: result['reason']=reason
        if genes:
            done={(entry['gene'],entry['trait_group']) for entry in queries}
            queries+=[{'gene':gene,'trait_group':group,'status':'not_queried','returned_rows':0}
                      for gene in genes for group in (source.get('trait_groups') or []) if (gene,group) not in done]
            result['queries']=queries
            result['absence_note']=('A complete per-gene query without a row for the phenotype is an upstream absence, not a biological'
                                    ' negative; partial, unavailable and not_queried genes were not fully observed.')
        result.update(returned_rows=len(result['items']),pages=page_meta,
                      coverage='Complete only for returned source exposure; restricted rows and upstream filtering may remain.')
        raw=canonical_json({'format':CAPTURE_FORMAT,'source_mode':'bioindex_small_phenotype','source':source,
                            'operation':operation,'arguments':deepcopy(arguments),'result':result})
        return QueryCapture(result,raw,'bioindex_small_phenotype',source,pages)
