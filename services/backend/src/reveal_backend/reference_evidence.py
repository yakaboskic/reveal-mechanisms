"""Collect a gap and KPN factors from MySQL without CFDE/BioIndex requests.

The evidence source for model eaggl-capped-v1 (docs/reference-reload.md §8). It produces the
same build input as evidence_collector.collect_package, so the unchanged offline builder
(evidence_package.build_package) and the worker's checks accept it. DisMech gap, attachment
and document handling is identical to collect_package. Frozen researcher text and uploads are
captured privately with the same immutable storage references and checksums. The CFDE interactive API and BioIndex
captures are replaced by captures read from one reference generation in MySQL. Every capture
goes through the same CaptureStore (exact bytes, sha256) with a string `origin` naming its SQL
tables and the generation: `mysql:<table>+<table>?generation_id=<gen>` for SQL results and
`mysql-derived:<artifact>+<artifact>?generation_id=<gen>` for captures computed only from them.

Capture formats (JSON artifacts; numbers are the MySQL values, never rescaled)

`reveal.reference-evidence.mysql-capture/1`: SQL statements and their rows
    {format, generation_id, model, source: {kind: 'mysql', tables, statements: [{sql, parameters,
     row_count}]}, ..., data: [row, ...]}     rows sorted deterministically, not in engine order
  reference-generation      the reference_generations row
  reference-factors         the anchors: reference_factors x kpn_traits, with metadata
  eaggl-factor-index        the anchors' eaggl_factors rows (factor_index, factor_id)
  factor-gene-overlap       loadings of every EAGGL factor on the gene candidates
  candidate-factors         reference_factors x kpn_traits of the top overlapping factors (an
                            EAGGL factor missing from the generation is left out: requested_eaggl_factor_ids)
  gene-set-payloads         cfde_gene_sets rows of the gene-set candidates (metadata JSON)
  gene-set-collections      their collections; of the payload only `provenance` (the collection
                            YAML's prefixes, organizations, datasets, files and activities)
  contextual-gene-set-projections  retained candidate factors x retained gene sets
BioIndex-compatible captures add {index, q, limit: null, continuation: null, scope}, so the
builder binds them like its BioIndex queries. `limit` is null because each is a complete SQL
result, not a page. Every row has phenotype (the KPN trait number), trait_group 'kpn',
gene_set_size (the model string: the builder's scope key, not a gene count) and factor, plus:
  gene-factor-<tag>         gene, factor_value = eaggl_gene_loadings.loading, gene_index
  gene_set-factor-<tag>     gene_set (dapper:GeneSet id), factor_value = joint_loading, the
                            marginal loading, both %.4g texts, joint/marginal ranks,
                            is_joint_top_factor, gene_set_name, library, collection_id,
                            cfde_label, n_genes, n_genes_in_eaggl_universe
Trait-level PIGEAN phenotype associations (gene/gene-set to trait: combined/log_bf/prior,
beta/rs_score) are not in MySQL, so no trait-scope query is bound. The build policy allows
exactly that gap: the package records the capture blockers
bioindex:trait:trait:kpn:NNNNNNN:(gene|gene_set):not_captured, and box_adapter accepts a KPN
package whose only blockers are these. Factor-scope rows carry no trait metrics either: the
package's trait observations (ascertained via a mechanism) have empty reported_metrics, never zero.

`reveal.reference-evidence.derived-capture/1`: computed only from the captures above
  connections-<target>  typed candidates in the interactive-API request/response shape that the
      builder reads. `method: 'POST'` and `status: 200` are the builder's completed-query
      envelope. No HTTP request is made: source.kind is 'mysql-derived' and there is no `url`.
      gene      genes loaded on the anchors; edge raw_score = loading
      gene_set  projected gene sets; edge raw_score = joint_loading, projection in `extra`
      trait     the anchors' KPN traits; edge factor->trait (relation factor_of_trait), no score
      factor    other factors loading the gene candidates; per anchor raw_score =
                sum_g min(anchor_g, factor_g) / sum_g anchor_g over the gene candidates g
      aggregate_score = sum of the per-anchor raw scores / number of anchors (reducer 'mean'; an
      anchor without a row contributes 0; for traits, the fraction of anchors in the trait).
      The top `limit` by aggregate_score descending, then node id ascending.
  contextual            every factor->gene, factor->gene set, factor->trait and shared-gene edge
                        among the retained nodes (anchors and retained factor candidates)
  gene-set-resolution   per gene-set candidate: exact CFDE DAPPER GeneSet, or alias only and why

Identifiers. Anchors are KPN public ids, factor:kpn:NNNNNNN:eaggl-capped-v1:FactorN. Their fit
has trait_group 'kpn', phenotype NNNNNNN, trait_id trait:kpn:NNNNNNN and upstream_build = the
generation id. Mechanism nodes come from reference_generation.mechanism_node, so their ids
agree with the catalog. Genes are gene:<EAGGL symbol>, traits trait:kpn:NNNNNNN, and gene sets
gene_set:<dapper:GeneSet id>.

Gene sets. The builder binds a gene-set candidate to a DAPPER GeneSet whose
alternate_identifier contains the candidate id. A CFDE GeneSet (alternate_identifier = [name])
cannot contain it without changing its identity. Each retained gene set therefore has an alias
GeneSet {name, member_type, alternate_identifier: [gene_set:<CFDE id>]}, and the binding names
the alias. The exact CFDE GeneSet is added when cfde_gene_sets.metadata['dapper_gene_set']
holds the node as written in its GeneSetCollection YAML and every DAPPER object it refers to
is stored:
  - the non-identity slots in_gene_set_collection, in_gmt_file, gmt_entry and has_embedding
    are removed, and the DAPPER id must recompute to gene_set_id;
  - its generating Activity (and any other dependency) joins the DAPPER context too;
  - the alias gains was_derived_from: [CFDE id], and membership_status is 'loaded'.
Otherwise the alias stands alone (membership_status 'not_loaded') and gene-set-resolution
records why. Dependencies are looked up in the collection's payload.provenance lists
(activities, files, datasets, organizations) and in metadata['dapper_dependencies']. CURIE
prefixes come from payload.provenance.prefixes or metadata['dapper_prefixes'], with
HGNC.SYMBOL as the default; only prefixes the included objects use join the package.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re
import time

from .evidence_collector import CaptureStore, add_source_prefixes, deepcopy_record, gz_records
from .evidence_package import (BUILD_VERSION, INPUT_VERSION, TARGETS, EvidenceBuildError, build_package, canonical_json,
                               decode, finite, frozen_semantic_association, pointer, ref, require, sha256, unique)
from .reference_generation import (GENERATION_RE, KPN_KIND, KPN_MODEL, PROJECTION_SCOPE, ReferenceError, kpn_number,
                                   mechanism_node, parse_public_id)

ROWS_FORMAT = 'reveal.reference-evidence.mysql-capture/1'
DERIVED_FORMAT = 'reveal.reference-evidence.derived-capture/1'
USABLE_STATUSES = ('complete', 'superseded')
TRAIT_GROUP = 'kpn'
MAX_PACKAGE_BYTES = 2_000_000
IN_BATCH = 500
DEFAULT_GENE_SET_PREFIXES = {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:'}
NON_IDENTITY_GENE_SET_SLOTS = ('in_gene_set_collection', 'in_gmt_file', 'gmt_entry', 'has_embedding')
DAPPER_REFERENCE = re.compile(r'dapper:([A-Za-z]+)\.[A-Za-z0-9_-]+')
# The only capture gaps a KPN package may have (box_adapter dispatches such a package): MySQL holds no
# PIGEAN trait-level phenotype associations.
TRAIT_CAPTURE_BLOCKER = re.compile(r'bioindex:trait:trait:kpn:\d{7}:(?:gene|gene_set):not_captured')
INSTRUCTIONS = ('services/backend/agent-skills/construct-scientific-account/SKILL.md', 'docs/evidence-package.md',
                'docs/scientific-account-construction.md', 'docs/pigean-claim-model.md', 'docs/dapper-integration.md',
                'docs/agent-evidence-integration.md', 'services/backend/agent-skills/read-evidence-package/SKILL.md')

FACTOR_COLUMNS = ('factor_key', 'public_id', 'eaggl_factor_id', 'kpn_trait_id', 'factor_number', 'label', 'eaggl_import_id')
FACTOR_DETAIL = ('input_sha256', 'source_revision', 'metadata')
TRAIT_COLUMNS = ('phenotype_name', 'legacy_phenotype_id', 'trait_group', 'trait_type')
FACTOR_SQL = ('SELECT ' + ','.join('f.' + c for c in FACTOR_COLUMNS) + ',{detail}' + ','.join('t.' + c for c in TRAIT_COLUMNS) +
              ' FROM reference_factors f JOIN kpn_traits t ON t.generation_id=f.generation_id AND t.kpn_trait_id=f.kpn_trait_id'
              ' WHERE f.generation_id=%s AND f.{column} IN ({marks})')
PROJECTION_COLUMNS = ('factor_key', 'gene_set_id', 'joint_loading', 'marginal_loading', 'joint_loading_text',
                      'marginal_loading_text', 'joint_rank', 'marginal_rank', 'is_joint_top_factor')
PROJECTION_EXTRA = PROJECTION_COLUMNS[3:]
PROJECTION_SQL = ('SELECT ' + ','.join('p.' + c for c in PROJECTION_COLUMNS) + ' FROM factor_gene_set_projections p'
                  ' WHERE p.generation_id=%s AND p.scope=%s AND p.factor_key IN ({keys}) AND p.gene_set_id IN ({marks})')
GENE_SET_COLUMNS = ('gene_set_name', 'library', 'collection_id', 'cfde_label', 'n_genes', 'n_genes_in_eaggl_universe')
ANCHOR_PROJECTION_SQL = ('SELECT ' + ','.join('p.' + c for c in PROJECTION_COLUMNS) +
                         ',s.gene_set_name,s.library,s.collection_id,c.cfde_label,s.n_genes,s.n_genes_in_eaggl_universe'
                         ' FROM factor_gene_set_projections p JOIN cfde_gene_sets s ON s.generation_id=p.generation_id AND s.gene_set_id=p.gene_set_id'
                         ' JOIN cfde_gene_set_collections c ON c.generation_id=s.generation_id AND c.collection_id=s.collection_id'
                         ' WHERE p.generation_id=%s AND p.scope=%s AND p.factor_key=%s')
GENE_SET_SQL = ('SELECT gene_set_id,collection_id,gene_set_name,library,n_genes,n_genes_in_eaggl_universe,legacy_source_key,metadata'
                ' FROM cfde_gene_sets WHERE generation_id=%s AND gene_set_id IN ({marks})')
COLLECTION_SQL = ("SELECT collection_id,cfde_label,library,n_sets,JSON_EXTRACT(payload,'$.provenance') FROM cfde_gene_set_collections"
                  ' WHERE generation_id=%s AND collection_id IN ({marks})')
PROVENANCE_GROUPS = ('activities', 'files', 'datasets', 'organizations')
JSON_COLUMNS = ('manifest', 'metadata', 'provenance')


# --------------------------------------------------------------------------------------
# Small helpers


def _marks(values):
    return ','.join(['%s'] * len(values))


def _json_manifest(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value or {}


def _value(column, value):
    if isinstance(value, (bytes, bytearray)): value = bytes(value).decode('utf-8')
    if column in JSON_COLUMNS and isinstance(value, str): value = json.loads(value)
    if isinstance(value, float): finite(value, column)
    return value


class _Reader:
    """One DB-API connection (one consistent read transaction); every statement is recorded."""
    def __init__(self, connection):
        self.connection, self.count = connection, 0

    def rows(self, statements, sql, parameters, columns):
        with self.connection.cursor() as cursor:
            cursor.execute(sql, tuple(parameters))
            result = cursor.fetchall()
        require(all(len(row) == len(columns) for row in result), 'Unexpected MySQL result shape')
        statements.append({'sql': sql, 'parameters': list(parameters), 'row_count': len(result)}); self.count += 1
        return [{column: _value(column, value) for column, value in zip(columns, row)} for row in result]

    def batched(self, statements, template, head, values, columns, **fields):
        """`values` fill the final IN ({marks}) list in batches; `fields` fill other template slots."""
        rows = []
        for start in range(0, len(values), IN_BATCH):
            batch = list(values[start:start + IN_BATCH])
            rows += self.rows(statements, template.format(marks=_marks(batch), **fields), [*head, *batch], columns)
        return rows


def _origin(kind, names, generation_id):
    return f"{kind}:{'+'.join(names)}?generation_id={generation_id}"


def _add_rows(store, key, generation_id, tables, statements, data, **fields):
    body = {'format': ROWS_FORMAT, 'generation_id': generation_id, 'model': KPN_MODEL,
            'source': {'kind': 'mysql', 'tables': list(tables), 'statements': statements}, **fields, 'data': data}
    return store.add(key, canonical_json(body), 'json', origin=_origin('mysql', tables, generation_id))


def _add_derived(store, key, generation_id, derived_from, body):
    body = {'format': DERIVED_FORMAT, 'generation_id': generation_id, 'model': KPN_MODEL, **body,
            'source': {'kind': 'mysql-derived', 'derived_from': list(derived_from), **body.get('source', {})}}
    return store.add(key, canonical_json(body), 'json', origin=_origin('mysql-derived', derived_from, generation_id))


def _tag(value):
    return sha256(canonical_json(value))[:12]


def _mean(values, count):
    return math.fsum(values) / count


def _factor_of(public):
    return parse_public_id(public)['factor']


def _trait_node(kpn_trait_id):
    return f'trait:{TRAIT_GROUP}:{kpn_number(kpn_trait_id)}'


def _scope_row(row):
    """The builder's BioIndex scope keys for a reference_factors row."""
    return {'phenotype': kpn_number(row['kpn_trait_id']), 'trait_group': TRAIT_GROUP, 'gene_set_size': KPN_MODEL,
            'factor': _factor_of(row['public_id'])}


