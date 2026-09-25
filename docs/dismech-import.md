# DisMech database import

`scripts/import_dismech.py` imports the existing paired DisMech exports into
source-specific MySQL tables. It preserves all exported fields in JSON payloads,
with indexed projections and foreign keys for navigation. These remain DisMech
source records; this importer does not mint DAPPER KnowledgeGap/Claim identities
or generate embeddings.

## Run

From the repository root:

```bash
.venv/bin/python -m pip install -r scripts/requirements-dismech.txt

# Offline: validate both snapshots, all checksums and cross-record references.
.venv/bin/python scripts/import_dismech.py load

# Create the nine DisMech tables and populate the configured AWS database.
.venv/bin/python scripts/import_dismech.py load --apply \
  --report data/dismech/database-load.json

# Read-only: compare every projected column and full payload with the exports.
.venv/bin/python scripts/import_dismech.py verify \
  --report data/dismech/database-verification.json
```

The root `.env` supplies the same `REVEAL_MYSQL_*` settings as the EAGGL importer,
including password and CA bundle. No embedding API key is needed. Shell variables
override `.env`; explicit CLI arguments override both. Only `cyaka_` database
names are accepted, and the Python connection verifies TLS and hostname identity.
No database is created automatically. `load` without `--apply` never connects.

Use `--source DIR --gaps DIR` for another paired export. The default directories
are `data/dismech` and `data/dismech-gaps/2026-09-24`, resolved relative to the
repository, independent of the caller's working directory. `--batch-size` defaults
to 500. The optional `--report` writes a credential-free result.

## Input dependencies

The mechanism export directory, produced by `scripts/extract_dismech.py`, must contain:

- `manifest.json`, `source-files.json`, `schema-vocabularies.json`
- `entities.jsonl.gz`, `mechanisms.jsonl.gz`, `causal-edges.jsonl.gz`
- `hypotheses.jsonl.gz`, `ontology-terms.jsonl.gz`, `vocabulary.jsonl.gz`

The gap export directory, produced by `scripts/extract_dismech_gaps.py`, must contain:

- `manifest.json`, `source-files.json`
- `discussions.jsonl.gz`, `knowledge-gaps.jsonl.gz`, `gap-attachments.jsonl.gz`

The original DisMech checkout and its Python resolver are needed only to make new
exports. Importing an existing export needs neither. Preserve the accompanying
`SOURCE-LICENSE.txt` files with distributed exports. The importer reads the SQL
file `schema/migrations/003_dismech.sql` from this repository when applying a load.

Both manifests must report a complete extraction without errors. Their commits
and KB source-file SHA-256 inventories must match, even if retrieval dates differ.
Every compressed export must match its manifest checksum and row count. The gap
file must equal the exact gap subset of discussions, and attachment rows must
cover every source `attaches_to` reference exactly once. Duplicate IDs, dangling
document/mechanism references, and inconsistent resolved targets fail validation
before database access.

## Stored snapshot

The current export contains:

| Table | Rows | Meaning |
| --- | ---: | --- |
| `dismech_documents` | 3,361 | Exported document metadata and source-file hashes |
| `dismech_mechanisms` | 19,959 | Source mechanism occurrences, including nested occurrences |
| `dismech_causal_edges` | 37,149 | Source downstream assertions; target references remain unresolved |
| `dismech_hypotheses` | 1,025 | Exported mechanistic hypotheses |
| `dismech_ontology_terms` | 21,382 | Observed ontology terms, labels, and occurrence provenance |
| `dismech_vocabulary` | 54,690 | Observed vocabulary entries and occurrence provenance |
| `dismech_discussions` | 4,051 | All discussions, including **3,367 gaps** |
| `dismech_gap_attachments` | 6,422 | Source references and their recorded resolution |

`dismech_imports` stores the immutable import ID, both original manifests, source
file inventories, schema vocabulary definitions, progress, and completion status.
There are 148,039 source rows plus the import record; the gap subset is not stored
twice. Documents retain the *exported metadata*, not entire original YAML files.
All fields present in the other exported rows, including evidence, proposed
experiments, notes, rationale, and resolution notes, survive in `payload`.

Gaps are discussions with `is_gap = 1`: 2,604 `KNOWLEDGE_GAP` and 763
`HUMAN_MODEL_MISMATCH`. Absent statuses remain SQL `NULL`; resolved source gaps
remain resolved. The 10 ambiguous attachments keep their ambiguity and candidate
count in the payload. Whole-document/section references are retained as such.
Only an exact resolved mechanism target gets `target_mechanism_sha256`; references
to treatments, phenotypes, and other unprojected sections keep their source pointer
without a fabricated mechanism relation.

Source IDs are retained verbatim; SHA-256 hashes index them without SQL key-length
limits. Keys are scoped by `import_id`, so changed snapshots coexist rather than
overwriting earlier records. Hypothesis IDs are document-plus-JSON-pointer locators.
These IDs and mechanism labels do not assert global biological equivalence.

## Resume and verify

Each batch and its checkpoint commit in the same transaction. An interrupted
batch rolls back; rerun the identical command to resume. The loader takes an
advisory lock per database/import. It checks every already-committed row against
the export before resuming, and verifies every newly completed stage before
marking the import complete. Rerunning a complete import performs verification
without duplicate inserts. Changed source bytes produce a new import ID.

Read from a specific completed import, not from an unfiltered mixture of revisions:

```sql
SELECT d.source_id, d.kind, d.status, d.prompt, a.source_reference,
       a.resolution, a.target_id
FROM dismech_discussions d
JOIN dismech_imports i ON i.import_id = d.import_id AND i.status = 'complete'
LEFT JOIN dismech_gap_attachments a
  ON a.import_id = d.import_id AND a.gap_sha256 = d.id_sha256
WHERE d.import_id = ? AND d.is_gap = 1;
```

MySQL DDL is not transactional. A failure during table creation can leave empty
tables, and an interrupted load can leave committed rows with status `loading`.
Retry safely; do not manually delete rows or alter checkpoints to force completion.

## Validation performed

The current snapshot passed [offline validation](../data/dismech/database-dry-run.json)
and a [full import/read-back in disposable MySQL 8.0.42](../data/dismech/database-local-verification.json).
This is **not an Aurora import**. No DisMech production tables or records have been
created by this implementation task.

```bash
.venv/bin/python -m unittest discover -s services/backend/tests -p 'test_dismech.py'

# Optional integration tests: disposable local MySQL only, fixed test DB name.
# Install PyMySQL[rsa] for the test server's default caching_sha2_password auth.
# REVEAL_TEST_MYSQL_PASSWORD defaults to local-test-only; never use production secrets.
REVEAL_TEST_MYSQL_PORT=3307 .venv/bin/python -m unittest discover \
  -s services/backend/tests -p 'test_dismech_mysql.py'
```

The integration tests expect an existing database named `cyaka_dismech_test` on
127.0.0.1. They cover idempotent replay, rollback/resume, retained null statuses,
ambiguous references, foreign keys, same-count corruption, and concurrent loaders.

The companion [Prisma schema](../schema/prisma/README.md) maps all existing tables
and the nine added by this importer. SQL migration files remain authoritative.
