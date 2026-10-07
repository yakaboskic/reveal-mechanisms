"""The warm DAPPER helper returns the per-call programs' exact results, and release verification stays exact."""
import ast
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

from reveal_backend import acceptance, box_paragraph, dapper_helper, dapper_release
from reveal_backend.dapper_release import clone_release, verify_release
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, sha256
from reveal_backend.runtime_config import CURRENT_DAPPER_SNAPSHOT

EXAMPLES = CURRENT_DAPPER_SNAPSHOT/'snapshot/schema/examples'


def make_release(root):
    """A locally tagged copy of the vendored DAPPER schema, cloned and locked like a deployed release."""
    origin = root/'origin'; shutil.copytree(CURRENT_DAPPER_SNAPSHOT/'snapshot/schema', origin/'schema')
    for cache in origin.rglob('__pycache__'): shutil.rmtree(cache)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(origin), *args], text=True, stderr=subprocess.DEVNULL).strip()
    identity = ['-c', 'user.name=REVEAL test', '-c', 'user.email=test@example.invalid']
    git('init'); git('add', 'schema'); git(*identity, 'commit', '-m', 'Helper fixture')
    git(*identity, 'tag', '-a', 'helper-1', '-m', 'Helper fixture release')
    files = {str(p.relative_to(origin)): sha256(p.read_bytes()) for p in (origin/'schema').rglob('*')
             if p.is_file() and p.suffix in ('.py', '.yaml', '.yml', '.json')}
    lock = root/'release.json'
    lock.write_bytes(canonical_json({'lock_version': 'reveal.dapper-release/1', 'repository': str(origin), 'tag': 'helper-1',
        'tag_object': git('rev-parse', 'refs/tags/helper-1'), 'commit': git('rev-parse', 'HEAD'), 'files': files,
        'compatible_input_snapshots': []}))
    clone_release(root/'runtime', lock)
    return root/'runtime', lock


def documents():
    """Real DAPPER documents with paragraphs, a clean copy, and variants that produce lint findings."""
    account = yaml.safe_load((EXAMPLES/'example_scientific_account.yaml').read_text())
    pigean = yaml.safe_load((EXAMPLES/'example_pigean_claims.yaml').read_text())
    clean = {key: value for key, value in account.items() if key != '_illustrative'}
    invented = deepcopy(clean); invented['claims'][0]['confidence'] = 'high'
    unminted = deepcopy(clean); unminted['paragraphs'][0]['id'] = 'urn:test:paragraph'; unminted['paragraphs'][0]['text'] += ' Ünïcode.'
    return [account, clean, pigean, invented, unminted]


def paragraph_inputs():
    paragraph_input = {'format': 'reveal.paragraph-input/1', 'account_id': 'urn:test:account',
        'account_document': {'scientific_accounts': [{'id': 'urn:test:account', 'component_claims': ['urn:test:claim']}]},
        'allowed_citations': [{'target_id': 'urn:test:claim', 'citation_metadata_revision': 1}]}
    output = {'format': 'reveal.paragraph-output/1', 'segments': [
        {'text': 'A cited statément.', 'citations': [{'target_id': 'urn:test:claim', 'citation_metadata_revision': 1}]},
        {'text': 'An uncited follow-up.'}]}
    return output, paragraph_input


class Spawns:
    """Counts interpreter and git processes started through subprocess.Popen (subprocess.run uses it too)."""
    def __enter__(self):
        self.interpreters = self.git = 0; original = subprocess.Popen; outer = self
        class Counting(original):
            def __init__(self, args, *rest, **options):
                outer.interpreters += args[0] == sys.executable; outer.git += args[0] == 'git'
                super().__init__(args, *rest, **options)
        self.patch = patch.object(subprocess, 'Popen', Counting); self.patch.start(); return self
    def __exit__(self, *exc): self.patch.stop()


class HelperEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.addClassCleanup(lambda: dapper_helper.HELPER.close())
        cls.root = Path(cls.temp.name); cls.release, cls.lock = make_release(cls.root)

    def setUp(self):
        for target in (patch.object(acceptance, 'release_root', return_value=self.release), patch.object(acceptance, 'LOCK', self.lock)):
            target.start(); self.addCleanup(target.stop)
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup); self.work = Path(work.name)

    def outcome(self, helper, document, index):
        directory = self.work/('helper' if helper else 'per-call'); directory.mkdir(exist_ok=True)
        path = directory/f'{index}.json'   # the same file name: lint reports it
        with patch.dict(os.environ, {'REVEAL_DAPPER_HELPER': '1' if helper else '0'}):
            minted = acceptance.mint(deepcopy(document), path)
            try: report = acceptance.validate_paragraph_document(path)
            except EvidenceBuildError as error: report = str(error)
        return path.read_bytes(), minted, report

    def test_mint_and_paragraph_lint_match_the_per_call_programs_exactly(self):
        docs = documents()
        expected = [self.outcome(False, document, index) for index, document in enumerate(docs)]
        self.assertTrue(any(isinstance(report, str) for _, _, report in expected))   # the variants exercise failing lint
        # One warm interpreter serves every document, in both orders: no state carries from one to the next.
        for index in [*range(len(docs)), *reversed(range(len(docs)))]:
            self.assertEqual(self.outcome(True, docs[index], index), expected[index], index)

    def test_assembly_and_reference_fields_match_the_per_call_programs(self):
        output, paragraph_input = paragraph_inputs()
        with patch.dict(os.environ, {'REVEAL_DAPPER_HELPER': '0'}):
            per_call = box_paragraph.assemble_paragraph(output, paragraph_input, dapper_root=self.release, release_lock=self.lock)
        with patch.dict(os.environ, {'REVEAL_DAPPER_HELPER': '1'}):
            helper = box_paragraph.assemble_paragraph(output, paragraph_input, dapper_root=self.release, release_lock=self.lock)
        self.assertEqual(per_call, helper); self.assertEqual(helper['text'], 'A cited statément. An uncited follow-up.')
        release = verify_release(self.release, self.lock); root = str(self.release.resolve())
        fields = {}
        for enabled in ('0', '1'):
            acceptance._reference_fields.cache_clear()
            with patch.dict(os.environ, {'REVEAL_DAPPER_HELPER': enabled}):
                fields[enabled] = acceptance._reference_fields(root, release['lock_sha256'], release['commit'])
        acceptance._reference_fields.cache_clear()
        self.assertEqual(fields['0'], fields['1']); self.assertIn('claims', fields['1'])

    def test_a_paragraph_commit_starts_one_interpreter_and_one_git_check_per_process(self):
        output, paragraph_input = paragraph_inputs()
        document = documents()[1]
        def commit(index):
            assembled = box_paragraph.assemble_paragraph(output, paragraph_input, dapper_root=self.release, release_lock=self.lock)
            path = self.work/str(index)/'commit.json'; path.parent.mkdir()
            minted = acceptance.mint(deepcopy(document), path)
            try: report = acceptance.validate_paragraph_document(path)
            except EvidenceBuildError as error: report = str(error)
            return assembled, minted, report
        dapper_helper.HELPER.close(); dapper_release._VERIFIED.clear()
        with patch.dict(os.environ, {'REVEAL_DAPPER_HELPER': '0'}), Spawns() as before:
            expected = commit(0)
        self.assertEqual(before.interpreters, 3)   # assembly, minting and linting each start a cold interpreter
        dapper_release._VERIFIED.clear()
        with Spawns() as cold: self.assertEqual(commit(1), expected)
        with Spawns() as warm: self.assertEqual(commit(2), expected)
        self.assertEqual((cold.interpreters, cold.git), (1, 5))
        self.assertEqual((warm.interpreters, warm.git), (0, 0))

    def test_prewarm_starts_the_helper_in_the_background_for_the_next_commit(self):
        dapper_helper.HELPER.close()
        with patch.dict(os.environ, {'REVEAL_DAPPER_PREWARM': '0'}):
            acceptance.prewarm()
        self.assertIsNone(dapper_helper.HELPER.process)
        with patch.dict(os.environ, {'REVEAL_DAPPER_PREWARM': '1'}):
            acceptance.prewarm()
            deadline = time.monotonic() + 120
            while dapper_helper.HELPER.warming and time.monotonic() < deadline: time.sleep(0.05)
        self.assertFalse(dapper_helper.HELPER.warming)
        self.assertEqual(dapper_helper.HELPER.served, 0)   # warming is not a served request
        with Spawns() as spawns:
            acceptance.mint(deepcopy(documents()[1]), self.work/'after-warmup.json')
        self.assertEqual(spawns.interpreters, 0)


