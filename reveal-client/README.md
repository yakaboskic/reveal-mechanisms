# REVEAL client

A standalone Next.js 15 / React 19 application for the shared REVEAL API. It
includes the session gateway, a working research UI, typed API calls, event
streams, and a framework-neutral server adapter. Copy this directory into your
own project; the backend and scientific workers run separately.

## Start locally

Use Node.js 22 or later. From this directory:

```sh
npm ci
npm run setup -- --credentials /private/path/to/dk-qa.env
npm run dev
```

Open **http://localhost:3200**. Use this exact origin. Click **Connect** to start
the configured demo session; the server resolves its workspace and signs API
assertions automatically. No manual token generation is required.

The supplied private dotenv handoff contains these server settings:

```text
REVEAL_API_URL
REVEAL_GATEWAY_SECRET
REVEAL_GATEWAY_SERVICE_TOKEN
REVEAL_GATEWAY_ISSUER
REVEAL_GATEWAY_AUDIENCE
```

Setup writes an ignored `.env.local` with mode `0600`, generates this client's
own `AUTH_SECRET`, and sets `APP_ORIGIN=http://localhost:3200`. It also configures
a fixed **loopback-only** demo identity: issuer `urn:reveal:client:dk`, subject
`developer`. Reconnecting resolves the same persistent registered workspace.
The browser cannot choose another owner or supply gateway credentials.

An existing `.env.local` is preserved. To update the supplied gateway settings,
run setup with `--force`; this preserves the existing local identity and
`AUTH_SECRET`. Restart the development server after changing settings. Keep
the supplied handoff and `.env.local` outside version control and browser code.
If the handoff includes an artifact download base URL, setup carries it over.

## Use the research flow

1. Connect, search the catalog, and select an actual source question.
2. Request suggested anchors and choose the mechanisms to investigate. Source
   identifiers, revisions, and suggestion provenance come from API responses.
3. Save the draft. Edits use its current version; the saved selections remain
   available after a failed request or page reload.
4. Submit the saved draft to start research. This starts paid backend execution
   under its configured model and time budgets. The example requests at most
   one account with `budgets: { max_accounts: 1 }`.
5. Watch progress arrive through events. Accepted accounts appear with their
   evidence and artifacts. The backend automatically starts cited-paragraph
   jobs; those have their own progress and can complete after the analysis.

A completed investigation can instead report **insufficient evidence**. A
failed or cancelled job remains visible with its status; neither is presented
as a validated scientific account. Review retry is an explicit action, not a
background resubmission.

## Verify the integration

Offline checks require no gateway credentials or scientific execution:

```sh
npm test
npm run typecheck
npm run build
```

With the local client running, the default flow check connects through its
cookie session, searches the catalog, uses stored semantic suggestions, creates
and edits a draft, proves its idempotent replay, and deletes that temporary draft.
It **does not submit a job**:

```sh
node scripts/check-flow.mjs
```

The private-report checker runs on macOS, Linux or WSL, where it can enforce
owner-only file permissions. The server adapter itself is framework-neutral.

To verify a job already submitted through the UI, use its exact ID:

```sh
node scripts/check-flow.mjs --job-id YOUR-JOB-UUID \
  --report .runtime/existing-job-flow.json
```

This follows SSE, reads the terminal result, inspects accounts or the saved
insufficient-evidence outcome, follows automatically created paragraph jobs,
and checks an available source artifact's downloaded SHA-256. It never creates
another analysis or paragraph job.

For an explicitly requested new end-to-end analysis, use:

```sh
node scripts/check-flow.mjs --run-analysis \
  --report .runtime/analysis-flow.json
```

That mode selects the CAD reverse-causation question when present, one returned
automatic anchor, and `biomarkerkg`, saves the draft, then submits exactly one
analysis with `budgets: { max_accounts: 1 }`. It uses the backend's configured
execution budget. Prefer `--job-id` when the UI already submitted the run.

Rerun the **same command and report path** after a lost connection. Before each
write, the checker saves its exact body and idempotency key; it recovers the
same submission and continues from its last event cursor. Reports are private
`0600` files under ignored `.runtime/`; cookies and gateway secrets are not
saved in them. A report is bound to its client origin, mode and user. A lock
prevents concurrent use; after a forcibly killed process, confirm its PID is no
longer running before removing that report's `.lock` file.

Useful options are `--base-url http://localhost:3200`, `--query "search terms"`
for a new smoke/analysis report, and `--timeout-seconds 3600`. Timeout leaves
the job running and the report resumable. The checker never polls job status:
it reconnects bounded SSE streams with `Last-Event-ID` and fetches a job once
after a terminal event. A passing smoke report proves the nonpaid path; only
the completed job report records scientific/result-path acceptance.

Verification on **2026-09-30**: the live QA nonpaid smoke passed. One analysis
submitted through the UI completed with a valid `insufficient_evidence`
outcome; the checker followed 396 events and verified the saved outcome. That
run produced no accepted accounts or paragraphs. The accepted-account →
automatic-paragraph → export path, artifact redirect/checksum checks, and failed
scientific-run reporting are covered by mocked HTTP tests; those branches were
not claimed as live scientific acceptance by this run.

## Integrate with another frontend

Give your implementation agent [INTEGRATION.md](INTEGRATION.md). It explains
which files to reuse, how to replace the local demonstration session with your
own authentication, the server adapter boundary, and the exact request/event
contracts. The UI-facing routes can stay unchanged in another framework.

The bundled [openapi.json](openapi.json) and generated
[API types](src/lib/api.generated.ts) describe the scientific API. The running
QA contract is also available at
https://api-qa.hugeampkpnbi.org/api/reveal/docs.
