"""Current design-contract amendments. Generates documentation only, no server routes."""
from copy import deepcopy
import gzip
import json
import shutil
import sys

OUTCOME_SUMMARY_KEYS = ('id','outcome','summary','knowledge_gap','anchors','created_at','attribution','publication')
KPN_TRAIT_PATTERN = r'^KPN\.TRAIT:[0-9]{7}$'
CAPTURED_AT = '2026-10-01T08:30:00Z'  # Explicitly fictional reference-reload capture and cutover times.
ARCHIVED_AT = '2026-10-01T09:00:00Z'
# KPN.TRAIT:0000398 Factor1 as copied from the LAP projection eaggl_capped__cfde_2026_09_28
# (factor_index.tsv, factor_metadata.tsv without its local source path, trait_kpn_map.tsv).
KPN_EXAMPLE = {'trait': {'id': 'KPN.TRAIT:0000398', 'name': 'Type 2 diabetes (T2D)', 'legacy_phenotype_id': 'T2D',
                         'trait_group': 'metabolic', 'trait_type': 'phenotype'},
    'gwas_source_category': 'KPN', 'factor': 'Factor1', 'label': 'Metabolic Dysregulation Indicators',
    'index': {'global_eaggl_column': 'Factor3357', 'factor_id': 'T2D::Factor1', 'trait': 'T2D', 'kpn_trait_id': 'KPN.TRAIT:0000398',
              'factor': 'Factor1', 'factor_number': '1', 'factor_label': 'Metabolic Dysregulation Indicators',
              'n_nonzero_loadings': '633', 'loading_l2': '4.95366', 'loading_variant': 'capped'},
    'metadata': {'label': 'Metabolic Dysregulation Indicators', 'gene_set_score': '3.06', 'gene_score': '0.154',
                 'top_genes': 'LEPR,LEP,STAT3,CYP19A1,ALMS1',
                 'top_gene_sets': 'mp_impaired_glucose_tolerance,mp_increased_circulating_insulin_level,mp_insulin_resistance,mp_increased_body_weight,mp_increased_circulating_glucose_level',
                 'source_gene_rows': '1265', 'aligned_gene_rows': '1265', 'nonzero_gene_loadings': '1074',
                 'raw_loading_l1': '72.93184311186471', 'raw_loading_l2': '4.967819929633034'},
    'lap': {'project': 'eaggl_capped__cfde_2026_09_28', 'pigean_commit': 'ca59661644dc9ead429fc7e59050870ba49b26e8',
            'projection_scope': 'per_trait', 'kpn_release': 'v0.0.2'}}


def reference_helpers(b):
    """The backend's identifier and archive-stamp helpers, so examples match what it writes."""
    source = str(b.ROOT / 'services/backend/src')
    if source not in sys.path: sys.path.insert(0, source)
    from reveal_backend import reference_archive, reference_generation
    return reference_generation, reference_archive


def summarize_outcome(record):
    """AnalysisOutcomeSummary of a record, as analysis_outcomes.summary(): archive propagates when present."""
    item = {key: deepcopy(record[key]) for key in OUTCOME_SUMMARY_KEYS}
    if record.get('archive'): item['archive'] = deepcopy(record['archive'])
    return item


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
    b.add('WorkspaceEvent', obj({'schema_version':{'type':'integer','const':1},
        'event_id':string(), 'cursor':string(pattern='^[0-9]+$'), 'scope':enum('workspace','public'),
        'committed_at':time, 'event_type':string(), 'entity_id':string(),
        'entity_revision':{'type':'integer','minimum':0}, 'operation':enum('upsert','remove','invalidate','resync'),
        'collections':array(enum('drafts','gaps','explorations','requests','jobs','accounts','identity','catalog'))},
        description='Committed invalidation event. SSE id is an opaque signed cursor bound to the principal and notification namespace; the envelope cursor is scope-local. Replay requires current authorization.'))
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
    stages=S['Job']['properties']['stage']['enum'];stages+=['preparing_evidence','starting_agent','collecting_output']
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
    b.add('AnalysisOutcomeSummary',obj({key:deepcopy(S['AnalysisOutcome']['properties'][key]) for key in OUTCOME_SUMMARY_KEYS}))
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
    reference_schemas(b)


