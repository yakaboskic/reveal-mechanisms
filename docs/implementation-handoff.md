# Implementation handoff

Read [the consolidated v12.1 plan](design-plan.md) first, then [the audit](design-package-audit.md). Implement the approved HTML interaction against [OpenAPI 0.2.0-draft](../api/openapi.json). Start with the populated EAGGL→CFDE links; improving mapping quality or completing the factor catalog is not a prerequisite. This document is a build checklist; this consolidation has not built the application.

## 1. Data and persistence first

- Preserve migration 001/002 imports, migration 004 routing links, original GeneSet IDs and source revisions. Aurora has 14 applied tables; Prisma maps those plus nine pending DisMech tables. Snapshot/backup before schema changes. SQL migrations remain authoritative; no `prisma db push` to an existing database.
- Apply migration 003 through the [DisMech importer](dismech-import.md) and run its exact payload/projection verifier. Read only completed import IDs. Expected counts are in its manifest and the consolidated plan.
- Connect `database_search_index` to `eaggl_cfde_factor_links` for the configured completed mapping run. Reuse the 3,103 existing label vectors and 1,756 mapped factors. Filter for mapped candidates before the top-five cutoff, deduplicate on native CFDE ID, and resolve detail with `lookup_factor`. Exact trait/factor-number routing is accepted even when labels/genes differ. No full CFDE re-import or re-embedding is required.
- Use one collector-compatible DAPPER Mechanism/catalog File projection for each resolved native anchor. Preserve the friendly EAGGL label as presentation metadata and record embedding/source/mapping runs. Join ranked GeneSet links through existing aliases to existing DAPPER objects; unresolved summary links do not disqualify an anchor. See [the mapping contract](design-plan.md#initial-crosswalk-backed-retrieval-contract).
- Add one shared DisMech→DAPPER adapter for search/API/collector. Preserve source gap text, rationale fallback, kinds, source hashes and exact attachments. Confirm API-selected gap ID/payload equals collected package ID/payload. Status-only source updates must not unexpectedly remint semantic objects through batch-specific provenance.
- Add application migrations/repositories: app users/login mappings/transfer audit; scientific source mappings/query provenance; drafts/explorations/frozen anchor bindings; immutable requests; job attempts/leases/events/outbox/idempotency; immutable artifacts/documents/payload snapshots; account membership; independent owner grants; citation registry revisions/render manifests. Add DisMech query embeddings as needed; reuse the existing factor vectors.
- Keep API Python repositories and Prisma mappings consistent with reviewed SQL; update Prisma only for intentionally added schema, not to pretend unapplied tables are live. Address native Prisma TLS independently if a colleague needs that client.

**Gate:** imports replay without duplicate rows; a stored EAGGL match routes through its completed mapping run to a native CFDE anchor and existing GeneSet objects where available; no label/gene agreement check blocks routing; unmatched factors are excluded from selectable anchors; all original catalog digests remain valid. Switching the configured mapping run affects new drafts only; saved requests replay their frozen bindings. Access/ownership is independent of scientific IDs.

## 2. Gateway, gap composer and history

Implement [gateway contracts](gateway-contract.md), NextAuth Google/ORCID and anonymous continuation, portable ownership, private-by-default data, first-login upgrade and existing-account claim with both proofs/consent. Add a DAPPER/citation fixture for pseudonymous human attribution using actual supported fields; do not invent an Agent class or name/ORCID.

Implement OpenAPI identity, gaps/source detail, mechanism search/suggestions, drafts and explorations. Use the [HTML source](../design/README.md) as visual reference, not as an auth implementation. No free-text submission, no editable source-linked DisMech list. Auto-add five total; removal persists and zero anchors blocks submit. Before session, keep edits local; afterward one-second autosave and version-conflict reconciliation.

**Gate:** two owners cannot read/cancel/export each other's private work; expired/forged/retired assertions fail; absent ORCID email works; empty drafts save; typed-but-unselected/forged source content cannot submit; missing/changed source revisions reject; a gap with no mechanism attachments can use an explicit valid EAGGL anchor; two-tab saves, lost acknowledgments and duplicate submits behave deterministically. Anonymous history can upgrade without rewriting prior attribution.

## 3. Queue, collector and evidence package

Use the [existing builder](evidence-package-builder.md); avoid a second payload implementation. Pass exact frozen gap/source revision and native anchors resolved through the pinned crosswalk. Keep the originating EAGGL hit, mapping run and retrieved scores in request/attempt provenance alongside the package hash; do not pass a legacy ID as the collector's factor argument. Replace local-file adapters with version-pinned repository adapters while preserving package semantics. Persist raw response bytes, attempts, source JSON pointers, graph clipping/coverage, package and runtime hashes. Validate against [LinkML/JSON Schema](../schema/README.md) and the builder invariants.

Create MySQL-backed job/outbox/attempt claiming, leases/heartbeats, Box IDs and terminal guards. Bind measured token budget, ownership, selected graphs and execution limits before agent dispatch; byte-size validation alone is insufficient. Emit typed preparation/public activity events and replayable JSON/SSE with reconnect. Stop is an authoritative server transition.

**Gate:** same frozen bytes/code/policy reproduce identical package; new live captures preserve new observations; one expansion round only; exact empties differ from failures; source/graph/model conflicts stop dispatch; worker restart does not launch a duplicate paid Box; stale attempts cannot overwrite accepted/cancelled results; browser disconnect does not stop work.

## 4. Research agent and scientific acceptance

Provision Box/Claude Code and call [the startup helper](../scripts/start_research_agent.py) for each fresh attempt. Clone/verify the [locked DAPPER release](../services/backend/agent-runtime/dapper-release.json), install the skill and read-only trusted runtime/input mounts, configure selected-graph MCP access and full bidirectional tool ledger. Pin Claude model/harness and actual runtime settings; no floating release checkout.

Implement [output assembly](agent-output-contract.md): preserve raw drafts, hydrate trusted nodes, stamp real provenance, mint temporary IDs and invoke [the shared final validator](scientific-account-linting.md) from the backend's own trusted environment. Add exact source-locator/metric checks, ledger/graph policy, attribution and scientific-support review. Bounded repair must not fabricate evidence or convert technical failures to biological insufficiency.

**Gate:** wrong selected gap, altered trusted payload, missing explicit CFDE lineage, wrong EvidenceItem target, illegal KG scope, fabricated score/citation, missing tool output and private-reasoning telemetry are rejected. Test one/multi-claim accounts with reused source artifacts and distinct proposition targets. One terminal account per document. Keep the UI's invented KG assertions out of the production acceptance fixture.

## 5. Account UI, Paragraph and citation service

Build account list/detail, claim detail, generic resolver and bounded provenance/file access. Closing synthesis leads; associated claims collapsed, inline expansion, separate claim page. DAPPER scientific content and application presentation metadata remain separate.

Implement a paragraph-generation skill and worker. Transactional account acceptance queues its default paragraph job through a unique outbox key; partial paragraph failure leaves conclusions available. Pin exact account payload/citation revisions/preferences; paragraph processing does not query KGs. Use DAPPER `assemble_cited_text`, independent account validation and the terminal Paragraph profile.

Implement registry first-mint/revision events, exact resolver/access rules, BibTeX/BibLaTeX/CSL and pinned CSL processor/styles/locales for APA/MLA. Implement complete Paragraph exports, sanitized HTML/plain clipboard content, Markdown/LaTeX and bibliography. Canonical resolver domain is deployment configuration; replace fixture/example/local URLs without mutating scientific identity.

**Gate:** Unicode/emoji/repeated/coincident spans; missing revision does not fall back to latest; no new unassessed propositions in paragraph; true human/software roles and ORCID provenance; no invented DOI; exports contain the same complete citation set; LaTeX reminder includes `references.bib`; unauthorized artifacts remain unavailable; partial collection traversal does not remint a subset collection.

## 6. Deploy and evaluate

Configure EC2 API/worker services, private RDS access with verified TLS, durable artifact store, HTTPS/allowed gateways, signing keys/provider registrations, Anthropic/Box secrets, selected tool proxy, immutable release/skill/image pins and observability. Set duration/cost/concurrency/anonymous quotas, retention and backup policy before paid runs; these are deployment choices, not a reason to redesign the scientific schema.

Run one bounded real selected-gap→existing EAGGL embedding→crosswalk→CFDE factor→package→Box/MCP→validated account→auto Paragraph→export scenario, plus retry/cancel/restart/access tests. Measure retrieval relevance and latency separately from claim grounding. Later data work includes broader/improved mappings, a full current-model embedding corpus and complete GeneSet source-row provenance. Other optional work: more KGs, graph embeddings, recursive user-directed expansion, public publishing and recovery links.

## Useful verification commands

Run from repository root in a suitable dependency environment; these are offline unless marked otherwise:

```bash
python scripts/build_openapi.py
python scripts/validate_openapi.py
python scripts/build_api_viewer.py
python scripts/evidence_package_schema.py generate --check
python scripts/evidence_package_schema.py validate api/examples/evidence-package/evidence-package.yaml
PYTHONPATH=services/backend/src python -m unittest discover -s services/backend/tests -p 'test_evidence*.py'
PYTHONPATH=services/backend/src python -m unittest discover -s services/backend/tests -p 'test_scientific_account_lint.py'
# Read-only Aurora verification after the separately authorized DisMech load:
# .venv/bin/python scripts/import_dismech.py verify --report data/dismech/database-verification.json
# Read-only lookup of the already populated crosswalk (no import/re-embedding):
# .venv/bin/python scripts/link_eaggl_cfde.py lookup --factor-id 'AD::Factor1' \
#   --run-id 272cfa19d093257863d7e7134776229dbc9b9b018d974e6b311285686833ec31
```

Agent startup with `--prepare-only` still clones the pinned GitHub release; it does not provision Box or call Anthropic. Import commands with `--apply` mutate the database and belong to the later implementation/import phase. Keep `.env` and credentials out of handoff archives.
