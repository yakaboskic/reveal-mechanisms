"""General-purpose agent jobs: a caller's prompt runs in the isolated Box runtime research uses.

No scientific gates apply. The agent gets the tools the caller chose, works in a sandbox, and its final
answer becomes the job result. The API freezes the input, the trusted in-Box runner reads it, and
acceptance reads the trusted response record.

Standard library only: this module is bundled into the Box.
"""
import hashlib
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
import re
import ssl
import threading

from .box_claims import base_url as claims_base_url, host as claims_host_of
from .box_mcp import GRAPHS

# The one agent input format. An earlier one is refused: a job queued under it fails at prepare.
INPUT_FORMAT = 'reveal.agent-input/4'
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
DEFAULT_NETWORK = 'restricted'
# A scientific_account job fetches the papers it cites with its own commands, so by default it gets every built-in
# tool and the open network. On a network it restricts, its default leaves out WebFetch, which needs the open one.
ACCOUNT_DEFAULT_TOOLS = DEFAULT_TOOLS + ('WebFetch',)
ACCOUNT_DEFAULT_NETWORK = 'open'
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
    # Calls to the scoped REVEAL tools (graph schema and query, claims-repository lookups), counted together.
    'tool_calls': {'type': int, 'minimum': 1, 'maximum': 5000, 'default': 30, 'ceiling': 300},
}
TIMEOUT_RANGE = (LIMITS['timeout_seconds']['minimum'], LIMITS['timeout_seconds']['maximum'])
DEFAULT_TIMEOUT_SECONDS = LIMITS['timeout_seconds']['default']
DEFAULT_MAX_TURNS = LIMITS['max_turns']['default']
DEFAULT_MAX_BUDGET_USD = LIMITS['max_budget_usd']['default']
# Job rows are listed in full, so a long answer is cut there; the captured record keeps all of it.
MAX_RESULT_RESPONSE_CHARS = 50_000
INPUT_KEYS = {'format', 'prompt', 'system_prompt', 'tools', 'graphs', 'network', 'allowed_domains', 'effort', 'model', 'limits',
              'deliverable', 'account', 'claims_repository'}
# Present only on an answer job that asked for structured output.
OPTIONAL_INPUT_KEYS = {'output_schema'}
# Structured output (output_schema): a JSON Schema the final answer must satisfy. Claude Code enforces it during the run
# (--json-schema, the mechanism Upstash Box's response_schema drives), and the backend checks the result against it again.
MAX_OUTPUT_SCHEMA_BYTES = 32_000
MAX_OUTPUT_SCHEMA_DEPTH = 32
# Job rows are listed whole, so the structured result has a cap like the answer text's; a larger result fails the job.
MAX_STRUCTURED_OUTPUT_BYTES = 100_000
# What a job returns. 'answer': the agent's final message. 'scientific_account': a DAPPER ScientificAccount the agent
# authors from papers it fetches and saves itself, which the backend then attributes, mints and deposits. Both run in
# the same sandbox and take the same tools, network, allowed_domains and graphs.
DELIVERABLES = ('answer', 'scientific_account')
MAX_QUESTION_CHARS = 2000
MAX_ACCOUNT_CLAIMS = 20
MAX_CLAIM_CHARS = 1000
# A claim may carry the structured form of its Proposition: DAPPER's subject_entity, relation and object_entity,
# all three or none, each a CURIE or an absolute URI.
TRIPLE_SLOTS = ('subject_entity', 'relation', 'object_entity')
MAX_TERM_CHARS = 300
TERM = re.compile(r'[A-Za-z][A-Za-z0-9+._-]*:\S+')
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


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def output_schema(value):
    """The shape checks of an output_schema that need no JSON Schema library (this module runs inside the Box). The
    backend also checks it is a valid JSON Schema (agent_output.check_schema) before the job is queued."""
    require(isinstance(value, dict), 'output_schema must be a JSON Schema object')
    try:
        size = len(canonical_json(value).encode())
    except (TypeError, ValueError):
        raise AgentInputError('output_schema must be plain JSON') from None
    require(size <= MAX_OUTPUT_SCHEMA_BYTES, f'output_schema is {size:,} bytes; the most is {MAX_OUTPUT_SCHEMA_BYTES:,}')
    require(value.get('type') == 'object', 'output_schema must describe an object: its top level needs "type": "object"')

    def walk(node, depth):
        require(depth <= MAX_OUTPUT_SCHEMA_DEPTH, f'output_schema nests more than {MAX_OUTPUT_SCHEMA_DEPTH} levels')
        if isinstance(node, dict):
            for key, child in node.items():
                # A reference may point only inside the schema: nothing is fetched to resolve it.
                require(key != '$ref' or isinstance(child, str) and child.startswith('#'),
                        'output_schema may use only local references ("$ref": "#/...")')
                walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)
    walk(value, 0)
    return value


