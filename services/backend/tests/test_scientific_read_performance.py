"""Reduce network round trips without weakening snapshot/ownership boundaries."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from reveal_backend.auth import owned
from reveal_backend.repository import Transaction, digest
from test_publication import PublicationTests


class ScientificReadPerformanceTests(unittest.TestCase):
    setUp = PublicationTests.setUp
    principal = PublicationTests.principal
    headers = PublicationTests.headers
    seed = PublicationTests.seed
    request = PublicationTests.request
    publish = PublicationTests.publish

    def test_account_batches_control_and_index_reads_but_uses_full_stored_document(self):
        envelope = deepcopy(self.fixture['account'])
        checksum = digest(envelope['document'])
        with self.repo.transaction() as tx:
            tx.put('object_document', digest([self.owner, self.account_id]), self.owner,
                   {'object_id': self.account_id, 'sha256': checksum})
            tx.put('scientific_document', digest([self.owner, checksum]), self.owner,
                   {'document': envelope['document'], 'citation_metadata': envelope['citation_metadata']})
            row = tx.get('account', digest([self.owner, self.account_id]))['data']
            # A stored first response may contain only the account root. Reading
            # must still hydrate the canonical document, with original IDs.
            row['result']['document'] = {'scientific_accounts': envelope['document']['scientific_accounts']}
            row['result']['coverage']['complete'] = False
            tx.put('account', digest([self.owner, self.account_id]), self.owner, row)
        statements = []
        execute = Transaction.execute
        def record(tx, sql, params=()):
            statements.append((sql, params))
            return execute(tx, sql, params)
        with patch.object(Transaction, 'execute', record):
            response = self.request('get', self.route, self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['document'], envelope['document'])
        selects = [(sql, params) for sql, params in statements if sql.startswith('SELECT')]
        self.assertEqual(len(selects), 4, selects)  # principal, batched headers, full document, batched current dependency authority
        self.assertIn('(kind,id) IN', selects[1][0])
        self.assertEqual(set(selects[1][1][::2]), {'account', 'object_document', 'publication', 'scientific_dependencies'})
        self.assertTrue(response.json()['publication']['can_manage'])

    def test_owned_row_needs_no_grant_but_cross_owner_row_still_requires_one(self):
        key = digest([self.owner, self.account_id])
        original = Transaction.execute
        statements = []
        def record(tx, sql, params=()):
            statements.append((sql, params))
            return original(tx, sql, params)
        with self.repo.read_transaction() as tx, patch.object(Transaction, 'execute', record):
            self.assertEqual(owned(tx, 'account', self.account_id, self.owner)['owner'], self.owner)
        self.assertEqual(len(statements), 1)
        with self.repo.transaction() as tx:
            row = tx.get('account', key)['data']
            tx.put('account', key, self.other, row)
            tx.remove('grant', digest([self.owner, self.account_id]))
        self.assertEqual(self.request('get', self.route, self.owner).status_code, 404)
        with self.repo.transaction() as tx:
            tx.put('grant', digest([self.owner, self.account_id]), self.owner, {'target_id': self.account_id})
        with self.repo.read_transaction() as tx:
            self.assertEqual(owned(tx, 'account', self.account_id, self.owner)['owner'], self.other)

    def test_batched_publication_controls_change_only_with_current_owner_state(self):
        response = self.publish()
        self.assertEqual(response.status_code, 200, response.text)
        owner = self.request('get', self.route, self.owner).json()
        public = self.client.get(self.route).json()
        self.assertTrue(owner['publication']['can_manage'])
        self.assertFalse(public['publication']['can_manage'])
        self.assertEqual(owner['publication']['version'], public['publication']['version'])
        response = self.publish(visibility='private', version=1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(self.route).status_code, 404)
        self.assertEqual(self.request('get', self.route, self.owner).json()['publication']['visibility'], 'private')
