"""Tests run the API lifespan without its background startup work: no catalog warmup thread, no readiness
monitor and no DAPPER helper warmup, unless a test starts one explicitly.

Tests needing the real DAPPER release use REVEAL_TEST_DAPPER_RELEASE, by default the first
checkout that verifies against the lock: REVEAL_DAPPER_ROOT, .runtime/dapper, .deployment-assets/dapper."""
import os
from pathlib import Path

os.environ.setdefault('REVEAL_CATALOG_WARMUP', '0')
os.environ.setdefault('REVEAL_READINESS_MONITOR', '0')
os.environ.setdefault('REVEAL_DAPPER_PREWARM', '0')


def verified_test_release():
    from reveal_backend.dapper_release import verified_release
    root = Path(__file__).resolve().parents[3]
    lock = root / 'services/backend/agent-runtime/dapper-release.json'
    candidates = [os.environ.get('REVEAL_DAPPER_ROOT'), root / '.runtime/dapper', root / '.deployment-assets/dapper']
    return next((str(Path(path)) for path in candidates if path and verified_release(Path(path), lock)), None)


if 'REVEAL_TEST_DAPPER_RELEASE' not in os.environ and (release := verified_test_release()):
    os.environ['REVEAL_TEST_DAPPER_RELEASE'] = release