def reference_schemas(b):
    """Versioned reference generations and archived work (docs/reference-reload.md §5)."""
    rg,_=reference_helpers(b)
    S=b.SCHEMAS; ref=b.ref; obj=b.obj; array=b.array; string=b.string; enum=b.enum; null=b.nullable; did=b.did
    uuid=string(format='uuid'); digest=string(pattern='^[a-f0-9]{64}$'); time=string(format='date-time')
    generation=string('Reference generation id: 64 lowercase hex characters.',pattern='^[a-f0-9]{64}$')
    kpn=string(pattern=KPN_TRAIT_PATTERN); number={'type':'number'}
    model=lambda:enum(*rg.MODELS,description='EAGGL reference model: cfde-inc-v2 (legacy CFDE-linked factors) or eaggl-capped-v1 (KPN reference generations).')
    b.add('KpnTrait',obj({'id':kpn,'name':string(),'legacy_phenotype_id':string(),'trait_group':null(string()),'trait_type':null(string())},
        description='KPN trait (kpn-data-models portal_id) of a factor in an eaggl-capped-v1 reference generation. legacy_phenotype_id is the EAGGL/portal phenotype code.'))
    S['EagglFactor']['properties'].update(model=model(),reference_generation_id=generation,kpn_trait=null(ref('KpnTrait')))
    S['EagglFactor']['description']+=' Each deployment serves the factors of one active reference generation. In an eaggl-capped-v1 generation, source_id is factor:kpn:{NNNNNNN}:eaggl-capped-v1:{FactorN}, the record adds reference_generation_id and kpn_trait, cfde_anchor.label is the EAGGL factor label, and catalog_file identifies the canonical factor metadata bytes. Legacy cfde-inc-v2 records omit both fields.'
    for name in ('Composer','SuggestInput'): S[name]['properties']['model']=model()
    b.add('ReferenceArchiveAnchor',obj({'source_id':string(pattern='^factor:'),'mechanism_id':null(did('Mechanism')),'factor_id':null(string()),
        'trait':null(string()),'kpn_trait_id':null(kpn),'label':null(string()),'name':null(string()),'origin':null(string()),
        'archived_reference_factor_id':digest},
        description='An original EAGGL anchor of archived work, frozen at archive time. trait, label and name are display text (trait is the phenotype shown with the anchor, else its phenotype code); fields that could not be recovered are null. Resolve the frozen factor at /v1/reference-factors/{archived_reference_factor_id}.'))
    b.add('ReferenceArchive',obj({'status':{'type':'string','const':'archived'},'reason':{'type':'string','const':'reference_generation_superseded'},
        'archived_at':time,'from_reference_generation':generation,'to_reference_generation':generation,
        'history':array(obj({'from_reference_generation':generation,'to_reference_generation':generation,'archived_at':time}),minItems=1),
        'reference':obj({'model':model(),'anchors':array(ref('ReferenceArchiveAnchor'))}),
        'gap':null(obj({'id':did('KnowledgeGap'),'source_id':null(string()),'source_revision':null(digest)})),
        'analysis':obj({'job_id':null(uuid),'request_id':null(uuid),'evidence_package_sha256':null(digest),
            'account_id':null(did('ScientificAccount')),'outcome_id':null(uuid)})},
        description='Outdated-reference stamp on work built on a superseded reference generation. The work keeps every record and stays readable, downloadable and publishable, and paragraph jobs still run; re-analysis and review retry are blocked; start a new analysis on the same gap with current factors instead. reference holds the original anchors. Public copies null analysis.job_id and analysis.request_id. A later reload advances to_reference_generation and appends to history. Absent on current work and in deployments that never reloaded reference data.'))
    for name in ('AccountSummary','AccountResult','AnalysisOutcome','ResearchRequest'):
        S[name]['properties']['archive']=ref('ReferenceArchive')
    # The outcome summary is derived from the outcome: optional keys propagate with it.
    S['AnalysisOutcomeSummary']['properties']['archive']=deepcopy(S['AnalysisOutcome']['properties']['archive'])
    b.add('ArchivedReferenceFactor',obj({'format':{'type':'string','const':'reveal.archived-reference-factor/1'},'archive_id':digest,
        'generation_id':generation,'model':model(),'source_id':string(pattern='^factor:'),'factor_id':string(),'trait':string(),
        'kpn_trait_id':null(kpn),'label':string(),
        'mechanism':obj({'id':null(did('Mechanism')),'name':string(),'description':string()}),
        'metadata':{'type':'object','additionalProperties':True},
        'top_genes':array(obj({'symbol':string(),'loading':number})),
        'top_gene_sets':array(obj({'rank':{'type':'integer'},'gene_set_id':null(did('GeneSet')),'name':string(),'library':null(string()),
            'collection_id':null(string()),'source_key':null(string()),'joint_loading':null(number),'marginal_loading':null(number),'score':null(number)})),
        'generation_manifest_sha256':digest,'captured_at':time},
        description='Immutable snapshot of an EAGGL factor referenced by archived work, kept after its reference generation is purged. Public reference data. metadata is the source factor metadata; top_genes (by loading) and top_gene_sets (by rank) hold at most 50 entries each. Legacy cfde-inc-v2 snapshots have a null kpn_trait_id and null gene-set loadings. mechanism.id is the DAPPER Mechanism the catalog served, or null when it could not be minted.'))
    S['Problem']['properties']['archived_reference_factor']={**null(ref('ArchivedReferenceFactor')),
        'description':'Only on 410 REFERENCE_GENERATION_SUPERSEDED from a mechanism read: the frozen factor, or null when none was captured.'}


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
    e['reference']=reference_examples(b,f,e)
    return e