class CallSiteTests(unittest.TestCase):
    def setUp(self):
        targets = [patch.object(acceptance, 'verify_release', return_value={'lock_sha256': 'l', 'commit': 'c'}),
                   patch.object(dapper_release, 'verify_release', return_value={'lock_sha256': 'l', 'commit': 'c'}),
                   patch.object(acceptance, 'release_root', return_value=Path('/nonexistent/release'))]
        for target in targets: target.start(); self.addCleanup(target.stop)
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup); self.path = Path(work.name)/'doc.json'

    def test_helper_failures_keep_the_per_call_messages(self):
        with patch.object(dapper_helper, 'request', return_value=(False, 'Traceback: boom')) as request, \
                patch.object(subprocess, 'run', side_effect=AssertionError('no per-call interpreter')):
            with self.assertRaisesRegex(EvidenceBuildError, '^Trusted DAPPER identity assembly failed: Traceback: boom$'):
                acceptance.mint({}, self.path)
            self.path.write_text('{}')
            with self.assertRaisesRegex(EvidenceBuildError, '^Paragraph linter runtime failed$'):
                acceptance.validate_paragraph_document(self.path)
            with self.assertRaisesRegex(ValueError, '^Pinned DAPPER paragraph assembly failed$'):
                box_paragraph.assemble_paragraph(*paragraph_inputs(), dapper_root='/nonexistent', release_lock='/nonexistent')
            acceptance._reference_fields.cache_clear()
            with self.assertRaisesRegex(EvidenceBuildError, '^Trusted reference schema could not be loaded: Traceback: boom$'):
                acceptance._reference_fields('/nonexistent', 'l', 'c')
        self.assertEqual([call.args[2] for call in request.call_args_list], ['mint', 'lint_paragraph', 'assemble', 'reference_fields'])
        self.assertTrue(all(os.path.isabs(call.args[3]['path']) for call in request.call_args_list[:2]))

    def test_paragraph_errors_still_fail_validation(self):
        findings = [{'severity': 'error', 'check': 'terminal', 'where': 'doc.json', 'message': 'missing', 'why': ''}]
        self.path.write_text('{}')
        with patch.object(dapper_helper, 'request', return_value=(True, findings)):
            with self.assertRaisesRegex(EvidenceBuildError, 'Paragraph validation failed'):
                acceptance.validate_paragraph_document(self.path)
        with patch.object(dapper_helper, 'request', return_value=(True, [dict(findings[0], severity='warning')])):
            self.assertEqual(acceptance.validate_paragraph_document(self.path)['findings'][0]['severity'], 'warning')

    def test_kill_switch_keeps_the_per_call_programs(self):
        with patch.dict(os.environ, {'REVEAL_DAPPER_HELPER': '0'}), \
                patch.object(dapper_helper, 'request', side_effect=AssertionError('helper disabled')), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '[]', '')) as run:
            self.path.write_text('{}')
            self.assertTrue(acceptance.validate_paragraph_document(self.path)['valid'])
            acceptance.mint({}, self.path)
        self.assertEqual([call.args[0][1:3] for call in run.call_args_list], [['-I', '-B'], ['-I', '-B']])

    def test_helper_profile_is_the_per_call_programs_profile(self):
        source = Path(acceptance.__file__).read_text()
        line = next(text for text in source.splitlines() if text.startswith("profiles['profiles']['reveal-paragraph']="))
        self.assertEqual(ast.literal_eval(line.split('=', 1)[1]), dapper_helper.PARAGRAPH_PROFILE)


