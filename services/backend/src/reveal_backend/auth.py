"""Gateway assertions are credentials; research payloads never choose owners."""
import hmac
import os
import time
import jwt
from .repository import now

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

def principal(tx, authorization):
    if not authorization or not authorization.startswith('Bearer '):
        raise Problem(401, 'SESSION_EXPIRED', 'Continue with a registered or anonymous session.')
    claims = decode_assertion(authorization[7:])
    row = tx.get('principal', claims.get('sub', ''))
    if not row or row['data'].get('retired') or (row['data']['me']['workspace_expires_at'] and row['data']['me']['workspace_expires_at'] <= now()):
        raise Problem(401, 'SESSION_EXPIRED', 'This workspace session is expired or retired.')
    me = row['data']['me']
    if claims.get('principal_kind') != me['principal_kind']:
        raise Problem(401, 'SESSION_EXPIRED', 'Refresh the workspace session.')
    return me

def owned(tx, kind, identity, user):
    from .repository import digest
    key = digest([user,identity]) if kind in ('object','account','paragraph') else identity
    row = tx.get(kind, key)
    shared = False
    if row and kind in ('object','account','paragraph','citation'):
        target = identity.rsplit(':',1)[0] if kind=='citation' else identity
        grant = tx.get('grant',digest([user,target])); shared = bool(grant and grant['owner']==user)
    if row is None or (row['owner'] != user and not shared):
        raise Problem(404, 'NOT_FOUND', 'The requested resource is unavailable.')
    return row
