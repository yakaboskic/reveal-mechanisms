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
 "limits": {"timeout_seconds": 600, "max_turns": 30, "max_budget_usd": 1}}
EOF
```

| Field | Default | Notes |
|---|---|---|
| `prompt` | required | Up to 100,000 characters; the agent's first message. |
| `system_prompt` | none | Appended to Claude Code's own system prompt, after a short note describing the sandbox. |
| `tools` | Read, Write, Edit, Glob, Grep, Bash, WebSearch (a Scientific Account job adds WebFetch) | Claude Code built-ins. `WebFetch` needs `network: "open"`. |
| `graphs` | `[]` | OKN knowledge graphs for the scoped graph tools (`get_schema`, `describe_kg`, `query_graph`). |
| `network` | `restricted` (`open` for a Scientific Account job) | `restricted`: only the Anthropic API plus the services of the chosen graph tools and `query_claims`, and `allowed_domains`. `open`: the internet (package installs, downloads, `WebFetch`). |
| `allowed_domains` | `[]` | Up to 20 exact hostnames the restricted network may also reach, for example `eutils.ncbi.nlm.nih.gov` (PubMed E-utilities) or `api.platform.opentargets.org` (Open Targets GraphQL). No wildcards or IP addresses; not used with `open`. The agent calls them with Bash (`curl`, `python3`). |
| `claims_repository` | `false` (`true` for a Scientific Account job) | Offer the read-only `query_claims` tool. Needs a claims repository configured on the deployment; see [Return a Scientific Account](#return-a-scientific-account). |
| `deliverable`, `question`, `claims` | `answer` | See [Return a Scientific Account](#return-a-scientific-account). |
| `effort` | `medium` | `low`, `medium` or `high`. |
| `model` | `REVEAL_AGENT_JOB_MODEL` | A Claude model id. |
| `limits` | see [Limits](#limits) | Run time, turns, budget and the REVEAL tool-call allowance. |

To let an agent search PubMed and query a GraphQL API without opening the whole network, give it Bash and
those two hosts:

```json
{"kind": "agent",
 "prompt": "Use NCBI E-utilities to find the 3 most recent PubMed articles on LEPR variants and insulin resistance, then query the Open Targets GraphQL API (https://api.platform.opentargets.org/api/v4/graphql) for LEPR's top associated diseases. Save output/summary.md citing PMIDs.",
 "tools": ["Read", "Write", "Bash"],
 "allowed_domains": ["eutils.ncbi.nlm.nih.gov", "api.platform.opentargets.org"]}
