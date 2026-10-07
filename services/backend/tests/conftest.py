"""Tests run the API lifespan without its background startup work: no catalog warmup thread and no readiness
monitor, unless a test starts one explicitly."""
import os

os.environ.setdefault('REVEAL_CATALOG_WARMUP', '0')
os.environ.setdefault('REVEAL_READINESS_MONITOR', '0')
