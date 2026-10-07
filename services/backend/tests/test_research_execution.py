"""Local ingestion, exact context validation and atomic scientific persistence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_scientific_account_lint as fixtures
from reveal_backend import acceptance, citations, research_execution as execution, scientific_reuse
from reveal_backend.auth import Problem, owned
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.repository import Repository, digest
from reveal_backend.research_work import ResearchWorkService
from reveal_backend.scientific_account_lint import AccountValidationError


class ResearchExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.science = fixtures.ScientificAccountLintTests
        cls.science.setUpClass(); cls.addClassCleanup(cls.science.doClassCleanups)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = Repository(str(self.root/'research.sqlite')); self.repo.migrate()
        self.service = ResearchWorkService(self.repo)
        self.owner = 'local-researcher'; self.work_id = 'local-work'; self.request_id = 'research-request'
        self.package = deepcopy(self.science.package)
        self.package_sha = sha256(canonical_json(self.package))
        for context in (patch('reveal_backend.user_inputs.artifacts_root', return_value=self.root/'artifacts'),
                        patch.object(execution, 'artifacts_root', return_value=self.root/'artifacts'),
                        patch('reveal_backend.user_inputs.s3_enabled', return_value=False),
                        patch.object(acceptance, 'release_root', return_value=self.science.release),
                        patch.object(acceptance, 'LOCK', self.science.lock)):
            context.start(); self.addCleanup(context.stop)
        self.work = {'id':self.work_id, 'research_request_id':self.request_id, 'package_id':'seed',
                     'package_sha256':self.package_sha, 'owner_user_id':self.owner, 'state':'ready'}
        with self.repo.transaction() as tx:
            tx.put('principal', self.owner, self.owner, {'me':{'user_id':self.owner, 'workspace_expires_at':None}})
            tx.put('local_work', self.work_id, self.owner, self.work)
            tx.put('request', self.request_id, self.owner, {'id':self.request_id,
                'question_id':self.package['selection']['knowledge_gap_id'],
                'composer':{}, 'attribution':{'user_id':self.owner, 'principal_kind':'anonymous',
                    'person_id':None,'display_name':None,'orcid':None,'orcid_authenticated':False,'observed_at':'2026-10-05T00:00:00Z'}})
        artifacts = []
        for source in self.package['source_artifacts'].values():
            raw = (self.science.package_path.parent/source['path']).read_bytes()
            artifacts.append(self.service.retain(self.owner, self.work_id, raw, source['path']))
        with self.repo.transaction() as tx:
            tx.put('research_package','seed',self.owner,{'package':self.package,'artifacts':artifacts})

    def operation(self, identity, kind, arguments):
        result = {'id':identity, 'kind':kind, 'owner_user_id':self.owner, 'local_work_id':self.work_id,
                'research_request_id':self.request_id, 'attempt':1, 'arguments':arguments,
                'state':'running', 'lease_token':identity+'-lease'}
        with self.repo.transaction() as tx: tx.put('research_operation', identity, self.owner, result)
        return result

    def imported_draft(self):
        artifact = self.service.retain(self.owner, self.work_id,
            b'A retained independent experimental observation.', 'experiment.txt', purpose='evidence')
        result = execution.import_sources(self.service, self.operation('import-one','import',{'artifact_ids':[artifact['id']]}))
        draft = deepcopy(self.science.draft)
        draft['evidence_items'][0].update(was_derived_from=[result['sources'][0]['source_ids'][1]],
            context='The retained independent observation at /segments/0/text.',
            snippet='A retained independent experimental observation.')
        return draft, result['import_id']

    def submit(self, draft, import_id, identity='submit-one', **extra):
        artifact = self.service.retain(self.owner, self.work_id, canonical_json(draft), 'account.json', purpose='account')
        args = {'package_sha256':self.package_sha, 'account_artifact_ids':[artifact['id']], 'import_ids':[import_id], **extra}
        operation = self.operation(identity,'submit',args)
        try:
            prepared = execution.validate_submission(self.service, operation)
        except AccountValidationError as error:
            self.fail(error.report)
        with self.repo.transaction() as tx:
            result = execution.commit_accounts(self.service,tx,operation,prepared)
        return result, prepared

    def test_independent_import_validate_commit_creates_no_paid_execution(self):
        draft, import_id = self.imported_draft()
        result, prepared = self.submit(draft, import_id)
        self.assertEqual(len(result['account_ids']),1)
        self.assertEqual(result['report']['advisories'][0]['check'],'cfde-grounding-missing')
        document = prepared['validated'][0]['document']
        self.assertTrue(any(item['name']=='REVEAL independent evidence extraction' for item in document['activities']))
        self.assertTrue(any(item['name']=='REVEAL local account submission' for item in document['activities']))
        with self.repo.read_transaction() as tx:
            account = owned(tx,'account',result['account_ids'][0],self.owner)['data']
            self.assertEqual(account['summary']['research_statement']['status'],'not_requested')
            self.assertEqual(account['summary']['execution_mode'],'local')
            for kind in ('job','queue','paragraph'):
                self.assertEqual(tx.list(kind,self.owner),[])
            self.assertEqual(len(tx.list('scientific_document',self.owner)),1)
            self.assertEqual(len(tx.list('account_membership',self.owner)),1)
            from reveal_backend.account_discovery import visible_accounts
            self.assertEqual(visible_accounts(tx,self.owner,attribution=True)[0]['attribution']['user_id'],self.owner)

    def test_invalid_second_document_rejects_entire_batch_before_commit(self):
        draft, import_id = self.imported_draft()
        bad = deepcopy(draft); bad['evidence_items'][0]['snippet']='Invented text absent from the retained source.'
        uploads = [self.service.retain(self.owner,self.work_id,canonical_json(value),f'account-{i}.json',purpose='account')
                   for i,value in enumerate((draft,bad))]
        operation = self.operation('invalid-batch','submit',{'package_sha256':self.package_sha,
            'account_artifact_ids':[item['id'] for item in uploads], 'import_ids':[import_id]})
        with self.assertRaises(AccountValidationError): execution.validate_submission(self.service,operation)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('scientific_document',self.owner),[])
            self.assertEqual(tx.list('account',self.owner),[])

    def test_exact_existing_account_reuse_does_not_mint_new_document_or_paragraph(self):
        draft, import_id = self.imported_draft()
        first, _ = self.submit(draft, import_id)
        with self.repo.transaction() as tx:
            selected = scientific_reuse.search(tx,self.owner,'account')['items'][0]
            selected['purpose'] = 'existing_account'
            receipt = scientific_reuse.create_receipt(tx,self.owner,self.request_id,[selected],'reuse-one')
        operation = self.operation('reuse-only','submit',{'package_sha256':self.package_sha,
            'reuse_receipt_ids':[receipt['id']], 'existing_account_ids':first['account_ids']})
        prepared = execution.validate_submission(self.service,operation)
        with self.repo.transaction() as tx:
            result = execution.commit_accounts(self.service,tx,operation,prepared)
            self.assertEqual(result['account_ids'],[])
            self.assertEqual(result['reused_account_ids'],first['account_ids'])
            self.assertEqual(len(tx.list('scientific_document',self.owner)),1)
            self.assertEqual(len(tx.list('account_membership',self.owner)),1)
            self.assertEqual(tx.list('job',self.owner),[])
            self.assertEqual(tx.list('paragraph',self.owner),[])

    def test_mixed_submission_keeps_the_prior_claim_and_its_attribution(self):
        draft, import_id = self.imported_draft()
        first, first_prepared = self.submit(draft, import_id)
        original = first_prepared['validated'][0]['document']['claims'][0]
        with self.repo.transaction() as tx:
            account = scientific_reuse.search(tx,self.owner,'account')['items'][0]
            claim = scientific_reuse.search(tx,self.owner,'claim')['items'][0]
            receipt = scientific_reuse.create_receipt(tx,self.owner,self.request_id,
                [{**account,'purpose':'existing_account'},{**claim,'purpose':'source_claim'}],'mixed')
        draft['scientific_accounts'][0]['name']='A new locally developed assessment'
        draft['claims'][0]['statement']='A distinct assessment uses the retained earlier interpretation.'
        draft['evidence_items'][0]['source_claims']=[original['id']]
        second, prepared = self.submit(draft,import_id,identity='mixed-submit',
            reuse_receipt_ids=[receipt['id']],existing_account_ids=first['account_ids'])
        document = prepared['validated'][0]['document']
        self.assertIn(original,document['claims'])
        self.assertNotEqual(second['account_ids'],first['account_ids'])
        self.assertEqual(second['reused_account_ids'],first['account_ids'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('scientific_document',self.owner)),2)
            observations=[row['data'] for row in tx.list('scientific_document',self.owner)]
            retained=[item for row in observations for item in row['citation_metadata'] if item['target_id']==original['id']]
            self.assertTrue(retained); self.assertTrue(all(item==retained[0] for item in retained))
            self.assertEqual(tx.list('job',self.owner),[])

    def test_derived_import_cannot_launder_withdrawn_reused_sources(self):
        draft, import_id = self.imported_draft()
        self.submit(draft,import_id)
        with self.repo.transaction() as tx:
            source=tx.list('scientific_document',self.owner)[0]['data']
            records={row['data']['sha256']:row['data'] for row in tx.list('artifact',self.owner)}
            tx.put('publication_snapshot','external-snapshot','external-author',{
                'document':source['document'],'citation_metadata':source['citation_metadata'],
                'artifacts':source['artifact_access'],'artifact_records':records})
            tx.put('publication','external-publication','external-author',{'visibility':'public','version':1,
                'snapshot_id':'external-snapshot','published_at':'2026-10-05','account_id':source['document']['scientific_accounts'][0]['id']})
            selected=next(item for item in scientific_reuse.search(tx,self.owner,'claim')['items']
                          if item['source_kind']=='publication_snapshot')
            receipt=scientific_reuse.create_receipt(tx,self.owner,self.request_id,[selected],'foreign-input')
            input_id=receipt['selections'][0]['eligible_source_ids'][0]
        artifact=self.service.retain(self.owner,self.work_id,b'A locally computed result.','derived.txt',purpose='evidence')
        result=execution.import_sources(self.service,self.operation('derived-import','import',{
            'artifact_ids':[artifact['id']],'reuse_receipt_ids':[receipt['id']],
            'metadata':{'origin':'locally_derived','input_ids':[input_id],'method':'Declared local analysis'}}))
        with self.repo.transaction() as tx:
            tx.put('publication','external-publication','external-author',{'visibility':'private','version':2,'snapshot_id':None})
        with self.assertRaises(Problem) as failure:
            execution.load_context(self.service,self.owner,self.work_id,{'import_ids':[result['import_id']]})
        self.assertEqual(failure.exception.code,'REUSE_AUTHORITY_UNAVAILABLE')

    def test_completed_import_retry_reuses_identical_provenance_without_extracting_again(self):
        _,import_id=self.imported_draft()
        with self.repo.read_transaction() as tx:
            before=tx.get('evidence_import',import_id)['data']
            operation=tx.get('research_operation',import_id)['data']
        with patch('reveal_backend.evidence_imports.materialize_import',side_effect=AssertionError('Immutable retry must not extract')):
            result=execution.import_sources(self.service,operation)
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('evidence_import',import_id)['data'],before)
        self.assertEqual(result['context'],before['context']); self.assertEqual(result['sources'],before['sources'])

    def test_stale_import_worker_cannot_commit_or_overwrite_a_receipt(self):
        artifact=self.service.retain(self.owner,self.work_id,b'A captured local result.','result.txt',purpose='evidence')
        operation=self.operation('stale-import','import',{'artifact_ids':[artifact['id']]})
        stage=execution.stage_context
        def supersede(*args,**kwargs):
            result=stage(*args,**kwargs)
            with self.repo.transaction() as tx:
                current=tx.get('research_operation',operation['id'])['data']; current['lease_token']='new-worker'
                tx.put('research_operation',operation['id'],self.owner,current)
            return result
        with self.repo.read_transaction() as tx: before=len(tx.list('research_artifact',self.owner))
        with patch.object(execution,'stage_context',side_effect=supersede):
            with self.assertRaises(Problem) as failure: execution.import_sources(self.service,operation)
        self.assertEqual(failure.exception.code,'STALE_OPERATION')
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('evidence_import',operation['id']))
            # Staged artifacts commit only with the record that cites them.
            self.assertEqual(len(tx.list('research_artifact',self.owner)),before)


if __name__ == '__main__':
    unittest.main()
