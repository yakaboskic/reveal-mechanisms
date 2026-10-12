"""Each saved account's traced data sources: the Datasets its claims reach, how, and who is credited for them.

A new account is traced once, when an agent run or a local submission saves it, before the acceptance write
fence; the compact result is written in the same commit and served with the account. Tracing never fails an
acceptance: an error is recorded as `unavailable`. GET /v1/accounts/{id}/provenance keeps the full on-demand
trace, and both go through provenance_api.trace. A stored record is a point-in-time observation of the reference
tables: re-run scripts/backfill_account_data_sources.py --force after a lineage curation or policy change.
"""
from collections import Counter, defaultdict
import concurrent.futures
from copy import deepcopy
import json
import logging
from urllib.parse import urlsplit

from .account_provenance import TRACING_POLICY_VERSION
from .evidence_package import canonical_json, sha256
from .repository import digest, now

log = logging.getLogger(__name__)
KIND = 'account_data_sources'
FORMAT = 'reveal.account-data-sources/1'
DEADLINE_SECONDS = 90
MAX_RECORD_BYTES = 512_000
NAME_LIMIT = 300
DESCRIPTION_LIMIT = 600
SERVED = ('status', 'traced_at', 'error', 'reference_generation_ids', 'coverage', 'claims', 'datasets',
          'organizations', 'via')


def owner_key(owner, account_id): return digest([owner, account_id])


def publication_key(snapshot_id): return digest(['publication', snapshot_id])


def _text(value, limit):
    return value[:limit] if isinstance(value, str) and value.strip() else None


def _public_url(value):
    """Locations can name storage prefixes; only plain public web addresses are served."""
    try: parts = urlsplit(value) if isinstance(value, str) else None
    except ValueError: return None
    return value if parts and parts.scheme in ('http', 'https') and parts.netloc and not parts.username and not parts.password else None


def _described(identity, record, node, fields):
    item = {'id': identity}
    for key, limit in fields:
        value = _public_url(record.get(key)) if key == 'location' else _text(record.get(key, node.get(key)), limit)
        if value: item[key] = value
    return item


def compact(traced):
    """The stored fields of one trace (provenance_api.trace): per claim, the datasets it reaches."""
    result, snapshot = traced['result'], traced['snapshot']
    records = {row['id']: row for rows in traced['document'].values() if isinstance(rows, list)
               for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)}
    nodes = {node['id']: node for node in result['nodes']}
    edges = {edge['id']: edge for edge in result['edges']}
    reached = defaultdict(dict)
    for row in result['traces']:
        if row['claim_id'] is None: continue  # Context references never contribute claim support.
        entry = reached[row['claim_id']].setdefault(row['dataset_id'], {'input_roles': set(), 'routes': set(),
            'via_ids': set(), 'organizations': defaultdict(set), 'curation_ids': set(), 'trace_count': 0})
        entry['input_roles'].update(row['input_roles']); entry['routes'].update(row['route_classifications'])
        entry['via_ids'].add(row['source_id']); entry['trace_count'] += 1
        for attribution in row['organization_attributions']:
            entry['organizations'][attribution['organization_id']].add(attribution['role'])
        for edge_id in row['path_edge_ids']:
            for origin in edges.get(edge_id, {}).get('provenance_source', []):
                if origin.get('kind') == 'curated_lineage' and isinstance(origin.get('curation_id'), str):
                    entry['curation_ids'].add(origin['curation_id'])
    claims = []
    for wrapper in result['claims']:
        identity = wrapper['record']['id']
        claims.append({'claim_id': identity, 'resolution': wrapper['provenance']['resolution'], 'datasets': [
            {'dataset_id': dataset, 'input_roles': sorted(entry['input_roles']), 'routes': sorted(entry['routes']),
             'via_ids': sorted(entry['via_ids']),
             'organizations': [{'organization_id': key, 'roles': sorted(roles)} for key, roles in sorted(entry['organizations'].items())],
             'curation_ids': sorted(entry['curation_ids']), 'trace_count': entry['trace_count']}
            for dataset, entry in sorted(reached.get(identity, {}).items())]})
    connections = [item for claim in claims for item in claim['datasets']]
    coverage = result['coverage']
    return {'status': 'complete' if coverage['status'] == 'complete' else 'incomplete',
        'reference_generation_ids': snapshot['reference_generation_ids'],
        'source_observations_sha256': sha256(canonical_json(snapshot['source_observations'])),
        'coverage': {'trace_count': coverage['trace_count'], 'counts_are_lower_bounds': coverage['counts_are_lower_bounds'],
                     'issue_counts': dict(sorted(Counter(issue['code'] for issue in coverage['issues']).items()))},
        'claims': claims,
        'datasets': [_described(identity, records.get(identity, {}), nodes.get(identity, {}),
                                (('name', NAME_LIMIT), ('version', NAME_LIMIT), ('description', DESCRIPTION_LIMIT), ('location', None)))
                     for identity in sorted({item['dataset_id'] for item in connections})],
        'organizations': [_described(identity, records.get(identity, {}), nodes.get(identity, {}), (('name', NAME_LIMIT), ('ror', NAME_LIMIT)))
                          for identity in sorted({org['organization_id'] for item in connections for org in item['organizations']})],
        'via': [{**_described(identity, records.get(identity, {}), nodes.get(identity, {}), (('name', NAME_LIMIT), ('filename', NAME_LIMIT))),
                 'kind': nodes.get(identity, {}).get('kind', 'unresolved')}
                for identity in sorted({via for item in connections for via in item['via_ids']})]}


