"""Incremental Claude Code stream-json decoder; never publishes thinking blocks. Also the runner's private, gzipped
record of the whole stream (TranscriptWriter), captured under the ledger for admins."""
from collections import deque
import codecs
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
import time
import zlib

from .public_tool_activity import bounded, redact_text, tool_call_payload, tool_result_payload
from .box_timing import RuntimeTiming, provider_metrics

MAX_UNKNOWN_EVENT_TYPES = 8
# Claude CLI/Agent SDK bookkeeping, not assistant messages or new tool calls.
# Tool progress is a repeated heartbeat; summaries duplicate captured results.
# Authentication output can contain sensitive diagnostics and stays private.
PRIVATE_NOTIFICATION_TYPES = frozenset(('rate_limit_event', 'tool_progress', 'tool_use_summary', 'auth_status', 'api_retry'))


class StreamProtocolError(ValueError):
    pass


class SecretFilter:
    """Redact exact credentials even when a stream splits them across chunks."""
    def __init__(self, secrets=()):
        self.secrets = [s.encode() for s in secrets if s]
        self.keep = max((len(s) - 1 for s in self.secrets), default=0)
        self.pending = b''

    def feed(self, chunk, final=False):
        self.pending += chunk
        for secret in self.secrets:
            self.pending = self.pending.replace(secret, b'[REDACTED_CREDENTIAL]')
        cut = len(self.pending) if final else max(0, len(self.pending) - self.keep)
        result, self.pending = self.pending[:cut], self.pending[cut:]
        return result


