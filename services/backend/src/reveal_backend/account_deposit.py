"""The Scientific Account a general agent job returns and deposits.

An agent authors its account with the pinned DAPPER release (0.2.0). It quotes files it saved itself under
output/sources/, and records in the account how it got each one: an Activity per command, a File per saved file,
and provenance edges between them (source_evidence). That release has no ValueLocator, so the agent cites a quote by
its text, the File, and optionally the lines it sits on.

`deposit_document` rewrites the accepted account into the form that leaves REVEAL:

  - each quoting EvidenceItem points, through a DAPPER ValueLocator, at the exact byte span of its quote in the saved
    file (source_evidence finds it), and references a Publication when the file's URL names the paper;
  - each saved file is a File with REVEAL's sha256, size and media type and the URL it came from, and its Activity
    carries the Bash command REVEAL saw run (`observed_command`). The bytes are never deposited: REVEAL keeps them;
  - a recorded or observed command that is not one plain fetch or conversion is deposited quoted
    (deposit_screen.quoted_command): text that prints the command and runs nothing if pasted into a shell;
  - everything is minted once, with DAPPER 0.2.1-a1 (the release that has ValueLocator), vendored at
    services/backend/vendor/dapper-0.2.1-a1. DAPPER ids are the same in 0.2.0 and 0.2.1-a1;
  - that document is then converted to DAPPER 0.3.0-a1, the release the claims repository accepts, by 0.3.0-a1's own
    converter (converter/migrate.py, vendored at services/backend/vendor/dapper-0.3.0-a1). DAPPER owns the
    conversion: it reshapes what 0.3 changed (CausalSteps into CausalLinks between CausalVariables, question and
    hypothesis into framing, DISMECH fields into a CorrespondsToDismech edge), re-mints the whole document and
    verifies it, or refuses, listing why. Nodes neither step touches keep the ids the pinned release gave them;
  - it is laid out one way (`layout`: nodes by id, edges in order), the way claims_read rebuilds it from the claims
    repository, so the account served back hashes to the account that was sent.

The ids of the returned document are the account's only public ids. Ids minted earlier, by the pinned release's
gate, name the internal form and are never stored or returned.
"""
import ast
from copy import deepcopy
from functools import lru_cache
import hashlib
import re
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from . import source_evidence

# The release the account is first minted with (it has ValueLocator, and shares 0.2.0's ids)...
VENDOR = Path(__file__).resolve().parents[2] / 'vendor/dapper-0.2.1-a1'
MINT_VERSION = '0.2.1-a1'
# ...and the release it is deposited in, converted by that release's converter. Deposits name it (dapper_version).
DEPOSIT_VENDOR = Path(__file__).resolve().parents[2] / 'vendor/dapper-0.3.0-a1'
DAPPER_VERSION = '0.3.0-a1'
# Where each disease's DISMECH page is listed: the DisMech export REVEAL ships (data/dismech-gaps).
DISMECH_GAPS = Path(__file__).resolve().parents[4] / 'data/dismech-gaps/2026-09-24/knowledge-gaps.jsonl.gz'
DISMECH_PAGES = 'https://dismech.monarchinitiative.org/pages/'
# Keys an accepted account document carries that are not DAPPER groups.
NON_GROUPS = frozenset({'prefixes'})


def verify_vendor():
    """Each vendored DAPPER is its recorded commit's files, byte for byte. Checked when the image is built and in
    tests, so a deployment cannot mint or convert with an edited or missing copy. {version: its VENDOR.json}."""
    found = {}
    for vendor, version in ((VENDOR, MINT_VERSION), (DEPOSIT_VENDOR, DAPPER_VERSION)):
        recorded = json.loads((vendor / 'VENDOR.json').read_text())
        for name, digest in recorded['files'].items():
            if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError(f'Vendored DAPPER {version} file {name} differs from its recorded checksum')
        if recorded['version'] != version:
            raise RuntimeError(f'Vendored DAPPER {recorded["version"]} is not the {version} this module uses')
        found[version] = recorded
    return found


class DepositError(ValueError):
    """An accepted account that cannot be put into its deposit form. The message names the object."""


