"""One agent job's transcript for admins: Claude Code's captured stream as readable turns, a page at a time.

The runner writes Claude Code's stream and stderr, gzipped, under the ledger (box_stream.TranscriptWriter), and the
capture keeps them with the attempt. This read is under the admin_content purpose, like the run view (admin_run), and
writes one admin_content_read record per page before anything is returned.

Whole assistant and user events are what the turns are made of. Partial-message deltas (--include-partial-messages)
repeat them, and are used only for a message Claude Code never completed: the run stopped mid-message, or the message
ended inside a cut. Text and tool input are redacted as admin_run redacts the response, then capped.
"""
import json

from .admin_jobs import redact
from .admin_run import agent_job, attempt_files, latest_attempt, record_read, require_job_id, workspace_of
from .auth import Problem
from .box_stream import TRANSCRIPT_CLOCK, TRANSCRIPT_CUT, inflate
from .box_upload import MAX_TRANSCRIPT_EXPANDED, TRANSCRIPT_FILES, capture_file_limit

STREAM_PATH, STDERR_PATH = TRANSCRIPT_FILES
MAX_TEXT = 20_000            # characters of one turn's text, and of its tool input as JSON
MAX_STDERR = 20_000          # characters of stderr, its end
MAX_PAGE, DEFAULT_PAGE = 200, 100
MAX_ATTEMPT = 100000


def capped(text, limit=MAX_TEXT):
    """(text, truncated): redacted first, then cut to `limit` characters."""
    if text is None: return None, False
    text = redact(text)
    return (text[:limit], True) if len(text) > limit else (text, False)


def input_text(value):
    """(JSON text, truncated) of a tool's input, redacted then capped. A partial input arrives as the JSON text so far."""
    if value is None: return None, False
    if isinstance(value, str): return capped(value)
    text = json.dumps(redact(value), ensure_ascii=False, separators=(',', ':'))
    return (text[:MAX_TEXT], True) if len(text) > MAX_TEXT else (text, False)


def content_text(content):
    """A tool result's or user message's content as text: strings joined, other blocks named."""
    if isinstance(content, str): return content
    if not isinstance(content, list): return None
    parts = []
    for block in content:
        if isinstance(block, str): parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get('text'), str): parts.append(block['text'])
        elif isinstance(block, dict): parts.append(f"[{block.get('type') or 'content'}]")
    return '\n'.join(parts)


def number(value):
    return value if type(value) in (int, float) else None


