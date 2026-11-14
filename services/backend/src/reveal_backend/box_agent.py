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

from .box_claims import base_url as claims_base_url, host as claims_host_of
from .box_mcp import GRAPHS

INPUT_FORMAT = 'reveal.agent-input/2'
# Jobs frozen before per-run tool and literature limits existed. They run with the allowances they were queued under.
LEGACY_INPUT_FORMAT = 'reveal.agent-input/1'
INPUT_FILENAME = 'agent-input.json'
RESPONSE_FORMAT = 'reveal.agent-response/1'
# The runner writes the response into the root-owned tool ledger, never into the agent-writable output.
RESPONSE_FILENAME = 'agent-response.json'
RESPONSE_CAPTURE_PATH = 'ledger/' + RESPONSE_FILENAME

BUILTIN_TOOLS = ('Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch')
DEFAULT_TOOLS = ('Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch')
# WebFetch downloads pages from inside the Box, so it needs the open network.
OPEN_NETWORK_TOOLS = frozenset({'WebFetch'})
NETWORKS = ('restricted', 'open')
EFFORTS = ('low', 'medium', 'high')
DEFAULT_MODEL = 'claude-opus-5-5'
MODEL_PATTERN = re.compile(r'claude-[a-z0-9-]{1,60}')
MAX_PROMPT_CHARS = 100_000
MAX_SYSTEM_PROMPT_CHARS = 50_000
# Every per-run limit of an agent job. A request may set any of them in `limits`.
#   default  what a request gets when it names none
#   ceiling  the most a request may ask for
#   minimum, maximum  the range the service supports at all
# Operators change default and ceiling (admin console or environment, see agent_limits.py). minimum and maximum
# are fixed here because this module's validation, which also runs inside the Box, and the Box adapter enforce them.
LIMITS = {
    'timeout_seconds': {'type': int, 'minimum': 60, 'maximum': 3600, 'default': 900, 'ceiling': 3600},
    'max_turns': {'type': int, 'minimum': 1, 'maximum': 1000, 'default': 50, 'ceiling': 100},
    'max_budget_usd': {'type': float, 'minimum': 0, 'maximum': 1000, 'default': 2.0, 'ceiling': 5.0},
    # Calls to the scoped REVEAL tools (paper search, paper read, graph schema and query), counted together.
    'tool_calls': {'type': int, 'minimum': 1, 'maximum': 5000, 'default': 30, 'ceiling': 300},
    'literature_searches': {'type': int, 'minimum': 1, 'maximum': 1000, 'default': 3, 'ceiling': 50},
    'literature_search_results': {'type': int, 'minimum': 1, 'maximum': 50, 'default': 5, 'ceiling': 25},
    # Distinct papers. Paging through one paper uses characters, never another paper.
    'literature_papers': {'type': int, 'minimum': 1, 'maximum': 1000, 'default': 4, 'ceiling': 50},
    'literature_characters': {'type': int, 'minimum': 1000, 'maximum': 20_000_000, 'default': 48_000, 'ceiling': 1_000_000},
}
LEGACY_LIMIT_KEYS = ('timeout_seconds', 'max_turns', 'max_budget_usd')
TIMEOUT_RANGE = (LIMITS['timeout_seconds']['minimum'], LIMITS['timeout_seconds']['maximum'])
DEFAULT_TIMEOUT_SECONDS = LIMITS['timeout_seconds']['default']
DEFAULT_MAX_TURNS = LIMITS['max_turns']['default']
DEFAULT_MAX_BUDGET_USD = LIMITS['max_budget_usd']['default']
# Job rows are listed in full, so a long answer is cut there; the captured record keeps all of it.
MAX_RESULT_RESPONSE_CHARS = 50_000
LEGACY_INPUT_KEYS = {'format', 'prompt', 'system_prompt', 'tools', 'graphs', 'literature', 'network', 'allowed_domains', 'effort', 'model', 'limits'}
INPUT_KEYS = LEGACY_INPUT_KEYS | {'deliverable', 'account', 'claims_repository'}
# What a job returns. 'answer': the agent's final message. 'scientific_account': a DAPPER ScientificAccount the agent
# authors from papers it reads through the captured literature tools, which the backend then attributes, mints and
# deposits. An account job has no shell and no network beyond its tools, like a research run: everything the
# account cites must be a captured read.
DELIVERABLES = ('answer', 'scientific_account')
ACCOUNT_TOOLS = ('Read', 'Write', 'Edit', 'Glob', 'Grep')
MAX_QUESTION_CHARS = 2000
MAX_ACCOUNT_CLAIMS = 20
MAX_CLAIM_CHARS = 1000
ACCOUNT_PACKAGE_KEYS = {'package_version', 'dapper_pin', 'selection', 'dapper_context', 'source_artifacts'}
DAPPER_ID = r'dapper:%s\.[A-Za-z0-9_-]{32}'
GITHUB_HOST = 'github.com'  # the Box clones the pinned DAPPER release, which the account tools lint against
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


