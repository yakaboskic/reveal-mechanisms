# Plan: admin console refactor, one agent-job model, and claims-repository-only science

Drafted 2026-11-18 on `chase/generic-agent-job`. Nothing here is implemented yet.

## Settled choices

These are decided; change them by editing this plan.

1. **Restyle:** port the claims console's patterns and design tokens into scoped plain CSS and a small set of local
   components.
   - The claims console uses Tailwind v4, shadcn/ui and TanStack; REVEAL's frontend has none of them, and its admin
     CSS is one 12-line file (`services/frontend/src/app/admin/admin.css`).
   - Porting adds no dependency, and Tailwind's reset cannot leak into the public REVEAL pages.
2. **Prompts and responses:** any admin can open one job's full content through a separate endpoint that logs who
   read which job. The 15-second overview stays sizes-only. This deliberately reverses commit `c7486dd`.
3. **Box snapshots:**
   - Failed agent jobs only, each kept for 14 days. Admins can switch to "all agent jobs" in the Limits tab.
   - The transcript capture covers most inspection; a snapshot adds the sandbox's filesystem on top.
4. **Job detail:** a full page at `/admin/jobs/[id]`. The old `/admin?job=` links are dropped.
5. **Paper sources:**
   - The agent fetches each paper itself and records how in a DAPPER Activity (Phase 1).
   - The saved bytes stay in REVEAL's private store. The deposited account carries the URL, the sha256, the
     Activities and the quote locators, never the paper.
6. **Prompt-injection screen:**
   - Fixed checks on the account, plus Llama Prompt Guard 2 22M baked into the backend image (step 1.9).
   - An unsafe account is still returned to the requester, but it is never written to the claims database, and the
     reason is recorded in telemetry. There is no review queue.
7. **No backward compatibility.**
   - Old input formats, deprecated fields, the `read_paper` quote path and the "either deploy order" rule all go.
   - Accounts already in the claims database stay there, but REVEAL no longer re-verifies them.
8. **One job model.** Analysis and paragraph jobs become `agent` jobs with the deliverables `gap_analysis` and
   `paragraph`. They share Phase 1's sandbox, source model and deposit screen (Phase 5).
9. **Science lives only in the claims repository.**
   - REVEAL keeps operational state only.
   - Each job ends with one deposit carrying every DAPPER object its result references, as `scientific_account`
     jobs do today. That includes objects REVEAL did not author, copied verbatim.
   - A deposit names a collection or none. Results default to none.
   - First, `scientific_account` jobs deposit and are served back from the claims repository (Phase 6).
   - `answer` jobs answer the requester directly and deposit nothing.
   - Gap analyses, paragraphs, the catalog and the rest of the pages come later (Phase 7).

## What the investigation found

### Paper tool "timeouts" are an unreliable single upstream

The tools are `search_papers` and `read_paper` (singular; one paper per call). Both call Europe PMC REST only
(`box_literature.py:17`), with no retry, no fallback source and no per-run cache.

Measured on the four agent jobs in your own QA workspace: 89 paper-tool calls, 11 failed.

| Failure | Count | What happened |
|---|---|---|
| `read_paper` HTTP 500, empty body | 7 | Europe PMC `fullTextXML` |
| `read_paper` "TimeoutError: evidence service unavailable" | 3 | Each took ~20.3 s: the 20 s urllib socket timeout (`box_literature.py:108`) |
| Wrong tool name `mcp__reveal:search_papers` | 1 | The model invented the name; the repo never uses it |
| Search budget exhausted | 1 | Expected behaviour |

- **No sign of the 35 s wall deadline or the "outstanding reads" slot error.** Slot starvation (`box_mcp.py:382-405`)
  is a real latent problem, but it is not what you have been seeing.
- **The agent wasted its turns.** In job `989b2113` it asked for PMC3491803's full text six times in two minutes:
  two timeouts, then four 500s. The tool never says that a failure is permanent or what to do instead.
- **Successful calls** take a median of 1.3 s, but some reach 19–20 s, right at the socket timeout.
- **Europe PMC was down on 2026-11-18.** Both `search` and `fullTextXML` returned nginx 503 in about 0.4 s. NCBI's
  PMC open-access BioC endpoint returned 200 for the same paper.

Caveats: this is four jobs from one workspace, so no failure rate is quoted. Events were paged with `next_after`, and
the page shape was not double-checked.

**Resolution:** Phase 1 removes these tools from agent jobs. The agent fetches with its own commands, sees each HTTP
status directly, and can go to another source (NCBI, bioRxiv, the publisher) when one is down.

### Why the paper tools are bespoke, and whether WebFetch would do

> **Superseded on 2026-11-18.**
> - Scientific Account jobs get the same freedom as answer jobs.
> - The agent fetches each paper with its own command, saves it, records the fetch as a DAPPER Activity, and cites
>   a location in that saved file.
> - REVEAL fetches nothing; it only verifies (Phase 1).
> - Two points below still hold: WebFetch output cannot be quoted, and every quote must sit in bytes REVEAL keeps.
>   The rest is history.

- **Answer jobs** can already use WebFetch: it is opt-in and needs `network: "open"` (`box_agent.py:33-36,227`).
  Nothing to change.
