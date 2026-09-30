"""Notification durability, authorized replay and absence of idle data polls."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import jwt
from reveal_backend import jobs, workspace_events as events, redis_notifications as notifications
from reveal_backend.auth import Problem
from reveal_backend.repository import Repository, digest


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

    def test_retired_identity_cannot_replay_previously_authorized_cursor(self):
        with self.repo.transaction() as tx:
            row=tx.get('principal','alice'); row['data']['retired']=True
            tx.put('principal','alice','alice',row['data'])
        with self.assertRaises(Problem): self.replay()

    def test_delete_and_bulk_mutations_produce_committed_events(self):
        with self.repo.transaction() as tx: tx.insert_many([('draft','first','alice',{'id':'first'})])
        with self.repo.transaction() as tx: tx.update_existing('draft','first','alice',{'id':'first','changed':True})
        with self.repo.transaction() as tx: tx.remove('draft','first')
        drafts=[item for item in self.replay()[1] if item['entity_id']=='first']
        self.assertEqual([item['operation'] for item in drafts],['upsert','upsert','remove'])
        self.assertEqual([item['entity_revision'] for item in drafts],[1,2,3])


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
                await first.wait(1)
                bridge.wake(notifications.channel('workspace:alice'))
                bridge.wake(notifications.channel('workspace:alice'))
                self.assertEqual(await first.wait(1),'resync')
                self.assertEqual(await second.wait(1),'resync')
            self.assertEqual(len(bridge.tasks),0)
            await bridge.close()

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