FAKE = r'''import json,os,sys,time
out=os.fdopen(os.dup(1),'w',buffering=1); os.dup2(2,1)
print('stray library output')
out.write('{"ready":true}\n')
for line in sys.stdin:
    request=json.loads(line); op=request['op']
    if op=='sleep': time.sleep(30)
    if op=='crash': sys.stderr.write('helper exploded\n'); sys.stderr.flush(); os._exit(3)
    if op=='raise': out.write(json.dumps({'ok':False,'error':'Traceback: boom'})+'\n'); continue
    out.write(json.dumps({'ok':True,'result':{'pid':os.getpid(),'root':sys.argv[1],'size':len(line)}})+'\n')
'''
RELEASE = {'lock_sha256': 'l', 'commit': 'c'}


class HelperProcessTests(unittest.TestCase):
    def setUp(self):
        target = patch.object(dapper_helper, 'SERVER', FAKE); target.start(); self.addCleanup(target.stop)
        self.helper = dapper_helper.Helper(); self.addCleanup(self.helper.close)
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup); self.root = Path(work.name)

    def call(self, op='echo', release=RELEASE, timeout=10, **arguments):
        return self.helper.request(self.root, release, op, arguments, timeout)

    def test_one_interpreter_serves_until_the_release_changes(self):
        ok, first = self.call(); self.assertTrue(ok)
        self.assertEqual(first['root'], str(self.root.resolve()))
        self.assertEqual(self.call(payload='x' * 300_000)[1]['pid'], first['pid'])   # framing survives large requests and stray prints
        old = self.helper.process
        self.assertNotEqual(self.call(release={'lock_sha256': 'l', 'commit': 'other'})[1]['pid'], first['pid'])
        self.assertIsNotNone(old.poll())

    def test_timeout_kills_it_and_the_next_request_restarts_it(self):
        pid = self.call()[1]['pid']; old = self.helper.process
        with self.assertRaises(subprocess.TimeoutExpired): self.call('sleep', timeout=0.5)
        self.assertIsNotNone(old.poll()); self.assertIsNone(self.helper.process)
        self.assertNotEqual(self.call()[1]['pid'], pid)

    def test_crash_and_failed_requests_are_reported_and_never_reused(self):
        pid = self.call()[1]['pid']
        ok, detail = self.call('crash')
        self.assertFalse(ok); self.assertIn('helper exploded', detail); self.assertIsNone(self.helper.process)
        second = self.call()[1]['pid']; self.assertNotEqual(second, pid)
        self.assertEqual(self.call('raise'), (False, 'Traceback: boom')); self.assertIsNone(self.helper.process)
        self.assertNotEqual(self.call()[1]['pid'], second)

    def test_recycled_after_its_request_budget_or_near_its_idle_exit(self):
        with patch.object(dapper_helper, 'MAX_REQUESTS', 2):
            first = self.call()[1]['pid']; self.assertEqual(self.call()[1]['pid'], first)
            third = self.call()[1]['pid']; self.assertNotEqual(third, first)
        self.helper.used = time.monotonic() - (dapper_helper.IDLE_SECONDS - dapper_helper.IDLE_MARGIN)
        self.assertNotEqual(self.call()[1]['pid'], third)

    def test_an_exited_helper_is_replaced_without_losing_the_request(self):
        pid = self.call()[1]['pid']
        self.helper.process.kill(); self.helper.process.wait()
        self.assertNotEqual(self.call()[1]['pid'], pid)
        # Exited after the liveness check but before reading: the unsent request runs once on a new helper.
        pid = self.call()[1]['pid']; self.helper.process.kill(); self.helper.process.wait()
        with patch.object(self.helper, 'current', return_value=True):
            ok, result = self.call()
        self.assertTrue(ok); self.assertNotEqual(result['pid'], pid)

    def test_startup_failure_is_reported(self):
        with patch.object(dapper_helper, 'SERVER', 'import sys; sys.stderr.write("cannot import schema"); sys.exit(1)'):
            ok, detail = self.call()
        self.assertFalse(ok); self.assertIn('cannot import schema', detail); self.assertIsNone(self.helper.process)

    def test_a_forked_child_forgets_its_parents_helper(self):
        parent = dapper_helper.HELPER
        try:
            dapper_helper.HELPER = self.helper; self.call(); process = self.helper.process
            dapper_helper._forget()
            self.assertIsNot(dapper_helper.HELPER, self.helper); self.assertIsNone(dapper_helper.HELPER.process)
            self.assertTrue(process.stdin.closed and process.stdout.closed)
        finally:
            dapper_helper.HELPER = parent


class ReleaseVerificationCacheTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.release, self.lock = make_release(Path(work.name))
        dapper_release._VERIFIED.clear(); self.addCleanup(dapper_release._VERIFIED.clear)

    def checks(self):
        with patch.object(dapper_release, 'git', wraps=dapper_release.git) as git:
            verify_release(self.release, self.lock)
        return git.call_count

    def settled(self):
        """git status may refresh the index (a racily clean one more than once); then the cache holds."""
        for _ in range(20):
            if self.checks() == 0: return
            time.sleep(0.2)
        self.fail('Release verification never reused its git checks')

    def test_git_checks_rerun_only_when_one_of_their_inputs_changes(self):
        self.assertEqual(self.checks(), 5); self.settled()
        later = time.time() + 5
        os.utime(self.release/'schema/dapper.yaml', (later, later))
        self.assertEqual(self.checks(), 5); self.settled()
        head = self.release/'.git/HEAD'; head.write_bytes(head.read_bytes())
        self.assertEqual(self.checks(), 5); self.settled()
        self.lock.write_text(json.dumps(json.loads(self.lock.read_text()), indent=1))
        self.assertEqual(self.checks(), 5); self.settled()
        (self.release/'schema/notes.txt').write_text('untracked')   # outside the locked file set, but a local change
        with self.assertRaisesRegex(EvidenceBuildError, 'local changes'): verify_release(self.release, self.lock)

    def test_file_contents_are_rehashed_even_when_git_inputs_look_unchanged(self):
        with patch.object(dapper_release, 'provenance_key', return_value=('unchanged',)):
            self.assertEqual(self.checks(), 5); self.assertEqual(self.checks(), 0)
            (self.release/'schema/dapper.yaml').write_text('changed')
            with patch.object(dapper_release, 'git', side_effect=AssertionError('cached')):
                with self.assertRaisesRegex(EvidenceBuildError, 'checksum mismatch: schema/dapper.yaml'):
                    verify_release(self.release, self.lock)

    def test_a_checkout_without_a_git_directory_is_never_cached(self):
        lock = json.loads(self.lock.read_text())
        self.assertIsNotNone(dapper_release.provenance_key(self.release.resolve(), self.lock.read_bytes(), lock))
        shutil.rmtree(self.release/'.git')
        self.assertIsNone(dapper_release.provenance_key(self.release.resolve(), self.lock.read_bytes(), lock))
        with self.assertRaisesRegex(EvidenceBuildError, 'Git operation failed|differs'): verify_release(self.release, self.lock)


if __name__ == '__main__': unittest.main()
