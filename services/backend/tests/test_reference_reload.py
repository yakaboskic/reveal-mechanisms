"""Protected reference reload CLI: build determinism, protections, apply order and purge order.

No network or MySQL: shared tables are a scripted fake DB-API connection, application
records are SQLite repositories, and reference_archive / Upstash / RDS / mysqldump are fakes.
"""
import csv
import gzip
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


def load_db(bundle, *, eaggl=True):
    factors = list(rr.read_jsonl(bundle / 'reference_factors.jsonl.gz'))
    inserted = {}
    def many(sql, rows):
        table = re.match(r'MANY INSERT INTO (\w+)', sql)
        if table: inserted[table[1]] = inserted.get(table[1], 0) + len(rows)
    def insert_generation(sql, params):
        db.generations[params[0]] = generation(params[0], params[1], 'loading', model=params[2], manifest=json.loads(params[6]))
        return 1
    def count(sql, params):
        table = re.search(r'FROM (\w+) WHERE generation_id', sql)[1]
        return [(inserted.get(table, 0),)]
    db = FakeDB(rules=[
        (r'^MANY ', many),
        (r'FROM eaggl_cfde_link_runs', [('a' * 64, 'e' * 64, '2' * 64)]),
        (r'FROM eaggl_embedding_runs WHERE import_id', [('f' * 64,)]),
        (r'FROM dismech_imports', [('1' * 64,)]),
        (r'FROM eaggl_imports WHERE', [('e' * 64, 'complete', rr.DEFAULT_EAGGL_SOURCE_VERSION)] if eaggl else []),
        (r'SELECT factor_id,input_sha256 FROM eaggl_factors', [(f['eaggl_factor_id'], f['input_sha256']) for f in factors]),
        (r'SELECT input_sha256 FROM eaggl_name_embeddings', [(f['input_sha256'],) for f in factors]),
        (r'^INSERT INTO reference_generations', insert_generation),
        (r'LEFT JOIN kpn_traits', [(0,)]),
        (r'SELECT COUNT\(\*\) FROM \w+ WHERE generation_id=%s', count),
        (r'^SHOW WARNINGS', [])])
    return db


def test_load_dry_run_writes_nothing_and_apply_inserts_generation(tmp_path):
    bundle, gen = built_bundle(tmp_path)
    db = load_db(bundle)
    services = fake_services(tmp_path, connect=lambda: db)
    dry = rr.load_bundle(services, bundle, apply=False)
    assert dry['eaggl']['eaggl_import_id'] == 'e' * 64 and dry['expected']['reference_vectors'] == 5
    assert not [sql for sql, _ in db.log if re.match(r'(INSERT|UPDATE|DELETE|CREATE)', sql)] and db.commits == 0
    result = rr.load_bundle(services, bundle, apply=True)
    assert result['status'] == 'complete' and result['counts'] == {'kpn_traits': 2, 'reference_factors': 3, 'cfde_collections': 2,
                                                                  'cfde_gene_sets': 3, 'projections': 9, 'reference_vectors': 5}
    assert result['legacy_generation_id'] == LEGACY and db.statements(r'^CREATE TABLE IF NOT EXISTS reference_generations')
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
    with pytest.raises(rr.Refused, match='import_eaggl_factors.py'):
        rr.load_bundle(fake_services(tmp_path, connect=lambda: db), bundle, apply=True)
    assert gen not in db.generations
    db = load_db(bundle)
    db.rules.insert(0, (r'SELECT COUNT\(\*\) FROM reference_vectors WHERE generation_id', [(4,)]))
    with pytest.raises(rr.Refused, match='differ from the bundle'):
        rr.load_bundle(fake_services(tmp_path, connect=lambda: db), bundle, apply=True)
    assert db.generations[gen]['status'] == 'failed'


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
