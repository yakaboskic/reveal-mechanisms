#!/usr/bin/env python3
"""Map the user flow to the existing, validated OpenAPI exchanges."""
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'api'


def step(key, title, route, action, incoming, outgoing, cases, rules=(), **extra):
    return dict(id=key, title=title, route=route, action=action, incoming=incoming,
                outgoing=outgoing, cases=cases.split(), rules=list(rules), **extra)


_stages = json.loads((ROOT / 'scripts/api-flow-stages.json').read_text())
STEPS, SUPPORT = _stages['steps'], _stages['support']

MERMAID = '''flowchart TB
  G["1. Select DisMech gap<br/>GET /v1/knowledge-gaps/search<br/>200 KnowledgeGap + context"]
  M["2. Attach mechanisms<br/>POST /v1/mechanisms/suggest<br/>200 five EAGGL candidates total"]
  D["3. Edit or explicitly save<br/>POST /v1/drafts; PATCH /v1/drafts/{id}<br/>201/200 Draft + version"]
  A["4. Submit analysis<br/>POST /v1/jobs · kind=analysis<br/>202 Job + research_request_id"]
  J["5. Follow analysis<br/>GET /v1/jobs/{id}; GET /events<br/>200 Job / events; result.account_ids"]
  S["6. Inspect account + claims<br/>GET /v1/accounts/{id}; /claims/{id}<br/>200 DAPPER document + provenance"]
  P["7. Auto-queue paragraph<br/>POST /v1/jobs · kind=paragraph<br/>202 paragraph Job"]
  Q["8. Follow paragraph job<br/>GET /v1/jobs/{id}<br/>200 Job; result.paragraph_id"]
  T["9. Read paragraph<br/>GET /v1/paragraphs/{id}<br/>200 Paragraph + pinned citations"]
  C["10. Copy/export paragraph + citations<br/>POST /v1/citations/render; GET /v1/citations/{id}<br/>200 bibliography / citation export"]
  U["Private documents<br/>POST /v1/uploads → direct S3 upload<br/>POST /v1/uploads/{id}/complete"] -. verified originals + extracted text .-> D
  G --> M --> D --> A --> J
  J -->|accepted accounts| S
  J -->|automatic outbox fan-out| P --> Q
  Q -->|succeeded| T --> C
  I["ORCID / Google / anonymous<br/>GET /v1/me → 200 user_id"] -. trusted session for saving/submitting .-> D
  H["Resume history<br/>GET /v1/drafts; /research-requests; /jobs"] -. editable draft .-> D
  H -. prior job .-> J
  D -. 409 version conflict .-> D
  A -. 422 no EAGGL anchor .-> M
  X["Cancel either job<br/>POST /v1/jobs/{id}/cancel<br/>200 current Job"] -. best effort .-> J
  X -. best effort .-> Q
  F["Show insufficient evidence / failed / cancelled<br/>Inspect saved inputs and diagnostics"]
  J -->|other terminal state| F
  Q -->|failed or cancelled| F
  W["Worker behind the analysis job<br/>CFDE connections ×4 with frozen anchors → contextual edges<br/>bounded evidence → Claude Code + selected Proto-OKN<br/>validate/mint/save DAPPER accounts"]
  A -. asynchronous work .-> W
  W -. persisted status/results .-> J
  V["Paragraph worker<br/>saved account → Claude Code expression<br/>validate spans/revisions → mint/save Paragraph"]
  P -. asynchronous work .-> V
  V -. persisted status/results .-> Q
'''


