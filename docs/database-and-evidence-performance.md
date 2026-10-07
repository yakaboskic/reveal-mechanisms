# Database and evidence preparation performance

The local API and worker connect to Aurora in `us-east-1`. On the development
connection measured on 2026-09-28, a single query took roughly 140–200 ms while
opening and verifying a new TLS session cost about 1.5 seconds. Serial requests
and repeated connection setup dominated the measured delays.

## Application database reads

The API and worker each keep a small, process-local pool for application
repository transactions. Each connection has one borrower at a time. A returned
connection goes back to the pool without any further server command only when
the OK packet of its final `COMMIT` or `ROLLBACK` shows no open transaction and
autocommit still off, and every statement on the lease was a plain `SELECT`,
`WITH`, `INSERT`, `UPDATE`, `DELETE` or `START TRANSACTION` without user
variables, user locks, temporary tables or similar session effects. Any other
lease, including one with a failed statement, is reset: the MySQL session is
reset, the configured database reselected, and transaction isolation, UTC,
UTF-8, autocommit and the session timeouts restored, all in one pipelined round
trip. Failed, interrupted or uncertain sessions are discarded; SQL is never
automatically replayed. Existing read-only snapshots, principal checks and
exclusive write fences remain in use. Imports and migrations continue to use
direct connections and keep the server's session defaults.

Pooled sessions wait at most 15 seconds for a row lock such as the global write
fence (`innodb_lock_wait_timeout`), and the server ends a pooled session that
sends nothing for 300 seconds (`wait_timeout`), which releases an orphaned fence
holder within minutes rather than hours. Idle expiry therefore stays at or below
240 seconds; connections idle longer than 60 seconds are checked with one
`COM_PING` (3 second deadline) before reuse instead of being reconnected. Idle
time includes host sleep.

Optional process environment settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `REVEAL_MYSQL_POOL_SIZE` | `10` | Connections per process; `0` disables pooling, maximum `32`. |
| `REVEAL_MYSQL_POOL_WAIT_SECONDS` | `5` | Maximum wait for an available lease, up to `30` seconds. |
| `REVEAL_MYSQL_POOL_IDLE_SECONDS` | `240` | Idle expiry; at most `240`, below the 300 second session `wait_timeout`. |
| `REVEAL_MYSQL_POOL_LIFETIME_SECONDS` | `3600` | Connection lifetime, shortened per connection by up to 10% jitter; between the idle expiry and `3600`. |
| `REVEAL_MYSQL_POOL_CLEAN_RELEASE` | `1` | `0` resets every returned connection. |
| `REVEAL_MYSQL_POOL_PIPELINED_RESET` | `1` | `0` sends the three reset commands one round trip at a time, for example behind a proxy. |

Changing database credentials, the CA file or these settings invalidates the
process pool. The first request after startup or expiry still pays connection
setup cost. A new pooled connection sends its session settings with the
handshake, and verified TLS is checked on the client from the completed,
certificate- and host-verified handshake, so setup needs two statements instead
of four. Pooling does not cache private data, authorization decisions or
publication visibility.

At most two write transactions per table prefix and process hold a pooled
connection on the global write fence: one holding it and one queued behind it.
Further writers wait inside the process, not on a pooled connection, so readers
keep their connections when writers contend. That wait is bounded by the same
15 second session lock wait, not by `REVEAL_MYSQL_POOL_WAIT_SECONDS`, so a
queued writer never fails sooner than it would have on the fence itself, even
while another process holds the fence for longer than the pool wait. A writer
that is still not admitted after 15 seconds, a writer whose fence statement
waits past that lock wait (MySQL error 1205, raised before its body runs), or a
request that runs out of pooled connections, raises `DatabaseBusy`, a
`TimeoutError`, which the API returns as the existing retryable 503
`SERVICE_UNAVAILABLE` and logs as `Database busy`, not as an unhandled failure.
Clients retry with their idempotency key. The database row lock remains the
only cross-process authority. Event streams survive a busy pool: when the
workspace replay or the job event read gets `DatabaseBusy`, the stream sends a
heartbeat comment, waits 0.5-2 seconds (never past its window) and reads the
same cursor again, instead of ending.

