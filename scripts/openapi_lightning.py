"""Private one-completion audit contract. Examples are synthetic, never model results."""
from copy import deepcopy


def extend(b, f, e):
    from reveal_backend.lightning_payload import RESULT_SCHEMA, PROMPT_VERSION
    obj, ref, array, string, null, enum = b.obj, b.ref, b.array, b.string, b.nullable, b.enum
    uuid, timestamp = string(format='uuid'), string(format='date-time')
    digest = string(pattern='^[a-f0-9]{64}$')
    b.add('LightningAuditInput', obj({'draft_id': uuid, 'draft_version': {'type': 'integer', 'minimum': 1}}))
    b.add('LightningAuditResult', deepcopy(RESULT_SCHEMA))
    b.add('LightningContinuationInput', obj({'mode': enum('online', 'local'),
        'research_direction': string(minLength=1, maxLength=6000)}))
    b.add('LightningContinuation', obj({'mode': enum('online', 'local'), 'id': uuid,
        'research_request_id': uuid, 'created_at': timestamp}, description=
        'An independently authorized child run with its own frozen request and reference-generation pin.'))
    b.add('LightningEvidenceReference', obj({'id': string(), 'pointer': string(), 'label': string(),
        'value': {}, 'source': {'type': 'object', 'additionalProperties': True}}, description=
        'A short model-visible reference resolved by the server to a retained source-state location. '
        'An audit observation is preliminary guidance and is not an accepted scientific Claim.'))
    b.add('LightningAudit', obj({'id': uuid, 'kind': {'type': 'string', 'const': 'lightning_audit'},
        'status': enum('preparing', 'assessing', 'succeeded', 'failed', 'interrupted'),
        'research_request_id': uuid, 'source_draft_id': uuid,
        'source_draft_version': {'type': 'integer', 'minimum': 1},
        'question': obj({'id': b.did('KnowledgeGap'), 'text': string()}),
        'created_at': timestamp, 'updated_at': timestamp, 'completed_at': null(timestamp),
        'continuation_expires_at': timestamp, 'reference_generation_id': digest,
        'result': null(ref('LightningAuditResult')), 'coverage': null(ref('CfdeAssessmentCoverage')),
        'evidence_references': array(ref('LightningEvidenceReference')),
        'provenance': obj({'model': string(), 'prompt_version': string(),
            'source_state_sha256': digest, 'request_sha256': digest, 'response_sha256': digest},
            required=['model', 'prompt_version']),
        'usage': null(obj({'input_tokens': {'type': 'integer', 'minimum': 0},
            'output_tokens': {'type': 'integer', 'minimum': 0}})),
        'error': null(ref('CfdeAssessmentError')), 'continuations': array(ref('LightningContinuation'))},
        description='Private frozen initial audit, independent of later editor changes or deletion. '
        'Only an explicit POST may make one provider attempt. There is no agent or retrieval loop. '
        'Every verdict is advisory and permits an explicitly requested continuation.'))
    b.add('LightningAuditList', obj({'items': array(ref('LightningAudit')), 'page': obj({
        'next_cursor': null(string()), 'has_more': {'type': 'boolean'}, 'snapshot_id': string()},
        required=['next_cursor', 'has_more'])}))
    b.SCHEMAS['ResearchRequest']['properties']['lightning_audit_id'] = uuid
    b.SCHEMAS['ResearchRequest']['properties']['lightning_context'] = {
        'type': 'object', 'additionalProperties': True,
        'description': 'Private frozen preliminary audit context, preserved separately from original researcher instructions; never eligible scientific evidence.'}
    collections = b.SCHEMAS['WorkspaceEvent']['properties']['collections']['items']['enum']
    if 'audits' not in collections:
        collections.append('audits')

    audit_id = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
    gap = e['research_request']['document']['knowledge_gaps'][0]
    prepared = {'id': audit_id, 'kind': 'lightning_audit', 'status': 'preparing',
        'research_request_id': b.REQUEST_ID, 'source_draft_id': b.DRAFT_ID, 'source_draft_version': 2,
        'question': {'id': gap['id'], 'text': gap['text']},
        'created_at': b.NOW, 'updated_at': b.NOW, 'completed_at': None,
        'continuation_expires_at': '2026-10-24T16:00:00Z', 'reference_generation_id': e['reference']['current'],
        'result': None, 'coverage': None, 'evidence_references': [],
        'provenance': {'model': 'claude-sonnet-4-6', 'prompt_version': PROMPT_VERSION},
        'usage': None, 'error': None, 'continuations': []}
    result = {'assessment': 'partial', 'summary': 'Synthetic example: the retained loading suggests a candidate direction that requires further checks.',
        'observations': [{'text': 'A retained factor-gene loading is relevant to the proposed comparison.', 'evidence_refs': ['E1']}],
        'recommended_direction': 'Investigate whether the observed factor-gene association provides a context-specific connection to the gap.',
        'missing_evidence': ['Direct evidence for the proposed biological connection.'],
        'next_steps': ['Inspect exact source captures and test the proposed connection using authorized evidence tools.'],
        'limitations': ['This synthetic example illustrates the contract and is not a scientific finding.']}
    completed = {**deepcopy(prepared), 'status': 'succeeded', 'updated_at': b.LATER, 'completed_at': b.LATER,
        'result': result, 'coverage': {'factor_count': 1, 'gene_loading_count': 1, 'gene_set_loading_count': 0,
            'unique_gene_set_count': 0, 'missing': ['gene_set_loadings'], 'truncations': [], 'complete': False},
        'evidence_references': [{'id': 'E1', 'pointer': '/factors/0/genes/rows/0',
            'label': 'Synthetic retained factor-gene loading', 'value': {'symbol': 'GENE_EXAMPLE', 'loading': 0.25},
            'source': {'generation_id': e['reference']['current']}}],
        'provenance': {**prepared['provenance'], 'source_state_sha256': b.sha('synthetic-source-state'),
            'request_sha256': b.sha('synthetic-model-request'), 'response_sha256': b.sha(result)},
        'usage': {'input_tokens': 1000, 'output_tokens': 300}}
    failed = {**deepcopy(prepared), 'status': 'failed', 'completed_at': b.LATER, 'updated_at': b.LATER,
        'error': {'code': 'LIGHTNING_TIMEOUT', 'detail': 'This audit timed out. Start a new audit to try again.', 'retryable': True}}
    continuation = {'mode': 'online', 'id': b.JOB_ID,
        'research_request_id': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', 'created_at': b.LATER}
    common = ('Requires the owning registered or anonymous workspace principal. All results are private, no-store. '
        'A Lightning audit assesses a bounded stored-evidence snapshot; it creates no accepted scientific account '
        'and performs no connected-graph or literature retrieval. Inputs and generated guidance are not instructions '
        'or scientific acceptance. Examples are synthetic contract fixtures. ')
    path = '/v1/lightning-audits'
    create = b.operation(path, 'post', 'createLightningAudit', 'Lightning audits', 'Create an initial evidence audit',
        common + 'Requires REVEAL_LIGHTNING_ENABLED. Freezes the owned draft and starts at most one Claude completion. '
        'Same-key replays recover the original audit even if the draft is later changed or removed. '
        'Changed inputs with the same key conflict. Source preparation and inference execute outside write transactions. '
        'The request has a 100,000-byte limit, a 3,000-output-token cap and a 120-second overall deadline. '
        'Failed or interrupted calls are not automatically retried; an explicit new submission creates a new audit.',
        'LightningAudit', {'preparing': prepared}, status=202, request_schema='LightningAuditInput',
        request_examples={'draft': {'draft_id': b.DRAFT_ID, 'draft_version': 2}}, idempotent=True,
        errors=('400', '401', '403', '404', '409', '422', '429', '503'))
    create['responses']['202']['description'] = 'Saved audit receipt; read Location to observe progress.'
    create['responses']['202']['headers']['Location']['example'] = path + '/' + audit_id
    listing = b.operation(path, 'get', 'listLightningAudits', 'Lightning audits', 'List private initial audits',
        common + 'Saved results remain independent of mutable or deleted drafts. Listing never starts inference.',
        'LightningAuditList', {'history': {'items': [completed], 'page': {'next_cursor': None, 'has_more': False}}},
        parameters=[b.parameter('limit', 'query', {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 50),
            b.parameter('cursor', 'query', string(), 'opaque-owned-audit-cursor')],
        errors=('400', '401', '403', '409', '422', '503'))
    identity = b.parameter('audit_id', 'path', uuid, audit_id, True)
    get = b.operation(path + '/{audit_id}', 'get', 'getLightningAudit', 'Lightning audits', 'Read an initial audit',
        common + 'Reading never dispatches or retries model work. Pending results may long-poll without holding a database connection. '
        'Terminal results return immediately. Audits remain inspectable after their continuation window expires.',
        'LightningAudit', {'completed': completed, 'preparing': prepared, 'failed': failed},
        parameters=[identity, b.parameter('wait', 'query', {'type': 'integer', 'minimum': 0, 'maximum': 20, 'default': 0}, 15)],
        errors=('401', '403', '404', '422', '503'))
    follow = b.operation(path + '/{audit_id}/continue', 'post', 'continueLightningAudit', 'Lightning audits',
        'Continue an audit with an agent', common + 'Requires REVEAL_LIGHTNING_ENABLED, a completed audit and '
        'an unexpired continuation window. All assessment categories may continue. Clones a new frozen request and '
        'independent generation pin, preserving original inputs separately from the edited direction. '
        'Uses ordinary online/local quotas and authorization. Retained superseded generations are allowed; unavailable '
        'generations require a fresh audit. Audit artifacts enter the agent seed as preliminary context, never eligible evidence.',
        'LightningContinuation', {'online': continuation, 'local': {**continuation, 'mode': 'local'}}, status=202,
        request_schema='LightningContinuationInput', request_examples={'online': {'mode': 'online', 'research_direction': result['recommended_direction']},
            'local': {'mode': 'local', 'research_direction': result['recommended_direction']}},
        parameters=[identity], idempotent=True, errors=('400', '401', '403', '404', '409', '422', '429', '503'))
    follow['responses']['202']['description'] = 'Saved child-run receipt; online dispatch or local seed preparation is asynchronous.'
    for op in (create, listing, get, follow):
        for response in op['responses'].values():
            response.setdefault('headers', {}).update({'Cache-Control': {'schema': string(), 'example': 'private, no-store'},
                'Vary': {'schema': string(), 'example': 'Authorization'}})
