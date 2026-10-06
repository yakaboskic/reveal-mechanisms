"""Browser lifecycle contract for stateless MCP/local research (no scientific fixture claims)."""
from copy import deepcopy


def extend(b, f, e):
    obj,ref,array,string,null,enum=b.obj,b.ref,b.array,b.string,b.nullable,b.enum
    uuid=string(format='uuid'); timestamp=string(format='date-time'); digest=string(pattern='^[a-f0-9]{64}$')
    generic={'type':'object','additionalProperties':True}
    instructions=obj({'codex':string(),'claude_code':string()})
    error=null(obj({'code':string(),'detail':string()}))
    b.add('LocalWorkCreate',obj({'draft_id':uuid,'draft_version':{'type':'integer','minimum':1}}))
    b.add('LocalWorkGrant',obj({'grant_id':uuid,'expires_at':timestamp,'revoked_at':null(timestamp),'created_at':timestamp},['grant_id','expires_at']))
    b.add('LocalResearchOperation',obj({'id':uuid,'kind':enum('prepare','query','import','export','validate','submit'),
        'state':enum('received','running','succeeded','accepted','rejected','failed','cancelled'),'local_work_id':uuid,
        'created_at':timestamp,'completed_at':timestamp,'result':generic,'error':error,'report':generic,
        'account_ids':array(b.did('ScientificAccount')),'reused_account_ids':array(b.did('ScientificAccount')),
        'validation_only':{'type':'boolean'}},['id','kind','state','created_at']))
    b.add('LocalWork',obj({'id':uuid,'research_request_id':uuid,'request':ref('ResearchRequest'),
        'state':enum('preparing','ready','preparation_failed','closed'),'created_at':timestamp,'last_activity':timestamp,
        'last_action':string(),'expires_at':timestamp,'reference_generation_id':digest,
        'package_id':null(string()),'package_sha256':null(digest),'last_error':error,'closed_at':timestamp,
        'preparation_operation_id':uuid,'submissions':array(ref('LocalResearchOperation')),'grants':array(ref('LocalWorkGrant')),
        'mcp_url':string(format='uri'),'prompt':string(),'instructions':instructions,'local_work_id':uuid},
        ['id','research_request_id','request','state','created_at','last_activity','expires_at','reference_generation_id',
         'package_id','package_sha256','submissions','grants','mcp_url','prompt','instructions']),
        )
    b.add('LocalWorkList',obj({'items':array(ref('LocalWork')),'page':ref('Page')},['items']))
    b.add('LocalWorkGrantIssued',obj({'grant_id':uuid,'expires_at':timestamp,'local_work_id':uuid,
        'token':string(pattern='^rvlm_',writeOnly=False),'mcp_url':string(format='uri'),'prompt':string(),'instructions':instructions},
        description='Legacy manual connection only; new workspaces use anonymous access and OAuth. The raw research credential is shown once, never present in later work reads. Store it outside prompts and committed config. Issuance retry returns GRANT_ALREADY_ISSUED, not the credential.'))
    b.add('ResearchArtifactDescriptor',obj({'id':string(),'filename':string(),'sha256':digest,
        'size_bytes':{'type':'integer','minimum':0},'purpose':string()}))
    b.add('EvidenceClosureArtifact',obj({'id':string(),'artifact_id':string(),'filename':string(),'path':string(),
        'sha256':digest,'size_bytes':{'type':'integer','minimum':0},'purpose':string(),'format':string(),
        'dapper_file_id':string()},['id','artifact_id','filename','path','sha256','size_bytes','purpose','format']))
    b.add('EvidenceClosureManifest',obj({'format':enum('reveal.context-manifest/2'),
        'seed_sha256':digest,'package_sha256':digest,'context_sha256':digest,
        'files':array(ref('EvidenceClosureArtifact')),'package':ref('EvidenceClosureArtifact')}))
    b.add('EvidenceContextExport',obj({'format':enum('reveal.validation-context-export/2'),
        'seed_sha256':digest,'package_sha256':digest,'context_sha256':digest,
        'receipt_ids':array(string()),'import_ids':array(string()),'reuse_receipt_ids':array(string()),
        'artifacts':array(ref('EvidenceClosureArtifact')),'package_artifact':ref('EvidenceClosureArtifact'),
        'manifest':ref('EvidenceClosureManifest'),'phase_timings_ms':{'type':'object','additionalProperties':{'type':'number','minimum':0}}},
        description='Successful durable export operation result. export_evidence_context immediately acknowledges with operation_id; poll get_operation. Reuse the same selection/idempotency key after interruption. Transfer only missing checksum-verified artifacts; reconstruct and verify the entire selected closure before authoring/validation. This result contains no inline package and reuses retained original scientific sources without re-querying.'))
    b.add('ResearchSeed',obj({'package_version':enum('reveal.evidence-package/0.2-draft'),
        'seed_version':enum('reveal.research-seed/1'),'retrieval_mode':enum('progressive'),'research_request_id':uuid,
        'reference_generation_id':digest},additionalProperties=True,
        description='Small immutable context, hash-pinned authoring kit and capability inventory. Factor expansions and phenotype observations are not initially captured. Source artifacts download separately. Existing package envelope version is retained for compatible DAPPER validation; retrieval mode is explicitly seed-only.'))
    ready=obj({'package_id':string(),'package_sha256':digest,'package':ref('ResearchSeed'),'manifest':generic,
        'artifacts':array(ref('ResearchArtifactDescriptor')),'authoring_instructions':string()})
    b.add('LocalWorkPackage',{'oneOf':[ready,obj({'state':enum('preparing'),'operation_id':uuid})]})
    b.add('LocalWorkspaceSetup',obj({'client':enum('codex','claude_code')}))
    b.add('ResearchSetupExchange',obj({'ticket':string(pattern='^rvls_[A-Za-z0-9_-]{43}$',writeOnly=True),
        'token_sha256':digest},description='Redeem the short-lived setup ticket using the SHA-256 of a locally generated rvlm_ bearer. Persist the bearer in the OS credential store before this request. Same-ticket/same-hash retries recover the original grant metadata without another grant.'))
    b.add('ResearchSetupConnection',obj({'grant_id':uuid,'expires_at':timestamp,'local_work_id':uuid,
        'package_sha256':digest,'mcp_url':string(format='uri')},description='Scoped connection metadata only. The server never receives or returns the launcher-generated bearer secret.'))
    work_id='77777777-7777-4777-8777-777777777777'; grant_id='99999999-9999-4999-8999-999999999999'
    prompt='Use the downloaded frozen seed and anonymous public MCP tools. Sign in and explicitly approve access before private reads, uploads, validation or submission.'
    instruction={'codex':'Run python3 start.py codex in the downloaded folder. Add --login only when ready to connect.',
                 'claude_code':'Run python3 start.py claude in the downloaded folder. Add --login only when ready to connect.'}
    work={'id':work_id,'research_request_id':e['research_request']['id'],'request':deepcopy(e['research_request']),
        'state':'preparing','created_at':b.NOW,'last_activity':b.NOW,'last_action':'created','expires_at':'2026-12-01T00:00:00Z',
        'reference_generation_id':'a'*64,'package_id':None,'package_sha256':None,'last_error':None,
        'preparation_operation_id':'88888888-8888-4888-8888-888888888888','submissions':[],'grants':[],
        'mcp_url':'https://api.reveal.example.org/mcp','prompt':prompt,'instructions':instruction,'local_work_id':work_id}
    ready_work={**work,'state':'ready','package_id':'b'*64,'package_sha256':'c'*64}
    common='Owner-scoped browser operation using the existing workspace API bearer. Research credentials are separate, bound to one work/request and do not authorize other work. '
    path='/v1/local-work'
    params=[b.parameter('work_id','path',uuid,work_id,True)]
    create=b.operation(path,'post','createLocalWork','Local research','Prepare local agent research',
        common+'Freeze the exact saved draft revision and retain its reference generation. Prepare a small seed as a durable bounded operation; do not enqueue a paid hosted job.',
        'LocalWork',{'preparing':work},request_schema='LocalWorkCreate',request_examples={'saved_draft':{'draft_id':b.DRAFT_ID,'draft_version':2}},
        status=201,idempotent=True,errors=('400','401','404','409','422','429','503'))
    create['responses']['201']['headers'].pop('Location',None)
    b.operation(path,'get','listLocalWork','Local research','List owned local research',common+'Returns retained local runs, including closed work. Hosted research contexts are excluded.',
        'LocalWorkList',{'owned':{'items':[ready_work]},'empty':{'items':[]}},errors=('401','429'))
    b.operation(path+'/{work_id}','get','getLocalWork','Local research','Read local research and results',
        common+'Read readiness, original frozen request, visible grants and validation/submission status. Reconnect later using the same work ID. Closing a browser does not close work.',
        'LocalWork',{'ready':ready_work,'closed':{**ready_work,'state':'closed','closed_at':b.NOW}},parameters=params,errors=('401','404','429'))
    b.operation(path+'/{work_id}/package','get','getLocalWorkPackage','Local research','Read the seed and artifact manifest',
        common+'Returns preparation status or the immutable seed and artifact descriptors. Download large artifacts separately with authorized transfers; a metadata path is not a download URL.',
        'LocalWorkPackage',{'preparing':{'state':'preparing','operation_id':work['preparation_operation_id']},'ready':{
            'package_id':'b'*64,'package_sha256':'c'*64,'package':{'package_version':'reveal.evidence-package/0.2-draft','seed_version':'reveal.research-seed/1',
            'retrieval_mode':'progressive','research_request_id':work['research_request_id'],'reference_generation_id':'a'*64},
            'manifest':{'format':'illustrative contract metadata; hashes are placeholders'},'artifacts':[],'authoring_instructions':prompt}},
        parameters=params,errors=('401','404','409','429'))
    b.operation(path+'/{work_id}/close','post','closeLocalWork','Local research','Close local research',
        common+'Prevents new retrieval/upload/submission operations; already received work may finish. Release the generation pin only after all operations settle. Does not delete accepted accounts.',
        'LocalWork',{'closed':{**ready_work,'state':'closed','closed_at':b.NOW}},parameters=params,request_schema=obj({}),request_examples={'close':{}},
        idempotent=True,errors=('400','401','404','409','429'))
    grant=b.operation(path+'/{work_id}/grants','post','issueLocalResearchGrant','Local research','Connect a local agent',
        common+'Deprecated manual bearer issuance requires a currently registered browser/API principal; anonymous issuance returns 403 SIGN_IN_REQUIRED. Prefer browser-consented OAuth for new local connections. Old anonymous-issued or unmarked manual grants return 403 REGISTERED_CONSENT_REQUIRED on protected MCP access even after principal promotion; reconnect with fresh registered consent. Issue a scoped expiring credential once. Repeated issuance with the same key returns conflict rather than exposing an old secret. The example credential is fictional.',
        'LocalWorkGrantIssued',{'connection':{'grant_id':grant_id,'expires_at':'2026-11-01T00:00:00Z','local_work_id':work_id,
            'token':'rvlm_FICTIONAL_EXAMPLE_DO_NOT_USE','mcp_url':work['mcp_url'],'prompt':prompt,'instructions':instruction}},
        parameters=params,request_schema=obj({}),request_examples={'connect':{}},status=201,idempotent=True,errors=('400','401','403','404','409','429'))
    grant['deprecated']=True
    grant['responses']['403']['content']=b.content('Problem',{'sign_in_required':b.problem(403,'SIGN_IN_REQUIRED','Sign in before issuing a local research credential.')},'application/problem+json')
    grant['responses']['201']['headers'].pop('Location',None)
    grant['responses']['201']['headers']['Cache-Control']={'schema':{'type':'string','const':'no-store'}}
    revoke=b.operation(path+'/{work_id}/grants/{grant_id}','delete','revokeLocalResearchGrant','Local research','Revoke an agent connection',
        common+'Revocation blocks future calls and pending-operation commits that require this credential. Retained scientific records remain accessible to their owner.',
        obj({}),{'revoked':{}},parameters=params+[b.parameter('grant_id','path',uuid,grant_id,True)],status=204,errors=('401','404','429'))
    revoke['responses']['204'].pop('content')
    setup=b.operation(path+'/{work_id}/setup-kit','post','downloadLocalWorkspace','Local research','Download an agent-ready workspace',
        common+'Return a credential-free reveal.local-setup/2 ZIP of exact retained seed/source and authoring bytes, both project-local stdio agent configurations, setup-manifest.json and a Python launcher. No setup ticket, setup.json secret, bearer, grant, hosted job or model call is created by download. The browser owner may be anonymous or registered; the downloaded private research inputs still require careful sharing. Default launch uses public MCP anonymously and does not require the backend to be online. Explicit --login uses browser device consent; --logout disables authenticated access and revokes only this installation. Requires ready, unexpired owned local work; scientific inputs stay unchanged on repeat download.',
        string(format='binary'),{},parameters=params,request_schema='LocalWorkspaceSetup',
        request_examples={'codex':{'client':'codex'},'claude_code':{'client':'claude_code'}},errors=('401','404','409','422','429','503'))
    setup['responses']['200']['content']={'application/zip':{'schema':string(format='binary')}}
    setup['responses']['200']['headers'].update({'Cache-Control':{'schema':string(const='private, no-store')},
        'Content-Disposition':{'schema':string(),'example':'attachment; filename="reveal-'+work_id+'.zip"'}})
    exchange=b.operation('/v1/research-setup/exchange','post','exchangeResearchSetup','Local research','Redeem a local workspace setup ticket',
        'Deprecated compatibility endpoint only for already-issued reveal.local-setup/1 tickets whose persisted issued_principal_kind is registered. Anonymous-issued or unmarked tickets return 403 SIGN_IN_REQUIRED even after identity promotion; a copied ticket cannot establish fresh consent. Download a credential-free v2 workspace and authorize through OAuth instead. New v2 downloads issue no ticket and use OAuth instead. The body ticket authorizes exactly one local connection for its original owner, work and package. No browser session is required. Generate and securely retain the rvlm_ bearer locally, then send only its hash. Repeating the same ticket and hash returns the same metadata; another hash conflicts. Closed, revoked or transferred authority fails; unconsumed expired tickets fail. Same-hash metadata replay after ticket expiry is allowed only while its grant is active and the work remains authorized. An optional Authorization: Bearer <current-research-token> rotates only that same-work local connection, preserving other installations. Lost-acknowledgement replay remains valid after the old connection was rotated. Never log the ticket or Authorization value.',
        'ResearchSetupConnection',{'connected':{'grant_id':grant_id,'expires_at':'2026-11-01T00:00:00Z',
            'local_work_id':work_id,'package_sha256':'c'*64,'mcp_url':work['mcp_url']}},
        request_schema='ResearchSetupExchange',request_examples={'redeem':{'ticket':'rvls_'+'F'*43,'token_sha256':'d'*64}},
        public=True,errors=('401','403','404','409','422','429','503'))
    exchange['deprecated']=True
    exchange['responses']['403']['content']=b.content('Problem',{'sign_in_required':b.problem(403,'SIGN_IN_REQUIRED','This old setup ticket has no retained proof of registered issuance. Download a v2 workspace and authorize through OAuth.')},'application/problem+json')
    exchange['security']=[]
    exchange['responses']['200']['headers']['Cache-Control']={'schema':string(const='private, no-store')}
    exchange['responses']['410']={'description':'Legacy setup ticket expired or was revoked. Download a credential-free v2 workspace and use explicit OAuth sign-in.',
        'content':b.content('Problem',{'setup_expired':b.problem(410,'SETUP_TICKET_EXPIRED','Download a v2 workspace and use OAuth sign-in.')},'application/problem+json')}
    for op in (setup,exchange):
        op['responses']['413']={'description':'Workspace or request exceeds its bounded size.',
            'content':b.content('Problem',{'setup_too_large':b.problem(413,'SETUP_TOO_LARGE','The local workspace or request is too large.')},'application/problem+json')}
    revoke_setup=b.operation(path+'/{work_id}/setup-tickets','delete','revokeLocalSetupTickets','Local research','Revoke unconsumed local setup tickets',
        common+'Invalidates every unconsumed setup download for this work. Existing connections and scientific results stay available; revoke a grant separately to disconnect an installed agent.',
        obj({}),{'revoked':{}},parameters=params,status=204,errors=('401','404','429'))
    revoke_setup['deprecated']=True
    revoke_setup['description']='Legacy compatibility: '+revoke_setup['description']+' New credential-free v2 downloads create no setup tickets.'
    revoke_setup['responses']['204'].pop('content')
    for exchange in b.EXCHANGES:
        if exchange['operation_id'] in ('revokeLocalResearchGrant','revokeLocalSetupTickets'):
            exchange['responses']['204']={'content_type':None,'examples':{}}
        if exchange['operation_id']=='downloadLocalWorkspace':
            exchange['responses']['200']={'content_type':'application/zip','examples':{}}
            exchange['request']['headers']['Accept']='application/zip'
            exchange['curl']=exchange['curl'].replace('Accept: application/json','Accept: application/zip')+' --output workspace.zip'
            sample=next(s for s in setup['x-codeSamples'] if s['label']==exchange['case'])
            sample['source']=exchange['curl']
    for operation in (create,grant,b.PATHS[path+'/{work_id}/close']['post']):
        parameter=next(p for p in operation['parameters'] if p['name']=='Idempotency-Key')
        parameter['schema'].update(minLength=1,maxLength=200)
    b.SCHEMAS['ParagraphState']['description']='Local accepted accounts begin with status not_requested and null job_id/paragraph_id. The owner may explicitly request a hosted research statement later.'

    _authentication_and_public(b, work_id)


