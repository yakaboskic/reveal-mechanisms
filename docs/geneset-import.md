# CFDE → DAPPER GeneSet inventory

**Scope:** all gene-set keys advertised by the CFDE BioIndex for `cfde-inc-v2`. This first import creates durable GeneSet identities and source mappings, so an account/claim can already trace its CFDE gene-set context to a DAPPER object. Original scientific construction provenance and full membership are later enrichment stages.

## Collected and encoded

- The full `pigean-gene-set/2` catalog contains 1,174,835 gene-set/model pairs; **801,934** belong to `cfde-inc-v2`.
- All 801,934 selected keys were encoded as GeneSet objects and validated with a closed JSON Schema generated from the pinned DAPPER schema. Unknown fields are rejected.
- The importer uses DAPPER's actual `compute_id` implementation. Its captured dependency passed **9/9 identity test vectors**.
- The `pigean-gene-set-source` query with source `all` returned 72,864 association rows covering only 25,511 distinct sets. That source-summary response is **not** the complete gene-set catalog. Factor top lists and bounded interactive searches are also insufficient for complete enumeration.
- Model filtering and exact case-sensitive source keys are preserved. This is completeness against the advertised index, not every gene set ever produced by CFDE projects or unexposed upstream libraries.
- The offline audit verifies **801,934 unique DAPPER IDs** and resolves **all 71 gene-set aliases** in the captured interactive API examples. See [validation results](../data/cfde-genesets/2026-09-24/validation.json).

Artifacts: [manifest](../data/cfde-genesets/2026-09-24/manifest.json), [compressed full key catalog](../data/cfde-genesets/2026-09-24/geneset-keys.json.gz), [encoded records](../data/cfde-genesets/2026-09-24/records.jsonl.gz), [DAPPER dependency snapshot](../data/cfde-genesets/2026-09-24/dapper/snapshot.json), and [HuBMAP example collection](../data/cfde-genesets/2026-09-24/example-hubmap.dapper.json). Each export line has a `gene_set` DAPPER object and its `source_key`, `node_id`, and `model` mapping. The [encoder source used for this export](../data/cfde-genesets/2026-09-24/importer-at-encoding.py) is preserved with the manifest's script hash; subsequent loader changes do not change the encoded objects.

## Mapping and metadata policy

Each GeneSet contains:

- `id`: computed `dapper:GeneSet.<digest>`.
- `name`: a readable, deterministic formatting of the source key.
- `member_type: gene`.
- `term`: the complete unmodified source key.
- `alternate_identifier`: the interactive `gene_set:...` alias and a model-qualified source alias.
- `term_prefix`: parsed namespace only when all components have the same explicit prefix. A combined set spanning multiple sources does not get an arbitrary single prefix.
- `was_generated_by`: the DAPPER Activity for **catalog encoding**, explicitly distinct from the original experiment or gene-set construction.
- `was_derived_from`: the catalog endpoint; the database mapping additionally pins the exact catalog artifact hash, model, import ID, and dependency snapshot.

The keys endpoint does not supply verified `assay`, `data_type`, `organism`, `genome_build`, `n_genes`, or member identities. These fields are omitted. The example's `bulk`, `transcriptomics`, `human`, `hg38`, and count are not safe defaults for the entire catalog. Association scores and the unverified `n` field from other endpoints are not repurposed as membership counts.

Import coverage explicitly records `membership=not_loaded` and `construction_provenance=not_loaded`. A GeneSet without inline members is allowed by the schema. It is a source-specific catalog record; its initial digest does not claim identity by complete biological membership or equivalence across models.

## Persistence and graph joins

**Loaded and verified:** 801,934 GeneSet objects and aliases, plus one encoding Activity, in `cyaka_reveal_mechanisms`. The [database load report](../data/cfde-genesets/2026-09-24/database-load.json) records verified TLS, the loader version, and a completed replay starting at all 801,934 rows with no duplicates added. The [read-back report](../data/cfde-genesets/2026-09-24/database-verification.json) verifies every alias's model, source key, hash, object class/ID/term, unique object count, the encoding Activity, and exact JSON/checksums for 18 samples distributed through the export.

The [migration](../schema/migrations/001_gene_set_inventory.sql) creates three tables inside the selected project database:

- `dapper_objects`: immutable GeneSet/Activity JSON, DAPPER ID/profile, and payload checksum. Existing content must match before an ID is reused; conflicting content is rejected.
- `gene_set_imports`: import manifest, expected/loaded counts, and `loading`/`complete` state.
- `cfde_gene_set_aliases`: model, exact source key, interactive node ID, DAPPER ID, snapshot/import ID, and provenance/coverage metadata. Digest indexes avoid MySQL key-length limits for long compound gene-set names.

During CFDE graph assembly, resolve `(model, exact gene_set node_id, chosen complete import)` to a DAPPER GeneSet ID. Add that reference to the evidence package and retain the source graph node ID. Claims and supporting evidence can then point to the DAPPER object even while original construction provenance remains unavailable. Missing aliases are explicit gaps; do not manufacture IDs or silently substitute a similarly named gene set. Only imports marked `complete` are eligible as the active mapping.

