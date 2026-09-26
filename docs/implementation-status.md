# Full-stack implementation status

## Scope and ownership

Local Next.js on the host, Docker Compose API/worker, existing Aurora over verified TLS, remote Upstash Box. EC2 provisioning is deferred by the current request. Preserve imported data and scientific identities; migrations are additive and bulk imports are explicit one-time setup.

| Workstream | Owner | Files | Gate |
| --- | --- | --- | --- |
| A: API, database, worker, deterministic adapter | backend agent | backend Python except Box files; SQL/Prisma; Docker/Compose; Python dependency files | Component checks, actual Aurora readiness |
| B: Next.js and auth gateway | frontend agent | `services/frontend/`, its package/lock/config files | Type/build/UI checks; contract-derived client |
| C: Box/Claude and paragraph runner | box agent | `services/backend/src/reveal_backend/box_*`, `agent_execution.py`, new paragraph skill, standalone Box scripts/tests | Standalone stream/lifecycle tests and bounded live execution |
| Integration, startup and acceptance | integration agent | root startup scripts, gateway validation, cross-service browser fixes | Browser against actual API, then validated Box journey |
| Cross-cutting acceptance and delivery | lead | citation registry/rendering, independent grounding review, root environment example, contract generators, documentation | Exact citation exports, source-support review, final evidence audit |

## Shared boundaries

- `api/openapi.json`, `schema/gateway.schema.json`, evidence-package schema, and `schema/agent-output.schema.json` are authoritative. Lead owns generator/contract edits; propose changes before modifying them.
- C owns `agent_execution.py`: typed execution request/result, public activity callback, cancellation callback, and remote handle checkpoint callback. A and C coordinate exact Python signatures before wiring. Request binds job/attempt, kind, trusted input paths, selected graphs and execution/time/budget limits. Result returns raw output paths plus actual runtime/ledger bindings; trusted backend validation alone accepts scientific results.
- The adapter emits only observable activity, tool calls/results, warnings and terminal execution outcomes. Backend assigns ordered persisted `JobEvent` IDs and authoritative job transitions. Do not expose private reasoning.
- Attempt leases fence all writes; cancellation is authoritative, timeout is failure, disconnect only stops the consumer, reconnect replays persisted event IDs, and recovery uses persisted Box handles rather than launching duplicate paid execution.
- Next.js owns OAuth/anonymous cookies, CSRF and short-lived signed gateway assertions. Backend alone provisions principals, enforces owner access and validates separate service authority. No secrets or provider tokens reach browser scientific requests.
- Deterministic execution and fixtures require explicit development/test configuration and visible labeling. They never certify a live scientific result.
- A owns SQL, Prisma, Python dependencies and Compose. B owns frontend dependencies. C owns separate Box runner dependency pins. Integration owns startup scripts. Lead owns root `.env.example`, the final report and shared documentation.
- Baseline imported routing remains 1,756 mappings / 4,056 GeneSet links; exact trait/factor routing, mapped filtering before top five and full native identity deduplication. Saved bindings preserve source/embedding/mapping runs.

## Gates and evidence

1. Inventory and shared boundaries established; imported scientific identities preserved.
2. Component checks passed: 177 backend tests (169 passed, eight optional database skips), seven startup tests, nine frontend tests, TypeScript and production build. Subsequent targeted corrections cover immutable dispatch recovery, measured evidence clipping and accurate omission reporting; final verification is recorded in the validation report.
3. Actual frontend/backend browser gate passed with Aurora, real CFDE collection and explicitly labelled deterministic execution: ownership, CSRF, stale autosave conflicts, idempotency, cancellation, replay, accepted test account, automatic Paragraph, claims, source downloads and citation exports. See `integration-validation.md`.
4. Standalone Box/Claude/MCP gate passed. A real account and its real Paragraph passed trusted DAPPER validation, exact source/citation checks and independent support/faithfulness reviews. The account leaves reverse causation unresolved. See `box-validation.md`.
5. Final combined live gate passed: browser → Aurora API/worker → Box/Claude → accepted account → automatic validated Paragraph → browser exports. Claims, provenance, exact source downloads, citation navigation, clipboard output and Markdown/LaTeX/BibTeX downloads passed. A first scientifically overstated draft was rejected; the corrected run passed independent source review. Both Boxes are deleted.
6. Actual stopped startup, healthy repeated startup, Aurora readiness, repeated teardown and persisted drafts/jobs/accounts/Paragraph across restart passed. Unrelated host/container sentinels survived shutdown and were explicitly removed. Final stack remains healthy in Box mode with no active jobs. Long citation URLs now wrap at 390px; that final CSS-only correction was verified with the actual page after the earlier successful production build.

## External verification

- Aurora connected over verified TLS. All 14 original table counts remain unchanged; 1,756 mappings, 3,103 existing embeddings and 4,056 resolved GeneSet links remain intact. Read-only reports and pre-change schema backup are in `data/validation/local-stack/`.
- Explicit resumable DisMech import and independent full readback passed: 148,039 rows, including 3,367 gaps. Routine startup does not import data.
- Trusted DAPPER 0.2.0-a1 checkout verified at `.runtime/dapper`; every fresh Box verifies its own clone and 34 locked files.
- Real Box create/exec/reconnect/cancel/delete and unprivileged runtime isolation passed. Selected Proto-OKN schema/query calls and exact empty results were captured; unselected queries were rejected.
- One live draft was rejected for unsupported causal inference. Its bounded revision passed source review, trusted assembly and minting. Separate real paragraph authoring, faithfulness review, span/revision validation and all four exports passed. Successful standalone inference costs total $1.34572950, excluding review calls and cancelled sessions whose final provider costs were unavailable.
- Actual token counts showed a public CAD capture at 44,890 tokens; deterministic retention reduced it to 21,416 under the 24,000-token cap, preserving raw sources, anchor identity and explicit omissions. New browser captures are independently measured before paid execution.
- OpenAPI, generated client and portable viewer include authorized captured-artifact downloads and explicit omitted-evidence status: 31 operations, 173 validated response examples and 42 exchanges. Illustrative contract fixtures remain separate from live scientific evidence.

## Configuration and limits

- Credentials are stored only in ignored `.env`; Google/ORCID OAuth client credentials remain unavailable, so live provider callbacks are unverified. Anonymous browser login and registered/anonymous ownership proofs are separately tested.
- No source reset, re-embedding, silent bulk import or EC2 provisioning.
- Evidence that cannot fit even after bounded retention fails before paid dispatch. A failed support review never becomes an accepted scientific result.
- Native Word paste is not verified; the browser's rich-text clipboard write and generated HTML/plain payloads were checked.
