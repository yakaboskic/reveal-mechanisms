"""
Embedding Service Client

Lightweight client for the embedding service REST API.
Replaces local sentence-transformers/torch usage with HTTP calls.

Service endpoint: POST /embed
Auth: X-API-Key header
Request: {"texts": [...], "model": "...", "provider": "huggingface"}
Response: {"embeddings": [[...], ...], "dimensions": N, "count": N}
Batch limit: 100 texts per request. Batches are sent concurrently (max_workers) with adaptive
concurrency: the number of requests in flight backs off when the service answers 429
"Rate exceeded" (Cloud Run at max instances x concurrency) and creeps back up while it is healthy.
"""

import json
import logging
import os
import random
import ssl
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np


def _make_ssl_context() -> ssl.SSLContext:
    """Use certifi when available, otherwise the system verified TLS context."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:
        pass
    return ctx


BATCH_SIZE = 100
DEFAULT_MODEL = "pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb"
DEFAULT_SERVICE_URL = "https://embedding-service-27386110942.us-east1.run.app"
DEFAULT_MAX_WORKERS = 1
MAX_RETRIES = 15           # with the capped backoff below this tolerates ~6-7 minutes of 429/503
BACKOFF_CAP_SECONDS = 30
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MIN_CONCURRENCY = 1
STATUS_EVERY_SECONDS = 30
WARN_FROM_ATTEMPT = 6      # retries below this are logged at DEBUG; the status line carries the counts


def _backoff(attempt: int) -> float:
    """Exponential backoff capped at BACKOFF_CAP_SECONDS, plus 0-1s jitter so retries don't align."""
    return min(2 ** attempt, BACKOFF_CAP_SECONDS) + random.random()


