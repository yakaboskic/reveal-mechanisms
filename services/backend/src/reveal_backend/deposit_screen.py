"""The prompt-injection screen an agent job's Scientific Account passes before it is deposited.

A Scientific Account job reads papers and web pages its agent chose. Any of them can carry text written to steer a
model ("ignore your instructions and ..."), and the account it steers would be written into the shared claims
repository. This screen runs once, at the end of the gate (agent_account.accept), on the account in its deposit
form, before the workflow's commit step sends it (claims_deposit.send). It looks for attacks, not for scientific
quality. It has two parts.

**Fixed checks** on the deposit-form document. These are the real protection: whatever a model was told, the
account cannot carry it out unless it gets past them.

  free text      Every prose field the agent wrote (names, statements, explanations, context, closing remarks, ...)
                 carries no link (http://, www., javascript:, data: URIs), no code fence, no HTML or XML tag, no
                 markdown link, no hidden characters (zero-width, bidirectional and tag-block characters, control
                 characters) and none of INSTRUCTION_PATTERNS: text addressed to a model. A quoted snippet is the
                 paper's own text, so it may carry markup and links, but not hidden characters or instructions; nor
                 may a command, an identifier or a URL.
                 Strings REVEAL itself wrote or the requester supplied (the question, the requested claims, REVEAL's
                 run Activity, the attributed agents) are not the agent's and are not checked.
  entities       Each Proposition's subject_entity, relation and object_entity is a requested claim's value, a CURIE
                 (or absolute URI) in a namespace the account may use (the evidence package's prefixes and the pinned
                 DAPPER release's), or a string printed in a cited source.
  claims         With requested claims, every Claim and every Proposition is about one of them: the requested
                 Proposition itself, or one with exactly its statement plus a triple. Without any, the agent's own
                 claims are allowed, each Proposition is the subject of a Claim, and there are at most
                 MAX_ACCOUNT_CLAIMS of them. Never more than MAX_CLAIMS Claims in all.
  commands       Each source Activity's `command` is one plain fetch or conversion (FETCH_PROGRAMS, the first word),
                 with no command substitution, nothing written outside output/, only the options OPTIONS lists for
                 its program, no argument naming a local file outside output/ (absolute, ~, .., @file) and no URL
                 that is not http(s); the observed Bash call it came from runs no shell or interpreter (SHELLS) and
                 writes nowhere outside output/ (or /dev/null). scripts/audit_account_sources.py reruns only commands
                 this accepts (command_refusal).
  size           Every field has a cap (FIELD_CAPS; DEFAULT_TEXT_CAP and IDENTIFIER_CAP otherwise), and the agent's
                 free text together stays under MAX_FREE_TEXT characters.

**The classifier.** Llama Prompt Guard 2 22M, run with onnxruntime and tokenizers, scores what the agent read and
wrote: the text of every cited source file (XML and HTML element names set aside, but comments, CDATA and
attribute values kept, and every line break), the results of every
non-Bash tool call in the ledger except REVEAL's own tools and kit files, and every string the agent wrote in the
account (prose, quotes, commands, identifiers).
Each text is cut into overlapping windows of WINDOW_TOKENS tokens (OVERLAP tokens shared between neighbours, so a
short attack never falls only across a boundary). A window scoring at or above the threshold
(REVEAL_PROMPT_GUARD_THRESHOLD, default 0.5) withholds the account. More than REVEAL_PROMPT_GUARD_MAX_WINDOWS windows
(default 2,000) or MAX_SCREEN_CHARACTERS characters withholds it too: nothing is ever truncated unscreened.

The model is loaded once per process (`startup`, from the API's lifespan), into one InferenceSession with one
thread, and warmed with a dummy input. No job loads it. In a deployed environment (REVEAL_ENVIRONMENT other than
development or test) a missing or unverified model stops the process from starting; the image build checks it too
(`verify_model`, as verify_release does for DAPPER). In development and tests the classifier is whatever was
installed (`install`, a fake in tests); with none, only the fixed checks run and the verdict says so.

**The verdict** is a small record with no text in it: `outcome` ('passed' or 'withheld'), the `check` that failed,
where (`location`: a source file and line range, a ledger call, or an account field), the classifier's `score` and
`threshold`, and how much was scored. It is saved with the accepted account, so a retried commit reads the same
verdict. A withheld account is still stored with its job and returned to its requester; it is never sent to the
claims repository, and the job result says why in one line (`withheld`). Operators see the verdict in telemetry.
"""
from bisect import bisect_right
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import threading
import time

from . import source_evidence
from .runtime_config import CURRENT_DAPPER_SNAPSHOT, ROOT, setting

log = logging.getLogger(__name__)

LOCK = ROOT / 'services/backend/agent-runtime/prompt-guard-release.json'
DEFAULT_MODEL_DIR = '/app/models/prompt-guard-2-22m'
DEFAULT_THRESHOLD = 0.5
DEFAULT_MAX_WINDOWS = 2000
# The model reads 512 tokens at a time, its special tokens included.
WINDOW_TOKENS = 512
OVERLAP = 64
MAX_SCREEN_CHARACTERS = 4_000_000
MODEL_FILES = ('model.onnx', 'tokenizer.json', 'config.json')

# Withheld-account checks, in the order they run. Each is also the deposit's `reason`.
CHECKS = ('oversized_field', 'unsafe_text', 'unsafe_command', 'unrequested_claim', 'unknown_entity', 'screen_too_large',
          'prompt_injection')
WHY = {
    'oversized_field': 'a field is over its size limit',
    'unsafe_text': 'a field carries a link, code, markup, hidden characters or text addressed to a model',
    'unsafe_command': 'a recorded command is not a plain fetch or conversion that writes only under output/',
    'unrequested_claim': 'it makes claims beyond those the job asked about',
    'unknown_entity': 'a Proposition names an entity or predicate that is not in the request, an allowed namespace or a cited source',
    'screen_too_large': 'what the run read is more than the screen can check',
    'prompt_injection': 'text the run read or wrote looks like an attempt to instruct a model',
}

