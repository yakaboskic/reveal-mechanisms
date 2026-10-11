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

## Later

The reference release on `integration/2026-10-10` replaces these tables (`reference_migration.py cleanup` drops them). Two things carry over:

- Apply the same role table in `reference_release.write_collections`, writing to `payload.provenance.used_edges` and `reveal_ref_dapper_edges`.
- Port the trace's hydration and identity binding to the `reveal_ref_*` tables.