- **Scientific-account jobs** must cite verbatim quotes, and REVEAL ties each quote to the exact bytes captured by
  the `read_paper` call that served it (`literature_locator.py`, `bind_sources`; `docs/agent-jobs.md:201-204`).
  WebFetch cannot do this, for four reasons:
  1. Claude Code's WebFetch passes the page through a small model and returns that model's answer, not the
     page text, so quotes would not be verbatim. Confirm this in the Box's pinned Claude version (step 1.7).
  2. The fetch happens inside Claude Code, so REVEAL never captures the bytes the agent read, and the
     ValueLocator binding breaks.
  3. Paper budgets and the closed network of account jobs would be lost.
  4. It would have failed in the same Europe PMC outage.
- **Conclusion:** keep a REVEAL-owned reading tool, but make it robust and multi-source (Phase 1). Later, it can
  grow into a captured `fetch_source` over an allowlist of scholarly origins, which gives WebFetch's reach with
  provenance (step 1.8, optional).

### Agent-job visibility gaps

- **The full transcript is lost.** Claude Code's stream is written to `/reveal/state/claude-stream.jsonl` and
  `claude-stderr.txt` (`box_remote.py:860`). Capture only takes `output/`, `ledger/` and `runtime.json`
  (`direct_capture.py:47`), so both files disappear when the Box is deleted.
- **The final response is stored**, but in pieces:
  - in the job result, cut at 50,000 characters (`box_agent.py:67,459`);
  - in full in `attempt-N/output/ledger/agent-response.json`, which is already in the admin diagnostics allowlist
    but is shown only as raw JSON, and only when it is under 128 KiB (`admin_jobs.py:74`).
- **The frozen input is never shown.** The queue record's frozen input (prompt, system prompt, question, claims,
  output schema, settings, limits) is read only for allowlisted settings and sizes (`telemetry.py:111-200`).
- **No Box link.** The Box id is recorded for each attempt, but the console shows it as text. Every Box is deleted
  at cleanup (`workflow_execution.py:514,591,887`).
- **Snapshots look feasible.** The Box SDK supports `box.snapshot(name=…)`, `AsyncBox.from_snapshot(id)` and
  account-level `delete_snapshots` (`upstash_box` 0.3.0). The API key file is deleted before the agent starts
  (`box_remote.py:787-788`), and the agent reaches Anthropic only through a loopback key proxy. A snapshot taken
  after the run should therefore hold no key, but this must be verified (step 4.2).

## Phase 0: land the work in progress (before anything else)

The working tree has uncommitted structured-output work in 37 files. It touches the files every phase below changes
(`agent_output.py`, `box_agent.py`, `box_remote.py`, `telemetry.py`, `AdminAgentJobs.tsx`, the contract files).

1. Finish, test and commit it on `chase/generic-agent-job`.
2. Start each phase below from a clean tree. QA runs a cherry-picked `release/qa-*` branch, so keep each phase in
   self-contained commits that cherry-pick cleanly.

## Phase 1: the agent fetches, saves and records each paper; REVEAL verifies

Scientific Account jobs get the same tools and network as answer jobs. There is no REVEAL paper tool and no
REVEAL-side fetching. The agent works like a careful scientist with a lab notebook:

1. **Fetch the paper with a command, and save the bytes.** For example
   `curl -sSL -o sources/PMC3491803.xml https://…/PMC3491803/fullTextXML`. Any source works: Europe PMC, NCBI,
   bioRxiv, a publisher's open-access page, a PDF.
2. **Convert it to text if needed, with another command.** For example
   `pdftotext -layout sources/x.pdf sources/x.txt`. This is a second step, and it is recorded too.
3. **Record each step as a DAPPER `Activity` in the account.** The Activity holds `command`, `software_name`,
   `software_version` and `generated_at_time`. The saved file is a DAPPER `File` whose `was_generated_by` names
   that Activity. A converted text file also has `was_derived_from` pointing to the raw file. `location` is the URL
   the bytes came from.
4. **Cite a location in its own saved file.** The citation is a `ValueLocator` naming the `File`, the verbatim
   quote as `value_text`, and a `selector` such as `line=212-213` (what `grep -n` prints).

The record says exactly how the paper was obtained and where the quote sits in it, so anyone can rerun the command
and check. These are the agent's own files, not REVEAL's internal capture records.

REVEAL's part is deterministic checking, offline:
- it computes each file's sha256 and size;
- it turns the selector into the exact byte span;
- it confirms the quote is at that span;
- it confirms the transcript shows the recorded command actually ran.

### Implementation notes (2026-11-19, from mapping the code)

These correct the steps below where they differ.

- **The agent authors in DAPPER 0.2.0, the release the Box pins.** 0.2.0 has `Activity` (`command`,
  `software_name`, `software_version`, `generated_at_time`) and `File` (`filename`, `location`, `mime_type`), with
  provenance as edges (`WasGeneratedBy`, `WasDerivedFrom`, `Used`). It has no `ValueLocator`.
  - The agent cites a quote as an `EvidenceItem` with `snippet` (the verbatim quote), `was_derived_from` the saved
    `File`, and optionally a line range in `context` (`line=212-213`).
  - REVEAL finds the quote in that file, and it must occur exactly once in the given lines (or in the whole file).
    REVEAL computes the byte span and mints the `ValueLocator` at deposit with 0.2.1-a1, as `account_deposit`
    already does today.
  - The Box's DAPPER pin does not move.
