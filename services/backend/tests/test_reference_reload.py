"""Protected reference reload CLI: build determinism, protections, apply order and purge order.

No network or MySQL: shared tables are a scripted fake DB-API connection, application
records are SQLite repositories, and reference_archive / Upstash / RDS / mysqldump are fakes.
"""
import csv
import gzip
import hashlib
import io
import json
import re
import stat
from types import SimpleNamespace

import numpy as np
import pytest

from reveal_backend import redis_notifications
from reveal_backend import reference_generation as rg
from reveal_backend import reference_reload as rr
from reveal_backend.embedding_client import DEFAULT_SERVICE_URL
from reveal_backend.repository import Repository, digest
from reveal_backend.vector_ingestion import VectorRegistry

KPN_A, KPN_B = 'KPN.TRAIT:0000398', 'KPN.TRAIT:0000100'
SET = ['dapper:GeneSet.' + c * 31 + '1' for c in 'abc']
COLL = ['dapper:GeneSetCollection.' + c * 31 + '1' for c in 'xy']
LEGACY = rg.legacy_generation_id('a' * 64)
GEN = 'b' * 64
NEW_SNAPSHOT, OLD_SNAPSHOT = 'c' * 64, 'd' * 64
PREFIX, ENV_NAME, HOST = 'reveal_reload_rehearsal', 'rehearsal', 'vec.example.upstash.io'
ENV = {'REVEAL_MYSQL_DATABASE': 'cyaka_reveal_mechanisms', 'UPSTASH_VECTOR_REST_URL': f'https://{HOST}',
       'REVEAL_MYSQL_PASSWORD': 'secret-pw', 'REVEAL_MYSQL_HOST': 'aurora-x.cluster-abc.us-east-1.rds.amazonaws.com', 'PATH': '/usr/bin'}
TARGETS = f"""targets:
  rehearsal:
    database: cyaka_reveal_mechanisms
    prefix: {PREFIX}
    vector_environment: {ENV_NAME}
    upstash_host: {HOST}
    production: false
  prod:
    database: cyaka_reveal_mechanisms
    prefix: reveal
    vector_environment: prod
    upstash_host: {HOST}
    production: true
"""


@pytest.fixture(autouse=True)
def quiet_notifications(monkeypatch):
    monkeypatch.setattr(redis_notifications, 'publish', lambda channels: None)


# --------------------------------------------------------------------------------------
# LAP fixture


def tsv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.name.endswith('.gz') else open
    with opener(path, 'wt', encoding='utf-8', newline='') as out:
        out.write('\t'.join(columns) + '\n')
        for row in rows: out.write('\t'.join(str(row[c]) for c in columns) + '\n')


def lap_project(root, *, qc_pass='True'):
    project = root / 'lap'
    traits = [('T2D', KPN_A, 'Type 2 diabetes', 2), ('BMI', KPN_B, 'Body mass index', 1)]
    tsv(project / 'proj.trait_kpn_map.tsv', rr.LAP_FILES['trait_kpn_map'][1], [
        {'trait': t, 'kpn_trait_id': k, 'kpn_release': 'v0.0.2', 'kpn_release_commit': '3cd554f', 'gwas_source_category': 'KPN',
         'phenotype_name': name, 'trait_group': 'metabolic', 'legacy_trait_group': 'GLYCEMIC', 'trait_type': 'phenotype', 'n_factors': n}
        for t, k, name, n in traits])
    factors = [('T2D', KPN_A, 1, 'Insulin secretion '), ('T2D', KPN_A, 2, 'Beta cell'), ('BMI', KPN_B, 1, 'Adipogenesis')]
    tsv(project / 'proj.factor_index.tsv', rr.LAP_FILES['factor_index'][1], [
        {'global_eaggl_column': f'Factor{i}', 'factor_id': f'{t}::Factor{n}', 'trait': t, 'kpn_trait_id': k, 'factor': f'Factor{n}',
         'factor_number': n, 'factor_label': label, 'n_nonzero_loadings': 10, 'loading_l2': '1.5', 'loading_variant': 'capped'}
        for i, (t, k, n, label) in enumerate(factors, 1)])
    tsv(project / 'proj.factor_metadata.tsv', ('factor_id', 'trait', 'factor', 'factor_number', 'label', 'gene_set_score', 'top_genes'), [
        {'factor_id': f'{t}::Factor{n}', 'trait': t, 'factor': f'Factor{n}', 'factor_number': n, 'label': label, 'gene_set_score': '4.2',
         'top_genes': 'INS,GCK'} for t, k, n, label in factors])
    sets = [(SET[0], COLL[0], 'LIB__part__HZ1', 'LIB', 'up'), (SET[1], COLL[0], 'LIB__part__HZ1', 'LIB', 'dn'), (SET[2], COLL[1], 'OTHER__p__HZ1', 'OTHER', 'x')]
    tsv(project / 'proj.gene_set_index.tsv.gz', rr.LAP_FILES['gene_set_index'][1], [
        {'gene_set_id': g, 'gene_set_name': name, 'collection_id': c, 'cfde_label': label, 'library': lib, 'partition': 'part', 'model': 'HZ1',
         'comparison': '', 'program': '', 'gmt_row': i, 'n_genes': 20, 'n_genes_in_eaggl_universe': 18, 'cfde_snapshot': '2026-09-28'}
        for i, (g, c, label, lib, name) in enumerate(sets, 1)])
    top = []
    for t, k, n, label in factors:
        for rank, (g, c, clabel, lib, name) in enumerate(sets, 1):
            top.append({'trait': t, 'kpn_trait_id': k, 'factor_id': f'{t}::Factor{n}', 'factor': f'Factor{n}', 'factor_label': label,
                        'gene_set_id': g, 'gene_set_name': name, 'collection_id': c, 'cfde_label': clabel, 'library': lib,
                        'joint_loading': f'0.{9 - rank}1', 'marginal_loading': '1.2e-05', 'joint_rank_in_factor': rank,
                        'marginal_rank_in_factor': 4 - rank, 'is_joint_top_factor': int(rank == 1)})
    tsv(project / 'proj.top_gene_sets_per_factor.tsv.gz', list(top[0]), top)
    tsv(project / 'proj.projection_manifest.tsv', ('trait', 'kpn_trait_id', 'kpn_release', 'n_factors', 'pigean_commit', 'qc_pass'), [
        {'trait': t, 'kpn_trait_id': k, 'kpn_release': 'v0.0.2', 'n_factors': n, 'pigean_commit': 'ca59661',
         'qc_pass': qc_pass if t == 'BMI' else 'True'} for t, k, name, n in traits])
    tsv(project / 'proj.cfde_index.tsv', rr.LAP_FILES['cfde_index'][1] + ('yaml_path',), [
        {'library': lib, 'partition': 'part', 'model': 'HZ1', 'comparison': '', 'program': '', 'label': label, 'collection_id': c,
         'n_sets': n, 'n_genes': 30, 'yaml_path': '/ignored'} for c, label, lib, n in ((COLL[0], 'LIB__part__HZ1', 'LIB', 2), (COLL[1], 'OTHER__p__HZ1', 'OTHER', 1))])
    tsv(project / 'proj.kpn_trait_registry.tsv', rr.LAP_FILES['kpn_trait_registry'][1], [
        {'portal_id': k, 'gwas_source_category': 'KPN', 'legacy_phenotype_id': t, 'phenotype_name': name, 'legacy_trait_group': 'GLYCEMIC',
         'trait_group': 'metabolic', 'trait_type': 'phenotype', 'pigean_id': ''} for t, k, name, n in traits + [('Other', 'KPN.TRAIT:0000001', 'x', 0)]])
    tsv(project / 'proj.kpn_trait_flat.tsv', rr.LAP_FILES['kpn_trait_flat'][1] + ('mapping_justification', 'source'), [
        {'portal_id': KPN_A, 'description': 'T2D', 'is_dichotomous': '1', 'is_complex': 'true', 'target_id': 'EFO:0001360',
         'target_label': 'type 2 diabetes', 'target_ontology': 'EFO', 'mapping_predicate': 'skos:exactMatch', 'confidence': '0.9',
         'mapping_justification': 'curated', 'source': 's.tsv'},
        {'portal_id': KPN_B, 'description': '', 'is_dichotomous': '', 'is_complex': 'false', 'target_id': '', 'target_label': '',
         'target_ontology': '', 'mapping_predicate': '', 'confidence': '', 'mapping_justification': '', 'source': ''}])
    for c, label, members in ((COLL[0], 'LIB__part__HZ1', SET[:2]), (COLL[1], 'OTHER__p__HZ1', SET[2:])):
        path = project / 'collections' / label / f'{label}.GeneSetCollection.yaml'
        path.parent.mkdir(parents=True)
        path.write_text("# DAPPER geneset document\nprefixes:\n  HGNC.SYMBOL: 'https://identifiers.org/hgnc.symbol:'\norganizations: []\n"
                        f'gene_set_collections:\n- id: {c}\n  name: {label}\n  n_sets: {len(members)}\n  members:\n'
                        + ''.join(f'  - {m}\n' for m in members) + 'gene_sets:\n'
                        + ''.join(f'- id: {m}\n  name: set {m[-3:]}\n  member_type: gene\n  members:\n  - HGNC.SYMBOL:INS\n  - HGNC.SYMBOL:GCK\n'
                                  f'  was_generated_by: dapper:Activity.{"a" * 32}\n' for m in members) +
                        # Sections after gene_sets are never parsed (the real LINCS document is 167 MB).
                        'embeddings:\n- id: x\n  broken: [yaml {{{\n')
    return project


def embeddings(root, bundle_sets=SET, colls=COLL, dims=8):
    directory = root / 'emb'
    directory.mkdir()
    nodes = [('GeneSetCollection', c) for c in colls] + [('GeneSet', g) for g in bundle_sets] + [('GeneSet', 'dapper:GeneSet.' + 'z' * 32)]
    rng = np.random.default_rng(7)
    np.save(directory / 'vectors.float16.npy', rng.normal(size=(len(nodes), dims)).astype(np.float16))
    tsv(directory / 'rows.tsv', ('row', 'node_id', 'node_class', 'embedding_id', 'text_template', 'text'),
        [{'row': i, 'node_id': node, 'node_class': cls, 'embedding_id': f'dapper:Embedding.{i}', 'text_template': 't', 'text': f'text {i} {node}'}
         for i, (cls, node) in enumerate(nodes)])
    return directory


def stored_embedder(directory, noise=0.0):
    matrix = np.load(directory / 'vectors.float16.npy')
    with open(directory / 'rows.tsv', newline='') as stream:
        rows = {row['text']: int(row['row']) for row in csv.DictReader(stream, delimiter='\t')}
    calls = []
    def embed(texts, **options):
        calls.append(options)
        vectors = np.asarray([matrix[rows[text]] for text in texts], dtype=np.float32)
        return vectors + noise * np.random.default_rng(1).normal(size=vectors.shape)
    return embed, calls


# --------------------------------------------------------------------------------------
# build / embed


def test_build_is_deterministic_and_content_addressed(tmp_path):
    project = lap_project(tmp_path)
    first = rr.build_bundle(project, tmp_path / 'out1', kpn_release='v0.0.2')
    second = rr.build_bundle(project, tmp_path / 'out2')
    assert first['generation_id'] == second['generation_id'] and re.fullmatch('[a-f0-9]{64}', first['generation_id'])
    assert first['counts'] == {'kpn_traits': 2, 'reference_factors': 3, 'cfde_collections': 2, 'cfde_gene_sets': 3, 'projections': 9}
    a, b = tmp_path / 'out1' / first['generation_id'], tmp_path / 'out2' / first['generation_id']
    for name in rr.BUNDLE_FILES: assert (a / name).read_bytes() == (b / name).read_bytes(), name
    assert rr.build_bundle(project, tmp_path / 'out1')['reused'] is True
    manifest = rr.open_bundle(a)
    assert manifest['pigean_commit'] == 'ca59661' and manifest['kpn_release_commit'] == '3cd554f'
    factors = {row['factor_key']: row for row in rr.read_jsonl(a / 'reference_factors.jsonl.gz')}
    first_factor = factors[f'{KPN_A}::Factor1']
    assert first_factor['public_id'] == 'factor:kpn:0000398:eaggl-capped-v1:Factor1' and first_factor['eaggl_factor_id'] == 'T2D::Factor1'
    assert first_factor['input_text'] == 'Insulin secretion' and first_factor['label'] == 'Insulin secretion '
    assert first_factor['input_sha256'] == __import__('hashlib').sha256(b'Insulin secretion').hexdigest()
    assert first_factor['source_revision'] == digest(first_factor['metadata'])
    assert first_factor['metadata']['global_eaggl_column'] == 'Factor1' and first_factor['metadata']['kpn']['phenotype_name'] == 'Type 2 diabetes'
    traits = {row['kpn_trait_id']: row for row in rr.read_jsonl(a / 'kpn_traits.jsonl')}
    assert traits[KPN_A]['is_complex'] == 1 and traits[KPN_B]['is_dichotomous'] is None and traits[KPN_B]['metadata']['ontology_mappings'] == []
    gene_set = next(rr.read_jsonl(a / 'cfde_gene_sets.jsonl.gz'))
    assert gene_set['metadata']['dapper_gene_set'] == {'id': SET[0], 'name': 'set aa1', 'member_type': 'gene',
                                                       'members': ['HGNC.SYMBOL:INS', 'HGNC.SYMBOL:GCK'], 'was_generated_by': 'dapper:Activity.' + 'a' * 32}
    collection = next(rr.read_jsonl(a / 'cfde_collections.jsonl'))
    assert 'members' not in collection['payload']['collection'] and collection['n_sets'] == 2
    rows = list(rr.iter_tsv(a / 'projections.tsv.gz', rr.PROJECTION_COLUMNS))
    assert rows[0]['marginal_loading'] == '1.2e-05' and rows[0]['scope'] == 'per_trait'
    # Any input byte change is a new generation.
    path = project / 'proj.factor_metadata.tsv'
    path.write_text(path.read_text().replace('4.2', '4.3'))
    assert rr.build_bundle(project, tmp_path / 'out3')['generation_id'] != first['generation_id']


def test_build_refuses_unless_every_projection_passed_qc(tmp_path):
    with pytest.raises(rr.Refused, match='QC did not pass for 1 traits'):
        rr.build_bundle(lap_project(tmp_path, qc_pass='False'), tmp_path / 'out')
    assert not list((tmp_path / 'out').glob('*/manifest.json'))


def test_collection_gene_sets_stream_one_item_at_a_time(tmp_path):
    path = tmp_path / 'c.yaml'
    path.write_text('gene_set_collections:\n- id: x\ngene_sets:\n- id: a\n  members:\n  - G1\n  - G2\n# note\n\n- id: b\n  name: long\n'
                    '    folded name\nused_edges:\n- {broken\n')
    assert list(rr.collection_gene_sets(path)) == [{'id': 'a', 'members': ['G1', 'G2']}, {'id': 'b', 'name': 'long folded name'}]
    path.write_text('gene_set_collections: []\ngene_sets:\n- id: a\n  bad: [x\n')
    with pytest.raises(rr.Refused, match='invalid gene set YAML'): list(rr.collection_gene_sets(path))
    path.write_text('gene_set_collections: []\n')
    with pytest.raises(rr.Refused, match='no top-level gene_sets'): list(rr.collection_gene_sets(path))


def test_collection_header_never_parses_gene_sets(tmp_path):
    path = tmp_path / 'c.yaml'
    path.write_text('gene_set_collections:\n- id: x\n  members: [a, b]\n  n_sets: 2\ngene_sets:\n' + '- {broken\n' * 10)
    header, collection = rr.collection_header(path)
    assert collection == {'id': 'x', 'n_sets': 2}
    path.write_text('gene_sets:\n- id: a\ngene_set_collections:\n- id: x\n')
    with pytest.raises(rr.Refused): rr.collection_header(path)


def fake_services(tmp_path, **kwargs):
    targets = tmp_path / 'targets.yaml'
    if not targets.exists(): targets.write_text(TARGETS)
    services = rr.Services(environ=dict(ENV), shell={}, targets_file=targets)
    services.err = io.StringIO(); services.clock = lambda: '2026-09-30T12:00:00Z'; services.sleep = lambda seconds: None
    for key, value in kwargs.items(): setattr(services, key, value)
    return services


