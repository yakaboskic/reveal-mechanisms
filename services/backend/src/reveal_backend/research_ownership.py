"""Transfer research control records without transferring bearer authority."""
from copy import deepcopy

from .repository import digest, now


def transfer_workspace(tx, source, target):
    """Run within the same serialized transaction as the workspace transfer.

    Scientific payloads and original attribution remain immutable. Only current
    ownership, operation fences, and source-bound authorization indexes change.
    """
    from .research_work import TERMINAL
    timestamp = now()
    # Assessment indexes include the old owner in their digest. Retain the
    # immutable assessment itself, but never carry an unusable private cache or
    # idempotency namespace into the newly claimed workspace.
    for kind in ('cfde_assessment_cache', 'cfde_assessment_idempotency', 'lightning_audit_idempotency', 'lightning_continuation'):
        for row in tx.list(kind, source): tx.remove(kind, row['id'])
    for row in tx.list('lightning_audit', source):
        value = deepcopy(row['data']); public = value['public']
        value['control_owner'] = target
        if public['status'] in ('preparing', 'assessing'):
            public.update(status='interrupted', updated_at=timestamp, completed_at=timestamp,
                error={'code': 'WORKSPACE_TRANSFERRED', 'retryable': True,
                    'detail': 'This workspace moved to your registered account. Create another audit to try again.'})
        # Frozen scientific input and attribution stay exactly as accepted. The old attempt cannot commit.
        tx.put('lightning_audit', row['id'], source, value)
    from .user_inputs import INPUT_FIELDS
    for row in tx.list('cfde_assessment', source):
        value = deepcopy(row['data']); public = value.get('public') or {}
        inputs, composer = value.get('inputs') or {}, value.get('composer') or {}
        private = bool(inputs.get('uploads') or composer.get('upload_ids') or any(
            not isinstance(context.get(field, ''), str) or context.get(field, '').strip()
            for context in (inputs, composer) for field in INPUT_FIELDS))
        if public.get('status') in ('preparing', 'assessing') and private:
            public.update(status='interrupted', updated_at=timestamp, error={
                'code': 'WORKSPACE_TRANSFERRED', 'retryable': True,
                'detail': 'This workspace moved to your registered account. Request another assessment to try again.'})
            tx.put('cfde_assessment', row['id'], source, value)
        # Public-only leaders/followers may finish their accepted immutable
        # snapshot across a claim. Their shared service records are untouched.
    for row in tx.list('research_access', source):
        value = deepcopy(row['data'])
        value.update(revoked_at=value.get('revoked_at') or timestamp, revocation_reason='workspace_transferred')
        tx.put('research_access', row['id'], source, value)
    for row in tx.list('research_setup_ticket', source):
        value = deepcopy(row['data'])
        value.update(revoked_at=value.get('revoked_at') or timestamp, revocation_reason='workspace_transferred')
        tx.put('research_setup_ticket', row['id'], source, value)
    for kind in ('research_oauth_family', 'research_oauth_request', 'research_oauth_code', 'research_oauth_device'):
        for row in tx.list(kind, source):
            value = deepcopy(row['data'])
            value.update(revoked_at=value.get('revoked_at') or timestamp, revocation_reason='workspace_transferred')
            tx.put(kind, row['id'], source, value)
    for row in tx.list('local_work', source):
        value = deepcopy(row['data']); value['owner_user_id'] = target
        tx.put('local_work', row['id'], source, value)
    for row in tx.list('research_operation', source):
        value = deepcopy(row['data']); value['owner_user_id'] = target
        if value['state'] not in TERMINAL:
            value.pop('lease_token', None); value.pop('lease_until', None)
            if value['kind'] == 'prepare' and not value.get('grant_id'):
                value['state'] = 'received'
            else:
                value.update(state='failed', completed_at=timestamp, error={
                    'code':'WORKSPACE_TRANSFERRED',
                    'detail':'This workspace moved to your registered account. Reconnect and submit this operation again.'})
        tx.put('research_operation', row['id'], source, value)
    for row in tx.list('scientific_dependencies', source):
        value = deepcopy(row['data']); key = digest([target, value['object_id']])
        previous = tx.get('scientific_dependencies', key)
        if previous and previous['owner'] == target:
            value['borrowed'] = value.get('borrowed', False) or previous['data'].get('borrowed', False)
            for binding in previous['data'].get('bindings', []):
                if binding not in value['bindings']: value['bindings'].append(deepcopy(binding))
        tx.put('scientific_dependencies', key, target, value)
        tx.remove('scientific_dependencies', row['id'])
    # This is current authorization history, not a rewrite of receipt payloads.
    tx.put('scientific_owner_transition', digest(source), target,
           {'source':source, 'target':target, 'occurred_at':timestamp})
