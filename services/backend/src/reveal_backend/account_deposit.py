"""The Scientific Account a general agent job returns and deposits.

An agent authors its account with the pinned DAPPER release, citing each paper read by the tool response it
received and a verbatim snippet. That form is right for REVEAL's own gate (the linter checks every snippet against
the captured response) and wrong for anyone else: the cited File is a REVEAL-internal JSON capture.

`deposit_document` rewrites the accepted account into the form that leaves REVEAL:

  - each literature EvidenceItem points, through a DAPPER ValueLocator, at the exact byte span of its quote in the
    paper's text (literature_locator), and references a Publication;
  - the paper's text is a File identified by checksum with no location (REVEAL keeps the bytes and does not
    redistribute them), derived from a File for the upstream Europe PMC document, whose location is public;
  - REVEAL's capture Files are dropped;
  - everything is minted once, with DAPPER 0.2.1-a1 (the release that has ValueLocator), vendored at
    services/backend/vendor/dapper-0.2.1-a1. DAPPER ids are the same in 0.2.0 and 0.2.1-a1, so nodes this rewrite
    does not touch keep the ids the pinned release gave them.

The ids of the returned document are the account's only public ids. Ids minted earlier, by the pinned release's
gate, name the internal form and are never stored or returned.
"""
from copy import deepcopy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import literature_locator as locator

VENDOR = Path(__file__).resolve().parents[2] / 'vendor/dapper-0.2.1-a1'
DAPPER_VERSION = '0.2.1-a1'
INTERNAL_LOCATOR = re.compile(r'ledger_sequence=\d+;pointer=[^\s.,;)]+')
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


def _references(value, identity):
    if isinstance(value, str):
        return value == identity
    if isinstance(value, list):
        return any(_references(item, identity) for item in value)
    if isinstance(value, dict):
        return any(_references(item, identity) for item in value.values())
    return False


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


