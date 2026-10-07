"""Async handlers never run repository transactions, catalog loads or other blocking work on the event loop.

There is one uvicorn process: a blocking statement on the loop stalls every concurrent request, health probe,
SSE heartbeat and Workflow callback in it. Async handlers await the request body on the loop and run the rest
in the threadpool, one whole transaction per thread call.
"""
import ast
import asyncio
import base64
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch

from reveal_backend.repository import Repository
import test_application as application

SOURCE = Path(__file__).resolve().parents[1] / 'src/reveal_backend'
BLOCKING = {'transaction', 'read_transaction', 'single_read', 'discard_notifications', 'preload_catalog'}
# Sites that are not on the API event loop, or that a later change owns. Each needs a reason.
ALLOWED = {
    ('app.py', 'render_citations'): 'citation rendering is split into read, render and write by its own change (F35)',
    ('deployment.py', '_run_probe'): 'standalone legacy worker process, not the API loop',
    ('worker.py', '_process'): 'standalone legacy worker process, not the API loop',
}


def blocking_calls(function):
    """Calls written directly in an async def body; nested defs and lambdas run elsewhere (the threadpool)."""
    found = []
    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)): continue
            if isinstance(child, ast.Call):
                target = child.func
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, 'id', None)
                catalog_load = (name == 'load' and isinstance(target, ast.Attribute)
                                and getattr(target.value, 'id', None) == 'catalog')
                if name in BLOCKING or catalog_load: found.append((child.lineno, ast.unparse(child)))
            walk(child)
    walk(function)
    return found


class StaticOffloadTests(unittest.TestCase):
    def test_async_functions_never_call_repository_transactions_directly(self):
        violations, used = [], set()
        for path in sorted(SOURCE.glob('*.py')):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.AsyncFunctionDef): continue
                calls = blocking_calls(node)
                if not calls: continue
                if (path.name, node.name) in ALLOWED: used.add((path.name, node.name)); continue
                violations += [f'{path.name}:{line} async {node.name}: {text}' for line, text in calls]
        self.assertEqual(violations, [], 'run these in the threadpool (run_in_threadpool / asyncio.to_thread)')
        self.assertEqual(used, set(ALLOWED), 'remove allowlist entries that no longer apply')

    def test_detector_sees_direct_calls_but_not_nested_thread_bodies(self):
        tree = ast.parse('async def handler(request):\n'
                         '    body = await request.json()\n'
                         '    def run():\n'
                         '        with repo.transaction() as tx: pass\n'
                         '    await run_in_threadpool(run)\n'
                         '    with repo.read_transaction() as tx: catalog.load()\n')
        calls = blocking_calls(tree.body[0])
        self.assertEqual([text for _, text in calls], ['repo.read_transaction()', 'catalog.load()'])