# Runs in its own interpreter: REVEAL allows one DAPPER runtime per process, and the pinned release owns it.
_MINT = r'''
import json, re, sys
from pathlib import Path
schema = Path(sys.argv[1])
sys.path[:0] = [str(schema / 'identity'), str(schema)]
import dapper_identity as identity
view = identity.load_schema(schema / 'dapper.yaml')
document = json.loads(Path(sys.argv[2]).read_bytes())
edges = {re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower() + '_edges': name for name, cls in view.all_classes().items()
         if name != 'Edge' and not cls.abstract and not cls.mixin and 'Edge' in view.class_ancestors(name)}
unknown = sorted(key for key in document if key not in identity.DOC_GROUPS and key not in edges)
if unknown:
    print(json.dumps({'error': 'not DAPPER ' + identity.PROFILE + ' groups: ' + ', '.join(unknown)})); raise SystemExit(0)
document = {key: rows for key, rows in document.items() if rows}
mapping = identity.assign_ids(document, view)
nodes = {}
for key, class_name in identity.DOC_GROUPS.items():
    for node in document.get(key, []):
        if identity.compute_id({k: v for k, v in node.items() if k != 'id'}, class_name, view) != node['id']:
            print(json.dumps({'error': node['id'] + ' does not verify after minting'})); raise SystemExit(0)
        nodes[node['id']] = class_name
pattern = re.compile(r'dapper:[A-Za-z]+\.[A-Za-z0-9_-]+')
unresolved = sorted({ref for ref in pattern.findall(json.dumps(document)) if ref not in nodes})
print(json.dumps({'document': document, 'mapping': mapping, 'unresolved': unresolved, 'classes': nodes, 'profile': identity.PROFILE}))
'''


