# PIGEAN claims collection (LAP)

This LAP pipeline builds the files of a claims-repository collection from one PIGEAN portal run and the CFDE gene
sets, with DAPPER provenance for every file, and deposits them on S3. It also deposits the CFDE gene sets themselves,
with the provenance their producer shipped, as a second collection. It is separate from `../lap` (the CFDE projection onto EAGGL factors): its own config,
scripts, `raw/`, `out/` and `log/`. The two share only the pinned pigean commit.

The layout and file rules are those of `claims-repository/docs/publishing-a-pigean-run.md`.

| Stage | What it does | Where it runs |
|---|---|---|
| 1. `source_` | Downloads every trait's `gs.out`, `gss.out` and `ggss.out` of the PIGEAN run into `raw/pigean_portal/` and checks sizes and MD5s | head node (S3) |
| 2. `genesets_` | One GMT per CFDE library and the gene-set index, from the local mirror of the CFDE release | head node or UGER |
| 3. `betas_` | Per trait: `python -m pigean betas` of the run's gene stats on each CFDE library | UGER |
| 4. `format_` | Per trait: the collection's NDJSON files, one folder per library | UGER |
| 5. `collect_` | Fan-in: file manifest (sha256 re-checked), betas manifest, run record | UGER |
| 6. `provenance_` | Per trait: a DAPPER provenance document and the raw `pigean betas` outputs; then one merged, validated document | head node (per trait), UGER (merge) |
| 7. `cfde_` | The CFDE gene-set collection as a folder: the release's GMTs with their producer DAPPER documents, the library GMTs, the gene-set index | head node or UGER |
| 8. `deposit_` | Uploads both collections under `s3://kpn-opendore-releases/collections/` | head node, by hand |

## Inputs

| Input | What |
|---|---|
| PIGEAN run | `s3://dig-pigean-paper-regression-2/out/falcon/staging/pigean/portal/<trait>/mouse_msigdb/{gs,gss,ggss}.out`: 1,444 traits with all three files (31 trait folders hold only `_SUCCESS` and are left out) |
| CFDE gene sets | `s3://dig-gene-set-data/v2.0_dapper-0.2.0-a1/`, mirrored on 2026-11-24 to `/humgen/diabetes/users/chase/data/dig-s3/gene_sets/cfde/2026-11-24` (every `genesets.dapper-ids.gmt`, `genesets.gmt`, `geneset.meta.json` and `geneset.provenance.dapper.yaml`, all libraries but LINCS_L1000) |
| pigean | commit `ca59661` in `raw/pigean_ca59661` (the commit `../lap` uses) plus `patches/pigean_ca59661_correlation_cap.patch` |

**The pigean patch.** pigean fits `r^2 = intercept + slope * distance` between genes from the trait's gene stats and
uses it as the residual correlation of nearby genes. It does not cap the value. For about one trait in five of this
run the fitted intercept is 1 or more, nearby genes get a correlation above 1, and pigean's own check refuses it
("Residual correlation must be symmetric and bounded by one"); pigean `main` has the same code. The patch caps the
correlation at 1 (one line). It changes nothing for a trait whose intercept is below 1: AbAge, AbFR and Act_out_dreams
gave byte-identical gene-set stats in all 13 libraries tested with and without it. Every betas job checks that the
clone differs from the commit by exactly this file, and the run record and the provenance name it.

The mirror holds six libraries the `2026-10-05` snapshot lacks, added to the release on 2026-10-08: AMP_AD, GEO_BULK,
IGVF, ImmPort, KPMP_KTA and PsychENCODE. The 14 fitted libraries hold 35,441 gene sets: RummaGEO 10,332; IGVF 7,697;
GlyGen 6,875; KPMP_KTA 6,805; IDG 1,240; GTEx 781; IMPC 536; HuBMAP 358; MetabolomicsWorkbench 295; GEO_BULK 200;
MoTrPAC 166; AMP_AD 110; PsychENCODE 40; ImmPort 6. For the eight libraries `../lap` also fits, the GMTs are identical to
its GMTs line for line.

Left out by default (`build_meta_yaml.py --exclude-library`): GaultonLab (in the bucket, not a CFDE library; `../lap`
leaves it out too) and LINCS_L1000 (1.5M signatures, about 30 minutes and 24 GB per trait; its files are in the
`2026-10-05` snapshot and unchanged on S3 since).

## What the collection holds

One dataset per (library, trait). Every row carries `phenotype` (the trait folder) and `gene_set_size` (the library
folder). Uncompressed NDJSON, finite numbers only.

```text
data/<library>/<trait>/combined_gene_stats.json        mouse_msigdb only
data/<library>/<trait>/combined_gene_set_stats.json
data/<library>/<trait>/gene_gene_set_stats.json
data/<library>/<trait>/<kind>.json.dapper.yaml                DAPPER provenance of the file beside it
betas/<library>/<trait>/gene_set_stats.tsv, params.tsv        pigean's own outputs (CFDE libraries with a fit)
betas/<library>/<trait>/<file>.tsv.dapper.yaml                DAPPER provenance of the file beside it
provenance/<trait instance>.dapper.yaml                       DAPPER provenance of the trait's files
provenance/<project>.provenance.dapper.json                   all traits merged (the full graph)
provenance/<project>.collection.dapper.json                   collection files and their generating Activities
run/<project>.collection_manifest.tsv, .provenance_manifest.tsv, .betas_manifest.tsv, .run.json
```