def require(condition, message):
    if not condition: raise AgentInputError(message)


def claim_entry(claim):
    """One requested claim as {'statement', and the three TRIPLE_SLOTS when given}. A claim is a statement, or an
    object with a statement and, together, subject_entity, relation and object_entity."""
    if isinstance(claim, str): claim = {'statement': claim}
    require(isinstance(claim, dict) and set(claim) <= {'statement', *TRIPLE_SLOTS},
            'each claim is a statement, or an object with statement and, together, ' + ', '.join(TRIPLE_SLOTS))
    entry = {'statement': text(claim.get('statement'), 'each claim', MAX_CLAIM_CHARS, required=True).strip()}
    given = [slot for slot in TRIPLE_SLOTS if claim.get(slot) is not None]
    if given:
        require(len(given) == len(TRIPLE_SLOTS), "a claim's " + ', '.join(TRIPLE_SLOTS) + ' go together: give all three or none')
        for slot in TRIPLE_SLOTS:
            require(isinstance(claim[slot], str) and len(claim[slot]) <= MAX_TERM_CHARS and TERM.fullmatch(claim[slot]),
                    f'{slot} must be a CURIE or an absolute URI of at most {MAX_TERM_CHARS} characters, such as HGNC.SYMBOL:LEPR')
            entry[slot] = claim[slot]
    return entry


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


def freeze_input(body, *, budget_cap=None, default_model=DEFAULT_MODEL, policy=None, account_builder=None, schema_checker=None):
    """The frozen input for one agent job, from a validated API body. Defaults are filled in so the frozen
    bytes say exactly what runs. policy gives each limit's default and ceiling (see LIMITS); a request may
    lower or raise a limit up to its ceiling. budget_cap, when given, is the budget ceiling.

    account_builder(question, claims) returns the account block of a scientific_account job (agent_account.py:
    it mints DAPPER nodes, which this standard-library module cannot). schema_checker(schema) raises AgentInputError
    when an output_schema is not a valid JSON Schema (agent_output.check_schema)."""
    require(isinstance(body, dict) and body.get('kind') == 'agent', 'Not an agent job')
    deliverable = body.get('deliverable', 'answer')
    require(deliverable in DELIVERABLES, 'deliverable must be answer or scientific_account')
    accounting = deliverable == 'scientific_account'
    require(accounting or not {'question', 'claims', 'attribute_requester'} & set(body),
            'question, claims and attribute_requester belong to deliverable "scientific_account"')
    schema = body.get('output_schema')
    require(schema is None or not accounting, 'output_schema belongs to deliverable "answer": a scientific_account job returns its account')
    if schema is not None:
        output_schema(schema)
        require(callable(schema_checker), 'Structured output is not available on this deployment')
        schema_checker(schema)
    require('literature' not in body, 'literature is not an agent job setting: the agent fetches papers itself with Bash, '
            'on network "open" or from the hosts in allowed_domains')
    network = body.get('network', ACCOUNT_DEFAULT_NETWORK if accounting else DEFAULT_NETWORK)
    require(network in NETWORKS, 'network must be restricted or open')
    if accounting:
        default_tools = [tool for tool in ACCOUNT_DEFAULT_TOOLS if network == 'open' or tool not in OPEN_NETWORK_TOOLS]
    else:
        default_tools = list(DEFAULT_TOOLS)
    tools = body.get('tools', default_tools)
    require(isinstance(tools, list) and all(tool in BUILTIN_TOOLS for tool in tools) and len(set(tools)) == len(tools),
            'tools must be distinct names from ' + ', '.join(BUILTIN_TOOLS))
    graphs = body.get('graphs', [])
    require(isinstance(graphs, list) and all(graph in GRAPHS for graph in graphs) and len(set(graphs)) == len(graphs),
            'graphs must be distinct names from ' + ', '.join(sorted(GRAPHS)))
    domains = body.get('allowed_domains', [])
    require(isinstance(domains, list) and all(isinstance(domain, str) for domain in domains), 'allowed_domains must be a list of hostnames')
    domains = sorted({domain.strip().lower().rstrip('.') for domain in domains})
    require(network == 'restricted' or not domains, 'allowed_domains narrows the restricted network; network "open" already reaches every host')
    require(network == 'open' or not OPEN_NETWORK_TOOLS & set(tools),
            'WebFetch downloads pages from inside the sandbox and needs network "open"; WebSearch works on the restricted network')
    account = None
    if accounting:
        question = text(body.get('question'), 'question', MAX_QUESTION_CHARS, required=True).strip()
        claims = body.get('claims', [])
        require(isinstance(claims, list) and len(claims) <= MAX_ACCOUNT_CLAIMS, f'claims must be a list of at most {MAX_ACCOUNT_CLAIMS} claims')
        claims = [claim_entry(claim) for claim in claims]
        require(len({claim['statement'] for claim in claims}) == len(claims), 'claims must be distinct statements')
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
              'network': network, 'allowed_domains': domains,
              'effort': body.get('effort', 'medium'), 'model': model, 'limits': frozen_limits,
              'deliverable': deliverable, 'account': account, 'claims_repository': claims_repository,
              **({'output_schema': schema} if schema is not None else {})}
    return validate_input(frozen)


