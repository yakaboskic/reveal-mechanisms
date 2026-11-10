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
| `REVEAL_AGENT_JOB_MAX_BUDGET_USD` | `5` | Server cap per run. A request's `limits.max_budget_usd` can lower it, never raise it. |
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
| `network` | `restricted` | `restricted`: only the Anthropic API plus the services of the chosen graph/literature tools. `open`: the internet (package installs, downloads, `WebFetch`). |
| `effort` | `medium` | `low`, `medium` or `high`. |
| `model` | `REVEAL_AGENT_JOB_MODEL` | A Claude model id. |
| `limits` | 900 s, 50 turns, min(2, cap) USD | The run stops at whichever comes first. |

The request is frozen into the job when it is queued (`reveal.agent-input/1`), with every default filled
in, so the stored input says exactly what ran.

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
those files.

## How it runs

The phases are the research workflow's: freeze the input, create a Box, bootstrap it from an immutable S3
bundle, launch the trusted runner, observe events, capture output and the tool ledger to S3, delete the Box.
Validation checks only that the capture is the run's own (job, attempt, input hash, model) and that the tool
ledger is complete; then the answer is saved as the job result. A failed or limit-stopped run fails the job
with the runner's reason (`AGENT_EXECUTION_FAILED`, or `AUTHORING_BUDGET_EXCEEDED` at the budget).

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

Code: [`box_agent.py`](../services/backend/src/reveal_backend/box_agent.py) (input, egress, relay, result),
the agent branches of [`box_remote.py`](../services/backend/src/reveal_backend/box_remote.py) and
[`workflow_execution.py`](../services/backend/src/reveal_backend/workflow_execution.py), admission in
[`app.py`](../services/backend/src/reveal_backend/app.py) (`admit_agent_job`), and
[`tests/test_agent_jobs.py`](../services/backend/tests/test_agent_jobs.py).
