"""Faithful transcript selection and atomic, environment-scoped constraint imports."""
from contextlib import redirect_stdout
import csv
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import import_gnomad_constraints as importer


def record(symbol='TEST', gene='ENSG00000000001', transcript='ENST00000000001', **changes):
    return {'gene': symbol, 'gene_id': gene, 'transcript': transcript, 'canonical': 'true', 'mane_select': 'true',
            'lof.pLI': '0.999', 'lof.oe_ci.upper': '0.12', 'mis.z_score': '-0.15', 'lof.oe': '0.07',
            'constraint_flags': '[]', **changes}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.tsv'

    def write(self, rows, path=None):
        path = path or self.source
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter='\t')
            writer.writeheader(); writer.writerows(rows)
        return path

    def prepare(self, rows, **kwargs): return importer.prepare(self.write(rows), **kwargs)


class SelectionTests(Fixture):
    def test_mane_precedes_canonical_without_metric_or_refseq_fallback(self):
        source = self.prepare([
            record(transcript='ENST00000000002', mane_select='false', **{'lof.pLI': '1'}),
            record(transcript='ENST00000000003', canonical='false', **{'lof.pLI': 'NA', 'lof.oe_ci.upper': 'NA', 'constraint_flags': '["no_exp_lof"]'}),
            record(gene='123', transcript='NM_123456.1', **{'lof.pLI': '1'}),
        ])
        row = source.genes[0]
        self.assertEqual((row['gene_id'], row['transcript_id'], row['selection_reason']), ('ENSG00000000001', 'ENST00000000003', 'mane_select'))
        self.assertIsNone(row['pli']); self.assertIsNone(row['loeuf'])
        self.assertEqual(row['constraint_flags'], ['no_exp_lof'])
        self.assertEqual(source.manifest['transcript_count'], 3)

    def test_canonical_fallback_is_preserved_with_zero_and_negative_z(self):
        source = self.prepare([record(mane_select='false', **{'lof.pLI': '0', 'lof.oe': '0'})])
        row = source.genes[0]
        self.assertEqual(row['selection_reason'], 'canonical')
        self.assertEqual(row['pli'], 0); self.assertEqual(row['lof_oe'], 0); self.assertEqual(row['mis_z'], -.15)

    def test_no_arbitrary_winner_for_gene_or_transcript_ambiguity(self):
        examples = [
            ([record(), record(gene='ENSG00000000002', transcript='ENST00000000002')], 'ambiguous_gene', 'multiple_ensembl_gene_ids'),
            ([record(), record(transcript='ENST00000000002')], 'ambiguous_transcript', 'multiple_mane_select'),
            ([record(mane_select='false'), record(transcript='ENST00000000002', mane_select='false')], 'ambiguous_transcript', 'multiple_canonical'),
            ([record(mane_select='false', canonical='false')], 'no_primary_transcript', 'no_mane_or_canonical'),
            ([record(gene='123', transcript='NM_123456.1')], 'no_ensembl_gene', 'refseq_only'),
        ]
        for rows, status, reason in examples:
            with self.subTest(status=status, reason=reason):
                selected = self.prepare(rows).genes[0]
                self.assertEqual((selected['selection_status'], selected['selection_reason']), (status, reason))
                self.assertTrue(all(selected[key] is None for key in importer.METRICS))
                self.assertIsNone(selected['transcript_id']); self.assertIsNone(selected['source_row'])

    def test_na_symbol_and_numeric_gene_identity_are_not_coerced_or_joined(self):
        source = self.prepare([record(symbol='NA'), record(symbol='test', gene='123', transcript='NM_123456.1'),
                               record(symbol='TEST', gene='ENSG00000000002', transcript='ENST00000000002')])
        rows = list(importer.rows(self.source))
        self.assertIsNone(rows[0]['gene_symbol'])
        self.assertEqual(rows[1]['gene_id'], '123')
        self.assertEqual([row['gene_symbol'] for row in source.genes], ['TEST', 'test'])
        self.assertEqual([row['selection_status'] for row in source.genes], ['selected', 'no_ensembl_gene'])

    def test_invalid_rows_fail_before_any_database_work(self):
        variants = [
            {'lof.pLI': 'nan'}, {'lof.pLI': 'inf'}, {'lof.pLI': '1.001'}, {'lof.pLI': '-.1'},
            {'lof.oe_ci.upper': '-1'}, {'mis.z_score': ''}, {'canonical': 'NA'},
            {'gene_id': '42'}, {'constraint_flags': '{}'}, {'constraint_flags': '[1]'},
        ]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.prepare([record(**changes)])
        with self.assertRaisesRegex(ValueError, 'Duplicate'): self.prepare([record(), record()])
        with self.assertRaisesRegex(ValueError, 'multiple symbols'):
            self.prepare([record(), record(symbol='OTHER', transcript='ENST00000000002')])

    def test_source_identity_and_content_fingerprints_are_reproducible(self):
        first = self.prepare([record()])
        other = self.root / 'relocated.tsv'; other.write_bytes(self.source.read_bytes())
        self.assertEqual(first.manifest, importer.prepare(other).manifest)
        self.assertNotEqual(first.manifest['import_id'], importer.prepare(other, source_version='4.1.1').manifest['import_id'])
        changed = self.prepare([record(**{'lof.pLI': '0.5'})])
        self.assertNotEqual(first.manifest['source_sha256'], changed.manifest['source_sha256'])
        self.assertNotEqual(first.manifest['gene_sha256'], changed.manifest['gene_sha256'])

    def test_raw_metrics_preserve_numeric_lexemes_and_na_without_json_float_rounding(self):
        self.write([record(**{'lof.pLI': '1.9078e-19', 'lof.oe': '-0.0000e+00', 'mis.z_score': 'NA'})])
        row = next(importer.rows(self.source))
        self.assertEqual(row['raw_metrics']['lof.pLI'], '1.9078e-19')
        self.assertEqual(row['raw_metrics']['canonical'], 'true')
        self.assertEqual(row['raw_metrics']['constraint_flags'], '[]')
        self.assertEqual(row['raw_metrics']['lof.oe'], '-0.0000e+00')
        self.assertIsNone(row['raw_metrics']['mis.z_score'])
        self.assertEqual(importer.canonical(row['lof_oe']), '0.0')
        self.assertEqual(row['pli'], 1.9078e-19)
        self.assertIsNone(row['mis_z'])
        # MySQL's numeric JSON path had returned 1.9078000000000002e-19.
        # Strings never pass through that lossy path; typed DOUBLE remains numeric.
        stored = json.loads(importer.canonical(row['raw_metrics']))
        self.assertEqual(stored, row['raw_metrics'])

    def test_cli_dry_runs_never_connect_and_environment_mismatch_fails_closed(self):
        self.write([record()])
        with patch.object(importer, 'mysql_connection') as connect, redirect_stdout(io.StringIO()):
            result = importer.main(['load', '--input', str(self.source), '--table-prefix', 'reveal_workflow_local'])
            self.assertFalse(result['apply']); connect.assert_not_called()
            importer.main(['migrate']); connect.assert_not_called()
        env = self.root / 'backend.env'; env.write_text('REVEAL_APPLICATION_TABLE_PREFIX=reveal_workflow_local\n')
        with patch.dict(importer.os.environ, {}, clear=True), patch.object(importer, 'mysql_connection') as connect:
            with self.assertRaisesRegex(ValueError, 'prefix does not match'):
                importer.main(['load', '--input', str(self.source), '--env-file', str(env), '--table-prefix', 'reveal_workflow_qa', '--apply'])
            connect.assert_not_called()