```

The request is frozen into the job when it is queued (`reveal.agent-input/4`), with every default filled
in, so the stored input says exactly what ran. A request that sends `literature` or a `literature_*` limit is refused
with `422`: agents fetch papers themselves with Bash.

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
| `tool_calls` | 30 | 300 | 1 to 5,000 | Graph tool and `query_claims` calls, counted together. |

The run stops at the first of `timeout_seconds`, `max_turns` and `max_budget_usd`, and the job fails with the
runner's reason. `tool_calls` never fails a job: when it is used up, the tool call returns an error that says
so, and the agent can still finish and write its answer. Each graph and `query_claims` result also carries a
`run_budget` with what has been used and what is allowed, so the agent can pace itself.

### Changing the defaults and ceilings

An operator sets each limit's default and ceiling in the admin console, under **Limits**
([admin telemetry](admin-telemetry.md#agent-job-limits)). A saved change applies to jobs queued after it, on
every API replica, with no deploy. The value in force is the first of:

1. the admin console;
2. the environment: `REVEAL_AGENT_JOB_DEFAULT_<NAME>` and `REVEAL_AGENT_JOB_MAX_<NAME>`, where `<NAME>` is the
   key in upper case without a leading `max_` (`REVEAL_AGENT_JOB_MAX_BUDGET_USD`, `REVEAL_AGENT_JOB_DEFAULT_TURNS`,
   `REVEAL_AGENT_JOB_MAX_TOOL_CALLS`);
3. the built-in value in the table.

A value outside the supported range is ignored, and the console shows that. This includes the environment: `REVEAL_AGENT_JOB_MAX_BUDGET_USD=0` or an unparseable value no longer refuses every agent job, it falls back to the built-in 5. Use `REVEAL_AGENT_JOB_USERS` to turn agent jobs off. A ceiling set below the default
lowers the default to it. The supported range is fixed in code (`box_agent.LIMITS`): the Box adapter bounds a
run at one hour, and the in-Box runner validates the same ranges.

Lowering the budget ceiling also reaches jobs that are already queued: their frozen budget is cut to the
ceiling when the run is dispatched. The other limits stay as frozen.

These limits apply to general agent jobs only. Research runs keep their fixed allowances.

**Deploy the API and the workflow workers together.** A worker runs only `reveal.agent-input/4`; a job still
queued in an earlier format fails at prepare.

Not adjustable, because they bound a single response or the sandbox, not how much work a run may do: the
100,000-character prompt, the 50,000-character answer kept on the job row, and the output-file sizes below.

## Return a Scientific Account

Set `deliverable` to `scientific_account` and give a `question`. The agent fetches the papers it needs with its
own commands, writes one DAPPER ScientificAccount that addresses the question, and REVEAL checks it and
deposits it in the claims repository.

```json
{"kind": "agent", "deliverable": "scientific_account",
 "prompt": "Prefer primary studies in mice and humans. Say what you did not read.",
 "question": "Does loss of leptin receptor signalling in hepatocytes cause hepatic insulin resistance?",
 "claims": ["Hepatocyte-specific deletion of LEPR reduces insulin-stimulated AKT phosphorylation in liver."],
 "limits": {"max_budget_usd": 5}}
```

| Field | Notes |
|---|---|
| `question` | Required, up to 2,000 characters. It becomes the account's question, recorded as supplied by the requester (it is not a curated knowledge gap). |
| `claims` | Optional, up to 20 claims. Each is a statement of up to 1,000 characters, or an object that adds the statement's structured form (see [proposition triples](#proposition-triples)). Each becomes a fixed Proposition. The agent assesses it from what it reads, for or against, and cannot reword it. Statements must be distinct. |
| `attribute_requester` | Optional, default `false`. Set it to `true` to be named in the account as the `Person` it is attributed to. See [who the account names](#who-the-account-names). |
| `prompt` | Still required: how you want the work done. |

### Proposition triples

A DAPPER Proposition can say the same thing twice: in words (`statement`) and as `subject_entity`, `relation` and
`object_entity`, which is the form others can search and join on. REVEAL asks for the triple wherever it can be given.

**From you.** Send a claim as an object and its Proposition is stored with the triple, exactly as given:

```json
"claims": [{"statement": "LITAF is associated with inflammatory bowel disease.",
            "subject_entity": "HGNC.SYMBOL:LITAF",
            "relation": "biolink:associated_with",
            "object_entity": "KPN.TRAIT:0000505"}]
