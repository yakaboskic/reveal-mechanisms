"""Trusted-gateway helper credentials, exact user identity, and atomic renewal."""
import json
import os
from pathlib import Path
import sys
import urllib.error
from uuid import uuid4

import jwt
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import gateway_session as cli

BASE = 'https://api-qa.example.invalid/api/reveal'
SECRET = 'gateway-signing-secret-' + 'a' * 40
SERVICE = 'gateway-service-credential-' + 'b' * 40


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    values = {'REVEAL_API_URL': BASE, 'REVEAL_GATEWAY_SECRET': SECRET,
        'REVEAL_GATEWAY_SERVICE_TOKEN': SERVICE, 'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs',
        'REVEAL_GATEWAY_AUDIENCE': 'reveal-api'}
    path = tmp_path / 'gateway.env'
    path.write_text(''.join(f'{key}={json.dumps(value)}\n' for key, value in values.items()))
    path.chmod(0o600)
    owner = str(uuid4())
    return {'root': tmp_path, 'env': path, 'output': tmp_path / 'session.json', 'values': values,
        'resolved': {'user_id': owner, 'principal_kind': 'registered'},
        'principal': {'user_id': owner, 'principal_kind': 'registered', 'workspace_expires_at': None,
            'display_name': None, 'email': None, 'email_verified': False, 'orcid': None,
            'orcid_authenticated': False, 'person': None}}


def arguments(setup, **extra):
    values = {'env-file': setup['env'], 'issuer': 'urn:reveal:application:dk',
              'subject': ' exact:CaseSensitive-user ', 'output': setup['output'], **extra}
    return [part for key, value in values.items() for part in ('--' + key, str(value))]


def mock_http(monkeypatch, responses):
    calls = []
    class Response:
        def __init__(self, value):
            self.status, self.value = value if isinstance(value, tuple) else (200, value)
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit):
            assert limit == 65537
            return self.value if isinstance(self.value, bytes) else json.dumps(self.value).encode()
    class Opener:
        def open(self, request, timeout):
            assert timeout == 30
            calls.append(request)
            value = responses.pop(0)
            if isinstance(value, Exception): raise value
            return Response(value)
    def build(*handlers):
        assert any(isinstance(value, cli.NoRedirect) for value in handlers)
        assert next(value for value in handlers if isinstance(value, cli.urllib.request.ProxyHandler)).proxies == {}
        return Opener()
    monkeypatch.setattr(cli.urllib.request, 'build_opener', build)
    return calls


def test_existing_gateway_protocol_signs_verifies_and_writes_private_session(setup, monkeypatch, capsys):
    before_env = setup['env'].read_bytes()
    calls = mock_http(monkeypatch, [setup['resolved'], setup['principal']])
    assert cli.main(arguments(setup)) == 0
    post, get = calls
    assert post.method == 'POST' and post.full_url == BASE + '/internal/v1/principals/resolve'
    assert post.get_header('Authorization') == 'Bearer ' + SERVICE
    assert post.get_header('Idempotency-key')
    assert json.loads(post.data) == {'issuer': 'urn:reveal:application:dk',
        'subject': ' exact:CaseSensitive-user ', 'display_name': None, 'email': None,
        'email_verified': False, 'orcid': None, 'orcid_authenticated': False}
    result = json.loads(setup['output'].read_text())
    token = result['access_token']
    assert get.method == 'GET' and get.full_url == BASE + '/v1/me' and get.data is None
    assert get.get_header('Authorization') == 'Bearer ' + token
    claims = jwt.decode(token, SECRET, algorithms=['HS256'], issuer='reveal-nextjs', audience='reveal-api')
    assert claims['sub'] == setup['principal']['user_id']
    assert claims['principal_kind'] == 'registered' and claims['exp'] - claims['iat'] == 300
    assert set(claims) == {'sub', 'principal_kind', 'iat', 'exp', 'iss', 'aud', 'jti'}
    assert jwt.get_unverified_header(token) == {'alg': 'HS256', 'typ': 'JWT'}
    # The real backend decoder accepts exactly this existing gateway contract.
    from reveal_backend.auth import decode_assertion
    for key, value in setup['values'].items(): monkeypatch.setenv(key, value)
    assert decode_assertion(token) == claims
    assert result == {'access_token': token, 'token_type': 'Bearer', 'expires_in': 300,
                      'principal': setup['principal'], 'api_url': BASE}
    assert setup['output'].stat().st_mode & 0o777 == 0o600
    assert setup['output'].stat().st_nlink == 1
    assert setup['env'].read_bytes() == before_env
    printed = capsys.readouterr()
    for secret in (SECRET, SERVICE, token):
        assert secret not in printed.out + printed.err
    assert SECRET not in setup['output'].read_text() and SERVICE not in setup['output'].read_text()
    assert not list(setup['root'].glob('.gateway-session-*'))


