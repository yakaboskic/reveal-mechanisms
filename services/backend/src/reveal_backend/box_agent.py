"""General-purpose agent jobs: a caller's prompt runs in the isolated Box runtime research uses.

No scientific gates apply. The agent gets the tools the caller chose, works in a sandbox, and its final
answer becomes the job result. The API freezes the input, the trusted in-Box runner reads it, and
acceptance reads the trusted response record.

Standard library only: this module is bundled into the Box.
"""
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
import re
import ssl
import threading

from .box_mcp import GRAPHS

INPUT_FORMAT = 'reveal.agent-input/1'
INPUT_FILENAME = 'agent-input.json'
RESPONSE_FORMAT = 'reveal.agent-response/1'
# The runner writes the response into the root-owned tool ledger, never into the agent-writable output.
RESPONSE_FILENAME = 'agent-response.json'
RESPONSE_CAPTURE_PATH = 'ledger/' + RESPONSE_FILENAME

BUILTIN_TOOLS = ('Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch')
# WebFetch downloads pages from inside the Box, so it needs the open network.
OPEN_NETWORK_TOOLS = frozenset({'WebFetch'})
# A request that names no tools gets every tool its network allows.
DEFAULT_TOOLS = BUILTIN_TOOLS
RESTRICTED_DEFAULT_TOOLS = tuple(tool for tool in BUILTIN_TOOLS if tool not in OPEN_NETWORK_TOOLS)
NETWORKS = ('restricted', 'open')
EFFORTS = ('low', 'medium', 'high')
DEFAULT_MODEL = 'claude-opus-5-5'
MODEL_PATTERN = re.compile(r'claude-[a-z0-9-]{1,60}')
MAX_PROMPT_CHARS = 100_000
MAX_SYSTEM_PROMPT_CHARS = 50_000
TIMEOUT_RANGE = (60, 3600)
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_BUDGET_USD = 2.0
# Job rows are listed in full, so a long answer is cut there; the captured record keeps all of it.
MAX_RESULT_RESPONSE_CHARS = 50_000
INPUT_KEYS = {'format', 'prompt', 'system_prompt', 'tools', 'graphs', 'literature', 'network', 'allowed_domains', 'effort', 'model', 'limits'}
MAX_ALLOWED_DOMAINS = 20
# Exact lowercase DNS hostnames: no IP addresses, ports, paths or wildcards (Box matches the list exactly).
HOSTNAME = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]')

ANTHROPIC_HOST = 'api.anthropic.com'
KEY_PROXY_PORT = 8766
# Claude Code needs some key; the loopback proxy replaces it with the real one.
KEY_PROXY_PLACEHOLDER = 'reveal-loopback-proxy'
MAX_PROXY_REQUEST_BYTES = 32_000_000


class AgentInputError(ValueError):
    pass


def require(condition, message):
    if not condition: raise AgentInputError(message)


def text(value, name, maximum, *, required):
    if value is None and not required: return None
    require(isinstance(value, str) and value.strip() and len(value) <= maximum,
            f'{name} must be nonblank text of at most {maximum:,} characters')
    return value


def freeze_input(body, *, budget_cap, default_model=DEFAULT_MODEL):
    """The frozen input for one agent job, from a validated API body. Defaults are filled in so the frozen
    bytes say exactly what runs; the caller's budget can lower the server cap but never raise it."""
    require(isinstance(body, dict) and body.get('kind') == 'agent', 'Not an agent job')
    domains = body.get('allowed_domains', [])
    require(isinstance(domains, list) and all(isinstance(domain, str) for domain in domains), 'allowed_domains must be a list of hostnames')
    domains = sorted({domain.strip().lower().rstrip('.') for domain in domains})
    # General purpose by default: the open network. Naming allowed_domains asks for the restricted network plus
    # those hosts, so that request needs no separate network field.
    network = body.get('network', 'restricted' if domains else 'open')
    require(network in NETWORKS, 'network must be restricted or open')
    tools = body.get('tools', list(DEFAULT_TOOLS if network == 'open' else RESTRICTED_DEFAULT_TOOLS))
    require(isinstance(tools, list) and all(tool in BUILTIN_TOOLS for tool in tools) and len(set(tools)) == len(tools),
            'tools must be distinct names from ' + ', '.join(BUILTIN_TOOLS))
    graphs = body.get('graphs', [])
    require(isinstance(graphs, list) and all(graph in GRAPHS for graph in graphs) and len(set(graphs)) == len(graphs),
            'graphs must be distinct names from ' + ', '.join(sorted(GRAPHS)))
    require(network == 'restricted' or not domains, 'allowed_domains narrows the restricted network; network "open" already reaches every host')
    require(network == 'open' or not OPEN_NETWORK_TOOLS & set(tools),
            'WebFetch downloads pages from inside the sandbox and needs network "open"; WebSearch works on the restricted network')
    limits = body.get('limits') or {}
    require(isinstance(limits, dict), 'limits must be an object')
    require(math.isfinite(budget_cap) and budget_cap > 0, 'Agent jobs have no configured budget')
    budget = limits.get('max_budget_usd', min(DEFAULT_MAX_BUDGET_USD, budget_cap))
    require(type(budget) in (int, float) and math.isfinite(budget) and 0 < budget <= budget_cap,
            f'limits.max_budget_usd must be above 0 and at most {budget_cap:g}')
    timeout = limits.get('timeout_seconds', DEFAULT_TIMEOUT_SECONDS)
    turns = limits.get('max_turns', DEFAULT_MAX_TURNS)
    model = body.get('model') or default_model
    frozen = {'format': INPUT_FORMAT,
              'prompt': text(body.get('prompt'), 'prompt', MAX_PROMPT_CHARS, required=True),
              'system_prompt': text(body.get('system_prompt'), 'system_prompt', MAX_SYSTEM_PROMPT_CHARS, required=False),
              'tools': [tool for tool in BUILTIN_TOOLS if tool in tools], 'graphs': sorted(graphs),
              'literature': body.get('literature', False), 'network': network, 'allowed_domains': domains,
              'effort': body.get('effort', 'medium'), 'model': model,
              'limits': {'timeout_seconds': timeout, 'max_turns': turns, 'max_budget_usd': float(budget)}}
    return validate_input(frozen)


