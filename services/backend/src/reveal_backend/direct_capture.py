"""Verify direct Box uploads and compose immutable workspace references.

File bytes travel from Box to S3. The API streams each version once for integrity
and credential checks, but does not materialize a capture workspace on disk.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time
from urllib.parse import urlsplit

from .artifact_store import StorageUnavailable
from .box_adapter import CAPTURE_MARKER, BoxTransportError, TERMINAL_STATUSES
from .runtime_config import setting

MAX_FILE = 8_000_000
MAX_TOTAL = 40_000_000
MAX_FILES = 10000
MAX_CONTROL = 16_000_000
CONCURRENCY = 4
UPLOAD_BATCH = 256


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def require(value, message):
    if not value: raise BoxTransportError(message)


def valid_path(name):
    return (isinstance(name, str) and bool(name) and len(name.encode()) <= 1024 and '\\' not in name and '\x00' not in name
        and not PurePosixPath(name).is_absolute() and all(p not in ('', '.', '..') for p in name.split('/')))


def validate_inventory(value, binding, state):
    require(isinstance(value, dict) and value.get('format') == 'reveal.direct-capture/1' and value.get('binding') == binding, 'Direct capture binding differs')
    require(isinstance(value.get('state'), dict) and value['state'].get('status') in TERMINAL_STATUSES and value['state'] == state, 'Direct capture terminal state changed')
    files = value.get('files'); require(isinstance(files, list) and 0 < len(files) <= MAX_FILES, 'Invalid direct capture inventory')
    names = set(); total = 0
    for item in files:
        require(isinstance(item, dict), 'Invalid direct capture inventory entry')
        name = item.get('path')
        require(valid_path(name) and (name.startswith(('output/', 'ledger/')) or name == 'runtime.json')
            and name not in names, 'Unsafe direct capture path')
        size = item.get('size_bytes'); sha = item.get('sha256')
        require(type(size) is int and 0 <= size <= MAX_FILE and isinstance(sha, str) and re.fullmatch('[a-f0-9]{64}', sha), 'Invalid direct capture size or checksum')
        total += size; names.add(name)
        require(total <= MAX_TOTAL, 'Direct capture exceeds byte budget')
    require('ledger/manifest.json' in names, 'Direct capture ledger is missing')
    require(not any(str(parent) in names for name in names for parent in PurePosixPath(name).parents if str(parent) != '.'),
        'Direct capture file conflicts with a parent path')
    return files


def storage_hosts(storage):
    region = getattr(getattr(storage.signer, 'meta', None), 'region_name', None) or setting('AWS_REGION', 'us-east-1')
    require(region == 'us-east-1', 'Direct capture requires the configured US East S3 region')
    # Boto3's default us-east-1 signer uses the global bucket endpoint. Both
    # names address this exact configured bucket; choose only the signed host.
    return {storage.bucket + '.s3.amazonaws.com', storage.bucket + '.s3.us-east-1.amazonaws.com'}


def tickets_for(storage, files):
    allowed_hosts = storage_hosts(storage)
    host = None
    encryption = setting('REVEAL_S3_ENCRYPTION', 'AES256')
    require(encryption == 'AES256', 'Direct capture requires the configured AES256 encryption')
    tickets = []
    for item in files:
        checksum = base64.b64encode(bytes.fromhex(item['sha256'])).decode()
        key = storage.prefix + 'artifacts/sha256/' + item['sha256'][:2] + '/' + item['sha256']
        parameters = {'Bucket': storage.bucket, 'Key': key, 'ContentLength': item['size_bytes'],
            'ContentType': 'application/octet-stream', 'ChecksumSHA256': checksum,
            'ServerSideEncryption': encryption, 'IfNoneMatch': '*'}
        url = storage.signer.generate_presigned_url('put_object', Params=parameters, ExpiresIn=300, HttpMethod='PUT')
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and parsed.netloc in allowed_hosts and not parsed.username and not parsed.fragment,
                'Presigned upload escaped configured S3 host')
        require(host is None or parsed.netloc == host, 'Upload tickets use inconsistent S3 hosts')
        host = parsed.netloc
        tickets.append({**item, 'url': url, 'headers': {'Content-Length': str(item['size_bytes']),
            'Content-Type': parameters['ContentType'], 'x-amz-checksum-sha256': checksum,
            'x-amz-server-side-encryption': encryption, 'If-None-Match': '*'}})
    return host, tickets


def verify_uploaded(storage, item, receipt, secrets):
    sha = item['sha256']; key = storage.prefix + 'artifacts/sha256/' + sha[:2] + '/' + sha
    require(receipt.get('status') in (200, 201, 412), 'Direct upload receipt is invalid')
    version = receipt.get('version_id')
    require((receipt['status'] == 412 and version is None) or (isinstance(version, str) and version not in ('', 'null')),
        'Direct upload has no immutable version')
    parameters = {'Bucket': storage.bucket, 'Key': key, 'ChecksumMode': 'ENABLED'}
    if version: parameters['VersionId'] = version
    try:
        head = storage.client.head_object(**parameters)
        require(not version or head.get('VersionId') == version, 'Direct upload immutable version changed')
        version = head.get('VersionId')
        require(head.get('ContentLength') == item['size_bytes']
            and head.get('ChecksumSHA256') == base64.b64encode(bytes.fromhex(sha)).decode()
            and isinstance(version, str) and version not in ('', 'null'), 'Direct upload checksum, size or version mismatch')
        ref = {'store': 's3', 'bucket': storage.bucket, 'key': key, 'version_id': version,
            'sha256': sha, 'size_bytes': item['size_bytes'], 'content_type': 'application/octet-stream'}
        response = storage.client.get_object(**storage.validate(ref))
        digest = hashlib.sha256(); count = 0; overlap = b''; saved = []
        maximum_secret = max([len(secret) for secret in secrets] + [1])
        with response['Body'] as body:
            while chunk := body.read(65536):
                count += len(chunk)
                require(count <= item['size_bytes'], 'Direct object exceeds its inventory')
                digest.update(chunk)
                joined = overlap + chunk
                require(not any(secret in joined for secret in secrets), 'Output contains credential material')
                overlap = joined[-(maximum_secret - 1):] if maximum_secret > 1 else b''
                if item['path'] == 'ledger/manifest.json': saved.append(chunk)
        require(count == item['size_bytes'] and digest.hexdigest() == sha, 'Direct object bytes differ from inventory')
        return item['path'], ref, b''.join(saved)
    except (BoxTransportError, StorageUnavailable): raise
    except Exception as exc: raise StorageUnavailable('Direct capture object verification failed') from exc


def verify_uploads(storage, files, receipts, binding, secrets):
    require(isinstance(receipts, list) and len(receipts) == len(files), 'Direct upload receipts are incomplete')
    require(all(isinstance(item, dict) and isinstance(item.get('path'), str) for item in receipts), 'Invalid direct upload receipt')
    by_path = {item['path']: item for item in receipts}
    require(len(by_path) == len(receipts) and set(by_path) == {item['path'] for item in files}, 'Direct upload receipt paths changed')
    refs = {}; ledger_bytes = None
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for name, ref, raw in pool.map(lambda item: verify_uploaded(storage, item, by_path[item['path']], secrets), files):
            refs[name] = ref
            if name == 'ledger/manifest.json': ledger_bytes = raw
    try:
        ledger = json.loads(ledger_bytes)
        require(isinstance(ledger, dict) and ledger.get('format') == 'reveal.tool-ledger/1' and ledger.get('complete') is True
            and ledger.get('job_id') == binding['job_id'] and ledger.get('attempt') == binding['attempt'], 'Uploaded ledger binding is incomplete')
        require(isinstance(ledger.get('calls'), list), 'Uploaded ledger calls are invalid')
        for call in ledger['calls']:
            require(isinstance(call, dict), 'Uploaded ledger call is invalid')
            for field in ('request', 'response', 'upstream_request', 'upstream_response'):
                descriptor = call.get(field)
                if descriptor is None:
                    require(field not in ('request', 'response'), 'Uploaded ledger is missing a tool capture')
                    continue
                require(isinstance(descriptor, dict), 'Uploaded ledger descriptor is invalid')
                name = descriptor.get('path')
                require(valid_path(name), 'Uploaded ledger path is unsafe')
                ref = refs.get('ledger/' + name)
                require(ref is not None and {key: ref[key] for key in ('sha256', 'size_bytes')} ==
                    {key: descriptor[key] for key in ('sha256', 'size_bytes')}, 'Uploaded ledger bytes do not match descriptors')
    except (ValueError, KeyError, TypeError) as exc:
        raise BoxTransportError('Uploaded ledger is incomplete; retain the Box') from exc
    return refs


def compose_workspace(storage, previous, binding, handle, files, refs):
    manifest = storage.workspace_manifest(previous)
    require(set(refs) == {item['path'] for item in files}, 'Composed capture references are incomplete')
    for item in files:
        reference = refs[item['path']]
        storage.validate(reference)
        require(reference['sha256'] == item['sha256'] and reference['size_bytes'] == item['size_bytes'],
            'Composed capture reference differs from inventory')
    prefix = 'attempt-' + str(binding['attempt']) + '/output/'
    merged = {}
    for item in manifest['files']:
        name = item['path']
        # Replace only this attempt's output subtree, retaining immutable input refs.
        if not name.startswith(prefix): merged[name] = item['storage']
    captured = dict(handle, phase='captured', timings={**handle.get('timings', {}), 'captured_at': time.time()})
    marker = {'format': 'reveal.box-capture/1', 'binding': binding, 'state': handle['state'],
        'files': {item['path']: {key: item[key] for key in ('sha256', 'size_bytes')} for item in files},
        'cleanup_complete': False, 'timings': captured['timings']}
    marker_raw = json.dumps(marker, sort_keys=True).encode()
    for name, reference in refs.items(): merged[prefix + name] = reference
    sizes = {name: reference['size_bytes'] for name, reference in merged.items()}
    sizes[prefix + CAPTURE_MARKER] = len(marker_raw)
    require(len(sizes) <= MAX_FILES, 'Composed checkpoint has too many files')
    require(not any(str(parent) in sizes for name in sizes for parent in PurePosixPath(name).parents if str(parent) != '.'),
        'Composed checkpoint file conflicts with a parent path')
    maximum = int(setting('REVEAL_WORKSPACE_MAX_BYTES', str(256 * 1024 * 1024)))
    require(sum(sizes.values()) <= maximum, 'Composed checkpoint exceeds its byte limit')
    merged[prefix + CAPTURE_MARKER] = storage.put(marker_raw, 'application/json')
    result = {'format': 'reveal.workspace/1', 'files': [{'path': name, 'storage': merged[name]} for name in sorted(merged)]}
    reference = storage.put(canonical(result), 'application/json')
    return {'box': captured, 'workspace': reference, 'capture_sha256': hashlib.sha256(marker_raw).hexdigest()}


async def remote_session(box, action, plan, *, python='/reveal/venv/bin/python'):
    """Root stdin is a private transport; no presigned URL enters argv or disk."""
    source = Path(__file__).with_name('box_upload.py').read_text()
    output = bytearray()
    def stdout(chunk):
        if len(output) + len(chunk) > MAX_CONTROL: raise BoxTransportError('Capture response exceeds control limit')
        output.extend(chunk)
    session = None
    try:
        async with asyncio.timeout(180):
            require(python in ('/reveal/venv/bin/python', '/usr/bin/python3'), 'Untrusted transfer interpreter')
            session = await box.exec.session(argv=['sudo', '-n', python, '-I', '-B', '-c', source, action],
                tty=False, on_stdout=stdout, on_stderr=lambda _chunk: None)
            raw = canonical(plan)
            require(len(raw) <= MAX_CONTROL, 'Capture transfer plan is too large')
            for offset in range(0, len(raw), 65536):
                await session.write(raw[offset:offset + 65536])
            await session.end_stdin()
            require(await session.wait() == 0, 'Trusted direct capture did not complete')
        return json.loads(output)
    except asyncio.CancelledError: raise
    except BoxTransportError: raise
    except Exception as exc: raise BoxTransportError('Direct capture transport interrupted; retain remote handle') from exc
    finally:
        if session is not None:
            from .workflow_execution import drain_on_cancel
            await drain_on_cancel(session.close())


async def capture_to_store(adapter, binding, handle, storage, workspace_ref):
    require(handle.get('capture_protocol') == 's3-v1' and handle.get('state', {}).get('status') in TERMINAL_STATUSES,
        'Direct capture requires an eligible terminal Box')
    require(binding.get('box_id') == handle['box_id'] and binding.get('job_id') == handle['job_id']
        and type(binding.get('attempt')) is int and binding['attempt'] == handle['attempt'] and binding['attempt'] > 0,
        'Direct capture handle binding differs')
    require(binding.get('kind') in ('research', 'paragraph') and isinstance(binding.get('selected_graphs'), list)
        and all(isinstance(graph, str) for graph in binding['selected_graphs'])
        and isinstance(binding.get('input_sha256'), str) and re.fullmatch('[a-f0-9]{64}', binding['input_sha256']),
        'Direct capture input binding is invalid')
    require(all(adapter.environ.get(key) for key in ('ANTHROPIC_API_KEY', 'UPSTASH_BOX_API_KEY')),
        'Direct capture credential scanner is not configured')
    box = await adapter.connect(handle)
    try:
        inventory = await remote_session(box, 'inventory', {'binding': binding})
        files = validate_inventory(inventory, binding, handle['state'])
        from .workflow_execution import run_sync, drain_on_cancel
        receipts = []; host = None
        for offset in range(0, len(files), UPLOAD_BATCH):
            batch_host, tickets = await run_sync(tickets_for, storage, files[offset:offset + UPLOAD_BATCH])
            require(host is None or host == batch_host, 'Upload batches use inconsistent S3 hosts')
            if host is None:
                host = batch_host
                await box.update_network_policy({'mode': 'custom', 'allowed_domains': [host]})
            uploaded = await remote_session(box, 'upload', {'binding': binding, 'host': host, 'tickets': tickets})
            require(isinstance(uploaded, dict) and isinstance(uploaded.get('receipts'), list), 'Invalid direct upload response')
            receipts.extend(uploaded['receipts'])
        # Run under the caller's cancellation-draining wrapper so verification
        # threads settle before this invocation releases its durable lease.
        secrets = tuple(adapter.environ[key].encode() for key in ('ANTHROPIC_API_KEY', 'UPSTASH_BOX_API_KEY'))
        refs = await run_sync(verify_uploads, storage, files, receipts, binding, secrets)
        return await run_sync(compose_workspace, storage, workspace_ref, binding, handle, files, refs)
    finally:
        from .workflow_execution import drain_on_cancel
        await drain_on_cancel(box.aclose())