def limit_value(key, value):
    """Whether value has the limit's type and lies in the range the service supports."""
    spec = LIMITS[key]
    if spec['type'] is int:
        return type(value) is int and spec['minimum'] <= value <= spec['maximum']
    return type(value) in (int, float) and math.isfinite(value) and spec['minimum'] < value <= spec['maximum']


def default_policy():
    """The default and ceiling of every limit when no operator has changed them."""
    return {key: {'default': spec['default'], 'ceiling': spec['ceiling']} for key, spec in LIMITS.items()}


def validate_policy(policy):
    require(isinstance(policy, dict) and set(policy) == set(LIMITS), 'Agent job limits are not configured')
    for key, entry in policy.items():
        require(isinstance(entry, dict) and set(entry) == {'default', 'ceiling'} and limit_value(key, entry['default'])
                and limit_value(key, entry['ceiling']) and entry['default'] <= entry['ceiling'],
                f'Agent job limit {key} is not configured')
    return policy


def limit_range(key, ceiling):
    if key == 'max_budget_usd': return f'limits.max_budget_usd must be above 0 and at most {ceiling:g}'
    return f"limits.{key} must be a whole number from {LIMITS[key]['minimum']} to {ceiling}"


def freeze_input(body, *, budget_cap=None, default_model=DEFAULT_MODEL, policy=None, account_builder=None):
    """The frozen input for one agent job, from a validated API body. Defaults are filled in so the frozen
    bytes say exactly what runs. policy gives each limit's default and ceiling (see LIMITS); a request may
    lower or raise a limit up to its ceiling. budget_cap, when given, is the budget ceiling.

    account_builder(question, claims) returns the account block of a scientific_account job (agent_account.py:
    it mints DAPPER nodes, which this standard-library module cannot)."""
    require(isinstance(body, dict) and body.get('kind') == 'agent', 'Not an agent job')
    deliverable = body.get('deliverable', 'answer')
    require(deliverable in DELIVERABLES, 'deliverable must be answer or scientific_account')
    accounting = deliverable == 'scientific_account'
    require(accounting or not {'question', 'claims', 'attribute_requester'} & set(body),
            'question, claims and attribute_requester belong to deliverable "scientific_account"')
    tools = body.get('tools', list(ACCOUNT_TOOLS if accounting else DEFAULT_TOOLS))
    require(isinstance(tools, list) and all(tool in BUILTIN_TOOLS for tool in tools) and len(set(tools)) == len(tools),
            'tools must be distinct names from ' + ', '.join(BUILTIN_TOOLS))
    graphs = body.get('graphs', [])
    require(isinstance(graphs, list) and all(graph in GRAPHS for graph in graphs) and len(set(graphs)) == len(graphs),
            'graphs must be distinct names from ' + ', '.join(sorted(GRAPHS)))
    network = body.get('network', 'restricted')
    require(network in NETWORKS, 'network must be restricted or open')
    domains = body.get('allowed_domains', [])
    require(isinstance(domains, list) and all(isinstance(domain, str) for domain in domains), 'allowed_domains must be a list of hostnames')
    domains = sorted({domain.strip().lower().rstrip('.') for domain in domains})
    require(network == 'restricted' or not domains, 'allowed_domains narrows the restricted network; network "open" already reaches every host')
    require(network == 'open' or not OPEN_NETWORK_TOOLS & set(tools),
            'WebFetch downloads pages from inside the sandbox and needs network "open"; WebSearch works on the restricted network')
    account = None
    if accounting:
        require(isinstance(tools, list) and set(tools) <= set(ACCOUNT_TOOLS), 'A scientific_account job reads papers through the captured '
                'literature tools and writes its account as a file: tools may only be ' + ', '.join(ACCOUNT_TOOLS))
        require(network == 'restricted' and not domains, 'A scientific_account job runs on the restricted network with no extra hosts: '
                'only captured reads can be cited')
        require(body.get('literature', True) is True, 'A scientific_account job needs literature: true')
        require(not graphs, 'A scientific_account job is built from papers the agent reads: graphs are not available for it')
        question = text(body.get('question'), 'question', MAX_QUESTION_CHARS, required=True).strip()
        claims = body.get('claims', [])
        require(isinstance(claims, list) and len(claims) <= MAX_ACCOUNT_CLAIMS and all(isinstance(claim, str) for claim in claims),
                f'claims must be a list of at most {MAX_ACCOUNT_CLAIMS} statements')
        claims = [text(claim, 'each claim', MAX_CLAIM_CHARS, required=True).strip() for claim in claims]
        require(len(set(claims)) == len(claims), 'claims must be distinct statements')
        require(callable(account_builder), 'Scientific account jobs are not available on this deployment')
        # The account names its requester only when the request asks for that. Only this yes or no is frozen: who the
        # requester is stays with the job's attribution, which never enters the sandbox.
        attribute = body.get('attribute_requester', False)
        require(type(attribute) is bool, 'attribute_requester must be true or false')
        account = {**account_builder(question, claims), 'attribute_requester': attribute}
    # Reading the claims repository is on for an account job unless the request turns it off, and off otherwise.
    claims_repository = body.get('claims_repository', accounting)
    require(type(claims_repository) is bool, 'claims_repository must be true or false')
    limits = body.get('limits') or {}
    require(isinstance(limits, dict), 'limits must be an object')
    unknown = sorted(str(key) for key in set(limits) - set(LIMITS))
    require(not unknown, 'limits has no ' + ', '.join(unknown)[:200] + '; it accepts ' + ', '.join(LIMITS))
    policy = default_policy() if policy is None else policy
    require(isinstance(policy, dict) and all(isinstance(entry, dict) for entry in policy.values()), 'Agent job limits are not configured')
    policy = {key: dict(entry) for key, entry in policy.items()}
    if budget_cap is not None:
        require(type(budget_cap) in (int, float) and math.isfinite(budget_cap) and budget_cap > 0, 'Agent jobs have no configured budget')
        budget = policy.get('max_budget_usd', {})
        policy['max_budget_usd'] = {'default': min(budget.get('default', DEFAULT_MAX_BUDGET_USD), budget_cap), 'ceiling': budget_cap}
    validate_policy(policy)
    frozen_limits = {}
    for key, spec in LIMITS.items():
        # Authoring an account takes many turns, and a run stopped at its time or turn limit returns nothing: an
        # account job gets the ceiling of both unless the request asks for less.
        default = policy[key]['ceiling'] if accounting and key in ('timeout_seconds', 'max_turns') else policy[key]['default']
        value = limits.get(key, default)
        if spec['type'] is int and type(value) is float and value.is_integer(): value = int(value)
        require(limit_value(key, value) and value <= policy[key]['ceiling'], limit_range(key, policy[key]['ceiling']))
        frozen_limits[key] = float(value) if spec['type'] is float else value
    model = body.get('model') or default_model
    frozen = {'format': INPUT_FORMAT,
              'prompt': text(body.get('prompt'), 'prompt', MAX_PROMPT_CHARS, required=True),
              'system_prompt': text(body.get('system_prompt'), 'system_prompt', MAX_SYSTEM_PROMPT_CHARS, required=False),
              'tools': [tool for tool in BUILTIN_TOOLS if tool in tools], 'graphs': sorted(graphs),
              'literature': True if accounting else body.get('literature', False), 'network': network, 'allowed_domains': domains,
              'effort': body.get('effort', 'medium'), 'model': model, 'limits': frozen_limits,
              'deliverable': deliverable, 'account': account, 'claims_repository': claims_repository}
    return validate_input(frozen)


