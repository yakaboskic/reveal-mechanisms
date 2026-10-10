"""Reference release: one folder of plain files per LAP projection, published to each environment.

`python -m reveal_backend.reference_release <command>` prints one JSON object on stdout (progress goes to stderr)
and exits non-zero on any failure:

  build    LAP project outputs, the per-trait long files and gene-set betas and the vector cache -> one release
           folder, built beside --out and swapped into place (a folder already holding the same release is reused
           untouched). Only factor labels and DisMech contexts have vectors; factor labels missing from the cache are
           embedded, and only after re-embedded cached texts reproduce their stored vectors.
  publish  every file checked against the manifest; then for each --env, in order: add the vectors its fixed
           Upstash namespaces lack, add new frozen factor snapshots to the shared archived_reference_factors,
           replace its <prefix>_ref_* tables with one RENAME, overwrite changed vectors and delete the ones the
           release no longer has, and record the release in <prefix>_records. Re-running a published release
           changes nothing; re-running a failed publish finishes it.

Secrets are read only from the environment (the repository .env via python-dotenv) and are never printed.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import gzip
import hashlib
from itertools import groupby, islice
import io
import json
import multiprocessing
from operator import itemgetter
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid

from . import reference_generation as rg
from .provenance_schema import PROVENANCE_EDGES, PROVENANCE_GROUPS, validate_provenance_edges
from .repository import Repository, application_sql, canonical, digest, now
from .runtime_config import ROOT

RELEASE_FORMAT = 'reveal.reference-release/1'
SNAPSHOT_FORMAT = 'reveal.archived-reference-factor/1'
RECORD_KIND, RECORD_ID = 'reference_release', 'current'
# Environment -> application table prefix. Its vectors live in <env>-factors and <env>-contexts.
ENVIRONMENTS = {'local': 'reveal_workflow_local', 'qa': 'reveal_workflow_qa', 'prod': 'reveal'}
# Each environment's REVEAL_NOTIFICATION_NAMESPACE (deploy/dig/service.yaml, scripts/durable_deployment.py).
NOTIFICATION_NAMESPACES = {'local': 'reveal-workflow-local', 'qa': 'reveal-qa', 'prod': 'reveal-prod'}
# Only factor labels and DisMech contexts are embedded: gene sets and collections are not (the app never reads them).
KINDS = ('factors', 'contexts')
VECTOR_KINDS = {'factors': 'factor', 'contexts': 'context'}
DEFAULT_TOP_N = 50
TOP_GENES, TOP_GENE_SETS = 50, 10  # archived snapshots: top genes by loading; top gene sets of each library by joint rank
CALIBRATION_TEXTS, CALIBRATION_THRESHOLD = 8, 0.999
UPSERT_BATCH, UPSERT_WORKERS, RANGE_PAGE, DELETE_BATCH = 200, 4, 200, 1000
INSERT_BYTES = 512 << 10  # per INSERT batch; pymysql splits executemany statements at ~1 MB (escaping included)
DUPLICATE_KEY = 1062  # the only MySQL warning INSERT IGNORE may raise here
# DDL on live tables waits at most SWAP_LOCK_WAIT seconds per attempt for their metadata locks: a RENAME queued behind a long
# reader would block every other reader of those tables. Retried SWAP_ATTEMPTS times.
SWAP_LOCK_WAIT, SWAP_ATTEMPTS, LOCK_WAIT_TIMEOUT = 5, 12, 1205
LONG_SUFFIX = '.projection.tsv'
LONG_COLUMNS = ['trait', 'kpn_trait_id', 'factor_id', 'factor', 'factor_label', 'gene_set_id', 'collection_id', 'cfde_label', 'library',
                'joint_loading', 'marginal_loading', 'joint_rank_in_factor', 'marginal_rank_in_factor', 'is_joint_top_factor',
                'joint_rank_in_library', 'marginal_rank_in_library']

# Release folder. manifest.json `files` = {name: sha256} of DATA_FILES and release_id = digest({format, files});
# archived_factors.jsonl.gz is written afterwards (it carries the release_id) and listed under `archive`.
VECTOR_FILES = tuple(f'vectors/{kind}.{suffix}' for kind in KINDS for suffix in ('f32.npy', 'tsv'))
DATA_FILES = ('traits.jsonl.gz', 'factors.jsonl.gz', 'factor_genes.tsv.gz', 'collections.jsonl.gz', 'gene_sets.jsonl.gz',
              'projections.tsv.gz', 'trait_gene_sets.tsv.gz', 'dapper_nodes.jsonl.gz', 'dapper_edges.tsv.gz') + VECTOR_FILES
ARCHIVE_FILE = 'archived_factors.jsonl.gz'
FACTOR_GENE_COLUMNS = ('factor_key', 'gene', 'loading')
# Trait -> CFDE gene-set betas: the LAP betas_ stage's per-trait files (projection_workflow.py annotate-gene-set-stats:
# `pigean betas` on the trait's existing PIGEAN gene stats, no outer Gibbs) and the release's trait_gene_sets.tsv.gz.
GENE_SET_STATS_SUFFIX = '.betas.tsv'
LAP_GENE_SET_STATS_COLUMNS = ('trait', 'kpn_trait_id', 'gene_set_id', 'collection_id', 'cfde_label', 'library', 'n_genes',
                              'beta_uncorrected', 'beta', 'avg_postp', 'library_rank', 'response', 'p', 'sigma2')
TRAIT_GENE_SET_COLUMNS = ('kpn_trait_id', 'gene_set_id', 'library', 'beta_uncorrected', 'beta', 'avg_postp', 'library_rank')
PROJECTION_COLUMNS = ('factor_key', 'gene_set_id', 'library', 'joint_loading', 'marginal_loading', 'joint_rank', 'marginal_rank', 'is_joint_top_factor')
EDGE_COLUMNS = ('subject', 'predicate', 'object', 'edge_role')
VECTOR_COLUMNS = ('row', 'id', 'input_sha256', 'vector_sha256')
# DAPPER node sections of a collection document (before or after its gene_sets list). Its `embeddings` and
# `has_embedding_edges` are left out: the release embeds no gene set or collection.
NODE_SECTIONS, SKIPPED_SECTIONS = ('organizations', 'datasets', 'files', 'activities', 'gene_set_collections'), ('embeddings', 'has_embedding_edges')
# A top-level section of a collection document starts with its key at column 0.
SECTION_RE = re.compile(r'([A-Za-z_][A-Za-z_0-9]*):(?:\s|$)')
# The vector cache (seeded by the migration script; build adds embedded factor labels). The same text may appear
# once per kind (a factor label can equal a DisMech context); meta holds model, model_revision, provider, dimensions.
CACHE_SCHEMA = ('CREATE TABLE IF NOT EXISTS vectors(input_sha256 TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, '
                'dimensions INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(input_sha256, kind))',
                'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
DAPPER_ID_RE = re.compile(r'dapper:([A-Za-z][A-Za-z0-9]*)\.[A-Za-z0-9_-]{1,64}')

# Per-environment tables (<prefix>_ref_<name>; one row in ref_release), in RENAME order.
COLUMNS = {
    'traits': ('kpn_trait_id', 'legacy_phenotype_id', 'phenotype_name', 'gwas_source_category', 'trait_group', 'legacy_trait_group',
               'trait_type', 'description', 'is_dichotomous', 'is_complex', 'n_factors', 'metadata'),
    'factors': ('factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'factor_number', 'label', 'input_sha256', 'source_revision', 'metadata'),
    'factor_genes': FACTOR_GENE_COLUMNS,
    'collections': ('collection_id', 'cfde_label', 'library', 'n_sets', 'payload'),
    'gene_sets': ('gene_set_id', 'collection_id', 'gene_set_name', 'library', 'n_genes', 'n_genes_in_eaggl_universe', 'legacy_source_key', 'metadata'),
    'projections': ('factor_key', 'gene_set_id', 'library', 'joint_loading', 'marginal_loading', 'joint_loading_text', 'marginal_loading_text',
                    'joint_rank', 'marginal_rank', 'is_joint_top_factor'),
    'dapper_nodes': ('id', 'class_name', 'payload'),
    'dapper_edges': EDGE_COLUMNS,
    'trait_gene_sets': TRAIT_GENE_SET_COLUMNS,
    'release': ('release_id', 'published_at', 'manifest')}
TABLES = tuple(COLUMNS)
JSON_COLUMNS = frozenset({'metadata', 'payload', 'manifest'})
# Written with reveal_ref_* names: repository.application_sql adds the environment prefix. Types follow migration 008
# without generation_id; no foreign keys, so every table is swapped at once.
SCHEMA = '''
CREATE TABLE reveal_ref_traits{suffix} (
  kpn_trait_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  legacy_phenotype_id VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  phenotype_name TEXT NOT NULL,
  gwas_source_category VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  trait_group VARCHAR(64) NULL,
  legacy_trait_group VARCHAR(64) NULL,
  trait_type VARCHAR(32) NULL,
  description TEXT NULL,
  is_dichotomous TINYINT NULL,
  is_complex TINYINT NULL,
  n_factors INT UNSIGNED NOT NULL,
  metadata JSON NOT NULL,
  UNIQUE KEY ref_trait_legacy (legacy_phenotype_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_factors{suffix} (
  factor_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  public_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  eaggl_factor_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  kpn_trait_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_number INT UNSIGNED NOT NULL,
  label TEXT NOT NULL,
  input_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_revision CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  metadata JSON NOT NULL,
  UNIQUE KEY ref_factor_public (public_id),
  UNIQUE KEY ref_factor_eaggl (eaggl_factor_id),
  INDEX ref_factor_trait (kpn_trait_id, factor_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_factor_genes{suffix} (
  factor_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene VARCHAR(64) NOT NULL,
  loading DOUBLE NOT NULL,
  PRIMARY KEY (factor_key, gene),
  INDEX ref_factor_gene (gene)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_collections{suffix} (
  collection_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  cfde_label VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  library VARCHAR(64) NOT NULL,
  n_sets INT UNSIGNED NOT NULL,
  payload JSON NOT NULL,
  UNIQUE KEY ref_collection_label (cfde_label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_gene_sets{suffix} (
  gene_set_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  collection_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set_name TEXT NOT NULL,
  library VARCHAR(64) NOT NULL,
  n_genes INT UNSIGNED NOT NULL,
  n_genes_in_eaggl_universe INT UNSIGNED NOT NULL,
  legacy_source_key TEXT NULL,
  metadata JSON NOT NULL,
  INDEX ref_gene_set_collection (collection_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_projections{suffix} (
  factor_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  library VARCHAR(64) NOT NULL,
  joint_loading DOUBLE NOT NULL,
  marginal_loading DOUBLE NOT NULL,
  joint_loading_text VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  marginal_loading_text VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  joint_rank INT UNSIGNED NOT NULL,
  marginal_rank INT UNSIGNED NOT NULL,
  is_joint_top_factor TINYINT NOT NULL,
  PRIMARY KEY (factor_key, gene_set_id),
  INDEX ref_projection_gene_set (gene_set_id),
  INDEX ref_projection_library (factor_key, library, joint_rank)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_dapper_nodes{suffix} (
  id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  class_name VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  payload JSON NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_dapper_edges{suffix} (
  subject VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  predicate VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  object VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  edge_role VARCHAR(64) NULL,
  PRIMARY KEY (subject, predicate, object),
  INDEX ref_dapper_edge_object (object)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_trait_gene_sets{suffix} (
  kpn_trait_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  library VARCHAR(64) NOT NULL,
  beta_uncorrected DOUBLE NOT NULL,
  beta DOUBLE NOT NULL,
  avg_postp DOUBLE NOT NULL,
  library_rank INT UNSIGNED NOT NULL,
  PRIMARY KEY (kpn_trait_id, gene_set_id),
  INDEX ref_trait_gene_set (gene_set_id),
  INDEX ref_trait_gene_set_library (kpn_trait_id, library, library_rank)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE reveal_ref_release{suffix} (
  release_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  published_at TIMESTAMP(6) NOT NULL,
  manifest JSON NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
'''
# Shared and never purged: migration 008's definition, created here only when absent.
ARCHIVED_TABLE = '''CREATE TABLE IF NOT EXISTS archived_reference_factors (
  archive_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  source_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  model VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  trait VARCHAR(255) NOT NULL,
  kpn_trait_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
  label TEXT NOT NULL,
  snapshot JSON NOT NULL,
  snapshot_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY archived_factor_source (generation_id, source_id_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''
ARCHIVED_COLUMNS = ('archive_id', 'generation_id', 'source_id', 'source_id_sha256', 'model', 'factor_id', 'trait', 'kpn_trait_id', 'label',
                    'snapshot', 'snapshot_sha256')
STRICT_MODE = "SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')"

# LAP project outputs <stem>.<suffix> (columns required).
LAP_FILES = {
    'trait_kpn_map': ('trait_kpn_map.tsv', ('trait', 'kpn_trait_id', 'kpn_release', 'kpn_release_commit', 'gwas_source_category',
                                            'phenotype_name', 'trait_group', 'legacy_trait_group', 'trait_type', 'n_factors')),
    'factor_index': ('factor_index.tsv', ('global_eaggl_column', 'factor_id', 'trait', 'kpn_trait_id', 'factor', 'factor_number',
                                          'factor_label', 'n_nonzero_loadings', 'loading_l2', 'loading_variant')),
    'factor_metadata': ('factor_metadata.tsv', ('factor_id', 'trait', 'factor', 'factor_number', 'label')),
    'gene_set_index': ('gene_set_index.tsv', ('gene_set_id', 'gene_set_name', 'collection_id', 'cfde_label', 'library', 'partition',
                                                 'model', 'comparison', 'program', 'gmt_row', 'n_genes', 'n_genes_in_eaggl_universe', 'cfde_snapshot')),
    'projection_manifest': ('projection_manifest.tsv', ('trait', 'kpn_trait_id', 'kpn_release', 'n_factors', 'pigean_commit', 'qc_pass')),
    'cfde_index': ('cfde_index.tsv', ('library', 'partition', 'model', 'comparison', 'program', 'label', 'collection_id', 'n_sets', 'n_genes')),
    'kpn_trait_registry': ('kpn_trait_registry.tsv', ('portal_id', 'gwas_source_category', 'legacy_phenotype_id', 'phenotype_name',
                                                      'legacy_trait_group', 'trait_group', 'trait_type', 'pigean_id')),
    'kpn_trait_flat': ('kpn_trait_flat.tsv', ('portal_id', 'description', 'is_dichotomous', 'is_complex', 'target_id', 'target_label',
                                              'target_ontology', 'mapping_predicate', 'confidence')),
    'factors_by_genes': ('all_factors.factors_by_genes.tsv', ('Factor',)),
}
MAPPING_COLUMNS = ('target_id', 'target_label', 'target_ontology', 'mapping_predicate', 'confidence', 'mapping_justification', 'source')


class Refused(RuntimeError):
    """A protection refused the command; nothing further was changed."""


class Services:
    """Connections, clients, embedder and clock. The CLI uses these defaults; tests replace them."""
    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ
        self.err = sys.stderr
    def connect(self):
        from .runtime_config import mysql_connection
        return mysql_connection()
    def repository(self, prefix): return Repository(table_prefix=prefix)
    def clock(self): return datetime.now(timezone.utc)
    def sleep(self, seconds): time.sleep(seconds)
    def embed(self, texts, **options):
        from .embedding_client import get_embeddings
        return get_embeddings(texts, **options)
    def vector_client(self, env):
        """The Upstash index with write credentials: UPSTASH_VECTOR_REST_URL/_WRITE_TOKEN, or both _<ENV> overrides."""
        import httpx
        from upstash_vector import Index
        suffix, environ = env.upper(), self.environ
        if environ.get(f'UPSTASH_VECTOR_REST_URL_{suffix}') or environ.get(f'UPSTASH_VECTOR_WRITE_TOKEN_{suffix}'):
            url, token = environ.get(f'UPSTASH_VECTOR_REST_URL_{suffix}', ''), environ.get(f'UPSTASH_VECTOR_WRITE_TOKEN_{suffix}', '')
        else: url, token = environ.get('UPSTASH_VECTOR_REST_URL', ''), environ.get('UPSTASH_VECTOR_WRITE_TOKEN', '')
        if not url.startswith('https://') or not token:
            raise Refused(f'Configure UPSTASH_VECTOR_REST_URL and UPSTASH_VECTOR_WRITE_TOKEN (or both _{suffix} overrides)')
        index = Index(url=url, token=token, retries=3, retry_interval=1, allow_telemetry=False)
        index._client.timeout = httpx.Timeout(60, connect=10)  # SDK 0.8.0 hardcodes 600 seconds
        return index


def emit(services, message): print(message, file=services.err, flush=True)


# --------------------------------------------------------------------------------------
# Files


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()


def _open_text(path):
    return gzip.open(path, 'rt', encoding='utf-8', newline='') if str(path).endswith('.gz') else open(path, encoding='utf-8', newline='')


def iter_tsv(path, required=()):
    with _open_text(path) as stream:
        reader = csv.DictReader(stream, delimiter='\t', quoting=csv.QUOTE_NONE)
        header = reader.fieldnames or []
        missing = [column for column in required if column not in header]
        if missing or len(set(header)) != len(header): raise Refused(f'{Path(path).name}: missing or duplicate columns {missing}')
        for row in reader:
            if None in row or None in row.values(): raise Refused(f'{Path(path).name}: malformed row {reader.line_num}')
            yield row


def read_tsv(path, required=()): return list(iter_tsv(path, required))


def tsv_rows(path, columns):
    """Rows of a release TSV (fixed columns, no quoting) as dicts."""
    columns = list(columns)
    with _open_text(path) as stream:
        if stream.readline().rstrip('\n').split('\t') != columns: raise Refused(f'{Path(path).name}: expected columns {columns}')
        for number, line in enumerate(stream, 2):
            fields = line.rstrip('\n').split('\t')
            if len(fields) != len(columns): raise Refused(f'{Path(path).name}: malformed row {number}')
            yield dict(zip(columns, fields))


@contextmanager
def _writer(path):
    """Text writer; .gz output depends only on content (no mtime or file name; fixed level 6)."""
    if str(path).endswith('.gz'):
        with open(path, 'wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0, compresslevel=6) as packed, \
                io.TextIOWrapper(packed, encoding='utf-8', newline='') as text:
            yield text
    else:
        with open(path, 'w', encoding='utf-8', newline='') as text: yield text


def write_jsonl(path, rows):
    count = 0
    with _writer(path) as out:
        for row in rows: out.write(canonical(row) + '\n'); count += 1
    return count


def write_tsv(path, columns, rows):
    count = 0
    with _writer(path) as out:
        out.write('\t'.join(columns) + '\n')
        for row in rows:
            if any('\t' in value or '\n' in value for value in row): raise Refused(f'{Path(path).name}: tab or newline in {row[:2]}')
            out.write('\t'.join(row) + '\n'); count += 1
    return count


def read_jsonl(path):
    with _open_text(path) as stream:
        for line in stream:
            if line.strip(): yield json.loads(line)


def read_json(path): return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)
    return path


def _jsonable(value): return json.loads(json.dumps(value, default=str))


def _chunks(values, size):
    for start in range(0, len(values), size): yield values[start:start + size]


# --------------------------------------------------------------------------------------
# build: LAP tables (rows and validation)


def lap_inputs(project_dir):
    project_dir = Path(project_dir)
    stems = [path.name[:-len('.projection_manifest.tsv')] for path in project_dir.glob('*.projection_manifest.tsv')]
    if len(stems) != 1: raise Refused(f'{project_dir}: expected one *.projection_manifest.tsv, found {len(stems)}')
    paths = {role: project_dir / f'{stems[0]}.{suffix}' for role, (suffix, _) in LAP_FILES.items()}
    missing = [path.name for path in paths.values() if not path.is_file()]
    if missing: raise Refused(f'{project_dir}: missing LAP outputs {missing}')
    return stems[0], paths


def _flag(value):
    value = (value or '').strip().lower()
    if value in ('', 'na', 'nan', 'none'): return None
    if value in ('1', 'true', 'yes'): return 1
    if value in ('0', 'false', 'no'): return 0
    raise Refused(f'Unrecognised boolean {value!r}')


def _number(value, where, kind=int):
    try:
        number = kind(value)
    except (TypeError, ValueError) as error: raise Refused(f'{where}: invalid number {value!r}') from error
    if kind is float and number != number: raise Refused(f'{where}: NaN')
    return number


def _single(values, what):
    values = set(values)
    if len(values) != 1: raise Refused(f'Expected one {what}, found {sorted(values)[:5]}')
    return values.pop()


def lap_tables(paths, kpn_release=None):
    """Traits, factors (source_revision still unset), gene sets and the CFDE index of a LAP project."""
    table = {role: read_tsv(paths[role], LAP_FILES[role][1]) for role in LAP_FILES if role not in ('gene_set_index', 'factors_by_genes')}
    qc = table['projection_manifest']
    failed = sorted(row['trait'] for row in qc if row['qc_pass'] != 'True')
    if failed or not qc: raise Refused(f'LAP projection QC did not pass for {len(failed)} traits: {failed[:10]}')
    kpn_map = {row['trait']: row for row in table['trait_kpn_map']}
    if len(kpn_map) != len(table['trait_kpn_map']) or {row['trait'] for row in qc} != set(kpn_map) or len(qc) != len(kpn_map):
        raise Refused('Projection manifest traits differ from the trait KPN map')
    release = _single((row['kpn_release'] for row in table['trait_kpn_map']), 'KPN release')
    release_commit = _single((row['kpn_release_commit'] for row in table['trait_kpn_map']), 'KPN release commit')
    pigean_commit = _single((row['pigean_commit'] for row in qc), 'pigean commit')
    if kpn_release and kpn_release != release: raise Refused(f'LAP outputs are KPN {release}, not {kpn_release}')
    if {row['kpn_release'] for row in qc} != {release}: raise Refused('Projection manifest KPN release differs from the trait map')
    registry = {row['portal_id']: row for row in table['kpn_trait_registry']}
    flat = {}
    for row in table['kpn_trait_flat']: flat.setdefault(row['portal_id'], []).append(row)
    traits, by_trait = [], {}
    for trait, row in sorted(kpn_map.items(), key=lambda item: item[1]['kpn_trait_id']):
        kpn = row['kpn_trait_id']; rg.kpn_number(kpn)
        entry = registry.get(kpn)
        if not entry or entry['legacy_phenotype_id'] != trait: raise Refused(f'{kpn} is not the registry entry of trait {trait}')
        extras = flat.get(kpn) or [{}]
        mappings = sorted(({key: extra.get(key, '') for key in MAPPING_COLUMNS} for extra in extras if extra.get('target_id')),
                          key=lambda item: (item['target_ontology'], item['target_id'], item['source']))
        record = {'kpn_trait_id': kpn, 'legacy_phenotype_id': trait, 'phenotype_name': row['phenotype_name'],
                  'gwas_source_category': row['gwas_source_category'], 'trait_group': row['trait_group'] or None,
                  'legacy_trait_group': row['legacy_trait_group'] or None, 'trait_type': row['trait_type'] or None,
                  'description': extras[0].get('description') or None, 'is_dichotomous': _flag(extras[0].get('is_dichotomous')),
                  'is_complex': _flag(extras[0].get('is_complex')), 'n_factors': _number(row['n_factors'], trait),
                  'metadata': {'kpn_release': release, 'kpn_release_commit': release_commit, 'pigean_id': entry.get('pigean_id') or None,
                               'ontology_mappings': mappings}}
        if kpn in by_trait.values(): raise Refused(f'KPN trait {kpn} maps to several EAGGL traits')
        by_trait[trait] = kpn; traits.append(record)
    metadata_rows = {row['factor_id']: row for row in table['factor_metadata']}
    index_rows = {row['factor_id']: row for row in table['factor_index']}
    if len(metadata_rows) != len(table['factor_metadata']) or set(metadata_rows) != set(index_rows) or len(index_rows) != len(table['factor_index']):
        raise Refused('factor_metadata and factor_index list different factors')
    factors, keys, per_trait = [], {}, {}
    for eaggl_id, entry in sorted(index_rows.items()):
        source = metadata_rows[eaggl_id]
        if any(source[key] != entry[key] for key in ('trait', 'factor', 'factor_number')) or source['label'] != entry['factor_label']:
            raise Refused(f'{eaggl_id}: factor_metadata and factor_index disagree')
        trait = entry['trait']
        if trait not in by_trait or by_trait[trait] != entry['kpn_trait_id']: raise Refused(f'{eaggl_id}: KPN trait id disagrees with the trait map')
        if eaggl_id != f"{trait}::{entry['factor']}": raise Refused(f'{eaggl_id}: factor id is not trait::FactorN')
        key = rg.factor_key(entry['kpn_trait_id'], entry['factor'])
        text = source['label'].strip()
        if not text: raise Refused(f'{eaggl_id}: empty label')
        kpn = kpn_map[trait]
        metadata = {**entry, **source, 'kpn': {'phenotype_name': kpn['phenotype_name'], 'trait_group': kpn['trait_group'],
                    'trait_type': kpn['trait_type'], 'gwas_source_category': kpn['gwas_source_category']}}
        factors.append({'factor_key': key, 'public_id': rg.public_id(entry['kpn_trait_id'], entry['factor']), 'eaggl_factor_id': eaggl_id,
                        'kpn_trait_id': entry['kpn_trait_id'], 'factor_number': _number(entry['factor_number'], eaggl_id),
                        'label': source['label'], 'input_sha256': hashlib.sha256(text.encode()).hexdigest(), 'source_revision': None,
                        'metadata': metadata})
        keys[eaggl_id] = key; per_trait[trait] = per_trait.get(trait, 0) + 1
    bad = sorted(trait for trait, record in kpn_map.items() if per_trait.get(trait, 0) != int(record['n_factors']))
    if bad: raise Refused(f'Factor counts differ from the trait map for {bad[:10]}')
    index = {row['collection_id']: row for row in table['cfde_index']}
    if len(index) != len(table['cfde_index']) or len({row['label'] for row in index.values()}) != len(index):
        raise Refused('cfde_index has duplicate collections or labels')
    gene_sets, members = [], {}
    for row in iter_tsv(paths['gene_set_index'], LAP_FILES['gene_set_index'][1]):
        collection = index.get(row['collection_id'])
        if not collection or collection['label'] != row['cfde_label'] or collection['library'] != row['library']:
            raise Refused(f"{row['gene_set_id']}: collection {row['collection_id']} disagrees with cfde_index")
        if not re.fullmatch(r'dapper:GeneSet\.[A-Za-z0-9_-]{32}', row['gene_set_id']): raise Refused(f"Invalid gene set id {row['gene_set_id']!r}")
        members[row['collection_id']] = members.get(row['collection_id'], 0) + 1
        gene_sets.append({'gene_set_id': row['gene_set_id'], 'collection_id': row['collection_id'], 'gene_set_name': row['gene_set_name'],
                          'library': row['library'], 'n_genes': _number(row['n_genes'], row['gene_set_id']),
                          'n_genes_in_eaggl_universe': _number(row['n_genes_in_eaggl_universe'], row['gene_set_id']),
                          'legacy_source_key': f"{row['library']}__{row['partition']}__{row['gene_set_name']}",
                          'metadata': {key: row[key] for key in ('cfde_label', 'partition', 'model', 'comparison', 'program', 'gmt_row', 'cfde_snapshot')}})
    gene_sets.sort(key=lambda item: item['gene_set_id'])
    if len({row['gene_set_id'] for row in gene_sets}) != len(gene_sets): raise Refused('gene_set_index has duplicate gene sets')
    for collection_id, row in index.items():
        if not re.fullmatch(r'dapper:GeneSetCollection\.[A-Za-z0-9_-]{32}', collection_id): raise Refused(f'Invalid collection id {collection_id!r}')
        if members.get(collection_id, 0) != _number(row['n_sets'], row['label']): raise Refused(f"{row['label']}: n_sets differs from its gene sets")
    return {'traits': traits, 'factors': sorted(factors, key=lambda item: item['factor_key']), 'keys': keys, 'index_rows': index_rows,
            'kpn_map': kpn_map, 'gene_sets': gene_sets, 'members': members, 'index': index, 'kpn_release': release,
            'kpn_release_commit': release_commit, 'pigean_commit': pigean_commit}


def write_factor_genes(path, out, keys, index_rows):
    """factor_genes.tsv.gz: every nonzero cell of the factors-by-genes table, loading text as written by LAP.

    Returns (rows, {factor_key: loadings_sha256}, {factor_key: top genes}); loadings_sha256 hashes the factor's
    sorted "gene\\tloading_text\\n" lines.
    """
    name, count, sums, top = Path(path).name, 0, {}, {}
    with _open_text(path) as stream, _writer(out) as writer:
        header = stream.readline().rstrip('\n').split('\t')
        if header[:1] != ['Factor'] or len(set(header)) != len(header): raise Refused(f'{name}: expected Factor and unique gene columns')
        if any(len(gene) > 64 for gene in header): raise Refused(f'{name}: gene symbol longer than 64 characters')
        writer.write('\t'.join(FACTOR_GENE_COLUMNS) + '\n')
        for number, line in enumerate(stream, 2):
            fields = line.rstrip('\n').split('\t')
            key = keys.get(fields[0])
            if len(fields) != len(header) or not key or key in sums: raise Refused(f'{name}: unknown, repeated or malformed factor row {number}')
            pairs = []  # (gene, loading text, loading) in column order; most cells are 0.0
            for position in [position for position, value in enumerate(fields) if value != '0.0' and value != '0' and position]:
                loading = _number(fields[position], f'{fields[0]} {header[position]}', float)
                if loading != 0.0: pairs.append((header[position], fields[position], loading))
            if len(pairs) != _number(index_rows[fields[0]]['n_nonzero_loadings'], fields[0]):
                raise Refused(f'{fields[0]}: {len(pairs)} nonzero loadings, the factor index says {index_rows[fields[0]]["n_nonzero_loadings"]}')
            writer.write(''.join(f'{key}\t{gene}\t{value}\n' for gene, value, _ in pairs))
            sums[key] = hashlib.sha256(''.join(f'{gene}\t{value}\n' for gene, value, _ in sorted(pairs)).encode()).hexdigest()
            top[key] = [{'symbol': gene, 'loading': loading} for gene, _, loading in sorted(pairs, key=lambda pair: -pair[2])[:TOP_GENES]]
            count += len(pairs)
    missing = sorted(set(keys.values()) - set(sums))
    if missing: raise Refused(f'{name} lacks {len(missing)} factors, e.g. {missing[:3]}')
    return count, sums, top


# --------------------------------------------------------------------------------------
# build: worker tasks (one process each; module level so a spawned pool can import them)


def read_collection(path, *, limit=32 << 20):
    """One streaming pass over a DAPPER GeneSetCollection document:
    {header, document, gene_sets: {gene set id: canonical node}, tail, provenance, sha256}.

    The header is everything before the top-level `gene_sets:` list. Each `- ` item of that list is parsed on its
    own (a LINCS document is up to 167 MB); the list ends at the next top-level key, and the sections after it
    (the collection, provenance nodes and edges in DAPPER 0.2.0 documents) are parsed together. A top-level section
    may appear only once in the document; SKIPPED_SECTIONS (embeddings) are passed over line by line, never parsed.
    `provenance` is collection_provenance of the header and tail.
    """
    import yaml
    loader, name = getattr(yaml, 'CSafeLoader', yaml.SafeLoader), Path(path).name
    def load(lines, where):
        try: return _jsonable(yaml.load(''.join(lines), Loader=loader))
        except yaml.YAMLError as error: raise Refused(f'{name}: invalid YAML {where}') from error
    head, size, nodes, tail, seen, skipping = [], 0, {}, [], set(), False
    def kept(line):
        """Whether a line outside the gene_sets list is parsed: not inside a SKIPPED_SECTIONS section."""
        nonlocal skipping
        match = SECTION_RE.match(line)
        if match:
            if match[1] in seen: raise Refused(f'{name}: duplicate collection section {match[1]}')
            seen.add(match[1]); skipping = match[1] in SKIPPED_SECTIONS
        return not skipping
    def add(lines, start):
        items = load(lines, f'near line {start}')
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or not isinstance(items[0].get('id'), str):
            raise Refused(f'{name}: malformed gene set near line {start}')
        if items[0]['id'] in nodes: raise Refused(f"{name}: duplicate gene set {items[0]['id']}")
        nodes[items[0]['id']] = canonical(items[0])
    with open(path, encoding='utf-8') as stream:
        number = 0
        for line in stream:
            number += 1
            if not kept(line): continue
            if line.startswith('gene_sets:'): break
            size += len(line)
            if size > limit: raise Refused(f'{name}: collection header exceeds {limit} bytes')
            head.append(line)
        else: raise Refused(f'{name}: no top-level gene_sets list')
        if line.split(':', 1)[1].strip() not in ('', '[]'): raise Refused(f'{name}: gene_sets must be a block list')
        seen.add('gene_sets')
        item, start = [], number
        for line in stream:
            number += 1
            if line.startswith('- ') or line.rstrip('\n') == '-':
                if item: add(item, start)
                item, start = [line], number
            elif line[:1] in (' ', '\n', '#'):
                if item: item.append(line)
            else:
                if kept(line): tail.append(line)
                break
        if item: add(item, start)
        tail.extend(line for line in stream if kept(line))
    header, rest = load(head, 'before gene_sets') or {}, load(tail, 'after gene_sets') or {}
    if not isinstance(header, dict) or not isinstance(rest, dict): raise Refused(f'{name}: expected top-level mappings')
    collections = header.get('gene_set_collections') or rest.get('gene_set_collections') or []
    if not isinstance(collections, list) or len(collections) != 1 or not isinstance(collections[0], dict):
        raise Refused(f'{name}: expected exactly one GeneSetCollection')
    return {'header': header, 'document': {key: value for key, value in collections[0].items() if key != 'members'},
            'gene_sets': nodes, 'tail': rest, 'provenance': collection_provenance(name, header, rest), 'sha256': _sha256_file(path)}


def collection_provenance(name, header, tail):
    """The provenance a collection document retains: every provenance_schema PROVENANCE_GROUPS section present before
    or after its gene_sets list (DAPPER 0.2.0 documents put organizations, datasets and edges after it).

    Node and edge groups must be lists of objects, every edge names a subject, predicate and object, and edges must
    validate against the current DAPPER schema. reveal_ref_collections.payload.provenance holds it, and
    reference_evidence resolves a gene set's dependencies and edges from it.
    """
    sections = {**header, **tail}  # read_collection refuses a section that appears twice
    provenance = {group: sections[group] for group in PROVENANCE_GROUPS if sections.get(group) is not None}
    for group, items in provenance.items():
        if group == 'prefixes': continue
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise Refused(f'{name}: {group} must be a list of objects')
        if group in PROVENANCE_EDGES and any(not isinstance(item.get(key), str) or not item[key]
                                             for item in items for key in ('subject', 'predicate', 'object')):
            raise Refused(f'{name}: malformed provenance edge in {group}')
    try: validate_provenance_edges(provenance)
    except ValueError as error: raise Refused(f'{name}: {error}') from error  # EvidenceBuildError
    return provenance


def long_file_ranks(path, top_n):
    """The per-library top rows of one per-trait long file: [(factor_id, kpn_trait_id, rows)], rows (gene_set_id, library,
    joint_loading, marginal_loading, joint_rank, marginal_rank, is_joint_top_factor) in (library, joint_rank) order.

    LAP (projection_workflow.py project-trait) ranks every gene set within its library over all the gene sets and writes
    the library ranks; a row is kept when either is <= top_n, and the kept ranks of each library must run 1..n. LAP
    writes each factor's rows together.
    """
    name, factors, seen = Path(path).name, [], set()
    factor = kpn = None; libraries = {}
    with _open_text(path) as stream:
        if stream.readline().rstrip('\n').split('\t') != LONG_COLUMNS: raise Refused(f'{name}: not a projection long file')
        for number, line in enumerate(stream, 2):
            f = line.rstrip('\n').split('\t')
            if len(f) != len(LONG_COLUMNS): raise Refused(f'{name}: malformed row {number}')
            if f[2] != factor:
                if f[2] in seen: raise Refused(f'{name}: the rows of {f[2]} are not contiguous')
                factor, kpn, libraries = f[2], f[1], {}; seen.add(factor); factors.append((factor, kpn, libraries))
            elif f[1] != kpn: raise Refused(f'{name}: {factor} has several KPN trait ids')
            try: libraries.setdefault(f[8], []).append((f[5], f[8], f[9], f[10], int(f[14]), int(f[15]), f[13]))
            except ValueError: raise Refused(f'{name}: invalid rank in row {number}') from None
    result = []
    for factor, kpn, libraries in factors:
        kept = []
        for library in sorted(libraries):
            rows = sorted((row for row in libraries[library] if row[4] <= top_n or row[5] <= top_n), key=itemgetter(4))
            for position in (4, 5):
                ranks = sorted(row[position] for row in rows if row[position] <= top_n)
                if ranks != list(range(1, len(ranks) + 1)): raise Refused(f'{name}: {factor} ranks in {library} are not 1..n')
            kept.extend(rows)
        result.append((factor, kpn, kept))
    return result


def _long_file_trait(path):
    with _open_text(path) as stream:
        header, first = stream.readline().rstrip('\n').split('\t'), stream.readline().rstrip('\n').split('\t')
    if header != LONG_COLUMNS or len(first) != len(LONG_COLUMNS): raise Refused(f'{Path(path).name}: not a projection long file with rows')
    return first[0]


@contextmanager
def _tasks(workers):
    """submit(function, *args) -> Future: a spawned process pool, or inline when workers <= 1."""
    if workers <= 1:
        def submit(function, *args):
            future = Future()
            try: future.set_result(function(*args))
            except Exception as error: future.set_exception(error)
            return future
        yield submit
        return
    pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'))
    try: yield pool.submit
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True); raise
    pool.shutdown(wait=True)


# --------------------------------------------------------------------------------------
# build: release files


def write_projections(path, futures, keys, index_rows, libraries):
    """projections.tsv.gz in factor_key order from the long-file results [(kpn_trait_id, future)], each released once written."""
    seen, count = set(), 0
    futures = sorted(futures, key=lambda item: item[0], reverse=True)
    with _writer(path) as out:
        out.write('\t'.join(PROJECTION_COLUMNS) + '\n')
        while futures:
            kpn, future = futures.pop()
            results = future.result(); del future
            for factor_id, factor_kpn, rows in sorted(results, key=lambda item: keys.get(item[0], '')):
                key = keys.get(factor_id)
                if not key or index_rows[factor_id]['kpn_trait_id'] != factor_kpn or factor_kpn != kpn or key in seen:
                    raise Refused(f'{factor_id}: unknown, repeated or misfiled factor in the long files')
                seen.add(key)
                if not rows: raise Refused(f'{factor_id}: no projections')
                sets = set()
                for gene_set, library, joint, marginal, joint_rank, marginal_rank, flag in rows:
                    where = f'{factor_id}/{gene_set}'
                    if libraries.get(gene_set) != library or gene_set in sets: raise Refused(f'{where}: unknown, repeated or misfiled gene set')
                    for value in (joint, marginal):
                        _number(value, where, float)
                        if len(value) > 32: raise Refused(f'{where}: loading text too long')
                    if flag not in ('0', '1'): raise Refused(f'{where}: invalid is_joint_top_factor')
                    sets.add(gene_set)
                    out.write(f'{key}\t{gene_set}\t{library}\t{joint}\t{marginal}\t{joint_rank}\t{marginal_rank}\t{flag}\n')
                count += len(rows)
    missing = sorted(set(keys.values()) - seen)
    if missing: raise Refused(f'{len(missing)} factors have no projections (missing long files?), e.g. {missing[:3]}')
    return count


def write_collections(directory, lap, documents, submit, in_flight):
    """collections.jsonl.gz and gene_sets.jsonl.gz (+ each exact GeneSet node) and the DAPPER graph, one collection
    document at a time in collection_id order, with at most `in_flight` parsed documents in memory.

    A collection's payload.provenance is its document's collection_provenance (every provenance node and edge group).
    Graph nodes: organizations, datasets, files, activities and the collection (without members); edges: every
    *_edges section but SKIPPED_SECTIONS, before or after the gene sets. First occurrence wins; a later differing
    variant is counted. Returns (collections, gene sets, nodes, edges, variants skipped).
    """
    by_collection = {}
    for row in lap['gene_sets']: by_collection.setdefault(row['collection_id'], []).append(row)
    nodes, edges, variants, counts = {}, {}, 0, {'collections': 0, 'gene_sets': 0}
    order = iter(sorted(documents))
    pending = deque((identity, submit(read_collection, documents[identity])) for identity in islice(order, in_flight))
    with _writer(directory / 'collections.jsonl.gz') as collections_out, _writer(directory / 'gene_sets.jsonl.gz') as gene_sets_out:
        while pending:
            collection_id, future = pending.popleft()
            following = next(order, None)
            if following: pending.append((following, submit(read_collection, documents[following])))
            parsed, row = future.result(), lap['index'][collection_id]
            header, document, count = parsed['header'], parsed['document'], lap['members'].get(collection_id, 0)
            if document.get('id') != collection_id or document.get('n_sets', count) != count:
                raise Refused(f"{row['label']}: document does not describe collection {collection_id}")
            members = by_collection.get(collection_id, [])
            if set(parsed['gene_sets']) != {member['gene_set_id'] for member in members}:
                raise Refused(f"{row['label']}: document gene sets differ from gene_set_index")
            collections_out.write(canonical({'collection_id': collection_id, 'cfde_label': row['label'], 'library': row['library'], 'n_sets': count,
                'payload': {'collection': document, 'index': row, 'document_sha256': parsed['sha256'], 'provenance': parsed['provenance']}}) + '\n')
            for member in members:
                gene_sets_out.write(canonical({**member, 'metadata': {**member['metadata'], 'dapper_gene_set': json.loads(parsed['gene_sets'][member['gene_set_id']])}}) + '\n')
            counts['collections'] += 1; counts['gene_sets'] += len(members)
            sections = {**header, **parsed['tail']}  # each section appears once (read_collection)
            for section in NODE_SECTIONS:
                for node in sections.get(section) or []:
                    match = DAPPER_ID_RE.fullmatch(str(node.get('id') if isinstance(node, dict) else ''))
                    if not match: raise Refused(f'{collection_id}: {section} entry without a DAPPER id')
                    if section == 'gene_set_collections': node = {key: value for key, value in node.items() if key != 'members'}
                    if node['id'] in nodes: variants += nodes[node['id']]['payload'] != node; continue
                    nodes[node['id']] = {'id': node['id'], 'class_name': match[1], 'payload': node}
            for section, entries in sections.items():
                if not section.endswith('_edges') or section in SKIPPED_SECTIONS: continue
                for edge in entries or []:
                    values = [edge.get(key) if isinstance(edge, dict) else None for key in EDGE_COLUMNS]
                    if not all(isinstance(value, str) and value for value in values[:3]) or not isinstance(values[3], (str, type(None))):
                        raise Refused(f'{collection_id}: malformed {section} entry')
                    key = tuple(values[:3])
                    if key in edges: variants += edges[key] != values[3]; continue
                    edges[key] = values[3]
            del parsed
    return counts['collections'], counts['gene_sets'], [nodes[key] for key in sorted(nodes)], [(*key, edges[key] or '') for key in sorted(edges)], variants


class VectorCache:
    """The SQLite vector cache (CACHE_SCHEMA): one float32 LE vector per (text sha256, kind); kind is factor_label or context."""
    def __init__(self, path):
        if not Path(path).is_file(): raise Refused(f'{path}: vector cache not found')
        self.connection = sqlite3.connect(str(path), timeout=60)
        self.meta = dict(self.connection.execute('SELECT key,value FROM meta').fetchall())
        if not self.meta.get('model') or not str(self.meta.get('dimensions') or '').isdigit() or int(self.meta['dimensions']) < 1:
            raise Refused(f'{path}: the cache meta needs model and dimensions')
        self.dimensions = int(self.meta['dimensions'])
    def close(self): self.connection.close()
    def vector(self, sha, dimensions, blob):
        import numpy as np
        vector = np.frombuffer(bytes(blob), dtype='<f4')
        if dimensions != self.dimensions or vector.shape != (self.dimensions,) or not np.isfinite(vector).all() or not vector.any():
            raise Refused(f'Vector cache row {sha[:12]}: not a finite nonzero {self.dimensions}-dimensional float32 vector')
        return vector
    def lookup(self, shas):
        """{sha: vector} of the cached texts; identical text has one vector, so any kind serves (factor_label first)."""
        found = {}
        for batch in _chunks(sorted(set(shas)), 500):
            for sha, dimensions, blob in self.connection.execute(
                    f"SELECT input_sha256,dimensions,vector FROM vectors WHERE input_sha256 IN ({','.join('?' * len(batch))}) "
                    "ORDER BY input_sha256,kind<>'factor_label',kind", batch):
                if sha not in found: found[sha] = self.vector(sha, dimensions, blob)
        return found
    def contexts(self):
        rows = []
        for sha, text, dimensions, blob in self.connection.execute(
                "SELECT input_sha256,text,dimensions,vector FROM vectors WHERE kind='context' ORDER BY input_sha256"):
            if hashlib.sha256(text.encode()).hexdigest() != sha: raise Refused(f'Vector cache row {sha[:12]}: input_sha256 is not the sha256 of its text')
            rows.append((sha, self.vector(sha, dimensions, blob)))
        return rows
    def probes(self, count):
        import numpy as np
        total = self.connection.execute('SELECT COUNT(*) FROM vectors').fetchone()[0]
        if not total: raise Refused('The vector cache is empty: no cached text to calibrate the embedding service with')
        positions = sorted({int(position) for position in np.linspace(0, total - 1, min(count, total))})
        rows = [self.connection.execute('SELECT input_sha256,text,dimensions,vector FROM vectors ORDER BY input_sha256,kind LIMIT 1 OFFSET ?',
                                        (position,)).fetchone() for position in positions]
        return [(text, self.vector(sha, dimensions, blob)) for sha, text, dimensions, blob in rows]
    def add(self, rows):
        """Newly embedded factor labels (kind factor_label)."""
        for statement in CACHE_SCHEMA: self.connection.execute(statement)
        with self.connection:
            self.connection.executemany('INSERT INTO vectors(input_sha256,kind,text,dimensions,vector) VALUES (?,?,?,?,?)',
                                        [(hashlib.sha256(text.encode()).hexdigest(), 'factor_label', text, self.dimensions,
                                          vector.astype('<f4').tobytes()) for text, vector in rows])


def _matrix(values, count, dimensions):
    """Finite, nonzero float32 little-endian rows (eaggl_embeddings.validate_vectors)."""
    import numpy as np
    raw = np.asarray(values)
    if raw.dtype.kind not in 'fiu': raise Refused('Embedding values must be numeric')
    with np.errstate(over='ignore', invalid='ignore'): matrix = raw.astype('<f4')
    if count == 0: matrix = matrix.reshape(0, dimensions)
    if matrix.shape != (count, dimensions): raise Refused(f'Expected {count} vectors of {dimensions} dimensions, found shape {matrix.shape}')
    if not np.isfinite(matrix).all() or (count and np.any(np.linalg.norm(matrix.astype(np.float64), axis=1) == 0)):
        raise Refused('Embeddings must be finite and nonzero')
    return np.ascontiguousarray(matrix, dtype='<f4')


def embed_missing(services, cache, texts):
    """Embed texts absent from the cache, only after CALIBRATION_TEXTS cached texts re-embed to cosine >= 0.999."""
    import numpy as np
    from .embedding_client import DEFAULT_MODEL, DEFAULT_SERVICE_URL
    env = services.environ
    model = env.get('EMBEDDING_MODEL') or DEFAULT_MODEL
    options = {'model': model, 'service_url': (env.get('EMBEDDING_SERVICE_URL') or DEFAULT_SERVICE_URL).rstrip('/'),
               'provider': env.get('EMBEDDING_PROVIDER') or cache.meta.get('provider') or 'huggingface', 'max_retries': 3, 'timeout': 120}
    if model != cache.meta['model']: raise Refused(f"EMBEDDING_MODEL {model} is not the vector cache model {cache.meta['model']}")
    probes = cache.probes(CALIBRATION_TEXTS)
    emit(services, f'Calibrating {len(probes)} cached texts against {model} before embedding {len(texts)} new factor labels')
    fresh = _matrix(services.embed([text for text, _ in probes], batch_size=len(probes), max_workers=1, **options), len(probes), cache.dimensions)
    cosines = [float(stored.astype(np.float64) @ probe.astype(np.float64) / (np.linalg.norm(stored.astype(np.float64)) * np.linalg.norm(probe.astype(np.float64))))
               for (_, stored), probe in zip(probes, fresh)]
    if min(cosines) < CALIBRATION_THRESHOLD:
        raise Refused(f'Calibration failed: minimum cosine {min(cosines):.6f} < {CALIBRATION_THRESHOLD}; the embedding service no longer '
                      'reproduces the cached vectors, so new labels would land in another embedding space')
    vectors = _matrix(services.embed(list(texts), batch_size=min(100, len(texts)), max_workers=4, **options), len(texts), cache.dimensions)
    cache.add(list(zip(texts, vectors)))
    return {'probes': len(probes), 'minimum_cosine': min(cosines)}, dict(zip(texts, vectors))


def write_vectors(directory, kind, ids, shas, matrix):
    import numpy as np
    np.save(directory / f'{kind}.f32.npy', matrix, allow_pickle=False)
    return write_tsv(directory / f'{kind}.tsv', VECTOR_COLUMNS, ((str(row), identity, sha, hashlib.sha256(matrix[row].tobytes()).hexdigest())
                                                               for row, (identity, sha) in enumerate(zip(ids, shas))))


def build_vectors(services, directory, factors, cache_path):
    """vectors/<kind>.f32.npy (float32 LE) + <kind>.tsv of the factor labels and the DisMech contexts, from the vector
    cache; factor labels it lacks are embedded. Returns (embedding block, counts)."""
    cache = VectorCache(cache_path)
    try:
        dimensions = cache.dimensions
        texts = {factor['input_sha256']: factor['label'].strip() for factor in factors}
        found = cache.lookup(texts)
        missing = sorted(set(texts) - set(found))
        embedding = {key: cache.meta.get(key) for key in ('model', 'model_revision', 'provider')}
        embedding.update(dimensions=dimensions, cache_hits=len(found), embedded=len(missing), calibration=None)
        if missing:
            embedding['calibration'], fresh = embed_missing(services, cache, [texts[sha] for sha in missing])
            found.update({hashlib.sha256(text.encode()).hexdigest(): vector for text, vector in fresh.items()})
        counts = {'factors': write_vectors(directory, 'factors', [f['factor_key'] for f in factors], [f['input_sha256'] for f in factors],
                                           _matrix([found[f['input_sha256']] for f in factors], len(factors), dimensions))}
        contexts = cache.contexts()
        counts['contexts'] = write_vectors(directory, 'contexts', [sha for sha, _ in contexts], [sha for sha, _ in contexts],
                                           _matrix([vector for _, vector in contexts], len(contexts), dimensions))
    finally: cache.close()
    return embedding, counts


def dapper_runtime():
    """The pinned DAPPER runtime the catalog mints Mechanism ids with."""
    from .acceptance import public_runtime
    return public_runtime()


def _archived_row(snapshot, source_revision):
    """One archived_reference_factors row of a snapshot. Its archive_id digests the factor's source_revision, not the
    release, so a later release that leaves the factor unchanged adds no row: generation_id stays the first release."""
    return {'archive_id': rg.archive_id(source_revision, snapshot['source_id']), 'generation_id': snapshot['generation_id'],
            'source_id': snapshot['source_id'], 'source_id_sha256': hashlib.sha256(snapshot['source_id'].encode('utf-8')).hexdigest(),
            'model': snapshot['model'], 'factor_id': snapshot['factor_id'], 'trait': snapshot['trait'], 'kpn_trait_id': snapshot['kpn_trait_id'],
            'label': snapshot['label'], 'snapshot': snapshot, 'snapshot_sha256': digest(snapshot)}


def archived_rows(release_id, factors, traits, top_genes, projections_path, gene_sets, runtime):
    """One frozen snapshot per factor (reference_archive's format), generation_id = the release id: the top TOP_GENES
    genes by loading, and each library's top TOP_GENE_SETS gene sets by joint rank (from the factor_key-ordered projections)."""
    mint = (lambda node: runtime.compute_id(dict(node), 'Mechanism', runtime.schema)) if runtime else (lambda node: None)
    info = {row['gene_set_id']: row for row in gene_sets}
    groups = groupby(tsv_rows(projections_path, PROJECTION_COLUMNS), key=lambda row: row['factor_key'])
    pending = next(groups, None)
    for factor in factors:
        key, public, kpn = factor['factor_key'], factor['public_id'], factor['kpn_trait_id']
        if not pending or pending[0] != key: raise RuntimeError(f'projections.tsv.gz is not in factor order at {key}')
        rows = sorted((row for row in pending[1] if int(row['joint_rank']) <= TOP_GENE_SETS), key=lambda row: (row['library'], int(row['joint_rank'])))
        pending = next(groups, None)
        trait = traits[kpn]
        node = rg.mechanism_node(public, trait['phenotype_name'], kpn, factor['metadata']['factor'], factor['label'])
        yield _archived_row({
            'format': SNAPSHOT_FORMAT, 'generation_id': release_id, 'model': rg.KPN_MODEL, 'source_id': public, 'factor_id': factor['eaggl_factor_id'],
            'trait': trait['legacy_phenotype_id'], 'kpn_trait_id': kpn, 'label': factor['label'], 'mechanism': {'id': mint(node), **node},
            'metadata': factor['metadata'], 'top_genes': top_genes[key],
            'top_gene_sets': [{'rank': int(row['joint_rank']), 'gene_set_id': row['gene_set_id'], 'name': info[row['gene_set_id']]['gene_set_name'],
                               'library': row['library'], 'collection_id': info[row['gene_set_id']]['collection_id'],
                               'source_key': info[row['gene_set_id']]['legacy_source_key'], 'joint_loading': float(row['joint_loading']),
                               'marginal_loading': float(row['marginal_loading']), 'score': None} for row in rows],
            'generation_manifest_sha256': release_id}, factor['source_revision'])
    if pending: raise RuntimeError(f'projections.tsv.gz holds the unknown factor {pending[0]}')


def release_identity(files):
    """release_id: digest of {format, files} over DATA_FILES (the manifest's `files`; not the archive or the manifest)."""
    return digest({'format': RELEASE_FORMAT, 'files': files})


def gene_set_stats_files_in(directory):
    directory = Path(directory)
    return sorted(path for path in directory.rglob('*' + GENE_SET_STATS_SUFFIX)
                  if not any(part.startswith('.') for part in path.relative_to(directory).parts))


def write_trait_gene_sets(path, files, kpn_map, gene_set_ids):
    """trait_gene_sets.tsv.gz from the per-trait `<trait>.betas.tsv` files, in KPN trait order: one file per
    trait (no gene set PIGEAN analyzed is a header-only file), one response, known gene sets, library ranks 1..n.
    Returns (rows, the manifest block)."""
    by_kpn = {}
    for file in map(Path, files):
        if not file.name.endswith(GENE_SET_STATS_SUFFIX): raise Refused(f'{file.name}: not a <trait>{GENE_SET_STATS_SUFFIX} file')
        trait = file.name[:-len(GENE_SET_STATS_SUFFIX)]
        if trait not in kpn_map: raise Refused(f'{file.name}: unknown trait {trait}')
        if kpn_map[trait]['kpn_trait_id'] in by_kpn: raise Refused(f'{file.name}: repeated trait {trait}')
        by_kpn[kpn_map[trait]['kpn_trait_id']] = (trait, file)
    absent = sorted(set(kpn_map) - {trait for trait, _ in by_kpn.values()})
    if absent: raise Refused(f'No gene-set stats for {len(absent)} traits, e.g. {absent[:5]}')
    responses, counts = set(), Counter()
    def rows():
        for kpn in sorted(by_kpn):
            trait, file = by_kpn[kpn]
            seen, ranks = set(), defaultdict(list)
            for row in tsv_rows(file, LAP_GENE_SET_STATS_COLUMNS):
                if (row['trait'], row['kpn_trait_id']) != (trait, kpn): raise Refused(f'{file.name}: a row of another trait')
                if row['gene_set_id'] not in gene_set_ids or row['gene_set_id'] in seen:
                    raise Refused(f"{file.name}: unknown or repeated gene set {row['gene_set_id']}")
                seen.add(row['gene_set_id']); ranks[row['library']].append(int(row['library_rank'])); responses.add(row['response'])
                if len(responses) > 1: raise Refused(f'The gene-set stats mix the responses {sorted(responses)}')
                counts[kpn] += 1
                yield tuple(row[column] for column in TRAIT_GENE_SET_COLUMNS)
            if any(values != list(range(1, len(values) + 1)) for values in ranks.values()):
                raise Refused(f'{file.name}: library ranks are not 1..n in rank order')
    total = write_tsv(path, TRAIT_GENE_SET_COLUMNS, rows())
    return total, {'response': next(iter(responses), None), 'traits': len(by_kpn), 'traits_with_rows': len(counts),
                   'source': 'pigean betas mode (no outer Gibbs) on each trait\'s PIGEAN gene stats, one fit per CFDE library; '
                             'LAP betas_ stage'}


def long_files_in(directory):
    directory = Path(directory)
    return sorted(path for path in directory.rglob('*' + LONG_SUFFIX) if not any(part.startswith('.') for part in path.relative_to(directory).parts))


def _replaceable(path):
    """A directory --out may replace: empty (dot entries aside) or an earlier release folder."""
    if not path.is_dir(): return False
    if all(entry.name.startswith('.') for entry in path.iterdir()): return True
    try: manifest = read_json(path / 'manifest.json')
    except (OSError, ValueError): return False
    return isinstance(manifest, dict) and manifest.get('format') == RELEASE_FORMAT


def build_release(services, project_dir, long_files, cache_path, out, *, gene_set_stats_files=(), kpn_release=None,
                  top_n=DEFAULT_TOP_N, workers=1, runtime=None):
    """Write the release folder (module docstring) and swap it into `out`; reuse an identical release already there."""
    project_dir, out = Path(project_dir), Path(out)
    if top_n < 1 or workers < 1: raise Refused('--top-n and --workers must be positive')
    if out.exists() and not _replaceable(out): raise Refused(f'{out} exists and is not a release folder: refusing to replace it')
    stem, paths = lap_inputs(project_dir)
    lap = lap_tables(paths, kpn_release)
    by_kpn = {trait['kpn_trait_id']: trait for trait in lap['traits']}
    files, kpn_of = sorted(set(map(Path, long_files))), {}
    if not files: raise Refused('Pass the per-trait long files (--long-file or --long-files-from)')
    if not gene_set_stats_files: raise Refused('Pass the per-trait gene-set stats (--gene-set-stats-file or --gene-set-stats-from)')
    for path in files:
        trait = _long_file_trait(path)
        if trait not in lap['kpn_map'] or lap['kpn_map'][trait]['kpn_trait_id'] in kpn_of.values(): raise Refused(f'{path}: unknown or repeated trait {trait}')
        kpn_of[path] = lap['kpn_map'][trait]['kpn_trait_id']
    absent = sorted(set(lap['kpn_map']) - {by_kpn[kpn]['legacy_phenotype_id'] for kpn in kpn_of.values()})
    if absent: raise Refused(f'No long file for {len(absent)} traits, e.g. {absent[:5]}')
    documents = {collection_id: project_dir / 'collections' / row['label'] / f"{row['label']}.GeneSetCollection.yaml"
                 for collection_id, row in lap['index'].items()}
    missing = sorted(str(path) for path in documents.values() if not path.is_file())
    if missing: raise Refused(f'Missing {len(missing)} collection documents, e.g. {missing[:3]}')
    if runtime is None:
        # Required: a factor revision's frozen snapshot is stored once, so a missing Mechanism id could never be added later.
        try: runtime = dapper_runtime()
        except Exception as error: raise Refused(f'The DAPPER runtime that mints Mechanism ids is unavailable ({type(error).__name__}: {error})') from error
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary, started = Path(tempfile.mkdtemp(prefix=f'.{out.name}.build-', dir=out.parent)), time.monotonic()
    def progress(message): emit(services, f'{stem}: {message} ({time.monotonic() - started:.0f}s)')
    try:
        (temporary / 'vectors').mkdir()
        progress('factor gene loadings')
        counts = {}
        counts['factor_genes'], sums, top_genes = write_factor_genes(paths['factors_by_genes'], temporary / 'factor_genes.tsv.gz', lap['keys'], lap['index_rows'])
        factors = lap['factors']
        for factor in factors:
            factor['source_revision'] = digest({'label': factor['label'], 'kpn_trait_id': factor['kpn_trait_id'],
                                                'factor': factor['metadata']['factor'], 'loadings_sha256': sums[factor['factor_key']]})
        progress('vectors')
        embedding, counts['vectors'] = build_vectors(services, temporary / 'vectors', factors, cache_path)
        counts['traits'] = write_jsonl(temporary / 'traits.jsonl.gz', lap['traits'])
        counts['factors'] = write_jsonl(temporary / 'factors.jsonl.gz', ({column: factor[column] for column in COLUMNS['factors']} for factor in factors))
        progress(f'{len(files)} long files on {workers} workers')
        libraries = {row['gene_set_id']: row['library'] for row in lap['gene_sets']}
        with _tasks(workers) as submit:  # long files in factor_key order
            counts['projections'] = write_projections(temporary / 'projections.tsv.gz', [
                (kpn_of[path], submit(long_file_ranks, path, top_n)) for path in sorted(files, key=lambda path: kpn_of[path])],
                lap['keys'], lap['index_rows'], libraries)
        progress(f'{len(gene_set_stats_files)} trait gene-set stats files')
        counts['trait_gene_sets'], gene_set_stats = write_trait_gene_sets(temporary / 'trait_gene_sets.tsv.gz', gene_set_stats_files,
                                                                          lap['kpn_map'], set(libraries))
        progress(f'{len(documents)} collection documents, gene sets and DAPPER provenance on {workers} workers')
        with _tasks(workers) as submit:
            counts['collections'], counts['gene_sets'], nodes, edges, variants = write_collections(temporary, lap, documents, submit, workers + 1)
        counts['dapper_nodes'] = write_jsonl(temporary / 'dapper_nodes.jsonl.gz', nodes)
        counts['dapper_edges'] = write_tsv(temporary / 'dapper_edges.tsv.gz', EDGE_COLUMNS, edges)
        files_sha = {name: _sha256_file(temporary / name) for name in DATA_FILES}
        release_id = release_identity(files_sha)
        progress(f'release {release_id[:12]}; archived factor snapshots')
        counts['archived_factors'] = write_jsonl(temporary / ARCHIVE_FILE, archived_rows(
            release_id, factors, by_kpn, top_genes, temporary / 'projections.tsv.gz', lap['gene_sets'], runtime))
        manifest = {'format': RELEASE_FORMAT, 'release_id': release_id, 'built_at': now(), 'files': files_sha,
                    'counts': counts, 'top_n': top_n, 'embedding': embedding,
                    'archive': {'file': ARCHIVE_FILE, 'sha256': _sha256_file(temporary / ARCHIVE_FILE), 'top_genes': TOP_GENES,
                                'top_gene_sets_per_library': TOP_GENE_SETS, 'mechanism_ids': bool(runtime)},
                    'dapper': {'nodes': len(nodes), 'edges': len(edges), 'variants_skipped': variants}, 'gene_set_stats': gene_set_stats,
                    'sources': {'lap_project': str(project_dir.resolve()), 'lap_stem': stem, 'pigean_commit': lap['pigean_commit'],
                                'kpn_release': lap['kpn_release'], 'kpn_release_commit': lap['kpn_release_commit'],
                                'cfde_snapshot': sorted({row['metadata']['cfde_snapshot'] for row in lap['gene_sets']}),
                                'long_files': len(files), 'vector_cache': str(Path(cache_path).resolve())}}
        write_json(temporary / 'manifest.json', manifest)
        progress('written')
        result = {'release_id': release_id, 'out': str(out), 'counts': counts, 'embedding': embedding}
        existing = out / 'manifest.json'
        if existing.is_file() and read_json(existing).get('release_id') == release_id: return {**result, 'reused': True}
        aside = out.with_name(f'.{out.name}.old-{uuid.uuid4().hex[:12]}') if out.exists() else None
        if aside: out.rename(aside)
        try: temporary.rename(out)
        except BaseException:
            if aside and not out.exists(): aside.rename(out)
            raise
        out.chmod(0o775)
        if aside: shutil.rmtree(aside, ignore_errors=True)
        return {**result, 'reused': False}
    finally:
        if temporary.exists(): shutil.rmtree(temporary, ignore_errors=True)


# --------------------------------------------------------------------------------------
# publish: per environment, under its named lock


def query(connection, sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def scalar(connection, sql, params=()):
    rows = query(connection, sql, params)
    return rows[0][0] if rows else None


def execute(connection, sql, params=()):
    with connection.cursor() as cursor: cursor.execute(sql, params)


def existing_tables(connection, names):
    names = list(names)
    return {row[0] for row in query(connection, 'SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND '
                                                f"TABLE_NAME IN ({','.join(['%s'] * len(names))})", tuple(names))}


def table_name(prefix, table, suffix=''): return application_sql(f'reveal_ref_{table}{suffix}', prefix)


def schema_statements(prefix, suffix=''):
    return [application_sql(statement.strip(), prefix) for statement in SCHEMA.format(suffix=suffix).split(';') if statement.strip()]


def _timestamp(value):
    """ISO-8601 UTC with microseconds ('...Z') of a datetime (naive = UTC) or a MySQL DATETIME string."""
    if isinstance(value, str): value = datetime.fromisoformat(value.rstrip('Z'))
    if value.tzinfo: value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec='microseconds') + 'Z'


def insert_rows(connection, table, columns, rows, batch_size, *, ignore=False, max_bytes=INSERT_BYTES):
    """Batched INSERT (as mysql_database.insert_batch), committed per batch; any MySQL
    warning refuses, except a duplicate key under INSERT IGNORE. Batches stay below pymysql's 1 MB statement
    split, so SHOW WARNINGS covers the whole batch."""
    sql = f"INSERT {'IGNORE ' if ignore else ''}INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})"
    batch, size, count = [], 0, 0
    def flush():
        with connection.cursor() as cursor:
            cursor.executemany(sql, batch)
            cursor.execute('SHOW WARNINGS')
            warnings = [row for row in cursor.fetchall() if not (ignore and int(row[1]) == DUPLICATE_KEY)]
        if warnings: raise RuntimeError(f'MySQL warning inserting {table}: {warnings[0][2]}')
        connection.commit()
    for row in rows:
        row_size = sum(len(value) if isinstance(value, str) else 8 for value in row)
        if batch and (len(batch) >= batch_size or size + row_size > max_bytes): flush(); batch, size = [], 0
        batch.append(row); size += row_size; count += 1
    if batch: flush()
    return count


def open_release(release):
    release = Path(release)
    if not (release / 'manifest.json').is_file(): raise Refused(f'{release}: no manifest.json')
    manifest = read_json(release / 'manifest.json')
    if manifest.get('format') != RELEASE_FORMAT or set(manifest.get('files') or {}) != set(DATA_FILES) \
            or release_identity(manifest['files']) != manifest.get('release_id'):
        raise Refused(f'{release}: not a {RELEASE_FORMAT} folder')
    expected = {**manifest['files'], ARCHIVE_FILE: (manifest.get('archive') or {}).get('sha256')}
    for name, sha in expected.items():
        path = release / name
        if not path.is_file() or _sha256_file(path) != sha:
            raise Refused(f'{path} does not match the manifest: a partial copy, or a rebuild in progress?')
    return manifest


def watch_release(release):
    """A check that refuses once the folder's files were replaced (a rebuild swaps the folder). The publisher re-reads them at
    each step, so it checks before each swap that every table came from the folder it verified."""
    names = (*DATA_FILES, ARCHIVE_FILE, 'manifest.json')
    def stats():
        try: return [(lambda stat: (stat.st_ino, stat.st_size, stat.st_mtime_ns))((Path(release) / name).stat()) for name in names]
        except OSError: return None
    before = stats()
    def unchanged():
        if before is None or stats() != before: raise Refused(f'{release} changed during the publish (a rebuild?): publish it again')
    return unchanged


def table_rows(release, manifest, published_at):
    """(table, rows(), batch size) of every reference table (COLUMNS order), ref_release last."""
    r = Path(release)
    def jsonl(name, table):
        columns = COLUMNS[table]
        return lambda: (tuple(canonical(row[c]) if c in JSON_COLUMNS else row[c] for c in columns) for row in read_jsonl(r / name))
    def projections():
        for row in tsv_rows(r / 'projections.tsv.gz', PROJECTION_COLUMNS):
            yield (row['factor_key'], row['gene_set_id'], row['library'], float(row['joint_loading']), float(row['marginal_loading']),
                   row['joint_loading'], row['marginal_loading'], int(row['joint_rank']), int(row['marginal_rank']), int(row['is_joint_top_factor']))
    stamp = published_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
    return [('traits', jsonl('traits.jsonl.gz', 'traits'), 1000), ('factors', jsonl('factors.jsonl.gz', 'factors'), 1000),
            ('factor_genes', lambda: ((row['factor_key'], row['gene'], float(row['loading']))
                                      for row in tsv_rows(r / 'factor_genes.tsv.gz', FACTOR_GENE_COLUMNS)), 10000),
            ('collections', jsonl('collections.jsonl.gz', 'collections'), 20), ('gene_sets', jsonl('gene_sets.jsonl.gz', 'gene_sets'), 500),
            ('projections', projections, 5000), ('dapper_nodes', jsonl('dapper_nodes.jsonl.gz', 'dapper_nodes'), 500),
            ('dapper_edges', lambda: ((row['subject'], row['predicate'], row['object'], row['edge_role'] or None)
                                      for row in tsv_rows(r / 'dapper_edges.tsv.gz', EDGE_COLUMNS)), 5000),
            ('trait_gene_sets', lambda: ((row['kpn_trait_id'], row['gene_set_id'], row['library'], float(row['beta_uncorrected']),
                                          float(row['beta']), float(row['avg_postp']), int(row['library_rank']))
                                         for row in tsv_rows(r / 'trait_gene_sets.tsv.gz', TRAIT_GENE_SET_COLUMNS)), 5000),
            ('release', lambda: [(manifest['release_id'], stamp, canonical(manifest))], 1)]


def current_release(connection, prefix):
    """{release_id, published_at} of the environment's ref_release row, or None."""
    table = table_name(prefix, 'release')
    if table not in existing_tables(connection, [table]): return None
    rows = query(connection, f'SELECT release_id,published_at FROM {table}')
    return {'release_id': rows[0][0], 'published_at': _timestamp(rows[0][1])} if len(rows) == 1 else None


def swap_ddl(services, connection, sql):
    """DDL on live or just-swapped tables, retried while a long reader holds their metadata locks (SWAP_LOCK_WAIT)."""
    for attempt in range(1, SWAP_ATTEMPTS + 1):
        try: return execute(connection, sql)
        except Exception as error:
            if not (error.args and error.args[0] == LOCK_WAIT_TIMEOUT) or attempt == SWAP_ATTEMPTS: raise
            emit(services, f'{sql.split()[0]} waits for a long reader of the live tables ({attempt}/{SWAP_ATTEMPTS})')
            services.sleep(SWAP_LOCK_WAIT)


def replace_tables(services, connection, release, manifest, prefix, unchanged_folder=lambda: None):
    """Fill <prefix>_ref_*__new, then swap every table (ref_release included) in one RENAME and drop the old ones.
    `unchanged_folder()` refuses before the RENAME when the release folder was replaced while its tables loaded."""
    new, old = [table_name(prefix, t, '__new') for t in TABLES], [table_name(prefix, t, '__old') for t in TABLES]
    swap_ddl(services, connection, 'DROP TABLE IF EXISTS ' + ', '.join(new + old))  # leftovers of a run that died
    current = current_release(connection, prefix)
    if current and current['release_id'] == manifest['release_id']: return {'action': 'unchanged', 'published_at': current['published_at']}
    for statement in schema_statements(prefix, '__new'): execute(connection, statement)
    published, rows = services.clock(), {}
    for table, rows_of, batch_size in table_rows(release, manifest, published):
        emit(services, f'{prefix}: loading {table}')
        rows[table] = insert_rows(connection, table_name(prefix, table, '__new'), COLUMNS[table], rows_of(), batch_size)
    unchanged_folder()
    present = existing_tables(connection, [table_name(prefix, t) for t in TABLES])
    pairs = []
    for table in TABLES:
        final = table_name(prefix, table)
        if final in present: pairs.append(f'{final} TO {table_name(prefix, table, "__old")}')
        pairs.append(f'{table_name(prefix, table, "__new")} TO {final}')
    swap_ddl(services, connection, 'RENAME TABLE ' + ', '.join(pairs))
    swap_ddl(services, connection, 'DROP TABLE IF EXISTS ' + ', '.join(old))
    return {'action': 'replaced', 'rows': rows, 'published_at': _timestamp(published)}


def insert_archived(connection, release):
    """Add the release's frozen factor snapshots that the shared archived_reference_factors lacks (INSERT IGNORE).
    Rows are keyed by factor revision, so only new or changed factors are added."""
    execute(connection, ARCHIVED_TABLE)
    total = inserted = 0
    rows = read_jsonl(Path(release) / ARCHIVE_FILE)
    while chunk := list(islice(rows, 1000)):
        total += len(chunk)
        ids = [row['archive_id'] for row in chunk]
        stored = {row[0] for row in query(connection, f"SELECT archive_id FROM archived_reference_factors WHERE archive_id IN ({','.join(['%s'] * len(ids))})", ids)}
        inserted += insert_rows(connection, 'archived_reference_factors', ARCHIVED_COLUMNS, (tuple(canonical(row[c]) if c == 'snapshot' else row[c]
                                for c in ARCHIVED_COLUMNS) for row in chunk if row['archive_id'] not in stored), 100, ignore=True)
    return {'rows': total, 'inserted': inserted}


def _value(row, name, default=None): return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def release_vectors(release, kind):
    """[(id, float32 vector, Upstash metadata)] of one corpus kind of a release."""
    import numpy as np
    r = Path(release)
    if kind == 'factors':
        extra = {row['factor_key']: {key: row[key] for key in ('public_id', 'kpn_trait_id', 'label')} for row in read_jsonl(r / 'factors.jsonl.gz')}
    else: extra = None
    matrix = np.load(r / f'vectors/{kind}.f32.npy', mmap_mode='r', allow_pickle=False)
    rows = list(tsv_rows(r / f'vectors/{kind}.tsv', VECTOR_COLUMNS))
    if matrix.ndim != 2 or matrix.shape[0] != len(rows): raise Refused(f'vectors/{kind}: matrix and rows differ')
    items = []
    for position, row in enumerate(rows):
        vector = np.asarray(matrix[position], dtype='<f4')
        sha = hashlib.sha256(vector.tobytes()).hexdigest()
        if int(row['row']) != position or sha != row['vector_sha256'] or (extra is not None and row['id'] not in extra):
            raise Refused(f"vectors/{kind}.tsv row {position} ({row['id']}) does not match the release")
        items.append((row['id'], vector, {'kind': VECTOR_KINDS[kind], **((extra or {}).get(row['id']) or {}),
                                          'input_sha256': row['input_sha256'], 'vector_sha256': sha}))
    return items


def list_vectors(client, namespace):
    """{id: metadata} of every vector in a namespace (range pages)."""
    found, cursor = {}, ''
    while True:
        page = client.range(cursor=cursor, limit=RANGE_PAGE, include_metadata=True, namespace=namespace)
        vectors = _value(page, 'vectors', []) or []
        for row in vectors: found[_value(row, 'id')] = _value(row, 'metadata')
        following = _value(page, 'next_cursor', '')
        if not following or following == '0': return found
        if following == cursor or not vectors: raise RuntimeError(f'Upstash range of {namespace} did not advance')
        cursor = following


def upsert_vectors(client, namespace, items):
    def upsert(batch):
        client.upsert(vectors=[{'id': identity, 'vector': vector.tolist(), 'metadata': metadata} for identity, vector, metadata in batch],
                      namespace=namespace)
    with ThreadPoolExecutor(max_workers=UPSERT_WORKERS) as pool:
        for _ in pool.map(upsert, list(_chunks(items, UPSERT_BATCH))): pass
    return len(items)


def add_vectors(services, client, release, env):
    """Before the swap: upsert the vectors each namespace lacks (all of a first publish). The served tables don't name these
    ids, so the app is unaffected. Plans the rest: `changed` (metadata, vector_sha256 included, differs) and `stale` ids."""
    plans, namespaces = {}, set(client.list_namespaces())
    for kind in KINDS:
        namespace = f"{env}-{kind.replace('_', '-')}"
        items, existing = release_vectors(release, kind), list_vectors(client, namespace) if namespace in namespaces else {}
        plan = plans[kind] = {'namespace': namespace, 'vectors': len(items), 'existing': len(existing),
                              'changed': [item for item in items if item[0] in existing and existing[item[0]] != item[2]],
                              'stale': sorted(set(existing) - {item[0] for item in items})}
        plan['added'] = upsert_vectors(client, namespace, [item for item in items if item[0] not in existing])
        emit(services, f"{namespace}: {plan['added']} of {len(items)} vectors added; {len(plan['changed'])} changed, {len(plan['stale'])} stale")
    return plans


def update_vectors(client, plans):
    """Once the environment's tables hold the release: overwrite the changed vectors, then delete the stale ones."""
    for plan in plans.values():
        plan['updated'] = upsert_vectors(client, plan['namespace'], plan['changed'])
        plan['deleted'] = 0
        for batch in _chunks(plan['stale'], DELETE_BATCH):
            plan['deleted'] += int(_value(client.delete(ids=batch, namespace=plan['namespace']), 'deleted', 0) or 0)


@contextmanager
def _environ(**values):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try: yield
    finally:
        for key, value in saved.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value


def write_record(repository, current, release_id, notification_namespace):
    """<prefix>_records reference_release/current: the release the environment's tables now hold (rewritten only on change).
    Its catalog.updated event goes to the environment's own channels, not this process's REVEAL_NOTIFICATION_NAMESPACE."""
    if not current or current['release_id'] != release_id: return {'written': False, 'reason': 'the tables do not hold this release'}
    data = {'release_id': current['release_id'], 'published_at': current['published_at']}
    with _environ(REVEAL_NOTIFICATION_NAMESPACE=notification_namespace), repository.transaction() as tx:
        old = tx.get(RECORD_KIND, RECORD_ID)
        if old and old['data'] == data: return {**data, 'written': False}
        tx.put(RECORD_KIND, RECORD_ID, rg.CATALOG_OWNER, data)
    return {**data, 'written': True}


def publish_environment(services, release, manifest, env, *, vectors=True, tables=True, unchanged_folder=lambda: None):
    """One environment (module docstring): add the missing vectors; then, under the environment's lock, store new frozen
    snapshots and swap the tables; once its tables hold the release, update changed vectors, delete stale ones, write the record."""
    prefix, release_id, lock = ENVIRONMENTS[env], manifest['release_id'], f'reveal:publish:{ENVIRONMENTS[env]}'
    seconds, started = {}, time.monotonic()
    result = {'env': env, 'prefix': prefix, 'release_id': release_id, 'seconds': seconds}
    def step(name, function):
        began = time.monotonic()
        try: return function()
        finally: seconds[name] = round(time.monotonic() - began, 3)
    client = services.vector_client(env) if vectors else None
    # Before any MySQL session: a first publish uploads every vector, and an idle locked session could be dropped meanwhile.
    plans = step('vectors', lambda: add_vectors(services, client, release, env)) if vectors else None
    connection, locked = services.connect(), False
    try:
        with connection.cursor() as cursor:  # executemany may split statements: strict mode fails every one
            cursor.execute(STRICT_MODE); cursor.execute("SET time_zone = '+00:00'")
            cursor.execute(f'SET SESSION lock_wait_timeout = {SWAP_LOCK_WAIT}')
        if scalar(connection, 'SELECT GET_LOCK(%s,%s)', (lock, 0)) != 1: raise Refused(f'Another publish to {env} holds the lock {lock}')
        locked = True
        if tables:
            # Snapshots first: immutable rows keyed by factor revision, so a run that dies at any later step never misses them.
            result['archived_factors'] = step('archived_factors', lambda: insert_archived(connection, release))
            result['tables'] = step('tables', lambda: replace_tables(services, connection, release, manifest, prefix, unchanged_folder))
        else: result['tables'] = result['archived_factors'] = 'skipped'
        current = current_release(connection, prefix)
        if plans is not None and current and current['release_id'] == release_id: step('update', lambda: update_vectors(client, plans))
        # updated and deleted stay None until the environment's tables hold the release.
        result['vectors'] = 'skipped' if plans is None else {kind: {
            'namespace': plan['namespace'], 'vectors': plan['vectors'], 'existing': plan['existing'], 'added': plan['added'],
            'changed': len(plan['changed']), 'updated': plan.get('updated'), 'stale': len(plan['stale']), 'deleted': plan.get('deleted')}
            for kind, plan in plans.items()}
        result['record'] = step('record', lambda: write_record(services.repository(prefix), current, release_id, NOTIFICATION_NAMESPACES[env]))
    finally:
        try:
            if locked: scalar(connection, 'SELECT RELEASE_LOCK(%s)', (lock,))
        finally: connection.close()
    seconds['total'] = round(time.monotonic() - started, 3)
    emit(services, json.dumps(result, sort_keys=True, default=str))
    return result


def publish_release(services, release, environments, *, vectors=True, tables=True, results=None):
    """publish_environment for each environment in order; `results` collects the finished ones (also on failure)."""
    unchanged_folder = watch_release(release)
    manifest, results = open_release(release), [] if results is None else results
    unchanged_folder()  # not replaced while its files were verified
    for env in dict.fromkeys(environments):
        if env not in ENVIRONMENTS: raise Refused(f'Unknown environment {env!r}; expected one of {sorted(ENVIRONMENTS)}')
        results.append(publish_environment(services, Path(release), manifest, env, vectors=vectors, tables=tables,
                                           unchanged_folder=unchanged_folder))
    return {'release_id': manifest['release_id'], 'release': str(release), 'environments': results}


# --------------------------------------------------------------------------------------
# CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message): raise Refused(message)


def parser():
    p = _Parser(prog='python -m reveal_backend.reference_release', description=__doc__.split('\n\n')[0])
    commands = p.add_subparsers(dest='command', required=True, parser_class=_Parser)
    c = commands.add_parser('build', help='Write one reference release folder from LAP outputs (pure files; embeds only cache misses)')
    c.add_argument('--lap-project-dir', type=Path, required=True)
    c.add_argument('--long-file', type=Path, action='extend', nargs='+', default=[], help='A per-trait <trait>.projection.tsv (repeatable)')
    c.add_argument('--long-files-from', type=Path, help='Directory searched recursively for *.projection.tsv')
    c.add_argument('--gene-set-stats-file', type=Path, action='extend', nargs='+', default=[],
                   help=f'A per-trait <trait>{GENE_SET_STATS_SUFFIX} of the LAP betas_ stage (repeatable)')
    c.add_argument('--gene-set-stats-from', type=Path, help=f'Directory searched recursively for *{GENE_SET_STATS_SUFFIX}')
    c.add_argument('--vector-cache', type=Path, required=True, help='SQLite vector cache of factor labels and contexts')
    c.add_argument('--out', type=Path, required=True, help='Release folder (replaced unless it already holds this release)')
    c.add_argument('--kpn-release')
    c.add_argument('--top-n', type=int, default=DEFAULT_TOP_N, help='Keep projections ranked <= N within their library (joint or marginal)')
    c.add_argument('--workers', type=int, default=min(8, os.cpu_count() or 1))
    c = commands.add_parser('publish', help="Replace each environment's reference tables and sync its Upstash vectors")
    c.add_argument('--release', type=Path, required=True)
    c.add_argument('--env', action='append', required=True, choices=sorted(ENVIRONMENTS), help='Repeatable; published in the given order')
    c.add_argument('--skip-vectors', action='store_true', help='Leave Upstash untouched')
    c.add_argument('--skip-tables', action='store_true', help='Leave the tables and archived snapshots untouched')
    return p


def run(services, args, result):
    if args.command == 'build':
        files = list(args.long_file) + (long_files_in(args.long_files_from) if args.long_files_from else [])
        stats = list(args.gene_set_stats_file) + (gene_set_stats_files_in(args.gene_set_stats_from) if args.gene_set_stats_from else [])
        return build_release(services, args.lap_project_dir, files, args.vector_cache, args.out,
                             gene_set_stats_files=sorted(set(stats)), kpn_release=args.kpn_release, top_n=args.top_n, workers=args.workers)
    return publish_release(services, args.release, args.env, vectors=not args.skip_vectors, tables=not args.skip_tables,
                           results=result.setdefault('environments', []))


def main(argv=None, services=None):
    """One JSON object on stdout; exit 0 on success, 2 when refused, 1 on any other failure."""
    result, code = {'command': None}, 2
    try:
        args = parser().parse_args(argv)
        result['command'] = args.command
        if services is None:
            from dotenv import load_dotenv
            load_dotenv(ROOT / '.env', override=False, interpolate=False)
            services = Services()
        result.update(run(services, args, result), ok=True); code = 0
    except Refused as error: result.update(ok=False, refused=str(error))
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        result.update(ok=False, error=f'{type(error).__name__}: {error}'); code = 1
    print(json.dumps(result, sort_keys=True, default=str, ensure_ascii=False), flush=True)
    return code


if __name__ == '__main__': raise SystemExit(main())
