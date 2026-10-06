"""Small relevance summary for Jev; source audit records stay in REVEAL."""
from copy import deepcopy
from .repository import canonical


def _text(value):
    return value.get('text', value.get('excerpt', '')) if isinstance(value, dict) else value or ''


def _scientific_value(value):
    """Readable qualifiers, without excerpt hashes or arbitrary metadata."""
    if isinstance(value, list): return [_scientific_value(item) for item in value]
    if not isinstance(value, dict): return deepcopy(value)
    for key in ('text', 'excerpt', 'label', 'name'):
        if isinstance(value.get(key), str): return value[key]
    return {key: _scientific_value(value[key]) for key in ('value', 'unit', 'term') if key in value}


def _rows(table):
    return [dict(zip(table.get('columns', []), row)) for row in table.get('rows', [])]


def model_state(state):
    """Project scientific names, exact loadings, source labels and user context.

    No record IDs, hashes, provenance graphs or reconstruction rules are needed
    for this forecast. Exact source state is separately retained by the caller.
    """
    definitions = _rows(state.get('gene_sets', {}))
    collections = state.get('collections', {})
    sources, source_keys = [], {}
    source_fields = ('organism', 'species', 'taxon', 'taxon_id', 'assay', 'data_type', 'tissue', 'cell_type', 'genome_build')
    def source(definition):
        collection_id = definition.get('collection_id')
        context = collections.get(collection_id, {}).get('context', {})
        fallback = definition.get('context', {})
        profile = {field: _scientific_value(fallback.get(field, context.get(field))) for field in source_fields}
        # Mixed species or assays must never inherit the first GeneSet's label.
        identity = (collection_id, definition.get('library'), canonical(profile))
        if identity not in source_keys:
            name = 'source_' + str(len(sources) + 1)
            record = {'source': name, 'library': definition.get('library'),
                'description': _text(context.get('name')) or definition.get('library') or 'Unknown source'}
            for field in source_fields:
                value = profile[field]
                if value not in (None, '', [], {}): record[field] = deepcopy(value)
            if not any(record.get(field) for field in ('organism', 'species', 'taxon', 'taxon_id')): record['species'] = 'unknown'
            source_keys[identity] = name; sources.append(record)
        return source_keys[identity]
    factors = []
    for factor in state.get('factors', []):
        gene_sets = []
        for loading in _rows(factor.get('gene_sets', {})):
            index = loading.get('gene_set_index')
            definition = definitions[index] if type(index) is int and 0 <= index < len(definitions) else {}
            context = definition.get('context', {})
            extra = {field: _scientific_value(context[field]) for field in (
                'perturbation', 'perturbation_target', 'treatment', 'dose', 'timepoint', 'condition', 'comparison', 'tissue', 'cell_type')
                if context.get(field) not in (None, '', [], {})}
            # Original decimal strings preserve precision without sending both
            # those strings and duplicate floating-point approximations.
            joint = loading.get('joint_loading_text')
            marginal = loading.get('marginal_loading_text')
            gene_sets.append([definition.get('name') or 'Metadata unavailable', source(definition),
                joint if joint is not None else loading.get('joint_loading'),
                marginal if marginal is not None else loading.get('marginal_loading'), definition.get('n_genes'), extra])
        columns = ['name', 'source', 'joint_loading', 'marginal_loading', 'gene_count', 'context']
        if all(not row[-1] for row in gene_sets):
            columns.pop(); gene_sets = [row[:-1] for row in gene_sets]
        trait = {key: deepcopy(value) for key, value in factor.get('trait', {}).items() if key in ('name', 'trait_group', 'trait_type')}
        factors.append({'name': factor.get('label'), 'trait': trait,
            'top_genes': {'columns': ['gene', 'loading'],
                'rows': [[row.get('symbol'), row.get('loading')] for row in _rows(factor.get('genes', {}))]},
            'top_gene_sets': {'columns': columns, 'rows': gene_sets}})
    gap = state.get('gap', {}); inputs = state.get('user_inputs', {}); coverage = state.get('coverage', {})
    return {'knowledge_gap': {'question': _text(gap.get('text')), 'rationale': _text(gap.get('rationale')), 'disease': gap.get('disease')},
        'dismech_mechanisms': [{'name': item.get('name'), 'description': _text(item.get('description'))}
            for item in state.get('dismech', {}).get('mechanisms', [])],
        'eaggl_mechanisms': factors, 'gene_set_sources': sources,
        'additional_context': {field: _text(inputs.get(field)) for field in ('research_direction', 'context', 'hypotheses')},
        'attachments': [{'text': '\n'.join(segment.get('text', '') for segment in upload.get('segments', []))}
            for upload in inputs.get('uploads', [])],
        'coverage': {'top_n_per_mechanism': state.get('selection', {}).get('top_n', 50),
            'mechanisms': coverage.get('factor_count', len(factors)), 'gene_loadings': coverage.get('gene_loading_count', 0),
            'gene_set_loadings': coverage.get('gene_set_loading_count', 0), 'missing_metadata': bool(coverage.get('missing')),
            'bounded_text_excerpts': bool(coverage.get('truncations'))},
        'reading_notes': 'Top loadings are a bounded sample, not proof of absence. GeneSet loading strings preserve source precision. Co-loading and perturbation names do not establish signature membership.'}
