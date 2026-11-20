"""Operator-configured credentials, each for one existing application user.

REVEAL_API_KEY_SHA256 / REVEAL_API_KEY_USER_ID hold the original single key. REVEAL_API_KEYS adds more, as
comma-separated `<sha256>:<user uuid>` entries, so a new holder never displaces an existing one.
"""
import hashlib
import hmac
import os
import re
from uuid import UUID

from .auth import Problem


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


def authenticate(token):
    """Return only the configured user ID; all application authority stays in RDS."""
    if not isinstance(token, str) or not re.fullmatch(r'rvl_[A-Za-z0-9_-]{43}', token):
        raise Problem(401, 'INVALID_API_KEY', 'The API key is invalid or unavailable.')
    digest, owner = hashlib.sha256(token.encode()).hexdigest(), None
    for fingerprint, user_id in keys():   # compare against every key: no early exit on a match
        if hmac.compare_digest(digest, fingerprint): owner = user_id
    if owner is None:
        raise Problem(401, 'INVALID_API_KEY', 'The API key is invalid or unavailable.')
    return owner
