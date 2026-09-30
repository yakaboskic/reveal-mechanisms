"""The root transfer helper freezes bounded regular files and keeps tickets private."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from reveal_backend import box_upload as upload
from reveal_backend import direct_capture as capture
from test_direct_capture import BINDING, STATE, staged


def fixture(root):
    for name in ('output','state/ledger'):(root/name).mkdir(parents=True)
    (root/'request.json').write_text(json.dumps({k:v for k,v in BINDING.items() if k!='box_id'}))
    (root/'state/status.json').write_text(json.dumps(STATE))
    (root/'state/runtime.json').write_bytes(b'{"cost_usd":0}')
    (root/'output/account.json').write_bytes(b'original bytes')
    (root/'state/ledger/manifest.json').write_bytes(b'{"complete":true}')
    return root


def test_inventory_freezes_private_bytes_and_retry_reuses_original(tmp_path):
    root=fixture(tmp_path)
    first=upload.inventory(root,BINDING)
    assert first['binding']==BINDING and first['state']==STATE
    private=root/'state/direct-capture-v1'
    assert private.stat().st_mode & 0o777 == 0o700
    for item in first['files']:
        path=private/'files'/item['path']
        assert path.stat().st_mode & 0o777 == 0o400
        assert path.stat().st_size==item['size_bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']
    (root/'output/account.json').write_bytes(b'mutated author file')
    assert upload.inventory(root,BINDING)==first
    assert (private/'files/output/account.json').read_bytes()==b'original bytes'
    with pytest.raises(ValueError,match='another execution'):
        upload.inventory(root,dict(BINDING,job_id='another'))
    with pytest.raises(ValueError,match='binding changed'):
        upload.inventory(root,dict(BINDING,box_id='another'))


@pytest.mark.parametrize('kind',['symlink','parent-link','fifo','oversize','runtime-budget','count','active'])
def test_inventory_rejects_unsafe_and_unbounded_sources(tmp_path,monkeypatch,kind):
    root=fixture(tmp_path)
    if kind=='symlink': (root/'output/link').symlink_to('/etc/passwd')
    if kind=='parent-link': (root/'output/directory').symlink_to(root/'state/ledger',target_is_directory=True)
    if kind=='fifo': os.mkfifo(root/'output/fifo')
    if kind=='oversize': monkeypatch.setattr(upload,'MAX_FILE',2)
    if kind=='runtime-budget': monkeypatch.setattr(upload,'MAX_TOTAL',len(b'original bytes')+len(b'{"complete":true}'))
    if kind=='count': monkeypatch.setattr(upload,'MAX_FILES',2)
    if kind=='active': (root/'state/status.json').write_text('{"status":"running"}')
    with pytest.raises((ValueError,OSError)): upload.inventory(root,BINDING)
    assert not (root/'state/direct-capture-v1').exists()
    assert not list((root/'state').glob('.capture-*'))


@pytest.mark.parametrize('name',['../escape','/absolute','a/../b','a//b','a\\b','a\x00b'])
def test_secure_open_rejects_unsafe_paths(tmp_path,name):
    with pytest.raises(ValueError): upload.source_file(tmp_path,name)


def test_upload_streams_original_file_and_sends_signed_constraints_without_redirect(tmp_path,monkeypatch):
    root=fixture(tmp_path); inv=upload.inventory(root,BINDING)
    storage,_,_,_=staged(); host,tickets=capture.tickets_for(storage,inv['files']); ticket=tickets[0]
    observed={}
    class Connection:
        def __init__(self,hostname,timeout): observed.update(host=hostname,timeout=timeout)
        def request(self,method,path,body,headers): observed.update(method=method,path=path,body=body.read(),headers=headers)
        def getresponse(self):return SimpleNamespace(status=200,read=lambda count:b'',getheader=lambda key:'immutable-v1')
        def close(self):observed['closed']=True
    monkeypatch.setattr(upload.http.client,'HTTPSConnection',Connection)
    result=upload.upload_one(root/'state/direct-capture-v1/files',ticket,host)
    assert result=={'path':ticket['path'],'status':200,'version_id':'immutable-v1'}
    assert observed['host']==host and observed['method']=='PUT' and observed['headers']==ticket['headers']
    assert hashlib.sha256(observed['body']).hexdigest()==ticket['sha256'] and observed['closed']
    # HTTP redirects are errors; capabilities never follow a remote Location.
    Connection.getresponse=lambda _:SimpleNamespace(status=307,read=lambda count:b'provider body',getheader=lambda key:'https://evil.example')
    with pytest.raises(ValueError,match='Direct upload failed'):
        upload.upload_one(root/'state/direct-capture-v1/files',ticket,host)


def test_corrupted_frozen_bytes_or_foreign_destination_never_reach_network(tmp_path,monkeypatch):
    root=fixture(tmp_path); inv=upload.inventory(root,BINDING)
    storage,_,_,_=staged(); host,tickets=capture.tickets_for(storage,inv['files']); ticket=tickets[0]
    monkeypatch.setattr(upload.http.client,'HTTPSConnection',lambda *args,**kwargs:pytest.fail('must reject before network'))
    with pytest.raises(ValueError,match='destination'):
        upload.upload_one(root/'state/direct-capture-v1/files',dict(ticket,url='https://evil.example'),host)
    path=root/'state/direct-capture-v1/files'/ticket['path']; path.chmod(0o600); path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='Frozen capture changed'):
        upload.upload_one(root/'state/direct-capture-v1/files',ticket,host)


def test_upload_batch_requires_unique_exact_inventory_members(tmp_path,monkeypatch):
    root=fixture(tmp_path); inv=upload.inventory(root,BINDING)
    storage,_,_,_=staged(); host,tickets=capture.tickets_for(storage,inv['files'])
    monkeypatch.setattr(upload,'upload_one',lambda root,item,host:{'path':item['path']})
    plan={'binding':BINDING,'host':host,'tickets':tickets[:1]}
    assert upload.upload(root,plan)=={'receipts':[{'path':tickets[0]['path']}]}
    for invalid in [[],[tickets[0],tickets[0]],[dict(tickets[0],path='unknown')],[dict(tickets[0],sha256='0'*64)]]:
        with pytest.raises(ValueError):upload.upload(root,dict(plan,tickets=invalid))


def test_upload_workers_are_bounded_and_all_settle_before_failure(tmp_path,monkeypatch):
    root=fixture(tmp_path)
    for index in range(8):(root/'output'/str(index)).write_bytes(str(index).encode())
    inv=upload.inventory(root,BINDING);storage,_,_,_=staged();host,tickets=capture.tickets_for(storage,inv['files'])
    filled,release,lock=threading.Event(),threading.Event(),threading.Lock()
    observed={'active':0,'peak':0,'finished':0}
    def transfer(root,item,host):
        with lock:
            observed['active']+=1;observed['peak']=max(observed['peak'],observed['active'])
            if observed['active']==4:filled.set()
        try:
            assert release.wait(5)
            if item==tickets[0]:raise ValueError('synthetic failure')
            return {'path':item['path']}
        finally:
            with lock:observed['active']-=1;observed['finished']+=1
    monkeypatch.setattr(upload,'upload_one',transfer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result=pool.submit(upload.upload,root,{'binding':BINDING,'host':host,'tickets':tickets})
        try:assert filled.wait(3)
        finally:release.set()
        with pytest.raises(ValueError,match='synthetic failure'):result.result(timeout=5)
    assert observed['active']==0 and observed['peak']==4
