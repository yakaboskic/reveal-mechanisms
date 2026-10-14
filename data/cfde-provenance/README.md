# Curated CFDE collection lineage

Account provenance (`GET /v1/accounts/{id}/provenance`) traces a dapper:GeneSet, or a dapper:Mechanism through its factor→GeneSet projections, to the CFDE Dataset and Organization that produced it. The served KPN reference generation `ec3364ae…` (loaded 2026-10-02 from the 2026-09-28 CFDE documents) stores each collection's Datasets and Organizations in `cfde_gene_set_collections.payload.provenance`. It does not store the edges that followed `gene_sets:` in those documents, so a GeneSet's generating Activity reached no Dataset.

The DAPPER 0.2.0 documents in `s3://dig-gene-set-data/v2.0_dapper-0.2.0-a1` cannot fill this:

- they contain no Datasets or Organizations, only Files, Activities and `used` edges to raw files;
- their GeneSet ids differ from the served ones.

The lineage is therefore curated by hand, from the nodes the database already holds.

## Curation (`2026-10-11/collection-lineage.json`)

Each collection's convert Activity (the `was_generated_by` of its gene sets) `prov:used` the upstream Datasets of its library. The intermediate `dig-gene-set-filtered/<LIB>` dataset is excluded and is not represented as a step.

| Library | `data_input` | `metadata_input` |
|---|---|---|
| GTEx | GTEx Analysis V8 bulk tissue RNA-seq (publisher Broad Institute, attributed to GTEx Consortium) | NCBI Gene `gene_info` (NCBI) |
| HuBMAP | HuBMAP ASCT+B tables v2.2 (HuBMAP Consortium; Indiana University) | NCBI Gene `gene_info` |
| LINCS_L1000 | LINCS L1000 consensus signatures (Icahn School of Medicine at Mount Sinai; Ma'ayan Laboratory) | Harmonizome `mappingFile_2017.txt` |
| MoTrPAC | PASS1B-06 released differential analysis; PASS1B-06 raw counts (MoTrPAC Study Group) | Harmonizome mapping |
| LIGER | none: no upstream Dataset or Organization is recorded, so none is invented | none |

The organizations are the Datasets' stored `publisher` and `was_attributed_to`; nothing about them is curated. Every curated edge is labelled `provenance_source.kind: curated_lineage` in provenance responses.

## Running it

```sh
.venv/bin/python scripts/apply_collection_lineage.py plan
.venv/bin/python scripts/apply_collection_lineage.py apply --apply --expect-plan <plan_sha256>
.venv/bin/python scripts/apply_collection_lineage.py rollback --backup .runtime/cfde-lineage/<ts>/payloads.jsonl.gz [--apply]
```

**`plan` only reads.** It refuses:

- a non-KPN or incomplete generation;
- a generating Activity that is not stored;
- an unclassified or renamed Dataset;
- different existing `used_edges`;
- a curated input stored nowhere.

It also proves these gates:

- the edges validate as DAPPER 0.2.0 `Used`;
- every patched collection's representative gene sets stay exact in evidence packages and gain the input Datasets and Organizations;
- shared nodes are identical across collections;
- the trace reaches each input.

**`apply`** works like this:

- It re-plans under the reference reload lock and requires the reviewed `plan_sha256`.
- It refuses if production serves the generation, unless `--allow-production` is given.
- It writes a gzip backup before any change, then updates in one transaction and reads back.
- Re-running reports `unchanged`.

**`rollback`** restores the backup only where a row still holds exactly the curated payload.

The reference tables are shared by every environment in `cyaka_reveal_mechanisms`, so one apply covers local and QA. Production serves no KPN generation.

`scripts/verify_account_provenance.py --prefix … --account … [--overlay]` runs the endpoint in-process, as each account's owner and read-only. With `--overlay` it substitutes the planned payloads, so the effect can be reviewed before applying. Pass the target environment's storage settings (`REVEAL_ARTIFACT_STORE`, `REVEAL_S3_BUCKET`, `REVEAL_S3_PREFIX`, AWS profile).

## Release era

This curation patches the shared reload-era tables only. It stays here as reload-era tooling: QA runs it while it serves generation `ec3364ae…`, and the trace labels its edges when a capture pinned to that generation reaches them. A published reference release (`reveal_ref_*`, see [reference releases](../../docs/reference-release.md)) never reads these tables:

- **Lineage:** the release keeps every provenance group of the 2026-10-05 producer documents in `payload.provenance`, and curates only collections whose producer reaches no Dataset, in `payload.curated_provenance` from [`lap/config/source_datasets.yaml`](../../lap/config/source_datasets.yaml) (LINCS HZ3/HZ4, IDG HZ2, GlyGen HZ2). Those curated edges are labelled `provenance_source.kind: curated_lineage` too, with the map's path as `curation_id`.
- **Identity binding** of uncaptured GeneSets and Mechanisms binds to the published release (`ref_gene_sets`, the Mechanism minted from `ref_factors` + `ref_traits`), never to a reload-era generation. See [account provenance](../../docs/account-provenance.md#identity-binding).

Compared with this curation, the 2026-10-05 producer documents link the following (one collection of each library and model checked, read-only, from `lap/out/projects/eaggl_capped__cfde_2026_10_05/collections`; the release build was not changed):

| Library | This curation | 2026-10-05 producer lineage |
|---|---|---|
| GTEx HZ1 | GTEx V8 (`data_input`), NCBI gene_info (`metadata_input`) | Same Datasets and ids. gene_info is a `data_input`; GTEx V8 is also reached as `metadata_input` through the sample and subject annotations. |
| GTEx HZ2 (`all_detailed_tissues`) | as GTEx | GTEx V8 only, and only through the `metadata_input` annotations: the `data_input` `gene_tpm.gct.gz` derives from no Dataset, and gene_info is not used. |
| HuBMAP | ASCT+B v2.2 (`data_input`), NCBI gene_info (`metadata_input`) | Same Datasets and ids; gene_info is a `data_input`. |
| MoTrPAC | PASS1B-06 differential analysis and PASS1B-06 raw counts (`data_input`), Harmonizome mapping (`metadata_input`) | Differential analysis and Harmonizome mapping (both `data_input`). The raw-counts Dataset `dapper:Dataset.l_ygaoXdms1Pvp0EZHYE5pwvBzvRec88` is not in the document. |
| LINCS_L1000 | consensus signatures `dapper:Dataset.6nVB5hkgLulEUdRFJBvaUi6N8lASgHy0` (`data_input`), Harmonizome mapping (`metadata_input`) | HZ1/HZ2: a consensus-signatures Dataset of the same name with another id (`dapper:Dataset.vtIz7XY66O_ox7A5nBkp2iHTLl2R5WLl`) and the Harmonizome mapping, both `data_input`. HZ3/HZ4: the curated Level-5 `cp_coeff_mat` Dataset, not the consensus one. |
| LIGER | unresolved | Not in the 2026-10-05 pool. |

GlyGen, IDG, IMPC, MetabolomicsWorkbench and RummaGEO are only in the release; each reaches a Dataset through its producer lineage or the curated map.