```

Make the triple say what the statement says: the agent assesses the statement, and its verdict is stored against both.

The three go together: all or none. Each is a CURIE or an absolute URI of at most 300 characters. A CURIE's prefix
must be one the pinned DAPPER release declares (`MONDO`, `HGNC`, `KPN.TRAIT`, `obo`, `CL`, ...) or `HGNC.SYMBOL` or
`biolink`; a request with any other prefix is refused with `422`, and the message lists the prefixes. This is the
reliable way to get a triple, because you know which identifiers your own system uses. The triple is part of what
the Proposition's id addresses: the same statement with and without it is two Propositions.

**From the agent.** For a Proposition it writes itself, and for a claim you sent as a plain statement, the agent is
told to add the triple when it can name both ends with identifiers it was given: in the request, in your prompt, in
a claims-repository record, or printed in a paper it read. Human genes are `HGNC.SYMBOL:<symbol>` and relations are
biolink predicates. It is told not to recall an ontology number from memory, so a Proposition whose ends it cannot
identify stays text. Its `lint_account` tool lists each Proposition without a triple as a `proposition-triple`
advisory. That is a reminder and never fails the account: a text-only Proposition is valid DAPPER.

When the agent adds a triple to a claim you sent as a plain statement, it writes a Proposition with your exact
statement plus the triple and makes its Claim about that one. `requested_claims` still reports your claim as assessed.

Triples are stored as you gave them: CURIEs stay CURIEs (`HGNC.SYMBOL:LITAF`), and the deposit does not carry a prefix
map. The claims repository's own PIGEAN Propositions name a gene by bare symbol (`LITAF`) and use the predicate
`associated_with`, so an account joins to them by the trait id today, not by gene or predicate.

`scripts/agent_job.py --claims-file claims.json` sends a JSON list of such claims.

What is different about such a job:

- **It has Bash and the open network by default.** It takes the same `tools`, `network`, `allowed_domains` and
  `graphs` as an answer job. Its tools default to Read, Write, Edit, Glob, Grep, Bash, WebSearch and WebFetch, and
  its network to `open`. On a network you restrict, the default leaves out WebFetch, and GitHub stays reachable
  for the pinned DAPPER release the account tools lint against. The agent fetches each paper with a command and
  saves it under `output/sources/`; the routine is in its skill
  ([`construct-literature-account`](../services/backend/agent-skills/construct-literature-account/SKILL.md)).
- **The Box has the fetch and convert tools.** Its toolchain installs `curl`, `pdftotext` (poppler-utils) and
  `xmllint` (libxml2-utils) with the rest. They are not yet pinned to exact versions; each account job records the
  installed versions in its `runtime.json` (`source_tools`: each package's version and each program's version line),
  so its Activities can be explained and rerun with the same tools.
- **It gets the time and turn ceilings by default**, because a run stopped at a limit returns no account. Ask for
  less in `limits` if you want.
- **The agent keeps a lab notebook, and REVEAL checks it.** For each paper the agent:
  1. fetches it with a Bash command and saves the bytes under `output/sources/`, for example
     `curl -fsSL -o output/sources/PMC3491803.xml https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML`;
  2. converts it to text with another command if needed, for example
     `pdftotext -layout output/sources/x.pdf output/sources/x.txt`;
  3. records each command as a DAPPER `Activity` (`command`, `software_name`, `software_version`,
     `generated_at_time`) and each saved file as a `File` (`filename` is its path under `output/`, `location` the URL
     it was fetched from), linked by `was_generated_by_edges` (File to Activity) and `was_derived_from_edges`
     (converted File to the File it came from);
  4. quotes the saved file in an `EvidenceItem`: the verbatim `snippet`, `was_derived_from` the File, and optionally
     the lines it is on in `context`, written `line=212-213` (what `grep -n` prints).

  WebSearch and WebFetch are for finding papers. Only text saved under `output/sources/` by a recorded command can be
  quoted. The agent never writes a checksum, a size or a byte offset.
- **REVEAL checks the record, offline and deterministically** (`source_evidence.py`), while the agent drafts
  (`lint_account`) and again after the run:
  - every File is a regular file under `output/sources/`, and REVEAL measures its SHA-256, size and media type;
  - every File is generated by an Activity with a command and a time. That command is part of a Bash call the tool
    ledger shows succeeded, and it names the file. A fetched file's command fetches its `location`. A converted file
    derives from a saved file through an Activity of its own, whose command names both files. The recorded command
    must equal one whole command of the call (split on `&&`, `||`, `;`, `|` and new lines outside quotes), so a
    command that was only echoed does not count. No later command may write the file: anything but a short list of
    reading programs (`grep`, `head`, `tail`, `wc`, `cat`, `sed` without `-i`, `xmllint`, `pdftotext … -`) or the
    recorded conversion that names it afterwards makes the record stale. A file written by hand, by the Write tool,
    or by a command that did not run is refused;
  - every quote occurs exactly once in the File it cites, or exactly once on the lines `context` names, within one
    paragraph. It is 20 to 400 characters. An account may quote at most 4,000 characters of one paper (its original
    and converted files together), and at most 40% of a short text such as an abstract (one quote is always
    allowed), so a deposited account never carries a paper;
  - each refusal says what to change, for example "the quote is not on lines 212-213 of sources/x.txt; it occurs on
    lines 240-241".

  The Bash call REVEAL matched is recorded on the Activity as `observed_command`. Nothing is guessed.

After the run REVEAL applies the same gate a research account passes (pinned DAPPER release, source checks), records
what ran, and puts the account in its deposit form, minted with DAPPER 0.2.1-a1:

- each quote becomes a `ValueLocator`: the exact byte span of the quote in the saved file, the bytes there as text,
  and the line selector;
- each saved file is a `File` with REVEAL's SHA-256, size and media type and the URL it came from, and each command an
  `Activity` with the command the agent recorded and the one REVEAL saw run. The bytes are not deposited: REVEAL
  keeps them for you. Anyone can rerun the commands and check the checksums and the spans;
- each paper whose URL names it (a PMC id, a PubMed URL, a DOI) is a `Publication` with those identifiers.

The account is the file `output/account-1.json`; the draft and lint tools accept no other name. An account that fails this gate fails the job with `VALIDATION_FAILED`. A job that ends without a saved account
fails with `AGENT_EXECUTION_FAILED`.

### The result

The job result gains an `account` block, and `response` is the agent's short report.

```json
"account": {"account_id": "dapper:ScientificAccount.…", "dapper_version": "0.2.1-a1",
  "document_sha256": "…", "document_bytes": 9120, "objects": 11, "edges": 4, "claims": 1, "value_locators": 1,
  "lint_warnings": 1,
  "requested_claims": [{"statement": "Hepatocyte-specific deletion of LEPR …", "assessed": true, "claim_ids": ["dapper:Claim.…"]}],
  "papers": [{"path": "sources/PMC3491803.xml", "url": "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML",
              "sha256": "…", "size_bytes": 182311, "media_type": "application/xml",
              "command": "curl -fsSL -o output/sources/PMC3491803.xml https://…/PMC3491803/fullTextXML",
              "observed_command": "mkdir -p output/sources && curl -fsSL -o output/sources/PMC3491803.xml https://…/PMC3491803/fullTextXML",
              "derived_from": null, "evidence_items": 1, "file_id": "dapper:File.…"}],
  "deposit": {"status": "deposited", "deposit_id": "dep_…", "objects": 11, "stored_differs": 0, "unresolved_references": 0}}