def deposit_document(account_document, ledger_root, calls, *, attribute_requester=False):
    """(the account in its deposit form, a summary) from an accepted account document and the run's tool ledger.

    The accepted document is attributed to its requester (assemble_account always does that). The deposit form names
    them only when `attribute_requester` is true; otherwise the requester's Person is left out and the account and
    its Claims are attributed to the service (SERVICE_AGENT): what made it, not who asked.

    `account_document` is what acceptance.assemble_account returned: valid against the pinned release, attributed,
    its sources checked. `calls` is the ledger manifest's `calls`. Raises DepositError when a literature
    EvidenceItem cannot be located (literature_locator.LocatorError explains how to fix it) or the result is not
    self-contained.

    The summary lists, for each paper cited, its text file (checksum, bytes) so the caller can keep those bytes."""
    document = {key: deepcopy(rows) for key, rows in account_document.items() if key not in NON_GROUPS}
    papers = locator.paper_texts(ledger_root, calls)
    files = {node['id']: node for node in document.get('files', []) if isinstance(node, dict)}
    captures = {identity: locator.paper_for_capture(papers, node.get('sha256')) for identity, node in files.items()}
    captures = {identity: paper for identity, paper in captures.items() if paper is not None}
    accounts = document.get('scientific_accounts', [])
    if len(accounts) != 1:
        raise DepositError('An account document holds exactly one ScientificAccount')
    activity = accounts[0].get('was_generated_by')
    if not attribute_requester:
        _reattribute(document, set(accounts[0].get('was_attributed_to') or []),
                     {'id': 'urn:reveal:deposit:organizations:service', **SERVICE_AGENT})

    new = {'files': {}, 'value_locators': {}}
    text_ids, derived_edges, cited = {}, [], {}
    publications = []  # one per paper, however it was read: the abstract by PMID and the full text by PMC id are one paper

    def publication_for(paper):
        described = locator.publication(paper)
        names = {described.get(key) for key in ('pmid', 'doi', 'citation')} - {None}
        if not described:
            return None
        known = next((entry for entry in publications if entry['names'] & names), None) if names else None
        if known is None:
            known = {'names': set(), 'node': {}, 'id': f'urn:reveal:deposit:publications:{len(publications) + 1}'}
            publications.append(known)
        known['names'] |= names
        for key, value in described.items():
            known['node'].setdefault(key, value)
        return known

    def node_id(group, key, content):
        """One temporary id per distinct new node; mint() replaces it with the node's DAPPER id."""
        identity = new[group].setdefault(key, {'id': f'urn:reveal:deposit:{group}:{len(new[group]) + 1}', **content})['id']
        return identity

    quotes, problems = locator.document_quotes(document, papers, locator.captures(calls))
    if problems:
        where, message = problems[0]
        raise DepositError(f'{where}: {message}')
    for index, paper, span in quotes:
        item = document['evidence_items'][index]
        sources = item['was_derived_from']
        text_file = locator.text_file(paper)
        text_id = node_id('files', ('text', text_file['sha256']), text_file)
        if paper['key'] not in text_ids:
            text_ids[paper['key']] = text_id
            upstream_id = node_id('files', ('upstream', paper['upstream']['sha256']), locator.upstream_file(paper))
            derived_edges.append({'subject': text_id, 'predicate': 'prov:wasDerivedFrom', 'object': upstream_id})
        located = node_id('value_locators', (text_file['sha256'], span['byte_start'], span['byte_length']), {
            'file': text_id, 'byte_start': span['byte_start'], 'byte_length': span['byte_length'], 'value_text': span['value_text'],
            'selector': span['selector'], 'media_type': span['media_type']})
        published = publication_for(paper)
        item['snippet'] = span['value_text']
        item['value_locators'] = [located]
        item['was_derived_from'] = [source for source in sources if source not in captures] + [text_id]
        if published:
            item['reference'] = published['id']
            item['_publication'] = published['id']
        if isinstance(item.get('context'), str):
            # The draft's locator names REVEAL's ledger; the deposit's names the place in the paper.
            item['context'] = INTERNAL_LOCATOR.sub(span['selector'], item['context'])
        entry = cited.setdefault(paper['key'], {'reference': locator.reference(paper), 'text_sha256': text_file['sha256'],
                                                'text_bytes': text_file['size_in_bytes'], 'media_type': text_file['mime_type'],
                                                'filename': text_file['filename'], 'evidence_items': 0})
        entry['evidence_items'] += 1

    # REVEAL's capture Files leave once nothing cites them; a capture some object still cites stays. An Activity that
    # used a capture is not a citation: below, it is recorded as having used the paper text instead.
    remaining = {key: rows for key, rows in document.items() if key not in ('files', 'used_edges')}
    internal = {identity for identity, node in files.items() if node.get('sha256') in locator.captures(calls)}
    kept = sorted(identity for identity in internal if _references(remaining, identity))
    if kept:
        # Nothing a lint-clean literature account cites is left pointing at a capture; this is a document that was not.
        raise DepositError('The account still cites REVEAL tool captures, which are not deposited: ' + ', '.join(kept[:3]))
    document['files'] = [node for node in document.get('files', []) if node['id'] not in internal]
    dropped = internal
    document['used_edges'] = [edge for edge in document.get('used_edges', []) if edge.get('object') not in dropped]
    document['files'] += list(new['files'].values())
    titles = {entry['id']: entry['node'].get('title') for entry in publications}
    for item in document.get('evidence_items', []):
        cited_publication = item.pop('_publication', None)
        if titles.get(cited_publication):
            item['reference_title'] = titles[cited_publication]
    document['publications'] = [*document.get('publications', []), *({'id': entry['id'], **entry['node']} for entry in publications)]
    document['value_locators'] = [*document.get('value_locators', []), *new['value_locators'].values()]
    document['was_derived_from_edges'] = [*document.get('was_derived_from_edges', []), *derived_edges]
    if activity:
        document['used_edges'] += [{'subject': activity, 'predicate': 'prov:used', 'object': text_id} for text_id in text_ids.values()]

    minted, mapping = mint(document)
    account = minted['scientific_accounts'][0]
    summary = {'account_id': account['id'], 'dapper_version': DAPPER_VERSION,
               'objects': sum(len(rows) for key, rows in minted.items() if not key.endswith('_edges')),
               'edges': sum(len(rows) for key, rows in minted.items() if key.endswith('_edges')),
               'claims': len(minted.get('claims', [])), 'value_locators': len(minted.get('value_locators', [])),
               'papers': [{'source': key[0], 'id': key[1], 'section': key[2], **entry, 'file_id': mapping[text_ids[key]]}
                          for key, entry in sorted(cited.items())]}
    return minted, summary


def paper_text_bytes(ledger_root, calls, sha256):
    """The bytes of one cited paper text, by checksum, for the caller to keep. None when the run read no such text."""
    for paper in locator.paper_texts(ledger_root, calls).values():
        if paper['text_sha256'] == sha256:
            return locator.text_bytes(paper)
    return None


def paper_text_files(ledger_root, calls, checksums):
    """{sha256: bytes} of the cited paper texts, rebuilt in one pass over the ledger."""
    wanted = set(checksums)
    return {paper['text_sha256']: locator.text_bytes(paper) for paper in locator.paper_texts(ledger_root, calls).values()
            if paper['text_sha256'] in wanted}