def test_embed_calibrates_upcasts_and_records_space(tmp_path):
    built = rr.build_bundle(lap_project(tmp_path), tmp_path / 'out')
    bundle, source = tmp_path / 'out' / built['generation_id'], embeddings(tmp_path)
    embed, calls = stored_embedder(source)
    result = rr.embed_bundle(fake_services(tmp_path, embed=embed), bundle, embeddings_dir=source, model='m/x', service_url='https://embed.invalid/')
    assert result['count'] == 5 and result['calibration']['probes'] == 5 and result['calibration']['minimum_cosine'] > 0.999
    assert calls[0]['model'] == 'm/x' and calls[0]['service_url'] == 'https://embed.invalid'
    space = rr.read_json(bundle / 'vectors/embedding_space.json')
    assert space['space_id'] == rr.space_digest(space) and space['dimensions'] == 8 and space['calibration']['skipped'] is False
    matrix = np.load(bundle / 'vectors/reference_vectors.f32.npy')
    assert matrix.dtype == np.dtype('<f4') and matrix.shape == (5, 8)
    rows = list(rr.read_jsonl(bundle / 'vectors/rows.jsonl.gz'))
    assert [row['source_kind'] for row in rows] == ['cfde_collection'] * 2 + ['cfde_gene_set'] * 3
    assert all(row['vector_sha256'] == __import__('hashlib').sha256(matrix[row['row']].tobytes()).hexdigest() for row in rows)
    assert rr.embed_bundle(fake_services(tmp_path), bundle, embeddings_dir=source)['reused'] is True


def test_embed_calibration_failure_refuses_and_skip_is_recorded(tmp_path):
    built = rr.build_bundle(lap_project(tmp_path), tmp_path / 'out')
    bundle, source = tmp_path / 'out' / built['generation_id'], embeddings(tmp_path)
    embed, _ = stored_embedder(source, noise=3.0)
    with pytest.raises(rr.Refused, match='Calibration failed'):
        rr.embed_bundle(fake_services(tmp_path, embed=embed), bundle, embeddings_dir=source, model='m')
    assert not (bundle / 'vectors').exists()
    def never(texts, **options): raise AssertionError('no probe when skipped')
    result = rr.embed_bundle(fake_services(tmp_path, embed=never), bundle, embeddings_dir=source, model='m', skip_calibration=True)
    space = rr.read_json(bundle / 'vectors/embedding_space.json')
    assert result['calibration']['skipped'] is True and space['calibration']['reason'] == 'operator --skip-calibration'


# --------------------------------------------------------------------------------------
# Fake shared-table connection


class FakeCursor:
    def __init__(self, db): self.db, self.rows, self.rowcount, self.description = db, [], 0, None
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, sql, params=()):
        sql = ' '.join(sql.split()); self.db.log.append((sql, tuple(params or ())))
        result = self.db.answer(sql, tuple(params or ()))
        if isinstance(result, int): self.rows, self.rowcount = [], result
        elif isinstance(result, dict): self.rows, self.rowcount, self.description = result.get('rows', []), result.get('rowcount', 0), result.get('description')
        else: self.rows = list(result or []); self.rowcount = len(self.rows)
    def executemany(self, sql, rows):
        rows = [tuple(row) for row in rows]; sql = ' '.join(sql.split())
        self.db.log.append((sql, rows)); self.db.answer('MANY ' + sql, rows); self.rowcount = len(rows)
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return list(self.rows)


class FakeDB:
    """Scripted DB-API connection: generations are stateful, everything else is a rule or []."""
    def __init__(self, generations=(), rules=()):
        self.log, self.commits, self.rules = [], 0, list(rules)
        self.generations = {g['generation_id']: dict(g) for g in generations}
    def cursor(self): return FakeCursor(self)
    def commit(self): self.commits += 1
    def rollback(self): self.log.append(('ROLLBACK', ()))
    def close(self): pass
    def statements(self, pattern): return [(sql, params) for sql, params in self.log if re.search(pattern, sql)]
    def answer(self, sql, params):
        for pattern, value in self.rules:
            if re.search(pattern, sql): return value(sql, params) if callable(value) else value
        if 'FROM reference_generations WHERE generation_id=%s' in sql:
            row = self.generations.get(params[0])
            return [tuple(json.dumps(row[c]) if c in ('manifest', 'cold_export_ref') and row[c] is not None else row[c] for c in rg.GENERATION_COLUMNS)] if row else []
        if 'FROM reference_generations ORDER BY' in sql:
            return [tuple(json.dumps(g[c]) if c in ('manifest', 'cold_export_ref') and g[c] is not None else g[c] for c in rg.GENERATION_COLUMNS)
                    for g in self.generations.values()]
        if sql.startswith('UPDATE reference_generations SET status=%s'):
            row = self.generations.get(params[1])
            if row and (len(params) == 2 or row['status'] in params[2:]): row['status'] = params[0]; return 1
            return 0
        if sql.startswith('SELECT GET_LOCK') or sql.startswith('SELECT RELEASE_LOCK'): return [(1,)]
        if 'information_schema.TABLES' in sql: return [(1,)]
        return []


def generation(identity, kind, status, **extra):
    row = {'generation_id': identity, 'kind': kind, 'model': rg.KPN_MODEL if kind == rg.KPN_KIND else rg.LEGACY_MODEL, 'status': status,
           'eaggl_import_id': 'e' * 64, 'eaggl_embedding_run_id': 'f' * 64, 'dismech_import_id': '1' * 64, 'legacy_mapping_run_id': None,
           'legacy_gene_set_import_id': None, 'manifest': {'counts': {}}, 'cold_export_ref': None}
    row.update(extra)
    return row


def built_bundle(tmp_path):
    built = rr.build_bundle(lap_project(tmp_path), tmp_path / 'out')
    bundle, source = tmp_path / 'out' / built['generation_id'], embeddings(tmp_path)
    rr.embed_bundle(fake_services(tmp_path), bundle, embeddings_dir=source, model='m', skip_calibration=True)
    return bundle, built['generation_id']


def run_row(**changes):
    """(config, dimensions) of the served EAGGL embedding run; None drops a key. Matches built_bundle's space."""
    config = {'import_id': 'e' * 64, 'model': 'm', 'model_revision': 'unspecified', 'provider': 'huggingface',
              'service_url': DEFAULT_SERVICE_URL.rstrip('/') + '/', 'template': 'factor-label-v1', **changes}
    return json.dumps({key: value for key, value in config.items() if value is not None}), 8


def mysql_json(text):
    """A JSON column as MySQL returns it: keys reordered (length first) and spaced."""
    value = json.loads(text)
    return json.dumps(dict(sorted(value.items(), key=lambda item: (len(item[0]), item[0]))), separators=(', ', ': '))


def sha(data): return hashlib.sha256(data).hexdigest()


def load_db(bundle, *, eaggl=True, run=None):
    factors = list(rr.read_jsonl(bundle / 'reference_factors.jsonl.gz'))
    inserted, stored = {}, {}
    def many(sql, rows):
        table = re.match(r'MANY INSERT INTO (\w+) \((.+?)\) VALUES', sql)
        if table:
            inserted[table[1]] = inserted.get(table[1], 0) + len(rows)
            stored.setdefault(table[1], []).extend(dict(zip(table[2].split(','), row)) for row in rows)
    def insert_generation(sql, params):
        db.generations[params[0]] = generation(params[0], params[1], 'loading', model=params[2], manifest=json.loads(params[6]))
        return 1
    def count(sql, params):
        table = re.search(r'FROM (\w+) WHERE generation_id', sql)[1]
        return [(inserted.get(table, 0),)]
    def bad_vectors(sql, params):
        return [(sum(len(r['vector']) != params[1] or sha(r['vector']) != r['vector_sha256'] or sha(r['input_text'].encode()) != r['input_sha256']
                     for r in stored.get('reference_vectors', [])),)]
    def per_factor(sql, params):
        counts = {}
        for r in stored.get('factor_gene_set_projections', []): counts[r['factor_key']] = counts.get(r['factor_key'], 0) + 1
        return sorted(counts.items())
    def sample(sql, params):
        key, columns, table = re.match(r'SELECT (\w+),(\S+) FROM (\w+) WHERE', sql).groups()
        return [(r[key], *(mysql_json(r[c]) if c == 'metadata' else r[c] for c in columns.split(','))) for r in stored.get(table, []) if r[key] in params[1:]]
    db = FakeDB(rules=[
        (r'^MANY ', many),
        (r'FROM eaggl_cfde_link_runs', [('a' * 64, 'e' * 64, '2' * 64)]),
        (r'FROM eaggl_embedding_runs WHERE import_id', [('f' * 64,)]),
        (r'FROM eaggl_embedding_runs WHERE run_id', lambda sql, params: [run or run_row()] if params[0] == 'f' * 64 else []),
        (r'FROM dismech_imports', [('1' * 64,)]),
        (r'FROM eaggl_imports WHERE', [('e' * 64, 'complete', rr.DEFAULT_EAGGL_SOURCE_VERSION)] if eaggl else []),
        (r'SELECT factor_id,input_sha256 FROM eaggl_factors', [(f['eaggl_factor_id'], f['input_sha256']) for f in factors]),
        (r'SELECT input_sha256 FROM eaggl_name_embeddings', [(f['input_sha256'],) for f in factors]),
        (r'^INSERT INTO reference_generations', insert_generation),
        (r'LEFT JOIN kpn_traits', [(0,)]),
        (r'LENGTH\(vector\)', bad_vectors),
        (r'^SELECT factor_key,COUNT\(\*\) FROM factor_gene_set_projections', per_factor),
        (r'^SELECT \w+,\S+ FROM (cfde_gene_sets|reference_factors) WHERE generation_id=%s AND \w+ IN', sample),
        (r'SELECT COUNT\(\*\) FROM \w+ WHERE generation_id=%s', count),
        (r'^SHOW WARNINGS', [])])
    db.stored, db.sample, db.per_factor = stored, sample, per_factor
    return db


def test_load_dry_run_writes_nothing_and_apply_inserts_generation(tmp_path):
    bundle, gen = built_bundle(tmp_path)
    db = load_db(bundle)
    services = fake_services(tmp_path, connect=lambda: db)
    dry = rr.load_bundle(services, bundle, apply=False)
    assert dry['eaggl']['eaggl_import_id'] == 'e' * 64 and dry['expected']['reference_vectors'] == 5 and dry['embedding_space_matches_run'] is True
    assert not [sql for sql, _ in db.log if re.match(r'(INSERT|UPDATE|DELETE|CREATE|SET SESSION)', sql)] and db.commits == 0
    result = rr.load_bundle(services, bundle, apply=True)
    assert result['status'] == 'complete' and result['counts'] == {'kpn_traits': 2, 'reference_factors': 3, 'cfde_collections': 2,
                                                                  'cfde_gene_sets': 3, 'projections': 9, 'reference_vectors': 5}
    assert result['legacy_generation_id'] == LEGACY and db.statements(r'^CREATE TABLE IF NOT EXISTS reference_generations')
    assert result['embedding_space_matches_run'] is True
    # MySQL JSON reorders keys: the sampled rows still read back equal as parsed values.
    assert result['readback'] == {'vectors_checked': True, 'projection_factors': 3, 'sampled': {'cfde_gene_sets': 3, 'reference_factors': 3}}
    readback = db.statements(r'LENGTH\(vector\)')[0]
    assert readback[1] == (gen, 32) and 'LOWER(SHA2(vector,256))<>vector_sha256' in readback[0] and 'LOWER(SHA2(input_text,256))<>input_sha256' in readback[0]
    # Strict mode right after the migration, before the lock and every insert.
    log = [sql for sql, _ in db.log]
    strict = log.index("SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')")
    assert max(i for i, sql in enumerate(log) if sql.startswith('CREATE TABLE')) < strict < log.index('SELECT GET_LOCK(%s,%s)')
    assert strict < min(i for i, sql in enumerate(log) if sql.startswith('INSERT'))
    order = [re.match(r'INSERT INTO (\w+)', sql)[1] for sql, _ in db.log if re.match(r'INSERT INTO \w+ \(', sql)]
    assert order == ['kpn_traits', 'reference_factors', 'cfde_gene_set_collections', 'cfde_gene_sets', 'factor_gene_set_projections', 'reference_vectors']
    assert db.generations[gen]['status'] == 'complete' and db.generations[gen]['model'] == rg.KPN_MODEL
    factor_rows = next(rows for sql, rows in db.log if sql.startswith('INSERT INTO reference_factors'))
    assert {row[7] for row in factor_rows} == {'e' * 64}  # eaggl_import_id from the verified import
    vector_rows = next(rows for sql, rows in db.log if sql.startswith('INSERT INTO reference_vectors'))
    assert all(len(row[6]) == 8 * 4 for row in vector_rows)


def test_load_requires_complete_eaggl_import_and_never_marks_complete_on_count_mismatch(tmp_path):
    bundle, gen = built_bundle(tmp_path)
    db = load_db(bundle, eaggl=False)
    with pytest.raises(rr.Refused, match='import_eaggl_factors.py') as refused:  # unpinned: the instructions, and the warning
        rr.load_bundle(fake_services(tmp_path, connect=lambda: db), bundle, apply=True)
    assert str(refused.value).endswith(rr.EAGGL_STOP)
    assert gen not in db.generations and not db.statements(r'^(CREATE|SET SESSION|INSERT)|GET_LOCK')  # refused before any write
    db = load_db(bundle)
    db.rules.insert(0, (r'SELECT COUNT\(\*\) FROM reference_vectors WHERE generation_id', [(4,)]))
    with pytest.raises(rr.Refused, match='differ from the bundle'):
        rr.load_bundle(fake_services(tmp_path, connect=lambda: db), bundle, apply=True)
    assert db.generations[gen]['status'] == 'failed'


def test_pinned_eaggl_refusals_say_stop_and_never_suggest_reimporting(tmp_path):
    bundle, gen = built_bundle(tmp_path)
    factors = list(rr.read_jsonl(bundle / 'reference_factors.jsonl.gz'))
    def refusal(db, environ=None, **pins):
        with pytest.raises(rr.Refused) as refused:
            rr.eaggl_source(fake_services(tmp_path, environ={**ENV, **(environ or {})}), db, factors, **pins)
        message = str(refused.value)
        assert message.endswith(rr.EAGGL_STOP)
        return message
    assert 'import_eaggl_factors.py' in refusal(load_db(bundle, eaggl=False))
    pinned = [refusal(load_db(bundle, eaggl=False), import_id='e' * 64),
              refusal(load_db(bundle, eaggl=False), environ={'REVEAL_EMBEDDING_RUN_ID': 'f' * 64})]
    db = load_db(bundle)
    db.rules.insert(0, (r'SELECT factor_id,input_sha256 FROM eaggl_factors', [(factors[0]['eaggl_factor_id'], 'x' * 64)]))
    pinned.append(refusal(db, run_id='f' * 64))
    assert pinned[-1].startswith("Stop: the pinned EAGGL import eeeeeeeeeeee does not carry this bundle's factors/labels (3 differ")
    pinned.append(refusal(load_db(bundle), run_id='9' * 64))  # the pinned run is not a complete run of the import
    db = load_db(bundle)
    db.rules.insert(0, (r'SELECT input_sha256 FROM eaggl_name_embeddings', []))
    pinned.append(refusal(db, environ={'REVEAL_EMBEDDING_RUN_ID': 'f' * 64}))
    assert all(message.startswith('Stop: ') and 'import_eaggl_factors.py' not in message for message in pinned), pinned
    assert 'lacks 3 factor label vectors' in pinned[-1]
    with pytest.raises(rr.Refused, match='Stop: .* not one'):
        rr.load_bundle(fake_services(tmp_path, connect=lambda: load_db(bundle)), bundle, apply=False, eaggl_embedding_run_id='9' * 64)


@pytest.mark.parametrize('apply', [False, True])
def test_load_refuses_before_any_write_unless_register_legacy_selects_the_same_source(tmp_path, apply):
    bundle, gen = built_bundle(tmp_path)
    # The served mapping run uses another EAGGL import than the one pinned by argument.
    db = load_db(bundle)
    db.rules.insert(0, (r'FROM eaggl_cfde_link_runs', [('a' * 64, '8' * 64, '2' * 64)]))
    with pytest.raises(rr.Refused, match=r'^Stop: the served mapping run aaaaaaaaaaaa uses EAGGL import 888888888888 and embedding run ffffffffffff '
                                         r'\(REVEAL_EMBEDDING_RUN_ID\), not the selected import eeeeeeeeeeee and run ffffffffffff'):
        rr.load_bundle(fake_services(tmp_path, connect=lambda: db), bundle, apply=apply, eaggl_import_id='e' * 64)
    assert gen not in db.generations and not db.statements(r'^(CREATE|INSERT|UPDATE|DELETE|SET SESSION)|GET_LOCK')
    # A stale REVEAL_EMBEDDING_RUN_ID while the argument names the served run.
    db = load_db(bundle)
    services = fake_services(tmp_path, connect=lambda: db, environ={**ENV, 'REVEAL_EMBEDDING_RUN_ID': '7' * 64})
    with pytest.raises(rr.Refused, match=r'pin REVEAL_EMBEDDING_RUN_ID'):
        rr.load_bundle(services, bundle, apply=apply, eaggl_embedding_run_id='f' * 64)
    assert gen not in db.generations and not db.statements(r'^(CREATE|INSERT|UPDATE|DELETE|SET SESSION)|GET_LOCK')


