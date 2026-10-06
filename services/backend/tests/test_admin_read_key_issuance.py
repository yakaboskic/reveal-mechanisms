"""Administrative read keys are generated offline into exclusive private files."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import sys
import urllib.request
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import issue_admin_read_api_key as cli

BASE = 'https://api-qa.example.invalid/api/reveal'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    env = tmp_path / 'operator.env'
    env.write_text('REVEAL_ENVIRONMENT=production\nSERVICE_ENV=qa\n'
                   'REVEAL_WORKFLOW_URL=' + BASE + '/internal/workflows/research-v1\n')
    def no_network(*args, **kwargs): raise AssertionError('Issuance must be offline')
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(urllib.request, 'build_opener', no_network)
    return {'root': tmp_path, 'env': env, 'output': tmp_path / 'handoff.json',
            'config': tmp_path / 'config.json'}


def args(setup, **extra):
    values = {'env-file': setup['env'], 'api-url': BASE, 'label': 'qa-review',
              'output': setup['output'], 'config-output': setup['config'], **extra}
    return [part for key, value in values.items() for part in ('--' + key, str(value))]


def test_offline_issuance_has_fixed_scope_private_files_and_hash_only_config(setup, capsys):
    before = setup['env'].read_bytes()
    assert cli.main(args(setup)) == 0
    handoff = json.loads(setup['output'].read_text())
    config = json.loads(setup['config'].read_text())
    key = handoff['api_key']
    assert re.fullmatch(r'rvl_admin_[A-Za-z0-9_-]{43}', key)
    assert len(base64.urlsafe_b64decode(key.removeprefix('rvl_admin_') + '=')) == 32
    assert str(UUID(handoff['key_id'])) == handoff['key_id']
    assert config == {'REVEAL_ADMIN_READ_API_KEY_SHA256': hashlib.sha256(key.encode()).hexdigest(),
                      'REVEAL_ADMIN_READ_API_KEY_ID': handoff['key_id']}
    assert handoff['api_url'] == BASE and handoff['scope'] == 'science:read'
    assert handoff['label'] == 'qa-review' and handoff['status'] == 'issued'
    assert 'user_id' not in handoff and 'workspace_expires_at' not in handoff
    issued = datetime.fromisoformat(handoff['created_at'].replace('Z', '+00:00'))
    assert 0 <= (datetime.now(timezone.utc) - issued).total_seconds() < 5
    assert setup['env'].read_bytes() == before
    assert setup['output'].stat().st_mode & 0o777 == 0o600
    assert setup['config'].stat().st_mode & 0o777 == 0o600
    printed = capsys.readouterr()
    assert key not in printed.out + printed.err + setup['config'].read_text()


def test_rotation_makes_new_key_and_credential_id_without_overwriting(setup):
    assert cli.main(args(setup)) == 0
    before = setup['output'].read_bytes()
    second = setup['root'] / 'rotation.json'; config = setup['root'] / 'rotation-config.json'
    assert cli.main(args(setup, output=second, **{'config-output': config})) == 0
    first_value = json.loads(before); second_value = json.loads(second.read_text())
    assert first_value['api_key'] != second_value['api_key']
    assert first_value['key_id'] != second_value['key_id']
    assert setup['output'].read_bytes() == before


@pytest.mark.parametrize('target', ['output', 'config'])
def test_existing_files_are_never_overwritten(setup, target):
    setup[target].write_text('preserve')
    assert cli.main(args(setup)) == 1
    assert setup[target].read_text() == 'preserve'
    other = setup['config' if target == 'output' else 'output']
    assert not other.exists() or not other.read_text()


def test_same_output_path_fails_before_key_written(setup):
    assert cli.main(args(setup, **{'config-output': setup['output']})) == 1
    assert setup['output'].read_text() == ''


@pytest.mark.parametrize('target', ['output', 'config'])
def test_symlink_output_and_parent_rejected(setup, target):
    source = setup['root'] / 'source'; source.write_text('preserve')
    setup[target].symlink_to(source)
    assert cli.main(args(setup)) == 1
    assert source.read_text() == 'preserve'
    link = setup['root'] / 'linked'; link.symlink_to(setup['root'], target_is_directory=True)
    assert cli.main(args(setup, output=link / 'new.json')) == 1
    assert not (setup['root'] / 'new.json').exists()


def test_nonprivate_output_directory_rejected(setup):
    setup['root'].chmod(0o755)
    assert cli.main(args(setup)) == 1
    assert not setup['output'].exists() and not setup['config'].exists()


@pytest.mark.parametrize('url', [
    'https://evil.invalid/api/reveal', BASE + '?secret=sensitive', BASE + '#fragment',
    'https://user:password@api-qa.example.invalid/api/reveal', BASE + '/..',
    BASE + '/%2e%2e', BASE + '\\evil', BASE + '\n',
    'http://127.0.0.1:18001', 'https://api-qa.example.invalid:99999/api/reveal',
])
def test_target_url_must_match_environment_before_creating_outputs(setup, url, capsys):
    assert cli.main(args(setup, **{'api-url': url})) == 1
    assert not setup['output'].exists() and not setup['config'].exists()
    assert 'sensitive' not in capsys.readouterr().err


def test_explicit_local_test_environment_allows_loopback_offline_issuance(setup):
    setup['env'].write_text('REVEAL_ENVIRONMENT=test\n')
    assert cli.main(args(setup, **{'api-url': 'http://127.0.0.1:18001'})) == 0
    assert json.loads(setup['output'].read_text())['api_url'] == 'http://127.0.0.1:18001'


@pytest.mark.parametrize('extra', [{'label': 'bad label'}, {'label': 'a' * 65}, {'scope': 'admin:*'}])
def test_invalid_arguments_cannot_expand_scope_or_echo_secrets(setup, extra, capsys):
    assert cli.main(args(setup, **extra)) == 1
    assert not setup['output'].exists() and not setup['config'].exists()
    assert 'admin:*' not in capsys.readouterr().err
