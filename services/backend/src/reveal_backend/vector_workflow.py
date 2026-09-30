"""Signed, durable Vector ingestion; no scheduler state or reads in Redis."""
from __future__ import annotations

import asyncio
import base64
import json
import os

import numpy as np

from . import jobs
from .repository import Repository, digest, now
from .vector_ingestion import VectorRegistry, environment, import_batch
from .vector_retrieval import UpstashFactorIndex, VectorUnavailable, client_from_environment, value, metadata, vector_checksum

PATH = '/internal/workflows/vector-import-v1'


def callback_url():
    from .workflow_routes import config, PATH as research_path
    return config().removesuffix(research_path) + PATH


def enroll(registry, identity, *, activate=False, expected_previous=None):
    """Durably request delivery; the managed reconciler repairs lost triggers."""
    with registry.repo.transaction() as tx:
        row = tx.get('vector_snapshot', identity)
        if not row or row['data']['environment'] != environment(): raise ValueError('Unknown vector snapshot')
        if not row['data'].get('quality_probes'): raise ValueError('Export exact quality probes before Workflow ingestion')
        existing = tx.get('vector_dispatch', identity)
        request = tx.get('vector_import_request', identity)
        intent = {'snapshot_id': identity, 'environment': environment(), 'namespace': jobs.namespace(), 'activate': bool(activate),
                  'expected_previous': expected_previous, 'created_at': now()}
        for saved in (existing, request):
            if not saved: continue
            for key in ('snapshot_id', 'environment', 'activate', 'expected_previous'):
                if saved['data'][key] != intent[key]: raise ValueError('Vector dispatch identity conflicts with existing intent')
            if saved['data'].get('namespace', jobs.namespace()) != jobs.namespace():
                raise ValueError('Vector dispatch namespace conflicts with existing intent')
            intent['created_at'] = saved['data']['created_at']
        # An explicit operator reenrollment upgrades pre-namespace intents only
        # after checking the snapshot and every persisted request field.
        tx.put('vector_dispatch', identity, 'catalog', intent)
        tx.put('vector_import_request', identity, 'catalog', intent)


async def dispatch_pending(repository, *, limit=10):
    from .workflow_routes import client, pending
    from upstash_workflow.workflow_requests import _get_first_invocation_batch_body
    delivered, failed = 0, 0
    for identity, intent in await asyncio.to_thread(pending, repository, 'vector_dispatch', min(limit, 25)):
        if intent['environment'] != environment() or intent.get('namespace') != jobs.namespace(): continue
        payload = {'snapshot_id': identity, 'environment': intent['environment'], 'namespace': intent['namespace']}
        dispatch_id = digest(['vector-import-v1', intent['namespace'], identity])
        batch = _get_first_invocation_batch_body('vector-' + dispatch_id[:40], callback_url(), {}, payload,
            retries=5, workflow_failure_url=callback_url())
        batch[0]['headers']['Upstash-Deduplication-Id'] = dispatch_id
        batch[0]['headers']['Upstash-Timeout'] = os.getenv('REVEAL_WORKFLOW_DELIVERY_TIMEOUT', '420s')
        try:
            async with asyncio.timeout(15):
                await client().http.request(path='/v2/batch', method='POST', headers={'Content-Type': 'application/json'}, body=json.dumps(batch))
            def acknowledge():
                with repository.transaction() as tx:
                    tx.remove('vector_dispatch', identity)
            await asyncio.to_thread(acknowledge)
            delivered += 1
        except Exception:
            failed += 1
    return {'delivered': delivered, 'failed': failed}