@pytest.mark.parametrize('apply', [False, True])
def test_load_refuses_a_bundle_embedded_outside_the_served_run_space(tmp_path, apply):
    bundle, gen = built_bundle(tmp_path)
    other = 'https://embedding-service.example.run.app'
    for run, differ in ((run_row(model='pritamdeka/BioBERT', service_url=other + '/'), "['model', 'service_url_sha256']"),
                        (run_row(model_revision='abc123', provider='openai'), "['model_revision', 'provider']"),
                        ((run_row()[0], 768), "['dimensions']")):
        db = load_db(bundle, run=run)
        with pytest.raises(rr.Refused, match=re.escape(f'differs from EAGGL embedding run ffffffffffff in {differ}')) as refused:
            rr.load_bundle(fake_services(tmp_path, connect=lambda: db), bundle, apply=apply)
        config = json.loads(run[0])
        # The run's own settings, and the URL itself (not a secret), to re-embed with.
        assert (f"--model {config['model']} --model-revision {config.get('model_revision', 'unspecified')} "
                f"--provider {config.get('provider', 'huggingface')} --service-url {config['service_url'].rstrip('/')}") in str(refused.value)
        assert f'Move {bundle}/vectors aside' in str(refused.value)
        assert gen not in db.generations and not db.statements(r'^(CREATE|INSERT|UPDATE|DELETE|SET SESSION)|GET_LOCK')
    # A config that omits the revision and provider means embed_capture's defaults; it matches.
    db = load_db(bundle, run=run_row(model_revision=None, provider=None))
    result = rr.load_bundle(fake_services(tmp_path, connect=lambda: db), bundle, apply=apply)
    assert result['embedding_space_matches_run'] is True and result.get('status', 'complete') == 'complete'


@pytest.mark.parametrize('tamper, message', [
    ('vectors', r'1 reference vectors differ from their length or checksums'),
    ('projections', r"projection counts differ for 1 factors, e\.g\. \['KPN\.TRAIT:0000398::Factor1'\]"),
    ('label', r'1 sampled reference_factors rows differ'),
    ('metadata', r'1 sampled cfde_gene_sets rows differ'),
    ('missing', r'1 sampled cfde_gene_sets rows differ')])
def test_load_readback_failure_marks_the_generation_failed(tmp_path, tamper, message):
    bundle, gen = built_bundle(tmp_path)
    db = load_db(bundle)
    if tamper == 'vectors':
        db.rules.insert(0, (r'LENGTH\(vector\)', [(1,)]))
    elif tamper == 'projections':
        db.rules.insert(0, (r'^SELECT factor_key,COUNT', lambda sql, p: [(key, n + (key == f'{KPN_A}::Factor1')) for key, n in db.per_factor(sql, p)]))
    elif tamper == 'label':
        db.rules.insert(0, (r'FROM reference_factors WHERE generation_id=%s AND factor_key IN',
                            lambda sql, p: [(row[0], row[1], row[2] + ('!' if i == 1 else ''), *row[3:]) for i, row in enumerate(db.sample(sql, p))]))
    elif tamper == 'metadata':
        db.rules.insert(0, (r'FROM cfde_gene_sets WHERE generation_id=%s AND gene_set_id IN',
                            lambda sql, p: [(*row[:2], json.dumps({**json.loads(row[2]), 'partition': 'other'}) if i == 0 else row[2])
                                            for i, row in enumerate(db.sample(sql, p))]))
    else:
        db.rules.insert(0, (r'FROM cfde_gene_sets WHERE generation_id=%s AND gene_set_id IN', lambda sql, p: db.sample(sql, p)[1:]))
    with pytest.raises(rr.Refused, match='Read-back: ' + message):
        rr.load_bundle(fake_services(tmp_path, connect=lambda: db), bundle, apply=True)
    assert db.generations[gen]['status'] == 'failed' and ('ROLLBACK', ()) in db.log
    assert all(params[0] != 'complete' for _, params in db.statements(r'^UPDATE reference_generations SET status=%s'))


# --------------------------------------------------------------------------------------
# preflight


SERVED_RUN, CONTEXT_RUN, DISMECH = 'f' * 64, '3' * 64, '1' * 64
GRANT_ROWS = ['GRANT USAGE ON *.* TO `cyaka`@`%`',
              'GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, REFERENCES, INDEX, ALTER ON `cyaka\\_reveal\\_mechanisms`.* TO `cyaka`@`%`']
SOURCE_COUNTS = {'eaggl_imports': 3, 'eaggl_embedding_runs': 2, 'eaggl_cfde_link_runs': 1, 'dismech_imports': 1, 'dismech_embedding_runs': 1,
                 'gene_set_imports': 1}


def preflight_db(**changes):
    """Scripted shared database in the served state; `db.state` drives every answer."""
    state = {'cipher': 'TLS_AES_256_GCM_SHA384', 'innodb_read_only': 0, 'read_only': 0, 'grants': list(GRANT_ROWS), 'roles': 'NONE', 'role_grants': None,
             'lock_free': 1,
             'tables': {**SOURCE_COUNTS, 'eaggl_factors': 9000, 'eaggl_gene_loadings': 2500000, 'dismech_discussions': 40, f'{PREFIX}_records': 10,
                        f'{PREFIX}_transaction_lock': 1, 'reveal_records': 900, 'reveal_transaction_lock': 1},
             'counts': dict(SOURCE_COUNTS), 'mapping': [('a' * 64, 'e' * 64, '2' * 64)],
             'imports': [('e' * 64, 'complete', rr.DEFAULT_EAGGL_SOURCE_VERSION)], 'runs': [(SERVED_RUN,)], 'run': run_row(),
             'dismech': [(DISMECH,)], 'context': [(CONTEXT_RUN, DISMECH, 'complete')]}
    state.update(changes)
    table = lambda sql: re.search(r'FROM (\w+)', sql)[1]
    def role_grants(sql, params):  # SHOW GRANTS ... USING <active roles>; None: the server cannot expand them
        if state['role_grants'] is None: raise RuntimeError('ERROR 3530: role is not granted')
        return [(grant,) for grant in state['role_grants']]
    db = FakeDB(rules=[
        (r"^SHOW SESSION STATUS LIKE 'Ssl_cipher'", lambda sql, p: [('Ssl_cipher', state['cipher'])]),
        (r'^SELECT VERSION\(\),@@GLOBAL.innodb_read_only,@@GLOBAL.read_only,@@GLOBAL.sql_mode,@@SESSION.sql_mode,@@SESSION.max_allowed_packet$',
         lambda sql, p: [('8.0.32', state['innodb_read_only'], state['read_only'], 'STRICT_TRANS_TABLES', 'STRICT_TRANS_TABLES,NO_ZERO_DATE', 67108864)]),
        (r"^SHOW GLOBAL VARIABLES LIKE 'aurora_version'$", [('aurora_version', '3.05.2')]),
        (r'^SELECT 1$', [(1,)]),
        (r'^SELECT CURRENT_ROLE\(\)$', lambda sql, p: [(state['roles'],)]),
        (r'^SHOW GRANTS FOR CURRENT_USER$', lambda sql, p: [(grant,) for grant in state['grants']]),
        (r'^SHOW GRANTS FOR CURRENT_USER USING ', role_grants),
        (r'^SELECT IS_FREE_LOCK\(%s\)$', lambda sql, p: [(state['lock_free'],)] if p == (rr.LOCK_NAME,) else []),
        (r'^SELECT TABLE_NAME,TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE\(\)', lambda sql, p: sorted(state['tables'].items())),
        (r'^SELECT COUNT\(\*\) FROM (\w+)$', lambda sql, p: [(state['counts'].get(table(sql), 0),)]),
        (r'^SELECT COUNT\(\*\) FROM eaggl_factors WHERE import_id=%s$', [(4037,)]),
        (r'^SELECT COUNT\(\*\) FROM eaggl_gene_loadings WHERE import_id=%s$', [(2553330,)]),
        (r'FROM eaggl_cfde_link_runs', lambda sql, p: state['mapping']),
        (r'FROM eaggl_imports WHERE', lambda sql, p: state['imports']),
        (r'FROM eaggl_embedding_runs WHERE import_id', lambda sql, p: state['runs']),
        (r'FROM eaggl_embedding_runs WHERE run_id', lambda sql, p: [state['run']] if (p[0],) in state['runs'] else []),
        (r'FROM dismech_imports', lambda sql, p: state['dismech']),
        (r'FROM dismech_embedding_runs WHERE eaggl_embedding_run_id=%s', lambda sql, p: state['context'] if p == (SERVED_RUN,) else [])])
    db.state = state
    return db


def test_preflight_is_read_only_and_reports_the_served_sources(tmp_path, capsys):
    db = preflight_db()
    services = fake_services(tmp_path, connect=lambda: db)
    result = rr.preflight(services, out=tmp_path / 'preflight.json')
    log = [sql for sql, _ in db.log]
    assert log[:2] == ['SET SESSION TRANSACTION READ ONLY', 'START TRANSACTION READ ONLY'] and log[-1] == 'ROLLBACK'
    assert all(re.match(r'(SELECT|SHOW) ', sql) for sql in log[2:-1]) and db.commits == 0
    assert not db.statements(r'\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|GET_LOCK|RELEASE_LOCK)\b')
    assert result['ok'] is True and result['blockers'] == [] and result['warnings'] == []
    server = result['server']
    assert {key: server[key] for key in ('version', 'aurora_version', 'innodb_read_only', 'read_only', 'max_allowed_packet', 'tls_cipher_present')} == {
        'version': '8.0.32', 'aurora_version': '3.05.2', 'innodb_read_only': False, 'read_only': False, 'max_allowed_packet': 67108864, 'tls_cipher_present': True}
    assert server['sql_mode_session'] == 'STRICT_TRANS_TABLES,NO_ZERO_DATE' and server['round_trip_ms'] >= 0 and len(db.statements(r'^SELECT 1$')) == 10
    assert result['grants'] == dict.fromkeys(rr.GRANTS, True) and db.statements(r'^SELECT CURRENT_ROLE\(\)$') and not db.statements('USING')
    assert result['lock_free'] is True and result['reference_tables'] == dict.fromkeys(rr.REFERENCE_TABLES, False)
    assert result['prefixes'] == [{'target': 'prod', 'prefix': 'reveal', 'records_table': True, 'lock_table': True},
                                  {'target': 'rehearsal', 'prefix': PREFIX, 'records_table': True, 'lock_table': True}]
    assert result['inventory'] == {'approx_rows': db.state['tables'], 'exact': SOURCE_COUNTS}
    sources = result['sources']
    assert sources['mapping_runs'] == {'complete': ['a' * 64], 'selected': 'a' * 64, 'eaggl_import_id': 'e' * 64, 'gene_set_import_id': '2' * 64,
                                       'embedding_runs': [SERVED_RUN]}
    assert sources['eaggl'] == {'imports': ['e' * 64], 'import_id': 'e' * 64, 'source_version': rr.DEFAULT_EAGGL_SOURCE_VERSION, 'status': 'complete',
                                'factors': 4037, 'loadings': 2553330, 'embedding_runs': [SERVED_RUN], 'selected_run': SERVED_RUN,
                                'run_config': {'model': 'm', 'model_revision': 'unspecified', 'provider': 'huggingface',
                                               'service_url': DEFAULT_SERVICE_URL.rstrip('/'), 'dimensions': 8}}
    assert sources['dismech'] == {'complete': [DISMECH], 'selected': DISMECH, 'selected_context_run': CONTEXT_RUN,
                                  'context_runs': [{'run_id': CONTEXT_RUN, 'dismech_import_id': DISMECH, 'status': 'complete'}]}
    assert result['embedding_service'] == 'not checked' and 'bundle' not in result and 'compare' not in result
    text = json.dumps(result)  # no secrets, user or host names
    assert not any(secret in text for secret in ('`cyaka`@', 'cyaka@', 'secret-pw', ENV['REVEAL_MYSQL_HOST'], HOST))
    assert rr.read_json(tmp_path / 'preflight.json') == result
    assert rr.main(['preflight'], services=services) == 0
    assert json.loads(capsys.readouterr().out)['command'] == 'preflight'


BROKEN = {
    'no TLS': ({'cipher': ''}, 'The connection has no TLS cipher'),
    'reader': ({'innodb_read_only': 1}, 'The server is read-only'),
    'read_only': ({'read_only': 1}, 'The server is read-only'),
    'grants': ({'grants': ['GRANT SELECT, INSERT, UPDATE ON `cyaka_reveal_mechanisms`.* TO `cyaka`@`%`']},
               "Missing grants on cyaka_reveal_mechanisms: ['delete', 'create', 'drop', 'references']"),
    # load --apply marks the generation complete (or failed) with UPDATE; migration 008's foreign keys need REFERENCES.
    'no update': ({'grants': ['GRANT SELECT, INSERT, DELETE, CREATE, DROP, REFERENCES ON `cyaka_reveal_mechanisms`.* TO `cyaka`@`%`']},
                  "Missing grants on cyaka_reveal_mechanisms: ['update']"),
    'no references': ({'grants': ['GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON `cyaka_reveal_mechanisms`.* TO `cyaka`@`%`']},
                      "Missing grants on cyaka_reveal_mechanisms: ['references']"),
    'table grant': ({'grants': ['GRANT ALL PRIVILEGES ON `cyaka_reveal_mechanisms`.`reveal_records` TO `cyaka`@`%`']}, 'Missing grants'),
    'other database': ({'grants': ['GRANT ALL PRIVILEGES ON `cyaka_other`.* TO `cyaka`@`%`']}, 'Missing grants'),
    'lock': ({'lock_free': 0}, 'Another reference reload holds the global lock'),
    'mapping runs': ({'mapping': [('a' * 64, 'e' * 64, '2' * 64), ('9' * 64, 'e' * 64, '2' * 64)]}, 'Not exactly one selected complete EAGGL mapping run'),
    'no mapping run': ({'mapping': []}, 'Not exactly one selected complete EAGGL mapping run'),
    'no EAGGL import': ({'imports': []}, 'EAGGL import not found or not complete'),
    'mapping import': ({'imports': [('8' * 64, 'complete', rr.DEFAULT_EAGGL_SOURCE_VERSION)]},
                       'The served mapping run uses EAGGL import eeeeeeeeeeee, not the selected 888888888888'),
    'two EAGGL imports': ({'imports': [('e' * 64, 'complete', 'v'), ('8' * 64, 'complete', 'v')]}, 'EAGGL import not found or not complete'),
    'embedding runs': ({'runs': [(SERVED_RUN,), ('9' * 64,)]}, 'Not exactly one selected complete EAGGL embedding run'),
    'DisMech imports': ({'dismech': [(DISMECH,), ('4' * 64,)]}, 'Not exactly one selected complete DisMech import'),
    'no context run': ({'context': [(CONTEXT_RUN, DISMECH, 'failed')]}, 'Not exactly one complete DisMech context run'),
    'context runs': ({'context': [(CONTEXT_RUN, DISMECH, 'complete'), ('7' * 64, DISMECH, 'complete')]}, 'Not exactly one complete DisMech context run'),
    'foreign context run': ({'context': [(CONTEXT_RUN, '4' * 64, 'complete')]}, 'Not exactly one complete DisMech context run')}


@pytest.mark.parametrize('case', sorted(BROKEN))
def test_preflight_reports_a_blocker_for_each_broken_invariant(tmp_path, case, capsys):
    changes, blocker = BROKEN[case]
    db = preflight_db(**changes)
    services = fake_services(tmp_path, connect=lambda: db)
    result = rr.preflight(services)
    assert result['ok'] is False and any(item.startswith(blocker) for item in result['blockers']), result['blockers']
    assert rr.main(['preflight'], services=services) == 1 and json.loads(capsys.readouterr().out)['ok'] is False
    assert not db.statements(r'\b(INSERT|UPDATE|DELETE|CREATE|DROP|GET_LOCK)\b')


