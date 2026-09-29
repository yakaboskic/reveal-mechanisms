# Database and evidence preparation performance

The local API and worker connect to Aurora in `us-east-1`. On the development
connection measured on 2026-09-28, a single query took roughly 140–200 ms while
opening and verifying a new TLS session cost about 1.5 seconds. Serial requests
and repeated connection setup dominated the measured delays.

## Application database reads

The API and worker each keep a small, process-local pool for application
repository transactions. Each connection has one borrower at a time. Returning
a connection resets the MySQL session, reselects the configured database, and
restores transaction isolation, UTC, UTF-8 and autocommit settings. Failed or
uncertain sessions are discarded; SQL is never automatically replayed. Existing
read-only snapshots, principal checks and exclusive write fences remain in use.
Imports and migrations continue to use direct connections.

Optional process environment settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `REVEAL_MYSQL_POOL_SIZE` | `4` | Connections per process; `0` disables pooling, maximum `32`. |
| `REVEAL_MYSQL_POOL_WAIT_SECONDS` | `5` | Maximum wait for an available lease, up to `30` seconds. |

Idle connections expire after 60 seconds and all connections after five minutes.
Changing database credentials or the CA file invalidates the process pool. The
first request after startup or expiry still pays connection setup cost. Pooling
does not cache private data, authorization decisions or publication visibility.

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
