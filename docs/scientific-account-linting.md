# Agent account linter and pinned DAPPER startup

Each new research-agent start gets a **fresh clone of a locked DAPPER release**, the scientific-account skill, and the same lint script used by the backend validator. The current lock is [DAPPER 0.2.0-a1](https://github.com/broadinstitute/dapper/releases/tag/0.2.0-a1), commit `c0cfce549baded068aa9e82f81a1400025614b51`.

Implemented components:

- [Release lock](../services/backend/agent-runtime/dapper-release.json): repository, annotated tag object, immutable commit, schema/code checksums and approved evidence-input snapshots.
- [Startup helper](../scripts/start_research_agent.py): clones and verifies the release, bundles the skill/scripts and frozen evidence, then launches the requested agent command.
- [Agent linter](../scripts/lint_scientific_account.py): JSON findings and a meaningful exit status for one ScientificAccount document.
- [Shared validator](../services/backend/src/reveal_backend/scientific_account_lint.py): invokes the same checks in a fresh Python interpreter; `validate_scientific_account` enforces final mode for backend callers.

The existing EC2/Box worker is not yet deployed. This helper is the required startup entry point for that worker and can be exercised locally now. It does not provision an Upstash Box, configure MCP access, or supply an Anthropic key.

## Startup

Install the backend's `evidence` extra in the host/Box image. Git, Python and the chosen Claude Code harness must be available. Provider credentials and MCP/tool permissions remain worker configuration.

```bash
python -m pip install -e 'services/backend[evidence]'

# Exercise bootstrap without an agent/API call.
python scripts/start_research_agent.py \
  --workspace /tmp/reveal-agent-attempt-1 \
  --evidence-package data/evidence-captures/cad-builder-v1/package/evidence-package.json \
  --prepare-only

# A real launch uses another fresh workspace and the configured harness command.
python scripts/start_research_agent.py \
  --workspace /tmp/reveal-agent-attempt-2 \
  --evidence-package path/to/evidence-package.json \
  -- claude -p 'Use the construct-scientific-account skill with the supplied evidence package. Save account documents and lint reports under output/.'
```

The command starts only after the clone's commit, tag and required source checksums verify and the source artifacts have been copied and checksummed. Existing workspaces are rejected. Retries that launch a new agent use a new workspace and clone; they never silently reuse a floating checkout. A download or validation failure stops startup.

```text
attempt/
  dapper/                 Verified upstream release, schema, identity code and linter
  runtime.json            Release/lock hash, dependency versions and copied-file hashes
  reveal/                 Agent working directory
    .claude/skills/construct-scientific-account/SKILL.md
    scripts/lint_scientific_account.py
    services/backend/...  Shared linter implementation and release lock
    docs/...              Scientific authoring and lint contracts
    input/evidence-package.json
    input/sources/...     Exact captured evidence artifacts
    output/...            Agent-authored documents and lint reports
```

The launcher sets `REVEAL_DAPPER_ROOT`, `REVEAL_EVIDENCE_PACKAGE` and the Python environment used by the lint command. `prepare-only` prints these locations; it does not modify the caller's shell environment. For direct use afterward, pass `--dapper-root ../dapper` and `--evidence-package input/evidence-package.json` from `attempt/reveal/`.

The trusted runtime bundle supplies the instructions for this new agent start. When an older evidence package contains earlier instruction captures, they remain unchanged as historical artifacts. `runtime.json` explicitly records the old and new instruction hashes in `authoring_instructions.updates_from_package`; record this manifest alongside the input hash in the job's provenance. The agent uses the installed skill and current bundled contracts, not historical instructions recovered from source artifacts.

In Box, the worker must mount the release, lock, lint code, skill and input artifacts read-only and expose a separate writable output directory. File verification is not an operating-system permission boundary. The local helper itself does not enforce a read-only mount. The backend uses its own trusted checkout, lock and evidence package when revalidating.

### Existing snapshot compatibility

The evidence-package collector keeps its historical v8 snapshot pin and its replay hashes. The release lock explicitly approves that input snapshot: the release changes only version metadata in its root and claims schemas; the identity and scientific-account validation modules match. The new linter checks existing object identities under the release, never remints saved inputs. An unknown input snapshot fails until compatibility is reviewed and the worker-owned lock is updated. Release upgrades are explicit configuration changes, never `latest` or branch-head lookups.

## Agent feedback loop

One file must contain **exactly one ScientificAccount plus its complete dependencies and provenance**. Linting an account ID without its referenced objects cannot work offline.

```bash
python scripts/lint_scientific_account.py output/account.yaml \
  --mode draft --output output/account.lint.json
```

The linter calls upstream `lint_provenance.lint(..., profile_name='scientific-account')` with DAPPER's **closed-schema validator**. It does not copy the upstream validation rules. DAPPER checks shapes, references, digest identities, provenance, account membership and conclusions, proposition/evidence agreement and source-claim cycles.

REVEAL additionally checks:

- The account references the exact selected DisMech KnowledgeGap and includes that frozen object.
- Referenced trusted objects retain their supplied payloads.
- `closing_remarks` is nonempty.
- Every account component Claim has explicit evidence lineage to an unchanged captured CFDE File. Both legacy API captures and generation-bound SQL reference captures qualify. SQL captures must match their recorded checksum, model, generation and scientific source tables; derived captures must resolve to captured reference observations. Instructions, DisMech inputs and unrelated database records do not qualify. A shared Activity's inputs are insufficient. Auxiliary source Claims may use other KGs.
- Evidence uses have the owning proposition as their target, a recorded direction and nonempty interpretation/context.
- Source artifacts are checksum-verified. New Files must match a completed trusted tool capture by checksum and size. The agent tool snapshots completed captures from the live ledger; command-line callers supplying external evidence pass `--ledger` with its trusted manifest.
- JSON row locators resolve in the cited source, snippets quote that exact observation, and ClaimScore metrics, values, and mathematical kinds match the cited row. These checks run in both draft and final modes through `source_validation.py`. A sentence-ending period is tolerated after a pointer; an unresolved pointer is an error and never falls back to searching the whole response.
- Final scientific nodes have DAPPER digest IDs; draft authored nodes may still use temporary IDs.

The script does not assign IDs, rewrite input files, fabricate evidence, publish accounts or access the network. Newly authored objects must be minted during trusted assembly before final mode. Upstream warnings are preserved and do not fail by default; `--strict` fails on warnings too. This keeps the known imported catalog Activity warning visible.

Trusted assembly hydrates missing frozen objects only through exact schema-declared relationships and edge endpoints. Mentioning an input ID in prose, a label, or another literal does not insert that object into the graph. Assembly derives these reference fields from the same pinned DAPPER schema as lint, then revalidates the assembled document; it does not suppress reachability errors for genuinely disconnected nodes.

Exit codes: **0** passes the requested lint mode, **1** contains validation errors (or strict-mode warnings), **2** means a runtime/setup failure. The JSON report records the mode, exact document/package hashes, DAPPER release, findings, error/warning counts and remaining acceptance checks. `profile-only` is an explicit diagnostic mode for DAPPER examples and is never accepted by the backend validator.

## Backend validator built on the linter

```python
from reveal_backend.scientific_account_lint import validate_scientific_account

report = validate_scientific_account(
    'output/account.yaml',
    dapper_root='/worker/verified-dapper',
    release_lock='services/backend/agent-runtime/dapper-release.json',
    evidence_package='/worker/frozen-input/evidence-package.json',
)
```

This reruns the same linter in **final** mode and raises `AccountValidationError` with `.report` on failure. The fresh interpreter avoids accidentally importing the collector's older DAPPER snapshot. The backend must run this against returned document bytes, never trust a report supplied by the agent, and persist/compare the report's hashes with the artifacts it accepts.

Passing this validator establishes structure and source fidelity. Draft and final modes use the same checks; final mode additionally requires minted scientific identities after trusted assembly. Trusted attribution/job ownership and execution-ledger/tool-policy enforcement remain worker responsibilities. Once those deterministic checks pass, the worker saves the account. No second AI review or scientific verdict is required. A correct source quotation does not by itself prove that an interpretation is scientifically correct. Failed worker lint reports are retained alongside the output so their findings remain inspectable.

The durable workflow preserves `attempt-N/failure.json` and `attempt-N/validation-M.json` before releasing its temporary workspace. These reports identify the failing phase and lint findings through the existing admin diagnostics view. Repairable query input errors are recorded as failed tool calls; actual authorization or tool-policy violations remain fatal at the execution-ledger gate. Old output retained after `REVIEW_UNAVAILABLE` or `REVIEW_BUDGET_EXCEEDED` can be revalidated and saved through **Save existing output**, without rerunning research or the retired AI review.
