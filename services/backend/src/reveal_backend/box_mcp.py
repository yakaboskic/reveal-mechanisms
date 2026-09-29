"""Trusted, narrow MCP proxy with complete append-only request/result artifacts.

No arbitrary SPARQL is exposed: the proxy constructs bounded SELECT queries in
one selected literal named graph. It runs as root; Claude runs as another uid.
"""
from __future__ import annotations
import hashlib
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .box_literature import LiteratureInputError, request_spec

GRAPHS = {'biomarkerkg': 'https://purl.org/okn/frink/kg/biomarkerkg',
          'prokn': 'https://purl.org/okn/frink/kg/prokn'}
ENDPOINT = 'https://apps.okn.us/okn-mcp-dev/mcp'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def stamp():
    return datetime.now(timezone.utc).isoformat()


class PolicyError(ValueError):
    pass


class DraftValidationError(ValueError):
    """Ordinary authoring feedback; not an attempted policy violation."""


class QueryValidationError(ValueError):
    """Correctable query-shape feedback, not a graph access violation."""


class EvidenceReadTimeout(TimeoutError):
    """The bounded upstream read did not complete; it is not empty evidence."""


class MCPClient:
    def __init__(self, endpoint=ENDPOINT, timeout=40, max_bytes=4_000_000):
        if endpoint != ENDPOINT:
            raise PolicyError('MCP endpoint must match the trusted endpoint')
        self.endpoint, self.timeout, self.max_bytes = endpoint, timeout, max_bytes
        self.session = None
        self.server_info = None
        self.sequence = 0

    def rpc(self, method, params=None, notification=False):
        self.sequence += 1
        request = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notification:
            request['id'] = self.sequence
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': '2025-03-26', 'User-Agent': 'REVEAL-Mechanisms/1'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        with urllib.request.urlopen(urllib.request.Request(self.endpoint, canonical(request), headers), timeout=self.timeout) as response:
            self.session = response.headers.get('Mcp-Session-Id') or self.session
            raw = response.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise PolicyError('MCP response exceeds capture limit; result rejected')
            if notification or not raw:
                return {}
            if 'text/event-stream' in response.headers.get('Content-Type', ''):
                candidates = [json.loads(line[5:].strip()) for line in raw.decode().splitlines() if line.startswith('data:')]
                data = next((x for x in candidates if x.get('id') == request['id']), None)
                if data is None:
                    raise ValueError('MCP stream has no matching response')
            else:
                data = json.loads(raw)
            return data

    def initialize(self):
        result = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                        'clientInfo': {'name': 'reveal-scoped-proxy', 'version': '1'}})
        if 'error' in result:
            raise ValueError('MCP initialization failed')
        self.rpc('notifications/initialized', notification=True)
        self.server_info = result.get('result', {}).get('serverInfo', {})
        return self.server_info

    def call(self, tool, arguments):
        if self.session is None:
            self.initialize()
        result = self.rpc('tools/call', {'name': tool, 'arguments': arguments})
        if 'error' in result:
            return {'isError': True, 'content': [{'type': 'text', 'text': json.dumps(result['error'])}]}
        if not isinstance(result.get('result'), dict):
            raise ValueError('MCP response has no tool result')
        return result['result']


