# Adapting `dig.geneset` gene-set provenance to DAPPER

**Status:** original adaptation report · **Date:** 2026-07-24

**Model update (2026-09-24):** the library described here is now a
`GeneSetCollection`, containing individual `GeneSet` records when known.
`has_gmt_file` identifies the physical file; a row uses `in_gmt_file` and
`gmt_entry`. Collection `n_sets` counts named sets and `n_genes` counts the
distinct union of genes. The historical crosswalk below records the earlier
mapping; follow the [current authoring guide](geneset-authoring.md) and
[converter behavior](../converter/README.md) for new exports.

## TL;DR

The lab's gene-set extractor (`flannick/dig-gene-set-extractors`) already emits structured
provenance for every gene set — a **CFDE provenance graph** (`geneset.provenance.json`) plus a
metadata sidecar (`geneset.meta.json`). Almost all of it maps cleanly onto **DAPPER**, the
NIH Dataset Attribution & Provenance Profile we're building. The one structural
addition DAPPER needed was a **`Set`** node — *a collection of members* — separate from
`Dataset`. This report is the field-by-field crosswalk, worked against a **real extraction**
(HuBMAP model HZ2), so we can decide whether to rebuild our gene-set provenance directly on the
DAPPER data model.

Everything below is backed by files in this repo:
`../dapper.yaml` (the extended model), `../examples/example_geneset_collection.yaml`
+ `example_geneset_graph.yaml` (the HZ2 gene set as DAPPER), and the source fixtures under
`../../tests/fixtures/geneset-hubmap-hz2/`.

## The one modeling decision: `Set` vs `Dataset`

