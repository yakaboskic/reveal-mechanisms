#!/usr/bin/env python3
"""Collect all advertised model-scoped CFDE GeneSets, mint DAPPER IDs, load MySQL.

The initial profile is catalog identity + import provenance. It does not claim
complete membership or reconstruct the original scientific generation activity.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import getpass
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import zlib

from pull_cfde import BASE, fetch, write_json

ROOT = Path(__file__).resolve().parents[1]
CATALOG_URL = BASE + '/api/bio/keys/pigean-gene-set/2'
PROFILE = 'cfde-geneset-catalog-v1'
_identity = _sv = _validate = _activity_id = None


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def file_hash(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(part)
    return result.hexdigest()


def read_json(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt') as stream:
        return json.load(stream)


def rows(path):
    with gzip.open(path, 'rt') as stream:
        for line in stream:
            yield json.loads(line)


def selected_keys(catalog, model):
    if not isinstance(catalog.get('keys'), list):
        raise ValueError('Missing source key catalog')
    keys = []
    for entry in catalog['keys']:
        if not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(x, str) and x for x in entry):
            raise ValueError('Unexpected gene-set/model key shape')
        if entry[1] == model:
            keys.append(entry[0])
    if not keys:
        raise ValueError('Model has no advertised gene-set keys')
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate model-scoped catalog keys')
    return sorted(keys)


def collect(output):
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'geneset-keys.json.gz'
    if path.exists():
        return path
    plain = output / 'geneset-keys.json'
    if not plain.exists():
        write_json(plain, fetch(CATALOG_URL))
    tmp = path.with_suffix('.tmp')
    with plain.open('rb') as source, gzip.open(tmp, 'wb', compresslevel=6) as target:
        shutil.copyfileobj(source, target)
    tmp.replace(path)
    plain.unlink()  # Only the redundant uncompressed capture created by this importer.
    if not (output / 'indexes.json').exists():
        write_json(output / 'indexes.json', fetch(BASE + '/api/bio/indexes'))
    return path


def freeze_dapper(source, output):
    target = output / 'dapper'
    manifest_path = target / 'snapshot.json'
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        for rel, expected in manifest['files'].items():
            if file_hash(target / rel) != expected:
                raise ValueError('Pinned DAPPER snapshot changed: ' + rel)
        return target, manifest
    paths = sorted((source / 'schema').glob('*.yaml')) + [
        source / 'schema/identity/dapper_identity.py', source / 'schema/identity/test_vectors.json']
    if (source / 'LICENSE').exists():
        paths.append(source / 'LICENSE')
    files = {}
    for path in paths:
        rel = path.relative_to(source)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        files[str(rel)] = file_hash(dest)
    # Reject a mixed snapshot if a co-developed schema changed during capture.
    if any(file_hash(source / rel) != sha for rel, sha in files.items()):
        raise ValueError('DAPPER changed during capture; use a fresh output directory')
    manifest = {'base_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                'working_tree_modified': bool(subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--', 'schema'], text=True).strip()),
                'files': files, 'snapshot_hash': digest(canonical(files))}
    write_json(manifest_path, manifest)
    return target, manifest


def identity_module(snapshot):
    sys.dont_write_bytecode = True
    path = snapshot / 'schema/identity/dapper_identity.py'
    spec = importlib.util.spec_from_file_location('reveal_pinned_dapper_identity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module.load_schema(snapshot / 'schema/dapper.yaml')


def metadata_record(key, model, activity_id):
    # These are display transformations only; term and aliases retain exact bytes.
    name = ' | '.join(' / '.join(part.replace('_', ' ') for part in segment.split('__'))
                      for segment in key.split('___'))
    record = {'name': name, 'member_type': 'gene', 'term': key,
              'alternate_identifier': ['gene_set:' + key, 'cfde:' + model + ':gene_set:' + key],
              'was_generated_by': activity_id,
              'was_derived_from': [CATALOG_URL]}
    prefixes = {segment.split('__', 1)[0] for segment in key.split('___') if '__' in segment}
    if len(prefixes) == 1 and all('__' in segment for segment in key.split('___')):
        record['term_prefix'] = next(iter(prefixes))
    return record


def init_encoder(snapshot, validation_schema, activity_id):
    global _identity, _sv, _validate, _activity_id
    from jsonschema import validators
    _identity, _sv = identity_module(Path(snapshot))
    schema = read_json(Path(validation_schema))
    cls = validators.validator_for(schema)
    _validate = cls(schema).validate
    _activity_id = activity_id


def encode_batch(work):
    keys, model = work
    lines = []
    for key in keys:
        node = metadata_record(key, model, _activity_id)
        node['id'] = _identity.compute_id(node, 'GeneSet', _sv)
        _validate(node)
        lines.append(canonical({'gene_set': node, 'source_key': key, 'model': model, 'node_id': 'gene_set:' + key}))
    return lines


def encode(args):
    from linkml.generators.jsonschemagen import JsonSchemaGenerator
    from jsonschema import validators
    output = args.output
    catalog_path = collect(output)
    keys = selected_keys(read_json(catalog_path), args.model)
    snapshot, dependency = freeze_dapper(args.dapper_source, output)
    module, sv = identity_module(snapshot)
    activity = {'name': 'CFDE GeneSet catalog encoding', 'activity_type': 'CFDEGeneSetCatalogImport',
                'description': 'Encodes advertised CFDE named gene-set identities; not the original biological gene-set creation activity.',
                'command': PROFILE + ' model=' + args.model}
    activity['id'] = module.compute_id(activity, 'Activity', sv)
    for class_name in ('GeneSet', 'Activity'):
        schema = json.loads(JsonSchemaGenerator(str(snapshot / 'schema/dapper.yaml'), top_class=class_name, not_closed=False).serialize())
        write_json(output / (class_name + '.schema.json'), schema)
        if class_name == 'Activity':
            validators.validator_for(schema)(schema).validate(activity)
    write_json(output / 'activity.json', activity)
    shutil.copyfile(Path(__file__), output / 'importer-at-encoding.py')
    basis = {'profile': PROFILE, 'model': args.model, 'catalog_sha256': file_hash(catalog_path),
             'dapper_snapshot_hash': dependency['snapshot_hash'], 'script_sha256': file_hash(Path(__file__)),
             'expected_rows': len(keys)}
    manifest = {**basis, 'import_id': digest(canonical(basis)), 'complete': False,
                'captured_at': datetime.now(timezone.utc).isoformat(), 'catalog_url': CATALOG_URL,
                'coverage': {'enumeration': 'all advertised pigean-gene-set/2 keys for the requested model',
                             'membership': 'not_loaded', 'construction_provenance': 'not_loaded',
                             'metadata': 'source identity, display name, parsed namespace, import activity only'},
                'dapper': dependency, 'activity_id': activity['id'], 'encoded_rows': 0}
    write_json(output / 'manifest.json', manifest)
    path = output / 'records.jsonl.gz'
    tmp = output / 'records.jsonl.gz.tmp'
    tasks = [(keys[i:i + 1000], args.model) for i in range(0, len(keys), 1000)]
    print(f'Encoding {len(keys):,} GeneSets with {args.workers} workers', flush=True)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=init_encoder,
                             initargs=(str(snapshot), str(output / 'GeneSet.schema.json'), activity['id'])) as pool:
        with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=5) as stream:
            for batch in pool.map(encode_batch, tasks):
                stream.write('\n'.join(batch) + '\n')
                manifest['encoded_rows'] += len(batch)
                if manifest['encoded_rows'] % 20000 == 0:
                    print(f"Encoded {manifest['encoded_rows']:,}/{len(keys):,}", flush=True)
    tmp.replace(path)
    manifest.update(complete=True, records_sha256=file_hash(path), activity_sha256=file_hash(output / 'activity.json'))
    write_json(output / 'manifest.json', manifest)
    sample = next(rows(path))['gene_set']
    write_json(output / 'example.dapper.json', {'activities': [activity], 'gene_sets': [sample]})
    print(f"Encoded and schema-validated {manifest['encoded_rows']:,} GeneSets. Import {manifest['import_id']}", flush=True)


def check_export(output):
    manifest = read_json(output / 'manifest.json')
    if not manifest['complete'] or manifest['encoded_rows'] != manifest['expected_rows']:
        raise ValueError('Refusing an incomplete export')
    if file_hash(output / 'records.jsonl.gz') != manifest['records_sha256']:
        raise ValueError('Export checksum mismatch')
    if file_hash(output / 'activity.json') != manifest['activity_sha256']:
        raise ValueError('Activity checksum mismatch')
    return manifest


def validate_db_name(name):
    if not re.fullmatch(r'cyaka_[a-z0-9_]+', name):
        raise ValueError('Database must use the literal cyaka_ prefix and lowercase identifier characters')


def store_objects(cursor, objects):
    values = [(obj['id'], cls, 'DAPPER-ID-1', digest(canonical(obj)), canonical(obj)) for cls, obj in objects]
    cursor.executemany('INSERT INTO dapper_objects (id,class_name,identity_profile,payload_sha256,payload) VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=id', values)
    expected = {v[0]: v[3] for v in values}
    cursor.execute('SELECT id,payload_sha256 FROM dapper_objects WHERE id IN (' + ','.join(['%s'] * len(expected)) + ')', tuple(expected))
    actual = dict(cursor.fetchall())
    if actual != expected:
        raise ValueError('Existing DAPPER ID conflicts with imported content; no overwrite permitted')


def compressed_rows(values):
    """MySQL COMPRESS wire format: little-endian length followed by zlib data."""
    raw = canonical(values).encode('utf-8')
    # Bound decompression/memory independently of compression ratio.
    if len(raw) > 48 * 1024 * 1024:
        raise ValueError('Bulk batch exceeds 48 MiB; reduce --batch-size')
    return len(raw).to_bytes(4, 'little') + zlib.compress(raw, level=6)


def bulk_stage(cursor, table, columns, values):
    """Send compressed JSON over verified TLS, then stage into typed temp tables."""
    # table/columns are fixed application constants, never source identifiers.
    fields = ','.join(f"{name} LONGTEXT PATH '$[{index}]' ERROR ON EMPTY ERROR ON ERROR"
                      for index, name in enumerate(columns.split(',')))
    packed = compressed_rows(values)
    cursor.execute('DELETE FROM ' + table)
    sql = ('INSERT INTO ' + table + ' (' + columns + ') SELECT ' + columns +
           " FROM JSON_TABLE(CONVERT(UNCOMPRESS(%s) USING utf8mb4), '$[*]' COLUMNS (" + fields + ')) AS staged')
    cursor.execute(sql, (packed,))
    cursor.execute('SHOW COUNT(*) WARNINGS')
    if cursor.fetchone()[0]:
        raise ValueError('Bulk staging reported conversion warnings; batch rejected')
    cursor.execute('SELECT COUNT(*) FROM ' + table)
    if cursor.fetchone()[0] != len(values):
        raise ValueError('Bulk staging count mismatch')


def bulk_store(cursor, batch, aliases):
    objects = [(row['gene_set']['id'], 'GeneSet', 'DAPPER-ID-1', digest(canonical(row['gene_set'])),
                canonical(row['gene_set'])) for row in batch]
    bulk_stage(cursor, 'reveal_stage_objects', 'id,class_name,identity_profile,payload_sha256,payload', objects)
    bulk_stage(cursor, 'reveal_stage_aliases', 'import_id,node_id_sha256,model,source_key,node_id,dapper_id,provenance', aliases)
    cursor.execute('SELECT COUNT(*) FROM reveal_stage_objects s JOIN dapper_objects d ON s.id=d.id WHERE s.payload_sha256<>d.payload_sha256')
    if cursor.fetchone()[0]:
        raise ValueError('Existing DAPPER ID conflicts with staged content')
    cursor.execute('INSERT INTO dapper_objects (id,class_name,identity_profile,payload_sha256,payload) SELECT s.id,s.class_name,s.identity_profile,s.payload_sha256,s.payload FROM reveal_stage_objects s LEFT JOIN dapper_objects d ON s.id=d.id WHERE d.id IS NULL')
    cursor.execute('INSERT INTO cfde_gene_set_aliases (import_id,node_id_sha256,model,source_key,node_id,dapper_id,provenance) SELECT import_id,node_id_sha256,model,source_key,node_id,dapper_id,provenance FROM reveal_stage_aliases')


def load(args):
    import pymysql
    manifest = check_export(args.output)
    if args.model != manifest['model']:
        raise ValueError('Requested load model differs from the exported model')
    validate_db_name(args.database)
    if not args.apply:
        print(f"Dry run: {manifest['expected_rows']:,} GeneSets would load into {args.database}; use --apply to write.")
        return
    password = getpass.getpass('MySQL password (not saved): ')
    connection = pymysql.connect(host=args.host, user=args.user, password=password,
        ssl=ssl.create_default_context(cafile=args.ca_file), connect_timeout=15,
        read_timeout=120, write_timeout=120, charset='utf8mb4', autocommit=False, binary_prefix=True)
    del password
    report = {'import_id': manifest['import_id'], 'database': args.database, 'complete': False,
              'started_at': datetime.now(timezone.utc).isoformat(), 'expected_rows': manifest['expected_rows'],
              'loader_script_sha256': file_hash(Path(__file__)), 'mode': 'compressed-json-staging' if args.bulk else 'batched-inserts'}
    report_path = args.output / 'database-load.json'
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            report['tls_cipher'] = cursor.fetchone()[1]
            if not report['tls_cipher']:
                raise ValueError('Verified TLS is required')
            if args.create_database:
                cursor.execute(f'CREATE DATABASE IF NOT EXISTS `{args.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
            cursor.execute(f'USE `{args.database}`')
            migration = (ROOT / 'schema/migrations/001_gene_set_inventory.sql').read_text()
            migration = '\n'.join(line for line in migration.splitlines() if not line.lstrip().startswith('--'))
            for statement in migration.split(';'):
                if statement.strip():
                    cursor.execute(statement)
            if args.bulk:
                cursor.execute('SELECT @@max_allowed_packet')
                if cursor.fetchone()[0] < 64 * 1024 * 1024:
                    raise ValueError('Bulk mode requires max_allowed_packet >= 64 MiB; omit --bulk')
                cursor.execute('CREATE TEMPORARY TABLE reveal_stage_objects LIKE dapper_objects')
                cursor.execute('CREATE TEMPORARY TABLE reveal_stage_aliases LIKE cfde_gene_set_aliases')
            cursor.execute('INSERT INTO gene_set_imports (import_id,model,status,expected_rows,manifest) VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE import_id=import_id',
                           (manifest['import_id'], args.model, 'loading', manifest['expected_rows'], canonical(manifest)))
            cursor.execute('SELECT loaded_rows,status FROM gene_set_imports WHERE import_id=%s FOR UPDATE', (manifest['import_id'],))
            loaded, status = cursor.fetchone()
            report['resumed_at_rows'] = loaded
            store_objects(cursor, [('Activity', read_json(args.output / 'activity.json'))])
            connection.commit()
            print(f'Resuming at {loaded:,}/{manifest["expected_rows"]:,} rows', flush=True)
            batch = []
            def commit_batch():
                nonlocal loaded
                with connection.cursor() as cur:
                    cur.execute('SELECT loaded_rows FROM gene_set_imports WHERE import_id=%s FOR UPDATE', (manifest['import_id'],))
                    if cur.fetchone()[0] != loaded:
                        raise ValueError('Concurrent importer advanced this snapshot; restart to resume')
                    aliases = []
                    for row in batch:
                        if row['model'] != manifest['model'] or row['node_id'] != 'gene_set:' + row['source_key']:
                            raise ValueError('Model/alias mismatch')
                        provenance = {'catalog_url': manifest['catalog_url'], 'catalog_sha256': manifest['catalog_sha256'],
                                      'dapper_snapshot_hash': manifest['dapper_snapshot_hash'],
                                      'activity_id': manifest['activity_id'], 'coverage': manifest['coverage']}
                        aliases.append((manifest['import_id'], digest(row['node_id']), row['model'], row['source_key'], row['node_id'], row['gene_set']['id'], canonical(provenance)))
                    if args.bulk:
                        bulk_store(cur, batch, aliases)
                    else:
                        store_objects(cur, [('GeneSet', row['gene_set']) for row in batch])
                        cur.executemany('INSERT INTO cfde_gene_set_aliases (import_id,node_id_sha256,model,source_key,node_id,dapper_id,provenance) VALUES (%s,%s,%s,%s,%s,%s,%s)', aliases)
                    loaded += len(batch)
                    cur.execute('UPDATE gene_set_imports SET loaded_rows=%s WHERE import_id=%s', (loaded, manifest['import_id']))
                connection.commit()
                batch.clear()
                report['loaded_rows'] = loaded
                if args.bulk or loaded % 20000 == 0 or loaded == manifest['expected_rows']:
                    print(f'Loaded {loaded:,}/{manifest["expected_rows"]:,}', flush=True)
            for index, row in enumerate(rows(args.output / 'records.jsonl.gz')):
                if index < loaded:
                    continue
                batch.append(row)
                if len(batch) >= args.batch_size:
                    commit_batch()
            if batch:
                commit_batch()
            cursor.execute('SELECT COUNT(*) FROM cfde_gene_set_aliases WHERE import_id=%s', (manifest['import_id'],))
            count = cursor.fetchone()[0]
            if count != manifest['expected_rows'] or loaded != count:
                raise ValueError('Database row-count verification failed')
            cursor.execute("UPDATE gene_set_imports SET status='complete' WHERE import_id=%s", (manifest['import_id'],))
            connection.commit()
            report.update(complete=True, loaded_rows=count, finished_at=datetime.now(timezone.utc).isoformat())
            print(f'Verified {count:,} persisted GeneSet aliases in {args.database}', flush=True)
    except Exception:
        report['failure'] = 'Import interrupted; committed batches can be resumed'
        if connection.open:
            connection.rollback()
        raise
    finally:
        write_json(report_path, report)
        if connection.open:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['encode', 'load'])
    parser.add_argument('--output', type=Path, default=ROOT / 'data/cfde-genesets/2026-09-24')
    parser.add_argument('--model', default='cfde-inc-v2')
    parser.add_argument('--dapper-source', type=Path, default=Path.home() / 'src/research/dapper')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--batch-size', type=int, default=1000)
    parser.add_argument('--host', default='aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com')
    parser.add_argument('--user', default='cyaka')
    parser.add_argument('--database', default='cyaka_reveal_mechanisms')
    parser.add_argument('--ca-file', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--create-database', action='store_true')
    parser.add_argument('--bulk', action='store_true', help='Use compressed JSON batches with MySQL 8 temporary staging tables')
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or not 1 <= args.batch_size <= (50000 if args.bulk else 2000):
        parser.error('workers must be 1..8; batch-size 1..2000, or 1..50000 with --bulk')
    if args.command == 'load' and args.apply and args.ca_file is None:
        parser.error('--ca-file is required for verified-TLS database writes')
    (encode if args.command == 'encode' else load)(args)


if __name__ == '__main__':
    main()
