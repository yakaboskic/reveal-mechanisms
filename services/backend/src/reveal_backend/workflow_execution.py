"""Bounded research execution operations for Workflow v1.

Each invocation restores immutable storage into unique scratch, performs one
phase, saves its checkpoint, and returns. Waiting on Box holds no local task.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time

from . import jobs, workflow_state as state
from .agent_execution import ExecutionRequest
from .artifact_store import store as artifact_store, StorageUnavailable
from .box_adapter import CAPTURE_MARKER, atomic_capture_marker, read_capture_marker, captured_result, BoxTransportError
from .box_lifecycle import BoxLifecycle
from .evidence_package import canonical_json, decode, require, sha256
from .repository import Repository, now, digest
from .runtime_config import ROOT, setting
from .worker import (Worker, collect, read_preparation_inputs, restore_dispatch_input, fit_input_budget,
    public_activity, validate_execution_ledger, assert_artifact, assemble_account)


class WorkflowExecution:
    def __init__(self, repository=None, *, storage=None, adapter=None):
        self.repository = repository or Repository()
        self.storage = storage
        self.adapter = adapter

    def store(self):
        if self.storage is None: self.storage = artifact_store()
        return self.storage

    def context(self, payload):
        with self.repository.read_transaction() as tx:
            execution = tx.get('execution', payload['job_id'])['data']; state.check(execution, payload)
            row = tx.get('job', payload['job_id']); job = row['data']; job['owner_user_id'] = row['owner']
            queue = tx.get('queue', payload['job_id'])['data']
            return job, queue, execution

    def checkpoint(self, payload, token, root, **updates):
        evidence_package = updates.pop('evidence_package', None)
        reference = self.store().snapshot(root)
        with self.repository.transaction() as tx:
            owner, execution = state.owned(tx, payload, token)
            execution.update(updates, workspace=reference, updated_at=now())
            tx.put('execution', payload['job_id'], owner, execution)
            if evidence_package is not None:
                tx.put('evidence', payload['job_id'], owner, {'job_id': payload['job_id'],
                    'package_sha256': updates['dispatch_input']['sha256'], 'package': evidence_package})
            queue = tx.get('queue', payload['job_id'])['data']; queue['workspace'] = reference
            if updates.get('dispatch_input'): queue['dispatch_input'] = updates['dispatch_input']
            if updates.get('box'): queue['remote_handle'] = updates['box']
            if updates.get('review_capture'): queue['review_capture'] = updates['review_capture']
            tx.put('queue', payload['job_id'], owner, queue)
        return execution

    def activity(self, payload, token, kind, detail):
        with self.repository.transaction() as tx:
            owner, execution = state.owned(tx, payload, token)
            delivery = digest([execution['step'], kind, detail])
            if tx.get('workflow_activity', delivery): return
            tx.put('workflow_activity', delivery, owner, {'job_id': payload['job_id']})
            current = tx.get('job', payload['job_id'])['data']; current['owner_user_id'] = owner
            mapped = public_activity(current, kind, detail)
            if mapped: jobs.event(tx, current, *mapped)

    def observe_commit(self, payload, token, handle, events):
        """Acknowledge remote cursor and deduplicated public events atomically."""
        with self.repository.transaction() as tx:
            owner, execution = state.owned(tx, payload, token)
            current = tx.get('job', payload['job_id'])['data']; current['owner_user_id'] = owner
            for kind, detail in events:
                delivery = digest([payload['job_id'], detail['remote_stream_id'], detail['remote_sequence']])
                if tx.get('remote_event', delivery): continue
                tx.put('remote_event', delivery, owner, {'job_id': payload['job_id'], 'sequence': detail['remote_sequence']})
                mapped = public_activity(current, kind, detail)
                if mapped: jobs.event(tx, current, *mapped)
            execution['box'] = handle
            tx.put('execution', payload['job_id'], owner, execution)
            queue = tx.get('queue', payload['job_id'])['data']; queue['remote_handle'] = handle
            tx.put('queue', payload['job_id'], owner, queue)

    async def step(self, payload, index):
        job, execution, replay = await asyncio.to_thread(state.acquire, self.repository, payload, index)
        if replay is not None: return replay
        token = execution['fence']
        try:
            if execution.get('deferred'):
                return await asyncio.to_thread(state.complete, self.repository, payload, token, next_phase=execution['phase'], sleep=10)
            # Even an acknowledgment lost after outcome commit must replay the
            # authoritative outcome rather than attempting scientific work twice.
            if job['status'] in jobs.TERMINAL and not execution.get('capacity_reserved'):
                return await asyncio.to_thread(state.complete, self.repository, payload, token, next_phase='complete', done=True)
            with tempfile.TemporaryDirectory(prefix='reveal-step-') as temporary:
                root = Path(temporary)
                if execution.get('workspace'):
                    await asyncio.to_thread(self.store().restore, execution['workspace'], root)
                async with asyncio.timeout(int(setting('REVEAL_WORKFLOW_STEP_TIMEOUT_SECONDS', '360'))):
                    result = await self.operate(payload, token, job, execution, root)
                return await asyncio.to_thread(state.complete, self.repository, payload, token, **result)
        except state.StepBusy:
            return await asyncio.to_thread(state.complete, self.repository, payload, token,
                                           next_phase=execution['phase'], sleep=10)
        except state.RecoveryRequired:
            await asyncio.to_thread(state.release, self.repository, payload, token, recovery=True, reason='External creation requires reconciliation')
            raise
        except state.StaleExecution:
            raise
        except (BoxTransportError, StorageUnavailable, TimeoutError, OSError):
            await asyncio.to_thread(state.release, self.repository, payload, token, reason='Transient external operation; retry the same phase')
            raise
        except Exception as exc:
            from .scientific_grounding import ScientificReviewUnavailable
            from .job_failures import review_failure
            failure = review_failure(exc) if isinstance(exc, ScientificReviewUnavailable) else {
                'code': 'EVIDENCE_PREPARATION_FAILED' if execution['phase'] == 'prepare' else 'VALIDATION_FAILED',
                'message': 'The workflow phase could not be completed. Saved output and source captures are preserved.', 'retryable': True}
            # Never terminalize a paid execution whose remote cleanup is unresolved.
            _, _, latest = await asyncio.to_thread(self.context, payload)
            if latest.get('capacity_reserved'):
                await asyncio.to_thread(state.release, self.repository, payload, token, recovery=True, reason=type(exc).__name__)
                raise
            await asyncio.to_thread(jobs.finish, self.repository, payload['job_id'], token, 'failed', failure=failure)
            return await asyncio.to_thread(state.complete, self.repository, payload, token, next_phase='complete', done=True)

    async def operate(self, payload, token, job, execution, root):
        job, queue, execution = await asyncio.to_thread(self.context, payload)
        phase = execution['phase']; box = execution.get('box')
        if job['status'] in ('cancel_requested', 'cancelled'):
            if not box and not execution.get('creation_intent'):
                if job['status'] != 'cancelled': await asyncio.to_thread(jobs.finish, self.repository, job['id'], token, 'cancelled')
                return {'next_phase': 'complete', 'done': True}
            if box and box['phase'] in ('created', 'prepared') and not execution.get('launch_intent'):
                return {'next_phase': 'cleanup', 'abandoned': True}
            if box and box['phase'] not in ('captured', 'deleted', 'terminal'):
                await self.box_adapter(queue).cancel_once(box)
                if phase not in ('observe', 'capture', 'cleanup'): return {'next_phase': 'observe'}
            if box and box['phase'] == 'deleted':
                await asyncio.to_thread(jobs.finish, self.repository, job['id'], token, 'cancelled')
                return {'next_phase': 'complete', 'done': True}
        if job['kind'] == 'deployment_probe': return await self.probe(payload, token, job, queue, execution, root)
        if phase == 'prepare': return await self.prepare(payload, token, job, queue, root)
        request, inputs = self.request(job, queue, execution, root)
        adapter = self.box_adapter(queue)
        if phase == 'create':
            if box: return {'next_phase': 'bootstrap'}
            if execution.get('creation_intent') and not box:
                raise state.RecoveryRequired('A previous Box creation may have succeeded; never create a duplicate')
            await asyncio.to_thread(self.activity, payload, token, 'stage', {'stage': 'starting_agent', 'message': 'Allocating the isolated research runtime.'})
            await asyncio.to_thread(state.reserve_box, self.repository, payload, token)
            handle = await adapter.create_once(request)
            await asyncio.to_thread(self.checkpoint, payload, token, root, box=handle)
            return {'next_phase': 'bootstrap'}
        if phase == 'bootstrap':
            if box['phase'] == 'prepared': return {'next_phase': 'launch'}
            handle = await adapter.prepare_once(request, box)
            await asyncio.to_thread(self.checkpoint, payload, token, root, box=handle)
            return {'next_phase': 'launch'}
        if phase == 'launch':
            if box['phase'] == 'running': return {'next_phase': 'observe', 'sleep': 5}
            await asyncio.to_thread(state.save, self.repository, payload, token, launch_intent=True,
                                    deadline=time.time() + request.timeout_seconds + 420)
            handle = await adapter.launch_once(box)
            await asyncio.to_thread(self.checkpoint, payload, token, root, box=handle)
            return {'next_phase': 'observe', 'sleep': 5}
        if phase == 'observe':
            if time.time() > execution.get('deadline', float('inf')): await adapter.cancel_once(box)
            handle, events, terminal = await adapter.inspect_once(box)
            await asyncio.to_thread(self.observe_commit, payload, token, handle, events)
            return {'next_phase': 'capture' if terminal else 'observe', 'sleep': 0 if terminal else 5}
        if phase == 'capture':
            if execution.get('capture_complete'): return {'next_phase': 'cleanup'}
            await asyncio.to_thread(self.activity, payload, token, 'stage', {'stage': 'collecting_output', 'message': 'Saving completed output and captured evidence to durable storage.'})
            handle = await adapter.capture_once(request, box)
            await asyncio.to_thread(self.checkpoint, payload, token, root, box=handle, capture_complete=True)
            return {'next_phase': 'cleanup'}
        if phase == 'cleanup':
            require(execution.get('capture_complete') or execution.get('abandoned'), 'Cannot delete the sole complete output')
            handle = await adapter.delete_once(box)
            updates = {'box': handle, 'cleanup_complete': True, 'capacity_reserved': False}
            if execution.get('capture_complete'):
                marker = read_capture_marker(request, box); marker['cleanup_complete'] = True; marker['timings'] = handle['timings']
                atomic_capture_marker(request.output_dir / CAPTURE_MARKER, marker)
                if marker['state']['status'] == 'succeeded':
                    updates['review_capture'] = {'attempt': request.attempt, 'box_id': box['box_id'],
                        'capture_sha256': sha256((request.output_dir / CAPTURE_MARKER).read_bytes())}
            await asyncio.to_thread(self.checkpoint, payload, token, root, **updates)
            if execution.get('abandoned'):
                await asyncio.to_thread(jobs.finish, self.repository, job['id'], token, 'cancelled')
                return {'next_phase': 'complete', 'done': True}
            return {'next_phase': 'validate'}
        if phase == 'validate': return await self.validate(payload, token, job, queue, execution, root, request, inputs)
        if phase in ('review_init', 'review_call', 'review_tools'):
            return await self.review(payload, token, job, queue, execution, root, request, inputs)
        if phase == 'commit': return await self.commit(payload, token, job, queue, execution, root, request, inputs)
        raise ValueError('Unknown workflow phase')

    def box_adapter(self, queue):
        return self.adapter or BoxLifecycle(ROOT, environ={**os.environ, 'REVEAL_CLAUDE_MODEL': queue.get('dispatch_input', {}).get('model', 'claude-sonnet-4-6')})

    def request(self, job, queue, execution, root):
        path, inputs = restore_dispatch_input(root, queue['dispatch_input'])
        selected = tuple(inputs['external_evidence']['selected_graphs']) if job['kind'] == 'analysis' else ()
        attempt = execution['authoring_attempt']
        request = ExecutionRequest(job_id=job['id'], attempt=attempt, kind='research' if job['kind'] == 'analysis' else 'paragraph',
            input_path=path, output_dir=root/f'attempt-{attempt}'/'output', selected_graphs=selected,
            timeout_seconds=int(setting('REVEAL_AGENT_TIMEOUT_SECONDS', '900')),
            max_budget_usd=float(setting('REVEAL_AGENT_MAX_BUDGET_USD', '3')),
            max_turns=int(setting('REVEAL_AGENT_MAX_TURNS', '100')), remote_handle=execution.get('box'))
        return request, inputs

    async def prepare(self, payload, token, job, queue, root):
        if queue.get('dispatch_input'):
            restore_dispatch_input(root, queue['dispatch_input'])
            return {'next_phase': 'create'}
        mode = setting('REVEAL_EXECUTION_MODE', 'box')
        require(mode == 'box', 'Workflow scientific execution requires Box; deployment probes do not use a model')
        await asyncio.to_thread(self.activity, payload, token, 'stage', {'stage': 'preparing_evidence', 'message': 'Preparing frozen evidence for durable execution.'})
        source = await asyncio.to_thread(read_preparation_inputs, self.repository, job)
        if job['kind'] == 'analysis':
            frozen, binding = source
            path, package = await asyncio.to_thread(collect, job, frozen, binding, queue['inputs'].get('budgets', {}), root/'evidence')
            path, package, measurement = await asyncio.to_thread(fit_input_budget, path, mode, queue['inputs'].get('budgets', {}).get('evidence_tokens', 24000))
            (root/'token-budget.json').write_bytes(canonical_json(measurement))
        else:
            metadata = source['result']['citation_metadata']
            allowed = [{'target_id': m['target_id'], 'citation_metadata_revision': m['metadata_revision']} for m in metadata]
            allowed.sort(key=lambda item: 0 if item['target_id'].startswith('dapper:Claim.') else 1)
            package = {'format': 'reveal.paragraph-input/1', 'account_document': source['result']['document'], 'account_id': job['input_account_id'], 'allowed_citations': allowed}
            path = root/'paragraph-input.json'; path.write_bytes(canonical_json(package))
        snapshot = {'path': str(path.relative_to(root)), 'sha256': sha256(path.read_bytes()), 'mode': mode,
                    'model': setting('REVEAL_CLAUDE_MODEL', 'claude-sonnet-4-6'), 'kind': job['kind']}
        await asyncio.to_thread(self.checkpoint, payload, token, root, dispatch_input=snapshot, evidence_package=package if job['kind'] == 'analysis' else None)
        with self.repository.transaction() as tx:
            owner, _ = state.owned(tx, payload, token)
            current = tx.get('job', job['id'])['data']; current['owner_user_id'] = owner
            if current['status'] != 'cancel_requested': current['status'] = 'running'
            jobs.event(tx, current, 'status', 'Frozen evidence saved. Preparing isolated research execution.')
        return {'next_phase': 'create'}

    async def validate(self, payload, token, job, queue, execution, root, request, inputs):
        if execution.get('validated_paths'): return {'next_phase': 'review_init'}
        require(execution['cleanup_complete'] and execution.get('capture_complete', bool(queue.get('review_source'))), 'Validation requires durable captured output and acknowledged cleanup')
        marker = read_capture_marker(request, execution['box'])
        require(marker and marker['cleanup_complete'], 'Capture cleanup checkpoint is missing')
        result = captured_result(request, execution['box'], marker)
        await asyncio.to_thread(self.activity, payload, token, 'stage', {'stage': 'validating', 'message': 'Checking source fidelity, identities and captured provenance.'})
        if result.status not in ('succeeded', 'insufficient_evidence'):
            from .job_failures import authoring_failure
            await asyncio.to_thread(jobs.finish, self.repository, job['id'], token, result.status,
                                   failure=authoring_failure(result, request) if result.status == 'failed' else None)
            return {'next_phase': 'complete', 'done': True}
        await asyncio.to_thread(validate_execution_ledger, result, request, queue['dispatch_input']['model'])
        if result.status == 'insufficient_evidence': return {'next_phase': 'commit', 'outcome': True}
        if job['kind'] == 'analysis':
            frozen, _ = await asyncio.to_thread(read_preparation_inputs, self.repository, job)
            require(0 < len(result.account_paths) <= queue['inputs'].get('budgets', {}).get('max_accounts', 3), 'Invalid account count')
            directory = root/'validated'; directory.mkdir(exist_ok=True)
            paths = []
            for index, path in enumerate(result.account_paths):
                target = directory/f'account-{index}.json'
                document, report = await asyncio.to_thread(assemble_account, assert_artifact(path, request.output_dir), request.input_path,
                    target, frozen['attribution'], job, request.attempt, 'box', result.ledger_manifest_path)
                (directory/f'report-{index}.json').write_bytes(canonical_json(report)); paths.append(str(target.relative_to(root)))
        else:
            from .box_paragraph import validate_paragraph_segments
            require(result.paragraph_path is not None, 'Missing paragraph')
            validate_paragraph_segments(decode(result.paragraph_path.read_bytes()), inputs)
            paths = [str(result.paragraph_path.relative_to(root))]
        await asyncio.to_thread(self.checkpoint, payload, token, root, validated_paths=paths, review_index=0, review_checkpoint=None)
        return {'next_phase': 'review_init'}

    async def review(self, payload, token, job, queue, execution, root, request, inputs):
        from . import durable_review
        index = execution['review_index']; path = root/'review'/f'{execution["review_attempt"]}-{index}.json'
        path.parent.mkdir(exist_ok=True)
        def checkpoint(value):
            path.write_bytes(canonical_json(value))
            self.checkpoint(payload, token, root, review_checkpoint=str(path.relative_to(root)))
        if execution['phase'] == 'review_init':
            document = decode((root/execution['validated_paths'][index]).read_bytes())
            marker = read_capture_marker(request, execution['box']); result = captured_result(request, execution['box'], marker)
            review = await asyncio.to_thread(durable_review.initial, job['kind'], document, inputs,
                ledger_path=result.ledger_manifest_path, budget=float(setting('REVEAL_GROUNDING_MAX_BUDGET_USD', '0.30')))
            await asyncio.to_thread(checkpoint, review)
            return {'next_phase': 'review_call'}
        review = decode((root/execution['review_checkpoint']).read_bytes())
        if execution['phase'] == 'review_call':
            await asyncio.to_thread(durable_review.call_one, review, setting('ANTHROPIC_API_KEY'), checkpoint)
            return {'next_phase': 'review_tools'}
        review = await asyncio.to_thread(durable_review.process_response, review)
        await asyncio.to_thread(checkpoint, review)
        if not review['complete']: return {'next_phase': 'review_call'}
        require(review['result']['accepted'], 'Independent source-grounding review rejected scientific content')
        if index + 1 < len(execution['validated_paths']):
            return {'next_phase': 'review_init', 'review_index': index + 1, 'review_checkpoint': None}
        return {'next_phase': 'commit'}

    async def commit(self, payload, token, job, queue, execution, root, request, inputs):
        require(execution['cleanup_complete'], 'Final acceptance requires acknowledged cleanup')
        engine = self
        class AcceptanceWorker(Worker):
            def save_workspace(self, _job, _token, _root):
                engine.checkpoint(payload, token, root)
        worker = AcceptanceWorker(self.repository)
        if not await worker.begin_persistence(job, token, 'Saving the independently validated research outcome.'):
            return {'next_phase': 'complete', 'done': True}
        directory = root/'validated'; directory.mkdir(exist_ok=True)
        marker = read_capture_marker(request, execution['box']); result = captured_result(request, execution['box'], marker)
        if job['kind'] == 'analysis':
            frozen, binding = await asyncio.to_thread(read_preparation_inputs, self.repository, job)
            if execution.get('outcome'):
                from .analysis_outcomes import prepare
                prepared = await asyncio.to_thread(prepare, job, frozen, binding, request.input_path, result,
                    attempt=request.attempt, mode='box', expected_model=queue['dispatch_input']['model'])
                await asyncio.to_thread(worker.accept_outcome, job, token, prepared)
            else:
                accepted = [(decode((root/path).read_bytes()), decode((directory/f'report-{index}.json').read_bytes()), root/path)
                            for index, path in enumerate(execution['validated_paths'])]
                await worker.accept_accounts(job, token, accepted, frozen, request.input_path, result, directory, 'box')
        else:
            await worker.accept_paragraph(job, token, decode((root/execution['validated_paths'][0]).read_bytes()), inputs, directory)
        current, _, _ = await asyncio.to_thread(self.context, payload)
        if current['status'] == 'cancel_requested': await asyncio.to_thread(jobs.finish, self.repository, job['id'], token, 'cancelled')
        return {'next_phase': 'complete', 'done': True}

    async def probe(self, payload, token, job, queue, execution, root):
        """No paid services: prove replacement during a durable wait and restore."""
        require(setting('REVEAL_ENVIRONMENT', 'development') in ('development', 'test'), 'Probes are restricted to development/test')
        if execution['phase'] == 'prepare':
            (root/'probe.json').write_bytes(canonical_json({'job_id': job['id'], 'nonce': queue['inputs']['nonce']}))
            await asyncio.to_thread(self.checkpoint, payload, token, root)
            return {'next_phase': 'probe_finish', 'sleep': min(120, max(1, int(queue['inputs'].get('delay', 3))))}
        value = decode((root/'probe.json').read_bytes())
        require(value == {'job_id': job['id'], 'nonce': queue['inputs']['nonce']}, 'Probe checkpoint changed after wait')
        await asyncio.to_thread(jobs.finish, self.repository, job['id'], token, 'succeeded', result={
            'kind': 'deployment_probe', 'checkpoint_sha256': sha256((root/'probe.json').read_bytes()), 'restored': True})
        return {'next_phase': 'complete', 'done': True}

    async def cancel(self, payload):
        job, _, execution = await asyncio.to_thread(self.context, payload)
        if job['status'] not in ('cancel_requested', 'cancelled'): return {'cancelled': False}
        box = execution.get('box')
        if box and box['phase'] in ('running', 'prepared', 'created'):
            # A pre-launch cancellation marker is observed by the trusted runner
            # if launch races this delivery; the normal workflow secures capture.
            if execution.get('launch_intent'): await self.box_adapter({}).cancel_once(box)
        return {'cancelled': True, 'cleanup_complete': execution.get('cleanup_complete', False)}