```

- `GET /v1/jobs/{id}/account` returns the DAPPER document (owner only), with `source` saying where it was read from;
  see [Reading the account back](#reading-the-account-back).
- `papers` lists every file the account's record names: its path under `output/`, the URL its bytes came from (for a
  converted file, the original's), REVEAL's checksum and size, the recorded and observed commands, the file it was
  converted from, and how many EvidenceItems quote it.
- `GET /v1/jobs/{id}/account/sources/{sha256}` downloads one of those files (owner only), exactly as saved: the bytes a
  ValueLocator's `byte_start` and `byte_length` address.
- A requested claim the agent made no Claim on has `assessed: false`. It is reported, not added to the account.

`scripts/agent_job.py --question … --claim … --account-out account.json` does all of this from a terminal.

#### Check that the sources reproduce

`scripts/audit_account_sources.py` reruns each recorded fetch and conversion command in a scratch directory on the
machine you run it on, fetches before the conversions that read them, and compares SHA-256s with the job result's
`papers`. It is not part of the deposit: an account is deposited whatever the audit later finds.

```bash
# From the owner route, with REVEAL_API_URL and REVEAL_API_KEY (or --handoff key.json):
python3 scripts/audit_account_sources.py --job <job-id>
# From an exported job result (GET /v1/jobs/{id}, GET /v1/jobs/{id}/account, or the account block):
python3 scripts/audit_account_sources.py --result job.json --json
```

Each source is `reproduced`; `changed_upstream` (the publisher's copy changed: not a failure, REVEAL keeps the
original bytes); `converter_differs` (a conversion read the same bytes and wrote others: the local `pdftotext` or
`xmllint` is not the Box's version, compare with `source_tools` in the run's `runtime.json`; not a failure); `failed`
(with the command's exit status); or `refused`. With `--job`, a conversion of a file that changed upstream is run on
the original bytes, downloaded from `/v1/jobs/{id}/account/sources/{sha256}` (`--originals DIR` supplies them from a
folder of files named by checksum). The exit status is 1 when a source failed or was refused.

It runs only commands the deposit screen accepts (one plain `curl`, `wget`, `pdftotext` or `xmllint` call writing under
`output/`, that program as the first word, only the options listed for it, no local file outside `output/` and no
URL that is not http(s): `deposit_screen.command_refusal`, the same rule that withholds an account). Anything else is
refused. Commands run without a shell, with their `output/` paths and redirects moved into the
scratch directory, and with the scratch directory as `HOME`. They do reach the network. The audit in a fresh Box, and
a console button for it, come later.

`scripts/scientific_account_job.sh <key.json> <run-directory>` is a worked example that prints every HTTP call: it takes
candidate genes from the claims repository's validation-challenge query, submits the job with one claim per gene,
reads the account back through REVEAL (failing unless its `source` is `claims_repository`), and reads the deposit
from the repository directly. Each successful run is a permanent deposit; reusing a run directory resumes
the same job.

### Deposit

When the job succeeds REVEAL sends the whole document to the claims repository in one request
(`POST /v1/deposits`). The repository stores every object under its DAPPER id and records the deposit against
REVEAL's key. An object it already holds is left as stored.

| `deposit.status` | Meaning |
|---|---|
| `deposited` | Stored, every object as sent. `deposit_id` is the repository's receipt. Accounts are deposited in no collection; a deposit that named one would carry `collection` and `release`. |
| `not_configured` | This deployment has no claims repository. The account is still returned. |
| `failed` | The attempt did not succeed (`reason`: `unavailable`, `rejected`, `unauthorized`, `too_large`). The job still succeeds and keeps its account. There is no automatic retry yet. A redirect from the repository is refused, not followed: the key is sent to the configured address only. |
| `failed`, `reason: inconsistent` | The repository took the deposit, but its receipt shows an object it already held with different unhashed content (`stored_differs`; its copy stands) or a reference left unresolved (`unresolved_references`). It does not hold the account as sent, so REVEAL keeps the `deposit_id` and counts, records the reason in job telemetry, never sends it again, and serves the account from the job. |
| `withheld_unsafe` | REVEAL's prompt-injection screen flagged the account, or text the run read, so it was never sent. The job still succeeds and keeps its account. `reason` names the check (`unsafe_text`, `unsafe_command`, `unrequested_claim`, `unknown_entity`, `oversized_field`, `screen_too_large`, `prompt_injection`) and `detail` says why and where in one line. There is no review or retry. |

A deposit cannot be recalled. A cancellation that arrives before the deposit is sent stops it.

Before sending, REVEAL screens the account for prompt injection. The screen looks for attacks, not scientific quality.
Fixed checks require that the account's prose carries no links, code, markup, hidden characters or text addressed to
a model; that each recorded command is a plain `curl`, `wget`, `pdftotext` or `xmllint` call writing only under
`output/`, using only the options listed for its program (`deposit_screen.OPTIONS`: fetching with GET, converting,
choosing the output) and naming no local file outside `output/` and no URL that is not http(s); that its claims are about the claims you asked about (or, if you named none, its own, at most 20); that
each triple term comes from your request, an allowed namespace or a cited paper; and that every field is within its
size limit. A classifier, Llama Prompt Guard 2 22M, will also score the saved papers, the other tool results and the
account's text, but it is off until an operator pins its model in
`services/backend/agent-runtime/prompt-guard-release.json`; until then only the fixed checks run, and the screen's
verdict in job metrics records the classifier as `disabled`. Keep links and markup out of `context`,
`closing_remarks` and the other prose fields.

### Reading the account back

REVEAL keeps no copy of a deposited account. `GET /v1/jobs/{id}/account` rebuilds it from the claims repository's
public read routes, sending no key:

1. `GET /v1/deposits/{deposit_id}`: the ids and classes of every object the deposit carried, and its edges in the
   order they were sent;
2. `POST /v1/objects:batchGet`, 500 ids at a time: the objects;
3. the document is laid out as REVEAL sent it (each object group ordered by id, edges in their order) and serialized
   in the same canonical JSON.

The rebuilt document must hold the job's `account_id` and hash to its `document_sha256`. Then the response says
`"source": "claims_repository"`. If the repository cannot be reached, or answers with anything else (a missing or
changed object), the route returns 502 (`CLAIMS_REPOSITORY_UNAVAILABLE` or `CLAIMS_REPOSITORY_MISMATCH`) and records
it in the job's telemetry; it never falls back to a local copy for a deposited account. The API keeps a rebuilt
deposit in memory for a few minutes (`REVEAL_CLAIMS_READ_CACHE_SECONDS`), and checks it against the job on every read.

When the repository holds nothing for the job (`withheld_unsafe`, `failed`, including `inconsistent`, or
`not_configured`), REVEAL keeps the document's bytes with the job, beside the saved sources, and the route serves
that copy, checked against the same `document_sha256`, as `"source": "job_artifact"`; `account.deposit.status` says
why. The requester always gets their account.

Deposited accounts are readable by anyone who can reach the claims repository: its object, deposit and search routes
have no read check.

### Who the account names

By default the account does not name you. DAPPER requires every Claim to be attributed to an agent, so the account
and its Claims are attributed (`was_attributed_to`) to one `Organization`, "REVEAL Mechanisms agent service", which
is the same object in every such account. Its `Activity` says that REVEAL built it, when, and from which saved
files; the repository records which deposit key sent it. Nothing in the document or the deposit is derived from your
user record.

With `"attribute_requester": true` the account and each of its Claims are attributed (`was_attributed_to`) to a
`Person` for you: the display name of a registered user, the ORCID iD when it was authenticated, and a short
REVEAL actor code that is the same in every account you ask for. An unregistered requester appears as "Anonymous
researcher" with that code. The Person is deposited with the account, and a deposit cannot be recalled.

Either way the document does not include paper text beyond the quoted passages, REVEAL's internal capture files, or
the prompt.

Operators turn deposits on with two settings on the **workers**, and set the address on the **API** too, which reads
deposited accounts back:

| Setting | Value |
|---|---|
| `REVEAL_CLAIMS_API_URL` | Base address of the claims repository, `https://…` (plain `http` only for localhost). Workers and API. |
| `REVEAL_CLAIMS_API_KEY` | A deposit key minted in the claims repository's admin console (Keys). It names REVEAL as the depositor. Workers only. |
| `REVEAL_CLAIMS_READ_CACHE_SECONDS` | How long the API keeps a deposit it read back (default 300; 0 turns the cache off). API. |

