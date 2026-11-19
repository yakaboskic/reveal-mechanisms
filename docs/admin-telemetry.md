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
- All-time job status totals and latest 100 jobs, including ownership IDs, stage, failures, worker leases and recoveries. Analysis and paragraph jobs show their first worker start and recorded finish directly in the execution table; details also show creation, latest update and current attempt start. Retries do not move the original start time. Durable workflow jobs take their start, attempt and recoveries from the workflow execution (step-failure recoveries; delivery recoveries and continue-as-new handoffs are reported separately), because they write no legacy attempt rows.
- Latest 100 persisted job event envelopes, excluding messages, tool arguments, scientific content and credentials.
- What kind of agent job each one was. A [general agent job](agent-jobs.md) returns either an answer or a Scientific Account, and the console says which everywhere a job is listed (`agent · scientific account`, `agent · answer`). The **Agent jobs** tab lists the latest 100 agent jobs, selected by kind so research runs do not push them out of view, with:
  - a tile per type: jobs, succeeded, failed, running, deposited, the typical cost, turns and wall time of a successful run, spend, and how many requesters;
  - what each job asked for: model and effort, built-in tools, whether it could read papers, the claims repository, graphs or extra hosts, how many claims it named and how many came with a triple, and the size of the prompt;
  - the limits it was given and what it used: turns, tool calls and failures, agent time, lint checks and draft writes;
  - what it returned: for an account the object, claim, quote and paper counts; for an answer the size of the response;
  - **Deposit**, in its own column, because a job can succeed while its account is not deposited. Such jobs are counted on the tile as "succeeded without a deposit".

  The job detail shows the same settings in full under **How this agent job was run**, with every limit. The 15-second snapshot and this settings summary never carry the prompt, system prompt, question, claims or response: the backend reports their sizes and counts, taken in SQL. To read them, open one job and select **Show request** ([below](#read-one-agent-jobs-request)). Jobs queued before a job could ask for an account are answers and are marked; settings their input never had show as "—". The tiles cover the listed jobs, not all time; the line above them gives the all-time number of jobs of each kind.
- Agent cost per job, in the **Execution** table, the job detail and the **Costs** tab. When an agent run ends, the workflow records a `job_metrics` row with these fields, copied from the runner's captured `runtime.json`:
  - the provider-reported Claude cost, its dollar cap and the share of the cap used;
  - turns, tokens (input, output, cache write, cache read), agent run time against its limit, and provider API time;
  - completed and failed tool calls, and lint checks and draft writes;
  - model, run outcome and the runner's reason.

  A run stopped before the provider reported its total (a timeout or cancellation) shows **Not reported**, never $0. Box, storage and delivery charges are not included.
- The recorded cause of a failed workflow phase: exception type, phase and a redacted message of up to 500 characters. The public failure message stays generic, for example "The workflow phase could not be completed." This shows why, without opening the artifact store.
- **Costs** tab, computed over the latest 1,000 jobs with metrics:
  - spend in the last 24 hours, 7 days and 30 days;
  - the typical (p50), p90 and maximum cost of a successful research run, and of a successful [general agent job](agent-jobs.md) once one has run;
  - daily spend split between research, statement (paragraph) and general agent runs;
  - budget stops;
  - failures by code, with their recorded causes;
  - spend by workspace owner;
  - the most expensive recent jobs.

  Owners are opaque principal ids. Client principals, such as a partner frontend's shared demo identity, are named (`dk/developer`) because every visitor of that client shares them.
- Job wall time, current attempt wall time, queue/pre-attempt delay, Box lifetime, setup, agent execution, artifact capture and cleanup. Phase timestamps live in the existing durable Box handle/capture marker; no paid calls are made for telemetry. Attempt start times survive worker lease recovery.
- API request latency/errors and application repository SQL execution latency/errors. These counters are bounded and process-local, reset on restart and vary across API replicas. Mean covers the process lifetime; p95 covers up to 500 recent samples per operation. SQL timings exclude row decoding, and HTTP timings stop at response headers (not stream completion). Worker activity is shown through persisted jobs/leases and Box timings, not API process counters.

The console refreshes every 15 seconds while visible, supports manual refresh/pause, and labels stale data after failures. Reads use a read-only snapshot without the application write mutex. This initial console provides database state and execution telemetry, not a database change-data-capture audit trail or external monitoring service: deletions and overwritten versions are not historical events, and imports written outside the application repository are not SQL-instrumented. Earlier runs have no phase timestamps and display `—`. Agent time includes polling/event delivery and is not isolated model compute time; elapsed intervals include recovery pauses.

All admin datetimes use the browser's local timezone, named above the views. Each datetime includes the offset applicable at that instant, including daylight saving time. Native MySQL timestamps are read in UTC before display conversion. Date-only scientific metadata stays a date. Missing starts/finishes are shown as `—`; creation time is never substituted for an unknown execution start.

Jobs that finished before cost telemetry existed can be backfilled from their S3 workspace checkpoints with `scripts/backfill_job_metrics.py --prefix <table prefix> [--aws-profile <profile>]`. It is a dry run unless you pass `--apply`, and it skips jobs that already have metrics.

No new tables or required migrations are introduced. Optional migration `007_admin_telemetry_indexes.sql` adds indexes for bounded recent-record scans on larger deployments; apply with your normal migration procedure before relying on frequent polling at scale.

## Agent job limits

The **Limits** tab sets the per-run limits of [general agent jobs](agent-jobs.md#limits): run time, turns,
budget, REVEAL tool calls and output per response (Claude Code's `CLAUDE_CODE_MAX_OUTPUT_TOKENS`). Each has a default (what a
request gets when it names none) and a ceiling (the most a request may ask for).

- Each row shows the value in force and where it comes from: the console, a named environment variable, or the
  built-in value. It also shows the range the service supports.
- Leave a field empty to not set it in the console; the environment or built-in value then applies.
- A saved change applies to jobs queued after it, on every API replica, with no deploy. Research runs are not
  affected.
- The server checks every value: it must be inside the supported range, and a default may not exceed its
  ceiling. If someone else saved first, your save is refused and you reload.
- The tab shows who last changed the limits and when, and the last 50 changes with the old and new values.

This is the console's only write. It uses its own signed, purpose-bound assertion (`admin_settings`), separate
from the read-only telemetry one, and carries the signed-in admin's verified email for the change record. The
values are one `agent_limits` application record; no migration is needed.

## Inspect a job's logs

Click any job in **Execution** (analysis, paragraph or general agent) to open its logs and diagnostics. The selection is also addressable as `/admin?job=<job-id>`, including older jobs outside the latest 100 dashboard rows. The detail shows the persisted failure message, the worker's saved diagnostic when available, local timestamps, attempt history, and phase timings.

The event log starts with the latest 100 persisted events, displayed chronologically so terminal failures are immediately available. **Load earlier events** retrieves previous pages by their indexed sequence without scanning other jobs. Messages, preparation stages, warnings, tool arguments and output excerpts can be searched within the loaded history. Adjacent agent text fragments are combined for readability; **Copy loaded logs** retains each original event and its stored timestamp. Active logs refresh every 15 seconds when dashboard refresh is enabled; loading earlier history pauses this until **Refresh logs** returns to the latest events.

Saved diagnostics come from the job's S3 workspace checkpoint when the artifact store is S3 (QA, prod and the durable local stack), otherwise from the API's configured `REVEAL_ARTIFACTS_DIR`: allowlisted worker failure, token-budget, runtime and validation/grounding JSON files, and a general agent job's response record (`output/ledger/agent-response.json`: its final answer, or Claude Code's error text when the run stopped). Workflow jobs list the files of their current authoring attempt. The local Compose API and worker share that artifact volume; the database event history remains available when files are absent. Previews are limited to 20 files and 128 KiB per file, with missing, oversized or unreadable diagnostics labelled explicitly. Arbitrary filesystem paths, symlinks and queue credentials are not exposed. The agent's transcript is not in this preview; it is read through its own recorded route ([below](#read-one-agent-jobs-transcript)). Known environment credentials and credential fields are redacted. These detail reads require the same server-side admin gate and signed assertion as the rest of the console.

## Read one agent job's request

On a general agent job's detail, **Show request** loads what the job was asked and what it answered, so you can see
whether a requester's own limits stopped the run. It is loaded only when you ask, never by the 15-second refresh.

- **Stop reason**, as a headline: "Stopped at its own max_turns (12), which the request set.", "Hit its budget
  ($1.00), the default it was given.", "Timed out at 600 s", "A response exceeded max_output_tokens (32,000)",
  "Finished normally.", or the failure code and message. It comes from the job's failure, Claude Code's result
  subtype and the runner's recorded reason. A budget the dispatch clamp lowered names the admin ceiling instead.
- **The request in full:** prompt, system prompt and (for an account job) question and claims as readable text with
  copy buttons; the output schema; model, effort, tools, graphs, network, extra hosts, claims-repository lookups,
  whether the requester is named, input format, and the owner workspace id.
- **Limits:** one row per limit with **Requested**, **Frozen**, **Ceiling** and **Used**; the limit the run hit is
  highlighted.
  - *Requested.* Jobs queued from this release record which limits the request named (`requested_limits` on the
    queue record, beside the input; names only). For older jobs nothing stored tells a requested value from a
    default, so the column says **Not recorded**, and only notes whether the frozen value differs from today's
    default.
  - *Ceiling.* The admin ceiling when the job was queued (`limit_ceilings` on the queue record); for older jobs
    today's ceiling, marked so.
  - *Used.* Run time, turns and cost from the job's metrics; whether a response reached the output cap. REVEAL tool
    calls are not counted on their own; the row shows the run's count of all tool calls instead.
- **Response:** the agent's full final message from the newest attempt's captured response record
  (`attempt-N/output/ledger/agent-response.json`, up to the 8 MB capture limit), or, when that record is missing,
  the job result's copy, marked when it was cut at 50,000 characters. Then any structured output, and the output
  files by name and size. Credentials in the response are redacted as in saved diagnostics; the prompt is shown as
  sent.
- **Configuration:** how the newest attempt's Claude Code was started, from its `runtime.json`: Claude Code's
  arguments and environment, the built-in and REVEAL MCP tools it was given and the ones it reported having, the
  network policy, the toolchain stamp, an account job's skill SHA-256, the limits, and the transcript files' sizes.
  Runs from before this was recorded say so (`not_recorded`).

The proxy route is `/api/admin/jobs/<id>/run`; the backend route is `GET /internal/v1/admin/jobs/{id}/run`. It needs
the gateway service credential and its own signed assertion purpose, `admin_content`, which must carry the signed-in
admin's verified email; the telemetry and settings assertions are refused. Any admin may read it. Before returning
anything, the backend writes one `admin_content_read` record (the admin's email, the job id, the time; never the
content). If that write fails, the read is refused and nothing is returned. Jobs of other kinds return 404. The lease
token, the account's attribution and evidence package, and the account document are never returned.

This record covers this read only. The saved-diagnostics preview in the job detail (which includes
`agent-response.json` up to 128 KiB) and the Database inspector (which shows queue payloads) read under the telemetry
assertion and are not recorded.

### The run response, field by field

`GET /internal/v1/admin/jobs/{id}/run` returns one JSON object. Every key below is always present; a value that is
unknown is `null`. Times are ISO 8601 strings.

- `job`: `id`, `owner` (the owner principal id), `status`, `stage`, `created_at`, `updated_at`, `completed_at`, and
  `failure` (`{code, message}`, redacted) or `null`.
- `input`: the frozen input. `format`, `prompt`, `system_prompt`, `tools` (built-in tool names), `graphs`, `network`
  (`open` or `restricted`), `allowed_domains`, `effort`, `model`, `deliverable` (`answer` or `scientific_account`),
  `claims_repository` (boolean), `output_schema` (a JSON Schema object or `null`), `limits` (`{timeout_seconds,
  max_turns, max_budget_usd, tool_calls, max_output_tokens}` or `null`), and `account`: for an account job
  `{question, attribute_requester, claims: [{statement, proposition_id?, subject_entity?, relation?, object_entity?}]}`,
  otherwise `null`.
- `limits`: one row per limit, in the order `timeout_seconds`, `max_turns`, `max_budget_usd`, `tool_calls`,
  `max_output_tokens`. Each row: `key`; `requested` (`true`, `false`, or `null` when not recorded);
  `requested_basis` (`recorded` or `not_recorded`); `differs_from_current_default` (boolean for older jobs, else
  `null`); `frozen`; `ceiling`; `ceiling_basis` (`queue_time` or `current`); `applied` (what the run was given, when
  known); `used`; `hit` (boolean). The `max_budget_usd` row adds `used_estimated` (boolean), the `tool_calls` row adds
  `all_tool_calls` (every tool call of the run, built-in ones included), and the `max_output_tokens` row adds
  `reached` (boolean once the run ended, else `null`).
- `stop`: `{limit, by, text}`. `limit` is the limit the run hit or `null`; `by` is `request`, `default`, `unknown`,
  `admin_ceiling` or `null`; `text` is the one-line reason shown as the headline.
- `run`: `subtype` (Claude Code's result subtype), `status`, `reason` (the runner's reason, redacted), `tool_calls`,
  `tool_failures`.
- `response`: `source` (`captured`, `job_result` or `null`), `attempt`, `text` (redacted), `characters`, `truncated`
  (the job result's copy was cut at 50,000 characters), `status`, `subtype`, `structured_output`, `files`
  (`[{path, size_bytes, sha256}]`, the output files), `captured_unavailable` (why the captured record could not be
  used: `not_captured`, `too_large`, `unreadable` or `unavailable`; `null` when it was).
- `configuration`: from the newest attempt's `runtime.json`, redacted.
  - `attempt`: the attempt it describes, or `null` before any attempt.
  - `missing`: `null`, or why there is no configuration: `not_captured` (no `runtime.json`), `not_recorded` (a run
    from before the configuration was recorded; `observed` and `transcript` may still be set), `too_large`,
    `unreadable` (not JSON, or another execution's) or `unavailable` (storage could not be read).
  - `format`: `reveal.run-configuration/1`.
  - `claude_code`: `{pinned_version, observed_version}`: the version the harness pins and what `claude --version`
    printed in the Box.
  - `cli`: `argv` (Claude Code's arguments, from the binary path on; the values of `--append-system-prompt` and
    `--json-schema` are request content and appear as `"<N characters, sha256 HEX>"`), `wrapper` (the `setpriv` and
    `timeout` command that starts it, with the seconds it was given), `environment` (an allowlist: `PATH`, `HOME`,
    `ANTHROPIC_BASE_URL`, `PYTHONDONTWRITEBYTECODE`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
    `DISABLE_AUTOUPDATER`, `REVEAL_DAPPER_ROOT`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS` when set, and `ANTHROPIC_API_KEY`,
    which is the key proxy's placeholder `reveal-loopback-proxy` for an agent job; a real key is never recorded),
    and `prompt`: `"stdin"` (the prompt is sent on standard input; it is `input.prompt`).
  - `tools`: `builtin` (the `--tools` list), `mcp` (the REVEAL MCP tools the runner offered, as Claude Code names
    them, e.g. `mcp__reveal__query_claims`), `mcp_servers` (the MCP configuration: `{"reveal": {"type": "http",
    "url": "http://127.0.0.1:8765/mcp"}}`).
  - `network`: the Box's egress, `{"mode": "allow-all"}` or `{"mode": "custom", "allowed_domains": [...]}`. The Box
    cannot observe its own policy: this is derived from the frozen request by the function the worker applies
    (`box_agent.egress_policy`).
  - `toolchain`: `{stamp}`, the SHA-256 of the toolchain recipe installed in the Box (`/reveal/toolchain.sha256`).
    Which snapshot id the Box was made from is not known inside the Box and is not recorded.
  - `account_kit`: for an account job `{skill, skill_sha256, schema_excerpt_sha256}` (the skill's path in the
    repository and the SHA-256 of the skill and schema excerpt the agent was given), otherwise `null`.
  - `limits`: `{execution, agent}`: `execution` is what the runner enforced (`timeout_seconds`, `max_budget_usd`,
    `max_turns`, and for an agent job `max_output_tokens`); `agent` is the job's frozen limits, or `null`.
  - `source_tools`: for an account job `{packages: {name: version}, programs: {name: version line}}` of the fetch
    and convert tools, otherwise `null`.
  - `observed`: what Claude Code reported when it started (its `init` event), or `null`: `claude_code_version`,
    `model`, `permission_mode`, `tools` (every tool name it had, built-in and MCP), `mcp_servers`
    (`[{name, status}]`).
  - `transcript`: what each transcript file holds, keyed by capture name (`ledger/claude-stream.jsonl.gz`,
    `ledger/claude-stderr.txt.gz`), or `null`: `{stored_bytes, original_bytes, cut, omitted_bytes, omitted_lines}`.
- `read`: `{by, at}`: the admin email and time of the access record this read wrote.

## Read one agent job's transcript

The run's full transcript (Claude Code's stream: every message, thinking block, tool call and tool result, and the
end of its stderr) is kept with each attempt ([agent jobs](agent-jobs.md#how-it-runs)) and read a page of turns at a
time. Like the request, it is loaded only when you ask. Research and paragraph runs capture the same two files, but
nothing reads them yet: this route serves general agent jobs only.

The backend route is `GET /internal/v1/admin/jobs/{id}/transcript`, under the same `admin_content` assertion as the
run view. Every page writes one `admin_content_read` record (route `jobs/transcript`, with the attempt and `after`)
before anything is returned; a page that cannot be recorded returns 503 `ACCESS_LOG_UNAVAILABLE` and nothing else.
Unknown ids return 404 `JOB_NOT_FOUND`, and jobs of other kinds 404 `NOT_AN_AGENT_JOB`, with no record written.

Query parameters, all optional:

- `attempt`: the attempt to read, 1 to 100000. Default: the newest attempt.
- `after`: the `index` of the last turn already read, 0 or more. Omit it to start at the first turn.
- `limit`: turns per page, 1 to 200. Default 100.

Out-of-range values return 422. To page, pass the response's `next_after` as `after` while `has_more` is true.

The response, every key always present:

- `job`: `{id, status}`.
- `attempt`: the attempt read, or `null` when the job has none yet. `latest_attempt`: the newest attempt, or `null`.
- `missing`: `null`, or why there is no transcript: `not_captured` (no transcript file: a job run before transcripts
  were kept, an attempt that never started Claude Code, or one not yet captured), `too_large`, `unreadable` (not a
  readable gzip file) or `unavailable` (storage could not be read). A missing transcript is still a 200, with
  `turns: []` and `total: 0`.
- `turns`: up to `limit` turns with `index` greater than `after`, in order.
- `total`: the number of turns in the whole transcript.
- `next_after`: the `index` of the last turn on this page; when the page is empty, the `after` you sent (`null` if
  you sent none).
- `has_more`: whether turns follow this page.
- `cut`: whether the stored stream had its middle left out to fit the 20 MB capture limit (a `system` turn stands
  where it was).
- `complete`: whether the stored file ended properly (`false` when the runner stopped while writing it; the turns
  are what could be read); `null` when there is no transcript.
- `skipped`: lines that are not turns: rate-limit, progress and authentication notices, unknown events and malformed
  lines.
- `stderr`: `null` when not captured, else `{text, truncated, cut, unreadable}`: the last 20,000 characters of
  Claude Code's stderr, redacted; `truncated` when it was longer; `cut` when the stored file had its middle left out.
- `read`: `{by, at}`, as in the run view.

Each turn:

| Field | Value |
|---|---|
| `index` | Its position in the whole transcript, from 0. Stable across pages of the same attempt. |
| `role` | `assistant`, `user`, `tool` (a tool's result), `system` (Claude Code's start, its other notices, or a cut) or `result` (the run's end). |
| `kind` | `text`, `thinking`, `tool_use`, `tool_result` or `result`. |
| `text` | The text, redacted, cut to 20,000 characters; `null` for a tool call. For a tool result, its content (non-text blocks appear as `[image]` and the like). For `result`, Claude Code's final message or error text. |
| `truncated` | Whether `text` was cut. |
| `tool_name` | For `tool_use` and `tool_result`, the tool, e.g. `Bash` or `mcp__reveal__query_claims` (a result takes its call's name; `null` if the call is not in the transcript). Otherwise `null`. |
| `tool_input` | For `tool_use`, the tool's input as compact JSON text, redacted, cut to 20,000 characters (a partial call's input is the JSON received so far and may not parse). Otherwise `null`. |
| `tool_input_truncated` | Whether `tool_input` was cut. |
| `call_id` | For `tool_use` and `tool_result`, the call id that pairs them. Otherwise `null`. |
| `is_error` | `true` for a failed tool result, or a `result` that ended in error. |
| `partial` | `true` for a message Claude Code never finished (the run stopped mid-message, or its end fell in a cut), rebuilt from the streamed pieces. Finished messages come from Claude Code's whole messages and are never repeated from their pieces. |
| `detail` | For the start turn, `{model, claude_code_version, permission_mode, cwd, tools, mcp_servers}`; for `result`, `{subtype, stop_reason, num_turns, duration_ms, duration_api_ms, total_cost_usd}`; for a cut, `{omitted_bytes, omitted_lines, original_bytes}`. Otherwise `null`. |
| `at` | When the line arrived, to the second (ISO 8601, UTC), from the runner's clock lines; `null` before the first one. |

Redaction is the saved diagnostics' (known environment credentials, credential-named fields, bearer tokens, URL
passwords). The run's own credentials were already removed in the Box, and the capture refused any file that still
held one.

## Inspect table contents

In **Database**, select any available table name. The inspector reads every scalar column defined by its Prisma model, including JSON payloads and binary embedding values. It provides column filters (`Contains` or `Equals`), 10/25/50 rows per page, and previous/next navigation ordered by the complete primary key. Contains filters treat `%` and `_` literally. JSON and binary columns are inspected through row details rather than scanned by the filter. Slow scans stop after five seconds of database execution; prefer exact primary-key matches for large tables.

Select **Inspect** beside a row to expand its fields and copy complete values. Large values load in 32,768-character chunks (bytes for binary data, displayed as hexadecimal), so the browser does not download an entire table's payloads. Chunk reads detect changed values and ask you to reload the row rather than mixing versions. Numeric scalar values and JSON numeric literals retain their stored precision. Application payloads omit the top-level worker lease `token` field.

Each row has a timestamp summary for available native date columns and application lifecycle fields, including creation/update, observation, completion, expiry and Box phase markers. Application dates are projected separately from large JSON payloads and labelled with their source path (for example, `payload.summary.created_at` versus the record's `updated_at`). Tables without stored timestamps say **Not recorded**. Expanded values and JSON previews display local datetimes; **Copy stored value** copies the original stored text, preserving timestamps and numeric precision.

The inspector uses the same admin gate and signed backend assertion as telemetry. Reads run in read-only transactions; there are no SQL editor, update, or delete actions. Table and column identifiers are allowlisted from the Prisma schema; row keys and filter values are parameterized. Pagination cursors are signed, bound to their table/filter/page size, and expire after 30 minutes. Pages are live reads, so changes between pages may be visible; use **Refresh rows** to refresh the current view. Dashboard auto-refresh does not reset an open inspector or its selected row.
