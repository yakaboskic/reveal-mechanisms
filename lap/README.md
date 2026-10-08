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

**One projection per trait, in chunks.** Each trait's factors are projected jointly on their own: 711 runs.
- eaggl densifies its genes × gene-sets matrix, so each trait runs over chunks of at most
  `projection_chunk_gene_sets` (20,000) gene sets. That's 76 chunks of the 1,517,472.
- A gene set's loadings depend only on its own column and the trait's factors, so chunking does not change them.
- The chunks of a trait run one after another in one job, and a rerun skips the chunks already done.
- The all-factor run of the 44,399-set release (one eaggl run over all 4,037 factors) is not feasible at this size and
  is not part of the pipeline any more.

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

Only letter case is harmonised: 141 CFDE symbols such as `C10orf71` map to EAGGL's `C10ORF71` through `--gene-map-in`. The GMTs themselves stay byte-identical, and gene aliases are not remapped.

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

- `project`: `eaggl_capped__cfde_2026_09_28`
- `geneset_collection` (133): the instance name is the CFDE label
- `trait` (711): the instance name is the EAGGL trait

**Commands, by stage** (the prefix selects the stage with `--only-cmd`)

| Stage | Commands |
|---|---|
| inputs | `prep_cfde_index_cmd` (the 9 libraries), `collection_index_cmd`, `prep_trait_kpn_map_cmd`, `prep_assemble_factors_cmd`, `prep_build_annotations_cmd`, `prep_chunk_annotations_cmd` |
| per trait | `trait_factors_cmd`, `trait_project_cmd` (eaggl over every chunk, then merge and rank) |
| collect | `collect_projections_cmd` (fan-in over all 711 traits) |
| gene-set betas | `betas_index_cmd` (project) → `betas_trait_gene_stats_cmd`, `betas_trait_run_cmd` (pigean betas), `betas_trait_annotate_cmd` |
| audit portal | `portal_build_db_cmd` (fan-in over all 711 traits) → `portal_export_audit_cmd`, `portal_build_html_cmd` |
| reference release | `release_build_cmd` (fan-in over all 711 traits; files only, publishing is by hand) |

## Outputs (`out/projects/eaggl_capped__cfde_2026_09_28/`)

| file | contents |
|---|---|
| `*.gene_set_index.tsv.gz` | `gene_set_id, gene_set_name, collection_id, cfde_label, library, partition, model, comparison, program, gmt_row, n_genes, n_genes_in_eaggl_universe, cfde_snapshot` |
| `*.factor_index.tsv` | `global_eaggl_column, factor_id, trait, kpn_trait_id, factor, factor_number, factor_label, n_nonzero_loadings, loading_l2, loading_variant` |
| `*.trait_kpn_map.tsv` | `trait, kpn_trait_id, kpn_release, kpn_release_commit, gwas_source_category, phenotype_name, trait_group, legacy_trait_group, trait_type, n_factors` |
| `traits/<trait>/<trait>.cfde_projection.long.tsv.gz` | **Main per-trait result.** For every gene set in some factor's per-library top 50 (joint or marginal), one row per factor: `trait, kpn_trait_id, factor_id, factor, factor_label, gene_set_id, collection_id, cfde_label, library, joint_loading, marginal_loading, joint_rank_in_factor, marginal_rank_in_factor, is_joint_top_factor, joint_rank_in_library, marginal_rank_in_library` (ranks over all 1.5M gene sets) |
| `traits/<trait>/<trait>.cfde_projection.top.tsv.gz` | The rows within that factor's own per-library top 50, plus `gene_set_name` |
| `*.cfde_annotations.chunks.tsv`, `annotation_chunks/` | The eaggl X input in chunks: `chunk, file, first_row, n_gene_sets, first_gene_set_id, last_gene_set_id, sha256` |
| `traits/<trait>/<trait>.projection_qc.tsv` | `ids_equal_index`, `label_check_failures`, `k1_joint_marginal_max_absdiff`, `aligned_genes`, `seed`, `pigean_commit`, `qc_pass` |
| `*.projection_manifest.tsv` / `*.top_gene_sets_per_factor.tsv.gz` | All 711 QC rows / all top rows |
| `traits/<trait>/*.eaggl.*` | eaggl params (first chunk), the logs and warnings of every chunk. The per-chunk loadings are deleted once merged. |
| `traits/<trait>/<trait>.cfde_gene_set_stats.tsv.gz` | **Trait → gene-set betas.** The gene sets PIGEAN analyzed: `trait, kpn_trait_id, gene_set_id, collection_id, cfde_label, library, n_genes, beta_uncorrected, beta, avg_postp, library_rank, response, p, sigma2` |
| `traits/<trait>/<trait>.pigean_gene_stats.tsv.gz`, `*.gene_set_stats.*` | The trait's slice of the gene stats; raw pigean output, params, log and warnings |

**About the loadings**
- Loadings are eaggl's `%.4g` strings, kept unchanged.
- eaggl sorts its rows by score, so always join on `gene_set_id`, never on row position.
- The per-trait long file also stores the rank of each gene set within each factor.

## Running

Paths are relative to this `lap/` directory. The pipeline runs on UGER; start the long stages from a tmux session.
- Per-trait projections run eaggl chunk by chunk: a 20,000-gene-set chunk peaks at ~11 GB (16 GB requested), and a
  40,000-gene-set chunk of T2D (K=13) took 11 min. A trait runs its 76 chunks one after another: ~7 h for K=13, ~3 h on
  average, ~2,000 CPU-hours for all 711.
