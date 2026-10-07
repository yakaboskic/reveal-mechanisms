"""Batched scientific acceptance (scientific_writes) leaves exactly the rows of the per-row put()s it replaced.

The pre-batching acceptance transactions are kept verbatim below and run against a copy of the same database with
deterministic ids and clocks: every row (kind, id, owner, version, payload, updated_at), including job events,
workspace events, cursors and the notification outbox, must be identical.
"""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reveal_backend import jobs, workspace_events, research_execution, worker as W
# Imported before any patch window: a module first imported inside one would keep the patched uid/now.
from reveal_backend import (acceptance, analysis_outcomes, citations, reference_archive, reference_generation,  # noqa: F401
    research_hosted, scientific_reuse, scientific_writes, workflow_execution)
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.repository import Repository, Transaction, digest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / 'data/fixtures/bubble-account-v1/scientific-account.json'
OWNER, OTHER = 'owner-1', 'owner-2'

# Worker.accept_accounts and research_execution.commit_accounts as of 2026-10-07, before batching.
SEQUENTIAL_ACCEPT = '''
async def accept_accounts(self,job,token,accepted,frozen,package_path,result,directory,mode):
    from .citations import register
    source_artifacts,artifact_access=await asyncio.to_thread(prepare_source_artifacts,package_path,job['id'])
    captured={}
    if mode=='box':
        from .acceptance import ledger_sources
        captured=await asyncio.to_thread(ledger_sources,result.ledger_manifest_path)
        for checksum,source in captured.items():
            source['retained']=await asyncio.to_thread(retained_file,source['path'],checksum)
    await asyncio.to_thread(self.save_workspace,job,token,artifacts_root()/job['id'])
    from .analysis_outcomes import creation_stamp, stamp_gap, stamped
    from .workflow_execution import drain_on_cancel
    def persist():
        # One fenced transaction in one worker thread: the event loop keeps serving while it holds the fence.
        evidence_sha256=sha256(package_path.read_bytes())
        package = decode(package_path.read_bytes())
        validation = package.get('validation_context', {})
        with self.repository.transaction() as tx:
            pair=jobs.fenced(tx,job['id'],token)
            if not pair or pair[0]['status']=='cancel_requested': return
            current,_=pair; owner=current['owner_user_id']; accounts=[]; paragraphs=[]; manifest_accounts=[]
            reused = {'contexts': [], 'borrowed_ids': [], 'citation_metadata': []}
            existing = validation.get('existing_account_ids', [])
            if validation:
                from .scientific_reuse import record_dependencies
                reused = record_dependencies(tx, owner, frozen['id'], validation.get('reuse_receipt_ids', []),
                    [doc['scientific_accounts'][0]['id'] for doc, _, _ in accepted] + existing)
                require(set(existing) <= {a['account_id'] for a in reused['existing_accounts']}, 'Reused result authority changed')
            retained_ids = {n['id'] for c in reused['contexts'] for rows in c['dapper_context'].values() if isinstance(rows, list)
                for n in rows if isinstance(n, dict) and 'id' in n}
            borrowed_ids = set(reused['borrowed_ids'])
            persist_source_artifacts(tx,owner,source_artifacts)
            for doc,report,path in accepted:
                if mode=='box':
                    for file in doc.get('files',[]):
                        source=captured.get(file.get('sha256'))
                        if source:
                            tx.put('artifact',digest([owner,file['sha256']]),owner,{'sha256':file['sha256'],'file':file,**source['retained'],'job_id':job['id']})
                            url=setting('NEXTAUTH_URL','http://localhost:3000').rstrip('/')+'/api/backend/v1/artifacts/'+file['sha256']
                            artifact_access[file['id']]={'file':file,'download_url':url,'expires_at':None,'availability':'available','verification':'checksum_verified'}
                runtime=decode(Path(result.runtime_manifest_path).read_bytes()) if result.runtime_manifest_path else {'model_id':None,'harness_version':None}
                runtime['model_id']=runtime.get('model_id') or runtime.get('model')
                actor=doc['scientific_accounts'][0].get('was_attributed_to',[None])[0]
                metadata=register(tx,owner,doc,{**frozen['attribution'],'person_id':actor},now(),runtime=runtime,
                    retained_citation_metadata=reused['citation_metadata'], retained_object_ids=retained_ids)
                account=doc['scientific_accounts'][0]; identity=account['id']; gap=next(g for g in doc['knowledge_gaps'] if g['id']==account['question'])
                outbox_key=digest([owner,identity,'default-paragraph']); previous=tx.get('outbox',outbox_key)
                if previous:
                    paragraph=tx.get('job',previous['data']['job_id'])['data']
                    old_account=owned(tx,'account',identity,owner)['data']
                    state=old_account['result']['research_statement']
                else:
                    paragraph=jobs.enqueue(tx,owner,'paragraph',account_id=identity,inputs={'kind':'paragraph','account_id':identity})
                    state={'status':'queued','job_id':paragraph['id'],'paragraph_id':None}
                envelope=object_envelope(doc,identity,metadata,artifact_access); envelope['research_statement']=state
                summary={'account':account,'knowledge_gap':gap,'claim_count':len(account['component_claims']),'created_at':now(),'job_id':job['id'],'research_statement':state}
                if not previous:
                    # A job that finishes after its reference generation was superseded is born archived.
                    stamp=creation_stamp(tx,owner,job['research_request_id'],gap=stamp_gap(frozen['composer'].get('source_gap'),frozen.get('question_id')),scientific_document=doc,
                        analysis={'job_id':job['id'],'request_id':job['research_request_id'],'evidence_package_sha256':evidence_sha256,'account_id':identity})
                    tx.put('account',digest([owner,identity]),owner,stamped('account',{'result':envelope,'summary':deepcopy(summary)},stamp))
                    tx.put('account_membership',digest([owner,identity]),owner,stamped('account_membership',{'account_id':identity,'summary':summary},stamp))
                document_sha=sha256(path.read_bytes())
                tx.put('scientific_document',digest([owner,document_sha]),owner,{'sha256':document_sha,'document':doc,'job_id':job['id'],'observed_at':now(),
                    'citation_metadata':metadata,'artifact_access':artifact_access})
                for rows in doc.values():
                    if isinstance(rows,list):
                        for node in rows:
                            if isinstance(node,dict) and str(node.get('id','')).startswith('dapper:'):
                                if node['id'] not in borrowed_ids:
                                    tx.put('grant',digest([owner,node['id']]),owner,{'target_id':node['id']})
                                projection=object_envelope(doc,node['id'],metadata,artifact_access)
                                tx.put('object_observation',digest([owner,node['id'],sha256(canonical_json(node))]),owner,{'object_id':node['id'],'payload':node,'document_sha256':document_sha})
                                if not tx.get('object',digest([owner,node['id']])):
                                    tx.put('object',digest([owner,node['id']]),owner,projection)
                                    tx.put('object_document',digest([owner,node['id']]),owner,{'object_id':node['id'],'sha256':document_sha})
                if not previous: tx.put('outbox',outbox_key,owner,{'account_id':identity,'job_id':paragraph['id'],'dispatched':True})
                accounts.append(identity); paragraphs.append(paragraph['id'])
                manifest_accounts.append({'path':str(path.resolve().relative_to(directory.resolve())),'sha256':sha256(path.read_bytes()),'account_id':identity,'lint_report_sha256':sha256(canonical_json(report))})
            public={'kind':'analysis','request_id':job['research_request_id'],'account_ids':accounts,'enrichment':enrichment_status(result,frozen['composer']['selected_kgs'],mode),
                'paragraph_job_ids':paragraphs,'evidence_package_sha256':evidence_sha256}
            if validation: public.update(reused_account_ids=existing, seed_sha256=validation['seed_sha256'])
            current.update(status='succeeded',stage='complete',result=public,completed_at=now())
            jobs.event(tx,current,'result','Gap analysis complete.' if mode=='box' else 'Development simulation complete — not a scientific result.')
            manifest={'format':'reveal.agent-output/1','job_id':job['id'],'attempt':pair[1]['attempt'],'status':'succeeded','input_package_sha256':public['evidence_package_sha256'],
                'runtime_manifest_sha256':sha256(Path(result.runtime_manifest_path).read_bytes()) if result.runtime_manifest_path else digest({'mode':mode}),
                'ledger_manifest_sha256':sha256(Path(result.ledger_manifest_path).read_bytes()) if result.ledger_manifest_path else None,'accounts':manifest_accounts,'reason':None}
            # S3 mode already has the exact output checkpoint and RDS result.
            if not s3_enabled(): (directory/'worker-output.json').write_bytes(canonical_json(manifest))
    await drain_on_cancel(asyncio.to_thread(persist))
'''
SEQUENTIAL_COMMIT = '''
def commit_accounts(service, tx, operation, prepared):
    from .acceptance import object_envelope
    from .citations import register
    from .scientific_reuse import record_dependencies
    from .analysis_outcomes import creation_stamp, stamp_gap, stamped
    owner = operation['owner_user_id']; work_id = operation['local_work_id']; args = operation['arguments']
    frozen = owned(tx, 'request', operation['research_request_id'], owner)['data']
    reused = record_dependencies(tx, owner, frozen['id'], prepared['reused']['receipt_ids'],
        prepared['account_ids'] + prepared['reused_account_ids'])
    retained_ids = {n['id'] for c in reused['contexts'] for rows in c['dapper_context'].values() if isinstance(rows, list)
                    for n in rows if isinstance(n, dict) and 'id' in n}
    borrowed_ids = set(reused['borrowed_ids']); access = {}
    for record in prepared['source_records']:
        file = record['file']
        tx.put('artifact', digest([owner, record['sha256']]), owner, record)
        access[file['id']] = {'file': file, 'download_url': setting('NEXTAUTH_URL', 'http://localhost:3000').rstrip('/')+'/api/backend/v1/artifacts/'+record['sha256'],
            'expires_at': None, 'availability': 'available', 'verification': 'checksum_verified'}
    for accepted in prepared['validated']:
        doc = accepted['document']; account = doc['scientific_accounts'][0]; identity = account['id']
        actor = account.get('was_attributed_to', [None])[0]
        metadata = register(tx, owner, doc, {**frozen['attribution'], 'person_id': actor}, now(),
            runtime={'model_id': None, 'harness_version': None}, retained_citation_metadata=reused['citation_metadata'], retained_object_ids=retained_ids)
        state = {'status': 'not_requested', 'job_id': None, 'paragraph_id': None}
        gap = next(g for g in doc['knowledge_gaps'] if g['id'] == account['question'])
        previous = tx.get('account', digest([owner, identity]))
        if not previous:
            envelope = object_envelope(doc, identity, metadata, access); envelope['research_statement'] = state
            summary = {'account': account, 'knowledge_gap': gap, 'claim_count': len(account['component_claims']),
                'created_at': now(), 'job_id': work_id, 'local_work_id': work_id, 'execution_mode': 'local', 'research_statement': state}
            stamp = creation_stamp(tx, owner, frozen['id'], gap=stamp_gap(frozen['composer'].get('source_gap'), frozen.get('question_id')),
                scientific_document=doc, analysis={'job_id': work_id, 'request_id': frozen['id'],
                    'evidence_package_sha256': prepared['evidence_manifest_sha256'], 'account_id': identity})
            tx.put('account', digest([owner, identity]), owner, stamped('account', {'result': envelope, 'summary': deepcopy(summary)}, stamp))
            tx.put('account_membership', digest([owner, identity]), owner, stamped('account_membership', {'account_id': identity, 'summary': summary}, stamp))
        document_sha = accepted['storage']['sha256']
        tx.put('scientific_document', digest([owner, document_sha]), owner, {'sha256': document_sha, 'document': doc,
            'job_id': work_id, 'local_work_id': work_id, 'observed_at': now(), 'citation_metadata': metadata, 'artifact_access': access,
            'evidence_manifest_sha256': prepared['evidence_manifest_sha256']})
        for rows in doc.values():
            if not isinstance(rows, list): continue
            for node in rows:
                if not isinstance(node, dict) or not str(node.get('id', '')).startswith('dapper:'): continue
                if node['id'] not in borrowed_ids:
                    tx.put('grant', digest([owner, node['id']]), owner, {'target_id': node['id']})
                tx.put('object_observation', digest([owner, node['id'], sha256(canonical_json(node))]), owner,
                    {'object_id': node['id'], 'payload': node, 'document_sha256': document_sha})
                if not tx.get('object', digest([owner, node['id']])):
                    tx.put('object', digest([owner, node['id']]), owner, object_envelope(doc, node['id'], metadata, access))
                    tx.put('object_document', digest([owner, node['id']]), owner, {'object_id': node['id'], 'sha256': document_sha})
    return {k: prepared[k] for k in ('report', 'account_ids', 'reused_account_ids', 'existing_accounts', 'evidence_manifest_sha256')}
'''


