"""Current design-contract amendments. Generates documentation only, no server routes."""
from copy import deepcopy
import gzip
import json
import shutil


def fixtures(b, f):
    """Use the same multi-claim CAD fixture as the approved HTML design."""
    p = b.ROOT / 'design/data/cad-account'
    doc = json.loads((p / 'scientific-account.json').read_text())
    paragraph = json.loads((p / 'paragraph.json').read_text())
    presentation = json.loads((p / 'presentation.json').read_text())
    source = presentation['gap_source']
    with gzip.open(b.ROOT / 'data/dismech-gaps/2026-09-24/gap-attachments.jsonl.gz', 'rt') as stream:
        attachments = [json.loads(line) for line in stream if source['id'] in line]
    f.update(account_document=doc, paragraph_document=paragraph, inquiry_document={'knowledge_gaps': doc['knowledge_gaps']},
        question=doc['knowledge_gaps'][0], gap=doc['knowledge_gaps'][0], account=doc['scientific_accounts'][0],
        claim=doc['claims'][0], paragraph=paragraph['paragraphs'][0], gene_set=doc['gene_sets'][0],
        citations=json.loads((p / 'citation-registry.json').read_text()), gap_source=source, attachments=attachments)
    f['gap_citation'] = f['citations'][0]
    captured = json.loads((b.ROOT / 'data/evidence-packages/cad-builder-v1/evidence-package.json').read_text())
    captured_mechanisms = {m['id']: m for m in captured['dapper_context']['mechanisms']}
    f['dismech_records'] = []
    for a in attachments:
        if a['resolution'] != 'resolved' or a['target_kind'] != 'pathophysiology': continue
        m = b.mint({'name': a['target_label'], 'dismech_entry_key': source['document_id'], 'dismech_class': 'Pathophysiology'}, 'Mechanism')
        if a['target_id'] in captured['dismech']['mechanisms']:
            m = captured_mechanisms[captured['dismech']['mechanisms'][a['target_id']]['dapper_id']]
        f['dismech_records'].append({'source': 'dismech', 'source_id': a['target_id'], 'source_revision': b.sha(a),
            'object_class': 'Mechanism', 'object': m, 'disease_label': source['document_name']})
    for r in f['factor_records']:
        catalog_file = r['object']
        m = captured_mechanisms[captured['pigean']['mechanisms'][r['source_id']]['dapper_id']] if r['source_id'] in captured['pigean']['mechanisms'] else b.mint({
            'name': r['source_id'].split(':')[2] + ' mechanism ' + r['source_id'].split(':')[4],
            'description': 'EAGGL mechanism ' + r['source_id'] + '. Source label: ' + r['cfde_anchor']['label'] + '.'}, 'Mechanism')
        r.update(object_class='Mechanism', object=m, catalog_file=catalog_file)
    for name, data in [('scientific-account.dapper.json', doc), ('paragraph.dapper.json', paragraph),
                       ('knowledge-gap.dapper.json', f['inquiry_document']), ('citation-registry.json', f['citations'])]:
        b.write_json(b.OUT / 'examples' / name, data)
    for path in (p / 'sources').iterdir():
        if path.is_file(): shutil.copyfile(path, b.OUT / 'examples' / path.name)
    for name in ['research-statement.md', 'research-statement.tex', 'references.bib', 'rich-text.json']:
        shutil.copyfile(p / name, b.OUT / 'examples' / name)
    legacy = b.OUT / 'examples/question-input.json'
    archive = b.OUT / 'history/v9/examples'
    archive.mkdir(parents=True, exist_ok=True)
    if legacy.exists(): legacy.replace(archive / legacy.name)
    return f