class Ledger:
    """Owned by the trusted runner. Starts a durable entry before external I/O."""
    def __init__(self, root: Path, job_id: str, attempt: int, secrets=()):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.job_id, self.attempt = job_id, attempt
        self.entries = []
        self.lock = threading.RLock()
        self.secrets = tuple(s for s in secrets if s)
        self.frozen = False

    def sanitized_bytes(self, value):
        raw = canonical(value)
        count = 0
        for secret in self.secrets:
            count += raw.count(secret.encode())
            raw = raw.replace(secret.encode(), b'[REDACTED_CREDENTIAL]')
        return raw, count

    def artifact(self, value):
        raw, redactions = self.sanitized_bytes(value)
        return self.artifact_bytes(raw, redactions=redactions)

    def artifact_bytes(self, raw, format='json', redactions=0):
        if format not in ('json', 'xml'): raise ValueError('Unsupported capture format')
        for secret in self.secrets:
            redactions += raw.count(secret.encode())
            raw = raw.replace(secret.encode(), b'[REDACTED_CREDENTIAL]')
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / 'artifacts' / (digest + '.' + format)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        return {'path': str(path.relative_to(self.root)), 'sha256': digest, 'size_bytes': len(raw), 'credential_redactions': redactions}

    def append(self, event):
        with (self.root / 'events.jsonl').open('ab') as handle:
            handle.write(self.sanitized_bytes(event)[0] + b'\n')
            handle.flush()
            import os
            os.fsync(handle.fileno())

    def start(self, tool, arguments, graph):
        with self.lock:
            if self.frozen:
                raise PolicyError('Execution ledger is already frozen')
            call_id = len(self.entries) + 1
            entry = {'job_id': self.job_id, 'attempt': self.attempt, 'sequence': call_id,
                     'tool': tool, 'selected_graph': graph, 'started_at': stamp(), 'status': 'pending',
                     'request': self.artifact({'tool': tool, 'arguments': arguments}),
                     'public_display': {'tool': tool, 'graph': graph}, 'source_version': None, 'locators': []}
            self.entries.append(entry)
            self.append({'phase': 'started', **entry})
            return entry

    def finish(self, entry, result, status):
        with self.lock:
            if self.frozen or entry['status'] != 'pending':
                return  # Cancellation/freeze already recorded its final outcome.
            entry.update(completed_at=stamp(), status=status, response=self.artifact(result))
            entry['locators'] = [entry['response']['path'] + '#/' + key for key in ('content', 'structuredContent') if key in result]
            if not entry['locators']:
                entry['locators'] = [entry['response']['path'] + '#']
            self.append({'phase': 'completed', **entry})

    def freeze(self):
        with self.lock:
            for entry in self.entries:
                if entry['status'] == 'pending':
                    self.finish(entry, {'isError': True, 'reason': 'execution interrupted before tool response'}, 'interrupted')
            self.frozen = True
            manifest = {'format': 'reveal.tool-ledger/1', 'job_id': self.job_id, 'attempt': self.attempt,
                        'calls': self.entries, 'complete': all(x['status'] != 'pending' for x in self.entries)}
            (self.root / 'manifest.json').write_bytes(self.sanitized_bytes(manifest)[0])
            return manifest


