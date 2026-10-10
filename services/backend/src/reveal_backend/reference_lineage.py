"""Collection lineage of a reference release, for serving: no database, files or network.

A reveal_ref_collections payload carries `provenance` (every provenance group of the producer's collection document,
reference_release.collection_provenance) and, where lap/config/source_datasets.yaml linked the raw files of a collection
whose producer names no Dataset, `curated_provenance.groups` (reference_release.curate). collection_lineage merges the
two; lineage_closure keeps what one gene set's construction depended on, with the fan-in of an activity's inputs bounded:
inputs that share their onward lineage are represented by one of them (GlyGen HZ2's 6,620 cached API responses all
derive from the GlyGen release, so they become one path to it).
"""
from collections import defaultdict, deque

from .account_provenance import ORG_ROLES
from .provenance_schema import PROVENANCE_EDGES, PROVENANCE_GROUPS, PROVENANCE_NODES

# The Dataset -> Organization fields (account_provenance's organization roles but an Activity's was_associated_with).
ORG_FIELDS = tuple(field for field in ORG_ROLES if field != 'was_associated_with')
# An Activity's agents (its was_associated_with and the same organization roles).
AGENT_FIELDS = tuple(ORG_ROLES)
GROUP_THRESHOLD = 25  # more inputs of one activity with one lineage signature than this keep one representative
EDGE_KEY = ('subject', 'predicate', 'object')


def _refs(value):
    return [item for item in (value if isinstance(value, list) else [value]) if isinstance(item, str) and item]


def _edge_key(edge): return tuple(edge.get(key) for key in EDGE_KEY)


def collection_lineage(payload):
    """{group: list} of a collection: payload['provenance'] merged with payload['curated_provenance']['groups']. Node
    groups are deduplicated by id (the producer's node wins), edge groups by (subject, predicate, object), and prefixes
    merged (the producer's wins); producer items come first."""
    producer = payload.get('provenance') or {}
    curated = (payload.get('curated_provenance') or {}).get('groups') or {}
    merged = {}
    for group in PROVENANCE_GROUPS:
        sources = [source[group] for source in (producer, curated) if source.get(group)]
        if not sources: continue
        if group == 'prefixes':
            merged[group] = {key: value for source in reversed(sources) for key, value in source.items()}
            continue
        key, kept = _edge_key if group in PROVENANCE_EDGES else (lambda item: item.get('id')), {}
        for source in sources:
            for item in source:
                if isinstance(item, dict): kept.setdefault(key(item), item)
        merged[group] = list(kept.values())
    return merged


class _Index:
    """A lineage's nodes and relations: relation -> {id: [(target, edge group or None, edge or None)]}; a relation stated by
    a node field has no edge record."""
    def __init__(self, lineage):
        self.nodes = {node['id']: group for group in PROVENANCE_NODES for node in lineage.get(group) or []
                      if isinstance(node, dict) and isinstance(node.get('id'), str)}
        self.records = {node['id']: node for group in PROVENANCE_NODES for node in lineage.get(group) or []
                        if isinstance(node, dict) and isinstance(node.get('id'), str)}
        self.out, self.members = defaultdict(lambda: defaultdict(list)), defaultdict(list)
        for identity, node in self.records.items():
            for field in ('was_generated_by', 'used', 'was_derived_from', *AGENT_FIELDS):
                for target in _refs(node.get(field)): self.out[field][identity].append((target, None, None))
            if self.nodes[identity] == 'datasets':
                for field in ('has_file', 'had_member'):
                    for child in _refs(node.get(field)): self.members[child].append((identity, None, None))
        for group in PROVENANCE_EDGES:
            relation = group[:-len('_edges')]
            for edge in lineage.get(group) or []:
                if not isinstance(edge, dict) or not all(isinstance(edge.get(key), str) for key in EDGE_KEY): continue
                if relation == 'has_file': self.members[edge['object']].append((edge['subject'], group, edge))
                else: self.out[relation][edge['subject']].append((edge['object'], group, edge))
    def targets(self, relation, identity): return self.out[relation].get(identity, ())
    def lineage(self, identity):
        """An input's onward-lineage signature: its generating activities and the entities it derives from (or belongs to)."""
        return (frozenset(target for target, _, _ in self.targets('was_generated_by', identity)),
                frozenset([target for target, _, _ in self.targets('was_derived_from', identity)]
                          + [parent for parent, _, _ in self.members.get(identity, ())]))


