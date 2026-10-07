"""Notification durability, authorized replay and absence of idle data polls."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import jwt
from reveal_backend import jobs, workspace_events as events, redis_notifications as notifications
from reveal_backend.auth import Problem
from reveal_backend.repository import Repository, Transaction, digest


class WorkspaceEventsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name)/'app.sqlite')); self.repo.migrate()
        setting = patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET':'s'*40, 'REVEAL_NOTIFICATION_NAMESPACE':'test-notifications',
            'REVEAL_NOTIFICATION_REDIS_URL':'', 'REVEAL_NOTIFICATION_REDIS_REST_URL':'',
            'UPSTASH_REDIS_REST_URL':'', 'UPSTASH_REDIS_REST_TOKEN':'', 'REVEAL_NOTIFICATION_REDIS_REST_TOKEN':'',
            'REVEAL_JOB_RUNNER':'legacy', 'REVEAL_JOB_TRANSPORT':'database'})
        setting.start(); self.addCleanup(setting.stop)
        for owner in ('alice', 'bob'):
            with self.repo.transaction() as tx:
                tx.put('principal', owner, owner, {'retired':False, 'me':{'user_id':owner,'principal_kind':'registered','workspace_expires_at':None}})

    def authorization(self, owner='alice'):
        return 'Bearer '+jwt.encode({'sub':owner,'principal_kind':'registered','iss':'reveal-nextjs','aud':'reveal-api',
            'iat':int(time.time()),'exp':int(time.time())+120,'jti':'test'}, 's'*40, algorithm='HS256')

    def replay(self, owner='alice', positions=None, limit=500):
        return events.replay(self.repo, self.authorization(owner), positions or {'workspace':0,'public':0}, limit)

    def test_rollback_never_publishes_or_retains_a_phantom_change(self):
        with patch.object(notifications, 'publish') as publish:
            with self.assertRaises(RuntimeError):
                with self.repo.transaction() as tx:
                    tx.put('draft','draft-1','alice',{'id':'draft-1'})
                    raise RuntimeError('rollback')
            publish.assert_not_called()
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('draft','draft-1'))
        self.assertFalse(any(item['entity_id']=='draft-1' for item in self.replay()[1]))

    def test_commit_precedes_publish_and_failed_publish_is_reconciled(self):
        def failure(channels):
            with self.repo.read_transaction() as tx:
                self.assertIsNotNone(tx.get('draft','draft-1'))
                self.assertEqual(len(tx.list('notification_outbox')),1)
            raise ConnectionError('injected delivery failure')
        with patch.object(notifications, 'publish', side_effect=failure):
            with self.repo.transaction() as tx: tx.put('draft','draft-1','alice',{'id':'draft-1'})
        with self.repo.read_transaction() as tx: self.assertEqual(len(tx.list('notification_outbox')),1)
        with patch.object(notifications, 'publish') as publish:
            self.assertEqual(events.reconcile_notifications(self.repo),1)
            publish.assert_called_once()
        with self.repo.read_transaction() as tx: self.assertEqual(tx.list('notification_outbox'),[])

    def test_replay_is_ordered_scoped_and_does_not_trust_publish_order(self):
        for owner, identity in [('alice','first'),('bob','private-bob'),('alice','second')]:
            with self.repo.transaction() as tx: tx.put('draft',identity,owner,{'id':identity})
        _, replay, highwater, expired = self.replay()
        self.assertFalse(expired)
        self.assertEqual([item['entity_id'] for item in replay],['alice','first','second'])
        self.assertEqual([item['cursor'] for item in replay],['1','2','3'])
        self.assertEqual(self.replay(positions=highwater)[1],[])
        cursor = events.encode_cursor('alice',highwater)
        self.assertEqual(events.decode_cursor('alice',cursor),highwater)
        with self.assertRaises(Problem): events.decode_cursor('bob',cursor)
        with patch.dict(os.environ, {'REVEAL_NOTIFICATION_NAMESPACE':'another-env'}):
            with self.assertRaises(Problem): events.decode_cursor('alice',cursor)

    def test_retention_limit_and_missing_sequence_require_resync(self):
        with self.repo.transaction() as tx:
            tx.put('draft','a','alice',{'id':'a'}); tx.put('draft','b','alice',{'id':'b'})
        self.assertTrue(self.replay(limit=1)[3])
        with self.repo.transaction() as tx:
            tx.remove('workspace_event',digest('workspace:alice')+':'+str(2).zfill(20))
        self.assertTrue(self.replay()[3])

    def test_public_invalidations_never_include_private_identifiers(self):
        with self.repo.transaction() as tx:
            tx.put('publication','secret-key','alice',{'account_id':'private-account-id','visibility':'private'})
        _, replay, _, _ = self.replay('bob')
        public = [item for item in replay if item['scope']=='public']
        self.assertEqual(len(public),1)
        self.assertNotIn('private-account-id',json.dumps(public))
        self.assertNotIn('secret-key',json.dumps(public))

    def test_only_a_reference_cutover_names_its_public_catalog_event(self):
        # Open composers recheck their anchors on `reference` catalog events only, never on publishes or votes;
        # a publish or vote committed with a cutover keeps the cutover's name.
        with self.repo.transaction() as tx:
            tx.put('publication','p1','alice',{'account_id':'a1','visibility':'public'})
        _, replay, cursor, _ = self.replay('bob')
        self.assertEqual([item['entity_id'] for item in replay if item['scope']=='public'],['catalog'])
        with self.repo.transaction() as tx:
            tx.put('vector_active','local','catalog',{'snapshot_id':'s2'})
            tx.put('outcome_publication','o1','alice',{'visibility':'public'})
            tx.put('vote','b1','alice',{'target_kind':'gap','target_id':'g1','gap_id':'g1','vote':1})
        public = [item for item in self.replay('bob',positions=cursor)[1] if item['scope']=='public']
        self.assertEqual([(item['entity_id'],item['collections']) for item in public],[('reference',['catalog','accounts','gaps','explorations'])])

    def test_vote_commit_pushes_scoped_workspace_and_anonymous_catalog_invalidation(self):
        before_alice, before_bob = self.replay('alice')[2], self.replay('bob')[2]
        ballot = {'target_kind':'account', 'target_id':'scientific-target', 'gap_id':'related-gap', 'vote':1}
        with patch.object(notifications, 'publish') as publish:
            with self.repo.transaction() as tx:
                tx.put('vote', 'private-ballot', 'alice', ballot)
                tx.put('vote_total', 'total', 'system', {**ballot, 'upvotes':1, 'downvotes':0})
        mine = self.replay('alice', before_alice)[1]
        others = self.replay('bob', before_bob)[1]
        self.assertEqual([item['event_type'] for item in mine], ['workspace.changed', 'catalog.updated'])
        self.assertEqual(mine[0]['collections'], ['gaps', 'accounts'])
        self.assertEqual(len(others), 1)
        self.assertEqual(others[0]['scope'], 'public')
        self.assertEqual(others[0]['entity_id'], 'catalog')
        for private in ('alice', 'scientific-target', 'related-gap', 'private-ballot'):
            self.assertNotIn(private, json.dumps(others))
        self.assertEqual(set(publish.call_args.args[0]),
            {notifications.channel('workspace:alice'), notifications.channel('public')})
        with patch.object(notifications, 'publish') as duplicate:
            with self.repo.transaction() as tx: tx.put('vote', 'private-ballot', 'alice', ballot)
        duplicate.assert_not_called()

    def test_rolled_back_vote_never_changes_public_or_workspace_replay(self):
        before = self.replay()[2]
        with patch.object(notifications, 'publish') as publish:
            with self.assertRaises(RuntimeError):
                with self.repo.transaction() as tx:
                    tx.put('vote', 'private-ballot', 'alice', {'target_kind':'gap', 'target_id':'gap', 'gap_id':'gap', 'vote':-1})
                    raise RuntimeError('rollback')
        publish.assert_not_called()
        self.assertEqual(self.replay(positions=before)[1], [])

    def test_detailed_job_events_wake_job_stream_without_workspace_invalidation(self):
        with self.repo.transaction() as tx: job = jobs.enqueue(tx,'alice','analysis')
        before = self.replay()[2]
        with patch.object(notifications,'publish') as publish:
            with self.repo.transaction() as tx: jobs.event(tx,job,'activity','A tool output')
        self.assertEqual(self.replay(positions=before)[1],[])
        self.assertEqual(publish.call_args.args[0],[notifications.channel('job:'+job['id'])])
        with self.repo.transaction() as tx:
            job['status']='running'; jobs.event(tx,job,'status','Started')
        self.assertEqual([item['event_type'] for item in self.replay(positions=before)[1]],['job.updated'])

    def test_local_work_lifecycle_invalidates_runs_for_its_owner_only(self):
        before, other = self.replay()[2], self.replay('bob')[2]
        work = {'id': 'local-run', 'state': 'preparing', 'package_id': None,
            'package_sha256': None, 'last_error': None, 'last_activity': 'created'}
        with self.repo.transaction() as tx:
            tx.put('local_work', work['id'], 'alice', work)
            tx.put('research_operation', 'prepare', 'alice', {'id': 'prepare',
                'kind': 'prepare', 'local_work_id': work['id'], 'state': 'received'})
        for update in (
                {'state': 'preparation_failed', 'last_error': {'detail': 'private failure'}},
                {'state': 'preparing', 'last_error': None},
                {'state': 'ready', 'package_id': 'private-package', 'package_sha256': 'private-hash'},
                {'state': 'closed', 'closed_at': 'closed'}):
            work.update(update)
            with self.repo.transaction() as tx: tx.put('local_work', work['id'], 'alice', work)
        replay = self.replay(positions=before)[1]
        self.assertEqual(len(replay), 5)
        self.assertTrue(all(item['event_type'] == 'workspace.changed' and item['collections'] == ['jobs']
            and item['entity_id'] == work['id'] and item['scope'] == 'workspace' for item in replay))
        self.assertEqual(self.replay('bob', positions=other)[1], [])
        for private in ('private failure', 'private-package', 'private-hash'):
            self.assertNotIn(private, json.dumps(replay))

    def test_local_activity_and_tool_records_do_not_invalidate_runs(self):
        work = {'id': 'local-run', 'state': 'ready', 'last_activity': 'created'}
        with self.repo.transaction() as tx: tx.put('local_work', work['id'], 'alice', work)
        before = self.replay()[2]
        with patch.object(notifications, 'publish') as publish:
            with self.repo.transaction() as tx:
                work.update(last_activity='later', last_action='data_query')
                tx.put('local_work', work['id'], 'alice', work)
                for kind in ('query', 'import', 'prepare'):
                    tx.put('research_operation', kind, 'alice', {'id': kind,
                        'kind': kind, 'local_work_id': work['id'], 'state': 'succeeded'})
                for kind in ('research_access', 'evidence_receipt', 'reuse_receipt'):
                    tx.put(kind, kind, 'alice', {'local_work_id': work['id'], 'token': 'private-token'})
            publish.assert_not_called()
        self.assertEqual(self.replay(positions=before)[1], [])

    def test_local_submissions_invalidate_runs_without_exposing_payloads_or_lease_heartbeats(self):
        with self.repo.transaction() as tx:
            tx.put('local_work', 'local-run', 'alice', {'id': 'local-run', 'state': 'ready'})
        before, other = self.replay()[2], self.replay('bob')[2]
        for kind, terminal in (('validate', 'rejected'), ('submit', 'accepted')):
            operation = {'id': kind, 'kind': kind, 'local_work_id': 'local-run', 'state': 'received',
                'arguments': {'token': 'private-token', 'content': 'private-science'}, 'grant_id': 'private-grant'}
            for state in ('received', 'running', terminal):
                operation['state'] = state
                if state == terminal:
                    operation.update(report={'detail': 'private-report'}, account_ids=['private-account'])
                with self.repo.transaction() as tx: tx.put('research_operation', kind, 'alice', operation)
                cursor = self.replay()[2]
                with patch.object(notifications, 'publish') as publish:
                    operation.update(lease_token='private-lease', lease_until=state, attempt=3)
                    with self.repo.transaction() as tx: tx.put('research_operation', kind, 'alice', operation)
                    publish.assert_not_called()
                self.assertEqual(self.replay(positions=cursor)[1], [])
        replay = self.replay(positions=before)[1]
        self.assertEqual([item['entity_id'] for item in replay], ['validate'] * 3 + ['submit'] * 3)
        self.assertTrue(all(item['collections'] == ['jobs'] and item['scope'] == 'workspace' for item in replay))
        self.assertEqual(self.replay('bob', positions=other)[1], [])
        for private in ('private-token', 'private-science', 'private-grant', 'private-report', 'private-account', 'private-lease'):
            self.assertNotIn(private, json.dumps(replay))

    def test_enqueued_submission_inserts_without_a_pre_read_and_still_invalidates_runs(self):
        from reveal_backend.research_work import ResearchWorkService
        work = {'id': 'local-run', 'state': 'ready', 'research_request_id': 'request'}
        with self.repo.transaction() as tx: tx.put('local_work', 'local-run', 'alice', work)
        before = self.replay()[2]
        with self.repo.transaction() as tx:
            submission = ResearchWorkService(self.repo).enqueue(tx, 'alice', work, 'validate', {}, None)
            query = ResearchWorkService(self.repo).enqueue(tx, 'alice', work, 'query', {}, None)
        self.assertEqual([item['entity_id'] for item in self.replay(positions=before)[1]], [submission['id']])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('research_operation', query['id'])['version'], 1)

    def test_hosted_or_unowned_research_operations_do_not_emit_local_run_events(self):
        before, other = self.replay()[2], self.replay('bob')[2]
        with patch.object(notifications, 'publish') as publish:
            with self.repo.transaction() as tx:
                tx.put('local_work', 'hosted', 'alice', {'id': 'hosted', 'job_id': 'job', 'state': 'preparing'})
                tx.put('research_operation', 'hosted-submission', 'alice', {'id': 'hosted-submission',
                    'local_work_id': 'hosted', 'kind': 'submit', 'state': 'accepted'})
                tx.put('research_operation', 'missing-parent', 'alice', {'id': 'missing-parent',
                    'local_work_id': 'missing', 'kind': 'submit', 'state': 'accepted'})
            publish.assert_not_called()
        self.assertEqual(self.replay(positions=before)[1], [])
        self.assertEqual(self.replay('bob', positions=other)[1], [])
        with self.repo.transaction() as tx:
            tx.put('local_work', 'bob-run', 'bob', {'id': 'bob-run', 'state': 'ready'})
        before, other = self.replay()[2], self.replay('bob')[2]
        with self.repo.transaction() as tx:
            tx.put('research_operation', 'foreign-parent', 'alice', {'id': 'foreign-parent',
                'local_work_id': 'bob-run', 'kind': 'submit', 'state': 'accepted'})
        self.assertEqual(self.replay(positions=before)[1], [])
        self.assertEqual(self.replay('bob', positions=other)[1], [])

    def test_retired_identity_cannot_replay_previously_authorized_cursor(self):
        with self.repo.transaction() as tx:
            row=tx.get('principal','alice'); row['data']['retired']=True
            tx.put('principal','alice','alice',row['data'])
        with self.assertRaises(Problem): self.replay()

    def test_bookkeeping_is_one_cursor_read_one_update_per_existing_scope_and_one_insert(self):
        statements, prepare, execute = [], events.prepare_commit, Transaction.execute
        def counted(tx):
            with patch.object(Transaction, 'execute', lambda self, sql, params=(): statements.append(sql.split()[0]) or execute(self, sql, params)):
                return prepare(tx)
        before, other = self.replay()[2], self.replay('bob')[2]
        with patch.object(events, 'prepare_commit', counted), patch.object(notifications, 'publish') as publish:
            with self.repo.transaction() as tx:
                for index in range(5): tx.put('draft', 'd%d' % index, 'alice', {'id':'d%d' % index})
                tx.put('publication', 'p', 'alice', {'account_id':'a', 'visibility':'public'})   # first public event
        self.assertEqual(statements, ['SELECT', 'UPDATE', 'INSERT'])   # alice's cursor exists; public's is new
        self.assertEqual(set(publish.call_args.args[0]), {notifications.channel('workspace:alice'), notifications.channel('public')})
        mine = self.replay(positions=before)[1]
        self.assertEqual([item['cursor'] for item in mine if item['scope'] == 'workspace'], [str(before['workspace']+n) for n in range(1, 7)])
        self.assertEqual([(item['cursor'], item['entity_id']) for item in self.replay('bob', positions=other)[1]], [('1', 'catalog')])
        with self.repo.read_transaction() as tx:
            cursor = tx.get('workspace_cursor', digest('workspace:alice'))
            self.assertEqual((cursor['data'], cursor['version']), ({'sequence':before['workspace']+6, 'oldest':1}, 2))
            self.assertEqual(tx.get('workspace_cursor', digest('public'))['data'], {'sequence':1, 'oldest':1})
            self.assertEqual(tx.list('notification_outbox'), [])

    def test_an_event_key_that_exists_after_all_keeps_the_overwrite(self):
        with self.repo.transaction() as tx: tx.remove('workspace_cursor', digest('workspace:alice'))   # sequence restarts at 1
        with self.repo.transaction() as tx: tx.put('draft', 'again', 'alice', {'id':'again'})
        _, replay, highwater, _ = self.replay()
        self.assertEqual(highwater['workspace'], 1)
        self.assertEqual([(item['cursor'], item['entity_id']) for item in replay if item['scope'] == 'workspace'], [('1', 'again')])

    def test_delete_and_bulk_mutations_produce_committed_events(self):
        with self.repo.transaction() as tx: tx.insert_many([('draft','first','alice',{'id':'first'})])
        with self.repo.transaction() as tx: tx.update_existing('draft','first','alice',{'id':'first','changed':True})
        with self.repo.transaction() as tx: tx.remove('draft','first')
        drafts=[item for item in self.replay()[1] if item['entity_id']=='first']
        self.assertEqual([item['operation'] for item in drafts],['upsert','upsert','remove'])
        self.assertEqual([item['entity_revision'] for item in drafts],[1,2,3])


class BackgroundDeliveryTests(unittest.TestCase):
    def setUp(self):
        WorkspaceEventsTests.setUp(self)
        mode = patch.dict(os.environ, {'REVEAL_NOTIFICATION_DELIVERY':'background'}); mode.start(); self.addCleanup(mode.stop)

    def outbox(self):
        with self.repo.read_transaction() as tx: return len(tx.list('notification_outbox'))

    def test_writer_returns_before_the_publish_which_then_deletes_the_row_without_a_second_fence(self):
        release, published, fenced, transaction = threading.Event(), [], [], Repository.transaction
        def publish(channels): release.wait(5); published.append(channels)
        def counted(repo): fenced.append(threading.current_thread().name); return transaction(repo)
        with patch.object(notifications, 'publish', side_effect=publish), patch.object(Repository, 'transaction', counted):
            with self.repo.transaction() as tx: tx.put('draft', 'draft-1', 'alice', {'id':'draft-1'})
            self.assertEqual((published, self.outbox()), ([], 1))   # committed with its outbox row; Redis not awaited
            release.set(); self.assertTrue(events.publisher.flush(5))
        self.assertEqual(published, [[notifications.channel('workspace:alice')]])
        self.assertEqual((self.outbox(), fenced), (0, [threading.current_thread().name]))   # one fence, the writer's

    def test_failed_background_publish_leaves_the_row_for_reconciliation(self):
        with patch.object(notifications, 'publish', side_effect=ConnectionError('down')), self.assertLogs(events.log, 'WARNING'):
            with self.repo.transaction() as tx: tx.put('draft', 'draft-1', 'alice', {'id':'draft-1'})
            self.assertTrue(events.publisher.flush(5))
        self.assertEqual(self.outbox(), 1)
        with patch.object(notifications, 'publish') as publish:
            self.assertEqual(events.reconcile_notifications(self.repo), 1); publish.assert_called_once()
        self.assertEqual(self.outbox(), 0)

    def test_a_burst_shares_one_publish_and_one_delete(self):
        release, calls, discards = threading.Event(), [], []
        discard = Repository.discard_notifications
        def publish(channels): release.wait(5); calls.append(sorted(channels))
        def counted(repo, ids): discards.append(len(ids)); return discard(repo, ids)
        with patch.object(notifications, 'publish', side_effect=publish), patch.object(Repository, 'discard_notifications', counted):
            for index, owner in enumerate(('alice', 'bob', 'alice')):   # the first holds the thread; two queue behind it
                with self.repo.transaction() as tx: tx.put('draft', 'd%d' % index, owner, {'id':'d%d' % index})
            release.set(); self.assertTrue(events.publisher.flush(5))
        self.assertEqual(len(calls), len(discards))
        self.assertEqual(sum(discards), 3)
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(self.outbox(), 0)

    def test_full_queue_and_forked_process_leave_rows_for_reconciliation(self):
        started, release, delivered = threading.Event(), threading.Event(), []
        def deliver(repository, pending): started.set(); release.wait(5); delivered.append(pending)
        publisher = events.Publisher(limit=1)
        with patch.object(events, 'deliver', deliver):
            self.assertTrue(publisher.submit(self.repo, ['a'])); self.assertTrue(started.wait(5))
            self.assertTrue(publisher.submit(self.repo, ['b']))
            self.assertEqual(publisher.backlog(), 1)
            with self.assertLogs(events.log, 'WARNING'): self.assertFalse(publisher.submit(self.repo, ['c']))
            release.set(); self.assertTrue(publisher.flush(5))
            first = publisher.thread
            publisher.pid = -1   # as after fork: the parent's queue and thread are not this process's
            self.assertTrue(publisher.flush(0))
            self.assertTrue(publisher.submit(self.repo, ['d'])); self.assertTrue(publisher.flush(5))
            self.assertIsNot(publisher.thread, first)
        self.assertEqual(delivered, [['a'], ['b'], ['d']])
        publisher._after_fork(); self.assertTrue(publisher.lock.acquire(blocking=False))

    def test_queue_full_still_commits_and_reports_nothing_delivered(self):
        with patch.object(events.publisher, 'submit', return_value=False), patch.object(notifications, 'publish') as publish:
            with self.repo.transaction() as tx: tx.put('draft', 'draft-1', 'alice', {'id':'draft-1'})
        publish.assert_not_called()
        self.assertEqual(self.outbox(), 1)
        self.assertEqual(events.reconcile_notifications(self.repo), 1)


class DiscardWireTests(unittest.TestCase):
    def test_discard_is_unfenced_read_committed_and_resets_its_pooled_lease(self):
        import test_mysql_pool as wire
        from reveal_backend import repository
        from reveal_backend.mysql_database import application_session_unchanged, reset_application_session
        from reveal_backend.mysql_pool import Pool
        class Cursor(wire.FakeCursor): rowcount = 2
        class Connection(wire.StatusConnection):
            def cursor(self, *args): return Cursor(self)
        created = []
        def factory(): created.append(Connection()); return created[-1]
        pool = Pool(factory, lambda c: reset_application_session(c, 'cyaka_expected'), unchanged=application_session_unchanged)
        self.addCleanup(pool.close); repo = Repository(); repo.connect = pool.acquire
        with patch.object(repository, 'writer_gate', side_effect=AssertionError('the delete is never fenced')):
            self.assertEqual(repo.discard_notifications(['a', 'b', 'a']), 2)
        sent = created[0].sql
        self.assertEqual(sent[:2], [('SET TRANSACTION ISOLATION LEVEL READ COMMITTED', ()),
            ('DELETE FROM reveal_records WHERE kind=%s AND id IN (%s,%s)', ('notification_outbox', 'a', 'b'))])
        self.assertFalse(any('FOR UPDATE' in sql for sql, _ in sent))
        self.assertEqual(created[0].reset_count, 1)   # SET TRANSACTION is not session-neutral: the lease is reset


class PublishClientTests(unittest.TestCase):
    def test_one_long_lived_rest_client_per_process_and_configuration(self):
        settings = {'UPSTASH_REDIS_REST_URL':'https://notify.invalid', 'UPSTASH_REDIS_REST_TOKEN':'token-1',
            'REVEAL_NOTIFICATION_REDIS_URL':'', 'REVEAL_NOTIFICATION_REDIS_REST_URL':'', 'REVEAL_NOTIFICATION_REDIS_REST_TOKEN':''}
        with patch.dict(os.environ, settings), patch.object(notifications, '_client', (None, None)):
            first = notifications._publisher_client(*notifications.configuration())
            box = []
            worker = threading.Thread(target=lambda: box.append(notifications._publisher_client(*notifications.configuration())))
            worker.start(); worker.join()
            self.assertIs(box[0], first)
            self.assertEqual(first._transport._pool._keepalive_expiry, 55)   # httpx 0.28.1 default is 5 s
            with patch.dict(os.environ, {'UPSTASH_REDIS_REST_TOKEN':'token-2'}):
                rotated = notifications._publisher_client(*notifications.configuration())
            self.assertIsNot(rotated, first); self.assertTrue(first.is_closed)
            rotated.close()

    def test_rest_publish_retries_once_on_a_closed_keepalive_socket(self):
        class Client:
            calls = 0
            def post(self, path, json):
                Client.calls += 1
                if Client.calls == 1: raise httpx.RemoteProtocolError('Server disconnected without sending a response.')
                return httpx.Response(200, json=[{'result':1}], request=httpx.Request('POST', 'https://notify.invalid/pipeline'))
        with patch.object(notifications, 'configuration', return_value=('rest', 'https://notify.invalid', 'token')), \
                patch.object(notifications, '_publisher_client', return_value=Client()):
            notifications.publish(['one'])
            self.assertEqual(Client.calls, 2)
            Client.calls = -5   # a timeout is not retried
            class Slow(Client):
                def post(self, path, json): Client.calls += 1; raise httpx.ReadTimeout('slow')
            with patch.object(notifications, '_publisher_client', return_value=Slow()), self.assertRaises(httpx.ReadTimeout):
                notifications.publish(['one'])
            self.assertEqual(Client.calls, -4)


class PushOnlyStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_keepalive_never_reloads_unchanged_rds_events(self):
        class Listener:
            async def wait(self, timeout): raise asyncio.TimeoutError()
        class Hub:
            @asynccontextmanager
            async def subscribe(self, scopes): yield Listener()
        with patch.object(notifications,'hub',return_value=Hub()), patch.object(events,'stream_deadline',return_value=time.monotonic()+60):
            def read(*args): return {'items':[],'terminal':False}
            async def call(fn, *args): return fn(*args)
            with patch('reveal_backend.workspace_events.asyncio.to_thread',side_effect=call) as read_call:
                stream=events.job_event_stream(None,None,'job','proof',0,100,read)
                self.assertEqual(await anext(stream),': heartbeat\n\n')
                self.assertEqual(await anext(stream),': heartbeat\n\n')
                self.assertEqual(read_call.call_count,1)
                await stream.aclose()

    async def test_one_local_subscription_is_shared_and_overflow_requests_replay(self):
        with patch.object(notifications,'configuration',return_value=('local','','')):
            bridge=notifications.NotificationHub()
            async with bridge.subscribe(['workspace:alice']) as first, bridge.subscribe(['workspace:alice']) as second:
                self.assertEqual(len(bridge.tasks),1)
                self.assertTrue(first.queue.empty())   # the subscribe's own 'replay' is subsumed by the caller's first read
                bridge.wake(notifications.channel('workspace:alice'))
                bridge.wake(notifications.channel('workspace:alice'))
                self.assertEqual(await first.wait(1),'resync')
                self.assertEqual(await second.wait(1),'resync')
            self.assertEqual(len(bridge.tasks),0)
            await bridge.close()

    async def test_job_connect_streams_the_initial_read_then_reads_once_after_the_ack(self):
        reads = []
        def read(job_id, authorization, cursor, limit): reads.append(cursor); return {'items':[], 'terminal':False}
        initial = {'items':[{'id':'1', 'event_type':'status', 'status':'running'}], 'terminal':False}
        with patch.object(notifications,'configuration',return_value=('local','','')), \
                patch.object(events,'stream_deadline',return_value=time.monotonic()+.3):
            output = [chunk async for chunk in events.job_event_stream(None,None,'job','proof',0,100,read,initial)]
        self.assertTrue(output[0].startswith('id: 1\nevent: status\n'))
        self.assertEqual((reads, output[1:]), ([1], [': heartbeat\n\n']))   # the handler's read is not repeated

    async def test_finished_job_connect_streams_the_initial_read_without_subscribing(self):
        class Hub:
            def subscribe(self, scopes): raise AssertionError('a finished job needs no wakeups')
        initial = {'items':[{'id':'1', 'event_type':'status'}, {'id':'2', 'event_type':'completed'}], 'terminal':True}
        with patch.object(notifications,'hub',return_value=Hub()), patch.object(events,'stream_deadline',return_value=time.monotonic()+60):
            output = [chunk async for chunk in events.job_event_stream(None,None,'job','proof',0,100,None,initial)]
        self.assertEqual([chunk.split('\n')[0] for chunk in output], ['id: 1', 'id: 2'])

    async def test_a_wake_during_the_catch_up_read_reads_again(self):
        loop, reads = asyncio.get_running_loop(), []
        def read(job_id, authorization, cursor, limit):
            reads.append(cursor)
            if len(reads) == 1:
                woke = threading.Event()
                loop.call_soon_threadsafe(lambda: (notifications.hub().wake(notifications.channel('job:job')), woke.set()))
                self.assertTrue(woke.wait(5))
            return {'items':[], 'terminal':False}
        with patch.object(notifications,'configuration',return_value=('local','','')), \
                patch.object(events,'stream_deadline',return_value=time.monotonic()+.5):
            await anext(events.job_event_stream(None,None,'job','proof',0,100,read,{'items':[], 'terminal':False}))
        self.assertEqual(reads, [0, 0])


class WorkspaceConnectTests(unittest.IsolatedAsyncioTestCase):
    setUp = WorkspaceEventsTests.setUp
    authorization = WorkspaceEventsTests.authorization

    async def test_workspace_connect_replays_once_before_waiting(self):
        from types import SimpleNamespace
        calls = []
        def replay(repository, authorization, positions, limit=500):
            calls.append(dict(positions)); return 'alice', [], {'workspace':0, 'public':0}, False
        with patch.object(notifications,'configuration',return_value=('local','','')), patch.object(events,'replay',replay), \
                patch.object(events,'stream_deadline',return_value=time.monotonic()+.3):
            response = await events.workspace_response(self.repo, SimpleNamespace(headers={'authorization':self.authorization()}))
            output = [chunk async for chunk in response.body_iterator]
        self.assertEqual(len(calls), 1)
        self.assertIn('event: ready', output[0])
        self.assertEqual(output[1:], [': heartbeat\n\n'])


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    setUp = WorkspaceEventsTests.setUp
    authorization = WorkspaceEventsTests.authorization

    async def asyncSetUp(self):
        closing = patch.object(notifications, '_closing', False); closing.start(); self.addCleanup(closing.stop)
        local = patch.object(notifications, 'configuration', return_value=('local','','')); local.start(); self.addCleanup(local.stop)

    async def test_close_streams_ends_open_streams_without_another_read(self):
        from types import SimpleNamespace
        reads, replays, replay = [], [], events.replay
        def read(*args): reads.append(args[2]); return {'items':[], 'terminal':False}
        def counted(*args, **kwargs): replays.append(1); return replay(*args, **kwargs)
        with patch.object(events, 'stream_deadline', return_value=time.monotonic()+60), patch.object(events, 'replay', counted):
            workspace = (await events.workspace_response(self.repo, SimpleNamespace(headers={'authorization':self.authorization()}))).body_iterator
            job = events.job_event_stream(None, None, 'job', 'proof', 0, 100, read, {'items':[], 'terminal':False})
            while 'event: ready' not in await anext(workspace): pass
            pending = asyncio.ensure_future(anext(job))
            while not reads: await asyncio.sleep(.01)
            await asyncio.sleep(.05)   # both now wait for a wake
            started = time.monotonic()
            await notifications.close_streams()
            with self.assertRaises(StopAsyncIteration): await asyncio.wait_for(pending, 1)
            with self.assertRaises(StopAsyncIteration): await asyncio.wait_for(anext(workspace), 1)
            self.assertLess(time.monotonic()-started, 1)
        self.assertEqual((reads, replays), ([0], [1]))   # shutdown is not a change: no further RDS read
        self.assertTrue(notifications.closing())

    async def test_streams_opened_during_shutdown_are_refused_or_end_at_once(self):
        from types import SimpleNamespace
        await notifications.close_streams()
        with self.assertRaises(Problem) as refused:
            await events.workspace_response(self.repo, SimpleNamespace(headers={'authorization':self.authorization()}))
        self.assertEqual((refused.exception.status, refused.exception.code), (503, 'SERVICE_UNAVAILABLE'))
        class Hub:
            def subscribe(self, scopes): raise AssertionError('no subscription during shutdown')
        with patch.object(notifications, 'hub', return_value=Hub()), patch.object(events, 'stream_deadline', return_value=time.monotonic()+60):
            output = [chunk async for chunk in events.job_event_stream(None, None, 'job', 'proof', 0, 100, None,
                {'items':[{'id':'1', 'event_type':'status'}], 'terminal':False})]
        self.assertEqual([chunk.split('\n')[0] for chunk in output], ['id: 1'])   # what was already read is kept


class WorkspaceEndpointTests(WorkspaceEventsTests):
    def test_workspace_endpoint_rejects_missing_auth_and_other_principals_cursor(self):
        from fastapi.testclient import TestClient
        from reveal_backend import app as api
        with patch.object(api,'repo',self.repo), patch.dict(os.environ, {'REVEAL_SSE_WINDOW_SECONDS':'1'}):
            client=TestClient(api.app)
            self.assertEqual(client.get('/v1/me/workspace/events').status_code,401)
            cursor=events.encode_cursor('alice',{'workspace':0,'public':0})
            response=client.get('/v1/me/workspace/events',headers={'Authorization':self.authorization('bob'),'Last-Event-ID':cursor})
            self.assertEqual(response.status_code,400)
            response=client.get('/v1/me/workspace/events',headers={'Authorization':self.authorization('alice')})
            self.assertEqual(response.status_code,200)
            self.assertIn('event: ready',response.text)
            self.assertIn('"entity_id":"alice"',response.text)
            self.assertNotIn('"entity_id":"bob"',response.text)
            self.assertIn('no-store',response.headers['cache-control'])
            with patch.object(notifications, '_closing', True):
                response=client.get('/v1/me/workspace/events',headers={'Authorization':self.authorization('alice')})
                self.assertEqual((response.status_code, response.json()['retryable']), (503, True))
                with self.repo.transaction() as tx: job=jobs.enqueue(tx,'alice','analysis')
                route='/v1/jobs/'+job['id']+'/events'
                response=client.get(route,headers={'Authorization':self.authorization('alice'),'Accept':'text/event-stream'})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(client.get(route,headers={'Authorization':self.authorization('alice')}).status_code, 200)   # JSON reads still served
