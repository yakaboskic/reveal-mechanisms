"""Shared verified-TLS MySQL connection and strict batch insertion helpers."""
from __future__ import annotations

import getpass
import os
import re
import ssl

DEFAULT_HOST = 'aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com'


def validate_database(name):
    if not re.fullmatch(r'cyaka_[a-z0-9_]+', name):
        raise ValueError('Database must be a simple identifier with literal cyaka_ prefix')


def connect(*, host=DEFAULT_HOST, port=3306, user='cyaka', database='cyaka_reveal_mechanisms', ca_file=None):
    import pymysql
    validate_database(database)
    password = os.getenv('REVEAL_MYSQL_PASSWORD') or getpass.getpass('MySQL password (not saved): ')
    connection = pymysql.connect(host=host, port=port, user=user, password=password, database=database,
        ssl=ssl.create_default_context(cafile=ca_file), charset='utf8mb4', autocommit=False,
        binary_prefix=True, connect_timeout=15, read_timeout=120, write_timeout=120)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            row = cursor.fetchone()
            if not row or not row[1]:
                raise ValueError('Verified TLS is required')
        connection.reveal_verified_tls = True
        return connection
    except BaseException:
        connection.close()
        raise


def initialize_application_session(connection):
    """Canonical settings for Repository-only leases; one server round trip."""
    with connection.cursor() as cursor:
        cursor.execute("SET SESSION autocommit=0, transaction_isolation='REPEATABLE-READ', "
            "time_zone='+00:00', character_set_client='utf8mb4', character_set_connection='utf8mb4', "
            "character_set_results='utf8mb4', collation_connection='utf8mb4_unicode_ci'")
    connection.reveal_session_defaults = True


def reset_application_session(connection, database):
    """Clear server state without reauthentication; failures discard the socket.

    MySQL's documented COM_RESET_CONNECTION (0x1f) rolls back, releases locks,
    drops temporary tables and clears session/user variables. PyMySQL 1.1.2
    exposes no public reset method; use its command/OK-packet primitives, also
    used by ping(). No reconnect or SQL replay is allowed here.
    https://dev.mysql.com/doc/c-api/8.0/en/mysql-reset-connection.html
    """
    validate_database(database)
    connection._execute_command(0x1F, b'')
    connection._read_ok_packet()
    # RESET does not promise to restore the selected schema. Never inherit a
    # borrower-selected database, even if future Repository code selects one.
    connection.select_db(database)
    initialize_application_session(connection)


def insert_batch(cursor, table, columns, batch):
    # Table and column names come only from the static importer stage declarations.
    cursor.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})", batch)
    cursor.execute('SHOW WARNINGS')
    if cursor.fetchall():
        raise ValueError(f'MySQL conversion warning inserting {table}; transaction rolled back')

