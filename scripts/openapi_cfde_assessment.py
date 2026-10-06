"""Automatic pre-run CFDE assessment contract; all examples are synthetic."""
from copy import deepcopy


def extend(b, f, e):
    obj, ref, array, string, null, enum = b.obj, b.ref, b.array, b.string, b.nullable, b.enum
    uuid = string(format='uuid')
    timestamp = string(format='date-time')
    digest = string(pattern='^[a-f0-9]{64}$')
    count = {'type': 'integer', 'minimum': 0}
    probability = {'type': 'number', 'minimum': 0, 'maximum': 1}
    b.add('CfdeAssessmentInput', obj({'draft_version': {'type': 'integer', 'minimum': 1},
        'composer': ref('Composer')}, description=
        'Assess the current composer, including unsaved edits, against an owned draft revision. '
        'This request neither saves the draft nor submits research. Caller-supplied owners, models, '
        'probabilities, evidence or administrative authority are not accepted.'))
    b.add('CfdeAssessmentResult', obj({
        'verdict': enum('yes', 'no'), 'probability_yes': deepcopy(probability),
        'probability_no': deepcopy(probability),
        'confidence': {**probability, 'description': 'Separate reported rubric confidence, not the yes probability.'},
        'main_blocker': enum('none', 'weak_relevance', 'missing_cfde_evidence', 'wrong_data_type',
            'species_or_context_mismatch', 'incomplete_inputs', 'unsupported_inference'), 'relationship_support': obj({
            key: deepcopy(probability) for key in ('gene_gene_set', 'gene_mechanism', 'gene_set_mechanism')}),
        'calibration': {'type': 'string', 'const': 'not_calibrated'}}, description=
        'Advisory model prediction of whether the scoped CFDE evidence could support at least one '
        'responsible, scientifically supported relationship relevant to the selected question. '
        'Neither all three relationship categories nor complete resolution of the knowledge gap is required. '
        'Choice yes/no likelihoods retain provider values without renormalizing rounded mass '
        '(for example, 0.56 plus 0.43). These values are not calibrated success rates. '
        'The three relationship-support scores and reported confidence are separate rubric judgments. '
        'A prediction is not scientific evidence, a supported claim, validation or permission to publish. '
        'Errors and unavailable evidence must never be converted into a no verdict.'))
    b.add('CfdeAssessmentCoverage', obj({
        'factor_count': deepcopy(count), 'gene_loading_count': deepcopy(count),
        'gene_set_loading_count': deepcopy(count), 'unique_gene_set_count': deepcopy(count),
        'missing': array(string()), 'truncations': array(obj({'source': string(),
            'included_chars': deepcopy(count), 'total_chars': deepcopy(count)})),
        'complete': {'type': 'boolean'}}, description=
        'Counts and explicit omissions for this bounded assessment input. Each selected factor uses '
        'its exact pinned top 50 gene and top 50 GeneSet loadings, where available, with bounded '
        'context excerpts. The provider receives a simple scientific projection of names, loadings, '
        'source labels and context. Exact source identities, hashes, definitions and provenance remain '
        'in the server-retained pinned audit state rather than the model payload. Complete means the requested '
        'bounded input was available; it does not claim complete biological or CFDE coverage. '
        'Oversized input fails explicitly instead of silently dropping evidence or returning a no verdict.'))
    b.add('CfdeAssessmentError', obj({'code': string(), 'detail': string(), 'retryable': {'type': 'boolean'}},
        description='Operational failure only. Retrying requires another explicit assessment request; polling never retries a model call.'))
    assessment = obj({'id': uuid, 'draft_id': uuid,
        'draft_version': {'type': 'integer', 'minimum': 1}, 'composer_sha256': digest,
        'status': enum('preparing', 'assessing', 'succeeded', 'failed', 'interrupted'),
        'created_at': {**timestamp, 'description': 'Creation time of this owner-scoped assessment receipt.'},
        'updated_at': {**timestamp, 'description': 'Original forecast update time, preserved when a cached forecast is reused; it can precede this receipt creation.'},
        'expires_at': {**timestamp, 'description': 'Underlying operation deadline, preserved on shared or completed forecasts; not the cache retention deadline.'},
        'model': string(), 'rubric_version': string(), 'reference_generation_id': null(string()),
        'result': null(ref('CfdeAssessmentResult')), 'coverage': null(ref('CfdeAssessmentCoverage')),
        'error': null(ref('CfdeAssessmentError')), 'stale': {'type': 'boolean'}}, description=
        'Owner-scoped, expiring advisory assessment of one exact composer. '
        'reference_generation_id can be null during preparation; the exact source generation is '
        'pinned when asynchronous source preparation completes. '
        'No Job, ResearchRequest, ScientificAccount or exploration outcome is created. '
        'Every owner receives a separate assessment UUID bound to their own draft revision and composer hash. '
        'Default inputs with no researcher notes or uploads may share the underlying forecast; '
        'no other owner, draft or assessment identity is exposed. '
        'expires_at is the operation deadline, not deletion of a completed prediction. '
        'A stale result remains inspectable but should not be shown as the current editor assessment. '
        'stale reports a changed saved draft revision or active reference generation; clients must also '
        'compare composer_sha256 with current unsaved edits before displaying a current prediction. '
        'Preparing and assessing have no prediction; failed and interrupted preserve an explicit error. '
        'The operation deadline is at most 120 seconds; interrupted or restarted work needs explicit '
        'user retry and is never automatically billed again.')
    assessment['allOf'] = [
        {'if': {'properties': {'status': {'const': 'succeeded'}}},
         'then': {'properties': {'result': ref('CfdeAssessmentResult'), 'error': {'type': 'null'}}},
         'else': {'properties': {'result': {'type': 'null'}}}},
        {'if': {'properties': {'status': {'enum': ['failed', 'interrupted']}}},
         'then': {'properties': {'error': ref('CfdeAssessmentError')}},
         'else': {'properties': {'error': {'type': 'null'}}}}]
    b.add('CfdeAssessment', assessment)

    assessment_id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    composer = deepcopy(e['composer'])
    factor = e['kpn_factor']
    composer['model'] = factor['model']
    composer['eaggl_anchors'] = [{'reference': {'source': factor['source'], 'source_id': factor['source_id'],
        'source_revision': factor['source_revision'], 'dapper_id': factor['object']['id']},
        'origin': 'manual', 'suggestion_id': None}]
    prepared = {'id': assessment_id, 'draft_id': b.DRAFT_ID, 'draft_version': 2,
        'composer_sha256': b.sha(composer), 'status': 'preparing',
        'created_at': b.NOW, 'updated_at': b.NOW, 'expires_at': '2026-09-24T16:02:00Z',
        'model': 'jev-1.13.0', 'rubric_version': 'cfde-support-v1',
        'reference_generation_id': None,
        'result': None, 'coverage': None, 'error': None, 'stale': False}
    coverage = {'factor_count': len(composer['eaggl_anchors']), 'gene_loading_count': 50,
        'gene_set_loading_count': 50, 'unique_gene_set_count': 50,
        'missing': [], 'truncations': [], 'complete': True}
    predicted = {**prepared, 'status': 'succeeded', 'updated_at': b.LATER, 'coverage': coverage,
        'reference_generation_id': e['reference']['current'],
        'result': {'verdict': 'yes', 'probability_yes': 0.56, 'probability_no': 0.43, 'confidence': 0.48,
            'main_blocker': 'weak_relevance',
            'relationship_support': {'gene_gene_set': 0.65, 'gene_mechanism': 0.54, 'gene_set_mechanism': 0.58},
            'calibration': 'not_calibrated'}}
    shared_receipt = {**predicted, 'created_at': '2026-09-25T16:00:00Z'}
    incomplete = {**coverage, 'complete': False, 'missing': ['Synthetic example: a pinned GeneSet definition is unavailable.'],
        'truncations': [{'source': 'user_inputs/context', 'included_chars': 2000, 'total_chars': 2400}]}
    examples = {
        'preparing': prepared,
        'assessing': {**prepared, 'status': 'assessing', 'coverage': coverage,
            'reference_generation_id': e['reference']['current']},
        'synthetic_low_confidence_yes': predicted,
        'synthetic_shared_cached_receipt': shared_receipt,
        'failed_oversized_input': {**prepared, 'status': 'failed', 'updated_at': b.LATER, 'coverage': incomplete,
            'reference_generation_id': e['reference']['current'],
            'error': {'code': 'CFDE_ASSESSMENT_TOO_LARGE',
                'detail': 'These inputs exceed the assessment size limit. Use fewer mechanisms or shorter context; the research run remains available.',
                'retryable': False}},
        'interrupted': {**prepared, 'status': 'interrupted',
            'error': {'code': 'CFDE_ASSESSMENT_INTERRUPTED',
                'detail': 'This check did not finish. Request another assessment to try again.', 'retryable': True}},
        'stale_prediction': {**predicted, 'stale': True}}
    common = ('Requires a valid existing registered or anonymous workspace principal that owns the draft. '
        'An administrator science-read key cannot authorize this operation. '
        'This advisory prediction never launches research or creates scientific records and is '
        'not a prerequisite for local or hosted research. CFDE grounding remains encouraged, not mandatory. '
        'Predictions must not be cited as evidence or used as scientific validation. '
        'All response and error bodies are private and no-store, with Vary: Authorization. '
        'Examples are synthetic contract fixtures; no model or scientific source was queried. ')
    path = '/v1/drafts/{draft_id}/cfde-assessments'
    draft_parameter = b.parameter('draft_id', 'path', uuid, b.DRAFT_ID, True)
    create = b.operation(path, 'post', 'createCfdeAssessment', 'CFDE assessment',
        'Assess CFDE support before research', common +
        'Create an assessment receipt for the supplied composer without saving its unsaved edits. '
        'The editor sends this POST automatically after the selected gap and mechanism suggestions are '
        'ready and edits have settled for 1.5 seconds, with at most one automatic attempt per input binding. '
        'Failed or interrupted assessments require an explicit retry; the editor does not automatically '
        'resubmit the same inputs after failure. Only this POST can dispatch assessment work; reading a '
        'draft or polling an assessment cannot start a provider call. '
        'The trusted service selects the model and rubric, retains exact pinned factor and GeneSet '
        'source state for audit, and makes the assessment asynchronously from a simple scientific '
        'projection of the question, selected factors, names, loadings, source labels and researcher context. '
        'The model does not receive DAPPER identifiers, hashes, provenance records or audit encoding rules. '
        'The operation is bounded by a '
        '120-second deadline and an owner daily quota. Source preparation and any cold catalog load '
        'happen asynchronously after acceptance. Public default inputs share forecasts when the source gap, '
        'selected factor references, reference model and selected graphs match and no researcher direction, '
        'context, hypotheses or uploads are present. Identical concurrent defaults share pending work; '
        'successful default forecasts can be reused for up to seven days, unless the active reference '
        'generation, assessment model or rubric changes. Each owner still receives a separate owned '
        'assessment UUID and independent draft revision binding. Composers containing notes or uploads '
        'use private caching only. No other owner identity, assessment ID or private context is exposed. '
        'A changed body with the same '
        'Idempotency-Key returns 409. Failed or interrupted work needs an explicit retry with a new key; '
        'retrying the original key recovers the original result. '
        'Process restart never automatically repeats provider calls. ',
        'CfdeAssessment', {'preparing': prepared, 'reused_prediction': predicted, 'shared_cached_receipt': shared_receipt},
        parameters=[draft_parameter], request_schema='CfdeAssessmentInput',
        request_examples={'current_composer': {'draft_version': 2, 'composer': composer}},
        status=202, idempotent=True, errors=('400', '401', '403', '404', '409', '422', '429', '503'))
    location = path.replace('{draft_id}', b.DRAFT_ID) + '/' + assessment_id
    create['responses']['202']['description'] = 'Assessment accepted or existing assessment reused; poll Location.'
    create['responses']['202']['headers']['Location']['example'] = location
    get = b.operation(path + '/{assessment_id}', 'get', 'getCfdeAssessment', 'CFDE assessment',
        'Read a CFDE support assessment', common +
        'Read only this owner/draft-bound assessment. Polling makes no remote calls and never starts '
        'or retries preparation, model work or research. No result after failure is replaced by a '
        'no verdict. Pending work past its deadline is reported as interrupted; completed results remain readable. '
        'Shared work is projected through this owner receipt: created_at is this receipt creation, '
        'updated_at remains the original forecast update, and expires_at remains its operation deadline. '
        'An unavailable assessment returns 404; inspect stale input pins '
        'before displaying a saved prediction for the current editor. ',
        'CfdeAssessment', examples, parameters=[draft_parameter,
            b.parameter('assessment_id', 'path', uuid, assessment_id, True)],
        errors=('401', '403', '404', '422', '503'))
    for op in (create, get):
        op['responses']['410'] = {'description': 'Temporary editor expired.', 'content': b.content('Problem',
            {'editor_expired': b.problem(410, 'EDITOR_EXPIRED',
                'This temporary editor expired. Open the knowledge gap to start again.')}, 'application/problem+json')}
        for status, response in op['responses'].items():
            response.setdefault('headers', {}).update({
                'Cache-Control': {'schema': string(), 'example': 'private, no-store'},
                'Vary': {'schema': string(), 'example': 'Authorization'}})
            if status == '403':
                response.update(description='Access denied.', content=b.content('Problem', {'access_denied':
                    b.problem(403, 'ACCESS_DENIED', 'This principal cannot access the requested assessment.')}, 'application/problem+json'))
            if status == '400':
                response.update(description='Assessment idempotency key required.', content=b.content('Problem', {'idempotency_key_required':
                    b.problem(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Supply an Idempotency-Key of 8–128 characters.')}, 'application/problem+json'))
        if '409' in op['responses']:
            op['responses']['409'].update(description='Draft revision or idempotency conflict.',
                content=b.content('Problem', {
                    'draft_changed': b.problem(409, 'VERSION_CONFLICT', 'The draft revision changed. Reload its current version.', current_version=3),
                    'idempotency_conflict': b.problem(409, 'IDEMPOTENCY_CONFLICT', 'This key was already used with a different request body.')}, 'application/problem+json'))
