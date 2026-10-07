"""One warm, isolated DAPPER interpreter per process for trusted assembly, minting and paragraph linting.

It runs the same pinned release code as the per-call `python -I -B` programs it
replaces, in its own fresh interpreter (never this process, which imports another
DAPPER snapshot), and loads the schema, vocabulary and validator once. Callers
verify the release before every request; a different release, a crash, a
timeout or an error restarts it. REVEAL_DAPPER_HELPER=0 restores the per-call
programs.
"""
import atexit
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time

from .runtime_config import setting

STARTUP_SECONDS = 120
IDLE_SECONDS = 1800       # the helper exits after this long without a request
IDLE_MARGIN = 60          # and is replaced, not reused, once it is this close to exiting
MAX_REQUESTS = 256
# The application-owned terminal Paragraph profile, exactly as validate_paragraph_document's program adds it.
PARAGRAPH_PROFILE = {'title': 'Cited research statement', 'terminal': {'class': 'Paragraph', 'min': 1, 'max': 1},
    'required_edges': [{'group': 'was_generated_by_edges', 'object_class': 'Activity', 'min': 1, 'severity': 'error',
                        'why': 'Paragraph records its own generation activity.'}],
    'activities_require_inputs': 'warning'}
# Node and edge groups of accepted accounts and paragraphs: their validators compile on first use.
WARM_GROUPS = ('activities', 'claims', 'evidence_items', 'files', 'knowledge_gaps', 'mechanisms', 'organizations',
               'paragraphs', 'persons', 'propositions', 'scientific_accounts', 'used_edges', 'was_derived_from_edges',
               'was_generated_by_edges')

SERVER = r'''import copy,json,os,select,sys,traceback
from dataclasses import asdict
from pathlib import Path
out=os.fdopen(os.dup(1),'w',encoding='ascii',buffering=1); os.dup2(2,1)
root=Path(sys.argv[1]); schema=root/'schema'; idle=float(sys.argv[2])
sys.path[:0]=[str(schema/'lint'),str(schema/'identity'),str(schema)]
import yaml
from dapper_identity import assign_ids,load_schema
from lint_provenance import Vocabulary,build_validator,lint
from scientific_claims import assemble_cited_text
SV=load_schema(schema/'dapper.yaml'); PROFILES=yaml.safe_load((schema/'lint/profiles.yaml').read_text())
profiles=copy.deepcopy(PROFILES); profiles['profiles']['reveal-paragraph']=json.loads(sys.argv[3])
PARAGRAPH=Vocabulary.build(SV,profiles); VALIDATOR=build_validator(schema/'dapper.yaml'); STOCK=[]
def fields():
    if not STOCK: STOCK.append(Vocabulary.build(SV,copy.deepcopy(PROFILES)))
    vocab=STOCK[0]
    result={group:sorted(vocab.relationship_slots.get(cls,{})) for group,cls in vocab.node_groups.items()}
    result.update({group:['subject','object'] for group in vocab.edge_groups})
    return result
def handle(request):
    op=request['op']
    if op=='assemble': return assemble_cited_text(request['segments'])
    if op=='mint':
        path=Path(request['path']); document=json.loads(path.read_text())
        assign_ids(document,SV)
        path.write_text(json.dumps(document,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n')
        return None
    if op=='lint_paragraph':
        return [asdict(x) for x in lint(Path(request['path']),PARAGRAPH,SV,VALIDATOR,profile_name='reveal-paragraph').findings]
    if op=='reference_fields': return fields()
    if op=='warm':
        for group in request['groups']:
            cls=PARAGRAPH.node_groups.get(group) or PARAGRAPH.edge_groups.get(group)
            if cls in SV.all_classes(): VALIDATOR.validate({},cls)
        return None
    raise ValueError('Unknown DAPPER helper operation')
out.write('{"ready":true}\n')
stdin=sys.stdin.buffer
while select.select([stdin],[],[],idle)[0]:
    line=stdin.readline()
    if not line: break
    try: reply={'ok':True,'result':handle(json.loads(line))}
    except BaseException: reply={'ok':False,'error':traceback.format_exc()[-1500:]}
    out.write(json.dumps(reply)+'\n')
'''


def enabled():
    return setting('REVEAL_DAPPER_HELPER', '1') != '0'


def ready(descriptor, event, deadline):
    """poll, not select: a busy server's descriptors can exceed select's FD_SETSIZE."""
    remaining = deadline - time.monotonic()
    if remaining <= 0: return False
    poller = select.poll(); poller.register(descriptor, event)
    return bool(poller.poll(remaining * 1000))


class Failed(Exception):
    """The helper died, broke framing or never became ready; it has been discarded."""