# --- fixed checks ---------------------------------------------------------------------------------------------------

MAX_FREE_TEXT = 200_000
DEFAULT_TEXT_CAP = 2_000
IDENTIFIER_CAP = 300
MAX_CLAIMS = 40
# Characters per field. A quote is capped by source_evidence (400); a requested statement by box_agent (1,000).
FIELD_CAPS = {
    ('scientific_accounts', 'name'): 300, ('scientific_accounts', 'context'): 4_000,
    ('scientific_accounts', 'closing_remarks'): 4_000, ('claims', 'statement'): 1_000, ('propositions', 'statement'): 1_000,
    ('evidence_items', 'explanation'): 2_000, ('evidence_items', 'context'): 1_000,
    ('evidence_items', 'snippet'): source_evidence.MAX_QUOTE_CHARACTERS,
    ('value_locators', 'value_text'): source_evidence.MAX_QUOTE_CHARACTERS,
    ('activities', 'name'): 300, ('activities', 'software_name'): 100, ('activities', 'software_version'): 100,
    ('activities', 'command'): 2_000, ('activities', 'observed_command'): 8_000,
    ('files', 'location'): 2_048, ('files', 'filename'): 1_024,
}
# The paper's own words: markup and links are allowed (a quote from XML carries tags), hidden characters and
# instructions are not. Scored by the classifier.
VERBATIM = {('evidence_items', 'snippet'), ('value_locators', 'value_text')}
COMMANDS = {('activities', 'command'), ('activities', 'observed_command')}
TERMS = {('propositions', 'subject_entity'), ('propositions', 'relation'), ('propositions', 'object_entity')}
# Identifiers, enums and values REVEAL measured: short, one token-like string each, never prose.
IDENTIFIERS = {('files', 'sha256'), ('files', 'mime_type'), ('files', 'filename'), ('value_locators', 'selector'),
               ('value_locators', 'media_type'), ('activities', 'generated_at_time'), ('publications', 'doi'),
               ('publications', 'pmid'), ('publications', 'citation'), ('evidence_items', 'reference'),
               ('evidence_items', 'direction'), ('claims', 'direction'), ('claims', 'status'),
               ('propositions', 'proposition_kind')}
# Groups REVEAL writes itself: the attributed requester or the service (account_deposit), and the requester's
# question (agent_account.build). The agent cannot change them.
REVEAL_GROUPS = {'persons', 'organizations', 'knowledge_gaps'}
REFERENCE = re.compile(r'^dapper:[A-Za-z]+\.[A-Za-z0-9_-]+$')

LINK = re.compile(r'(?i)\b(?:https?|s?ftp|file|ssh|wss?)://|\bwww\.[a-z0-9-]+\.[a-z]{2,}|\b(?:javascript|vbscript):|\bdata:[a-z]+/[a-z0-9.+-]+[;,]')
CODE = re.compile(r'```|~~~|<\?(?:php|xml)|\$\(|\$\{')
# A tag: < and a letter (or /letter, !--, ![CDATA[), so "p < 0.05" and "n<10" are prose. Markdown links and images.
MARKUP = re.compile(r'</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>]*)?/?>|<!--|<!\[CDATA\[|!?\[[^\]\n]{1,200}\]\([^)\s]{1,500}\)')
HIDDEN = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]')
# Text addressed to a model rather than to a reader. Deliberately short: the classifier covers paraphrase.
INSTRUCTION_PATTERNS = (
    # "ignore all previous instructions", "disregard your rules"; never "ignore the rules of fasting"
    r'\b(?:ignore|disregard|forget)\s+(?:(?:all|any|the|of)\s+){0,2}(?:(?:your|my|these|those|previous|prior|above|earlier|'
    r'preceding|original|system|safety)\s+){1,2}(?:instructions?|prompts?|directions|rules|guidelines|system message)\b',
    r'\b(?:ignore|disregard|forget)\s+(?:(?:all|any|the)\s+)?(?:instructions|prompts?|system message)\b',
    r'\b(?:override|bypass)\s+(?:(?:all|any|the|your|these)\s+){0,2}(?:(?:previous|prior|system|safety)\s+){0,2}'
    r'(?:instructions?|prompts?|restrictions|guardrails|system message)\b',
    r'\bsystem\s+prompt\b',
    r'\byou\s+are\s+now\b',
    r'\bnew\s+instructions?\s*:',
    r'\b(?:developer|jailbreak|god|dan)\s+mode\b',
    r'\bdo\s+anything\s+now\b',
    r'\b(?:reveal|print|repeat|output|send)\b[^.\n]{0,30}\b(?:system prompt|your instructions|api[ _-]?key|secret|password|credentials?)\b',
    r'<\|[A-Za-z_]{2,40}\|>',            # chat-template tokens: <|im_start|>, <|eot_id|>, <|endoftext|>
    r'\[/?INST\]|<</?SYS>>',
    r'(?<=\n)[ \t]*(?:system|assistant|user|human|developer)[ \t]*:',   # a role label opening a new line
    r'^[ \t]*(?:assistant|developer)[ \t]*:',                           # or opening the text
)
INSTRUCTION = re.compile('|'.join(f'(?:{pattern})' for pattern in INSTRUCTION_PATTERNS), re.IGNORECASE)

# The first word of a source Activity's command: fetch and convert only.
FETCH_PROGRAMS = frozenset({'curl', 'wget', 'pdftotext', 'xmllint'})
# Never run in the Bash call a source was saved by, piped into or otherwise.
SHELLS = frozenset({'sh', 'bash', 'zsh', 'dash', 'ksh', 'csh', 'tcsh', 'fish', 'python', 'python2', 'python3', 'perl', 'ruby',
                    'node', 'deno', 'php', 'lua', 'Rscript', 'osascript', 'eval', 'exec', 'source', '.', 'xargs', 'env',
                    'sudo', 'su', 'nohup', 'timeout', 'busybox'})
