"""Mode-specific bounds survive offline, authenticated and hosted tool adapters."""
import socket
from unittest.mock import patch

from jsonschema import Draft202012Validator
import pytest

from reveal_backend.box_mcp import Ledger, ScopedTools
from reveal_backend.evidence_package import canonical_json
from reveal_backend.evidence_reader import EvidenceReadError, TOOL_DEFINITION, WorkspaceReader, digest
from reveal_backend.research_tools import definitions, private_definitions
from test_box_research import hosted


def test_mode_bounds_survive_private_and_hosted_schema_wrapping(hosted):
    proxy, _, _ = hosted
    remote = {tool['name']: tool for tool in proxy.definitions()}['read_evidence']['inputSchema']
    schemas = [TOOL_DEFINITION['inputSchema'], remote]
    for catalog in (private_definitions(), definitions()):
        selected = next(tool['inputSchema'] for tool in catalog if tool['name'] == 'read_evidence')
        assert selected['allOf'] == TOOL_DEFINITION['inputSchema']['allOf']
        schemas.append(selected)
    for mode, maximum in ((None, 100), ('json', 100), ('text', 100), ('text_range', 16000)):
        for schema in schemas:
            args = {'artifact_id': 'capture', 'sha256': 'a' * 64}
            if 'local_work_id' in schema['required']: args['local_work_id'] = 'work'
            if mode is not None: args['mode'] = mode
            validator = Draft202012Validator(schema)
            assert validator.is_valid(args), 'Omitting limit must retain the runtime default of20'
            assert schema['properties']['limit']['default'] == 20
            for limit in (1, maximum):
                assert validator.is_valid({**args, 'limit': limit})
            for limit in (0, maximum + 1, True, 1.5):
                assert not validator.is_valid({**args, 'limit': limit}), (mode, limit)
    assert remote['allOf'] == TOOL_DEFINITION['inputSchema']['allOf']


def test_offline_and_hosted_reads_have_identical_bounds_and_repair_messages(tmp_path):
    inputs = tmp_path / 'input'; (inputs / 'sources').mkdir(parents=True)
    raw_sources = {'json': canonical_json(list(range(120))), 'text': b'line\n' * 120,
                   'text_range': b'x' * 17000}
    sources = {}
    for mode, raw in raw_sources.items():
        descriptor = {'path': 'sources/' + digest(raw) + ('.json' if mode == 'json' else '.text'),
                      'sha256': digest(raw), 'size_bytes': len(raw), 'format': 'json' if mode == 'json' else 'text'}
        sources[mode] = descriptor
        (inputs / descriptor['path']).write_bytes(raw)
    package = canonical_json({'source_artifacts': sources})
    (inputs / 'evidence-package.json').write_bytes(package)
    (inputs / 'manifest.json').write_bytes(canonical_json({'package_sha256': digest(package)}))
    reader = WorkspaceReader(tmp_path)
    tools = ScopedTools([], Ledger(tmp_path / 'ledger', 'fixture', 1), read_evidence=reader.read)
    advertised = next(tool for tool in tools.definitions() if tool['name'] == 'read_evidence')
    assert advertised == TOOL_DEFINITION
    with patch.object(socket, 'socket', side_effect=AssertionError('Offline reading must not use network')):
        for mode, maximum in (('json', 100), ('text', 100), ('text_range', 16000)):
            args = {'artifact_id': mode, 'sha256': sources[mode]['sha256'], 'mode': mode}
            for changes in ({}, {'limit': maximum}):
                local = reader.read(**args, **changes)
                hosted_result = tools.call('read_evidence', {**args, **changes})
                assert hosted_result['structuredContent'] == local
                assert local['returned'] == changes.get('limit', 20)
            for limit in (maximum + 1, 0, True):
                with pytest.raises(EvidenceReadError) as error:
                    reader.read(**args, limit=limit)
                message = str(error.value)
                assert f'mode={mode}' in message and f'1 to {maximum}' in message and 'limit=20' in message
                hosted_result = tools.call('read_evidence', {**args, 'limit': limit})
                assert hosted_result['isError']
                assert hosted_result['content'][0]['text'] == message
        with pytest.raises(EvidenceReadError, match='zero-based nonnegative integer'):
            reader.read(artifact_id='json', sha256=sources['json']['sha256'], offset=-1)
        # The omitted mode is json, including its 100-entry upper bound.
        with pytest.raises(EvidenceReadError, match='mode=json.*1 to 100'):
            reader.read(artifact_id='json', sha256=sources['json']['sha256'], limit=101)
