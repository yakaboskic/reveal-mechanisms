"""Pins and purge use the same per-prefix application transaction gates."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reveal_backend import reference_archive as archive
from reveal_backend import reference_generation as rg
from reveal_backend import reference_reload as reload
from reveal_backend.repository import Repository


class ResearchRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repos={prefix:Repository(str(Path(self.temp.name)/(prefix+'.sqlite')),table_prefix=prefix) for prefix in ('reveal','reveal_qa')}
        for repo in self.repos.values(): repo.migrate()
        self.services=SimpleNamespace(repository=self.repos.__getitem__)
        self.targets={name:{'prefix':name} for name in self.repos}
        self.generation='a'*64

    def test_expired_active_pin_blocks_until_explicitly_released(self):
        repo=self.repos['reveal']
        with repo.transaction() as tx:
            tx.put('research_pin','r','owner',{'generation_id':self.generation,'state':'active','expires_at':'2000-01-01T00:00:00Z'})
        self.assertEqual(len(reload.active_research_pins(repo,self.generation)),1)
        with self.assertRaises(reload.Refused):
            with reload.research_purge_guard(self.services,self.targets,[self.generation]): self.fail('Purge entered with an active pin')
        for repo in self.repos.values():
            with repo.read_transaction() as tx: self.assertIsNone(tx.get(rg.CONTROL_KIND,rg.CONTROL_ID))

    def test_all_prefix_gates_close_and_restore_after_failure(self):
        prior={'closed':False,'reason':'previous operator state','changed_at':'fixed'}
        with self.repos['reveal'].transaction() as tx: tx.put(rg.CONTROL_KIND,rg.CONTROL_ID,'catalog',prior)
        with self.assertRaisesRegex(RuntimeError,'simulated'):
            with reload.research_purge_guard(self.services,self.targets,[self.generation]):
                for repo in self.repos.values():
                    with repo.read_transaction() as tx: self.assertTrue(rg.read_gate(tx)['closed'])
                raise RuntimeError('simulated delete failure')
        with self.repos['reveal'].read_transaction() as tx: self.assertEqual(tx.get(rg.CONTROL_KIND,rg.CONTROL_ID)['data'],prior)
        with self.repos['reveal_qa'].read_transaction() as tx: self.assertIsNone(tx.get(rg.CONTROL_KIND,rg.CONTROL_ID))

    def test_final_check_catches_new_pin_after_plan_and_preserves_operator_change(self):
        original=reload.active_research_pins
        called=[]
        def race(repo,generation):
            if not called:
                called.append(True)
                with self.repos['reveal_qa'].transaction() as tx:
                    tx.put('research_pin','late','owner',{'state':'active','generation_id':generation})
            return original(repo,generation)
        with patch.object(reload,'active_research_pins',race),self.assertRaises(reload.Refused):
            with reload.research_purge_guard(self.services,self.targets,[self.generation]): self.fail('Missed late pin')
        with self.repos['reveal_qa'].transaction() as tx: tx.remove('research_pin','late')
        with reload.research_purge_guard(self.services,self.targets,[self.generation]):
            with self.repos['reveal'].transaction() as tx: rg.set_gate(tx,True,reason='operator intervention')
        with self.repos['reveal'].read_transaction() as tx: self.assertEqual(rg.read_gate(tx)['reason'],'operator intervention')

    def test_new_durable_record_kinds_are_retained(self):
        kinds=['local_work','research_pin','research_package','research_access','research_grant_issue','research_idempotency',
               'research_operation','research_artifact','research_upload','evidence_receipt','evidence_import','reuse_receipt',
               'reuse_idempotency','reuse_authorization','scientific_dependencies','scientific_share','research_setup_ticket']
        self.assertTrue(all(archive.classify(kind)==archive.KEEP for kind in kinds))


if __name__=='__main__': unittest.main()
