"""Runtime configuration. Secrets are read only from the environment."""
from pathlib import Path
import hashlib
import os
import threading

ROOT = Path(__file__).resolve().parents[4]

def setting(name, default=None):
    return os.environ.get(name, default)

def mysql_connection():
    from .mysql_database import connect
    if not setting('REVEAL_MYSQL_PASSWORD'):
        raise RuntimeError('REVEAL_MYSQL_PASSWORD is required')
    return connect(host=setting('REVEAL_MYSQL_HOST', 'aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com'),
        port=int(setting('REVEAL_MYSQL_PORT', '3306')), user=setting('REVEAL_MYSQL_USER', 'cyaka'),
        database=setting('REVEAL_MYSQL_DATABASE', 'cyaka_reveal_mechanisms'), ca_file=setting('REVEAL_MYSQL_CA_FILE') or None)


_application_pool = None
_application_pool_key = None
_application_pool_lock = threading.Lock()


def _after_fork():
    global _application_pool, _application_pool_key, _application_pool_lock
    _application_pool_lock = threading.Lock()
    if _application_pool is not None: _application_pool._process()
    _application_pool = None; _application_pool_key = None


if hasattr(os, 'register_at_fork'): os.register_at_fork(after_in_child=_after_fork)


def application_mysql_connection():
    """Lease a bounded clean session, only for runtime Repository transactions."""
    from .mysql_database import initialize_application_session, reset_application_session
    from .mysql_pool import Pool
    global _application_pool, _application_pool_key
    maximum = int(setting('REVEAL_MYSQL_POOL_SIZE', '4'))
    if maximum == 0: return mysql_connection()
    wait = float(setting('REVEAL_MYSQL_POOL_WAIT_SECONDS', '5'))
    if not 1 <= maximum <= 32 or not 0 < wait <= 30: raise ValueError('Invalid application database pool bounds')
    ca_file = setting('REVEAL_MYSQL_CA_FILE') or None
    ca_revision = hashlib.sha256(Path(ca_file).read_bytes()).hexdigest() if ca_file else None
    database = setting('REVEAL_MYSQL_DATABASE', 'cyaka_reveal_mechanisms')
    # Never retain credentials in registry keys or diagnostic representations.
    key = (os.getpid(), setting('REVEAL_MYSQL_HOST'), setting('REVEAL_MYSQL_PORT'),
        setting('REVEAL_MYSQL_USER'), database, ca_file, ca_revision,
        hashlib.sha256((setting('REVEAL_MYSQL_PASSWORD') or '').encode()).digest(), maximum, wait)
    with _application_pool_lock:
        if _application_pool is None or key != _application_pool_key:
            if _application_pool is not None: _application_pool.close()
            def factory():
                connection = mysql_connection()
                try: initialize_application_session(connection)
                except BaseException:
                    connection.close(); raise
                return connection
            _application_pool = Pool(factory, lambda connection: reset_application_session(connection, database),
                maximum=maximum, wait_seconds=wait)
            _application_pool_key = key
        pool = _application_pool
    return pool.acquire()

def artifacts_root():
    return Path(setting('REVEAL_ARTIFACTS_DIR', str(ROOT / '.runtime/artifacts'))).resolve()
