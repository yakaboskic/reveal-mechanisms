# Authoring a DAPPER end-result document

This directory holds the linter that decides whether a provenance document is
valid DAPPER. If you have been asked to produce one document per end result,
this is the guide.

## What you are producing

One YAML file per end result. Each file holds **the end result object plus all
the provenance around how it was generated** — the activities that ran, the
files they consumed and produced, and the edges connecting them. All reified
edge endpoints and inline DAPPER identifiers must resolve inside the file.
Inline relationships may also reference external identifiers such as ORCIDs.

An *end modality* is a terminal product of a pipeline. The supported profiles are:

| Profile | End result | How many per file |
|---|---|---|
| `bottom-line-result` | A `Dataset` — variant-level summary statistics from a meta-analysis | exactly 1 |
| `geneset` | A `GeneSet` or `GeneSetCollection` | 1 or more |
| `scientific-account` | A `ScientificAccount` | exactly 1 |

A bottom-line document has exactly one final Dataset, but may contain other
Datasets as upstream inputs or derivation sources. Terminal selection excludes
resources consumed by `prov:used`, named as `prov:wasDerivedFrom` sources,
or contained through `prov:hadMember`,
whether represented inline or as edges. Profile detection and required
provenance checks apply to the remaining end results. Multiple final Datasets
are still rejected. Gene sets have no cap because a converter run may emit
many gene sets sharing a single provenance subgraph. A collection's member
sets are not separate terminal results. GMT selectors must pair `in_gmt_file`
with `gmt_entry`. Counts must agree with complete membership lists when
supplied; collection `n_genes` is a union, not a sum. See the
[gene-set guide](../docs/geneset-authoring.md) and
[row-level example](../examples/example_geneset_collection_rows.yaml).

Direct membership uses `GeneSetCollection.members` and the optional inverse
`GeneSet.in_gene_set_collection` list. Either direction is accepted; when both
are supplied they must agree. Inverse links also connect contained sets when
the collection's full membership has not been enumerated. These links are
independent of GMT file and row references.

## The three things you need

1. **The schema** — [`schema/dapper.yaml`](../dapper.yaml). The authoritative
   list of every class and every field. If a field is not in there, it does not
   exist.
2. **The canonical example** for your modality — copy its shape:
   - `bottom-line-result` → [`example_bottom_line_result.yaml`](../examples/example_bottom_line_result.yaml)
   - `geneset` → [`example_geneset_graph.yaml`](../examples/example_geneset_graph.yaml)
3. **The linter** — this directory. Run it before you hand anything back.

## Running the linter

```bash
# auto-detects the modality from the end result in the file
uv run schema/lint/lint_provenance.py path/to/your/result.yaml

# or pin it, which also fails if the file is not that shape
uv run schema/lint/lint_provenance.py result.yaml --profile bottom-line-result

uv run schema/lint/lint_provenance.py --list-profiles
uv run schema/lint/lint_provenance.py --self-test    # lint the canonical examples
```

Exit status is 0 only when there are no errors. Warnings do not fail the run
unless you pass `--strict`. Errors mean the document is wrong; warnings mean it
is probably incomplete.

For DIG bottom-line exports, run the optional source-specific check:

```bash
uv run schema/lint/lint_dig_ancestry.py path/to/your/result.yaml
```

It runs DAPPER validation first, then compares Dataset ancestry with its own
or linked File's production/staging export folder. It reports unknown folder
codes and conflicting declarations without changing metadata or querying S3.
See the [ancestry guide](../docs/ancestry.md) for its exact scope. These storage
conventions are not part of generic DAPPER validation.

## Document shape

Documents may also declare a top-level `prefixes` mapping of namespace names
to absolute URI bases. These supplement the selected model's prefixes without
overriding them. Undeclared prefixes and relative values in identifier/URI
slots are errors; CURIEs and full URIs resolve to the same graph endpoints.
Names, commands, and opaque alternate identifiers are not parsed as CURIEs.
See [gene-set authoring](../docs/geneset-authoring.md) and its corrected HuBMAP
example for the complete rules and `has_gmt_file` references.

To export identifier/URI fields as absolute URIs, with updated content-derived
IDs and references, run:

```bash
uv run schema/expand_prefixes.py result.yaml --output result.expanded.yaml
```

The exporter validates before writing and fails on any unresolved prefix.

Nodes go in lists keyed by type, edges in lists keyed by relationship. The key
names are fixed — a typo like `dataset:` for `datasets:` is an error, because
nodes under an unrecognised key are silently never validated.

```yaml
# ---- nodes ----
datasets:
  - id: dapper:Dataset.3_QblmXDJVn8WX9JZWuf7tVXDM8201F8
    name: Bottom-line trans-ethnic meta-analysis for T2D
    resource_type: dataset
    access_level: controlled

activities:
  - id: dapper:Activity.wJzaZQWxxACtrWYRP89EhNjzc5ZaXBSF
    name: LoadTransEthnicStage T2D
    command: "python loadAnalysis.py --trans-ethnic --phenotype T2D"

# ---- edges: subject -> predicate -> object ----
was_generated_by_edges:
  - subject: dapper:Dataset.3_QblmXDJVn8WX9JZWuf7tVXDM8201F8
    predicate: prov:wasGeneratedBy
    object: dapper:Activity.wJzaZQWxxACtrWYRP89EhNjzc5ZaXBSF
```

Common node keys: `datasets`, `gene_sets`, `activities`, `files`, `c2m2_files`,
`drs_objects`, `persons`, `organizations`, `awards`, `publications`.
Common edge keys: `was_generated_by_edges` (result → the activity that made it),
`used_edges` (activity → an input it consumed), `has_file_edges`
(dataset → an ordinary file), and `has_drs_object_edges`
(dataset → a registered DRS representation).

