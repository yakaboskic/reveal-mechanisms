"""Fresh snapshot orchestration uses bounded workers without releasing the reload lock early.

Every service is an in-memory fake; no database, network, upload or filesystem artifacts.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Event, Lock, Thread
from types import SimpleNamespace

from reveal_backend import reference_reload as rr


class SnapshotHarness:
    def __init__(self, monkeypatch, *, fail=False):
        self.guard = Lock()
        self.four_started, self.finish_batches, self.fail_batch = Event(), Event(), Event()
        self.shutdown_entered, self.finished = Event(), Event()
        self.active = self.maximum = self.started = 0
        self.held = self.closed = self.verified = False
        self.releases = self.bindings = 0
        self.results, self.output = set(), {}
        self.fail = fail
        self.generation = 'a' * 64
        self.target = {'name': 'rehearsal', 'prefix': 'reveal_reload_rehearsal', 'vector_environment': 'rehearsal'}
        self.manifest = {'snapshot_id': 'b' * 64, 'environment': 'rehearsal', 'reference_generation_id': self.generation,
                         'status': 'loading', 'batches': {f'factors:{i}': [f'factor:{i}'] for i in range(8)},
                         'factor_namespace': 'rehearsal-test-factors'}
        self.client, self.exported = object(), object()
        self.registry = SimpleNamespace(repo=self, register=self.register, get=self.get)
        self.vi = SimpleNamespace(VectorRegistry=self.make_registry, build_generation_export=self.build,
            save_export=self.save, import_batch=self.import_batch, verify_snapshot=self.verify,
            record_vector_bindings=self.record_bindings)
        self.services = SimpleNamespace(module=lambda name: self.vi if name == 'vector_ingestion' else None,
            repository=lambda prefix: self if prefix == self.target['prefix'] else None, connect=lambda: self,
            vector_client=lambda *, write: self.client if write else None)
        monkeypatch.setattr(rr, 'bind_target', lambda services, target: None)
        monkeypatch.setattr(rr, 'scalar', self.scalar)
        monkeypatch.setattr(rr.rg, 'get_generation', lambda connection, generation: {
            'generation_id': generation, 'kind': rr.rg.KPN_KIND, 'status': 'complete'})
        monkeypatch.setattr(rr, 'generation_snapshots', lambda tx, environment, generation: [])
        harness = self

        class ObservedExecutor(ThreadPoolExecutor):
            def shutdown(self, wait=True, *, cancel_futures=False):
                harness.shutdown_entered.set()
                return super().shutdown(wait=wait, cancel_futures=cancel_futures)

        monkeypatch.setattr(rr, 'ThreadPoolExecutor', ObservedExecutor)

    @contextmanager
    def read_transaction(self):
        yield self

    def scalar(self, connection, sql, parameters):
        assert connection is self
        with self.guard:
            if sql.startswith('SELECT GET_LOCK'):
                assert parameters == (rr.LOCK_NAME, 0) and not self.held
                self.held = True
            else:
                assert sql == 'SELECT RELEASE_LOCK(%s)' and parameters == (rr.LOCK_NAME,)
                assert self.held and self.active == 0  # no worker survives release
                self.held = False
                self.releases += 1
        return 1

    def close(self):
        assert not self.held
        self.closed = True

    def make_registry(self, repo, *, environment_name):
        assert repo is self and environment_name == self.target['vector_environment']
        return self.registry

    def build(self, connection, generation, *, batch_size):
        assert connection is self and self.held and generation == self.generation and batch_size == 200
        return deepcopy(self.manifest), self.exported

    def save(self, manifest, exported):
        assert self.held and exported is self.exported
        return {**manifest, 'export_ref': {'store': 'test'}}

    def register(self, manifest):
        assert self.held and manifest['status'] == 'loading'

    def import_batch(self, registry, identity, key, *, client):
        assert registry is self.registry and identity == self.manifest['snapshot_id'] and client is self.client
        with self.guard:
            assert self.held
            self.active += 1
            self.started += 1
            self.maximum = max(self.maximum, self.active)
            if self.started == 4:
                self.four_started.set()
        try:
            if self.fail and key == 'factors:0':
                assert self.fail_batch.wait(5), 'test did not permit the failing batch to finish'
                raise RuntimeError('test batch failed readback')
            assert self.finish_batches.wait(5), 'test did not permit batch completion'
            with self.guard:
                assert self.held
                self.results.add(key)
            return {'batch': key, 'verified': True}
        finally:
            with self.guard:
                self.active -= 1

    def verify(self, registry, identity, *, client):
        assert registry is self.registry and identity == self.manifest['snapshot_id'] and client is self.client
        assert self.held and self.active == 0 and self.results == set(self.manifest['batches'])
        assert self.shutdown_entered.is_set()
        self.verified = True
        return {'passed': True}

    def get(self, identity):
        assert self.verified and self.held and identity == self.manifest['snapshot_id']
        return {**self.manifest, 'status': 'complete'}

    def record_bindings(self, connection, state):
        assert connection is self and self.held and self.verified and state['status'] == 'complete'
        self.bindings += 1
        return len(self.results)

    def start(self):
        def run():
            try:
                self.output['result'] = rr.snapshot_generation(self.services, self.target, self.generation, apply=True)
            except BaseException as error:
                self.output['error'] = error
            finally:
                self.finished.set()
        self.thread = Thread(target=run, daemon=True)
        self.thread.start()

    def finish(self):
        self.finish_batches.set()
        self.fail_batch.set()
        self.thread.join(5)
        assert not self.thread.is_alive(), 'snapshot worker did not finish'


def test_fresh_snapshot_upload_has_four_workers_then_verifies_and_writes_bindings(monkeypatch):
    harness = SnapshotHarness(monkeypatch)
    harness.start()
    try:
        assert harness.four_started.wait(5), 'snapshot did not start four parallel batches'
        assert harness.active == harness.maximum == harness.started == 4
        assert harness.held and not harness.verified and harness.bindings == harness.releases == 0
    finally:
        harness.finish()
    assert 'error' not in harness.output, harness.output.get('error')
    assert harness.maximum == 4 and harness.started == len(harness.manifest['batches'])
    assert harness.verified and harness.bindings == harness.releases == 1 and harness.closed
    assert harness.output['result']['verification'] == {'passed': True}
    assert harness.output['result']['status'] == 'complete' and harness.output['result']['bindings'] == 8


def test_failed_batch_waits_for_workers_before_unlock_and_never_verifies_or_writes_bindings(monkeypatch):
    harness = SnapshotHarness(monkeypatch, fail=True)
    harness.start()
    try:
        assert harness.four_started.wait(5), 'snapshot did not start four parallel batches'
        harness.fail_batch.set()
        assert harness.shutdown_entered.wait(5), 'snapshot did not propagate the failed batch'
        assert harness.held and harness.active > 0
        assert not harness.finished.is_set() and harness.releases == 0
        assert not harness.verified and harness.bindings == 0
    finally:
        harness.finish()
    assert isinstance(harness.output.get('error'), RuntimeError)
    assert str(harness.output['error']) == 'test batch failed readback'
    assert 'result' not in harness.output and harness.maximum == 4
    assert not harness.verified and harness.bindings == 0
    assert harness.releases == 1 and harness.closed
