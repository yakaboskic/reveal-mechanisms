"""Only a phase's own failures spend the recovery budget; infrastructure retries are delivery-class."""
import asyncio
import errno
import gzip
import io
import shutil
import socket
import ssl
import unittest
from unittest.mock import AsyncMock, Mock, patch

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
import httpx
import pymysql
from upstash_box.errors import BoxError

from reveal_backend import workflow_state as state
from reveal_backend.artifact_store import StorageUnavailable
from reveal_backend.box_adapter import BoxTransportError
from reveal_backend.box_lifecycle import BoxLifecycle
from reveal_backend.repository import DatabaseBusy, FenceBusy, Transaction
from reveal_backend.runtime_config import ROOT
from reveal_backend.workflow_execution import WorkflowExecution, infrastructure_error, retried
from reveal_backend.workflow_routes import reconcile_stale
import test_durable_workflow as durable


def wrapped(outer, cause):
    """outer raised `from` cause, as the storage and Box wrappers do."""
    try:
        try: raise cause
        except BaseException as exc: raise outer from exc
    except BaseException as exc: return exc


S3 = 'https://s3.invalid'
INFRASTRUCTURE = (
    DatabaseBusy('Application database writers are busy'), FenceBusy('The application write fence is held'),
    TimeoutError(), ConnectionResetError(errno.ECONNRESET, 'reset'), OSError(errno.EHOSTUNREACH, 'No route to host'),
    OSError(errno.ENOSPC, 'No space left on device'), OSError('write failed'), OSError(errno.EMFILE, 'Too many open files'),
    # Resolver and TLS codes are not OS errnos (on macOS EAI_NONAME and SSL_ERROR_EOF are both 8, as is ENOEXEC).
    socket.gaierror(8, 'nodename nor servname provided, or not known'), ssl.SSLEOFError(8, 'EOF occurred in violation of protocol'),
    pymysql.err.OperationalError(2013, 'Lost connection to MySQL server during query'), pymysql.err.InterfaceError(0, ''),
    *(pymysql.err.OperationalError(code, 'transient') for code in (1040, 1053, 1205, 1213, 1290, 1836, 2003, 2006, 2055, 4031)),
    httpx.ConnectError('refused'), httpx.ReadTimeout('slow'), EndpointConnectionError(endpoint_url=S3),
    ReadTimeoutError(endpoint_url=S3), ClientError({'Error': {'Code': 'SlowDown'}, 'ResponseMetadata': {'HTTPStatusCode': 503}}, 'PutObject'),
    ClientError({'Error': {'Code': 'InternalError'}, 'ResponseMetadata': {'HTTPStatusCode': 500}}, 'GetObject'),
    BoxError('unavailable', status_code=503), BoxError('rate limited', status_code=429),
    wrapped(StorageUnavailable('Artifact upload failed'), EndpointConnectionError(endpoint_url=S3)),
    wrapped(StorageUnavailable('Observation commit interrupted; retry the same phase'),
            pymysql.err.OperationalError(2013, 'Lost connection to MySQL server during query')),
    wrapped(BoxTransportError('Box command transport interrupted; retain the remote handle'), httpx.ConnectError('reset')),
    wrapped(BoxTransportError('Box cleanup remains pending'), BoxError('bad gateway', status_code=502)),
    wrapped(StorageUnavailable('Workflow failure diagnostics could not be retained'),
            wrapped(StorageUnavailable('Artifact upload failed'), ConnectionResetError())),
)
PHASE = (
    StorageUnavailable('Artifact exceeds the configured size limit'), StorageUnavailable('Invalid workspace manifest'),
    StorageUnavailable('Artifact checksum mismatch'), BoxTransportError('Capture size exceeds limit'),
    BoxTransportError('Output contains credential material'), BoxTransportError('Remote cursor is inconsistent'),
    BoxTransportError('Box command did not complete; inspect protected remote diagnostics'),
    FileNotFoundError(errno.ENOENT, 'missing'), PermissionError(errno.EACCES, 'denied'), OSError(errno.ENAMETOOLONG, 'name'),
    gzip.BadGzipFile('Not a gzipped file'), io.UnsupportedOperation('not writable'), shutil.Error('copy failed'),
    OSError(errno.E2BIG, 'Argument list too long'), OSError(errno.EXDEV, 'Invalid cross-device link'),
    OSError(errno.ENOTEMPTY, 'Directory not empty'), OSError(errno.EROFS, 'Read-only file system'),
    wrapped(BoxTransportError('Downloaded ledger is incomplete; retain the remote copy'), FileNotFoundError(errno.ENOENT, 'ledger')),
    wrapped(BoxTransportError('Downloaded ledger is incomplete; retain the remote copy'), ValueError('Incomplete capture binding')),
    wrapped(BoxTransportError('Box command transport interrupted; retain the remote handle'), BoxError('bad request', status_code=400)),
    wrapped(StorageUnavailable('Workflow failure diagnostics could not be retained'),
            StorageUnavailable('Checkpoint exceeds the workspace limit')),
    ClientError({'Error': {'Code': 'AccessDenied'}, 'ResponseMetadata': {'HTTPStatusCode': 403}}, 'GetObject'),
    httpx.UnsupportedProtocol('Request URL has an unsupported protocol'), ValueError('Unknown workflow phase'),
    # pymysql reports every unmapped server error as OperationalError, including deterministic rejections.
    pymysql.err.OperationalError(1153, "Got a packet bigger than 'max_allowed_packet' bytes"),
    pymysql.err.OperationalError(3140, 'Invalid JSON text: "Invalid value." at position 10'),
    pymysql.err.OperationalError(3157, 'The JSON document exceeds the maximum depth'),
    pymysql.err.OperationalError(1045, 'Access denied'), pymysql.err.OperationalError(),
    wrapped(StorageUnavailable('Observation commit interrupted; retry the same phase'),
            pymysql.err.OperationalError(1153, "Got a packet bigger than 'max_allowed_packet' bytes")),
)


