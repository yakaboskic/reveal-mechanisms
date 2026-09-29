"""Both publication routes require current sign-in, including replayed requests."""
import time
import unittest
import jwt

from reveal_backend.repository import uid
import test_publication
import test_analysis_outcomes


class PublicationSignInTests(unittest.TestCase):
    def fixture(self, kind):
        fixture = (test_publication.PublicationTests if kind == 'account' else test_analysis_outcomes.AnalysisOutcomeTests)()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        if kind == 'account':
            route = fixture.control
            tables = ('publication', 'publication_snapshot')
        else:
            identity, _ = fixture.save()
            route = '/v1/analysis-outcomes/' + identity + '/publication'
            tables = ('outcome_publication', 'outcome_snapshot')
        return fixture, route, tables

    def identity(self, fixture, kind):
        with fixture.repo.transaction() as tx:
            row = tx.get('principal', fixture.owner)
            row['data']['me']['principal_kind'] = kind
            tx.put('principal', fixture.owner, fixture.owner, row['data'])
        stamp = int(time.time())
        token = jwt.encode({'sub': fixture.owner, 'principal_kind': kind, 'iss': 'reveal-nextjs',
            'aud': 'reveal-api', 'iat': stamp, 'exp': stamp + 120, 'jti': uid()}, 's' * 40, algorithm='HS256')
        return {'Authorization': 'Bearer ' + token, 'Idempotency-Key': uid()}

    def test_anonymous_rejected_without_writes_then_registered_owner_can_publish(self):
        for kind in ('account', 'exploration'):
            with self.subTest(kind=kind):
                fixture, route, tables = self.fixture(kind)
                anonymous = self.identity(fixture, 'anonymous')
                body = {'visibility': 'public', 'expected_version': 0}
                self.assertEqual(fixture.client.post(route, json=body).status_code, 401)
                response = fixture.client.post(route, headers=anonymous, json=body)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()['code'], 'SIGN_IN_REQUIRED')
                self.assertEqual(fixture.client.get(route, headers=anonymous).json()['visibility'], 'private')
                with fixture.repo.read_transaction() as tx:
                    for table in tables: self.assertEqual(tx.list(table), [])
                    self.assertEqual(tx.list('idempotency'), [])
                # Merely claiming registered in a signed assertion cannot
                # upgrade the persisted anonymous principal.
                self.assertEqual(fixture.client.post(route, headers=fixture.headers(fixture.owner), json=body).status_code, 401)
                signed_in = self.identity(fixture, 'registered')
                response = fixture.client.post(route, headers=signed_in, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['visibility'], 'public')

    def test_current_sign_in_is_checked_before_replay_but_unpublish_remains_available(self):
        for kind in ('account', 'exploration'):
            with self.subTest(kind=kind):
                fixture, route, _ = self.fixture(kind)
                signed_in = self.identity(fixture, 'registered')
                body = {'visibility': 'public', 'expected_version': 0}
                response = fixture.client.post(route, headers=signed_in, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                anonymous = self.identity(fixture, 'anonymous')
                anonymous['Idempotency-Key'] = signed_in['Idempotency-Key']
                response = fixture.client.post(route, headers=anonymous, json=body)
                self.assertEqual(response.status_code, 403, response.text)
                anonymous['Idempotency-Key'] = uid()
                response = fixture.client.post(route, headers=anonymous, json={'visibility':'public','expected_version':1})
                self.assertEqual(response.status_code, 403, response.text)
                response = fixture.client.post(route, headers=anonymous, json={'visibility':'private','expected_version':1})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['visibility'], 'private')
