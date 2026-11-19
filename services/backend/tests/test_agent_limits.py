"""Operator limits for general agent jobs: console, then environment, then code."""
import pytest

from reveal_backend import agent_limits, box_agent
from reveal_backend.repository import Conflict, Repository


@pytest.fixture
def repo(tmp_path):
    repository = Repository(str(tmp_path / 'limits.sqlite')); repository.migrate()
    return repository


def test_code_values_apply_when_nothing_is_set(repo):
    with repo.read_transaction() as tx:
        assert agent_limits.policy(tx, {}) == box_agent.default_policy()
        view = agent_limits.view(tx, {})
    assert view['version'] is None and view['updated_by'] is None and view['history'] == []
    assert {limit['key'] for limit in view['limits']} == set(box_agent.LIMITS) == set(agent_limits.LABELS)
    assert all(limit[field]['source'] == 'code' for limit in view['limits'] for field in agent_limits.FIELDS)
    box_agent.validate_policy(box_agent.default_policy())


def test_environment_names_follow_the_existing_budget_cap():
    assert agent_limits.environment_name('max_budget_usd', 'ceiling') == 'REVEAL_AGENT_JOB_MAX_BUDGET_USD'
    assert agent_limits.environment_name('max_budget_usd', 'default') == 'REVEAL_AGENT_JOB_DEFAULT_BUDGET_USD'
    assert agent_limits.environment_name('tool_calls', 'ceiling') == 'REVEAL_AGENT_JOB_MAX_TOOL_CALLS'
    assert agent_limits.environment_name('max_turns', 'default') == 'REVEAL_AGENT_JOB_DEFAULT_TURNS'


def test_console_beats_environment_which_beats_code(repo):
    environ = {'REVEAL_AGENT_JOB_MAX_BUDGET_USD': '7.5', 'REVEAL_AGENT_JOB_DEFAULT_TIMEOUT_SECONDS': '1200',
               'REVEAL_AGENT_JOB_MAX_TOOL_CALLS': 'plenty', 'REVEAL_AGENT_JOB_MAX_TURNS': '99999'}
    with repo.transaction() as tx:
        policy = agent_limits.policy(tx, environ)
        assert policy['max_budget_usd'] == {'default': 2.0, 'ceiling': 7.5} and policy['timeout_seconds']['default'] == 1200
        # An unusable environment value is ignored and reported, never applied.
        assert policy['tool_calls'] == {'default': 30, 'ceiling': 300} and policy['max_turns']['ceiling'] == 100
        by_key = {limit['key']: limit for limit in agent_limits.view(tx, environ)['limits']}
        assert by_key['tool_calls']['ceiling'] == {'value': 300, 'source': 'code', 'environment': 'REVEAL_AGENT_JOB_MAX_TOOL_CALLS',
                                                   'ignored_environment_value': True}
        assert by_key['timeout_seconds']['default']['source'] == 'environment'
        agent_limits.change(tx, {'expected_version': None, 'values': {'timeout_seconds': {'default': 1800}, 'max_budget_usd': {'ceiling': 20}}},
                            'admin@example.org', environ)
        policy = agent_limits.policy(tx, environ)
    assert policy['timeout_seconds'] == {'default': 1800, 'ceiling': 3600} and policy['max_budget_usd'] == {'default': 2.0, 'ceiling': 20.0}
    assert agent_limits.budget_ceiling(repo, environ) == 20.0
    box_agent.validate_policy(policy)


def test_a_ceiling_below_the_default_lowers_the_default(repo):
    with repo.transaction() as tx:
        assert agent_limits.policy(tx, {'REVEAL_AGENT_JOB_MAX_BUDGET_USD': '0.5'})['max_budget_usd'] == {'default': 0.5, 'ceiling': 0.5}
        view = agent_limits.change(tx, {'expected_version': None, 'values': {'tool_calls': {'ceiling': 10}}}, 'admin@example.org', {})
        calls = next(limit for limit in view['limits'] if limit['key'] == 'tool_calls')
        assert calls['default'] == {'value': 10, 'source': 'code', 'environment': 'REVEAL_AGENT_JOB_DEFAULT_TOOL_CALLS', 'lowered_to_ceiling': True}
        # A console default above the resulting ceiling is a mistake to refuse, not to lower silently.
        with pytest.raises(agent_limits.LimitsError, match='above its ceiling 10'):
            agent_limits.change(tx, {'expected_version': 1, 'values': {'tool_calls': {'default': 20, 'ceiling': 10}}}, 'admin@example.org', {})


