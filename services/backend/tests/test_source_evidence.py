"""The source model of an account job, offline: the agent saves each paper with a command into output/sources/, records
the command as a DAPPER Activity and the bytes as a File, and quotes the File. REVEAL checks the record against the
files and the tool ledger (source_evidence). The end-to-end run through lint, gate and deposit is in
test_scientific_account_lint; these are the rules one at a time.

The fixture below is shared with that test."""
import hashlib
import os
from copy import deepcopy
from pathlib import Path

import pytest

from reveal_backend import source_evidence
from reveal_backend.box_mcp import Ledger

XML_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML'
PDF_URL = 'https://journals.example.org/doi/pdf/10.1234/jmet.2012.5'
XML_QUOTE = 'Hepatocyte-specific deletion of Lepr reduced insulin-stimulated AKT phosphorylation by 42% in liver.'
XML = ('<?xml version="1.0" encoding="UTF-8"?>\n<article>\n<front><article-meta>\n'
       '<article-id pub-id-type="pmcid">PMC3491803</article-id>\n'
       '<title-group><article-title>Leptin receptor loss and hepatic insulin signalling in β-cells</article-title></title-group>\n'
       '</article-meta></front>\n<body><sec><title>Results</title>\n'
       f'<p>{XML_QUOTE}</p>\n'
       '<p>Glucose production rose. Later, glucose production rose again in knockout mice.</p>\n'
       '</sec></body>\n</article>\n').encode()
# What pdftotext -layout writes: a sentence wrapped across lines 3-4 with a run of spaces, a form feed at a page break
# (grep -n does not count it as a line), and a sentence printed twice on one line.
TEXT_QUOTE = ('In knockout mice, fasting glucose production was 35% higher than in littermate controls after a '
              'hyperinsulinaemic clamp.')
REPEATED = 'Glucose output rose in every knockout mouse we studied.'
TEXT = ('Hepatic insulin resistance in LEPR-deficient β-cells\n'
        '\n'
        'In knockout mice, fasting glucose production was 35% higher than in\n'
        'littermate   controls after a hyperinsulinaemic clamp.\n'
        f'\fDiscussion\n{REPEATED} {REPEATED}\n').encode()
PDF = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj << /Type /Catalog >> endobj\ntrailer\n%%EOF\n'
FETCH_XML = f'curl -sSL -o output/sources/PMC3491803.xml {XML_URL}'
FETCH_PDF = f'curl -sSL -o /reveal/output/sources/jmet-2012.pdf "{PDF_URL}"'
CONVERT = 'pdftotext -layout output/sources/jmet-2012.pdf output/sources/jmet-2012.txt'
# The Bash calls as the agent ran them: the first is compound, so the Activity's command is part of it.
RAN = {'xml': 'mkdir -p output/sources && ' + FETCH_XML, 'pdf': FETCH_PDF, 'txt': CONVERT}
FILES = {'xml': ('sources/PMC3491803.xml', XML), 'pdf': ('sources/jmet-2012.pdf', PDF), 'txt': ('sources/jmet-2012.txt', TEXT)}


def bash(ledger, command, ok=True):
    """One Bash call, ledgered the way box_remote records Claude Code's built-in tools."""
    entry = ledger.start('Bash', {'command': command, 'description': 'Fetch a paper'}, None)
    ledger.finish(entry, {'type': 'tool_result', 'tool_use_id': 'toolu_' + str(entry['sequence']), 'content': '' if ok else 'curl: (22) 404',
                          'is_error': not ok}, 'completed' if ok else 'failed')
    return entry


def save_sources(output, ledger):
    """The files and the ledger of a run that fetched one XML paper and one PDF it converted to text, after a failed
    attempt at the PDF."""
    (output / 'sources').mkdir(parents=True, exist_ok=True)
    bash(ledger, FETCH_PDF.replace('jmet.2012.5', 'jmet.2012.6'), ok=False)
    for key in ('xml', 'pdf', 'txt'):
        path, data = FILES[key]
        (output / path).write_bytes(data)
        bash(ledger, RAN[key])