def _edge(generation_id, family, relation, source, target, raw_score=None, extra=None):
    """Deterministic edge: one relationship has the same bytes in every capture."""
    edge = {'id': sha256(canonical_json(['reveal.reference-edge/1', generation_id, family, source, target]))[:32],
            'source': source, 'target': target, 'family': family, 'relation': relation, 'label': family.replace('_', ' '),
            'path_nodes': [source, target], 'extra': extra or {}}
    if raw_score is not None: edge['raw_score'] = finite(raw_score, 'edge raw_score')
    return edge


def _gene_edge(generation_id, source, symbol, loading):
    return _edge(generation_id, 'factor_gene_direct', 'direct', source, 'gene:' + symbol, loading)


def _gene_set_edge(generation_id, source, row):
    return _edge(generation_id, 'factor_gene_set_direct', 'direct', source, 'gene_set:' + row['gene_set_id'], row['joint_loading'],
                 {'projection_scope': PROJECTION_SCOPE, **{c: row[c] for c in PROJECTION_EXTRA}})


def _trait_edge(generation_id, source, kpn_trait_id):
    return _edge(generation_id, 'factor_trait_direct', 'factor_of_trait', source, _trait_node(kpn_trait_id), None, {'kpn_trait_id': kpn_trait_id})


def _candidate(node, edges, anchor_count, aggregate):
    scores = [edge['raw_score'] for edge in edges if 'raw_score' in edge]
    item = {'candidate': node, 'aggregate_score': finite(aggregate, 'aggregate_score'), 'support_path_count': len(edges),
            'support_anchor_count': len({edge['source'] for edge in edges}), 'anchor_count': anchor_count, 'edges': edges}
    if scores: item.update(raw_max_score=max(scores), raw_mean_score=_mean(scores, len(scores)))
    return item


