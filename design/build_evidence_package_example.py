#!/usr/bin/env python3
"""Build source-checked, offline evidence-package design examples; no API/DB calls."""
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/examples/evidence-package-cad'
PIN = ROOT / 'data/dapper/2026-09-24-v8/snapshot/schema'
sys.path.insert(0, str(PIN / 'identity'))
sys.path.insert(0, str(PIN / 'lint'))
sys.path.insert(0, str(PIN))
from dapper_identity import compute_id, load_schema
from document_prefixes import PrefixResolver, transform_identifiers
from lint_provenance import build_validator

FACTOR = 'factor:portal:CADinT2D:cfde-inc-v2:Factor1'
TRAIT = 'trait:portal:CADinT2D'
MECH = 'dismech:disorders/Coronary_Artery_Disease#/pathophysiology/0'
GENES = ['SHH', 'GLI3', 'TCF7L2']
SKILL = 'services/backend/agent-skills/construct-scientific-account/SKILL.md'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def write_pair(stem, value):
    class ReadableDumper(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True
    stem.with_suffix('.json').write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    stem.with_suffix('.yaml').write_text(yaml.dump(value, Dumper=ReadableDumper, sort_keys=False, allow_unicode=True, width=100))


def pointer(value, path):
    for key in path.lstrip('/').split('/') if path else []:
        key = key.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def source_ref(artifact, path):
    return {'artifact_id': artifact, 'pointer': path}


def package_prefixes(schema):
    schema.imports_closure()
    # Preserve DAPPER's declared bindings; GO/ECTO agree with the DisMech schema.
    return {**{name: str(schema.namespaces()[name]) for name in ['dapper', 'MONDO', 'CL', 'PMID']},
            'GO': 'http://purl.obolibrary.org/obo/GO_', 'ECTO': 'http://purl.obolibrary.org/obo/ECTO_',
            'factor': 'urn:cfde:factor:', 'gene': 'urn:cfde:gene:', 'gene_set': 'urn:cfde:gene_set:',
            'trait': 'urn:cfde:trait:', 'cfde': 'urn:cfde:record:'}


def validate_prefixes(packet, schema):
    """Validate declared identifier slots/map keys, without rewriting source text or minted nodes."""
    resolver = PrefixResolver(schema, packet)
    assert not resolver.errors, resolver.errors
    expanded = {}
    scalar_fields = {'id', 'dapper_id', 'knowledge_gap_id', 'required_question', 'entity_uri', 'trait_id', 'reference', 'ascertained_via_mechanism'}
    list_fields = {'eaggl_mechanism_ids', 'dismissed_eaggl_ids', 'omitted_gene_ids', 'omitted_gene_set_ids'}

    def identifier(value):
        if value.startswith(('http://', 'https://', 'urn:')):
            resolver.expand(value)
            return
        prefix = value.partition(':')[0]
        assert prefix in packet['prefixes'], f'Undeclared package CURIE: {value}'
        expanded[value] = resolver.expand(value)

    def walk(value, path=()):
        if isinstance(value, dict):
            curie_keys = path in [('pigean', 'mechanisms'), ('pigean', 'traits'), ('entities', 'genes'), ('entities', 'gene_sets'), ('selection', 'origins')]
            curie_keys = curie_keys or (bool(path) and path[-1] == 'associated_eaggl_mechanisms')
            curie_keys = curie_keys or (len(path) >= 2 and path[-1] == 'items' and path[-2] in
                                      ['gene_loadings', 'gene_set_loadings', 'trait_loadings', 'gene_associations', 'gene_set_associations'])
            for key, child in value.items():
                # DisMech record aliases and result keys are opaque, not vocabulary CURIEs.
                if curie_keys:
                    identifier(key)
                if key in scalar_fields and isinstance(child, str):
                    identifier(child)
                if key in list_fields:
                    for item in child: identifier(item)
                walk(child, (*path, key))
        elif isinstance(value, list):
            for child in value: walk(child, path)
    walk(packet)
    if 'dapper_context' in packet:
        groups = {'knowledge_gaps': 'KnowledgeGap', 'mechanisms': 'Mechanism', 'gene_sets': 'GeneSet',
                  'activities': 'Activity', 'files': 'File'}
        _, errors = transform_identifiers(packet['dapper_context'], schema, groups)
        assert not errors, errors
    return expanded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-bundle', type=Path, help='Optional legacy EAGGL share directory; kept separate from CFDE.')
    args = parser.parse_args()
    (OUT / 'sources').mkdir(parents=True, exist_ok=True)
    schema = load_schema(PIN / 'dapper.yaml')
    account = read(ROOT / 'design/data/cad-account/scientific-account.json')
    gap_source = read(ROOT / 'design/data/cad-account/sources/dismech-gap.source.json')
    gap = account['knowledge_gaps'][0]
    assert gap['text'] == gap_source['raw']['prompt']
    pin = read(ROOT / 'data/dapper/2026-09-24-v8/snapshot.json')
    dis_manifest = read(ROOT / 'data/dismech/manifest.json')
    gap_manifest = read(ROOT / 'data/dismech-gaps/2026-09-24/manifest.json')
    assert dis_manifest['source_commit'] == gap_manifest['source_commit']
    source_path = gap_source['source_file']
    expected_sha = next(r['sha256'] for r in read(ROOT / 'data/dismech/source-files.json') if r['path'] == source_path)
    assert expected_sha == next(r['sha256'] for r in read(ROOT / 'data/dismech-gaps/2026-09-24/source-files.json') if r['path'] == source_path)
    local_dis = ROOT.parent / 'dismech' / source_path
    if not local_dis.exists():
        local_dis = OUT / 'sources/Coronary_Artery_Disease.yaml'
    assert sha(local_dis) == expected_sha, 'DisMech source changed; do not silently replace the frozen example.'
    dis_document = yaml.safe_load(local_dis.read_text())
    assert dis_document['discussions'][0] == gap_source['raw']
    artifacts, source_values, files = {}, {}, []

    def artifact(key, path, filename=None, retrieval=None, origin=None):
        target = OUT / 'sources' / (filename or path.name)
        if target.resolve() != path.resolve():
            shutil.copyfile(path, target)
        payload = yaml.safe_load(target.read_text()) if target.suffix == '.yaml' else read(target)
        file = {'filename': target.name, 'mime_type': 'application/yaml' if target.suffix == '.yaml' else 'application/json',
                'sha256': sha(target), 'size_in_bytes': target.stat().st_size}
        file['id'] = compute_id(file, 'File', schema)
        files.append(file)
        artifacts[key] = {'path': 'sources/' + target.name, 'sha256': file['sha256'], 'dapper_file_id': file['id'],
                          'origin': origin or str(path.relative_to(ROOT))}
        if retrieval:
            artifacts[key]['retrieval'] = {k: retrieval[k] for k in
                ['url', 'method', 'request', 'retrieved_at', 'status', 'body_sha256'] if k in retrieval}
        source_values[key] = payload
        return payload

    artifact('dismech-cad', local_dis, origin={
        'repository': dis_manifest['source_repository'], 'commit': dis_manifest['source_commit'], 'path': source_path})
    interactive = {}
    for target in ['gene', 'gene_set', 'trait']:
        path = ROOT / f'data/interactive/2026-09-24/cad-in-t2d-factor-{target}.json'
        wrapper = read(path)
        interactive[target] = artifact('interactive-' + target, path, retrieval=wrapper)['response']
    cpath = ROOT / 'data/interactive/2026-09-25/cad-in-t2d-contextual-edges.json'
    contextual = artifact('contextual', cpath, retrieval=read(cpath))['response']
    rows = {}
    for kind in ['gene', 'gene-set']:
        wrapper = read(ROOT / f'data/cfde/cad-in-t2d/pigean-{kind}-factor.json')
        raw = ROOT / f'data/cfde/cad-in-t2d/pigean-{kind}-factor.response.json'
        assert sha(raw) == wrapper['body_sha256']
        rows[kind] = artifact('bioindex-' + kind, raw, retrieval=wrapper)['data']
    artifact('geneset-import', ROOT / 'data/cfde-genesets/2026-09-24/manifest.json', 'geneset-import.manifest.json')
    mechanism_source = dis_document['pathophysiology'][0]
    mechanism = {k: mechanism_source[k] for k in ['name', 'description']}
    mechanism['id'] = compute_id(mechanism, 'Mechanism', schema)
    registry = {'genes': {}, 'gene_sets': {}}
    factor = {'dapper_id': account['mechanisms'][0]['id'], 'display_name': account['mechanisms'][0]['name'],
              'fit': {'trait_id': TRAIT, 'trait_group': 'portal', 'phenotype': 'CADinT2D', 'factor': 'Factor1',
                      'model': 'cfde-inc-v2', 'upstream_build': None},
              'gene_loadings': {'status': 'ok', 'items': {}}, 'gene_set_loadings': {'status': 'ok', 'items': {}},
              'trait_loadings': {'status': 'empty', 'items': {},
                                 'source_ref': source_ref('interactive-trait', '/response'),
                                 'interpretation': 'No direct trait candidates returned; the fit trait is metadata, not an observed trait loading.'}}
    trait = {'display_name': 'Coronary artery disease in people with type 2 diabetes', 'model': 'cfde-inc-v2',
             'gene_associations': {'status': 'ok', 'items': {}}, 'gene_set_associations': {'status': 'ok', 'items': {}},
             'coverage': 'Trait statistics retained from factor-query rows; no independent phenotype-wide query captured.'}
    selected_sets = account['gene_sets']
    selected_nodes = [FACTOR] + ['gene:' + g for g in GENES] + ['gene_set:' + g['term'] for g in selected_sets]
    selected_edges = []
    for kind, names, field, query_kind in [
        ('gene', GENES, 'gene', 'gene'),
        ('gene_set', [g['term'] for g in selected_sets], 'gene_set', 'gene-set')]:
        for name in names:
            key = f'{kind}:{name}'
            row_index, row = next((i, r) for i, r in enumerate(rows[query_kind]) if r[field] == name)
            assert row['factor'] == 'Factor1' and row['phenotype'] == 'CADinT2D' and row['gene_set_size'] == 'cfde-inc-v2'
            candidate_index, candidate = next((i, c) for i, c in enumerate(interactive[kind]['candidates']) if c['candidate']['node_id'] == key)
            edge = candidate['edges'][0]
            assert edge['source'] == FACTOR and edge['target'] == key
            assert abs(row['factor_value'] - edge['raw_score']) < 1e-6, 'Reported precision differs beyond the example tolerance.'
            selected_edges.append(edge)
            # This groups overlapping source observations; it is not a DAPPER ID or independent-study count.
            result_key = f'portal:CADinT2D:cfde-inc-v2:Factor1:{key}'
            ref = source_ref('bioindex-' + query_kind, f'/data/{row_index}')
            loading = {'result_key': result_key, 'factor_value': row['factor_value'], 'source_ref': ref,
                       'interactive': {k: candidate[k] for k in ['aggregate_score', 'support_anchor_count', 'anchor_count']}}
            loading['interactive'].update({'edge_id': edge['id'], 'family': edge['family'], 'relation': edge['relation'],
                                          'raw_score': edge['raw_score'], 'normalized_score': edge['normalized_score'],
                                          'source_ref': source_ref('interactive-' + kind, f'/response/candidates/{candidate_index}')})
            factor[kind + '_loadings']['items'][key] = loading
            stats = ['combined', 'log_bf', 'prior'] if kind == 'gene' else ['beta', 'beta_uncorrected', 'rs_score']
            trait[kind + '_associations']['items'][key] = {'result_key': result_key, 'reported_metrics': {k: row[k] for k in stats if k in row},
                                                'source_ref': ref, 'ascertained_via_mechanism': FACTOR}
            if kind == 'gene':
                registry['genes'][key] = {'source_symbol': name, 'entity_uri': 'urn:cfde:gene:' + name,
                                           'external_identity_mapping': 'not_resolved', 'organism': None}
            else:
                g = next(g for g in selected_sets if g['term'] == name)
                registry['gene_sets'][key] = {'dapper_id': g['id'], 'display_name': g['name'],
                    'import_id': source_values['geneset-import']['import_id'], 'membership_status': 'not_loaded',
                    'construction_provenance_status': 'not_loaded', 'provenance_refs': [source_ref('geneset-import', '')]}
    all_edges = [e for k in ['gene', 'gene_set'] for e in interactive[k]['graph']['edges']]
    assert len(all_edges) == 16 and len(contextual['edges']) == 16
    assert {e['id']: e for e in all_edges} == {e['id']: e for e in contextual['edges']}
    attachments = []
    with gzip.open(ROOT / 'data/dismech-gaps/2026-09-24/gap-attachments.jsonl.gz', 'rt') as stream:
        for line in stream:
            a = json.loads(line)
            if a['gap_id'] != gap_source['id']:
                continue
            target = pointer(dis_document, a['target_pointer'])
            attachments.append({'source_reference': a['source_reference'], 'kind': a['target_kind'],
                                'source_id': a['target_id'], 'label': a['target_label'], 'resolution': a['resolution'],
                                'source_ref': source_ref('dismech-cad', a['target_pointer'])})
    related = [{'source_id': f"{gap_source['document_id']}#discussion:{d['discussion_id']}",
                'prompt': d['prompt'], 'kind': d['kind'], 'status': d.get('status'),
                'source_ref': source_ref('dismech-cad', f'/discussions/{i}')}
               for i, d in enumerate(dis_document['discussions']) if i != 0 and d['kind'] in ['KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH']]
    packet = {
        'package_version': 'reveal.evidence-package/0.1-draft', 'prefixes': package_prefixes(schema),
        'identifier_policy': {
            'expansion': 'Split a CURIE at the first colon and concatenate its declared prefix base and local identifier.',
            'source_local_prefixes': ['factor', 'gene', 'gene_set', 'trait', 'cfde'],
            'dismech_record_ids': 'Opaque native aliases resolved through their source_ref and source revision; not DisMech schema vocabulary CURIEs.',
            'opaque_fields': ['source_id', 'source_reference', 'result_key', 'artifact_id', 'alternate_identifier'],
            'preserve_existing_dapper_payloads': True},
        'mode': 'offline_design_example', 'dispatch_ready': False,
        'dapper_pin': {'name': '2026-09-24-v8', 'snapshot_sha256': pin['snapshot_sha256']},
        'selection': {'knowledge_gap_id': gap['id'], 'dismech_mechanism_ids': [MECH], 'eaggl_mechanism_ids': [FACTOR],
                      'origins': {FACTOR: 'manual_design_example'}, 'dismissed_eaggl_ids': [],
                      'semantic_retrieval': {'status': 'not_computed', 'embedding_model': 'pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb',
                                             'embedding_run_id': None, 'automatic_limit_total': 5, 'aggregation': 'max_cosine_across_selected_dismech_mechanisms'},
                      'expansion_policy': {'rounds': 1, 'reducer': 'mean', 'connection_scope': 'direct', 'context': ''}},
        'dismech': {
            'source_revision': {'commit': dis_manifest['source_commit'], 'source_sha256': expected_sha},
            'knowledge_gap': {'dapper_id': gap['id'], 'source_id': gap_source['id'],
                              'source_kind': gap_source['kind'], 'source_status': gap_source['status'],
                              'prompt': gap_source['raw']['prompt'], 'rationale': gap_source['raw']['rationale'],
                              'disease': gap_source['disease_term'], 'attachments': attachments,
                              'proposed_experiments': gap_source['raw'].get('proposed_experiments', []),
                              'source_ref': source_ref('dismech-cad', '/discussions/0')},
            'mechanisms': {MECH: {'dapper_id': mechanism['id'], 'projection_status': 'example_projection_not_imported',
                                 **{k: mechanism_source[k] for k in ['name', 'description', 'cell_types', 'biological_processes', 'evidence']},
                                 'source_ref': source_ref('dismech-cad', '/pathophysiology/0'),
                                 'associated_eaggl_mechanisms': {FACTOR: {'status': 'not_computed', 'semantic_similarity': None,
                                                                       'association_basis': 'co_selected_for_design_example_only'}}}},
            'other_context': [{'source_id': a['source_id'], 'kind': a['kind'], 'record': pointer(dis_document, a['source_ref']['pointer']),
                               'source_ref': a['source_ref']} for a in attachments if a['kind'] != 'pathophysiology'],
            'related_knowledge_gaps': {'status': 'same_document_subset', 'items': related, 'selection_method': 'same frozen disease document; no semantic ranking'}},
        'pigean': {'source_family': 'cfde-interactive-and-bioindex', 'model': 'cfde-inc-v2',
                   'mechanisms': {FACTOR: factor}, 'traits': {TRAIT: trait},
                   'contextual_relationships': {'status': 'ok', 'returned_edges': 16, 'new_unique_edges': 0,
                                                 'source_ref': source_ref('contextual', '/response/edges'),
                                                 'retained_edge_ids': [e['id'] for e in selected_edges],
                                                 'gene_gene_set_membership': 'not_observed'}},
        'entities': registry,
        'dapper_context': {'prefixes': package_prefixes(schema), 'knowledge_gaps': [gap], 'mechanisms': [*account['mechanisms'], mechanism],
                          'gene_sets': selected_sets, 'activities': [account['activities'][0]], 'files': files},
        'source_artifacts': artifacts,
        'coverage': {'kind': 'authored_subset_of_captured_results', 'selection_rule': 'Three genes and two sets used in the existing account UI fixture; not a significance threshold.',
                     'captured': {'anchors': 1, 'genes': 8, 'gene_sets': 8, 'unique_graph_nodes': 17, 'unique_graph_edges': 16},
                     'retained': {'anchors': 1, 'genes': 3, 'gene_sets': 2, 'unique_graph_nodes': 6, 'unique_graph_edges': 5},
                     'omitted_gene_ids': [c['candidate']['node_id'] for c in interactive['gene']['candidates'] if c['candidate']['node_id'] not in selected_nodes],
                     'omitted_gene_set_ids': [c['candidate']['node_id'] for c in interactive['gene_set']['candidates'] if c['candidate']['node_id'] not in selected_nodes],
                     'queries': {'gene': 'ok_limit_reached', 'gene_set': 'ok_limit_reached', 'trait': 'empty', 'factor': 'not_captured', 'contextual': 'ok'},
                     'upstream_total': None, 'same_upstream_build_verified': False,
                     'limitations': ['Interactive and BioIndex responses were collected on different dates.',
                                    'Candidate counts describe returned rows, not exhaustive neighborhoods.',
                                    'The gap concerns CAD; this fit concerns CAD within a T2D population.',
                                    'No measured semantic match, retrieved KG assertion, or gene-set membership is included.',
                                    'No causal direction, interaction effect or exposure timing is established by these scores.']},
        'external_evidence': {'status': 'not_queried', 'mcp_url': 'https://apps.okn.us/okn-mcp-dev/mcp',
                              'selected_graphs': [{'shortname': k, 'graph_uri': f'https://purl.org/okn/frink/kg/{k}'} for k in ['biomarkerkg', 'prokn']],
                              'ledger': [], 'assertions': []},
        'authoring': {'skill': {'path': SKILL, 'sha256': sha(ROOT / SKILL)},
                      'contract': {'path': 'docs/evidence-package.md', 'sha256': sha(ROOT / 'docs/evidence-package.md')},
                      'references': [{'path': f'docs/{name}.md', 'sha256': sha(ROOT / f'docs/{name}.md')}
                                     for name in ['scientific-account-construction', 'pigean-claim-model', 'dapper-integration', 'agent-evidence-integration']],
                      'max_accounts': 3, 'required_question': gap['id'],
                      'budgets': {'selected_eaggl_anchors': 10, 'dismech_mechanisms': 10, 'candidates_per_target': 100,
                                  'retained_nodes': 250, 'retained_edges': 1000, 'total_context_tokens': 24000,
                                  'mcp_calls': 20, 'rows_per_mcp_query': 100, 'retained_external_tokens': 5000},
                      'runtime': {'harness': 'Claude Code', 'model': None, 'harness_version': None, 'status': 'not_launched'},
                      'token_count': None, 'token_count_status': 'not_measured_for_target_model'}}
    # Validate exact source pointers and projected metric values, including all duplicated views.
    refs = 0
    def check(value):
        nonlocal refs
        if isinstance(value, dict):
            if 'artifact_id' in value and 'pointer' in value:
                pointer(source_values[value['artifact_id']], value['pointer']); refs += 1
            if 'factor_value' in value and 'source_ref' in value:
                r = value['source_ref']; assert pointer(source_values[r['artifact_id']], r['pointer'])['factor_value'] == value['factor_value']
            if 'reported_metrics' in value:
                r = value['source_ref']; row = pointer(source_values[r['artifact_id']], r['pointer'])
                assert all(row[k] == v for k, v in value['reported_metrics'].items())
            for item in value.values(): check(item)
        elif isinstance(value, list):
            for item in value: check(item)
    check(packet)
    expanded_identifiers = validate_prefixes(packet, schema)
    validator = build_validator(PIN / 'dapper.yaml')
    checked_objects = 0
    for collection, cls in [('knowledge_gaps', 'KnowledgeGap'), ('mechanisms', 'Mechanism'), ('gene_sets', 'GeneSet'), ('activities', 'Activity'), ('files', 'File')]:
        for node in packet['dapper_context'][collection]:
            assert compute_id(node, cls, schema) == node['id'], f'Identity mismatch for {node["id"]}'
            report = validator.validate(node, cls)
            assert not report.results, [(r.message, node['id']) for r in report.results]
            checked_objects += 1
    write_pair(OUT / 'evidence-package', packet)
    manifest = {'package_version': packet['package_version'], 'json_sha256': sha(OUT / 'evidence-package.json'),
                'yaml_sha256': sha(OUT / 'evidence-package.yaml'), 'checked_source_refs': refs,
                'checked_dapper_objects': checked_objects,
                'expanded_identifiers': expanded_identifiers,
                'checks': ['All source pointers resolve.', 'Projected metric values equal captured rows.',
                           'Declared CURIE fields and entity-map keys expand through the pinned prefix map.',
                           'DisMech source hash matches both frozen imports.', 'DAPPER input shapes and identities verify under pinned schema.',
                           'All 16 contextual edges equal direct-query edges.', 'Retained six-node/five-edge subset is explicit.'],
                'production_schema_validation': 'not_run; application envelope is a design draft', 'dispatch_ready': False}
    (OUT / 'validation.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if args.legacy_bundle:
        build_legacy(args.legacy_bundle)
    print(json.dumps({'output': str(OUT), 'source_refs_checked': refs, 'retained_genes': 3, 'retained_gene_sets': 2, 'dispatch_ready': False}))


def build_legacy(bundle):
    """Small native-source fragment, deliberately not a cfde-inc-v2 alias."""
    data = bundle / 'data'
    wanted = ['CAD::Factor1', 'CAD::Factor8']
    metadata = list(csv.DictReader((data / 'factor_metadata.tsv').open(), delimiter='\t'))
    selected = {r['factor_id']: r for r in metadata if r['factor_id'] in wanted}
    with gzip.open(data / 'capped_entries.tsv.gz', 'rt') as stream:
        cap_audit = {(r['factor_id'], r['gene']): r for r in csv.DictReader(stream, delimiter='\t') if r['factor_id'] in wanted}
    raw_rows = {}
    with gzip.open(data / 'capped_factor_gene_loadings.tsv.gz', 'rt') as stream:
        header = next(stream).rstrip('\n').split('\t')
        for line in stream:
            factor_id = line.split('\t', 1)[0]
            if factor_id in selected:
                raw_rows[factor_id] = dict(zip(header, line.rstrip('\n').split('\t')))
            if len(raw_rows) == len(wanted): break
    fragment = {'package_version': 'reveal.evidence-package/0.1-draft',
                'prefixes': {'eaggl': 'urn:eaggl:record:', 'gene': 'urn:eaggl:legacy-711-trait-capped-union:gene:'},
                'identifier_policy': {'source_local_prefixes': ['eaggl', 'gene'],
                                      'opaque_fields': ['native_id', 'factor_id', 'row_factor_id', 'gene_set_annotations'],
                                      'interpretation': 'URNs identify source-local records; they do not establish equivalence to current CFDE or external gene authorities.'},
                'mode': 'legacy_source_fragment', 'dispatch_ready': False,
                'source_namespace': 'eaggl:legacy-711-trait-capped-union', 'cfde_inc_v2_mapping': 'not_established',
                'pigean': {'model': None, 'mechanisms': {}, 'traits': {}},
                'source_artifacts': {name: {'sha256': sha(data / name), 'upstream_relative_path': 'data/' + name}
                                     for name in ['factor_metadata.tsv', 'capped_factor_gene_loadings.tsv.gz', 'capped_entries.tsv.gz', 'analysis_summary.json']}}
    for fid in wanted:
        r = selected[fid]
        key = 'eaggl:legacy-711-trait-capped-union:' + fid
        fragment['pigean']['mechanisms'][key] = {
            'native_id': fid, 'display_name': r['label'], 'fit': {'trait': r['trait'], 'factor': r['factor'], 'model': None},
            'gene_loadings': {'status': 'ok', 'items': {'gene:' + g: {'metric': 'capped_gene_loading', 'value': float(raw_rows[fid][g]),
                                           'reported_text': raw_rows[fid][g],
                                           'source_locator': {'artifact_id': 'capped_factor_gene_loadings.tsv.gz', 'row_factor_id': fid, 'column': g}}
                              for g in r['top_genes'].split(',')}},
            'gene_set_loadings': {'status': 'not_available', 'items': {}},
            'gene_set_annotations': r['top_gene_sets'].split(','),
            'trait_loadings': {'status': 'not_available', 'items': {}},
            'metadata_row': r,
            'coverage': {'retained_genes': 5, 'source_nonzero_gene_loadings': int(r['nonzero_gene_loadings']), 'selection': 'source top_genes annotation'},
            'interpretation': 'Capped membership-like scores, not calibrated probabilities. Top-gene-set labels have no per-set loading in this metadata row.'}
        for gene, loading in fragment['pigean']['mechanisms'][key]['gene_loadings']['items'].items():
            audit = cap_audit.get((fid, gene.removeprefix('gene:')))
            if audit:
                assert float(audit['capped_loading']) == loading['value']
                loading['transformation'] = {
                    'operation': 'cap_at_one', 'original_loading': float(audit['original_loading']),
                    'source_locator': {'artifact_id': 'capped_entries.tsv.gz', 'row_factor_id': fid, 'gene': gene.removeprefix('gene:')}}
    validate_prefixes(fragment, load_schema(PIN / 'dapper.yaml'))
    write_pair(OUT.parent / 'evidence-fragment-legacy-eaggl', fragment)


if __name__ == '__main__':
    main()
