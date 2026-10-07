"""Runtime configuration. Secrets are read only from the environment."""
from pathlib import Path
import hashlib
import json
import os
import threading

ROOT = Path(__file__).resolve().parents[4]
CURRENT_DAPPER_SNAPSHOT = ROOT / 'data/dapper/0.2.0'


def dapper_snapshot_for_pin(pin, *, root=ROOT):
    """Select an installed, approved immutable snapshot without changing its pin."""
    root = Path(root)
    lock = json.loads((root / 'services/backend/agent-runtime/dapper-release.json').read_bytes())
    wanted = pin.get('snapshot_sha256')
    if wanted not in lock['compatible_input_snapshots']:
        raise ValueError('Package DAPPER snapshot is not approved for the current release')
    for relative in ('data/dapper/0.2.0', 'data/dapper/2026-09-24-v8'):
        directory = root / relative
        manifest = directory / 'snapshot.json'
        if not manifest.is_file(): continue
        raw = manifest.read_bytes()
        if json.loads(raw).get('snapshot_sha256') != wanted: continue
        if (pin.get('snapshot_manifest_sha256') is not None and
                hashlib.sha256(raw).hexdigest() != pin['snapshot_manifest_sha256']):
            raise ValueError('Package DAPPER snapshot manifest differs from its original pin')
        return directory
    raise ValueError('The approved DAPPER snapshot is not installed')

def setting(name, default=None):
    return os.environ.get(name, default)

def mysql_connection(*, timeout_seconds=None):
    from .mysql_database import connect
    if not setting('REVEAL_MYSQL_PASSWORD'):
        raise RuntimeError('REVEAL_MYSQL_PASSWORD is required')
    return connect(host=setting('REVEAL_MYSQL_HOST', 'aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com'),
        port=int(setting('REVEAL_MYSQL_PORT', '3306')), user=setting('REVEAL_MYSQL_USER', 'cyaka'),
        database=setting('REVEAL_MYSQL_DATABASE', 'cyaka_reveal_mechanisms'), ca_file=setting('REVEAL_MYSQL_CA_FILE') or None,
        **({'timeout_seconds': timeout_seconds} if timeout_seconds is not None else {}))


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
    from . import mysql_database as db
    from .mysql_pool import Pool
    from .runtime_metrics import measure
    global _application_pool, _application_pool_key
    maximum = int(setting('REVEAL_MYSQL_POOL_SIZE', '4'))
    if maximum == 0: return mysql_connection()
    wait = float(setting('REVEAL_MYSQL_POOL_WAIT_SECONDS', '5'))
    clean_release = setting('REVEAL_MYSQL_POOL_CLEAN_RELEASE', '1') != '0'
    pipelined = setting('REVEAL_MYSQL_POOL_PIPELINED_RESET', '1') != '0'
    if not 1 <= maximum <= 32 or not 0 < wait <= 30: raise ValueError('Invalid application database pool bounds')
    ca_file = setting('REVEAL_MYSQL_CA_FILE') or None
    ca_revision = hashlib.sha256(Path(ca_file).read_bytes()).hexdigest() if ca_file else None
    database = setting('REVEAL_MYSQL_DATABASE', 'cyaka_reveal_mechanisms')
    # Never retain credentials in registry keys or diagnostic representations.
    key = (os.getpid(), setting('REVEAL_MYSQL_HOST'), setting('REVEAL_MYSQL_PORT'),
        setting('REVEAL_MYSQL_USER'), database, ca_file, ca_revision,
        hashlib.sha256((setting('REVEAL_MYSQL_PASSWORD') or '').encode()).digest(), maximum, wait, clean_release, pipelined)
    with _application_pool_lock:
        if _application_pool is None or key != _application_pool_key:
            if _application_pool is not None: _application_pool.close()
            reset_session = db.reset_application_session if pipelined else db.reset_application_session_sequential
            def factory():
                connection = mysql_connection()
                try: db.initialize_application_session(connection)
                except BaseException:
                    connection.close(); raise
                return connection
            def reset(connection):
                with measure('database', 'RESET'): reset_session(connection, database)
            _application_pool = Pool(factory, reset, unchanged=db.application_session_unchanged if clean_release else None,
                maximum=maximum, wait_seconds=wait)
            _application_pool_key = key
        pool = _application_pool
    return pool.acquire()

def artifacts_root():
    if setting('REVEAL_ARTIFACT_STORE') == 's3':
        return Path(setting('REVEAL_WORK_DIR', '/work')).resolve()
    return Path(setting('REVEAL_ARTIFACTS_DIR', str(ROOT / '.runtime/artifacts'))).resolve()