| File | Columns | Rows |
|---|---|---|
| `combined_gene_stats.json` | `gene`, `prior`, `combined`, `log_bf`, `trait_group`, `phenotype`, `gene_set_size` | Every gene of `gs.out` |
| `combined_gene_set_stats.json` | `gene_set`, `source`, `beta`, `beta_uncorrected`, `n`, `trait_group`, `phenotype`, `gene_set_size`; CFDE libraries also `gene_set_name`, `collection_id` | The gene sets PIGEAN analyzed (`filter_reason` kept) with a nonzero `beta_uncorrected` |
| `gene_gene_set_stats.json` | `gene`, `gene_set`, `phenotype`, `gene_set_size` | The memberships of those gene sets |

- **`mouse_msigdb`** rows are the PIGEAN run's own. The gene-set rule reproduces the existing collection's file: for
  IBD, `gss.out`'s 1,567 kept gene sets less the 126 with a zero `beta_uncorrected` are the 1,441 rows of the published
  `combined_gene_set_stats.json`. Memberships are `ggss.out`'s pairs.
- **Dropped from the old files**: `huge_score`, `n` (gene), `sigma`, and the EAGGL `label` and `factor` columns of the
  gene and gene-set files; all eight metric columns of the membership file. The claims collection reads none of them.
- **CFDE libraries** have gene-set and membership files only. `pigean betas` regresses the run's `log_bf` on a library's
  gene sets and produces no gene scores, so a CFDE dataset has no gene file; the trait's gene scores are in its
  `mouse_msigdb` dataset. `gene_set` is the `dapper:GeneSet` id, `gene_set_name` its name in the producer's DAPPER
  document, `source` the collection label (`GTEx__adipose_tissue__HZ1__20-29_30-39`) and `collection_id` the
  `dapper:GeneSetCollection` id.
- **CFDE memberships** are the gene set's whole roster from the library's GMT. A gene that is also in the trait's
  `gs.out` is written in `gs.out`'s spelling (CFDE `C10orf71` is PIGEAN's `C10ORF71`). `n` is PIGEAN's `N`, the member
  genes the fit used, so it can be smaller than the roster.
- A few `mouse_msigdb` gene sets (1 to 4, for some traits) have no row in `ggss.out`. They keep their gene-set row and
  have no roster.
- A library with no gene-set rows for a trait gets no folder for it.

## Provenance

**Beside every file the pipeline writes there is `<file>.dapper.yaml`**: for `combined_gene_set_stats.json`,
`combined_gene_set_stats.json.dapper.yaml` in the same folder; likewise for `betas/.../gene_set_stats.tsv` and
`params.tsv`, and for each `library_gmts/<library>.gmt` of the CFDE gene-set collection. A sidecar is a complete DAPPER
document of that one file's lineage: the File, the Activity that generated it, the files that Activity used, and how
those were produced in turn (about 5 KB). The claims CLI does not read them: its path pattern ends at `.json`.

The same nodes, for a whole trait, are in `provenance/<instance>.dapper.yaml`:

| Activity | Used | Generated |
|---|---|---|
| PIGEAN portal run: `<trait>` (mouse_msigdb) | | `gs.out`, `gss.out`, `ggss.out` at their source location |
| pigean betas: `<trait>` on `<library>`, one per CFDE library (commit, patch, command, kept gene sets, p, sigma2) | `gs.out`, the library GMT | `betas/<library>/<trait>/gene_set_stats.tsv`, `params.tsv` |
| Format `<trait>` on `<library>` for the PIGEAN claims collection, one per library | mouse_msigdb: the three source files; a CFDE library: its betas stats, its GMT and `gs.out` | `data/<library>/<trait>/*.json` |

Every File has its sha256, size and deposit location. Ids are DAPPER-ID-1 content addresses minted with the claims
repository's identity module, so this stage runs on its Python 3.12 venv (`claims_repo_dir` in the cfg), and the
merged document is validated with the publisher's own loader. `provenance_collect_cmd` fails unless every collection
file has a File node with its sha256 and a generating Activity. Locations depend on `release_prefix` and
`cfde_release_prefix`, so set those before this stage.

The claims collection lists `<project>.collection.dapper.json` (copied to its `provenance/` folder) under `provenance`
in `source.yaml`; the publisher matches result files to it by sha256 and links each dataset to the Activity that
generated it.

## CFDE gene-set collection (`cfde_` stage)

`s3://kpn-opendore-releases/collections/cfde-genesets/2026.11/`:

