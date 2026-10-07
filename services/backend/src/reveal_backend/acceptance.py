"""Trusted assembly, identity minting and final acceptance (outside the agent)."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from functools import lru_cache
from .runtime_config import ROOT, CURRENT_DAPPER_SNAPSHOT, setting
from .repository import digest, now
from .evidence_package import canonical_json, decode, require, sha256
from .dapper_release import verify_release
from . import dapper_helper
from .scientific_account_lint import validate_scientific_account
from .source_validation import ledger_sources, validate_new_files, validate_observations

LOCK=ROOT/'services/backend/agent-runtime/dapper-release.json'


def build_validation_context(seed, *, contexts=(), source_artifacts=None):
    """Merge server-resolved observations without changing the frozen seed.

    Contexts are produced by authorized data/import/reuse services, never taken
    from an agent's submitted JSON. Their byte maps are materialized by the
    caller beside the resulting package before assembly/linting.
    """
    result = deepcopy(seed)
    require(isinstance(result.get('selection'), dict) and result['selection'].get('knowledge_gap_id'),
            'Validation context requires the frozen selected gap')
    result.setdefault('source_artifacts', {})
    result.setdefault('dapper_context', {})
    previous = result.get('validation_context', {})
    eligible = set(result.get('eligible_source_ids', [])) | set(previous.get('eligible_source_ids', []))
    cfde = set(result.get('cfde_source_ids', [])) | set(previous.get('cfde_source_ids', []))
    nodes = {node['id']: (group, node) for group, rows in result['dapper_context'].items()
             if isinstance(rows, list) for node in rows if isinstance(node, dict) and 'id' in node}
    for context in contexts:
        for artifact in context.get('source_artifacts', {}).values():
            capture = artifact.get('research_capture', {})
            if (context.get('source_ref') and 'artifact_records' not in context
                    and capture.get('generation_id') and result.get('reference_generation_id')):
                require(capture['generation_id'] == result['reference_generation_id'],
                        'Retained observation belongs to a different frozen reference generation')
        for resolution in context.get('reference_objects', context.get('object_resolution', [])):
            if resolution.get('reference_generation_id') and result.get('reference_generation_id'):
                require(resolution['reference_generation_id'] == result['reference_generation_id'],
                        'Resolved scientific object belongs to a different frozen reference generation')
            target = result.setdefault('reference_objects', [])
            if resolution not in target: target.append(deepcopy(resolution))
        for group, rows in context.get('dapper_context', {}).items():
            if group == 'prefixes':
                prefixes = result['dapper_context'].setdefault('prefixes', {})
                for prefix, value in rows.items():
                    require(prefix not in prefixes or prefixes[prefix] == value, 'Conflicting source prefix binding')
                    prefixes[prefix] = value
                continue
            require(isinstance(rows, list), 'Context document groups must be lists')
            target = result['dapper_context'].setdefault(group, [])
            for node in rows:
                require(isinstance(node, dict), 'Context objects must be mappings')
                identity = node.get('id')
                if identity in nodes:
                    require(nodes[identity] == (group, node), 'Conflicting retained scientific payload')
                    continue
                if node not in target:
                    target.append(deepcopy(node))
                if identity:
                    nodes[identity] = (group, node)
        for key, artifact in context.get('source_artifacts', {}).items():
            prior = result['source_artifacts'].get(key)
            require(prior is None or prior == artifact, 'Conflicting retained source artifact')
            result['source_artifacts'][key] = deepcopy(artifact)
        eligible.update(context.get('eligible_source_ids', []))
        cfde.update(context.get('cfde_source_ids', []))
        for upload in context.get('user_inputs', {}).get('uploads', []):
            uploads = result.setdefault('user_inputs', {}).setdefault('uploads', [])
            if upload not in uploads:
                uploads.append(deepcopy(upload))
    for key, artifact in (source_artifacts or {}).items():
        prior = result['source_artifacts'].get(key)
        require(prior is None or prior == artifact, 'Conflicting retained source artifact')
        result['source_artifacts'][key] = deepcopy(artifact)
    captured = {artifact['dapper_file_id'] for artifact in result['source_artifacts'].values()}
    require((eligible | cfde) <= captured, 'Eligible evidence must have retained source bytes')
    for artifact in result['source_artifacts'].values():
        entry = nodes.get(artifact['dapper_file_id'])
        require(entry is not None and entry[0] == 'files', 'Source artifact lacks its canonical File')
        require(entry[1].get('sha256') == artifact['sha256'], 'Source File checksum differs from retained bytes')
        if 'size_bytes' in artifact:
            require(entry[1].get('size_in_bytes') == artifact['size_bytes'], 'Source File size differs from retained bytes')
    result['validation_context'] = {'format': 'reveal.validation-context/1',
        'acceptance_policy': 'reveal.scientific-account/2', 'seed_sha256': sha256(canonical_json(seed)),
        'eligible_source_ids': sorted(eligible | cfde), 'cfde_source_ids': sorted(cfde)}
    return result

def release_root(): return Path(setting('REVEAL_DAPPER_ROOT',str(ROOT/'.runtime/dapper')))

@lru_cache(maxsize=1)
def public_runtime():
    from .evidence_package import DapperRuntime
    return DapperRuntime(CURRENT_DAPPER_SNAPSHOT)

def mint(document,path):
    root=release_root(); release=verify_release(root,LOCK)
    path.write_bytes(canonical_json(document))
    if dapper_helper.enabled():
        ok,detail=dapper_helper.request(root,release,'mint',{'path':os.path.abspath(path)},120)
        require(ok,'Trusted DAPPER identity assembly failed: '+str(detail)[-1500:])
        return decode(path.read_bytes())
    program='''import json,sys
from pathlib import Path
root=Path(sys.argv[1]); sys.path[:0]=[str(root/'schema/identity'),str(root/'schema')]
from dapper_identity import assign_ids,load_schema
path=Path(sys.argv[2]); document=json.loads(path.read_text())
assign_ids(document,load_schema(root/'schema/dapper.yaml'))
path.write_text(json.dumps(document,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\\n')
'''
    result=subprocess.run([sys.executable,'-I','-B','-c',program,str(root),str(path)],capture_output=True,text=True,timeout=120)
    require(result.returncode==0,'Trusted DAPPER identity assembly failed: '+result.stderr[-1500:])
    return decode(path.read_bytes())

def replace_authored_attribution(document,trusted,person,activity):
    """Replace guessed runtime/actor declarations, preserving exact source nodes."""
    replacements={}
    for group,target in (('persons',person['id']),('organizations',person['id']),('activities',activity['id'])):
        for node in document.get(group,[]):
            if node['id'] not in trusted: replacements[node['id']]=target
        document[group]=[node for node in document.get(group,[]) if node['id'] in trusted]
    def rewrite(value):
        if isinstance(value,dict): return {key:rewrite(child) for key,child in value.items()}
        if isinstance(value,list): return [rewrite(child) for child in value]
        return replacements.get(value,value) if isinstance(value,str) else value
    for group,rows in document.items():
        if isinstance(rows,list):
            document[group]=[node if isinstance(node,dict) and node.get('id') in trusted else rewrite(node) for node in rows]
    document.setdefault('persons',[]).append(person)
    document.setdefault('activities',[]).append(activity)
    return document


@lru_cache(maxsize=4)
def _reference_fields(root, lock_sha256, commit=None):
    """Use the same pinned schema as lint, isolated from imported catalog schemas."""
    if commit and dapper_helper.enabled():
        ok,fields=dapper_helper.request(root,{'lock_sha256':lock_sha256,'commit':commit},'reference_fields',{},120)
        require(ok,'Trusted reference schema could not be loaded: '+str(fields)[-1500:])
        return fields
    program='''import json,sys,yaml
from pathlib import Path
schema=Path(sys.argv[1])/'schema'
sys.path[:0]=[str(schema/'lint'),str(schema/'identity'),str(schema)]
from dapper_identity import load_schema
from lint_provenance import Vocabulary
sv=load_schema(schema/'dapper.yaml')
vocab=Vocabulary.build(sv,yaml.safe_load((schema/'lint/profiles.yaml').read_text()))
fields={group:sorted(vocab.relationship_slots.get(cls,{})) for group,cls in vocab.node_groups.items()}
fields.update({group:['subject','object'] for group in vocab.edge_groups})
print(json.dumps(fields))
'''
    result=subprocess.run([sys.executable,'-I','-B','-c',program,root],capture_output=True,text=True,timeout=120)
    require(result.returncode==0,'Trusted reference schema could not be loaded: '+result.stderr[-1500:])
    return decode(result.stdout.encode())


def hydrate_inputs(document,trusted,edges=()):
    """Hydrate exact schema-declared references, never IDs mentioned in prose."""
    root=release_root(); release=verify_release(root,LOCK)
    fields=_reference_fields(str(root.resolve()),release['lock_sha256'],release.get('commit'))
    for _ in range(len(trusted)+1):
        present={node['id'] for rows in document.values() if isinstance(rows,list)
                 for node in rows if isinstance(node,dict) and 'id' in node}
        for group, edge in edges:
            if edge.get('subject') in present and edge not in document.setdefault(group, []):
                document[group].append(deepcopy(edge))
        references=set()
        for group,names in fields.items():
            rows=document.get(group,[])
            for node in rows if isinstance(rows,list) else []:
                if not isinstance(node,dict): continue
                for name in names:
                    value=node.get(name,[])
                    references.update(item for item in (value if isinstance(value,list) else [value]) if isinstance(item,str))
        missing=[identity for identity in trusted if identity not in present and identity in references]
        if not missing: break
        for identity in missing:
            group,node=trusted[identity]; document.setdefault(group,[]).append(deepcopy(node))
    return document


def assemble_account(raw_path,package_path,output_path,attribution,job,attempt,execution,ledger_path=None):
    raw=Path(raw_path).read_bytes(); require(len(raw)<=4_000_000,'Account output exceeds byte limit')
    doc=decode(raw,'yaml' if Path(raw_path).suffix in ('.yaml','.yml') else 'json'); package=decode(Path(package_path).read_bytes())
    require(len(doc.get('scientific_accounts',[]))==1,'Each output must contain exactly one ScientificAccount')
    require(doc['scientific_accounts'][0].get('question')==package['selection']['knowledge_gap_id'],'Agent changed the selected KnowledgeGap')
    trusted={n['id']:(group,n) for group,rows in package['dapper_context'].items() if isinstance(rows,list) for n in rows if isinstance(n,dict) and 'id' in n}
    for group,rows in doc.items():
        if isinstance(rows,list):
            for node in rows:
                if isinstance(node,dict) and node.get('id') in trusted: require(node==trusted[node['id']][1],'Agent altered a trusted input payload')
    retained_edges=[(group,edge) for group,rows in package['dapper_context'].items()
                    if group.endswith('_edges') and isinstance(rows,list) for edge in rows]
    doc=hydrate_inputs(doc,trusted,retained_edges)
    # Operational attribution comes from the frozen submitting principal and
    # actual worker, never an agent-authored Person or runtime declaration.
    actor_id='urn:reveal:actor:'+digest(['scientific-actor',attribution['user_id']])
    label=attribution.get('display_name') if attribution['principal_kind']=='registered' else 'Anonymous researcher'
    person={'id':actor_id,'name':(label or 'Registered researcher')+' [actor '+digest(['scientific-actor',attribution['user_id']])[:20]+']'}
    if attribution.get('orcid_authenticated') and attribution.get('orcid'): person['orcid']=attribution['orcid']
    activity_id='urn:reveal:execution:'+job['id']+':'+str(attempt)
    local = execution in ('local', 'local-agent')
    activity={'id':activity_id,'name':'REVEAL local account submission' if local else
        'REVEAL deterministic development execution' if execution=='deterministic' else 'REVEAL Claude Code account construction',
        'command':('reveal-local-submit' if local else 'reveal-worker '+execution)+' '+job['id']+' --attempt '+str(attempt),
        'software_name':'REVEAL acceptance service' if local else 'REVEAL Mechanisms worker','software_version':'0.2.0',
        'generated_at_time':now()}
    doc=replace_authored_attribution(doc,trusted,person,activity)
    for group in ('claims','scientific_accounts'):
        for node in doc.get(group,[]):
            if node['id'] not in trusted: node['was_generated_by']=activity_id; node['was_attributed_to']=[actor_id]
    sources=sorted({ref for item in doc.get('evidence_items',[]) for ref in item.get('was_derived_from',[]) if ref in trusted})
    doc.setdefault('used_edges',[]).extend({'subject':activity_id,'predicate':'prov:used','object':ref} for ref in sources)
    document=mint(doc,output_path)
    # An authored reference may converge on an already retained scientific
    # identity (for example the same Proposition or submitting Person). Keep
    # one exact payload; never merge different observations under one identity.
    observed = {}
    for group, rows in document.items():
        if not isinstance(rows, list): continue
        unique = []
        for node in rows:
            identity = node.get('id') if isinstance(node, dict) else None
            if identity in observed:
                require(observed[identity] == (group, node), 'Conflicting scientific payloads share a minted identity')
                continue
            if identity: observed[identity] = (group, node)
            if node not in unique: unique.append(node)
        document[group] = unique
    Path(output_path).write_bytes(canonical_json(document))
    report=validate_scientific_account(output_path,dapper_root=release_root(),release_lock=LOCK,
        evidence_package=package_path,ledger_path=ledger_path)
    return document,report


def object_envelope(document,identity,metadata,artifact_access=None,*,max_depth=5,max_nodes=250,offset=0,continuation=None):
    return with_citations(object_projection(document,identity,artifact_access,max_depth=max_depth,max_nodes=max_nodes,
        offset=offset,continuation=continuation),metadata)

def with_citations(projection,metadata):
    """The envelope of an object_projection with the citation metadata of the objects it retains."""
    envelope,retained=projection
    return dict(envelope,citation_metadata=[m for m in metadata if m['target_id'] in retained])

def object_projection(document,identity,artifact_access=None,*,max_depth=5,max_nodes=250,offset=0,continuation=None):
    """(envelope without its citation metadata, ids it retains). Citation metadata is the only input acceptance
    reads under the write fence, so the costly projection can be made before taking it."""
    runtime=public_runtime()
    document,errors=runtime.transform(document,runtime.schema,runtime.groups,compact_dapper=True)
    require(not errors,'Public object projection contains unresolved identifiers')
    document.pop('prefixes',None)
    nodes={n['id']:(group,n) for group,rows in document.items() if isinstance(rows,list) for n in rows if isinstance(n,dict) and 'id' in n}
    require(identity in nodes,'Public object root is absent from its validated document')
    visited=set(); ordered=[]; frontier=[(identity,0)]; missing=set(); selected_edges=[]
    def refs(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if key!='id': yield from refs(child)
        elif isinstance(value,list):
            for child in value: yield from refs(child)
        elif isinstance(value,str) and value in nodes: yield value
    while frontier:
        current,depth=frontier.pop(0)
        if current in visited: continue
        if depth>max_depth: missing.add(current); continue
        visited.add(current); ordered.append(current)
        frontier.extend((ref,depth+1) for ref in sorted(set(refs(nodes[current][1]))) if ref not in visited)
        for group,rows in document.items():
            if group.endswith('_edges') and isinstance(rows,list):
                for edge in rows:
                    if edge.get('subject')==current:
                        selected_edges.append((group,edge)); target=edge.get('object')
                        if target in nodes and target not in visited: frontier.append((target,depth+1))
    # Every response preserves its terminal object as required by the typed
    # Account/Claim/Paragraph document. Continuation pages repeat that one node.
    upstream=[node for node in ordered if node!=identity]; capacity=max_nodes-1
    retained={identity}|set(upstream[offset:offset+capacity]); end=min(len(upstream),offset+capacity)
    missing=(missing-visited)|set(upstream[end:])
    # A collection node is kept intact even when its dependencies exceed bounds.
    # Missing authorized IDs disclose the limit without reminting its membership.
    projected={}
    for group,rows in document.items():
        if isinstance(rows,list):
            values=[n for n in rows if isinstance(n,dict) and n.get('id') in retained]
            values.extend(edge for edge_group,edge in selected_edges if edge_group==group and edge.get('subject') in retained and edge.get('object') in visited)
            if values: projected[group]=values
    document=projected
    missing-=retained
    raw=LOCK.read_bytes(); lock=decode(raw)
    return {'root_id':identity,'schema':{'schema_sha256':lock['files']['schema/dapper.yaml'],'dependency_snapshot_sha256':sha256(raw),'identity_profile':'DAPPER-ID-1'},
        'document':document,'payloads':[{'object_id':n['id'],'payload_sha256':sha256(canonical_json(n))} for rows in document.values() if isinstance(rows,list) for n in rows if isinstance(n,dict) and str(n.get('id','')).startswith('dapper:')],
        'citation_metadata':None,'artifacts':[(artifact_access or {}).get(f['id'],{'file':f,'download_url':None,'expires_at':None,'availability':'not_available','verification':'source_reported'}) for f in document.get('files',[])],
        'coverage':{'direction':'upstream','max_depth':max_depth,'max_nodes':max_nodes,'complete':not missing,'missing_ids':sorted(missing-retained),
            'next_cursor':continuation(end) if continuation and capacity and end<len(upstream) else None}},retained

def validate_paragraph_document(path):
    """Use pinned upstream lint with an application-owned terminal Paragraph profile."""
    release=verify_release(release_root(),LOCK)
    if dapper_helper.enabled():
        ok,findings=dapper_helper.request(release_root(),release,'lint_paragraph',{'path':os.path.abspath(path)},120)
        require(ok,'Paragraph linter runtime failed')
        return paragraph_report(findings)
    program='''import sys,json,yaml
from pathlib import Path
from dataclasses import asdict
root=Path(sys.argv[1]); schema=root/'schema'; sys.path[:0]=[str(schema/'lint'),str(schema/'identity'),str(schema)]
from lint_provenance import Vocabulary,build_validator,lint
from dapper_identity import load_schema
sv=load_schema(schema/'dapper.yaml'); profiles=yaml.safe_load((schema/'lint/profiles.yaml').read_text())
profiles['profiles']['reveal-paragraph']={'title':'Cited research statement','terminal':{'class':'Paragraph','min':1,'max':1},'required_edges':[{'group':'was_generated_by_edges','object_class':'Activity','min':1,'severity':'error','why':'Paragraph records its own generation activity.'}],'activities_require_inputs':'warning'}
vocabulary=Vocabulary.build(sv,profiles)
report=lint(Path(sys.argv[2]),vocabulary,sv,build_validator(schema/'dapper.yaml'),profile_name='reveal-paragraph')
print(json.dumps([asdict(x) for x in report.findings]))
'''
    result=subprocess.run([sys.executable,'-I','-B','-c',program,str(release_root()),str(path)],capture_output=True,text=True,timeout=120)
    require(result.returncode==0,'Paragraph linter runtime failed')
    return paragraph_report(json.loads(result.stdout))

def paragraph_report(findings):
    require(not any(x['severity']=='error' for x in findings),'Paragraph validation failed: '+str(findings)[:2000])
    return {'profile':'reveal-paragraph','valid':True,'findings':findings}

def prewarm():
    """Warm this process's DAPPER helper while a Box runs, so trusted assembly and linting start warm."""
    dapper_helper.prewarm(release_root(),LOCK)
