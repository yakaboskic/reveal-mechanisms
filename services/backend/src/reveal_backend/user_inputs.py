"""Private, owner-scoped research inputs and immutable upload captures.

The editable upload record is not the authority after submission: requests pin
the verified original and extraction objects, independently of draft lifetime.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

from .auth import Problem, owned
from .repository import now, uid
from .artifact_store import s3_enabled, store, checksum
from .runtime_config import artifacts_root

MAX_FILE_BYTES = 8_000_000
MAX_UPLOAD_BYTES = 16_000_000
MAX_TEXT_BYTES = 500_000
MAX_EXTRACTED_BYTES = 1_000_000
INPUT_FIELDS = ('research_direction', 'context', 'hypotheses')
TYPES = {'.txt':'text/plain', '.md':'text/markdown', '.csv':'text/csv', '.tsv':'text/tab-separated-values',
         '.json':'application/json', '.yaml':'application/yaml', '.yml':'application/yaml',
         '.pdf':'application/pdf', '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}


def expiration(hours=24):
    return (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat().replace('+00:00','Z')


def saved(draft):
    return draft.get('lifecycle', 'saved') == 'saved'


def available(draft):
    if not saved(draft) and draft.get('expires_at', '') <= now():
        raise Problem(410, 'EDITOR_EXPIRED', 'This temporary editor expired. Open the knowledge gap to start again.')
    return draft


def has_inputs(value):
    return bool(value and (any(value.get(k) for k in INPUT_FIELDS) or value.get('uploads') or value.get('upload_ids')))


def prevent_private_publication(tx, job_id):
    job=tx.get('job',job_id) if job_id else None
    local=tx.get('local_work',job_id) if job_id and not job else None
    origin=job or local
    request=tx.get('request',origin['data'].get('research_request_id')) if origin else None
    if request and has_inputs(request['data'].get('user_inputs')):
        raise Problem(409,'PRIVATE_RESEARCH_INPUTS','This result used private researcher inputs. Public disclosure is not enabled for these results.')


def public_upload(value):
    return {key: deepcopy(value.get(key)) for key in ('id','draft_id','filename','media_type','size_bytes','sha256',
        'status','created_at','expires_at','storage','extraction','error')}


def retain(data, media_type):
    if s3_enabled(): return store().put(data, media_type)
    sha = checksum(data); path = artifacts_root()/'user-inputs'/sha
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists(): path.write_bytes(data)
    return {'store':'filesystem','key':str(path),'sha256':sha,'size_bytes':len(data),'content_type':media_type}


def read(ref):
    if ref.get('store') == 's3': return store().get(ref)
    path = Path(ref['key']).resolve()
    if not path.is_relative_to((artifacts_root()/'user-inputs').resolve()): raise ValueError('Invalid input storage path')
    data = path.read_bytes()
    if checksum(data) != ref['sha256'] or len(data) != ref['size_bytes']: raise ValueError('Input checksum changed')
    return data


def initiate(tx, owner, body):
    draft = available(owned(tx,'draft',body['draft_id'],owner)['data'])
    filename = body['filename']
    if Path(filename).name != filename or '\\' in filename or any(ord(c)<32 for c in filename):
        raise Problem(422,'INVALID_UPLOAD','Use a plain filename without path components.')
    media = TYPES.get(Path(filename).suffix.lower())
    if not media: raise Problem(422,'UNSUPPORTED_UPLOAD','Supported files: text, Markdown, CSV, TSV, JSON, YAML, PDF and DOCX.')
    recent=[r['data'] for r in tx.list('upload',owner) if r['data'].get('expires_at','')>now()]
    active = [r for r in recent if r['draft_id']==draft['id'] and r['status']=='pending']
    if len(active)>=5 or sum(r['size_bytes'] for r in active)+body['size_bytes']>MAX_UPLOAD_BYTES:
        raise Problem(422,'UPLOAD_LIMIT','At most five transfers totaling 16 MB may be pending in an editor.')
    if len(recent)>=50 or sum(r['size_bytes'] for r in recent)+body['size_bytes']>100_000_000:
        raise Problem(429,'UPLOAD_QUOTA','This workspace has reached its 24-hour upload allowance.')
    value = {'id':uid(),'draft_id':draft['id'],'filename':filename,'media_type':media,
        'size_bytes':body['size_bytes'],'sha256':body['sha256'],'status':'pending','created_at':now(),
        'expires_at':expiration(),'storage':None,'extraction':None,'error':None}
    if s3_enabled():
        storage=store(); value['staging_key']=storage.prefix+'uploads/staging/'+value['id']
    tx.put('upload',value['id'],owner,value)
    # Idempotency persists the identity, never a short-lived signed credential.
    return {'upload':public_upload(value)}


def transfer(value):
    if value['status'] not in ('pending','ready') or (value['status']=='pending' and value['expires_at']<=now()):
        raise Problem(409,'UPLOAD_EXPIRED','Start a new upload.')
    if s3_enabled():
        storage=store(); media=value['media_type']
        # POST policy enforces exact byte count. SHA is independently checked on completion.
        fields={'Content-Type':media,'x-amz-server-side-encryption':'AES256'}
        ticket=storage.signer.generate_presigned_post(storage.bucket,value['staging_key'],Fields=fields,
            Conditions=[{'Content-Type':media},{'x-amz-server-side-encryption':'AES256'},
                        ['content-length-range',value['size_bytes'],value['size_bytes']]],ExpiresIn=900)
        return {'method':'POST','url':ticket['url'],'fields':ticket['fields'],'encoding':'multipart'}
    return {'method':'POST','url':'/v1/uploads/'+value['id']+'/content','fields':{},'encoding':'base64'}


def stage_local(tx, owner, identity, encoded):
    value=owned(tx,'upload',identity,owner)['data']
    if s3_enabled(): raise Problem(404,'NOT_FOUND','Local upload transport is disabled.')
    if value['status']!='pending' or value['expires_at']<=now(): raise Problem(409,'UPLOAD_EXPIRED','Start a new upload.')
    if not isinstance(encoded,str) or len(encoded)>MAX_FILE_BYTES*4//3+8: raise Problem(413,'UPLOAD_TOO_LARGE','Upload exceeds 8 MB.')
    try: data=base64.b64decode(encoded,validate=True)
    except ValueError: raise Problem(422,'INVALID_UPLOAD','Upload encoding is invalid.') from None
    verify_bytes(value,data)
    value['staged']=retain(data,value['media_type']); tx.put('upload',identity,owner,value)
    return public_upload(value)


def verify_bytes(value,data):
    if len(data)!=value['size_bytes'] or checksum(data)!=value['sha256']:
        raise Problem(422,'UPLOAD_CHECKSUM_MISMATCH','The uploaded size or checksum differs. Upload the file again.')


def parse_document(data, filename):
    suffix=Path(filename).suffix.lower(); parts=[]; structured=None
    if suffix=='.pdf':
        if not data.startswith(b'%PDF-'): raise ValueError('The file is not a PDF')
        from pypdf import PdfReader
        pdf=PdfReader(io.BytesIO(data),strict=True)
        if pdf.is_encrypted or len(pdf.pages)>100: raise ValueError('Use an unencrypted PDF with at most 100 pages')
        for number,page in enumerate(pdf.pages,1):
            # Bound decompressed page streams before parsing text.
            content=page.get_contents()
            if content is not None and len(content.get_data())>4_000_000: raise ValueError('PDF page exceeds extraction limits')
            parts.append({'locator':'page:'+str(number),'text':page.extract_text() or ''})
            if sum(len(p['text'].encode()) for p in parts)>MAX_TEXT_BYTES: raise ValueError('Extracted text exceeds 500 KB')
    elif suffix=='.docx':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist())>1000 or sum(f.file_size for f in archive.infolist())>20_000_000:
                raise ValueError('DOCX expanded size exceeds limits')
            node=ET.fromstring(archive.read('word/document.xml'))
            ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            for index,paragraph in enumerate(node.iter(ns+'p'),1):
                text=''.join(t.text or '' for t in paragraph.iter(ns+'t'))
                if text: parts.append({'locator':'paragraph:'+str(index),'text':text})
    else:
        text=data.decode('utf-8-sig')
        if '\x00' in text or any(ord(c)<32 and c not in '\n\r\t' for c in text): raise ValueError('File is not UTF-8 text')
        if suffix=='.json': structured=json.loads(text)
        if suffix in ('.yaml','.yml'):
            import yaml
            structured=yaml.safe_load(text)
        if suffix in ('.csv','.tsv'):
            rows=list(csv.reader(io.StringIO(text),delimiter=',' if suffix=='.csv' else '\t'))
            if not rows or not rows[0] or len(set(rows[0]))!=len(rows[0]) or any(not key for key in rows[0]):
                raise ValueError('Tables require unique nonempty column names')
            if len(rows)>10001 or any(len(row)!=len(rows[0]) for row in rows[1:]):
                raise ValueError('Table rows must match their header and fit the row limit')
            def cell(value):
                # Preserve nonnumeric text exactly; normalize finite JSON numbers
                # only, with original bytes always retained for inspection.
                try:
                    parsed=json.loads(value)
                    if type(parsed) in (int,float) and __import__('math').isfinite(parsed): return parsed
                except (ValueError,TypeError): pass
                return value
            structured=[{key:cell(value) for key,value in zip(rows[0],row)} for row in rows[1:]]
        parts=[{'locator':'line:'+str(i),'text':line} for i,line in enumerate(text.splitlines(),1)]
    if len(parts)>10000: raise ValueError('Document exceeds 10,000 text segments')
    if not any(p['text'].strip() for p in parts): raise ValueError('No readable text was found; scanned documents require OCR before upload')
    if sum(len(p['text'].encode()) for p in parts)>MAX_TEXT_BYTES: raise ValueError('Extracted text exceeds 500 KB')
    result={'format':'reveal.upload-text/1','original_sha256':checksum(data),
        'extractor':{'name':'REVEAL document extraction','version':'1','parser':'pypdf 6.19.0' if suffix=='.pdf' else 'docx-xml' if suffix=='.docx' else 'utf-8'},'segments':parts}
    if structured is not None:
        # JSON serialization rejects nonfinite numeric values and cyclic YAML.
        encoded=json.dumps(structured,allow_nan=False,ensure_ascii=False)
        if len(encoded.encode())>MAX_EXTRACTED_BYTES: raise ValueError('Structured extraction exceeds 1 MB')
        result.update(data=structured,structured_extractor='reveal.structured-import/1')
    return result


def extract(data, filename):
    """Untrusted parsers run without credentials, bounded by time and memory."""
    try:
        result=subprocess.run([sys.executable,'-m','reveal_backend.user_inputs',filename],input=data,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=20,check=True,
            env={'PATH':os.defpath,'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONIOENCODING':'utf-8'})
        value=json.loads(result.stdout)
        if 'error' in value: raise ValueError(value['error'])
        return value
    except (subprocess.TimeoutExpired,subprocess.CalledProcessError,json.JSONDecodeError):
        raise ValueError('Document parsing failed or exceeded its resource limits') from None


def complete(value):
    if value['status']=='ready': return value
    if value['status']!='pending' or value['expires_at']<=now(): raise Problem(409,'UPLOAD_EXPIRED','Start a new upload.')
    if s3_enabled():
        storage=store()
        response=storage.client.get_object(Bucket=storage.bucket,Key=value['staging_key'])
        if response.get('VersionId') in (None,'','null'): raise Problem(503,'UPLOAD_UNAVAILABLE','The upload has no immutable S3 version.')
        with response['Body'] as stream: data=stream.read(MAX_FILE_BYTES+1)
    else:
        if not value.get('staged'): raise Problem(409,'UPLOAD_INCOMPLETE','Upload file bytes first.')
        data=read(value['staged'])
    verify_bytes(value,data)
    try: extracted=extract(data,value['filename'])
    except (ValueError,KeyError,UnicodeError,zipfile.BadZipFile,ET.ParseError) as error:
        raise Problem(422,'UPLOAD_EXTRACTION_FAILED',str(error)[:250]) from None
    original=retain(data,value['media_type'])
    raw=json.dumps(extracted,ensure_ascii=False,separators=(',',':')).encode()
    derived=retain(raw,'application/json')
    return {**value,'status':'ready','storage':original,'extraction':{'storage':derived,
        'format':extracted['format'],'segment_count':len(extracted['segments']),'original_sha256':value['sha256']},'error':None}


def resolve(tx, owner, composer):
    uploads=[]
    identities=composer.get('upload_ids',[])
    if len(identities)>5 or len(set(identities))!=len(identities):
        raise Problem(422,'UPLOAD_LIMIT','Select at most five distinct attachments.')
    for identity in identities:
        value=owned(tx,'upload',identity,owner)['data']
        if value['status']!='ready': raise Problem(409,'UPLOAD_NOT_READY','Wait for every selected attachment to finish processing.')
        uploads.append(public_upload(value))
    if sum(v['size_bytes'] for v in uploads)>MAX_UPLOAD_BYTES: raise Problem(422,'UPLOAD_LIMIT','Selected attachments exceed 16 MB.')
    if sum(v['extraction']['storage']['size_bytes'] for v in uploads)>MAX_EXTRACTED_BYTES:
        raise Problem(422,'UPLOAD_TEXT_LIMIT','Selected attachments exceed 1 MB of extracted text and locators. Select shorter documents.')
    return {'format':'reveal.user-inputs/1',**{key:composer.get(key,'') for key in INPUT_FIELDS},'uploads':uploads}


def referenced_ids(tx, owner=None):
    identities=set()
    for kind in ('draft','request','cfde_assessment'):
        for row in tx.list(kind,owner):
            value=row['data']
            if kind=='draft' and not saved(value) and value.get('expires_at','')<=now(): continue
            identities.update(value.get('composer',{}).get('upload_ids',[]))
            identities.update(u['id'] for u in value.get('user_inputs',{}).get('uploads',[]))
            if kind == 'cfde_assessment':
                identities.update(u['id'] for u in value.get('inputs',{}).get('uploads',[]))
    return identities


def referenced(tx, identity):
    return identity in referenced_ids(tx)


def cleanup_candidates(tx, owner=None, limit=100):
    """Ids of expired temporary editors and of expired uploads due a reference check, without payloads.

    A superset of what cleanup() removes: it re-reads and re-decides each id, so this may run in a lock-free
    snapshot. A referenced upload is checked again only after its private reference_recheck_at."""
    if tx.sqlite:
        field=lambda name: "json_extract(payload,'$."+name+"')"
        temporary="json_type(payload,'$.lifecycle') IS NOT NULL AND COALESCE("+field('lifecycle')+",'')<>'saved'"
    else:
        field=lambda name: "NULLIF(JSON_UNQUOTE(JSON_EXTRACT(payload,'$."+name+"')),'null')"
        temporary="COALESCE(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.lifecycle')),'saved')<>'saved'"   # JSON null is not saved
    scope=' AND owner_id=%s' if owner is not None else ''
    sql=('SELECT kind,id FROM (SELECT kind,id FROM reveal_records WHERE kind=%s'+scope+" AND COALESCE("+field('expires_at')+",'')<=%s AND "
         +temporary+' ORDER BY id LIMIT %s) d UNION ALL SELECT kind,id FROM (SELECT kind,id FROM reveal_records WHERE kind=%s'+scope+
         ' AND '+field('expires_at')+"<=%s AND COALESCE("+field('reference_recheck_at')+",'')<=%s ORDER BY id LIMIT %s) u")
    t=now(); owned=(owner,) if owner is not None else ()
    rows=tx.execute(sql,('draft',*owned,t,limit,'upload',*owned,t,t,limit)).fetchall()
    return [i for k,i in rows if k=='draft'],[i for k,i in rows if k=='upload']


def cleanup(tx, owner=None, limit=100, candidates=None):
    """Expire metadata only. Shared content-addressed object versions are retained.

    Runs under the write fence and decides every candidate again from its current row. An expired upload that is
    still referenced is kept and rechecked a day later (a private field; expires_at, which feeds quotas and
    public metadata, never moves)."""
    drafts,uploads=candidates if candidates is not None else cleanup_candidates(tx,owner,limit)
    expired=[]; owners=set()
    for identity,row in tx.get_many('draft',drafts).items():
        draft=row['data']
        if (owner is None or row['owner']==owner) and not saved(draft) and draft.get('expires_at','')<=now():
            tx.remove('draft',identity); tx.remove('draft_binding',identity); expired.append(identity); owners.add(row['owner'])
    if expired:
        for scope in [owner] if owner is not None else sorted(owners):
            for row in tx.list('exploration',scope):   # an exploration names only its owner's draft
                if row['data'].get('draft_id') in expired:
                    value={**row['data'],'draft_id':None}; tx.put('exploration',row['id'],row['owner'],value)
    due={identity:row for identity,row in tx.get_many('upload',uploads).items()
         if (owner is None or row['owner']==owner) and row['data']['expires_at']<=now() and (row['data'].get('reference_recheck_at') or '')<=now()}
    if due:
        selected=referenced_ids(tx,owner)
        for identity,row in due.items():
            if identity in selected: tx.put('upload',identity,row['owner'],{**row['data'],'reference_recheck_at':expiration(24)})
            else: tx.remove('upload',identity)
    return expired


if __name__=='__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(15,15))
    # Linux service containers support address-space limits. macOS rejects
    # lowering these limits; local parser tests still have CPU/wall/byte bounds.
    if sys.platform=='linux':
        _,hard=resource.getrlimit(resource.RLIMIT_AS)
        bound=1_073_741_824 if hard==resource.RLIM_INFINITY else min(hard,1_073_741_824)
        resource.setrlimit(resource.RLIMIT_AS,(bound,bound))
    try:
        raw=sys.stdin.buffer.read(MAX_FILE_BYTES+1)
        if len(raw)>MAX_FILE_BYTES: raise ValueError('Upload exceeds 8 MB')
        result=parse_document(raw,sys.argv[1])
    except Exception as error:
        result={'error':str(error)[:250] if isinstance(error,ValueError) else 'The document could not be parsed safely'}
    sys.stdout.write(json.dumps(result,ensure_ascii=False,separators=(',',':')))