A read that is genuinely one statement, such as readiness, uses
`Repository.single_read()`: one plain `SELECT` with no `START TRANSACTION`,
ended by `ROLLBACK`, or two round trips on a clean pooled connection. It
rejects a second statement and any locking read, user variable, user lock or
`INTO` before sending it. Authorization followed by data reads keep
`read_transaction()`'s single snapshot. Within any transaction, a row already
read is not read again before it is updated, removed or read a second time, and
new workspace events and notification outbox rows are inserted without a
lookup. Rows the transaction wrote itself are always read back from the
database.

A write that changes a tracked record commits its workspace events and one
notification outbox row inside its own fenced transaction. That bookkeeping is
one cursor `SELECT`, one `UPDATE` per changed audience and one multi-row
`INSERT`, however many records changed; previously each change cost three
statements under the lock, so a 65-grant acceptance sent about 200. The writer
no longer waits for Redis or takes the fence a second time. After `COMMIT`, one
background thread per process `PUBLISH`es the wakeups, coalescing whatever
queued meanwhile into one pipeline, then deletes the published outbox rows with
one `DELETE` outside the write fence, under `READ COMMITTED` so that a row
reconciliation already removed takes no gap lock. Outbox rows are inserted once
and never updated, so a lost or repeated delete only repeats a wakeup. If Redis
or the delete fails, the 10,000-entry queue is full, or the process exits
first, the row stays and the scheduled reconciliation publishes it again:
delivery remains at least once. One HTTPS client per process keeps its Upstash
connection for 55 seconds (httpx defaults to 5) and retries a POST once when
the provider closed the kept-alive socket. `REVEAL_NOTIFICATION_DELIVERY=inline`
publishes before the writer returns, as SQLite repositories always do. Admin
telemetry reports the queue as `notifications.publish_queue` and publish time
as the `notification`/`PUBLISH` row. A draft rename is now one fenced
transaction of 10 round trips instead of two fenced transactions totalling 14
plus a Redis call.

Local research serves each request from one snapshot. A local-work status poll
is one read transaction of five round trips: the principal and the work in one
read, the frozen request, one read of the work's operations and grants, and
`COMMIT`. Received operations and operations whose lease expired are resumed
from those rows, not from a second read. The local-work list reads every work's
operations, grants, requests, cited evidence and OAuth families in a fixed
number of statements, however many works the owner has. An MCP read tool
authenticates once, inside the transaction that serves it, and an
authentication failure still returns the HTTP challenge. Write tools are still
checked by the transport before dispatch, outside the global write fence, so
an invalid credential never takes it. A data tool's background run takes the
fence twice: once to lease the operation and once to commit its artifacts,
receipt and result together, re-authorized under the lease. Between the two
come one authorized read and the blob uploads, outside the lock. With two
captured files that is 17 round trips, 10 of them under the fence, instead of
54 with 28. Operation quotas are counted in SQL, so stored results are no
longer read under the fence. The 30 second research recovery loop reads one
snapshot: local works, their principals and closed works' pins, and every
unsettled operation. It resumes received and lease-expired operations from that
snapshot. It takes the write fence only when a work must close or an idle pin
must be released, re-reading and deciding each one again under the fence. It
uses `FOR UPDATE NOWAIT`, so a fence held by another writer defers those
changes to the next cycle instead of queueing. A closed work and a released pin
are no longer rewritten every cycle. In steady state a cycle is one read of
five round trips, instead of a fenced transaction plus one read per work.

Workspace reads never take the global write fence. `GET /v1/me` is one
principal read (two round trips); the draft, job, research-request and
exploration lists and the draft and request details are one read snapshot of
four, so a workspace tab's parallel requests no longer queue behind each other
or behind writers. Listing drafts no longer runs expiry cleanup: it was three
extra owner-wide lists under the fence on every call. Managed reconciliation
finds expired editors and uploads and stale executions by id in one read
snapshot, and takes the fence, with `NOWAIT`, only when something is due,
re-reading and deciding each candidate again under it. An idle tick is one read
of four round trips instead of a fenced cleanup plus one fenced transaction per
unfinished execution; a held fence defers the work to the next tick. Async
handlers read the request body on the event loop and run validation, catalog
loads and their transactions in the threadpool, so a writer waiting on the
fence no longer stalls every other request in the process. Job submission,
cancellation and review retry reply before Workflow delivery, which then reads
the job's intents in one statement with one QStash client.

