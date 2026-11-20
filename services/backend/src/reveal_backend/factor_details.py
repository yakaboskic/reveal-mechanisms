"""Public, release-pinned factor loadings and reference gene-set provenance.

These reads use the environment's reference release tables only (reveal_ref_*, named per application prefix by
repository.application_sql): no research jobs, account graphs, embeddings or remote calls. Missing scores stay null,
never inferred from rank. A factor's loading summaries are facts of a published, immutable release, so each process
reads them once (keyed by the release id); the active gnomAD pin is re-read at most every few seconds. A request leases a
pooled reference session only for what it does not already know, and that session's first read is the published release
(reference_pin): a release the catalog has not loaded yet is refused, never read as the catalog's. A collection's parsed
lineage is kept the same way (collection_facts, keyed by release, collection and document hash), for this module and
research_data: GlyGen HZ2's payload alone is 4.9 MB of JSON.
"""
from collections import OrderedDict
from copy import deepcopy
import json
import re
import threading
from time import monotonic
from urllib.parse import quote

from .auth import Problem
from .evidence_package import decode
from .reference_generation import GENERATION_RE
from .reference_lineage import EDGE_KEY, collection_lineage, lineage_closure, lineage_index
from .reference_pin import ReleaseUnavailable, pin_release
from .repository import application_prefix, application_sql
from .runtime_config import reference_mysql_connection, setting

GENE_COVERAGE = 'All stored nonzero EAGGL gene loadings. Genes absent from this import are not assigned a zero loading.'
SET_COVERAGE = ('Retained projections, ranked within each gene-set library: the gene sets in the top 50 of their library by joint or '
                'marginal loading. Search covers this retained subset, not every gene set in the source library.')
LEGACY_COVERAGE = 'Imported ranked gene-set links only. This legacy import does not store numeric gene-set loadings.'  # research_data's legacy pins
GENE_SET_ID = re.compile(r'dapper:GeneSet\.[A-Za-z0-9_-]{32}')
GNOMAD_SORTS = {'gnomad_pli': ('c.pli', 'DESC'), 'gnomad_loeuf': ('c.loeuf', 'ASC'),
                'gnomad_mis_z': ('c.mis_z', 'DESC')}
# Links are built from ids, never fetched; REVEAL_PROVENANCE_PORTAL_URL overrides the portal.
PORTAL_URL = 'https://translator.broadinstitute.org/artifact_provenance'
# FactorDetail.trait_links: reveal_ref_factors.metadata.trait_links (reference_release.write_factor_links).
TRAIT_LINK_KEYS = ('n_phenotypes', 'n_linked', 'n_linked_kpn_traits', 'n_linked_atlas_traits', 'own_trait_q')
TOP_LINK_KEYS = ('phenotype', 'kpn_trait_id', 'name', 'phewas_q')
CACHE_ENTRIES, PIN_TTL_SECONDS = 16384, 5.0  # small tuples keyed by the immutable release id
# Parsed collection lineages (collection_facts): at most this many, holding at most this much payload JSON (all 532 of a
# release are 12.5 MB, GlyGen HZ2 4.9 MB of it; parsed with its index it is ~15 MB of memory).
LINEAGE_ENTRIES, LINEAGE_BYTES = 64, 8 << 20
_facts, _pins, _lineages, _cache_lock = OrderedDict(), {}, OrderedDict(), threading.Lock()


def _immutable(key, compute):
    """A fact of a published release; failures (including a 503 lookup) are never cached."""
    with _cache_lock:
        if key in _facts:
            _facts.move_to_end(key); return _facts[key]
    value = compute()
    with _cache_lock:
        _facts[key] = value; _facts.move_to_end(key)
        while len(_facts) > CACHE_ENTRIES: _facts.popitem(last=False)
    return value


def clear_caches():
    with _cache_lock: _facts.clear(); _pins.clear(); _lineages.clear()


