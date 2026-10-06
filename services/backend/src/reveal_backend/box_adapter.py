"""Real Upstash Box transport using the pinned SDK and a trusted Claude runner."""
from __future__ import annotations
import asyncio
import base64
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile
import tempfile
import time

from .agent_execution import (ExecutionRequest, ExecutionResult, MAX_EMIT_BATCH_BYTES,
                              MAX_EMIT_BATCH_EVENTS, emit_batch_size)
from .box_mcp import GRAPHS
from .box_research import network_policy, validate_context, validate_access, validate_hosted_context
from .box_upload import MAX_TOTAL, capture_file_limit

CLAUDE_VERSION = '2.1.282'
MODEL = 'claude-sonnet-4-6'
REMOTE_PYTHON = '/reveal/venv/bin/python'
REMOTE_MODULE = 'reveal_backend.box_remote'
CAPTURE_MARKER = '.box-capture-complete.json'
TERMINAL_STATUSES = ('succeeded', 'failed', 'cancelled', 'insufficient_evidence')


def dispatchable_capture(package):
    """Complete evidence capture, or a KPN (eaggl-capped-v1) package whose only capture blockers are the
    trait-level PIGEAN phenotype queries its MySQL reference generation cannot answer."""
    readiness = package.get('readiness') or {}
    if package.get('retrieval_mode') == 'progressive':
        return package.get('seed_version') == 'reveal.research-seed/1' and readiness.get('seed_ready') is True
    if readiness.get('input_capture_complete') is True: return True
    from .reference_evidence import TRAIT_CAPTURE_BLOCKER
    blockers = readiness.get('capture_blockers') or []
    return (bool(blockers) and (package.get('pigean') or {}).get('model') == 'eaggl-capped-v1'
            and all(isinstance(blocker, str) and TRAIT_CAPTURE_BLOCKER.fullmatch(blocker) for blocker in blockers))


class BoxConfigurationError(ValueError):
    pass


class BoxTransportError(RuntimeError):
    """Recovery must reuse the persisted remote handle after this exception."""


