"""New workspaces keep a bounded index and original artifacts, never recursive files."""
import json
import unittest
from reveal_backend.evidence_files import INDEX_PATH, build_evidence_files, child_pointer
from reveal_backend.evidence_package import canonical_json, sha256


def fixture():
    raw=b'{"data":[{"gene":"TEST","loading":0.123456789012345678901,"exponent":1.2300e-400,"integer":900719925474099312345}]}'
    descriptor={'sha256':sha256(raw),'size_bytes':len(raw),'format':'json','path':'sources/'+sha256(raw)+'.json','dapper_file_id':'urn:test:file'}
    return {'selection':{'knowledge_gap_id':'urn:test:gap','eaggl_mechanism_ids':['factor:one']},'pigean':{'mechanisms':{'factor:one':{'dapper_id':'urn:test:mechanism'}}},'dismech':{'knowledge_gap':{'source_ref':{'artifact_id':'capture','pointer':'/data/0'}}},'source_artifacts':{'capture':descriptor}}, {'capture':raw}

class EvidenceFilesTests(unittest.TestCase):
    def test_file_count_constant_with_vector_growth_and_no_expansion(self):
        package,sources=fixture()
        for count in (0,100,30720):
            package['retrieval_audit']={'embedding':[0.123456]*count}
            raw=canonical_json(package);files=build_evidence_files(raw,source_bytes=sources)
            self.assertEqual(set(files),{INDEX_PATH})
            index=json.loads(files[INDEX_PATH]);self.assertEqual(index['format'],'reveal.evidence-index/2')
            self.assertEqual(index['canonical_package']['sha256'],sha256(raw))
            self.assertLess(len(files[INDEX_PATH]),6000)
    def test_hash_verification_and_deterministic_source_locators(self):
        package,sources=fixture();raw=canonical_json(package)
        self.assertEqual(build_evidence_files(raw,source_bytes=sources),build_evidence_files(raw,source_bytes=sources))
        for bad in ({},{'capture':b'changed'},{**sources,'extra':b'{}'}):
            with self.assertRaises(ValueError):build_evidence_files(raw,source_bytes=bad)
        self.assertEqual(child_pointer('/data','a/~b'),'/data/a~1~0b')