class _Throttle:
    """AIMD concurrency control + shared stats for one get_embeddings() call.

    `target` is how many requests may be in flight. A 429/503 halves it (at most once per 5s so a
    burst of simultaneous rejections counts once); a full round of successes at the current target
    raises it by one, up to max_workers. Workers block in acquire() until a slot is free.
    """

    def __init__(self, max_workers: int, n_batches: int) -> None:
        self.max_workers = max_workers
        self.target = max(MIN_CONCURRENCY, min(max_workers, 8)) if max_workers > 1 else 1
        self.n_batches = n_batches
        self.in_flight = 0
        self.done = 0
        self.texts_done = 0
        self.consecutive_ok = 0
        self.last_decrease = 0.0
        self.retrying = set()
        self.n_429 = 0
        self.n_5xx = 0
        self.n_neterr = 0
        self.window_429 = 0
        self.latencies = []
        self.started = time.monotonic()
        self.cv = threading.Condition()

    def acquire(self) -> None:
        with self.cv:
            while self.in_flight >= self.target:
                self.cv.wait(timeout=1.0)
            self.in_flight += 1

    def release(self) -> None:
        with self.cv:
            self.in_flight -= 1
            self.cv.notify_all()

    def on_success(self, batch: int, latency: float, n_texts: int) -> None:
        with self.cv:
            self.done += 1
            self.texts_done += n_texts
            self.latencies.append(latency)
            self.retrying.discard(batch)
            self.consecutive_ok += 1
            if self.consecutive_ok >= self.target and self.target < self.max_workers:
                self.target += 1
                self.consecutive_ok = 0
                self.cv.notify_all()

    def on_reject(self, batch: int, status: int) -> None:
        with self.cv:
            self.retrying.add(batch)
            self.consecutive_ok = 0
            if status == 429:
                self.n_429 += 1
                self.window_429 += 1
            elif status >= 500:
                self.n_5xx += 1
            else:
                self.n_neterr += 1
            now = time.monotonic()
            if now - self.last_decrease > 5.0 and self.target > MIN_CONCURRENCY:
                self.target = max(MIN_CONCURRENCY, self.target // 2)
                self.last_decrease = now

    def status_line(self, n_texts: int, batch_size: int) -> str:
        with self.cv:
            elapsed = time.monotonic() - self.started
            texts_done = self.texts_done
            rate = texts_done / elapsed if elapsed > 0 else 0.0
            remaining = n_texts - texts_done
            eta = remaining / rate if rate > 0 else float("inf")
            recent = self.latencies[-50:]
            lat = sum(recent) / len(recent) if recent else 0.0
            line = (f"status: batches {self.done}/{self.n_batches} ({100.0 * self.done / self.n_batches:.0f}%)"
                    f" | in-flight {self.in_flight} / target {self.target} (max {self.max_workers})"
                    f" | retrying {len(self.retrying)}"
                    f" | 429s {self.window_429} last {STATUS_EVERY_SECONDS}s, {self.n_429} total"
                    f" | 5xx {self.n_5xx} | net-err {self.n_neterr}"
                    f" | latency {lat:.1f}s/batch | {rate:.0f} texts/s"
                    f" | ETA {time.strftime('%H:%M:%S', time.gmtime(eta)) if eta != float('inf') else '?'}")
            self.window_429 = 0
            return line


def _validated_embeddings(body: dict, expected_count: int) -> np.ndarray:
    """Reject malformed responses before they can misalign entity/vector rows."""
    if not isinstance(body, dict):
        raise ValueError("Embedding response must be an object")
    dimensions, count = body.get("dimensions"), body.get("count")
    if type(dimensions) is not int or dimensions <= 0 or type(count) is not int or count != expected_count:
        raise ValueError("Embedding response count/dimensions do not match the request")
    try:
        raw = np.asarray(body["embeddings"])
        if raw.dtype.kind not in "fiu":
            raise ValueError("Embedding values must be numeric")
        with np.errstate(over="ignore", invalid="ignore"):
            array = raw.astype(np.float32)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid embedding matrix") from error
    if array.shape != (expected_count, dimensions) or not np.isfinite(array).all():
        raise ValueError("Embedding matrix shape or values are invalid")
    return array


def _embed_batch(batch: list, model: str, url: str, api_key: str, provider: str,
                 batch_idx: int = 0, throttle: Optional[_Throttle] = None,
                 max_retries: int = MAX_RETRIES, timeout: float = 300) -> np.ndarray:
    """POST one batch to /embed, retrying transient failures with capped, jittered backoff."""
    payload = json.dumps({"texts": batch, "model": model, "provider": provider}).encode("utf-8")
    ssl_ctx = _make_ssl_context()
    tag = f"batch {batch_idx} ({threading.current_thread().name})"
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
        )
        wait = 0.0
        if throttle:
            throttle.acquire()
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
                out = _validated_embeddings(json.loads(resp.read().decode("utf-8")), len(batch))
            if throttle:
                throttle.on_success(batch_idx, time.monotonic() - t0, len(batch))
            return out
        except urllib.error.HTTPError as e:
            e.close()
            if e.code not in RETRY_STATUSES or attempt >= max_retries:
                logging.error("%s: HTTP %d, giving up", tag, e.code)
                raise
            if throttle:
                throttle.on_reject(batch_idx, e.code)
            wait = _backoff(attempt)
            logging.log(logging.WARNING if attempt + 1 >= WARN_FROM_ATTEMPT else logging.DEBUG,
                        "%s: HTTP %d (attempt %d/%d), retrying in %.0fs",
                        tag, e.code, attempt + 1, max_retries, wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt >= max_retries:
                logging.error("%s: failed to reach embedding service, giving up: %s", tag, e)
                raise
            if throttle:
                throttle.on_reject(batch_idx, 0)
            wait = _backoff(attempt)
            logging.log(logging.WARNING if attempt + 1 >= WARN_FROM_ATTEMPT else logging.DEBUG,
                        "%s: %s (attempt %d/%d), retrying in %.0fs", tag, e, attempt + 1, max_retries, wait)
        finally:
            if throttle:
                throttle.release()  # slot is free while we back off
        time.sleep(wait)
    raise RuntimeError("unreachable")


def get_embeddings(
    texts: list[str],
    model: Optional[str] = None,
    service_url: Optional[str] = None,
    api_key: Optional[str] = None,
    provider: str = "huggingface",
    batch_size: int = BATCH_SIZE,
    max_workers: int = DEFAULT_MAX_WORKERS,
    log_every: int = 10,
    *,
    max_retries: int = MAX_RETRIES,
    timeout: float = 300,
) -> np.ndarray:
    """Batch-encode texts via the embedding service with up to `max_workers` requests in flight.

    The service (Cloud Run) scales horizontally with concurrent requests, so a sequential loop
    leaves it nearly idle. Concurrency is adaptive (see _Throttle): it starts at min(8, max_workers), halves on
    429 "Rate exceeded"/5xx, and grows back toward max_workers while requests succeed, so the run
    settles at whatever the service can actually absorb (max instances x concurrency). A status
    line every STATUS_EVERY_SECONDS reports in-flight/target, retries, rejects, latency, texts/s and
    ETA; individual retries are DEBUG until attempt WARN_FROM_ATTEMPT.

    Args:
        texts: List of strings to embed.
        model: Explicit model, otherwise EMBEDDING_MODEL or DEFAULT_MODEL.
        service_url: Explicit URL, otherwise EMBEDDING_SERVICE_URL or DEFAULT_SERVICE_URL.
        api_key: Explicit key, otherwise EMBEDDING_SERVICE_API_KEY. Never logged.
        provider: Model provider, default 'huggingface'.
        batch_size: Max texts per request (service limit is 100).
        max_workers: Upper bound on concurrent requests (1 = sequential, the original behaviour).
        log_every: Also log a plain progress line every N completed batches.
        max_retries: Transient retries per batch; use a smaller value for interactive requests.
        timeout: HTTP timeout per attempt in seconds, not a total job deadline.

    Returns:
        np.ndarray of shape (len(texts), embedding_dim), rows in input order.
    """
    for name, value in (("batch_size", batch_size), ("max_workers", max_workers), ("log_every", log_every)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if batch_size > BATCH_SIZE:
        raise ValueError("Service batch limit is 100 texts")
    if type(max_retries) is not int or max_retries < 0 or not np.isfinite(timeout) or timeout <= 0:
        raise ValueError("Retries must be nonnegative and timeout must be positive and finite")
    if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
        raise ValueError("texts must be a list of strings")
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    model = model if model is not None else os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
    service_url = service_url if service_url is not None else os.getenv("EMBEDDING_SERVICE_URL", DEFAULT_SERVICE_URL)
    api_key = api_key if api_key is not None else os.getenv("EMBEDDING_SERVICE_API_KEY", "")
    if not api_key or not api_key.strip():
        raise ValueError("Set EMBEDDING_SERVICE_API_KEY or pass api_key")
    if not model or not provider or not service_url.startswith("https://"):
        raise ValueError("Model/provider and an HTTPS service URL are required")
    url = f"{service_url.rstrip('/')}/embed"
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
    results: list = [None] * len(batches)
    if max_workers <= 1:
        for i, batch in enumerate(batches):
            results[i] = _embed_batch(batch, model, url, api_key, provider, batch_idx=i,
                                     max_retries=max_retries, timeout=timeout)
            if (i + 1) % log_every == 0 or i + 1 == len(batches):
                logging.info("Embedded %d / %d texts", min((i + 1) * batch_size, len(texts)), len(texts))
        return np.concatenate(results, axis=0)

    throttle = _Throttle(max_workers, len(batches))
    stop = threading.Event()

    def reporter() -> None:
        while not stop.wait(STATUS_EVERY_SECONDS):
            logging.info(throttle.status_line(len(texts), batch_size))

    def work(i: int):
        return i, _embed_batch(batches[i], model, url, api_key, provider, batch_idx=i, throttle=throttle,
                              max_retries=max_retries, timeout=timeout)

    logging.info("Embedding %d texts in %d batches, adaptive concurrency up to %d workers",
                 len(texts), len(batches), max_workers)
    rep = threading.Thread(target=reporter, name="status", daemon=True)
    rep.start()
    try:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="emb") as pool:
            done = 0
            futures = [pool.submit(work, i) for i in range(len(batches))]
            for future in as_completed(futures):
                i, emb = future.result()
                results[i] = emb
                done += 1
                if done % log_every == 0 or done == len(batches):
                    logging.debug("Embedded %d / %d texts", min(done * batch_size, len(texts)), len(texts))
    finally:
        stop.set()
        rep.join(timeout=2)
    logging.info(throttle.status_line(len(texts), batch_size).replace("status:", "done:"))
    return np.concatenate(results, axis=0)


def cosine_similarity(
    a: np.ndarray,
    b: np.ndarray,
) -> np.ndarray | float:
    """Compute cosine similarity between vectors/matrices.

    Supports:
    - 1-D vs 1-D  -> scalar (float)
    - 2-D vs 1-D  -> 1-D array of similarities (one per row of a)
    - 2-D vs 2-D  -> 2-D similarity matrix (a_rows x b_rows)

    Args:
        a: numpy array, shape (d,) or (m, d).
        b: numpy array, shape (d,) or (n, d).

    Returns:
        Cosine similarity as a numpy scalar, 1-D, or 2-D array.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.ndim not in (1, 2) or b.ndim not in (1, 2) or a.shape[-1] != b.shape[-1]:
        raise ValueError("Inputs must be vectors/matrices with matching dimensions")
    if a.shape[-1] == 0 or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("Inputs must have finite values and a nonzero dimension")
    a_vector, b_vector = a.ndim == 1, b.ndim == 1
    a, b = np.atleast_2d(a), np.atleast_2d(b)

    # Normalize rows
    a_norm = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-12)
    b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-12)

    sim = a_norm @ b_norm.T  # (m, n)

    # Preserve matrix rank, including a single-row matrix.
    if a_vector and b_vector:
        return float(sim[0, 0])
    if a_vector:
        return sim[0]
    if b_vector:
        return sim[:, 0]
    return sim
