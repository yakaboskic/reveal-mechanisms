"""Only a phase's own failures spend the recovery budget; infrastructure retries are delivery-class."""
import asyncio
import errno
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
import httpx
import pymysql
from upstash_box.errors import BoxError

from reveal_backend import workflow_state as state
from reveal_backend.artifact_store import StorageUnavailable
from reveal_backend.box_adapter import BoxTransportError
from reveal_backend.repository import DatabaseBusy, FenceBusy, Transaction
from reveal_backend.workflow_execution import infrastructure_error
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
    OSError(errno.ENOSPC, 'No space left on device'), OSError('write failed'),
    pymysql.err.OperationalError(2013, 'Lost connection to MySQL server during query'), pymysql.err.InterfaceError(0, ''),
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
    wrapped(BoxTransportError('Downloaded ledger is incomplete; retain the remote copy'), FileNotFoundError(errno.ENOENT, 'ledger')),
    wrapped(BoxTransportError('Downloaded ledger is incomplete; retain the remote copy'), ValueError('Incomplete capture binding')),
    wrapped(BoxTransportError('Box command transport interrupted; retain the remote handle'), BoxError('bad request', status_code=400)),
    wrapped(StorageUnavailable('Workflow failure diagnostics could not be retained'),
            StorageUnavailable('Checkpoint exceeds the workspace limit')),
    ClientError({'Error': {'Code': 'AccessDenied'}, 'ResponseMetadata': {'HTTPStatusCode': 403}}, 'GetObject'),
    httpx.UnsupportedProtocol('Request URL has an unsupported protocol'), ValueError('Unknown workflow phase'),
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

    async def failed_tick(self, engine, payload, expected):
        with self.assertRaises(expected): await engine.step(payload, 0)
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

    async def test_lost_observation_commit_is_infrastructure(self):
        job, payload, handle, adapter, engine = self.observing()
        inspected = []
        async def inspect(box): inspected.append(box); return {**handle, 'cursor': 1}, [], False
        adapter.inspect_once.side_effect = inspect
        put = Transaction.put
        def failing(tx, kind, *args, **kwargs):   # the completion's last write; the release that follows writes normally
            if kind == 'queue' and inspected:
                inspected.clear(); raise pymysql.err.OperationalError(2013, 'Lost connection to MySQL server during query')
            return put(tx, kind, *args, **kwargs)
        with patch.object(Transaction, 'put', failing):
            execution = await self.failed_tick(engine, payload, StorageUnavailable)
        self.assertEqual(execution['retry_cause'], 'infrastructure')

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

    async def test_recurring_outages_never_exhaust_but_phase_failures_do(self):
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


if __name__ == '__main__': unittest.main()