def _authentication_and_public(b, work_id):
    """Standards endpoints are direct-backend routes, not browser provider-token proxies."""
    from urllib.parse import urlencode
    import shlex
    obj, ref, array, string, null, enum = b.obj, b.ref, b.array, b.string, b.nullable, b.enum
    uuid=string(format='uuid'); digest=string(pattern='^[a-f0-9]{64}$'); uri=string(format='uri')
    timestamp=string(format='date-time'); generic={'type':'object','additionalProperties':True}
    scopes=array(enum('research:read','research:write'),minItems=1,uniqueItems=True)
    device='urn:ietf:params:oauth:grant-type:device_code'; resource=b.BASE+'/mcp'
    req='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'; client='rvlc_FICTIONAL_PUBLIC_CLIENT'
    registered='Requires a registered Reveal browser principal from the existing Google/ORCID gateway. An anonymous browser must sign in, and may explicitly claim its existing anonymous workspace through the session gateway. Codes, URLs and work IDs never establish ownership. Provider tokens and cookies are not forwarded. '
    b.add('LocalWorkspaceManifest',obj({'schema_version':{'const':1},'setup_version':enum('reveal.local-setup/2'),
        'client':enum('codex','claude_code'),'local_work_id':uuid,'research_request_id':uuid,'reference_generation_id':digest,
        'package_sha256':digest,'mcp_url':uri,'device_authorization_url':uri,'token_url':uri,'revocation_url':uri,'return_url':uri,
        'files':array(obj({'path':string(),'sha256':digest,'size_bytes':{'type':'integer','minimum':0}}))},
        description='Credential-free setup-manifest.json inside reveal-<work_id>/. Source paths are preserved under input/; pinned authoring files are materialized at their declared paths. Includes both stdio client configurations and start.py; no access token, refresh token, device code or setup ticket.'))
    b.PATHS['/v1/local-work/{work_id}/setup-kit']['post']['x-workspace-manifest-schema']=ref('LocalWorkspaceManifest')
    b.add('ResearchOAuthError',obj({'error':string(),'error_description':string()}))
    b.add('ResearchConsentFailure',{'oneOf':[obj({'code':string(),'detail':string()},additionalProperties=True),ref('ResearchOAuthError')]})
    registration={'client_name':string(maxLength=120),'redirect_uris':array(uri,maxItems=10,uniqueItems=True),
        'grant_types':array(enum('authorization_code','refresh_token',device),minItems=1,uniqueItems=True),
        'response_types':array(enum('code')),'token_endpoint_auth_method':enum('none'),'scope':string()}
    b.add('ResearchOAuthRegistration',obj(registration,[],description='Public-client registration. Default grant_types is authorization_code, which requires at least one exact HTTPS or HTTP loopback callback (localhost,127.0.0.1,::1). Register refresh_token explicitly for refresh support. No custom schemes, callback fragments, credentials, reserved OAuth query parameters, implicit flow or client secret. A client name is self-reported metadata, not verified branding.'))
    b.add('ResearchOAuthClient',obj({**registration,'client_id':string(),'client_id_issued_at':{'type':'integer','minimum':0}}))
    b.add('ResearchOAuthDeviceRequest',obj({'client_id':string(),'resource':uri,'scope':string(),'local_work_id':uuid},['client_id','resource']))
    b.add('ResearchOAuthDeviceResponse',obj({'device_code':string(writeOnly=False),'user_code':string(),
        'verification_uri':uri,'verification_uri_complete':uri,'expires_in':{'type':'integer','const':600},'interval':{'type':'integer','const':5}}))
    common={'client_id':string(),'resource':uri}
    b.add('ResearchOAuthTokenRequest',{'oneOf':[
        obj({**common,'grant_type':enum('authorization_code'),'code':string(writeOnly=True),'redirect_uri':uri,
            'code_verifier':string(minLength=43,maxLength=128,writeOnly=True)}),
        obj({**common,'grant_type':enum(device),'device_code':string(writeOnly=True)}),
        obj({**common,'grant_type':enum('refresh_token'),'refresh_token':string(writeOnly=True),'scope':string()},
            ['client_id','resource','grant_type','refresh_token'])]})
    b.add('ResearchOAuthTokens',obj({'access_token':string(),'refresh_token':string(),'token_type':enum('Bearer'),
        'expires_in':{'type':'integer','minimum':0,'maximum':900},'scope':string(),'resource':uri,'local_work_id':uuid,
        'package_sha256':digest,'mcp_url':uri,'grant_id':uuid},
        ['access_token','token_type','expires_in','scope','resource','local_work_id','package_sha256','mcp_url','grant_id'],
        description='Opaque Reveal credentials, never Google/ORCID credentials. Access lasts at most 900 seconds; family/rotating refresh lasts at most 30 days, each bounded by work lifetime. Refresh reuse revokes the family. Persist securely and never include in prompts, source files, logs or URLs.'))
    b.add('ResearchOAuthRevocation',obj({'client_id':string(),'token':string(writeOnly=True),'token_type_hint':enum('access_token','refresh_token')},['client_id','token']))
    b.add('ResearchOAuthConsent',obj({'request_id':uuid,'kind':enum('authorization_code','device'),'client_id':string(),
        'client_name':string(),'scopes':scopes,'resource':uri,'requested_local_work_id':null(uuid),'redirect_uri':null(uri),
        'expires_at':timestamp,'status':enum('pending','approved','denied'),'requires_registered':{'const':True}}))
    b.add('ResearchOAuthDecision',obj({'request_id':uuid,'user_code':string(),'local_work_id':uuid,'approve':{'type':'boolean'}},
        ['approve'],oneOf=[{'required':['request_id'],'not':{'required':['user_code']}},{'required':['user_code'],'not':{'required':['request_id']}}],
        **{'if':{'properties':{'approve':{'const':True}}},'then':{'required':['local_work_id']}}))
    b.add('ResearchOAuthDecisionResult',{'oneOf':[obj({'redirect_url':uri}),obj({'approved':{'type':'boolean'},'local_work_id':uuid},['approved'])]})
    metadata={'issuer':b.BASE,'authorization_endpoint':b.BASE+'/oauth/authorize','token_endpoint':b.BASE+'/oauth/token',
        'registration_endpoint':b.BASE+'/oauth/register','device_authorization_endpoint':b.BASE+'/oauth/device_authorization',
        'revocation_endpoint':b.BASE+'/oauth/revoke','response_types_supported':['code'],
        'grant_types_supported':['authorization_code','refresh_token',device],'token_endpoint_auth_methods_supported':['none'],
        'revocation_endpoint_auth_methods_supported':['none'],'code_challenge_methods_supported':['S256'],'scopes_supported':['research:read','research:write']}
    b.add('ResearchAuthorizationMetadata',obj({key:uri if isinstance(value,str) else array(string()) for key,value in metadata.items()}))
    protected={'resource':resource,'authorization_servers':[b.BASE],'scopes_supported':['research:read','research:write'],
        'bearer_methods_supported':['header'],'resource_name':'Reveal research'}
    b.add('ResearchProtectedResourceMetadata',obj({'resource':uri,'authorization_servers':array(uri),'scopes_supported':scopes,
        'bearer_methods_supported':array(enum('header')),'resource_name':string()}))

    def protocol(path, method, name, summary, description, response_schema, response_examples, *, form=False, status=200, **kwargs):
        op=b.operation(path,method,name,'Local research',summary,description,response_schema,response_examples,
            public=True,status=status,errors=(),**kwargs)
        op['security']=[]
        if status in (201,202): op['responses'][str(status)]['headers'].pop('Location',None)
        if not path.startswith('/.well-known/'):
            op['responses'][str(status)]['headers'].update({'Cache-Control':{'schema':string(const='private, no-store')},
                'Pragma':{'schema':string(const='no-cache')},'Referrer-Policy':{'schema':string(const='no-referrer')}})
            for code,label in [('400','invalid_request'),('401','invalid_client'),('413','invalid_request'),('429','temporarily_unavailable')]:
                op['responses'][code]={'description':'OAuth protocol error; never a provider-token response.',
                    'content':b.content('ResearchOAuthError',{label:{'error':label,'error_description':'Illustrative OAuth error; no live authentication occurred.'}})}
        if form:
            op['requestBody']['content']['application/x-www-form-urlencoded']=op['requestBody']['content'].pop('application/json')
            for ex in b.EXCHANGES:
                if ex['operation_id']!=name: continue
                ex['request']['headers']['Content-Type']='application/x-www-form-urlencoded'
                pieces=['curl','-X',method.upper(),shlex.quote(ex['request']['url'])]
                for key,value in ex['request']['headers'].items(): pieces+=['-H',shlex.quote(key+': '+value)]
                pieces+=['--data-raw',shlex.quote(urlencode(ex['request']['body']))]
                ex['curl']=' '.join(pieces)
                next(sample for sample in op['x-codeSamples'] if sample['label']==ex['case'])['source']=ex['curl']
        return op

    protocol('/.well-known/oauth-authorization-server','get','getResearchAuthorizationMetadata','Discover Reveal OAuth endpoints',
        'Public RFC8414 metadata for the canonical backend origin/path prefix. Direct backend route; the browser v1 gateway does not proxy this path.', 'ResearchAuthorizationMetadata',{'metadata':metadata})
    for path,name in [('/.well-known/oauth-protected-resource','getResearchProtectedResourceMetadata'),('/.well-known/oauth-protected-resource/mcp','getResearchMcpProtectedResourceMetadata')]:
        protocol(path,'get',name,'Discover the protected MCP resource','Public metadata. Public MCP reads need no bearer. Protected operations return a Bearer challenge pointing to this metadata; invalid supplied credentials never fall back to anonymous.', 'ResearchProtectedResourceMetadata',{'metadata':protected})
    client_body={'client_name':'Illustrative local client','redirect_uris':['http://127.0.0.1:8765/callback'],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','scope':'research:read research:write'}
    protocol('/oauth/register','post','registerResearchOAuthClient','Register a public MCP OAuth client',
        'Rate-limited dynamic public-client registration. Exact registered callbacks, S256 PKCE and resource binding are mandatory for authorization codes. The fixed reveal-local-launcher client already supports device+refresh; downloads do not register or authorize a client.',
        'ResearchOAuthClient',{'registered':{**client_body,'client_id':client,'client_id_issued_at':1791244800}},request_schema='ResearchOAuthRegistration',request_examples={'public_client':client_body},status=201)
    authorize=protocol('/oauth/authorize','get','authorizeResearchOAuthClient','Start authorization-code browser consent',
        'Validates exact client callback, response_type=code, S256 PKCE and exact MCP resource, then 303 redirects to /research/connect?request_id=... on the canonical Reveal web app. This does not grant access. Authorization requests expire after 600 seconds; approved codes expire after 60 seconds and may be exchanged once.',obj({}),{},status=303,
        parameters=[b.parameter('client_id','query',string(),client,True),b.parameter('redirect_uri','query',uri,client_body['redirect_uris'][0],True),
            b.parameter('response_type','query',enum('code'),'code',True),b.parameter('code_challenge','query',string(pattern='^[A-Za-z0-9_-]{43}$'),'F'*43,True),
            b.parameter('code_challenge_method','query',enum('S256'),'S256',True),b.parameter('resource','query',uri,resource,True),
            b.parameter('scope','query',string(),'research:read research:write'),b.parameter('state','query',string(maxLength=1024),'client-state')])
    authorize['responses']['303'].pop('content');authorize['responses']['303']['headers']['Location']={'schema':uri,'example':'https://reveal.example.org/research/connect?request_id='+req}
    protocol('/oauth/device_authorization','post','startResearchDeviceAuthorization','Start optional launcher sign-in',
        'Explicit sign-in only; default workspace launch does not call this endpoint. Supply exact MCP resource, public client and optional work hint. A hint fixes the permitted selection but confers no ownership. Return a user code/browser link plus a secret device code; keep the device code in secure storage. Poll no faster than interval; slow_down adds five seconds.',
        'ResearchOAuthDeviceResponse',{'pending':{'device_code':'rvld_FICTIONAL_DEVICE_SECRET','user_code':'ABCD-EFGH','verification_uri':'https://reveal.example.org/research/connect',
            'verification_uri_complete':'https://reveal.example.org/research/connect?user_code=ABCD-EFGH','expires_in':600,'interval':5}},form=True,
        request_schema='ResearchOAuthDeviceRequest',request_examples={'launcher':{'client_id':'reveal-local-launcher','resource':resource,'scope':'research:read research:write','local_work_id':work_id}})
    protocol('/oauth/token','post','exchangeResearchOAuthToken','Exchange an approved code or rotate refresh credentials',
        'Public clients use form-encoded authorization_code+S256 verifier, device_code grant, or refresh_token. client_id and exact resource are required. Returns authorization_pending/slow_down while device approval is pending, expired_token/access_denied on expiry/denial. Each code is single-use; refresh rotates on every use, and old-token replay revokes its family. Lost token acknowledgements require safe reauthorization, not blind reuse of a consumed refresh token. Owner, open work, package, scope and family are rechecked. No client secret or provider token is accepted.',
        'ResearchOAuthTokens',{'tokens':{'access_token':'rvlm_FICTIONAL_ACCESS','refresh_token':'rvlrt_FICTIONAL_REFRESH','token_type':'Bearer','expires_in':900,'scope':'research:read research:write',
            'resource':resource,'local_work_id':work_id,'package_sha256':'c'*64,'mcp_url':resource,'grant_id':'99999999-9999-4999-8999-999999999999'}},form=True,
        request_schema='ResearchOAuthTokenRequest',request_examples={'authorization_code':{'grant_type':'authorization_code','client_id':client,'resource':resource,'code':'rvlac_FICTIONAL_CODE','redirect_uri':client_body['redirect_uris'][0],'code_verifier':'F'*43},
            'device':{'grant_type':device,'client_id':'reveal-local-launcher','resource':resource,'device_code':'rvld_FICTIONAL_DEVICE_SECRET'},
            'refresh':{'grant_type':'refresh_token','client_id':client,'resource':resource,'refresh_token':'rvlrt_FICTIONAL_REFRESH'}})
    revoke=protocol('/oauth/revoke','post','revokeResearchOAuthConnection','Revoke this client connection',
        'Revoke the matched access/refresh token family, including its active access grants. An unknown token returns 200 without revealing ownership. Other client connections and local files remain intact. Logout disables local use immediately and retries server revocation if offline.',obj({}),{},form=True,
        request_schema='ResearchOAuthRevocation',request_examples={'refresh':{'client_id':'reveal-local-launcher','token':'rvlrt_FICTIONAL_REFRESH','token_type_hint':'refresh_token'}})
    revoke['responses']['200'].pop('content')
    info={'request_id':req,'kind':'device','client_id':'reveal-local-launcher','client_name':'Reveal local launcher','scopes':['research:read','research:write'],'resource':resource,
        'requested_local_work_id':work_id,'redirect_uri':None,'expires_at':'2026-10-06T12:10:00Z','status':'pending','requires_registered':True}
    get=b.operation('/v1/research-oauth/consent','get','getResearchOAuthConsent','Local research','Read a pending agent connection request',
        registered+'Supply exactly one request_id or user_code. Reading or refreshing never approves access. The UI displays the trusted request, self-reported client name/ID, scopes, resource, callback and work before a decision.',
        'ResearchOAuthConsent',{'device':info},parameters=[b.parameter('request_id','query',uuid,req)],errors=('400','401','403','404','422','429'))
    get['parameters'].append(b.parameter('user_code','query',string(),'ABCD-EFGH'))
    decide=b.operation('/v1/research-oauth/consent','post','decideResearchOAuthConsent','Local research','Approve or decline an agent connection',
        registered+'Explicit consent selects one owned, ready, unexpired local run. A device work hint cannot be switched. Approving a generic PKCE request requires an explicit work selection. Denial needs no work ID. Only navigate the backend-returned exact registered callback carrying code/error,state and issuer; never a query-supplied callback. Device approval returns a status for the launcher to poll. Repeated decisions return 409.',
        'ResearchOAuthDecisionResult',{'device_approved':{'approved':True,'local_work_id':work_id},'device_denied':{'approved':False},'code_redirect':{'redirect_url':client_body['redirect_uris'][0]+'?code=rvlac_FICTIONAL&state=client-state&iss='+b.BASE}},
        request_schema='ResearchOAuthDecision',request_examples={'approve':{'request_id':req,'local_work_id':work_id,'approve':True},'decline':{'request_id':req,'approve':False}},errors=('400','401','403','404','409','422','429'))
    decide['responses']['413']={'description':'Consent request body exceeds16384bytes.'}
    for op in (get,decide):
        for status,response in op['responses'].items():
            if int(status)>=400:
                response['content']=b.content('ResearchConsentFailure',{'failure':{'code':'SIGN_IN_REQUIRED' if status=='403' else 'OAUTH_REQUEST_UNAVAILABLE','detail':'Illustrative browser-consent failure; no live request occurred.'}})
        for response in op['responses'].values(): response.setdefault('headers',{})['Cache-Control']={'schema':string(const='private, no-store')}
    b.add('PublicResearchArtifact',obj({'path':string(),'sha256':digest,'size_bytes':{'type':'integer','minimum':0},'media_type':enum('application/json'),
        'download_url':uri,'authentication':enum('none'),'expires_at':timestamp}))
    b.add('PublicResearchCapture',obj({'format':enum('reveal.public-reference-capture/1'),'capture_id':digest,'expires_at':timestamp,'reference_generation_id':null(digest),
        'operation':string(),'arguments':generic,'reader_version':null(string()),'source_mode':enum('imported_reference','bioindex_small_phenotype','external_kg'),'source':generic,'result':generic,
        'raw_sha256':digest,'metric_definitions':generic,'dapper_context':generic,'source_artifacts':generic,'source_ref':null(generic),'dapper_file_id':null(string()),
        'object_resolution':array(generic),'artifacts':array(ref('PublicResearchArtifact')),'attachment_policy':string()},
        allOf=[{'if':{'properties':{'source_mode':{'const':'external_kg'}}},
            'then':{'properties':{'reference_generation_id':{'type':'null'}}},
            'else':{'properties':{'reference_generation_id':digest}}}],
        description='MCP get_public_capture/data/connected-KG response. Exact server-retained public bytes and context; no researcher prompts, work IDs or private scientific closures. Default retention 7 days; use the explicit expires_at, never assume indefinite replay. Authenticated attach_public_captures re-verifies bytes without a source query. Imported-reference and small-model phenotype captures require the same frozen generation. External-KG captures have null reference_generation_id, retain graph/query/source metadata and require that graph in frozen request.composer.selected_kgs; they do not assert CFDE ancestry or a known upstream release. Already-attached evidence outlives public capture expiry. Public science lookup is current-public snapshots only, with authority rechecked when reusing.'))
    artifact=b.operation('/v1/public-research/captures/{capture_id}/artifacts/{artifact_sha256}','get','downloadPublicResearchArtifact','Local research','Download exact retained public reference bytes',
        'Anonymous bounded download of one artifact belonging to a verified public reference or connected-KG capture. Default capture retention is 7 days (explicit expires_at on MCP descriptors). Expired captures return 410 PUBLIC_CAPTURE_EXPIRED; missing/foreign artifact hashes return 404. This route never serves user uploads, private packages, prompts or scientific reuse closures. Verify length/SHA-256 against the retained descriptor. Authenticated attachment checks the same frozen generation for reference captures or the frozen selected graph for generation-independent external-KG captures, and retains exact bytes beyond public expiry without re-querying the source.',
        string(format='binary'),{},public=True,parameters=[b.parameter('capture_id','path',digest,'a'*64,True),b.parameter('artifact_sha256','path',digest,'b'*64,True)],errors=('404','409','422','429','503'))
    artifact['security']=[]; artifact['responses']['200']['content']={'application/json':{'schema':string(format='binary')}}
    artifact['responses']['200']['headers'].update({'Cache-Control':{'schema':string(const='no-store')},'X-Content-Type-Options':{'schema':enum('nosniff')},'Content-Disposition':{'schema':string()}})
    artifact['responses']['410']={'description':'Explicit public retention expiry; authenticated prior attachments remain retained.',
        'content':b.content('Problem',{'expired':b.problem(410,'PUBLIC_CAPTURE_EXPIRED','The public capture retention period ended.')},'application/problem+json')}
    for ex in b.EXCHANGES:
        if ex['operation_id'] in ('authorizeResearchOAuthClient','revokeResearchOAuthConnection'):
            code='303' if ex['operation_id']=='authorizeResearchOAuthClient' else '200'
            ex['responses'][code]={'content_type':None,'examples':{}}
        if ex['operation_id']=='downloadPublicResearchArtifact':
            ex['responses']['200']={'content_type':'application/json','examples':{}}
            ex['curl']+=' --output public-capture.json'
            artifact['x-codeSamples'][0]['source']=ex['curl']