# Options that name a file the program writes, by program; and options refused outright (they write somewhere not
# named, or read further commands from elsewhere).
WRITE_OPTIONS = {
    'curl': {'-o', '--output', '-D', '--dump-header', '-c', '--cookie-jar', '--trace', '--trace-ascii', '--libcurl',
             '--etag-save', '--stderr', '--hsts', '--alt-svc', '--output-dir'},
    'wget': {'-O', '--output-document', '-o', '--output-file', '-a', '--append-output', '-P', '--directory-prefix',
             '--save-cookies', '--save-headers'},
    'xmllint': {'-o', '--output'},
    'pdftotext': set(),
}
REFUSED_OPTIONS = {'curl': {'-O', '--remote-name', '--remote-name-all', '-J', '--remote-header-name', '-K', '--config'},
                   'wget': {'-e', '--execute', '-i', '--input-file', '-x', '--force-directories', '-r', '--recursive', '-m',
                            '--mirror', '-N', '--timestamping'}}
DEVICES = ('/dev/null', '/dev/stdout', '/dev/stderr')
# The only options a source command may use, by program, each with whether it takes a value: fetching with GET,
# converting, and choosing where the result goes. Anything else (sending data, reading a config, variable, cookie,
# DTD or catalog file, expanding entities or XIncludes) is refused. Exact names only, so wget's abbreviated long
# options (--exec for --execute) and xmllint's single-dash spellings (-output) are refused too.
OPTIONS = {
    'curl': {'-s': 0, '--silent': 0, '-S': 0, '--show-error': 0, '-L': 0, '--location': 0, '-f': 0, '--fail': 0,
             '--fail-with-body': 0, '-o': 1, '--output': 1, '-D': 1, '--dump-header': 1, '--create-dirs': 0,
             '-H': 1, '--header': 1, '-A': 1, '--user-agent': 1, '-e': 1, '--referer': 1, '--compressed': 0,
             '--retry': 1, '--retry-delay': 1, '--retry-max-time': 1, '--retry-all-errors': 0, '--retry-connrefused': 0,
             '-m': 1, '--max-time': 1, '--connect-timeout': 1, '--max-filesize': 1, '--max-redirs': 1, '-w': 1,
             '--write-out': 1, '-g': 0, '--globoff': 0, '-#': 0, '--progress-bar': 0, '--no-progress-meter': 0,
             '-v': 0, '--verbose': 0, '-q': 0, '--disable': 0, '--url': 1, '--http1.1': 0, '--http2': 0,
             '--proto': 1, '--proto-redir': 1, '-I': 0, '--head': 0, '-i': 0, '--include': 0, '-R': 0, '--remote-time': 0,
             '-X': 1, '--request': 1},
    'wget': {'-q': 0, '--quiet': 0, '-nv': 0, '--no-verbose': 0, '-v': 0, '--verbose': 0, '-O': 1, '--output-document': 1,
             '-t': 1, '--tries': 1, '-T': 1, '--timeout': 1, '--waitretry': 1, '--retry-connrefused': 0, '-U': 1,
             '--user-agent': 1, '--header': 1, '--referer': 1, '--max-redirect': 1, '-S': 0, '--server-response': 0,
             '--content-on-error': 0, '--compression': 1, '--no-cache': 0, '--https-only': 0, '--progress': 1,
             '--show-progress': 0},
    'xmllint': {'--format': 0, '--noout': 0, '--xpath': 1, '--nonet': 0, '--recover': 0, '--html': 0, '--encode': 1,
                '--noblanks': 0, '--nocdata': 0, '--nsclean': 0, '--noenc': 0, '--output': 1, '-o': 1, '--c14n': 0,
                '--c14n11': 0, '--exc-c14n': 0, '--huge': 0, '--pretty': 1, '--nowarning': 0, '--quiet': 0},
    'pdftotext': {'-layout': 0, '-raw': 0, '-fixed': 1, '-f': 1, '-l': 1, '-r': 1, '-x': 1, '-y': 1, '-W': 1, '-H': 1,
                  '-enc': 1, '-eol': 1, '-nopgbrk': 0, '-q': 0, '-nodiag': 0, '-htmlmeta': 0, '-bbox': 0,
                  '-bbox-layout': 0, '-cropbox': 0, '-tsv': 0},
}
# Options whose value must be one of these: a request method that sends nothing.
OPTION_CHOICES = {'curl': {'-X': {'GET', 'HEAD'}, '--request': {'GET', 'HEAD'}}}
# Values that are text, not paths: headers, user agents, referrers, write-out formats, XPath expressions. Only @file
# (curl reads -H @file and -w @file from a file) is refused in them.
TEXT_VALUES = {'curl': {'-H', '--header', '-A', '--user-agent', '-e', '--referer', '-w', '--write-out'},
               'wget': {'--header', '-U', '--user-agent', '--referer'},
               'xmllint': {'--xpath', '--encode'}}
# A word that starts like a URL (scheme:), as curl and wget read one.
URL_SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:(?!\s)')


class ScreenError(RuntimeError):
    """The screen cannot run where it must: no verified model in a deployed process."""


class Finding(Exception):
    """One failed check: which, and where (never the text that failed it)."""
    def __init__(self, check, location, score=None):
        super().__init__(check)
        self.check, self.location, self.score = check, location, score


def _place(group, index, key):
    return f'{group}[{index}].{key}'


def _output_path(token):
    """True for a path under the run's output/ folder, as the agent's commands name it."""
    if not isinstance(token, str):
        return False
    match = re.match(r'^(?:\./|/reveal/(?:trusted/)?)?output/(.*)$', token)
    return bool(match) and all(part not in ('..',) for part in match.group(1).split('/'))


def _tokens(segment):
    lexer = shlex.shlex(segment, posix=True, punctuation_chars='<>|&;')
    lexer.whitespace_split = True
    return list(lexer)


