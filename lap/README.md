# CFDE gene-set projection onto EAGGL factors (LAP)

This LAP pipeline projects every CFDE gene set onto the EAGGL mechanism factors, scores every trait against the gene
sets, and links every factor to every PIGEAN phenotype. It runs in five stages, each a prefix of its LAP commands
(`--only-cmd`), and every intermediate is a plain-text table you can open in the LAP web view:

| Stage | What it does | Per trait you can open |
|---|---|---|
| 1. `genesets_` | Indexes the release's collections, combines them into one GMT and gene-set index (the gene sets are read once), packs them for the projection and splits them per library for the betas | |
| 2. `factors_` | KPN trait ids, all factors in eaggl's layout, and each trait's factors | `<trait>.factors.tsv` (factor_id, gene, loading), `<trait>.factor_index.tsv` |
| 3. `projection_` | Checks the projection against the pinned eaggl, projects each trait, collects the QC | `<trait>.projection.tsv`, `<trait>.projection_top.tsv`, `<trait>.projection_qc.tsv`, `<trait>.projection.log` |
| 4. `betas_` | Slices each trait's PIGEAN gene stats and fits `python -m pigean betas` once per library (all but LINCS), then collects | `<trait>.gene_stats.tsv`, `<trait>.betas_runs.tsv`, `<trait>.betas_all.tsv`, `<trait>.betas.tsv` |
| 5. `linkage_` | Links every factor to every PIGEAN phenotype (6,698, not only the trait it was fitted on) with eaggl's trait linkage and factor-PheWAS, then q-values over all traits | `<trait>.factor_links.tsv`, `<trait>.factor_links_qc.tsv` |

