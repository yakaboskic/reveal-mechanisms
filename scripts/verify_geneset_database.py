#!/usr/bin/env python3
"""Read-only verification of a completed GeneSet import against its local export."""
import argparse
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import ssl

import pymysql
import import_cfde_genesets as source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=source.ROOT / 'data/cfde-genesets/2026-09-24')
    parser.add_argument('--host', default='aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com')
    parser.add_argument('--user', default='cyaka')
    parser.add_argument('--database', default='cyaka_reveal_mechanisms')
    parser.add_argument('--ca-file', type=Path, required=True)
    args = parser.parse_args()
    source.validate_db_name(args.database)
    manifest = source.check_export(args.output)
    samples = [row for index, row in enumerate(source.rows(args.output / 'records.jsonl.gz'))
               if index % 50000 == 0 or index == manifest['expected_rows'] - 1]
    connection = pymysql.connect(host=args.host, user=args.user, database=args.database,
        password=getpass.getpass('MySQL password (not saved): '),
        ssl=ssl.create_default_context(cafile=args.ca_file), connect_timeout=15,
        read_timeout=120, write_timeout=30, charset='utf8mb4')
    report = {'import_id': manifest['import_id'], 'database': args.database,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'complete': False}
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            cursor.execute('SELECT status,loaded_rows,expected_rows,manifest FROM gene_set_imports WHERE import_id=%s', (manifest['import_id'],))
            status, loaded, expected, stored_manifest = cursor.fetchone()
            assert status == 'complete' and loaded == expected == manifest['expected_rows']
            assert json.loads(stored_manifest) == manifest, 'Stored manifest differs'
            cursor.execute('''SELECT COUNT(*), COUNT(DISTINCT a.dapper_id),
                SUM(BINARY a.model <> BINARY %s),
                SUM(BINARY a.node_id <> BINARY CONCAT('gene_set:',a.source_key)),
                SUM(a.node_id_sha256 <> SHA2(a.node_id,256)),
                SUM(BINARY o.class_name <> BINARY 'GeneSet'),
                SUM(BINARY JSON_UNQUOTE(JSON_EXTRACT(o.payload,'$.id')) <> BINARY o.id),
                SUM(BINARY JSON_UNQUOTE(JSON_EXTRACT(o.payload,'$.term')) <> BINARY a.source_key)
                FROM cfde_gene_set_aliases a JOIN dapper_objects o ON o.id=a.dapper_id
                WHERE a.import_id=%s''', (manifest['model'], manifest['import_id']))
            count, distinct, *errors = cursor.fetchone()
            assert count == distinct == expected and not any(errors), 'Persisted alias/object invariants failed'
            for row in samples:
                cursor.execute('''SELECT a.node_id,a.model,o.payload,o.payload_sha256
                    FROM cfde_gene_set_aliases a JOIN dapper_objects o ON o.id=a.dapper_id
                    WHERE a.import_id=%s AND a.node_id_sha256=%s''',
                    (manifest['import_id'], source.digest(row['node_id'])))
                node_id, model, payload, sha = cursor.fetchone()
                assert node_id == row['node_id'] and model == row['model']
                assert json.loads(payload) == row['gene_set'], 'Read-back payload differs'
                assert sha == source.digest(source.canonical(row['gene_set']))
            activity = source.read_json(args.output / 'activity.json')
            cursor.execute('SELECT payload FROM dapper_objects WHERE id=%s', (activity['id'],))
            assert json.loads(cursor.fetchone()[0]) == activity
            report.update(complete=True, persisted_aliases=count, unique_gene_sets=distinct,
                          full_alias_checks_passed=True, exact_payload_samples=len(samples), activity_verified=True)
            print(source.canonical(report))
    finally:
        connection.close()
        source.write_json(args.output / 'database-verification.json', report)


if __name__ == '__main__':
    main()
