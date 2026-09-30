"""Explicit publication snapshots; never impersonate an owner for public reads."""
from copy import deepcopy
from .auth import Problem, owned
from .repository import digest, now, uid

_UNREAD = object()

def state(tx, owner, account_id, *, can_manage=False, record=_UNREAD, account_result=None):
    row = tx.get('publication', digest([owner, account_id])) if record is _UNREAD else record
    data = row['data'] if row and row['owner'] == owner else {}
    if can_manage and data.get('visibility') == 'public' and account_result is None:
        account = tx.get('account', digest([owner, account_id]))
        account_result = account['data']['result'] if account and account['owner'] == owner else None
    statement = (account_result or {}).get('research_statement', {}) if can_manage else {}
    changed = (data.get('visibility') == 'public' and statement.get('status') == 'succeeded'
        and statement.get('paragraph_id') != data.get('paragraph_id'))
    return {'visibility': data.get('visibility', 'private'), 'version': data.get('version', 0),
        'published_at': data.get('published_at'), 'updated_at': data.get('updated_at'), 'can_manage': can_manage,
        'has_unpublished_changes': bool(changed)}


def full_document(tx, owner, identity, result):
    reference = tx.get('object_document', digest([owner, identity]))
    if not reference:
        observations = [row for row in tx.list('object_observation', owner) if row['data'].get('object_id') == identity]
        if observations: reference = {'owner': owner, 'data': {'sha256': observations[-1]['data']['document_sha256']}}
    if reference and reference['owner'] == owner:
        row = tx.get('scientific_document', digest([owner, reference['data']['sha256']]))
        if row and row['owner'] == owner:
            return row['data']['document'], row['data'].get('citation_metadata', result.get('citation_metadata', [])), row['data'].get('artifact_access', {})
    if result.get('coverage', {}).get('complete') is False:
        raise Problem(409, 'PUBLICATION_INCOMPLETE', 'The complete accepted document is unavailable; a partial response cannot be published.')
    return result['document'], result.get('citation_metadata', []), {a['file']['id']: a for a in result.get('artifacts', [])}


def freeze(tx, owner, account_id):
    from .acceptance import object_envelope
    from .account_discovery import visible_accounts
    data = owned(tx, 'account', account_id, owner)['data']; result = data['result']
    summaries = [item for item in visible_accounts(tx, owner, attribution=True) if item['account']['id'] == account_id]
    if len(summaries) != 1: raise Problem(409, 'ACCOUNT_NOT_ACCEPTED', 'Only a saved accepted scientific account can be published.')
    inputs = [(account_id, result)]
    statement = result.get('research_statement', {})
    paragraph_id = statement.get('paragraph_id') if statement.get('status') == 'succeeded' else None
    if paragraph_id:
        paragraph = owned(tx, 'paragraph', paragraph_id, owner)['data']
        if paragraph.get('account_id') != account_id: raise Problem(409, 'PUBLICATION_CONFLICT', 'The saved statement belongs to another account.')
        inputs.append((paragraph_id, paragraph['result']))
    document, citations, artifacts = {}, {}, {}
    nodes = {}
    for identity, envelope in inputs:
        source, metadata, access = full_document(tx, owner, identity, envelope)
        count = sum(len(rows) for rows in source.values() if isinstance(rows, list)) + 1
        # Freeze the complete upstream graph, not a paginated first response.
        closure = object_envelope(source, identity, metadata, access, max_depth=count, max_nodes=count)
        if not closure['coverage']['complete']: raise Problem(409, 'PUBLICATION_INCOMPLETE', 'The accepted provenance closure is incomplete.')
        for group, rows in closure['document'].items():
            for node in rows:
                key = node.get('id') or digest([group, node])
                if key in nodes and nodes[key] != node: raise Problem(409, 'PUBLICATION_CONFLICT', 'Conflicting observations cannot share one public snapshot.')
                if key not in nodes: document.setdefault(group, []).append(deepcopy(node)); nodes[key] = node
        for citation in closure['citation_metadata']:
            citations[(citation['target_id'], citation['metadata_revision'])] = deepcopy(citation)
        for artifact in closure['artifacts']: artifacts[artifact['file']['id']] = deepcopy(artifact)
    # Only Files actually reachable from the published roots authorize bytes.
    captured = {}
    for file in document.get('files', []):
        checksum = file.get('sha256')
        row = tx.get('artifact', digest([owner, checksum])) if checksum else None
        if row and row['owner'] == owner and row['data'].get('file', {}).get('id') == file['id']:
            captured[checksum] = deepcopy(row['data'])
    # Freeze the exact registry revisions cited by the accepted paragraph.
    from .citations import get
    for paragraph in document.get('paragraphs', []):
        for occurrence in paragraph.get('citations', []):
            target, revision = occurrence['target_id'], occurrence['citation_metadata_revision']
            if target not in nodes: raise Problem(409, 'PUBLICATION_INCOMPLETE', 'A cited scientific target is absent from the public closure.')
            citations[(target, revision)] = deepcopy(get(tx, owner, target, revision))
    summary = deepcopy(summaries[0]); summary['job_id'] = None
    summary['research_statement'] = {'status': 'succeeded' if paragraph_id else 'not_requested', 'job_id': None, 'paragraph_id': paragraph_id}
    return {'account_id': account_id, 'paragraph_id': paragraph_id, 'document': document,
        'citation_metadata': list(citations.values()), 'artifacts': artifacts, 'artifact_records': captured, 'summary': summary}


