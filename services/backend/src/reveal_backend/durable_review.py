"""Restartable scientific review with a persisted reservation before each paid call.

No transport retry assumes an unacknowledged provider call was free. The frozen
checkpoint contains the evidence, conversation, read coverage and exact budgets.
"""
from copy import deepcopy
import json
from jsonschema import validate
from .evidence_package import canonical_json, sha256, require
from . import scientific_grounding as grounding
from .scientific_review_reader import EvidenceReader

TOOLS = [
    {'name': 'read_evidence', 'description': 'Read exact captured evidence by JSON pointer and optional page offset. Inventory is not evidence; follow child pointers.', 'input_schema': grounding.READ_SCHEMA},
    {'name': 'submit_review', 'description': 'Submit a complete source-grounded verdict covering every claim and synthesis; every reference must have been read.', 'input_schema': grounding.SCHEMA},
    {'name': 'review_unavailable', 'description': 'Stop when the evidence cannot support an adequate review.', 'input_schema': {'type': 'object', 'properties': {'reason': {'type': 'string', 'maxLength': 1600}}, 'required': ['reason'], 'additionalProperties': False}},
]
FINISH = {'name': 'finish_review', 'description': 'Finish with the complete review or an unavailable reason. No further reads remain.',
    'input_schema': grounding.FINISH_SCHEMA}


def initial(kind, document, inputs, *, ledger_path=None, budget=.30):
    # Validates the pinned model and configured limits before scheduling calls.
    session = grounding._ReviewSession(grounding.MODEL, 'configuration-check', budget, None)
    state = {'format': 'reveal.review-checkpoint/1', 'kind': kind, 'model': grounding.MODEL,
        'budget': budget, 'document': document, 'inputs': inputs, 'turn': 0, 'messages': [],
        'pending': None, 'response': None, 'used_ids': [], 'session': export_session(session),
        'document_sha256': sha256(canonical_json(document)), 'input_sha256': sha256(canonical_json(inputs)),
        'complete': False, 'result': None}
    if kind == 'analysis':
        evidence = grounding.review_evidence(inputs, ledger_path)
        reader = EvidenceReader(evidence); index = reader.initial()
        state.update(evidence=evidence, evidence_sha256=sha256(canonical_json(evidence)),
            initial_context_sha256=sha256(canonical_json(index)), ledger_sha256=sha256(ledger_path.read_bytes()),
            reads=[], provided=sorted(reader.provided), chunks={})
        state['messages'] = [{'role': 'user', 'content': canonical_json({'proposed_document': document, 'evidence_index': index}).decode()}]
    else:
        from .box_paragraph import validate_paragraph_segments
        segments = validate_paragraph_segments(document, inputs)
        content = {group: values for group, values in inputs['account_document'].items()
                   if group in {'scientific_accounts', 'claims', 'propositions', 'claim_scores', 'evidence_items', 'mechanisms', 'knowledge_gaps', 'questions'}}
        state['segment_count'] = len(segments)
        state['messages'] = [{'role': 'user', 'content': canonical_json({'accepted_scientific_content': content, 'proposed_segments': segments}).decode()}]
    return state


def export_session(session):
    return {'calls': session.calls, 'spent': session.spent, 'previous_messages': session.previous_messages,
        'previous_configuration': session.previous_configuration.decode() if session.previous_configuration else None,
        'previous_input_tokens': session.previous_input_tokens, 'max_turns': session.max_turns,
        'max_request_bytes': session.max_request_bytes, 'max_input_tokens': session.max_input_tokens}


def call_one(state, api_key, checkpoint, *, client=None):
    """One model call. checkpoint must durably persist before returning."""
    state = deepcopy(state)
    if state.get('pending'):
        audit = {**state['session'], 'ambiguous_call': state['pending'], 'configured_max_usd': state['budget']}
        raise grounding.ScientificReviewUnavailable('Scientific review call has an unacknowledged paid reservation; explicit review retry is required', audit)
    if state.get('response') or state['complete']: return state
    require(sha256(canonical_json(state['document'])) == state['document_sha256'] and
            sha256(canonical_json(state['inputs'])) == state['input_sha256'], 'Review frozen input changed')
    def reserve(value):
        state['pending'] = value
        checkpoint(state)
    session = grounding._ReviewSession(state['model'], api_key, state['budget'], client, reserve=reserve)
    for key, value in state['session'].items():
        if key == 'previous_configuration': value = value.encode() if value else None
        setattr(session, key, value)
    payload = {'model': state['model'], 'messages': state['messages'], 'max_tokens': grounding.MAX_OUTPUT_TOKENS}
    if state['kind'] == 'analysis':
        final = state['turn'] == session.max_turns - 1
        payload.update(system=grounding.SYSTEM + grounding.READER_SYSTEM, tools=TOOLS + [FINISH] if final else TOOLS,
                       tool_choice={'type': 'tool', 'name': 'finish_review'} if final else {'type': 'any'})
    else:
        payload.update(system=grounding.PARAGRAPH_SYSTEM, output_config={'format': {'type': 'json_schema', 'schema': grounding.PARAGRAPH_SCHEMA}})
    try: response = session.post(payload)
    except Exception as exc:
        audit = {**session.audit(), 'ambiguous_call': state.get('pending')}
        state.update(session=export_session(session), failure={
            'error_type': type(exc).__name__,
            'message': str(exc)[:1600] if isinstance(exc, grounding.ScientificReviewUnavailable) else 'Scientific review request failed',
            'audit': audit})
        checkpoint(state)
        raise grounding.ScientificReviewUnavailable('Scientific review response unavailable; any reserved call remains accounted',
            audit) from exc
    state.update(response=response, session=export_session(session), pending=None, processed_response_sha256=None)
    checkpoint(state)  # A response is durable before any tool/decision processing.
    return state


