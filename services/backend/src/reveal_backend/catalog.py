"""Aurora source adapters shared by the API and collector.

Served EAGGL factors are this environment's flat reference tables (`reveal_ref_*`, named per
application prefix by repository.application_sql): the published `reveal_ref_release` row and the
factors of `reveal_ref_factors` joined to `reveal_ref_traits`. An anchor is current iff its factor
id is served by that factor table; there are no generations, snapshots or pointers. Vectors live in
this environment's fixed Upstash namespaces (vector_retrieval). DisMech gaps and mechanisms load
from the dismech_* source tables.

load() reads the release once, on the cold load; a loaded catalog does no I/O in load(). A
background poller (Catalog(poll_seconds=...), which the API enables) re-reads the release id every
RELEASE_TTL_SECONDS off the request path, and a change reloads and swaps the catalog.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import timezone
from difflib import SequenceMatcher
import json
import logging
import os
import threading
from time import monotonic
from .auth import Problem
from .repository import Repository, application_prefix, application_sql, now
from .runtime_config import ROOT, setting, mysql_connection
from .evidence_package import DapperRuntime, canonical_json, sha256
from .embedding_client import get_embeddings
from .dismech_embeddings import TEMPLATES, context_input
from .vector_retrieval import (UpstashFactorIndex, VectorUnavailable, embedding_config, embedding_space, retrieve_native,
    query_vector_provenance)
from .reference_generation import (GENERATION_RE, KPN_MODEL, ReferenceError as ReferenceInvariant,
    mechanism_node, parse_factor_key, public_id)
import numpy as np

LOGGER = logging.getLogger(__name__)
RELEASE_TTL_SECONDS = 5.0
RELEASE_SQL = 'SELECT release_id,published_at,manifest FROM reveal_ref_release'
RELEASE_ID_SQL = 'SELECT release_id FROM reveal_ref_release'
FACTOR_COLUMNS = ('factor_key', 'label', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'factor_number', 'source_revision', 'metadata',
                  'phenotype_name', 'legacy_phenotype_id', 'trait_group', 'trait_type')
FACTORS_SQL = ('SELECT ' + ','.join('f.' + column for column in FACTOR_COLUMNS[:8]) + ',' + ','.join('t.' + column for column in FACTOR_COLUMNS[8:])
               + ' FROM reveal_ref_factors f JOIN reveal_ref_traits t ON t.kpn_trait_id=f.kpn_trait_id ORDER BY f.kpn_trait_id,f.factor_number')


def check_vector_readiness(index):
    try:
        index.check()
    except VectorUnavailable as error:
        raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error


def missing_table(error):
    """MySQL 1146: the table does not exist (yet) in this database."""
    args = getattr(error, 'args', ())
    return bool(args) and args[0] == 1146


def reference_sql(sql):
    """A reveal_ref_* statement for this environment's application prefix."""
    return application_sql(sql, application_prefix())


def utc_text(value):
    if hasattr(value, 'isoformat'):  # MySQL sessions run in UTC.
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return value


def read_release(cursor):
    """The published reveal_ref_release row; a missing table or row is 503 SOURCE_NOT_READY."""
    try:
        cursor.execute(reference_sql(RELEASE_SQL))
        rows = cursor.fetchall()
    except Exception as error:
        if not missing_table(error): raise
        rows = []
    if len(rows) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'No reference release is published for this environment.')
    identity, published, manifest = rows[0]
    if isinstance(manifest, (str, bytes)): manifest = json.loads(manifest)
    if not isinstance(identity, str) or not GENERATION_RE.fullmatch(identity) or not isinstance(manifest, dict):
        raise Problem(503, 'SOURCE_NOT_READY', 'The published reference release is malformed.')
    return {'release_id': identity, 'published_at': utc_text(published), 'manifest': manifest}


def current_release_id(repo=None):
    """The published release id, or None without a release row; read through the application pool."""
    with (repo or Repository()).read_transaction() as tx:
        rows = tx.execute(RELEASE_ID_SQL).fetchall()
    if len(rows) > 1: raise ValueError('More than one reference release row is published')
    return rows[0][0] if rows else None


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
    return {**snapshot, 'archive_id': identity, 'generation_id': generation, 'source_id': source, 'captured_at': utc_text(captured)}


