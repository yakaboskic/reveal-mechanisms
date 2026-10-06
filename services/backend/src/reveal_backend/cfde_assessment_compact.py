"""Lossless, readable column/default encoding for bounded assessment inputs.

This changes representation only. Scientific rows, decimal strings, identities,
hashes, source locators and coverage survive an exact reconstruction. No model,
source query, token estimate, truncation or scientific inference occurs here.
"""
from collections import Counter
from copy import deepcopy
import json
from os.path import commonprefix


FORMAT = 'reveal.cfde-assessment-compaction/1'
RULES = (
    'Lossless table encoding; omitted fields mean the explicit defaults below, never absent evidence. '
    'GeneSet collection_index selects compaction.gene_sets.collection_ids and its context/column defaults. '
    'name_suffix follows that collection name_prefix; context.* columns override context defaults. '
    'Derived context fields copy the stated reconstructed row column (array_column wraps it in a list). '
    'compaction.factor_tables defaults apply to the listed factor indexes; removed decimal-text columns equal the '
    'JSON number spelling of their named numeric column, verified before encoding. '
    'Integers in the listed collection provenance tables reference compaction.provenance.records. '
    'All other values, ordering, rows, numeric precision and hexadecimal hashes are unchanged.'
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _same(first, second):
    # Python equality alone would conflate True/1 and 1/1.0.
    return _json(first) == _json(second)


def _table(value):
    if not isinstance(value, dict): return False
    columns, rows = value.get('columns'), value.get('rows')
    return (isinstance(columns, list) and all(isinstance(key, str) for key in columns)
            and len(set(columns)) == len(columns) and isinstance(rows, list)
            and all(isinstance(row, list) and len(row) == len(columns) for row in rows))


def _gene_sets(state):
    table = state.get('gene_sets')
    if not _table(table) or not table['rows']: return None
    columns = table['columns']; indexes = {key: index for index, key in enumerate(columns)}
    if not {'id', 'name', 'collection_id', 'context'} <= indexes.keys(): return None
    if any(key in indexes for key in ('collection_index', 'name_suffix')): return None
    rows = table['rows']
    if not all(isinstance(row[indexes['collection_id']], (str, type(None))) and
               isinstance(row[indexes['name']], str) and isinstance(row[indexes['context']], dict)
               for row in rows): return None
    contexts = [deepcopy(row[indexes['context']]) for row in rows]
    derived = {}
    for field, source, array in (('gmt_entry', 'id', False), ('alternate_identifier', 'name', True)):
        if all(field in context and _same(context[field], [row[indexes[source]]] if array else row[indexes[source]])
               for row, context in zip(rows, contexts)):
            derived[field] = {'array_column' if array else 'column': source}
            for context in contexts: context.pop(field)
    identities = list(dict.fromkeys(row[indexes['collection_id']] for row in rows))
    groups = [[i for i, row in enumerate(rows) if row[indexes['collection_id']] == identity] for identity in identities]
    defaults, prefixes = [], []
    for group in groups:
        first = contexts[group[0]]
        common = {key: deepcopy(value) for key, value in first.items()
                  if all(key in contexts[i] and _same(value, contexts[i][key]) for i in group)}
        defaults.append(common)
        for i in group:
            for key in common: contexts[i].pop(key)
        prefix = commonprefix([rows[i][indexes['name']] for i in group]) if len(group) >= 10 else ''
        # Keep a visible word/identifier boundary in the shared prefix.
        boundary = max((prefix.rfind(character) for character in ('_', '-', ':', ' ')), default=-1) + 1
        prefixes.append(prefix[:boundary] if boundary >= 8 else '')
    constant_columns = [key for key in ('library', 'n_genes', 'n_genes_in_eaggl_universe') if key in indexes and
        all(all(_same(rows[group[0]][indexes[key]], rows[i][indexes[key]]) for i in group) for group in groups)]
    column_defaults = [{key: deepcopy(rows[group[0]][indexes[key]]) for key in constant_columns} for group in groups]
    context_columns = sorted(contexts[0]) if all(set(context) == set(contexts[0]) for context in contexts) else None
    if context_columns is not None and any('context.' + key in indexes for key in context_columns): context_columns = None
    encoded_columns = []
    for key in columns:
        if key in constant_columns: continue
        if key == 'collection_id': encoded_columns.append('collection_index')
        elif key == 'name' and any(prefixes): encoded_columns.append('name_suffix')
        elif key == 'context' and context_columns is not None:
            encoded_columns.extend('context.' + field for field in context_columns)
        else: encoded_columns.append(key)
    collection_indexes = {identity: index for index, identity in enumerate(identities)}
    encoded_rows = []
    for i, row in enumerate(rows):
        group = collection_indexes[row[indexes['collection_id']]]
        record = dict(zip(columns, row)); record['context'] = contexts[i]
        record['collection_index'] = group
        record['name_suffix'] = record['name'][len(prefixes[group]):]
        if context_columns is not None:
            record.update({'context.' + key: value for key, value in contexts[i].items()})
        encoded_rows.append([record[key] for key in encoded_columns])
    table.update(columns=encoded_columns, rows=encoded_rows)
    return {'original_columns': columns, 'collection_ids': identities, 'context_defaults': defaults,
            'column_defaults': column_defaults, 'name_prefixes': prefixes,
            'derived_context': derived, 'context_columns': context_columns}


def _factors(state):
    factors = state.get('factors')
    if not isinstance(factors, list): return {}
    rules = {}
    for group in ('genes', 'gene_sets'):
        indexes = [i for i, factor in enumerate(factors) if isinstance(factor, dict) and _table(factor.get(group))]
        if not indexes: continue
        tables = [factors[i][group] for i in indexes]
        columns = tables[0]['columns']
        equal_columns = all(table['columns'] == columns for table in tables)
        derived = {}
        if equal_columns and any(table['rows'] for table in tables):
            for text, numeric in (('joint_loading_text', 'joint_loading'), ('marginal_loading_text', 'marginal_loading')):
                if text not in columns or numeric not in columns: continue
                t, n = columns.index(text), columns.index(numeric)
                if all(type(row[n]) in (int, float) and isinstance(row[t], str) and row[t] == _json(row[n])
                       for table in tables for row in table['rows']):
                    derived[text] = numeric
        if derived:
            keep = [i for i, key in enumerate(columns) if key not in derived]
            for table in tables:
                table['columns'] = [columns[i] for i in keep]
                table['rows'] = [[row[i] for i in keep] for row in table['rows']]
        defaults = {key: deepcopy(value) for key, value in tables[0].items() if key != 'rows' and
                    all(key in table and _same(table[key], value) for table in tables)}
        # One table alone gains nothing from hoisting unchanged metadata.
        if len(tables) < 2: defaults = {}
        for table in tables:
            for key in defaults: table.pop(key)
        if defaults or derived:
            rules[group] = {'factor_indexes': indexes, 'defaults': defaults,
                            'original_columns': columns if derived else None, 'decimal_text_columns': derived}
    return rules


def _provenance(state):
    collections = state.get('collections')
    if not isinstance(collections, dict): return None
    tables = []
    for identity, collection in collections.items():
        provenance = collection.get('provenance') if isinstance(collection, dict) else None
        if not isinstance(provenance, dict): continue
        for group, values in provenance.items():
            if isinstance(values, list) and values and all(isinstance(value, dict) for value in values):
                tables.append((identity, group, values))
    counts = Counter(_json(value) for _, _, values in tables for value in values)
    repeated = {value for value, count in counts.items() if count > 1 and len(value.encode()) >= 100}
    if not repeated: return None
    records, lookup, paths = [], {}, []
    for identity, group, values in tables:
        changed = False
        for i, value in enumerate(values):
            encoded = _json(value)
            if encoded not in repeated: continue
            if encoded not in lookup:
                lookup[encoded] = len(records); records.append(value)
            values[i] = lookup[encoded]; changed = True
        if changed: paths.append([identity, group])
    return {'records': records, 'tables': paths}


def compact(state):
    """Return an independently owned lossless projection with explicit rules."""
    if not isinstance(state, dict): raise ValueError('Assessment state must be an object')
    if 'compaction' in state:
        if isinstance(state['compaction'], dict) and state['compaction'].get('format') == FORMAT: return deepcopy(state)
        raise ValueError('Assessment compaction field is reserved')
    value = deepcopy(state)
    metadata = {'format': FORMAT, 'rules': RULES}
    gene_sets = _gene_sets(value)
    if gene_sets: metadata['gene_sets'] = gene_sets
    factors = _factors(value)
    if factors: metadata['factor_tables'] = factors
    provenance = _provenance(value)
    if provenance: metadata['provenance'] = provenance
    if len(metadata) == 2: return value
    value['compaction'] = metadata
    # Avoid enlarging tiny fixtures or unusual future state shapes.
    return value if len(_json(value).encode()) < len(_json(state).encode()) else deepcopy(state)


def reconstruct(state):
    """Restore the exact JSON values represented by ``compact``; no I/O."""
    value = deepcopy(state)
    metadata = value.pop('compaction', None)
    if metadata is None: return value
    if not isinstance(metadata, dict) or metadata.get('format') != FORMAT:
        raise ValueError('Unsupported assessment compaction')
    for identity, group in metadata.get('provenance', {}).get('tables', []):
        rows = value['collections'][identity]['provenance'][group]
        records = metadata['provenance']['records']
        value['collections'][identity]['provenance'][group] = [deepcopy(records[item]) if type(item) is int else item for item in rows]
    for group, rules in metadata.get('factor_tables', {}).items():
        for index in rules['factor_indexes']:
            table = value['factors'][index][group]
            table.update(deepcopy(rules['defaults']))
            if rules['decimal_text_columns']:
                rows = []
                for row in table['rows']:
                    record = dict(zip(table['columns'], row))
                    for text, numeric in rules['decimal_text_columns'].items(): record[text] = _json(record[numeric])
                    rows.append([record[key] for key in rules['original_columns']])
                table.update(columns=deepcopy(rules['original_columns']), rows=rows)
    rules = metadata.get('gene_sets')
    if rules:
        table = value['gene_sets']; rows = []
        for row in table['rows']:
            record = dict(zip(table['columns'], row)); index = record['collection_index']
            record['collection_id'] = rules['collection_ids'][index]
            record.update(deepcopy(rules['column_defaults'][index]))
            if 'name_suffix' in record: record['name'] = rules['name_prefixes'][index] + record['name_suffix']
            context = deepcopy(rules['context_defaults'][index])
            for key, source in rules['derived_context'].items():
                context[key] = ([record[source['array_column']]] if 'array_column' in source else record[source['column']])
            if rules['context_columns'] is None: context.update(record['context'])
            else: context.update({key: record['context.' + key] for key in rules['context_columns']})
            record['context'] = context
            rows.append([record[key] for key in rules['original_columns']])
        table.update(columns=deepcopy(rules['original_columns']), rows=rows)
    return value