- **Code shared with research jobs.** Today `write_draft_tool`, `acceptance.replace_authored_attribution`,
  `source_validation.new_file_findings` and the empty-`source_artifacts` check erase or refuse agent-authored
  Activities and Files. Each change is gated on the agent `scientific_account` deliverable. REVEAL's own run
  Activity still replaces any attribution the agent writes. Only the source Activities and Files survive.
- **Step 5 uses the ledger, not the transcript.** Every Bash call is already ledgered with its full command and a
  success flag (`box_remote.py:882-891`).
  - Rule: an Activity's `command` must appear verbatim in a successful ledgered Bash call, and that call must name
    the file's path.
  - This removes Phase 1's dependency on step 2.1, which can move back to Phase 2.
  - Not yet verified: whether a process the agent starts could forge lines in Claude Code's stream. This needs a
    real Box.
- **Network.** Account jobs default to `network: "open"` and the answer-job tool set plus Bash, WebSearch and
  WebFetch. Dropping `literature` would otherwise remove Europe PMC from a restricted job's hosts.
- **Kit pin.** Account jobs have no versioned kit today. The new `construct-literature-account/SKILL.md` and the
  schema excerpt are pinned by sha256 in a test.
- **Step 11 deletes only agent-job-only code.** `cites_by_quotation`, `capture_descriptor` and
  `source_validation.bind_sources` also serve research jobs, so they stay until Phase 5. `literature_locator` and
  `literature_feedback` are agent-only and go.
- **Capture limits are per prefix.** `MAX_FILE` also bounds bootstrap files. Only `output/sources/` gets 25 MB per
  file. The run total stays well under the 256 MiB workspace cap.
- **New in-Box modules** use the standard library only, and join the bundle allowlist (`box_adapter.py:198-202`).
- **Left for you, because each needs an environment or an account:**
  - rebuilding the toolchain snapshot per environment (any recipe change makes existing snapshots stale);
  - exact apt version pins, which need the Box image's distro;
  - fetching the gated classifier model;
  - running the audit in a real Box.

  A deployment image without the model fails its build. It never skips the screen.

### Steps

1. **Remove the reading tools and open up the sandbox.**
   - In `box_agent.py`, drop the account-only restrictions at lines 82, 214, 230-235 and 338. Account jobs then take
     the same `tools`, `network`, `allowed_domains` and `graphs` as answer jobs, and default to Bash and the open
     network.
   - GitHub stays reachable for DAPPER (line 390).
   - Agent jobs no longer get `search_papers` or `read_paper` (`box_remote.py:527-541`).
   - Remove the `literature` input and the `literature_*` limits (`literature_searches`, `literature_search_results`,
     `literature_papers`, `literature_characters`) from the agent input, the admin limits and the console. A request
     that still sends them is rejected as invalid.
   - Analysis and paragraph jobs are unchanged until Phase 5.
2. **Give the Box what the agent needs.**
   - Add `curl`, `poppler-utils` (`pdftotext`) and `xmllint` to the pinned toolchain, at fixed versions, and rebuild
     the toolchain snapshot (`scripts/build_box_toolchain_snapshot.py`, once per environment).
   - Pinned versions are what make the recorded conversion commands reproducible.
3. **Capture the sources.**
   - The agent writes papers under `output/sources/`, which capture already takes.
   - Raise the capture limits for that folder: today they are 8 MB per file and 40 MB per run
     (`box_upload.py:21-23`). Allow something like 25 MB per file and 200 MB per run.
   - The source bytes stay in REVEAL's private store. A deposited account carries the Activities, Files (URL, sha256,
     size) and locators, never the paper. The existing quote caps still apply: 400 characters per quote, 4,000 per
     paper.
4. **Write the checks.** Replace the literature quote binding for agent jobs with a new module,
   `source_evidence.py`, used by `lint_account` during the run and by `agent_account.accept` at the gate. It checks:
   - every `File` an evidence locator names is a captured file under `output/sources/`, and REVEAL fills in its
     `sha256`, `size_in_bytes` and media type;
   - every such `File` has `was_generated_by` an Activity with a `command` and `generated_at_time`;
   - a converted file has `was_derived_from` a captured raw file, through its own Activity;
   - the `value_text` appears exactly once within the `selector`'s line range (or once in the whole file when no
     selector is given), and REVEAL writes the `byte_start` and `byte_length` it found;
   - the 20-to-400-character, single-paragraph and per-paper rules on quotes, as now;
   - each refusal says what to fix, e.g. "the quote is not on lines 212-213 of `sources/x.txt`; it occurs on line 240".
   The agent never computes checksums or byte offsets.
5. **Check the commands really ran.** Use the transcript capture from Phase 2.1 (move it into this phase).
   - For every Activity, find the Bash call in the transcript whose command wrote that file. REVEAL records it as
     `observed_command`, plus the call's time and exit status.
   - An Activity with no matching call is refused, so a file typed by hand cannot claim to be a download.