def test_renewal_is_explicit_atomic_and_retains_exact_user(setup, monkeypatch):
    calls = mock_http(monkeypatch, [setup['resolved'], setup['principal']] * 2)
    assert cli.main(arguments(setup)) == 0
    first = json.loads(setup['output'].read_text())
    inode = setup['output'].stat().st_ino
    assert cli.main(arguments(setup)) == 1
    assert len(calls) == 2
    assert cli.main([*arguments(setup), '--replace']) == 0
    second = json.loads(setup['output'].read_text())
    assert first['principal'] == second['principal']
    assert first['access_token'] != second['access_token']
    assert setup['output'].stat().st_ino != inode
    assert setup['output'].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure', ['network', 'wrong_owner', 'anonymous', 'expired', 'missing_expiry', 'denied'])
def test_failed_verification_preserves_previous_session_and_hides_provider_errors(setup, monkeypatch, capsys, failure):
    setup['output'].write_text('previous session'); setup['output'].chmod(0o600)
    principal = dict(setup['principal'])
    if failure == 'wrong_owner': principal['user_id'] = str(uuid4())
    if failure == 'anonymous': principal['principal_kind'] = 'anonymous'
    if failure == 'expired': principal['workspace_expires_at'] = '2000-01-01T00:00:00Z'
    if failure == 'missing_expiry': principal.pop('workspace_expires_at')
    if failure == 'network': principal = urllib.error.URLError('sensitive ' + SERVICE)
    if failure == 'denied': principal = urllib.error.HTTPError(BASE, 401, 'sensitive ' + SECRET, {}, None)
    calls = mock_http(monkeypatch, [setup['resolved'], principal])
    assert cli.main([*arguments(setup), '--replace']) == 1
    assert len(calls) == 2 and setup['output'].read_text() == 'previous session'
    assert not list(setup['root'].glob('.gateway-session-*'))
    output = capsys.readouterr()
    assert SERVICE not in output.out + output.err and SECRET not in output.out + output.err


def test_invalid_resolve_response_does_not_sign_or_leave_output(setup, monkeypatch):
    calls = mock_http(monkeypatch, [{'user_id': str(uuid4()), 'principal_kind': 'anonymous'}])
    def forbidden(*args, **kwargs): raise AssertionError('Do not sign an unverified resolution')
    monkeypatch.setattr(cli.jwt, 'encode', forbidden)
    assert cli.main(arguments(setup)) == 1
    assert len(calls) == 1 and not setup['output'].exists()
    assert not list(setup['root'].glob('.gateway-session-*'))


@pytest.mark.parametrize('url', ['http://api.example.invalid', 'http://127.0.0.1:18001',
    'https://name:password@api.example.invalid', BASE + '?credential=secret', BASE + '#fragment',
    BASE + '/%2e%2e', BASE + '/..', BASE + '\\path', 'https://api.example.invalid:99999'])
def test_unapproved_api_urls_fail_before_credentials_are_sent(setup, monkeypatch, url):
    setup['env'].write_text(setup['env'].read_text().replace(BASE, url))
    calls = mock_http(monkeypatch, [])
    assert cli.main(arguments(setup)) == 1
    assert not calls and not setup['output'].exists()