def test_preflight_pins_grants_and_missing_prefix_tables(tmp_path):
    # An explicit pin selects among several runs; ALL PRIVILEGES on *.* or a matching wildcard grants everything.
    db = preflight_db(runs=[(SERVED_RUN,), ('9' * 64,)], grants=['GRANT ALL PRIVILEGES ON *.* TO `admin`@`%`'])
    result = rr.preflight(fake_services(tmp_path, connect=lambda: db), eaggl_embedding_run_id=SERVED_RUN)
    assert result['sources']['eaggl']['selected_run'] == SERVED_RUN and all(result['grants'].values())
    assert any('deployed readiness needs exactly one' in item for item in result['blockers'])  # still two complete runs
    assert rr.parse_grants([('GRANT ALL ON `cyaka\\_%`.* TO `cyaka`@`%`',)], 'cyaka_reveal_mechanisms')['drop'] is True
    assert rr.parse_grants([('GRANT DROP ON `cyakaXreveal\\_mechanisms`.* TO `c`@`%`',)], 'cyaka_reveal_mechanisms')['drop'] is False
    assert rr.parse_grants([('GRANT DROP ON `cyaka_reveal_mechanisms`.* TO `c`@`%`',)], 'cyakaXreveal_mechanisms')['drop'] is True  # _ is a wildcard
    # The mapping pin and the DisMech pin are honoured like register_legacy and select_dismech_import.
    db = preflight_db(mapping=[('a' * 64, 'e' * 64, '2' * 64), ('9' * 64, '8' * 64, '2' * 64)], dismech=[(DISMECH,), ('4' * 64,)])
    services = fake_services(tmp_path, connect=lambda: db, environ={**ENV, 'REVEAL_MAPPING_RUN_ID': 'a' * 64, 'REVEAL_DISMECH_IMPORT_ID': DISMECH})
    result = rr.preflight(services)
    assert result['sources']['mapping_runs']['selected'] == 'a' * 64 and result['sources']['dismech']['selected'] == DISMECH
    assert [item for item in result['blockers']] == [
        f"Not exactly one selected complete DisMech import (deployed readiness needs exactly one); complete: {[DISMECH, '4' * 64]}"]
    # A missing records or lock table of an allow-listed target is a warning, not a blocker.
    db = preflight_db()
    del db.state['tables']['reveal_transaction_lock']
    result = rr.preflight(fake_services(tmp_path, connect=lambda: db))
    assert result['ok'] is True and result['warnings'] == ['prod: reveal_transaction_lock does not exist']
    assert result['prefixes'][0] == {'target': 'prod', 'prefix': 'reveal', 'records_table': True, 'lock_table': False}


def test_preflight_counts_the_privileges_of_active_roles(tmp_path):
    roles = ['GRANT USAGE ON *.* TO `cyaka`@`%`', 'GRANT `reveal_rw`@`%`,`audit`@`%` TO `cyaka`@`%`']
    # Without USING, MySQL 8 lists the roles granted, not what they allow: on their own they hold nothing.
    assert not any(rr.parse_grants([(row,) for row in roles], 'cyaka_reveal_mechanisms').values())
    db = preflight_db(grants=roles, roles='`reveal_rw`@`%`,`audit`@`%`', role_grants=roles + GRANT_ROWS[1:])
    result = rr.preflight(fake_services(tmp_path, connect=lambda: db))
    assert result['ok'] is True and result['grants'] == dict.fromkeys(rr.GRANTS, True) and result['warnings'] == []
    assert [sql for sql, _ in db.statements('^SHOW GRANTS')] == ['SHOW GRANTS FOR CURRENT_USER USING `reveal_rw`@`%`,`audit`@`%`']
    assert 'reveal_rw' not in json.dumps(result)  # no account or role names
    # Roles that are granted but not active in a new session give load nothing either.
    db = preflight_db(grants=roles, role_grants=roles + GRANT_ROWS[1:])
    result = rr.preflight(fake_services(tmp_path, connect=lambda: db))
    assert result['blockers'] == [f'Missing grants on cyaka_reveal_mechanisms: {list(rr.REQUIRED_GRANTS)}'] and not db.statements('USING')
    # Active roles the server will not expand: only the direct grants count (fails closed), with a warning.
    db = preflight_db(grants=roles, roles='`reveal_rw`@`%`', role_grants=None)
    result = rr.preflight(fake_services(tmp_path, connect=lambda: db))
    assert result['warnings'] == ['The privileges of 1 active roles could not be read (RuntimeError); only direct grants count']
    assert result['blockers'] == [f'Missing grants on cyaka_reveal_mechanisms: {list(rr.REQUIRED_GRANTS)}']


def test_preflight_blocks_unless_the_arguments_select_what_register_legacy_will(tmp_path):
    # register_legacy takes the mapping run's EAGGL import and only REVEAL_EMBEDDING_RUN_ID, never the arguments.
    def blockers(db, environ=None, **pins):
        return rr.preflight(fake_services(tmp_path, connect=lambda: db, environ={**ENV, **(environ or {})}), **pins)['blockers']
    db = preflight_db(imports=[('8' * 64, 'complete', 'v')])
    assert blockers(db, eaggl_import_id='8' * 64) == ['The served mapping run uses EAGGL import eeeeeeeeeeee, not the selected 888888888888']
    # A stale REVEAL_EMBEDDING_RUN_ID beside the right --eaggl-embedding-run-id.
    stale = blockers(preflight_db(), {'REVEAL_EMBEDDING_RUN_ID': '7' * 64}, eaggl_embedding_run_id=SERVED_RUN)
    assert stale == ["Not exactly one complete EAGGL embedding run of the served mapping run's import eeeeeeeeeeee selected by "
                     'REVEAL_EMBEDDING_RUN_ID (load registers the legacy generation with it): []']
    # Two complete runs, the argument and the environment pinning different ones.
    split = blockers(preflight_db(runs=[(SERVED_RUN,), ('9' * 64,)]), {'REVEAL_EMBEDDING_RUN_ID': '9' * 64}, eaggl_embedding_run_id=SERVED_RUN)
    assert split[-1] == "The selected EAGGL embedding run ffffffffffff (--eaggl-embedding-run-id) is not REVEAL_EMBEDDING_RUN_ID's 999999999999: pin both to the served run"
    assert blockers(preflight_db(), {'REVEAL_EMBEDDING_RUN_ID': SERVED_RUN}, eaggl_embedding_run_id=SERVED_RUN, eaggl_import_id='e' * 64) == []


def test_preflight_selects_the_context_run_as_vector_ingestion_does(tmp_path):
    def dismech(context, environ=None):
        result = rr.preflight(fake_services(tmp_path, connect=lambda: preflight_db(context=context), environ={**ENV, **(environ or {})}))
        return result['sources']['dismech']['selected_context_run'], result['blockers']
    # A complete run of another DisMech import is not a candidate.
    assert dismech([(CONTEXT_RUN, DISMECH, 'complete'), ('4' * 64, '4' * 64, 'complete')]) == (CONTEXT_RUN, [])
    # Several complete runs of the selected import: REVEAL_DISMECH_EMBEDDING_RUN_ID chooses one, as ingestion does.
    several = [(CONTEXT_RUN, DISMECH, 'complete'), ('7' * 64, DISMECH, 'complete')]
    run, blocked = dismech(several)
    assert run is None and blocked[0].startswith('Not exactly one complete DisMech context run') and 'REVEAL_DISMECH_EMBEDDING_RUN_ID' in blocked[0]
    assert dismech(several, {'REVEAL_DISMECH_EMBEDDING_RUN_ID': '7' * 64}) == ('7' * 64, [])
    run, blocked = dismech(several, {'REVEAL_DISMECH_EMBEDDING_RUN_ID': '6' * 64})
    assert run is None and len(blocked) == 1


def test_preflight_compare_flags_schema_and_source_changes_but_not_row_estimates(tmp_path):
    db = preflight_db()
    services = fake_services(tmp_path, connect=lambda: db)
    baseline = tmp_path / 'baseline.json'
    rr.preflight(services, out=baseline)
    def compare(**changes):
        db.state.update(changes)
        return rr.preflight(services, compare=baseline)
    # Live traffic moves estimates; migration 008 adds only its own tables, whose rows are not sources.
    tables = dict(db.state['tables'], reveal_records=1400, reference_generations=2, embedding_spaces=1)
    result = compare(tables=tables, counts={**SOURCE_COUNTS, 'reference_generations': 2, 'embedding_spaces': 1})
    assert result['ok'] is True, result['blockers']
    assert result['compare'] == {'baseline_checked_at': '2026-09-30T12:00:00Z', 'new_tables': ['embedding_spaces', 'reference_generations'],
                                 'removed_tables': [], 'source_count_changes': {}, 'source_changes': {},
                                 'approx_row_changes': {'reveal_records': [900, 1400]}}
    assert result['inventory']['exact'] == {**SOURCE_COUNTS, 'reference_generations': 2, 'embedding_spaces': 1}
    assert result['reference_tables']['reference_generations'] is True
    result = compare(tables={**tables, 'eaggl_extra': 0})
    assert result['blockers'] == ['New table eaggl_extra is not a migration-008 table']
    result = compare(tables={key: value for key, value in tables.items() if key != 'dismech_discussions'})
    assert result['blockers'] == ['Table dismech_discussions was removed'] and result['compare']['removed_tables'] == ['dismech_discussions']
    result = compare(tables=tables, counts={**SOURCE_COUNTS, 'eaggl_embedding_runs': 3})
    assert result['blockers'] == ['eaggl_embedding_runs changed from 2 to 3 rows'] and result['compare']['source_count_changes'] == {'eaggl_embedding_runs': [2, 3]}
    result = compare(counts=dict(SOURCE_COUNTS), context=[(CONTEXT_RUN, DISMECH, 'complete'), ('7' * 64, DISMECH, 'failed')])
    assert result['blockers'] == ['Served source dismech changed'] and set(result['compare']['source_changes']) == {'dismech'}
    assert result['compare']['source_changes']['dismech']['after']['context_runs'][1]['run_id'] == '7' * 64
    rr.write_json(tmp_path / 'other.json', {'format': 'x'})
    with pytest.raises(rr.Refused, match='not a preflight result'): rr.preflight(services, compare=tmp_path / 'other.json')


def test_preflight_probes_the_embedding_service_with_the_run_settings(tmp_path):
    db, calls = preflight_db(), []
    def embed(texts, **options): calls.append((texts, options)); return np.zeros((1, 8), dtype=np.float32)
    services = fake_services(tmp_path, connect=lambda: db, embed=embed)
    result = rr.preflight(services, check_embedding_service=True)
    assert result['ok'] is True and result['embedding_service'] == {'reachable': True, 'dimensions': 8}
    assert calls == [(['reveal reload preflight probe'], {'model': 'm', 'service_url': DEFAULT_SERVICE_URL.rstrip('/'), 'provider': 'huggingface',
                                                          'batch_size': 1, 'max_workers': 1, 'max_retries': 1, 'timeout': 60})]
    assert db.log[-1] == ('ROLLBACK', ())  # the probe runs after the read-only transaction ends
    services.embed = lambda texts, **options: np.zeros((1, 4))
    assert rr.preflight(services, check_embedding_service=True)['blockers'] == ['The embedding service returns 4 dimensions; the EAGGL run has 8']
    def down(texts, **options): raise ConnectionError('service unavailable')
    services.embed = down
    result = rr.preflight(services, check_embedding_service=True)
    assert result['embedding_service'] == {'reachable': False, 'error': 'ConnectionError: service unavailable'}
    assert result['blockers'] == [f'Embedding service {DEFAULT_SERVICE_URL.rstrip("/")} failed (ConnectionError)']


def test_preflight_checks_a_bundle_against_the_eaggl_source(tmp_path):
    bundle, gen = built_bundle(tmp_path)
    factors = list(rr.read_jsonl(bundle / 'reference_factors.jsonl.gz'))
    db = preflight_db()
    db.rules[:0] = [(r'SELECT factor_id,input_sha256 FROM eaggl_factors', [(f['eaggl_factor_id'], f['input_sha256']) for f in factors]),
                    (r'SELECT input_sha256 FROM eaggl_name_embeddings', [(f['input_sha256'],) for f in factors])]
    services = fake_services(tmp_path, connect=lambda: db)
    result = rr.preflight(services, bundle=bundle)
    assert result['ok'] is True and result['bundle'] == {
        'generation_id': gen, 'vectors': True, 'ok': True, 'embedding_space_matches_run': True,
        'eaggl': {'eaggl_import_id': 'e' * 64, 'eaggl_embedding_run_id': SERVED_RUN, 'source_version': rr.DEFAULT_EAGGL_SOURCE_VERSION}}
    assert not db.statements(r'\b(INSERT|UPDATE|DELETE|CREATE|DROP|GET_LOCK)\b')
    db.state['run'] = run_row(model='pritamdeka/BioBERT')
    result = rr.preflight(services, bundle=bundle)
    assert result['bundle']['ok'] is False and "in ['model']" in result['bundle']['refused']
    assert len(result['blockers']) == 1 and result['blockers'][0].startswith("Bundle: The bundle's embedding space differs")
    db.state['run'] = run_row()
    db.rules.insert(0, (r'SELECT input_sha256 FROM eaggl_name_embeddings', []))
    result = rr.preflight(services, bundle=bundle, eaggl_embedding_run_id=SERVED_RUN)
    assert result['blockers'][0].startswith('Bundle: Stop: the pinned EAGGL embedding run') and 'import_eaggl_factors.py' not in result['blockers'][0]


# --------------------------------------------------------------------------------------
# abandon


SPACE = '5' * 64
GENERATION_ROWS = dict(zip(rr.GENERATION_TABLES, (5, 9, 3, 2, 3, 2)))


def abandon_services(tmp_path, *, status='failed', bindings=0, archived=0, shared=0, other_rows=None):
    """Cutover fixture whose GEN is an abandonable KPN generation with rows in every generation table."""
    tmp_path.mkdir(exist_ok=True)
    services, db, archive, repo = cutover_services(tmp_path)
    db.generations[GEN].update(status=status, manifest={'counts': {}, 'vectors': {'space_id': SPACE}})
    rows = {**GENERATION_ROWS, 'embedding_spaces': 1, **(other_rows or {})}
    def delete(sql, params):
        table = re.match(r'DELETE FROM (\w+)', sql)[1]
        if table == 'reference_generations': return int(db.generations.pop(params[0], None) is not None)
        count, rows[table] = rows.get(table, 0), 0
        return count
    count = lambda sql, p: [(rows.get(re.search(r'FROM (\w+)', sql)[1], 0),)]
    db.rules[:0] = [
        (r'^DELETE FROM', delete),
        (r'^SELECT COUNT\(\*\) FROM vector_bindings WHERE generation_id=%s$', [(bindings,)]),
        (r'^SELECT COUNT\(\*\) FROM archived_reference_factors WHERE generation_id=%s$', [(archived,)]),
        (r'^SELECT COUNT\(\*\) FROM reference_vectors WHERE space_id=%s', lambda sql, p: [(shared,)]),
        (r'^SELECT COUNT\(\*\) FROM \w+ WHERE generation_id=%s$', count),
        (r'^SELECT COUNT\(\*\) FROM reference_generations WHERE kind<>%s$', lambda sql, p: [(sum(g['kind'] != p[0] for g in db.generations.values()),)]),
        (r'^SELECT COUNT\(\*\) FROM \w+$', count)]
    return services, db, repo


def confirm(services, phrase=GEN[:12]):
    services.interactive = lambda: True
    services.input = lambda prompt: phrase


def writes(db): return db.statements(r'^(INSERT|UPDATE|DELETE|DROP|CREATE)|GET_LOCK')


def test_abandon_dry_run_reports_and_writes_nothing(tmp_path, capsys):
    services, db, repo = abandon_services(tmp_path)
    result = rr.abandon_generation(services, GEN, apply=False, drop_empty_schema=True)
    assert result['ok'] is True and result['refusals'] == [] and result['rows'] == GENERATION_ROWS and (result['kind'], result['status']) == (rg.KPN_KIND, 'failed')
    assert result['embedding_space'] == {'space_id': SPACE, 'delete': True}
    assert result['drop_schema'] == {'would_drop': list(rr.REFERENCE_TABLES), 'reasons': []}  # only the legacy registration would remain
    assert not writes(db) and db.commits == 0
    assert rr.main(['abandon', '--generation', GEN], services=services) == 0 and json.loads(capsys.readouterr().out)['command'] == 'abandon'
    # Another generation's vectors still use the space: it is kept.
    services, db, repo = abandon_services(tmp_path / 'shared', shared=4)
    assert rr.abandon_generation(services, GEN, apply=False)['embedding_space'] == {'space_id': SPACE, 'delete': False}
    # A refused dry run reports why (and exits 1) without writing.
    services, db, repo = abandon_services(tmp_path / 'bound', bindings=7)
    result = rr.abandon_generation(services, GEN, apply=False, drop_empty_schema=True)
    assert result['ok'] is False and result['refusals'] == ['vector_bindings holds 7 rows of this generation']
    assert result['drop_schema']['would_drop'] == [] and 'reference_vectors holds 5 rows' in result['drop_schema']['reasons']
    assert rr.main(['abandon', '--generation', GEN], services=services) == 1 and not writes(db)


