"""Provenance survives streaming, exact dependency hydration, and additive historical recovery."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from reveal_backend import provenance_supplements as ps, reference_reload as rr
from reveal_backend.artifact_store import StorageUnavailable
from reveal_backend.evidence_package import DapperRuntime, EvidenceBuildError
from reveal_backend.evidence_schema import load_generated_schema
from reveal_backend.reference_evidence import _resolve_gene_set, _class_groups
from reveal_backend.reference_archive import classify, KEEP
from reveal_backend.repository import Repository, canonical
from reveal_backend.runtime_config import CURRENT_DAPPER_SNAPSHOT, ROOT

GEN = 'a' * 64
COLLECTION = 'dapper:GeneSetCollection.' + 'c' * 32


@pytest.fixture
def provenance():
    runtime = DapperRuntime(CURRENT_DAPPER_SNAPSHOT)
    def node(cls, **fields):
        return {**fields, 'id': runtime.compute_id(fields, cls, runtime.schema)}
    org = node('Organization', name='Consortium')
    dataset = node('Dataset', name='Expression source')
    mapping = node('Dataset', name='Gene mapping')
    activity = node('Activity', name='Gene set extraction')
    gene_set = node('GeneSet', name='Selected genes', member_type='gene', members=['HGNC.SYMBOL:UBA1'], was_generated_by=activity['id'])
    graph = {'prefixes': {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:'},
        'organizations': [org], 'datasets': [dataset, mapping], 'activities': [activity],
        'used_edges': [{'subject': activity['id'], 'predicate': 'prov:used', 'object': dataset['id'], 'edge_role': 'data_input'},
                       {'subject': activity['id'], 'predicate': 'prov:used', 'object': mapping['id'], 'edge_role': 'metadata_input'}],
        'has_creator_edges': [{'subject': dataset['id'], 'predicate': 'schema:creator', 'object': org['id']}]}
    return runtime, gene_set, graph


def source_document(path, gene_set, graph):
    # Activity nodes precede gene_sets, edges and attribution come after it.
    source = {'prefixes': graph['prefixes'], 'activities': graph['activities'],
        'gene_sets': [gene_set], 'gene_set_collections': [{'id': COLLECTION, 'n_sets': 1}],
        **{k: v for k, v in graph.items() if k not in ('prefixes', 'activities')}}
    path.write_text(yaml.safe_dump(source, sort_keys=False))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repository(tmp_path, path, sha):
    repo = Repository(sqlite_path=str(tmp_path / 'recovery.sqlite')); repo.migrate()
    with sqlite3.connect(repo.sqlite_path) as db:
        db.executescript('CREATE TABLE reference_generations(generation_id TEXT,manifest TEXT); '
            'CREATE TABLE cfde_gene_set_collections(generation_id TEXT,collection_id TEXT,cfde_label TEXT,payload TEXT);')
        db.execute('INSERT INTO reference_generations VALUES (?,?)', (GEN, canonical({'collections': {'TEST': sha}})))
        db.execute('INSERT INTO cfde_gene_set_collections VALUES (?,?,?,?)',
            (GEN, COLLECTION, 'TEST', canonical({'document_sha256': sha, 'provenance': {'activities': []}})))
    return repo


def test_streams_edges_after_gene_sets_and_skips_large_unwanted_sections(tmp_path, provenance):
    runtime, node, graph = provenance
    path = tmp_path / 'source.yaml'; source_document(path, node, graph)
    original = path.read_text().replace('gene_sets:\n', 'embeddings:\n- {unparsed garbage\ngene_sets:\n')
    path.write_text(original)
    header, collection = rr.collection_header(path)
    assert collection['id'] == COLLECTION
    assert 'gene_sets' not in header and 'embeddings' not in header
    assert header['used_edges'] == graph['used_edges']
    assert list(rr.collection_gene_sets(path)) == [node]
    with pytest.raises(rr.Refused, match='exceeds'): rr.collection_header(path, limit=10)
    path.write_text(original + '\nused_edges: []\n')
    with pytest.raises(rr.Refused, match='duplicate'): rr.collection_header(path)


def test_preserves_edges_dependencies_roles_and_original_id(provenance):
    runtime, node, graph = provenance
    before = deepcopy(node); edges = {}
    exact, deps, prefixes, reason = _resolve_gene_set(runtime,
        {'gene_set_id': node['id'], 'metadata': {'dapper_gene_set': node}}, {'provenance': graph}, {},
        _class_groups(runtime), edge_groups=edges)
    assert reason is None and exact == before and node == before
    assert {item['id'] for item in deps} == {item['id'] for group in ('activities', 'datasets', 'organizations') for item in graph[group]}
    assert edges == {group: graph[group] for group in ('used_edges', 'has_creator_edges')}
    hydrated = {**graph, 'gene_sets': [exact]}
    assert runtime.validate(hydrated)[node['id']] == before
    projected, errors = runtime.transform(hydrated, runtime.schema, runtime.groups, compact_dapper=True)
    assert not errors
    assert projected['used_edges'] == hydrated['used_edges']
    # Runtime validation and context schema accept anonymous canonical DAPPER edges.
    schema = load_generated_schema(ROOT / 'schema/evidence-package.schema.json')
    import jsonschema
    jsonschema.Draft202012Validator({'$ref': '#/$defs/EPDapperContext', '$defs': schema['$defs']}).validate(hydrated)
    tampered = deepcopy(hydrated); tampered['used_edges'][0]['predicate'] = 'prov:wasDerivedFrom'
    with pytest.raises(EvidenceBuildError, match='predicate'): runtime.validate(tampered)


def test_missing_and_conflicting_edge_dependencies_are_explicit(provenance):
    runtime, node, graph = provenance
    for change in ('missing', 'conflicting'):
        bad = deepcopy(graph)
        if change == 'missing': bad['organizations'] = []
        else: bad['organizations'].append({**bad['organizations'][0], 'name': 'Conflicting'})
        result = _resolve_gene_set(runtime, {'gene_set_id': node['id'], 'metadata': {'dapper_gene_set': node}},
                                  {'provenance': bad}, {}, _class_groups(runtime))
        assert result[0] is None and result[3]


def test_recovery_dry_run_apply_idempotence_and_immutable_scientific_rows(tmp_path, monkeypatch, provenance):
    runtime, node, graph = provenance
    path = tmp_path / 'source.yaml'; sha = source_document(path, node, graph)
    monkeypatch.setenv('REVEAL_ARTIFACT_STORE', 'filesystem'); monkeypatch.setenv('REVEAL_ARTIFACTS_DIR', str(tmp_path / 'artifacts'))
    repo = repository(tmp_path, path, sha)
    with sqlite3.connect(repo.sqlite_path) as db:
        before = list(db.iterdump())
    result = ps.recover_collection_provenance(repo, GEN, path)
    assert not result['applied'] and not (tmp_path / 'artifacts').exists()
    with sqlite3.connect(repo.sqlite_path) as db: assert list(db.iterdump()) == before
    result = ps.recover_collection_provenance(repo, GEN, path, apply=True)
    assert result['applied'] and not result['reused']
    with repo.read_transaction() as tx:
        restored = ps.load_supplement(tx, GEN, COLLECTION, sha)
        assert restored['graph'] == graph
        version = tx.get(ps.KIND, result['id'])['version']
    repeated = ps.recover_collection_provenance(repo, GEN, path, apply=True)
    assert repeated['reused']
    with repo.read_transaction() as tx: assert tx.get(ps.KIND, result['id'])['version'] == version
    with sqlite3.connect(repo.sqlite_path) as db:
        assert db.execute('SELECT manifest FROM reference_generations').fetchone()[0] == canonical({'collections': {'TEST': sha}})
        assert db.execute('SELECT payload FROM cfde_gene_set_collections').fetchone()[0] == canonical({'document_sha256': sha, 'provenance': {'activities': []}})
    assert classify(ps.KIND) == KEEP
    Path(restored['registry']['storage']['key']).unlink()
    with repo.read_transaction() as tx, pytest.raises(StorageUnavailable): ps.load_supplement(tx, GEN, COLLECTION, sha)


@pytest.mark.parametrize('mismatch', ['collection', 'manifest'])
def test_recovery_requires_both_original_checksum_bindings(tmp_path, monkeypatch, provenance, mismatch):
    runtime, node, graph = provenance
    path = tmp_path / 'source.yaml'; sha = source_document(path, node, graph)
    monkeypatch.setenv('REVEAL_ARTIFACTS_DIR', str(tmp_path / 'artifacts'))
    repo = repository(tmp_path, path, sha)
    with sqlite3.connect(repo.sqlite_path) as db:
        if mismatch == 'collection': db.execute('UPDATE cfde_gene_set_collections SET payload=?', (canonical({'document_sha256': 'b' * 64}),))
        else: db.execute('UPDATE reference_generations SET manifest=?', (canonical({'collections': {'TEST': 'b' * 64}}),))
    with pytest.raises(ps.RecoveryRefused, match=mismatch): ps.recover_collection_provenance(repo, GEN, path, apply=True)
    assert not (tmp_path / 'artifacts').exists()


def test_conflicting_registry_and_corrupt_artifacts_never_look_missing(tmp_path, monkeypatch, provenance):
    runtime, node, graph = provenance
    path = tmp_path / 'source.yaml'; sha = source_document(path, node, graph)
    monkeypatch.setenv('REVEAL_ARTIFACT_STORE', 'filesystem'); monkeypatch.setenv('REVEAL_ARTIFACTS_DIR', str(tmp_path / 'artifacts'))
    repo = repository(tmp_path, path, sha)
    ps.recover_collection_provenance(repo, GEN, path, apply=True)
    with repo.read_transaction() as tx:
        restored = ps.load_supplement(tx, GEN, COLLECTION, sha)
        assert ps.load_supplement(tx, GEN, COLLECTION, 'b' * 64) is None
    Path(restored['registry']['storage']['key']).write_text('{}')
    with repo.read_transaction() as tx, pytest.raises(StorageUnavailable): ps.load_supplement(tx, GEN, COLLECTION, sha)


def test_recovery_rejects_a_conflicting_but_valid_registered_graph(tmp_path, monkeypatch, provenance):
    runtime, node, graph = provenance
    path = tmp_path / 'source.yaml'; sha = source_document(path, node, graph)
    monkeypatch.setenv('REVEAL_ARTIFACT_STORE', 'filesystem'); monkeypatch.setenv('REVEAL_ARTIFACTS_DIR', str(tmp_path / 'artifacts'))
    repo = repository(tmp_path, path, sha)
    result = ps.recover_collection_provenance(repo, GEN, path, apply=True)
    different = ps._storage_write(canonical({'prefixes': {}}).encode())
    with repo.transaction() as tx:
        row = tx.get(ps.KIND, result['id'])
        row['data'].update(storage=different, graph_sha256=different['sha256'])
        tx.put(ps.KIND, result['id'], row['owner'], row['data'])
    with pytest.raises(ps.RecoveryRefused, match='Conflicting registered'):
        ps.recover_collection_provenance(repo, GEN, path, apply=True)
    with repo.read_transaction() as tx:
        assert tx.get(ps.KIND, result['id'])['data']['graph_sha256'] == different['sha256']


def test_filesystem_artifact_publication_is_atomic_and_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv('REVEAL_ARTIFACT_STORE', 'filesystem')
    monkeypatch.setenv('REVEAL_ARTIFACTS_DIR', str(tmp_path / 'artifacts'))
    data = canonical({'datasets': []}).encode()
    original_link = ps.os.link
    def fail_link(*args):
        raise OSError('simulated publication failure')
    monkeypatch.setattr(ps.os, 'link', fail_link)
    with pytest.raises(OSError, match='simulated'): ps._storage_write(data)
    directory = tmp_path / 'artifacts' / 'provenance-supplements'
    assert list(directory.iterdir()) == []
    monkeypatch.setattr(ps.os, 'link', original_link)
    ref = ps._storage_write(data)
    assert ps._storage_read(ref) == data and ps._storage_write(data) == ref
    assert [p.name for p in directory.iterdir()] == [ref['sha256']]


@pytest.mark.parametrize('membership', ['native', 'edge'])
def test_resolver_hydrates_file_dataset_membership_without_reverse_sibling_expansion(provenance, membership):
    runtime, original, graph = provenance
    def node(cls, **fields):
        return {**fields, 'id': runtime.compute_id(fields, cls, runtime.schema)}
    used_file = node('File', filename='used.tsv')
    sibling = node('File', filename='sibling.tsv')
    dataset = node('Dataset', name='Parent', has_creator=[graph['organizations'][0]['id']],
                   **({'has_file': [used_file['id'], sibling['id']]} if membership == 'native' else {}))
    unrelated = node('Dataset', name='Sibling parent', has_file=[sibling['id']])
    graph['files'] = [used_file, sibling]
    graph['datasets'] = [dataset, unrelated]
    graph['used_edges'] = [{'subject': graph['activities'][0]['id'], 'predicate': 'prov:used', 'object': used_file['id']}]
    graph.pop('has_creator_edges')
    if membership == 'edge':
        graph['has_file_edges'] = [{'subject': dataset['id'], 'predicate': 'dapper:hasFile', 'object': used_file['id']},
                                  {'subject': unrelated['id'], 'predicate': 'dapper:hasFile', 'object': sibling['id']}]
    edges = {}
    exact, dependencies, prefixes, reason = _resolve_gene_set(runtime,
        {'gene_set_id': original['id'], 'metadata': {'dapper_gene_set': original}}, {'provenance': graph}, {},
        _class_groups(runtime), edge_groups=edges)
    assert reason is None and exact == original
    by_id = {item['id']: item for item in dependencies}
    assert by_id[dataset['id']] == dataset and graph['organizations'][0]['id'] in by_id
    assert unrelated['id'] not in by_id
    if membership == 'native': assert by_id[sibling['id']] == sibling
    else: assert edges['has_file_edges'] == [graph['has_file_edges'][0]]
