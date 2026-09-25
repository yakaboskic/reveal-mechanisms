#!/usr/bin/env python3
"""Bundle the HTML design with source-derived records; no network or DB calls."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'design'
PIN = ROOT / 'data/dapper/2026-09-24-v8/snapshot/schema'
sys.path[:0] = [str(PIN / 'identity'), str(PIN)]
from dapper_identity import compute_id, load_schema
from build_account_example import main as build_account_example, GAP_SOURCE

def read(p):
    return json.loads(p.read_text())

def response(name):
    return next(iter(read(ROOT / f'api/examples/{name}.json')['responses']['200']['examples'].values()))

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    build_account_example()
    datadir = OUT / 'data'
    datadir.mkdir(parents=True, exist_ok=True)
    source = Path('/Users/cyakaboski/src/research/dapper/.build/exports/geneset-hubmap-corrected/geneset.provenance.corrected.yaml')
    frozen = datadir / 'hubmap.provenance.yaml'
    manifest_path = datadir / 'manifest.json'
    if not frozen.exists():
        frozen.write_bytes(source.read_bytes())
        (datadir / 'hubmap.README.md').write_bytes(source.with_name('README.md').read_bytes())
        manifest_path.write_text(json.dumps({'hubmap_source': str(source), 'hubmap_sha256': sha(frozen),
            'captured_on': '2026-09-25', 'refresh_policy': 'Frozen; explicitly replace to refresh.'}, indent=2)+'\n')
    manifest = read(manifest_path)
    assert sha(frozen) == manifest['hubmap_sha256']
    sv = load_schema(PIN / 'dapper.yaml')
    with gzip.open(ROOT / 'data/dismech-gaps/2026-09-24/knowledge-gaps.jsonl.gz', 'rt') as f:
        all_gaps = [json.loads(line) for line in f]
    selected = []
    for topic in ['aip-related', 'type 2 diabetes', 'alzheimer']:
        selected.extend([r for r in all_gaps if r['kind'] == 'KNOWLEDGE_GAP'
                         and topic in r['document_name'].lower()][:16])
    selected.append(next(r for r in all_gaps if r['id'] == GAP_SOURCE))
    selected_ids = {r['id'] for r in selected}
    with gzip.open(ROOT / 'data/dismech-gaps/2026-09-24/gap-attachments.jsonl.gz', 'rt') as f:
        attachments = [json.loads(line) for line in f]
    attachments = [r for r in attachments if r['gap_id'] in selected_ids]
    gaps = []
    for row in selected:
        raw = row['raw']
        node = {'text': raw['prompt'], 'gap_description': raw.get('rationale') or raw['prompt'],
                'gap_kind': row['kind'], 'scope': row['document_name']}
        term = ((row.get('disease_term') or {}).get('term') or {}).get('id')
        if term and term.startswith('MONDO:'):
            node['about_entities'] = ['http://purl.obolibrary.org/obo/' + term.replace(':', '_')]
        node['id'] = compute_id(node, 'KnowledgeGap', sv)
        gaps.append({'object': node, 'source_record': row,
                     'attachments': [a for a in attachments if a['gap_id'] == row['id']]})
    # The pinned API example retains exactly its existing digest and projection.
    aip = response('getKnowledgeGap.request')
    matching = next(g for g in gaps if g['object']['id'] == aip['object']['id'])
    matching['api_record'] = aip
    data = {
        'gaps': gaps, 'aip_id': aip['object']['id'],
        'factors': [x['factor'] for x in response('suggestMechanisms.dismech_context')['automatic_anchors']],
        'suggestion_example': response('suggestMechanisms.dismech_context'),
        'account_document': read(datadir / 'cad-account/scientific-account.json'),
        'account_view': read(datadir / 'cad-account/presentation.json'),
        'paragraph_document': read(datadir / 'cad-account/paragraph.json'),
        'citation_registry': read(datadir / 'cad-account/citation-registry.json'),
        'paragraph_rich_text': read(datadir / 'cad-account/rich-text.json'),
        'graph': read(ROOT / 'api/examples/cfde-response.json'),
        'hubmap': yaml.safe_load(frozen.read_text()),
        'exchanges': {key: read(ROOT / f'api/examples/{key}.json') for key in [
            'createDraft.question_and_anchor', 'updateDraft.save_revision_two', 'createJob.analysis',
            'createJob.paragraph', 'getJobEvents.request', 'getAccount.request', 'getClaim.request',
            'getGeneSet.request', 'getKnowledgeGap.request', 'getCitation.bibtex', 'getCitation.apa']},
        'manifest': {**manifest, 'gap_sample_count': len(gaps),
                     'source_gap_count': len(all_gaps), 'openapi_sha256': sha(ROOT / 'api/openapi.json'),
                     'dismech_snapshot': '2026-09-24', 'dapper_schema': '2026-09-24-v8',
                     'account_fixture_sha256': sha(datadir / 'cad-account/scientific-account.json')}
    }
    assert len(data['factors']) == 5
    hub = data['hubmap']; collection = hub['gene_set_collections'][0]
    assert set(collection['members']) == {s['id'] for s in hub['gene_sets']}
    assert len(hub['gene_sets']) == 358
    assert len(set(m for s in hub['gene_sets'] for m in s['members'])) == collection['n_genes'] == 964
    nodes = {r['id']: r for value in hub.values() if isinstance(value, list) for r in value if 'id' in r}
    for edge in hub['used_edges'] + hub['was_generated_by_edges']:
        assert edge['subject'] in nodes and edge['object'] in nodes
    for s in hub['gene_sets']:
        assert s['in_gmt_file'] in nodes and s['gmt_entry'] == s['id']
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    template = (OUT / 'prototype.html').read_text()
    assert template.count('/*__DATA__*/') == 1
    assert template.count('/*__ACCOUNT_VIEW__*/') == template.count('/*__ACCOUNT_STYLE__*/') == 1
    template = template.replace('/*__ACCOUNT_VIEW__*/', (OUT / 'account-view.js').read_text())
    template = template.replace('/*__ACCOUNT_STYLE__*/', (OUT / 'account-view.css').read_text())
    assert template.count('/*__WORKSPACE_VIEW__*/') == template.count('/*__WORKSPACE_STYLE__*/') == 1
    template = template.replace('/*__WORKSPACE_VIEW__*/', (OUT / 'workspace-view.js').read_text())
    template = template.replace('/*__WORKSPACE_STYLE__*/', (OUT / 'workspace-view.css').read_text())
    assert template.count('/*__POLISH_STYLE__*/') == 1
    template = template.replace('/*__POLISH_STYLE__*/', (OUT / 'polish.css').read_text())
    (OUT / 'index.html').write_text(template.replace('/*__DATA__*/', encoded))
    (datadir / 'build-review.json').write_text(json.dumps({
        'data_integrity_checks': 'passed', 'gap_sample_count': len(gaps), 'hubmap_nodes': len(nodes),
        'hubmap_edges': len(hub['used_edges']) + len(hub['was_generated_by_edges']),
        'hubmap_sets': 358, 'hubmap_gene_union': 964,
        'limitations': ['No schema migration, login or agent was executed.',
                       'HuBMAP IDs preserved from corrected export; not re-minted under the API pin.',
                       'Source paths are recorded locations, not public download links.']}, indent=2)+'\n')
    print(f'Built design/index.html ({len(encoded):,} embedded JSON characters; {len(gaps)} real gaps)')

if __name__ == '__main__':
    main()
