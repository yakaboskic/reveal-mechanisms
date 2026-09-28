"""CLI notifications stay private; warning replay never floods public activity."""
import json
from pathlib import Path
import tempfile
import unittest

from reveal_backend import jobs
from reveal_backend.box_stream import ClaudeStream, MAX_UNKNOWN_EVENT_TYPES
from reveal_backend.public_tool_activity import display_arguments
from reveal_backend.repository import Repository, uid
from reveal_backend.worker import persist_activity_batch, public_activity


def line(value):
    return json.dumps(value).encode() + b'\n'


class StreamNotificationTests(unittest.TestCase):
    def test_known_notifications_do_not_duplicate_calls_or_publish_private_content(self):
        parser = ClaudeStream()
        events = parser.feed(line({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'read-1', 'name': 'Read', 'input': {'file_path': '/input/index.json'}}]}}))
        for kind in ('tool_progress', 'tool_use_summary', 'auth_status', 'rate_limit_event'):
            for _ in range(20):
                events += parser.feed(line({'type': kind, 'tool_use_id': 'read-1', 'tool_name': 'Read',
                    'heartbeat': True, 'elapsed_time_seconds': 2, 'summary': 'PRIVATE',
                    'output': ['PRIVATE'], 'thinking': 'PRIVATE', 'authorization': 'Bearer PRIVATE'}))
        events += parser.feed(line({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'read-1', 'content': 'Public source row.'}]}}))
        events += parser.feed(line({'type': 'result', 'is_error': False}))
        events += parser.finish()
        self.assertEqual([kind for kind, _ in events], ['tool_call', 'tool_result', 'agent_completed'])
        self.assertEqual(events[0][1]['call_id'], events[1][1]['call_id'])
        self.assertNotIn('PRIVATE', json.dumps(events))
        self.assertEqual(parser.tools['read-1']['result']['content'], 'Public source row.')

    def test_true_unknown_types_warn_once_per_type_with_a_hard_memory_and_event_cap(self):
        parser = ClaudeStream()
        event = line({'type': 'future_event_PRIVATE', 'message': 'PRIVATE', 'thinking': 'PRIVATE'})
        warnings = []
        for _ in range(100):
            warnings += parser.feed(event)
        self.assertEqual(len(warnings), 1)
        for index in range(100):
            warnings += parser.feed(line({'type': 'PRIVATE_type_' + str(index), 'payload': 'PRIVATE'}))
        self.assertEqual(len(warnings), MAX_UNKNOWN_EVENT_TYPES + 1)
        self.assertEqual(len(parser.unknown_event_types), MAX_UNKNOWN_EVENT_TYPES)
        self.assertEqual(warnings[-1][1]['code'], 'unknown_agent_event_limit')
        self.assertNotIn('PRIVATE', json.dumps(warnings))
        self.assertTrue(all(isinstance(value, bytes) and len(value) == 32 for value in parser.unknown_event_types))

    def test_worker_suppresses_old_warning_repeats_without_suppressing_distinct_warnings(self):
        job = {'warnings': ['An unsupported agent event was retained privately.']}
        self.assertIsNone(public_activity(job, 'warning', {'message': job['warnings'][0]}))
        self.assertIsNotNone(public_activity(job, 'warning', {'message': 'The selected graph is unavailable.'}))
        self.assertEqual(len(job['warnings']), 2)

    def test_describe_graph_uses_only_its_graph_selector(self):
        arguments = display_arguments('mcp__reveal__describe_kg', {'graph': 'prokn', 'secret': 'PRIVATE'})
        self.assertEqual(json.loads(arguments), {'graph': 'prokn'})
        self.assertNotIn('PRIVATE', arguments)
        self.assertIn('unavailable', display_arguments('unknown_future_tool', {'graph': 'prokn'}))

    def test_durable_warning_dedup_acknowledges_every_remote_sequence_across_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Repository(str(Path(temporary) / 'application.sqlite')); repo.migrate()
            owner = uid()
            with repo.transaction() as tx:
                job = jobs.enqueue(tx, owner, 'analysis', request_id=uid())
            job, queue = jobs.claim(repo, 'worker')
            def warning(sequence, message='An unsupported agent event was retained privately.'):
                return ('warning', {'remote_stream_id': 'box:attempt:1', 'remote_sequence': sequence, 'message': message})
            persist_activity_batch(repo, job['id'], queue['token'], [warning(1), warning(2)])
            # Reopen the repository to exercise persisted warning state, rather
            # than a process-local dedupe set or the same event batch alone.
            repo = Repository(str(Path(temporary) / 'application.sqlite'))
            persist_activity_batch(repo, job['id'], queue['token'], [warning(2), warning(3)])
            persist_activity_batch(repo, job['id'], queue['token'], [warning(4, 'A different warning.')])
            with repo.read_transaction() as tx:
                current = tx.get('job', job['id'])['data']
                events = [row['data'] for row in tx.list('event', owner) if row['data']['event_type'] == 'warning']
                deliveries = tx.list('remote_event', owner)
            self.assertEqual(current['warnings'], ['An unsupported agent event was retained privately.', 'A different warning.'])
            self.assertEqual(len(events), 2)
            self.assertEqual(len(deliveries), 4)
            self.assertEqual(current['last_event_id'], '4')


if __name__ == '__main__':
    unittest.main()
