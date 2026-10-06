"""Versioned, network-free reads over immutable artifacts. Standard library only.

content_json is JSON text, deliberately not a float-decoded object: all original
number tokens survive the MCP/HTTP serializer. No derived files are generated.
"""
import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path

READER_VERSION = 'reveal.evidence-reader/2'
MAX_SOURCE_BYTES = 32_000_000
MAX_RESPONSE_BYTES = 24_000
MAX_ITEMS = 100
MAX_TEXT_CHARACTERS = 16000
SECURE_DIRECTORY_READS = os.open in os.supports_dir_fd and hasattr(os,'O_NOFOLLOW')

class EvidenceReadError(ValueError):
    pass

class Number(str):
    pass

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def check(condition, message):
    if not condition:
        raise EvidenceReadError(message)

def parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, 'Duplicate source object key')
            result[key] = value
        return result
    def constant(_):
        raise EvidenceReadError('Nonfinite source number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_int=Number, parse_float=Number, parse_constant=constant)
    except (ValueError, RecursionError) as error:
        raise EvidenceReadError('Invalid JSON source: '+str(error)) from None

def exact_json(value):
    if isinstance(value, Number):
        return str(value)
    if isinstance(value, dict):
        return '{'+','.join(json.dumps(k, ensure_ascii=False)+':'+exact_json(v) for k,v in value.items())+'}'
    if isinstance(value, list):
        return '['+','.join(exact_json(v) for v in value)+']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)

def child_pointer(pointer, key):
    return pointer+'/'+str(key).replace('~','~0').replace('/','~1')

def select(value, pointer):
    check(isinstance(pointer,str) and len(pointer)<=4096 and (pointer=='' or pointer.startswith('/')), 'Invalid JSON Pointer')
    for part in pointer.split('/')[1:]:
        check(re.search(r'~(?![01])',part) is None, 'Invalid JSON Pointer escape')
        key=part.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            check(re.fullmatch(r'0|[1-9][0-9]*',key) is not None, 'Invalid array index')
            key=int(key)
            check(key<len(value), 'JSON Pointer does not exist')
        else:
            check(isinstance(value,dict) and key in value, 'JSON Pointer does not exist')
        value=value[key]
    return value

def _token(binding, offset):
    return base64.urlsafe_b64encode(json.dumps({'binding':binding,'offset':offset},sort_keys=True,separators=(',',':')).encode()).decode()

def read_artifact(raw, descriptor, *, artifact_id, sha256, pointer='', mode='json', offset=0, limit=20, continuation=None):
    """Read JSON entries, text lines or Unicode character ranges from frozen bytes."""
    check(isinstance(raw,bytes) and len(raw)<=MAX_SOURCE_BYTES, 'Artifact exceeds inspection resource limit')
    check(artifact_id==descriptor.get('artifact_id',descriptor.get('id')), 'Artifact identity differs')
    check(sha256==descriptor.get('sha256') and digest(raw)==sha256, 'Artifact checksum changed')
    check(len(raw)==descriptor.get('size_bytes'), 'Artifact size changed')
    check(mode in ('json','text','text_range'), 'Choose JSON selection, text lines or text_range')
    maximum = MAX_TEXT_CHARACTERS if mode == 'text_range' else MAX_ITEMS
    check(type(offset) is int and offset>=0, 'offset must be a zero-based nonnegative integer; use offset=0 for the first page')
    unit = {'json': 'JSON entries', 'text': 'text lines', 'text_range': 'Unicode characters'}[mode]
    check(type(limit) is int and 1<=limit<=maximum,
          f'For mode={mode}, limit must be an integer from 1 to {maximum} {unit}; retry with limit=20 (the default)')
    check(isinstance(pointer,str) and len(pointer)<=4096, 'Invalid JSON Pointer')
    check(mode!='text' or pointer=='', 'Text line reads do not accept JSON Pointers; use text_range for a JSON string')
    binding=digest(json.dumps([READER_VERSION,artifact_id,sha256,mode,pointer,limit],separators=(',',':')).encode())
    if continuation is not None:
        check(isinstance(continuation,str) and len(continuation)<=2048, 'Invalid continuation')
        try:
            token=json.loads(base64.b64decode(continuation,altchars=b'-_',validate=True))
        except (ValueError,TypeError):
            raise EvidenceReadError('Invalid continuation') from None
        check(isinstance(token,dict) and token.get('binding')==binding and type(token.get('offset')) is int and token['offset']>=0, 'Continuation belongs to another artifact or selection')
        check(offset in (0,token['offset']), 'Continuation offset differs')
        offset=token['offset']
    response={'format':READER_VERSION,'artifact_id':artifact_id,'sha256':sha256,'size_bytes':len(raw),
              'dapper_file_id':descriptor.get('dapper_file_id'),'mode':mode,'pointer':pointer,
              'coverage_note':'Inspection coverage only; scientific query coverage is unchanged.'}
    if mode=='text_range':
        if pointer:
            text = select(parse(raw),pointer)
            check(type(text) is str, 'Text ranges at a JSON Pointer require a string value')
        else:
            try: text=raw.decode('utf-8')
            except UnicodeDecodeError: raise EvidenceReadError('Artifact is not UTF-8 text') from None
        total=len(text); chosen=text[offset:offset+limit]; kind='text_characters'
        content=lambda xs: xs
    elif mode=='text':
        try: lines=raw.decode('utf-8').splitlines(keepends=True)
        except UnicodeDecodeError: raise EvidenceReadError('Artifact is not UTF-8 text') from None
        total=len(lines); chosen=lines[offset:offset+limit]; kind='text_lines'
        content=lambda xs: ''.join(xs)
    else:
        value=select(parse(raw),pointer)
        if isinstance(value,(dict,list)):
            kind='object' if isinstance(value,dict) else 'array'
            entries=list(value.items()) if kind=='object' else value
            total=len(entries); chosen=entries[offset:offset+limit]
            content=lambda xs: exact_json(dict(xs) if kind=='object' else xs)
        else:
            check(offset==0, 'Scalar selection cannot be paged; use text_range for a string or a narrower JSON pointer')
            kind='scalar'; total=1; chosen=[value]; content=lambda xs: exact_json(xs[0])
    check(offset<=total, 'Offset exceeds selection')
    # Reserve space for envelope, source locators and continuation. Large rows
    # require a narrower pointer; never recursively materialize them into files.
    budget=MAX_RESPONSE_BYTES-7000
    encoded_size=lambda xs: len(json.dumps(content(xs),ensure_ascii=False).encode())
    if chosen and encoded_size(chosen)>budget:
        check(kind!='scalar', 'Selection exceeds response limit; for a JSON string use mode=text_range with the same JSON Pointer and bounded character offset/limit')
        # Binary search avoids quadratic work on long Unicode lines/strings.
        lower,upper=0,len(chosen)
        while lower<upper:
            middle=(lower+upper+1)//2
            if encoded_size(chosen[:middle])<=budget: lower=middle
            else: upper=middle-1
        check(lower>0, 'Selection exceeds response limit; request a narrower JSON Pointer or use text_range for long lines')
        chosen=chosen[:lower]
    end=offset+len(chosen)
    response.update(kind=kind,offset=offset,returned=len(chosen),total=total,truncated=end<total,
                    complete_selection=offset==0 and end==total,
                    continuation=_token(binding,end) if end<total else None)
    response['text' if mode in ('text','text_range') else 'content_json']=content(chosen)
    if mode=='text_range':
        response['locator']={'pointer':pointer,'character_start':offset,'character_end':end,
            'character_indices':'zero-based Unicode code points, end exclusive',
            'scope':'JSON-decoded string value' if pointer else 'UTF-8 source text'}
        if not pointer:
            response['locator'].update(byte_start=len(text[:offset].encode()),byte_end=len(text[:end].encode()))
    elif mode=='text': response['locator']={'line_start':offset+1,'line_end':end,'line_numbers':'one-based inclusive'}
    elif kind=='object': response['locators']=[child_pointer(pointer,k) for k,v in chosen]
    elif kind=='array': response['locators']=[child_pointer(pointer,k) for k in range(offset,end)]
    else: response['locators']=[pointer]
    check(len(json.dumps(response,ensure_ascii=False).encode())<=MAX_RESPONSE_BYTES, 'Response exceeds inspection limit; narrow the selection')
    return response

TOOL_DEFINITION = {'name':'read_evidence','description':'Read a hash-verified frozen artifact by JSON Pointer, text lines, or Unicode character ranges. mode=json (default): limit 1-100 entries. mode=text: limit 1-100 lines. mode=text_range: limit 1-16000 characters, with an optional JSON-string pointer. Default limit=20; offset is zero-based in the selected unit. No scientific-source query or new observation; offline reads never access the network. content_json preserves exact numeric tokens. Use the index for artifact IDs and hashes.',
 'inputSchema':{'type':'object','properties':{'artifact_id':{'type':'string'},'sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},'pointer':{'type':'string','default':''},
     'mode':{'enum':['json','text','text_range'],'default':'json','description':'json selects JSON entries; text selects source lines; text_range selects Unicode characters from source text or a JSON string.'},
     'offset':{'type':'integer','minimum':0,'default':0,'description':'Zero-based entry, line or Unicode character offset, according to mode.'},
     'limit':{'type':'integer','minimum':1,'maximum':MAX_TEXT_CHARACTERS,'default':20,
              'description':'mode=json (default): 1-100 entries; mode=text: 1-100 lines; mode=text_range: 1-16000 Unicode characters. Default 20.'},
     'continuation':{'type':'string'}},'required':['artifact_id','sha256'],'additionalProperties':False,
     'allOf':[{'if':{'properties':{'mode':{'const':'text_range'}},'required':['mode']},
               'then':{'properties':{'limit':{'maximum':MAX_TEXT_CHARACTERS}}},
               'else':{'properties':{'limit':{'maximum':MAX_ITEMS}}}}]},
 'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}}


def safe_read(root, relative):
    check(isinstance(relative,str) and relative and '\\' not in relative and ':' not in relative
          and not relative.startswith('/') and not any(ord(c)<32 for c in relative), 'Invalid artifact path')
    parts=relative.split('/')
    check(all(p not in ('','.','..') and not p.startswith('.') for p in parts), 'Artifact path is outside evidence layout')
    # Anchor every component to an open directory descriptor. Checking a path
    # and then opening it leaves a race in which a parent can become a symlink.
    check(SECURE_DIRECTORY_READS, 'Secure local artifact reading is unavailable on this platform')
    handles=[]
    try:
        directory=os.open(Path(root).absolute(),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        handles.append(directory)
        for part in parts[:-1]:
            directory=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            handles.append(directory)
        file=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        handles.append(file)
        info=os.fstat(file)
        check(stat.S_ISREG(info.st_mode), 'Artifact must be a regular file')
        check(info.st_size<=MAX_SOURCE_BYTES, 'Artifact exceeds inspection resource limit')
        chunks=[]; size=0
        while True:
            chunk=os.read(file,min(1_000_000,MAX_SOURCE_BYTES+1-size))
            if not chunk: break
            size+=len(chunk)
            check(size<=MAX_SOURCE_BYTES, 'Artifact exceeds inspection resource limit')
            chunks.append(chunk)
        return b''.join(chunks)
    except FileNotFoundError:
        raise EvidenceReadError('Artifact unavailable offline; materialize it while connected first') from None
    except OSError:
        raise EvidenceReadError('Artifact is unreadable or has a forbidden symlink/path component') from None
    finally:
        for handle in reversed(handles): os.close(handle)


def control_json(raw):
    """Manifest/package metadata uses ordinary integers but rejects duplicate keys."""
    value=parse(raw)
    check(isinstance(value,dict), 'Evidence control document must be an object')
    # parse first for duplicate-key/nonfinite rejection; source values remain
    # untouched by read_artifact's exact number representation.
    return json.loads(raw)

class WorkspaceReader:
    """Only seed-manifest sources and verified exported closures are addressable."""
    def __init__(self, root, *, expected_seed_sha256=None):
        self.root=Path(root)
        self.expected_seed_sha256=expected_seed_sha256

    def inventory(self):
        records={}
        def source_path(path):
            check(isinstance(path,str) and path.startswith(('sources/','imports/','reuse/')),
                  'Artifact is outside the permitted evidence layout')
            check(all(part not in ('','.','..') and not part.startswith('.') for part in path.split('/')),
                  'Artifact is outside the permitted evidence layout')
            return path
        def package(path, expected, sources_root):
            raw=safe_read(self.root,path)
            check(digest(raw)==expected, 'Evidence package checksum changed')
            value=control_json(raw)
            records[('package:'+expected,expected)]={'artifact_id':'package:'+expected,'sha256':expected,'size_bytes':len(raw),'path':path,'format':'json'}
            for identity,entry in value.get('source_artifacts',{}).items():
                descriptor={**entry,'artifact_id':identity,'path':sources_root+source_path(entry['path'])}
                key=(identity,entry['sha256'])
                check(key not in records or records[key]['sha256']==entry['sha256'], 'Conflicting source inventory')
                records[key]=descriptor
            return value, len(raw)
        manifest=control_json(safe_read(self.root,'input/manifest.json'))
        seed_sha256=manifest['package_sha256']
        check(self.expected_seed_sha256 is None or self.expected_seed_sha256==seed_sha256, 'Evidence seed differs from the verified workspace scope')
        seed,_=package('input/evidence-package.json',seed_sha256,'input/')
        closure_root=self.root/'evidence/closures'
        if closure_root.exists():
            check(not (self.root/'evidence').is_symlink() and not closure_root.is_symlink(), 'Closure symlink is forbidden')
            for candidate in sorted(closure_root.glob('*/manifest.json')):
                relative=str(candidate.relative_to(self.root))
                closure=control_json(safe_read(self.root,relative)); parent=str(candidate.parent.relative_to(self.root))+'/'
                check(candidate.parent.name==closure['package_sha256'], 'Closure package identity changed')
                check(closure.get('seed_sha256')==seed_sha256, 'Closure belongs to another frozen seed')
                value,size=package(parent+'evidence-package.json',closure['package_sha256'],parent)
                check(value.get('research_request_id')==seed.get('research_request_id')
                      and value.get('selection')==seed.get('selection'), 'Closure changed the frozen research selection')
                permitted={entry['path']:entry for entry in value.get('source_artifacts',{}).values()}
                permitted['evidence-package.json']={'sha256':closure['package_sha256'],'size_bytes':size}
                for entry in closure.get('files', []):
                    identity=entry.get('artifact_id',entry.get('id'))
                    path=entry.get('path',entry.get('filename'))
                    check(path in permitted and all(entry.get(key)==permitted[path].get(key) for key in ('sha256','size_bytes')),
                          'Closure source differs from its frozen package')
                    check('dapper_file_id' not in entry or entry['dapper_file_id']==permitted[path].get('dapper_file_id'),
                          'Closure source identity differs from its frozen package')
                    if identity:
                        records[(identity,entry['sha256'])]={**entry,**permitted[path],'artifact_id':identity,'path':parent+path}
        public_root=self.root/'evidence/public'
        if public_root.exists():
            check(not (self.root/'evidence').is_symlink() and not public_root.is_symlink(), 'Public capture symlink is forbidden')
            for candidate in sorted(public_root.glob('*/manifest.json')):
                capture=control_json(safe_read(self.root,str(candidate.relative_to(self.root))))
                check(candidate.parent.name==capture.get('capture_id'), 'Public capture identity changed')
                parent=str(candidate.parent.relative_to(self.root))+'/'
                artifacts={entry['path']:entry for entry in capture.get('artifacts',[])}
                for identity,entry in capture.get('source_artifacts',{}).items():
                    source_path(entry['path'])
                    declared=artifacts.get(entry['path'],{})
                    check(all(declared.get(key)==entry.get(key) for key in ('sha256','size_bytes')), 'Public source differs from retained capture')
                    records[(identity,entry['sha256'])]={**entry,'artifact_id':identity,'path':parent+entry['path']}
                    alias=declared.get('artifact_id',declared.get('id'))
                    if alias:
                        records[(alias,entry['sha256'])]={**entry,'artifact_id':alias,'path':parent+entry['path']}
        return records

    def read(self, **arguments):
        descriptor=self.inventory().get((arguments.get('artifact_id'),arguments.get('sha256')))
        check(descriptor is not None, 'Artifact unavailable offline or absent from verified inventory')
        return read_artifact(safe_read(self.root,descriptor['path']),descriptor,**arguments)