Then `portal_` (the audit portal) and `release_` (one **reference release** folder, which one command publishes to the
app's environments; see [Reference release](#reference-release-release_-stage) and `../docs/reference-release.md`).
`lincs_` fits the LINCS betas; it is optional and not part of a full run (about 30 minutes per trait).

**Inputs**

| Input | What | Size |
|---|---|---|
| CFDE gene sets | The DAPPER 0.2.0 release `s3://dig-gene-set-data/v2.0_dapper-0.2.0-a1/`, annotated snapshot `2026-10-05` (`/humgen/diabetes/users/chase/data/dig-s3/gene_sets/cfde/2026-10-05`) minus GaultonLab (`build_meta_yaml.py --exclude-library`; the meta key `cfde_libraries` lists the 9 kept) | 532 GMTs, 1,517,472 gene sets: LINCS_L1000 1,496,889; RummaGEO 10,332; GlyGen 6,875; IDG 1,240; GTEx 781; IMPC 536; HuBMAP 358; MetabolomicsWorkbench 295; MoTrPAC 166 |
| EAGGL factors | Capped loadings of the legacy 711-trait atlas (`raw/EAGGL_capped_union_graph_share/data`, unzipped from the share) | 4,037 factors × 18,477 genes |
| Trait ids | KPN trait registry **v0.0.2** from `dig-portal-data-models` | `versions/trait/v0.0.2/kpn_trait_registry.tsv`; tag commit `3cd554f` |
| PIGEAN gene stats | All-trait export of the mouse_msigdb PIGEAN runs the factors came from (`/humgen/diabetes2/users/chase/projects/pigean/raw/all_traits/mouse_msigdb/gene_stats.tsv`, meta key `pigean_gene_stats_file`) | 18 GB; `phenotype, gene, combined, log_bf, prior` for 6,698 phenotypes, all 711 traits among them |

**One projection per trait, from gene sets read once.** Each trait's factors are projected jointly on their own: 711
runs, with `packages/pigean`'s supplied-factor projection (`python -m eaggl factor --factor-gene-clusters-in …`), joint
and marginal.
- `genesets_pack_cmd` parses the 1.5M gene sets once into one binary 0/1 genes × gene-sets matrix
  (`*.gene_sets.{indptr,indices}.npy`). Every trait's projection maps that matrix, so no trait re-reads the GMT.
- The projection is eaggl's, computed by `scripts/projection_kernel.py`. eaggl rebuilds two dense genes × gene-sets
  products at every update, and they take 98% of its time: 303 of 310 s for 5,000 gene sets of T2D. Both are fixed
  linear maps, so the kernel forms them once. The same 5,000 gene sets take 0.1 s, and a whole trait about a minute
  instead of ~3 h. On the 2026-09-28 release it reproduced all 179,238,763 of eaggl's values for the 711 traits.
- `projection_check_cmd` runs the pinned eaggl itself on sample gene sets of every library, plus ones with case-mapped
  genes, for `projection_check_traits` (T2D, 2hrG, Ap-LM). Every `%.4g` loading and top factor must be identical, and
  the per-trait projections wait for it.
- The gene sets are projected in chunks of `projection_chunk_gene_sets` (20,000; 76 chunks of the 1,517,472). Each
  chunk is projected as one eaggl run on it would be: its own random start and stopping rule. A gene set's loadings
  depend only on its own column and the trait's factors, up to that stopping tolerance.

**How the two loadings are computed**
- **Joint:** a fixed-W multiplicative-update NNLS of X ≈ W·Hᵀ (`eaggl/state.py:_project_H_with_fixed_W`).
  - H is clipped to [0, 1]. The run starts from a random H (seeded, `--seed 1`) and does at most 100 iterations.
  - The trait's factors compete to explain each gene set.
- **Marginal:** `clip(xᵀw / wᵀw, 0, 1)` per factor.
- A trait with a single factor (127 traits) gets joint = marginal exactly. Every such trait is checked for this.
- **Convergence limit:** eaggl stops the joint update at 100 iterations or when the relative change falls below 1e-4.
  Neither limit is a CLI option. The per-trait runs (K ≤ 15) meet the tolerance.

**Ranks and what is kept.** After the chunks, every factor ranks all 1.5M gene sets (ordinal, highest loading first,
ties by gene-set id): `*_rank_in_factor` over all of them, `*_rank_in_library` within the gene set's library. Writing
every (factor, gene set) row would be ~6 billion rows, so the per-trait long file keeps the gene sets that are in some
factor's per-library top `top_n_gene_sets` (50), joint or marginal, with that gene set's row for every factor of the
trait.

**Caveat when interpreting the loadings.** Loadings are not normalised for gene-set size, so very large gene sets score high on many factors. An example is MoTrPAC `t60-adrenal_Consensus_up`, which has 4,050 genes. Use `n_genes_in_eaggl_universe` in the gene-set index when interpreting results.

## Identifiers

Every id is carried through unchanged. The pipeline refuses to run if any link below cannot be made exactly.

| id | source | how it reaches the outputs |
|---|---|---|
| `dapper:GeneSet.*` | column 1 of each `genesets.dapper-ids.gmt` | Becomes eaggl's `Gene_Set`, unchanged. The names come from `genesets.gmt`, zipped row by row. |
| `dapper:GeneSetCollection.*` | CFDE `index.tsv`, stored as a meta prop | Joined through the gene-set index. Each linked YAML's name is checked against the id. |
| `trait::FactorN` (EAGGL factor) | `factor_ids.tsv` | eaggl names factors by position (`Factor1..K`). Each factor file's row order is recorded in a factor index and mapped back; eaggl's own `label` column is checked against that mapping. |
| `KPN.TRAIT:*` | KPN registry `legacy_phenotype_id` = EAGGL trait (exact, unique; 711/711: 546 `KPN`, 165 `rare_v2`) | Stored in `trait_kpn_map.tsv`, in the trait meta prop, and in the factor index. Also written to every long, top and QC file and to the manifest. The registry must be byte-identical to the release tag. |

Only letter case is harmonised: CFDE symbols such as `C10orf71` map to EAGGL's `C10ORF71` through `--gene-map-in`, and gene aliases are not remapped. The combined GMT keeps every gene set's id and genes in GMT order but blanks the description column. DAPPER 0.2.0 fills that column with free text ("LINCS L1000 chemical perturbation Characteristic Direction signature", "na"), and eaggl and pigean split GMT lines on any whitespace, so they would read those words as genes. `collection-index` also refuses gene tokens that eaggl and pigean would misread (whitespace or `:`).

## Layout

```
config/cfde_projection.cfg          LAP pipeline
config/cfde_projection.meta.yaml    GENERATED by scripts/build_meta_yaml.py
config/cfde_projection.meta         GENERATED by meta-sanity from the .yaml (never hand-edit)
scripts/build_meta_yaml.py          index.tsv + factor_ids.tsv + KPN registry -> meta.yaml
scripts/projection_workflow.py      helper subcommands (Python 3.9; numpy/scipy for the projection)
scripts/projection_kernel.py        eaggl's supplied-factor projection on the packed gene sets
scripts/factor_portal/              audit portal: build / export-audit / serve / html (stdlib only, Python 3.9)
scripts/tests/                      unit + end-to-end tests (real eaggl runs on tiny fixtures; release stage lint)
raw/  out/  log/                    inputs / LAP outputs / run logs (git-ignored)
```

**LAP classes**

- `project`: `eaggl_capped__cfde_2026_10_05`
- `geneset_collection` (532): the instance name is the CFDE label
- `trait` (711): the instance name is the EAGGL trait

**Commands, by stage**

| Stage | Commands |
|---|---|
| 1. gene sets | `genesets_cfde_index_cmd` (the 9 libraries), `genesets_collection_index_cmd` (per collection), `genesets_combine_cmd` (one GMT and gene-set index), `genesets_pack_cmd` (for the projection), `genesets_library_gmts_cmd` (for the betas) |
| 2. factors | `factors_kpn_map_cmd`, `factors_assemble_cmd` → `factors_trait_cmd` (per trait) |
| 3. projection | `projection_check_cmd` (the kernel against eaggl) → `projection_trait_cmd` (per trait) → `projection_collect_cmd` (fan-in over all 711 traits) |
| 4. gene-set betas | `betas_gene_stats_index_cmd` → `betas_trait_gene_stats_cmd`, `betas_trait_cmd` (per trait: a pigean fit per library, then ranked) → `betas_collect_cmd` (fan-in) |
| 5. factor-trait links | `linkage_phenotype_stats_cmd` (one pass over the export) → `linkage_trait_cmd` (per trait: one eaggl run; needs the betas stage's `<trait>.gene_stats.tsv`) → `linkage_collect_cmd` (fan-in) |
| optional | `lincs_trait_betas_cmd` (per trait: the LINCS fit) |
| audit portal | `portal_build_db_cmd` (fan-in over all 711 traits) → `portal_export_audit_cmd`, `portal_build_html_cmd` |
| reference release | `release_build_cmd` (fan-in over all 711 traits; files only, publishing is by hand) |

## Outputs (`out/projects/eaggl_capped__cfde_2026_10_05/`)

Everything is plain text except the linked source loadings and the `.npy` pack.

| file | contents |
|---|---|
| `*.cfde_index.tsv`, `collections/<label>/<label>.gene_sets.tsv` | The release's collections; each collection's gene sets |
| `*.gene_sets.gmt` | All gene sets as one GMT: id, a blank description, the genes in GMT order |
| `*.gene_set_index.tsv` | `gene_set_id, gene_set_name, collection_id, cfde_label, library, partition, model, comparison, program, gmt_row, n_genes, n_genes_in_eaggl_universe, cfde_snapshot` (1.5M rows) |
| `*.gene_overlap_report.tsv`, `*.cfde_to_eaggl_case.gene.map` | Gene overlap with the EAGGL factors per library; the case-only gene map |
| `*.gene_sets.indptr.npy`, `*.gene_sets.indices.npy`, `*.projection_chunks.tsv` | The packed gene sets (CSC over the EAGGL genes in the factor tables' column order: gene set j holds the gene rows `indices[indptr[j]:indptr[j+1]]`) and the projection chunks |
| `*.library_gmts.tsv`, `library_gmts/<library>.gmt` | One GMT per library for the betas: `library, file, n_gene_sets, sha256` |
| `*.trait_kpn_map.tsv` | `trait, kpn_trait_id, kpn_release, kpn_release_commit, gwas_source_category, phenotype_name, trait_group, legacy_trait_group, trait_type, n_factors` |
| `*.all_factors.factors_by_genes.tsv`, `*.factor_index.tsv` | All factors in eaggl's layout (header `Factor`, one column per gene); `global_eaggl_column, factor_id, trait, kpn_trait_id, factor, factor_number, factor_label, n_nonzero_loadings, loading_l2, loading_variant` |
| `traits/<trait>/<trait>.factors.tsv`, `<trait>.factor_index.tsv` | The trait's nonzero loadings `factor_id, gene, loading` (text unchanged); its `Factor1..K` order |
| `*.projection_check.tsv`, `projection_check/` | The kernel against the pinned eaggl, per checked trait: `n_gene_sets, n_case_mapped_gene_sets, n_values, joint_mismatches, marginal_mismatches, top_factor_mismatches, kernel_updates, eaggl_seconds, kernel_seconds, pigean_commit, check_pass` (eaggl's inputs and outputs are kept in the directory) |
| `traits/<trait>/<trait>.projection.tsv` | **Main per-trait result.** For every gene set in some factor's per-library top 50 (joint or marginal), one row per factor: `trait, kpn_trait_id, factor_id, factor, factor_label, gene_set_id, collection_id, cfde_label, library, joint_loading, marginal_loading, joint_rank_in_factor, marginal_rank_in_factor, is_joint_top_factor, joint_rank_in_library, marginal_rank_in_library` (ranks over all 1.5M gene sets) |
| `traits/<trait>/<trait>.projection_top.tsv` | The rows within that factor's own per-library top 50, plus `gene_set_name` |
| `traits/<trait>/<trait>.projection_qc.tsv`, `<trait>.projection.log` | `ids_equal_index, label_check_failures, k1_joint_marginal_max_absdiff, aligned_genes, seed, pigean_commit` (the checked eaggl), `n_chunks, most_updates, chunks_at_max_updates, qc_pass`; one log line per chunk (updates, last relative change) |
| `*.projection_manifest.tsv`, `*.projection_top.tsv` | All 711 QC rows; all traits' top rows |
| `*.gene_stats_index.tsv`, `traits/<trait>/<trait>.gene_stats.tsv` | Where each trait's rows are in the PIGEAN export; the trait's slice: `phenotype, gene, combined, log_bf, prior` |
| `traits/<trait>/<trait>.betas_runs.tsv` | One row per library fit: `trait, kpn_trait_id, library, status (fitted or no_gene_sets), n_gene_sets, n_kept, n_nonzero_beta_uncorrected, p, sigma2, response, pigean_commit, seconds` and pigean's own files (in `betas/`) |
| `traits/<trait>/<trait>.betas_all.tsv` | Every gene set pigean read, with its `filter_reason` and betas: `library` plus pigean's columns |
| `traits/<trait>/<trait>.betas.tsv` | **Trait → gene-set betas.** The gene sets PIGEAN analyzed: `trait, kpn_trait_id, gene_set_id, collection_id, cfde_label, library, n_genes, beta_uncorrected, beta, avg_postp, library_rank, response, p, sigma2` |
| `*.betas_manifest.tsv` | Every trait × library fit: `trait, kpn_trait_id, library, status, n_gene_sets, n_kept, n_nonzero_beta_uncorrected, p, sigma2, seconds` |
| `traits/<trait>/<trait>.lincs_betas_runs.tsv`, `<trait>.lincs_betas.tsv` | Optional: the LINCS fit and its ranked betas |
| `*.linkage_gene_phewas_stats.tsv`, `*.linkage_phenotypes.tsv`, `*.pigean_to_eaggl_case.gene.map` | The export's rows eaggl links (`combined` > 1, one per gene and phenotype); every phenotype: `phenotype, kpn_trait_id, kpn_match (legacy_phenotype_id, pigean_id or none), gwas_source_category, phenotype_name, is_anchor, n_genes, n_genes_kept`; the case-only gene map |
| `traits/<trait>/<trait>.factor_links.tsv` | **Factor → every phenotype.** One row per factor and phenotype: `trait, kpn_trait_id, factor_id, factor_label, phenotype, phenotype_kpn_trait_id, phenotype_name, is_own_trait, is_atlas_trait, nnls_loading, cosine_loading, phewas_beta, phewas_se, phewas_z, phewas_p, phewas_p_onesided` (each factor's rows by one-sided p); eaggl's own files are in `linkage/` |
| `traits/<trait>/<trait>.factor_links_qc.tsv`, `*.linkage_manifest.tsv` | `n_factors, n_phenotypes, n_rows, n_genes, n_genes_with_stats` (factor genes with anchor stats), the factor-PheWAS model, `pigean_commit, eaggl_seconds`; all traits |
| `*.factor_links.tsv`, `*.factor_links_summary.tsv` | The links with q ≤ `linkage_max_q` (0.05; Benjamini-Hochberg over every factor-phenotype test of every trait), with `phewas_q`; per factor: `n_phenotypes, n_linked, n_linked_kpn_traits, n_linked_atlas_traits` (other phenotypes), `own_trait_q, top_linked` |

**About the loadings**
- Loadings are written as eaggl writes them (`%.4g`), and `projection_check_cmd` requires them to equal eaggl's.
- Always join on `gene_set_id`, never on row position.

## Running

Paths are relative to this `lap/` directory. The pipeline runs on UGER; start it from a tmux session.
- **Gene sets:** combining the 532 GMTs and packing them take a few minutes each.
- **Projection:** the check runs eaggl on ~2,000 gene sets for each of its three traits (a few minutes each). Each
  trait then maps the pack and projects its 76 chunks in about a minute (8 GB requested).
- **Betas:** each trait's 8 library fits take a minute or two and under 1 GB. The optional LINCS fit takes ~30 min
  (24 GB requested), almost all of it reading the 1.5M signatures.
- **Links:** after the betas (each trait reads its `<trait>.gene_stats.tsv`). The filter reads the 18 GB export once
  (about 10 min, 40 MB). Each trait's eaggl run then takes a few minutes and up to ~4 GB (8 GB requested).
- **Release:** the build reads the 9 GB of collection documents one at a time (32 GB requested).

```bash
alias lap_run='perl /humgen/diabetes/users/chase/lap/trunk/bin/run.pl --meta config/cfde_projection.meta'

# 0. regenerate the meta (after changing inputs or the generator; already done for eaggl_capped__cfde_2026_10_05)
python3 scripts/build_meta_yaml.py
PYTHONPATH=/humgen/diabetes2/users/chase/packages/meta-sanity python3 -m meta_sanity.generate_meta \
    config/cfde_projection.meta.yaml config/cfde_projection.meta

# 1. pinned eaggl code (once; raw/ is git-ignored)
git clone --no-checkout /humgen/diabetes2/users/chase/packages/pigean raw/pigean_ca59661
git -C raw/pigean_ca59661 checkout --detach ca59661644dc9ead429fc7e59050870ba49b26e8

# 2. link inputs and create directories (--init links the meta's input files)
lap_run --init && lap_run --mkdir && lap_run --check

# 3. the stages in order (never add --only: the fan-ins over collections and traits would shrink)
lap_run --only-cmd '^(genesets_|factors_)' --bsub
lap_run --only-cmd '^projection_' --bsub
lap_run --only-cmd '^betas_' --bsub
lap_run --only-cmd '^linkage_' --bsub
lap_run --only-cmd '^(portal_|release_)' --bsub

# or everything but the optional LINCS betas in one run
lap_run --skip-cmd '^lincs_' --bsub

# optional: the LINCS betas
lap_run --only-cmd '^lincs_' --bsub
```

Publishing to the app is always a separate command run by hand ([Reference release](#reference-release-release_-stage)).

**Cluster behaviour**
- `max_sge_batch=1` turns off UGER job arrays. LAP otherwise bundles every ready command into one array with `-tc 1000`, and `max_jobs` counts an array as a single job. With arrays off, `max_jobs=80` caps the running tasks; raise it (in the cfg) if the cluster gives you more.
- Long jobs (only the optional LINCS fits) may hold at most `max_long_jobs=50` of them, so the short per-trait jobs always have slots.
- LAP resubmits a job that UGER killed for memory with twice the memory.

**Tests**

```bash
/humgen/diabetes2/users/chase/projects/pigean/.venv/bin/python -B -m unittest discover -s scripts/tests -v
```

The backend venv runs them too (`../services/backend/.venv/bin/python`). `test_release_stage.py` also checks the cfg:
every command belongs to a stage, no output is gzipped, every helper flag exists, and every flag `release_build_cmd`
passes exists in the backend CLI (it builds the CLI's parser with the backend venv; nothing connects anywhere).

**LAP operating notes**
- Write `\$` in cfg commands wherever the shell needs a literal `$`.
- Delete stale `.error` and `.started` markers before relaunching failed jobs.
- Use `--update-cmd-key` for cosmetic command edits. Otherwise all 711 traits re-run.
- In command text, a `$dir_key` whose value contains `@instance` keeps the placeholder (`*file_key` too).
  Write directories as `!{key::dir_key}` or `!{key::file_key:dir}`.
- `!{input:file_key}` (no prefix field) is a dependency that is not written into the command.
- `!{raw,,class,text,if_prop=prop:eq:value,allow_empty=1}` writes `text` only for matching instances.

## Audit portal (`portal_` stage)

A search-and-browse view of the projection, for checking that each factor's gene sets make sense. It is modelled on
the PIGEAN results portal (`python -m pigean.portal`, used by `projects/pigean/config/inferiority.cfg`): LAP builds a
SQLite file and a static page, and you start a small read-only server yourself. The code is
`scripts/factor_portal/` (standard library only, run with the helpers' Python).

**What it shows**
- **Search** (press `/`): traits by name, EAGGL id or `KPN.TRAIT` id; factors by label or id; gene sets by name or
  `dapper:GeneSet` id; genes by symbol.
- **Trait:** KPN metadata and ontology mappings, the projection QC row, every factor, and a heatmap of the top gene
  sets across the trait's factors (joint or marginal, with ranks).
- **Factor:** sibling factors as tabs, the audit columns and flags, and EAGGL's own top genes and gene sets from the
  factorization, for comparison with:
  - the **projected CFDE gene sets**: joint and marginal loadings with ranks, whether this factor has the trait's
    highest joint loading (★), set size, overlap with the factor's loaded genes, fold enrichment, a hypergeometric
    −log₁₀ p, the overlapping genes and hub status. Filters cover library and hubs; a joint-vs-marginal scatter
    sits below the table;
  - the **gene loadings**: every nonzero EAGGL loading as projected, its share of the factor's loading mass, how many
    of the factor's top gene sets contain the gene and how many factors load it.
- **Gene set on a factor** (click a gene set): each member gene's loading and its exact contribution to the marginal
  loading, the members without a loading, the members outside the EAGGL gene universe, and the gene set's joint and
  marginal loadings on every factor of the trait. That last table shows which sibling factor took the gene set in the
  joint solve.
- **Gene set** and **gene** pages: every factor whose top lists contain the gene set; every factor loading the gene.
- **Overview:** counts, the marginal check, flag definitions and counts, library representation in the top lists,
  hub gene sets and trait groups. **Factors** is the full audit table, sortable and filterable by flag.

**Why the gene contributions are exact.** The marginal loading is `clip(xᵀw / wᵀw, 0, 1)` for a 0/1 membership
vector x, so each member gene g adds exactly `w_g / wᵀw`. `build` recomputes all 278,272 stored marginal loadings this
way from the factors-by-genes table eaggl projected, and refuses to write the database if any differs by more than
eaggl's `%.4g` rounding (1.5e-4). The current run's largest difference is 5e-5. The joint loading has no per-gene
split, because the trait's factors compete for each gene set.

**Audit flags** (`build --threshold-*` overrides; values are stored in the database and shown in the UI) mark the tail
of each distribution for review. They are prompts, not verdicts. The default counts, out of 4,037 factors:

| flag | rule (top-N = 50) | factors |
|---|---|---|
| `few_genes` | fewer than 25 genes carry a nonzero loading | 202 |
| `weak_joint` | best joint loading below 0.15 | 36 |
| `joint_marginal_disagree` | under 25% of the joint top-N is also in the marginal top-N | 190 |
| `sibling_explained` | under 50% of the joint top-N load highest on this factor among the trait's factors | 109 |
| `hub_heavy` | at least 50% of the joint top-N are hub gene sets (in the joint top-N of ≥ 40 factors; 522 such sets) | 240 |
| `weak_overlap` | median fold enrichment of the joint top-N's overlap with the loaded genes below 2 | 435 |

1,033 factors carry at least one flag. Hubs are worth a look first. The four biggest are large MoTrPAC consensus
sets, the size effect described above: `t60-adrenal_Consensus_up` (3,863 genes in the EAGGL universe) is in the joint
top 50 of 2,249 factors, and the colon, white-adipose and brown-adipose sets in 956–1,354. Overlaps are modest in general: the median joint top-50 row shares 12 of ~240 member genes with the
factor (fold 3.4, p ≈ 3e-4).

**Outputs** (`out/projects/<project>/portal/`)

| file | contents |
|---|---|
| `*.portal.sqlite` | the portal database (~330 MB; 2.0 M sibling loadings) |
| `*.portal.factor_audit.tsv` | one row per factor: audit columns, top joint and marginal gene sets, flags |
| `*.portal.gene_set_audit.tsv` | every gene set with the number of factors and traits whose top lists contain it, and `is_hub` |
| `*.portal.html` | the UI as a static page that calls `$portal_api_url` (default `http://localhost:8766`) |
| `*.portal.build.log` | build log |

**Running.** A full LAP run builds the portal after `projection_collect_cmd`. To build it on its own, run the stage
without `--only`, because the build is a fan-in over all traits:

```bash
lap_run --only-cmd '^portal_' --bsub
```

The build takes ~11 min, ~8 of them reading the 711 long files, and peaks at ~0.7 GB. Then serve the database, in tmux:

```bash
env PYTHONPATH=scripts /humgen/diabetes2/users/chase/projects/pigean/.venv/bin/python -m factor_portal serve \
    --db out/projects/eaggl_capped__cfde_2026_10_05/portal/eaggl_capped__cfde_2026_10_05.portal.sqlite --port 8766
```

From your laptop, run `ssh -L 8766:localhost:8766 <this host>` and open `http://localhost:8766/`. Alternatively, open
the static `*.portal.html`: it calls the same URL. The server binds to 127.0.0.1 and is read-only. It has no
authentication, so keep it behind the tunnel. The database is opened immutable; restart the server after a rebuild.

## Gene-set betas (`betas_` stage)

Every trait gets a PIGEAN score for every CFDE gene set **without rerunning PIGEAN**. Each trait's existing gene-level
statistics go into `python -m pigean betas`: PIGEAN's gene-set stage on its own, which stops before the outer Gibbs
loop. The gene sets are the CFDE gene sets the projection uses, **fitted one library at a time**.

**Why per library.** A pigean fit learns one prior p and keeps at most 5,000 gene sets, so in one pooled fit LINCS
(98.6% of the gene sets) crowds out the rest. For T2D:

| | pooled (all 9 libraries) | per library |
|---|---|---|
| analyzed (`kept`) | 5,000: LINCS 3,937, RummaGEO 983, the other 7 libraries 80 | each library's own prefilter survivors |
| learned p | 1.15e-5 | RummaGEO 1.25e-3; libraries with < 100 survivors keep the default 1e-3 |
| nonzero `beta_uncorrected` | 163 (RummaGEO 24, GTEx 2) | RummaGEO 778, GTEx 14 of 14 |
| run | 35 min, 58.5 GB peak | RummaGEO 30 s, GTEx 7 s (< 0.5 GB); LINCS alone 35 min, 56 GB peak |

1. `betas_gene_stats_index_cmd` (project, about 2 minutes) reads the 18 GB export once and records where each trait's
   rows are (they must be contiguous). It stores the export's size and mtime, and the next step refuses a changed
   export.
2. `betas_trait_gene_stats_cmd` writes the trait's byte range: its `gene, combined, log_bf, prior` rows
   (`<trait>.gene_stats.tsv`).
3. `betas_trait_cmd` runs `pigean betas` once per library on that library's GMT (`genesets_library_gmts_cmd` wrote
   them), for every library but `lincs_libraries`, with the pinned clone (checked first) and the GWAS profile:
   - gene locations (not OLS), the NCBI37.3 gene universe and the portal gene map;
   - the marginal p < 0.01 prefilter and the 5,000-gene-set cap;
   - at most `gene_set_stats_max_initial` (20,000) gene sets, the most significant, enter pigean's pruning and betas
     (`--max-num-gene-sets-initial`; only LINCS ever reaches it);
   - `--deterministic --seed 1`.

   It then keeps the gene sets PIGEAN analyzed (`filter_reason` `kept`; the prefiltered ones carry no beta), attaches
   the KPN and collection ids, ranks them by `beta_uncorrected` within their library and records the `p` and `sigma2`
   that library's fit used: learned, or pigean's default (p = 1e-3) when fewer than 100 gene sets survive its filters. It writes `<trait>.betas_runs.tsv` (one row per library: status, kept, nonzero, p,
   sigma2), `<trait>.betas_all.tsv` (every gene set pigean read, with its `filter_reason`) and `<trait>.betas.tsv`.
   pigean's own files stay in `traits/<trait>/betas/`; a rerun skips the libraries already fitted with the same inputs.
   A library where no gene set survives pigean's filters is recorded as `no_gene_sets`. It refuses another response, a
   gene set outside its run's library or another pigean commit. About a minute or two per trait, under 1 GB.
4. `betas_collect_cmd` (fan-in) writes `*.betas_manifest.tsv`, every trait × library fit, and refuses a missing trait or
   library.

**LINCS (optional, `lincs_trait_betas_cmd`).** The same fit on LINCS alone writes `<trait>.lincs_betas_runs.tsv` and
`<trait>.lincs_betas.tsv`; the release build does not read them. Without the cap, LINCS's prefilter kept 50,000 to
427,000 of its 1.5M signatures depending on the trait, and the fits took 40 minutes to 2.3 hours and up to 160 GB
(UGER killed them). With it, T2D took 29 minutes at ~10 GB and HospC19vAll (killed above 140 GB uncapped) 41 minutes
at ~6 GB; nearly all of that is reading the signatures. The capped fit is an approximation of the uncapped one. For
T2D it learns nearly the same p (1.18e-5 against 1.12e-5) and the betas of gene sets both runs analyze agree (Pearson
0.9999 for `beta_uncorrected`), but only a third of the 5,000 gene sets it analyzes are the uncapped run's (56 of the
top 100 by `beta_uncorrected`).

**Response** (`gene_set_stats_response`, default `log_bf`):
- `log_bf`, the direct genetic support. This is a PIGEAN run on the CFDE gene sets minus the outer Gibbs loop.
- `combined` adds the export's mouse_msigdb priors, which come from another gene-set library. On the 2026-09-28
  release T2D analyzed 4,974 gene sets with it, against 1,197 with `log_bf`.

Only `log_bf` is the same across the export's libraries; `prior` and `combined` differ.

The release build reads every `<trait>.betas.tsv` into `trait_gene_sets.tsv.gz`, published as
`<prefix>_ref_trait_gene_sets`.

## Factor-trait links (`linkage_` stage)

Each factor is fitted on one trait, its anchor, and until this stage it was linked to that trait alone. This stage
links every factor to **every phenotype of the PIGEAN export**: the 711 traits and 5,987 more (2,531 GWAS Catalog,
1,413 rare diseases and 2,043 portal traits), 6,178 of them with a KPN trait id. It uses the pinned eaggl's own
projection-only modes, run once per trait on the trait's factors:

- **Trait linkage** (`--trait-factor-links-out`), eaggl's primary annotation layer. Each phenotype's `combined` support
  becomes a probability (background prior 0.05) and is projected onto the trait's factors with the fixed-W NNLS of the
  gene-set projection: `nnls_loading` (and `cosine_loading`, its share of the phenotype's loadings). It is
  descriptive, with no p-value.
- **Factor-PheWAS** (`--run-factor-phewas`, default `marginal_anchor_adjusted_binary`). For each factor and phenotype,
  an OLS of the phenotype's hits (genes with `combined` > 1) on the factor's gene loadings, adjusted for the anchor
  trait's direct support (its `log_bf`), with robust (HC3) standard errors. It asks whether the phenotype is enriched
  in the factor beyond the anchor's own genes: `phewas_beta, phewas_se, phewas_z, phewas_p, phewas_p_onesided`.

1. `linkage_phenotype_stats_cmd` (project; 11 minutes, 50 MB) reads the 18 GB export once and keeps the rows eaggl
   reads: `combined` > `linkage_min_combined` (1, eaggl's `--trait-linkage-threshold` and hit cutoff), with `combined`,
   `log_bf` and `prior` all numbers. eaggl's reader skips any other row (47,649 such rows above the cutoff); the
   export's prior-only genes have `log_bf` NA. That keeps 1,382,780 of 283,667,679 rows (74 MB). It also lists every
   phenotype with its KPN id (`legacy_phenotype_id` as for the anchors, else the registry's `pigean_id`).
   - **Why a filtered file.** eaggl keeps only these rows anyway (0.5% of the export). When its read has dropped rows,
     its factor-PheWAS re-reads the whole file once per 300 phenotypes: about 24 passes over 18 GB per trait. Given
     only these rows, it reuses what it read.
   - **The same results.** For T2D on 150 traits, eaggl given every row of those traits (and re-reading them) and
     eaggl given the filtered rows wrote byte-identical trait links and factor-PheWAS statistics. That holds once the
     rows eaggl's first read skips are left out; its re-read would count those prior-only rows as hits.
   - **Gene symbols.** The export spells 235 EAGGL genes in another case (`C10orf105` for `C10ORF105`). A case-only gene
     map (`--gene-map-in`, as for the CFDE gene sets) renames them. Some rare-disease phenotypes list both spellings,
     the upper-case one with the disease's direct support; a phenotype keeps one row per gene, the exact spelling first.
     72 factor genes (older symbols such as `AARS`) are not in the export.
2. `linkage_trait_cmd` (per trait; needs the betas stage's `<trait>.gene_stats.tsv`) runs eaggl on the trait's
   factors (rebuilt in eaggl's layout from `<trait>.factors.tsv`). It passes the filtered rows, and the trait's own
   gene stats on the factor genes for the anchor adjustment; eaggl would add any other gene and then refuse the factor
   basis. A factor gene without stats gets eaggl's fill, the mean `log_bf`. It writes `<trait>.factor_links.tsv`: one
   row per factor and phenotype, with the factor id and label, the phenotype's KPN id and name, `is_own_trait` and
   `is_atlas_trait`. Each factor's rows are sorted by one-sided p, and eaggl's own files are kept in `linkage/`. T2D (13
   factors × 6,697 phenotypes) took 258 s at a 3.8 GB peak, mostly the dense 18,477 × 6,697 trait linkage.
3. `linkage_collect_cmd` (fan-in) gives every factor-phenotype test of every trait a Benjamini-Hochberg q-value (one
   pass of p-values, one of rows). It writes the links with q ≤ `linkage_max_q` (0.05) and one summary row per factor:
   - `n_linked`;
   - `n_linked_kpn_traits` and `n_linked_atlas_traits` (other phenotypes);
   - `own_trait_q`: the anchor itself, tested beyond its own direct support;
   - `top_linked`.

**How many.** For T2D alone (the collect run on T2D's file), q ≤ 0.05 keeps 14,617 of 87,061 tests. Each factor
links to 464-1,910 phenotypes, 2,344 in all, which are 2,306 KPN traits besides T2D. A factor's strongest links are
related traits; for example "Lipid Metabolism Regulation" links to polyunsaturated fatty acids and "Behavioral Response
Mechanisms" to chronotype. A stricter q or an `nnls_loading` floor gives shorter lists; every test stays in the
per-trait files.

Neither the release build nor the portal reads these files yet.

## Reference release (`release_` stage)

The app's reference data is published as one release folder: traits, factors and their gene loadings, CFDE
collections and gene sets, per-library top projections, DAPPER provenance (nodes and edges), frozen factor
snapshots and the vectors. Design, file formats and the one-time migration from the old reload:
[../docs/reference-release.md](../docs/reference-release.md).

**Build** (`release_build_cmd`, a project-level cluster job with 32 GB; files only). It writes
`out/projects/<project>/release/files/` and the JSON result `<project>.release.json`. It is a fan-in over all 711
traits, so never run it under `--only`:

```bash
lap_run --only-cmd '^release_build_cmd$' --bsub
```

It reads the LAP outputs, the per-trait long files (per-library ranks), the per-trait gene-set betas, the collection
YAMLs and the vector cache `raw/reference_vector_cache.sqlite` (factor-label and DisMech context vectors keyed by the
sha256 of their text, seeded once from Aurora with `../scripts/reference_migration.py export-vectors`). Gene sets are
not embedded. Only factor labels missing from the cache are embedded, with `EMBEDDING_SERVICE_URL` from the repository
`.env`.

**Publish** (by hand, from this host; about 5 minutes per environment):

```bash
../services/backend/.venv/bin/python -m reveal_backend.reference_release publish \
    --release out/projects/eaggl_capped__cfde_2026_10_05/release/files --env local --env qa --env prod
```

For each environment it loads that environment's `<prefix>_ref_*` tables (`reveal_workflow_local`,
`reveal_workflow_qa`, `reveal`), swaps them in with one `RENAME`, and syncs the fixed Upstash namespaces
`<env>-factors` and `<env>-contexts`. Running apps pick the new release up
within a few seconds. A re-run is safe, and publishing an earlier release folder rolls back.

## Environment and versions

- **eaggl and pigean:** run from a **pinned clone** of `packages/pigean` at `ca59661`: `raw/pigean_ca59661`, using its `src/` with the pigean project's venv (`/humgen/diabetes2/users/chase/projects/pigean/.venv`, Python 3.9), which also runs the helpers.
  - `check-projection` (before any projection) and `betas-trait` (before its pigean fits) check that the clone is at `pigean_commit` and that `src/` is clean.
  - The projection records the eaggl commit it was checked against, and the project, annotate, relabel and collect steps reject any other commit.
  - No branch has `scripts/run_eaggl_supplied_projection.sh`. The check calls `python -m eaggl factor` with the flags of the per-trait runs.
- **KPN:** trait ids come from `/humgen/diabetes/users/chase/packages/dig-portal-data-models` (`main`, fast-forwarded to include tag `v0.0.2`).
  - That release removed `versions/phenotype/v0.0.1`.
  - The pigean configs that still point there need to be repointed separately: `projects/pigean/config/{analyses,inferiority,validation}.meta.yaml` and their generated `.meta` files.
