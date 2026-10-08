"""Reference release: build a release folder from a synthetic LAP project, publish it per environment.

No network or MySQL: the embedder, the Upstash index and the MySQL connection are fakes; the
reference_release record is written to a SQLite repository.
"""
from datetime import datetime, timezone
import gzip
import hashlib
import io
from itertools import groupby
import json
import os
import re
import shutil
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from reveal_backend import redis_notifications, workspace_events
from reveal_backend import reference_generation as rg
from reveal_backend import reference_release as rr
from reveal_backend.repository import Repository, application_sql, canonical, digest
from reveal_backend.runtime_config import ROOT

KPN_A, KPN_B = 'KPN.TRAIT:0000398', 'KPN.TRAIT:0000100'
GENES = ['GCK', 'INS', 'LEP', 'MC4R', 'PPARG']
COLL = ['dapper:GeneSetCollection.' + c * 31 + '1' for c in 'xy']
LIB_SETS = ['dapper:GeneSet.' + c * 31 + '1' for c in 'abcd']
OTHER_SETS = ['dapper:GeneSet.' + c * 31 + '1' for c in 'efg']
SETS = LIB_SETS + OTHER_SETS
LIBRARY = {**dict.fromkeys(LIB_SETS, 'LIB'), **dict.fromkeys(OTHER_SETS, 'OTHER')}
LABELS = {COLL[0]: 'LIB__part__HZ1', COLL[1]: 'OTHER__p__HZ1'}
TRAITS = [('T2D', KPN_A, 'Type 2 diabetes', 2), ('BMI', KPN_B, 'Body mass index', 1)]
FACTORS = [('T2D', KPN_A, 1, 'Insulin secretion '), ('T2D', KPN_A, 2, 'Beta cell'), ('BMI', KPN_B, 1, 'Adipogenesis')]
LOADINGS = {'T2D::Factor1': ['0.5', '0', '1.2e-05', '0.0', '0.25'], 'T2D::Factor2': ['0', '0.75', '0', '0.1', '0'],
            'BMI::Factor1': ['0.3', '0.3', '0', '0.9', '0']}
CONTEXTS = ['ctx one', 'ctx two', 'Beta cell']  # a context may share a factor label's text
# The LAP betas_ stage's per-trait gene-set betas: (gene set, library, beta_uncorrected, beta, avg_postp, library rank).
# BMI's PIGEAN run analyzed no gene set: a header-only file.
GENE_SET_STATS = {'T2D': [(LIB_SETS[0], 'LIB', '0.9', '0.8', '0.95', 1), (LIB_SETS[2], 'LIB', '0.2', '0.1', '0.3', 2),
                          (OTHER_SETS[1], 'OTHER', '0.05', '0.04', '0.06', 1)], 'BMI': []}
ORG = {'id': 'dapper:Organization.' + 'o' * 32, 'name': 'Broad Institute'}
SHARED_EDGE = {'subject': 'dapper:Activity.' + 'a' * 32, 'predicate': 'prov:used', 'object': 'dapper:File.' + 'f' * 32, 'edge_role': 'data_input'}
MODEL, DIMS = 'm/x', 8
ENVIRON = {'EMBEDDING_MODEL': MODEL, 'EMBEDDING_SERVICE_URL': 'https://embed.invalid/'}
CLOCK = datetime(2026, 10, 5, 12, 0, 0, 123456, tzinfo=timezone.utc)
RUNTIME = SimpleNamespace(schema='schema', compute_id=lambda node, cls, schema: f'dapper:{cls}.' + hashlib.sha256(canonical(node).encode()).hexdigest()[:32])
SNAPSHOT_KEYS = {'format', 'generation_id', 'model', 'source_id', 'factor_id', 'trait', 'kpn_trait_id', 'label', 'mechanism', 'metadata',
                 'top_genes', 'top_gene_sets', 'generation_manifest_sha256'}


@pytest.fixture(autouse=True)
def quiet_notifications(monkeypatch):
    monkeypatch.setattr(redis_notifications, 'publish', lambda channels: None)


# --------------------------------------------------------------------------------------
# Synthetic LAP project, CFDE snapshot vectors and vector cache


def tsv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.name.endswith('.gz') else open
    with opener(path, 'wt', encoding='utf-8', newline='') as out:
        out.write('\t'.join(columns) + '\n')
        for row in rows: out.write('\t'.join(str(row[c]) for c in columns) + '\n')


def ranks(factor_number, trait):
    """Global joint and marginal ranks (1..7) of every gene set for one factor: two different permutations."""
    shift = factor_number + (3 if trait == 'BMI' else 0)
    joint = SETS[shift:] + SETS[:shift]
    marginal = joint[1::2] + joint[0::2]
    return {gene_set: (joint.index(gene_set) + 1, marginal.index(gene_set) + 1) for gene_set in SETS}