6. **Keep a reproducibility check out of the deposit path.**
   - Add an audit script and an admin button that reruns an account's recorded commands in a fresh, short-lived Box
     and compares sha256s.
   - It reports "reproduced" or "changed upstream". A changed upstream is not a failure: the original bytes are kept.
7. **Rewrite the agent's instructions.**
   - New authoring-kit version of `agent-skills/construct-literature-account/SKILL.md`, with its prompt bytes pinned
     in `test_authoring_pins.py`. The QA release branch gets a matching kit name, as with v3.1.
   - The instructions are the lab-notebook routine above, with one worked example of each step.
   - WebSearch and WebFetch are for finding papers only. Only text saved under `sources/` by a recorded command can
     be quoted.
   - Run `lint_account` after each paper's evidence is drafted.
8. **Screen for prompt injection before depositing.** A new module, `deposit_screen.py`, runs between acceptance
   and the claims-repository deposit (`account_deposit.py`). It has two checks.
   - **Fixed checks on the account.** These are the real protection.
     - Free-text fields carry no URLs, code, markup or instruction-like text.
     - Entities and predicates come from the requested claims, their ontology namespaces, or the cited sources.
     - No claims are added beyond those requested.
     - Activity commands are plain fetch and convert commands: no pipes into shells, no writes outside `output/`.
     - Every field has a size cap.
   - **The classifier (step 1.9).** It scores the saved source files, the other tool results in the transcript, and
     the account's free text. Nothing leaves REVEAL.
   - **If either check fails, the account is not saved.**
     - The job still succeeds, and the requester still gets the agent's response and the account document in the job
       result.
     - The deposit status becomes `withheld_unsafe`, and the job result says, in one plain line, that the account was
       not stored and why.
     - Nothing is written to the claims database.
     - Telemetry records which check failed, the classifier score and threshold, and where the text came from:
       source file and line range, or transcript turn. The admin console shows this.
     - There is no review queue and no admin action.
   - **Tuning.** Classifiers miss some attacks and flag harmless text, and a flag here costs a deposit. Track the
     withheld rate by check in telemetry, and tune the threshold from it.
   - Update the README's "No second AI review runs" line: this screen looks for attacks, not scientific quality.
9. **Bake the classifier into the image, and load it once per process.**
   - **Model:** Llama Prompt Guard 2 22M, as a pinned revision. Its weights are 283 MB, so they fit easily in the
     worker task (1 vCPU, 3 GB) and the API task (1 vCPU, 4 GB) (`deploy/dig/render_workers.py:127`,
     `deploy/dig/service.yaml:9-10`). The 86M variant (1.1 GB, multilingual) is not needed for English papers.
   - **Fetch it with the other deployment assets.** `scripts/platform_bundle.py --fetch-assets` downloads it into
     `.deployment-assets/prompt-guard-2-22m/`, the same way DAPPER and the RDS certificate get there. It checks a
     pinned sha256 and converts the weights to ONNX once.
     - The model is gated, so accept its licence on Hugging Face once, and have the fetch use an operator token.
     - The platform's CI never downloads from Hugging Face, and needs no token.
   - **Build the image with it inside.** The `deployment` stage of `services/backend/Dockerfile` copies the folder
     to `/app/models/prompt-guard-2-22m`. A `RUN python -c "…verify_model()"` line checks it at build time, as
     `verify_release` does for DAPPER.
     - The runtime needs only `onnxruntime` and `tokenizers`, not PyTorch, which keeps the image about 300 MB
       larger instead of more than 1 GB.
   - **Load it once per process.** `deposit_screen.py` holds one ONNX `InferenceSession` (one thread), created and
     warmed with a single dummy input when the process starts, next to the existing DAPPER `prewarm()`
     (`acceptance.py:343`). No job loads the model.
   - **Measure on the 1-vCPU task before relying on it.** Target: under 50 ms per 512-token window, and under 5 s
     to screen a job with a few full papers. Long texts are scored in overlapping 512-token windows.
   - **Fallback:** if the measured latency or false-positive rate is unacceptable, ProtectAI's
     `deberta-v3-base-prompt-injection-v2` (Apache-2.0, ungated, ONNX ready) slots in the same way. It is 738 MB and
     slower.
10. **Show it in the admin console.** For each source: URL, command, observed command, size, sha256, and the
   reproducibility result. On each job: the lint refusals and any screen flags.
11. **Delete the old quote path.**
    - Remove `literature_locator.py` and `tests/test_literature_locator.py`.
    - Remove their uses in `account_deposit.py`, `agent_account.py`, `box_adapter.py`, `box_mcp.py` and
      `box_remote.py`: `bind_sources`, `literature_feedback`, `capture_descriptor`, `cites_by_quotation`.
    - Remove the agent-job paper budgets in `ScopedTools`.
    - Remove the old-format agent input handling (`reveal.agent-input/1` and "before job types existed") from
      `box_agent.py`, `telemetry.py` and the console.
    - Fold `reveal.agent-input/3` back into one input format. Phase 0's structured output added `/3` only so that
      `/2` jobs stayed byte-identical for older workers; `output_schema` becomes an optional key of the one format.
    - Remove the superseded authoring kits.
    - `box_literature.py` stays only until Phase 5. Analysis jobs (Box run kind `research`) still use it for their
      own captures (`box_research.py`, `box_remote.py:806`). Phase 5 deletes it.
