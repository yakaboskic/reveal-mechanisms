"""Network-free exact reading, integrity, paging and transport parity."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from reveal_backend.evidence_reader import *
from reveal_backend.evidence_package import canonical_json
from test_evidence_files import fixture

class ReaderTests(unittest.TestCase):
    def read(self,raw,**args):
        return read_artifact(raw,{'artifact_id':'source','sha256':digest(raw),'size_bytes':len(raw)},artifact_id='source',sha256=digest(raw),**args)
    def test_exact_numeric_tokens_and_json_pointer(self):
        raw=b'{"a/~b":[{"n":0.123456789012345678901,"e":1.2300e-400,"i":900719925474099312345}]}'
        result=self.read(raw,pointer='/a~1~0b/0')
        self.assertIn('1.2300e-400',result['content_json']);self.assertIn('900719925474099312345',result['content_json'])
        self.assertEqual(result['locators'],['/a~1~0b/0/n','/a~1~0b/0/e','/a~1~0b/0/i'])
        self.assertFalse(result['truncated'])
    def test_vector_slice_continuation_selection_and_hash_bound(self):
        raw=canonical_json({'v':list(range(5000))})
        result=self.read(raw,pointer='/v',offset=30,limit=3)
        self.assertEqual(json.loads(result['content_json']),[30,31,32]);self.assertTrue(result['truncated'])
        nxt=self.read(raw,pointer='/v',limit=3,continuation=result['continuation'])
        self.assertEqual(json.loads(nxt['content_json']),[33,34,35])
        final=self.read(raw,pointer='/v',offset=4999,limit=3)
        self.assertFalse(final['truncated']);self.assertFalse(final['complete_selection'])
        for args in ({'pointer':'','limit':3},{'pointer':'/v','limit':4}):
            with self.assertRaisesRegex(EvidenceReadError,'another artifact'):self.read(raw,continuation=result['continuation'],**args)
        with self.assertRaises(EvidenceReadError):self.read(raw+b' ',pointer='/v',limit=3,continuation=result['continuation'])
    def test_duplicate_keys_nonfinite_and_bounds_rejected(self):
        for raw in (b'{"a":1,"a":2}',b'{"n":NaN}',b'{"nested":{"a":1,"a":2}}'):
            with self.assertRaises(EvidenceReadError):self.read(raw)
        with self.assertRaises(EvidenceReadError):self.read(b'{}',limit=True)
        with self.assertRaises(EvidenceReadError):self.read(b'{"s":"'+b'a'*30000+b'"}',pointer='/s')
        with self.assertRaises(EvidenceReadError):self.read(b'{"a":1}',pointer='/a~2')
    def test_text_ranges_have_exact_unicode_and_line_locators(self):
        value=self.read('first\n🧬 second\nthird'.encode(),mode='text',offset=1,limit=1)
        self.assertEqual(value['text'],'🧬 second\n');self.assertEqual(value['locator']['line_start'],2)
        self.assertTrue(value['truncated'])
    def test_oversized_single_line_and_json_string_ranges_replay_exactly(self):
        source=('🧬long single line without newline '*2000).encode()
        text=source.decode(); parts=[]; token=None
        while True:
            page=self.read(source,mode='text_range',limit=16000,continuation=token)
            parts.append(page['text'])
            locator=page['locator']
            self.assertEqual(source[locator['byte_start']:locator['byte_end']].decode(),page['text'])
            self.assertLessEqual(len(json.dumps(page,ensure_ascii=False).encode()),MAX_RESPONSE_BYTES)
            token=page['continuation']
            if token is None: break
        self.assertEqual(''.join(parts),text)
        raw=json.dumps({'body':text}).encode()
        page=self.read(raw,pointer='/body',mode='text_range',offset=17,limit=100)
        self.assertEqual(page['text'],text[17:117]);self.assertEqual(page['locator']['pointer'],'/body')
        for changes in ({'pointer':''},{'mode':'text'},{'limit':99}):
            args={'pointer':'/body','mode':'text_range','limit':100,'continuation':page['continuation'],**changes}
            with self.assertRaises(EvidenceReadError):self.read(raw,**args)
        with self.assertRaises(EvidenceReadError):self.read(b'{"body":123}',mode='text_range',pointer='/body')
    def test_control_characters_ranges_are_bounded_and_continuations_validate_shape(self):
        result=self.read(b'\x00'*20000,mode='text_range',limit=16000)
        self.assertTrue(result['truncated']);self.assertLessEqual(len(json.dumps(result).encode()),MAX_RESPONSE_BYTES)
        for value in ([],1,None,'text'):
            with self.assertRaises(EvidenceReadError):self.read(b'{}',continuation=base64.b64encode(json.dumps(value).encode()).decode())
    def test_offline_parity_integrity_and_path_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'input/sources').mkdir(parents=True)
            package,sources=fixture();raw=canonical_json(package)
            (root/'input/evidence-package.json').write_bytes(raw)
            (root/'input/manifest.json').write_bytes(canonical_json({'package_sha256':digest(raw),'files':{'evidence-package.json':digest(raw)}}))
            descriptor=package['source_artifacts']['capture']; path=root/'input'/descriptor['path'];path.write_bytes(sources['capture'])
            args={'artifact_id':'capture','sha256':descriptor['sha256'],'pointer':'/data/0'}
            with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
                local=WorkspaceReader(root).read(**args)
                hosted=read_artifact(sources['capture'],{**descriptor,'artifact_id':'capture'},**args)
                self.assertEqual(local,hosted)
                with self.assertRaises(EvidenceReadError):WorkspaceReader(root).read(artifact_id='missing',sha256='a'*64)
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(EvidenceReadError,'checksum'):WorkspaceReader(root).read(**args)
            path.unlink();path.symlink_to(root/'input/evidence-package.json')
            with self.assertRaisesRegex(EvidenceReadError,'symlink'):WorkspaceReader(root).read(**args)
            for bad in ('../outside','.reveal-setup/state.json','/etc/passwd','input/../start.py'):
                with self.assertRaises(EvidenceReadError):safe_read(root,bad)
    def test_directory_and_final_symlink_races_cannot_escape_open_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'input').mkdir();(root/'outside').mkdir()
            (root/'input/source').write_bytes(b'allowed');(root/'outside/source').write_bytes(b'secret')
            real_open=os.open
            def swap_directory(path,flags,*args,**kwargs):
                handle=real_open(path,flags,*args,**kwargs)
                if path=='input':
                    (root/'input').rename(root/'original')
                    (root/'input').symlink_to(root/'outside',target_is_directory=True)
                return handle
            with patch('reveal_backend.evidence_reader.os.open',side_effect=swap_directory):
                self.assertEqual(safe_read(root,'input/source'),b'allowed')
            (root/'input').unlink();(root/'original').rename(root/'input')
            def swap_file(path,flags,*args,**kwargs):
                if path=='source':
                    (root/'input/source').unlink();(root/'input/source').symlink_to(root/'outside/source')
                return real_open(path,flags,*args,**kwargs)
            with patch('reveal_backend.evidence_reader.os.open',side_effect=swap_file):
                with self.assertRaisesRegex(EvidenceReadError,'symlink'):safe_read(root,'input/source')
    def test_manifest_duplicate_keys_and_expected_seed_pin_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'input').mkdir();raw=b'{"source_artifacts":{}}'
            (root/'input/evidence-package.json').write_bytes(raw)
            manifest=root/'input/manifest.json'
            manifest.write_text('{"package_sha256":"'+digest(raw)+'","package_sha256":"'+digest(raw)+'"}')
            with self.assertRaisesRegex(EvidenceReadError,'Duplicate'):WorkspaceReader(root).inventory()
            manifest.write_text(json.dumps({'package_sha256':digest(raw)}))
            with self.assertRaisesRegex(EvidenceReadError,'scope'):WorkspaceReader(root,expected_seed_sha256='a'*64).inventory()
    def test_public_and_closure_sources_and_server_aliases_are_package_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'input/sources').mkdir(parents=True)
            seed,sources=fixture();seed_raw=canonical_json(seed);seed_hash=digest(seed_raw)
            (root/'input/evidence-package.json').write_bytes(seed_raw)
            (root/'input/manifest.json').write_bytes(canonical_json({'package_sha256':seed_hash}))
            for key,entry in seed['source_artifacts'].items():(root/'input'/entry['path']).write_bytes(sources[key])
            capture_hash='c'*64; public=root/'evidence/public'/capture_hash;public.mkdir(parents=True)
            public_entry={**seed['source_artifacts']['capture'],'artifact_id':'public-server-id'}
            (public/public_entry['path']).parent.mkdir(parents=True);(public/public_entry['path']).write_bytes(sources['capture'])
            (public/'manifest.json').write_bytes(canonical_json({'capture_id':capture_hash,
                'source_artifacts':{'public-capture':public_entry},'artifacts':[public_entry]}))
            closure={**seed,'closure_marker':True};closure_raw=canonical_json(closure);checksum=digest(closure_raw)
            directory=root/'evidence/closures'/checksum;directory.mkdir(parents=True)
            (directory/'evidence-package.json').write_bytes(closure_raw)
            entry={**seed['source_artifacts']['capture'],'artifact_id':'private-server-id'}
            (directory/entry['path']).parent.mkdir(parents=True);(directory/entry['path']).write_bytes(sources['capture'])
            manifest={'package_sha256':checksum,'seed_sha256':seed_hash,'files':[entry]}
            (directory/'manifest.json').write_bytes(canonical_json(manifest))
            with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
                for identity in ('public-capture','public-server-id','capture','private-server-id'):
                    value=WorkspaceReader(root).read(artifact_id=identity,sha256=entry['sha256'],pointer='/data/0/loading')
                    self.assertEqual(value['content_json'],'0.123456789012345678901')
            for bad in ({**entry,'path':'setup.json'},{**entry,'sha256':'f'*64},{**entry,'dapper_file_id':'forged:file'},
                        {**entry,'path':'../../.reveal-setup/state.json'}):
                (directory/'manifest.json').write_bytes(canonical_json({**manifest,'files':[bad]}))
                with self.assertRaisesRegex(EvidenceReadError,'differs'):WorkspaceReader(root).inventory()
    def test_source_layout_cannot_name_credentials_or_unrelated_subtrees(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'input/sources').mkdir(parents=True)
            raw=b'{"n":0.123456789012345678901}'
            entry={'path':'sources/retained.json','sha256':digest(raw),'size_bytes':len(raw)}
            package={'source_artifacts':{'query':entry}}
            (root/'input/sources/retained.json').write_bytes(raw)
            def save():
                package_raw=canonical_json(package)
                (root/'input/evidence-package.json').write_bytes(package_raw)
                (root/'input/manifest.json').write_bytes(canonical_json({'package_sha256':digest(package_raw)}))
            save()
            self.assertEqual(WorkspaceReader(root).read(artifact_id='query',sha256=digest(raw),pointer='/n')['content_json'],
                             '0.123456789012345678901')
            for path in ('setup.json','sources/../setup.json','sources/.reveal-setup/state.json','sources/../../setup.json','queries/unrelated.json'):
                entry['path']=path;save()
                with self.assertRaisesRegex(EvidenceReadError,'layout'):WorkspaceReader(root).inventory()
