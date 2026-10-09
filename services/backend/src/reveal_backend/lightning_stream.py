"""Bounded Anthropic SSE assembly and allowlisted, unfinished audit prose.

The preview is deliberately not a result: JSON structure and evidence references
are validated only after the complete provider message arrives.
"""
import codecs
import json

from .auth import Problem

MAX_STREAM_BYTES = 1_000_000
MAX_EVENT_BYTES = 64_000
MAX_EVENTS = 4096
MAX_CONTENT_BYTES = 128_000
MAX_PREVIEW_CHARS = 24_000


def invalid():
    return Problem(503, 'LIGHTNING_RESPONSE_INVALID', 'The model returned an invalid or oversized response.')


def incomplete():
    return Problem(503, 'LIGHTNING_INCOMPLETE', 'The model stream ended before the audit was complete. Start a new audit to try again.')


class MessageStream:
    """Incremental UTF-8/SSE parser. Neither reasoning nor tool blocks are public."""
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.pending = ''; self.data = []; self.event = ''; self.event_bytes = 0
        self.total_bytes = 0; self.events = 0; self.content_bytes = 0
        self.message = None; self.open_index = None; self.stopped = False; self.delta_seen = False
        self.text = ''

    def feed(self, chunk):
        self.total_bytes += len(chunk)
        if self.total_bytes > MAX_STREAM_BYTES: raise invalid()
        try: self.pending += self.decoder.decode(chunk)
        except UnicodeError: raise invalid() from None
        changed = False
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            if line.endswith('\r'): line = line[:-1]
            self.event_bytes += len(line.encode('utf-8')) + 1
            if self.event_bytes > MAX_EVENT_BYTES: raise invalid()
            if not line:
                if self.data:
                    changed = self._event() or changed
                self.data = []; self.event = ''; self.event_bytes = 0
            elif not line.startswith(':'):
                name, separator, value = line.partition(':')
                if separator and value.startswith(' '): value = value[1:]
                if name == 'data': self.data.append(value)
                elif name == 'event': self.event = value
                # SSE id/retry and future fields have no effect on this attempt.
        if self.event_bytes + len(self.pending.encode('utf-8')) > MAX_EVENT_BYTES: raise invalid()
        return changed

    def _append(self, text):
        if not isinstance(text, str): raise invalid()
        try: self.content_bytes += len(text.encode('utf-8'))
        except UnicodeError: raise invalid() from None
        if self.content_bytes > MAX_CONTENT_BYTES: raise invalid()
        self.message['content'][self.open_index]['text'] += text
        self.text += text
        return bool(text)

    def _event(self):
        self.events += 1
        if self.events > MAX_EVENTS: raise invalid()
        try: value = json.loads('\n'.join(self.data))
        except (ValueError, TypeError): raise invalid() from None
        if not isinstance(value, dict) or not isinstance(value.get('type'), str): raise invalid()
        kind = value['type']
        if self.event and self.event != kind: raise invalid()
        if kind == 'ping': return False
        if kind == 'error':
            raise Problem(503, 'LIGHTNING_PROVIDER_UNAVAILABLE',
                'The model service could not complete this audit. Start a new audit to try again.')
        known = ('message_start', 'content_block_start', 'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop')
        if kind not in known: return False  # Forward-compatible events cannot become preview text.
        if self.stopped: raise invalid()
        if kind == 'message_start':
            message = value.get('message')
            if self.message is not None or not isinstance(message, dict) or message.get('content') != []:
                raise invalid()
            usage = message.get('usage')
            if not isinstance(usage, dict): raise invalid()
            self.message = {key: message[key] for key in ('id', 'type', 'role', 'model', 'stop_sequence') if key in message}
            self.message.update(content=[], usage=dict(usage), stop_reason=None)
            return False
        if self.message is None: raise invalid()
        if kind == 'content_block_start':
            index = value.get('index'); block = value.get('content_block')
            if self.open_index is not None or self.delta_seen or type(index) is not int or index != len(self.message['content']): raise invalid()
            if not isinstance(block, dict) or block.get('type') != 'text': raise invalid()
            self.message['content'].append({'type': 'text', 'text': ''}); self.open_index = index
            return self._append(block.get('text', ''))
        if kind in ('content_block_delta', 'content_block_stop'):
            if self.open_index is None or type(value.get('index')) is not int or value['index'] != self.open_index: raise invalid()
            if kind == 'content_block_stop': self.open_index = None; return False
            delta = value.get('delta')
            if not isinstance(delta, dict) or delta.get('type') != 'text_delta': raise invalid()
            return self._append(delta.get('text'))
        if self.open_index is not None: raise invalid()
        if kind == 'message_delta':
            delta = value.get('delta'); usage = value.get('usage')
            if not isinstance(delta, dict) or not isinstance(usage, dict): raise invalid()
            if 'stop_reason' in delta: self.message['stop_reason'] = delta['stop_reason']
            if 'stop_sequence' in delta: self.message['stop_sequence'] = delta['stop_sequence']
            self.message['usage'].update(usage); self.delta_seen = True
        elif kind == 'message_stop':
            if not self.delta_seen: raise incomplete()
            self.stopped = True
        return False

    def finish(self):
        try: tail = self.decoder.decode(b'', final=True)
        except UnicodeError: raise invalid() from None
        if tail or self.pending.strip() or self.data: raise incomplete()
        if not self.stopped or self.message is None or self.open_index is not None: raise incomplete()
        return self.message


