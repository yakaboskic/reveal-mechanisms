"""Protected reference reload: build, load, snapshot and cut over reference generations.

`python -m reveal_backend.reference_reload <subcommand>` (docs/reference-reload.md §7):

  preflight                                      read-only: server, grants, lock, schema, served
                                                 sources (and a bundle); --compare a baseline
  build → embed → load → capture                 additive, shared scientific tables
  abandon                                        remove a KPN generation that never served
                                                 (and, --drop-empty-schema, the empty 008 tables)
  snapshot → plan → approve → apply → verify     per allow-listed target (a plan for a target whose
                                                 cutover failed after activation resumes it)
  purge-retired                                  after every target verified
  gate, status                                   reopen a kept gate; report

Every subcommand prints one JSON result on stdout (progress goes to stderr). Nothing
destructive runs without --apply; `abandon --apply` also needs the typed generation id, and
`apply` and `purge-retired` a plan, a typed approval of its sha and a target from
config/reference_reload.targets.yaml whose MySQL database, application prefix, vector
environment and Upstash host match this shell.
Secrets are read only from the environment (the repository .env via python-dotenv) and
are never printed.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime
from decimal import Decimal
import gzip
import hashlib
import importlib
import io
import itertools
import json
import logging
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

from . import reference_generation as rg
from .repository import Repository, application_prefix, canonical, digest, now
from .runtime_config import ROOT

BUNDLE_FORMAT = 'reveal.reference-bundle/1'
VECTORS_FORMAT = 'reveal.reference-vectors/1'
PLAN_FORMAT = 'reveal.reference-reload-plan/1'
PURGE_FORMAT = 'reveal.reference-purge-plan/1'
APPROVAL_FORMAT = 'reveal.reference-reload-approval/1'
# 2: exports also hold every row purge-retired deletes for the generation: the Activity and unaliased
# GeneSet dapper_objects rows, every embedding run of its EAGGL import and their DisMech context runs.
EXPORT_FORMAT = 'reveal.reference-cold-export/2'
BUNDLE_SCHEMA_VERSION = 2  # 2: cfde_gene_sets.metadata.dapper_gene_set holds the exact CFDE GeneSet node
DEFAULT_LAP_PROJECT = ROOT / 'lap/out/projects/eaggl_capped__cfde_2026_09_28'
DEFAULT_EMBEDDINGS = Path('/humgen/diabetes/users/chase/data/dig-s3/gene_sets/cfde/2026-09-28/embeddings')
DEFAULT_DAPPER = ROOT / 'data/dapper/2026-09-24-v8'
DEFAULT_EAGGL_SOURCE_VERSION = 'legacy-711-trait-capped-union'
TARGETS_FILE = ROOT / 'config/reference_reload.targets.yaml'
LOCK_NAME = 'reveal:reference-reload'
PRODUCTION_APPROVAL = 'REVEAL_RELOAD_PRODUCTION_APPROVAL'
PURGE_NAME = 'purge-retired'
CALIBRATION_THRESHOLD = 0.999
DELETE_BATCH = 10000
EXPORT_PAGE_ROWS = 5000
EXPORT_PART_BYTES = 32 << 20  # uncompressed; parts stay far below the artifact size limit
ENVIRONMENT_RE = re.compile(r'[a-z][a-z0-9_-]{0,24}')
HOST_RE = re.compile(r'[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?')
# Volatile fields that never count as plan drift; `observed` holds informational counts.
TIMESTAMP_KEYS = frozenset({'planned_at', 'changed_at', 'activated_at', 'updated_at', 'created_at', 'verified_at', 'approved_at'})
BOOKKEEPING_KINDS = frozenset({rg.CONTROL_KIND, rg.RELOAD_KIND, rg.ARCHIVE_RUN_KIND})
# Where each archived kind carries its stamp (docs §4.2).
STAMP_PATHS = {'account': ('summary',), 'account_membership': ('summary',), 'publication': ('summary',),
               'publication_snapshot': ('summary',), 'analysis_outcome': ('record',), 'outcome_snapshot': ('record',),
               'outcome_summary': (), 'outcome_publication': ('summary',), 'request': ()}
COUNT_TABLES = (('kpn_traits', 'kpn_traits'), ('reference_factors', 'reference_factors'),
                ('cfde_collections', 'cfde_gene_set_collections'), ('cfde_gene_sets', 'cfde_gene_sets'),
                ('projections', 'factor_gene_set_projections'), ('reference_vectors', 'reference_vectors'))
GENERATION_TABLES = ('reference_vectors', 'factor_gene_set_projections', 'cfde_gene_sets', 'cfde_gene_set_collections',
                     'reference_factors', 'kpn_traits')  # child first
# The only tables purge-retired may delete from; DisMech sources and frozen snapshots never.
PURGE_TABLES = frozenset({
    'eaggl_cfde_gene_set_links', 'eaggl_cfde_factor_links', 'eaggl_cfde_link_runs',
    'dismech_embedding_inputs', 'dismech_embedding_vectors', 'dismech_embedding_runs',
    'eaggl_name_embeddings', 'eaggl_embedding_runs', 'eaggl_graph_edges', 'eaggl_graph_nodes', 'eaggl_gene_loadings',
    'eaggl_factors', 'eaggl_genes', 'eaggl_imports', 'cfde_gene_set_aliases', 'gene_set_imports', 'dapper_objects',
    *GENERATION_TABLES, 'vector_bindings'})


def _dismech_source_tables():
    path = ROOT / 'schema/migrations/003_dismech.sql'
    names = set(re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', path.read_text())) if path.exists() else set()
    return frozenset(names | {'dismech_imports', 'dismech_documents', 'dismech_mechanisms', 'dismech_causal_edges',
                              'dismech_hypotheses', 'dismech_ontology_terms', 'dismech_vocabulary',
                              'dismech_discussions', 'dismech_gap_attachments'})


PROTECTED_TABLES = _dismech_source_tables() | {'archived_reference_factors', 'reference_generations', 'embedding_spaces'}


class Refused(RuntimeError):
    """A protection refused the command; nothing further was changed."""


# --------------------------------------------------------------------------------------
# Side effects (tests replace these)


def _prompt(message):
    """Interactive prompt on stderr; stdout carries only the JSON result."""
    print(message, end='', file=sys.stderr, flush=True)
    return sys.stdin.readline()


class Services:
    """Connections, clients, clock and terminal. The CLI uses these defaults."""
    def __init__(self, environ=None, shell=None, targets_file=TARGETS_FILE):
        self.environ = os.environ if environ is None else environ
        self.shell = dict(self.environ if shell is None else shell)
        self.targets_file = Path(targets_file)
        self.input, self.sleep, self.clock, self.run, self.err = _prompt, time.sleep, now, subprocess.run, sys.stderr
    def interactive(self): return sys.stdin.isatty()
    def module(self, name): return importlib.import_module(f'{__package__}.{name}')
    def connect(self):
        from .runtime_config import mysql_connection
        return mysql_connection()
    def repository(self, prefix): return Repository(table_prefix=prefix)
    def vector_client(self, *, write=False):
        from .vector_retrieval import client_from_environment
        return client_from_environment(write=write)
    def embed(self, texts, **options):
        from .embedding_client import get_embeddings
        return get_embeddings(texts, **options)
    def rds(self):
        import boto3
        return boto3.client('rds', region_name=self.environ.get('AWS_REGION', 'us-east-1'))
    def dapper_runtime(self, path):
        from .evidence_package import DapperRuntime
        return DapperRuntime(path)
    def put_artifact(self, data, directory):
        """Content-addressed immutable object: S3 when configured, else a local file."""
        from . import artifact_store
        if artifact_store.s3_enabled(): return artifact_store.store().put(data, 'application/gzip')
        sha = hashlib.sha256(data).hexdigest()
        path = Path(directory) / sha[:2] / (sha + '.jsonl.gz')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != sha: raise Refused('Immutable export checksum conflict')
        path.write_bytes(data)
        return {'store': 'filesystem', 'path': str(path.resolve()), 'sha256': sha, 'size_bytes': len(data)}
    def read_artifact(self, ref):
        from . import artifact_store
        if ref.get('store') == 's3': return artifact_store.store().get(ref)
        return Path(ref['path']).read_bytes()
    def artifact_exists(self, ref):
        from . import artifact_store
        if ref.get('store') == 's3':
            storage = artifact_store.store(); parameters = storage.validate(ref)
            head = storage.client.head_object(Bucket=parameters['Bucket'], Key=parameters['Key'], VersionId=parameters['VersionId'])
            return head.get('ContentLength') == ref['size_bytes']
        path = Path(ref.get('path', ''))
        return path.is_file() and _sha256_file(path) == ref.get('sha256')


def emit(services, message):
    print(message, file=services.err, flush=True)


# --------------------------------------------------------------------------------------
# Targets and protections


def load_targets(path):
    import yaml
    from .mysql_database import validate_database
    data = yaml.safe_load(Path(path).read_text()) or {}
    keys, targets = {'database', 'prefix', 'vector_environment', 'upstash_host', 'production'}, {}
    for name, entry in (data.get('targets') or {}).items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', str(name)) or name == PURGE_NAME: raise Refused(f'Invalid target name {name!r}')
        if not isinstance(entry, dict) or set(entry) != keys: raise Refused(f'Target {name} must define exactly {sorted(keys)}')
        try:
            validate_database(entry['database']); application_prefix(entry['prefix'])
        except ValueError as error: raise Refused(f'Target {name}: {error}') from error
        if not ENVIRONMENT_RE.fullmatch(str(entry['vector_environment'])) or not HOST_RE.fullmatch(str(entry['upstash_host']).lower()):
            raise Refused(f'Target {name}: invalid vector environment or Upstash host')
        if not isinstance(entry['production'], bool): raise Refused(f'Target {name}: production must be true or false')
        targets[name] = {'name': name, **entry, 'upstash_host': entry['upstash_host'].lower()}
    if not targets: raise Refused('No reload targets are allow-listed')
    return targets


def select_target(services, name):
    targets = load_targets(services.targets_file)
    if name not in targets: raise Refused(f'Target {name!r} is not allow-listed in {services.targets_file.name}')
    return targets[name]


def check_shell(services, targets):
    for key, field in (('REVEAL_APPLICATION_TABLE_PREFIX', 'prefix'), ('REVEAL_VECTOR_ENVIRONMENT', 'vector_environment')):
        value = services.shell.get(key)
        if value and any(value != target[field] for target in targets):
            raise Refused(f'Shell {key}={value} disagrees with the target ({", ".join(sorted({t[field] for t in targets}))}); unset it')


def check_database(services, targets):
    database = services.environ.get('REVEAL_MYSQL_DATABASE') or 'cyaka_reveal_mechanisms'
    if any(database != target['database'] for target in targets):
        raise Refused(f'REVEAL_MYSQL_DATABASE={database} is not the allow-listed database of the target')
    return database


def upstash_host(services):
    return (urlparse(services.environ.get('UPSTASH_VECTOR_REST_URL') or '').hostname or '').lower()


def check_upstash(services, targets):
    host = upstash_host(services)
    if any(host != target['upstash_host'] for target in targets):
        raise Refused(f'UPSTASH_VECTOR_REST_URL host {host or "(unset)"} is not the allow-listed host of the target')
    return host


def bind_target(services, target, *, upstash=True):
    """Refuse unless shell, database and Upstash host match; then pin this process to the target."""
    check_shell(services, [target]); check_database(services, [target])
    if upstash: check_upstash(services, [target])
    # Helpers such as vector_ingestion.environment() read these settings.
    services.environ['REVEAL_APPLICATION_TABLE_PREFIX'] = target['prefix']
    services.environ['REVEAL_VECTOR_ENVIRONMENT'] = target['vector_environment']
    return target


def check_production(services, plan, allow_production):
    if not plan_production(plan): return False
    if not allow_production: raise Refused('Production plans require --allow-production')
    if services.environ.get(PRODUCTION_APPROVAL) != plan['plan_sha256']:
        raise Refused(f'Production plans require {PRODUCTION_APPROVAL}=<plan_sha256>')
    return True


def plan_production(plan): return bool(plan['target'].get('production'))


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


@contextmanager
def _writer(path):
    """Text writer; .gz output depends only on content (no mtime or file name)."""
    if str(path).endswith('.gz'):
        with open(path, 'wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as packed, \
                io.TextIOWrapper(packed, encoding='utf-8', newline='') as text:
            yield text
    else:
        with open(path, 'w', encoding='utf-8', newline='') as text: yield text


def write_jsonl(path, rows):
    count = 0
    with _writer(path) as out:
        for row in rows: out.write(canonical(row) + '\n'); count += 1
    return count


def read_jsonl(path):
    with _open_text(path) as stream:
        for line in stream:
            if line.strip(): yield json.loads(line)


def read_json(path): return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value, *, private=False):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n'
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    if private: temporary.chmod(0o600)
    temporary.replace(path)
    return path


def _jsonable(value): return json.loads(json.dumps(value, default=str))


def _plain(value):
    """JSON-ready copy: sets become sorted lists, tuples lists, dates ISO strings."""
    if isinstance(value, dict): return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)): return sorted((_plain(item) for item in value), key=canonical)
    if isinstance(value, (list, tuple)): return [_plain(item) for item in value]
    if isinstance(value, (datetime, date)): return value.isoformat()
    return value


# --------------------------------------------------------------------------------------
# build: LAP project -> immutable bundle DIR/<generation_id>/

LAP_FILES = {
    'trait_kpn_map': ('trait_kpn_map.tsv', ('trait', 'kpn_trait_id', 'kpn_release', 'kpn_release_commit', 'gwas_source_category',
                                            'phenotype_name', 'trait_group', 'legacy_trait_group', 'trait_type', 'n_factors')),
    'factor_index': ('factor_index.tsv', ('global_eaggl_column', 'factor_id', 'trait', 'kpn_trait_id', 'factor', 'factor_number',
                                          'factor_label', 'n_nonzero_loadings', 'loading_l2', 'loading_variant')),
    'factor_metadata': ('factor_metadata.tsv', ('factor_id', 'trait', 'factor', 'factor_number', 'label')),
    'gene_set_index': ('gene_set_index.tsv.gz', ('gene_set_id', 'gene_set_name', 'collection_id', 'cfde_label', 'library', 'partition',
                                                 'model', 'comparison', 'program', 'gmt_row', 'n_genes', 'n_genes_in_eaggl_universe', 'cfde_snapshot')),
    'top_gene_sets': ('top_gene_sets_per_factor.tsv.gz', ('trait', 'kpn_trait_id', 'factor_id', 'factor', 'gene_set_id', 'joint_loading',
                                                          'marginal_loading', 'joint_rank_in_factor', 'marginal_rank_in_factor', 'is_joint_top_factor')),
    'projection_manifest': ('projection_manifest.tsv', ('trait', 'kpn_trait_id', 'kpn_release', 'n_factors', 'pigean_commit', 'qc_pass')),
    'cfde_index': ('cfde_index.tsv', ('library', 'partition', 'model', 'comparison', 'program', 'label', 'collection_id', 'n_sets', 'n_genes')),
    'kpn_trait_registry': ('kpn_trait_registry.tsv', ('portal_id', 'gwas_source_category', 'legacy_phenotype_id', 'phenotype_name',
                                                      'legacy_trait_group', 'trait_group', 'trait_type', 'pigean_id')),
    'kpn_trait_flat': ('kpn_trait_flat.tsv', ('portal_id', 'description', 'is_dichotomous', 'is_complex', 'target_id', 'target_label',
                                              'target_ontology', 'mapping_predicate', 'confidence')),
}
BUNDLE_FILES = ('kpn_traits.jsonl', 'reference_factors.jsonl.gz', 'cfde_collections.jsonl', 'cfde_gene_sets.jsonl.gz', 'projections.tsv.gz')
PROJECTION_COLUMNS = ('scope', 'factor_key', 'gene_set_id', 'joint_loading', 'marginal_loading', 'joint_rank', 'marginal_rank', 'is_joint_top_factor')
MAPPING_COLUMNS = ('target_id', 'target_label', 'target_ontology', 'mapping_predicate', 'confidence', 'mapping_justification', 'source')


def lap_inputs(project_dir):
    project_dir = Path(project_dir)
    stems = [path.name[:-len('.projection_manifest.tsv')] for path in project_dir.glob('*.projection_manifest.tsv')]
    if len(stems) != 1: raise Refused(f'{project_dir}: expected one *.projection_manifest.tsv, found {len(stems)}')
    paths = {role: project_dir / f'{stems[0]}.{suffix}' for role, (suffix, _) in LAP_FILES.items()}
    missing = [path.name for path in paths.values() if not path.is_file()]
    if missing: raise Refused(f'{project_dir}: missing LAP outputs {missing}')
    return stems[0], paths


def collection_header(path, *, limit=32 << 20):
    """Collection-level fields of a DAPPER GeneSetCollection document, never parsing its gene sets."""
    import yaml
    lines, size, seen = [], 0, False
    with open(path, encoding='utf-8') as stream:
        for line in stream:
            if line.startswith('gene_sets:'): break
            seen = seen or line.startswith('gene_set_collections:')
            size += len(line)
            if size > limit: raise Refused(f'{Path(path).name}: collection header exceeds {limit} bytes')
            lines.append(line)
        else: raise Refused(f'{Path(path).name}: no top-level gene_sets list')
    if not seen: raise Refused(f'{Path(path).name}: gene_set_collections must precede gene_sets')
    header = _jsonable(yaml.safe_load(''.join(lines)) or {})
    collections = header.get('gene_set_collections') or []
    if len(collections) != 1: raise Refused(f'{Path(path).name}: expected exactly one GeneSetCollection')
    return header, {key: value for key, value in collections[0].items() if key != 'members'}


def collection_gene_sets(path):
    """Every GeneSet node of a DAPPER GeneSetCollection document, parsed one list item at a time.

    The top-level `gene_sets:` list is streamed (the LINCS document is 167 MB); each `- ` item
    at column 0 is loaded on its own and the list ends at the next top-level key, so later
    sections (edges, embeddings) are never parsed.
    """
    import yaml
    loader = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
    def parse(lines, number):
        try: items = yaml.load(''.join(lines), Loader=loader)
        except yaml.YAMLError as error: raise Refused(f'{Path(path).name}: invalid gene set YAML near line {number}') from error
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or not isinstance(items[0].get('id'), str):
            raise Refused(f'{Path(path).name}: malformed gene set near line {number}')
        return _jsonable(items[0])
    with open(path, encoding='utf-8') as stream:
        number = 0
        for line in stream:
            number += 1
            if line.startswith('gene_sets:'): break
        else: raise Refused(f'{Path(path).name}: no top-level gene_sets list')
        if line.split(':', 1)[1].strip() not in ('', '[]'): raise Refused(f'{Path(path).name}: gene_sets must be a block list')
        item, start = [], number
        for line in stream:
            number += 1
            if line.startswith('- ') or line.rstrip('\n') == '-':
                if item: yield parse(item, start)
                item, start = [line], number
            elif line[:1] in (' ', '\n', '#'):
                if item: item.append(line)
            else: break
        if item: yield parse(item, start)


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


def build_bundle(project_dir, out_dir, *, kpn_release=None):
    """Validate LAP outputs and write the load-ready bundle. Pure files; deterministic."""
    project_dir = Path(project_dir)
    stem, paths = lap_inputs(project_dir)
    table = {role: read_tsv(paths[role], LAP_FILES[role][1]) for role in LAP_FILES if role not in ('top_gene_sets', 'gene_set_index')}
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
    lap = {'project': stem, 'pigean_commit': pigean_commit, 'projection_scope': rg.PROJECTION_SCOPE, 'kpn_release': release}
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
                    'trait_type': kpn['trait_type'], 'gwas_source_category': kpn['gwas_source_category']}, 'lap': lap}
        factors.append({'factor_key': key, 'public_id': rg.public_id(entry['kpn_trait_id'], entry['factor']), 'eaggl_factor_id': eaggl_id,
                        'kpn_trait_id': entry['kpn_trait_id'], 'factor_number': _number(entry['factor_number'], eaggl_id),
                        'label': source['label'], 'input_text': text, 'input_sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'source_revision': digest(metadata), 'metadata': metadata})
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
    known_sets = {row['gene_set_id'] for row in gene_sets}
    if len(known_sets) != len(gene_sets): raise Refused('gene_set_index has duplicate gene sets')
    collections, documents, nodes = [], {}, {}
    indexed = {}
    for row in gene_sets: indexed.setdefault(row['collection_id'], set()).add(row['gene_set_id'])
    for collection_id, row in sorted(index.items()):
        if not re.fullmatch(r'dapper:GeneSetCollection\.[A-Za-z0-9_-]{32}', collection_id): raise Refused(f'Invalid collection id {collection_id!r}')
        if members.get(collection_id, 0) != _number(row['n_sets'], row['label']): raise Refused(f"{row['label']}: n_sets differs from its gene sets")
        path = project_dir / 'collections' / row['label'] / f"{row['label']}.GeneSetCollection.yaml"
        if not path.is_file(): raise Refused(f'Missing collection document {path}')
        header, document = collection_header(path)
        if document.get('id') != collection_id or document.get('n_sets', members[collection_id]) != members[collection_id]:
            raise Refused(f"{row['label']}: document does not describe collection {collection_id}")
        documents[row['label']] = _sha256_file(path)
        found = {}
        for node in collection_gene_sets(path):
            if node['id'] in found: raise Refused(f"{row['label']}: duplicate gene set {node['id']}")
            found[node['id']] = canonical(node)  # compact text; ~250 MB of YAML across the snapshot
        if set(found) != indexed.get(collection_id, set()): raise Refused(f"{row['label']}: document gene sets differ from gene_set_index")
        nodes.update(found)
        collections.append({'collection_id': collection_id, 'cfde_label': row['label'], 'library': row['library'],
                            'n_sets': members[collection_id], 'payload': {'collection': document, 'index': row, 'document_sha256': documents[row['label']],
                            'provenance': {key: header.get(key) for key in ('prefixes', 'organizations', 'datasets', 'files', 'activities')}}})
    identity = {'format': BUNDLE_FORMAT, 'schema_version': BUNDLE_SCHEMA_VERSION, 'kind': rg.KPN_KIND, 'model': rg.KPN_MODEL,
                'projection_scope': rg.PROJECTION_SCOPE, 'kpn_release': release, 'kpn_release_commit': release_commit,
                'pigean_commit': pigean_commit, 'inputs': {role: _sha256_file(path) for role, path in sorted(paths.items())},
                'collections': dict(sorted(documents.items()))}
    generation_id = digest(identity)
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / generation_id
    temporary = Path(tempfile.mkdtemp(prefix='.bundle-', dir=out_dir))
    try:
        counts = {'kpn_traits': write_jsonl(temporary / 'kpn_traits.jsonl', traits),
                  'reference_factors': write_jsonl(temporary / 'reference_factors.jsonl.gz', factors),
                  'cfde_collections': write_jsonl(temporary / 'cfde_collections.jsonl', collections),
                  'cfde_gene_sets': write_jsonl(temporary / 'cfde_gene_sets.jsonl.gz', (
                      {**row, 'metadata': {**row['metadata'], 'dapper_gene_set': json.loads(nodes[row['gene_set_id']])}} for row in gene_sets)),
                  'projections': _write_projections(temporary / 'projections.tsv.gz', paths['top_gene_sets'], keys, index_rows, known_sets)}
        missing_top = sorted(set(keys.values()) - _projected_factors(temporary / 'projections.tsv.gz'))
        if missing_top: raise Refused(f'{len(missing_top)} factors have no projections, e.g. {missing_top[:3]}')
        manifest = {**identity, 'generation_id': generation_id, 'lap_project': stem, 'sources': {role: str(path) for role, path in sorted(paths.items())},
                    'counts': counts, 'files': {name: _sha256_file(temporary / name) for name in BUNDLE_FILES}}
        write_json(temporary / 'manifest.json', manifest)
        if final.exists():
            existing = read_json(final / 'manifest.json')
            if {k: existing.get(k) for k in ('generation_id', 'files', 'counts')} != {k: manifest[k] for k in ('generation_id', 'files', 'counts')}:
                raise Refused(f'{final} exists with different content')
            return {'generation_id': generation_id, 'bundle': str(final), 'counts': counts, 'reused': True}
        temporary.rename(final); final.chmod(0o775)
    finally:
        if temporary.exists(): shutil.rmtree(temporary)
    return {'generation_id': generation_id, 'bundle': str(final), 'counts': counts, 'reused': False}


def _write_projections(path, source, keys, factors, gene_sets):
    seen, count = set(), 0
    with _writer(path) as out:
        out.write('\t'.join(PROJECTION_COLUMNS) + '\n')
        for row in iter_tsv(source, LAP_FILES['top_gene_sets'][1]):
            where = f"{row['factor_id']}/{row['gene_set_id']}"
            key = keys.get(row['factor_id'])
            if not key or factors[row['factor_id']]['kpn_trait_id'] != row['kpn_trait_id'] or row['gene_set_id'] not in gene_sets:
                raise Refused(f'{where}: unknown factor or gene set in projections')
            for value in (row['joint_loading'], row['marginal_loading']):
                _number(value, where, float)
                if len(value) > 32: raise Refused(f'{where}: loading text too long')
            joint, marginal = _number(row['joint_rank_in_factor'], where), _number(row['marginal_rank_in_factor'], where)
            if joint < 1 or marginal < 1 or row['is_joint_top_factor'] not in ('0', '1'): raise Refused(f'{where}: invalid rank or flag')
            identity = key + '\t' + row['gene_set_id']
            if identity in seen: raise Refused(f'{where}: duplicate projection')
            seen.add(identity)
            out.write('\t'.join((rg.PROJECTION_SCOPE, key, row['gene_set_id'], row['joint_loading'], row['marginal_loading'],
                                 str(joint), str(marginal), row['is_joint_top_factor'])) + '\n')
            count += 1
    return count


def _projected_factors(path): return {row['factor_key'] for row in iter_tsv(path, PROJECTION_COLUMNS)}


def open_bundle(bundle):
    bundle = Path(bundle)
    manifest = read_json(bundle / 'manifest.json')
    if manifest.get('format') != BUNDLE_FORMAT: raise Refused(f'{bundle}: not a reference bundle')
    identity = {key: manifest[key] for key in ('format', 'schema_version', 'kind', 'model', 'projection_scope', 'kpn_release',
                                                'kpn_release_commit', 'pigean_commit', 'inputs', 'collections')}
    if digest(identity) != manifest['generation_id'] or bundle.name != manifest['generation_id']: raise Refused(f'{bundle}: invalid generation identity')
    for name, sha in manifest['files'].items():
        if _sha256_file(bundle / name) != sha: raise Refused(f'{bundle}: checksum mismatch in {name}')
    return manifest


# --------------------------------------------------------------------------------------
# embed: CFDE snapshot vectors -> DIR/<gen>/vectors/ (float32 LE) + embedding space


def open_vectors(bundle):
    directory = Path(bundle) / 'vectors'
    if not (directory / 'manifest.json').is_file(): return None
    manifest = read_json(directory / 'manifest.json')
    if manifest.get('format') != VECTORS_FORMAT or manifest.get('generation_id') != Path(bundle).name: raise Refused(f'{directory}: invalid vectors manifest')
    for name, sha in manifest['files'].items():
        if _sha256_file(directory / name) != sha: raise Refused(f'{directory}: checksum mismatch in {name}')
    space = read_json(directory / 'embedding_space.json')
    if space['space_id'] != space_digest(space): raise Refused(f'{directory}: invalid embedding space identity')
    return manifest


def space_digest(space):
    calibration = space['calibration']
    identity = {key: space[key] for key in ('model', 'model_revision', 'provider', 'service_url_sha256', 'dimensions', 'metric', 'normalization')}
    # Measured cosines vary in the last bits between probes; the probe inputs, method,
    # threshold and source matrix identify the calibration.
    identity['calibration'] = {key: calibration.get(key) for key in ('method', 'threshold', 'skipped', 'reason', 'source_matrix_sha256', 'source_dtype', 'upcast')}
    identity['calibration']['probe_inputs'] = [probe['input_sha256'] for probe in calibration.get('probes', [])]
    return digest(identity)


def embed_bundle(services, bundle, *, embeddings_dir=None, model=None, model_revision=None, provider=None, service_url=None,
                 samples=32, skip_calibration=False):
    import numpy as np
    from .eaggl_embeddings import validate_vectors
    from .embedding_client import DEFAULT_MODEL, DEFAULT_SERVICE_URL
    bundle = Path(bundle)
    manifest = open_bundle(bundle)
    existing = open_vectors(bundle)
    if existing: return {'generation_id': manifest['generation_id'], 'space_id': existing['space_id'], 'count': existing['count'], 'reused': True}
    env = services.environ
    model = model or env.get('EMBEDDING_MODEL') or DEFAULT_MODEL
    service_url = (service_url or env.get('EMBEDDING_SERVICE_URL') or DEFAULT_SERVICE_URL).rstrip('/')
    provider = provider or env.get('EMBEDDING_PROVIDER') or 'huggingface'
    model_revision = model_revision or env.get('EMBEDDING_MODEL_REVISION') or 'unspecified'
    source = Path(embeddings_dir) if embeddings_dir else DEFAULT_EMBEDDINGS / model.replace('/', '-')
    matrix_path = next((path for path in (source / 'vectors.float16.npy', source / 'vectors.float32.npy') if path.is_file()), None)
    if matrix_path is None or not (source / 'rows.tsv').is_file(): raise Refused(f'{source}: missing vectors.<dtype>.npy or rows.tsv')
    matrix = np.load(matrix_path, mmap_mode='r', allow_pickle=False)
    rows = read_tsv(source / 'rows.tsv', ('row', 'node_id', 'node_class', 'text_template', 'text'))
    if matrix.ndim != 2 or matrix.shape[0] != len(rows) or any(int(row['row']) != position for position, row in enumerate(rows)):
        raise Refused(f'{matrix_path.name} does not match rows.tsv')
    matrix_sha = _sha256_file(matrix_path)
    snapshot_manifest = source.parent.parent / 'MANIFEST.json'
    if snapshot_manifest.is_file():
        recorded = (read_json(snapshot_manifest).get('embeddings') or {})
        if recorded.get('model') not in (None, model) or recorded.get('matrix_sha256') not in (None, matrix_sha):
            raise Refused('CFDE snapshot MANIFEST.json disagrees with the embedding matrix or model')
    by_node = {}
    for row in rows: by_node.setdefault((row['node_class'], row['node_id']), []).append(row)
    wanted = [('cfde_collection', row['collection_id'], 'GeneSetCollection') for row in read_jsonl(bundle / 'cfde_collections.jsonl')]
    wanted += [('cfde_gene_set', row['gene_set_id'], 'GeneSet') for row in read_jsonl(bundle / 'cfde_gene_sets.jsonl.gz')]
    selected = []
    for kind, identity, node_class in sorted(wanted):
        found = by_node.get((node_class, identity), [])
        if len(found) != 1: raise Refused(f'{identity}: {len(found)} vectors in the CFDE snapshot (expected 1)')
        selected.append((kind, identity, found[0]))
    vectors = validate_vectors(np.asarray(matrix[[int(row['row']) for _, _, row in selected]], dtype='<f4'), len(selected))
    dimensions = int(vectors.shape[1])
    calibration = {'method': 'reembed-cosine', 'threshold': CALIBRATION_THRESHOLD, 'skipped': bool(skip_calibration),
                   'reason': 'operator --skip-calibration' if skip_calibration else None, 'source_matrix_sha256': matrix_sha,
                   'source_dtype': str(matrix.dtype), 'upcast': 'float32-le', 'probes': []}
    if not skip_calibration:
        positions = {kind: [i for i, item in enumerate(selected) if item[0] == kind] for kind in ('cfde_collection', 'cfde_gene_set')}
        few = min(4, len(positions['cfde_collection']), max(1, samples // 8))
        chosen = [positions['cfde_collection'][i] for i in np.linspace(0, len(positions['cfde_collection']) - 1, few, dtype=int)] if few else []
        rest = max(1, samples - len(chosen))
        chosen += [positions['cfde_gene_set'][i] for i in np.linspace(0, len(positions['cfde_gene_set']) - 1, min(rest, len(positions['cfde_gene_set'])), dtype=int)]
        chosen = sorted(set(chosen))
        texts = [selected[i][2]['text'] for i in chosen]
        emit(services, f'Calibrating {len(texts)} CFDE vectors against {model}')
        fresh = validate_vectors(services.embed(texts, model=model, service_url=service_url, provider=provider,
                                                batch_size=min(100, len(texts)), max_workers=1, max_retries=3, timeout=120), len(texts), dimensions)
        for i, vector in zip(chosen, fresh):
            stored = vectors[i].astype(np.float64); probe = np.asarray(vector, dtype=np.float64)
            cosine = float(stored @ probe / (np.linalg.norm(stored) * np.linalg.norm(probe)))
            calibration['probes'].append({'source_kind': selected[i][0], 'source_id': selected[i][1],
                                          'input_sha256': hashlib.sha256(selected[i][2]['text'].encode()).hexdigest(), 'cosine': cosine})
        calibration['minimum_cosine'] = min(probe['cosine'] for probe in calibration['probes'])
        if calibration['minimum_cosine'] < CALIBRATION_THRESHOLD:
            raise Refused(f"Calibration failed: minimum cosine {calibration['minimum_cosine']:.6f} < {CALIBRATION_THRESHOLD}; "
                          'the embedding service no longer reproduces the CFDE snapshot vectors (re-embed them)')
    space = {'model': model, 'model_revision': model_revision, 'provider': provider,
             'service_url_sha256': hashlib.sha256(service_url.encode()).hexdigest(), 'dimensions': dimensions, 'metric': 'cosine',
             'normalization': 'none', 'calibration': calibration}
    space['space_id'] = space_digest(space)
    temporary = Path(tempfile.mkdtemp(prefix='.vectors-', dir=bundle))
    try:
        np.save(temporary / 'reference_vectors.f32.npy', np.ascontiguousarray(vectors, dtype='<f4'), allow_pickle=False)
        write_jsonl(temporary / 'rows.jsonl.gz', ({'row': position, 'source_kind': kind, 'source_id': identity, 'input_text': row['text'],
                    'input_sha256': hashlib.sha256(row['text'].encode()).hexdigest(), 'text_template': row['text_template'],
                    'embedding_id': row.get('embedding_id') or None, 'source_row': int(row['row']),
                    'vector_sha256': hashlib.sha256(vectors[position].tobytes()).hexdigest()}
                   for position, (kind, identity, row) in enumerate(selected)))
        write_json(temporary / 'embedding_space.json', space)
        files = {name: _sha256_file(temporary / name) for name in ('reference_vectors.f32.npy', 'rows.jsonl.gz', 'embedding_space.json')}
        write_json(temporary / 'manifest.json', {'format': VECTORS_FORMAT, 'generation_id': manifest['generation_id'], 'space_id': space['space_id'],
                                                 'count': len(selected), 'dimensions': dimensions, 'source': str(matrix_path), 'files': files})
        temporary.rename(bundle / 'vectors'); (bundle / 'vectors').chmod(0o775)
    finally:
        if temporary.exists(): shutil.rmtree(temporary)
    return {'generation_id': manifest['generation_id'], 'space_id': space['space_id'], 'count': len(selected),
            'calibration': {key: calibration.get(key) for key in ('skipped', 'minimum_cosine', 'threshold')} | {'probes': len(calibration['probes'])},
            'reused': False}


# --------------------------------------------------------------------------------------
# Shared-table helpers (explicit DB-API connection)


def query(connection, sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def scalar(connection, sql, params=()):
    rows = query(connection, sql, params)
    return rows[0][0] if rows else None


def table_exists(connection, name):
    return bool(scalar(connection, 'SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s', (name,)))


def _placeholders(values): return ','.join(['%s'] * len(values))


def _pinned(services, key, values):
    pin = services.environ.get(key)
    return [value for value in values if not pin or value == pin]


def select_dismech_import(services, connection):
    imports = _pinned(services, 'REVEAL_DISMECH_IMPORT_ID', [row[0] for row in query(connection, "SELECT import_id FROM dismech_imports WHERE status='complete'")])
    if len(imports) != 1: raise Refused('Select one complete DisMech import (pin REVEAL_DISMECH_IMPORT_ID)')
    return imports[0]


def legacy_sources(services, connection):
    """The served mapping run, its EAGGL and gene-set imports and its embedding run, selected by the REVEAL_* pins
    alone, as the deployed catalog selects them (never by --eaggl-* arguments). None when there is no mapping run."""
    if not table_exists(connection, 'eaggl_cfde_link_runs'): return None
    runs = [row for row in query(connection, "SELECT run_id,eaggl_import_id,gene_set_import_id FROM eaggl_cfde_link_runs WHERE status='complete'")
            if not services.environ.get('REVEAL_MAPPING_RUN_ID') or row[0] == services.environ['REVEAL_MAPPING_RUN_ID']]
    if not runs and services.environ.get('REVEAL_MAPPING_RUN_ID'): raise Refused('REVEAL_MAPPING_RUN_ID is not a complete EAGGL mapping run')
    if not runs: return None
    if len(runs) != 1: raise Refused('Several complete EAGGL mapping runs: pin REVEAL_MAPPING_RUN_ID to the served one')
    mapping, eaggl_import, gene_set_import = runs[0]
    embedding = served_runs(services, connection, eaggl_import)
    if len(embedding) != 1: raise Refused('Select the served EAGGL embedding run (pin REVEAL_EMBEDDING_RUN_ID)')
    return {'mapping_run_id': mapping, 'eaggl_import_id': eaggl_import, 'gene_set_import_id': gene_set_import, 'eaggl_embedding_run_id': embedding[0]}


def register_legacy(services, connection):
    """Register the pre-reload data as the legacy generation (idempotent). None when there is none."""
    legacy = legacy_sources(services, connection)
    if not legacy: return None
    return rg.register_legacy_generation(connection, **legacy, dismech_import_id=select_dismech_import(services, connection))


EAGGL_INSTRUCTIONS = ('Load the EAGGL factors of this bundle first: python scripts/import_eaggl_factors.py prepare --bundle '
                      'lap/raw/EAGGL_capped_union_graph_share --source-version {version} --output <capture>; then `embed --output <capture>` and '
                      '`load --output <capture> --apply`; then rerun with --eaggl-import-id <import_id>.')
EAGGL_STOP = 'Do not re-import EAGGL factors: a second complete embedding run or import breaks the deployed readiness checks.'


def eaggl_imports(connection, import_id, version):
    if import_id: return query(connection, 'SELECT import_id,status,source_version FROM eaggl_imports WHERE import_id=%s', (import_id,))
    return query(connection, "SELECT import_id,status,source_version FROM eaggl_imports WHERE source_version=%s AND status='complete'", (version,))


def complete_runs(connection, import_id):
    return [row[0] for row in query(connection, "SELECT run_id FROM eaggl_embedding_runs WHERE import_id=%s AND status='complete'", (import_id,))]


def served_runs(services, connection, import_id):
    """Complete embedding runs of an import that REVEAL_EMBEDDING_RUN_ID selects: register_legacy's choice (no argument pin)."""
    return _pinned(services, 'REVEAL_EMBEDDING_RUN_ID', sorted(complete_runs(connection, import_id)))


