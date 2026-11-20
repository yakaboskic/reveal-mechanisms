"""Research records survive reference archive passes.

The reference reload's research-pin purge guard was removed with the reload: publishing a reference
release swaps its tables in place and purges no generation (docs/reference-release.md).
"""
import unittest

from reveal_backend import reference_archive as archive


class ResearchRetentionTests(unittest.TestCase):
    def test_new_durable_record_kinds_are_retained(self):
        kinds=['local_work','research_pin','research_package','research_access','research_grant_issue','research_idempotency',
               'research_operation','research_artifact','research_upload','evidence_receipt','evidence_import','reuse_receipt',
               'reuse_idempotency','reuse_authorization','scientific_dependencies','scientific_share','research_setup_ticket']
        self.assertTrue(all(archive.classify(kind)==archive.KEEP for kind in kinds))


if __name__=='__main__': unittest.main()
