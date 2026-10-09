"""Streamed token usage, its estimated price, and the spend guidance authoring tools return."""
import json

from reveal_backend.box_research import AuthoringTiming
from reveal_backend.box_timing import RuntimeTiming, estimated_cost_usd
from reveal_backend.job_metrics import agent_metrics


def streamed(timing, identity, start, output):
    timing.observe({'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'id': identity, 'usage': {**start, 'output_tokens': 1}}}})
    timing.observe({'type': 'stream_event', 'event': {'type': 'message_delta', 'usage': {'output_tokens': output}}})
    # Claude Code also emits the assembled message; the same usage must not be counted twice.
    timing.observe({'type': 'assistant', 'message': {'id': identity, 'usage': {**start, 'output_tokens': output}, 'content': []}})
    timing.observe({'type': 'stream_event', 'event': {'type': 'message_stop'}})


def test_usage_counts_each_provider_message_once_streamed_or_not():
    timing = RuntimeTiming(clock=lambda: 10.0, started=0.0)
    streamed(timing, 'msg_1', {'input_tokens': 5, 'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': 2000}, 300)
    timing.observe({'type': 'assistant', 'message': {'id': 'msg_2', 'content': [], 'usage': {
        'input_tokens': 3, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3000, 'output_tokens': 50}}})
    assert timing.snapshot()['usage'] == {'messages': 2, 'input_tokens': 8, 'output_tokens': 350,
                                          'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': 5000}


def test_malformed_usage_is_ignored_without_failing_the_stream():
    timing = RuntimeTiming(clock=lambda: 1.0, started=0.0)
    for event in ({'type': 'stream_event', 'event': {'type': 'message_start', 'message': 'text'}},
                  {'type': 'stream_event', 'event': {'type': 'message_delta', 'usage': {'output_tokens': -4}}},
                  {'type': 'assistant', 'message': {'id': 'x' * 500, 'usage': {'output_tokens': 9}}},
                  {'type': 'assistant', 'message': {'id': 'msg', 'usage': {'output_tokens': True, 'input_tokens': '7'}}},
                  {'type': 'assistant', 'message': {'id': 'msg', 'usage': 'none'}}):
        timing.observe(event)
    usage = timing.snapshot()['usage']
    assert usage['output_tokens'] == usage['input_tokens'] == 0


def test_estimate_matches_the_provider_bill_of_a_captured_statement_run():
    # A QA statement run: the provider reported $0.165738 for exactly these tokens.
    usage = {'input_tokens': 5, 'output_tokens': 3824, 'cache_creation_input_tokens': 26314, 'cache_read_input_tokens': 32285}
    assert estimated_cost_usd('claude-sonnet-4-6', usage) == 0.165738
    assert estimated_cost_usd('unpriced-model', usage) is None
    assert estimated_cost_usd('claude-sonnet-4-6', {'output_tokens': 1}) is None


def test_a_killed_run_is_priced_from_its_streamed_usage():
    usage = {'messages': 3, 'input_tokens': 5, 'output_tokens': 3824, 'cache_creation_input_tokens': 26314, 'cache_read_input_tokens': 32285}
    agent = agent_metrics({'model': 'claude-sonnet-4-6', 'execution_limits': {'max_budget_usd': 1},
                           'completion': {'status': 'failed', 'cost_usd': None, 'timing': {'usage': usage}}})
    assert (agent['cost_usd'], agent['estimated_cost_usd'], agent['cost_source']) == (None, 0.165738, 'estimated')
    assert agent['budget_used'] == 0.1657 and agent['tokens']['output'] == 3824


def authoring(tmp_path, usage):
    if usage is not None:
        (tmp_path / 'runtime.json').write_text(json.dumps({'model': 'claude-sonnet-4-6', 'execution_limits': {'max_budget_usd': 5},
                                                           'timing_checkpoint': {'timing': {'usage': usage}}}))
    timing = AuthoringTiming(tmp_path / 'ledger/authoring-timing.json', 'lint_account', 100, clock=lambda: 10)
    return timing.feedback({'content': [{'type': 'text', 'text': 'Lint report.'}]}, 70)


def test_authoring_tools_warn_before_the_dollar_cap_stops_the_run(tmp_path):
    # 200k output tokens alone price at $3.00: 60% of a $5 cap.
    result = authoring(tmp_path, {'messages': 20, 'input_tokens': 0, 'output_tokens': 200_000,
                                  'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0})
    budget = result['structuredContent']['execution_budget']
    assert budget['estimated_spend_usd'] == 3.0 and budget['spend_cap_usd'] == 5
    assert 'Execution time remaining: 60.000 seconds' in result['content'][-1]['text']
    assert 'Estimated model spend so far: $3.00 of the $5.00 execution cap' in result['content'][-1]['text']


def test_low_or_unknown_spend_adds_no_warning(tmp_path):
    low = authoring(tmp_path, {'messages': 2, 'input_tokens': 0, 'output_tokens': 2000,
                               'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0})
    assert low['structuredContent']['execution_budget']['estimated_spend_usd'] == 0.03
    assert 'Estimated model spend' not in low['content'][-1]['text']
    (tmp_path / 'unpriced').mkdir()
    unknown = authoring(tmp_path / 'unpriced', None)
    assert unknown['structuredContent']['execution_budget'] == {'remaining_seconds': 60, 'authoring_call_elapsed_seconds': 0}
