#!/usr/bin/env python3
"""Read-only MySQL capability audit; prompts for credentials without echoing.

No DDL or DML is performed. Only this account's grants and server capabilities
are inspected. The output excludes unrelated database names and auth material.
"""
import argparse
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import re
import ssl
import pymysql


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--ca-file', required=True)
    parser.add_argument('--database', default='cyaka_reveal_mechanisms')
    parser.add_argument('--output', type=Path, default=Path('data/infrastructure/mysql-audit-2026-09-24.json'))
    args = parser.parse_args()
    if not args.database.startswith('cyaka_') or not re.fullmatch(r'[a-z0-9_]+', args.database):
        raise SystemExit('Project database must be a simple cyaka_-prefixed identifier')
    password = getpass.getpass('MySQL password (not saved): ')
    result = {'audited_at': datetime.now(timezone.utc).isoformat(), 'host': args.host,
              'user': args.user, 'proposed_database': args.database, 'mode': 'read_only',
              'database_created': False, 'creation_executed': False}
    try:
        connection = pymysql.connect(host=args.host, user=args.user, password=password,
            ssl=ssl.create_default_context(cafile=args.ca_file), connect_timeout=12,
            read_timeout=15, write_timeout=15, autocommit=True)
        del password
        with connection, connection.cursor() as cursor:
            cursor.execute('SELECT VERSION(), @@version_comment, CURRENT_USER()')
            version, comment, account = cursor.fetchone()
            result.update(connected=True, version=version, version_comment=comment, authenticated_account=account)
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            result['tls_cipher'] = cursor.fetchone()[1]
            cursor.execute('SHOW GRANTS FOR CURRENT_USER')
            grant_scopes = []
            for (grant,) in cursor.fetchall():
                match = re.match(r'GRANT (.+?) ON (.+?) TO ', grant)
                if match:
                    privileges, scope = match.groups()
                    if scope == '*.*' or 'cyaka' in scope:
                        grant_scopes.append({'privileges': privileges, 'scope': scope})
            result['relevant_grants'] = grant_scopes
            cursor.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s', (args.database,))
            result['project_database_visible'] = bool(cursor.fetchone())
            cursor.execute("SHOW VARIABLES LIKE 'aurora_version'")
            aurora = cursor.fetchone()
            result['aurora_version_variable'] = aurora[1] if aurora else None
            # Non-mutating arithmetic probe; success does not imply indexed search.
            try:
                cursor.execute('USE information_schema')
                cursor.execute('SELECT VECTOR_DIM(STRING_TO_VECTOR(%s))', ('[1,2,3]',))
                result['mysql_vector_function_probe'] = {'supported': True, 'dimension': cursor.fetchone()[0]}
            except pymysql.MySQLError as error:
                result['mysql_vector_function_probe'] = {'supported': False if error.args[0] == 1305 else None, 'error_code': error.args[0],
                    'error': str(error.args[1])[:500] if len(error.args) > 1 else type(error).__name__}
    except (pymysql.MySQLError, OSError) as error:
        result.update(connected=False, error_type=type(error).__name__, error_code=error.args[0] if error.args else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