def collection_document(collection, members):
    label = LABELS[collection]
    activity, file = f'dapper:Activity.{label[:5].lower():a<32}', f'dapper:File.{label[:5].lower():f<32}'
    embeddings = [{'id': f'dapper:Embedding.{node[-6:]:e<32}', 'embedding_of': node, 'embedding_model': MODEL, 'dimensions': DIMS}
                  for node in [collection] + members]
    document = {
        'prefixes': {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:'},
        'organizations': [ORG if collection == COLL[0] else {**ORG, 'name': 'Broad Institute (variant)'}],
        'datasets': [{'id': f'dapper:Dataset.{label[:5].lower():d<32}', 'name': f'{label} dataset'}],
        'files': [{'id': file, 'name': 'genesets.gmt', 'sha256': '0' * 64}],
        'activities': [{'id': activity, 'name': f'extract {label}', 'command': 'python -m extract --x 1'}],
        'gene_set_collections': [{'id': collection, 'name': label, 'n_sets': len(members), 'members': members, 'was_generated_by': activity}],
        'gene_sets': [{'id': node, 'name': f'set {node[-3:]}', 'member_type': 'gene', 'members': ['HGNC.SYMBOL:INS', 'HGNC.SYMBOL:GCK'],
                       'in_gene_set_collection': [collection]} for node in members],
        'used_edges': [SHARED_EDGE, {'subject': activity, 'predicate': 'prov:used', 'object': file, 'edge_role': 'metadata_input'}],
        'was_generated_by_edges': [{'subject': collection, 'predicate': 'prov:wasGeneratedBy', 'object': activity}],
        'was_derived_from_edges': [{'subject': file, 'predicate': 'prov:wasDerivedFrom', 'object': f'dapper:Dataset.{label[:5].lower():d<32}'}],
        'embeddings': embeddings,
        'has_embedding_edges': [{'subject': item['embedding_of'], 'predicate': 'dapper:hasEmbedding', 'object': item['id']} for item in embeddings]}
    if collection == COLL[1]:  # DAPPER 0.2.0 order: the collection follows its gene sets
        document = {key: value for key, value in document.items() if key != 'gene_set_collections'} | {
            'gene_set_collections': document['gene_set_collections']}
        document = dict(sorted(document.items(), key=lambda item: list(document).index(item[0]) if item[0] != 'gene_set_collections'
                               else list(document).index('gene_sets') + 0.5))
    return '# DAPPER geneset document\n' + yaml.safe_dump(document, sort_keys=False, width=1000)


def lap_project(root):
    project = root / 'lap'
    tsv(project / 'proj.trait_kpn_map.tsv', rr.LAP_FILES['trait_kpn_map'][1], [
        {'trait': t, 'kpn_trait_id': k, 'kpn_release': 'v0.0.2', 'kpn_release_commit': '3cd554f', 'gwas_source_category': 'KPN',
         'phenotype_name': name, 'trait_group': 'metabolic', 'legacy_trait_group': 'GLYCEMIC', 'trait_type': 'phenotype', 'n_factors': n}
        for t, k, name, n in TRAITS])
    tsv(project / 'proj.factor_index.tsv', rr.LAP_FILES['factor_index'][1], [
        {'global_eaggl_column': f'Factor{i}', 'factor_id': f'{t}::Factor{n}', 'trait': t, 'kpn_trait_id': k, 'factor': f'Factor{n}',
         'factor_number': n, 'factor_label': label, 'n_nonzero_loadings': sum(v not in ('0', '0.0') for v in LOADINGS[f'{t}::Factor{n}']),
         'loading_l2': '1.5', 'loading_variant': 'capped'} for i, (t, k, n, label) in enumerate(FACTORS, 1)])
    tsv(project / 'proj.factor_metadata.tsv', ('factor_id', 'trait', 'factor', 'factor_number', 'label', 'gene_set_score', 'top_genes'), [
        {'factor_id': f'{t}::Factor{n}', 'trait': t, 'factor': f'Factor{n}', 'factor_number': n, 'label': label, 'gene_set_score': '4.2',
         'top_genes': 'INS,GCK'} for t, k, n, label in FACTORS])
    tsv(project / 'proj.all_factors.factors_by_genes.tsv.gz', ['Factor'] + GENES, [
        {'Factor': factor, **dict(zip(GENES, values))} for factor, values in LOADINGS.items()])
    tsv(project / 'proj.gene_set_index.tsv.gz', rr.LAP_FILES['gene_set_index'][1], [
        {'gene_set_id': g, 'gene_set_name': f'set {g[-3:]}', 'collection_id': COLL[0] if g in LIB_SETS else COLL[1],
         'cfde_label': LABELS[COLL[0] if g in LIB_SETS else COLL[1]], 'library': LIBRARY[g], 'partition': 'part', 'model': 'HZ1',
         'comparison': '', 'program': '', 'gmt_row': i, 'n_genes': 20, 'n_genes_in_eaggl_universe': 18, 'cfde_snapshot': '2026-09-28'}
        for i, g in enumerate(SETS, 1)])
    tsv(project / 'proj.projection_manifest.tsv', ('trait', 'kpn_trait_id', 'kpn_release', 'n_factors', 'pigean_commit', 'qc_pass'), [
        {'trait': t, 'kpn_trait_id': k, 'kpn_release': 'v0.0.2', 'n_factors': n, 'pigean_commit': 'ca59661', 'qc_pass': 'True'}
        for t, k, name, n in TRAITS])
    tsv(project / 'proj.cfde_index.tsv', rr.LAP_FILES['cfde_index'][1] + ('yaml_path',), [
        {'library': lib, 'partition': 'part', 'model': 'HZ1', 'comparison': '', 'program': '', 'label': LABELS[c], 'collection_id': c,
         'n_sets': n, 'n_genes': 30, 'yaml_path': '/ignored'} for c, lib, n in ((COLL[0], 'LIB', 4), (COLL[1], 'OTHER', 3))])
    tsv(project / 'proj.kpn_trait_registry.tsv', rr.LAP_FILES['kpn_trait_registry'][1], [
        {'portal_id': k, 'gwas_source_category': 'KPN', 'legacy_phenotype_id': t, 'phenotype_name': name, 'legacy_trait_group': 'GLYCEMIC',
         'trait_group': 'metabolic', 'trait_type': 'phenotype', 'pigean_id': ''} for t, k, name, n in TRAITS])
    tsv(project / 'proj.kpn_trait_flat.tsv', rr.LAP_FILES['kpn_trait_flat'][1] + ('mapping_justification', 'source'), [
        {'portal_id': KPN_A, 'description': 'T2D', 'is_dichotomous': '1', 'is_complex': 'true', 'target_id': 'EFO:0001360',
         'target_label': 'type 2 diabetes', 'target_ontology': 'EFO', 'mapping_predicate': 'skos:exactMatch', 'confidence': '0.9',
         'mapping_justification': 'curated', 'source': 's.tsv'},
        {'portal_id': KPN_B, 'description': '', 'is_dichotomous': '', 'is_complex': 'false', 'target_id': '', 'target_label': '',
         'target_ontology': '', 'mapping_predicate': '', 'confidence': '', 'mapping_justification': '', 'source': ''}])
    for collection, members in ((COLL[0], LIB_SETS), (COLL[1], OTHER_SETS)):
        path = project / 'collections' / LABELS[collection] / f'{LABELS[collection]}.GeneSetCollection.yaml'
        path.parent.mkdir(parents=True)
        path.write_text(collection_document(collection, members))
    for trait, kpn, name, n in TRAITS:
        rows = []
        for number in range(1, n + 1):
            ranked = ranks(number, trait)
            in_library = {}  # the factor's global order restricted to each library
            for kind in (0, 1):
                counts = {}
                for gene_set in sorted(SETS, key=lambda g: ranked[g][kind]):
                    counts[LIBRARY[gene_set]] = counts.get(LIBRARY[gene_set], 0) + 1
                    in_library[gene_set, kind] = counts[LIBRARY[gene_set]]
            for gene_set in sorted(SETS, key=lambda g: ranked[g][0]):  # LAP writes each factor by joint rank
                joint, marginal = ranked[gene_set]
                rows.append({'trait': trait, 'kpn_trait_id': kpn, 'factor_id': f'{trait}::Factor{number}', 'factor': f'Factor{number}',
                             'factor_label': 'x', 'gene_set_id': gene_set, 'collection_id': COLL[0] if gene_set in LIB_SETS else COLL[1],
                             'cfde_label': 'x', 'library': LIBRARY[gene_set], 'joint_loading': f'{(8 - joint) / 10:.4g}',
                             'marginal_loading': f'{(8 - marginal) / 1000:.4g}', 'joint_rank_in_factor': joint,
                             'marginal_rank_in_factor': marginal, 'is_joint_top_factor': int(joint == 1),
                             'joint_rank_in_library': in_library[gene_set, 0], 'marginal_rank_in_library': in_library[gene_set, 1]})
        tsv(project / 'traits' / trait / f'{trait}{rr.LONG_SUFFIX}', rr.LONG_COLUMNS, rows)
        gene_set_stats(project, trait, kpn, GENE_SET_STATS[trait])
    return project


def gene_set_stats(project, trait, kpn, items, response='log_bf'):
    path = project / 'traits' / trait / f'{trait}{rr.GENE_SET_STATS_SUFFIX}'
    tsv(path, rr.LAP_GENE_SET_STATS_COLUMNS, [
        {'trait': trait, 'kpn_trait_id': kpn, 'gene_set_id': g, 'collection_id': COLL[0] if g in LIB_SETS else COLL[1], 'cfde_label': 'x',
         'library': library, 'n_genes': 3, 'beta_uncorrected': uncorrected, 'beta': beta, 'avg_postp': postp, 'library_rank': rank,
         'response': response, 'p': '0.0004', 'sigma2': '7e-09'} for g, library, uncorrected, beta, postp, rank in items])
    return path


def text_vector(text):
    """The 'true' embedding of a text: what the cache holds and the fake service reproduces."""
    return np.random.default_rng(int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)).normal(size=DIMS).astype('<f4')


def vector_cache(root, labels=('Insulin secretion', 'Beta cell', 'Adipogenesis'), contexts=CONTEXTS):
    path = root / 'cache.sqlite3'
    connection = sqlite3.connect(path)
    for statement in rr.CACHE_SCHEMA: connection.execute(statement)
    connection.executemany('INSERT INTO meta VALUES (?,?)', [('model', MODEL), ('model_revision', 'rev1'), ('provider', 'huggingface'), ('dimensions', str(DIMS))])
    connection.executemany('INSERT INTO vectors VALUES (?,?,?,?,?)', [
        (hashlib.sha256(text.encode()).hexdigest(), kind, text, DIMS, text_vector(text).tobytes())
        for kind, texts in (('factor_label', labels), ('context', contexts)) for text in texts])
    connection.commit(); connection.close()
    return path


def no_embedding(texts, **options): raise AssertionError('every vector should come from the cache')


def build_services(embed=no_embedding, environ=ENVIRON):
    services = rr.Services(environ=dict(environ))
    services.err, services.embed = io.StringIO(), embed
    return services


@pytest.fixture
def lap(tmp_path):
    project = lap_project(tmp_path)
    return SimpleNamespace(project=project, long_files=rr.long_files_in(project / 'traits'), cache=vector_cache(tmp_path),
                           gene_set_stats=rr.gene_set_stats_files_in(project / 'traits'))


def build(lap, out, services=None, **options):
    return rr.build_release(services or build_services(), lap.project, lap.long_files, lap.cache, out,
                            **{'gene_set_stats_files': lap.gene_set_stats, 'top_n': 2, 'workers': 1, 'runtime': RUNTIME, **options})


def rows(path, columns=None):
    return list(rr.tsv_rows(path, columns)) if columns else list(rr.read_jsonl(path))


# --------------------------------------------------------------------------------------
# build


