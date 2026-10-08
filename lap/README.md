# CFDE gene-set projection onto EAGGL factors (LAP)

This LAP pipeline projects every CFDE gene set onto the EAGGL mechanism factors. It uses the
supplied-factor projection in `packages/pigean`: `python -m eaggl factor --factor-gene-clusters-in …`.
For each (factor, gene set) pair it writes a **joint** and a **marginal** loading.

Its `betas_` stage scores every trait against every CFDE gene set from the trait's existing PIGEAN gene stats, with
`python -m pigean betas` (no PIGEAN rerun, no outer Gibbs). See [Gene-set betas](#gene-set-betas-betas_-stage).

Its `release_` stage then writes one **reference release** folder (factors, gene sets, projections, provenance and the
factor-label and DisMech context vectors; gene sets are not embedded), which one command publishes to the app's
environments. See
[Reference release](#reference-release-release_-stage) and `../docs/reference-release.md`.

**Inputs**

| Input | What | Size |
|---|---|---|
| CFDE gene sets | The DAPPER 0.2.0 release `s3://dig-gene-set-data/v2.0_dapper-0.2.0-a1/`, annotated snapshot `2026-10-05` (`/humgen/diabetes/users/chase/data/dig-s3/gene_sets/cfde/2026-10-05`) minus GaultonLab (`build_meta_yaml.py --exclude-library`; the meta key `cfde_libraries` lists the 9 kept) | 532 GMTs, 1,517,472 gene sets: LINCS_L1000 1,496,889; RummaGEO 10,332; GlyGen 6,875; IDG 1,240; GTEx 781; IMPC 536; HuBMAP 358; MetabolomicsWorkbench 295; MoTrPAC 166 |
| EAGGL factors | Capped loadings of the legacy 711-trait atlas (`raw/EAGGL_capped_union_graph_share/data`, unzipped from the share) | 4,037 factors × 18,477 genes |
| Trait ids | KPN trait registry **v0.0.2** from `dig-portal-data-models` | `versions/trait/v0.0.2/kpn_trait_registry.tsv`; tag commit `3cd554f` |
| PIGEAN gene stats | All-trait export of the mouse_msigdb PIGEAN runs the factors came from (`/humgen/diabetes2/users/chase/projects/pigean/raw/all_traits/mouse_msigdb/gene_stats.tsv`, meta key `pigean_gene_stats_file`) | 18 GB; `phenotype, gene, combined, log_bf, prior` for 6,698 phenotypes, all 711 traits among them |

**One projection per trait, from gene sets read once.** Each trait's factors are projected jointly on their own: 711
runs.
- `prep_pack_annotations_cmd` parses the 1.5M gene sets once into one binary 0/1 genes × gene-sets matrix
  (`*.cfde_annotations.{indptr,indices}.npy`). Every trait's projection maps that matrix, so no trait re-reads the
  GMTs.
- The projection is eaggl's, computed by `scripts/projection_kernel.py`. eaggl rebuilds two dense genes × gene-sets
  products at every update, and they take 98% of its time: 303 of 310 s for 5,000 gene sets of T2D. Both are fixed
  linear maps, so the kernel forms them once. The same 5,000 gene sets take 0.1 s, and a whole trait about a minute
  instead of ~3 h.
- `prep_check_projection_cmd` runs the pinned eaggl itself on sample gene sets of every library, plus ones with
  case-mapped genes, for `projection_check_traits` (T2D, 2hrG, Ap-LM). Every `%.4g` loading and top factor must be
  identical, and the per-trait projections wait for it.
- The gene sets are projected in chunks of `projection_chunk_gene_sets` (20,000; 76 chunks of the 1,517,472). Each
  chunk is projected as one eaggl run on it would be: its own random start and stopping rule. A gene set's loadings
  depend only on its own column and the trait's factors, up to that stopping tolerance.
- The all-factor run of the 44,399-set release (one eaggl run over all 4,037 factors) is not part of the pipeline any
  more.

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

Only letter case is harmonised: CFDE symbols such as `C10orf71` map to EAGGL's `C10ORF71` through `--gene-map-in`, and gene aliases are not remapped. The annotations GMT keeps every gene set's id and genes in GMT order but blanks the description column. DAPPER 0.2.0 fills that column with free text ("LINCS L1000 chemical perturbation Characteristic Direction signature", "na"), and eaggl and pigean split GMT lines on any whitespace, so they would read those words as genes. `collection-index` also refuses gene tokens that eaggl and pigean would misread (whitespace or `:`).

## Layout

```
config/cfde_projection.cfg          LAP pipeline
config/cfde_projection.meta.yaml    GENERATED by scripts/build_meta_yaml.py
config/cfde_projection.meta         GENERATED by meta-sanity from the .yaml (never hand-edit)
scripts/build_meta_yaml.py          index.tsv + factor_ids.tsv + KPN registry -> meta.yaml
scripts/projection_workflow.py      helper subcommands (stdlib only, Python 3.9)
scripts/factor_portal/              audit portal: build / export-audit / serve / html (stdlib only, Python 3.9)
scripts/tests/                      unit + end-to-end tests (real eaggl runs on tiny fixtures; release stage lint)
raw/  out/  log/                    inputs / LAP outputs / run logs (git-ignored)
```

**LAP classes**

- `project`: `eaggl_capped__cfde_2026_10_05`
- `geneset_collection` (532): the instance name is the CFDE label
- `trait` (711): the instance name is the EAGGL trait

**Commands, by stage** (the prefix selects the stage with `--only-cmd`)

| Stage | Commands |
|---|---|
| inputs | `prep_cfde_index_cmd` (the 9 libraries), `collection_index_cmd`, `prep_trait_kpn_map_cmd`, `prep_assemble_factors_cmd`, `prep_build_annotations_cmd`, `prep_pack_annotations_cmd` (the gene sets, read once), `prep_check_projection_cmd` (the kernel against eaggl) |
| per trait | `trait_factors_cmd`, `trait_project_cmd` (every chunk of the packed gene sets, then rank) |
| collect | `collect_projections_cmd` (fan-in over all 711 traits) |
| gene-set betas | `betas_index_cmd`, `betas_library_gmts_cmd` (project) → `betas_trait_gene_stats_cmd`, `betas_trait_run_cmd` and `betas_trait_run_large_cmd` (pigean betas, one fit per library), `betas_trait_annotate_cmd` |
| audit portal | `portal_build_db_cmd` (fan-in over all 711 traits) → `portal_export_audit_cmd`, `portal_build_html_cmd` |
| reference release | `release_build_cmd` (fan-in over all 711 traits; files only, publishing is by hand) |

## Outputs (`out/projects/eaggl_capped__cfde_2026_10_05/`)

| file | contents |
|---|---|
| `*.gene_set_index.tsv.gz` | `gene_set_id, gene_set_name, collection_id, cfde_label, library, partition, model, comparison, program, gmt_row, n_genes, n_genes_in_eaggl_universe, cfde_snapshot` |
| `*.factor_index.tsv` | `global_eaggl_column, factor_id, trait, kpn_trait_id, factor, factor_number, factor_label, n_nonzero_loadings, loading_l2, loading_variant` |
| `*.trait_kpn_map.tsv` | `trait, kpn_trait_id, kpn_release, kpn_release_commit, gwas_source_category, phenotype_name, trait_group, legacy_trait_group, trait_type, n_factors` |
| `traits/<trait>/<trait>.cfde_projection.long.tsv.gz` | **Main per-trait result.** For every gene set in some factor's per-library top 50 (joint or marginal), one row per factor: `trait, kpn_trait_id, factor_id, factor, factor_label, gene_set_id, collection_id, cfde_label, library, joint_loading, marginal_loading, joint_rank_in_factor, marginal_rank_in_factor, is_joint_top_factor, joint_rank_in_library, marginal_rank_in_library` (ranks over all 1.5M gene sets) |
| `traits/<trait>/<trait>.cfde_projection.top.tsv.gz` | The rows within that factor's own per-library top 50, plus `gene_set_name` |
| `*.cfde_annotations.indptr.npy`, `*.cfde_annotations.indices.npy` | The packed gene sets (CSC over the EAGGL genes in the factor tables' column order): gene set j holds the gene rows `indices[indptr[j]:indptr[j+1]]` |
| `*.cfde_annotations.chunks.tsv` | The projection chunks: `chunk, first_row, n_gene_sets, first_gene_set_id, last_gene_set_id, n_entries` |
| `*.projection_check.tsv`, `projection_check/` | The kernel against the pinned eaggl, per checked trait: `n_gene_sets, n_case_mapped_gene_sets, n_values, joint_mismatches, marginal_mismatches, top_factor_mismatches, kernel_updates, eaggl_seconds, kernel_seconds, pigean_commit, check_pass` (eaggl's outputs and the sample GMT are kept in the directory) |
| `traits/<trait>/<trait>.projection_qc.tsv` | `ids_equal_index`, `label_check_failures`, `k1_joint_marginal_max_absdiff`, `aligned_genes`, `seed`, `pigean_commit` (the checked eaggl), `qc_pass` |
| `*.projection_manifest.tsv` / `*.top_gene_sets_per_factor.tsv.gz` | All 711 QC rows / all top rows |
| `traits/<trait>/<trait>.projection.*` | Parameters, one log line per chunk (updates, last relative change) and warnings (chunks that stopped at 100 updates, as eaggl does) |
| `traits/<trait>/<trait>.cfde_gene_set_stats.tsv.gz` | **Trait → gene-set betas.** The gene sets PIGEAN analyzed: `trait, kpn_trait_id, gene_set_id, collection_id, cfde_label, library, n_genes, beta_uncorrected, beta, avg_postp, library_rank, response, p, sigma2` |
| `traits/<trait>/<trait>.pigean_gene_stats.tsv.gz` | The trait's slice of the gene stats |
| `traits/<trait>/<trait>.gene_set_stats.runs.tsv`, `*.gene_set_stats.large_runs.tsv`, `traits/<trait>/gene_set_stats/` | The trait's pigean fits, one per library: `library, status (fitted or no_gene_sets), n_gene_sets, gene_set_stats_file, params_file, log_file, warnings_file, response, pigean_commit, seconds`; the raw pigean outputs are `gene_set_stats/<library>.*` |
| `*.library_gmts.tsv`, `library_gmts/` | One GMT per library for the betas: `library, file, n_gene_sets, sha256` |

**About the loadings**
- Loadings are written as eaggl writes them (`%.4g`), and `prep_check_projection_cmd` requires them to equal eaggl's.
- Always join on `gene_set_id`, never on row position.
- The per-trait long file also stores the rank of each gene set within each factor.

## Running

Paths are relative to this `lap/` directory. The pipeline runs on UGER; start the long stages from a tmux session.
- The pack reads the annotations GMT once (a few minutes). The check runs eaggl on ~2,000 gene sets for each of its
  three traits (a few minutes each).
- A per-trait projection maps the pack and projects its 76 chunks in about a minute (8 GB requested).
- The gene-set betas fit each library separately: the 8 smaller libraries in about 1–2 minutes per trait (< 1 GB),
  LINCS in its own job (`gene_set_stats_large_mem`, 64 GB): T2D took 35 min and peaked at 56 GB, so all 711 traits
  are a few hundred CPU-hours; see [Gene-set betas](#gene-set-betas-betas_-stage).
- The release build reads the 9 GB of collection documents one at a time (32 GB requested).

```bash
alias lap_run='perl /humgen/diabetes/users/chase/lap/trunk/bin/run.pl --meta config/cfde_projection.meta'

# 0. regenerate the meta (after changing inputs or the generator; already done for eaggl_capped__cfde_2026_10_05)
python3 scripts/build_meta_yaml.py
PYTHONPATH=/humgen/diabetes2/users/chase/packages/meta-sanity python3 -m meta_sanity.generate_meta \
    config/cfde_projection.meta.yaml config/cfde_projection.meta

# 1. pinned eaggl code (once; raw/ is git-ignored)
git clone --no-checkout /humgen/diabetes2/users/chase/packages/pigean raw/pigean_ca59661
git -C raw/pigean_ca59661 checkout --detach ca59661644dc9ead429fc7e59050870ba49b26e8

# 2. link inputs and create directories (only --init links meta files; done for eaggl_capped__cfde_2026_10_05,
#    but rerun --mkdir for the projection_check/, library_gmts/ and traits/*/gene_set_stats/ directories)
lap_run --init && lap_run --mkdir && lap_run --check

# 3. inputs, the gene-set pack, the projection check and the project-level betas inputs (the gene-stats index and the
#    library GMTs). Never add --only here: the project-level fan-ins would shrink.
lap_run --only-cmd '^(prep_|collection_|betas_index_cmd|betas_library_gmts_cmd)' --bsub

# 4. smoke test (3 traits), then every trait: projections, gene-set betas, collect
lap_run --only '^(2hrG|T2D|Ap-LM)$' --only-cmd '^(trait_|betas_)' --bsub
lap_run --only-cmd '^(trait_|betas_|collect_)' --bsub

# 5. the release build (a cluster job; files only)
lap_run --only-cmd '^release_build_cmd$' --bsub
```

Publishing to the app is always a separate command run by hand ([Reference release](#reference-release-release_-stage)).

**Cluster behaviour**
- `max_sge_batch=1` turns off UGER job arrays. LAP otherwise bundles every ready command into one array with `-tc 1000`, and `max_jobs` counts an array as a single job. With arrays off, `max_jobs=50` caps the running tasks; raise it (in the cfg) if the cluster gives you more.


**Tests**

```bash
/humgen/diabetes2/users/chase/projects/pigean/.venv/bin/python -B -m unittest discover -s scripts/tests -v
```

The backend venv runs them too (`../services/backend/.venv/bin/python`). `test_release_stage.py` also checks that
every flag `release_build_cmd` passes exists in the backend CLI (it builds the CLI's parser with the backend venv;
nothing connects anywhere).

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

**Running.** A full LAP run builds the portal after `collect_projections_cmd`. To build it on its own, run the stage
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

1. `betas_index_cmd` (project, about 2 minutes) reads the 18 GB export once and records where each trait's rows are
   (they must be contiguous). It stores the export's size and mtime, and the next step refuses a changed export.
2. `betas_library_gmts_cmd` (project) reads the annotations GMT once and writes one GMT per library
   (`library_gmts/<library>.gmt.gz`, listed in `*.library_gmts.tsv` with their sha256).
3. `betas_trait_gene_stats_cmd` reads the trait's byte range: its `gene, combined, log_bf, prior` rows.
4. `betas_trait_run_cmd` and `betas_trait_run_large_cmd` run `pigean betas` once per library on that library's GMT,
   with the pinned clone (checked first) and the GWAS profile:
   - gene locations (not OLS), the NCBI37.3 gene universe and the portal gene map;
   - the marginal p < 0.01 prefilter and the 5,000-gene-set cap;
   - `--deterministic --seed 1`.

   `betas_trait_run_large_cmd` fits `gene_set_stats_large_libraries` (LINCS_L1000) as its own job with
   `gene_set_stats_large_mem`; `betas_trait_run_cmd` fits the other 8 libraries (about 1–2 minutes per trait). Each
   writes `beta_uncorrected` (marginal) and `beta` (joint, corrected for gene-set overlap without Gibbs) for every gene
   set PIGEAN analyzes into `traits/<trait>/gene_set_stats/<library>.*`, and a table of its runs. A rerun skips the
   libraries already fitted with the same inputs. A library where no gene set survives pigean's filters is recorded
   as `no_gene_sets`.
5. `betas_trait_annotate_cmd` keeps the analyzed gene sets (`filter_reason` `kept`; the prefiltered ones carry no
   beta), attaches the KPN and collection ids, ranks them by `beta_uncorrected` within their library, and records that
   library's learned `p` and `sigma2`. It refuses a missing or repeated library, another response, a gene set outside
   its run's library or another pigean commit.

**Response** (`gene_set_stats_response`, default `log_bf`):
- `log_bf`, the direct genetic support. This is a PIGEAN run on the CFDE gene sets minus the outer Gibbs loop.
- `combined` adds the export's mouse_msigdb priors, which come from another gene-set library. On the 2026-09-28
  release T2D analyzed 4,974 gene sets with it, against 1,197 with `log_bf`.

Only `log_bf` is the same across the export's libraries; `prior` and `combined` differ.

The release build reads every `<trait>.cfde_gene_set_stats.tsv.gz` into `trait_gene_sets.tsv.gz`, published as
`<prefix>_ref_trait_gene_sets`.

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
  - `check-projection` (before any projection) and `record-pigean-commit` (before each pigean run) check that the clone is at `pigean_commit` and that `src/` is clean.
  - The projection records the eaggl commit it was checked against, and the project, annotate, relabel and collect steps reject any other commit.
  - No branch has `scripts/run_eaggl_supplied_projection.sh`. The check calls `python -m eaggl factor` with the flags of the per-trait runs.
- **KPN:** trait ids come from `/humgen/diabetes/users/chase/packages/dig-portal-data-models` (`main`, fast-forwarded to include tag `v0.0.2`).
  - That release removed `versions/phenotype/v0.0.1`.
  - The pigean configs that still point there need to be repointed separately: `projects/pigean/config/{analyses,inferiority,validation}.meta.yaml` and their generated `.meta` files.
