"""Root-owned bridge to the same stateless research tools used by local agents.

The model sees neither the scoped bearer credential nor arbitrary transport URLs.
Only trusted exported closure bytes are added to the draft-lint source inventory.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import threading
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ALLOWED_TOOLS = frozenset({
    'get_local_work', 'get_research_package', 'get_operation',
    'list_data_operations', 'describe_data_operation', 'query_data',
    'search_factors', 'get_factor', 'get_factor_loadings', 'search_genes', 'resolve_gene',
    'get_gene_factors', 'search_gene_sets', 'get_gene_set', 'get_gene_set_members',
    'get_gene_set_factors', 'search_traits', 'get_trait', 'get_connections', 'get_imported_graph',
    'get_pigean_gene_phenotype', 'get_pigean_gene_set_phenotype',
    'find_propositions', 'find_claims', 'find_scientific_accounts', 'get_scientific_object',
    'reuse_scientific_objects', 'get_evidence_result', 'export_evidence_context', 'read_evidence',
})
MAX_RESPONSE = 10_000_000


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class ResearchAccessError(ValueError):
    """Safe error text which never includes transport credentials."""


def require(value, message):
    if not value: raise ResearchAccessError(message)


def validate_context(value):
    require(isinstance(value, dict) and set(value) == {'mcp_url', 'research_request_id', 'local_work_id'}, 'Invalid research context')
    for key in ('research_request_id', 'local_work_id'):
        require(isinstance(value[key], str) and 0 < len(value[key]) <= 256 and not any(ord(c) < 32 for c in value[key]), 'Invalid research identity')
    parsed = urlsplit(value['mcp_url']) if isinstance(value.get('mcp_url'), str) else None
    require(parsed and parsed.hostname and parsed.path.endswith('/mcp') and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and (parsed.scheme == 'https' or
            (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1'))), 'Invalid research MCP endpoint')
    return deepcopy(value)


def validate_access(value, context):
    validate_context(context)
    require(isinstance(value, dict) and set(value) == {*context, 'token'}, 'Missing scoped hosted research credential')
    require({key: value[key] for key in context} == context, 'Hosted research credential binding differs from frozen context')
    token = value['token']
    require(isinstance(token, str) and 16 <= len(token) <= 4096 and all(32 < ord(c) < 127 for c in token), 'Invalid hosted research credential')
    return token


def validate_hosted_context(value):
    context = validate_context(value)
    parsed = urlsplit(context['mcp_url'])
    host = parsed.hostname.rstrip('.').lower()
    local = host == 'localhost' or host.endswith('.localhost')
    try:
        address = ipaddress.ip_address(host)
        local = local or address.is_loopback or address.is_unspecified
    except ValueError:
        pass
    require(parsed.scheme == 'https' and not local,
        'Online research requires a backend MCP HTTPS URL reachable from the hosted agent; configure a public HTTPS endpoint or tunnel before starting a run')
    return context


def network_policy(context=None):
    # LiteratureClient permits only this fixed HTTPS origin and rejects redirects.
    domains = ['api.anthropic.com', 'apps.okn.us', 'github.com', 'www.ebi.ac.uk']
    if context:
        validate_context(context)
        host = urlsplit(context['mcp_url']).hostname
        if host not in domains: domains.append(host)
    return {'mode': 'custom', 'allowed_domains': domains}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ResearchAccessError('Research transport redirects are forbidden')


class ResearchInvocation:
    """One transport deadline; only the receiving harness can commit observations.

    A daemon read may outlive its socket timeout. Its result cannot mutate the
    receipt journal: acceptance happens on the caller after a timely delivery.
    """
    def __init__(self, client, tool, arguments, timeout):
        self.client, self.tool, self.arguments = client, tool, deepcopy(arguments)
        self.deadline = time.monotonic() + min(client.timeout, timeout * 0.9)
        parent = getattr(client._calls, 'current', None)
        if parent: self.deadline = min(self.deadline, parent.deadline)
        self.cancelled = threading.Event()

    def check(self):
        require(not self.cancelled.is_set() and time.monotonic() < self.deadline,
                'Research response deadline reached; retry the same tool with identical arguments to recover its durable operation. This is unavailable evidence, not an empty result.')

    def run(self):
        self.check()
        previous = getattr(self.client._calls, 'current', None)
        self.client._calls.current = self
        try:
            result = self.client._call_result(self.tool, self.arguments)
            self.check()
            return result
        finally:
            self.client._calls.current = previous

    def accept(self, result):
        self.check()
        self.client._accept_call(self.tool, self.arguments, result)

    def cancel(self):
        self.cancelled.set()

    def recovery(self):
        return self.client._recovery_result(self.tool, self.arguments)


class HostedResearchClient:
    def __init__(self, context, token, root, *, seed_path, execution_id, opener=None, timeout=30, operation_wait=25):
        self.context = validate_context(context)
        self._token = validate_access({**context, 'token': token}, context)
        self.root, self.seed_path = Path(root), Path(seed_path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.execution_id, self.timeout, self.operation_wait = execution_id, timeout, operation_wait
        self._opener = opener or build_opener(NoRedirect())
        self._definitions = None
        self._schemas = {}
        self._lock = threading.RLock()
        self._materialize_lock = threading.RLock()
        self._calls = threading.local()
        self.operations = {}
        self.receipt_ids, self.reuse_receipt_ids, self.existing_account_ids = set(), set(), set()
        self._sequence = 0
        self._frozen = False
        self.server_info = {'name': 'reveal-research', 'scope': deepcopy(context)}
        receipt_path = self.root.parent / 'research-receipts.json'
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_bytes())
            require(all(previous.get(key) == value for key, value in self.context.items()), 'Saved research receipt scope differs')
            self.receipt_ids.update(previous.get('receipt_ids', []))
            self.reuse_receipt_ids.update(previous.get('reuse_receipt_ids', []))
            self.existing_account_ids.update(previous.get('existing_account_ids', []))
        operations_path = self.root.parent / 'research-operations.json'
        if operations_path.exists():
            require(operations_path.stat().st_size <= 200_000, 'Saved research operation journal exceeds its bound')
            previous = json.loads(operations_path.read_bytes())
            require(all(previous.get(key) == value for key, value in self.context.items())
                and previous.get('execution_id') == execution_id, 'Saved research operation scope differs')
            self.operations = previous.get('operations', {})
            require(isinstance(self.operations, dict) and len(self.operations) <= 300
                and all(isinstance(key, str) and isinstance(value, dict)
                    and isinstance(value.get('operation_id'), str) for key, value in self.operations.items()),
                'Invalid saved research operation journal')
        self._write_receipts()

    def _http(self, url, body=None, maximum=MAX_RESPONSE):
        invocation = getattr(self._calls, 'current', None)
        if invocation: invocation.check()
        timeout = min(self.timeout, invocation.deadline-time.monotonic()) if invocation else self.timeout
        require(timeout > 0, 'Research response deadline reached; retry the same tool with identical arguments')
        request = Request(url, body, {'Authorization': 'Bearer ' + self._token,
            'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-03-26'}, method='POST' if body is not None else 'GET')
        try:
            with self._opener.open(request, timeout=timeout) as response:
                require(response.geturl() == url, 'Research response changed its destination')
                # read1 performs at most one underlying read. Check the common
                # deadline between chunks, including a slowly streamed body.
                read = getattr(response, 'read1', response.read)
                chunks, received = [], 0
                while received <= maximum:
                    if invocation: invocation.check()
                    chunk = read(min(65_536, maximum + 1 - received))
                    if not chunk: break
                    chunks.append(chunk); received += len(chunk)
                raw = b''.join(chunks)
                if invocation: invocation.check()
                require(len(raw) <= maximum, 'Research response exceeds capture limit')
                require(self._token.encode() not in raw, 'Research response contains credential material')
                return raw
        except ResearchAccessError: raise
        except Exception as exc:
            raise ResearchAccessError(type(exc).__name__ + ': research service unavailable; retry the same tool with identical arguments to recover the operation') from None

    def rpc(self, method, params=None):
        with self._lock:
            self._sequence += 1
            identity = self._sequence
        payload = {'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params or {}}
        try: response = json.loads(self._http(self.context['mcp_url'], canonical(payload)))
        except ResearchAccessError: raise
        except (ValueError, UnicodeError): raise ResearchAccessError('Research response is not valid JSON') from None
        require(response.get('id') == identity and 'error' not in response and isinstance(response.get('result'), dict), 'Research MCP request failed')
        return response['result']

    def definitions(self):
        if self._definitions is None:
            response = self.rpc('tools/list')
            require(isinstance(response.get('tools'), list), 'Research catalog is unavailable')
            definitions = []
            for original in response['tools']:
                if original.get('name') not in ALLOWED_TOOLS: continue
                item = deepcopy(original)
                schema = item.get('inputSchema', {})
                if 'anyOf' in schema:
                    variants = [variant for variant in schema['anyOf'] if
                        {'local_work_id', 'research_request_id'} & set(variant.get('required', []))]
                    require(len(variants) == 1, 'Research catalog has no unique hosted scope')
                    schema = deepcopy(variants[0])
                self._schemas[item['name']] = deepcopy(schema)
                metadata = item.get('_meta') or {}
                if metadata.get('reveal/hostedDescription'):
                    item['description'] = metadata['reveal/hostedDescription']
                item.pop('securitySchemes', None)
                metadata.pop('securitySchemes', None)
                metadata.pop('reveal/hostedDescription', None)
                if metadata: item['_meta'] = metadata
                else: item.pop('_meta', None)
                # The proxy, not model-authored arguments, binds the immutable work/request and retry identity.
                hidden = {'local_work_id', 'research_request_id', 'idempotency_key'}
                schema['properties'] = {key: value for key, value in schema.get('properties', {}).items() if key not in hidden}
                schema['required'] = [key for key in schema.get('required', []) if key not in hidden]
                item['inputSchema'] = schema
                definitions.append(item)
            self._definitions = definitions
        return deepcopy(self._definitions)

    @staticmethod
    def payload(result):
        if isinstance(result.get('structuredContent'), dict): return result['structuredContent']
        for block in result.get('content', []):
            if block.get('type') == 'text':
                try:
                    value = json.loads(block['text'])
                    if isinstance(value, dict): return value
                except (ValueError, KeyError): pass
        return {}

    def _key(self, tool, arguments):
        return 'hosted-' + hashlib.sha256(canonical([self.execution_id, tool, arguments])).hexdigest()

    def _invoke(self, tool, arguments):
        require(tool in ALLOWED_TOOLS, 'Research tool is not authorized for hosted authoring')
        arguments = deepcopy(arguments)
        require('reference_generation_id' not in arguments, 'Research call cannot change its frozen generation')
        for key in ('local_work_id', 'research_request_id'):
            require(key not in arguments or arguments[key] == self.context[key], 'Research call cannot change its frozen scope')
        fields = self._schemas.get(tool, {}).get('properties', {})
        for key in ('local_work_id', 'research_request_id'):
            arguments.pop(key, None)
            if key in fields: arguments[key] = self.context[key]
        arguments.pop('idempotency_key', None)
        if 'idempotency_key' in fields:
            arguments['idempotency_key'] = self._key(tool, arguments)
        return self.rpc('tools/call', {'name': tool, 'arguments': arguments})

    def _observe(self, value):
        with self._lock:
            if self._frozen: return
            if isinstance(value.get('receipt_id'), str): self.receipt_ids.add(value['receipt_id'])
            if isinstance(value.get('reuse_receipt_id'), str): self.reuse_receipt_ids.add(value['reuse_receipt_id'])
            if value.get('format') == 'reveal.scientific-reuse/1' or value.get('reuse_receipt_id'):
                if isinstance(value.get('id'), str): self.reuse_receipt_ids.add(value['id'])
                for item in value.get('selections', []):
                    if item.get('purpose') == 'existing_account' and isinstance(item.get('selection', {}).get('object_id'), str):
                        self.existing_account_ids.add(item['selection']['object_id'])
            receipt = value.get('receipt')
            if isinstance(receipt, dict) and isinstance(receipt.get('id'), str): self.reuse_receipt_ids.add(receipt['id'])
            self._write_receipts()

    def _write_receipts(self):
        body = {'format': 'reveal.hosted-research-receipts/1', **self.context,
            'receipt_ids': sorted(self.receipt_ids), 'reuse_receipt_ids': sorted(self.reuse_receipt_ids),
            'existing_account_ids': sorted(self.existing_account_ids)}
        raw = canonical(body)
        require(self._token.encode() not in raw, 'Credential cannot enter research capture')
        path = self.root.parent / 'research-receipts.json'
        temporary = path.with_suffix('.tmp'); temporary.write_bytes(raw); temporary.replace(path)

    def freeze(self):
        with self._lock:
            self._frozen = True
            self._write_receipts()

    def begin_bounded_call(self, tool, arguments, timeout):
        return ResearchInvocation(self, tool, arguments, timeout)

    def _known_operation(self, tool, arguments):
        if tool == 'get_operation': return arguments.get('operation_id')
        with self._lock:
            return self.operations.get(self._key(tool, arguments), {}).get('operation_id')

    def _recovery_result(self, tool, arguments):
        require(isinstance(arguments, dict) and self._token not in json.dumps(arguments), 'Invalid recovery arguments')
        identity = self._known_operation(tool, arguments)
        recovery = ({'tool': 'get_operation', 'arguments': {'operation_id': identity}} if identity else
                    {'tool': tool, 'arguments': deepcopy(arguments)})
        value = {'status': 'response_unavailable', 'recovery': recovery,
                 'detail': 'No timely response was observed. Retry the recovery call; identical scientific-query arguments reuse the same durable operation. This is not an empty scientific result.'}
        if identity: value['operation_id'] = identity
        return {'isError': True, 'structuredContent': value, 'content': [{'type': 'text', 'text': json.dumps(value)}]}

    def _call_result(self, tool, arguments):
        require(not self._frozen, 'Hosted research execution has ended')
        require(isinstance(arguments, dict), 'Research arguments must be an object')
        require(tool in {item['name'] for item in self.definitions()}, 'Research tool is not available')
        require(self._token not in json.dumps(arguments), 'Credential cannot be used as research data')
        identity = self._known_operation(tool, arguments)
        result = self._invoke('get_operation', {'operation_id': identity}) if identity else self._invoke(tool, arguments)
        value = self.payload(result)
        operation_id = value.get('operation_id') or identity
        if operation_id and not result.get('isError'):
            value = {**value, 'operation_id': operation_id}
            if value.get('state') in ('received', 'queued', 'running', 'preparing'):
                value['recovery'] = {'tool': 'get_operation', 'arguments': {'operation_id': operation_id}}
                value['detail'] = 'The durable operation is pending; poll get_operation. No scientific result is available yet.'
            result = {**result, 'structuredContent': value, 'content': [{'type': 'text', 'text': json.dumps(value)}]}
        if value.get('state') in ('failed', 'rejected', 'cancelled'):
            result = {**result, 'isError': True}
        return result

    def _accept_call(self, tool, arguments, result):
        """Commit only responses delivered before the harness's wall deadline."""
        value = self.payload(result)
        operation_id = value.get('operation_id') or (arguments.get('operation_id') if tool == 'get_operation' else None)
        with self._lock:
            if self._frozen: return
            if operation_id and value.get('state'):
                key = self._key(tool, arguments)
                if tool == 'get_operation':
                    keys = [key for key, item in self.operations.items() if item['operation_id'] == operation_id]
                else:
                    keys = [key]
                for key in keys:
                    self.operations[key] = {'operation_id': operation_id, 'state': value['state'],
                        'tool': self.operations.get(key, {}).get('tool', tool)}
                require(len(self.operations) <= 300, 'Research operation journal limit reached')
                body = {'format': 'reveal.hosted-research-operations/1', **self.context,
                    'execution_id': self.execution_id, 'operations': self.operations}
                raw = canonical(body)
                require(len(raw) <= 200_000 and self._token.encode() not in raw, 'Research operation journal exceeds capture bounds')
                path = self.root.parent / 'research-operations.json'
                temporary = path.with_suffix('.tmp'); temporary.write_bytes(raw); temporary.replace(path)
        if not result.get('isError'):
            self._observe(value)
            if tool == 'reuse_scientific_objects' and isinstance(value.get('id'), str):
                with self._lock:
                    if not self._frozen:
                        self.reuse_receipt_ids.add(value['id']); self._write_receipts()
            if isinstance(value.get('result'), dict): self._observe(value['result'])

    def call(self, tool, arguments):
        invocation = self.begin_bounded_call(tool, arguments, self.timeout)
        result = invocation.run()
        invocation.accept(result)
        return result

    def materialize(self):
        # The export acknowledgement, observations and downloads share one
        # deadline. Already verified local files survive for incremental retry.
        invocation = self.begin_bounded_call('export_evidence_context', {}, self.operation_wait)
        previous = getattr(self._calls, 'current', None)
        self._calls.current = invocation
        try: return self._materialize()
        except ResearchAccessError as exc:
            with self._lock:
                arguments = {'receipt_ids': sorted(self.receipt_ids), 'reuse_receipt_ids': sorted(self.reuse_receipt_ids)}
            identity = self._known_operation('export_evidence_context', arguments)
            suffix = (' Operation '+identity+' can be observed with get_operation.' if identity else '')
            raise ResearchAccessError(str(exc)+suffix+' Retry the same draft/lint call to resume verified incremental materialization.') from None
        finally: self._calls.current = previous

    def _materialize(self):
        self.definitions()
        if not self._materialize_lock.acquire(blocking=False):
            raise ResearchAccessError('Evidence materialization is already running; no duplicate export was started')
        try:
            self._calls.current.check()
            require(not self._frozen, 'Hosted research execution has ended')
            with self._lock:
                arguments = {'receipt_ids': sorted(self.receipt_ids), 'reuse_receipt_ids': sorted(self.reuse_receipt_ids)}
            result = self.call('export_evidence_context', arguments)
            require(not result.get('isError'), 'Evidence closure export failed')
            value = self.payload(result)
            operation_id = value.get('operation_id')
            deadline = self._calls.current.deadline
            while operation_id and value.get('state') in ('received', 'queued', 'running', 'preparing'):
                if time.monotonic() >= deadline:
                    raise ResearchAccessError('Evidence export is pending ('+operation_id+'); retry materialization to resume the same export')
                time.sleep(min(0.15, max(0, deadline-time.monotonic())))
                try: result = self.call('get_operation', {'operation_id': operation_id})
                except ResearchAccessError:
                    raise ResearchAccessError('Evidence export observation unavailable ('+operation_id+'); retry materialization or poll get_operation with this ID') from None
                require(not result.get('isError'), 'Evidence closure export failed')
                value = self.payload(result)
            if operation_id:
                require(value.get('state') == 'succeeded', 'Evidence closure export failed')
                value = value.get('result', {})
            if value.get('format') == 'reveal.validation-context-export/2':
                descriptor = value.get('package_artifact', {})
                require(descriptor.get('sha256') == value.get('package_sha256'), 'Exported package descriptor differs')
                size = descriptor.get('size_bytes')
                require(type(size) is int and 0 < size <= 8_000_000, 'Evidence package size exceeds bound')
                path = self.root / 'evidence-package.json'
                require(not path.is_symlink() and path.resolve().is_relative_to(self.root.resolve()), 'Evidence destination is unsafe')
                cached = None
                if path.is_file():
                    with path.open('rb') as handle: cached = handle.read(8_000_001)
                    require(len(cached) <= 8_000_000, 'Cached evidence package size exceeds bound')
                if cached is not None and hashlib.sha256(cached).hexdigest() == descriptor['sha256']:
                    raw = cached
                else:
                    if cached is not None and (self.root / 'manifest.json').is_file():
                        with (self.root / 'manifest.json').open('rb') as handle: prior_raw = handle.read(MAX_RESPONSE+1)
                        require(len(prior_raw) <= MAX_RESPONSE, 'Cached evidence manifest size exceeds bound')
                        prior = json.loads(prior_raw)
                        require(prior.get('package_sha256') != descriptor['sha256'], 'Cached evidence package checksum differs')
                    endpoint = self.context['mcp_url'][:-4] + '/v1/research-artifacts/' + quote(str(descriptor['id']), safe='') + '/content'
                    raw = self._http(endpoint, maximum=size)
                require(len(raw) == descriptor['size_bytes'], 'Exported package size differs')
                package = json.loads(raw)
                require(hashlib.sha256(canonical(package.get('validation_context')) + b'\n').hexdigest() == value.get('context_sha256'), 'Exported context checksum differs')
            else:  # Frozen v1 workspaces remain readable.
                package = value.get('package')
                raw = canonical(package) + b'\n'
            require(isinstance(package, dict) and isinstance(value.get('artifacts'), list), 'Evidence export has no closed package')
            require(hashlib.sha256(raw).hexdigest() == value.get('package_sha256'), 'Exported package checksum differs')
            with self.seed_path.open('rb') as handle: seed_raw = handle.read(8_000_001)
            require(len(seed_raw) <= 8_000_000, 'Seed evidence package size exceeds bound')
            seed = json.loads(seed_raw)
            require(package.get('selection') == seed.get('selection') and package.get('research_request_id') == self.context['research_request_id'], 'Evidence export changed the frozen research question')
            require(value.get('seed_sha256') == hashlib.sha256(seed_raw).hexdigest(), 'Evidence closure belongs to another seed')
            # Keep the verified package across an interrupted source download.
            # manifest.json is written only after every source is complete.
            self._calls.current.check()
            package_path = self.root / 'evidence-package.json'
            temporary = package_path.with_suffix('.tmp')
            require(not temporary.is_symlink(), 'Evidence destination is unsafe')
            temporary.write_bytes(raw); temporary.replace(package_path); package_path.chmod(0o444)
            seed_sources = {}
            for descriptor in seed.get('source_artifacts', {}).values():
                seed_sources.setdefault((descriptor.get('sha256'), descriptor.get('size_bytes')), []).append(descriptor)
            by_path = {item['filename']: item for item in value['artifacts']}
            total = len(raw)
            for descriptor in package.get('source_artifacts', {}).values():
                self._calls.current.check()
                require(not self._frozen, 'Hosted research execution has ended')
                relative = descriptor['path']
                require(isinstance(relative, str) and relative.startswith(('sources/', 'imports/', 'reuse/'))
                    and len(relative) < 1024 and '\\' not in relative and not PurePosixPath(relative).is_absolute()
                    and all(part and not part.startswith('.') for part in relative.split('/')), 'Evidence artifact path is unsafe')
                artifact = by_path.get(relative)
                require(isinstance(artifact, dict) and artifact.get('sha256') == descriptor.get('sha256')
                    and artifact.get('size_bytes') == descriptor.get('size_bytes'), 'Evidence source descriptor differs')
                size = artifact['size_bytes']
                require(type(size) is int and 0 <= size <= 8_000_000, 'Evidence artifact size exceeds bound')
                total += size; require(total <= 32_000_000, 'Evidence closure exceeds bounded local capture')
                path = self.root / relative
                require(not path.is_symlink() and path.resolve().is_relative_to(self.root.resolve()), 'Evidence destination is unsafe')
                if path.is_file():
                    with path.open('rb') as handle: data = handle.read(size+1)
                else:
                    data = None
                    for source in seed_sources.get((artifact['sha256'], size), []):
                        source_relative = source.get('path')
                        require(isinstance(source_relative, str) and source_relative.startswith(('sources/', 'imports/', 'reuse/'))
                            and '\\' not in source_relative and all(part and not part.startswith('.') for part in source_relative.split('/')),
                            'Seed source path is unsafe')
                        source_path = self.seed_path.parent / source_relative
                        require(not source_path.is_symlink() and source_path.resolve().is_relative_to(self.seed_path.parent.resolve()), 'Seed source path is unsafe')
                        if source_path.is_file():
                            with source_path.open('rb') as handle: data = handle.read(size+1)
                            require(len(data) == size and hashlib.sha256(data).hexdigest() == artifact['sha256'], 'Pinned seed source bytes differ')
                            break
                    if data is None:
                        endpoint = self.context['mcp_url'][:-4] + '/v1/research-artifacts/' + quote(str(artifact['id']), safe='') + '/content'
                        data = self._http(endpoint, maximum=size)
                self._calls.current.check()
                require(len(data) == size and hashlib.sha256(data).hexdigest() == artifact['sha256'], 'Downloaded evidence bytes differ')
                require(self._token.encode() not in data, 'Credential cannot enter evidence capture')
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    temporary = path.with_suffix(path.suffix+'.tmp')
                    require(not temporary.is_symlink(), 'Evidence destination is unsafe')
                    temporary.write_bytes(data); temporary.replace(path)
                path.chmod(0o444)
            self._calls.current.check()
            (self.root / 'manifest.json').write_bytes(canonical(value.get('manifest', {})))
            with self._lock: self._write_receipts()
            return package_path
        finally:
            self._materialize_lock.release()
