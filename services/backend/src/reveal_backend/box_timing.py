"""Bounded numeric observations of a hosted stream; never retain its content.

These are local receipt times, not measurements of model reasoning, network
latency or provider compute. Concurrent tool intervals are unioned separately
from the sum of completed call durations. Provider metrics are copied only when
the terminal event actually reports finite, nonnegative numbers.
"""
import hashlib
import math
import time

from .public_tool_activity import tool_failed


MAX_ACTIVE_TOOLS = 256
MAX_TRACKED_MESSAGES = 4096
MAX_COUNTER = 2 ** 53 - 1
# Claude API list prices in USD per million tokens (5-minute cache writes) for the models REVEAL pins.
# A run's provider-reported total stays authoritative; these only estimate spend it has not reported yet.
MODEL_PRICES_PER_MTOK = {'claude-sonnet-4-6': {'input_tokens': 3.0, 'output_tokens': 15.0,
                                               'cache_creation_input_tokens': 3.75, 'cache_read_input_tokens': 0.30}}
LIFECYCLE = ('message_start', 'message_delta', 'message_stop', 'content_block_start',
             'content_block_delta', 'content_block_stop')
NOTIFICATIONS = ('rate_limit_event', 'tool_progress', 'tool_use_summary', 'auth_status', 'api_retry')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
RESULT_SUBTYPES = frozenset(('success', 'error_during_execution', 'error_max_turns',
                           'error_max_budget_usd', 'error_max_structured_output_retries'))


def number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > MAX_COUNTER or not math.isfinite(value) or integer and not isinstance(value, int):
        return None
    return value


def estimated_cost_usd(model, usage):
    """Price observed token usage at the pinned model's list prices; None for an unpriced model or no usage."""
    prices = MODEL_PRICES_PER_MTOK.get(model)
    if not prices or not isinstance(usage, dict): return None
    counts = {field: number(usage.get(field), integer=True) for field in prices}
    if any(value is None for value in counts.values()): return None
    return round(sum(counts[field] * price for field, price in prices.items()) / 1_000_000, 6)


def provider_metrics(result):
    """Closed projection; unknown usage is null, never a fabricated zero."""
    result = result if isinstance(result, dict) else {}
    usage = result.get('usage')
    tokens = {field: value for field in TOKEN_FIELDS
              if (value := number(usage.get(field), integer=True)) is not None} if isinstance(usage, dict) else {}
    subtype = result.get('subtype')
    return {'duration_ms': number(result.get('duration_ms')),
            'duration_api_ms': number(result.get('duration_api_ms')),
            'cost_usd': number(result.get('total_cost_usd')),
            'num_turns': number(result.get('num_turns'), integer=True),
            'subtype': subtype if isinstance(subtype, str) and subtype in RESULT_SUBTYPES else None,
            'usage': tokens or None}


