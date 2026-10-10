"""Bounded scientific-use lineage over an already authorized immutable document.

This module performs no I/O. The caller supplies retained reference observations,
projection edges and registered recovery supplements; ordinary scientific objects
are never rewritten. Projection is a recorded fitted-model relationship, not proof
of the complete construction lineage of a Mechanism.
"""
from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
import re

from .auth import Problem

MAX_NODES = 10_000
MAX_EDGES = 50_000
MAX_DEPTH = 128
MAX_STATES = 100_000
TRACING_POLICY_VERSION = 'account-provenance-v1'
CLASSIFICATIONS = ('proposition_reference', 'evidence_derivation', 'inherited_source_claim', 'factor_projection')
DIRECTIONS = frozenset(('SUPPORTS', 'DISPUTES', 'MIXED', 'NEUTRAL', 'UNKNOWN'))
KINDS = {'scientific_accounts': 'account', 'claims': 'claim', 'propositions': 'proposition',
         'evidence_items': 'evidence', 'datasets': 'dataset', 'files': 'file', 'c2m2_files': 'file',
         'activities': 'activity', 'gene_sets': 'gene_set', 'mechanisms': 'mechanism',
         'mechanistic_models': 'mechanistic_model', 'causal_steps': 'causal_step', 'organizations': 'organization',
         'persons': 'person', 'software': 'software'}
FIELDS = {'was_derived_from': 'prov:wasDerivedFrom', 'was_generated_by': 'prov:wasGeneratedBy',
          'used': 'prov:used', 'has_file': 'dapper:hasFile', 'had_member': 'prov:hadMember',
          'was_attributed_to': 'prov:wasAttributedTo', 'was_associated_with': 'prov:wasAssociatedWith',
          'has_creator': 'schema:creator', 'has_contributor': 'dcterms:contributor',
          'publisher': 'dcterms:publisher', 'funded_by': 'schema:funding',
          'subject_entity': 'dapper:subjectEntity', 'object_entity': 'dapper:objectEntity',
          'mechanistic_model': 'dapper:mechanisticModel', 'has_causal_step': 'dapper:hasCausalStep',
          'source_claims': 'dapper:sourceClaims', 'proposition': 'dapper:proposition',
          'has_evidence': 'dapper:hasEvidence', 'component_claims': 'dapper:componentClaims',
          'conclusion_claims': 'dapper:conclusionClaims', 'step_subject': 'dapper:stepSubject',
          'step_object': 'dapper:stepObject', 'via_mechanism': 'dapper:viaMechanism'}
ORG_ROLES = {'was_attributed_to': 'attribution', 'has_creator': 'creator',
             'has_contributor': 'contributor', 'publisher': 'publisher', 'funded_by': 'funder',
             'was_associated_with': 'generation_agent'}
ENTITY_KINDS = frozenset(('gene_set', 'mechanism', 'mechanistic_model', 'causal_step', 'file', 'dataset', 'activity'))
PREDICATES = {value: key for key, value in FIELDS.items()}
PREDICATES.update({f'dapper:{key}': key for key in FIELDS})
PREDICATES.update({'dcterms:creator': 'has_creator', 'schema:contributor': 'has_contributor',
                   'schema:publisher': 'publisher', 'FRAPO:isSupportedBy': 'funded_by',
                   'dapper:fundedBy': 'funded_by', 'dcat:distribution': 'has_file',
                   'reveal:factorProjection': 'factor_projection'})
NAMESPACES = {'https://broadinstitute.github.io/dapper/ns#': 'dapper:',
              'http://www.w3.org/ns/prov#': 'prov:', 'http://purl.org/dc/terms/': 'dcterms:',
              'http://www.w3.org/ns/dcat#': 'dcat:', 'https://schema.org/': 'schema:',
              'http://schema.org/': 'schema:', 'http://purl.org/cerif/frapo/': 'FRAPO:'}