Each API request logs one JSON line to stdout with the route template (never
the raw path, query string, ids or parameters), status, duration, and its
database cost: statements (including `COMMIT`, `ROLLBACK` and the fence),
database milliseconds, pool and writer wait, global-lock wait, new connections
and session resets. Set `REVEAL_ACCESS_LOG=0` to turn it off. The admin
telemetry adds per-route means of the same fields and these database rows:
`ACQUIRE` (pool queue wait), `WRITER_WAIT`, `CONNECT`, `PING`, `RESET`,
`RESET_SKIPPED`, `FENCE` (lock wait plus one round trip), `COMMIT`,
`ROLLBACK` and `LOCK_HOLD` (fence grant to commit). Statements on direct
connections opened outside the pool are not counted; their connects are.
`services/backend/tests/test_round_trip_budget.py` holds the round trips of the
hottest routes; lower its budgets with each change that removes round trips.

Scientific account reads batch account, publication and document-index records,
then load the complete stored scientific document. This reduces data queries
from seven to three while retaining provenance pagination and exact payload
checks. Readiness uses a bounded table read rather than counting every record.

Knowledge-gap discovery (the gap list, gap search, a gap and its accounts, and
`/v1/accounts`) reads one snapshot with one principal lookup. A browse over the
whole catalog reads vote state in one statement: the stored vote totals plus
the registered viewer's own ballots, since only voted targets have rows. A row
counts only at its canonical key, so ballots re-owned by a workspace transfer
stay excluded as before. Small target sets, including a vote under the write
fence, still read their exact keys. A registered browse of the 3,367-gap
catalog dropped from 2 leases and about 34 round trips to 1 lease and 5.
Cursors for the gap list, gap search and mechanism search bind compact rows
(identities, revisions, counts, votes, mechanism counts and ranking) instead of
deep-copying and hashing every matching item, which cut about 105 ms of CPU from
each gap-list page and about 1.3 seconds from a hybrid mechanism search over
1,000 semantic candidates. A cursor issued before this change expires once.
Fuzzy gap search scores each distinct catalog word at most once per query and
skips words whose difflib upper bounds cannot reach the 0.7 threshold, using a
word index built with each catalog load (about 35 ms and 14 MB). Results and
scores are identical; a search takes about 15 ms of CPU instead of 0.3-0.9 s.

Citation rendering (`POST /v1/citations/render`) reads one snapshot instead of
taking the global write fence. It no longer writes the unread
`citation_rendering` row, and it reads every cited revision, grant and
dependency row in one statement: 5 round trips instead of 45 for the fixture
paragraph with 13 citations. The CSL engine runs after the connection is
returned, off the event loop, on one per-process citeproc thread that keeps
the processor warm: about 55 ms per APA render instead of about 2.4 seconds
spent building the APA engine each time. The first APA render in a process
still builds it once.

Mechanism suggestions (`POST /v1/mechanisms/suggest`) write their uuid-keyed
audit row with one `INSERT` and `COMMIT` outside the global write fence
(`Repository.append`), before the response hands out its id. The row is
immutable, untracked and read only by a later fenced draft write, so the fence
protected nothing; it made each Composer gap open wait behind every other
writer, including that open's own exploration write. A suggestion is now two
round trips with no lock wait instead of a fenced transaction of three.

A CFDE assessment POST, fired 1.5 seconds after each settled Composer edit,
reads its principal, draft, idempotency, cache and upload rows in one statement
and the receipts they name in a second, then rejects (replays, version
conflicts, busy, unconfigured) without the write fence. Otherwise it re-reads
those keys in one statement under the fence, counts the daily quota only for a
new attempt, and inserts every new row in one statement. A private miss is 8
round trips with 4 under the fence instead of 17 and 11; a busy or
unconfigured check takes no fence at all. A status
poll reads the principal, draft and receipt in one statement (3 round trips;
4 when it follows a shared forecast). `?wait=` long-polls a pending receipt,
holding no connection while it waits, so a client re-reads only when the status
changes. The worker batches each transaction's reads in one statement: 17 round
trips per private attempt, 10 of them under the fence, instead of 25 and 17.

## Reference reads

