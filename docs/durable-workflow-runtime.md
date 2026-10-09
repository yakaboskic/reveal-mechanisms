# Durable execution and workspace delivery

The default deployment is an HTTP API plus the Next.js frontend. Upstash Workflow
delivers bounded, signed execution requests through QStash. Aurora owns job state,
step fences, dispatch intent and event replay; versioned S3 objects hold immutable
inputs, captured outputs and historical review checkpoints. Upstash Vector provides semantic
candidate retrieval. Upstash Redis provides Pub/Sub wakeups only.

## Integration contract

- Python dependencies: `upstash-workflow==2.0.0`, `qstash==3.4.0`,
  `upstash-vector==0.8.0`; existing Box SDK remains `upstash-box==0.3.0`.
- Workflow endpoint: `/internal/workflows/research-v1`. With the DIG prefix this
  is `/api/reveal/internal/workflows/research-v1`. `REVEAL_WORKFLOW_URL` is the
  complete externally reachable URL. Both signing keys are mandatory when
  workflow routing is enabled. Application gateway credentials do not authorize
  workflow callbacks.
- `QSTASH_URL=https://qstash-us-east-1.upstash.io` selects the verified US project.
  The SDK default is the EU project; credentials are region-specific.
- Workflow transport retries reuse a stable run/dispatch identity. RDS fences
  prevent duplicate or stale commits. A lost Box creation response is explicitly
  retained for operator recovery rather than creating a second paid agent.
- The pinned Python Workflow SDK needs a compatibility guard for duplicate
  history records after a lost HTTP response. Both workflow routes normalize
  history only after signature verification, acknowledge duplicate tails without
  effects, and reject conflicting results. Continuations carry stable step
  deduplication identities and the configured delivery timeout. Keep the signed
  SDK integration tests when upgrading the pinned SDK.
- Box observation performs one bounded status request followed by a durable
  Workflow sleep. The selected Python SDK does not expose `wait_for_event`.
  These are Box requests, not Redis polling. No handler remains alive while
  waiting for the agent's next observation. An observe step is two fenced
  transactions: acquiring it reads its execution, step, job and queue in one
  statement, and the Box cursor, deduplicated events and step result commit
  with its completion (11 round trips with no events, 13 with events; it was
  28 and 32 across four transactions). The step decides on the snapshot it
  acquired; the completion re-reads execution and queue under the fence, so a
  cancellation committed meanwhile is kept. If that commit loses its database
  connection, the step is released for a retry, which inspects the Box again
  from the saved cursor. Paid handles (creation, launch, abandoned cleanup)
  still commit before their step completes.
- Managed reconciliation pushes to `/internal/workflows/reconcile-v1` once per
  minute (every five minutes for a local deployment; set `REVEAL_RECONCILE_CRON`
  to `* * * * *`, `*/2 * * * *` or `*/5 * * * *`). It repairs RDS
  dispatch/notification outboxes, stale execution and expired research inputs.
  It does not scan, read, or ping Redis. Its stable schedule ID includes the
  application table prefix and job namespace. Ticks are not retried: one read
  snapshot finds the work, the write fence is taken only for that work and
  never waited for, and a busy database answers 200 `{"status": "deferred"}`
  so the next tick resumes it. `durable_deployment.py down` removes a managed
  schedule after the stack stops.
- Job submission, cancellation and review retry reply as soon as their
  transaction commits. Workflow delivery starts after the reply; the committed
  dispatch or control intent and reconciliation guarantee it.
- Redis consumers use the supplied HTTPS REST streaming subscription. A backend
  shares subscriptions across interested browser clients. Idle SSE heartbeats
  write HTTP comments only. There are no recurring Redis commands. Pub/Sub has
  no replay; RDS provides authorized replay after reconnection.
- `/v1/me/workspace/events` emits committed invalidations and signed reconnect
  cursors. `workspace_change`, `ready`, `resync_required`, `connection_degraded`,
  and `access_revoked` are the stream event names. Job SSE keeps its existing
  event contract but wakes from notifications. A stream renews before gateway
  authorization expires. A read that finds the database pool busy sends a
  heartbeat comment and retries the same cursor after 0.5-2 seconds; the
  stream stays open.
- Vector snapshots require compatible embedding spaces, complete source-binding
  inventories, verified numeric readback and an ANN quality gate. Activation
  is a compare-and-swap registry update. Semantic outages return an explicit
  availability error. There is no automatic local-matrix fallback.

## Direct transfers and capture optimizations

The direct transfer, parallel restore and independent cleanup changes passed
807 backend tests (8 skipped, 303 subtests), plus all 70 frontend tests and
typecheck. Both local images are running with healthy API/frontend responses;
the running transfer modules match the tested source hashes. The same backend
source is deployed to QA and has passed public and signed checkpoint checks.
Production awaits the approval recorded below. No paid scientific run has
verified these optimizations.

Initial preparation still uses local scratch to collect and validate evidence.
It builds the existing allowlisted input/harness bundle once, stores that bundle
in immutable S3, and atomically commits its reference, exact execution settings
and fingerprint before Box allocation. New bootstrap steps let a trusted Box
helper download that exact S3 version using a short-lived signed GET, verify its
size, checksum and fingerprint, and safely unpack it. The API does not restore
the checkpoint or create a bootstrap temporary directory. Saved settings survive
environment changes; the trusted completion marker makes bootstrap retries
idempotent. Older descriptors retain their existing restore-and-bootstrap path.
Focused tests and independent review cover lost acknowledgments, cancellation,
invalid evidence and storage failures before allocation, binding checks and the
legacy fallback. A live synthetic Box downloaded and verified its bundle with
API downloads, workspace restores and temporary directories disabled in the
probe. Initial setup, including dependency installation, took 26.267 seconds;
replaying the original created handle after a simulated lost acknowledgment
took 1.020 seconds. The remote input checksum and bootstrap fingerprint matched,
and the Box was deleted and verified through its status endpoint. No model calls
or Redis commands were made.

