"""Collect a gap and selected factor IDs into frozen, replayable builder inputs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi

from .evidence_package import (BUILD_VERSION, EvidenceBuildError, INPUT_VERSION, TARGETS, build_package, canonical_json,
                               decode, finite, pointer, ref, require, sha256, unique)

INTERACTIVE_BASE = 'https://dev.cfdeknowledge.org'
BIOINDEX_BASE = 'https://cfde-dev.hugeampkpnbi.org'
DISMECH_PREFIX_ALIASES = {'hgnc': 'HGNC'}


def add_source_prefixes(value, namespaces, prefixes):
    """Declare known source spellings without rewriting captured identifiers.

    Real DisMech gene terms use lowercase hgnc although its schema declares
    HGNC. Both spellings resolve through the already selected canonical binding
    (the pinned DAPPER namespace takes precedence over DisMech's schema).
    Unknown spellings remain errors; this is not general case folding.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('id', 'reference') and isinstance(child, str) and ':' in child and not re.search(r'\s', child) and not child.startswith(('http:', 'https:', 'urn:')):
                prefix = child.split(':', 1)[0]
                canonical = DISMECH_PREFIX_ALIASES.get(prefix, prefix)
                require(canonical in namespaces, f'Unknown DisMech CURIE prefix: {prefix}')
                require(prefix not in namespaces or namespaces[prefix] == namespaces[canonical],
                        f'Conflicting DisMech CURIE alias binding: {prefix}')
                prefixes[prefix] = namespaces[canonical]
            add_source_prefixes(child, namespaces, prefixes)
    elif isinstance(value, list):
        for child in value:
            add_source_prefixes(child, namespaces, prefixes)


class CaptureStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        require(not self.directory.exists(), f'Capture directory already exists: {directory}; use replay or a new output')
        (self.directory / 'inputs').mkdir(parents=True)
        self.artifacts, self.blobs = {}, {}
        self.lock = threading.Lock()

    def add(self, key, data, format, filename=None, **metadata):
        require(bool(re.fullmatch(r'[A-Za-z0-9_.-]+', key)), 'Invalid artifact key')
        path = f'inputs/{key}.{format}'
        descriptor = {'path': path, 'sha256': sha256(data), 'format': format,
                      'filename': filename or (key + '.' + format), **metadata}
        with self.lock:
            require(key not in self.artifacts, f'Duplicate capture name: {key}')
            (self.directory / path).write_bytes(data)
            self.artifacts[key], self.blobs[key] = descriptor, data
        return key


class HttpCaptureClient:
    """Bounded public read requests; every attempt and exact response body is saved."""
    def __init__(self, store, timeout=30, attempts=2, max_response_bytes=8_000_000):
        self.store, self.timeout, self.attempts, self.max_response_bytes = store, timeout, attempts, max_response_bytes
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def request(self, name, url, payload=None):
        for attempt in range(1, self.attempts + 1):
            key = f'{name}.attempt-{attempt}'
            sent = canonical_json(payload) if payload is not None else None
            method = 'POST' if payload is not None else 'GET'
            headers = {'Accept': 'application/json', 'User-Agent': 'REVEAL-EvidenceCollector/1'}
            if sent is not None: headers['Content-Type'] = 'application/json'
            captured = {'url': url, 'method': method, 'request': payload,
                        'retrieved_at': datetime.now(timezone.utc).isoformat()}
            body = b''
            try:
                try:
                    response = urlopen(Request(url, data=sent, headers=headers, method=method),
                                       timeout=self.timeout, context=self.ssl_context)
                except HTTPError as exc:
                    response = exc
                with response:
                    captured['status'] = response.status
                    captured['content_type'] = response.headers.get('Content-Type')
                    body = response.read(self.max_response_bytes + 1)
                require(len(body) <= self.max_response_bytes, 'HTTP response exceeded capture byte limit')
                captured['body_sha256'] = sha256(body)
                captured['body_bytes'] = len(body)
                try:
                    captured['response'] = decode(body)
                    format = 'json'
                except EvidenceBuildError:
                    format = 'text'
                self.store.add(key + '.body', body, format, origin=url)
            except (OSError, URLError, TimeoutError, EvidenceBuildError) as exc:
                captured['error'] = str(exc)
                captured.setdefault('status', None)
                if body: self.store.add(key + '.partial-body', body, 'text', origin=url)
            self.store.add(key, canonical_json(captured), 'json', origin=url)
            if captured.get('status') == 200 and isinstance(captured.get('response'), dict) and 'error' not in captured:
                return key, captured
            if captured.get('status') not in (None, 429, 500, 502, 503, 504) or attempt == self.attempts:
                raise EvidenceBuildError(f'{name} failed (HTTP {captured.get("status")}); attempt saved as {key}')
            time.sleep(min(attempt, 2))
        raise EvidenceBuildError(f'{name} exhausted retries')


def parse_factor(identity, model):
    parts = identity.split(':')
    require(len(parts) == 5 and parts[0] == 'factor' and all(parts),
            'Use a full interactive factor ID: factor:<trait_group>:<phenotype>:<model>:<factor>')
    require(parts[3] == model, f'Factor model differs from requested model: {identity}')
    require(',' not in ''.join(parts), 'BioIndex query keys cannot contain commas')
    return {'trait_group': parts[1], 'phenotype': parts[2], 'model': parts[3], 'factor': parts[4],
            'trait_id': f'trait:{parts[1]}:{parts[2]}', 'upstream_build': None}


def gz_records(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        for line in stream: yield decode(line.encode('utf-8'))


def collect_package(*, gap_id, factor_ids, output, dapper, project_root, dismech_source, dismech_index,
                    geneset_import, model='cfde-inc-v2', limit=100, max_nodes=250, max_edges=1000,
                    client_factory=HttpCaptureClient, geneset_resolver=None,
                    selected_graphs=('biomarkerkg', 'prokn'), max_accounts=3,selection_metadata=None,
                    max_parallel_requests=4):
    """Resolve input IDs, retrieve bounded evidence, freeze sources, then build.

    The local DisMech index and GeneSet export are configurable source adapters;
    they can later be replaced by DB lookups returning the same frozen records.
    """
    started = time.monotonic()
    require(type(max_parallel_requests) is int and 1 <= max_parallel_requests <= 4,
            'max_parallel_requests must be between 1 and 4')
    project_root, dismech_source = Path(project_root).resolve(), Path(dismech_source).resolve()
    dismech_index, geneset_import = Path(dismech_index), Path(geneset_import)
    factor_ids = unique(factor_ids, 'factor IDs')
    require(0 < len(factor_ids) <= 10, 'Supply between one and ten EAGGL factors')
    require(type(limit) is int and 1 <= limit <= 100, 'limit must be between 1 and 100')
    require(type(max_nodes) is int and len(factor_ids) <= max_nodes <= 250, 'max_nodes must preserve anchors and be at most 250')
    require(type(max_edges) is int and 1 <= max_edges <= 1000, 'max_edges must be between 1 and 1000')
    fits = {identity: parse_factor(identity, model) for identity in factor_ids}
    gap_manifest = decode((dismech_index / 'manifest.json').read_bytes())
    for filename in ['knowledge-gaps.jsonl.gz', 'gap-attachments.jsonl.gz']:
        require(sha256((dismech_index / filename).read_bytes()) == gap_manifest['files'][filename]['sha256'], 'DisMech index checksum mismatch')
    matches = [r for r in gz_records(dismech_index / 'knowledge-gaps.jsonl.gz') if r['id'] == gap_id or r['discussion_id'] == gap_id]
    require(len(matches) == 1, 'DisMech ID must resolve to exactly one indexed knowledge gap')
    gap_record = matches[0]
    require(gap_record['kind'] in ('KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH'), 'Selected discussion is not a knowledge gap')
    hashes = {r['path']: r['sha256'] for r in decode((dismech_index / 'source-files.json').read_bytes())}
    attachments = [a for a in gz_records(dismech_index / 'gap-attachments.jsonl.gz') if a['gap_id'] == gap_record['id']]
    store = CaptureStore(output)
    if selection_metadata:
        require(set(selection_metadata['origins'])==set(factor_ids),'Selection provenance differs from selected anchors')
        store.add('selection-provenance',canonical_json(selection_metadata),'json',filename='selection-provenance.json')
    client = client_factory(store)
    timings = {'format': 'reveal.evidence-collection-timings/1', 'max_parallel_requests': max_parallel_requests,
               'status': 'failed', 'stages': []}

    def parallel(stage, operations):
        """Bound I/O fanout and consume results in deterministic source order.

        The transport keeps its existing per-request deadlines/retries and exact
        attempt captures. Only independent reads run here; DAPPER assembly and
        graph/alias decisions remain on the calling thread.
        """
        began = time.monotonic(); status = 'failed'
        try:
            with ThreadPoolExecutor(max_workers=max_parallel_requests, thread_name_prefix='evidence-read') as executor:
                pending = [executor.submit(function, *arguments) for function, arguments in operations]
                results = [future.result() for future in pending]
            status = 'completed'
            return results
        finally:
            timings['stages'].append({'stage': stage, 'seconds': round(time.monotonic() - began, 6),
                                      'request_count': len(operations), 'status': status})

    def document(relative):
        key = 'dismech-' + sha256(relative.encode())[:16]
        if key in store.blobs: return key, decode(store.blobs[key], 'yaml')
        path = (dismech_source / relative).resolve()
        require(path.is_relative_to(dismech_source), 'DisMech source path escapes checkout')
        data = path.read_bytes()
        require(relative in hashes and sha256(data) == hashes[relative], 'DisMech checkout differs from frozen index; refresh the index or use its source revision')
        store.add(key, data, 'yaml', filename=path.name,
                  origin={'repository': 'https://github.com/monarch-initiative/dismech', 'commit': gap_manifest['source_commit'], 'path': relative})
        return key, decode(data, 'yaml')

    try:
        gap_artifact, gap_doc = document(gap_record['source_file'])
        raw_gap = pointer(gap_doc, gap_record['source_pointer'])
        require(raw_gap == gap_record['raw'], 'Gap index differs from source document')
        dapper.schema.imports_closure()
        namespaces = {k: str(v) for k, v in dapper.schema.namespaces().items()}
        source_schema_path = dismech_source / 'src/dismech/schema/dismech.yaml'
        source_schema = decode(source_schema_path.read_bytes(), 'yaml')
        namespaces = {**source_schema['prefixes'], **namespaces}  # DAPPER bindings take precedence.
        prefixes = {name: namespaces[name] for name in ['dapper', 'MONDO', 'CL', 'PMID', 'GO', 'ECTO']}
        prefixes.update(factor='urn:cfde:factor:', gene='urn:cfde:gene:', gene_set='urn:cfde:gene_set:',
                        trait='urn:cfde:trait:', cfde='urn:cfde:record:')
        add_source_prefixes(raw_gap, namespaces, prefixes)
        gap_node = {'text': raw_gap['prompt'], 'gap_description': raw_gap.get('rationale') or raw_gap['prompt'],
                    'gap_kind': gap_record['kind'], 'scope': gap_record['document_name']}
        disease_curie = (gap_record.get('disease_term') or {}).get('term', {}).get('id')
        if disease_curie: gap_node['about_entities'] = [dapper.resolver(prefixes).expand(disease_curie)]
        gap_node['id'] = dapper.compute_id(gap_node, 'KnowledgeGap', dapper.schema)
        context = {'knowledge_gaps': [gap_node], 'mechanisms': [], 'gene_sets': [], 'activities': [], 'files': []}
        dismech = {'source_revision': {'commit': gap_manifest['source_commit'], 'source_sha256': hashes[gap_record['source_file']]},
                   'knowledge_gap': {'source_id': gap_record['id'], 'source_ref': ref(gap_artifact, gap_record['source_pointer']),
                                     'disease': gap_record.get('disease_term'), 'attachments': []},
                   'mechanisms': {}, 'other_context': [], 'related_knowledge_gaps': []}
        for attachment in sorted(attachments, key=lambda x: x['attachment_index']):
            item = deepcopy_record(attachment)
            if attachment['resolution'] in ('resolved', 'whole_section', 'whole_document'):
                artifact_id, target_doc = document(attachment['target_source_file'])
                location = ref(artifact_id, attachment['target_pointer']); raw = pointer(target_doc, attachment['target_pointer'])
                add_source_prefixes(raw, namespaces, prefixes)
                item['source_ref'] = location
                if attachment['target_kind'] == 'pathophysiology' and attachment['resolution'] == 'resolved':
                    node = {'name': raw['name'], 'description': raw.get('description', raw['name'])}
                    node['id'] = dapper.compute_id(node, 'Mechanism', dapper.schema)
                    if node['id'] not in {m['id'] for m in context['mechanisms']}: context['mechanisms'].append(node)
                    dismech['mechanisms'][attachment['target_id']] = {'dapper_id': node['id'], 'source_ref': location,
                        'associated_eaggl_mechanisms': {factor: {'status': 'not_computed', 'semantic_similarity': None,
                                                               'association_basis': 'user_supplied_anchor'} for factor in factor_ids}}
                else:
                    dismech['other_context'].append({'source_id': attachment['target_id'], 'kind': attachment['target_kind'], 'source_ref': location})
            dismech['knowledge_gap']['attachments'].append(item)
        require(len(dismech['mechanisms']) <= 10, 'Linked DisMech mechanism count exceeds package budget')
        # Related questions are a bounded same-document context, not additional selected gaps.
        for i, row in enumerate(gap_doc.get('discussions', [])):
            if row.get('discussion_id') != gap_record['discussion_id'] and row.get('kind') in ('KNOWLEDGE_GAP', 'HUMAN_MODEL_MISMATCH'):
                dismech['related_knowledge_gaps'].append({'source_id': gap_record['document_id'] + '#discussion:' + row.get('discussion_id', f'/discussions/{i}'),
                                                        'source_ref': ref(gap_artifact, f'/discussions/{i}')})
        dismech['related_knowledge_gaps'] = dismech['related_knowledge_gaps'][:10]
        catalog_by_trait, factor_by_trait = {}, {}

        def query(name, index, keys):
            # Factor catalog resolution is separate from evidence retention;
            # lowering candidates must never lose a selected FactorN anchor.
            url = BIOINDEX_BASE + '/api/bio/query/' + index + '?' + urlencode({'q': ','.join(keys), 'limit': 100 if index=='pigean-factor' else limit})
            capture_id, wrapper = client.request(name, url)
            body_id = capture_id + '.body'
            require(body_id in store.blobs, 'HTTP capture is missing its exact response body')
            return body_id, wrapper['response']

        # Catalogs for different traits, and the interactive/BioIndex views of
        # each trait, are independent reads. Shared-trait anchors still reuse
        # one captured catalog of each kind, exactly as before.
        traits = sorted({(fit['trait_group'], fit['phenotype']) for fit in fits.values()})
        catalog_operations = []
        for group, phenotype in traits:
            tag = sha256(canonical_json((group, phenotype)))[:12]
            catalog_operations.extend([
                (client.request, ('catalog-' + tag, INTERACTIVE_BASE + '/api/interactive/catalog?' + urlencode(
                    {'entity_type': 'factor', 'q': phenotype, 'limit': 100, 'model': model}))),
                (query, ('factor-catalog-' + tag, 'pigean-factor', [phenotype, model]))])
        catalog_results = parallel('factor_catalogs', catalog_operations)
        for index, key in enumerate(traits):
            _, cat = catalog_results[2 * index]
            catalog_by_trait[key] = {n['node_id']: n for n in cat['response']['items']}
            factor_by_trait[key] = catalog_results[2 * index + 1]
        mechanisms, anchor_items = {}, []
        for identity, fit in sorted(fits.items()):
            key = (fit['trait_group'], fit['phenotype'])
            body_id, rows = factor_by_trait[key]
            found = [(i, row) for i, row in enumerate(rows['data']) if row['factor'] == fit['factor'] and row['phenotype'] == fit['phenotype']
                     and row['gene_set_size'] == model and row['trait_group'] == fit['trait_group']]
            require(len(found) == 1, f'Exact factor not found in bounded BioIndex result: {identity}; increase collection limit if clipped')
            i, row = found[0]
            native = catalog_by_trait[key].get(identity)
            if native is None:
                native = {'node_id': identity, 'node_type': 'factor', 'label': row['label'],
                          'subtitle': f"{fit['phenotype']} ({fit['factor']})", 'node_key': fit['factor']}
            anchor_items.append({k: native[k] for k in ['node_id', 'node_type', 'label', 'subtitle']})
            node = {'name': f"{fit['phenotype']} mechanism {fit['factor']}",
                    'description': f"EAGGL mechanism {identity}. Source label: {row['label']}."}
            node['id'] = dapper.compute_id(node, 'Mechanism', dapper.schema); context['mechanisms'].append(node)
            mechanisms[identity] = {'dapper_id': node['id'], 'fit': fit, 'source_label': row['label'], 'source_ref': ref(body_id, f'/data/{i}')}
        request = {'anchor_items': anchor_items, 'exclude_node_ids': factor_ids, 'model': model,
                   'reducer': 'mean', 'connection_scope': 'direct', 'context': '', 'limit': limit}
        responses = dict(zip(TARGETS, parallel('connections', [
            (client.request, ('connections-' + target, INTERACTIVE_BASE + '/api/interactive/connections',
                              {**request, 'target_type': target})) for target in TARGETS])))
        candidates = {}
        for target, (_, response) in responses.items():
            for candidate in response['response']['candidates']:
                identity = candidate['candidate']['node_id']
                require(identity not in candidates, 'Duplicate typed candidate')
                candidates[identity] = candidate
        database_resolution = None
        unresolved_sets = set()
        if geneset_resolver:
            candidate_sets = {identity for identity in candidates if identity.startswith('gene_set:')}
            began = time.monotonic()
            database_resolution = geneset_resolver(candidate_sets, model)
            timings['stages'].append({'stage': 'geneset_aliases', 'seconds': round(time.monotonic() - began, 6),
                                      'candidate_count': len(candidate_sets), 'status': 'completed'})
            resolved_sets = {row['node_id'] for row in database_resolution[2]}
            unresolved_sets = candidate_sets - resolved_sets
            store.add('geneset-alias-resolution', canonical_json({'gene_set_import_id': decode(database_resolution[0])['import_id'],
                'requested_node_ids': sorted(candidate_sets), 'resolved_node_ids': sorted(resolved_sets),
                'unresolved_node_ids': sorted(unresolved_sets),
                'policy': 'Keep mapped factor anchors; omit only unresolved GeneSet candidates from retained graph. Raw query results remain captured.'}), 'json')
        ranking = sorted((identity for identity in candidates if identity not in unresolved_sets), key=lambda k: (-finite(candidates[k]['aggregate_score'], 'candidate score'), k))
        retained = sorted(set(factor_ids) | set(ranking[:max_nodes - len(factor_ids)]))
        # These reads do not depend on one another. Keep each native factor and
        # trait observation separate, even when their returned values coincide.
        observations = []
        for identity, fit in sorted(fits.items()):
            tag = sha256(identity.encode())[:12]
            for kind in ('gene', 'gene_set'):
                observations.append(({'scope': 'factor', 'kind': kind, 'mechanism_id': identity},
                    (query, (f'{kind}-factor-{tag}', 'pigean-' + kind.replace('_', '-') + '-factor',
                             [fit['phenotype'], model, fit['factor']]))))
        for group, phenotype in sorted(factor_by_trait):
            tag = sha256(canonical_json([group, phenotype]))[:12]
            for kind in ('gene', 'gene_set'):
                observations.append(({'scope': 'trait', 'kind': kind, 'trait_id': f'trait:{group}:{phenotype}'},
                    (query, (f'{kind}-trait-{tag}', 'pigean-' + kind.replace('_', '-') + '-phenotype', [phenotype, model]))))
        observed = parallel('source_observations', [
            (client.request, ('contextual', INTERACTIVE_BASE + '/api/interactive/contextual-edges',
                              {'node_ids': retained, 'model': model})), *[operation for _, operation in observations]])
        contextual_id, _ = observed[0]
        bioindex = [{**metadata, 'artifact_id': result[0]} for (metadata, _), result in zip(observations, observed[1:])]
        # Resolve the retained set aliases against the completed import, preserving its DAPPER objects.
        wanted = {k for k in retained if k.startswith('gene_set:')}
        set_bindings = {}
        if geneset_resolver:
            manifest_bytes, activity, all_database_rows = database_resolution
            database_rows = [row for row in all_database_rows if row['node_id'] in wanted]
        else:
            manifest_bytes = (geneset_import / 'manifest.json').read_bytes()
            activity = decode((geneset_import / 'activity.json').read_bytes())
        manifest = decode(manifest_bytes)
        require(manifest['complete'] and manifest['model'] == model and manifest['encoded_rows'] == manifest['expected_rows'], 'Incomplete/wrong-model GeneSet import')
        def file_sha(path):
            import hashlib
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
            return digest.hexdigest()
        if not geneset_resolver:
            require(file_sha(geneset_import / 'records.jsonl.gz') == manifest['records_sha256'], 'GeneSet import checksum mismatch')
            require(file_sha(geneset_import / 'activity.json') == manifest['activity_sha256'], 'GeneSet activity checksum mismatch')
        selected_rows = []
        for row in (database_rows if geneset_resolver else gz_records(geneset_import / 'records.jsonl.gz') if wanted else []):
            if row['node_id'] in wanted and row['model'] == model:
                require(row['node_id'] not in set_bindings, 'Duplicate GeneSet source alias')
                context['gene_sets'].append(row['gene_set']); selected_rows.append(row)
                set_bindings[row['node_id']] = {'dapper_id': row['gene_set']['id'], 'import_id': manifest['import_id'],
                                               'membership_status': 'not_loaded', 'construction_provenance_status': 'not_loaded',
                                               'provenance_refs': [ref('geneset-import')]}
                if set(set_bindings) == wanted: break
        require(set(set_bindings) == wanted, f'Missing GeneSet aliases in import: {sorted(wanted - set(set_bindings))}')
        context['activities'].append(activity)
        store.add('geneset-import', manifest_bytes, 'json')
        store.add('geneset-selected-records', canonical_json(sorted(selected_rows, key=lambda r: r['node_id'])), 'json')
        context['prefixes'] = prefixes
        instructions = []
        paths = ['services/backend/agent-skills/construct-scientific-account/SKILL.md', 'docs/evidence-package.md',
                 'docs/scientific-account-construction.md', 'docs/pigean-claim-model.md', 'docs/dapper-integration.md', 'docs/agent-evidence-integration.md',
                 'services/backend/agent-skills/read-evidence-package/SKILL.md']
        for i, path in enumerate(paths):
            data = (project_root / path).read_bytes(); key = 'instruction-' + str(i)
            store.add(key, data, 'text', filename=Path(path).name, origin=path)
            instructions.append({'artifact_id': key, 'path': path, 'sha256': sha256(data)})
        builder_bytes = Path(__file__).with_name('evidence_package.py').read_bytes()
        store.add('assembly-builder-source', builder_bytes, 'text', filename='evidence_package.py')
        store.add('collector-source', Path(__file__).read_bytes(), 'text', filename='evidence_collector.py')
        spec = {'input_version': INPUT_VERSION, 'prefixes': prefixes,
                'identifier_policy': {'source_local_prefixes': ['factor', 'gene', 'gene_set', 'trait', 'cfde'],
                                       'dismech_record_ids': 'Opaque aliases resolved through source_ref and source revision.', 'preserve_existing_dapper_payloads': True},
                'dapper_pin': {'name': 'configured-snapshot', 'snapshot_sha256': dapper.manifest['snapshot_sha256']},
                'selection': {'knowledge_gap_id': gap_node['id'], 'dismech_mechanism_ids': sorted(dismech['mechanisms']),
                              'eaggl_mechanism_ids': factor_ids, 'origins': (selection_metadata or {}).get('origins',{k:'user_supplied' for k in factor_ids}),
                              'dismissed_eaggl_ids': (selection_metadata or {}).get('dismissed_eaggl_ids',[]),
                              'semantic_retrieval': (selection_metadata or {}).get('semantic_retrieval',{'status':'not_computed','embedding_run_id':None}),
                              'expansion_policy': {'rounds': 1, 'reducer': 'mean', 'connection_scope': 'direct', 'context': ''}},
                'dismech': dismech, 'mechanisms': mechanisms, 'gene_sets': set_bindings, 'dapper_context': context,
                'captures': {'connections': {k: {'status': 'captured', 'artifact_id': v[0]} for k, v in responses.items()},
                             'contextual': {'status': 'captured', 'artifact_id': contextual_id}, 'bioindex': bioindex},
                'artifacts': store.artifacts,
                'policy': {'max_nodes': max_nodes, 'max_edges': max_edges, 'max_package_bytes': 2_000_000, 'max_anchors': 10,
                           'max_dismech_mechanisms': 10, 'max_candidates_per_target': 100, 'retain_node_ids': retained, 'allow_incomplete_capture': False},
                'authoring': {'skill': instructions[0], 'contract': instructions[1], 'references': instructions[2:], 'required_question': gap_node['id'], 'max_accounts': max_accounts,
                              'assembly_builder': {'version': BUILD_VERSION, 'source_sha256': sha256(builder_bytes)}},
                'external_evidence': {'status': 'not_queried', 'selected_graphs': list(selected_graphs), 'ledger': [], 'assertions': []}}
        (store.directory / 'build-input.json').write_bytes(canonical_json(spec))
        began = time.monotonic()
        built = build_package(spec, store.blobs, dapper)
        built.write(store.directory / 'package')
        timings['stages'].append({'stage': 'validated_assembly', 'seconds': round(time.monotonic() - began, 6),
                                  'status': 'completed'})
        timings['status'] = 'completed'
        return built
    except Exception as exc:
        (store.directory / 'collection-error.json').write_bytes(canonical_json({'error': str(exc), 'artifacts': store.artifacts}))
        raise
    finally:
        timings['elapsed_seconds'] = round(time.monotonic() - started, 6)
        # Operational telemetry is outside both the scientific package and its
        # replay input: durations cannot change evidence identity or hashing.
        try:
            (store.directory / 'collection-timings.json').write_bytes(canonical_json(timings))
        except OSError:
            pass  # Optional telemetry cannot mask a source failure or a valid package.


def deepcopy_record(value):
    return decode(canonical_json(value))
