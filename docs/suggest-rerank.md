# Jev ordering of automatic mechanism suggestions

`POST /v1/mechanisms/suggest` proposes five EAGGL factors for a DisMech knowledge gap.

Without this feature, the order comes from two sources:

- Eligible exact disease identities, in native id order.
- Cosine similarity between the gap's linked DisMech mechanism descriptions and the EAGGL factor labels.

The gap's own question is used only when no mechanism is linked.

With `REVEAL_SUGGEST_RERANK=jev`, the endpoint keeps that retrieval but widens it to a candidate pool. Jev (TypeSafe SystemOne, `jev-1.13.0`) rates every factor in the pool against the gap, and the endpoint returns the top five by that rating. The implementation is in `services/backend/src/reveal_backend/gap_rerank.py` and is wired in `build_suggestions` (`app.py`).

These ratings order candidates for inspection. They are a model's reading of factor names and are not evidence of biological support.

## When it applies

The rerank runs only when all of these hold:

- the flag is set
- `TYPESAFE_API_KEY` is configured
- `mode` is `semantic`
- `subquery` is empty
- at least one anchor slot is open

This is the automatic path the Composer uses.

The response's `rerank.status` reports what happened. It is absent when the flag is off, so flag-off responses and audit rows are unchanged.

| status | reason | anchors |
|---|---|---|
| `applied` | none | Ordered by Jev, with metric `jev_gap_relevance` |
| `fallback` | `not_configured` | Today's path: no key, so retrieval is unchanged |
| `fallback` | `timeout`, `too_large`, `unavailable`, `invalid`, `internal_error` | Today's disease-then-cosine policy over the same pool |
| `not_applicable` | `subquery`, `hybrid_mode`, `no_open_slots` | Today's path |

A failed rerank never fails the suggestion. Unexpected errors are also logged as `Suggestion rerank failed (<type>)`.

## Candidate pool

The pool contains every eligible disease identity (at most 50) plus the top 100 cosine factors, de-duplicated by native id.

Disease identities are not excluded from cosine retrieval. As a result, a fallback reproduces today's disease-first, then cosine order, except that the cosine top five come from a deeper approximate-nearest-neighbour candidate window (`max(32, 4 × 100)` per context), as recorded in each hit's retrieval provenance.

In the Jev order, disease identities compete on their rating and are no longer pinned first.

## What Jev sees

**State, sent once per request:**

- `evaluation_policy`
- `knowledge_gap`: the exact question, the rationale (when it differs from the question), and the disease
- `dismech_mechanisms`: the name and description of each linked mechanism

**Questions:** two Score questions (0–4) per factor. They name the factor only by its EAGGL label and trait: `cfde_anchor.label`, plus `kpn_trait.name` or the trait part of the legacy subtitle. No genes, loadings, ids or revisions are sent.

- `relevance:Fnnn`: How relevant is this factor to the knowledge gap and its linked mechanisms?
  - Levels: unrelated; generic theme or disease name only; related tissue, pathway or process; directly involves the mechanism in question; central to the mechanism in question.
- `addresses:Fnnn`: Could this factor's genetic evidence help answer the knowledge gap's question?
  - Levels: cannot help; background only; part of an answer; a substantial answer; directly targets the open question.

**Rubric version:** `gap-factor-relevance-v1`. Change it whenever the policy, questions or levels change, because it is part of the cache key and the audit record.

**Ordering:**
- `ranking.value = (relevance + addresses) / 8`.
- Ties prefer higher cosine, with disease-only factors (no cosine) last, then native id.

## Requests, cost and limits

- **Chunks:** factors go in chunks of up to 50 (100 questions), sent concurrently. A chunk halves until its serialized body is at most 100,000 bytes. A factor that cannot fit even alone gives `too_large`.
- **Deadline:** `REVEAL_SUGGEST_RERANK_TIMEOUT_SECONDS`, default 12, covering all chunks. Each chunk is one attempt with no retries or redirects, and the response is capped at 256 KB.
- **Transport:** `jev_batch.post_systemone`, the bounded transport shared with the CFDE assessment.
- **Validation:** responses pass `jev_batch.validate_response`. Live probability sums of 0.99 are kept and recorded as `validation_warnings`. The returned model must equal `jev-1.13.0`.
- **Cache:** answers are cached in process for 1 hour, keyed by model, rubric, state and the factor's own question text. Questions are independent, so a repeated or partially changed pool sends only unscored factors. After a dismissal, only the one newly admitted factor is sent. The cache does no database I/O, so the suggest round-trip budget remains one audit append.
- **Pricing:** $0.042 per million input tokens; output tokens are free.