def select_runs(services, runs, run_id=None):
    pin = run_id or services.environ.get('REVEAL_EMBEDDING_RUN_ID')
    return [run for run in runs if not pin or run == pin]


def eaggl_source(services, connection, factors, *, import_id=None, source_version=None, run_id=None):
    """The complete EAGGL import and embedding run that carry this bundle's factors and label vectors.

    A pinned import or run (argument or REVEAL_EMBEDDING_RUN_ID) is the served one: refusals then
    say stop, never re-import (a second import or embedding run breaks deployed readiness).
    """
    version = source_version or DEFAULT_EAGGL_SOURCE_VERSION
    pinned = bool(import_id or run_id or services.environ.get('REVEAL_EMBEDDING_RUN_ID'))
    def refuse(stop, problem): raise Refused(f'Stop: {stop}. {EAGGL_STOP}' if pinned else f'{problem}. {EAGGL_INSTRUCTIONS.format(version=version)} {EAGGL_STOP}')
    rows = eaggl_imports(connection, import_id, version)
    if len(rows) != 1 or rows[0][1] != 'complete':
        refuse(f"the pinned EAGGL source ({f'import {import_id}' if import_id else f'source version {version}'}) has no complete import",
               'No complete EAGGL import for this bundle')
    import_id = rows[0][0]
    stored = dict(query(connection, 'SELECT factor_id,input_sha256 FROM eaggl_factors WHERE import_id=%s', (import_id,)))
    wrong = [factor['eaggl_factor_id'] for factor in factors if stored.get(factor['eaggl_factor_id']) != factor['input_sha256']]
    if wrong:
        refuse(f"the pinned EAGGL import {import_id[:12]} does not carry this bundle's factors/labels ({len(wrong)} differ, e.g. {wrong[:3]})",
               f'EAGGL import {import_id[:12]} lacks {len(wrong)} bundle factors or labels, e.g. {wrong[:3]}')
    runs = select_runs(services, complete_runs(connection, import_id), run_id)
    if len(runs) != 1:
        stop = f'the pinned EAGGL source has {len(runs)} matching complete embedding runs of import {import_id[:12]}, not one'
        raise Refused(f'Stop: {stop}. {EAGGL_STOP}' if pinned else
                      f'Select one complete EAGGL embedding run of import {import_id[:12]} (--eaggl-embedding-run-id), found {len(runs)}. {EAGGL_STOP}')
    embedded = {row[0] for row in query(connection, 'SELECT input_sha256 FROM eaggl_name_embeddings WHERE run_id=%s', (runs[0],))}
    missing = {factor['input_sha256'] for factor in factors} - embedded
    if missing:
        message = f'EAGGL embedding run {runs[0][:12]} lacks {len(missing)} factor label vectors of this bundle'
        raise Refused(f'Stop: the pinned {message}. {EAGGL_STOP}' if pinned else f'{message}. {EAGGL_STOP}')
    return {'eaggl_import_id': import_id, 'eaggl_embedding_run_id': runs[0], 'source_version': rows[0][2]}


