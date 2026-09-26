import json
from pathlib import Path
import tempfile
import unittest

from reveal_backend.auth import Problem
from reveal_backend.citations import register, revise, get, export, render, csl
from reveal_backend.repository import Repository, digest

OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
CLAIM = 'dapper:Claim.' + 'a' * 32
CLAIM2 = 'dapper:Claim.' + 'b' * 32
PARAGRAPH = 'dapper:Paragraph.' + 'c' * 32


class CitationsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repository(str(Path(tmp.name) / 'test.db'))
        self.repo.migrate()
        attribution = {'principal_kind': 'registered', 'display_name': 'Ada Example', 'observed_at': '2026-09-25T12:00:00Z'}
        self.document = {'claims': [{'id': CLAIM, 'statement': 'Alpha claim <script>alert(1)</script> & 5%.'}, {'id': CLAIM2, 'statement': 'Beta claim.'}]}
        self.attribution = attribution
        with self.repo.transaction() as tx:
            self.records = register(tx, OWNER, self.document, attribution, '2026-09-25T12:00:00Z')
            self.paragraph = {'id': PARAGRAPH, 'text': 'A 🧬 result. Again.', 'citations': [
                {'target_id': CLAIM, 'citation_metadata_revision': 1, 'start': 2, 'end': 10, 'exact_text': '🧬 result'},
                {'target_id': CLAIM2, 'citation_metadata_revision': 1, 'start': 2, 'end': 10, 'exact_text': '🧬 result'},
                {'target_id': CLAIM, 'citation_metadata_revision': 1, 'start': 12, 'end': 17, 'exact_text': 'Again'}]}
            tx.put('paragraph', digest([OWNER, PARAGRAPH]), OWNER, {'result': {'document': {'paragraphs': [self.paragraph]}}})

    def test_first_registration_and_metadata_revisions_are_immutable(self):
        with self.repo.transaction() as tx:
            again = register(tx, OTHER, self.document, {'display_name': 'Different'}, '2026-10-01T00:00:00Z')
            self.assertEqual(again, self.records)
            revision = revise(tx, OWNER, CLAIM, 1, {'language': 'en'})
            self.assertEqual(revision['metadata_revision'], 2)
            self.assertEqual(get(tx, OWNER, CLAIM, 1), self.records[0])
            self.assertEqual(revision['first_minted_at'], self.records[0]['first_minted_at'])

    def test_missing_exact_revision_and_foreign_paragraph_are_denied(self):
        with self.repo.transaction() as tx:
            with self.assertRaises(Problem):
                get(tx, OTHER, CLAIM, 1)
            with self.assertRaises(Problem):
                export(tx, OTHER, PARAGRAPH)
            with self.assertRaises(Problem):
                get(tx, OWNER, CLAIM, 2)

    def test_exports_preserve_unicode_coincident_and_repeated_citations(self):
        with self.repo.transaction() as tx:
            exports = {f: export(tx, OWNER, PARAGRAPH, f) for f in ('markdown', 'latex', 'bibtex', 'rich-text')}
        targets = [{'target_id': CLAIM, 'citation_metadata_revision': 1}, {'target_id': CLAIM2, 'citation_metadata_revision': 1}]
        for item in exports.values():
            self.assertEqual(item['citation_targets'], targets)
        self.assertIn('A 🧬 result [1, 2]. Again [1].', exports['rich-text']['plain_text'])
        self.assertNotIn('<script>', exports['rich-text']['content'])
        self.assertNotIn('<script>', exports['markdown']['content'])
        self.assertIn('\\<script\\>', exports['markdown']['content'])
        self.assertIn('\\cite{' + CLAIM + '-r1,' + CLAIM2 + '-r1}', exports['latex']['content'])
        self.assertEqual(exports['latex']['required_companions'], ['references.bib'])
        self.assertEqual(exports['bibtex']['content'].count('@misc{'), 2)
        self.assertNotIn('doi =', exports['bibtex']['content'])

    def test_utf16_offsets_are_not_mistaken_for_unicode_codepoints(self):
        with self.repo.transaction() as tx:
            self.paragraph['citations'][0]['end'] += 1
            tx.put('paragraph', digest([OWNER, PARAGRAPH]), OWNER, {'result': {'document': {'paragraphs': [self.paragraph]}}})
            with self.assertRaises(Problem) as error:
                export(tx, OWNER, PARAGRAPH)
            self.assertEqual(error.exception.code, 'INVALID_CITATION_SPAN')

    def test_actual_csl_processor_disambiguates_whole_set(self):
        with self.repo.transaction() as tx:
            for style in ('apa', 'mla'):
                result = render(tx, OWNER, PARAGRAPH, style)
                labels = [x['label'] for x in result['in_text']]
                self.assertEqual(labels[0], labels[2])
                self.assertNotEqual(labels[0], labels[1])
                self.assertEqual(len(result['bibliography']), 2)
                self.assertEqual(result['rendering_manifest']['processor'], 'citeproc-js')
                self.assertEqual(len(result['rendering_manifest']['style_sha256']), 64)

    def test_unknown_dates_authors_are_not_fabricated(self):
        record = dict(self.records[0], byline=[], issued_date=None)
        item = csl(record)
        self.assertNotIn('author', item)
        self.assertNotIn('issued', item)
        self.assertNotIn('DOI', item)


if __name__ == '__main__':
    unittest.main()