def validate_input(value):
    """Strict check of frozen input bytes, repeated in the bundle and inside the Box."""
    require(isinstance(value, dict) and value.get('format') == INPUT_FORMAT and INPUT_KEYS <= set(value) <= INPUT_KEYS | OPTIONAL_INPUT_KEYS,
            'Invalid agent input format')
    if 'output_schema' in value:
        output_schema(value['output_schema'])
        require(value['deliverable'] == 'answer', 'output_schema belongs to deliverable "answer"')
    text(value['prompt'], 'prompt', MAX_PROMPT_CHARS, required=True)
    text(value['system_prompt'], 'system_prompt', MAX_SYSTEM_PROMPT_CHARS, required=False)
    tools, graphs = value['tools'], value['graphs']
    require(isinstance(tools, list) and tools == [tool for tool in BUILTIN_TOOLS if tool in tools] and len(set(tools)) == len(tools),
            'Invalid frozen agent tools')
    require(isinstance(graphs, list) and graphs == sorted(set(graphs)) and set(graphs) <= set(GRAPHS), 'Invalid frozen agent graphs')
    require(type(value['claims_repository']) is bool, 'claims_repository must be true or false')
    require(value['network'] in NETWORKS and (value['network'] == 'open' or not OPEN_NETWORK_TOOLS & set(tools)),
            'Invalid frozen agent network')
    validate_domains(value['allowed_domains'], value['network'])
    require(value['effort'] in EFFORTS, 'effort must be low, medium or high')
    require(isinstance(value['model'], str) and MODEL_PATTERN.fullmatch(value['model']), 'model must be a Claude model id')
    limits = value['limits']
    require(isinstance(limits, dict) and set(limits) == set(LIMITS), 'Invalid agent limits')
    for key in LIMITS:
        if key == 'max_budget_usd':
            # An operator may have raised the budget ceiling; only the type and sign are fixed.
            require(type(limits[key]) in (int, float) and math.isfinite(limits[key]) and limits[key] > 0, 'Invalid agent budget')
        else:
            require(limit_value(key, limits[key]), limit_range(key, LIMITS[key]['maximum']))
    validate_account(value)
    return value


def deliverable_of(value):
    """What a frozen input returns: 'answer' or 'scientific_account'."""
    return value['deliverable']


def schema_of(value):
    """The output_schema a frozen input asked the answer to satisfy, or None."""
    return value.get('output_schema')


def validate_account(value):
    """The account block of a frozen input: absent for an answer job; for an account job, the question, the
    caller's claims as fixed Propositions, and the minimal evidence package the account tools lint against. Checked
    for shape and internal agreement only; the ids were minted when the job was created."""
    deliverable, account = value['deliverable'], value['account']
    require(deliverable in DELIVERABLES, 'Invalid agent deliverable')
    if deliverable == 'answer':
        require(account is None, 'Only a scientific_account job carries an account')
        return
    require(isinstance(account, dict) and set(account) - {'attribute_requester'} == {'question', 'claims', 'package'}
            and type(account.get('attribute_requester', False)) is bool, 'Invalid account block')
    question, claims, package = account['question'], account['claims'], account['package']
    text(question, 'question', MAX_QUESTION_CHARS, required=True)
    require(isinstance(claims, list) and len(claims) <= MAX_ACCOUNT_CLAIMS and all(
        isinstance(claim, dict) and set(claim) in ({'statement', 'proposition_id'}, {'statement', 'proposition_id', *TRIPLE_SLOTS})
        and isinstance(claim['statement'], str)
        and 0 < len(claim['statement']) <= MAX_CLAIM_CHARS and isinstance(claim['proposition_id'], str)
        and re.fullmatch(DAPPER_ID % 'Proposition', claim['proposition_id'])
        and all(isinstance(claim[slot], str) and len(claim[slot]) <= MAX_TERM_CHARS and TERM.fullmatch(claim[slot])
                for slot in TRIPLE_SLOTS if slot in claim) for claim in claims), 'Invalid account claims')
    # prefixes: the CURIE bindings triples in this account may use beyond the pinned schema's own (agent_account.PREFIXES).
    require(isinstance(package, dict) and set(package) == ACCOUNT_PACKAGE_KEYS and package['source_artifacts'] == {}
            and isinstance(package['selection'], dict) and isinstance(package['dapper_context'], dict)
            and set(package['dapper_context']) <= {'knowledge_gaps', 'propositions', 'prefixes'}, 'Invalid account package')
    prefixes = package['dapper_context'].get('prefixes', {})
    require(isinstance(prefixes, dict) and all(isinstance(name, str) and isinstance(target, str) and target.startswith('https://')
                                               for name, target in prefixes.items()), 'Invalid account prefixes')
    gaps = package['dapper_context'].get('knowledge_gaps')
    require(isinstance(gaps, list) and len(gaps) == 1 and isinstance(gaps[0], dict) and gaps[0].get('text') == question
            and isinstance(gaps[0].get('id'), str) and re.fullmatch(DAPPER_ID % 'KnowledgeGap', gaps[0]['id'])
            and package['selection'].get('knowledge_gap_id') == gaps[0]['id'], 'The account package does not carry the question')
    propositions = package['dapper_context'].get('propositions', [])
    require(isinstance(propositions, list) and all(isinstance(node, dict) for node in propositions)
            and [(node.get('id'), node.get('statement'), *(node.get(slot) for slot in TRIPLE_SLOTS)) for node in propositions]
            == [(claim['proposition_id'], claim['statement'], *(claim.get(slot) for slot in TRIPLE_SLOTS)) for claim in claims],
            'The account package does not carry the claims')


