"""A prebuilt Box toolchain snapshot is only an optimization: the stamped bootstrap decides, offline."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reveal_backend import direct_bootstrap
from reveal_backend.box_adapter import (BoxConfigurationError, BoxExecutionAdapter, CLAUDE_VERSION, TOOLCHAIN_SNAPSHOT,
                                        create_box, toolchain_snapshot)
from reveal_backend.box_lifecycle import BoxLifecycle
from reveal_backend.runtime_config import ROOT
from test_direct_bootstrap import frozen  # noqa: F401 -- a frozen bootstrap descriptor and its handle

spec = importlib.util.spec_from_file_location('build_box_toolchain_snapshot', ROOT/'scripts/build_box_toolchain_snapshot.py')
builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)

INSTALL = ['id reveal-agent >/dev/null 2>&1 || sudo useradd --create-home --uid 1999 --shell /usr/sbin/nologin reveal-agent',
           'sudo apt-get update -qq', 'sudo apt-get install -y -qq python3-venv', 'sudo python3 -m venv /reveal/venv',
           'sudo /reveal/venv/bin/pip -q install PyYAML==6.0.2 linkml==1.11.1 rdflib==7.6.0', 'sudo mkdir -p /reveal/claude',
           'sudo npm install --prefix /reveal/claude --no-audit --no-fund @anthropic-ai/claude-code@' + CLAUDE_VERSION]
FAKES = {
    'sudo': 'exec "$@"',
    'apt-get': 'echo "apt-get $*" >> "$FAKE_LOG"; [ -z "${FAKE_FAIL:-}" ]',
    'id': '[ -f "$FAKE_STATE/user" ]',
    'useradd': 'touch "$FAKE_STATE/user"; echo useradd >> "$FAKE_LOG"',
    'python3': ('[ "$1 $2" = "-m venv" ] || exit 9\nmkdir -p "$3/bin"\nprintf \'#!/bin/sh\\nexit 0\\n\' > "$3/bin/python"\n'
                'printf \'#!/bin/sh\\necho "pip $*" >> "$FAKE_LOG"\\n\' > "$3/bin/pip"\nchmod +x "$3/bin/python" "$3/bin/pip"\n'
                'echo venv >> "$FAKE_LOG"'),
    'npm': ('prefix=$3; for value in "$@"; do package=$value; done; version=${package##*@}\nmkdir -p "$prefix/node_modules/.bin"\n'
            'printf \'#!/bin/sh\\necho "%s (Claude Code)"\\n\' "${FAKE_CLAUDE:-$version}" > "$prefix/node_modules/.bin/claude"\n'
            'chmod +x "$prefix/node_modules/.bin/claude"\necho npm >> "$FAKE_LOG"'),
}


@pytest.fixture(scope='module')
def fakes(tmp_path_factory):
    bin_dir = tmp_path_factory.mktemp('bin')
    for name, body in FAKES.items():
        (bin_dir/name).write_text('#!/bin/sh\n' + body + '\n'); (bin_dir/name).chmod(0o755)
    return bin_dir


def boot(bin_dir, tmp_path, version=CLAUDE_VERSION, **environment):
    """Run the real bootstrap script against a scratch /reveal with fake system commands."""
    state = tmp_path/'state'; state.mkdir(exist_ok=True)
    script = tmp_path/'bootstrap.sh'
    script.write_text(BoxExecutionAdapter.bootstrap_script(version, unpack=False).replace('/reveal', str(tmp_path/'reveal')))
    log = tmp_path/'commands.log'; log.write_text('')
    result = subprocess.run(['sh', str(script)], capture_output=True, text=True, timeout=60,
        env={'PATH': str(bin_dir) + ':/usr/bin:/bin', 'FAKE_LOG': str(log), 'FAKE_STATE': str(state), **environment})
    return result, log.read_text().split('\n')


def test_bootstrap_installs_once_then_reuses_only_an_exact_stamped_toolchain(fakes, tmp_path):
    first, commands = boot(fakes, tmp_path)
    assert first.returncode == 0, first.stderr
    assert 'toolchain=installed' in first.stdout.split('\n') and first.stdout.strip().endswith(CLAUDE_VERSION + ' (Claude Code)')
    assert [c.split()[0] for c in commands if c] == ['useradd', 'apt-get', 'apt-get', 'venv', 'pip', 'npm']
    stamp = (tmp_path/'reveal/toolchain.sha256').read_text()
    assert stamp == BoxExecutionAdapter.toolchain_stamp(CLAUDE_VERSION)
    assert stat.S_IMODE((tmp_path/'reveal/state').stat().st_mode) == 0o755
    # What a Box made from the snapshot sees: nothing is downloaded or installed.
    again, commands = boot(fakes, tmp_path)
    assert again.returncode == 0 and 'toolchain=snapshot' in again.stdout.split('\n') and not any(commands)
    assert direct_bootstrap.toolchain(again.stdout) == 'snapshot' and direct_bootstrap.toolchain(first.stdout) == 'installed'


@pytest.mark.parametrize('change', ['version', 'binary', 'python', 'stamp'])
def test_any_mismatch_reinstalls_cleanly_instead_of_mixing(fakes, tmp_path, change):
    assert boot(fakes, tmp_path)[0].returncode == 0
    leftover = tmp_path/'reveal/venv/stale-package'; leftover.write_text('old')
    version, environment = CLAUDE_VERSION, {}
    if change == 'version': version = '9.9.9'
    if change == 'binary': (tmp_path/'reveal/claude/node_modules/.bin/claude').write_text('#!/bin/sh\necho "0.0.1 (Claude Code)"\n')
    if change == 'python': (tmp_path/'reveal/venv/bin/python').unlink()
    if change == 'stamp': (tmp_path/'reveal/toolchain.sha256').write_text('0' * 64)
    result, commands = boot(fakes, tmp_path, version, **environment)
    assert result.returncode == 0, result.stderr
    assert 'toolchain=installed' in result.stdout.split('\n') and 'apt-get update -qq' in commands and 'npm' in commands
    assert not leftover.exists()
    assert (tmp_path/'reveal/toolchain.sha256').read_text() == BoxExecutionAdapter.toolchain_stamp(version)


def test_failed_install_writes_no_stamp(fakes, tmp_path):
    result, _ = boot(fakes, tmp_path, FAKE_FAIL='1')
    assert result.returncode != 0 and not (tmp_path/'reveal/toolchain.sha256').exists()


def test_install_recipe_is_unchanged_and_the_stamp_binds_it():
    script = BoxExecutionAdapter.bootstrap_script(CLAUDE_VERSION, unpack=False)
    assert BoxExecutionAdapter.toolchain_script(CLAUDE_VERSION).splitlines() == INSTALL
    assert '\n'.join(INSTALL) in script and script == BoxExecutionAdapter.bootstrap_script(CLAUDE_VERSION, unpack=False)
    assert BoxExecutionAdapter.toolchain_stamp(CLAUDE_VERSION) in script
    assert BoxExecutionAdapter.toolchain_stamp('9.9.9') != BoxExecutionAdapter.toolchain_stamp(CLAUDE_VERSION)
    assert BoxExecutionAdapter.bootstrap_script(CLAUDE_VERSION).startswith(
        'set -eu\nsudo mkdir -p /reveal/state\nsudo tar -xzf /tmp/reveal-bundle.tgz -C /reveal\nsudo mv /tmp/reveal-request.json /reveal/request.json\n')
    with pytest.raises(BoxConfigurationError): BoxExecutionAdapter.bootstrap_script('2.1; rm -rf /')


class Factory:
    def __init__(self, refusal=None):
        self.calls = []; self.refusal = refusal
    async def create(self, **kwargs):
        self.calls.append(('create', kwargs)); return SimpleNamespace(id='fresh', aclose=AsyncMock())
    async def from_snapshot(self, snapshot, **kwargs):
        self.calls.append(('from_snapshot', snapshot, kwargs))
        if self.refusal: raise self.refusal
        return SimpleNamespace(id='restored', aclose=AsyncMock())


def lifecycle(factory, **environment):
    return BoxLifecycle(ROOT, environ={'ANTHROPIC_API_KEY': 'a', 'UPSTASH_BOX_API_KEY': 'b', **environment}, box_factory=factory)


def test_create_once_defaults_to_a_fresh_node_box_and_uses_a_configured_snapshot():
    factory = Factory()
    handle = asyncio.run(lifecycle(factory).create_once('job', 2))
    assert handle['box_id'] == 'fresh' and factory.calls[0][0] == 'create' and factory.calls[0][1]['runtime'] == 'node'
    labels = factory.calls[0][1]['labels']
    factory = Factory()
    handle = asyncio.run(lifecycle(factory, **{TOOLCHAIN_SNAPSHOT: ' snap_01.a '}).create_once('job', 2))
    assert handle['box_id'] == 'restored'
    assert factory.calls == [('from_snapshot', 'snap_01.a', {'api_key': 'b', 'labels': labels})]


def test_only_a_refused_snapshot_post_may_fall_back_to_one_fresh_box():
    from upstash_box.errors import BoxError
    factory = Factory(BoxError('snapshot not found', 404))
    assert asyncio.run(create_box(factory, {'UPSTASH_BOX_API_KEY': 'b', TOOLCHAIN_SNAPSHOT: 'gone'}, ['reveal'])).id == 'fresh'
    assert [call[0] for call in factory.calls] == ['from_snapshot', 'create']
    # Anything else may already have created a Box: it reaches creation recovery, never a second POST.
    for failure in (BoxError('Box creation from snapshot timed out'), BoxError('server', 500), BoxError('busy', 429), OSError('reset')):
        factory = Factory(failure)
        with pytest.raises(type(failure)):
            asyncio.run(create_box(factory, {'UPSTASH_BOX_API_KEY': 'b', TOOLCHAIN_SNAPSHOT: 'snap'}, ['reveal']))
        assert [call[0] for call in factory.calls] == ['from_snapshot']


def test_invalid_snapshot_setting_fails_before_any_post():
    factory = Factory()
    for value in ('../x', 'a b', '-x', 'x' * 129):
        with pytest.raises(BoxConfigurationError): toolchain_snapshot({TOOLCHAIN_SNAPSHOT: value})
        with pytest.raises(BoxConfigurationError):
            asyncio.run(create_box(factory, {'UPSTASH_BOX_API_KEY': 'b', TOOLCHAIN_SNAPSHOT: value}, ['reveal']))
    assert factory.calls == [] and toolchain_snapshot({}) is None and toolchain_snapshot({TOOLCHAIN_SNAPSHOT: ''}) is None


def test_legacy_execute_creates_through_the_same_snapshot_rule(tmp_path):
    from reveal_backend.agent_execution import ExecutionRequest
    source = tmp_path/'paragraph.json'; source.write_text('{"format":"reveal.paragraph-input/1","account_document":{},"allowed_citations":[]}')
    factory = Factory(RuntimeError('stop after creation'))
    adapter = BoxExecutionAdapter(ROOT, environ={'ANTHROPIC_API_KEY': 'a', 'UPSTASH_BOX_API_KEY': 'b', TOOLCHAIN_SNAPSHOT: 'snap'},
                                  box_factory=factory)
    request = ExecutionRequest('job', 1, 'paragraph', source, tmp_path/'out', timeout_seconds=60, max_turns=2)
    async def nothing(*args): return False
    with pytest.raises(RuntimeError, match='stop after creation'):
        asyncio.run(adapter.execute(request, nothing, nothing, nothing))
    assert factory.calls[0][0] == 'from_snapshot' and factory.calls[0][2]['labels'][0] == 'reveal' and len(factory.calls) == 1


def clean_facts(**changes):
    expected = builder.plan()
    facts = {'reveal': list(builder.EXPECTED), 'state': [], 'modes': [0o755, 0o755], 'owners': [0, 0], 'temporary': [],
             'credentials': [], 'home': [], 'stamp': expected['stamp'], 'claude': CLAUDE_VERSION + ' (Claude Code)',
             'python': ['linkml==1.11.1'], 'npm_lock_sha256': 'f' * 64}
    return {**facts, **changes}


class BuilderBox:
    def __init__(self, facts):
        self.id = 'builder'; self.facts = facts; self.writes = []; self.commands = []
        self.files = SimpleNamespace(write=AsyncMock(side_effect=lambda **kw: self.writes.append(kw)))
        self.exec = SimpleNamespace(command=AsyncMock(side_effect=self.command))
        self.snapshot = AsyncMock(return_value=SimpleNamespace(id='snap-1', name='n', size_bytes=1, status='ready'))
        self.delete = AsyncMock(); self.aclose = AsyncMock()
        self.update_network_policy = AsyncMock(side_effect=AssertionError('the builder never narrows egress'))
    async def command(self, value):
        self.commands.append(value)
        return SimpleNamespace(status='completed', result=json.dumps(self.facts) if builder.CHECK in value else 'toolchain=installed')


def test_builder_runs_the_job_recipe_on_a_clean_box_and_snapshots_it():
    box = BuilderBox(clean_facts()); factory = SimpleNamespace(create=AsyncMock(return_value=box))
    result = asyncio.run(builder.build({'UPSTASH_BOX_API_KEY': 'key'}, factory, emit=lambda line: None))
    expected = builder.plan()
    assert factory.create.call_args.kwargs == {'runtime': 'node', 'api_key': 'key', 'labels': expected['labels']}
    assert box.writes[0] == {'path': builder.BUILD, 'content': expected['script']}
    assert expected['script'].startswith(BoxExecutionAdapter.bootstrap_script(CLAUDE_VERSION, unpack=False))
    assert [write['path'] for write in box.writes] == [builder.BUILD, builder.CHECK]
    assert not any(word in write['path'] for write in box.writes for word in ('credential', 'bundle', 'request'))
    assert 'credential' not in box.writes[0]['content'] and 'update_network_policy' not in box.writes[0]['content']
    box.snapshot.assert_awaited_once_with(name=expected['name']); box.update_network_policy.assert_not_awaited()
    box.delete.assert_not_awaited(); box.aclose.assert_awaited_once()
    assert result['snapshot_id'] == 'snap-1' and result['stamp'] == expected['stamp'] and TOOLCHAIN_SNAPSHOT in result['next']


@pytest.mark.parametrize('change', [{'state': ['bootstrap-ready']}, {'reveal': builder.EXPECTED + ['credentials.json']},
    {'credentials': ['/reveal/credentials.json']}, {'temporary': ['/tmp/reveal-request.json']}, {'modes': [0o777, 0o755]},
    {'home': ['.claude']}, {'stamp': '0' * 64}, {'claude': '0.0.1 (Claude Code)'}, {'npm_lock_sha256': None}])
def test_builder_refuses_to_snapshot_an_unclean_box_and_deletes_it(change):
    box = BuilderBox(clean_facts(**change)); factory = SimpleNamespace(create=AsyncMock(return_value=box))
    with pytest.raises(RuntimeError, match='not a clean toolchain'):
        asyncio.run(builder.build({'UPSTASH_BOX_API_KEY': 'key'}, factory, emit=lambda line: None))
    box.snapshot.assert_not_awaited(); box.delete.assert_awaited_once(); box.aclose.assert_awaited_once()


def test_builder_dry_run_and_missing_key_call_nothing(capsys, monkeypatch):
    monkeypatch.delenv('UPSTASH_BOX_API_KEY', raising=False)
    factory = SimpleNamespace(create=AsyncMock(side_effect=AssertionError('no Box')))
    assert builder.main(['--dry-run'], factory=factory) == 0
    assert json.loads(capsys.readouterr().out)['stamp'] == BoxExecutionAdapter.toolchain_stamp(CLAUDE_VERSION)
    assert builder.main([], factory=factory) == 2
    factory.create.assert_not_awaited()
    assert os.access(ROOT/'scripts/build_box_toolchain_snapshot.py', os.R_OK)


def test_prepared_handle_records_which_toolchain_the_stamped_bootstrap_used(frozen, monkeypatch):
    adapter, storage, descriptor, handle, _ = frozen
    box = SimpleNamespace(files=SimpleNamespace(write=AsyncMock()), aclose=AsyncMock(), update_network_policy=AsyncMock())
    adapter.connect = AsyncMock(return_value=box)
    async def command(_box, value):
        return 'toolchain=snapshot\n' + CLAUDE_VERSION + ' (Claude Code)\n' if value == 'sh /tmp/reveal-bootstrap.sh' else ''
    adapter.command = AsyncMock(side_effect=command)
    monkeypatch.setattr(direct_bootstrap, 'remote_session', AsyncMock(return_value={'fingerprint': descriptor['fingerprint']}))
    prepared = asyncio.run(adapter.prepare_from_store(descriptor, handle, storage))
    assert prepared['phase'] == 'prepared' and prepared['toolchain'] == 'snapshot'
    scripts = [call.kwargs['content'] for call in box.files.write.call_args_list if call.kwargs['path'] == '/tmp/reveal-bootstrap.sh']
    assert scripts == [adapter.bootstrap_script(descriptor['config']['claude_version'], unpack=False)]
    assert direct_bootstrap.toolchain('') is None and direct_bootstrap.toolchain('toolchain=other') is None