class RuntimeTiming:
    def __init__(self, *, clock=None, started=None):
        self.clock = clock or time.monotonic
        self.started = self.clock() if started is None else started
        self.io = {name: {'bytes': 0, 'chunks': 0, 'last': None, 'max_gap': 0.0}
                   for name in ('stdout', 'stderr')}
        self.lifecycle = dict.fromkeys(LIFECYCLE, 0)
        self.lifecycle_first = dict.fromkeys(LIFECYCLE)
        self.lifecycle_last = dict.fromkeys(LIFECYCLE)
        self.message_open = None
        self.notifications = dict.fromkeys(NOTIFICATIONS, 0)
        self.event_count = 0
        self.first_event = self.last_event = None
        self.max_event_gap = 0.0
        self.active = {}
        self.active_authoring = set()
        self.started_tools = self.completed_tools = self.failed_tools = self.peak_active = 0
        self.tool_tracking_complete = True
        self.union_started = None
        self.union_seconds = self.completed_seconds = self.longest_completed = 0.0
        # Token usage observed per provider message, so spend can be estimated before (or without) a terminal result.
        self.message_usage = {}
        self.usage_totals = dict.fromkeys(TOKEN_FIELDS, 0)
        self.messages_seen = 0
        self.current_message = None

    def account(self, identity, usage):
        """Fold one observation of a provider message's usage. Within a message the counts only grow."""
        if not isinstance(identity, str) or not identity or len(identity) > 200 or not isinstance(usage, dict):
            return
        entry = self.message_usage.get(identity)
        if entry is None:
            if len(self.message_usage) >= MAX_TRACKED_MESSAGES:
                self.message_usage.pop(next(iter(self.message_usage)))
            entry = self.message_usage[identity] = dict.fromkeys(TOKEN_FIELDS, 0)
            self.messages_seen = self.increment(self.messages_seen)
        for field in TOKEN_FIELDS:
            value = number(usage.get(field), integer=True)
            if value is not None and value > entry[field]:
                self.usage_totals[field] = self.increment(self.usage_totals[field], value - entry[field])
                entry[field] = value

    @staticmethod
    def increment(value, amount=1):
        return min(MAX_COUNTER, value + amount)

    def bytes_received(self, channel, count):
        if channel not in self.io or number(count, integer=True) is None or not count:
            return
        observed = self.clock()
        entry = self.io[channel]
        entry['max_gap'] = max(entry['max_gap'], observed - (entry['last'] if entry['last'] is not None else self.started))
        entry['last'] = observed
        entry['bytes'] = self.increment(entry['bytes'], count)
        entry['chunks'] = self.increment(entry['chunks'])

    def observe(self, event):
        observed = self.clock()
        self.event_count = self.increment(self.event_count)
        self.max_event_gap = max(self.max_event_gap, observed - (self.last_event if self.last_event is not None else self.started))
        self.last_event = observed
        if self.first_event is None:
            self.first_event = observed
        kind = event.get('type')
        if isinstance(kind, str) and kind in self.notifications:
            self.notifications[kind] = self.increment(self.notifications[kind])
        if kind == 'system' and event.get('subtype') == 'api_retry':
            self.notifications['api_retry'] = self.increment(self.notifications['api_retry'])
        if kind == 'stream_event':
            nested = event.get('event')
            subtype = nested.get('type') if isinstance(nested, dict) else None
            if isinstance(subtype, str) and subtype in self.lifecycle:
                self.lifecycle[subtype] = self.increment(self.lifecycle[subtype])
                if self.lifecycle_first[subtype] is None:
                    self.lifecycle_first[subtype] = observed
                self.lifecycle_last[subtype] = observed
                if subtype in ('message_start', 'message_stop'):
                    self.message_open = subtype == 'message_start'
                # Usage numbers only: message_start carries input and cache counts, message_delta the output total.
                if subtype == 'message_start':
                    started = nested.get('message') if isinstance(nested.get('message'), dict) else {}
                    identity = started.get('id')
                    self.current_message = identity if isinstance(identity, str) and identity else f"stream:{self.lifecycle['message_start']}"
                    self.account(self.current_message, started.get('usage'))
                elif subtype == 'message_delta':
                    self.account(self.current_message, nested.get('usage'))
            # The delta's text, thinking, signature and input fragments are never read.
        if kind not in ('assistant', 'user'):
            return
        message = event.get('message')
        if kind == 'assistant' and isinstance(message, dict):
            # Messages that were not streamed still report their usage on the assembled message.
            self.account(message.get('id'), message.get('usage'))
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            subtype = block.get('type')
            identity = block.get('id' if subtype == 'tool_use' else 'tool_use_id')
            if subtype not in ('tool_use', 'tool_result') or not isinstance(identity, str) or len(identity) > 256:
                continue
            identity = hashlib.sha256(identity.encode()).digest()
            if subtype == 'tool_use':
                if identity in self.active:
                    continue
                self.started_tools = self.increment(self.started_tools)
                if len(self.active) >= MAX_ACTIVE_TOOLS:
                    self.tool_tracking_complete = False
                    continue
                if not self.active:
                    self.union_started = observed
                self.active[identity] = observed
                if block.get('name') in ('lint_account', 'write_account_draft', 'mcp__reveal__lint_account', 'mcp__reveal__write_account_draft'):
                    self.active_authoring.add(identity)
                self.peak_active = max(self.peak_active, len(self.active))
            else:
                began = self.active.pop(identity, None)
                if began is None:
                    continue
                elapsed = max(0.0, observed - began)
                self.completed_tools = self.increment(self.completed_tools)
                if tool_failed('lint_account' if identity in self.active_authoring else '', block):
                    self.failed_tools = self.increment(self.failed_tools)
                self.active_authoring.discard(identity)
                self.completed_seconds += elapsed
                self.longest_completed = max(self.longest_completed, elapsed)
                if not self.active:
                    self.union_seconds += max(0.0, observed - self.union_started)
                    self.union_started = None

    def snapshot(self):
        observed = self.clock()
        def seconds(value):
            return round(max(0.0, value), 3)
        streams = {}
        for name, entry in self.io.items():
            silence = observed - (entry['last'] if entry['last'] is not None else self.started)
            streams[name] = {'bytes': entry['bytes'], 'chunks': entry['chunks'],
                'last_received_elapsed_seconds': seconds(entry['last'] - self.started) if entry['last'] is not None else None,
                'silence_seconds': seconds(silence), 'longest_gap_seconds': seconds(max(entry['max_gap'], silence))}
        silence = observed - (self.last_event if self.last_event is not None else self.started)
        union = self.union_seconds + (observed - self.union_started if self.union_started is not None else 0)
        return {'format': 'reveal.runtime-timing/1',
            'measurement': 'Local receipt intervals; gaps do not identify provider, network or reasoning time.',
            'elapsed_seconds': seconds(observed - self.started), 'streams': streams,
            'usage': {'messages': self.messages_seen, **self.usage_totals},
            'stream_events': {'count': self.event_count, 'lifecycle': dict(self.lifecycle),
                'lifecycle_receipts': {name: {
                    'first_elapsed_seconds': seconds(self.lifecycle_first[name] - self.started) if self.lifecycle_first[name] is not None else None,
                    'last_elapsed_seconds': seconds(self.lifecycle_last[name] - self.started) if self.lifecycle_last[name] is not None else None}
                    for name in LIFECYCLE},
                'message_open_observed': self.message_open,
                'notifications': dict(self.notifications),
                'first_received_elapsed_seconds': seconds(self.first_event - self.started) if self.first_event is not None else None,
                'last_received_elapsed_seconds': seconds(self.last_event - self.started) if self.last_event is not None else None,
                'silence_seconds': seconds(silence), 'longest_gap_seconds': seconds(max(self.max_event_gap, silence))},
            'tools': {'started': self.started_tools, 'completed': self.completed_tools, 'failed': self.failed_tools,
                'active': len(self.active), 'peak_active': self.peak_active, 'tracking_complete': self.tool_tracking_complete,
                'active_union_seconds': seconds(union), 'completed_duration_sum_seconds': seconds(self.completed_seconds),
                'longest_completed_seconds': seconds(self.longest_completed),
                'oldest_active_seconds': seconds(observed - min(self.active.values())) if self.active else None}}
