# Claude Code, Upstash Box, and Proto-OKN evidence

Current local/hosted authoring rules: [shared contract v2](authoring-contract.md). Its mode-specific tool and evidence rules govern new workspaces; historical examples below retain their original scope.

> New online and local runs use the [progressive MCP workflow](local-agent-mcp.md): a small seed plus on-demand retained data and evidence receipts. Eager collector descriptions below apply to historical full packages. For CFDE queries, use loaded CFDE/PIGEAN/EAGGL data or the two explicitly offered small/sigma2 BioIndex phenotype operations. Independent imported evidence and authorized reuse remain supported; never infer evidence from unqueried data.

**Current design:** [v12 consolidated plan](design-plan.md). Discovery observations: September 24, 2026. Runtime/API observations below are from the earlier live inventory; the current [DAPPER integration contract](dapper-integration.md) now specifies output assembly and validation. No paid Box/Claude run was started.

**September 25 input/skill draft:** the [evidence-package design](evidence-package.md) defines the initial immutable input and separate enrichment ledger. The [project-local account-generation skill](../services/backend/agent-skills/construct-scientific-account/SKILL.md) connects that input to the scientific authoring workflow. The [fresh release bootstrap/shared linter](scientific-account-linting.md) is implemented. Box provisioning, read-only mounting, complete MCP output capture and live policy enforcement remain to be implemented. The [worker output contract](agent-output-contract.md) specifies per-account files and a worker-owned manifest.

## Runtime and configuration