def iri(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:https?://|urn:)[^\s<>"{}|^`\\]+', value):
        raise PolicyError('Query terms must be absolute, syntactically safe IRIs')
    return '<' + value + '>'


def query_arguments(arguments, selected_graphs):
    if set(arguments) - {'graph', 'subject', 'predicate', 'object', 'literal', 'contains', 'limit'}:
        raise PolicyError('Unexpected query fields; arbitrary SPARQL is unavailable')
    graph = arguments.get('graph')
    if graph not in selected_graphs or graph not in GRAPHS:
        raise PolicyError('Graph was not selected for this request')
    limit = arguments.get('limit', 25)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise PolicyError('Query limit must be an integer from 1 to 100')
    terms = [iri(arguments[k]) if arguments.get(k) else '?' + alias for k, alias in [('subject', 's'), ('predicate', 'p'), ('object', 'o')]]
    literal = arguments.get('literal')
    if literal is not None:
        if not isinstance(literal, str) or not 1 <= len(literal) <= 200:
            raise QueryValidationError('literal must be exact source text of 1–200 characters')
        if not arguments.get('predicate') or arguments.get('object') or arguments.get('contains'):
            raise QueryValidationError('Use literal with a schema-derived predicate IRI, without object or contains')
        terms[2] = json.dumps(literal, ensure_ascii=True)
    if not any(arguments.get(k) for k in ('subject', 'predicate', 'object', 'literal', 'contains')):
        raise PolicyError('Query needs a bound IRI or text search')
    filters = ''
    if arguments.get('contains'):
        term = arguments['contains']
        if not isinstance(term, str) or not 2 <= len(term) <= 100:
            raise PolicyError('Search term must contain 2–100 characters')
        if not arguments.get('subject') and not arguments.get('predicate'):
            raise QueryValidationError('Unbound whole-graph text scans are unavailable. Supply a schema-derived predicate or exact subject IRI; prefer predicate plus literal for an exact label/symbol lookup. An unavailable query is not evidence of absence.')
        search_literal = json.dumps(term.lower(), ensure_ascii=True)
        filters = f' FILTER(CONTAINS(LCASE(STR(?o)), {search_literal})) '
        if arguments.get('object'):
            raise PolicyError('Text search and fixed object cannot be combined')
    # Include bound terms in the result so every assertion retains all three positions.
    bindings = ' '.join(f'BIND({iri(arguments[k])} AS ?{alias})' for k, alias in [('subject', 's'), ('predicate', 'p'), ('object', 'o')] if arguments.get(k))
    if literal is not None:
        bindings += ' BIND(' + json.dumps(literal, ensure_ascii=True) + ' AS ?o)'
    query = f'SELECT ?s ?p ?o WHERE {{ GRAPH <{GRAPHS[graph]}> {{ {" ".join(terms)} . {bindings} {filters} }} }} LIMIT {limit}'
    return {'query': query, 'format': 'json', 'exploratory': True, 'compact': False}


def response_payloads(result):
    payloads = [result, result.get('structuredContent', {})]
    for block in result.get('content', []):
        if isinstance(block, dict) and block.get('type') == 'text':
            try:
                payloads.append(json.loads(block.get('text', '')))
            except (ValueError, TypeError):
                pass
    return payloads


class ScopedTools:
    def __init__(self, selected_graphs, ledger, client=None, max_calls=30, lint=None, write_draft=None,
                 read_timeout=35, max_parallel_reads=2, literature=None, write_outcome=None):
        if set(selected_graphs) - set(GRAPHS):
            raise PolicyError('Unsupported selected graph')
        self.selected_graphs, self.ledger = tuple(selected_graphs), ledger
        if not 0 < read_timeout < 60 or not 1 <= max_parallel_reads <= 4:
            raise ValueError('Evidence transport limits are out of bounds')
        self.client = client
        self.max_calls, self.lint = max_calls, lint
        self.write_draft = write_draft
        self.literature, self.write_outcome = literature, write_outcome
        self.literature_calls = {'search_papers': 0, 'read_paper': 0}
        self.tool_calls = 0
        self.call_lock = threading.Lock()
        self.author_lock = threading.Lock()
        self.read_timeout = read_timeout
        self.read_slots = threading.BoundedSemaphore(max_parallel_reads)

    def upstream_call(self, tool, arguments, client=None):
        # Never queue a graph read behind an unrelated slow graph. Each actual
        # read owns its MCP session; the session IDs and RPC counters cannot race.
        if not self.read_slots.acquire(blocking=False):
            raise EvidenceReadTimeout('Evidence service has outstanding reads; no additional query was started')
        outcome = queue.Queue(maxsize=1)
        def run():
            try:
                upstream_client = client or self.client or MCPClient(timeout=self.read_timeout)
                outcome.put((True, upstream_client.call(tool, arguments), getattr(upstream_client, 'server_info', None)))
            except Exception as exc:
                outcome.put((False, exc, None))
            finally:
                self.read_slots.release()
        threading.Thread(target=run, daemon=True).start()
        try:
            success, value, source_version = outcome.get(timeout=self.read_timeout)
        except queue.Empty:
            # A late network response cannot update this call or its ledger.
            # The occupied slot remains reserved until that network read ends.
            raise EvidenceReadTimeout(f'Evidence read exceeded {self.read_timeout:g} seconds; result unavailable, not empty') from None
        if not success:
            raise value
        if not isinstance(value, dict):
            raise ValueError('Evidence service returned an invalid tool envelope')
        return value, source_version

    def definitions(self):
        graph = {'type': 'string', 'enum': list(self.selected_graphs)}
        tools = []
        if self.selected_graphs:
            for name, description in [('get_schema', 'Inspect one selected graph schema'), ('describe_kg', 'Inspect selected graph scope')]:
                tools.append({'name': name, 'description': description, 'inputSchema': {'type': 'object', 'properties': {'graph': graph}, 'required': ['graph'], 'additionalProperties': False}})
            tools.append({'name': 'query_graph', 'description': 'Fetch bounded triples in one selected named graph. Prefer exact subject/object IRIs. For exact source label or gene-symbol discovery use a schema-derived predicate IRI plus literal (case-sensitive). contains requires a bound predicate or subject; whole-graph text scans are unavailable. A hit needs entity/scope verification; empty or failed reads do not prove biological absence.',
                          'inputSchema': {'type': 'object', 'properties': {'graph': graph, 'subject': {'type': 'string'}, 'predicate': {'type': 'string'}, 'object': {'type': 'string'}, 'literal': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'contains': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, 'required': ['graph'], 'additionalProperties': False}})
        if self.lint:
            tools.append({'name': 'lint_account', 'description': 'Run the worker\'s shared account linter: pinned DAPPER structure, exact source pointers, verbatim snippets, metric agreement, and trusted captured Files. Returns repairable findings; draft IDs are allowed.', 'inputSchema': {'type': 'object', 'properties': {'filename': {'type': 'string', 'pattern': '^account-[1-3]\\.(json|yaml|yml)$'}}, 'required': ['filename'], 'additionalProperties': False}})
        if self.write_draft:
            tools.append({'name': 'write_account_draft', 'description': 'Write a DAPPER document with plural group arrays: {"scientific_accounts":[one account],"propositions":[...],"claims":[...],"evidence_items":[...]}. Automatically copies exact referenced trusted source nodes and dependencies. Do not supply a class instance as document root or a graph/nodes envelope. No acceptance or minting occurs.', 'inputSchema': {'type': 'object', 'properties': {'filename': {'type': 'string', 'enum': ['account-1.json', 'account-2.json', 'account-3.json']}, 'document': {'type': 'object', 'properties': {'scientific_accounts': {'type': 'array', 'minItems': 1, 'maxItems': 1, 'items': {'type': 'object'}}, 'propositions': {'type': 'array', 'items': {'type': 'object'}}, 'claims': {'type': 'array', 'items': {'type': 'object'}}, 'evidence_items': {'type': 'array', 'items': {'type': 'object'}}}, 'required': ['scientific_accounts']}}, 'required': ['filename', 'document'], 'additionalProperties': False}})
        if self.write_outcome:
            tools.append({'name': 'write_outcome', 'description': 'Save a structured insufficient-evidence outcome to the canonical writable output directory. Follow the skill format; this does not accept scientific claims.',
                          'inputSchema': {'type': 'object', 'properties': {'outcome': {'type': 'object'}}, 'required': ['outcome'], 'additionalProperties': False}})
        if self.literature:
            tools.extend([
                {'name': 'search_papers', 'description': 'Search Europe PMC scholarly literature (maximum three searches per attempt). Discovery metadata only; read the exact record before interpreting findings. No arbitrary web URLs.',
                 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string', 'minLength': 2, 'maxLength': 300}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5}}, 'required': ['query'], 'additionalProperties': False}},
                {'name': 'read_paper', 'description': 'Read a bounded abstract or available open-access PMC full-text excerpt with exact captured provenance (maximum four reads). MED IDs are numeric PMIDs; PMC IDs start PMC. Literature is auxiliary and cannot replace CFDE lineage. Follow next_offset only when needed; abstracts do not imply full-paper review.',
                 'inputSchema': {'type': 'object', 'properties': {'source': {'type': 'string', 'enum': ['MED', 'PMC']}, 'id': {'type': 'string'}, 'section': {'type': 'string', 'enum': ['abstract', 'full_text']}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12000}}, 'required': ['source', 'id'], 'additionalProperties': False}}])
        return tools

    def call(self, tool, arguments):
        with self.call_lock:
            self.tool_calls += 1
            within_budget = self.tool_calls <= self.max_calls
            entry = self.ledger.start(tool, arguments, arguments.get('graph') if isinstance(arguments, dict) else None)
        status = 'failed'
        source_version = upstream_response = None
        raw_response = None; raw_format = 'json'
        try:
            if not isinstance(arguments, dict):
                raise PolicyError('Tool arguments must be an object')
            if any(secret in json.dumps(arguments, ensure_ascii=False) for secret in self.ledger.secrets):
                raise PolicyError('Credentials cannot be sent as tool arguments')
            if not within_budget:
                raise PolicyError('Attempt tool-call budget exhausted')
            if tool == 'lint_account' and self.lint:
                if set(arguments) != {'filename'} or not re.fullmatch(r'account-[1-3]\.(json|yaml|yml)', arguments['filename']):
                    raise PolicyError('Only one account output filename may be linted')
                with self.author_lock:
                    result = self.lint(arguments['filename'])
            elif tool == 'write_account_draft' and self.write_draft:
                if set(arguments) != {'filename', 'document'} or arguments['filename'] not in ('account-1.json', 'account-2.json', 'account-3.json'):
                    raise PolicyError('Invalid draft output arguments')
                with self.author_lock:
                    result = self.write_draft(arguments['filename'], arguments['document'])
            elif tool == 'write_outcome' and self.write_outcome:
                if set(arguments) != {'outcome'}: raise DraftValidationError('Expected one outcome object')
                with self.author_lock: result = self.write_outcome(arguments['outcome'])
            elif tool in ('search_papers', 'read_paper') and self.literature:
                spec = request_spec(tool, arguments)
                with self.call_lock:
                    if self.literature_calls[tool] >= (3 if tool == 'search_papers' else 4):
                        raise LiteratureInputError('Bounded literature read budget exhausted; report the remaining limitation')
                    self.literature_calls[tool] += 1
                with self.ledger.lock:
                    if self.ledger.frozen: raise EvidenceReadTimeout('Execution ended before the literature read started')
                    entry['upstream_request'] = self.ledger.artifact({'provider': 'Europe PMC', 'method': 'GET', **spec})
                result, source_version = self.upstream_call(tool, arguments, client=self.literature)
                result = dict(result)
                raw_response = result.pop('_raw_response', None); raw_format = result.pop('_raw_format', 'json')
            elif tool in ('get_schema', 'describe_kg'):
                if set(arguments) != {'graph'} or arguments['graph'] not in self.selected_graphs:
                    raise PolicyError('Graph was not selected for this request')
                upstream = {'shortname': arguments['graph']}
                upstream.update({'compact': True} if tool == 'get_schema' else {'long_description': True})
                result, source_version = self.upstream_call(tool, upstream)
            elif tool == 'query_graph':
                upstream = query_arguments(arguments, self.selected_graphs)
                with self.ledger.lock:
                    if self.ledger.frozen or entry['status'] != 'pending':
                        raise EvidenceReadTimeout('Execution ended before the evidence query started')
                    entry['upstream_request'] = self.ledger.artifact({'tool': 'sparql_query', 'arguments': upstream})
                result, source_version = self.upstream_call('sparql_query', upstream)
            else:
                raise PolicyError('Tool is not authorized')
            payloads = response_payloads(result)
            if tool in ('query_graph', 'get_schema', 'describe_kg') and any(
                    isinstance(value, dict) and value.get('error') for value in payloads):
                # Preserve the exact upstream bytes as well as the correctly
                # classified public envelope; an embedded HTTP429 is not evidence.
                upstream_response = result
                result = {**result, 'isError': True}
            status = 'failed' if result.get('isError') else 'completed'
            if status == 'completed' and any(isinstance(value, dict) and (value.get('row_count') == 0 or value.get('count') == 0) for value in payloads):
                status = 'empty'
        except Exception as exc:
            # Exceptions may contain sensitive request headers; retain a safe typed diagnostic.
            status = 'denied' if isinstance(exc, PolicyError) else 'failed'
            result = {'isError': True, 'content': [{'type': 'text', 'text': str(exc) if isinstance(exc, (PolicyError, DraftValidationError, QueryValidationError, EvidenceReadTimeout, LiteratureInputError)) else type(exc).__name__ + ': evidence service unavailable'}]}
        with self.ledger.lock:
            if self.ledger.frozen or entry['status'] != 'pending':
                result = json.loads((self.ledger.root / entry['response']['path']).read_bytes())
                status = entry['status']
            else:
                entry['source_version'] = source_version
                if upstream_response is not None:
                    entry['upstream_response'] = self.ledger.artifact(upstream_response)
                if raw_response is not None:
                    entry['upstream_response'] = self.ledger.artifact_bytes(raw_response, raw_format)
                self.ledger.finish(entry, result, status)
        if tool in ('query_graph', 'get_schema', 'describe_kg', 'search_papers', 'read_paper'):
            # The response capture is finalized before this descriptor is
            # added to the model-facing envelope, avoiding recursive hashes.
            # This supplies exact bytes/identity metadata, never evidence
            # interpretation; failed and empty captures remain diagnostic.
            artifact = entry['response']
            capture = {'format': 'reveal.tool-capture/1', 'ledger_sequence': entry['sequence'],
                       'tool': tool, 'selected_graph': entry['selected_graph'], 'status': status,
                       'artifact': {**artifact, 'path': 'ledger/' + artifact['path']},
                       'file': {'id': 'urn:reveal:tool-response:sha256:' + artifact['sha256'],
                                'filename': artifact['sha256'] + '.json', 'mime_type': 'application/json',
                                'sha256': artifact['sha256'], 'size_in_bytes': artifact['size_bytes']},
                       'source_locator': 'ledger_sequence=' + str(entry['sequence']) + ';pointer=/content',
                       'usage': 'Only completed query_graph assertions or completed read_paper excerpts can support auxiliary biological claims. Search metadata, schemas, errors and empty searches do not establish biological presence or absence. Paper evidence retains its abstract/full-text scope and never replaces required CFDE lineage.'}
            if tool == 'read_paper': capture['source_locator'] = 'ledger_sequence=' + str(entry['sequence']) + ';pointer=/structuredContent/data/text'
            result = {**result, 'content': [*result.get('content', []),
                      {'type': 'text', 'text': json.dumps(capture, ensure_ascii=False)}]}
        return result


def serve(tools: ScopedTools, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != '/mcp':
                self.send_error(404); return
            try:
                size = int(self.headers.get('Content-Length', 0))
                if size < 1 or size > 100000:
                    raise ValueError('Invalid request size')
                request = json.loads(self.rfile.read(size))
                method, params = request.get('method'), request.get('params', {})
                if method == 'notifications/initialized':
                    self.send_response(202); self.end_headers(); return
                if method == 'initialize':
                    result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'reveal-scoped-tools', 'version': '1'}}
                elif method == 'tools/list':
                    result = {'tools': tools.definitions()}
                elif method == 'tools/call':
                    result = tools.call(params.get('name'), params.get('arguments', {}))
                elif method == 'ping':
                    result = {}
                else:
                    raise ValueError('Unsupported MCP method')
                body = canonical({'jsonrpc': '2.0', 'id': request.get('id'), 'result': result})
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception:
                self.send_error(400, 'Invalid MCP request')

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
