# Reference release

The app's reference data is the EAGGL factors and their gene loadings, the KPN traits and the other PIGEAN phenotypes, the CFDE gene sets and collections, their projections onto the factors, the trait betas, the factor-trait links, the gene-set provenance, and the embeddings. It is built as **one release folder of plain files** and **published with one command** that replaces each environment's reference tables and vectors. The command has no generations, snapshots, plans, approvals, gates or archive pass.

## How the app uses it

- **Tables per environment.** Each environment has its own reference tables in the shared Aurora database `cyaka_reveal_mechanisms`, named after its table prefix like its records:
  - local: `reveal_workflow_local_ref_*`
  - QA: `reveal_workflow_qa_ref_*`
  - prod: `reveal_ref_*`
- **Vectors.** Each environment has two fixed Upstash namespaces: `<env>-factors` (factor labels) and `<env>-contexts` (DisMech gap and mechanism texts). Gene sets and collections are not embedded.
- **Current factors.** A factor anchor is current exactly when its factor id is in the environment's `ref_factors` table. Submitting, freezing or retrying with an unserved factor returns 409 `REFERENCE_GENERATION_SUPERSEDED`. An unserved factor's page returns 410 with its frozen snapshot from `archived_reference_factors`, the latest one the environment's archive index lists (below).
- **Picking up a release.** The API watches the one-row `ref_release` table and swaps its catalog within about 5 seconds of a publish. Workers read the tables for each job.
- **Query embeddings.** These use `EMBEDDING_SERVICE_URL` (currently `https://embedding-service-848707719401.us-east1.run.app`), with `EMBEDDING_MODEL` and `EMBEDDING_PROVIDER` from the environment.
- **Stability across rebuilds.** Factor ids (`factor:kpn:<NNNNNNN>:eaggl-capped-v1:FactorN`) and DAPPER gene-set ids don't change when a release is rebuilt. A factor's `source_revision` is a digest of its label, trait and gene loadings, so a rebuild that leaves the factors unchanged keeps every saved selection valid.
- **Adding gene-set libraries** is just a new LAP projection, build and publish. Nothing a user saved is invalidated.
- **Archived work stays as it is.** Accounts and outcomes that the earlier cutover stamped as built on an older reference keep their stamps and keep rendering exactly as before.

## Build: the LAP `release_build_cmd`

The build reads only files and runs in about 10 minutes. Besides the projection and betas outputs it reads the `linkage_` stage's links and phenotypes, the `provenance_` stage's collection map and bottom-line artifacts (fetched from the portal once by the local `provenance_bottom_line_cmd`; delete `bottom_line_artifacts.tsv` to refresh them), and `--source-datasets lap/config/source_datasets.yaml`. Its output goes to `lap/out/projects/<project>/release/files/`:

| File → table | Content |
|---|---|
| `manifest.json` → `ref_release` | `release_id` (a digest of the data files), sources (LAP project, pigean commit, KPN release, CFDE snapshot, source datasets), embedding model and dimensions, counts, file sha256s, the curated-lineage edge counts per dataset and the GWAS ancestry. The stored copy adds `absent_files`. |
| `traits.jsonl.gz` → `ref_traits` | KPN traits (711). `metadata.bottom_line`: the trait's bottom-line GWAS artifacts on the Translator portal, the one its GWAS route uses (`gwas_artifact`, ancestry `--gwas-ancestry`, Mixed), and otherwise a `gwas_reason`: `not_a_gwas_trait` (rare-disease lists) or `not_on_portal` |
| `factors.jsonl.gz` → `ref_factors` | factors (4,037): `factor_key`, `public_id`, `eaggl_factor_id`, `kpn_trait_id`, `factor_number`, `label`, `input_sha256`, `source_revision`, metadata. `metadata.trait_links` summarizes the factor's links: `n_phenotypes` (6,697 tested), `n_linked` (its own trait included), `n_linked_kpn_traits` (distinct KPN ids) and `n_linked_atlas_traits` (both excluding its own trait), `own_trait_q` and the 5 best other links `top_linked` (`phenotype`, `kpn_trait_id`, `name`, `phewas_q`). The frozen snapshot keeps the LAP metadata only. |
| `factor_genes.tsv.gz` → `ref_factor_genes` | the nonzero EAGGL gene loadings (2,553,330) |
| `collections.jsonl.gz` → `ref_collections` | the CFDE collections (the 2026-10-05 DAPPER 0.2.0 release minus GaultonLab: 532), each with its Translator portal collection (`portal_collection_id`; for the 59 re-minted LINCS collections it differs from `collection_id`). `payload.provenance` keeps every DAPPER provenance node and edge group of the document, before or after the gene sets, with the edges validated (`collection_provenance`); a section may appear once, and embedding sections are skipped unparsed. `payload.curated_provenance` holds curated lineage (below). |
| `gene_sets.jsonl.gz` → `ref_gene_sets` | **only the referenced gene sets**: those in `projections.tsv.gz` or `trait_gene_sets.tsv.gz` (about 151,000 of the 1,517,472), each with its exact DAPPER node (members included when the document inlines them). Every document's gene sets are still checked against the gene-set index; `gene_sets_indexed` counts them all. |
| `projections.tsv.gz` → `ref_projections` | joint and marginal loadings with **per-library** ranks. A row is kept when either rank is at most 50 within its library (GTEx, HuBMAP, LIGER, LINCS_L1000, MoTrPAC). |
| `trait_gene_sets.tsv.gz` → `ref_trait_gene_sets` | trait → CFDE gene-set betas from the LAP `betas_` stage: `pigean betas` (no outer Gibbs) on each trait's existing PIGEAN gene stats, fitted per CFDE library (each library its own prior, prefilter and 5,000-gene-set cap). It holds `beta_uncorrected`, joint `beta`, `avg_postp` and the rank within the library, for the gene sets PIGEAN analyzed (its marginal p < 0.01 prefilter). The manifest records the response (`log_bf` by default). |
| `factor_trait_links.tsv.gz` → `ref_factor_trait_links` | every factor × PIGEAN phenotype link at the factor-PheWAS q cutoff (2,335,999; the LAP `linkage_` stage), in (`factor_key`, `phenotype`) order: `phenotype_kpn_trait_id` (NULL for the phenotypes without a KPN id), `is_own_trait`, `is_atlas_trait`, `nnls_loading`, `phewas_beta`, `phewas_p_onesided`, `phewas_q`. Indexed by (`phenotype_kpn_trait_id`, `phewas_q`) and (`factor_key`, `phewas_q`). |
| `phenotypes.jsonl.gz` → `ref_phenotypes` | the 6,698 PIGEAN phenotypes the linkage reads: `kpn_trait_id` (unique, NULL without one), `phenotype_name`, `display_name` (the name, or the id without `gcat_trait_`), `legacy_phenotype_id` (KPN registry), `category` (`gwas_source_category`), `is_atlas`, and metadata with `kpn_match`, gene counts and `bottom_line` |
| `archived_factors.jsonl.gz` | a frozen snapshot of every factor in the release: its label, trait, metadata, top 50 genes and each library's top 10 gene sets |
| `vectors/*.f32.npy` + `vectors/*.tsv` | factor-label and DisMech context vectors |