def activity(key, command, software):
    return {'id': f'urn:draft:activity-{key}', 'name': f'Save {FILES[key][0]}', 'command': command, 'software_name': software[0],
            'software_version': software[1], 'generated_at_time': '2026-11-19T10:0%d:00Z' % 'xml pdf txt'.split().index(key)}


def draft(proposition_id, gap_id, *, xml_snippet=XML_QUOTE, text_snippet=TEXT_QUOTE, text_context='Mouse liver after a clamp; line=3-4'):
    """The account the agent writes: its Activities, Files and provenance edges, and two EvidenceItems quoting the XML
    and the converted text."""
    return {
        'activities': [activity('xml', FETCH_XML, ('curl', '8.5.0')), activity('pdf', FETCH_PDF, ('curl', '8.5.0')),
                       activity('txt', CONVERT, ('pdftotext', '24.02.0'))],
        'files': [{'id': 'urn:draft:file-xml', 'filename': FILES['xml'][0], 'location': XML_URL},
                  {'id': 'urn:draft:file-pdf', 'filename': FILES['pdf'][0], 'location': PDF_URL},
                  {'id': 'urn:draft:file-txt', 'filename': FILES['txt'][0]}],
        'was_generated_by_edges': [{'subject': f'urn:draft:file-{key}', 'predicate': 'prov:wasGeneratedBy', 'object': f'urn:draft:activity-{key}'}
                                   for key in ('xml', 'pdf', 'txt')],
        'was_derived_from_edges': [{'subject': 'urn:draft:file-txt', 'predicate': 'prov:wasDerivedFrom', 'object': 'urn:draft:file-pdf'}],
        'used_edges': [{'subject': 'urn:draft:activity-txt', 'predicate': 'prov:used', 'object': 'urn:draft:file-pdf'}],
        'evidence_items': [
            {'id': 'urn:draft:evidence-1', 'target_proposition': proposition_id, 'direction': 'SUPPORTS', 'snippet': xml_snippet,
             'was_derived_from': ['urn:draft:file-xml'], 'explanation': 'The paper reports this directly in mouse liver.',
             'context': 'Mouse liver, hepatocyte-specific knockout; Results', 'reference': 'PMID:23154982'},
            {'id': 'urn:draft:evidence-2', 'target_proposition': proposition_id, 'direction': 'SUPPORTS', 'snippet': text_snippet,
             'was_derived_from': ['urn:draft:file-txt'], 'explanation': 'Raised glucose output is what impaired hepatic insulin action predicts.',
             'context': text_context}],
        'claims': [{'id': 'urn:draft:claim-1', 'proposition': proposition_id, 'direction': 'SUPPORTS', 'status': 'proposed',
                    'statement': 'Two mouse studies support the requester\'s claim.', 'has_evidence': ['urn:draft:evidence-1', 'urn:draft:evidence-2']}],
        'scientific_accounts': [{'id': 'urn:draft:account-1', 'name': 'LEPR and hepatic insulin signalling', 'question': gap_id,
                                 'context': 'Two papers, fetched and saved in this run.', 'component_claims': ['urn:draft:claim-1'],
                                 'closing_remarks': 'Mouse studies only; no human data were read.'}]}


@pytest.fixture
def run(tmp_path):
    output = tmp_path / 'output'
    ledger = Ledger(tmp_path / 'ledger', 'account-job', 1)
    save_sources(output, ledger)

    def check(document):
        return source_evidence.check(document, output, ledger.entries, ledger.root)
    return output, ledger, check


def messages(problems):
    return {where: message for where, message in problems}


