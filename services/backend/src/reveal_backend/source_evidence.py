"""What an account job's quotes rest on: files the agent saved itself, with a record of how.

An agent that writes a Scientific Account fetches each paper with a command and saves the bytes under
output/sources/, converting them to text with another command where needed. It records each step in the account the
way a lab notebook would, in DAPPER 0.2.0:

  - an Activity per command: `command`, `software_name`, `software_version`, `generated_at_time`;
  - a File per saved file: `filename` is its path under output/ (sources/PMC3491803.xml) and `location` the URL a
    fetched file came from;
  - `was_generated_by_edges` from each File to the Activity that wrote it, and `was_derived_from_edges` from a
    converted file to the file it was converted from;
  - an EvidenceItem per quote: the verbatim `snippet`, `was_derived_from` the File it quotes, and optionally the lines
    it sits on in `context`, written `line=212-213` (what grep -n prints).

`check` holds that record against the saved files and the run's tool ledger, deterministically and offline:

  - every File of the account (those its EvidenceItems quote and the files they were converted from) is a regular
    file under output/sources/;
  - each is written by an Activity with a command and a time, and that command is one whole command of a Bash call
    the ledger shows succeeded (`segments`: split on &&, ||, ;, | and new lines outside quotes, so an echoed command
    does not count); the command names the file, and a converted file's command names the file it came from, through
    an Activity of its own; a fetched file's command fetches its `location`;
  - no later command wrote the file: every command after the recorded one that names it only reads it (`READERS`)
    or is the recorded conversion of a file made from it;
  - each quote occurs exactly once in the file it cites (once on the lines given, when lines are given), within one
    paragraph, at 20 to 400 characters; all quotes of one paper together stay within 4,000 characters.

The agent never computes a checksum, a size or a byte offset. For every File it accepts, `check` gives the sha256,
size and media type it measured, the Bash call it matched (the observed command), and for every quote the exact byte
span in the file, the bytes there as text, and a line selector: what a DAPPER ValueLocator records. Each refusal says
what to change and never repeats the file's text.

The same function runs while the agent drafts (box_remote.lint_tool), at the gate (agent_account.accept) and when the
deposit form is made (account_deposit), so they cannot disagree about a document.

Standard library only: this module is bundled into the Box.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
from urllib.parse import unquote, urlsplit

CHECK = 'source-evidence'
SOURCES = 'sources/'
MAX_SOURCE_BYTES = 25_000_000
MIN_QUOTE_CHARACTERS = 20
# A locator carries the text it points at. The caps keep a deposited account from carrying the paper: one quote is a
# sentence or two, and all the quotes of one paper together stay a small part of it.
MAX_QUOTE_CHARACTERS = 400
MAX_PAPER_QUOTE_CHARACTERS = 4000
# ...and of a short text such as an abstract, at most this share (one full-length quote is always allowed).
MAX_PAPER_QUOTE_SHARE = 0.4
LINE_RANGE = re.compile(r'(?<![A-Za-z0-9_])line\s*=\s*(\d+)(?:\s*-\s*(\d+))?')
# A paragraph ends at a blank line or a page break; in markup, at a paragraph, section or title tag.
PARAGRAPH_BREAK = re.compile(r'\n[ \t\r\v]*\n|\f')
MARKUP_BREAK = re.compile(r'</?(?:p|sec|title|article-title|div|h[1-6]|li|td)(?:[\s>/])', re.IGNORECASE)
MARKUP = ('application/xml', 'text/html')


class SourceError(ValueError):
    """One thing about the record that does not hold. The message says what to change."""


def _rows(document, group):
    rows = document.get(group)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _id(node):
    return node.get('id') if isinstance(node.get('id'), str) else None


def source_activities(document):
    """Ids of the Activities that wrote a File of the document: the objects of its was_generated_by_edges whose
    subject is a File. These are the agent's own record of its sources; every other Activity it writes is replaced by
    REVEAL's record of the run."""
    files = {_id(node) for node in _rows(document, 'files')} - {None}
    return {edge['object'] for edge in _rows(document, 'was_generated_by_edges')
            if isinstance(edge.get('subject'), str) and edge['subject'] in files and isinstance(edge.get('object'), str)}


