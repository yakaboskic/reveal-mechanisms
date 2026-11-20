#!/usr/bin/env python3
"""Rerun the commands an account job recorded for its sources, and say whether each file comes out the same.

An account job fetches each paper with a command (curl, wget) and converts it with another (pdftotext, xmllint),
saving the files under output/sources/. The job result lists every saved file in its account's `papers`: the path,
the URL, the sha256 REVEAL measured, the recorded `command` and the file it was converted from (`derived_from`).
This audit reruns each recorded command in a scratch directory on the machine running it, fetches before the
conversions that read them, and compares sha256s. It is not part of the deposit path: an account is deposited
whatever the audit later finds.

Each source is reported as one of:

  reproduced         the command wrote the same bytes;
  changed_upstream   a fetch wrote different bytes: the publisher's copy changed. Not a failure (REVEAL keeps the
                     original bytes). A conversion of a changed file whose original bytes were not available is
                     reported the same way;
  converter_differs  a conversion read the same bytes and wrote different ones: the converter here is not the
                     version the Box ran (compare with source_tools in the run's runtime.json). Not a failure;
  failed             the command exited non-zero, timed out or wrote no file (exit_status says which), or the file
                     it converts could not be had;
  refused            the command is not one this audit runs (below). Nothing was run.

Only commands the deposit screen accepts are run (deposit_screen.command_refusal): one plain curl, wget, pdftotext or
xmllint command, that program as the first word, writing only under output/, with only the options
deposit_screen.OPTIONS lists for it (so nothing sends data, reads a config, variable or DTD file, or loads entities),
and no argument naming a local file outside output/ (an absolute path, ~, .., @file) or a URL that is not http(s). Commands run without a shell, with every output/ path (output/,
./output/, /reveal/output/, /reveal/trusted/output/) rewritten into the scratch directory, the scratch directory as
working directory and HOME (so no ~/.curlrc, ~/.wgetrc or ~/.netrc is read), and the shell redirects the screen
allows (> >> 2> 2>&1 &> <) applied by this script. The environment is PATH, HOME and any proxy or CA bundle
settings. The commands still reach the network: run the audit where that is acceptable.

Where the sources come from:

  --result FILE             an exported job result: the body of GET /v1/jobs/{id}, of GET /v1/jobs/{id}/account,
                            or the account block itself;
  --job ID                  the job's owner route (GET /v1/jobs/{id}/account), with REVEAL_API_URL and
                            REVEAL_API_KEY (or --handoff FILE with api_url and api_key). The original bytes of a
                            file that changed upstream are then fetched from /v1/jobs/{id}/account/sources/{sha256},
                            so its conversion is checked against the same input.
  --originals DIR           original bytes, one file per source named by its sha256 (used the same way).

Exit status: 0 when nothing failed or was refused, 1 otherwise, 2 when the sources cannot be read.

  python3 scripts/audit_account_sources.py --result job.json
  python3 scripts/audit_account_sources.py --job 95d0bc18-... --json
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.deposit_screen import DEVICES, _tokens, command_refusal  # noqa: E402

DEFAULT_TIMEOUT = 300
# An output/ path as the agent's commands name it (deposit_screen._output_path), alone or as the value of an option:
# --output=PATH, or a clustered short option such as -sSLoPATH.
OUTPUT_PATH = re.compile(r'^(--[A-Za-z0-9-]+=|-[A-Za-z]+)?(?:\./|/reveal/(?:trusted/)?)?output/(.*)$')
# Settings a fetch may need on the auditor's network, passed through when set.
NETWORK_ENVIRONMENT = ('http_proxy', 'https_proxy', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
                       'all_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'CURL_CA_BUNDLE')
REPORTED = ('reproduced', 'changed_upstream', 'converter_differs', 'failed', 'refused')


class Refused(ValueError):
    """A recorded command this audit will not run. The message says why."""


@dataclass
class Invocation:
    """One command to run: the argument vector (no shell), where it runs, and where its streams go. Each stream target
    is None (discarded), a path with a mode ('w' or 'a'), or for stderr 'stdout'."""
    argv: list
    cwd: Path
    env: dict
    stdin: Path | None = None
    stdout: tuple | None = None
    stderr: tuple | str | None = None
    timeout: float = DEFAULT_TIMEOUT


@dataclass
class Result:
    path: str
    kind: str
    command: str | None
    recorded_sha256: str | None
    status: str = 'failed'
    sha256: str | None = None
    size_bytes: int | None = None
    exit_status: int | None = None
    note: str | None = None
    derived_from: str | None = None
    used_original_input: bool = False


def _rewrite(token, scratch):
    """The token with an output/ path moved into the scratch directory; a path that climbs out with .. is refused."""
    match = OUTPUT_PATH.match(token)
    if not match:
        return token
    rest = match.group(2)
    if any(part == '..' for part in rest.split('/')):
        raise Refused(f'{token!r} climbs out of output/')
    return (match.group(1) or '') + str(scratch / 'output' / rest)


def _redirect(token, target, descriptor, streams, scratch):
    """Apply one shell redirect the deposit screen allows to `streams` ({0: stdin, 1: stdout, 2: stderr})."""
    def into_output(word):
        match = OUTPUT_PATH.match(word)
        return Path(_rewrite(word, scratch)) if match and not match.group(1) else None
    if token == '<':
        source = into_output(target)
        if descriptor not in (None, 0) or source is None:
            raise Refused('it reads standard input from somewhere other than output/')
        streams[0] = source
        return
    if token == '>&':
        # 2>&1: standard error goes where standard output goes now. Anything else is refused.
        if (descriptor or 1) != 2 or target != '1':
            raise Refused(f'the redirect {token}{target} is not one this audit applies')
        # Where standard output goes now: discarded, or (refused below if it moves later) the same file.
        streams[2] = 'stdout' if streams[1] is not None else None
        streams['dup'] = streams[1] is not None
        return
    if token not in ('>', '>>', '>|', '&>'):
        raise Refused(f'the redirect {token} is not one this audit applies')
    if target in DEVICES:
        destination = None   # only the file under output/ is compared
    else:
        path = into_output(target)
        if path is None:
            raise Refused(f'it writes {target!r}, which is not under output/')
        destination = (path, 'a' if token == '>>' else 'w')
    numbers = (1, 2) if token == '&>' else (descriptor or 1,)
    if any(number not in (1, 2) for number in numbers):
        raise Refused('it redirects a descriptor other than standard output or error')
    if 1 in numbers and streams.get('dup'):
        raise Refused('it redirects standard output after 2>&1')
    for number in numbers:
        streams[number] = destination
    if numbers == (1, 2) and destination is not None:
        streams[2] = 'stdout'


def plan(command, scratch, *, timeout=DEFAULT_TIMEOUT):
    """The Invocation that reruns one recorded command in `scratch`, or Refused. Which commands may run is the deposit
    screen's rule (deposit_screen.command_refusal); this only moves their paths and redirects into `scratch`."""
    if not isinstance(command, str) or not command.strip():
        raise Refused('no command was recorded')
    reason = command_refusal(command)
    if reason:
        raise Refused(reason)
    tokens = _tokens(command)
    argv, streams, index = [tokens[0]], {0: None, 1: None, 2: None}, 1
    while index < len(tokens):
        token = tokens[index]
        descriptor = None
        if re.fullmatch(r'\d+', token) and index + 1 < len(tokens) and tokens[index + 1][:1] in '<>':
            descriptor, index = int(token), index + 1
            token = tokens[index]
        if token and not set(token) - set('<>&|;'):
            _redirect(token, tokens[index + 1] if index + 1 < len(tokens) else '', descriptor, streams, scratch)
            index += 2
            continue
        argv.append(_rewrite(token, scratch)); index += 1
    environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(scratch),
                   **{name: os.environ[name] for name in NETWORK_ENVIRONMENT if os.environ.get(name)}}
    return Invocation(argv, scratch, environment, stdin=streams[0], stdout=streams[1], stderr=streams[2], timeout=timeout)


