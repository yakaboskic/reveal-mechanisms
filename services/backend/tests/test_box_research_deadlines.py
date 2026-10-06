"""Hosted asynchronous reads recover without blocking or accepting late bytes."""
import json
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from reveal_backend import box_research
from reveal_backend.box_literature import BASE, NoRedirect, request_spec
from reveal_backend.box_mcp import Ledger, ScopedTools
from reveal_backend.box_research import HostedResearchClient, ResearchAccessError, network_policy
from test_box_research import CONTEXT, TOKEN, Response, hosted


def test_pending_id_is_durable_and_same_call_after_restart_observes_without_requery(hosted):
    research,state,root=hosted
    args={'arguments':{'factor_id':'factor'}}
    result=research.call('get_factor',args)
    assert result['structuredContent']['operation_id']=='op'
    assert result['structuredContent']['state']=='received'
    assert state['polls']==0 and not research.receipt_ids
    journal=root/'ledger/research-operations.json'
    recorded=json.loads(journal.read_bytes())
    assert recorded['execution_id']=='job:1'
    assert list(recorded['operations'].values())[0]['operation_id']=='op'
    recovered=HostedResearchClient(CONTEXT,TOKEN,research.root,seed_path=research.seed_path,
        execution_id='job:1',opener=research._opener)
    result=recovered.call('get_factor',args)
    assert result['structuredContent']['state']=='running'
    assert len([call for call in state['calls'] if call[0]=='get_factor'])==1
    assert recovered.call('get_operation',{'operation_id':'op'})['structuredContent']['state']=='succeeded'
    assert recovered.receipt_ids=={'receipt'}
    with pytest.raises(ResearchAccessError,match='operation scope'):
        HostedResearchClient(CONTEXT,TOKEN,research.root,seed_path=research.seed_path,
            execution_id='job:2',opener=research._opener)


def test_outer_timeout_returns_known_recovery_and_background_cannot_write_receipts(hosted):
    research,state,root=hosted
    args={'arguments':{'factor_id':'factor'}}
    research.call('get_factor',args)
    original=research._invoke
    release=threading.Event(); finished=threading.Event()
    def blocked(tool,arguments):
        assert tool=='get_operation'
        try:
            assert release.wait(2)
            return {'structuredContent':{'id':'op','state':'succeeded','result':{'receipt_id':'late-receipt'}}}
        finally: finished.set()
    research._invoke=blocked
    ledger=Ledger(root/'deadline-ledger','job',1,secrets=(TOKEN,))
    proxy=ScopedTools([],ledger,research=research,read_timeout=.05,max_parallel_reads=1)
    paths=[root/'ledger/research-receipts.json',root/'ledger/research-operations.json']
    before=[path.read_bytes() for path in paths]
    started=time.monotonic(); result=proxy.call('get_factor',args)
    assert time.monotonic()-started<.3
    assert result['isError']
    assert result['structuredContent']['recovery']=={'tool':'get_operation','arguments':{'operation_id':'op'}}
    ledger.freeze(); ledger_bytes=(ledger.root/'manifest.json').read_bytes()
    release.set(); assert finished.wait(1)
    # Wait for the worker's remaining deadline check and queue delivery.
    until=time.monotonic()+1
    while not proxy.read_slots.acquire(blocking=False):
        assert time.monotonic()<until
        time.sleep(.001)
    proxy.read_slots.release()
    assert [path.read_bytes() for path in paths]==before
    assert (ledger.root/'manifest.json').read_bytes()==ledger_bytes
    assert 'late-receipt' not in research.receipt_ids
    research._invoke=original
    research.call('get_operation',{'operation_id':'op'})
    research.call('get_operation',{'operation_id':'op'})
    assert research.receipt_ids=={'receipt'}


def test_lost_initial_ack_returns_exact_retry_without_inventing_id_or_late_journal(hosted):
    research,state,root=hosted
    research.definitions()
    original=research._invoke; release=threading.Event(); finished=threading.Event(); keys=[]
    def blocked(tool,arguments):
        value=original(tool,arguments)
        keys.append(state['calls'][-1][1]['idempotency_key'])
        try:
            assert release.wait(2)
            return value
        finally: finished.set()
    research._invoke=blocked
    proxy=ScopedTools([],Ledger(root/'lost-ack-ledger','job',1),research=research,read_timeout=.05,max_parallel_reads=1)
    args={'arguments':{'factor_id':'factor'}}
    result=proxy.call('get_factor',args)
    assert result['structuredContent']['recovery']=={'tool':'get_factor','arguments':args}
    assert 'operation_id' not in result['structuredContent']
    release.set(); assert finished.wait(1)
    until=time.monotonic()+1
    while not proxy.read_slots.acquire(blocking=False):
        assert time.monotonic()<until
        time.sleep(.001)
    proxy.read_slots.release()
    assert not (root/'ledger/research-operations.json').exists()
    research._invoke=original
    research.call('get_factor',args)
    sent=[arguments for tool,arguments in state['calls'] if tool=='get_factor']
    assert len(sent)==2 and sent[1]['idempotency_key']==sent[0]['idempotency_key']==keys[0]