class RuntimeOffloadTests(unittest.TestCase):
    """Every lease a migrated route opens is opened on a thread with no running event loop."""
    setUp = application.ApplicationTests.setUp
    provision = application.ApplicationTests.provision
    token = application.ApplicationTests.token
    headers = application.ApplicationTests.headers
    draft = application.ApplicationTests.draft

    @contextmanager
    def loop_guard(self):
        on_loop = []
        def guarded(name):
            original = getattr(Repository, name)
            @contextmanager
            def lease(repo, *args, **kwargs):
                try: asyncio.get_running_loop(); on_loop.append(name)
                except RuntimeError: pass
                with original(repo, *args, **kwargs) as tx: yield tx
            return patch.object(Repository, name, lease)
        patches = [guarded(name) for name in ('transaction', 'read_transaction', 'single_read')]
        for item in patches: item.start()
        try: yield on_loop
        finally:
            for item in reversed(patches): item.stop()

    def test_migrated_routes_open_leases_off_the_event_loop(self):
        from reveal_backend.evidence_package import sha256
        data = b'First observation\nSecond observation'
        with patch.dict('os.environ', {'REVEAL_ARTIFACT_STORE': 'filesystem', 'REVEAL_ARTIFACTS_DIR': self.temp.name}), \
                self.loop_guard() as on_loop:
            user = self.provision()
            draft = self.draft(user)
            patched = self.client.patch('/v1/drafts/' + draft['id'], json={'expected_version': 1, 'name': 'Named'}, headers=self.headers(user))
            self.assertEqual(patched.status_code, 200, patched.text)
            ticket = self.client.post('/v1/uploads', headers=self.headers(user), json={'draft_id': draft['id'], 'filename': 'notes.txt',
                'media_type': 'text/plain', 'size_bytes': len(data), 'sha256': sha256(data)})
            self.assertEqual(ticket.status_code, 201, ticket.text); identity = ticket.json()['upload']['id']
            staged = self.client.post(ticket.json()['transfer']['url'], headers=self.headers(user),
                                      json={'content_base64': base64.b64encode(data).decode()})
            self.assertEqual(staged.status_code, 200, staged.text)
            completed = self.client.post('/v1/uploads/' + identity + '/complete', json={}, headers=self.headers(user))
            self.assertEqual(completed.status_code, 200, completed.text)
            download = self.client.get('/v1/uploads/' + identity + '/download', headers=self.headers(user))
            self.assertEqual(download.content, data)
            resolved = self.client.post('/internal/v1/principals/resolve', json={'issuer': 'https://issuer.example', 'subject': 'subject-1',
                                        'display_name': None, 'email': None, 'email_verified': False, 'orcid': None, 'orcid_authenticated': False},
                                        headers={'Authorization': 'Bearer ' + 't' * 40})
            self.assertEqual(resolved.status_code, 200, resolved.text)
            deleted = self.client.request('DELETE', '/v1/drafts/' + draft['id'], json={'expected_version': 2}, headers=self.headers(user))
            self.assertEqual(deleted.status_code, 200, deleted.text)
            for route in ('/v1/me', '/v1/drafts', '/v1/jobs', '/v1/research-requests', '/v1/me/explorations'):
                self.assertEqual(self.client.get(route, headers=self.headers(user)).status_code, 200, route)
        self.assertEqual(on_loop, [])

    def test_paragraph_acceptance_persists_from_a_worker_thread(self):
        from reveal_backend import jobs, worker
        from reveal_backend.evidence_package import canonical_json
        from reveal_backend.repository import digest
        owner, account_id = self.provision(), 'dapper:ScientificAccount.' + 'a' * 32
        with patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT': 'database'}), self.repo.transaction() as tx:
            tx.put('account', digest([owner, account_id]), owner, {'result': {'citation_metadata': [], 'artifacts': [],
                'research_statement': {'status': 'queued'}}, 'summary': {'research_statement': {'status': 'queued'}}})
            job = jobs.enqueue(tx, owner, 'paragraph', account_id=account_id, inputs={'kind': 'paragraph', 'account_id': account_id})
        with patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT': 'database'}): job, queue = jobs.claim(self.repo, 'offload', job_id=job['id'])
        document = {'scientific_accounts': [{'id': account_id, 'was_attributed_to': []}]}
        def mint(value, path): path.write_bytes(canonical_json(value)); return value
        stubs = [patch('reveal_backend.box_paragraph.assemble_paragraph', return_value={'text': 'Statement.', 'citations': []}),
                 patch.object(worker, 'mint', side_effect=mint), patch.object(worker, 'validate_paragraph_document', return_value={'valid': True}),
                 patch.object(worker, 'object_envelope', return_value={'root_id': 'paragraph'}),
                 patch.object(worker.Worker, 'save_workspace', lambda *args: None)]
        for item in stubs: item.start(); self.addCleanup(item.stop)
        directory = Path(self.temp.name) / 'paragraph'; directory.mkdir()
        with self.loop_guard() as on_loop:
            asyncio.run(worker.Worker(self.repo).accept_paragraph(job, queue['token'], {}, {'account_document': document}, directory))
        self.assertEqual(on_loop, [])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'succeeded')
            stored = tx.get('account', digest([owner, account_id]))['data']
        self.assertEqual(stored['result']['research_statement']['status'], 'succeeded')

    def test_guard_detects_a_lease_opened_on_the_loop(self):
        async def on_loop():
            with self.repo.read_transaction(): pass
        with self.loop_guard() as seen: asyncio.run(on_loop())
        self.assertEqual(seen, ['read_transaction'])


if __name__ == '__main__': unittest.main()