class Catalog:
    # Process-wide state that a reload (load() or refresh_if_changed()) keeps when it swaps in a freshly loaded catalog.
    PROCESS_STATE = frozenset({'lock', 'dismech_catalog_lock', 'refresh_lock', 'lookup_lock', 'repo', 'archive_cache',
                               'archive_unavailable_at', 'poll_seconds', 'poller', 'poller_lock', 'poller_stop'})
    def __init__(self, repo=None, *, poll_seconds=None):
        self.loaded = False
        self.lock = threading.Lock()
        self.dismech_catalog_lock=threading.Lock()
        self.complete_dismech_catalog=None
        self.repo = repo
        self.refresh_lock, self.lookup_lock = threading.Lock(), threading.Lock()
        self.release_checked_at = None
        # The served reference release, resolved by load(), and the run id of its embedding space (embedding_space).
        self.release = self.release_id = self.reference_generation_id = self.geneset_import = None
        self.embedding_run = self.mapping_run = None
        self.model = KPN_MODEL
        # archive_cache: {archive_id: (checked_at, snapshot|None)}; archive_unavailable_at: when the table was last found missing or empty.
        self.archive_cache, self.archive_unavailable_at = {}, None
        # None: no poller (tests, tools); the API polls the release id every RELEASE_TTL_SECONDS.
        self.poll_seconds, self.poller, self.poller_lock, self.poller_stop = poll_seconds, None, threading.Lock(), threading.Event()
    def load(self):
        """Serve the loaded release without I/O; a cold load (or one after a failed reload) reads the published release.

        Callers may hold a pooled write transaction, so a loaded catalog never touches the database
        here: release changes are picked up by the background poller (refresh_if_changed).
        """
        with self.lock:
            if self.loaded: return
            checked = monotonic()
            # Always load beside the current state: a failed load must leave nothing half-set.
            fresh = self.fresh()
            fresh._load()
            fresh.release_checked_at = checked
            self.adopt(fresh)
        self.start_poller()
    def fresh(self):
        fresh = type(self)(self.repo)
        if getattr(self, 'runtime', None) is not None: fresh.runtime = self.runtime  # The DAPPER runtime is process-wide.
        return fresh
    def adopt(self, fresh):
        """Replace the serving state with a loaded catalog's (caller holds self.lock)."""
        self.__dict__.update({key: value for key, value in fresh.__dict__.items() if key not in self.PROCESS_STATE})
    def refresh_if_changed(self):
        """Reload when the published release id changed; checked at most every TTL.

        Runs on the poller thread, never on the request path. The new release loads beside the
        serving state, which keeps serving meanwhile, and replaces it at once. A failed check keeps
        serving; a failed reload fails closed (loaded=False, so the next load() retries and raises
        until the release loads).
        """
        checked = self.release_checked_at
        if not self.loaded or checked is None or monotonic() - checked < RELEASE_TTL_SECONDS: return False
        if not self.refresh_lock.acquire(blocking=False): return False
        try:
            if not self.loaded or monotonic() - self.release_checked_at < RELEASE_TTL_SECONDS: return False
            self.release_checked_at = monotonic()
            try: release = current_release_id(self.repo)
            except Exception:
                LOGGER.warning('Reference release check failed; serving the loaded release', exc_info=True)
                return False
            finally: self.release_checked_at = monotonic()  # A slow check never re-triggers at once.
            if release == self.release_id: return False
            fresh = self.fresh()
            try: fresh._load()
            except BaseException:
                with self.lock: self.loaded = False
                raise
            fresh.release_checked_at = monotonic()
            with self.lock: self.adopt(fresh)
            LOGGER.info('Reference release changed to %s; catalog reloaded', self.release_id)
            return True
        finally: self.refresh_lock.release()
    def start_poller(self):
        """Start (once per process) the thread that re-checks the release id every poll_seconds.

        It holds no request's lease or lock, so its pooled read may wait without stalling writers
        or the event loop. A failed reload is logged; the next load() then reloads inline, fail-closed.
        """
        if not self.poll_seconds: return
        with self.poller_lock:
            if self.poller and self.poller[0] == os.getpid() and self.poller[1].is_alive(): return
            def run():
                while not self.poller_stop.wait(self.poll_seconds):
                    try: self.refresh_if_changed()
                    except Exception: LOGGER.warning('Reference release refresh failed; the next request reloads', exc_info=True)
            thread = threading.Thread(target=run, name='reference-release-poll', daemon=True)
            self.poller = (os.getpid(), thread); thread.start()
    def stop_poller(self, timeout=None):
        self.poller_stop.set()
        if self.poller and self.poller[1].is_alive(): self.poller[1].join(timeout)
    def release_factors(self, cursor):
        """Every served factor joined to its KPN trait, identities checked."""
        try:
            cursor.execute(reference_sql(FACTORS_SQL))
            rows = [dict(zip(FACTOR_COLUMNS, row)) for row in cursor.fetchall()]
        except Exception as error:
            if not missing_table(error): raise
            raise Problem(503, 'SOURCE_NOT_READY', 'The reference factor tables are not loaded.') from error
        if not rows: raise Problem(503, 'SOURCE_NOT_READY', 'The reference release has no factors.')
        for row in rows:
            try:
                row['factor'] = parse_factor_key(row['factor_key'])['factor']
                consistent = (row['factor_key'] == f"{row['kpn_trait_id']}::{row['factor']}"
                              and row['public_id'] == public_id(row['kpn_trait_id'], row['factor']))
            except ReferenceInvariant: consistent = False
            if not consistent: raise Problem(503, 'SOURCE_NOT_READY', 'Reference release factor identities are inconsistent.')
            if isinstance(row['metadata'], (str, bytes)): row['metadata'] = json.loads(row['metadata'])
        return rows
    def vector_index(self, factor_rows):
        """The environment's Upstash index for the served factor keys, in the release's embedding dimensions."""
        embedding = self.release['manifest'].get('embedding') or {}
        published = embedding.get('model')
        if published and published != embedding_config()['model']:
            LOGGER.warning('The configured query embedding model differs from the reference release embedding model')
        try:
            index = UpstashFactorIndex([row['factor_key'] for row in factor_rows], dimensions=embedding.get('dimensions'), release_id=self.release_id,
                                       embedding_run=self.embedding_run)
        except VectorUnavailable as error:
            raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
        check_vector_readiness(index)
        return index
    def _load(self):
        runtime = getattr(self, 'runtime', None) or DapperRuntime(ROOT / 'data/dapper/2026-09-24-v8')
        self.runtime = runtime
        connection = mysql_connection()
        try:
            # One connection, one read transaction: the release row and its factors are read consistently.
            with connection.cursor() as cursor:
                release = read_release(cursor)
                cursor.execute("SELECT import_id,source_commit,source_files FROM dismech_imports WHERE status='complete'")
                imports = cursor.fetchall()
                requested = setting('REVEAL_DISMECH_IMPORT_ID')
                imports = [r for r in imports if not requested or r[0] == requested]
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
                factor_rows = self.release_factors(cursor)
            self.release, self.release_id = release, release['release_id']
            # Bindings keep their field names: the generation and gene-set fields name the release, the run fields its
            # embedding space (vector_retrieval.embedding_space), so a release that only adds gene sets keeps every run.
            self.reference_generation_id = self.geneset_import = self.release_id
            self.mapping_run = self.embedding_run = embedding_space(release['manifest'].get('embedding'))
            self.eaggl_import = release['manifest'].get('eaggl_import_id')
            self.index = self.vector_index(factor_rows)
        finally: connection.close()
        self.mechanisms, self.gaps, self.by_source, self.bindings = {}, {}, {}, {}
        for row in mechanism_rows:
            node = {'name': row['name'], 'description': row.get('description') or row['name']}
            node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
            self.mechanisms[row['id']] = {'source': 'dismech', 'source_id': row['id'], 'source_revision': self.file_hashes[row['source_file']],
                'object_class': 'Mechanism', 'object': node, 'disease_label': row.get('document_name', '')}
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
        self.factors, self.factor_legacy = {}, {}
        self.kpn_factors(runtime, factor_rows)
        self.loaded = True
    def kpn_factors(self, runtime, rows):
        """KPN records keyed by public id (docs §4); factor_legacy keyed by factor key, the vector id."""
        release = self.reference_generation_id
        for row in rows:
            native, trait, factor, label, phenotype, metadata = (row[key] for key in ('public_id', 'kpn_trait_id', 'factor', 'label', 'phenotype_name', 'metadata'))
            node = mechanism_node(native, phenotype, trait, factor, label)
            node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
            record = {'source': 'eaggl', 'source_id': native, 'source_revision': row['source_revision'], 'object_class': 'Mechanism', 'object': node,
                'cfde_anchor': {'node_id': native, 'node_type': 'factor', 'label': label, 'subtitle': f'{phenotype} ({factor})'},
                'model': KPN_MODEL, 'reference_generation_id': release,
                'kpn_trait': {'id': trait, 'name': phenotype, 'legacy_phenotype_id': row['legacy_phenotype_id'], 'trait_group': row['trait_group'], 'trait_type': row['trait_type']},
                'catalog_file': runtime.file('cfde-factor.json', canonical_json(metadata), 'application/json')}
            self.factors[native] = record; self.factor_legacy[row['factor_key']] = record
            self.bindings[native] = {'eaggl_factor_id': row['eaggl_factor_id'], 'factor_key': row['factor_key'], 'kpn_trait_id': trait,
                'eaggl_import_id': getattr(self, 'eaggl_import', None), 'embedding_run_id': self.embedding_run, 'mapping_run_id': self.mapping_run,
                'gene_set_import_id': release, 'reference_generation_id': release, 'model': KPN_MODEL,
                'cfde_node_id': native, 'cfde_payload': metadata}
    def retrieval_index(self):
        return self.index

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
    def archived_reference_factor(self, archive_id):
        """GET /v1/reference-factors/{archive_id}: a frozen factor snapshot plus its ids, or None.

        archived_reference_factors rows are immutable and never purged, so hits are cached for the
        process. The route is public, so misses are cached for the TTL (a later capture may add the
        row), and while the table is missing or empty it connects at most once per TTL.
        """
        if not isinstance(archive_id, str) or not GENERATION_RE.fullmatch(archive_id): return None
        with self.lookup_lock: cached = self.archive_cache.get(archive_id)
        if cached is not None and cached[1] is None and monotonic() - cached[0] >= RELEASE_TTL_SECONDS: cached = None
        if cached is None:
            cached = (monotonic(), next(iter(self.archived_factors('archive_id=%s', (archive_id,))), None))
            with self.lookup_lock:
                self.archive_cache.pop(archive_id, None); self.archive_cache[archive_id] = cached
                while len(self.archive_cache) > 1024: self.archive_cache.pop(next(iter(self.archive_cache)))
        return deepcopy(cached[1])
    def archived_for_source(self, source_id):
        """The latest frozen snapshot of a factor source_id, or None."""
        if not isinstance(source_id, str) or not source_id: return None
        return next(iter(self.archived_factors('source_id=%s', (source_id,))), None)
    def archived_factors(self, where, args):
        with self.lookup_lock: unavailable = self.archive_unavailable_at
        # Table absent or nothing archived yet: no connection until the TTL passes.
        if unavailable is not None and monotonic() - unavailable < RELEASE_TTL_SECONDS: return []
        connection = mysql_connection()
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
    def provenance(self, query, mode, semantic=False):
        return {'query': query, 'mode': mode, 'corpus_snapshot': self.release_id if semantic else self.dismech_import,
            'embedding_model': embedding_config()['model'] if semantic else None, 'embedding_revision': self.release_id if semantic else None,
            'template_version': 'eaggl-label-v1' if semantic else 'dismech-question-v1', 'score_aggregation': 'maximum_per_context' if semantic else None}
    def search_gaps(self, query, limit=20,mode='fuzzy'):
        self.load(); words = query.casefold().split()
        scored = []
        for gap in self.gaps.values():
            text = (gap['object']['text']+' '+gap['source']['disease_label']).casefold()
            score = sum(w in text for w in words)/max(1,len(words)) if words else 1
            if words and not score and mode=='fuzzy':
                score = max((SequenceMatcher(None, query.casefold(), word).ratio() for word in text.split()), default=0)
                if score < 0.7: score = 0
            if score: scored.append((score, gap))
        scored.sort(key=lambda r: (-r[0], r[1]['source']['source_id']))
        return [{'gap': g, 'ranking': {'value': s, 'metric': 'fuzzy_similarity' if mode=='fuzzy' else 'lexical_rank', 'rank': i+1}} for i,(s,g) in enumerate(scored[:limit])]
    def search_factors(self, query, mode='semantic', limit=20, exclude=(), *, query_vector=None, _index=None):
        self.load()
        if mode in ('semantic', 'hybrid') and query.strip():
            index = _index or self.retrieval_index()
            try:
                vector = query_vector if query_vector is not None else self.runtime_query_vectors([query.strip()], index=index)[0]
                # Hybrid retains RRF over semantic and lexical ranks. The ANN
                # semantic leg is explicitly bounded and recorded in provenance.
                semantic_limit = min(len(self.factors), index.candidate_limit) if mode == 'hybrid' else limit
                semantic = retrieve_native(index, self.factor_legacy, np.asarray([vector]), semantic_limit, exclude)
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

    def context_inputs(self, contexts):
        """Exact source bindings (id, kind, revision, template, text hash and text) of DisMech contexts."""
        inputs = []
        for identity, text in contexts:
            if identity in self.mechanisms:
                source = self.mechanisms[identity]
                row = context_input(source['source_id'], 'mechanism', source['source_revision'], text)
            elif identity in self.gaps:
                source = self.gaps[identity]['source']
                row = context_input(source['source_id'], 'knowledge_gap', source['source_revision'], text)
            else:
                raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY', 'The selected source context is not a loaded DisMech mechanism or gap.')
            inputs.append(row)
        return inputs

    def context_embedding_provenance(self, contexts):
        inputs = self.context_inputs(contexts)
        return {'dismech_embedding_run_id': self.embedding_run, 'dismech_import_id': self.dismech_import,
                'context_embedding_templates': deepcopy(TEMPLATES),
                'context_embedding_inputs': [{key: value for key, value in row.items() if key != 'input_text'} for row in inputs]}

    def runtime_query_vectors(self, texts, *, index=None):
        return (index or self.index).query_vectors(texts, embedder=get_embeddings)

    def suggest_factors(self,contexts,mode,remaining,exclude,*,precomputed=False):
        self.load()
        if not remaining: return []
        index = self.retrieval_index()
        vectors = None
        query_inputs = [{'context_id': identity, 'input_sha256': sha256(text.encode('utf-8'))} for identity, text in contexts]
        try:
            if mode in ('semantic', 'hybrid'):
                if precomputed:
                    # Source contexts: stored `<env>-contexts` vectors by text hash; a miss is embedded live.
                    inputs = self.context_inputs(contexts)
                    query_inputs = [{**frozen, **{key: value for key, value in row.items() if key != 'input_text'}}
                                    for frozen, row in zip(query_inputs, inputs)]
                    vectors = index.context_vectors([row['input_text'] for row in inputs], embedder=get_embeddings)
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
                fetched = index.fetch_vectors(aliases)
                for item in items: item['context_similarities'] = {}
                for alias in aliases:
                    if alias not in fetched: continue  # A lexical winner without a stored vector has no measured similarity.
                    item = selected[self.factor_legacy[alias]['source_id']]
                    for (identity, _), score in zip(contexts, np.asarray(fetched[alias]) @ vectors.T):
                        item['context_similarities'][identity] = max(item['context_similarities'].get(identity, -1), float(np.clip(score, -1, 1)))
                if hasattr(index, 'provenance'):
                    for item in items:
                        # Lexical-only hybrid winners still freeze the actual
                        # semantic vectors used to score every context/alias.
                        item['retrieval'] = {**index.provenance(), **item.get('retrieval', {}), **query_vector_provenance(vectors),
                            'context_retrievals': context_retrievals, 'query_inputs': query_inputs,
                            'aliases': [{'id': alias, 'vector_sha256': getattr(index, 'vector_sha256', {}).get(alias)} for alias in aliases
                                        if alias in fetched and self.factor_legacy[alias]['source_id'] == item['record']['source_id']]}
            return items
        except VectorUnavailable as error:
            raise Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', str(error)) from error
        except ValueError as error:
            raise Problem(503, 'EMBEDDING_UNAVAILABLE', 'The embedding service returned incompatible vectors.') from error