class SQLiteConnection:
    """Real local constraints/transactions; only MySQL DDL/control spelling is adapted."""
    def __init__(self):
        self.db = sqlite3.connect(':memory:'); self.db.execute('PRAGMA foreign_keys=ON')
        self.lock_available, self.released, self.fail_table = True, [], None
    def cursor(self): return SQLiteCursor(self)
    def commit(self): self.db.commit()
    def rollback(self): self.db.rollback()
    def close(self): self.db.close()


class SQLiteCursor:
    def __init__(self, connection): self.connection, self.cursor, self.synthetic = connection, connection.db.cursor(), None
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, sql, args=()):
        self.synthetic = None
        if 'GET_LOCK(' in sql: self.synthetic = [(int(self.connection.lock_available),)]; return
        if 'RELEASE_LOCK(' in sql: self.connection.released.append(args[0]); self.synthetic = [(1,)]; return
        if sql.startswith('SET SESSION') or sql == 'SHOW WARNINGS': self.synthetic = []; return
        if 'CREATE TABLE' in sql:
            sql = re.sub(r'CHARACTER SET \w+|COLLATE \w+|\bUNSIGNED\b', '', sql)
            sql = re.sub(r'UNIQUE KEY \w+\s*\(', 'UNIQUE (', sql)
            sql = re.sub(r'^\s*INDEX [^\n]+\n', '', sql, flags=re.M)
            sql = sql.replace('ENGINE=InnoDB DEFAULT CHARSET=utf8mb4', '')
        sql = sql.replace('START TRANSACTION', 'BEGIN').replace(' FOR UPDATE', '')
        return self.cursor.execute(sql.replace('%s', '?'), args)
    def executemany(self, sql, args):
        self.synthetic = None
        self.cursor.executemany(sql.replace('%s', '?'), args)
        if self.connection.fail_table and self.connection.fail_table in sql: raise ConnectionError('Simulated failure after insert')
    def fetchone(self): return self.synthetic[0] if self.synthetic is not None else self.cursor.fetchone()
    def fetchall(self): return self.synthetic if self.synthetic is not None else self.cursor.fetchall()


