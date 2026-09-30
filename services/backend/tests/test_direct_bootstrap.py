"""S3→Box bootstrap preserves frozen validation and avoids API workspace restores."""
from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from reveal_backend import box_upload as helper, direct_bootstrap as bootstrap
from reveal_backend.agent_execution import ExecutionRequest
from reveal_backend.artifact_store import S3Store
from reveal_backend.box_adapter import BoxConfigurationError, BoxTransportError, make_bundle
from reveal_backend.box_lifecycle import BoxLifecycle
from reveal_backend.runtime_config import ROOT
from test_deployment import MemoryS3
from test_direct_capture import signer, run_async


@pytest.fixture
def frozen(tmp_path):
    raw=b'{"format":"reveal.paragraph-input/1","account_document":{},"allowed_citations":[]}'
    source=tmp_path/'paragraph.json';source.write_bytes(raw)
    request=ExecutionRequest('direct-bootstrap',1,'paragraph',source,tmp_path/'output',timeout_seconds=60,max_turns=2)
    adapter=BoxLifecycle(ROOT,environ={'ANTHROPIC_API_KEY':'synthetic-key','UPSTASH_BOX_API_KEY':'synthetic-box'})
    storage=S3Store('reveal-test-artifacts','qa/',client=MemoryS3(),signer=signer())
    descriptor=adapter.freeze_bootstrap(request,storage)
    handle={'job_id':request.job_id,'attempt':1,'box_id':'isolated','phase':'created','timings':{}}
    return adapter,storage,descriptor,handle,request


def helper_plan(storage,descriptor):
    return {'descriptor':descriptor,**bootstrap.download_ticket(storage,descriptor['bundle'])}


def fake_https(monkeypatch,raw,version,*,status=200,length=None):
    observations=[]
    class Connection:
        def __init__(self,host,timeout):self.body=io.BytesIO(raw);observations.append({'host':host,'timeout':timeout})
        def request(self,method,path):observations[-1].update(method=method,path=path)
        def getresponse(self):
            return SimpleNamespace(status=status,read=self.body.read,
                getheader=lambda key:{'Content-Length':str(len(raw) if length is None else length),'x-amz-version-id':version}.get(key))
        def close(self):observations[-1]['closed']=True
    monkeypatch.setattr(helper.http.client,'HTTPSConnection',Connection)
    return observations


def box_root(tmp_path):
    root=tmp_path/'remote';(root/'state').mkdir(parents=True);return root


def test_frozen_descriptor_has_no_capability_and_bundle_is_deterministic(frozen,monkeypatch):
    adapter,storage,descriptor,_,request=frozen
    first=storage.get(descriptor['bundle'])
    monkeypatch.setattr('time.time',lambda:1234567890)
    assert make_bundle(ROOT,request)==first
    monkeypatch.setattr('time.time',lambda:9876543210)
    assert make_bundle(ROOT,request)==first
    again=adapter.freeze_bootstrap(request,storage)
    assert again==descriptor and 'url' not in json.dumps(descriptor)
    assert descriptor['fingerprint']==hashlib.sha256(first+json.dumps(descriptor['config'],sort_keys=True).encode()).hexdigest()
    assert first[4:8]==b'\0\0\0\0'  # Explicit fixed gzip mtime.


def test_freeze_keeps_scientific_validation_before_any_s3_put(frozen,monkeypatch):
    adapter,storage,_,_,request=frozen
    request.input_path.write_text('{"format":"untrusted"}')
    monkeypatch.setattr(storage,'put',lambda *args:pytest.fail('invalid evidence must not become a bootstrap'))
    with pytest.raises(BoxConfigurationError,match='paragraph'):adapter.freeze_bootstrap(request,storage)


def test_actual_offline_get_signature_pins_immutable_version(frozen):
    _,storage,descriptor,_,_=frozen
    ticket=bootstrap.download_ticket(storage,descriptor['bundle']);query=parse_qs(urlsplit(ticket['url']).query)
    assert ticket['host']=='reveal-test-artifacts.s3.amazonaws.com'
    assert query['versionId']==[descriptor['bundle']['version_id']]
    assert query['X-Amz-Expires']==['300'] and query['X-Amz-SignedHeaders']==['host']


@pytest.mark.parametrize('replacement',['https://evil.example/path','http://reveal-test-artifacts.s3.amazonaws.com/path',
    'https://reveal-test-artifacts.s3.amazonaws.com/path?versionId=other'])
