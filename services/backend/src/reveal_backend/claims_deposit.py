"""Deposit an accepted account in the claims repository.

A scientific_account agent job ends by depositing its whole account: every object in it, in one request, to the
claims repository's POST /v1/deposits. The repository stores each object under its DAPPER id (an object it already
holds is left as stored) and records which key deposited what.

Two settings turn this on, and both live in the backend's environment only:

  REVEAL_CLAIMS_API_URL   base address of the claims repository, e.g. https://claims.example.org
  REVEAL_CLAIMS_API_KEY   a deposit key minted in the claims repository's admin console

The key identifies REVEAL as the depositor. It is never written to a job, a record, a log or the sandbox: the sandbox
receives only the credentials box_adapter names, and this is not one of them.

A deposit is an attempt. `send` never raises: the job has already produced and stored its account, and whether the
repository took a copy is reported beside it, not allowed to undo it.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from .runtime_config import setting

# The repository refuses a larger body; an account that large is not sent.
MAX_BODY_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 30
ATTEMPTS = 2
LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is refused, never followed: urllib would repeat the request, key header included, at whatever
    address the answer names, and would turn the POST into a GET whose answer is not a receipt."""
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def configuration():
    """(base url, key) when deposits are configured and the address is safe to send a key to, else None. A malformed
    address is not a configuration; it never raises."""
    try:
        url, key = setting('REVEAL_CLAIMS_API_URL', '').strip().rstrip('/'), setting('REVEAL_CLAIMS_API_KEY', '').strip()
        if not url or not key:
            return None
        parsed = urllib.parse.urlsplit(url)
        parsed.port   # raises on a port that is not a number
        secure = parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in LOCAL_HOSTS)
        if not secure or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            return None
        return url, key
    except ValueError:
        return None


def _detail(raw):
    """The repository's own reason for a refusal, bounded. It describes the document, which the requester owns."""
    try:
        detail = json.loads(raw).get('detail')
    except (ValueError, AttributeError):
        return None
    return detail[:500] if isinstance(detail, str) else None


def _receipt(body):
    if not isinstance(body, dict) or not isinstance(body.get('deposit_id'), str) or not isinstance(body.get('deposited'), list):
        raise ValueError('The answer is not a deposit receipt')
    deposited = body['deposited']
    unresolved = body.get('unresolved_references') if isinstance(body.get('unresolved_references'), list) else []
    return {'status': 'deposited', 'deposit_id': body['deposit_id'],
            'objects': len(deposited),
            # An object the repository already held under the same id with different unhashed content: its stored copy stands.
            'stored_differs': sum(1 for item in deposited if isinstance(item, dict) and item.get('stored_differs')),
            'unresolved_references': len(unresolved)}


def send(document_bytes, *, opener=None, configured=None):
    """Deposit one account document (its canonical JSON bytes). Returns the outcome; never raises.

    {'status': 'deposited', 'deposit_id', 'objects', 'stored_differs', 'unresolved_references'}
    {'status': 'not_configured'}
    {'status': 'failed', 'reason': 'too_large' | 'rejected' | 'unauthorized' | 'unavailable', 'http_status'?, 'detail'?}
    """
    try:
        configured = configuration() if configured is None else configured
        if not configured:
            return {'status': 'not_configured'}
        url, key = configured
        body = b'{"document":' + document_bytes + b'}'
        if len(body) > MAX_BODY_BYTES:
            return {'status': 'failed', 'reason': 'too_large'}
        opener = opener or urllib.request.build_opener(NoRedirect)
        outcome = {'status': 'failed', 'reason': 'unavailable'}
        for _ in range(ATTEMPTS):
            request = urllib.request.Request(url + '/v1/deposits', data=body, method='POST', headers={
                'Content-Type': 'application/json', 'Accept': 'application/json', 'X-API-Key': key,
                'User-Agent': 'reveal-mechanisms-backend'})
            try:
                with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                    return _receipt(json.loads(response.read(MAX_BODY_BYTES)))
            except urllib.error.HTTPError as error:
                status = error.code
                if status in (401, 403):
                    return {'status': 'failed', 'reason': 'unauthorized', 'http_status': status}
                if 300 <= status < 400:   # the address is wrong; the key was sent once, to the configured host only
                    return {'status': 'failed', 'reason': 'rejected', 'http_status': status}
                if 400 <= status < 500 and status != 429:
                    detail = _detail(error.read(65536))
                    return {'status': 'failed', 'reason': 'rejected', 'http_status': status, **({'detail': detail} if detail else {})}
                outcome = {'status': 'failed', 'reason': 'unavailable', 'http_status': status}
            except Exception:   # unreachable, timed out, or an answer that is not a receipt: the same deposit is safe to repeat
                outcome = {'status': 'failed', 'reason': 'unavailable'}
        return outcome
    except Exception:
        return {'status': 'failed', 'reason': 'unavailable'}