def plan(registry, payload):
    if payload.get('environment') != environment(): raise ValueError('Vector workflow environment mismatch')
    if payload.get('namespace') != jobs.namespace(): raise ValueError('Vector workflow namespace mismatch')
    snapshot = registry.get(payload['snapshot_id'])
    with registry.repo.read_transaction() as tx:
        request = tx.get('vector_import_request', payload['snapshot_id'])
    if not request or request['data']['environment'] != environment() or request['data'].get('namespace') != jobs.namespace():
        raise ValueError('No authorized vector import intent')
    return {'batches': list(snapshot['batches']), 'probes': len(snapshot['quality_probes']),
            'activate': request['data']['activate'], 'expected_previous': request['data']['expected_previous']}


def inventory_page(registry, identity, kind, cursor='', *, client=None):
    """One bounded inventory page, checkpointed independently of the manifest."""
    if kind not in ('factors', 'contexts'): raise ValueError('Invalid Vector inventory kind')
    snapshot = registry.get(identity)
    key = digest([identity, kind, cursor])
    with registry.repo.read_transaction() as tx:
        saved = tx.get('vector_inventory', key)
    if saved: return saved['data']['result']
    rows = {row['id']: row for row in snapshot[kind]}
    client = client or client_from_environment(write=True)
    namespace = snapshot['factor_namespace' if kind == 'factors' else 'context_namespace']
    page = client.range(cursor=cursor, limit=200, include_metadata=True, namespace=namespace)
    ids = []
    for row in value(page, 'vectors', []):
        record = rows.get(value(row, 'id'))
        if record is None or value(row, 'metadata') != metadata(snapshot, record): raise VectorUnavailable('Inventory source binding mismatch')
        ids.append(record['id'])
    if len(set(ids)) != len(ids): raise VectorUnavailable('Duplicate vector inventory entry')
    next_cursor = value(page, 'next_cursor', '')
    result = {'cursor': next_cursor, 'done': not next_cursor or next_cursor == '0', 'count': len(ids)}
    if not result['done'] and (next_cursor == cursor or not ids): raise VectorUnavailable('Vector inventory cursor did not advance')
    with registry.repo.transaction() as tx:
        old = tx.get('vector_inventory', key)
        data = {'snapshot_id': identity, 'kind': kind, 'input_cursor': cursor, 'ids': ids, 'result': result}
        if old and old['data'] != data: raise VectorUnavailable('Vector inventory changed during verification')
        tx.put('vector_inventory', key, 'catalog', data)
    return result


def query_probe(registry, identity, probe_index, *, client=None):
    """One ANN query against a frozen exact baseline, with numeric validation."""
    snapshot = registry.get(identity)
    key = digest([identity, probe_index])
    with registry.repo.read_transaction() as tx: saved = tx.get('vector_quality', key)
    if saved: return saved['data']
    candidate = dict(snapshot, status='complete')
    candidate['factors'] = [dict(row, roundtrip_sha256=snapshot['verified_batches'][row['batch']][row['id']]) for row in snapshot['factors']]
    index = UpstashFactorIndex(candidate, client=client or client_from_environment(write=True))
    probe = snapshot['quality_probes'][probe_index]
    query = np.frombuffer(base64.b64decode(probe['query_vector_base64'], validate=True), dtype='<f8')
    if vector_checksum(query) != probe['query_checksum']: raise VectorUnavailable('Frozen query vector changed')
    rows = index.candidates([query], min(100, len(index.factors)))[0]
    returned = {row['vector_id'] for row in rows}
    recall = min(probe['k'], len(returned & set(probe['eligible_ids']))) / probe['k']
    fetched = index.fetch_vectors([row['factor_id'] for row in rows])
    scores = fetched @ query
    error = max((abs(float(score) - row['cosine_similarity']) for score, row in zip(scores, rows)), default=1)
    top1 = bool(len(scores)) and bool(scores[0] >= probe['top1_score'] - 1e-5)
    report = {'snapshot_id': identity, 'probe': probe_index, 'recall_at_10': recall,
              'top1_agreement': top1, 'maximum_cosine_error': error, 'passed': recall >= .9 and top1 and error < 5e-4}
    if not report['passed']: raise VectorUnavailable('Vector retrieval quality probe failed')
    with registry.repo.transaction() as tx: tx.put('vector_quality', key, 'catalog', report)
    return report


