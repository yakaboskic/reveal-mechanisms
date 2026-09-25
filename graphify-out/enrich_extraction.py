"""Local Graphify build adapter: JSON pointers, file coverage, and schema links.

Run with the interpreter in .graphify_python after AST/semantic extraction.
No credentials, network requests, or database access. Inputs are build artifacts.
"""
from pathlib import Path
import ast
import collections
import json
import re

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'graphify-out'


def ident(value):
    return re.sub(r'[^a-z0-9_]', '_', value.lower()).strip('_')


def file_id(path):
    return '_'.join(ident(part) for part in Path(path).with_suffix('').parts)


def build():
    detection = json.loads((OUT / '.graphify_detect.json').read_text())
    structural = json.loads((OUT / '.graphify_ast.json').read_text())
    semantic = json.loads((OUT / '.graphify_semantic.json').read_text())
    nodes = {}
    edges = []
    fixes = collections.Counter()
    all_files = {str(Path(f).relative_to(ROOT)): category
                 for category, files in detection['files'].items() for f in files}
    config = 'services/backend/pyproject.toml'
    if (ROOT / config).exists() and config not in all_files:
        all_files[config] = 'code'
        detection['files']['code'].append(str(ROOT / config))
        detection['total_files'] += 1
        detection['total_words'] += len((ROOT / config).read_text().split())
    file_ids = {file_id(f): f for f in all_files}

    def relative(source):
        p = Path(source)
        return str(p.relative_to(ROOT)) if p.is_absolute() and p.is_relative_to(ROOT) else str(p)

    def node(nid, label, source, kind='concept', **attrs):
        nodes.setdefault(nid, dict(id=nid, label=label, source_file=source,
                                   file_type=kind, source_location=None, **attrs))
        return nid

    def edge(source, target, relation, file, location=None, **attrs):
        if source == target:
            return
        edges.append(dict(source=source, target=target, relation=relation,
                          confidence='EXTRACTED', confidence_score=1.0,
                          source_file=file, source_location=location, weight=1.0,
                          **attrs))

    # Graphify's generic JSON walk merges repeated property names and skips many
    # documents. Replace only that pass with named definitions + exact pointers.
    for n in structural['nodes']:
        if not n.get('source_file', '').endswith('.json'):
            nodes[n['id']] = dict(n)
    json_ids = {n['id'] for n in structural['nodes'] if n.get('source_file', '').endswith('.json')}
    for e in structural['edges']:
        if (not e.get('source_file', '').endswith('.json') and e['target'] in json_ids
                and e.get('context') == 'call'):
            # Graphify resolves common Python variable names (PATHS, FILES,
            # version, Path, null) to unrelated JSON property names. These six
            # cross-language matches were checked against their source lines.
            fixes['rejected_cross_language_json_symbol_matches'] += 1
        elif not e.get('source_file', '').endswith('.json'):
            edges.append(dict(e))

    for file, category in all_files.items():
        node(file_id(file), Path(file).name, file,
             'code' if category == 'code' else 'document', entity_type='file')
        nodes[file_id(file)].setdefault('source_files', []).append(file)

    for file in sorted(f for f in all_files if f.endswith('.json')):
        data = json.loads((ROOT / file).read_text())
        base = file_id(file)
        pointers = {'#': base}
        if not isinstance(data, dict):
            continue
        sections = [(key, data.get(key, {})) for key in ('$defs', 'definitions')]
        if isinstance(data.get('components'), dict):
            sections += [(f'components/{key}', value) for key, value in data['components'].items()]
        for section, values in sections:
            if not isinstance(values, dict):
                continue
            for name, value in values.items():
                nid = base + '_' + ident(section + '_' + name)
                pointer = '#/' + section + '/' + name.replace('~', '~0').replace('/', '~1')
                pointers[pointer] = node(nid, name, file, 'code', json_pointer=pointer,
                                         entity_type='schema_definition')
                edge(base, nid, 'defines', file, pointer)
        paths = data.get('paths', {})
        for path, methods in (paths.items() if isinstance(paths, dict) else []):
            if not isinstance(methods, dict):
                continue
            for method, value in methods.items():
                if method not in {'get', 'post', 'put', 'patch', 'delete', 'options', 'head'}:
                    continue
                name = value.get('operationId', f'{method} {path}')
                pointer = '#/paths/' + path.replace('~', '~0').replace('/', '~1') + '/' + method
                nid = base + '_operation_' + ident(name)
                pointers[pointer] = node(nid, name, file, 'code', json_pointer=pointer,
                                         entity_type='api_operation')
                edge(base, nid, 'defines', file, pointer)

        def walk(value, pointer='#', owner=base):
            owner = pointers.get(pointer, owner)
            if isinstance(value, dict):
                ref = value.get('$ref')
                if isinstance(ref, str):
                    if ref in pointers:
                        edge(owner, pointers[ref], 'references', file, pointer + '/$ref')
                    elif not ref.startswith('#'):
                        target_file = ref.split('#')[0]
                        target = relative((ROOT / file).parent / target_file)
                        target = str(Path(target))
                        if target in all_files:
                            edge(owner, file_id(target), 'references', file, pointer + '/$ref')
                        else:
                            fixes['unresolved_external_json_refs'] += 1
                    else:
                        fixes['unresolved_local_json_refs'] += 1
                for key, item in value.items():
                    walk(item, pointer + '/' + key.replace('~', '~0').replace('/', '~1'), owner)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    walk(item, pointer + '/' + str(i), owner)
        walk(data)

    # Merge host-agent semantics after deterministic source nodes.
    for n in semantic['nodes']:
        n = dict(n)
        n['source_file'] = relative(n['source_file'])
        if n['id'] not in nodes:
            nodes[n['id']] = n
        elif n['id'] in file_ids and n['source_file'] == file_ids[n['id']]:
            nodes[n['id']].update(n)
        elif n.get('rationale'):
            nodes[n['id']]['rationale'] = n['rationale']
    edges.extend(dict(e, source_file=relative(e['source_file'])) for e in semantic['edges'])

    # Match SQL FK targets across migrations, and local imports by unique module.
    tables = {n['label']: nid for nid, n in nodes.items()
              if n.get('source_file', '').endswith('.sql') and nid not in file_ids}
    modules = collections.defaultdict(list)
    for file in all_files:
        if file.endswith('.py'):
            modules[ident(Path(file).stem)].append(file_id(file))
    for e in edges:
        target = e['target']
        if target not in nodes:
            table = next((name for name in sorted(tables, key=len, reverse=True)
                          if target.endswith('_' + name)), None)
            if e.get('source_file', '').endswith('.sql') and table:
                e['target'] = tables[table]
                fixes['resolved_sql_foreign_keys'] += 1
            elif e['relation'] in {'imports', 'imports_from', 'depends_on'}:
                if len(modules.get(target, [])) == 1:
                    e['target'] = modules[target][0]
                    fixes['resolved_local_imports'] += 1
                else:
                    node(target, target.removeprefix('ref_'), e['source_file'],
                         entity_type='external_reference', external=True)
                    fixes['external_import_references'] += 1
        if e.get('confidence') == 'EXTRACTED':
            e['confidence_score'] = 1.0

    # Keep explicitly linked, excluded artifacts as reference-only nodes. Their
    # contents are not read or indexed (e.g. generated viewers and data bundles).
    unresolved = {e['target'] for e in edges if e['target'] not in nodes}
    for file in {e['source_file'] for e in edges if e['target'] in unresolved}:
        if file not in all_files:
            continue
        text = (ROOT / file).read_text()
        links = re.findall(r'\]\(([^)\s]+)', text)
        links += re.findall(r'`([^`\n]+)`', text)
        for link in links:
            link = link.split('#')[0]
            if not link or '://' in link:
                continue
            for base in ((ROOT / file).parent, ROOT):
                candidate = (base / link).resolve()
                if (candidate.is_relative_to(ROOT) and candidate.is_file()
                        and candidate.suffix in {'.json', '.md', '.html', '.yaml', '.yml', '.sql', '.py'}):
                    rel = str(candidate.relative_to(ROOT))
                    nid = file_id(rel)
                    if nid in unresolved:
                        node(nid, candidate.name + ' (reference only)', rel, 'document',
                             entity_type='referenced_artifact', extraction_scope='reference_only')
                        fixes['resolved_excluded_artifact_references'] += 1
                        unresolved.remove(nid)

    # Literal table references and filenames bridge code to migrations/docs.
    # A reference does not assert a read/write behavior or live deployment state.
    for file in all_files:
        text = (ROOT / file).read_text()
        for quoted in re.finditer(r"[`\"']([^`\"'\n]{1,200})[`\"']", text):
            value = quoted.group(1)
            if value in all_files:
                edge(file_id(file), file_id(value), 'references', file,
                     'L' + str(text[:quoted.start()].count('\n') + 1))
        if file.endswith('.py'):
            tree = ast.parse(text)
            for value in ast.walk(tree):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    for table in tables:
                        if re.search(r'\b' + re.escape(table) + r'\b', value.value):
                            edge(file_id(file), tables[table], 'references', file,
                                 'L' + str(value.lineno))
        if file.startswith('api/examples/') and file.endswith('.json'):
            value = json.loads(text)
            if isinstance(value, dict) and isinstance(value.get('operation_id'), str):
                operation = 'api_openapi_operation_' + ident(value['operation_id'])
                if operation in nodes:
                    edge(file_id(file), operation, 'example_of', file, '#/operation_id')
                edge(file_id('scripts/build_openapi.py'), file_id(file), 'generates',
                     'scripts/build_openapi.py', 'L776-L778')

    # Bind every local source entity to its source artifact for complete browsing.
    existing_pairs = {(e['source'], e['target']) for e in edges}
    for nid, n in list(nodes.items()):
        file = n.get('source_file')
        if file in all_files and nid != file_id(file) and not n.get('external'):
            # Referenced file nodes have their own identity and shouldn't be
            # declared as definitions of the document that mentions them.
            if nid not in file_ids and (file_id(file), nid) not in existing_pairs:
                edge(file_id(file), nid, 'contains', file, n.get('source_location'))

    # Keep evidence for repeated relations, while removing identical records.
    seen = set()
    deduped = []
    for e in edges:
        signature = json.dumps(e, sort_keys=True)
        if signature not in seen:
            seen.add(signature)
            deduped.append(e)
    fixes['exact_duplicate_records_removed'] = len(edges) - len(deduped)
    result = dict(nodes=list(nodes.values()), edges=deduped,
                  hyperedges=semantic.get('hyperedges', []), input_tokens=0,
                  output_tokens=0, token_usage_status='unavailable_from_host_agent')
    (OUT / '.graphify_extract.json').write_text(json.dumps(result, indent=2))
    (OUT / '.graphify_detect.json').write_text(json.dumps(detection, indent=2))
    coverage = {f: {'category': category,
                    'node_count': sum(n.get('source_file') == f for n in nodes.values()),
                    'file_node': file_id(f)} for f, category in sorted(all_files.items())}
    (OUT / 'SOURCE_COVERAGE.json').write_text(json.dumps(coverage, indent=2))
    (OUT / 'BUILD_ADAPTER_AUDIT.json').write_text(json.dumps(dict(fixes), indent=2))
    print(f'Enriched extraction: {len(nodes)} nodes, {len(deduped)} edges, {len(all_files)} files')
    print(json.dumps(dict(fixes), indent=2))


if __name__ == '__main__':
    build()
