"""Offline, deterministic assembly of frozen CFDE/DisMech evidence.

Inputs are a build manifest, exact artifact bytes, and a pinned DAPPER runtime.
No database, network, embedding, clock, random value, or LLM is used here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile

import yaml

BUILD_VERSION = 'reveal.evidence-builder/1'
INPUT_VERSION = 'reveal.evidence-build/1'
PACKAGE_VERSION = 'reveal.evidence-package/0.2-draft'
TARGETS = ('gene', 'gene_set', 'trait', 'factor')


class EvidenceBuildError(ValueError):
    """Frozen inputs are inconsistent, incomplete, or outside the declared policy."""


def require(condition, message):
    if not condition:
        raise EvidenceBuildError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    """Version-1 serialization: UTF-8, sorted string keys, compact JSON, final LF.

    Array order and numeric types are preserved. This is not advertised as RFC 8785.
    """
    try:
        return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                           separators=(',', ':')) + '\n').encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise EvidenceBuildError(f'Not finite JSON data: {exc}') from exc


def _pairs(items):
    result = {}
    for key, value in items:
        require(isinstance(key, str), 'Mapping keys must be strings')
        require(key not in result, f'Duplicate mapping key: {key}')
        result[key] = value
    return result


class StrictLoader(yaml.SafeLoader):
    pass


def _yaml_mapping(loader, node):
    return _pairs((loader.construct_object(k), loader.construct_object(v)) for k, v in node.value)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def decode(data, format='json'):
    try:
        if format == 'json':
            result = json.loads(data, object_pairs_hook=_pairs,
                                parse_constant=lambda x: (_ for _ in ()).throw(EvidenceBuildError(f'Nonfinite number: {x}')))
        elif format == 'yaml':
            result = yaml.load(data.decode('utf-8'), Loader=StrictLoader)
        else:
            raise EvidenceBuildError(f'Unsupported structured format: {format}')
        canonical_json(result)  # Reject YAML timestamps, nonfinite numbers, cycles, and non-JSON values.
        return result
    except (UnicodeError, yaml.YAMLError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceBuildError(f'Invalid {format}: {exc}') from exc


def pointer(value, path):
    require(isinstance(path, str) and (not path or path.startswith('/')), f'Invalid JSON Pointer: {path}')
    try:
        for part in path.split('/')[1:] if path else []:
            require(not re.search(r'~(?![01])', part), f'Invalid JSON Pointer escape: {path}')
            part = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                require(bool(re.fullmatch(r'0|[1-9][0-9]*', part)), f'Invalid array index: {path}')
                value = value[int(part)]
            else:
                value = value[part]
        return value
    except (KeyError, IndexError, TypeError) as exc:
        raise EvidenceBuildError(f'Unresolved source pointer: {path}') from exc


def ref(artifact, path=''):
    return {'artifact_id': artifact, 'pointer': path}


def unique(items, label):
    require(isinstance(items, list) and all(isinstance(x, str) for x in items), f'{label} must be a string list')
    require(len(items) == len(set(items)), f'Duplicate {label}')
    return sorted(items)


def finite(value, label):
    require(type(value) in (int, float) and math.isfinite(value), f'{label} must be finite numeric data')
    return value


class DapperRuntime:
    """Use one verified, locally installed schema/code snapshot per worker process."""
    _loaded_schema = None

    def __init__(self, snapshot_directory):
        directory = Path(snapshot_directory).resolve()
        manifest_bytes = (directory / 'snapshot.json').read_bytes()
        self.manifest = decode(manifest_bytes)
        self.manifest_sha256 = sha256(manifest_bytes)
        self.schema_path = directory / 'snapshot/schema'
        for relative, expected in self.manifest['files'].items():
            if relative.startswith('schema/'):
                path = (directory / 'snapshot' / relative).resolve()
                require(path.is_relative_to(directory / 'snapshot'), 'DAPPER snapshot path escapes its root')
                require(sha256(path.read_bytes()) == expected, f'DAPPER snapshot checksum mismatch: {relative}')
        require(self._loaded_schema in (None, self.schema_path), 'Use a separate process for a different DAPPER snapshot')
        for name, relative in [('dapper_identity', 'identity/dapper_identity.py'), ('document_prefixes', 'document_prefixes.py'),
                               ('lint_provenance', 'lint/lint_provenance.py')]:
            cached = sys.modules.get(name)
            require(cached is None or Path(cached.__file__).resolve() == self.schema_path / relative,
                    'A different DAPPER runtime is already imported; use a separate process')
        type(self)._loaded_schema = self.schema_path
        sys.path[:0] = [str(self.schema_path), str(self.schema_path / 'identity'), str(self.schema_path / 'lint')]
        from dapper_identity import compute_id, load_schema, DOC_GROUPS
        from document_prefixes import PrefixResolver, transform_identifiers
        from lint_provenance import build_validator
        self.schema = load_schema(self.schema_path / 'dapper.yaml')
        self.compute_id = compute_id
        self.groups = DOC_GROUPS
        self.resolver_class = PrefixResolver
        self.transform = transform_identifiers
        self.validator = build_validator(self.schema_path / 'dapper.yaml')

    def resolver(self, prefixes):
        resolver = self.resolver_class(self.schema, {'prefixes': prefixes})
        require(not resolver.errors, f'Invalid prefix map: {resolver.errors}')
        return resolver

    def file(self, name, data, mime):
        node = {'filename': name, 'mime_type': mime, 'sha256': sha256(data), 'size_in_bytes': len(data)}
        node['id'] = self.compute_id(node, 'File', self.schema)
        return node

    def validate(self, document):
        require(set(document) <= set(self.groups) | {'prefixes'}, 'Unknown DAPPER input collection')
        index = {}
        for group, nodes in document.items():
            if group == 'prefixes':
                continue
            require(isinstance(nodes, list), f'{group} must be a list')
            cls = self.groups[group]
            for node in nodes:
                require(node.get('id') not in index, f'Duplicate DAPPER ID: {node.get("id")}')
                result = self.validator.validate(node, cls)
                require(not result.results, f'Invalid {cls}: {[r.message for r in result.results]}')
                if node['id'].startswith('dapper:'):
                    require(self.compute_id(node, cls, self.schema) == node['id'], f'DAPPER identity mismatch: {node["id"]}')
                index[node['id']] = node
        _, errors = self.transform(document, self.schema, self.groups)
        require(not errors, f'Invalid DAPPER references: {errors}')
        def references(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key not in ('id', 'prefixes'): references(child)
            elif isinstance(value, list):
                for child in value: references(child)
            elif isinstance(value, str) and re.fullmatch(r'dapper:[A-Za-z]+\.[A-Za-z0-9_-]+', value):
                require(value in index, f'Missing DAPPER dependency: {value}')
        references(document)
        return index


@dataclass(frozen=True)
class BuiltPackage:
    package: dict
    files: dict[str, bytes]
    manifest: dict

    def write(self, output):
        """Publish only a completely validated bundle; refuse conflicting overwrites."""
        output = Path(output)
        if output.exists():
            existing = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()}
            require(existing == self.files, f'Output already exists with different content: {output}')
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='.evidence-', dir=output.parent))
        try:
            for name, data in self.files.items():
                path = temporary / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            temporary.rename(output)
        finally:
            if temporary.exists(): shutil.rmtree(temporary)


def load_build_input(path, source_root):
    """Paths are transport only; artifact checksums define source identity."""
    path, root = Path(path), Path(source_root).resolve()
    spec = decode(path.read_bytes(), 'yaml' if path.suffix in ('.yaml', '.yml') else 'json')
    blobs = {}
    for key, item in spec['artifacts'].items():
        resolved = (root / item['path']).resolve()
        require(resolved.is_relative_to(root), f'Artifact outside source root: {key}')
        blobs[key] = resolved.read_bytes()
    return spec, blobs


def build_package(spec, blobs, dapper):
    """Assemble one gap's selected mechanisms from immutable association captures.

    The caller resolves DB/import aliases and captures network responses first.
    This function does not perform selection by embeddings or model inference.
    """
    spec = deepcopy(spec)
    require(spec.get('input_version') == INPUT_VERSION, 'Unsupported build input version')
    allowed = {'input_version', 'prefixes', 'identifier_policy', 'dapper_pin', 'selection', 'dismech',
               'mechanisms', 'gene_sets', 'dapper_context', 'captures', 'artifacts', 'policy', 'authoring', 'external_evidence'}
    require(set(spec) == allowed, f'Build input keys differ from contract: {sorted(set(spec) ^ allowed)}')
    canonical_json(spec)
    require(set(blobs) == set(spec['artifacts']), 'Artifact byte map does not match manifest')
    require(spec['dapper_pin']['snapshot_sha256'] == dapper.manifest['snapshot_sha256'], 'DAPPER snapshot pin mismatch')
    resolver = dapper.resolver(spec['prefixes'])
    policy = spec['policy']
    require(set(policy) == {'max_nodes', 'max_edges', 'max_package_bytes', 'max_anchors', 'max_dismech_mechanisms',
                            'max_candidates_per_target', 'retain_node_ids', 'allow_incomplete_capture'}, 'Unknown or missing build policy')
    for key, value in policy.items():
        if key not in ('retain_node_ids', 'allow_incomplete_capture'):
            require(type(value) is int and value > 0, f'{key} must be a positive integer')
    require(type(policy['allow_incomplete_capture']) is bool, 'allow_incomplete_capture must be boolean')
    selection = spec['selection']
    for key in ['eaggl_mechanism_ids', 'dismech_mechanism_ids', 'dismissed_eaggl_ids']:
        selection[key] = unique(selection[key], key)
    anchors = selection['eaggl_mechanism_ids']
    require(0 < len(anchors) <= policy['max_anchors'], 'A bounded, nonempty EAGGL anchor set is required')
    require(len(selection['dismech_mechanism_ids']) <= policy['max_dismech_mechanisms'], 'DisMech context budget exceeded')
    require(set(anchors).isdisjoint(selection['dismissed_eaggl_ids']), 'Selected anchor is also dismissed')
    require(set(selection['origins']) == set(anchors), 'Every anchor needs its selection origin')
    require(set(spec['mechanisms']) == set(anchors), 'Mechanism bindings must exactly match selected anchors')
    require(selection['expansion_policy'] == {'rounds': 1, 'reducer': 'mean', 'connection_scope': 'direct', 'context': ''}, 'Unsupported expansion policy')
    for identity in anchors:
        require(identity.startswith('factor:'), 'A current CFDE factor identity is required')
        resolver.expand(identity)
        fit = spec['mechanisms'][identity]['fit']
        require(identity == f"factor:{fit['trait_group']}:{fit['phenotype']}:{fit['model']}:{fit['factor']}", 'Mechanism identity/fit mismatch')
    models = {m['fit']['model'] for m in spec['mechanisms'].values()}
    require(len(models) == 1, 'A package must use one CFDE model')
    model = next(iter(models))
    require(spec['authoring']['assembly_builder'] == {'version': BUILD_VERSION, 'source_sha256': sha256(Path(__file__).read_bytes())},
            'Builder code pin mismatch; replay with the captured builder version')

    document = deepcopy(spec['dapper_context'])
    require(document.get('prefixes', spec['prefixes']) == spec['prefixes'], 'DAPPER context prefix map differs')
    document['prefixes'] = spec['prefixes']
    index = dapper.validate(document)
    payloads, source_artifacts, output_files = {}, {}, {}
    for key, item in sorted(spec['artifacts'].items()):
        data = blobs[key]
        require(sha256(data) == item['sha256'], f'Artifact checksum mismatch: {key}')
        require(item['format'] in ('json', 'yaml', 'text'), f'Unknown artifact format: {key}')
        require(Path(item['filename']).name == item['filename'], f'Invalid artifact filename: {key}')
        if item['format'] != 'text': payloads[key] = decode(data, item['format'])
        path = f"sources/{item['sha256']}.{item['format']}"
        output_files[path] = data
        file_node = dapper.file(item['filename'], data, {'json': 'application/json', 'yaml': 'application/yaml', 'text': 'text/plain'}[item['format']])
        if file_node['id'] not in index:
            document.setdefault('files', []).append(file_node); index[file_node['id']] = file_node
        else:
            require(index[file_node['id']] == file_node, 'Conflicting source File payload')
        source_artifacts[key] = {k: v for k, v in item.items() if k != 'path'}
        source_artifacts[key].update(path=path, dapper_file_id=file_node['id'])

    # HTTP metadata is useful only if its parsed response matches the exact saved body.
    for key, wrapper in payloads.items():
        if not isinstance(wrapper, dict) or not {'url', 'method', 'status'} <= wrapper.keys():
            continue
        if 'body_sha256' in wrapper:
            body_key = key + '.body'
            require(body_key in blobs, f'Missing exact HTTP body: {key}')
            require(sha256(blobs[body_key]) == wrapper['body_sha256'] and
                    len(blobs[body_key]) == wrapper['body_bytes'], f'HTTP body checksum/size mismatch: {key}')
            if 'response' in wrapper:
                require(body_key in payloads and canonical_json(payloads[body_key]) == canonical_json(wrapper['response']),
                        f'HTTP parsed response differs from exact body: {key}')
        else:
            require('response' not in wrapper, f'HTTP response lacks an exact body: {key}')

    def source(reference):
        require(reference['artifact_id'] in payloads, f'Unknown structured artifact: {reference["artifact_id"]}')
        return pointer(payloads[reference['artifact_id']], reference['pointer'])

    def bound_node(identity, cls):
        require(identity in index and identity.startswith('dapper:' + cls + '.'), f'Missing {cls} binding: {identity}')
        return index[identity]

    gap_binding = spec['dismech']['knowledge_gap']
    gap = source(gap_binding['source_ref'])
    gap_node = bound_node(selection['knowledge_gap_id'], 'KnowledgeGap')
    require(gap_node['text'] == gap['prompt'], 'Selected gap text differs from frozen source')
    require(gap_node.get('gap_description') == (gap.get('rationale') or gap['prompt']), 'Selected gap rationale differs from frozen source')
    dismech = {'source_revision': spec['dismech']['source_revision'],
               'knowledge_gap': {**gap_binding, 'dapper_id': gap_node['id'], 'prompt': gap['prompt'],
                                 'rationale': gap.get('rationale'), 'source_kind': gap.get('kind'), 'source_status': gap.get('status'),
                                 'proposed_experiments': gap.get('proposed_experiments', [])},
               'mechanisms': {}, 'other_context': [], 'related_knowledge_gaps': {'status': 'provided_subset', 'items': []}}
    require(set(spec['dismech']['mechanisms']) == set(selection['dismech_mechanism_ids']), 'DisMech selections/bindings differ')
    for key, binding in sorted(spec['dismech']['mechanisms'].items()):
        raw = source(binding['source_ref']); node = bound_node(binding['dapper_id'], 'Mechanism')
        require(node.get('name') == raw.get('name') and node.get('description') == (raw.get('description') or raw['name']),
                'DisMech mechanism projection differs from source')
        for factor, match in binding.get('associated_eaggl_mechanisms', {}).items():
            require(factor in spec['mechanisms'], 'Semantic association points outside selected mechanisms')
            score = match.get('semantic_similarity')
            if match['status'] == 'computed':
                require(-1 <= finite(score, 'semantic similarity') <= 1, 'Cosine outside [-1,1]')
                require(bool(selection['semantic_retrieval'].get('embedding_run_id')), 'Computed similarity needs an embedding run')
            else:
                require(score is None, 'Uncomputed similarity must be null')
        dismech['mechanisms'][key] = {**deepcopy(raw), **binding}
    for binding in sorted(spec['dismech'].get('other_context', []), key=lambda b: b['source_id']):
        dismech['other_context'].append({**binding, 'record': source(binding['source_ref'])})
    for binding in sorted(spec['dismech'].get('related_knowledge_gaps', []), key=lambda b: b['source_id']):
        raw = source(binding['source_ref'])
        dismech['related_knowledge_gaps']['items'].append({**binding, 'prompt': raw['prompt'], 'kind': raw['kind'], 'status': raw.get('status')})

    captures = spec['captures']
    require(set(captures['connections']) == set(TARGETS), 'Record all four typed query outcomes')
    query_states, candidates, edges, edge_refs = {}, {}, {}, {}
    blockers = []
    expected_bioindex = {('factor', identity, kind) for identity in anchors for kind in ('gene', 'gene_set')}
    expected_bioindex |= {('trait', m['fit']['trait_id'], kind) for m in spec['mechanisms'].values() for kind in ('gene', 'gene_set')}
    actual_bioindex = [(c['scope'], c.get('mechanism_id') if c['scope'] == 'factor' else c.get('trait_id'), c['kind'])
                      for c in captures['bioindex']]
    require(len(actual_bioindex) == len(set(actual_bioindex)), 'Duplicate BioIndex query binding')
    require(set(actual_bioindex) <= expected_bioindex, 'Unexpected BioIndex query binding')
    for scope, identity, kind in sorted(expected_bioindex - set(actual_bioindex)):
        require(policy['allow_incomplete_capture'], f'Required BioIndex query unavailable: {scope}/{identity}/{kind}')
        blockers.append(f'bioindex:{scope}:{identity}:{kind}:not_captured')

    def add_edge(edge, location):
        edge_id = edge['id']
        for field in ['raw_score', 'normalized_score', 'weight']:
            if field in edge: finite(edge[field], f'edge {field}')
        require(edge['source'] in known_nodes and edge['target'] in known_nodes, 'Dangling graph edge')
        require(set(edge.get('path_nodes', [])).issubset(known_nodes), 'Dangling graph path endpoint')
        if edge_id in edges:
            require(edges[edge_id] == edge, f'Conflicting duplicate edge: {edge_id}')
        else: edges[edge_id] = edge
        edge_refs.setdefault(edge_id, []).append(location)

    known_nodes = set(anchors)
    direct_edge_occurrences = []
    for target in TARGETS:
        capture = captures['connections'][target]
        if capture['status'] != 'captured':
            require(capture['status'] in ('not_captured', 'failed'), f'Invalid query status: {target}')
            require(policy['allow_incomplete_capture'], f'Required query unavailable: {target}')
            query_states[target] = capture['status']; blockers.append(f'{target}:{capture["status"]}')
            continue
        wrapper = source(ref(capture['artifact_id']))
        request, response = wrapper['request'], wrapper['response']
        require(wrapper['status'] == 200 and wrapper['method'] == 'POST', f'Unsuccessful connections capture: {target}')
        require(request['target_type'] == target and request['model'] == model, 'Query target/model mismatch')
        require(unique([a['node_id'] for a in request['anchor_items']], 'request anchors') == anchors, 'Query uses a different seed set')
        require(unique(request['exclude_node_ids'], 'excluded nodes') == anchors, 'Query exclusion set differs from seeds')
        require(all(request[k] == selection['expansion_policy'][k] for k in ('reducer', 'connection_scope', 'context')), 'Query expansion parameters differ')
        require(type(request['limit']) is int and 0 < request['limit'] <= policy['max_candidates_per_target'], 'Query candidate limit exceeds policy')
        require(response['candidate_count'] == len(response['candidates']) <= request['limit'], 'Candidate count/limit mismatch')
        query_states[target] = 'empty' if not response['candidates'] else ('ok_limit_reached' if len(response['candidates']) == request['limit'] else 'ok')
        seen = set()
        for i, candidate in enumerate(response['candidates']):
            node = candidate['candidate']; identity = node['node_id']
            require(node['node_type'] == target and identity.startswith(target + ':'), 'Candidate type mismatch')
            require(identity not in seen and identity not in anchors, 'Duplicate/seed candidate')
            seen.add(identity); resolver.expand(identity)
            require(identity not in candidates, 'Candidate repeated across typed queries')
            finite(candidate['aggregate_score'], 'candidate aggregate_score')
            candidates[identity] = {'node': node, 'candidate': candidate, 'source_ref': ref(capture['artifact_id'], f'/response/candidates/{i}')}
            known_nodes.add(identity)
            for j, edge in enumerate(candidate.get('edges', [])):
                direct_edge_occurrences.append((edge, ref(capture['artifact_id'], f'/response/candidates/{i}/edges/{j}')))
        graph_nodes = {n['node_id'] for n in response['graph']['nodes']}
        require(graph_nodes <= known_nodes, 'Unrepresented graph nodes in response')
        for i, edge in enumerate(response['graph']['edges']):
            direct_edge_occurrences.append((edge, ref(capture['artifact_id'], f'/response/graph/edges/{i}')))
    for edge, location in direct_edge_occurrences: add_edge(edge, location)
    direct_ids = set(edges)
    context_capture = captures['contextual']
    contextual_returned = 0
    if context_capture['status'] == 'captured':
        wrapper = source(ref(context_capture['artifact_id']))
        require(wrapper['status'] == 200 and wrapper['method'] == 'POST', 'Unsuccessful contextual capture')
        requested_nodes = set(unique(wrapper['request']['node_ids'], 'contextual nodes'))
        require(wrapper['request']['model'] == model and requested_nodes <= known_nodes and set(anchors) <= requested_nodes, 'Contextual request scope mismatch')
        for i, edge in enumerate(wrapper['response']['edges']):
            require({edge['source'], edge['target'], *edge.get('path_nodes', [])} <= requested_nodes, 'Contextual edge outside queried nodes')
            add_edge(edge, ref(context_capture['artifact_id'], f'/response/edges/{i}'))
        contextual_returned = len(wrapper['response']['edges']); query_states['contextual'] = 'ok' if contextual_returned else 'empty'
    else:
        require(context_capture['status'] in ('not_captured', 'failed') and policy['allow_incomplete_capture'], 'Required contextual capture unavailable')
        query_states['contextual'] = context_capture['status']; blockers.append('contextual:' + context_capture['status'])

    ranking = sorted(candidates, key=lambda key: (-candidates[key]['candidate']['aggregate_score'], key))
    if policy['retain_node_ids'] is None:
        retained = set(anchors) | set(ranking[:max(0, policy['max_nodes'] - len(anchors))])
    else:
        policy['retain_node_ids'] = unique(policy['retain_node_ids'], 'retained nodes')
        retained = set(policy['retain_node_ids']) | set(anchors)
    require(retained <= known_nodes and len(retained) <= policy['max_nodes'], 'Retention selection outside graph/budget')
    eligible_edges = [key for key, edge in edges.items() if {edge['source'], edge['target'], *edge.get('path_nodes', [])} <= retained]
    retained_edges = sorted(eligible_edges)[:policy['max_edges']]
    if context_capture['status'] == 'captured':
        require(retained <= requested_nodes, 'Retained nodes were not included in contextual query')

    mechanisms, traits = {}, {}
    observed_loadings = {(edge['source'], kind) for edge in edges.values()
                         for kind in ('gene', 'gene_set', 'trait')
                         if edge['family'] == 'factor_' + kind + '_direct'}
    queried_trait_collections, observed_trait_collections = set(), set()
    for identity, binding in sorted(spec['mechanisms'].items()):
        node = bound_node(binding['dapper_id'], 'Mechanism')
        fit = binding['fit']; trait_id = fit['trait_id']; resolver.expand(trait_id)
        require(trait_id == f"trait:{fit['trait_group']}:{fit['phenotype']}", 'Trait grouping key differs from fit')
        mechanisms[identity] = {**binding, 'display_name': node['name']}
        for collection, target in [('gene_loadings', 'gene'), ('gene_set_loadings', 'gene_set'), ('trait_loadings', 'trait')]:
            mechanisms[identity][collection] = {'status': query_states[target], 'items': {}}
        traits.setdefault(trait_id, {'model': model, 'gene_associations': {'status': 'not_available', 'items': {}},
                                    'gene_set_associations': {'status': 'not_available', 'items': {}}})
    # BioIndex rows are not graph membership. Join only retained direct relationships.
    bioindex_coverage = []
    for capture in sorted(captures['bioindex'], key=lambda x: (x.get('mechanism_id', x.get('trait_id', '')), x['kind'], x['artifact_id'])):
        if capture.get('scope') == 'trait':
            trait_id, kind, artifact_id = capture['trait_id'], capture['kind'], capture['artifact_id']
            require(trait_id in traits and kind in ('gene', 'gene_set'), 'Invalid trait query binding')
            fit = next(m['fit'] for m in mechanisms.values() if m['fit']['trait_id'] == trait_id)
            response = source(ref(artifact_id))
            require(response['q'] == [fit['phenotype'], model], 'Trait query scope mismatch')
            require(response['index'] == 'pigean-' + kind.replace('_', '-') + '-phenotype', 'Trait index mismatch')
            trait_collection = traits[trait_id][kind + '_associations']
            queried_trait_collections.add((trait_id, kind))
            if response['data']: observed_trait_collections.add((trait_id, kind))
            for i, row in enumerate(response['data']):
                require((row['phenotype'], row['trait_group'], row['gene_set_size']) ==
                        (fit['phenotype'], fit['trait_group'], model), 'Trait row scope mismatch')
                identity = kind + ':' + row[kind]
                if identity not in retained: continue
                keys = ['combined', 'log_bf', 'prior'] if kind == 'gene' else ['beta', 'beta_uncorrected', 'rs_score']
                collection = traits[trait_id][kind + '_associations']; collection['status'] = 'ok'
                collection['items'].setdefault(identity, {'observations': []})['observations'].append(
                    {'reported_metrics': {k: finite(row[k], k) for k in keys if k in row},
                     'source_ref': ref(artifact_id, f'/data/{i}'), 'source_scope': 'phenotype_query'})
            bioindex_coverage.append({'artifact_id': artifact_id, 'returned_rows': len(response['data']),
                                     'limit': response.get('limit'), 'continuation_present': bool(response.get('continuation')),
                                     'progress': response.get('progress'), 'exhaustive': False})
            continue
        mechanism_id, kind, artifact_id = capture['mechanism_id'], capture['kind'], capture['artifact_id']
        require(mechanism_id in mechanisms and kind in ('gene', 'gene_set'), 'Invalid BioIndex binding')
        response = source(ref(artifact_id)); fit = mechanisms[mechanism_id]['fit']
        require(response['q'] == [fit['phenotype'], model, fit['factor']], 'BioIndex query scope mismatch')
        require(response['index'] == 'pigean-' + kind.replace('_', '-') + '-factor', 'BioIndex index mismatch')
        queried_trait_collections.add((fit['trait_id'], kind))
        if response['data']:
            observed_loadings.add((mechanism_id, kind))
            observed_trait_collections.add((fit['trait_id'], kind))
        bioindex_coverage.append({'artifact_id': artifact_id, 'returned_rows': len(response['data']),
                                 'limit': response.get('limit'), 'continuation_present': bool(response.get('continuation')),
                                 'progress': response.get('progress'), 'exhaustive': False})
        for i, row in enumerate(response['data']):
            require((row['factor'], row['phenotype'], row['trait_group'], row['gene_set_size']) ==
                    (fit['factor'], fit['phenotype'], fit['trait_group'], model), 'BioIndex row scope mismatch')
            identity = kind + ':' + row[kind]
            if identity not in retained: continue
            direct = [e for e in retained_edges if edges[e]['family'] == 'factor_' + kind + '_direct' and
                      edges[e]['source'] == mechanism_id and edges[e]['target'] == identity]
            if not direct: continue  # A source row is retained in its artifact but is not a new graph edge.
            collection = mechanisms[mechanism_id][kind + '_loadings']['items']
            require(identity not in collection, f'Duplicate BioIndex observation: {identity}')
            value = finite(row['factor_value'], 'factor_value')
            result_key = f'{mechanism_id}:{identity}'
            collection[identity] = {'result_key': result_key, 'factor_value': value, 'source_ref': ref(artifact_id, f'/data/{i}'),
                                    'interactive_edge_ids': direct,
                                    'reported_values_agree_within_1e_6': all(abs(value - edges[e]['raw_score']) <= 1e-6 for e in direct)}
            mechanisms[mechanism_id][kind + '_loadings']['status'] = 'ok'
            keys = ['combined', 'log_bf', 'prior'] if kind == 'gene' else ['beta', 'beta_uncorrected', 'rs_score']
            metrics = {k: finite(row[k], k) for k in keys if k in row}
            trait_collection = traits[fit['trait_id']][kind + '_associations']
            trait_collection['status'] = 'ok'
            # One trait row can occur in multiple factor queries; retain each source occurrence.
            trait_collection['items'].setdefault(identity, {'observations': []})['observations'].append(
                {'result_key': result_key, 'reported_metrics': metrics, 'source_ref': ref(artifact_id, f'/data/{i}'),
                 'ascertained_via_mechanism': mechanism_id})
    # Preserve direct observations even when BioIndex enrichment is absent.
    for edge_id in retained_edges:
        edge = edges[edge_id]
        if edge['source'] not in mechanisms: continue
        for kind in ('gene', 'gene_set', 'trait'):
            if edge['family'] != 'factor_' + kind + '_direct': continue
            collection = mechanisms[edge['source']][kind + '_loadings']
            item = collection['items'].setdefault(edge['target'], {'result_key': edge['source'] + ':' + edge['target'],
                                                                  'interactive_edge_ids': []})
            item['interactive_edge_ids'] = sorted(set(item['interactive_edge_ids']) | {edge_id})
            item['interactive_observations'] = [{k: edges[e][k] for k in ('id', 'raw_score', 'normalized_score', 'family', 'relation') if k in edges[e]}
                                                for e in item['interactive_edge_ids']]
            collection['status'] = 'ok'

    entities = {'genes': {}, 'gene_sets': {}}
    for identity, mechanism in mechanisms.items():
        for collection, target in [('gene_loadings', 'gene'), ('gene_set_loadings', 'gene_set'), ('trait_loadings', 'trait')]:
            mechanism[collection]['query_status'] = query_states[target]
            if not mechanism[collection]['items']:
                if (identity, target) in observed_loadings:
                    mechanism[collection]['status'] = 'omitted'
                elif query_states[target] == 'empty':
                    mechanism[collection]['status'] = 'empty'
                elif query_states[target] in ('ok', 'ok_limit_reached'):
                    # A positive multi-anchor query with no observation for this
                    # anchor is not a successful zero-result query for the anchor.
                    mechanism[collection]['status'] = 'not_available'
    for identity, trait in traits.items():
        for kind in ('gene', 'gene_set'):
            collection = trait[kind + '_associations']
            if not collection['items']:
                if (identity, kind) in observed_trait_collections:
                    collection['status'] = 'omitted'
                elif (identity, kind) in queried_trait_collections:
                    collection['status'] = 'empty'
    for identity in sorted(retained - set(anchors)):
        kind = candidates[identity]['node']['node_type']
        if kind == 'gene':
            entities['genes'][identity] = {'source_symbol': candidates[identity]['node']['node_key'], 'entity_uri': resolver.expand(identity),
                                           'external_identity_mapping': 'not_resolved'}
        if kind == 'gene_set':
            require(identity in spec['gene_sets'], f'Missing GeneSet binding: {identity}')
            binding = spec['gene_sets'][identity]; node = bound_node(binding['dapper_id'], 'GeneSet')
            require(identity in node.get('alternate_identifier', []), f'GeneSet alias mismatch: {identity}')
            entities['gene_sets'][identity] = {**binding, 'display_name': node['name']}
    # Verify all embedded source locators, including supplied associations/attachments.
    source_ref_count = 0
    def check_refs(value):
        nonlocal source_ref_count
        if isinstance(value, dict):
            if 'artifact_id' in value and 'pointer' in value:
                source(value); source_ref_count += 1
            for key, child in value.items():
                if key in ('id', 'reference', 'entity_uri') and isinstance(child, str) and ':' in child:
                    try: resolver.expand(child)
                    except ValueError as exc: raise EvidenceBuildError(f'Unresolvable identifier: {child}') from exc
                check_refs(child)
        elif isinstance(value, list):
            for child in value: check_refs(child)
    for node in document.values():
        if isinstance(node, list): node.sort(key=lambda x: x['id'])  # Within-node arrays retain their scientific order.
    index = dapper.validate(document)
    require(spec['authoring']['required_question'] == gap_node['id'], 'Authoring question differs from selection')
    require(spec['external_evidence']['status'] == 'not_queried' and not spec['external_evidence'].get('assertions') and
            not spec['external_evidence'].get('ledger'), 'Initial package cannot contain mutable enrichment results')
    packet = {'package_version': PACKAGE_VERSION, 'prefixes': spec['prefixes'], 'identifier_policy': spec['identifier_policy'],
              'builder': {'version': BUILD_VERSION, 'source_sha256': sha256(Path(__file__).read_bytes())},
              'dapper_pin': {**spec['dapper_pin'], 'snapshot_manifest_sha256': dapper.manifest_sha256},
              'selection': selection, 'dismech': dismech, 'pigean': {'model': model, 'mechanisms': mechanisms, 'traits': traits,
                 'graph': {'node_ids': sorted(retained),
                           'edges': [{**edges[k], 'source_refs': sorted(edge_refs[k], key=lambda r: (r['artifact_id'], r['pointer']))} for k in retained_edges]},
                 'contextual_relationships': {'status': query_states['contextual'], 'returned_edges': contextual_returned,
                                              'new_unique_edges': len(set(edges) - direct_ids)},
                 'candidates': {k: candidates[k] for k in sorted(retained - set(anchors))}},
              'entities': entities, 'dapper_context': document, 'source_artifacts': source_artifacts,
              'coverage': {'queries': query_states, 'bioindex_queries': bioindex_coverage, 'captured_nodes': len(known_nodes), 'captured_unique_edges': len(edges),
                           'retained_nodes': len(retained), 'retained_edges': len(retained_edges),
                           'omitted_node_ids': sorted(known_nodes - retained), 'omitted_edge_ids': sorted(set(edges) - set(retained_edges)),
                           'ranking': 'aggregate_score descending; exact node ID ascending on ties',
                           'upstream_total': None, 'same_upstream_build_verified': False},
              'policy': policy, 'external_evidence': spec['external_evidence'], 'authoring': spec['authoring'],
              'readiness': {'input_capture_complete': not blockers, 'capture_blockers': sorted(blockers),
                            'agent_dispatch_validated': False, 'remaining_checks': ['runtime and trusted attribution', 'target-model token budget', 'worker authorization and tool policy']}}
    check_refs(packet)
    # Bind local instruction metadata to the exact bytes included with this package.
    for instruction in [spec['authoring']['skill'], spec['authoring']['contract'], *spec['authoring']['references']]:
        require(instruction['artifact_id'] in blobs, 'Instruction artifact is not frozen')
        require(sha256(blobs[instruction['artifact_id']]) == instruction['sha256'], 'Instruction hash mismatch')
    package_bytes = canonical_json(packet)
    require(len(package_bytes) <= policy['max_package_bytes'], 'Serialized evidence package exceeds byte budget')
    require(yaml.__version__ == '6.0.2', 'Deterministic YAML renderer requires PyYAML 6.0.2')
    class Dumper(yaml.SafeDumper):
        def ignore_aliases(self, data): return True
    yaml_bytes = yaml.dump(decode(package_bytes), Dumper=Dumper, sort_keys=True, allow_unicode=True, width=100).encode('utf-8')
    require(decode(yaml_bytes, 'yaml') == packet, 'YAML/JSON round-trip mismatch')
    output_files.update({'evidence-package.json': package_bytes, 'evidence-package.yaml': yaml_bytes})
    manifest = {'builder_version': BUILD_VERSION, 'package_sha256': sha256(package_bytes),
                'yaml_sha256': sha256(yaml_bytes), 'checked_source_refs': source_ref_count, 'checked_dapper_objects': len(index),
                'files': {name: sha256(data) for name, data in sorted(output_files.items())}}
    output_files['manifest.json'] = canonical_json(manifest)
    return BuiltPackage(packet, output_files, manifest)