def validate_input(value):
    """Strict check of frozen input bytes, repeated in the bundle and inside the Box."""
    require(isinstance(value, dict) and set(value) == INPUT_KEYS and value['format'] == INPUT_FORMAT, 'Invalid agent input format')
    text(value['prompt'], 'prompt', MAX_PROMPT_CHARS, required=True)
    text(value['system_prompt'], 'system_prompt', MAX_SYSTEM_PROMPT_CHARS, required=False)
    tools, graphs = value['tools'], value['graphs']
    require(isinstance(tools, list) and tools == [tool for tool in BUILTIN_TOOLS if tool in tools] and len(set(tools)) == len(tools),
            'Invalid frozen agent tools')
    require(isinstance(graphs, list) and graphs == sorted(set(graphs)) and set(graphs) <= set(GRAPHS), 'Invalid frozen agent graphs')
    require(type(value['literature']) is bool, 'literature must be true or false')
    require(value['network'] in NETWORKS and (value['network'] == 'open' or not OPEN_NETWORK_TOOLS & set(tools)),
            'Invalid frozen agent network')
    validate_domains(value['allowed_domains'], value['network'])
    require(value['effort'] in EFFORTS, 'effort must be low, medium or high')
    require(isinstance(value['model'], str) and MODEL_PATTERN.fullmatch(value['model']), 'model must be a Claude model id')
    limits = value['limits']
    require(isinstance(limits, dict) and set(limits) == {'timeout_seconds', 'max_turns', 'max_budget_usd'}, 'Invalid agent limits')
    require(type(limits['timeout_seconds']) is int and TIMEOUT_RANGE[0] <= limits['timeout_seconds'] <= TIMEOUT_RANGE[1],
            f'limits.timeout_seconds must be from {TIMEOUT_RANGE[0]} to {TIMEOUT_RANGE[1]}')
    require(type(limits['max_turns']) is int and 1 <= limits['max_turns'] <= 100, 'limits.max_turns must be from 1 to 100')
    budget = limits['max_budget_usd']
    require(type(budget) in (int, float) and math.isfinite(budget) and budget > 0, 'Invalid agent budget')
    return value


def validate_domains(domains, network):
    require(isinstance(domains, list) and len(domains) <= MAX_ALLOWED_DOMAINS and domains == sorted(set(domains))
            and all(isinstance(domain, str) and HOSTNAME.fullmatch(domain) for domain in domains),
            f'allowed_domains must be at most {MAX_ALLOWED_DOMAINS} exact hostnames such as eutils.ncbi.nlm.nih.gov')
    require(network == 'restricted' or not domains, 'allowed_domains applies only to the restricted network')
    return domains


def network_policy(selected_graphs, network, literature, allowed_domains=()):
    """Box egress for one agent run: the Anthropic API, the services its chosen tools call and the hosts the
    caller allowed; or everything."""
    if network == 'open': return {'mode': 'allow-all'}
    require(network == 'restricted', 'Invalid agent network')
    domains = [ANTHROPIC_HOST]
    if selected_graphs: domains.append('apps.okn.us')
    if literature: domains.append('www.ebi.ac.uk')
    domains += [domain for domain in allowed_domains if domain not in domains]
    return {'mode': 'custom', 'allowed_domains': domains}


def bootstrap_settings(value):
    """The part of the frozen input the Box bootstrap needs before the runner starts: its egress."""
    return {'network': value['network'], 'literature': value['literature'], 'allowed_domains': list(value['allowed_domains'])}