def sequential(source, module, name):
    """The old function, compiled against the live module globals so the same patches apply to both versions."""
    namespace = {}
    exec(compile(textwrap.dedent(source), 'sequential_' + name, 'exec'), module.__dict__, namespace)
    return namespace[name]


class SequentialWorker(W.Worker):
    accept_accounts = sequential(SEQUENTIAL_ACCEPT, W, 'accept_accounts')


def rename(document, identity):
    """The same document under another account id (ids are content hashes; projection does not re-verify them)."""
    old = document['scientific_accounts'][0]['id']
    return json.loads(json.dumps(document).replace(old, identity))


class Deterministic:
    """uid() and now() of every reveal_backend module, reset per run so both versions see one sequence."""
    def __init__(self, label='run'): self.label = label
    def __enter__(self):
        from reveal_backend import repository
        self.stack = ExitStack(); counter = iter(range(10 ** 6)); uid, now = repository.uid, repository.now
        label = self.label
        for name, module in list(sys.modules.items()):
            if not name.startswith('reveal_backend'): continue
            if getattr(module, 'uid', None) is uid:
                self.stack.enter_context(patch.object(module, 'uid', lambda: '%s-%06d' % (label, next(counter))))
            if getattr(module, 'now', None) is now:
                self.stack.enter_context(patch.object(module, 'now', lambda: '2026-10-07T00:00:00Z'))
        return self
    def __exit__(self, *exc):
        from reveal_backend import repository
        self.stack.close()
        leaked = [name for name, module in list(sys.modules.items()) if name.startswith('reveal_backend')
                  and (getattr(getattr(module, 'uid', None), '__qualname__', '') + getattr(getattr(module, 'now', None), '__qualname__', '')).count('Deterministic')]
        for name in leaked: sys.modules[name].uid, sys.modules[name].now = repository.uid, repository.now
        assert not leaked, 'imported inside a patch window: ' + ', '.join(leaked)


