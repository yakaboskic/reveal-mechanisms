"""Operator-configured credentials, each for one existing application user.

REVEAL_API_KEY_SHA256 / REVEAL_API_KEY_USER_ID hold the original single key. REVEAL_API_KEYS adds more, as
comma-separated `<sha256>:<user uuid>` entries, so a new holder never displaces an existing one.

The admin console can also mint keys (docs/api-keys.md, "Mint a key in the admin console"). Each is one application
record whose id is the key's SHA-256, so authentication reads one exact row and a key takes effect, or stops working
when revoked, without a deploy. The raw key is returned once, when it is minted, and never stored.
"""
import hashlib
import hmac
import os
import re
import secrets
from uuid import UUID

from .auth import Problem
from .repository import Conflict, now, uid

KIND, OWNER, FORMAT, VIEW_FORMAT = 'api_key', 'admin-console', 'reveal.api-key/1', 'reveal.api-keys/1'
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}')
MAX_ACTIVE = 100
TOKEN = re.compile(r'rvl_[A-Za-z0-9_-]{43}')


class KeyRequestError(ValueError):
    """A mint or revoke request the console can correct; the message is shown as sent."""


def configuration(environ=None):
    """Validate shape only; never provision a principal or expose configuration."""
    values = os.environ if environ is None else environ
    fingerprint = values.get('REVEAL_API_KEY_SHA256', '')
    user_id = values.get('REVEAL_API_KEY_USER_ID', '')
    if isinstance(fingerprint, str) and isinstance(user_id, str) and not fingerprint.strip() and not user_id.strip():
        return None
    try:
        valid = (isinstance(fingerprint, str) and re.fullmatch(r'[a-f0-9]{64}', fingerprint)
            and isinstance(user_id, str) and str(UUID(user_id)) == user_id)
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise Problem(503, 'API_KEY_CONFIGURATION_INVALID', 'API key authentication is unavailable.')
    return fingerprint, user_id


def additional(environ=None):
    """The REVEAL_API_KEYS entries; any malformed or duplicated entry disables key authentication (fail closed)."""
    values = os.environ if environ is None else environ
    raw = values.get('REVEAL_API_KEYS', '')
    if isinstance(raw, str) and not raw.strip(): return ()
    try:
        entries = []
        for item in raw.split(','):
            fingerprint, user_id = item.strip().split(':')
            if not re.fullmatch(r'[a-f0-9]{64}', fingerprint) or str(UUID(user_id)) != user_id: raise ValueError()
            entries.append((fingerprint, user_id))
        if len({fingerprint for fingerprint, _ in entries}) != len(entries): raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise Problem(503, 'API_KEY_CONFIGURATION_INVALID', 'API key authentication is unavailable.') from None
    return tuple(entries)


def keys(environ=None):
    """Every configured (fingerprint, user id); validates all of them, as readiness does."""
    single = configuration(environ)
    entries = ((single,) if single else ()) + additional(environ)
    if len({fingerprint for fingerprint, _ in entries}) != len(entries):
        raise Problem(503, 'API_KEY_CONFIGURATION_INVALID', 'API key authentication is unavailable.')
    return entries


def well_formed(token):
    return isinstance(token, str) and bool(TOKEN.fullmatch(token))


def authenticate(token, tx=None):
    """Return only the key's user ID; all application authority stays in RDS. Configured keys are checked first;
    with a transaction, a key minted in the admin console is then read by its SHA-256, and a revoked one is refused."""
    if not well_formed(token):
        raise Problem(401, 'INVALID_API_KEY', 'The API key is invalid or unavailable.')
    digest, owner = hashlib.sha256(token.encode()).hexdigest(), None
    for fingerprint, user_id in keys():   # compare against every key: no early exit on a match
        if hmac.compare_digest(digest, fingerprint): owner = user_id
    if owner is None and tx is not None:
        data = active(tx.get(KIND, digest))
        if data: owner = data['user_id']
    if owner is None:
        raise Problem(401, 'INVALID_API_KEY', 'The API key is invalid or unavailable.')
    return owner