**Required, optional and retired files.** A folder must hold the traits, factors, factor genes, collections, gene sets, projections and vectors. `trait_gene_sets.tsv.gz`, `factor_trait_links.tsv.gz` and `phenotypes.jsonl.gz` are optional: a folder without one (an older release) publishes its table empty, and the stored manifest lists it under `absent_files`, so the routes reading it answer "not in this release". `dapper_nodes.jsonl.gz` and `dapper_edges.tsv.gz` are retired: a folder holding them (or without the manifest's `curated` block) predates the curated lineage and portal collection ids and is refused before anything changes ("rebuild this release"). Publish drops the `ref_dapper_*` tables by name (live ones only after the new release is swapped in; never by pattern). A folder naming any other unknown file was built by a newer checkout and is refused ("publish with a newer checkout"). Before loading, publish refuses when the largest row (the manifest's `max_row_bytes`) cannot fit `max_allowed_packet`; before the swap, when a loaded table's row count differs from the manifest.

**Curated lineage (`lap/config/source_datasets.yaml`).** Some producers name no source Dataset: their collections' lineage ends at raw files (LINCS HZ3 and HZ4, IDG HZ2, GlyGen HZ2). The map lists:
- `organizations` and `datasets`: verbatim copies of existing DAPPER nodes, id included (the build refuses one whose id does not recompute), or new Datasets (`name`, `description`, `version`, `alternate_identifier`, `location`, `publisher` and `attributed_to` naming organizations by key), whose ids the build mints;
- `links`: `{dataset, libraries, files, note}`, where `files` are regexes on a raw file's `filename` (its `name` when it has none).

For a collection whose own provenance has no `was_derived_from` edge to one of its Datasets, every raw File or C2M2File (never the subject of a `was_generated_by` edge) matching a link of its library gets a `prov:wasDerivedFrom` edge to the link's Dataset. `payload.curated_provenance` = `{source, source_sha256, groups: {organizations, datasets, was_derived_from_edges}, notes}` holds only what those edges reach, validated like the producer's edges; `payload.provenance` stays exactly the producer's. `reference_lineage.collection_lineage(payload)` merges the two (the producer's node wins an id collision), and `reference_lineage.lineage_closure(gene_set, lineage)` gives one gene set's lineage with the inputs that share their onward lineage collapsed to one representative (GlyGen HZ2's 6,620 inputs become one path to the GlyGen release). The build logs the edges per dataset and **fails while a collection holding a referenced gene set reaches no Dataset**.

**Where the vectors come from:**
- **Factor labels and DisMech contexts:** the vector cache `lap/raw/reference_vector_cache.sqlite`, keyed by the sha256 of the text.
- **Text not in the cache** (a new factor label, say) is embedded with `EMBEDDING_SERVICE_URL`. First, 8 cached texts are re-embedded; this must give a cosine of at least 0.999, or the build stops.

## Publish

Run this from the LAP host, by hand:

