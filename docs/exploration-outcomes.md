# Saved explorations and literature search

An analysis that ends with `insufficient_evidence` produces a durable exploration
record. It records what this attempt investigated and why it could not support a
scientific account. It is not an accepted DAPPER ScientificAccount, a biological
null finding, or a failed execution. Scientific validation failures and timeouts
continue to be failures and cannot be relabeled as explorations.

## Reading and sharing

The completed job links to `/analyses/{outcome_id}`. The record includes the exact
selected gap, mechanism anchors and traits, original researcher attribution,
captured explanation, evidence limitations, proposed next steps when supplied,
source versions, and provenance hashes. Older captured outcome files retain
their original explanation without a new model call.

Records start private. The owner can explicitly publish or unpublish an
exploration from its detail page. Publishing freezes the displayed result,
attribution and captured source evidence; source evidence becomes downloadable.
The full evidence-package JSON, job activity, runtime and ledger contents,
request, and workspace drafts remain private. Public responses omit private job
IDs. Unpublishing revokes the snapshot; evidence independently published with
another result can remain accessible.

Selected knowledge gaps list published explorations in **Explored, with evidence
gaps**, alongside their published scientific accounts. Workspace scope shows
that workspace's records. Exploration outcomes never increase the scientific
account count used for trending. A previous investigation is context for a new
attempt, not a block on repeating the question with different evidence.

## Agent capabilities

Research agents have bounded `SearchPapers` and `ReadPaper` MCP tools backed by
Europe PMC. Searches find scholarly records; reads retrieve available abstracts
or open-access full-text excerpts. Search metadata alone is discovery, not
support for a biological assertion. Captured reads retain their provider,
record identity, retrieval time, scope, exact excerpt boundaries, and source
response checksums. These tools do not provide arbitrary browsing or access to
paywalled full text. They are unavailable to the later research-statement agent,
which must render only the accepted scientific account.

Literature is auxiliary evidence: it does not replace the required relevant
CFDE ancestry of scientific-account component claims. The independent scientific
reviewer can read the captured paper evidence and must retain the distinction
between an abstract, a full text, and a partial excerpt.

The trusted outcome-writing tool writes the structured insufficient-evidence
record to the canonical `/reveal/output/outcome.json`. The workspace-relative
`output` directory resolves to that writable location. Frozen evidence and
agent instructions remain protected.

## Verification

Backend tests exercise source integrity, scientific lineage, outcome ownership,
publication snapshots, and recovery of saved attempts. Browser regressions
`check-analysis-outcomes.mjs` and `check-session-menu.mjs` intercept all API calls;
they do not publish actual results or start paid jobs. Existing live outcomes
are recovered only through an explicit, idempotent recovery operation that
checks the saved input, runtime, ledger and capture marker.