def change(tx, owner, account_id, visibility, expected_version):
    owned(tx, 'account', account_id, owner)  # Publishing never inherits public read permission.
    key = digest([owner, account_id])
    current = state(tx, owner, account_id, can_manage=True)
    if current['version'] != expected_version:
        raise Problem(409, 'PUBLICATION_VERSION_CONFLICT', 'Publication changed in another session; refresh before choosing again.', current_version=current['version'])
    stamp = now(); data = {'account_id': account_id, 'visibility': visibility, 'version': current['version'] + 1,
        'published_at': current['published_at'] if current['visibility'] == 'public' and visibility == 'public' else stamp if visibility == 'public' else None,
        'updated_at': stamp, 'snapshot_id': None, 'paragraph_id': None, 'object_ids': [], 'citation_keys': [], 'artifact_sha256': [], 'summary': None}
    if visibility == 'public':
        snapshot = freeze(tx, owner, account_id); snapshot_id = uid()
        tx.put('publication_snapshot', snapshot_id, owner, snapshot)
        data.update(snapshot_id=snapshot_id, paragraph_id=snapshot['paragraph_id'], object_ids=sorted({node['id'] for rows in snapshot['document'].values() for node in rows if 'id' in node}),
            citation_keys=sorted(f"{row['target_id']}:{row['metadata_revision']}" for row in snapshot['citation_metadata']),
            artifact_sha256=sorted(snapshot['artifact_records']), summary=snapshot['summary'])
    tx.put('publication', key, owner, data)
    return state(tx, owner, account_id, can_manage=True)


def published(tx):
    return sorted((row for row in tx.list('publication') if row['data'].get('visibility') == 'public' and row['data'].get('snapshot_id')),
        key=lambda row: (row['data']['published_at'], row['id']))


def find(tx, identity, kind='object', revision=None):
    for row in published(tx):
        data = row['data']
        if kind == 'account': allowed = identity == data['account_id']
        elif kind == 'paragraph': allowed = identity in data['object_ids'] and identity.startswith('dapper:Paragraph.')
        elif kind == 'citation': allowed = (f'{identity}:{revision}' in data['citation_keys'] if revision is not None else any(key.rsplit(':', 1)[0] == identity for key in data['citation_keys']))
        elif kind == 'artifact': allowed = identity in data['artifact_sha256']
        else: allowed = identity in data['object_ids']
        if not allowed: continue
        snapshot = tx.get('publication_snapshot', data['snapshot_id'])
        if snapshot and snapshot['owner'] == row['owner']:
            return row, snapshot['data']
    raise Problem(404, 'NOT_FOUND', 'The requested scientific resource is unavailable.')


def public_accounts(tx):
    unique = {}
    for row in published(tx):
        summary = deepcopy(row['data']['summary'])
        if summary and summary['account']['id'] not in unique:
            summary['publication'] = state(tx, row['owner'], summary['account']['id'], record=row)
            unique[summary['account']['id']] = summary
    items = sorted(unique.values(), key=lambda item: item['account']['id'])
    return sorted(items, key=lambda item: item['created_at'], reverse=True)


class SnapshotReader:
    """Minimal citation adapter with no access to any underlying private rows."""
    user = 'published-snapshot'
    def __init__(self, snapshot): self.snapshot = snapshot
    def get(self, kind, identity):
        if kind == 'citation':
            item = next((row for row in self.snapshot['citation_metadata'] if f"{row['target_id']}:{row['metadata_revision']}" == identity), None)
            return {'owner': self.user, 'data': item} if item else None
        if kind == 'paragraph':
            paragraph = next((node for node in self.snapshot['document'].get('paragraphs', []) if digest([self.user, node['id']]) == identity), None)
            return {'owner': self.user, 'data': {'result': {'document': self.snapshot['document']}}} if paragraph else None
        return None
    def list(self, kind, owner=None):
        return [{'owner': self.user, 'data': row} for row in self.snapshot['citation_metadata']] if kind == 'citation' else []
    def put(self, kind, identity, owner, data):
        if kind != 'citation_rendering': raise RuntimeError('Public snapshot is read-only')
        # Rendering is deterministic and needs no persistent owner-side cache.
