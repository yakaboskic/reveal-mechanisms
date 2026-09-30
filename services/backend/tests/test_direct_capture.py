"""Direct Box→S3 transport may acknowledge only complete immutable evidence."""
import asyncio
import base64
from copy import deepcopy
import hashlib
from functools import wraps
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import boto3
from botocore.config import Config
import pytest

from reveal_backend import direct_capture as capture
from reveal_backend.artifact_store import S3Store, StorageUnavailable
from reveal_backend.box_adapter import BoxTransportError, CAPTURE_MARKER
from test_deployment import MemoryS3


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs): return asyncio.run(function(*args, **kwargs))
    return run


BINDING = {'job_id':'job-direct', 'attempt':1, 'kind':'research', 'box_id':'box-direct',
           'selected_graphs':['dismech'], 'input_sha256':hashlib.sha256(b'input').hexdigest()}
STATE = {'status':'succeeded', 'reason':None, 'completed_at':'frozen'}
HANDLE = {**{key:BINDING[key] for key in ('job_id','attempt','box_id')},
          'state':STATE, 'phase':'terminal', 'capture_protocol':'s3-v1', 'timings':{}}


def signer(**kwargs):
    return boto3.client('s3', region_name='us-east-1', aws_access_key_id='offline-test',
        aws_secret_access_key='offline-test-secret', config=Config(signature_version='s3v4', **kwargs))


def descriptor(path, data):
    return {'path':path, 'sha256':hashlib.sha256(data).hexdigest(), 'size_bytes':len(data)}


def fixture_bytes(**extra):
    request, response = b'{"query":"frozen evidence"}', b'{"rows":[]}'
    ledger = {'format':'reveal.tool-ledger/1', 'complete':True, 'job_id':BINDING['job_id'], 'attempt':1,
        'calls':[{'request':descriptor('request.json',request), 'response':descriptor('response.json',response)}]}
    return {'output/account.json':b'{"unchanged":"scientific output"}', 'ledger/request.json':request,
        'ledger/response.json':response, 'ledger/manifest.json':capture.canonical(ledger),
        'runtime.json':b'{"cost_usd":0}', **extra}


def staged(data=None):
    storage = S3Store('reveal-test-artifacts', 'qa/', client=MemoryS3(), signer=signer())
    data = data or fixture_bytes()
    files, receipts, refs = [], [], {}
    for name, raw in data.items():
        item = descriptor(name,raw); files.append(item)
        refs[name] = storage.put(raw)
        receipts.append({'path':name,'status':200,'version_id':refs[name]['version_id']})
    return storage, files, receipts, refs


def inventory(files):
    return {'format':'reveal.direct-capture/1','binding':deepcopy(BINDING),'state':deepcopy(STATE),'files':deepcopy(files)}


def previous_workspace(storage, **files):
    records = [{'path':name,'storage':storage.put(raw)} for name, raw in files.items()]
    return storage.put(capture.canonical({'format':'reveal.workspace/1','files':records}))


def test_real_boto_signer_signs_conditional_checksum_length_encryption_and_exact_bucket():
    storage, files, _, _ = staged()
    host, tickets = capture.tickets_for(storage, files)
    assert host == 'reveal-test-artifacts.s3.amazonaws.com'  # Real us-east-1 default.
    for item, ticket in zip(files, tickets):
        parsed = urlsplit(ticket['url']); query = parse_qs(parsed.query)
        assert parsed.hostname == host and query['X-Amz-Expires'] == ['300']
        assert set(query['X-Amz-SignedHeaders'][0].split(';')) == {
            'host','content-length','content-type','if-none-match','x-amz-checksum-sha256','x-amz-server-side-encryption'}
        assert parsed.path == '/qa/artifacts/sha256/' + item['sha256'][:2] + '/' + item['sha256']
        assert ticket['headers']['If-None-Match'] == '*'
        assert ticket['headers']['Content-Length'] == str(item['size_bytes'])
        assert ticket['headers']['x-amz-checksum-sha256'] == base64.b64encode(bytes.fromhex(item['sha256'])).decode()
        assert ticket['headers']['x-amz-server-side-encryption'] == 'AES256'


@pytest.mark.parametrize('url', ['http://reveal-test-artifacts.s3.amazonaws.com/path',
    'https://evil.example/path','https://reveal-test-artifacts.s3.amazonaws.com.evil/path',
    'https://reveal-test-artifacts.s3.amazonaws.com/path#fragment',
    'https://user@reveal-test-artifacts.s3.amazonaws.com/path'])
def test_signer_cannot_expand_egress(url):
    storage, files, _, _ = staged()
    storage.signer = SimpleNamespace(generate_presigned_url=lambda *args, **kwargs:url)
    with pytest.raises(BoxTransportError, match='host'): capture.tickets_for(storage,files)


