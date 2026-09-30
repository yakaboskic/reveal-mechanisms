"""Immutable S3 artifacts and portable, checksum-verified worker checkpoints.

Database references are committed only after uploads complete. Filesystem mode
is retained for the existing developer workflow and isolated unit tests.
"""
from __future__ import annotations

import base64
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from threading import Lock
from urllib.parse import quote
from uuid import uuid4

from .runtime_config import setting

VERIFIED_PUT_CACHE_SIZE = 4096
SNAPSHOT_CONCURRENCY = 4
SNAPSHOT_PENDING_BYTES = 16 * 1024 * 1024


class StorageUnavailable(RuntimeError):
    """A storage failure must not turn an incomplete checkpoint into success."""


def s3_enabled():
    mode = setting('REVEAL_ARTIFACT_STORE', 'filesystem')
    if mode not in ('filesystem', 's3'):
        raise ValueError('Unknown artifact store')
    return mode == 's3'


def checksum(data):
    return hashlib.sha256(data).hexdigest()


class S3Store:
    def __init__(self, bucket, prefix='reveal/', *, client=None, signer=None, read_prefixes=()):
        if not bucket or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
            raise ValueError('Configure a valid REVEAL_S3_BUCKET')
        if '..' in PurePosixPath(prefix).parts or prefix.startswith('/'):
            raise ValueError('Invalid S3 prefix')
        self.bucket, self.prefix = bucket, prefix.rstrip('/') + '/' if prefix else ''
        self.read_prefixes = {self.prefix}
        for legacy in read_prefixes:
            if not isinstance(legacy, str) or not legacy or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?', legacy):
                raise ValueError('Invalid artifact read prefix')
            self.read_prefixes.add(legacy.rstrip('/') + '/')
        if client is None:
            import boto3
            from botocore.config import Config
            config = Config(signature_version='s3v4', connect_timeout=5, read_timeout=30,
                            retries={'max_attempts': 3, 'mode': 'standard'},
                            s3={'addressing_style': setting('REVEAL_S3_ADDRESSING_STYLE', 'auto')})
            options = {'region_name': setting('AWS_REGION', 'us-east-1'), 'config': config}
            endpoint = setting('REVEAL_S3_ENDPOINT_URL') or None
            client = boto3.client('s3', endpoint_url=endpoint, **options)
            public = setting('REVEAL_S3_PUBLIC_ENDPOINT_URL') or endpoint
            signer = client if public == endpoint else boto3.client('s3', endpoint_url=public, **options)
        self.client, self.signer = client, signer or client
        self.maximum = int(setting('REVEAL_ARTIFACT_MAX_BYTES', str(128 * 1024 * 1024)))
        # Keep only confirmed immutable version references, never artifact bytes.
        # This store instance is scoped to its bucket, prefix and client settings.
        self._verified_puts = OrderedDict()
        self._cache_lock = Lock()
        # Duplicate content submitted concurrently shares one verification/upload.
        self._put_locks = tuple(Lock() for _ in range(32))

    def check(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
            if self.client.get_bucket_versioning(Bucket=self.bucket).get('Status') != 'Enabled':
                raise StorageUnavailable('Artifact bucket versioning must be enabled')
        except StorageUnavailable:
            raise
        except Exception as exc:
            raise StorageUnavailable('Artifact bucket is unavailable') from exc

    def validate(self, ref):
        sha = ref.get('sha256', '')
        expected = {prefix + 'artifacts/sha256/' + sha[:2] + '/' + sha for prefix in self.read_prefixes}
        if (ref.get('store') != 's3' or ref.get('bucket') != self.bucket
                or not re.fullmatch('[a-f0-9]{64}', sha) or ref.get('key') not in expected
                or not isinstance(ref.get('version_id'), str) or ref['version_id'] in ('', 'null')
                or type(ref.get('size_bytes')) is not int or not 0 <= ref['size_bytes'] <= self.maximum):
            raise StorageUnavailable('Invalid artifact storage reference')
        return {'Bucket': self.bucket, 'Key': ref['key'], 'VersionId': ref['version_id']}

    def put(self, data, content_type='application/octet-stream'):
        if len(data) > self.maximum:
            raise StorageUnavailable('Artifact exceeds the configured size limit')
        sha = checksum(data)
        key = self.prefix + 'artifacts/sha256/' + sha[:2] + '/' + sha
        ref = {'store': 's3', 'bucket': self.bucket, 'key': key, 'sha256': sha,
               'size_bytes': len(data), 'content_type': content_type}
        with self._put_locks[int(sha[:2], 16) % len(self._put_locks)]:
            cache_key = (self.bucket, key)
            with self._cache_lock:
                verified = self._verified_puts.get(cache_key)
                if verified is not None and verified[1] == len(data):
                    self._verified_puts.move_to_end(cache_key)
                    return dict(ref, version_id=verified[0])
            ref = self._put_verified(data, ref)
            with self._cache_lock:
                self._verified_puts[cache_key] = (ref['version_id'], ref['size_bytes'])
                self._verified_puts.move_to_end(cache_key)
                while len(self._verified_puts) > VERIFIED_PUT_CACHE_SIZE:
                    self._verified_puts.popitem(last=False)
            return ref

    def _put_verified(self, data, ref):
        key, sha = ref['key'], ref['sha256']
        try:
            # Avoid another version for identical immutable bytes on every
            # checkpoint. Integrity is established by a server-verified SHA-256.
            try:
                previous = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode='ENABLED')
            except self.client.exceptions.ClientError as exc:
                if str(exc.response.get('Error', {}).get('Code')) not in ('404', 'NoSuchKey', 'NotFound'):
                    raise
                previous = None
            encoded = base64.b64encode(bytes.fromhex(sha)).decode()
            if (previous and previous.get('ContentLength') == len(data)
                    and previous.get('ChecksumSHA256') == encoded and previous.get('VersionId') not in (None, 'null')):
                ref = dict(ref, version_id=previous['VersionId'])
                self.validate(ref)
                return ref
            options = {}
            encryption = setting('REVEAL_S3_ENCRYPTION', 'AES256')
            if encryption:
                options['ServerSideEncryption'] = encryption
            result = self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                ContentType=ref['content_type'], ChecksumSHA256=encoded, Metadata={'sha256': sha}, **options)
            ref['version_id'] = result.get('VersionId')
            self.validate(ref)
            return ref
        except StorageUnavailable:
            raise
        except Exception as exc:
            raise StorageUnavailable('Artifact upload failed') from exc

    def get(self, ref):
        parameters = self.validate(ref)
        try:
            result = self.client.get_object(**parameters)
            with result['Body'] as body:
                data = body.read(ref['size_bytes'] + 1)
            if len(data) != ref['size_bytes'] or checksum(data) != ref['sha256']:
                raise StorageUnavailable('Artifact checksum mismatch')
            return data
        except StorageUnavailable:
            raise
        except Exception as exc:
            raise StorageUnavailable('Artifact download failed') from exc

    def download_url(self, ref, filename, content_type):
        parameters = self.validate(ref)
        # Verify the exact immutable version exists before advertising it.
        try:
            self.client.head_object(**parameters)
            parameters.update(ResponseContentDisposition="attachment; filename*=UTF-8''" + quote(filename, safe=''),
                              ResponseContentType=content_type, ResponseCacheControl='private, no-store')
            return self.signer.generate_presigned_url('get_object', Params=parameters, ExpiresIn=60)
        except Exception as exc:
            raise StorageUnavailable('Artifact download is unavailable') from exc

    def snapshot(self, root):
        root = Path(root).resolve()
        files = []
        total = 0
        maximum = int(setting('REVEAL_WORKSPACE_MAX_BYTES', str(256 * 1024 * 1024)))
        pending = deque()
        pending_bytes = 0

        def finish_one():
            nonlocal pending_bytes
            name, future, size = pending.popleft()
            files.append({'path': name, 'storage': future.result()})
            pending_bytes -= size

        # Bound submitted data as well as threads. A file larger than the byte
        # window is sent alone; otherwise at most one small window is buffered.
        # Consume futures in path order to keep the manifest deterministic.
        with ThreadPoolExecutor(max_workers=SNAPSHOT_CONCURRENCY) as pool:
            for path in sorted(root.rglob('*')):
                if path.is_symlink():
                    raise StorageUnavailable('Checkpoint contains a symbolic link')
                if not path.is_file():
                    continue
                size = path.stat().st_size
                if len(files) + len(pending) >= 10000 or size > self.maximum or total + size > maximum:
                    raise StorageUnavailable('Checkpoint exceeds the workspace limit')
                while pending and (len(pending) >= SNAPSHOT_CONCURRENCY or pending_bytes + size > SNAPSHOT_PENDING_BYTES):
                    finish_one()
                with path.open('rb') as source:
                    data = source.read(size + 1)
                if len(data) != size:
                    raise StorageUnavailable('Checkpoint file changed during capture')
                total += size
                pending.append((path.relative_to(root).as_posix(), pool.submit(self.put, data), size))
                pending_bytes += size
                del data
            while pending:
                finish_one()
        manifest = {'format': 'reveal.workspace/1', 'files': files}
        return self.put(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode(), 'application/json')

    def restore(self, ref, root):
        root = Path(root).resolve()
        manifest = json.loads(self.get(ref))
        if manifest.get('format') != 'reveal.workspace/1' or not isinstance(manifest.get('files'), list):
            raise StorageUnavailable('Invalid workspace manifest')
        seen, total, targets = set(), 0, []
        for item in manifest['files']:
            name = item['path']
            relative = PurePosixPath(name)
            target = root / name
            if (not name or name in seen or len(seen) >= 10000 or relative.is_absolute()
                    or any(part in ('', '.', '..') for part in name.split('/')) or '\\' in name
                    or not target.resolve().is_relative_to(root) or target.is_symlink()):
                raise StorageUnavailable('Unsafe workspace manifest path')
            self.validate(item['storage'])
            seen.add(name)
            total += item['storage']['size_bytes']
            if total > int(setting('REVEAL_WORKSPACE_MAX_BYTES', str(256 * 1024 * 1024))):
                raise StorageUnavailable('Restored workspace exceeds its limit')
            targets.append((target, item['storage']))
        for target, storage in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name('.restore-' + uuid4().hex)
            try:
                temporary.write_bytes(self.get(storage))
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)


@lru_cache(maxsize=8)
def _store(bucket, prefix, endpoint, public, region, addressing, encryption, read_prefixes):
    return S3Store(bucket, prefix, read_prefixes=tuple(item.strip() for item in read_prefixes.split(',') if item.strip()))


def store():
    return _store(*(setting(key, default) for key, default in (
        ('REVEAL_S3_BUCKET', ''), ('REVEAL_S3_PREFIX', 'reveal/'), ('REVEAL_S3_ENDPOINT_URL', ''),
        ('REVEAL_S3_PUBLIC_ENDPOINT_URL', ''), ('AWS_REGION', 'us-east-1'),
        ('REVEAL_S3_ADDRESSING_STYLE', 'auto'), ('REVEAL_S3_ENCRYPTION', 'AES256'), ('REVEAL_S3_READ_PREFIXES', ''))))


def retained_file(path, expected_sha=None):
    """Create operational location metadata without changing scientific bytes."""
    path = Path(path)
    if not s3_enabled():
        return {'path': str(path.resolve())}
    data = path.read_bytes()
    if expected_sha and checksum(data) != expected_sha:
        raise StorageUnavailable('Artifact changed before upload')
    return {'storage': store().put(data)}
