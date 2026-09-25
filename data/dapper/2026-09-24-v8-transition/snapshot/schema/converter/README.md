# `geneset_to_dapper.py` — dig.geneset → DAPPER converter

Reads the lab's `dig.geneset` provenance (`geneset.provenance.json` + `geneset.meta.json`, from
`flannick/dig-gene-set-extractors`) and emits **validated DAPPER instances**. It's the
executable form of the crosswalk in
[`../docs/geneset-provenance-nih-dapp-adaptation.md`](../docs/geneset-provenance-nih-dapp-adaptation.md).

The script is self-contained (PEP 723 inline deps — just `pyyaml`); `uv` installs them on first
run. No changes to any project's dependencies.

## Usage

```bash
# a single extraction (sibling geneset.meta.json auto-discovered), validated
uv run schema/converter/geneset_to_dapper.py \
  tests/fixtures/geneset-hubmap-hz2/ -o out/ --validate

# a local tree — converts every geneset.provenance.json found
uv run schema/converter/geneset_to_dapper.py /path/to/runs/ -o out/

# straight from S3 (batch; reuses your existing AWS auth)
uv run schema/converter/geneset_to_dapper.py \
  s3://dig-gene-set-data/LINCS_L1000/ -o out/ --validate

# inject the NIH attribution dig.geneset doesn't carry
uv run schema/converter/geneset_to_dapper.py <input> -o out/ --overlay attribution.yaml
```

`<input>` is a `geneset.provenance.json` file, a local directory (walked recursively), or an
`s3://` URI/prefix (the `*.provenance.json` + `*.meta.json` sidecars are pulled with `aws s3`,
then converted).

### Output

Per extraction, into `-o OUT_DIR`:

| file | contents |
|------|----------|
| `<id>.dapper.yaml` | the full provenance graph — `files`, `c2m2_files`, `activities`, `gene_sets` / `gene_set_collections`, `used_edges`, `was_generated_by_edges` (empty groups are omitted) |
| `<id>.geneset_collection.yaml` | standalone `GeneSetCollection` for library exports (see `examples/example_geneset_collection.yaml`) |
| `<id>.geneset.yaml` | standalone `GeneSet` for an individual set; both focus-file variants receive `--overlay` attribution |

### Options

- `--validate` — `linkml-validate` every emitted node against `../dapper.yaml`; non-zero exit on any failure.
- `--overlay FILE` — YAML of NIH attribution (`has_creator`, `funded_by`, `is_described_by`,
  `has_recommended_citation`, …) merged onto the focus `GeneSet` or `GeneSetCollection`. Without it those authoritative
  fields are empty and the run logs which are missing (dig.geneset has no NIH attribution).
- `--schema PATH` — schema to validate against (defaults to `../dapper.yaml`).

## Mapping (summary)

| dig.geneset | DAPPER |
|---|---|
| `File` node + `c2m2_properties` | `C2M2File` (+ `sha256` from the metadata sidecar) |
| `File` node without nonempty `c2m2_properties` | `File` (generic fields directly on the node; `location` joins sidecar SHA-256) |
| `AnalysisType` node + `analysis{}` | `Activity` (command / observed_command / script_url / code_version / entrypoint / container_image) |
| `GeneSet` node with `summary.n_sets_emitted` or node `n_sets`, or explicit `GeneSetCollection` | `GeneSetCollection` (library metadata, `n_sets`, union `n_genes`) |
| `GeneSet` without a library count | `GeneSet` (individual set) |
| edge `data input` / `metadata input` | `Used` edge (`prov:used`, `edge_role`) |
| edge `data output` | `WasGeneratedBy` edge (`prov:wasGeneratedBy`) |

Library counts of zero or one still describe collections. If exactly one GMT
is emitted by the collection's generating activity, the converter adds
`has_gmt_file`. Multiple candidates are left unlinked rather than guessed.
This converter reads metadata, not GMT contents, so it does not invent per-row
GeneSets or selectors. See [gene-set authoring](../docs/geneset-authoring.md)
for explicit `in_gmt_file` / `gmt_entry` records and membership counts.

The converter retains source `c2m2_properties` as C2M2File metadata. It cannot
establish registration from a filename or path. The corrected HZ1 example
separately applies the requested decision to retain C2M2 metadata only on the GMT.

The multi-step DAG (multiple `AnalysisType` nodes) is handled generically — the converter
iterates nodes/edges, so an arbitrary provenance graph maps without special-casing.

The full field-by-field crosswalk, worked examples, and open questions are in the
[adaptation report](../docs/geneset-provenance-nih-dapp-adaptation.md).

## Not handled

- **NIH attribution** (creators / awards / publications / citation) — not present in dig.geneset;
  supply via `--overlay`.
- **Rebasing** (the lab's local→public path rewrite, formerly called "mirror") is orthogonal: it
  determines where bytes can be retrieved. Put storage paths and retrieval
  URIs in `File.location`; reserve `C2M2File.local_id` for a source identifier.
  The converter preserves supplied C2M2 local IDs rather than guessing whether
  they were intended as paths.