def names(command, filename):
    """True when `command` names output/<filename> as a whole path: relative (output/…, ./output/…) or absolute
    (/reveal/output/…, /reveal/trusted/output/…)."""
    return re.search(r'(?:^|(?<=[\s\'"=<>(;&|]))(?:\./|/reveal/(?:trusted/)?)?output/' + re.escape(filename)
                     + r'(?=$|[\s\'";&|)<>])', command) is not None


def segments(command):
    """The commands one Bash call ran, in order: its text split on `&&`, `||`, `;`, `|` and newlines outside quotes,
    each with its whitespace normalized. Text inside quotes (an `echo '…'`, say) is never a segment of its own."""
    parts, current, quote, index = [], [], None, 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            if char == '\\' and quote == '"' and index + 1 < len(command):
                current.append(command[index + 1]); index += 1
            elif char == quote:
                quote = None
        elif char == '\\' and index + 1 < len(command):
            current.append(char + command[index + 1]); index += 1
        elif char in '\'"':
            quote = char; current.append(char)
        elif command.startswith(('&&', '||'), index):
            parts.append(''.join(current)); current = []; index += 1
        elif char in ';|\n':
            parts.append(''.join(current)); current = []
        else:
            current.append(char)
        index += 1
    parts.append(''.join(current))
    return [' '.join(part.split()) for part in parts if part.split()]


def contains(observed, command):
    """True when `command`, whitespace normalized, is one whole command of the call `observed` (segments)."""
    return ' '.join(command.split()) in segments(observed)


# Programs that only read the files they name, as the first word of a command: a later call that runs one of them on
# a saved file leaves it as its recorded command wrote it. Anything else that names the file afterwards (sed -i, a
# redirect into it, another download over it) may have changed it, and the record no longer describes the bytes.
READERS = frozenset({'grep', 'egrep', 'fgrep', 'rg', 'head', 'tail', 'wc', 'less', 'more', 'file', 'stat', 'ls',
                     'sha256sum', 'md5sum', 'cat', 'sed', 'xmllint', 'pdftotext', 'pdfinfo'})


def reads_only(segment, filename):
    """True when one command `segment` only reads output/<filename>: its program is in READERS and the file is not the
    target of a redirect; sed without -i; xmllint without --output; pdftotext writing to stdout (-)."""
    try:
        lexer = shlex.shlex(segment, posix=True, punctuation_chars='<>')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens or PurePosixPath(tokens[0]).name not in READERS:
        return False
    program = PurePosixPath(tokens[0]).name
    for index, token in enumerate(tokens[:-1]):
        if set(token) <= set('<>') and '>' in token and names(tokens[index + 1], filename):
            return False
    if program == 'sed' and any(token == '--in-place' or token.startswith(('-i', '--in-place=')) for token in tokens[1:]):
        return False
    if program == 'xmllint' and any(token in ('--output', '-o') or token.startswith('--output=') for token in tokens[1:]):
        return False
    if program == 'pdftotext' and tokens[-1] != '-':
        return False
    return True


def _safe(filename):
    return (isinstance(filename, str) and filename.startswith(SOURCES) and len(filename.encode()) <= 1024 and '\\' not in filename
            and '\x00' not in filename and not PurePosixPath(filename).is_absolute()
            and all(part not in ('', '.', '..') for part in filename.split('/')))


def _read(root, filename):
    """The bytes of output/<filename>, opened without following a link at any step. Raises SourceError."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = filename.split('/')
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor); descriptor = child
            target = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        except FileNotFoundError:
            raise SourceError(f'There is no file output/{filename}. Save the paper there with the command its Activity records, '
                              'or correct filename') from None
        except (NotADirectoryError, OSError):
            raise SourceError(f'output/{filename} is not a regular file (a link or a directory). Save the bytes themselves there') from None
        with os.fdopen(target, 'rb') as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise SourceError(f'output/{filename} is not a regular file (a link or a directory). Save the bytes themselves there')
            if metadata.st_size > MAX_SOURCE_BYTES:
                raise SourceError(f'output/{filename} is over the {MAX_SOURCE_BYTES // 1_000_000} MB a saved source may have. Save a '
                                  'smaller form of the paper (its full-text XML or a text conversion)')
            return handle.read(MAX_SOURCE_BYTES + 1)
    finally:
        os.close(descriptor)


def read_source(root, filename):
    """The bytes of output/<filename> under the run's output folder `root`, read the way `check` reads them."""
    if not _safe(filename):
        raise SourceError(f'{filename} is not a path under sources/')
    return _read(Path(root), filename)