def _id(prefix, value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return f'{prefix}:{hashlib.sha256(raw).hexdigest()}'


def _refs(value):
    return sorted({item for item in (value if isinstance(value, list) else [value])
                   if isinstance(item, str) and item})


def _relation(predicate, group=''):
    expected = 'factor_projection' if group == 'factor_projection_edges' else group.removesuffix('_edges')
    if predicate is None:
        return expected if expected in FIELDS or expected == 'factor_projection' else None
    if not isinstance(predicate, str):
        return None
    for namespace, prefix in NAMESPACES.items():
        if predicate.startswith(namespace):
            predicate = prefix + predicate[len(namespace):]
            break
    relation = PREDICATES.get(predicate)
    if expected in FIELDS or expected == 'factor_projection':
        return relation if relation == expected else None
    return relation


def _limit():
    raise Problem(422, 'PROVENANCE_LIMIT_EXCEEDED',
                  'Scientific provenance exceeds the configured processing bound; no rankings were returned.')


class _Graph:
    def __init__(self, document):
        self.records, self.kinds, self.conflicts = {}, {}, set()
        self.adj = defaultdict(list)
        self.parents = defaultdict(list)
        self.edges, self.nodes = {}, {}
        self.issues = {}
        self.issue_origins = defaultdict(set)
        self.reference_origins = defaultdict(set)
        self.projections = set()
        self.factor_bindings = {item.get('mechanism_id') for item in
            document.get('_provenance_coverage', {}).get('retained_projection_coverage', [])
            if isinstance(item, dict) and (item.get('known_factor_binding') is True or
                item.get('status') in ('retained_rows_loaded', 'retained_legacy_links_loaded', 'retained'))}
        self.processing_states = 0
        self._input_edges = {}
        for group, rows in document.items():
            if not isinstance(rows, list) or group.endswith('_edges'):
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                    continue
                identity, kind = row['id'], KINDS.get(group, 'record')
                if identity in self.records and (row != self.records[identity] or kind != self.kinds[identity]):
                    self.conflicts.add(identity)
                else:
                    self.records[identity], self.kinds[identity] = row, kind
                if len(self.records) > MAX_NODES:
                    _limit()
        self.conflicts.update(document.get('_provenance_coverage', {}).get('blocked_reference_ids', []))
        for identity, node in self.records.items():
            for field, predicate in FIELDS.items():
                for target in _refs(node.get(field)):
                    self._edge(identity, target, predicate, field, {'source_field': field})
        for group, rows in document.items():
            if not isinstance(rows, list) or not group.endswith('_edges'):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                subject, target = row.get('subject'), row.get('object')
                predicate = row.get('predicate', FIELDS.get(group.removesuffix('_edges')))
                relation = _relation(predicate, group)
                if not isinstance(subject, str) or not isinstance(target, str) or not relation:
                    continue
                metadata = {key: deepcopy(value) for key, value in row.items()
                            if key not in ('subject', 'object', 'predicate', 'id')}
                self._edge(subject, target, predicate or 'reveal:factorProjection', relation, metadata)
        for edge in self._input_edges.values():
            self.adj[edge['subject']].append(edge)
            if edge['relation'] in ('has_file', 'had_member') and self.kinds.get(edge['subject']) == 'dataset':
                self.parents[edge['object']].append(edge)
        for values in (*self.adj.values(), *self.parents.values()):
            values.sort(key=lambda edge: (edge['relation'], edge['object'], edge['id']))

    def _edge(self, subject, target, predicate, relation, metadata):
        # Native fields and explicit triples represent the same relationship.
        # Retain observations as metadata without multiplying graph edges.
        role = metadata.get('input_role', metadata.get('edge_role', metadata.get('role', 'unknown')))
        role = role if isinstance(role, str) and role else 'unknown'
        key = (subject, relation, target, role if relation == 'used' else '')
        if key not in self._input_edges:
            value = {'subject': subject, 'predicate': predicate, 'object': target, 'relation': relation}
            if relation == 'used':
                value['input_role'] = role
            value['id'] = _id('edge', key)
            self._input_edges[key] = value
            if len(self._input_edges) > MAX_EDGES:
                _limit()
        value = self._input_edges[key]
        if metadata:
            observations = value.setdefault('observations', [])
            if metadata not in observations:
                observations.append(metadata)
                observations.sort(key=lambda item: json.dumps(item, sort_keys=True))
            if 'provenance_source' in metadata:
                sources = value.setdefault('provenance_source', [])
                if metadata['provenance_source'] not in sources:
                    sources.append(deepcopy(metadata['provenance_source']))
                    sources.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return value

    def add(self, identity):
        if identity not in self.nodes:
            node = self.records.get(identity)
            status = 'conflicting' if identity in self.conflicts else 'resolved' if node else 'unresolved'
            compact = {'id': identity, 'kind': self.kinds.get(identity, 'unresolved'), 'status': status}
            if node and status == 'resolved':
                for key in ('name', 'sha256', 'filename', 'ror'):
                    if key in node:
                        compact[key] = deepcopy(node[key])
            self.nodes[identity] = compact
            if len(self.nodes) > MAX_NODES:
                _limit()
        return identity in self.records and identity not in self.conflicts

    def use(self, edge, reverse=False):
        self.add(edge['subject']); self.add(edge['object'])
        self.edges[edge['id']] = edge
        if len(self.edges) > MAX_EDGES:
            _limit()
        return {'edge_id': edge['id'], 'direction': 'reverse' if reverse else 'forward'}

    def issue(self, code, origin, identity=None):
        # Missing target IDs may refer to withheld private objects: never expose
        # them in diagnostics. The authorized origin is sufficient to locate it.
        key = (code, origin)
        self.issues[key] = {'code': code, 'origin_id': origin}
        self.issue_origins[origin].add(code)

    def outgoing(self, identity, relations):
        return [edge for edge in self.adj.get(identity, ()) if edge['relation'] in relations]

    def resolve(self, identity, origin):
        self.reference_origins[identity].add(origin)
        if self.add(identity):
            return True
        self.issue('CONFLICTING_OBSERVATION' if identity in self.conflicts else 'MISSING_LINEAGE', origin)
        return False

    def evidence(self, claim_id, origin):
        props = self.outgoing(claim_id, {'proposition'})
        if len(props) != 1 or not self.resolve(props[0]['object'], origin) or self.kinds.get(props[0]['object']) != 'proposition':
            self.issue('INVALID_CLAIM_PROPOSITION', origin)
            return []
        proposition = props[0]['object']
        result = []
        for edge in self.outgoing(claim_id, {'has_evidence'}):
            identity = edge['object']
            if not self.resolve(identity, origin):
                continue
            item = self.records[identity]
            if (self.kinds[identity] != 'evidence' or item.get('target_proposition') != proposition
                    or item.get('direction') not in DIRECTIONS):
                self.issue('INVALID_EVIDENCE_TARGET', origin)
                continue
            result.append(edge)
        return result

    def organizations(self, dataset_id, origin):
        """Organization roles belong only to this dataset or its generation."""
        result = []
        sources = [(dataset_id, [])]
        for edge in self.outgoing(dataset_id, {'was_generated_by'}):
            if self.resolve(edge['object'], origin):
                sources.append((edge['object'], [self.use(edge)]))
        for source, path in sources:
            for edge in self.outgoing(source, set(ORG_ROLES)):
                target = edge['object']
                if not self.resolve(target, origin):
                    continue
                if self.kinds.get(target) != 'organization':
                    continue
                role = ORG_ROLES[edge['relation']]
                if source != dataset_id:
                    role = 'generation_' + role if not role.startswith('generation_') else role
                result.append({'organization_id': target, 'role': role,
                               'path_steps': path + [self.use(edge)]})
        unique = {_id('organization-path', row): row for row in result}
        return [unique[key] for key in sorted(unique)]

    def walk(self, seed):
        first = seed['edge']
        origin = seed['origin_id']
        initial = frozenset(seed['classifications'])
        queue = deque([(first['object'], [self.use(first)], initial, frozenset(), False, 1, frozenset({origin}))])
        seen, traces = set(), []
        def push(state):
            if self.processing_states + len(queue) >= MAX_STATES:
                _limit()
            queue.append(state)
        while queue:
            identity, path, classes, roles, membership, depth, ancestors = queue.popleft()
            self.processing_states += 1
            if self.processing_states > MAX_STATES:
                _limit()
            if identity in ancestors:
                self.issue('CYCLIC_LINEAGE', origin)
                continue
            state = (identity, classes, roles, membership)
            if state in seen:
                continue
            if depth > MAX_DEPTH:
                _limit()
            seen.add(state)
            if not self.resolve(identity, origin):
                continue
            kind, node = self.kinds[identity], self.records[identity]
            next_ancestors = ancestors | {identity}
            if kind == 'dataset':
                orgs = self.organizations(identity, origin)
                row = {key: deepcopy(seed[key]) for key in ('claim_id', 'proposition_id', 'evidence_item_id', 'origin_id', 'source_field')}
                row.update(source_id=first['object'], dataset_id=identity,
                           route_classifications=sorted(classes), input_roles=sorted(roles or {'unknown'}),
                           organization_ids=sorted({item['organization_id'] for item in orgs}),
                           organization_attributions=orgs, path_steps=path,
                           path_edge_ids=[item['edge_id'] for item in path])
                if seed.get('direction') is not None:
                    row['direction'] = seed['direction']
                row['id'] = _id('trace', {key: value for key, value in row.items()
                                        if key not in ('path_steps', 'path_edge_ids', 'organization_attributions')})
                traces.append(row)
                if membership:
                    continue
            links = self.outgoing(identity, {'was_derived_from', 'was_generated_by'}) if kind in ENTITY_KINDS else []
            if kind == 'file':
                for edge in self.parents.get(identity, ()):
                    push((edge['subject'], path + [self.use(edge, reverse=True)], classes, roles, True, depth + 1, next_ancestors))
            if kind == 'activity':
                links += self.outgoing(identity, {'used'})
            elif kind == 'mechanism':
                projections = self.outgoing(identity, {'factor_projection'})
                if projections or identity in self.factor_bindings:
                    self.projections.add(identity)
                    if not projections:
                        self.issue('MISSING_FACTOR_PROJECTIONS', origin)
                elif not links:
                    # A generic biological Mechanism is not necessarily a fitted
                    # factor. Its native lineage is sufficient when recorded.
                    self.issue('MISSING_LINEAGE', origin)
                links += projections
            elif kind in ('proposition', 'mechanistic_model', 'causal_step'):
                links += self.outgoing(identity, {'subject_entity', 'object_entity', 'mechanistic_model', 'has_causal_step',
                                                   'step_subject', 'step_object', 'via_mechanism'})
            elif kind == 'evidence':
                links += self.outgoing(identity, {'was_derived_from', 'source_claims', 'mechanistic_model'})
            elif kind == 'claim':
                links += self.outgoing(identity, {'proposition'}) + self.evidence(identity, origin)
            if kind in ('gene_set', 'file', 'activity', 'mechanistic_model') and not links and not (kind == 'file' and self.parents.get(identity)):
                self.issue('MISSING_LINEAGE', origin)
            for edge in links:
                relation, target = edge['relation'], edge['object']
                # Biological CURIEs are terminal entities, not missing datasets.
                if relation in ('subject_entity', 'object_entity', 'has_causal_step', 'step_subject', 'step_object') and not self.relevant(target):
                    continue
                if relation == 'source_claims' and self.kinds.get(target) not in ('claim', None):
                    self.issue('INVALID_SOURCE_CLAIM', origin)
                    continue
                next_classes = classes
                if relation == 'source_claims': next_classes |= {'inherited_source_claim'}
                if relation == 'factor_projection': next_classes |= {'factor_projection'}
                if kind == 'proposition': next_classes |= {'proposition_reference'}
                if relation == 'has_evidence': next_classes |= {'evidence_derivation'}
                next_roles = roles | {edge['input_role']} if relation == 'used' else roles
                push((target, path + [self.use(edge)], next_classes, next_roles, False, depth + 1, next_ancestors))
        return traces

    def relevant(self, identity):
        return (self.kinds.get(identity) in ENTITY_KINDS or identity in self.conflicts
                or bool(re.match(r'dapper:(GeneSet|Mechanism|MechanisticModel|Dataset|File|Activity)\.', identity)))


def _metrics(traces):
    datasets = defaultdict(list)
    organizations = defaultdict(list)
    for trace in traces:
        if trace['claim_id'] is None:
            continue
        datasets[trace['dataset_id']].append(trace)
        for attribution in trace['organization_attributions']:
            organizations[attribution['organization_id']].append((trace, attribution['role']))
    dataset_rows = []
    for identity, rows in datasets.items():
        by_class = {key: sorted({row['claim_id'] for row in rows if key in row['route_classifications']}) for key in CLASSIFICATIONS}
        claims = sorted({row['claim_id'] for row in rows})
        dataset_rows.append({'dataset_id': identity, 'claim_count': len(claims), 'claim_ids': claims,
                             'trace_count': len(rows), 'classification_claim_counts': {key: len(value) for key, value in by_class.items()},
                             'classification_claim_ids': by_class,
                             'input_roles': sorted({role for row in rows for role in row['input_roles']})})
    organization_rows = []
    for identity, rows in organizations.items():
        def counts(items):
            claims = sorted({row['claim_id'] for row in items})
            datasets = sorted({row['dataset_id'] for row in items})
            pairs = {(row['claim_id'], row['dataset_id']) for row in items}
            return {'claim_count': len(claims), 'claim_ids': claims, 'dataset_count': len(datasets),
                    'dataset_ids': datasets, 'claim_dataset_count': len(pairs)}
        organization_rows.append({'organization_id': identity, **counts([row for row, role in rows]),
                                  'roles': {role: counts([row for row, item_role in rows if item_role == role])
                                            for role in sorted({role for _, role in rows})}})
    return (sorted(dataset_rows, key=lambda row: (-row['claim_count'], row['dataset_id'])),
            sorted(organization_rows, key=lambda row: (-row['claim_count'], -row['dataset_count'], row['organization_id'])))


def collect(document, account_id):
    """Return complete metrics and unpaginated trace wrappers for one account.

    ``_provenance_coverage`` may carry the caller's sanitized ``issues``,
    ``retained_projection_coverage`` and ``recovery_status`` observations.
    The caller must authorize and enrich the full document before this call,
    and handle immutable snapshot cursors and response-size bounds afterward.
    """
    graph = _Graph(document)
    if not graph.resolve(account_id, account_id) or graph.kinds.get(account_id) != 'account':
        raise Problem(404, 'NOT_FOUND', 'The requested scientific resource is unavailable.')
    account = graph.records[account_id]
    claim_ids = sorted({edge['object'] for edge in graph.outgoing(account_id, {'component_claims', 'conclusion_claims'})})
    seeds, included_claims, propositions, evidence_items = [], set(), set(), set()
    claim_annotations = defaultdict(set)
    def seed_edges(origin, claim, prop=None, evidence=None, classes=()):
        relations = {'was_derived_from', 'source_claims', 'mechanistic_model'} if evidence else {'subject_entity', 'object_entity', 'mechanistic_model'}
        for edge in graph.outgoing(origin, relations):
            if edge['relation'] in ('subject_entity', 'object_entity') and not graph.relevant(edge['object']):
                continue
            if edge['relation'] == 'source_claims' and graph.kinds.get(edge['object']) not in ('claim', None):
                graph.issue('INVALID_SOURCE_CLAIM', origin)
                continue
            categories = set(classes)
            if edge['relation'] == 'source_claims': categories.add('inherited_source_claim')
            seeds.append({'claim_id': claim, 'proposition_id': prop, 'evidence_item_id': evidence,
                          'origin_id': origin, 'source_field': edge['relation'], 'edge': edge,
                          'classifications': categories,
                          'direction': graph.records[evidence].get('direction') if evidence else None})
    for edge in graph.outgoing(account_id, {'component_claims', 'conclusion_claims'}):
        graph.use(edge)
    for claim_id in claim_ids:
        if not graph.resolve(claim_id, account_id) or graph.kinds.get(claim_id) != 'claim':
            graph.issue('MISSING_CLAIM', account_id)
            continue
        included_claims.add(claim_id)
        props = graph.outgoing(claim_id, {'proposition'})
        if len(props) != 1 or not graph.resolve(props[0]['object'], claim_id) or graph.kinds.get(props[0]['object']) != 'proposition':
            graph.issue('INVALID_CLAIM_PROPOSITION', claim_id)
            continue
        prop = props[0]['object']; propositions.add(prop); graph.use(props[0])
        claim_annotations[claim_id].add(prop)
        seed_edges(prop, claim_id, prop, classes={'proposition_reference'})
        for attached in graph.outgoing(claim_id, {'has_evidence'}):
            identity = attached['object']
            if identity in graph.records and identity not in graph.conflicts and graph.kinds.get(identity) == 'evidence':
                evidence_items.add(identity)
                claim_annotations[claim_id].add(identity)
                if graph.records[identity].get('target_proposition') != prop or graph.records[identity].get('direction') not in DIRECTIONS:
                    graph.issue('INVALID_EVIDENCE_TARGET', identity)
        for edge in graph.evidence(claim_id, claim_id):
            evidence = edge['object']; evidence_items.add(evidence); graph.use(edge)
            claim_annotations[claim_id].add(evidence)
            seed_edges(evidence, claim_id, prop, evidence, {'evidence_derivation'})
    # Context references can be displayed but never contribute claim incidence.
    for edge in graph.outgoing(account_id, {'mechanistic_model', 'was_derived_from'}):
        seeds.append({'claim_id': None, 'proposition_id': None, 'evidence_item_id': None,
                      'origin_id': account_id, 'source_field': edge['relation'], 'edge': edge,
                      'classifications': set(), 'direction': None})
    traces_by_id = {}
    for seed in seeds:
        for row in graph.walk(seed):
            traces_by_id.setdefault(row['id'], row)
    traces = sorted(traces_by_id.values(), key=lambda row: (row['claim_id'] or '', row['origin_id'], row['source_field'], row['dataset_id'], row['id']))
    additional = document.get('_provenance_coverage', {})
    issues = sorted(graph.issues.values(), key=lambda row: (row['code'], row['origin_id']))
    issues += deepcopy(additional.get('issues', []))
    # Enrichment failures may be attached to a reached reference rather than
    # an account record. Propagate them to every originating scientific use,
    # including failed branches with no emitted dataset trace.
    affected_origins = set()
    for item in additional.get('issues', []):
        if not isinstance(item, dict):
            continue
        reference = item.get('object_id', item.get('origin_id'))
        if reference is None:
            affected_origins.update(seed['origin_id'] for seed in seeds)
        else:
            affected_origins.update(graph.reference_origins.get(reference, ()))
    for origin in affected_origins:
        graph.issue_origins[origin].add('REFERENCE_COVERAGE_INCOMPLETE')
    incomplete = bool(issues)
    dataset_reuse, organization_reuse = _metrics(traces)
    by_claim, by_proposition, by_evidence = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in traces:
        by_claim[row['claim_id']].append(row)
        by_proposition[row['proposition_id']].append(row)
        by_evidence[row['evidence_item_id']].append(row)
    issue_origin_ids = set(graph.issue_origins)
    def wrapper(identity, matches, origins=()):
        origins = set(origins) | {identity}
        bad = bool(origins & issue_origin_ids)
        return {'record': deepcopy(graph.records[identity]), 'provenance': {
            'trace_count': len(matches), 'trace_ids': [row['id'] for row in matches],
            'dataset_ids': sorted({row['dataset_id'] for row in matches}),
            'organization_ids': sorted({org for row in matches for org in row['organization_ids']}),
            'resolution': 'incomplete' if bad else 'complete' if matches else 'not_applicable'}}
    account_wrapper = wrapper(account_id, traces, set(graph.issue_origins))
    if incomplete: account_wrapper['provenance']['resolution'] = 'incomplete'
    projection_coverage = deepcopy(additional.get('retained_projection_coverage', []))
    if not projection_coverage:
        projection_coverage = [{'mechanism_id': identity, 'retained_projection_count': len(graph.outgoing(identity, {'factor_projection'})),
                               'status': 'retained' if graph.outgoing(identity, {'factor_projection'}) else 'unavailable',
                               'construction_lineage_complete': False} for identity in sorted(graph.projections)]
    return {'account': account_wrapper,
            'claims': [wrapper(identity, by_claim[identity], claim_annotations[identity]) for identity in sorted(included_claims)],
            'propositions': [wrapper(identity, by_proposition[identity]) for identity in sorted(propositions)],
            'evidence_items': [wrapper(identity, by_evidence[identity]) for identity in sorted(evidence_items)],
            'traces': traces, 'nodes': [graph.nodes[key] for key in sorted(graph.nodes)],
            'edges': [deepcopy(graph.edges[key]) for key in sorted(graph.edges)],
            'dataset_reuse': dataset_reuse, 'organization_reuse': organization_reuse,
            'coverage': {'status': 'incomplete' if incomplete else 'complete', 'counts_are_lower_bounds': incomplete,
                         'claim_count': len(claim_ids), 'resolved_claim_count': len(included_claims), 'trace_count': len(traces),
                         'lineage_resolution': {'status': 'incomplete' if incomplete else 'complete', 'issue_count': len(issues)},
                         'retained_projection_coverage': projection_coverage,
                         'recovery_status': deepcopy(additional.get('recovery_status', [])), 'issues': issues}}


def eligible_reference_ids(document, account_id=None):
    """Return reached reference entities/files, excluding unrelated capture rows.

    Enrichment may call this again after adding exact captured/projection links.
    Nothing is loaded here and the same bounds and scientific-use rules apply.
    """
    accounts = [account_id] if account_id else [row['id'] for row in document.get('scientific_accounts', [])
                                               if isinstance(row, dict) and isinstance(row.get('id'), str)]
    identities = set()
    for identity in accounts:
        result = collect(document, identity)
        for node in result['nodes']:
            if node['status'] != 'conflicting' and (node['kind'] in ('gene_set', 'mechanism', 'file')
                    or re.match(r'dapper:(GeneSet|Mechanism|File)\.', node['id'])):
                identities.add(node['id'])
    return identities
