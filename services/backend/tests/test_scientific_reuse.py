"""Reuse must pin exact observations without granting permanent private access."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import scientific_reuse as reuse, user_inputs
from reveal_backend.auth import Problem, owned
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.repository import Repository, digest

GAP='dapper:KnowledgeGap.gap'
ACCOUNT='dapper:ScientificAccount.account'
CLAIM='dapper:Claim.claim'
FILE='dapper:File.file'
PROPOSITION='dapper:Proposition.proposition'


class ScientificReuseTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo=Repository(str(Path(temporary.name)/'reuse.sqlite')); self.repo.migrate()
        fields={'scientific_accounts':['question','component_claims'], 'claims':['proposition','was_derived_from'],
                'propositions':[], 'files':[], 'knowledge_gaps':[]}
        for context in (patch('reveal_backend.acceptance._reference_fields',return_value=fields),
                        patch('reveal_backend.dapper_release.verify_release',return_value={'lock_sha256':'test'})):
            context.start(); self.addCleanup(context.stop)
        self.raw=b'{"data":[{"beta":0.125}]}'
        file={'id':FILE,'sha256':sha256(self.raw),'size_in_bytes':len(self.raw),'filename':'evidence.json','mime_type':'application/json'}
        self.doc={'knowledge_gaps':[{'id':GAP,'text':'Exact selected gap'}],
            'scientific_accounts':[{'id':ACCOUNT,'question':GAP,'component_claims':[CLAIM]}],
            'propositions':[{'id':PROPOSITION,'statement':'A scoped proposition'}],
            'claims':[{'id':CLAIM,'proposition':PROPOSITION,'statement':'A prior attributed assessment','was_derived_from':[FILE]}],
            'files':[file]}
        self.record={'file':file,'sha256':file['sha256'],'path':str(Path(temporary.name)/'source.json'),
            'research_source':{'sha256':file['sha256'],'dapper_file_id':FILE,'format':'json','origin':'verified-test-source'},
            'eligible_evidence':True,'cfde':False}
        Path(self.record['path']).write_bytes(self.raw)
        with self.repo.transaction() as tx:
            tx.put('request','request','reader',{'question_id':GAP})
            self.seed(tx,'author',self.doc)

    def seed(self,tx,owner,document):
        checksum=sha256(canonical_json(document))
        tx.put('scientific_document',digest([owner,checksum]),owner,{'sha256':checksum,'document':document,'citation_metadata':[],'artifact_access':{}})
        tx.put('account_membership',digest([owner,ACCOUNT]),owner,{'account_id':ACCOUNT,'summary':{
            'account':document['scientific_accounts'][0],'knowledge_gap':document['knowledge_gaps'][0],'created_at':'2026-10-05'}})
        tx.put('artifact',digest([owner,self.record['sha256']]),owner,self.record)
        return checksum

    def publish(self,tx,snapshot='snapshot',document=None):
        doc=document or self.doc
        tx.put('publication_snapshot',snapshot,'author',{'document':doc,'citation_metadata':[],
            'artifacts':{},'artifact_records':{self.record['sha256']:self.record}})
        tx.put('publication','publication','author',{'visibility':'public','version':1,'snapshot_id':snapshot,
            'published_at':'2026-10-05','account_id':ACCOUNT})

    def selected(self,tx,kind='claim'):
        return reuse.search(tx,'reader',kind)['items'][0]

    def test_private_search_does_not_leak_ids_counts_or_candidates(self):
        with self.repo.transaction() as tx:
            result=reuse.search(tx,'reader','claim',query='prior')
            self.assertEqual(result['items'],[]); self.assertEqual(result['total'],0)
            self.assertEqual(reuse.search(tx,'author','claim')['total'],1)
            self.publish(tx)
            self.assertEqual(reuse.search(tx,'reader','claim')['total'],1)

    def test_receipt_is_idempotent_exact_and_does_not_create_grants(self):
        with self.repo.transaction() as tx:
            self.publish(tx); selected=self.selected(tx)
            receipt=reuse.create_receipt(tx,'reader','request',[selected],'key')
            again=reuse.create_receipt(tx,'reader','request',[selected],'key')
            self.assertEqual(receipt,again)
            self.assertEqual(tx.list('grant','reader'),[])
            context=reuse.resolve_receipts(tx,'reader','request',[receipt['id']])['contexts'][0]
            self.assertEqual(context['dapper_context']['claims'],self.doc['claims'])
            self.assertNotIn('scientific_accounts',context['dapper_context'])
            self.assertEqual(context['eligible_source_ids'],[FILE])
            altered={**selected,'payload_sha256':'0'*64}
            with self.assertRaises(Problem) as failure:
                reuse.create_receipt(tx,'reader','request',[altered],'key')
            self.assertEqual(failure.exception.code,'IDEMPOTENCY_CONFLICT')
            with self.assertRaises(Problem) as failure:
                reuse.get_object(tx,'reader',altered)
            self.assertEqual(failure.exception.code,'REUSE_PAYLOAD_CONFLICT')

    def test_withdrawal_blocks_commit_and_copied_owned_dependencies(self):
        with self.repo.transaction() as tx:
            self.publish(tx); selected=self.selected(tx)
            receipt=reuse.create_receipt(tx,'reader','request',[selected],'key')
            reuse.record_dependencies(tx,'reader','request',[receipt['id']],['dapper:ScientificAccount.new'])
            self.seed(tx,'reader',self.doc)  # An owned copy cannot launder public access.
            tx.put('publication','publication','author',{'visibility':'private','version':2,'snapshot_id':None})
            for action in (lambda:reuse.resolve_receipts(tx,'reader','request',[receipt['id']]),
                           lambda:reuse.authorize_object(tx,'reader',CLAIM),
                           lambda:reuse.authorize_publication(tx,'reader','dapper:ScientificAccount.new')):
                with self.assertRaises(Problem) as failure: action()
                self.assertEqual(failure.exception.code,'REUSE_AUTHORITY_UNAVAILABLE')
            self.assertEqual(reuse.search(tx,'reader','claim')['total'],0)
            reuse.authorize_object(tx,'reader','dapper:ScientificAccount.new')

    def test_alternate_authority_requires_identical_payload_and_closure(self):
        with self.repo.transaction() as tx:
            self.publish(tx); receipt=reuse.create_receipt(tx,'reader','request',[self.selected(tx)],'key')
            modified=deepcopy(self.doc); modified['claims'][0]['statement']='Changed observation under same ID'
            self.publish(tx,'replacement',modified)
            with self.assertRaises(Problem): reuse.resolve_receipts(tx,'reader','request',[receipt['id']])
            self.publish(tx,'exact-replacement',self.doc)
            resolved=reuse.resolve_receipts(tx,'reader','request',[receipt['id']])
            self.assertEqual(resolved['replacement_authorizations'][0]['authorization']['source_id'],'exact-replacement')

    def test_reusing_an_owned_copy_does_not_erase_inherited_authority(self):
        with self.repo.transaction() as tx:
            self.publish(tx)
            first=reuse.create_receipt(tx,'reader','request',[self.selected(tx)],'first')
            reuse.record_dependencies(tx,'reader','request',[first['id']],[ACCOUNT])
            self.seed(tx,'reader',self.doc)
            owned=next(item for item in reuse.search(tx,'reader','claim')['items'] if item['source_kind']=='owned_document')
            second=reuse.create_receipt(tx,'reader','request',[owned],'second')
            self.assertTrue(second['selections'][0]['dependency_bindings'])
            tx.put('publication','publication','author',{'visibility':'private','version':2,'snapshot_id':None})
            with self.assertRaises(Problem) as failure:
                reuse.resolve_receipts(tx,'reader','request',[second['id']])
            self.assertEqual(failure.exception.code,'REUSE_AUTHORITY_UNAVAILABLE')

    def test_account_reuse_pins_exact_question_and_needs_no_new_document(self):
        with self.repo.transaction() as tx:
            self.publish(tx); selected={**self.selected(tx,'account'),'purpose':'existing_account'}
            receipt=reuse.create_receipt(tx,'reader','request',[selected],'key')
            result=reuse.resolve_receipts(tx,'reader','request',[receipt['id']])
            self.assertEqual(result['existing_accounts'][0]['account_id'],ACCOUNT)
            tx.put('request','other','reader',{'question_id':'different-gap'})
            with self.assertRaises(Problem) as failure:
                reuse.create_receipt(tx,'reader','other',[selected],'other-key')
            self.assertEqual(failure.exception.code,'REUSE_QUESTION_MISMATCH')

    def test_missing_source_roles_fail_closed(self):
        with self.repo.transaction() as tx:
            unknown=deepcopy(self.record); unknown.pop('research_source'); unknown.pop('eligible_evidence'); unknown.pop('cfde')
            self.publish(tx)
            snapshot=tx.get('publication_snapshot','snapshot')['data']
            snapshot['artifact_records'][self.record['sha256']]=unknown
            tx.put('publication_snapshot','snapshot','author',snapshot)
            with self.assertRaises(Problem) as failure:
                reuse.create_receipt(tx,'reader','request',[self.selected(tx)],'key')
            self.assertEqual(failure.exception.code,'REUSE_DEPENDENCY_UNAVAILABLE')

    def test_borrowed_artifact_and_exact_citation_reads_recheck_authority(self):
        from reveal_backend.citations import get
        with self.repo.transaction() as tx:
            self.publish(tx)
            snapshot=tx.get('publication_snapshot','snapshot')['data']
            metadata={'target_id':CLAIM,'metadata_revision':1,'title':'Exact retained byline','byline':[{'display_name':'Original author'}]}
            snapshot['citation_metadata']=[metadata]
            tx.put('publication_snapshot','snapshot','author',snapshot)
            receipt=reuse.create_receipt(tx,'reader','request',[self.selected(tx)],'read-guard')
            reuse.record_dependencies(tx,'reader','request',[receipt['id']],['dapper:ScientificAccount.own'])
            tx.put('artifact',digest(['reader',self.record['sha256']]),'reader',self.record)
            self.assertEqual(get(tx,'reader',CLAIM,None),metadata)
            with self.assertRaises(Problem): get(tx,'reader',CLAIM,2)
            self.assertEqual(owned(tx,'artifact',digest(['reader',self.record['sha256']]),'reader')['data'],self.record)
            tx.put('publication','publication','author',{'visibility':'private','version':2,'snapshot_id':None})
            for read in (lambda:get(tx,'reader',CLAIM,None),
                         lambda:owned(tx,'artifact',digest(['reader',self.record['sha256']]),'reader')):
                with self.assertRaises(Problem) as failure: read()
                self.assertEqual(failure.exception.code,'REUSE_AUTHORITY_UNAVAILABLE')
            own={'scientific_accounts':[{'id':'dapper:ScientificAccount.own','component_claims':[CLAIM]}],
                 'claims':self.doc['claims'],'files':self.doc['files']}
            visible=reuse.readable_document(tx,'reader',own)
            self.assertEqual(visible['scientific_accounts'],own['scientific_accounts'])
            self.assertEqual(visible['claims'],[]); self.assertEqual(visible['files'],[])

    def test_legacy_provenance_visibility_batches_dependency_lookup(self):
        with self.repo.read_transaction() as tx:
            with patch.object(tx,'get_many',wraps=tx.get_many) as batch, patch.object(tx,'get',wraps=tx.get) as single:
                self.assertEqual(reuse.readable_document(tx,'author',self.doc),self.doc)
                self.assertEqual(batch.call_count,1)
                self.assertEqual(single.call_count,0)

    def test_workspace_transfer_cannot_erase_borrowed_artifact_guards(self):
        with self.repo.transaction() as tx:
            self.publish(tx)
            receipt=reuse.create_receipt(tx,'reader','request',[self.selected(tx)],'transfer')
            reuse.record_dependencies(tx,'reader','request',[receipt['id']],['dapper:ScientificAccount.own'])
            tx.put('artifact',digest(['reader',self.record['sha256']]),'reader',self.record)
            tx.transfer('reader','registered')
            reuse.resolve_receipts(tx,'registered','request',[receipt['id']])
            self.assertIsNone(tx.get('scientific_dependencies',digest(['reader',FILE])))
            self.assertIsNotNone(tx.get('scientific_dependencies',digest(['registered',FILE])))
            tx.put('publication','publication','author',{'visibility':'private','version':2,'snapshot_id':None})
            with self.assertRaises(Problem) as failure:
                owned(tx,'artifact',digest(['registered',self.record['sha256']]),'registered')
            self.assertEqual(failure.exception.code,'REUSE_AUTHORITY_UNAVAILABLE')

    def test_workspace_transfer_keeps_exact_own_document_receipts_authorized(self):
        with self.repo.transaction() as tx:
            tx.put('request','own-request','author',{'question_id':GAP})
            selected=reuse.search(tx,'author','claim')['items'][0]
            receipt=reuse.create_receipt(tx,'author','own-request',[selected],'own-transfer')
            tx.transfer('author','registered')
            result=reuse.resolve_receipts(tx,'registered','own-request',[receipt['id']])
            self.assertEqual(result['contexts'][0]['dapper_context']['claims'],self.doc['claims'])
            self.assertEqual(tx.get('reuse_receipt',receipt['id'])['data']['selections'][0]['source_owner'],'author')

    def test_import_and_status_replays_do_not_expose_withdrawn_borrowed_context(self):
        import hashlib
        from reveal_backend.research_tools import dispatch
        from reveal_backend.research_work import ResearchWorkService
        service=ResearchWorkService(self.repo); token='rvlm_read-guard'
        with self.repo.transaction() as tx:
            self.publish(tx)
            receipt=reuse.create_receipt(tx,'reader','request',[self.selected(tx)],'replay-read')
            tx.put('principal','reader','reader',{'me':{'user_id':'reader','principal_kind':'registered','workspace_expires_at':None}})
            tx.put('local_work','work','reader',{'id':'work','research_request_id':'request','state':'ready','expires_at':'2999-01-01T00:00:00Z'})
            tx.put('research_access',hashlib.sha256(token.encode()).hexdigest(),'reader',{
                'grant_id':'grant','kind':'local','issued_principal_kind':'registered','local_work_id':'work','research_request_id':'request',
                'expires_at':'2999-01-01T00:00:00Z','revoked_at':None})
            context=receipt['selections'][0]['dapper_context']
            imported={'id':'import','local_work_id':'work','research_request_id':'request',
                      'reuse_receipt_ids':[receipt['id']],'context':{'dapper_context':context}}
            tx.put('evidence_import','import','reader',imported)
            imported_op={'id':'import','kind':'import','state':'succeeded','local_work_id':'work','research_request_id':'request',
                         'arguments':{},'result':{'context':imported['context']}}
            submitted_op={'id':'submitted','kind':'submit','state':'accepted','local_work_id':'work','research_request_id':'request',
                          'arguments':{'import_ids':['import']},'result':{'context':imported['context']},'report':{'snippet':'borrowed'},
                          'account_ids':['dapper:ScientificAccount.own'],'reused_account_ids':[ACCOUNT]}
            for operation in (imported_op,submitted_op): tx.put('research_operation',operation['id'],'reader',operation)
        self.assertEqual(dispatch(service,'Bearer '+token,'get_evidence_import',{'local_work_id':'work','import_id':'import'})['context'],imported['context'])
        with self.repo.transaction() as tx:
            tx.put('publication','publication','author',{'visibility':'private','version':2,'snapshot_id':None})
        for name,args in (('get_evidence_import',{'import_id':'import'}),('get_operation',{'operation_id':'import'}),
                          ('get_submission',{'submission_id':'submitted'})):
            with self.subTest(tool=name),self.assertRaises(Problem) as failure:
                dispatch(service,'Bearer '+token,name,{'local_work_id':'work',**args})
            self.assertEqual(failure.exception.code,'REUSE_AUTHORITY_UNAVAILABLE')
        with self.repo.read_transaction() as tx, patch('reveal_backend.research_work.connection_instructions',return_value={}):
            view=service.view(tx,'reader','work')['submissions'][0]
            self.assertEqual(view['state'],'accepted'); self.assertEqual(view['account_ids'],submitted_op['account_ids'])
            self.assertNotIn('result',view); self.assertNotIn('report',view); self.assertNotIn('reused_account_ids',view)
            self.assertEqual(view['error']['code'],'REUSE_AUTHORITY_UNAVAILABLE')

    def test_local_work_private_inputs_still_prevent_publication(self):
        with self.repo.transaction() as tx:
            tx.put('request','private','reader',{'user_inputs':{'context':'private notes'}})
            tx.put('local_work','local','reader',{'research_request_id':'private'})
            with self.assertRaises(Problem) as failure:
                user_inputs.prevent_private_publication(tx,'local')
            self.assertEqual(failure.exception.code,'PRIVATE_RESEARCH_INPUTS')


if __name__=='__main__': unittest.main()
