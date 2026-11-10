"""General agent jobs: a caller's prompt, tools and limits run in the isolated Box runtime; the result is the
agent's final answer. Examples are synthetic, never model results."""
from copy import deepcopy

JOB_ID = '77777777-7777-4777-8777-777777777777'
CREATED_AT = '2026-11-09T15:00:00Z'
COMPLETED_AT = '2026-11-09T15:03:12Z'


def extend(b, f, e):
    from reveal_backend.box_agent import (BUILTIN_TOOLS, DEFAULT_MAX_BUDGET_USD, DEFAULT_MAX_TURNS, DEFAULT_MODEL, MAX_ALLOWED_DOMAINS,
        DEFAULT_TOOLS, DEFAULT_TIMEOUT_SECONDS, EFFORTS, MAX_PROMPT_CHARS, MAX_RESULT_RESPONSE_CHARS,
        MAX_SYSTEM_PROMPT_CHARS, NETWORKS, TIMEOUT_RANGE)
    from reveal_backend.box_mcp import GRAPHS
    S = b.SCHEMAS
    obj, ref, array, string, null, enum = b.obj, b.ref, b.array, b.string, b.nullable, b.enum
    digest = string(pattern='^[a-f0-9]{64}$')
    integer = lambda minimum, maximum=None, **kw: {'type': 'integer', 'minimum': minimum, **({'maximum': maximum} if maximum else {}), **kw}
    b.add('AgentJobLimits', obj({
        'timeout_seconds': integer(TIMEOUT_RANGE[0], TIMEOUT_RANGE[1], default=DEFAULT_TIMEOUT_SECONDS),
        'max_turns': integer(1, 100, default=DEFAULT_MAX_TURNS),
        'max_budget_usd': {'type': 'number', 'exclusiveMinimum': 0, 'default': DEFAULT_MAX_BUDGET_USD,
            'description': 'Lowers, never raises, the server cap (REVEAL_AGENT_JOB_MAX_BUDGET_USD). The default is the smaller of the two.'}},
        [], description='Per-run limits. The run stops at whichever comes first.'))
    b.add('AgentJobInput', obj({
        'kind': {'const': 'agent', 'type': 'string'},
        'prompt': string(minLength=1, maxLength=MAX_PROMPT_CHARS, description='The task. Sent to the agent as its first message.'),
        'system_prompt': string(minLength=1, maxLength=MAX_SYSTEM_PROMPT_CHARS,
            description="Appended to Claude Code's own system prompt, after a short note on the sandbox."),
        'tools': array(enum(*BUILTIN_TOOLS), uniqueItems=True, default=list(DEFAULT_TOOLS),
            description='Claude Code built-in tools the agent may use. Default: all of them on the open network, all but '
                        'WebFetch on the restricted network. WebFetch needs network open.'),
        'graphs': array(enum(*sorted(GRAPHS)), uniqueItems=True, default=[],
            description='OKN knowledge graphs the agent may query through the scoped graph tools.'),
        'literature': {'type': 'boolean', 'default': False, 'description': 'Europe PMC search_papers and read_paper tools.'},
        'network': enum(*NETWORKS, default='open', description=
            'open reaches the internet: web APIs, downloads, package installs and WebFetch. restricted reaches only the '
            'Anthropic API, the services of the selected graph and literature tools, and allowed_domains; it is the default '
            'when allowed_domains is given. Either way the agent never holds the API key.'),
        'allowed_domains': array(string(pattern='^[a-z0-9.-]{1,253}$'), uniqueItems=True, maxItems=MAX_ALLOWED_DOMAINS, default=[],
            description='Exact hostnames the restricted network may also reach over HTTPS, for example eutils.ncbi.nlm.nih.gov '
                        'or api.platform.opentargets.org. No wildcards or IP addresses. Given without network, they select '
                        'the restricted network. Not used with network open.'),
        'effort': enum(*EFFORTS, default='medium'),
        'model': string(pattern='^claude-[a-z0-9-]{1,60}$', description=f'Claude model id. Default {DEFAULT_MODEL} (or REVEAL_AGENT_JOB_MODEL).'),
        'limits': ref('AgentJobLimits')}, ['kind', 'prompt'],
        description='Runs a prompt as a general-purpose Claude Code agent in an isolated, disposable sandbox. No scientific '
            'validation or DAPPER objects; the result is the final answer and a list of the files the agent wrote to output/. '
            'Only workspaces named in REVEAL_AGENT_JOB_USERS (or, with *, any registered user) may start one (403 AGENT_JOBS_NOT_ENABLED), and only on '
            'the durable workflow runtime (409 AGENT_JOBS_UNAVAILABLE). Watch progress on the job events stream.'))
    b.add('AgentFile', obj({'path': string(), 'size_bytes': integer(0), 'sha256': digest}))
    b.add('AgentResult', obj({
        'kind': {'const': 'agent', 'type': 'string'},
        'response': string(maxLength=MAX_RESULT_RESPONSE_CHARS, description="The agent's final message."),
        'response_truncated': {'type': 'boolean', 'description': f'True when the final message exceeded {MAX_RESULT_RESPONSE_CHARS:,} characters.'},
        'response_sha256': digest,
        'turns': null(integer(0)), 'cost_usd': null({'type': 'number', 'minimum': 0}),
        'duration_seconds': null({'type': 'number', 'minimum': 0}),
        'files': array(ref('AgentFile'), description='Files the agent wrote to output/, retained with the job capture.')}))
    S['JobCreate']['oneOf'].append(ref('AgentJobInput'))
    S['JobCreate']['discriminator']['mapping']['agent'] = '#/components/schemas/AgentJobInput'
    S['Job']['properties']['kind']['enum'].append('agent')
    S['Job']['properties']['stage']['enum'].append('running_agent')
    for name in ('Job', 'JobEvent'):
        S[name]['properties']['result']['anyOf'][0]['oneOf'].append(ref('AgentResult'))
    S['Job']['allOf'].append({'if': {'properties': {'kind': {'const': 'agent'}}}, 'then': {'properties': {
        'research_request_id': {'type': 'null'}, 'input_account_id': {'type': 'null'}, 'result': null(ref('AgentResult'))}}})
    operation = b.PATHS['/v1/jobs']['post']
    operation['summary'] = 'Start an analysis, paragraph or general agent job'
    operation['description'] += (' kind=agent runs a caller prompt as a general-purpose agent with the chosen tools and limits; '
        'it needs a workspace named in REVEAL_AGENT_JOB_USERS (or * for registered users) and the durable workflow runtime.')
    request = {'kind': 'agent', 'prompt': 'Find three recent PubMed papers on LEPR variants and insulin resistance with NCBI '
        'E-utilities, look up LEPR\'s known drugs in the Open Targets GraphQL API, and save a short summary as output/summary.md.',
        'system_prompt': 'Be concise. Cite PMIDs.', 'tools': ['Read', 'Write', 'Bash'],
        'allowed_domains': ['api.platform.opentargets.org', 'eutils.ncbi.nlm.nih.gov'],
        'limits': {'timeout_seconds': 600, 'max_turns': 30, 'max_budget_usd': 1}}
    operation['requestBody']['content']['application/json']['examples']['agent'] = {'summary': 'General agent', 'value': request}
    links = {'self': '/v1/jobs/' + JOB_ID, 'events': '/v1/jobs/' + JOB_ID + '/events', 'cancel': '/v1/jobs/' + JOB_ID + '/cancel'}
    queued = {'id': JOB_ID, 'kind': 'agent', 'owner_user_id': '11111111-1111-4111-8111-111111111111', 'status': 'queued',
              'stage': 'queued', 'research_request_id': None, 'input_account_id': None, 'created_at': CREATED_AT,
              'updated_at': CREATED_AT, 'completed_at': None, 'result': None, 'failure': None, 'warnings': [],
              'last_event_id': '1', 'links': links}
    operation['responses']['202']['content']['application/json']['examples']['agent_accepted'] = {'summary': 'Agent accepted', 'value': queued}
    answer = ('1. Does LEPR signalling loss act through hypothalamic or peripheral insulin resistance? (PMID 00000000)\n'
              '2. ...\n\nSaved to output/questions.md.')
    complete = dict(deepcopy(queued), status='succeeded', stage='complete', updated_at=COMPLETED_AT, completed_at=COMPLETED_AT,
        last_event_id='42', result={'kind': 'agent', 'response': answer, 'response_truncated': False,
            'response_sha256': b.sha(answer.encode()), 'turns': 9, 'cost_usd': 0.41, 'duration_seconds': 182.4,
            'files': [{'path': 'questions.md', 'size_bytes': 1874, 'sha256': '0' * 63 + '1'}]})
    getter = b.PATHS['/v1/jobs/{job_id}']['get']
    getter['responses']['200']['content']['application/json']['examples']['agent_complete'] = {
        'summary': 'Agent complete (synthetic)', 'value': complete}
    for parameter in b.PATHS['/v1/jobs']['get'].get('parameters', []):
        if parameter['name'] == 'kind': parameter['schema']['enum'].append('agent')