Future membership/metadata/creation-provenance enrichment uses verified GMT/member artifacts and upstream activity/dataset evidence. Adding hashable content creates a new DAPPER digest. Preserve old records, add a new import/mapping revision, and keep historical account/claim citations pinned to their original object. Do not overwrite old digests or label the import activity as the scientific creation activity.

## Compatibility with the current DAPPER schema

The [v8 integration audit](dapper-integration.md#1-audited-dependency-and-results) scanned field coverage for all 801,934 exported objects and replayed identity plus closed-schema validation on 82 samples and the encoding Activity against the newer DAPPER implementation. All samples retained their IDs. New unhashable `in_gmt_file`/`gmt_entry` fields are absent from this catalog-only import. This does not replace the original full encoding validation or its frozen dependency.

Retain existing records and aliases. Later source-verified enrichment can use GeneSetCollection, GMT File/row references, and membership; the import guide's catalog coverage does not assert those data are already available. DAPPER permits unhashable inverse collection links without reminting a GeneSet, so general object storage will need append-only payload observations rather than extending this fixed export's strict whole-payload replay rule. A single collection listing all 801,934 sets would exceed DAPPER-ID-1's 10,000-triple per-object limit; retain the catalog manifest and model-scoped aliases.

## Run the importer

From the project root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements-genesets.txt

# Collect the entire key catalog, filter the model, freeze DAPPER, mint and validate.
.venv/bin/python scripts/import_cfde_genesets.py encode \
  --model cfde-inc-v2 \
  --dapper-source ~/src/research/dapper \
  --output data/cfde-genesets/NEW_CAPTURE \
  --workers 4

# Verify artifact checksums and show the load scope without writing.
.venv/bin/python scripts/import_cfde_genesets.py load \
  --output data/cfde-genesets/NEW_CAPTURE

# Load into the isolated project database; password is prompted without echoing.
.venv/bin/python scripts/import_cfde_genesets.py load \
  --output data/cfde-genesets/NEW_CAPTURE \
  --database cyaka_reveal_mechanisms \
  --ca-file /path/to/verified-rds-ca-bundle.pem \
  --apply --create-database --bulk --batch-size 20000
```

Host/user default to the supplied Aurora instance and `cyaka`. Database names must have the literal `cyaka_` prefix. The loader verifies TLS, checks export checksums/completeness before writing, commits batches with progress in the same transaction, and resumes committed batches on rerun. It verifies the final alias count before marking an import complete. Replaying a completed import checks it without adding another copy. The loader does not truncate or replace tables.

`--bulk` sends compressed JSON batches over verified TLS and uses MySQL 8 temporary staging tables. It rejects conversion warnings, dropped rows, and conflicting existing objects. Batches are bounded to 48 MiB uncompressed; this mode requires `max_allowed_packet` of at least 64 MiB, as observed on the supplied server. It does not change server configuration or enable local file loading. Omit `--bulk` and use `--batch-size 1000` for ordinary batched inserts. Temporary tables disappear when the connection closes. Transport uses MySQL's documented [compression format](https://dev.mysql.com/doc/refman/8.0/en/encryption-functions.html#function_compress) and [JSON_TABLE](https://dev.mysql.com/doc/refman/8.0/en/json-table-functions.html). Initial development batches used ordinary inserts/local-file staging; the current compressed mode resumes the same import without changing its object identities.

Use a new capture directory to refresh data or the evolving DAPPER dependency. Reusing a directory reuses its captured key catalog and schema snapshot. A changed schema/identity dependency is therefore explicit and does not race against edits in the sibling repository. Encoding is restartable from the cached catalog; interrupted database loading resumes from its committed row count.

On Python installations without configured certificate roots, set `SSL_CERT_FILE=/etc/ssl/cert.pem` for collection. Verification remains enabled. Credentials never belong in exported records or source code.

## Checks

```bash
.venv/bin/python -m unittest discover -s scripts -p 'test_geneset_import.py'
.venv/bin/python scripts/validate_genesets.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  data/cfde-genesets/2026-09-24/dapper/schema/identity/dapper_identity.py verify-vectors
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  data/cfde-genesets/2026-09-24/dapper/schema/identity/dapper_identity.py verify \
  data/cfde-genesets/2026-09-24/example.dapper.json

# Read-only database counts, complete alias invariants, and sampled JSON read-back.
.venv/bin/python scripts/verify_geneset_database.py \
  --ca-file /path/to/verified-rds-ca-bundle.pem
```

Tests cover exact model/case scope, omissions of unknown scientific metadata, compound namespaces, deterministic identities, identity changes after enrichment, closed schema validation, corrupted/incomplete exports, database-prefix validation, immutable-content conflicts, and lossless bulk staging guards. This is catalog/import provenance validation; it does not certify the still-missing original scientific generation history.

Delivered validation: **13 GeneSet tests**, **7 existing ingestion tests**, **10 embedding-client tests**, **9 DAPPER identity vectors**, full local catalog/alias uniqueness checks, 82 sampled GeneSet identity replays, database read-back, and completed-import replay all passed. Every exported GeneSet was schema-validated during encoding.