def _redirects(tokens):
    """(the tokens without their redirects, True when every output redirect writes under output/ or to a device)."""
    kept, safe, index = [], True, 0
    while index < len(tokens):
        token = tokens[index]
        if token and not set(token) - set('<>&|;'):
            if token in ('&', '|', ';', '&&', '||'):
                return kept, False   # a background job or a second command inside what should be one
            target = tokens[index + 1] if index + 1 < len(tokens) else ''
            if '>' in token and not (token.endswith('&') or re.fullmatch(r'\d+|-', target)
                                     or _output_path(target) or target in DEVICES):
                safe = False
            index += 2; continue
        if re.fullmatch(r'\d+', token) and index + 1 < len(tokens) and tokens[index + 1][:1] in '<>':
            index += 1; continue   # the descriptor of 2>/dev/null
        kept.append(token); index += 1
    return kept, safe


def _program(tokens):
    """(index, name) of the program a command runs, past any VAR=value assignments."""
    for index, token in enumerate(tokens):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', token):
            return index, PurePosixPath(token).name
    return len(tokens), ''


# Short options that take a value, for the clustered forms curl and wget accept (-fsSLo FILE): scanning a cluster
# stops at the first of them, and the value is the rest of the cluster or the next word.
VALUE_LETTERS = {'curl': set('AbcCdDeEFHKmoPQrTuUwxXyYz'), 'wget': set('OoaPeitTwQUlARDIXB')}


def _local_path(value):
    """True when a word names a file on this machine by an absolute, home or parent-relative path, or as @file."""
    return value.startswith(('/', '~', '../', '@')) or value == '..' or '/../' in value or value.endswith('/..') or '=@' in value


def options_of(program, token):
    """(the options one word sets, the value it carries itself or None): --name[=value]; an option the program spells
    as one word (pdftotext -layout, wget -nv); or each letter of a short-option cluster up to the first that takes a
    value (VALUE_LETTERS), with the rest of the word as that value (-sSLoPATH)."""
    if token.startswith('--'):
        name, equals, value = token.partition('=')
        return [name], (value if equals else None)
    if token in OPTIONS.get(program, ()) or program not in VALUE_LETTERS:
        return [token], None
    letters = []
    for offset, letter in enumerate(token[1:], start=2):
        letters.append('-' + letter)
        if letter in VALUE_LETTERS[program]:
            return letters, token[offset:] or None
    return letters, None


def _value_refusal(token, value, option, program):
    """Why one argument (an option's value, or a positional word) is refused, or None."""
    if option in OPTION_CHOICES.get(program, {}):
        return None if value.upper() in OPTION_CHOICES[program][option] else f'{option} {value} is not a plain GET'
    if option in TEXT_VALUES.get(program, ()):
        return f'{token} reads a local file' if value.startswith('@') else None
    if URL_SCHEME.match(value) and not value.lower().startswith(('http://', 'https://')):
        return f'{token} is not an http(s) URL'
    if _local_path(value) and not _output_path(value) and value not in DEVICES:
        return f'{token} names a file outside output/'
    return None


def _argument_refusal(tokens):
    """Why the words of one plain command (its redirects removed) are refused, or None: the first word is the program
    itself, every option is one OPTIONS lists for it, and every argument passes _value_refusal."""
    if not tokens or tokens[0] not in OPTIONS:
        return 'its first word is not ' + ', '.join(sorted(FETCH_PROGRAMS))
    program, allowed, awaiting = tokens[0], OPTIONS[tokens[0]], None
    for token in tokens[1:]:
        if awaiting is not None:
            # The value of the option before it, whatever it looks like.
            reason = _value_refusal(token, token, awaiting, program); awaiting = None
        elif token.startswith('-') and token != '-':
            options, inline = options_of(program, token)
            unknown = next((option for option in options if option not in allowed), None)
            if unknown:
                return f'{unknown} is not an option a source command may use with {program}'
            last = options[-1]
            if inline is not None and not allowed[last]:
                return f'{last} takes no value'
            if allowed[last] and inline is None:
                awaiting = last; continue
            reason = _value_refusal(token, inline or '', last, program)
        else:
            reason = _value_refusal(token, token, None, program)
        if reason:
            return reason
    return f'{awaiting} has no value' if awaiting else None


def command_refusal(command):
    """Why a source Activity's recorded command is not one plain fetch or conversion writing only under output/, or
    None when it is. The audit (scripts/audit_account_sources.py) runs only commands this accepts."""
    if not _plain(command):
        return 'it is not one plain curl, wget, pdftotext or xmllint command writing only under output/'
    tokens = _tokens(command)
    # Standard input may come only from a saved file.
    for index, token in enumerate(tokens):
        if '<' in token and not set(token) - set('<>&|;'):
            if token != '<' or index + 1 >= len(tokens) or not _output_path(tokens[index + 1]):
                return 'it reads standard input from somewhere other than output/'
    return _argument_refusal(_redirects(tokens)[0])


def check_command(command):
    """True when a source Activity's recorded command is one plain fetch or conversion writing only under output/,
    using only the options and arguments command_refusal allows."""
    return command_refusal(command) is None


def _plain(command):
    """True when a command is one fetch or conversion program writing only under output/ (its options unchecked)."""
    if not isinstance(command, str) or HIDDEN.search(command) or '\n' in command or '`' in command or re.search(r'\$\(|<\(|>\(', command):
        return False
    if len(source_evidence.segments(command)) != 1:
        return False
    try:
        tokens = _tokens(command)
    except ValueError:
        return False
    tokens, safe = _redirects(tokens)
    at, program = _program(tokens)
    if program not in FETCH_PROGRAMS or not safe:
        return False
    writes, refused = WRITE_OPTIONS[program], REFUSED_OPTIONS.get(program, set())
    arguments, positional, index = tokens[at + 1:], [], 0

    def writable(target):
        return _output_path(target) or target == '-'
    while index < len(arguments):
        token = arguments[index]
        following = arguments[index + 1] if index + 1 < len(arguments) else ''
        if token.startswith('--'):
            name, equals, inline = token.partition('=')
            if name in refused:
                return False
            if name in writes:
                if not writable(inline if equals else following):
                    return False
                index += 1 if equals else 2; continue
        elif token.startswith('-') and len(token) > 1 and program in VALUE_LETTERS:
            for offset, letter in enumerate(token[1:], start=2):
                option = '-' + letter
                if option in refused:
                    return False
                if letter in VALUE_LETTERS[program]:
                    value = token[offset:]
                    if option in writes and not writable(value or following):
                        return False
                    if not value:
                        index += 1   # the value is the next word
                    break
        elif token.startswith('-') and len(token) > 1:
            if token in refused:
                return False
            if token in writes:
                if not writable(following):
                    return False
                index += 2; continue
        else:
            positional.append(token)
        index += 1
    # pdftotext <input> [<output>]: the output is a file under output/, or - for standard output.
    if program == 'pdftotext' and len(positional) >= 2 and not writable(positional[-1]):
        return False
    return True