class Helper:
    def __init__(self):
        self.lock = threading.Lock(); self.guard = threading.Lock()
        self.process = self.errors = self.key = None
        self.buffer = b''; self.served = 0; self.used = 0.0; self.warming = False

    def close(self):
        process, self.process, self.key, self.buffer = self.process, None, None, b''
        if process is not None:
            if process.poll() is None: process.kill()
            for stream in (process.stdin, process.stdout):
                try: stream.close()
                except OSError: pass
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: pass
        if self.errors is not None:
            self.errors.close(); self.errors = None

    def diagnostics(self):
        if self.errors is None: return 'DAPPER helper failed'
        try:
            self.errors.seek(0, os.SEEK_END); size = self.errors.tell()
            self.errors.seek(max(0, size - 1500)); return self.errors.read().decode('utf-8', 'replace')
        except (OSError, ValueError): return 'DAPPER helper failed'

    def read_line(self, deadline):
        descriptor = self.process.stdout.fileno()
        while b'\n' not in self.buffer:
            if not ready(descriptor, select.POLLIN, deadline): raise TimeoutError
            chunk = os.read(descriptor, 65536)
            if not chunk: raise Failed
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b'\n')
        return line

    def write(self, data, deadline):
        descriptor = self.process.stdin.fileno(); view = memoryview(data)
        while view:
            if not ready(descriptor, select.POLLOUT, deadline): raise TimeoutError
            try: view = view[os.write(descriptor, view):]
            except BlockingIOError: pass

    def start(self, root, key):
        self.close()
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen([sys.executable, '-I', '-B', '-c', SERVER, str(root), str(IDLE_SECONDS),
                                         json.dumps(PARAGRAPH_PROFILE)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.key, self.served, self.used = key, 0, time.monotonic()
        if json.loads(self.read_line(time.monotonic() + STARTUP_SECONDS)) != {'ready': True}: raise Failed

    def current(self, key):
        process = self.process
        return (process is not None and process.poll() is None and self.key == key and self.served < MAX_REQUESTS
                and time.monotonic() - self.used < IDLE_SECONDS - IDLE_MARGIN)

    def request(self, root, release, op, arguments, timeout, *, warm=False):
        """(True, result), or (False, diagnostics) once the helper failed. A deadline raises subprocess.TimeoutExpired."""
        root = Path(root).resolve(); key = (str(root), release['lock_sha256'], release.get('commit'))
        line = (json.dumps({'op': op, **arguments}) + '\n').encode()
        with self.lock:
            limit, fresh = STARTUP_SECONDS, False
            try:
                if not self.current(key): self.start(root, key); fresh = True
                limit = timeout; deadline = time.monotonic() + timeout
                try: self.write(line, deadline)
                except BrokenPipeError:
                    # It exited before reading anything, so the request never ran: replace it once.
                    if fresh: raise Failed
                    limit = STARTUP_SECONDS; self.start(root, key)
                    limit = timeout; deadline = time.monotonic() + timeout; self.write(line, deadline)
                reply = json.loads(self.read_line(deadline))
                if not isinstance(reply, dict): raise Failed
            except TimeoutError:
                self.close(); raise subprocess.TimeoutExpired(['dapper-helper', op], limit)
            except (Failed, OSError, ValueError):
                detail = self.diagnostics(); self.close(); return False, detail
            self.used = time.monotonic()
            if not warm: self.served += 1
            if not reply.get('ok'):
                # Never reuse an interpreter after a failed request.
                self.close(); return False, str(reply.get('error', ''))
            return True, reply.get('result')

    def prewarm(self, root, lock_path):
        """Start and warm the helper for this release in a background thread; never blocks or raises."""
        with self.guard:
            if self.warming: return
            self.warming = True
        def run():
            try:
                from .dapper_release import verify_release
                self.request(root, verify_release(root, lock_path), 'warm', {'groups': list(WARM_GROUPS)},
                             STARTUP_SECONDS, warm=True)
            except Exception:
                pass
            finally:
                self.warming = False
        threading.Thread(target=run, name='dapper-helper-warmup', daemon=True).start()


HELPER = Helper()


def request(root, release, op, arguments, timeout):
    return HELPER.request(root, release, op, arguments, timeout)


def prewarm(root, lock_path):
    """REVEAL_DAPPER_PREWARM=0 leaves the first request to start the helper."""
    if enabled() and setting('REVEAL_DAPPER_PREWARM', '1') != '0': HELPER.prewarm(root, lock_path)


def _forget():
    """A forked child neither shares nor kills its parent's helper."""
    global HELPER
    inherited = HELPER.process
    HELPER = Helper()
    if inherited is not None:
        for stream in (inherited.stdin, inherited.stdout):
            try: stream.close()
            except OSError: pass


os.register_at_fork(after_in_child=_forget)
atexit.register(lambda: HELPER.close())