def collection_facts(release_id, collection_id, document_sha256, read):
    """A collection's parsed lineage, read and parsed once per process: {'lineage' (collection_lineage), 'index' (its
    lineage_index), 'collection' (the collection object), 'document_sha256', 'curated' ({source, notes} of its curated
    provenance), 'curated_edges' (the curated was_derived_from edge keys)}, shared read-only: callers copy what they return.

    read() returns the collection's stored payload JSON (text), on the caller's lease pinned to release_id. The entry is
    kept under (release_id, collection_id, document_sha256), never without the release id, least recently used out first
    within LINEAGE_ENTRIES and LINEAGE_BYTES; a payload whose document_sha256 is not the one asked for is served, not kept."""
    key = (release_id, collection_id, document_sha256)
    cacheable = isinstance(release_id, str) and GENERATION_RE.fullmatch(release_id) and isinstance(document_sha256, str) and document_sha256
    if cacheable:
        with _cache_lock:
            if key in _lineages:
                _lineages.move_to_end(key); return _lineages[key][0]
    raw = read()
    if isinstance(raw, str): raw = raw.encode()
    payload = (decode(raw) if raw is not None else None) or {}
    lineage = collection_lineage(payload)
    curated = payload.get('curated_provenance') if isinstance(payload.get('curated_provenance'), dict) else {}
    facts = {'lineage': lineage, 'index': lineage_index(lineage), 'collection': payload.get('collection'),
             'document_sha256': payload.get('document_sha256'),
             'curated': {'source': curated.get('source'), 'notes': [item for item in curated.get('notes') or [] if isinstance(item, dict)]},
             'curated_edges': frozenset(tuple(edge.get(key) for key in EDGE_KEY)
                                        for edge in (curated.get('groups') or {}).get('was_derived_from_edges') or [] if isinstance(edge, dict))}
    if cacheable and facts['document_sha256'] == document_sha256 and len(raw or b'') <= LINEAGE_BYTES:
        with _cache_lock:
            _lineages[key] = (facts, len(raw)); _lineages.move_to_end(key)
            while len(_lineages) > LINEAGE_ENTRIES or sum(size for _, size in _lineages.values()) > LINEAGE_BYTES:
                _lineages.popitem(last=False)
    return facts


def extracted(value):
    """A JSON_EXTRACT string: JSON text in MySQL ('"..."'), the bare string in SQLite."""
    if isinstance(value, bytes): value = value.decode()
    return json.loads(value) if isinstance(value, str) and value.startswith('"') else value


class _Reads:
    """One pooled reference lease per request, opened on the first fact that is not cached. Its first read is the published
    release (reference_pin.pin_release, same connection), so every later ref_* read of the request is of `release_id`."""
    def __init__(self, release_id): self.release_id, self.connection = release_id, None
    def __call__(self):
        if self.connection is None:
            self.connection = reference_mysql_connection()
            try: pin_release(self.connection, self.release_id)
            except ReleaseUnavailable as error:
                if error.current is None: raise Problem(503, 'SOURCE_NOT_READY', 'No reference release is published for this environment.') from error
                raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The reference release has changed. Reload the factor page.') from error
        return self.connection
    def close(self):
        if self.connection is not None: self.connection.close()


def _json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else (value or {})


def _rows(reads, sql, args=()):
    with reads().cursor() as cursor:
        cursor.execute(application_sql(sql, application_prefix()), args)
        return cursor.fetchall()


def _active_gnomad(reads, prefix):
    try:
        rows = _rows(reads, 'SELECT i.import_id,i.source_version,i.source_sha256,i.source_url,i.selection_policy'
                     ' FROM gnomad_constraint_active a JOIN gnomad_constraint_imports i ON i.import_id=a.import_id'
                     ' WHERE a.table_prefix=%s AND i.status=%s', (prefix, 'ready'))
    except Exception as error:
        # A code rollout may precede this optional, additive migration. Only the
        # missing annotation table is optional; outages and other SQL errors fail.
        if not (getattr(error, 'args', ()) and error.args[0] == 1146 and 'gnomad_constraint_' in str(error)):
            raise
        rows = []
    result = dict(zip(('import_id', 'version', 'source_sha256', 'source_url', 'selection_policy'), rows[0])) if rows else None
    with _cache_lock: _pins[prefix] = (monotonic(), result)
    return result


