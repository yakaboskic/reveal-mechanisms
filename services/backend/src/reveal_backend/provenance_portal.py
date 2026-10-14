"""Bottom-line GWAS lineage from the Translator artifact-provenance portal, fetched on the serving loop: no files, no Redis.

REVEAL_PROVENANCE_PORTAL_URL is the portal's base URL (deploy/dig/service.yaml, scripts/durable_deployment.py). Unset, the portal
is disabled: nothing is fetched and the account trace reports each trait's GWAS route as external_provenance_unavailable. Links
are built from ids (the public portal is their default base); only bottom_line() fetches.

bottom_line(artifact_id) reads <portal>/get_provenance?id=<artifact>: one httpx.AsyncClient, Semaphore(4) and in-flight map per
running loop (created lazily; aclose() in the app lifespan closes it), a 10 s timeout (3 s to connect), no redirects, the body
streamed into memory and capped at 2 MiB, one retry on a transport error, 5xx or 429 (Retry-After capped to the fetch's remaining
budget); the whole fetch, slot wait and body included, ends within FETCH_BUDGET_SECONDS. The document is parsed in memory
(json.loads, NaN -> None) in a worker thread and compacted:

  R           the subject of the has_drs_object edge whose DrsObject drs_id is the artifact: exactly one Dataset, else invalid
  raw inputs  the C2M2Files never the subject of was_generated_by and reachable from R over was_generated_by and used

The trace adds trait -reveal:publishedGwas-> R, the only traversed edge into this graph; each raw input hangs off R by the
non-traversed reveal:portalTransitiveInput with its path length, the portal URL and the document's sha256 (T2D Mixed: 74 nodes of
749). A compacted graph's sha256 is that of its canonical JSON. Outcomes are cached in a byte-bounded LRU (64 MB): a graph is
fresh for 24 h, then served only when a refetch fails, for up to 7 days; a 404 or an invalid document is cached for 5 minutes;
timeouts, 5xx and 429 never are. A fetch is shared by every request waiting for it and shielded, so one request's budget never
cancels another's fetch. Collection YAML is never fetched: gene-set lineage is local and the portal is only a link-out for it.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
import threading
import time
from urllib.parse import quote
import weakref

import httpx

PORTAL_ENV = 'REVEAL_PROVENANCE_PORTAL_URL'
PUBLIC_PORTAL = 'https://translator.broadinstitute.org/artifact_provenance'
MAX_BYTES = 2 << 20
CONCURRENCY = 4
TIMEOUT_SECONDS, CONNECT_SECONDS = 10, 3
FETCH_BUDGET_SECONDS = 15  # one fetch, its retry included
RETRY_SECONDS = 0.5        # the retry's delay when the portal sends no Retry-After
CACHE_BYTES = 64 << 20
FRESH_SECONDS, STALE_SECONDS, NEGATIVE_SECONDS = 24 * 3600, 7 * 24 * 3600, 300
NODE_KEYS = ('name', 'description', 'version', 'access_level', 'filename')
HEADERS = {'Accept': 'application/json', 'User-Agent': 'REVEAL-provenance/1'}


@dataclass(frozen=True)
class Outcome:
    """status ok | not_found | unavailable | invalid. graph (ok) is shared by every request: read it, never change it."""
    status: str
    artifact_id: str
    portal_url: str | None = None
    graph: dict | None = None
    sha256: str | None = None
    stale: bool = False
    reason: str | None = None


def portal_url():
    """The configured portal base URL, or None when fetching is disabled."""
    return (os.getenv(PORTAL_ENV) or '').strip().rstrip('/') or None


def artifact_href(artifact_id, base=None):
    return f'{base or portal_url() or PUBLIC_PORTAL}/artifact/{quote(artifact_id, safe="")}'


def bottom_line_href(portal_trait_id, base=None):
    return f'{base or portal_url() or PUBLIC_PORTAL}/bottom_line/{quote(portal_trait_id, safe="")}'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def _refs(value):
    return [item for item in (value if isinstance(value, list) else [value]) if isinstance(item, str) and item]


def compact(document, artifact_id, *, base=None, portal=None, document_sha256=None):
    """{artifact_id, portal_url, document_sha256, dataset, raw_inputs} of a bottom-line document, or None when it does not
    name exactly one Dataset for the artifact. Relations may be edge records or node fields."""
    provenance = document.get('provenance') if isinstance(document, dict) else None
    if not isinstance(provenance, dict): return None
    nodes = {}
    for group, rows in provenance.items():
        if group.endswith('_edges') or not isinstance(rows, list): continue
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get('id'), str): nodes.setdefault(row['id'], (group, row))
    out = defaultdict(lambda: defaultdict(list))
    for identity, (_, row) in nodes.items():
        for relation in ('has_drs_object', 'was_generated_by', 'used'):
            out[relation][identity] += _refs(row.get(relation))
    for relation in ('has_drs_object', 'was_generated_by', 'used'):
        for edge in provenance.get(relation + '_edges') or ():
            if isinstance(edge, dict) and isinstance(edge.get('subject'), str) and isinstance(edge.get('object'), str):
                out[relation][edge['subject']].append(edge['object'])
    drs = {identity for identity, (group, row) in nodes.items() if group == 'drs_objects' and row.get('drs_id') == artifact_id}
    roots = {subject for subject, targets in out['has_drs_object'].items() if drs & set(targets)}
    if len(roots) != 1: return None
    root = roots.pop()
    if nodes.get(root, ('',))[0] != 'datasets': return None
    generated = {subject for subject, targets in out['was_generated_by'].items() if targets}
    depth, pending = {root: 0}, deque([root])
    while pending:
        identity = pending.popleft()
        for target in out['was_generated_by'].get(identity, []) + out['used'].get(identity, []):
            if target not in depth: depth[target] = depth[identity] + 1; pending.append(target)
    href = artifact_href(artifact_id, base)
    def record(identity):
        row = nodes[identity][1]
        return {'id': identity, **{key: row[key] for key in NODE_KEYS if isinstance(row.get(key), str)}, 'href': href}
    raw = sorted(identity for identity in depth if nodes.get(identity, ('',))[0] == 'c2m2_files' and identity not in generated)
    return {'artifact_id': artifact_id, 'portal_url': portal, 'document_sha256': document_sha256, 'dataset': record(root),
            'raw_inputs': [{**record(identity), 'path_length': depth[identity]} for identity in raw]}


def _parse(body, artifact_id, base, portal):
    try: document = json.loads(body, parse_constant=lambda _: None)
    except (ValueError, UnicodeError): return Outcome('invalid', artifact_id, portal, reason='malformed_document')
    graph = compact(document, artifact_id, base=base, portal=portal, document_sha256=hashlib.sha256(body).hexdigest())
    if graph is None: return Outcome('invalid', artifact_id, portal, reason='no_single_artifact_dataset')
    return Outcome('ok', artifact_id, portal, graph, hashlib.sha256(canonical(graph)).hexdigest())


def trait_lineage(trait_id, outcome):
    """The document groups an ok outcome adds to the trace: R, its raw inputs, trait -publishedGwas-> R and
    R -portalTransitiveInput-> each raw input (with path_length, portal_url and document_sha256 as observations)."""
    graph = outcome.graph
    root = graph['dataset']['id']
    source = {'kind': 'portal_bottom_line', 'portal_url': outcome.portal_url, 'sha256': outcome.sha256}
    return {'datasets': [dict(graph['dataset'])],
            'c2m2_files': [{key: value for key, value in node.items() if key != 'path_length'} for node in graph['raw_inputs']],
            'published_gwas_edges': [{'subject': trait_id, 'predicate': 'reveal:publishedGwas', 'object': root,
                                      'artifact_id': outcome.artifact_id, 'provenance_source': source}],
            'portal_transitive_input_edges': [{'subject': root, 'predicate': 'reveal:portalTransitiveInput', 'object': node['id'],
                                               'path_length': node['path_length'], 'portal_url': outcome.portal_url,
                                               'document_sha256': graph['document_sha256']} for node in graph['raw_inputs']]}


class _Cache:
    """Outcomes by (portal, artifact) with their time: an LRU bounded by the canonical JSON bytes of their graphs."""
    def __init__(self, limit=CACHE_BYTES):
        self.limit, self.items, self.size, self.lock = limit, OrderedDict(), 0, threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.items.get(key)
            if entry: self.items.move_to_end(key)
            return entry

    def put(self, key, outcome):
        size = len(canonical(outcome.graph)) if outcome.graph else 256
        with self.lock:
            prior = self.items.pop(key, None)
            if prior: self.size -= prior[2]
            if size > self.limit: return
            self.items[key] = (time.monotonic(), outcome, size); self.size += size
            while self.size > self.limit: self.size -= self.items.popitem(last=False)[1][2]

    def clear(self):
        with self.lock: self.items.clear(); self.size = 0


_CACHE = _Cache()


class _Loop:
    def __init__(self): self.client, self.slots, self.inflight = None, asyncio.Semaphore(CONCURRENCY), {}


_LOOPS = weakref.WeakKeyDictionary()


def _new_client():
    return httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_SECONDS), follow_redirects=False)


def _state():
    loop = asyncio.get_running_loop()
    state = _LOOPS.get(loop)
    if state is None: state = _LOOPS[loop] = _Loop()
    if state.client is None: state.client = _new_client()
    return state


async def aclose():
    """Close the running loop's client (the app lifespan's shutdown); a later fetch creates a new one."""
    state = _LOOPS.pop(asyncio.get_running_loop(), None)
    if state and state.client: await state.client.aclose()


def _retry_after(value):
    if not value: return None
    try: return max(0.0, float(value))
    except ValueError: pass
    try: return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, OverflowError): return None


async def _body(response):
    """The body, or None past MAX_BYTES (decoded bytes are counted, so a compressed body cannot exceed it either)."""
    length = response.headers.get('content-length')
    # [0-9] only: str.isdigit also accepts other Unicode digits, some of which int() rejects.
    if length and length.isascii() and length.isdigit() and int(length) > MAX_BYTES: return None
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BYTES: return None
    return bytes(body)


async def _fetch(state, base, artifact_id):
    url, deadline, outcome = base + '/get_provenance', time.monotonic() + FETCH_BUDGET_SECONDS, None
    for attempt in (1, 2):
        remaining, retry, delay = deadline - time.monotonic(), False, None
        if remaining <= 0: break
        try:
            # httpx's read timeout bounds each read, not the body: a portal dripping bytes would otherwise hold a slot and
            # this shared fetch for as long as it likes. The attempt (slot wait included) ends with the fetch's budget.
            async with asyncio.timeout(remaining), state.slots:
                async with state.client.stream('GET', url, params={'id': artifact_id}, headers=HEADERS, follow_redirects=False,
                                               timeout=httpx.Timeout(min(TIMEOUT_SECONDS, remaining), connect=min(CONNECT_SECONDS, remaining))) as response:
                    portal, status = str(response.request.url), response.status_code
                    if status == 404: outcome = Outcome('not_found', artifact_id, portal)
                    elif status != 200:  # a redirect is never followed; only 429 and 5xx are retried
                        outcome = Outcome('unavailable', artifact_id, portal, reason=f'http_{status}')
                        retry, delay = status == 429 or status >= 500, _retry_after(response.headers.get('retry-after'))
                    else:
                        body = await _body(response)
                        outcome = (Outcome('invalid', artifact_id, portal, reason='oversize') if body is None else
                                   await asyncio.to_thread(_parse, body, artifact_id, base, portal))
        except TimeoutError:  # the budget ran out: nothing is left for a retry
            outcome = Outcome('unavailable', artifact_id, url, reason='timeout'); break
        except httpx.HTTPError as error:  # httpx timeouts included
            outcome, retry = Outcome('unavailable', artifact_id, url, reason=type(error).__name__), True
        if not retry or attempt == 2: break
        delay = min(RETRY_SECONDS if delay is None else delay, deadline - time.monotonic() - 1)  # leave the retry a second
        if delay < 0: break
        await asyncio.sleep(delay)
    outcome = outcome or Outcome('unavailable', artifact_id, url, reason='timeout')
    if outcome.status != 'unavailable': _CACHE.put((base, artifact_id), outcome)
    return outcome


async def bottom_line(artifact_id, *, timeout=FETCH_BUDGET_SECONDS):
    """The artifact's Outcome within `timeout` seconds. Never raises: a disabled portal, a timeout or an outage is
    'unavailable' (a cached graph up to STALE_SECONDS old is served instead, marked stale)."""
    base = portal_url()
    if not base: return Outcome('unavailable', artifact_id, reason='portal_disabled')
    key = (base, artifact_id)
    cached = _CACHE.get(key)
    if cached:
        age = time.monotonic() - cached[0]
        if age < (FRESH_SECONDS if cached[1].status == 'ok' else NEGATIVE_SECONDS): return cached[1]
    try:
        state = _state()
        task = state.inflight.get(key)
        if task is None:
            task = state.inflight[key] = asyncio.get_running_loop().create_task(_fetch(state, base, artifact_id))
            task.add_done_callback(lambda done: state.inflight.pop(key) if state.inflight.get(key) is done else None)
        outcome = await asyncio.wait_for(asyncio.shield(task), max(0.0, timeout))
    except asyncio.TimeoutError:
        outcome = Outcome('unavailable', artifact_id, reason='timeout')
    except Exception as error:  # a fetch never raises; this is the waiter's own failure
        outcome = Outcome('unavailable', artifact_id, reason=type(error).__name__)
    if outcome.status == 'unavailable' and cached and cached[1].status == 'ok' and time.monotonic() - cached[0] < STALE_SECONDS:
        return replace(cached[1], stale=True)
    return outcome
