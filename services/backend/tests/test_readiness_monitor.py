"""/readyz is served from a background readiness monitor: a probe does no I/O, and readiness still fails closed."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from round_trips import count_round_trips
from reveal_backend import app as api, readiness
from reveal_backend.auth import Problem
from reveal_backend.catalog import Catalog, GENERATION_TTL_SECONDS, READINESS_VERIFICATION_TTL_SECONDS
from reveal_backend.mysql_pool import DatabaseBusy
from reveal_backend.reference_generation import KPN_MODEL, write_active
from reveal_backend.repository import Repository, digest

KPN1, KPN2 = digest('kpn-1'), digest('kpn-2')


class Sources:
    """Catalog stand-in: verify_binding counts verifications and can fail or block."""
    def __init__(self):
        self.verified, self.observed, self.failure, self.gate = [], [], None, None
    def verify_binding(self, binding):
        if self.gate: self.gate.wait(5)
        self.verified.append(binding)
        if self.failure: raise self.failure
        return {'mapping_run': binding[0], 'snapshot': binding[2]}
    def observe_pointers(self, generation, snapshot): self.observed.append((generation, snapshot))
    def readiness(self): raise AssertionError('a monitored probe never checks sources itself')


class ReadinessMonitorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.repo = Repository(str(Path(directory.name) / 'records.sqlite')); self.repo.migrate()
        self.activate(KPN1, 'snapshot-1')
        environment = patch.dict(os.environ, {'REVEAL_RETRIEVAL_BACKEND': 'upstash', 'REVEAL_VECTOR_ENVIRONMENT': 'local'})
        environment.start(); self.addCleanup(environment.stop)
        for name in ('REVEAL_REFERENCE_GENERATION_ID', 'REVEAL_ARTIFACT_STORE'): os.environ.pop(name, None)
        self.sources, self.now = Sources(), [1000.0]
        self.monitor = readiness.ReadinessMonitor(lambda: self.repo, lambda: self.sources, clock=lambda: self.now[0])
        for target, value in (('repo', self.repo), ('catalog', self.sources)):
            context = patch.object(api, target, value); context.start(); self.addCleanup(context.stop)
        context = patch.object(readiness, '_monitor', self.monitor); context.start(); self.addCleanup(context.stop)
        self.client = TestClient(api.app)

    def activate(self, generation, snapshot, previous=None):
        with self.repo.transaction() as tx:
            write_active(tx, generation, KPN_MODEL, expected_previous=previous)
            tx.put('vector_active', 'local', 'catalog', {'snapshot_id': snapshot})

    def tick(self):
        self.monitor.tick()
        for thread in threading.enumerate():
            if thread.name == 'readiness-verify': thread.join(5)

    def probe(self):
        response = self.client.get('/readyz')
        return response.status_code, response.json()

    def test_a_probe_reads_nothing_and_reports_the_monitors_last_check(self):
        with count_round_trips() as budget: self.tick()
        self.assertEqual((budget.kinds(), budget.statements()), (['single'], 1), budget)  # database and both pointers: one statement
        self.assertEqual(self.sources.observed, [(KPN1, 'snapshot-1')])  # shared with the catalog's poller
        with patch.object(Repository, 'single_read', side_effect=AssertionError('probe read')), \
             patch.object(Repository, 'read_transaction', side_effect=AssertionError('probe read')), count_round_trips() as budget:
            status, body = self.probe()
        self.assertEqual((status, body['status'], body['database'], body['sources']), (200, 'ready', 'sqlite-test', {'mapping_run': KPN1, 'snapshot': 'snapshot-1'}))
        self.assertEqual((budget.leases, budget.unleased, budget.connects), ([], 0, 0))
        self.tick(); self.assertEqual(len(self.sources.verified), 1)  # sources re-verified only once a minute
        self.now[0] += READINESS_VERIFICATION_TTL_SECONDS; self.tick(); self.assertEqual(len(self.sources.verified), 2)

    def test_an_unreachable_database_turns_readiness_false_at_the_next_tick(self):
        self.tick(); self.assertEqual(self.probe()[0], 200)
        self.now[0] += GENERATION_TTL_SECONDS
        with patch.object(Repository, 'single_read', side_effect=DatabaseBusy('Application database connection pool is busy')):
            self.monitor.tick()
        status, body = self.probe()
        self.assertEqual((status, body['code']), (503, 'SERVICE_UNAVAILABLE'))
        with patch.object(Repository, 'single_read', side_effect=OSError('connection refused')): self.monitor.tick()
        self.assertEqual(self.probe()[0], 503)
        self.now[0] += GENERATION_TTL_SECONDS; self.tick()
        self.assertEqual(self.probe()[0], 200)  # recovers on the next good read, with no new verification
        self.assertEqual(len(self.sources.verified), 1)

    def test_a_hung_or_stopped_monitor_never_keeps_a_cached_ready(self):
        self.tick(); self.assertEqual(self.probe()[0], 200)
        self.now[0] += readiness.POINTER_MAX_AGE + 1
        status, body = self.probe()
        self.assertEqual((status, body['code']), (503, 'SOURCE_NOT_READY'))
        self.monitor.stopped.set()  # a stopped monitor is no longer consulted: probes check synchronously again
        with patch.object(api, 'catalog', Catalog()), patch.object(Catalog, 'readiness', return_value={'sources': 'checked'}):
            self.assertEqual(self.probe()[1]['sources'], {'sources': 'checked'})

    def test_a_failed_verification_is_reported_with_its_code_and_retried_at_the_next_tick(self):
        self.sources.failure = Problem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', 'Vector provider unavailable')
        with self.assertLogs(readiness.LOGGER, 'WARNING') as logs:
            self.tick(); self.tick()  # not retried twice within one tick
            self.assertEqual(len(self.sources.verified), 1)
            for _ in range(2):
                status, body = self.probe()
                self.assertEqual((status, body['code'], body['detail']), (503, 'SEMANTIC_SEARCH_UNAVAILABLE', 'Vector provider unavailable'))
            self.now[0] += GENERATION_TTL_SECONDS; self.tick()
            self.assertEqual((len(self.sources.verified), self.probe()[1]['code']), (2, 'SEMANTIC_SEARCH_UNAVAILABLE'))
        self.assertEqual(len(logs.records), 1)  # an ongoing failure is logged once
        self.sources.failure = None
        self.now[0] += GENERATION_TTL_SECONDS; self.tick()
        self.assertEqual((self.probe()[0], len(self.sources.verified)), (200, 3))
        self.now[0] += GENERATION_TTL_SECONDS; self.tick()
        self.assertEqual(len(self.sources.verified), 3)  # a pass holds for the verification TTL again

    def test_a_busy_reference_pool_during_verification_costs_one_tick(self):
        self.tick()
        self.now[0] += READINESS_VERIFICATION_TTL_SECONDS
        self.sources.failure = DatabaseBusy('Reference read capacity is busy'); self.tick()
        status, body = self.probe()
        self.assertEqual((status, body['code']), (503, 'SERVICE_UNAVAILABLE'))
        self.sources.failure = None
        self.now[0] += GENERATION_TTL_SECONDS; self.tick()
        self.assertEqual((self.probe()[0], len(self.sources.verified)), (200, 3))

    def test_a_cutover_is_verified_and_a_missing_snapshot_pointer_fails_closed(self):
        self.tick()
        self.activate(KPN2, 'snapshot-2', previous=KPN1); self.sources.gate = threading.Event()
        self.now[0] += GENERATION_TTL_SECONDS; self.monitor.tick()  # verification of the new pointers is in flight
        status, body = self.probe()
        self.assertEqual((status, body['sources']['mapping_run']), (200, KPN1))  # the last verified sources, briefly
        self.now[0] += readiness.NEW_BINDING_GRACE + 1; self.monitor.current = (self.now[0],) + self.monitor.current[1:]
        self.assertEqual(self.probe()[1]['code'], 'SOURCE_NOT_READY')
        self.sources.gate.set(); self.tick()
        self.assertEqual(self.probe()[1]['sources'], {'mapping_run': KPN2, 'snapshot': 'snapshot-2'})
        with self.repo.transaction() as tx: tx.remove('vector_active', 'local')
        self.monitor.tick()
        self.assertEqual(self.probe()[1]['code'], 'SEMANTIC_SEARCH_UNAVAILABLE')

    def test_readiness_still_checks_api_key_configuration_first(self):
        self.tick()
        with patch.dict(os.environ, {'REVEAL_API_KEY_SHA256': 'x' * 64, 'REVEAL_API_KEY_USER_ID': ''}):
            status, body = self.probe()
        self.assertEqual((status, body['code']), (503, 'API_KEY_CONFIGURATION_INVALID'))


class LifespanTests(unittest.TestCase):
    def test_the_api_lifespan_starts_the_warmup_and_the_monitor_and_stops_the_monitor(self):
        class Lazy(Sources):
            warmed = 0
            def start_warmup(self): Lazy.warmed += 1
        sources = Lazy()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'REVEAL_CATALOG_WARMUP': '1', 'REVEAL_READINESS_MONITOR': '1', 'REVEAL_RETRIEVAL_BACKEND': 'legacy'}), \
                patch.object(api, 'catalog', sources), patch.object(api, 'repo', Repository(str(Path(directory) / 'r.sqlite'))):
            api.repo.migrate()
            with TestClient(api.app):
                monitor = readiness.running()
                self.assertIsNotNone(monitor); self.assertEqual(Lazy.warmed, 1)
            self.assertIsNone(readiness.running()); self.assertTrue(monitor.stopped.is_set())
        with patch.dict(os.environ, {'REVEAL_CATALOG_WARMUP': '0', 'REVEAL_READINESS_MONITOR': '0'}), patch.object(api, 'catalog', sources):
            with TestClient(api.app): self.assertIsNone(readiness.running())
        self.assertEqual(Lazy.warmed, 1)


if __name__ == '__main__':
    unittest.main()
