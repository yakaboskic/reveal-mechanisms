"""One optional operator-configured credential for one existing application user."""
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


def authenticate(token):
    """Return only the configured user ID; all application authority stays in RDS."""
    if not isinstance(token, str) or not re.fullmatch(r'rvl_[A-Za-z0-9_-]{43}', token):
        raise Problem(401, 'INVALID_API_KEY', 'The API key is invalid or unavailable.')
    configured = configuration()
    if configured is None or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), configured[0]):
        raise Problem(401, 'INVALID_API_KEY', 'The API key is invalid or unavailable.')
    return configured[1]
