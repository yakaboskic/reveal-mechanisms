"""Incremental Claude Code stream-json decoder; never publishes thinking blocks."""
import codecs
import json


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
    def __init__(self, max_line_bytes=2_000_000):
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer = ''
        self.max_line_bytes = max_line_bytes
        self.result = None
        self.tools = {}
        self.saw_deltas = False
        self.events = 0
        self.runtime = {}

    def feed(self, chunk: bytes, final=False):
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
        kind = event['type']
        if kind == 'stream_event':
            delta = event.get('event', {}).get('delta', {})
            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                self.saw_deltas = True
                return [('agent_message', {'text': delta['text'][:16000], 'delta': True})]
            return []  # thinking_delta and signature_delta stay private
        if kind in ('assistant', 'user'):
            out = []
            content = event.get('message', {}).get('content', [])
            if not isinstance(content, list):
                return out
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text' and kind == 'assistant' and not self.saw_deltas:
                    out.append(('agent_message', {'text': str(block.get('text', ''))[:16000]}))
                elif block.get('type') == 'tool_use':
                    call_id = block.get('id')
                    if not isinstance(call_id, str) or call_id in self.tools:
                        raise StreamProtocolError('Invalid or duplicate tool call ID')
                    self.tools[call_id] = {'name': block.get('name'), 'input': block.get('input'), 'result': None}
                    out.append(('tool_call', {'call_id': call_id, 'tool': block.get('name'), 'message': 'Using an authorized tool'}))
                elif block.get('type') == 'tool_result':
                    call_id = block.get('tool_use_id')
                    if call_id not in self.tools:
                        raise StreamProtocolError('Tool result has no matching call')
                    if self.tools[call_id]['result'] is not None:
                        raise StreamProtocolError('Duplicate tool result would overwrite captured evidence')
                    self.tools[call_id]['result'] = block
                    out.append(('tool_result', {'call_id': call_id, 'tool': self.tools[call_id]['name'],
                                               'status': 'error' if block.get('is_error') else 'completed'}))
            return out
        if kind == 'result':
            self.result = event
            return [('agent_completed', {'status': 'failed' if event.get('is_error') else 'succeeded',
                                         'cost_usd': event.get('total_cost_usd')})]
        if kind == 'system':
            if event.get('subtype') == 'init':
                self.runtime = {key: event[key] for key in ('model', 'claude_code_version', 'session_id', 'permissionMode') if key in event}
            return []
        if kind == 'rate_limit_event':
            return []
        # Unknown upstream events must not become unfiltered public messages.
        return [('warning', {'code': 'unknown_agent_event', 'message': 'An unsupported agent event was retained privately.'})]

    def finish(self):
        events = self.feed(b'', final=True)
        if self.result is None:
            raise StreamProtocolError('Claude stream ended without a terminal result')
        return events
