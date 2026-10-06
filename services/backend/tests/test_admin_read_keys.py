"""The administrative read key never becomes an application or gateway identity."""
import hashlib
from unittest.mock import Mock

import pytest

from reveal_backend import admin_read_keys as keys, api_keys, auth

KEY = 'rvl_admin_' + 'A' * 43
ROTATED = 'rvl_admin_' + 'B' * 43
KEY_ID = '11111111-1111-4111-8111-111111111111'
PAIR = dict(zip(keys.SETTINGS, (hashlib.sha256(KEY.encode()).hexdigest(), KEY_ID)))


@pytest.fixture
def configured(monkeypatch):
    for name, value in PAIR.items(): monkeypatch.setenv(name, value)


def test_fixed_scope_and_exact_disabled_or_enabled_pair():
    assert keys.SCOPE == 'science:read'
    assert keys.configuration({}) is None
    assert keys.configuration(dict(zip(keys.SETTINGS, (' ', '\t')))) is None
    assert keys.configuration(PAIR) == (PAIR[keys.SETTINGS[0]], KEY_ID)


@pytest.mark.parametrize('fingerprint,key_id', [
    ('x' * 64, KEY_ID), ('A' * 64, KEY_ID), (KEY, KEY_ID),
    (PAIR[keys.SETTINGS[0]], ''), ('', KEY_ID),
    (PAIR[keys.SETTINGS[0]], KEY_ID.replace('-', '')),
    (PAIR[keys.SETTINGS[0]], 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'),
    (PAIR[keys.SETTINGS[0]], ' ' + KEY_ID), (None, None), ([], {}),
])
def test_invalid_configuration_fails_closed_without_values(fingerprint, key_id):
    with pytest.raises(auth.Problem) as error:
        keys.configuration(dict(zip(keys.SETTINGS, (fingerprint, key_id))))
    assert error.value.status == 503
    assert error.value.code == 'ADMIN_READ_API_KEY_CONFIGURATION_INVALID'
    assert error.value.detail == 'Administrative scientific access is unavailable.'
    assert error.value.extra == {}


@pytest.mark.parametrize('authorization', [
    None, '', KEY, 'Bearer ', 'bearer ' + KEY, 'Basic ' + KEY,
    'Bearer ' + KEY + ' ', 'Bearer ' + KEY + '\n', 'Bearer  ' + KEY,
    'Bearer rvl_' + 'A' * 43, 'Bearer eyJhbGciOiJIUzI1NiJ9.e30.signature',
    'Bearer service-token-' + 'a' * 40, 'Bearer rvl_admin_' + 'é' * 43,
    'Bearer ' + ROTATED, 'Bearer rvl_admin_' + 'A' * 42,
])
def test_only_exact_administrator_bearer_is_accepted(configured, authorization):
    with pytest.raises(auth.Problem) as error: keys.authenticate(authorization)
    assert error.value.status == 401
    assert error.value.code == 'INVALID_ADMIN_READ_API_KEY'
    assert error.value.detail == 'An administrative scientific read key is required.'
    assert error.value.extra == {}


def test_authentication_returns_credential_id_and_rotation_and_revocation_take_effect(configured, monkeypatch):
    assert keys.authenticate('Bearer ' + KEY) == KEY_ID
    monkeypatch.setenv(keys.SETTINGS[0], hashlib.sha256(ROTATED.encode()).hexdigest())
    replacement_id = '22222222-2222-4222-8222-222222222222'
    monkeypatch.setenv(keys.SETTINGS[1], replacement_id)
    with pytest.raises(auth.Problem) as error: keys.authenticate('Bearer ' + KEY)
    assert error.value.status == 401
    assert keys.authenticate('Bearer ' + ROTATED) == replacement_id
    for name in keys.SETTINGS: monkeypatch.setenv(name, '')
    with pytest.raises(auth.Problem) as error: keys.authenticate('Bearer ' + ROTATED)
    assert error.value.status == 401


def test_admin_key_is_never_an_owner_gateway_or_identity_assertion(configured, monkeypatch):
    monkeypatch.setenv('REVEAL_GATEWAY_SECRET', 's' * 40)
    monkeypatch.setenv('REVEAL_GATEWAY_SERVICE_TOKEN', 't' * 40)
    tx = Mock()
    for check in (lambda: auth.principal(tx, 'Bearer ' + KEY),
                  lambda: auth.credential_expiry('Bearer ' + KEY),
                  lambda: api_keys.authenticate(KEY),
                  lambda: auth.service_authority('Bearer ' + KEY),
                  lambda: auth.decode_assertion(KEY, 'admin_telemetry')):
        with pytest.raises(auth.Problem) as error: check()
        assert error.value.status in (401, 403)
    tx.get.assert_not_called()


def test_valid_token_with_invalid_configuration_returns_unavailable(configured, monkeypatch):
    monkeypatch.setenv(keys.SETTINGS[1], '')
    with pytest.raises(auth.Problem) as error: keys.authenticate('Bearer ' + KEY)
    assert error.value.status == 503


def test_readiness_checks_admin_configuration_before_database_or_catalog(configured, monkeypatch):
    from fastapi.testclient import TestClient
    from reveal_backend import app as api
    monkeypatch.setenv(keys.SETTINGS[1], '')
    database = Mock(); catalog = Mock()
    monkeypatch.setattr(api, 'repo', database)
    monkeypatch.setattr(api, 'catalog', catalog)
    response = TestClient(api.app).get('/readyz')
    assert response.status_code == 503
    assert response.json()['code'] == 'ADMIN_READ_API_KEY_CONFIGURATION_INVALID'
    database.readiness.assert_not_called()
    catalog.readiness.assert_not_called()
    assert KEY not in response.text and PAIR[keys.SETTINGS[0]] not in response.text
