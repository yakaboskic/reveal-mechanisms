"""Operator key issuance uses isolated identities and private, exclusive outputs."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
from uuid import uuid4

import jwt
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import issue_api_key as cli

BASE = 'https://api-qa.example.invalid/api/reveal'
SERVICE_TOKEN = 'operator-only-service-' + 'x' * 40
GATEWAY_SECRET = 'operator-only-signing-' + 'y' * 40


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    env = tmp_path / 'operator.env'
    env.write_text('REVEAL_GATEWAY_SERVICE_TOKEN=' + SERVICE_TOKEN + '\n'
                   'REVEAL_GATEWAY_SECRET=' + GATEWAY_SECRET + '\n'
                   'REVEAL_WORKFLOW_URL=' + BASE + '/internal/workflows/research-v1\n'
                   'REVEAL_ENVIRONMENT=production\n')
    return {'root': tmp_path, 'env': env, 'output': tmp_path / 'handoff.json',
            'config': tmp_path / 'config.json'}


def args(setup, **extra):
    values = {'env-file': setup['env'], 'api-url': BASE, 'label': 'dk',
              'output': setup['output'], **extra}
    return [part for key, value in values.items() for part in ('--' + key, str(value))]


def identity(*, user=None, kind='anonymous'):
    return {'user_id': user or str(uuid4()), 'principal_kind': kind,
            'workspace_expires_at': ((datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                                     if kind == 'anonymous' else None)}


def mock_http(monkeypatch, responses):
    calls = []
    class Response:
        def __init__(self, value):
            self.status, self.value = value if isinstance(value, tuple) else (201, value)
        def __enter__(self): return self
        def __exit__(self, *ignored): return False
        def read(self, limit):
            return self.value if isinstance(self.value, bytes) else json.dumps(self.value).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 30
            value = responses.pop(0)
            if isinstance(value, Exception): raise value
            return Response(value)
    def build(*handlers):
        assert any(isinstance(handler, cli.NoRedirect) for handler in handlers)
        assert next(handler for handler in handlers if isinstance(handler, cli.urllib.request.ProxyHandler)).proxies == {}
        return Opener()
    monkeypatch.setattr(cli.urllib.request, 'build_opener', build)
    return calls


def test_first_issuance_private_handoff_headers_hash_and_no_credential_output(setup, monkeypatch, capsys):
    owner = identity()
    before = setup['env'].read_bytes()
    calls = mock_http(monkeypatch, [owner])
    assert cli.main(args(setup, **{'config-output': setup['config']})) == 0
    handoff = json.loads(setup['output'].read_text())
    config = json.loads(setup['config'].read_text())
    key = handoff['api_key']
    assert re.fullmatch(r'rvl_[A-Za-z0-9_-]{43}', key)
    assert len(base64.urlsafe_b64decode(key[4:] + '=')) == 32
    assert config == {'REVEAL_API_KEY_SHA256': hashlib.sha256(key.encode()).hexdigest(),
                      'REVEAL_API_KEY_USER_ID': owner['user_id']}
    assert handoff['user_id'] == owner['user_id']
    assert handoff['workspace_expires_at'] == owner['workspace_expires_at']
    assert handoff['label'] == 'dk' and handoff['api_url'] == BASE and handoff['status'] == 'issued'
    assert len(calls) == 1
    request = calls[0]
    assert request.full_url == BASE + '/internal/v1/principals/anonymous'
    assert request.method == 'POST' and request.data == b'{}'
    assert request.get_header('Authorization') == 'Bearer ' + SERVICE_TOKEN
    assert request.get_header('Idempotency-key') == handoff['request_id']
    assert handoff['request_id'].startswith('api-key-')
    assert setup['env'].read_bytes() == before
    assert setup['output'].stat().st_mode & 0o777 == 0o600
    assert setup['config'].stat().st_mode & 0o777 == 0o600
    printed = capsys.readouterr()
    for secret in (key, SERVICE_TOKEN, GATEWAY_SECRET):
        assert secret not in printed.out + printed.err
        assert secret not in setup['config'].read_text()
    assert SERVICE_TOKEN not in setup['output'].read_text()
    assert GATEWAY_SECRET not in setup['output'].read_text()


def test_config_output_optional_and_environment_unchanged(setup, monkeypatch):
    before = dict(os.environ)
    mock_http(monkeypatch, [identity()])
    assert cli.main(args(setup)) == 0
    assert not setup['config'].exists()
    assert dict(os.environ) == before


@pytest.mark.parametrize('target', ['output', 'config'])
def test_existing_outputs_never_overwritten_or_provisioned(setup, monkeypatch, target):
    setup[target].write_text('preserve')
    calls = mock_http(monkeypatch, [])
    assert cli.main(args(setup, **{'config-output': setup['config']})) == 1
    assert setup[target].read_text() == 'preserve'
    assert not calls


def test_symlink_output_and_parent_refused(setup, monkeypatch):
    target = setup['root'] / 'target'
    target.write_text('preserve')
    setup['output'].symlink_to(target)
    calls = mock_http(monkeypatch, [])
    assert cli.main(args(setup)) == 1
    assert target.read_text() == 'preserve' and not calls
    link = setup['root'] / 'linked-directory'
    link.symlink_to(setup['root'], target_is_directory=True)
    assert cli.main(args(setup, output=link / 'new.json')) == 1
    assert not calls and not (setup['root'] / 'new.json').exists()


def test_output_parent_must_be_private_before_http(setup, monkeypatch):
    setup['root'].chmod(0o755)
    calls = mock_http(monkeypatch, [])
    assert cli.main(args(setup)) == 1
    assert not calls and not setup['output'].exists()


@pytest.mark.parametrize('url', [
    'https://evil.invalid/api/reveal', 'https://api-qa.example.invalid.evil.invalid/api/reveal',
    'http://api-qa.example.invalid/api/reveal', BASE + '?token=secret', BASE + '#fragment',
    'https://user:password@api-qa.example.invalid/api/reveal', BASE + '/wrong',
    BASE + '/..', BASE + '/%2e%2e', BASE + '\\evil', BASE + '\n',
    'https://api-qa.example.invalid:99999/api/reveal',
])
def test_url_rejected_before_any_credential_request(setup, monkeypatch, url):
    calls = mock_http(monkeypatch, [])
    assert cli.main(args(setup, **{'api-url': url})) == 1
    assert not calls and not setup['output'].exists()


@pytest.mark.parametrize('environment,service_env,allowed', [
    ('development', '', True), ('test', '', True), ('production', '', False),
    ('development', 'prod', False), ('test', 'qa', False),
])
def test_loopback_http_requires_explicit_development_mode(setup, monkeypatch, environment, service_env, allowed):
    text = setup['env'].read_text().replace('REVEAL_ENVIRONMENT=production', 'REVEAL_ENVIRONMENT=' + environment)
    setup['env'].write_text(text + 'SERVICE_ENV=' + service_env + '\n')
    calls = mock_http(monkeypatch, [identity()] if allowed else [])
    assert cli.main(args(setup, **{'api-url': 'http://127.0.0.1:18001'})) == (0 if allowed else 1)
    assert len(calls) == int(allowed)


def test_redirects_not_followed_and_error_body_is_not_exposed(setup, monkeypatch, capsys):
    error = urllib.error.HTTPError(BASE, 302, SERVICE_TOKEN, {'Location': 'https://evil.invalid'}, None)
    calls = mock_http(monkeypatch, [error])
    assert cli.main(args(setup)) == 1
    assert len(calls) == 1
    assert cli.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid') is None
    assert SERVICE_TOKEN not in capsys.readouterr().err


def test_lost_response_retains_pending_request_and_never_automatically_retries(setup, monkeypatch, capsys):
    calls = mock_http(monkeypatch, [urllib.error.URLError('secret=' + SERVICE_TOKEN)])
    assert cli.main(args(setup)) == 1
    pending = json.loads(setup['output'].read_text())
    assert pending['status'] == 'pending' and 'api_key' not in pending
    assert pending['request_id'] == calls[0].get_header('Idempotency-key')
    assert cli.main(args(setup)) == 1
    assert len(calls) == 1 and json.loads(setup['output'].read_text()) == pending
    printed = capsys.readouterr()
    assert SERVICE_TOKEN not in printed.out + printed.err


@pytest.mark.parametrize('kind', ['anonymous', 'registered'])
def test_rotation_reuses_verified_owner_without_creating_identity_or_extending_expiry(setup, monkeypatch, kind):
    owner = identity(kind=kind)
    answers = [(200, owner)]
    if kind == 'registered': answers.insert(0, (401, {}))
    calls = mock_http(monkeypatch, answers * 2)
    keys = []
    for number in (1, 2):
        path = setup['root'] / f'rotation-{number}.json'
        assert cli.main(args(setup, output=path, **{'owner-user-id': owner['user_id']})) == 0
        handoff = json.loads(path.read_text())
        assert handoff['user_id'] == owner['user_id']
        assert handoff['workspace_expires_at'] == owner['workspace_expires_at']
        keys.append(handoff['api_key'])
    assert keys[0] != keys[1]
    for call in calls:
        assert call.method == 'GET' and call.data is None and call.full_url == BASE + '/v1/me'
        token = call.get_header('Authorization').removeprefix('Bearer ')
        claims = jwt.decode(token, GATEWAY_SECRET, algorithms=['HS256'], audience='reveal-api', issuer='reveal-nextjs')
        assert claims['sub'] == owner['user_id'] and claims['exp'] - claims['iat'] == 60
        assert set(claims) == {'sub', 'principal_kind', 'iat', 'exp', 'iss', 'aud', 'jti'}


@pytest.mark.parametrize('failure', ['wrong_owner', 'wrong_kind', 'expired', 'inactive'])
def test_rotation_refuses_mismatched_or_inactive_principal(setup, monkeypatch, failure):
    owner = identity()
    requested = owner['user_id']
    if failure == 'wrong_owner': owner['user_id'] = str(uuid4())
    if failure == 'wrong_kind': owner['principal_kind'] = 'registered'; owner['workspace_expires_at'] = None
    if failure == 'expired': owner['workspace_expires_at'] = '2000-01-01T00:00:00Z'
    answers = [(401, {}), (401, {})] if failure == 'inactive' else [(200, owner)]
    calls = mock_http(monkeypatch, answers)
    assert cli.main(args(setup, **{'owner-user-id': requested})) == 1
    assert 'api_key' not in json.loads(setup['output'].read_text())
    assert all(call.method == 'GET' for call in calls)


@pytest.mark.parametrize('response', [[], b'not json ' + SERVICE_TOKEN.encode(), b'x' * 65537,
                                      {'user_id': 'not-a-uuid'}, identity(kind='registered')])
def test_invalid_creation_response_never_issues_or_prints_credentials(setup, monkeypatch, capsys, response):
    calls = mock_http(monkeypatch, [response])
    assert cli.main(args(setup)) == 1
    assert len(calls) == 1
    assert 'api_key' not in json.loads(setup['output'].read_text())
    printed = capsys.readouterr()
    assert SERVICE_TOKEN not in printed.out + printed.err


def test_argument_errors_do_not_echo_arbitrary_values(capsys):
    assert cli.main(['--unknown', SERVICE_TOKEN]) == 1
    printed = capsys.readouterr()
    assert SERVICE_TOKEN not in printed.out + printed.err