- The gene-set betas read all 1.5M gene sets per trait (most of their time).
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

# 2. link inputs and create directories (only --init links meta files; done for eaggl_capped__cfde_2026_10_05)
lap_run --init && lap_run --mkdir && lap_run --check

# 3. inputs (never add --only here: the project-level fan-ins would shrink)
lap_run --only-cmd '^(prep_|collection_)' --bsub

# 4. smoke test (3 traits), then every trait: projections, gene-set betas, collect
lap_run --only '^(2hrG|T2D|Ap-LM)$' --only-cmd '^(trait_|betas_)' --bsub
lap_run --only-cmd '^(trait_|betas_|collect_)' --bsub

# 5. the release build (a cluster job; files only)
lap_run --only-cmd '^release_build_cmd$' --bsub
```

Publishing to the app is always a separate command run by hand ([Reference release](#reference-release-release_-stage)).

**Cluster behaviour**
- `max_sge_batch=1` turns off UGER job arrays. LAP otherwise bundles every ready command into one array with `-tc 1000`, and `max_jobs` counts an array as a single job. With arrays off, `max_jobs=50` caps the running tasks; raise it (in the cfg) if the cluster gives you more.
- A per-trait projection that is killed resumes from its finished chunks when LAP reruns it (`traits/<trait>/eaggl_chunks/`).


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
    --db out/projects/eaggl_capped__cfde_2026_09_28/portal/eaggl_capped__cfde_2026_09_28.portal.sqlite --port 8766
```

From your laptop, run `ssh -L 8766:localhost:8766 <this host>` and open `http://localhost:8766/`. Alternatively, open
the static `*.portal.html`: it calls the same URL. The server binds to 127.0.0.1 and is read-only. It has no
authentication, so keep it behind the tunnel. The database is opened immutable; restart the server after a rebuild.

## Gene-set betas (`betas_` stage)

Every trait gets a PIGEAN score for every CFDE gene set **without rerunning PIGEAN**. Each trait's existing gene-level
statistics go into `python -m pigean betas`: PIGEAN's gene-set stage on its own, which stops before the outer Gibbs
loop. The gene sets are the same CFDE file the projection uses.

1. `betas_index_cmd` (project, about 2 minutes) reads the 18 GB export once and records where each trait's rows are
   (they must be contiguous). It stores the export's size and mtime, and the next step refuses a changed export.
2. `betas_trait_gene_stats_cmd` reads the trait's byte range: its `gene, combined, log_bf, prior` rows.
3. `betas_trait_run_cmd` runs `pigean betas` with the pinned clone and the GWAS profile:
   - gene locations (not OLS), the NCBI37.3 gene universe and the portal gene map;
   - the marginal p < 0.01 prefilter and the 5,000-gene-set cap;
   - `--deterministic --seed 1`.

   It writes `beta_uncorrected` (marginal) and `beta` (joint, corrected for gene-set overlap without Gibbs) for every
   gene set PIGEAN analyzes. T2D took about 1 minute and 0.6 GB.
4. `betas_trait_annotate_cmd` keeps the analyzed gene sets (`filter_reason` `kept`; the prefiltered ones carry no
   beta), attaches the KPN and collection ids, and ranks them by `beta_uncorrected` within their library. It refuses
   another response, an unknown gene set or another pigean commit.

**Response** (`gene_set_stats_response`, default `log_bf`):
- `log_bf`, the direct genetic support. This is a PIGEAN run on the CFDE gene sets minus the outer Gibbs loop. T2D
  analyzes 1,197 gene sets.
- `combined` adds the export's mouse_msigdb priors, which come from another gene-set library. T2D then analyzes 4,974.

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
    --release out/projects/eaggl_capped__cfde_2026_09_28/release/files --env local --env qa --env prod
```

For each environment it loads that environment's `<prefix>_ref_*` tables (`reveal_workflow_local`,
`reveal_workflow_qa`, `reveal`), swaps them in with one `RENAME`, and syncs the fixed Upstash namespaces
`<env>-factors`, `<env>-contexts`, `<env>-gene-sets` and `<env>-collections`. Running apps pick the new release up
within a few seconds. A re-run is safe, and publishing an earlier release folder rolls back.

## Environment and versions

- **eaggl:** runs from a **pinned clone** of `packages/pigean` at `ca59661`: `raw/pigean_ca59661`, using its `src/` with the pigean project's venv (`/humgen/diabetes2/users/chase/projects/pigean/.venv`, Python 3.9).
  - Before each eaggl run, `record-pigean-commit` checks that the clone is at `pigean_commit` and that `src/` is clean.
  - The annotate, relabel and collect steps reject any other commit, so all 712 runs are guaranteed to use one code version.
  - No branch has `scripts/run_eaggl_supplied_projection.sh`. The pipeline calls `python -m eaggl factor` with the same flags.
- **KPN:** trait ids come from `/humgen/diabetes/users/chase/packages/dig-portal-data-models` (`main`, fast-forwarded to include tag `v0.0.2`).
  - That release removed `versions/phenotype/v0.0.1`.
  - The pigean configs that still point there need to be repointed separately: `projects/pigean/config/{analyses,inferiority,validation}.meta.yaml` and their generated `.meta` files.
