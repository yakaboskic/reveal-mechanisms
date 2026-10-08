"""Small immutable inputs for both hosted and local progressive research.

Preparation is frozen-input assembly, not evidence collection. It deliberately
does not import or call either eager collector, a catalog or a network client.
"""
from copy import deepcopy
from pathlib import Path
import re

from .dispatch_view import CLAIM_STRUCTURE_PATH, KIT_V3
from .evidence_package import BuiltPackage, PACKAGE_VERSION, canonical_json, decode, require, sha256
from .reference_generation import generation_of_anchors
from .research_data import capability_catalog as default_catalog

SEED_VERSION = 'reveal.research-seed/1'
KIT_FILES = (
    'services/backend/agent-skills/construct-scientific-account/SKILL.md',
    'docs/evidence-package.md',
    'docs/authoring-contract.md',
    'docs/local-agent-mcp.md',
    'docs/local-workspaces-and-authentication.md',
    'docs/scientific-account-construction.md',
    'docs/pigean-claim-model.md',
    'docs/dapper-integration.md',
    'docs/agent-evidence-integration.md',
    'services/backend/agent-skills/read-evidence-package/SKILL.md',
    CLAIM_STRUCTURE_PATH,
    'services/backend/agent-runtime/dapper-release.json',  # Last: the kit's release lock.
)
KIT_IMPLEMENTATION = (
    'services/backend/agent-runtime/authoring-schema-dependencies.json',
    'services/backend/agent-runtime/linkml-types-1.11.1.yaml',
    'scripts/lint_scientific_account.py',
    'services/backend/src/reveal_backend/__init__.py',
    'services/backend/src/reveal_backend/evidence_package.py',
    'services/backend/src/reveal_backend/evidence_files.py',
    'services/backend/src/reveal_backend/evidence_reader.py',
    'services/backend/src/reveal_backend/dapper_release.py',
    'services/backend/src/reveal_backend/scientific_account_lint.py',
    'services/backend/src/reveal_backend/source_validation.py',
    'services/backend/src/reveal_backend/relationship_provenance.py',
    'services/backend/src/reveal_backend/claim_suggestions.py',
    'docs/scientific-account-linting.md',
)
PREFIXES = {'factor':'urn:cfde:factor:', 'gene':'urn:cfde:gene:', 'gene_set':'urn:cfde:gene_set:',
            'trait':'urn:cfde:trait:', 'cfde':'urn:cfde:record:'}
