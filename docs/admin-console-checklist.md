# Admin console checklist (Phase 3)

Every field of the console as it stood at `d965518`, and where it lives after the restyle
(`docs/plans/admin-console-and-agent-jobs.md`, Phase 3). A box is ticked when the field is on its new page; every box is
ticked except those marked pending or not in this slice. Nothing is dropped; a field the telemetry does not return yet is
marked **pending** with the reason. Slice 3b ticked the job-detail fields against the new page and then deleted
`AdminJobDetail`, `AgentRunDetail` and `AdminAgentRequest`.

Pages: **Shell** (sidebar, top bar, notices on every page), **Overview** `/admin`, **Jobs** `/admin/jobs`,
**Job detail** `/admin/jobs/[id]` (nine sections: Summary, Prompt and settings, Response, Transcript, Event log, Usage,
Timings, Sandbox, Diagnostics, attempts and warnings),
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
- [x] Job detail opens below the table → its own page, `/admin/jobs/[id]`, from every job link (`jobHref`); `?job=` is
  dropped with no redirect (settled choice 4)

## `AdminJobDetail` (deleted in slice 3b; every field is on the job detail page)

- [x] Heading "Execution detail · <label>" and the job id → Job detail heading "Job <short id>", with the job's label and
  the full id to copy
- [x] "Refresh logs" and "Close detail" buttons → Job detail heading: "Refresh logs", and "Back to jobs" in place of closing
- [x] "Loading job logs and diagnostics…" → Job detail
- [x] Error with "Showing previously loaded logs." (stale banner) → Job detail
- [x] Status, stage and "Link to this job" → Summary status callout; the page's own URL is the link to the job
- [x] Failure block: code, message, recorded cause (type · phase, message, time), saved `failure.json` files → Summary
  status callout
- [x] `AgentRunDetail` (see below) → Prompt and settings, and Response
- [x] `AgentUsage`: summary with cost; budget bar → Usage
- [x] `AgentUsage` rows: Agent cost, Budget used, Turns, Agent run time, Provider API time, Tokens output · input, Tokens cache write · cache read, Tool calls (failed), Lint checks · draft writes, Model, Run outcome → Usage
- [x] `AgentUsage`: Runner reason; "Research statement for <parent job>" link → Usage (the link opens the parent's page)
- [x] dl rows: Created, Started, Updated, Finished, Current attempt started, Worker, Box, Attempt / recoveries → Summary
  key values (with Kind and Owner, and delivery recoveries and handoffs when recorded)
- [x] Event log heading, count, "earlier events available" / "beginning of retained history", live/refresh/finished note → Event log
- [x] Event search "Search loaded messages, tools or stages" → Event log
- [x] "Load earlier events", "Copy loaded logs", "Oldest to newest · adjacent agent text fragments are combined" → Event log
- [x] Each event: time, id · kind, stage · state, message, tool name · graph, tool arguments, tool output (open when failed), event data; failed events marked → Event log
- [x] Event log empty states ("No loaded events match this search." / "No retained events are available for this job.") → Event log
- [x] Warnings (count and list) → Diagnostics, attempts and warnings (and "No warnings." when there are none)
- [x] Saved diagnostics: heading, description, one `<details>` per file (failure.json open), unavailable reason, empty note, "first 20" note → Diagnostics, attempts and warnings
- [x] Attempt history: Attempt, Started, Worker at claim, Box at claim; "Latest 50 attempt snapshots." → Diagnostics, attempts and warnings
- [x] Wall time and phase timings: Queue / pre-attempt, Attempt wall time, Box lifetime, Box setup, Agent execution, Artifact capture, Box cleanup, with bars and the instrumentation note → Timings
- [x] 15-second refresh of an active job's logs, paused once earlier events are loaded → Job detail (the job record
  only; the recorded reads never refresh)

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

## `AdminAgentJobs`: one job (`AgentRunDetail`, deleted in slice 3b)

- [x] Summary "How this agent job was run · <type>" → Prompt and settings: the "Asked to return" setting (the panel
  replaces the summary line)
- [x] dl rows: Asked to return, Model · effort, Built-in tools, Claims repository lookups, Graphs, Network, Prompt → Prompt and settings: Settings (extra hosts in their own row)
- [x] dl rows (accounts): Question, Claims named by the requester, Requester named in the account → Prompt and settings:
  Settings; Deposit, Prompt-injection screen, Account, Account contents → Response
