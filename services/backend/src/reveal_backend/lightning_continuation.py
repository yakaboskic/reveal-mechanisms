"""Turn one retained preliminary audit into ordinary, independently pinned research work."""
from copy import deepcopy

from .auth import Problem, owned, principal_with
from .reference_generation import CONTROL_KIND, CONTROL_ID, generation_of_anchors
from .repository import digest, now, uid
from .research_work import ResearchWorkService


def validate_body(body):
    if (not isinstance(body, dict) or set(body) != {'mode', 'research_direction'}
            or body.get('mode') not in ('online', 'local')
            or not isinstance(body.get('research_direction'), str)
            or not body['research_direction'].strip() or len(body['research_direction']) > 6000):
        raise Problem(422, 'INVALID_REQUEST', 'Supply mode online or local and a research direction of 1–6000 characters.')


def hydrate_context(tx, owner, frozen):
    """Load exact retained audit bytes once for seed assembly; keep request histories compact."""
    if not frozen.get('lightning_audit_id'): return frozen
    result = deepcopy(frozen)
    context = result.get('lightning_context') or {}
    if context.get('audit_id') != result['lightning_audit_id']:
        raise Problem(409, 'AUDIT_CONTEXT_UNAVAILABLE', 'The research request’s audit lineage changed.')
    audit = owned(tx, 'lightning_audit', context['audit_id'], owner)['data']
    artifacts = {name: deepcopy(audit.get(name)) for name in ('source_state', 'model_payload', 'response')}
    if any(value is None or digest(value) != context.get('hashes', {}).get(name)
            for name, value in artifacts.items()):
        raise Problem(409, 'AUDIT_CONTEXT_UNAVAILABLE', 'The exact audit context is unavailable or changed.')
    context['artifacts'] = artifacts
    result['lightning_context'] = context
    return result


def continue_audit(repository, authorization, audit_id, body, key, *, reload_gate,
                   check_job_quota, analysis_job_rows):
    from .lightning_audits import require_enabled
    validate_body(body)
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise Problem(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Supply an Idempotency-Key of 8–128 characters.')
    checksum = digest([audit_id, body])
    with repository.transaction() as tx:
        identity, records = principal_with(tx, authorization, lambda owner: (
            ('lightning_audit', audit_id), ('lightning_continuation', digest([owner, key])),
            (CONTROL_KIND, CONTROL_ID)))
        owner = identity['user_id']; retry_id = digest([owner, key])
        # Authorize the parent even on a delivery retry; no stale response can reveal transferred work.
        audit = deepcopy(owned(tx, 'lightning_audit', audit_id, owner)['data'])
        previous = records.get(('lightning_continuation', retry_id))
        if previous:
            if previous['owner'] != owner or previous['data']['checksum'] != checksum:
                raise Problem(409, 'IDEMPOTENCY_CONFLICT', 'This retry key was used for different input.')
            return deepcopy(previous['data']['result'])
        require_enabled()
        reload_gate(tx)
        public = audit['public']
        if public['status'] != 'succeeded':
            raise Problem(409, 'AUDIT_NOT_COMPLETE', 'Wait for a completed Lightning audit before continuing.')
        if public['continuation_expires_at'] <= now():
            raise Problem(409, 'AUDIT_CONTINUATION_EXPIRED', 'This audit’s continuation window has expired. Create a fresh audit.')
        source = owned(tx, 'request', public['research_request_id'], owner)['data']
        binding = deepcopy(owned(tx, 'request_binding', source['id'], owner)['data'])
        pin = owned(tx, 'research_pin', source['id'], owner)['data']
        generation = generation_of_anchors(binding['anchors'])
        if (pin.get('state') != 'active' or pin.get('generation_id') != generation
                or generation != public['reference_generation_id'] or pin.get('expires_at', '') <= now()):
            raise Problem(409, 'SOURCE_UNAVAILABLE', 'The audit’s reference generation is no longer retained. Create a fresh audit.')
        artifacts = {name: deepcopy(audit.get(name)) for name in ('source_state', 'model_payload', 'response')}
        if any(value is None for value in artifacts.values()):
            raise Problem(409, 'AUDIT_CONTEXT_UNAVAILABLE', 'The exact audit context is unavailable. Create a fresh audit.')
        frozen = deepcopy(source)
        # The source request may have acquired an archive stamp after cutover; child acceptance determines its own.
        frozen.pop('archive', None)
        frozen.update(id=uid(), owner_user_id=owner, submitted_at=now(), retrieval_mode='progressive',
            attribution={'user_id': owner, 'person_id': None, 'display_name': identity.get('display_name'),
                'orcid': identity.get('orcid'), 'orcid_authenticated': identity.get('orcid_authenticated', False),
                'observed_at': now(), 'principal_kind': identity['principal_kind']},
            lightning_audit_id=audit_id, lightning_context={'audit_id': audit_id,
                'research_direction': body['research_direction'],
                'hashes': {name: digest(value) for name, value in artifacts.items()}})
        rows = [('request', frozen['id'], owner, frozen), ('request_binding', frozen['id'], owner, binding)]
        if body['mode'] == 'online':
            check_job_quota(tx, identity)
            child, child_rows = analysis_job_rows(identity, frozen, binding, {'kind': 'analysis', 'lightning_audit_id': audit_id})
        else:
            ResearchWorkService.check_create_quota(tx, owner)
            child, child_rows = ResearchWorkService.frozen_rows(identity, frozen, binding)
        result = {'mode': body['mode'], 'id': child['id'], 'research_request_id': frozen['id'], 'created_at': child['created_at']}
        public.setdefault('continuations', []).append(result)
        public['updated_at'] = now()
        tx.put('lightning_audit', audit_id, owner, audit)
        tx.insert_many([*rows, *child_rows, ('lightning_continuation', retry_id, owner,
            {'checksum': checksum, 'result': result})])
        return deepcopy(result)
