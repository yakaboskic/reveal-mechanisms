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
that is still not admitted after 15 seconds, or a request that runs out of
pooled connections, raises `DatabaseBusy`, a `TimeoutError`, which the API
returns as the existing retryable 503 `SERVICE_UNAVAILABLE`. The database row
lock remains the only cross-process authority.

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
query-count comparison, not a new live acceptance timing. Citation registration,
scientific object projection and the account/paragraph outbox remain unchanged.

The inspected jobs also spent approximately 38–40 seconds setting up their Box
runtime. That delay and model/external-KG latency are separate from evidence
collection. Initial API source-catalog loading also remains a cold-start cost:
the deployment check took 61.6 seconds before the pinned catalogs were ready.
The read benchmarks above apply after normal initialization. More Box RAM does
not remove the measured database and HTTP network
waits. Running the API and worker near Aurora would reduce the remaining database
round-trip cost; that deployment change was not part of this optimization.