def validate_input(value):
    """Strict check of frozen input bytes, repeated in the bundle and inside the Box."""
    require(isinstance(value, dict) and (value.get('format') == INPUT_FORMAT and set(value) == INPUT_KEYS
                                         or value.get('format') == LEGACY_INPUT_FORMAT and set(value) == LEGACY_INPUT_KEYS),
            'Invalid agent input format')
    text(value['prompt'], 'prompt', MAX_PROMPT_CHARS, required=True)
    text(value['system_prompt'], 'system_prompt', MAX_SYSTEM_PROMPT_CHARS, required=False)
    tools, graphs = value['tools'], value['graphs']
    require(isinstance(tools, list) and tools == [tool for tool in BUILTIN_TOOLS if tool in tools] and len(set(tools)) == len(tools),
            'Invalid frozen agent tools')
    require(isinstance(graphs, list) and graphs == sorted(set(graphs)) and set(graphs) <= set(GRAPHS), 'Invalid frozen agent graphs')
    require(type(value['literature']) is bool, 'literature must be true or false')
    require(type(value.get('claims_repository', False)) is bool, 'claims_repository must be true or false')
    require(value['network'] in NETWORKS and (value['network'] == 'open' or not OPEN_NETWORK_TOOLS & set(tools)),
            'Invalid frozen agent network')
    validate_domains(value['allowed_domains'], value['network'])
    require(value['effort'] in EFFORTS, 'effort must be low, medium or high')
    require(isinstance(value['model'], str) and MODEL_PATTERN.fullmatch(value['model']), 'model must be a Claude model id')
    limits = value['limits']
    keys = set(LIMITS) if value['format'] == INPUT_FORMAT else set(LEGACY_LIMIT_KEYS)
    require(isinstance(limits, dict) and set(limits) == keys, 'Invalid agent limits')
    for key in keys:
        if key == 'max_budget_usd':
            # An operator may have raised the budget ceiling; only the type and sign are fixed.
            require(type(limits[key]) in (int, float) and math.isfinite(limits[key]) and limits[key] > 0, 'Invalid agent budget')
        else:
            require(limit_value(key, limits[key]), limit_range(key, LIMITS[key]['maximum']))
    validate_account(value)
    return value