def rows(repo):
    with sqlite3.connect(repo.sqlite_path) as connection:
        return {(kind, identity): (owner, version, payload, updated) for kind, identity, owner, version, payload, updated in
                connection.execute('SELECT kind,id,owner_id,version,payload,updated_at FROM reveal_records')}


class AcceptanceBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = Repository(str(self.root / 'base.sqlite')); self.repo.migrate()
        self.document = json.loads(FIXTURE.read_text())
        for context in (patch.dict(os.environ, {'REVEAL_JOB_TRANSPORT': 'database', 'REVEAL_ARTIFACTS_ROOT': str(self.root / 'artifacts')}),
                        patch.object(W.Worker, 'save_workspace', lambda *args: None),
                        patch.object(workspace_events, 'publish_committed', lambda *args: 0),   # keep the outbox rows to compare
                        patch.object(W, 'enrichment_status', lambda *args: [])):
            context.start(); self.addCleanup(context.stop)

    def claimed(self, repo, owner):
        with repo.transaction() as tx: job = jobs.enqueue(tx, owner, 'analysis', request_id='request-' + owner)
        return jobs.claim(repo, 'acceptance-test', job_id=job['id'])

    def accept(self, repo, cls, owner, documents, mode='deterministic', package=None, claimed=None, label='run'):
        """One accept_accounts call on repo for a claimed job, under deterministic ids and clocks."""
        job, queue = claimed or self.claimed(repo, owner)
        directory = self.root / (repo.sqlite_path.rsplit('/', 1)[-1] + '-' + job['id']); directory.mkdir()
        package_path = directory / 'package.json'
        package_path.write_bytes(canonical_json(package or {'dapper_context': {'files': []}, 'source_artifacts': {}}))
        accepted = []
        for index, document in enumerate(documents):
            path = directory / ('accepted-%d.json' % index); path.write_bytes(canonical_json(document))
            accepted.append((document, {'valid': True, 'index': index}, path))
        frozen = {'id': job['research_request_id'], 'question_id': self.document['knowledge_gaps'][0]['id'],
                  'attribution': {'person_id': None, 'display_name': 'Tester', 'principal_kind': 'registered', 'observed_at': '2026-10-07T00:00:00Z'},
                  'composer': {'selected_kgs': [], 'source_gap': None}}
        result = SimpleNamespace(runtime_manifest_path=None, ledger_manifest_path=directory / 'ledger.json' if mode == 'box' else None)
        if mode == 'box': result.ledger_manifest_path.write_bytes(b'{}')
        with Deterministic(label):
            asyncio.run(cls(repo).accept_accounts(job, queue['token'], accepted, frozen, package_path, result, directory, mode))
        with repo.read_transaction() as tx: self.assertEqual(tx.get('job', job['id'])['data']['status'], 'succeeded')

    def compare(self, owner, documents, *, before=(), **options):
        """Run the batched and the sequential acceptance on copies of one database; their rows must be identical."""
        for index, (prior_owner, prior) in enumerate(before): self.accept(self.repo, SequentialWorker, prior_owner, prior, label='prior%d' % index)
        claimed = self.claimed(self.repo, owner)   # one job and lease, copied into both databases
        batched = Repository(str(self.root / 'batched.sqlite')); sequential = Repository(str(self.root / 'sequential.sqlite'))
        for copy in (batched, sequential): shutil.copy(self.repo.sqlite_path, copy.sqlite_path)
        statements = []; execute = Transaction.execute
        def counted(tx, sql, params=()): statements.append(sql.split()[0]); return execute(tx, sql, params)
        with patch.object(Transaction, 'execute', counted): self.accept(batched, W.Worker, owner, documents, claimed=claimed, **options)
        batched_statements = len(statements); statements.clear()
        with patch.object(Transaction, 'execute', counted):
            self.accept(sequential, SequentialWorker, owner, documents, claimed=deepcopy(claimed), **options)
        after, reference = rows(batched), rows(sequential)
        self.assertEqual(set(after), set(reference))
        for key in reference: self.assertEqual(after[key], reference[key], key)
        kinds = {kind for kind, _ in after}
        self.assertTrue({'grant', 'object', 'object_document', 'object_observation', 'citation', 'workspace_event', 'event'} <= kinds)
        return batched_statements, len(statements)

    def test_fresh_owner(self):
        batched, sequential = self.compare(OWNER, [self.document])
        print('\nfresh 63-node acceptance statements: batched', batched, 'sequential', sequential)
        self.assertLess(batched, 40); self.assertGreater(sequential, 400)

    def test_reaccepting_the_same_document(self):
        self.compare(OWNER, [self.document], before=[(OWNER, [self.document])])

    def test_two_accounts_sharing_nodes_in_one_commit(self):
        self.compare(OWNER, [self.document, rename(self.document, 'dapper:ScientificAccount.' + 'Z' * 32)])

    def test_two_documents_of_one_account_in_one_commit(self):
        shorter = deepcopy(self.document); shorter['used_edges'] = shorter['used_edges'][:-1]
        self.compare(OWNER, [self.document, shorter])

    def test_another_owner_minted_the_citations_first(self):
        self.compare(OWNER, [rename(self.document, 'dapper:ScientificAccount.' + 'Y' * 32)], before=[(OTHER, [self.document])])

    def test_borrowed_and_retained_nodes(self):
        claims = self.document['claims']
        reused = {'contexts': [{'dapper_context': {'claims': [claims[0]]}}], 'borrowed_ids': [claims[1]['id'], self.document['knowledge_gaps'][0]['id']],
                  'citation_metadata': [], 'existing_accounts': []}
        package = {'dapper_context': {'files': []}, 'source_artifacts': {},
                   'validation_context': {'existing_account_ids': [], 'reuse_receipt_ids': ['receipt'], 'seed_sha256': 'seed'}}
        with patch('reveal_backend.scientific_reuse.record_dependencies', return_value=reused):
            self.compare(OWNER, [self.document], package=package)

    def test_box_captures_add_artifact_access_document_by_document(self):
        files = self.document['files']
        later = rename(self.document, 'dapper:ScientificAccount.' + 'X' * 32)
        self.document['files'] = files[:4]   # the first document cites half the captured files, the second all of them
        captured = {file['sha256']: {'path': '/captured/' + file['filename']} for file in files}
        with patch('reveal_backend.acceptance.ledger_sources', lambda path: deepcopy(captured)), \
                patch.object(W, 'retained_file', lambda path, checksum: {'path': path}):
            self.compare(OWNER, [self.document, later], mode='box')