def media_type(data):
    """The media type of saved bytes, from the bytes themselves."""
    if data.startswith(b'%PDF-'):
        return 'application/pdf'
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return 'application/octet-stream'
    head = text[:2048].lstrip('﻿ \t\r\n').lower()
    if head.startswith(('<!doctype html', '<html')):
        return 'text/html'
    if head.startswith('<'):
        return 'application/xml'
    if head.startswith(('{', '[')):
        try:
            json.loads(text)
            return 'application/json'
        except ValueError:
            pass
    return 'text/plain'


def bash_calls(calls, ledger_root):
    """[(sequence, command, succeeded)] of the run's Bash calls, read from the requests the ledger captured."""
    found = []
    root = Path(ledger_root).resolve()
    for call in calls:
        if not isinstance(call, dict) or call.get('tool') != 'Bash' or not isinstance(call.get('request'), dict):
            continue
        request = call['request']
        path = (root / str(request.get('path', ''))).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != request.get('sha256'):
            raise SourceError('A captured Bash call does not match its recorded checksum')
        arguments = json.loads(raw).get('arguments')
        command = arguments.get('command') if isinstance(arguments, dict) else None
        if isinstance(command, str):
            found.append((call.get('sequence'), command, call.get('status') == 'completed'))
    return found


def _lines(text, start, end):
    """1-based numbers of the first and last line a character span covers; lines end at '\n' only, as grep -n counts."""
    first = text.count('\n', 0, start) + 1
    return first, first + text.count('\n', start, max(start, end - 1))


def _label(first, last):
    return f'line {first}' if first == last else f'lines {first}-{last}'


def _places(text, spans):
    """Where spans of a text sit, as grep -n would say: line 6, lines 3-4, or lines 6, 9-10."""
    lines = sorted({_lines(text, start, end) for start, end in spans})
    if len(lines) == 1:
        return _label(*lines[0])
    return 'lines ' + ', '.join(str(a) if a == b else f'{a}-{b}' for a, b in lines)


def _merge(spans):
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