def _gnomad(reads, import_id=None):
    """Resolve the environment's independently versioned constraint annotation (re-read at most every
    PIN_TTL_SECONDS). Pages join the pinned import explicitly, so a pin a few seconds old stays consistent; a
    caller's pin that differs from the cached one is re-read once before it is rejected."""
    if import_id is not None and import_id != 'none' and not re.fullmatch('[a-f0-9]{64}', import_id):
        raise Problem(422, 'INVALID_QUERY', 'Invalid gnomAD import pin.')
    prefix = application_prefix()
    with _cache_lock: cached = _pins.get(prefix)
    fresh = cached is None or monotonic() - cached[0] >= PIN_TTL_SECONDS
    result = _active_gnomad(reads, prefix) if fresh else cached[1]
    pinned = lambda: import_id is None or import_id == (result['import_id'] if result else 'none')
    if not pinned() and not fresh: result = _active_gnomad(reads, prefix)
    if not pinned():
        raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The gnomAD annotations have changed. Reload the factor page before loading more genes.')
    return deepcopy(result)


def _selection(catalog, source_id, generation_id=None, source_revision=None):
    catalog.load()
    # A refresh replaces all catalog state under this lock. Copy related records
    # together so an in-flight read cannot mix old and new releases.
    with catalog.lock:
        record = deepcopy(catalog.factors.get(source_id))
        binding = deepcopy(catalog.bindings.get(source_id))
        release = catalog.reference_generation_id
    if generation_id and generation_id != release:
        raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The requested reference release is no longer active. Reload the factor page.')
    if not record or not binding:
        raise Problem(404, 'NOT_FOUND', 'The factor is unavailable in the active reference release.')
    if source_revision and source_revision != record['source_revision']:
        raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The exact requested factor revision is unavailable.')
    return record, binding, release


# The ranked table's count and range without ranking it: same rows, no window or sort.
GENE_SUMMARY = 'SELECT COUNT(*),MIN(loading),MAX(loading) FROM reveal_ref_factor_genes WHERE factor_key=%s'


def _query(binding, kind, metric):
    """(FROM..WHERE, args, search label, score, loading order, columns, coverage, summary SQL or None) of a factor's rows."""
    if kind == 'gene':
        # Rank the complete immutable factor before search/pagination. A search
        # for its 600th gene must still show rank 600, not rank 1.
        return (' FROM (SELECT gene AS symbol,loading,ROW_NUMBER() OVER (ORDER BY loading DESC,gene) AS loading_rank'
                ' FROM reveal_ref_factor_genes WHERE factor_key=%s) ranked WHERE 1=1',
                (binding['factor_key'],), 'ranked.symbol', 'ranked.loading', 'ranked.loading_rank',
                'ranked.symbol,ranked.loading,ranked.loading_rank', GENE_COVERAGE, GENE_SUMMARY)
    # Every projected gene set is in reveal_ref_gene_sets (the release keeps the gene sets its projections reference), so the
    # summary reads the factor's projections alone, and ties order by the projection key (factor_key, gene_set_id).
    score = 'p.marginal_loading' if metric == 'marginal' else 'p.joint_loading'
    return (' FROM reveal_ref_projections p JOIN reveal_ref_gene_sets s ON s.gene_set_id=p.gene_set_id WHERE p.factor_key=%s',
            (binding['factor_key'],), "CONCAT(s.gene_set_name,' ',p.library,' ',s.gene_set_id)", score, score + ' DESC,p.gene_set_id',
            's.gene_set_id,s.gene_set_name,p.library,s.n_genes,p.joint_loading,p.marginal_loading,p.joint_rank,p.marginal_rank', SET_COVERAGE,
            'SELECT COUNT(*),MIN(' + score + '),MAX(' + score + ') FROM reveal_ref_projections p WHERE p.factor_key=%s')