def validate_domains(domains, network):
    require(isinstance(domains, list) and len(domains) <= MAX_ALLOWED_DOMAINS and domains == sorted(set(domains))
            and all(isinstance(domain, str) and HOSTNAME.fullmatch(domain) for domain in domains),
            f'allowed_domains must be at most {MAX_ALLOWED_DOMAINS} exact hostnames such as eutils.ncbi.nlm.nih.gov')
    require(network == 'restricted' or not domains, 'allowed_domains applies only to the restricted network')
    return domains


def network_policy(selected_graphs, network, allowed_domains=(), deliverable='answer', claims_host=None):
    """Box egress for one agent run: the Anthropic API, the services its chosen tools call and the hosts the
    caller allowed; or everything."""
    if network == 'open': return {'mode': 'allow-all'}
    require(network == 'restricted', 'Invalid agent network')
    domains = [ANTHROPIC_HOST]
    if selected_graphs: domains.append('apps.okn.us')
    if deliverable == 'scientific_account': domains.append(GITHUB_HOST)
    if claims_host and claims_host not in domains: domains.append(claims_host)
    domains += [domain for domain in allowed_domains if domain not in domains]
    return {'mode': 'custom', 'allowed_domains': domains}


def bootstrap_settings(value, claims_url=None):
    """The part of the frozen input the Box bootstrap needs before the runner starts: its egress.

    claims_url is this deployment's claims repository (REVEAL_CLAIMS_API_URL on the worker). It is frozen here, for a
    job that asked to read the repository, so the address the sandbox may reach and the query_claims tool it is
    offered are fixed with the bootstrap. A deployment with no repository offers no such tool."""
    settings = {'network': value['network'], 'allowed_domains': list(value['allowed_domains']), 'deliverable': value['deliverable']}
    claims_url = claims_base_url(claims_url)
    if value.get('claims_repository') and claims_url: settings['claims_url'] = claims_url
    return settings


def validate_bootstrap_settings(settings):
    base = {'network', 'allowed_domains', 'deliverable'}
    require(isinstance(settings, dict) and base <= set(settings) <= base | {'claims_url'} and settings['network'] in NETWORKS
            and settings['deliverable'] in DELIVERABLES
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
    structured = usage.get('structured_output') if isinstance(usage.get('structured_output'), (dict, list)) else None
    if structured is not None and secrets:
        def scrub(node):
            if isinstance(node, str):
                for secret in secrets:
                    if secret: node = node.replace(secret, '[REDACTED_CREDENTIAL]')
                return node
            if isinstance(node, dict): return {key: scrub(child) for key, child in node.items()}
            if isinstance(node, list): return [scrub(child) for child in node]
            return node
        structured = scrub(structured)
    return {'format': RESPONSE_FORMAT, 'job_id': job_id, 'attempt': attempt, 'status': status, 'response': answer,
            **({'structured_output': structured} if structured is not None else {}),
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
            'files': files, **({'account': account} if account else {}),
            # Present only for a job that asked for it; the backend checked it against the job's output_schema.
            **({'structured_output': record['structured_output']} if 'structured_output' in record else {})}


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
