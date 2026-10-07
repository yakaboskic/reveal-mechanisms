import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

from reveal_backend import citations
from reveal_backend.auth import Problem
from reveal_backend.citations import register, revise, get, export, render, csl
from reveal_backend.repository import Repository, Transaction, digest

OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
CLAIM = 'dapper:Claim.' + 'a' * 32
CLAIM2 = 'dapper:Claim.' + 'b' * 32
PARAGRAPH = 'dapper:Paragraph.' + 'c' * 32


def fresh_render(records, occurrences, style):
    """render_records before the warm engine: a new QuickJS context and citeproc Engine per call, verbatim."""
    import quickjs
    _, files = citations.assets()
    items = {item['id']: item for item in map(csl, records)}
    keys = [x['target_id'] + '@' + str(x['citation_metadata_revision']) for x in occurrences]
    context = quickjs.Context()
    context.set_memory_limit(256 * 1024 * 1024)
    context.set_max_stack_size(8 * 1024 * 1024)
    context.set_time_limit(10)
    context.eval('var module={exports:{}}; var console={log:function(){},warn:function(){}};')
    context.eval(files['citeproc.js'])
    context.eval('var input=' + json.dumps({'items': items, 'keys': keys, 'style': files[style + '.csl'], 'locale': files['en-US.xml']}) + ';')
    return json.loads(context.eval('''(function(){
      var engine = new module.exports.Engine({retrieveLocale:function(){return input.locale;}, retrieveItem:function(id){return input.items[id];}},input.style,'en-US',true);
      engine.setOutputFormat('text'); engine.updateItems(Object.keys(input.items));
      var prior=[], labels=[];
      input.keys.forEach(function(key,i){
        var result=engine.processCitationCluster({citationID:'occ-'+i,citationItems:[{id:key}],properties:{noteIndex:0}},prior,[]);
        result[1].forEach(function(change){labels[change[0]]=change[1];}); prior.push(['occ-'+i,0]);
      });
      var bib=engine.makeBibliography();
      return JSON.stringify({labels:labels, ids:bib ? bib[0].entry_ids : [], entries:bib ? bib[1] : []});
    })()'''))


def view(result):
    return [x['label'] for x in result['in_text']], [(x['target_id'], x['text']) for x in result['bibliography']]


def fresh_view(records, occurrences, style):
    rendered = fresh_render(records, occurrences, style)
    lookup = {r['target_id'] + '@' + str(r['metadata_revision']): r for r in records}
    return rendered['labels'], [(lookup[i]['target_id'], text.strip()) for ids, text in zip(rendered['ids'], rendered['entries']) for i in ids]


def record(index, title, author, date):
    return {'target_id': 'dapper:Claim.%032x' % index, 'metadata_revision': 1, 'title': title, 'target_class': 'Claim',
            'repository': 'REVEAL Mechanisms', 'canonical_url': 'https://example.org/id/%d' % index,
            'byline': [{'display_name': author}] if author else [], 'issued_date': date, 'publisher': None, 'doi': None}


