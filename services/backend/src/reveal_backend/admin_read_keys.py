"""Separate operator credential for the four cross-owner scientific GET routes.

This credential is never an application principal or gateway authority. Its
scope is fixed in code; configuration and callers cannot broaden it.
"""
import hashlib
import hmac
import os
import re
from uuid import UUID

from .auth import Problem

SCOPE = 'science:read'
SETTINGS = ('REVEAL_ADMIN_READ_API_KEY_SHA256', 'REVEAL_ADMIN_READ_API_KEY_ID')


def configuration(environ=None):
    """Validate the hash/id pair without creating or looking up an identity."""
    values = os.environ if environ is None else environ
    fingerprint, key_id = (values.get(name, '') for name in SETTINGS)
    if (isinstance(fingerprint, str) and isinstance(key_id, str)
            and not fingerprint.strip() and not key_id.strip()):
        return None
    try:
        valid = (isinstance(fingerprint, str) and re.fullmatch(r'[a-f0-9]{64}', fingerprint)
                 and isinstance(key_id, str) and str(UUID(key_id)) == key_id)
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise Problem(503, 'ADMIN_READ_API_KEY_CONFIGURATION_INVALID',
                      'Administrative scientific access is unavailable.')
    return fingerprint, key_id


def authenticate(authorization):
    """Return the nonsecret credential ID, never an owner or user identity."""
    if not isinstance(authorization, str) or not re.fullmatch(
            r'Bearer rvl_admin_[A-Za-z0-9_-]{43}', authorization):
        raise Problem(401, 'INVALID_ADMIN_READ_API_KEY',
                      'An administrative scientific read key is required.')
    configured = configuration()
    fingerprint = hashlib.sha256(authorization[7:].encode()).hexdigest()
    if configured is None or not hmac.compare_digest(fingerprint, configured[0]):
        raise Problem(401, 'INVALID_ADMIN_READ_API_KEY',
                      'An administrative scientific read key is required.')
    return configured[1]
