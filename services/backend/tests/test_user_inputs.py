"""Explicit saves, private uploads, frozen runs and exact agent/review inputs."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tarfile
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import test_application as application
import test_evidence_package as evidence_fixture
from reveal_backend import app as api, jobs, user_inputs, publication, analysis_outcomes
from reveal_backend.auth import Problem
from reveal_backend.repository import digest, uid
from reveal_backend.evidence_package import canonical_json, sha256


@pytest.fixture
def client(tmp_path,monkeypatch):
    case=application.ApplicationTests()
    case.setUp()
    monkeypatch.setenv('REVEAL_ARTIFACT_STORE','filesystem')
    monkeypatch.setenv('REVEAL_ARTIFACTS_DIR',str(tmp_path))
    try: yield case
    finally: case.doCleanups()


def temporary(case,owner,**extra):
    response=case.client.post('/v1/drafts',json={'composer':deepcopy(application.COMPOSER),'lifecycle':'temporary',**extra},headers=case.headers(owner))
    assert response.status_code==201,response.text
    return response.json()


def upload(case,owner,draft,data=b'First observation\nSecond observation',filename='notes.txt'):
    body={'draft_id':draft['id'],'filename':filename,'media_type':'text/plain','size_bytes':len(data),'sha256':sha256(data)}
    headers=case.headers(owner)
    response=case.client.post('/v1/uploads',json=body,headers=headers)
    assert response.status_code==201,response.text
    assert case.client.post('/v1/uploads',json=body,headers=headers).json()==response.json()
    ticket=response.json(); identity=ticket['upload']['id']
    response=case.client.post(ticket['transfer']['url'],json={'content_base64':base64.b64encode(data).decode()},headers=case.headers(owner))
    assert response.status_code==200,response.text
    response=case.client.post('/v1/uploads/'+identity+'/complete',json={},headers=case.headers(owner))
    assert response.status_code==200,response.text
    return response.json()


def test_temporary_hidden_explicit_save_atomic_and_expiry(client):
    owner=client.provision(); draft=temporary(client,owner)
    assert client.client.get('/v1/drafts',headers=client.headers(owner)).json()['items']==[]
    path='/v1/drafts/'+draft['id']
    response=client.client.patch(path,json={'expected_version':1,'lifecycle':'saved'},headers=client.headers(owner))
    assert response.status_code==422
    response=client.client.patch(path,json={'expected_version':1,'lifecycle':'saved','name':'Specific question'},headers=client.headers(owner))
    assert response.status_code==200,response.text
    assert response.json()['expires_at'] is None
    assert len(client.client.get('/v1/drafts',headers=client.headers(owner)).json()['items'])==1
    expired=temporary(client,owner)
    with client.repo.transaction() as tx:
        expired['expires_at']='2000-01-01T00:00:00Z'; tx.put('draft',expired['id'],owner,expired)
    assert client.client.get('/v1/drafts/'+expired['id'],headers=client.headers(owner)).status_code==410
    with client.repo.transaction() as tx: user_inputs.cleanup(tx,owner)
    assert client.client.get('/v1/drafts/'+expired['id'],headers=client.headers(owner)).status_code==404


def test_draft_listing_is_a_pure_read_and_reconciliation_expires_editors(client):
    from reveal_backend.workflow_routes import sweep
    owner=client.provision(); kept=client.draft(owner); expired=temporary(client,owner)
    with client.repo.transaction() as tx:
        expired['expires_at']='2000-01-01T00:00:00Z'; tx.put('draft',expired['id'],owner,expired)
        tx.put('exploration','visit',owner,{'source_gap':{'id':'gap'},'draft_id':expired['id']})
    with patch.object(client.repo,'transaction',side_effect=AssertionError('GET must not take the write fence')):
        listed=client.client.get('/v1/drafts',headers=client.headers(owner))
    assert listed.status_code==200 and [d['id'] for d in listed.json()['items']]==[kept['id']]
    with client.repo.read_transaction() as tx: assert tx.get('draft',expired['id'])   # the listing deleted nothing
    assert client.client.get('/v1/drafts/'+expired['id'],headers=client.headers(owner)).status_code==410
    assert sweep(client.repo)==(0,False,[])
    assert client.client.get('/v1/drafts/'+expired['id'],headers=client.headers(owner)).status_code==404
    with client.repo.read_transaction() as tx:
        assert tx.get('draft',kept['id']) and tx.get('exploration','visit')['data']['draft_id'] is None


def test_cleanup_decides_snapshot_candidates_again_under_the_fence(client):
    owner,other=client.provision(),client.provision()
    renewed,removed=temporary(client,owner),temporary(client,other)
    with client.repo.transaction() as tx:
        for draft,draft_owner in ((renewed,owner),(removed,other)):
            draft['expires_at']='2000-01-01T00:00:00Z'; tx.put('draft',draft['id'],draft_owner,draft)
    with client.repo.read_transaction() as tx: candidates=user_inputs.cleanup_candidates(tx)
    assert sorted(candidates[0])==sorted([renewed['id'],removed['id']]) and candidates[1]==[]
    with client.repo.transaction() as tx:   # renewed between the snapshot and the fence
        renewed['expires_at']=user_inputs.expiration(); tx.put('draft',renewed['id'],owner,renewed)
    with client.repo.transaction() as tx:
        assert user_inputs.cleanup(tx,candidates=candidates)==[removed['id']]
        assert tx.get('draft',renewed['id']) and not tx.get('draft',removed['id'])
        assert user_inputs.cleanup_candidates(tx,owner)==([],[])


def test_submission_clone_preserves_loaded_saved_revision_after_concurrent_edit(client):
    owner=client.provision(); source=client.draft(owner)
    loaded=deepcopy(source['composer'])
    changed={**loaded,'context':'Saved in another session'}
    response=client.client.patch('/v1/drafts/'+source['id'],json={'expected_version':1,'composer':changed},headers=client.headers(owner))
    assert response.status_code==200 and response.json()['version']==2
    working={**loaded,'context':'My local unsaved edits'}
    body={'composer':working,'lifecycle':'temporary','source_draft_id':source['id'],'source_draft_version':1}
    result=client.client.post('/v1/drafts',json=body,headers=client.headers(owner))
    assert result.status_code==201,result.text
    assert result.json()['source_draft_version']==1 and result.json()['composer']==working
    assert client.client.get('/v1/drafts/'+source['id'],headers=client.headers(owner)).json()['composer']==changed
    # An older client can still omit lineage revision; impossible future revisions
    # and a revision without an originating saved draft are rejected.
    result=client.client.post('/v1/drafts',json={k:v for k,v in body.items() if k!='source_draft_version'},headers=client.headers(owner))
    assert result.status_code==201 and result.json()['source_draft_version']==2
    assert client.client.post('/v1/drafts',json={**body,'source_draft_version':3},headers=client.headers(owner)).status_code==409
    assert client.client.post('/v1/drafts',json={k:v for k,v in body.items() if k!='source_draft_id'},headers=client.headers(owner)).status_code==422


def test_temporary_submission_preserves_reload_gate_expiry_and_context(client):
    from reveal_backend import reference_generation as reference
    owner=client.provision()
    composer={**deepcopy(application.COMPOSER),'context':'Research context to retain'}
    draft=temporary(client,owner,composer=composer)
    expired=temporary(client,owner)
    with client.repo.transaction() as tx:
        expired['expires_at']='2000-01-01T00:00:00Z'
        tx.put('draft',expired['id'],owner,expired)
        reference.set_gate(tx,True,reason='test reload')
    def submit(value):
        return client.client.post('/v1/jobs',json={'kind':'analysis','draft_id':value['id'],'draft_version':value['version']},
                                  headers=client.headers(owner))
    for value in (draft,expired):
        result=submit(value)
        assert result.status_code==503 and result.json()['code']=='REFERENCE_RELOAD_IN_PROGRESS'
    with client.repo.transaction() as tx:
        assert tx.list('request',owner)==[] and tx.list('job',owner)==[]
        reference.set_gate(tx,False,reason='test reload complete')
    result=submit(expired)
    assert result.status_code==410 and result.json()['code']=='EDITOR_EXPIRED'
    result=submit(draft)
    # The live editor now reaches ordinary validation; the empty test catalog
    # has no selected anchors, so it still cannot dispatch research work.
    assert result.status_code==422 and result.json()['code']=='ANCHOR_REQUIRED'
    with client.repo.read_transaction() as tx:
        assert tx.list('request',owner)==[] and tx.list('job',owner)==[]
        assert tx.get('draft',draft['id'])['data']['composer']['context']==composer['context']
        assert tx.get('draft',draft['id'])['data']['lifecycle']=='temporary'


def test_upload_checksum_owner_clone_and_retained_request(client):
    owner,other=client.provision(),client.provision(); draft=temporary(client,owner); item=upload(client,owner,draft)
    path='/v1/uploads/'+item['id']
    assert client.client.get(path,headers=client.headers(other)).status_code==404
    assert client.client.get(path+'/download',headers=client.headers(other)).status_code==404
    assert client.client.get(path+'/download',headers=client.headers(owner)).content==b'First observation\nSecond observation'
    composer=dict(application.COMPOSER,upload_ids=[item['id']],research_direction='Compare observations',context='Private context',hypotheses='An unverified proposal')
    with client.repo.transaction() as tx:
        frozen=user_inputs.resolve(tx,owner,composer)
        with pytest.raises(Problem): user_inputs.resolve(tx,other,composer)
        tx.put('request','frozen',owner,{'source_draft_id':draft['id'],'composer':composer,'user_inputs':frozen})
    response=client.client.request('DELETE','/v1/drafts/'+draft['id'],json={'expected_version':1},headers=client.headers(owner))
    assert response.status_code==200
    assert user_inputs.read(frozen['uploads'][0]['storage'])==b'First observation\nSecond observation'
    assert client.client.delete(path,headers=client.headers(owner)).status_code==409
    with client.repo.transaction() as tx:
        stored=tx.get('upload',item['id'])['data']; stored['expires_at']='2000-01-01T00:00:00Z'; tx.put('upload',item['id'],owner,stored)
        user_inputs.cleanup(tx,owner)
        retained=tx.get('upload',item['id'])['data']
        assert retained['expires_at']=='2000-01-01T00:00:00Z' and retained['reference_recheck_at']>user_inputs.now()
        assert user_inputs.cleanup_candidates(tx,owner)==([],[])   # rechecked a day later, not every sweep
    assert 'reference_recheck_at' not in client.client.get(path,headers=client.headers(owner)).json()
    assert client.client.post(path+'/complete',json={},headers=client.headers(owner)).json()['storage']==item['storage']
    with client.repo.transaction() as tx:
        tx.remove('request','frozen')
        stored=tx.get('upload',item['id'])['data']; stored['reference_recheck_at']='2000-01-01T00:00:00Z'; tx.put('upload',item['id'],owner,stored)
        assert user_inputs.cleanup_candidates(tx,owner)==([],[item['id']])
        user_inputs.cleanup(tx,owner)
        assert not tx.get('upload',item['id'])   # no longer referenced once due again


def test_upload_bad_bytes_and_unsupported_types(client):
    owner=client.provision(); draft=temporary(client,owner)
    body={'draft_id':draft['id'],'filename':'data.txt','media_type':'text/plain','size_bytes':3,'sha256':sha256(b'abc')}
    result=client.client.post('/v1/uploads',json=body,headers=client.headers(owner)).json()
    response=client.client.post(result['transfer']['url'],json={'content_base64':base64.b64encode(b'bad').decode()},headers=client.headers(owner))
    assert response.status_code==422
    assert client.client.post('/v1/uploads',json={**body,'filename':'../secret.txt'},headers=client.headers(owner)).status_code==422
    assert client.client.post('/v1/uploads',json={**body,'filename':'program.exe'},headers=client.headers(owner)).status_code==422
    assert client.client.post('/v1/uploads',json={**body,'size_bytes':8_000_001},headers=client.headers(owner)).status_code==422
    with pytest.raises(ValueError): user_inputs.extract(b'\x00binary','data.txt')
    with pytest.raises(ValueError): user_inputs.extract(b'a'*500001,'notes.txt')


def test_s3_retry_refreshes_ticket_and_retains_immutable_bytes(client,monkeypatch):
    from test_deployment import MemoryS3
    from reveal_backend.artifact_store import S3Store
    class UploadS3(MemoryS3):
        signs=0
        policies=[]
        def generate_presigned_post(self,bucket,key,**kwargs):
            self.signs+=1; self.policies.append(kwargs)
            return {'url':'https://storage.invalid/'+str(self.signs),'fields':{'key':key,**kwargs['Fields']}}
        def get_object(self,Key,VersionId=None,**kwargs):
            version=VersionId or self.latest[Key]
            return {'Body':io.BytesIO(self.objects[Key,version]),'VersionId':version}
    storage=S3Store('reveal-test-artifacts','local/',client=UploadS3())
    monkeypatch.setattr(user_inputs,'s3_enabled',lambda:True)
    monkeypatch.setattr(user_inputs,'store',lambda:storage)
    owner,other=client.provision(),client.provision(); draft=temporary(client,owner)
    raw=b'Exact retained observation'
    body={'draft_id':draft['id'],'filename':'notes.txt','size_bytes':len(raw),'media_type':'text/plain','sha256':sha256(raw)}
    headers=client.headers(owner)
    first=client.client.post('/v1/uploads',json=body,headers=headers).json()
    replay=client.client.post('/v1/uploads',json=body,headers=headers).json()
    assert replay['upload']==first['upload']
    assert replay['transfer']['url']!=first['transfer']['url']
    assert ['content-length-range',len(raw),len(raw)] in storage.client.policies[-1]['Conditions']
    assert storage.client.policies[-1]['ExpiresIn']==900
    identity=first['upload']['id']; staging=first['transfer']['fields']['key']
    storage.client.put_object(Key=staging,Body=raw)
    ready=client.client.post('/v1/uploads/'+identity+'/complete',json={},headers=client.headers(owner)).json()
    assert ready['status']=='ready' and ready['storage']['version_id']
    storage.client.put_object(Key=staging,Body=b'new untrusted staging bytes')
    assert user_inputs.read(ready['storage'])==raw
    with client.repo.transaction() as tx:
        row=tx.get('upload',identity); row['data']['expires_at']='2000-01-01T00:00:00Z'; tx.put('upload',identity,owner,row['data'])
    replay=client.client.post('/v1/uploads',json=body,headers=headers)
    assert replay.status_code==201 and replay.json()['upload']['status']=='ready'
    with client.repo.transaction() as tx:
        row=tx.get('upload',identity); tx.put('upload',identity,other,row['data'])
    assert client.client.post('/v1/uploads',json=body,headers=headers).status_code==404


def test_replace_saved_upload_without_discarding_saved_snapshot(client):
    owner=client.provision(); draft=client.draft(owner)
    items=[upload(client,owner,draft,filename=str(index)+'.txt') for index in range(5)]
    composer={**application.COMPOSER,'upload_ids':[item['id'] for item in items]}
    response=client.client.patch('/v1/drafts/'+draft['id'],json={'expected_version':1,'composer':composer},headers=client.headers(owner))
    assert response.status_code==200,response.text
    # A pending replacement has separate quota from the five saved selections.
    replacement=upload(client,owner,draft,filename='replacement.txt')
    assert client.client.delete('/v1/uploads/'+items[0]['id'],headers=client.headers(owner)).status_code==409
    with client.repo.read_transaction() as tx:
        with pytest.raises(Problem,match='five distinct'): user_inputs.resolve(tx,owner,{**composer,'upload_ids':composer['upload_ids']+[replacement['id']]})
    composer['upload_ids']=[replacement['id'],*composer['upload_ids'][1:]]
    response=client.client.patch('/v1/drafts/'+draft['id'],json={'expected_version':2,'composer':composer},headers=client.headers(owner))
    assert response.status_code==200,response.text
    assert client.client.delete('/v1/uploads/'+items[0]['id'],headers=client.headers(owner)).status_code==200


def test_pdf_and_docx_extraction_preserves_locators():
    from pypdf import PdfWriter
    from pypdf.generic import NameObject,DictionaryObject,DecodedStreamObject
    pdf=PdfWriter(); page=pdf.add_blank_page(width=200,height=200)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):pdf._add_object(font)})})
    stream=DecodedStreamObject(); stream.set_data(b'BT /F1 12 Tf 10 100 Td (Observed trend) Tj ET')
    page[NameObject('/Contents')]=pdf._add_object(stream)
    output=io.BytesIO(); pdf.write(output)
    assert user_inputs.extract(output.getvalue(),'paper.pdf')['segments']==[{'locator':'page:1','text':'Observed trend'}]
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Measured result</w:t></w:r></w:p></w:body></w:document>')
    assert user_inputs.extract(output.getvalue(),'paper.docx')['segments']==[{'locator':'paragraph:1','text':'Measured result'}]


def test_frozen_input_integrity_and_exact_upload_citations():
    from reveal_backend.worker import check_user_inputs
    from reveal_backend.source_validation import observation_findings
    frozen={'user_inputs':{'format':'reveal.user-inputs/1','research_direction':'Test','context':'Private','hypotheses':'Proposal','uploads':[{'id':'one','sha256':'a'*64}]}}
    package=deepcopy(frozen); package['user_inputs']['uploads'][0]['content']={'segments':[]}
    check_user_inputs(package,frozen)
    package['user_inputs']['context']='Replacement'
    with pytest.raises(ValueError,match='researcher text changed'): check_user_inputs(package,frozen)
    source={'format':'reveal.upload-text/1','segments':[{'locator':'line:1','text':'Measured observation'},{'locator':'line:2','text':'Different finding'}]}
    item={'id':'item','was_derived_from':['file'],'source_locator':'/segments/0 (line:1)','snippet':'Measured observation'}
    assert observation_findings({'evidence_items':[item]},{'file':source})==[]
    assert any(f['check']=='evidence-snippet' for f in observation_findings({'evidence_items':[{**item,'source_locator':'/segments/1 (line:2)'}]},{'file':source}))
    assert any(f['check']=='source-locator' for f in observation_findings({'evidence_items':[{**item,'source_locator':'line:1'}]},{'file':source}))


def test_run_freezes_inputs_independently_and_private_publication_blocked(client):
    owner=client.provision(); source=client.draft(owner)
    response=client.client.patch('/v1/drafts/'+source['id'],json={'expected_version':1,'name':'Other session revision'},headers=client.headers(owner))
    assert response.status_code==200 and response.json()['version']==2
    draft=temporary(client,owner,source_draft_id=source['id'],source_draft_version=1); item=upload(client,owner,draft)
    gap={'object':{'id':'dapper:KnowledgeGap.'+'g'*32},'attachments':[]}
    native='factor:portal:demo:cfde-inc-v2:Factor1'
    composer={**application.COMPOSER,'source_gap':{'id':gap['object']['id'],'source_id':'dismech:test','source_revision':'a'*64},
        'eaggl_anchors':[{'reference':{'source':'eaggl','source_id':native,'source_revision':'b'*64,'dapper_id':'dapper:Mechanism.'+'m'*32},'origin':'manual','suggestion_id':None}],
        'upload_ids':[item['id']],'context':'Exact private context'}
    with client.repo.transaction() as tx:
        draft['composer']=composer; tx.put('draft',draft['id'],owner,draft)
        tx.put('draft_binding',draft['id'],owner,{'dismech_import_id':'test','source_gap':gap,'selections':{native:{
            'record':{'object':{'id':composer['eaggl_anchors'][0]['reference']['dapper_id']}},
            'binding':{'cfde_node_id':native,'reference_generation_id':'a'*64}}}})
    with patch.object(api,'catalog',SimpleNamespace(selected=lambda _:gap)):
        response=client.client.post('/v1/jobs',json={'kind':'analysis','draft_id':draft['id'],'draft_version':1},headers=client.headers(owner))
    assert response.status_code==202,response.text
    job=response.json()
    assert client.client.request('DELETE','/v1/drafts/'+draft['id'],json={'expected_version':1},headers=client.headers(owner)).status_code==200
    with client.repo.transaction() as tx:
        frozen=tx.get('request',job['research_request_id'])['data']
        assert frozen['user_inputs']['context']=='Exact private context'
        assert frozen['user_inputs']['uploads'][0]['storage']==item['storage']
        assert frozen['originating_saved_draft_id']==source['id'] and frozen['originating_saved_draft_version']==1
        with pytest.raises(Problem,match='private researcher inputs'): user_inputs.prevent_private_publication(tx,job['id'])
        account_id='dapper:ScientificAccount.'+'a'*32
        tx.put('account',digest([owner,account_id]),owner,{'summary':{'job_id':job['id']},'result':{}})
        with pytest.raises(Problem,match='private researcher inputs'): publication.freeze(tx,owner,account_id)
        outcome_id=uid(); tx.put('analysis_outcome',outcome_id,owner,{'record':{'job_id':job['id']}})
        with pytest.raises(Problem,match='private researcher inputs'): analysis_outcomes.change(tx,outcome_id,owner,'public',0)
        assert tx.list('publication')==[] and tx.list('outcome_snapshot')==[]


def test_migration_preserves_legacy_and_is_idempotent(client):
    import importlib.util
    path=Path(__file__).resolve().parents[3]/'scripts/migrate_research_inputs.py'
    spec=importlib.util.spec_from_file_location('migration',path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    owner=client.provision(); draft=client.draft(owner)
    with client.repo.transaction() as tx:
        legacy=tx.get('draft',draft['id'])['data']; legacy.pop('lifecycle'); legacy.pop('expires_at'); tx.put('draft',draft['id'],owner,legacy)
        tx.put('request','historic',owner,{'composer':deepcopy(legacy['composer'])})
        rows=module.migration_rows(tx)
        assert len(rows)==1 and rows[0][1]['lifecycle']=='saved'
        for row,value in rows: tx.put('draft',row['id'],row['owner'],value,expected=row['version'])
        assert module.migration_rows(tx)==[]
        assert 'context' not in tx.get('request','historic')['data']['composer']


def test_supplied_files_reach_bundle_and_review_with_exact_bytes(client,tmp_path):
    from reveal_backend.evidence_collector import collect_package
    from reveal_backend.evidence_schema import validate_package_shape,load_generated_schema
    from reveal_backend.box_adapter import make_bundle
    from reveal_backend.agent_execution import ExecutionRequest
    from reveal_backend.evidence_files import build_evidence_files
    from reveal_backend.scientific_grounding import review_evidence
    from reveal_backend.scientific_review_reader import EvidenceReader
    fixture=evidence_fixture.EvidencePackageTests; fixture.setUpClass()
    try:
        owner=client.provision(); draft=temporary(client,owner); item=upload(client,owner,draft)
        with client.repo.read_transaction() as tx: supplied=user_inputs.resolve(tx,owner,{'context':'Do not assume causality','upload_ids':[item['id']]})
        built=collect_package(gap_id=evidence_fixture.GAP,factor_ids=[evidence_fixture.FACTOR],output=tmp_path/'capture',
            dapper=fixture.runtime,project_root=evidence_fixture.ROOT,dismech_source=fixture.source,
            dismech_index=evidence_fixture.ROOT/'data/dismech-gaps/2026-09-24',geneset_import=fixture.imported,
            limit=8,client_factory=evidence_fixture.FixtureClient,user_inputs=supplied)
        package=built.package
        validate_package_shape(package,load_generated_schema(evidence_fixture.ROOT/'schema/evidence-package.schema.json'))
        assert package['user_inputs']['uploads'][0]['content']['segments'][0]['text']=='First observation'
        request=ExecutionRequest(job_id=uid(),attempt=1,kind='research',input_path=tmp_path/'capture/package/evidence-package.json',
            output_dir=tmp_path/'output',selected_graphs=tuple(package['external_evidence']['selected_graphs']))
        with tarfile.open(fileobj=io.BytesIO(make_bundle(evidence_fixture.ROOT,request)),mode='r:gz') as archive:
            for key in ('original_artifact_id','extraction_artifact_id'):
                artifact=package['source_artifacts'][package['user_inputs']['uploads'][0][key]]
                assert sha256(archive.extractfile('input/'+artifact['path']).read())==artifact['sha256']
        reading=build_evidence_files(request.input_path.read_bytes())
        assert 'user_inputs' in json.loads(reading['input/evidence-index.json'])['sections']
        ledger=tmp_path/'ledger.json'; ledger.write_text(json.dumps({'format':'reveal.tool-ledger/1','complete':True,'calls':[]}))
        reviewer=EvidenceReader(review_evidence(package,ledger))
        assert any(row['pointer']=='/package/user_inputs' for row in reviewer.initial()['index'])
        assert reviewer.read('/package/user_inputs/uploads/0/content/segments/0')['value']['text']=='First observation'
    finally: fixture.tearDownClass()