class _Check:
    def __init__(self, document, output_root, calls, ledger_root):
        self.document, self.root = document, output_root
        self.files = {_id(node): node for node in _rows(document, 'files') if _id(node)}
        self.activities = {_id(node): node for node in _rows(document, 'activities') if _id(node)}
        self.generated, self.derived = {}, {}
        # The draft is the agent's own file: anything that is not an id is skipped here and left to the pinned linter.
        for group, target in (('was_generated_by_edges', self.generated), ('was_derived_from_edges', self.derived)):
            for edge in _rows(document, group):
                if isinstance(edge.get('subject'), str) and isinstance(edge.get('object'), str) and edge['subject'] in self.files:
                    target.setdefault(edge['subject'], []).append(edge['object'])
        self.calls = bash_calls(calls, ledger_root)
        self.problems, self.bound, self.bad, self.data, self.saved = [], {}, set(), {}, {}

    def file(self, identity, seen=()):
        """The binding of one File and the files it was converted from, or None after recording why not."""
        if identity in self.bound:
            return self.bound[identity]
        if identity in self.bad or identity in seen:
            return None
        try:
            entry = self._file(identity, (*seen, identity))
        except SourceError as exc:
            self.bad.add(identity)
            self.problems.append((identity, str(exc)))
            return None
        if entry is None:
            self.bad.add(identity)
            return None
        self.bound[identity] = entry
        return entry

    def _file(self, identity, seen):
        node = self.files[identity]
        filename = node.get('filename')
        if not _safe(filename):
            raise SourceError('filename is the path of the saved file under output/, and it must be under sources/ (for example '
                              'sources/PMC3491803.xml). Save the paper there with a recorded command and name it so')
        if filename not in self.saved:   # several Files may name one saved file; it is read once
            self.saved[filename] = _read(self.root, filename)
        data = self.saved[filename]
        made = self.generated.get(identity, [])
        if not made:
            raise SourceError(f'Record how output/{filename} was made: add an Activity with command, software_name, software_version '
                              'and generated_at_time, and a was_generated_by_edges row from this File to it')
        if len(made) > 1:
            raise SourceError(f'This File has {len(made)} was_generated_by_edges rows. Keep the one to the Activity whose command '
                              'wrote the file')
        activity_id = made[0]
        if isinstance(activity_id, str) and activity_id.startswith('urn:reveal:'):
            raise SourceError("The Activity that wrote this file has an id REVEAL uses for its own records. Give it an id of your own "
                              '(for example urn:draft:activity-1)')
        activity = self.activities.get(activity_id)
        if activity is None:
            raise SourceError(f'This File was generated by {activity_id}, which is not an Activity in this account. Add the Activity '
                              'whose command wrote the file')
        command = activity.get('command')
        if not isinstance(command, str) or not command.strip():
            raise SourceError(f'Its Activity {activity_id} has no command: write the exact command you ran to save output/{filename}')
        if not isinstance(activity.get('generated_at_time'), str) or not activity['generated_at_time'].strip():
            raise SourceError(f'Its Activity {activity_id} has no generated_at_time: write when the command ran (for example '
                              '2026-11-19T10:00:00Z)')
        command = command.strip()
        origins = self.derived.get(identity, [])
        raw = None
        if len(origins) > 1:
            raise SourceError(f'This File derives from {len(origins)} files. A converted file derives from the one saved file it was '
                              'converted from')
        if origins:
            origin = origins[0]
            if origin not in self.files:
                raise SourceError(f'This File derives from {origin}, which is not a File in this account. Point was_derived_from_edges '
                                  'at the File of the saved file it was converted from')
            if origin in seen:
                raise SourceError('The was_derived_from_edges of this File lead back to it. A converted file derives from the saved '
                                  'file it was converted from, and that one from nothing it made')
            if self.generated.get(origin) and activity_id in self.generated[origin]:
                raise SourceError('A converted file is made by its own Activity: record the conversion command (for example '
                                  'pdftotext -layout output/sources/x.pdf output/sources/x.txt) as a second Activity')
            raw = self.file(origin, seen)
            if raw is None:
                return None   # the file it came from is refused, with its own reason
        if not names(command, filename):
            raise SourceError(f'The command of its Activity does not name output/{filename}. Record the command that wrote this file '
                              f'(for example curl -sSL -o output/{filename} <url>)')
        if raw is not None and not names(command, raw['path']):
            raise SourceError(f"The command of its Activity does not name output/{raw['path']}, the file it derives from. Record the "
                              'command that converted that file into this one')
        recorded = ' '.join(command.split())
        ran = [(sequence, observed, ok) for sequence, observed, ok in self.calls if recorded in segments(observed)]
        succeeded = [(sequence, observed) for sequence, observed, ok in ran if ok]
        if not ran:
            raise SourceError('No Bash command run in this run is the command of its Activity. command must be exactly one command '
                              'you ran (copy it from your Bash call, without the && or ; around it); a file saved any other way '
                              'cannot be cited')
        if not succeeded:
            raise SourceError('The Bash call that ran the command of its Activity failed. Run it again and save the file it writes')
        sequence, observed = max(succeeded, key=lambda pair: pair[0] or 0)
        # The record holds only if nothing wrote the file after that command: every later command that names it must
        # only read it, or be the recorded conversion of a file made from it.
        consumers = {' '.join(self.activities[self.generated[made][0]]['command'].split())
                     for made, origins in self.derived.items() if identity in origins and self.generated.get(made)
                     and isinstance(self.activities.get(self.generated[made][0], {}).get('command'), str)}
        start = segments(observed).index(recorded) + 1
        for later, text, ok in sorted(self.calls, key=lambda call: call[0] or 0):
            if not ok or (later or 0) < (sequence or 0):
                continue
            for segment in segments(text)[start if later == sequence else 0:]:
                if names(segment, filename) and segment not in consumers and not reads_only(segment, filename):
                    raise SourceError(f'Bash call {later} ({segment[:120]}) touched output/{filename} after the command its Activity '
                                      'records, so the record no longer describes the file. Save the file again with one '
                                      'command and record that command; read files only with grep, head, sed -n and the like')
        location = node.get('location')
        if raw is None:
            if not isinstance(location, str) or not re.match(r'https?://', location):
                raise SourceError('A fetched file needs location: the http(s) URL its command fetched')
            if location not in command:
                raise SourceError(f'The command of its Activity does not fetch {location}. location is the URL the command fetched, '
                                  'exactly as the command has it')
        self.data[identity] = data
        return {'id': identity, 'path': filename, 'sha256': hashlib.sha256(data).hexdigest(), 'size_in_bytes': len(data),
                'mime_type': media_type(data), 'location': location if isinstance(location, str) else None,
                'url': location if raw is None else raw['url'], 'activity': activity_id, 'command': command,
                'observed_command': observed, 'ledger_sequence': sequence,
                'derived_from': raw['id'] if raw else None, 'root': raw['root'] if raw else identity}

    def quote(self, index, item):
        where = _id(item) or f'evidence_items[{index}]'
        cited = [ref for ref in item.get('was_derived_from', []) if isinstance(ref, str) and ref in self.files] \
            if isinstance(item.get('was_derived_from'), list) else []
        snippet = item.get('snippet')
        quoted = isinstance(snippet, str) and bool(snippet.strip())
        if not cited and not quoted:
            return None
        if not cited:
            raise SourceError('Cite the saved file this quote is from: set was_derived_from to the id of its File (a file under '
                              'output/sources/ saved by a recorded command)')
        if len(cited) > 1:
            raise SourceError(f'This EvidenceItem cites {len(cited)} files. Cite the one file the quote is from; use another '
                              'EvidenceItem for another file')
        entry = self.file(cited[0])
        if entry is None:
            return None   # the File is refused, with its own reason
        path = entry['path']
        if not quoted:
            raise SourceError(f'Quote the passage this EvidenceItem rests on in snippet, verbatim from {path}')
        try:
            text = self.data[cited[0]].decode('utf-8')
        except UnicodeDecodeError:
            raise SourceError(f"{path} is not UTF-8 text (it is {entry['mime_type']}). Convert it to text with a recorded command (for "
                              f'example pdftotext -layout output/{path} output/{path.rsplit(".", 1)[0]}.txt) and quote the text file') from None
        tokens = snippet.split()
        if sum(len(token) for token in tokens) < MIN_QUOTE_CHARACTERS:
            raise SourceError(f'The snippet is too short to locate: quote at least {MIN_QUOTE_CHARACTERS} characters of the passage, verbatim')
        if len(' '.join(tokens)) > MAX_QUOTE_CHARACTERS:
            raise SourceError(f'The snippet is too long: quote at most {MAX_QUOTE_CHARACTERS} characters, the sentence that carries the '
                              'finding. Put the rest in explanation, in your own words')
        context = item.get('context') if isinstance(item.get('context'), str) else ''
        lines = LINE_RANGE.search(context)
        first = last = None
        if lines:
            first = int(lines.group(1)); last = int(lines.group(2) or first)
            if first < 1 or last < first:
                raise SourceError(f'The line range in context ({lines.group(0)}) is not a range of lines. Write it as line=212 or '
                                  'line=212-213, as grep -n prints the lines')
        pattern = re.compile(r'\s+'.join(re.escape(token) for token in tokens))
        found = [match.span() for match in pattern.finditer(text)]
        if not found:
            raise SourceError(f'The snippet is not verbatim text of {path}. Copy the passage exactly as the file has it (same words, '
                              'punctuation, symbols and markup; only whitespace may differ), for example from what grep -n prints')
        if lines:
            inside = [span for span in found if first <= _lines(text, *span)[0] and _lines(text, *span)[1] <= last]
            if not inside:
                raise SourceError(f'The quote is not on {_label(first, last)} of {path}; it occurs on {_places(text, found)}. Correct '
                                  'the line range in context')
            if len(inside) > 1:
                raise SourceError(f'The quote occurs {len(inside)} times on {_label(first, last)} of {path}. Quote a longer passage that '
                                  'occurs once')
            found = inside
        elif len(found) > 1:
            raise SourceError(f'The quote occurs {len(found)} times in {path}, on {_places(text, found)}. Quote a longer passage, or '
                              'give the line range in context as line=N')
        start, end = found[0]
        value = text[start:end]
        if PARAGRAPH_BREAK.search(value) or (entry['mime_type'] in MARKUP and MARKUP_BREAK.search(value)):
            raise SourceError(f'The quote runs across two paragraphs of {path} ({_label(*_lines(text, start, end))}). Quote from one '
                              'paragraph; use a second EvidenceItem for the other')
        if len(value) > MAX_QUOTE_CHARACTERS:
            raise SourceError(f'The quoted passage is {len(value)} characters in {path}, over the {MAX_QUOTE_CHARACTERS} allowed. Quote a '
                              'shorter passage')
        byte_start = len(text[:start].encode('utf-8'))
        return {'evidence_item': where, 'index': index, 'file': cited[0], 'path': path, 'sha256': entry['sha256'],
                'byte_start': byte_start, 'byte_length': len(value.encode('utf-8')), 'value_text': value,
                'selector': 'line=' + (lambda a, b: str(a) if a == b else f'{a}-{b}')(*_lines(text, start, end)),
                'media_type': entry['mime_type'], 'characters': (start, end), 'text_length': len(text)}


