# Automatic CFDE support assessment

The draft automatically asks TypeSafe Jev whether the selected inputs offer a defensible path to a useful CFDE-supported scientific account once its gap and mechanism suggestions are ready. Changes are debounced for 1.5 seconds; saving, uploads, conflicts and outdated selections pause new checks. It never starts an agent, saves the editor, validates an account, or publishes science. The normal local/online research controls remain available regardless of the estimate.

While a check runs, the ring around the launch arrow rotates and a subtle label reads “Assessing likely CFDE support”. Reduced-motion preferences disable rotation. Once ready, the ring fills by Jev's Yes probability and only the support sentence appears below it: “Likely CFDE support” above 60%, “CFDE support unlikely” below 40%, and “CFDE support uncertain” otherwise. The interface has no numeric percentage, estimate details or initial check button. The API retains probabilities, separate provider confidence, the main limitation and input coverage. These bands are presentation choices, not validated scientific thresholds. A low forecast does not establish that no relevant CFDE data exists. CFDE grounding remains encouraged rather than required for research.

## Inputs and interpretation

The backend uses the current browser composer, including unsaved edits, while authorizing its draft/version and uploads. The package sent to Jev contains:

- The selected knowledge gap question, rationale and disease.
- Names and descriptions of attached DisMech mechanisms.
- Names and trait context of only the selected EAGGL mechanisms, in their chosen order.
- Up to 50 gene names/loadings and 50 GeneSet names/loadings per mechanism. GeneSets are ordered by joint loading, with exact joint/marginal decimal strings. These are retained per-trait projections, not an exhaustive library query.
- Short GeneSet source labels, library, species, assay/data type and relevant experimental context. Different species or contexts remain distinct.
- Up to 2,000 characters of each researcher input field and each verified upload extraction, plus a short coverage summary.

No new embeddings, BioIndex queries, member lists, or searches for replacement factors are performed. Missing legacy numeric GeneSet loadings stay missing. Unknown species stays unknown. Co-loading is not membership, a perturbation target is not necessarily a signature member, and loadings are not causal effects. A useful partial explanation with at least one supported CFDE claim can qualify; every relationship category need not be filled.

Jev receives no application record IDs, hashes, provenance graphs or compression instructions. Exact identities, source pins, provenance and checksums stay in the server audit record. The complete model request has a 100,000-byte ceiling. Selected mechanisms and numeric rows are never silently discarded to fit. Oversized requests, including provider token-limit rejections, fail with guidance to reduce mechanisms/context; the research controls still work. The byte ceiling is not an exact provider tokenizer. Bounded excerpts and absent metadata are disclosed in coverage. The five-mechanism public-source test sends about 38 KB, including 143 available gene rows and 250 GeneSet loading rows.

## API and cache

`POST /v1/drafts/{draft_id}/cfde-assessments` requires an ordinary owner bearer credential, an 8–128-character `Idempotency-Key`, and `{draft_version, composer}`. It returns `202`, a private assessment receipt, `Location`, and `Retry-After: 2`. An anonymous owner may use it. An admin science-read key cannot.

`GET /v1/drafts/{draft_id}/cfde-assessments/{assessment_id}` polls that receipt. The response progresses through `preparing`, `assessing`, and `succeeded` or `failed`. Neither endpoint exposes another user's assessment ID or inputs. Responses are `private, no-store` and vary by authorization.

The durable application database stores request receipts, exact prepared model inputs, filtered typed responses and hashes. No research jobs or scientific records are created. Source reads and provider calls execute outside application write transactions. POST returns before a cold catalog is prepared.

Same-key retries recover the original receipt; changed input with that key returns a conflict. Unchanged private inputs reuse their owner/draft cache. Saving an unchanged composer can reuse a successful private estimate through a fresh receipt bound to the new draft version; the original receipt keeps its original version and becomes stale. Uploads are authorized and resolved again before reuse. Pending private work stays version-bound. Meaningful notes or any uploads disable cross-user reuse. Defaults with the same gap, selected factor references, model, selected graphs, reference generation and rubric share pending work and successful forecasts for up to seven days. Each requesting owner gets their own receipt. Shared cache entries contain only public-source assessment fields, with immutable version references so a later refresh does not erase older receipts. Different reference generations, Jev versions or rubrics do not reuse them.

Each cache miss makes at most one provider attempt. The browser submits a check automatically after inputs settle. Polling never dispatches a model call, and failed/interrupted checks require an explicit retry rather than a background retry loop. Two workers and four in-flight slots bound local dispatch; new uncached receipts are limited to 20/day per anonymous workspace and 100/day per registered workspace. Cache hits remain usable while the provider credential is unavailable.

The receipt has a 120-second acceptance deadline. Queued/active receipts left by a process restart become `interrupted`; polling never silently retries a potentially billed request. A deliberate retry uses a new key. Failed/interrupted checks never become a “No” forecast. Source statements check the remaining deadline, direct connections use bounded socket timeouts, and the provider attempt has a 25-second budget plus a 64 KB response cap. Existing cold catalog loading has its own database timeouts; the acceptance deadline is not a hard cancellation of that shared loader. Completed receipts remain readable after their operation deadline.

Edits invalidate the UI's result immediately; server reads also mark changed saved versions or reference generations stale. A stale or incomplete result cannot block research. Private assessments follow normal workspace ownership and draft access rules; a deleted/expired draft makes its assessment unavailable.

## Configuration and verification

Set `TYPESAFE_API_KEY` in the **backend** environment for automatic draft checks. The local deployment preparer copies it from the ignored root `.env`; existing runtime env files need regeneration or a private key-only update followed by backend recreation. Never expose it through a `NEXT_PUBLIC_*` variable or frontend bundle. Cloud environments require their own backend secret configuration; this feature does not deploy that secret automatically.

The implementation pins `jev-1.13.0` and rubric `cfde-support-v1`. The [SystemOne API](https://docs.typesafe.ai/api) returns typed probabilities; [provider confidence](https://docs.typesafe.ai/confidence) is separate from Yes probability. These forecasts have **not** been calibrated against completed REVEAL runs. The model provides a typed blocker category, not a fabricated prose explanation.

Local fixture tests:

```sh
.venv/bin/python -m pytest services/backend/tests/test_cfde_assessment*.py -q
cd services/frontend && npm test && npm run typecheck
```

For a manual test, open a knowledge gap and retain its suggested mechanisms (or select your own). Once the inputs settle, the ring rotates beside the subtle assessment label, then displays the completed support sentence without a percentage or details panel. Change a note or mechanism: the old estimate disappears and a new check follows after typing/selection settles. Open the same default selection in a separate anonymous session to verify shared reuse. Do not click a research mode merely to test the estimate.