def reference_examples(b, f, e):
    """A cutover from the legacy generation to an illustrative KPN generation, built with the backend's own helpers.

    The legacy generation id is the real one (tracked EAGGL->CFDE mapping run). The KPN generation id
    is illustrative: no reference_reload build was executed for these fixtures.
    """
    rg,archive=reference_helpers(b)
    run=json.loads((b.ROOT/'data/eaggl-cfde-mapping/2026-09-25/lookup-ad-factor1.json').read_text())
    manifest={'format':'reveal.reference-generation/1','kind':rg.LEGACY_KIND,'model':rg.LEGACY_MODEL,'legacy_mapping_run_id':run['run_id'],
        'eaggl_import_id':run['eaggl_import_id'],'legacy_gene_set_import_id':run['gene_set_import_id']}
    legacy=rg.legacy_generation_id(run['run_id'])
    current=b.sha({'format':'reveal.reference-generation/1','kind':rg.KPN_KIND,'model':rg.KPN_MODEL,'lap_project':KPN_EXAMPLE['lap']['project'],
        'kpn_release':KPN_EXAMPLE['lap']['kpn_release'],'note':'Illustrative contract fixture; no reference_reload build was executed.'})
    # The legacy anchor that the example drafts, requests, accounts and outcomes were built on.
    record=f['factor_records'][0]; native=record['source_id']; anchor=e['composer']['eaggl_anchors'][0]; parts=native.split(':')
    binding={'eaggl_factor_id':parts[2]+'::'+parts[4],'mapping_run_id':run['run_id'],'gene_set_import_id':run['gene_set_import_id'],'cfde_node_id':native}
    request_binding={'anchors':[binding],'anchor_display':{native:{'reference':deepcopy(anchor['reference']),
        'label':record['cfde_anchor']['label'],'subtitle':record['cfde_anchor']['subtitle']}}}
    anchors=archive.anchors_for_stamp(request_binding,composer=e['composer'],scientific_document={'mechanisms':[record['object']]},generation_id=legacy)
    gap={key:e['composer']['source_gap'][key] for key in ('id','source_id','source_revision')}
    package=e['evidence_package']['package_sha256']
    def stamp(**analysis):
        return rg.build_stamp(legacy,current,reference={'model':rg.LEGACY_MODEL,'anchors':deepcopy(anchors)},gap=dict(gap),
            analysis={'job_id':b.JOB_ID,'request_id':b.REQUEST_ID,'evidence_package_sha256':package,**analysis},at=ARCHIVED_AT)
    # Frozen snapshot of that anchor, from the captured catalog record and interactive loadings (top five genes/three gene sets).
    captured=json.loads((b.ROOT/'data/evidence-packages/cad-builder-v1/evidence-package.json').read_text())
    loadings=captured['pigean']['mechanisms'][native]; entities=captured['entities']['gene_sets']
    ranked=lambda items,n:sorted(items.items(),key=lambda item:(-item[1]['factor_value'],item[0]))[:n]
    def gene_set(rank,key):
        source=key.removeprefix('gene_set:'); segments=source.split('___'); prefixes={s.split('__',1)[0] for s in segments}
        return {'rank':rank,'gene_set_id':entities[key]['dapper_id'],'name':entities[key]['display_name'],
            'library':next(iter(prefixes)) if len(prefixes)==1 and all('__' in s for s in segments) else None,
            'collection_id':None,'source_key':source,'joint_loading':None,'marginal_loading':None,'score':None}
    catalog=json.loads((b.ROOT/'data/interactive/2026-09-24/catalog-factor-t2d.json').read_text())['response']['items']
    node=record['object']
    snapshot={'format':archive.SNAPSHOT_FORMAT,'generation_id':legacy,'model':rg.LEGACY_MODEL,'source_id':native,'factor_id':binding['eaggl_factor_id'],
        'trait':parts[2],'kpn_trait_id':None,'label':record['cfde_anchor']['label'],
        'mechanism':{key:node[key] for key in ('id','name','description')},'metadata':next(item for item in catalog if item['node_id']==native),
        'top_genes':[{'symbol':key.removeprefix('gene:'),'loading':value['factor_value']} for key,value in ranked(loadings['gene_loadings']['items'],5)],
        'top_gene_sets':[gene_set(rank,key) for rank,(key,_) in enumerate(ranked(loadings['gene_set_loadings']['items'],3),1)],
        'generation_manifest_sha256':b.sha(manifest)}
    archived={**snapshot,'archive_id':rg.archive_id(legacy,native),'captured_at':CAPTURED_AT}
    assert all(item['archived_reference_factor_id']==archived['archive_id'] for item in anchors)
    # A factor of the KPN generation, minted as catalog.kpn_factors does.
    trait=KPN_EXAMPLE['trait']; factor=KPN_EXAMPLE['factor']; label=KPN_EXAMPLE['label']; public=rg.public_id(trait['id'],factor)
    metadata={**KPN_EXAMPLE['index'],**KPN_EXAMPLE['metadata'],'kpn':{'phenotype_name':trait['name'],'trait_group':trait['trait_group'],
        'trait_type':trait['trait_type'],'gwas_source_category':KPN_EXAMPLE['gwas_source_category']},'lap':KPN_EXAMPLE['lap']}
    raw=(b.canonical(metadata)+'\n').encode()
    kpn_factor={'source':'eaggl','source_id':public,'source_revision':b.sha(metadata),'object_class':'Mechanism',
        'object':b.mint(rg.mechanism_node(public,trait['name'],trait['id'],factor,label),'Mechanism'),
        'cfde_anchor':{'node_id':public,'node_type':'factor','label':label,'subtitle':trait['name']+' ('+factor+')'},
        'model':rg.KPN_MODEL,'reference_generation_id':current,'kpn_trait':deepcopy(trait),
        'catalog_file':b.mint({'filename':'cfde-factor.json','mime_type':'application/json','sha256':b.sha(raw),'size_in_bytes':len(raw)},'File')}
    return {'legacy':legacy,'current':current,'stamp':stamp,'public_stamp':rg.public_stamp,'archived_factor':archived,'kpn_factor':kpn_factor,
        'account':stamp(account_id=f['account']['id']),'request':stamp()}


