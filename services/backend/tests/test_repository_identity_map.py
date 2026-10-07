"""Rows read once per transaction are not re-read to write them, without changing what is written."""
from contextlib import ExitStack
import itertools
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reveal_backend import repository, jobs, redis_notifications, workflow_state
# Imported before deterministic() patches uid/now, so no module binds the fakes permanently.
from reveal_backend import research_hosted, research_ownership, research_work, user_inputs, workspace_events  # noqa: F401
from reveal_backend.repository import Conflict, Repository, Transaction, digest
from reveal_backend.workflow_execution import WorkflowExecution


class Recorder:
    def __init__(self): self.statements = []
    def __enter__(self):
        original = Transaction.execute; statements = self.statements
        def execute(tx, sql, params=()): statements.append(' '.join(sql.split()[:4])); return original(tx, sql, params)
        self.patch = patch.object(Transaction, 'execute', execute); self.patch.start(); return self
    def __exit__(self, *args): self.patch.stop()


def deterministic():
    """Same uid/now sequence in every run, patched wherever a module bound them."""
    ids, ticks = itertools.count(1), itertools.count(1)
    uid = lambda: 'u-%06d' % next(ids)
    now = lambda: '2026-10-07T00:00:%06dZ' % next(ticks)
    stack, real_uid, real_now = ExitStack(), repository.uid, repository.now
    for module in [m for name, m in list(sys.modules.items()) if name.startswith('reveal_backend.')]:
        if getattr(module, 'uid', None) is real_uid: stack.enter_context(patch.object(module, 'uid', uid))
        if getattr(module, 'now', None) is real_now: stack.enter_context(patch.object(module, 'now', now))
    stack.enter_context(patch.object(workflow_state, 'after', lambda seconds: '2099-01-01T00:00:00Z'))
    stack.enter_context(patch.object(redis_notifications, 'publish', lambda names: None))
    stack.enter_context(patch.dict(os.environ, {'REVEAL_JOB_TRANSPORT': 'workflow'}))
    return stack


def workload(repo):
    """Hot write shapes: tracked get-mutate-put, idempotent job creation, a workflow step, ownership transfer."""
    owner, other = 'owner-1', 'owner-2'
    with repo.transaction() as tx:
        tx.put('principal', owner, owner, {'user_id': owner, 'retired': False})
        tx.put('local_work', 'w1', owner, {'id': 'w1', 'state': 'preparing', 'expires_at': 'x'})
        tx.put('draft', 'd0', owner, {'id': 'd0', 'saved': True})
    with repo.transaction() as tx:
        row = tx.get('local_work', 'w1'); row['data']['state'] = 'ready'   # mutated in place, then written back
        tx.put('local_work', 'w1', owner, row['data'])
        tx.put('local_work', 'w1', owner, dict(row['data'], last_error='late'))
    created = []
    for key, kind in (('k1', 'paragraph'), ('k2', 'analysis'), ('k1', 'paragraph')):
        with repo.transaction() as tx:
            identity = digest([owner, 'jobs', key]); existing = tx.get('idempotency', identity)
            if existing: created.append(existing['data']['response']); continue
            job = jobs.enqueue(tx, owner, kind, account_id='a1' if kind == 'paragraph' else None, inputs={})
            tx.put('idempotency', identity, owner, {'checksum': digest(kind), 'response': job}); created.append(job)
    job = created[1]
    with repo.transaction() as tx:
        current = tx.get('job', job['id']); current['data'].update(status='running', stage='collecting')
        tx.put('job', job['id'], owner, current['data'])
        tx.put('job', job['id'], owner, current['data'])                    # unchanged: no event
    with repo.read_transaction() as tx: execution = tx.get('execution', job['id'])['data']
    payload = {'job_id': job['id'], 'namespace': execution['namespace'], 'generation': execution['generation']}
    _, step, _ = workflow_state.acquire(repo, payload, 0); token = step['fence']
    work = SimpleNamespace(repository=repo)
    WorkflowExecution.observe_commit(work, payload, token, {'box_id': 'b1', 'phase': 'running'}, [])
    events = [('agent_message', {'remote_stream_id': 's', 'remote_sequence': i, 'message': 'm%d' % i}) for i in range(3)]
    WorkflowExecution.observe_commit(work, payload, token, {'box_id': 'b1', 'phase': 'running'}, events)
    WorkflowExecution.observe_commit(work, payload, token, {'box_id': 'b1', 'phase': 'running'}, events)  # replay
    for _ in range(2): WorkflowExecution.activity(work, payload, token, 'stage', {'stage': 'collecting_output', 'message': 'Capturing'})
    workflow_state.complete(repo, payload, token, next_phase='author', sleep=5)
    with repo.transaction() as tx:
        tx.put('draft', 'd1', owner, {'id': 'd1', 'v': 1}); tx.put('draft', 'd1', owner, {'id': 'd1', 'v': 2})
        tx.execute('UPDATE reveal_records SET owner_id=%s WHERE owner_id=%s AND kind=%s', (other, owner, 'draft'))
        assert tx.get('draft', 'd1')['owner'] == other
        tx.put('draft', 'd1', other, {'id': 'd1', 'v': 3}); tx.remove('draft', 'd1'); assert tx.get('draft', 'd1') is None
        tx.update_existing('draft', 'd0', other, {'id': 'd0', 'saved': True, 'renamed': True})
    with repo.transaction() as tx:
        tx.put('principal', other, other, {'user_id': other, 'retired': False})
        tx.transfer(other, owner)


