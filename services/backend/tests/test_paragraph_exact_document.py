"""Paragraph assembly re-mints the account it extends; it must start from the exact accepted document."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from reveal_backend import jobs
from reveal_backend.acceptance import LOCK, mint, object_projection, release_root
from reveal_backend.dapper_release import verified_release
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.worker import Worker, read_preparation_inputs

TEST_RELEASE = Path(os.environ.get('REVEAL_TEST_DAPPER_RELEASE', str(release_root())))


def atomic_claim_account():
    """An atomic RESULT claim as the authoring skill asks for: gene CURIE subject, RO relation."""
    return {
        'prefixes': {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:'},
        'mechanisms': [{'id': 'urn:test:mechanism', 'name': 'Factor 14 (cytokine signalling)'}],
        'propositions': [{'id': 'urn:test:proposition', 'proposition_kind': 'RESULT', 'subject_entity': 'HGNC.SYMBOL:JAK1',
                          'relation': 'obo:RO_0002610', 'object_entity': 'urn:test:mechanism',
                          'statement': 'JAK1 has gene loading 1.0 on factor 14.'}],
        'claims': [{'id': 'urn:test:claim', 'proposition': 'urn:test:proposition', 'statement': 'JAK1 loads on factor 14.',
                    'direction': 'SUPPORTS', 'status': 'proposed'}],
        'scientific_accounts': [{'id': 'urn:test:account', 'name': 'Atomic claim account', 'component_claims': ['urn:test:claim']}],
    }


@unittest.skipUnless(verified_release(TEST_RELEASE, LOCK), 'No DAPPER checkout verifies against the release lock; see docs/local-development.md')
class ParagraphExactDocumentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict('os.environ', {'REVEAL_DAPPER_ROOT': str(TEST_RELEASE), 'REVEAL_ARTIFACTS_DIR': str(self.root)})
        environment.start(); self.addCleanup(environment.stop)
        self.exact = mint(atomic_claim_account(), self.root / 'accepted.json')
        self.account_id = self.exact['scientific_accounts'][0]['id']
        self.projection = object_projection(self.exact, self.account_id)[0]['document']
        self.repository = Repository(str(self.root / 'app.sqlite')); self.repository.migrate()
        owner = uid()
        with self.repository.transaction() as tx:
            tx.put('account', digest([owner, self.account_id]), owner,
                   {'result': {'document': self.projection, 'citation_metadata': []}, 'summary': {}})
            tx.put('object_document', digest([owner, self.account_id]), owner, {'object_id': self.account_id, 'sha256': 'accepted'})
            tx.put('scientific_document', digest([owner, 'accepted']), owner, {'sha256': 'accepted', 'document': self.exact})
            self.job = jobs.enqueue(tx, owner, 'paragraph', account_id=self.account_id)

    def accept(self, account_document):
        worker = Worker(self.repository)
        worker.begin_persistence = AsyncMock(return_value=False); worker.save_workspace = Mock()
        inputs = {'format': 'reveal.paragraph-input/1', 'account_document': account_document,
                  'account_id': self.account_id, 'allowed_citations': []}
        directory = self.root / uid(); directory.mkdir()
        with patch('reveal_backend.box_paragraph.assemble_paragraph', return_value={'text': 'JAK1 loads on factor 14.', 'citations': []}), \
                patch('reveal_backend.worker.validate_paragraph_document', return_value={'valid': True, 'findings': []}):
            asyncio.run(worker.accept_paragraph(self.job, 'token', {}, inputs, directory))
        return worker

    def test_projection_expands_curies_so_reminting_it_rekeys_accepted_claims(self):
        subject = self.projection['propositions'][0]['subject_entity']
        self.assertEqual(subject, 'https://identifiers.org/hgnc.symbol:JAK1')
        with self.assertRaisesRegex(EvidenceBuildError, 'changed accepted scientific content'):
            self.accept(self.projection)

    def test_paragraph_from_the_prepared_input_keeps_every_accepted_identity(self):
        document = read_preparation_inputs(self.repository, self.job)['result']['document']
        self.assertEqual(document, self.exact)
        worker = self.accept(document)
        worker.begin_persistence.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