def test_build_writes_every_release_file_deterministically(lap, tmp_path):
    first = build(lap, tmp_path / 'one')
    second = build(lap, tmp_path / 'two')
    assert first['release_id'] == second['release_id'] and re.fullmatch('[a-f0-9]{64}', first['release_id']) and first['reused'] is False
    counts = {key: value for key, value in first['counts'].items() if key != 'projections'}
    assert counts == {'traits': 2, 'factors': 3, 'factor_genes': 8, 'collections': 2, 'gene_sets': 7, 'dapper_nodes': 9, 'dapper_edges': 7,
                      'trait_gene_sets': 3, 'vectors': {'factors': 3, 'contexts': 3}, 'archived_factors': 3}
    release, other = tmp_path / 'one', tmp_path / 'two'
    for name in rr.DATA_FILES: assert (release / name).read_bytes() == (other / name).read_bytes(), name
    manifest = rr.open_release(release)
    assert manifest['release_id'] == first['release_id'] == rr.release_identity(manifest['files'])
    assert set(manifest['files']) == set(rr.DATA_FILES) and manifest['release_id'] == digest({'format': rr.RELEASE_FORMAT, 'files': manifest['files']})
    assert manifest['archive']['file'] == rr.ARCHIVE_FILE and manifest['archive']['sha256'] == hashlib.sha256((release / rr.ARCHIVE_FILE).read_bytes()).hexdigest()
    assert all(hashlib.sha256((release / name).read_bytes()).hexdigest() == sha for name, sha in manifest['files'].items())
    assert manifest['sources']['pigean_commit'] == 'ca59661' and manifest['sources']['kpn_release_commit'] == '3cd554f'
    assert manifest['embedding'] == {'model': MODEL, 'model_revision': 'rev1', 'provider': 'huggingface', 'dimensions': DIMS,
                                     'cache_hits': 3, 'embedded': 0, 'calibration': None}
    factors = {row['factor_key']: row for row in rows(release / 'factors.jsonl.gz')}
    first_factor = factors[f'{KPN_A}::Factor1']
    assert set(first_factor) == set(rr.COLUMNS['factors']) and 'lap' not in first_factor['metadata']
    assert first_factor['public_id'] == 'factor:kpn:0000398:eaggl-capped-v1:Factor1' and first_factor['eaggl_factor_id'] == 'T2D::Factor1'
    assert first_factor['label'] == 'Insulin secretion ' and first_factor['input_sha256'] == hashlib.sha256(b'Insulin secretion').hexdigest()
    genes = rows(release / 'factor_genes.tsv.gz', rr.FACTOR_GENE_COLUMNS)
    assert [row['loading'] for row in genes if row['factor_key'] == f'{KPN_A}::Factor1'] == ['0.5', '1.2e-05', '0.25']
    loadings = hashlib.sha256(b'GCK\t0.5\nLEP\t1.2e-05\nPPARG\t0.25\n').hexdigest()
    assert first_factor['source_revision'] == digest({'label': 'Insulin secretion ', 'kpn_trait_id': KPN_A, 'factor': 'Factor1', 'loadings_sha256': loadings})
    traits = {row['kpn_trait_id']: row for row in rows(release / 'traits.jsonl.gz')}
    assert traits[KPN_A]['is_complex'] == 1 and traits[KPN_B]['is_dichotomous'] is None and set(traits[KPN_A]) == set(rr.COLUMNS['traits'])
    gene_set = rows(release / 'gene_sets.jsonl.gz')[0]
    assert gene_set['metadata']['dapper_gene_set'] == {'id': SETS[0], 'name': 'set aa1', 'member_type': 'gene',
                                                       'members': ['HGNC.SYMBOL:INS', 'HGNC.SYMBOL:GCK'], 'in_gene_set_collection': [COLL[0]]}
    collection = rows(release / 'collections.jsonl.gz')[0]
    assert set(collection['payload']) == {'collection', 'index', 'document_sha256', 'provenance'} and 'members' not in collection['payload']['collection']
    assert collection['payload']['provenance']['organizations'] == [ORG] and collection['n_sets'] == 4
    # Rebuilding into the same folder reuses it untouched.
    stamp = (release / 'manifest.json').stat().st_mtime_ns
    assert build(lap, release)['reused'] is True and (release / 'manifest.json').stat().st_mtime_ns == stamp


def test_projection_ranks_are_per_library_and_keep_the_top_n_of_either(lap, tmp_path):
    result = build(lap, tmp_path / 'release', top_n=2)
    kept = rows(tmp_path / 'release' / 'projections.tsv.gz', rr.PROJECTION_COLUMNS)
    assert result['counts']['projections'] == len(kept)
    expected = {}
    for trait, kpn, name, n in TRAITS:
        for number in range(1, n + 1):
            ranked, key = ranks(number, trait), rg.factor_key(kpn, f'Factor{number}')
            for library in ('LIB', 'OTHER'):
                members = [g for g in SETS if LIBRARY[g] == library]
                for g in members:
                    joint = 1 + sum(ranked[o][0] < ranked[g][0] for o in members)
                    marginal = 1 + sum(ranked[o][1] < ranked[g][1] for o in members)
                    if joint <= 2 or marginal <= 2:
                        expected[(key, g)] = {'library': library, 'joint_rank': str(joint), 'marginal_rank': str(marginal),
                                              'joint_loading': f'{(8 - ranked[g][0]) / 10:.4g}', 'marginal_loading': f'{(8 - ranked[g][1]) / 1000:.4g}',
                                              'is_joint_top_factor': str(int(ranked[g][0] == 1))}
    assert {(row['factor_key'], row['gene_set_id']): {k: v for k, v in row.items() if k not in ('factor_key', 'gene_set_id')} for row in kept} == expected
    assert len(expected) < 3 * len(SETS)  # some rows rank > 2 in their library both ways
    for (key, library), group in groupby(kept, key=lambda row: (row['factor_key'], row['library'])):
        group = [(int(row['joint_rank']), int(row['marginal_rank'])) for row in group]
        assert {1, 2} <= {joint for joint, _ in group} and {1, 2} <= {marginal for _, marginal in group}
        assert all(joint <= 2 or marginal <= 2 for joint, marginal in group) and [j for j, _ in group] == sorted(j for j, _ in group)
    assert [row['factor_key'] for row in kept] == sorted(row['factor_key'] for row in kept)
    assert build(lap, tmp_path / 'all', top_n=50)['counts']['projections'] == 3 * len(SETS)  # every library has fewer than 50 gene sets


def test_source_revision_follows_label_and_loadings_only(lap, tmp_path):
    def revisions(out):
        build(lap, out)
        return {row['factor_key']: row['source_revision'] for row in rows(out / 'factors.jsonl.gz')}, rr.open_release(out)['release_id']
    base, release = revisions(tmp_path / 'a')
    path = lap.project / 'proj.factor_metadata.tsv'
    path.write_text(path.read_text().replace('4.2', '4.3'))  # factor metadata changes, its content does not
    same, other = revisions(tmp_path / 'b')
    assert same == base and other != release
    matrix = lap.project / 'proj.all_factors.factors_by_genes.tsv.gz'
    with gzip.open(matrix, 'rt') as stream: text = stream.read()
    with gzip.open(matrix, 'wt') as stream: stream.write(text.replace('T2D::Factor2\t0\t0.75', 'T2D::Factor2\t0\t0.8'))
    changed, _ = revisions(tmp_path / 'c')
    assert changed[f'{KPN_A}::Factor2'] != base[f'{KPN_A}::Factor2']
    assert {key: value for key, value in changed.items() if key != f'{KPN_A}::Factor2'} == {key: value for key, value in base.items() if key != f'{KPN_A}::Factor2'}


def test_dapper_nodes_and_edges_come_from_every_document_without_embeddings(lap, tmp_path):
    build(lap, tmp_path / 'release')
    nodes = {row['id']: row for row in rows(tmp_path / 'release' / 'dapper_nodes.jsonl.gz')}
    classes = sorted(row['class_name'] for row in nodes.values())
    assert classes.count('Organization') == 1 and classes.count('GeneSetCollection') == 2  # either section order
    assert set(classes) == {'Organization', 'Dataset', 'File', 'Activity', 'GeneSetCollection'}  # gene sets are not embedded
    assert nodes[ORG['id']]['payload'] == ORG  # first document (collection_id order) wins
    assert 'members' not in nodes[COLL[0]]['payload'] and nodes[COLL[0]]['payload']['n_sets'] == 4
    edges = rows(tmp_path / 'release' / 'dapper_edges.tsv.gz', rr.EDGE_COLUMNS)
    keys = [(row['subject'], row['predicate'], row['object']) for row in edges]
    assert keys == sorted(set(keys)) and len(edges) == 7
    shared = [row for row in edges if row['subject'] == SHARED_EDGE['subject'] and row['object'] == SHARED_EDGE['object']]
    assert shared == [SHARED_EDGE]  # deduplicated across documents, edge_role kept
    assert {row['predicate'] for row in edges} == {'prov:used', 'prov:wasGeneratedBy', 'prov:wasDerivedFrom'}
    assert all(row['edge_role'] == '' for row in edges if row['predicate'] != 'prov:used')
    manifest = rr.open_release(tmp_path / 'release')
    assert manifest['dapper'] == {'nodes': 9, 'edges': 7, 'variants_skipped': 1}


