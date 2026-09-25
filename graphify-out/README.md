# REVEAL repository knowledge graph

Open `graph.html` locally for the interactive graph. Its visualization library is
loaded from a pinned public CDN, so the first load needs internet access. Search
for `import_eaggl_factors.py`, `schema.prisma`, `DismechImport`, or
`eaggl_cfde_links.py`; select nodes to inspect their source and relationships.

- `graph.json`: Graphify's undirected graph, 1,718 nodes, 4,583 connections.
- `GRAPH_REPORT.md`: labeled communities, architectural hubs, and audit notes.
- `extraction.json`: all 4,887 source-oriented relationship records and nine
  hyperedges, including the 304 connections combined in the undirected export.
- `SOURCE_COVERAGE.json`: 194 scoped files with canonical file-node IDs.
- `BUILD_CORPUS.json`: the full scope inventory and excluded sensitive filenames.
- `GRAPH_HEALTH.json`: endpoint, loop, and repeated-connection diagnostics.
- `BUILD_ADAPTER_AUDIT.json`: deterministic extraction repairs.
- `analysis.json`: complete analysis and suggested questions.
- `BENCHMARK.json`: estimated retrieval-context compression, not actual usage.
- `cost.json`: explicitly unavailable host-agent token counts.

The scope includes scripts, backend modules, tests, SQL migrations, Prisma,
LinkML/JSON/OpenAPI schemas, project documentation, and application examples.
`.graphifyignore` excludes credentials, bulk data captures, dependency/vendor
copies, and redundant generated UI bundles. Named references to excluded
artifacts may appear as reference-only nodes. No database or embedding service
was called for this build. Planned and historical documentation remains labeled
as such; the graph is not a deployment-state audit.

From the repository root:

```sh
graphify query "EAGGL CFDE Prisma"
graphify path "link_eaggl_cfde.py" "cfde_gene_set_aliases"
graphify explain "DismechImport"
```

For a complete refresh, ask Codex to rerun the graphify skill in this repo. The
installed detector does not recognize `.prisma`: add `schema/prisma/schema.prisma`
to the semantic pass explicitly, and retain the custom JSON-pointer and
cross-migration reference handling in `enrich_extraction.py`. The two Python
helpers here document this build's assembly stages; they require the temporary
extraction inputs produced by the skill, which are cleaned after completion.
The normal `graphify update` command alone will not refresh the Prisma semantics
or reproduce these supplemental links. Graphify's manifest and semantic cache
are saved for subsequent skill runs.

Validation: all 23 Prisma models are present; no dangling/missing endpoints or
self-loops; embedded viewer JavaScript passes a syntax check. Local browser
security policy prevented visual preview in the automated browser.