def test_definition_and_request_share_remaining_deadline(hosted,monkeypatch):
    research,_,_=hosted
    tick=[100.0]; timeouts=[]
    monkeypatch.setattr(box_research,'time',SimpleNamespace(monotonic=lambda:tick[0],sleep=lambda delay:None))
    class Opener:
        def open(self,request,timeout):
            timeouts.append(timeout)
            message=json.loads(request.data); tick[0]+=2
            if message['method']=='tools/list':
                result={'tools':[{'name':'get_local_work','inputSchema':{'type':'object','properties':{},'required':[]}}]}
            else: result={'structuredContent':{'id':'work','state':'ready'}}
            return Response(json.dumps({'id':message['id'],'result':result}).encode(),request.full_url)
    research._opener=Opener()
    call=research.begin_bounded_call('get_local_work',{},timeout=10)
    result=call.run(); call.accept(result)
    assert timeouts==[9,7]
    assert call.deadline==109


def test_explicit_polls_use_inspection_allowance_not_scientific_query_budget(hosted):
    research,_,root=hosted
    proxy=ScopedTools([],Ledger(root/'poll-budget-ledger','job',1),research=research,max_research_calls=1)
    assert not proxy.call('get_factor',{'arguments':{'factor_id':'factor'}}).get('isError')
    assert not proxy.call('get_operation',{'operation_id':'op'}).get('isError')
    assert not proxy.call('get_operation',{'operation_id':'op'}).get('isError')
    assert proxy.research_calls==1 and proxy.inspection_calls==2
    assert proxy.call('get_factor',{'arguments':{'factor_id':'other'}})['isError']


def test_box_network_policy_includes_only_exact_fixed_literature_host():
    domain=urlsplit(BASE).hostname
    assert domain=='www.ebi.ac.uk'
    for tool,args in [('search_papers',{'query':'gene'}),('read_paper',{'source':'MED','id':'123'}),
                      ('read_paper',{'source':'PMC','id':'PMC123','section':'full_text'})]:
        assert urlsplit(request_spec(tool,args)['url']).hostname==domain
    policy=network_policy({**CONTEXT,'mcp_url':'https://backend.example.test/mcp'})
    assert policy['allowed_domains']==['api.anthropic.com','apps.okn.us','github.com',domain,'backend.example.test']
    with pytest.raises(HTTPError):
        NoRedirect().redirect_request(SimpleNamespace(full_url=BASE),None,302,'redirect',{},'https://untrusted.invalid/')


def test_materialization_reuses_all_29_verified_seed_files_without_artifact_get(hosted):
    import hashlib
    from reveal_backend.evidence_package import canonical_json
    research,state,root=hosted
    seed=json.loads(research.seed_path.read_bytes()); sources={}; artifacts=[]; before={}
    for index in range(29):
        raw=canonical_json({'row':index,'precise':'0.12345678901234567890'})
        path=f'sources/original-{index}.json'
        descriptor={'path':path,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw),'format':'json','dapper_file_id':f'dapper:File.{index}'}
        file=research.seed_path.parent/path; file.parent.mkdir(exist_ok=True); file.write_bytes(raw)
        before[file]=raw; sources[str(index)]=descriptor
        artifacts.append({'id':f'source-{index}','filename':path,'sha256':descriptor['sha256'],'size_bytes':len(raw)})
    seed['source_artifacts']=sources; research.seed_path.write_bytes(canonical_json(seed)); before[research.seed_path]=research.seed_path.read_bytes()
    package={**state['export']['package'],'source_artifacts':sources}
    state['export'].update(package=package,artifacts=artifacts,package_sha256=hashlib.sha256(canonical_json(package)).hexdigest(),seed_sha256=hashlib.sha256(research.seed_path.read_bytes()).hexdigest())
    original=research._http; downloads=[]
    def http(url,body=None,maximum=10_000_000):
        if body is None: downloads.append(url)
        return original(url,body,maximum)
    research._http=http
    result=research.materialize()
    assert not downloads
    assert len(list((result.parent/'sources').glob('*.json')))==29
    assert all(path.read_bytes()==raw for path,raw in before.items())
    # A changed immutable original must never be silently replaced by a fetch.
    victim=next(iter(before)); victim.write_bytes(b'changed')
    cached=research.root/sources['0']['path']; cached.unlink()
    with pytest.raises(ResearchAccessError,match='Pinned seed source bytes differ'):
        research.materialize()
    assert not downloads