def test_read_collection_streams_gene_sets_and_parses_the_tail(tmp_path):
    path = tmp_path / 'c.yaml'
    path.write_text('prefixes: {}\ngene_set_collections:\n- id: x\n  members: [a, b]\ngene_sets:\n- id: a\n  members:\n  - G1\n# note\n\n'
                    '- id: b\n  name: long\n    folded name\nused_edges:\n- subject: s\n  predicate: p\n  object: o\nembeddings: []\n')
    parsed = rr.read_collection(path)
    assert parsed['document'] == {'id': 'x'} and json.loads(parsed['gene_sets']['b']) == {'id': 'b', 'name': 'long folded name'}
    assert parsed['tail'] == {'used_edges': [{'subject': 's', 'predicate': 'p', 'object': 'o'}], 'embeddings': []}
    assert parsed['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text('gene_set_collections:\n- id: x\ngene_sets:\n- id: a\n  bad: [x\n')
    with pytest.raises(rr.Refused, match='invalid YAML near line'): rr.read_collection(path)
    path.write_text('gene_set_collections:\n- id: x\ngene_sets:\n- id: a\nused_edges: [broken\n')
    with pytest.raises(rr.Refused, match='invalid YAML after gene_sets'): rr.read_collection(path)


def test_vectors_reuse_the_cache_and_cfde_snapshot_without_embedding(lap, tmp_path):
    result = build(lap, tmp_path / 'release')
    directory = tmp_path / 'release' / 'vectors'
    factors = rows(directory / 'factors.tsv', rr.VECTOR_COLUMNS)
    matrix = np.load(directory / 'factors.f32.npy')
    assert matrix.dtype == np.dtype('<f4') and matrix.shape == (3, DIMS)
    assert [row['id'] for row in factors] == sorted(rg.factor_key(k, f'Factor{n}') for t, k, n, label in FACTORS)
    labels = {rg.factor_key(k, f'Factor{n}'): label.strip() for t, k, n, label in FACTORS}
    for position, row in enumerate(factors):
        assert np.array_equal(matrix[position], text_vector(labels[row['id']]))
        assert row['input_sha256'] == hashlib.sha256(labels[row['id']].encode()).hexdigest()
        assert row['vector_sha256'] == hashlib.sha256(matrix[position].tobytes()).hexdigest()
    contexts = rows(directory / 'contexts.tsv', rr.VECTOR_COLUMNS)
    assert [row['id'] for row in contexts] == sorted(hashlib.sha256(text.encode()).hexdigest() for text in CONTEXTS)
    assert all(row['id'] == row['input_sha256'] for row in contexts)
    # Gene sets and collections are not embedded.
    assert sorted(path.name for path in directory.iterdir()) == ['contexts.f32.npy', 'contexts.tsv', 'factors.f32.npy', 'factors.tsv']
    assert result['embedding']['cache_hits'] == 3 and result['embedding']['embedded'] == 0


def test_cache_lookup_prefers_factor_label_rows(tmp_path):
    path = vector_cache(tmp_path, labels=('Beta cell',), contexts=())
    connection = sqlite3.connect(path)
    other = np.ones(DIMS, dtype='<f4')
    connection.execute('INSERT INTO vectors VALUES (?,?,?,?,?)', (hashlib.sha256(b'Beta cell').hexdigest(), 'context', 'Beta cell', DIMS, other.tobytes()))
    connection.commit(); connection.close()
    cache = rr.VectorCache(path)
    try:
        found = cache.lookup([hashlib.sha256(b'Beta cell').hexdigest()])
        assert np.array_equal(next(iter(found.values())), text_vector('Beta cell'))
        assert [sha for sha, _ in cache.contexts()] == [hashlib.sha256(b'Beta cell').hexdigest()]
    finally: cache.close()


def stored_embedder(noise=0.0):
    calls = []
    def embed(texts, **options):
        calls.append((list(texts), options))
        return np.asarray([text_vector(text) for text in texts]) + noise * np.random.default_rng(1).normal(size=(len(texts), DIMS))
    return embed, calls


def test_missing_label_is_embedded_only_after_calibration_and_cached(lap, tmp_path):
    lap.cache.unlink()
    lap.cache = vector_cache(tmp_path, labels=('Insulin secretion', 'Beta cell'))
    embed, calls = stored_embedder()
    result = build(lap, tmp_path / 'release', services=build_services(embed))
    assert len(calls) == 2
    probes, options = calls[0]
    assert len(probes) == 5 and 'Adipogenesis' not in probes and options['model'] == MODEL and options['service_url'] == 'https://embed.invalid'
    assert calls[1][0] == ['Adipogenesis'] and calls[1][1]['provider'] == 'huggingface'
    assert result['embedding']['cache_hits'] == 2 and result['embedding']['embedded'] == 1
    assert result['embedding']['calibration']['probes'] == 5 and result['embedding']['calibration']['minimum_cosine'] > 0.999
    connection = sqlite3.connect(lap.cache)
    assert connection.execute('SELECT kind,text FROM vectors WHERE input_sha256=?', (hashlib.sha256(b'Adipogenesis').hexdigest(),)).fetchall() == \
        [('factor_label', 'Adipogenesis')]
    connection.close()
    # The next build finds every label in the cache: no embedding call, same release.
    again = build(lap, tmp_path / 'again')
    assert again['release_id'] == result['release_id'] and again['embedding']['embedded'] == 0


def test_calibration_failure_or_another_model_refuses_and_writes_nothing(lap, tmp_path):
    lap.cache.unlink()
    lap.cache = vector_cache(tmp_path, labels=('Insulin secretion', 'Beta cell'))
    embed, calls = stored_embedder(noise=3.0)
    with pytest.raises(rr.Refused, match='Calibration failed'): build(lap, tmp_path / 'release', services=build_services(embed))
    assert len(calls) == 1 and not (tmp_path / 'release').exists() and not list(tmp_path.glob('.release.*'))
    connection = sqlite3.connect(lap.cache)
    assert connection.execute('SELECT COUNT(*) FROM vectors').fetchone()[0] == 5
    connection.close()
    with pytest.raises(rr.Refused, match='is not the vector cache model'):
        build(lap, tmp_path / 'release', services=build_services(stored_embedder()[0], environ={'EMBEDDING_MODEL': 'other/model'}))


def test_archived_factors_are_frozen_snapshots_of_the_release(lap, tmp_path):
    result = build(lap, tmp_path / 'release')
    release_id = result['release_id']
    archived = rows(tmp_path / 'release' / rr.ARCHIVE_FILE)
    projections = rows(tmp_path / 'release' / 'projections.tsv.gz', rr.PROJECTION_COLUMNS)
    revisions = {factor['public_id']: factor['source_revision'] for factor in rows(tmp_path / 'release' / 'factors.jsonl.gz')}
    assert [row['source_id'] for row in archived] == [rg.public_id(k, f'Factor{n}') for k, n in sorted((k, n) for t, k, n, label in FACTORS)]
    schema = json.loads((ROOT / 'api/openapi.json').read_text())['components']['schemas']['ArchivedReferenceFactor']
    import jsonschema
    for row in archived:
        snapshot = row['snapshot']
        assert set(row) == set(rr.ARCHIVED_COLUMNS) and set(snapshot) == SNAPSHOT_KEYS
        assert row['archive_id'] == rg.archive_id(revisions[row['source_id']], row['source_id']) and row['generation_id'] == release_id
        assert row['snapshot_sha256'] == digest(snapshot) and row['source_id_sha256'] == hashlib.sha256(row['source_id'].encode()).hexdigest()
        assert snapshot['generation_manifest_sha256'] == release_id and snapshot['model'] == rg.KPN_MODEL
        jsonschema.validate({**snapshot, 'archive_id': row['archive_id'], 'captured_at': '2026-10-05T12:00:00Z'}, schema)
        key = rg.parse_public_id(row['source_id'])['factor_key']
        assert snapshot['mechanism']['id'] == RUNTIME.compute_id({key: value for key, value in snapshot['mechanism'].items() if key != 'id'}, 'Mechanism', '')
        loadings = [gene['loading'] for gene in snapshot['top_genes']]
        assert loadings == sorted(loadings, reverse=True) and len(loadings) == len(LOADINGS[snapshot['factor_id']]) - LOADINGS[snapshot['factor_id']].count('0') - LOADINGS[snapshot['factor_id']].count('0.0')
        expected = sorted(((r['library'], int(r['joint_rank']), r['gene_set_id']) for r in projections
                           if r['factor_key'] == key and int(r['joint_rank']) <= rr.TOP_GENE_SETS))
        assert [(g['library'], g['rank'], g['gene_set_id']) for g in snapshot['top_gene_sets']] == expected
        assert all(g['name'] == f"set {g['gene_set_id'][-3:]}" and g['collection_id'] in COLL for g in snapshot['top_gene_sets'])
    first = archived[1]['snapshot']  # KPN.TRAIT:0000398::Factor1
    assert first['trait'] == 'T2D' and first['label'] == 'Insulin secretion ' and first['top_genes'][0] == {'symbol': 'GCK', 'loading': 0.5}


def test_archived_snapshots_keep_each_librarys_top_gene_sets(lap, tmp_path, monkeypatch):
    monkeypatch.setattr(rr, 'TOP_GENE_SETS', 1)
    build(lap, tmp_path / 'release')
    projections = rows(tmp_path / 'release' / 'projections.tsv.gz', rr.PROJECTION_COLUMNS)
    for row in rows(tmp_path / 'release' / rr.ARCHIVE_FILE):
        key = rg.parse_public_id(row['source_id'])['factor_key']
        libraries = sorted({r['library'] for r in projections if r['factor_key'] == key})
        assert [(g['library'], g['rank']) for g in row['snapshot']['top_gene_sets']] == [(library, 1) for library in libraries]


def test_trait_gene_set_betas_are_released_per_trait_in_library_rank_order(lap, tmp_path):
    build(lap, tmp_path / 'release')
    manifest = rr.open_release(tmp_path / 'release')
    assert manifest['gene_set_stats']['response'] == 'log_bf' and (manifest['gene_set_stats']['traits'], manifest['gene_set_stats']['traits_with_rows']) == (2, 1)
    assert [tuple(row.values()) for row in rows(tmp_path / 'release' / 'trait_gene_sets.tsv.gz', rr.TRAIT_GENE_SET_COLUMNS)] == [
        (KPN_A, LIB_SETS[0], 'LIB', '0.9', '0.8', '0.95', '1'), (KPN_A, LIB_SETS[2], 'LIB', '0.2', '0.1', '0.3', '2'),
        (KPN_A, OTHER_SETS[1], 'OTHER', '0.05', '0.04', '0.06', '1')]


@pytest.mark.parametrize('trait, items, response, message', [
    ('T2D', [('dapper:GeneSet.' + 'z' * 31 + '1', 'LIB', '0.9', '0.8', '0.9', 1)], 'log_bf', 'unknown or repeated gene set'),
    ('T2D', [(LIB_SETS[0], 'LIB', '0.9', '0.8', '0.9', 1), (LIB_SETS[0], 'LIB', '0.8', '0.8', '0.9', 2)], 'log_bf', 'unknown or repeated gene set'),
    ('T2D', [(LIB_SETS[0], 'LIB', '0.9', '0.8', '0.9', 2)], 'log_bf', 'library ranks are not 1..n'),
    ('BMI', [(LIB_SETS[0], 'LIB', '0.9', '0.8', '0.9', 1)], 'combined', 'mix the responses')])
def test_gene_set_stats_that_do_not_hold_are_refused(lap, tmp_path, trait, items, response, message):
    gene_set_stats(lap.project, trait, dict((t, k) for t, k, *_ in TRAITS)[trait], items, response)
    with pytest.raises(rr.Refused, match=message): build(lap, tmp_path / 'release')
    with pytest.raises(rr.Refused, match='No gene-set stats for 1 traits'): build(lap, tmp_path / 'release', gene_set_stats_files=lap.gene_set_stats[:1])
    with pytest.raises(rr.Refused, match='Pass the per-trait gene-set stats'): build(lap, tmp_path / 'release', gene_set_stats_files=[])


def test_out_folder_is_swapped_only_when_the_release_changes(lap, tmp_path):
    out = tmp_path / 'release' / 'files'
    out.mkdir(parents=True)  # LAP creates the folder before the command runs
    first = build(lap, out)
    assert first['reused'] is False and (out / 'manifest.json').is_file()
    (out / 'stale-marker').write_text('old release')
    assert build(lap, out)['reused'] is True and (out / 'stale-marker').exists()
    path = lap.project / 'proj.factor_metadata.tsv'
    path.write_text(path.read_text().replace('4.2', '4.4'))
    second = build(lap, out)
    assert second['reused'] is False and second['release_id'] != first['release_id'] and not (out / 'stale-marker').exists()
    assert rr.open_release(out)['release_id'] == second['release_id'] and sorted(p.name for p in out.parent.iterdir()) == ['files']


def test_build_never_replaces_a_folder_that_is_not_a_release(lap, tmp_path):
    out = tmp_path / 'project'  # a mistyped --out, say the LAP project dir
    (out / 'outputs').mkdir(parents=True); (out / 'outputs' / 'projection.tsv').write_text('keep')
    with pytest.raises(rr.Refused, match='is not a release folder'): build(lap, out)
    assert (out / 'outputs' / 'projection.tsv').read_text() == 'keep' and not list(tmp_path.glob('.project*'))
    (tmp_path / 'empty' / '.nfs0001').parent.mkdir(); (tmp_path / 'empty' / '.nfs0001').write_text('')  # LAP's mkdir'd folder
    assert build(lap, tmp_path / 'empty')['reused'] is False


def test_build_requires_the_dapper_runtime_before_doing_any_work(lap, tmp_path, monkeypatch):
    def unavailable(): raise ImportError('no DAPPER runtime')
    monkeypatch.setattr(rr, 'dapper_runtime', unavailable)
    with pytest.raises(rr.Refused, match='DAPPER runtime that mints Mechanism ids is unavailable'): build(lap, tmp_path / 'out', runtime=None)
    assert not (tmp_path / 'out').exists() and not list(tmp_path.glob('.out.build-*'))


def test_library_ranks_with_a_gap_are_refused(tmp_path):
    row = {'trait': 'T2D', 'kpn_trait_id': KPN_A, 'factor_id': 'T2D::Factor1', 'factor': 'Factor1', 'factor_label': 'x', 'collection_id': COLL[0],
           'cfde_label': 'x', 'library': 'LIB', 'joint_loading': '0.5', 'marginal_loading': '0.1', 'is_joint_top_factor': 0,
           'joint_rank_in_factor': 1, 'marginal_rank_in_factor': 1}
    path = tmp_path / f'T2D{rr.LONG_SUFFIX}'
    tsv(path, rr.LONG_COLUMNS, [{**row, 'gene_set_id': SETS[0], 'joint_rank_in_library': 1, 'marginal_rank_in_library': 1},
                                {**row, 'gene_set_id': SETS[1], 'joint_rank_in_library': 3, 'marginal_rank_in_library': 2}])
    with pytest.raises(rr.Refused, match='T2D::Factor1 ranks in LIB are not 1..n'): rr.long_file_ranks(path, 3)


def test_build_refuses_missing_long_files_and_unordered_factors(lap, tmp_path):
    with pytest.raises(rr.Refused, match='No long file for 1 traits'):
        rr.build_release(build_services(), lap.project, lap.long_files[:1], lap.cache, tmp_path / 'out',
                         gene_set_stats_files=lap.gene_set_stats, workers=1, runtime=RUNTIME)
    path = next(path for path in lap.long_files if path.name.startswith('T2D'))
    with gzip.open(path, 'rt') as stream: lines = stream.readlines()
    with gzip.open(path, 'wt') as stream: stream.writelines(lines[:2] + lines[9:10] + lines[2:9] + lines[10:])
    with pytest.raises(rr.Refused, match='are not contiguous'): build(lap, tmp_path / 'out')
    with pytest.raises(rr.Refused, match='are not contiguous'): build(lap, tmp_path / 'out', workers=2)  # raised in a spawned worker
    assert not (tmp_path / 'out').exists() and not list(tmp_path.glob('.out.*'))


def test_long_files_from_skips_hidden_directories(lap):
    hidden = lap.project / 'traits' / 'T2D' / '.filtered' / f'T2D{rr.LONG_SUFFIX}'
    hidden.parent.mkdir(); hidden.write_bytes(b'')
    assert [path.name for path in rr.long_files_in(lap.project / 'traits')] == [f'BMI{rr.LONG_SUFFIX}', f'T2D{rr.LONG_SUFFIX}']


def test_process_pool_build_matches_the_inline_build(lap, tmp_path):
    assert build(lap, tmp_path / 'pool', workers=2)['release_id'] == build(lap, tmp_path / 'inline', workers=1)['release_id']


# --------------------------------------------------------------------------------------
# publish


class FakeCursor:
    def __init__(self, db): self.db, self.rows, self.rowcount = db, [], 0
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, sql, params=()):
        sql = ' '.join(sql.split()); self.db.events.append(('sql', sql, tuple(params or ())))
        self.rows = list(self.db.answer(sql, tuple(params or ()))); self.rowcount = len(self.rows)
    def executemany(self, sql, rows):
        sql, rows = ' '.join(sql.split()), [tuple(row) for row in rows]
        self.db.events.append(('many', sql, rows)); self.db.insert(sql, rows); self.rowcount = len(rows)
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return list(self.rows)


class FakeDB:
    """Stateful MySQL stand-in: tables are lists of row dicts; DDL, RENAME, INSERT [IGNORE] and the publish SELECTs."""
    def __init__(self, events, lock=1, failures=None):
        self.events, self.tables, self.lock, self.closed = events, {}, lock, 0
        self.failures = failures or {}  # {sql prefix: [error code, times]}: that statement raises before it changes anything
    def cursor(self): return FakeCursor(self)
    def commit(self): pass
    def rollback(self): pass
    def close(self): self.closed += 1
    def answer(self, sql, params):
        for prefix, failure in self.failures.items():
            if sql.startswith(prefix) and failure[1]:
                failure[1] -= 1; raise OSError(failure[0], f'scripted failure of {prefix}')
        if sql.startswith('SELECT GET_LOCK'): return [(self.lock,)]
        if sql.startswith(('SELECT RELEASE_LOCK',)): return [(1,)]
        if sql.startswith(('SET ', 'SHOW WARNINGS')): return []
        if 'information_schema.TABLES' in sql: return [(name,) for name in params if name in self.tables]
        if sql.startswith('DROP TABLE IF EXISTS '):
            for name in sql[len('DROP TABLE IF EXISTS '):].split(', '): self.tables.pop(name, None)
            return []
        create = re.match(r'CREATE TABLE (IF NOT EXISTS )?(\w+) \(', sql)
        if create:
            assert create[1] or create[2] not in self.tables, sql
            self.tables.setdefault(create[2], []); return []
        if sql.startswith('RENAME TABLE '):
            for pair in sql[len('RENAME TABLE '):].split(', '):
                source, target = pair.split(' TO ')
                assert source in self.tables and target not in self.tables, pair
                self.tables[target] = self.tables.pop(source)
            return []
        select = re.fullmatch(r'SELECT release_id,published_at FROM (\w+)', sql)
        if select: return [(row['release_id'], row['published_at']) for row in self.tables[select[1]]]
        if sql.startswith('SELECT archive_id FROM archived_reference_factors WHERE archive_id IN ('):
            assert sql.count('%s') == len(params)
            return [(row['archive_id'],) for row in self.tables['archived_reference_factors'] if row['archive_id'] in params]
        raise AssertionError(f'Unexpected SQL {sql}')
    def insert(self, sql, rows):
        match = re.fullmatch(r'INSERT (IGNORE )?INTO (\w+) \((.+?)\) VALUES \(.+\)', sql)
        table, columns = self.tables[match[2]], match[3].split(',')
        for row in rows:
            record = dict(zip(columns, row))
            if match[1] and any(stored['archive_id'] == record['archive_id'] for stored in table): continue
            table.append(record)


class FakeIndex:
    def __init__(self, events, namespaces=None): self.events, self.namespaces = events, namespaces or {}
    def list_namespaces(self): return list(self.namespaces) + ['']
    def range(self, cursor='', limit=1, include_metadata=False, namespace='', **_):
        assert namespace in self.namespaces, f'range of the absent namespace {namespace}'
        items, start = sorted(self.namespaces.get(namespace, {}).items()), int(cursor or 0)
        return SimpleNamespace(vectors=[SimpleNamespace(id=identity, metadata=item['metadata'] if include_metadata else None)
                                        for identity, item in items[start:start + limit]],
                               next_cursor=str(start + limit) if start + limit < len(items) else '')
    def upsert(self, vectors, namespace=''):
        self.events.append(('upsert', namespace, [item['id'] for item in vectors]))
        for item in vectors: self.namespaces.setdefault(namespace, {})[item['id']] = {'vector': item['vector'], 'metadata': item['metadata']}
    def delete(self, ids=None, namespace='', **_):
        self.events.append(('delete', namespace, list(ids)))
        return SimpleNamespace(deleted=sum(self.namespaces.get(namespace, {}).pop(identity, None) is not None for identity in ids))


def publish_services(tmp_path, events, db=None, index=None):
    services = rr.Services(environ={})
    services.err, services.clock = io.StringIO(), lambda: CLOCK
    services.db = db or FakeDB(events)
    services.index = index or FakeIndex(events, {'qa-factors': {'KPN.TRAIT:0000001::Factor9': {'vector': [1.0] * DIMS, 'metadata': {'kind': 'factor'}}}})
    services.connect = lambda: services.db
    services.vector_client = lambda env: services.index
    sqlite = tmp_path / 'records.sqlite3'
    def repository(prefix):
        repo = Repository(sqlite_path=str(sqlite), table_prefix=prefix); repo.migrate(); return repo
    services.repository = repository
    return services


def sql_events(events, pattern):
    return [event for event in events if event[0] in ('sql', 'many') and re.search(pattern, event[1])]


def record(services, prefix):
    with services.repository(prefix).read_transaction() as tx: return tx.get(rr.RECORD_KIND, rr.RECORD_ID)


@pytest.fixture
def release(lap, tmp_path):
    build(lap, tmp_path / 'release')
    return tmp_path / 'release'


def test_publish_fills_new_tables_swaps_them_in_one_rename_and_records_the_release(release, tmp_path):
    events = []
    services = publish_services(tmp_path, events)
    manifest = rr.open_release(release)
    result = rr.publish_release(services, release, ['qa'], results=[])
    qa = result['environments'][0]
    assert qa['tables']['action'] == 'replaced' and qa['tables']['published_at'] == '2026-10-05T12:00:00.123456Z'
    assert qa['tables']['rows'] == {'traits': 2, 'factors': 3, 'factor_genes': 8, 'collections': 2, 'gene_sets': 7,
                                    'projections': manifest['counts']['projections'], 'dapper_nodes': 9, 'dapper_edges': 7, 'trait_gene_sets': 3,
                                    'release': 1}
    statements = [event[1] for event in events if event[0] == 'sql']
    # Strict mode before the lock, the lock before any change; released at the end.
    assert statements.index(rr.STRICT_MODE) < statements.index('SELECT GET_LOCK(%s,%s)')
    assert sql_events(events, r'^SELECT GET_LOCK')[0][2] == ('reveal:publish:reveal_workflow_qa', 0)
    assert statements[-1] == 'SELECT RELEASE_LOCK(%s)' and services.db.closed == 1
    tables = [f'reveal_workflow_qa_ref_{name}' for name in rr.TABLES]
    assert sql_events(events, r'^DROP TABLE')[0][1] == 'DROP TABLE IF EXISTS ' + ', '.join([t + '__new' for t in tables] + [t + '__old' for t in tables])
    assert [re.match(r'CREATE TABLE (\w+)', event[1])[1] for event in sql_events(events, r'^CREATE TABLE reveal')] == [t + '__new' for t in tables]
    inserted = {re.match(r'INSERT INTO (\w+)', event[1])[1] for event in sql_events(events, r'^INSERT INTO reveal')}
    assert inserted == {t + '__new' for t in tables}
    renames = sql_events(events, r'^RENAME TABLE')
    assert len(renames) == 1 and renames[0][1] == 'RENAME TABLE ' + ', '.join(f'{t}__new TO {t}' for t in tables)
    assert sql_events(events, r'^DROP TABLE')[-1][1] == 'DROP TABLE IF EXISTS ' + ', '.join(t + '__old' for t in tables)
    db = services.db.tables
    assert set(db) == set(tables) | {'archived_reference_factors'}
    assert [row['release_id'] for row in db['reveal_workflow_qa_ref_release']] == [manifest['release_id']]
    assert json.loads(db['reveal_workflow_qa_ref_release'][0]['manifest']) == manifest
    assert db['reveal_workflow_qa_ref_release'][0]['published_at'] == '2026-10-05 12:00:00.123456'
    factor = next(row for row in db['reveal_workflow_qa_ref_factors'] if row['factor_key'] == f'{KPN_A}::Factor1')
    assert json.loads(factor['metadata'])['factor'] == 'Factor1' and isinstance(db['reveal_workflow_qa_ref_factor_genes'][0]['loading'], float)
    projection = db['reveal_workflow_qa_ref_projections'][0]
    assert isinstance(projection['joint_loading'], float) and isinstance(projection['joint_loading_text'], str) and isinstance(projection['joint_rank'], int)
    assert {row['edge_role'] for row in db['reveal_workflow_qa_ref_dapper_edges']} == {'data_input', 'metadata_input', None}
    # Frozen snapshots: INSERT IGNORE into the shared table, before the swap.
    archived = sql_events(events, r'^INSERT IGNORE INTO archived_reference_factors')
    assert len(db['archived_reference_factors']) == 3 and qa['archived_factors'] == {'rows': 3, 'inserted': 3}
    assert {row['generation_id'] for row in db['archived_reference_factors']} == {manifest['release_id']}
    # Upstash: the missing vectors before any MySQL statement, the stale vector deleted after the swap.
    position = {id(event): i for i, event in enumerate(events)}
    upserts, deletes = [e for e in events if e[0] == 'upsert'], [e for e in events if e[0] == 'delete']
    first_sql = min(position[id(e)] for e in events if e[0] in ('sql', 'many'))
    assert max(position[id(e)] for e in upserts) < first_sql < position[id(archived[0])] < position[id(renames[0])] < min(position[id(e)] for e in deletes)
    assert 'SET SESSION lock_wait_timeout = 5' in statements[:statements.index('SELECT GET_LOCK(%s,%s)')]
    assert deletes == [('delete', 'qa-factors', ['KPN.TRAIT:0000001::Factor9'])]
    namespaces = services.index.namespaces
    assert sorted(namespaces) == ['qa-contexts', 'qa-factors']
    assert sorted(namespaces['qa-factors']) == sorted(rg.factor_key(k, f'Factor{n}') for t, k, n, label in FACTORS)
    metadata = namespaces['qa-factors'][f'{KPN_A}::Factor1']['metadata']
    vector = np.asarray(namespaces['qa-factors'][f'{KPN_A}::Factor1']['vector'], dtype='<f4')
    assert metadata == {'kind': 'factor', 'public_id': rg.public_id(KPN_A, 'Factor1'), 'kpn_trait_id': KPN_A, 'label': 'Insulin secretion ',
                        'input_sha256': hashlib.sha256(b'Insulin secretion').hexdigest(), 'vector_sha256': hashlib.sha256(vector.tobytes()).hexdigest()}
    assert np.array_equal(vector, text_vector('Insulin secretion'))
    context = next(iter(namespaces['qa-contexts'].items()))
    assert context[1]['metadata'] == {'kind': 'context', 'input_sha256': context[0], 'vector_sha256': context[1]['metadata']['vector_sha256']}
    assert qa['vectors']['factors'] == {'namespace': 'qa-factors', 'vectors': 3, 'existing': 1, 'added': 3, 'changed': 0, 'updated': 0,
                                        'stale': 1, 'deleted': 1}
    stored = record(services, 'reveal_workflow_qa')
    assert stored['owner'] == rg.CATALOG_OWNER and stored['data'] == {'release_id': manifest['release_id'], 'published_at': '2026-10-05T12:00:00.123456Z'}
    assert qa['record']['written'] is True and set(qa['seconds']) == {'vectors', 'tables', 'archived_factors', 'update', 'record', 'total'}
    if workspace_events.tracked(rr.RECORD_KIND):  # the app announces a published release as the public 'reference' catalog event
        with services.repository('reveal_workflow_qa').read_transaction() as tx: published = tx.list('workspace_event', 'public')
        assert [(event['data']['event_type'], event['data']['entity_id']) for event in published] == [('catalog.updated', 'reference')]


def test_republishing_the_same_release_changes_nothing(release, tmp_path, monkeypatch):
    events = []
    services = publish_services(tmp_path, events)
    monkeypatch.setattr(rr, 'RANGE_PAGE', 2)  # exercise range paging
    rr.publish_release(services, release, ['qa'])
    events.clear()
    again = rr.publish_release(services, release, ['qa'])['environments'][0]
    assert again['tables']['action'] == 'unchanged' and again['archived_factors'] == {'rows': 3, 'inserted': 0}
    tables = [f'reveal_workflow_qa_ref_{name}' for name in rr.TABLES]
    # Only the leftovers of a run that died are dropped: none here.
    assert [event[1] for event in sql_events(events, r'^(CREATE TABLE reveal|RENAME|DROP|INSERT)')] == [
        'DROP TABLE IF EXISTS ' + ', '.join([t + '__new' for t in tables] + [t + '__old' for t in tables])]
    assert not [e for e in events if e[0] in ('upsert', 'delete')]
    assert all((plan['added'], plan['updated'], plan['deleted']) == (0, 0, 0) for plan in again['vectors'].values())
    assert again['record']['written'] is False and record(services, 'reveal_workflow_qa')['version'] == 1


def test_publishing_a_new_release_replaces_every_table_and_prunes_old_vectors(lap, release, tmp_path):
    events = []
    services = publish_services(tmp_path, events)
    first = rr.publish_release(services, release, ['qa'])
    matrix = lap.project / 'proj.all_factors.factors_by_genes.tsv.gz'
    with gzip.open(matrix, 'rt') as stream: text = stream.read()
    with gzip.open(matrix, 'wt') as stream: stream.write(text.replace('BMI::Factor1\t0.3', 'BMI::Factor1\t0.35'))
    second_release = build(lap, tmp_path / 'second')
    services.clock = lambda: datetime(2026, 10, 6, tzinfo=timezone.utc)
    services.index.namespaces['qa-contexts']['f' * 64] = {'vector': [1.0] * DIMS, 'metadata': {'kind': 'context'}}  # a retired context
    relabeled = f'{KPN_A}::Factor2'  # as if the served release had another vector for it
    services.index.namespaces['qa-factors'][relabeled]['metadata'] = dict(services.index.namespaces['qa-factors'][relabeled]['metadata'],
                                                                          vector_sha256='0' * 64)
    events.clear()
    second = rr.publish_release(services, tmp_path / 'second', ['qa'])['environments'][0]
    tables = [f'reveal_workflow_qa_ref_{name}' for name in rr.TABLES]
    renames = sql_events(events, r'^RENAME TABLE')
    assert len(renames) == 1 and renames[0][1] == 'RENAME TABLE ' + ', '.join(f'{t} TO {t}__old, {t}__new TO {t}' for t in tables)
    assert sql_events(events, r'^DROP TABLE')[-1][1] == 'DROP TABLE IF EXISTS ' + ', '.join(t + '__old' for t in tables)
    assert [row['release_id'] for row in services.db.tables['reveal_workflow_qa_ref_release']] == [second_release['release_id']]
    archived = services.db.tables['archived_reference_factors']  # only the changed factor gets a new frozen snapshot
    assert second['archived_factors'] == {'rows': 3, 'inserted': 1} and len(archived) == 4
    changed = rg.public_id(KPN_B, 'Factor1')
    assert sorted((row['source_id'] == changed, row['generation_id']) for row in archived) == sorted(
        [(False, first['release_id'])] * 2 + [(True, first['release_id']), (True, second_release['release_id'])])
    assert second['vectors']['contexts']['deleted'] == 1
    # The changed vector is overwritten only once the tables hold the release (the served tables keep their vectors until the swap).
    assert (second['vectors']['factors']['added'], second['vectors']['factors']['changed'], second['vectors']['factors']['updated']) == (0, 1, 1)
    position = {id(event): i for i, event in enumerate(events)}
    (update,) = [e for e in events if e[0] == 'upsert' and relabeled in e[2]]
    assert position[id(update)] > position[id(renames[0])] and update[2] == [relabeled]
    assert record(services, 'reveal_workflow_qa')['data'] == {'release_id': second_release['release_id'], 'published_at': '2026-10-06T00:00:00.000000Z'}
    assert first['release_id'] != second_release['release_id']


def test_publish_refuses_while_another_publish_holds_the_lock(release, tmp_path):
    events = []
    services = publish_services(tmp_path, events, db=FakeDB(events, lock=0))
    with pytest.raises(rr.Refused, match='holds the lock reveal:publish:reveal_workflow_qa'): rr.publish_release(services, release, ['qa'])
    # Only the vectors the namespaces lack were added (the served tables never name them): no table change, update or delete.
    assert not sql_events(events, r'^(CREATE|RENAME|DROP|INSERT)|RELEASE_LOCK') and not [e for e in events if e[0] == 'delete']
    assert 'KPN.TRAIT:0000001::Factor9' in services.index.namespaces['qa-factors'] and services.db.closed == 1


def test_publish_environments_in_order_with_their_own_prefix_lock_and_namespaces(release, tmp_path):
    events = []
    services = publish_services(tmp_path, events)
    result = rr.publish_release(services, release, ['local', 'qa', 'local'])
    assert [item['env'] for item in result['environments']] == ['local', 'qa']
    assert [event[2][0] for event in sql_events(events, r'^SELECT GET_LOCK')] == ['reveal:publish:reveal_workflow_local', 'reveal:publish:reveal_workflow_qa']
    assert {'reveal_workflow_local_ref_release', 'reveal_workflow_qa_ref_release'} <= set(services.db.tables)
    assert set(services.index.namespaces) == {'local-factors', 'local-contexts', 'qa-factors', 'qa-contexts'}
    assert result['environments'][1]['archived_factors'] == {'rows': 3, 'inserted': 0}  # shared table, written once
    assert record(services, 'reveal_workflow_local')['data']['release_id'] == result['release_id']
    prod = publish_services(tmp_path, events)
    rr.publish_release(prod, release, ['prod'], tables=True, vectors=False)
    assert 'reveal_ref_release' in prod.db.tables and record(prod, 'reveal')['data']['release_id'] == result['release_id']


def test_skip_flags_leave_their_side_untouched(release, tmp_path):
    events = []
    services = publish_services(tmp_path, events)
    services.vector_client = lambda env: pytest.fail('--skip-vectors must not build an Upstash client')
    only_tables = rr.publish_release(services, release, ['qa'], vectors=False)['environments'][0]
    assert only_tables['vectors'] == 'skipped' and only_tables['tables']['action'] == 'replaced' and only_tables['record']['written'] is True
    events.clear()
    fresh = publish_services(tmp_path / 'fresh', events)
    (tmp_path / 'fresh').mkdir()
    only_vectors = rr.publish_release(fresh, release, ['qa'], tables=False)['environments'][0]
    assert only_vectors['tables'] == 'skipped' and not sql_events(events, r'^(CREATE|RENAME|DROP|INSERT)')
    # The tables do not hold this release yet: add, but neither update, delete nor record.
    assert (only_vectors['vectors']['factors']['added'], only_vectors['vectors']['factors']['updated'], only_vectors['vectors']['factors']['deleted']) == (3, None, None)
    assert not [e for e in events if e[0] == 'delete'] and only_vectors['record'] == {'written': False, 'reason': 'the tables do not hold this release'}


def test_open_release_refuses_files_that_do_not_match_the_manifest(release, tmp_path):
    for name in ('factor_genes.tsv.gz', rr.ARCHIVE_FILE):
        copy = tmp_path / f'copy-{len(name)}'
        shutil.copytree(release, copy)
        data = (copy / name).read_bytes(); (copy / name).write_bytes(data[:len(data) // 2])  # a partial copy
        with pytest.raises(rr.Refused, match=f'{re.escape(name)} does not match the manifest'): rr.open_release(copy)
        events = []
        with pytest.raises(rr.Refused, match='does not match the manifest'): rr.publish_release(publish_services(tmp_path, events), copy, ['qa'])
        assert not events


def test_a_rebuild_during_the_publish_stops_it_before_the_swap(release, tmp_path):
    events = []
    services = publish_services(tmp_path, events)
    def rebuild():  # what build_release does: the folder is renamed aside and a new one renamed in
        shutil.copytree(release, tmp_path / 'rebuilt'); release.rename(tmp_path / 'aside'); (tmp_path / 'rebuilt').rename(release)
        return CLOCK
    services.clock = rebuild  # called once the tables start loading
    with pytest.raises(rr.Refused, match='changed during the publish'): rr.publish_release(services, release, ['qa'])
    assert not sql_events(events, r'^RENAME') and 'reveal_workflow_qa_ref_release' not in services.db.tables
    services.clock = lambda: CLOCK
    again = rr.publish_release(services, release, ['qa'])['environments'][0]  # the rebuilt folder is published as a whole
    assert again['tables']['action'] == 'replaced' and not [name for name in services.db.tables if name.endswith('__new')]


def test_a_publish_that_died_after_the_swap_is_finished_by_a_rerun(release, tmp_path):
    events = []
    services = publish_services(tmp_path, events, db=FakeDB(events, failures={'DROP TABLE IF EXISTS reveal_workflow_qa_ref_traits__old': [2003, 1]}))
    with pytest.raises(OSError, match='scripted failure'): rr.publish_release(services, release, ['qa'])
    tables = services.db.tables
    assert 'reveal_workflow_qa_ref_release' in tables and 'reveal_workflow_qa_ref_traits__old' not in tables  # first publish: no __old
    assert 'KPN.TRAIT:0000001::Factor9' in services.index.namespaces['qa-factors'] and record(services, 'reveal_workflow_qa') is None
    tables['reveal_workflow_qa_ref_traits__old'] = []  # as a later release's crash would leave it
    again = rr.publish_release(services, release, ['qa'])['environments'][0]
    assert again['tables']['action'] == 'unchanged' and 'reveal_workflow_qa_ref_traits__old' not in tables
    assert again['vectors']['factors']['deleted'] == 1 and again['record']['written'] is True


def test_the_swap_waits_briefly_for_long_readers_and_retries(release, tmp_path):
    events, slept = [], []
    services = publish_services(tmp_path, events, db=FakeDB(events, failures={'RENAME TABLE': [rr.LOCK_WAIT_TIMEOUT, 2]}))
    services.sleep = slept.append
    assert rr.publish_release(services, release, ['qa'])['environments'][0]['tables']['action'] == 'replaced'
    assert len(sql_events(events, r'^RENAME')) == 3 and slept == [rr.SWAP_LOCK_WAIT] * 2
    events.clear()
    other = publish_services(tmp_path / 'other', events, db=FakeDB(events, failures={'RENAME TABLE': [rr.LOCK_WAIT_TIMEOUT, rr.SWAP_ATTEMPTS]}))
    (tmp_path / 'other').mkdir(); other.sleep = slept.append
    with pytest.raises(OSError): rr.publish_release(other, release, ['qa'])
    assert len(sql_events(events, r'^RENAME')) == rr.SWAP_ATTEMPTS and 'reveal_workflow_qa_ref_release' not in other.db.tables


def test_the_release_event_goes_to_each_environments_own_channels(release, tmp_path, monkeypatch):
    published = []
    monkeypatch.setattr(redis_notifications, 'publish', lambda channels: published.append((os.environ.get('REVEAL_NOTIFICATION_NAMESPACE'), list(channels))))
    monkeypatch.setenv('REVEAL_NOTIFICATION_NAMESPACE', 'reveal-local')
    rr.publish_release(publish_services(tmp_path, []), release, ['qa', 'prod'], vectors=False)
    if not workspace_events.tracked(rr.RECORD_KIND): pytest.skip('the record kind emits no workspace event')
    assert [namespace for namespace, _ in published] == ['reveal-qa', 'reveal-prod']
    assert all(name.startswith(namespace + ':notify:') for namespace, channels in published for name in channels)
    assert os.environ['REVEAL_NOTIFICATION_NAMESPACE'] == 'reveal-local'


def test_vector_listing_pages_until_the_cursor_ends_and_refuses_a_stuck_cursor():
    pages = {'': (['a', 'b'], '7'), '7': (['c'], '')}
    client = SimpleNamespace(range=lambda cursor, limit, include_metadata, namespace: SimpleNamespace(
        vectors=[SimpleNamespace(id=i, metadata={'kind': 'factor'}) for i in pages[cursor][0]], next_cursor=pages[cursor][1]))
    assert rr.list_vectors(client, 'qa-factors') == {name: {'kind': 'factor'} for name in 'abc'}
    stuck = SimpleNamespace(range=lambda **_: SimpleNamespace(vectors=[SimpleNamespace(id='a', metadata={})], next_cursor='5'))
    with pytest.raises(RuntimeError, match='did not advance'): rr.list_vectors(stuck, 'qa-factors')


def test_insert_batches_respect_row_and_byte_limits_and_refuse_warnings():
    class Connection:
        def __init__(self, warnings=()): self.batches, self.warnings, self.commits = [], list(warnings), 0
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def executemany(self, sql, rows): self.sql = sql; self.batches.append(list(rows))
        def execute(self, sql, params=()): assert sql == 'SHOW WARNINGS'
        def fetchall(self): return self.warnings
        def commit(self): self.commits += 1
    connection = Connection()
    assert rr.insert_rows(connection, 't', ('a', 'b'), [('x' * 300, 1)] * 7, 3, max_bytes=1000) == 7
    assert [len(batch) for batch in connection.batches] == [3, 3, 1] and connection.commits == 3
    assert connection.sql == 'INSERT INTO t (a,b) VALUES (%s,%s)'
    connection = Connection()
    rr.insert_rows(connection, 't', ('a',), [('x' * 400,)] * 5 + [('y' * 2000,)], 100, max_bytes=1000)
    assert [len(batch) for batch in connection.batches] == [2, 2, 1, 1]  # flushed before a row would pass the budget
    duplicate = Connection([('Warning', 1062, "Duplicate entry 'a' for key 'PRIMARY'")])
    assert rr.insert_rows(duplicate, 't', ('a',), [('a',)], 10, ignore=True) == 1 and duplicate.sql.startswith('INSERT IGNORE INTO t')
    with pytest.raises(RuntimeError, match='MySQL warning inserting t: Duplicate entry'): rr.insert_rows(duplicate, 't', ('a',), [('a',)], 10)
    truncated = Connection([('Warning', 1265, "Data truncated for column 'a' at row 1")])
    with pytest.raises(RuntimeError, match='Data truncated'): rr.insert_rows(truncated, 't', ('a',), [('a',)], 10, ignore=True)
    assert truncated.commits == 0


def test_open_release_refuses_a_folder_whose_identity_does_not_hold(release, tmp_path):
    manifest = rr.read_json(release / 'manifest.json')
    manifest['files']['projections.tsv.gz'] = '0' * 64
    rr.write_json(release / 'manifest.json', manifest)
    with pytest.raises(rr.Refused, match='not a reveal.reference-release/1 folder'): rr.open_release(release)


def test_vector_client_uses_the_write_token_and_per_environment_overrides(monkeypatch):
    import upstash_vector
    made = []
    class Index:
        def __init__(self, **options): made.append(options); self._client = SimpleNamespace(timeout=None)
    monkeypatch.setattr(upstash_vector, 'Index', Index)
    services = rr.Services(environ={'UPSTASH_VECTOR_REST_URL': 'https://shared.upstash.io', 'UPSTASH_VECTOR_WRITE_TOKEN': 'w',
                                    'UPSTASH_VECTOR_REST_TOKEN': 'read-only', 'UPSTASH_VECTOR_REST_URL_PROD': 'https://prod.upstash.io',
                                    'UPSTASH_VECTOR_WRITE_TOKEN_PROD': 'pw'})
    services.vector_client('qa'); services.vector_client('prod')
    assert [(item['url'], item['token']) for item in made] == [('https://shared.upstash.io', 'w'), ('https://prod.upstash.io', 'pw')]
    services.environ = {'UPSTASH_VECTOR_REST_URL': 'https://shared.upstash.io', 'UPSTASH_VECTOR_REST_TOKEN': 'read-only'}
    with pytest.raises(rr.Refused, match='UPSTASH_VECTOR_WRITE_TOKEN'): services.vector_client('qa')
    services.environ = {'UPSTASH_VECTOR_REST_URL': 'https://shared.upstash.io', 'UPSTASH_VECTOR_WRITE_TOKEN': 'w', 'UPSTASH_VECTOR_REST_URL_QA': 'https://qa.upstash.io'}
    with pytest.raises(rr.Refused, match='_QA overrides'): services.vector_client('qa')


# --------------------------------------------------------------------------------------
# CLI and table names


def test_cli_prints_one_json_object_and_exits_by_outcome(lap, tmp_path, capsys, monkeypatch):
    out = tmp_path / 'cli'
    argv = ['build', '--lap-project-dir', str(lap.project), '--long-files-from', str(lap.project / 'traits'),
            '--gene-set-stats-from', str(lap.project / 'traits'),
            '--vector-cache', str(lap.cache), '--out', str(out), '--top-n', '2', '--workers', '1']
    monkeypatch.setattr(rr, 'dapper_runtime', lambda: RUNTIME)
    assert rr.main(argv, build_services()) == 0
    built = json.loads(capsys.readouterr().out)
    assert built['command'] == 'build' and built['ok'] is True and built['reused'] is False and built['out'] == str(out)
    events = []
    assert rr.main(['publish', '--release', str(out), '--env', 'qa'], publish_services(tmp_path, events)) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])['environments'][0]['tables']['action'] == 'replaced'
    assert rr.main(['publish', '--release', str(tmp_path / 'missing'), '--env', 'qa'], publish_services(tmp_path, [])) == 2
    failed = json.loads(capsys.readouterr().out)
    assert failed == {'command': 'publish', 'environments': [], 'ok': False, 'refused': f"{tmp_path / 'missing'}: no manifest.json"}
    assert rr.main(['publish', '--env', 'qa'], publish_services(tmp_path, [])) == 2
    assert 'required: --release' in json.loads(capsys.readouterr().out)['refused']
    broken = publish_services(tmp_path, [])
    broken.connect = lambda: (_ for _ in ()).throw(ConnectionError('no route'))
    assert rr.main(['publish', '--release', str(out), '--env', 'qa'], broken) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'ConnectionError: no route'


def test_application_sql_prefixes_reference_tables():
    assert application_sql('SELECT f.label FROM reveal_ref_factors f JOIN reveal_ref_traits t', 'reveal_workflow_qa') == \
        'SELECT f.label FROM reveal_workflow_qa_ref_factors f JOIN reveal_workflow_qa_ref_traits t'
    assert application_sql('RENAME TABLE reveal_ref_factor_genes__new TO reveal_ref_factor_genes', 'reveal_workflow_local') == \
        'RENAME TABLE reveal_workflow_local_ref_factor_genes__new TO reveal_workflow_local_ref_factor_genes'
    assert application_sql('SELECT 1 FROM reveal_records, reveal_transaction_lock', 'reveal_workflow_qa') == \
        'SELECT 1 FROM reveal_workflow_qa_records, reveal_workflow_qa_transaction_lock'
    unchanged = 'SELECT reveal_reference, xreveal_ref_a FROM archived_reference_factors, reveal_ref_release'
    assert application_sql(unchanged, 'reveal') == unchanged
    assert application_sql(unchanged, 'reveal_workflow_qa') == unchanged.replace(' reveal_ref_release', ' reveal_workflow_qa_ref_release')
    statements = rr.schema_statements('reveal_workflow_qa', '__new')
    assert [re.match(r'CREATE TABLE (\w+) \(', s)[1] for s in statements] == [f'reveal_workflow_qa_ref_{t}__new' for t in rr.TABLES]
    assert all('FOREIGN KEY' not in s and 'generation_id' not in s and 'utf8mb4_bin' in s for s in statements)