class ClassificationTests(unittest.TestCase):
    def test_infrastructure_is_told_apart_from_the_phases_own_failures(self):
        for error in INFRASTRUCTURE:
            with self.subTest(error=repr(error), cause=repr(error.__cause__)): self.assertTrue(infrastructure_error(error))
        for error in PHASE:
            with self.subTest(error=repr(error), cause=repr(error.__cause__)): self.assertFalse(infrastructure_error(error))

    def test_only_an_explicit_cause_classifies_a_wrapper(self):
        try:
            try: raise ConnectionResetError()
            except ConnectionResetError: raise BoxTransportError('Capture size exceeds limit')   # implicit context only
        except BoxTransportError as error: self.assertFalse(infrastructure_error(error))

    def test_release_rejects_an_unknown_cause(self):
        with self.assertRaises(ValueError): state.release(None, {}, 'token', cause='delivery')

    def test_the_infrastructure_window_must_be_positive(self):
        with patch.dict('os.environ', {'REVEAL_WORKFLOW_INFRA_RETRY_SECONDS': '0'}), self.assertRaises(ValueError):
            state.infrastructure_window()

    def test_step_retries_an_unwrapped_error_by_the_error_itself(self):
        # Raw client errors, as AsyncBox.get raises them, and any database session error are retried; retry_phase
        # classifies them. An application error raised from an outage keeps its own outcome.
        for error in (httpx.ConnectError('refused'), httpx.ReadTimeout('slow'), BoxError('unavailable', status_code=503),
                      BoxError('bad request', status_code=400), EndpointConnectionError(endpoint_url=S3),
                      pymysql.err.OperationalError(1153, 'packet'), pymysql.err.InterfaceError(0, '')):
            with self.subTest(error=repr(error)): self.assertTrue(retried(error))
        for error in (ValueError('Unknown workflow phase'), httpx.UnsupportedProtocol('unsupported'),
                      wrapped(ValueError('Malformed remote batch'), httpx.ConnectError('reset'))):
            with self.subTest(error=repr(error)): self.assertFalse(retried(error))