def test_get_ticket_rejects_unpinned_or_foreign_objects(frozen,replacement):
    _,storage,descriptor,_,_=frozen
    storage.signer=SimpleNamespace(generate_presigned_url=lambda *args,**kwargs:replacement)
    with pytest.raises(BoxTransportError):bootstrap.download_ticket(storage,descriptor['bundle'])


def test_root_download_verifies_and_installs_exact_input_then_replays_without_network(frozen,tmp_path,monkeypatch):
    _,storage,descriptor,_,request=frozen
    plan=helper_plan(storage,descriptor);raw=storage.get(descriptor['bundle']);root=box_root(tmp_path)
    observations=fake_https(monkeypatch,raw,descriptor['bundle']['version_id'])
    assert helper.bootstrap(root,plan)=={'fingerprint':descriptor['fingerprint']}
    assert (root/'input/paragraph-input.json').read_bytes()==request.input_path.read_bytes()
    assert json.loads((root/'request.json').read_bytes())==descriptor['config']
    assert (root/'bundle/services/backend/src/reveal_backend/box_remote.py').is_file()
    assert observations[0]['closed'] and observations[0]['method']=='GET'
    assert parse_qs(urlsplit(observations[0]['path']).query)['versionId']==[descriptor['bundle']['version_id']]
    monkeypatch.setattr(helper.http.client,'HTTPSConnection',lambda *args,**kwargs:pytest.fail('verified download must replay'))
    assert helper.bootstrap(root,plan)=={'fingerprint':descriptor['fingerprint']}
    assert not list(root.glob('.bootstrap-*'))
    assert 'X-Amz' not in (root/'request.json').read_text()


@pytest.mark.parametrize('fault',['checksum','version','length','redirect','config','started','different-request'])
def test_failed_download_or_changed_execution_never_installs_or_marks_ready(frozen,tmp_path,monkeypatch,fault):
    _,storage,descriptor,_,_=frozen;plan=helper_plan(storage,descriptor);raw=storage.get(descriptor['bundle'])
    root=box_root(tmp_path);version=descriptor['bundle']['version_id'];length=None;status=200
    if fault=='checksum':raw=bytes([raw[0]^1])+raw[1:]
    if fault=='version':version='different-version'
    if fault=='length':length=len(raw)+1
    if fault=='redirect':status=307
    if fault=='config':plan=deepcopy(plan);plan['descriptor']['config']['model']='changed'
    if fault=='started':(root/'state/status.json').write_text('{"status":"running"}')
    if fault=='different-request':(root/'request.json').write_text('{"job_id":"other"}')
    fake_https(monkeypatch,raw,version,status=status,length=length)
    with pytest.raises(ValueError):helper.bootstrap(root,plan)
    assert not (root/'input').exists() and not (root/'bundle').exists()
    assert not (root/'state/bootstrap-downloaded').exists() and not list(root.glob('.bootstrap-*'))


def malicious_archive(entries):
    raw=io.BytesIO()
    with tarfile.open(fileobj=raw,mode='w:gz') as archive:
        for name,kind,content in entries:
            info=tarfile.TarInfo(name);info.type=kind
            info.size=len(content) if kind==tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE,tarfile.LNKTYPE):info.linkname='/etc/passwd'
            archive.addfile(info,io.BytesIO(content) if info.size else None)
    return raw.getvalue()


@pytest.mark.parametrize('entries',[
    [('../escape',tarfile.REGTYPE,b'x')], [('/absolute',tarfile.REGTYPE,b'x')],
    [('state/bootstrap-ready',tarfile.REGTYPE,b'x')], [('input/link',tarfile.SYMTYPE,b'')],
    [('input/link',tarfile.LNKTYPE,b'')], [('input/fifo',tarfile.FIFOTYPE,b'')],
    [('input/file',tarfile.REGTYPE,b'x'),('input/file',tarfile.REGTYPE,b'x')],
    [('input/a',tarfile.REGTYPE,b'x'),('input/a/child',tarfile.REGTYPE,b'x')],
    [('input/a/child',tarfile.REGTYPE,b'x'),('input/a',tarfile.REGTYPE,b'x')]])
def test_unsafe_tar_members_never_escape_private_stage(tmp_path,entries):
    archive=tmp_path/'archive.tgz';archive.write_bytes(malicious_archive(entries));target=tmp_path/'stage';target.mkdir()
    with pytest.raises((ValueError,OSError)):
        helper.extract_bundle(archive,target,{'kind':'paragraph','input_sha256':'0'*64})
    assert not (tmp_path/'escape').exists()