## Audit record

The `suggestion` row adds two fields:

- **`rerank`:** status, reason, model, rubric, `pool_size`, `state_sha256`, per-request `request_sha256` and `factor_count`, token `usage`, `cached_factor_count`, `validation_warnings` and `elapsed_ms`.
- **`rerank_pool`:** compact rows `[native id, cosine|null, disease identity, relevance|null, addresses|null]` for the whole pool.

Each returned hit also keeps the following:

- its ranking, `context_similarities` and retrieval provenance
- its `jev` answers (score, confidence, probabilities)
- `cosine_rank`
- for a factor that is also a disease identity, `disease_identity`

Draft and request bindings freeze the `rerank` summary and the selected hit, not `rerank_pool`. Evidence packages keep using `context_similarities` for semantic association.

## Configuration

- **Local:** set `REVEAL_SUGGEST_RERANK=jev` in the root `.env`. Local deployments copy `REVEAL_*` and `TYPESAFE_API_KEY` into the backend.
- **QA:** `deploy/dig/service.yaml` sets the flag and already provides the QA key.

## Comparing orders

`scripts/compare_suggest_rerank.py` runs the real `build_suggestions` path for selected gaps, with the audit row captured in memory (nothing is written). It reports today's top five beside Jev's, with label, trait, cosine and both ratings, plus overlap@5, the Jev rank of today's five, and token usage.

- Without `--send`, Jev is never called. The script only measures the request bodies.
- Results are saved per gap. Rerunning skips gaps that already have a result unless `--force` is given.

```sh
REVEAL_APPLICATION_TABLE_PREFIX=reveal_workflow_local REVEAL_VECTOR_ENVIRONMENT=local \
  .venv/bin/python scripts/compare_suggest_rerank.py --disease 'Type 2 Diabetes' --limit 3 \
  --send --output .runtime/suggest-rerank/pilot-t2d
```

## Pilot, October 10, 2026

Three open Type 2 Diabetes gaps, local catalog, `jev-1.13.0`:

- **Requests:** every request succeeded on its first attempt, with two 50-factor chunks per suggestion (100 questions per request).
- **Tokens:** 24,780–26,118 input tokens per suggestion, about $0.001.
- **Latency:** 0.8–1.3 s.
- **Validation:** 1–5 accepted probability-sum warnings per suggestion.
- **Overlap@5 with today's order:** 0, 1 and 0.

In all three gaps the change was in the expected direction:

- **Beta-cell dedifferentiation gap:** today's first pick was a "Beta Cell Dysfunction" factor fitted to serum cystatin C (eGFRcys). Jev ranked it 72nd and chose islet and insulin-secretion factors fitted to glycaemic traits.
- **FATP1 gap:** today's order included a preeclampsia insulin-signalling factor and a smoking-interaction factor. Jev ranked them 60th and 93rd.
- **Reverse-causation gap:** Jev preferred HOMA-IR and fasting-insulin factors over leptin, IGF-1 and hip-circumference fits.

"Addresses" ratings stayed at or below 2.3/4. Factor names alone rarely answer causal questions, so relevance carries most of the ordering signal.

These gaps had no eligible disease identity in this generation, so the pilot did not exercise disease pooling; unit tests cover it.

## Validation

```sh
PYTHONPATH=services/backend/src .venv/bin/python -m unittest discover \
  -s services/backend/tests -p 'test_suggest_rerank.py'
```

The tests cover:

- request content (label and trait only) and chunking under the byte cap
- ranking, tie-breaks and disease pooling
- fallback equality with flag-off anchors, for every provider failure
- `not_configured` and the not-applicable cases
- cache reuse after a dismissal
- audit fields, and the binding copy that excludes `rerank_pool`
- conformance to the OpenAPI `Rank` and `rerank` schemas
