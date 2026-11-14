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
    assert agent_limits.environment_name('literature_papers', 'ceiling') == 'REVEAL_AGENT_JOB_MAX_LITERATURE_PAPERS'
    assert agent_limits.environment_name('max_turns', 'default') == 'REVEAL_AGENT_JOB_DEFAULT_TURNS'


def test_console_beats_environment_which_beats_code(repo):
    environ = {'REVEAL_AGENT_JOB_MAX_BUDGET_USD': '7.5', 'REVEAL_AGENT_JOB_DEFAULT_LITERATURE_PAPERS': '12',
               'REVEAL_AGENT_JOB_MAX_TOOL_CALLS': 'plenty', 'REVEAL_AGENT_JOB_MAX_TURNS': '99999'}
    with repo.transaction() as tx:
        policy = agent_limits.policy(tx, environ)
        assert policy['max_budget_usd'] == {'default': 2.0, 'ceiling': 7.5} and policy['literature_papers']['default'] == 12
        # An unusable environment value is ignored and reported, never applied.
        assert policy['tool_calls'] == {'default': 30, 'ceiling': 300} and policy['max_turns']['ceiling'] == 100
        by_key = {limit['key']: limit for limit in agent_limits.view(tx, environ)['limits']}
        assert by_key['tool_calls']['ceiling'] == {'value': 300, 'source': 'code', 'environment': 'REVEAL_AGENT_JOB_MAX_TOOL_CALLS',
                                                   'ignored_environment_value': True}
        assert by_key['literature_papers']['default']['source'] == 'environment'
        agent_limits.change(tx, {'expected_version': None, 'values': {'literature_papers': {'default': 30}, 'max_budget_usd': {'ceiling': 20}}},
                            'admin@example.org', environ)
        policy = agent_limits.policy(tx, environ)
    assert policy['literature_papers'] == {'default': 30, 'ceiling': 50} and policy['max_budget_usd'] == {'default': 2.0, 'ceiling': 20.0}
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
