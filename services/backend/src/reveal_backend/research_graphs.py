"""Bounded selected-graph reads shared by stateless research MCP clients.

Only the trusted OKN MCP origin is contacted. The existing hosted query builder
constructs triple queries; custom SELECT uses the shared structural validator.
Callers cannot supply endpoints, updates or federated queries.
Captures describe the time of an external observation, not an imported release.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import queue
import threading
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jsonschema import Draft202012Validator

from .auth import Problem
from .box_mcp import ENDPOINT, GRAPHS, MCPClient, PolicyError, QueryValidationError, query_arguments
from .evidence_package import canonical_json
from .research_data import CAPTURE_FORMAT, QueryCapture

VERSION = 'reveal.external-graph-query/1'
MAX_RESPONSE_BYTES = 4_000_000
READ_TIMEOUT = 35
_READ_SLOTS = threading.BoundedSemaphore(2)
_TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2000}
_GRAPH = {'type': 'string', 'enum': sorted(GRAPHS)}


def _schema(properties):
    return {'type': 'object', 'properties': {'graph': _GRAPH, **properties},
            'required': ['graph'], 'additionalProperties': False}


OPERATIONS = {
    'get_schema': {'description': 'Inspect a connected graph schema. Metadata is not biological evidence.',
                   'inputSchema': _schema({})},
    'describe_kg': {'description': 'Inspect a connected graph scope. Metadata is not biological evidence.',
                    'inputSchema': _schema({})},
    'query_graph': {'description': 'Fetch bounded triples from one connected named graph. Use absolute entity IRIs, '
        'or a schema-derived predicate and exact literal. Contains needs a bound predicate or subject. '
        'Empty or failed reads do not prove biological absence; verify entity identity and source scope.',
        'inputSchema': _schema({'subject': _TEXT, 'predicate': _TEXT, 'object': _TEXT,
            'literal': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'contains': {'type': 'string', 'minLength': 2, 'maxLength': 100},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}})},
    'sparql_query': {'description': 'Run a bounded read-only SELECT over one literal connected GRAPH. '
        'PREFIX is allowed; federation, dataset overrides and writes are unavailable. '
        'Cite exact returned bindings and query semantics; projections and aggregates are not complete triples.',
        'inputSchema': {**_schema({'query': {'type': 'string', 'minLength': 1, 'maxLength': 16000},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}), 'required': ['graph', 'query']}},
}


def definitions():
    return [{'name': name, **deepcopy(metadata)} for name, metadata in OPERATIONS.items()]


def catalog(selected_graphs=None):
    selected = set(GRAPHS if selected_graphs is None else selected_graphs)
    operations = [{'id': name, **deepcopy(metadata)} for name, metadata in OPERATIONS.items()]
    for operation in operations:
        operation['inputSchema']['properties']['graph']['enum'] = sorted(selected & GRAPHS.keys())
    return {'source_mode': 'external_kg', 'adapter_version': VERSION, 'endpoint': ENDPOINT,
            'graphs': [{'id': identity, 'named_graph': iri} for identity, iri in sorted(GRAPHS.items()) if identity in selected],
            'operations': operations}


def validate(operation, arguments, selected_graphs=None):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise Problem(422, 'INVALID_GRAPH_QUERY', 'Use a supported bounded knowledge-graph operation.')
    if not Draft202012Validator(OPERATIONS[operation]['inputSchema']).is_valid(arguments):
        raise Problem(422, 'INVALID_GRAPH_QUERY', 'Supply the bounded arguments declared for this knowledge-graph tool.')
    selected = tuple(GRAPHS) if selected_graphs is None else tuple(selected_graphs)
    if arguments['graph'] not in selected:
        raise Problem(403, 'GRAPH_NOT_SELECTED', 'This graph was not selected for the frozen research work.')
    if operation in ('query_graph', 'sparql_query'):
        try:
            if operation == 'sparql_query':
                from .research_sparql import query_arguments as select_arguments
                select_arguments(arguments, selected)
            else: query_arguments(arguments, selected)
        except (PolicyError, QueryValidationError) as error:
            raise Problem(422, 'INVALID_GRAPH_QUERY', str(error)) from None
    return deepcopy(arguments)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Knowledge-graph redirects are forbidden')


class _GraphMCPClient(MCPClient):
    """The existing protocol client, with redirect rejection and one read deadline."""
    def __init__(self, *, timeout, max_bytes, opener=None):
        super().__init__(timeout=timeout, max_bytes=max_bytes)
        self.opener = opener or build_opener(_NoRedirect())
        self.deadline = time.monotonic() + timeout
        self.remaining_bytes = max_bytes

    def rpc(self, method, params=None, notification=False):
        self.sequence += 1
        request = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notification: request['id'] = self.sequence
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': '2025-03-26', 'User-Agent': 'REVEAL-Mechanisms/1'}
        if self.session: headers['Mcp-Session-Id'] = self.session
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('Graph read deadline expired')
        with self.opener.open(Request(ENDPOINT, canonical_json(request), headers), timeout=remaining) as response:
            if response.geturl() != ENDPOINT: raise ValueError('Knowledge-graph destination changed')
            self.session = response.headers.get('Mcp-Session-Id') or self.session
            raw = response.read(self.remaining_bytes + 1)
            if len(raw) > self.remaining_bytes: raise ValueError('Knowledge-graph response exceeds byte budget')
            self.remaining_bytes -= len(raw)
            if notification or not raw: return {}
            if 'text/event-stream' in response.headers.get('Content-Type', ''):
                messages = [json.loads(line[5:].strip()) for line in raw.decode().splitlines() if line.startswith('data:')]
                data = next((item for item in messages if isinstance(item, dict) and item.get('id') == request['id']), None)
            else: data = json.loads(raw)
            if not isinstance(data, dict) or data.get('id') != request['id']:
                raise ValueError('Knowledge-graph response identity differs')
            return data


def _payloads(envelope):
    """Decode common MCP JSON wrappers while keeping the original untouched."""
    pending = [(envelope, 0)]; result = []
    while pending:
        value, depth = pending.pop(0)
        if not isinstance(value, dict): continue
        result.append(value)
        if depth >= 4: continue
        for key in ('structuredContent', 'data', 'result', 'results'):
            if isinstance(value.get(key), dict): pending.append((value[key], depth+1))
        for block in value.get('content', []) if isinstance(value.get('content'), list) else []:
            if isinstance(block, dict) and block.get('type') == 'text':
                try: pending.append((json.loads(block.get('text', '')), depth+1))
                except (ValueError, TypeError): pass
    return result


def _term(value):
    return (isinstance(value, str) and bool(value)) or (isinstance(value, dict)
        and value.get('type') in ('uri', 'bnode', 'literal', 'typed-literal')
        and isinstance(value.get('value'), str))


def _binding(value):
    return value is None or isinstance(value, (str, int, float, bool)) or _term(value)


class GraphQueryService:
    def __init__(self, client_factory=None, *, timeout=READ_TIMEOUT, max_bytes=MAX_RESPONSE_BYTES,
                 read_slots=None, clock=None):
        if not 0 < timeout <= READ_TIMEOUT or not 1000 <= max_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError('Knowledge-graph read bounds are invalid')
        self.timeout, self.max_bytes = timeout, max_bytes
        self.client_factory = client_factory or (lambda: _GraphMCPClient(timeout=timeout, max_bytes=max_bytes))
        self.read_slots = read_slots if read_slots is not None else _READ_SLOTS
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def _read(self, tool, arguments):
        if not self.read_slots.acquire(blocking=False):
            raise TimeoutError('Knowledge-graph read capacity is busy')
        outcome = queue.Queue(maxsize=1)
        def run():
            try:
                client = self.client_factory()
                outcome.put((True, client.call(tool, arguments), getattr(client, 'server_info', None)))
            except Exception as error: outcome.put((False, error, None))
            finally: self.read_slots.release()
        threading.Thread(target=run, daemon=True).start()
        try: success, value, version = outcome.get(timeout=self.timeout)
        except queue.Empty: raise TimeoutError('Knowledge-graph read deadline expired') from None
        if not success: raise value
        return value, version

    def query(self, operation, arguments, *, generation_id=None, selected_graphs=None):
        args = validate(operation, arguments, selected_graphs)
        query = operation in ('query_graph', 'sparql_query')
        upstream_tool = 'sparql_query' if query else operation
        if operation == 'sparql_query':
            from .research_sparql import query_arguments as select_arguments
            upstream = select_arguments(args, selected_graphs)
        elif operation == 'query_graph':
            upstream = query_arguments(args, tuple(GRAPHS) if selected_graphs is None else selected_graphs)
        else:
            upstream = {'shortname': args['graph'], **({'compact': True} if operation == 'get_schema' else {'long_description': True})}
        source = {'origin': ENDPOINT, 'kind': 'sparql_mcp', 'graph_id': args['graph'], 'named_graph': GRAPHS[args['graph']],
            'generation_id': None, 'adapter_version': VERSION, 'observed_at': self.clock(),
            'upstream_request': {'tool': upstream_tool, 'arguments': upstream},
            'upstream_server': None, 'upstream_release': None,
            'bounds': {'seconds': self.timeout, 'bytes': self.max_bytes, 'rows': args.get('limit', 25) if query else None}}
        result = {'items': [], 'status': 'source_unavailable', 'returned_rows': 0, 'truncated': False, 'next_cursor': None,
            'snapshot_consistency': 'not_guaranteed',
            'coverage': 'External selected-graph observation at the recorded time, separate from imported CFDE/PIGEAN/EAGGL. '
                        'A bounded query is not a complete graph search; empty or failed reads do not prove biological absence.'}
        extras = {}
        try:
            original, version = self._read(upstream_tool, upstream)
            if not isinstance(original, dict): raise ValueError('Invalid MCP tool envelope')
            original_bytes = canonical_json(original)
            if len(original_bytes) > self.max_bytes: raise ValueError('MCP envelope exceeds byte budget')
            extras['mcp-tool-result.json'] = original_bytes
            if version is not None and (not isinstance(version, dict) or len(canonical_json(version)) > 16_000):
                raise ValueError('MCP server metadata exceeds capture bounds')
            source['upstream_server'] = deepcopy(version)
            source['retained_response_format'] = 'canonical_json_mcp_tool_result'
            payloads = _payloads(original)
            if original.get('isError') or any(item.get('error') or item.get('isError') for item in payloads):
                result['reason'] = 'The connected graph returned an upstream error; no biological evidence was captured.'
            elif not query:
                result.update(status='metadata', metadata=deepcopy(original))
            else:
                candidates = [item[key] for item in payloads for key in ('rows', 'bindings') if key in item]
                if not candidates or any(not isinstance(rows, list) for rows in candidates):
                    raise ValueError('No explicit triple rows in graph response')
                rows = candidates[0]
                if any(rows != other for other in candidates[1:]): raise ValueError('Conflicting graph response rows')
                if len(rows) > args.get('limit', 25): raise ValueError('Graph response exceeded requested row limit')
                if operation == 'query_graph':
                    if any(not isinstance(row, dict) or any(not _term(row.get(key)) for key in ('s', 'p', 'o')) for row in rows):
                        raise ValueError('Graph response is not a complete triple observation')
                elif any(not isinstance(row, dict) or not row or not any(value is not None for value in row.values())
                         or any(not _binding(value) for value in row.values()) for row in rows):
                    raise ValueError('Graph response is not a valid SELECT binding observation')
                partial = bool(rows) and len(rows) == args.get('limit', 25)
                result.update(items=deepcopy(rows), status='partial' if partial else 'complete' if rows else 'empty',
                              returned_rows=len(rows), truncated=partial,
                              query_form='triples' if operation == 'query_graph' else 'select_bindings')
        except Exception as error:
            result.update(items=[], status='source_unavailable', returned_rows=0, truncated=False,
                          reason=type(error).__name__+': connected graph unavailable or response outside capture bounds.')
        envelope = {'format': CAPTURE_FORMAT, 'reader_version': VERSION, 'source_mode': 'external_kg',
                    'source': source, 'operation': operation, 'arguments': args, 'result': result}
        raw = canonical_json(envelope)
        if len(raw) > self.max_bytes:
            result.pop('metadata', None)
            result.update(items=[], status='source_unavailable', returned_rows=0, truncated=False,
                          reason='The normalized graph capture exceeds its byte budget.')
            raw = canonical_json(envelope)
        if len(raw) > self.max_bytes:
            raise Problem(413, 'GRAPH_CAPTURE_TOO_LARGE', 'The graph capture exceeds its byte budget.')
        return QueryCapture(result, raw, 'external_kg', source, extras)