class _PartialJSON:
    """Parse only complete syntax and safe prefixes of strings, including escapes."""
    def __init__(self, text): self.text = text; self.index = 0

    def space(self):
        while self.index < len(self.text) and self.text[self.index] in ' \t\r\n': self.index += 1

    def string(self):
        self.index += 1; output = []
        escapes = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
        while self.index < len(self.text):
            char = self.text[self.index]; self.index += 1
            if char == '"': return ''.join(output), True
            if ord(char) < 32: raise ValueError()
            if char != '\\': output.append(char); continue
            if self.index >= len(self.text): break
            escape = self.text[self.index]; self.index += 1
            if escape in escapes: output.append(escapes[escape]); continue
            if escape != 'u': raise ValueError()
            if len(self.text) - self.index < 4: break
            digits = self.text[self.index:self.index + 4]
            if any(char not in '0123456789abcdefABCDEF' for char in digits): raise ValueError()
            point = int(digits, 16); self.index += 4
            if 0xD800 <= point <= 0xDBFF:
                if len(self.text) - self.index < 6: break
                if self.text[self.index:self.index + 2] != '\\u': raise ValueError()
                digits = self.text[self.index + 2:self.index + 6]
                if any(char not in '0123456789abcdefABCDEF' for char in digits): raise ValueError()
                low = int(digits, 16)
                if not 0xDC00 <= low <= 0xDFFF: raise ValueError()
                point = 0x10000 + ((point - 0xD800) << 10) + low - 0xDC00; self.index += 6
            elif 0xDC00 <= point <= 0xDFFF: raise ValueError()
            output.append(chr(point))
        return ''.join(output), False

    def value(self, depth=0):
        if depth > 16: raise ValueError()
        self.space()
        if self.index >= len(self.text): return None, False
        char = self.text[self.index]
        if char == '"': return self.string()
        if char in '{[':
            mapping = char == '{'; output = {} if mapping else []; self.index += 1; self.space()
            end = '}' if mapping else ']'
            if self.index < len(self.text) and self.text[self.index] == end:
                self.index += 1; return output, True
            while self.index < len(self.text):
                self.space()
                if self.index >= len(self.text): break
                if mapping:
                    if self.text[self.index] != '"': raise ValueError()
                    key, complete = self.string()
                    if not complete: break
                    self.space()
                    if self.index >= len(self.text): break
                    if self.text[self.index] != ':': raise ValueError()
                    self.index += 1
                value, complete = self.value(depth + 1)
                if mapping: output[key] = value
                else: output.append(value)
                if not complete: break
                self.space()
                if self.index >= len(self.text): break
                separator = self.text[self.index]; self.index += 1
                if separator == end: return output, True
                if separator != ',': raise ValueError()
            return output, False
        # Non-string scalars never become visible, but complete ones allow the
        # parser to reach a subsequent allowlisted prose field.
        try: value, consumed = json.JSONDecoder().raw_decode(self.text[self.index:])
        except ValueError: return None, False
        self.index += consumed
        return value, True


def preview(text):
    """Never show raw JSON, references, assessments, unknown fields or model reasoning."""
    try: value, _ = _PartialJSON(text).value()
    except (ValueError, TypeError, RecursionError): return None
    if not isinstance(value, dict): return None
    remaining = MAX_PREVIEW_CHARS
    def prose(item):
        nonlocal remaining
        if not isinstance(item, str): return ''
        # Control characters cannot affect the interface; paragraph breaks stay.
        clean = ''.join(char for char in item if ord(char) >= 32 or char in '\n\t')[:remaining]
        remaining -= len(clean)
        return clean
    result = {'summary': prose(value.get('summary')), 'recommended_direction': prose(value.get('recommended_direction'))}
    observations = value.get('observations')
    result['observations'] = [prose(item.get('text')) for item in observations[:12] if isinstance(item, dict)] if isinstance(observations, list) else []
    for field in ('missing_evidence', 'next_steps', 'limitations'):
        items = value.get(field)
        result[field] = [prose(item) for item in items[:12] if isinstance(item, str)] if isinstance(items, list) else []
    return result if any(result.values()) else None