def test_materialization_deadline_retains_id_package_and_completed_files_for_restart(hosted,monkeypatch):
    import hashlib
    from reveal_backend.evidence_package import canonical_json
    research,state,root=hosted
    package={**state['export']['package'],'source_artifacts':{}}
    raw_sources={}; artifacts=[]
    for index in range(2):
        raw=canonical_json({'row':index}); identity=f'source-{index}'; path=f'sources/{index}.json'
        source={'path':path,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw),'format':'json','dapper_file_id':f'dapper:File.{index}'}
        package['source_artifacts'][identity]=source; raw_sources[identity]=raw
        artifacts.append({'id':identity,'filename':path,'sha256':source['sha256'],'size_bytes':len(raw)})
    package_raw=canonical_json(package); digest=hashlib.sha256(package_raw).hexdigest()
    descriptor={'id':'package','sha256':digest,'size_bytes':len(package_raw),'filename':'evidence-package.json'}
    exported={**state['export'],'format':'reveal.validation-context-export/2','package_sha256':digest,'package_artifact':descriptor,
        'context_sha256':hashlib.sha256(canonical_json(package['validation_context'])).hexdigest(),'artifacts':[*artifacts,descriptor]}
    exported.pop('package')
    tick=[100.0]; downloads=[]; exports=[]; slow=[True]
    monkeypatch.setattr(box_research,'time',SimpleNamespace(monotonic=lambda:tick[0],sleep=lambda delay:None))
    def invoke(tool,args):
        if tool=='export_evidence_context': exports.append(args); return {'structuredContent':{'operation_id':'durable-export','state':'received'}}
        assert tool=='get_operation' and args=={'operation_id':'durable-export'}
        return {'structuredContent':{'id':'durable-export','state':'succeeded','result':exported}}
    def http(url,body=None,maximum=10_000_000):
        identity=url.split('/')[-2]; downloads.append(identity)
        tick[0]+=30 if identity=='source-1' and slow[0] else 1
        return package_raw if identity=='package' else raw_sources[identity]
    research.definitions(); research._invoke=invoke; research._http=http
    with pytest.raises(ResearchAccessError,match='durable-export.*get_operation'):
        research.materialize()
    assert (research.root/'evidence-package.json').read_bytes()==package_raw
    assert (research.root/'sources/0.json').read_bytes()==raw_sources['source-0']
    assert not (research.root/'sources/1.json').exists() and not (research.root/'manifest.json').exists()
    assert downloads==['package','source-0','source-1']
    slow[0]=False
    recovered=HostedResearchClient(CONTEXT,TOKEN,research.root,seed_path=research.seed_path,execution_id='job:1',opener=research._opener)
    recovered.definitions(); recovered._invoke=invoke; recovered._http=http
    assert recovered.materialize().read_bytes()==package_raw
    assert downloads==['package','source-0','source-1','source-1'] and len(exports)==1
    assert (research.root/'manifest.json').is_file()


def test_hosted_lint_passes_only_shared_remaining_budget(hosted,monkeypatch):
    monkeypatch.setattr('reveal_backend.authoring_structure.preflight_document', lambda *a, **k: {'valid': True})
    from reveal_backend import box_remote, scientific_account_lint
    research,_,root=hosted
    state=root/'state'; state.mkdir(); output=root/'output'; output.mkdir()
    (output/'account-1.json').write_text('{}')
    (state/'runtime.json').write_text(json.dumps({'evidence_package':str(research.seed_path),'dapper_root':'fixture'}))
    tick=[100.0]; observed=[]
    def materialize(): tick[0]+=12; return research.seed_path
    monkeypatch.setattr(box_remote,'time',SimpleNamespace(monotonic=lambda:tick[0]))
    monkeypatch.setattr(box_remote,'STATE',state); monkeypatch.setattr(box_remote,'OUTPUT',output)
    monkeypatch.setattr(box_remote,'RESEARCH',SimpleNamespace(materialize=materialize))
    monkeypatch.setattr(scientific_account_lint,'lint_scientific_account',lambda *a,**kwargs: observed.append(kwargs) or {'valid':True})
    assert not box_remote.lint_tool('account-1.json',Ledger(root/'budget-ledger','job',1))['isError']
    assert observed[0]['timeout']==43


@pytest.mark.parametrize(('tool','arguments'),[
    ('write_account_draft',{'filename':'account-1.json','document':{}}),
    ('lint_account',{'filename':'account-1.json'}),
    ('write_outcome',{'outcome':{}}),
])
def test_busy_authoring_returns_prompt_retry_instead_of_waiting_for_outer_timeout(tmp_path,tool,arguments):
    def forbidden(*args): pytest.fail('Must not start a second authoring operation')
    proxy=ScopedTools([],Ledger(tmp_path/'busy-ledger','job',1),write_draft=forbidden,lint=forbidden,write_outcome=forbidden)
    proxy.author_lock.acquire()
    try:
        started=time.monotonic()
        result=proxy.call(tool,arguments)
        assert time.monotonic()-started<.1 and result['isError']
        assert 'still running' in result['content'][0]['text']
    finally: proxy.author_lock.release()


def test_catalog_discovery_is_inside_the_outer_call_deadline(hosted):
    research,_,root=hosted
    release=threading.Event(); finished=threading.Event(); original=research.definitions
    def slow_definitions():
        try:
            assert release.wait(2)
            return original()
        finally: finished.set()
    research.definitions=slow_definitions
    proxy=ScopedTools([],Ledger(root/'catalog-ledger','job',1),research=research,read_timeout=.05)
    started=time.monotonic()
    try:
        result=proxy.call('get_local_work',{})
        assert time.monotonic()-started<.3 and result['isError']
        assert result['structuredContent']['recovery']=={'tool':'get_local_work','arguments':{}}
    finally:
        release.set(); assert finished.wait(1)