def _references(value, found):
    """DAPPER ids a node refers to (the same scan as DapperRuntime.validate)."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in ('id', 'prefixes'): _references(child, found)
    elif isinstance(value, list):
        for child in value: _references(child, found)
    elif isinstance(value, str) and DAPPER_REFERENCE.fullmatch(value):
        found.add(value)
    return found


def _strings(value):
    if isinstance(value, dict):
        for child in value.values(): yield from _strings(child)
    elif isinstance(value, list):
        for child in value: yield from _strings(child)
    elif isinstance(value, str): yield value


# --------------------------------------------------------------------------------------
# DisMech gap, attachments and documents (identical to evidence_collector.collect_package)


def _resolve_gap(dismech_index, gap_id):
    manifest = decode((dismech_index / 'manifest.json').read_bytes())
    for filename in ['knowledge-gaps.jsonl.gz', 'gap-attachments.jsonl.gz']:
        require(sha256((dismech_index / filename).read_bytes()) == manifest['files'][filename]['sha256'], 'DisMech index checksum mismatch')
    matches = [r for r in gz_records(dismech_index / 'knowledge-gaps.jsonl.gz') if r['id'] == gap_id or r['discussion_id'] == gap_id]
    require(len(matches) == 1, 'DisMech ID must resolve to exactly one indexed knowledge gap')
    record = matches[0]
    require(record['kind'] in ('KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH'), 'Selected discussion is not a knowledge gap')
    hashes = {r['path']: r['sha256'] for r in decode((dismech_index / 'source-files.json').read_bytes())}
    attachments = [a for a in gz_records(dismech_index / 'gap-attachments.jsonl.gz') if a['gap_id'] == record['id']]
    return manifest, record, hashes, attachments


def _dismech_context(store, dapper, dismech_source, gap_manifest, gap_record, hashes, attachments, factor_ids, selection_metadata):
    def document(relative):
        key = 'dismech-' + sha256(relative.encode())[:16]
        if key in store.blobs: return key, decode(store.blobs[key], 'yaml')
        path = (dismech_source / relative).resolve()
        require(path.is_relative_to(dismech_source), 'DisMech source path escapes checkout')
        data = path.read_bytes()
        require(relative in hashes and sha256(data) == hashes[relative], 'DisMech checkout differs from frozen index; refresh the index or use its source revision')
        store.add(key, data, 'yaml', filename=path.name,
                  origin={'repository': 'https://github.com/monarch-initiative/dismech', 'commit': gap_manifest['source_commit'], 'path': relative})
        return key, decode(data, 'yaml')

    gap_artifact, gap_doc = document(gap_record['source_file'])
    raw_gap = pointer(gap_doc, gap_record['source_pointer'])
    require(raw_gap == gap_record['raw'], 'Gap index differs from source document')
    dapper.schema.imports_closure()
    namespaces = {k: str(v) for k, v in dapper.schema.namespaces().items()}
    source_schema = decode((dismech_source / 'src/dismech/schema/dismech.yaml').read_bytes(), 'yaml')
    namespaces = {**source_schema['prefixes'], **namespaces}  # DAPPER bindings take precedence.
    prefixes = {name: namespaces[name] for name in ['dapper', 'MONDO', 'CL', 'PMID', 'GO', 'ECTO']}
    prefixes.update(factor='urn:cfde:factor:', gene='urn:cfde:gene:', gene_set='urn:cfde:gene_set:',
                    trait='urn:cfde:trait:', cfde='urn:cfde:record:')
    add_source_prefixes(raw_gap, namespaces, prefixes)
    gap_node = {'text': raw_gap['prompt'], 'gap_description': raw_gap.get('rationale') or raw_gap['prompt'],
                'gap_kind': gap_record['kind'], 'scope': gap_record['document_name']}
    disease_curie = (gap_record.get('disease_term') or {}).get('term', {}).get('id')
    if disease_curie: gap_node['about_entities'] = [dapper.resolver(prefixes).expand(disease_curie)]
    gap_node['id'] = dapper.compute_id(gap_node, 'KnowledgeGap', dapper.schema)
    context = {'knowledge_gaps': [gap_node], 'mechanisms': [], 'gene_sets': [], 'activities': [], 'files': []}
    dismech = {'source_revision': {'commit': gap_manifest['source_commit'], 'source_sha256': hashes[gap_record['source_file']]},
               'knowledge_gap': {'source_id': gap_record['id'], 'source_ref': ref(gap_artifact, gap_record['source_pointer']),
                                 'disease': gap_record.get('disease_term'), 'attachments': []},
               'mechanisms': {}, 'other_context': [], 'related_knowledge_gaps': []}
    for attachment in sorted(attachments, key=lambda x: x['attachment_index']):
        item = deepcopy_record(attachment)
        if attachment['resolution'] in ('resolved', 'whole_section', 'whole_document'):
            artifact_id, target_doc = document(attachment['target_source_file'])
            location = ref(artifact_id, attachment['target_pointer']); raw = pointer(target_doc, attachment['target_pointer'])
            add_source_prefixes(raw, namespaces, prefixes)
            item['source_ref'] = location
            if attachment['target_kind'] == 'pathophysiology' and attachment['resolution'] == 'resolved':
                node = {'name': raw['name'], 'description': raw.get('description', raw['name'])}
                node['id'] = dapper.compute_id(node, 'Mechanism', dapper.schema)
                if node['id'] not in {m['id'] for m in context['mechanisms']}: context['mechanisms'].append(node)
                dismech['mechanisms'][attachment['target_id']] = {'dapper_id': node['id'], 'source_ref': location,
                    'associated_eaggl_mechanisms': {factor: frozen_semantic_association(selection_metadata, factor,
                        attachment['target_id'], hashes[attachment['target_source_file']], raw.get('description') or raw['name'])
                        for factor in factor_ids}}
            else:
                dismech['other_context'].append({'source_id': attachment['target_id'], 'kind': attachment['target_kind'], 'source_ref': location})
        dismech['knowledge_gap']['attachments'].append(item)
    require(len(dismech['mechanisms']) <= 10, 'Linked DisMech mechanism count exceeds package budget')
    # Related questions are a bounded same-document context, not additional selected gaps.
    for i, row in enumerate(gap_doc.get('discussions', [])):
        if row.get('discussion_id') != gap_record['discussion_id'] and row.get('kind') in ('KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH'):
            dismech['related_knowledge_gaps'].append({'source_id': gap_record['document_id'] + '#discussion:' + row.get('discussion_id', f'/discussions/{i}'),
                                                    'source_ref': ref(gap_artifact, f'/discussions/{i}')})
    dismech['related_knowledge_gaps'] = dismech['related_knowledge_gaps'][:10]
    return gap_node, context, dismech, prefixes


# --------------------------------------------------------------------------------------
# MySQL reads: anchors, BioIndex-compatible captures and typed candidates


class _Evidence:
    """Everything read for one package; captures are written to the store as they are read."""
    def __init__(self, store, reader, dapper, generation_id, factor_ids, limit):
        self.store, self.reader, self.dapper, self.generation_id = store, reader, dapper, generation_id
        self.factor_ids, self.limit, self.count = factor_ids, limit, len(factor_ids)
        self.mechanisms, self.mechanism_nodes, self.bioindex, self.connections, self.candidates = {}, {}, [], {}, {}
        self.factor_rows = {}        # public id -> reference_factors row (anchors and factor candidates)
        self.gene_loadings = {}      # anchor -> {symbol: loading}
        self.gene_index = {}         # symbol -> eaggl gene_index
        self.projections = {}        # anchor -> {gene_set_id: projection row}
        self.gene_set_info = {}      # gene_set_id -> name/library/collection columns
        self.profiles = {}           # eaggl factor id -> {symbol: loading} on the gene candidates
        self.anchor_captures = {}    # anchor -> (gene capture, gene-set capture)

    def rows(self, key, tables, sql, parameters, columns, sort, **fields):
        statements = []
        data = sorted(self.reader.rows(statements, sql, parameters, columns), key=sort)
        _add_rows(self.store, key, self.generation_id, tables, statements, data, **fields)
        return data

    def factor_rows_by(self, key, column, values, detail=False, **fields):
        statements, rows = [], []
        if values:
            columns = FACTOR_COLUMNS + (FACTOR_DETAIL if detail else ()) + TRAIT_COLUMNS
            rows = self.reader.batched(statements, FACTOR_SQL, [self.generation_id], sorted(values), columns, column=column,
                                       detail=''.join('f.' + c + ',' for c in FACTOR_DETAIL) if detail else '')
        rows.sort(key=lambda row: (row['kpn_trait_id'], row['factor_number'], row['factor_key']))
        _add_rows(self.store, key, self.generation_id, ['reference_factors', 'kpn_traits'], statements, rows, **fields)
        return rows

    def read(self):
        generation = self.generation()
        anchors = self.anchors(generation)
        for identity in self.factor_ids: self.anchor_observations(identity)
        self.request = {'anchor_items': [self.anchor_item(identity) for identity in self.factor_ids], 'exclude_node_ids': self.factor_ids,
                        'model': KPN_MODEL, 'reducer': 'mean', 'connection_scope': 'direct', 'context': '', 'limit': self.limit}
        self.gene_candidates = self.typed_genes()
        self.gene_set_candidates = self.typed_gene_sets()
        self.traits = self.typed_traits(anchors)
        self.typed_factors()
        # No trait-scope query: PIGEAN phenotype associations are not in MySQL, and other factors'
        # loadings are not phenotype-query associations. The builder records the explicit blockers.
        return self

    def generation(self):
        rows = self.rows('reference-generation', ['reference_generations'],
                         'SELECT generation_id,kind,model,status,eaggl_import_id,eaggl_embedding_run_id,dismech_import_id,manifest '
                         'FROM reference_generations WHERE generation_id=%s', [self.generation_id],
                         ('generation_id', 'kind', 'model', 'status', 'eaggl_import_id', 'eaggl_embedding_run_id', 'dismech_import_id', 'manifest'),
                         lambda row: row['generation_id'])
        require(len(rows) == 1, f'Reference generation {self.generation_id} is not loaded')
        generation = rows[0]
        require(generation['kind'] == KPN_KIND and generation['model'] == KPN_MODEL,
                f'Reference generation {self.generation_id} is not a {KPN_MODEL} generation')
        require(generation['status'] in USABLE_STATUSES,
                f'Reference generation {self.generation_id} is {generation["status"]}; its reference data is not servable')
        return generation

    def anchors(self, generation):
        rows = self.factor_rows_by('reference-factors', 'public_id', self.factor_ids, detail=True)
        self.anchor_refs = {row['public_id']: i for i, row in enumerate(rows)}
        missing = sorted(set(self.factor_ids) - set(self.anchor_refs))
        require(not missing, f'Selected factors are not in reference generation {self.generation_id}: {missing}')
        for row in rows:
            fit = parse_public_id(row['public_id'])
            require(row['factor_key'] == fit['factor_key'] and row['kpn_trait_id'] == fit['kpn_trait_id'],
                    f'Reference factor row differs from its public id: {row["public_id"]}')
            self.factor_rows[row['public_id']] = row
        imports = {row['eaggl_import_id'] or generation['eaggl_import_id'] for row in rows}
        require(len(imports) == 1 and None not in imports, 'Selected factors need one EAGGL import')
        self.import_id = next(iter(imports))
        statements = []
        index = self.reader.batched(statements, 'SELECT factor_index,factor_id,trait,label FROM eaggl_factors WHERE import_id=%s AND factor_id_sha256 IN ({marks})',
                                    [self.import_id], sorted({sha256(row['eaggl_factor_id'].encode()) for row in rows}),
                                    ('factor_index', 'factor_id', 'trait', 'label'))
        index.sort(key=lambda row: row['factor_index'])
        _add_rows(self.store, 'eaggl-factor-index', self.generation_id, ['eaggl_factors'], statements, index, eaggl_import_id=self.import_id)
        self.eaggl_index = {row['factor_id']: row['factor_index'] for row in index}
        for row in rows:
            require(row['eaggl_factor_id'] in self.eaggl_index, f'EAGGL factor {row["eaggl_factor_id"]} is not in import {self.import_id}')
        self.anchor_eaggl = {row['eaggl_factor_id'] for row in rows}
        for row in rows:
            identity, number = row['public_id'], kpn_number(row['kpn_trait_id'])
            node = mechanism_node(identity, row['phenotype_name'], row['kpn_trait_id'], _factor_of(identity), row['label'],
                identity_version=_json_manifest(generation.get('manifest')).get('mechanism_identity_version', 1),
                eaggl_import_id=self.import_id)
            node['id'] = self.dapper.compute_id(node, 'Mechanism', self.dapper.schema)
            self.mechanism_nodes[identity] = node
            self.mechanisms[identity] = {'dapper_id': node['id'], 'source_label': row['label'],
                'source_ref': ref('reference-factors', f'/data/{self.anchor_refs[identity]}'),
                'fit': {'trait_group': TRAIT_GROUP, 'phenotype': number, 'model': KPN_MODEL, 'factor': _factor_of(identity),
                        'trait_id': _trait_node(row['kpn_trait_id']), 'upstream_build': self.generation_id}}
        return rows

    def anchor_item(self, identity):
        row = self.factor_rows[identity]
        return {'node_id': identity, 'node_type': 'factor', 'label': row['label'], 'subtitle': f'{row["phenotype_name"]} ({_factor_of(identity)})'}

    def anchor_observations(self, identity):
        row, tag = self.factor_rows[identity], _tag(identity)
        q = [kpn_number(row['kpn_trait_id']), KPN_MODEL, _factor_of(identity)]
        statements = []
        loadings = self.reader.rows(statements, 'SELECT l.gene_index,g.symbol,l.loading FROM eaggl_gene_loadings l JOIN eaggl_genes g '
                                    'ON g.import_id=l.import_id AND g.gene_index=l.gene_index WHERE l.import_id=%s AND l.factor_index=%s',
                                    [self.import_id, self.eaggl_index[row['eaggl_factor_id']]], ('gene_index', 'symbol', 'loading'))
        loadings.sort(key=lambda r: (-r['loading'], r['symbol'], r['gene_index']))
        require(len({r['symbol'] for r in loadings}) == len(loadings), f'Duplicate gene symbol in the EAGGL loadings of {identity}')
        gene_key = _add_rows(self.store, 'gene-factor-' + tag, self.generation_id, ['eaggl_gene_loadings', 'eaggl_genes'], statements,
                             [{**_scope_row(row), 'gene': r['symbol'], 'factor_value': r['loading'], 'gene_index': r['gene_index']} for r in loadings],
                             index='pigean-gene-factor', q=q, limit=None, continuation=None,
                             scope={'mechanism_id': identity, 'factor_key': row['factor_key'], 'eaggl_factor_id': row['eaggl_factor_id'],
                                    'eaggl_import_id': self.import_id})
        self.gene_loadings[identity] = {r['symbol']: r['loading'] for r in loadings}
        self.gene_index.update({r['symbol']: r['gene_index'] for r in loadings})
        statements = []
        projections = self.reader.rows(statements, ANCHOR_PROJECTION_SQL, [self.generation_id, PROJECTION_SCOPE, row['factor_key']],
                                       PROJECTION_COLUMNS + GENE_SET_COLUMNS)
        projections.sort(key=lambda r: (r['joint_rank'], r['marginal_rank'], r['gene_set_id']))
        set_key = _add_rows(self.store, 'gene_set-factor-' + tag, self.generation_id,
                            ['factor_gene_set_projections', 'cfde_gene_sets', 'cfde_gene_set_collections'], statements,
                            [{**_scope_row(row), 'gene_set': r['gene_set_id'], 'factor_value': r['joint_loading'],
                              **{c: r[c] for c in PROJECTION_COLUMNS[2:] + GENE_SET_COLUMNS}} for r in projections],
                            index='pigean-gene-set-factor', q=q, limit=None, continuation=None,
                            scope={'mechanism_id': identity, 'factor_key': row['factor_key'], 'projection_scope': PROJECTION_SCOPE})
        self.projections[identity] = {r['gene_set_id']: r for r in projections}
        self.gene_set_info.update({r['gene_set_id']: {c: r[c] for c in GENE_SET_COLUMNS} for r in projections})
        self.anchor_captures[identity] = (gene_key, set_key)
        self.bioindex += [{'scope': 'factor', 'kind': 'gene', 'mechanism_id': identity, 'artifact_id': gene_key},
                          {'scope': 'factor', 'kind': 'gene_set', 'mechanism_id': identity, 'artifact_id': set_key}]

    def connect(self, target, derived_from, items, scoring):
        items = sorted(items, key=lambda item: (-item['aggregate_score'], item['candidate']['node_id']))[:self.limit]
        response = {'candidates': items, 'candidate_count': len(items),
                    'graph': {'nodes': [item['candidate'] for item in items], 'edges': [edge for item in items for edge in item['edges']]}}
        self.connections[target] = _add_derived(self.store, 'connections-' + target, self.generation_id, derived_from,
            {'method': 'POST', 'status': 200, 'source': {'target_type': target, 'scoring': scoring},
             'request': {**self.request, 'target_type': target}, 'response': response})
        for item in items:
            require(item['candidate']['node_id'] not in self.candidates, 'Candidate repeated across typed queries')
            self.candidates[item['candidate']['node_id']] = item
        return [item['candidate']['node_key'] for item in items]

    def typed_genes(self):
        genes = {}
        for identity in self.factor_ids:
            for symbol, loading in self.gene_loadings[identity].items(): genes.setdefault(symbol, {})[identity] = loading
        items = [_candidate({'node_id': 'gene:' + symbol, 'node_type': 'gene', 'node_key': symbol, 'label': symbol, 'subtitle': 'Gene'},
                            [_gene_edge(self.generation_id, a, symbol, loads[a]) for a in sorted(loads)], self.count, _mean(loads.values(), self.count))
                 for symbol, loads in genes.items()]
        return sorted(self.connect('gene', [self.anchor_captures[a][0] for a in self.factor_ids], items,
                                   'raw_score = eaggl_gene_loadings.loading; aggregate_score = sum over anchors / anchor count'))

    def typed_gene_sets(self):
        sets = {}
        for identity in self.factor_ids:
            for gene_set_id, row in self.projections[identity].items(): sets.setdefault(gene_set_id, {})[identity] = row
        items = []
        for gene_set_id, rows in sets.items():
            info = self.gene_set_info[gene_set_id]
            node = {'node_id': 'gene_set:' + gene_set_id, 'node_type': 'gene_set', 'node_key': gene_set_id, 'label': info['gene_set_name'],
                    'subtitle': f'{info["library"]} gene set ({info["cfde_label"]})'}
            items.append(_candidate(node, [_gene_set_edge(self.generation_id, a, rows[a]) for a in sorted(rows)], self.count,
                                    _mean([row['joint_loading'] for row in rows.values()], self.count)))
        return sorted(self.connect('gene_set', [self.anchor_captures[a][1] for a in self.factor_ids], items,
                                   f'raw_score = factor_gene_set_projections.joint_loading (scope {PROJECTION_SCOPE}); '
                                   'aggregate_score = sum over anchors / anchor count; anchors without a retained projection row contribute 0'))

    def typed_traits(self, anchors):
        traits = {}
        for row in anchors: traits.setdefault(row['kpn_trait_id'], []).append(row)
        items = [_candidate({'node_id': _trait_node(trait), 'node_type': 'trait', 'node_key': trait, 'label': rows[0]['phenotype_name'],
                             'subtitle': f'KPN trait {trait} ({rows[0]["trait_group"] or "ungrouped"})'},
                            [_trait_edge(self.generation_id, row['public_id'], trait) for row in rows], self.count, len(rows) / self.count)
                 for trait, rows in traits.items()]
        self.connect('trait', ['reference-factors'], items, 'factor_of_trait membership; aggregate_score = fraction of anchors fitted in the trait')
        return {trait: sorted(row['public_id'] for row in rows) for trait, rows in traits.items()}

    def typed_factors(self):
        statements, overlap = [], []
        if self.gene_candidates:
            overlap = self.reader.batched(statements, 'SELECT e.factor_id,g.symbol,l.loading FROM eaggl_gene_loadings l JOIN eaggl_factors e '
                                          'ON e.import_id=l.import_id AND e.factor_index=l.factor_index JOIN eaggl_genes g ON g.import_id=l.import_id '
                                          'AND g.gene_index=l.gene_index WHERE l.import_id=%s AND l.gene_index IN ({marks})',
                                          [self.import_id], sorted(self.gene_index[s] for s in self.gene_candidates), ('factor_id', 'symbol', 'loading'))
        overlap.sort(key=lambda r: (r['factor_id'], r['symbol']))
        _add_rows(self.store, 'factor-gene-overlap', self.generation_id, ['eaggl_gene_loadings', 'eaggl_factors', 'eaggl_genes'], statements, overlap,
                  eaggl_import_id=self.import_id, gene_candidates=self.gene_candidates)
        for row in overlap: self.profiles.setdefault(row['factor_id'], {})[row['symbol']] = row['loading']
        anchors = {a: {s: self.gene_loadings[a].get(s, 0.0) for s in self.gene_candidates} for a in self.factor_ids}
        shares = {}
        for factor_id, profile in self.profiles.items():
            if factor_id in self.anchor_eaggl: continue
            per_anchor = {}
            for identity, anchor in anchors.items():
                total = math.fsum(anchor.values())
                shared = sorted(s for s in self.gene_candidates if anchor[s] > 0 and profile.get(s, 0) > 0)
                if total > 0 and shared: per_anchor[identity] = (math.fsum(min(anchor[s], profile[s]) for s in shared) / total, shared)
            if per_anchor: shares[factor_id] = per_anchor
        chosen = sorted(shares, key=lambda f: (-_mean([v[0] for v in shares[f].values()], self.count), f))[:self.limit]
        rows = self.factor_rows_by('candidate-factors', 'eaggl_factor_id', chosen, requested_eaggl_factor_ids=chosen)
        items = []
        for row in rows:
            public, per_anchor = row['public_id'], shares[row['eaggl_factor_id']]
            self.factor_rows[public] = row
            edges = [_edge(self.generation_id, 'factor_factor_shared_genes', 'shared_candidate_gene_loading', a, public, per_anchor[a][0],
                           {'shared_genes': per_anchor[a][1], 'shared_gene_count': len(per_anchor[a][1]), 'gene_candidate_count': len(self.gene_candidates)})
                     for a in sorted(per_anchor)]
            items.append(_candidate({'node_id': public, 'node_type': 'factor', 'node_key': _factor_of(public), 'label': row['label'],
                                     'subtitle': f'{row["phenotype_name"]} ({_factor_of(public)})'},
                                    edges, self.count, _mean([v[0] for v in per_anchor.values()], self.count)))
        self.connect('factor', ['factor-gene-overlap', 'candidate-factors'], items,
                     'raw_score = sum_g min(anchor_g, factor_g) / sum_g anchor_g over the gene candidates g; aggregate_score = sum over anchors / anchor count')

    def contextual(self, retained):
        """Every direct relationship among the retained nodes, derived like collect_package's contextual query."""
        members = set(retained)
        factors = [node for node in retained if node in self.factor_rows]
        others = [node for node in factors if node not in self.mechanisms]
        genes = sorted(node[len('gene:'):] for node in retained if node.startswith('gene:'))
        gene_sets = sorted(node[len('gene_set:'):] for node in retained if node.startswith('gene_set:'))
        statements, rows = [], []
        if others and gene_sets:
            keys = sorted(self.factor_rows[node]['factor_key'] for node in others)
            rows = self.reader.batched(statements, PROJECTION_SQL, [self.generation_id, PROJECTION_SCOPE, *keys], gene_sets, PROJECTION_COLUMNS,
                                       keys=_marks(keys))
        rows.sort(key=lambda r: (r['factor_key'], r['gene_set_id']))
        _add_rows(self.store, 'contextual-gene-set-projections', self.generation_id, ['factor_gene_set_projections'], statements, rows)
        by_key = {self.factor_rows[node]['factor_key']: node for node in others}
        edges = {}
        def add(edge):
            require(edges.setdefault(edge['id'], edge) == edge, f'Conflicting contextual edge: {edge["id"]}')
        for node in factors:
            row = self.factor_rows[node]
            loads = self.gene_loadings[node] if node in self.mechanisms else self.profiles.get(row['eaggl_factor_id'], {})
            for symbol in genes:
                if symbol in loads: add(_gene_edge(self.generation_id, node, symbol, loads[symbol]))
            for gene_set_id in gene_sets:
                if gene_set_id in self.projections.get(node, {}): add(_gene_set_edge(self.generation_id, node, self.projections[node][gene_set_id]))
            if _trait_node(row['kpn_trait_id']) in members: add(_trait_edge(self.generation_id, node, row['kpn_trait_id']))
        for row in rows: add(_gene_set_edge(self.generation_id, by_key[row['factor_key']], row))
        for node in others:
            for edge in self.candidates[node]['edges']:
                if edge['source'] in members: add(edge)
        derived = [key for pair in self.anchor_captures.values() for key in pair] + ['factor-gene-overlap', 'connections-factor', 'contextual-gene-set-projections']
        return _add_derived(self.store, 'contextual', self.generation_id, derived,
            {'method': 'POST', 'status': 200, 'source': {'scope': 'direct factor relationships among the requested nodes'},
             'request': {'node_ids': sorted(members), 'model': KPN_MODEL}, 'response': {'edges': [edges[key] for key in sorted(edges)]}})