def schemas(b):
    S=b.SCHEMAS; ref=b.ref; obj=b.obj; array=b.array; string=b.string; enum=b.enum; null=b.nullable; did=b.did
    uuid=string(format='uuid'); digest=string(pattern='^[a-f0-9]{64}$'); time=string(format='date-time')
    b.add('SelectedGap', obj({'id':did('KnowledgeGap'),'source_id':string(pattern='^dismech:'),'source_revision':digest},
        description='Exact imported DisMech source observation and DAPPER mapping. Resolve and validate server-side. No client-authored question.'))
    props=S['Composer']['properties'];props.pop('inquiry');props.pop('dismech_context');props['source_gap']=null(ref('SelectedGap'))
    S['Composer']['required']=list(props)
    S['Composer']['description']='Mutable source selection and EAGGL anchors. An empty draft may have null source_gap/zero anchors. Analysis requires an exact source-selected DisMech gap and at least one current-model EAGGL anchor. Linked DisMech context is server-owned, not editable input.'
    for k in ['inquiry','dismech_context']: S['SuggestInput']['properties'].pop(k)
    S['SuggestInput']['properties']['source_gap']=ref('SelectedGap');S['SuggestInput']['required']=list(S['SuggestInput']['properties'])
    # These input projections belonged to the superseded free-text composer.
    for k in ['InquiryInput','QuestionInput','KnowledgeGapInput']: S.pop(k)
    S['EagglFactor']['properties'].update(object_class={'type':'string','const':'Mechanism'},object=ref('DapperMechanism'),catalog_file=ref('DapperFile'))
    S['EagglFactor']['required'].append('catalog_file')
    S['EagglFactor']['description']='An EAGGL factor is a DAPPER Mechanism, with exact native CFDE identity/model and a separate File for captured catalog bytes. Initial retrieval joins existing EAGGL label embeddings through the populated exact-trait/factor-number crosswalk, ignoring label/gene differences. Return the resolved native CFDE source_id and anchor; cfde_anchor.label may carry the friendly EAGGL display label without changing the scientific object. Preserve source hit, embedding and mapping runs in server-owned selection/request provenance.'
    for k in ['Me','AttributionSnapshot']:
        S[k]['properties']['principal_kind']=enum('anonymous','registered');S[k]['required'].append('principal_kind')
    S['Me']['properties']['workspace_expires_at']=null(time);S['Me']['required'].append('workspace_expires_at')
    S['GapSource']['properties']['source']=enum('dismech')
    S['GapSource']['properties']['description_derivation']=enum('source_rationale','prompt_fallback')
    b.add('GapSourceDetail',obj({'source_file':string(),'source_pointer':string(),'payload_sha256':digest,'raw':{'type':'object','additionalProperties':True}},
        description='Lossless versioned DisMech discussion sidecar, including evidence/experiments when present. Not hashable KnowledgeGap fields. Hash covers raw canonical JSON.'))
    b.add('AccountCount',obj({'count':{'type':'integer','minimum':0},'scope':enum('public_exact_gap','owner_exact_gap'),
        'as_of':time,'ranking':enum('curated','recent_account_count','account_count'),'window_days':null({'type':'integer','minimum':1})},
        description='Distinct accessible saved account digests linked to this exact gap digest; no implicit cross-revision rollup or private-count leakage.'))
    S['GapRecord']['properties'].update(source_detail=ref('GapSourceDetail'),scientific_accounts=ref('AccountCount'))
    S['GapRecord']['required']+=['source_detail','scientific_accounts']
    S['ResearchRequest']['properties']['question_id']=did('KnowledgeGap')
    S['ResearchRequest']['properties']['linked_dismech_context']=array(ref('SourceRef'))
    S['ResearchRequest']['required'].append('linked_dismech_context')
    S['ResearchRequest']['description']='Immutable source-selected gap plus server-resolved linked context, selected native anchors and attribution. Later draft/ownership changes do not rewrite original actor or scientific gap.'
    S['AnalysisJobInput']['description']='Freeze the owned saved draft after exact source-gap/revision and current EAGGL alias checks. Queue evidence collection; accounts automatically fan out to paragraph jobs after validation/persistence.'
    S['AnalysisResult']['properties'].update(paragraph_job_ids=array(uuid,maxItems=3),evidence_package_sha256=digest)
    S['AnalysisResult']['required']+=['paragraph_job_ids','evidence_package_sha256']
    stages=S['Job']['properties']['stage']['enum'];stages+=['preparing_evidence','starting_agent']
    b.add('ActivityDetail',obj({'kind':enum('preparation','agent_message','tool_call','tool_result','validation','account_saved'),
        'state':enum('started','completed','failed','cancelled','unavailable'), 'source':enum('worker','harness','validator'),
        'call_id':null(string()),'tool_name':null(string()),'selected_kg':null(enum('biomarkerkg','prokn')),
        'display_arguments':null(string(maxLength=8000)),'output_excerpt':null(string(maxLength=16000)),
        'artifact_sha256':null(digest),'duration_ms':null({'type':'integer','minimum':0}),
        'counts':null(obj({'scope':enum('retrieved','retained'),'nodes':{'type':'integer','minimum':0},'edges':{'type':'integer','minimum':0},
            'truncated':{'type':'boolean'},'snapshot_sha256':digest}))},
        description='Observable activity only; never private reasoning or secrets. Missing metrics stay null. Explicit call states drive UI completion; narrative arrival is not tool completion. Large outputs use artifact references.'))
    S['ActivityDetail']['properties']['message_delta']={'type':'boolean','description':'True only for incremental public text. Adjacent marked agent-message events may be concatenated for display while preserving original event IDs for replay. Absent or false denotes a complete standalone message.'}
    S['JobEvent']['properties']['event_type']['enum']+=['activity']
    S['JobEvent']['properties']['detail']=null(ref('ActivityDetail'));S['JobEvent']['required'].append('detail')
    b.add('ParagraphState',obj({'status':enum('queued','running','succeeded','failed','cancelled','not_requested'),'job_id':null(uuid),'paragraph_id':null(did('Paragraph'))}))
    S['AccountResult']['properties']['research_statement']=ref('ParagraphState');S['AccountResult']['required'].append('research_statement')
    b.add('PublicationState',obj({'visibility':enum('private','public'),'version':{'type':'integer','minimum':0},
        'published_at':null(time),'updated_at':null(time),'can_manage':{'type':'boolean'},'has_unpublished_changes':{'type':'boolean'}},
        description='Mutable application publication control, separate from immutable DAPPER content and citation revisions. A public snapshot contains only the accepted graph and cited statement present when explicitly published. Later accepted statements require Update publication. can_manage is true only for this account owner, including an anonymous workspace. No job telemetry is public.'))
    b.add('PublicationInput',obj({'visibility':enum('private','public'),'expected_version':{'type':'integer','minimum':0}},
        description='Explicit owner publication choice. public creates or updates a frozen public snapshot; private revokes this owner publication. Optimistic version and Idempotency-Key prevent stale or duplicate choices. Existing accounts start private/version0.'))
    S['AccountResult']['properties']['publication']=ref('PublicationState')
    b.add('AnalysisOutcomeResult',obj({'kind':{'type':'string','const':'analysis_outcome'},'outcome_id':uuid,'evidence_package_sha256':digest}))
    for name in ('Job','JobEvent'):
        S[name]['properties']['result']['anyOf'][0]['oneOf'].append(ref('AnalysisOutcomeResult'))
    S['Job']['allOf'][0]['then']['properties']['result']=null({'oneOf':[ref('AnalysisResult'),ref('AnalysisOutcomeResult')]})
    S['Job']['allOf'][2]['else']={'if':{'properties':{'status':{'const':'insufficient_evidence'}}},
        'then':{'properties':{'result':null(ref('AnalysisOutcomeResult'))}},'else':{'properties':{'result':{'type':'null'}}}}
    S['Job']['allOf'].append({'if':{'properties':{'result':{'type':'object','properties':{'kind':{'const':'analysis_outcome'}},'required':['kind']}}},
        'then':{'properties':{'status':{'const':'insufficient_evidence'}}}})
    b.add('OutcomeAnchor',obj({'source_id':string(),'mechanism_id':did('Mechanism'),'name':string(),'trait':null(string()),'origin':string()}))
    b.add('OutcomeEvidenceRef',obj({'source':enum('package','tool_response'),'pointer':string(),'ledger_sequence':null({'type':'integer','minimum':1}),
        'artifact_sha256':digest,'download_url':null(string(format='uri'))}))
    b.add('OutcomeProvenance',obj({'evidence_package_sha256':digest,'outcome_sha256':digest,'runtime_sha256':null(digest),'ledger_sha256':null(digest),
        'execution_mode':enum('box','deterministic'),'source_bindings':array(obj({'source_id':string(),'source_revision':string(),
            'embedding_run_id':null(string()),'mapping_run_id':null(string())})),
        'coverage':{'type':'object','additionalProperties':True},'evidence_refs':array(ref('OutcomeEvidenceRef')),'source_artifacts':array(ref('ArtifactAccess')),
        'graph_queries':array(obj({'sequence':{'type':'integer','minimum':1},'graph':enum('biomarkerkg','prokn'),'status':enum('completed','empty','failed','denied','interrupted'),
            'request_sha256':digest,'response_sha256':digest}))}))
    b.add('AnalysisOutcome',obj({'id':uuid,'outcome':enum('insufficient_evidence'),'summary':string(),'reason':string(),
        'explored_topics':array(string()),'missing_evidence':array(string()),'limitations':array(string()),'next_steps':array(string()),
        'knowledge_gap':ref('DapperKnowledgeGap'),'source_gap':ref('SelectedGap'),'anchors':array(ref('OutcomeAnchor')),'selected_kgs':array(enum('biomarkerkg','prokn')),
        'created_at':time,'attribution':null(ref('AttributionSnapshot')),'scope_note':string(),'record_format':enum('structured','legacy'),
        'provenance':ref('OutcomeProvenance'),'job_id':null(uuid),'publication':ref('PublicationState')},
        description='Immutable application record of one scoped insufficient-evidence investigation, not a ScientificAccount, independently validated scientific claim, or globally established null result. Reasons are captured author reports; coverage and hashes retain their exact scope. Private by default and excluded from scientific-account counts. Public snapshots omit private job identifiers.'))
    b.add('AnalysisOutcomeSummary',obj({key:deepcopy(S['AnalysisOutcome']['properties'][key]) for key in
        ('id','outcome','summary','knowledge_gap','anchors','created_at','attribution','publication')}))
    b.add('AnalysisOutcomeList',obj({'items':array(ref('AnalysisOutcomeSummary')),'page':ref('Page')}))
    b.add('AccountSummary',obj({'account':ref('DapperScientificAccount'),'knowledge_gap':ref('DapperKnowledgeGap'),
        'claim_count':{'type':'integer','minimum':1},'created_at':time,'job_id':null(uuid),'research_statement':ref('ParagraphState')}))
    S['AccountSummary']['properties']['attribution']=null(ref('AttributionSnapshot'))
    S['AccountSummary']['description']='One accessible accepted scientific account, deduplicated by its DAPPER identity. Optional attribution is the immutable original request actor, not the current workspace owner; null denotes unavailable historical attribution. Title and brief synthesis are account.name and account.closing_remarks.'
    b.add('AccountList',obj({'items':array(ref('AccountSummary')),'page':ref('Page')}))
    b.add('ExplorationInput',obj({'source_gap':ref('SelectedGap'),'draft_id':null(uuid)},['source_gap'],
        description='Records a visit under the authenticated principal. Exact source revision is required; optional draft must belong to that principal and selected gap.'))
    b.add('Exploration',obj({'source_gap':ref('SelectedGap'),'knowledge_gap':ref('DapperKnowledgeGap'),
        'last_explored_at':time,'draft_id':null(uuid),'scientific_accounts':ref('AccountCount')}))
    b.add('ExplorationList',obj({'items':array(ref('Exploration')),'page':ref('Page')}))
    b.add('TraversalCoverage',obj({'direction':enum('upstream'),'max_depth':{'type':'integer','minimum':0,'maximum':5},
        'max_nodes':{'type':'integer','minimum':1,'maximum':250},'complete':{'type':'boolean'},
        'missing_ids':array(did()),'next_cursor':null(string())},description='Authorized upstream provenance only; omissions/limits explicit. Do not disclose unauthorized IDs. Immutable large collection membership is paged as a projection, never reminted from a subset.'))
    for k in ['ObjectResult','AccountResult','ClaimResult','ParagraphObjectResult']:
        S[k]['properties']['coverage']=ref('TraversalCoverage');S[k]['required'].append('coverage')
    S['ArtifactAccess']['properties'].update(availability=enum('available','not_available','forbidden'),verification=enum('checksum_verified','source_reported','not_checked'))
    S['ArtifactAccess']['required']+=['availability','verification']
    # Bundle the actual generated LinkML schema; keep its DAPPER definitions distinct
    # because the package has prefix-aware IDs rather than the API's compact-ID projection.
    package=json.loads((b.ROOT/'schema/evidence-package.schema.json').read_text())
    mapping={'#/$defs/'+k:'#/components/schemas/Package'+k for k in package['$defs']}
    for name, definition in package['$defs'].items(): b.add('Package'+name,b.remap_refs(definition,mapping))
    root={k:v for k,v in package.items() if k not in ['$schema','$id','$defs']}
    b.add('EvidencePackage',b.remap_refs(root,mapping))
    b.add('EvidencePackageResult',obj({'job_id':uuid,'package_sha256':digest,'package':ref('EvidencePackage')},
        description='Authorized frozen initial input. It excludes later Proto-OKN enrichment. Package bytes/hash must verify; package readiness alone is not worker dispatch authorization.'))
    b.add('ParagraphExport',obj({'paragraph_id':did('Paragraph'),'format':enum('markdown','latex','bibtex','rich-text'),
        'filename':string(),'media_type':string(),'content':string(),'plain_text':null(string()),'required_companions':array(string()),
        'citation_targets':array(ref('CitationTarget')),'warnings':array(string())},
        description='Download content or clipboard HTML/plain text derived from the same immutable Paragraph and pinned citation registry. rich-text HTML is sanitized; canonical resolver links enforce authorization. latex includes cite commands and requires references.bib.'))


