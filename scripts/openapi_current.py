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
        'as_of':time,'ranking':enum('curated','recent_account_count'),'window_days':null({'type':'integer','minimum':1})},
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
    b.add('AccountSummary',obj({'account':ref('DapperScientificAccount'),'knowledge_gap':ref('DapperKnowledgeGap'),
        'claim_count':{'type':'integer','minimum':1},'created_at':time,'job_id':uuid,'research_statement':ref('ParagraphState')}))
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
    op('/v1/knowledge-gaps')['description']='Browse imported DisMech gaps. Initial homepage is curated, with exact public-account counts as of the returned timestamp. Counts never include private work. Default source=dismech, include both gap kinds and all original statuses; filters may narrow. No manufactured popularity ranking.'
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
