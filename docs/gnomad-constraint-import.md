# gnomAD constraint annotations

The importer stores the supplied gnomAD v4.1 transcript constraint snapshot and a reproducible gene-symbol projection. These annotations complement EAGGL loadings; they are not probabilities that a gene causes a disease or that a particular variant loses function.

| Stored field | TSV column | Meaning |
| --- | --- | --- |
| `pli` | `lof.pLI` | Probability of loss-of-function **intolerance**, from the high-confidence pLoF model; higher values indicate stronger evidence for the intolerant class. |
| `loeuf` | `lof.oe_ci.upper` | Upper bound of the 90% confidence interval for the high-confidence pLoF observed/expected ratio; lower values indicate stronger constraint. |
| `lof_oe` | `lof.oe` | Observed/expected high-confidence pLoF ratio. |
| `mis_z` | `mis.z_score` | Missense constraint Z score; more positive values indicate stronger constraint. |

Definitions and transcript flags come from the [versioned gnomAD v4.1 README](https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/constraint/README.txt). The [v4.1 release notes](https://gnomad.broadinstitute.org/news/2024-05-gnomad-v4-1-updates/) retain the [v4.0 constraint caveats](https://gnomad.broadinstitute.org/news/2024-03-gnomad-v4-0-gene-constraint/), including experimental metrics, high-coverage exome regions, and autosomal coverage. This import is explicitly v4.1, not a later release. Constraint flags are retained alongside reported values; the importer does not invent replacement scores or apply clinical thresholds.

## Transcript selection and exact symbols

Policy `gnomad-ensembl-mane-canonical-exact-symbol-v2` first identifies all Ensembl gene IDs for each exact, case-sensitive source symbol. It selects a transcript only when that symbol maps to one Ensembl gene ID: exactly one MANE Select transcript wins; if none is marked MANE, exactly one canonical transcript wins. This follows the release's MANE/canonical priority while making the Ensembl namespace an explicit application choice. Selection does not depend on the metric values or file row order.

Both Ensembl and RefSeq transcripts remain in the raw table. A RefSeq transcript never fills a missing Ensembl metric. Missing `NA` metrics become SQL/JSON null, including on a successfully selected transcript. A source symbol of `NA` becomes null and is excluded from the symbol projection. No case folding, fuzzy matching, historical-symbol substitution, or Ensembl/NCBI equivalence is inferred.

| `selection_status` | `selection_reason` | Result |
| --- | --- | --- |
| `selected` | `mane_select` or `canonical` | Selected transcript and its original metrics/flags. |
| `ambiguous_gene` | `multiple_ensembl_gene_ids` | Candidate IDs retained; metrics and chosen transcript null. |
| `ambiguous_transcript` | `multiple_mane_select` or `multiple_canonical` | Gene ID retained; metrics and chosen transcript null. |
| `no_primary_transcript` | `no_mane_or_canonical` | Gene ID retained; metrics and chosen transcript null. |
| `no_ensembl_gene` | `refseq_only` | RefSeq candidate IDs retained; metrics and chosen transcript null. |

A factor gene absent from the TSV has no projection row. The API should distinguish it from a recorded transcript with missing metrics, and sort missing pLI values last rather than as zero.

## Schema and isolation

[Migration 009](../schema/migrations/009_gnomad_constraints.sql) creates four additive tables:

- `gnomad_constraint_imports`: immutable import manifest, version, declared source URL, file SHA-256, selection policy and status.
- `gnomad_constraint_transcripts`: every transcript row, selected numeric columns, and exact TSV field strings in `raw_metrics` (`NA` becomes null). Original file bytes are identified by the source checksum. Numeric and boolean strings remain strings in the provenance JSON; parsed metrics remain numbers in their dedicated columns.
- `gnomad_gene_constraints`: the exact-symbol projection, selected transcript, metrics, flags, candidate IDs and selection outcome.
- `gnomad_constraint_active`: one pointer per application `table_prefix`. Local, QA and production share immutable annotation tables but have independent active imports.

The import ID hashes source version, declared source URL, file SHA-256 and selection policy. `source_url` records the operator's provenance assertion; reading a local file does **not** verify its checksum against the remote server. The manifest also records row counts and independent normalized content hashes for both tables.

No table migration or remote read happens during dry-run. Migration is an explicit separate command because MySQL DDL commits independently. Data loading, digest/count verification, transition to `ready`, and optional scoped activation happen in one transaction. Failed inserts or verification roll back the complete data import and preserve existing pointers. Named locks prevent competing imports/activations. Repeating `load` rehashes stored contents and becomes a no-op when already active. It never deletes old imports or repairs conflicting immutable data silently.

Use `activate` to promote an existing verified import into another environment sharing the database. It checks `ready` status, manifest format and supported selection policy, agreement between source metadata and the manifest, the recomputed import identity, valid recorded digests and indexed transcript/gene counts. It then updates only the selected environment's pointer in one transaction, using the same import and environment locks as `load`. Repeating activation is idempotent. It needs no TSV file, does not ingest or rewrite scientific records, and does not transfer transcript provenance JSON.

Activation relies on the immutable import's successful ingestion verification; it does **not** rehash content. Its receipt explicitly records `content_rehashed: false`, `content_verification: previously_verified_at_ingestion` and the previously verified content digests. This distinguishes routine environment promotion from a full content audit: use `load` again when you need to detect changes to row contents as well as metadata and counts. An activation dry-run is an offline plan and reports `database_checked: false`.

Policy v2 preserves raw numeric spellings because native MySQL JSON can round tiny probabilities by one floating-point step, while the dedicated DOUBLE columns preserve the parsed values. The earlier v1 attempt failed its exact read-back check and rolled back before activation. Verification remains exact; no numerical tolerance or skipped rows were introduced. Typed signed zeros normalize to zero, with their source spelling retained in the raw fields. The CLI reports insert and verification progress to stderr, including final counts and digest verification.

## Commands

Validate the supplied file without connecting to a database:

```sh
.venv/bin/python scripts/import_gnomad_constraints.py load \
  --input gnomad.v4.1.constraint_metrics.tsv \
  --table-prefix reveal_workflow_local \
  --report .runtime/gnomad-annotations/dry-run.json
```

The explicit local apply commands are:

```sh
.venv/bin/python scripts/import_gnomad_constraints.py migrate \
  --env-file .runtime/workflow/backend.env \
  --ca-file .deployment-assets/rds-ca.pem --apply

.venv/bin/python scripts/import_gnomad_constraints.py load \
  --input gnomad.v4.1.constraint_metrics.tsv \
  --env-file .runtime/workflow/backend.env \
  --ca-file .deployment-assets/rds-ca.pem \
  --table-prefix reveal_workflow_local --apply \
  --report .runtime/gnomad-annotations/local-import.json
```

The host certificate override is needed because the local runtime environment file contains the container path `/app/runtime-ca.pem`. The explicit environment file is authoritative for database settings; its application prefix must match the activation argument. Without `--table-prefix`, an apply imports without activating anywhere. Without `--env-file`, only the shell's existing database settings are used. Credentials never enter the manifest or report.

After the initial local import is ready, preview its promotion into QA with the exact import ID and explicit QA scope:

```sh
.venv/bin/python scripts/import_gnomad_constraints.py activate \
  --import-id 98bcd909753bf26b8d0b119972dac27f5f33803d7a323ff6c8de99bf1660be17 \
  --env-file .runtime/workflow/qa-backend.env \
  --ca-file .deployment-assets/rds-ca.pem \
  --table-prefix reveal_workflow_qa
```

Add `--apply --report .runtime/gnomad-annotations/qa-activation.json` to perform the checked, atomic QA activation. Unlike optional activation during `load`, `activate` requires both `--import-id` and `--table-prefix`. The QA environment file must match the target prefix. The existing local pointer and any production pointer are preserved.

The supplied 95,546,041-byte file has SHA-256 `68d8abdb7fc48f570869b02dfaa74b9fecaece7fcc5f301ddca40ec1ce12da00`: 211,523 transcript rows (100,967 Ensembl; 110,556 RefSeq), 18,203 named symbol records and 18,105 selected transcripts. Five symbols are ambiguous (`MATR3`, `PINX1`, `POLR2J3`, `SIGLEC5`, `TBCE`); five lack a primary Ensembl transcript; 88 are RefSeq-only. These counts describe this exact file, not universal gnomAD coverage.

A versioned bulk snapshot makes global factor sorting and paginated results reproducible without fetching a remote annotation separately for every gene. The application pins the import ID across pages; an activation change must not mix two annotation versions within one result. Live browser/API annotations are useful for inspection, but are not substituted into this frozen projection.

Run the focused local tests with:

```sh
.venv/bin/python -m pytest services/backend/tests/test_gnomad_constraints_import.py -q
```

Tests exercise transcript ambiguity, missing values, namespace separation, source identity, dry-run, environment mismatch, idempotence, digest verification, ready-import promotion and transaction rollback using SQLite with MySQL statement adaptation. They do not certify MySQL engine behavior; an initial MySQL load verifies persisted counts and content before marking the import ready. The large source TSV is an input artifact and should not be added to Git.
