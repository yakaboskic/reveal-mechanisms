"""General agent jobs: a caller's prompt, tools and limits run in the isolated Box runtime; the result is the
agent's final answer. Examples are synthetic, never model results."""
from copy import deepcopy

JOB_ID = '77777777-7777-4777-8777-777777777777'
CREATED_AT = '2026-11-09T15:00:00Z'
COMPLETED_AT = '2026-11-09T15:03:12Z'


def extend(b, f, e):
    from reveal_backend.box_agent import (ACCOUNT_TOOLS, BUILTIN_TOOLS, DEFAULT_MODEL, DELIVERABLES, LIMITS, MAX_ACCOUNT_CLAIMS,
        MAX_ALLOWED_DOMAINS, MAX_CLAIM_CHARS, DEFAULT_TOOLS, EFFORTS, MAX_PROMPT_CHARS, MAX_QUESTION_CHARS,
        MAX_RESULT_RESPONSE_CHARS, MAX_SYSTEM_PROMPT_CHARS, NETWORKS)
    from reveal_backend.box_mcp import GRAPHS
    S = b.SCHEMAS
    obj, ref, array, string, null, enum = b.obj, b.ref, b.array, b.string, b.nullable, b.enum
    digest = string(pattern='^[a-f0-9]{64}$')
    integer = lambda minimum, maximum=None, **kw: {'type': 'integer', 'minimum': minimum, **({'maximum': maximum} if maximum else {}), **kw}
    # The schema carries the range the service supports and the built-in default. An operator may change each
    # limit's default and ceiling (admin console or environment), so a value inside the schema's range can still
    # be refused with 422 when it is above the current ceiling.
    meaning = {
        'timeout_seconds': 'Wall-clock seconds for the run.',
        'max_turns': 'Claude Code turns.',
        'max_budget_usd': 'Provider cost of the run in US dollars.',
        'tool_calls': 'Calls to the scoped REVEAL tools (search_papers, read_paper and the graph tools), counted together. '
                      'When they are used up the agent is told and can still finish.',
        'literature_searches': 'search_papers calls.',
        'literature_search_results': 'Most records one search_papers call may return.',
        'literature_papers': 'Distinct papers read_paper may open. Paging through one paper with offset does not open another.',
        'literature_characters': 'Characters of abstract and full text read_paper may return across the run.'}
    def limit(key):
        spec = LIMITS[key]
        if spec['type'] is int:
            schema = integer(spec['minimum'], spec['maximum'], default=spec['default'])
        else:
            schema = {'type': 'number', 'exclusiveMinimum': spec['minimum'], 'maximum': spec['maximum'], 'default': spec['default']}
        return {**schema, 'description': meaning[key]}
    b.add('AgentJobLimits', obj({key: limit(key) for key in LIMITS}, [], description=
        'Per-run limits. The run stops at the first of timeout_seconds, max_turns and max_budget_usd. Each limit has a default, used '
        'when the request leaves it out, and a ceiling, the most a request may ask for; a value above the ceiling is refused with 422 '
        'and the message names the ceiling. Operators set both per deployment, so the defaults and maximums shown here are the '
        'built-in ones and the range the service supports.'))
    b.add('AgentJobInput', obj({
        'kind': {'const': 'agent', 'type': 'string'},
        'prompt': string(minLength=1, maxLength=MAX_PROMPT_CHARS, description='The task. Sent to the agent as its first message.'),
        'system_prompt': string(minLength=1, maxLength=MAX_SYSTEM_PROMPT_CHARS,
            description="Appended to Claude Code's own system prompt, after a short note on the sandbox."),
        'tools': array(enum(*BUILTIN_TOOLS), uniqueItems=True, default=list(DEFAULT_TOOLS),
            description='Claude Code built-in tools the agent may use. WebFetch needs network open.'),
        'graphs': array(enum(*sorted(GRAPHS)), uniqueItems=True, default=[],
            description='OKN knowledge graphs the agent may query through the scoped graph tools.'),
        'literature': {'type': 'boolean', 'default': False, 'description': 'Europe PMC search_papers and read_paper tools.'},
        'network': enum(*NETWORKS, default='restricted', description=
            'restricted reaches only the Anthropic API and the services of the selected graph and literature tools; '
            'open reaches the internet (package installs, downloads, WebFetch). Either way the agent never holds the API key.'),
        'allowed_domains': array(string(pattern='^[a-z0-9.-]{1,253}$'), uniqueItems=True, maxItems=MAX_ALLOWED_DOMAINS, default=[],
            description='Extra exact hostnames the restricted network may reach over HTTPS, for example eutils.ncbi.nlm.nih.gov '
                        'or api.platform.opentargets.org. No wildcards or IP addresses. Not used with network open.'),
        'effort': enum(*EFFORTS, default='medium'),
        'model': string(pattern='^claude-[a-z0-9-]{1,60}$', description=f'Claude model id. Default {DEFAULT_MODEL} (or REVEAL_AGENT_JOB_MODEL).'),
        'limits': ref('AgentJobLimits'),
        'deliverable': enum(*DELIVERABLES, default='answer', description=
            'What the job returns. answer: the final message. scientific_account: a DAPPER ScientificAccount the agent authors '
            'from papers it reads through the captured literature tools; REVEAL then checks and mints it. Such a job needs '
            f'question, has literature on, may use only the tools {", ".join(ACCOUNT_TOOLS)} (the default for it), runs on the '
            'restricted network with no allowed_domains and no graphs, and by default gets the run-time and turn ceilings.'),
        'question': string(minLength=1, maxLength=MAX_QUESTION_CHARS, description=
            'Only with deliverable scientific_account, where it is required: the question the account addresses. It is recorded '
            'in the account as a requester-supplied question, not a curated knowledge gap.'),
        'attribute_requester': {'type': 'boolean', 'default': False, 'description':
            'Only with deliverable scientific_account: name the requester in the account as the Person it is attributed to '
            '(display name, and the ORCID iD when it was authenticated). Default false: the account and its deposit say what '
            'made it and not who asked.'},
        'claims_repository': {'type': 'boolean', 'description':
            'Offer the query_claims tool: read-only lookups in the claims repository (Propositions, Claims and evidence that '
            'earlier runs and other groups deposited). Default true for deliverable scientific_account, false otherwise. The '
            'tool is offered only on a deployment that has a claims repository configured; what it returns cannot be cited as evidence.'},
        'claims': array(string(minLength=1, maxLength=MAX_CLAIM_CHARS), uniqueItems=True, maxItems=MAX_ACCOUNT_CLAIMS, default=[],
            description='Only with deliverable scientific_account: statements the requester wants assessed. Each becomes a fixed '
                        'DAPPER Proposition; the agent assesses it from literature (supporting or disputing) and cannot reword it. '
                        'A claim the agent found no literature for is reported in the result, not included in the account.')},
        ['kind', 'prompt'],
        description='Runs a prompt as a general-purpose Claude Code agent in an isolated, disposable sandbox. No scientific '
            'validation or DAPPER objects; the result is the final answer and a list of the files the agent wrote to output/. '
            'Only workspaces named in REVEAL_AGENT_JOB_USERS (or, with *, any registered user) may start one (403 AGENT_JOBS_NOT_ENABLED), and only on '
            'the durable workflow runtime (409 AGENT_JOBS_UNAVAILABLE). Watch progress on the job events stream.'))
    b.add('AgentFile', obj({'path': string(), 'size_bytes': integer(0), 'sha256': digest}))
    dapper_id = lambda name: string(pattern='^dapper:' + name + r'\.[A-Za-z0-9_-]{8,64}$')
    b.add('AgentAccountDeposit', obj({
        'status': enum('deposited', 'failed', 'not_configured', description=
            'deposited: the claims repository stored every object of the account. not_configured: this deployment has no '
            'claims repository. failed: the attempt did not succeed; the account itself is unaffected and stays with the job.'),
        'deposit_id': null(string(description="The repository's receipt id for this deposit.")),
        'objects': integer(0, description='Objects the repository accepted, counting ones it already held.'),
        'stored_differs': integer(0, description='Objects the repository already held under the same id with different '
                                                 'unhashed content. Its stored copy stands.'),
        'unresolved_references': integer(0),
        'reason': enum('too_large', 'rejected', 'unauthorized', 'unavailable'),
        'http_status': integer(100, 599),
        'detail': string(maxLength=500, description="The repository's own reason for refusing the document.")}, ['status']))
    b.add('AgentAccountPaper', obj({
        'source': enum('MED', 'PMC'), 'id': string(), 'section': enum('abstract', 'full_text'),
        'reference': null(string(description='PMID, PMCID or DOI of the paper.')),
        'text_sha256': digest, 'text_bytes': integer(0), 'media_type': string(), 'filename': string(),
        'evidence_items': integer(1, description='EvidenceItems that quote this text.'),
        'file_id': dapper_id('File')}, description=
        'One paper text the account quotes. The account names it by checksum and does not carry it; the owner downloads the '
        'exact bytes a ValueLocator addresses from /v1/jobs/{job_id}/account/texts/{text_sha256}.'))
    b.add('AgentAccountRequestedClaim', obj({
        'statement': string(), 'assessed': {'type': 'boolean', 'description': 'False when the agent made no Claim on it.'},
        'claim_ids': array(dapper_id('Claim'))}))
    b.add('AgentAccount', obj({
        'account_id': dapper_id('ScientificAccount'),
        'dapper_version': string(description='DAPPER version the returned and deposited document is minted with.'),
        'document_sha256': digest, 'document_bytes': integer(0),
        'objects': integer(0), 'edges': integer(0), 'claims': integer(0),
        'value_locators': integer(0, description='Quoted passages located by exact byte span in a paper text.'),
        'lint_warnings': integer(0),
        'requested_claims': array(ref('AgentAccountRequestedClaim'), description='What became of each claim in the request.'),
        'papers': array(ref('AgentAccountPaper')),
        'deposit': ref('AgentAccountDeposit')}, description=
        'The ScientificAccount a deliverable scientific_account job returned. Fetch the document itself from '
        '/v1/jobs/{job_id}/account.'))
    b.add('AgentAccountDocument', obj({
        'job_id': string(format='uuid'), 'account': ref('AgentAccount'),
        'document': {'type': 'object', 'additionalProperties': True, 'description':
            'The DAPPER document: plural arrays of objects (scientific_accounts, claims, propositions, evidence_items, '
            'value_locators, files, publications, ...) and edges, every id a DAPPER-ID-1 address.'}}))
    b.add('AgentResult', obj({
        'kind': {'const': 'agent', 'type': 'string'},
        'response': string(maxLength=MAX_RESULT_RESPONSE_CHARS, description="The agent's final message."),
        'response_truncated': {'type': 'boolean', 'description': f'True when the final message exceeded {MAX_RESULT_RESPONSE_CHARS:,} characters.'},
        'response_sha256': digest,
        'turns': null(integer(0)), 'cost_usd': null({'type': 'number', 'minimum': 0}),
        'duration_seconds': null({'type': 'number', 'minimum': 0}),
        'files': array(ref('AgentFile'), description='Files the agent wrote to output/, retained with the job capture.'),
        'account': ref('AgentAccount')},
        ['kind', 'response', 'response_truncated', 'response_sha256', 'turns', 'cost_usd', 'duration_seconds', 'files']))
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
    literature = {'kind': 'agent', 'prompt': 'Search Europe PMC for recent papers on LEPR variants and insulin resistance. Read the '
        'open-access ones in full and save a short summary that cites each PMID as output/summary.md.',
        'tools': ['Read', 'Write'], 'literature': True,
        'limits': {'literature_searches': 10, 'literature_papers': 20, 'literature_characters': 400000, 'tool_calls': 120}}
    operation['requestBody']['content']['application/json']['examples']['agent'] = {'summary': 'General agent', 'value': request}
    account = {'kind': 'agent', 'deliverable': 'scientific_account',
        'prompt': 'Build the account from open-access papers. Prefer primary studies in mice and humans; say what was not read.',
        'question': 'Does loss of leptin receptor signalling in hepatocytes cause hepatic insulin resistance?',
        'claims': ['Hepatocyte-specific deletion of LEPR reduces insulin-stimulated AKT phosphorylation in liver.'],
        'limits': {'literature_searches': 10, 'literature_papers': 20, 'literature_characters': 400000, 'tool_calls': 150}}
    operation['requestBody']['content']['application/json']['examples']['agent_account'] = {
        'summary': 'General agent authoring a DAPPER ScientificAccount from literature', 'value': account}
    operation['requestBody']['content']['application/json']['examples']['agent_literature'] = {
        'summary': 'General agent reading literature with its own limits', 'value': literature}
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
    account_id = 'dapper:ScientificAccount.' + 'A' * 32
    text_sha = '0' * 63 + '2'
    account_block = {'account_id': account_id, 'dapper_version': '0.2.1-a1', 'document_sha256': '0' * 63 + '3', 'document_bytes': 9120,
        'objects': 11, 'edges': 4, 'claims': 1, 'value_locators': 1, 'lint_warnings': 1,
        'requested_claims': [{'statement': account['claims'][0], 'assessed': True, 'claim_ids': ['dapper:Claim.' + 'C' * 32]}],
        'papers': [{'source': 'PMC', 'id': 'PMC0000000', 'section': 'full_text', 'reference': 'PMID:00000000', 'text_sha256': text_sha,
                    'text_bytes': 48211, 'media_type': 'text/plain', 'filename': 'PMC0000000.fulltext.txt', 'evidence_items': 1,
                    'file_id': 'dapper:File.' + 'F' * 32}],
        'deposit': {'status': 'deposited', 'deposit_id': 'dep_' + '0' * 32, 'objects': 11, 'stored_differs': 0, 'unresolved_references': 0}}
    account_complete = deepcopy(complete)
    account_complete['result'].update(files=[{'path': 'account-1.json', 'size_bytes': 5210, 'sha256': '0' * 63 + '4'}], account=account_block)
    getter['responses']['200']['content']['application/json']['examples']['agent_account_complete'] = {
        'summary': 'Agent complete with a ScientificAccount (synthetic)', 'value': account_complete}
    job_parameter = b.parameter('job_id', 'path', string(format='uuid'), JOB_ID, True)
    b.operation('/v1/jobs/{job_id}/account', 'get', 'getJobAccount', 'Jobs', 'Read the ScientificAccount an agent job returned',
        'Owner-only. The account of a deliverable scientific_account job, in the form REVEAL deposits it: one DAPPER document '
        'with every object the account needs, quoted passages as ValueLocators over paper texts named by checksum. Returns 404 '
        'until the job has succeeded, and for any other job. The document names the requester as the Person the account is '
        'attributed to only when the job asked for that with attribute_requester.',
        'AgentAccountDocument', {'account': {'job_id': JOB_ID, 'account': account_block, 'document': {
            'scientific_accounts': [{'id': account_id, 'name': 'LEPR and hepatic insulin signalling'}]}}},
        parameters=[job_parameter], errors=('401', '404', '429'))
    text = b.operation('/v1/jobs/{job_id}/account/texts/{sha256}', 'get', 'downloadJobAccountText', 'Jobs',
        'Download a paper text an agent job account quotes',
        'Owner-only. The exact bytes a ValueLocator of the account addresses: the text of one paper as the agent read it '
        '(text_sha256 in the account block). REVEAL keeps it for the owner and does not publish it. Attachment disposition, '
        'private no-store caching and nosniff, like a captured source.',
        string(format='binary'), {'paper_text': 'Hepatocyte-specific deletion of Lepr reduced insulin-stimulated AKT phosphorylation.'},
        parameters=[job_parameter, b.parameter('sha256', 'path', string(pattern='^[a-f0-9]{64}$'), text_sha, True)],
        errors=('401', '404', '503'))
    text['responses']['200']['content'] = {'*/*': text['responses']['200']['content']['application/json']}
    text['responses']['307'] = {'description': 'After authorization, redirect to the exact stored object. The private URL expires after 60 seconds.',
        'headers': {'Location': {'description': 'Short-lived download URL. Never cache or persist as an identifier.',
                                 'schema': {'type': 'string', 'format': 'uri'}}}}
    for exchange in b.EXCHANGES:
        if exchange['operation_id'] == 'downloadJobAccountText':
            exchange['request']['headers']['Accept'] = '*/*'
            exchange['responses']['200']['content_type'] = '*/*'
    for parameter in b.PATHS['/v1/jobs']['get'].get('parameters', []):
        if parameter['name'] == 'kind': parameter['schema']['enum'].append('agent')