def check(document, output_root, calls, ledger_root):
    """([(where, what to change)], binding) for an account document, the folder its files are saved in (the run's
    output/), and the run's tool ledger (`calls`, with request paths relative to `ledger_root`).

    The binding holds what REVEAL measured and matched, for the accepted parts only:
      files: {File id: path, sha256, size_in_bytes, mime_type, location, url (the URL the bytes ultimately came from),
              activity, command, observed_command, ledger_sequence, derived_from (File id or None), root (File id)}
      activities: {Activity id: observed_command, ledger_sequence}
      quotes: [{evidence_item, index, file, path, sha256, byte_start, byte_length, value_text, selector, media_type}]
    """
    state = _Check(document, Path(output_root), calls, ledger_root)
    quotes = []
    items = document.get('evidence_items') if isinstance(document.get('evidence_items'), list) else []
    for index, item in enumerate(items):   # index into the document's own list: the deposit edits that item
        if not isinstance(item, dict):
            continue
        try:
            quote = state.quote(index, item)
        except SourceError as exc:
            state.problems.append((_id(item) or f'evidence_items[{index}]', str(exc)))
            continue
        if quote is not None:
            quotes.append(quote)
    # Every File of the account is part of its record of sources, whether or not a quote cites it.
    for identity in state.files:
        state.file(identity)
    # All quotes of one paper together: the file it was fetched as and every file converted from it.
    papers = {}
    for quote in quotes:
        entry = papers.setdefault(state.bound[quote['file']]['root'], {'spans': {}, 'length': 0, 'where': None})
        entry['spans'].setdefault(quote['file'], []).append(quote['characters'])
        entry['length'] = max(entry['length'], quote['text_length']); entry['where'] = quote['evidence_item']
    over = set()
    for root, entry in papers.items():
        total = sum(_merge(spans) for spans in entry['spans'].values())
        allowed = min(MAX_PAPER_QUOTE_CHARACTERS, max(MAX_QUOTE_CHARACTERS, int(entry['length'] * MAX_PAPER_QUOTE_SHARE)))
        if total > allowed:
            over.add(root)
            state.problems.append((entry['where'], f"This account quotes {total:,} characters of one paper ({state.bound[root]['path']} "
                                   f'and the files made from it); the most is {allowed:,}. Keep the sentences that carry each finding '
                                   'and put the rest in explanation, in your own words'))
    quotes = [{key: value for key, value in quote.items() if key not in ('characters', 'text_length')}
              for quote in quotes if state.bound[quote['file']]['root'] not in over]
    binding = {'files': dict(sorted(state.bound.items())),
               'activities': {entry['activity']: {'observed_command': entry['observed_command'], 'ledger_sequence': entry['ledger_sequence']}
                              for _, entry in sorted(state.bound.items())},
               'quotes': quotes}
    return state.problems, binding


