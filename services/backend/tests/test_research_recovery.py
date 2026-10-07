"""The research recover loop reads once, fences only rows that change and never waits on the write fence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from round_trips import count_round_trips
from reveal_backend.repository import FenceBusy, Repository
from reveal_backend.research_work import ResearchWorkService, deadline

PAST, FUTURE = '2000-01-01T00:00:00Z', '2999-01-01T00:00:00Z'


class ResearchRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name) / 'app.sqlite')); self.repo.migrate()
        self.resumed = []
        service = ResearchWorkService(self.repo)
        service.resume_operation = lambda identity, lease_token=None: self.resumed.append((identity, lease_token))
        self.service = service
        with self.repo.transaction() as tx:
            for owner, retired in (('alice', False), ('bob', True)):
                tx.put('principal', owner, owner, {'retired': retired, 'me': {'user_id': owner,
                    'principal_kind': 'registered', 'workspace_expires_at': None}})
            for identity, owner, state, expires, extra in (
                    ('open', 'alice', 'ready', FUTURE, {}), ('expired', 'alice', 'ready', PAST, {}),
                    ('retired', 'bob', 'ready', FUTURE, {}),
                    ('closed-busy', 'alice', 'closed', FUTURE, {'closed_at': PAST}),
                    ('closed-idle', 'alice', 'closed', FUTURE, {'closed_at': PAST}),
                    ('closed-released', 'alice', 'closed', PAST, {'closed_at': PAST}),
                    ('hosted', 'alice', 'ready', PAST, {'job_id': 'hosted'})):
                tx.put('local_work', identity, owner, {'id': identity, 'owner_user_id': owner, 'state': state,
                    'expires_at': expires, 'research_request_id': 'request-' + identity, **extra})
                tx.put('research_pin', 'request-' + identity, owner, {'state': 'released' if identity == 'closed-released'
                    else 'active', 'research_request_id': 'request-' + identity})
            for identity, owner, work, state, lease in (
                    ('received', 'alice', 'open', 'received', None), ('stale', 'alice', 'open', 'running', deadline(-60)),
                    ('live', 'alice', 'closed-busy', 'running', deadline(600)),
                    ('orphan', 'mallory', 'open', 'received', None), ('hosted-op', 'alice', 'hosted', 'received', None),
                    ('done', 'alice', 'closed-idle', 'succeeded', None)):
                tx.put('research_operation', identity, owner, {'id': identity, 'owner_user_id': owner, 'local_work_id': work,
                    'kind': 'query', 'state': state, 'lease_until': lease, 'lease_token': identity + '-lease' if lease else None})

    def rows(self, kind):
        with self.repo.read_transaction() as tx: return {row['id']: row for row in tx.list(kind)}

    def test_one_cycle_closes_releases_and_resumes_from_one_snapshot(self):
        events = len(self.rows('workspace_event'))
        self.service.reconcile()
        works, pins = self.rows('local_work'), self.rows('research_pin')
        self.assertEqual({identity for identity, row in works.items() if row['data']['state'] == 'closed'},
                         {'expired', 'retired', 'closed-busy', 'closed-idle', 'closed-released'})
        self.assertEqual(works['hosted']['data']['state'], 'ready')   # the hosted lifecycle owns its work
        self.assertEqual({identity[len('request-'):] for identity, row in pins.items() if row['data']['state'] == 'released'},
                         {'expired', 'retired', 'closed-idle', 'closed-released'})   # a running lease keeps its pin
        self.assertEqual(sorted(self.resumed), [('hosted-op', None), ('received', None), ('stale', 'stale-lease')])
        self.assertEqual(len(self.rows('workspace_event')), events + 2)   # one per newly closed local work
        works, pins, events = self.rows('local_work'), self.rows('research_pin'), len(self.rows('workspace_event'))
        self.resumed.clear()
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('steady state took the write fence')), \
                count_round_trips() as budget:
            self.service.reconcile()
        self.assertEqual((budget.kinds(), budget.unleased, budget.connects), (['read'], 0, 0), budget)
        self.assertLessEqual(budget.trips(), 5, budget)   # START, local_work, principals+pins, open operations, COMMIT
        self.assertEqual({k: v['version'] for k, v in self.rows('local_work').items()}, {k: v['version'] for k, v in works.items()})
        self.assertEqual({k: v['version'] for k, v in self.rows('research_pin').items()}, {k: v['version'] for k, v in pins.items()})
        self.assertEqual(len(self.rows('workspace_event')), events)
        self.assertEqual(sorted(self.resumed), [('hosted-op', None), ('received', None), ('stale', 'stale-lease')])

    def test_pin_is_released_once_the_last_operation_settles(self):
        self.service.reconcile()
        with self.repo.transaction() as tx:
            operation = tx.get('research_operation', 'live')['data']; operation['state'] = 'succeeded'
            tx.put('research_operation', 'live', 'alice', operation)
        self.service.reconcile()
        self.assertEqual(self.rows('research_pin')['request-closed-busy']['data']['state'], 'released')
        released = self.rows('research_pin')['request-closed-idle']
        self.service.reconcile()
        self.assertEqual(self.rows('research_pin')['request-closed-idle'], released)   # released_at is never rewritten

    def test_candidates_are_decided_again_under_the_fence(self):
        with self.repo.read_transaction() as tx: snapshot = tx.list('local_work')
        with self.repo.transaction() as tx:   # renewed between the snapshot and the fence
            work = tx.get('local_work', 'expired')['data']; work['expires_at'] = FUTURE
            tx.put('local_work', 'expired', 'alice', work)
        list_snapshot = patch('reveal_backend.repository.Transaction.list',
                              new=lambda tx, kind, owner=None: snapshot if kind == 'local_work' else [])
        with list_snapshot: self.service.reconcile()
        self.assertEqual(self.rows('local_work')['expired']['data']['state'], 'ready')
        self.assertEqual(self.rows('local_work')['retired']['data']['state'], 'closed')

    def test_a_held_fence_defers_writes_but_not_recovery(self):
        with patch.object(self.repo, 'transaction', side_effect=FenceBusy('held')) as fenced:
            self.service.reconcile()
        self.assertEqual(fenced.call_args.kwargs, {'nowait': True})
        self.assertEqual(self.rows('local_work')['expired']['data']['state'], 'ready')
        self.assertEqual(sorted(self.resumed), [('hosted-op', None), ('received', None), ('stale', 'stale-lease')])
        self.service.reconcile()
        self.assertEqual(self.rows('local_work')['expired']['data']['state'], 'closed')

    def test_no_local_work_is_one_read(self):
        with self.repo.transaction() as tx:
            for row in tx.list('local_work'): tx.remove('local_work', row['id'])
        with count_round_trips() as budget: self.service.reconcile()
        self.assertEqual(budget.leases, [['read', 1]], budget)
        self.assertEqual(self.resumed, [])


if __name__ == '__main__': unittest.main()