def test_a_record_that_matches_the_files_and_the_ledger_binds_every_quote_to_its_exact_bytes(run):
    output, ledger, check = run
    problems, binding = check(draft('urn:p', 'urn:g'))
    assert problems == []
    files = binding['files']
    for key, (path, data) in FILES.items():
        bound = files[f'urn:draft:file-{key}']
        assert (bound['path'], bound['sha256'], bound['size_in_bytes']) == (path, hashlib.sha256(data).hexdigest(), len(data))
    assert [files[f'urn:draft:file-{key}']['mime_type'] for key in ('xml', 'pdf', 'txt')] == ['application/xml', 'application/pdf', 'text/plain']
    # The observed command is the whole ledgered Bash call; the Activity's command is part of it.
    assert binding['activities']['urn:draft:activity-xml']['observed_command'] == RAN['xml']
    assert binding['activities']['urn:draft:activity-xml']['ledger_sequence'] == 2
    assert files['urn:draft:file-txt']['derived_from'] == 'urn:draft:file-pdf' and files['urn:draft:file-txt']['url'] == PDF_URL
    quotes = {quote['evidence_item']: quote for quote in binding['quotes']}
    for item, (path, data), selector in (('urn:draft:evidence-1', FILES['xml'], 'line=8'), ('urn:draft:evidence-2', FILES['txt'], 'line=3-4')):
        quote = quotes[item]
        # The span addresses the file's own bytes, multibyte characters before it included; whitespace is the file's.
        assert data[quote['byte_start']:quote['byte_start'] + quote['byte_length']].decode() == quote['value_text']
        assert quote['selector'] == selector and quote['path'] == path
    assert quotes['urn:draft:evidence-2']['value_text'] == TEXT_QUOTE.replace('than in ', 'than in\n').replace('littermate ', 'littermate   ')
    assert quotes['urn:draft:evidence-1']['media_type'] == 'application/xml'
    # The lint copy carries what REVEAL measured; the agent never wrote a checksum.
    bound = source_evidence.bind(draft('urn:p', 'urn:g'), binding)
    assert {node['id']: node['sha256'] for node in bound['files']} == {fid: entry['sha256'] for fid, entry in files.items()}
    assert all('observed_command' not in node for node in bound['activities'])
    assert source_evidence.verified_files(binding) == sorted(({'sha256': entry['sha256'], 'size_bytes': entry['size_in_bytes']}
                                                             for entry in files.values()), key=lambda entry: entry['sha256'])


def test_a_file_written_by_hand_with_an_invented_activity_is_refused(run):
    output, ledger, check = run
    (output / 'sources/notes.txt').write_text('Hepatic insulin signalling was entirely abolished in every knockout animal.\n')
    entry = ledger.start('Write', {'file_path': '/reveal/output/sources/notes.txt', 'content': '...'}, None)
    ledger.finish(entry, {'type': 'tool_result', 'content': 'ok', 'is_error': False}, 'completed')
    document = draft('urn:p', 'urn:g')
    document['activities'].append({'id': 'urn:draft:activity-notes', 'name': 'Fetch', 'command': 'curl -sSL -o output/sources/notes.txt https://example.org/notes.txt',
                                   'software_name': 'curl', 'software_version': '8.5.0', 'generated_at_time': '2026-11-19T10:05:00Z'})
    document['files'].append({'id': 'urn:draft:file-notes', 'filename': 'sources/notes.txt', 'location': 'https://example.org/notes.txt'})
    document['was_generated_by_edges'].append({'subject': 'urn:draft:file-notes', 'predicate': 'prov:wasGeneratedBy', 'object': 'urn:draft:activity-notes'})
    document['evidence_items'][0].update(snippet='Hepatic insulin signalling was entirely abolished in every knockout animal.', was_derived_from=['urn:draft:file-notes'])
    problems, binding = check(document)
    assert list(messages(problems)) == ['urn:draft:file-notes']
    assert 'No Bash command run in this run is the command of its Activity' in problems[0][1]
    assert 'urn:draft:file-notes' not in binding['files'] and 'urn:draft:evidence-1' not in {q['evidence_item'] for q in binding['quotes']}


