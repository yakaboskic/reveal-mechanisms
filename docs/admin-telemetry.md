# Admin telemetry

Open `/admin` in Next.js. Configure server-side environment variables (the local stack supervisor loads the root `.env`):

```dotenv
ADMIN_EMAILS=chase@broadinstitute.org,cyakabos@broadinstitute.org
DISABLE_ADMIN_LOGIN=false
```

Sign in on the admin page with Google or ORCID; the callback returns to `/admin`. Email matching is exact, case-insensitive and whitespace-trimmed. Access requires the email and `email_verified: true` from the trusted OAuth callback. ORCID's current `openid` flow commonly supplies no verified email, so use Google if your ORCID session is denied. Editable profiles, anonymous workspace emails and client session updates cannot grant access. Existing sessions may require signing in again to obtain an email claim.

For local development, `DISABLE_ADMIN_LOGIN=true` allows direct access. It works only when `NODE_ENV=development` and `REVEAL_ENVIRONMENT` is not `production`. It is ignored by `next build` / `next start`. Restart the frontend after changing environment variables. The bypass never opens the backend endpoint: both the gateway service credential and a signed, short-lived, purpose-bound admin assertion are required. The regular public gateway cannot proxy internal routes. Do not use `NEXT_PUBLIC_` for these variables.

## Available telemetry

- Exact counts and latest update times for all application record kinds; latest 100 record envelopes across workspaces.
- Every table mapped by the Prisma schema, availability, estimated MySQL row counts and data/index size. Missing migrations are visible. Row estimates are intentionally not expensive full counts of scientific tables.
- Latest 10 scientific import, mapping and embedding runs per table, with persisted status and loading progress where available.
- All-time job status totals and latest 100 jobs, including ownership IDs, stage, failures, worker leases and recoveries. Analysis and paragraph jobs show their first worker start and recorded finish directly in the execution table; details also show creation, latest update and current attempt start. Retries do not move the original start time.
- Latest 100 persisted job event envelopes, excluding messages, tool arguments, scientific content and credentials.
- Job wall time, current attempt wall time, queue/pre-attempt delay, Box lifetime, setup, agent execution, artifact capture and cleanup. Phase timestamps live in the existing durable Box handle/capture marker; no paid calls are made for telemetry. Attempt start times survive worker lease recovery.
- API request latency/errors and application repository SQL execution latency/errors. These counters are bounded and process-local, reset on restart and vary across API replicas. Mean covers the process lifetime; p95 covers up to 500 recent samples per operation. SQL timings exclude row decoding, and HTTP timings stop at response headers (not stream completion). Worker activity is shown through persisted jobs/leases and Box timings, not API process counters.

The console refreshes every 15 seconds while visible, supports manual refresh/pause, and labels stale data after failures. Reads use a read-only snapshot without the application write mutex. This initial console provides database state and execution telemetry, not a database change-data-capture audit trail or external monitoring service: deletions and overwritten versions are not historical events, and imports written outside the application repository are not SQL-instrumented. Earlier runs have no phase timestamps and display `—`. Agent time includes polling/event delivery and is not isolated model compute time; elapsed intervals include recovery pauses.

All admin datetimes use the browser's local timezone, named above the views. Each datetime includes the offset applicable at that instant, including daylight saving time. Native MySQL timestamps are read in UTC before display conversion. Date-only scientific metadata stays a date. Missing starts/finishes are shown as `—`; creation time is never substituted for an unknown execution start.

No new tables or required migrations are introduced. Optional migration `007_admin_telemetry_indexes.sql` adds indexes for bounded recent-record scans on larger deployments; apply with your normal migration procedure before relying on frequent polling at scale.

## Inspect a job's logs

Click an analysis or paragraph job in **Execution** to open its logs and diagnostics. The selection is also addressable as `/admin?job=<job-id>`, including older jobs outside the latest 100 dashboard rows. The detail shows the persisted failure message, the worker's saved diagnostic when available, local timestamps, attempt history, and phase timings.

The event log starts with the latest 100 persisted events, displayed chronologically so terminal failures are immediately available. **Load earlier events** retrieves previous pages by their indexed sequence without scanning other jobs. Messages, preparation stages, warnings, tool arguments and output excerpts can be searched within the loaded history. Adjacent agent text fragments are combined for readability; **Copy loaded logs** retains each original event and its stored timestamp. Active logs refresh every 15 seconds when dashboard refresh is enabled; loading earlier history pauses this until **Refresh logs** returns to the latest events.

Saved diagnostics come from the API's configured `REVEAL_ARTIFACTS_DIR`: allowlisted worker failure, token-budget, runtime and validation/grounding JSON files. The local Compose API and worker share that artifact volume. Other deployments need a shared artifact store for these reports; the database event history remains available when files are absent. Previews are limited to 20 files and 128 KiB per file, with missing, oversized or unreadable diagnostics labelled explicitly. Arbitrary filesystem paths, symlinks, queue credentials and private raw agent traces are not exposed. Known environment credentials and credential fields are redacted. These detail reads require the same server-side admin gate and signed assertion as the rest of the console.

## Inspect table contents

In **Database**, select any available table name. The inspector reads every scalar column defined by its Prisma model, including JSON payloads and binary embedding values. It provides column filters (`Contains` or `Equals`), 10/25/50 rows per page, and previous/next navigation ordered by the complete primary key. Contains filters treat `%` and `_` literally. JSON and binary columns are inspected through row details rather than scanned by the filter. Slow scans stop after five seconds of database execution; prefer exact primary-key matches for large tables.

Select **Inspect** beside a row to expand its fields and copy complete values. Large values load in 32,768-character chunks (bytes for binary data, displayed as hexadecimal), so the browser does not download an entire table's payloads. Chunk reads detect changed values and ask you to reload the row rather than mixing versions. Numeric scalar values and JSON numeric literals retain their stored precision. Application payloads omit the top-level worker lease `token` field.

Each row has a timestamp summary for available native date columns and application lifecycle fields, including creation/update, observation, completion, expiry and Box phase markers. Application dates are projected separately from large JSON payloads and labelled with their source path (for example, `payload.summary.created_at` versus the record's `updated_at`). Tables without stored timestamps say **Not recorded**. Expanded values and JSON previews display local datetimes; **Copy stored value** copies the original stored text, preserving timestamps and numeric precision.

The inspector uses the same admin gate and signed backend assertion as telemetry. Reads run in read-only transactions; there are no SQL editor, update, or delete actions. Table and column identifiers are allowlisted from the Prisma schema; row keys and filter values are parameterized. Pagination cursors are signed, bound to their table/filter/page size, and expire after 30 minutes. Pages are live reads, so changes between pages may be visible; use **Refresh rows** to refresh the current view. Dashboard auto-refresh does not reset an open inspector or its selected row.