Request-path reads of the imported reference tables (the factor and gene-set
pages, the research data tools and public research queries, archived factor
lookups) borrow sessions from the same process pool instead of opening a new
verified-TLS connection each time (about 1.2 seconds from the development
laptop). They are plain `SELECT`s ended by `ROLLBACK`, so the session returns
to the pool with no reset, and at most half of the pool may serve them at once,
so repository transactions always keep the rest. The catalog cold load, worker
evidence collection, importers and migrations keep direct connections. A
factor's import index and loading summaries cannot change for a complete
import or generation, so each process reads them once; the active gnomAD pin
is re-read at most every 5 seconds. The gene summary counts loadings without
ranking them, and a loading search returns its match count with the page
instead of ranking the factor a second time. Measured from the laptop on
2026-10-07 with the real code: a factor detail took 1.85 seconds before, 0.90
seconds on a warm pool with nothing cached, and no database work once cached;
a searched loadings page took 1.99, 0.85 and 0.34 seconds.

A research factor lookup resolves its id through the unique keys on
`reference_factors` and joins the import row by its SHA-256 factor id, instead
of an `OR` that scanned all 4,037 factors of the generation and of the import
(about 12 ms of server time per call). A CFDE assessment build reads its
generation once and all of its KPN anchors at once: one statement for the
factors, one for every anchor's top 50 genes and one for every anchor's top 50
gene sets, each still bounded per anchor and ordered exactly as before. With
the gene-set definitions and collections that is six `SELECT`s on one pooled
session for any number of anchors, instead of 48 for five anchors (93 for
ten) on a new TLS connection. The build's deadline bounds each socket read on
the borrowed session and is restored before the session returns to the pool;
past the deadline the session is dropped, never returned. Measured read-only
for five real anchors on one connection from the laptop: the anchor reads took
7.1-8.2 seconds before and 0.5-0.8 seconds after, with identical rows.

The catalog reads the active reference generation and the active Vector
snapshot together, in one statement, and pins that snapshot with the loaded
generation. Semantic search and suggestions therefore read no pointer at all
once the snapshot's index is built; a suggestion previously re-read the Vector
pointer three times (three read transactions, about 2.4 seconds from the
laptop), and a semantic mechanism search twice. A Vector-only activation is
picked up by the catalog's poller within the same 5 second check as a
generation change, and reloads the catalog. Each suggestion or search resolves
the index once, so its hits, context inputs and provenance always come from one
snapshot. The first semantic request after a load builds the index once:
concurrent requests wait for that build (at most 60 seconds) instead of each
reading the 7-10 MB serving subset on its own pooled connection, and a failed
build is reported to them, not repeated.

