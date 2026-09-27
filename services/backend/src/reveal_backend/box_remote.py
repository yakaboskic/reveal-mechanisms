"""Trusted root process inside Box. Not an agent-authored executable.

The transport uploads this module and a minimal project bundle. Every fresh
attempt clones and verifies DAPPER before a separate unprivileged Claude uid runs.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time

from .box_mcp import DraftValidationError, Ledger, PolicyError, ScopedTools, canonical, serve, stamp
from .box_stream import ClaudeStream, SecretFilter, StreamProtocolError
from .dispatch_view import (BUDGET_FILENAME, VIEW_FILENAME, dispatch_view, research_authoring_requirements,
                            research_prompt, validate_dispatch_budget)

BASE = Path('/reveal')
STATE = BASE / 'state'
OUTPUT = BASE / 'output'
SECRETS = ()


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(canonical(data)); temporary.replace(path)


def emit(kind, payload):
    raw = canonical({'type': kind, 'payload': payload})
    for secret in SECRETS:
        raw = raw.replace(secret.encode(), b'[REDACTED_CREDENTIAL]')
    with (STATE / 'events.jsonl').open('ab') as handle:
        handle.write(raw + b'\n')
        handle.flush(); os.fsync(handle.fileno())


def protect(root):
    for path in [root, *root.rglob('*')]:
        if path.is_symlink():
            continue
        os.chown(path, 0, 0)
        os.chmod(path, 0o555 if path.is_dir() or os.access(path, os.X_OK) else 0o444)



def setup(request):
    from .dapper_release import clone_release, prepare_agent_workspace
    project = BASE / 'bundle'
    lock = project / 'services/backend/agent-runtime/dapper-release.json'
    if request['kind'] == 'research':
        runtime = prepare_agent_workspace(BASE / 'workspace', project, BASE / 'input/evidence-package.json', lock)
        work = Path(runtime['working_directory'])
        package = Path(runtime['evidence_package'])
        # The bootstrap already verifies all original artifact bytes.
        frozen = json.loads(package.read_text())
        if set(request['selected_graphs']) != set(frozen['external_evidence']['selected_graphs']):
            raise ValueError('Selected graph bindings differ from the immutable package')
        # Line-readable derived views prevent repeated truncated reads of compact JSON.
        # The canonical package and source artifact bytes remain unchanged.
        sections = work / 'input/package-sections'
        sections.mkdir()
        views = {}
        for name, value in frozen.items():
            target = sections / (name + '.json')
            data = json.dumps(value, ensure_ascii=False, indent=2).encode() + b'\n'
            target.write_bytes(data)
            views[str(target.relative_to(work))] = hashlib.sha256(data).hexdigest()
        runtime['derived_input_views'] = views
        import yaml
        schema_root = Path(runtime['dapper_root']) / 'schema'
        schema_parts = [yaml.safe_load((schema_root / filename).read_text()) for filename in ('dapper.yaml', 'claims.yaml')]
        wanted = {'Claim', 'Proposition', 'EvidenceItem', 'ClaimScore', 'ScientificAccount', 'ProvenancedResource', 'File', 'Activity', 'Paragraph'}
        excerpt = {'note': 'Exact excerpts for authoring convenience. The full pinned schema remains authoritative.', 'classes': {}, 'slots': {}, 'enums': {}}
        for part in schema_parts:
            excerpt['classes'].update({key: value for key, value in part.get('classes', {}).items() if key in wanted})
            excerpt['enums'].update(part.get('enums', {}))
        used_slots = {slot for value in excerpt['classes'].values() for slot in value.get('slots', [])}
        for part in schema_parts:
            excerpt['slots'].update({key: value for key, value in part.get('slots', {}).items() if key in used_slots})
        excerpt_path = sections / 'authoring-schema-excerpt.yaml'
        excerpt_path.write_text(yaml.safe_dump(excerpt, sort_keys=False))
        views[str(excerpt_path.relative_to(work))] = hashlib.sha256(excerpt_path.read_bytes()).hexdigest()
        prompt = research_prompt(request['selected_graphs'], request.get('validation_feedback', ()))
        view_bytes = dispatch_view(package.read_bytes())
        view_path = work / 'input' / VIEW_FILENAME
        view_path.write_bytes(view_bytes)
        views[str(view_path.relative_to(work))] = hashlib.sha256(view_bytes).hexdigest()
        files_path = sections / 'dapper-files.json'
        files_path.write_text(json.dumps(frozen['dapper_context'].get('files', []), ensure_ascii=False, indent=2) + '\n')
        views[str(files_path.relative_to(work))] = hashlib.sha256(files_path.read_bytes()).hexdigest()
        frozen_budget = BASE / 'input' / BUDGET_FILENAME
        if frozen_budget.exists():
            budget = json.loads(frozen_budget.read_bytes())
            supplied_view = (BASE / 'input' / VIEW_FILENAME).read_bytes()
            validate_dispatch_budget(package.read_bytes(), supplied_view, budget, prompt, request['model'])
            runtime['dispatch_budget'] = budget
        runtime['dispatch_view_sha256'] = hashlib.sha256(view_bytes).hexdigest()
        runtime['research_prompt_sha256'] = hashlib.sha256(prompt.encode()).hexdigest()

    else:
        root = BASE / 'workspace'
        root.mkdir()
        release = clone_release(root / 'dapper', lock)
        work = BASE / 'trusted'
        work.mkdir()
        shutil.copyfile(BASE / 'input/paragraph-input.json', work / 'paragraph-input.json')
        paragraph_input = json.loads((work / 'paragraph-input.json').read_text())
        if paragraph_input.get('format') != 'reveal.paragraph-input/1' or request['selected_graphs']:
            raise ValueError('Invalid paragraph inputs or forbidden external tools')
        readable = work / 'paragraph-input-readable.json'
        readable.write_text(json.dumps(paragraph_input, ensure_ascii=False, indent=2) + '\n')
        skill = project / 'services/backend/agent-skills/write-cited-paragraph/SKILL.md'
        runtime = {'runtime_version': 'reveal.agent-runtime/1', 'dapper': release,
                   'dapper_root': str(root / 'dapper'), 'working_directory': str(work),
                   'evidence_package_sha256': hashlib.sha256((work / 'paragraph-input.json').read_bytes()).hexdigest(),
                   'derived_input_views': {readable.name: hashlib.sha256(readable.read_bytes()).hexdigest()}}
        prompt = f'Read and follow {skill}. Use only the frozen /reveal/trusted/paragraph-input.json; /reveal/trusted/paragraph-input-readable.json contains the identical object formatted across lines for economical reads. Write /reveal/output/paragraph.json. No fresh evidence or new propositions.'
    runtime.update({'provider': 'anthropic', 'model': request['model'], 'harness': 'Claude Code',
                    'harness_version': request['claude_version'], 'started_at': stamp(),
                    'job_id': request['job_id'], 'attempt': request['attempt'],
                    'execution_limits': {k: request[k] for k in ['timeout_seconds', 'max_budget_usd', 'max_turns']},
                    'input_sha256': request['input_sha256']})
    runtime['validation_feedback_sha256'] = hashlib.sha256(canonical(request.get('validation_feedback', []))).hexdigest()
    if request['kind'] == 'research':
        executor_id = 'urn:reveal:runtime:executor'
        activity_id = 'urn:reveal:runtime:execution:' + request['job_id'] + ':' + str(request['attempt'])
        runtime['draft_attribution'] = {'organizations': [{'id': executor_id, 'name': 'REVEAL Mechanisms runtime executor'}],
                                      'activities': [{'id': activity_id, 'name': 'Claude Code research execution',
                                                      'command': 'reveal Box scientific-account authoring',
                                                      'software_name': 'Claude Code', 'software_version': request['claude_version'],
                                                      'generated_at_time': runtime['started_at']}]}
        (work / 'runtime-context.json').write_bytes(canonical(runtime['draft_attribution']))
    if request['kind'] != 'research' and request.get('validation_feedback'):
        prompt += '\nTrusted independent review feedback from a rejected earlier draft. Address these constraints afresh; they are not new evidence:\n' + '\n'.join(request['validation_feedback'])
    write_json(STATE / 'runtime.json', runtime)
    for target in [project, BASE / 'input', BASE / 'workspace']:
        protect(target)
    if (BASE / 'trusted').exists():
        protect(BASE / 'trusted')
    return work, runtime, prompt


def terminate(process):
    if process.poll() is None:
        def send_signal(name):
            # Box root has SETUID but may lack CAP_KILL for a different uid.
            user = pwd.getpwnam('reveal-agent')
            command = ['setpriv', '--reuid', str(user.pw_uid), '--regid', str(user.pw_gid),
                       '--clear-groups', '--no-new-privs', '/bin/kill', '-' + name, '--', '-' + str(process.pid)]
            subprocess.run(command, capture_output=True, check=False, timeout=5)
        send_signal('TERM')
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            send_signal('KILL')
            process.wait(timeout=5)


def lint_tool(filename):
    from .scientific_account_lint import lint_scientific_account
    path = OUTPUT / filename
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'Account output missing, symlinked or oversized'}]}
    frozen = STATE / ('lint-' + filename)
    frozen.write_bytes(path.read_bytes())
    runtime = json.loads((STATE / 'runtime.json').read_text())
    report = lint_scientific_account(frozen, dapper_root=runtime['dapper_root'],
                                     release_lock=BASE / 'bundle/services/backend/agent-runtime/dapper-release.json',
                                     evidence_package=runtime['evidence_package'], mode='draft')
    return {'content': [{'type': 'text', 'text': json.dumps(report)}], 'isError': not report.get('valid')}


def write_draft_tool(filename, document):
    """Representation assistance only: source hydration never implies acceptance."""
    if not isinstance(document, dict) or len(canonical(document)) > 4_000_000:
        raise DraftValidationError('Invalid or oversized draft document')
    if not isinstance(document.get('scientific_accounts'), list) or len(document['scientific_accounts']) != 1:
        raise DraftValidationError('Expected document={"scientific_accounts":[one account],"claims":[...],"propositions":[...],"evidence_items":[...]}; group values must be arrays, not a class instance or graph/nodes envelope')
    runtime = json.loads((STATE / 'runtime.json').read_text())
    package = json.loads(Path(runtime['evidence_package']).read_text())
    trusted = {n['id']: (group, n) for group, rows in package['dapper_context'].items() if isinstance(rows, list) for n in rows if isinstance(n, dict) and 'id' in n}
    for rows in document.values():
        if isinstance(rows, list):
            for node in rows:
                if isinstance(node, dict) and node.get('id') in trusted and node != trusted[node['id']][1]:
                    raise PolicyError('A trusted source object was changed')
    for _ in range(len(trusted) + 1):
        present = {n['id'] for rows in document.values() if isinstance(rows, list) for n in rows if isinstance(n, dict) and 'id' in n}
        serialized = json.dumps(document)
        missing = [identity for identity in trusted if identity not in present and identity in serialized]
        if not missing:
            break
        for identity in missing:
            group, node = trusted[identity]
            document.setdefault(group, []).append(node)
    context = runtime.get('draft_attribution')
    if context:
        for group in ('persons', 'organizations', 'activities'):
            document[group] = [node for node in document.get(group, []) if node.get('id') in trusted]
            document[group].extend(context.get(group, []))
        for group in ('claims', 'scientific_accounts'):
            for node in document.get(group, []):
                if node.get('id') not in trusted:
                    node['was_generated_by'] = context['activities'][0]['id']
                    node['was_attributed_to'] = [context['organizations'][0]['id']]
    path = OUTPUT / filename
    if path.is_symlink():
        raise PolicyError('Draft output symlink forbidden')
    path.write_bytes(canonical(document))
    user = pwd.getpwnam('reveal-agent')
    os.chown(path, user.pw_uid, user.pw_gid)
    return {'content': [{'type': 'text', 'text': 'Draft saved to ' + str(path) + '. Run lint_account; this file has not been accepted or minted.'}]}


def main():
    global SECRETS
    if os.getuid() != 0:
        raise RuntimeError('The trusted Box runner must be root')
    STATE.mkdir(exist_ok=True)
    # flock fences duplicated launch requests after an uncertain network response.
    import fcntl
    lockfile = (STATE / 'runner.lock').open('w')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    if (STATE / 'status.json').exists():
        return 0
    request = json.loads((BASE / 'request.json').read_text())
    ledger = Ledger(STATE / 'ledger', request['job_id'], request['attempt'])
    process = server = None
    builtin_calls = {}
    status, reason = 'failed', None
    started = time.monotonic()
    write_json(STATE / 'status.json', {'status': 'preparing', 'started_at': stamp(), 'pid': os.getpid()})
    try:
        work, runtime, prompt = setup(request)
        actual_version = subprocess.run(['/reveal/claude/node_modules/.bin/claude', '--version'], capture_output=True, text=True, check=True, timeout=15).stdout.strip()
        if actual_version.split()[0] != request['claude_version']:
            raise ValueError('Installed Claude Code version differs from the pinned harness')
        runtime['observed_harness_version'] = actual_version
        user = pwd.getpwnam('reveal-agent')
        OUTPUT.mkdir(exist_ok=True)
        os.chown(OUTPUT, user.pw_uid, user.pw_gid)
        os.chmod(OUTPUT, 0o700)
        tools = ScopedTools(request['selected_graphs'], ledger, lint=lint_tool if request['kind'] == 'research' else None,
                            write_draft=write_draft_tool if request['kind'] == 'research' else None)
        server = serve(tools)
        config = {'mcpServers': {'reveal': {'type': 'http', 'url': 'http://127.0.0.1:8765/mcp'}}}
        config_path = BASE / 'mcp.json'
        config_path.write_bytes(canonical(config)); os.chmod(config_path, 0o444)
        api_key = (BASE / 'credentials.json').read_text()
        (BASE / 'credentials.json').unlink()
        SECRETS = (json.loads(api_key)['ANTHROPIC_API_KEY'],)
        ledger.secrets = SECRETS
        env = {'PATH': '/reveal/venv/bin:/usr/local/bin:/usr/bin:/bin', 'HOME': user.pw_dir,
               'ANTHROPIC_API_KEY': json.loads(api_key)['ANTHROPIC_API_KEY'],
               'PYTHONDONTWRITEBYTECODE': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
               'DISABLE_AUTOUPDATER': '1', 'REVEAL_DAPPER_ROOT': runtime['dapper_root']}
        command = ['setpriv', '--reuid', str(user.pw_uid), '--regid', str(user.pw_gid), '--clear-groups', '--no-new-privs',
                   '/usr/bin/timeout', '--signal=TERM', '--kill-after=10s', str(max(1, int(request['timeout_seconds'] - (time.monotonic() - started)))),
                   '/reveal/claude/node_modules/.bin/claude', '-p', '--verbose', '--output-format', 'stream-json',
                   '--include-partial-messages', '--model', request['model'], '--max-turns', str(request['max_turns']),
                   '--effort', 'medium',
                   '--max-budget-usd', str(request['max_budget_usd']), '--permission-mode', 'bypassPermissions',
                   '--tools', 'Read,Glob,Grep,Write,Edit', '--strict-mcp-config', '--mcp-config', str(config_path),
                   '--setting-sources', '', '--disable-slash-commands']
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=work, env=env, start_new_session=True)
        process.stdin.write(prompt.encode()); process.stdin.close()
        write_json(STATE / 'status.json', {'status': 'running', 'started_at': stamp(), 'pid': os.getpid(), 'agent_pid': process.pid})
        emit('agent_started', {'message': 'Claude is reading the frozen evidence and authoring a result.', 'model': request['model']})
        parser = ClaudeStream()
        trace_filter, error_filter = SecretFilter(SECRETS), SecretFilter(SECRETS)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        # Private raw trace is never an SSE payload. Restrict access to the trusted runner.
        with (STATE / 'claude-stream.jsonl').open('wb') as trace, (STATE / 'claude-stderr.txt').open('wb') as errors:
            os.chmod(trace.name, 0o600); os.chmod(errors.name, 0o600)
            total = 0
            while selector.get_map():
                if (STATE / 'cancel').exists():
                    status, reason = 'cancelled', 'Cancelled by the owning job'
                    terminate(process); break
                if time.monotonic() - started > request['timeout_seconds']:
                    reason = 'Agent execution timed out'; terminate(process); break
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    total += len(chunk)
                    if total > 40_000_000:
                        raise StreamProtocolError('Agent trace exceeded capture budget')
                    if key.data == 'stdout':
                        trace.write(trace_filter.feed(chunk)); trace.flush()
                        for kind, payload in parser.feed(chunk):
                            if kind == 'tool_call':
                                call = parser.tools[payload['call_id']]
                                entry = ledger.start(call['name'], call['input'], None)
                                entry['claude_call_id'] = payload['call_id']
                                builtin_calls[payload['call_id']] = entry
                            elif kind == 'tool_result':
                                call_id = payload['call_id']
                                result = parser.tools[call_id]['result']
                                ledger.finish(builtin_calls[call_id], result, 'failed' if result.get('is_error') else 'completed')
                            emit(kind, payload)
                    else:
                        errors.write(error_filter.feed(chunk)); errors.flush()
            trace.write(trace_filter.feed(b'', final=True)); trace.flush()
            errors.write(error_filter.feed(b'', final=True)); errors.flush()
            if process.poll() is None:
                process.wait(timeout=10)
        if not reason:
            if process.returncode in (124, 137):
                raise TimeoutError('The independent agent deadline expired')
            for kind, payload in parser.finish():
                emit(kind, payload)
            if process.returncode != 0 or parser.result.get('is_error'):
                reason = 'Claude execution failed: ' + str(parser.result.get('subtype', 'nonzero exit'))
            else:
                status = 'succeeded'
                if (OUTPUT / 'outcome.json').exists():
                    outcome = json.loads((OUTPUT / 'outcome.json').read_text())
                    if outcome.get('status') in ('insufficient_evidence', 'failed'):
                        status, reason = outcome['status'], str(outcome.get('reason', ''))[:2000]
                expected = list(OUTPUT.glob('account-*.*')) if request['kind'] == 'research' else list(OUTPUT.glob('paragraph.json'))
                if status == 'succeeded' and not expected:
                    status, reason = 'failed', 'Claude completed without the required output documents'
        runtime['completed_at'] = stamp()
        runtime['cost_usd'] = parser.result.get('total_cost_usd') if parser.result else None
        runtime['observed_claude_runtime'] = parser.runtime
        write_json(STATE / 'runtime.json', runtime)
    except Exception as exc:
        # Values from provider exceptions can contain credentials. Keep diagnostics typed.
        status = 'failed'
        reason = type(exc).__name__ + ': trusted agent execution failed'
        emit('warning', {'code': 'agent_runtime_error', 'message': reason})
        (STATE / 'failure-type.txt').write_text(type(exc).__name__)
    finally:
        if process:
            terminate(process)
        if server:
            server.shutdown()
        ledger.freeze()
        write_json(STATE / 'status.json', {'status': status, 'reason': reason, 'completed_at': stamp()})
    return 0


def poll(cursor=0):
    status_path = STATE / 'status.json'
    status = json.loads(status_path.read_text()) if status_path.exists() else {'status': 'not_started'}
    if status.get('status') in ('preparing', 'running') and status.get('pid'):
        try:
            os.kill(status['pid'], 0)
            alive = True
        except ProcessLookupError:
            alive = False
        if not alive:
            # Recover durable tool starts/results conservatively; never invent a missing result.
            request = json.loads((BASE / 'request.json').read_text())
            ledger = Ledger(STATE / 'ledger', request['job_id'], request['attempt'])
            events = STATE / 'ledger/events.jsonl'
            latest = {}
            for line in events.read_text().splitlines() if events.exists() else []:
                event = json.loads(line)
                latest[event['sequence']] = {key: value for key, value in event.items() if key != 'phase'}
            ledger.entries = [latest[key] for key in sorted(latest)]
            ledger.freeze()
            status = {'status': 'cancelled' if (STATE / 'cancel').exists() else 'failed',
                      'reason': 'Trusted runner stopped before terminal state; unfinished calls marked interrupted', 'completed_at': stamp()}
            write_json(status_path, status)
    path = STATE / 'events.jsonl'
    lines = path.read_text().splitlines() if path.exists() else []
    # Caller checkpoints cursor only after durable event writes. Replay is at least once.
    items = [json.loads(line) for line in lines[cursor:cursor + 100]]
    return {'state': status, 'events': items, 'cursor': cursor + len(items), 'has_more': cursor + len(items) < len(lines)}


def collect():
    files, size = {}, 0
    for root, prefix in [(OUTPUT, 'output'), (STATE / 'ledger', 'ledger')]:
        for path in root.rglob('*'):
            if path.is_symlink():
                raise ValueError('Output symlinks are forbidden')
            if not path.is_file():
                continue
            data = path.read_bytes(); size += len(data)
            if len(data) > 8_000_000 or size > 40_000_000:
                raise ValueError('Output artifact budget exceeded')
            files[prefix + '/' + str(path.relative_to(root))] = base64.b64encode(data).decode()
    if (STATE / 'runtime.json').exists():
        files['runtime.json'] = base64.b64encode((STATE / 'runtime.json').read_bytes()).decode()
    return {'files': files}


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if command == 'run':
        raise SystemExit(main())
    elif command == 'poll':
        print(json.dumps(poll(int(sys.argv[2]))))
    elif command == 'collect':
        print(json.dumps(collect()))
    elif command == 'cancel':
        STATE.mkdir(exist_ok=True); (STATE / 'cancel').touch()
        path = STATE / 'status.json'
        status = json.loads(path.read_text()) if path.exists() else {}
        if status.get('status') == 'running' and type(status.get('agent_pid')) is int:
            user = pwd.getpwnam('reveal-agent')
            subprocess.run(['setpriv', '--reuid', str(user.pw_uid), '--regid', str(user.pw_gid), '--clear-groups',
                            '--no-new-privs', '/bin/kill', '-TERM', '--', '-' + str(status['agent_pid'])], capture_output=True, timeout=5)