@pytest.mark.parametrize('change, expected', [
    ({'text_context': 'Mouse liver; line=1-2'}, 'The quote is not on lines 1-2 of sources/jmet-2012.txt; it occurs on lines 3-4'),
    ({'text_context': 'Mouse liver; line=5'}, 'The quote is not on line 5 of sources/jmet-2012.txt; it occurs on lines 3-4'),
    ({'text_snippet': 'Hepatic glucose output was entirely abolished in every animal.'}, 'not verbatim text of sources/jmet-2012.txt'),
    ({'text_snippet': REPEATED, 'text_context': 'Mouse liver'}, 'The quote occurs 2 times in sources/jmet-2012.txt, on line 6'),
    ({'text_snippet': REPEATED, 'text_context': 'Mouse liver; line=6'}, 'The quote occurs 2 times on line 6 of sources/jmet-2012.txt'),
    ({'text_snippet': 'controls after a hyperinsulinaemic clamp. Discussion', 'text_context': 'Mouse liver'}, 'runs across two paragraphs'),
    ({'text_snippet': 'glucose production'}, 'too short'),
    ({'text_snippet': 'word ' * 90}, 'too long'),
])
def test_a_quote_must_occur_once_where_the_evidence_says(run, change, expected):
    _, _, check = run
    problems, binding = check(draft('urn:p', 'urn:g', **change))
    assert [where for where, _ in problems] == ['urn:draft:evidence-2']
    assert expected in problems[0][1], problems
    assert {quote['evidence_item'] for quote in binding['quotes']} == {'urn:draft:evidence-1'}


def test_the_record_of_each_file_is_checked_against_the_ledger(run):
    output, ledger, check = run

    def refused(change, where='urn:draft:file-xml'):
        document = draft('urn:p', 'urn:g'); change(document)
        found = messages(check(document)[0])
        assert where in found, found
        return found[where]
    files = lambda document: {node['id']: node for node in document['files']}
    acts = lambda document: {node['id']: node for node in document['activities']}
    # outside output/sources/, missing, or a link
    assert 'under sources/' in refused(lambda d: files(d)['urn:draft:file-xml'].update(filename='PMC3491803.xml'))
    assert 'under sources/' in refused(lambda d: files(d)['urn:draft:file-xml'].update(filename='sources/../account-1.json'))
    assert 'There is no file output/sources/missing.xml' in refused(lambda d: files(d)['urn:draft:file-xml'].update(filename='sources/missing.xml'))
    os.symlink('/etc/hosts', output / 'sources/hosts.txt')
    assert 'not a regular file' in refused(lambda d: files(d)['urn:draft:file-xml'].update(filename='sources/hosts.txt'))
    # no Activity, or one without its command or time, or one named like REVEAL's own
    assert 'Record how output/sources/PMC3491803.xml was made' in refused(lambda d: d['was_generated_by_edges'].pop(0))
    assert 'command' in refused(lambda d: acts(d)['urn:draft:activity-xml'].pop('command'))
    assert 'generated_at_time' in refused(lambda d: acts(d)['urn:draft:activity-xml'].pop('generated_at_time'))
    def reveal_id(document):
        acts(document)['urn:draft:activity-xml']['id'] = 'urn:reveal:runtime:execution:job:1'
        document['was_generated_by_edges'][0]['object'] = 'urn:reveal:runtime:execution:job:1'
    assert 'id of your own' in refused(reveal_id)
    # a command no Bash call ran, or ran only in a failed call, or one that does not name the file
    assert 'No Bash command run in this run is the command' in refused(lambda d: acts(d)['urn:draft:activity-xml'].update(command=FETCH_XML + ' --compressed'))
    failed = FETCH_PDF.replace('jmet.2012.5', 'jmet.2012.6')
    def failed_call(document):
        acts(document)['urn:draft:activity-pdf']['command'] = failed
        files(document)['urn:draft:file-pdf']['location'] = PDF_URL.replace('jmet.2012.5', 'jmet.2012.6')
    assert 'failed' in refused(failed_call, 'urn:draft:file-pdf')
    assert 'does not name output/sources/PMC3491803.xml' in refused(lambda d: acts(d)['urn:draft:activity-xml'].update(command='mkdir -p output/sources'))
    # a fetched file names the URL its command fetched
    assert 'location' in refused(lambda d: files(d)['urn:draft:file-xml'].pop('location'))
    assert 'does not fetch' in refused(lambda d: files(d)['urn:draft:file-xml'].update(location='https://example.org/other.xml'))
    # a converted file comes from a saved raw file through its own Activity, and its command names the raw file
    def same_activity(document):
        document['was_generated_by_edges'][2]['object'] = 'urn:draft:activity-pdf'
    assert 'its own Activity' in refused(same_activity, 'urn:draft:file-txt')
    def unknown_raw(document):
        document['was_derived_from_edges'][0]['object'] = 'urn:draft:file-elsewhere'
    assert 'derives from urn:draft:file-elsewhere' in refused(unknown_raw, 'urn:draft:file-txt')
    def cycle(document):
        document['was_derived_from_edges'].append({'subject': 'urn:draft:file-pdf', 'predicate': 'prov:wasDerivedFrom', 'object': 'urn:draft:file-txt'})
    assert 'lead back to it' in refused(cycle, 'urn:draft:file-pdf')
    def unnamed_raw(document):
        document['was_derived_from_edges'][0]['object'] = 'urn:draft:file-xml'
    assert 'does not name output/sources/PMC3491803.xml' in refused(unnamed_raw, 'urn:draft:file-txt')
    # a File no quote cites is held to the same record
    def stray(document):
        document['files'].append({'id': 'urn:draft:file-stray', 'filename': 'sources/unsaved.xml', 'location': XML_URL})
    assert 'There is no file output/sources/unsaved.xml' in refused(stray, 'urn:draft:file-stray')