class DatabaseTests(Fixture):
    def setUp(self):
        super().setUp(); self.connection = SQLiteConnection(); self.addCleanup(self.connection.close)
        importer.migrate(self.connection)
    def active(self): return dict(self.connection.db.execute('SELECT table_prefix,import_id FROM gnomad_constraint_active'))

    def test_load_preserves_all_rows_and_replay_is_idempotent(self):
        capture = self.prepare([record(), record(gene='123', transcript='NM_123456.1')])
        result = importer.load(self.connection, capture, table_prefix='reveal_workflow_local', batch_size=1)
        self.assertTrue(result['activated']); self.assertFalse(result['reused'])
        self.assertEqual(self.connection.db.execute('SELECT COUNT(*) FROM gnomad_constraint_transcripts').fetchone()[0], 2)
        self.assertEqual(self.connection.db.execute('SELECT status FROM gnomad_constraint_imports').fetchone()[0], 'ready')
        replay = importer.load(self.connection, capture, table_prefix='reveal_workflow_local', batch_size=1)
        self.assertTrue(replay['reused']); self.assertFalse(replay['activated'])
        self.assertEqual(self.active(), {'reveal_workflow_local': capture.manifest['import_id']})

    def test_failure_rolls_back_complete_import_and_leaves_other_environments_unchanged(self):
        first = self.prepare([record()]); importer.load(self.connection, first, table_prefix='reveal_workflow_qa')
        second = self.prepare([record(**{'lof.pLI': '0.1'})])
        self.connection.fail_table = 'gnomad_gene_constraints'
        with self.assertRaises(ConnectionError): importer.load(self.connection, second, table_prefix='reveal_workflow_local')
        self.assertEqual(self.active(), {'reveal_workflow_qa': first.manifest['import_id']})
        self.assertEqual(self.connection.db.execute('SELECT COUNT(*) FROM gnomad_constraint_imports').fetchone()[0], 1)
        self.assertEqual(self.connection.db.execute('SELECT COUNT(*) FROM gnomad_constraint_transcripts').fetchone()[0], 1)
        self.connection.fail_table = None
        importer.load(self.connection, second, table_prefix='reveal_workflow_local')
        self.assertEqual(self.active(), {'reveal_workflow_qa': first.manifest['import_id'], 'reveal_workflow_local': second.manifest['import_id']})

    def test_digest_readback_failure_prevents_activation_and_rolls_back_rows(self):
        capture = self.prepare([record()]); capture.manifest['gene_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'content digest'):
            importer.load(self.connection, capture, table_prefix='reveal_workflow_local')
        self.assertEqual(self.active(), {})
        self.assertEqual(self.connection.db.execute('SELECT COUNT(*) FROM gnomad_constraint_imports').fetchone()[0], 0)

    def test_existing_content_corruption_is_not_silently_repaired_or_activated(self):
        capture = self.prepare([record()]); importer.load(self.connection, capture)
        self.connection.db.execute('UPDATE gnomad_gene_constraints SET pli=0'); self.connection.db.commit()
        with self.assertRaisesRegex(ValueError, 'content digest'):
            importer.load(self.connection, capture, table_prefix='reveal_workflow_local')
        self.assertEqual(self.active(), {})

    def test_changed_file_and_lock_contention_cannot_start_an_import(self):
        capture = self.prepare([record()]); self.write([record(**{'lof.pLI': '0.2'})])
        with self.assertRaisesRegex(ValueError, 'changed after validation'): importer.load(self.connection, capture)
        capture = importer.prepare(self.source); self.connection.lock_available = False
        with self.assertRaisesRegex(RuntimeError, 'Another gnomAD'): importer.load(self.connection, capture)
        self.assertEqual(self.connection.db.execute('SELECT COUNT(*) FROM gnomad_constraint_imports').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
