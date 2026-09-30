"""Standalone trusted root helper; upload capabilities arrive only on stdin.

Executed via the Box SDK session transport after terminal authoring. This module
uses only the standard library and never receives AWS or Box API credentials.
"""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import tarfile
from urllib.parse import parse_qs, unquote, urlsplit

MAX_FILE = 8_000_000
MAX_TOTAL = 40_000_000
MAX_FILES = 10000
MAX_PLAN = 16_000_000
MAX_BUNDLE = 25_000_000
MAX_EXPANDED_BUNDLE = 64_000_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def safe_name(name):
    return (isinstance(name, str) and bool(name) and len(name.encode()) <= 1024 and '\\' not in name and '\x00' not in name
        and not PurePosixPath(name).is_absolute() and all(p not in ('', '.', '..') for p in name.split('/')))


def source_file(root, relative):
    """Open without following links, including any intermediate directory."""
    if not safe_name(relative): raise ValueError('Unsafe capture path')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        metadata = os.fstat(target)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE:
            os.close(target); raise ValueError('Capture requires bounded regular files')
        return os.fdopen(target, 'rb'), metadata
    finally:
        os.close(descriptor)


def inventory(base, binding):
    base = Path(base)
    request = json.loads((base / 'request.json').read_bytes())
    for key in ('job_id', 'attempt', 'kind', 'selected_graphs', 'input_sha256'):
        if request.get(key) != binding.get(key): raise ValueError('Capture belongs to another execution')
    status = json.loads((base / 'state/status.json').read_bytes())
    if status.get('status') not in ('succeeded', 'failed', 'cancelled', 'insufficient_evidence'):
        raise ValueError('Cannot capture active authoring')
    capture = base / 'state/direct-capture-v1'
    if capture.exists():
        if capture.is_symlink() or capture.stat().st_uid != os.geteuid() or capture.stat().st_mode & 0o077:
            raise ValueError('Unsafe private capture directory')
        saved = json.loads((capture / 'inventory.json').read_bytes())
        if saved.get('binding') != binding: raise ValueError('Frozen capture binding changed')
        return saved
    temporary = Path(tempfile.mkdtemp(prefix='.capture-', dir=base / 'state'))
    try:
        entries = []; total = 0
        roots = [(base / 'output', 'output'), (base / 'state/ledger', 'ledger')]
        candidates = []; visited = 0
        for root, prefix in roots:
            if root.is_symlink() or not root.is_dir(): raise ValueError('Missing capture root')
            for path in root.rglob('*'):
                visited += 1
                if visited > MAX_FILES * 4: raise ValueError('Capture tree exceeds entry limit')
                if path.is_symlink(): raise ValueError('Capture contains a link')
                if path.is_dir(): continue
                name = prefix + '/' + path.relative_to(root).as_posix()
                candidates.append((root, path.relative_to(root).as_posix(), name))
                if len(candidates) > MAX_FILES: raise ValueError('Too many capture files')
        runtime = base / 'state/runtime.json'
        if runtime.exists() or runtime.is_symlink(): candidates.append((base / 'state', 'runtime.json', 'runtime.json'))
        for root, relative, name in sorted(candidates, key=lambda value: value[2]):
            if not safe_name(name) or len(entries) >= MAX_FILES: raise ValueError('Invalid capture inventory')
            source, before = source_file(root, relative)
            total += before.st_size
            if total > MAX_TOTAL: source.close(); raise ValueError('Capture exceeds byte budget')
            target = temporary / 'files' / name; target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(); count = 0
            with source, target.open('xb') as output:
                while chunk := source.read(65536):
                    count += len(chunk)
                    if count > before.st_size: raise ValueError('Capture changed while freezing')
                    digest.update(chunk); output.write(chunk)
                after = os.fstat(source.fileno())
                if count != before.st_size or (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('Capture changed while freezing')
            target.chmod(0o400)
            entries.append({'path': name, 'size_bytes': count, 'sha256': digest.hexdigest()})
        result = {'format': 'reveal.direct-capture/1', 'binding': binding, 'state': status, 'files': entries}
        (temporary / 'inventory.json').write_bytes(canonical(result))
        (temporary / 'inventory.json').chmod(0o400)
        temporary.rename(capture)
        return result
    finally:
        if temporary.exists(): shutil.rmtree(temporary)


def upload_one(root, item, host):
    parsed = urlsplit(item['url'])
    if parsed.scheme != 'https' or parsed.netloc != host or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Upload destination escaped configured bucket')
    headers = item['headers']
    if set(headers) != {'Content-Length', 'Content-Type', 'x-amz-checksum-sha256', 'x-amz-server-side-encryption', 'If-None-Match'}:
        raise ValueError('Unexpected upload headers')
    if headers['Content-Length'] != str(item['size_bytes']) or headers['If-None-Match'] != '*':
        raise ValueError('Upload constraints changed')
    source, metadata = source_file(root, item['path'])
    with source:
        digest = hashlib.sha256(); count = 0
        while chunk := source.read(65536): digest.update(chunk); count += len(chunk)
        if count != item['size_bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Frozen capture changed')
        source.seek(0)
        connection = http.client.HTTPSConnection(host, timeout=30)
        try:
            connection.request('PUT', parsed.path + '?' + parsed.query, body=source, headers=headers)
            response = connection.getresponse()
            response.read(4096)
            if response.status not in (200, 201, 412): raise ValueError('Direct upload failed')
            return {'path': item['path'], 'version_id': response.getheader('x-amz-version-id'), 'status': response.status}
        finally:
            connection.close()


def upload(base, plan):
    saved = inventory(base, plan['binding'])
    expected = {item['path']: item for item in saved['files']}
    tickets = plan['tickets']
    names = {item['path'] for item in tickets}
    if not tickets or len(tickets) > 256 or len(names) != len(tickets) or not names.issubset(expected):
        raise ValueError('Upload batch must contain unique frozen inventory files')
    for item in tickets:
        if any(item.get(k) != expected[item['path']][k] for k in ('size_bytes', 'sha256')):
            raise ValueError('Upload ticket does not match frozen bytes')
    with ThreadPoolExecutor(max_workers=4) as pool:
        return {'receipts': list(pool.map(lambda item: upload_one(Path(base) / 'state/direct-capture-v1/files', item, plan['host']), tickets))}


class BoundedReader:
    """Also bound tar extended headers and padding before tarfile parses them."""
    def __init__(self, source):
        self.source, self.count = source, 0

    def read(self, size):
        if size < 0: raise ValueError('Unbounded bootstrap read')
        data = self.source.read(min(size, MAX_EXPANDED_BUNDLE - self.count + 1))
        self.count += len(data)
        if self.count > MAX_EXPANDED_BUNDLE: raise ValueError('Expanded bootstrap exceeds limit')
        return data


def extract_bundle(archive_path, destination, config):
    """Extract only bounded regular files into an unused root-private directory."""
    names = set(); total = 0
    with gzip.open(archive_path, 'rb') as compressed:
        with tarfile.open(fileobj=BoundedReader(compressed), mode='r|') as archive:
            for member in archive:
                name = member.name
                if (not safe_name(name) or not name.startswith(('bundle/','input/'))
                        or name in names or len(names) >= MAX_FILES or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                        or member.sparse is not None
                        or member.size < 0 or member.size > MAX_FILE):
                    raise ValueError('Unsafe bootstrap archive member')
                names.add(name); total += member.size
                if total > 50_000_000: raise ValueError('Bootstrap files exceed expanded byte budget')
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('xb') as output:
                    count = 0
                    while chunk := source.read(65536):
                        count += len(chunk)
                        if count > member.size: raise ValueError('Bootstrap file exceeds declared size')
                        output.write(chunk)
                    if count != member.size: raise ValueError('Bootstrap file is incomplete')
                target.chmod(0o644)
    expected = 'input/evidence-package.json' if config['kind'] == 'research' else 'input/paragraph-input.json'
    if (expected not in names or not any(name.startswith('bundle/') for name in names)
            or hashlib.sha256((destination / expected).read_bytes()).hexdigest() != config['input_sha256']):
        raise ValueError('Bootstrap input differs from frozen configuration')


def bootstrap(base, plan):
    """Download a verified immutable input/harness bundle without API relaying it."""
    base = Path(base); descriptor = plan['descriptor']; reference = descriptor['bundle']
    config = descriptor['config']; fingerprint = descriptor['fingerprint']
    if descriptor.get('format') != 'reveal.box-bootstrap/1': raise ValueError('Unknown bootstrap format')
    ready = base / 'state/bootstrap-ready'
    if ready.exists():
        if ready.is_symlink() or ready.read_text().strip() != fingerprint: raise ValueError('Bootstrap identity changed')
        return {'fingerprint': fingerprint}
    # A stale created handle must never overwrite an already launched runtime.
    if (base / 'state/status.json').exists(): raise ValueError('Cannot bootstrap started authoring')
    request = base / 'request.json'
    if request.exists() and (request.is_symlink() or json.loads(request.read_bytes()) != config):
        raise ValueError('Existing bootstrap belongs to another execution')
    downloaded = base / 'state/bootstrap-downloaded'
    if downloaded.exists():
        if downloaded.is_symlink() or downloaded.read_text().strip() != fingerprint:
            raise ValueError('Downloaded bootstrap identity changed')
        return {'fingerprint': fingerprint}
    parsed = urlsplit(plan['url']); host = plan['host']
    if (parsed.scheme != 'https' or parsed.netloc != host or parsed.username or parsed.fragment
            or host not in (reference['bucket'] + '.s3.amazonaws.com', reference['bucket'] + '.s3.us-east-1.amazonaws.com')
            or unquote(parsed.path) != '/' + reference['key']
            or parse_qs(parsed.query).get('versionId') != [reference['version_id']]
            or not 0 < reference['size_bytes'] <= MAX_BUNDLE):
        raise ValueError('Bootstrap capability does not name the frozen object')
    temporary = Path(tempfile.mkdtemp(prefix='.bootstrap-', dir=base))
    try:
        archive_path = temporary / 'frozen.tgz'; digest = hashlib.sha256(); count = 0
        connection = http.client.HTTPSConnection(host, timeout=30)
        try:
            connection.request('GET', parsed.path + '?' + parsed.query)
            response = connection.getresponse()
            if (response.status != 200 or int(response.getheader('Content-Length')) != reference['size_bytes']
                    or response.getheader('x-amz-version-id') != reference['version_id']):
                raise ValueError('Bootstrap download version or length changed')
            with archive_path.open('xb') as output:
                while chunk := response.read(65536):
                    count += len(chunk)
                    if count > reference['size_bytes']: raise ValueError('Bootstrap download exceeds limit')
                    digest.update(chunk); output.write(chunk)
        finally:
            connection.close()
        if count != reference['size_bytes'] or digest.hexdigest() != reference['sha256']:
            raise ValueError('Bootstrap download checksum changed')
        digest.update(json.dumps(config, sort_keys=True).encode())
        if digest.hexdigest() != fingerprint: raise ValueError('Bootstrap configuration fingerprint changed')
        extracted = temporary / 'extracted'; extracted.mkdir()
        extract_bundle(archive_path, extracted, config)
        # These are the only installed trees; authoring has not started. A
        # previous interrupted installation may have committed one tree only.
        for name in ('bundle','input'):
            target = base / name
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_dir() or target.stat().st_uid != os.geteuid():
                    raise ValueError('Unsafe existing bootstrap directory')
                shutil.rmtree(target)
            (extracted / name).replace(target)
        (temporary / 'request.json').write_bytes(json.dumps(config, sort_keys=True).encode())
        (temporary / 'request.json').replace(request)
        marker = temporary / 'downloaded'; marker.write_text(fingerprint); marker.chmod(0o600); marker.replace(downloaded)
        return {'fingerprint': fingerprint}
    finally:
        shutil.rmtree(temporary)


def main():
    if os.geteuid() != 0: raise ValueError('Direct capture requires the trusted root runner')
    raw = sys.stdin.buffer.read(MAX_PLAN + 1)
    if len(raw) > MAX_PLAN: raise ValueError('Transfer plan exceeds limit')
    plan = json.loads(raw)
    base = Path('/reveal')
    if sys.argv[1] == 'bootstrap':
        for path in (base, base / 'state'):
            if not path.exists(): path.mkdir(mode=0o755)
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
                raise ValueError('Unsafe trusted bootstrap directory')
    descriptor = os.open(base / 'state/direct-capture.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        action = sys.argv[1]
        result = (inventory(base, plan['binding']) if action == 'inventory' else upload(base, plan) if action == 'upload'
            else bootstrap(base, plan) if action == 'bootstrap' else None)
        if result is None: raise ValueError('Unknown capture action')
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try: main()
    except Exception:
        # Neither provider bodies nor signed upload capabilities reach logs.
        print('{"error":"trusted capture operation failed"}')
        raise SystemExit(1)