def run_config(connection, run_id):
    """Model, revision, provider, service URL and dimensions of an EAGGL embedding run (embed_capture's defaults)."""
    rows = query(connection, 'SELECT config,dimensions FROM eaggl_embedding_runs WHERE run_id=%s', (run_id,))
    if not rows: raise Refused(f'Unknown EAGGL embedding run {run_id[:12]}')
    config = json.loads(rows[0][0]) if isinstance(rows[0][0], (str, bytes, bytearray)) else rows[0][0] or {}
    return {'model': config.get('model'), 'model_revision': config.get('model_revision') or 'unspecified',
            'provider': config.get('provider') or 'huggingface', 'service_url': (config.get('service_url') or '').rstrip('/') or None,
            'dimensions': rows[0][1]}


def check_embedding_space(connection, space, run_id, bundle='<bundle>'):
    """Refuse unless the bundle's CFDE vectors were made in the EAGGL run's space (comparable with its factor vectors)."""
    run = run_config(connection, run_id)
    expected = {'model': run['model'], 'model_revision': run['model_revision'], 'provider': run['provider'], 'dimensions': run['dimensions'],
                'service_url_sha256': run['service_url'] and hashlib.sha256(run['service_url'].encode()).hexdigest()}
    differ = sorted(key for key, value in expected.items() if space.get(key) != value)
    if differ:
        raise Refused(f"The bundle's embedding space differs from EAGGL embedding run {run_id[:12]} in {differ}. Move {bundle}/vectors aside and "
                      f"re-run embed with that run's settings: embed --bundle {bundle} --model {run['model']} --model-revision {run['model_revision']} "
                      f"--provider {run['provider']} --service-url {run['service_url']}")
    return True


def insert_rows(connection, table, columns, rows, batch_size):
    from .mysql_database import insert_batch
    batch, count = [], 0
    def flush():
        with connection.cursor() as cursor: insert_batch(cursor, table, columns, batch)
        connection.commit()
    for row in rows:
        batch.append(row); count += 1
        if len(batch) >= batch_size: flush(); batch = []
    if batch: flush()
    return count


def delete_batches(connection, table, where, params=(), *, limit=DELETE_BATCH):
    """DELETE ... LIMIT n, committed per batch, only from purge-allow-listed tables."""
    if table in PROTECTED_TABLES or table not in PURGE_TABLES: raise Refused(f'Refusing to delete from protected table {table}')
    total = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(f'DELETE FROM {table} WHERE {where} LIMIT {int(limit)}', tuple(params))
            count = max(cursor.rowcount or 0, 0)
        connection.commit(); total += count
        if count < limit: return total


# The only deletes from protected tables: one row each, for `abandon` of a KPN generation that never served.
ABANDON_STATUSES = ('loading', 'failed', 'complete')
ABANDON_DELETES = {
    'embedding_spaces': 'DELETE FROM embedding_spaces WHERE space_id=%s AND NOT EXISTS (SELECT 1 FROM reference_vectors WHERE space_id=%s) LIMIT 1',
    'reference_generations': 'DELETE FROM reference_generations WHERE generation_id=%s AND kind=%s AND status IN (%s,%s,%s) LIMIT 1'}


def delete_abandoned_row(connection, table, identity):
    """One embedding_spaces row no vector uses, or one abandonable KPN reference_generations row."""
    if table not in ABANDON_DELETES: raise Refused(f'Refusing to delete from protected table {table}')
    params = (identity, identity) if table == 'embedding_spaces' else (identity, rg.KPN_KIND, *ABANDON_STATUSES)
    with connection.cursor() as cursor:
        cursor.execute(ABANDON_DELETES[table], params)
        count = max(cursor.rowcount or 0, 0)
    connection.commit()
    return count


def generation_counts(connection, generation_id):
    return {name: scalar(connection, f'SELECT COUNT(*) FROM {table} WHERE generation_id=%s', (generation_id,)) for name, table in COUNT_TABLES}


# --------------------------------------------------------------------------------------
# preflight: read-only checks of the shared database (and a bundle) before `load`

PREFLIGHT_FORMAT = 'reveal.reference-reload-preflight/1'
# Migration 008's tables, children before the tables their foreign keys name (the DROP order).
REFERENCE_TABLES = ('reference_vectors', 'factor_gene_set_projections', 'cfde_gene_sets', 'cfde_gene_set_collections', 'reference_factors',
                    'kpn_traits', 'vector_bindings', 'archived_reference_factors', 'embedding_spaces', 'reference_generations')
SOURCE_TABLES = ('eaggl_imports', 'eaggl_embedding_runs', 'eaggl_cfde_link_runs', 'dismech_imports', 'dismech_embedding_runs', 'gene_set_imports')
GRANTS = ('select', 'insert', 'update', 'delete', 'create', 'drop', 'alter', 'index', 'references')
# load --apply: UPDATE marks the generation complete or failed; REFERENCES creates migration 008's foreign keys.
REQUIRED_GRANTS = ('select', 'insert', 'update', 'delete', 'create', 'drop', 'references')
GRANT_RE = re.compile(r'GRANT (.+?) ON (\*|`(?:[^`]|``)+`|\w+)\.(\*|`(?:[^`]|``)+`|\w+) TO ', re.I)
ROLE_RE = re.compile(r'`(?:[^`]|``)+`@`(?:[^`]|``)+`')
PROBE_TEXT = 'reveal reload preflight probe'


def _grant_database(pattern, database):
    """A SHOW GRANTS database: `*`, or a name in which unescaped `_` and `%` are wildcards."""
    if pattern == '*': return True
    text, regex, i = (pattern[1:-1] if pattern.startswith('`') else pattern).replace('``', '`'), '', 0
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text): regex += re.escape(text[i + 1]); i += 2
        else: regex += {'%': '.*', '_': '.'}.get(text[i], re.escape(text[i])); i += 1
    return re.fullmatch(regex, database) is not None


def parse_grants(rows, database):
    """Database-wide privileges (on this database or *.*) of SHOW GRANTS rows; ALL PRIVILEGES counts as every one."""
    held = set()
    for row in rows:
        match = GRANT_RE.match(str(row[0]))
        if not match or match[3] != '*' or not _grant_database(match[2], database): continue
        privileges = {item.strip().lower() for item in match[1].split(',')}
        held |= set(GRANTS) if privileges & {'all', 'all privileges'} else privileges
    return {name: name in held for name in GRANTS}


def _grant_rows(connection, warnings):
    """SHOW GRANTS rows of the current user and its active roles. Without USING, MySQL 8 lists a granted role
    (GRANT `r`@`%` TO ...), not its privileges, so those of the roles CURRENT_ROLE() names are merged in."""
    roles = ROLE_RE.findall(str(scalar(connection, 'SELECT CURRENT_ROLE()') or ''))
    if roles:
        try: return query(connection, 'SHOW GRANTS FOR CURRENT_USER USING ' + ','.join(roles))
        except Exception as error: warnings.append(f'The privileges of {len(roles)} active roles could not be read ({type(error).__name__}); only direct grants count')
    return query(connection, 'SHOW GRANTS FOR CURRENT_USER')


def _server(connection):
    version, innodb_read_only, read_only, mode_global, mode_session, packet = query(
        connection, 'SELECT VERSION(),@@GLOBAL.innodb_read_only,@@GLOBAL.read_only,@@GLOBAL.sql_mode,@@SESSION.sql_mode,@@SESSION.max_allowed_packet')[0]
    aurora = query(connection, "SHOW GLOBAL VARIABLES LIKE 'aurora_version'")
    cipher = query(connection, "SHOW SESSION STATUS LIKE 'Ssl_cipher'")
    timings = []
    for _ in range(10):
        started = time.perf_counter(); query(connection, 'SELECT 1'); timings.append((time.perf_counter() - started) * 1000)
    return {'version': version, 'aurora_version': aurora[0][1] if aurora else None, 'innodb_read_only': bool(int(innodb_read_only or 0)),
            'read_only': bool(int(read_only or 0)), 'sql_mode_global': mode_global, 'sql_mode_session': mode_session,
            'max_allowed_packet': int(packet), 'tls_cipher_present': bool(cipher and cipher[0][1]), 'round_trip_ms': round(statistics.median(timings), 3)}


def _preflight_sources(services, connection, tables, *, import_id, source_version, run_id):
    """The served sources as `load` and register_legacy select them (same pins)."""
    pin = services.environ.get('REVEAL_MAPPING_RUN_ID')
    links = query(connection, "SELECT run_id,eaggl_import_id,gene_set_import_id FROM eaggl_cfde_link_runs WHERE status='complete' ORDER BY run_id") \
        if 'eaggl_cfde_link_runs' in tables else []
    chosen = [row for row in links if not pin or row[0] == pin]
    one = chosen[0] if len(chosen) == 1 else (None, None, None)
    # embedding_runs: register_legacy's (and the deployed catalog's) runs of the mapping run's import, REVEAL_EMBEDDING_RUN_ID only.
    mapping = {'complete': [row[0] for row in links], 'selected': one[0], 'eaggl_import_id': one[1], 'gene_set_import_id': one[2],
               'embedding_runs': served_runs(services, connection, one[1]) if one[1] and 'eaggl_embedding_runs' in tables else []}
    version = source_version or DEFAULT_EAGGL_SOURCE_VERSION
    imports = eaggl_imports(connection, import_id, version) if 'eaggl_imports' in tables else []
    eaggl = {'imports': [row[0] for row in imports], 'import_id': None, 'source_version': version, 'status': None, 'factors': None,
             'loadings': None, 'embedding_runs': [], 'selected_run': None, 'run_config': None}
    if len(imports) == 1:
        identity = imports[0][0]
        runs = sorted(complete_runs(connection, identity))
        selected = select_runs(services, runs, run_id)
        eaggl.update(import_id=identity, status=imports[0][1], source_version=imports[0][2], embedding_runs=runs,
                     factors=scalar(connection, 'SELECT COUNT(*) FROM eaggl_factors WHERE import_id=%s', (identity,)),
                     loadings=scalar(connection, 'SELECT COUNT(*) FROM eaggl_gene_loadings WHERE import_id=%s', (identity,)),
                     selected_run=selected[0] if len(selected) == 1 else None)
        if eaggl['selected_run']: eaggl['run_config'] = run_config(connection, eaggl['selected_run'])
    complete = sorted(row[0] for row in query(connection, "SELECT import_id FROM dismech_imports WHERE status='complete'")) if 'dismech_imports' in tables else []
    try: selected = select_dismech_import(services, connection) if complete else None
    except Refused: selected = None
    context = [{'run_id': row[0], 'dismech_import_id': row[1], 'status': row[2]} for row in query(
        connection, 'SELECT run_id,dismech_import_id,status FROM dismech_embedding_runs WHERE eaggl_embedding_run_id=%s ORDER BY run_id',
        (eaggl['selected_run'],))] if eaggl['selected_run'] and 'dismech_embedding_runs' in tables else []
    # As vector ingestion selects it: complete, of the selected DisMech import, then REVEAL_DISMECH_EMBEDDING_RUN_ID.
    bound = _pinned(services, 'REVEAL_DISMECH_EMBEDDING_RUN_ID', [run['run_id'] for run in context
                                                                 if run['status'] == 'complete' and selected and run['dismech_import_id'] == selected])
    return {'mapping_runs': mapping, 'eaggl': eaggl, 'dismech': {'complete': complete, 'selected': selected, 'context_runs': context,
                                                                 'selected_context_run': bound[0] if len(bound) == 1 else None}}


def _preflight_blockers(result):
    blockers, server, sources = [], result['server'], result['sources']
    if not server['tls_cipher_present']: blockers.append('The connection has no TLS cipher')
    if server['innodb_read_only'] or server['read_only']: blockers.append('The server is read-only (innodb_read_only or read_only): connect to the writer')
    missing = [name for name in REQUIRED_GRANTS if not result['grants'][name]]
    if missing: blockers.append(f"Missing grants on {result['database']}: {missing}")
    if not result['lock_free']: blockers.append('Another reference reload holds the global lock')
    mapping, eaggl, dismech = sources['mapping_runs'], sources['eaggl'], sources['dismech']
    if not mapping['selected']: blockers.append(f"Not exactly one selected complete EAGGL mapping run (REVEAL_MAPPING_RUN_ID); complete: {mapping['complete']}")
    if len(eaggl['imports']) != 1 or eaggl['status'] != 'complete':
        blockers.append(f"EAGGL import not found or not complete (source version {eaggl['source_version']}): {eaggl['imports']} {eaggl['status'] or ''}".rstrip())
    elif len(eaggl['embedding_runs']) != 1 or not eaggl['selected_run']:
        blockers.append(f"Not exactly one selected complete EAGGL embedding run of import {eaggl['import_id'][:12]} (deployed readiness needs exactly one); "
                        f"complete: {eaggl['embedding_runs']}, selected: {eaggl['selected_run']}")
    # load registers the legacy generation from the mapping run's import and REVEAL_EMBEDDING_RUN_ID, never the arguments.
    if mapping['eaggl_import_id'] and eaggl['import_id'] and mapping['eaggl_import_id'] != eaggl['import_id']:
        blockers.append(f"The served mapping run uses EAGGL import {mapping['eaggl_import_id'][:12]}, not the selected {eaggl['import_id'][:12]}")
    elif mapping['selected'] and len(mapping['embedding_runs']) != 1:
        blockers.append(f"Not exactly one complete EAGGL embedding run of the served mapping run's import {mapping['eaggl_import_id'][:12]} "
                        f"selected by REVEAL_EMBEDDING_RUN_ID (load registers the legacy generation with it): {mapping['embedding_runs']}")
    elif mapping['embedding_runs'] and eaggl['selected_run'] and mapping['embedding_runs'][0] != eaggl['selected_run']:
        blockers.append(f"The selected EAGGL embedding run {eaggl['selected_run'][:12]} (--eaggl-embedding-run-id) is not REVEAL_EMBEDDING_RUN_ID's "
                        f"{mapping['embedding_runs'][0][:12]}: pin both to the served run")
    if len(dismech['complete']) != 1 or not dismech['selected']:
        blockers.append(f"Not exactly one selected complete DisMech import (deployed readiness needs exactly one); complete: {dismech['complete']}")
    if not dismech['selected_context_run']:
        blockers.append(f"Not exactly one complete DisMech context run of the selected DisMech import bound to EAGGL embedding run "
                        f"{(eaggl['selected_run'] or '(none)')[:12]} (pin REVEAL_DISMECH_EMBEDDING_RUN_ID among several): {dismech['context_runs']}")
    return blockers


def compare_preflight(baseline, result):
    """Schema and served-source differences from a baseline preflight; approximate row counts are informational."""
    before, after = baseline['inventory'], _jsonable(result['inventory'])
    new = sorted(set(after['approx_rows']) - set(before['approx_rows']))
    removed = sorted(set(before['approx_rows']) - set(after['approx_rows']))
    counts = {table: [before['exact'].get(table), after['exact'].get(table)] for table in SOURCE_TABLES
              if before['exact'].get(table) != after['exact'].get(table)}
    sources = _jsonable(result['sources'])
    changes = {key: {'before': baseline['sources'].get(key), 'after': sources[key]} for key in ('mapping_runs', 'eaggl', 'dismech')
               if baseline['sources'].get(key) != sources[key]}
    approx = {table: [before['approx_rows'][table], after['approx_rows'][table]] for table in sorted(set(before['approx_rows']) & set(after['approx_rows']))
              if before['approx_rows'][table] != after['approx_rows'][table]}
    blockers = [f'Table {table} was removed' for table in removed]
    blockers += [f'New table {table} is not a migration-008 table' for table in new if table not in REFERENCE_TABLES]
    blockers += [f'{table} changed from {pair[0]} to {pair[1]} rows' for table, pair in counts.items()]
    blockers += [f'Served source {key} changed' for key in changes]
    compare = {'baseline_checked_at': baseline.get('checked_at'), 'new_tables': new, 'removed_tables': removed, 'source_count_changes': counts,
               'source_changes': changes, 'approx_row_changes': approx}
    return compare, blockers