def lineage_closure(gene_set, lineage, *, threshold=GROUP_THRESHOLD):
    """One gene set's reachable lineage: {'provenance': {group: [...]}, 'collapsed': [{activity, representative,
    collapsed_inputs}]}.

    From the gene set's was_generated_by activities (a string or a list, or was_generated_by edges): each activity's used
    inputs (used edges keep their edge_role) and agents; an input's generating activity, recursively; an entity's
    was_derived_from targets and the Datasets it belongs to (has_file / had_member); a Dataset's organizations
    (ORG_FIELDS). An activity's inputs are grouped by their lineage signature (generating activities, derived-from and
    parent entities); a group larger than `threshold` keeps its smallest id and drops the others with their edges.
    Groups are PROVENANCE_GROUPS keys holding only reached nodes and edges (both ends reached), sorted by id or
    (subject, predicate, object); prefixes are kept whole. Relations stated by node fields stay on the nodes.
    """
    index, root = _Index(lineage), gene_set.get('id')
    reached, edges, collapsed = set(), {}, []
    pending = deque((activity, 'activity') for activity in _refs(gene_set.get('was_generated_by')))
    if isinstance(root, str):
        reached.add(root)
        for activity, group, edge in index.targets('was_generated_by', root):
            pending.append((activity, 'activity'))
            if edge: edges[group, _edge_key(edge)] = edge
    def follow(relation, identity, kind):
        for target, group, edge in index.targets(relation, identity):
            if edge: edges[group, _edge_key(edge)] = edge
            pending.append((target, kind))
    while pending:
        identity, kind = pending.popleft()
        if identity in reached: continue
        reached.add(identity)
        if index.nodes.get(identity) == 'activities': kind = 'activity'
        if kind == 'activity':
            inputs = defaultdict(list)
            for item in index.targets('used', identity): inputs[index.lineage(item[0])].append(item)
            for signature, items in sorted(inputs.items(), key=lambda pair: min(item[0] for item in pair[1])):
                if len({item[0] for item in items}) > threshold:
                    representative = min(item[0] for item in items)
                    collapsed.append({'activity': identity, 'representative': representative,
                                      'collapsed_inputs': len({item[0] for item in items})})
                    items = [item for item in items if item[0] == representative]
                for target, group, edge in items:
                    if edge: edges[group, _edge_key(edge)] = edge
                    pending.append((target, 'entity'))
            for field in AGENT_FIELDS: follow(field, identity, 'agent')
            continue
        if kind == 'agent': continue
        follow('was_generated_by', identity, 'activity')
        follow('was_derived_from', identity, 'entity')
        for parent, group, edge in index.members.get(identity, ()):
            if edge: edges[group, _edge_key(edge)] = edge
            pending.append((parent, 'entity'))
        if index.nodes.get(identity) == 'datasets':
            for field in ORG_FIELDS: follow(field, identity, 'agent')
    provenance = {}
    for group in PROVENANCE_GROUPS:
        if group == 'prefixes':
            if lineage.get('prefixes'): provenance['prefixes'] = dict(lineage['prefixes'])
        elif group in PROVENANCE_NODES:
            nodes = sorted((index.records[identity] for identity in reached if index.nodes.get(identity) == group), key=lambda node: node['id'])
            if nodes: provenance[group] = nodes
        else:
            kept = sorted((edge for (name, key), edge in edges.items() if name == group and key[0] in reached and key[2] in reached), key=_edge_key)
            if kept: provenance[group] = kept
    return {'provenance': provenance, 'collapsed': sorted(collapsed, key=lambda item: (item['activity'], item['representative']))}
