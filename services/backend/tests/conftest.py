"""Tests run the API lifespan without its background startup work: no catalog warmup thread, no readiness
monitor and no DAPPER helper warmup, unless a test starts one explicitly."""
import os

os.environ.setdefault('REVEAL_CATALOG_WARMUP', '0')
os.environ.setdefault('REVEAL_READINESS_MONITOR', '0')
os.environ.setdefault('REVEAL_DAPPER_PREWARM', '0')