def preflight(services, *, out=None, compare=None, bundle=None, eaggl_import_id=None, eaggl_source_version=None, eaggl_embedding_run_id=None,
              check_embedding_service=False):
    """Read-only: server, grants, reload lock, schema inventory and the served sources `load` will pin (docs §7).

    One READ ONLY transaction of SELECT/SHOW statements, rolled back; no secrets, user or host names.
    """
    targets = load_targets(services.targets_file)
    database = check_database(services, targets.values())
    baseline = read_json(compare) if compare else None
    if baseline is not None and (baseline.get('format') != PREFLIGHT_FORMAT or 'inventory' not in baseline or 'sources' not in baseline):
        raise Refused(f'{compare}: not a preflight result')
    manifest = open_bundle(bundle) if bundle else None
    factors = list(read_jsonl(Path(bundle) / 'reference_factors.jsonl.gz')) if bundle else None
    space = read_json(Path(bundle) / 'vectors/embedding_space.json') if bundle and open_vectors(bundle) else None
    result = {'format': PREFLIGHT_FORMAT, 'database': database, 'checked_at': services.clock()}
    blockers, warnings = [], []
    connection = services.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET SESSION TRANSACTION READ ONLY'); cursor.execute('START TRANSACTION READ ONLY')
        result['server'] = _server(connection)
        result['grants'] = parse_grants(_grant_rows(connection, warnings), database)
        result['lock_free'] = scalar(connection, 'SELECT IS_FREE_LOCK(%s)', (LOCK_NAME,)) == 1
        tables = {row[0]: None if row[1] is None else int(row[1]) for row in query(
            connection, 'SELECT TABLE_NAME,TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME')}
        result['reference_tables'] = {table: table in tables for table in REFERENCE_TABLES}
        result['prefixes'] = [{'target': name, 'prefix': target['prefix'], 'records_table': f"{target['prefix']}_records" in tables,
                               'lock_table': f"{target['prefix']}_transaction_lock" in tables} for name, target in sorted(targets.items())]
        result['inventory'] = {'approx_rows': tables, 'exact': {table: scalar(connection, f'SELECT COUNT(*) FROM {table}')
                                                                for table in SOURCE_TABLES + REFERENCE_TABLES if table in tables}}
        result['sources'] = _preflight_sources(services, connection, tables, import_id=eaggl_import_id, source_version=eaggl_source_version,
                                               run_id=eaggl_embedding_run_id)
        if manifest:
            check = result['bundle'] = {'generation_id': manifest['generation_id'], 'vectors': space is not None}
            try:
                check['eaggl'] = eaggl_source(services, connection, factors, import_id=eaggl_import_id, source_version=eaggl_source_version,
                                              run_id=eaggl_embedding_run_id)
                if space: check['embedding_space_matches_run'] = check_embedding_space(connection, space, check['eaggl']['eaggl_embedding_run_id'], bundle)
                else: warnings.append(f'{bundle} has no vectors yet: run `embed --bundle {bundle}` with the EAGGL run settings')
                check['ok'] = True
            except Refused as error:
                check.update(ok=False, refused=str(error)); blockers.append(f'Bundle: {error}')
    finally:
        try: connection.rollback()
        finally: connection.close()
    for entry in result['prefixes']:
        for key in ('records_table', 'lock_table'):
            if not entry[key]: warnings.append(f"{entry['target']}: {entry['prefix']}_{'records' if key == 'records_table' else 'transaction_lock'} does not exist")
    blockers = _preflight_blockers(result) + blockers
    config = result['sources']['eaggl']['run_config']
    if not check_embedding_service: result['embedding_service'] = 'not checked'
    elif not config:
        result['embedding_service'] = {'reachable': False, 'error': 'no selected EAGGL embedding run'}
        blockers.append('Embedding service not checked: no selected EAGGL embedding run')
    else:
        try:
            vectors = services.embed([PROBE_TEXT], model=config['model'], service_url=config['service_url'], provider=config['provider'],
                                     batch_size=1, max_workers=1, max_retries=1, timeout=60)
            result['embedding_service'] = {'reachable': True, 'dimensions': len(vectors[0])}
            if len(vectors[0]) != config['dimensions']:
                blockers.append(f"The embedding service returns {len(vectors[0])} dimensions; the EAGGL run has {config['dimensions']}")
        except Exception as error:
            result['embedding_service'] = {'reachable': False, 'error': f'{type(error).__name__}: {str(error)[:300]}'}
            blockers.append(f"Embedding service {config['service_url']} failed ({type(error).__name__})")
    if baseline is not None:
        result['compare'], changed = compare_preflight(baseline, result)
        blockers += changed
    result.update(blockers=blockers, warnings=warnings, ok=not blockers)
    result = _jsonable(result)
    if out: write_json(out, result)
    return result


# --------------------------------------------------------------------------------------
# load: additive insert of a KPN generation


def _bundle_rows(bundle, vectors_manifest, generation_id, eaggl_import_id):
    import numpy as np
    matrix = np.load(Path(bundle) / 'vectors/reference_vectors.f32.npy', mmap_mode='r', allow_pickle=False)
    space_id = vectors_manifest['space_id']
    def vectors():
        for row in read_jsonl(Path(bundle) / 'vectors/rows.jsonl.gz'):
            blob = np.asarray(matrix[row['row']], dtype='<f4').tobytes()
            if hashlib.sha256(blob).hexdigest() != row['vector_sha256']: raise Refused(f"{row['source_id']}: vector checksum mismatch")
            yield (generation_id, row['source_kind'], row['source_id'], space_id, row['input_sha256'], row['input_text'], blob, row['vector_sha256'])
    def projections():
        for row in iter_tsv(Path(bundle) / 'projections.tsv.gz', PROJECTION_COLUMNS):
            yield (generation_id, row['scope'], row['factor_key'], row['gene_set_id'], float(row['joint_loading']), float(row['marginal_loading']),
                   row['joint_loading'], row['marginal_loading'], int(row['joint_rank']), int(row['marginal_rank']), int(row['is_joint_top_factor']))
    b = Path(bundle)
    return [
        ('kpn_traits', ('generation_id', 'kpn_trait_id', 'legacy_phenotype_id', 'phenotype_name', 'gwas_source_category', 'trait_group',
                        'legacy_trait_group', 'trait_type', 'description', 'is_dichotomous', 'is_complex', 'n_factors', 'metadata'),
         lambda: ((generation_id, r['kpn_trait_id'], r['legacy_phenotype_id'], r['phenotype_name'], r['gwas_source_category'], r['trait_group'],
                   r['legacy_trait_group'], r['trait_type'], r['description'], r['is_dichotomous'], r['is_complex'], r['n_factors'],
                   canonical(r['metadata'])) for r in read_jsonl(b / 'kpn_traits.jsonl')), 1000),
        ('reference_factors', ('generation_id', 'factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'factor_number', 'label',
                               'eaggl_import_id', 'input_sha256', 'source_revision', 'metadata'),
         lambda: ((generation_id, r['factor_key'], r['public_id'], r['eaggl_factor_id'], r['kpn_trait_id'], r['factor_number'], r['label'],
                   eaggl_import_id, r['input_sha256'], r['source_revision'], canonical(r['metadata'])) for r in read_jsonl(b / 'reference_factors.jsonl.gz')), 1000),
        ('cfde_gene_set_collections', ('generation_id', 'collection_id', 'cfde_label', 'library', 'n_sets', 'payload'),
         lambda: ((generation_id, r['collection_id'], r['cfde_label'], r['library'], r['n_sets'], canonical(r['payload']))
                  for r in read_jsonl(b / 'cfde_collections.jsonl')), 20),
        ('cfde_gene_sets', ('generation_id', 'gene_set_id', 'collection_id', 'gene_set_name', 'library', 'n_genes', 'n_genes_in_eaggl_universe',
                            'legacy_source_key', 'metadata'),
         lambda: ((generation_id, r['gene_set_id'], r['collection_id'], r['gene_set_name'], r['library'], r['n_genes'], r['n_genes_in_eaggl_universe'],
                   r['legacy_source_key'], canonical(r['metadata'])) for r in read_jsonl(b / 'cfde_gene_sets.jsonl.gz')), 500),
        ('factor_gene_set_projections', ('generation_id', 'scope', 'factor_key', 'gene_set_id', 'joint_loading', 'marginal_loading',
                                         'joint_loading_text', 'marginal_loading_text', 'joint_rank', 'marginal_rank', 'is_joint_top_factor'),
         projections, 5000),
        ('reference_vectors', ('generation_id', 'source_kind', 'source_id', 'space_id', 'input_sha256', 'input_text', 'vector', 'vector_sha256'),
         vectors, 500)]


def ensure_space(connection, space):
    columns = ('space_id', 'model', 'model_revision', 'provider', 'service_url_sha256', 'dimensions', 'metric', 'normalization')
    rows = query(connection, 'SELECT ' + ','.join(columns) + ' FROM embedding_spaces WHERE space_id=%s', (space['space_id'],))
    if rows:
        if tuple(rows[0]) != tuple(space[key] for key in columns): raise Refused('embedding_spaces row conflicts with this bundle')
        return False
    with connection.cursor() as cursor:
        cursor.execute('INSERT INTO embedding_spaces(' + ','.join(columns) + ',calibration) VALUES (' + _placeholders(columns) + ',%s)',
                       (*(space[key] for key in columns), canonical(space['calibration'])))
    connection.commit()
    return True


READBACK_SAMPLE = 64
# (table, bundle file, key, text columns compared exactly, JSON columns compared by parsed value)
READBACK_TABLES = (('cfde_gene_sets', 'cfde_gene_sets.jsonl.gz', 'gene_set_id', ('gene_set_name',), ('metadata',)),
                   ('reference_factors', 'reference_factors.jsonl.gz', 'factor_key', ('public_id', 'label', 'kpn_trait_id'), ('metadata',)))


