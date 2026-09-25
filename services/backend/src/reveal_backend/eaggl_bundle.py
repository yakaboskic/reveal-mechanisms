"""Validate and freeze EAGGL share bundles without executing their HTML or scripts."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
from collections import deque
from pathlib import Path
import re
import shutil
import tempfile

import numpy as np
from scipy import sparse

FORMAT = 'reveal-eaggl-bundle-v1'
TEMPLATE = 'factor-label-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(canonical(value) + '\n', encoding='utf-8')


def read_tsv(path, required):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f'{path}: missing or duplicate column names')
        if not set(required).issubset(reader.fieldnames):
            raise ValueError(f'{path}: required columns: {required}')
        rows = list(reader)
    if not rows or any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError(f'{path}: empty table or malformed rows')
    return rows


def unique(values, description):
    if not values or any(not isinstance(x, str) or not x.strip() for x in values):
        raise ValueError(f'{description}: empty identifiers')
    if len(set(values)) != len(values):
        raise ValueError(f'{description}: duplicate identifiers')


def read_matrix(path, factor_ids=None, genes=None):
    """NPZ needs explicit axis files; wide TSV carries its own row/column IDs."""
    path = Path(path)
    if path.suffix == '.npz':
        if factor_ids is None or genes is None:
            raise ValueError('NPZ requires factor_ids.tsv and genes.tsv')
        matrix = sparse.load_npz(path)
        if matrix.format != 'csr':
            raise ValueError('NPZ must be CSR (as exported by the share bundle)')
        matrix.check_format(full_check=True)
        if not matrix.has_canonical_format:
            raise ValueError('NPZ has unsorted or duplicate sparse coordinates')
    else:
        opener = gzip.open if path.suffix == '.gz' else open
        data, indices, indptr, row_ids = [], [], [0], []
        with opener(path, 'rt', encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, delimiter='\t')
            header = next(reader, [])
            if not header or header[0] != 'factor_id':
                raise ValueError('Wide matrix must begin with a factor_id column')
            columns = header[1:]
            unique(columns, 'matrix genes')
            for number, row in enumerate(reader, 2):
                if len(row) != len(header):
                    raise ValueError(f'Matrix row {number}: incorrect column count')
                values = np.asarray(row[1:], dtype=np.float64)
                if not np.isfinite(values).all() or np.any((values < 0) | (values > 1)):
                    raise ValueError(f'Matrix row {number}: expected finite capped values in [0, 1]')
                nonzero = np.flatnonzero(values)
                data.extend(values[nonzero]); indices.extend(nonzero); indptr.append(len(data))
                row_ids.append(row[0])
        if factor_ids is not None and factor_ids != row_ids:
            raise ValueError('factor_ids.tsv order differs from matrix rows')
        if genes is not None and genes != columns:
            raise ValueError('genes.tsv order differs from matrix columns')
        factor_ids, genes = row_ids, columns
        matrix = sparse.csr_matrix((data, indices, indptr), shape=(len(row_ids), len(columns)))
    unique(factor_ids, 'factor IDs'); unique(genes, 'genes')
    if matrix.shape != (len(factor_ids), len(genes)):
        raise ValueError('Matrix dimensions do not match factor/gene axes')
    if matrix.dtype.kind not in 'fiu' or not np.isfinite(matrix.data).all():
        raise ValueError('Loadings must be finite real numbers')
    if np.any((matrix.data < 0) | (matrix.data > 1)):
        raise ValueError('Expected already capped loadings in [0, 1]; no implicit recapping')
    # Float64 retains even float32 subnormals (the supplied bundle includes 1e-45).
    matrix = matrix.astype(np.float64)
    matrix.eliminate_zeros()
    return matrix, factor_ids, genes


def read_graph(path, factors):
    if path is None:
        return None
    if Path(path).suffix.lower() == '.html':
        match = re.search(r'const DATA=(.*?);const byId=', Path(path).read_text(encoding='utf-8'), re.S)
        if not match:
            raise ValueError('Cannot locate the share explorer JSON; supply --graph with a JSON export')
        graph = json.loads(match[1])
    else:
        graph = read_json(path)
    nodes = graph.get('nodes', [])
    unique([n['id'] for n in nodes], 'graph nodes')
    by_id = {n['id']: n for n in nodes}
    factor_ids = {f['factor_id'] for f in factors}
    leaves, edges, parent_edges = [], set(), set()
    for node in nodes:
        if len(node['id']) > 255 or node.get('kind') not in ('factor', 'group', 'root'):
            raise ValueError('Invalid graph node ID/kind')
        if node['kind'] == 'factor':
            leaves.append(node.get('factor_id'))
            if node.get('children'):
                raise ValueError('Factor graph leaves cannot have children')
            index = node.get('factor_index')
            if index is not None and (type(index) is not int or not 0 <= index < len(factors)
                                      or factors[index]['factor_id'] != node['factor_id']):
                raise ValueError('Graph factor_index/factor_id mismatch')
        for field, target_set in [('children', edges), ('parents', parent_edges)]:
            related = node.get(field, [])
            if len(related) != len(set(related)):
                raise ValueError('Duplicate graph edge')
            for other in related:
                if other not in by_id or other == node['id']:
                    raise ValueError('Graph has dangling edges or self loops')
                target_set.add((node['id'], other) if field == 'children' else (other, node['id']))
    if edges != parent_edges:
        raise ValueError('Graph parent and child edges disagree')
    if len(leaves) != len(set(leaves)) or set(leaves) != factor_ids:
        raise ValueError('Graph factor leaves do not exactly cover matrix factors')
    indegree = {key: 0 for key in by_id}
    for _, child in edges:
        indegree[child] += 1
    roots = [key for key, degree in indegree.items() if degree == 0]
    if roots != [graph.get('root')]:
        raise ValueError('Graph must have its declared single root')
    queue, visited = deque(roots), 0
    while queue:
        key = queue.popleft(); visited += 1
        for child in by_id[key].get('children', []):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(nodes):
        raise ValueError('Graph contains a cycle')
    return graph


def prepare(output, *, bundle=None, metadata=None, loadings=None, factor_ids=None,
            genes=None, graph=None, source_namespace='eaggl', source_version):
    output = Path(output)
    if not source_namespace.strip() or not source_version.strip():
        raise ValueError('A nonempty source namespace/version is required')
    if output.exists():
        raise ValueError('Output already exists; use a new directory for a new capture')
    paths = {}
    if bundle is not None:
        root = Path(bundle).resolve()
        data = root / 'data' if (root / 'data').is_dir() else root
        metadata = metadata or data / 'factor_metadata.tsv'
        loadings = loadings or next((p for p in [data / 'capped_factor_gene_loadings.npz',
                                                data / 'capped_factor_gene_loadings.tsv.gz'] if p.exists()), None)
        factor_ids = factor_ids or (data / 'factor_ids.tsv' if (data / 'factor_ids.tsv').exists() else None)
        genes = genes or (data / 'genes.tsv' if (data / 'genes.tsv').exists() else None)
        graph = graph or (root / 'index.html' if (root / 'index.html').exists() else None)
        for name in ('analysis_summary.json', 'capped_entries.tsv.gz', 'capped_factor_list.tsv'):
            if (data / name).exists():
                paths[name] = data / name
    if metadata is None or loadings is None:
        raise ValueError('Supply --bundle or both --metadata and --loadings')
    paths.update({k: Path(v).resolve() for k, v in dict(metadata=metadata, loadings=loadings,
                    factor_ids=factor_ids, genes=genes, graph=graph).items() if v is not None})
    source_files = {k: {'path': str(p), 'sha256': file_hash(p)} for k, p in paths.items()}
    # A supplied checksum manifest is authoritative for every consumed source file it lists.
    if bundle is not None:
        sums = root / 'SHA256SUMS.txt'
        if sums.exists():
            checks = {}
            for line in sums.read_text().splitlines():
                checksum, relative = line.split(maxsplit=1)
                checks[(root / relative.lstrip('*')).resolve()] = checksum
            for key, path in paths.items():
                if path in checks and source_files[key]['sha256'] != checks[path]:
                    raise ValueError(f'Source checksum mismatch: {path.name}')
    rows = read_tsv(metadata, ['factor_id', 'trait', 'factor', 'label'])
    unique([r['factor_id'] for r in rows], 'metadata factors')
    row_axis = [r['factor_id'] for r in read_tsv(factor_ids, ['factor_id'])] if factor_ids else None
    gene_axis = [r['gene'] for r in read_tsv(genes, ['gene'])] if genes else None
    matrix, row_axis, gene_axis = read_matrix(loadings, row_axis, gene_axis)
    mapping = {r['factor_id']: r for r in rows}
    if set(mapping) != set(row_axis):
        raise ValueError('Metadata factors do not exactly match the matrix')
    factors = []
    for index, key in enumerate(row_axis):
        row = mapping[key]
        if any(not row[field].strip() for field in ('label', 'trait', 'factor')):
            raise ValueError(f'Missing label/trait/factor for {key}')
        text = row['label'].strip()
        factors.append({'index': index, 'factor_id': key, 'trait': row['trait'], 'factor': row['factor'],
                        'label': row['label'], 'input_text': text, 'input_sha256': text_hash(text), 'metadata': row})
    graph_data = read_graph(graph, factors)
    summary = read_json(paths['analysis_summary.json']) if 'analysis_summary.json' in paths else {}
    counts = {'factors': len(factors), 'genes': len(gene_axis), 'loadings': int(matrix.nnz),
              'traits': len({r['trait'] for r in factors}),
              'unique_labels': len({r['input_sha256'] for r in factors}),
              'graph_nodes': len(graph_data['nodes']) if graph_data else 0,
              'graph_edges': sum(len(n.get('children', [])) for n in graph_data['nodes']) if graph_data else 0}
    for field in ('factors', 'genes', 'traits'):
        if field in summary and summary[field] != counts[field]:
            raise ValueError(f'analysis_summary {field} disagrees with input data')
    identity = {'format': FORMAT, 'namespace': source_namespace, 'source_version': source_version,
                'sources': {k: v['sha256'] for k, v in source_files.items()}}
    manifest = {**identity, 'import_id': text_hash(canonical(identity)), 'source_files': source_files,
                'counts': counts, 'template': TEMPLATE, 'source_summary': summary,
                'loading_semantics': 'capped [0,1], not normalized, not calibrated probabilities',
                'graph_edge_semantics': 'parent-to-child hierarchy containment, not causal or semantic edges'}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.eaggl-', dir=output.parent))
    try:
        write_json(temporary / 'factors.json', factors)
        write_json(temporary / 'genes.json', gene_axis)
        sparse.save_npz(temporary / 'loadings.npz', matrix)
        write_json(temporary / 'graph.json', graph_data)
        artifacts = ['factors.json', 'genes.json', 'loadings.npz', 'graph.json']
        for name in ('capped_entries.tsv.gz', 'capped_factor_list.tsv', 'analysis_summary.json'):
            if name in paths:
                shutil.copyfile(paths[name], temporary / name); artifacts.append(name)
        manifest['artifacts'] = {name: file_hash(temporary / name) for name in artifacts}
        for key, path in paths.items():
            if file_hash(path) != source_files[key]['sha256']:
                raise ValueError(f'Source changed during capture: {path.name}')
        write_json(temporary / 'manifest.json', manifest)
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def open_capture(output):
    output = Path(output)
    manifest = read_json(output / 'manifest.json')
    if manifest.get('format') != FORMAT:
        raise ValueError('Unsupported capture format')
    required = {'factors.json', 'genes.json', 'loadings.npz', 'graph.json'}
    if not required.issubset(manifest.get('artifacts', {})):
        raise ValueError('Capture manifest is missing required artifact checksums')
    identity = {k: manifest[k] for k in ('format', 'namespace', 'source_version', 'sources')}
    if text_hash(canonical(identity)) != manifest['import_id']:
        raise ValueError('Invalid import identity')
    for name, checksum in manifest['artifacts'].items():
        if Path(name).name != name or file_hash(output / name) != checksum:
            raise ValueError(f'Capture checksum mismatch: {name}')
    return manifest, read_json(output / 'factors.json'), read_json(output / 'genes.json'), sparse.load_npz(output / 'loadings.npz'), read_json(output / 'graph.json')
