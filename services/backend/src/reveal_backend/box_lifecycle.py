"""One remote effect per request; no lifetime polling loop or local worker lease."""
import base64
import hashlib
import json
import time
from pathlib import PurePosixPath
from .box_upload import MAX_TOTAL, capture_file_limit
from .box_adapter import (BoxExecutionAdapter, BoxTransportError, BoxConfigurationError, required_environment,
    make_bundle, TERMINAL_STATUSES, CAPTURE_MARKER, capture_binding, atomic_capture_marker,
    public_event_batches, verified_box_not_found)


class BoxLifecycle(BoxExecutionAdapter):
    def factory(self):
        if self.box_factory: return self.box_factory
        from upstash_box import AsyncBox
        return AsyncBox

    async def connect(self, handle):
        return await self.factory().get(handle['box_id'], api_key=self.environ['UPSTASH_BOX_API_KEY'])

    async def create_once(self, job_id, attempt):
        missing = required_environment(self.environ)
        if missing: raise BoxConfigurationError('Missing required environment: ' + ', '.join(missing))
        box = await self.factory().create(runtime='node', api_key=self.environ['UPSTASH_BOX_API_KEY'],
            labels=['reveal', 'job-' + hashlib.sha256(job_id.encode()).hexdigest()[:16], 'attempt-' + str(attempt)])
        try:
            return {'box_id': box.id, 'job_id': job_id, 'attempt': attempt,
                    'cursor': 0, 'phase': 'created', 'created_at': time.time(), 'timings': {}}
        finally: await box.aclose()

    async def prepare_once(self, request, handle):
        box = await self.connect(handle)
        try:
            await self.prepare(box, request, make_bundle(self.project_root, request))
            return dict(handle, phase='prepared', capture_protocol='s3-v1', timings={**handle.get('timings', {}), 'prepared_at': time.time()})
        finally: await box.aclose()

    def freeze_bootstrap(self, request, storage):
        from .direct_bootstrap import freeze_bootstrap
        return freeze_bootstrap(self, request, storage)

    async def prepare_from_store(self, descriptor, handle, storage):
        from .direct_bootstrap import prepare_from_store
        return await prepare_from_store(self, descriptor, handle, storage)

    async def launch_once(self, handle):
        box = await self.connect(handle)
        try:
            # The remote runner's flock protects retries after a lost launch response.
            await self.command(box, 'sudo -n env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/reveal/bundle/services/backend/src nohup /reveal/venv/bin/python -B -m reveal_backend.box_remote run > /tmp/reveal-runner.log 2>&1 < /dev/null &')
            return dict(handle, phase='running', timings={**handle.get('timings', {}), 'running_at': handle.get('timings', {}).get('running_at', time.time())})
        finally: await box.aclose()

    async def inspect_once(self, handle):
        box = await self.connect(handle)
        try: batch = json.loads(await self.remote(box, 'poll', handle['cursor']))
        finally: await box.aclose()
        if batch['cursor'] != handle['cursor'] + len(batch['events']):
            raise BoxTransportError('Remote cursor is inconsistent')
        events = []
        for chunk in public_event_batches(batch['events'], handle['cursor'],
                handle['box_id'] + ':' + handle['job_id'] + ':' + str(handle['attempt']),
                (self.environ[k] for k in ('ANTHROPIC_API_KEY', 'UPSTASH_BOX_API_KEY'))):
            events.extend(chunk)
        complete = batch['state']['status'] in TERMINAL_STATUSES and not batch['has_more']
        updated = dict(handle, cursor=batch['cursor'], state=batch['state'])
        if complete:
            updated['phase'] = 'terminal'
            updated['timings'] = {**handle.get('timings', {}), 'terminal_at': time.time()}
        return updated, events, complete

    async def capture_to_store(self, binding, handle, storage, workspace_ref):
        from .direct_capture import capture_to_store
        return await capture_to_store(self, binding, handle, storage, workspace_ref)

    async def capture_once(self, request, handle):
        if handle.get('state', {}).get('status') not in TERMINAL_STATUSES:
            raise BoxTransportError('Cannot capture a live remote ledger')
        box = await self.connect(handle)
        try: captured = json.loads(await self.remote(box, 'collect'))
        finally: await box.aclose()
        total, files = 0, {}
        request.output_dir.mkdir(parents=True, exist_ok=True)
        for name, encoded in captured['files'].items():
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts: raise BoxTransportError('Artifact path escapes capture')
            content = base64.b64decode(encoded, validate=True); total += len(content)
            if len(content) > capture_file_limit(name) or total > MAX_TOTAL: raise BoxTransportError('Capture size exceeds limit')
            if any(self.environ[k].encode() in content for k in ('ANTHROPIC_API_KEY', 'UPSTASH_BOX_API_KEY')):
                raise BoxTransportError('Output contains credential material')
            destination = request.output_dir / str(path); destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            files[name] = {'sha256': hashlib.sha256(content).hexdigest(), 'size_bytes': len(content)}
        if not files: raise BoxTransportError('Capture manifest is empty')
        # Verify the finalized trusted ledger and every referenced captured byte
        # before the caller may acknowledge deletion of the remote copy.
        try:
            ledger = json.loads((request.output_dir / 'ledger/manifest.json').read_bytes())
            if (ledger.get('format') != 'reveal.tool-ledger/1' or ledger.get('complete') is not True
                    or ledger.get('job_id') != request.job_id or ledger.get('attempt') != request.attempt):
                raise ValueError('Incomplete capture binding')
            for call in ledger['calls']:
                for field in ('request', 'response', 'upstream_request', 'upstream_response'):
                    descriptor = call.get(field)
                    if descriptor is None:
                        if field in ('request', 'response'): raise ValueError('Missing tool capture')
                        continue
                    relative = PurePosixPath(descriptor['path'])
                    if relative.is_absolute() or '..' in relative.parts: raise ValueError('Tool capture path escapes ledger')
                    captured_file = files.get(str(PurePosixPath('ledger') / relative))
                    if captured_file != {key: descriptor[key] for key in ('sha256', 'size_bytes')}:
                        raise ValueError('Tool capture checksum or size mismatch')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BoxTransportError('Downloaded ledger is incomplete; retain the remote copy') from exc
        updated = dict(handle, phase='captured', timings={**handle.get('timings', {}), 'captured_at': time.time()})
        atomic_capture_marker(request.output_dir / CAPTURE_MARKER, {'format': 'reveal.box-capture/1',
            'binding': capture_binding(request, handle), 'state': handle['state'], 'files': files,
            'cleanup_complete': False, 'timings': updated['timings']})
        return updated

    async def delete_once(self, handle):
        box = None
        try:
            box = await self.connect(handle)
            await box.delete()
        except Exception as exc:
            if not verified_box_not_found(exc): raise BoxTransportError('Box cleanup remains pending') from exc
        finally:
            if box: await box.aclose()
        return dict(handle, phase='deleted', timings={**handle.get('timings', {}), 'deleted_at': time.time()})

    async def cancel_once(self, handle):
        box = await self.connect(handle)
        try: await self.remote(box, 'cancel')
        finally: await box.aclose()