12. **Update the docs and the API contract.**
    - `docs/agent-jobs.md` (replace "The sandbox is closed") and the `openapi_agent.py` descriptions.
    - Regenerate the contract files.
    - Update `scripts/scientific_account_job.sh` if it sends `literature`.

**Done when** all of these hold:
- an account job that fetches one paper as XML and one as a PDF (converted with `pdftotext`) deposits an account
  with an Activity for every fetch and conversion, and a locator for every quote that REVEAL re-verifies byte for
  byte;
- a file written by hand, with an invented Activity, is refused;
- a source carrying planted instructions leads to `withheld_unsafe`. The requester still gets the response and the
  account, nothing reaches the claims database, and telemetry records the reason;
- the screen runs with the model already loaded, and its measured time is recorded;
- the audit script reproduces the XML fetch;
- no code path refers to `literature_locator` or the removed fields.

Test deposits only against scratch or throwaway databases: QA and prod share the claims database.

## Phase 2: capture everything a run did, and serve it to admins

1. **Capture the transcript.** This step ships with Phase 1, because Phase 1's command check (step 5) and injection
   screen (step 8) read the transcript.
   - Write `claude-stream.jsonl` and `claude-stderr.txt` into `ledger/`, so the existing capture and its secret
     scan (`box_adapter.py:593`) cover them.
   - Gzip them and cap each at about 20 MB. If a file exceeds the cap, keep its head and tail and record that it
     was cut.
2. **Record the run's full configuration in `runtime.json`.** This covers:
   - the Claude CLI arguments (`box_remote.py:832-841`);
   - the MCP tools actually exposed;
   - the network policy actually applied;
   - the Claude Code version and toolchain snapshot id;
   - the authoring kit version;
   - the effective limits.
3. **Add an admin endpoint for one job's content:** `GET /internal/v1/admin/jobs/{id}/run`, under a new assertion
   purpose `admin_content`. It returns:
   - the frozen input in full (prompt, system prompt, question, claims, output schema, settings, limits);
   - the configuration from step 2.2;
   - the full response record and any structured output;
   - the list of output files, each with a download link;
   - the transcript, paged and turned into readable turns (assistant text, tool calls with arguments, tool results).
   Each read is logged with the admin's email, the job and the time.
4. **Reverse `c7486dd` deliberately.**
   - The 15-second snapshot stays sizes-only.
   - Update `test_admin_telemetry.py` to assert that the snapshot still selects no content, and that only the new
     endpoint does.
   - Replace the "never shown" copy in `AdminAgentJobs.tsx`.
5. **Verify first.** Open one QA agent job and check whether `agent-response.json` already shows up in its saved
   diagnostics. If it does, the response part of this phase is mostly UI work.

## Phase 3: restyle the admin console like the claims console, keeping every field

Patterns to bring over, from `claims-repository/frontend/src/components/admin/`:

- **Shell:** a 224px sticky sidebar holding the navigation, the signed-in admin and sign-out, plus a top bar
  naming the section.
- **Page structure:** each page has a `PageHeading` with a one-line subtitle, filters in a single row, then content
  in `Panel`s that each carry a plain-language description.
- **Shared components:**
  - `Metric` cards that link to filtered lists;
  - a `Status` pill with tones neutral, good, warn, bad and info;
  - `KeyValues` (a definition-list grid) and `Callout` for the result first;
  - `Pre` and `Json` with height caps, with raw JSON last inside `<details>`;
  - short ids and short ages with the full value on hover and a copy button;
  - `Pagination`, and empty states written out in words.
- **View state in the URL:** filters, the page and the open item, as in claims' `use-url-state.ts`.
- **Styling:** monochrome tokens with colour only on status; Geist type scale; hairline borders.

Navigation, mapped from today's seven tabs. Nothing is dropped.

| New page | Holds |
|---|---|
| Overview | The five totals, the status counts, and the latest failures and running jobs |
| Jobs | Today's Execution table, all nine columns, with filters for kind, status and owner |
| Agent jobs | Today's Agent jobs tab: per-type cards and the ten-column table, plus columns for sources fetched, lint refusals and withheld deposits (with a "withheld" filter and the reason) |
| Costs | Today's Costs tab |
| Database | Tables (with the inspector), imports and embedding runs, record types |
| Activity | Recent record changes and the job event stream |
| Performance | Runtime metrics, plus fetch failures and latency by host, from the agents' recorded fetch commands |
| Limits | Agent limits, plus the snapshot policy from Phase 4 |

The job detail at `/admin/jobs/[id]` gets these sections:

1. **Summary.** A status callout with the failure code, message, recorded cause and saved `failure.json` files.
   Then key values: created, started, updated and finished times; the current attempt's start; worker; Box;
   attempts and recoveries.
2. **Prompt and settings.** The full prompt, system prompt, question, claims and output schema; every setting and
   limit; the CLI and MCP configuration from step 2.2.
