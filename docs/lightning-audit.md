# Lightning audit

Lightning is an explicitly requested, private initial assessment of the evidence associated with a selected knowledge gap. Choose **Lightning audit** alongside **Run online** and **Use my local agent**. The automatic Jev indicator remains independent.

The audit uses one Claude Messages API completion with [structured JSON output](https://platform.claude.com/docs/en/build-with-claude/structured-outputs). It returns an advisory assessment, a substantive rationale connecting the question to the supplied CFDE and DisMech observations, referenced observations, one proposed research direction, missing evidence, next steps, and limitations. The rationale explains both the proposed connection and its strongest uncertainty; a partial or unsupported assessment still includes an explanation and concrete next checks. **Promising direction**, **Limited or partial direction**, and **No supported direction in this package** all allow the researcher to continue. An audit does not close a gap, create an accepted scientific account, or establish that evidence is absent elsewhere.

## Evidence and execution

The service reuses the batched CFDE assessment-state builder: the selected gap, attached DisMech context, up to 50 gene and 50 gene-set loading rows per selected mechanism, source metadata, and bounded researcher-input and verified attachment excerpts. Exact values, source revisions, species/context distinctions, and missing/truncated information are retained. It performs no fresh BioIndex, connected-graph, or literature queries.

Lightning adds short references to a readable scientific projection. Each reference resolves to a JSON pointer in the separately retained source snapshot. The request explicitly asks for scientific synthesis, and field descriptions specify the required rationale, observations and research brief. Provider responses must contain nonblank rationale and direction, next checks and scope limitations; partial or promising assessments also require supporting observations. Responses are checked against the result schema and the supplied reference inventory. Empty model responses are diagnosed separately from invalid evidence references. These checks establish format and reference integrity, not scientific correctness. Source text, notes, and attachments are treated as data rather than model instructions.

The request preserves schema field order so the rationale precedes supporting lists. The exact serialized request is retained privately and its byte hash appears in provenance. Instructions budget 350–450 words across the complete response. The complete provider request is limited to 100,000 UTF-8 bytes and the response to 3,000 output tokens, under a 120-second overall deadline. Selected numeric rows are not silently discarded to fit. Oversized requests fail with guidance to reduce the selection. Inference has one attempt; timeout, refusal, truncated output, malformed responses, unknown references, and process interruption remain explicit failures. Opening a page or polling does not dispatch or retry inference.

The deployment uses `ANTHROPIC_API_KEY` and `REVEAL_CLAUDE_MODEL`. Request/response hashes, model and prompt version, token usage, and timing are retained privately. HTTP failures retain only the status, an allowlisted provider error type and a bounded provider request ID privately; transport and timeout failures retain their category. Credentials and raw provider error bodies or messages are not retained or returned. Completion within 60 seconds on warm, typical inputs is a measurement target, not a guaranteed response time.

## Private history and continuation

Audits live under **Workspace → Research runs**, independently of changes to or deletion of the original draft. Their detail pages show preparing/assessing progress followed by the complete structured result. A failed audit offers an explicit return to the composer to start a new audit.

Review or edit the proposed brief, then choose local or online research. Each continuation creates a new immutable research request and its own generation pin. Original researcher instructions remain separate from the edited brief. The parent audit and its child runs link to each other. Ordinary ownership, research quotas, and local connection/consent rules still apply.

Both execution modes receive the same checksummed audit snapshot, model input, validated response, and edited brief through the shared seed path. These are preliminary planning context, excluded from eligible scientific source IDs. An agent must obtain claim support through ordinary evidence receipts, source validation, and final acceptance. Generated audit prose must not be cited as scientific evidence.

Continuation is available for 30 days, capped by the owning workspace's expiry. A retained superseded reference generation can still be used; no newer generation is silently substituted. Audit reconciliation explicitly releases the parent's pin once its window expires or the owner retires and preparation has stopped. Each child pin follows that run's normal lifecycle. Stored audit content remains reviewable under ordinary workspace retention. Reference cutover may cancel an online child whose dispatch inputs have not yet been captured, as it does for other online runs.

## API

- `POST /v1/lightning-audits`: `{draft_id, draft_version}` and an `Idempotency-Key`; returns `202` with the saved audit and its `Location`.
- `GET /v1/lightning-audits`: the caller's private audit history.
- `GET /v1/lightning-audits/{audit_id}?wait=15`: current audit state; optional `wait` is 0–20 seconds, with no database connection held while waiting.
- `POST /v1/lightning-audits/{audit_id}/continue`: `{mode: "online" | "local", research_direction}` and an `Idempotency-Key`; returns the child run and request IDs.

Identical keyed POST retries reconcile the original operation; reusing a key with different content conflicts. A new explicit audit uses a new key. Replays do not start another paid call. Detail and history reads are authorized against current ownership, and responses are `private, no-store`. Ownership transfer interrupts active audit attempts and invalidates their commit authority while preserving immutable attribution and completed content.

## Enable and verify

The feature is disabled by default. Set `REVEAL_LIGHTNING_ENABLED=true` on the backend and `NEXT_PUBLIC_REVEAL_LIGHTNING_ENABLED=true` **when building the frontend**. The frontend Dockerfile accepts that public flag as a build argument; the local Compose build passes the matching environment variable. In cloud deployments, set the backend flag in the environment's service configuration and supply the public flag to the frontend build. Existing audit reads remain available when creation is disabled.

Use isolated, mocked tests first. Run Lightning tests together with existing CFDE assessment, round-trip-budget, reference retention, ownership, local setup, workflow recovery, and frontend submission/workspace suites. Regenerate the API and both clients' contracts using the documented API build commands. A live provider smoke test is a separate billed operation; no fixture or test suite should invoke it implicitly.

Before a deployment enables Lightning for researchers, measure representative completed audits in that environment. Compare `completed_at` with `created_at` for the end-to-end time, and use the privately retained `timings` to distinguish source preparation from the provider call. Record the first cold request separately from subsequent warm requests; check the 60-second warm target with typical anchor and attachment selections. Mocked transport tests validate deadline handling, not provider latency or scientific quality.
