"""The rows a scientific acceptance writes, batched under the caller's one fenced transaction.

Hosted account acceptance (Worker.accept_accounts) and MCP submission (research_execution.commit_accounts) read
every row they may touch in one statement first (acceptance_keys). Citation registration, grants, object
observations, objects and the callers' own rows are then planned in sequential put() order and written by one
Transaction.apply_puts: final owners, payloads, versions and workspace events equal those of the per-row puts it
replaces, in a few multi-row statements instead of about 14 per DAPPER node.
"""
from .acceptance import with_citations
from .evidence_package import canonical_json, sha256
from .repository import digest

CITED = (('knowledge_gaps', 'KnowledgeGap'), ('questions', 'Question'), ('claims', 'Claim'))


def dapper_nodes(document):
    for rows in document.values():
        if isinstance(rows, list):
            for node in rows:
                if isinstance(node, dict) and str(node.get('id', '')).startswith('dapper:'): yield node


def observation_key(owner, node):
    return digest([owner, node['id'], sha256(canonical_json(node))])


def acceptance_keys(owner, documents):
    """Every citation, grant, object and observation key accepting these documents can touch."""
    keys = [('citation_actor', owner)]
    for document in documents:
        keys += [('citation', node['id'] + ':1') for group, _ in CITED for node in document.get(group, [])]
        for node in dapper_nodes(document):
            key = digest([owner, node['id']])
            keys += [('grant', key), ('object', key), ('object_document', key), ('object_observation', observation_key(owner, node))]
    return keys


class AcceptanceWrites:
    """Planned puts in first-write order: (kind, id) -> [owner, last data, number of puts]. A key read earlier in
    this transaction (acceptance_keys) costs no further read; others are read by apply_puts as put() would."""
    def __init__(self, tx, owner):
        self.tx, self.owner = tx, owner
        self.writes, self.fresh, self.objects = {}, [], {}

    def put(self, kind, identity, owner, data):
        entry = self.writes.get((kind, identity))
        if entry is None: self.writes[(kind, identity)] = [owner, data, 1]
        else: entry[:] = [owner, data, entry[2] + 1]

    def insert(self, rows):
        """Rows whose keys are new uuids: inserted unread."""
        self.fresh.extend(rows)

    def get(self, kind, identity):
        """A row as the sequential puts would have read it: this commit's planned value, else the stored row."""
        entry = self.writes.get((kind, identity))
        return {'owner': entry[0], 'data': entry[1]} if entry else self.tx.get(kind, identity)

    def planned(self, kind, identity):
        return (kind, identity) in self.writes

    def document(self, document, document_sha256, metadata, projection, borrowed_ids=()):
        """Grants, observations (last write wins) and, for objects absent before this commit, the object and its
        document index (first document wins). projection(node_id) gives that node's object_projection."""
        for node in dapper_nodes(document):
            key = digest([self.owner, node['id']])
            if node['id'] not in borrowed_ids: self.put('grant', key, self.owner, {'target_id': node['id']})
            self.put('object_observation', observation_key(self.owner, node), self.owner,
                     {'object_id': node['id'], 'payload': node, 'document_sha256': document_sha256})
            if key not in self.objects and not self.tx.exists('object', key):
                self.objects[key] = (node['id'], document_sha256, metadata, projection(node['id']))

    def flush(self):
        for key, (identity, document_sha256, metadata, projection) in self.objects.items():
            self.put('object', key, self.owner, with_citations(projection, metadata))
            self.put('object_document', key, self.owner, {'object_id': identity, 'sha256': document_sha256})
        self.tx.apply_puts([(kind, identity, *entry) for (kind, identity), entry in self.writes.items()], self.fresh)
        self.writes, self.fresh, self.objects = {}, [], {}
