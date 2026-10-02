# Reference cutover runbook (driven from the laptop)

This runbook moves every environment from the legacy reference data (`cfde-inc-v2`) to the KPN generation that was loaded into Aurora on 2026-10-02. It is driven from the laptop with the reload CLI directly. The contract is [reference-reload.md](reference-reload.md); this page is the operating procedure.

## Where things stand

| | |
|---|---|
| Code | `chase/reference-reload`. It holds the reload, the archive UI, and `origin/main` up to `872b54f`, merged. |
| KPN generation (new) | `G=ec3364ae1dfdeeead0110dd8129a16aa03dbd90b11400108e274f8d9ded92c8a`. Status `complete`, active nowhere. |
| Legacy generation (served now) | `L=16e78f6a1282320367b4e6507ad828fc71bdb38ad06e0e0a7d384026f4414e19`. Status `complete`. |
| Served sources (pinned) | mapping run `272cfa19…`, EAGGL import `a548ad80…`, embedding run `d4c03009…`, DisMech import `4062563d…`. The pins live in `lap/config/cfde_projection.cfg` (`reload_*`). |
| Targets | `rehearsal` (no app, tables missing), `local` (the laptop's durable Workflow stack), `compose` (unused), `qa`, `prod`. All are in legacy mode with no gate. |
| Load evidence | [data/reference-reload/2026-10-02-load/](../data/reference-reload/2026-10-02-load/RUN_LOG.md): preflight before and after, status, the load result, QA readiness. |

Two kinds of step do not change any environment's served state: capture's backfills, and Upstash snapshots. **An `apply` does.** It closes that target's gate, drains its jobs and backs it up. It then activates the KPN generation and archives that target's user work; anchored drafts are dropped. **There is no CLI undo of an apply**; the rollback is restoring that prefix's dump.

## 0. Rules for the operator and the agent

- **The laptop drives the cutover from now on.** Never run a `db_` LAP command on dig-ae-dev-03 again. Its `db_capture_cmd` writes into every prefix, and LAP would not know about the laptop's applies.
- **Approvals need the human.** `approve` (and `abandon --apply`) need a real terminal and a typed `<target>:<sha12>`. An agent prepares the plan and shows its summary; the human approves.
- **Back up before the first capture.** The first capture, and every apply's delta capture, write `anchor_display` backfills into **all five prefixes, prod and QA included**. An apply backs up only its own target. So dump every prefix first (§4).
- **Plan, then approve, then apply, right away.** A plan pins the drafts it will drop, the backfill and the namespace counts. Work in that prefix between plan and apply (a new anchored draft, a snapshot run) makes apply refuse with drift: just plan again.
- **Snapshots before plans.** Finish every `snapshot --apply` before planning any target.
- **Merged code before apply.** Each prefix must already run this branch's code before its apply. Pre-merge code returns 503 from `/readyz` and from semantic search against a KPN snapshot, and it ignores the gate. For the same reason, never roll a cut-over prefix back to a pre-merge image: fix forward.
- **Use the right local stack.** The headed UI test runs on the durable Workflow stack (`scripts/durable_deployment.py`: prefix `reveal_workflow_local`, frontend :3000, API :18001). **Never** use `scripts/local_deployment.py` or `dev-up.sh` for this: they run prefix `reveal`, which is prod's records.
- `REVEAL_REFERENCE_GENERATION_ID` stays unset everywhere. It overrides the active pointer.
- **Secrets stay in `.env`.** Never print the values. Name variables only.
- KPN-anchored analysis submits start paid Box/Claude runs. Run one on purpose in the local UI test (§7b), not more.

## 1. Decisions before starting (defaults in bold)

1. **Rehearsal tables:** **create them empty (§4.1)**, or remove `rehearsal` from `config/reference_reload.targets.yaml`. If you remove it: regenerate the LAP meta (lap/README, Running step 0) and rerun the tests. Capture and every apply crash on the missing tables otherwise.
2. **Aurora backup:** **one manual cluster snapshot of `aurora-giant-bioindex`, taken once and passed to every apply as `--backup-snapshot-id`**. The alternative is the latest automated snapshot. Do not use `--create-aurora-snapshot`: it keeps the target's gate closed while a snapshot of the whole shared cluster is created, and a missing IAM permission fails only after the gate is closed.
3. **S3 prefix for reload artifacts:** **`local/`**, kept fixed until the purge. Prod already reads `local/`; the cold export and purge validate against this prefix.
4. **Target order:** **rehearsal → local (headed UI) → compose → QA → prod**, then soak, then purge. To skip `compose`, remove it from the allow-list now (as in item 1): the purge needs every allow-listed target cut over.
5. **PR timing:** **open the PR `chase/reference-reload` → `main` now; merge it after the local cutover passes**. Then do the QA release from `main` the usual way.
6. **Soak before purge:** **at least a week after prod.** The purge is irreversible.

## 2. Laptop setup

```bash
cd <repo> && git fetch origin && git switch chase/reference-reload && git pull --ff-only
PY=.venv/bin/python     # the repo-root venv the durable stack uses; it must import reveal_backend:
$PY -c 'import reveal_backend' || uv pip install --python .venv -e services/backend
(cd services/frontend && npm ci) && (cd reveal-client && npm ci)
brew install mysql-client      # Oracle mysqldump 8.x (keg-only); then:
export REVEAL_MYSQLDUMP="$(brew --prefix mysql-client)/bin/mysqldump"
```

**Repo-root `.env` (0600).** Keys only; the values come from the existing laptop `.env` and Secrets Manager:
- `REVEAL_MYSQL_HOST`, `REVEAL_MYSQL_PORT`, `REVEAL_MYSQL_USER`, `REVEAL_MYSQL_PASSWORD`;
- `REVEAL_MYSQL_CA_FILE`: an **absolute** path to the RDS global bundle. `shasum -a 256` must print `fe45bbebf92ad3e27a583bbb2ddd1553c521ed4d49af5514dc0a40372ea5395c`, which is `RDS_CA_SHA256` in `scripts/platform_assets.py`;
- `UPSTASH_VECTOR_REST_URL`: the host must be `relevant-mite-29002-us1-vector.upstash.io`, the allow-listed host. If the October credential rotation moved it, update the allow-list first;
- `UPSTASH_VECTOR_REST_TOKEN` (read) and `UPSTASH_VECTOR_WRITE_TOKEN` (snapshot and purge);
- `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`;
- `EMBEDDING_*`.

**Shell helpers.** Define these in tmux, from the repo root. Prevent sleep with `caffeinate -dimsu &`, because an interrupted apply leaves its gate closed.

```bash
PY=.venv/bin/python
pin() { sed -n "s/^$1=//p" lap/config/cfde_projection.cfg; }
G=ec3364ae1dfdeeead0110dd8129a16aa03dbd90b11400108e274f8d9ded92c8a
L=16e78f6a1282320367b4e6507ad828fc71bdb38ad06e0e0a7d384026f4414e19
C=.runtime/reference-cutover; mkdir -p $C/backups && chmod 700 $C
reload() {   # NS=<notification namespace> reload ...  (optional, see §7)
  env -u REVEAL_APPLICATION_TABLE_PREFIX -u REVEAL_VECTOR_ENVIRONMENT -u REVEAL_REFERENCE_GENERATION_ID \
    REVEAL_MAPPING_RUN_ID=$(pin reload_mapping_run_id) REVEAL_EMBEDDING_RUN_ID=$(pin reload_eaggl_embedding_run_id) \
    REVEAL_DISMECH_IMPORT_ID=$(pin reload_dismech_import_id) \
    REVEAL_ARTIFACT_STORE=s3 REVEAL_S3_BUCKET=cyaka-reveal-data REVEAL_S3_PREFIX=local/ AWS_REGION=us-east-1 \
    ${NS:+REVEAL_NOTIFICATION_NAMESPACE=$NS} \
    $PY -m reveal_backend.reference_reload "$@"
}
eaggl_pins() { echo --eaggl-import-id $(pin reload_eaggl_import_id) --eaggl-embedding-run-id $(pin reload_eaggl_embedding_run_id); }
export AWS_PROFILE=<profile with S3 on cyaka-reveal-data and RDS describe/snapshot on aurora-giant-bioindex>
```

Every command prints one JSON result: exit 0 means it ran, 1 means `ok: false`, 2 means refused. Save each result under `$C/`.

## 3. Read-only baseline

```bash
reload status > $C/status.0.json
reload preflight $(eaggl_pins) --check-embedding-service \
  --compare data/reference-reload/2026-10-02-load/preflight.after.json --out $C/preflight.0.json
```

The result must be `ok: true`. The only expected warnings are about the missing rehearsal tables. Both generations must show as `complete` and every target as `legacy`.

## 4. Prepare (writes: new tables, backups)

**4.1 Rehearsal tables** (decision 1). This runs migration 005 for that prefix: `CREATE TABLE IF NOT EXISTS`, two empty tables.

```bash
env REVEAL_APPLICATION_TABLE_PREFIX=reveal_reload_rehearsal $PY -m reveal_backend.repository --apply
reload preflight $(eaggl_pins) --out $C/preflight.1.json      # warnings gone
```

**4.2 Dump every prefix's records.** The human runs this, so the password never leaves `.env`.

```bash
set -a; . ./.env; set +a
MYSQL_PWD="$REVEAL_MYSQL_PASSWORD" "$REVEAL_MYSQLDUMP" --single-transaction --skip-lock-tables --no-tablespaces --hex-blob \
  --set-gtid-purged=OFF --ssl-mode=VERIFY_IDENTITY --ssl-ca="$REVEAL_MYSQL_CA_FILE" \
  -h "$REVEAL_MYSQL_HOST" -P "${REVEAL_MYSQL_PORT:-3306}" -u "$REVEAL_MYSQL_USER" cyaka_reveal_mechanisms \
  reveal_records reveal_workflow_qa_records reveal_workflow_local_records reveal_compose_records reveal_reload_rehearsal_records \
  > $C/backups/all-prefixes.$(date -u +%Y%m%dT%H%M%SZ).sql && chmod 600 $C/backups/*.sql
```

**4.3 One Aurora cluster snapshot** (decision 2):

```bash
SNAP=reveal-reload-pre-cutover-$(date -u +%Y%m%d%H%M)
aws rds create-db-cluster-snapshot --db-cluster-identifier aurora-giant-bioindex --db-cluster-snapshot-identifier $SNAP --region us-east-1
aws rds wait db-cluster-snapshot-available --db-cluster-snapshot-identifier $SNAP --region us-east-1
```

## 5. Capture, once, for every prefix

This freezes every legacy factor that user work references into `archived_reference_factors`, and backfills `request_binding.anchor_display` in all five prefixes. There is no cold export yet (§8).

```bash
reload capture --from $L > $C/capture.dry.json                 # read-only: review the per-prefix counts
reload capture --from $L --apply > $C/capture.json
```

## 6. Upstash snapshots, before any plan

Each vector environment gets 69,157 vectors (4,037 factors, about 20.6k DisMech contexts, 44,399 gene sets and 133 collections), uploaded in 347 batches. `local` and `compose` share vector environment `local`, so one snapshot serves both. Nothing is activated. If a run fails partway, rerun it with the same `--batch-size`.

```bash
for t in rehearsal local qa prod; do reload snapshot --generation $G --target $t --apply > $C/snapshot.$t.json || break; done
reload snapshot --generation $G --target compose --apply > $C/snapshot.compose.json   # reuses local's verified snapshot
```

## 7. Cut over one target at a time

The same loop runs for every target. The apply drains for up to 10 minutes; while its gate is closed, submits and anchor edits return 503.

```bash
t=<target>
reload plan --target $t --generation $G --out $C/plan.$t.json        # read-only; read blockers and summary
reload approve --plan $C/plan.$t.json                                # HUMAN, at a terminal: types <target>:<sha12>
NS=<namespace> reload apply --target $t --plan $C/plan.$t.json --approval $C/approval.$t.json \
  --backup-dir $C/backups/$t --backup-snapshot-id $SNAP --drain-seconds 600 > $C/apply.$t.json
reload verify --target $t --generation $G > $C/verify.$t.json        # must be ok: true
```

`NS` is that app's notification namespace: `reveal-workflow-local`, `reveal-qa` or `reveal-prod`. It is empty for `rehearsal` and `compose`. Without it, open browser tabs only see the cutover on their next reconnect or reload.

**7a. rehearsal.** There is no app. Plan, apply and verify only. This proves the AWS snapshot check, mysqldump and the whole apply path. It also writes `anchor_display` backfills into the other prefixes and marks `L` `superseded`. The other prefixes keep serving `L`, because legacy mode serves superseded generations.

**7b. local, with the headed UI test.**
1. Start the durable Workflow stack on this branch: `.venv/bin/python scripts/durable_deployment.py prepare`, then `… up --build`. Check `http://127.0.0.1:18001/api/reveal/readyz` returns `ready` in legacy mode, with mapping run `272cfa19…`.
2. **Before planning**, make the fixtures in the UI:
   - one legacy account or outcome, ideally published (use an existing one if `reveal_workflow_local` has it);
   - one named, explicitly saved draft with legacy factor anchors;
   - one named, explicitly saved gap-only draft. Temporary editors are not retained unless saved.
3. Run the loop with `t=local NS=reveal-workflow-local`. Keep a composer tab open during the apply.
4. Headed checks, with the stack still running:
   - [ ] `readyz` is ready. Factor search and suggest return `factor:kpn:…` ids labelled with KPN phenotype names.
   - [ ] The open composer re-reads its anchors after the cutover. The anchored draft is dropped; editing it recovers into a new draft without losing the edit. The gap-only draft is kept.
   - [ ] The old account or outcome shows the **Outdated reference** badge. Its banner lists the original anchors, and **Start a new analysis on this gap with current factors** opens a temporary `/drafts/<id>?suggest=current` editor with suggestions. Its copied inputs are retained only when the researcher explicitly saves or submits.
   - [ ] The workspace and gap pages offer the current/archived/all filters. The collapsed account counts match the filter. Votes and workspace search still work on archived items.
   - [ ] A published old item stays public, with the badge. The old Mechanism URL returns the frozen factor.
   - [ ] One KPN-anchored analysis submit completes. It is paid, so run exactly one.
   - [ ] `node services/frontend/scripts/check-composer-navigation.mjs` passes against the stack (Playwright; it includes the dropped-draft adoption scenario).

**7c. compose.** CLI only: plan, approve, apply, verify.

**7d. QA.**
1. Merge the PR into `main` (decision 5).
2. Release `main` to QA the usual way (docs/platform-deployment.md): `scripts/platform_bundle.py --fetch-assets`, the dig-service-platform CI, the QA deploy with the explicit platform revision, then the Vercel preview and the QA alias.
3. Check that QA `readyz` is ready in legacy mode on the new code. Only then run the loop with `t=qa NS=reveal-qa`.
4. Repeat the headed checks on https://reveal-mechanisms-qa.vercel.app.

**7e. prod.**
1. Deploy the same platform revision to prod. This needs sagehen03's approval.
2. Stop or upgrade every legacy colleague stack that uses prefix `reveal`; they break at this apply.
3. Run the loop with `t=prod NS=reveal-prod`, plus the production flags:

```bash
reload approve --plan $C/plan.prod.json --allow-production                     # HUMAN
REVEAL_RELOAD_PRODUCTION_APPROVAL=<plan_sha256 from plan.prod.json> NS=reveal-prod reload apply --target prod ... --allow-production
```

## 8. Soak, cold export, purge (irreversible)

After the soak, export the legacy generation to S3, with the same prefix as before:

```bash
reload capture --from $L --cold-export --apply > $C/cold-export.json   # cold_export.root.store must be "s3"
reload purge-retired --out $C/purge-plan.json
reload approve --plan $C/purge-plan.json --allow-production            # HUMAN: purge-retired:<sha12>
REVEAL_RELOAD_PRODUCTION_APPROVAL=<sha> reload purge-retired --apply --plan $C/purge-plan.json --approval $C/approval.purge-retired.json --allow-production
```

The purge runs only when every allow-listed target has been cut over and verified, the export exists, and no old-generation job is running.

## Rollback and recovery

| Situation | Action |
|---|---|
| Before §5 (no capture or snapshot yet) | `reload abandon --generation $G` (dry run), then the human runs `reload abandon --generation $G --apply`. Add `--drop-empty-schema` to remove migration 008 too. |
| A capture or snapshot failed | Fix the cause and rerun it; both are idempotent. A snapshot must be rerun with the same `--batch-size`. |
| `plan` reports drift, or `apply` refuses on drift | Plan again, then approve and apply again. |
| `apply` failed **before** activation | Its gate reopens and its audit records the failure. Fix the cause and plan again. |
| `apply` failed **after** activation, or the laptop slept | `reload plan` now produces a *resume plan*. Approve and apply it; only the idempotent tail runs. If the gate is still closed and nothing is running, `reload gate --target $t --open`. |
| A cut-over prefix is wrong | Fix forward. The last resort is restoring that prefix's `<prefix>_records` from `$C/backups/<t>/` or §4.2, which loses every write made after the dump. Never go back to a pre-merge image. |
| After the purge | Only an Aurora restore. |