Every new Box still installs its toolchain (`apt-get update`, `python3-venv`, a
venv with the pinned PyYAML, LinkML and rdflib, and the pinned Claude Code
package); in QA the whole bootstrap step takes 21-24 seconds, most of it that
install. `REVEAL_BOX_TOOLCHAIN_SNAPSHOT` can name a prebuilt Box snapshot that
already holds it. Unset (the default), a job's Box is created exactly as before.
The bootstrap script now writes `/reveal/toolchain.sha256`, a hash of its exact
install recipe and Claude version, only after a complete install. A Box whose
stamp, venv interpreter, `reveal-agent` user and `claude --version` all match
skips the install (`toolchain=snapshot`, recorded on the prepared handle);
anything else removes `/reveal/venv`, `/reveal/claude` and the stamp and installs
cleanly (`toolchain=installed`), so a stale or partial snapshot never mixes with a
new recipe and bumping `CLAUDE_VERSION` without a rebuild only costs the old
install time. Creation is still one POST with the same labels, so the creation
fence, recovery and label binding are unchanged. Only a snapshot POST refused
with 400, 404, 410 or 422 (no Box was created) falls back to a fresh Box; any
other failure reaches creation recovery as before. Nothing else moves earlier:
the Box is still created at its create step, never in prepare or from a warm pool.

`scripts/build_box_toolchain_snapshot.py` builds the snapshot. It creates one
paid Box per run and has not been run against Upstash yet. Snapshots belong to
one Box account, so build one with each environment's `UPSTASH_BOX_API_KEY` (QA
and production differ), set the printed id as `REVEAL_BOX_TOOLCHAIN_SNAPSHOT` in
that environment, and rebuild whenever `CLAUDE_VERSION` or a pinned package
changes, and at least monthly for apt, PyPI and npm freshness. The builder runs
the job bootstrap's own toolchain branch, never writes credentials, a bundle or a
request, never changes the network policy and never runs the agent, `box_upload`
or `box_remote`. Before snapshotting it requires `/reveal` to hold only `state`
(empty, root-owned 0755), `venv`, `claude` and the stamp, no `/tmp/reveal-*` or
credential files, a pristine `reveal-agent` home and the pinned Claude version,
and it prints `pip freeze --all` and the npm lockfile hash. It keeps the builder
Box unless `--delete-builder` is passed: confirm first that deleting a Box keeps
its snapshots. `--dry-run` prints the stamp, name and script without calling
Upstash. Measure create-to-prepared with and without the snapshot before relying
on it: restoring a snapshot may add creation time of its own.

Newly prepared Boxes upload completed output and evidence directly to the exact
S3 bucket using short-lived presigned PUT URLs bound to object checksum and
length. The API streams the uploaded objects to verify hashes, credential
absence and the trusted ledger, then commits the immutable capture reference.
It does not materialize a capture workspace. Existing Box handles retain their
previous capture protocol; initial evidence preparation remains in the API.

Capture keeps authored output and ledger files within 8,000,000 bytes each. The
exact trusted `runtime.json` manifest has a separate 20,000,000-byte limit because
its derived input-view checksums can exceed the authored-file limit. Both paths
retain the unchanged 40,000,000-byte aggregate capture limit, path checks,
checksums and credential scans. `output/runtime.json` is an authored file and
receives no exception. Existing terminal Boxes can retry capture with this
allowance without changing their bytes, frozen input or authoring attempt.

A live synthetic probe captured 244 files in 38.013 seconds, then repeated the
capture in 39.021 seconds with exactly the same immutable object versions.
Independent cleanup persisted its deletion receipt, and a duplicate delivery
replayed that receipt. A fresh Box status request confirmed HTTP 404 with
"Box has been deleted"; the provider still retains the deleted Box's metadata.
This verifies the transfer and cleanup protocol, not scientific acceptance.

The capture transaction also records a durable `workflow_cleanup` obligation.
Signed `/internal/workflows/cleanup-v1` deliveries delete the recorded Box
independently of the main workflow's generation or terminal state. Validation
can proceed after verified capture. Deletion is idempotent, failed deliveries
remain recoverable, and a matching durable handoff releases the main execution
reservation. The cleanup record owns the Box until deletion or a confirmed
provider 404 is acknowledged; a released reservation alone is not deletion. No Redis reads or
in-process background task are needed to keep cleanup alive.

Account and paragraph acceptance use deterministic validation, with no second AI
review. Their pinned DAPPER work (account and Paragraph lint, identity minting and
cited-text assembly) runs in one warm, isolated helper interpreter per process,
warmed while the Box runs (`REVEAL_DAPPER_HELPER=0` starts a fresh interpreter
per call; see database-and-evidence-performance.md). Passing captured output proceeds directly to saving. Historical review
checkpoints remain available for diagnostics; saved output from an incomplete
legacy review can be revalidated through the compatible retry route. Initial
preparation, validation and final acceptance use bounded file-based scratch where
their validators require files; legacy bootstrap keeps its existing scratch path. The legacy
`REVEAL_MAX_REVIEW_STEPS` setting caps concurrent deterministic validation steps.
Their S3 restores and checkpoint snapshots keep 16 objects in flight
(`REVEAL_S3_TRANSFER_CONCURRENCY`, 1-64; botocore's connection pool is sized to
match) with bounded buffered bytes and cancellation draining. A restore downloads
each distinct object version once, however many workspace paths share its bytes.
Every version a restore or direct capture has verified (size and SHA-256 on that
exact VersionId) seeds the process's verified-put cache, so the validate step's
closing checkpoint, and a commit on another task, upload only new files instead of
sending a HEAD per captured file. This adds no workspace cache and no Redis reads.