def record(row):
    """A minted key's data, or None when the row is not one."""
    data = row.get('data') if isinstance(row, dict) else None
    return data if isinstance(data, dict) and data.get('format') == FORMAT and isinstance(data.get('user_id'), str) else None


def active(row):
    data = record(row)
    return data if data and not data.get('revoked_at') else None


def minted(tx):
    """Every minted key, newest first, with its record id (the key's SHA-256) and version."""
    rows = [(row, record(row)) for row in tx.list(KIND, OWNER)]
    return sorted(((row, data) for row, data in rows if data), key=lambda item: item[1].get('created_at', ''), reverse=True)


def agent_job_users(tx):
    """Workspaces whose active console key was minted with agent jobs allowed. The grant is the workspace's, so it
    holds for any credential of that workspace while such a key is active, and ends when the last one is revoked."""
    return {data['user_id'] for _, data in minted(tx) if not data.get('revoked_at') and data.get('agent_jobs') is True}


def minted_key(tx, authorization):
    """The active console-minted key this credential is, or None (a signed session or a configured key)."""
    token = authorization[7:] if isinstance(authorization, str) and authorization.startswith('Bearer ') else None
    if not well_formed(token): return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    if any(hmac.compare_digest(digest, fingerprint) for fingerprint, _ in keys()): return None
    return active(tx.get(KIND, digest))


def summary(data, principal=None):
    me = (principal or {}).get('data', {}).get('me', {}) if isinstance(principal, dict) else {}
    return {'key_id': data.get('key_id'), 'label': data.get('label'), 'prefix': data.get('prefix'),
            'user_id': data['user_id'], 'agent_jobs': data.get('agent_jobs') is True,
            'workspace': {'principal_kind': me.get('principal_kind'), 'display_name': me.get('display_name'),
                          'workspace_expires_at': me.get('workspace_expires_at'),
                          'retired': bool(principal and principal['data'].get('retired'))} if principal else None,
            'budget': data.get('budget'), 'budget_changed_at': data.get('budget_changed_at'),
            'budget_changed_by': data.get('budget_changed_by'),
            'created_at': data.get('created_at'), 'created_by': data.get('created_by'),
            'revoked_at': data.get('revoked_at'), 'revoked_by': data.get('revoked_by')}


def view(tx, environ=None):
    """The console's list: minted keys (no hash, no raw key) and, read-only, the keys configured in the environment."""
    entries = minted(tx)
    principals = tx.get_many('principal', sorted({data['user_id'] for _, data in entries})) if entries else {}
    environ = os.environ if environ is None else environ
    allowed = {value.strip() for value in environ.get('REVEAL_AGENT_JOB_USERS', '').split(',') if value.strip()}
    try:
        configured = [{'fingerprint': fingerprint[:12], 'user_id': user_id, 'agent_jobs': user_id in allowed}
                      for fingerprint, user_id in keys(environ)]
        problem = None
    except Problem as exc:
        configured, problem = [], exc.detail
    from .api_key_budget import status
    return {'format': VIEW_FORMAT,
            'keys': [{**summary(data, principals.get(data['user_id'])), 'usage': status(tx, data, data['user_id'])}
                     for _, data in entries],
            'environment': {'keys': configured, 'problem': problem}}


def usable_principal(row):
    if not row or row['data'].get('retired'):
        return False
    expiry = row['data'].get('me', {}).get('workspace_expires_at')
    return not expiry or expiry > now()