3. **Response.** The rendered answer, with a copy button; any structured output; output files; the deposit and
   account, linked to the claims console.
   - **Sources:** each fetched file with its Activity (command, observed command, time, software), URL, size, sha256
     and reproducibility result, and the quotes that cite it.
   - **Screen:** the result; for a withheld account, which check failed, the score, and the passage and where it
     came from.
4. **Transcript.** Turn by turn, including tool calls and results, with each failed call highlighted.
5. **Event log.** Today's log, unchanged: search, "load earlier", copy, tool arguments and output, event data.
6. **Usage.** Everything in today's `AgentUsage`: cost, budget bar, turns, run time, API time, tokens, tool calls
   and failures, lint checks and draft writes, model, outcome, runner reason.
7. **Timings.** Today's phase-timing bars.
8. **Sandbox.** The Box console link and the snapshot (Phase 4).
9. **Diagnostics, attempts and warnings.** As today.

Keep the 15-second auto-refresh, the time-zone note and "showing stale data" banners, and the development-bypass
notice. Before deleting any old component, write a field-by-field checklist of the current console (every column
and every `dl` row in `AdminConsole`, `AdminJobDetail`, `AdminAgentJobs`, `AdminCosts`, `AdminLimits`,
`AdminPerformance` and `AdminTableInspector`) and tick each field off on the new pages. Update
`tests/admin*.test.ts` to match.

## Phase 4: the Box link and Box snapshots

1. **Console link.**
   - Find the real Upstash console URL for a Box: open one in the console, since the SDK has no helper.
   - Make it configurable as `REVEAL_BOX_CONSOLE_URL_TEMPLATE`, not hard-coded.
   - Show the link for each attempt, labelled live or "deleted at …".
2. **Verify before building.** Does a snapshot survive `box.delete()`? What does it cost, and how long can it be
   kept? Is any secret left on disk after a run? Answer these with one throwaway QA Box.
3. **A new workflow step, `preserve`,** between capture and each of the three `delete_once` sites. It must be
   idempotent and must never block cleanup.
   - Applies when the snapshot policy covers the job.
   - Confirm `/reveal/credentials.json` is gone, then scan `/reveal/state`, the agent's home directory and `/tmp`
     for the key bytes. If any are found, skip the snapshot and record why.
   - Then call `box.snapshot(name="reveal-<job8>-a<attempt>")` and save the snapshot id, size and status on the
     attempt's Box handle.
4. **Retention.** A scheduled sweep deletes snapshots older than the policy allows (`delete_snapshots`). Its
   settings live in the Limits tab and are written under the existing `admin_settings` authority.
5. **"Open a copy".** An admin action creates a Box from the snapshot with the network locked, `keep_alive` off
   and a short life, then shows its console link. Each copy is a paid Box, so ask for confirmation and log the
   action.

## Phase 5: one job model — analysis and paragraph jobs become agent jobs

Today REVEAL has three ways to run Claude in a Box (`EXECUTION_KINDS`, `agent_execution.py:33`):

| Job kind | Box run | What it does today |
|---|---|---|
| `analysis` | `research` | Investigates a DisMech knowledge gap from a frozen evidence package. It uses REVEAL's graph, evidence and paper tools (`box_research.py`, `evidence_collector.py`, `read_evidence`, `write_outcome`, `search_papers`/`read_paper`) and returns a ScientificAccount or an insufficient-evidence outcome. |
| `paragraph` | `paragraph` | Writes a cited research statement from one accepted ScientificAccount (`box_paragraph.py`), with no network. |
| `agent` | `agent` | A caller's prompt; returns an answer or a ScientificAccount (Phases 1–2). |

After this phase there is one kind, `agent`, with four deliverables. Each deliverable is a preset: a pinned
instruction kit, an input builder and a deterministic output check. All four share one sandbox, one source model
(Phase 1), one deposit screen and one admin view.

1. **`answer`** — unchanged.
2. **`scientific_account`** — unchanged from Phase 1.
3. **`gap_analysis`** (replaces `analysis`).
   - **Input:** the knowledge gap and the selected anchors (EAGGL factors, CFDE gene sets, traits). The input
     builder resolves these to identifiers and short descriptions, not to a frozen evidence package.
   - **Sources:** the agent gathers evidence itself, under the Phase 1 rule.
     - Papers are fetched by command, saved, and recorded as Activities.
     - Graph and data queries follow the same rule. The agent runs the query with a command (for example `curl` of a
       SPARQL query to the graph endpoint), saves the result file under `output/sources/`, records the Activity
       (endpoint and query), and cites a `ValueLocator` (the row) in that file.
     - So graph rows and paper quotes are checked the same way, and the separate data-row binding
       (`source_validation.bind_sources`) goes.
   - **Output:** a ScientificAccount whose `Question` and `KnowledgeGap` are the gap's. If the agent cannot support
     a mechanism, it states an insufficient-evidence outcome in the same structure: a `Claim` with
     `ClaimStatusEnum` set accordingly, and the sources it checked. There is no separate outcome record type.