def endpoints(b,f,e):
    P=b.PATHS;S=b.SCHEMAS;ref=b.ref;obj=b.obj;array=b.array;string=b.string;enum=b.enum;did=b.did;null=b.nullable
    uuid=string(format='uuid')
    P['/v1/me/workspace/events']={'get':{'operationId':'subscribeWorkspaceEvents','tags':['Research history'],
        'summary':'Subscribe to committed workspace changes',
        'description':'Authenticated SSE backed by durable RDS replay and managed Redis Pub/Sub wakeups. Subscribe before replay. Send Last-Event-ID or after to resume; both must agree if supplied. workspace_change carries WorkspaceEvent. ready, resync_required, connection_degraded and access_revoked are stream control events. Resync explicitly reloads authorized collections. Heartbeats do not read Redis or query application state.',
        'security':[{'ApplicationBearer':[]}],
        'parameters':[{'name':'Last-Event-ID','in':'header','required':False,'schema':string(),'example':'opaque-signed-cursor','description':'Opaque signed reconnect cursor.'},
                      {'name':'after','in':'query','required':False,'schema':string(),'example':'opaque-signed-cursor','description':'Opaque signed reconnect cursor, equivalent to Last-Event-ID.'}],
        'x-codeSamples':[{'lang':'Shell','label':'Subscribe','source':"curl --no-buffer http://localhost:3100/api/backend/v1/me/workspace/events -H 'Accept: text/event-stream' -H 'Authorization: Bearer <api-key-or-gateway-assertion>'"}],
        'responses':{'200':{'description':'Committed events and stream control messages. Each workspace_change data frame is a WorkspaceEvent JSON object.',
            'content':{'text/event-stream':{'schema':string(),'examples':{'change':{'value':'id: opaque-signed-cursor\nevent: workspace_change\ndata: '+json.dumps({
                'schema_version':1,'event_id':'scope:1','cursor':'1','scope':'workspace','committed_at':'2026-09-30T00:00:00Z',
                'event_type':'draft.changed','entity_id':b.DRAFT_ID,'entity_revision':2,'operation':'upsert','collections':['drafts','gaps']})+'\n\n'}}}},
            'x-event-schema':ref('WorkspaceEvent')},
            **{code:{'description':b.ERRORS[code]['title'],'content':b.content('Problem',{b.ERRORS[code]['code'].lower():b.ERRORS[code]},'application/problem+json')} for code in ('400','401','503')}}}}
    b.EXCHANGES.append({'operation_id':'subscribeWorkspaceEvents','case':'request','method':'GET',
        'path_template':'/v1/me/workspace/events',
        'request':{'url':b.BASE+'/v1/me/workspace/events','path':{},'query':{},
            'headers':{'Authorization':'Bearer <api-key-or-gateway-assertion>','Accept':'text/event-stream'},'body':None},
        'responses':{'200':{'content_type':'text/event-stream','examples':{
            'change':P['/v1/me/workspace/events']['get']['responses']['200']['content']['text/event-stream']['examples']['change']['value']}}},'curl':''})
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
    S['JobBudgetFailure']=obj({'scope':enum('authoring','review'),
        'limit_usd':{'type':'number','minimum':0},'spent_usd':null({'type':'number','minimum':0}),
        'next_call_max_usd':null({'type':'number','minimum':0})})
    S['JobFailure']['properties']['budget']=ref('JobBudgetFailure')
    S['ReviewRetryInput']=obj({'expected_last_event_id':string(pattern='^[0-9]+$')})
    b.operation('/v1/jobs/{job_id}/retry-review','post','retryJobReview','Jobs','Retry independent review on saved output',
        'Owner-only, idempotent retry for REVIEW_UNAVAILABLE or REVIEW_BUDGET_EXCEEDED. Require the latest job event ID and a checksum-verified completed authoring capture. Requeue the same job with a new validation attempt; preserve original evidence, authoring model, artifacts and activity. Never launch the research agent. Scientific rejection, incomplete capture and active or successful jobs cannot use this route. Current configured review budget applies to each explicit retry. Normal account acceptance and paragraph generation follow a passing review.',
        'Job',{'queued':dict(e['complete'],status='queued',stage='validating',failure=None,result=None,completed_at=None)},
        request_schema='ReviewRetryInput',request_examples={'saved_output':{'expected_last_event_id':'2'}},
        parameters=[b.parameter('job_id','path',uuid,b.JOB_ID,True)],status=202,idempotent=True,errors=('401','404','409','422','429'))
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
    R=e['reference']
    reference_state=b.parameter('reference_state','query',enum('current','archived','all',default='all'),'all',
        description='Filter by reference state: current work, archived work built on a superseded reference generation (it carries archive), or all (default) with current items first, each group in the usual order. Deployments that never reloaded reference data have no archived items.')
    state_note=' reference_state filters current and archived work; the default all lists current items first.'
    account={'account':f['account'],'knowledge_gap':f['gap'],'claim_count':len(f['account_document']['claims']),
        'created_at':b.NOW,'job_id':b.JOB_ID,'research_statement':e['account']['research_statement']}
    b.operation('/v1/accounts','get','listAccounts','Scientific content','List your scientific accounts',
        'Owner-scoped, newest first then account ID; deduplicate repeated deliveries by digest. Filter by exact gap_id. No private records from other users with the same gap. Closing remarks provide the summary; paragraph status is independent.'+state_note,
        'AccountList',{'owned':{'items':[account],'page':e['page']},'archived':{'items':[dict(account,archive=R['account'])],'page':e['page']}},
        parameters=[b.parameter('gap_id','query',did('KnowledgeGap'),f['gap']['id']),reference_state]+b.page_parameters(),errors=('400','401','429'))
    b.operation('/v1/knowledge-gaps/{gap_id}/accounts','get','listKnowledgeGapAccounts','Knowledge gaps','List visible scientific accounts for a knowledge gap',
        'Accepted accounts for the exact DAPPER KnowledgeGap identity, newest first then account ID, deduplicated by scientific-account digest. Public scope (default) lists explicitly published snapshots across owners and omits private job IDs. Workspace scope requires a valid session and lists only its saved accounts. Invalid supplied sessions are rejected for either scope. Source-revision checks validate the selected observation; changed gap digests never merge. Attribution remains the original request actor. Each account ID links to its existing scientific endpoint, whose public reads are restricted to its published snapshot.'+state_note,
        'AccountList',{'owned':{'items':[dict(account,attribution=e['research_request']['attribution'])],'page':e['page']},'no_visible_accounts':{'items':[],'page':e['page']},
            'archived_published':{'items':[dict(account,attribution=e['research_request']['attribution'],job_id=None,
                research_statement=dict(account['research_statement'],job_id=None),archive=R['public_stamp'](R['account']))],'page':e['page']}},
        parameters=[b.parameter('gap_id','path',did('KnowledgeGap'),f['gap']['id'],True),b.parameter('source_revision','query',string(pattern='^[a-f0-9]{64}$'),e['composer']['source_gap']['source_revision']),b.parameter('scope','query',enum('public','workspace',default='public'),'public'),reference_state]+b.page_parameters(),public=True,errors=('400','401','404','409','429'))
    private_publication={'visibility':'private','version':0,'published_at':None,'updated_at':None,'can_manage':True,'has_unpublished_changes':False}
    public_publication={'visibility':'public','version':1,'published_at':b.NOW,'updated_at':b.NOW,'can_manage':True,'has_unpublished_changes':False}
    b.operation('/v1/accounts/{dapper_id}/publication','get','getAccountPublication','Scientific content','Inspect account publication',
        'Owner receives mutable publication controls even while private. Other readers receive only an active public publication with can_manage=false. Publishing is separate from scientific identity and frozen citation metadata; old citation access labels describe that exact historical metadata revision.',
        'PublicationState',{'private_owner':private_publication,'public_reader':dict(public_publication,can_manage=False)},
        parameters=[b.parameter('dapper_id','path',did('ScientificAccount'),f['account']['id'],True)],public=True,errors=('401','404','429'))
    b.operation('/v1/accounts/{dapper_id}/publication','post','updateAccountPublication','Scientific content','Publish, update or unpublish an account',
        'Explicit owner-only choice. Publishing or updating a public snapshot requires a registered signed-in session; anonymous owners receive 403 SIGN_IN_REQUIRED, including on idempotent retries. Owners may unpublish an existing snapshot with either session kind. Publishing freezes the complete accepted account provenance and current accepted paragraph, exact citation revisions and only reachable source artifacts. Future paragraphs remain private until an explicit update. Unpublishing revokes this snapshot immediately; identical content independently published elsewhere stays public. No job logs, draft, queue, request or unrelated owner artifacts are published. Immutable scientific IDs, citations and original authorship do not change.',
        'PublicationState',{'published':public_publication,'unpublished':dict(private_publication,version=2,updated_at=b.NOW)},
        parameters=[b.parameter('dapper_id','path',did('ScientificAccount'),f['account']['id'],True)],request_schema='PublicationInput',
        request_examples={'publish':{'visibility':'public','expected_version':0},'unpublish':{'visibility':'private','expected_version':1}},idempotent=True,errors=('400','401','403','404','409','422','429'))
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
    outcome_summary=summarize_outcome(public_outcome)
    outcome_stamp=R['stamp'](outcome_id=outcome_id)
    archived_outcome={**outcome,'archive':outcome_stamp}
    archived_public_outcome={**public_outcome,'archive':R['public_stamp'](outcome_stamp)}
    description='A durable scoped insufficient-evidence exploration, separate from ScientificAccounts and excluded from their popularity counts. Captured author reasons are not independently validated scientific findings. Private by default; explicit publication shares only this frozen scope, original attribution and captured source evidence. Job logs, requests, runtime/ledger contents and the complete private package remain private. Invalid supplied credentials never downgrade to public.'
    b.operation('/v1/analysis-outcomes','get','listAnalysisOutcomes','Personal workspace','List saved workspace explorations',
        'Session-required, newest-first summaries of all completed insufficient-evidence explorations owned by this workspace, private or published, across every knowledge gap. Includes previously saved records without a new run or publication. Operational failures are not scientific exploration outcomes. Other owners are excluded, even for published records. Detail and provenance are loaded only when opened.'+state_note,
        'AnalysisOutcomeList',{'saved':{'items':[dict(outcome_summary,publication=private_publication)],'page':e['page']},'empty':{'items':[],'page':e['page']},
            'archived':{'items':[summarize_outcome(dict(archived_outcome,publication=private_publication))],'page':e['page']}},
        parameters=[reference_state]+b.page_parameters(),errors=('401','409','429'))
    b.operation('/v1/analysis-outcomes/{outcome_id}','get','getAnalysisOutcome','Scientific content','Read an explored analysis outcome',
        description+' An outcome built on a superseded reference generation carries archive (outside provenance) and stays readable and publishable.',
        'AnalysisOutcome',{'private_owner':outcome,'public_reader':public_outcome,'archived_public_reader':archived_public_outcome},
        parameters=[b.parameter('outcome_id','path',uuid,outcome_id,True)],public=True,errors=('401','404','429'))
    b.operation('/v1/jobs/{job_id}/outcome','get','getJobAnalysisOutcome','Jobs','Find the saved scoped outcome for an owned job',
        'Owner-only lookup of the durable outcome already saved by this analysis job. Returns404 if no record exists; GET never runs research or performs a historical import.',
        'AnalysisOutcome',{'owner':outcome},parameters=[b.parameter('job_id','path',uuid,b.JOB_ID,True)],errors=('401','404','429'))
    b.operation('/v1/knowledge-gaps/{gap_id}/outcomes','get','listKnowledgeGapOutcomes','Knowledge gaps','List explored analysis outcomes for an exact gap',
        'Newest-first compact summaries for this exact gap. Public scope defaults to explicitly published outcome snapshots; workspace scope requires a session. Scientific-account counts and ranking remain unchanged. Detail/provenance is fetched only when an outcome is opened.'+state_note,
        'AnalysisOutcomeList',{'published':{'items':[outcome_summary],'page':e['page']},'empty':{'items':[],'page':e['page']},
            'archived_published':{'items':[summarize_outcome(archived_public_outcome)],'page':e['page']}},
        parameters=[b.parameter('gap_id','path',did('KnowledgeGap'),f['gap']['id'],True),b.parameter('scope','query',enum('public','workspace',default='public'),'public'),
            b.parameter('source_revision','query',string(pattern='^[a-f0-9]{64}$'),e['composer']['source_gap']['source_revision']),reference_state]+b.page_parameters(),public=True,errors=('401','404','409','429'))
    b.operation('/v1/analysis-outcomes/{outcome_id}/publication','get','getOutcomePublication','Scientific content','Inspect exploration outcome publication',
        description,'PublicationState',{'private_owner':private_publication,'public_reader':dict(public_publication,can_manage=False)},
        parameters=[b.parameter('outcome_id','path',uuid,outcome_id,True)],public=True,errors=('401','404','429'))
    b.operation('/v1/analysis-outcomes/{outcome_id}/publication','post','updateOutcomePublication','Scientific content','Publish or unpublish a scoped exploration',
        'Explicit owner-only publication. Publishing or updating a public snapshot requires a registered signed-in session; anonymous owners receive 403 SIGN_IN_REQUIRED, including on idempotent retries. Owners may unpublish with either session kind. Freeze this exploration and its captured source artifacts, never job logs or unrelated workspace artifacts. Unpublish revokes this snapshot; independently published evidence can remain available. Immutable records and original attribution do not change.',
        'PublicationState',{'published':public_publication,'unpublished':dict(private_publication,version=2,updated_at=b.NOW)},
        parameters=[b.parameter('outcome_id','path',uuid,outcome_id,True)],request_schema='PublicationInput',
        request_examples={'publish':{'visibility':'public','expected_version':0},'unpublish':{'visibility':'private','expected_version':1}},idempotent=True,errors=('400','401','403','404','409','422','429'))
    for path in ('/v1/analysis-outcomes/{outcome_id}','/v1/analysis-outcomes/{outcome_id}/publication','/v1/knowledge-gaps/{gap_id}/outcomes'):
        op(path)['security']=[{}, {'ApplicationBearer': []}]
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
    download['responses']['307']={'description':'After authorization, redirect to an exact, verified S3 object version. The private URL expires after 60 seconds; previously issued URLs can remain valid until expiry after unpublication.',
        'headers':{'Location':{'description':'Short-lived artifact download URL. Never cache or persist as an identifier.','schema':{'type':'string','format':'uri'}}}}
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
        operation=op(path,method); operation['security']=[{}, {'ApplicationBearer': []}]
        operation['description']+=' A valid owner retains private access. Without owner access, only an active explicit publication snapshot authorizes this scientific resource, exact cited revisions and reachable source artifacts. Invalid supplied credentials fail even on public reads. Unpublication revokes snapshot access; job/draft/request routes remain private.'
    for example in op('/v1/accounts/{dapper_id}')['responses']['200']['content']['application/json']['examples'].values():
        example['value']['publication']=private_publication
    reference_endpoints(b,f,e)
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


