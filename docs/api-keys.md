# API keys for workspace and administrative scientific access

The dk application integration uses the [trusted gateway credentials](application-gateway.md)
and registered-user sessions. The individual workspace-key mechanism below remains
available but is not needed for that integration.

An operator can issue an opaque `rvl_` key for one existing workspace. It uses the
same ownership, idempotency, quota and expiry checks as a browser session. It can
create and edit drafts, submit jobs, read results and consume progress events.
It cannot provision users, inspect administrator data or authorize Workflow
callbacks. The backend stores only the key's SHA-256 hash and workspace UUID in
environment configuration; research records never contain the raw key.

The initial QA handoff creates a separate anonymous workspace. It expires after
30 days and retains the configured anonymous daily allowance (five analyses
per day). QA permits ten outstanding jobs per workspace; the default for other
environments remains two. Its exact expiry appears in the handoff file and
`GET /v1/me`. Publishing results publicly still requires a registered owner.

## Use the key

Open [QA Swagger](https://api-qa.hugeampkpnbi.org/api/reveal/docs), click
**Authorize**, and paste the raw `rvl_` key under **ApplicationBearer**. Swagger
adds `Bearer` automatically. Start with `GET /v1/me` to check your workspace.

For scripts, read the provided handoff JSON (requires `jq`):

```bash
export REVEAL_API_URL="$(jq -r .api_url dk-qa.json)"
export REVEAL_API_KEY="$(jq -r .api_key dk-qa.json)"
curl --fail-with-body "$REVEAL_API_URL/v1/me" \
  -H "Authorization: Bearer $REVEAL_API_KEY"
```

Keep the key in the Authorization header, not a URL. The handoff JSON is the
credential to share privately with its intended holder. Gateway signing keys,
gateway service tokens and backend environment files stay with the operator.

## Save a draft and submit a job

Use current API records for scientific identifiers and source revisions. This
example selects the first returned gap and its suggested anchors; inspect those
choices before submitting a research job.

```bash
curl --fail-with-body "$REVEAL_API_URL/v1/knowledge-gaps?limit=3" > gaps.json
jq '.items[0] | {id:.object.id, source_id:.source.source_id,
  source_revision:.source.source_revision}' gaps.json > selected-gap.json

jq -n --slurpfile gap selected-gap.json '{source_gap:$gap[0],
  manual_eaggl_anchors:[], dismissed_source_ids:[], subquery:"",
  mode:"semantic", model:"cfde-inc-v2"}' > suggestions-input.json
curl --fail-with-body "$REVEAL_API_URL/v1/mechanisms/suggest" \
  -H 'Content-Type: application/json' \
  --data-binary @suggestions-input.json > suggestions.json

jq -n --slurpfile gap selected-gap.json --slurpfile s suggestions.json '
  {name:"API exploration", composer:{source_gap:$gap[0],
    eaggl_anchors:($s[0].automatic_anchors | map({reference:{
      source:.factor.source, source_id:.factor.source_id,
      source_revision:.factor.source_revision, dapper_id:.factor.object.id},
      origin:"automatic", suggestion_id:$s[0].suggestion_id})),
    dismissed_source_ids:[], mechanism_subquery:"", model:"cfde-inc-v2",
    selected_kgs:[]}}' > draft-input.json

DRAFT_REQUEST_ID="$(uuidgen)"
curl --fail-with-body "$REVEAL_API_URL/v1/drafts" \
  -H "Authorization: Bearer $REVEAL_API_KEY" \
  -H "Idempotency-Key: $DRAFT_REQUEST_ID" -H 'Content-Type: application/json' \
  --data-binary @draft-input.json > draft.json

jq '{kind:"analysis", draft_id:.id, draft_version:.version}' draft.json > job-input.json
JOB_REQUEST_ID="$(uuidgen)"
# This request starts research execution and consumes the configured model budget.
curl --fail-with-body "$REVEAL_API_URL/v1/jobs" \
  -H "Authorization: Bearer $REVEAL_API_KEY" \
  -H "Idempotency-Key: $JOB_REQUEST_ID" -H 'Content-Type: application/json' \
  --data-binary @job-input.json > job.json
```

An empty draft is allowed, but job submission requires a selected gap and at
least one anchor. Reuse the same idempotency key and body when retrying an
ambiguous response. Use a new key for a new action. Draft updates also require
the latest `expected_version`; job submission freezes that saved draft version.

## Receive progress through events

```bash
JOB_ID="$(jq -r .id job.json)"
curl --no-buffer --fail-with-body "$REVEAL_API_URL/v1/jobs/$JOB_ID/events" \
  -H "Authorization: Bearer $REVEAL_API_KEY" -H 'Accept: text/event-stream'
```

Save each event's `id`. When the bounded stream closes, reconnect with
`Last-Event-ID: <last-received-id>` to replay only subsequent events. Stop
reconnecting after a terminal job event. The same bearer works for
`GET /v1/me/workspace/events`. These streams use Pub/Sub wakeups and durable
replay; clients do not need to poll Redis or job status. To request cancellation,
send `POST /v1/jobs/{job_id}/cancel` with the same Authorization header.

## Operator issuance and rotation

The operator CLI creates an anonymous workspace through the separately trusted
provisioning endpoint. It writes the raw key only to a private handoff file and
optionally writes the two hash/owner configuration fields to a separate file.
It never uploads secrets or changes a running deployment.

```bash
install -d -m 700 .runtime/api-keys
.venv/bin/python scripts/issue_api_key.py \
  --env-file .runtime/workflow/qa-backend.env \
  --api-url https://api-qa.hugeampkpnbi.org/api/reveal \
  --label dk --output .runtime/api-keys/dk-qa.json \
  --config-output .runtime/workflow/qa-api-key-config.json
```

Install `REVEAL_API_KEY_SHA256` and `REVEAL_API_KEY_USER_ID` together in the
matching environment's backend secret, reconcile the saved configuration, and
roll out the service. The cloud preparation helper reads only that environment's
`{environment}-api-key-config.json`; it does not copy a local key into QA or
production. An absent record disables API-key access. The frontend receives
neither setting. The local Docker backend can use its own independent pair.

For rotation, issue into new output paths with `--owner-user-id <existing-uuid>`,
then replace the configured pair and roll out. Ownership and saved work stay
with the existing UUID. To revoke, blank both settings and complete the rollout;
old tasks must drain before revocation is complete. Existing event streams are
bounded to at most four minutes and reauthorize before replaying further data.
Retired or expired principals are rejected even if their key remains configured.
The current deployment supports one configured colleague key per environment.

## Administrative scientific read key

A separate `rvl_admin_` key has the fixed `science:read` scope. It can list and
retrieve scientific accounts and explorations across all owners in its
environment, including unpublished records. It does not act as a user and
cannot create or modify records, run research, publish science, use MCP tools,
download evidence exports, inspect arbitrary database tables, or read telemetry.
Existing workspace keys and browser sessions cannot use these administrative
endpoints. The key is intended only for trusted operators with access to all
private science in that environment.

The four supported requests are:

| Request | Result |
| --- | --- |
| `GET /v1/admin/accounts` | A bounded page of scientific accounts across owners |
| `GET /v1/admin/accounts/{record_id}` | One account identified by its owner-specific stored record ID |
| `GET /v1/admin/explorations` | A bounded page of explorations across owners |
| `GET /v1/admin/explorations/{record_id}` | One exploration identified by its stored record ID |

Lists default to `visibility=private`, covering unpublished records. Use
`visibility=public` or `visibility=all` when needed. Pass `limit` (1–100) to bound
each page and follow the returned cursor to continue. Cursors are tied to the
credential, record type, visibility filter, and collection revision; changing
those requires a fresh first page. Use each item's `record_id`
for retrieval: a scientific account's DAPPER object ID can occur under different
owners and must not be used to guess another owner's stored record ID.

Each list item contains `record_id`, `owner_user_id`, and a `summary`. Account
record IDs are SHA-256 identifiers; exploration record IDs are UUIDs. Account
detail adds `result`, preserving the saved account envelope, and `paragraph`,
the saved linked statement or `null`. Exploration detail adds its full saved
`result`. Internal artifacts and artifact download URLs are excluded, and
`publication.can_manage` remains `false` for this read credential.
Saved account and paragraph envelopes retain their original coverage bounds;
this endpoint does not reconstruct omitted graph records or re-query sources.
Artifact URLs and owner-bound continuation cursors are cleared. A published
account with an unpublished statement update is included by `visibility=all`
or `public`; inspect `publication.has_unpublished_changes` for that case.

Read the credential from its private handoff file, keeping it out of URLs:

```bash
export REVEAL_API_URL="$(jq -r .api_url admin-read-qa.json)"
export REVEAL_ADMIN_READ_API_KEY="$(jq -r .api_key admin-read-qa.json)"
curl --fail-with-body "$REVEAL_API_URL/v1/admin/accounts?visibility=private&limit=25" \
  -H "Authorization: Bearer $REVEAL_ADMIN_READ_API_KEY"
curl --fail-with-body "$REVEAL_API_URL/v1/admin/explorations?visibility=private&limit=25" \
  -H "Authorization: Bearer $REVEAL_ADMIN_READ_API_KEY"
```

For Swagger, use the separate **AdminReadBearer** authorization
scheme. Do not supply this key to ApplicationBearer, a browser session, or an
agent workspace. Responses contain private science and use `Cache-Control:
private, no-store`.

## Issue, rotate, or revoke an administrative read key

The dedicated command generates a key locally and writes two new private files.
It makes no network requests, creates no principal, reads no scientific records,
and changes no deployment. The environment file is used only to verify the API
URL against that environment's configured Workflow callback. Raw keys are
written only to the handoff file, never stdout or the backend configuration.

```bash
install -d -m 700 .runtime/api-keys
.venv/bin/python scripts/issue_admin_read_api_key.py \
  --env-file .runtime/workflow/qa-backend.env \
  --api-url https://api-qa.hugeampkpnbi.org/api/reveal \
  --label qa-science-review \
  --output .runtime/api-keys/admin-read-qa.json \
  --config-output .runtime/workflow/qa-admin-read-api-key-config.json
```

Both output directories must already be owner-only (`0700`); both output files
must be new and are created with mode `0600`. Symlinks and existing files are
refused. The handoff includes the API URL, raw key, nonsecret credential UUID,
fixed scope, creation time, and operator label. The configuration file contains
only `REVEAL_ADMIN_READ_API_KEY_SHA256` and `REVEAL_ADMIN_READ_API_KEY_ID`. That
UUID identifies the credential for auditing; it is not an application user ID.

Install the pair together in the matching backend environment secret, explicitly
reconcile the saved backend secret, and roll out the service. The QA service
manifest includes both secret references; install even a disabled pair of empty
strings before deploying a task that references them. Production has no admin
read secret references and remains disabled. Enabling another environment
requires both an explicit private `{environment}-admin-read-api-key-config.json`
record and both corresponding manifest secret references. Cloud preparation
refuses an enabled record without those references. It never inherits this
credential from shared local configuration and never sends either field to the
frontend.

There is one administrative read credential per environment, separate from the
owner-scoped colleague key. It has no automatic expiry. For rotation, generate
new private output paths, install the replacement hash and new credential UUID,
update the environment-specific configuration record, and roll out. To revoke,
blank both fields in that record and the backend secret and complete a rollout.
Revocation and rotation are complete only after old service tasks have drained.
Do not delete or overwrite the previous handoff until its retirement is verified.