def check_observed(observed):
    """True when the Bash call a source was saved by runs no shell or interpreter and writes only under output/."""
    if not isinstance(observed, str) or HIDDEN.search(observed) or '`' in observed or re.search(r'\$\(|<\(|>\(', observed):
        return False
    for segment in source_evidence.segments(observed):
        try:
            tokens = _tokens(segment)
        except ValueError:
            return False
        tokens, safe = _redirects(tokens)
        at, program = _program(tokens)
        if not safe or program in SHELLS:
            return False
        if program == 'tee' and not all(_output_path(token) or token.startswith('-') for token in tokens[at + 1:]):
            return False
    return True


def _cap(group, key, kind):
    return FIELD_CAPS.get((group, key), IDENTIFIER_CAP if kind in ('identifier', 'term') else DEFAULT_TEXT_CAP)


def _kind(group, key):
    if (group, key) in VERBATIM: return 'verbatim'
    if (group, key) in COMMANDS: return 'command'
    if (group, key) in TERMS: return 'term'
    if (group, key) == ('files', 'location'): return 'location'
    if (group, key) in IDENTIFIERS: return 'identifier'
    return 'text'


def fields(document, trusted, reveal_activities):
    """[(group, index, key, kind, value)] for every string the agent wrote in a deposit-form document."""
    found = []
    for group, rows in document.items():
        if group.endswith('_edges') or group in REVEAL_GROUPS or not isinstance(rows, list):
            continue
        for index, node in enumerate(rows):
            if not isinstance(node, dict) or (group == 'activities' and node.get('id') in reveal_activities):
                continue
            for key, value in node.items():
                if key == 'id':
                    continue
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if not isinstance(item, str) or REFERENCE.match(item) or item in trusted:
                        continue
                    found.append((group, index, key, _kind(group, key), item))
    return found


def reveal_activities(document):
    """Ids of the Activities REVEAL wrote: the run that generated the account and its Claims (acceptance replaces any
    the agent wrote), never one that wrote a saved source."""
    nodes = [node for group in ('scientific_accounts', 'claims') for node in document.get(group, []) if isinstance(node, dict)]
    return {node.get('was_generated_by') for node in nodes} - source_evidence.source_activities(document) - {None}


def trusted_strings(account):
    """What the requester wrote (the question and the claim statements) and REVEAL's own question description."""
    from .agent_account import GAP_DESCRIPTION
    return {account.get('question'), GAP_DESCRIPTION} | {claim.get('statement') for claim in account.get('claims') or []} - {None}


def _prefixes(package):
    """{prefix: expansion} the account may use: the evidence package's and the pinned DAPPER release's."""
    allowed = dict(package.get('dapper_context', {}).get('prefixes', {})) if isinstance(package, dict) else {}
    allowed.update(schema_prefixes())
    return {key: value for key, value in allowed.items() if isinstance(key, str) and isinstance(value, str)}


_SCHEMA_PREFIXES = None


def schema_prefixes():
    """The prefixes the pinned DAPPER release declares, read once."""
    global _SCHEMA_PREFIXES
    if _SCHEMA_PREFIXES is None:
        import yaml
        path = CURRENT_DAPPER_SNAPSHOT / 'snapshot/schema/dapper.yaml'
        try:
            declared = yaml.safe_load(path.read_text()).get('prefixes', {})
        except (OSError, AttributeError):
            declared = {}
        _SCHEMA_PREFIXES = {key: (value['prefix_reference'] if isinstance(value, dict) else value)
                            for key, value in declared.items()}
    return _SCHEMA_PREFIXES


def _term_allowed(value, requested_terms, prefixes, source_texts):
    from .box_agent import TERM
    if value in requested_terms:
        return True
    if not TERM.fullmatch(value) or HIDDEN.search(value) or MARKUP.search(value):
        return False
    if '://' in value:
        if any(value.startswith(expansion) for expansion in prefixes.values() if expansion):
            return True
    elif value.split(':', 1)[0] in prefixes:
        return True
    return any(value in text for text in source_texts)