A read-only comparison of one cold restore per setting used an actual checkpoint
containing 244 files and 9,347,440 bytes: serial download took 74.173 seconds;
four downloads took 16.570 seconds, a 4.48× improvement. All files were verified,
with no model calls or Redis commands. This is one checkpoint measurement, not
an end-to-end job speedup; the report is
`.runtime/workflow/parallel-restore-benchmark.json`.

Progress now distinguishes completed authoring from ongoing collection: the
authoring completion notice no longer inherits a growing forward timer, and
validation is announced before restoring its saved evidence.

## Bounded workflow runs and capacity (October 8, 2026)

`REVEAL_WORKFLOW_STEPS_PER_RUN` defaults to six phase steps. Each segment uses
at most six `phase-N` steps and six durable `wait-N` sleeps before handing off
through the existing dispatch outbox. The segment size is frozen into its
dispatch payload, so changing the environment does not change replay ordering.
The handoff increments generation and fences the parent, preserves the global
phase index, Box handle, observation cursor, deadline and authoring attempt, and
uses a deterministic child run ID. Dispatch retries publish the same identity.
Handoffs increment their own counter; they do not spend the recovery budget.

The pinned Python Workflow 2.0.0 SDK advertises the LazyFetch feature, but its
request parser still expects the inline history and has no supported history
fetch path. The pinned QStash 3.4.0 client adds no usable lazy-history control.
The fix therefore bounds history by starting a fresh run; retain the signed SDK
replay tests when upgrading. See the [Python SDK source](https://github.com/upstash/workflow-py)
and [Workflow serve options](https://upstash.com/docs/workflow/basics/serve).
The signed SDK regression drives 204 continuous phases through 34 fresh runs:
maximum simulated callback body size falls from 198,183 bytes without handoff
to 6,574 bytes with six-phase segments (below the 16 KiB test threshold). This
is measured SDK wire-history simulation, not a live nginx measurement. The
ingress body limit must still accommodate the bounded history; its separate
administrator repair is not replaced by the application bound.

A stale delivery with no failed application step records `delivery_recoveries`
and can resume without exhausting `REVEAL_WORKFLOW_MAX_RECOVERIES` (default
three). Genuine step failures consume that budget. Once genuine step failures
exhaust it, reconciliation or the workflow failure callback transfers known
Boxes to an independent cleanup obligation. A delivery-only failure callback
remains recoverable instead. The execution reservation is released only after
the cleanup handoff is recorded atomically. The job becomes failed, or cancelled
if cancellation was pending, with a specific failure code; checkpoint and recovery audit fields remain.
An allocation whose creation response was lost and whose Box identity is unknown
stays reserved for explicit reconciliation: inventing a second allocation or a
cleanup receipt would leak the first Box. After locating it, an operator can
supply `workflow_admin resume --box-id … --expected-generation N`; verified
Box labels bind it to this job/attempt, and a newly terminal recovery routes
directly to abandonment cleanup instead of restarting authoring. Abandonment
records why uncaptured work was stopped, cancels/deletes the Box, preserves the
existing workspace and does not claim that final capture succeeded. Observe
pending cleanup separately, because provider concurrency can temporarily exceed the execution reservation
count while deletion catches up.

A phase that raises a retryable error records why it is retried (`retry_cause`).
Only its own failures are `step_failure` and spend the budget: an expired step
deadline, or an error with no infrastructure cause, such as a size limit, an
unsafe path, a checksum or binding mismatch, rejected credential material, an
inconsistent remote cursor, a remote command that did not complete, a rejected
provider request (HTTP 4xx other than 408/425/429), a deterministic database
rejection (for example an oversized packet or invalid JSON text), a corrupt
archive or a missing local file. Infrastructure is `infrastructure` and counts
in `delivery_recoveries`: `DatabaseBusy`/`FenceBusy`; a lost, refused or
timed-out session, connection limit, lock wait timeout, deadlock, server
shutdown or read-only failover (MySQL 1040, 1053, 1205, 1213, 1290, 1836, 2003,
2006, 2013, 2055, 4031 and driver interface errors); a lost observation commit;
network timeouts; connection, DNS or TLS errors; a full scratch disk or
exhausted descriptors; and S3 or Box throttling or 5xx responses. These are
recognized when a client library raises them directly, such as the raw `httpx`
error or `BoxError` that `AsyncBox.get` raises when a phase connects to its Box,
as well as when `StorageUnavailable` or `BoxTransportError` was explicitly
raised from them; those wrappers are classified by that cause. Every database
session error and every Box provider response retries the phase rather than
failing the job. An application error raised from an outage keeps its own
outcome.

Infrastructure retries are bounded by time. The first one records
`infrastructure_since`, and only a step that runs the phase to completion clears
it. A step that merely reschedules the phase keeps it: one deferred because the
phase's concurrency group or scratch cap is full, or one that finds its Box,
cleanup or capacity busy. A busy system therefore cannot restart the window.
Once a phase has been unable to complete for
`REVEAL_WORKFLOW_INFRA_RETRY_SECONDS` (default one hour), its further retries
are recorded as `step_failure`, with the outage start in the diagnostic and a
warning in the log. A Box API that keeps failing therefore exhausts the budget
and reaches the same recovery as any failed phase: the job fails, the Box is
handed to durable cleanup and its reservation is released. Operator `resume`
resets `retry_cause`, `infrastructure_since`, the scheduler-failure fields and
any sweep hold together with the budget, keeping the previous values in the
recovery audit.

Reconciliation decides each stale execution in its own transaction. A busy,
lost or failing database still defers or fails the whole tick. Any other
failure belongs to that row alone, for example `Recovery cleanup binding
changed` when an existing cleanup intent no longer matches the execution: the
row's transaction rolls back, so its reservation, Box, cleanup obligation, job
and disposition are unchanged. The row is logged, listed in the reconcile
response's `needs_operator`, marked `sweep_hold` (shown by `workflow_admin
inspect`) and left out of the sweep for 15 minutes, after which it is decided
again; the other rows continue.

### Concurrency settings

These limits apply to separate resources; raising the Box cap does not require
raising scratch or validation concurrency. QA has room for 25 authoring Boxes
across users, with five unfinished jobs per user. Additional accepted work can
wait for allocation. No global limit of 25 queued job records is imposed.
Production keeps its explicit two-Box setting.

| Control | Code default | QA setting | Scope |
| --- | --- | --- | --- |
| `REVEAL_MAX_ACTIVE_BOXES` | 25 | 25 | Reserved allocations in this job namespace; matching durable cleanup handoffs and deleted Boxes are excluded. |
| `REVEAL_MAX_ACTIVE_JOBS` | 2 | 5 | Unfinished jobs per user, including queued jobs and review retries. |
| `REVEAL_MAX_RUNNING_JOBS` | 2 for Redis, 0 for database | 25 | Legacy queue leases only; Workflow consumers bypass this limiter. Zero means unlimited legacy leases. |
| `REVEAL_MAX_SCRATCH_STEPS` | 2 | 2 | Concurrent file-based phases, across replicas. |
| `REVEAL_MAX_PREPARATION_STEPS` | 2 | 2 | Preparation/bootstrap phase leases. |
| `REVEAL_MAX_CAPTURE_STEPS` | 2 | 2 | Capture phase leases, including direct capture. |
| `REVEAL_MAX_REVIEW_STEPS` | 2 | 2 | Deterministic validation/commit phase leases; retained legacy setting name. |
| `REVEAL_MYSQL_POOL_SIZE` | 10 | 10 | Pooled sessions per process; allowed 1–32, half of them (rounded down) for pooled reference reads. Zero bypasses the bounded pool and is unsuitable for this load. |
| `REVEAL_MYSQL_POOL_WAIT_SECONDS` | 5 | 5 | Bounded connection wait; allowed greater than zero through 30 seconds. |
| `REVEAL_WORKFLOW_MAX_RECOVERIES` | 3 | 3 | Genuine step failures an execution may retry before it fails and hands its Box to cleanup; delivery stalls, handoffs and infrastructure retries within the window below do not count. |
| `REVEAL_WORKFLOW_INFRA_RETRY_SECONDS` | 3600 | 3600 | How long a phase may keep retrying unavailable infrastructure before its retries spend `REVEAL_WORKFLOW_MAX_RECOVERIES`; positive integer. |
| `REVEAL_WORKFLOW_OBSERVE_INTERVAL_SECONDS` | 5 | 10 | Durable sleep between Box observations. |
| `REVEAL_WORKFLOW_STEPS_PER_RUN` | 6 | 6 | Phase steps before a fresh run; positive integer, frozen per dispatch. |
| `REVEAL_AGENT_TIMEOUT_SECONDS` | 1800 | 1800 | Agent runtime budget; frozen with each prepared input; the service-wide setting in `deploy/dig/service.yaml` is also 1800. |
| `REVEAL_AGENT_MAX_BUDGET_USD` / `REVEAL_AGENT_MAX_TURNS` | 5 / 100 | 5 / 100 | Per-agent research spending target / turn bound, frozen at preparation. |
| `REVEAL_PARAGRAPH_MAX_BUDGET_USD` | 1 | 1 | Per-agent spending target for a paragraph (research statement) job, frozen at preparation. |

All listed environment controls are read by the runtime. QA leaves the pool
settings at their defaults. DIG task count remains `min_tasks=max_tasks=1`; the
backend runs one Uvicorn process (`python -m reveal_backend.serve`). That
process leases clean pooled sessions, and a process-local writer gate admits at
most two fenced transactions per table prefix to a pooled session at once (one
holding the fence, one queued on it). Further writers wait in the process for at
most the 15-second session lock wait and then get the retryable busy 503
(`DatabaseBusy`); reconciliation takes the fence with `NOWAIT` and defers its work
to the next tick instead. Aurora still serializes application writes through one
`FOR UPDATE` fence; more processes or connections do not increase that fence's
throughput.

Additional fanout limits are within an operation, not active-job admission:
S3 snapshots and restores keep `REVEAL_S3_TRANSFER_CONCURRENCY` objects in
flight (default 16) and direct-capture verification uses four workers;
evidence collection accepts `max_parallel_requests` between one and four;
Box MCP evidence reads accept `max_parallel_reads` between one and four (default
two). These memory/IO bounds are independent of the Box cap, and their
enclosing phase leases bound backend fanout. Anonymous users also have
`REVEAL_ANONYMOUS_ANALYSES_PER_DAY=5` by default, independently of concurrency.

Allocation uses a narrow database count under the existing transaction fence,
scoped to the namespace and excluding valid cleanup handoffs. It does not load
all historical execution and cleanup JSON into Python and requires no Aurora
DDL. `CapacityBudget` in `tests/test_round_trip_budget.py` (budget
`reserve_box`) covers 500 completed executions and cleanup receipts: three
statements under the fence (the owned execution read, the single `SUM(active)`
count and the execution update, whose pre-read the transaction's identity map
skips), or five round trips with the fence and `COMMIT`; a clean pooled session
returns to the pool with no reset command. Before this merged with the
pooled-session work it was four statements and nine round trips, three of them
session-reset commands. Cold connection setup is separate. An
authorized read-only execution of this count against QA Aurora returned two
reserved Boxes in 139 ms; this single WAN measurement confirms compatibility,
not server-side lock hold time or concurrent load capacity.

Read-only `EXPLAIN ANALYZE` on Aurora (October 9, 2026; QA 30 executions, 30
queue rows, 22 cleanup receipts; local 25/25/13) showed the single `NOT EXISTS`
over both cleanup kinds as a hash antijoin that range-scanned and hashed every
cleanup receipt on each reservation, whether or not any execution was
reserved. One `NOT EXISTS` per cleanup kind, comparing the id in the column's
own `ascii_bin` collation, is a single-row primary-key lookup per reserved
execution instead (optimizer cost 374 to 80 on QA and 213 to 70 locally; median
server time 0.39 to 0.23 ms and 0.30 to 0.19 ms). Each execution and queue row
of the prefix is still read once through the `(kind, id)` primary-key prefix:
their reservation fields live in JSON, and avoiding that read needs an index or
the planned archive of completed records, both outside this no-DDL change.

### Backend and provider headroom

At five seconds, 25 observing jobs request about five Box inspections/second.
Each observation is two fenced write transactions: acquire, then the completion
that commits the Box cursor, deduplicated events and step result together (11
round trips, 13 with events). It decides on acquire's snapshot, with no separate
context read: about 10 serialized writes/second before other traffic. The
six-phase handoff adds roughly two callbacks per segment to the usual phase/sleep
pair: approximately 11.7 Workflow callbacks/second, plus reconcile once/minute,
starts, cleanup and retries. Each handoff is one more fenced transaction with
one batched read (budget `workflow_handoff`, 7 round trips) plus its dispatch
acknowledgment. QA's ten-second interval halves those steady rates to 2.5
inspections, 5 writes and 5.8 callbacks/second.
This adds at most about five seconds to normal completion detection compared
with the old setting; cancellation control deliveries remain independent.

For 50% write-fence utilization at ten seconds, average lock hold time needs to
stay below about 100 ms (1 / (2 × 5)). Handoffs (about 0.4/second for 25 jobs at
ten seconds, two fenced writes each), event bursts and UI writes need additional
allowance. Measure database and callback tail latency, connection
waits, dispatch age, pending cleanup and provider 429s during the first QA ramp.
The 25-job SQLite/WAL test completes create/bootstrap/launch, three observations,
capture, cancellation and independent cleanup with a fake Box adapter, without
external calls. All 25 allocations coexist before any can proceed; final
reservations and fences are released, and duplicate cleanup reuses the receipt.
Three local runs of the merged code took 1.8–2.0 seconds with 821–881 write
transactions (1,052 before the merge, when each observation committed its cursor
in a transaction of its own) and a maximum SQLite lock acquisition wait of
114–349 ms (test limits: five-second busy timeout, 30-second total deadline). Durable waits are scaled and staggered in the test.
This proves local coordination with WAL, not rollback-journal behavior, Aurora
latency or a 25-agent cloud qualification. Retain one task/process and two scratch
leases until the shared database measurements justify increasing capacity.
Do not hold a callback open to poll repeatedly: doing so would consume ingress
and QStash concurrency while awaiting remote work.

External limits were checked against official sources on October 8, 2026:

- **Box:** the public Free plan allows 10 concurrent Boxes; Pay as You Go lists
  a default soft quota of 1,000, with increases by request. Therefore 25 requires
  paid/custom capacity. QA's actual Box subscription/quota was not available
  from the read-only inspection and must be confirmed in the account. The
  Claude Code agent uses our Anthropic key; Upstash's built-in LLM allowance is
  not its token budget. [Box pricing](https://upstash.com/pricing/box).
- **QStash:** Free lists 1,000 messages/day and global parallelism 10; Pay as You
  Go lists unlimited daily messages and global parallelism 100. QA's authenticated
  read-only `/v2/globalParallelism` returned **100**, which confirms its current
  parallelism, not its billing plan. Publish/batch delivery APIs have no stated
  requests-per-second cap; overflow queues behind global parallelism. The separate
  queue parallelism limit (10 on paid standard plans) does not govern our batch
  publishes. Every delivery attempt, including retries, is billable; observability
  APIs have separate rate limits. At 5.8 callbacks/second, continuous occupancy
  is approximately 504,000 deliveries/day before extra traffic, well above Free.
  [QStash pricing and limits](https://upstash.com/pricing/qstash),
  [global parallelism API](https://upstash.com/docs/workflow/rest/flow-control/global-parallelism).
- **Anthropic:** the pinned `claude-sonnet-4-6` costs $3 per million uncached
  input tokens and $15 per million output tokens; cache reads are $0.30 per
  million (cache writes have separate prices). A worked example of 100,000
  uncached input + 10,000 output tokens costs $0.45 per agent, or $11.25 for 25.
  The configured $5 research budgets total $125 for 25 fresh runs, excluding Box,
  storage and delivery charges; they are client-side per-run targets, not an
  organization-wide spend reservation. [Sonnet 4.6 pricing](https://platform.claude.com/docs/en/models/sonnet-4-6/overview).
  Public Start-tier Sonnet 4.x limits are currently 1,000 RPM, 2 million input
  tokens/minute and 400,000 output tokens/minute; shared organization/workspace
  limits, acceleration limits and spend caps can be lower. A simultaneous first
  100,000-token request from each agent is 2.5 million input tokens and would
  exceed that example tier. Cache-read tokens do not consume Sonnet ITPM.
  QA's actual tier, workspace limits and other workloads are unverified; inspect
  the console/read-only Rate Limits API and ramp gradually before paid load.
  [Anthropic rate limits](https://platform.claude.com/docs/en/api/rate-limits).

### Verification of this repair

After merging with the pooled-session and round-trip work, the full backend
suite completed with 2,352 passed, 10 skipped and 1,123 passing subtests in
952.57 seconds. Its only failure was the pre-existing
`test_worker_collection.py::WorkerCollectionTests::test_failed_agent_notice_has_no_forward_timer_or_terminal_job_claim`.
The round-trip budgets (`reserve_box` 5, `workflow_handoff` 7, `observe_tick` 11,
`reconcile_idle` 4), signed 204-phase replay (6,574-byte maximum callback body),
recovery/cleanup races and the 25-job test passed. Before the merge, the branch
alone completed with 2,023 passed, 10 skipped and 674 subtests.

In an isolated worktree, set `REVEAL_DAPPER_ROOT` and
`REVEAL_TEST_DAPPER_RELEASE` to an existing checkout of the pinned DAPPER release.
The ignored `.runtime/dapper` dependency is not copied into Git worktrees; without
it, citation, fixture and research integration tests fail on missing files.
The run above used that dependency read-only and made no QA writes.

### QA recovery remains an operator action

Read-only inspection found jobs `04cc4e03-4e2a-49df-9856-3ffbf7ed64d0` and
`6831e472-679d-49f9-9100-69d078d7cbb3` at generation four, phase index 37,
`recovery_required`, three recoveries, reserved capacity and no cleanup handoff.
The provider still reported Boxes `legal-tuna-25525` and `infinite-teal-30261`
as existing and idle. The first job has pending cancellation; the second has
passed its deadline. This implementation and test work makes no QA writes.

Use `workflow_admin inspect` to re-read each generation, then obtain explicit
user approval before
`workflow_admin resume --job-id … --expected-generation N` against QA. Process
cancellation or deadline expiry, preserve capture when available, and let the
independent cleanup consumer delete the recorded Box. Verify the authoritative
job outcome, `capacity_reserved=false`, the completed cleanup receipt and a
provider deletion/404 for both Boxes. Resume dispatches QStash and changes QA
records; do not infer authorization from the read-only inspection. Deploy the
fixed image and QA configuration before attempting the 25-job load ramp.

## Local pilot

Supply the root `.env` credentials from `.env.example`; never expose provider
credentials to the frontend. Existing verified scientific assets and AWS storage
configuration are reused from `.deployment-assets` and `.runtime/deployment`.

For local scheduling, run the pinned development server in its own terminal:

```sh
mkdir -p .runtime/workflow
npx --yes @upstash/qstash-cli@2.37.18 dev -port 18080 > .runtime/workflow/qstash.log 2>&1
```

Then prepare and run the pilot:

```sh
.venv/bin/python scripts/durable_deployment.py prepare
# Import/verify/activate the local Vector snapshot before the API is ready.
# Import steps need UPSTASH_VECTOR_WRITE_TOKEN; serving searches use the read token.
.venv/bin/python scripts/durable_deployment.py up --build
```

The pilot uses `http://localhost:3000`, API port `18001`, application tables
`reveal_workflow_local_*`, job namespace `reveal-workflow-local`, and Vector
environment `local`. It does not overwrite existing users, accounts, jobs or
publications in `reveal_*`. Only API and frontend containers run. The local
QStash server schedules requests separately; the actual managed Redis and Vector
services are used.

If the embedding service moves, local and QA query traffic can explicitly opt in with
`REVEAL_QUERY_EMBEDDING_SERVICE_URL` in the backend environment. This must be an
HTTPS base URL without credentials, query parameters or a fragment, and is
accepted only with `REVEAL_APPLICATION_TABLE_PREFIX=reveal_workflow_local` or
`reveal_workflow_qa`. Production and other prefixes reject the override.
Before enabling it, retain a compatibility report comparing representative exact
stored texts/vectors from every active corpus against the replacement endpoint:
dimensions must match and every cosine must meet the reload calibration threshold
(`0.999`). A successful health response alone is insufficient. Keep using the
pinned model/provider and update the runtime `EMBEDDING_SERVICE_API_KEY` if needed.
The opt-in changes only the transport for new query embeddings; frozen runs,
snapshots, generations and embedding-space identities remain unchanged. Runtime
query caches are scoped by both the frozen space and the selected endpoint;
stored vectors still bypass the service. Updating `EMBEDDING_SERVICE_URL` alone
does not override a frozen query endpoint. For QA, set the override and the service
URL in `qa.env` in `deploy/dig/service.yaml`; the API key is already passed through
QA's dedicated secret record as `EMBEDDING_SERVICE_API_KEY`. A root `.env` URL alone
does not change QA deployment settings. Recreate the local API or deploy QA after
an approved runtime configuration change; do not rewrite the shared embedding pins.

Stop the older legacy frontend before starting this stack if it already occupies
port `3000`. Local OAuth callback URLs are
`http://localhost:3000/api/auth/callback/google` and
`http://localhost:3000/api/auth/callback/orcid`. If choosing another port with
`--frontend-port`, register matching callback URLs with the providers.

For managed QStash against a local HTTPS tunnel, configure the public
`REVEAL_WORKFLOW_URL` and use `--scheduler managed`. The tunnel is a development
callback path; cloud deployment uses the stable DIG HTTPS endpoint.

After startup, reconciliation can be inspected/reapplied idempotently, or
removed with `--remove` once the stack is stopped:

```sh
.venv/bin/python scripts/configure_workflow_schedule.py \
  --env-file .runtime/workflow/backend.env --host --apply
```

## Release and rollback

`deploy/compose.yaml` is the worker-free deployment. The former pool is retained
in `deploy/compose.legacy.yaml`, used only by `scripts/local_deployment.py` during
migration. Do not run it with managed notification Redis as its queue.

The DIG manifest uses the standard HTTP service and versioned callback route.
QA has separate `reveal_workflow_qa_*` tables, job/event namespace, `qa/` S3 write
prefix and Vector environment. Production uses the existing application records.
The release renderer emits only the HTTP service; it no longer emits a background
worker stack. Production remains subject to the platform's environment gate.

File-based phases share an RDS scratch concurrency limit of two. Local and cloud
containers use `TMPDIR=/work`, a 1 GiB work
tmpfs and a 256 MiB checkpoint limit. Restore participates in the bounded step
deadline; cancellation drains the restore thread before deleting its directory.
REVEAL requests a 300-second target-group drain and a 120-second process stop
window, while other DIG services retain their existing defaults. The API runs
`python -m reveal_backend.serve`. On SIGTERM it first ends open workspace and
job event streams, which clients renew with their cursors exactly as at the
240-second window, and answers new streams with the retryable 503. It then
waits only for in-flight requests such as workflow steps, bounded by
`REVEAL_GRACEFUL_SHUTDOWN_SECONDS` (default 110, inside the stop window).
Previously each open stream held shutdown until SIGKILL, so a local restart
with a browser tab open refused connections for up to 120 seconds.

The shared QA and production ALBs currently retain their 60-second idle timeout;
the nginx timeout configuration has not been inspected. Setting a 420-second
QStash delivery timeout does not extend those ingress limits. Lost-response
recovery has been exercised through the local public tunnel, while the cloud
acceptance probes cover short callbacks, durable waits and SSE. Long scientific
phases through the complete cloud ingress path remain a separate qualification
check; no shared timeout setting was changed by this release.

Prepare the private environment files without changing remote configuration:

```sh
.venv/bin/python scripts/workflow_cloud_config.py qa
.venv/bin/python scripts/workflow_cloud_config.py prod
```

The helper follows `deploy/dig/service.yaml`, reuses the saved environment keys,
and refuses an accidental credential rotation. Only the frontend environment
file belongs in Vercel. Backend secrets are bound through AWS Secrets Manager.

Before production cutover, drain legacy authoring and cleanup, activate the
verified production Vector registry, enable workflow routing for new jobs, then
remove legacy services. Existing jobs retain their transport and generation;
legacy claimers exclude workflow-owned records. Keep endpoint v1 available until
its runs finish. Roll back future routing independently from existing workflow
recovery and Vector snapshot activation; never let a legacy worker claim an
in-progress workflow job.

After deploying independent cleanup, rollback must either drain all pending
`workflow_cleanup` obligations or retain the signed cleanup consumer and its
reconciliation dispatch. A terminal scientific job or released execution reservation does not prove its
Box has been deleted. Inspect pending cleanup records and provider status too.

## Verification status

Tests and live probes are recorded in the ignored `.runtime/workflow` directory.
Unit tests do not prove a cloud release or a paid scientific run. Record actual
deployment URLs, image/source revisions, live callback and browser results at
release time; do not infer those outcomes from a successful template render.

September 30 local checks completed: five non-scientific local-QStash probes,
including two jobs resumed after forced API replacement; one managed-QStash
probe through an HTTPS tunnel; and real Redis fanout with no idle read commands.
The active local Vector snapshot passed all 24 frozen reference queries with
minimum recall@10 and top-result agreement of 1.0, and maximum cosine error
`3.4934e-7`. Its verified inventory contains 1,756 mapped factor aliases and
20,588 context bindings; 2,281 unmapped factor bindings are archived separately
in immutable S3. The real browser gateway delivered create/rename/delete events
to two tabs, with zero collection requests over 40 seconds idle and no browser
errors. Before the capture optimizations above, the backend suite passed 690
tests (8 skipped, 299 subtests), and the frontend passed typecheck and all 69
tests. Focused resource/cancellation checks
passed 40 tests, and QA configuration guards passed 30 tests. These counts are
test results; scientific-run and cloud acceptance are recorded separately.

A subsequent managed Vector workflow verified two bindings, both import batches,
the exact inventory and two frozen quality probes without changing the serving
snapshot. A non-scientific managed workflow also passed lost-response retry and
duplicate-history injection through the actual signed HTTP route: application
state and event hashes stayed unchanged, and the original generation completed
with exactly two recorded phases and restored S3 evidence. No Box or model call
was used by that failure-injection probe.

An earlier QA release used source `cf87246e1aedf06afa3deedd023a09a19e002d3f` and platform image
revision `c563f40e1061c408459081310f4dc534f0038680`, deployed by
[platform run 36657927190](https://github.com/broadinstitute/dig-service-platform/actions/runs/36657927190)
at `https://api-qa.hugeampkpnbi.org/api/reveal`. The platform suite passed 240 tests.
All 15 public HTTPS checks passed:
readiness, catalog and stored-context semantic retrieval, exact QA provenance,
anonymous authentication, table isolation, three pushed workspace mutations,
two-event reconnect replay and invalid-credential denials. Managed signed QA
probe `1e817d2a-d6bc-4558-b6d0-de3cf8ff9ce3` then survived task replacement in
generation 1 with exactly two phases: task `c0531f378af645e382c26b85f412c02c`
prepared its S3 checkpoint, and task `d027d3b88a0b4cf6b45310397070830c` restored
the same checksum and completed. It allocated no Box, left the active Vector
snapshot unchanged, and used no Redis read commands. The saved report is
`.runtime/workflow/qa-managed-replacement-final.json`. The QA managed
reconciliation schedule is installed. The
[read-only lifecycle audit](https://github.com/broadinstitute/dig-service-platform/actions/runs/36660817274)
reported that the old task was stopped and its replacement was then the sole
running task; both used task definition `svc-reveal-qa:2` and image digest
`sha256:b9f27ba0ebc2ed31cb0f3c4f8b7ec7218399a0347609a8a2d715d2e68536ff93`.
Its report is `.runtime/workflow/qa-replacement-completion-audit/report.json`;
the audit made no mutations.

The latest QA release uses source `649e54f837145e8112d07acf3a0dc77ded122d95` and platform revision `31bcfb51a9ca0230c5e0cb76f24fa91a49fee402`, deployed by [QA run 36697127927](https://github.com/broadinstitute/dig-service-platform/actions/runs/36697127927). The dk integration uses the [existing trusted gateway flow](application-gateway.md), with shared QA signing/service credentials delivered privately. Registered workspaces persist without a daily analysis cap. QA permits ten outstanding jobs per workspace while Box/scratch execution stays at two; production settings are unchanged. No additional application-key authentication layer was introduced.

The gateway helper and existing authorization/capacity behavior passed 118 focused tests and 11 subtests. All 240 platform tests, 557 export checksums and template lint passed; the QA render adds only the job-limit setting, while the production render is byte-identical. Local and public QA each passed 12 checks for persistent identity, distinct users, draft writes/idempotency, job input validation and authenticated SSE. The checks launched no research jobs or model calls. The deployment pipeline verified the expected task definition and public health. Reports: `.runtime/workflow/gateway-local-acceptance.json`, `.runtime/workflow/gateway-qa-acceptance.json`, `.runtime/workflow/gateway-qa-deployment-acceptance.json`.

The preceding workspace-key release used source `54b9a2cc086922abd0480765c50507c336b106ba`, exported as platform revision `bed2256496d2a6f1ac2df649c68bc350a9fd7e0d`, and was deployed to QA by [QA run 36691740277](https://github.com/broadinstitute/dig-service-platform/actions/runs/36691740277). [Workspace API keys](api-keys.md) now support direct draft/job access and authenticated event streams. Configuration stores only a key hash and existing owner UUID; QA and local keys are separate, and production key access is disabled. Swagger presents one ApplicationBearer input and retains the supported OpenAPI dialect.

Contract validation, 881 backend tests and 303 subtests (8 optional skips), 70 frontend tests, typecheck, 240 platform/service tests, and QA/prod template render/lint passed. All 556 exported file checksums match. Both the running local API and public QA API passed 15 live checks for identity/expiry, environment isolation, internal/admin denial, draft creation/editing/idempotency, job input validation and authenticated SSE. The deployed contract exactly matches the canonical contract apart from its mounted server prefix. Accepted job submission, quota, lifecycle and cross-owner boundaries are covered by behavioral tests; these live smoke checks created no research job or model call. Reports: `.runtime/workflow/api-key-local-acceptance.json`, `.runtime/workflow/api-key-qa-acceptance.json` and `.runtime/workflow/api-key-release.json`.

The preceding direct-transfer release, source `473aa6f4932d32be0de0045d5d103a81269968bd` and platform `baa1b581eb3bc2b2c644503fa7358321748e8b39`, passed [QA run 36685849303](https://github.com/broadinstitute/dig-service-platform/actions/runs/36685849303), template lint and all 15 public HTTPS acceptance checks, including semantic retrieval, authenticated SSE, reconnect replay and environment isolation. Unsigned cleanup callbacks returned 401.

On that preceding release, managed signed probe `ce4b4ebd-0691-4433-908b-625de20ae032` completed in generation 1 with exactly two durable phases, restoring the same S3 checksum on the deployed ECS task. It created no Box, made no model calls or Redis reads, and left the active Vector snapshot unchanged. No task replacement was requested for this probe. Reports are `.runtime/workflow/qa-direct-transfer-public.json`, `.runtime/workflow/qa-direct-transfer-managed.json` and `.runtime/workflow/qa-direct-transfer-cleanup-auth.json`.

[Production run 36692523152](https://github.com/broadinstitute/dig-service-platform/actions/runs/36692523152) requests the earlier QA-tested platform revision `bed2256496d2a6f1ac2df649c68bc350a9fd7e0d` with API-key access disabled and awaits required reviewer `sagehen03` (Drew Hite); the current operator cannot approve that environment. The older pending run `36689579490` was cancelled after creating this replacement request. The preceding read-only production preflight found no queued, running or cancellation-pending jobs. All 16 frontend production settings are already uploaded to Vercel as sensitive variables. Frontend deployment awaits production backend readiness.

Real Box direct-transfer and deletion probes passed locally. The deployed QA probe verifies task-role S3 checkpoint reads/writes and signed delivery; full cloud Box transfer and long scientific execution remain unverified. No new paid scientific run was made. Shared ingress limits are unchanged.

The first scientific pilot, before retirement of the AI reviewer, completed one
authoring attempt, durable capture and Box cleanup. After 31 acknowledged reviewer calls, its final response was not
acknowledged; the original HTTP status was not retained. Independent free
token-count requests reproduced rejection of the old final-decision tool schema
and acceptance of the corrected schema. The historical reviewer implementations
retain that schema and bounded provider-error diagnostics; live acceptance no
longer calls them.
The fix passed 82 focused tests and 35 subtests.

The original job remains `REVIEW_UNAVAILABLE`, with no accepted account or
paragraph. A private operator S3 download verified the captured account's hash;
the scientific artifact API correctly returns 404 until acceptance. Its sole
Box was deleted and capacity released. Acknowledged author/reviewer spend totals
$4.6584054; the unresolved $0.508677 final-call reservation remains accounted.
No authoring or paid review was repeated. Scientific acceptance after the fix
has not been verified by another paid run.

Provider references: [FastAPI integration](https://upstash.com/docs/workflow/quickstarts/fastapi),
[QStash regions](https://upstash.com/docs/qstash/howto/multi-region),
[Redis streaming Pub/Sub](https://upstash.com/docs/redis/features/restapi),
[Vector Python SDK](https://upstash.com/docs/vector/sdks/py/gettingstarted).
