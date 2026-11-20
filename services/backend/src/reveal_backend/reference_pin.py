"""The environment's published reference release, read inside a caller's transaction.

Every reader of the <prefix>_ref_* tables first reads the one ref_release row on the same connection (REPEATABLE READ):
the publisher's RENAME waits on that metadata lock, so every later read of the transaction is of the same release.
A work, capture or account pinned to an earlier release is superseded: the tables hold only the current release, so
callers refuse it (research reads) or record it as a coverage gap (the account trace), never read the new rows as the old.
"""
from __future__ import annotations

from datetime import timezone
import json

from .auth import Problem
from .reference_generation import GENERATION_RE
from .repository import application_prefix, application_sql

RELEASE_SQL = 'SELECT release_id,published_at,manifest FROM reveal_ref_release'
NO_TABLE = 1146  # ER_NO_SUCH_TABLE: nothing was ever published to this environment
SUPERSEDED = 'reference_release_superseded'


def _utc_text(value):
    """A MySQL datetime as ISO-8601 UTC with a trailing Z, as catalog.utc_text writes it (MySQL sessions run in UTC); other
    values unchanged. Local: importing catalog here would be a cycle (catalog -> factor_details -> reference_pin)."""
    if hasattr(value, 'isoformat'):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return value


class ReleaseUnavailable(Exception):
    """The pinned release is not the environment's published release (`current` is that release, or None)."""
    def __init__(self, pinned, current):
        super().__init__(f'The pinned reference release {pinned} is not the published release {current}')
        self.pinned, self.current = pinned, current


def current_release(connection, prefix=None):
    """{release_id, published_at, manifest} of the environment's ref_release row, or None when nothing is published."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(application_sql(RELEASE_SQL, application_prefix(prefix)))
            rows = cursor.fetchall()
    except Exception as error:
        if getattr(error, 'args', None) and error.args[0] == NO_TABLE: return None
        raise
    if len(rows) != 1: return None
    release_id, published_at, manifest = rows[0]
    if isinstance(release_id, bytes): release_id = release_id.decode()
    if not isinstance(release_id, str) or not GENERATION_RE.fullmatch(release_id): raise ValueError('The published reference release id is malformed')
    manifest = json.loads(manifest) if isinstance(manifest, (str, bytes)) else manifest or {}
    return {'release_id': release_id, 'published_at': _utc_text(published_at), 'manifest': manifest}


def is_release(connection, identity, prefix=None):
    """Whether identity is the environment's published release (a release id rather than a reload-era generation)."""
    release = current_release(connection, prefix)
    return bool(release) and release['release_id'] == identity


def pin_release(connection, release_id, prefix=None):
    """The published release when it is release_id; ReleaseUnavailable otherwise (a later release replaced it)."""
    release = current_release(connection, prefix)
    if not release or release['release_id'] != release_id: raise ReleaseUnavailable(release_id, release and release['release_id'])
    return release


def superseded_problem(error):
    """The research reads' answer to a superseded pin: 409 SOURCE_UNAVAILABLE with reason reference_release_superseded."""
    return Problem(409, 'SOURCE_UNAVAILABLE', 'The pinned reference release is no longer published; start from the current release.',
                   reason=SUPERSEDED, pinned_release_id=error.pinned, current_release_id=error.current)