class ClaudeStream:
    def __init__(self, max_line_bytes=2_000_000, secrets=(), clock=None, timing=None):
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer = ''
        self.max_line_bytes = max_line_bytes
        self.result = None
        self.tools = {}
        self.saw_deltas = False
        self.events = 0
        self.runtime = {}
        self.secrets = tuple(secret for secret in secrets if secret)
        self.clock = clock or time.monotonic
        self.timing = timing if timing is not None else RuntimeTiming()
        self.public_filter = SecretFilter(self.secrets)
        self.public_decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.unknown_event_types = set()
        self.unknown_event_overflow = False

    def public_text(self, text='', final=False):
        raw = self.public_filter.feed(text.encode(), final=final)
        value = self.public_decoder.decode(raw, final=final)
        if final:
            self.public_decoder.reset()
        return [('agent_message', {'text': bounded(redact_text(value, self.secrets), 16000), 'delta': True})] if value else []

    def feed(self, chunk: bytes, final=False):
        self.timing.bytes_received('stdout', len(chunk))
        try:
            self.buffer += self.decoder.decode(chunk, final=final)
        except UnicodeDecodeError as exc:
            raise StreamProtocolError('Invalid UTF-8 in Claude stream') from exc
        if len(self.buffer.encode()) > self.max_line_bytes and '\n' not in self.buffer:
            raise StreamProtocolError('Claude stream line exceeds size limit')
        lines = self.buffer.split('\n')
        self.buffer = lines.pop()
        if final and self.buffer:
            lines.append(self.buffer)
            self.buffer = ''
        events = []
        for line in lines:
            if not line.strip():
                continue
            if len(line.encode()) > self.max_line_bytes:
                raise StreamProtocolError('Claude stream line exceeds size limit')
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StreamProtocolError('Malformed Claude stream JSON') from exc
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise StreamProtocolError('Invalid Claude stream event envelope')
            if self.result is not None:
                raise StreamProtocolError('Claude stream emitted data after terminal result')
            events.extend(self.parse(event))
        self.events += len(events)
        return events

    def parse(self, event):
        self.timing.observe(event)
        kind = event['type']
        if kind == 'stream_event':
            delta = event.get('event', {}).get('delta', {})
            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                self.saw_deltas = True
                return self.public_text(delta['text'])
            return []  # thinking_delta and signature_delta stay private
        if kind in ('assistant', 'user'):
            out = self.public_text(final=True)
            content = event.get('message', {}).get('content', [])
            if not isinstance(content, list):
                return out
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text' and kind == 'assistant' and not self.saw_deltas and isinstance(block.get('text'), str):
                    out.append(('agent_message', {'text': bounded(redact_text(block['text'], self.secrets), 16000)}))
                elif block.get('type') == 'tool_use':
                    call_id = block.get('id')
                    if not isinstance(call_id, str) or len(call_id) > 256 or call_id in self.tools:
                        raise StreamProtocolError('Invalid or duplicate tool call ID')
                    self.tools[call_id] = {'name': block.get('name'), 'input': block.get('input'), 'result': None,
                                           'started_monotonic': self.clock()}
                    out.append(('tool_call', tool_call_payload(call_id, block.get('name'), block.get('input'), self.secrets)))
                elif block.get('type') == 'tool_result':
                    call_id = block.get('tool_use_id')
                    if call_id not in self.tools:
                        raise StreamProtocolError('Tool result has no matching call')
                    if self.tools[call_id]['result'] is not None:
                        raise StreamProtocolError('Duplicate tool result would overwrite captured evidence')
                    self.tools[call_id]['result'] = block
                    call = self.tools[call_id]
                    payload = tool_result_payload(call_id, call['name'], call['input'], block, self.secrets)
                    payload['duration_ms'] = max(0, round((self.clock() - call['started_monotonic']) * 1000))
                    out.append(('tool_result', payload))
            return out
        if kind == 'result':
            self.result = event
            return self.public_text(final=True) + [('agent_completed', {'status': 'failed' if event.get('is_error') else 'succeeded',
                                         'message': 'Claude finished its work.' if not event.get('is_error') else 'Claude stopped with an error.',
                                         'cost_usd': provider_metrics(event)['cost_usd']})]
        if kind == 'system':
            if event.get('subtype') == 'init':
                self.runtime = {key: event[key] for key in ('model', 'claude_code_version', 'session_id', 'permissionMode') if key in event}
                # The tools and MCP servers Claude Code says it has, for the run's recorded configuration.
                tools = event.get('tools')
                if isinstance(tools, list) and len(tools) <= 500 and all(isinstance(name, str) and len(name) <= 200 for name in tools):
                    self.runtime['tools'] = tools
                servers = event.get('mcp_servers')
                if isinstance(servers, list) and len(servers) <= 50:
                    self.runtime['mcp_servers'] = [{key: server.get(key) for key in ('name', 'status')
                                                    if isinstance(server.get(key), str) and len(server[key]) <= 200}
                                                   for server in servers if isinstance(server, dict)]
            return []
        if kind in PRIVATE_NOTIFICATION_TYPES:
            return []
        # Keep unknown payloads (including their names) out of public messages.
        # Fingerprints bound retained memory; repeated notifications cannot flood
        # the public stream even if a future CLI emits many new event types.
        fingerprint = hashlib.sha256(kind.encode()).digest()
        if fingerprint in self.unknown_event_types:
            return []
        if len(self.unknown_event_types) >= MAX_UNKNOWN_EVENT_TYPES:
            if self.unknown_event_overflow:
                return []
            self.unknown_event_overflow = True
            return [('warning', {'code': 'unknown_agent_event_limit',
                                 'message': 'Additional unsupported agent event types were retained privately.'})]
        self.unknown_event_types.add(fingerprint)
        return [('warning', {'code': 'unknown_agent_event', 'message': 'An unsupported agent event was retained privately.'})]

    def finish(self):
        events = self.feed(b'', final=True)
        if self.result is None:
            raise StreamProtocolError('Claude stream ended without a terminal result')
        return events


# Lines the runner adds to a transcript; Claude Code's own event types never start with "reveal_".
TRANSCRIPT_CUT = 'reveal_transcript_cut'
TRANSCRIPT_CLOCK = 'reveal_clock'
# An unfinished line longer than this is written as it is (stderr need not end its lines). Above ClaudeStream's 2 MB
# line limit, so a stream line is never split by a clock line, the cut or the tail's start.
MAX_PENDING_LINE = 4_000_000