def _summary(reads, release, query):
    _, args, _, _, _, _, coverage, sql = query
    total, minimum, maximum = _immutable(('summary', release, sql, args), lambda: tuple(_rows(reads, sql, args)[0]))
    return {'total': int(total), 'min': float(minimum) if minimum is not None else None,
            'max': float(maximum) if maximum is not None else None, 'coverage': coverage, 'available': True}


def _trait_links(metadata):
    """The factor's cross-trait link summary (its factor-PheWAS counts and best links by q), or None when the release has none."""
    links = metadata.get('trait_links') if isinstance(metadata, dict) else None
    if not isinstance(links, dict): return None
    return {**{key: links.get(key) for key in TRAIT_LINK_KEYS},
            'top_linked': [{key: item.get(key) for key in TOP_LINK_KEYS} for item in links.get('top_linked') or [] if isinstance(item, dict)]}


def factor_detail(catalog, source_id, *, generation_id=None, source_revision=None):
    record, binding, release = _selection(catalog, source_id, generation_id, source_revision)
    reads = _Reads(release)
    try:
        # A release has no separate EAGGL import: eaggl_import_id names the release that holds the fit.
        result = {'factor': record, 'generation_id': release,
                  'provenance': {'eaggl_import_id': release, 'factor_id': binding['eaggl_factor_id'],
                                 'factor_key': binding.get('factor_key'), 'model': record['model']},
                  'gnomad': _gnomad(reads),
                  'genes': _summary(reads, release, _query(binding, 'gene', 'joint')),
                  'gene_sets': _summary(reads, release, _query(binding, 'gene_set', 'joint'))}
    finally:
        reads.close()
    links = _trait_links(binding.get('cfde_payload'))
    if links is not None: result['trait_links'] = links
    return result