def finalize(registry, identity, *, client=None):
    """Aggregate bounded step evidence, confirm ready counts, mark complete."""
    snapshot = registry.get(identity)
    if set(snapshot['verified_batches']) != set(snapshot['batches']): raise ValueError('Missing verified import batches')
    with registry.repo.read_transaction() as tx:
        for kind in ('factors', 'contexts'):
            cursor, seen = '', set()
            while True:
                row = tx.get('vector_inventory', digest([identity, kind, cursor]))
                if not row: raise ValueError('Incomplete vector inventory')
                data = row['data']
                if seen.intersection(data['ids']): raise ValueError('Repeated vector inventory IDs')
                seen.update(data['ids']); cursor = data['result']['cursor']
                if data['result']['done']: break
                if len(seen) > len(snapshot[kind]): raise ValueError('Unexpected inventory size')
            if seen != {row['id'] for row in snapshot[kind]}: raise ValueError('Vector inventory coverage mismatch')
        reports = [tx.get('vector_quality', digest([identity, i])) for i in range(len(snapshot['quality_probes']))]
    if not reports or any(not row or not row['data']['passed'] for row in reports): raise ValueError('Missing successful quality probes')
    UpstashFactorIndex(dict(snapshot, status='complete'), client=client or client_from_environment(write=True)).check()
    report = {'passed': True, 'probe_count': len(reports),
        'minimum_recall_at_10': min(row['data']['recall_at_10'] for row in reports),
        'top1_agreement': sum(row['data']['top1_agreement'] for row in reports) / len(reports),
        'maximum_cosine_error': max(row['data']['maximum_cosine_error'] for row in reports),
        'record_count': {kind: len(snapshot[kind]) for kind in ('factors', 'contexts')}}
    registry.complete(identity, report)
    return report


def mount_vector_workflow(app, repository):
    from qstash import Receiver
    from upstash_workflow.fastapi import Serve
    from .workflow_routes import client
    registry = VectorRegistry(repository)
    receiver = Receiver(os.environ['QSTASH_CURRENT_SIGNING_KEY'], os.environ['QSTASH_NEXT_SIGNING_KEY'])

    async def failure(context, status, body, headers):
        payload = context.request_payload
        if payload.get('environment') != environment() or payload.get('namespace') != jobs.namespace(): return
        def record_failure():
            with repository.transaction() as tx:
                tx.put('vector_failure', payload['snapshot_id'], 'catalog', {'status': status, 'at': now(), 'recovery_required': True})
        await asyncio.to_thread(record_failure)

    @Serve(app).post(PATH, qstash_client=client(), receiver=receiver, url=callback_url(), retries=5, failure_function=failure)
    async def vector_import(context):
        payload = context.request_payload
        details = await context.run('import-plan', lambda: asyncio.to_thread(plan, registry, payload))
        identity = payload['snapshot_id']
        for key in details['batches']:
            await context.run('import-' + key, lambda key=key: asyncio.to_thread(import_batch, registry, identity, key))
        for kind in ('factors', 'contexts'):
            cursor = ''
            for page_index in range(1000):
                result = await context.run('inventory-' + kind + '-' + str(page_index),
                    lambda kind=kind, cursor=cursor: asyncio.to_thread(inventory_page, registry, identity, kind, cursor))
                if result['done']: break
                cursor = result['cursor']
            else: raise ValueError('Vector inventory exceeds bounded workflow policy')
        for i in range(details['probes']):
            await context.run('quality-' + str(i), lambda i=i: asyncio.to_thread(query_probe, registry, identity, i))
        await context.run('complete-import', lambda: asyncio.to_thread(finalize, registry, identity))
        if details['activate']:
            await context.run('activate-import', lambda: asyncio.to_thread(registry.activate, identity, details['expected_previous']))
