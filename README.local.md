# Legacy colleague handoff on your laptop

For the new API-only execution stack, use [the durable workflow guide](docs/durable-workflow-runtime.md)
and `scripts/durable_deployment.py`. This page retains the existing encrypted
colleague handoff and legacy worker stack during migration.

This is the retained colleague onboarding guide for the legacy worker stack. It runs in Docker at **http://localhost:3000**: Next.js, FastAPI, Redis, a dispatcher and two workers. It connects to the existing development Aurora/RDS database and real S3 storage. You do not need an EC2 instance, Vercel account, Node installation, local database, or storage emulator. These commands use `deploy/compose.legacy.yaml`; the current Workflow stack uses **http://localhost:3000** with isolated application tables. Stop the legacy frontend before running the Workflow frontend on that port.

The [DIG platform integration](docs/platform-deployment.md) now deploys an HTTP-only Workflow service. The QA backend is deployed and its public event/replay, retrieval and managed callback checks passed; production and Vercel remain pending. Use this page only for an existing legacy handoff, and the workflow guide for the current runtime.

## 1. Prerequisites

- Access to this GitHub repository, Git, curl, and **Python 3.12** (3.10+ supported by the setup script).
- Docker Desktop on macOS, or Docker Engine/Compose on Linux/WSL2. Start Docker and allocate **at least 10 GiB RAM**. Compose **2.30+** is required.
- **GnuPG** (`gpg`) to decrypt the handoff: macOS `brew install gnupg python@3.12`; Ubuntu/WSL `sudo apt-get install gnupg python3-venv git curl`. Install Docker separately.
- Chase's encrypted `reveal-local.env.json.gpg` file, sent privately, and its password received through a different channel.
- Network access to the shared RDS endpoint on port 3306 and HTTPS access to GitHub, AWS S3, the embedding service and execution providers. If RDS times out, use the approved network/VPN or ask the administrator to allow your connection.

The encrypted bundle includes shared development database, S3, embedding, OAuth and Box/Anthropic credentials. It does not contain research data, Chase's browser session, or laptop-specific paths. AWS CLI is unnecessary with the shared-credential bundle. Temporary AWS credentials, if used, still expire; encryption does not extend their lifetime.

## 2. Clone and start

Clone the `main` branch:

```sh
git clone --branch main https://github.com/yakaboskic/reveal-mechanisms.git
cd reveal-mechanisms
python3 scripts/local_setup.py --bundle ~/Downloads/reveal-local.env.json.gpg
```

Enter the password at the hidden prompt. Setup creates `.venv`, installs Python dependencies, fetches and verifies the exact DAPPER release and DisMech revision, downloads the AWS RDS CA bundle, prepares private configuration, builds both application images and starts the stack. No database imports, resets, or migrations run against the existing application tables. The first build and catalog warmup can take several minutes; Docker startup waits up to ten minutes for service health.

Open **http://localhost:3000** when setup reports readiness. Continue anonymously to learn the API, or sign in with your own Google/ORCID account. OAuth callbacks must already allow `http://localhost:3000/api/auth/callback/google` or `/orcid`; anonymous access works without those providers. Your login does not impersonate Chase or grant access to his private workspace. Published accounts remain available under their existing identities.

For an agent that cannot answer a hidden prompt, save the password in a private file and use:

```sh
chmod 600 ~/Downloads/reveal-handoff-password.txt
python3 scripts/local_setup.py --bundle ~/Downloads/reveal-local.env.json.gpg \
  --password-file ~/Downloads/reveal-handoff-password.txt
```

Do not paste credentials into an agent conversation or terminal command. Delete the downloaded password file after import if it is no longer needed. The script refuses to overwrite an existing `.env` or storage configuration; rerun setup **without** `--bundle` after an interrupted dependency download or build.

## 3. Daily commands

Run these from the repository root; activating `.venv` is optional.

```sh
.venv/bin/python scripts/local_deployment.py status
.venv/bin/python scripts/local_deployment.py logs
.venv/bin/python scripts/local_deployment.py down
.venv/bin/python scripts/local_deployment.py up
# Update from main, then rebuild:
git pull --ff-only origin main
.venv/bin/python scripts/local_deployment.py up --build
```

If you previously cloned the handoff branch, preserve any local code changes, then run `git fetch origin`, `git switch main` and `git pull --ff-only origin main` before rebuilding. Your ignored `.env` and `.runtime/` configuration stay in the same checkout; do not reimport the encrypted bundle.