class Folder:
    """Folds the stream's events, in order, into turns."""

    def __init__(self):
        self.turns, self.cut, self.skipped, self.at = [], False, 0, None
        self.names = {}            # tool call id -> tool name, to name each result
        self.whole = {}            # message id -> whole content blocks seen in assistant events
        self.pending = None        # the message being streamed: {'id', 'blocks': {index: block}}

    def add(self, role, kind, text=None, *, tool_name=None, tool_input=None, call_id=None, is_error=False, partial=False, detail=None):
        text, truncated = capped(text)
        tool_input, input_truncated = input_text(tool_input)
        self.turns.append({'index': len(self.turns), 'role': role, 'kind': kind, 'text': text, 'truncated': truncated,
                           'tool_name': tool_name, 'tool_input': tool_input, 'tool_input_truncated': input_truncated,
                           'call_id': call_id, 'is_error': bool(is_error), 'partial': partial,
                           'detail': redact(detail) if detail is not None else None, 'at': self.at})

    def flush(self):
        """The blocks of a streamed message that no whole assistant event carried, as partial turns."""
        pending, self.pending = self.pending, None
        if not pending: return
        done = self.whole.get(pending['id'], 0)
        for index in sorted(pending['blocks'])[done:]:
            block = pending['blocks'][index]
            if block['type'] == 'tool_use':
                if block.get('id'): self.names[block['id']] = block.get('name')
                self.add('assistant', 'tool_use', tool_name=block.get('name'), tool_input=block['input'] or None,
                         call_id=block.get('id'), partial=True)
            elif block['type'] in ('text', 'thinking') and block['text']:
                self.add('assistant', block['type'], block['text'], partial=True)

    def stream(self, event):
        event = event.get('event')
        if not isinstance(event, dict): return
        kind = event.get('type')
        if kind == 'message_start':
            self.flush()
            message = event.get('message') if isinstance(event.get('message'), dict) else {}
            self.pending = {'id': message.get('id'), 'blocks': {}}
        elif self.pending is None:
            return
        elif kind == 'content_block_start' and type(event.get('index')) is int and isinstance(event.get('content_block'), dict):
            block = event['content_block']
            self.pending['blocks'][event['index']] = {'type': block.get('type'), 'id': block.get('id'), 'name': block.get('name'),
                                                      'text': '', 'input': ''}
        elif kind == 'content_block_delta' and isinstance(event.get('delta'), dict):
            block, delta = self.pending['blocks'].get(event.get('index')), event['delta']
            if block is None: return
            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str): block['text'] += delta['text']
            elif delta.get('type') == 'thinking_delta' and isinstance(delta.get('thinking'), str): block['text'] += delta['thinking']
            elif delta.get('type') == 'input_json_delta' and isinstance(delta.get('partial_json'), str): block['input'] += delta['partial_json']

    def assistant(self, event):
        message = event.get('message') if isinstance(event.get('message'), dict) else {}
        content = message.get('content') if isinstance(message.get('content'), list) else []
        self.whole[message.get('id')] = self.whole.get(message.get('id'), 0) + len(content)
        for block in content:
            if not isinstance(block, dict): continue
            kind = block.get('type')
            if kind == 'text': self.add('assistant', 'text', block.get('text') if isinstance(block.get('text'), str) else '')
            elif kind == 'thinking': self.add('assistant', 'thinking', block.get('thinking') if isinstance(block.get('thinking'), str) else '')
            elif kind == 'tool_use':
                call_id = block.get('id') if isinstance(block.get('id'), str) else None
                if call_id: self.names[call_id] = block.get('name')
                self.add('assistant', 'tool_use', tool_name=block.get('name'), tool_input=block.get('input'), call_id=call_id)

    def user(self, event):
        message = event.get('message') if isinstance(event.get('message'), dict) else {}
        content = message.get('content')
        if isinstance(content, str):
            self.add('user', 'text', content); return
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict): continue
            if block.get('type') == 'tool_result':
                call_id = block.get('tool_use_id') if isinstance(block.get('tool_use_id'), str) else None
                self.add('tool', 'tool_result', content_text(block.get('content')), tool_name=self.names.get(call_id),
                         call_id=call_id, is_error=block.get('is_error') is True)
            elif block.get('type') == 'text':
                self.add('user', 'text', block.get('text') if isinstance(block.get('text'), str) else '')

    def system(self, event):
        if event.get('subtype') == 'init':
            tools = event.get('tools') if isinstance(event.get('tools'), list) else []
            self.add('system', 'text', f"Claude Code {event.get('claude_code_version') or '(version not reported)'} started with "
                     f"model {event.get('model') or '(not reported)'} and {len(tools)} tools.",
                     detail={'model': event.get('model'), 'claude_code_version': event.get('claude_code_version'),
                             'permission_mode': event.get('permissionMode'), 'cwd': event.get('cwd'), 'tools': tools,
                             'mcp_servers': event.get('mcp_servers')})
        else:
            detail = {key: value for key, value in event.items() if key not in ('type', 'session_id', 'uuid')}
            self.add('system', 'text', json.dumps(detail, ensure_ascii=False, default=str))

    def result(self, event):
        self.flush()
        text = event.get('result') if isinstance(event.get('result'), str) else None
        if text is None and isinstance(event.get('errors'), list):
            text = '\n'.join(str(item) for item in event['errors'])
        self.add('result', 'result', text, is_error=event.get('is_error') is True,
                 detail={'subtype': event.get('subtype') if isinstance(event.get('subtype'), str) else None,
                         'stop_reason': event.get('stop_reason') if isinstance(event.get('stop_reason'), str) else None,
                         'num_turns': number(event.get('num_turns')), 'duration_ms': number(event.get('duration_ms')),
                         'duration_api_ms': number(event.get('duration_api_ms')), 'total_cost_usd': number(event.get('total_cost_usd'))})

    def marker(self, event):
        self.flush()
        self.cut = True
        omitted, lines = number(event.get('omitted_bytes')), number(event.get('omitted_lines'))
        self.add('system', 'text', f'{omitted or 0:,} bytes ({lines or 0:,} lines) of the transcript were left out here to fit the '
                 'capture limit.', detail={'omitted_bytes': omitted, 'omitted_lines': lines,
                                           'original_bytes': number(event.get('original_bytes'))})

    def feed(self, line):
        try: event = json.loads(line)
        except ValueError: event = None
        kind = event.get('type') if isinstance(event, dict) else None
        if not isinstance(kind, str):
            self.skipped += 1; return
        if kind == TRANSCRIPT_CLOCK:
            if isinstance(event.get('at'), str): self.at = event['at'][:40]
            return
        if isinstance(event.get('timestamp'), str): self.at = event['timestamp'][:40]
        handler = {'stream_event': self.stream, 'assistant': self.assistant, 'user': self.user, 'system': self.system,
                   'result': self.result, TRANSCRIPT_CUT: self.marker}.get(kind)
        # Rate-limit, progress and authentication notices (box_stream.PRIVATE_NOTIFICATION_TYPES) and unknown events
        # are not turns.
        if handler: handler(event)
        else: self.skipped += 1