class SubmissionBatchTests(unittest.TestCase):
    """research_execution.commit_accounts (MCP submit): batched against its sequential predecessor."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.document = json.loads(FIXTURE.read_text())
        self.repo = Repository(str(self.root / 'base.sqlite')); self.repo.migrate()
        with self.repo.transaction() as tx:
            tx.put('request', 'request', OWNER, {'id': 'request', 'question_id': self.document['knowledge_gaps'][0]['id'],
                'attribution': {'person_id': None, 'display_name': 'Tester', 'principal_kind': 'registered'}, 'composer': {'source_gap': None}})
        for context in (patch.object(workspace_events, 'publish_committed', lambda *args: 0),
                        patch('reveal_backend.scientific_reuse.record_dependencies',
                              return_value={'contexts': [], 'borrowed_ids': [], 'citation_metadata': [], 'existing_accounts': []})):
            context.start(); self.addCleanup(context.stop)

    def commit(self, repo, function, documents):
        operation = {'owner_user_id': OWNER, 'local_work_id': 'work', 'arguments': {}, 'research_request_id': 'request'}
        file = self.document['files'][0]
        prepared = {'reused': {'receipt_ids': []}, 'account_ids': [d['scientific_accounts'][0]['id'] for d in documents], 'reused_account_ids': [],
                    'source_records': [{'sha256': file['sha256'], 'file': file, 'storage': {'sha256': file['sha256']}}],
                    'validated': [{'document': d, 'storage': {'sha256': sha256(canonical_json(d))}} for d in documents],
                    'evidence_manifest_sha256': 'manifest', 'report': {}, 'existing_accounts': []}
        with Deterministic(), repo.transaction() as tx: function(None, tx, operation, prepared)

    def test_submission_rows_match_sequential_puts(self):
        documents = [self.document, rename(self.document, 'dapper:ScientificAccount.' + 'W' * 32)]
        batched = Repository(str(self.root / 'batched.sqlite')); sequential_repo = Repository(str(self.root / 'sequential.sqlite'))
        for copy in (batched, sequential_repo): shutil.copy(self.repo.sqlite_path, copy.sqlite_path)
        self.commit(batched, research_execution.commit_accounts, documents)
        self.commit(sequential_repo, sequential(SEQUENTIAL_COMMIT, research_execution, 'commit_accounts'), documents)
        after, reference = rows(batched), rows(sequential_repo)
        self.assertEqual(set(after), set(reference))
        for key in reference: self.assertEqual(after[key], reference[key], key)


class ApplyPutsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name) / 'db.sqlite')); self.repo.migrate()

    def test_an_insert_conflict_rolls_back_the_whole_acceptance(self):
        with self.repo.transaction() as tx: tx.put('object_document', 'taken', OWNER, {'object_id': 'x'})
        with self.assertRaises(Exception), self.repo.transaction() as tx:
            tx.get_records([('grant', 'g'), ('object_document', 'other')])
            tx.apply_puts([('grant', 'g', OWNER, {'target_id': 'x'}, 2)], [('object_document', 'taken', OWNER, {'object_id': 'y'})])
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('grant', 'g')); self.assertEqual(tx.get('object_document', 'taken')['data'], {'object_id': 'x'})

    def test_unread_keys_fall_back_to_put_and_unchanged_rows_only_bump_versions(self):
        with self.repo.transaction() as tx:
            tx.put('grant', 'same', OWNER, {'target_id': 'a'}); tx.put('grant', 'changed', OWNER, {'target_id': 'b'})
        with self.repo.transaction() as tx:
            tx.get_records([('grant', 'same'), ('grant', 'changed')])
            tx.apply_puts([('grant', 'same', OWNER, {'target_id': 'a'}, 2), ('grant', 'changed', OWNER, {'target_id': 'c'}, 1),
                           ('grant', 'unread', OWNER, {'target_id': 'd'}, 2)])
            changes = dict(tx.workspace_changes)
        self.assertEqual(sorted(identity for _, _, identity in changes), ['changed', 'unread'])   # an unchanged grant emits nothing
        with self.repo.read_transaction() as tx:
            self.assertEqual([tx.get('grant', key)['version'] for key in ('same', 'changed', 'unread')], [3, 2, 2])


if __name__ == '__main__': unittest.main()
