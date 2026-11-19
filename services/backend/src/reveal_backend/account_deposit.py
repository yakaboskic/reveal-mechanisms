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
  - everything is minted once, with DAPPER 0.2.1-a1 (the release that has ValueLocator), vendored at
    services/backend/vendor/dapper-0.2.1-a1. DAPPER ids are the same in 0.2.0 and 0.2.1-a1, so nodes this rewrite
    does not touch keep the ids the pinned release gave them.

The ids of the returned document are the account's only public ids. Ids minted earlier, by the pinned release's
gate, name the internal form and are never stored or returned.
"""
from copy import deepcopy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from . import source_evidence

VENDOR = Path(__file__).resolve().parents[2] / 'vendor/dapper-0.2.1-a1'
DAPPER_VERSION = '0.2.1-a1'
# Keys an accepted account document carries that are not DAPPER groups.
NON_GROUPS = frozenset({'prefixes'})


def verify_vendor():
    """The vendored DAPPER is the recorded commit's files, byte for byte. Checked when the image is built and in tests,
    so a deployment cannot mint with an edited or missing copy."""
    recorded = json.loads((VENDOR / 'VENDOR.json').read_text())
    for name, digest in recorded['files'].items():
        if hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Vendored DAPPER {DAPPER_VERSION} file {name} differs from its recorded checksum')
    if recorded['version'] != DAPPER_VERSION:
        raise RuntimeError('Vendored DAPPER version differs from the one this module mints with')
    return recorded


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
        raise DepositError('DAPPER ' + DAPPER_VERSION + ' could not mint the account: ' + done.stderr.strip()[-600:])
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


def mint(document):
    """(document minted with the vendored DAPPER 0.2.1-a1, {old id: new id}). Every id is verified after minting,
    each object appears once, and every dapper reference must resolve inside the document."""
    mapping = None
    for _ in range(6):  # each pass can only merge; a document of a few levels settles in two or three
        result = _mint_once(document)
        mapping = result['mapping'] if mapping is None else {old: result['mapping'].get(new, new) for old, new in mapping.items()}
        document = result['document']
        if not _collapse(document):
            break
    else:
        raise DepositError('The account did not settle on one id per object')
    if result['unresolved']:
        raise DepositError('The account references objects it does not contain: ' + ', '.join(result['unresolved'][:5]))
    return document, mapping


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


def deposit_document(account_document, sources_root, calls, ledger_root, *, attribute_requester=False):
    """(the account in its deposit form, a summary) from an accepted account document, the folder its sources were
    saved in (the run's output/) and the run's tool ledger (`calls`, request paths relative to `ledger_root`).

    The accepted document is attributed to its requester (assemble_account always does that). The deposit form names
    them only when `attribute_requester` is true; otherwise the requester's Person is left out and the account and
    its Claims are attributed to the service (SERVICE_AGENT): what made it, not who asked.

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

    minted, mapping = mint(document)
    account = minted['scientific_accounts'][0]
    files = binding['files']
    summary = {'account_id': account['id'], 'dapper_version': DAPPER_VERSION,
               'objects': sum(len(rows) for key, rows in minted.items() if not key.endswith('_edges')),
               'edges': sum(len(rows) for key, rows in minted.items() if key.endswith('_edges')),
               'claims': len(minted.get('claims', [])), 'value_locators': len(minted.get('value_locators', [])),
               'papers': [{'path': entry['path'], 'url': entry['url'], 'sha256': entry['sha256'], 'size_bytes': entry['size_in_bytes'],
                           'media_type': entry['mime_type'], 'command': entry['command'], 'observed_command': entry['observed_command'],
                           'derived_from': files[entry['derived_from']]['path'] if entry['derived_from'] else None,
                           'evidence_items': quoted.get(identity, 0), 'file_id': mapping.get(identity, identity)}
                          for identity, entry in sorted(files.items(), key=lambda pair: pair[1]['path'])]}
    return minted, summary


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
