#!/usr/bin/env python3
"""Capture bounded, read-only CFDE interactive API probes and exact payloads."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = 'https://dev.cfdeknowledge.org'
MODEL = 'cfde-inc-v2'
USER_FACTOR_IDS = [
    'factor:gcat_trait:gcat_trait_language_measurement:cfde-inc-v2:Factor5',
    'factor:gcat_trait:gcat_trait_presubiculum_volume:cfde-inc-v2:Factor1',
]


def capture(root, name, route, payload=None):
    url = BASE + route
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Accept': 'application/json', 'User-Agent': 'RevealMechanisms-DesignAudit/0.2'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    start = time.monotonic()
    result = {'url': url, 'method': 'POST' if data else 'GET', 'request': payload,
              'retrieved_at': datetime.now(timezone.utc).isoformat()}
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=90) as response:
            raw = response.read()
            result.update(status=response.status, content_type=response.headers.get('Content-Type'),
                          body_sha256=hashlib.sha256(raw).hexdigest(), body_bytes=len(raw))
            try:
                result['response'] = json.loads(raw)
            except ValueError:
                result['non_json_excerpt'] = raw[:500].decode(errors='replace')
    except HTTPError as error:
        result.update(status=error.code, error_body=error.read().decode(errors='replace')[:3000])
    except (URLError, TimeoutError, OSError) as error:
        result.update(status=None, error=str(error))
    result['elapsed_seconds'] = round(time.monotonic() - start, 3)
    (root / f'{name}.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    body = result.get('response', {})
    print(json.dumps({'probe': name, 'status': result['status'], 'seconds': result['elapsed_seconds'],
                      'shape': {k: len(v) if isinstance(v, list) else type(v).__name__ for k, v in body.items()} if isinstance(body, dict) else type(body).__name__}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/interactive/2026-09-24'))
    args = parser.parse_args()
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    catalog = capture(root, 'catalog-factor-age', '/api/interactive/catalog?' + urlencode(
        {'entity_type': 'factor', 'q': 'age', 'limit': 8, 'model': MODEL}))
    if catalog.get('status') != 200:
        raise SystemExit('Catalog unavailable; connection probes require verified anchors')
    by_id = {item['node_id']: item for item in catalog['response']['items']}
    if not all(node_id in by_id for node_id in USER_FACTOR_IDS):
        raise SystemExit('Catalog changed: supplied anchors missing; do not substitute other factors')
    items = [by_id[node_id] for node_id in USER_FACTOR_IDS]
    anchors = [{k: item[k] for k in ['node_id', 'label', 'subtitle', 'node_type']} for item in items]
    base_payload = {'anchor_items': anchors, 'context': '', 'reducer': 'mean',
                    'connection_scope': 'direct', 'limit': 100,
                    'exclude_node_ids': [a['node_id'] for a in anchors], 'model': MODEL}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {target: executor.submit(capture, root, 'connections-' + target,
                   '/api/interactive/connections', {**base_payload, 'target_type': target})
                   for target in ['gene', 'gene_set', 'trait', 'factor']}
        responses = {target: future.result() for target, future in futures.items()}
    user_gene_sets = [
        'AMP_AD__all_brain__AMP_AD_MAYO_CBE_Diagnosis_OTHER-CONTROL_ALL_up___LINCS_L1000__all_signatures__LINCS_L1000_Chem_Pert_BRD-K36726853_dn',
        'AMP_AD__all_brain__AMP_AD_MAYO_CBE_SourceDiagnosis_AD-CONTROL_ALL_dn___LINCS_L1000__all_signatures__LINCS_L1000_Chem_Pert_BRD-K56119660_up',
        'AMP_AD__all_brain__AMP_AD_MAYO_CBE_SourceDiagnosis_AD-CONTROL_ALL_dn___LINCS_L1000__all_signatures__LINCS_L1000_Chem_Pert_BRD-K62226454_dn',
        'AMP_AD__all_brain__AMP_AD_MAYO_CBE_SourceDiagnosis_AD-CONTROL_ALL_dn___LINCS_L1000__all_signatures__LINCS_L1000_Chem_Pert_BRD-K99078335_dn',
        'AMP_AD__all_brain__AMP_AD_MAYO_CBE_SourceDiagnosis_AD-PATH_AGE_ALL_dn___LINCS_L1000__all_signatures__LINCS_L1000_Chem_Pert_BRD-K26122955_up',
        *['LINCS_L1000__all_signatures__LINCS_L1000_Chem_Pert_' + key for key in [
            'BRD-K16330310_dn', 'BRD-K18428988_dn', 'BRD-K20039572_up', 'BRD-K31844308_up',
            'BRD-K43624274_up', 'BRD-K54250149_up', 'BRD-K60782883_dn', 'BRD-K71561082_up', 'BRD-K81463178_dn']],
        'PsychENCODE__all_signatures__PsychENCODE_geneM12']
    capture(root, 'contextual-edges-user-example', '/api/interactive/contextual-edges',
            {'node_ids': [a['node_id'] for a in anchors] + ['gene_set:' + key for key in user_gene_sets], 'model': MODEL})
    for route, name in [('/openapi.json', 'openapi-root'), ('/api/openapi.json', 'openapi-api')]:
        capture(root, name, route)
    capture(root, 'connections-empty-anchors', '/api/interactive/connections', {**base_payload, 'anchor_items': [], 'target_type': 'gene'})
    for target in ['gene', 'trait', 'factor']:
        capture(root, 'single-factor-' + target, '/api/interactive/connections',
                {**base_payload, 'anchor_items': anchors[:1], 'target_type': target})
    control = capture(root, 'catalog-factor-t2d', '/api/interactive/catalog?' + urlencode(
        {'entity_type': 'factor', 'q': 'T2D', 'limit': 8, 'model': MODEL}))
    # This query matches CADinT2D, among others; don't call its first hit T2D.
    control_items = control.get('response', {}).get('items', [])
    comparator = next((a for a in control_items if a['node_id'] == 'factor:portal:CADinT2D:cfde-inc-v2:Factor1'), None)
    if comparator:
        for target in ['gene', 'trait', 'gene_set']:
            capture(root, 'cad-in-t2d-factor-' + target, '/api/interactive/connections',
                    {**base_payload, 'anchor_items': [comparator], 'target_type': target,
                     'limit': 8, 'exclude_node_ids': [comparator['node_id']]})
    gene_sets = responses['gene_set'].get('response', {}).get('candidates', [])
    if gene_sets:
        seed = gene_sets[0]['candidate']
        capture(root, 'geneset-direct-gene', '/api/interactive/connections',
                {**base_payload, 'anchor_items': [seed], 'target_type': 'gene',
                 'limit': 8, 'exclude_node_ids': [seed['node_id']]})
    capture(root, 'connections-invalid-target', '/api/interactive/connections',
            {**base_payload, 'target_type': 'invalid_probe'})
    capture(root, 'contextual-edges-anchors-only', '/api/interactive/contextual-edges',
            {'node_ids': [a['node_id'] for a in anchors], 'model': MODEL})


if __name__ == '__main__':
    main()
