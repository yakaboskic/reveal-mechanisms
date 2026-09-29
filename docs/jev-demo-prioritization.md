# DisMech demo prioritization with Jev

`scripts/prioritize_demo_gaps.py` prepares one request per knowledge gap, asks
multiple typed questions in that request, submits bounded concurrent HTTP calls,
and writes a ranked result plus a diverse shortlist. Preparation is completely
offline and reads the existing source captures and embeddings. It does not
change scientific records, the database, the app's suggestions, or its jobs.

## Proposed evaluation

Favor a clear, interesting biological story grounded in available EAGGL data.
Topic familiarity can help presentation, but is not evidence. These scores
triage candidates for a subsequent live demo rehearsal; they do not establish
that a gap has been solved or that the current CFDE service contains the same
evidence as the imported atlas.

The default eight questions are defined in
`services/backend/src/reveal_backend/demo_prioritization.py::default_questions`:

1. **prioritize** (Noul): should this gap enter the demo shortlist?
2. **evidence_fit** (Score 0–4): generic label match through specific, consistent
   gene/process support.
3. **answerability** (Score 0–4): wrong data type through a focused computational
   investigation that these observations can meaningfully support.
4. **demo_clarity** (Score 0–4): diffuse story through a clear question and small
   set of traceable biological actors.
5. **distinctiveness** (Score 0–4): redundant through complementary, relative to
   the supplied comparison questions only. This does not measure literature novelty.
6. **overclaim_risk** (Noul): would even a qualified hypothesis-generation demo
   require an unsupported causal, clinical, cross-species or cross-atlas claim?
7. **main_blocker** (Choice): none, weak fit, wrong data type, cross-atlas
   validation, missing evidence, unclear story, or redundancy.
8. **best_anchor** (Choice): one of the supplied factor references (`F01`–`F20`
   by default), or `none`.

Each question stands alone. Jev evaluates questions independently; it cannot
use an answer to one question as input to another within the same request.
Noul is a 0–1 model probability; Score is a probability-weighted rubric value
and can fall between levels. Model confidence is not scientific certainty.
There is no free-text explanation primitive: the component scores and blocker
provide structured reasons. A later synthesis pass can write prose if useful.

## State sent for each gap

- Exact gap question, rationale, disease, kind/status, source revision, attached
  mechanism descriptions, and an unresolved-attachment count.
- Up to 20 **mapped, semantically retrieved EAGGL factors** by default, ordered by maximum
  cosine against attached DisMech mechanism descriptions. A gap with no linked
  mechanism uses its stored exact question vector. This matches the app's
  automatic semantic retrieval logic, expanded beyond its five default anchors.
- Each factor's legacy/native IDs, legacy and current CFDE labels, trait,
  matched context references, top gene-set labels, and top 20 genes with their
  actual **capped legacy loading weights**. Equal weights sort by gene symbol.
  Gene symbols and weights are compact two-column rows; numbers are rounded
  to five significant digits. Total nonzero genes and retained loading mass
  make the truncation visible. A factor with fewer than 20 genes sends fewer.
- Shared direct hierarchy parents among selected factors, plus the 20 largest
  retained-gene Jaccard overlaps. Hierarchy membership and gene overlap are
  explicitly not independent causal evidence.
- Up to 12 other exact question texts: half nearest mean context vectors,
  half distinct diseases in a stable hash order. These questions provide topic
  comparison only; their evidence is not included. The focal question is excluded.

The local snapshot has 3,367 gaps, including three marked `RESOLVED`, which are
excluded by default. All question text totals 621,061 characters before IDs or
JSON, so sending the entire other-gap catalog per request is inappropriate.
The complete catalog is saved locally as `gap-catalog.json`. Comparison sampling
uses all eligible gaps even when preparing a small disease-filtered pilot.

The crosswalk uses exact trait + factor number, not gene/label agreement.
Only mapped factors are candidates, so the shortlist is usable for app anchor
selection; unmapped legacy factors are outside this evaluation. Every request
labels legacy gene provenance and preserves both factor labels. A high score
still requires inspecting live evidence for the chosen current CFDE anchor.

