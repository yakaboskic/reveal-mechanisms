# Integrate a trusted application with the QA API

The dk application uses the same gateway credentials and request flow as the
REVEAL frontend server. The private `dk-qa.env` handoff contains the QA API URL,
gateway service token, signing secret, JWT issuer and audience. This is shared
QA gateway authority. Install it in the application server environment; browser
code receives neither shared credential.

API: https://api-qa.hugeampkpnbi.org/api/reveal

Interactive contract: https://api-qa.hugeampkpnbi.org/api/reveal/docs

## Establish a user session

Use a stable identity issuer for this application, such as
`urn:reveal:application:dk`, and a stable subject from the application's
authenticated server session. Each issuer/subject pair resolves to a persistent
registered workspace. Repeating that pair returns the same workspace. Different
subjects keep users' drafts and results separate. This identity issuer is distinct
from `REVEAL_GATEWAY_ISSUER`, which identifies the server signing API assertions.

The helper below implements and verifies the existing flow. It uses PyJWT and
python-dotenv from this repository's Python environment. Keep the environment
file and output directory private (mode 600 and 700 respectively).

```bash
mkdir -p .runtime/gateway-session
chmod 700 .runtime/gateway-session
.venv/bin/python scripts/gateway_session.py \
  --env-file /absolute/path/to/dk-qa.env \
  --issuer urn:reveal:application:dk \
  --subject user-123 \
  --output .runtime/gateway-session/session.json
```

The output contains `access_token`, `token_type`, `expires_in`, `principal` and
`api_url`. The token expires after five minutes. Run the same command with
`--replace` to renew it without creating another workspace. The helper does not
submit a research job.

To implement this in another backend:

1. Send `POST /internal/v1/principals/resolve` with
   `Authorization: Bearer <REVEAL_GATEWAY_SERVICE_TOKEN>` and this JSON:

   ```json
   {
     "issuer": "urn:reveal:application:dk",
     "subject": "user-123",
     "display_name": null,
     "email": null,
     "email_verified": false,
     "orcid": null,
     "orcid_authenticated": false
   }
   ```

2. Sign an HS256 JWT with the raw UTF-8 `REVEAL_GATEWAY_SECRET`. Set `sub` to the
   returned `user_id`, `principal_kind` to `registered`, `iss` and `aud` to the
   supplied gateway settings, integer `iat` to the current time, `exp` to
   `iat + 300`, and `jti` to a fresh UUID. Omit `purpose`.
3. Use `Authorization: Bearer <JWT>` on `/v1` research endpoints. For Swagger,
   paste that JWT into **ApplicationBearer**. Renew it before expiry.

Reference implementations are [gateway.ts](../services/frontend/src/lib/gateway.ts)
and [gateway_session.py](../scripts/gateway_session.py). The backend resolves
identities; it does not authenticate the integrating application's users for it.
Keep verified email and ORCID claims false unless the application actually
verified them. User names and profile values are optional.

## Drafts, jobs and progress

Registered workspaces do not expire and have no daily analysis cap. QA allows
ten outstanding jobs per workspace, including queued, running and cancellation
in progress. Its execution pool runs at most two Box jobs concurrently; accepted
work waits in the durable queue. Existing per-job model budgets still apply.

Use the existing draft and job operations. Writes that accept `Idempotency-Key`
must reuse the same key and body when retrying an uncertain response. A saved
draft must select a real knowledge gap and valid anchors before submission.
The scientific request examples are in [the API contract](../api/openapi.json)
and [the workflow walkthrough](api-keys.md#save-a-draft-and-submit-a-job); use the
short-lived JWT wherever that walkthrough uses a workspace key.

Forward `GET /v1/jobs/{id}/events` or `GET /v1/me/workspace/events` through the
application server without response buffering. Retain each event ID and forward
`Last-Event-ID` when reconnecting with a fresh token. Stop job-stream reconnects
after a terminal event. Progress uses durable replay and Pub/Sub notifications;
no Redis or job-status polling is required. The existing Next.js
[proxy](../services/frontend/src/app/api/backend/%5B...path%5D/route.ts) shows how
to forward streams. Do not forward the backend Authorization header to an S3
artifact redirect.

The private credentials are delivered separately from Git. The existing
individual `rvl_` key is not part of this gateway flow. Production configuration
and production release approvals are separate from this QA handoff.