def deliverable_of(value):
    """What a frozen input returns. Inputs frozen before deliverables existed return an answer."""
    return value.get('deliverable', 'answer')


def validate_account(value):
    """The account block of a frozen input: absent for an answer job; for an account job, the question, the
    caller's claims as fixed Propositions, and the minimal evidence package the account tools lint against. Checked
    for shape and internal agreement only; the ids were minted when the job was created."""
    deliverable, account = deliverable_of(value), value.get('account')
    require(deliverable in DELIVERABLES, 'Invalid agent deliverable')
    if deliverable == 'answer':
        require(account is None, 'Only a scientific_account job carries an account')
        return
    require(set(value['tools']) <= set(ACCOUNT_TOOLS) and value['network'] == 'restricted' and not value['allowed_domains']
            and value['literature'] is True and not value['graphs'], 'Invalid scientific_account sandbox')
    require(isinstance(account, dict) and set(account) - {'attribute_requester'} == {'question', 'claims', 'package'}
            and type(account.get('attribute_requester', False)) is bool, 'Invalid account block')
    question, claims, package = account['question'], account['claims'], account['package']
    text(question, 'question', MAX_QUESTION_CHARS, required=True)
    require(isinstance(claims, list) and len(claims) <= MAX_ACCOUNT_CLAIMS and all(
        isinstance(claim, dict) and set(claim) == {'statement', 'proposition_id'} and isinstance(claim['statement'], str)
        and 0 < len(claim['statement']) <= MAX_CLAIM_CHARS and isinstance(claim['proposition_id'], str)
        and re.fullmatch(DAPPER_ID % 'Proposition', claim['proposition_id']) for claim in claims), 'Invalid account claims')
    require(isinstance(package, dict) and set(package) == ACCOUNT_PACKAGE_KEYS and package['source_artifacts'] == {}
            and isinstance(package['selection'], dict) and isinstance(package['dapper_context'], dict)
            and set(package['dapper_context']) <= {'knowledge_gaps', 'propositions'}, 'Invalid account package')
    gaps = package['dapper_context'].get('knowledge_gaps')
    require(isinstance(gaps, list) and len(gaps) == 1 and isinstance(gaps[0], dict) and gaps[0].get('text') == question
            and isinstance(gaps[0].get('id'), str) and re.fullmatch(DAPPER_ID % 'KnowledgeGap', gaps[0]['id'])
            and package['selection'].get('knowledge_gap_id') == gaps[0]['id'], 'The account package does not carry the question')
    propositions = package['dapper_context'].get('propositions', [])
    require(isinstance(propositions, list) and all(isinstance(node, dict) for node in propositions)
            and [(node.get('id'), node.get('statement')) for node in propositions]
            == [(claim['proposition_id'], claim['statement']) for claim in claims], 'The account package does not carry the claims')


