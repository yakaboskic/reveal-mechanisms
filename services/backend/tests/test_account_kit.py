"""The instructions and schema an account job's agent reads, pinned.

Account jobs have no versioned authoring kit: the Box copies the skill and the schema excerpt from the bundle as they
are. These hashes are the kit's version. Changing either file is a deliberate change of what every later account job
is told: update the hash here in the same commit, and say so in its message."""
import hashlib
import json
import os
import re
from pathlib import Path

import pytest
import yaml

from reveal_backend import source_evidence
from reveal_backend.authoring_contract import ACCOUNT_EXCERPT, ACCOUNT_WANTED, EXCERPT, pinned_schema, schema_excerpt
from reveal_backend.box_mcp import Ledger
from reveal_backend.box_remote import ACCOUNT_SKILL
from reveal_backend.runtime_config import ROOT

PINS = {ACCOUNT_SKILL: 'df7d53f03a5159a4f34da8ad3af0904286f9cd84606170010bc917d895632118',
        ACCOUNT_EXCERPT: '9ae37c6a974b12cd97bf5ee2bcad0552b5dca0e1e5d87436630336d5be34cd38'}
LOCK = ROOT / 'services/backend/agent-runtime/dapper-release.json'


@pytest.mark.parametrize('relative', sorted(PINS))
def test_the_account_kit_is_the_pinned_bytes(relative):
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == PINS[relative], relative


def test_the_account_excerpt_adds_the_record_of_sources_to_the_research_excerpt():
    account = yaml.safe_load(pinned_schema(ROOT, ACCOUNT_EXCERPT))
    research = yaml.safe_load(pinned_schema(ROOT, EXCERPT))
    assert ACCOUNT_WANTED <= set(account['classes'])
    assert set(research['classes']) < set(account['classes'])
    assert {'Activity', 'WasGeneratedBy', 'WasDerivedFrom', 'Used'}.isdisjoint(research['classes'])
    assert {'command', 'software_name', 'software_version', 'generated_at_time'} <= set(account['classes']['Activity']['attributes'])
    assert {'filename', 'location'} <= set(account['classes']['File']['attributes'])
    release = Path(os.environ.get('REVEAL_TEST_DAPPER_RELEASE', ROOT / '.runtime/dapper'))
    if not (release / 'schema/dapper.yaml').is_file():
        pytest.skip('Set REVEAL_TEST_DAPPER_RELEASE to a checkout of the locked release')
    assert pinned_schema(ROOT, ACCOUNT_EXCERPT) == schema_excerpt(release, LOCK, ACCOUNT_WANTED)


def test_the_worked_example_in_the_skill_passes_the_source_checks(tmp_path):
    """The skill's three steps, run as written: its commands saved the files, and its record and quote hold."""
    skill = (ROOT / ACCOUNT_SKILL).read_text()
    blocks = [json.loads(block) for block in re.findall(r'```json\n(.*?)\n```', skill, re.S)]
    commands = re.findall(r'```bash\n(.*?)\n```', skill, re.S)
    fetch_pdf = re.search(r'For a PDF you ran\n`([^`]+)`', skill).group(1)
    document = {}
    for block in blocks:
        for group, rows in block.items():
            document.setdefault(group, []).extend(rows)
    # Step 2 refers to the PDF's own record, recorded "as in step 1".
    pdf_url = fetch_pdf.split()[-1]
    document['activities'].append({'id': 'urn:draft:activity-2', 'name': 'Fetch the PDF of Smith 2012', 'command': fetch_pdf,
                                   'software_name': 'curl', 'software_version': '8.5.0', 'generated_at_time': '2026-11-19T10:02:00Z'})
    document['files'].append({'id': 'urn:draft:file-2', 'filename': 'sources/smith-2012.pdf', 'location': pdf_url})
    document['was_generated_by_edges'].append({'subject': 'urn:draft:file-2', 'predicate': 'prov:wasGeneratedBy', 'object': 'urn:draft:activity-2'})
    # The step 1 file is cited too, so every File of the example is part of the account.
    quoted = 'Hepatocyte-specific deletion of Lepr reduced insulin-stimulated AKT phosphorylation by 42% in liver.'
    document['evidence_items'].append({**document['evidence_items'][0], 'id': 'urn:draft:evidence-2', 'snippet': quoted,
                                       'was_derived_from': ['urn:draft:file-1'], 'context': 'Mouse liver; Results'})
    output = tmp_path / 'output'; (output / 'sources').mkdir(parents=True)
    (output / 'sources/PMC3491803.xml').write_text(f'<?xml version="1.0"?>\n<article><body><p>{quoted}</p></body></article>\n')
    (output / 'sources/smith-2012.pdf').write_bytes(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    lines = [f'Line {number} of the converted paper.' for number in range(1, 212)]
    lines += ['In knockout mice, fasting glucose production was 35% higher than in', 'littermate controls after a hyperinsulinaemic clamp.']
    (output / 'sources/smith-2012.txt').write_text('\n'.join(lines) + '\n')
    ledger = Ledger(tmp_path / 'ledger', 'kit', 1)
    for command in (commands[0], fetch_pdf, commands[1]):
        entry = ledger.start('Bash', {'command': command}, None)
        ledger.finish(entry, {'type': 'tool_result', 'content': '', 'is_error': False}, 'completed')
    problems, binding = source_evidence.check(document, output, ledger.entries, ledger.root)
    assert problems == []
    assert {quote['selector'] for quote in binding['quotes']} == {'line=212-213', 'line=2'}
    assert binding['activities']['urn:draft:activity-1']['observed_command'] == commands[0]