def run(path, *, identity_map):
    repo = Repository(str(path)); repo.migrate()
    with ExitStack() as stack:
        stack.enter_context(deterministic())
        if not identity_map:  # the previous behaviour: every get and pre-read goes to the database
            stack.enter_context(patch.object(repository, '_exact', lambda identity: False))
            stack.enter_context(patch.object(Transaction, 'insert', lambda tx, kind, identity, owner, data, replace=False:
                                             tx.put(kind, identity, owner, data)))
        recorder = stack.enter_context(Recorder())
        workload(repo)
    rows = sqlite3.connect(path).execute('SELECT kind,id,owner_id,version,payload,updated_at FROM reveal_records ORDER BY kind,id').fetchall()
    return rows, recorder.statements


class EquivalenceTests(unittest.TestCase):
    def test_rows_versions_payloads_and_workspace_events_are_byte_identical_with_fewer_statements(self):
        with tempfile.TemporaryDirectory() as directory:
            before, sent_before = run(Path(directory) / 'before.sqlite', identity_map=False)
            after, sent_after = run(Path(directory) / 'after.sqlite', identity_map=True)
        self.assertEqual(after, before)
        kinds = {row[0] for row in after}
        self.assertTrue({'workspace_event', 'workspace_cursor', 'event', 'remote_event', 'job', 'queue', 'execution',
                         'workflow_activity', 'idempotency', 'local_work', 'draft'} <= kinds, kinds)
        self.assertGreater(sum(row[0] == 'workspace_event' for row in after), 5)
        reads = lambda sent: sum(sql.startswith('SELECT') for sql in sent)
        # prepare_commit batches its cursor reads itself, so the map no longer saves its per-change pre-reads.
        self.assertLess(len(sent_after), len(sent_before) - 10, (len(sent_after), len(sent_before)))
        self.assertEqual([sql for sql in sent_after if not sql.startswith('SELECT')],
                         [sql for sql in sent_before if not sql.startswith('SELECT')])   # identical writes, in order
        self.assertLess(reads(sent_after), reads(sent_before))


class IdentityMapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name) / 'app.sqlite')); self.repo.migrate()
        stack = deterministic(); stack.__enter__(); self.addCleanup(stack.__exit__, None, None, None)
        with self.repo.transaction() as tx:
            tx.put('job', 'j1', 'alice', {'id': 'j1', 'status': 'queued', 'stage': 'queued'})
            tx.put('queue', 'j1', 'alice', {'attempt': 0})

    def test_duplicate_reads_and_pre_reads_of_rows_already_read_send_no_statement(self):
        with self.repo.read_transaction() as tx, Recorder() as seen:
            first, second = tx.get('job', 'j1'), tx.get('job', 'j1')
            self.assertEqual(first, second); self.assertIsNot(first['data'], second['data'])  # fresh objects
            self.assertIsNone(tx.get('job', 'missing')); self.assertIsNone(tx.get('job', 'missing'))
        self.assertEqual(len(seen.statements), 2)
        with self.repo.transaction() as tx, Recorder() as seen:
            job = tx.get('job', 'j1')['data']; queue = tx.get('queue', 'j1')['data']
            job['status'] = 'running'; tx.put('job', 'j1', 'alice', job)       # mutated in place: still diffed
            queue['attempt'] = 1; tx.update_existing('queue', 'j1', 'alice', queue)
            queue['attempt'] = 2; tx.put('queue', 'j1', 'alice', queue)        # own write: version is known
            self.assertEqual(len(tx.workspace_changes), 1)
        self.assertEqual([sql.split()[0] for sql in seen.statements], ['SELECT', 'SELECT', 'UPDATE', 'UPDATE', 'UPDATE'])
        with self.repo.read_transaction() as tx:
            self.assertEqual((tx.get('job', 'j1')['version'], tx.get('queue', 'j1')['version']), (2, 3))
            self.assertEqual([r['data']['event_type'] for r in tx.list('workspace_event')], ['job.updated'] * 2)  # setUp + status

    def test_own_writes_and_raw_writes_are_read_back_from_the_database(self):
        with self.repo.transaction() as tx:
            tx.put('job', 'j1', 'alice', {'id': 'j1', 'status': 'running', 'stage': 'queued'})
            with Recorder() as seen: self.assertEqual(tx.get('job', 'j1')['data']['status'], 'running')
            self.assertEqual(seen.statements, ['SELECT owner_id,version,payload FROM reveal_records'])
            tx.get('queue', 'j1')
            tx.execute('UPDATE reveal_records SET owner_id=%s WHERE kind=%s', ('bob', 'queue'))  # raw: forgets the map
            with Recorder() as seen: self.assertEqual(tx.get('queue', 'j1')['owner'], 'bob')
            self.assertEqual(len(seen.statements), 1)

    def test_a_stale_entry_fails_closed_instead_of_losing_an_update(self):
        with self.assertRaisesRegex(Conflict, 'outside the write fence'):
            with self.repo.transaction() as tx:
                tx.get('queue', 'j1'); owner, version, text = tx._rows[('queue', 'j1')]
                tx._rows[('queue', 'j1')] = (owner, version + 5, text)
                tx.put('queue', 'j1', 'alice', {'attempt': 9})
        with self.assertRaisesRegex(Conflict, 'Expected existing'):
            with self.repo.transaction() as tx:
                tx.get('queue', 'j1'); owner, version, text = tx._rows[('queue', 'j1')]
                tx._rows[('queue', 'j1')] = (owner, version + 5, text)
                tx.update_existing('queue', 'j1', 'alice', {'attempt': 9})
        with self.repo.read_transaction() as tx: self.assertEqual(tx.get('queue', 'j1')['data'], {'attempt': 0})

    def test_ids_that_padding_or_charset_conversion_could_match_are_never_remembered(self):
        with self.repo.transaction() as tx, Recorder() as seen:
            for identity in ('j1 ', 'jé1'):
                tx.get('job', identity); tx.get('job', identity); tx.get_many('job', [identity])
        self.assertEqual(len(seen.statements), 6)
        with self.repo.transaction() as tx:
            self.assertEqual(set(tx.get_many('job', ['j1', 'nope'])), {'j1'})
            with Recorder() as seen: tx.get('job', 'j1'); tx.get('job', 'nope'); tx.put('queue', 'nope', 'alice', {})
            self.assertEqual([sql for sql in seen.statements if sql.startswith('SELECT')], ['SELECT owner_id,version FROM reveal_records'])

    def test_payload_memory_is_bounded_and_large_rows_keep_only_their_version(self):
        with self.repo.transaction() as tx: tx.put('evidence', 'big', 'alice', {'blob': 'x' * 200})
        with patch.object(repository, '_TEXT_BUDGET', 100), self.repo.transaction() as tx:
            tx.get('evidence', 'big'); self.assertIsNone(tx._rows[('evidence', 'big')][2])
            with Recorder() as seen:
                tx.put('evidence', 'big', 'alice', {'blob': 'y'})                   # untracked: version suffices
                self.assertEqual(tx.get('evidence', 'big')['data'], {'blob': 'y'})
            self.assertEqual(sum(sql.startswith('SELECT') for sql in seen.statements), 1)

    def test_insert_skips_the_pre_read_and_replace_keeps_put_semantics_for_an_existing_key(self):
        with self.repo.transaction() as tx, Recorder() as seen:
            tx.insert('notification_outbox', 'n1', 'system', {'channels': []})
        self.assertEqual([sql.split()[0] for sql in seen.statements], ['INSERT'])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.repo.transaction() as tx: tx.insert('queue', 'j1', 'alice', {'attempt': 7})
        with self.repo.transaction() as tx: tx.insert('queue', 'j1', 'alice', {'attempt': 7}, replace=True)
        with self.repo.read_transaction() as tx:
            row = tx.get('queue', 'j1'); self.assertEqual((row['version'], row['data']), (2, {'attempt': 7}))


    def test_absent_is_true_only_for_keys_this_transaction_read_and_found_missing(self):
        with self.repo.transaction() as tx:
            tx.get_records([('job', 'j1'), ('job', 'nope'), ('job', 'nope ')])
            self.assertEqual([tx.absent('job', identity) for identity in ('j1', 'nope', 'nope ', 'unread')],
                             [False, True, False, False])   # a padded id is never remembered
            tx.insert_many([('job', 'nope', 'alice', {'id': 'nope'})]); self.assertFalse(tx.absent('job', 'nope'))
            tx.remove('job', 'j1'); self.assertTrue(tx.absent('job', 'j1'))
            tx.get('queue', 'j1'); tx.execute('UPDATE reveal_records SET owner_id=%s WHERE kind=%s', ('bob', 'queue'))
            self.assertFalse(tx.absent('job', 'j1'))   # raw SQL forgets everything


if __name__ == '__main__': unittest.main()
