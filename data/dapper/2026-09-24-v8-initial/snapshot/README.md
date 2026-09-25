# DAPPER

LinkML models for dataset attribution, provenance, evidence retrieval, and
related biomedical knowledge products.

## Repository layout

The root `schema/` directory is the model boundary. Keep schema modules,
examples, converters, and model-specific documentation relative to that
directory so the repository can grow without mixing model concerns with
repository tooling.

```text
schema/
  dapper.yaml               # current root model
  claims.yaml               # propositions, claims, scores, and scientific accounts
  trusty-identifiers.md     # Trusty URI and nanopublication design notes
  identity/                 # computed content identifiers (DAPPER-ID-1)
  examples/                 # LinkML instance and graph examples
  converter/                # Source-data to model converters
```

## Identifiers

Every object carries a computed content address, `dapper:{ClassName}.{digest}` — a GA4GH
`sha512t24u` digest over the fields that constitute what the object *is*. Timestamps, signatures and
mirror observations are excluded, so re-running a pipeline or re-signing a nanopublication does not
change an identifier; changing the analysis does.

**Identifiers are never written by hand.** Leave `id` out of your source data and let the tool mint
it. To turn a pile of gene-set runs into one identified collection:

```bash
uv run schema/identity/mint.py /path/to/your/genesets -o collection.yaml
uv run schema/identity/dapper_identity.py verify collection.yaml
```

`schema/identity/README.md` has the walkthrough, the full `DAPPER-ID-1` profile, and why this is
deliberately *not* a Trusty URI.

Additional model types should be added as separate YAML modules under
`schema/`, with imports expressed relative to `schema/`. Shared vocabulary,
base classes, and reusable enums should be factored into their own modules
once there is a concrete second consumer.

## Current migration

`schema/dapper.yaml` is DAPPER, migrated from the NIH Dataset Attribution and
Provenance Profile it's named after. It covers citation, funding, PROV
lineage, file identity, controlled-access terms, workflow provenance,
nanopublications, scientific accounts, and agentic replay.

Scientific assessments use `Claim`, reusable `Proposition` records, and typed
`ClaimScore` values. `ScientificAccount` organizes a question, hypothesis role,
context, claims, and optional conclusions. Its question references `Question`
or the `KnowledgeGap` subclass. `EvidenceItem` records how source
claims bear on a target proposition; `MechanisticModel` supplies optional
biological structure. `Paragraph` saves a textual expression of an account.
See the [Scientific Claims design](schema/docs/claims.md) and the
[fictional account example](schema/examples/example_scientific_account.yaml).
Questions and gaps can carry entity context; gaps can specify `gap_kind`.
Paragraph citations pin exact scientific objects and citation metadata revisions.
The separate [citation metadata contract](schema/docs/citations.md) records ordered
credit, dates, repository metadata, and publication state without changing the
scientific object's digest.

```bash
uv run schema/lint/lint_provenance.py schema/examples/example_scientific_account.yaml
uv run schema/scientific_claims.py schema/examples/example_scientific_account.yaml
```

Use `File` for generic inputs, intermediates, and outputs, and `C2M2File` for
files carrying C2M2 metadata. Optional DRS representations are separate nodes.
See [files and DRS](schema/docs/files-and-drs.md) and the
[intermediate-file example](schema/examples/example_file_graph.yaml).

`GeneSetCollection` represents a library of named `GeneSet` records and links
its GMT serialization through `has_gmt_file`. A single set selects its row
with `in_gmt_file` plus `gmt_entry`. Direct membership uses collection `members`
and the set's inverse `in_gene_set_collection` list. Collection `n_sets` counts sets and
`n_genes` counts their distinct gene union. Locations and checksums belong on
the referenced `File` or `C2M2File`. See the
[gene-set authoring guide](schema/docs/geneset-authoring.md) and
[two-row example](schema/examples/example_geneset_collection_rows.yaml).

Validate a single instance from the repository root with:

```bash
uv run --with linkml linkml-validate \
  -s schema/dapper.yaml -C Dataset schema/examples/example_dataset.yaml
```

## End-result documents

