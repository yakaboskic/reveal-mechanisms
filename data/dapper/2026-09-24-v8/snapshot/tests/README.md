# Tests

```
uv run --with-requirements tests/requirements.txt pytest tests/ -q
node --test tests/portal.test.cjs
```

Nothing to install first. The code under test — `schema/converter/geneset_to_dapper.py`,
`schema/identity/dapper_identity.py`, `portal/build.py` — is a set of standalone PEP-723
scripts that declare their dependencies inline, so there is no package to install in
editable mode. `tests/requirements.txt` exists only so `uv` can assemble one environment
holding pytest *and* those scripts' dependencies at once. `tests/conftest.py` puts the two
script directories on `sys.path`.

The portal's JavaScript tests use Node's built-in test runner and the bundled
Cytoscape library, with no npm install. They exercise reference links, safe value
rendering, uploaded graph defaults, and tracing across nested graph groups.
They also check that arrow views preserve input-to-output order, that stored
predicate arrows point from subject to object, and that both views trace the
same upstream provenance without including unrelated outputs.

`test_citations.py` checks gap entity context, inline citation shapes and Unicode
spans, pinned registry revisions, attribution/date constraints, and identity
isolation between scientific objects, paragraphs, and citation metadata.

| File | Covers |
|---|---|
| `test_converter.py` | the dig.geneset → DAPPER crosswalk: node mapping, the sha256 join across the metadata sidecar, edge direction, overlay precedence, and an end-to-end `convert_one` |
| `test_identity.py` | the minting algorithm: the GA4GH digest primitive, `digest_of` parsing, what constitutes identity, `assign_ids`/`verify`, the frozen vectors, and `DOC_GROUPS` coverage |
| `test_document_prefixes.py` | document namespace declarations, unresolved CURIE failures, mixed URI/CURIE graphs, GMT file references, and validated URI export with rewritten content IDs |
| `test_gene_set_collections.py` | collection membership, GMT row selectors, distinct gene counts, location expansion, and converter library detection |
| `test_examples.py` | every `schema/examples/*.yaml`: parses, ids match content, no duplicates, `_illustrative` points at real nodes |
| `test_claims.py` | scientific accounts, hypothesis roles, Question/KnowledgeGap inheritance, evidence targets, paragraph rendering, and content identity |
| `test_files.py` | generic intermediate files, stable IDs across relocation and DRS registration, converter provenance, and portal inheritance |
| `test_ancestry.py` | ancestry validation and identity, qualified ontology mappings, example scope, and optional DIG folder consistency checks |
| `test_traits.py` | catalog trait references, AF's stable KPN identifier, trait identity, and portal inheritance |
| `test_model_uris.py` | documentation CURIEs, preserved semantic mappings, and namespace redirects to generated pages |
| `test_portal.py` | portal links agree with model routes; enum mapping strength and embedded-data escaping |

## Fixtures

[`fixtures/linter-review/`](fixtures/linter-review/README.md) contains a valid
control and seven synthetic regression cases for PR #27. Some are deliberately
invalid; `test_lint_provenance.py` checks their intended acceptance or rejection.

`fixtures/geneset-hubmap-hz2/` is a real `dig.geneset` run — HuBMAP gene set
`402cf4a1f3682a2e5bf1b002`, 9 File / 2 AnalysisType / 1 GeneSet nodes and 11 edges. It
moved here from `schema/examples/`, which is the model's worked examples in DAPPER's own
YAML; this is raw upstream input, and only the converter and its docs ever read it.

## Two things these tests deliberately do differently

**`test_examples.py` globs, it does not list.** `lint_identity.py` checks a hardcoded
`GRAPH_DOCS` of four files out of eleven. A stale `BioComputeObject` id in
`example_graph.yaml` — introduced when `5ff61ad` rewrote the `nih:` prefix without
re-minting — sat outside that list and was invisible. Globbing means a new example is
covered the moment it is added, rather than when someone remembers to extend a list.

**The identity tests assert behaviour, not just outcomes.** `verify-vectors` proves the
frozen fixtures still reproduce; it cannot tell you *why* a digest moved. The unit tests
pin the individual properties — class name is part of the identity, a node's own id never
feeds its digest, external ORCID/ROR ids survive minting — so a regression names itself.

## When a test fails

| Failure | Usually means |
|---|---|
| `has stale identifiers` | content changed without re-minting → `uv run schema/identity/mint.py <file> -o <file>`, or substitute the reported digest |
| `add these to DOC_GROUPS` | a new Node class needs a document-list key in `dapper_identity.py`, or the portal and minter will disagree about it |
| a permanent vector fails | **fix the code, never the vector** — see `schema/identity/README.md`. A changed algorithm is `DAPPER-ID-2` under a new prefix |