def test_tar_expansion_is_bounded_even_before_extended_headers_are_parsed(tmp_path,monkeypatch):
    archive=tmp_path/'archive.tgz'
    archive.write_bytes(malicious_archive([('input/'+'a'*1000,tarfile.REGTYPE,b'x')]))
    target=tmp_path/'stage';target.mkdir();monkeypatch.setattr(helper,'MAX_EXPANDED_BUNDLE',100)
    with pytest.raises(ValueError,match='Expanded bootstrap'):helper.extract_bundle(archive,target,{'kind':'paragraph','input_sha256':'0'*64})


def test_interrupted_tree_install_retries_same_verified_bundle(frozen,tmp_path,monkeypatch):
    _,storage,descriptor,_,request=frozen;plan=helper_plan(storage,descriptor)
    root=box_root(tmp_path);raw=storage.get(descriptor['bundle'])
    fake_https(monkeypatch,raw,descriptor['bundle']['version_id'])
    original=Path.replace;interrupted=False
    def replace(path,target):
        nonlocal interrupted
        if path.name=='input' and not interrupted:
            interrupted=True;raise OSError('simulated interrupted installation')
        return original(path,target)
    monkeypatch.setattr(Path,'replace',replace)
    with pytest.raises(OSError,match='interrupted'):helper.bootstrap(root,plan)
    assert (root/'bundle').is_dir() and not (root/'state/bootstrap-downloaded').exists()
    assert helper.bootstrap(root,plan)=={'fingerprint':descriptor['fingerprint']}
    assert (root/'input/paragraph-input.json').read_bytes()==request.input_path.read_bytes()


def test_downloaded_marker_cannot_hide_changed_configuration(frozen,tmp_path,monkeypatch):
    _,storage,descriptor,_,_=frozen;plan=helper_plan(storage,descriptor);root=box_root(tmp_path)
    fake_https(monkeypatch,storage.get(descriptor['bundle']),descriptor['bundle']['version_id'])
    helper.bootstrap(root,plan)
    changed=deepcopy(plan);changed['descriptor']['config']['model']='different-model'
    monkeypatch.setattr(helper.http.client,'HTTPSConnection',lambda *args,**kwargs:pytest.fail('must fail before network'))
    with pytest.raises(ValueError,match='another execution'):helper.bootstrap(root,changed)


@run_async
async def test_lost_policy_ack_retries_same_sentinel_without_download_restore_or_reinstall(frozen,monkeypatch):
    adapter,storage,descriptor,handle,_=frozen;sentinel='';commands=[]
    box=SimpleNamespace(files=SimpleNamespace(write=AsyncMock()),aclose=AsyncMock(),
        update_network_policy=AsyncMock(side_effect=[OSError('lost acknowledgement'),None]))
    adapter.connect=AsyncMock(return_value=box)
    async def command(_box,value):
        nonlocal sentinel
        commands.append(value)
        if 'if [ -f' in value:return sentinel
        if 'printf' in value:sentinel=descriptor['fingerprint']
        return ''
    adapter.command=AsyncMock(side_effect=command)
    session=AsyncMock(return_value={'fingerprint':descriptor['fingerprint']})
    monkeypatch.setattr(bootstrap,'remote_session',session)
    monkeypatch.setattr(storage,'get',lambda *args:pytest.fail('API must not download bundle'))
    monkeypatch.setattr(storage,'restore',lambda *args:pytest.fail('API must not restore workspace'))
    with pytest.raises(OSError):await adapter.prepare_from_store(descriptor,handle,storage)
    count=box.files.write.await_count
    assert (await adapter.prepare_from_store(descriptor,handle,storage))['phase']=='prepared'
    assert box.files.write.await_count==count
    assert commands.count('sh /tmp/reveal-bootstrap.sh')==1
    session.assert_awaited_once()
    assert session.call_args.kwargs['python']=='/usr/bin/python3'
    assert session.call_args.args[1]=='bootstrap'
    assert box.aclose.await_count==2


@run_async
async def test_different_ready_sentinel_or_launched_handle_cannot_be_overwritten(frozen,monkeypatch):
    adapter,storage,descriptor,handle,_=frozen
    box=SimpleNamespace(aclose=AsyncMock());adapter.connect=AsyncMock(return_value=box)
    adapter.command=AsyncMock(return_value='different')
    monkeypatch.setattr(bootstrap,'remote_session',AsyncMock(side_effect=AssertionError('must not transfer')))
    with pytest.raises(BoxConfigurationError):await adapter.prepare_from_store(descriptor,handle,storage)
    with pytest.raises(BoxTransportError):await adapter.prepare_from_store(descriptor,dict(handle,phase='running'),storage)
    assert adapter.connect.await_count==1