def fixed_checks(document, account, source_texts):
    """Raise Finding for the first fixed check the deposit-form `document` fails. `account` is the job's frozen
    account block (question, claims, package); `source_texts` the decoded text of every cited source file."""
    from .box_agent import MAX_ACCOUNT_CLAIMS, TRIPLE_SLOTS
    requested = account.get('claims') or []
    requested_terms = {claim[slot] for claim in requested for slot in TRIPLE_SLOTS if isinstance(claim.get(slot), str)}
    revealed = reveal_activities(document)
    found = fields(document, trusted_strings(account), revealed)
    # Size first: the other checks then work on bounded text.
    total = 0
    for group, index, key, kind, value in found:
        if len(value) > _cap(group, key, kind):
            raise Finding('oversized_field', 'account ' + _place(group, index, key))
        if kind in ('text', 'verbatim'):
            total += len(value)
    if total > MAX_FREE_TEXT:
        raise Finding('oversized_field', 'account free text')
    for group, index, key, kind, value in found:
        where = 'account ' + _place(group, index, key)
        if kind == 'text' and (LINK.search(value) or CODE.search(value) or MARKUP.search(value) or HIDDEN.search(value)
                               or INSTRUCTION.search(value)):
            raise Finding('unsafe_text', where)
        if kind == 'verbatim' and (HIDDEN.search(value) or INSTRUCTION.search(value)):
            raise Finding('unsafe_text', where)
        if kind == 'identifier' and (HIDDEN.search(value) or '\n' in value or LINK.search(value) or MARKUP.search(value)
                                     or CODE.search(value) or '`' in value or INSTRUCTION.search(value)):
            raise Finding('unsafe_text', where)
        if kind == 'location' and (not re.fullmatch(r'https?://\S+', value) or HIDDEN.search(value) or '`' in value or '<' in value
                                   or '>' in value or INSTRUCTION.search(value)):
            raise Finding('unsafe_text', where)
        if kind == 'command' and INSTRUCTION.search(value):
            raise Finding('unsafe_text', where)
    # Every Activity the agent wrote: those that saved sources, and any other it left in.
    for index, node in enumerate(document.get('activities', [])):
        if not isinstance(node, dict) or node.get('id') in revealed:
            continue
        if not check_command(node.get('command')):
            raise Finding('unsafe_command', 'account ' + _place('activities', index, 'command'))
        if 'observed_command' in node and not check_observed(node['observed_command']):
            raise Finding('unsafe_command', 'account ' + _place('activities', index, 'observed_command'))
    # Claims: what the job asked about, or (when it asked about none) the agent's own, bounded.
    listed = [node for node in document.get('propositions', []) if isinstance(node, dict)]
    propositions = {node.get('id'): node for node in listed}
    claims = [node for node in document.get('claims', []) if isinstance(node, dict)]
    if len(claims) > MAX_CLAIMS:
        raise Finding('unrequested_claim', 'account claims')
    if requested:
        ids = {claim.get('proposition_id') for claim in requested}
        statements = {claim.get('statement') for claim in requested}
        for index, node in enumerate(listed):
            if node.get('id') not in ids and node.get('statement') not in statements:
                raise Finding('unrequested_claim', 'account ' + _place('propositions', index, 'statement'))
        for index, node in enumerate(claims):
            target = propositions.get(node.get('proposition'))
            if node.get('proposition') not in ids and (target is None or target.get('statement') not in statements):
                raise Finding('unrequested_claim', 'account ' + _place('claims', index, 'proposition'))
    else:
        if len(claims) > MAX_ACCOUNT_CLAIMS:
            raise Finding('unrequested_claim', 'account claims')
        about = {node.get('proposition') for node in claims}
        for index, node in enumerate(listed):
            if node.get('id') not in about:
                raise Finding('unrequested_claim', 'account ' + _place('propositions', index, 'statement'))
    prefixes = _prefixes(account.get('package'))
    for index, node in enumerate(listed):
        for slot in TRIPLE_SLOTS:
            value = node.get(slot)
            if value is not None and not (isinstance(value, str) and _term_allowed(value, requested_terms, prefixes, source_texts)):
                raise Finding('unknown_entity', 'account ' + _place('propositions', index, slot))


# --- classifier -----------------------------------------------------------------------------------------------------

class Classifier:
    """A loaded scorer. `encode(text)` gives (token ids, [(char start, char end)]) without special tokens, `score(windows)`
    the probability that each window of ids is an attack, and `content_tokens` how many ids one window may hold. Tests
    install a fake with the same three members."""
    def __init__(self, encode, score, content_tokens, name, summary=None):
        self.encode, self.score, self.content_tokens, self.name = encode, score, content_tokens, name
        self.summary = summary or {}


_CLASSIFIER = None
_INSTALL = threading.Lock()


def install(classifier):
    """Make `classifier` (or None) the process's classifier. Returns the one it replaces."""
    global _CLASSIFIER
    with _INSTALL:
        previous, _CLASSIFIER = _CLASSIFIER, classifier
    return previous


def installed():
    return _CLASSIFIER


def deployed():
    """True outside development and test: there the screen never runs without its model."""
    return setting('REVEAL_ENVIRONMENT', 'development') not in ('development', 'test')


def model_dir():
    return Path(setting('REVEAL_PROMPT_GUARD_DIR') or DEFAULT_MODEL_DIR)


def threshold():
    value = float(setting('REVEAL_PROMPT_GUARD_THRESHOLD') or DEFAULT_THRESHOLD)
    if not 0 < value < 1 or not math.isfinite(value):
        raise ValueError('REVEAL_PROMPT_GUARD_THRESHOLD must be between 0 and 1')
    return value


def max_windows():
    value = int(setting('REVEAL_PROMPT_GUARD_MAX_WINDOWS') or DEFAULT_MAX_WINDOWS)
    if value < 1:
        raise ValueError('REVEAL_PROMPT_GUARD_MAX_WINDOWS must be positive')
    return value