```bash
services/backend/.venv/bin/python -m reveal_backend.reference_release publish \
    --release lap/out/projects/eaggl_capped__cfde_2026_09_28/release/files --env local --env qa --env prod
```

First, every file is checked against the manifest. A partial copy, or a folder that a rebuild is replacing, is refused.

Then, for each `--env` in the order given:
1. Add the vectors each of the two namespaces lacks. The served tables don't name them yet, so the app is unaffected.
2. Add the frozen snapshots of new or changed factors to `archived_reference_factors`. A snapshot is keyed by the factor's `source_revision`, so an unchanged factor adds no row. Its `generation_id` is the first release that published it. The shared table is never altered.
3. List the release's snapshots in the environment's archive index, `<prefix>_archive_index` (`source_id_sha256`, `archive_id`, `mechanism_id`, `published_at`), created when absent and never swapped or purged (`INSERT IGNORE`). Each publish also lists the shared snapshots of the KPN and legacy generations the releases replaced. The catalog reads an unserved factor's snapshot, and a Mechanism id's, through this index, so an environment never serves another one's snapshot; until the index exists it falls back to the shared table. Lookups by `archive_id` stay unscoped.
4. Load the `<prefix>_ref_*__new` tables, then swap all of them, `ref_release` included, with one `RENAME TABLE`, and drop the old ones and the retired `ref_dapper_*` tables.
   - The swap waits at most 5 seconds at a time for a long reader of the live tables, up to 12 times, so app queries never queue behind it for long.
   - If the release folder was replaced while the tables loaded (a rebuild), the swap is refused.
5. Overwrite the vectors that changed, then delete the ones that are no longer in the release.
6. Write a `reference_release` record. Open composers in that environment get the `catalog.updated` (`reference`) event from it.

It takes about 5 minutes per environment, and a repeat run is fast. If a publish dies, run it again: it cleans up and finishes the remaining steps. To roll back, publish the previous release folder. **Every publish, a rollback included, runs from the newest checkout**: it knows every file and table an older folder has (an absent optional table is published empty, retired tables are dropped), while an older checkout refuses a newer folder's files.

A release that only adds gene sets keeps every saved anchor valid. An analysis can mix factors saved under different releases, because the binding's run ids name the embedding space, not the release.

Credentials:
- MySQL: `REVEAL_MYSQL_*` from the repository `.env`.
- Upstash: `UPSTASH_VECTOR_REST_URL` and `UPSTASH_VECTOR_WRITE_TOKEN`, since one index serves every environment. `UPSTASH_VECTOR_REST_URL_<ENV>` and `UPSTASH_VECTOR_WRITE_TOKEN_<ENV>` override them per environment.

## One-time migration from the reference reload

`scripts/reference_migration.py` makes the one-time move from the old generation/snapshot reload. It runs on the LAP host. Every subcommand is a dry run unless given `--apply`.

1. **Export the vectors.** `export-vectors --cache lap/raw/reference_vector_cache.sqlite` copies the factor-label vectors (EAGGL embedding run `d4c03009…`) and the DisMech context vectors (run `dd922e3b…`) from Aurora into the cache. It is read-only on Aurora.
2. **Freeze the legacy factors.** `freeze-legacy --apply` stores a frozen snapshot of each of the 1,756 legacy `cfde-inc-v2` factors in `archived_reference_factors`, so every legacy factor link still shows its factor.
3. **Build** with `release_build_cmd`.
4. **Switch each environment over, in this order:**
   - **local:** publish local, run the new code, check the UI.
   - **QA:** publish QA (the code still running ignores the new tables), deploy, check.
   - **prod:** publish prod, then deploy. The prod deploy needs sagehen03's approval.
     - If prod was never cut over by the old reload, run `archive-prod --apply --backup-dir <dir>` after the new code is serving prod.
       - It backs up `reveal_records`, then stamps prod's legacy work exactly as the cutover stamped local's and QA's.
       - Work made with the new code stays current.
       - It cancels the legacy analysis jobs that haven't started collecting, since the new code can't collect legacy anchors.
       - It refuses while a legacy job that has already collected is still running. Run it again once that job finishes.
     - It refuses a prefix that the old reload already cut over.

   Until cleanup, rolling back an environment means redeploying its previous code.
5. **Clean up** after about a week, with `cleanup --apply`. It deletes:
   - the old Upstash namespaces;
   - the old `vector_*` and `reference_*` bookkeeping records;
   - the old tables: migration 008's, except `archived_reference_factors`, plus the `eaggl_*`, `eaggl_cfde_*`, gene-set import and DisMech embedding tables;
   - the compose and rehearsal tables.

   It keeps `archived_reference_factors` and the DisMech source tables. It refuses to start until every legacy factor is frozen and every environment has its `ref_*` tables and namespaces. After cleanup, the following can be deleted from the code, since the serving code reads none of the dropped tables:
   - `reference_archive.py`;
   - `reference_generation.py`'s pointer helpers;
   - migration 008's other tables;
   - the old importers that read the dropped tables: `eaggl_database.py`, `eaggl_cfde_links.py`, and the database readers in `eaggl_embeddings.py` and `dismech_embeddings.py`.
