"""Version-pinned Aurora source adapters shared by the API and collector.

Served EAGGL factors come from one reference generation (docs/reference-reload.md §8). The
prefix's `reference_active` record, or REVEAL_REFERENCE_GENERATION_ID, names it. load() reads it
once, on the cold load, together with the active Vector snapshot (`vector_active`) in one
statement; a loaded catalog does no I/O in load(), as before reference reloads, and serves that
pinned snapshot's semantic index. A background poller (Catalog(poll_seconds=...), which the API
enables) re-checks both pointers every GENERATION_TTL_SECONDS off the request path (reusing the
readiness monitor's reads when it runs), and a change reloads the catalog. Without an active
record the catalog runs in legacy mode, exactly as before reference reloads: the CFDE-linked
factors of the one selected mapping run (cfde-inc-v2).
"""
from collections import defaultdict
from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import copy, deepcopy
from datetime import timezone
from difflib import SequenceMatcher
import json
import logging
import os
from pathlib import Path
import threading
from time import monotonic
from .auth import Problem
from .repository import Repository, canonical, digest, now
from .runtime_config import ROOT, CURRENT_DAPPER_SNAPSHOT, setting, mysql_connection, reference_mysql_connection
from .evidence_package import DapperRuntime, canonical_json, sha256
from .eaggl_embeddings import database_search_index
from .fuzzy import FuzzyWords
from .embedding_client import get_embeddings
from .dismech_embeddings import context_input, load_context_vectors
from .mapping_identity import POLICY_VERSION, interpreted_mappings, normalize_disease_id
from .vector_retrieval import UpstashFactorIndex, VectorUnavailable, check_snapshot_readiness, retrieve_native, query_vector_provenance
from .vector_ingestion import VectorRegistry
from .reference_generation import (ACTIVE_ID, ACTIVE_KIND, GENERATION_RE, KPN_KIND, KPN_MODEL, LEGACY_KIND, LEGACY_MODEL, ReferenceError as ReferenceInvariant,
    archive_id as reference_archive_id, generation_of_binding, get_generation, legacy_generation_id, mechanism_node, model_of_source_id, parse_factor_key,
    public_id, read_active)
import numpy as np

LOGGER = logging.getLogger(__name__)
GENERATION_TTL_SECONDS = 5.0
READINESS_VERIFICATION_TTL_SECONDS = 60.0
VECTOR_BUILD_WAIT_SECONDS = 60.0  # a request waiting on another request's cold semantic index build
# Prefixes cut over one at a time while reference_generations.status is shared by every prefix
# of the scientific database, so a generation another prefix superseded stays servable.
SERVED_STATUSES = ('complete', 'superseded')
SERVED_KINDS = {LEGACY_KIND: LEGACY_MODEL, KPN_KIND: KPN_MODEL}
KPN_FACTOR_COLUMNS = ('factor_key', 'label', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'factor_number', 'eaggl_import_id',
                      'source_revision', 'metadata', 'phenotype_name', 'legacy_phenotype_id', 'trait_group', 'trait_type')
# Trait metadata (about 6.6 MB of the KPN cold load when joined onto each of 4,037 factors) is read once per trait.
KPN_FACTORS = ('SELECT ' + ','.join('f.' + column for column in KPN_FACTOR_COLUMNS[:9]) + ',' + ','.join('t.' + column for column in KPN_FACTOR_COLUMNS[9:])
               + ' FROM reference_factors f JOIN kpn_traits t ON t.generation_id=f.generation_id AND t.kpn_trait_id=f.kpn_trait_id'
               ' WHERE f.generation_id=%s ORDER BY f.kpn_trait_id,f.factor_number')

def check_vector_readiness(index):
    try:
        index.check()
    except VectorUnavailable as error:
        raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error


def vector_backend():
    backend = setting('REVEAL_RETRIEVAL_BACKEND', 'upstash')
    if backend not in ('upstash', 'legacy'): raise ValueError('Invalid retrieval backend')
    return backend == 'upstash'


def active_pointers(repo=None, vector=False, *, inspect=None):
    """(active reference generation or None for legacy mode, active Vector snapshot id or None).

    REVEAL_REFERENCE_GENERATION_ID pins the generation. Otherwise both pointers come from one statement:
    reference_reload flips them in one transaction, so a reader never sees a cutover half done. inspect(tx)
    runs inside that read (the readiness monitor's database check), which then happens even when pinned."""
    pinned = setting('REVEAL_REFERENCE_GENERATION_ID')
    if pinned and not GENERATION_RE.fullmatch(pinned):
        raise Problem(503, 'SOURCE_NOT_READY', 'REVEAL_REFERENCE_GENERATION_ID is not a reference generation id.')
    repo = repo or Repository()
    registry = VectorRegistry(repo) if vector else None
    if pinned and inspect is None: return pinned, registry.active_identity(required=False) if registry else None
    with repo.single_read() as tx:
        tx.get_records([(ACTIVE_KIND, ACTIVE_ID)] + ([('vector_active', registry.scope)] if registry else []))  # the reads below reuse it
        active = read_active(tx)
        if inspect: inspect(tx)
        return pinned or (active['generation_id'] if active else None), registry.active_identity_in(tx) if registry else None


def readiness_binding(generation_id, snapshot_id):
    """What readiness verifies: the active pointers plus the deployment's source pins."""
    backend = setting('REVEAL_RETRIEVAL_BACKEND', 'upstash')
    if backend == 'upstash' and not snapshot_id:
        raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', 'No verified active Vector snapshot in this environment')
    return (generation_id, backend, snapshot_id, *(setting(key) for key in
        ('REVEAL_DISMECH_IMPORT_ID', 'REVEAL_MAPPING_RUN_ID', 'REVEAL_EMBEDDING_RUN_ID')))


def missing_table(error):
    """MySQL 1146: migration 008 is not applied yet, so no generation or archived factor exists."""
    args = getattr(error, 'args', ())
    return bool(args) and args[0] == 1146


def factor_key_index(index, rows):
    """The exact (REVEAL_RETRIEVAL_BACKEND=legacy) index re-keyed from EAGGL factor ids to generation factor keys."""
    keys = {row['eaggl_factor_id']: row['factor_key'] for row in rows}
    if not set(keys) <= set(index.by_id): raise Problem(503, 'SOURCE_NOT_READY', 'The EAGGL embedding run does not cover every reference factor.')
    index = copy(index)
    index.factors = [dict(row, factor_id=keys.get(row['factor_id'], row['factor_id'])) for row in index.factors]
    index.by_id = {row['factor_id']: position for position, row in enumerate(index.factors)}
    return index


def gap_search_text(gap):
    return (gap['object']['text']+' '+gap['source']['disease_label']).casefold()


def factor_search_text(record):
    """Lexical/fuzzy text: label and source id; KPN factors add the trait name and ids."""
    text = record['cfde_anchor']['label']+' '+record['source_id']
    trait = record.get('kpn_trait')
    if trait: text += ' ' + ' '.join(value for value in (trait.get('name'), trait.get('legacy_phenotype_id'), trait.get('id')) if value)
    return text.casefold()


