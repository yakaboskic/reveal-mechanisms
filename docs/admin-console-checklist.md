# Admin console checklist (Phase 3)

Every field of the console as it stood at `d965518`, and where it lives after the restyle
(`docs/plans/admin-console-and-agent-jobs.md`, Phase 3). A box is ticked when the field is on its new page; every box is ticked except the three marked pending or not in this slice.
Nothing is dropped; a field the telemetry does not return yet is marked **pending** with the reason.

Pages: **Shell** (sidebar, top bar, notices on every page), **Overview** `/admin`, **Jobs** `/admin/jobs`,
**Job detail** (today's detail, opened on Jobs with `?job=<id>` until slice 3b moves it to `/admin/jobs/[id]`),
**Agent jobs** `/admin/agent-jobs`, **Costs** `/admin/costs`, **Database** `/admin/database`,
**Activity** `/admin/activity`, **Performance** `/admin/performance`, **Limits** `/admin/limits`.

## Page frame (`app/admin/page.tsx`)

- [x] Link home to REVEAL ("REVEAL Mechanisms") → Shell sidebar
- [x] Title "Application observatory" and subtitle "Database activity, scientific data, and agent execution." → Shell sidebar and Overview heading
- [x] "Admin console" label → Shell sidebar
- [x] Page metadata: title, `noindex, nofollow`, `force-dynamic` → `app/admin/layout.tsx` (every `/admin/*` route)
- [x] Admin gate (`adminAccess`): console when allowed, sign-in otherwise → `app/admin/layout.tsx`

## `AdminLogin`

- [x] Heading "Sign in to view telemetry" / "This account does not have admin access" → layout gate
- [x] ADMIN_EMAILS explanation and the ORCID note → layout gate
- [x] "Continue with Google" and "Continue with ORCID" buttons, disabled when not configured → layout gate
- [x] "No sign-in providers are configured." → layout gate
- [x] "Sign out" for a signed-in account without access → layout gate

## `AdminConsole`: notices, toolbar and refresh

- [x] Development bypass notice "Development access: admin sign-in is bypassed on this server." → Shell (every page), and the sidebar says sign-in is bypassed instead of offering sign-out
- [x] Signed-in admin and sign-out (new) → Shell sidebar
- [x] "Updated <time> · <n> ms database read" / "Connecting to telemetry…" → Shell top bar
- [x] "Refresh every 15s" checkbox (on by default; skipped while the tab is hidden) → Shell top bar
- [x] "Refresh now" / "Refreshing…" button → Shell top bar
- [x] Time-zone note "Times shown in <zone>. Each timestamp includes its UTC offset; “—” means not recorded." → Shell, under the top bar
- [x] Error banner with "Showing the last successful snapshot; data is stale." → Shell (every page)
- [x] 401/403 clears the snapshot → telemetry provider
- [x] Old `/admin?job=` deep link and its popstate handling → **dropped** (settled choice 4); `/admin` lands on Overview

## `AdminConsole`: totals (five)

- [x] Application records → Overview metric, links to Database
- [x] Active jobs → Overview metric, links to Jobs filtered to active
- [x] Failed jobs → Overview metric, links to Jobs filtered to failed
- [x] Workspace identities → Overview metric, links to Database record types
- [x] Agent spend, last 7 days → Overview metric, links to Costs

## `AdminConsole`: tabs

- [x] Tab bar (Execution, Agent jobs, Costs, Database, Activity, Performance, Limits) → Shell sidebar navigation (Overview, Jobs, Agent jobs, Costs, Database, Activity, Performance, Limits)
- [x] Limits stays mounted after first open so unsaved edits survive switching pages → Shell keeps `AdminLimits` mounted once opened
- [x] "This backend does not report agent job types yet." → Agent jobs
- [x] "This backend does not report agent costs yet." → Costs

## `AdminConsole`: Execution tab

- [x] Heading "Jobs and Box agents" and "Latest 100 jobs across all workspaces. Elapsed times include recovery pauses." → Jobs panel
- [x] Filter "Filter by job, owner, type, worker or status" → Jobs filter row (free text), plus new kind, status and owner filters
- [x] Status counts (each status with its count) → Overview panel and Jobs, each linking to Jobs filtered by that status
- [x] Column "Job / owner": short id · job label (opens the detail), owner → Jobs
- [x] Column "State": status, stage, failure code, error type · phase → Jobs
- [x] Column "Started" → Jobs (a short age; hover shows the full local time with its UTC offset)
- [x] Column "Finished" → Jobs (a short age; hover shows the full local time with its UTC offset)
- [x] Column "Attempt / Box": attempt, Box phase or "No Box runtime" → Jobs
- [x] Column "Wall time" with "in progress" → Jobs
- [x] Column "Agent time" → Jobs
- [x] Column "Agent cost" (`JobCost`: provider, estimate, not reported, turns, budget share) → Jobs
- [x] Column "Worker lease": expired warning, Finished, lease time or "Awaiting worker", recoveries → Jobs
- [x] Empty state "No jobs match this view. Research jobs will appear here as they are created." → Jobs
- [x] Job detail opens below the table → Jobs with `?job=<id>` (open item in the URL)

## `AdminJobDetail` (kept as it is; slice 3b replaces it)

- [x] Heading "Execution detail · <label>" and the job id → Job detail
- [x] "Refresh logs" and "Close detail" buttons → Job detail
- [x] "Loading job logs and diagnostics…" → Job detail
- [x] Error with "Showing previously loaded logs." (stale banner) → Job detail
- [x] Status, stage and "Link to this job" → Job detail (link now points at the Jobs page URL)
- [x] Failure block: code, message, recorded cause (type · phase, message, time), saved `failure.json` files → Job detail
- [x] `AgentRunDetail` (see Agent jobs below) → Job detail
- [x] `AgentUsage`: summary with cost; budget bar → Job detail
- [x] `AgentUsage` rows: Agent cost, Budget used, Turns, Agent run time, Provider API time, Tokens output · input, Tokens cache write · cache read, Tool calls (failed), Lint checks · draft writes, Model, Run outcome → Job detail
- [x] `AgentUsage`: Runner reason; "Research statement for <parent job>" link → Job detail (link now points at the Jobs page URL)
- [x] dl rows: Created, Started, Updated, Finished, Current attempt started, Worker, Box, Attempt / recoveries → Job detail
- [x] Event log heading, count, "earlier events available" / "beginning of retained history", live/refresh/finished note → Job detail
- [x] Event search "Search loaded messages, tools or stages" → Job detail
- [x] "Load earlier events", "Copy loaded logs", "Oldest to newest · adjacent agent text fragments are combined" → Job detail
- [x] Each event: time, id · kind, stage · state, message, tool name · graph, tool arguments, tool output (open when failed), event data; failed events marked → Job detail
- [x] Event log empty states ("No loaded events match this search." / "No retained events are available for this job.") → Job detail
- [x] Warnings (count and list) → Job detail
- [x] Saved diagnostics: heading, description, one `<details>` per file (failure.json open), unavailable reason, empty note, "first 20" note → Job detail
- [x] Attempt history: Attempt, Started, Worker at claim, Box at claim; "Latest 50 attempt snapshots." → Job detail
- [x] Wall time and phase timings: Queue / pre-attempt, Attempt wall time, Box lifetime, Box setup, Agent execution, Artifact capture, Box cleanup, with bars and the instrumentation note → Job detail
- [x] 15-second refresh of an active job's logs, paused once earlier events are loaded → Job detail

## `AdminAgentJobs`: list (`AdminAgentJobsView`)

- [x] Heading "Agent jobs" and the window description (latest n, limit, sizes only) → Agent jobs heading and panel
- [x] Filter "Filter by job, owner, state, model or deposit" → Agent jobs filter row
- [x] "All jobs ever queued, by kind: …" → Agent jobs
- [x] Per-type cards: jobs, succeeded, failed, cancelled, running or queued → Agent jobs metrics (each links to the list filtered to that type)
- [x] Per-type cards (accounts): deposited, succeeded without a deposit, withheld as unsafe and by check → Agent jobs metrics
- [x] Per-type cards: typical successful run (cost, turns, time), spend, requesters, latest → Agent jobs metrics
- [x] Type filter (All types / each type with counts) → Agent jobs filter row
- [x] Column "Job / owner" (opens the detail) → Agent jobs
- [x] Column "Type" → Agent jobs
- [x] Column "State": status, failure code, stage while running → Agent jobs
- [x] Column "Asked for": model · effort, what it could read, tools, claims named, prompt size → Agent jobs
- [x] Column "Limits given": turns · budget · time, REVEAL tool calls, output tokens per response → Agent jobs
- [x] Column "Used": turns of limit, tool calls and failures, agent time of limit, lint checks · draft writes → Agent jobs
- [x] Column "Returned": objects, claims · quotes · papers, lint warnings; or characters, truncated, files → Agent jobs
- [x] Column "Deposit": outcome, deposit id, screen summary when withheld; "Not applicable" for answers → Agent jobs
- [x] Column "Created" with wall time → Agent jobs
- [x] Column "Cost" (`JobCost`) → Agent jobs
- [x] Empty states "No agent jobs match this view." / "No general agent job has been submitted yet." → Agent jobs
- [x] New column "Sources": saved source files the account lists (`result.papers`, the same list the detail reads) → Agent jobs
- [x] New deposit filter including "Withheld" (`deposit_status` `withheld_unsafe`), with the reason (`screen_check` or `deposit_reason`) in the Deposit column → Agent jobs
- [x] New column "Screen": the deposit screen's verdict (`screenSummary`) → Agent jobs
- [ ] Lint refusals → **pending**: the telemetry returns only `lint_checks` (calls) and `lint_warnings` (on the accepted account), not refusals

## `AdminAgentJobs`: one job (`AgentRunDetail`, in the job detail)

- [x] Summary "How this agent job was run · <type>" → Job detail
- [x] dl rows: Asked to return, Model · effort, Built-in tools, Claims repository lookups, Graphs, Network, Prompt → Job detail
- [x] dl rows (accounts): Question, Claims named by the requester, Requester named in the account, Deposit, Prompt-injection screen, Account, Account contents → Job detail
- [x] dl rows: Response, Structured output, Input format → Job detail
- [x] "Limits the job was given" (Run time, Turns, Budget, REVEAL tool calls, Output per response; "Not part of this input") → Job detail
- [x] "Sources the agent saved": Path (converted from), URL, Command, Observed command ("Same as the command"), Size, sha256, Media type, Quotes citing it; capped and empty notes → Job detail
- [x] Note on sizes, sources and the event log → Job detail

## `AdminAgentRequest` (in the job detail)

- [x] Section "Request" with the recorded-read note; "Show request" / "Loading…" / "Hide request"; error → Job detail
- [x] Stop reason (highlighted when a limit was hit); failure code and message → Job detail
- [x] Prompt, System prompt, Question, Claims named (with triples), Output schema, each with Copy → Job detail
- [x] Settings: Asked to return, Model · effort, Built-in tools, Graphs, Network, Extra hosts, Claims repository lookups, Requester named in the account, Input format, Owner workspace → Job detail
- [x] Limits table: Limit, Requested, Frozen, Ceiling, Used; hit row; applied, ceiling basis and default notes → Job detail
- [x] Response: characters, truncated, source, attempt, unavailable reason, text with Copy → Job detail
- [x] Structured output; Output files (path · size); "Read by … at …" → Job detail

## `AdminCosts`

- [x] Heading "Agent spend" and the window description → Costs heading and panel
- [x] Tiles: Last 24 hours, Last 7 days, Last 30 days (jobs, failed, estimated, not reported) → Costs metrics
- [x] Tile: Typical successful research run (p50, p90, max, runs) → Costs metrics
- [x] Tile: Typical successful agent job (when any) → Costs metrics
- [x] Budget stops (exceeded, near limit) → Costs
- [x] Daily spend (last 30 days): date, stacked bar, total, jobs and failed; legend; empty note → Costs
- [x] Failures by code: Failure code, Jobs, Spend, Recorded causes; empty note → Costs
- [x] Spend by workspace owner: Owner (label, id), Jobs, Spend, Failed, Estimated / not reported; note on owners → Costs
- [x] Most expensive recent jobs: Job (opens detail; statement for parent), Created, Outcome, Cost, Tokens, Agent time; empty note → Costs

## `AdminConsole`: Database tab

- [x] Heading "Scientific database" and description (database name, estimates, missing tables) → Database
- [x] Tables: Prisma table (opens inspector), Availability (Missing warning), Estimated rows, Data + indexes → Database
- [x] Imports and embedding runs: Source, Run / import, State, Loaded / expected (rows or vectors and bindings), Created, Updated; "Latest 10 runs per table"; empty note → Database
- [x] Application record types: Record type, Exact count, Last update → Database

## `AdminTableInspector`

- [x] "Back to database tables" → Database (`?table=` cleared)
- [x] Table name, model, "Read only", "Refresh rows" → Database inspector
- [x] Description (inspect rows, time zone, copy preserves value, ordering) and the `reveal_records` note → Database inspector
- [x] Filter form: Column, Match (Contains/Equals), Value, Apply filter, Clear → Database inspector
- [x] Pager: status (rows · page · filtered), Rows per page (10/25/50), Previous, Next (cursor-based) → Database inspector
- [x] Error with "Reload first page" → Database inspector
- [x] Rows table: Inspect, Timestamps, one column per field (type, primary key), previews → Database inspector
- [x] Empty states (filter / no rows) → Database inspector
- [x] Row contents: Reload row, Close row, key, timestamps, loading and error → Database inspector
- [x] Each value: name, type, nullable, length; value; Load more; Copy stored value / Copy hex; error → Database inspector
- [x] Open table in the URL (`?table=`) (new) → Database

## `AdminConsole`: Activity tab

- [x] Recent record changes: description; Type, Record, Owner, Version, Updated → Activity
- [x] Job event stream: description; Time, Job, Event, State, Stage → Activity (job id opens the job detail)

## `AdminPerformance`

- [x] Heading "API and database latency", scope, lock note, process and uptime → Performance
- [x] Columns: Operation (category, name), Requests, Errors, Mean, Recent p95, Statements / req, DB ms / req, Pool wait, Lock wait, Connects / req, Resets / req → Performance
- [ ] Fetch failures and latency by host → **pending**: the runtime metrics carry no per-host fetch data, and the agents' recorded fetch commands are not aggregated anywhere yet

## `AdminLimits`

- [x] Heading "General agent job limits", description, "Reload limits" → Limits
- [x] Loading and error states, conflict "Reload limits and discard my edits" → Limits
- [x] Last changed by, and the default/ceiling explanation → Limits
- [x] Table: Limit (label, description, unit), Default in effect, Default set here, Ceiling in effect, Ceiling set here, Supported range → Limits
- [x] Each limit row, including Output per response (`max_output_tokens`) → Limits
- [x] Field notes (unsaved, not set here, ignored environment value, lowered to the ceiling), Clear buttons → Limits
- [x] Save changes, Discard changes, status line → Limits
- [x] Change history: When, Who, What changed; note; empty state → Limits
- [ ] Snapshot policy (Phase 4) → **not in this slice**

## New in the restyle

- [x] Overview: latest failures panel (links to each job) → Overview
- [x] Overview: running and queued jobs panel → Overview
- [x] Pagination on long lists (Jobs, Agent jobs, Activity) → each page
- [x] View state in the URL: filters, page and open item → each page