def test_evidence_must_cite_one_saved_text_file(run):
    output, _, check = run

    def refused(change):
        document = draft('urn:p', 'urn:g'); change(document['evidence_items'][0])
        found = messages(check(document)[0])
        assert list(found) == ['urn:draft:evidence-1'], found
        return found['urn:draft:evidence-1']
    assert 'Cite the saved file' in refused(lambda item: item.pop('was_derived_from'))
    assert 'cites 2 files' in refused(lambda item: item.update(was_derived_from=['urn:draft:file-xml', 'urn:draft:file-txt']))
    assert 'not UTF-8 text' in refused(lambda item: item.update(was_derived_from=['urn:draft:file-pdf']))
    assert 'Quote the passage' in refused(lambda item: item.pop('snippet'))
    # Evidence that rests on other Claims quotes nothing and is left alone.
    document = draft('urn:p', 'urn:g')
    document['evidence_items'].append({'id': 'urn:draft:evidence-3', 'source_claims': ['urn:draft:claim-0'], 'target_proposition': 'urn:p'})
    assert check(document)[0] == []


def test_all_quotes_of_one_paper_together_stay_short(run):
    output, ledger, check = run
    sentences = [f'Sentence number {index:03d} reports a separate measurement taken in knockout mice at week {index}.' for index in range(200)]
    (output / 'sources/long.txt').write_text('\n\n'.join(sentences) + '\n')
    bash(ledger, 'pdftotext -layout output/sources/jmet-2012.pdf output/sources/long.txt')
    document = draft('urn:p', 'urn:g')
    document['activities'].append(activity('txt', 'pdftotext -layout output/sources/jmet-2012.pdf output/sources/long.txt', ('pdftotext', '24.02.0')) | {'id': 'urn:draft:activity-long'})
    document['files'].append({'id': 'urn:draft:file-long', 'filename': 'sources/long.txt'})
    document['was_generated_by_edges'].append({'subject': 'urn:draft:file-long', 'predicate': 'prov:wasGeneratedBy', 'object': 'urn:draft:activity-long'})
    document['was_derived_from_edges'].append({'subject': 'urn:draft:file-long', 'predicate': 'prov:wasDerivedFrom', 'object': 'urn:draft:file-pdf'})
    for index, sentence in enumerate(sentences[:50]):
        document['evidence_items'].append({'id': f'urn:draft:evidence-long-{index:02d}', 'target_proposition': 'urn:p', 'snippet': sentence,
                                           'was_derived_from': ['urn:draft:file-long']})
    problems, _ = check(document)
    # The text and the long text both come from one PDF: one paper, one allowance.
    assert len(problems) == 1 and problems[0][0] == 'urn:draft:evidence-long-49', problems
    assert 'one paper (sources/jmet-2012.pdf and the files made from it)' in problems[0][1] and 'the most is 4,000' in problems[0][1]