def fold(raw):
    """(turns, cut, skipped lines) of a stream's inflated bytes."""
    folder = Folder()
    for line in raw.decode('utf-8', 'replace').splitlines():
        if line.strip(): folder.feed(line)
    folder.flush()
    return folder.turns, folder.cut, folder.skipped


def stderr_view(found):
    """The end of Claude Code's stderr, redacted, or None when it was not captured."""
    raw, why = found
    if raw is None: return None
    try: data, _ = inflate(raw, MAX_TRANSCRIPT_EXPANDED)
    except ValueError: return {'text': None, 'truncated': False, 'cut': False, 'unreadable': True}
    text = redact(data.decode('utf-8', 'replace'))
    cut = TRANSCRIPT_CUT.encode() in data
    return {'text': text[-MAX_STDERR:], 'truncated': len(text) > MAX_STDERR, 'cut': cut, 'unreadable': False}


def job_transcript(repository, job_id, admin, *, attempt=None, after=None, limit=DEFAULT_PAGE):
    """GET /internal/v1/admin/jobs/{id}/transcript. after: the index of the last turn already read (None: from the
    start). attempt: None for the newest."""
    require_job_id(job_id)
    if (not (type(limit) is int and 1 <= limit <= MAX_PAGE) or after is not None and not (type(after) is int and 0 <= after)
            or attempt is not None and not (type(attempt) is int and 1 <= attempt <= MAX_ATTEMPT)):
        raise Problem(422, 'INVALID_CURSOR', f'Choose an attempt, a turn index of 0 or more and 1–{MAX_PAGE} turns.')
    with repository.read_transaction(utc=True) as tx:
        row, queue = agent_job(tx, job_id)
        latest = latest_attempt(tx, job_id)
        remote, workspace = workspace_of(tx, job_id, queue)
    attempt = latest if attempt is None else attempt
    # Storage reads stay outside the database transaction.
    found = attempt_files(remote, workspace, job_id, attempt, {STREAM_PATH: capture_file_limit(STREAM_PATH),
                                                               STDERR_PATH: capture_file_limit(STDERR_PATH)})
    raw, missing = found[STREAM_PATH]
    turns, cut, skipped, complete = [], False, 0, None
    if raw is not None:
        try:
            data, complete = inflate(raw, MAX_TRANSCRIPT_EXPANDED)
            turns, cut, skipped = fold(data)
        except ValueError:
            missing, complete = 'unreadable', None
    page = [turn for turn in turns if after is None or turn['index'] > after][:limit]
    view = {'job': {'id': job_id, 'status': row['data'].get('status')}, 'attempt': attempt, 'latest_attempt': latest,
            'missing': missing, 'turns': page, 'total': len(turns),
            'next_after': page[-1]['index'] if page else after, 'has_more': bool(page) and page[-1]['index'] < len(turns) - 1,
            'cut': cut, 'complete': complete, 'skipped': skipped, 'stderr': stderr_view(found[STDERR_PATH])}
    read_at = record_read(repository, admin, job_id, 'jobs/transcript', attempt=attempt, after=after)
    return {**view, 'read': {'by': admin, 'at': read_at}}
