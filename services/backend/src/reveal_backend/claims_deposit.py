"""Deposit an accepted account in the claims repository.

A scientific_account agent job ends by depositing its whole account: every object in it, in one request, to the
claims repository's POST /v1/deposits. The repository stores each object under its DAPPER id (an object it already
holds is left as stored) and records which key deposited what.

Two settings turn this on, and both live in the backend's environment only:

  REVEAL_CLAIMS_API_URL   base address of the claims repository, e.g. https://claims.example.org
  REVEAL_CLAIMS_API_KEY   a deposit key minted in the claims repository's admin console

The key identifies REVEAL as the depositor. It is never written to a job, a record, a log or the sandbox: the sandbox
receives only the credentials box_adapter names, and this is not one of them.

A deposit names the DAPPER release its document is in (`dapper_version`); the repository accepts only the release it
vendors, and refuses (400) a deposit that names none or another.

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


def address(url):
    """The base url when it is safe to call (https, or plain http to this machine), else None. Never raises."""
    try:
        url = (url or '').strip().rstrip('/')
        if not url:
            return None
        parsed = urllib.parse.urlsplit(url)
        parsed.port   # raises on a port that is not a number
        secure = parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in LOCAL_HOSTS)
        if not secure or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            return None
        return url
    except ValueError:
        return None


def configuration():
    """(base url, key) when deposits are configured and the address is safe to send a key to, else None. A malformed
    address is not a configuration; it never raises."""
    url, key = address(setting('REVEAL_CLAIMS_API_URL', '')), setting('REVEAL_CLAIMS_API_KEY', '').strip()
    return (url, key) if url and key else None


def _detail(raw):
    """The repository's own reason for a refusal, bounded. It describes the document, which the requester owns."""
    try:
        detail = json.loads(raw).get('detail')
    except (ValueError, AttributeError):
        return None
    return detail[:500] if isinstance(detail, str) else None


def _receipt(body):
    """The deposit block of a receipt. The repository reports, without refusing, an object it already held with
    different unhashed content (its stored copy stands) and a reference the deposit left unresolved. Either means the
    repository does not hold the account as sent, so the deposit is `failed` with reason `inconsistent`: it is kept
    with its deposit id and counts, and the account is served from the job, never rebuilt from the repository."""
    if not isinstance(body, dict) or not isinstance(body.get('deposit_id'), str) or not isinstance(body.get('deposited'), list):
        raise ValueError('The answer is not a deposit receipt')
    deposited = body['deposited']
    unresolved = body.get('unresolved_references') if isinstance(body.get('unresolved_references'), list) else []
    counts = {'deposit_id': body['deposit_id'], 'objects': len(deposited),
              # An object the repository already held under the same id with different unhashed content: its stored copy stands.
              'stored_differs': sum(1 for item in deposited if isinstance(item, dict) and item.get('stored_differs')),
              'unresolved_references': len(unresolved)}
    collection = body.get('collection')
    if isinstance(collection, dict) and isinstance(collection.get('key'), str):
        counts['collection'] = collection['key']
        if isinstance(collection.get('release'), str):
            counts['release'] = collection['release']
    if counts['stored_differs'] or counts['unresolved_references']:
        return {'status': 'failed', 'reason': 'inconsistent', **counts}
    return {'status': 'deposited', **counts}


def send(document_bytes, *, dapper_version=None, opener=None, configured=None):
    """Deposit one account document (its canonical JSON bytes), in DAPPER `dapper_version` (by default the release
    account_deposit deposits in; pass the version recorded with the account). Returns the outcome; never raises.

    {'status': 'deposited', 'deposit_id', 'objects', 'stored_differs': 0, 'unresolved_references': 0, 'collection'?, 'release'?}
    {'status': 'failed', 'reason': 'inconsistent', the same counts}    the repository took it but holds it otherwise
    {'status': 'not_configured'}
    {'status': 'failed', 'reason': 'too_large' | 'rejected' | 'unauthorized' | 'unavailable', 'http_status'?, 'detail'?}
    """
    try:
        configured = configuration() if configured is None else configured
        if not configured:
            return {'status': 'not_configured'}
        url, key = configured
        if dapper_version is None:
            from .account_deposit import DAPPER_VERSION as dapper_version
        body = b'{"dapper_version":' + json.dumps(dapper_version).encode() + b',"document":' + document_bytes + b'}'
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
