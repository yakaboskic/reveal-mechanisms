"""Bounded research reads of an immutable imported reference generation.

No active-catalog selection or external fallback is used here. Callers authorize
the request, retain its generation and persist each QueryCapture before exposing
it to an agent. This module owns the scientific bytes and their File identities.
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
from .factor_details import GENE_COVERAGE, SET_COVERAGE, LEGACY_COVERAGE
from .reference_generation import KPN_MODEL, LEGACY_MODEL, GENERATION_RE, mechanism_node, parse_public_id

READER_VERSION = 'reveal.reference-query/2'
CAPTURE_FORMAT = 'reveal.reference-query.capture/1'
MAX_CAPTURE_BYTES = 1_000_000
PAGE_FIELDS = {'limit', 'cursor'}
OPERATIONS = {
    'search_factors': {'q'}, 'get_factor': {'factor_id'},
    'get_factor_loadings': {'factor_id', 'kind', 'metric', 'q'},
    'search_genes': {'q'}, 'resolve_gene': {'gene', 'taxon', 'mapping_revision'}, 'get_gene_factors': {'gene'},
    'search_gene_sets': {'q'}, 'get_gene_set': {'gene_set_id'},
    'get_gene_set_members': {'gene_set_id'}, 'get_gene_set_factors': {'gene_set_id'},
    'search_traits': {'q'}, 'get_trait': {'trait_id'},
    'get_connections': {'factor_id'}, 'get_imported_graph': {'node_id'},
}
METRICS = {
    'loading': 'Stored nonzero EAGGL gene weight; absent rows are not zero.',
    'joint_loading': 'Retained per-trait gene-set joint projection, not a phenotype association.',
    'marginal_loading': 'Retained per-trait gene-set marginal projection, not a phenotype association.',
    'joint_rank': 'Rank in source per-trait joint projections, before retained-subset filtering.',
    'marginal_rank': 'Rank in source per-trait marginal projections, before retained-subset filtering.',
}


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
                exact, dependencies, prefixes, reason = _resolve_gene_set(dapper, row, collection, {}, groups)
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
                    for dependency in dependencies:
                        target = context.setdefault(groups[_class_of(dependency['id'])], [])
                        if dependency['id'] not in {node['id'] for node in target}: target.append(dependency)
        return {'source_artifacts': artifacts, 'dapper_context': context, 'files': files,
                'eligible_source_ids': eligible,
                'cfde_source_ids': eligible if self.source_mode == 'imported_reference' else [],
                'object_resolution': resolutions, 'reference_objects': resolutions,
                'source_ref': {'artifact_id': key, 'pointer': '/result/items'}, 'dapper_file_id': source['dapper_file_id']}


class ReferenceQueryService:
    def __init__(self, connection_factory, *, cursor_secret=None, max_capture_bytes=MAX_CAPTURE_BYTES):
        self.connection_factory = connection_factory
        self.cursor_secret = cursor_secret.encode() if isinstance(cursor_secret, str) else cursor_secret
        self.max_capture_bytes = max_capture_bytes

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

    def _generation(self, connection, identity):
        if not isinstance(identity, str) or not GENERATION_RE.fullmatch(identity):
            raise Problem(422, 'INVALID_QUERY', 'An exact reference generation is required.')
        columns = ('generation_id', 'kind', 'model', 'status', 'eaggl_import_id', 'legacy_mapping_run_id', 'legacy_gene_set_import_id', 'manifest')
        rows = self._rows(connection, 'SELECT ' + ','.join(columns) + ' FROM reference_generations WHERE generation_id=%s', [identity], columns)
        if not rows or rows[0]['status'] not in ('complete', 'superseded'):
            raise Problem(409, 'SOURCE_UNAVAILABLE', 'The pinned reference generation is not retained for research queries.')
        return rows[0]

    def catalog(self, generation_id):
        connection = self.connection_factory()
        try:
            generation = self._generation(connection, generation_id)
            # Catalogs expose scientific source identity, never backend manifest
            # paths or operator-only import configuration.
            public = {key: value for key, value in generation.items() if key != 'manifest'}
            public['generation_manifest_sha256'] = sha256(canonical_json(generation['manifest']))
            return capability_catalog(public)
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
        binding = sha256(canonical_json([READER_VERSION, generation_id, operation, args, limit]))
        offset = self._cursor(binding, value=cursor) if cursor is not None else 0
        connection = self.connection_factory()
        try:
            generation = self._generation(connection, generation_id)
            result, tables = self._execute(connection, generation, operation, args, limit, offset)
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
            result.update(returned_rows=len(items), limit=limit, offset=offset, truncated=has_more,
                          next_cursor=self._cursor(binding, offset + len(items)) if has_more else None)
            source = {'origin': 'mysql:' + '+'.join(tables) + '?generation_id=' + generation_id,
                      'kind': 'mysql', 'tables': tables, 'generation_id': generation_id,
                      'model': generation['model'], 'eaggl_import_id': generation['eaggl_import_id'],
                      'mapping_run_id': generation['legacy_mapping_run_id'],
                      'gene_set_import_id': generation['legacy_gene_set_import_id'],
                      'generation_manifest_sha256': sha256(canonical_json(generation['manifest']))}
            if generation['model'] == KPN_MODEL:
                source['mechanism_identity_version'] = generation['manifest'].get('mechanism_identity_version', 1)
            envelope = {'format': CAPTURE_FORMAT, 'reader_version': READER_VERSION,
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
        def factor(identity):
            _text(identity, 'factor_id')
            if kpn:
                columns = ('factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'label', 'source_revision', 'metadata', 'factor_index')
                found = rows('SELECT f.factor_key,f.public_id,f.eaggl_factor_id,f.kpn_trait_id,f.label,f.source_revision,f.metadata,e.factor_index'
                    ' FROM reference_factors f JOIN eaggl_factors e ON e.import_id=%s AND e.factor_id=f.eaggl_factor_id'
                    ' WHERE f.generation_id=%s AND (f.public_id=%s OR f.factor_key=%s OR f.eaggl_factor_id=%s)',
                    [imp, gen, identity, identity, identity], columns, ['reference_factors', 'eaggl_factors'])
            else:
                columns = ('factor_index', 'eaggl_factor_id', 'trait', 'label', 'source_revision', 'metadata', 'public_id', 'mapping')
                found = rows('SELECT e.factor_index,e.factor_id,e.trait,e.label,e.input_sha256,e.metadata,l.cfde_node_id,l.payload'
                    ' FROM eaggl_factors e LEFT JOIN eaggl_cfde_factor_links l ON l.run_id=%s AND l.factor_index=e.factor_index'
                    ' WHERE e.import_id=%s AND (e.factor_id=%s OR l.cfde_node_id=%s)',
                    [g['legacy_mapping_run_id'], imp, identity, identity], columns, ['eaggl_factors', 'eaggl_cfde_factor_links'])
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
                return page('SELECT public_id,factor_key,eaggl_factor_id,kpn_trait_id,label,source_revision FROM reference_factors'
                    " WHERE generation_id=%s AND (LOWER(label) LIKE %s ESCAPE '!' OR LOWER(eaggl_factor_id) LIKE %s ESCAPE '!')",
                    [gen,like,like], ('public_id','factor_key','eaggl_factor_id','kpn_trait_id','label','source_revision'),
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
        if op in ('search_traits','get_trait'):
            if not kpn: return missing('This imported model has factor trait labels but no KPN trait catalog.')
            columns = ('kpn_trait_id','legacy_phenotype_id','phenotype_name','trait_group','trait_type','description','metadata')
            trait = _text(a.get('trait_id'),'trait_id') if op == 'get_trait' else None
            if trait and re.fullmatch(r'trait:kpn:\d{7}',trait): trait='KPN.TRAIT:'+trait.rsplit(':',1)[1]
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


def capability_catalog(generation=None, *, phenotype_verified=None):
    from .gene_identity import crosswalk_capability
    if phenotype_verified is None:
        from .runtime_config import setting
        phenotype_verified = setting('REVEAL_SMALL_PHENOTYPE_VERIFIED', 'false').lower() == 'true'
    model=(generation or {}).get('model')
    operations=[]
    for name,fields in OPERATIONS.items():
        availability='supported' if model in (KPN_MODEL,LEGACY_MODEL) else 'not_supported_for_model' if model else 'requires_generation_metadata'
        reason=None
        if model==LEGACY_MODEL and name in ('search_traits','get_trait','get_gene_set_factors'):
            availability='not_stored'
            reason=('This model stores factor trait labels, not a KPN trait catalog.' if name in ('search_traits','get_trait')
                    else 'This model stores ranked factor-to-gene-set links, not numeric gene-set projections.')
        descriptor={'name':name,'arguments':sorted(fields | PAGE_FIELDS),'availability':availability}
        if reason: descriptor['reason']=reason
        if name=='get_factor_loadings':
            descriptor['semantics']={'gene':GENE_COVERAGE,'gene_set':SET_COVERAGE if model==KPN_MODEL else LEGACY_COVERAGE if model==LEGACY_MODEL else 'requires_generation_metadata'}
        operations.append(descriptor)
    return {'version':READER_VERSION,'source_mode':'imported_reference',
        'generation':deepcopy(generation),'operations':operations,
        'coverage':{'gene_loadings':GENE_COVERAGE,'gene_set_projections':SET_COVERAGE if model==KPN_MODEL else 'not_stored' if model==LEGACY_MODEL else 'requires_generation_metadata',
                    'legacy_gene_set_links':LEGACY_COVERAGE if model==LEGACY_MODEL else 'not_applicable' if model==KPN_MODEL else 'requires_generation_metadata',
                    'phenotype_associations':'not_stored'},
        'availability_note':'Supported means a reader exists for the retained model; each query reports actual imported coverage and missing rows.',
        'external_fallback':False,'gene_identity_crosswalk':crosswalk_capability(),
        'small_model_phenotype':SmallModelBioIndex.descriptor(verified=phenotype_verified)}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class SmallModelBioIndex:
    """The only external reference exception. Never follows redirects/fallbacks."""
    HOST = 'https://bioindex.hugeamp.org'
    VERSION = 'reveal.bioindex-small-phenotype/1'
    INDEXES = {'get_pigean_gene_phenotype':'pigean-gene-phenotype',
               'get_pigean_gene_set_phenotype':'pigean-gene-set-phenotype'}

    @staticmethod
    def arguments_schema():
        """One advertised contract for the two fixed phenotype readers."""
        return {'type': 'object', 'properties': {'phenotype_id': {
            'type': 'string', 'minLength': 1, 'maxLength': 200,
            'pattern': r'^(?=.*\S)[^,\x00-\x1f]+(?![\s\S])',
            'description': 'Exact phenotype identifier. Inspect returned gene or GeneSet rows after capture.'}},
            'required': ['phenotype_id'], 'additionalProperties': False}

    def __init__(self, *, fetch=None, verified=False, max_bytes=2_000_000, max_pages=3, max_rows=5000, timeout=15, clock=None):
        self.fetch = fetch or self._fetch
        self.verified = verified
        self.max_bytes, self.max_pages, self.max_rows, self.timeout = max_bytes,max_pages,max_rows,timeout
        self.clock = clock or (lambda:datetime.now(timezone.utc).isoformat())

    @classmethod
    def descriptor(cls, *, verified=None):
        if verified is None:
            from .runtime_config import setting
            verified = setting('REVEAL_SMALL_PHENOTYPE_VERIFIED', 'false').lower() == 'true'
        return {'host':cls.HOST,'model':'small','sigma':2,'signature':['phenotype','sigma','gene_set_size'],
                'adapter_version':cls.VERSION,'operations':cls.INDEXES,'arguments':cls.arguments_schema(),
                'availability':'supported' if verified else 'requires_verified_deployment_access',
                'deployment_verified':bool(verified), 'coverage':'not_queried',
                'availability_note':'Deployment capability does not establish source coverage or a successful scientific query.',
                'source_mode':'bioindex_small_phenotype'}

    @staticmethod
    def _fetch(url, max_bytes, timeout):
        with build_opener(_NoRedirect()).open(Request(url, headers={'Accept':'application/json'}),timeout=timeout) as response:
            data = response.read(max_bytes+1)
        if len(data)>max_bytes: raise ValueError('Source response exceeds byte budget')
        return data

    def verify(self):
        """Administrative metadata probe; never exposed as an agent operation."""
        catalog = decode(self.fetch(self.HOST+'/api/bio/indexes',self.max_bytes,self.timeout))
        for index in self.INDEXES.values():
            if not any(row.get('index')==index and row.get('query',{}).get('keys')==['phenotype','sigma','gene_set_size'] for row in catalog.get('data',[])):
                raise Problem(503,'SOURCE_UNAVAILABLE','The small-model deployment query signature has not been verified.')
            keys = decode(self.fetch(self.HOST+'/api/bio/keys/'+index+'/3?columns=gene_set_size',self.max_bytes,self.timeout))
            if ['small'] not in keys.get('keys',[]): raise Problem(503,'SOURCE_UNAVAILABLE','The deployment does not advertise literal small.')
        self.verified=True
        return catalog

    def query(self, operation, arguments, *, generation_id=None):
        if operation not in self.INDEXES or not isinstance(arguments,dict) or set(arguments)-{'phenotype_id'}:
            raise Problem(422,'INVALID_QUERY','Only the two fixed small-model phenotype operations are permitted.')
        phenotype=_text(arguments.get('phenotype_id'),'phenotype_id',200)
        if ',' in phenotype: raise Problem(422,'INVALID_QUERY','A phenotype identifier cannot add positional query keys.')
        index=self.INDEXES[operation]
        source={'origin':self.HOST+'/api/bio/query/'+index,'host':self.HOST,'index':index,'model':'small','sigma':2,
                'phenotype':phenotype,'adapter_version':self.VERSION,'observed_at':self.clock(),
                'upstream_release':None,'generation_id':None,
                'bounds':{'bytes':self.max_bytes,'pages':self.max_pages,'rows':self.max_rows,'seconds':self.timeout}}
        result={'items':[],'status':'source_unavailable','import_coverage':'Separate small-model observation; not part of the imported reference generation.',
                'snapshot_consistency':'not_guaranteed','returned_rows':0,'truncated':False,'next_cursor':None}
        pages={}; page_meta=[]; reason=None
        if not self.verified: reason='Deployment access and the three-key small-model signature have not been verified.'
        else:
            # This is an explicit result cap, never advertised as upstream page
            # size or complete retrieval. Unread byte progress remains partial.
            url=source['origin']+'?'+urlencode({'q':phenotype+',2,small','limit':self.max_rows})
            started=time.monotonic(); seen=set(); consumed=0
            try:
                for number in range(self.max_pages):
                    remaining=self.timeout-(time.monotonic()-started)
                    if remaining<=0: raise TimeoutError('Query latency budget exhausted')
                    raw=self.fetch(url,self.max_bytes-consumed,max(0.1,remaining))
                    if not isinstance(raw,bytes) or len(raw)>self.max_bytes-consumed: raise ValueError('Source response exceeds byte budget')
                    page=decode(raw)
                    if not isinstance(page,dict): raise ValueError('Invalid BioIndex response envelope')
                    data=page.get('data')
                    if not isinstance(data,list): raise ValueError('Invalid BioIndex response rows')
                    if page.get('index',index)!=index: raise ValueError('Source index escaped query scope')
                    query_keys = page.get('q')
                    if query_keys is not None:
                        if isinstance(query_keys,str): query_keys=query_keys.split(',')
                        if not isinstance(query_keys,list) or [str(v) for v in query_keys] != [phenotype,'2','small']:
                            raise ValueError('Source positional query escaped its pinned scope')
                    for row in data:
                        if not isinstance(row,dict): raise ValueError('Invalid source row')
                        if row.get('gene_set_size','small')!='small' or str(row.get('sigma',2)) not in ('2','2.0'):
                            raise ValueError('Source model or sigma escaped query scope')
                        if row.get('phenotype',phenotype)!=phenotype: raise ValueError('Source phenotype escaped query scope')
                    pages['page-'+str(number)+'.json']=raw; consumed+=len(raw)
                    available=self.max_rows-len(result['items'])
                    result['items'].extend(data[:available])
                    progress=page.get('progress') or {}
                    if not isinstance(progress,dict): raise ValueError('Invalid source progress')
                    continuation=page.get('continuation')
                    finished = (type(progress.get('bytes_read')) is int and type(progress.get('bytes_total')) is int
                                and 0 <= progress['bytes_read'] == progress['bytes_total'])
                    partial=len(data)>available or bool(continuation) or not finished
                    page_meta.append({'sha256':sha256(raw),'size_bytes':len(raw),'rows':len(data),'progress':progress,
                                      'restricted':page.get('restricted'),'page':page.get('page')})
                    result.update(status='partial' if partial else 'complete' if result['items'] else 'empty',truncated=partial)
                    if not continuation or len(result['items'])>=self.max_rows or consumed>=self.max_bytes: break
                    if not isinstance(continuation,str) or len(continuation)>10000 or continuation in seen:
                        raise ValueError('Invalid or repeated upstream continuation')
                    seen.add(continuation)
                    url=self.HOST+'/api/bio/cont?'+urlencode({'token':continuation})
            except (HTTPError,URLError,TimeoutError,OSError) as error:
                result.update(status='partial' if result['items'] else 'source_unavailable',truncated=bool(result['items']))
                reason='The configured small-model source was unavailable ('+type(error).__name__+').'
            except (ValueError,TypeError,KeyError) as error:
                # A scope mismatch invalidates the whole query; earlier valid
                # pages stay diagnostic only and cannot become eligible evidence.
                result.update(items=[],status='source_unavailable',truncated=False)
                reason=str(error)
        if reason: result['reason']=reason
        result.update(returned_rows=len(result['items']),pages=page_meta,
                      coverage='Complete only for returned source exposure; restricted rows and upstream filtering may remain.')
        raw=canonical_json({'format':CAPTURE_FORMAT,'source_mode':'bioindex_small_phenotype','source':source,
                            'operation':operation,'arguments':{'phenotype_id':phenotype},'result':result})
        return QueryCapture(result,raw,'bioindex_small_phenotype',source,pages)