def run(invocation):
    """Run one Invocation without a shell; its exit status, or None when it timed out or could not start."""
    handles = []

    def opened(target, mode='rb'):
        if target is None:
            return subprocess.DEVNULL
        path, kind = (target, mode) if isinstance(target, Path) else target
        handle = open(path, kind + 'b' if kind in ('w', 'a') else kind)
        handles.append(handle)
        return handle
    try:
        stdout = opened(invocation.stdout)
        stderr = subprocess.STDOUT if invocation.stderr == 'stdout' else opened(invocation.stderr)
        stdin = opened(invocation.stdin) if invocation.stdin else subprocess.DEVNULL
        done = subprocess.run(invocation.argv, cwd=invocation.cwd, env=invocation.env, stdin=stdin, stdout=stdout, stderr=stderr,
                              timeout=invocation.timeout, check=False)
        return done.returncode
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        for handle in handles:
            handle.close()


def papers_of(value):
    """The account's `papers` from a job result, the owner route's body, or an account block."""
    for path in (('papers',), ('account', 'papers'), ('result', 'account', 'papers'), ('job', 'result', 'account', 'papers')):
        node = value
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, list):
            return [paper for paper in node if isinstance(paper, dict)]
    raise ValueError('No account papers found: pass a job result, the body of /v1/jobs/{id}/account or an account block')