def run_limits(value):
    """Every limit of a frozen input. A legacy input names three; the rest are what such a job always had."""
    return {key: value['limits'].get(key, spec['default']) for key, spec in LIMITS.items()}


def validate_domains(domains, network):
    require(isinstance(domains, list) and len(domains) <= MAX_ALLOWED_DOMAINS and domains == sorted(set(domains))
            and all(isinstance(domain, str) and HOSTNAME.fullmatch(domain) for domain in domains),
            f'allowed_domains must be at most {MAX_ALLOWED_DOMAINS} exact hostnames such as eutils.ncbi.nlm.nih.gov')
    require(network == 'restricted' or not domains, 'allowed_domains applies only to the restricted network')
    return domains


def network_policy(selected_graphs, network, literature, allowed_domains=(), deliverable='answer', claims_host=None):
    """Box egress for one agent run: the Anthropic API, the services its chosen tools call and the hosts the
    caller allowed; or everything."""
    if network == 'open': return {'mode': 'allow-all'}
    require(network == 'restricted', 'Invalid agent network')
    domains = [ANTHROPIC_HOST]
    if selected_graphs: domains.append('apps.okn.us')
    if literature: domains.append('www.ebi.ac.uk')
    if deliverable == 'scientific_account': domains.append(GITHUB_HOST)
    if claims_host and claims_host not in domains: domains.append(claims_host)
    domains += [domain for domain in allowed_domains if domain not in domains]
    return {'mode': 'custom', 'allowed_domains': domains}


def bootstrap_settings(value, claims_url=None):
    """The part of the frozen input the Box bootstrap needs before the runner starts: its egress.

    claims_url is this deployment's claims repository (REVEAL_CLAIMS_API_URL on the worker). It is frozen here, for a
    job that asked to read the repository, so the address the sandbox may reach and the query_claims tool it is
    offered are fixed with the bootstrap. A deployment with no repository offers no such tool."""
    settings = {'network': value['network'], 'literature': value['literature'], 'allowed_domains': list(value['allowed_domains']),
                'deliverable': deliverable_of(value)}
    claims_url = claims_base_url(claims_url)
    if value.get('claims_repository') and claims_url: settings['claims_url'] = claims_url
    return settings


def validate_bootstrap_settings(settings):
    # Bootstraps frozen before deliverables existed carry no deliverable: they are answer jobs.
    base = {'network', 'literature', 'allowed_domains'}
    require(isinstance(settings, dict) and base <= set(settings) <= base | {'deliverable', 'claims_url'} and settings['network'] in NETWORKS
            and type(settings['literature']) is bool and settings.get('deliverable', 'answer') in DELIVERABLES
            and ('claims_url' not in settings or claims_base_url(settings['claims_url']) == settings['claims_url']),
            'Invalid agent bootstrap settings')
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


def job_result(record, captured_files, *, job_id, attempt, account=None):
    """The public job result: the answer (cut to a listing-safe size), the files the agent wrote and, for a job that
    returns a scientific account, what was accepted and whether it was deposited (agent_account.public)."""
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
            'files': files, **({'account': account} if account else {})}


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