def reference_endpoints(b, f, e):
    """Reference-generation route, parameters, error codes and examples (docs/reference-reload.md §5)."""
    P=b.PATHS; R=e['reference']; string=b.string
    def op(path,method='get'):return P[path][method]
    def example(path,name,value,method='get',exchange=True):
        """Add a 200 example to an existing operation and, unless exchange=False, to its request exchange."""
        op(path,method)['responses']['200']['content']['application/json']['examples'][name]={'summary':name.replace('_',' ').capitalize(),'value':value}
        for ex in b.EXCHANGES:
            if exchange and ex['path_template']==path and ex['method']==method.upper() and ex['case']=='request':ex['responses']['200']['examples'][name]=value
    def problem(path,method,status,code,detail,**extra):
        o=op(path,method);key=str(status)
        if key not in o['responses']:
            o['responses'][key]={'description':code.replace('_',' ').title(),'content':b.content('Problem',{},'application/problem+json')}
            o['responses']=dict(sorted(o['responses'].items()))
        o['responses'][key]['content']['application/problem+json']['examples'][code.lower()]={'value':b.problem(status,code,detail,**extra)}
    def describe(path,method,text,old=None,new=None):
        o=op(path,method)
        if old is not None:
            assert old in o['description'],(path,old);o['description']=o['description'].replace(old,new)
        o['description']+=text
    archived=R['archived_factor'];kpn=R['kpn_factor'];reload='Reference data is being reloaded. Retry shortly.'
    b.operation('/v1/reference-factors/{archive_id}','get','getArchivedReferenceFactor','Mechanisms','Inspect a factor frozen for archived work',
        'Public reference data. Returns the immutable snapshot of an EAGGL factor that archived work referenced: identity, model, trait, label, DAPPER Mechanism, source metadata, top genes and top gene sets. Snapshots are captured before a reference reload and never purged, so archived accounts and outcomes keep rendering after their generation is retired. archive_id is archive.reference.anchors[].archived_reference_factor_id. Unknown or malformed ids return 404. The example is illustrative: captured catalog and interactive loadings stand in for the EAGGL metadata and top-50 lists.',
        'ArchivedReferenceFactor',{'legacy_factor':archived},parameters=[b.parameter('archive_id','path',string(pattern='^[a-f0-9]{64}$'),archived['archive_id'],True)],
        public=True,errors=('404','429'))
    describe('/v1/mechanisms/{source_id}','get',' After a reference reload, the id of a factor from a superseded generation returns 410 REFERENCE_GENERATION_SUPERSEDED with the frozen archived_reference_factor (null when none was captured). Deployments that never reloaded reference data do not return 410. The kpn_factor example is from an illustrative eaggl-capped-v1 generation.')
    example('/v1/mechanisms/{source_id}','kpn_factor',kpn,exchange=False)
    problem('/v1/mechanisms/{source_id}','get',410,'REFERENCE_GENERATION_SUPERSEDED','This factor belongs to a superseded reference generation.',archived_reference_factor=archived)
    e['kpn_factor']=kpn
    for p in op('/v1/mechanisms/search')['parameters']:
        if p['name']=='model':
            p['schema']=deepcopy(b.SCHEMAS['EagglFactor']['properties']['model'])
            p['description']='EAGGL reference model. Results always come from the active reference generation (cfde-inc-v2 until a reference reload, eaggl-capped-v1 after one); this value only scopes the pagination cursor.'
    describe('/v1/mechanisms/search','get',' Results come from the active reference generation: after a reference reload, eaggl-capped-v1 KPN factors replace the mapped cfde-inc-v2 factors.')
    describe('/v1/mechanisms/suggest','post',' Suggestions come from the active reference generation; manual anchors from a superseded generation return 409 REFERENCE_GENERATION_SUPERSEDED.')
    problem('/v1/mechanisms/suggest','post',409,'REFERENCE_GENERATION_SUPERSEDED','A kept anchor belongs to a superseded reference generation; remove it and select current factors.')
    describe('/v1/drafts','post','',old='cfde-inc-v2 and both initial KGs',new='the active reference model (cfde-inc-v2 until a reference reload) and both initial KGs')
    for path,method in [('/v1/drafts','post'),('/v1/drafts/{draft_id}','patch')]:
        describe(path,method,' New or changed EAGGL anchors must come from the active reference generation (409 REFERENCE_GENERATION_SUPERSEDED) and wait while a reference reload is in progress (503 REFERENCE_RELOAD_IN_PROGRESS). Unchanged current anchors and gap-only edits are not blocked.')
        problem(path,method,409,'REFERENCE_GENERATION_SUPERSEDED','This anchor belongs to a superseded reference generation; select current factors.')
        problem(path,method,503,'REFERENCE_RELOAD_IN_PROGRESS',reload)
    describe('/v1/jobs','post',' Analysis anchors must belong to the active reference generation (409 REFERENCE_GENERATION_SUPERSEDED: start a new analysis on the gap with current factors), and analysis submission waits while a reference reload is in progress (503 REFERENCE_RELOAD_IN_PROGRESS). Paragraph jobs, including on archived accounts, are not affected.')
    problem('/v1/jobs','post',409,'REFERENCE_GENERATION_SUPERSEDED','This draft uses factors from a superseded reference generation; start a new analysis on this gap with current factors.')
    problem('/v1/jobs','post',503,'REFERENCE_RELOAD_IN_PROGRESS',reload)
    describe('/v1/jobs/{job_id}/retry-review','post',' An analysis frozen on a superseded reference generation cannot retry review (409 REFERENCE_GENERATION_SUPERSEDED: start a new analysis on the gap with current factors). Analysis review retry waits while a reference reload is in progress (503 REFERENCE_RELOAD_IN_PROGRESS).')
    problem('/v1/jobs/{job_id}/retry-review','post',409,'REFERENCE_GENERATION_SUPERSEDED','This analysis used a superseded reference generation. Start a new analysis on this gap with current factors.')
    problem('/v1/jobs/{job_id}/retry-review','post',503,'REFERENCE_RELOAD_IN_PROGRESS','Reference data is being reloaded. Retry review shortly.')
    describe('/v1/accounts/{dapper_id}','get',' An account built on a superseded reference generation carries archive (public copies null its job and request ids); it stays readable, downloadable and publishable, and paragraph jobs still run.')
    example('/v1/accounts/{dapper_id}','archived',dict(deepcopy(e['account']),archive=R['account']))
    describe('/v1/research-requests/{request_id}','get',' A request frozen on a superseded reference generation carries archive.')
    example('/v1/research-requests/{request_id}','archived',dict(deepcopy(e['research_request']),archive=R['request']))
    describe('/v1/knowledge-gaps','get',' Counts include current accounts only: accounts archived by a reference reload stay listed but are not counted.')