class TranscriptWriter:
    """One of Claude Code's output streams, gzipped as it arrives, for the ledger: the run's full transcript.

    The stored file never exceeds `limit` bytes, and never inflates to more than `expanded` bytes. The head is kept
    while it fits in half the limit (measured compressed); after that only a rolling tail of whole lines is kept, and
    on close one cut line, {"type":"reveal_transcript_cut",...}, stands where lines were left out. With a clock, a
    {"type":"reveal_clock","at":...} line is written before a line, at most once a second, saying when it arrived.
    A context manager, so the gzip trailer is written however the run ends."""

    def __init__(self, path, limit, *, expanded, clock=None):
        self.limit, self.expanded, self.clock = limit, expanded, clock
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(descriptor, 'wb')
        self.gzip = gzip.GzipFile(fileobj=self.file, mode='wb', filename='', mtime=0)
        self.head_limit = limit // 2
        # Deflate can grow incompressible bytes by about 0.1%, plus the marker and the gzip trailer.
        self.tail_budget = max(0, limit - self.head_limit - 1024 - (limit - self.head_limit) // 500)
        self.head, self.head_bytes, self.unflushed = True, 0, 0
        self.tail, self.tail_bytes = deque(), 0
        self.pending = b''
        self.second = None
        self.original_bytes = self.original_lines = self.omitted_bytes = self.omitted_lines = 0
        self.stored_bytes = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def write(self, data):
        self.pending += data
        *lines, self.pending = self.pending.split(b'\n')
        for line in lines:
            self.line(line + b'\n')
        if len(self.pending) > MAX_PENDING_LINE:
            self.line(self.pending); self.pending = b''

    def line(self, line):
        if self.clock is not None:
            now = self.clock()
            if int(now) != self.second:
                self.second = int(now)
                at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                self.put(json.dumps({'type': TRANSCRIPT_CLOCK, 'at': at}).encode() + b'\n')
        self.put(line)

    def fits(self, line):
        """Whether the line fits in the head. Bytes not yet flushed are counted at their raw size, never less than
        they compress to; only when that estimate says no is the compressor flushed and the head measured exactly."""
        if self.head_bytes + len(line) > self.expanded - self.tail_budget - 4096: return False
        def room(pending): return self.file.tell() + pending + pending // 500 + 64 <= self.head_limit
        if room(self.unflushed + len(line)): return True
        if self.unflushed:
            self.gzip.flush(zlib.Z_SYNC_FLUSH); self.unflushed = 0
        return room(len(line))

    def put(self, line):
        self.original_bytes += len(line); self.original_lines += 1
        if self.head:
            if self.fits(line):
                self.gzip.write(line); self.head_bytes += len(line); self.unflushed += len(line)
                return
            self.head = False
        self.tail.append(line); self.tail_bytes += len(line)
        while self.tail_bytes > self.tail_budget:
            dropped = self.tail.popleft(); self.tail_bytes -= len(dropped)
            self.omitted_bytes += len(dropped); self.omitted_lines += 1

    def close(self):
        if self.stored_bytes is not None: return
        if self.pending:
            self.put(self.pending); self.pending = b''
        if self.omitted_lines:
            self.gzip.write(json.dumps({'type': TRANSCRIPT_CUT, 'omitted_bytes': self.omitted_bytes,
                                        'omitted_lines': self.omitted_lines, 'original_bytes': self.original_bytes}).encode() + b'\n')
        for line in self.tail:
            self.gzip.write(line)
        self.tail.clear()
        self.gzip.close()
        self.stored_bytes = self.file.tell()
        self.file.close()

    def summary(self):
        """What runtime.json records about this file once it is closed."""
        return {'stored_bytes': self.stored_bytes, 'original_bytes': self.original_bytes, 'cut': self.omitted_lines > 0,
                'omitted_bytes': self.omitted_bytes, 'omitted_lines': self.omitted_lines}


class Inflater:
    """Bounded inflation of a captured transcript, fed its stored bytes in pieces. A file cut short (a runner stopped
    mid-write) inflates as far as it goes: `complete` says whether the gzip trailer was reached. Corrupt bytes, bytes
    after the gzip member, or more than `limit` inflated bytes raise ValueError."""

    def __init__(self, limit):
        self.decompressor, self.limit, self.count = zlib.decompressobj(31), limit, 0

    @property
    def complete(self):
        return self.decompressor.eof

    def feed(self, chunk):
        if not chunk: return b''
        if self.decompressor.eof: raise ValueError('Data follows the gzip member')
        try: data = self.decompressor.decompress(chunk, self.limit - self.count + 1)
        except zlib.error: raise ValueError('Not a readable gzip stream') from None
        self.count += len(data)
        if self.count > self.limit: raise ValueError('The inflated transcript exceeds its limit')
        if self.decompressor.unused_data: raise ValueError('Data follows the gzip member')
        return data


def inflate(data, limit):
    """(inflated bytes, whether the gzip trailer was reached) of one stored transcript. Raises ValueError as Inflater."""
    inflater = Inflater(limit)
    return inflater.feed(data), inflater.complete
