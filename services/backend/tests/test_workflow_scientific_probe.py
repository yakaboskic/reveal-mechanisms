"""The live probe is bounded and resumable; these tests use no live service."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from reveal_backend.evidence_package import canonical_json as backend_canonical_json


ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('scientific_probe',ROOT/'scripts/workflow_scientific_probe.py')
probe=importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)


def test_probe_hash_bytes_match_backend_manifest_serializer_including_final_newline():
    manifest=json.loads((ROOT/'services/backend/agent-runtime/dapper-release.json').read_bytes())
    expected=backend_canonical_json(manifest)
    assert expected.endswith(b'\n')
    assert probe.canonical(manifest)==expected
    assert hashlib.sha256(probe.canonical(manifest)).hexdigest()!=hashlib.sha256(expected[:-1]).hexdigest()


def test_reference_model_requires_one_current_model_and_rejects_requested_mismatch():
    record={'record':{'source_id':'factor:kpn:0000136:eaggl-capped-v1:Factor1','model':'eaggl-capped-v1'}}
    assert probe.model_from_records([record])=='eaggl-capped-v1'
    for records,selected in [([],None),([record],'cfde-inc-v2'),
        ([record,{'record':{'source_id':'factor:trait:x:cfde-inc-v2:Factor1'}}],None)]:
        with pytest.raises(RuntimeError): probe.model_from_records(records,selected)


def test_budget_guard_counts_automatic_paragraph_without_increasing_runtime_limit():
    env={'REVEAL_AGENT_MAX_BUDGET_USD':'5'}
    assert probe.budget_observation(env,10)['maximum_model_budget_usd']==10
    for cap in ['25','nan','inf','0']:
        with pytest.raises(RuntimeError): probe.budget_observation({'REVEAL_AGENT_MAX_BUDGET_USD':cap},10)
    assert env=={'REVEAL_AGENT_MAX_BUDGET_USD':'5'}


def test_relationship_report_traverses_source_claims_and_exact_geneset_provenance():
    document={'propositions':[{'id':'p','subject_entity':'NCBIGene:1','relation':'member_of','object_entity':'set'}],
        'claims':[{'id':'c','proposition':'p','has_evidence':['e']}],
        'evidence_items':[{'id':'e','was_derived_from':['f','set'],'source_claims':['prior'],'source_locator':'/result/items/0'}],
        'gene_sets':[{'id':'set','was_derived_from':['upstream']}],
        'files':[{'id':'f','sha256':'a'*64},{'id':'upstream','sha256':'b'*64}],
        'mechanisms':[{'id':'factor'}]}
    findings=probe.relationship_report(document,{'a'*64,'b'*64})
    assert findings['relationship_families']=={'gene–GeneSet':['p'],'gene–Mechanism':[],'GeneSet–Mechanism':[]}
    assert findings['evidence_provenance'][0]['verified_file_ids']==['f','upstream']
    assert findings['evidence_provenance'][0]['source_claims']==['prior']


def test_source_local_genes_count_relationships_without_inferring_identifier_equivalence():
    first='urn:reveal:eaggl-gene:first-import:42'
    other='urn:reveal:eaggl-gene:other-import:42'
    document={'propositions':[
        {'id':'p1','subject_entity':first,'relation':'loads_on','object_entity':'factor'},
        {'id':'p2','subject_entity':other,'relation':'loads_on','object_entity':'factor'},
        {'id':'p3','subject_entity':'NCBIGene:42','relation':'loads_on','object_entity':'factor'}],
        'mechanisms':[{'id':'factor'}]}
    report=probe.relationship_report(document,set())
    assert report['relationship_families']['gene–Mechanism']==['p1','p2','p3']
    scopes=report['gene_identity_scopes']
    assert scopes[first]=={'scope':'source_local','source_import':'first-import','gene_index':42,
        'taxon':None,'cross_dataset_equivalence':'not_inferred'}
    assert scopes[other]['source_import']=='other-import'
    assert scopes['NCBIGene:42']['scope']=='external_identifier_namespace'
    assert all(item['cross_dataset_equivalence']=='not_inferred' for item in scopes.values())


def arguments(tmp_path,**overrides):
    env=tmp_path/'fixture.env'
    env.write_text('REVEAL_ENVIRONMENT=test\nREVEAL_APPLICATION_TABLE_PREFIX=reveal_probe\nREVEAL_JOB_NAMESPACE=probe\n'
        'REVEAL_GATEWAY_SECRET=fixture-gateway-secret-at-least-32-bytes\nREVEAL_GATEWAY_SERVICE_TOKEN=fixture-service\n'
        'REVEAL_AGENT_MAX_BUDGET_USD=5\n')
    return SimpleNamespace(env=str(env),base='http://fixture.invalid',output=str(tmp_path/'state.json'),
        query='coronary',factor_query=None,model=None,max_run_budget_usd=10,prepare_only=True,max_seconds=10,**overrides)


def test_lost_draft_ack_resumes_same_owner_composer_and_idempotency_key(tmp_path,monkeypatch):
    args=arguments(tmp_path); calls=[]; drafts=[]
    factor={'source_id':'factor:kpn:0000136:eaggl-capped-v1:Factor1','model':'eaggl-capped-v1',
        'source_revision':'revision','object':{'id':'mechanism'}}
    def handle(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/internal/v1/principals/anonymous': value={'user_id':'fixture-owner'}
        elif request.url.path=='/v1/mechanisms/search': value={'items':[{'record':factor}]}
        elif request.url.path=='/v1/knowledge-gaps/search':
            value={'items':[{'gap':{'object':{'id':'gap'},'source':{'source_id':'dismech:cad:reverse_causation','source_revision':'frozen'}}}]}
        elif request.url.path=='/v1/mechanisms/suggest':
            assert json.loads(request.content)['model']=='eaggl-capped-v1'
            value={'automatic_anchors':[{'factor':factor}],'suggestion_id':'suggestion'}
        elif request.url.path=='/v1/drafts':
            drafts.append((request.headers['idempotency-key'],request.content))
            if len(drafts)==1: raise httpx.ReadError('lost acknowledgement')
            value={'id':'draft','version':1}
        else: raise AssertionError('Unexpected or paid request: '+str(request.url))
        return httpx.Response(200,json=value)
    client=httpx.AsyncClient
    monkeypatch.setattr(probe.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(handle),**kwargs))
    with pytest.raises(httpx.ReadError): asyncio.run(probe.main(args))
    asyncio.run(probe.main(args))
    assert drafts[0]==drafts[1]
    assert calls.count(('POST','/internal/v1/principals/anonymous'))==1
    assert calls.count(('POST','/v1/mechanisms/suggest'))==1
    state=json.loads(Path(args.output).read_bytes())
    assert state['requested_job_budgets']['max_accounts']==1
    assert state['reference_model']=='eaggl-capped-v1'
    assert 'fixture-service' not in Path(args.output).read_text()
    assert Path(args.output).stat().st_mode & 0o777==0o600


def test_artifact_redirect_does_not_forward_credentials_and_download_is_bounded(monkeypatch):
    raw=b'exact scientific original'; checksum=hashlib.sha256(raw).hexdigest()
    def handle(request):
        if request.url.host=='fixture.invalid':
            assert request.headers['authorization']=='Bearer fixture-private'
            return httpx.Response(307,headers={'location':'https://storage.invalid/object?signed=fixture'})
        assert request.url.host=='storage.invalid'
        assert 'authorization' not in request.headers
        return httpx.Response(200,content=raw)
    client=httpx.AsyncClient
    monkeypatch.setattr(probe.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(handle),**kwargs))
    async def fetch():
        async with probe.httpx.AsyncClient(base_url='http://fixture.invalid') as connection:
            return await probe.download_artifact(connection,{'Authorization':'Bearer fixture-private'},checksum)
    assert asyncio.run(fetch())==raw
    monkeypatch.setattr(probe,'MAX_ARTIFACT_BYTES',4)
    with pytest.raises(RuntimeError,match='byte bound'): asyncio.run(fetch())


@pytest.mark.parametrize('paragraph_status',['succeeded','failed'])
def test_completed_job_replay_verifies_pin_originals_and_paragraph_without_new_submission(tmp_path,monkeypatch,paragraph_status):
    args=arguments(tmp_path); args.prepare_only=False
    lock_raw=(ROOT/'services/backend/agent-runtime/dapper-release.json').read_bytes()
    lock=json.loads(lock_raw); lock_sha=hashlib.sha256(lock_raw).hexdigest()
    kit={'release_tag':lock['tag'],'release_commit':lock['commit'],'release_lock_sha256':lock_sha,
         'files':[],'kit_sha256':hashlib.sha256(backend_canonical_json([])).hexdigest()}
    snapshot=json.loads((probe.CURRENT_DAPPER_SNAPSHOT/'snapshot.json').read_bytes())
    package={'authoring_kit':kit,'dapper_pin':{'snapshot_sha256':snapshot['snapshot_sha256']},
             'authoring':{'max_accounts':1}}
    original=b'{"value":0.12345678901234567890123456789}'
    checksum=hashlib.sha256(original).hexdigest()
    file={'id':'file','sha256':checksum,'size_in_bytes':len(original)}
    account={'root_id':'account','schema':{'dependency_snapshot_sha256':lock_sha},
        'document':{'scientific_accounts':[{'id':'account','component_claims':['claim']}],
            'claims':[{'id':'claim','proposition':'p','has_evidence':['e']}],
            'propositions':[{'id':'p','subject_entity':'NCBIGene:1','relation':'loads_on','object_entity':'factor'}],
            'mechanisms':[{'id':'factor'}],'evidence_items':[{'id':'e','was_derived_from':['file']}],'files':[file]},
        'artifacts':[{'file':file,'availability':'available'}],'coverage':{'missing_ids':[],'next_cursor':None}}
    job={'id':'job','status':'succeeded','result':{'account_ids':['account'],'paragraph_job_ids':['paragraph-job']}}
    paragraph_job={'id':'paragraph-job','status':paragraph_status,
        'result':{'paragraph_id':'dapper:Paragraph.fixture'} if paragraph_status=='succeeded' else None,
        'failure':{'detail':'Synthetic paragraph generation failed'} if paragraph_status=='failed' else None}
    projection={'root_id':'dapper:Paragraph.fixture','schema':{'dependency_snapshot_sha256':lock_sha},
        'document':{'paragraphs':[{'id':'dapper:Paragraph.fixture','text':'Synthetic paragraph.'}]},
        'coverage':{'complete':True,'missing_ids':[],'next_cursor':None}}
    probe.write_private(args.output,probe.canonical({'namespace':'probe','events':{},'user_id':'fixture-owner',
        'draft':{'id':'draft','version':1},'job':job,'requested_job_budgets':{'max_accounts':1}}))
    def handle(request):
        assert request.method=='GET', 'Completed replay must not create another job or principal'
        assert request.headers['authorization'].startswith('Bearer ')
        if request.url.path=='/v1/jobs/job': value=job
        elif request.url.path=='/v1/jobs/paragraph-job': value=paragraph_job
        elif request.url.path=='/v1/paragraphs/dapper:Paragraph.fixture':
            assert paragraph_status=='succeeded'
            value=projection
        elif request.url.path=='/v1/jobs/job/evidence-package':
            value={'package':package,'package_sha256':hashlib.sha256(backend_canonical_json(package)).hexdigest()}
        elif request.url.path=='/v1/accounts/account': value=account
        elif request.url.path=='/v1/artifacts/'+checksum: return httpx.Response(200,content=original)
        else: raise AssertionError(request.url)
        return httpx.Response(200,json=value)
    client=httpx.AsyncClient
    monkeypatch.setattr(probe.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(handle),**kwargs))
    assert asyncio.run(probe.main(args))==(0 if paragraph_status=='succeeded' else 1)
    state=json.loads(Path(args.output).read_bytes())
    assert state['contract_verified']['commit']==lock['commit']
    assert state['accounts']['account']['relationship_counts']['gene–Mechanism']==1
    reports=tmp_path/'state-reports'
    assert (reports/'artifacts'/checksum).read_bytes()==original
    assert json.loads((reports/'account-1/relationship-provenance.json').read_bytes())['coverage_complete']
    assert json.loads((reports/'paragraph-1/result.json').read_bytes())==paragraph_job
    summary=json.loads((reports/'workflow-summary.json').read_bytes())
    assert summary==state['workflow_summary']
    assert summary['analysis_status']=='succeeded'
    assert summary['full_flow_succeeded']==(paragraph_status=='succeeded')
    observed=state['paragraphs']['paragraph-job']
    assert observed['status']==paragraph_status
    assert observed['failure']==paragraph_job['failure']
    assert observed['projection_verified']==(paragraph_status=='succeeded')
    if paragraph_status=='succeeded':
        assert json.loads((reports/'paragraph-1/projection.json').read_bytes())==projection
        assert (reports/'paragraph-1/projection.json').stat().st_mode & 0o777==0o600
    else:
        assert not (reports/'paragraph-1/projection.json').exists()
        assert summary['full_flow_status']=='paragraph_incomplete'


@pytest.mark.parametrize('condition',['missing_id','unavailable','wrong_root','wrong_release'])
def test_successful_paragraph_job_requires_bound_authorized_projection_for_full_flow(condition):
    analysis={'id':'analysis','status':'succeeded','result':{'paragraph_job_ids':['paragraph-job']}}
    final={'id':'paragraph-job','status':'succeeded','result':{} if condition=='missing_id' else {'paragraph_id':'dapper:Paragraph.fixture'}}
    reports={}; recorded={}; calls=[]
    async def wait_job(identity):
        assert identity=='paragraph-job'; return final
    async def request(method,path,**kwargs):
        calls.append((method,path))
        assert method=='GET' and kwargs['headers']=={'Authorization':'Bearer fixture'}
        assert path=='/v1/paragraphs/dapper%3AParagraph.fixture'
        if condition=='unavailable': raise RuntimeError('Authorized projection unavailable (HTTP 404)')
        return {'root_id':'other' if condition=='wrong_root' else 'dapper:Paragraph.fixture',
                'schema':{'dependency_snapshot_sha256':'wrong' if condition=='wrong_release' else 'locked'}}
    observations=asyncio.run(probe.capture_paragraph_results(analysis,wait_job=wait_job,request=request,
        auth=lambda:{'Authorization':'Bearer fixture'},report=lambda path,value:reports.update({path:value}),
        record=lambda identity,value:recorded.update({identity:value}),release_lock_sha256='locked'))
    assert reports['paragraph-1/result.json']==final
    assert observations==[recorded['paragraph-job']]
    assert observations[0]['status']=='succeeded' and not observations[0]['projection_verified']
    assert observations[0]['projection_error']
    assert len(calls)==(0 if condition=='missing_id' else 1)
    summary=probe.workflow_summary(analysis,observations)
    assert not summary['full_flow_succeeded'] and summary['exit_code']==1


def test_analysis_only_and_failed_analysis_are_not_reported_as_full_flow_success():
    for status,expected in [('succeeded','analysis_only'),('failed','failed'),('insufficient_evidence','insufficient_evidence')]:
        summary=probe.workflow_summary({'id':'analysis','status':status},[])
        assert summary['full_flow_status']==expected
        assert not summary['full_flow_succeeded'] and summary['exit_code']==1