def examples(b, f, e):
    selected={'id':f['gap']['id'],'source_id':f['gap_source']['id'],'source_revision':b.sha(f['gap_source']['raw'])}
    e['composer'].pop('inquiry');e['composer'].pop('dismech_context');e['composer']['source_gap']=selected
    for d in [e['draft'],e['saved']]: d['composer']=deepcopy(e['composer'])
    e['research_request'].update(composer=deepcopy(e['composer']),linked_dismech_context=[e['source_ref'](r) for r in f['dismech_records']])
    e['research_request']['attribution']['principal_kind']='registered'
    targets={r['source_id']:e['source_ref'](r) for r in f['dismech_records']}
    for a, raw in zip(e['gap']['attachments'],f['attachments']): a['target']=targets.get(raw.get('target_id'))
    e['gap']['source_detail']={'source_file':f['gap_source']['source_file'],'source_pointer':f['gap_source']['source_pointer'],
        'payload_sha256':b.sha(f['gap_source']['raw']),'raw':f['gap_source']['raw']}
    e['gap']['scientific_accounts']={'count':0,'scope':'public_exact_gap','as_of':b.NOW,'ranking':'curated','window_days':None}
    e['mechanism_search']['search'] = deepcopy(e['mechanism_search']['search'])
    e['mechanism_search']['search'].update(query='Endothelial dysfunction', mode='semantic', score_aggregation=None)
    e['gap_search']['search'].update(query='CAD genetic risk',mode='fuzzy',embedding_model=None,embedding_revision=None,score_aggregation=None)
    e['gap_search']['items'][0]['ranking']={'value':0.8,'metric':'fuzzy_similarity','rank':1}
    for key in ['queued_event','event']:e[key]['detail']=None
    coverage={'direction':'upstream','max_depth':5,'max_nodes':250,'complete':True,'missing_ids':[],'next_cursor':None}
    for key in ['account','claim','paragraph','question_result']:
        e[key]['coverage']=deepcopy(coverage)
    for key in ['account','claim','paragraph','question_result','gene_set']:
        for artifact in e[key]['artifacts']:artifact.update(availability='not_available',verification='checksum_verified')
    e['account']['research_statement']={'status':'succeeded','job_id':b.PARAGRAPH_JOB_ID,'paragraph_id':f['paragraph']['id']}
    package_dir=b.OUT/'examples/evidence-package'
    shutil.copytree(b.ROOT/'data/evidence-packages/cad-builder-v1',package_dir,dirs_exist_ok=True)
    package=json.loads((package_dir/'evidence-package.json').read_text())
    manifest=json.loads((package_dir/'manifest.json').read_text())
    e['evidence_package']={'job_id':b.JOB_ID,'package_sha256':manifest['package_sha256'],'package':package}
    e['complete']['result'].update(paragraph_job_ids=[b.PARAGRAPH_JOB_ID],evidence_package_sha256=manifest['package_sha256'])
    e['suggestions']['search']['query']='Endothelial dysfunction'
    e['suggestions']['limitations']=['Layout-only similarity scores. These five native CFDE example factors are not measured recommendations for the CAD gap or a demonstration of mapped-subset retrieval. Production defaults use existing EAGGL embeddings joined to the populated exact-trait/factor-number crosswalk.']
    return e


