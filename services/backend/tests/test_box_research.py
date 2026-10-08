"""Real MCP SDK transport, fake scientific data: no upstream/model/Box execution."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server

from reveal_backend.agent_execution import ExecutionRequest
from reveal_backend.box_research import ALLOWED_TOOLS, HostedResearchClient, ResearchAccessError, canonical, network_policy, validate_access, validate_hosted_context
from reveal_backend.box_mcp import Ledger, ScopedTools
from reveal_backend.dispatch_view import research_prompt, file_input_manifest
from reveal_backend.evidence_package import canonical_json
from reveal_backend.research_tools import definitions
from reveal_backend import box_remote

CONTEXT = {'local_work_id': 'hosted-work', 'research_request_id': 'request', 'mcp_url': 'http://127.0.0.1:18000/mcp'}
TOKEN = 'only-root-owns-this-fixture-token'


class Response(io.BytesIO):
    def __init__(self, value, url): super().__init__(value); self.url = url
    def geturl(self): return self.url


@pytest.fixture
def hosted(tmp_path):
    raw = b'{"rows":[{"gene":"TEST1","loading":0.125}]}\n'
    source = {'path': 'sources/observations.json', 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw), 'format': 'json', 'dapper_file_id': 'dapper:File.exact'}
    seed = {'retrieval_mode': 'progressive', 'seed_version': 'reveal.research-seed/1', 'research_request_id': 'request', 'research_context': CONTEXT,
        'selection': {'knowledge_gap_id': 'dapper:KnowledgeGap.gap'}, 'source_artifacts': {}, 'dapper_context': {}, 'external_evidence': {'selected_graphs': []}}
    seed_path = tmp_path/'seed.json'; seed_path.write_bytes(canonical_json(seed))
    package = {**seed, 'source_artifacts': {'evidence': source}, 'dapper_context': {'files': [{'id': source['dapper_file_id'], 'sha256': source['sha256']}]},
               'validation_context': {'format': 'reveal.validation-context/1', 'eligible_source_ids': [source['dapper_file_id']]}}
    export = {'package': package, 'package_sha256': hashlib.sha256(canonical_json(package)).hexdigest(), 'seed_sha256': hashlib.sha256(seed_path.read_bytes()).hexdigest(),
        'artifacts': [{'id': 'artifact', 'filename': source['path'], 'sha256': source['sha256'], 'size_bytes': len(raw)}], 'manifest': {'format': 'reveal.context-manifest/1'}}
    state = {'calls': [], 'polls': 0, 'artifact': raw, 'export': export, 'fail': False}
    contracts = {item['name']: item for item in definitions()}
    async def list_tools(context, params):
        assert context.request.headers['authorization'] == 'Bearer '+TOKEN
        return types.ListToolsResult(tools=[types.Tool(**item) for item in contracts.values()])
    async def call_tool(context, params):
        assert context.request.headers['authorization'] == 'Bearer '+TOKEN
        arguments = params.arguments or {}; name = params.name
        Draft202012Validator(contracts[name]['inputSchema']).validate(arguments)
        for key in ('research_request_id', 'local_work_id'):
            if key in arguments: assert arguments[key] == CONTEXT[key]
        state['calls'].append((name, arguments))
        if name == 'get_factor': value = {'operation_id': 'op', 'state': 'received'}
        elif name == 'get_operation':
            state['polls'] += 1
            value = {'id': 'op', 'state': 'failed' if state['fail'] else 'running' if state['polls'] == 1 else 'succeeded', 'result': {'receipt_id': 'receipt', 'result': {'items': [{'gene': 'TEST1'}]}}}
        elif name == 'reuse_scientific_objects': value = {'reuse_receipt_id': 'reuse', 'id': 'reuse', 'selections': [{'purpose': 'existing_account', 'selection': {'object_id': 'dapper:ScientificAccount.prior'}}]}
        elif name == 'export_evidence_context': value = state['export']
        elif name == 'get_local_work': value = {'id': CONTEXT['local_work_id'], 'state': 'ready'}
        else: value = {}
        return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(value))], structuredContent=value)
    server = Server('shared-research-fixture', on_list_tools=list_tools, on_call_tool=call_tool,
        get_tool_input_schema=lambda name: contracts.get(name, {}).get('inputSchema'))
    transport = server.streamable_http_app(json_response=True, stateless_http=True)
    with TestClient(transport, base_url='http://127.0.0.1:18000') as http:
        class Opener:
            def open(self, request, timeout):
                if request.full_url.endswith('/v1/research-artifacts/artifact/content'):
                    assert request.get_header('Authorization') == 'Bearer '+TOKEN
                    return Response(state['artifact'], request.full_url)
                response = http.request(request.method, request.full_url, headers=dict(request.header_items()), content=request.data)
                if response.status_code >= 400: raise HTTPError(request.full_url, response.status_code, 'fixture', {}, None)
                return Response(response.content, str(response.url))
        research = HostedResearchClient(CONTEXT, TOKEN, tmp_path/'ledger/research-context', seed_path=seed_path, execution_id='job:1', opener=Opener())
        yield research, state, tmp_path


def test_shared_sdk_proxy_polls_captures_reuses_and_exports_without_disclosing_credential(hosted):
    research, state, root = hosted
    schemas = research.definitions()
    assert {'get_factor', 'find_claims', 'reuse_scientific_objects'} <= {item['name'] for item in schemas}
    assert not {'submit_accounts', 'validate_submission', 'prepare_artifact_upload'} & {item['name'] for item in schemas}
    assert all(not {'local_work_id', 'research_request_id', 'idempotency_key'} & set(item['inputSchema']['properties']) for item in schemas)
    value = research.call('get_factor', {'arguments': {'factor_id': 'factor:study:trait:model:Factor1'}})
    assert research.payload(value)['state'] == 'received' and state['polls'] == 0
    assert research.payload(value)['recovery'] == {'tool': 'get_operation', 'arguments': {'operation_id': 'op'}}
    research.call('get_operation', {'operation_id': 'op'})
    value = research.call('get_operation', {'operation_id': 'op'})
    assert research.payload(value)['state'] == 'succeeded' and state['polls'] == 2
    research.call('reuse_scientific_objects', {'selections': [{'object_id': 'dapper:ScientificAccount.prior', 'purpose': 'existing_account'}]})
    assert research.receipt_ids == {'receipt'} and research.reuse_receipt_ids == {'reuse'}
    assert research.existing_account_ids == {'dapper:ScientificAccount.prior'}
    path = research.materialize()
    assert path.read_bytes() == canonical_json(state['export']['package'])
    assert (path.parent/'sources/observations.json').read_bytes() == state['artifact']
    assert research.materialize() == path, 'Repeated lint uses unchanged closed sources'
    assert all(TOKEN.encode() not in file.read_bytes() for file in root.rglob('*') if file.is_file())
    capture = json.loads((root/'ledger/research-receipts.json').read_bytes())
    assert capture['receipt_ids'] == ['receipt'] and capture['reuse_receipt_ids'] == ['reuse']
    query = next(arguments for name, arguments in state['calls'] if name == 'get_factor')
    assert query['research_request_id'] == 'request' and query['idempotency_key'].startswith('hosted-')


def test_proxy_rejects_scope_changes_submission_credentials_and_broken_capture(hosted):
    research, state, _ = hosted
    research.definitions()
    for tool, arguments in [('get_factor', {'local_work_id': 'other'}), ('submit_accounts', {}), ('get_factor', {'arguments': {'q': TOKEN}})]:
        with pytest.raises(ResearchAccessError): research.call(tool, arguments)
    state['artifact'] = b'tampered'
    with pytest.raises(ResearchAccessError, match='Downloaded evidence bytes differ'): research.materialize()
    state['artifact'] = TOKEN.encode()
    with pytest.raises(ResearchAccessError, match='credential material'): research.materialize()


def test_hosted_catalog_selects_private_variant_and_hides_remote_oauth(hosted):
    research, state, _ = hosted
    from reveal_backend.research_tools import private_definitions
    original = {item['name']: item for item in private_definitions()}
    catalog = research.definitions()
    for tool in catalog:
        name = tool['name']
        assert tool['description'] == original[name]['description']
        assert 'securitySchemes' not in tool
        assert 'securitySchemes' not in tool.get('_meta', {})
        assert 'anyOf' not in tool['inputSchema']
        assert 'reference_generation_id' not in tool['inputSchema']['properties']
        assert research._schemas[name] == original[name]['inputSchema']
    factor = next(tool for tool in catalog if tool['name'] == 'get_factor')
    arguments = {'arguments': {'factor_id': 'factor'}}
    validator = Draft202012Validator(factor['inputSchema'])
    assert validator.is_valid(arguments)
    assert not validator.is_valid({**arguments, 'reference_generation_id': 'another-generation'})
    with pytest.raises(ResearchAccessError, match='frozen generation'):
        research.call('get_factor', {**arguments, 'reference_generation_id': 'another-generation'})
    research.call('get_factor', arguments)
    called = next(args for name, args in state['calls'] if name == 'get_factor')
    assert called['research_request_id'] == CONTEXT['research_request_id']
    assert called['idempotency_key'].startswith('hosted-')
    assert 'reference_generation_id' not in called


def test_gene_gene_sets_is_registered_on_every_research_surface(hosted):
    from reveal_backend import research_public, research_tools
    from reveal_backend.public_tool_activity import ARGUMENTS, DURABLE_TOOLS
    research, state, _ = hosted
    name = 'get_gene_gene_sets'
    for tools in (research_tools.private_definitions(), research_public.definitions(), research_tools.definitions(), research.definitions()):
        tool = next(item for item in tools if item['name'] == name)
        assert 'stored membership contains the gene' in tool['description'] and 'factor_id' in tool['description']
    hosted_tool = next(item for item in research.definitions() if item['name'] == name)
    assert set(hosted_tool['inputSchema']['properties']['arguments']['properties']) == {'gene', 'factor_id', 'limit', 'cursor'}
    assert name in ALLOWED_TOOLS and ARGUMENTS[name] == ('arguments',) and name in DURABLE_TOOLS
    research.call(name, {'arguments': {'gene': 'INS', 'factor_id': 'factor'}})
    called = next(args for tool, args in state['calls'] if tool == name)
    assert called['arguments'] == {'gene': 'INS', 'factor_id': 'factor'}
    assert called['research_request_id'] == CONTEXT['research_request_id']


def test_failed_operation_is_tool_error_without_an_evidence_receipt(hosted):
    research, state, _ = hosted
    state['fail'] = True
    research.call('get_factor', {'arguments': {'factor_id': 'factor'}})
    response = research.call('get_operation', {'operation_id': 'op'})
    assert response['isError'] and not research.receipt_ids


def test_shared_reference_budget_does_not_consume_selected_graph_or_authoring_allowance(hosted):
    research, _, root = hosted
    ledger = Ledger(root/'tool-ledger', 'job', 1, secrets=(TOKEN,))
    proxy = ScopedTools([], ledger, research=research, max_calls=1, max_research_calls=2,
                        write_draft=lambda name, document: {'content': []})
    assert not proxy.call('get_local_work', {}).get('isError')
    assert not proxy.call('get_local_work', {}).get('isError')
    assert not proxy.call('write_account_draft', {'filename': 'account-1.json', 'document': {}}).get('isError')
    assert proxy.call('get_local_work', {})['isError']
    ledger.freeze()
    assert TOKEN not in json.dumps(ledger.entries)


def test_writer_linter_and_reuse_only_outcome_use_trusted_materialized_context(hosted, monkeypatch):
    monkeypatch.setattr('reveal_backend.authoring_structure.preflight_document', lambda *a, **k: {'valid': True})
    research, state, root = hosted
    research.call('reuse_scientific_objects', {'selections': [{'object_id': 'dapper:ScientificAccount.prior', 'purpose': 'existing_account'}]})
    output = root/'output'; output.mkdir()
    runtime_dir = root/'state'; runtime_dir.mkdir()
    runtime = {'evidence_package': str(research.seed_path), 'dapper_root': 'fixture-dapper'}
    (runtime_dir/'runtime.json').write_text(json.dumps(runtime))
    monkeypatch.setattr(box_remote, 'RESEARCH', research)
    monkeypatch.setattr(box_remote, 'BASE', root)
    monkeypatch.setattr(box_remote, 'STATE', runtime_dir)
    monkeypatch.setattr(box_remote, 'OUTPUT', output)
    monkeypatch.setattr(box_remote.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    box_remote.write_draft_tool('account-1.json', {'scientific_accounts': [{'id': 'new', 'was_derived_from': ['dapper:File.exact']}]})
    document = json.loads((output/'account-1.json').read_bytes())
    assert document['files'][0]['id'] == 'dapper:File.exact'
    observed = []
    import reveal_backend.scientific_account_lint as lint
    monkeypatch.setattr(lint, 'lint_scientific_account', lambda filename, **kwargs: observed.append(kwargs) or {'valid': True, 'findings': []})
    assert not box_remote.lint_tool('account-1.json', Ledger(root/'lint-ledger', 'job', 1))['isError']
    assert Path(observed[0]['evidence_package']).read_bytes() == canonical_json(state['export']['package'])
    box_remote.write_outcome_tool({'format': 'reveal.research-outcome/1', 'status': 'succeeded', 'existing_account_ids': ['dapper:ScientificAccount.prior'], 'reuse_receipt_ids': ['reuse']})
    outcome = json.loads((output/'outcome.json').read_bytes())
    assert outcome['existing_account_ids'] == ['dapper:ScientificAccount.prior'] and outcome['reuse_receipt_ids'] == ['reuse']
    with pytest.raises(box_remote.DraftValidationError): box_remote.write_outcome_tool({**outcome, 'existing_account_ids': ['not-authorized']})


def test_context_and_secret_are_distinct_and_progressive_prompt_is_explicit(tmp_path):
    assert validate_access({**CONTEXT, 'token': TOKEN}, CONTEXT) == TOKEN
    with pytest.raises(ResearchAccessError): validate_access({**CONTEXT, 'token': TOKEN, 'local_work_id': 'other'}, CONTEXT)
    assert network_policy({**CONTEXT, 'mcp_url': 'https://backend.example.test/mcp'})['allowed_domains'][-1] == 'backend.example.test'
    request = ExecutionRequest('job', 1, 'research', tmp_path/'seed.json', tmp_path/'out', research_access={**CONTEXT, 'token': TOKEN})
    assert TOKEN not in repr(request)
    prompt = research_prompt([], progressive=True)
    assert 'absence is advisory' in prompt and 'reuse_receipt_ids' in prompt and 'small/sigma2' in prompt
    seed = {'retrieval_mode': 'progressive', 'external_evidence': {'selected_graphs': []}}
    assert file_input_manifest(canonical_json(seed))['prompt_sha256'] == hashlib.sha256(prompt.encode()).hexdigest()


def test_hosted_bundle_rejects_local_only_mcp_before_remote_creation(tmp_path):
    from reveal_backend.box_adapter import make_bundle
    root = Path(__file__).resolve().parents[3]
    path = tmp_path/'seed.json'
    for endpoint in ('http://127.0.0.1:8000/mcp', 'https://127.0.0.2/mcp', 'https://[::1]/mcp',
                     'https://localhost/mcp', 'https://backend.localhost./mcp', 'https://0.0.0.0/mcp'):
        context = {**CONTEXT, 'mcp_url': endpoint}
        path.write_text(json.dumps({'retrieval_mode': 'progressive', 'research_context': context}))
        with pytest.raises(ResearchAccessError, match='HTTPS URL reachable'):
            make_bundle(root, ExecutionRequest('job', 1, 'research', path, tmp_path/'out'))
    assert validate_hosted_context({**CONTEXT, 'mcp_url': 'https://backend.example.test/mcp'})['mcp_url'].startswith('https://')


def test_frozen_receipt_capture_cannot_be_changed_by_late_calls(hosted):
    research, _, root = hosted
    research.call('get_factor', {'arguments': {'factor_id': 'factor'}})
    research.freeze()
    path = root/'ledger/research-receipts.json'
    before = path.read_bytes()
    research._observe({'receipt_id': 'too-late'})
    assert path.read_bytes() == before
    with pytest.raises(ResearchAccessError, match='ended'): research.call('get_local_work', {})
    with pytest.raises(ResearchAccessError, match='ended'): research.materialize()


def test_hosted_polls_v2_export_and_replays_same_complete_context(hosted):
    research, state, root = hosted
    legacy = state['export']; package_raw = canonical_json(legacy['package'])
    descriptor = {'id': 'package-v2', 'artifact_id': 'package-v2', 'filename': 'evidence-package.json',
        'path': 'evidence-package.json', 'sha256': hashlib.sha256(package_raw).hexdigest(), 'size_bytes': len(package_raw)}
    exported = {**legacy, 'format': 'reveal.validation-context-export/2', 'package_artifact': descriptor,
        'context_sha256': hashlib.sha256(canonical_json(legacy['package']['validation_context'])).hexdigest(),
        'artifacts': [*legacy['artifacts'], descriptor]}
    exported.pop('package')
    original_invoke = research._invoke; original_http = research._http
    polls = []; downloads = []
    def invoke(tool, arguments):
        if tool == 'export_evidence_context': return {'structuredContent': {'operation_id': 'export-v2', 'state': 'received'}}
        if tool == 'get_operation' and arguments.get('operation_id') == 'export-v2':
            polls.append(True)
            return {'structuredContent': {'id': 'export-v2', 'state': 'running' if len(polls) == 1 else 'succeeded', 'result': exported}}
        return original_invoke(tool, arguments)
    def http(url, body=None, maximum=10_000_000):
        if url.endswith('/package-v2/content'):
            downloads.append(url); return package_raw
        return original_http(url, body, maximum)
    research._invoke = invoke; research._http = http
    path = research.materialize()
    assert path.read_bytes() == package_raw and len(polls) == 2
    assert research.materialize() == path and len(downloads) == 1
    recovered = HostedResearchClient(CONTEXT, TOKEN, research.root, seed_path=research.seed_path,
        execution_id='job:1', opener=research._opener)
    assert recovered.receipt_ids == research.receipt_ids


def test_hosted_v2_package_descriptor_size_is_bounded_before_download(hosted):
    research, state, _ = hosted
    legacy = state['export']; checksum = legacy['package_sha256']
    for size in (-1, 0, True, 8_000_001, '123'):
        state['export'] = {'format': 'reveal.validation-context-export/2', 'package_sha256': checksum,
            'package_artifact': {'id': 'too-large', 'sha256': checksum, 'size_bytes': size}}
        with pytest.raises(ResearchAccessError, match='package size exceeds bound'):
            research.materialize()