## Jev API contract and context budget

Documentation was retrieved with Context7 (`/websites/typesafe_ai`) and checked
against the official model page on September 29, 2026:

- [API reference](https://docs.typesafe.ai/api):
  `POST https://api.typesafe.ai/v1/systemone`, bearer `TYPESAFE_API_KEY`, with
  `state`, `model`, and a map of typed `questions`. State can be a JSON object.
- [Models](https://docs.typesafe.ai/models): the documented current model is
  `jev-1.13.0`; this script pins it by default. The page specifies 64k tokens for
  the complete request and 32k for state plus the longest question. Older
  Context7 excerpts described an approximate 32k shared budget.
- [Primitives](https://docs.typesafe.ai/primitives): questions are independent;
  no native multi-state batch endpoint is used here. “Batch” means several
  one-gap HTTP requests scheduled by this script.

The default 48,000-byte request guard counts the complete serialized request.
It removes comparison questions from the end first, recording how many remain.
It never silently drops focal context, factors, or genes. A focal payload that
still exceeds the guard is recorded under `manifest.json.blocked` and is not
sent; preparation continues for other gaps and exits nonzero. Reprepare blocked
IDs with lower `--top-factors`/`--top-genes`, or deliberately adjust the guard.

Token estimates use UTF-8 bytes / 2. This is a conservative heuristic, **not Jev's tokenizer
or a guarantee that a request fits**; gene symbols and numeric JSON can tokenize
differently from English prose. Inspect a small live pilot before submitting
the corpus. HTTP 400/413/422 errors are retained without automatic size-changing
retries. Retry payloads always remain identical. Actual response usage and model
versions are retained; no hard-coded dollar estimate is treated as billing.

The initial 50×50 pilot (about 90,000 bytes per request, using bytes / 3) was
rejected by Jev with `max_tokens_exceeded`. Re-running those immutable requests
does not shrink them. The defaults above were reduced to 20×20 with 12 comparison
questions; prepare into a new directory after changing payload settings.
The runner now identifies this provider error explicitly and prints a reprepare
instruction. Connection resets and server disconnects are separate transport
failures; the default two retries cover them, while a context rejection is never
automatically retried. Both each failed attempt and any eventual success are logged.

A useful calibration experiment is to compare 10×20, 20×20 and 25×30 on the same
10–20 gaps, including plausible positives and questions requiring unavailable
clinical/experimental data. Check ranking stability and anchor quality before
assuming the largest state is better. Use a separate output directory per
payload/rubric/model variant.

## Run from the repository root

Use the existing `.venv`, with backend dependencies and its `eaggl` extra
(including SciPy), as used for the existing import scripts. The default local
captures are ignored by Git and must already exist:

- `data/eaggl/captures/legacy-711`
- `.runtime/dismech-embeddings`
- `data/dismech`, `data/dismech-gaps/2026-09-24`, and `data/cfde`

All source files and stored embedding bindings are verified against their
manifests; incompatible imports or revisions fail before evaluation. Missing
captures do not trigger downloads, embedding generation, or database fallback.
Override capture paths with the corresponding CLI arguments if needed.

Prepare a pilot (use a new output directory):

```sh
.venv/bin/python scripts/prioritize_demo_gaps.py prepare \
  --disease 'Type 2 Diabetes' --limit 3 --output .runtime/jev-demo-pilot
```

Inspect `manifest.json` and a referenced file in `requests/`. The files are the
exact JSON bodies sent to Jev. To revise the rubric, edit `default_questions`,
or supply `--questions-file path/to/questions.json` with a SystemOne questions
object, then prepare a new directory. Custom rubrics produce raw answers and
usage; the standard weighted ranking is intentionally disabled.

Add `TYPESAFE_API_KEY='your-key'` to the ignored root `.env`. Existing process
environment values take precedence. The key is not written into request files.

```sh
# Inspect pending work, cached results and maximum HTTP attempts; sends nothing.
.venv/bin/python scripts/prioritize_demo_gaps.py run \
  --output .runtime/jev-demo-pilot

# Submit this pilot with one worker and bounded retries for transient failures.
.venv/bin/python scripts/prioritize_demo_gaps.py run \
  --output .runtime/jev-demo-pilot --send --limit 3 --workers 1 --retries 2

.venv/bin/python scripts/prioritize_demo_gaps.py report \
  --output .runtime/jev-demo-pilot

# After reviewing the pilot, prepare all eligible gaps and send a bounded slice.
.venv/bin/python scripts/prioritize_demo_gaps.py prepare --output .runtime/jev-demo-all
.venv/bin/python scripts/prioritize_demo_gaps.py run \
  --output .runtime/jev-demo-all --send --batch-size 25 --workers 4 --limit 100

# Repeat to process the next uncached slice, or omit --limit for all pending gaps.
.venv/bin/python scripts/prioritize_demo_gaps.py report --output .runtime/jev-demo-all
```

`--batch-size` bounds locally queued requests; `--workers` bounds simultaneous
requests. `--limit` counts uncached gaps, not HTTP attempts. Default retries are
two extra attempts for 429, selected server errors, and transport errors.
`Retry-After` is honored; delays over 60 seconds pause further batches so the
operator can resume later. Already queued requests in the current batch finish.
Authentication errors stop subsequent batches. Permanent failures are retained
and return a nonzero exit code.

Every `run` invocation prints its log path and appends to `OUTPUT/run.log`,
including dry runs and preflight failures. The timestamped log includes a unique
run ID, request hash and gap ID, every HTTP attempt and its duration, HTTP status,
provider request IDs when supplied, retry decisions, and the final summary.
Error response bodies (JSON or plain text) are redacted and retained up to 8 KiB;
truncation is explicitly marked. A rejected 200 response also logs the precise
validation failure. The terminal prints a short failure reason as it happens.
Only selected diagnostic response headers are retained; authorization headers,
cookies and known API keys are not logged. Each entry is flushed immediately,
and old entries are preserved across retries and subsequent runs.

Jev's live probability values can total 0.99 when reported in hundredths. The
validator permits a fixed absolute sum error of at most 0.01 (plus floating-point
epsilon), while still requiring every expected option and finite probabilities
between zero and one. Accepted non-unit sums are recorded in `validation_warnings`
in the result and attempt log. Raw probabilities, scores, choices, and confidence
are preserved; the script does not renormalize or recompute them. Larger sum
errors and malformed responses still fail validation. This tolerance is a local
compatibility policy based on observed responses, not a documented API guarantee.

To revalidate previously rejected HTTP 200 responses without paying to resend
them, run:

```sh
.venv/bin/python scripts/prioritize_demo_gaps.py recover --output .runtime/jev-demo-full
```

Recovery verifies the prepared request and saved error identity, accepts only
complete response bodies that pass the current validator, and writes successful
results into the normal cache. It makes no network calls. Existing successful
results and historical error files are preserved. Recovery provenance appears
in each recovered result, `events.jsonl`, `run.log`, and `last-recovery.json`.
Subsequent runs skip recovered results; regenerate the report to include them.

```sh
tail -f .runtime/jev-demo/run.log
```

Every valid success is saved immediately under its full request hash. Re-running
uses the cache; changed prepared inputs or corrupted cached results are rejected.
A process lock prevents simultaneous runners on the same preparation. Provider
idempotency is not assumed: a timeout or crash after server acceptance but before
local persistence can incur a duplicate billed attempt on retry. `--retries 0`
avoids automatic retries, but cannot eliminate that network ambiguity.

## Outputs and interpretation

- `manifest.json`, `gap-catalog.json`, `requests/*.json`: exact inputs, source
  versions, request hashes, size estimates, comparison omissions, blocked gaps.
- `results/*.json`: raw validated responses, actual returned model, token usage,
  request/response hashes, elapsed time, attempts, and completion time.
- `errors/*.json`, `events.jsonl`, `last-run.json`: failures and execution journal.
  Failures include the HTTP status, redacted response body, selected response
  headers, and transport or validation details when available.
  Old error files remain historical if a later attempt succeeds; valid results
  determine current completion.
- `run.log`: append-only attempt history, retry decisions and run summaries.
- `answers.json`: all successful typed answers, including custom rubrics.
- `report.json`, `ranking.csv`: component scores, blocker, anchor, review flags,
  ranked candidates, a shortlist, and completed/pending/blocked counts. CSV is
  written only when there are standard-rubric results.
- `review-candidates.md`: readable candidates that pass every gate except model
  confidence, with their questions, anchors, component scores and confidences.
  The same rows appear under `report.json.review_candidates`.

The transparent ranking is
`25 × (0.4 evidence_fit + 0.3 answerability + 0.2 demo_clarity + 0.1 distinctiveness)`.
The direct prioritization probability breaks ties and is retained separately.
The default shortlist excludes prioritization probability <0.5, overclaim probability ≥0.5, any rubric confidence
<0.5, evidence-fit/answerability scores <2, and `best_anchor=none`. These are
initial operating thresholds, not validated scientific cutoffs. The shortlist
allows at most two gaps per disease and suppresses candidates whose top-five
anchor Jaccard overlap exceeds 0.6 with an already selected candidate. The full
ranking retains every successful evaluation and its review flags.

An empty automatic shortlist does not mean the batch failed or that the ranked
results are empty. The report now includes `shortlist_diagnostics`: overlapping
counts of each exclusion reason, the model's main blockers, and a sequential
gate funnel showing exactly where candidates disappear. In particular, the
minimum-confidence rule requires **all four** rubric confidences to meet the
threshold, including distinctiveness. Jev's [confidence](https://docs.typesafe.ai/confidence)
describes concentration across score levels; it is not a calibrated likelihood
that the gap makes a good demo. A plausible fractional score can have low
confidence because probability is spread across adjacent levels.

For exploratory demo selection, `review_candidates` lists rows rejected **only**
by the confidence gate. It preserves their flags and all four confidences, and
applies the same size, disease, and factor-overlap limits. These are candidates
for manual inspection, not automatically approved demos. Evidence/answerability,
prioritization, overclaim-risk, and anchor checks remain mandatory for this list.
The CLI prints both `shortlist_count` and `review_candidate_count`.

Run `report` again to compute diagnostics and review candidates entirely from
saved responses; no Jev requests or re-preparation are needed. Changing
`--min-confidence` also only reprocesses the report, but changes the automatic
shortlist policy and should be an explicit choice.

Report usage totals include successful responses only, not unreported provider
work on failed or retried calls. Reports flag mixed returned model versions.
Before choosing the actual demo, run the shortlisted question and anchor through
REVEAL and inspect its current evidence and resulting scientific account.

## Validation

```sh
PYTHONPATH=services/backend/src .venv/bin/python -m unittest discover \
  -s services/backend/tests -p 'test_demo_prioritization.py'
```

Tests cover actual gene weights/truncation, maximum-context ranking, comparison
omission, blocked payloads, dry runs, HTTP retry behavior, resume without
duplicate completed calls, tamper detection, malformed responses, and ranking
review flags. Mock HTTP tests do not certify Jev's live responses or tokenization.

On September 29, 2026, the three Type 2 Diabetes pilot requests using the reduced
20×20 defaults succeeded against `jev-1.13.0`, each on its first HTTP attempt
once network access was available. Returned input usage ranged from 16,246 to
16,861 tokens per request (49,359 total input and 1,167 total output tokens).
The saved requests, responses, report, and append-only log are in
`.runtime/jev-demo-compact/`. This verifies those three payloads; other gaps can
have longer focal context and still require size adjustment.