def _mint_once(document):
    with tempfile.TemporaryDirectory(prefix='reveal-deposit-') as directory:
        path = Path(directory) / 'document.json'
        path.write_text(json.dumps(document, ensure_ascii=False))
        done = subprocess.run([sys.executable, '-I', '-B', '-c', _MINT, str(VENDOR / 'schema'), str(path)],
                              capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise DepositError('DAPPER ' + MINT_VERSION + ' could not mint the account: ' + done.stderr.strip()[-600:])
    result = json.loads(done.stdout)
    if 'error' in result:
        raise DepositError('The account is ' + result['error'])
    return result


def _collapse(document):
    """Merge nodes that minted to one id, and repeated ids in a node's lists. True when anything changed.

    Content addressing gives identical objects one id (two EvidenceItems quoting the same span, say). A document
    holds each object once, and a list that named both now names one; the objects that held such a list have
    different content than before, so the caller mints again."""
    changed = False
    for key, rows in document.items():
        if key.endswith('_edges'):
            unique = [edge for index, edge in enumerate(rows) if edge not in rows[:index]]
        else:
            seen, unique = set(), []
            for node in rows:
                if node['id'] not in seen:
                    seen.add(node['id']); unique.append(node)
            for node in unique:
                for slot, value in node.items():
                    if isinstance(value, list) and all(isinstance(item, str) for item in value) and len(set(value)) != len(value):
                        node[slot] = list(dict.fromkeys(value)); changed = True
        changed = changed or len(unique) != len(rows)
        document[key] = unique
    return changed


def mint(document, repository_claims=()):
    """(document minted with the vendored DAPPER 0.2.1-a1, {old id: new id}). Every id is verified after minting,
    each object appears once, and every dapper reference must resolve inside the document, except the
    `repository_claims`: Claims the claims repository holds, which the account names without containing."""
    mapping = None
    for _ in range(6):  # each pass can only merge; a document of a few levels settles in two or three
        result = _mint_once(document)
        mapping = result['mapping'] if mapping is None else {old: result['mapping'].get(new, new) for old, new in mapping.items()}
        document = result['document']
        if not _collapse(document):
            break
    else:
        raise DepositError('The account did not settle on one id per object')
    unresolved = sorted(set(result['unresolved']) - set(repository_claims))
    if unresolved:
        raise DepositError('The account references objects it does not contain: ' + ', '.join(unresolved[:5]))
    return document, mapping


# Runs in its own interpreter, like _MINT: the converter imports the 0.3.0-a1 identity code and checks. The checks
# see one document, so a Claim of the claims repository that an EvidenceItem names in source_claims (resolved there by
# the gate, box_claims.verify_references) is not a local record; that one finding is set aside for exactly those ids,
# as mint() allows them. Every other finding refuses.
_CONVERT = r'''
import json, sys
from pathlib import Path
vendor = Path(sys.argv[1])
sys.path[:0] = [str(vendor / 'converter')]
import migrate
import scientific_claims
request = json.loads(Path(sys.argv[2]).read_bytes())
external = {f"{claim} must resolve to Claim; found no local record" for claim in request['repository_claims']}
check = scientific_claims.check_scientific_content
scientific_claims.check_scientific_content = lambda index: [
    (where, problem) for where, problem in check(index)
    if not (where.endswith('.source_claims') and problem in external)]
try:
    document, ids, notes = migrate.migrate(request['document'], request['from'], dismech_pages=request['dismech_pages'])
except migrate.Refusal as refusal:
    print(json.dumps({'refused': refusal.problems})); raise SystemExit(0)
print(json.dumps({'document': document, 'ids': ids, 'notes': notes, 'version': migrate.current_version()}))
'''


@lru_cache(maxsize=1)
def dismech_pages():
    """{MONDO CURIE: DISMECH page URL} for every disorder in the shipped DisMech export whose term names one page.
    The converter needs the page for an object that names a DISMECH disease but no dismech_source_url."""
    import gzip
    pages = {}
    if not DISMECH_GAPS.exists():
        return pages
    with gzip.open(DISMECH_GAPS, 'rt', encoding='utf-8') as lines:
        for line in lines:
            record = json.loads(line)
            source = record.get('source_file') or ''
            if record.get('source_kind') != 'disorders' or not source.startswith('kb/disorders/') or not source.endswith('.yaml'):
                continue
            term = record.get('disease_term')
            curie = (term.get('term') or {}).get('id') if isinstance(term, dict) else None
            if isinstance(curie, str):
                pages.setdefault(curie, set()).add(DISMECH_PAGES + source[len('kb/'):-len('.yaml')] + '.html')
    return {curie: next(iter(urls)) for curie, urls in pages.items() if len(urls) == 1}


def convert(document, repository_claims=()):
    """(document in DAPPER 0.3.0-a1, {0.2.1-a1 id: 0.3.0-a1 id}, notes) from a document minted with 0.2.1-a1, by the
    vendored 0.3.0-a1 release's converter. `repository_claims` are the claims repository's Claims the account may
    name without containing (as for mint). Raises DepositError listing every problem when it refuses."""
    with tempfile.TemporaryDirectory(prefix='reveal-convert-') as directory:
        path = Path(directory) / 'request.json'
        path.write_text(json.dumps({'document': document, 'from': MINT_VERSION, 'dismech_pages': dismech_pages(),
                                    'repository_claims': sorted(repository_claims)}, ensure_ascii=False))
        done = subprocess.run([sys.executable, '-I', '-B', '-c', _CONVERT, str(DEPOSIT_VENDOR), str(path)],
                              capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise DepositError(f'DAPPER {DAPPER_VERSION} could not convert the account: ' + done.stderr.strip()[-600:])
    result = json.loads(done.stdout)
    if 'refused' in result:
        problems = result['refused']
        raise DepositError(f'DAPPER {DAPPER_VERSION} refuses to convert the account ({len(problems)} problem'
                           f'{"s" if len(problems) != 1 else ""}): ' + '; '.join(problems)[:1200])
    if result['version'] != DAPPER_VERSION:
        raise DepositError(f"The vendored converter converts to {result['version']}, not {DAPPER_VERSION}")
    return result['document'], result['ids'], result['notes']


# Who an account is attributed to when its requester is not named. DAPPER requires an agent on every Claim; this one
# is the service, the same Organization (one id) in every such deposit, and says nothing about who asked.
SERVICE_AGENT = {'name': 'REVEAL Mechanisms agent service'}


def _reattribute(document, identities, agent):
    """Take the named agents out of the document and put `agent` (an Organization) wherever they were referenced."""
    def swap(value):
        return agent['id'] if isinstance(value, str) and value in identities else value
    for key, rows in document.items():
        if key.endswith('_edges'):
            document[key] = [{name: swap(value) for name, value in edge.items()} for edge in rows]
            continue
        kept = [node for node in rows if node.get('id') not in identities]
        for node in kept:
            for slot, value in node.items():
                if slot == 'id':
                    continue
                node[slot] = list(dict.fromkeys(swap(item) for item in value)) if isinstance(value, list) else swap(value)
        document[key] = kept
    document['organizations'] = [*document.get('organizations', []), agent]


def deposit_document(account_document, sources_root, calls, ledger_root, *, attribute_requester=False, repository_claims=()):
    """(the account in its deposit form, a summary) from an accepted account document, the folder its sources were
    saved in (the run's output/) and the run's tool ledger (`calls`, request paths relative to `ledger_root`).

    The accepted document is attributed to its requester (assemble_account always does that). The deposit form names
    them only when `attribute_requester` is true; otherwise the requester's Person is left out and the account and
    its Claims are attributed to the service (SERVICE_AGENT): what made it, not who asked.

    `repository_claims` are the Claims of the claims repository the account builds on, as the gate resolved them
    (box_claims.verify_references): the account names each by id in an EvidenceItem's source_claims and does not
    contain it. Any other reference to an object outside the document is refused.

    `account_document` is what acceptance.assemble_account returned: valid against the pinned release, attributed,
    its Files measured. Its record of sources is checked again here, by the rule the gate applied
    (source_evidence.check); raises DepositError when it does not hold or the result is not self-contained.

    The summary lists every saved source file (`papers`) with what the job result says about it, so the caller can
    keep its bytes."""
    document = {key: deepcopy(rows) for key, rows in account_document.items() if key not in NON_GROUPS}
    try:
        problems, binding = source_evidence.check(document, sources_root, calls, ledger_root)
    except source_evidence.SourceError as exc:
        problems = [('ledger', str(exc))]
    if problems:
        where, message = problems[0]
        raise DepositError(f'{where}: {message}')
    accounts = document.get('scientific_accounts', [])
    if len(accounts) != 1:
        raise DepositError('An account document holds exactly one ScientificAccount')
    activity = accounts[0].get('was_generated_by')
    if not attribute_requester:
        _reattribute(document, set(accounts[0].get('was_attributed_to') or []),
                     {'id': 'urn:reveal:deposit:organizations:service', **SERVICE_AGENT})

    for node in document.get('files', []):
        entry = binding['files'][node['id']]
        if (node.get('sha256'), node.get('size_in_bytes'), node.get('mime_type')) != (entry['sha256'], entry['size_in_bytes'], entry['mime_type']):
            raise DepositError(f"{node['id']}: the File does not carry what REVEAL measured of output/{entry['path']}")
    # The command REVEAL saw run, beside the command the agent recorded.
    for node in document.get('activities', []):
        if node['id'] in binding['activities']:
            node['observed_command'] = binding['activities'][node['id']]['observed_command']
    # A recorded command that is more than one plain fetch or conversion (a Bash call that also ran a script, say) is
    # kept, quoted so that it prints itself and runs nothing if pasted: the account is deposited either way.
    from .deposit_screen import quote_unsafe_commands
    quoted_commands = quote_unsafe_commands(document)

    new = {'value_locators': {}}
    publications = {}   # one per paper, from the identifiers the URL it was fetched from carries

    def node_id(group, key, content):
        """One temporary id per distinct new node; mint() replaces it with the node's DAPPER id."""
        return new[group].setdefault(key, {'id': f'urn:reveal:deposit:{group}:{len(new[group]) + 1}', **content})['id']

    def publication_for(url):
        described = source_evidence.publication(url)
        if not described:
            return None
        names = set(described.values())
        known = next((entry for entry in publications.values() if entry['names'] & names), None)
        if known is None:
            known = publications[url] = {'names': set(), 'node': {}, 'id': f'urn:reveal:deposit:publications:{len(publications) + 1}'}
        known['names'] |= names
        for key, value in described.items():
            known['node'].setdefault(key, value)
        return known['id']

    quoted = {}
    for quote in binding['quotes']:
        item = document['evidence_items'][quote['index']]
        located = node_id('value_locators', (quote['sha256'], quote['byte_start'], quote['byte_length']), {
            'file': quote['file'], 'byte_start': quote['byte_start'], 'byte_length': quote['byte_length'],
            'value_text': quote['value_text'], 'selector': quote['selector'], 'media_type': quote['media_type']})
        item['snippet'] = quote['value_text']
        item['value_locators'] = [located]
        published = publication_for(binding['files'][quote['file']]['url'])
        if published:
            item['reference'] = published
        quoted[quote['file']] = quoted.get(quote['file'], 0) + 1

    document['publications'] = [*document.get('publications', []), *({'id': entry['id'], **entry['node']} for entry in publications.values())]
    document['value_locators'] = [*document.get('value_locators', []), *new['value_locators'].values()]
    if activity:
        # The run that wrote the account used the files it quotes.
        used = document.setdefault('used_edges', [])
        for identity in sorted(quoted):
            edge = {'subject': activity, 'predicate': 'prov:used', 'object': identity}
            if edge not in used:
                used.append(edge)

    # Only where the gate allowed them: an id resolved for source_claims does not excuse it anywhere else.
    from .box_claims import repository_references
    cited, misplaced = repository_references(document)
    allowed = set(repository_claims) & set(cited) - set(misplaced)
    minted, mapping = mint(document, allowed)
    minted, converted, notes = convert(minted, allowed)
    mapping = {old: converted.get(new, new) for old, new in mapping.items()}
    minted = layout(minted)
    account = minted['scientific_accounts'][0]
    files = binding['files']
    summary = {'account_id': account['id'], 'dapper_version': DAPPER_VERSION, 'conversion_notes': notes,
               'objects': sum(len(rows) for key, rows in minted.items() if not key.endswith('_edges')),
               'edges': sum(len(rows) for key, rows in minted.items() if key.endswith('_edges')),
               'claims': len(minted.get('claims', [])), 'value_locators': len(minted.get('value_locators', [])),
               'repository_claims': sorted(set(repository_claims) & set(cited) - set(misplaced)),
               'quoted_commands': quoted_commands,
               'papers': [{'path': entry['path'], 'url': entry['url'], 'sha256': entry['sha256'], 'size_bytes': entry['size_in_bytes'],
                           'media_type': entry['mime_type'], 'command': entry['command'], 'observed_command': entry['observed_command'],
                           'derived_from': files[entry['derived_from']]['path'] if entry['derived_from'] else None,
                           'evidence_items': quoted.get(identity, 0), 'file_id': mapping.get(identity, identity)}
                          for identity, entry in sorted(files.items(), key=lambda pair: pair[1]['path'])]}
    return minted, summary


def layout(document):
    """The deposit form's one layout: every node group ordered by id, every edge group in its own order, no empty
    group and no null slot. The claims repository keeps a deposit's edges in order but lists its objects by id, and
    stores a node or edge without its null slots (which identity ignores), so a document in this layout is the one
    claims_read rebuilds from the repository, byte for byte."""
    laid = {}
    for key, rows in document.items():
        rows = [{slot: value for slot, value in row.items() if value is not None} for row in rows]
        if rows:
            laid[key] = rows if key.endswith('_edges') else sorted(rows, key=lambda node: node['id'])
    return laid


def _doc_groups(path, version):
    source = path.read_text()
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and any(getattr(target, 'id', None) == 'DOC_GROUPS' for target in statement.targets):
            return {class_name: group for group, class_name in ast.literal_eval(statement.value).items()}
    raise RuntimeError(f'Vendored DAPPER {version} names no document groups')


@lru_cache(maxsize=1)
def node_groups():
    """{class name: document group} of the vendored DAPPER releases (their dapper_identity.DOC_GROUPS), read from the
    source without importing it: the pinned release owns this process's DAPPER runtime. Accounts are deposited in
    0.3.0-a1; earlier deposits are 0.2.1-a1, so its classes (CausalStep) are read back too. No class sits in
    different groups in the two."""
    earlier = _doc_groups(VENDOR / 'schema/identity/dapper_identity.py', MINT_VERSION)
    return {**earlier, **_doc_groups(DEPOSIT_VENDOR / 'identity/dapper_identity.py', DAPPER_VERSION)}


def edge_group(class_name):
    """The document group of an edge class, as DAPPER names it: Used -> used_edges, WasGeneratedBy ->
    was_generated_by_edges."""
    return re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower() + '_edges'


def source_bytes(sources_root, papers):
    """{sha256: bytes} of the saved source files a summary lists, read from the run's output/ and checked against what
    was measured, for the caller to keep. REVEAL never deposits them."""
    kept = {}
    for paper in papers:
        try:
            data = source_evidence.read_source(sources_root, paper['path'])
        except source_evidence.SourceError as exc:
            raise DepositError(str(exc)) from None
        if hashlib.sha256(data).hexdigest() != paper['sha256']:
            raise DepositError(f"output/{paper['path']} changed after it was checked")
        kept[paper['sha256']] = data
    return kept