@pytest.mark.parametrize('case', ['not interactive', 'wrong confirmation', 'active', 'bindings', 'archived', 'legacy', 'superseded', 'unknown'])
def test_abandon_apply_refuses_unless_confirmed_unreferenced_and_kpn(tmp_path, case):
    options = {'bindings': {'bindings': 2}, 'archived': {'archived': 1}, 'superseded': {'status': 'superseded'}}.get(case, {})
    services, db, repo = abandon_services(tmp_path, **options)
    confirm(services, 'b' * 11 + 'c' if case == 'wrong confirmation' else GEN[:12])
    if case == 'not interactive': services.interactive = lambda: False
    if case == 'active':
        with services.repository('reveal').transaction() as tx: rg.write_active(tx, GEN, rg.KPN_MODEL, expected_previous=None)
    generation_id = {'legacy': LEGACY, 'unknown': '9' * 64}.get(case, GEN)
    expected = {'not interactive': 'interactive', 'wrong confirmation': 'Confirmation did not match; nothing was deleted',
                'active': 'reveal serves this generation', 'bindings': 'vector_bindings holds 2 rows', 'archived': 'archived_reference_factors holds 1 rows',
                'legacy': f'generation kind is {rg.LEGACY_KIND}, not {rg.KPN_KIND}', 'superseded': 'generation is superseded',
                'unknown': 'Unknown reference generation'}[case]
    with pytest.raises(rr.Refused, match=re.escape(expected)): rr.abandon_generation(services, generation_id, apply=True)
    assert not db.statements(r'^(DELETE|DROP)') and GEN in db.generations and LEGACY in db.generations
    locks, releases = db.statements('GET_LOCK'), db.statements('RELEASE_LOCK')
    assert len(locks) == len(releases) == (0 if case == 'not interactive' else 1)


def test_abandon_apply_deletes_child_first_then_space_then_generation(tmp_path):
    services, db, repo = abandon_services(tmp_path)
    confirm(services)
    result = rr.abandon_generation(services, GEN, apply=True)
    assert result['ok'] is True and result['deleted'] == {**GENERATION_ROWS, 'embedding_spaces': 1, 'reference_generations': 1}
    deletes = [(re.match(r'DELETE FROM (\w+)', sql)[1], sql, params) for sql, params in db.log if sql.startswith('DELETE FROM')]
    assert [table for table, _, _ in deletes] == [*rr.GENERATION_TABLES, 'embedding_spaces', 'reference_generations']
    assert all(sql.endswith('WHERE generation_id=%s LIMIT 10000') and params == (GEN,) for table, sql, params in deletes[:6])
    assert deletes[6][1:] == (rr.ABANDON_DELETES['embedding_spaces'], (SPACE, SPACE)) and 'NOT EXISTS (SELECT 1 FROM reference_vectors' in deletes[6][1]
    assert deletes[7][1:] == (rr.ABANDON_DELETES['reference_generations'], (GEN, rg.KPN_KIND, 'loading', 'failed', 'complete'))
    assert GEN not in db.generations and LEGACY in db.generations and 'drop_schema' not in result
    log = [sql for sql, _ in db.log]
    assert log.index('SELECT GET_LOCK(%s,%s)') < log.index(deletes[0][1]) and log[-1] == 'SELECT RELEASE_LOCK(%s)'
    # A space another generation still uses is kept.
    services, db, repo = abandon_services(tmp_path / 'shared', shared=4, status='complete')
    confirm(services)
    assert rr.abandon_generation(services, GEN, apply=True)['deleted']['embedding_spaces'] == 0
    assert not db.statements('^DELETE FROM embedding_spaces') and GEN not in db.generations
    # The single-row path serves only these two protected tables; delete_batches still refuses both.
    for table in ('dismech_imports', 'archived_reference_factors', 'kpn_traits'):
        with pytest.raises(rr.Refused, match='protected'): rr.delete_abandoned_row(FakeDB(), table, GEN)
    for table in ('embedding_spaces', 'reference_generations'):
        with pytest.raises(rr.Refused, match='protected'): rr.delete_batches(FakeDB(), table, '1=1')


@pytest.mark.parametrize('table', ['vector_bindings', 'archived_reference_factors'])
def test_abandon_rechecks_rows_written_while_the_prompt_waits(tmp_path, table):
    services, db, repo = abandon_services(tmp_path, status='complete')
    # REPEATABLE READ: until the transaction ends, every read sees the view of its first read.
    live, view = {table: 0}, {table: 0}
    db.rules.insert(0, (rf'^SELECT COUNT\(\*\) FROM {table} WHERE generation_id=%s$', lambda sql, p: [(view[table],)]))
    rollback = db.rollback
    def end(): view.update(live); rollback()
    db.rollback = end
    confirm(services)
    def typed(prompt): live[table] = 4; return GEN[:12]  # a snapshot or capture commits rows meanwhile
    services.input = typed
    with pytest.raises(rr.Refused, match=f'{table} holds 4 rows of this generation'): rr.abandon_generation(services, GEN, apply=True)
    assert not db.statements(r'^(DELETE|DROP)') and GEN in db.generations and db.statements('RELEASE_LOCK')


def test_snapshot_and_capture_apply_hold_the_reload_lock(tmp_path):
    services, db, archive, repo = cutover_services(tmp_path)
    services.module('vector_ingestion').record_vector_bindings = lambda connection, state: db.log.append(('BINDINGS', ())) or 3
    target = rr.select_target(services, 'rehearsal')
    dry = rr.snapshot_generation(services, target, GEN, apply=False)
    assert dry['snapshot_id'] == NEW_SNAPSHOT and dry['bindings'] is None and not db.statements('GET_LOCK')
    assert rr.snapshot_generation(services, target, GEN, apply=True)['bindings'] == 3
    assert rr.capture_generation(services, LEGACY, [PREFIX], apply=True, dapper=None)['written'] == 1
    log = [sql for sql, _ in db.log]
    locks = [i for i, sql in enumerate(log) if sql.startswith('SELECT GET_LOCK')]
    releases = [i for i, sql in enumerate(log) if sql.startswith('SELECT RELEASE_LOCK')]
    assert len(locks) == len(releases) == 2 and locks[0] < log.index('BINDINGS') < releases[0] < locks[1]
    reads = [i for i, sql in enumerate(log) if 'FROM reference_generations WHERE generation_id=%s' in sql]
    assert all(any(lock < i < release for i in reads) for lock, release in zip(locks, releases))  # the generation is read under the lock
    # While abandon (or any reload) holds the lock, neither writes; apply holds it itself and passes locked=True.
    db.rules.insert(0, (r'^SELECT GET_LOCK', [(0,)]))
    db.log.clear(); archive.calls.clear()
    with pytest.raises(rr.Refused, match='global lock'): rr.snapshot_generation(services, target, GEN, apply=True)
    with pytest.raises(rr.Refused, match='global lock'): rr.capture_generation(services, LEGACY, [PREFIX], apply=True, dapper=None)
    assert ('BINDINGS', ()) not in db.log and not archive.calls and not db.statements('RELEASE_LOCK')
    assert rr.capture_generation(services, LEGACY, [PREFIX], apply=True, dapper=None, locked=True)['written'] == 1
    assert len(db.statements('GET_LOCK')) == 2


def test_reference_tables_drop_in_foreign_key_order():
    sql = (rr.ROOT / 'schema/migrations/008_reference_generation.sql').read_text()
    tables = {name: set(re.findall(r'REFERENCES (\w+)\(', body)) for name, body in re.findall(r'CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\) ENGINE', sql, re.S)}
    assert set(rr.REFERENCE_TABLES) == set(tables) and len(rr.REFERENCE_TABLES) == 10
    for position, table in enumerate(rr.REFERENCE_TABLES):  # every table a foreign key names is dropped later
        assert all(rr.REFERENCE_TABLES.index(parent) > position for parent in tables[table] - {table}), table
    assert set(rr.GENERATION_TABLES) < set(rr.REFERENCE_TABLES)


def test_abandon_drop_empty_schema_drops_in_order_only_when_empty(tmp_path, capsys):
    services, db, repo = abandon_services(tmp_path / 'archived', other_rows={'archived_reference_factors': 2})
    confirm(services)
    result = rr.abandon_generation(services, GEN, apply=True, drop_empty_schema=True)
    assert result['deleted']['reference_generations'] == 1 and result['ok'] is False
    assert result['drop_schema'] == {'dropped': [], 'reasons': ['archived_reference_factors holds 2 rows']} and not db.statements('^DROP')
    services, db, repo = abandon_services(tmp_path / 'gate')
    with repo.transaction() as tx: rg.set_gate(tx, False, reason='operator')  # a prefix has used the reload
    confirm(services)
    result = rr.abandon_generation(services, GEN, apply=True, drop_empty_schema=True)
    assert result['drop_schema'] == {'dropped': [], 'reasons': [f"{PREFIX}_records holds reload records {{'{rg.CONTROL_KIND}': 1}}"]}
    assert not db.statements('^DROP')
    services, db, repo = abandon_services(tmp_path / 'empty')
    confirm(services)
    assert rr.main(['abandon', '--generation', GEN, '--drop-empty-schema', '--apply'], services=services) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['drop_schema'] == {'dropped': list(rr.REFERENCE_TABLES), 'reasons': []}
    drops = [sql for sql, _ in db.log if sql.startswith('DROP')]
    assert drops == [f'DROP TABLE {table}' for table in rr.REFERENCE_TABLES]
    log = [sql for sql, _ in db.log]
    assert log.index('DELETE FROM reference_generations WHERE generation_id=%s AND kind=%s AND status IN (%s,%s,%s) LIMIT 1') < log.index(drops[0])
    assert log.index(drops[-1]) < log.index('SELECT RELEASE_LOCK(%s)')


# --------------------------------------------------------------------------------------
# Targets and protections


def test_targets_and_environment_mismatches_are_refused(tmp_path):
    services = fake_services(tmp_path)
    with pytest.raises(rr.Refused, match='not allow-listed'): rr.select_target(services, 'staging')
    target = rr.select_target(services, 'rehearsal')
    for shell in ({'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal'}, {'REVEAL_VECTOR_ENVIRONMENT': 'prod'}):
        with pytest.raises(rr.Refused, match='Shell'): rr.bind_target(fake_services(tmp_path, shell=shell), target)
    with pytest.raises(rr.Refused, match='REVEAL_MYSQL_DATABASE'):
        rr.bind_target(fake_services(tmp_path, environ={**ENV, 'REVEAL_MYSQL_DATABASE': 'cyaka_other'}), target)
    with pytest.raises(rr.Refused, match='host other.upstash.io'):
        rr.bind_target(fake_services(tmp_path, environ={**ENV, 'UPSTASH_VECTOR_REST_URL': 'https://other.upstash.io'}), target)
    ok = fake_services(tmp_path, shell={'REVEAL_VECTOR_ENVIRONMENT': ENV_NAME}, environ={**ENV, 'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal'})
    rr.bind_target(ok, target)  # .env values are overridden by the target; only the shell must agree
    assert ok.environ['REVEAL_APPLICATION_TABLE_PREFIX'] == PREFIX and ok.environ['REVEAL_VECTOR_ENVIRONMENT'] == ENV_NAME
    (tmp_path / 'bad.yaml').write_text(TARGETS.replace('production: false', 'production: maybe', 1))
    with pytest.raises(rr.Refused, match='production'): rr.load_targets(tmp_path / 'bad.yaml')


# --------------------------------------------------------------------------------------
# plan / approve / apply


class Archive:
    """Fake reference_archive recording calls and the gate/active state it observed."""
    KIND_ACTIONS = {'account': 'archive', 'draft': 'drop_anchored_draft', 'job': 'cancel_nonterminal', 'principal': 'keep',
                    'vector_snapshot': 'keep', 'vector_active': 'keep', 'suggestion': 'purge_at_retire'}
    def __init__(self, services):
        self.services, self.calls = services, []
        self.prefix = {'counts_by_kind': {'account': 2, 'draft': 1, 'principal': 3, rg.CONTROL_KIND: 1}, 'unknown_kinds': [],
                       'archive_candidates': {'account': [{'owner': 'u1', 'id': 'a1'}, {'owner': 'u1', 'id': 'a2'}]},
                       'anchored_drafts': ['d1'], 'nonterminal_jobs': []}
    def classify(self, kind): return self.KIND_ACTIONS.get(kind, 'keep' if kind.startswith('reference_') else None)
    def _state(self, repo):
        with repo.read_transaction() as tx: return {'gate': bool(rg.read_gate(tx)), 'active': (rg.read_active(tx) or {}).get('generation_id')}
    def plan_prefix(self, repo, generation_id, **options):
        self.calls.append(('plan_prefix', generation_id, options)); return json.loads(json.dumps(self.prefix))
    def cancel_nonterminal_jobs(self, repo, generation_id, *, apply): self.calls.append(('cancel', generation_id, apply, self._state(repo))); return ['j1']
    def referenced_sources(self, repo): self.calls.append(('referenced', repo.table_prefix)); return {LEGACY: {'factor:portal:T2D:cfde-inc-v2:Factor1'}}
    def capture_factors(self, connection, generation, sources, *, runtime=None): self.calls.append(('capture', generation['generation_id'])); return [{'source_id': s} for s in sources]
    def write_archived_factors(self, connection, rows): self.calls.append(('write', len(rows))); return len(rows)
    def backfill_anchor_display(self, repo, *, apply): self.calls.append(('backfill', repo.table_prefix, apply)); return {'backfilled': 0}
    def archive_prefix(self, repo, source, target, *, apply, at=None):
        self.calls.append(('archive', source, target, apply, self._state(repo))); return {'counts': {'account': 2}}


class Upstash:
    def __init__(self): self.deleted = []
    def delete_namespace(self, name): self.deleted.append(name)


def cutover_services(tmp_path, *, calls=None):
    calls = [] if calls is None else calls
    db = FakeDB(generations=[generation(GEN, rg.KPN_KIND, 'complete'), generation(LEGACY, rg.LEGACY_KIND, 'complete', legacy_mapping_run_id='a' * 64)])
    sqlite = tmp_path / 'records.sqlite3'
    def repository(prefix):
        repo = Repository(sqlite_path=str(sqlite), table_prefix=prefix); repo.migrate(); return repo
    namespaces = {f'{ENV_NAME}-eaggl-factor-' + 'c' * 24: 3, f'{ENV_NAME}-f-' + 'd' * 48: 3, 'prod-f-' + 'e' * 48: 1}
    from reveal_backend.vector_ingestion import deletable_namespace
    def delete_namespace(client, name, *, environment, protected=()):
        assert deletable_namespace(name, environment) and name not in protected
        calls.append(('delete_namespace', name, environment)); return True
    vi = SimpleNamespace(VectorRegistry=VectorRegistry, list_namespaces=lambda client: dict(namespaces),
                         deletable_namespace=deletable_namespace, delete_namespace=delete_namespace)
    services = fake_services(tmp_path, connect=lambda: db, repository=repository, vector_client=lambda write=False: Upstash())
    archive = Archive(services)
    services.module = lambda name: {'reference_archive': archive, 'vector_ingestion': vi}.get(name) or rr.importlib.import_module('reveal_backend.' + name)
    def run(command, *, stdout, stderr, env, check):
        calls.append(('mysqldump', command, env)); stdout.write(b'-- MySQL dump\nINSERT ...;\n-- Dump completed on 2026-09-30\n')
        return SimpleNamespace(returncode=0, stderr=b'')
    services.run = run
    services.rds = lambda: SimpleNamespace(describe_db_cluster_snapshots=lambda **kw: calls.append(('rds', kw)) or {'DBClusterSnapshots': [{'Status': 'available'}]})
    repo = repository(PREFIX)
    snapshot = {'snapshot_id': NEW_SNAPSHOT, 'environment': ENV_NAME, 'status': 'complete', 'reference_generation_id': GEN, 'verification': {'passed': True},
                'factor_namespace': f'{ENV_NAME}-eaggl-factor-' + 'c' * 24, 'context_namespace': f'{ENV_NAME}-dismech-context-' + 'c' * 24,
                'factors': [{'id': 'x'}], 'contexts': [], 'batches': {}, 'verified_batches': {}}
    with repo.transaction() as tx:
        tx.put('vector_snapshot', NEW_SNAPSHOT, 'catalog', snapshot)
        tx.put('vector_snapshot', OLD_SNAPSHOT, 'catalog', {**snapshot, 'snapshot_id': OLD_SNAPSHOT, 'reference_generation_id': None,
                                                            'factor_namespace': f'{ENV_NAME}-f-' + 'd' * 48, 'context_namespace': f'{ENV_NAME}-c-' + 'd' * 48})
        tx.put('vector_active', ENV_NAME, 'catalog', {'snapshot_id': OLD_SNAPSHOT, 'previous_snapshot_id': None, 'activated_at': 'then'})
    return services, db, archive, repo


