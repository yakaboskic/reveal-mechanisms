"""Read a deposited account back from the claims repository.

A scientific_account job deposits its account in one request (claims_deposit) and REVEAL keeps no copy of a
deposited document. GET /v1/jobs/{id}/account rebuilds it from the repository's public read routes:

  1. GET  /v1/deposits/{deposit_id}   every object id and class the deposit carried, and its edges in stored order;
  2. POST /v1/objects:batchGet        the objects, at most 500 ids a call;
  3. the document is laid out as REVEAL laid it out before sending (account_deposit.layout: each node group by id,
     edges in their order), and serialized as it was sent (evidence_package.canonical_json).

The caller checks the rebuilt bytes against the job's record: the sha256 of the document that was sent, and the
account id. The repository stores a node without the `class_name` it presents it with (and an object stored before
DAPPER-ID-1 may carry `identity_profile`), so those keys are dropped.

The read routes take no key, and none is sent: REVEAL's deposit key goes only to POST /v1/deposits. Only the base
address is needed (REVEAL_CLAIMS_API_URL, the same safety rules as a deposit).

A deposit never changes, so a rebuilt document is cached in this process, keyed by deposit id, for a short time
(REVEAL_CLAIMS_READ_CACHE_SECONDS, default 300; 0 turns the cache off) and within a bounded size. Failures are not
cached. Phase 7's catalog reads are to reuse this cache.
"""
from collections import OrderedDict
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .runtime_config import setting

BATCH = 500                         # the repository's objects:batchGet limit
TIMEOUT_SECONDS = 20
MAX_BODY_BYTES = 32 * 1024 * 1024   # a deposit is at most 16 MB of documents; their presentation adds a little
DEPOSIT_ID = re.compile(r'^dep_[0-9a-f]{32}$')
# Keys the repository presents or once stored around a node that are not DAPPER content (its `_plain`): an object it
# already held in that envelope is the same object, so the deposit's receipt is clean and the rebuild drops them.
ENVELOPE = ('class_name', 'identity_profile')
CACHE_ENTRIES = 64
CACHE_BYTES = 64 * 1024 * 1024


class Unavailable(RuntimeError):
    """The claims repository could not be read: not configured here, unreachable, or an answer that is not what its
    read routes return. Nothing is known about the account."""


class Mismatch(RuntimeError):
    """The repository answered, but what it holds does not rebuild the account that was sent. `reason` is one of
    deposit_id, missing_objects, unknown_class, account_id or sha256."""
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def base_url():
    from .claims_deposit import address
    url = address(setting('REVEAL_CLAIMS_API_URL', ''))
    if not url:
        raise Unavailable('This deployment has no claims repository address to read the account from')
    return url


def build_opener():
    """The HTTP opener reads go through; a redirect is refused, as for a deposit. Tests replace it."""
    from .claims_deposit import NoRedirect
    return urllib.request.build_opener(NoRedirect)


def _call(opener, url, body=None):
    opener = opener or build_opener()
    headers = {'Accept': 'application/json', 'User-Agent': 'reveal-mechanisms-backend'}
    data = None
    if body is not None:
        data = json.dumps(body).encode(); headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=data, method='POST' if body is not None else 'GET', headers=headers)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise Unavailable(f'The claims repository answered HTTP {error.code}') from None
    except Exception as error:
        raise Unavailable(f'The claims repository could not be reached ({type(error).__name__})') from None
    if len(raw) > MAX_BODY_BYTES:
        raise Unavailable('The claims repository answer is larger than any account')
    try:
        return json.loads(raw)
    except ValueError:
        raise Unavailable('The claims repository answer is not JSON') from None


