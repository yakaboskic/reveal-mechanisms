# Connect another frontend to REVEAL

Use **[reveal-client](../reveal-client/README.md)** as the working reference. Give
its [integration instructions](../reveal-client/INTEGRATION.md) to the agent
building the frontend. It uses the same gateway authentication as our original
Next.js frontend; there is no separate API-key system to implement.

## Start the reference client

The private `dk-qa.env` handoff contains the deployed QA API URL and gateway
credentials. Supply that file to setup; the script creates this frontend's own
session secret and local configuration.

```bash
cd reveal-client
npm ci
npm run setup -- --credentials /absolute/path/to/dk-qa.env
npm run dev
```

Open **http://localhost:3200**. Connect the workspace, choose a knowledge gap and
mechanism, save a draft, run an analysis, and follow its live events and results.
The app handles session creation and request signing automatically. Keep the
server integration and replace the UI, or adapt the framework-neutral gateway
to a different server using the agent guide.

## Request flow

The browser calls the frontend's `/api/backend/v1/...` routes. Its server reads
the browser session, signs a fresh five-minute API assertion, and forwards the
request to QA. The backend applies workspace ownership and runs the job.

On first connection, the reference client's server resolves a configured demo
identity through `/internal/v1/principals/resolve`. A signed HttpOnly cookie
keeps that workspace connected. The example starts on loopback with a fixed
server-configured developer identity. When integrating a hosted application,
replace that demo identity adapter with the application's existing authenticated
user session, as described in the integration guide.

QA API: https://api-qa.hugeampkpnbi.org/api/reveal

Interactive contract: https://api-qa.hugeampkpnbi.org/api/reveal/docs

The shared gateway values stay in the frontend server environment, outside Git
and browser bundles. No database, Redis, Vector, Box or model credentials are
needed by a frontend. Registered workspaces persist without a daily analysis
cap. QA permits ten outstanding jobs per workspace and executes at most two Box
jobs at once. Existing per-job model budgets apply.

Progress uses job/workspace event streams. The proxy streams bytes immediately,
re-signs each reconnect automatically, and forwards `Last-Event-ID` for replay.
No Redis or job-status polling is needed. An analysis can produce accepted
scientific accounts, an explicit insufficient-evidence outcome, or a reported
failure; the reference app shows the actual state.

The original [gateway](../services/frontend/src/lib/gateway.ts) and
[proxy](../services/frontend/src/app/api/backend/%5B...path%5D/route.ts) remain
reference implementations. The [Python session helper](../scripts/gateway_session.py)
is an optional terminal debugging tool; it is not part of frontend startup or
normal application use.