4. **`paragraph`** (replaces `paragraph`).
   - **Input:** one accepted ScientificAccount, by its id.
   - **Output:** a DAPPER `Paragraph` with `CitationOccurrence`s pointing at the account's claims and sources.
   - **Check:** the existing segment and citation rules (`box_paragraph.validate_paragraph_segments`).
   - **Sandbox:** the same as the other deliverables, so it can check a source if it needs to.

Steps:

1. **One input format and one execution path.**
   - `freeze_input` (`box_agent.py:194`) takes the four deliverables and their preset inputs.
   - Remove the `research` and `paragraph` branches from `box_adapter.py:222-310`, `box_remote.py`,
     `workflow_execution.py` and `research_execution.py`.
   - Remove `EXECUTION_KINDS` and `authoring_stage` (`agent_execution.py`).
   - Remove the job kinds `analysis` and `paragraph`.
2. **Delete the analysis-only machinery.**
   - Evidence packages: `evidence_collector.py`, `evidence_package.py`, `evidence_budget.py`, `evidence_files.py`,
     `evidence_reader.py`, `stage_file_evidence`, `schema/evidence-package*`.
   - The hosted and progressive research client: `box_research.py`, `research_tools.py`, `research_hosted.py`,
     `research_http.py`, `research_oauth.py`, `research_access`.
   - The MCP evidence tools: `read_evidence`, `write_outcome`, `query_graph` and the `ScopedTools` budgets.
   - `box_literature.py`, and `box_mcp.py` itself if nothing is left in it. Keep `lint_account` and
     `write_account_draft`, as plain Python helpers on the agent's path rather than MCP tools, if that is simpler.
   - Remove whatever `tests/` cover these.
3. **Write one instruction kit for each deliverable.** These are the new authoring-kit versions for `gap_analysis`
   and `paragraph`.
   - Same lab-notebook routine as Phase 1: fetch, save, record the Activity, cite the location.
   - Each kit adds only what is specific to its deliverable.
   - Replaces `construct-scientific-account`, `read-evidence-package` and `write-cited-paragraph` under
     `agent-skills/`.
4. **Point the frontend at the new path.**
   - The research composer, research modes, CFDE assessment, the Lightning flows and replay submit agent jobs with
     a deliverable, not `analysis` or `paragraph` jobs.
   - Remove the old job kinds from `api.generated.ts` and the fixtures.
   - The admin console groups jobs by deliverable.
5. **Screen and deposit the same way.** Phase 1's deposit screen and `withheld_unsafe` rule apply to every
   deliverable that writes to the claims repository. `gap_analysis` accounts and paragraphs keep REVEAL's own
   tables until Phase 7, after the same screen.

**Done when:**
- a gap analysis on a DismechGap and a paragraph on its account both run as `agent` jobs, with every source
  recorded as an Activity and cited by a `ValueLocator`;
- the old kinds, evidence packages and research tools are gone from the code and the API contract.

## Phase 6: deposit Scientific Accounts and serve them back from the claims repository

**Scope:** `scientific_account` jobs only.
- An `answer` job answers the requester directly and deposits nothing. Nothing about it goes into DAPPER.
- Gap analyses, paragraphs and the catalog are deferred to Phase 7.
- This phase does not wait for Phase 5.

### What the claims repository already serves

Built on `chase/collections-and-deposits` and checked against the code on 2026-11-18:

- `POST /v1/deposits`:
  - takes an optional `collection`/`release`;
  - is stored whole or not at all;
  - leaves objects it already holds as stored.

  The receipt names, for each object, whether it `stored_differs`, and lists `unresolved_references`. Those are
  reported, not refused.
- `GET /v1/deposits/{id}` returns every object id and class a deposit carried, plus its edges in order.
- `POST /v1/objects:batchGet` returns up to 500 ids per call, with the ids it could not find.
- `GET /v1/objects/{id}` and `GET /v1/objects/{id}/lineage`.
- `GET /v1/objects/search?class_name=…&references=…`.

Two limits apply:
- A deposit holds at most 1,000 objects and 5,000 edges, and 16 MB.
- A claim id resolves only once a query has returned that claim. Account jobs get claim ids from `runQuery`
  responses, which store them, so citing such a claim does not leave an unresolved reference.

### Steps

1. **Keep today's deposit, and tighten its receipt check.**
   - `commit_account` (`workflow_execution.py:823`) keeps its order:
     1. honour a pending cancellation;
     2. send the deposit;
     3. record the receipt before anything else, so a retry never deposits twice.
   - The deposit screen (step 1.8) runs on the whole document first. `withheld_unsafe` sends nothing.
   - A receipt with `stored_differs` on any object, or any `unresolved_references`, marks the deposit `failed`
     with reason `inconsistent`. The reason goes to telemetry.
   - The job result carries `account_id` and `deposit`: the status, the `deposit_id`, and the collection and
     release if there is one.
   - Accounts are deposited with no collection.
   - Deposited accounts are readable by anyone who can reach the claims repository. Its object, deposit and search
     routes have no read check (`service/app.py`); whether the platform gateway adds one is not verified. This is
     already true today. The request docs say so.