`/readyz` (the ALB health path and the compose healthcheck) answers from
memory. A readiness monitor in each API process reads the application database
and both active pointers in one statement every 5 seconds (two round trips;
the catalog's poller reuses that read instead of making its own), and
re-verifies the sources (reference tables on a pooled session, the verified
Vector snapshot, the Vector provider and the artifact bucket) in its own
thread whenever the pointers change, at least once a minute, and at the next
5 second check after a failed verification. The snapshot
summary is projected from the 31-53 MB row again only when that row's version
changes. A probe therefore costs no database, S3 or Upstash call; before, each
probe read the database and both pointers and called S3 twice (3.1-3.3 seconds
from the laptop in the 2026-10-06 audit), and every other probe also opened a
direct TLS connection, ran five reference queries, projected the snapshot row
and called Upstash (6.6-8 seconds). Readiness still fails closed: a failed
database read or verification answers 503 until the next successful one,
within one 5 second interval, and so does a monitor whose last read is more
than 15 seconds old. A busy pool (`DatabaseBusy`) is load, not an outage, so a
load spike does not take the task out of the ALB: while every check since the
last good read found the pool busy and the monitor still ticks, that read keeps
answering for up to 60 seconds, and a busy verification keeps the last verified
sources (still at most 3 minutes old) and is retried at the next check. Any
other failure, such as a refused connection or a failed TLS or credential
check, answers 503 at once. Right after a cutover the last verified sources are
reported for up to two minutes while the new pointers are verified.
`REVEAL_READINESS_MONITOR=0` turns the monitor off; probes then check
synchronously as before. `/healthz` is unchanged.

The API process also loads the catalog and the full DisMech mechanism corpus in
a background thread at startup (`REVEAL_CATALOG_WARMUP`, on by default), so
the first gap list, suggestion, draft or mechanism search no longer pays the
cold load itself (17-29 seconds measured from the laptop) and requests that
arrive meanwhile wait only for its remainder. A failed warmup changes nothing:
the first request loads inline and fails closed. Nothing built is persisted.
KPN trait metadata is read once per trait (711 rows) instead of being joined
onto every one of the 4,037 factors, which removes about 6.6 MB from each KPN
cold load.

The research `get_gene_factors` query joins from the gene by symbol, through
the loadings' `(import_id, gene_index)` index, to its factors. A `JOIN_ORDER`
hint fixes that order: unhinted, the optimizer's 10% guess for the TEXT symbol
filter made it drive from all 4,037 factors and read every one of the import's
2.4 million loadings (25-29 seconds cold in QA) to return a few dozen rows.
Migration `010_eaggl_gene_symbol_index.sql` adds an online
`(import_id, symbol(64))` index so the gene lookup reads one row instead of the
import's 18,477 genes; `resolve_gene` uses it too. The reader is correct with
or without the index, and migration 010 is applied by an operator, not by the
application.

Read-only measurements included transaction completion and pool reset:

| Operation | Before | After |
| --- | ---: | ---: |
| Simple database transaction, median | 1.85 s | 0.90 s with a warm pool |
| Existing account read | 4.39 s | 2.01 s; subsequent read 1.34 s |
| Public gap-count lookup | 2.01 s | 0.86 s |
| Workspace gap-count lookup | 1.93 s | 1.04 s |

These are development-connection samples, not latency guarantees or browser
page-load benchmarks. The first pooled transaction measured 2.41 seconds.

On 2026-10-07 (about 130 ms round trip from the development laptop), a warm
one-query read transaction took a median 785 ms with the previous three-step
reset, 523 ms with the pipelined reset on every return, and 408 ms with the
clean return (20 interleaved samples each). A new pooled connection took 938 ms
instead of 1,266 ms.

## Evidence collection

Independent source catalog, factor and trait reads run with at most four
concurrent requests. Dependent alias resolution, ranking and graph selection
retain their original order. Request counts, retries, byte limits, candidate
bounds, exact response captures and scientific validation are unchanged.

Replaying an existing job's 35 saved requests with their observed delays took
38.5 seconds with the previous scheduling and 15.3 seconds with bounded parallel
reads, about 60% less time. Each benchmark used a fresh DAPPER validator. All
86 output files matched byte-for-byte between scheduling variants. This was an
offline replay, not another paid agent run or a live upstream-service benchmark.

Each collection writes a best-effort `collection-timings.json` alongside its
capture directory. It records catalog, connection, source-observation, alias
resolution and validated assembly durations. It is outside the scientific
package and cannot change evidence hashes. A telemetry write failure does not
replace a source error or invalidate an otherwise successful package.

Analysis submission (`POST /v1/jobs`) keeps one fenced transaction. It reads the
principal, idempotency key, reference-reload gate, draft and draft binding in one
statement and lists the owner's jobs once for both quotas. The request is frozen
as progressive before its only write, and every new row (request, binding,
execution, Workflow dispatch, queue, first event, job, hosted local work, pin and
idempotency key) goes in one `INSERT`. An anonymous submission sent 31 statements
under the fence and now sends 6 (33 round trips to 8 with the fence and `COMMIT`); the
stored rows are the same except that the request's version, and its workspace
event revision, is 1 instead of 2. Draft creation inserts the draft, its binding
and the retry key together, a draft save prefetches its rows with the principal,
and a draft's anchors read a shared suggestion once: a rename is 8 round trips
instead of 10, an anchor save 9 instead of 12 and a new draft 7 instead of 13.
Selected uploads are read in one statement.

## Worker boundaries and remaining latency

Frozen preparation inputs are read together without taking the write lock.
Cancellation checks use consistent read-only snapshots. Evidence and the exact
dispatch checkpoint are saved in one fenced transaction, so recovery sees both
or neither. These database waits run outside the worker's event loop.

Captured source-file paths and checksums are verified before taking the final
account-save fence. Their metadata is then written in batches inside the same
atomic acceptance transaction. Existing rows and repeated checksums retain the
same update order, version increments and last-write behavior. A local database
replay of 84 captured file descriptors used four SQL statements instead of 168,
with identical final owners, versions, payloads and fixed timestamps. This is a
query-count comparison, not a new live acceptance timing.

Account acceptance (the Workflow commit step and MCP account submission) writes
every other row the same way. One statement reads every citation, grant, object,
observation, account, outbox and job row the commit can touch. Citation
registration, grants, object observations, objects and the account rows are then
planned in the order the per-row writes used and written by
`Transaction.apply_puts`: new rows in multi-row `INSERT`s, rows rewritten with an
unchanged payload in one version bump, and one `UPDATE` per changed row. Object
envelopes are projected before the write fence is taken; only their citation
metadata is attached under it. The 63-node fixture account sends 10 statements
under the fence instead of 576, so a laptop commit holds the fence for about 2
seconds instead of up to 174. An equivalence test replays the previous per-row
transaction against a copy of the database (a fresh owner, a re-accept, two
accounts or two documents of one account sharing nodes, another owner's
citations, borrowed and retained nodes, and box captures) and requires identical
rows: owners, versions, payloads, timestamps, job events, workspace events and
the notification outbox.

Paragraph acceptance projects its object envelope before taking the write fence
too (the first projection in a process loads the public DAPPER runtime, about
0.7 seconds on the laptop, which was spent holding the fence). Its pinned DAPPER
work (cited-text assembly, identity minting and the Paragraph lint) used to start
three cold `python -I -B` interpreters and verify the release three times, each
verification running five `git` processes and hashing the 35 locked files: about
4 seconds per commit on the laptop and 12.5-14.5 seconds of the 14-16 second
commit step in QA. Each API or worker process now keeps one warm helper
interpreter (`dapper_helper.py`) that loads the release's schema, vocabulary and
validator once and serves assembly, minting, paragraph linting and the reference
field map. It is still a separate `-I -B` interpreter, never the API process
(which imports another DAPPER snapshot), and it runs the per-call programs'
exact code: over the DAPPER examples and their failing variants, served in both
orders by one helper, minted bytes, lint findings, assembled text and reference
fields equal the per-call programs'. Every request still verifies the release
first, and a different release, an error, a crash, a timeout, 256 requests or
30 idle minutes restart it. Release verification still rehashes every locked
file on each call; only its five `git` checks are reused, while HEAD and the ref
it names, the refs, config, index, ignore and attribute files and every entry
under `schema/` keep the stat they had when the checks passed (status runs with
`GIT_OPTIONAL_LOCKS=0`, so it never rewrites the index). A commit's DAPPER work
now starts one interpreter and five `git` processes per process, then none:
4.0 seconds became 28 ms warm on the laptop (3.2 seconds for the first commit in
a process). The Workflow's launch and capture steps, and the legacy worker
before a Box run, warm the helper in a background thread while the Box works, so
the first commit takes about 70 ms. `REVEAL_DAPPER_HELPER=0` restores the
per-call interpreters; `REVEAL_DAPPER_PREWARM=0` leaves the first request to
start the helper.

Backend account validation (`validate_scientific_account`: each account the
analysis validate phase assembles, up to three per job, plus MCP submission and
hosted reuse checks) used to start one more cold interpreter per account. It now
runs in the same helper. The helper imports this backend's
`scientific_account_lint` after the pinned schema directories, as the per-call
script does, and calls the same `_lint` with its preloaded schema, an unmodified
stock vocabulary and the shared validator; release verification, the frozen
copy, every REVEAL check and the report are unchanged. Over ten real accepted
accounts, linted in final mode against the release, evidence package and tool
ledger that accepted them, the reports equal the per-call ones, and an account
went from 3.0 seconds (median, one interpreter each) to 82 ms warm on the laptop
(2.8 seconds for the first in a process, 0.2 seconds after the background
warmup). A lint input error is an ordinary report; a helper timeout, crash or
failed reply gives the same `linter-runtime` operational-error report as the
per-call program. The agent's lint tool in the Box, fixture seeding and the
scripts still start a fresh interpreter per call.

The inspected jobs also spent approximately 38–40 seconds setting up their Box
runtime. That delay and model/external-KG latency are separate from evidence
collection. A prebuilt toolchain snapshot (`REVEAL_BOX_TOOLCHAIN_SNAPSHOT`, see
durable-workflow-runtime.md) can skip the per-job toolchain install. Initial API source-catalog loading also remains a cold-start cost:
the deployment check took 61.6 seconds before the pinned catalogs were ready.
The API now pays it in a startup thread rather than on the first request.
The read benchmarks above apply after normal initialization. More Box RAM does
not remove the measured database and HTTP network
waits. Running the API and worker near Aurora would reduce the remaining database
round-trip cost; that deployment change was not part of this optimization.
