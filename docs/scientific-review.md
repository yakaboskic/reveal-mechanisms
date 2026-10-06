# Retired independent scientific review — 28 September 2026

> **Historical design, retired 4 October 2026.** Account and paragraph acceptance no longer run a second AI review. The worker applies trusted deterministic validation and saves passing output. Research and paragraph authoring remain separate jobs. The former reviewer implementation and audit records are retained for historical diagnostics; the limits and acceptance policy below no longer describe the live flow.
>
> For a historical job stopped by `REVIEW_UNAVAILABLE` or `REVIEW_BUDGET_EXCEEDED`, **Save existing output** uses the compatible `/retry-review` route to revalidate the captured authoring output and save it if deterministic checks pass. It does not rerun research or call an AI reviewer. Capture checksums, ownership, execution policy, and current-reference guards still apply. Historical scientific rejections are not eligible for this action. See [current acceptance policy](local-development.md).

At the time of this design, account acceptance required trusted DAPPER assembly, source-observation
checks, and an independent verdict covering every Claim and the complete closing
synthesis. The reviewer now reads captured evidence through a bounded local
JSON-pointer tool. It does not receive the author's conversation or gain external
search, filesystem or execution tools. Source content remains untrusted data.

This addresses the operational failure of job
`20f0db79-e269-4d66-bbad-9b6b1ba89752`: its former eager review payload was
903,005 bytes and exceeded the review input-token limit. That failure was not a
scientific verdict. The replacement does not prune the captured scientific
evidence or lower the acceptance standard.

## Evidence access and citation checks

The existing scientific projection retains the exact package fields `selection`,
`dismech`, `pigean`, `entities`, `coverage` and `external_evidence`, together with
captured `query_graph` requests and responses for the selected graphs. Ledger
paths, sizes and hashes are verified before review; failed and empty query
outcomes remain visible. The original package and ledger stay unchanged.

The first request contains the complete proposed document, selected gap, curated
DisMech context, coverage, graph policy, and exact headers for every selected
anchor. Larger observation collections are indexed by pointer, count and hash.
The reviewer is instructed to consider competing observations and limitations,
not just the draft's cited support.

`read_evidence` resolves only `/package/...` and `/graph_calls/...` pointers in
that captured projection. It returns an exact complete value when it fits, child
descriptors with continuation offsets for larger collections, or ordered exact
character slices for long strings. A descriptor page does **not** count as reading
its members. A long string becomes citable only after its whole value has been
supplied without gaps. Initially supplied exact fields are also citable.

`submit_review` must be the only call in its final response. The backend requires
each real Claim exactly once, a synthesis assessment, resolving source pointers,
and actual supplied/read coverage for every cited value. Only all-`supported`
verdicts can pass. An unresolved question, association, failed query or empty
query must retain its original meaning; none establishes causal direction or
biological absence. This is fallible model review, not experimental confirmation.

## Operational limits and outcomes

| Bound | Current value |
|---|---|
| Pinned reviewer model | `claude-sonnet-4-6` |
| Serialized request | Default 96 KiB; `REVEAL_GROUNDING_MAX_REQUEST_BYTES` |
| Each evidence response | 12 KiB |
| Collection page | At most 20 child descriptors |
| Successful evidence reads | At most 64 per account review |
| Model turns | Default 8; `REVEAL_GROUNDING_MAX_TURNS` |
| Tool calls in one response | At most 12 |
| Actual input usage | Default 64,000 tokens per request; `REVEAL_GROUNDING_MAX_INPUT_TOKENS` |
| Output allowance | 3,072 tokens per request |
| Account review budget | Default $0.30 cumulative; configurable positive finite USD cap |
| HTTP timeout | 120 seconds per request; no automatic retry |

`REVEAL_GROUNDING_MAX_BUDGET_USD` is separate from authoring costs. Before each
request, the worker reserves a conservative input/output cost. Subsequent turns
may reuse the provider-measured input count only when the complete earlier
message prefix and request configuration match exactly; appended content is
reserved by byte length plus overhead. Otherwise the full-byte reservation is
used. Actual response usage is accumulated and checked. There is no separate
token-count API call, prompt-cache configuration or automatic paid repair loop.
The last allowed model call requires `finish_review`, which accepts either a
complete review or an explicit unavailable reason, so evidence reading cannot
use the final opportunity to return a verdict. A final
submission still requires complete coverage and verified, actually read sources;
insufficiently inspected evidence remains an unavailable review.

These limits can prevent a review from completing, including when required
initial context is too large. Nothing is silently truncated to force a verdict.
Transport/protocol errors, insufficient context, unread citations,
incomplete verdict coverage, and the reviewer's explicit `review_unavailable`
response yield `REVIEW_UNAVAILABLE`. No scientific verdict was reached, and no
account is published. A complete negative scientific verdict remains a validation
failure. A complete passing verdict proceeds through the existing acceptance
path. Paragraph faithfulness remains a separate check against the frozen accepted
account; paragraph failure does not invalidate an already accepted account.

An insufficient next-call reservation specifically yields `REVIEW_BUDGET_EXCEEDED`,
with the configured cap, actual spend and next-call maximum in the public failure.
It reports an incomplete review, never a scientific rejection. Set
`REVEAL_GROUNDING_MAX_BUDGET_USD` in `.env`; for example, `10` gives a $10 demo cap
per account/paragraph review. Authoring has its own `REVEAL_AGENT_MAX_BUDGET_USD`.

The owner may explicitly POST `/v1/jobs/{job_id}/retry-review` with an idempotency
key and the current `expected_last_event_id`. Only incomplete reviews qualify.
The queue retains the frozen dispatch input and original successful capture;
checksums, complete cleanup, ownership and latest job state are checked before
queueing and capture checksums are checked again at execution. A new validation
attempt reuses those exact authoring bytes without collecting new evidence or
starting a Box/Claude Code agent. It repeats source/identity validation and model
review, with fresh bounded review spend and separate diagnostic files. Existing
attempts remain unchanged. A passing account follows normal acceptance and
paragraph scheduling; a negative verdict stays a scientific rejection.

Successful or negative completed account reviews record
`reveal.scientific-grounding/2`: document/package/ledger and request/response
hashes, actual usage/cost, read pointer/offset/response hashes and supplied-context
pointers. Review-unavailable diagnostics retain the available call/read audit in
`failure.json`; structural `validation-N.json` is written before model review.

## Verification scope

The backend's targeted offline suite passed 17 tests, covering unchanged evidence,
parallel tool results, exact pagination and Unicode strings, unread/invented
citations, complete Claim coverage, negative verdicts, budgets, turn exhaustion,
usage anomalies, and unchanged-prefix reservation checks. Independent source
review found no reader-semantic acceptance bypass.

The actual saved `20f0db79` capture was also replayed through a **fake two-turn
reviewer**, with no API call, database write or scientific verdict. It read the
cited FG Factor3 record and all four captured graph outcomes. The initial request
was 60,319 bytes and the follow-up 74,604 bytes, with source hashes unchanged.
The simulated usage/cost is test input, not measured provider performance or a
bill. The audit is `.runtime/scientific-review-20f0db79/offline-replay.json`.
This proves bounded protocol operation on that capture; a new live scientific
review has not been established by these tests.

Implementation: [review protocol](../services/backend/src/reveal_backend/scientific_grounding.py),
[evidence reader](../services/backend/src/reveal_backend/scientific_review_reader.py),
and [offline reader tests](../services/backend/tests/test_scientific_review_reader.py).
