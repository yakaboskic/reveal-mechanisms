"""Canonical workspace links come only from trusted deployment configuration."""
import pytest

from reveal_backend.auth import Problem
from reveal_backend.research_setup import urls


@pytest.fixture(autouse=True)
def isolated_urls(monkeypatch):
    for key in ('REVEAL_PUBLIC_WEB_URL', 'REVEAL_CANONICAL_URL', 'NEXTAUTH_URL', 'REVEAL_WORKFLOW_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('REVEAL_PUBLIC_API_URL', 'https://tunnel.example.org/api/reveal')


@pytest.mark.parametrize('key', ['REVEAL_CANONICAL_URL', 'NEXTAUTH_URL'])
@pytest.mark.parametrize('web', ['http://localhost:3100', 'http://127.0.0.1:3200',
    'http://[::1]:3300', 'https://reveal.example.org'])
def test_public_api_uses_configured_browser_origin_and_port(monkeypatch, key, web):
    monkeypatch.setenv(key, web+'/')
    result = urls('work/id')
    assert result['return_url'] == web+'/local-runs/work%2Fid'
    assert result['mcp_url'] == 'https://tunnel.example.org/api/reveal/mcp'
    assert result['device_authorization_url'] == 'https://tunnel.example.org/api/reveal/oauth/device_authorization'


def test_explicit_public_web_overrides_canonical_and_auth_urls(monkeypatch):
    monkeypatch.setenv('REVEAL_PUBLIC_WEB_URL', 'https://public.example.org/reveal/')
    monkeypatch.setenv('REVEAL_CANONICAL_URL', 'https://canonical.example.org')
    monkeypatch.setenv('NEXTAUTH_URL', 'https://auth.example.org')
    assert urls('work')['return_url'] == 'https://public.example.org/reveal/local-runs/work'
    monkeypatch.setenv('REVEAL_PUBLIC_WEB_URL', '')
    assert urls('work')['return_url'] == 'https://canonical.example.org/local-runs/work'
    monkeypatch.setenv('REVEAL_CANONICAL_URL', '')
    assert urls('work')['return_url'] == 'https://auth.example.org/local-runs/work'


@pytest.mark.parametrize('bad', ['http://remote.example.org', '/relative', 'https://',
    'https://user:pass@example.org', 'https://@example.org', 'https://example.org?redirect=evil',
    'https://example.org#fragment', 'https://example.org:invalid', 'https://example.org:70000',
    'https://example.org:0', 'https://example.org:', 'https://[broken',
    'https://example.org\n.evil', 'https://example.org/path with space',
    ' https://example.org', 'https://example.org\\evil', 'https://example%2eorg'])
@pytest.mark.parametrize('key', ['REVEAL_PUBLIC_WEB_URL', 'REVEAL_CANONICAL_URL', 'NEXTAUTH_URL'])
def test_invalid_selected_setting_fails_closed(monkeypatch, key, bad):
    monkeypatch.setenv('NEXTAUTH_URL', 'https://otherwise-valid.example.org')
    monkeypatch.setenv(key, bad)
    with pytest.raises(Problem) as error:
        urls('work')
    assert error.value.status == 503
    assert error.value.code == 'SETUP_NOT_CONFIGURED'
    assert bad not in error.value.detail


def test_missing_remote_config_does_not_trust_request_environment(monkeypatch):
    for key in ('HTTP_HOST', 'HTTP_ORIGIN', 'HTTP_X_FORWARDED_HOST'):
        monkeypatch.setenv(key, 'https://untrusted.example.org')
    with pytest.raises(Problem) as error:
        urls('work')
    assert error.value.code == 'SETUP_NOT_CONFIGURED'


def test_loopback_without_browser_configuration_preserves_legacy_default(monkeypatch):
    monkeypatch.setenv('REVEAL_PUBLIC_API_URL', 'http://127.0.0.1:18001')
    assert urls('work')['return_url'] == 'http://localhost:3000/local-runs/work'