def validate_bootstrap_settings(settings):
    require(isinstance(settings, dict) and set(settings) == {'network', 'literature', 'allowed_domains'} and settings['network'] in NETWORKS
            and type(settings['literature']) is bool, 'Invalid agent bootstrap settings')
    validate_domains(settings['allowed_domains'], settings['network'])
    return settings


def response_record(job_id, attempt, status, result, secrets=()):
    """The trusted record of the agent's final answer, from Claude Code's terminal stream result."""
    answer = result.get('result') if isinstance(result, dict) else None
    answer = answer if isinstance(answer, str) else None
    if answer:
        for secret in secrets:
            if secret: answer = answer.replace(secret, '[REDACTED_CREDENTIAL]')
    usage = result if isinstance(result, dict) else {}
    def number(key):
        value = usage.get(key)
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    return {'format': RESPONSE_FORMAT, 'job_id': job_id, 'attempt': attempt, 'status': status, 'response': answer,
            'subtype': usage.get('subtype') if isinstance(usage.get('subtype'), str) else None,
            'is_error': usage.get('is_error') is True, 'num_turns': number('num_turns'),
            'duration_ms': number('duration_ms'), 'cost_usd': number('total_cost_usd')}


def job_result(record, captured_files, *, job_id, attempt):
    """The public job result: the answer (cut to a listing-safe size) and the files the agent wrote."""
    require(isinstance(record, dict) and record.get('format') == RESPONSE_FORMAT and record.get('job_id') == job_id
            and record.get('attempt') == attempt, 'Agent response record belongs to another execution')
    answer = record.get('response')
    require(answer is None or isinstance(answer, str), 'Invalid agent response text')
    answer = answer or ''
    files = [{'path': name[len('output/'):], 'size_bytes': item['size_bytes'], 'sha256': item['sha256']}
             for name, item in sorted(captured_files.items()) if name.startswith('output/')]
    duration = record.get('duration_ms')
    return {'kind': 'agent', 'response': answer[:MAX_RESULT_RESPONSE_CHARS],
            'response_truncated': len(answer) > MAX_RESULT_RESPONSE_CHARS,
            'response_sha256': hashlib.sha256(answer.encode()).hexdigest(),
            'turns': record.get('num_turns'), 'cost_usd': record.get('cost_usd'),
            'duration_seconds': round(duration / 1000, 3) if duration is not None else None,
            'files': files}


class KeyProxy:
    """Root-owned loopback relay to the Anthropic API, so the agent's uid never holds the API key.

    Claude Code talks to it through ANTHROPIC_BASE_URL with a placeholder key; the relay swaps in the real key
    and streams responses back unbuffered. A Bash-capable agent can still call it directly until the job's
    deadline, but it cannot carry the key anywhere else.
    """
    HOP = frozenset({'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding', 'te', 'trailer', 'upgrade',
                     'host', 'content-length', 'x-api-key', 'authorization', 'accept-encoding'})

    def __init__(self, api_key, *, host=ANTHROPIC_HOST, port=KEY_PROXY_PORT, connection=None):
        require(isinstance(api_key, str) and api_key, 'The key proxy needs an API key')
        self.api_key, self.host, self.port = api_key, host, port
        self.connection = connection or (lambda: http.client.HTTPSConnection(host, 443, timeout=900,
                                                                             context=ssl.create_default_context()))
        self.server = None

    def handler(self):
        relay = self
        class Handler(BaseHTTPRequestHandler):
            # Close-delimited responses: an SSE stream passes through chunk by chunk with no re-framing.
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                pass

            def forward(self):
                if not self.path.startswith('/v1/'):
                    self.send_error(404); return
                size = int(self.headers.get('Content-Length') or 0)
                if size < 0 or size > MAX_PROXY_REQUEST_BYTES:
                    self.send_error(413); return
                body = self.rfile.read(size) if size else None
                headers = {key: value for key, value in self.headers.items() if key.lower() not in relay.HOP}
                headers.update({'x-api-key': relay.api_key, 'accept-encoding': 'identity'})
                upstream = relay.connection()
                try:
                    upstream.request(self.command, self.path, body=body, headers=headers)
                    response = upstream.getresponse()
                    self.send_response(response.status, response.reason)
                    for key, value in response.getheaders():
                        if key.lower() not in relay.HOP - {'content-length'}: self.send_header(key, value)
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk); self.wfile.flush()
                except (OSError, http.client.HTTPException):
                    # Headers may already be sent; closing the connection is the only safe signal left.
                    self.close_connection = True
                finally:
                    upstream.close()

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = forward

        return Handler

    def start(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), self.handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def shutdown(self):
        if self.server:
            self.server.shutdown(); self.server.server_close(); self.server = None


def agent_environment(home, path, proxy):
    """The Claude Code process environment: the proxy address and a placeholder key, never the real key."""
    return {'PATH': path, 'HOME': home, 'ANTHROPIC_BASE_URL': proxy.base_url, 'ANTHROPIC_API_KEY': KEY_PROXY_PLACEHOLDER,
            'PYTHONDONTWRITEBYTECODE': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1'}