`Dataset` in DAPPER already means `schema:Dataset` — *a set of data*. A gene set is a
different thing: *a set of genes*. Rather than overload `Dataset`, DAPPER now has a parent
**`Set`** node — grounded in **`prov:Collection`** ("an entity that provides a structure to some
constituents, which are themselves entities"), with members via **`prov:hadMember`** — and
**`GeneSet is_a Set`** as the first concrete subtype. Protein sets, variant sets, etc. extend
`Set` the same way later.

Crucially, `Set` and `Dataset` **share the same attribution/provenance/mirror slots** (a new
`ProvenancedResource` mixin), so a returned gene set is citable, fundable, publication-linked,
and **mirror-protected under the mirroring invariant** exactly like a dataset — no separate rules.

`GeneSet` asserts **no** ontology `class_uri`: no term for "gene set" resolves in OLS4 (the hits
are all SET-domain genes), so it grounds via `is_a: Set` (→ `prov:Collection`) plus a
`nih:source_standard: dig.geneset` annotation. This follows our standing prefix policy — assert a
CURIE only if it dereferences.

> **Nuance surfaced by the real data:** a dig.geneset "gene set" is usually a **library** of many
> named term→genes sets (HZ2 = **487** sets over **1747** unique genes, materialized as a `.gmt`).
> DAPPER now models this as `GeneSetCollection`, with `n_genes` (distinct union)
> and `n_sets` (named sets). Individual rows use GeneSet.

## Crosswalk 1 — `geneset.provenance.json` (CFDE Provenance Graph)

| dig.geneset element | DAPPER target | Notes |
|---|---|---|
| `ProvenanceGraph.nodes[]` / `edges[]` | graph of `Node` + `Edge` instances | DAPPER is a KG (Node/Edge skeleton) already |
| **BaseNode** `id` | `Node.id` | |
| BaseNode `type` (File/GeneSet/AnalysisType) | the DAPPER class itself | discriminator → `C2M2File` / `GeneSet` / `Activity` |
| BaseNode `name` | `Node.name` | |
| BaseNode `description` | `.description` | on `Set`/`Dataset`; added to `Set` |
| BaseNode `dcc_url`, `drc_url` | `PortalLinked.dcc_url` / `drc_url` | `schema:url` + `dcat:landingPage`; shared mixin |
| **FileNode** → | **`C2M2File`** | `dcat:Distribution` / `schema:DataDownload` |
| `c2m2_properties.filename` | `C2M2File.filename` | |
| `c2m2_properties.persistent_id` | `C2M2File.persistent_id` | |
| `c2m2_properties.local_id` | `C2M2File.local_id` | the `s3://` / `https://` URI |
| `c2m2_properties.size_in_bytes` | `C2M2File.size_in_bytes` | `dcat:byteSize` |
| `c2m2_properties._uuid` | `C2M2File.c2m2_uuid` | |
| `c2m2_properties.md5` | `C2M2File.md5` | base64 MD5 (`spdx:checksum`) |
| **GeneSetNode** → | **`GeneSet`** | see Crosswalk 2 for the gene_set fields |
| **AnalysisTypeNode** → | **`Activity`** (`prov:Activity`) | `AnalysisType` kept as a `Activity` alias |
| `analysis.script_url` | `Activity.script_url` | |
| `analysis.version` | `Activity.code_version` | git commit |
| `analysis.command` | `Activity.command` | canonical replay command |
| `analysis.observed_command` | `Activity.observed_command` | original runtime command |
| `analysis.environment.entrypoint` | `Activity.entrypoint` | |
| `analysis.environment.container_image` | `Activity.container_image` | |
| `analysis.environment.repo_url` | `Activity.repo_url` | |
| `analysis.parameters`, `analysis.environment.*` (structured) | `BioComputeObject` (`parametric_domain` / `execution_domain`) | reuse the existing class for full structured detail |
| `AnalysisTypeC2M2Properties.synonyms` | `Activity` `aliases` | |
| **Edge** `label` = `data input` | **`Used`** edge, `edge_role: data_input` (`prov:used`) | |
| **Edge** `label` = `metadata input` | **`Used`** edge, `edge_role: metadata_input` (`prov:used`) | role distinguishes it |
| **Edge** `label` = `data output` | **`WasGeneratedBy`** edge (`prov:wasGeneratedBy`) | |
| Edge `source` / `target` | `Edge.subject` / `Edge.object` | direction normalized to PROV |
| Edge `description` | — | Edge base carries none; can add if wanted |
| Edge `id` | — | Dock `Edge` base defines no `id`; edges are reified statements |

## Crosswalk 2 — `geneset.meta.json` (metadata sidecar)

| dig.geneset field | DAPPER target | Notes |
|---|---|---|
| `standard_name` = `dig.geneset` | `GeneSet` `nih:source_standard` annotation | |
| `standard_version`, `schema_version` | provenance annotations | |
| `created_at` | `Activity.generated_at_time` | |
| `geneset_id` | `GeneSet.id` | |
| `gene_set.id` / `name` / `description` | `GeneSet.id` / `name` / `description` | |
| `gene_set.assay` | `GeneSet.assay` | |
| `gene_set.data_type` | `GeneSet.data_type` | |
| `gene_set.organism` | `GeneSet.organism` | |
| `gene_set.genome_build` | `GeneSet.genome_build` | |
| `gene_set.n_genes` | `GeneSet.n_genes` | |
| `gene_set.primary_artifact` | a `C2M2File` (role) + `was_generated_by` | the `geneset.tsv` |
| `summary.n_sets_emitted` | `GeneSet.n_sets` | library size |
| `summary.n_genes` | `GeneSet.n_genes` | |
| `summary.n_input_features` / `n_features_assigned` / `fraction_features_assigned` | `Activity` params / summary annotation | extraction QC stats |
| `converter.name` / `version` | `Activity.name` / `code_version` | |
| `converter.code.{git_commit,repo_url,module,script_url}` | `Activity.code_version` / `repo_url` / `script_url` | |
| `converter.execution.{command,observed_command,entrypoint,container_image}` | `Activity.command` / `observed_command` / `entrypoint` / `container_image` | |
| `converter.parameters` | `BioComputeObject.parametric_domain` | structured detail |
| `input.{data_type,assay,organism,genome_build}` | `GeneSet` fields (redundant with gene_set) | |
| `input.files[].{path,local_path}` | `C2M2File.local_id` | |
| `input.files[].sha256` | `C2M2File.sha256` | added (`spdx:checksum`); MD5 lives on the same node |
| `input.files[].size_bytes` | `C2M2File.size_in_bytes` | |
| `input.files[].{canonical_uri,download_url,landing_page_url}` | `DrsObject.self_uri` / `dcc_url` / `drc_url` | public identity, when present |
| `input.files[].persistent_id` | `C2M2File.persistent_id` | |
| `input.files[].provider` / `version` | `C2M2File` / `Activity` | |
| `input.files[].license` | `License` + `has_license` | |
| `input.files[].access_level` | `access_level` (`AccessLevelEnum`) | `local_only` → private |
| `gene_annotation.{mode,gtf_path,source,gene_id_field}` | `Activity` params / `BioComputeObject` | annotation config |
| `weights.{weight_type,normalization,aggregation}` | `Activity` params / `BioComputeObject` | scoring method |
| `program_extraction.*` | `Activity` params / `BioComputeObject` | selection method |
| `output.files[]` | `C2M2File` output nodes + `WasGeneratedBy` | |
| `provenance.path` / `focus_node_id` | pointer to the graph / the `GeneSet.id` | |
| `gmt` | a `C2M2File` (the `.gmt`) + `GeneSet.members` reference | the member manifest |
| `lineage.{nodes,edges,processes}` | **redundant** with `geneset.provenance.json` | see note |

### Two provenance views in the source

`geneset.meta.json` contains its **own** `lineage` graph (file nodes/edges/`processes`) *in
addition to* the standalone `geneset.provenance.json`. In HZ2 the `meta.json.lineage` is
**flatter** — it collapses to a single `process:converter_invocation` and omits the upstream
`hubmap_asctb_augmented` workflow step, which only the standalone `geneset.provenance.json`
captures. DAPPER represents the **richer** graph (both Activities) once; the `lineage` block
would be a lossy duplicate. **Recommendation:** treat `geneset.provenance.json` as the source of
truth and drop the sidecar `lineage` on migration.

## "Mirror" is overloaded — the lab's is **rebasing**, not mirroring

Both codebases say "mirror," but they mean **unrelated** things. To keep the vocabulary clear we
reserve **"mirror" for DAPPER's governance rule** and rename the lab's mechanism to **rebasing**:

- **Rebasing** (the lab's current "mirror"): `--provenance_mirror_local_prefix` /
  `--provenance_mirror_remote_prefix` (`mirror_graph_payload`, `mirror_provenance_path`) rewrite a
  file's `local_id` / `dcc_url` / embedded command paths from a **local storage prefix**
  (`/humgen/diabetes2/.../HZ2/...`) onto a **public/remote prefix**
  (`s3://dig-gene-set-data/.../HZ2/...`) and recompute content-addressed IDs. That is
  **rebasing local references onto public URIs** — a publish-time transform. (Null in HZ2.)
- **Mirror** (DAPPER): `MirrorProvenance` + the `nih:mirror_mutable: false` invariant
  is a **governance rule** — a downstream cache MAY append its own provenance but MUST NOT rewrite
  authoritative NIH attribution.

They're complementary, not competing: rebasing is what produces the public URIs a mirror would
then cache. **Rebasing needs no new DAPPER structure** — the model already separates the two
identities a rebase moves between: `C2M2File.local_id` (local) vs. the public URI
(`DrsObject.self_uri` / `dcc_url`). Rebasing just chooses which populates which.

### Suggested rename for the lab (compatibility, not required)

To end the collision at the source, we'd suggest eventually renaming the lab's flags/functions
— `--provenance_mirror_local_prefix` → `--provenance_rebase_local_prefix`,
`mirror_graph_payload` → `rebase_graph_payload`, `mirror_provenance_path` → `rebase_path` — so
"mirror" is free to mean only DAPPER's rule across both projects. This is a suggestion; DAPPER
does not depend on it.

## What maps cleanly / what's new / open questions

**Clean (no new DAPPER structure):** the whole provenance DAG, C2M2 file identity, the analysis
commands/versions/env, gene-set descriptive fields, funding/citation/license/access, and the
edge semantics.

**New in DAPPER (this change):** `Set` (+ `GeneSet`, `C2M2File`), `PortalLinked` (`dcc_url`/
`drc_url`), the `Used` edge + `ProvEdgeRoleEnum`, extended `Activity` execution fields (+
`C2M2File.sha256`, `description`), and the `ProvenancedResource` mixin refactor (non-breaking —
`Dataset` still validates).

**Resolved modeling questions:** libraries use `GeneSetCollection`; named
sets use `GeneSet`. A collection can omit members when only library metadata
is available, or enumerate them completely when row data is available. The
HZ2 example retains its GMT reference without inventing row records.

**Open question:** drop sidecar `lineage` in favor of the standalone provenance
graph (recommended above)?

*(Resolved: `input.files[].sha256` now maps to a real `C2M2File.sha256` slot.)*

## Converter — turn this crosswalk into instances

The crosswalk is now executable: **`../converter/geneset_to_dapper.py`** reads
`geneset.provenance.json` (+ `geneset.meta.json`) and emits validated DAPPER instances — a full
graph doc (`<id>.dapper.yaml`) plus the focus `GeneSet` node. It takes a local file/dir **or an
`s3://` prefix** (batch), and `--overlay` injects the NIH attribution `dig.geneset` doesn't carry.

```bash
# one gene set, validated
uv run schema/converter/geneset_to_dapper.py \
  tests/fixtures/geneset-hubmap-hz2/ -o out/ --validate
# batch straight from S3
uv run schema/converter/geneset_to_dapper.py \
  s3://dig-gene-set-data/LINCS_L1000/ -o out/ --validate
```

Verified: the HZ2 fixture round-trips to the full DAG (9 `C2M2File`, 2 `Activity`, `Used` +
`WasGeneratedBy` edges), and two LINCS_L1000 gene sets convert + validate straight from S3 — so
the gene-set/KG work can point at DAPPER directly. See
[`../converter/README.md`](../converter/README.md).