def test_the_command_may_name_the_file_by_its_absolute_path_and_must_name_it_whole(run):
    _, ledger, check = run
    assert source_evidence.names('curl -o /reveal/output/sources/a.xml https://x', 'sources/a.xml')
    assert source_evidence.names('curl -o "/reveal/trusted/output/sources/a.xml" https://x', 'sources/a.xml')
    assert source_evidence.names('curl https://x > ./output/sources/a.xml', 'sources/a.xml')
    assert not source_evidence.names('curl -o output/sources/a.xml.bak https://x', 'sources/a.xml')
    assert not source_evidence.names('curl -o myoutput/sources/a.xml https://x', 'sources/a.xml')


def test_the_recorded_command_is_one_whole_command_of_the_call_that_ran():
    ran = 'mkdir -p output/sources && curl -fsSL -o output/sources/a.xml https://x/a; ls output/sources | wc -l\nhead -1 output/sources/a.xml || true'
    assert source_evidence.segments(ran) == ['mkdir -p output/sources', 'curl -fsSL -o output/sources/a.xml https://x/a', 'ls output/sources',
                                              'wc -l', 'head -1 output/sources/a.xml', 'true']
    for part in ('curl -fsSL -o output/sources/a.xml https://x/a', 'curl  -fsSL -o output/sources/a.xml\thttps://x/a', 'mkdir -p output/sources'):
        assert source_evidence.contains(ran, part), part
    for part in (ran, 'url -fsSL -o output/sources/a.xml https://x/a', 'curl -fsSL -o output/sources/a.xml https://x/',
                 'mkdir -p output/sources && curl -fsSL -o output/sources/a.xml https://x/a'):
        assert not source_evidence.contains(ran, part), part
    # Separators inside quotes split nothing, and a quoted command is not a command that ran.
    assert source_evidence.segments("echo 'a && b; c | d' > out") == ["echo 'a && b; c | d' > out"]
    assert source_evidence.segments('printf "x\\"; y" && z') == ['printf "x\\"; y"', 'z']
    assert not source_evidence.contains("echo 'curl -fsSL -o output/sources/a.xml https://x/a'", 'curl -fsSL -o output/sources/a.xml https://x/a')


def test_a_command_that_was_only_echoed_does_not_record_a_download(run):
    output, ledger, check = run
    (output / 'sources/echoed.xml').write_text('<p>Hepatic insulin signalling was entirely abolished in every knockout animal.</p>\n')
    fetch = 'curl -fsSL -o output/sources/echoed.xml https://example.org/echoed.xml'
    bash(ledger, f"echo '{fetch}' && printf '<p>made up</p>' > /tmp/x")
    document = draft('urn:p', 'urn:g')
    document['activities'].append({'id': 'urn:draft:activity-echoed', 'name': 'Fetch', 'command': fetch, 'software_name': 'curl',
                                   'software_version': '8.5.0', 'generated_at_time': '2026-11-19T10:05:00Z'})
    document['files'].append({'id': 'urn:draft:file-echoed', 'filename': 'sources/echoed.xml', 'location': 'https://example.org/echoed.xml'})
    document['was_generated_by_edges'].append({'subject': 'urn:draft:file-echoed', 'predicate': 'prov:wasGeneratedBy', 'object': 'urn:draft:activity-echoed'})
    found = messages(check(document)[0])
    assert list(found) == ['urn:draft:file-echoed'] and 'No Bash command run in this run is the command' in found['urn:draft:file-echoed']


@pytest.mark.parametrize('later', ['sed -i s/42%/24%/ output/sources/PMC3491803.xml', 'echo extra >> output/sources/PMC3491803.xml',
                                   'cat /tmp/edited > /reveal/output/sources/PMC3491803.xml', 'mv /tmp/x output/sources/PMC3491803.xml',
                                   'curl -fsSL -o output/sources/PMC3491803.xml https://example.org/other.xml'])
def test_a_file_written_again_after_its_recorded_command_is_refused(run, later):
    _, ledger, check = run
    entry = bash(ledger, later)
    found = messages(check(draft('urn:p', 'urn:g'))[0])
    assert list(found) == ['urn:draft:file-xml'], found
    assert f"Bash call {entry['sequence']} ({later[:40]}" in found['urn:draft:file-xml'] and 'touched output/sources/PMC3491803.xml' in found['urn:draft:file-xml']


