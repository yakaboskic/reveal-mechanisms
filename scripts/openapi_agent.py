"""General agent jobs: a caller's prompt, tools and limits run in the isolated Box runtime; the result is the
agent's final answer. Examples are synthetic, never model results."""
from copy import deepcopy

JOB_ID = '77777777-7777-4777-8777-777777777777'
CREATED_AT = '2026-11-09T15:00:00Z'
COMPLETED_AT = '2026-11-09T15:03:12Z'


def extend(b, f, e):
    from reveal_backend.box_agent import (ACCOUNT_DEFAULT_NETWORK, ACCOUNT_DEFAULT_TOOLS, BUILTIN_TOOLS, DEFAULT_MODEL, DEFAULT_NETWORK,
        DELIVERABLES, LIMITS, MAX_ACCOUNT_CLAIMS,
        MAX_ALLOWED_DOMAINS, MAX_CLAIM_CHARS, MAX_OUTPUT_SCHEMA_BYTES, MAX_STRUCTURED_OUTPUT_BYTES, DEFAULT_TOOLS, EFFORTS, MAX_PROMPT_CHARS, MAX_QUESTION_CHARS,
        MAX_RESULT_RESPONSE_CHARS, MAX_SYSTEM_PROMPT_CHARS, MAX_TERM_CHARS, NETWORKS, TERM)
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
        'tool_calls': 'Calls to the scoped REVEAL tools (the graph tools and query_claims), counted together. '
                      'When they are used up the agent is told and can still finish.'}
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
            description='Claude Code built-in tools the agent may use. WebFetch needs network open. The default shown is for deliverable '
                        f'answer; deliverable scientific_account defaults to {", ".join(ACCOUNT_DEFAULT_TOOLS)} (without WebFetch when '
                        'the request sets network restricted).'),
        'graphs': array(enum(*sorted(GRAPHS)), uniqueItems=True, default=[],
            description='OKN knowledge graphs the agent may query through the scoped graph tools.'),
        'network': enum(*NETWORKS, default=DEFAULT_NETWORK, description=
            'restricted reaches only the Anthropic API, the services of the selected graph tools and query_claims, and the hosts in '
            'allowed_domains; open reaches the internet (package installs, downloads, WebFetch). The default shown is for deliverable '
            f'answer; deliverable scientific_account defaults to {ACCOUNT_DEFAULT_NETWORK}. Either way the agent never holds the API key.'),
        'allowed_domains': array(string(pattern='^[a-z0-9.-]{1,253}$'), uniqueItems=True, maxItems=MAX_ALLOWED_DOMAINS, default=[],
            description='Extra exact hostnames the restricted network may reach over HTTPS, for example eutils.ncbi.nlm.nih.gov '
                        'or api.platform.opentargets.org. No wildcards or IP addresses. Not used with network open.'),
        'effort': enum(*EFFORTS, default='medium'),
        'model': string(pattern='^claude-[a-z0-9-]{1,60}$', description=f'Claude model id. Default {DEFAULT_MODEL} (or REVEAL_AGENT_JOB_MODEL).'),
        'limits': ref('AgentJobLimits'),
        'deliverable': enum(*DELIVERABLES, default='answer', description=
            'What the job returns. answer: the final message. scientific_account: a DAPPER ScientificAccount the agent authors '
            'from papers it fetches with its own commands and saves under output/sources/; REVEAL then checks and mints it. Such a '
            'job needs question. It takes the same tools, network, allowed_domains and graphs as an answer job, defaults to Bash, '
            'WebSearch and WebFetch on the open network so it can reach the papers, and by default gets the run-time and turn ceilings.'),
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
        'output_schema': {'type': 'object', 'additionalProperties': True, 'description':
            'Only with deliverable answer: a JSON Schema (Draft 2020-12 unless it names another draft with $schema) that the '
            'final answer must satisfy. Its top level must be "type": "object"; it may use only local references ($ref "#/..."); '
            f'it is at most {MAX_OUTPUT_SCHEMA_BYTES:,} bytes. Claude Code is run with it as --json-schema, the mechanism behind '
            "Upstash Box's response_schema, and returns the object as structured output; REVEAL checks the object against the "
            'schema again and returns it as result.structured_output. A run that returns no such object, or one that does not '
            'satisfy the schema, fails with VALIDATION_FAILED. An invalid schema is refused with 422.'},
        'claims': array({'oneOf': [string(minLength=1, maxLength=MAX_CLAIM_CHARS), ref('AgentAccountClaim')]}, uniqueItems=True,
            maxItems=MAX_ACCOUNT_CLAIMS, default=[],
            description='Only with deliverable scientific_account: what the requester wants assessed. Each item is a statement, '
                        'or an object that adds the statement\'s structured form (subject_entity, relation, object_entity). Each '
                        'becomes a fixed DAPPER Proposition; the agent assesses it from literature (supporting or disputing) and '
                        'cannot reword it. Statements must be distinct. A claim the agent found no literature for is reported in '
                        'the result, not included in the account.')},
        ['kind', 'prompt'],
        description='Runs a prompt as a general-purpose Claude Code agent in an isolated, disposable sandbox. No scientific '
            'validation or DAPPER objects; the result is the final answer and a list of the files the agent wrote to output/. '
            'Only workspaces named in REVEAL_AGENT_JOB_USERS (or, with *, any registered user) may start one (403 AGENT_JOBS_NOT_ENABLED), and only on '
            'the durable workflow runtime (409 AGENT_JOBS_UNAVAILABLE). Watch progress on the job events stream.'))
    b.add('AgentFile', obj({'path': string(), 'size_bytes': integer(0), 'sha256': digest}))
    dapper_id = lambda name: string(pattern='^dapper:' + name + r'\.[A-Za-z0-9_-]{8,64}$')
    b.add('AgentAccountDeposit', obj({
        'status': enum('deposited', 'failed', 'not_configured', 'withheld_unsafe', description=
            'deposited: the claims repository stored every object of the account. not_configured: this deployment has no '
            'claims repository. failed: the attempt did not succeed; the account itself is unaffected and stays with the job. '
            'withheld_unsafe: REVEAL\'s prompt-injection screen found text in the account, or in what the run read, that '
            'could be an attack, so the account was never sent; the job still succeeds and the account stays with the job. '
            'The screen looks for attacks, not scientific quality; detail says why in one line, and there is no review or retry.'),
        'deposit_id': null(string(description="The repository's receipt id for this deposit.")),
        'objects': integer(0, description='Objects the repository accepted, counting ones it already held.'),
        'stored_differs': integer(0, description='Objects the repository already held under the same id with different '
                                                 'unhashed content. Its stored copy stands.'),
        'unresolved_references': integer(0),
        'reason': enum('too_large', 'rejected', 'unauthorized', 'unavailable', 'oversized_field', 'unsafe_text', 'unsafe_command',
                       'unrequested_claim', 'unknown_entity', 'screen_too_large', 'prompt_injection', description=
            'Why no deposit was made. For failed: too_large, rejected, unauthorized or unavailable. For withheld_unsafe, the '
            'screen check that withheld the account: oversized_field (a field over its size limit), unsafe_text (a link, code, '
            'markup, hidden characters or text addressed to a model in the account), unsafe_command (a recorded command that '
            'is not a plain fetch or conversion writing under output/), unrequested_claim (claims beyond those asked about), '
            'unknown_entity (a triple term not in the request, an allowed namespace or a cited source), screen_too_large '
            '(more text than the screen checks) or prompt_injection (the classifier flagged text the run read or wrote).'),
        'http_status': integer(100, 599),
        'detail': string(maxLength=500, description="For rejected, the repository's own reason for refusing the document. For "
                         'withheld_unsafe, one plain line saying the account was not stored in the claims repository, why, and '
                         'where (a saved source file and lines, a tool call, or an account field); never the text itself.')}, ['status']))
    b.add('AgentAccountPaper', obj({
        'path': string(description='Where the agent saved the file, relative to output/ (for example sources/PMC3491803.xml).'),
        'url': null(string(description='The URL the bytes were fetched from; for a converted file, the URL of the file it was '
                                       'converted from.')),
        'sha256': digest, 'size_bytes': integer(0, description='Size of the saved file, measured by REVEAL.'),
        'media_type': string(description='Media type REVEAL read from the bytes.'),
        'command': string(description='The command the agent recorded on the Activity that saved the file.'),
        'observed_command': string(description='The successful Bash call REVEAL saw run, which contains the recorded command '
                                               'and names the file.'),
        'derived_from': null(string(description='path of the saved file this one was converted from, or null for a fetched file.')),
        'evidence_items': integer(0, description='EvidenceItems that quote this file.'),
        'file_id': dapper_id('File')}, description=
        'One file the agent saved under output/sources/ and recorded in the account. The account names it by checksum and URL '
        'and does not carry it; the owner downloads the exact bytes a ValueLocator addresses from '
        '/v1/jobs/{job_id}/account/sources/{sha256}.'))
    term = lambda what: string(minLength=3, maxLength=MAX_TERM_CHARS, pattern='^' + TERM.pattern + '$', description=what)
    b.add('AgentAccountClaim', obj({
        'statement': string(minLength=1, maxLength=MAX_CLAIM_CHARS, description='The claim in words.'),
        'subject_entity': term('Subject of the structured form, as a CURIE or absolute URI, for example HGNC.SYMBOL:LEPR.'),
        'relation': term('Predicate, for example biolink:genetically_associated_with.'),
        'object_entity': term('Object, for example KPN.TRAIT:0000505 or MONDO:0005265.')},
        ['statement'], dependentRequired={'subject_entity': ['relation', 'object_entity'], 'relation': ['subject_entity', 'object_entity'],
                                          'object_entity': ['subject_entity', 'relation']}, description=
        'A requested claim with the structured form of its Proposition. subject_entity, relation and object_entity go together: '
        'give all three or none. A CURIE prefix must be one the pinned DAPPER release declares (MONDO, HGNC, KPN.TRAIT, obo, CL, '
        '...) or HGNC.SYMBOL or biolink; anything else must be an absolute URI. The three are stored on the Proposition as given, '
        'so the account can be found by them in the claims repository. A claim sent as a plain statement has no structured form '
        'unless the agent can supply one from what it was given and read.'))
    b.add('AgentAccountRequestedClaim', obj({
        'statement': string(), 'assessed': {'type': 'boolean', 'description': 'False when the agent made no Claim on it.'},
        'claim_ids': array(dapper_id('Claim'))}))
    b.add('AgentAccount', obj({
        'account_id': dapper_id('ScientificAccount'),
        'dapper_version': string(description='DAPPER version the returned and deposited document is minted with.'),
        'document_sha256': digest, 'document_bytes': integer(0),
        'objects': integer(0), 'edges': integer(0), 'claims': integer(0),
        'value_locators': integer(0, description='Quoted passages located by exact byte span in a saved source file.'),
        'lint_warnings': integer(0),
        'requested_claims': array(ref('AgentAccountRequestedClaim'), description='What became of each claim in the request.'),
        'papers': array(ref('AgentAccountPaper'), description='Every source file the account records, fetched or converted.'),
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
        'structured_output': {'type': 'object', 'additionalProperties': True, 'description':
            f'Present only when the request sent output_schema: the object the agent returned, checked against that schema. At '
            f'most {MAX_STRUCTURED_OUTPUT_BYTES:,} bytes; a larger one fails the job.'},
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
    operation['requestBody']['content']['application/json']['examples']['agent'] = {'summary': 'General agent', 'value': request}
    account = {'kind': 'agent', 'deliverable': 'scientific_account',
        'prompt': 'Build the account from open-access papers. Prefer primary studies in mice and humans; say what was not read.',
        'question': 'Does loss of leptin receptor signalling in hepatocytes cause hepatic insulin resistance?',
        'claims': ['Hepatocyte-specific deletion of LEPR reduces insulin-stimulated AKT phosphorylation in liver.',
                   {'statement': 'LEPR is genetically associated with type 2 diabetes.', 'subject_entity': 'HGNC.SYMBOL:LEPR',
                    'relation': 'biolink:genetically_associated_with', 'object_entity': 'MONDO:0005148'}],
        'limits': {'max_budget_usd': 5}}
    operation['requestBody']['content']['application/json']['examples']['agent_account'] = {
        'summary': 'General agent authoring a DAPPER ScientificAccount from literature', 'value': account}
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
        'papers': [{'path': 'sources/PMC0000000.xml', 'url': 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC0000000/fullTextXML',
                    'sha256': text_sha, 'size_bytes': 48211, 'media_type': 'application/xml',
                    'command': 'curl -sSL -o output/sources/PMC0000000.xml https://www.ebi.ac.uk/europepmc/webservices/rest/PMC0000000/fullTextXML',
                    'observed_command': 'mkdir -p output/sources && curl -sSL -o output/sources/PMC0000000.xml '
                                        'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC0000000/fullTextXML',
                    'derived_from': None, 'evidence_items': 1, 'file_id': 'dapper:File.' + 'F' * 32}],
        'deposit': {'status': 'deposited', 'deposit_id': 'dep_' + '0' * 32, 'objects': 11, 'stored_differs': 0, 'unresolved_references': 0}}
    account_complete = deepcopy(complete)
    account_complete['result'].update(files=[{'path': 'account-1.json', 'size_bytes': 5210, 'sha256': '0' * 63 + '4'}], account=account_block)
    getter['responses']['200']['content']['application/json']['examples']['agent_account_complete'] = {
        'summary': 'Agent complete with a ScientificAccount (synthetic)', 'value': account_complete}
    job_parameter = b.parameter('job_id', 'path', string(format='uuid'), JOB_ID, True)
    b.operation('/v1/jobs/{job_id}/account', 'get', 'getJobAccount', 'Jobs', 'Read the ScientificAccount an agent job returned',
        'Owner-only. The account of a deliverable scientific_account job, in the form REVEAL deposits it: one DAPPER document '
        'with every object the account needs, quoted passages as ValueLocators over the saved source files, named by checksum '
        'and URL, and the commands that saved them. Returns 404 '
        'until the job has succeeded, and for any other job. The document names the requester as the Person the account is '
        'attributed to only when the job asked for that with attribute_requester.',
        'AgentAccountDocument', {'account': {'job_id': JOB_ID, 'account': account_block, 'document': {
            'scientific_accounts': [{'id': account_id, 'name': 'LEPR and hepatic insulin signalling'}]}}},
        parameters=[job_parameter], errors=('401', '404', '429'))
    text = b.operation('/v1/jobs/{job_id}/account/sources/{sha256}', 'get', 'downloadJobAccountSource', 'Jobs',
        'Download a source file an agent job account records',
        'Owner-only. One file the agent saved under output/sources/ (sha256 in the account block papers), exactly as saved: '
        'the bytes a ValueLocator of the account addresses, or the file a quoted text was converted from. REVEAL keeps it '
        'for the owner and does not publish it. Attachment disposition, private no-store caching and nosniff, like a '
        'captured source.',
        string(format='binary'), {'source_file': 'Hepatocyte-specific deletion of Lepr reduced insulin-stimulated AKT phosphorylation.'},
        parameters=[job_parameter, b.parameter('sha256', 'path', string(pattern='^[a-f0-9]{64}$'), text_sha, True)],
        errors=('401', '404', '503'))
    text['responses']['200']['content'] = {'*/*': text['responses']['200']['content']['application/json']}
    text['responses']['307'] = {'description': 'After authorization, redirect to the exact stored object. The private URL expires after 60 seconds.',
        'headers': {'Location': {'description': 'Short-lived download URL. Never cache or persist as an identifier.',
                                 'schema': {'type': 'string', 'format': 'uri'}}}}
    for exchange in b.EXCHANGES:
        if exchange['operation_id'] == 'downloadJobAccountSource':
            exchange['request']['headers']['Accept'] = '*/*'
            exchange['responses']['200']['content_type'] = '*/*'
    for parameter in b.PATHS['/v1/jobs']['get'].get('parameters', []):
        if parameter['name'] == 'kind': parameter['schema']['enum'].append('agent')