def test_a_change_replaces_the_console_values_records_who_and_is_fenced(repo):
    with repo.transaction() as tx:
        agent_limits.change(tx, {'expected_version': None, 'values': {'max_turns': {'default': 80, 'ceiling': 400}}}, 'first@example.org', {})
    with repo.transaction() as tx:
        with pytest.raises(Conflict):
            agent_limits.change(tx, {'expected_version': None, 'values': {}}, 'late@example.org', {})
        view = agent_limits.change(tx, {'expected_version': 1, 'values': {'max_turns': {'ceiling': 400, 'default': None}}}, 'second@example.org', {})
    assert view['version'] == 2 and view['updated_by'] == 'second@example.org'
    assert view['history'][0] == {'at': view['updated_at'], 'by': 'second@example.org', 'changes': {'max_turns': {'default': [80, None]}}}
    assert view['history'][1]['by'] == 'first@example.org'
    turns = next(limit for limit in view['limits'] if limit['key'] == 'max_turns')
    assert turns['console'] == {'default': None, 'ceiling': 400} and turns['default']['value'] == 50
    with repo.transaction() as tx:
        # Saving the same values writes nothing.
        assert agent_limits.change(tx, {'expected_version': 2, 'values': {'max_turns': {'ceiling': 400}}}, 'third@example.org', {})['version'] == 2
        for body in (None, {'values': []}, {'expected_version': 2, 'values': {}, 'force': True}):
            with pytest.raises(agent_limits.LimitsError):
                agent_limits.change(tx, body, 'x', {})


def test_history_is_bounded_and_a_damaged_record_is_never_applied(repo):
    with repo.transaction() as tx:
        version = None
        for turns in range(1, agent_limits.MAX_HISTORY + 6):
            version = agent_limits.change(tx, {'expected_version': version, 'values': {'max_turns': {'default': turns}}}, 'admin@example.org', {})['version']
        assert len(agent_limits.view(tx, {})['history']) == agent_limits.MAX_HISTORY
        tx.put(agent_limits.KIND, agent_limits.IDENTITY, agent_limits.OWNER,
               {'format': agent_limits.FORMAT, 'values': {'max_turns': {'default': 10_000, 'ceiling': 'many'}, 'unknown': {'default': 1}},
                'history': 7})
        assert agent_limits.policy(tx, {}) == box_agent.default_policy()
        # A damaged history does not take the console down, and the next save repairs the record.
        damaged = agent_limits.view(tx, {})
        assert damaged['history'] == []
        repaired = agent_limits.change(tx, {'expected_version': damaged['version'], 'values': {'max_turns': {'default': 60}}}, 'admin@example.org', {})
        assert len(repaired['history']) == 1 and agent_limits.policy(tx, {})['max_turns']['default'] == 60


def test_a_saved_record_naming_a_removed_limit_is_ignored_and_its_history_kept(repo):
    # The console saved literature_papers before the paper limits were removed; the record is read, not rewritten.
    with repo.transaction() as tx:
        tx.put(agent_limits.KIND, agent_limits.IDENTITY, agent_limits.OWNER, {
            'format': agent_limits.FORMAT, 'updated_at': '2026-11-12T20:29:03Z', 'updated_by': 'a@b.org',
            'values': {'literature_papers': {'default': 12}, 'max_turns': {'ceiling': 400}},
            'history': [{'at': '2026-11-12T20:29:03Z', 'by': 'a@b.org',
                         'changes': {'literature_papers': {'default': [None, 12]}, 'max_turns': {'ceiling': [None, 400]}}}]})
    with repo.transaction() as tx:
        policy = agent_limits.policy(tx, {})
        assert set(policy) == set(box_agent.LIMITS) and policy['max_turns']['ceiling'] == 400
        box_agent.validate_policy(policy)
        view = agent_limits.view(tx, {})
        assert [limit['key'] for limit in view['limits']] == list(box_agent.LIMITS)
        assert view['history'][0]['changes']['literature_papers'] == {'default': [None, 12]}
        changed = agent_limits.change(tx, {'expected_version': view['version'], 'values': {'max_turns': {'ceiling': 400}, 'tool_calls': {'default': 40}}},
                                      'b@b.org', {})
    # The next change records only what it changed, and the removed limit's value is no longer kept.
    assert changed['history'][0]['changes'] == {'tool_calls': {'default': [None, 40]}}
    with repo.read_transaction() as tx:
        assert set(tx.get(agent_limits.KIND, agent_limits.IDENTITY)['data']['values']) == {'max_turns', 'tool_calls'}