def windows(classifier, text):
    """[(ids, char start, char end)] covering `text` in windows of classifier.content_tokens ids, neighbours sharing
    OVERLAP ids (a quarter of a window, for windows under 256 ids). Empty for text with no tokens."""
    ids, offsets = classifier.encode(text)
    size = classifier.content_tokens
    step = max(1, size - min(OVERLAP, size // 4))
    found, start = [], 0
    while start < len(ids):
        end = min(len(ids), start + size)
        found.append((list(ids[start:end]), offsets[start][0], offsets[end - 1][1]))
        if end == len(ids):
            break
        start += step
    return found


def _line_finder(text):
    """A function from a character span of `text` to 'line=N' or 'line=N-M', counted as grep -n counts."""
    breaks = [index for index, char in enumerate(text) if char == '\n']

    def lines(start, end):
        first = bisect_right(breaks, start - 1) + 1
        last = bisect_right(breaks, max(start, end - 1) - 1) + 1
        return f'line={first}' if first == last else f'line={first}-{last}'
    return lines


MARKUP_TOKEN = re.compile(r'<!--([\s\S]*?)-->|<!\[CDATA\[([\s\S]*?)\]\]>|<\?[^<>]{0,2000}\?>|<!DOCTYPE[^<>]{0,2000}>'
                          r'|</?[A-Za-z_][\w:.-]*(?:\s[^<>]{0,2000})?/?>', re.IGNORECASE)
ATTRIBUTE_VALUE = re.compile(r'"([^"]*)"|\'([^\']*)\'')


def _unmarked(text):
    """Markup's element names set aside, keeping every line break so line numbers still match the file. What a reader
    never sees but a model may read stays in: comment bodies, CDATA and attribute values."""
    def replace(match):
        whole = match.group(0)
        if match.group(1) is not None or match.group(2) is not None:
            kept = match.group(1) if match.group(1) is not None else match.group(2)
        elif whole.startswith(('<?', '<!')):
            kept = ''
        else:
            kept = ' '.join(first or second for first, second in ATTRIBUTE_VALUE.findall(whole))
        return ' ' + kept + ' ' + '\n' * (whole.count('\n') - kept.count('\n'))
    return MARKUP_TOKEN.sub(replace, text)


MARKUP_TYPES = ('application/xml', 'text/html')


def source_texts(papers, kept):
    """[(path, media type, text)] of the cited source files that are text. A PDF's text is the converted file it cites."""
    found = []
    for paper in papers:
        data = kept.get(paper['sha256'])
        if data is None or paper.get('media_type') == 'application/pdf':
            continue
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        found.append((paper['path'], paper.get('media_type'), text))
    return found


# Tools whose results REVEAL wrote (its draft and lint tools, and the agent's own file edits), and the kit files REVEAL
# hands the agent: their text is instructions REVEAL wrote for the model, which the classifier would rightly flag.
OWN_TOOLS = frozenset({'lint_account', 'write_account_draft', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'TodoWrite'})
KIT_FILES = ('account/SKILL.md', 'account/authoring-schema-excerpt.yaml')


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value: yield from _strings(item)
    elif isinstance(value, dict):
        if value.get('type') == 'base64':
            return   # an image or document block: bytes, not text
        for key, item in value.items():
            if key not in ('type', 'tool_use_id', 'id', 'sha256', 'media_type', 'mime_type'):
                yield from _strings(item)


def _artifact(root, descriptor):
    if not isinstance(descriptor, dict):
        return None
    path = (root / str(descriptor.get('path', ''))).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != descriptor.get('sha256'):
        raise ScreenError('A captured tool call does not match its recorded checksum')
    return json.loads(raw)


def tool_texts(calls, ledger_root):
    """[(ledger sequence, tool, text)] of every non-Bash tool result in the ledger, except REVEAL's own tools and the
    kit files REVEAL hands the agent."""
    root = Path(ledger_root).resolve()
    found = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        tool = str(call.get('tool') or '')
        name = tool.rsplit('__', 1)[-1] if tool.startswith('mcp__') else tool
        if name == 'Bash' or name in OWN_TOOLS:
            continue
        if name == 'Read':
            request = _artifact(root, call.get('request')) or {}
            arguments = request.get('arguments') if isinstance(request, dict) else None
            target = arguments.get('file_path') if isinstance(arguments, dict) else None
            if isinstance(target, str) and target.endswith(KIT_FILES):
                continue
        response = _artifact(root, call.get('response'))
        if response is None:
            continue
        text = '\n'.join(_strings(response))
        if text.strip():
            found.append((call.get('sequence'), name, text))
    return found


def score_texts(classifier, texts, limit):
    """(highest score, its location, windows scored) for [(location prefix, text, line-numbered?)]. Raises Finding at the
    first window at or over `limit`, and for more text than one screen may check."""
    if sum(len(text) for _, text, _ in texts) > MAX_SCREEN_CHARACTERS:
        raise Finding('screen_too_large', f'more than {MAX_SCREEN_CHARACTERS:,} characters to screen')
    planned = [(where, text, numbered, windows(classifier, text)) for where, text, numbered in texts]
    count = sum(len(found) for *_, found in planned)
    if count > max_windows():
        raise Finding('screen_too_large', f'{count:,} windows to score, over {max_windows():,}')
    best, best_where = 0.0, None
    for where, text, numbered, found in planned:
        lines = _line_finder(text) if numbered else None
        for start in range(0, len(found), 16):
            batch = found[start:start + 16]
            scores = classifier.score([ids for ids, _, _ in batch])
            for (ids, first, last), score in zip(batch, scores):
                score = float(score)
                location = f'{where} {lines(first, last)}' if lines else where
                if score >= limit:
                    raise Finding('prompt_injection', location, round(score, 4))
                if score > best:
                    best, best_where = score, location
    return round(best, 4), best_where, count


# --- the screen -----------------------------------------------------------------------------------------------------

def screen(document, account, papers, kept, calls, ledger_root, *, classifier=None):
    """The verdict on one deposit-form account: {'outcome', 'check', 'location', 'score', 'threshold', 'classifier',
    'windows', 'texts', 'seconds'}. Never contains the text it judged.

    `account` is the job's frozen account block, `papers` the gate's summary of the saved sources with `kept` their
    bytes by sha256, `calls` the run's ledger entries with request and response paths relative to `ledger_root`.
    Raises ScreenError in a deployed process that has no classifier."""
    started = time.monotonic()
    classifier = classifier or installed()
    if classifier is None and deployed():
        raise ScreenError('The prompt-injection classifier is not loaded; a deployed process loads it when it starts')
    limit = threshold()
    verdict = {'outcome': 'passed', 'check': None, 'location': None, 'score': None, 'threshold': limit,
               'classifier': classifier.name if classifier else 'not_loaded', 'windows': 0, 'texts': 0}
    sources = source_texts(papers, kept)
    try:
        fixed_checks(document, account, [text for _, _, text in sources])
        if classifier is not None:
            texts = [(path, _unmarked(text) if media in MARKUP_TYPES else text, True) for path, media, text in sources]
            texts += [(f'ledger call {sequence} ({tool})', text, True) for sequence, tool, text in tool_texts(calls, ledger_root)]
            # The account's prose and quotes, each once (a ValueLocator repeats its EvidenceItem's snippet).
            texts += [('account ' + _place(group, index, key), value, False)
                      for group, index, key, kind, value in fields(document, trusted_strings(account), reveal_activities(document))
                      if kind in ('text', 'command', 'identifier', 'location') or (group, key) == ('evidence_items', 'snippet')]
            verdict['texts'] = len(texts)
            verdict['score'], verdict['location'], verdict['windows'] = score_texts(classifier, texts, limit)
    except Finding as finding:
        verdict.update(outcome='withheld', check=finding.check, location=finding.location)
        if finding.score is not None:
            verdict['score'] = finding.score
    verdict['seconds'] = round(time.monotonic() - started, 3)
    return verdict


def withheld(verdict):
    """The deposit outcome of a withheld account: never sent. One plain line for its requester, with where, not what."""
    where = verdict.get('location')
    return {'status': 'withheld_unsafe', 'reason': verdict['check'],
            'detail': (f"The account was not stored in the claims repository: {WHY[verdict['check']]}"
                       + (f' ({where})' if where else '') + '.')[:500]}


# --- the model ------------------------------------------------------------------------------------------------------

def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def pinned(lock):
    """True when the lock names a revision and a sha256 for every model file."""
    files = lock.get('files') if isinstance(lock.get('files'), dict) else {}
    return (isinstance(lock.get('revision'), str) and re.fullmatch(r'[0-9a-f]{40}', lock['revision']) is not None
            and set(MODEL_FILES) <= set(files)
            and all(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest) for digest in files.values()))


def check_files(directory, lock):
    """Raise ScreenError unless `directory` holds every file the lock pins, byte for byte."""
    directory = Path(directory)
    if not pinned(lock):
        raise ScreenError(f'{LOCK.name} pins no revision or file checksums. Fetch and pin the model with '
                          'scripts/platform_bundle.py --fetch-assets --pin-prompt-guard (see the lock file)')
    if not directory.is_dir():
        raise ScreenError(f'The prompt-injection model directory {directory} is missing')
    for name, digest in sorted(lock['files'].items()):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ScreenError(f'The prompt-injection model file {name} is missing from {directory}')
        if _sha256(path) != digest:
            raise ScreenError(f'The prompt-injection model file {name} differs from its pinned checksum')


def load_model(directory, lock):
    """A Classifier over the ONNX model in `directory`: one InferenceSession with one thread, warmed once."""
    import numpy
    import onnxruntime
    from tokenizers import Tokenizer
    directory = Path(directory)
    config = json.loads((directory / 'config.json').read_text())
    labels = {int(key): value for key, value in (config.get('id2label') or {}).items()}
    wanted = lock.get('malicious_label', 'LABEL_1')
    malicious = next((index for index, label in labels.items() if label == wanted), None)
    if malicious is None:
        raise ScreenError(f'The model config has no label {wanted}')
    tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
    tokenizer.no_truncation(); tokenizer.no_padding()
    # The special tokens the model expects around a text, as its own tokenizer adds them.
    plain, framed = tokenizer.encode('a', add_special_tokens=False).ids, tokenizer.encode('a', add_special_tokens=True).ids
    at = next((index for index in range(len(framed)) if framed[index:index + len(plain)] == plain), None)
    if at is None:
        raise ScreenError('The tokenizer frames text in a way the screen does not recognise')
    prefix, suffix = framed[:at], framed[at + len(plain):]
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 1; options.inter_op_num_threads = 1
    options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    session = onnxruntime.InferenceSession(str(directory / 'model.onnx'), options, providers=['CPUExecutionProvider'])
    inputs = [item.name for item in session.get_inputs()]
    unknown = set(inputs) - {'input_ids', 'attention_mask', 'token_type_ids'}
    if unknown:
        raise ScreenError('The model asks for inputs the screen does not supply: ' + ', '.join(sorted(unknown)))
    run = threading.Lock()   # one session per process; requests in other threads take turns

    def encode(text):
        encoding = tokenizer.encode(text, add_special_tokens=False)
        return encoding.ids, encoding.offsets

    def score(batch):
        scores = []
        for ids in batch:
            framed_ids = numpy.array([prefix + list(ids) + suffix], dtype=numpy.int64)
            feeds = {'input_ids': framed_ids, 'attention_mask': numpy.ones_like(framed_ids),
                     'token_type_ids': numpy.zeros_like(framed_ids)}
            with run:
                logits = session.run(None, {name: feeds[name] for name in inputs})[0][0].astype(numpy.float64)
            exponent = numpy.exp(logits - logits.max())
            scores.append(float(exponent[malicious] / exponent.sum()))
        return scores

    classifier = Classifier(encode, score, WINDOW_TOKENS - len(prefix) - len(suffix),
                            'prompt-guard-2-22m@' + str(lock.get('revision', ''))[:12])
    started = time.monotonic()
    score([encode('Warm-up input for the deposit screen.')[0]])
    classifier.summary = {'revision': lock.get('revision'), 'warm_seconds': round(time.monotonic() - started, 3)}
    return classifier


def verify_model(directory=None, lock_path=None, *, loader=None):
    """Check the model folder against the lock and load it once. Returns the Classifier; raises ScreenError (or the
    runtime's own error for a file it cannot load). The image build runs this, so an image without the model, or with
    other bytes, is never built."""
    lock = json.loads(Path(lock_path or LOCK).read_text())
    directory = Path(directory) if directory else model_dir()
    check_files(directory, lock)
    return (loader or load_model)(directory, lock)


def startup():
    """Load and warm the process's classifier when it starts. A deployed process without a verified model raises, so
    it never starts; elsewhere a missing model is logged and only the fixed checks run."""
    threshold(); max_windows()   # a malformed setting stops the process here, not a job later
    if installed() is not None:
        return installed()
    directory = model_dir()
    if deployed():
        install(verify_model(directory))
        log.info('Deposit screen: prompt-injection model loaded from %s', directory)
        return installed()
    if not directory.exists():
        log.warning('Deposit screen: no prompt-injection model at %s; development runs the fixed checks only', directory)
        return None
    try:
        install(verify_model(directory))
    except Exception as exc:
        log.warning('Deposit screen: the model at %s was not loaded (%s); development runs the fixed checks only',
                    directory, type(exc).__name__)
    return installed()
