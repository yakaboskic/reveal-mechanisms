# Reference release

The app's reference data is the EAGGL factors and their gene loadings, the KPN traits, the CFDE gene sets and collections, their projections onto the factors, the gene-set provenance, and the embeddings. It is built as **one release folder of plain files** and **published with one command** that replaces each environment's reference tables and vectors. The command has no generations, snapshots, plans, approvals, gates or archive pass.

## How the app uses it

- **Tables per environment.** Each environment has its own reference tables in the shared Aurora database `cyaka_reveal_mechanisms`, named after its table prefix like its records:
  - local: `reveal_workflow_local_ref_*`
  - QA: `reveal_workflow_qa_ref_*`
  - prod: `reveal_ref_*`
- **Vectors.** Each environment has fixed Upstash namespaces: `<env>-factors`, `<env>-contexts` (DisMech gap and mechanism texts), `<env>-gene-sets` and `<env>-collections`.
- **Current factors.** A factor anchor is current exactly when its factor id is in the environment's `ref_factors` table. Submitting, freezing or retrying with an unserved factor returns 409 `REFERENCE_GENERATION_SUPERSEDED`. An unserved factor's page returns 410 with its frozen snapshot from `archived_reference_factors`.
- **Picking up a release.** The API watches the one-row `ref_release` table and swaps its catalog within about 5 seconds of a publish. Workers read the tables for each job.
- **Query embeddings.** These use `EMBEDDING_SERVICE_URL` (currently `https://embedding-service-848707719401.us-east1.run.app`), with `EMBEDDING_MODEL` and `EMBEDDING_PROVIDER` from the environment.
- **Stability across rebuilds.** Factor ids (`factor:kpn:<NNNNNNN>:eaggl-capped-v1:FactorN`) and DAPPER gene-set ids don't change when a release is rebuilt. A factor's `source_revision` is a digest of its label, trait and gene loadings, so a rebuild that leaves the factors unchanged keeps every saved selection valid.
- **Adding gene-set libraries** is just a new LAP projection, build and publish. Nothing a user saved is invalidated.
- **Archived work stays as it is.** Accounts and outcomes that the earlier cutover stamped as built on an older reference keep their stamps and keep rendering exactly as before.

## Build: the LAP `release_build_cmd`

The build reads only files and runs in about 10 minutes. Its output goes to `lap/out/projects/<project>/release/files/`:

| File | Content |
|---|---|
| `manifest.json` | `release_id` (a digest of the data files), sources (LAP project, pigean commit, KPN release, CFDE snapshot), embedding model and dimensions, counts, file sha256s |
| `traits.jsonl.gz` | KPN traits (711) |
| `factors.jsonl.gz` | factors (4,037): `factor_key`, `public_id`, `eaggl_factor_id`, `kpn_trait_id`, `factor_number`, `label`, `input_sha256`, `source_revision`, metadata |
| `factor_genes.tsv.gz` | the nonzero EAGGL gene loadings (2,553,330) |
| `collections.jsonl.gz`, `gene_sets.jsonl.gz` | the CFDE collections (133) and gene sets (44,399), each gene set with its exact DAPPER node, members included |
| `projections.tsv.gz` | joint and marginal loadings with **per-library** ranks. A row is kept when either rank is at most 50 within its library (GTEx, HuBMAP, LIGER, LINCS_L1000, MoTrPAC). |
| `dapper_nodes.jsonl.gz`, `dapper_edges.tsv.gz` | the full DAPPER provenance graph from the collection documents: organizations, datasets, files, activities and embeddings, plus the `used`, `was_generated_by`, `was_derived_from` and `has_embedding` edges |
| `archived_factors.jsonl.gz` | a frozen snapshot of every factor in the release: its label, trait, metadata, top 50 genes and each library's top 10 gene sets |
| `vectors/*.f32.npy` + `vectors/*.tsv` | factor-label, context, gene-set and collection vectors |

**Where the vectors come from:**
- **Gene sets and collections:** the CFDE snapshot's float16 matrix, converted to float32.
- **Factor labels and DisMech contexts:** the vector cache `lap/raw/reference_vector_cache.sqlite`, keyed by the sha256 of the text.
- **Text not in the cache** (a new factor label, say) is embedded with `EMBEDDING_SERVICE_URL`. First, 8 cached texts are re-embedded; this must give a cosine of at least 0.999, or the build stops.

## Publish

Run this from the LAP host, by hand:

```bash
services/backend/.venv/bin/python -m reveal_backend.reference_release publish \
    --release lap/out/projects/eaggl_capped__cfde_2026_09_28/release/files --env local --env qa --env prod
```

For each `--env`, in the order given:
1. Upsert into the four namespaces every vector that is missing or changed.
2. Load the `<prefix>_ref_*__new` tables, then swap all of them, `ref_release` included, with one `RENAME TABLE`.
3. Add the frozen snapshots of new or changed factors to `archived_reference_factors`. A snapshot is keyed by the factor's `source_revision`, so an unchanged factor adds no row. Its `generation_id` is the first release that published it.
4. Delete the vectors that are no longer in the release.
5. Write a `reference_release` record. Open composers get the `catalog.updated` (`reference`) event from it.

It takes about 5 minutes per environment, and a repeat run is fast. To roll back, publish the previous release folder.

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
     - If prod was never cut over by the old reload, run `archive-prod --apply --backup-dir <dir>` after the new code is serving prod. It backs up `reveal_records`, then stamps prod's existing work exactly as the cutover stamped local's and QA's.
     - Let running legacy analysis jobs finish first, or cancel them; the new code can't collect legacy anchors.

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
