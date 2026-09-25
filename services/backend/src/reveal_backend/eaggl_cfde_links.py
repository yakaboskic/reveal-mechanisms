"""Application crosswalk from EAGGL trait/factor number to CFDE and GeneSets.

Matching intentionally ignores labels, top genes, and loading agreement. The
CFDE factor's metadata supplies initial ranked GeneSet links for immediate use.
"""
from __future__ import annotations

from collections import Counter
import gzip
import json
from pathlib import Path
import re

from .dismech_import import canonical, digest, require
from .mysql_database import insert_batch

FORMAT = 'eaggl-cfde-trait-factor-number-v1'
METHOD = 'exact_trait_factor_number'
MIGRATION = Path(__file__).resolve().parents[4] / 'schema/migrations/004_eaggl_cfde_links.sql'


def factor_number(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'Factor[1-9][0-9]*', value)), 'Expected a canonical FactorN identifier')
    number = int(value[6:])
    require(number < 2**32, 'Factor number exceeds MySQL unsigned integer range')
    return number


def read_catalog(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    raw = (directory / 'factors.jsonl.gz').read_bytes()
    require(manifest.get('complete') is True and not manifest.get('errors'), 'CFDE catalog is incomplete')
    require(not manifest.get('restricted_queries'), 'CFDE catalog contains restricted results')
    require(digest(raw) == manifest['sha256'], 'CFDE catalog checksum mismatch')
    rows = [json.loads(line) for line in gzip.decompress(raw).splitlines()]
    require(len(rows) == manifest['factor_records'], 'CFDE catalog row count mismatch')
    return manifest, rows


def match_factors(eaggl, cfde, model):
    """Exact, case-sensitive trait + FactorN lookup; no scientific agreement gate."""
    targets = {}
    for row in cfde:
        raw = row['raw']
        require(raw['gene_set_size'] == model, 'CFDE factor model mismatch')
        key = (row['phenotype_key'], factor_number(raw['factor']))
        require(key not in targets, f'Ambiguous CFDE trait/factor key: {key}')
        require(raw['phenotype'].casefold() == row['phenotype_key'].casefold(), 'CFDE phenotype escaped query key')
        require(all(isinstance(raw.get(k), str) and raw[k] and ':' not in raw[k]
                    for k in ('trait_group', 'factor')), 'CFDE interactive ID component missing/invalid')
        require(':' not in row['phenotype_key'] and ':' not in model, 'CFDE interactive ID contains ambiguous separators')
        targets[key] = row
    traits = {key[0] for key in targets}
    matches, unmatched, seen = [], [], set()
    for row in eaggl:
        metadata = row['metadata']
        number = factor_number(metadata['factor'])
        if metadata.get('factor_number') is not None:
            require(str(number) == str(metadata['factor_number']), 'EAGGL factor and factor_number disagree')
        require(row['factor_id'] == row['trait'] + '::' + metadata['factor'], 'EAGGL factor ID and metadata disagree')
        key = (row['trait'], number)
        require(key not in seen, f'Duplicate EAGGL trait/factor key: {key}')
        seen.add(key)
        target = targets.get(key)
        if target is None:
            unmatched.append({'factor_id': row['factor_id'], 'factor_index': row['factor_index'],
                              'reason': 'trait_not_in_cfde_catalog' if key[0] not in traits else 'factor_number_not_in_cfde_catalog'})
            continue
        raw = target['raw']
        node_id = f"factor:{raw['trait_group']}:{target['phenotype_key']}:{model}:{raw['factor']}"
        genesets = raw.get('top_gene_sets') or ''
        require(isinstance(genesets, str), 'Expected semicolon-delimited CFDE top_gene_sets')
        matches.append({'factor_index': row['factor_index'], 'eaggl_factor_id': row['factor_id'],
                        'cfde_node_id': node_id, 'cfde_trait': target['phenotype_key'],
                        'cfde_factor_number': number, 'cfde_trait_group': raw['trait_group'],
                        'gene_sets': [s.strip() for s in genesets.split(';') if s.strip()], 'payload': target})
    return matches, unmatched


def completed_import(cursor, table, field, requested, model=None):
    # table/field are fixed names supplied by prepare_mapping, never CLI SQL.
    clauses, params = ["status='complete'"], []
    if requested:
        clauses.append(f'{field}=%s'); params.append(requested)
    if model:
        clauses.append('model=%s'); params.append(model)
    cursor.execute(f"SELECT {field} FROM {table} WHERE {' AND '.join(clauses)}", params)
    rows = cursor.fetchall()
    require(len(rows) == 1, f'Select exactly one completed {table} import explicitly')
    return rows[0][0]


def resolve_aliases(cursor, keys, gene_set_import_id, model):
    hashes = {digest('gene_set:' + key): key for key in keys}
    result = {}
    items = sorted(hashes)
    for start in range(0, len(items), 500):
        batch = items[start:start + 500]
        cursor.execute('SELECT node_id_sha256,source_key,node_id,dapper_id,model FROM cfde_gene_set_aliases '
                       'WHERE import_id=%s AND node_id_sha256 IN (' + ','.join(['%s'] * len(batch)) + ')',
                       [gene_set_import_id, *batch])
        for hashed, source_key, node_id, dapper_id, alias_model in cursor.fetchall():
            require(source_key == hashes[hashed] and node_id == 'gene_set:' + source_key and alias_model == model,
                    'GeneSet alias identity/model mismatch')
            result[source_key] = {'node_id': node_id, 'node_id_sha256': hashed, 'dapper_id': dapper_id}
    return result


def prepare_mapping(connection, directory, *, eaggl_import_id=None, gene_set_import_id=None):
    catalog_manifest, cfde = read_catalog(directory)
    model = catalog_manifest['model']
    with connection.cursor() as cursor:
        eaggl_import_id = completed_import(cursor, 'eaggl_imports', 'import_id', eaggl_import_id)
        gene_set_import_id = completed_import(cursor, 'gene_set_imports', 'import_id', gene_set_import_id, model)
        cursor.execute('SELECT factor_index,factor_id,trait,metadata FROM eaggl_factors WHERE import_id=%s ORDER BY factor_index', (eaggl_import_id,))
        eaggl = [dict(factor_index=i, factor_id=identity, trait=trait, metadata=json.loads(metadata))
                 for i, identity, trait, metadata in cursor.fetchall()]
        require(eaggl, 'EAGGL import has no factors')
        matches, unmatched = match_factors(eaggl, cfde, model)
        aliases = resolve_aliases(cursor, {key for match in matches for key in match['gene_sets']}, gene_set_import_id, model)
    # Catalog bytes and both immutable imports pin the routing run. No label or
    # gene agreement enters the matching rule or excludes otherwise valid keys.
    identity = {'format': FORMAT, 'match_method': METHOD, 'eaggl_import_id': eaggl_import_id,
                'gene_set_import_id': gene_set_import_id, 'model': model, 'cfde_catalog_sha256': catalog_manifest['sha256']}
    summary_count = sum(len(m['gene_sets']) for m in matches)
    resolved_count = sum(key in aliases for m in matches for key in m['gene_sets'])
    counts = {'eaggl_factors': len(eaggl), 'cfde_catalog_factors': len(cfde), 'matched_factors': len(matches),
              'unmatched_factors': len(unmatched), 'gene_set_summary_links': summary_count,
              'resolved_gene_set_links': resolved_count, 'unresolved_gene_set_links': summary_count - resolved_count,
              'distinct_resolved_gene_sets': len(aliases)}
    manifest = {'run_id': digest(canonical(identity)), 'identity': identity, 'counts': counts,
                'unmatched_by_reason': dict(Counter(row['reason'] for row in unmatched)), 'unmatched': unmatched,
                'unresolved_gene_sets': sorted({key for m in matches for key in m['gene_sets'] if key not in aliases}),
                'cfde_catalog_manifest': catalog_manifest, 'gene_set_scope': 'CFDE factor metadata top_gene_sets only'}
    return {'manifest': manifest, 'matches': matches, 'aliases': aliases}


def projected_rows(plan):
    manifest = plan['manifest']; identity = manifest['identity']; run_id = manifest['run_id']
    factors, gene_sets = [], []
    for match in plan['matches']:
        factors.append((run_id, match['factor_index'], identity['eaggl_import_id'], match['cfde_node_id'],
                        digest(match['cfde_node_id']), match['cfde_trait'], match['cfde_factor_number'],
                        match['cfde_trait_group'], canonical(match['payload'])))
        for rank, key in enumerate(match['gene_sets'], 1):
            hashed = digest('gene_set:' + key)
            gene_sets.append((run_id, match['factor_index'], rank, identity['gene_set_import_id'], key,
                              'gene_set:' + key, hashed, hashed if key in plan['aliases'] else None))
    return [
        ('eaggl_cfde_factor_links', ('run_id', 'factor_index', 'eaggl_import_id', 'cfde_node_id', 'cfde_node_sha256',
                                   'cfde_trait', 'cfde_factor_number', 'cfde_trait_group', 'payload'), factors),
        ('eaggl_cfde_gene_set_links', ('run_id', 'factor_index', 'gene_set_rank', 'gene_set_import_id', 'source_key',
                                     'node_id', 'node_id_sha256', 'resolved_alias_sha256'), gene_sets),
    ]


def verify_plan(cursor, plan):
    for table, columns, rows in projected_rows(plan):
        order = 'factor_index,gene_set_rank' if table.endswith('gene_set_links') else 'factor_index'
        cursor.execute(f"SELECT {','.join(columns)} FROM {table} WHERE run_id=%s ORDER BY {order}", (plan['manifest']['run_id'],))
        actual = list(cursor.fetchall())
        expected = sorted(rows, key=lambda r: r[1:3] if table.endswith('gene_set_links') else r[1:2])
        if 'payload' in columns:
            actual = [(*row[:-1], json.loads(row[-1])) for row in actual]
            expected = [(*row[:-1], json.loads(row[-1])) for row in expected]
        require(actual == expected, f'{table}: database read-back differs from mapping')


def load_mapping(connection, plan, *, batch_size=500, migration=MIGRATION):
    require(batch_size > 0, 'Batch size must be positive')
    manifest = plan['manifest']; identity = manifest['identity']; run_id = manifest['run_id']
    locked = False
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT DATABASE()'); database = cursor.fetchone()[0]
            lock = 'cfde-link:' + digest(database + run_id)[:54]
            cursor.execute('SELECT GET_LOCK(%s,0)', (lock,))
            locked = cursor.fetchone()[0] == 1
            require(locked, 'Another loader holds this mapping lock')
            cursor.execute("SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')")
            sql = '\n'.join(line for line in Path(migration).read_text().splitlines() if not line.lstrip().startswith('--'))
            for statement in sql.split(';'):
                if statement.strip(): cursor.execute(statement)
            cursor.execute('SELECT manifest,status FROM eaggl_cfde_link_runs WHERE run_id=%s', (run_id,))
            old = cursor.fetchone()
            if old:
                require(json.loads(old[0]) == manifest and old[1] == 'complete', 'Existing mapping conflicts with prepared run')
                verify_plan(cursor, plan)
            else:
                cursor.execute('INSERT INTO eaggl_cfde_link_runs (run_id,eaggl_import_id,gene_set_import_id,model,match_method,status,manifest) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    (run_id, identity['eaggl_import_id'], identity['gene_set_import_id'], identity['model'], METHOD, 'loading', canonical(manifest)))
                for table, columns, rows in projected_rows(plan):
                    for start in range(0, len(rows), batch_size):
                        insert_batch(cursor, table, columns, rows[start:start + batch_size])
                verify_plan(cursor, plan)
                cursor.execute("UPDATE eaggl_cfde_link_runs SET status='complete' WHERE run_id=%s", (run_id,))
            # This mapping is small enough for one atomic transaction. Rerunning
            # after a failure either verifies the committed run or starts clean.
            connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        if locked:
            with connection.cursor() as cursor: cursor.execute('SELECT RELEASE_LOCK(%s)', (lock,))
    return {'run_id': run_id, 'status': 'complete', 'verified': True, 'counts': manifest['counts']}


def lookup_factor(connection, factor_id, *, run_id=None, eaggl_import_id=None):
    with connection.cursor() as cursor:
        clauses, params = ["r.status='complete'"], []
        if run_id: clauses.append('r.run_id=%s'); params.append(run_id)
        if eaggl_import_id: clauses.append('r.eaggl_import_id=%s'); params.append(eaggl_import_id)
        cursor.execute('SELECT r.run_id,r.eaggl_import_id,r.gene_set_import_id,r.model,r.manifest FROM eaggl_cfde_link_runs r '
                       'JOIN eaggl_imports e ON e.import_id=r.eaggl_import_id AND e.status=\'complete\' '
                       'JOIN gene_set_imports g ON g.import_id=r.gene_set_import_id AND g.status=\'complete\' '
                       "WHERE " + ' AND '.join(clauses), params)
        runs = cursor.fetchall()
        require(len(runs) == 1, 'Select one completed mapping with --run-id')
        run_id, source_import, gene_set_import, model, manifest_json = runs[0]
        cursor.execute('SELECT factor_index,label,factor_id FROM eaggl_factors WHERE import_id=%s AND factor_id_sha256=%s', (source_import, digest(factor_id)))
        source = cursor.fetchone()
        require(source is not None and source[2] == factor_id, 'EAGGL factor does not exist in this import')
        index, label, _ = source
        cursor.execute('SELECT cfde_node_id,cfde_trait,cfde_factor_number,cfde_trait_group,payload FROM eaggl_cfde_factor_links WHERE run_id=%s AND factor_index=%s', (run_id, index))
        link = cursor.fetchone()
        result = {'run_id': run_id, 'eaggl_import_id': source_import, 'eaggl_factor_id': factor_id,
                  'eaggl_label': label, 'model': model, 'match_method': METHOD}
        if link is None:
            missing = next((row for row in json.loads(manifest_json)['unmatched'] if row['factor_index'] == index), None)
            require(missing is not None, 'Mapping run lacks a match or unmatched record')
            return {**result, 'status': 'unmatched', 'reason': missing['reason'], 'gene_sets': []}
        cursor.execute('SELECT l.gene_set_rank,l.source_key,l.node_id,a.dapper_id FROM eaggl_cfde_gene_set_links l '
                       'LEFT JOIN cfde_gene_set_aliases a ON a.import_id=l.gene_set_import_id AND a.node_id_sha256=l.resolved_alias_sha256 '
                       'WHERE l.run_id=%s AND l.factor_index=%s ORDER BY l.gene_set_rank', (run_id, index))
        gene_sets = [{'rank': rank, 'source_key': key, 'node_id': node, 'dapper_id': dapper,
                     'status': 'resolved' if dapper else 'not_in_gene_set_catalog'} for rank, key, node, dapper in cursor.fetchall()]
        return {**result, 'status': 'matched', 'cfde_node_id': link[0], 'cfde_trait': link[1],
                'cfde_factor': 'Factor' + str(link[2]), 'cfde_trait_group': link[3],
                'cfde_metadata': json.loads(link[4])['raw'], 'gene_set_import_id': gene_set_import,
                'gene_set_scope': 'CFDE factor metadata top_gene_sets only', 'gene_sets': gene_sets}
