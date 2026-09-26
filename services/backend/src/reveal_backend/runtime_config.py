"""Runtime configuration. Secrets are read only from the environment."""
from pathlib import Path
import os

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

def artifacts_root():
    return Path(setting('REVEAL_ARTIFACTS_DIR', str(ROOT / '.runtime/artifacts'))).resolve()