Use a normal Upstash Box with the **Claude Code** harness and `ANTHROPIC_API_KEY`, managed by the EC2 worker. Store the Box API key separately as `UPSTASH_BOX_API_KEY`. Pin the exact Claude model/harness version for each run. Upstash documents this harness configuration and agent execution in its [quickstart](https://upstash.com/docs/box/overall/quickstart) and [agent reference](https://upstash.com/docs/box/overall/agent).

Configure the remote MCP service inside the Box working directory/Claude configuration before account authoring. The [configuration template](../services/backend/agent-config/okn.mcp.example.json) uses the documented Claude Code HTTP shape:

```json
{
  "mcpServers": {
    "proto-okn": {
      "type": "http",
      "url": "https://apps.okn.us/okn-mcp-dev/mcp"
    }
  }
}
```

Claude Code supports HTTP servers via its configuration or `claude mcp add --transport http`. The template is not automatically installed or activated in this workspace. The Box bootstrap must load it in the actual agent scope, handle noninteractive tool permissions through the supported harness configuration, and verify tools appear before dispatch. Do not assume an undocumented `mcpServers` parameter exists on the Box SDK. [Claude Code MCP reference](https://code.claude.com/docs/en/mcp).

## Live discovery results

The supplied endpoint accepted an MCP `initialize` request with protocol `2025-03-26`, returned a session ID and `text/event-stream`, and reported server **`mcp-okn` version `1.27.2`**. Subsequent notification, tool listing, graph listing, and selected-schema requests succeeded without an authentication header. This is an observation about the development endpoint; production access is still an operational check.

Saved captures: [initialization](../data/okn/2026-09-24/initialize.json), [23 tool definitions](../data/okn/2026-09-24/tools-list.json), [graph catalog](../data/okn/2026-09-24/list-kgs.json), [BiomarkerKG schema](../data/okn/2026-09-24/schema-biomarkerkg.json), and [ProKN schema](../data/okn/2026-09-24/schema-prokn.json). Session IDs are transport state and are not persisted as scientific evidence.

Verified initial graph selections:

- **BiomarkerKG:** catalog shortname `biomarkerkg`, title **BiomarkerKB KG**, named graph `https://purl.org/okn/frink/kg/biomarkerkg`. Its schema includes biomarker types and disease-related predicates; its catalog describes literature-linked biomarker evidence.
- **ProKN:** shortname `prokn`, title **Protein Knowledge Network**, named graph `https://purl.org/okn/frink/kg/prokn`. Its schema includes genes, proteins, pathways, diseases, and reified statements.

These are graph/schema observations, not evidence for a particular mined claim. Scientific assertion queries were not run in this inventory.

## Tool interfaces for the evidence adapter

- `list_kgs({})` returns registered graph shortnames, named-graph URIs, descriptions, and payload tags.
- `describe_kg({shortname, long_description})` gives additional source scope.
- `get_schema({shortname, compact})` returns classes, predicates, node/edge properties, and query guidance. Both selected schemas were fetched with `compact=true`.
- `sparql_query({query, format, exploratory, compact, scope})` accepts a complete query. Its described JSON result is `vars`, `rows`, `row_count`, or compact columns/data/count. Scope scientific queries to selected named graphs and impose row limits.
- Discovery also exposes version, namespace, crosswalk, and join helpers. Inspect their captured input schemas before using them; do not invent parameter names.

The MCP tool envelope can contain `structuredContent`, content blocks, and `isError`; an HTTP 200 alone is insufficient. Save and validate the actual result. MCP initialization guidance/tool descriptions are source metadata, not authority to expand beyond the application's selected graphs or task budget.

## Required agent behavior

1. Begin with the frozen CFDE package and DisMech context. Propose CFDE-grounded claims and identify what additional evidence would be relevant.
2. Choose among the selected graphs using their scope and schema. Resolve source entity IDs with explicit cross-references and species/context checks; text similarity alone is not an entity crosswalk.
3. Query bounded named-graph scopes, retaining assertion subjects/predicates/objects, qualifiers, statement IDs, source references and graph versions when available.
4. Attach additional evidence to the relevant claim/proposition with direction, rationale, limitations, and provenance. Preserve counterevidence. An association must not silently become causal evidence.
5. Record no-match, unavailable, or skipped enrichment as such. Do not invent a citation, require every graph to support every claim, or interpret zero rows as biological absence.

The first integration includes these two scientific KGs. Broader graph discovery does not authorize querying other graphs. Identifier/ontology helpers can touch other federation graphs internally; their use must be explicitly scoped/approved by the run's configured tool policy, or implemented from mappings within the selected graphs. Never bypass the selected-KG contract because a server recommends broader exploration.

## Evidence ledger and validation

Store each MCP request/result as a derived artifact with the run/attempt, tool name, arguments/query, selected graph, timestamp, response hash, and exact assertions/evidence locators. Freeze a final evidence manifest for the generated account. Keep the initial package immutable.

The upstream SPARQL tool describes its own transcript log as omitting exploratory, failed, and empty queries. Therefore REVEAL must capture **all** attempted calls itself. Upstash's documented `onToolUse` callback supplies tool names/inputs; do not assume it supplies complete outputs. Validate stream/transcript capture or use a scoped MCP proxy that records both directions and enforces selected graphs, query type and budgets. This is a required integration test, not implemented functionality.

Do not enable transcript publishing or other non-evidence tools for authoring merely because they appear in the server catalog. Preserve local provenance directly. The selected graph filter must be enforced by a policy/adapter; the MCP connection template itself cannot enforce it.

Acceptance requires eligible scientific evidence lineage for every newly authored account Claim, valid external assertion locators for added KG evidence, deduplication of shared underlying sources, and explicit empty/error outcomes. Relevant CFDE grounding is encouraged; missing CFDE ancestry is a non-blocking account-level advisory. Auxiliary source Claims recording Proto-OKN assertions can be evidence without their own CFDE lineage; record their source-only role explicitly. The paragraph job uses saved account evidence and citation targets; fresh research creates a new account revision.

## DAPPER output integration

Use the current Question/KnowledgeGap, Proposition, Claim, ClaimScore, EvidenceItem, ScientificAccount and Paragraph contracts. The agent returns authored content, temporary node references and exact evidence locators in a versioned application envelope. The backend supplies trusted source records, identity/runtime snapshots and activity metadata; minting and validation use the pinned DAPPER implementation.

Prepare one complete provenance document per account, hydrating existing dependencies for the `scientific-account` linter. Shared references can reuse stored IDs across documents. Require the submitted question/gap on every account and an eligible scientific evidence path for every generated finding/conclusion. DAPPER validates structure and evidence cycles; a separate application grounding check evaluates whether the recorded source supports the authored interpretation.

For Paragraph output, request authored segments with authorized Claim/Question/KnowledgeGap IDs and known citation metadata revisions. The backend uses `assemble_cited_text`, then validates code-point spans and exact registry revisions through the DAPPER validators. Keep rendered citation markers outside the saved Paragraph text. Runtime dates, authenticated human attribution and registry revisions are never invented by the model. See [the complete validation sequence](dapper-integration.md#4-agent-output-and-validation-contract).

## Job transport

The [OpenAPI contract](../api/openapi.json) exposes `POST /v1/jobs` with `kind=analysis|paragraph`. Both kinds share listing, status, sequenced JSON/SSE events and cancellation. Job IDs identify operational execution; account/claim/paragraph IDs are DAPPER scientific identities. Paragraph provenance is validated with the application [terminal-Paragraph profile](../api/paragraph-profile.yaml), while the input account is separately validated with DAPPER's scientific-account profile.