An *end modality* is a terminal product of a pipeline — a bottom-line result, a
gene set. Each instantiation is published as one self-contained YAML file: the
end result object plus all the provenance around how it was generated.

`linkml-validate` cannot check such a file. The schema declares no `tree_root`,
so pointing it at a graph document raises rather than validating; it only works
on one node at a time against a named class. Lint a whole document with:

```bash
uv run schema/lint/lint_provenance.py path/to/result.yaml   # modality auto-detected
uv run schema/lint/lint_provenance.py --list-profiles
```

On top of per-node schema conformance, it checks the document shape, typed edge endpoints, referential
integrity, identifier correctness, and that every node is reachable from the end
result. Modalities are declared as data in
[`schema/lint/profiles.yaml`](schema/lint/profiles.yaml) Please see [schema/lint/README.md](schema/lint/README.md) for additional documentation.

## Model documentation

The [gene-set authoring guide](schema/docs/geneset-authoring.md) shows document-local
`prefixes`, digest-based GMT file references, and a corrected HuBMAP export.
Use `uv run schema/expand_prefixes.py input.yaml --output expanded.yaml` to
resolve identifier/URI fields and re-mint affected records. The provenance
linter rejects undeclared prefixes in these fields.

The [AF / AA bottom-line mapping](schema/docs/bottom-line-results.md) shows how
to represent S3 prefix collections, pipeline activities, and a published File
distribution without inventing C2M2 or DRS registrations. Its
[complete YAML example](schema/examples/example_bottom_line_af_aa.yaml) passes
the provenance linter and is included in the portal.

The [ancestry guide](schema/docs/ancestry.md) describes `AncestryEnum`, its
HANCESTRO mappings, and the optional `ancestry` field on Dataset and Activity.
The AF / AA example includes explicit ancestry scope. Use
`uv run schema/lint/lint_dig_ancestry.py path/to/result.yaml` to also check
declared ancestry against DIG's public/staging export folder conventions.
The example also uses `trait: KPN.TRAIT:0000096` for atrial fibrillation;
the portal links this CURIE to the renamed KPN trait catalog.

Build the searchable LinkML reference and the provenance inspector together:

```bash
uv run tools/build_docs.py
uv run tools/build_docs.py --serve --port 8000
```

The preview is at `http://127.0.0.1:8000/model/`, with the inspector at `/`.
The reference includes class inheritance diagrams, inherited slots, enums,
ontology mappings, and full-text search. Reference pages come directly from
`schema/dapper.yaml`; the landing page and theme live in `docs/`.

The inspector orders inputs before outputs in both arrow views. **Stored predicates**
points each arrow from its statement's subject to its object; `prov:wasGeneratedBy`
therefore points from a result back to the producing activity. **Forward flow** keeps
arrows flowing toward outputs and uses inverse labels, such as `prov:generated`
and the reading aid “was used by.” Graph connections always shows the original
subject → predicate → object statement. Switching views changes neither the layout
nor upstream tracing, and does not rewrite the document or its identifiers.
The selected result's files and DRS access records stay visible as context during
tracing; unrelated downstream analyses remain dimmed. File nodes use their
filename as the display label when available.

Generated Markdown (`.build/model-docs/`) and HTML (`site/`) are ignored by Git.
CI builds the documentation in strict mode to catch broken links. The existing
Pages workflow publishes the combined site after changes reach `main`, with model
documentation under `/dapper/model/` and the inspector at its existing URL.

The schema declares `dapper_class:`, `dapper_slot:`, `dapper_enum:`, and `dapper_type:` prefixes
for links to definitions in the model reference. Existing vocabulary CURIEs
such as `dapper:Edge` resolve through `/dapper/ns/#Edge` to those pages.
External semantic mappings and `dapper:{ClassName}.{digest}` record IDs retain
their existing meanings. The build checks that every namespace redirect has a
generated destination page.

## Contribution workflow

Validate schema changes, examples, and converters locally; push to
`broadinstitute/dapper` only once the migration shape is agreed.

We use prek to validate and check files before committing. Before your
commit, please run

```bash
prek install
```
