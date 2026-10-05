#!/usr/bin/env python3
"""Import a pinned gnomAD constraint TSV or activate a ready import; dry-run by default."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import csv
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.mysql_database import insert_batch
from reveal_backend.repository import application_prefix
from reveal_backend.runtime_config import mysql_connection

POLICY = 'gnomad-ensembl-mane-canonical-exact-symbol-v2'
SOURCE_URL = 'https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/constraint/gnomad.v4.1.constraint_metrics.tsv'
MIGRATION = ROOT / 'schema/migrations/009_gnomad_constraints.sql'
METRICS = {'pli': 'lof.pLI', 'loeuf': 'lof.oe_ci.upper', 'mis_z': 'mis.z_score', 'lof_oe': 'lof.oe'}
REQUIRED = {'gene', 'gene_id', 'transcript', 'canonical', 'mane_select', 'constraint_flags', *METRICS.values()}
TEXT = {'gene', 'gene_id', 'transcript', 'level', 'transcript_type', 'chromosome'}
TRANSCRIPT_COLUMNS = ('source_row', 'gene_symbol', 'gene_id', 'transcript_id', 'namespace', 'canonical', 'mane_select', *METRICS, 'constraint_flags', 'raw_metrics')
GENE_COLUMNS = ('gene_symbol', 'selection_status', 'selection_reason', 'gene_id', 'transcript_id', 'namespace', 'source_row', *METRICS, 'constraint_flags', 'candidate_gene_ids')
JSON_COLUMNS = {'constraint_flags', 'raw_metrics', 'candidate_gene_ids'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''): result.update(part)
    return result.hexdigest()


def hash_record(hasher, record):
    hasher.update((canonical(record) + '\n').encode())


def progress(message):
    print(message, file=sys.stderr, flush=True)


def parse_row(raw, source_row):
    if None in raw or any(value is None for value in raw.values()):
        raise ValueError(f'Row {source_row}: field count differs from the header')
    data = {}
    for key, value in raw.items():
        if key in ('canonical', 'mane_select'):
            if value not in ('true', 'false'): raise ValueError(f'Row {source_row}: invalid {key}')
            data[key] = value == 'true'
        elif key == 'constraint_flags':
            try: flags = json.loads(value)
            except ValueError: raise ValueError(f'Row {source_row}: malformed constraint_flags') from None
            if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
                raise ValueError(f'Row {source_row}: constraint_flags must be a list of strings')
            data[key] = flags
        elif value == 'NA': data[key] = None
        elif key in TEXT: data[key] = value
        else:
            try: numeric = float(value)
            except ValueError: raise ValueError(f'Row {source_row}: {key} is not a number or NA') from None
            if not math.isfinite(numeric): raise ValueError(f'Row {source_row}: nonfinite {key}')
            # Signed zero is not a distinct scientific value in DOUBLE columns.
            # Its original spelling is still preserved in raw_metrics below.
            data[key] = 0.0 if numeric == 0 else numeric
    gene, transcript = data['gene_id'], data['transcript']
    if isinstance(gene, str) and re.fullmatch(r'ENSG\d+(?:\.\d+)?', gene) and isinstance(transcript, str) and re.fullmatch(r'ENST\d+(?:\.\d+)?', transcript):
        namespace = 'ensembl'
    elif isinstance(gene, str) and gene.isascii() and gene.isdigit() and isinstance(transcript, str) and re.fullmatch(r'[NX][MR]_\d+(?:\.\d+)?', transcript):
        namespace = 'refseq'
    else: raise ValueError(f'Row {source_row}: unsupported or mismatched gene/transcript identifiers')
    if any(len(value) > 64 for value in (gene, transcript)) or (data['gene'] is not None and (not data['gene'] or len(data['gene']) > 128 or data['gene'] != data['gene'].strip())):
        raise ValueError(f'Row {source_row}: identifier exceeds its database boundary')
    row = {'source_row': source_row, 'gene_symbol': data['gene'], 'gene_id': gene, 'transcript_id': transcript,
           'namespace': namespace, 'canonical': data['canonical'], 'mane_select': data['mane_select'],
           **{name: data[column] for name, column in METRICS.items()},
           'constraint_flags': data['constraint_flags'],
           # Native MySQL JSON can round tiny floating-point probabilities by
           # one ULP even when the DOUBLE columns round-trip exactly. Preserve
           # the TSV lexemes here; hashes remain exact, without any tolerance.
           'raw_metrics': {key: None if value == 'NA' else value for key, value in raw.items()}}
    if row['pli'] is not None and not 0 <= row['pli'] <= 1: raise ValueError(f'Row {source_row}: pLI is outside [0,1]')
    if any(row[key] is not None and row[key] < 0 for key in ('loeuf', 'lof_oe')): raise ValueError(f'Row {source_row}: negative LoF ratio')
    return row


def rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or not REQUIRED <= set(reader.fieldnames):
            raise ValueError('Missing or duplicate required gnomAD constraint columns')
        for source_row, raw in enumerate(reader, 2): yield parse_row(raw, source_row)


@dataclass
class Capture:
    path: Path
    manifest: dict
    genes: list[dict]


def prepare(path, *, source_version='4.1', source_url=SOURCE_URL):
    path = Path(path)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}', source_version): raise ValueError('Invalid source version')
    if not source_url.startswith('https://') or len(source_url) > 4096: raise ValueError('Record an HTTPS source URL')
    checksum = file_hash(path)
    namespaces, availability = Counter(), Counter()
    symbols = defaultdict(lambda: defaultdict(set))
    gene_symbols, candidates, identities = {}, defaultdict(list), set()
    transcript_digest = hashlib.sha256()
    for row in rows(path):
        identity = row['gene_id'], row['transcript_id']
        if identity in identities: raise ValueError(f'Duplicate gene/transcript identity at row {row["source_row"]}')
        identities.add(identity)
        previous = gene_symbols.setdefault(row['gene_id'], row['gene_symbol'])
        if previous != row['gene_symbol']: raise ValueError('One source gene ID maps to multiple symbols')
        namespaces[row['namespace']] += 1
        hash_record(transcript_digest, row)
        for metric in METRICS: availability[metric + ('_missing' if row[metric] is None else '_reported')] += 1
        if row['gene_symbol'] is not None: symbols[row['gene_symbol']][row['namespace']].add(row['gene_id'])
        if row['namespace'] == 'ensembl' and (row['mane_select'] or row['canonical']):
            candidates[row['gene_id']].append({key: value for key, value in row.items() if key != 'raw_metrics'})
    if not identities: raise ValueError('Constraint TSV has no rows')
    genes, reasons = [], Counter()
    for symbol, by_namespace in sorted(symbols.items()):
        ensembl = sorted(by_namespace['ensembl'])
        result = {'gene_symbol': symbol, 'gene_id': None, 'transcript_id': None, 'namespace': None, 'source_row': None,
                  **dict.fromkeys(METRICS), 'constraint_flags': [], 'candidate_gene_ids': ensembl or sorted(by_namespace['refseq'])}
        if not ensembl: status, reason = 'no_ensembl_gene', 'refseq_only'
        elif len(ensembl) > 1: status, reason = 'ambiguous_gene', 'multiple_ensembl_gene_ids'
        else:
            result.update(gene_id=ensembl[0], namespace='ensembl')
            mane = [row for row in candidates[ensembl[0]] if row['mane_select']]
            canonical_rows = [row for row in candidates[ensembl[0]] if row['canonical']]
            preferred = mane or canonical_rows
            if len(preferred) > 1:
                status, reason = 'ambiguous_transcript', 'multiple_mane_select' if mane else 'multiple_canonical'
            elif not preferred: status, reason = 'no_primary_transcript', 'no_mane_or_canonical'
            else:
                status, reason = 'selected', 'mane_select' if mane else 'canonical'
                selected = preferred[0]
                result.update({key: selected[key] for key in ('gene_id', 'transcript_id', 'namespace', 'source_row', *METRICS)})
                result['constraint_flags'] = selected['constraint_flags']
        result.update(selection_status=status, selection_reason=reason)
        genes.append(result); reasons[status] += 1
    gene_digest = hashlib.sha256()
    for row in genes: hash_record(gene_digest, row)
    selected_metrics = Counter(metric + ('_missing' if row[metric] is None else '_reported')
                               for row in genes if row['selection_status'] == 'selected' for metric in METRICS)
    if file_hash(path) != checksum: raise ValueError('Constraint TSV changed during validation')
    basis = {'source_version': source_version, 'source_sha256': checksum, 'source_url': source_url, 'selection_policy': POLICY}
    manifest = {**basis, 'format': 'reveal.gnomad-constraints/1',
                'import_id': hashlib.sha256(canonical(basis).encode()).hexdigest(),
                'source_bytes': path.stat().st_size, 'transcript_count': len(identities), 'gene_count': len(genes),
                'namespace_counts': dict(namespaces), 'metric_counts': dict(availability), 'selection_counts': dict(reasons),
                'selected_metric_counts': dict(selected_metrics),
                'transcript_sha256': transcript_digest.hexdigest(), 'gene_sha256': gene_digest.hexdigest()}
    return Capture(path, manifest, genes)


@contextmanager
def locks(connection, names):
    acquired = []
    try:
        with connection.cursor() as cursor:
            for name in sorted(names):
                cursor.execute('SELECT GET_LOCK(%s, 0)', (name,))
                if cursor.fetchone()[0] != 1: raise RuntimeError('Another gnomAD migration/import/activation is running')
                acquired.append(name)
        yield
    finally:
        with connection.cursor() as cursor:
            for name in reversed(acquired): cursor.execute('SELECT RELEASE_LOCK(%s)', (name,))


def migrate(connection):
    # This is a separate explicit command, never hidden inside an import transaction.
    with locks(connection, ['gnomad:migration']):
        with connection.cursor() as cursor:
            sql = '\n'.join(line for line in MIGRATION.read_text().splitlines() if not line.lstrip().startswith('--'))
            for statement in sql.split(';'):
                if statement.strip(): cursor.execute(statement)
        connection.commit()
    return {'migration': MIGRATION.name, 'status': 'applied', 'data_loaded': False, 'activation_changed': False}


def encoded(record, columns):
    return tuple(canonical(record[column]) if column in JSON_COLUMNS else record[column] for column in columns)


def decoded(values, columns):
    result = dict(zip(columns, values))
    for column in JSON_COLUMNS.intersection(result):
        if isinstance(result[column], (str, bytes)): result[column] = json.loads(result[column])
    for column in ('canonical', 'mane_select'):
        if column in result: result[column] = bool(result[column])
    return result


def verify_rows(cursor, capture, batch_size):
    manifest, identity = capture.manifest, capture.manifest['import_id']
    for table, columns, key, expected_count, expected_hash in [
        ('gnomad_constraint_transcripts', TRANSCRIPT_COLUMNS, 'source_row', manifest['transcript_count'], manifest['transcript_sha256']),
        ('gnomad_gene_constraints', GENE_COLUMNS, 'gene_symbol', manifest['gene_count'], manifest['gene_sha256'])]:
        count, last, digest, next_progress = 0, None, hashlib.sha256(), 25000
        progress(f'Verifying {table}: {expected_count:,} rows')
        while True:
            after = '' if last is None else f' AND {key}>%s'
            args = (identity, batch_size) if last is None else (identity, last, batch_size)
            cursor.execute(f'SELECT {",".join(columns)} FROM {table} WHERE import_id=%s{after} ORDER BY {key} LIMIT %s', args)
            batch = cursor.fetchall()
            for values in batch:
                row = decoded(values, columns); hash_record(digest, row); last = row[key]; count += 1
            if count >= next_progress:
                progress(f'Verified {table}: {count:,}/{expected_count:,} rows')
                next_progress = (count // 25000 + 1) * 25000
            if len(batch) < batch_size: break
        if count != expected_count or digest.hexdigest() != expected_hash:
            raise ValueError(f'{table}: stored count or content digest differs from validated source '
                             f'(rows {count}/{expected_count}; sha256 {digest.hexdigest()} != {expected_hash})')
        progress(f'Verified {table}: {count:,} rows; exact content digest matches')


def activate_pointer(cursor, identity, table_prefix):
    cursor.execute('SELECT import_id FROM gnomad_constraint_active WHERE table_prefix=%s FOR UPDATE', (table_prefix,))
    active = cursor.fetchone()
    if not active:
        cursor.execute('INSERT INTO gnomad_constraint_active (table_prefix,import_id) VALUES (%s,%s)', (table_prefix, identity))
    elif active[0] != identity:
        cursor.execute('UPDATE gnomad_constraint_active SET import_id=%s,activated_at=CURRENT_TIMESTAMP WHERE table_prefix=%s', (identity, table_prefix))
    return not active or active[0] != identity


def validate_ready_manifest(identity, stored):
    if not stored: raise ValueError('The requested gnomAD import does not exist')
    metadata = dict(zip(('source_version', 'source_sha256', 'source_url', 'selection_policy', 'status', 'manifest'), stored))
    if metadata['status'] != 'ready': raise ValueError('Activation requires a ready, previously verified gnomAD import')
    manifest = metadata['manifest']
    if isinstance(manifest, (str, bytes)): manifest = json.loads(manifest)
    if not isinstance(manifest, dict) or manifest.get('format') != 'reveal.gnomad-constraints/1':
        raise ValueError('Unsupported gnomAD import manifest')
    basis = {key: metadata[key] for key in ('source_version', 'source_sha256', 'source_url', 'selection_policy')}
    if any(manifest.get(key) != value for key, value in basis.items()):
        raise ValueError('Stored gnomAD import metadata differs from its manifest')
    if (not isinstance(basis['source_version'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}', basis['source_version'])
            or not isinstance(basis['source_url'], str) or not basis['source_url'].startswith('https://') or len(basis['source_url']) > 4096
            or basis['selection_policy'] != POLICY):
        raise ValueError('Unsupported gnomAD source metadata or selection policy')
    for key in ('source_sha256', 'transcript_sha256', 'gene_sha256'):
        if not isinstance(manifest.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', manifest[key]):
            raise ValueError(f'Invalid previously verified {key} in gnomAD manifest')
    if manifest.get('import_id') != identity or hashlib.sha256(canonical(basis).encode()).hexdigest() != identity:
        raise ValueError('Stored gnomAD import identity differs from its source metadata')
    for key, minimum in (('source_bytes', 1), ('transcript_count', 1), ('gene_count', 0)):
        if type(manifest.get(key)) is not int or manifest[key] < minimum:
            raise ValueError(f'Invalid {key} in gnomAD manifest')
    if manifest['gene_count'] > manifest['transcript_count']:
        raise ValueError('Invalid gene_count in gnomAD manifest')
    return manifest


def activate(connection, identity, *, table_prefix):
    """Promote an immutable ready import using its ingestion verification receipt."""
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity): raise ValueError('Invalid gnomAD import ID')
    if not table_prefix: raise ValueError('Activation requires an explicit table prefix')
    application_prefix(table_prefix)
    with locks(connection, ['gnomad:import:' + identity[:48], 'gnomad:active:' + table_prefix]):
        try:
            with connection.cursor() as cursor:
                cursor.execute('START TRANSACTION')
                cursor.execute('SELECT source_version,source_sha256,source_url,selection_policy,status,manifest FROM gnomad_constraint_imports WHERE import_id=%s FOR UPDATE', (identity,))
                manifest = validate_ready_manifest(identity, cursor.fetchone())
                for table, key in (('gnomad_constraint_transcripts', 'transcript_count'), ('gnomad_gene_constraints', 'gene_count')):
                    # The import_id prefix of each primary key bounds this count;
                    # raw provenance JSON never crosses the network on promotion.
                    cursor.execute(f'SELECT COUNT(*) FROM {table} WHERE import_id=%s', (identity,))
                    count = cursor.fetchone()[0]
                    if count != manifest[key]: raise ValueError(f'{table}: stored count {count} differs from verified manifest {manifest[key]}')
                activated = activate_pointer(cursor, identity, table_prefix)
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
    progress(f'Activated ready gnomAD import for {table_prefix}; reused ingestion digests, current row counts verified')
    return {'import_id': identity, 'status': 'ready', 'reused': True, 'activated': activated, 'table_prefix': table_prefix,
            'transcript_count': manifest['transcript_count'], 'gene_count': manifest['gene_count'],
            'verification': {'metadata_verified': True, 'row_counts_verified': True, 'content_rehashed': False,
                             'content_verification': 'previously_verified_at_ingestion',
                             'transcript_sha256': manifest['transcript_sha256'], 'gene_sha256': manifest['gene_sha256']}}


def load(connection, capture, *, table_prefix=None, batch_size=1000):
    if not 1 <= batch_size <= 5000: raise ValueError('Batch size must be between 1 and 5000')
    if table_prefix is not None:
        if not table_prefix: raise ValueError('Activation requires an explicit table prefix')
        application_prefix(table_prefix)
    manifest, identity = capture.manifest, capture.manifest['import_id']
    if file_hash(capture.path) != manifest['source_sha256']: raise ValueError('Constraint TSV changed after validation')
    names = ['gnomad:import:' + identity[:48]] + (['gnomad:active:' + table_prefix] if table_prefix else [])
    reused, activated = False, False
    with locks(connection, names):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')")
                cursor.execute('START TRANSACTION')
                cursor.execute('SELECT manifest,status FROM gnomad_constraint_imports WHERE import_id=%s', (identity,))
                previous = cursor.fetchone()
                if previous:
                    old = json.loads(previous[0]) if isinstance(previous[0], (str, bytes)) else previous[0]
                    if old != manifest or previous[1] != 'ready': raise ValueError('Existing immutable gnomAD import conflicts with this capture')
                    reused = True
                else:
                    cursor.execute('INSERT INTO gnomad_constraint_imports (import_id,source_version,source_sha256,source_url,selection_policy,status,manifest) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (identity, manifest['source_version'], manifest['source_sha256'], manifest['source_url'], POLICY, 'loading', canonical(manifest)))
                    for table, columns, records in [('gnomad_constraint_transcripts', TRANSCRIPT_COLUMNS, rows(capture.path)), ('gnomad_gene_constraints', GENE_COLUMNS, iter(capture.genes))]:
                        inserted, next_progress = 0, 25000
                        progress(f'Inserting {table}')
                        while batch := list(islice(records, batch_size)):
                            insert_batch(cursor, table, ('import_id', *columns), [(identity, *encoded(row, columns)) for row in batch])
                            inserted += len(batch)
                            if inserted >= next_progress:
                                progress(f'Inserted {table}: {inserted:,} rows (not committed)')
                                next_progress = (inserted // 25000 + 1) * 25000
                        progress(f'Inserted {table}: {inserted:,} rows (not committed)')
                if file_hash(capture.path) != manifest['source_sha256']: raise ValueError('Constraint TSV changed while loading')
                verify_rows(cursor, capture, batch_size)
                if not reused: cursor.execute("UPDATE gnomad_constraint_imports SET status='ready' WHERE import_id=%s", (identity,))
                if table_prefix: activated = activate_pointer(cursor, identity, table_prefix)
                connection.commit()
                progress('Committed verified gnomAD import' + (f'; activation scope {table_prefix}' if table_prefix else '; no activation'))
        except BaseException:
            connection.rollback()
            raise
    return {'import_id': identity, 'status': 'ready', 'reused': reused, 'activated': activated, 'table_prefix': table_prefix,
            'transcript_count': manifest['transcript_count'], 'gene_count': manifest['gene_count']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('migrate', 'load', 'activate'))
    parser.add_argument('--input', type=Path, help='Uncompressed gnomAD constraint TSV; required for load')
    parser.add_argument('--import-id', help='Existing ready import ID; required for activate')
    parser.add_argument('--source-version', default='4.1')
    parser.add_argument('--source-url', default=SOURCE_URL)
    parser.add_argument('--env-file', type=Path, help='Explicit private connection settings; no default environment file is read')
    parser.add_argument('--ca-file', type=Path, help='Verified TLS certificate override for host/container path differences')
    parser.add_argument('--table-prefix', help='Explicit environment activation target; omitted means import without activation')
    parser.add_argument('--batch-size', type=int, default=1000)
    parser.add_argument('--apply', action='store_true', help='Apply migration, transactional import or ready-import activation to the configured MySQL database')
    parser.add_argument('--report', type=Path, help='Optional local JSON receipt; contains no credentials')
    args = parser.parse_args(argv)
    if args.command == 'load' and args.input is None: parser.error('load requires --input')
    if args.command == 'migrate' and (args.input or args.table_prefix): parser.error('migrate does not load or activate a source')
    if args.command == 'activate':
        if args.input: parser.error('activate uses an existing import and does not accept --input')
        if not args.import_id or not re.fullmatch(r'[a-f0-9]{64}', args.import_id): parser.error('activate requires --import-id as 64 lowercase hexadecimal characters')
        if not args.table_prefix: parser.error('activate requires an explicit --table-prefix')
    elif args.import_id: parser.error('--import-id is only valid for activate')
    if args.table_prefix is not None:
        if not args.table_prefix: parser.error('--table-prefix must not be empty')
        application_prefix(args.table_prefix)
    if not 1 <= args.batch_size <= 5000: parser.error('--batch-size must be between 1 and 5000')
    capture = prepare(args.input, source_version=args.source_version, source_url=args.source_url) if args.command == 'load' else None
    result = {'apply': args.apply, 'command': args.command, 'migration': MIGRATION.name}
    if capture: result.update(manifest=capture.manifest, table_prefix=args.table_prefix)
    if args.command == 'activate':
        result.update(import_id=args.import_id, table_prefix=args.table_prefix,
                      verification_plan='Validate ready manifest, source identity and current row counts; reuse ingestion content digests without rehashing',
                      database_checked=False)
    if args.apply:
        if args.env_file:
            from dotenv import dotenv_values
            if not args.env_file.is_file(): raise ValueError('The selected environment file does not exist')
            # Explicit file is authoritative; shell credentials from another environment cannot leak in.
            values = dotenv_values(args.env_file, interpolate=False)
            for key in ('REVEAL_MYSQL_HOST', 'REVEAL_MYSQL_PORT', 'REVEAL_MYSQL_USER', 'REVEAL_MYSQL_DATABASE', 'REVEAL_MYSQL_PASSWORD', 'REVEAL_MYSQL_CA_FILE'):
                os.environ.pop(key, None)
                if values.get(key) is not None: os.environ[key] = values[key]
            target = values.get('REVEAL_APPLICATION_TABLE_PREFIX')
            if args.table_prefix and target != args.table_prefix: raise ValueError('Explicit activation prefix does not match the selected environment file')
        if args.ca_file: os.environ['REVEAL_MYSQL_CA_FILE'] = str(args.ca_file.resolve())
        connection = mysql_connection()
        try:
            if args.command == 'activate':
                result['result'] = activate(connection, args.import_id, table_prefix=args.table_prefix)
                result['database_checked'] = True
            else: result['result'] = load(connection, capture, table_prefix=args.table_prefix, batch_size=args.batch_size) if capture else migrate(connection)
        finally: connection.close()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == '__main__': main()
