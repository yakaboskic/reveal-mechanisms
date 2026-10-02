"""Dataset evidence uses from one complete, frozen public DAPPER document.

This is deliberately separate from the display graph: rendering may show all
dataset members and activity inputs, neither of which licenses evidence credit.
Publication eligibility and aggregation across accounts belong to the caller.
"""
from collections import defaultdict, deque


DIRECTIONS = frozenset(('SUPPORTS', 'DISPUTES', 'MIXED', 'NEUTRAL', 'UNKNOWN'))
_MAX_VISITS = 10000
_MAX_DEPTH = 128
_KINDS = {'scientific_accounts': 'account', 'claims': 'claim', 'propositions': 'proposition',
          'evidence_items': 'evidence', 'datasets': 'dataset', 'files': 'file',
          'c2m2_files': 'file', 'activities': 'activity'}
_EDGE_DEFAULTS = {'has_evidence_edges': 'dapper:hasEvidence',
                  'was_derived_from_edges': 'prov:wasDerivedFrom',
                  'has_file_edges': 'dapper:hasFile'}
_NAMESPACES = {'https://broadinstitute.github.io/dapper/ns#': 'dapper:',
               'http://www.w3.org/ns/prov#': 'prov:'}


def _refs(value):
    return {item for item in (value if isinstance(value, list) else [value])
            if isinstance(item, str) and item}


def _predicate(value):
    if not isinstance(value, str):
        return None
    for namespace, prefix in _NAMESPACES.items():
        if value.startswith(namespace):
            return prefix + value[len(namespace):]
    return value


def collect(document, account_id):
    """Return per-(claim, evidence, dataset) uses and rejected-path diagnostics.

    ``uses`` rows contain dataset_id, dataset_label, claim_id, evidence_id,
    direction, and sorted file_ids. Only files actually reached by the evidence
    path and explicitly recorded as members of that dataset appear in file_ids.
    Directions always belong to the component claim's own evidence item; they
    are never multiplied through source claims. ``excluded_paths`` counts
    distinct unresolved, conflicting, invalid or bounded traversal branches,
    not missing evidence or a scientific quality score.

    Pass the publication snapshot's full document, never an API envelope or
    bubble projection. No record is fetched or inferred outside that document.
    """
    if not isinstance(document, dict) or 'document' in document or 'coverage' in document:
        return {'uses': [], 'excluded_paths': 1}
    records, kinds, ambiguous = {}, {}, set()
    edges = defaultdict(lambda: defaultdict(set))
    for group, rows in document.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if group.endswith('_edges'):
                subject, target = row.get('subject'), row.get('object')
                predicate = _predicate(row.get('predicate', _EDGE_DEFAULTS.get(group)))
                if isinstance(subject, str) and isinstance(target, str) and predicate:
                    edges[subject][predicate].add(target)
                continue
            identity = row.get('id')
            if not isinstance(identity, str) or not identity:
                continue
            kind = _KINDS.get(group, 'record')
            if identity in records and (records[identity] != row or kinds[identity] != kind):
                ambiguous.add(identity)
            else:
                records[identity], kinds[identity] = row, kind
    for identity in ambiguous:
        records.pop(identity, None)
        kinds.pop(identity, None)
    account = records.get(account_id)
    if not account or kinds[account_id] != 'account':
        return {'uses': [], 'excluded_paths': 1}

    # Reverse membership associates a reached File with its recorded datasets.
    # It must not expand other members or inherit the parent dataset's lineage.
    parents = defaultdict(set)
    for identity, node in records.items():
        if kinds[identity] == 'dataset':
            members = _refs(node.get('has_file')) | edges[identity]['dapper:hasFile'] | edges[identity]['prov:hadMember']
            for member in members:
                if kinds.get(member) == 'file':
                    parents[member].add(identity)

    excluded, uses = set(), []

    def evidence_for(claim_id, root_claim, root_evidence=''):
        claim = records.get(claim_id)
        if not claim or kinds[claim_id] != 'claim':
            excluded.add((root_claim, root_evidence, claim_id, 'unresolved-claim'))
            return []
        proposition = claim.get('proposition')
        valid_proposition = isinstance(proposition, str) and kinds.get(proposition) == 'proposition'
        result = []
        for identity in sorted(_refs(claim.get('has_evidence')) | edges[claim_id]['dapper:hasEvidence']):
            item = records.get(identity)
            if (not item or kinds[identity] != 'evidence' or not valid_proposition
                    or item.get('target_proposition') != proposition
                    or not isinstance(item.get('direction'), str) or item['direction'] not in DIRECTIONS):
                excluded.add((root_claim, root_evidence, identity, 'invalid-evidence-use'))
                continue
            result.append(identity)
        return result

    for claim_id in sorted(_refs(account.get('component_claims'))):
        for evidence_id in evidence_for(claim_id, claim_id):
            direction = records[evidence_id]['direction']
            datasets, files, visited = set(), set(), set()
            pending = deque([(evidence_id, 0, frozenset())])
            while pending:
                identity, depth, ancestors = pending.popleft()
                if identity in ancestors:
                    excluded.add((claim_id, evidence_id, identity, 'cycle'))
                    continue
                if identity in visited:
                    continue
                if depth > _MAX_DEPTH or len(visited) >= _MAX_VISITS:
                    excluded.add((claim_id, evidence_id, identity, 'truncated'))
                    continue
                visited.add(identity)
                node = records.get(identity)
                if node is None:
                    excluded.add((claim_id, evidence_id, identity, 'unresolved-source'))
                    continue
                kind = kinds[identity]
                if kind == 'activity':
                    # Activity.used is computational lineage, not an evidence
                    # interpretation. Even explicit arrival here stops traversal.
                    continue
                if kind == 'claim' and (not isinstance(node.get('proposition'), str)
                                        or kinds.get(node['proposition']) != 'proposition'):
                    excluded.add((claim_id, evidence_id, identity, 'unresolved-source-proposition'))
                    continue
                if kind == 'dataset':
                    datasets.add(identity)
                elif kind == 'file':
                    files.add(identity)
                    datasets.update(parents[identity])
                links = _refs(node.get('was_derived_from')) | edges[identity]['prov:wasDerivedFrom']
                if kind == 'evidence':
                    for source in _refs(node.get('source_claims')):
                        if kinds.get(source) == 'claim':
                            links.add(source)
                        else:
                            excluded.add((claim_id, evidence_id, source, 'unresolved-source-claim'))
                elif kind == 'claim':
                    links.update(evidence_for(identity, claim_id, evidence_id))
                next_ancestors = ancestors | {identity}
                pending.extend((link, depth + 1, next_ancestors) for link in sorted(links))
            for dataset_id in sorted(datasets):
                label = records[dataset_id].get('name')
                uses.append({'dataset_id': dataset_id,
                             'dataset_label': label if isinstance(label, str) and label.strip() else dataset_id,
                             'claim_id': claim_id, 'evidence_id': evidence_id, 'direction': direction,
                             'file_ids': sorted(file for file in files if dataset_id in parents[file])})
    return {'uses': uses, 'excluded_paths': len(excluded)}