class WarmEngineTests(unittest.TestCase):
    def test_warm_engine_renders_exactly_like_a_fresh_engine(self):
        rng = random.Random(35)
        same = [record(1, 'Gene X regulates Y', 'Anonymous researcher', '2026-09-25'), record(2, 'Gene X regulates Y', 'Anonymous researcher', '2026-09-25'),
                record(3, 'Other', 'Anonymous researcher', '2026-09-25')]
        cases = [same, same[:1]]   # year suffixes, then a subset that must lose them
        for _ in range(6):
            cases.append(list({r['target_id']: r for r in (record(rng.randint(0, 30), rng.choice(['Gene X regulates Y', 'Pathway Z gap']),
                rng.choice(['Anonymous researcher', 'Ada Lovelace', None]), rng.choice(['2026-09-25', '2026-10-01', None])) for _ in range(rng.randint(1, 5)))}.values()))
        occurrences = [[{'target_id': r['target_id'], 'citation_metadata_revision': 1} for r in records] +
                       [{'target_id': rng.choice(records)['target_id'], 'citation_metadata_revision': 1} for _ in range(rng.randint(0, 3))] for records in cases]
        expected = {}   # a fresh APA engine costs ~2.4 s to build, so the reference is computed once per case
        sequence = [(index, 'mla') for index in range(len(cases))] + [(0, 'apa'), (1, 'apa'), (2, 'mla'), (0, 'apa'), (1, 'apa'), (3, 'apa'), (1, 'mla'), (4, 'apa')]
        for index, style in sequence:
            if (index, style) not in expected: expected[index, style] = fresh_view(cases[index], occurrences[index], style)
            with self.subTest(case=index, style=style):
                self.assertEqual(view(citations.render_records(cases[index], occurrences[index], style)), expected[index, style])

    def test_one_thread_builds_each_engine_once_and_rebuilds_after_failure(self):
        import quickjs
        records = [record(1, 'Title', 'Ada Lovelace', '2026-09-25')]
        occurrences = [{'target_id': records[0]['target_id'], 'citation_metadata_revision': 1}]
        citations.render_records(records, occurrences, 'apa'); expected = fresh_view(records, occurrences, 'mla')
        contexts, threads, original = [], set(), quickjs.Context
        def context(*args):
            contexts.append(1); threads.add(threading.current_thread().name); return original(*args)
        process = citations._process
        def tracked(*args): threads.add(threading.current_thread().name); return process(*args)
        with patch.object(quickjs, 'Context', context), patch.object(citations, '_process', tracked):
            for style in ('apa', 'apa', 'mla', 'apa'): citations.render_records(records, occurrences, style)
            self.assertEqual(contexts, [])   # warm: no new runtime, no new engine
            with self.assertRaises(Exception): citations.render_records(records, [{'target_id': 'dapper:Claim.missing', 'citation_metadata_revision': 1}], 'mla')
            self.assertEqual(view(citations.render_records(records, occurrences, 'mla')), expected)
            self.assertEqual(len(contexts), 1)   # a failed render never leaves its engine behind
            with patch.object(citations, 'ENGINE_RENDERS', 1):
                citations.render_records(records, occurrences, 'mla'); citations.render_records(records, occurrences, 'mla')
            self.assertEqual(len(contexts), 3)
        self.assertEqual(len(threads), 1); self.assertTrue(next(iter(threads)).startswith('citeproc'))
        self.assertNotEqual(threading.current_thread().name, next(iter(threads)))


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

    def test_paragraph_citations_are_read_in_one_statement_and_rendering_writes_nothing(self):
        executed = []; original = Transaction.execute
        def counted(tx, sql, params=()): executed.append(sql.split()[0]); return original(tx, sql, params)
        with self.repo.read_transaction() as tx, patch.object(Transaction, 'execute', counted):
            result = render(tx, OWNER, PARAGRAPH, 'apa')
        self.assertEqual(executed, ['SELECT', 'SELECT'])   # the paragraph, then every citation's rows at once
        self.assertEqual(len(result['bibliography']), 2)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('citation_rendering'), [])
            with self.assertRaises(Problem) as error: render(tx, OTHER, PARAGRAPH, 'apa')
        self.assertEqual(error.exception.status, 404)

    def test_batched_reads_keep_span_and_authorization_order(self):
        with self.repo.transaction() as tx:
            tx.remove('citation', CLAIM2 + ':1')
            with self.assertRaises(Problem) as missing: export(tx, OWNER, PARAGRAPH)
            self.assertEqual(missing.exception.code, 'CITATION_NOT_FOUND')
            self.paragraph['citations'][0]['end'] += 1
            tx.put('paragraph', digest([OWNER, PARAGRAPH]), OWNER, {'result': {'document': {'paragraphs': [self.paragraph]}}})
            with self.assertRaises(Problem) as span: export(tx, OWNER, PARAGRAPH)
            self.assertEqual(span.exception.code, 'INVALID_CITATION_SPAN')   # the earlier bad span still wins

    def test_unknown_dates_authors_are_not_fabricated(self):
        record = dict(self.records[0], byline=[], issued_date=None)
        item = csl(record)
        self.assertNotIn('author', item)
        self.assertNotIn('issued', item)
        self.assertNotIn('DOI', item)


if __name__ == '__main__':
    unittest.main()
