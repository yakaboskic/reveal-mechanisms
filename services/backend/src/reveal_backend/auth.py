"""Application credentials resolve existing principals; payloads never choose owners."""
import hmac
import os
import time
import jwt
from .repository import now

_UNREAD = object()

class Problem(Exception):
    def __init__(self, status, code, detail, **extra):
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

def decode_assertion(token, purpose=None):
    secret = os.getenv('REVEAL_GATEWAY_SECRET', '')
    if len(secret) < 32: raise Problem(503, 'GATEWAY_NOT_CONFIGURED', 'Gateway signing configuration is missing.')
    try:
        claims = jwt.decode(token, secret, algorithms=['HS256'], audience=os.getenv('REVEAL_GATEWAY_AUDIENCE', 'reveal-api'),
            issuer=os.getenv('REVEAL_GATEWAY_ISSUER', 'reveal-nextjs'), options={'require': ['exp', 'iat', 'jti']})
        if claims['exp'] - claims['iat'] > 300 or claims['iat'] > time.time()+10: raise ValueError()
        if purpose and claims.get('purpose') != purpose: raise ValueError()
        if not purpose and claims.get('purpose'): raise ValueError()
        return claims
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
        raise Problem(401, 'INVALID_IDENTITY_PROOF', 'The signed session is invalid or expired.') from None

def service_authority(authorization):
    expected = os.getenv('REVEAL_GATEWAY_SERVICE_TOKEN', '')
    if len(expected) < 32 or not hmac.compare_digest(authorization or '', 'Bearer '+expected):
        raise Problem(403, 'SERVICE_IDENTITY_REQUIRED', 'A trusted gateway service credential is required.')

def _credential(authorization):
    """The principal id a credential names, and its claims; no database access."""
    if not authorization or not authorization.startswith('Bearer ') or not authorization[7:]:
        raise Problem(401, 'SESSION_EXPIRED', 'Continue with a registered or anonymous session.')
    token = authorization[7:]
    if token.startswith('rvl_'):
        from .api_keys import authenticate
        return authenticate(token), None
    claims = decode_assertion(token)
    return claims.get('sub', ''), claims

def _accept(row, claims):
    if not row or row['data'].get('retired') or (row['data']['me']['workspace_expires_at'] and row['data']['me']['workspace_expires_at'] <= now()):
        raise Problem(401, 'SESSION_EXPIRED', 'This workspace session is expired or retired.')
    me = row['data']['me']
    if claims is not None and claims.get('principal_kind') != me['principal_kind']:
        raise Problem(401, 'SESSION_EXPIRED', 'Refresh the workspace session.')
    return me

def principal(tx, authorization):
    identity, claims = _credential(authorization)
    return _accept(tx.get('principal', identity), claims)

def principal_with(tx, authorization, keys):
    """principal() and exact extra keys in one read. The principal is accepted first, so an invalid session
    still fails 401 before any 404; the extra rows are NOT authorized: pass each through require_owned."""
    identity, claims = _credential(authorization)
    rows = tx.get_records((('principal', identity), *keys))
    return _accept(rows.get(('principal', identity)), claims), rows


def credential_expiry(authorization):
    """Credential-only bound; event replay independently rechecks its RDS user."""
    if not authorization or not authorization.startswith('Bearer ') or not authorization[7:]:
        raise Problem(401, 'SESSION_EXPIRED', 'Continue with a registered or anonymous session.')
    token = authorization[7:]
    if token.startswith('rvl_'):
        from .api_keys import authenticate
        authenticate(token)
        return None  # Opaque keys use the ordinary bounded SSE renewal window.
    return decode_assertion(token)['exp']

def publication_principal(tx, authorization, visibility):
    me = principal(tx, authorization)
    if visibility == 'public' and me['principal_kind'] != 'registered':
        raise Problem(403, 'SIGN_IN_REQUIRED', 'Sign in to publish a scientific account or exploration.')
    return me

def require_owned(tx, kind, identity, user, row, *, dependency=_UNREAD):
    """Authorize one exact row from this transaction, including batched reads."""
    from .repository import digest
    shared = False
    if row and row['owner'] != user and kind in ('object','account','paragraph','citation'):
        target = identity.rsplit(':',1)[0] if kind=='citation' else identity
        grant = tx.get('grant',digest([user,target])); shared = bool(grant and grant['owner']==user)
    if row is None or (row['owner'] != user and not shared):
        raise Problem(404, 'NOT_FOUND', 'The requested resource is unavailable.')
    if kind in ('object', 'account', 'paragraph', 'citation', 'artifact'):
        from .scientific_reuse import authorize_object
        target = row['data'].get('file', {}).get('id') if kind == 'artifact' else identity
        if kind == 'citation': target = row['data'].get('target_id', identity.rsplit(':', 1)[0])
        if target:
            if dependency is _UNREAD: authorize_object(tx, user, target)
            else: authorize_object(tx, user, target, dependency=dependency)
    elif kind == 'scientific_document':
        from .scientific_reuse import authorize_document
        authorize_document(tx, user, row['data'].get('document', {}))
    return row

def owned(tx, kind, identity, user):
    from .repository import digest
    key = digest([user,identity]) if kind in ('object','account','paragraph') else identity
    if kind in ('object','account','paragraph') and hasattr(tx, 'get_records'):
        records = tx.get_records(((kind, key), ('scientific_dependencies', key)))
        return require_owned(tx, kind, identity, user, records.get((kind, key)),
                             dependency=records.get(('scientific_dependencies', key)))
    return require_owned(tx, kind, identity, user, tx.get(kind, key))
