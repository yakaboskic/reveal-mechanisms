# General agent jobs

`kind: "agent"` runs a prompt as a general-purpose Claude Code agent in the same isolated Upstash Box
runtime that research jobs use. It is not an analysis or a research statement: no evidence package,
no DAPPER release, no scientific validation and no minted objects. You send a prompt (and optionally a
system prompt, tools and limits), watch the run on the ordinary job event stream, and get the agent's
final answer back as the job result.

## Enable it

Agent jobs are off until an operator names who may start them:

| Setting | Default | Meaning |
|---|---|---|
| `REVEAL_AGENT_JOB_USERS` | unset (off) | Comma-separated workspace user ids allowed to start agent jobs, of any principal kind (for example an `rvl_` API-key workspace). `*` admits every registered user, never an anonymous guest. |
| `REVEAL_AGENT_JOB_MAX_BUDGET_USD` | `5` | Budget ceiling per run, unless the admin console sets one. See [Limits](#limits). |
| `REVEAL_AGENT_JOB_MODEL` | `claude-opus-5-5` | Model when the request names none. |

They run only on the durable workflow runtime (`REVEAL_JOB_TRANSPORT=workflow`, as on QA). Elsewhere the API
answers `409 AGENT_JOBS_UNAVAILABLE`; a workspace that is not admitted gets `403 AGENT_JOBS_NOT_ENABLED`.
The usual active-job quota (`REVEAL_MAX_ACTIVE_JOBS`) applies.

## Submit one

```bash
curl --fail-with-body "$REVEAL_API_URL/v1/jobs" \
  -H "Authorization: Bearer $REVEAL_API_KEY" -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' --data-binary @- <<'EOF'
{"kind": "agent",
 "prompt": "List three open questions about LEPR variants and insulin resistance, one recent paper each. Save them as output/questions.md.",
 "system_prompt": "Be concise. Cite PMIDs.",
 "tools": ["Read", "Write", "Bash", "WebSearch"],
 "literature": true,
 "limits": {"timeout_seconds": 600, "max_turns": 30, "max_budget_usd": 1}}
EOF
```

| Field | Default | Notes |
|---|---|---|
| `prompt` | required | Up to 100,000 characters; the agent's first message. |
| `system_prompt` | none | Appended to Claude Code's own system prompt, after a short note describing the sandbox. |
| `tools` | Read, Write, Edit, Glob, Grep, Bash, WebSearch | Claude Code built-ins. `WebFetch` needs `network: "open"`. |
| `graphs` | `[]` | OKN knowledge graphs for the scoped graph tools (`get_schema`, `describe_kg`, `query_graph`). |
| `literature` | `false` | Europe PMC `search_papers` / `read_paper`. |
| `network` | `restricted` | `restricted`: only the Anthropic API plus the services of the chosen graph/literature tools and `allowed_domains`. `open`: the internet (package installs, downloads, `WebFetch`). |
| `allowed_domains` | `[]` | Up to 20 exact hostnames the restricted network may also reach, for example `eutils.ncbi.nlm.nih.gov` (PubMed E-utilities) or `api.platform.opentargets.org` (Open Targets GraphQL). No wildcards or IP addresses; not used with `open`. The agent calls them with Bash (`curl`, `python3`). |
| `claims_repository` | `false` (`true` for a Scientific Account job) | Offer the read-only `query_claims` tool. Needs a claims repository configured on the deployment; see [Return a Scientific Account](#return-a-scientific-account). |
| `deliverable`, `question`, `claims` | `answer` | See [Return a Scientific Account](#return-a-scientific-account). |
| `effort` | `medium` | `low`, `medium` or `high`. |
| `model` | `REVEAL_AGENT_JOB_MODEL` | A Claude model id. |
| `limits` | see [Limits](#limits) | Run time, turns, budget, and the tool and literature allowances. |

To let an agent search PubMed and query a GraphQL API without opening the whole network, give it Bash and
those two hosts:

```json
{"kind": "agent",
 "prompt": "Use NCBI E-utilities to find the 3 most recent PubMed articles on LEPR variants and insulin resistance, then query the Open Targets GraphQL API (https://api.platform.opentargets.org/api/v4/graphql) for LEPR's top associated diseases. Save output/summary.md citing PMIDs.",
 "tools": ["Read", "Write", "Bash"],
 "allowed_domains": ["eutils.ncbi.nlm.nih.gov", "api.platform.opentargets.org"]}
```

The request is frozen into the job when it is queued (`reveal.agent-input/2`), with every default filled
in, so the stored input says exactly what ran.

[`scripts/agent_job.py`](../scripts/agent_job.py) does all of this in one command with only the Python
standard library: it submits the prompt, prints stages, the agent's text and each tool call as they happen,
then the final answer. It reads an API-key handoff file (`--handoff`) or `REVEAL_API_URL`/`REVEAL_API_KEY`:

```bash
python3 scripts/agent_job.py --handoff key.json --tool Read --tool Write --tool Bash \
  --allow-host eutils.ncbi.nlm.nih.gov --allow-host api.platform.opentargets.org \
  --prompt "Find three recent PubMed papers on LEPR variants and insulin resistance with NCBI E-utilities ..."
```

## Limits

Every per-run limit has a **default**, used when the request leaves it out, and a **ceiling**, the most a
request may ask for. A request sets any of them in `limits`; a value above the ceiling is refused with `422`
and the message names the ceiling. An unknown key in `limits` is refused too.

| `limits` key | Built-in default | Built-in ceiling | Supported range | Meaning |
|---|---|---|---|---|
| `timeout_seconds` | 900 | 3,600 | 60 to 3,600 | Wall-clock time. The run is stopped at it. |
| `max_turns` | 50 | 100 | 1 to 1,000 | Claude Code turns. |
| `max_budget_usd` | 2 | 5 | above 0, to 1,000 | Provider cost. |
| `tool_calls` | 30 | 300 | 1 to 5,000 | `search_papers`, `read_paper` and graph tool calls, counted together. |
| `literature_searches` | 3 | 50 | 1 to 1,000 | `search_papers` calls. |
| `literature_search_results` | 5 | 25 | 1 to 50 | Most records one search may return. |
| `literature_papers` | 4 | 50 | 1 to 1,000 | Distinct papers `read_paper` may open. |
| `literature_characters` | 48,000 | 1,000,000 | 1,000 to 20,000,000 | Abstract and full-text characters returned across the run. |

The run stops at the first of `timeout_seconds`, `max_turns` and `max_budget_usd`, and the job fails with the
runner's reason. The other five never fail a job: when an allowance is used up, the tool call returns an error
that says so, and the agent can still finish and write its answer. Each paper and graph tool result also
carries a `run_budget` with what has been used and what is allowed, so the agent can pace itself.

Reading is charged by paper and by character, not by call. Paging through one paper with `offset` uses
characters only, and the PMID and PMC id of one paper count as one paper. A read that returns no text (an
error, a missing record) uses neither.

### Changing the defaults and ceilings

An operator sets each limit's default and ceiling in the admin console, under **Limits**
([admin telemetry](admin-telemetry.md#agent-job-limits)). A saved change applies to jobs queued after it, on
every API replica, with no deploy. The value in force is the first of:

1. the admin console;
2. the environment: `REVEAL_AGENT_JOB_DEFAULT_<NAME>` and `REVEAL_AGENT_JOB_MAX_<NAME>`, where `<NAME>` is the
   key in upper case without a leading `max_` (`REVEAL_AGENT_JOB_MAX_BUDGET_USD`, `REVEAL_AGENT_JOB_DEFAULT_TURNS`,
   `REVEAL_AGENT_JOB_MAX_LITERATURE_PAPERS`);
3. the built-in value in the table.

A value outside the supported range is ignored, and the console shows that. This includes the environment: `REVEAL_AGENT_JOB_MAX_BUDGET_USD=0` or an unparseable value no longer refuses every agent job, it falls back to the built-in 5. Use `REVEAL_AGENT_JOB_USERS` to turn agent jobs off. A ceiling set below the default
lowers the default to it. The supported range is fixed in code (`box_agent.LIMITS`): the Box adapter bounds a
run at one hour, and the in-Box runner validates the same ranges.

Lowering the budget ceiling also reaches jobs that are already queued: their frozen budget is cut to the
ceiling when the run is dispatched. The other limits stay as frozen.

These limits apply to general agent jobs only. Research runs keep their fixed allowances.

**Deploy the workflow workers before the API, or both together.** The API freezes new jobs as `reveal.agent-input/2`. A worker from before this change accepts only `/1` and fails such a job at prepare. The newer worker runs both: a `/1` job already queued keeps the built-in tool and literature allowances. Rolling a worker back while `/2` jobs are queued fails them.

Not adjustable, because they bound a single response or the sandbox, not how much work a run may do: 12,000
characters per `read_paper` call (continue with `offset`), the 100,000-character prompt, the 50,000-character
answer kept on the job row, and the output-file sizes below.

### Reading papers

`search_papers` and `read_paper` use Europe PMC. `read_paper` returns the **abstract** unless asked for more.
For full text, call it with `source: "PMC"`, the paper's PMC id and `section: "full_text"`. That works only
for open-access papers in Europe PMC; a PMID alone cannot fetch full text. Search results list each record's
`pmcid` and `isOpenAccess`, and an abstract read returns a `full_text` entry that either gives the exact
`read_paper` arguments or says why full text is unavailable. Full text is the title and paragraph text of the
article XML; tables, figures and supplements are not included.

## Return a Scientific Account

Set `deliverable` to `scientific_account` and give a `question`. The agent reads papers through the captured
literature tools, writes one DAPPER ScientificAccount that addresses the question, and REVEAL checks it and
deposits it in the claims repository.

```json
{"kind": "agent", "deliverable": "scientific_account",
 "prompt": "Prefer primary studies in mice and humans. Say what you did not read.",
 "question": "Does loss of leptin receptor signalling in hepatocytes cause hepatic insulin resistance?",
 "claims": ["Hepatocyte-specific deletion of LEPR reduces insulin-stimulated AKT phosphorylation in liver."],
 "limits": {"literature_searches": 10, "literature_papers": 20, "literature_characters": 400000, "tool_calls": 150}}
```

| Field | Notes |
|---|---|
| `question` | Required, up to 2,000 characters. It becomes the account's question, recorded as supplied by the requester (it is not a curated knowledge gap). |
| `claims` | Optional, up to 20 statements of up to 1,000 characters. Each becomes a fixed Proposition. The agent assesses it from what it reads, for or against, and cannot reword it. |
| `attribute_requester` | Optional, default `false`. Set it to `true` to be named in the account as the `Person` it is attributed to. See [who the account names](#who-the-account-names). |
| `prompt` | Still required: how you want the work done. |

What is different about such a job:

- **The sandbox is closed.** Tools are limited to Read, Write, Edit, Glob and Grep, the network is restricted, and
  `allowed_domains` and `graphs` are not accepted. Only text returned by `read_paper` during the run can be cited,
  so there is no shell and no other way to fetch a paper. `literature` is on.
- **It gets the time and turn ceilings by default**, because a run stopped at a limit returns no account. Ask for
  less in `limits` if you want. Raise the literature allowances for a real review; the built-in defaults are small.
- **Every piece of evidence is a verbatim quote from one paper.** The agent's `lint_account` tool refuses a quote
  that is not word for word in the text it was served, occurs more than once in the paper, or runs across
  paragraphs, and says what to change. A quote is 20 to 400 characters. An account may quote at most 4,000
  characters of one paper text, and at most 40% of a short one such as an abstract (one quote is always allowed),
  so a deposited account never carries a paper or a whole abstract.

After the run REVEAL applies the same gate a research account passes (pinned DAPPER release, source and snippet
checks), records what ran, and puts the account in its deposit form, minted with DAPPER 0.2.1-a1:

- each quote becomes a `ValueLocator`: an exact byte span, with the quoted text, in a File that is the paper's text;
- that File is named by SHA-256 and size and carries no text. REVEAL keeps the bytes for you and does not publish
  them. Its description says how the text is derived from the Europe PMC document, which is a second File with a
  public location, so anyone with the same document can rebuild the text and check the span;
- each paper is a `Publication` with its PMID, DOI and PMC id.

The account is the file `output/account-1.json`; the draft and lint tools accept no other name. An account that fails this gate fails the job with `VALIDATION_FAILED`. A job that ends without a saved account
fails with `AGENT_EXECUTION_FAILED`.

### The result

The job result gains an `account` block, and `response` is the agent's short report.

```json
"account": {"account_id": "dapper:ScientificAccount.…", "dapper_version": "0.2.1-a1",
  "document_sha256": "…", "document_bytes": 9120, "objects": 11, "edges": 4, "claims": 1, "value_locators": 1,
  "lint_warnings": 1,
  "requested_claims": [{"statement": "Hepatocyte-specific deletion of LEPR …", "assessed": true, "claim_ids": ["dapper:Claim.…"]}],
  "papers": [{"source": "PMC", "id": "PMC…", "section": "full_text", "reference": "PMID:…", "text_sha256": "…",
              "text_bytes": 48211, "media_type": "text/plain", "filename": "PMC….fulltext.txt", "evidence_items": 1,
              "file_id": "dapper:File.…"}],
  "deposit": {"status": "deposited", "deposit_id": "dep_…", "objects": 11, "stored_differs": 0, "unresolved_references": 0}}
```

- `GET /v1/jobs/{id}/account` returns the DAPPER document (owner only).
- `GET /v1/jobs/{id}/account/texts/{text_sha256}` downloads one quoted paper text (owner only): the bytes a
  ValueLocator's `byte_start` and `byte_length` address.
- A requested claim the agent made no Claim on has `assessed: false`. It is reported, not added to the account.

`scripts/agent_job.py --question … --claim … --account-out account.json` does all of this from a terminal.

### Deposit

When the job succeeds REVEAL sends the whole document to the claims repository in one request
(`POST /v1/deposits`). The repository stores every object under its DAPPER id and records the deposit against
REVEAL's key. An object it already holds is left as stored.

| `deposit.status` | Meaning |
|---|---|
| `deposited` | Stored. `deposit_id` is the repository's receipt. `stored_differs` counts objects it already held with different unhashed content; its copy stands. |
| `not_configured` | This deployment has no claims repository. The account is still returned. |
| `failed` | The attempt did not succeed (`reason`: `unavailable`, `rejected`, `unauthorized`, `too_large`). The job still succeeds and keeps its account. There is no automatic retry yet. A redirect from the repository is refused, not followed: the key is sent to the configured address only. |

A deposit cannot be recalled. A cancellation that arrives before the deposit is sent stops it.

### Who the account names

By default the account does not name you. DAPPER requires every Claim to be attributed to an agent, so the account
and its Claims are attributed (`was_attributed_to`) to one `Organization`, "REVEAL Mechanisms agent service", which
is the same object in every such account. Its `Activity` says that REVEAL built it, when, and from which paper
texts; the repository records which deposit key sent it. Nothing in the document or the deposit is derived from your
user record.

With `"attribute_requester": true` the account and each of its Claims are attributed (`was_attributed_to`) to a
`Person` for you: the display name of a registered user, the ORCID iD when it was authenticated, and a short
REVEAL actor code that is the same in every account you ask for. An unregistered requester appears as "Anonymous
researcher" with that code. The Person is deposited with the account, and a deposit cannot be recalled.

Either way the document does not include paper text beyond the quoted passages, REVEAL's internal capture files, or
the prompt.

Operators turn deposits on with two settings on the **workers**:

| Setting | Value |
|---|---|
| `REVEAL_CLAIMS_API_URL` | Base address of the claims repository, `https://…` (plain `http` only for localhost). |
| `REVEAL_CLAIMS_API_KEY` | A deposit key minted in the claims repository's admin console (Keys). It names REVEAL as the depositor. |

The key stays in the worker's environment. It is not written to jobs, records or logs, and it is not among the
credentials passed to the sandbox.

### Looking up earlier claims

With `claims_repository` on (the default for a Scientific Account job, opt-in otherwise) and
`REVEAL_CLAIMS_API_URL` set, the agent gets `query_claims`: read-only search of the repository by text, by what
refers to a DAPPER id, or by id. The sandbox may reach that one host and holds no key. Lookups count against
`tool_calls`. What they return is an earlier claim, not evidence, and cannot be cited in an account. If the
deployment has no repository, the tool is not offered.

## Watch it and read the result

Progress arrives on `GET /v1/jobs/{id}/events` like any other job: the agent's text (`agent_message`
activity), every tool call and result, and stage changes (`preparing_evidence` → `starting_agent` →
`running_agent` → `collecting_output` → `validating` → `persisting` → `complete`). Cancel with
`POST /v1/jobs/{id}/cancel`.

A succeeded job's `result` is an `AgentResult`:

```json
{"kind": "agent", "response": "…the final message…", "response_truncated": false,
 "response_sha256": "…", "turns": 9, "cost_usd": 0.41, "duration_seconds": 182.4,
 "files": [{"path": "questions.md", "size_bytes": 1874, "sha256": "…"}]}
```

`response` is cut at 50,000 characters (`response_truncated`); the full text and every file the agent wrote
to `output/` are kept with the job's captured workspace in S3. There is not yet an endpoint that downloads
those files. A Scientific Account job also returns an `account` block; see
[Return a Scientific Account](#return-a-scientific-account).

## Telemetry

Agent jobs appear in the admin console like any other job (kind `agent`). The **Execution** table shows
their state, Box phase timings, agent time and cost; the job detail shows the event log and the run's saved
diagnostics, including `runtime.json` and the response record, so a failed run's Claude Code error text is
readable without opening S3. The **Costs** tab counts their spend in its own daily series and shows the typical
cost of a successful agent job, separately from research runs.

## How it runs

The phases are the research workflow's: freeze the input, create a Box, bootstrap it from an immutable S3
bundle, launch the trusted runner, observe events, capture output and the tool ledger to S3, delete the Box.
Validation checks only that the capture is the run's own (job, attempt, input hash, model) and that the tool
ledger is complete; then the answer is saved as the job result. A Scientific Account job is also checked,
minted and deposited as described [above](#return-a-scientific-account). A failed run, or one stopped at its time, turn
or budget limit, fails the job with the runner's reason (`AGENT_EXECUTION_FAILED`, or `AUTHORING_BUDGET_EXCEEDED`
at the budget). Using up a tool or literature allowance does not.

Inside the Box the agent runs as the unprivileged `reveal-agent` user, in a read-only working directory with
the frozen input, and writes deliverables to `output/` (regular files, each under 8 MB, 40 MB in all). It
can use `/tmp` and its home directory for scratch.

**The agent never holds the Anthropic API key.** The root runner starts a loopback relay
(`box_agent.KeyProxy`, `127.0.0.1:8766`) and runs Claude Code with `ANTHROPIC_BASE_URL` pointing at it and a
placeholder key; the relay swaps in the real key and streams responses back. A Bash-capable agent on the open
network therefore cannot carry the key off the Box. It can still call the relay directly until the job's
deadline, so `limits.timeout_seconds` is the hard bound on spend there; Claude Code enforces
`max_budget_usd` for its own calls. Events, the response record and captured outputs are also scanned and
redacted for credentials, as for research.

The final answer is recorded by the root runner as `ledger/agent-response.json`, beside the tool ledger,
where the agent cannot write it.

## Test it against a real Box

`scripts/run_box_agent.py --kind agent` takes a `POST /v1/jobs` agent body, freezes it as the API would,
and runs it through a real Box with the adapter's own create/bootstrap/launch/poll/collect/delete loop:

```bash
.venv/bin/python scripts/run_box_agent.py --kind agent --input agent-body.json \
  --output .runtime/agent-smoke --env-file .env --budget 1
```

It needs `UPSTASH_BOX_API_KEY` and a working `ANTHROPIC_API_KEY`. `--budget` plays the server cap.

Measured on 2026-11-09 with a smoke prompt (Bash, one WebSearch, one Write to `output/`, `claude-opus-5-5`
at low effort): 4 turns, $0.11–0.13. Through the full workflow phases on the local namespace the job took
75 s: 33 s Box setup, 19 s agent, 4 s capture, 4 s cleanup. The agent saw only the placeholder key.

Code: [`box_agent.py`](../services/backend/src/reveal_backend/box_agent.py) (input, egress, relay, result),
the agent branches of [`box_remote.py`](../services/backend/src/reveal_backend/box_remote.py) and
[`workflow_execution.py`](../services/backend/src/reveal_backend/workflow_execution.py),
[`agent_limits.py`](../services/backend/src/reveal_backend/agent_limits.py) (operator defaults and ceilings), admission in
[`app.py`](../services/backend/src/reveal_backend/app.py) (`admit_agent_job`), and
[`tests/test_agent_jobs.py`](../services/backend/tests/test_agent_jobs.py).
