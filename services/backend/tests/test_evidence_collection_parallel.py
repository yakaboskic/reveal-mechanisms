"""Bounded source reads change scheduling, never evidence bytes or selection."""
from collections import Counter
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import test_evidence_package as fixtures
from reveal_backend.evidence_collector import collect_package
from reveal_backend.evidence_package import EvidenceBuildError, decode, sha256


class EvidenceCollectionParallelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the committed source fixture without inheriting/re-running its
        # unrelated test methods through unittest discovery.
        fixtures.EvidencePackageTests.setUpClass()
        cls.fixture = fixtures.EvidencePackageTests

    @classmethod
    def tearDownClass(cls):
        fixtures.EvidencePackageTests.tearDownClass()

    def collect(self, name, factory, **overrides):
        return collect_package(gap_id=fixtures.GAP,
            factor_ids=[fixtures.FACTOR, fixtures.FACTOR.rsplit(':', 1)[0] + ':Factor2'],
            output=self.fixture.root / name, dapper=self.fixture.runtime, project_root=fixtures.ROOT,
            dismech_source=self.fixture.source, dismech_index=fixtures.ROOT / 'data/dismech-gaps/2026-09-24',
            geneset_import=self.fixture.imported, limit=8, client_factory=factory, **overrides)

    def test_parallel_and_serial_are_byte_identical_with_bound_and_shared_trait_reuse(self):
        clients = []
        class Client(fixtures.FixtureClient):
            def __init__(self, store):
                super().__init__(store, two_anchors=True)
                self.lock = threading.Lock(); self.active = self.peak = 0; self.names = []
                clients.append(self)
            def request(self, name, *args):
                with self.lock:
                    self.active += 1; self.peak = max(self.peak, self.active); self.names.append(name)
                try:
                    # Different completion order stresses deterministic merging.
                    time.sleep(.003 * (1 + sum(name.encode()) % 4))
                    return super().request(name, *args)
                finally:
                    with self.lock: self.active -= 1
        serial = self.collect('parallel-test-serial', Client, max_parallel_requests=1)
        parallel = self.collect('parallel-test-four', Client)
        self.assertEqual(serial.files, parallel.files)
        self.assertEqual((self.fixture.root / 'parallel-test-serial/build-input.json').read_bytes(),
                         (self.fixture.root / 'parallel-test-four/build-input.json').read_bytes())
        self.assertEqual(clients[0].peak, 1)
        self.assertGreater(clients[1].peak, 1); self.assertLessEqual(clients[1].peak, 4)
        self.assertEqual(Counter(clients[0].names), Counter(clients[1].names))
        self.assertEqual(len(clients[1].names), 13)
        self.assertEqual(sum(name.startswith('catalog-') for name in clients[1].names), 1)
        self.assertEqual(sum(name.startswith('factor-catalog-') for name in clients[1].names), 1)
        self.assertEqual(sum('-trait-' in name for name in clients[1].names), 2)
        timings = decode((self.fixture.root / 'parallel-test-four/collection-timings.json').read_bytes())
        self.assertEqual(timings['status'], 'completed')
        self.assertEqual([(row['stage'], row.get('request_count')) for row in timings['stages']],
            [('factor_catalogs', 2), ('connections', 4), ('source_observations', 7), ('validated_assembly', None)])
        self.assertNotIn('collection-timings.json', parallel.files)
        self.assertTrue(all(row['seconds'] >= 0 for row in timings['stages']))

    def test_failed_parallel_stage_retains_all_started_captures_without_partial_package(self):
        class Client(fixtures.FixtureClient):
            def __init__(self, store): super().__init__(store, two_anchors=True)
            def request(self, name, *args):
                time.sleep(.005)
                result = super().request(name, *args)
                if name.startswith('gene-factor-'):
                    raise EvidenceBuildError('Simulated validation failure of saved source response')
                return result
        with self.assertRaisesRegex(EvidenceBuildError, 'Simulated validation'):
            self.collect('parallel-test-failed', Client)
        root = self.fixture.root / 'parallel-test-failed'
        self.assertFalse((root / 'package').exists())
        error = decode((root / 'collection-error.json').read_bytes())
        self.assertTrue(any(name.startswith('gene-factor-') for name in error['artifacts']))
        self.assertTrue(any(name.startswith('gene_set-trait-') for name in error['artifacts']))
        timings = decode((root / 'collection-timings.json').read_bytes())
        self.assertEqual(timings['status'], 'failed')
        self.assertEqual(timings['stages'][-1]['stage'], 'source_observations')
        self.assertEqual(timings['stages'][-1]['status'], 'failed')
        before = {str(p): sha256(p.read_bytes()) for p in root.rglob('*') if p.is_file()}
        time.sleep(.025)
        self.assertEqual(before, {str(p): sha256(p.read_bytes()) for p in root.rglob('*') if p.is_file()})

    def test_invalid_parallelism_is_rejected_before_capture_or_network(self):
        def unavailable(_): self.fail('Invalid bounds reached transport')
        for maximum in (0, 5, True, 1.5):
            with self.subTest(maximum=maximum), self.assertRaisesRegex(EvidenceBuildError, 'max_parallel_requests'):
                self.collect('invalid-parallelism-' + str(maximum), unavailable, max_parallel_requests=maximum)

    def test_timing_report_write_failure_cannot_change_success_or_mask_source_failure(self):
        original = Path.write_bytes
        def write(path, data):
            if path.name == 'collection-timings.json': raise PermissionError('Simulated telemetry disk failure')
            return original(path, data)
        class Client(fixtures.FixtureClient):
            def __init__(self, store): super().__init__(store, two_anchors=True)
        with patch.object(Path, 'write_bytes', write):
            result = self.collect('timing-report-unavailable', Client)
        self.assertTrue(result.files['evidence-package.json'])
        class Failed(Client):
            def request(self, *args): raise EvidenceBuildError('Original source failure')
        with patch.object(Path, 'write_bytes', write), self.assertRaisesRegex(EvidenceBuildError, '^Original source failure$'):
            self.collect('timing-and-source-failure', Failed)


if __name__ == '__main__':
    unittest.main()