The key stays in the worker's environment. It is not written to jobs, records or logs, and it is not among the
credentials passed to the sandbox.

### Looking up earlier claims

With `claims_repository` on (the default for a Scientific Account job, opt-in otherwise) and
`REVEAL_CLAIMS_API_URL` set, the agent gets `query_claims`: read-only search of the repository by text, by what
refers to a DAPPER id, or by id. The sandbox may reach that one host and holds no key. Lookups count against
`tool_calls`. What they return is an earlier claim, not evidence, and cannot be cited in an account. If the
deployment has no repository, the tool is not offered.

## Return structured output

An answer job can ask for its result as a JSON object of a shape you define. Send `output_schema`, a JSON
Schema, with an answer job:

```json
{"kind": "agent", "prompt": "Grade the evidence that each gene has a role in IBD: NKX2-3, JARID2, EMSY.",
 "network": "open",
 "output_schema": {"type": "object", "required": ["genes"], "additionalProperties": false,
   "properties": {"genes": {"type": "array", "items": {"type": "object", "required": ["symbol", "evidence"],
     "properties": {"symbol": {"type": "string"}, "evidence": {"enum": ["strong", "weak", "none"]},
                    "pmids": {"type": "array", "items": {"type": "string"}}}}}}}}
```

- **What the schema must be.** Its top level is `"type": "object"`, it uses only local references
  (`"$ref": "#/..."`), and it is at most 32,000 bytes. It is checked as Draft 2020-12 unless it names another
  draft with `$schema`. An invalid schema is refused with 422 and says where it is wrong. It is for
  `deliverable` `answer` only.