def mint(tx, body, actor, new_principal):
    """Mint one key for an existing workspace (user_id) or for a new registered service workspace named by the label.
    Returns the key's summary and the raw key, which nothing stores."""
    from .api_key_budget import BudgetError, parse
    if not isinstance(body, dict) or set(body) - {'label', 'user_id', 'agent_jobs', 'budget'}:
        raise KeyRequestError('Send a label, and optionally user_id, agent_jobs and budget.')
    try: budget = parse(body.get('budget'))
    except BudgetError as exc: raise KeyRequestError(str(exc)) from None
    label, user_id, agent_jobs = body.get('label'), body.get('user_id'), body.get('agent_jobs', False)
    if not isinstance(label, str) or not LABEL.fullmatch(label.strip()) or label != label.strip():
        raise KeyRequestError('The label must be 1 to 64 letters, digits, spaces, dots, underscores or hyphens, starting with a letter or digit.')
    if not isinstance(agent_jobs, bool):
        raise KeyRequestError('agent_jobs must be true or false.')
    entries = minted(tx)
    if sum(1 for _, data in entries if not data.get('revoked_at')) >= MAX_ACTIVE:
        raise KeyRequestError(f'There are already {MAX_ACTIVE} active keys. Revoke one first.')
    if user_id is not None:
        try:
            if not isinstance(user_id, str) or str(UUID(user_id)) != user_id: raise ValueError()
        except ValueError:
            raise KeyRequestError('user_id must be a workspace id: a lowercase UUID.') from None
        if not usable_principal(tx.get('principal', user_id)):
            raise KeyRequestError('No active workspace has that id.')
    else:
        me = new_principal(label)
        user_id = me['user_id']
        tx.insert('principal', user_id, user_id, {'me': me, 'retired': False, 'created_at': now(), 'created_by': actor,
                                                  'service_workspace': True})
    key = 'rvl_' + secrets.token_urlsafe(32)
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    if any(hmac.compare_digest(fingerprint, configured) for configured, _ in keys()) or tx.get(KIND, fingerprint):
        raise Conflict('Mint again.')  # a 256-bit collision; never expected
    data = {'format': FORMAT, 'key_id': uid(), 'label': label, 'prefix': key[:8], 'user_id': user_id,
            'agent_jobs': agent_jobs, 'budget': budget, 'created_at': now(), 'created_by': actor,
            'revoked_at': None, 'revoked_by': None}
    tx.insert(KIND, fingerprint, OWNER, data)
    from .api_key_budget import status
    return {'key': {**summary(data, tx.get('principal', user_id)), 'usage': status(tx, data, user_id)}, 'api_key': key}


def change_budget(tx, key_id, body, actor):
    """Set, change or remove (null) a minted key's budget. It applies to the next job the key starts."""
    from .api_key_budget import BudgetError, parse, status
    if not isinstance(body, dict) or set(body) != {'budget'}:
        raise KeyRequestError('Send {"budget": {"limit_usd": <dollars>, "period": "month" or "total"}}, or {"budget": null}.')
    try: budget = parse(body['budget'])
    except BudgetError as exc: raise KeyRequestError(str(exc)) from None
    for row, data in minted(tx):
        if data.get('key_id') == key_id:
            if data.get('revoked_at'): raise KeyRequestError('This key is revoked.')
            data = {**data, 'budget': budget, 'budget_changed_at': now(), 'budget_changed_by': actor}
            tx.put(KIND, row['id'], OWNER, data, expected=row['version'])
            return {'key': {**summary(data, tx.get('principal', data['user_id'])), 'usage': status(tx, data, data['user_id'])}}
    raise Problem(404, 'NOT_FOUND', 'No key has that id.')


def revoke(tx, key_id, actor):
    """Revoke one minted key by its key_id. Revoking a revoked key returns it unchanged."""
    for row, data in minted(tx):
        if data.get('key_id') == key_id:
            if not data.get('revoked_at'):
                data = {**data, 'revoked_at': now(), 'revoked_by': actor}
                tx.put(KIND, row['id'], OWNER, data, expected=row['version'])
            return {'key': summary(data, tx.get('principal', data['user_id']))}
    raise Problem(404, 'NOT_FOUND', 'No key has that id.')
