"""Read-only inspector: auth isolation, composite pagination, bounded cells and SQL inputs."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import jwt
from fastapi.testclient import TestClient
from reveal_backend import app as api
from reveal_backend.admin_database import CHUNK, inspect_cell, inspect_row, inspect_table, tables
from reveal_backend.auth import Problem
from reveal_backend.repository import Repository, uid


class DatabaseInspectorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.repo = Repository(str(Path(tmp.name)/'app.sqlite')); self.repo.migrate()
        for p in (patch.object(api, 'repo', self.repo), patch.dict(os.environ, {
            'REVEAL_GATEWAY_SECRET':'s'*40, 'REVEAL_GATEWAY_SERVICE_TOKEN':'t'*40,
            'REVEAL_GATEWAY_ISSUER':'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE':'reveal-api', 'DISABLE_ADMIN_LOGIN':'true'})):
            p.start(); self.addCleanup(p.stop)
        self.client = TestClient(api.app)
        self.root = '/internal/v1/admin/tables/reveal_records'
        with self.repo.transaction() as tx:
            for kind in ('draft', 'job', 'queue'):
                for i in range(4): tx.put(kind, str(i), 'owner', {'name':'gene_% '+str(i), 'token':'lease-credential', 'nested':{'value':i}})

    def headers(self, purpose='admin_telemetry'):
        proof = jwt.encode({'sub':'admin', 'iss':'reveal-nextjs','aud':'reveal-api','purpose':purpose,
            'iat':int(time.time()),'exp':int(time.time())+120,'jti':uid()},'s'*40,algorithm='HS256')
        return {'Authorization':'Bearer '+'t'*40, 'X-Reveal-Admin-Assertion':proof}

    def test_every_endpoint_requires_both_admin_credentials(self):
        for suffix, params in [('',{}),('/row',{'key':json.dumps({'kind':'draft','id':'0'})}),('/cell',{'key':json.dumps({'kind':'draft','id':'0'}),'column':'payload'})]:
            for headers, status in [({},403),({'Authorization':'Bearer '+'t'*40},401),(self.headers('verified_identity'),401)]:
                self.assertEqual(self.client.get(self.root+suffix,params=params,headers=headers).status_code,status)
            response=self.client.get(self.root+suffix,params=params,headers=self.headers())
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.headers['cache-control'],'private, no-store')
            self.assertNotIn('lease-credential',response.text)

    def test_composite_pagination_no_duplicates_and_bound_to_filter(self):
        # Observable writes also create durable notification cursors/events.
        # Compare against the actual full composite-key inventory independently
        # of the inspector's cursor implementation.
        with self.repo.read_transaction() as tx:
            expected=tx.execute('SELECT kind,id FROM reveal_records ORDER BY kind,id').fetchall()
        keys=[]; cursor=None
        while True:
            page=inspect_table(self.repo,'reveal_records',limit=5,cursor=cursor)
            keys.extend((r['key']['kind'],r['key']['id']) for r in page['rows'])
            cursor=page['next_cursor']
            if not cursor: break
        self.assertEqual(keys,expected); self.assertEqual(len(set(keys)),len(expected)); self.assertEqual(keys,sorted(keys))
        first=inspect_table(self.repo,'reveal_records',limit=2,column='kind',operator='equals',q='job')
        self.assertEqual(len(first['rows']),2)
        for kwargs in ({'limit':3,'column':'kind','operator':'equals','q':'job'}, {'limit':2,'column':'kind','operator':'equals','q':'draft'}):
            with self.assertRaises(Problem): inspect_table(self.repo,'reveal_records',cursor=first['next_cursor'],**kwargs)
        with self.assertRaises(Problem): inspect_table(self.repo,'reveal_records',cursor=first['next_cursor']+'bad')

    def test_read_only_search_inputs_do_not_become_sql_or_wildcards(self):
        for table in ('sqlite_master','users','reveal_records; DROP TABLE reveal_records'):
            with self.assertRaises(Problem): inspect_table(self.repo,table)
        for column in ('payload','id) OR 1=1 --','missing'):
            with self.assertRaises(Problem): inspect_table(self.repo,'reveal_records',column=column,q='x')
        self.assertEqual(inspect_table(self.repo,'reveal_records',column='id',q="' OR 1=1 --")['rows'],[])
        with self.repo.transaction() as tx:
            tx.put('draft','literal_%!','owner',{})
            tx.put('draft','literal_other','owner',{})
        page=inspect_table(self.repo,'reveal_records',column='id',q='_%!')
        self.assertEqual([r['key']['id'] for r in page['rows']],['literal_%!'])
        before=inspect_table(self.repo,'reveal_records')['rows']
        for _ in range(2): inspect_row(self.repo,'reveal_records',{'kind':'queue','id':'0'})
        self.assertEqual(before,inspect_table(self.repo,'reveal_records')['rows'])
        self.assertEqual(self.client.post(self.root,headers=self.headers(),json={'sql':'DELETE FROM reveal_records'}).status_code,405)

    def test_large_json_unicode_chunks_and_changed_value(self):
        content='🧬é\n'*20000
        with self.repo.transaction() as tx: tx.put('queue','large','owner',{'text':content,'token':'hidden-lease'})
        key={'kind':'queue','id':'large'}
        page=inspect_table(self.repo,'reveal_records',column='id',operator='equals',q='large')
        self.assertEqual(page['rows'][0]['cells']['payload']['loaded'],400)
        row=inspect_row(self.repo,'reveal_records',key)
        self.assertEqual(row['cells']['payload']['loaded'],CHUNK)
        first=inspect_cell(self.repo,'reveal_records',key,'payload')
        chunks=[first['text']]; current=first
        while current['truncated']:
            current=inspect_cell(self.repo,'reveal_records',key,'payload',current['loaded'],current['digest'])
            chunks.append(current['text'])
        payload=json.loads(''.join(chunks)); self.assertEqual(payload,{'text':content})
        with self.repo.transaction() as tx: tx.put('queue','large','owner',{'text':'changed','token':'new-hidden'})
        with self.assertRaises(Problem) as caught: inspect_cell(self.repo,'reveal_records',key,'payload',first['loaded'],first['digest'])
        self.assertEqual(caught.exception.status,409)

    def test_binary_null_large_integer_and_empty_table(self):
        with self.repo.transaction() as tx:
            tx.execute('CREATE TABLE dismech_embedding_vectors(run_id TEXT,input_sha256 TEXT,input_text TEXT,vector BLOB,vector_sha256 TEXT,PRIMARY KEY(run_id,input_sha256))')
            tx.execute('CREATE TABLE reveal_transaction_lock(id INTEGER PRIMARY KEY,revision INTEGER)')
            tx.execute('INSERT INTO reveal_transaction_lock VALUES(1,9007199254740993)')
        self.assertEqual(inspect_table(self.repo,'dismech_embedding_vectors')['rows'],[])
        raw=bytes(range(256))*200
        with self.repo.transaction() as tx:
            tx.execute('INSERT INTO dismech_embedding_vectors VALUES(%s,%s,%s,%s,%s)',('run','sha',None,raw,'checksum'))
        page=inspect_table(self.repo,'dismech_embedding_vectors'); row=page['rows'][0]
        self.assertIsNone(row['cells']['input_text']['text'])
        self.assertEqual(row['cells']['vector']['length'],len(raw))
        first=inspect_cell(self.repo,'dismech_embedding_vectors',row['key'],'vector')
        second=inspect_cell(self.repo,'dismech_embedding_vectors',row['key'],'vector',first['loaded'],first['digest'])
        self.assertEqual(bytes.fromhex(first['text']+second['text']),raw)
        self.assertEqual(inspect_table(self.repo,'reveal_transaction_lock')['rows'][0]['cells']['revision']['text'],'9007199254740993')

    def test_missing_rows_tables_and_invalid_bounds_are_explicit(self):
        for params in ({'limit':0},{'limit':51},{'limit':'abc'},{'q':'a'*257},{'operator':'SQL'}):
            self.assertEqual(self.client.get(self.root,params=params,headers=self.headers()).status_code,422)
        self.assertEqual(self.client.get('/internal/v1/admin/tables/dismech_documents',headers=self.headers()).status_code,404)
        for key,status in [('not-json',422),(json.dumps({'kind':'draft'}),422),(json.dumps({'kind':'draft','id':'absent'}),404)]:
            self.assertEqual(self.client.get(self.root+'/row',params={'key':key},headers=self.headers()).status_code,status)
        self.assertEqual(len(tables()),38)
        self.assertTrue(all(s['primary_key'] for s in tables().values()))

    def test_lifecycle_fields_are_visible_beyond_payload_preview_without_full_download(self):
        created = '2026-09-26T21:15:45Z'
        with self.repo.transaction() as tx:
            tx.put('account', 'dated', 'owner', {'large_document':'x'*100000,
                   'summary':{'created_at':created}, 'updated_at':created, 'completed_at':None,
                   'remote_handle':{'created_at':1790457345, 'timings':{'running_at':1790457350}}, 'token':'private'})
        page = inspect_table(self.repo, 'reveal_records', column='id', operator='equals', q='dated')
        row = page['rows'][0]
        self.assertNotIn(created, row['cells']['payload']['text'])
        self.assertEqual(row['timestamps']['payload.summary.created_at'], created)
        self.assertEqual(row['timestamps']['payload.updated_at'], created)
        self.assertIn('updated_at', row['timestamps'])
        self.assertNotIn('payload.completed_at', row['timestamps'])
        self.assertNotIn('payload.created_at', row['timestamps'])
        self.assertEqual(row['timestamps']['payload.remote_handle.timings.running_at'], '1790457350')
        self.assertLess(len(json.dumps(row)), 2000)
        detail = inspect_row(self.repo, 'reveal_records', row['key'])
        self.assertEqual(detail['timestamps'], row['timestamps'])
        self.assertNotIn('private', json.dumps(detail))

    def test_native_timestamp_columns_and_absent_dates(self):
        with self.repo.transaction() as tx:
            tx.execute('CREATE TABLE eaggl_cfde_link_runs(run_id TEXT PRIMARY KEY,eaggl_import_id TEXT,gene_set_import_id TEXT,model TEXT,match_method TEXT,status TEXT,manifest TEXT,created_at TIMESTAMP)')
            tx.execute('INSERT INTO eaggl_cfde_link_runs(run_id,status,manifest,created_at) VALUES(%s,%s,%s,%s)', ('run','ready','{}','2026-09-26 21:15:45'))
            tx.execute('CREATE TABLE reveal_transaction_lock(id INTEGER PRIMARY KEY,revision INTEGER)')
            tx.execute('INSERT INTO reveal_transaction_lock VALUES(1,1)')
        row = inspect_table(self.repo, 'eaggl_cfde_link_runs')['rows'][0]
        self.assertEqual(row['timestamps'], {'created_at':'2026-09-26 21:15:45'})
        self.assertEqual(inspect_table(self.repo, 'reveal_transaction_lock')['rows'][0]['timestamps'], {})