def main():
    spec_bytes = (API / 'openapi.json').read_bytes()
    spec = json.loads(spec_bytes)
    validation = json.loads((API / 'validation.json').read_text())
    checksum = hashlib.sha256(spec_bytes).hexdigest()
    assert validation['passed'] and validation['openapi_sha256'] == checksum
    examples = {e['operation_id'] + '.' + e['case']: e for e in json.loads((API / 'examples/exchanges.json').read_text())}
    operations = {op['operationId']: op for methods in spec['paths'].values() for op in methods.values()}
    mapped = set()
    for node in STEPS + SUPPORT:
        assert node['cases']
        for key in node['cases']:
            assert key in examples, key
            mapped.add(examples[key]['operation_id'])
    assert mapped == set(operations), 'Every OpenAPI operation must appear in the flow or support paths'
    assert set(examples) == {key for n in STEPS + SUPPORT for key in n['cases']}, 'Every validated exchange must be reachable'
    operation_data = {}
    for key, op in operations.items():
        operation_data[key] = {'summary': op['summary'], 'description': op['description'], 'responses': op['responses'],
            'reference': 'index.html#/' + quote(op['tags'][0], safe='') + '/' + key,
            'request_schema': op.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')}
    mapping = {'openapi_sha256': checksum, 'steps': STEPS, 'support': SUPPORT,
               'operation_count': len(mapped), 'exchange_count': len(examples)}
    (API / 'flow-map.json').write_text(json.dumps(mapping, indent=2) + '\n')
    data = {**mapping, 'examples': examples, 'operations': operation_data}
    template = (ROOT / 'scripts/api-flow.html').read_text()
    encoded = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
    (API / 'flow.html').write_text(template.replace('<!-- FLOW_DATA -->', '<script>window.REVEAL_FLOW=' + encoded + ';</script>'))
    (API / 'flow.mmd').write_text(MERMAID)
    lines = ['# User interaction and endpoint flow', '',
        'Open [the interactive diagram](flow.html) to select any step and inspect its exact request/response or error examples. The diagram is documentation, not a running research interface.', '',
        'The ten numbered steps are the main path. Login, saved history, cancellation and recovery are supporting paths. Backend worker calls appear separately from the public REST contract.', '',
        '```mermaid', MERMAID.rstrip(), '```', '', '## Complete endpoint and exchange mapping', '']
    for i, node in enumerate(STEPS + SUPPORT):
        title = (str(i + 1) + '. ' if i < len(STEPS) else '') + node['title']
        lines += ['### ' + title, '', node['action'], '', '**Request:** ' + node['incoming'], '', '**Response:** ' + node['outgoing'], '']
        for key in node['cases']:
            e = examples[key]
            lines.append('- `' + e['method'] + ' ' + e['path_template'] + '` — [' + e['case'].replace('_', ' ') + ' request/response](examples/' + key + '.json)')
        lines += ['', *['- ' + rule for rule in node['rules']], '']
        if node.get('worker'):
            lines += ['**Worker processing behind the job:**', '', *['- ' + x for x in node['worker']], '']
    lines += ['## Evidence and validation', '',
        'Requests/responses are taken from the existing validated OpenAPI exchange library. The CADinT2D analysis/paragraph sequence is internally linked. The CAD source-selected gap now frames the request and account. The evidence package is a separate captured input; the authored account is not its validated agent output. Semantic scores and agent outputs remain illustrative fixtures.', '',
        f'The mapping covers {len(mapped)} operations and all {len(examples)} exchanges. OpenAPI SHA-256: `{checksum}`. No endpoints or payloads were changed to build this diagram.', '']
    (API / 'flow.md').write_text('\n'.join(lines))
    (API / 'flow-validation.json').write_text(json.dumps({'openapi_sha256': checksum, 'operations_mapped': len(mapped),
        'exchanges_mapped': len(examples), 'main_steps': len(STEPS), 'support_paths': len(SUPPORT),
        'all_operations_mapped': True, 'all_exchanges_reachable': True,
        'flow_html_sha256': hashlib.sha256((API / 'flow.html').read_bytes()).hexdigest(), 'passed': True}, indent=2) + '\n')
    print(f'Built interactive flow and Mermaid: {len(mapped)} operations, {len(examples)} exchanges.')


if __name__ == '__main__':
    main()
