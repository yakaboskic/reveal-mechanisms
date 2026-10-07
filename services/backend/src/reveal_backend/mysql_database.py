"""Shared verified-TLS MySQL connection and strict batch insertion helpers."""
from __future__ import annotations

import getpass
import os
import re
import ssl
import struct

DEFAULT_HOST = 'aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com'
# Pooled Repository sessions only (importers hold long sessions and GET_LOCK): a writer queued on the
# global fence gives up after 15 s, and the server ends a session silent for 300 s, such as an orphaned
# fence holder. Pool idle expiry stays below that (runtime_config), so it never lends a killed session.
SESSION_LOCK_WAIT_SECONDS, SESSION_IDLE_KILL_SECONDS = 15, 300
APPLICATION_SESSION_SQL = ("SET SESSION autocommit=0, completion_type='NO_CHAIN', transaction_isolation='REPEATABLE-READ', "
    "time_zone='+00:00', character_set_client='utf8mb4', character_set_connection='utf8mb4', "
    "character_set_results='utf8mb4', collation_connection='utf8mb4_unicode_ci', "
    f"innodb_lock_wait_timeout={SESSION_LOCK_WAIT_SECONDS}, wait_timeout={SESSION_IDLE_KILL_SECONDS}")
_COM_INIT_DB, _COM_QUERY, _COM_RESET_CONNECTION = 0x02, 0x03, 0x1F
# server_status bits (PyMySQL 1.1.2 lacks IN_TRANS_READONLY) and the CLIENT_MULTI_STATEMENTS capability.
_IN_TRANS, _AUTOCOMMIT, _MORE_RESULTS, _IN_TRANS_READONLY, _MULTI_STATEMENTS = 0x1, 0x2, 0x8, 0x2000, 0x10000


def validate_database(name):
    if not re.fullmatch(r'cyaka_[a-z0-9_]+', name):
        raise ValueError('Database must be a simple identifier with literal cyaka_ prefix')


def require_verified_tls(connection, context):
    """A completed handshake under CERT_REQUIRED + check_hostname is verified TLS; PyMySQL silently stays
    plaintext when the server lacks CLIENT_SSL, leaving a plain socket. PyMySQL==1.1.2 internals: fail closed."""
    sock = getattr(connection, '_sock', None)
    if not (context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            and getattr(connection, '_secure', False) is True and isinstance(sock, ssl.SSLSocket)
            and sock.version() and sock.cipher()):
        raise ValueError('Verified TLS is required')


def connect(*, host=DEFAULT_HOST, port=3306, user='cyaka', database='cyaka_reveal_mechanisms', ca_file=None,
            timeout_seconds=None, application_session=False):
    """application_session (pool factory only) applies APPLICATION_SESSION_SQL as init_command; other
    callers keep server time_zone/collation. PyMySQL runs init_command before autocommit(False), which then
    sends nothing because the SET already reported autocommit off."""
    import pymysql
    validate_database(database)
    if timeout_seconds is not None and timeout_seconds <= 0: raise ValueError('Connection deadline elapsed')
    timeout = min(120, timeout_seconds) if timeout_seconds is not None else 120
    password = os.getenv('REVEAL_MYSQL_PASSWORD') or getpass.getpass('MySQL password (not saved): ')
    context = ssl.create_default_context(cafile=ca_file)
    connection = pymysql.connect(host=host, port=port, user=user, password=password, database=database,
        ssl=context, charset='utf8mb4', autocommit=False, init_command=APPLICATION_SESSION_SQL if application_session else None,
        binary_prefix=True, connect_timeout=min(15, timeout), read_timeout=timeout, write_timeout=timeout)
    try:
        require_verified_tls(connection, context)
        connection.reveal_verified_tls = True
        if application_session: connection.reveal_session_defaults = True
        return connection
    except BaseException:
        connection.close()
        raise


def initialize_application_session(connection):
    """Canonical settings for Repository-only leases; one server round trip."""
    with connection.cursor() as cursor:
        cursor.execute(APPLICATION_SESSION_SQL)
    connection.reveal_session_defaults = True


def application_session_unchanged(connection):
    """Zero-round-trip proof from the OK packet of a lease's final COMMIT/ROLLBACK: no transaction (row lock,
    read view or READ ONLY flag) is open, autocommit is still 0, no result is pending and one execute() is one
    statement. Session/user variables are invisible here; the lease's statement classification guards them."""
    status, flags = getattr(connection, 'server_status', None), getattr(connection, 'client_flag', None)
    return (getattr(connection, 'reveal_session_defaults', False) is True and type(status) is int and type(flags) is int
            and not status & (_IN_TRANS | _AUTOCOMMIT | _MORE_RESULTS | _IN_TRANS_READONLY) and not flags & _MULTI_STATEMENTS)


def _command(code, payload):
    # PyMySQL's own first-packet layout: 3-byte length, sequence id 0, command byte.
    return struct.pack('<I', len(payload) + 1)[:3] + b'\x00' + bytes([code]) + payload


def reset_application_session(connection, database):
    """COM_RESET_CONNECTION, COM_INIT_DB and the canonical SET SESSION in one write, acknowledged in order:
    one round trip. RESET rolls back, releases locks, drops temporary tables and clears variables, but keeps a
    borrower-selected schema and restores autocommit=1 and the server time zone (measured on Aurora 3.10.3),
    so neither INIT_DB nor SET may be dropped. Any error leaves unread replies: Pool.release must discard the
    socket. No reconnect or SQL replay. PyMySQL==1.1.2 primitives, as used by its own ping().
    https://dev.mysql.com/doc/c-api/8.0/en/mysql-reset-connection.html
    """
    import pymysql
    validate_database(database)
    if connection._sock is None: raise pymysql.err.InterfaceError(0, 'Session reset on a closed socket')
    pending = connection._result
    if pending is not None and (pending.unbuffered_active or pending.has_next):
        raise pymysql.err.InterfaceError(0, 'Unfinished result before session reset')
    connection._result = None
    connection._write_bytes(_command(_COM_RESET_CONNECTION, b'') + _command(_COM_INIT_DB, database.encode('ascii'))
                            + _command(_COM_QUERY, APPLICATION_SESSION_SQL.encode('ascii')))
    for _ in range(3):
        connection._next_seq_id = 1  # each reply is sequence 1 of its own command
        connection._read_ok_packet()
    connection.reveal_session_defaults = True


def reset_application_session_sequential(connection, database):
    """The same three commands, one round trip each (REVEAL_MYSQL_POOL_PIPELINED_RESET=0, e.g. behind a proxy)."""
    validate_database(database)
    connection._execute_command(_COM_RESET_CONNECTION, b'')
    connection._read_ok_packet()
    connection.select_db(database)
    initialize_application_session(connection)


def validate_idle_session(connection, timeout_seconds=3):
    """COM_PING a long-idle pooled session under a short deadline: 1 round trip, no SQL, never reconnects."""
    saved = connection._read_timeout, connection._write_timeout  # PyMySQL applies these on every read/write
    connection._read_timeout = connection._write_timeout = timeout_seconds
    try: connection.ping(reconnect=False)
    finally: connection._read_timeout, connection._write_timeout = saved


def insert_batch(cursor, table, columns, batch):
    # Table and column names come only from the static importer stage declarations.
    cursor.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})", batch)
    cursor.execute('SHOW WARNINGS')
    if cursor.fetchall():
        raise ValueError(f'MySQL conversion warning inserting {table}; transaction rolled back')