def write_plan(services, tmp_path, name='rehearsal'):
    target = rr.bind_target(services, rr.select_target(services, name))
    plan = rr.build_plan(services, target, GEN)
    return target, plan, rr.write_json(tmp_path / f'plan.{name}.json', plan)


def approve(services, plan_path, **kwargs):
    plan = rr.read_json(plan_path)
    services.interactive = lambda: True
    services.input = lambda prompt: f"{plan['target']['name']}:{plan['plan_sha256'][:12]}"
    return rr.approve_plan(services, plan_path, **kwargs)


def test_plan_is_read_only_and_its_sha_ignores_timestamps_and_observed_counts(tmp_path):
    services, db, archive, repo = cutover_services(tmp_path)
    target, plan, path = write_plan(services, tmp_path)
    assert plan['from_generation'] == LEGACY and plan['snapshot']['snapshot_id'] == NEW_SNAPSHOT and not plan['blockers']
    assert plan['observed']['archive_counts_by_kind'] == {'account': 2, 'draft': 1}  # keep kinds and bookkeeping are informational only
    assert set(plan['archive']) == {'anchored_drafts', 'unresolved', 'anchor_display_backfill'}
    assert archive.calls[0] == ('plan_prefix', GEN, {'from_generation': LEGACY})  # legacy mode assumes the served generation
    assert rr._summary(plan)['archive_candidates'] == 2 and rr._summary(plan)['archive_candidates_by_kind'] == {'account': 2}
    assert set(plan['namespaces']) == {f'{ENV_NAME}-eaggl-factor-' + 'c' * 24, f'{ENV_NAME}-f-' + 'd' * 48}
    assert not [sql for sql, _ in db.log if re.match(r'(INSERT|UPDATE|DELETE|CREATE)', sql)]
    with repo.read_transaction() as tx: assert rg.read_active(tx) is None and tx.get(rg.CONTROL_KIND, rg.CONTROL_ID) is None
    services.clock = lambda: '2026-10-01T00:00:00Z'
    archive.prefix['counts_by_kind']['principal'] = 9
    assert rr.build_plan(services, target, GEN)['plan_sha256'] == plan['plan_sha256'] == rr.read_plan(path)['plan_sha256']
    # Normal activity between plan, approve and apply does not drift the plan: new suggestions and
    # accounts, and jobs moving between states, are observed only (stamping is idempotent; jobs are
    # re-evaluated under the gate).
    archive.prefix['counts_by_kind'].update(suggestion=40, job=7, account=3)
    archive.prefix['archive_candidates']['account'].append({'owner': 'u2', 'id': 'a3'})
    archive.prefix['nonterminal_jobs'] = [{'id': 'j1', 'owner': 'u1', 'kind': 'paragraph', 'status': 'running', 'collected': False, 'action': 'continue'}]
    moved = rr.build_plan(services, target, GEN)
    assert moved['plan_sha256'] == plan['plan_sha256'] and moved['observed']['nonterminal_jobs'][0]['status'] == 'running'
    archive.prefix['anchored_drafts'].append('d2')  # the destructive set stays pinned
    assert rr.build_plan(services, target, GEN)['plan_sha256'] != plan['plan_sha256']


def test_plan_fails_closed_on_unknown_kinds(tmp_path, capsys):
    services, db, archive, repo = cutover_services(tmp_path)
    archive.prefix['unknown_kinds'] = ['mystery']
    code = rr.main(['plan', '--target', 'rehearsal', '--generation', GEN, '--out', str(tmp_path / 'plan.json')], services=services)
    assert code == 2 and 'mystery' in json.loads(capsys.readouterr().out)['refused'] and not (tmp_path / 'plan.json').exists()


def test_approval_needs_interactive_typed_confirmation_and_production_flags(tmp_path):
    services, db, archive, repo = cutover_services(tmp_path)
    target, plan, path = write_plan(services, tmp_path)
    services.interactive = lambda: False
    with pytest.raises(rr.Refused, match='interactive'): rr.approve_plan(services, path)
    services.interactive, services.input = (lambda: True), (lambda prompt: 'rehearsal:000000000000')
    with pytest.raises(rr.Refused, match='did not match'): rr.approve_plan(services, path)
    assert not (tmp_path / 'approval.rehearsal.json').exists()
    result = approve(services, path)
    approval = rr.read_json(result['approval'])
    assert approval['plan_sha256'] == plan['plan_sha256'] and approval['target'] == 'rehearsal' and approval['production'] is False
    assert stat.S_IMODE((tmp_path / 'approval.rehearsal.json').stat().st_mode) == 0o600
    tampered = dict(plan, generation_id='9' * 64)
    rr.write_json(tmp_path / 'tampered.json', tampered)
    with pytest.raises(rr.Refused, match='sha does not match'): rr.approve_plan(services, tmp_path / 'tampered.json')
    production = dict(plan, target=rr.select_target(services, 'prod'))
    production['plan_sha256'] = rr.plan_digest(production)
    rr.write_json(tmp_path / 'prod.json', production)
    with pytest.raises(rr.Refused, match='--allow-production'): approve(services, tmp_path / 'prod.json')
    with pytest.raises(rr.Refused, match=rr.PRODUCTION_APPROVAL): approve(services, tmp_path / 'prod.json', allow_production=True)
    services.environ[rr.PRODUCTION_APPROVAL] = production['plan_sha256']
    assert approve(services, tmp_path / 'prod.json', allow_production=True)['production'] is True


def apply(services, tmp_path, **kwargs):
    options = dict(backup_dir=tmp_path / 'backups', backup_snapshot_id='rds-snap-1', poll_seconds=1)
    options.update(kwargs)
    return rr.apply_plan(services, rr.select_target(services, 'rehearsal'), tmp_path / 'plan.rehearsal.json',
                         tmp_path / 'approval.rehearsal.json', **options)


def test_apply_runs_the_protected_cutover_in_order(tmp_path, monkeypatch):
    calls = []
    services, db, archive, repo = cutover_services(tmp_path, calls=calls)
    target, plan, path = write_plan(services, tmp_path)
    approve(services, path)
    verified = []
    def verify(services_, target_, *, generation_id=None, client=None):
        with repo.read_transaction() as tx: verified.append((rg.read_active(tx)['generation_id'], bool(rg.read_gate(tx))))
        return {'passed': True, 'checks': {}}
    monkeypatch.setattr(rr, 'verify_target', verify)
    result = apply(services, tmp_path)
    assert result['status'] == 'complete' and result['ok'] is True
    assert [step['step'] for step in result['steps']] == ['preflight', 'lock', 'gate_closed', 'drain', 'backups', 'delta_capture', 'snapshot_verified',
                                                          'activated', 'superseded', 'archived', 'post_archive_capture', 'verified', 'gate_opened']
    dumps = [call[1] for call in calls if call[0] == 'mysqldump']
    assert len(dumps) == 2 and '--no-data' in dumps[0] and '--no-data' not in dumps[1]  # schema-only pre-flight, then the backup
    assert len(list((tmp_path / 'backups').iterdir())) == 1  # the pre-flight dump is removed
    kinds = [call[0] for call in archive.calls]
    assert kinds[:2] == ['plan_prefix', 'plan_prefix']  # plan, then the drift re-plan
    assert kinds[2:] == ['cancel', 'referenced', 'referenced', 'capture', 'write', 'backfill', 'backfill', 'archive',
                         'referenced', 'capture', 'write', 'backfill']  # post-archive capture of this target's new stamps
    cancel, archived = archive.calls[2], archive.calls[-5]
    assert archive.calls[-4] == ('referenced', PREFIX)
    assert cancel[1:3] == (LEGACY, True) and cancel[3] == {'gate': True, 'active': None}
    assert archived[1:4] == (LEGACY, GEN, True) and archived[4] == {'gate': True, 'active': GEN}
    assert verified == [(GEN, True)]
    dump = next(call for call in calls if call[0] == 'mysqldump')
    assert dump[2]['MYSQL_PWD'] == 'secret-pw' and not any('secret-pw' in part for part in dump[1])
    assert f'{PREFIX}_records' in dump[1] and ('rds', {'DBClusterSnapshotIdentifier': 'rds-snap-1'}) in calls
    assert db.generations[LEGACY]['status'] == 'superseded'
    lock = [i for i, (sql, _) in enumerate(db.log) if 'GET_LOCK' in sql]
    release = [i for i, (sql, _) in enumerate(db.log) if 'RELEASE_LOCK' in sql]
    assert lock and release and lock[0] < release[0]
    with repo.read_transaction() as tx:
        active, vector_active = rg.read_active(tx), tx.get('vector_active', ENV_NAME)['data']
        audit = tx.get(rg.RELOAD_KIND, digest([plan['plan_sha256']]))['data']
        assert rg.read_gate(tx) is None
    assert [step['step'] for step in audit['steps']][-1] == 'gate_opened'
    assert stat.S_IMODE((tmp_path / 'backups').iterdir().__next__().stat().st_mode) == 0o600
    assert active['generation_id'] == GEN and active['vector_snapshot_id'] == NEW_SNAPSHOT and vector_active['snapshot_id'] == NEW_SNAPSHOT
    assert audit['status'] == 'complete' and audit['verification']['passed'] and audit['backups']['aurora']['snapshot_id'] == 'rds-snap-1'
    # A repeated apply is refused as drift (the target is already active), before the lock or gate.
    before = len(db.statements('GET_LOCK'))
    with pytest.raises(rr.Refused, match='Plan drift'): apply(services, tmp_path)
    assert len(db.statements('GET_LOCK')) == before


def test_apply_refuses_drift_missing_backup_and_unapproved_production(tmp_path, monkeypatch):
    services, db, archive, repo = cutover_services(tmp_path)
    target, plan, path = write_plan(services, tmp_path)
    approve(services, path)
    with pytest.raises(rr.Refused, match='backup'): apply(services, tmp_path, backup_snapshot_id=None)
    archive.prefix['anchored_drafts'].append('d-new')
    with pytest.raises(rr.Refused, match=r"Plan drift in \['archive'\]"): apply(services, tmp_path)
    assert not db.statements('GET_LOCK') and ('cancel' not in [call[0] for call in archive.calls])
    with repo.read_transaction() as tx: assert tx.get(rg.CONTROL_KIND, rg.CONTROL_ID) is None
    other = rr.read_json(tmp_path / 'approval.rehearsal.json')
    rr.write_json(tmp_path / 'approval.rehearsal.json', dict(other, plan_sha256='0' * 64))
    with pytest.raises(rr.Refused, match='approval does not match'): apply(services, tmp_path)


def waiver_cutover_services(tmp_path, name, *, calls=None):
    services, db, archive, original = cutover_services(tmp_path, calls=calls)
    services.targets_file.write_text(TARGETS + f"""  {name}:
    database: cyaka_reveal_mechanisms
    prefix: reveal_workflow_{name}
    vector_environment: {name}
    upstash_host: {HOST}
    production: false
""")
    repo = services.repository(f'reveal_workflow_{name}')
    with original.read_transaction() as tx:
        snapshots = tx.list('vector_snapshot')
        active = tx.get('vector_active', ENV_NAME)['data']
    with repo.transaction() as tx:
        for row in snapshots:
            data = row['data']
            tx.put('vector_snapshot', row['id'], 'catalog', {**data, 'environment': name,
                'factor_namespace': data['factor_namespace'].replace(ENV_NAME + '-', name + '-', 1),
                'context_namespace': data['context_namespace'].replace(ENV_NAME + '-', name + '-', 1)})
        tx.put('vector_active', name, 'catalog', active)
    return services, db, archive, repo


@pytest.mark.parametrize('name', ['local', 'qa'])
def test_snapshot_waiver_keeps_protected_apply_and_truthful_audit(tmp_path, monkeypatch, capsys, name):
    calls = []
    services, db, archive, repo = waiver_cutover_services(tmp_path, name, calls=calls)
    target, plan, path = write_plan(services, tmp_path, name)
    approve(services, path)
    def forbidden_rds(): raise AssertionError('The explicit waiver must not call RDS')
    services.rds = forbidden_rds
    verified = []
    def verify(*args, **kwargs):
        with repo.read_transaction() as tx:
            verified.append((rg.read_active(tx)['generation_id'], bool(rg.read_gate(tx))))
        return {'passed': True, 'checks': {}}
    monkeypatch.setattr(rr, 'verify_target', verify)
    assert rr.main(['apply', '--target', name, '--plan', str(path), '--approval', str(tmp_path / f'approval.{name}.json'),
                    '--backup-dir', str(tmp_path / 'backups'), '--skip-aurora-snapshot'], services=services) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] and result['status'] == 'complete'
    skipped = {'status': 'skipped', 'reason': f'explicit_{name}_only_waiver', 'created': False}
    assert result['backups']['aurora'] == result['steps'][0]['aurora'] == skipped
    assert [step['step'] for step in result['steps']] == ['preflight', 'lock', 'gate_closed', 'drain', 'backups',
        'delta_capture', 'snapshot_verified', 'activated', 'superseded', 'archived', 'post_archive_capture', 'verified', 'gate_opened']
    dumps = [call[1] for call in calls if call[0] == 'mysqldump']
    assert len(dumps) == 2 and '--no-data' in dumps[0] and '--no-data' not in dumps[1]
    assert all(f'reveal_workflow_{name}_records' in command for command in dumps)
    assert len(list((tmp_path / 'backups').iterdir())) == 1
    assert result['backups']['records']['bytes'] > 0 and result['backups']['records']['sha256']
    assert verified == [(GEN, True)]
    with repo.read_transaction() as tx:
        audit = tx.get(rg.RELOAD_KIND, digest([plan['plan_sha256']]))['data']
        assert rg.read_gate(tx) is None and rg.read_active(tx)['generation_id'] == GEN
        assert tx.get('vector_active', name)['data']['snapshot_id'] == NEW_SNAPSHOT
    assert audit['backups']['aurora'] == audit['steps'][0]['aurora'] == skipped
    assert audit['verification']['passed'] and audit['status'] == 'complete'


@pytest.mark.parametrize('name', ['local', 'qa'])
@pytest.mark.parametrize('change', [
    {'name': 'rehearsal'}, {'name': 'compose'}, {'name': 'prod'}, {'name': 'other'},
    {'prefix': 'reveal'}, {'prefix': 'reveal_compose'}, {'vector_environment': 'prod'},
    {'production': True}, {'production': None}, {'production': 0},
])
def test_snapshot_waiver_refuses_other_identities_before_side_effects(tmp_path, name, change):
    target = {'name': name, 'prefix': f'reveal_workflow_{name}', 'vector_environment': name, 'production': False, **change}
    with pytest.raises(rr.Refused, match='allowed only for the exact non-production local or QA target'):
        rr.apply_plan(SimpleNamespace(), target, tmp_path / 'unused-plan', tmp_path / 'unused-approval', skip_aurora_snapshot=True)


@pytest.mark.parametrize('name', ['local', 'qa'])
@pytest.mark.parametrize('field', ['name', 'prefix', 'vector_environment'])
def test_snapshot_waiver_refuses_mixed_local_qa_identities(tmp_path, name, field):
    other = 'qa' if name == 'local' else 'local'
    target = {'name': name, 'prefix': f'reveal_workflow_{name}', 'vector_environment': name, 'production': False}
    target[field] = f'reveal_workflow_{other}' if field == 'prefix' else other
    with pytest.raises(rr.Refused, match='allowed only for the exact non-production local or QA target'):
        rr.apply_plan(SimpleNamespace(), target, tmp_path / 'unused-plan', tmp_path / 'unused-approval', skip_aurora_snapshot=True)


