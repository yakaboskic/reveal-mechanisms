"""Exercise signed wire histories through the pinned SDK, not a replay mock."""
import base64
from copy import deepcopy
import hashlib
import json
import time
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt
from qstash import AsyncQStash, Receiver
from upstash_workflow.constants import WORKFLOW_PROTOCOL_VERSION
from upstash_workflow.fastapi import Serve

from reveal_backend.workflow_compat import normalize_history


KEY = 'workflow-compat-test-signing-key-32-bytes'
URL = 'https://workflow.invalid/replay'


def encode(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def history(count=13):
    return [{'body': encode({'index': 0})}] + [
        {'callType': 'step', 'body': encode({'stepId': i + 1, 'stepName': 'phase-' + str(i),
            'stepType': 'Run', 'concurrent': 1,
            'out': json.dumps({'index': i + 1, 'done': False, 'sleep': 0})})}
        for i in range(count)]


class WorkflowCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.effects = []
        self.normalizations = []
        self.qstash = AsyncQStash('fake-test-token')
        self.qstash.http.request = AsyncMock(return_value=[])
        self.app = FastAPI()

        @Serve(self.app).post('/replay', qstash_client=self.qstash, receiver=Receiver(KEY, KEY), url=URL)
        async def replay(context):
            self.normalizations.append(len(context._steps))
            normalize_history(context)
            index = context.request_payload['index']
            for _ in range(100):
                current_index = index
                result = await context.run('phase-' + str(index), lambda: self.effect(current_index))
                if result['done']: return
                index = result['index']
                if result.get('sleep'): await context.sleep('wait-' + str(index), result['sleep'])

    def effect(self, index):
        self.effects.append(index)
        return {'index': index + 1, 'done': False, 'sleep': 0}

    def send(self, rows, *, signed=True):
        body = json.dumps(rows)
        headers = {'Content-Type': 'application/json', 'Upstash-Workflow-Sdk-Version': WORKFLOW_PROTOCOL_VERSION,
                   'Upstash-Workflow-Runid': 'offline-test'}
        if signed:
            headers['Upstash-Signature'] = jwt.encode({'iss': 'Upstash', 'sub': URL,
                'exp': int(time.time()) + 60, 'nbf': int(time.time()) - 1,
                'body': base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest()).decode().rstrip('=')}, KEY, algorithm='HS256')
        with TestClient(self.app) as client:
            return client.post('/replay', content=body, headers=headers)

    def test_ordered_history_advances_once(self):
        response = self.send(history())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.effects, [13])
        self.qstash.http.request.assert_awaited_once()

    def test_duplicate_tail_acknowledges_without_effect_submission_or_cleanup(self):
        rows = history(); rows.append(deepcopy(rows[12]))
        response = self.send(rows)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.effects, [])
        self.qstash.http.request.assert_not_awaited()

    def test_duplicate_middle_replays_canonical_history_and_advances_once(self):
        rows = history(); rows.insert(13, deepcopy(rows[12]))
        response = self.send(rows)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.effects, [13])
        self.qstash.http.request.assert_awaited_once()
        batch = json.loads(self.qstash.http.request.call_args.kwargs['body'])
        submitted = json.loads(batch[0]['body'])
        self.assertEqual((submitted['stepId'], submitted['stepName']), (14, 'phase-13'))

    def test_conflicting_duplicate_fails_closed(self):
        rows = history(); duplicate = json.loads(base64.b64decode(rows[12]['body']))
        duplicate['out'] = json.dumps({'index': 99, 'done': False, 'sleep': 0})
        rows.append({'callType': 'step', 'body': encode(duplicate)})
        response = self.send(rows)
        self.assertEqual(response.status_code, 500)
        self.assertIn('Conflicting duplicate', response.text)
        self.assertEqual(self.effects, [])
        self.qstash.http.request.assert_not_awaited()

    def test_duplicate_with_durable_sleeps_preserves_step_numbers(self):
        rows = [{'body': encode({'index': 0})}]
        for i in range(3):
            for step in ({'stepId': 2*i+1, 'stepName': 'phase-' + str(i), 'stepType': 'Run',
                          'out': json.dumps({'index': i+1, 'done': False, 'sleep': 5})},
                         {'stepId': 2*i+2, 'stepName': 'wait-' + str(i+1), 'stepType': 'SleepFor', 'out': 'null'}):
                rows.append({'callType': 'step', 'body': encode(dict(step, concurrent=1))})
        rows.insert(5, deepcopy(rows[2]))
        response = self.send(rows)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.effects, [3])
        self.qstash.http.request.assert_awaited_once()
        batch = json.loads(self.qstash.http.request.call_args.kwargs['body'])
        submitted = json.loads(batch[0]['body'])
        self.assertEqual((submitted['stepId'], submitted['stepName']), (7, 'phase-3'))

    def test_unsigned_duplicate_never_reaches_normalization(self):
        rows = history(); rows.append(deepcopy(rows[12]))
        response = self.send(rows, signed=False)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.normalizations, [])
        self.assertEqual(self.effects, [])
        self.qstash.http.request.assert_not_awaited()

    def test_missing_or_reordered_unique_steps_fail_closed(self):
        for rows in (history()[:10] + history()[11:], history()[:10] + [history()[11], history()[10]] + history()[12:]):
            response = self.send(rows)
            self.assertEqual(response.status_code, 500)
            self.assertIn('incomplete or out of order', response.text)
        self.assertEqual(self.effects, [])
        self.qstash.http.request.assert_not_awaited()


if __name__ == '__main__': unittest.main()
