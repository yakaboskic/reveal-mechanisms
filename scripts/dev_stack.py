#!/usr/bin/env python3
"""Operate only this checkout's local frontend and Compose API/worker.

No imports, migrations, dependency installation, volume removal, or secret output.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
STATE = RUNTIME / 'dev-stack.json'
LOGS = RUNTIME / 'logs'
PROJECT = 'reveal-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]


class StartupError(RuntimeError):
    pass


def environment():
    """Read dotenv values as data, with process environment taking precedence."""
    result = {}
    path = ROOT / '.env'
    if path.exists():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            key, sep, raw = line.partition('=')
            key = key.strip()
            if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise StartupError(f'.env line {number}: expected NAME=value.')
            raw = raw.strip()
            if raw.startswith(('"', "'")):
                try:
                    tokens = shlex.split(raw, comments=True, posix=True)
                except ValueError:
                    raise StartupError(f'.env line {number}: unclosed quote.') from None
                if len(tokens) != 1:
                    raise StartupError(f'.env line {number}: use one quoted value.')
                value = tokens[0]
            else:
                value = re.split(r'\s+#', raw, maxsplit=1)[0].rstrip()
            result[key] = value
    result.update(os.environ)
    result['COMPOSE_PROJECT_NAME'] = PROJECT
    result.setdefault('REVEAL_FRONTEND_PORT', '3000')
    result.setdefault('REVEAL_API_PORT', '8000')
    result.setdefault('REVEAL_API_URL', 'http://127.0.0.1:' + result['REVEAL_API_PORT'])
    result.setdefault('NEXTAUTH_URL', 'http://localhost:' + result['REVEAL_FRONTEND_PORT'])
    result.setdefault('REVEAL_ENVIRONMENT', 'development')
    result.setdefault('REVEAL_EXECUTION_MODE', 'box')
    return result


def run(args, env, *, check=True, timeout=30):
    try:
        p = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StartupError(f'{Path(args[0]).name} failed ({type(exc).__name__}).') from None
    if check and p.returncode:
        # Docker build logs may contain env values; retain a restricted local log,
        # never print arbitrary command output into the terminal.
        LOGS.mkdir(parents=True, exist_ok=True)
        log = LOGS / 'startup-error.log'
        output = p.stdout + '\n' + p.stderr
        for key, value in env.items():
            if value and (re.search(r'(?:_KEY|_TOKEN|_PASSWORD|_SECRET)$', key) or key == 'DATABASE_URL'):
                output = output.replace(value, '[REDACTED]')
        log.write_text(output)
        log.chmod(0o600)
        raise StartupError(f'{Path(args[0]).name} exited {p.returncode}; inspect {log}.')
    return p


def compose(env, *args, check=True, timeout=30):
    return run(['docker', 'compose', '--project-name', PROJECT, '--project-directory', str(ROOT),
                '-f', str(ROOT / 'compose.yaml'), *args], env, check=check, timeout=timeout)


def load_state():
    if not STATE.exists():
        return {}
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        raise StartupError(f'Invalid process state in {STATE}; inspect before removing it.') from None
    if state.get('project') != PROJECT or state.get('root') != str(ROOT):
        raise StartupError('Process state belongs to another checkout; refusing to stop it.')
    return state


def save_state(state):
    state.update(project=PROJECT, root=str(ROOT))
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(STATE)


def start_signature(pid):
    p = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ''


def frontend_alive(state):
    pid = state.get('frontend_pid')
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        return os.getpgid(pid) == pid and start_signature(pid) == state.get('frontend_started')
    except ProcessLookupError:
        return False


def stop_frontend(state):
    if not frontend_alive(state):
        return
    pid = state['frontend_pid']
    os.killpg(pid, signal.SIGTERM)
    def group_alive():
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False
    deadline = time.monotonic() + 20
    while group_alive() and time.monotonic() < deadline:
        time.sleep(.2)
    if group_alive():
        os.killpg(pid, signal.SIGKILL)


def health(url):
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def port_available(port):
    with socket.socket() as sock:
        # Next.js reuses a recently closed listening address. Match that socket
        # behavior so TCP TIME_WAIT does not look like an unrelated live server.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def wait_ready(url, seconds, description, *, guard=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if health(url):
            return
        if guard and not guard():
            raise StartupError(f'{description} exited before readiness. Inspect {LOGS}.')
        time.sleep(1)
    raise StartupError(f'{description} was not ready at {url} after {seconds}s. '
                       f'Check logs, Aurora network access/TLS and one-time setup.')


def preflight(env):
    if not (ROOT / '.env').exists():
        raise StartupError('Copy .env.example to .env and configure it; see docs/local-development.md.')
    for name in ('docker', 'node'):
        if not shutil.which(name):
            raise StartupError(f'Install {name} before starting the stack.')
    if not (ROOT / 'services/frontend/node_modules/next/dist/bin/next').exists():
        raise StartupError('Frontend dependencies missing: run npm ci --prefix services/frontend once.')
    mode = env['REVEAL_EXECUTION_MODE']
    if mode not in ('box', 'deterministic'):
        raise StartupError('REVEAL_EXECUTION_MODE must be box or deterministic.')
    required = ['REVEAL_MYSQL_HOST', 'REVEAL_MYSQL_USER', 'REVEAL_MYSQL_DATABASE', 'REVEAL_MYSQL_CA_FILE',
                'REVEAL_MYSQL_PASSWORD', 'REVEAL_GATEWAY_SECRET', 'REVEAL_GATEWAY_SERVICE_TOKEN',
                'AUTH_SECRET', 'EMBEDDING_SERVICE_API_KEY']
    if mode == 'box':
        required += ['UPSTASH_BOX_API_KEY', 'ANTHROPIC_API_KEY']
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise StartupError('Set required values in ignored .env: ' + ', '.join(missing))
    for key in ('REVEAL_GATEWAY_SECRET', 'REVEAL_GATEWAY_SERVICE_TOKEN', 'AUTH_SECRET'):
        if len(env[key]) < 32:
            raise StartupError(f'{key} must contain at least 32 characters of random secret material.')
    ca = env.get('REVEAL_MYSQL_CA_FILE')
    if ca and not Path(ca).expanduser().is_file():
        raise StartupError('REVEAL_MYSQL_CA_FILE must point to a readable trusted RDS CA PEM file.')
    if not (RUNTIME / 'dapper/schema/dapper.yaml').exists():
        raise StartupError('Pinned DAPPER checkout missing. Run the one-time setup in docs/local-development.md.')
    source = Path(env.get('REVEAL_DISMECH_SOURCE', str(ROOT.parent / 'dismech'))).expanduser()
    if not source.is_absolute():
        source = ROOT / source
    if not source.is_dir():
        raise StartupError('REVEAL_DISMECH_SOURCE must point to the exact DisMech source checkout. See one-time setup.')
    for key in ('REVEAL_FRONTEND_PORT', 'REVEAL_API_PORT'):
        try:
            if not 1 <= int(env[key]) <= 65535:
                raise ValueError()
        except ValueError:
            raise StartupError(f'{key} must be a valid TCP port.') from None
    run(['docker', 'info', '--format', '{{.ServerVersion}}'], env)
    compose(env, 'version')


def up(env, *, build=False):
    preflight(env)
    state = load_state()
    mode = env['REVEAL_EXECUTION_MODE']
    existing = set(compose(env, 'ps', '--status', 'running', '--services').stdout.split())
    if (existing or frontend_alive(state)) and state.get('mode', mode) != mode:
        raise StartupError('Execution mode changed: run ./scripts/dev-down.sh before restarting.')
    front_port = int(env['REVEAL_FRONTEND_PORT'])
    if not frontend_alive(state) and not port_available(front_port):
        raise StartupError(f'Port {front_port} is already in use by an unmanaged process; choose REVEAL_FRONTEND_PORT.')
    if 'api' not in existing and not port_available(int(env['REVEAL_API_PORT'])):
        raise StartupError(f"API port {env['REVEAL_API_PORT']} is occupied by an unmanaged process.")
    new_services = sorted({'api', 'worker'} - existing)
    started_frontend = False
    old_state = dict(state)
    state['mode'] = mode
    try:
        print('Starting API and worker containers; existing healthy services are reused.', flush=True)
        compose(env, 'up', '-d', '--no-recreate', *(['--build'] if build else []), 'api', 'worker', timeout=900)
        wait_ready(env['REVEAL_API_URL'] + '/health/ready', 120, 'API and Aurora')
        if not frontend_alive(state):
            LOGS.mkdir(parents=True, exist_ok=True)
            log_path = LOGS / 'frontend.log'
            with log_path.open('ab') as log:
                log_path.chmod(0o600)
                child = subprocess.Popen([shutil.which('node'), str(ROOT / 'services/frontend/node_modules/next/dist/bin/next'),
                                          'dev', '--hostname', '127.0.0.1', '--port', str(front_port)],
                                         cwd=ROOT / 'services/frontend', env=env, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            state['frontend_pid'] = child.pid
            state['frontend_started'] = start_signature(child.pid)
            started_frontend = True
            save_state(state)
        wait_ready(f'http://127.0.0.1:{front_port}/api/health', 120, 'Next.js', guard=lambda: frontend_alive(state))
        running = set(compose(env, 'ps', '--status', 'running', '--services').stdout.split())
        if not {'api', 'worker'} <= running:
            raise StartupError('API/worker exited during startup; inspect Compose logs.')
        save_state(state)
        print(f"Frontend: http://localhost:{front_port}\nAPI: {env['REVEAL_API_URL']}\n"
              f"Services: api ready (Aurora connected), worker running, frontend ready\n"
              f"Execution: {mode}" + (' (SIMULATED DEVELOPMENT EXECUTION)' if mode == 'deterministic' else '') +
              f"\nFrontend log: {LOGS / 'frontend.log'}\n"
              f"Container logs: docker compose -p {PROJECT} -f {ROOT / 'compose.yaml'} logs -f api worker\n"
              'Stop: ./scripts/dev-down.sh')
    except BaseException:
        # Diagnostics and each cleanup action are independent: a failed Docker
        # log read or unwritable log directory must not mask the startup failure
        # or prevent us from stopping resources created by this invocation.
        try:
            diagnostic = compose(env, 'logs', '--no-color', '--tail', '100', 'api', 'worker', check=False)
            output = diagnostic.stdout + diagnostic.stderr
            for key, value in env.items():
                if value and (re.search(r'(?:_KEY|_TOKEN|_PASSWORD|_SECRET)$', key) or key == 'DATABASE_URL'):
                    output = output.replace(value, '[REDACTED]')
            LOGS.mkdir(parents=True, exist_ok=True)
            failure_log = LOGS / 'container-startup.log'
            failure_log.write_text(output)
            failure_log.chmod(0o600)
        except Exception:
            print('REVEAL: Startup diagnostics could not be saved; continuing cleanup.', file=sys.stderr)
        cleanup = []
        if started_frontend:
            cleanup.append(('frontend', lambda: stop_frontend(state)))
        if new_services:
            cleanup.append(('container stop', lambda: compose(env, 'stop', '--timeout', '120', *new_services, check=False, timeout=150)))
            cleanup.append(('container removal', lambda: compose(env, 'rm', '-f', *new_services, check=False)))
        cleanup.append(('process state', lambda: save_state(old_state) if old_state else STATE.unlink(missing_ok=True)))
        for description, action in cleanup:
            try:
                action()
            except Exception:
                print(f'REVEAL: Could not finish {description}; run ./scripts/dev-down.sh to retry cleanup.', file=sys.stderr)
        raise


def down(env):
    state = load_state()
    stop_frontend(state)
    if not shutil.which('docker'):
        raise StartupError('Frontend stopped; Docker CLI missing, so container shutdown cannot be verified.')
    compose(env, 'stop', '--timeout', '120', 'api', 'worker', timeout=150)
    compose(env, 'rm', '-f', 'api', 'worker')
    STATE.unlink(missing_ok=True)
    print('Stopped this project’s frontend, API and worker. Aurora data, artifacts and volumes are preserved.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['up', 'down'])
    p.add_argument('--build', action='store_true', help='Explicitly rebuild backend images on startup')
    args = p.parse_args()
    def interrupted(_signal, _frame):
        raise StartupError('Startup/shutdown interrupted by termination signal.')
    signal.signal(signal.SIGTERM, interrupted)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    LOGS.mkdir(mode=0o700, exist_ok=True)
    try:
        with (RUNTIME / 'dev-stack.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StartupError('Another startup/shutdown operation is in progress.') from None
            env = environment()
            up(env, build=args.build) if args.action == 'up' else down(env)
    except (StartupError, KeyboardInterrupt) as exc:
        print(f'REVEAL: {exc or "Interrupted; newly started resources cleaned up."}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
