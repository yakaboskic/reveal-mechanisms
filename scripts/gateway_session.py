#!/usr/bin/env python3
"""Resolve an application user through the existing trusted gateway and save a session.

Keep the dotenv handoff and this helper on a trusted server. --issuer names the
application identity namespace; --subject must come from its authenticated user
session. Neither value should be taken unchecked from a browser request.
Outputs require an existing owner-only directory. Renewal requires --replace;
failed renewal preserves the previous file. Credentials are never printed.
"""
import argparse
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

from dotenv import dotenv_values
import jwt


class SessionError(Exception):
    """Only fixed, credential-free messages may be displayed."""


def private_regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077 and info.st_nlink == 1)


def environment(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        if not private_regular(os.fstat(stream.fileno())):
            raise SessionError('Environment handoff must be a private, owner-only regular file.')
        values = dotenv_values(stream=stream, interpolate=False)
    for key in ('REVEAL_GATEWAY_SECRET', 'REVEAL_GATEWAY_SERVICE_TOKEN'):
        if not isinstance(values.get(key), str) or len(values[key]) < 32:
            raise SessionError('Required gateway credentials are missing.')
    for key in ('REVEAL_API_URL', 'REVEAL_GATEWAY_ISSUER', 'REVEAL_GATEWAY_AUDIENCE'):
        if not isinstance(values.get(key), str) or not values[key].strip():
            raise SessionError('Required gateway configuration is missing.')
    return values


def api_base(value, allow_local_http=False):
    if not isinstance(value, str) or re.search(r'[\s\\%]', value):
        raise SessionError('Invalid API URL.')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or '?' in value or '#' in value
            or any(part in ('.', '..') for part in parsed.path.split('/'))):
        raise SessionError('Invalid API URL.')
    parsed.port
    if parsed.scheme == 'http':
        try: loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError: loopback = parsed.hostname == 'localhost'
        if not allow_local_http or not loopback:
            raise SessionError('HTTP requires --allow-local-http and a loopback API URL.')
    return value.rstrip('/')


def identity_input(issuer, subject):
    if not isinstance(issuer, str) or len(issuer) > 512:
        raise SessionError('Issuer must be an HTTPS or URN application namespace.')
    if issuer.startswith('https://'):
        api_base(issuer)
    elif not re.fullmatch(r'urn:[A-Za-z0-9][A-Za-z0-9-]{0,31}:[A-Za-z0-9][A-Za-z0-9:._-]{0,255}', issuer):
        raise SessionError('Issuer must be an HTTPS or URN application namespace.')
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in subject):
        raise SessionError('Subject must be an exact nonblank user ID of at most 256 characters without control characters.')
    return {'issuer': issuer, 'subject': subject, 'display_name': None, 'email': None,
            'email_verified': False, 'orcid': None, 'orcid_authenticated': False}


def target_stat(parent, name):
    try: return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError: return None


def version(info):
    return None if info is None else (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@contextmanager
def output_file(path, replace=False):
    path = Path(os.path.abspath(path))
    if any(parent.is_symlink() for parent in path.parents):
        raise SessionError('Output directories must not be symlinks.')
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise SessionError('Output parent must be an existing owner-only directory (0700).')
        before = target_stat(parent, path.name)
        if before is not None and (not replace or not private_regular(before)):
            raise SessionError('Output exists; --replace requires an owner-only regular session file.')
        candidate = '.gateway-session-' + secrets.token_hex(16)
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        temporary = candidate
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        if version(target_stat(parent, path.name)) != version(before):
            raise SessionError('Output changed during renewal; the newer file was preserved.')
        if replace and before is not None:
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            temporary = None
        else:
            # Atomic exclusive publication: unlike rename, this never overwrites
            # a file created concurrently after the initial existence check.
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
    finally:
        try:
            if temporary is not None:
                os.unlink(temporary, dir_fd=parent)
        finally:
            os.close(parent)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, bearer, body=None):
    headers = {'Authorization': 'Bearer ' + bearer, 'Accept': 'application/json'}
    if body is not None:
        headers.update({'Content-Type': 'application/json', 'Idempotency-Key': str(uuid4())})
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
        headers=headers, method='GET' if body is None else 'POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200:
                raise SessionError('Gateway request failed; no session file was changed.')
            raw = response.read(65537)
            if len(raw) > 65536:
                raise SessionError('Gateway response is invalid.')
            value = json.loads(raw)
            if not isinstance(value, dict): raise SessionError('Gateway response is invalid.')
            return value
    except urllib.error.HTTPError:
        raise SessionError('Gateway request failed; no session file was changed.') from None


def registered_identity(value, owner=None):
    user = value.get('user_id')
    if (not isinstance(user, str) or str(UUID(user)) != user
            or value.get('principal_kind') != 'registered' or (owner and owner != user)):
        raise SessionError('Gateway returned an unexpected identity.')
    return user


def create_session(args):
    values = environment(args.env_file)
    base = api_base(values['REVEAL_API_URL'], args.allow_local_http)
    body = identity_input(args.issuer, args.subject)
    if (Path(os.path.abspath(args.env_file)) == Path(os.path.abspath(args.output))
            or (args.output.exists() and os.path.samefile(args.env_file, args.output))):
        raise SessionError('Session output must not replace the environment handoff.')
    with output_file(args.output, args.replace) as stream:
        resolved = request_json(base + '/internal/v1/principals/resolve', values['REVEAL_GATEWAY_SERVICE_TOKEN'], body)
        user = registered_identity(resolved)
        issued = int(time.time())
        claims = {'sub': user, 'principal_kind': 'registered', 'iat': issued, 'exp': issued + 300,
                  'iss': values['REVEAL_GATEWAY_ISSUER'], 'aud': values['REVEAL_GATEWAY_AUDIENCE'], 'jti': str(uuid4())}
        token = jwt.encode(claims, values['REVEAL_GATEWAY_SECRET'], algorithm='HS256', headers={'typ': 'JWT'})
        principal = request_json(base + '/v1/me', token)
        registered_identity(principal, user)
        if 'workspace_expires_at' not in principal or principal['workspace_expires_at'] is not None:
            raise SessionError('Gateway returned an unexpected workspace expiry.')
        if time.time() >= claims['exp']:
            raise SessionError('Session expired during verification; no session file was changed.')
        json.dump({'access_token': token, 'token_type': 'Bearer', 'expires_in': 300,
                   'principal': principal, 'api_url': base}, stream, indent=2, sort_keys=True)
        stream.write('\n')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SessionError('Invalid arguments; use --help for usage.')


def main(argv=None):
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--issuer', required=True, help='Application namespace, e.g. urn:reveal:application:dk.')
    parser.add_argument('--subject', required=True, help='Exact user ID from the application authenticated session.')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--replace', action='store_true', help='Atomically renew an existing private session output.')
    parser.add_argument('--allow-local-http', action='store_true', help='Allow literal loopback HTTP for development/test.')
    try:
        create_session(parser.parse_args(argv))
    except SessionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Gateway session failed; credentials were not displayed and no deployment configuration was changed.', file=sys.stderr)
        return 1
    print('Verified session written to the private output file.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