def atomic_capture_marker(path, value):
    """Only the trusted local transport writes this marker, outside agent output."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.capture-', delete=False) as output:
            temporary = Path(output.name)
            output.write(json.dumps(value, sort_keys=True).encode())
            output.flush(); os.fsync(output.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    except OSError as exc:
        raise BoxTransportError('Local capture checkpoint is not durable; retain the remote handle') from exc
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def capture_binding(request, handle):
    return {'job_id': request.job_id, 'attempt': request.attempt, 'kind': request.kind,
            'box_id': handle['box_id'], 'selected_graphs': list(request.selected_graphs),
            'input_sha256': hashlib.sha256(request.input_path.read_bytes()).hexdigest()}


def read_capture_marker(request, handle):
    path = request.output_dir / CAPTURE_MARKER
    if not path.exists(): return None
    try:
        marker = json.loads(path.read_text())
        if marker['format'] != 'reveal.box-capture/1' or marker['binding'] != capture_binding(request, handle):
            raise ValueError('Capture binding differs')
        if marker['state']['status'] not in TERMINAL_STATUSES or type(marker['cleanup_complete']) is not bool:
            raise ValueError('Capture is not terminal')
        if not isinstance(marker['files'], dict) or not marker['files']:
            raise ValueError('Capture files are missing')
        total = 0
        for name, expected in marker['files'].items():
            relative = PurePosixPath(name)
            target = request.output_dir / name
            if relative.is_absolute() or '..' in relative.parts or target.is_symlink() or not target.resolve().is_relative_to(request.output_dir.resolve()):
                raise ValueError('Capture path escapes attempt')
            raw = target.read_bytes(); total += len(raw)
            if len(raw) > capture_file_limit(name) or total > MAX_TOTAL or len(raw) != expected['size_bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
                raise ValueError('Captured bytes changed')
        return marker
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BoxTransportError('Trusted local capture is incomplete or changed; recovery cannot accept it') from exc


def captured_result(request, handle, marker):
    files = marker['files']
    accounts = tuple(request.output_dir / name for name in sorted(files)
                     if re.fullmatch(r'output/account-[1-3]\.(json|yaml|yml)', name))
    def found(name): return request.output_dir / name if name in files else None
    return ExecutionResult(marker['state']['status'], request.output_dir, account_paths=accounts,
                           paragraph_path=found('output/paragraph.json'), runtime_manifest_path=found('runtime.json'),
                           ledger_manifest_path=found('ledger/manifest.json'), reason=marker['state'].get('reason'),
                           remote_handle=handle, outcome_path=found('output/outcome.json'),
                           research_receipts_path=found('ledger/research-receipts.json'))


def verified_box_not_found(exc):
    try:
        from upstash_box.errors import BoxError
    except ImportError:
        return False
    return isinstance(exc, BoxError) and exc.status_code == 404


def public_event_batches(events, cursor, stream_id, secrets):
    """Bound already available events without coalescing, clipping or waiting."""
    secrets = tuple(secrets)
    chunk = []
    for index, item in enumerate(events, start=cursor + 1):
        payload = dict(item['payload'], remote_sequence=index, remote_stream_id=stream_id)
        encoded = json.dumps(payload, ensure_ascii=False)
        for secret in secrets:
            encoded = encoded.replace(secret, '[REDACTED]')
        event = (item['type'], json.loads(encoded))
        if emit_batch_size([event]) > MAX_EMIT_BATCH_BYTES:
            raise BoxTransportError('Public event exceeds delivery limit; retain the remote cursor and capture')
        if chunk and (len(chunk) == MAX_EMIT_BATCH_EVENTS or emit_batch_size([*chunk, event]) > MAX_EMIT_BATCH_BYTES):
            yield chunk
            chunk = []
        chunk.append(event)
    if chunk:
        yield chunk


def required_environment(environ=None):
    environment = os.environ if environ is None else environ
    return [key for key in ('UPSTASH_BOX_API_KEY', 'ANTHROPIC_API_KEY') if not environment.get(key)]


def make_bundle(project_root: Path, request: ExecutionRequest):
    """Explicit file allowlist: never upload project .env, caches or unrelated code."""
    source = project_root / 'services/backend/src/reveal_backend'
    files = {}
    for name in ('__init__.py', 'evidence_package.py', 'dapper_release.py', 'scientific_account_lint.py', 'source_validation.py',
                 'box_remote.py', 'box_upload.py', 'box_stream.py', 'box_mcp.py', 'box_research.py', 'box_literature.py', 'research_outcome.py',
                 'dispatch_view.py', 'evidence_files.py', 'evidence_reader.py', 'authoring_contract.py', 'authoring_structure.py',
                 'box_timing.py', 'relationship_provenance.py', 'public_tool_activity.py'):
        files['bundle/services/backend/src/reveal_backend/' + name] = (source / name).read_bytes()
    relative = ['scripts/lint_scientific_account.py', 'services/backend/agent-runtime/dapper-release.json',
                'services/backend/agent-runtime/authoring-schema-dependencies.json',
                'services/backend/agent-runtime/linkml-types-1.11.1.yaml',
                'services/backend/agent-runtime/authoring-schema-excerpt.yaml',
                'services/backend/agent-runtime/authoring-examples.json', 'docs/authoring-contract.md', 'docs/local-agent-mcp.md', 'docs/local-workspaces-and-authentication.md',
                'services/backend/agent-skills/construct-scientific-account/SKILL.md',
                'services/backend/agent-skills/read-evidence-package/SKILL.md',
                'services/backend/agent-skills/write-cited-paragraph/SKILL.md',
                'docs/evidence-package.md', 'docs/scientific-account-construction.md', 'docs/pigean-claim-model.md',
                'docs/dapper-integration.md', 'docs/agent-evidence-integration.md', 'docs/scientific-account-linting.md']
    for name in relative:
        files['bundle/' + name] = (project_root / name).read_bytes()
    if request.input_path.stat().st_size > 8_000_000:
        raise BoxConfigurationError('Frozen input exceeds the byte budget')
    data = request.input_path.read_bytes()
    value = json.loads(data)
    if request.kind == 'research':
        from jsonschema import FormatChecker
        from jsonschema.validators import validator_for
        schema = json.loads((project_root / 'schema/evidence-package.schema.json').read_text())
        if value.get('retrieval_mode') == 'progressive':
            validate_hosted_context(value.get('research_context'))
            if value.get('package_version') != 'reveal.evidence-package/0.2-draft' or value.get('research_request_id') != value['research_context']['research_request_id']:
                raise BoxConfigurationError('Research seed identity is inconsistent')
        else:
            validator_for(schema)(schema, format_checker=FormatChecker()).validate(value)
        if not dispatchable_capture(value):
            raise BoxConfigurationError('Evidence capture is incomplete; paid execution is disabled')
        if set(value['external_evidence']['selected_graphs']) != set(request.selected_graphs):
            raise BoxConfigurationError('Selected graphs do not match the frozen evidence package')
        files['input/evidence-package.json'] = data
        from .dispatch_view import (BUDGET_FILENAME, VIEW_FILENAME, legacy_research_prompt, research_prompt, pinned_contract_sha256, pinned_skeleton_sha256,
                                    validate_dispatch_budget, validate_file_input)
        budget_path = request.input_path.parent / BUDGET_FILENAME
        view_path = request.input_path.parent / VIEW_FILENAME
        frozen_view = None
        # A resumed worker may bypass fit_input_budget. Require the same frozen
        # sidecars even if both were removed after its queue snapshot was saved.
        for parent in list(request.input_path.resolve().parents)[:3]:
            manifest_path = parent / 'dispatch-input.json'
            if not manifest_path.exists():
                continue
            frozen_input = json.loads(manifest_path.read_bytes())
            if (parent / frozen_input['path']).resolve() == request.input_path.resolve():
                if frozen_input['sha256'] != hashlib.sha256(data).hexdigest():
                    raise BoxConfigurationError('Frozen dispatch package changed')
                if frozen_input.get('format') not in ('reveal.dispatch-input/1', 'reveal.dispatch-input/2'):
                    raise BoxConfigurationError('Unknown frozen dispatch format')
                if frozen_input.get('format') == 'reveal.dispatch-input/2':
                    binding = frozen_input.get('file_input')
                    if not binding:
                        raise BoxConfigurationError('Frozen file input manifest is missing')
                    input_manifest = (parent / binding['path']).resolve()
                    if not input_manifest.is_relative_to(parent) or not input_manifest.is_file():
                        raise BoxConfigurationError('Frozen file input manifest escapes source capture or is missing')
                    manifest_data = input_manifest.read_bytes()
                    if hashlib.sha256(manifest_data).hexdigest() != binding['sha256']:
                        raise BoxConfigurationError('Frozen file input manifest changed')
                    validate_file_input(data, json.loads(manifest_data),
                                        research_prompt(request.selected_graphs, request.validation_feedback, progressive=value.get('retrieval_mode') == 'progressive',
                                                        contract_sha256=pinned_contract_sha256(value), skeleton_sha256=pinned_skeleton_sha256(value)))
                    files['input/evidence-input.json'] = manifest_data
                frozen_view = frozen_input.get('dispatch_view')
                if frozen_view:
                    view_path = (parent / frozen_view['path']).resolve()
                    budget_path = (parent / frozen_view['budget_path']).resolve()
                    if not view_path.is_relative_to(parent) or not budget_path.is_relative_to(parent):
                        raise BoxConfigurationError('Frozen dispatch sidecar path escapes source capture')
                break
        if frozen_view and not (budget_path.exists() and view_path.exists()):
            raise BoxConfigurationError('Frozen dispatch view or budget is missing')
        if budget_path.exists() or view_path.exists():
            if not (budget_path.exists() and view_path.exists()):
                raise BoxConfigurationError('Frozen dispatch view or budget is missing')
            budget_data, view_data = budget_path.read_bytes(), view_path.read_bytes()
            if frozen_view and (hashlib.sha256(view_data).hexdigest() != frozen_view['sha256'] or
                                hashlib.sha256(budget_data).hexdigest() != frozen_view['budget_sha256']):
                raise BoxConfigurationError('Frozen dispatch view or measurement changed')
            validate_dispatch_budget(data, view_data, json.loads(budget_data),
                                     legacy_research_prompt(request.selected_graphs, request.validation_feedback))
            # Legacy counts bind their historical prompt/view only. Keep those
            # local integrity checks without presenting them as verification
            # of the new file-reading prompt in the remote runtime.
        input_bytes = len(data)
        for item in value['source_artifacts'].values():
            path = PurePosixPath(item['path'])
            origin = (request.input_path.parent / str(path)).resolve()
            if path.is_absolute() or '..' in path.parts or not origin.is_relative_to(request.input_path.parent.resolve()):
                raise BoxConfigurationError('Source artifact path escapes input bundle')
            input_bytes += origin.stat().st_size
            if origin.stat().st_size > 8_000_000 or input_bytes > 40_000_000:
                raise BoxConfigurationError('Source artifacts exceed the input byte budget')
            content = origin.read_bytes()
            if hashlib.sha256(content).hexdigest() != item['sha256']:
                raise BoxConfigurationError('Source artifact checksum mismatch')
            files['input/' + str(path)] = content
    elif request.kind == 'paragraph':
        if value.get('format') != 'reveal.paragraph-input/1' or request.selected_graphs:
            raise BoxConfigurationError('Invalid paragraph input format or external graph selection')
        files['input/paragraph-input.json'] = data
    else:
        raise BoxConfigurationError('Unsupported execution kind')
    result = io.BytesIO()
    # The bootstrap sentinel identifies exact bundle bytes. Wall-clock gzip
    # metadata must not make a retry of identical frozen input look different.
    with gzip.GzipFile(fileobj=result, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for name, content in files.items():
                if len(content) > 8_000_000:
                    raise BoxConfigurationError('Input artifact exceeds limit')
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(content), 0o644, 0
                archive.addfile(info, io.BytesIO(content))
    if result.tell() > 25_000_000:
        raise BoxConfigurationError('Input bundle exceeds limit')
    return result.getvalue()


class BoxExecutionAdapter:
    def __init__(self, project_root: Path, *, environ=None, box_factory=None, poll_interval=0.7):
        self.project_root = Path(project_root).resolve()
        self.environ = os.environ if environ is None else environ
        self.box_factory = box_factory
        self.poll_interval = poll_interval

    async def command(self, box, command):
        try:
            run = await box.exec.command(command)
        except Exception as exc:
            raise BoxTransportError('Box command transport interrupted; retain the remote handle') from exc
        if str(run.status) != 'completed':
            # Do not interpolate remote command/stderr: credentials could occur there.
            raise BoxTransportError('Box command did not complete; inspect protected remote diagnostics')
        return run.result

    async def remote(self, box, action, *args):
        command = ['sudo', '-n', 'env', 'PYTHONDONTWRITEBYTECODE=1',
                   'PYTHONPATH=/reveal/bundle/services/backend/src', REMOTE_PYTHON,
                   '-B', '-m', REMOTE_MODULE, action, *[str(x) for x in args]]
        return await self.command(box, shlex.join(command))

    def request_config(self, request):
        config = {'job_id': request.job_id, 'attempt': request.attempt, 'kind': request.kind,
                  'selected_graphs': list(request.selected_graphs), 'timeout_seconds': request.timeout_seconds,
                  'max_budget_usd': request.max_budget_usd, 'max_turns': request.max_turns,
                  'model': self.environ.get('REVEAL_CLAUDE_MODEL', MODEL), 'claude_version': CLAUDE_VERSION,
                  'input_sha256': hashlib.sha256(request.input_path.read_bytes()).hexdigest()}
        config['validation_feedback'] = list(request.validation_feedback)
        if request.kind == 'research':
            package = json.loads(request.input_path.read_bytes())
            if package.get('retrieval_mode') == 'progressive':
                config['research_context'] = validate_context(package.get('research_context'))
                if request.research_access is not None:
                    validate_access(request.research_access, config['research_context'])
        return config

    async def prepare(self, box, request, bundle):
        config = self.request_config(request)
        # Persist a trusted bootstrap identity before narrowing network egress.
        # A lost policy-update response must not rerun apt/pip/npm behind the
        # now-restricted firewall. The root-owned sentinel binds the exact
        # harness bundle, input, model, limits and selected evidence services.
        fingerprint = hashlib.sha256(bundle + json.dumps(config, sort_keys=True).encode()).hexdigest()
        marker = await self.command(box, "sudo -n sh -c 'if [ -f /reveal/state/bootstrap-ready ]; then cat /reveal/state/bootstrap-ready; fi'")
        policy = network_policy(config.get('research_context'))
        if marker.strip():
            if marker.strip() != fingerprint:
                raise BoxConfigurationError('Existing Box bootstrap belongs to different frozen input or harness')
            if config.get('research_context'):
                await self.finish_prepare(box, fingerprint, research_context=config['research_context'], research_access=request.research_access)
            else:
                await box.update_network_policy(policy)
            return
        await box.files.write(path='/tmp/reveal-bundle.tgz', content=base64.b64encode(bundle).decode(), encoding='base64')
        await box.files.write(path='/tmp/reveal-request.json', content=json.dumps(config))
        # Setup is a trusted static command; no model-authored shell or credentials.
        bootstrap = self.bootstrap_script(config['claude_version'])
        await box.files.write(path='/tmp/reveal-bootstrap.sh', content=bootstrap)
        await self.command(box, 'sh /tmp/reveal-bootstrap.sh')
        await self.finish_prepare(box, fingerprint, research_context=config.get('research_context'), research_access=request.research_access)

    @staticmethod
    def bootstrap_script(claude_version, *, unpack=True):
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', claude_version):
            raise BoxConfigurationError('Invalid frozen Claude runtime version')
        return '''set -eu
sudo mkdir -p /reveal/state
''' + ('''sudo tar -xzf /tmp/reveal-bundle.tgz -C /reveal
sudo mv /tmp/reveal-request.json /reveal/request.json
''' if unpack else '') + '''id reveal-agent >/dev/null 2>&1 || sudo useradd --create-home --uid 1999 --shell /usr/sbin/nologin reveal-agent
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv
sudo python3 -m venv /reveal/venv
sudo /reveal/venv/bin/pip -q install PyYAML==6.0.2 linkml==1.11.1 rdflib==7.6.0
sudo mkdir -p /reveal/claude
sudo npm install --prefix /reveal/claude --no-audit --no-fund @anthropic-ai/claude-code@''' + claude_version + '''
sudo chmod 755 /reveal
sudo chmod 755 /reveal/state
sudo /reveal/claude/node_modules/.bin/claude --version
'''

    async def finish_prepare(self, box, fingerprint, *, research_context=None, research_access=None):
        # Secret only uses structured SDK file input, then root-only protection before launch.
        credentials = {'ANTHROPIC_API_KEY': self.environ['ANTHROPIC_API_KEY']}
        if research_context:
            credentials['REVEAL_RESEARCH_TOKEN'] = validate_access(research_access, research_context)
        elif research_access is not None:
            raise BoxConfigurationError('Research credential has no frozen context')
        await box.files.write(path='/tmp/reveal-credential.json', content=json.dumps(credentials))
        await self.command(box, 'sudo mv /tmp/reveal-credential.json /reveal/credentials.json && sudo chown root:root /reveal/credentials.json && sudo chmod 600 /reveal/credentials.json')
        await self.command(box, "sudo -n sh -c " + shlex.quote("printf '%s' " + fingerprint + " > /reveal/state/bootstrap-ready && chmod 600 /reveal/state/bootstrap-ready"))
        # After installation only Anthropic and the fixed evidence service are reachable.
        await box.update_network_policy(network_policy(research_context))
        # DAPPER clone is intentionally fresh and requires github.com after policy tightening.

    async def execute(self, request, emit, cancelled, checkpoint):
        missing = required_environment(self.environ)
        if missing:
            raise BoxConfigurationError('Missing required environment: ' + ', '.join(missing))
        if set(request.selected_graphs) - set(GRAPHS) or not 1 <= request.attempt or not 10 <= request.timeout_seconds <= 3600:
            raise BoxConfigurationError('Invalid execution graph, attempt or time limit')
        if not math.isfinite(request.max_budget_usd) or request.max_budget_usd <= 0 or not 1 <= request.max_turns <= 100:
            raise BoxConfigurationError('Invalid execution budget')
        if len(request.validation_feedback) > 10 or any(not isinstance(x, str) or len(x) > 4000 for x in request.validation_feedback):
            raise BoxConfigurationError('Validation feedback exceeds limits')
        bundle = make_bundle(self.project_root, request) if not request.remote_handle else None
        request.output_dir.mkdir(parents=True, exist_ok=True)
        if self.box_factory is None:
            from upstash_box import AsyncBox
            factory = AsyncBox
        else:
            factory = self.box_factory
        box, handle, terminal = None, dict(request.remote_handle or {}), False
        marker = None
        checkpointed = bool(request.remote_handle)
        preserve_capture = False
        try:
            if handle:
                if handle.get('job_id') != request.job_id or handle.get('attempt') != request.attempt:
                    raise BoxConfigurationError('Remote handle belongs to a different attempt')
                marker = read_capture_marker(request, handle)
                if marker:
                    handle.setdefault('timings', {}).update(marker.get('timings', {}))
                if marker and (marker['cleanup_complete'] or handle.get('phase') == 'deleted'):
                    await emit('stage', {'stage': 'collecting_output',
                                        'message': 'Restoring the saved results before validation.'})
                    handle['phase'] = 'deleted'
                    handle.setdefault('timings', {}).setdefault('deleted_at', time.time())
                    try: await checkpoint(handle.copy())
                    except Exception as exc:
                        raise BoxTransportError('Completed local capture awaits its cleanup checkpoint') from exc
                    return captured_result(request, handle, marker)
                try:
                    box = await factory.get(handle['box_id'], api_key=self.environ['UPSTASH_BOX_API_KEY'])
                except Exception as exc:
                    if marker and verified_box_not_found(exc):
                        marker['cleanup_complete'] = True
                        atomic_capture_marker(request.output_dir / CAPTURE_MARKER, marker)
                        handle['phase'] = 'deleted'
                        handle.setdefault('timings', {}).setdefault('deleted_at', time.time())
                        try: await checkpoint(handle.copy())
                        except Exception as checkpoint_error:
                            raise BoxTransportError('Verified deleted Box awaits its cleanup checkpoint') from checkpoint_error
                        return captured_result(request, handle, marker)
                    raise BoxTransportError('Box reconnect failed; retain the existing handle') from exc
                if marker:
                    await emit('stage', {'stage': 'collecting_output',
                                        'message': 'Finalizing the saved results before validation.'})
                    terminal = True  # Complete local capture; only acknowledged cleanup remains.
                    return captured_result(request, handle, marker)
            else:
                box = await factory.create(runtime='node', api_key=self.environ['UPSTASH_BOX_API_KEY'],
                                           labels=['reveal', 'job-' + hashlib.sha256(request.job_id.encode()).hexdigest()[:16], 'attempt-' + str(request.attempt)])
                handle = {'box_id': box.id, 'job_id': request.job_id, 'attempt': request.attempt, 'cursor': 0,
                          'phase': 'created', 'created_at': time.time()}
                await checkpoint(handle.copy())  # Persist before any paid model execution.
                checkpointed = True
                await emit('stage', {'stage': 'starting_agent', 'state': 'started', 'source': 'harness',
                                     'message': 'Preparing an isolated agent runtime.'})
                try:
                    await self.prepare(box, request, bundle)
                except Exception as exc:
                    raise BoxTransportError('Box preparation interrupted; recover using its saved handle') from exc
                handle.setdefault('timings', {})['prepared_at'] = time.time()
                handle['phase'] = 'prepared'; await checkpoint(handle.copy())
            # A resumed running attempt must reach cancel/poll/collect so its
            # durable tool ledger is finalized before the Box is deleted.
            if handle['phase'] != 'running' and await cancelled():
                terminal = True
                return ExecutionResult('cancelled', request.output_dir, reason='Cancelled before execution', remote_handle=handle)
            if handle['phase'] == 'created':
                # Preparation interrupted: never reuse a partly constructed trusted workspace.
                terminal = True
                return ExecutionResult('failed', request.output_dir, reason='Runtime preparation interrupted; retry with a fresh attempt', remote_handle=handle)
            if handle['phase'] == 'prepared':
                # Idempotent remote flock/status guard makes uncertain launch recovery safe.
                launch = 'sudo -n env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/reveal/bundle/services/backend/src nohup /reveal/venv/bin/python -B -m reveal_backend.box_remote run > /tmp/reveal-runner.log 2>&1 < /dev/null &'
                await self.command(box, launch)
                handle.setdefault('timings', {}).setdefault('running_at', time.time())
                handle['phase'] = 'running'; await checkpoint(handle.copy())
            errors = 0
            while True:
                if await cancelled():
                    await self.remote(box, 'cancel')
                try:
                    batch = json.loads(await self.remote(box, 'poll', handle['cursor']))
                    errors = 0
                except (BoxTransportError, OSError, ValueError):
                    errors += 1
                    if errors > 3:
                        raise BoxTransportError('Box polling disconnected; resume the same persisted handle')
                    await asyncio.sleep(min(errors, 3)); continue
                if batch['cursor'] != handle['cursor'] + len(batch['events']):
                    raise BoxTransportError('Remote event cursor is inconsistent; retain the saved handle')
                chunks = public_event_batches(batch['events'], handle['cursor'],
                    handle['box_id'] + ':' + request.job_id + ':' + str(request.attempt),
                    (self.environ[key] for key in ('ANTHROPIC_API_KEY', 'UPSTASH_BOX_API_KEY')))
                batch_emit = getattr(emit, 'emit_batch', None)
                for chunk_index, chunk in enumerate(chunks):
                    if chunk_index and await cancelled():
                        await self.remote(box, 'cancel')
                    if callable(batch_emit):
                        await batch_emit(chunk)
                        handle['cursor'] = chunk[-1][1]['remote_sequence']
                        await checkpoint(handle.copy())
                    else:
                        for kind, payload in chunk:
                            await emit(kind, payload)
                            handle['cursor'] = payload['remote_sequence']
                            await checkpoint(handle.copy())
                state = batch['state']
                remote_terminal = state['status'] in TERMINAL_STATUSES
                if remote_terminal and not batch['has_more']:
                    handle.setdefault('timings', {}).setdefault('terminal_at', time.time())
                    break
                # A terminal runner already froze its ledger. Slow callback
                # delivery must not cancel or abandon its remaining event pages.
                if not remote_terminal and time.time() - handle['created_at'] > request.timeout_seconds + 420:
                    await self.remote(box, 'cancel')
                    # Wait for trusted terminal state and ledger freeze. A
                    # timeout must not race collection against in-flight writes.
                    if time.time() - handle['created_at'] > request.timeout_seconds + 480:
                        raise BoxTransportError('Box cancellation not yet finalized; resume the existing handle')
                await asyncio.sleep(self.poll_interval)
            await emit('stage', {'stage': 'collecting_output',
                                'message': 'Retrieving the agent’s results and captured evidence.'})
            captured = json.loads(await self.remote(box, 'collect'))
            total, captured_files = 0, {}
            for name, encoded in captured['files'].items():
                relative = PurePosixPath(name)
                if relative.is_absolute() or '..' in relative.parts:
                    raise BoxTransportError('Remote artifact path escapes attempt')
                content = base64.b64decode(encoded, validate=True); total += len(content)
                if any(self.environ[key].encode() in content for key in ('ANTHROPIC_API_KEY', 'UPSTASH_BOX_API_KEY')):
                    raise BoxTransportError('Credential material detected in an output artifact; capture rejected')
                if len(content) > capture_file_limit(name) or total > MAX_TOTAL:
                    raise BoxTransportError('Remote artifact exceeds capture limit')
                target = request.output_dir / str(relative)
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('wb') as output:
                        output.write(content); output.flush(); os.fsync(output.fileno())
                except OSError as exc:
                    raise BoxTransportError('Local artifact capture interrupted; retain the remote handle') from exc
                captured_files[name] = {'sha256': hashlib.sha256(content).hexdigest(), 'size_bytes': len(content)}
            handle.setdefault('timings', {}).setdefault('captured_at', time.time())
            marker = {'format': 'reveal.box-capture/1', 'binding': capture_binding(request, handle),
                      'state': state, 'files': captured_files, 'cleanup_complete': False,
                      'timings': dict(handle['timings'])}
            atomic_capture_marker(request.output_dir / CAPTURE_MARKER, marker)
            # Keep the remote copy until captured bytes have reached durable
            # storage outside the disposable worker.
            preserve_capture = True
            handle['phase'] = 'captured'
            await emit('stage', {'stage': 'collecting_output',
                                'message': 'Saving the results and evidence to durable storage.'})
            try: await checkpoint(handle.copy())
            except Exception as exc:
                raise BoxTransportError('Captured output awaits durable storage; retain the remote copy') from exc
            preserve_capture = False
            handle['timings']['capture_saved_at'] = time.time()
            terminal = True  # Delete only after all attempt artifacts are durable locally.
            return captured_result(request, handle, marker)
        finally:
            if box:
                cleanup_error = None
                if (terminal and not preserve_capture) or not checkpointed:
                    try:
                        if marker:
                            await emit('stage', {'stage': 'collecting_output',
                                                'message': 'Finalizing the saved results before validation.'})
                        await box.delete()
                        handle.setdefault('timings', {}).setdefault('deleted_at', time.time())
                        if marker:
                            marker['cleanup_complete'] = True
                            marker['timings'] = dict(handle['timings'])
                            atomic_capture_marker(request.output_dir / CAPTURE_MARKER, marker)
                        handle['phase'] = 'deleted'; await checkpoint(handle.copy())
                    except Exception as exc:
                        await emit('warning', {'code': 'box_cleanup_pending', 'message': 'Remote Box cleanup needs retry.'})
                        cleanup_error = BoxTransportError('Box deletion or cleanup checkpoint interrupted; retain captured artifacts and remote handle')
                        cleanup_error.__cause__ = exc
                await box.aclose()
                if cleanup_error:
                    # A terminal job would never reclaim a leaked Box. Keep the
                    # lease recoverable until remote cleanup is acknowledged.
                    raise cleanup_error