def test_a_later_write_in_the_same_call_is_refused_too(tmp_path):
    output = tmp_path / 'output'; (output / 'sources').mkdir(parents=True)
    ledger = Ledger(tmp_path / 'ledger', 'account-job', 1)
    for key in ('xml', 'pdf', 'txt'):
        (output / FILES[key][0]).write_bytes(FILES[key][1])
    bash(ledger, RAN['xml'] + ' && sed -i s/Lepr/LEPR/ output/sources/PMC3491803.xml')
    bash(ledger, RAN['pdf']); bash(ledger, RAN['txt'])
    found = messages(source_evidence.check(draft('urn:p', 'urn:g'), output, ledger.entries, ledger.root)[0])
    assert list(found) == ['urn:draft:file-xml'] and 'sed -i' in found['urn:draft:file-xml']


@pytest.mark.parametrize('later', ['grep -n "AKT phosphorylation" output/sources/PMC3491803.xml', 'head -40 output/sources/PMC3491803.xml',
                                   'sed -n 5,9p output/sources/PMC3491803.xml', 'cat output/sources/PMC3491803.xml > /tmp/copy',
                                   'xmllint --xpath "//p" output/sources/PMC3491803.xml', 'wc -c output/sources/jmet-2012.txt',
                                   'pdftotext -layout output/sources/jmet-2012.pdf -', 'sha256sum output/sources/jmet-2012.pdf',
                                   'tail -n 3 output/sources/jmet-2012.txt | grep -n clamp'])
def test_reading_a_saved_file_afterwards_leaves_its_record_standing(run, later):
    _, ledger, check = run
    bash(ledger, later)
    assert check(draft('urn:p', 'urn:g'))[0] == []
    # The recorded conversion reads the PDF after the PDF was fetched: it is the record, not a change.
    assert source_evidence.reads_only('pdftotext -layout output/sources/x.pdf output/sources/x.txt', 'sources/x.pdf') is False


def test_publications_come_from_the_identifiers_in_the_source_url():
    assert source_evidence.publication(XML_URL) == {'citation': 'PMCID:PMC3491803'}
    assert source_evidence.publication(PDF_URL) == {'doi': 'doi:10.1234/jmet.2012.5'}
    assert source_evidence.publication('https://pubmed.ncbi.nlm.nih.gov/23154982/') == {'pmid': '23154982'}
    assert source_evidence.publication('https://doi.org/10.1101/2020.01.01.123456') == {'doi': 'doi:10.1101/2020.01.01.123456'}
    assert source_evidence.publication('https://example.org/content/10.1101/2020.01.01.123456v1.full.pdf') == {}


def test_a_malformed_record_is_reported_never_a_crash(run):
    """The gate reads the agent's own account-1.json before any structural check: values of the wrong type are
    skipped here (the pinned linter reports them), and a File named twice is read once."""
    _, _, check = run
    document = draft('urn:p', 'urn:g')
    document['was_generated_by_edges'].append({'subject': ['urn:draft:file-xml'], 'object': {'id': 1}})
    document['was_derived_from_edges'].append({'subject': {'x': 1}, 'object': None})
    document['evidence_items'][0]['was_derived_from'].append({'id': 'urn:draft:file-xml'})
    document['evidence_items'].append('not an object')
    document['files'].append({'id': 'urn:draft:file-xml-again', 'filename': 'sources/PMC3491803.xml', 'location': XML_URL})
    document['was_generated_by_edges'].append({'subject': 'urn:draft:file-xml-again', 'object': 'urn:draft:activity-xml'})
    problems, binding = check(document)
    assert problems == [] and binding['files']['urn:draft:file-xml-again']['sha256'] == binding['files']['urn:draft:file-xml']['sha256']
    assert source_evidence.source_activities({'files': [{'id': 'f'}], 'was_generated_by_edges': [{'subject': ['f'], 'object': 'a'}]}) == set()


def test_the_draft_keeps_only_activities_that_made_a_file():
    document = draft('urn:p', 'urn:g')
    assert source_evidence.source_activities(document) == {'urn:draft:activity-xml', 'urn:draft:activity-pdf', 'urn:draft:activity-txt'}
    edited = deepcopy(document); edited['was_generated_by_edges'] = []
    assert source_evidence.source_activities(edited) == set()
