# Research-agent output and worker acceptance boundary

**v12 transport specification.** The research skill and linter exist; the queue/Box result collector and trusted assembly pipeline do not yet exist. The input is the frozen [evidence-package schema](../schema/README.md). The scientific output remains DAPPER, using [account construction](scientific-account-construction.md) and [result templates](pigean-claim-model.md).

## Files and responsibility

1. Worker creates an attempt/workspace tied to one immutable request, selected gap, evidence-package hash and verified `runtime.json`.
2. Agent writes `output/account-1.yaml` (or JSON), optionally up to three documents. **Each file contains exactly one ScientificAccount and all referenced dependencies/provenance**. The linter uses that file directly. Shared DAPPER nodes can occur in several documents with identical payloads.
3. Agent invokes the installed linter in draft mode and repairs representation errors. Temporary authored IDs may remain until trusted minting; trusted source objects must remain unchanged. An agent lint report is feedback, not an acceptance certificate.
4. Worker records all output bytes before assembly. It verifies allowed paths/size, selected gap and trusted input bindings, inserts actual actor/runtime records, resolves temporary IDs and mints through the locked DAPPER module.
5. Backend reruns the shared validator in **final** mode against its own trusted release/package, followed by exact-source, selected-KG ledger, ownership/attribution and scientific-grounding checks. Store reports/hashes. Invalid outputs remain attempt artifacts, not API account results.
6. Each accepted account commits with scientific payloads/documents/edges, claims/evidence, permissions, registry first-mint metadata and an automatic Paragraph outbox entry. Application events expose accepted IDs only after that transaction.

The worker-owned manifest follows [agent-output.schema.json](../schema/agent-output.schema.json). The agent cannot assert `accepted=true`, select its owner or supply trusted execution timestamps. Worker records `succeeded`, `insufficient_evidence`, `failed` or `cancelled` according to actual execution and checks. Raw account artifacts are not sufficient for success; a succeeded manifest lists final validated documents. An insufficient-evidence outcome has no accepted accounts and records a scope/coverage explanation. Exceptions, tool-policy violations and invalid output are failures, not biological conclusions.

Manifest example (paths/hashes illustrative):

```json
{
  "format": "reveal.agent-output/1",
  "job_id": "44444444-4444-4444-8444-444444444444",
  "attempt": 1,
  "status": "insufficient_evidence",
  "input_package_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "runtime_manifest_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "ledger_manifest_sha256": null,
  "accounts": [],
  "reason": "No usable observations remained within the retained scope."
}
```

The manifest is a proposed worker transport, not an additional scientific class and not yet produced by `start_research_agent.py`. Raw tool/failed-artifact inventory stays in the attempt ledger. The API publishes `Job`/`AnalysisResult`, not this internal manifest. Exactly one account document is passed to the existing validator at a time; no unsupported DAPPER multi-account envelope is passed to its terminal profile.

## Remaining acceptance implementation

The shared linter already checks DAPPER shapes/references/identity/provenance, exact selected gap/trusted objects, closing remarks, explicit EvidenceItems and CFDE-file ancestry of account Claims. It does **not** check that every quoted metric matches its cited row, that prose is scientifically supported, that external results were authorized, or that an agent-provided runtime attribution is genuine. These remain worker gates. See [lint limits](scientific-account-linting.md).

Append-only Proto-OKN ledger entries require attempt/call sequence, tool name, selected graph, sanitized public display, exact arguments and result artifacts, response status, timestamps, hashes, locators and source version when available. Empty/error calls must be included. Enforcement and complete-output capture require a real integration test before enabling the tools.

Paragraph output is separate: the agent supplies text segments with exact allowed citation target/revision pairs; the backend calls pinned `assemble_cited_text`, validates the immutable Paragraph and registry/span links, and stores a separate generation Activity. Create a paragraph-specific skill before implementing this worker. It may not invent a new proposition, fetch new evidence or cite an unavailable/latest-substituted metadata revision.