# Server-declared prefixes of the recommended claim triples: captured gene symbols (as CFDE GeneSet members) and Biolink predicates.
CLAIM_PREFIXES = {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:', 'biolink': 'https://w3id.org/biolink/vocab/'}


def validate_seed_shape(package, source_root=None):
    """Validate progressive seed invariants without claiming eager completeness.

    This is the dispatch schema branch for immutable initial seeds. With
    ``source_root`` it also checks every declared source's exact stored bytes.
    DAPPER identity validation remains the pinned runtime's responsibility.
    """
    require(isinstance(package, dict), 'Research seed must be an object')
    require(package.get('package_version') == PACKAGE_VERSION and package.get('seed_version') == SEED_VERSION
            and package.get('retrieval_mode') == 'progressive', 'Unsupported research seed version or retrieval mode')
    digest = lambda value: isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
    require(digest(package.get('reference_generation_id')), 'Research seed requires a pinned reference generation')
    require(isinstance(package.get('research_request_id'), str) and bool(package['research_request_id']), 'Research seed requires a frozen request')
    require(len(canonical_json(package)) <= 2_000_000, 'Research seed exceeds byte budget')
    for name in ('selection','dismech','pigean','entities','dapper_context','source_artifacts','coverage',
                 'policy','external_evidence','authoring','authoring_kit','readiness','dapper_pin','capabilities'):
        require(isinstance(package.get(name), dict), 'Missing research seed section: '+name)
    pin=package['dapper_pin']
    require(digest(pin.get('snapshot_sha256')) and digest(pin.get('snapshot_manifest_sha256')), 'Research seed requires exact DAPPER pins')
    readiness=package['readiness']
    require(readiness.get('seed_ready') is True and readiness.get('input_capture_complete') is False,
            'Progressive seed must explicitly declare incomplete capture')
    require('progressive_data_not_requested' in readiness.get('capture_blockers', []), 'Progressive seed must declare unrequested data')
    require(package['coverage'].get('mode') == 'seed_only', 'Progressive seed coverage must be seed_only')
    selection=package['selection']; context=package['dapper_context']
    gaps=context.get('knowledge_gaps', [])
    require(isinstance(gaps, list) and len(gaps)==1 and gaps[0].get('id')==selection.get('knowledge_gap_id')
            and gaps[0].get('id')==package['authoring'].get('required_question'), 'Research seed question binding changed')
    anchors=selection.get('eaggl_mechanism_ids', [])
    require(isinstance(anchors, list) and 1 <= len(anchors) <= 10 and all(isinstance(x,str) for x in anchors)
            and len(set(anchors))==len(anchors), 'Research seed requires distinct selected factors')
    mechanisms=package['pigean'].get('mechanisms', {})
    require(isinstance(mechanisms,dict) and set(mechanisms)==set(anchors), 'Research seed factor bindings changed')
    context_mechanisms={x.get('id') for x in context.get('mechanisms', [])}
    require(all(x.get('dapper_id') in context_mechanisms and x.get('fit',{}).get('upstream_build')==package['reference_generation_id']
                for x in mechanisms.values()), 'Research seed factors are not bound to the pinned generation')
    require(package['pigean'].get('candidates')=={} and package['pigean'].get('graph',{}).get('edges')==[],
            'Initial seed must not present unqueried candidates or edges')
    nodes=context.get('files', [])
    require(isinstance(nodes,list) and all(isinstance(node,dict) and isinstance(node.get('id'),str) for node in nodes),
            'Research seed File inventory is invalid')
    file_nodes={node['id']:node for node in nodes}
    require(len(file_nodes)==len(nodes), 'Research seed has duplicate File identities')
    sources=package['source_artifacts']
    require(bool(sources), 'Research seed source manifest is empty')
    for key,artifact in sources.items():
        require(isinstance(artifact,dict) and isinstance(key,str), 'Research seed source manifest is invalid')
        checksum=artifact.get('sha256'); size=artifact.get('size_bytes'); path=artifact.get('path')
        require(digest(checksum) and type(size) is int and size >= 0, 'Research seed source checksum or size is invalid')
        require(artifact.get('format') in ('json','text','binary') and path=='sources/'+checksum+'.'+artifact['format'],
                'Research seed source path is not checksum addressed')
        node=file_nodes.get(artifact.get('dapper_file_id'),{})
        require(node.get('sha256')==checksum and node.get('size_in_bytes')==size
                and node.get('filename')==artifact.get('filename') and node.get('mime_type')==artifact.get('media_type'),
                'Research seed source differs from its authoritative File')
        if source_root is not None:
            root=Path(source_root).resolve(); target=(root/path).resolve()
            require(target.is_relative_to(root), 'Research seed source escapes its package')
            require(target.is_file(), 'Research seed source bytes are missing')
            raw=target.read_bytes()
            require(len(raw)==size and sha256(raw)==checksum, 'Research seed source bytes changed')
    for name in ('eligible_source_ids','cfde_source_ids'):
        require(isinstance(package.get(name),list) and set(package[name]) <= set(file_nodes), 'Research seed source inventory has unknown Files')
    require(package['cfde_source_ids']==[], 'Initial seed has no captured CFDE scientific evidence')
    kit=package['authoring_kit']; entries=kit.get('files')
    require(isinstance(entries,list) and bool(entries) and kit.get('kit_sha256')==sha256(canonical_json(entries)), 'Research authoring kit manifest changed')
    require(all(isinstance(item,dict) and item.get('artifact_id') in sources
                and item.get('sha256')==sources[item['artifact_id']]['sha256'] for item in entries), 'Research authoring kit source binding changed')
    from .dispatch_view import pinned_claim_structure_sha256, pinned_skeleton_sha256
    pinned_skeleton_sha256(package); pinned_claim_structure_sha256(package)


def prepare_research_seed(frozen, binding, *, dapper, project_root, output=None,
                          capability_catalog=None, read_upload=None, max_accounts=3):
    """Return a package-compatible BuiltPackage, publishing only complete bytes.

    `frozen` and `binding` are the existing request/request_binding records. Their
    owner and retained generation pin are checked by the orchestrating service.
    `read_upload` reads an already authorized immutable upload storage reference.
    """
    root = Path(project_root)
    require(1 <= max_accounts <= 3, 'A seed allows one to three accounts per submission')
    generation = generation_of_anchors(binding['anchors'])
    require(generation is not None, 'Research seed requires an exact reference generation')
    anchors = deepcopy(binding['anchors'])
    factor_ids = [a['cfde_node_id'] for a in anchors]
    require(0 < len(factor_ids) <= 10 and len(set(factor_ids)) == len(factor_ids), 'Supply one to ten distinct frozen factors')
    context = deepcopy(frozen['document'])
    require(context.get('knowledge_gaps') == [binding['source_gap']['object']], 'Frozen request and gap binding differ')
    require(context['knowledge_gaps'][0]['id'] == frozen['question_id'], 'Frozen question identity changed')
    require(len(context.get('mechanisms', [])) == len(factor_ids), 'Frozen factor objects are missing')
    prefixes = {**PREFIXES, **CLAIM_PREFIXES, **context.get('prefixes', {})}
    context['prefixes'] = prefixes
    dapper.validate(context)
    context.setdefault('files', [])
    sources, files, kit = {}, {}, []
    eligible = []

    def capture(key, raw, filename, format='json', origin=None, media_type=None, private=False):
        media_type = media_type or {'json':'application/json','text':'text/plain','binary':'application/octet-stream'}[format]
        node = dapper.file(filename, raw, media_type)
        path = 'sources/' + sha256(raw) + '.' + format
        files[path] = raw
        if node['id'] not in {item['id'] for item in context['files']}: context['files'].append(node)
        sources[key] = {'path':path,'filename':filename,'sha256':sha256(raw),'size_bytes':len(raw),
                        'format':format,'media_type':media_type,'dapper_file_id':node['id'],
                        'origin':origin or 'reveal:frozen-research-context','private':private}
        return {'artifact_id':key,'pointer':''}

    # Source excerpts and selection are retained exactly. They remain context,
    # not a claim that a complete DisMech checkout or reference model was read.
    gap_record = deepcopy(binding['source_gap'])
    gap_ref = capture('frozen-gap', canonical_json(gap_record), 'frozen-gap.json',
                      origin='reveal:frozen-dismech-selection')
    gap_ref['pointer'] = '/source_detail/raw' if gap_record.get('source_detail', {}).get('raw') is not None else '/object'
    # Preserve the original detailed audit, including embeddings, as one opt-in
    # artifact. The default scientific selection has only bounded reasons and pins.
    selection_audit = {'anchors': anchors, 'document': frozen['document'],
        'retrieval': binding.get('retrieval', {}), 'dismech_import_id': binding.get('dismech_import_id')}
    audit_ref = capture('frozen-selection-audit', canonical_json(selection_audit),
        'frozen-selection-audit.json', origin='reveal:frozen-reference-selection-audit')
    summary = {}
    for identity, retrieval in binding.get('retrieval', {}).items():
        if not isinstance(retrieval, dict): continue
        hit = retrieval.get('hit', {})
        summary[identity] = {key: deepcopy(hit[key]) for key in ('ranking', 'matched_context_ids', 'reason') if key in hit}
    selection_ref = capture('frozen-selection', canonical_json({'anchors': anchors,
        'document': frozen['document'], 'retrieval_summary': summary,
        'audit': {**audit_ref, 'sha256': sources[audit_ref['artifact_id']]['sha256']},
        'dismech_import_id': binding.get('dismech_import_id')}),
        'frozen-selection.json', origin='reveal:frozen-reference-selection')
    dismech_mechanisms = {}
    pinned_context = binding.get('pinned_dismech_context', [])
    require(len(pinned_context) <= 10, 'Frozen DisMech attachment budget exceeded')
    selected_context = {item['source_id']: item for item in frozen.get('linked_dismech_context', [])}
    for number, record in enumerate(pinned_context):
        selected = selected_context.get(record.get('source_id'))
        require(selected is not None and selected['source_revision'] == record.get('source_revision'),
                'Frozen DisMech attachment revision changed')
        detail = record.get('source_detail', {})
        raw = detail.get('raw')
        if raw is None: continue
        require(detail.get('payload_sha256') == sha256(canonical_json(raw)), 'Frozen DisMech attachment bytes changed')
        source = capture('dismech-attachment-'+str(number), canonical_json(raw),
            'dismech-attachment-'+str(number)+'.json', origin='reveal:pinned-dismech-attachment')
        eligible.append(sources[source['artifact_id']]['dapper_file_id'])
        body = raw.get('raw', raw)
        text = str(body.get('description') or raw.get('description') or '')
        # Preserve evidence/curator fields in the exact artifact; do not invent
        # fields absent from the importer or cut a structured reference in half.
        available = [key for key in ('evidence', 'notes', 'qualifications', 'curators', 'conforms_to') if key in body]
        dismech_mechanisms[record['source_id']] = {
            'source_id': record['source_id'], 'source_revision': record['source_revision'],
            'source_ref': source, 'source_file': detail.get('source_file'),
            'source_pointer': detail.get('source_pointer'), 'source_commit': detail.get('source_commit'),
            'import_id': detail.get('import_id'), 'name': raw.get('name', record.get('object', {}).get('name')),
            'description_excerpt': text[:4000], 'truncated': len(text) > 4000,
            'qualifications_excerpt': {key: {'text': str(body[key])[:1000], 'truncated': len(str(body[key])) > 1000,
                'source_ref': {**source, 'pointer': ('/raw' if 'raw' in raw else '')+'/'+key}}
                for key in ('notes', 'qualifications', 'curators', 'conforms_to') if key in body},
            'available_detail_fields': available, 'evidence_refs': [
                {'reference': str(item.get('reference', ''))[:300], 'supports': str(item.get('supports', ''))[:100],
                 'source_ref': {**source, 'pointer': ('/raw' if 'raw' in raw else '')+'/evidence/'+str(i)}}
                for i, item in enumerate(body.get('evidence', [])[:8]) if isinstance(item, dict)],
            'evidence_refs_truncated': len(body.get('evidence', [])) > 8,
            'upstream_detail': 'Only fields preserved in this imported pathophysiology record are available; absent curator qualifications or evidence details are unknown.'}
    for number, relative in enumerate(KIT_FILES):
        raw = (root / relative).read_bytes()
        capture('instruction-'+str(number),raw,Path(relative).name,'text',origin=relative)
        kit.append({'artifact_id':'instruction-'+str(number),'path':relative,'sha256':sha256(raw)})
    kit_by_path = {item['path']: item for item in kit}
    lock = decode((root / KIT_FILES[-1]).read_bytes())
    require(dapper.manifest['snapshot_sha256'] in lock['compatible_input_snapshots'], 'Seed DAPPER snapshot is incompatible with authoring release')
    implementation=[]
    for number,relative in enumerate(KIT_IMPLEMENTATION):
        raw=(root/relative).read_bytes()
        key='authoring-implementation-'+str(number)
        capture(key,raw,Path(relative).name,'text',origin=relative)
        implementation.append({'artifact_id':key,'path':relative,'sha256':sha256(raw)})

    from .authoring_contract import pinned_schema, SCHEMA_PATH, EXAMPLE_PATH, SKELETON_PATH
    schema_raw = pinned_schema(root)
    for key, target, raw in (
        ('authoring-schema', SCHEMA_PATH, schema_raw),
        ('authoring-examples', EXAMPLE_PATH, (root/'services/backend/agent-runtime/authoring-examples.json').read_bytes()),
        ('authoring-skeleton', SKELETON_PATH, (root/'services/backend/agent-runtime/authoring-skeleton.json').read_bytes()),
    ):
        capture(key, raw, Path(target).name, 'text', origin='reveal:pinned-authoring-schema' if key == 'authoring-schema' else 'reveal:synthetic-authoring-contract')
        implementation.append({'artifact_id': key, 'path': target, 'sha256': sha256(raw)})
        files[target.removeprefix('input/')] = raw

    user_inputs = deepcopy(frozen.get('user_inputs'))
    if user_inputs is not None:
        if user_inputs.get('uploads') and read_upload is None:
            from .user_inputs import read
            read_upload = read
        for upload in user_inputs.get('uploads',[]):
            original = read_upload(upload['storage']); extracted = read_upload(upload['extraction']['storage'])
            require(sha256(original)==upload['sha256'], 'Frozen attachment bytes changed')
            require(sha256(extracted)==upload['extraction']['storage']['sha256'], 'Frozen extraction bytes changed')
            content=decode(extracted)
            require(content.get('original_sha256')==sha256(original), 'Extraction belongs to a different attachment')
            key='user-upload-'+upload['id']
            capture(key,original,upload['filename'],'binary',media_type=upload['media_type'],private=True)
            capture(key+'-text',extracted,upload['id']+'-text.json',private=True)
            upload.update(original_artifact_id=key,extraction_artifact_id=key+'-text',
                original_file_id=sources[key]['dapper_file_id'],extraction_file_id=sources[key+'-text']['dapper_file_id'])
            # The server-side request retains private storage locators. The seed
            # exposes only checksum-addressed artifacts and bounded metadata.
            upload.pop('storage',None)
            upload['extraction'].pop('storage',None)
            upload.pop('content',None)
            upload['extraction'].update(sha256=sha256(extracted),size_bytes=len(extracted))
            eligible.append(sources[key+'-text']['dapper_file_id'])

    model = anchors[0].get('model') or factor_ids[0].split(':')[3]
    mechanisms={}
    for number,(anchor,node) in enumerate(zip(anchors,context['mechanisms'])):
        parts=anchor['cfde_node_id'].split(':')
        require(len(parts)==5 and parts[3]==model,'Frozen anchors have inconsistent source models')
        mechanisms[anchor['cfde_node_id']]={
            'dapper_id':node['id'],'source_label':node.get('name',''),
            'source_ref':{'artifact_id':selection_ref['artifact_id'],'pointer':'/document/mechanisms/'+str(number)},
            'fit':{'trait_group':parts[1],'phenotype':parts[2],'model':parts[3],'factor':parts[4],
                   'trait_id':'trait:'+parts[1]+':'+parts[2],'upstream_build':generation},
            **{kind+'_loadings':{'status':'not_requested','query_status':'not_requested','items':{}} for kind in ('gene','gene_set','trait')},
        }
    composer=frozen['composer']
    origins={a['reference']['source_id']:a['origin'] for a in composer['eaggl_anchors']}
    catalog=deepcopy(capability_catalog) if capability_catalog is not None else default_catalog(
        {'generation_id':generation,'model':model,'status':'pinned','eaggl_import_id':anchors[0].get('eaggl_import_id')})
    package={
        'package_version':PACKAGE_VERSION,'seed_version':SEED_VERSION,'retrieval_mode':'progressive',
        'research_request_id':frozen['id'],'reference_generation_id':generation,
        'prefixes':prefixes,'identifier_policy':{'preserve_existing_dapper_payloads':True,
            'source_local_prefixes':list(PREFIXES),'dismech_record_ids':'Frozen source aliases; resolve through exact source bindings.'},
        'builder':{'version':SEED_VERSION,'source_sha256':sha256(Path(__file__).read_bytes())},
        'dapper_pin':{'name':'configured-snapshot','snapshot_sha256':dapper.manifest['snapshot_sha256'],
                      'snapshot_manifest_sha256':dapper.manifest_sha256},
        'selection':{'knowledge_gap_id':frozen['question_id'],'eaggl_mechanism_ids':factor_ids,
            'dismech_mechanism_ids':[x['source_id'] for x in frozen.get('linked_dismech_context',[])],
            'origins':origins,'dismissed_eaggl_ids':composer.get('dismissed_source_ids',[]),
            'semantic_retrieval':{'status':'frozen_selection_only','binding_artifact_id':selection_ref['artifact_id']},
            'expansion_policy':{'mode':'progressive','rounds':0}},
        'dismech':{'source_revision':{'source_sha256':composer['source_gap']['source_revision'],
                                     'import_id':binding.get('dismech_import_id')},
            'knowledge_gap':{'source_id':composer['source_gap']['source_id'],'source_ref':gap_ref,
                'attachments':deepcopy(gap_record.get('attachments',[]))},
            'mechanisms':dismech_mechanisms,'other_context':[],'related_knowledge_gaps':[],
            'attachment_status': {identity: ('pinned_record' if identity in dismech_mechanisms else 'record_not_frozen')
                for identity in selected_context},
            'coverage':'Pinned selected attachment records with bounded excerpts; exact imported records are available through source_ref. Unpinned historical attachments remain reference-only; full documents were not collected.'},
        'pigean':{'model':model,'mechanisms':mechanisms,'traits':{},'graph':{'node_ids':factor_ids,'edges':[]},
                  'contextual_relationships':{'status':'not_requested','returned_edges':0,'new_unique_edges':0},'candidates':{}},
        'entities':{'genes':{},'gene_sets':{}},'dapper_context':context,'source_artifacts':sources,
        'eligible_source_ids':eligible,'cfde_source_ids':[],
        'capabilities':catalog,
        'coverage':{'mode':'seed_only','queries':{k:'not_requested' for k in ('gene','gene_set','trait','factor','contextual')},
            'bioindex_queries':[],'captured_nodes':len(factor_ids),'captured_unique_edges':0,
            'retained_nodes':len(factor_ids),'retained_edges':0,'omitted_node_ids':[],'omitted_edge_ids':[],
            'ranking':'No candidate retrieval performed','upstream_total':None,'same_upstream_build_verified':False},
        'policy':{'max_nodes':250,'max_edges':1000,'max_anchors':10,'max_dismech_mechanisms':10,
            'max_candidates_per_target':100,'max_package_bytes':2_000_000,'retain_node_ids':factor_ids,'allow_incomplete_capture':True},
        'external_evidence':{'status':'not_queried','selected_graphs':composer['selected_kgs'],'ledger':[],'assertions':[]},
        'authoring':{'skill':kit_by_path['services/backend/agent-skills/construct-scientific-account/SKILL.md'],
            'contract':kit_by_path['docs/authoring-contract.md'],
            'references':[item for item in kit if item['path'] not in {
                'services/backend/agent-skills/construct-scientific-account/SKILL.md',
                'docs/authoring-contract.md', 'services/backend/agent-runtime/dapper-release.json'}] +
                [next(item for item in implementation if item['path'] == SKELETON_PATH)],
            'required_question':frozen['question_id'],'max_accounts':max_accounts,
            'assembly_builder':{'version':SEED_VERSION,'source_sha256':sha256(Path(__file__).read_bytes())}},
        'authoring_kit':{'version':KIT_V3,'files':kit+implementation,
            'kit_sha256':sha256(canonical_json(kit+implementation)),
            'release_lock_sha256':sha256((root/KIT_FILES[-1]).read_bytes()),'release_tag':lock['tag'],'release_commit':lock['commit']},
        'readiness':{'input_capture_complete':False,'seed_ready':True,'capture_blockers':['progressive_data_not_requested'],
            'agent_dispatch_validated':False,'remaining_checks':['runtime and trusted attribution','worker authorization and tool policy']},
    }
    if user_inputs is not None: package['user_inputs']=user_inputs
    dapper.validate(context)
    validate_seed_shape(package)
    raw=canonical_json(package)
    require(len(raw)<=2_000_000,'Research seed exceeds byte budget')
    files['evidence-package.json']=raw
    from .evidence_files import build_evidence_index
    files['evidence-index.json']=build_evidence_index(raw)
    manifest={'builder_version':SEED_VERSION,'seed_version':SEED_VERSION,'retrieval_mode':'progressive',
        'research_request_id':frozen['id'],'reference_generation_id':generation,'package_sha256':sha256(raw),
        'files':{name:sha256(data) for name,data in sorted(files.items())}}
    files['manifest.json']=canonical_json(manifest)
    built=BuiltPackage(package,files,manifest)
    if output is not None:
        destination = Path(output)
        # Callers commonly own an empty TemporaryDirectory already. Removing
        # that empty shell lets BuiltPackage publish its complete bundle by rename.
        if destination.is_dir() and not any(destination.iterdir()): destination.rmdir()
        built.write(destination)
    return built