def process_response(state):
    """A malformed reviewer response is no verdict, not rejected science.

    Keep the same failure boundary as the synchronous reviewer. The durable
    response remains available for inspection and an explicit review retry.
    """
    try:
        return _process_response(state)
    except Exception as exc:
        audit = {'calls': state['session']['calls'],
                 'actual_cost_usd': state['session']['spent'],
                 'configured_max_usd': state['budget'],
                 'reads': state.get('reads', []),
                 'response_error_type': type(exc).__name__}
        if isinstance(exc, grounding.ScientificReviewUnavailable):
            audit.update(exc.audit)
        raise grounding.ScientificReviewUnavailable(
            str(exc) if isinstance(exc, grounding.ScientificReviewUnavailable)
            else 'Scientific review evidence or response was invalid', audit) from exc


def _process_response(state):
    state = deepcopy(state); response = state['response']
    if response is None and state.get('processed_response_sha256'): return state
    require(response is not None and state['pending'] is None, 'Review response is not durably acknowledged')
    state['processed_response_sha256'] = sha256(canonical_json(response))
    if state['kind'] != 'analysis':
        grounding._available(response.get('stop_reason') == 'end_turn', 'Paragraph reviewer did not complete')
        review = json.loads(''.join(block['text'] for block in response.get('content', []) if block.get('type') == 'text'))
        validate(review, grounding.PARAGRAPH_SCHEMA)
        indexes = [item['segment_index'] for item in review['segments']]
        require(len(indexes) == state['segment_count'] and set(indexes) == set(range(state['segment_count'])), 'Paragraph review coverage is invalid')
        require(all(1 <= len(item['finding']) <= 1600 for item in review['segments']), 'Paragraph finding is invalid')
        return finish(state, review, all(item['faithful'] for item in review['segments']))
    require(sha256(canonical_json(state['evidence'])) == state['evidence_sha256'], 'Frozen review evidence changed')
    reader = EvidenceReader(state['evidence'])
    reader.reads, reader.provided, reader.chunks = state['reads'], set(state['provided']), state['chunks']
    grounding._available(response.get('stop_reason') == 'tool_use', 'Scientific review did not return a complete tool response')
    blocks = response.get('content', [])
    grounding._available(isinstance(blocks, list) and all(block.get('type') in ('text', 'tool_use') for block in blocks), 'Scientific review returned unsupported content')
    calls = [block for block in blocks if block.get('type') == 'tool_use']
    grounding._available(0 < len(calls) <= 12, 'Review tool batch exceeds limit')
    final = state['turn'] == state['session']['max_turns'] - 1
    allowed = TOOLS + [FINISH] if final else TOOLS
    used = set(state['used_ids'])
    for call in calls:
        identity = call.get('id')
        grounding._available(isinstance(identity, str) and identity and identity not in used, 'Review tool identity reused')
        used.add(identity)
        grounding._available(not final or len(calls) == 1 and call.get('name') == 'finish_review', 'Final review turn must decide')
        grounding._available(call.get('name') in {tool['name'] for tool in allowed}, 'Unauthorized review tool')
        validate(call.get('input'), next(tool['input_schema'] for tool in allowed if tool['name'] == call['name']))
    if final:
        decision = calls[0]['input']
        grounding._available('unavailable_reason' not in decision, 'Scientific reviewer could not adequately inspect evidence')
        calls = [{**calls[0], 'name': 'submit_review', 'input': decision['review']}]
    grounding._available(not any(call['name'] == 'review_unavailable' for call in calls), 'Scientific reviewer could not adequately inspect evidence')
    verdicts = [call for call in calls if call['name'] == 'submit_review']
    if verdicts:
        grounding._available(len(calls) == 1, 'Review submitted before pending reads completed')
        review = verdicts[0]['input']; accepted = grounding.validate_review(review, state['document'], state['evidence'])
        for item in [*review['claims'], review['synthesis']]:
            grounding._available(all(reader.was_read(ref) for ref in item['source_refs']), 'Review cited unread evidence')
        return finish(state, review, accepted)
    results = []
    for call in calls:
        try:
            value = reader.read(call['input']['pointer'], call['input'].get('offset', 0))
            result = {'type': 'tool_result', 'tool_use_id': call['id'], 'content': canonical_json(value).decode()}
        except (KeyError, IndexError, ValueError):
            result = {'type': 'tool_result', 'tool_use_id': call['id'], 'is_error': True,
                      'content': 'Invalid or out-of-bounds evidence read; use an indexed pointer and valid offset.'}
        results.append(result)
    state['messages'].extend([{'role': 'assistant', 'content': blocks}, {'role': 'user', 'content': results}])
    state.update(reads=reader.reads, provided=sorted(reader.provided), chunks=reader.chunks,
                 used_ids=sorted(used), response=None, turn=state['turn'] + 1)
    return state


def finish(state, review, accepted):
    audit = {'model': state['model'], 'calls': state['session']['calls'], 'actual_cost_usd': state['session']['spent'],
             'configured_max_usd': state['budget'], 'document_sha256': state['document_sha256'],
             'input_sha256': state['input_sha256'], 'reads': state.get('reads', []),
             'provided_context_pointers': state.get('provided', []), 'review': review, 'accepted': accepted,
             'format': 'reveal.scientific-grounding/2' if state['kind'] == 'analysis' else 'reveal.paragraph-faithfulness/1'}
    state.update(complete=True, result=audit, response=None, turn=state['turn'] + 1)
    return state