`down` drains this clone's workers before stopping containers. It preserves shared RDS records and S3 objects. `up` also stops this checkout's legacy development stack before taking port 3000. If another unrelated program uses the port, stop it deliberately; do not kill arbitrary processes.

| Address | Purpose |
| --- | --- |
| `http://localhost:3000` | Browser application |
| `http://localhost:3000/api/backend/v1/...` | Browser gateway with session authorization |
| `http://127.0.0.1:18000` | Direct API; private routes require gateway assertions |
| `http://127.0.0.1:18000/healthz` | Process liveness |
| `http://127.0.0.1:18000/readyz` | Database, storage, queue and catalog readiness |

Start learning with the [API walkthrough](docs/api-quickstart.md). Full contract and offline viewer instructions are in [api/README.md](api/README.md). API/dispatcher/workers use the same backend image and scientific validation implementation.

## Shared data and separate queues

Every colleague uses the original **`reveal_*` tables** and **`cyaka-reveal-data/local/`** artifacts. Import creates a random `REVEAL_JOB_NAMESPACE=reveal-local-...` in your `.env`; only your API's jobs are dispatched to your Redis and workers. Keep that namespace stable across restarts. This separates worker claims and drain controls, not users or scientific data. New writes and publications are real changes to the shared dev database. The two-job concurrency limit applies per namespace, so simultaneous colleague stacks can increase total provider spend.

Session, gateway and Redis secrets are generated locally on first preparation and retained in `.runtime/deployment/local-secrets.json`. No local session from Chase is copied. Keep `.env`, `.runtime/`, and `.deployment-assets/` out of Git. Application artifacts are durable in S3; containers use bounded RAM scratch. Host disk holds source checkouts, configuration, images and bounded logs.

Submitting research or requesting a review invokes live paid providers with the configured budgets. Setup and the read-only API examples do not submit research. Do **not** run catalog importers, Prisma reset/push, artifact migrations or disruptive `verify` probes as onboarding steps. `verify` rejects the shared application tables.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| GnuPG failure | Check the password/file; failed decryption installs no configuration. |
| Existing `.env` | Setup already imported it. Rerun without `--bundle`; do not overwrite working secrets. |
| Git clone/fetch failure | Verify access to this repo and the pinned upstream DAPPER/DisMech repositories; retry setup. |
| Readiness is slow | Inspect API logs; remote RDS catalog/vector warmup can take minutes. `/healthz` alone does not establish readiness. |
| RDS timeout / denied | Check VPN/network access and database credentials. Do not disable TLS. |
| S3 expired or denied | Refresh shared `storage.env` credentials through Chase, or use your own AWS profile (see the deployment guide); rerun `up`. |
| API 401 in curl | Use the browser gateway with its session; Auth.js cookies are not direct API bearer tokens. |
| API 403 on writes | Send the matching `Origin` and retain the session cookie; use the walkthrough examples. |
| Scientific validation fails | Inspect the persisted job failure. Keep the draft and captured evidence; this is separate from container readiness. |
| Docker build runs out of memory | Increase Docker VM RAM to at least 10 GiB, then rerun setup without `--bundle`. |

## Prompt for your coding agent

> Read README.local.md and docs/api-quickstart.md. Set up this clone using scripts/local_setup.py and the encrypted configuration/password file paths I provide. Never print secret values or commit private configuration. Preserve the existing RDS tables, accounts and S3 artifacts. Keep the generated per-clone job namespace. Start the complete Docker stack, check readiness and exercise only the read-only API examples. Do not submit a paid research job, run an import/migration, or change shared data unless I ask. Report the local URL and any specific network/access blocker.

## Preparing a new handoff (Chase)

With the working root `.env`, storage configuration and GnuPG installed:

```sh
.venv/bin/python scripts/local_handoff.py export
```

This writes `.runtime/handoff/reveal-local.env.json.gpg` and the separate `.runtime/handoff/reveal-local.env.json.password.txt`, both mode 0600. It exports only the documented application settings and resolved S3 credentials, normalizes paths, omits session/gateway secrets and unrelated provider keys, and verifies an encrypt/decrypt round trip. Send the encrypted file via Slack and the password through a different private channel. Neither file belongs in Git, a Docker build context, or the public API documentation. Use `--output` with a new filename to create another package; existing packages are never overwritten.

The shared AWS credentials retain their existing account permissions; the package does not create narrower IAM credentials. Replace them with scoped project credentials when those become available. For key rotation, update your local source settings and create a fresh handoff.
