"""Durable bounded stream batches preserve individual events across recovery."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import jobs
from reveal_backend.agent_execution import ExecutionResult, MAX_EMIT_BATCH_BYTES, MAX_EMIT_BATCH_EVENTS
from reveal_backend.box_adapter import BoxTransportError
from reveal_backend.repository import Repository, Transaction, digest, uid
from reveal_backend.worker import Worker, persist_activity_batch


def remote(sequence, text=None, kind='agent_message', **values):
    return (kind, {'remote_sequence': sequence, 'remote_stream_id': 'box:job:1',
                   'text': text or str(sequence), 'delta': True, **values})


class EventBatchTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.repo=Repository(str(self.root/'application.sqlite')); self.repo.migrate()
        self.owner=uid()
        with self.repo.transaction() as tx:
            self.job=jobs.enqueue(tx,self.owner,'analysis',request_id=uid())
        self.job,self.queue=jobs.claim(self.repo,'first-worker')

    def persist(self, events, token=None):
        persist_activity_batch(self.repo,self.job['id'],token or self.queue['token'],events)

    def delivered(self):
        with self.repo.read_transaction() as tx:
            rows=[row['data'] for row in tx.list('event',self.owner)]
            return sorted([row for row in rows if row.get('detail')],key=lambda row:int(row['id']))

    def test_partial_overlap_and_changed_batch_boundaries_preserve_order_and_individual_ids(self):
        self.persist([remote(i) for i in (1,2,3)])
        self.persist([remote(i) for i in (2,3,4,4,5)])
        self.persist([remote(i) for i in (1,2)])
        self.persist([remote(i) for i in (3,4,5)])
        rows=self.delivered()
        self.assertEqual([row['message'] for row in rows],['1','2','3','4','5'])
        self.assertEqual([row['id'] for row in rows],['3','4','5','6','7'])
        self.assertTrue(all(row['detail']['message_delta'] for row in rows))
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('remote_event',self.owner)),5)
            self.assertEqual(tx.get('job',self.job['id'])['data']['last_event_id'],'7')

    def test_failure_after_bulk_insert_rolls_back_dedupe_events_and_job_together(self):
        with patch.object(Transaction,'update_existing',side_effect=OSError('Lost before commit')):
            with self.assertRaises(OSError): self.persist([remote(1),remote(2)])
        self.assertEqual(self.delivered(),[])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('remote_event',self.owner),[])
            self.assertEqual(tx.get('job',self.job['id'])['data']['last_event_id'],'2')
        self.persist([remote(1),remote(2)])
        self.assertEqual([row['id'] for row in self.delivered()],['3','4'])

    def test_stale_attempt_cannot_acknowledge_or_publish_batch(self):
        with self.repo.transaction() as tx:
            queue=tx.get('queue',self.job['id'])['data']; queue['lease_until']='2000-01-01T00:00:00Z'
            tx.put('queue',self.job['id'],self.owner,queue)
        _,current=jobs.claim(self.repo,'replacement-worker')
        with self.assertRaisesRegex(RuntimeError,'lease lost'):
            self.persist([remote(1)])
        self.assertEqual(self.delivered(),[])
        self.persist([remote(1)],current['token'])
        self.assertEqual([row['message'] for row in self.delivered()],['1'])

    def test_uncertain_commit_acknowledgment_replays_without_duplicates(self):
        connect=self.repo.connect
        class LostCommitAcknowledgment:
            def __init__(self): self.connection=connect()
            def __getattr__(self,name): return getattr(self.connection,name)
            def commit(self):
                self.connection.commit()
                raise OSError('Commit succeeded but acknowledgment was lost')
        with patch.object(self.repo,'connect',side_effect=LostCommitAcknowledgment):
            with self.assertRaises(OSError): self.persist([remote(1),remote(2)])
        self.persist([remote(1),remote(2),remote(3)])
        self.assertEqual([row['message'] for row in self.delivered()],['1','2','3'])
        self.assertEqual([row['id'] for row in self.delivered()],['3','4','5'])

    def test_cancel_between_batches_preserves_cleanup_events_and_blocks_success(self):
        self.persist([remote(1)])
        with self.repo.transaction() as tx:
            jobs.cancel(tx,tx.get('job',self.job['id'])['data'])
        # Remote cancellation still needs to drain durable terminal activity.
        self.persist([remote(1),remote(2,kind='agent_completed')])
        rows=self.delivered()
        self.assertEqual([row['status'] for row in rows],['running','cancel_requested'])
        self.assertFalse(jobs.finish(self.repo,self.job['id'],self.queue['token'],'succeeded'))
        self.assertTrue(jobs.finish(self.repo,self.job['id'],self.queue['token'],'cancelled'))
        with self.assertRaisesRegex(RuntimeError,'lease lost'):
            self.persist([remote(3)])

    def test_twenty_events_use_one_bulk_insert_one_job_update_and_constant_sql_count(self):
        original=Transaction.execute
        def capture(statements):
            def execute(tx,sql,params=()):
                statements.append(sql)
                return original(tx,sql,params)
            return execute
        single=[]; batch=[]
        with patch.object(Transaction,'execute',new=capture(single)):
            self.persist([remote(1)])
        with patch.object(Transaction,'execute',new=capture(batch)):
            self.persist([remote(i) for i in range(2,22)])
        # SQLite executes BEGIN separately; Aurora additionally takes its one
        # transaction fence. The per-batch work is constant in either backend.
        # Notification delivery adds a job-state comparison, durable outbox
        # insert and post-publication delete, independent of event batch size.
        self.assertEqual(len(single),9); self.assertEqual(len(batch),9)
        self.assertEqual(sum(sql.startswith('INSERT') for sql in batch),2)
        self.assertEqual(sum(sql.startswith('UPDATE') for sql in batch),1)
        self.assertIn('id IN (',batch[2])
        self.assertEqual(len(self.delivered()),21)

    def test_batch_caps_are_checked_before_any_connection_and_count_utf8_bytes(self):
        with patch.object(self.repo,'connect',side_effect=AssertionError('Oversized batch accessed DB')):
            with self.assertRaises(ValueError):
                self.persist([remote(i) for i in range(MAX_EMIT_BATCH_EVENTS+1)])
            with self.assertRaises(ValueError):
                self.persist([remote(1,'🧬'*(MAX_EMIT_BATCH_BYTES//4))])
        self.assertEqual(self.delivered(),[])

    def test_worker_commit_before_cursor_checkpoint_recovers_without_duplicate_events(self):
        # Use the actual Worker callbacks, frozen paragraph preparation, queue
        # recovery and a fake transport that disconnects after batch commit.
        with self.repo.transaction() as tx:
            jobs.cancel(tx,tx.get('job',self.job['id'])['data'])
        jobs.finish(self.repo,self.job['id'],self.queue['token'],'cancelled')
        account='dapper:ScientificAccount.'+'a'*32
        with self.repo.transaction() as tx:
            tx.put('account',digest([self.owner,account]),self.owner,{'result':{'document':{},'citation_metadata':[]},'summary':{}})
            job=jobs.enqueue(tx,self.owner,'paragraph',account_id=account)
        class InterruptedAdapter:
            async def execute(self,request,emit,cancelled,checkpoint):
                handle={'box_id':'same-paid-box','job_id':request.job_id,'attempt':request.attempt,'cursor':0,'phase':'running'}
                await checkpoint(handle)
                await emit.emit_batch([remote(1),remote(2)])
                original=Transaction.put
                def failed_checkpoint(tx,kind,identity,owner,data,expected=None):
                    if kind=='queue' and data.get('remote_handle',{}).get('cursor')==2:
                        raise OSError('Database disconnected while saving the cursor')
                    return original(tx,kind,identity,owner,data,expected)
                with patch.object(Transaction,'put',new=failed_checkpoint):
                    await checkpoint(dict(handle,cursor=2))
        class ResumedAdapter:
            async def execute(self,request,emit,cancelled,checkpoint):
                self.request=request
                await emit.emit_batch([remote(1),remote(2),remote(3)])
                await checkpoint(dict(request.remote_handle,cursor=3))
                return ExecutionResult('failed',request.output_dir,reason='Intentional fixture finish')
        with patch.dict('os.environ',{'REVEAL_EXECUTION_MODE':'deterministic','REVEAL_ARTIFACTS_DIR':str(self.root)}):
            first=jobs.claim(self.repo,'first-stream-worker')
            asyncio.run(Worker(self.repo,InterruptedAdapter()).process(*first))
            with self.repo.transaction() as tx:
                queue=tx.get('queue',job['id'])['data']
                self.assertEqual(queue['remote_handle']['cursor'],0)
                queue['lease_until']='2000-01-01T00:00:00Z'; tx.put('queue',job['id'],self.owner,queue)
            second=jobs.claim(self.repo,'resumed-stream-worker'); adapter=ResumedAdapter()
            asyncio.run(Worker(self.repo,adapter).process(*second))
        self.assertEqual(first[1]['attempt'],second[1]['attempt'])
        self.assertEqual(adapter.request.remote_handle['box_id'],'same-paid-box')
        self.assertEqual(adapter.request.remote_handle['cursor'],0)
        rows=[row for row in self.delivered() if row['job_id']==job['id'] and row['detail'].get('message_delta')]
        self.assertEqual([row['message'] for row in rows],['1','2','3'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('queue',job['id'])['data']['remote_handle']['cursor'],3)