def rebuild(deposit_id, *, opener=None, url=None):
    """The deposit's document, as the canonical bytes REVEAL sent. Raises Unavailable or Mismatch."""
    from .account_deposit import edge_group, layout, node_groups
    from .evidence_package import canonical_json
    if not isinstance(deposit_id, str) or not DEPOSIT_ID.match(deposit_id):
        raise Mismatch('deposit_id', 'The job records no valid deposit id')
    url = url or base_url()
    deposit = _call(opener, f'{url}/v1/deposits/{urllib.parse.quote(deposit_id, safe="")}')
    if (not isinstance(deposit, dict) or deposit.get('deposit_id') != deposit_id
            or not isinstance(deposit.get('objects'), list) or not isinstance(deposit.get('edges'), list)):
        raise Unavailable('The claims repository did not answer with the deposit')
    groups = node_groups()
    classes = {}
    for item in deposit['objects']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            raise Unavailable('The claims repository listed a deposit object without an id')
        if item.get('class_name') not in groups:
            raise Mismatch('unknown_class', f"{item['id'][:140]} is not stored as a DAPPER class the account form uses")
        classes[item['id']] = item['class_name']
    ids = sorted(classes)
    found = {}
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        answer = _call(opener, f'{url}/v1/objects:batchGet', {'ids': chunk})
        if not isinstance(answer, dict) or not isinstance(answer.get('objects'), dict) or not isinstance(answer.get('missing', []), list):
            raise Unavailable('The claims repository did not answer with objects')
        if answer.get('missing'):
            raise Mismatch('missing_objects', f"The claims repository no longer holds {len(answer['missing'])} of the deposit's objects")
        for identity in chunk:
            node = answer['objects'].get(identity)
            if not isinstance(node, dict) or node.get('id') != identity:
                raise Mismatch('missing_objects', f'The claims repository did not return {identity[:140]}')
            found[identity] = {slot: value for slot, value in node.items() if slot not in ENVELOPE}
    document = {}
    for identity in ids:
        document.setdefault(groups[classes[identity]], []).append(found[identity])
    for edge in deposit['edges']:
        if not isinstance(edge, dict) or not isinstance(edge.get('class_name'), str):
            raise Unavailable('The claims repository listed an edge without its class')
        document.setdefault(edge_group(edge['class_name']), []).append({key: value for key, value in edge.items() if key != 'class_name'})
    return canonical_json(layout(document))


class Cache:
    """Rebuilt documents by deposit id: least recently used first out, each kept `ttl` seconds, at most `entries`
    documents and `size` bytes in all. Thread-safe; a miss is fetched outside the lock."""
    def __init__(self, ttl, entries=CACHE_ENTRIES, size=CACHE_BYTES, clock=time.monotonic):
        self.ttl, self.entries, self.size, self.clock = ttl, entries, size, clock
        self.items, self.lock = OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.items.get(key)
            if entry is None:
                return None
            if entry[0] <= self.clock():
                del self.items[key]
                return None
            self.items.move_to_end(key)
            return entry[1]

    def put(self, key, data):
        if self.ttl <= 0 or len(data) > self.size:
            return
        with self.lock:
            self.items.pop(key, None)
            self.items[key] = (self.clock() + self.ttl, data)
            while len(self.items) > self.entries or sum(len(value) for _, value in self.items.values()) > self.size:
                self.items.popitem(last=False)


def _ttl():
    try:
        return max(0.0, float(setting('REVEAL_CLAIMS_READ_CACHE_SECONDS', '300')))
    except ValueError:
        return 300.0


_cache = None
_cache_lock = threading.Lock()


def cache():
    global _cache
    with _cache_lock:
        if _cache is None or _cache.ttl != _ttl():
            _cache = Cache(_ttl())
        return _cache


def account_document(deposit_id, account_id, document_sha256, *, opener=None):
    """(the deposited document, as a dict) once its rebuilt bytes hash to `document_sha256` and its one
    ScientificAccount is `account_id`. Read through the cache; checked on every call, cached or not."""
    from .evidence_package import decode, sha256
    data = cache().get(deposit_id)
    fetched = data is None
    if fetched:
        data = rebuild(deposit_id, opener=opener)
    document = decode(data)
    accounts = document.get('scientific_accounts') or []
    if [node.get('id') for node in accounts] != [account_id]:
        raise Mismatch('account_id', "The deposit's ScientificAccount is not the job's account")
    if sha256(data) != document_sha256:
        raise Mismatch('sha256', 'The document rebuilt from the claims repository differs from the one the job deposited')
    if fetched:   # only a document that passed both checks is kept
        cache().put(deposit_id, data)
    return document