# --------------------------------------------------------------------------------------
# Gene sets: exact CFDE DAPPER GeneSets plus the builder's alias binding


def _class_groups(dapper):
    groups = {}
    for group, cls in dapper.groups.items(): groups.setdefault(cls, group)
    return groups


def _class_of(identity):
    return DAPPER_REFERENCE.fullmatch(identity).group(1)


def _resolve_gene_set(dapper, row, collection, base_prefixes, class_groups):
    """(exact GeneSet, dependency objects, extra prefixes, reason); reason is None when exact."""
    metadata = row['metadata'] if isinstance(row.get('metadata'), dict) else {}
    payload = metadata.get('dapper_gene_set')
    if not isinstance(payload, dict): return None, [], {}, 'cfde_gene_sets.metadata.dapper_gene_set is not stored'
    node = {key: value for key, value in payload.items() if key not in NON_IDENTITY_GENE_SET_SLOTS}
    if node.get('id') != row['gene_set_id']: return None, [], {}, 'Stored GeneSet payload id differs from gene_set_id'
    provenance = (collection or {}).get('provenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    pool = {}
    for source in (metadata.get('dapper_dependencies'), *(provenance.get(group) for group in PROVENANCE_GROUPS)):
        for item in source if isinstance(source, list) else []:
            if isinstance(item, dict) and isinstance(item.get('id'), str): pool.setdefault(item['id'], item)
    dependencies, pending, seen = [], sorted(_references(node, set())), {node['id']}
    while pending:
        identity = pending.pop(0)
        if identity in seen: continue
        seen.add(identity)
        if identity not in pool: return None, [], {}, f'DAPPER dependency is not stored: {identity}'
        if _class_of(identity) not in class_groups: return None, [], {}, f'Unsupported DAPPER dependency class: {identity}'
        dependencies.append(pool[identity]); pending.extend(sorted(_references(pool[identity], set())))
    declared = dict(DEFAULT_GENE_SET_PREFIXES)
    for source in (provenance.get('prefixes'), metadata.get('dapper_prefixes')):
        if isinstance(source, dict): declared.update({k: v for k, v in source.items() if isinstance(k, str) and isinstance(v, str)})
    values = [value for item in [node, *dependencies] for value in _strings(item)]
    used = {name: uri for name, uri in sorted(declared.items()) if any(value.startswith(name + ':') for value in values)}
    if any(base_prefixes.get(name, uri) != uri for name, uri in used.items()): return None, [], {}, 'GeneSet CURIE prefix conflicts with package prefixes'
    document = {'prefixes': {**base_prefixes, **used}, 'gene_sets': [node]}
    for item in dependencies: document.setdefault(class_groups[_class_of(item['id'])], []).append(item)
    try:
        canonical_json(document); dapper.validate(document)
    except (EvidenceBuildError, ValueError, KeyError, TypeError) as exc:
        return None, [], {}, ('DAPPER validation failed: ' + str(exc))[:500]
    return node, dependencies, used, None


def _gene_set_objects(evidence, base_prefixes):
    """Per gene-set candidate id: DAPPER objects for the context, extra prefixes and the builder binding."""
    store, reader, dapper, generation_id = evidence.store, evidence.reader, evidence.dapper, evidence.generation_id
    wanted = evidence.gene_set_candidates
    statements = []
    rows = reader.batched(statements, GENE_SET_SQL, [generation_id], wanted,
                          ('gene_set_id', 'collection_id', 'gene_set_name', 'library', 'n_genes', 'n_genes_in_eaggl_universe',
                           'legacy_source_key', 'metadata')) if wanted else []
    rows.sort(key=lambda row: row['gene_set_id'])
    _add_rows(store, 'gene-set-payloads', generation_id, ['cfde_gene_sets'], statements, rows)
    identities = sorted({row['collection_id'] for row in rows})
    statements = []
    collections = reader.batched(statements, COLLECTION_SQL, [generation_id], identities,
                                 ('collection_id', 'cfde_label', 'library', 'n_sets', 'provenance')) if identities else []
    collections.sort(key=lambda row: row['collection_id'])
    _add_rows(store, 'gene-set-collections', generation_id, ['cfde_gene_set_collections'], statements, collections)
    by_id = {row['gene_set_id']: (i, row) for i, row in enumerate(rows)}
    require(set(by_id) == set(wanted), f'Gene-set candidates are missing from cfde_gene_sets: {sorted(set(wanted) - set(by_id))[:5]}')
    by_collection = {row['collection_id']: row for row in collections}
    class_groups, shared, resolution, objects = _class_groups(dapper), {}, [], {}
    for gene_set_id in wanted:
        i, row = by_id[gene_set_id]; node_id = 'gene_set:' + gene_set_id
        exact, dependencies, used, reason = _resolve_gene_set(dapper, row, by_collection.get(row['collection_id']), base_prefixes, class_groups)
        if exact and any(shared.get(item['id'], item) != item for item in dependencies):
            exact, dependencies, used, reason = None, [], {}, 'A stored DAPPER dependency conflicts with another gene set'
        if exact: shared.update({item['id']: item for item in dependencies})
        alias = {'name': row['gene_set_name'], 'member_type': 'gene', 'alternate_identifier': [node_id]}
        if exact: alias['was_derived_from'] = [exact['id']]
        alias['id'] = dapper.compute_id(alias, 'GeneSet', dapper.schema)
        activity = any(_class_of(item['id']) == 'Activity' for item in dependencies)
        objects[node_id] = {'prefixes': used,
            'objects': [('gene_sets', alias)] + ([('gene_sets', exact)] + [(class_groups[_class_of(item['id'])], item) for item in dependencies] if exact else []),
            'binding': {'dapper_id': alias['id'], 'import_id': generation_id, 'membership_status': 'loaded' if exact else 'not_loaded',
                        'construction_provenance_status': 'generating_activity_loaded' if activity else 'not_loaded',
                        'provenance_refs': [ref('gene-set-payloads', f'/data/{i}'), ref('gene-set-resolution', f'/gene_sets/{len(resolution)}')]}}
        resolution.append({'node_id': node_id, 'gene_set_id': gene_set_id, 'alias_dapper_id': alias['id'],
                           'status': 'exact_dapper_gene_set' if exact else 'alias_only', 'reason': reason,
                           'dapper_gene_set_id': exact['id'] if exact else None, 'dependency_ids': sorted(item['id'] for item in dependencies)})
    _add_derived(store, 'gene-set-resolution', generation_id, ['gene-set-payloads', 'gene-set-collections'],
                 {'policy': 'Each gene-set candidate binds an alias GeneSet whose alternate_identifier is the candidate id. The exact CFDE '
                            'GeneSet (identity slots only, id recomputed) and its dependencies are added when stored; the alias then '
                            'was_derived_from it.', 'gene_sets': resolution})
    return objects


# --------------------------------------------------------------------------------------
# Collector


def collect_reference_package(*, gap_id, factor_ids, output, dapper, project_root, dismech_source, dismech_index,
                              generation_id, connection_factory, selected_graphs=('biomarkerkg', 'prokn'), max_accounts=3,
                              selection_metadata=None, limit=100, max_nodes=250, max_edges=1000, user_inputs=None):
    """Resolve a gap and KPN factor public ids, freeze MySQL reference evidence, then build.

    Returns the builder's BuiltPackage, like collect_package (`.package`, `.write(path)`).
    connection_factory() returns a DB-API connection; only reads run, in one transaction.
    """
    started = time.monotonic()
    project_root, dismech_source, dismech_index = Path(project_root).resolve(), Path(dismech_source).resolve(), Path(dismech_index)
    factor_ids = unique(factor_ids, 'factor IDs')
    require(0 < len(factor_ids) <= 10, 'Supply between one and ten EAGGL factors')
    require(type(limit) is int and 1 <= limit <= 100, 'limit must be between 1 and 100')
    require(type(max_nodes) is int and len(factor_ids) <= max_nodes <= 250, 'max_nodes must preserve anchors and be at most 250')
    require(type(max_edges) is int and 1 <= max_edges <= 1000, 'max_edges must be between 1 and 1000')
    require(isinstance(generation_id, str) and bool(GENERATION_RE.fullmatch(generation_id)), 'A reference generation id is required')
    for identity in factor_ids:
        try: parse_public_id(identity)
        except ReferenceError as exc: raise EvidenceBuildError(f'Use a KPN factor public id: {identity}') from exc
    gap_manifest, gap_record, hashes, attachments = _resolve_gap(dismech_index, gap_id)
    store = CaptureStore(output)
    if selection_metadata:
        require(set(selection_metadata['origins']) == set(factor_ids), 'Selection provenance differs from selected anchors')
        store.add('selection-provenance', canonical_json(selection_metadata), 'json', filename='selection-provenance.json')
    timings = {'format': 'reveal.evidence-collection-timings/1', 'source': 'mysql', 'status': 'failed', 'stages': []}

    def stage(name, began, **values):
        timings['stages'].append({'stage': name, 'seconds': round(time.monotonic() - began, 6), 'status': 'completed', **values})

    connection = None
    try:
        began = time.monotonic()
        gap_node, context, dismech, prefixes = _dismech_context(store, dapper, dismech_source, gap_manifest, gap_record, hashes,
                                                                attachments, factor_ids, selection_metadata)
        stage('dismech_context', began)
        began = time.monotonic()
        connection = connection_factory()
        evidence = _Evidence(store, _Reader(connection), dapper, generation_id, factor_ids, limit).read()
        gene_sets = _gene_set_objects(evidence, prefixes)
        # One ranking over all typed candidates, as in collect_package.
        candidates = evidence.candidates
        ranking = sorted(candidates, key=lambda k: (-candidates[k]['aggregate_score'], k))
        requested = sorted(set(factor_ids) | set(ranking[:max_nodes - len(factor_ids)]))
        contextual_id = evidence.contextual(requested)
        stage('reference_reads', began, statement_count=evidence.reader.count)
        try: connection.rollback()
        except Exception: pass

        instructions = []
        for i, path in enumerate(INSTRUCTIONS):
            data = (project_root / path).read_bytes(); key = 'instruction-' + str(i)
            store.add(key, data, 'text', filename=Path(path).name, origin=path)
            instructions.append({'artifact_id': key, 'path': path, 'sha256': sha256(data)})
        builder_bytes = Path(__file__).with_name('evidence_package.py').read_bytes()
        store.add('assembly-builder-source', builder_bytes, 'text', filename='evidence_package.py')
        store.add('collector-source', Path(__file__).read_bytes(), 'text', filename='reference_evidence.py')
        context['mechanisms'] += [evidence.mechanism_nodes[identity] for identity in factor_ids]
        supplied = None
        if user_inputs is not None:
            from .user_inputs import read
            supplied = deepcopy(user_inputs)
            # Capture once, before byte-budget retries. Storage references and exact
            # upload bytes stay frozen; the source artifacts remain private.
            for upload in supplied['uploads']:
                original = read(upload['storage']); extracted = read(upload['extraction']['storage'])
                key = 'user-upload-' + upload['id']
                store.add(key, original, 'binary', filename=upload['filename'], media_type=upload['media_type'], private=True)
                store.add(key + '-text', extracted, 'json', filename=upload['id'] + '-text.json', private=True)
                upload.update(original_artifact_id=key, extraction_artifact_id=key + '-text', content=decode(extracted))

        def build_spec(retained):
            document, package_prefixes, bindings = {key: list(value) for key, value in context.items()}, dict(prefixes), {}
            known = {node['id'] for nodes in document.values() for node in nodes}
            for identity in retained:
                if identity not in gene_sets: continue
                bindings[identity] = gene_sets[identity]['binding']; package_prefixes.update(gene_sets[identity]['prefixes'])
                for group, node in gene_sets[identity]['objects']:
                    if node['id'] not in known: known.add(node['id']); document.setdefault(group, []).append(node)
            document['prefixes'] = package_prefixes
            return {'input_version': INPUT_VERSION, 'prefixes': package_prefixes,
                    'identifier_policy': {'source_local_prefixes': ['factor', 'gene', 'gene_set', 'trait', 'cfde'],
                                          'dismech_record_ids': 'Opaque aliases resolved through source_ref and source revision.',
                                          'preserve_existing_dapper_payloads': True},
                    'dapper_pin': {'name': 'configured-snapshot', 'snapshot_sha256': dapper.manifest['snapshot_sha256']},
                    'selection': {'knowledge_gap_id': gap_node['id'], 'dismech_mechanism_ids': sorted(dismech['mechanisms']),
                                  'eaggl_mechanism_ids': factor_ids,
                                  'origins': (selection_metadata or {}).get('origins', {k: 'user_supplied' for k in factor_ids}),
                                  'dismissed_eaggl_ids': (selection_metadata or {}).get('dismissed_eaggl_ids', []),
                                  'semantic_retrieval': (selection_metadata or {}).get('semantic_retrieval', {'status': 'not_computed', 'embedding_run_id': None}),
                                  'expansion_policy': {'rounds': 1, 'reducer': 'mean', 'connection_scope': 'direct', 'context': ''}},
                    **({'user_inputs': supplied} if supplied is not None else {}),
                    'dismech': dismech, 'mechanisms': evidence.mechanisms, 'gene_sets': bindings, 'dapper_context': document,
                    'captures': {'connections': {target: {'status': 'captured', 'artifact_id': evidence.connections[target]} for target in TARGETS},
                                 'contextual': {'status': 'captured', 'artifact_id': contextual_id}, 'bioindex': evidence.bioindex},
                    'artifacts': store.artifacts,
                    'policy': {'max_nodes': max_nodes, 'max_edges': max_edges, 'max_package_bytes': MAX_PACKAGE_BYTES, 'max_anchors': 10,
                               'max_dismech_mechanisms': 10, 'max_candidates_per_target': 100, 'retain_node_ids': retained,
                               # Only for the trait-scope PIGEAN queries MySQL cannot answer (see the module docstring).
                               'allow_incomplete_capture': True},
                    'authoring': {'skill': instructions[0], 'contract': instructions[1], 'references': instructions[2:],
                                  'required_question': gap_node['id'], 'max_accounts': max_accounts,
                                  'assembly_builder': {'version': BUILD_VERSION, 'source_sha256': sha256(builder_bytes)}},
                    'external_evidence': {'status': 'not_queried', 'selected_graphs': list(selected_graphs), 'ledger': [], 'assertions': []}}

        # Exact GeneSet memberships can exceed the package byte budget. Then the lowest-ranked
        # candidates are dropped (anchors always stay); coverage lists every omitted node.
        began = time.monotonic(); keep = len(requested) - len(factor_ids); reductions = []
        while True:
            spec = build_spec(sorted(set(factor_ids) | set(ranking[:keep])))
            try:
                built = build_package(spec, store.blobs, dapper); break
            except EvidenceBuildError as exc:
                if 'exceeds byte budget' not in str(exc) or keep == 0: raise
                reductions.append(keep); keep = keep * 3 // 4
        unexpected = [b for b in built.package['readiness']['capture_blockers'] if not TRAIT_CAPTURE_BLOCKER.fullmatch(b)]
        require(not unexpected, f'Required capture unavailable: {unexpected}')
        (store.directory / 'build-input.json').write_bytes(canonical_json(spec))
        built.write(store.directory / 'package')
        stage('validated_assembly', began, retained_candidates=keep, byte_budget_reductions=reductions)
        timings['status'] = 'completed'
        return built
    except Exception as exc:
        (store.directory / 'collection-error.json').write_bytes(canonical_json({'error': str(exc), 'artifacts': store.artifacts}))
        raise
    finally:
        if connection is not None:
            try: connection.close()
            except Exception: pass
        timings['elapsed_seconds'] = round(time.monotonic() - started, 6)
        try:
            (store.directory / 'collection-timings.json').write_bytes(canonical_json(timings))
        except OSError:
            pass  # Optional telemetry cannot mask a source failure or a valid package.