- **How it runs.** The runner passes the schema to Claude Code as `--json-schema`, the mechanism Upstash Box's
  structured output (`response_schema`) uses. REVEAL keeps running Claude Code itself, so the job keeps its
  tools, tool ledger, limits and key proxy. Claude Code makes the final answer an object that satisfies the
  schema and retries when it does not.
- **What comes back.** `result.structured_output` holds the object, beside `response` (the final message). The
  backend checks it against the frozen schema again before storing it. It is at most 100,000 bytes; have the
  agent write anything larger to `output/`.
- **When it fails.** A run that returns no object, or one that does not satisfy the schema, fails with
  `VALIDATION_FAILED`. A run in which Claude Code gave up after its retries fails with
  `AGENT_EXECUTION_FAILED` and says so.
- **The input format.** `output_schema` is an optional key of the one input format, `reveal.agent-input/4`.

`scripts/agent_job.py --output-schema schema.json --structured-out result.json` sends a schema from a file and
saves the object.

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
[Return a Scientific Account](#return-a-scientific-account). A job that sent `output_schema` also returns `structured_output`; see
[Return structured output](#return-structured-output).

## Telemetry

Agent jobs appear in the admin console like any other job (kind `agent`). The **Execution** table shows
their state, Box phase timings, agent time and cost; the job detail shows the event log and the run's saved
diagnostics, including `runtime.json` and the response record, so a failed run's Claude Code error text is
readable without opening S3. The **Costs** tab counts their spend in its own daily series and shows the typical
cost of a successful agent job, separately from research runs.

An account job's detail also lists each source file it saved: the path, URL, recorded command, the Bash call REVEAL
saw run, size, short SHA-256, media type and how many quotes cite it, with the deposit screen's verdict (check,
score against threshold, where, and how many windows and texts it scored). These are metadata the agent wrote and
REVEAL measured, never a file's text; credentials in a URL or command are redacted. The job list carries none of it.

## How it runs

The phases are the research workflow's: freeze the input, create a Box, bootstrap it from an immutable S3
bundle, launch the trusted runner, observe events, capture output and the tool ledger to S3, delete the Box.
Validation checks only that the capture is the run's own (job, attempt, input hash, model) and that the tool
ledger is complete; then the answer is saved as the job result. A Scientific Account job is also checked,
minted and deposited as described [above](#return-a-scientific-account). A failed run, or one stopped at its time, turn
or budget limit, fails the job with the runner's reason (`AGENT_EXECUTION_FAILED`, or `AUTHORING_BUDGET_EXCEEDED`
at the budget). Using up the tool-call allowance does not.

Inside the Box the agent runs as the unprivileged `reveal-agent` user, in a read-only working directory with
the frozen input, and writes deliverables to `output/` (regular files, each under 8 MB, or 25 MB under
`output/sources/`, and 100 MB in all). It
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