@pytest.mark.parametrize('mutation', [lambda x:x.update(binding={}), lambda x:x.update(state={'status':'running'}),
    lambda x:x.update(files=[None]), lambda x:x.update(files=[]),
    lambda x:x['files'].append(dict(x['files'][0])),
    lambda x:x['files'][0].update(path='../escape'), lambda x:x['files'][0].update(path='output/a//b'),
    lambda x:x['files'][0].update(path='output/\x00'), lambda x:x['files'][0].update(size_bytes=True),
    lambda x:x['files'][0].update(sha256='bad'), lambda x:x['files'][0].update(size_bytes=8_000_001),
    lambda x:x['files'].append(descriptor('output/account.json/child', b'x'))])
def test_inventory_rejects_malformed_binding_paths_sizes_and_parent_collisions(mutation):
    _,files,_,_ = staged(); value=inventory(files); mutation(value)
    with pytest.raises(BoxTransportError): capture.validate_inventory(value,BINDING,STATE)


def test_verified_versions_survive_latest_overwrite_and_retry_412():
    storage, files, receipts, refs = staged()
    assert capture.verify_uploads(storage,files,receipts,BINDING,()) == refs
    # A dropped PUT acknowledgement is safely recovered via conditional retry.
    retried = [dict(row,status=412,version_id=None) for row in receipts]
    assert capture.verify_uploads(storage,files,retried,BINDING,()) == refs
    first = refs[files[0]['path']]
    storage.client.put_object(Key=first['key'],Body=b'different latest bytes')
    assert capture.verify_uploaded(storage,files[0],receipts[0],())[1] == first
    with pytest.raises(BoxTransportError, match='checksum'):
        capture.verify_uploaded(storage,files[0],retried[0],())


@pytest.mark.parametrize('field,value', [('ContentLength',999),('ChecksumSHA256','wrong'),('VersionId','null'),('VersionId','another-version')])
def test_head_mismatch_cannot_acknowledge_capture(field,value):
    storage, files, receipts, _ = staged(); original=storage.client.head_object
    storage.client.head_object=lambda **kwargs:{**original(**kwargs),field:value}
    with pytest.raises(BoxTransportError): capture.verify_uploaded(storage,files[0],receipts[0],())


def test_get_corruption_and_cross_chunk_secrets_are_rejected():
    secret=b'synthetic-known-credential'; raw=b'a'*(65536-7)+secret+b'end'
    storage,files,receipts,refs=staged({'output/large':raw})
    with pytest.raises(BoxTransportError,match='credential'): capture.verify_uploaded(storage,files[0],receipts[0],(secret,))
    assert capture.verify_uploaded(storage,files[0],receipts[0],())[1] == refs['output/large']
    storage.client.get_object=lambda **kwargs:{'Body':io.BytesIO(b'x'*len(raw))}
    with pytest.raises(BoxTransportError,match='bytes differ'): capture.verify_uploaded(storage,files[0],receipts[0],())


@pytest.mark.parametrize('mutation', [lambda x:x.update(job_id='another-job'),lambda x:x.update(complete=False),
    lambda x:x.update(calls=[None]),lambda x:x['calls'][0].pop('request'),
    lambda x:x['calls'][0]['response'].update(path='../escape'),
    lambda x:x['calls'][0]['response'].update(sha256='0'*64)])
def test_ledger_must_match_trusted_binding_and_exact_uploaded_descriptors(mutation):
    data=fixture_bytes(); ledger=json.loads(data['ledger/manifest.json']); mutation(ledger)
    data['ledger/manifest.json']=capture.canonical(ledger)
    storage,files,receipts,_=staged(data)
    with pytest.raises(BoxTransportError): capture.verify_uploads(storage,files,receipts,BINDING,())


def test_composer_keeps_input_versions_replaces_only_current_output_and_never_restores(tmp_path,monkeypatch):
    storage,files,receipts,refs=staged()
    prior=previous_workspace(storage, **{'attempt-1/input.json':b'input', 'attempt-1/output/stale.txt':b'stale',
        'attempt-2/output/keep.txt':b'other attempt'})
    before={row['path']:row['storage'] for row in storage.workspace_manifest(prior)['files']}
    monkeypatch.setattr(storage,'restore',lambda *args:pytest.fail('capture must not restore workspace'))
    monkeypatch.setattr(Path,'write_bytes',lambda *args:pytest.fail('capture must not write local files'))
    result=capture.compose_workspace(storage,prior,BINDING,HANDLE,files,refs)
    after={row['path']:row['storage'] for row in storage.workspace_manifest(result['workspace'])['files']}
    assert after['attempt-1/input.json']==before['attempt-1/input.json']
    assert after['attempt-2/output/keep.txt']==before['attempt-2/output/keep.txt']
    assert 'attempt-1/output/stale.txt' not in after
    for name,ref in refs.items(): assert after['attempt-1/output/'+name]==ref
    raw=storage.get(after['attempt-1/output/'+CAPTURE_MARKER]); marker=json.loads(raw)
    assert hashlib.sha256(raw).hexdigest()==result['capture_sha256']
    assert marker['binding']==BINDING and marker['cleanup_complete'] is False and marker['state']==STATE
    assert result['box']['phase']=='captured'


