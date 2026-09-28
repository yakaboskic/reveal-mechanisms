"""Trusted assembly, identity minting and final acceptance (outside the agent)."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import re
from functools import lru_cache
from .runtime_config import ROOT, setting
from .repository import digest, now
from .evidence_package import canonical_json, decode, require, sha256, pointer
from .dapper_release import verify_release
from .scientific_account_lint import validate_scientific_account

LOCK=ROOT/'services/backend/agent-runtime/dapper-release.json'

def release_root(): return Path(setting('REVEAL_DAPPER_ROOT',str(ROOT/'.runtime/dapper')))

@lru_cache(maxsize=1)
def public_runtime():
    from .evidence_package import DapperRuntime
    return DapperRuntime(ROOT/'data/dapper/2026-09-24-v8')

def mint(document,path):
    root=release_root(); verify_release(root,LOCK)
    path.write_bytes(canonical_json(document))
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

def ledger_sources(ledger_path):
    """Read exact completed tool response captures from the trusted ledger."""
    if ledger_path is None: return {}
    ledger_path=Path(ledger_path).resolve(); ledger=decode(ledger_path.read_bytes()); sources={}
    for call in ledger['calls']:
        if call.get('tool') not in ('query_graph','read_paper') or call.get('status') not in ('completed','empty') or not call.get('response'): continue
        if call.get('tool')=='read_paper' and call.get('status')!='completed': continue
        source=call['response']; path=(ledger_path.parent/source['path']).resolve()
        require(path.is_relative_to(ledger_path.parent),'Tool source artifact path escape')
        data=path.read_bytes()
        require(sha256(data)==source['sha256'] and len(data)==source['size_bytes'],'Tool source checksum or size changed')
        sources[source['sha256']]={'path':path,'bytes':data,'size_bytes':len(data),'tool':call['tool'],'selected_graph':call.get('selected_graph')}
    return sources

def validate_new_files(document,trusted,sources):
    for file in document.get('files',[]):
        if file['id'] in trusted: continue
        captured=sources.get(file.get('sha256'))
        require(captured is not None and file.get('size_in_bytes')==captured['size_bytes'],
            'New evidence File is not bound to an exact trusted tool response checksum and size')

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
    # Hydrate only referenced inputs and their transitive dependencies.
    for _ in range(len(trusted)+1):
        present={n['id'] for rows in doc.values() if isinstance(rows,list) for n in rows if isinstance(n,dict) and 'id' in n}
        serialized=json.dumps(doc); missing=[i for i in trusted if i not in present and i in serialized]
        if not missing: break
        for identity in missing:
            group,node=trusted[identity]; doc.setdefault(group,[]).append(deepcopy(node))
    # Operational attribution comes from the frozen submitting principal and
    # actual worker, never an agent-authored Person or runtime declaration.
    actor_id='urn:reveal:actor:'+digest(['scientific-actor',attribution['user_id']])
    label=attribution.get('display_name') if attribution['principal_kind']=='registered' else 'Anonymous researcher'
    person={'id':actor_id,'name':(label or 'Registered researcher')+' [actor '+digest(['scientific-actor',attribution['user_id']])[:20]+']'}
    if attribution.get('orcid_authenticated') and attribution.get('orcid'): person['orcid']=attribution['orcid']
    activity_id='urn:reveal:execution:'+job['id']+':'+str(attempt)
    activity={'id':activity_id,'name':'REVEAL deterministic development execution' if execution=='deterministic' else 'REVEAL Claude Code account construction',
        'command':'reveal-worker '+execution,'software_name':'REVEAL Mechanisms worker','software_version':'0.2.0',
        'generated_at_time':now()}
    doc=replace_authored_attribution(doc,trusted,person,activity)
    captured=ledger_sources(ledger_path)
    validate_new_files(doc,trusted,captured)
    for group in ('claims','scientific_accounts'):
        for node in doc.get(group,[]):
            if node['id'] not in trusted: node['was_generated_by']=activity_id; node['was_attributed_to']=[actor_id]
    sources=sorted({ref for item in doc.get('evidence_items',[]) for ref in item.get('was_derived_from',[]) if ref in trusted})
    doc.setdefault('used_edges',[]).extend({'subject':activity_id,'predicate':'prov:used','object':ref} for ref in sources)
    document=mint(doc,output_path)
    report=validate_scientific_account(output_path,dapper_root=release_root(),release_lock=LOCK,evidence_package=package_path)
    # ClaimScore values must be traceable to observed numeric source data. The
    # final linter separately verifies explicit File ancestry and proposition targets.
    observed={}
    for artifact in package['source_artifacts'].values():
        path=(Path(package_path).parent/artifact['path']).resolve()
        require(path.is_relative_to(Path(package_path).parent.resolve()),'Source artifact path escape')
        data=path.read_bytes(); require(sha256(data)==artifact['sha256'],'Captured source checksum changed')
        if artifact.get('format')=='json': observed[artifact['dapper_file_id']]=decode(data)
    for file in document.get('files',[]):
        if file.get('sha256') in captured: observed[file['id']]=decode(captured[file['sha256']]['bytes'])
    validate_observations(document,observed)
    return document,report

def validate_observations(document,observed):
    """Quantities bind to a cited artifact, exact JSON row and original metric.

    A coincident numeric value elsewhere in a response is not a match. Free-text
    biological interpretation remains explicitly separate from this source check.
    """
    nodes={n['id']:n for rows in document.values() if isinstance(rows,list) for n in rows if isinstance(n,dict) and 'id' in n}
    def rows_for(evidence,seen=None):
        seen=set() if seen is None else seen
        if evidence.get('id') in seen: return []
        seen=seen|{evidence.get('id')}
        pointers=re.findall(r'/(?:data|response|content|structuredContent)(?:/[A-Za-z0-9_~.-]+)*',evidence.get('context','')+' '+evidence.get('source_locator',''))
        values=[]
        for source in evidence.get('was_derived_from',[]):
            if source not in observed: continue
            for locator in pointers:
                try: value=pointer(observed[source],locator)
                except ValueError: continue
                if isinstance(value,dict): values.append(value)
        for identity in evidence.get('source_claims',[]):
            if identity in nodes: values.extend(rows_for(nodes[identity],seen))
        return values
    for claim in document.get('claims',[]):
        scores=[nodes[s] for s in claim.get('has_score',[]) if s in nodes]
        scores.extend(claim.get('scores',[]))
        evidence=[nodes[e] for e in claim.get('has_evidence',[]) if e in nodes]
        rows=rows_for(claim)+[row for item in evidence for row in rows_for(item)]
        for score in scores:
            metric=score['metric']; value=score['value']
            require(any(metric in row and isinstance(row[metric],(int,float)) and not isinstance(row[metric],bool) and row[metric]==value for row in rows),
                'ClaimScore does not match the exact metric at its cited source row')
            expected={'factor_value':'LOADING','beta':'EFFECT_ESTIMATE','beta_uncorrected':'EFFECT_ESTIMATE','combined':'SCORE'}.get(metric)
            if expected: require(score['score_kind']==expected,'Source metric was relabeled as a different mathematical quantity')
        for item in evidence:
            snippet=item.get('snippet')
            if not snippet: continue
            direct=[observed[s] for s in item.get('was_derived_from',[]) if s in observed]
            if not direct: continue
            candidates=rows_for(item) or direct
            def norm(text): return re.sub(r'\s+','',text)
            require(any(norm(snippet) in norm(json.dumps(value,ensure_ascii=False)) or norm(snippet) in norm(json.dumps(value,ensure_ascii=False,separators=(',',':'))) for value in candidates),
                'Evidence snippet is not a verbatim excerpt of the cited source observation')

def object_envelope(document,identity,metadata,artifact_access=None,*,max_depth=5,max_nodes=250,offset=0,continuation=None):
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
    metadata=[m for m in metadata if m['target_id'] in retained]
    lock=decode(LOCK.read_bytes())
    return {'root_id':identity,'schema':{'schema_sha256':lock['files']['schema/dapper.yaml'],'dependency_snapshot_sha256':sha256(LOCK.read_bytes()),'identity_profile':'DAPPER-ID-1'},
        'document':document,'payloads':[{'object_id':n['id'],'payload_sha256':sha256(canonical_json(n))} for rows in document.values() if isinstance(rows,list) for n in rows if isinstance(n,dict) and str(n.get('id','')).startswith('dapper:')],
        'citation_metadata':metadata,'artifacts':[(artifact_access or {}).get(f['id'],{'file':f,'download_url':None,'expires_at':None,'availability':'not_available','verification':'source_reported'}) for f in document.get('files',[])],
        'coverage':{'direction':'upstream','max_depth':max_depth,'max_nodes':max_nodes,'complete':not missing,'missing_ids':sorted(missing-retained),
            'next_cursor':continuation(end) if continuation and capacity and end<len(upstream) else None}}

def validate_paragraph_document(path):
    """Use pinned upstream lint with an application-owned terminal Paragraph profile."""
    verify_release(release_root(),LOCK)
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
    findings=json.loads(result.stdout)
    require(not any(x['severity']=='error' for x in findings),'Paragraph validation failed: '+str(findings)[:2000])
    return {'profile':'reveal-paragraph','valid':True,'findings':findings}