def archived_factor(row):
    """An archived_reference_factors row as ArchivedReferenceFactor: the frozen snapshot plus its ids."""
    identity, generation, source, snapshot, captured = row
    snapshot = json.loads(snapshot) if isinstance(snapshot, (str, bytes)) else dict(snapshot)
    if hasattr(captured, 'isoformat'):  # MySQL sessions run in UTC.
        captured = (captured if captured.tzinfo else captured.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {**snapshot, 'archive_id': identity, 'generation_id': generation, 'source_id': source, 'captured_at': captured}


class Catalog:
    # Process-wide state that a reload (load() or refresh_if_changed()) keeps when it swaps in a freshly loaded catalog.
    PROCESS_STATE = frozenset({'lock', 'dismech_catalog_lock', 'vector_lock', 'refresh_lock', 'lookup_lock', 'repo', 'generation_cache', 'archive_cache',
                               'archive_unavailable_at', 'poll_seconds', 'poller', 'poller_lock', 'poller_stop', 'poller_wake', 'readiness_lock',
                               'readiness_cache', 'observed_pointers', 'warmed', 'warmup'})
    def __init__(self, repo=None, *, poll_seconds=None):
        self.loaded = False
        self.lock = threading.Lock()
        self.readiness_lock = threading.Lock()
        self.readiness_cache = None
        self.dismech_catalog_lock=threading.Lock()
        self.complete_dismech_catalog=None
        self.vector_lock = threading.Lock()
        # Loaded state (not PROCESS_STATE): adopt() swaps them with the generation, so a request after a reload
        # never joins or publishes an index built for the previous one.
        self.vector_indexes, self.vector_builds, self.vector_snapshot_id = {}, {}, None
        # Reference generation: resolved by load(); None = legacy mode.
        self.repo = repo
        self.refresh_lock, self.lookup_lock = threading.Lock(), threading.Lock()
        self.generation_checked_at = None
        self.active_generation = self.reference_generation_id = self.model = self.generation_record = None
        self.gap_fuzzy = None  # FuzzyWords over the served gaps' search texts, built with them by _load
        # archive_cache: {archive_id: (checked_at, snapshot|None)}; archive_unavailable_at: when the table was last found missing or empty.
        self.generation_cache, self.archive_cache, self.archive_unavailable_at = {}, {}, None
        # None: no poller (tests, tools); the API polls reference_active every GENERATION_TTL_SECONDS.
        self.poll_seconds, self.poller, self.poller_lock, self.poller_stop = poll_seconds, None, threading.Lock(), threading.Event()
        # (read at, retrieval backend, generation, snapshot): the readiness monitor's latest pointer read.
        self.poller_wake, self.observed_pointers = threading.Event(), None
        self.warmed, self.warmup = threading.Event(), None  # set once a startup warmup loaded everything
    def readiness(self):
        """Verify serving pins and coverage without materializing research corpora (the synchronous path, when no
        readiness monitor runs).

        This is separate from the catalog lock: health checks must not wait for
        an unrelated browse/suggestion request to construct all DAPPER objects.
        """
        with self.readiness_lock:
            if self.readiness_cache and monotonic() - self.readiness_cache[0] < GENERATION_TTL_SECONDS:
                return dict(self.readiness_cache[1])
            binding = readiness_binding(*active_pointers(self.repo, vector_backend()))
            # Recheck small active pointers frequently; independently verify
            # provider health/coverage at least once a minute, even with no cutover.
            if (self.readiness_cache and self.readiness_cache[2] == binding
                    and monotonic() - self.readiness_cache[3] < READINESS_VERIFICATION_TTL_SECONDS):
                self.readiness_cache = (monotonic(), self.readiness_cache[1], binding, self.readiness_cache[3])
                return dict(self.readiness_cache[1])
            result = self.verify_binding(binding)
            self.readiness_cache = (monotonic(), result, binding, monotonic())
            return dict(result)
    def verify_binding(self, binding):
        """Check one readiness_binding against the reference tables, the verified snapshot and the Vector provider.
        The pooled reference read ends before the snapshot read, so no lease nests in another."""
        generation_id, backend, snapshot_id = binding[:3]
        try:
            connection = reference_mysql_connection()
            try:
                generation = self.select_generation(connection, generation_id) if generation_id else None
                kpn = bool(generation) and generation['kind'] == KPN_KIND
                with connection.cursor() as cursor:
                    cursor.execute("SELECT import_id FROM dismech_imports WHERE status='complete'")
                    imports = [row[0] for row in cursor.fetchall() if not setting('REVEAL_DISMECH_IMPORT_ID')
                        or row[0] == setting('REVEAL_DISMECH_IMPORT_ID')]
                    if generation and generation['dismech_import_id']:
                        imports = [identity for identity in imports if identity == generation['dismech_import_id']]
                    if len(imports) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed DisMech import.')
                    dismech = imports[0]
                    if kpn:
                        mapping, eaggl, embedding = generation_id, generation['eaggl_import_id'], generation['eaggl_embedding_run_id']
                        cursor.execute('SELECT COUNT(*) FROM reference_factors WHERE generation_id=%s', (generation_id,))
                    else:
                        cursor.execute("SELECT run_id,eaggl_import_id FROM eaggl_cfde_link_runs WHERE status='complete'")
                        mappings = [row for row in cursor.fetchall() if not setting('REVEAL_MAPPING_RUN_ID')
                            or row[0] == setting('REVEAL_MAPPING_RUN_ID')]
                        if generation: mappings = [row for row in mappings if row[0] == generation['legacy_mapping_run_id']]
                        if len(mappings) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed EAGGL mapping run.')
                        mapping, eaggl = mappings[0]
                        embedding = generation['eaggl_embedding_run_id'] if generation else None
                        cursor.execute('SELECT COUNT(*) FROM eaggl_cfde_factor_links WHERE run_id=%s', (mapping,))
                    factors = cursor.fetchone()[0]
                    if not factors: raise Problem(503, 'SOURCE_NOT_READY', 'The selected reference has no factors.')
                    cursor.execute("SELECT run_id FROM eaggl_embedding_runs WHERE import_id=%s AND status='complete'", (eaggl,))
                    embeddings = [row[0] for row in cursor.fetchall() if (not embedding or row[0] == embedding)
                        and (kpn or not setting('REVEAL_EMBEDDING_RUN_ID') or row[0] == setting('REVEAL_EMBEDDING_RUN_ID'))]
                    if len(embeddings) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed embedding run.')
                    embedding = embeddings[0]
                    cursor.execute('SELECT COUNT(*) FROM dismech_discussions WHERE import_id=%s AND is_gap=1', (dismech,))
                    gaps = cursor.fetchone()[0]
            finally: connection.close()
            if backend == 'upstash':
                snapshot = VectorRegistry(self.repo).serving(snapshot_id, summary=True)
                pinned = (snapshot.get('reference_generation_id') == generation_id if kpn else snapshot['mapping_run'] == mapping)
                if (not pinned or snapshot['run']['run_id'] != embedding or snapshot['run']['config']['import_id'] != eaggl
                        or snapshot['dismech_import'] != dismech or snapshot['_serving_counts'].get('factors') != factors):
                    raise VectorUnavailable('Active Vector snapshot differs from selected source runs')
                check_snapshot_readiness(snapshot)
            else:
                # The optional local-vector backend has no verified serving
                # manifest, so preserve its full compatibility checks.
                self.load()
        except VectorUnavailable as error:
            raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
        return {'dismech_import': dismech, 'gaps': gaps, 'mapping_run': mapping,
            'mapped_factors': factors, 'embedding_run': embedding}
    def load(self):
        """Serve the loaded generation without I/O; a cold load (or one after a failed reload) reads the active generation.

        Callers may hold a pooled write transaction, so a loaded catalog never touches the database
        here: generation changes are picked up by the background poller (refresh_if_changed).
        """
        with self.lock:
            if self.loaded: return
            try: generation, snapshot = active_pointers(self.repo, vector_backend())
            except Problem: raise
            except VectorUnavailable as error: raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
            except Exception as error:
                raise Problem(503, 'SOURCE_NOT_READY', 'The active reference generation is unavailable.') from error
            checked = monotonic()
            # Always load beside the current state: a failed reload leaves the previous generation's
            # state (e.g. its DisMech context run) behind, and a failed load must leave nothing half-set.
            fresh = self.fresh()
            fresh._load(generation, snapshot)
            fresh.generation_checked_at = checked
            self.adopt(fresh)
        self.start_poller()
    def warm(self):
        """Load the catalog and the full DisMech corpus off the request path (API startup). Never raises and sets
        nothing on failure: the first request then loads inline and fails closed, as without a warmup. Nothing
        built here is persisted."""
        try:
            self.load(); self.dismech_catalog()
        except Exception:
            LOGGER.warning('Catalog warmup failed; the first request loads the catalog', exc_info=True)
            return False
        self.warmed.set(); return True
    def start_warmup(self):
        """Run warm() once per process on its own thread; requests that arrive meanwhile wait only for its remainder."""
        with self.poller_lock:
            if self.warmup and self.warmup[0] == os.getpid() and self.warmup[1].is_alive(): return
            thread = threading.Thread(target=self.warm, name='catalog-warmup', daemon=True)
            self.warmup = (os.getpid(), thread); thread.start()
    def fresh(self):
        fresh = type(self)(self.repo)
        if getattr(self, 'runtime', None) is not None: fresh.runtime = self.runtime  # The DAPPER runtime is process-wide.
        return fresh
    def adopt(self, fresh):
        """Replace the serving state with a loaded catalog's (caller holds self.lock)."""
        with self.vector_lock:
            self.__dict__.update({key: value for key, value in fresh.__dict__.items() if key not in self.PROCESS_STATE})
    def observe_pointers(self, generation, snapshot, at=None):
        """Share a pointer read (the readiness monitor's) with the poller, which then needs no read of its own."""
        self.observed_pointers = (monotonic() if at is None else at, setting('REVEAL_RETRIEVAL_BACKEND', 'upstash'), generation, snapshot)
        self.poller_wake.set()
    def refresh_if_changed(self):
        """Reload when the active reference generation or Vector snapshot changed; checked at most every TTL.

        Runs on the poller thread, never on the request path. A newer readiness-monitor observation replaces
        the read; without one for two TTLs the poller reads itself. The new generation loads beside the
        serving state, which keeps serving meanwhile, and replaces it at once. A failed check keeps
        serving; a failed reload fails closed (loaded=False, so the next load() retries and raises
        until the generation loads).
        """
        checked = self.generation_checked_at
        if not self.loaded or checked is None: return False
        if not self.refresh_lock.acquire(blocking=False): return False
        try:
            checked = self.generation_checked_at
            observed = self.observed_pointers; now_ = monotonic()
            if observed and observed[1] != setting('REVEAL_RETRIEVAL_BACKEND', 'upstash'): observed = None
            if not self.loaded: return False
            if observed and observed[0] > checked and now_ - observed[0] <= GENERATION_TTL_SECONDS:
                self.generation_checked_at = observed[0]; generation, snapshot = observed[2:]
            else:
                if now_ - checked < GENERATION_TTL_SECONDS * (2 if observed and now_ - observed[0] < 2 * GENERATION_TTL_SECONDS else 1):
                    return False
                self.generation_checked_at = now_
                try: generation, snapshot = active_pointers(self.repo, vector_backend())
                except Exception:
                    LOGGER.warning('Reference generation check failed; serving the loaded generation', exc_info=True)
                    return False
                finally: self.generation_checked_at = monotonic()  # A slow check never re-triggers at once.
            if generation == self.active_generation and snapshot == self.vector_snapshot_id: return False
            fresh = self.fresh()
            try: fresh._load(generation, snapshot)
            except BaseException:
                with self.lock: self.loaded = False
                raise
            fresh.generation_checked_at = self.generation_checked_at
            with self.lock: self.adopt(fresh)
            LOGGER.info('Reference generation %s or Vector snapshot changed; catalog reloaded', generation or 'legacy')
            return True
        finally: self.refresh_lock.release()
    def start_poller(self):
        """Start (once per process) the thread that re-checks the active pointers every poll_seconds, or as
        soon as the readiness monitor shares a read.

        It holds no request's lease or lock, so its pooled read may wait without stalling writers
        or the event loop. A failed reload is logged; the next load() then reloads inline, fail-closed.
        """
        if not self.poll_seconds: return
        with self.poller_lock:
            if self.poller and self.poller[0] == os.getpid() and self.poller[1].is_alive(): return
            def run():
                while not self.poller_stop.is_set():
                    self.poller_wake.wait(self.poll_seconds); self.poller_wake.clear()
                    if self.poller_stop.is_set(): return
                    try: self.refresh_if_changed()
                    except Exception: LOGGER.warning('Reference generation refresh failed; the next request reloads', exc_info=True)
            thread = threading.Thread(target=run, name='reference-generation-poll', daemon=True)
            self.poller = (os.getpid(), thread); thread.start()
    def stop_poller(self, timeout=None):
        self.poller_stop.set(); self.poller_wake.set()
        if self.poller and self.poller[1].is_alive(): self.poller[1].join(timeout)
    def select_generation(self, connection, generation_id):
        """The active generation's reference_generations row; only a loaded, servable generation qualifies."""
        try: generation = get_generation(connection, generation_id)
        except Exception as error:
            if not missing_table(error): raise
            generation = None
        if (not generation or generation['status'] not in SERVED_STATUSES or SERVED_KINDS.get(generation['kind']) != generation['model']
                or (generation['kind'] == LEGACY_KIND and (not GENERATION_RE.fullmatch(generation['legacy_mapping_run_id'] or '')
                                                            or legacy_generation_id(generation['legacy_mapping_run_id']) != generation_id))
                or (generation['kind'] == KPN_KIND and not (generation['eaggl_import_id'] and generation['eaggl_embedding_run_id']))):
            raise Problem(503, 'SOURCE_NOT_READY', 'Select one loaded reference generation.')
        return generation
    def generation_factors(self, cursor, generation):
        """KPN mode: every factor of the generation joined to its KPN trait, identities checked."""
        identity, self.eaggl_import = generation['generation_id'], generation['eaggl_import_id']
        cursor.execute("SELECT run_id FROM eaggl_embedding_runs WHERE run_id=%s AND import_id=%s AND status='complete'",
                       (generation['eaggl_embedding_run_id'], self.eaggl_import))
        if len(cursor.fetchall()) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed embedding run.')
        # KPN bindings carry the generation where legacy ones carry the mapping run and gene-set import.
        self.mapping_run = self.geneset_import = identity
        self.embedding_run = generation['eaggl_embedding_run_id']
        cursor.execute(KPN_FACTORS, (identity,))
        rows = [dict(zip(KPN_FACTOR_COLUMNS, row)) for row in cursor.fetchall()]
        if not rows: raise Problem(503, 'SOURCE_NOT_READY', 'The reference generation has no factors.')
        cursor.execute('SELECT kpn_trait_id,metadata FROM kpn_traits WHERE generation_id=%s', (identity,))
        # One parsed dict per trait, shared by its factors: kpn_factors copies what it keeps and never mutates it.
        traits = {trait: json.loads(metadata) if isinstance(metadata, (str, bytes)) else metadata for trait, metadata in cursor.fetchall()}
        for row in rows: row['trait_metadata'] = traits.get(row['kpn_trait_id'])
        for row in rows:
            try: row['factor'] = parse_factor_key(row['factor_key'])['factor']
            except ReferenceInvariant: row['factor'] = None
            if (not row['factor'] or row['factor_key'] != f"{row['kpn_trait_id']}::{row['factor']}" or row['public_id'] != public_id(row['kpn_trait_id'], row['factor'])
                    or row['eaggl_import_id'] not in (None, self.eaggl_import)):
                raise Problem(503, 'SOURCE_NOT_READY', 'Reference generation factor identities are inconsistent.')
            if isinstance(row['metadata'], (str, bytes)): row['metadata'] = json.loads(row['metadata'])
        return rows
    def _load(self, generation_id, vector_snapshot_id=None):
        """Load generation_id; with the Upstash backend, serve vector_snapshot_id (read with it by active_pointers)."""
        runtime = getattr(self, 'runtime', None) or DapperRuntime(CURRENT_DAPPER_SNAPSHOT)
        self.runtime = runtime
        connection = mysql_connection()
        try:
            generation = self.select_generation(connection, generation_id) if generation_id else None
            kpn = bool(generation) and generation['kind'] == KPN_KIND
            with connection.cursor() as cursor:
                cursor.execute("SELECT import_id,source_commit,source_files FROM dismech_imports WHERE status='complete'")
                imports = cursor.fetchall()
                requested = setting('REVEAL_DISMECH_IMPORT_ID')
                imports = [r for r in imports if not requested or r[0] == requested]
                # An active generation also pins the DisMech import that its archived gap ids depend on.
                if generation and generation['dismech_import_id']: imports = [r for r in imports if r[0] == generation['dismech_import_id']]
                if len(imports) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed DisMech import.')
                self.dismech_import, self.source_commit, files = imports[0]
                files = json.loads(files)
                # Import source_files stores both source and gap manifests.
                if isinstance(files, dict): files = files.get('source', files.get('gaps', []))
                self.file_hashes = files if isinstance(files, dict) else {r['path']: r['sha256'] for r in files}
                cursor.execute('SELECT source_file,source_sha256 FROM dismech_documents WHERE import_id=%s', (self.dismech_import,))
                self.file_hashes.update(dict(cursor.fetchall()))
                cursor.execute('SELECT payload FROM dismech_discussions WHERE import_id=%s AND is_gap=1', (self.dismech_import,))
                gap_rows = [json.loads(r[0]) for r in cursor.fetchall()]
                cursor.execute('SELECT m.payload FROM dismech_mechanisms m WHERE m.import_id=%s AND EXISTS (SELECT 1 FROM dismech_gap_attachments a WHERE a.import_id=m.import_id AND a.target_mechanism_sha256=m.id_sha256)', (self.dismech_import,))
                mechanism_rows = [json.loads(r[0]) for r in cursor.fetchall()]
                cursor.execute('SELECT payload FROM dismech_gap_attachments WHERE import_id=%s ORDER BY attachment_index', (self.dismech_import,))
                attachments = defaultdict(list)
                for r in cursor.fetchall():
                    item = json.loads(r[0]); attachments[item['gap_id']].append(item)
                if kpn: factor_rows = self.generation_factors(cursor, generation)
                else:
                    cursor.execute("SELECT run_id,eaggl_import_id,gene_set_import_id FROM eaggl_cfde_link_runs WHERE status='complete'")
                    runs = [r for r in cursor.fetchall() if not setting('REVEAL_MAPPING_RUN_ID') or r[0] == setting('REVEAL_MAPPING_RUN_ID')]
                    # An active legacy generation (e.g. a rollback) pins its own mapping and embedding runs.
                    if generation: runs = [r for r in runs if r[0] == generation['legacy_mapping_run_id']]
                    if len(runs) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed EAGGL mapping run.')
                    self.mapping_run, self.eaggl_import, self.geneset_import = runs[0]
                    cursor.execute("SELECT run_id FROM eaggl_embedding_runs WHERE import_id=%s AND status='complete'", (self.eaggl_import,))
                    runs = [r[0] for r in cursor.fetchall() if not setting('REVEAL_EMBEDDING_RUN_ID') or r[0] == setting('REVEAL_EMBEDDING_RUN_ID')]
                    if generation and generation['eaggl_embedding_run_id']: runs = [r for r in runs if r == generation['eaggl_embedding_run_id']]
                    if len(runs) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed embedding run.')
                    self.embedding_run = runs[0]
                    cursor.execute('SELECT f.factor_id,f.label,l.cfde_node_id,l.payload FROM eaggl_cfde_factor_links l JOIN eaggl_factors f ON f.import_id=l.eaggl_import_id AND f.factor_index=l.factor_index WHERE l.run_id=%s', (self.mapping_run,))
                    factor_rows = cursor.fetchall()
            self.active_generation, self.generation_record = generation_id, generation
            self.model = KPN_MODEL if kpn else LEGACY_MODEL
            self.reference_generation_id = generation_id or (legacy_generation_id(self.mapping_run) if GENERATION_RE.fullmatch(str(self.mapping_run)) else None)
            self.vector_backend = vector_backend()
            context_ids = {row['id'] for row in mechanism_rows}
            inputs = [context_input(row['id'], 'mechanism', self.file_hashes[row['source_file']],
                row.get('description') or row['name']) for row in mechanism_rows]
            inputs.extend(context_input(row['id'], 'knowledge_gap', self.file_hashes[row['source_file']], row['raw']['prompt'])
                for row in gap_rows if not any(item.get('target_id') in context_ids for item in attachments[row['id']]))
            self.context_source_ids = tuple(row['source_id'] for row in inputs)
            self.expected_context_inputs = tuple(inputs)
            self.vector_snapshot_id = vector_snapshot_id if self.vector_backend else None
            if self.vector_backend:
                # Browsing and freezing selected records need their native source
                # bindings, not a semantic index. Verify aliases/contexts on the
                # first actual semantic request, before returning any matches.
                self.index = None
                self.expected_factor_aliases = ({(row['factor_key'], row['label'], row['public_id']) for row in factor_rows} if kpn
                    else {(row[0], row[1], row[2]) for row in factor_rows})
                try:
                    if not vector_snapshot_id: raise VectorUnavailable('No verified active Vector snapshot in this environment')
                    summary = VectorRegistry(self.repo).serving(vector_snapshot_id, summary=True)
                except VectorUnavailable as error:
                    raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
                pinned = (summary.get('reference_generation_id') == self.reference_generation_id if kpn
                    else summary['mapping_run'] == self.mapping_run)
                if (not pinned or summary['run']['run_id'] != self.embedding_run
                        or summary['run']['config']['import_id'] != self.eaggl_import
                        or summary['dismech_import'] != self.dismech_import
                        or summary['_serving_counts']['factors'] != len(self.expected_factor_aliases)):
                    raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', 'Active Vector snapshot differs from selected source runs')
                try: check_snapshot_readiness(summary)
                except VectorUnavailable as error:
                    raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
            else:
                self.index = database_search_index(connection, self.eaggl_import, self.embedding_run)
                if kpn: self.index = factor_key_index(self.index, factor_rows)
            # Actual catalog use loads only bindings used by these gaps. The
            # provider still returns exact selected vectors on demand.
            try:
                if self.vector_backend:
                    self.dismech_embeddings = {'run_id': summary['context_run_id'], 'config': summary['context_config']}
                    self.context_text_vectors = {}
                else:
                    self.dismech_embeddings = load_context_vectors(connection, self.dismech_import, self.index.run, inputs,
                        run_id=setting('REVEAL_DISMECH_EMBEDDING_RUN_ID'))
                    self.context_text_vectors = {row['input_sha256']: (row['input_text'], self.dismech_embeddings['vectors'][identity])
                        for identity, row in self.dismech_embeddings['bindings'].items()}
            except Exception as error:
                raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY',
                    'Import and verify compatible DisMech context embeddings before automatic retrieval.') from error
        finally: connection.close()
        self.mechanisms, self.gaps, self.by_source, self.bindings = {}, {}, {}, {}
        for row in mechanism_rows:
            node = {'name': row['name'], 'description': row.get('description') or row['name']}
            node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
            self.mechanisms[row['id']] = {'source': 'dismech', 'source_id': row['id'], 'source_revision': self.file_hashes[row['source_file']],
                'object_class': 'Mechanism', 'object': node, 'disease_label': row.get('document_name', ''),
                'source_detail': {'source_file': row['source_file'],
                    'source_pointer': row.get('source_pointer', row.get('json_pointer')),
                    'import_id': self.dismech_import, 'source_commit': self.source_commit,
                    'payload_sha256': sha256(canonical_json(row)), 'raw': deepcopy(row)}}
        for row in gap_rows:
            raw = row['raw']
            node = {'text': raw['prompt'], 'gap_description': raw.get('rationale') or raw['prompt'], 'gap_kind': row['kind'], 'scope': row['document_name']}
            disease = (row.get('disease_term') or {}).get('term', {}).get('id')
            if disease:
                node['about_entities'] = [runtime.resolver({'MONDO': 'http://purl.obolibrary.org/obo/MONDO_'}).expand(disease)]
            node['id'] = runtime.compute_id(node, 'KnowledgeGap', runtime.schema)
            linked = []
            for item in attachments[row['id']]:
                target = self.mechanisms.get(item.get('target_id'))
                reference = {k: target[k] for k in ('source', 'source_id', 'source_revision')} | {'dapper_id': target['object']['id']} if target else None
                linked.append({'source_reference': item['source_reference'], 'target_kind': item.get('target_kind') or 'unknown',
                    'resolution': item['resolution'], 'target': reference, 'label': target['object']['name'] if target else None})
            gap = {'object': node, 'source': {'source': 'dismech', 'source_id': row['id'], 'source_revision': self.file_hashes[row['source_file']],
                'status': row.get('status'), 'disease_label': row['document_name'], 'description_derivation': 'source_rationale' if raw.get('rationale') else 'prompt_fallback'},
                'attachments': linked, 'source_detail': {'source_file': row['source_file'], 'source_pointer': row['source_pointer'], 'payload_sha256': sha256(canonical_json(raw)), 'raw': raw},
                'scientific_accounts': {'count': 0, 'scope': 'public_exact_gap', 'as_of': now(), 'ranking': 'curated', 'window_days': None}}
            self.gaps[node['id']] = gap; self.by_source[row['id']] = gap
        self.gap_fuzzy = FuzzyWords([gap_search_text(gap) for gap in self.gaps.values()])
        self.factors, self.factor_legacy = {}, {}
        if kpn: self.kpn_factors(runtime, factor_rows)
        else:
            for legacy, label, native, payload in factor_rows:
                raw = json.loads(payload)['raw']; trait, factor = native.split(':')[2], native.split(':')[4]
                node = {'name': f'{trait} mechanism {factor}', 'description': f'EAGGL mechanism {native}. Source label: {raw["label"]}.'}
                node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
                record = {'source': 'eaggl', 'source_id': native, 'source_revision': sha256(canonical_json(raw)), 'object_class': 'Mechanism', 'object': node,
                    'cfde_anchor': {'node_id': native, 'node_type': 'factor', 'label': label, 'subtitle': f'{trait} ({factor})'}, 'model': 'cfde-inc-v2',
                    'catalog_file': runtime.file('cfde-factor.json', canonical_json(raw), 'application/json')}
                self.factors[native] = record; self.factor_legacy[legacy] = record
                self.bindings[native] = {'eaggl_factor_id': legacy, 'eaggl_import_id': self.eaggl_import, 'embedding_run_id': self.embedding_run,
                    'mapping_run_id': self.mapping_run, 'gene_set_import_id': self.geneset_import, 'cfde_node_id': native, 'cfde_payload': raw}
        self.loaded = True
    def kpn_factors(self, runtime, rows):
        """KPN records keyed by public id (docs §4); factor_legacy keyed by factor key, the vector alias."""
        generation = self.reference_generation_id
        for row in rows:
            native, trait, factor, label, phenotype, metadata = (row[key] for key in ('public_id', 'kpn_trait_id', 'factor', 'label', 'phenotype_name', 'metadata'))
            trait_metadata = row.get('trait_metadata') or {}
            if isinstance(trait_metadata, (str, bytes)): trait_metadata = json.loads(trait_metadata)
            mappings = deepcopy(trait_metadata.get('ontology_mappings', []))
            node = mechanism_node(native, phenotype, trait, factor, label,
                identity_version=((getattr(self, 'generation_record', None) or {}).get('manifest') or {}).get('mechanism_identity_version', 1),
                eaggl_import_id=self.eaggl_import)
            node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
            record = {'source': 'eaggl', 'source_id': native, 'source_revision': row['source_revision'], 'object_class': 'Mechanism', 'object': node,
                'cfde_anchor': {'node_id': native, 'node_type': 'factor', 'label': label, 'subtitle': f'{phenotype} ({factor})'},
                'model': KPN_MODEL, 'reference_generation_id': generation,
                'kpn_trait': {'id': trait, 'name': phenotype, 'legacy_phenotype_id': row['legacy_phenotype_id'], 'trait_group': row['trait_group'], 'trait_type': row['trait_type'],
                    'ontology_mappings': mappings, 'mapping_interpretations': interpreted_mappings(mappings),
                    'mapping_policy_version': POLICY_VERSION},
                'catalog_file': runtime.file('cfde-factor.json', canonical_json(metadata), 'application/json')}
            self.factors[native] = record; self.factor_legacy[row['factor_key']] = record
            self.bindings[native] = {'eaggl_factor_id': row['eaggl_factor_id'], 'factor_key': row['factor_key'], 'kpn_trait_id': trait,
                'eaggl_import_id': self.eaggl_import, 'embedding_run_id': self.embedding_run, 'mapping_run_id': generation,
                'gene_set_import_id': generation, 'reference_generation_id': generation, 'model': KPN_MODEL,
                'cfde_node_id': native, 'cfde_payload': metadata}
    def _retrieval_binding(self):
        return (self.reference_generation_id, self.mapping_run, self.eaggl_import, self.embedding_run,
            self.dismech_import, (getattr(self, 'dismech_embeddings', None) or {}).get('run_id'), self.model)
    def retrieval_index(self):
        """The loaded snapshot's semantic index; no database read once it is built.

        The snapshot id is pinned with the loaded generation (the poller reloads on a change). One caller
        per catalog state builds a cold index while the others wait for it; a build that straddles a reload
        is never published, and a failed build is never replayed by its waiters.
        """
        if not getattr(self, 'vector_backend', False): return self.index
        try:
            with self.vector_lock:
                identity, binding = self.vector_snapshot_id, self._retrieval_binding()
                cached = self.vector_indexes.get(identity)
                if cached is not None:
                    self.index = cached; self.dismech_embeddings = cached.contexts
                    return cached
                builds = self.vector_builds; future = builds.get(identity); owner = future is None
                if owner:
                    future = builds[identity] = Future()
                    expected = (self.expected_factor_aliases, self.factor_legacy, self.expected_context_inputs,
                                getattr(self, 'context_source_ids', None))
            if owner:
                try:
                    index = self._build_index(identity, binding, *expected)
                    future.set_result(index)
                except BaseException as error:
                    future.set_exception(error); raise
                finally:
                    with self.vector_lock:
                        if builds.get(identity) is future: del builds[identity]
            else:
                try: index = future.result(timeout=VECTOR_BUILD_WAIT_SECONDS)
                except FutureTimeout: raise VectorUnavailable('The semantic index is still loading; retry the query') from None
                except Problem as error: raise Problem(error.status, error.code, error.detail) from error
                except Exception as error: raise VectorUnavailable('The semantic index could not be prepared; retry the query') from error
            with self.vector_lock:
                if self._retrieval_binding() != binding or self.vector_snapshot_id != identity:
                    raise VectorUnavailable('The reference generation changed during retrieval; retry the query')
                self.vector_indexes.setdefault(identity, index)
                while len(self.vector_indexes) > 4: self.vector_indexes.pop(next(iter(self.vector_indexes)))
                self.index = self.vector_indexes[identity]
                self.dismech_embeddings = self.index.contexts
                return self.index
        except VectorUnavailable as error:
            raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
    def _build_index(self, identity, binding, aliases, factor_legacy, context_inputs, context_ids):
        """Read and verify one snapshot's serving subset outside vector_lock (a pooled read plus a provider call)."""
        snapshot = VectorRegistry(self.repo).serving(identity, context_ids=context_ids)
        generation, mapping, eaggl, embedding, dismech, context_run, model = binding
        # KPN snapshots are pinned by their generation, legacy ones by the mapping run.
        pinned = (snapshot.get('reference_generation_id') == generation if model == KPN_MODEL
                  else snapshot['mapping_run'] == mapping)
        if (not pinned or snapshot['run']['run_id'] != embedding
                or snapshot['run']['config']['import_id'] != eaggl or snapshot['dismech_import'] != dismech
                or (context_run and snapshot['context_run_id'] != context_run)):
            raise VectorUnavailable('Active Vector snapshot differs from selected source runs')
        index = UpstashFactorIndex(snapshot)
        check_vector_readiness(index)
        actual = {(row['factor_id'], row['label'], row['native_id']) for row in index.factors}
        if actual != aliases:
            raise VectorUnavailable('Vector aliases differ from selected mapping')
        if any(row['source_revision'] != factor_legacy[row['factor_id']]['source_revision'] for row in index.factors):
            raise VectorUnavailable('Vector source revisions differ from current canonical factor payloads')
        if any(index.contexts['bindings'].get(row['source_id']) != row for row in context_inputs):
            raise VectorUnavailable('Vector context bindings differ from current source revisions/text')
        return index

    def dismech_catalog(self):
        """Load the independent full corpus only when mechanism search needs it."""
        self.load()
        with self.dismech_catalog_lock:
            if self.complete_dismech_catalog is not None: return self.complete_dismech_catalog
            connection=mysql_connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT source_id,name,description,JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_file')),JSON_UNQUOTE(JSON_EXTRACT(payload,'$.document_name')) FROM dismech_mechanisms WHERE import_id=%s",(self.dismech_import,))
                    rows=cursor.fetchall()
            finally: connection.close()
            records={}
            for identity,name,description,source_file,disease in rows:
                node={'name':name,'description':description or name}
                node['id']=self.runtime.compute_id(node,'Mechanism',self.runtime.schema)
                records[identity]={'source':'dismech','source_id':identity,'source_revision':self.file_hashes[source_file],
                    'object_class':'Mechanism','object':node,'disease_label':disease or ''}
            self.complete_dismech_catalog=records
            return records
    def gap(self, identity):
        self.load()
        result = self.gaps.get(identity) or self.by_source.get(identity)
        if not result: raise Problem(404, 'GAP_NOT_FOUND', 'This source gap is unavailable.')
        return result
    def selected(self, selection):
        gap = self.gap(selection['id'])
        if any(selection[k] != gap['source'][k] for k in ('source_id', 'source_revision')):
            raise Problem(409, 'SOURCE_REVISION_CHANGED', 'Reload the source question before submitting.')
        return gap
    def validate_composer(self, composer, submit=False):
        self.load()
        gap = self.selected(composer['source_gap']) if composer['source_gap'] else None
        if submit and (not gap or not composer['eaggl_anchors']): raise Problem(422, 'ANCHOR_REQUIRED', 'Select a source question and at least one mechanism anchor.')
        seen = set()
        for selection in composer['eaggl_anchors']:
            ref = selection['reference']; record = self.factors.get(ref['source_id'])
            if not record or record['source_revision'] != ref['source_revision'] or record['object']['id'] != ref['dapper_id'] or ref['source'] != 'eaggl':
                raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The selected mechanism binding is unavailable; select it again.')
            if ref['source_id'] in seen: raise Problem(422, 'DUPLICATE_ANCHOR', 'Select each native factor only once.')
            seen.add(ref['source_id'])
        return gap
    def binding_superseded(self, binding):
        """True when a frozen catalog binding is not of the active generation; never in legacy mode."""
        self.load()
        if self.active_generation is None: return False
        try: return generation_of_binding(binding or {}) != self.reference_generation_id
        except ReferenceInvariant: return True
    def source_superseded(self, source_id):
        """True for an EAGGL factor source_id that the active generation does not serve; never in legacy mode."""
        self.load()
        return self.active_generation is not None and source_id not in self.factors and model_of_source_id(source_id) is not None
    def generation(self, generation_id):
        """A reference_generations row (cached for the generation TTL), or None."""
        if not isinstance(generation_id, str) or not GENERATION_RE.fullmatch(generation_id): return None
        with self.lookup_lock: cached = self.generation_cache.get(generation_id)
        if cached and monotonic() - cached[0] < GENERATION_TTL_SECONDS: return deepcopy(cached[1])
        connection = mysql_connection()
        try: value = get_generation(connection, generation_id)
        except Exception as error:
            if not missing_table(error): raise
            value = None
        finally: connection.close()
        with self.lookup_lock:
            self.generation_cache.pop(generation_id, None); self.generation_cache[generation_id] = (monotonic(), value)
            while len(self.generation_cache) > 16: self.generation_cache.pop(next(iter(self.generation_cache)))
        return deepcopy(value)
    def archived_reference_factor(self, archive_id):
        """GET /v1/reference-factors/{archive_id}: a frozen factor snapshot plus its ids, or None.

        archived_reference_factors rows are immutable and never purged, so hits are cached for the
        process. The route is public, so misses are cached for the generation TTL (a later capture
        may add the row) and legacy mode, with the table missing or empty, connects at most once per TTL.
        """
        if not isinstance(archive_id, str) or not GENERATION_RE.fullmatch(archive_id): return None
        with self.lookup_lock: cached = self.archive_cache.get(archive_id)
        if cached is not None and cached[1] is None and monotonic() - cached[0] >= GENERATION_TTL_SECONDS: cached = None
        if cached is None:
            cached = (monotonic(), next(iter(self.archived_factors('archive_id=%s', (archive_id,))), None))
            with self.lookup_lock:
                self.archive_cache.pop(archive_id, None); self.archive_cache[archive_id] = cached
                while len(self.archive_cache) > 1024: self.archive_cache.pop(next(iter(self.archive_cache)))
        return deepcopy(cached[1])
    def archived_for_source(self, source_id, generation_id=None):
        """The frozen snapshot of a factor source_id: of generation_id when given, else the latest captured; or None."""
        if not isinstance(source_id, str) or not source_id: return None
        if generation_id: return self.archived_reference_factor(reference_archive_id(generation_id, source_id))
        return next(iter(self.archived_factors('source_id=%s', (source_id,))), None)
    def archived_factors(self, where, args):
        with self.lookup_lock: unavailable = self.archive_unavailable_at
        # Migration 008 absent or nothing archived yet (legacy mode): no connection until the TTL passes.
        if unavailable is not None and monotonic() - unavailable < GENERATION_TTL_SECONDS: return []
        connection = reference_mysql_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT archive_id,generation_id,source_id,snapshot,captured_at FROM archived_reference_factors WHERE '
                               + where + ' ORDER BY captured_at DESC,archive_id', args)
                rows = cursor.fetchall()
                if not rows: cursor.execute('SELECT 1 FROM archived_reference_factors LIMIT 1')
                empty = not rows and not cursor.fetchall()
        except Exception as error:
            if not missing_table(error): raise
            rows, empty = [], True
        finally: connection.close()
        with self.lookup_lock: self.archive_unavailable_at = monotonic() if empty else None
        return [archived_factor(row) for row in rows]
    def provenance(self, query, mode, semantic=False, *, index=None):
        """index: the one a request already resolved, so its hits and provenance share a snapshot."""
        index = (index if index is not None else self.retrieval_index()) if semantic else None
        corpus = (self.reference_generation_id if self.model == KPN_MODEL else self.mapping_run) if semantic else self.dismech_import
        return {'query': query, 'mode': mode, 'corpus_snapshot': corpus,
            'embedding_model': index.run['config']['model'] if semantic else None, 'embedding_revision': self.embedding_run if semantic else None,
            'template_version': 'eaggl-label-v1' if semantic else 'dismech-question-v1', 'score_aggregation': 'maximum_per_context' if semantic else None}
    def fuzzy_gaps(self, texts):
        """The fuzzy accelerator for exactly these gap search texts (rebuilt only if the served gaps changed)."""
        index = self.__dict__.get('gap_fuzzy')
        if index is None or index.texts != texts: index = self.gap_fuzzy = FuzzyWords(texts)
        return index
    def search_gaps(self, query, limit=20,mode='fuzzy'):
        self.load(); words = query.casefold().split(); gaps = list(self.gaps.values())
        texts = [gap_search_text(gap) for gap in gaps] if words else None
        scored = []; fuzzy = None
        for index, gap in enumerate(gaps):
            if not words: scored.append((1, gap)); continue
            text = texts[index]
            score = sum(w in text for w in words)/max(1,len(words))
            if not score and mode=='fuzzy':
                if fuzzy is None: fuzzy = self.fuzzy_gaps(texts).scorer(query)
                score = fuzzy(index)
            if score: scored.append((score, gap))
        scored.sort(key=lambda r: (-r[0], r[1]['source']['source_id']))
        return [{'gap': g, 'ranking': {'value': s, 'metric': 'fuzzy_similarity' if mode=='fuzzy' else 'lexical_rank', 'rank': i+1}} for i,(s,g) in enumerate(scored[:limit])]
    def search_factors(self, query, mode='semantic', limit=20, exclude=(), *, query_vector=None, _index=None, provenance=True):
        """provenance=False omits each semantic hit's retrieval record (search responses never return it)."""
        self.load()
        if mode in ('semantic', 'hybrid') and query.strip():
            index = _index if _index is not None else self.retrieval_index()
            try:
                vector = query_vector if query_vector is not None else self.runtime_query_vectors([query.strip()], index=index)[0]
                # Hybrid retains RRF over semantic and lexical ranks. The ANN
                # semantic leg is explicitly bounded and recorded in provenance.
                semantic_limit = min(len(self.factors), index.candidate_limit) if mode == 'hybrid' else limit
                semantic = retrieve_native(index, self.factor_legacy, np.asarray([vector]), semantic_limit, exclude, provenance=provenance)
                items = [{'record': row['record'], 'ranking': {'value': row['value'], 'metric': 'cosine_similarity', 'rank': rank},
                          **({'retrieval': {**row['retrieval'], 'query_inputs': [{'context_id': 'search_query',
                              'input_sha256': sha256((query if query_vector is not None else query.strip()).encode('utf-8'))}]}}
                             if 'retrieval' in row else {})}
                         for rank, row in enumerate(semantic, 1)]
                if mode == 'semantic': return items
                combined = {item['record']['source_id']: [1/(60+item['ranking']['rank']), item['record'], item.get('retrieval')]
                            for item in items}
                for item in self.search_factors(query, 'lexical', len(self.factors), exclude):
                    identity = item['record']['source_id']
                    if identity not in combined: combined[identity] = [0, item['record'], None]
                    combined[identity][0] += 1/(60+item['ranking']['rank'])
                ranked = sorted(combined.values(), key=lambda row: (-row[0], row[1]['source_id']))[:limit]
                return [{'record': record, 'ranking': {'value': score, 'metric': 'reciprocal_rank_fusion', 'rank': rank},
                         **({'retrieval': provenance} if provenance else {})}
                        for rank, (score, record, provenance) in enumerate(ranked, 1)]
            except VectorUnavailable as error:
                raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
        candidates = []
        for factor in self.factors.values():
            text = factor_search_text(factor)
            score = sum(word in text for word in query.casefold().split())/max(1,len(query.split())) if query else 1
            if mode=='fuzzy' and query and not score:
                score=max((SequenceMatcher(None,query.casefold(),word).ratio() for word in text.split()),default=0)
                if score<0.7: score=0
            if score and factor['source_id'] not in exclude: candidates.append((score, factor))
        candidates.sort(key=lambda row: (-row[0], row[1]['source_id']))
        metric = 'fuzzy_similarity' if mode=='fuzzy' else 'lexical_rank'
        return [{'record': record, 'ranking': {'value': score, 'metric': metric, 'rank': rank}}
                for rank, (score, record) in enumerate(candidates[:limit], 1)]

    def stored_context_inputs(self, contexts, *, stored=None):
        if stored is None and getattr(self, 'vector_backend', False): stored = self.retrieval_index().contexts
        stored = stored if stored is not None else getattr(self, 'dismech_embeddings', None)
        if stored is None:
            raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY', 'Compatible imported DisMech context vectors are unavailable.')
        inputs = []
        for identity, text in contexts:
            if identity in self.mechanisms:
                source = self.mechanisms[identity]
                row = context_input(source['source_id'], 'mechanism', source['source_revision'], text)
            elif identity in self.gaps:
                source = self.gaps[identity]['source']
                row = context_input(source['source_id'], 'knowledge_gap', source['source_revision'], text)
            else:
                raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY', 'The selected source context has no imported vector binding.')
            if stored['bindings'].get(row['source_id']) != row or row['source_id'] not in stored['vectors']:
                raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY', 'The imported context vector does not match the exact source revision and text.')
            inputs.append(row)
        return inputs

    def context_embedding_provenance(self, contexts, *, index=None):
        stored = index.contexts if isinstance(index, UpstashFactorIndex) else None
        inputs = self.stored_context_inputs(contexts, stored=stored)
        stored = stored if stored is not None else self.dismech_embeddings
        return {'dismech_embedding_run_id': stored['run_id'], 'dismech_import_id': self.dismech_import,
                'context_embedding_templates': stored['config']['templates'],
                'context_embedding_inputs': [{key: value for key, value in row.items() if key != 'input_text'} for row in inputs]}

    def runtime_query_vectors(self, texts, *, index=None):
        index = index if index is not None else self.retrieval_index()
        if isinstance(index, UpstashFactorIndex): return index.query_vectors(texts, embedder=get_embeddings)
        imported = getattr(self, 'context_text_vectors', {})
        known = [imported.get(sha256(text.encode('utf-8'))) for text in texts]
        missing = [i for i, (text, row) in enumerate(zip(texts, known)) if row is None or row[0] != text]
        resolved = {i: row[1] for i, row in enumerate(known) if i not in missing}
        if missing:
            fresh = index.query_vectors([texts[i] for i in missing], embedder=get_embeddings)
            resolved.update(zip(missing, fresh))
        return np.stack([resolved[i] for i in range(len(texts))])

    def disease_factors(self, gap, remaining, exclude):
        """Eligible exact disease identity is a retrieval reason, never scientific support."""
        self.load()
        diseases = {normalize_disease_id(value) for value in gap['object'].get('about_entities', [])} - {None}
        if not diseases or remaining <= 0: return []
        candidates = []
        for native, factor in sorted(self.factors.items()):
            if native in exclude: continue
            trait = factor.get('kpn_trait', {})
            # Re-evaluate raw mappings with this policy rather than trusting an old derived flag.
            matches = [item for item in interpreted_mappings(trait.get('ontology_mappings', []))
                       if item['identity_eligible'] and item['normalized_target_id'] in diseases]
            if not matches: continue
            matched = matches[0]
            candidates.append({'record': factor, 'ranking': {'value': 1, 'metric': 'eligible_disease_identity', 'rank': len(candidates)+1},
                'contexts': [gap['object']['id']], 'reason': 'Pinned trait mapping matches the selected disease '+matched['normalized_target_id']+'. Inspect for relevance; this is not biological support.',
                'retrieval': {'strategy': 'disease_identity', 'policy_version': POLICY_VERSION,
                    'reference_generation_id': factor.get('reference_generation_id'), 'trait_id': trait.get('id'),
                    'mapping_index': matched['mapping_index'], 'normalized_target_id': matched['normalized_target_id']}})
        return candidates[:remaining]

    def suggest_factors(self,contexts,mode,remaining,exclude,*,precomputed=False,index=None):
        self.load()
        if not remaining: return []
        index = (index if index is not None else self.retrieval_index()) if mode in ('semantic', 'hybrid') else None
        vectors = None
        query_inputs = [{'context_id': identity, 'input_sha256': sha256(text.encode('utf-8'))} for identity, text in contexts]
        try:
            if mode in ('semantic', 'hybrid'):
                if precomputed:
                    stored = index.contexts if isinstance(index, UpstashFactorIndex) else self.dismech_embeddings
                    inputs = self.stored_context_inputs(contexts, stored=stored)
                    query_inputs = [{**frozen, **{key: value for key, value in row.items() if key != 'input_text'}}
                                    for frozen, row in zip(query_inputs, inputs)]
                    vectors = (index.context_vectors([row['source_id'] for row in inputs]) if isinstance(index, UpstashFactorIndex)
                               else np.stack([stored['vectors'][row['source_id']] for row in inputs]))
                else:
                    vectors = self.runtime_query_vectors([text for _, text in contexts], index=index)
            if mode == 'semantic':
                rows = retrieve_native(index, self.factor_legacy, vectors, remaining, exclude)
                items = [{'record': row['record'], 'ranking': {'value': row['value'], 'metric': 'cosine_similarity', 'rank': rank},
                    'contexts': [contexts[i][0] for i, score in enumerate(row['scores']) if np.isclose(score, row['value'])],
                    'context_similarities': {identity: float(score) for (identity, _), score in zip(contexts, row['scores'])},
                    **({'retrieval': {**row['retrieval'], 'query_inputs': query_inputs}} if 'retrieval' in row else {})} for rank, row in enumerate(rows, 1)]
                return items
            candidates = {}
            context_retrievals = {}
            for position, (identity, text) in enumerate(contexts):
                for item in self.search_factors(text, mode, 5, exclude, query_vector=None if vectors is None else vectors[position], _index=index):
                    if item.get('retrieval') and identity not in context_retrievals:
                        context_retrievals[identity] = item['retrieval']
                    native = item['record']['source_id']; previous = candidates.get(native)
                    if previous is None or item['ranking']['value'] > previous['ranking']['value']:
                        candidates[native] = {**item, 'contexts': [identity]}
            items = sorted(candidates.values(), key=lambda row: (-row['ranking']['value'], row['record']['source_id']))[:remaining]
            for rank, item in enumerate(items, 1): item['ranking']['rank'] = rank
            if vectors is not None and items:
                selected = {item['record']['source_id']: item for item in items}
                aliases = [row['factor_id'] for row in index.factors if row['factor_id'] in self.factor_legacy
                           and self.factor_legacy[row['factor_id']]['source_id'] in selected]
                scores = index.fetch_vectors(aliases) @ vectors.T
                for item in items: item['context_similarities'] = {}
                for alias, row in zip(aliases, scores):
                    item = selected[self.factor_legacy[alias]['source_id']]
                    for (identity, _), score in zip(contexts, row):
                        item['context_similarities'][identity] = max(item['context_similarities'].get(identity, -1), float(np.clip(score, -1, 1)))
                if isinstance(index, UpstashFactorIndex):
                    for item in items:
                        # Lexical-only hybrid winners still freeze the actual
                        # semantic vectors used to score every context/alias.
                        item['retrieval'] = {**index.provenance(), **item.get('retrieval', {}), **query_vector_provenance(vectors),
                            'context_retrievals': context_retrievals, 'query_inputs': query_inputs,
                            'aliases': [{'id': index.by_id[alias]['id'], 'original_vector_sha256': index.by_id[alias]['original_vector_sha256']}
                                for alias in aliases if self.factor_legacy[alias]['source_id'] == item['record']['source_id']]}
            return items
        except VectorUnavailable as error:
            raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
        except ValueError as error:
            raise Problem(503, 'EMBEDDING_UNAVAILABLE', 'The embedding service returned incompatible vectors.') from error
