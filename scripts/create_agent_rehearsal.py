#!/usr/bin/env python3
"""Build a new synthetic local-agent kit without services, credentials or model calls.

Uses the actual seed/setup builders and pinned schema. The question echoes the
TTD scope regression; it does not reconstruct the downloaded run's observations.
"""
import argparse
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/backend/src'))
from reveal_backend.evidence_package import DapperRuntime, canonical_json, sha256
from reveal_backend.research_seed import prepare_research_seed
from reveal_backend import research_setup
from reveal_backend.repository import uid
from reveal_backend.runtime_config import CURRENT_DAPPER_SNAPSHOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); destination=args.output.resolve()
    if destination.exists(): parser.error('Use a new destination; historical workspaces are never overwritten.')
    runtime=DapperRuntime(CURRENT_DAPPER_SNAPSHOT)
    gap={'name':'Synthetic TTD scope rehearsal','text':'Synthetic rehearsal: do transcription, RNA-processing and translation observations support a shared fitted mechanism?',
         'gap_description':'Synthetic fixture for authoring and reader tests; no observations from the original run are available here.',
         'gap_kind':'KNOWLEDGE_GAP','scope':'Synthetic test only; not scientific evidence','about_entities':['http://purl.obolibrary.org/obo/MONDO_0018053']}
    gap['id']=runtime.compute_id(gap,'KnowledgeGap',runtime.schema)
    mechanism={'name':'Synthetic selected factor','description':'Synthetic fit used only to test the immutable authoring kit.'}
    mechanism['id']=runtime.compute_id(mechanism,'Mechanism',runtime.schema)
    factor='factor:kpn:0005855:eaggl-capped-v1:Factor1';generation='a'*64; work_id=uid();request_id=uid()
    frozen={'id':request_id,'question_id':gap['id'],'document':{'knowledge_gaps':[gap],'mechanisms':[mechanism]},
            'composer':{'source_gap':{'source_id':'synthetic:ttd-gap','source_revision':'f'*64},'eaggl_anchors':[{'reference':{'source_id':factor},'origin':'manual'}],
                        'selected_kgs':[],'dismissed_source_ids':[]},'linked_dismech_context':[]}
    binding={'source_gap':{'object':gap,'attachments':[],'source_detail':{'raw':{'prompt':gap['text'],'synthetic':True}}},'dismech_import_id':'f'*64,
             'anchors':[{'cfde_node_id':factor,'reference_generation_id':generation,'eaggl_import_id':'c'*64,'model':'eaggl-capped-v1'}]}
    built=prepare_research_seed(frozen,binding,dapper=runtime,project_root=ROOT)
    # Port 9 is intentionally not an application endpoint. Rehearse offline.
    built.package['research_context']={'local_work_id':work_id,'research_request_id':request_id,'mcp_url':'http://127.0.0.1:9/mcp'}
    raw=canonical_json(built.package);built.files['evidence-package.json']=raw
    from reveal_backend.evidence_files import build_evidence_index
    built.files['evidence-index.json']=build_evidence_index(raw)
    built.manifest.update(package_sha256=sha256(raw),files={path:sha256(data) for path,data in sorted(built.files.items()) if path!='manifest.json'})
    built.files['manifest.json']=canonical_json(built.manifest)
    work={'id':work_id,'research_request_id':request_id,'reference_generation_id':generation,'package_sha256':sha256(raw)}
    artifacts=[{'id':sha256(data),'filename':path,'sha256':sha256(data),'size_bytes':len(data),'purpose':'seed'} for path,data in built.files.items()]
    package={'package':built.package,'manifest':built.manifest,'sha256':sha256(raw),'artifacts':artifacts}
    connection={'mcp_url':'http://127.0.0.1:9/mcp','device_authorization_url':'http://127.0.0.1:9/oauth/device_authorization',
        'token_url':'http://127.0.0.1:9/oauth/token','revocation_url':'http://127.0.0.1:9/oauth/revoke','return_url':'http://127.0.0.1:9/local-runs/'+work_id}
    launcher=(ROOT/'services/backend/agent-runtime/reveal_local_launcher.py').read_bytes()
    with patch.object(research_setup,'_read',lambda artifact:(artifact['filename'],built.files[artifact['filename']])):
        archive=research_setup._archive(work,package,artifacts,'codex',connection,launcher)
    destination.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        for name in zipped.namelist():
            relative='/'.join(name.split('/')[1:]); target=destination/relative
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(zipped.read(name))
    report={'synthetic':True,'original_run':'/Users/cyakaboski/Downloads/reveal-24afc4d7-3757-4ce1-a5c6-4d6dc2ce8fb7',
        'relationship':'New synthetic contract rehearsal motivated by the original run; none of its absent query bytes are reconstructed.',
        'package_sha256':sha256(raw),'setup_manifest_sha256':sha256((destination/'setup-manifest.json').read_bytes()),
        'workspace_files':len(json.loads((destination/'setup-manifest.json').read_bytes())['files']),
        'seed_bytes':len(raw),'index_bytes':len(built.files['evidence-index.json']),'zip_bytes':len(archive),
        'check_command':'python3 start.py --offline --check-only','agent_command':'python3 start.py codex --offline'}
    (destination/'REHEARSAL.json').write_bytes(canonical_json(report))
    print(json.dumps({'workspace':str(destination),**report},indent=2))

if __name__=='__main__': main()