def endpoints(b,f,e):
    P=b.PATHS;S=b.SCHEMAS;ref=b.ref;obj=b.obj;array=b.array;string=b.string;enum=b.enum;did=b.did;null=b.nullable
    uuid=string(format='uuid')
    def op(path,method='get'):return P[path][method]
    def response_values(operation):
        for response in operation['responses'].values():
            for media in response['content'].values():
                for ex in media['examples'].values():yield ex['value']
    me=op('/v1/me');me['description']='Resolve a trusted registered or anonymous gateway assertion to a stable application UUID. No bearer means no private workspace access. Session cookies and OAuth routes belong to the gateway contract.'
    for value in response_values(me):
        if isinstance(value,dict) and 'user_id' in value:value.update(principal_kind='registered',workspace_expires_at=None)
    me['responses']['200']['content']['application/json']['examples']['anonymous']={'value':{
        'user_id':b.USER_ID,'principal_kind':'anonymous','workspace_expires_at':'2026-10-24T16:00:00Z',
        'display_name':None,'email':None,'email_verified':None,'orcid':None,'orcid_authenticated':False,'person':None}}
    for path in ['/v1/knowledge-gaps','/v1/knowledge-gaps/search']:
        o=op(path)
        for p in o['parameters']:
            if p['name']=='source':p['schema']=enum('dismech',default='dismech')
            if p['name']=='mode':p['schema']['default']='fuzzy';p['example']='fuzzy'
            if p['name']=='q':p['example']='CAD genetic risk'
            if p['name']=='disease_id':p['example']='MONDO:0021661'
    op('/v1/knowledge-gaps/search')['description']='Fuzzy lookup of imported DisMech gaps by default. Query text is not an authored inquiry or permission to launch research. Other explicit modes require their configured index and return 503 if unavailable. Rankings are retrieval signals. Example uses the exact CAD gap from the HTML study.'
    op('/v1/knowledge-gaps')['description']='Browse imported DisMech gaps ordered by distinct scientific-account count descending. Public scope (default) counts only explicitly published snapshots across users; workspace scope requires a session and counts owned saved accounts. Equal-count gaps shuffle on each new browse; a server seed in the signed continuation cursor preserves tie order across pages. Exact gap digests only: no text matching, attempts, paragraph jobs or implicit source-revision rollup. Invalid supplied credentials are rejected even for public reads. Filters apply before pagination; count or corpus changes expire cursors.'
    op('/v1/knowledge-gaps/search')['description']+=' Account counts use the same optional-session visibility rules as gap browsing; relevance order remains unchanged.'
    op('/v1/knowledge-gaps/{gap_id}')['description']+=' Account counts use the same optional-session visibility rules as gap browsing.'
    for path in ['/v1/knowledge-gaps','/v1/knowledge-gaps/search','/v1/knowledge-gaps/{gap_id}']:
        op(path)['parameters'].append(b.parameter('scope','query',enum('public','workspace',default='public'),'public'))
    op('/v1/drafts','post')['description']='Create editable selection state. Empty body creates a null source gap, empty anchors/dismissals, cfde-inc-v2 and both initial KGs. A selected gap is resolved from exact source identity/revision. No client question or linked DisMech list is accepted. Saving does not collect evidence or run an agent.'
    for parameter in op('/v1/mechanisms/search')['parameters']:
        if parameter['name']=='q': parameter['example']='Endothelial dysfunction'
    op('/v1/mechanisms/search')['description']='Search mechanisms independently of gap lookup. Initial EAGGL retrieval uses existing label embeddings joined to the configured completed exact-trait/factor-number CFDE mapping run. Return selectable mapped records with DAPPER Mechanism, catalog File and native cfde-inc-v2 anchor; exclude unmatched source factors from selectable results. Label/gene agreement is not required. Retain source-hit, embedding and mapping provenance server-side; a full new CFDE embedding corpus is not a prerequisite.'
    suggestion=op('/v1/mechanisms/suggest','post')
    suggestion['description']='Resolve linked DisMech context server-side from source_gap. Search existing EAGGL label embeddings and join the configured completed exact-trait/factor-number CFDE crosswalk before the top-five cutoff. Rank mapped factors by maximum per-mechanism cosine, deduplicate full native identity, stable ID tie-break, at most five total excluding manual anchors and dismissals. Return fewer if needed; no silent refill on removal. With no linked mechanisms use the exact gap text/subquery and require user anchor selection. Ignore label/gene differences for routing; pin mapping/source/embedding runs in server-owned selection provenance.'
    for ex in suggestion['requestBody']['content']['application/json']['examples'].values():
        v=ex['value'];v.pop('inquiry');v.pop('dismech_context');v['source_gap']=e['composer']['source_gap'];v['subquery']='Endothelial dysfunction'
    submit=op('/v1/jobs','post')
    submit['description']+=' Analysis submission requires an exact imported DisMech gap and nonempty resolved CFDE anchors. Freeze the saved source/mapping-run bindings and native IDs with the request; later mapping imports do not retarget historical jobs. Label/gene disagreement does not reject a mapped anchor. Persist each accepted account with an outbox record that automatically schedules its default paragraph job, unique by account payload, citation pins and paragraph settings. Analysis completion does not wait for paragraph success. Manual paragraph jobs support retries or alternate focus/settings.'
    for code,detail in [('SOURCE_GAP_REQUIRED','Select an imported DisMech gap; typed search text is not an inquiry.'),('SOURCE_REVISION_UNAVAILABLE','The exact gap source observation is unavailable; choose an available catalog revision.'),('EAGGL_MODEL_MISMATCH','The selected anchor does not resolve to cfde-inc-v2 in its pinned source/mapping context.')]:
        submit['responses']['422']['content']['application/problem+json']['examples'][code.lower()]={'value':b.problem(422,code,detail)}
    for o in [submit,op('/v1/drafts','post')]:
        o['responses']['429']['content']['application/problem+json']['examples']['anonymous_quota']={'value':b.problem(429,'ANONYMOUS_QUOTA_EXCEEDED','Anonymous workspace quota reached. Saved selections remain available; retry after the indicated interval.')}
    for path in ['/v1/accounts/{dapper_id}','/v1/claims/{dapper_id}','/v1/objects/{dapper_id}']:
        o=op(path);o['description']+=' Upstream closure follows scientific references and provenance inputs only, max_depth 0..5 and max_nodes 1..250 (default 5/250). An opaque cursor continues the same owner/filter/payload observation; missing or bounded references are reported explicitly. Unauthorized references are omitted without revealing private identifiers.'
        o['parameters'] += [b.parameter('max_depth','query',{'type':'integer','minimum':0,'maximum':5,'default':5},5),
            b.parameter('max_nodes','query',{'type':'integer','minimum':1,'maximum':250,'default':250},250),
            b.parameter('cursor','query',string(),'opaque-authorized-continuation')]
    account={'account':f['account'],'knowledge_gap':f['gap'],'claim_count':len(f['account_document']['claims']),
        'created_at':b.NOW,'job_id':b.JOB_ID,'research_statement':e['account']['research_statement']}
    b.operation('/v1/accounts','get','listAccounts','Scientific content','List your scientific accounts',
        'Owner-scoped, newest first then account ID; deduplicate repeated deliveries by digest. Filter by exact gap_id. No private records from other users with the same gap. Closing remarks provide the summary; paragraph status is independent.',
        'AccountList',{'owned':{'items':[account],'page':e['page']}},parameters=[b.parameter('gap_id','query',did('KnowledgeGap'),f['gap']['id'])]+b.page_parameters(),errors=('400','401','429'))
    b.operation('/v1/knowledge-gaps/{gap_id}/accounts','get','listKnowledgeGapAccounts','Knowledge gaps','List visible scientific accounts for a knowledge gap',
        'Accepted accounts for the exact DAPPER KnowledgeGap identity, newest first then account ID, deduplicated by scientific-account digest. Public scope (default) lists explicitly published snapshots across owners and omits private job IDs. Workspace scope requires a valid session and lists only its saved accounts. Invalid supplied sessions are rejected for either scope. Source-revision checks validate the selected observation; changed gap digests never merge. Attribution remains the original request actor. Each account ID links to its existing scientific endpoint, whose public reads are restricted to its published snapshot.',
        'AccountList',{'owned':{'items':[dict(account,attribution=e['research_request']['attribution'])],'page':e['page']},'no_visible_accounts':{'items':[],'page':e['page']}},
        parameters=[b.parameter('gap_id','path',did('KnowledgeGap'),f['gap']['id'],True),b.parameter('source_revision','query',string(pattern='^[a-f0-9]{64}$'),e['composer']['source_gap']['source_revision']),b.parameter('scope','query',enum('public','workspace',default='public'),'public')]+b.page_parameters(),public=True,errors=('400','401','404','409','429'))
    private_publication={'visibility':'private','version':0,'published_at':None,'updated_at':None,'can_manage':True,'has_unpublished_changes':False}
    public_publication={'visibility':'public','version':1,'published_at':b.NOW,'updated_at':b.NOW,'can_manage':True,'has_unpublished_changes':False}
    b.operation('/v1/accounts/{dapper_id}/publication','get','getAccountPublication','Scientific content','Inspect account publication',
        'Owner receives mutable publication controls even while private. Other readers receive only an active public publication with can_manage=false. Publishing is separate from scientific identity and frozen citation metadata; old citation access labels describe that exact historical metadata revision.',
        'PublicationState',{'private_owner':private_publication,'public_reader':dict(public_publication,can_manage=False)},
        parameters=[b.parameter('dapper_id','path',did('ScientificAccount'),f['account']['id'],True)],public=True,errors=('401','404','429'))
    b.operation('/v1/accounts/{dapper_id}/publication','post','updateAccountPublication','Scientific content','Publish, update or unpublish an account',
        'Explicit owner-only choice, also available to anonymous workspace owners. Publishing freezes the complete accepted account provenance and current accepted paragraph, exact citation revisions and only reachable source artifacts. Future paragraphs remain private until an explicit update. Unpublishing revokes this snapshot immediately; identical content independently published elsewhere stays public. No job logs, draft, queue, request or unrelated owner artifacts are published. Immutable scientific IDs, citations and original authorship do not change.',
        'PublicationState',{'published':public_publication,'unpublished':dict(private_publication,version=2,updated_at=b.NOW)},
        parameters=[b.parameter('dapper_id','path',did('ScientificAccount'),f['account']['id'],True)],request_schema='PublicationInput',
        request_examples={'publish':{'visibility':'public','expected_version':0},'unpublish':{'visibility':'private','expected_version':1}},idempotent=True,errors=('400','401','404','409','422','429'))
    outcome_id='66666666-6666-4666-8666-666666666666'
    outcome={'id':outcome_id,'outcome':'insufficient_evidence','summary':'The captured investigation did not support an accepted scientific account.',
        'reason':'This illustrative scoped investigation lacked evidence connecting the selected mechanism to the selected question.',
        'explored_topics':['The selected question and captured mechanism evidence.'],'missing_evidence':['Evidence connecting the mechanism to this question.'],
        'limitations':['This scoped result does not establish that a relationship is globally absent.'],'next_steps':['Review additional directly relevant studies before repeating this investigation.'],
        'knowledge_gap':f['gap'],'source_gap':e['composer']['source_gap'],
        'anchors':[{'source_id':a['reference']['source_id'],'mechanism_id':a['reference']['dapper_id'],'name':a['reference']['source_id'],'trait':None,'origin':a['origin']} for a in e['composer']['eaggl_anchors']],
        'selected_kgs':e['composer']['selected_kgs'],'created_at':b.NOW,'attribution':e['research_request']['attribution'],
        'scope_note':'A scoped exploration report, not a ScientificAccount or globally established negative finding.','record_format':'structured','job_id':b.JOB_ID,'publication':private_publication,
        'provenance':{'evidence_package_sha256':e['evidence_package']['package_sha256'],'outcome_sha256':'a'*64,'runtime_sha256':'b'*64,'ledger_sha256':'c'*64,
            'execution_mode':'box','source_bindings':[{'source_id':a['reference']['source_id'],'source_revision':a['reference']['source_revision'],'embedding_run_id':None,'mapping_run_id':None} for a in e['composer']['eaggl_anchors']],
            'coverage':{},'evidence_refs':[],'source_artifacts':[],'graph_queries':[]}}
    public_outcome={**outcome,'job_id':None,'publication':dict(public_publication,can_manage=False)}
    outcome_summary={key:public_outcome[key] for key in ('id','outcome','summary','knowledge_gap','anchors','created_at','attribution','publication')}
    description='A durable scoped insufficient-evidence exploration, separate from ScientificAccounts and excluded from their popularity counts. Captured author reasons are not independently validated scientific findings. Private by default; explicit publication shares only this frozen scope, original attribution and captured source evidence. Job logs, requests, runtime/ledger contents and the complete private package remain private. Invalid supplied credentials never downgrade to public.'
    b.operation('/v1/analysis-outcomes/{outcome_id}','get','getAnalysisOutcome','Scientific content','Read an explored analysis outcome',description,
        'AnalysisOutcome',{'private_owner':outcome,'public_reader':public_outcome},parameters=[b.parameter('outcome_id','path',uuid,outcome_id,True)],public=True,errors=('401','404','429'))
    b.operation('/v1/jobs/{job_id}/outcome','get','getJobAnalysisOutcome','Jobs','Find the saved scoped outcome for an owned job',
        'Owner-only lookup of the durable outcome already saved by this analysis job. Returns404 if no record exists; GET never runs research or performs a historical import.',
        'AnalysisOutcome',{'owner':outcome},parameters=[b.parameter('job_id','path',uuid,b.JOB_ID,True)],errors=('401','404','429'))
    b.operation('/v1/knowledge-gaps/{gap_id}/outcomes','get','listKnowledgeGapOutcomes','Knowledge gaps','List explored analysis outcomes for an exact gap',
        'Newest-first compact summaries for this exact gap. Public scope defaults to explicitly published outcome snapshots; workspace scope requires a session. Scientific-account counts and ranking remain unchanged. Detail/provenance is fetched only when an outcome is opened.',
        'AnalysisOutcomeList',{'published':{'items':[outcome_summary],'page':e['page']},'empty':{'items':[],'page':e['page']}},
        parameters=[b.parameter('gap_id','path',did('KnowledgeGap'),f['gap']['id'],True),b.parameter('scope','query',enum('public','workspace',default='public'),'public'),
            b.parameter('source_revision','query',string(pattern='^[a-f0-9]{64}$'),e['composer']['source_gap']['source_revision'])]+b.page_parameters(),public=True,errors=('401','404','409','429'))
    b.operation('/v1/analysis-outcomes/{outcome_id}/publication','get','getOutcomePublication','Scientific content','Inspect exploration outcome publication',
        description,'PublicationState',{'private_owner':private_publication,'public_reader':dict(public_publication,can_manage=False)},
        parameters=[b.parameter('outcome_id','path',uuid,outcome_id,True)],public=True,errors=('401','404','429'))
    b.operation('/v1/analysis-outcomes/{outcome_id}/publication','post','updateOutcomePublication','Scientific content','Publish or unpublish a scoped exploration',
        'Explicit owner-only publication, including anonymous workspace owners. Freeze this exploration and its captured source artifacts, never job logs or unrelated workspace artifacts. Unpublish revokes this snapshot; independently published evidence can remain available. Immutable records and original attribution do not change.',
        'PublicationState',{'published':public_publication,'unpublished':dict(private_publication,version=2,updated_at=b.NOW)},
        parameters=[b.parameter('outcome_id','path',uuid,outcome_id,True)],request_schema='PublicationInput',
        request_examples={'publish':{'visibility':'public','expected_version':0},'unpublish':{'visibility':'private','expected_version':1}},idempotent=True,errors=('400','401','404','409','422','429'))
    for path in ('/v1/analysis-outcomes/{outcome_id}','/v1/analysis-outcomes/{outcome_id}/publication','/v1/knowledge-gaps/{gap_id}/outcomes'):
        op(path)['security']=[{}, {'GatewayAssertion': []}]
    exploration={'source_gap':e['composer']['source_gap'],'knowledge_gap':f['gap'],'last_explored_at':b.NOW,'draft_id':b.DRAFT_ID,
        'scientific_accounts':{'count':1,'scope':'owner_exact_gap','as_of':b.NOW,'ranking':'curated','window_days':None}}
    b.operation('/v1/me/explorations','get','listExplorations','Research history','List your explored knowledge gaps',
        'Owner-scoped exact gap/source-revision projection, most recently explored first then source ID. Contains visits before a job is submitted. Resume through draft_id; account counts include only accessible owned results.',
        'ExplorationList',{'history':{'items':[exploration],'page':e['page']}},parameters=b.page_parameters(),errors=('400','401','429'))
    b.operation('/v1/me/explorations','post','recordExploration','Research history','Save an explored gap',
        'Upsert one owner + exact source-gap/revision visit. Before session establishment browsing stays local; replay selected visits after trusted bootstrap. Owner is derived from the assertion. Does not create a scientific record or paid job.',
        'Exploration',{'saved':exploration},request_schema='ExplorationInput',request_examples={'selected_gap':{'source_gap':e['composer']['source_gap'],'draft_id':b.DRAFT_ID}},idempotent=True,errors=('401','404','409','422','429'))
    b.operation('/v1/jobs/{job_id}/evidence-package','get','getEvidencePackage','Jobs','Inspect the frozen agent input',
        'Owner-authorized analysis-job package. Return 409 EVIDENCE_PACKAGE_NOT_READY before freeze, 404 for unavailable/non-analysis jobs. Exact package hash and LinkML schema are independent of later tool ledger/output. Recorded source paths are not public download URLs. The example is a real bounded capture; the account example is separately authored and not a validated output of this capture.',
        'EvidencePackageResult',{'captured_input':e['evidence_package']},parameters=[b.parameter('job_id','path',string(format='uuid'),b.JOB_ID,True)],errors=('401','404','409','429'))
    source_root=b.ROOT/'api/examples/evidence-package'
    artifact=min(e['evidence_package']['package']['source_artifacts'].values(),key=lambda a:(source_root/a['path']).stat().st_size)
    artifact_text=(source_root/artifact['path']).read_text()
    download=b.operation('/v1/artifacts/{sha256}','get','downloadArtifact','Scientific content','Download an authorized captured source',
        'Download exact captured bytes after owner authorization and SHA-256 verification. A digest is not an access grant. The File metadata describes the media type; the response uses attachment disposition, private no-store caching and nosniff. Missing captures remain explicitly unavailable; this route never fetches mutable source URLs.',
        string(format='binary'),{'captured_bytes':artifact_text},parameters=[b.parameter('sha256','path',string(pattern='^[a-f0-9]{64}$'),artifact['sha256'],True)],errors=('401','404','503'))
    download['responses']['200']['content']={'*/*':download['responses']['200']['content']['application/json']}
    for exchange in b.EXCHANGES:
        if exchange['operation_id']=='downloadArtifact':
            exchange['request']['headers']['Accept']='*/*'
            exchange['responses']['200']['content_type']='*/*'
    exports={}
    p=b.ROOT/'design/data/cad-account'
    targets=list({(c['target_id'],c['citation_metadata_revision']):c for c in f['paragraph']['citations']}.keys())
    for fmt,filename,media in [('markdown','research-statement.md','text/markdown'),('latex','research-statement.tex','application/x-tex'),('bibtex','references.bib','application/x-bibtex'),('rich-text','research-statement.html','text/html')]:
        rich=json.loads((p/'rich-text.json').read_text())
        exports[fmt]={'paragraph_id':f['paragraph']['id'],'format':fmt,'filename':filename,'media_type':media,
            'content':rich['html'] if fmt=='rich-text' else (p/filename).read_text(),
            'plain_text':rich.get('text',rich.get('plain_text')) if fmt=='rich-text' else None,
            'required_companions':['references.bib'] if fmt=='latex' else [],
            'citation_targets':[{'target_id':t,'citation_metadata_revision':r} for t,r in targets],
            'warnings':['Download references.bib alongside the LaTeX file.'] if fmt=='latex' else []}
    b.operation('/v1/paragraphs/{dapper_id}/export','get','exportParagraph','Citations','Export a cited research statement',
        'Return content and download metadata in JSON. Markdown includes linked references; rich-text returns sanitized clipboard HTML plus plain text; LaTeX uses cite commands and references.bib. BibTeX contains every distinct exact target/revision cited by the paragraph. These numbered-reference exports do not claim APA/MLA styling. Fixture links are local-preview URLs; runtime must render authorized canonical resolver URLs.',
        'ParagraphExport',exports,parameters=[b.parameter('dapper_id','path',did('Paragraph'),f['paragraph']['id'],True),
            b.parameter('format','query',enum('markdown','latex','bibtex','rich-text'),'markdown',True)],public=True,errors=('400','401','404','429'))
    detail={'kind':'preparation','state':'completed','source':'worker','call_id':None,'tool_name':None,
        'selected_kg':None,'display_arguments':None,'output_excerpt':None,'artifact_sha256':e['evidence_package']['package_sha256'],
        'duration_ms':None,'counts':{'scope':'retained','nodes':17,'edges':16,'truncated':True,'snapshot_sha256':e['evidence_package']['package_sha256']}}
    preparation={**e['queued_event'],'id':'2','event_type':'activity','status':'running','stage':'preparing_evidence',
        'message':'Retained 17 nodes and 16 edges in this bounded captured example.','detail':detail}
    activity={**preparation,'id':'3','stage':'authoring_account','message':'Inspecting the captured PIGEAN observations.',
        'detail':{**detail,'kind':'agent_message','state':'started','source':'harness','counts':None,
            'output_excerpt':'Public activity fixture; no live agent was run.'}}
    e['event']['id']='4';e['complete']['last_event_id']='4'
    stream=op('/v1/jobs/{job_id}/events')
    stream['responses']['200']['content']['application/json']['examples']['completed_events']['value']['items']=[e['queued_event'],preparation,activity,e['event']]
    stream['responses']['200']['content']['application/json']['examples']['completed_events']['value']['next_after']='4'
    stream['responses']['200']['content']['text/event-stream']['examples']['result_event']['value']='id: 4\nevent: result\ndata: '+b.canonical(e['event'])+'\n\n'
    for path,method in [('/v1/accounts/{dapper_id}','get'),('/v1/claims/{dapper_id}','get'),('/v1/objects/{dapper_id}','get'),('/v1/gene-sets/{dapper_id}','get'),('/v1/paragraphs/{dapper_id}','get'),('/v1/paragraphs/{dapper_id}/export','get'),('/v1/citations/{dapper_id}','get'),('/v1/citations/render','post'),('/v1/artifacts/{sha256}','get')]:
        if path not in P: continue
        operation=op(path,method); operation['security']=[{}, {'GatewayAssertion': []}]
        operation['description']+=' A valid owner retains private access. Without owner access, only an active explicit publication snapshot authorizes this scientific resource, exact cited revisions and reachable source artifacts. Invalid supplied credentials fail even on public reads. Unpublication revokes snapshot access; job/draft/request routes remain private.'
    for example in op('/v1/accounts/{dapper_id}')['responses']['200']['content']['application/json']['examples'].values():
        example['value']['publication']=private_publication
    # Synchronize exchange request values and curl snippets after amendments.
    from urllib.parse import quote, urlencode
    import shlex
    for methods in P.values():
        for o in methods.values():o['x-codeSamples']=[]
    for ex in b.EXCHANGES:
        o=op(ex['path_template'],ex['method'].lower());request=ex['request']
        if 'requestBody' in o:request['body']=o['requestBody']['content']['application/json']['examples'][ex['case']]['value']
        request['query']={p['name']:p['example'] for p in o['parameters'] if p['in']=='query' and p['name']!='cursor'}
        path=ex['path_template']
        for k,v in request['path'].items():path=path.replace('{'+k+'}',quote(str(v),safe=''))
        request['url']=b.BASE+path+('?' + urlencode(request['query']) if request['query'] else '')
        pieces=['curl','-X',ex['method'],shlex.quote(request['url'])]
        for k,v in request['headers'].items():pieces+=['-H',shlex.quote(k+': '+v)]
        if request['body'] is not None:pieces+=['--data-raw',shlex.quote(b.canonical(request['body']))]
        ex['curl']=' '.join(pieces);o['x-codeSamples'].append({'lang':'Shell','label':ex['case'],'source':ex['curl']})