def bind(document, binding):
    """A copy of `document` whose accepted Files carry what REVEAL measured: sha256, size_in_bytes and mime_type.
    Nothing else changes; the agent's saved draft is left as it wrote it."""
    bound = deepcopy(document)
    for node in _rows(bound, 'files'):
        entry = binding['files'].get(_id(node))
        if entry:
            node.update(sha256=entry['sha256'], size_in_bytes=entry['size_in_bytes'], mime_type=entry['mime_type'])
    return bound


def verified_files(binding):
    """[{sha256, size_bytes}] of the accepted files: the inventory the pinned linter counts as captured sources
    (source_validation.ledger_sources reads it as `source_files`)."""
    unique = {(entry['sha256'], entry['size_in_bytes']) for entry in binding['files'].values()}
    return [{'sha256': digest, 'size_bytes': size} for digest, size in sorted(unique)]


def ledger_view(binding):
    """The ledger the pinned linter reads for an account job: no tool responses (an account cites only files the agent
    saved), and the saved files this module accepted (source_validation.ledger_sources). REVEAL writes it; the agent
    cannot."""
    return {'format': 'reveal.tool-ledger/1', 'calls': [], 'source_files': verified_files(binding)}


def publication(url):
    """A DAPPER Publication's identifiers, from those a source URL carries: a PMC id, a PubMed id, a DOI. Empty when it
    carries none that can be told apart from the rest of the address."""
    if not isinstance(url, str):
        return {}
    parts = urlsplit(url)
    path = unquote(parts.path)
    node = {}
    pmcid = re.search(r'(?i)(?<![A-Za-z0-9])PMC(\d{1,9})(?![0-9])', path + '?' + parts.query)
    if pmcid:
        node['citation'] = 'PMCID:PMC' + pmcid.group(1)   # DAPPER's Publication has no PMC id slot
    pmid = re.fullmatch(r'/(\d{1,9})/?', path) if parts.hostname == 'pubmed.ncbi.nlm.nih.gov' else \
        re.search(r'/MED/(\d{1,9})(?![0-9])', path)
    if pmid:
        node['pmid'] = pmid.group(1)
    doi = re.fullmatch(r'/(10\.\d{4,9}/\S+)', path) if parts.hostname in ('doi.org', 'dx.doi.org') else \
        re.search(r'/doi/(?:(?:abs|full|pdf|epdf|pdfdirect)/)?(10\.\d{4,9}/[^\s?#]+?)/?$', path)
    if doi:
        node['doi'] = 'doi:' + doi.group(1)
    return node