2. **Serve the account back from the claims repository.**
   - `GET /v1/jobs/{id}/account` rebuilds the document:
     1. read `GET /v1/deposits/{deposit_id}`;
     2. fetch its objects with `objects:batchGet`, in chunks of 500;
     3. lay the edges back in their stored order.
   - Before returning, check that the rebuilt document has the job's `account_id` and the same sha256 as the
     document that was sent. A mismatch is a 502, recorded in telemetry.
   - The response says `"source": "claims_repository"`.
   - The route sits behind a short-TTL read-through cache keyed by deposit id. Phase 7 reuses the cache.
   - **When the claims repository holds nothing** (`withheld_unsafe`, `failed` or `not_configured`), the route
     serves the job's own checkpointed copy, marked `"source": "job_artifact"` with the deposit status. The
     requester always gets their account (settled choice 6).
   - The route stays owner-only and `no-store`.
3. **What REVEAL stops keeping.**
   - The `agent_account` record drops `document`.
   - It keeps only the private paper-text index (`texts`), which `/v1/jobs/{id}/account/texts/{sha256}` serves.
     Those bytes are never deposited (settled choice 5).
4. **Frontend, script and contract.**
   - The job view shows the deposit status and links the account to its claims-repository object page.
   - `scripts/scientific_account_job.sh` reads the account back through REVEAL's route and checks for
     `"source": "claims_repository"`. Its uncommitted change from the claims-repository work lands first.
   - Regenerate the contract files.

**Test only on scratch:** QA and prod share the claims database. So the round trip (deposit, read back, compare
sha256) is tested against a local claims repository on scratch SQLite. The first real deposit is a QA job you run.

**Done when:**
- an account job deposits one self-contained document;
- `GET /v1/jobs/{id}/account` returns it rebuilt from the claims repository, byte-identical to what was sent;
- a withheld or failed deposit still returns the account from the job artifact, marked not deposited;
- no REVEAL table holds the account document.

## Phase 7 (later): the rest of the science moves to the claims repository

Deferred on 2026-11-18. It is recorded so later work keeps to it. The rules are Phase 6's:
- one self-contained deposit per job that produces DAPPER objects;
- objects REVEAL did not author are copied verbatim, because ids are content-addressed;
- the collection is optional;
- the deposit is screened first;
- REVEAL keeps only references.

1. **`gap_analysis` and `paragraph` deposits.**
   - The deposit carries the KnowledgeGap and anchors, or the Paragraph with its CitationOccurrences and the
     account objects they cite.
   - Until this ships, these results keep REVEAL's tables (Phase 5).
2. **The catalog as collection deposits.** The `dismech` collection and its `gaps-by-disease` and
   `gap-with-anchors` queries exist in the toolkit. Still to build:
   - EAGGL factors and CFDE gene sets by trait;
   - gnomAD constraint by gene;
   - registering `dismech` on QA.

   QA and prod share the claims database, so rehearse each load on scratch and send the real one once.
3. **Switch the gap, factor, trait, account, provenance and leaderboard pages** to the claims repository, behind
   one short-TTL read-through cache keyed by object id and collection release. Phase 6 adds the cache, for its job
   routes. The leaderboard and "accounts by requester" come from REVEAL's own jobs, deposit ids and votes.
4. **Delete the local science.**
   - Dump the scientific `reveal_records` kinds and the `reveal_ref_*` tables to S3 once, then drop them.
   - Delete the import scripts and the modules that served them (`catalog.py`, `eaggl_database.py`,
     `research_data.py`, `factor_details.py`, `trait_details.py`, `publication.py`, `analysis_outcomes.py`, the
     `provenance_*` readers).
   - Nothing is migrated.

## Order and rollout

Order: 0 → 1 (with 2.1) → 6 → 2 → 3 → 4 → 5 → 7.

- Phase 1 ships together with Phase 2.1 (transcript capture). The new sandbox, the checks, the command check
  and the injection screen go to QA together, so an open-network account job never deposits unscreened.
- Fetch and verify the classifier asset before bundling the platform release (step 1.9).
- The toolchain snapshot (step 1.2) is rebuilt per environment before the backend that expects it is deployed.
- Phase 3 needs the endpoint from Phase 2.
- Phase 4 needs the console from Phase 3.
- Phase 5 needs Phase 1's source model and deposit screen. It can start in parallel with Phases 3 and 4.
- Phase 6 comes right after Phase 1, and needs nothing from Phases 2–5. Its claims-repository side is built.
  - It needs Phase 0 because both edit `workflow_execution.py`, `agent_account.py` and the contract files.
  - It needs Phase 1 because Phase 1 rewrites the account's deposit form and adds the deposit screen. Phase 6 then
    only checks and serves back the new form, and every account it deposits has been screened.
  - Phase 6 does not depend on Phases 2–4, but their admin console shows its deposit status once both exist.
- Phase 7 needs Phase 5 for the gap-analysis and paragraph deposits; its catalog work can start any time.

Each phase:

- deploys the backend and the frontend together, with no compatibility shims for older versions of either;
- regenerates the contract files (`build_openapi.py`, `validate_openapi.py`, `build_api_viewer.py`,
  `npm run generate`/`fixtures`);
- goes to QA through the cherry-picked release branch and `dig-service-platform`, as before.

QA and prod share one claims database, so test deposits only against scratch or throwaway databases.