def _error_code(error):
    from .artifact_store import StorageUnavailable
    from .auth import Problem
    from .mysql_pool import DatabaseBusy
    if isinstance(error, Problem): return error.code
    if isinstance(error, StorageUnavailable): return 'STORAGE_UNAVAILABLE'
    if isinstance(error, DatabaseBusy): return 'DATABASE_BUSY'
    return 'TRACE_FAILED'


def stored(account_id, document_sha256, account_payload_sha256, fields):
    """A complete stored record from compact (or failure) fields; oversized results become `unavailable`."""
    record = {'format': FORMAT, 'account_id': account_id, 'document_sha256': document_sha256,
              'account_payload_sha256': account_payload_sha256, 'policy_version': TRACING_POLICY_VERSION,
              'traced_at': now(), **fields}
    if len(json.dumps(record, separators=(',', ':')).encode()) > MAX_RECORD_BYTES:
        record = {key: value for key, value in record.items() if key not in SERVED or key == 'traced_at'}
        record.update(status='unavailable', error={'code': 'DATA_SOURCES_LIMIT_EXCEEDED'})
    return record


class _LazyRead:
    """The trace reads at most a registered supplement row: open a read transaction only if it does."""
    def __init__(self, repository): self.repository, self.context, self.tx = repository, None, None
    def get(self, kind, identity):
        if self.tx is None:
            self.context = self.repository.read_transaction(); self.tx = self.context.__enter__()
        return self.tx.get(kind, identity)
    def close(self):
        if self.context is not None: self.context.__exit__(None, None, None)


def compute(repository, owner, account_id, document, document_sha256, records, *, deadline=DEADLINE_SECONDS):
    """The record for a new account, traced before its acceptance commits. Never raises.

    `records` are the retained capture records the same commit writes, keyed by checksum, in its write order.
    Nothing is withheld yet: the account's reuse receipts were just authorized for this owner."""
    from .provenance_api import trace, visible_records
    account = next((node for node in document.get('scientific_accounts', []) if node.get('id') == account_id), None)
    payload = sha256(canonical_json(account)) if account else None
    source = {'document': document, 'public': None, 'user': owner, 'publication_record': None,
              'withheld_ids': set(), 'withheld_dependencies': False, 'root_payload': payload}
    def run():
        tx = _LazyRead(repository)
        try: return compact(trace(tx, account_id, source, visible_records(document, records)))
        finally: tx.close()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='account-data-sources')
    try:
        if account is None: raise ValueError('The document does not contain the account')
        future = executor.submit(run)
        try: fields = future.result(timeout=deadline)
        except concurrent.futures.TimeoutError:
            if future.done(): raise  # The trace itself raised a TimeoutError, such as DatabaseBusy.
            fields = {'status': 'unavailable', 'error': {'code': 'TRACE_TIMEOUT'}}
    except Exception as error:  # A trace is an annotation: it never becomes the acceptance's failure.
        log.warning('Account data sources were not traced for %s (%s)', account_id, type(error).__name__)
        fields = {'status': 'unavailable', 'error': {'code': _error_code(error)}}
    finally:
        executor.shutdown(wait=False)
    return stored(account_id, document_sha256, payload, fields)