def test_explicit_loopback_http_allows_local_development_only(setup, monkeypatch):
    setup['env'].write_text(setup['env'].read_text().replace(BASE, 'http://127.0.0.1:18001'))
    calls = mock_http(monkeypatch, [setup['resolved'], setup['principal']])
    assert cli.main([*arguments(setup), '--allow-local-http']) == 0
    assert calls[0].full_url.startswith('http://127.0.0.1:18001/')
    setup['env'].write_text(setup['env'].read_text().replace('127.0.0.1', 'evil.invalid'))
    assert cli.main([*arguments(setup), '--allow-local-http', '--replace']) == 1
    assert len(calls) == 2


@pytest.mark.parametrize('issuer,subject', [('http://app.example.invalid', 'u'), ('not-a-uri', 'u'),
    ('urn:reveal:application:dk', ''), ('urn:reveal:application:dk', ' '),
    ('urn:reveal:application:dk', 'a' * 257), ('urn:reveal:application:dk', 'a\nb')])
def test_invalid_identity_namespace_or_subject_makes_no_request(setup, monkeypatch, issuer, subject):
    calls = mock_http(monkeypatch, [])
    assert cli.main(arguments(setup, issuer=issuer, subject=subject)) == 1
    assert not calls and not setup['output'].exists()


def test_redirects_refused_without_forwarding_credential(setup, monkeypatch):
    redirect = urllib.error.HTTPError(BASE, 302, 'redirect', {'Location': 'https://evil.invalid'}, None)
    calls = mock_http(monkeypatch, [redirect])
    assert cli.main(arguments(setup)) == 1
    assert len(calls) == 1 and not setup['output'].exists()
    assert cli.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid') is None


def test_private_environment_symlink_and_replacing_environment_are_rejected(setup, monkeypatch):
    calls = mock_http(monkeypatch, [])
    setup['env'].chmod(0o644)
    assert cli.main(arguments(setup)) == 1
    setup['env'].chmod(0o600)
    link = setup['root'] / 'env-link'; link.symlink_to(setup['env'])
    assert cli.main(arguments(setup, **{'env-file': link})) == 1
    before = setup['env'].read_bytes()
    assert cli.main([*arguments(setup, output=setup['env']), '--replace']) == 1
    assert setup['env'].read_bytes() == before and not calls


def test_output_symlinks_hardlinks_and_public_parent_rejected_before_http(setup, monkeypatch):
    calls = mock_http(monkeypatch, [])
    target = setup['root'] / 'original'; target.write_text('preserve'); target.chmod(0o600)
    setup['output'].symlink_to(target)
    assert cli.main([*arguments(setup), '--replace']) == 1
    setup['output'].unlink(); os.link(target, setup['output'])
    assert cli.main([*arguments(setup), '--replace']) == 1
    setup['output'].unlink(); setup['root'].chmod(0o755)
    assert cli.main(arguments(setup)) == 1
    assert target.read_text() == 'preserve' and not calls


def test_concurrent_output_creation_does_not_get_overwritten(setup, monkeypatch):
    responses = [setup['resolved'], setup['principal']]
    def request(*args, **kwargs):
        value = responses.pop(0)
        if not responses:
            setup['output'].write_text('concurrent session'); setup['output'].chmod(0o600)
        return value
    monkeypatch.setattr(cli, 'request_json', request)
    assert cli.main(arguments(setup)) == 1
    assert setup['output'].read_text() == 'concurrent session'
    assert not list(setup['root'].glob('.gateway-session-*'))


def test_environment_interpolation_disabled_and_argument_errors_sanitized(setup, monkeypatch, capsys):
    setup['env'].write_text(setup['env'].read_text().replace(SECRET, SECRET + '${DO_NOT_EXPAND}'))
    monkeypatch.setenv('DO_NOT_EXPAND', 'expanded-value')
    assert cli.environment(setup['env'])['REVEAL_GATEWAY_SECRET'] == SECRET + '${DO_NOT_EXPAND}'
    assert cli.main(['--unknown', SECRET]) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