- [x] dl rows: Response, Structured output, Input format → Response (the size line); Prompt and settings: Settings
  (Structured output, Input format)
- [x] "Limits the job was given" (Run time, Turns, Budget, REVEAL tool calls, Output per response; "Not part of this input") → Prompt and settings: Limits, until the request is read; then the table's Frozen column, which says "Not part of this input" too
- [x] "Sources the agent saved": Path (converted from), URL, Command, Observed command ("Same as the command"), Size, sha256, Media type, Quotes citing it; capped and empty notes → Response
- [x] Note on sizes, sources and the event log → the Prompt and settings, Response and Event log panels' descriptions, and
  the note under the sources

## `AdminAgentRequest` (deleted in slice 3b; every field is on the job detail page)

- [x] Section "Request" with the recorded-read note; "Show request" / "Loading…" / "Hide request"; error → Prompt and
  settings and Response: "Show prompt and response" / "Loading…" / "Read again" (one read shared by both panels; there is
  nothing to hide on the job's own page), the recorded-read note in the descriptions, and the error, keeping a previous read
- [x] Stop reason (highlighted when a limit was hit); failure code and message → Prompt and settings, first
- [x] Prompt, System prompt, Question, Claims named (with triples), Output schema, each with Copy → Prompt and settings
- [x] Settings: Asked to return, Model · effort, Built-in tools, Graphs, Network, Extra hosts, Claims repository lookups, Requester named in the account, Input format, Owner workspace → Prompt and settings: Settings (from the job record, so
  they show before the request is read); Owner workspace → Summary "Owner"
- [x] Limits table: Limit, Requested, Frozen, Ceiling, Used; hit row; applied, ceiling basis and default notes → Prompt and settings
- [x] Response: characters, truncated, source, attempt, unavailable reason, text with Copy → Response (the text wrapped
  in the reading font; the repository has no Markdown renderer, and the agent's text is never interpreted as markup)
- [x] Structured output; Output files (path · size); "Read by … at …" → Response (output files add their sha256); Prompt
  and settings ("Read by")

## Job detail page: new in slice 3b

- [x] Raw JSON last in its section, folded: job, request, response, metrics, transcript page → each section
- [x] How Claude Code was started (`configuration`): attempt, Claude Code pinned and observed versions, arguments,
  wrapper, prompt on standard input, environment, built-in and REVEAL MCP tools given, MCP servers, Box egress, toolchain
  stamp, account kit, limits enforced and frozen, fetch and convert tools, what it reported at start (version, model,
  permission mode, tools, MCP servers with status), transcript files (stored, written, middle left out); why it is
  missing → Prompt and settings
- [x] Deposit screen in parts: verdict, classifier (enabled, disabled with its lock not pinned, not loaded), failed
  check, score against threshold, where, texts scored → Response
- [x] Account: where it is served from (claims repository or the copy kept with the job) → Response
- [x] Transcript: "Show transcript"; attempt chooser; turns loaded of total, failures, notices skipped; each turn's
  index, role and time; text; thinking folded with its size; tool calls with arguments (pretty JSON, or as received and
  marked when partial or cut) and their result paired by call id across pages; failed calls marked and opened; a result
  whose call is not loaded; unfinished (partial) and cut-at-20,000 marks; the cut marker; the run's end (subtype, stop
  reason, turns, duration, API time, cost); "Load more turns" while more remain; missing reasons; stream cut and
  incomplete notes; stderr last (last 20,000 characters, cut, unreadable); "Last page read by … at …" → Transcript
- [x] Sandbox: Box id with copy, Box phase, Box lifetime, Boxes by attempt → Sandbox
- [x] Box console link → Sandbox, only when `REVEAL_BOX_CONSOLE_URL_TEMPLATE` (server-side, an https URL with
  `{box_id}`) is set; otherwise the id to copy and a note that the link comes with Phase 4
- [ ] Box snapshot → **not in this slice**: Sandbox says "Not yet: Box snapshots come with Phase 4."
- [ ] Each source's fetch time, software and reproducibility result → **pending**: the job record's sources carry path,
  URL, command, observed command, size, sha256, media type and citing quotes only
- [ ] The account linked to the claims console → **pending**: no claims-console address is configured in the frontend,
  so the account is named by its DAPPER id
- [ ] A withheld account's passage → **not shown by design**: the screen records where the text was, never the text

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