def trace_source(tx, account_id, source, document_sha256):
    """The record for an already stored, authorized account source (the backfill); raises like the endpoint."""
    from .provenance_api import artifact_records, trace
    return stored(account_id, document_sha256, source['root_payload'],
                  compact(trace(tx, account_id, source, artifact_records(tx, source))))


def summary(record):
    claims = record.get('claims') or []
    return {'claims': sum(1 for claim in claims if claim['datasets']), 'datasets': len(record.get('datasets') or [])}


def publish(tx, owner, account_id, snapshot_id, snapshot):
    """Freeze the owner's record beside a new publication snapshot when it traced this exact account payload."""
    row = tx.get(KIND, owner_key(owner, account_id))
    account = next((node for node in snapshot['document'].get('scientific_accounts', []) if node.get('id') == account_id), None)
    data = row['data'] if row and row['owner'] == owner else None
    if (not data or not account or not current(data)
            or data.get('account_payload_sha256') != sha256(canonical_json(account))): return False
    tx.put(KIND, publication_key(snapshot_id), owner, {**deepcopy(data), 'snapshot_id': snapshot_id})
    return True


def current(data):
    """Whether a stored record was traced under the current record format and tracing policy."""
    return data.get('format') == FORMAT and data.get('policy_version') == TRACING_POLICY_VERSION


def _withhold(view, withheld):
    """Drop connections that only reach a dataset through objects this reader may no longer see."""
    claims = []
    for claim in view.get('claims', []):
        if claim['claim_id'] in withheld: continue
        datasets = []
        for item in claim['datasets']:
            via = [identity for identity in item['via_ids'] if identity not in withheld]
            if via: datasets.append({**item, 'via_ids': via})
        claims.append({**claim, 'datasets': datasets})
    keep = {item['dataset_id'] for claim in claims for item in claim['datasets']}
    orgs = {org['organization_id'] for claim in claims for item in claim['datasets'] for org in item['organizations']}
    via = {identity for claim in claims for item in claim['datasets'] for identity in item['via_ids']}
    view.update(claims=claims, status='incomplete', datasets=[row for row in view.get('datasets', []) if row['id'] in keep],
                organizations=[row for row in view.get('organizations', []) if row['id'] in orgs],
                via=[row for row in view.get('via', []) if row['id'] in via])
    return view


def response(tx, account_id, source):
    """The data sources served with an account response: the record traced from exactly this source, if any."""
    public = source.get('public')
    if public is not None:
        snapshot_id = public['data'].get('snapshot_id')
        row = tx.get(KIND, publication_key(snapshot_id)) if snapshot_id else None
        data = row['data'] if row and row['owner'] == public['owner'] else None
    else:
        user = source.get('user')
        row = tx.get(KIND, owner_key(user, account_id)) if user else None
        data = (row['data'] if row and row['owner'] == user and source.get('document_sha256')
                and row['data'].get('document_sha256') == source['document_sha256'] else None)
    if not data or not current(data) or data.get('account_payload_sha256') != source.get('root_payload'):
        return {'status': 'not_traced'}
    view = {key: deepcopy(data[key]) for key in SERVED if key in data}
    if source.get('withheld_dependencies') and view.get('claims') is not None:
        view = _withhold(view, set(source.get('withheld_ids') or ()))
    return view