def _digest(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None, None
    return hashlib.sha256(data).hexdigest(), len(data)


def _safe_path(value):
    return isinstance(value, str) and value.startswith('sources/') and '\\' not in value and \
        all(part not in ('', '.', '..') for part in value.split('/'))


def audit(papers, scratch, *, runner=run, original=None, timeout=DEFAULT_TIMEOUT):
    """[Result] for each saved source, fetches before the conversions that read them. `original(sha256)` returns the
    bytes a file had when the job saved it, or None."""
    scratch = Path(scratch)
    by_path, results, done = {}, [], {}
    for paper in papers:
        path = paper.get('path')
        if path in by_path:
            continue   # several Files may name one saved file: it is rerun once
        by_path[path] = paper
    pending = list(by_path.values())
    while pending:
        ready = [paper for paper in pending if not paper.get('derived_from') or paper['derived_from'] in done]
        if not ready:
            for paper in pending:
                results.append(Result(paper.get('path'), 'conversion', paper.get('command'), paper.get('sha256'), 'failed',
                                      derived_from=paper.get('derived_from'),
                                      note='the file it was converted from is not among the sources (or they convert each other)'))
            break
        for paper in ready:
            pending.remove(paper)
            result = _one(paper, scratch, by_path, done, runner, original, timeout)
            done[paper.get('path')] = result
            results.append(result)
    return results


def _one(paper, scratch, by_path, done, runner, original, timeout):
    path, parent = paper.get('path'), paper.get('derived_from')
    result = Result(path, 'conversion' if parent else 'fetch', paper.get('command'), paper.get('sha256'), derived_from=parent)
    if not _safe_path(path):
        result.status, result.note = 'refused', 'its path is not a file under sources/'
        return result
    try:
        invocation = plan(paper.get('command'), scratch, timeout=timeout)
    except Refused as exc:
        result.status, result.note = 'refused', str(exc)
        return result
    target = scratch / 'output' / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    input_changed = False
    if parent:
        source = done[parent]
        recorded = by_path[parent].get('sha256')
        if not _safe_path(parent):
            result.note = 'the file it was converted from is not a file under sources/'
            return result
        if source.sha256 != recorded:
            # Convert what the job converted when its original bytes are at hand; otherwise what was fetched now.
            data = original(recorded) if original and isinstance(recorded, str) else None
            if data is not None and hashlib.sha256(data).hexdigest() == recorded:
                (scratch / 'output' / parent).parent.mkdir(parents=True, exist_ok=True)
                (scratch / 'output' / parent).write_bytes(data)
                result.used_original_input = True
            elif source.sha256 is None:
                result.note = 'the file it was converted from could not be fetched, and its original bytes are not available'
                return result
            else:
                input_changed = True
    result.exit_status = runner(invocation)
    result.sha256, result.size_bytes = _digest(target)
    if result.exit_status != 0:
        result.status = 'failed'
        result.note = 'timed out or could not start' if result.exit_status is None else f'exited with status {result.exit_status}'
        result.sha256 = result.size_bytes = None
    elif result.sha256 is None:
        result.status, result.note = 'failed', 'it wrote no file at output/' + path
    elif result.sha256 == paper.get('sha256'):
        result.status = 'reproduced'
    elif not parent:
        result.status = 'changed_upstream'
    elif input_changed:
        result.status, result.note = 'changed_upstream', 'converted from a file that changed upstream'
    else:
        result.status = 'converter_differs'
        result.note = 'same input, different output: this converter is not the version the Box ran'
    return result


class Client:
    """The job owner's routes. A redirect to object storage is followed without the API key."""
    def __init__(self, url, key, opener=None):
        self.url, self.key = url.rstrip('/'), key
        self.opener = opener or urllib.request.build_opener(_NoRedirect)

    def get(self, path):
        request = urllib.request.Request(self.url + path, headers={'Authorization': 'Bearer ' + self.key})
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and error.headers.get('Location'):
                location = urllib.parse.urljoin(self.url + path, error.headers['Location'])
                with self.opener.open(urllib.request.Request(location), timeout=120) as response:
                    return response.read()
            raise

    def papers(self, job_id):
        return papers_of(json.loads(self.get(f'/v1/jobs/{urllib.parse.quote(job_id, safe="")}/account')))

    def original(self, job_id):
        def read(sha256):
            if not re.fullmatch(r'[a-f0-9]{64}', sha256):
                return None
            try:
                return self.get(f'/v1/jobs/{urllib.parse.quote(job_id, safe="")}/account/sources/{sha256}')
            except (urllib.error.URLError, OSError):
                return None
        return read


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def from_directory(directory):
    def read(sha256):
        path = Path(directory) / sha256
        return path.read_bytes() if re.fullmatch(r'[a-f0-9]{64}', sha256) and path.is_file() else None
    return read


def credentials(handoff):
    if handoff:
        value = json.loads(Path(handoff).read_text())
        return value['api_url'], value['api_key']
    return os.environ.get('REVEAL_API_URL'), os.environ.get('REVEAL_API_KEY')


def describe(result):
    sha = (result.sha256 or '—')[:12]
    head = f'{result.status:<18} {result.path}  ({result.kind}'
    head += f', exit {result.exit_status}' if result.exit_status not in (None, 0) else ''
    head += f', sha256 {sha} vs recorded {str(result.recorded_sha256 or "—")[:12]})' if result.status not in ('refused',) else ')'
    return head + (f'\n    {result.note}' if result.note else '') + ('\n    converted the original bytes' if result.used_original_input else '')


def main(argv=None, *, runner=run, client=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--result', type=Path, help='an exported job result, owner-route account body, or account block (JSON)')
    source.add_argument('--job', help="the job id, read through its owner's route")
    parser.add_argument('--handoff', type=Path, help='JSON file with api_url and api_key (default: REVEAL_API_URL, REVEAL_API_KEY)')
    parser.add_argument('--originals', type=Path, help='directory of original source bytes, each named by its sha256')
    parser.add_argument('--scratch', type=Path, help='an empty or new directory to rerun the commands in (default: a temporary one)')
    parser.add_argument('--keep', action='store_true', help='keep the scratch directory and its files')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT, help='seconds each command may run (default %(default)s)')
    parser.add_argument('--json', action='store_true', help='print the report as JSON')
    args = parser.parse_args(argv)
    original = from_directory(args.originals) if args.originals else None
    try:
        if args.result:
            papers = papers_of(json.loads(args.result.read_text()))
        else:
            if client is None:
                url, key = credentials(args.handoff)
                if not url or not key:
                    print('Pass --handoff <file.json> or set REVEAL_API_URL and REVEAL_API_KEY.', file=sys.stderr); return 2
                client = Client(url, key)
            papers = client.papers(args.job)
            original = original or client.original(args.job)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f'Cannot read the sources: {exc}', file=sys.stderr); return 2
    if args.scratch:
        if args.scratch.exists() and any(args.scratch.iterdir()):
            print(f'{args.scratch} is not empty', file=sys.stderr); return 2
        args.scratch.mkdir(parents=True, exist_ok=True)
        scratch = args.scratch.resolve()
    else:
        scratch = Path(tempfile.mkdtemp(prefix='reveal-source-audit-')).resolve()
    try:
        results = audit(papers, scratch, runner=runner, original=original, timeout=args.timeout)
    finally:
        if not args.keep and not args.scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    counts = {status: sum(result.status == status for result in results) for status in REPORTED}
    if args.json:
        print(json.dumps({'sources': [vars(result) for result in results],
                          'counts': counts, 'scratch': str(scratch) if args.keep or args.scratch else None}, indent=2))
    else:
        for result in results:
            print(describe(result))
        print(' · '.join(f'{count} {status.replace("_", " ")}' for status, count in counts.items()))
        if args.keep:
            print(f'Files kept in {scratch}')
    return 1 if counts['failed'] or counts['refused'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
