"""Workspace claims preserve research results without widening old credentials."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.auth import Problem
from reveal_backend.repository import Repository
from reveal_backend.research_work import ResearchWorkService, authenticate


class ResearchOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo=Repository(str(Path(temporary.name)/'transfer.sqlite')); self.repo.migrate()
        self.token='rvlm_test-transfer-token'; self.owner='anonymous-owner'; self.target='registered-owner'
        with self.repo.transaction() as tx:
            for identity in (self.owner,self.target):
                tx.put('principal',identity,identity,{'retired':False,'me':{'user_id':identity,
                    'principal_kind':'registered','workspace_expires_at':None}})
            tx.put('request','request',self.owner,{'id':'request','attribution':{'user_id':self.owner}})
            tx.put('local_work','work',self.owner,{'id':'work','owner_user_id':self.owner,'research_request_id':'request',
                'state':'ready','expires_at':'2999-01-01T00:00:00Z'})
            tx.put('research_pin','request',self.owner,{'id':'request','state':'active'})
            tx.put('research_access',hashlib.sha256(self.token.encode()).hexdigest(),self.owner,{
                'grant_id':'grant','local_work_id':'work','research_request_id':'request','kind':'local',
                'expires_at':'2999-01-01T00:00:00Z','revoked_at':None,'issued_principal_kind':'registered'})

    def operation(self,identity,kind='query',grant='grant',state='received'):
        value={'id':identity,'kind':kind,'owner_user_id':self.owner,'research_request_id':'request',
               'local_work_id':'work','grant_id':grant,'state':state,'attempt':0,'arguments':{}}
        with self.repo.transaction() as tx: tx.put('research_operation',identity,self.owner,value)

    def test_transfer_revokes_token_and_fences_pending_operations_but_keeps_attribution(self):
        self.operation('query',state='running'); self.operation('prepare','prepare',None,'running')
        with self.repo.transaction() as tx:
            self.assertEqual(authenticate(tx,'Bearer '+self.token)['owner'],self.owner)
            tx.transfer(self.owner,self.target)
            work=tx.get('local_work','work')
            self.assertEqual(work['owner'],self.target); self.assertEqual(work['data']['owner_user_id'],self.target)
            query=tx.get('research_operation','query')['data']
            self.assertEqual(query['state'],'failed'); self.assertEqual(query['owner_user_id'],self.target)
            self.assertEqual(query['error']['code'],'WORKSPACE_TRANSFERRED')
            self.assertEqual(tx.get('research_operation','prepare')['data']['state'],'received')
            self.assertEqual(tx.get('request','request')['data']['attribution']['user_id'],self.owner)
            with self.assertRaises(Problem) as failure: authenticate(tx,'Bearer '+self.token)
            self.assertEqual(failure.exception.code,'MCP_GRANT_EXPIRED')

    def test_transfer_during_execution_fences_old_worker_without_error_handler_rollback(self):
        self.operation('query')
        service=ResearchWorkService(self.repo)
        def transfer_during_query(operation):
            with self.repo.transaction() as tx: tx.transfer(self.owner,self.target)
            return {'unexpected':'old execution must not commit this'}
        with patch.object(service,'query',side_effect=transfer_during_query): service.run_operation('query')
        with self.repo.read_transaction() as tx:
            row=tx.get('research_operation','query')
            self.assertEqual(row['owner'],self.target)
            self.assertEqual(row['data']['state'],'failed')
            self.assertNotIn('result',row['data']); self.assertNotIn('lease_token',row['data'])

    def test_retirement_finishes_pending_work_and_releases_closed_pin(self):
        self.operation('query')
        with self.repo.transaction() as tx:
            principal=tx.get('principal',self.owner)['data']; principal['retired']=True
            tx.put('principal',self.owner,self.owner,principal)
            work=tx.get('local_work','work')['data']; work['state']='closed'; tx.put('local_work','work',self.owner,work)
        ResearchWorkService(self.repo).run_operation('query')
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('research_operation','query')['data']['state'],'failed')
            self.assertEqual(tx.get('research_pin','request')['data']['state'],'released')


if __name__=='__main__': unittest.main()
