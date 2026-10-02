# Local browser regressions

Start a development frontend separately. These checks intercept every API request
and block external origins; they do not log in, write a real database or S3 bucket,
or dispatch an agent. Playwright is an external test tool, not an application
dependency. `PLAYWRIGHT_MODULE` and `PLAYWRIGHT_EXECUTABLE_PATH` can select an
installed browser/runtime.

```sh
DRAFT_LIFECYCLE_BASE_URL=http://127.0.0.1:3017 node scripts/check-draft-lifecycle.mjs
NAVIGATION_BASE_URL=http://127.0.0.1:3017 node scripts/check-composer-navigation.mjs
ACCOUNT_GRAPH_BASE_URL=http://127.0.0.1:3017 node scripts/check-account-graph.mjs
```

The lifecycle harness keeps mutable mocked drafts, frozen research requests,
idempotency receipts and uploads. It exercises the real editor, dialogs,
navigation, request deadlines, file hashing and upload XHR. Its assertions cover:

- Anonymous workspace creation before editing; delayed suggestions retain typed
  input; failed editor creation can be retried with the same key.
- Discard and reload of unsaved input; explicit named Save and Cancel; reload of
  the last saved version; lost Save acknowledgement replay across reload;
  canceling a conflict copy and discarding a stale Save receipt.
- Independent submission snapshots; unchanged named drafts; frozen inputs after
  source-draft deletion or catalog outage; stale editor restoration and pending
  receipts cannot override explicit run navigation.
- Duplicate clicks, lost acknowledgements, locked inputs after uncertain Back,
  bounded session/draft/job/body-decoding requests, and same-key recovery.
- Google and ORCID pending-return receipts, bounded CSRF/provider requests, and
  late provider responses after Back. A controlled development-only boot-effect
  replay retains the Fast Refresh stuck-spinner regression check.
- Failed upload completion recovery without a second transfer, submission
  blocking while uploads are pending, and removal releasing attachment quota.

`DRAFT_LIFECYCLE_SCENARIO_FILTER` accepts a regular expression against the scenario
names printed by the harness. Results and screenshots go to
`.runtime/draft-lifecycle-audit/` at the repository root; use
`DRAFT_LIFECYCLE_AUDIT_DIR` to override it. Failed runs include the current page text,
intercepted requests and a screenshot in the report directory.

`check-submission.mjs` remains a compatibility entrypoint and maps the previous
`SUBMISSION_BASE_URL`, `SUBMISSION_AUDIT_DIR` and `SUBMISSION_SCENARIO_FILTER`
variables onto the lifecycle harness. Filter names now refer to the current
scenarios. The former assumptions that submitting creates the first anonymous
editor, ordinary edits autosave, and `reveal:composer` persists the open editor
are intentionally retired. Pending explicit Save and Submit receipts still
survive reload. The navigation harness separately retains home/cache isolation,
legacy query links, cold saved-draft routes, same-component navigation, missing
draft recovery, and runs whose source draft was deleted.

The account graph harness uses the canonical fixture in
`data/fixtures/bubble-account-v1/` and derives partial/error cases in memory. It
checks real Cytoscape canvas hit testing, nested dataset inspection, keyboard
list navigation, snapshot-bound page continuation, stale-source reload and
mobile layout. These mocked checks complement a local seeded-database smoke
test; they do not prove real storage access or extraction.