def _json_value(value): return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def readback(connection, bundle, manifest, vectors, generation_id):
    """Loaded rows read back before the generation is marked complete: vector bytes and checksums,
    per-factor projection counts and a deterministic sample of text and JSON columns.

    MySQL JSON reorders keys and whitespace, so JSON columns compare as parsed values.
    """
    bad = scalar(connection, 'SELECT COUNT(*) FROM reference_vectors WHERE generation_id=%s AND (LENGTH(vector)<>%s OR '
                             'LOWER(SHA2(vector,256))<>vector_sha256 OR LOWER(SHA2(input_text,256))<>input_sha256)',
                 (generation_id, int(vectors['dimensions']) * 4))
    if bad: raise Refused(f'Read-back: {bad} reference vectors differ from their length or checksums')
    expected = {}
    for row in iter_tsv(Path(bundle) / 'projections.tsv.gz', PROJECTION_COLUMNS): expected[row['factor_key']] = expected.get(row['factor_key'], 0) + 1
    stored = {key: int(count) for key, count in query(
        connection, 'SELECT factor_key,COUNT(*) FROM factor_gene_set_projections WHERE generation_id=%s GROUP BY factor_key', (generation_id,))}
    wrong = sorted(key for key in set(expected) | set(stored) if expected.get(key) != stored.get(key))
    if wrong: raise Refused(f'Read-back: projection counts differ for {len(wrong)} factors, e.g. {wrong[:3]}')
    sampled = {}
    for table, name, key, texts, documents in READBACK_TABLES:
        step = max(1, -(-int(manifest['counts'][table]) // READBACK_SAMPLE))
        with _open_text(Path(bundle) / name) as stream:
            rows = [json.loads(line) for line in itertools.islice(stream, 0, None, step)][:READBACK_SAMPLE]
        keys = [row[key] for row in rows]
        found = {row[0]: row[1:] for row in query(connection, f"SELECT {key},{','.join(texts + documents)} FROM {table} "
                                                              f'WHERE generation_id=%s AND {key} IN ({_placeholders(keys)})', (generation_id, *keys))} if keys else {}
        differ = [row[key] for row in rows if row[key] not in found
                  or [*found[row[key]][:len(texts)], *map(_json_value, found[row[key]][len(texts):])] != [row[column] for column in texts + documents]]
        if differ: raise Refused(f'Read-back: {len(differ)} sampled {table} rows differ from the bundle, e.g. {differ[:3]}')
        sampled[table] = len(rows)
    return {'vectors_checked': True, 'projection_factors': len(stored), 'sampled': sampled}


def load_bundle(services, bundle, *, apply, eaggl_import_id=None, eaggl_source_version=None, eaggl_embedding_run_id=None):
    targets = load_targets(services.targets_file)
    check_database(services, targets.values())
    bundle = Path(bundle)
    manifest = open_bundle(bundle)
    vectors = open_vectors(bundle)
    if not vectors: raise Refused(f'{bundle}: run `embed --bundle {bundle}` first')
    space = read_json(bundle / 'vectors/embedding_space.json')
    generation_id = manifest['generation_id']
    expected = {**manifest['counts'], 'reference_vectors': vectors['count']}
    report = {'generation_id': generation_id, 'apply': apply, 'expected': expected, 'space_id': space['space_id']}
    factors = list(read_jsonl(bundle / 'reference_factors.jsonl.gz'))
    connection, locked = services.connect(), False
    try:
        # Before any write: the EAGGL source and the embedding space its factor vectors live in.
        source = eaggl_source(services, connection, factors, import_id=eaggl_import_id, source_version=eaggl_source_version, run_id=eaggl_embedding_run_id)
        report['embedding_space_matches_run'] = check_embedding_space(connection, space, source['eaggl_embedding_run_id'], bundle)
        # register_legacy selects by the REVEAL_* pins alone: refuse here, not after the migration, when they disagree.
        served = legacy_sources(services, connection)
        if served and (served['eaggl_import_id'], served['eaggl_embedding_run_id']) != (source['eaggl_import_id'], source['eaggl_embedding_run_id']):
            raise Refused(f"Stop: the served mapping run {served['mapping_run_id'][:12]} uses EAGGL import {served['eaggl_import_id'][:12]} and embedding "
                          f"run {served['eaggl_embedding_run_id'][:12]} (REVEAL_EMBEDDING_RUN_ID), not the selected import {source['eaggl_import_id'][:12]} "
                          f"and run {source['eaggl_embedding_run_id'][:12]}: pin both to the served ones. {EAGGL_STOP}")
        if not apply:
            report['migration_applied'] = table_exists(connection, 'reference_generations')
            existing = rg.get_generation(connection, generation_id) if report['migration_applied'] else None
            report['existing_status'] = existing['status'] if existing else None
            report['eaggl'] = source
            return report
        rg.migrate(connection)
        with connection.cursor() as cursor:  # executemany may split statements: truncation fails in every one
            cursor.execute("SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')")
        if scalar(connection, 'SELECT GET_LOCK(%s,%s)', (LOCK_NAME, 0)) != 1: raise Refused('Another reference reload holds the global lock')
        locked = True
        report['legacy_generation_id'] = register_legacy(services, connection)
        dismech = select_dismech_import(services, connection)
        report['eaggl'], report['dismech_import_id'] = source, dismech
        existing = rg.get_generation(connection, generation_id)
        if existing and existing['status'] in ('complete', 'superseded'):
            counts = generation_counts(connection, generation_id)
            if counts != expected: raise Refused(f'Generation {generation_id[:12]} is {existing["status"]} but its counts differ: {counts}')
            return {**report, 'status': existing['status'], 'counts': counts, 'reused': True}
        if existing and existing['status'] == 'retired': raise Refused(f'Generation {generation_id[:12]} was retired; rebuild instead of reloading it')
        stored = {**manifest, 'vectors': {key: vectors[key] for key in ('space_id', 'count', 'dimensions', 'files')}, 'eaggl': source}
        if existing:  # resume a loading/failed attempt from scratch: only this generation's rows
            for table in GENERATION_TABLES: delete_batches(connection, table, 'generation_id=%s', (generation_id,))
            with connection.cursor() as cursor:
                cursor.execute("UPDATE reference_generations SET status='loading',eaggl_import_id=%s,eaggl_embedding_run_id=%s,dismech_import_id=%s,"
                               'manifest=%s WHERE generation_id=%s', (source['eaggl_import_id'], source['eaggl_embedding_run_id'], dismech,
                                                                     canonical(stored), generation_id))
            connection.commit()
        else:
            with connection.cursor() as cursor:
                cursor.execute('INSERT INTO reference_generations(generation_id,kind,model,status,eaggl_import_id,eaggl_embedding_run_id,'
                               "dismech_import_id,manifest) VALUES (%s,%s,%s,'loading',%s,%s,%s,%s)",
                               (generation_id, rg.KPN_KIND, rg.KPN_MODEL, source['eaggl_import_id'], source['eaggl_embedding_run_id'], dismech, canonical(stored)))
            connection.commit()
        try:
            report['space_created'] = ensure_space(connection, space)
            loaded = {}
            for table, columns, rows, batch_size in _bundle_rows(bundle, vectors, generation_id, source['eaggl_import_id']):
                emit(services, f'Loading {table}')
                loaded[table] = insert_rows(connection, table, columns, rows(), batch_size)
            counts = generation_counts(connection, generation_id)
            if counts != expected: raise Refused(f'Loaded counts {counts} differ from the bundle {expected}')
            orphans = scalar(connection, 'SELECT COUNT(*) FROM reference_factors f LEFT JOIN kpn_traits t ON t.generation_id=f.generation_id '
                                         'AND t.kpn_trait_id=f.kpn_trait_id WHERE f.generation_id=%s AND t.kpn_trait_id IS NULL', (generation_id,))
            if orphans: raise Refused(f'{orphans} factors lack their KPN trait')
            report['readback'] = readback(connection, bundle, manifest, vectors, generation_id)
            rg.set_generation_status(connection, generation_id, 'complete', expected=('loading',))
        except BaseException:
            try:
                connection.rollback(); rg.set_generation_status(connection, generation_id, 'failed', expected=('loading', 'failed'))
            except Exception as error: emit(services, f'Could not mark the generation failed ({type(error).__name__})')
            raise
        return {**report, 'status': 'complete', 'counts': counts, 'reused': False}
    finally:
        try:
            if locked: scalar(connection, 'SELECT RELEASE_LOCK(%s)', (LOCK_NAME,))
        finally: connection.close()


# --------------------------------------------------------------------------------------
# abandon: remove a KPN generation that never served (and the then-empty 008 schema)


def _abandon_refusals(services, connection, targets, generation_id):
    """(generation, reasons it cannot be abandoned)."""
    if not table_exists(connection, 'reference_generations'): return None, ['migration 008 is not applied: there is no generation to abandon']
    generation = rg.get_generation(connection, generation_id)
    if not generation: return None, [f'Unknown reference generation {generation_id}']
    refusals = []
    if generation['kind'] != rg.KPN_KIND: refusals.append(f"generation kind is {generation['kind']}, not {rg.KPN_KIND}")
    if generation['status'] not in ABANDON_STATUSES: refusals.append(f"generation is {generation['status']}, not one of {list(ABANDON_STATUSES)}")
    for prefix in allow_listed_prefixes(targets):
        if not table_exists(connection, f'{prefix}_records'): continue
        with services.repository(prefix).read_transaction() as tx: active = rg.read_active(tx)
        if (active or {}).get('generation_id') == generation_id: refusals.append(f'{prefix} serves this generation (reference_active)')
    for table in ('vector_bindings', 'archived_reference_factors'):
        rows = scalar(connection, f'SELECT COUNT(*) FROM {table} WHERE generation_id=%s', (generation_id,)) if table_exists(connection, table) else 0
        if rows: refusals.append(f'{table} holds {rows} rows of this generation')
    return generation, refusals


def _schema_refusals(services, connection, targets, *, pending=None):
    """(existing migration-008 tables, reasons not to drop them): rows other than legacy registrations
    (less `pending` deletes) or reload records in any allow-listed prefix."""
    pending, reasons = pending or {}, []
    present = [table for table in REFERENCE_TABLES if table_exists(connection, table)]
    for table in present:
        legacy = table == 'reference_generations'  # legacy registrations are re-created by the next `load --apply`
        sql, params = (f'SELECT COUNT(*) FROM {table} WHERE kind<>%s', (rg.LEGACY_KIND,)) if legacy else (f'SELECT COUNT(*) FROM {table}', ())
        rows = (scalar(connection, sql, params) or 0) - pending.get(table, 0)
        if rows > 0: reasons.append(f'{table} holds {rows} rows' + (' that are not legacy registrations' if legacy else ''))
    kinds = sorted(BOOKKEEPING_KINDS | {rg.ACTIVE_KIND})
    for prefix in allow_listed_prefixes(targets):
        if not table_exists(connection, f'{prefix}_records'): continue
        with services.repository(prefix).read_transaction() as tx:
            held = tx.execute(f'SELECT kind,COUNT(*) FROM reveal_records WHERE kind IN ({_placeholders(kinds)}) GROUP BY kind', tuple(kinds)).fetchall()
        if held: reasons.append(f'{prefix}_records holds reload records {dict(sorted((kind, int(n)) for kind, n in held))}')
    return present, reasons


def abandon_generation(services, generation_id, *, apply, drop_empty_schema=False):
    """Delete a KPN generation that never served: its rows child-first, its embedding space when no
    other generation uses it, then its reference_generations row. Dry run unless --apply, which is
    interactive (typed generation id) and holds the reload lock."""
    if not rg.GENERATION_RE.fullmatch(generation_id or ''): raise Refused('--generation must be a 64-character generation id')
    targets = load_targets(services.targets_file)
    database = check_database(services, targets.values())
    if apply and not services.interactive(): raise Refused('abandon --apply is interactive: run it by hand in a terminal')
    report = {'generation_id': generation_id, 'apply': apply, 'database': database}
    connection, locked = services.connect(), False
    try:
        if apply:
            if scalar(connection, 'SELECT GET_LOCK(%s,%s)', (LOCK_NAME, 0)) != 1: raise Refused('Another reference reload holds the global lock')
            locked = True
        generation, refusals = _abandon_refusals(services, connection, targets, generation_id)
        space_id = (((generation or {}).get('manifest') or {}).get('vectors') or {}).get('space_id')
        rows = {table: scalar(connection, f'SELECT COUNT(*) FROM {table} WHERE generation_id=%s', (generation_id,)) or 0
                for table in GENERATION_TABLES} if generation else {}
        shared = scalar(connection, 'SELECT COUNT(*) FROM reference_vectors WHERE space_id=%s AND generation_id<>%s', (space_id, generation_id)) if space_id else None
        report.update(kind=(generation or {}).get('kind'), status=(generation or {}).get('status'), rows=rows,
                      embedding_space={'space_id': space_id, 'delete': bool(space_id) and not shared}, refusals=refusals)
        if not apply:
            if drop_empty_schema:
                pending = {} if refusals or not generation else {**rows, 'reference_generations': 1, 'embedding_spaces': int(report['embedding_space']['delete'])}
                present, reasons = _schema_refusals(services, connection, targets, pending=pending)
                report['drop_schema'] = {'would_drop': [] if reasons else present, 'reasons': reasons}
            return {**report, 'ok': not refusals}
        if refusals: raise Refused(f'Cannot abandon {generation_id[:12]}: ' + '; '.join(refusals))
        phrase = generation_id[:12]
        emit(services, json.dumps({key: report[key] for key in ('generation_id', 'kind', 'status', 'rows', 'embedding_space')}, indent=2, sort_keys=True))
        if (services.input(f'Type {phrase} to delete this generation: ') or '').strip() != phrase:
            raise Refused('Confirmation did not match; nothing was deleted')
        # Nothing changed while waiting: end the transaction first, or REPEATABLE READ re-reads the snapshot taken before
        # the prompt (the named lock is per session and survives it; snapshot and capture --apply take it too).
        connection.rollback()
        generation, refusals = _abandon_refusals(services, connection, targets, generation_id)
        if refusals: raise Refused(f'Cannot abandon {generation_id[:12]}: ' + '; '.join(refusals))
        deleted = {}
        for table in GENERATION_TABLES:  # child first
            emit(services, f'abandon: {table}')
            deleted[table] = delete_batches(connection, table, 'generation_id=%s', (generation_id,))
        in_use = scalar(connection, 'SELECT COUNT(*) FROM reference_vectors WHERE space_id=%s', (space_id,)) if space_id else None
        deleted['embedding_spaces'] = delete_abandoned_row(connection, 'embedding_spaces', space_id) if space_id and not in_use else 0
        deleted['reference_generations'] = delete_abandoned_row(connection, 'reference_generations', generation_id)
        if deleted['reference_generations'] != 1: raise Refused(f'The reference_generations row of {phrase} changed during abandon; inspect it')
        report['deleted'] = deleted
        if drop_empty_schema:
            present, reasons = _schema_refusals(services, connection, targets)
            dropped = []
            for table in [] if reasons else present:  # REFERENCE_TABLES order: children first
                emit(services, f'abandon: DROP TABLE {table}')
                with connection.cursor() as cursor: cursor.execute(f'DROP TABLE {table}')
                dropped.append(table)
            report['drop_schema'] = {'dropped': dropped, 'reasons': reasons}
        return {**report, 'ok': not (report.get('drop_schema') or {}).get('reasons')}
    finally:
        try:
            if locked: scalar(connection, 'SELECT RELEASE_LOCK(%s)', (LOCK_NAME,))
        finally: connection.close()


# --------------------------------------------------------------------------------------
# capture: freeze referenced factors, backfill anchor display, cold export


KPN_EXPORT = (('kpn_traits', 'generation_id=%s', 'generation_id', 'kpn_trait_id'),
              ('reference_factors', 'generation_id=%s', 'generation_id', 'factor_key'),
              ('cfde_gene_set_collections', 'generation_id=%s', 'generation_id', 'collection_id'),
              ('cfde_gene_sets', 'generation_id=%s', 'generation_id', 'gene_set_id'),
              ('factor_gene_set_projections', 'generation_id=%s', 'generation_id', 'scope,factor_key,gene_set_id'),
              ('reference_vectors', 'generation_id=%s', 'generation_id', 'source_kind,source_id'),
              ('embedding_spaces', 'space_id IN (SELECT DISTINCT space_id FROM reference_vectors WHERE generation_id=%s)', 'generation_id', 'space_id'))
LEGACY_EXPORT = (('eaggl_cfde_link_runs', 'run_id=%s', 'legacy_mapping_run_id', 'run_id'),
                 ('eaggl_cfde_factor_links', 'run_id=%s', 'legacy_mapping_run_id', 'factor_index'),
                 ('eaggl_cfde_gene_set_links', 'run_id=%s', 'legacy_mapping_run_id', 'factor_index,gene_set_rank'),
                 ('gene_set_imports', 'import_id=%s', 'legacy_gene_set_import_id', 'import_id'),
                 ('cfde_gene_set_aliases', 'import_id=%s', 'legacy_gene_set_import_id', 'node_id_sha256'),
                 # purge step 4 also deletes every Activity row and GeneSet rows no alias names.
                 ('dapper_objects', "(id IN (SELECT dapper_id FROM cfde_gene_set_aliases WHERE import_id=%s) OR class_name='Activity' OR "
                                    "(class_name='GeneSet' AND NOT EXISTS (SELECT 1 FROM cfde_gene_set_aliases a WHERE a.dapper_id=dapper_objects.id)))",
                  'legacy_gene_set_import_id', 'id'))
# purge steps 2-3 delete every embedding run of a retired EAGGL import and the DisMech context runs bound to them.
RUNS_OF_IMPORT = 'SELECT run_id FROM eaggl_embedding_runs WHERE import_id=%s'
CONTEXT_RUNS_OF_IMPORT = f'SELECT run_id FROM dismech_embedding_runs WHERE eaggl_embedding_run_id IN ({RUNS_OF_IMPORT})'
EAGGL_EXPORT = (('eaggl_imports', 'import_id=%s', 'eaggl_import_id', 'import_id'),
                ('eaggl_genes', 'import_id=%s', 'eaggl_import_id', 'gene_index'),
                ('eaggl_factors', 'import_id=%s', 'eaggl_import_id', 'factor_index'),
                ('eaggl_gene_loadings', 'import_id=%s', 'eaggl_import_id', 'factor_index,gene_index'),
                ('eaggl_graph_nodes', 'import_id=%s', 'eaggl_import_id', 'node_index'),
                ('eaggl_graph_edges', 'import_id=%s', 'eaggl_import_id', 'parent_index,child_index'),
                ('eaggl_embedding_runs', 'import_id=%s', 'eaggl_import_id', 'run_id'),
                ('eaggl_name_embeddings', f'run_id IN ({RUNS_OF_IMPORT})', 'eaggl_import_id', 'run_id,input_sha256'),
                ('dismech_embedding_runs', f'eaggl_embedding_run_id IN ({RUNS_OF_IMPORT})', 'eaggl_import_id', 'run_id'),
                ('dismech_embedding_vectors', f'run_id IN ({CONTEXT_RUNS_OF_IMPORT})', 'eaggl_import_id', 'run_id,input_sha256'),
                ('dismech_embedding_inputs', f'run_id IN ({CONTEXT_RUNS_OF_IMPORT})', 'eaggl_import_id', 'run_id,source_id_sha256'))


def _cell(value):
    if isinstance(value, (bytes, bytearray, memoryview)): return {'$base64': base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value, (datetime, date)): return value.isoformat()
    if isinstance(value, Decimal): return str(value)
    return value


def cold_export(services, connection, generation, *, directory):
    """Content-addressed gzip JSONL parts of every shared-table row of one generation, plus a root index.

    It covers every row purge-retired deletes when the generation retires (docs §7 delete order).
    """
    specs = ((('reference_generations', 'generation_id=%s', 'generation_id', 'generation_id'),)
             + (LEGACY_EXPORT if generation['kind'] == rg.LEGACY_KIND else KPN_EXPORT) + EAGGL_EXPORT)
    tables = {}
    for table, where, key, order in specs:
        if not generation.get(key): continue
        parts, total, offset, buffer = [], 0, 0, bytearray()
        while True:
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s', (generation[key], EXPORT_PAGE_ROWS, offset))
                columns = [column[0] for column in cursor.description or ()]
                rows = cursor.fetchall()
            for row in rows:
                buffer += (canonical({'table': table, 'row': dict(zip(columns, map(_cell, row)))}) + '\n').encode()
                if len(buffer) >= EXPORT_PART_BYTES:
                    parts.append(services.put_artifact(gzip.compress(bytes(buffer), mtime=0), directory)); buffer = bytearray()
            total += len(rows); offset += len(rows)
            if len(rows) < EXPORT_PAGE_ROWS: break
        if buffer: parts.append(services.put_artifact(gzip.compress(bytes(buffer), mtime=0), directory))
        tables[table] = {'rows': total, 'parts': parts}
        emit(services, f'Exported {total} rows of {table}')
    root = {'format': EXPORT_FORMAT, 'generation_id': generation['generation_id'], 'kind': generation['kind'], 'model': generation['model'],
            'tables': tables}
    ref = services.put_artifact(gzip.compress((canonical(root) + '\n').encode(), mtime=0), directory)
    return {'format': EXPORT_FORMAT, 'root': ref, 'rows': {table: item['rows'] for table, item in tables.items()}}


def export_current(services, ref):
    """A cold export of the current format whose root index and every listed part exist unchanged."""
    if not ref or ref.get('format') != EXPORT_FORMAT or not ref.get('root') or not services.artifact_exists(ref['root']): return False
    root = json.loads(gzip.decompress(services.read_artifact(ref['root'])))
    return all(services.artifact_exists(part) for table in (root.get('tables') or {}).values() for part in table.get('parts') or [])


def allow_listed_prefixes(targets): return sorted({target['prefix'] for target in targets.values()})


def active_source(services, prefixes):
    """`capture --from active`: the one generation these prefixes serve now.

    Legacy-mode prefixes serve the registered legacy generation. Refuses when the prefixes
    serve different generations (capture each group with an explicit --from).
    """
    actives = {}
    for prefix in prefixes:
        with services.repository(prefix).read_transaction() as tx: active = rg.read_active(tx)
        actives[prefix] = active['generation_id'] if active else None
    if None in actives.values():
        connection = services.connect()
        try: legacy = legacy_generation(services, rg.list_generations(connection))
        finally: connection.close()
        if not legacy: raise Refused('Legacy-mode prefixes serve no registered legacy generation; run `load --apply` first')
        actives = {prefix: generation or legacy for prefix, generation in actives.items()}
    if len(set(actives.values())) != 1:
        raise Refused(f'The prefixes serve different generations {actives}; capture each with --from <generation> --prefixes <prefixes>')
    return next(iter(actives.values()))


def capture_generation(services, generation_id, prefixes, *, apply, cold=False, export_dir=None, dapper=DEFAULT_DAPPER, connection=None, locked=False):
    archive = services.module('reference_archive')
    own, holds = connection is None, False
    connection = connection or services.connect()
    try:
        if apply and not locked:  # the reload lock, unless the caller (apply) holds it: abandon never deletes under a capture
            if scalar(connection, 'SELECT GET_LOCK(%s,%s)', (LOCK_NAME, 0)) != 1: raise Refused('Another reference reload holds the global lock')
            holds = True
        generation = rg.get_generation(connection, generation_id)
        if not generation: raise Refused(f'Unknown reference generation {generation_id}')
        sources, per_prefix = set(), {}
        for prefix in prefixes:
            referenced = set(archive.referenced_sources(services.repository(prefix)).get(generation_id, ()))
            per_prefix[prefix] = len(referenced); sources |= referenced
        runtime = None
        if sources and dapper and Path(dapper).exists():
            try: runtime = services.dapper_runtime(dapper)
            except Exception as error: emit(services, f'DAPPER runtime unavailable ({type(error).__name__}); mechanism ids are not recomputed')
        rows = archive.capture_factors(connection, generation, sorted(sources), runtime=runtime) if sources else []
        captured = {row.get('source_id') for row in rows}
        report = {'generation_id': generation_id, 'apply': apply, 'referenced': per_prefix, 'sources': len(sources),
                  'captured': len(rows), 'unresolved': sorted(sources - captured)[:50],
                  'written': archive.write_archived_factors(connection, rows) if apply and rows else 0,
                  'anchor_display': {prefix: archive.backfill_anchor_display(services.repository(prefix), apply=apply) for prefix in prefixes}}
        if cold:
            if export_current(services, generation.get('cold_export_ref')):
                report['cold_export'] = {**generation['cold_export_ref'], 'reused': True}
            elif apply:
                ref = cold_export(services, connection, generation, directory=export_dir or ROOT / '.runtime/reference-exports')
                with connection.cursor() as cursor:
                    cursor.execute('UPDATE reference_generations SET cold_export_ref=%s WHERE generation_id=%s', (canonical(ref), generation_id))
                connection.commit()
                report['cold_export'] = ref
            else: report['cold_export'] = 'pending (--apply)'
        return report
    finally:
        try:
            if holds: scalar(connection, 'SELECT RELEASE_LOCK(%s)', (LOCK_NAME,))
        finally:
            if own: connection.close()


# --------------------------------------------------------------------------------------
# snapshot: per-target Upstash snapshot of a generation (never activated here)


def _json_field(tx, path):
    return f"json_extract(payload,'$.{path}')" if tx.sqlite else f"JSON_UNQUOTE(JSON_EXTRACT(payload,'$.{path}'))"


def generation_snapshots(tx, environment, generation_id):
    rows = tx.execute('SELECT id,' + ','.join(_json_field(tx, path) for path in ('status', 'environment', 'reference_generation_id')) +
                      ' FROM reveal_records WHERE kind=%s ORDER BY id', ('vector_snapshot',)).fetchall()
    return [row[0] for row in rows if row[1] == 'complete' and row[2] == environment and row[3] == generation_id]


def namespaces_of(snapshot): return {key: value for key, value in sorted(snapshot.items()) if key.endswith('_namespace')}


def snapshot_generation(services, target, generation_id, *, apply, batch_size=200):
    bind_target(services, target)
    vi = services.module('vector_ingestion')
    registry = vi.VectorRegistry(services.repository(target['prefix']), environment_name=target['vector_environment'])
    connection, locked = services.connect(), False
    try:
        if apply:  # the reload lock: abandon never deletes a generation while its vector_bindings are being written
            if scalar(connection, 'SELECT GET_LOCK(%s,%s)', (LOCK_NAME, 0)) != 1: raise Refused('Another reference reload holds the global lock')
            locked = True
        generation = rg.get_generation(connection, generation_id)
        if not generation or generation['kind'] != rg.KPN_KIND or generation['status'] != 'complete':
            raise Refused(f'Generation {generation_id[:12]} is not a complete KPN generation')
        with registry.repo.read_transaction() as tx: existing = generation_snapshots(tx, target['vector_environment'], generation_id)
        if existing:
            state = registry.get(existing[0])
            return {'target': target['name'], 'snapshot_id': existing[0], 'status': 'complete', 'reused': True, 'namespaces': namespaces_of(state),
                    'bindings': vi.record_vector_bindings(connection, state) if apply else None}
        if not apply:  # the export holds every vector in memory; build it only to upload it
            return {'target': target['name'], 'generation_id': generation_id, 'apply': False, 'snapshot_id': None,
                    'note': 'no verified snapshot of this generation yet; --apply builds, uploads and verifies it'}
        manifest, exported = vi.build_generation_export(connection, generation_id, batch_size=batch_size)
        if manifest.get('environment') != target['vector_environment'] or manifest.get('reference_generation_id') != generation_id:
            raise Refused('Vector export is not for this target environment and generation')
        summary = {'target': target['name'], 'snapshot_id': manifest['snapshot_id'], 'namespaces': namespaces_of(manifest),
                   'batches': len(manifest['batches']), 'apply': apply}
        manifest = vi.save_export(manifest, exported)
        registry.register(manifest)
        client = services.vector_client(write=True)
        # Keep the reload lock through executor shutdown, including failures.
        # Consume every result before final verification or binding writes.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for _ in pool.map(lambda key: vi.import_batch(registry, manifest['snapshot_id'], key, client=client), manifest['batches']):
                pass
        report = vi.verify_snapshot(registry, manifest['snapshot_id'], client=client)
        bindings = vi.record_vector_bindings(connection, registry.get(manifest['snapshot_id']))
        return {**summary, 'status': 'complete', 'verification': report, 'bindings': bindings, 'reused': False}
    finally:
        try:
            if locked: scalar(connection, 'SELECT RELEASE_LOCK(%s)', (LOCK_NAME,))
        finally: connection.close()


# --------------------------------------------------------------------------------------
# plan / approve


def _strip(value):
    if isinstance(value, dict): return {key: _strip(item) for key, item in value.items() if key not in TIMESTAMP_KEYS}
    if isinstance(value, list): return [_strip(item) for item in value]
    return value


def plan_digest(plan):
    return digest(_strip({key: value for key, value in plan.items() if key not in ('plan_sha256', 'observed')}))


def legacy_generation(services, generations):
    pin = services.environ.get('REVEAL_MAPPING_RUN_ID')
    if pin:
        identity = rg.legacy_generation_id(pin)
        return identity if any(g['generation_id'] == identity for g in generations) else None
    legacy = [g['generation_id'] for g in generations if g['kind'] == rg.LEGACY_KIND and g['status'] in ('complete', 'superseded')]
    return legacy[0] if len(legacy) == 1 else None


def _activated(audit):
    """True for a reference_reload audit of an apply that switched the pointers (or resumed one that had)."""
    return any(step.get('step') in ('activated', 'resumed') for step in audit.get('steps') or [])


def resume_point(services, target, active, audits, generations):
    """{from_generation, interrupted_plans} of a cutover of this target to the active generation that
    never recorded completion (it failed after activation, or its verification failed), or None.

    A `complete` audit of the generation for this target means there is nothing to resume.
    """
    if any(audit.get('status') == 'complete' for audit in audits): return None
    interrupted = [audit for audit in audits if _activated(audit)]
    sources = {audit.get('from_generation') for audit in interrupted if audit.get('from_generation')}
    if len(sources) > 1: raise Refused(f'Interrupted cutovers of {target["name"]} name different source generations {sorted(sources)}')
    source = next(iter(sources), None) or active.get('previous_generation_id') or legacy_generation(services, generations)
    # Each failed resume adds its plan, so the next resume plan (and its approval) differs.
    plans = sorted({audit['plan_sha256'] for audit in interrupted if audit.get('plan_sha256')})
    return {'from_generation': source, 'interrupted_plans': plans} if source else None


def build_plan(services, target, generation_id, *, client=None):
    """Read-only apply plan for one target. Fails closed on record kinds with no action.

    When the target is already active on the generation but its cutover never completed (a
    failure after activation, or a failed verification), the plan is a resume plan: `apply`
    then reruns only the idempotent archive pass, the post-archive capture and verify.
    """
    archive, vi = services.module('reference_archive'), services.module('vector_ingestion')
    environment = target['vector_environment']
    connection = services.connect()
    try:
        generation = rg.get_generation(connection, generation_id)
        generations = rg.list_generations(connection)
    finally: connection.close()
    if not generation: raise Refused(f'Unknown reference generation {generation_id}')
    repo = services.repository(target['prefix'])
    with repo.read_transaction() as tx:
        active, gate = rg.read_active(tx), rg.read_gate(tx)
        vector_active = tx.get('vector_active', environment)
        snapshots = generation_snapshots(tx, environment, generation_id)
        audits = [row['data'] for row in tx.list(rg.RELOAD_KIND) if not row['data'].get('purge')
                  and row['data'].get('target') == target['name'] and row['data'].get('generation_id') == generation_id]
    blockers, resume = [], None
    if generation['kind'] != rg.KPN_KIND or generation['status'] not in (('complete', 'superseded') if active and active['generation_id'] == generation_id else ('complete',)):
        blockers.append(f"generation is {generation['kind']}/{generation['status']}, not a complete KPN generation")
    if active and active['generation_id'] == generation_id:
        resume = resume_point(services, target, active, audits, generations)
        if not resume: blockers.append('target is already active on this generation')
    source = resume['from_generation'] if resume else active['generation_id'] if active else legacy_generation(services, generations)
    if not source: blockers.append('the served (legacy) generation is not registered; run `load --apply` first')
    if gate: blockers.append('the reload gate is closed')
    if len(snapshots) != 1: blockers.append(f'{len(snapshots)} complete vector snapshots of this generation in {environment}; run `snapshot --apply`')
    # In legacy mode rows carry no generation of their own; the served legacy generation is assumed.
    prefix = _plain(archive.plan_prefix(repo, generation_id, **({'from_generation': source} if (not active or resume) and source else {})))
    unknown = sorted(prefix.get('unknown_kinds') or [])
    if unknown: raise Refused(f'Record kinds with no reload action in {target["prefix"]}_records: {unknown}; classify them before planning')
    counts = prefix.get('counts_by_kind') or {}
    snapshot = None
    if len(snapshots) == 1:
        state = vi.VectorRegistry(repo, environment_name=environment).get(snapshots[0], progress=False)
        snapshot = {'snapshot_id': snapshots[0], 'namespaces': namespaces_of(state), 'status': state['status'],
                    'verified': bool((state.get('verification') or {}).get('passed')),
                    'counts': {kind: len(state.get(kind) or []) for kind in ('factors', 'contexts', 'gene_sets', 'collections')}}
        if not snapshot['verified']: blockers.append('the target vector snapshot is not verified; run `snapshot --apply`')
    listed = vi.list_namespaces(client or services.vector_client())
    # The sha pins the destructive and assumed decisions (drafts dropped, rows archived by assumption,
    # anchor display backfill). Stamping is idempotent and jobs are re-evaluated under the gate, so row
    # counts, archive candidates and job states are observed only: normal activity must not drift the plan.
    plan = {'format': PLAN_FORMAT, 'kind': 'apply', 'target': target, 'generation_id': generation_id, 'model': generation['model'],
            'from_generation': source, 'active': {'reference': active, 'vector': vector_active['data'] if vector_active else None}, 'gate': gate,
            'archive': {key: prefix.get(key) for key in ('anchored_drafts', 'unresolved', 'anchor_display_backfill')},
            'namespaces': {name: count for name, count in sorted(listed.items()) if name.startswith(environment + '-')},
            'snapshot': snapshot, 'blockers': blockers, **({'resume': resume} if resume else {}),
            'observed': {'counts_by_kind': dict(sorted(counts.items())),
                         'archive_counts_by_kind': {kind: n for kind, n in sorted(counts.items()) if kind not in BOOKKEEPING_KINDS and archive.classify(kind) != 'keep'},
                         'archive_candidates': prefix.get('archive_candidates'), 'nonterminal_jobs': prefix.get('nonterminal_jobs')},
            'planned_at': services.clock()}
    plan['plan_sha256'] = plan_digest(plan)
    return plan


def read_plan(path):
    plan = read_json(path)
    if plan.get('format') not in (PLAN_FORMAT, PURGE_FORMAT) or plan.get('plan_sha256') != plan_digest(plan):
        raise Refused(f'{path}: not a valid plan or its sha does not match its content')
    return plan


def check_approval(plan, approval):
    if (approval.get('format') != APPROVAL_FORMAT or approval.get('plan_sha256') != plan['plan_sha256']
            or approval.get('target') != plan['target']['name'] or approval.get('plan_kind') != plan['kind']
            or bool(approval.get('production')) != plan_production(plan)):
        raise Refused('The approval does not match this plan')


def _summary(plan):
    if plan['kind'] == 'purge':
        return {'generation_id': plan['generation_id'], 'retired': plan['retired'], 'targets': plan['target']['targets'],
                'deletes': [f"{step['step']}. {step['table']}: {step['rows']}" for step in plan['deletes']],
                'records': (plan.get('observed') or {}).get('records'), 'namespaces': plan['namespace_deletes']}
    archive, observed = plan['archive'], plan.get('observed') or {}
    candidates = observed.get('archive_candidates')
    return {'target': plan['target']['name'], 'prefix': plan['target']['prefix'], 'from': plan['from_generation'], 'to': plan['generation_id'],
            'resume': plan.get('resume'), 'snapshot': (plan['snapshot'] or {}).get('snapshot_id'), 'archive_candidates': _size(candidates),
            'archive_candidates_by_kind': {kind: _size(rows) for kind, rows in candidates.items()} if isinstance(candidates, dict) else None,
            'anchored_drafts_dropped': _size(archive.get('anchored_drafts')), 'nonterminal_jobs': _size(observed.get('nonterminal_jobs')),
            'unresolved': _size(archive.get('unresolved')), 'counts_by_kind': observed.get('archive_counts_by_kind')}


def approve_plan(services, plan_path, *, out=None, allow_production=False, approver=None):
    plan = read_plan(plan_path)
    if plan['blockers']: raise Refused(f"The plan has blockers: {plan['blockers']}")
    production = check_production(services, plan, allow_production)
    if not services.interactive(): raise Refused('approve is interactive: run it by hand in a terminal')
    phrase = f"{plan['target']['name']}:{plan['plan_sha256'][:12]}"
    emit(services, json.dumps(_summary(plan), indent=2, sort_keys=True, default=str))
    typed = services.input(f'Type {phrase} to approve this plan: ')
    if (typed or '').strip() != phrase: raise Refused('Confirmation did not match; nothing was approved')
    approval = {'format': APPROVAL_FORMAT, 'plan_sha256': plan['plan_sha256'], 'plan_kind': plan['kind'], 'target': plan['target']['name'],
                'generation_id': plan['generation_id'], 'production': production, 'approved_at': services.clock(),
                'approver': approver or services.environ.get('USER') or services.environ.get('LOGNAME') or 'unknown'}
    path = write_json(out or Path(plan_path).with_name(f"approval.{plan['target']['name']}.json"), approval, private=True)
    return {'approval': str(path), 'plan_sha256': plan['plan_sha256'], 'target': plan['target']['name'], 'production': production}


# --------------------------------------------------------------------------------------
# apply


def open_jobs(repo, generation_id):
    """Non-terminal analysis jobs whose frozen anchors belong to the generation (or cannot be resolved)."""
    from .jobs import TERMINAL
    with repo.read_transaction() as tx:
        jobs = [row['data'] for row in tx.list('job') if row['data'].get('kind') == 'analysis' and row['data'].get('status') not in TERMINAL]
        bindings = tx.get_many('request_binding', sorted({job['research_request_id'] for job in jobs if job.get('research_request_id')}))
    result = []
    for job in jobs:
        binding = bindings.get(job.get('research_request_id'))
        try: generation = rg.generation_of_anchors(binding['data'].get('anchors')) if binding else None
        except rg.ReferenceError: generation = None
        if generation in (generation_id, None): result.append({'id': job['id'], 'status': job['status'], 'generation_id': generation})
    return result


def drain_jobs(services, repo, generation_id, *, cancel_active, drain_seconds, poll_seconds=5):
    from .jobs import cancel
    archive = services.module('reference_archive')
    cancelled = list(archive.cancel_nonterminal_jobs(repo, generation_id, apply=True))
    remaining = [job for job in open_jobs(repo, generation_id) if job['status'] != 'cancel_requested']
    stopped = []
    if remaining and cancel_active:
        with repo.transaction() as tx:
            for job in remaining:
                row = tx.get('job', job['id'])
                # The row owner, not the payload's owner_user_id: a workspace transfer moves only owner_id.
                if row: cancel(tx, dict(row['data'], owner_user_id=row['owner'])); stopped.append(job['id'])
    waited = 0
    while True:
        remaining = [job for job in open_jobs(repo, generation_id) if job['status'] != 'cancel_requested']
        if not remaining or waited >= drain_seconds: break
        services.sleep(poll_seconds); waited += poll_seconds
    if remaining:
        raise Refused(f'{len(remaining)} analysis jobs of the superseded generation are still running after {waited}s; '
                      'wait longer (--drain-seconds) or stop them (--cancel-active)')
    return {'cancelled': cancelled, 'stopped': stopped, 'waited_seconds': waited}


def mysqldump(services, target, backup_dir, stamp, *, preflight=False):
    """mysqldump of the target's records. `preflight` dumps only the schema (--no-data) to a file it
    removes: the same binary, TLS and GTID flags, credentials and table access, before anything changes."""
    env = services.environ
    password = env.get('REVEAL_MYSQL_PASSWORD')
    if not password: raise Refused('REVEAL_MYSQL_PASSWORD is required for the records backup')
    backup_dir = Path(backup_dir); backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"{target['prefix']}_records.{stamp}{'.preflight' if preflight else ''}.sql"
    command = [env.get('REVEAL_MYSQLDUMP') or 'mysqldump', '--single-transaction', '--skip-lock-tables', '--no-tablespaces', '--hex-blob',
               '--set-gtid-purged=OFF',
               f"--host={env.get('REVEAL_MYSQL_HOST') or 'aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com'}",
               f"--port={env.get('REVEAL_MYSQL_PORT') or '3306'}", f"--user={env.get('REVEAL_MYSQL_USER') or 'cyaka'}", '--ssl-mode=VERIFY_IDENTITY']
    if env.get('REVEAL_MYSQL_CA_FILE'): command.append(f"--ssl-ca={env['REVEAL_MYSQL_CA_FILE']}")
    if preflight: command.append('--no-data')
    command += [target['database'], f"{target['prefix']}_records", f"{target['prefix']}_transaction_lock"]
    child = {key: env[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ') if env.get(key)}
    child['MYSQL_PWD'] = password  # never on the command line
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as out:
            try: result = services.run(command, stdout=out, stderr=subprocess.PIPE, env=child, check=False)
            except OSError as error: raise Refused(f'mysqldump could not run ({type(error).__name__}: {error}); install it or set REVEAL_MYSQLDUMP') from error
        tail = path.read_bytes()[-512:] if path.exists() else b''
        if result.returncode != 0 or b'-- Dump completed' not in tail:
            message = (result.stderr or b'')[-300:].decode('utf-8', 'replace') if isinstance(result.stderr, bytes) else str(result.stderr or '')[-300:]
            raise Refused(f'mysqldump{" pre-flight" if preflight else ""} of {target["prefix"]}_records failed ({result.returncode}): {message}')
        if preflight: return {'checked': True}
        return {'path': str(path), 'sha256': _sha256_file(path), 'bytes': path.stat().st_size}
    finally:
        if preflight: path.unlink(missing_ok=True)


def aurora_snapshot(services, plan_sha, stamp, *, snapshot_id=None, create=False, cluster_id=None, timeout=4 * 3600, poll=30):
    rds = services.rds()
    if snapshot_id:
        found = rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=snapshot_id).get('DBClusterSnapshots') or []
        if not found or found[0].get('Status') != 'available': raise Refused(f'Aurora snapshot {snapshot_id} is not available')
        return {'snapshot_id': snapshot_id, 'status': 'available', 'created': False}
    if not create: raise Refused('A backup is required: --backup-snapshot-id <available snapshot> or --create-aurora-snapshot')
    cluster = aurora_cluster(services, cluster_id)
    identifier = f'reveal-reload-{plan_sha[:12]}-{stamp.lower()}'
    rds.create_db_cluster_snapshot(DBClusterSnapshotIdentifier=identifier, DBClusterIdentifier=cluster,
                                   Tags=[{'Key': 'reveal-reference-reload-plan', 'Value': plan_sha}])
    waited = 0
    while True:
        found = rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=identifier).get('DBClusterSnapshots') or []
        if found and found[0].get('Status') == 'available': return {'snapshot_id': identifier, 'status': 'available', 'created': True, 'cluster': cluster}
        if waited >= timeout: raise Refused(f'Aurora snapshot {identifier} did not become available in {timeout}s')
        services.sleep(poll); waited += poll


def aurora_cluster(services, cluster_id=None):
    host = services.environ.get('REVEAL_MYSQL_HOST') or 'aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com'
    cluster = cluster_id or (host.split('.cluster-')[0] if '.cluster-' in host else None)
    if not cluster: raise Refused('Pass --aurora-cluster-id; it cannot be derived from REVEAL_MYSQL_HOST')
    return cluster


def aurora_preflight(services, *, snapshot_id=None, create=False, cluster_id=None):
    """Read-only: the named snapshot is available, or the cluster to snapshot exists (and AWS answers)."""
    if snapshot_id: return aurora_snapshot(services, '', '', snapshot_id=snapshot_id)
    if not create: raise Refused('A backup is required: --backup-snapshot-id <available snapshot> or --create-aurora-snapshot')
    cluster = aurora_cluster(services, cluster_id)
    try: found = services.rds().describe_db_clusters(DBClusterIdentifier=cluster).get('DBClusters') or []
    except Exception as error: raise Refused(f'Aurora cluster {cluster} cannot be described ({type(error).__name__}: {error})') from error
    if not found: raise Refused(f'Aurora cluster {cluster} was not found')
    return {'cluster': cluster, 'status': found[0].get('Status')}


def verified_snapshot(services, repo, target, plan):
    """The plan's vector snapshot, which must be a complete, verified snapshot of the plan's generation."""
    state = services.module('vector_ingestion').VectorRegistry(repo, environment_name=target['vector_environment']).get(plan['snapshot']['snapshot_id'], progress=False)
    if (state.get('status') != 'complete' or not (state.get('verification') or {}).get('passed')
            or state.get('reference_generation_id') != plan['generation_id']):
        raise Refused('The target snapshot is not a verified snapshot of this generation')
    return state


def activate(services, repo, target, plan):
    """vector_active and reference_active flip in one repository transaction (both compare-and-swap)."""
    vi = services.module('vector_ingestion')
    snapshot_id = plan['snapshot']['snapshot_id']
    with repo.transaction() as tx:
        vi.VectorRegistry(repo, environment_name=target['vector_environment']).activate_in(
            tx, snapshot_id, (plan['active']['vector'] or {}).get('snapshot_id'))
        return rg.write_active(tx, plan['generation_id'], plan['model'], expected_previous=(plan['active']['reference'] or {}).get('generation_id'),
                               vector_snapshot_id=snapshot_id)


def apply_plan(services, target, plan_path, approval_path, *, allow_production=False, cancel_active=False, drain_seconds=600,
               backup_dir=None, backup_snapshot_id=None, create_aurora_snapshot=False, skip_aurora_snapshot=False,
               aurora_cluster_id=None, keep_gate=False, poll_seconds=5):
    if skip_aurora_snapshot:
        if (target.get('name') != 'local' or target.get('prefix') != 'reveal_workflow_local'
                or target.get('vector_environment') != 'local' or target.get('production') is not False):
            raise Refused('--skip-aurora-snapshot is allowed only for local / reveal_workflow_local (non-production, vector environment local)')
        if backup_snapshot_id or create_aurora_snapshot:
            raise Refused('--skip-aurora-snapshot cannot be combined with another Aurora backup option')
    skipped_aurora = {'status': 'skipped', 'reason': 'explicit_local_only_waiver', 'created': False}
    bind_target(services, target)
    plan, approval = read_plan(plan_path), read_json(approval_path)
    if plan['kind'] != 'apply' or plan['target'] != target: raise Refused('The plan is for a different target or allow-list entry')
    check_approval(plan, approval)
    if plan['blockers']: raise Refused(f"The plan has blockers: {plan['blockers']}")
    check_production(services, plan, allow_production)
    resume = plan.get('resume')
    if not resume:
        if not backup_dir: raise Refused('--backup-dir is required')
        if not (backup_snapshot_id or create_aurora_snapshot or skip_aurora_snapshot):
            raise Refused('A backup is required: --backup-snapshot-id or --create-aurora-snapshot (local alone may use --skip-aurora-snapshot)')
    fresh = build_plan(services, target, plan['generation_id'])
    if fresh['plan_sha256'] != plan['plan_sha256']:
        changed = sorted(key for key in set(plan) | set(fresh) if key not in ('plan_sha256', 'planned_at', 'observed')
                         and _strip(plan.get(key)) != _strip(fresh.get(key)))
        raise Refused(f'Plan drift in {changed}: re-plan and re-approve')
    archive = services.module('reference_archive')
    repo, sha, generation_id, source = services.repository(target['prefix']), plan['plan_sha256'], plan['generation_id'], plan['from_generation']
    steps, result = [], {'target': target['name'], 'plan_sha256': sha, 'generation_id': generation_id, 'from_generation': source, 'status': 'failed'}
    def step(name, **detail):
        steps.append({'step': name, 'at': services.clock(), **detail}); emit(services, f'apply {target["name"]}: {name}')
    stamp = re.sub(r'[^0-9A-Za-z]', '', services.clock())[:15]
    if not resume:
        # Pre-flight what can abort the cutover before the gate closes and jobs are cancelled.
        verified_snapshot(services, repo, target, plan)
        step('preflight', records=mysqldump(services, target, backup_dir, stamp, preflight=True),
             aurora=dict(skipped_aurora) if skip_aurora_snapshot else aurora_preflight(
                 services, snapshot_id=backup_snapshot_id, create=create_aurora_snapshot, cluster_id=aurora_cluster_id))
    lock = services.connect()
    locked = gate_closed = False
    try:
        if scalar(lock, 'SELECT GET_LOCK(%s,%s)', (LOCK_NAME, 0)) != 1: raise Refused('Another reference reload holds the global lock')
        locked = True; step('lock')
        with repo.transaction() as tx: rg.set_gate(tx, True, reason='reference_reload', target=target['name'], plan_sha256=sha)
        gate_closed = True; step('gate_closed')
        if resume:
            # A failure after activation: the pointers stay switched; finish the idempotent tail only.
            with repo.read_transaction() as tx:
                active, vector_active = rg.read_active(tx), tx.get('vector_active', target['vector_environment'])
            if ((active or {}).get('generation_id') != generation_id
                    or ((vector_active or {}).get('data') or {}).get('snapshot_id') != plan['snapshot']['snapshot_id']):
                raise Refused('The target is no longer active on this generation and snapshot; re-plan')
            step('resumed', interrupted_plans=resume['interrupted_plans'], generation_id=source)
        else:
            step('drain', **drain_jobs(services, repo, source, cancel_active=cancel_active, drain_seconds=drain_seconds, poll_seconds=poll_seconds))
            backups = {'records': mysqldump(services, target, backup_dir, stamp),
                       'aurora': dict(skipped_aurora) if skip_aurora_snapshot else aurora_snapshot(
                           services, sha, stamp, snapshot_id=backup_snapshot_id, create=create_aurora_snapshot, cluster_id=aurora_cluster_id)}
            result['backups'] = backups; step('backups')
            capture = capture_generation(services, source, allow_listed_prefixes(load_targets(services.targets_file)), apply=True, locked=True)
            step('delta_capture', captured=capture['captured'], written=capture['written'], unresolved=len(capture['unresolved']))
            state = verified_snapshot(services, repo, target, plan)
            step('snapshot_verified', snapshot_id=state['snapshot_id'])
            result['active'] = activate(services, repo, target, plan); step('activated')
            connection = services.connect()
            try:
                if rg.get_generation(connection, source): rg.set_generation_status(connection, source, 'superseded', expected=('complete', 'superseded'))
            finally: connection.close()
            step('superseded', generation_id=source)
        result['archive'] = archive.archive_prefix(repo, source, generation_id, apply=True); step('archived')
        # Stamps whose anchors came from frozen Mechanism nodes (no usable request binding) only
        # become visible to referenced_sources now; freeze those factors before verifying.
        capture = capture_generation(services, source, [target['prefix']], apply=True, locked=True)
        step('post_archive_capture', captured=capture['captured'], written=capture['written'], unresolved=len(capture['unresolved']))
        result['verification'] = verify_target(services, target, generation_id=generation_id)
        step('verified', passed=result['verification']['passed'])
        result['status'] = 'complete' if result['verification']['passed'] else 'verify_failed'
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        try:
            if gate_closed and not keep_gate:
                with repo.transaction() as tx: rg.set_gate(tx, False, reason='reference_reload ' + result['status'], target=target['name'], plan_sha256=sha)
                steps.append({'step': 'gate_opened', 'at': services.clock()})
        except Exception as error: emit(services, f'Could not reopen the reload gate ({type(error).__name__}); run `gate --open`')
        try:
            if locked:
                audit = {'plan_sha256': sha, 'target': target['name'], 'generation_id': generation_id, 'from_generation': source,
                         'status': result['status'], 'error': result.get('error'), 'backups': result.get('backups'), 'steps': steps,
                         'counts': (result.get('archive') or {}).get('counts'), 'verification': result.get('verification'), 'namespaces_deleted': []}
                with repo.transaction() as tx: tx.put(rg.RELOAD_KIND, digest([sha]), rg.CATALOG_OWNER, audit)
        except Exception as error: emit(services, f'Could not write the reload audit record ({type(error).__name__})')
        finally:
            try:
                if locked: scalar(lock, 'SELECT RELEASE_LOCK(%s)', (LOCK_NAME,))
            finally: lock.close()
    result['steps'] = steps
    result['ok'] = result['status'] == 'complete'
    return result


# --------------------------------------------------------------------------------------
# verify (read-only post-conditions, docs §9)


def _stamps(tx, kind, path):
    function = 'json_extract' if tx.sqlite else 'JSON_EXTRACT'
    rows = tx.execute(f'SELECT id,{function}(payload,%s) FROM reveal_records WHERE kind=%s', ('$.' + '.'.join(path + ('archive',)), kind)).fetchall()
    stamps = {}
    for identity, raw in rows:
        stamp = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if stamp: stamps[identity] = stamp
    return stamps


def verify_target(services, target, *, generation_id=None, client=None):
    archive, vi, vr = services.module('reference_archive'), services.module('vector_ingestion'), services.module('vector_retrieval')
    checks = {}
    def check(name, passed, **detail): checks[name] = {'passed': bool(passed), **detail}
    repo = services.repository(target['prefix'])
    with repo.read_transaction() as tx:
        active = rg.read_active(tx)
        vector_active = tx.get('vector_active', target['vector_environment'])
        stamps = {kind: _stamps(tx, kind, path) for kind, path in STAMP_PATHS.items()}
    if not active: return {'target': target['name'], 'passed': False, 'ok': False, 'checks': {'active': {'passed': False, 'detail': 'legacy mode: no reference_active'}}}
    generation_id = generation_id or active['generation_id']
    check('active_generation', active['generation_id'] == generation_id, active=active['generation_id'])
    snapshot = None
    if vector_active:
        try: snapshot = vi.VectorRegistry(repo, environment_name=target['vector_environment']).get(vector_active['data']['snapshot_id'])
        except Exception as error: check('vector_snapshot', False, error=f'{type(error).__name__}: {error}')
    check('pointers_agree', snapshot and snapshot.get('status') == 'complete' and snapshot.get('reference_generation_id') == generation_id
          and active.get('vector_snapshot_id') == snapshot['snapshot_id'], vector_snapshot_id=snapshot and snapshot['snapshot_id'])
    connection = services.connect()
    try:
        generation = rg.get_generation(connection, generation_id) or {}
        expected = {**(generation.get('manifest') or {}).get('counts', {}), 'reference_vectors': ((generation.get('manifest') or {}).get('vectors') or {}).get('count')}
        counts = generation_counts(connection, generation_id)
        # Another prefix's later cutover may already have marked it superseded; it stays servable.
        check('generation_counts', generation.get('status') in ('complete', 'superseded') and counts == expected, counts=counts, expected=expected)
        orphans = scalar(connection, 'SELECT COUNT(*) FROM reference_factors f LEFT JOIN kpn_traits t ON t.generation_id=f.generation_id '
                                     'AND t.kpn_trait_id=f.kpn_trait_id WHERE f.generation_id=%s AND t.kpn_trait_id IS NULL', (generation_id,))
        check('factor_traits', orphans == 0, orphans=orphans)
        if snapshot and snapshot.get('status') == 'complete':
            client = client or services.vector_client()
            try:
                vr.UpstashFactorIndex(snapshot, client=client).check(); check('namespace_counts', True)
            except Exception as error: check('namespace_counts', False, error=str(error))
            inventory = {}
            for key, namespace in namespaces_of(snapshot).items():
                seen, cursor = set(), ''
                while True:
                    page = client.range(cursor=cursor, limit=200, namespace=namespace)
                    seen |= {vr.value(row, 'id') for row in vr.value(page, 'vectors', [])}
                    cursor = vr.value(page, 'next_cursor', '')
                    if not cursor or cursor == '0': break
                bound = {row[0] for row in query(connection, 'SELECT vector_id FROM vector_bindings WHERE environment=%s AND namespace=%s',
                                                 (target['vector_environment'], namespace))}
                inventory[namespace] = {'upstash': len(seen), 'bindings': len(bound), 'equal': seen == bound and bool(seen)}
            check('vector_inventory', inventory and all(item['equal'] for item in inventory.values()), namespaces=inventory)
            for name, probe in (('self_neighbour', _self_neighbour), ('context_hits', _context_hits)):
                passed, detail = probe(client, snapshot, vr)
                check(name, passed, **detail)
        prefix_plan = archive.plan_prefix(repo, generation_id)
        pending = {key: prefix_plan.get(key) for key in ('archive_candidates', 'anchored_drafts')}
        check('archive_complete', not any(_size(value) for value in pending.values()), **{key: _size(value) for key, value in pending.items()})
        stale = sorted(f'{kind}:{identity}' for kind, rows in stamps.items() for identity, stamp in rows.items()
                       if stamp.get('to_reference_generation') != generation_id)
        check('stamps_current', not stale, stale=stale[:20])
        referenced = {anchor.get('archived_reference_factor_id') for rows in stamps.values() for stamp in rows.values()
                      for anchor in (stamp.get('reference') or {}).get('anchors') or []}
        missing = sorted(identity or '(none)' for identity in referenced if not identity)
        present = set()
        wanted = sorted(identity for identity in referenced if identity)
        for start in range(0, len(wanted), 500):
            batch = wanted[start:start + 500]
            present |= {row[0] for row in query(connection, f'SELECT archive_id FROM archived_reference_factors WHERE archive_id IN ({_placeholders(batch)})', tuple(batch))}
        missing += sorted(set(wanted) - present)
        check('archived_factors', not missing, missing=missing[:20])
        dismech = generation.get('dismech_import_id')
        gaps = {row[0] for row in query(connection, "SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.id')) FROM dismech_discussions WHERE import_id=%s AND is_gap=1",
                                         (dismech,))} if dismech else set()
        unresolved = sorted({(stamp.get('gap') or {}).get('source_id') for rows in stamps.values() for stamp in rows.values()
                             if stamp.get('gap')} - gaps)
        check('gaps_resolve', bool(dismech) and not unresolved, unresolved=unresolved[:20])
    finally: connection.close()
    passed = all(item['passed'] for item in checks.values())
    return {'target': target['name'], 'generation_id': generation_id, 'passed': passed, 'ok': passed, 'checks': checks}


def _size(value):
    """Row count of a plan entry: a list, a {kind: rows} mapping or a number."""
    if isinstance(value, dict): return sum(_size(item) if isinstance(item, (list, dict)) else 1 for item in value.values())
    return len(value) if isinstance(value, (list, set, tuple)) else int(value or 0)


def _fetch_vector(client, identity, namespace, vr):
    rows = client.fetch(ids=[identity], namespace=namespace, include_vectors=True)
    return vr.value(rows[0], 'vector') if rows and rows[0] is not None else None


def _self_neighbour(client, snapshot, vr):
    """A known factor is its own nearest neighbour (factors sharing its label tie with it)."""
    try:
        factor = snapshot['factors'][0]
        vector = _fetch_vector(client, factor['id'], snapshot['factor_namespace'], vr)
        hits = client.query(vector=vector, top_k=10, namespace=snapshot['factor_namespace']) if vector is not None else []
        best = max((vr.value(hit, 'score', 0) for hit in hits), default=None)
        tied = [vr.value(hit, 'id') for hit in hits if best is not None and vr.value(hit, 'score', 0) >= best - 1e-6]
        return factor['id'] in tied, {'factor': factor['id'], 'top': tied[:3]}
    except Exception as error: return False, {'error': f'{type(error).__name__}: {error}'}


def _context_hits(client, snapshot, vr):
    try:
        context = snapshot['contexts'][0]
        vector = _fetch_vector(client, context['id'], snapshot['context_namespace'], vr)
        hits = client.query(vector=vector, top_k=5, namespace=snapshot['factor_namespace']) if vector is not None else []
        return bool(hits), {'context': context['id'], 'hits': len(hits)}
    except Exception as error: return False, {'error': f'{type(error).__name__}: {error}'}


# --------------------------------------------------------------------------------------
# purge-retired


def _in(column, values): return f'{column} IN ({_placeholders(values)})', tuple(values)


def purge_statements(new, retired, kept, *, all_gene_set_imports_retired):
    """Ordered (step, table, where, params) deletes; children first (docs §7)."""
    statements = []
    add = lambda step, table, where, params=(): statements.append((step, table, where, tuple(params)))
    mapping = sorted({g['legacy_mapping_run_id'] for g in retired if g['kind'] == rg.LEGACY_KIND and g.get('legacy_mapping_run_id')})
    if mapping:
        for table in ('eaggl_cfde_gene_set_links', 'eaggl_cfde_factor_links', 'eaggl_cfde_link_runs'): add(1, table, *_in('run_id', mapping))
    runs = sorted({g['eaggl_embedding_run_id'] for g in kept if g.get('eaggl_embedding_run_id')})
    retired_imports = sorted({g['eaggl_import_id'] for g in retired if g.get('eaggl_import_id')})
    if retired_imports:
        # DisMech context runs bound to an embedding run of a retired generation's EAGGL import (exactly what
        # the cold exports hold, and what step 3 needs gone), except those bound to a kept generation's run.
        where = f'eaggl_embedding_run_id IN (SELECT run_id FROM eaggl_embedding_runs WHERE import_id IN ({_placeholders(retired_imports)}))'
        if runs: where += f' AND eaggl_embedding_run_id NOT IN ({_placeholders(runs)})'
        params, unbound = retired_imports + runs, f'SELECT run_id FROM dismech_embedding_runs WHERE {where}'
        add(2, 'dismech_embedding_inputs', f'run_id IN ({unbound})', params)
        add(2, 'dismech_embedding_vectors', f'run_id IN ({unbound})', params)
        add(2, 'dismech_embedding_runs', where, params)
    imports = sorted({g['eaggl_import_id'] for g in retired if g.get('eaggl_import_id')} - {g['eaggl_import_id'] for g in kept if g.get('eaggl_import_id')})
    if imports:
        add(3, 'eaggl_name_embeddings', f'run_id IN (SELECT run_id FROM eaggl_embedding_runs WHERE import_id IN ({_placeholders(imports)}))', imports)
        for table in ('eaggl_embedding_runs', 'eaggl_graph_edges', 'eaggl_graph_nodes', 'eaggl_gene_loadings', 'eaggl_factors', 'eaggl_genes', 'eaggl_imports'):
            add(3, table, *_in('import_id', imports))
    gene_sets = sorted({g['legacy_gene_set_import_id'] for g in retired if g.get('legacy_gene_set_import_id')}
                       - {g['legacy_gene_set_import_id'] for g in kept if g.get('legacy_gene_set_import_id')})
    if gene_sets:
        add(4, 'cfde_gene_set_aliases', *_in('import_id', gene_sets))
        add(4, 'gene_set_imports', *_in('import_id', gene_sets))
        add(4, 'dapper_objects', "class_name='GeneSet' AND NOT EXISTS (SELECT 1 FROM cfde_gene_set_aliases a WHERE a.dapper_id=dapper_objects.id)")
        if all_gene_set_imports_retired: add(4, 'dapper_objects', "class_name='Activity'")
    kpn = sorted(g['generation_id'] for g in retired if g['kind'] == rg.KPN_KIND)
    if kpn:
        for table in GENERATION_TABLES: add(5, table, *_in('generation_id', kpn))
    return statements


def _count_sql(table, where): return f'SELECT COUNT(*) FROM {table} WHERE {where}'


def stray_link_runs(connection, retired, kept):
    """EAGGL mapping runs other than the registered legacy ones that reference a retiring EAGGL or gene-set import.

    Step 1 deletes only registered runs' links (the cold export holds only those), so any other run
    would make steps 3-4 fail on foreign keys partway through; the purge plan refuses instead.
    """
    if not table_exists(connection, 'eaggl_cfde_link_runs'): return []
    registered = {g['legacy_mapping_run_id'] for g in retired if g.get('legacy_mapping_run_id')}
    retiring = lambda key: sorted({g[key] for g in retired if g.get(key)} - {g[key] for g in kept if g.get(key)})
    clauses, params = [], []
    for column, values in (('eaggl_import_id', retiring('eaggl_import_id')), ('gene_set_import_id', retiring('legacy_gene_set_import_id'))):
        if values: clauses.append(f'{column} IN ({_placeholders(values)})'); params += values
    if not clauses: return []
    rows = query(connection, 'SELECT run_id FROM eaggl_cfde_link_runs WHERE ' + ' OR '.join(clauses) + ' ORDER BY run_id', tuple(params))
    return [row[0] for row in rows if row[0] not in registered]


def _prefix_deletes(tx, environments):
    """Record ids to drop in one prefix: suggestions, non-active snapshots of the environments and their batches."""
    suggestions = [row[0] for row in tx.execute('SELECT id FROM reveal_records WHERE kind=%s ORDER BY id', ('suggestion',)).fetchall()]
    active = {json.loads(row[0])['snapshot_id'] for row in tx.execute('SELECT payload FROM reveal_records WHERE kind=%s', ('vector_active',)).fetchall()}
    snapshots = [row[0] for row in tx.execute('SELECT id,' + _json_field(tx, 'environment') + ' FROM reveal_records WHERE kind=%s ORDER BY id',
                                                ('vector_snapshot',)).fetchall() if row[1] in environments and row[0] not in active]
    dropped = set(snapshots)
    batches = [row[0] for row in tx.execute('SELECT id,' + _json_field(tx, 'snapshot_id') + ' FROM reveal_records WHERE kind=%s ORDER BY id',
                                              ('vector_batch',)).fetchall() if row[1] in dropped]
    return {'suggestion': suggestions, 'vector_snapshot': snapshots, 'vector_batch': batches}


def _active_namespaces(services, targets):
    active = {}
    for target in targets.values():
        repo = services.repository(target['prefix'])
        with repo.read_transaction() as tx:
            rows = [json.loads(row[0]) for row in tx.execute('SELECT payload FROM reveal_records WHERE kind=%s', ('vector_active',)).fetchall()]
        registry = services.module('vector_ingestion').VectorRegistry(repo, environment_name=target['vector_environment'])
        for row in rows:
            try: state = registry.get(row['snapshot_id'], progress=False)
            except Exception: continue
            active.setdefault(state['environment'], set()).update(namespaces_of(state).values())
    return active


def build_purge_plan(services, generation_id=None, *, client=None):
    targets = load_targets(services.targets_file)
    check_shell(services, list(targets.values())); check_database(services, list(targets.values())); check_upstash(services, list(targets.values()))
    blockers, audits, actives = [], {}, {}
    for target in targets.values():
        with services.repository(target['prefix']).read_transaction() as tx:
            active = rg.read_active(tx)
            actives[target['name']] = active['generation_id'] if active else None
            audits[target['name']] = [row['data'] for row in tx.list(rg.RELOAD_KIND)]
    generation_id = generation_id or (_single(actives.values(), 'active generation across targets') if len(set(actives.values())) == 1 else None)
    if not generation_id: raise Refused(f'Targets are not all active on one generation: {actives}')
    connection = services.connect()
    try:
        generations = rg.list_generations(connection)
        new = next((g for g in generations if g['generation_id'] == generation_id), None)
        if not new or new['kind'] != rg.KPN_KIND or new['status'] != 'complete': raise Refused(f'{generation_id[:12]} is not a complete KPN generation')
        if not new.get('eaggl_embedding_run_id'): raise Refused('The new generation has no EAGGL embedding run')
        retired = [g for g in generations if g['generation_id'] != generation_id and g['status'] in ('superseded', 'failed', 'retired')]
        kept = [g for g in generations if g not in retired]
        production = any(target['production'] for target in targets.values())
        for target in targets.values():
            name = target['name']
            if actives[name] != generation_id: blockers.append(f'{name} is not active on {generation_id[:12]}')
            if not any(audit.get('generation_id') == generation_id and (audit.get('verification') or {}).get('passed') for audit in audits[name]):
                blockers.append(f'{name} has no recorded passing verification of {generation_id[:12]}')
        for g in retired:
            if g['status'] != 'superseded': continue
            ref = g.get('cold_export_ref')
            if not ref or not ref.get('root'): blockers.append(f"{g['generation_id'][:12]} has no cold export (`capture --cold-export --apply`)")
            elif ref.get('format') != EXPORT_FORMAT:
                blockers.append(f"{g['generation_id'][:12]} cold export predates {EXPORT_FORMAT}; rerun `capture --from {g['generation_id']} --cold-export --apply`")
            elif production and ref['root'].get('store') != 's3': blockers.append(f"{g['generation_id'][:12]} cold export is not in S3")
            elif not export_current(services, ref): blockers.append(f"{g['generation_id'][:12]} cold export is missing or incomplete")
            for target in targets.values():
                jobs = open_jobs(services.repository(target['prefix']), g['generation_id'])
                if jobs: blockers.append(f"{target['name']} has {len(jobs)} non-terminal jobs of {g['generation_id'][:12]}")
        for target in targets.values():
            if actives[target['name']] == generation_id:
                verification = verify_target(services, target, generation_id=generation_id, client=client)
                if not verification['passed']: blockers.append(f"{target['name']} verification fails: {sorted(k for k, v in verification['checks'].items() if not v['passed'])}")
        stray = stray_link_runs(connection, retired, kept)
        if stray: blockers.append(f'Unregistered EAGGL mapping runs reference retiring imports: {[run[:12] for run in stray]}; '
                                  'no cold export holds them: export and remove them by hand before purging')
        retiring = {g['legacy_gene_set_import_id'] for g in retired if g.get('legacy_gene_set_import_id')}
        remaining_imports = [row[0] for row in query(connection, 'SELECT import_id FROM gene_set_imports')] if table_exists(connection, 'gene_set_imports') else []
        statements = purge_statements(new, retired, kept, all_gene_set_imports_retired=set(remaining_imports) <= retiring)
        deletes = [{'step': step, 'table': table, 'where': where, 'params': list(params),
                    'rows': scalar(connection, _count_sql(table, where), params) if table_exists(connection, table) else 0}
                   for step, table, where, params in statements]
    finally: connection.close()
    environments = sorted({target['vector_environment'] for target in targets.values()})
    records = {}
    for prefix in allow_listed_prefixes(targets):
        environment = {t['vector_environment'] for t in targets.values() if t['prefix'] == prefix}
        with services.repository(prefix).read_transaction() as tx: records[prefix] = {kind: len(ids) for kind, ids in _prefix_deletes(tx, environment).items()}
    active_namespaces = _active_namespaces(services, targets)
    listed = services.module('vector_ingestion').list_namespaces(client or services.vector_client())
    deletable = services.module('vector_ingestion').deletable_namespace
    namespace_deletes = {environment: sorted(name for name in listed if deletable(name, environment)
                                             and name not in active_namespaces.get(environment, set())) for environment in environments}
    plan = {'format': PURGE_FORMAT, 'kind': 'purge', 'target': {'name': PURGE_NAME, 'targets': sorted(targets), 'production': any(t['production'] for t in targets.values()),
            'allow_list': targets}, 'generation_id': generation_id, 'retired': sorted(g['generation_id'] for g in retired), 'deletes': deletes,
            'namespace_deletes': namespace_deletes, 'blockers': blockers,
            # Step 6 always drops every catalog-owned suggestion and each non-active snapshot, recomputed at
            # purge time: users keep creating suggestions, so their counts are observed only (no drift).
            'observed': {'records': records}, 'planned_at': services.clock()}
    plan['plan_sha256'] = plan_digest(plan)
    return plan


def purge_retired(services, plan_path, approval_path, *, allow_production=False, client=None):
    """Protected purge. Executes the freshly computed statements, which must hash to the approved plan."""
    plan, approval = read_plan(plan_path), read_json(approval_path)
    if plan['kind'] != 'purge': raise Refused('Not a purge plan')
    check_approval(plan, approval)
    if plan['blockers']: raise Refused(f"The purge plan has blockers: {plan['blockers']}")
    check_production(services, plan, allow_production)
    targets = load_targets(services.targets_file)
    if plan['target']['allow_list'] != targets: raise Refused('The allow-list changed since the purge was planned')
    client = client or services.vector_client(write=True)
    fresh = build_purge_plan(services, plan['generation_id'], client=client)
    if fresh['plan_sha256'] != plan['plan_sha256']: raise Refused('Purge plan drift: re-plan and re-approve')
    vi = services.module('vector_ingestion')
    report = {'generation_id': fresh['generation_id'], 'deleted': [], 'records': {}, 'namespaces': {}, 'retired': []}
    lock = services.connect()
    locked = False
    try:
        if scalar(lock, 'SELECT GET_LOCK(%s,%s)', (LOCK_NAME, 0)) != 1: raise Refused('Another reference reload holds the global lock')
        locked = True
        connection = services.connect()
        try:
            for item in fresh['deletes']:  # steps 1-5, children first
                emit(services, f"purge step {item['step']}: {item['table']}")
                report['deleted'].append({'step': item['step'], 'table': item['table'],
                                          'rows': delete_batches(connection, item['table'], item['where'], item['params'])})
            for prefix in allow_listed_prefixes(targets):  # step 6
                environments = {t['vector_environment'] for t in targets.values() if t['prefix'] == prefix}
                repo = services.repository(prefix)
                with repo.read_transaction() as tx: doomed = _prefix_deletes(tx, environments)
                for kind in ('vector_batch', 'vector_snapshot', 'suggestion'):
                    for offset in range(0, len(doomed[kind]), 500):
                        with repo.transaction() as tx:
                            for identity in doomed[kind][offset:offset + 500]: tx.remove(kind, identity)
                report['records'][prefix] = {kind: len(ids) for kind, ids in doomed.items()}
            active = _active_namespaces(services, targets)  # step 7
            for environment, names in fresh['namespace_deletes'].items():
                report['namespaces'][environment] = []
                for name in names:
                    if not vi.deletable_namespace(name, environment) or name in active.get(environment, set()):
                        raise Refused(f'Refusing to delete namespace {name}')
                    vi.delete_namespace(client, name, environment=environment, protected=active.get(environment, set()))
                    delete_batches(connection, 'vector_bindings', 'environment=%s AND namespace=%s', (environment, name))
                    report['namespaces'][environment].append(name)
            for identity in fresh['retired']:  # step 8
                rg.set_generation_status(connection, identity, 'retired', expected=('superseded', 'failed', 'retired'))
                report['retired'].append(identity)
        finally: connection.close()
    finally:
        try:
            if locked:
                scalar(lock, 'SELECT RELEASE_LOCK(%s)', (LOCK_NAME,))
                audit = {'plan_sha256': plan['plan_sha256'], 'target': PURGE_NAME, 'generation_id': plan['generation_id'], 'purge': True,
                         'status': 'complete' if report['retired'] == fresh['retired'] else 'failed', 'steps': report['deleted'],
                         'counts': report['records'], 'namespaces_deleted': sorted(n for names in report['namespaces'].values() for n in names)}
                for prefix in allow_listed_prefixes(targets):
                    try:
                        with services.repository(prefix).transaction() as tx: tx.put(rg.RELOAD_KIND, digest([plan['plan_sha256']]), rg.CATALOG_OWNER, audit)
                    except Exception as error: emit(services, f'Could not write the purge audit in {prefix} ({type(error).__name__})')
        finally: lock.close()
    return {**report, 'ok': True}


def set_gate(services, target, *, closed, reason):
    """Explicit gate control, e.g. to reopen after `apply --keep-gate`."""
    bind_target(services, target, upstash=False)
    with services.repository(target['prefix']).transaction() as tx:
        return {'target': target['name'], 'gate': rg.set_gate(tx, closed, reason=reason or 'operator', target=target['name'])}


# --------------------------------------------------------------------------------------
# status


def status(services):
    targets = load_targets(services.targets_file)
    result = {'targets': {}}
    try:
        connection = services.connect()
        try:
            result['generations'] = [{**{key: g[key] for key in ('generation_id', 'kind', 'model', 'status', 'eaggl_import_id', 'legacy_mapping_run_id')},
                                      'counts': (g.get('manifest') or {}).get('counts'), 'cold_export': bool(g.get('cold_export_ref'))}
                                     for g in rg.list_generations(connection)]
        finally: connection.close()
    except Exception as error: result['generations'] = {'error': f'{type(error).__name__}: {error}'}
    for name, target in targets.items():
        try:
            with services.repository(target['prefix']).read_transaction() as tx:
                gate = tx.get(rg.CONTROL_KIND, rg.CONTROL_ID)
                vector_active = tx.get('vector_active', target['vector_environment'])
                audits = [{key: row['data'].get(key) for key in ('plan_sha256', 'generation_id', 'status', 'purge')} | {'version': row['version']}
                          for row in tx.list(rg.RELOAD_KIND)[:3]]
                runs = [{key: row['data'].get(key) for key in ('from_generation', 'to_generation', 'completed_at')} for row in tx.list(rg.ARCHIVE_RUN_KIND)[:3]]
                result['targets'][name] = {'prefix': target['prefix'], 'vector_environment': target['vector_environment'], 'production': target['production'],
                                           'reference_active': rg.read_active(tx), 'mode': 'kpn' if rg.read_active(tx) else 'legacy',
                                           'vector_active': vector_active['data'] if vector_active else None, 'gate': gate['data'] if gate else None,
                                           'audits': audits, 'archive_runs': runs}
        except Exception as error: result['targets'][name] = {'prefix': target['prefix'], 'error': f'{type(error).__name__}: {error}'}
    return result


# --------------------------------------------------------------------------------------
# CLI


def parser():
    p = argparse.ArgumentParser(prog='python -m reveal_backend.reference_reload', description=__doc__.split('\n\n')[0])
    p.add_argument('--targets-file', type=Path, help=argparse.SUPPRESS)
    commands = p.add_subparsers(dest='command', required=True)
    c = commands.add_parser('preflight', help='Read-only: server, grants, lock, schema and served sources (and a bundle) before load')
    c.add_argument('--out', type=Path); c.add_argument('--compare', type=Path, help='A previous preflight result: flag schema and source changes')
    c.add_argument('--bundle', type=Path, help="Also check the bundle's factors, labels and embedding space against the EAGGL source")
    c.add_argument('--eaggl-import-id'); c.add_argument('--eaggl-source-version'); c.add_argument('--eaggl-embedding-run-id')
    c.add_argument('--check-embedding-service', action='store_true', help="Embed one probe text with the EAGGL run's model and service")
    c = commands.add_parser('build', help='Validate LAP outputs and write DIR/<generation_id>/ (pure files)')
    c.add_argument('--lap-project-dir', type=Path, default=DEFAULT_LAP_PROJECT)
    c.add_argument('--kpn-release')
    c.add_argument('--out', type=Path, required=True)
    c = commands.add_parser('embed', help='Calibrate and convert CFDE snapshot vectors into the bundle')
    c.add_argument('--bundle', type=Path, required=True)
    c.add_argument('--embeddings-dir', type=Path)
    c.add_argument('--model'); c.add_argument('--model-revision'); c.add_argument('--provider'); c.add_argument('--service-url')
    c.add_argument('--calibration-samples', type=int, default=32)
    c.add_argument('--skip-calibration', action='store_true', help='Record that no calibration probe was run')
    c = commands.add_parser('load', help='Additive: migration 008, legacy registration, KPN generation insert')
    c.add_argument('--bundle', type=Path, required=True)
    c.add_argument('--eaggl-import-id'); c.add_argument('--eaggl-source-version'); c.add_argument('--eaggl-embedding-run-id')
    c.add_argument('--apply', action='store_true')
    c = commands.add_parser('abandon', help='Remove a KPN generation that never served (dry run unless --apply; interactive)')
    c.add_argument('--generation', required=True)
    c.add_argument('--drop-empty-schema', action='store_true', help='Then drop the migration-008 tables when nothing but legacy registrations remain')
    c.add_argument('--apply', action='store_true')
    c = commands.add_parser('capture', help='Additive: archived factor snapshots, anchor display backfill, cold export')
    c.add_argument('--from', dest='generation', required=True,
                   help='Generation to freeze and export, or `active`: the one generation the prefixes serve now (legacy mode: the legacy generation)')
    c.add_argument('--prefixes', help='Comma-separated allow-listed prefixes (default: all)')
    c.add_argument('--cold-export', action='store_true')
    c.add_argument('--export-dir', type=Path, help='Local cold-export directory when S3 is not configured')
    c.add_argument('--dapper-snapshot', type=Path, default=DEFAULT_DAPPER)
    c.add_argument('--apply', action='store_true')
    c = commands.add_parser('snapshot', help='Build, upload and verify the Upstash snapshot of a target (no activation)')
    c.add_argument('--generation', required=True); c.add_argument('--target', required=True)
    c.add_argument('--batch-size', type=int, default=200, help='Vectors per import batch (1-200)'); c.add_argument('--apply', action='store_true')
    c = commands.add_parser('plan', help='Read-only cutover plan for one target')
    c.add_argument('--target', required=True); c.add_argument('--generation', required=True); c.add_argument('--out', type=Path)
    c = commands.add_parser('approve', help='Interactive typed approval of a plan')
    c.add_argument('--plan', type=Path, required=True); c.add_argument('--out', type=Path)
    c.add_argument('--allow-production', action='store_true'); c.add_argument('--approver')
    c = commands.add_parser('apply', help='Protected cutover of one target (or, for a resume plan, finish an interrupted one)')
    c.add_argument('--target', required=True); c.add_argument('--plan', type=Path, required=True); c.add_argument('--approval', type=Path, required=True)
    c.add_argument('--allow-production', action='store_true'); c.add_argument('--cancel-active', action='store_true')
    # Required for a cutover (apply refuses without them); a resume plan takes no new backups.
    c.add_argument('--drain-seconds', type=int, default=600); c.add_argument('--backup-dir', type=Path)
    backup = c.add_mutually_exclusive_group()
    backup.add_argument('--backup-snapshot-id'); backup.add_argument('--create-aurora-snapshot', action='store_true')
    backup.add_argument('--skip-aurora-snapshot', action='store_true',
                        help='Explicit local-only waiver: local / reveal_workflow_local; still requires the records dump, typed plan approval and verification')
    c.add_argument('--aurora-cluster-id'); c.add_argument('--keep-gate', action='store_true')
    c = commands.add_parser('verify', help='Read-only post-conditions of one target')
    c.add_argument('--target', required=True); c.add_argument('--generation')
    c = commands.add_parser('purge-retired', help='Plan (default) or, with --apply and an approval, purge retired generations')
    c.add_argument('--generation'); c.add_argument('--out', type=Path)
    c.add_argument('--plan', type=Path); c.add_argument('--approval', type=Path)
    c.add_argument('--allow-production', action='store_true'); c.add_argument('--apply', action='store_true')
    c = commands.add_parser('gate', help='Close or reopen the reload gate of one target (e.g. after apply --keep-gate)')
    c.add_argument('--target', required=True); c.add_argument('--reason')
    state = c.add_mutually_exclusive_group(required=True)
    state.add_argument('--open', action='store_true'); state.add_argument('--close', action='store_true')
    commands.add_parser('status', help='Generations, active pointers, gates and audits')
    return p


def _prefixes(services, value):
    targets = load_targets(services.targets_file)
    allowed = allow_listed_prefixes(targets)
    chosen = sorted({item.strip() for item in value.split(',') if item.strip()}) if value else allowed
    unknown = sorted(set(chosen) - set(allowed))
    if unknown: raise Refused(f'Prefixes {unknown} are not allow-listed')
    return targets, chosen


def run(services, args):
    command = args.command
    if command == 'preflight':
        return preflight(services, out=args.out, compare=args.compare, bundle=args.bundle, eaggl_import_id=args.eaggl_import_id,
                         eaggl_source_version=args.eaggl_source_version, eaggl_embedding_run_id=args.eaggl_embedding_run_id,
                         check_embedding_service=args.check_embedding_service)
    if command == 'build': return build_bundle(args.lap_project_dir, args.out, kpn_release=args.kpn_release)
    if command == 'embed':
        return embed_bundle(services, args.bundle, embeddings_dir=args.embeddings_dir, model=args.model, model_revision=args.model_revision,
                            provider=args.provider, service_url=args.service_url, samples=args.calibration_samples, skip_calibration=args.skip_calibration)
    if command == 'load':
        return load_bundle(services, args.bundle, apply=args.apply, eaggl_import_id=args.eaggl_import_id,
                           eaggl_source_version=args.eaggl_source_version, eaggl_embedding_run_id=args.eaggl_embedding_run_id)
    if command == 'abandon': return abandon_generation(services, args.generation, apply=args.apply, drop_empty_schema=args.drop_empty_schema)
    if command == 'capture':
        targets, prefixes = _prefixes(services, args.prefixes)
        check_database(services, list(targets.values()))
        generation = active_source(services, prefixes) if args.generation == 'active' else args.generation
        return capture_generation(services, generation, prefixes, apply=args.apply, cold=args.cold_export, export_dir=args.export_dir, dapper=args.dapper_snapshot)
    if command == 'snapshot': return snapshot_generation(services, select_target(services, args.target), args.generation, apply=args.apply, batch_size=args.batch_size)
    if command == 'plan':
        target = bind_target(services, select_target(services, args.target))
        plan = build_plan(services, target, args.generation)
        path = write_json(args.out or Path(f"plan.{target['name']}.json"), plan)
        return {'plan': str(path), 'plan_sha256': plan['plan_sha256'], 'target': target['name'], 'generation_id': args.generation,
                'blockers': plan['blockers'], 'summary': _summary(plan)}
    if command == 'approve': return approve_plan(services, args.plan, out=args.out, allow_production=args.allow_production, approver=args.approver)
    if command == 'apply':
        return apply_plan(services, select_target(services, args.target), args.plan, args.approval, allow_production=args.allow_production,
                          cancel_active=args.cancel_active, drain_seconds=args.drain_seconds, backup_dir=args.backup_dir,
                          backup_snapshot_id=args.backup_snapshot_id, create_aurora_snapshot=args.create_aurora_snapshot,
                          skip_aurora_snapshot=args.skip_aurora_snapshot,
                          aurora_cluster_id=args.aurora_cluster_id, keep_gate=args.keep_gate)
    if command == 'verify':
        target = bind_target(services, select_target(services, args.target))
        return verify_target(services, target, generation_id=args.generation)
    if command == 'purge-retired':
        if args.apply:
            if not (args.plan and args.approval): raise Refused('purge-retired --apply needs --plan and --approval')
            return purge_retired(services, args.plan, args.approval, allow_production=args.allow_production)
        plan = build_purge_plan(services, args.generation)
        path = write_json(args.out or Path('purge-plan.json'), plan)
        return {'plan': str(path), 'plan_sha256': plan['plan_sha256'], 'blockers': plan['blockers'], 'summary': _summary(plan)}
    if command == 'gate': return set_gate(services, select_target(services, args.target), closed=args.close, reason=args.reason)
    return status(services)


def main(argv=None, services=None):
    if services is None:
        shell = dict(os.environ)  # the shell's own values, before .env fills defaults
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False, interpolate=False)
        logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stderr)
        services = Services(shell=shell)
    args = parser().parse_args(argv)
    if args.targets_file: services.targets_file = Path(args.targets_file)
    try:
        result, code = run(services, args), 0
        if result.get('ok') is False: code = 1
    except Refused as error: result, code = {'ok': False, 'refused': str(error)}, 2
    print(json.dumps({'command': args.command, **result}, sort_keys=True, default=str, ensure_ascii=False), flush=True)
    return code


if __name__ == '__main__': raise SystemExit(main())