```text
genesets/<release key>/genesets.dapper-ids.gmt          the release's GMT: dapper:GeneSet id, description, genes
genesets/<release key>/geneset.provenance.dapper.yaml   the producer's DAPPER document: GeneSets with members, the
                                                        GeneSetCollection, the Files and Activities behind them
genesets/<release key>/geneset.meta.json                the extractor's metadata
library_gmts/<library>.gmt                              one GMT per library, as the betas read it
library_gmts/<library>.gmt.dapper.yaml                  DAPPER provenance of the GMT beside it
gene_set_index.tsv                                      gene_set_id, gene_set_name, collection_id, cfde_label, library, n_genes
run/<project>.cfde_manifest.tsv, .cfde_collection.json
```

14 libraries, 7,228 GMT folders, 7,128 gene-set collections, 35,441 gene sets. `scripts/cfde_objects_deposit.py` sends
the producer documents to a claims service as deposits into a release of the object collection
`kpn-dapper-toolkit/collections/cfde-genesets` (needs a deposit key granted that collection; C2M2File nodes are left
out because the claims repository's DAPPER profile cannot verify their ids).

## Layout

```
config/pigean_collection.cfg          LAP pipeline
config/pigean_collection.meta.yaml    GENERATED by scripts/build_meta_yaml.py
config/pigean_collection.traits.tsv   GENERATED with it: instance, trait key, S3 sizes and ETags
config/pigean_collection.meta         GENERATED by meta-sanity from the .yaml
scripts/build_meta_yaml.py            lists the run on S3 -> meta.yaml and the traits table
scripts/collection_workflow.py        helper subcommands (Python 3.9, standard library; S3 through the aws CLI)
scripts/provenance_workflow.py        DAPPER provenance documents (claims repository's Python 3.12)
scripts/release_workflow.py           the CFDE gene-set collection folder; deposits to S3
scripts/cfde_objects_deposit.py       sends the CFDE DAPPER documents to a claims service (by hand)
scripts/tests/                        unit tests on tiny fixtures
patches/                              the one patch on the pinned pigean clone
raw/  out/  log/                      downloads and the pinned clone / LAP outputs / run logs (git-ignored)
```

A trait key can hold shell characters (`Female_infertility_(all_causes)`), so the LAP instance name replaces them with
`_`. The key names the folders and fills `phenotype`; helpers look it up in the traits table by instance name.

## Running

Paths are relative to this directory. Start it from a tmux session.

```bash
alias lap_run='perl /humgen/diabetes/users/chase/lap/trunk/bin/run.pl --meta config/pigean_collection.meta'

# 0. the meta (lists the run on S3; rerun after the run or the mirror changes)
python3 scripts/build_meta_yaml.py
PYTHONPATH=/humgen/diabetes2/users/chase/packages/meta-sanity python3 -m meta_sanity.generate_meta \
    config/pigean_collection.meta.yaml config/pigean_collection.meta

# 1. pinned pigean (once)
git clone --no-checkout /humgen/diabetes2/users/chase/packages/pigean raw/pigean_ca59661
git -C raw/pigean_ca59661 checkout --detach ca59661644dc9ead429fc7e59050870ba49b26e8
git -C raw/pigean_ca59661 apply ../../patches/pigean_ca59661_correlation_cap.patch

# 2. link inputs and create directories; --check only prints the commands
lap_run --init && lap_run --mkdir && lap_run --check

# 3. the stages (never add --only: the collect fan-in would shrink)
lap_run --only-cmd '^(source_|genesets_)'              # head node
lap_run --only-cmd '^(betas_|format_|collect_|provenance_)' --bsub
lap_run --only-cmd '^cfde_'

# 4. deposit, by hand: the dry runs first, then the uploads
lap_run --only-cmd '^deposit_plan_'
lap_run --only-cmd '^deposit_upload_'
```

Tests: `/humgen/diabetes2/users/chase/projects/pigean/.venv/bin/python -B -m unittest discover -s scripts/tests -v`

Always name the stages with `--only-cmd`. A run without it would also run the deposit.

**Deposit.** Of the PIGEAN collection, `data/` is what the claims CLI reads: `source.yaml` gets `location:
s3://kpn-opendore-releases/collections/pigean-cfde/2026.11/data/` and `path: "{library}/{trait}/{kind}.json"`. The
upload refuses to overwrite an object that differs (the manifests under `run/` excepted) and skips the ones already
there, so it resumes. A rerun of PIGEAN
or of the betas goes to a new `release_prefix` (in the cfg) and becomes a new release.

To mirror the CFDE release again:

```bash
D=/humgen/diabetes/users/chase/data/dig-s3/gene_sets/cfde/$(date +%F)
aws s3 sync s3://dig-gene-set-data/v2.0_dapper-0.2.0-a1/ $D/ --only-show-errors --exclude '*' \
    --include '*/genesets.dapper-ids.gmt' --include '*/genesets.gmt' --include '*/geneset.meta.json' \
    --include '*/geneset.provenance.dapper.yaml' --exclude 'LINCS_L1000/*'
python3 scripts/build_meta_yaml.py --cfde-snapshot $(date +%F)
```