@pytest.mark.parametrize('cause',['parent','budget'])
def test_composition_invalid_parent_or_budget_fails_before_any_put(monkeypatch,cause):
    storage,files,_,refs=staged()
    prior=previous_workspace(storage, **({'attempt-1/output':b'parent'} if cause=='parent' else {'input.json':b'input'}))
    if cause=='budget': monkeypatch.setenv('REVEAL_WORKSPACE_MAX_BYTES','400')
    monkeypatch.setattr(storage,'put',lambda *args:pytest.fail('must validate before PUT'))
    with pytest.raises(BoxTransportError): capture.compose_workspace(storage,prior,BINDING,HANDLE,files,refs)


@run_async
async def test_session_keeps_capabilities_off_argv_chunks_stdin_and_closes():
    captured={}; parts=[]
    session=SimpleNamespace(write=AsyncMock(side_effect=lambda raw:parts.append(raw)),end_stdin=AsyncMock(),
        wait=AsyncMock(return_value=0),close=AsyncMock())
    async def create(**kwargs):
        captured.update(kwargs); kwargs['on_stdout'](b'{"receipts":[]}'); return session
    plan={'url':'https://private.example/?signature=PRIVATE-TICKET','padding':'a'*200000}
    result=await capture.remote_session(SimpleNamespace(exec=SimpleNamespace(session=create)),'upload',plan)
    assert result=={'receipts':[]} and json.loads(b''.join(parts))==plan
    assert max(map(len,parts))<=65536 and len(parts)>1
    assert 'PRIVATE-TICKET' not in ' '.join(captured['argv'])
    assert captured['tty'] is False
    session.end_stdin.assert_awaited_once();session.close.assert_awaited_once()


@run_async
async def test_session_transport_failure_is_sanitized_and_closes():
    session=SimpleNamespace(write=AsyncMock(side_effect=RuntimeError('PRIVATE-SIGNED-URL')),close=AsyncMock())
    box=SimpleNamespace(exec=SimpleNamespace(session=AsyncMock(return_value=session)))
    with pytest.raises(BoxTransportError,match='transport interrupted') as error:
        await capture.remote_session(box,'upload',{'binding':BINDING})
    assert 'PRIVATE' not in str(error.value); session.close.assert_awaited_once()


@run_async
async def test_direct_capture_batches_narrows_egress_and_commits_without_local_workspace(monkeypatch):
    storage,files,receipts,refs=staged(); prior=previous_workspace(storage, **{'input.json':b'input'})
    calls=[]; box=SimpleNamespace(update_network_policy=AsyncMock(),aclose=AsyncMock())
    adapter=SimpleNamespace(connect=AsyncMock(return_value=box),
        environ={'ANTHROPIC_API_KEY':'synthetic-anthropic-key','UPSTASH_BOX_API_KEY':'synthetic-box-key'})
    async def remote(_box,action,plan):
        calls.append((action,plan))
        if action=='inventory': return inventory(files)
        return {'receipts':[row for row in receipts if row['path'] in {item['path'] for item in plan['tickets']}]}
    monkeypatch.setattr(capture,'remote_session',remote);monkeypatch.setattr(capture,'UPLOAD_BATCH',2)
    result=await capture.capture_to_store(adapter,BINDING,HANDLE,storage,prior)
    assert result['box']['phase']=='captured' and len(calls)==4
    box.update_network_policy.assert_awaited_once_with({'mode':'custom','allowed_domains':['reveal-test-artifacts.s3.amazonaws.com']})
    assert all(len(plan['tickets'])<=2 for action,plan in calls if action=='upload')
    assert 'signature' not in json.dumps(result).lower()
    box.aclose.assert_awaited_once()


@run_async
async def test_legacy_or_live_handles_cannot_enter_direct_path():
    adapter=SimpleNamespace(connect=AsyncMock())
    for handle in [dict(HANDLE,capture_protocol=None),dict(HANDLE,state={'status':'running'})]:
        with pytest.raises(BoxTransportError): await capture.capture_to_store(adapter,BINDING,handle,None,None)
    adapter.connect.assert_not_awaited()