# The programs an Activity's command may run to fetch or convert a paper (deposit_screen.FETCH_PROGRAMS), and the Debian
# packages the Box toolchain installs them from (box_adapter.toolchain_script).
SOURCE_PACKAGES = ('curl', 'poppler-utils', 'libxml2-utils')
SOURCE_PROGRAMS = (('curl', '--version'), ('pdftotext', '-v'), ('xmllint', '--version'))


def tool_versions():
    """{'packages': {package: installed version}, 'programs': {program: first line of its version output}} of the fetch
    and convert tools, so the Activities an account records can be explained (and the apt pins chosen). A package or
    program that is not installed, or does not answer, is None. Never raises.

    Self-contained on purpose: the toolchain snapshot builder runs this function's source in its builder Box
    (scripts/build_box_toolchain_snapshot.py), with SOURCE_PACKAGES and SOURCE_PROGRAMS defined beside it."""
    import subprocess

    def run(argv):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    packages = dict.fromkeys(SOURCE_PACKAGES)
    listed = run(['dpkg-query', '-W', '-f', '${Package}=${Version}\n', *SOURCE_PACKAGES])
    for line in (listed.stdout if listed else '').splitlines():
        name, _, version = line.partition('=')
        if name in packages and version.strip():
            packages[name] = version.strip()[:200]
    programs = {}
    for program, flag in SOURCE_PROGRAMS:
        done = run([program, flag])
        # pdftotext -v and xmllint --version print to standard error, and pdftotext -v exits non-zero on some releases.
        lines = [line.strip() for line in ((done.stdout or '') + '\n' + (done.stderr or '') if done else '').splitlines() if line.strip()]
        programs[program] = lines[0][:200] if lines else None
    return {'packages': packages, 'programs': programs}
