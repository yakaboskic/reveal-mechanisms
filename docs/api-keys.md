# API keys for colleague access

An operator can issue an opaque `rvl_` key for one existing workspace. It uses the
same ownership, idempotency, quota and expiry checks as a browser session. It can
create and edit drafts, submit jobs, read results and consume progress events.
It cannot provision users, inspect administrator data or authorize Workflow
callbacks. The backend stores only the key's SHA-256 hash and workspace UUID in
environment configuration; research records never contain the raw key.

The initial QA handoff creates a separate anonymous workspace. It expires after
30 days and retains the configured anonymous allowance (currently five analyses
per day and two active jobs). Its exact expiry appears in the handoff file and
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
