"""Public, generation-pinned factor loadings and imported gene-set provenance.

These reads use scientific reference tables only: no research jobs, account graphs,
embeddings or remote calls. Missing scores stay null, never inferred from rank.
"""
from copy import deepcopy
import hashlib
import json
import re

from .auth import Problem
from .reference_generation import KPN_MODEL
from .repository import digest
from .runtime_config import mysql_connection

GENE_COVERAGE = 'All stored nonzero EAGGL gene loadings. Genes absent from this import are not assigned a zero loading.'
SET_COVERAGE = 'Retained per-trait projections: gene sets in the top 50 by joint or marginal loading. Search covers this retained subset, not every gene set in the source library.'
LEGACY_COVERAGE = 'Imported ranked gene-set links only. This legacy import does not store numeric gene-set loadings.'
GENE_SET_ID = re.compile(r'dapper:GeneSet\.[A-Za-z0-9_-]{32}')


def _json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else (value or {})


def _rows(connection, sql, args=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchall()


def _selection(catalog, source_id, generation_id=None, source_revision=None):
    catalog.load()
    # A refresh replaces all catalog state under this lock. Copy related records
    # together so an in-flight read cannot mix old and new generations.
    with catalog.lock:
        record = deepcopy(catalog.factors.get(source_id))
        binding = deepcopy(catalog.bindings.get(source_id))
        generation = catalog.reference_generation_id
    if generation_id and generation_id != generation:
        raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The requested reference generation is no longer active. Reload the factor page.')
    if not record or not binding:
        raise Problem(404, 'NOT_FOUND', 'The factor is unavailable in the active reference generation.')
    if source_revision and source_revision != record['source_revision']:
        raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The exact requested factor revision is unavailable.')
    return record, binding, generation


def _factor_index(connection, binding):
    identity = binding['eaggl_factor_id']
    rows = _rows(connection, 'SELECT factor_index FROM eaggl_factors WHERE import_id=%s AND factor_id_sha256 IN (%s,%s) AND factor_id=%s',
                 (binding['eaggl_import_id'], hashlib.sha256(identity.encode()).hexdigest(), digest(identity), identity))
    if len(rows) != 1:
        raise Problem(503, 'SOURCE_NOT_READY', 'The imported factor gene loadings are unavailable.')
    return rows[0][0]


def _query(binding, generation, index, kind, metric):
    if kind == 'gene':
        # Rank the complete immutable factor before search/pagination. A search
        # for its 600th gene must still show rank 600, not rank 1.
        return (' FROM (SELECT g.symbol,g.gene_index,l.loading,ROW_NUMBER() OVER (ORDER BY l.loading DESC,g.symbol,g.gene_index) AS loading_rank'
                ' FROM eaggl_gene_loadings l JOIN eaggl_genes g ON g.import_id=l.import_id AND g.gene_index=l.gene_index'
                ' WHERE l.import_id=%s AND l.factor_index=%s) ranked WHERE 1=1',
                (binding['eaggl_import_id'], index), 'ranked.symbol', 'ranked.loading', 'ranked.loading_rank',
                'ranked.symbol,ranked.loading,ranked.loading_rank', GENE_COVERAGE, True)
    if binding.get('model') == KPN_MODEL:
        score = 'p.marginal_loading' if metric == 'marginal' else 'p.joint_loading'
        return (' FROM factor_gene_set_projections p JOIN cfde_gene_sets s ON s.generation_id=p.generation_id AND s.gene_set_id=p.gene_set_id'
                ' WHERE p.generation_id=%s AND p.scope=%s AND p.factor_key=%s',
                (generation, 'per_trait', binding['factor_key']), "CONCAT(s.gene_set_name,' ',s.library,' ',s.gene_set_id)", score,
                score + ' DESC,s.gene_set_id',
                's.gene_set_id,s.gene_set_name,s.library,s.n_genes,p.joint_loading,p.marginal_loading,p.joint_rank,p.marginal_rank', SET_COVERAGE, True)
    return (' FROM eaggl_cfde_gene_set_links l LEFT JOIN cfde_gene_set_aliases a ON a.import_id=l.gene_set_import_id AND a.node_id_sha256=l.resolved_alias_sha256'
            ' WHERE l.run_id=%s AND l.factor_index=%s',
            (binding['mapping_run_id'], index), 'l.source_key', 'NULL', 'l.gene_set_rank,l.source_key',
            'l.node_id,l.source_key,a.dapper_id,l.gene_set_rank', LEGACY_COVERAGE, False)


def _summary(connection, query):
    base, args, _, score, _, _, coverage, available = query
    total, minimum, maximum = _rows(connection, 'SELECT COUNT(*),MIN(' + score + '),MAX(' + score + ')' + base, args)[0]
    return {'total': int(total), 'min': float(minimum) if minimum is not None else None,
            'max': float(maximum) if maximum is not None else None, 'coverage': coverage, 'available': available}


def factor_detail(catalog, source_id, *, generation_id=None, source_revision=None):
    record, binding, generation = _selection(catalog, source_id, generation_id, source_revision)
    connection = mysql_connection()
    try:
        index = _factor_index(connection, binding)
        return {'factor': record, 'generation_id': generation,
                'provenance': {'eaggl_import_id': binding['eaggl_import_id'], 'factor_id': binding['eaggl_factor_id'],
                               'factor_key': binding.get('factor_key'), 'model': record['model']},
                'genes': _summary(connection, _query(binding, generation, index, 'gene', 'joint')),
                'gene_sets': _summary(connection, _query(binding, generation, index, 'gene_set', 'joint'))}
    finally:
        connection.close()


def factor_loadings(catalog, source_id, *, kind='gene', metric='joint', sort='loading', q='', limit=200, offset=0, generation_id=None, source_revision=None):
    if kind not in ('gene', 'gene_set') or metric not in ('joint', 'marginal') or sort not in ('alphabetical', 'loading') or not 1 <= limit <= 500 or offset < 0 or len(q) > 200:
        raise Problem(422, 'INVALID_QUERY', 'Invalid loading kind, metric, sort, search, or pagination.')
    _, binding, generation = _selection(catalog, source_id, generation_id, source_revision)
    connection = mysql_connection()
    try:
        index = _factor_index(connection, binding)
        query = _query(binding, generation, index, kind, metric)
        summary = _summary(connection, query)
        base, args, label, _, ordering, columns, _, _ = query
        if sort == 'alphabetical':
            ordering = ('LOWER(ranked.symbol),ranked.symbol,ranked.gene_index' if kind == 'gene' else
                        'LOWER(s.gene_set_name),s.gene_set_id' if binding.get('model') == KPN_MODEL else
                        'LOWER(l.source_key),l.node_id,l.gene_set_rank')
        if q.strip():
            # Search is literal; SQL wildcards in source names or user text do not
            # expand the result, and every value remains parameter-bound.
            search = q.strip().lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
            base += ' AND LOWER(' + label + ") LIKE %s ESCAPE '!'"
            args += ('%' + search + '%',)
            total = int(_rows(connection, 'SELECT COUNT(*)' + base, args)[0][0])
        else:
            total = summary['total']
        rows = _rows(connection, 'SELECT ' + columns + base + ' ORDER BY ' + ordering + ' LIMIT %s OFFSET %s', args + (limit, offset))
        items = []
        for row in rows:
            if kind == 'gene':
                symbol, loading, original_rank = row
                item = {'id': symbol, 'label': symbol, 'loading': float(loading), 'rank': original_rank}
            elif binding.get('model') == KPN_MODEL:
                identity, label, library, size, joint, marginal, joint_rank, marginal_rank = row
                item = {'id': identity, 'gene_set_id': identity, 'label': label, 'library': library, 'gene_count': size,
                        'loading': float(marginal if metric == 'marginal' else joint), 'rank': marginal_rank if metric == 'marginal' else joint_rank,
                        'joint_loading': float(joint), 'marginal_loading': float(marginal),
                        'joint_rank': joint_rank, 'marginal_rank': marginal_rank}
            else:
                identity, label, dapper_id, original_rank = row
                item = {'id': identity, 'label': label, 'gene_set_id': dapper_id, 'loading': None, 'rank': original_rank}
            items.append(item)
        next_offset = offset + len(items)
        return {'source_id': source_id, 'generation_id': generation, 'kind': kind, 'metric': metric, 'sort': sort, 'items': items,
                'total': total, 'offset': offset, 'limit': limit, 'next_offset': next_offset if next_offset < total else None, 'summary': summary}
    finally:
        connection.close()


def gene_set_detail(catalog, identity, *, generation_id=None):
    if not GENE_SET_ID.fullmatch(identity):
        raise Problem(404, 'NOT_FOUND', 'The imported gene set is unavailable.')
    catalog.load()
    with catalog.lock:
        generation, model, import_id = catalog.reference_generation_id, catalog.model, catalog.geneset_import
    if generation_id and generation_id != generation:
        raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The requested reference generation is no longer active. Reload the factor page.')
    connection = mysql_connection()
    try:
        if model != KPN_MODEL:
            rows = _rows(connection, 'SELECT o.payload,a.provenance,a.source_key FROM cfde_gene_set_aliases a JOIN dapper_objects o ON o.id=a.dapper_id'
                         ' WHERE a.import_id=%s AND a.dapper_id=%s ORDER BY a.node_id_sha256', (import_id, identity))
            if not rows: raise Problem(404, 'NOT_FOUND', 'The imported gene set is unavailable.')
            node, provenance, key = rows[0]
            node, provenance = _json(node), _json(provenance)
            return {'id': identity, 'generation_id': generation, 'name': node.get('name', key), 'library': None,
                    'gene_count': len(node['members']) if isinstance(node.get('members'), list) else None, 'genes_in_universe': None, 'collection': None, 'object': node,
                    'metadata': {'source_key': key}, 'provenance': provenance,
                    'limitations': ['This legacy import stores source alias provenance; collection provenance and member counts may be unavailable.']}
        rows = _rows(connection, 'SELECT s.gene_set_name,s.library,s.n_genes,s.n_genes_in_eaggl_universe,s.metadata,'
                     'c.collection_id,c.cfde_label,c.library,c.n_sets,c.payload FROM cfde_gene_sets s'
                     ' JOIN cfde_gene_set_collections c ON c.generation_id=s.generation_id AND c.collection_id=s.collection_id'
                     ' WHERE s.generation_id=%s AND s.gene_set_id=%s', (generation, identity))
        if not rows: raise Problem(404, 'NOT_FOUND', 'The imported gene set is unavailable.')
        name, library, size, universe, metadata, collection_id, label, collection_library, count, payload = rows[0]
        metadata, payload = _json(metadata), _json(payload)
        node = metadata.pop('dapper_gene_set', None)
        # The identifier and exact imported object must agree; a similarly named
        # set, including a derived account alias, is never substituted.
        if node is not None and node.get('id') != identity:
            raise Problem(503, 'SOURCE_NOT_READY', 'The imported gene-set identity does not match its stored object.')
        return {'id': identity, 'generation_id': generation, 'name': name, 'library': library, 'gene_count': size,
                'genes_in_universe': universe, 'collection': {'id': collection_id, 'label': label, 'library': collection_library,
                'gene_set_count': count, 'object': payload.get('collection'), 'payload_sha256': payload.get('document_sha256')},
                'object': node, 'metadata': metadata, 'provenance': payload.get('provenance') or {},
                'limitations': [] if node else ['The exact gene-set object is not stored in this reference generation.']}
    finally:
        connection.close()