Every node or edge group must be a list of mappings. Each node must have a
nonempty string `id`; records with missing IDs are reported and still checked
for schema violations. Unknown keys and malformed containers are errors.

The full list of node keys is `DOC_GROUPS` in
[`../identity/dapper_identity.py`](../identity/dapper_identity.py); edge keys are
the snake-cased name of any `Edge` class in the schema, plus `_edges`.

## Do not write identifiers by hand

Leave `id` out and let the tool mint it. Identifiers are content digests —
`dapper:{ClassName}.{digest}` — computed from the fields that determine what the
object *is*:

```bash
uv run schema/identity/dapper_identity.py assign your-result.yaml
```

If you edit a node after minting, re-run `assign`. The linter recomputes every
digest and reports any id that no longer matches its content, because an id that
addresses different content is worse than a missing one: the file still parses,
still validates and still renders.

## The rules the linter enforces

Generic, applied to every modality:

| Check | What fails it |
|---|---|
| `shape` | A top-level key that is not a known node or edge group |
| `nodes` | A node with an invented field, a bad enum value, or a malformed date |
| `edges` | An edge with an invented field |
| `endpoints` | An edge whose subject or object is the wrong type of node |
| `predicates` | *(warning)* A predicate that disagrees with the edge type |
| `refs` | A reference to an id that is not defined in the file |
| `duplicate-ids` | Two nodes sharing one id |
| `id-class` | A node whose id names a different class than its group |
| `identity` | A node whose id no longer matches its content |
| `reachability` | A node the end result cannot reach through the graph |

Per modality, from [`profiles.yaml`](profiles.yaml): how many end results the
file may contain, and which provenance edges each must carry. A
`bottom-line-result` must have a `was_generated_by` relationship to an `Activity`
(error) and should identify a distribution through `has_file` to a `File` or
`C2M2File`, or `has_drs_object` to a `DrsObject` (warning if neither exists).
The link alone does not establish that bytes are accessible or checksummed.
The profile's `any_of` alternatives share a minimum count of distinct targets;
`object_class` accepts one class name or a list of accepted classes.

Inline and reified forms satisfy the same requirements. For example,
`Dataset.was_generated_by: <activity-id>` is equivalent to a `WasGeneratedBy`
edge. Predicate CURIEs and expanded URIs are compared by their expanded URI;
an omitted edge predicate uses the schema default. Repeating the same
relationship in both forms counts once.

### Two rules worth understanding

**Invented fields are rejected outright.** This is the main thing the linter is
for. If you need to record something the schema has no field for, say so — do
not add a field. It will be caught, and a field that is silently dropped is
worse than a question.

**Every node must be reachable from the end result.** The file is that result's
provenance, so a node nothing connects to does not belong in it. This is the
check that catches a plausible-looking object that is attached to nothing.
Reachability follows edges from the result backwards through
`was_generated_by` → activity → `used` → inputs, and also picks up an
activity's other outputs, including outputs linked by inline generation
relationships. Only schema-declared relationship slots are traversed: literal
fields such as `description` never create a connection or a dangling-reference
error merely because their text happens to look like an identifier.

Paragraph citation targets are also followed through `citations[].target_id`.
They are reference links, not evidence edges. Scientific-content checks validate
the target class, Unicode code-point spans, and optional exact-text anchors.
External citation metadata is checked separately by `schema/citation_metadata.py`;
the graph linter cannot establish that a pinned registry revision is available.

## Adding a modality

Add an entry to [`profiles.yaml`](profiles.yaml) — naming the terminal class,
how many are allowed, and the required edges — and a canonical example. No code
changes. The engine reads edge classes and their expected predicates straight
out of `dapper.yaml`.

Two constraints: no two profiles may share a terminal class (that is the
auto-detection key), and a class must be a node in the schema to be a terminal.
`GeneProgramQualityScore`, for instance, is currently an inline struct with no
`id`, so a "QC report" modality would require promoting it to a node class
first.

---

## Appendix: why a linter, when `linkml-validate` exists

Maintainer notes. Nothing here is a limitation of this linter — each row is a
limitation of the standard LinkML validator, and the right-hand column is the
part of `lint_provenance.py` that covers it. Every one is exercised by
`tests/test_lint_provenance.py`.

`linkml-validate` does reject invented *fields*, and that is the highest-value
check of the lot, so `check_nodes` and `check_edges` call it rather than
reimplementing field checking. These are the gaps around it:

| Gap in `linkml-validate` | Covered by |
|---|---|
| **Cannot validate a document at all.** The schema has no `tree_root`, so pointing it at a graph document raises rather than validating. It only works on one node at a time against a named class. | The linter supplies the group-key map (`datasets:` → `Dataset`), then validates each node and edge individually. |
| **Edge endpoints are untyped.** `subject`/`object` are bare `uriorcurie`; intended types exist only as prose on the edge class (`description: Dataset → Agent`). A `prov:wasGeneratedBy` edge pointing a Dataset at an Award validates clean. | `check_endpoints`, against the types in `profiles.yaml`. |
| **Predicates are unconstrained.** `predicate: prov:totallyMadeUpPredicate` passes clean, while an invented field on the same edge is caught. | `check_predicates` (warning), reading the `ifabsent` default the schema already declares. |
| **No notion of a connected graph.** Every node can be individually valid while one typo'd id leaves the result unable to reach its own provenance. | `check_refs` and `check_reachability`. |

**One trap if you write your own validation script.** The in-process validator
API defaults to `closed=False`, and in that mode invented fields are *accepted* —
the same document the CLI rejects passes clean. `build_validator` sets
`closed=True` explicitly; removing it would silently disable the
hallucinated-field check while leaving every test looking green.