class StepRetryCauseTests(unittest.IsolatedAsyncioTestCase):
    setUp = durable.WorkflowTests.setUp
    new = durable.WorkflowTests.new
    execution = durable.WorkflowTests.execution
    observing = durable.WorkflowTests.observing

    def stall(self, payload):
        """QStash exhausted its retries: the run is gone and the execution is due for reconciliation."""
        with self.repo.transaction() as tx:
            tx.remove('workflow_dispatch', payload['job_id'])
            row = tx.get('execution', payload['job_id']); row['data']['expected_at'] = ''
            tx.put('execution', payload['job_id'], row['owner'], row['data'])

    async def failed_tick(self, engine, payload, expected, index=0):
        with self.assertRaises(expected): await engine.step(payload, index)
        execution = self.execution(payload)
        self.assertEqual((execution['fence'], execution['disposition']), (None, 'retry'))
        return execution

    async def test_each_infrastructure_class_retries_as_delivery(self):
        # The writer gate refuses the completion before any statement: nothing of the tick committed.
        busy = lambda: patch.object(state, 'complete', side_effect=DatabaseBusy('Application database writers are busy'))
        lost = pymysql.err.OperationalError(2013, 'Lost connection to MySQL server during query')
        cases = (
            ('busy writer gate at completion', None, busy, DatabaseBusy),
            ('Box API timeout', TimeoutError('Box poll timed out'), None, TimeoutError),
            ('connection reset', ConnectionResetError(errno.ECONNRESET, 'reset'), None, ConnectionResetError),
            ('Box transport outage', wrapped(BoxTransportError('Box command transport interrupted; retain the remote handle'),
                                             httpx.ConnectError('refused')), None, BoxTransportError),
            ('Box provider 503', wrapped(BoxTransportError('Box command transport interrupted; retain the remote handle'),
                                         BoxError('unavailable', status_code=503)), None, BoxTransportError),
            ('storage outage', wrapped(StorageUnavailable('Artifact download failed'), EndpointConnectionError(endpoint_url=S3)),
             None, StorageUnavailable),
            ('lost database session inside the phase', lost, None, pymysql.err.OperationalError),
        )
        for name, error, context, expected in cases:
            with self.subTest(name):
                job, payload, handle, adapter, engine = self.observing()
                if error is not None: adapter.inspect_once.side_effect = error
                with (context() if context else patch.dict('os.environ', {})):
                    execution = await self.failed_tick(engine, payload, expected)
                self.assertEqual(execution['retry_cause'], 'infrastructure', execution['diagnostic'])
                self.assertIn('Transient infrastructure', execution['diagnostic'])
                with self.repo.read_transaction() as tx: self.assertEqual(tx.get('job', job['id'])['data']['status'], 'running')
                self.stall(payload)
                self.assertEqual(reconcile_stale(self.repo), 1)
                execution = self.execution(payload)
                self.assertEqual((execution['recoveries'], execution['delivery_recoveries'], execution['retry_cause']), (0, 1, None))

    async def test_an_observation_commit_failure_is_classified_by_its_code(self):
        cases = ((pymysql.err.OperationalError(2013, 'Lost connection to MySQL server during query'), StorageUnavailable,
                  'infrastructure'),
                 (pymysql.err.OperationalError(1153, "Got a packet bigger than 'max_allowed_packet' bytes"),
                  pymysql.err.OperationalError, 'step_failure'))
        for error, expected, cause in cases:
            with self.subTest(code=error.args[0]):
                job, payload, handle, adapter, engine = self.observing()
                inspected = []
                async def inspect(box): inspected.append(box); return {**handle, 'cursor': 1}, [], False
                adapter.inspect_once.side_effect = inspect
                put = Transaction.put
                def failing(tx, kind, *args, **kwargs):   # the completion's last write; the release that follows writes normally
                    if kind == 'queue' and inspected:
                        inspected.clear(); raise error
                    return put(tx, kind, *args, **kwargs)
                with patch.object(Transaction, 'put', failing):
                    execution = await self.failed_tick(engine, payload, expected)
                self.assertEqual(execution['retry_cause'], cause)
                self.assertEqual(execution['box']['cursor'], 0)   # the cursor rolled back with the completion

    async def test_a_deterministic_database_rejection_inside_a_phase_spends_the_budget(self):
        for error in (pymysql.err.OperationalError(1153, "Got a packet bigger than 'max_allowed_packet' bytes"),
                      pymysql.err.OperationalError(3140, 'Invalid JSON text: "Invalid value." at position 10')):
            with self.subTest(code=error.args[0]):
                job, payload, handle, adapter, engine = self.observing()
                adapter.inspect_once.side_effect = error
                execution = await self.failed_tick(engine, payload, pymysql.err.OperationalError)
                self.assertEqual(execution['retry_cause'], 'step_failure')
                self.assertNotIn('infrastructure_since', execution)
                self.stall(payload); self.assertEqual(reconcile_stale(self.repo), 1)
                execution = self.execution(payload)
                self.assertEqual((execution['recoveries'], execution.get('delivery_recoveries', 0)), (1, 0))

    def lifecycle(self, error):
        """The observe phase with the real BoxLifecycle, whose AsyncBox.get raises error unwrapped."""
        job, payload, handle, _, _ = self.observing()
        factory = Mock(); factory.get = AsyncMock(side_effect=error)
        adapter = BoxLifecycle(ROOT, environ={'UPSTASH_BOX_API_KEY': 'test-box-key'}, box_factory=factory)
        return job, payload, factory, WorkflowExecution(self.repo, storage=self.store, adapter=adapter)

    async def test_a_raw_box_api_outage_when_connecting_retries_as_infrastructure(self):
        for name, error in (('BoxError 503', BoxError('unavailable', status_code=503)),
                            ('httpx ConnectError', httpx.ConnectError('connection refused')),
                            ('httpx ReadTimeout', httpx.ReadTimeout('read timed out'))):
            with self.subTest(name):
                job, payload, factory, engine = self.lifecycle(error)
                execution = await self.failed_tick(engine, payload, type(error))
                factory.get.assert_awaited_once()
                self.assertEqual(execution['retry_cause'], 'infrastructure', execution['diagnostic'])
                self.assertTrue(execution['capacity_reserved'])
                self.assertFalse(execution.get('cleanup_abandoned'))
                self.assertIsNone(execution.get('failure_code'))
                with self.repo.read_transaction() as tx:
                    self.assertEqual(tx.get('job', job['id'])['data']['status'], 'running')
                    self.assertIsNone(tx.get('workflow_cleanup', execution.get('cleanup_id') or 'none'))
                self.stall(payload); self.assertEqual(reconcile_stale(self.repo), 1)
                execution = self.execution(payload)
                self.assertEqual((execution['recoveries'], execution['delivery_recoveries'], execution['disposition']),
                                 (0, 1, 'ready'))
                self.assertTrue(execution['capacity_reserved'])

    async def test_a_rejected_box_request_when_connecting_spends_the_budget(self):
        job, payload, factory, engine = self.lifecycle(BoxError('bad request', status_code=400))
        for expected in (1, 1, 1, 0):
            execution = await self.failed_tick(engine, payload, BoxError)
            self.assertEqual(execution['retry_cause'], 'step_failure')
            self.assertIn('Phase failed', execution['diagnostic'])
            self.stall(payload); self.assertEqual(reconcile_stale(self.repo), expected)
            execution = self.execution(payload); payload['generation'] = execution['generation']
        self.assertEqual((execution['disposition'], execution['recoveries'], execution.get('delivery_recoveries', 0)),
                         ('recovery_required', 3, 0))
        self.assertFalse(execution['capacity_reserved'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'failed')
            self.assertTrue(state.has_cleanup_handoff(tx, execution))

    async def test_an_application_error_raised_from_an_outage_keeps_its_outcome(self):
        job, payload, handle, adapter, engine = self.observing()
        adapter.inspect_once.side_effect = wrapped(ValueError('Malformed remote batch'), httpx.ConnectError('reset'))
        with self.assertRaises(ValueError): await engine.step(payload, 0)
        execution = self.execution(payload)
        self.assertEqual((execution['disposition'], execution['failure_code'], execution.get('retry_cause')),
                         ('recovery_required', 'WORKFLOW_STEP_FAILED', None))

    async def test_the_phases_own_failures_spend_the_budget(self):
        cases = (
            ('inconsistent remote cursor', BoxTransportError('Remote cursor is inconsistent'), BoxTransportError),
            ('capture over its limit', BoxTransportError('Capture size exceeds limit'), BoxTransportError),
            ('workspace over its limit', StorageUnavailable('Checkpoint exceeds the workspace limit'), StorageUnavailable),
            ('rejected Box request', wrapped(BoxTransportError('Box command transport interrupted; retain the remote handle'),
                                             BoxError('bad request', status_code=400)), BoxTransportError),
            ('missing local file', FileNotFoundError(errno.ENOENT, 'missing'), FileNotFoundError),
        )
        for name, error, expected in cases:
            with self.subTest(name):
                job, payload, handle, adapter, engine = self.observing()
                adapter.inspect_once.side_effect = error
                execution = await self.failed_tick(engine, payload, expected)
                self.assertEqual(execution['retry_cause'], 'step_failure')
                self.assertIn('Phase failed', execution['diagnostic'])
                self.stall(payload)
                self.assertEqual(reconcile_stale(self.repo), 1)
                execution = self.execution(payload)
                self.assertEqual((execution['recoveries'], execution.get('delivery_recoveries', 0)), (1, 0))

    async def test_an_expired_step_deadline_is_the_phases_own_failure(self):
        job, payload, handle, adapter, engine = self.observing()
        async def hang(box): await asyncio.sleep(30)
        adapter.inspect_once.side_effect = hang
        with patch.dict('os.environ', {'REVEAL_WORKFLOW_STEP_TIMEOUT_SECONDS': '1'}):
            execution = await self.failed_tick(engine, payload, TimeoutError)
        self.assertEqual(execution['retry_cause'], 'step_failure')

    async def test_recurring_outages_within_the_window_do_not_exhaust_but_phase_failures_do(self):
        job, payload, handle, adapter, engine = self.observing()
        outage = wrapped(StorageUnavailable('Artifact download failed'), EndpointConnectionError(endpoint_url=S3))
        adapter.inspect_once.side_effect = outage
        for attempt in range(1, 6):   # more than REVEAL_WORKFLOW_MAX_RECOVERIES (3)
            await self.failed_tick(engine, payload, StorageUnavailable)
            self.stall(payload); self.assertEqual(reconcile_stale(self.repo), 1)
            execution = self.execution(payload); payload['generation'] = execution['generation']
            self.assertEqual((execution['recoveries'], execution['delivery_recoveries']), (0, attempt))
        with self.repo.read_transaction() as tx: self.assertEqual(tx.get('job', job['id'])['data']['status'], 'running')
        self.assertTrue(execution['capacity_reserved'])
        adapter.inspect_once.side_effect = BoxTransportError('Remote cursor is inconsistent')
        for expected in (1, 1, 1, 0):
            await self.failed_tick(engine, payload, BoxTransportError)
            self.stall(payload); self.assertEqual(reconcile_stale(self.repo), expected)
            execution = self.execution(payload); payload['generation'] = execution['generation']
        self.assertEqual((execution['disposition'], execution['recoveries'], execution['delivery_recoveries']),
                         ('recovery_required', 3, 5))
        self.assertFalse(execution['capacity_reserved'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'failed')
            self.assertTrue(state.has_cleanup_handoff(tx, execution))

    def backdate(self, payload, seconds):
        with self.repo.transaction() as tx:
            row = tx.get('execution', payload['job_id']); row['data']['infrastructure_since'] = state.after(-seconds)
            tx.put('execution', payload['job_id'], row['owner'], row['data'])

    async def test_an_outage_longer_than_the_window_spends_the_budget_and_hands_off_the_box(self):
        # A Box whose API keeps failing: without a bound the phase would retry forever, holding its reservation.
        job, payload, factory, engine = self.lifecycle(BoxError('bad gateway', status_code=502))
        with patch.dict('os.environ', {'REVEAL_WORKFLOW_INFRA_RETRY_SECONDS': '600'}):
            execution = await self.failed_tick(engine, payload, BoxError)
            self.assertEqual(execution['retry_cause'], 'infrastructure')
            since = execution['infrastructure_since']
            self.stall(payload); self.assertEqual(reconcile_stale(self.repo), 1)
            execution = self.execution(payload); payload['generation'] = execution['generation']
            self.assertEqual(execution['infrastructure_since'], since)   # a re-fenced generation keeps the window
            self.backdate(payload, 599)
            execution = await self.failed_tick(engine, payload, BoxError)
            self.assertEqual(execution['retry_cause'], 'infrastructure')
            self.stall(payload); self.assertEqual(reconcile_stale(self.repo), 1)
            execution = self.execution(payload); payload['generation'] = execution['generation']
            self.backdate(payload, 601)
            for expected in (1, 1, 1, 0):
                execution = await self.failed_tick(engine, payload, BoxError)
                self.assertEqual(execution['retry_cause'], 'step_failure')
                self.assertIn('Infrastructure unavailable since', execution['diagnostic'])
                self.stall(payload); self.assertEqual(reconcile_stale(self.repo), expected)
                execution = self.execution(payload); payload['generation'] = execution['generation']
        self.assertEqual((execution['disposition'], execution['failure_code'], execution['recoveries'],
                          execution['delivery_recoveries']), ('recovery_required', 'WORKFLOW_RECOVERY_EXHAUSTED', 3, 2))
        self.assertFalse(execution['capacity_reserved'])
        self.assertTrue(execution['cleanup_abandoned'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'failed')
            self.assertTrue(state.has_cleanup_handoff(tx, execution))
            self.assertEqual(tx.get('workflow_cleanup', execution['cleanup_id'])['data']['box_id'], 'box')

    def capturing(self):
        """A capture-phase job whose Box API answers 502 when its output is captured to the store."""
        job, payload = self.new('paragraph')
        handle = {'box_id': 'box', 'job_id': job['id'], 'attempt': 1, 'phase': 'terminal', 'cursor': 3,
                  'capture_protocol': 's3-v1', 'state': {'status': 'succeeded'}}
        with self.repo.transaction() as tx:
            execution = tx.get('execution', job['id'])['data']
            execution.update(phase='capture', box=handle, capacity_reserved=True, workspace={'ref': 'w'})
            tx.put('execution', job['id'], 'owner', execution)
            queue = tx.get('queue', job['id'])['data']; queue['dispatch_input'] = {'selected_graphs': [], 'sha256': 'x'}
            tx.put('queue', job['id'], 'owner', queue)
            current = tx.get('job', job['id'])['data']; current.update(status='running', stage='authoring_paragraph')
            tx.put('job', job['id'], 'owner', current)
        adapter = Mock(); adapter.capture_to_store = AsyncMock(side_effect=BoxError('bad gateway', status_code=502))
        return job, payload, adapter, WorkflowExecution(self.repo, storage=self.store, adapter=adapter)

    async def test_a_deferred_or_busy_reschedule_keeps_the_infrastructure_window(self):
        # A reschedule is not a completion. Were a deferral to restart the window, a Box API that keeps failing
        # on a busy system would retry as infrastructure forever while its job held the Box reservation.
        job, payload, adapter, engine = self.capturing()
        others = [self.new('paragraph')[0]['id'] for _ in range(2)]
        def capture_leases(until):
            with self.repo.transaction() as tx:
                for identity in others:
                    row = tx.get('execution', identity); row['data'].update(phase='capture', lease_until=until)
                    tx.put('execution', identity, row['owner'], row['data'])
        with patch.dict('os.environ', {'REVEAL_MAX_CAPTURE_STEPS': '2'}), patch('reveal_backend.acceptance.prewarm'):
            execution = await self.failed_tick(engine, payload, BoxError)
            self.assertEqual(execution['retry_cause'], 'infrastructure')
            self.backdate(payload, 7200)   # failing for two hours, past the default 3600-second window
            since = self.execution(payload)['infrastructure_since']
            capture_leases(state.after(600))   # the capture group is full: the retry is deferred
            self.assertEqual(await engine.step(payload, 0), {'phase': 'capture', 'index': 1, 'sleep': 10, 'done': False})
            self.assertEqual(self.execution(payload)['infrastructure_since'], since)
            capture_leases(None)
            # A busy resource (here, cleanup owning the Box) reschedules the phase the same way.
            adapter.capture_to_store.side_effect = state.StepBusy('Cleanup already owns its Box')
            self.assertEqual(await engine.step(payload, 1), {'phase': 'capture', 'index': 2, 'sleep': 10, 'done': False})
            execution = self.execution(payload)
            self.assertEqual((execution['infrastructure_since'], execution['phase_index']), (since, 2))
            adapter.capture_to_store.side_effect = BoxError('bad gateway', status_code=502)
            for expected in (1, 1, 1, 0):
                execution = await self.failed_tick(engine, payload, BoxError, index=2)
                self.assertEqual(execution['retry_cause'], 'step_failure')
                self.assertIn('Infrastructure unavailable since ' + since, execution['diagnostic'])
                self.stall(payload); self.assertEqual(reconcile_stale(self.repo), expected)
                execution = self.execution(payload); payload['generation'] = execution['generation']
        self.assertEqual((execution['disposition'], execution['failure_code'], execution['recoveries'],
                          execution.get('delivery_recoveries', 0)), ('recovery_required', 'WORKFLOW_RECOVERY_EXHAUSTED', 3, 0))
        self.assertFalse(execution['capacity_reserved'])
        self.assertTrue(execution['cleanup_abandoned'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'failed')
            self.assertTrue(state.has_cleanup_handoff(tx, execution))
            self.assertEqual(tx.get('workflow_cleanup', execution['cleanup_id'])['data']['box_id'], 'box')

    async def test_a_completed_phase_starts_a_new_infrastructure_window(self):
        job, payload, handle, adapter, engine = self.observing()
        adapter.inspect_once.side_effect = httpx.ConnectError('refused')
        execution = await self.failed_tick(engine, payload, httpx.ConnectError)
        self.assertEqual(execution['retry_cause'], 'infrastructure')
        self.backdate(payload, 7200)   # an outage that has since ended
        adapter.inspect_once.side_effect = None
        self.assertEqual((await engine.step(payload, 0))['phase'], 'observe')
        execution = self.execution(payload)
        self.assertNotIn('infrastructure_since', execution)
        self.assertIsNone(execution['retry_cause'])
        adapter.inspect_once.side_effect = httpx.ConnectError('refused')
        with self.assertRaises(httpx.ConnectError): await engine.step(payload, 1)   # the next tick
        execution = self.execution(payload)
        self.assertEqual(execution['retry_cause'], 'infrastructure')
        self.assertGreater(execution['infrastructure_since'], state.after(-60))


if __name__ == '__main__': unittest.main()