def test_output_tokens_per_response_is_a_limit_with_its_own_default_ceiling_and_range(repo):
    spec = box_agent.LIMITS['max_output_tokens']
    assert (spec['type'], spec['minimum'], spec['maximum'], spec['default'], spec['ceiling']) == (int, 4096, 128_000, 128_000, 128_000)
    assert agent_limits.environment_name('max_output_tokens', 'default') == 'REVEAL_AGENT_JOB_DEFAULT_OUTPUT_TOKENS'
    assert agent_limits.environment_name('max_output_tokens', 'ceiling') == 'REVEAL_AGENT_JOB_MAX_OUTPUT_TOKENS'
    with repo.read_transaction() as tx:
        row = next(limit for limit in agent_limits.view(tx, {})['limits'] if limit['key'] == 'max_output_tokens')
    assert (row['label'], row['unit'], row['type'], row['minimum'], row['maximum']) == ('Output per response', 'tokens', 'integer', 4096, 128_000)
    assert 'CLAUDE_CODE_MAX_OUTPUT_TOKENS' in row['description']
    assert row['default']['value'] == 128_000 and row['ceiling']['value'] == 128_000
    # The environment sets it as it does the others, and an out-of-range value is ignored.
    with repo.read_transaction() as tx:
        assert agent_limits.policy(tx, {'REVEAL_AGENT_JOB_DEFAULT_OUTPUT_TOKENS': '16000', 'REVEAL_AGENT_JOB_MAX_OUTPUT_TOKENS': '200000'}
                                   )['max_output_tokens'] == {'default': 16_000, 'ceiling': 128_000}
    with repo.transaction() as tx:
        for value in (4095, 128_001, 64_000.5):
            with pytest.raises(agent_limits.LimitsError, match='Output per response ceiling must be a whole number from 4096 to 128000'):
                agent_limits.change(tx, {'expected_version': None, 'values': {'max_output_tokens': {'ceiling': value}}}, 'admin@example.org', {})


def test_a_console_change_to_output_tokens_reaches_the_frozen_input_and_bounds_a_request(repo):
    body = {'kind': 'agent', 'prompt': 'x'}
    with repo.transaction() as tx:
        agent_limits.change(tx, {'expected_version': None, 'values': {'max_output_tokens': {'default': 48_000, 'ceiling': 64_000}}},
                            'admin@example.org', {})
    with repo.read_transaction() as tx:
        policy = agent_limits.policy(tx, {})
    assert agent_limits.ceiling(repo, 'max_output_tokens', {}) == 64_000
    assert box_agent.freeze_input(body, policy=policy)['limits']['max_output_tokens'] == 48_000
    assert box_agent.freeze_input({**body, 'limits': {'max_output_tokens': 64_000}}, policy=policy)['limits']['max_output_tokens'] == 64_000
    assert box_agent.freeze_input({**body, 'limits': {'max_output_tokens': 8000}}, policy=policy)['limits']['max_output_tokens'] == 8000
    with pytest.raises(box_agent.AgentInputError, match='limits.max_output_tokens must be a whole number from 4096 to 64000'):
        box_agent.freeze_input({**body, 'limits': {'max_output_tokens': 64_001}}, policy=policy)
    # An account job gets the default too: only run time and turns default to their ceiling there.
    from test_agent_jobs import fake_account
    account = box_agent.freeze_input({**body, 'deliverable': 'scientific_account', 'question': 'Why?'}, policy=policy, account_builder=fake_account)
    assert account['limits']['max_output_tokens'] == 48_000