def factor_loadings(catalog, source_id, *, kind='gene', metric='joint', sort='loading', q='', limit=200, offset=0, generation_id=None, source_revision=None,
                    gnomad_import_id=None, library=None):
    """library keeps one library's gene-set rows (genes have none, so it is ignored for them, like metric); the summary stays
    over every library."""
    library = library or None
    if kind not in ('gene', 'gene_set') or metric not in ('joint', 'marginal') or sort not in ('alphabetical', 'loading', *GNOMAD_SORTS) or not 1 <= limit <= 500 or offset < 0 or len(q) > 200 or (kind != 'gene' and (sort in GNOMAD_SORTS or gnomad_import_id is not None)) or (library is not None and len(library) > 64):
        raise Problem(422, 'INVALID_QUERY', 'Invalid loading kind, metric, sort, library, search, or pagination.')
    _, binding, release = _selection(catalog, source_id, generation_id, source_revision)
    reads = _Reads(release)
    try:
        query = _query(binding, kind, metric)
        summary = _summary(reads, release, query)
        base, args, label, _, ordering, columns, _, _ = query
        gnomad = _gnomad(reads, gnomad_import_id) if kind == 'gene' else None
        if sort in GNOMAD_SORTS and gnomad is None:
            raise Problem(503, 'SOURCE_NOT_READY', 'gnomAD constraint annotations have not been loaded for this environment.')
        if kind == 'gene' and gnomad:
            # One exact-symbol row per import, including explicit unresolved
            # identities. Join after ranking so annotations cannot change ranks.
            base = base.replace(' ranked WHERE 1=1', ' ranked LEFT JOIN gnomad_gene_constraints c ON c.import_id=%s AND c.gene_symbol=ranked.symbol WHERE 1=1')
            args += (gnomad['import_id'],)
            columns += ',c.gene_symbol,c.gene_id,c.transcript_id,c.selection_status,c.selection_reason,c.pli,c.loeuf,c.mis_z,c.lof_oe,c.constraint_flags'
        if sort in GNOMAD_SORTS:
            column, direction = GNOMAD_SORTS[sort]
            ordering = column + ' IS NULL,' + column + ' ' + direction + ',LOWER(ranked.symbol),ranked.symbol'
        if sort == 'alphabetical':
            ordering = 'LOWER(ranked.symbol),ranked.symbol' if kind == 'gene' else 'LOWER(s.gene_set_name),p.gene_set_id'
        filtered = kind == 'gene_set' and library is not None
        if filtered:
            base += ' AND p.library=%s'; args += (library,)
        if q.strip():
            # Search is literal; SQL wildcards in source names or user text do not
            # expand the result, and every value remains parameter-bound.
            search = q.strip().lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
            base += ' AND LOWER(' + label + ") LIKE %s ESCAPE '!'"
            args += ('%' + search + '%',)
        if q.strip() or filtered:
            # The match count rides on the page (a window over the filtered rows, before LIMIT): one ranking, not two.
            rows = _rows(reads, 'SELECT ' + columns + ',COUNT(*) OVER ()' + base + ' ORDER BY ' + ordering + ' LIMIT %s OFFSET %s', args + (limit, offset))
            total = int(rows[0][-1]) if rows else 0 if not offset else int(_rows(reads, 'SELECT COUNT(*)' + base, args)[0][0])
            rows = [row[:-1] for row in rows]
        else:
            total = summary['total']
            rows = _rows(reads, 'SELECT ' + columns + base + ' ORDER BY ' + ordering + ' LIMIT %s OFFSET %s', args + (limit, offset))
        items = []
        for row in rows:
            if kind == 'gene':
                symbol, loading, original_rank = row[:3]
                item = {'id': symbol, 'label': symbol, 'loading': float(loading), 'rank': original_rank}
                item['gnomad'] = None
                if gnomad and row[3] is not None:
                    matched, gene, transcript, status, reason, pli, loeuf, mis_z, lof_oe, flags = row[3:]
                    item['gnomad'] = {'symbol': matched, 'gene_id': gene, 'transcript': transcript, 'status': status,
                        'selection_method': reason if status == 'selected' else None, 'selection_reason': reason,
                        'pli': float(pli) if pli is not None else None, 'loeuf': float(loeuf) if loeuf is not None else None,
                        'mis_z': float(mis_z) if mis_z is not None else None, 'lof_oe': float(lof_oe) if lof_oe is not None else None,
                        'flags': _json(flags) if flags else []}
            else:
                # Ranks are within the gene set's library.
                identity, label, set_library, size, joint, marginal, joint_rank, marginal_rank = row
                item = {'id': identity, 'gene_set_id': identity, 'label': label, 'library': set_library, 'gene_count': size,
                        'loading': float(marginal if metric == 'marginal' else joint), 'rank': marginal_rank if metric == 'marginal' else joint_rank,
                        'joint_loading': float(joint), 'marginal_loading': float(marginal),
                        'joint_rank': joint_rank, 'marginal_rank': marginal_rank}
            items.append(item)
        next_offset = offset + len(items)
        return {'source_id': source_id, 'generation_id': release, 'kind': kind, 'metric': metric, 'sort': sort, 'items': items,
                'total': total, 'offset': offset, 'limit': limit, 'next_offset': next_offset if next_offset < total else None, 'summary': summary, 'gnomad': gnomad}
    finally:
        reads.close()


# The collection's payload is read only when its parsed lineage is not cached (collection_facts): first its document hash.
GENE_SET_SQL = ('SELECT s.gene_set_name,s.library,s.n_genes,s.n_genes_in_eaggl_universe,s.metadata,c.collection_id,c.cfde_label,c.library,c.n_sets,'
                "c.portal_collection_id,JSON_EXTRACT(c.payload,'$.document_sha256') FROM reveal_ref_gene_sets s"
                ' JOIN reveal_ref_collections c ON c.collection_id=s.collection_id WHERE s.gene_set_id=%s')
PAYLOAD_SQL = 'SELECT payload FROM reveal_ref_collections WHERE collection_id=%s'