@pytest.mark.parametrize('name', ['local', 'qa'])
@pytest.mark.parametrize('failure', ['approval', 'backup_dir', 'drift', 'records', 'vector'])
def test_snapshot_waiver_preserves_pre_gate_guards(tmp_path, monkeypatch, failure, name):
    services, db, archive, repo = waiver_cutover_services(tmp_path, name)
    target, plan, path = write_plan(services, tmp_path, name)
    approval_path = tmp_path / f'approval.{name}.json'
    approve(services, path)
    options = {'skip_aurora_snapshot': True, 'backup_dir': tmp_path / 'backups'}
    def forbidden_rds(): raise AssertionError('The explicit waiver must not call RDS')
    services.rds = forbidden_rds
    if failure == 'approval':
        approval = rr.read_json(approval_path)
        rr.write_json(approval_path, dict(approval, plan_sha256='0' * 64))
        expected = 'approval does not match'
    elif failure == 'backup_dir':
        options['backup_dir'] = None
        expected = '--backup-dir is required'
    elif failure == 'drift':
        archive.prefix['anchored_drafts'].append('newly-saved-draft')
        expected = 'Plan drift'
    elif failure == 'records':
        services.run = lambda *args, **kwargs: SimpleNamespace(returncode=2, stderr=b'Test dump failure')
        expected = 'mysqldump pre-flight'
    else:
        def unverified(*args): raise rr.Refused('The target snapshot is not verified')
        monkeypatch.setattr(rr, 'verified_snapshot', unverified)
        expected = 'not verified'
    with pytest.raises(rr.Refused, match=expected):
        rr.apply_plan(services, target, path, approval_path, **options)
    assert not db.statements('GET_LOCK') and 'cancel' not in [call[0] for call in archive.calls]
    with repo.read_transaction() as tx:
        assert rg.read_gate(tx) is None and rg.read_active(tx) is None


@pytest.mark.parametrize('name', ['local', 'qa'])
@pytest.mark.parametrize('option', ['--backup-snapshot-id', '--create-aurora-snapshot'])
def test_snapshot_waiver_cannot_be_combined_with_other_backup_choices(tmp_path, option, name):
    arguments = ['apply', '--target', name, '--plan', 'plan.json', '--approval', 'approval.json', '--skip-aurora-snapshot', option]
    if option == '--backup-snapshot-id': arguments.append('real-snapshot')
    with pytest.raises(SystemExit) as error: rr.parser().parse_args(arguments)
    assert error.value.code == 2
    target = {'name': name, 'prefix': f'reveal_workflow_{name}', 'vector_environment': name, 'production': False}
    options = {'backup_snapshot_id': 'real-snapshot'} if option == '--backup-snapshot-id' else {'create_aurora_snapshot': True}
    with pytest.raises(rr.Refused, match='cannot be combined'):
        rr.apply_plan(SimpleNamespace(), target, tmp_path / 'unused-plan', tmp_path / 'unused-approval', skip_aurora_snapshot=True, **options)


def test_apply_reopens_the_gate_and_records_failure(tmp_path, monkeypatch):
    services, db, archive, repo = cutover_services(tmp_path)
    target, plan, path = write_plan(services, tmp_path)
    approve(services, path)
    def fail(*args, **kwargs): raise RuntimeError('archive exploded')
    archive.archive_prefix = fail
    with pytest.raises(RuntimeError, match='archive exploded'): apply(services, tmp_path)
    with repo.read_transaction() as tx:
        audit = tx.get(rg.RELOAD_KIND, digest([plan['plan_sha256']]))['data']
        assert rg.read_gate(tx) is None and rg.read_active(tx)['generation_id'] == GEN
    assert audit['status'] == 'failed' and 'archive exploded' in audit['error']
    assert [step['step'] for step in audit['steps']][-2:] == ['superseded', 'gate_opened']
    # Resume: a fresh plan for the already-active target finishes the cutover (archive, capture, verify).
    del archive.archive_prefix
    monkeypatch.setattr(rr, 'verify_target', lambda *a, **k: {'passed': False, 'checks': {'self_neighbour': {'passed': False}}})
    target, resume, path = write_plan(services, tmp_path)
    assert resume['blockers'] == [] and resume['resume'] == {'from_generation': LEGACY, 'interrupted_plans': [plan['plan_sha256']]}
    assert resume['from_generation'] == LEGACY and rr._summary(resume)['resume']['from_generation'] == LEGACY
    approve(services, path)
    cancels, dumps = len([c for c in archive.calls if c[0] == 'cancel']), len(db.statements('GET_LOCK'))
    result = apply(services, tmp_path, backup_dir=None, backup_snapshot_id=None)  # a resume takes no new backups
    assert result['status'] == 'verify_failed' and result['ok'] is False
    assert [step['step'] for step in result['steps']] == ['lock', 'gate_closed', 'resumed', 'archived', 'post_archive_capture', 'verified', 'gate_opened']
    assert len([c for c in archive.calls if c[0] == 'cancel']) == cancels  # no drain, no activation, no backups
    assert next(c for c in reversed(archive.calls) if c[0] == 'archive')[1:4] == (LEGACY, GEN, True)
    # A failed verification is resumable too; a passing one is recorded for purge-retired.
    monkeypatch.setattr(rr, 'verify_target', lambda *a, **k: {'passed': True, 'checks': {}})
    target, again, path = write_plan(services, tmp_path)
    assert again['resume'] == {'from_generation': LEGACY, 'interrupted_plans': sorted([plan['plan_sha256'], resume['plan_sha256']])}
    assert again['plan_sha256'] != resume['plan_sha256']
    approve(services, path)
    result = apply(services, tmp_path)
    assert result['status'] == 'complete' and len(db.statements('GET_LOCK')) == dumps + 2
    with repo.read_transaction() as tx:
        audit = tx.get(rg.RELOAD_KIND, digest([again['plan_sha256']]))['data']
        assert rg.read_gate(tx) is None
    assert (audit['status'], audit['generation_id'], audit['from_generation'], audit['verification']['passed']) == ('complete', GEN, LEGACY, True)
    assert 'rehearsal has no recorded passing verification' not in ' '.join(rr.build_purge_plan(services, GEN)['blockers'])
    # Once complete, there is nothing to resume.
    assert rr.build_plan(services, target, GEN)['blockers'] == ['target is already active on this generation']


def test_apply_preflight_refuses_before_the_gate_and_cancels_nothing(tmp_path):
    services, db, archive, repo = cutover_services(tmp_path)
    target, plan, path = write_plan(services, tmp_path)
    approve(services, path)
    def missing(command, **kwargs): raise FileNotFoundError(2, 'No such file or directory', 'mysqldump')
    services.run = missing
    with pytest.raises(rr.Refused, match='mysqldump could not run'): apply(services, tmp_path)
    services.run = lambda command, *, stdout, stderr, env, check: SimpleNamespace(returncode=2, stderr=b'SSL connection error')
    with pytest.raises(rr.Refused, match='pre-flight .* SSL connection error'): apply(services, tmp_path)
    assert not list((tmp_path / 'backups').iterdir())
    services.run = lambda command, *, stdout, stderr, env, check: stdout.write(b'-- Dump completed\n') and SimpleNamespace(returncode=0, stderr=b'')
    services.rds = lambda: SimpleNamespace(describe_db_cluster_snapshots=lambda **kw: {'DBClusterSnapshots': []},
                                           describe_db_clusters=lambda **kw: {'DBClusters': []})
    with pytest.raises(rr.Refused, match='rds-snap-1 is not available'): apply(services, tmp_path)
    with pytest.raises(rr.Refused, match='cluster aurora-x was not found'): apply(services, tmp_path, backup_snapshot_id=None, create_aurora_snapshot=True)
    assert not db.statements('GET_LOCK') and 'cancel' not in [call[0] for call in archive.calls]
    with repo.read_transaction() as tx: assert rg.read_gate(tx) is None and rg.read_active(tx) is None
    # An unverified snapshot is a plan blocker, and apply re-checks it before the gate.
    with repo.transaction() as tx:
        state = tx.get('vector_snapshot', NEW_SNAPSHOT)['data']
        tx.put('vector_snapshot', NEW_SNAPSHOT, 'catalog', dict(state, verification={'passed': False}))
    assert 'the target vector snapshot is not verified; run `snapshot --apply`' in rr.build_plan(services, target, GEN)['blockers']
    with pytest.raises(rr.Refused, match='not a verified snapshot'): rr.verified_snapshot(services, repo, target, plan)


def test_drain_waits_then_refuses_or_cancels_collected_jobs(tmp_path):
    services, db, archive, repo = cutover_services(tmp_path)
    with repo.transaction() as tx:
        tx.put('job', 'j9', 'u1', {'id': 'j9', 'kind': 'analysis', 'status': 'running', 'owner_user_id': 'u1', 'research_request_id': 'r9',
                                  'stage': 'x', 'result': None, 'last_event_id': '0'})
        tx.put('request_binding', 'r9', 'u1', {'anchors': [{'mapping_run_id': 'a' * 64}]})
        tx.put('job', 'j8', 'u1', {'id': 'j8', 'kind': 'analysis', 'status': 'running', 'owner_user_id': 'u1', 'research_request_id': 'r8'})
        tx.put('request_binding', 'r8', 'u1', {'anchors': [{'reference_generation_id': GEN}]})
    assert [job['id'] for job in rr.open_jobs(repo, LEGACY)] == ['j9']
    slept = []
    services.sleep = slept.append
    with pytest.raises(rr.Refused, match='--cancel-active'):
        rr.drain_jobs(services, repo, LEGACY, cancel_active=False, drain_seconds=10, poll_seconds=5)
    assert slept == [5, 5]
    # The anonymous workspace that submitted j9 was claimed by u2: only owner_id moved, not the payload.
    with repo.transaction() as tx: tx.transfer('u1', 'u2')
    with repo.read_transaction() as tx: assert (tx.get('job', 'j9')['owner'], tx.get('job', 'j9')['data']['owner_user_id']) == ('u2', 'u1')
    result = rr.drain_jobs(services, repo, LEGACY, cancel_active=True, drain_seconds=10, poll_seconds=5)
    assert result['stopped'] == ['j9'] and result['cancelled'] == ['j1']
    with repo.read_transaction() as tx:
        job = tx.get('job', 'j9')
        assert job['data']['status'] == 'cancelled' and job['owner'] == 'u2' and job['data']['owner_user_id'] == 'u2'
        assert {row['owner'] for row in tx.list('event') if row['id'].startswith('j9:')} == {'u2'}


def test_activation_is_one_compare_and_swap_transaction(tmp_path):
    services, db, archive, repo = cutover_services(tmp_path)
    target, plan, path = write_plan(services, tmp_path)
    stale = dict(plan, active={'reference': {'generation_id': '7' * 64}, 'vector': plan['active']['vector']})
    with pytest.raises(rg.ReferenceError): rr.activate(services, repo, target, stale)
    with repo.read_transaction() as tx:
        assert tx.get('vector_active', ENV_NAME)['data']['snapshot_id'] == OLD_SNAPSHOT and rg.read_active(tx) is None


# --------------------------------------------------------------------------------------
# purge-retired


def test_purge_statements_run_child_first_and_skip_kept_imports():
    new = generation(GEN, rg.KPN_KIND, 'complete')
    legacy = generation(LEGACY, rg.LEGACY_KIND, 'superseded', legacy_mapping_run_id='a' * 64, legacy_gene_set_import_id='2' * 64)
    old_kpn = generation('6' * 64, rg.KPN_KIND, 'superseded', eaggl_import_id='5' * 64, eaggl_embedding_run_id='4' * 64)
    statements = rr.purge_statements(new, [legacy, old_kpn], [new], all_gene_set_imports_retired=True)
    assert [(step, table) for step, table, _, _ in statements] == [
        (1, 'eaggl_cfde_gene_set_links'), (1, 'eaggl_cfde_factor_links'), (1, 'eaggl_cfde_link_runs'),
        (2, 'dismech_embedding_inputs'), (2, 'dismech_embedding_vectors'), (2, 'dismech_embedding_runs'),
        (3, 'eaggl_name_embeddings'), (3, 'eaggl_embedding_runs'), (3, 'eaggl_graph_edges'), (3, 'eaggl_graph_nodes'),
        (3, 'eaggl_gene_loadings'), (3, 'eaggl_factors'), (3, 'eaggl_genes'), (3, 'eaggl_imports'),
        (4, 'cfde_gene_set_aliases'), (4, 'gene_set_imports'), (4, 'dapper_objects'), (4, 'dapper_objects'),
        (5, 'reference_vectors'), (5, 'factor_gene_set_projections'), (5, 'cfde_gene_sets'), (5, 'cfde_gene_set_collections'),
        (5, 'reference_factors'), (5, 'kpn_traits')]
    step3 = [params for step, _, _, params in statements if step == 3]
    assert all(params == ('5' * 64,) for params in step3)  # the shared EAGGL import 'e'*64 is kept
    step2 = [(where, params) for step, _, where, params in statements if step == 2]
    # Only DisMech context runs bound to an embedding run of a retired import (what the cold exports hold), never a kept run.
    assert all(params == ('5' * 64, 'e' * 64, 'f' * 64) and 'SELECT run_id FROM eaggl_embedding_runs WHERE import_id IN (%s,%s)' in where
               and 'NOT IN (%s)' in where for where, params in step2)
    assert [table for step, table, _, _ in rr.purge_statements(new, [], [new], all_gene_set_imports_retired=False)] == []
    assert not any(table in rr.PROTECTED_TABLES for _, table, _, _ in statements)


def test_delete_batches_limits_commits_and_refuses_protected_tables():
    for table in ('dismech_discussions', 'dismech_mechanisms', 'dismech_imports', 'archived_reference_factors', 'reference_generations',
                  'embedding_spaces', 'reveal_records'):
        with pytest.raises(rr.Refused, match='protected'): rr.delete_batches(FakeDB(), table, '1=1')
    counts = iter([10000, 10000, 7])
    db = FakeDB(rules=[(r'^DELETE FROM', lambda sql, params: next(counts))])
    assert rr.delete_batches(db, 'eaggl_genes', 'import_id IN (%s)', ('5' * 64,)) == 20007
    assert [sql for sql, _ in db.log] == ['DELETE FROM eaggl_genes WHERE import_id IN (%s) LIMIT 10000'] * 3 and db.commits == 3


def test_purge_plan_gates_then_deletes_in_order_and_only_stale_namespaces(tmp_path, monkeypatch):
    (tmp_path / 'targets.yaml').write_text(TARGETS.split('  prod:')[0])
    calls = []
    services, db, archive, repo = cutover_services(tmp_path, calls=calls)
    old = generation(LEGACY, rg.LEGACY_KIND, 'superseded', legacy_mapping_run_id='a' * 64, legacy_gene_set_import_id='2' * 64)
    db.generations[LEGACY] = old
    db.rules += [(r'^SELECT COUNT\(\*\) FROM', [(5,)]), (r'SELECT import_id FROM gene_set_imports', [('2' * 64,)]),
                 (r'^DELETE FROM', lambda sql, params: 3)]
    monkeypatch.setattr(rr, 'verify_target', lambda *a, **k: {'passed': True, 'checks': {}})
    services.artifact_exists = lambda ref: True
    plan = rr.build_purge_plan(services, GEN)
    assert any('not active' in b for b in plan['blockers']) and any('no cold export' in b for b in plan['blockers'])
    # Satisfy the gates: activate, record a verified audit, cold-export the retired generation.
    with repo.transaction() as tx:
        rg.write_active(tx, GEN, rg.KPN_MODEL, expected_previous=None, vector_snapshot_id=NEW_SNAPSHOT)
        tx.put('vector_active', ENV_NAME, 'catalog', {'snapshot_id': NEW_SNAPSHOT})
        tx.put(rg.RELOAD_KIND, 'audit', rg.CATALOG_OWNER, {'generation_id': GEN, 'verification': {'passed': True}})
        tx.put('suggestion', 's1', 'catalog', {'x': 1})
        tx.put('vector_batch', digest([OLD_SNAPSHOT, 'factors:0']), 'catalog', {'snapshot_id': OLD_SNAPSHOT, 'batch': 'factors:0', 'checksums': {}})
        tx.put('vector_batch', digest([NEW_SNAPSHOT, 'factors:0']), 'catalog', {'snapshot_id': NEW_SNAPSHOT, 'batch': 'factors:0', 'checksums': {}})
    old['cold_export_ref'] = {'root': {'store': 'filesystem', 'path': '/x', 'sha256': '0' * 64}}
    part = {'store': 'filesystem', 'path': '/x/part', 'sha256': '1' * 64}
    services.read_artifact = lambda ref: gzip.compress(json.dumps({'tables': {'eaggl_cfde_link_runs': {'rows': 1, 'parts': [part]}}}).encode())
    assert any('cold export predates ' + rr.EXPORT_FORMAT in b for b in rr.build_purge_plan(services, GEN)['blockers'])
    old['cold_export_ref']['format'] = rr.EXPORT_FORMAT
    services.artifact_exists = lambda ref: ref != part
    assert any('cold export is missing or incomplete' in b for b in rr.build_purge_plan(services, GEN)['blockers'])
    services.artifact_exists = lambda ref: True
    # Another complete mapping run on the retiring gene-set import would break steps 3-4 on foreign keys.
    db.rules.insert(0, (r'^SELECT run_id FROM eaggl_cfde_link_runs WHERE', lambda sql, params: [('a' * 64,), ('9' * 64,)]))
    assert [b for b in rr.build_purge_plan(services, GEN)['blockers'] if 'Unregistered' in b] == [
        "Unregistered EAGGL mapping runs reference retiring imports: ['999999999999']; no cold export holds them: export and remove them by hand before purging"]
    stray = db.statements(r'^SELECT run_id FROM eaggl_cfde_link_runs WHERE')[-1]
    assert stray[0].endswith('WHERE gene_set_import_id IN (%s) ORDER BY run_id') and stray[1] == ('2' * 64,)
    db.rules.pop(0)
    plan = rr.build_purge_plan(services, GEN)
    assert plan['blockers'] == [] and plan['retired'] == [LEGACY]
    assert plan['namespace_deletes'] == {ENV_NAME: [f'{ENV_NAME}-f-' + 'd' * 48]}  # never active, never another env
    assert plan['observed']['records'] == {PREFIX: {'suggestion': 1, 'vector_snapshot': 1, 'vector_batch': 1}}
    # Users keep creating suggestions: step 6 drops them all at purge time, so their count never drifts the plan.
    with repo.transaction() as tx: tx.put('suggestion', 's2', 'catalog', {'x': 2})
    assert rr.build_purge_plan(services, GEN)['plan_sha256'] == plan['plan_sha256']
    path = rr.write_json(tmp_path / 'purge-plan.json', plan)
    approval = approve(services, path)
    db.log.clear()
    result = rr.purge_retired(services, path, approval['approval'], client=Upstash())
    deletes = [re.match(r'DELETE FROM (\w+)', sql)[1] for sql, _ in db.log if sql.startswith('DELETE FROM')]
    assert deletes == ['eaggl_cfde_gene_set_links', 'eaggl_cfde_factor_links', 'eaggl_cfde_link_runs', 'dismech_embedding_inputs',
                       'dismech_embedding_vectors', 'dismech_embedding_runs', 'cfde_gene_set_aliases', 'gene_set_imports',
                       'dapper_objects', 'dapper_objects', 'vector_bindings']
    assert all(sql.endswith('LIMIT 10000') for sql, _ in db.log if sql.startswith('DELETE FROM'))
    assert ('delete_namespace', f'{ENV_NAME}-f-' + 'd' * 48, ENV_NAME) in calls and len([c for c in calls if c[0] == 'delete_namespace']) == 1
    assert db.generations[LEGACY]['status'] == 'retired' and result['retired'] == [LEGACY]
    with repo.read_transaction() as tx:
        assert tx.get('suggestion', 's1') is None and tx.get('suggestion', 's2') is None and tx.get('vector_snapshot', OLD_SNAPSHOT) is None
        assert tx.get('vector_snapshot', NEW_SNAPSHOT) and tx.get('vector_batch', digest([NEW_SNAPSHOT, 'factors:0']))
        assert tx.get(rg.RELOAD_KIND, digest([plan['plan_sha256']]))['data']['status'] == 'complete'


