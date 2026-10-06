#!/usr/bin/env python3
"""Downloaded as start.py. Python stdlib only; no model calls during setup.

The archive and its manifest come from the authenticated Reveal download. Hashes
detect damaged/modified files, not a malicious replacement of the whole archive.
Credentials use the OS store, never a configuration file or command argument.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


SERVICE = 'org.reveal.local-mcp'
SCOPE_FIELDS = ('local_work_id', 'research_request_id', 'reference_generation_id', 'package_sha256', 'mcp_url',
                'device_authorization_url', 'token_url', 'revocation_url', 'return_url')
CLIENT_ID = 'reveal-local-launcher'
PROMPT = ('Read RESEARCH.md and the pinned authoring instructions in this workspace, '
          'then investigate this frozen Reveal research question using its MCP connection. '
          'Keep your findings in output/ and preserve existing local work. '
          'Explore public data without signing in. Use connect_reveal for private research, validation or contribution. '
          'Do not read .reveal-setup/, disclose credentials, publish accounts, '
          'or start hosted agents.')
OFFLINE_INSTRUCTIONS = ('This workspace is offline. Use read_evidence to read exact downloaded evidence. '
                        'Remote Reveal tools and sign-in are unavailable. Missing artifacts remain unavailable; '
                        'restart without --offline when remote Reveal access is needed.')
OFFLINE_PROMPT = ('Read RESEARCH.md and the pinned authoring instructions in this workspace, '
                  'then investigate this frozen Reveal research question using the local read_evidence tool. '
                  + OFFLINE_INSTRUCTIONS + ' Keep your findings in output/ and preserve existing local work. '
                  'Do not read .reveal-setup/, disclose credentials, publish accounts, or start hosted agents.')
MAX_RESPONSE = 16_000_000


class SetupError(Exception):
    """A deliberately secret-free message safe to show in a terminal."""


class HTTPFailure(SetupError):
    def __init__(self, status, code=None):
        self.status = status
        self.code = code
        super().__init__('Reveal request failed (HTTP %s).' % status)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(c) < 32 for c in name)):
        raise SetupError('The setup contains an unsafe file path.')
    pieces = name.split('/')
    if any(piece in ('', '.', '..') for piece in pieces) or PurePosixPath(name).is_absolute():
        raise SetupError('The setup contains an unsafe file path.')
    path = root
    for piece in pieces:
        path = path / piece
        if path.is_symlink():
            raise SetupError('Setup files and state must not be symbolic links.')
    return path


def read_bytes(root, name, limit=32_000_000):
    path = safe_path(root, name)
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise SetupError('A setup file is not a regular file or exceeds its size limit.')
            value = handle.read(limit + 1)
            if len(value) > limit:
                raise SetupError('A setup file exceeds its size limit.')
            return value
    except OSError:
        raise SetupError('A required setup file is missing or unreadable.') from None


def read_json(root, name, limit=4_000_000):
    try:
        value = json.loads(read_bytes(root, name, limit))
    except (ValueError, UnicodeError):
        raise SetupError('A setup JSON file is invalid.') from None
    if not isinstance(value, dict):
        raise SetupError('A setup JSON file must contain an object.')
    return value


def checked_url(value):
    if (not isinstance(value, str) or not value or not value.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value):
        raise SetupError('The setup contains an invalid endpoint URL.')
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise SetupError('The setup contains an invalid endpoint URL.') from None
    if (not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment
            or not url.path.startswith('/') or url.scheme not in ('https', 'http')
            or (url.scheme == 'http' and url.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise SetupError('Use HTTPS endpoints, or literal loopback addresses for local development.')
    return (url.scheme, url.hostname.lower(), port or (443 if url.scheme == 'https' else 80))


def scope_metadata(value):
    if value.get('schema_version') != 1 or value.get('setup_version') != 'reveal.local-setup/2':
        raise SetupError('This setup format is not supported; download a new setup.')
    identity = value.get('local_work_id', '')
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity):
        raise SetupError('The setup research identity is invalid.')
    if not isinstance(value.get('package_sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', value['package_sha256']):
        raise SetupError('The setup package checksum is invalid.')
    origins = {key: checked_url(value.get(key)) for key in ('mcp_url', 'device_authorization_url', 'token_url', 'revocation_url', 'return_url')}
    if any(origins[key] != origins['mcp_url'] for key in ('device_authorization_url', 'token_url', 'revocation_url')):
        raise SetupError('OAuth and MCP must use the same Reveal origin.')
    for key in ('research_request_id', 'reference_generation_id'):
        if not isinstance(value.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value[key]):
            raise SetupError('The setup research version is invalid.')
    if value.get('client') not in ('codex', 'claude_code'):
        raise SetupError('The setup agent client is invalid.')
    return {key: value[key] for key in ('schema_version', 'setup_version', 'client') + SCOPE_FIELDS}


def verify_bundle(root):
    manifest = read_json(root, 'setup-manifest.json')
    scope = scope_metadata(manifest)
    files = manifest.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 10_000:
        raise SetupError('The setup file manifest is invalid.')
    seen = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise SetupError('The setup file manifest is invalid.')
        name = item.get('path')
        safe_path(root, name)
        if (name.casefold() in seen or name in ('setup.json', 'setup-manifest.json')
                or name.startswith('.reveal-setup/')):
            raise SetupError('The setup manifest contains duplicate or reserved paths.')
        seen.add(name.casefold())
        size = item.get('size_bytes')
        checksum = item.get('sha256')
        if type(size) is not int or not 0 <= size <= 32_000_000 or not isinstance(checksum, str) or not re.fullmatch(r'[a-f0-9]{64}', checksum):
            raise SetupError('A setup file checksum or size is invalid.')
        total += size
        if total > 128_000_000:
            raise SetupError('The setup exceeds its total size limit.')
        raw = read_bytes(root, name)
        if len(raw) != size or sha256(raw) != checksum:
            raise SetupError('Setup integrity verification failed. Restore the downloaded input/configuration files; keep output/ intact.')
    required = {'start.py', 'input/evidence-package.json', 'input/manifest.json', '.codex/config.toml',
                '.mcp.json', 'research.md', 'agents.md', 'claude.md', '.gitignore'}
    if not required <= seen:
        raise SetupError('The setup manifest is missing required launcher, input, or instruction files.')
    if sha256(read_bytes(root, 'input/evidence-package.json')) != scope['package_sha256']:
        raise SetupError('The research seed does not match this setup scope.')
    ignored = read_bytes(root, '.gitignore').decode('utf-8').splitlines()
    if not {'setup.json', '.reveal-setup/'} <= {line.strip().lstrip('/') for line in ignored}:
        raise SetupError('Setup credential/state files must be excluded from version control.')
    name = 'reveal_' + scope['local_work_id'].replace('-', '_')
    config = read_json(root, '.mcp.json')
    expected = {'type': 'stdio', 'command': 'python3', 'args': ['start.py', '--mcp']}
    if config.get('mcpServers', {}).get(name) != expected:
        raise SetupError('The project MCP configuration does not match the research scope.')
    return scope


def expiry(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise SetupError('The setup contains an invalid expiry time.') from None


class MacKeychain:
    def __init__(self):
        try:
            self.security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        except OSError:
            raise SetupError('The macOS Keychain is unavailable. No plaintext credential fallback is used.') from None
        lib = self.security
        lib.SecKeychainFindGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        lib.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        lib.SecKeychainAddGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        lib.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        lib.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.SecKeychainItemFreeContent.restype = ctypes.c_int32
        lib.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        lib.SecKeychainItemDelete.restype = ctypes.c_int32

    def get(self, key):
        service, account = SERVICE.encode(), key.encode()
        size, data = ctypes.c_uint32(), ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(None, len(service), service, len(account), account,
            ctypes.byref(size), ctypes.byref(data), None)
        if status == -25300:
            return None
        if status != 0:
            raise SetupError('macOS Keychain access failed or was denied. Unlock/allow access and rerun; no plaintext fallback is used.')
        try:
            if size.value > 16384:
                raise SetupError('The saved Reveal credential is invalid.')
            return ctypes.string_at(data, size.value).decode('ascii')
        finally:
            self.security.SecKeychainItemFreeContent(None, data)

    def put(self, key, token):
        service, account, secret = SERVICE.encode(), key.encode(), token.encode()
        status = self.security.SecKeychainAddGenericPassword(None, len(service), service, len(account), account,
            len(secret), ctypes.c_char_p(secret), None)
        if status != 0:
            raise SetupError('Could not save the credential in macOS Keychain. No exchange was attempted.')

    def delete(self, key):
        """Remove one exact service/account entry (also used by opt-in smoke tests)."""
        service, account = SERVICE.encode(), key.encode()
        item = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(None, len(service), service, len(account), account,
            None, None, ctypes.byref(item))
        if status == -25300:
            return
        if status != 0:
            raise SetupError('Could not locate the exact Reveal Keychain entry for removal.')
        core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRelease.restype = None
        try:
            if self.security.SecKeychainItemDelete(item) != 0:
                raise SetupError('Could not remove the exact Reveal Keychain entry.')
        finally:
            core.CFRelease(item)


class LinuxSecretStore:
    def __init__(self):
        self.executable = shutil.which('secret-tool')
        if not self.executable:
            raise SetupError('Linux needs an unlocked Secret Service credential store and secret-tool. Install/configure it, then rerun; no plaintext fallback is used.')

    def get(self, key):
        try:
            result = subprocess.run([self.executable, 'lookup', 'application', SERVICE, 'credential', key],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            raise SetupError('The Linux Secret Service credential store is unavailable.') from None
        if result.returncode == 1 and not result.stdout:
            return None
        if result.returncode != 0 or len(result.stdout) > 16384:
            raise SetupError('Could not read the Linux Secret Service credential store.')
        try:
            return result.stdout.decode('ascii').strip()
        except UnicodeError:
            raise SetupError('The saved Reveal credential is invalid.') from None

    def put(self, key, token):
        try:
            result = subprocess.run([self.executable, 'store', '--label=Reveal local research',
                'application', SERVICE, 'credential', key], input=token.encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            raise SetupError('Could not save the credential in Linux Secret Service. No exchange was attempted.') from None
        if result.returncode != 0:
            raise SetupError('Unlock the Linux Secret Service credential store and rerun. No exchange was attempted.')

    def delete(self, key):
        try:
            result = subprocess.run([self.executable, 'clear', 'application', SERVICE, 'credential', key],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            raise SetupError('Could not remove the Reveal credential from Linux Secret Service.') from None
        if result.returncode not in (0, 1):
            raise SetupError('Could not remove the Reveal credential from Linux Secret Service.')


def credential_store():
    if sys.platform == 'darwin':
        return MacKeychain()
    if sys.platform.startswith('linux'):
        return LinuxSecretStore()
    raise SetupError('Anonymous research works on this platform. Reveal sign-in through this helper needs macOS Keychain or Linux Secret Service; use a client with native remote MCP OAuth on other platforms.')


@contextmanager
def locked_state(root):
    directory = safe_path(root, '.reveal-setup')
    directory.mkdir(mode=0o700, exist_ok=True)
    if not directory.is_dir():
        raise SetupError('The local setup state path must be a directory.')
    lock = safe_path(root, '.reveal-setup/launch.lock')
    descriptor = os.open(str(lock), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SetupError('The setup lock must be a regular file.')
        if sys.platform == 'win32':
            raise SetupError('Public research needs no credentials. On Windows, use a native remote MCP OAuth client for sign-in; this helper supports macOS Keychain and Linux Secret Service.')
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('Another launcher is using this workspace. Wait for it to finish.') from None
        yield


def write_state(root, value):
    destination = safe_path(root, '.reveal-setup/state.json')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def key_prefix(scope):
    raw = json.dumps({key: scope[key] for key in SCOPE_FIELDS}, sort_keys=True).encode()
    return sha256(raw) + ':'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise SetupError('Reveal redirected a request; redirects are refused.')


def request_http(url, *, body=None, method='POST', token=None, protocol=None, form=False, raw=False):
    checked_url(url)
    headers = {'Accept': 'application/json, text/event-stream'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if protocol:
        headers['MCP-Protocol-Version'] = protocol
    if isinstance(body, bytes):
        data = body
        headers['Content-Type'] = 'application/octet-stream'
    elif form:
        data = urlencode(body or {}).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    else:
        data = None
    try:
        with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers, method=method), timeout=45) as response:
            if response.geturl() != url:
                raise SetupError('Reveal returned a different endpoint.')
            value = response.read(MAX_RESPONSE + 1)
            if len(value) > MAX_RESPONSE:
                raise SetupError('The Reveal response exceeds its size limit.')
            if raw:
                return value
            if not value and response.status in (200, 202, 204):
                return None
            if response.headers.get_content_type() != 'application/json':
                raise SetupError('Reveal returned an unsupported response format.')
            result = json.loads(value)
            if not isinstance(result, dict):
                raise ValueError()
            return result
    except HTTPError as error:
        # Only expose recognized OAuth error names, never request bodies/tokens.
        code = None
        try:
            failure = json.loads(error.read(8192))
            candidate = failure.get('error')
            if candidate in ('authorization_pending', 'slow_down', 'access_denied', 'expired_token',
                             'invalid_grant', 'invalid_client', 'invalid_scope'):
                code = candidate
        except (ValueError, AttributeError, OSError):
            pass
        status = error.code
        error.close()
        raise HTTPFailure(status, code) from None
    except (URLError, TimeoutError, OSError):
        raise SetupError('Could not contact Reveal. Local files remain available; retry when connected.') from None
    except (ValueError, UnicodeError):
        raise SetupError('Reveal returned invalid JSON.') from None


def post_json(url, body, token=None, protocol=None):
    return request_http(url, body=body, token=token, protocol=protocol)


def initialize_mcp(scope, post=post_json):
    response = post(scope['mcp_url'], {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': CLIENT_ID, 'version': '2'}}})
    result = response.get('result') if isinstance(response, dict) else None
    protocol = result.get('protocolVersion') if isinstance(result, dict) else None
    if not isinstance(response, dict) or response.get('id') != 1 or protocol not in ('2025-03-26', '2025-06-18', '2025-11-25'):
        raise SetupError('Reveal MCP initialization failed or requires a newer launcher.')
    post(scope['mcp_url'], {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, protocol=protocol)
    return protocol


def verify_mcp(scope, post=post_json):
    protocol = initialize_mcp(scope, post)
    response = post(scope['mcp_url'], {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, protocol=protocol)
    tools = response.get('result', {}).get('tools') if isinstance(response, dict) else None
    if not isinstance(tools, list) or not any(tool.get('name') == 'find_claims' for tool in tools):
        raise SetupError('Reveal did not expose its public research tools.')


class Connection:
    """The only holder of OAuth secrets; state.json stores scoped OS-store keys."""
    def __init__(self, root, scope, *, store=None, http=request_http, clock=time.time):
        self.root, self.scope, self._store, self.http, self.clock = root, scope, store, http, clock

    @property
    def store(self):
        if self._store is None:
            self._store = credential_store()
        return self._store

    def state(self):
        path = safe_path(self.root, '.reveal-setup/state.json')
        value = read_json(self.root, '.reveal-setup/state.json') if path.exists() else {'scope': self.scope}
        if value.get('scope') != self.scope:
            raise SetupError('Saved connection metadata belongs to another workspace. Use a fresh folder.')
        return value

    def secret(self, state, name):
        key = state.get(name)
        if key is None:
            return None
        if not isinstance(key, str) or not re.fullmatch(re.escape(key_prefix(self.scope)) + r'[a-f0-9]{32}', key):
            raise SetupError('The saved connection has an invalid credential scope.')
        raw = self.store.get(key)
        try:
            value = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            raise SetupError('The saved Reveal credential is invalid. Sign in again.') from None
        if value is not None and not isinstance(value, dict):
            raise SetupError('The saved Reveal credential is invalid.')
        return value

    def save_secret(self, state, name, value):
        key = key_prefix(self.scope) + secrets.token_hex(16)
        raw = json.dumps(value, separators=(',', ':'))
        self.store.put(key, raw)
        if self.store.get(key) != raw:
            raise SetupError('Could not verify the securely stored credential.')
        old = state.get(name)
        state[name] = key
        write_state(self.root, state)
        if old:
            self.store.delete(old)

    def discard(self, state, name):
        key = state.pop(name, None)
        write_state(self.root, state)
        if key:
            self.store.delete(key)

    def accept_tokens(self, state, value):
        if (not isinstance(value, dict) or value.get('token_type', '').lower() != 'bearer'
                or value.get('local_work_id') != self.scope['local_work_id']
                or value.get('package_sha256') != self.scope['package_sha256']
                or value.get('mcp_url') != self.scope['mcp_url']
                or not re.fullmatch(r'rvlm_[A-Za-z0-9_-]{43,128}', value.get('access_token', ''))
                or not isinstance(value.get('refresh_token'), str) or not 32 <= len(value['refresh_token']) <= 2048
                or not isinstance(value.get('scope'), str)
                or not {'research:read', 'research:write'} <= set(value['scope'].split())
                or type(value.get('expires_in')) is not int or not 1 <= value['expires_in'] <= 86400):
            raise SetupError('Reveal returned invalid or mismatched connection metadata. Sign in again.')
        state.pop('disabled', None)
        self.save_secret(state, 'tokens', {**value, 'expires_at': self.clock() + value['expires_in']})

    def begin(self):
        with locked_state(self.root):
            state = self.state()
            pending = self.secret(state, 'device')
            if pending and state.get('authorized_device') == state.get('device'):
                self.discard(state, 'device')
                pending = None
            if pending and pending.get('expires_at', 0) > self.clock():
                return self.pending_view(pending)
            if state.get('tokens') and state.get('disabled'):
                previous = self.secret(state, 'tokens')
                if previous:
                    # Keep the only revocation credential until the server has
                    # acknowledged revoking this installation's prior family.
                    self.http(self.scope['revocation_url'], form=True, body={
                        'token': previous['refresh_token'], 'token_type_hint': 'refresh_token', 'client_id': CLIENT_ID})
                self.discard(state, 'tokens')
            if self._token_locked(state):
                return {'state': 'connected', 'local_work_id': self.scope['local_work_id'],
                        'instructions': 'This workspace is already connected. Continue your research, or disconnect_reveal to sign in with another account.'}
            # Initialize the OS store before asking the user to approve.
            _ = self.store
            value = self.http(self.scope['device_authorization_url'], form=True, body={
                'client_id': CLIENT_ID, 'scope': 'research:read research:write',
                'resource': self.scope['mcp_url'], 'local_work_id': self.scope['local_work_id']})
            uri = value.get('verification_uri') if isinstance(value, dict) else None
            if (checked_url(uri) != checked_url(self.scope['return_url'])
                    or not isinstance(value.get('device_code'), str) or not 32 <= len(value['device_code']) <= 2048
                    or not isinstance(value.get('user_code'), str) or not re.fullmatch(r'[A-Za-z0-9-]{4,32}', value['user_code'])
                    or type(value.get('expires_in')) is not int or not 1 <= value['expires_in'] <= 1800):
                raise SetupError('Reveal returned invalid sign-in instructions.')
            interval = value.get('interval', 5)
            if type(interval) is not int or not 1 <= interval <= 60:
                raise SetupError('Reveal returned an invalid sign-in polling interval.')
            pending = {**value, 'expires_at': self.clock() + value['expires_in'],
                       'interval': interval, 'next_poll': self.clock() + interval}
            self.save_secret(state, 'device', pending)
            return self.pending_view(pending)

    def pending_view(self, pending):
        # Build the complete URL ourselves; never open a server-selected third-party URL.
        return {'state': 'authorization_pending', 'verification_uri': pending['verification_uri'],
                'verification_uri_complete': pending['verification_uri'] + '?' + urlencode({'user_code': pending['user_code']}),
                'user_code': pending['user_code'], 'poll_after_seconds': max(1, int(pending['next_poll'] - self.clock() + 1)),
                'instructions': 'Open this link, sign in to Reveal and approve this research connection. Then check get_reveal_connection.'}

    def status(self):
        with locked_state(self.root):
            state = self.state()
            pending = self.secret(state, 'device')
            if pending and state.get('authorized_device') == state.get('device'):
                self.discard(state, 'device')
                pending = None
            if pending:
                if pending.get('expires_at', 0) <= self.clock():
                    self.discard(state, 'device')
                    return {'state': 'expired', 'instructions': 'Call connect_reveal for a new sign-in link.'}
                if pending['next_poll'] > self.clock():
                    return self.pending_view(pending)
                # Record the interval before polling, including network failures.
                pending['next_poll'] = self.clock() + pending['interval']
                self.save_secret(state, 'device', pending)
                try:
                    value = self.http(self.scope['token_url'], form=True, body={
                        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': pending['device_code'],
                        'client_id': CLIENT_ID, 'resource': self.scope['mcp_url']})
                except HTTPFailure as error:
                    if error.code in ('authorization_pending', 'slow_down'):
                        if error.code == 'slow_down':
                            pending['interval'] += 5
                            pending['next_poll'] = self.clock() + pending['interval']
                            self.save_secret(state, 'device', pending)
                        return self.pending_view(pending)
                    if error.code in ('access_denied', 'expired_token', 'invalid_grant'):
                        self.discard(state, 'device')
                        return {'state': error.code, 'instructions': 'Sign-in was not completed. Public research remains available.'}
                    raise
                state['authorized_device'] = state['device']
                self.accept_tokens(state, value)
                self.discard(state, 'device')
            active = self._token_locked(state)
            return {'state': 'connected' if active else 'anonymous', 'local_work_id': self.scope['local_work_id'],
                    'instructions': 'Public research is available without sign-in.'}

    def token(self):
        with locked_state(self.root):
            return self._token_locked(self.state())

    def _token_locked(self, state):
        if state.get('disabled'):
            return None
        active = self.secret(state, 'tokens')
        if not active:
            return None
        if active.get('expires_at', 0) > self.clock() + 30:
            return active['access_token']
        try:
            value = self.http(self.scope['token_url'], form=True, body={
                'grant_type': 'refresh_token', 'refresh_token': active['refresh_token'],
                'client_id': CLIENT_ID, 'resource': self.scope['mcp_url']})
        except HTTPFailure as error:
            if error.code == 'invalid_grant':
                self.discard(state, 'tokens')
                return None
            raise
        self.accept_tokens(state, value)
        return value['access_token']

    def mark_disconnected(self):
        with locked_state(self.root):
            state = self.state()
            state['disabled'] = True
            write_state(self.root, state)

    def disconnect(self):
        with locked_state(self.root):
            state = self.state()
            active = self.secret(state, 'tokens')
            state['disabled'] = True
            write_state(self.root, state)
            if state.get('device'):
                self.discard(state, 'device')
            if active:
                try:
                    self.http(self.scope['revocation_url'], form=True, body={
                        'token': active['refresh_token'], 'token_type_hint': 'refresh_token', 'client_id': CLIENT_ID})
                except SetupError:
                    return {'state': 'disconnected', 'revoked': False,
                            'instructions': 'Local authenticated access is disabled. Retry disconnect when online to revoke the server connection.'}
                self.discard(state, 'tokens')
            return {'state': 'disconnected', 'revoked': True, 'instructions': 'Local files and anonymous public tools remain available.'}


def tool_result(value, error=False):
    return {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': error}


def evidence_reader_module(root):
    # The setup manifest verifies this stdlib-only module before the MCP starts.
    import importlib.util
    path = safe_path(root, 'services/backend/src/reveal_backend/evidence_reader.py')
    if not path.is_file():
        raise SetupError('This historical kit has no bounded reader. Download a newly generated workspace.')
    spec = importlib.util.spec_from_file_location('reveal_workspace_reader', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reader_definition(root):
    return evidence_reader_module(root).TOOL_DEFINITION


def read_local_evidence(root, arguments, *, expected_seed_sha256=None):
    try:
        return evidence_reader_module(root).WorkspaceReader(root, expected_seed_sha256=expected_seed_sha256).read(**arguments)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise SetupError(str(error)) from None


def local_tools():
    def define(name, description, props=None, required=None, read=False):
        return {'name': name, 'description': description,
                'inputSchema': {'type': 'object', 'properties': props or {}, 'required': required or [], 'additionalProperties': False},
                'annotations': {'readOnlyHint': read, 'destructiveHint': False, 'openWorldHint': False}}
    return [define('connect_reveal', 'Start Reveal browser sign-in when the user wants private research, validation or submission. Return the link and code to the user; no credentials are exposed.'),
            define('get_reveal_connection', 'Check sign-in after browser approval. Respect poll_after_seconds. Public research needs no sign-in.'),
            define('disconnect_reveal', 'Revoke this workspace connection. Preserve local files and anonymous public tools.'),
            define('upload_research_files', 'Upload account/evidence files from output/ using the authenticated connection. Returns immutable artifact IDs without exposing credentials.',
                   {'purpose': {'enum': ['account', 'evidence']}, 'paths': {'type': 'array', 'minItems': 1, 'maxItems': 3,
                    'items': {'type': 'string'}}, 'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 180}},
                   ['purpose', 'paths', 'idempotency_key']),
            define('materialize_evidence_context', 'Export selected receipts/imports/reuse and atomically download the verified full closure into evidence/closures/. Poll again with the same selection while pending; no scientific query is made.',
                   {**{key: {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 100, 'uniqueItems': True}
                       for key in ('receipt_ids', 'import_ids', 'reuse_receipt_ids')}, 'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 180}},
                   ['idempotency_key']),
            define('download_public_capture', 'Download exact anonymous capture bytes before expiry into evidence/public/ for offline inspection. Does not attach evidence or contribute science.',
                   {'capture_id': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}}, ['capture_id']),
            define('download_research_artifact', 'Download one authorized private research artifact into output/downloads/. Verify its size and checksum.',
                   {'artifact_id': {'type': 'string', 'minLength': 1}}, ['artifact_id'])]


class Bridge:
    """Bounded stdio adapter: public reads direct, device OAuth only on request."""
    def __init__(self, root, scope, *, connection=None, post=post_json, http=request_http):
        self.root, self.scope, self.post, self.http = root, scope, post, http
        self.connection = connection or Connection(root, scope, http=http)
        self.protocol = None
        self.catalog = {}

    def remote(self, method, params=None, token=None):
        if self.protocol is None:
            self.protocol = initialize_mcp(self.scope, self.post)
        response = self.post(self.scope['mcp_url'], {'jsonrpc': '2.0', 'id': 3, 'method': method, **({'params': params} if params is not None else {})}, token, self.protocol)
        if not isinstance(response, dict) or response.get('id') != 3 or 'result' not in response:
            raise SetupError('Reveal returned an invalid MCP response.')
        return response['result']

    def tools(self):
        result = self.remote('tools/list')
        if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
            raise SetupError('Reveal returned an invalid tool catalog.')
        self.catalog = {tool['name']: tool for tool in result['tools']}
        # Remote OAuth metadata applies to HTTP clients; this stdio helper owns
        # authentication and returns explicit connect_reveal instructions instead.
        remote = [{key: value for key, value in tool.items() if key not in ('securitySchemes', '_meta')}
                  for tool in self.catalog.values() if tool['name'] != 'read_evidence']
        return {'tools': remote + local_tools() + [reader_definition(self.root)]}

    def call_remote(self, name, arguments, *, token=None):
        value = self.remote('tools/call', {'name': name, 'arguments': arguments}, token)
        if not isinstance(value, dict):
            raise SetupError('Reveal returned an invalid tool result.')
        return value

    def require_token(self):
        token = self.connection.token()
        if not token:
            raise SetupError('Sign in to Reveal for this action: call connect_reveal, show the link and code, then check get_reveal_connection after browser approval.')
        return token

    def unpack(self, name, arguments, token):
        result = self.call_remote(name, arguments, token=token)
        if result.get('isError') or not isinstance(result.get('structuredContent'), dict):
            raise SetupError('Reveal could not complete '+name+'. Inspect the research state before retrying.')
        return result['structuredContent']

    def upload(self, args):
        token = self.require_token()
        paths = args.get('paths'); purpose = args.get('purpose'); key = args.get('idempotency_key')
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 3 or purpose not in ('account', 'evidence')
                or not isinstance(key, str) or not 1 <= len(key) <= 180):
            raise SetupError('Choose one to three output/ files, a purpose, and a retry key.')
        raw_files = []
        for path in paths:
            if not isinstance(path, str) or not path.startswith('output/'):
                raise SetupError('Only files inside output/ can be uploaded.')
            raw = read_bytes(self.root, path, 8_000_000 if purpose == 'evidence' else 4_000_000)
            raw_files.append((path, raw))
        prepared = self.unpack('prepare_artifact_upload', {'local_work_id': self.scope['local_work_id'],
            'purpose': purpose, 'files': [{'filename': Path(path).name, 'sha256': sha256(raw), 'size_bytes': len(raw)} for path, raw in raw_files],
            'idempotency_key': key+':prepare'}, token)
        uploads = prepared.get('uploads')
        if not isinstance(uploads, list) or len(uploads) != len(raw_files):
            raise SetupError('Reveal returned an invalid upload plan.')
        base = self.scope['mcp_url'].rsplit('/mcp', 1)[0]
        for upload, (_, raw) in zip(uploads, raw_files):
            identity = upload.get('upload_id', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity) or upload.get('url') != base+'/v1/research-uploads/'+identity+'/content':
                raise SetupError('Reveal returned an invalid upload destination.')
            self.http(upload['url'], method='PUT', body=raw, token=token)
        return self.unpack('complete_artifact_upload', {'local_work_id': self.scope['local_work_id'],
            'upload_ids': [upload['upload_id'] for upload in uploads], 'idempotency_key': key+':complete'}, token)

    def download(self, args):
        token = self.require_token(); identity = args.get('artifact_id', '')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity):
            raise SetupError('Choose a valid artifact ID.')
        record = self.unpack('get_artifact_download', {'local_work_id': self.scope['local_work_id'], 'artifact_id': identity}, token)
        base = self.scope['mcp_url'].rsplit('/mcp', 1)[0]
        if record.get('url') != base+'/v1/research-artifacts/'+identity+'/content':
            raise SetupError('Reveal returned an invalid download destination.')
        raw = self.http(record['url'], method='GET', token=token, raw=True)
        if len(raw) != record.get('size_bytes') or sha256(raw) != record.get('sha256'):
            raise SetupError('The downloaded artifact failed checksum verification.')
        filename = record.get('filename', '')
        if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', Path(filename).name):
            raise SetupError('The artifact filename is invalid.')
        relative = 'output/downloads/'+identity+'/'+Path(filename).name
        path = safe_path(self.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if read_bytes(self.root, relative) != raw:
                raise SetupError('A different local file already occupies this download path.')
        else:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(raw)
        return {'path': relative, 'sha256': record['sha256'], 'size_bytes': len(raw)}

    def _save_evidence(self, relative, raw):
        path = safe_path(self.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if read_bytes(self.root, relative) != raw:
                raise SetupError('Existing evidence bytes changed; restore them before retrying: '+relative)
            return
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def _evidence_bytes(self, descriptor, relative, token=None, *, public_url=None, cache=None):
        checksum = descriptor.get('sha256'); size = descriptor.get('size_bytes')
        if (not isinstance(checksum, str) or not re.fullmatch(r'[a-f0-9]{64}', checksum)
                or type(size) is not int or not 0 <= size <= 32_000_000):
            raise SetupError('Invalid evidence checksum or size.')
        path = safe_path(self.root, relative)
        if path.exists():
            raw = read_bytes(self.root, relative)
        elif cache is not None and checksum in cache:
            raw = cache[checksum]
        else:
            # Reuse only explicitly inventoried and hash-verified seed bytes.
            seed_path = 'input/'+descriptor.get('path', descriptor.get('filename', ''))
            seed_manifest = read_json(self.root, 'input/manifest.json')
            if seed_manifest.get('files', {}).get(seed_path[6:]) == checksum and safe_path(self.root, seed_path).exists():
                raw = read_bytes(self.root, seed_path)
            else:
                base = self.scope['mcp_url'].rsplit('/mcp', 1)[0]
                identity = descriptor.get('artifact_id', descriptor.get('id', ''))
                if public_url is None and (not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity)):
                    raise SetupError('Invalid evidence artifact identity.')
                url = public_url or base+'/v1/research-artifacts/'+identity+'/content'
                raw = self.http(url, method='GET', token=token, raw=True)
        if len(raw) != size or sha256(raw) != checksum:
            raise SetupError('Evidence checksum verification failed: '+relative)
        if cache is not None: cache[checksum] = raw
        self._save_evidence(relative, raw)
        return raw

    def _account_evidence_status(self, package):
        """Report actual draft citations, never equate selected closure with all citations."""
        available = {source['dapper_file_id'] for source in package.get('source_artifacts', {}).values()}
        output = safe_path(self.root, 'output'); reports = []
        for path in sorted(output.glob('account*.json'))[:100] if output.is_dir() else []:
            relative = str(path.relative_to(self.root)); raw = read_bytes(self.root, relative, 4_000_000)
            try: draft = json.loads(raw)
            except (ValueError, UnicodeError):
                reports.append({'path': relative, 'sha256': sha256(raw), 'status': 'unreadable_draft', 'cited_evidence_present_locally': False})
                continue
            if not isinstance(draft, dict): continue
            nodes = {}; file_ids = set()
            for document in (package.get('dapper_context', {}), draft):
                for group, values in document.items():
                    if not isinstance(values, list): continue
                    for node in values:
                        if isinstance(node, dict) and isinstance(node.get('id'), str):
                            nodes[node['id']] = node
                            if group == 'files': file_ids.add(node['id'])
            def references(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key != 'id': yield from references(child)
                elif isinstance(value, list):
                    for child in value: yield from references(child)
                elif isinstance(value, str): yield value
            queue = [item['id'] for item in draft.get('scientific_accounts', []) if isinstance(item, dict) and 'id' in item]
            seen = set(); cited = set(); unresolved = set()
            while queue:
                identity = queue.pop()
                if identity in seen: continue
                seen.add(identity)
                if identity in file_ids: cited.add(identity)
                for ref in references(nodes.get(identity, {})):
                    if ref in nodes: queue.append(ref)
                    elif ref.startswith(('dapper:File.', 'dapper:Claim.', 'dapper:EvidenceItem.')):
                        unresolved.add(ref)
            missing = cited - available
            complete = bool(cited) and not missing and not unresolved
            reports.append({'path': relative, 'sha256': sha256(raw),
                'status': 'complete' if complete else 'incomplete' if cited or unresolved else 'no_cited_files',
                'cited_evidence_present_locally': complete, 'cited_file_ids': sorted(cited),
                'missing_file_ids': sorted(missing), 'unresolved_dependency_ids': sorted(unresolved)})
        return reports

    def materialize(self, args):
        token = self.require_token()
        if not isinstance(args.get('idempotency_key'), str) or not 1 <= len(args['idempotency_key']) <= 180:
            raise SetupError('Provide a stable export idempotency_key.')
        value = self.unpack('export_evidence_context', {'local_work_id': self.scope['local_work_id'], **args}, token)
        if value.get('operation_id'):
            operation_id = value['operation_id']
            value = self.unpack('get_operation', {'local_work_id': self.scope['local_work_id'], 'operation_id': operation_id}, token)
            if value.get('state') in ('received', 'running', 'queued'):
                return {'operation_id': operation_id, 'state': value['state'], 'instructions': 'Retry materialize_evidence_context with the same selection and idempotency_key.'}
            if value.get('state') != 'succeeded':
                raise SetupError('Evidence export failed. Inspect get_operation for '+operation_id+'.')
            value = value.get('result', {})
        if value.get('seed_sha256') != self.scope['package_sha256']:
            raise SetupError('Export does not belong to this frozen seed.')
        checksum = value.get('package_sha256', '')
        if not isinstance(checksum, str) or not re.fullmatch(r'[a-f0-9]{64}', checksum):
            raise SetupError('Export has no valid package checksum.')
        directory = 'evidence/closures/'+checksum+'/'
        cache = {}
        if value.get('format') == 'reveal.validation-context-export/2':
            descriptor = value.get('package_artifact', {})
            if descriptor.get('sha256') != checksum: raise SetupError('Package descriptor differs.')
            raw = self._evidence_bytes(descriptor, directory+'evidence-package.json', token, cache=cache)
            package = json.loads(raw)
        else:
            package = value.get('package')
            raw = (json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()
            if sha256(raw) != checksum: raise SetupError('Exported package checksum differs.')
            self._save_evidence(directory+'evidence-package.json', raw)
        if package.get('research_request_id') != self.scope['research_request_id']:
            raise SetupError('Export changed the frozen research request.')
        seed = read_json(self.root, 'input/evidence-package.json')
        if package.get('selection') != seed.get('selection'): raise SetupError('Export changed the frozen question.')
        if value.get('context_sha256'):
            context_raw = (json.dumps(package.get('validation_context'), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()
            if sha256(context_raw) != value['context_sha256']: raise SetupError('Full context checksum differs.')
        by_path = {item.get('path', item.get('filename')): item for item in value.get('artifacts', [])}
        total = len(raw)
        for source in package.get('source_artifacts', {}).values():
            relative = source['path']; descriptor = by_path.get(relative)
            if not isinstance(descriptor, dict) or any(descriptor.get(key) != source.get(key) for key in ('sha256', 'size_bytes')):
                raise SetupError('Closure source descriptor differs from package.')
            total += descriptor['size_bytes']
            if total > 128_000_000: raise SetupError('Evidence closure exceeds local size limit.')
            self._evidence_bytes(descriptor, directory+relative, token, cache=cache)
        manifest = value.get('manifest', {})
        if manifest.get('package_sha256', checksum) != checksum: raise SetupError('Closure manifest differs.')
        manifest = {**manifest, 'package_sha256': checksum, 'seed_sha256': value['seed_sha256']}
        self._save_evidence(directory+'manifest.json', (json.dumps(manifest, sort_keys=True, separators=(',', ':'))+'\n').encode())
        return {'state': 'succeeded', 'path': directory, 'package_sha256': checksum,
            'selected_evidence_present_locally': True, 'source_count': len(package.get('source_artifacts', {})),
            'accounts': self._account_evidence_status(package),
            'scope': 'Complete selected closure verified; include every cited receipt/import/reuse in the selection.'}

    def download_public_capture(self, args):
        identity = args.get('capture_id', '')
        if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity): raise SetupError('Choose an exact capture ID.')
        value = self.unpack('get_public_capture', {'capture_id': identity}, None)
        if value.get('capture_id') != identity: raise SetupError('Capture identity differs.')
        if value.get('reference_generation_id') not in (None, self.scope['reference_generation_id']):
            raise SetupError('Capture generation differs from this workspace.')
        directory = 'evidence/public/'+identity+'/'
        cache = {}; total = 0
        for artifact in value.get('artifacts', []):
            url = self.scope['mcp_url'].rsplit('/mcp', 1)[0]+'/v1/public-research/captures/'+identity+'/artifacts/'+artifact['sha256']
            if artifact.get('download_url') != url: raise SetupError('Capture download destination differs.')
            total += artifact['size_bytes']
            if total > 32_000_000: raise SetupError('Capture exceeds local size limit.')
            self._evidence_bytes(artifact, directory+artifact['path'], public_url=url, cache=cache)
        self._save_evidence(directory+'manifest.json', (json.dumps(value, sort_keys=True, separators=(',', ':'))+'\n').encode())
        return {'state': 'succeeded', 'capture_id': identity, 'path': directory, 'source_count': len(value.get('artifacts', []))}

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise SetupError('Tool arguments must be an object.')
        if name == 'read_evidence':
            return tool_result(read_local_evidence(self.root, arguments, expected_seed_sha256=self.scope['package_sha256']))
        if name in ('connect_reveal', 'get_reveal_connection', 'disconnect_reveal'):
            if arguments:
                raise SetupError('This connection tool takes no arguments.')
            action = {'connect_reveal': self.connection.begin, 'get_reveal_connection': self.connection.status,
                      'disconnect_reveal': self.connection.disconnect}[name]
            return tool_result(action())
        if name == 'materialize_evidence_context':
            return tool_result(self.materialize(arguments))
        if name == 'download_public_capture':
            return tool_result(self.download_public_capture(arguments))
        if name == 'upload_research_files':
            return tool_result(self.upload(arguments))
        if name == 'download_research_artifact':
            return tool_result(self.download(arguments))
        if not self.catalog:
            self.tools()
        definition = self.catalog.get(name)
        if definition is None:
            raise SetupError('Unknown Reveal tool.')
        schemes = definition.get('securitySchemes', definition.get('_meta', {}).get('securitySchemes', []))
        public = any(item.get('type') == 'noauth' for item in schemes)
        args = dict(arguments)
        # Public queries remain tied to the downloaded snapshot and do not
        # become owner-bound operations simply because the user signs in.
        schema = definition.get('inputSchema', {})
        variants = schema.get('anyOf', [schema])
        supports_generation = any('reference_generation_id' in item.get('properties', {}) for item in variants)
        if public and supports_generation and 'research_request_id' not in args:
            args.setdefault('reference_generation_id', self.scope['reference_generation_id'])
        token = None
        if not public or 'research_request_id' in args:
            token = self.require_token()
        return self.call_remote(name, args, token=token)


def serve(root, scope, *, source=None, destination=None, bridge=None, offline=False):
    source, destination = source or sys.stdin, destination or sys.stdout
    bridge = bridge or Bridge(root, scope)
    while True:
        line = source.readline(2_000_001)
        if not line:
            return 0
        identity = None
        try:
            if len(line.encode()) > 2_000_000:
                raise SetupError('MCP request exceeds its size limit.')
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise SetupError('Invalid MCP request.')
            identity = message.get('id')
            if 'id' not in message:
                continue
            method = message.get('method'); params = message.get('params') or {}
            if method == 'initialize':
                result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'reveal-local', 'version': '2'},
                          'instructions': OFFLINE_INSTRUCTIONS if offline else
                              'Read downloaded evidence with read_evidence and explore public Reveal data anonymously. '
                              'Use connect_reveal for private research, validation or contribution.'}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [reader_definition(root)]} if offline else bridge.tools()
            elif method == 'tools/call':
                try:
                    if offline and params.get('name') != 'read_evidence':
                        raise SetupError('This workspace is offline. Use read_evidence for downloaded evidence; restart without --offline to use remote Reveal tools.')
                    result = bridge.call(params.get('name'), params.get('arguments') or {})
                except HTTPFailure as error:
                    if error.status == 401:
                        bridge.connection.mark_disconnected()
                    detail = ('Sign in with connect_reveal, then retry this action.' if error.status in (401, 403)
                              else str(error))
                    result = tool_result({'code': 'REVEAL_REQUEST_FAILED', 'detail': detail}, True)
                except SetupError as error:
                    result = tool_result({'code': 'REVEAL_CONNECTION', 'detail': str(error)}, True)
            else:
                raise SetupError('Unsupported MCP method.')
            response = {'jsonrpc': '2.0', 'id': identity, 'result': result}
        except (ValueError, TypeError, AttributeError, SetupError):
            response = {'jsonrpc': '2.0', 'id': identity, 'error': {'code': -32600, 'message': 'Reveal could not process this request. Check the connection and request format.'}}
        except Exception:
            response = {'jsonrpc': '2.0', 'id': identity, 'error': {'code': -32603, 'message': 'Reveal connection failed. Existing local work is retained.'}}
        destination.write(json.dumps(response)+'\n')
        destination.flush()


def agent_command(executable, client, root, scope, *, offline=False):
    name = 'reveal_' + scope['local_work_id'].replace('-', '_')
    prompt = OFFLINE_PROMPT if offline else PROMPT
    if client == 'codex':
        command = [executable, '-C', str(root), '-c', 'mcp_servers.'+name+'.command='+json.dumps(sys.executable),
                   '-c', 'mcp_servers.'+name+'.args='+json.dumps([str(root/'start.py'), '--mcp']),
                   '-c', 'mcp_servers.'+name+'.cwd='+json.dumps(str(root))]
        command += ['-c', 'mcp_servers.'+name+'.env='+json.dumps({'REVEAL_LOCAL_OFFLINE': '1' if offline else '0'})]
        return command + [prompt]
    # Absolute paths make the connection independent of client working-directory
    # changes. Generated runtime configuration contains no credential or secret.
    config = safe_path(root, '.reveal-setup/agent-mcp.json')
    config.parent.mkdir(mode=0o700, exist_ok=True)
    servers = {name: {'type': 'stdio', 'command': sys.executable, 'args': [str(root/'start.py'), '--mcp'],
                     'env': {'REVEAL_LOCAL_OFFLINE': '1' if offline else '0'}}}
    if config.is_symlink():
        raise SetupError('Runtime configuration must not be a symbolic link.')
    descriptor = os.open(str(config), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump({'mcpServers': servers}, handle)
    command = [executable, '--mcp-config', str(config)]
    return command + ['--', prompt]


def launch(root, client=None, check_only=False, *, offline=False, login=False, logout=False,
           store=None, post=post_json, http=request_http, runner=subprocess.run, which=shutil.which):
    if offline and (login or logout):
        raise SetupError('Offline mode disables Reveal sign-in and logout. Remove --offline and unset REVEAL_LOCAL_OFFLINE to manage this connection.')
    root = Path(root).resolve(); scope = verify_bundle(root)
    if login or logout:
        connection = Connection(root, scope, store=store, http=http)
        if logout:
            print(json.dumps(connection.disconnect(), indent=2))
            return 0
        pending = connection.begin()
        if pending['state'] == 'connected':
            print('This workspace is already connected to Reveal.')
            return 0
        print('Sign in to Reveal: '+pending['verification_uri_complete'])
        print('Confirm code: '+pending['user_code'])
        webbrowser.open(pending['verification_uri_complete'])
        while pending['state'] == 'authorization_pending':
            time.sleep(min(10, pending['poll_after_seconds']))
            pending = connection.status()
        print('Reveal connection: '+pending['state'])
        return 0 if pending['state'] == 'connected' else 1
    if check_only:
        if not offline:
            verify_mcp(scope, post)
        print('Verified workspace'+(' and anonymous Reveal MCP' if not offline else '')+'. No agent, sign-in or model was started.')
        return 0
    selected = client or ('claude' if scope['client'] == 'claude_code' else 'codex')
    if selected not in ('codex', 'claude'):
        raise SetupError('Choose codex or claude.')
    executable = which(selected)
    if not executable:
        raise SetupError(('Codex' if selected == 'codex' else 'Claude Code')+' CLI is not installed or not on PATH. Install it using its official instructions, then rerun.')
    environment = dict(os.environ)
    environment.pop('REVEAL_MCP_TOKEN', None)
    environment['REVEAL_LOCAL_OFFLINE'] = '1' if offline else '0'
    print('Starting '+('Codex' if selected == 'codex' else 'Claude Code')+
          ('. Downloaded evidence reading remains available; remote Reveal calls are disabled.' if offline else
           '. Reveal sign-in is only needed for private research, validation and contributions.'))
    try:
        return runner(agent_command(executable, selected, root, scope, offline=offline), cwd=str(root), env=environment, check=False).returncode
    except OSError:
        raise SetupError('The agent could not be started. Existing local work is retained.') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Open a Reveal workspace and explore public research without signing in.')
    parser.add_argument('client', nargs='?', choices=('codex', 'claude'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-only', action='store_true', help='Verify workspace and anonymous MCP without starting an agent')
    mode.add_argument('--login', action='store_true', help='Sign in through the browser to contribute to Reveal')
    mode.add_argument('--logout', action='store_true', help='Revoke this connection while retaining local files')
    mode.add_argument('--materialize-evidence', metavar='SELECTION_JSON', help='Export/download selected receipt_ids, import_ids, reuse_receipt_ids and idempotency_key from a JSON file under output/')
    mode.add_argument('--download-public-capture', metavar='CAPTURE_ID', help='Download anonymous capture bytes before expiry')
    mode.add_argument('--mcp', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--offline', action='store_true', help='Keep the local evidence reader; disable remote Reveal calls')
    args = parser.parse_args(argv)
    offline = args.offline or os.environ.get('REVEAL_LOCAL_OFFLINE') == '1'
    try:
        root = Path(__file__).absolute().parent
        if args.materialize_evidence or args.download_public_capture:
            if offline: raise SetupError('Downloading evidence requires a connection. Remove --offline and unset REVEAL_LOCAL_OFFLINE to download.')
            scope = verify_bundle(root); bridge = Bridge(root, scope)
            if args.materialize_evidence:
                if not args.materialize_evidence.startswith('output/'): raise SetupError('Put the export selection JSON under output/.')
                result = bridge.materialize(read_json(root, args.materialize_evidence))
            else:
                result = bridge.download_public_capture({'capture_id': args.download_public_capture})
            print(json.dumps(result, indent=2)); return 0
        if args.mcp:
            return serve(root, verify_bundle(root), offline=offline)
        return launch(root, args.client, args.check_only, offline=offline, login=args.login, logout=args.logout)
    except SetupError as error:
        print('Reveal setup: '+str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Reveal setup interrupted. Existing work is retained.', file=sys.stderr)
        return 130
    except Exception:
        print('Reveal setup could not finish. Existing work is retained; check local file and credential-store access.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