def portal_links(gene_set_id, portal_collection_id=None):
    """CatalogGeneSet.external_links: the provenance portal's pages of a gene set and of its collection (the portal's id for it)."""
    base = (setting('REVEAL_PROVENANCE_PORTAL_URL') or PORTAL_URL).rstrip('/')
    links = [{'label': 'Gene-set provenance on the Translator portal', 'kind': 'portal_gene_set',
              'href': base + '/gene_set/details/id=' + quote(gene_set_id, safe='')}]
    if portal_collection_id:
        links.append({'label': 'Collection on the Translator portal', 'kind': 'portal_collection',
                      'href': base + '/gene_set/list/id=' + quote(portal_collection_id, safe='')})
    return links


def _lineage_notes(closure, facts):
    """Limitations of a served lineage closure: inputs it represents by one, and Datasets only the curated map links."""
    notes = [f"{item['collapsed_inputs']:,} inputs of {item['activity']} share one upstream lineage; {item['representative']} represents them."
             for item in closure['collapsed']]
    curated, edges = facts['curated'], facts['curated_edges']
    reached = {edge['object'] for edge in closure['provenance'].get('was_derived_from_edges') or [] if tuple(edge.get(key) for key in EDGE_KEY) in edges}
    names = [node.get('name') or node['id'] for node in closure['provenance'].get('datasets') or [] if node['id'] in reached]
    if names:
        notes.append(f"{'; '.join(names)}: linked to this gene set's raw files by REVEAL's curated source-dataset map "
                     f"({curated['source']}), not by the producer's collection document.")
        notes += [item['note'] for item in curated['notes'] if item.get('dataset') in reached and item.get('note')]
    return notes


def gene_set_detail(catalog, identity, *, generation_id=None):
    if not GENE_SET_ID.fullmatch(identity):
        raise Problem(404, 'NOT_FOUND', 'The imported gene set is unavailable.')
    catalog.load()
    with catalog.lock: release = catalog.reference_generation_id
    if generation_id and generation_id != release:
        raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The requested reference release is no longer active. Reload the factor page.')
    reads = _Reads(release)
    try:
        rows = _rows(reads, GENE_SET_SQL, (identity,))
        # The release keeps only the gene sets its projections or betas reference; others are 404 (GeneSetView falls back to /v1/gene-sets).
        if not rows: raise Problem(404, 'NOT_FOUND', 'The imported gene set is unavailable.')
        name, library, size, universe, metadata, collection_id, label, collection_library, count, portal_collection, document = rows[0]
        facts = collection_facts(release, collection_id, extracted(document), lambda: _rows(reads, PAYLOAD_SQL, (collection_id,))[0][0])
    finally: reads.close()
    metadata = _json(metadata)
    node = metadata.pop('dapper_gene_set', None)
    # The identifier and exact imported object must agree; a similarly named
    # set, including a derived account alias, is never substituted.
    if node is not None and node.get('id') != identity:
        raise Problem(503, 'SOURCE_NOT_READY', 'The imported gene-set identity does not match its stored object.')
    # The gene set's own lineage (producer and curated provenance), not the whole collection's: GlyGen HZ2's 6,620 cached
    # API responses become one path to the GlyGen release. Copied: the cached lineage is shared.
    closure = deepcopy(lineage_closure(node or {'id': identity}, facts['lineage'], index=facts['index']))
    return {'id': identity, 'generation_id': release, 'name': name, 'library': library, 'gene_count': size,
            'genes_in_universe': universe, 'collection': {'id': collection_id, 'label': label, 'library': collection_library,
            'gene_set_count': count, 'object': deepcopy(facts['collection']), 'payload_sha256': facts['document_sha256']},
            'object': node, 'metadata': metadata, 'provenance': closure['provenance'],
            'limitations': ([] if node else ['The exact gene-set object is not stored in this reference release.']) + _lineage_notes(closure, facts),
            'external_links': portal_links(identity, portal_collection)}