# --------------------------------------------------------------------------------------
# capture / status / CLI


@pytest.mark.parametrize('do_apply', [False, True])
def test_capture_supplements_only_missing_factors_from_verified_retained_fixture(tmp_path, do_apply):
    services, db, archive, repo = cutover_services(tmp_path)
    imported, external, unavailable = 'source:imported', 'source:external', 'source:unavailable'
    imported_row, retained_row = {'source_id': imported, 'origin': 'import'}, {'source_id': external, 'origin': 'fixture_capture'}
    archive.referenced_sources = lambda repository: {LEGACY: {imported, external, unavailable}}
    archive.capture_factors = lambda *args, **kwargs: [dict(imported_row)]
    reads = []
    def retained(repository, generation, source_ids, *, read_artifact):
        assert generation['generation_id'] == LEGACY and read_artifact == services.read_artifact
        reads.append((repository.table_prefix, source_ids))
        assert imported not in source_ids  # An external capture can never replace an imported row.
        return [dict(retained_row)] if external in source_ids else []
    archive.capture_fixture_factors = retained
    written = []
    def write(connection, rows): written.extend(rows); return len(rows)
    archive.write_archived_factors = write
    result = rr.capture_generation(services, LEGACY, [PREFIX, 'reveal'], apply=do_apply, dapper=None)
    assert reads == [(PREFIX, [external, unavailable]), ('reveal', [unavailable])]
    assert result['sources'] == 3 and result['captured'] == 2 and result['unresolved'] == [unavailable]
    assert result['written'] == (2 if do_apply else 0)
    assert written == ([imported_row, retained_row] if do_apply else [])


def test_capture_freezes_referenced_factors_and_writes_a_cold_export(tmp_path):
    services, db, archive, repo = cutover_services(tmp_path)
    db.generations[LEGACY]['legacy_gene_set_import_id'] = '2' * 64
    db.rules[:0] = [(r'^SELECT \* FROM eaggl_cfde_link_runs', {'rows': [('a' * 64, b'\x00\x01')], 'description': [('run_id',), ('blob',)]}),
                    (r'^SELECT \* FROM', {'rows': [], 'description': [('x',)]})]
    dry = rr.capture_generation(services, LEGACY, [PREFIX], apply=False, cold=True, dapper=None)
    assert dry['captured'] == 1 and dry['written'] == 0 and dry['cold_export'] == 'pending (--apply)'
    report = rr.capture_generation(services, LEGACY, [PREFIX], apply=True, cold=True, export_dir=tmp_path / 'exports', dapper=None)
    assert report['written'] == 1 and report['anchor_display'] == {PREFIX: {'backfilled': 0}}
    assert ('backfill', PREFIX, True) in archive.calls
    ref = report['cold_export']
    root = json.loads(gzip.decompress(open(ref['root']['path'], 'rb').read()))
    assert root['generation_id'] == LEGACY and root['tables']['eaggl_cfde_link_runs']['rows'] == 1 and ref['format'] == rr.EXPORT_FORMAT
    # Everything purge-retired deletes for a retired legacy generation is exported first.
    assert {'dapper_objects', 'eaggl_embedding_runs', 'eaggl_name_embeddings', 'dismech_embedding_runs', 'dismech_embedding_vectors',
            'dismech_embedding_inputs'} <= set(root['tables'])
    selects = {re.match(r'SELECT \* FROM (\w+)', sql)[1]: (sql, params) for sql, params in db.log if sql.startswith('SELECT * FROM')}
    assert "class_name='Activity'" in selects['dapper_objects'][0] and 'NOT EXISTS' in selects['dapper_objects'][0]
    assert selects['dismech_embedding_runs'][1][0] == 'e' * 64 and 'import_id=%s' in selects['dismech_embedding_runs'][0]
    part = json.loads(gzip.decompress(open(root['tables']['eaggl_cfde_link_runs']['parts'][0]['path'], 'rb').read()))
    assert part['row'] == {'run_id': 'a' * 64, 'blob': {'$base64': 'AAE='}}
    update = db.statements(r'^UPDATE reference_generations SET cold_export_ref')
    assert update and json.loads(update[0][1][0])['root']['sha256'] == ref['root']['sha256']


def test_capture_from_active_resolves_the_served_generation_or_refuses(tmp_path, capsys):
    services, db, archive, repo = cutover_services(tmp_path)
    other = services.repository('reveal')
    # Legacy mode everywhere: `active` is the registered legacy generation.
    assert rr.active_source(services, [PREFIX, 'reveal']) == LEGACY
    assert rr.main(['capture', '--from', 'active', '--prefixes', PREFIX], services=services) == 0
    assert json.loads(capsys.readouterr().out)['generation_id'] == LEGACY
    with repo.transaction() as tx: rg.write_active(tx, GEN, rg.KPN_MODEL, expected_previous=None)
    with pytest.raises(rr.Refused, match='different generations'): rr.active_source(services, [PREFIX, 'reveal'])
    with other.transaction() as tx: rg.write_active(tx, GEN, rg.KPN_MODEL, expected_previous=None)
    assert rr.active_source(services, [PREFIX, 'reveal']) == GEN
    db.generations.pop(LEGACY)
    assert rr.active_source(services, [PREFIX]) == GEN  # no legacy lookup once every prefix is active
    with other.transaction() as tx: tx.remove(rg.ACTIVE_KIND, rg.ACTIVE_ID)
    with pytest.raises(rr.Refused, match='no registered legacy generation'): rr.active_source(services, ['reveal'])


def test_status_and_cli_print_one_json_result(tmp_path, capsys):
    services, db, archive, repo = cutover_services(tmp_path)
    assert rr.main(['status'], services=services) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['command'] == 'status' and {g['generation_id'] for g in result['generations']} == {GEN, LEGACY}
    assert result['targets']['rehearsal']['mode'] == 'legacy' and result['targets']['rehearsal']['vector_active']['snapshot_id'] == OLD_SNAPSHOT
    assert rr.main(['gate', '--target', 'rehearsal', '--close', '--reason', 'maintenance'], services=services) == 0
    assert json.loads(capsys.readouterr().out)['gate']['closed'] is True
    with repo.read_transaction() as tx: assert rg.read_gate(tx)['reason'] == 'maintenance'
    assert rr.main(['verify', '--target', 'staging'], services=services) == 2
    assert 'not allow-listed' in json.loads(capsys.readouterr().out)['refused']


class Vectors:
    """Fake Upstash index: one vector per id, cosine-free scores (equal vectors tie)."""
    def __init__(self, namespaces): self.namespaces = namespaces
    def range(self, *, cursor, limit, namespace):
        return SimpleNamespace(vectors=[SimpleNamespace(id=i) for i in self.namespaces.get(namespace, {})], next_cursor='')
    def fetch(self, *, ids, namespace, include_vectors):
        return [SimpleNamespace(id=i, vector=self.namespaces[namespace][i]) for i in ids]
    def query(self, *, vector, top_k, namespace):
        rows = [SimpleNamespace(id=i, score=float(np.dot(v, vector))) for i, v in self.namespaces[namespace].items()]
        return sorted(rows, key=lambda row: (-row.score, row.id))[:top_k]


def test_gap_verification_resolves_canonical_only_fixture_against_pinned_import(tmp_path):
    services = fake_services(tmp_path)
    fixture = rr.ROOT / 'data/fixtures/bubble-account-v1'
    source = rr.read_json(fixture / 'sources/dismech-gap.source.json')
    identity = rr.read_json(fixture / 'scientific-account.json')['knowledge_gaps'][0]['id']
    dismech_import_id = '1' * 64
    db = FakeDB(rules=[
        (r'^SELECT JSON_UNQUOTE.*FROM dismech_discussions', [(source['id'],)]),
        (r'^SELECT payload FROM dismech_discussions', [(json.dumps(source),)]),
    ])
    gap = {'id': identity, 'source_id': None, 'source_revision': None}
    # Actual pinned DAPPER runtime: scope, rationale and expanded MONDO term must match the catalog.
    assert rr.unresolved_gaps(services, db, dismech_import_id, {'account': {'fixture': {'gap': gap}}}) == []
    assert all('import_id=%s AND is_gap=1' in sql and params == (dismech_import_id,) for sql, params in db.log)
    bad = {'account': {
        'missing_canonical': {'gap': {**gap, 'id': 'dapper:KnowledgeGap.missing'}},
        'bad_source': {'gap': {**gap, 'source_id': 'dismech:missing'}},
        'missing_identity': {'gap': {'id': None, 'source_id': None}},
        'empty_gap': {'gap': {}},
        'not_bound_to_a_gap': {'gap': None},
    }}
    assert rr.unresolved_gaps(services, db, dismech_import_id, bad) == [
        '(missing gap identity)', 'dapper:KnowledgeGap.missing', 'dismech:missing']
    # A gap from a different payload/import cannot be accepted just because its id is well formed.
    changed = {**source, 'raw': {**source['raw'], 'prompt': 'A different scientific question'}}
    db.rules[1] = (r'^SELECT payload FROM dismech_discussions', [(json.dumps(changed),)])
    assert rr.unresolved_gaps(services, db, dismech_import_id, {'account': {'fixture': {'gap': gap}}}) == [identity]
    assert rr.unresolved_gaps(services, db, None, {'account': {'fixture': {'gap': gap}}}) == [identity]


def test_gap_verification_keeps_source_path_strict_without_loading_canonical_runtime(tmp_path):
    services = fake_services(tmp_path)
    def no_runtime(*args): raise AssertionError('Source-backed gaps do not need canonical reconstruction')
    services.dapper_runtime = no_runtime
    db = FakeDB(rules=[(r'FROM dismech_discussions', [('gap-1',)])])
    stamps = {'request': {'r1': {'gap': {'id': 'kg', 'source_id': 'gap-1'}}}}
    assert rr.unresolved_gaps(services, db, '1' * 64, stamps) == []
    stamps['request']['r2'] = {'gap': {'id': 'kg', 'source_id': 'gap-missing'}}
    assert rr.unresolved_gaps(services, db, '1' * 64, stamps) == ['gap-missing']


def test_verify_checks_pointers_counts_inventory_neighbours_and_archive(tmp_path):
    services, db, archive, repo = cutover_services(tmp_path)
    factor_ns, context_ns = f'{ENV_NAME}-eaggl-factor-' + 'c' * 24, f'{ENV_NAME}-dismech-context-' + 'c' * 24
    counts = {'kpn_traits': 2, 'reference_factors': 3, 'cfde_collections': 2, 'cfde_gene_sets': 3, 'projections': 9}
    db.generations[GEN]['manifest'] = {'counts': counts, 'vectors': {'count': 5}}
    tables = dict(rr.COUNT_TABLES)
    by_table = {tables[name]: n for name, n in {**counts, 'reference_vectors': 5}.items()}
    archived = [('z' * 64,)]
    db.rules[:0] = [(r'SELECT COUNT\(\*\) FROM (\w+) WHERE generation_id=%s$', lambda sql, p: [(by_table[re.search(r'FROM (\w+)', sql)[1]],)]),
                    (r'LEFT JOIN kpn_traits', [(0,)]),
                    (r'FROM vector_bindings WHERE environment', lambda sql, p: [(i,) for i in ({'x', 'x2'} if p[1] == factor_ns else {'ctx'})]),
                    (r'FROM archived_reference_factors', lambda sql, p: archived),
                    (r'FROM dismech_discussions', [('gap-1',)])]
    with repo.transaction() as tx:
        state = tx.get('vector_snapshot', NEW_SNAPSHOT)['data']
        tx.put('vector_snapshot', NEW_SNAPSHOT, 'catalog', dict(state, contexts=[{'id': 'ctx'}]))
        rg.write_active(tx, GEN, rg.KPN_MODEL, expected_previous=None, vector_snapshot_id=NEW_SNAPSHOT)
        tx.put('vector_active', ENV_NAME, 'catalog', {'snapshot_id': NEW_SNAPSHOT})
        stamp = rg.build_stamp(LEGACY, GEN, reference={'model': rg.LEGACY_MODEL, 'anchors': [{'archived_reference_factor_id': 'z' * 64}]},
                               gap={'id': 'kg', 'source_id': 'gap-1', 'source_revision': 'r'}, analysis={'account_id': 'a1'})
        tx.put('account', 'a1', 'u1', {'summary': {'archive': stamp}, 'result': {'root_id': 'root'}})
    archive.prefix.update(archive_candidates={}, anchored_drafts=[])
    client = Vectors({factor_ns: {'x': [1.0, 0.0], 'x2': [1.0, 0.0]}, context_ns: {'ctx': [0.9, 0.1]}})
    services.module = (lambda original: lambda name: SimpleNamespace(UpstashFactorIndex=lambda snapshot, client: SimpleNamespace(check=lambda: None),
                       value=rr.importlib.import_module('reveal_backend.vector_retrieval').value) if name == 'vector_retrieval' else original(name))(services.module)
    target = rr.select_target(services, 'rehearsal')
    result = rr.verify_target(services, target, client=client)
    assert result['passed'] is True, {k: v for k, v in result['checks'].items() if not v['passed']}
    assert result['checks']['self_neighbour']['top'] == ['x', 'x2']  # a tied label vector still counts as itself
    archived.clear()
    archive.prefix['anchored_drafts'] = ['d-left']
    result = rr.verify_target(services, target, client=client)
    failed = {k for k, v in result['checks'].items() if not v['passed']}
    assert failed == {'archived_factors', 'archive_complete'} and result['ok'] is False
    with repo.transaction() as tx: tx.remove(rg.ACTIVE_KIND, rg.ACTIVE_ID)
    assert rr.verify_target(services, target, client=client)['checks'] == {'active': {'passed': False, 'detail': 'legacy mode: no reference_active'}}
