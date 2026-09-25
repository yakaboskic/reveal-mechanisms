# EAGGL bundle → factor database and name embeddings

[`scripts/import_eaggl_factors.py`](../scripts/import_eaggl_factors.py) imports a capped EAGGL share bundle or equivalent individual files. It freezes the source locally, embeds the natural-language **factor labels** through the existing lab embedding client, and loads factors, gene weights, the supplied hierarchy, and reusable vectors into the existing Aurora MySQL project database. Similarities are computed at query time; no all-pairs semantic edge table is generated.

The supplied `EAGGL_capped_union_graph_share` is explicitly the **legacy 711-trait atlas**. It contains 4,037 factors, 3,103 distinct labels, 18,477 genes, 2,553,330 nonzero loadings, 5,682 graph nodes, and 8,535 parent-to-child hierarchy edges. Its factor IDs are not automatically aliases for the current `cfde-inc-v2` API. This is a source-specific persistence adapter; it does not mint DAPPER objects or assert equivalence to DisMech mechanisms.

The separate [application crosswalk](eaggl-cfde-links.md) now maps exact trait and
factor-number matches to `cfde-inc-v2`, as requested for application routing.
That mapping deliberately does not require label or gene agreement.

## Run

Run commands from the repository root. Use Python 3.10 or later:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements-eaggl.txt

# For a fresh checkout, copy .env.example to .env (preserve any existing .env).
# Fill EMBEDDING_SERVICE_API_KEY and REVEAL_MYSQL_PASSWORD in .env.
# Set REVEAL_MYSQL_CA_FILE to your trusted RDS CA bundle path when needed.

# Validate/freeze the bundle. No database access or embedding calls.
.venv/bin/python scripts/import_eaggl_factors.py prepare \
  --bundle /Users/cyakaboski/Downloads/EAGGL_capped_union_graph_share \
  --source-version legacy-711-trait-capped-union \
  --output data/eaggl/captures/legacy-711

# Preview database scope without credentials or writes.
.venv/bin/python scripts/import_eaggl_factors.py load \
  --output data/eaggl/captures/legacy-711

# The importer reads the repository-root .env automatically.
.venv/bin/python scripts/import_eaggl_factors.py embed \
  --output data/eaggl/captures/legacy-711

# Write to the existing project database. Prompts without echoing for the
# password unless REVEAL_MYSQL_PASSWORD is already configured.
.venv/bin/python scripts/import_eaggl_factors.py load \
  --output data/eaggl/captures/legacy-711 \
  --database cyaka_reveal_mechanisms \
  --apply
```

`load` is a dry run unless `--apply` is passed. The database must already exist. Its default host/user/database match the GeneSet importer. The CLI automatically reads the repository-root `.env`, regardless of your working directory. See [.env.example](../.env.example) for embedding configuration and `REVEAL_MYSQL_HOST`, `REVEAL_MYSQL_PORT`, `REVEAL_MYSQL_USER`, `REVEAL_MYSQL_DATABASE`, `REVEAL_MYSQL_PASSWORD`, and `REVEAL_MYSQL_CA_FILE`. Existing shell variables take precedence over `.env`; explicit CLI options (`--host`, `--port`, `--user`, `--database`, `--ca-file`) override those defaults. Values are parsed without shell execution or variable interpolation. The private `.env` is ignored by Git, and credentials are not written into the capture.

Database names must have the literal `cyaka_` prefix. TLS certificate and hostname verification remain enabled. Set `REVEAL_MYSQL_CA_FILE` to the absolute path of a trusted RDS CA bundle if your Python trust store does not already trust the server certificate; a blank value uses the system trust store. Direct Python component calls do not load `.env`; supply their configuration through the process environment or explicit arguments.

The embedding command honors `EMBEDDING_SERVICE_URL` and `EMBEDDING_MODEL`, with the existing lab URL and BioBERT defaults. `--service-url`, `--model`, and `--provider` override these. `--batch-size` is at most 100 names (default 100), with one concurrent request and three transient retries per batch by default. `--max-retries` and `--timeout` control retries and per-attempt timeouts. Successful batches are committed before the next request. Repeating `embed` resumes unfinished names; repeating a completed run makes no service calls.

Use `--model-revision` to record a known deployed model revision. The service wire contract accepts model/provider but no revision pin; `unspecified` is recorded if none is supplied. A deployment changing weights under the same model name cannot be detected from dimensions alone. When changing a deployment, use a new revision value to generate a new run, and query it with that same deployment. Revisions, models, endpoints, providers, templates, and source imports never share an embedding run.

## Equivalent files

A bundle root (with `data/`) or the data directory itself is accepted. Preparation prefers the CSR NPZ matrix when present, falling back to the wide `.tsv.gz` file. You can override every file individually:

```bash
.venv/bin/python scripts/import_eaggl_factors.py prepare \
  --metadata /path/to/factor_metadata.tsv \
  --loadings /path/to/capped_factor_gene_loadings.npz \
  --factor-ids /path/to/factor_ids.tsv \
  --genes /path/to/genes.tsv \
  --graph /path/to/index.html \
  --source-namespace eaggl \
  --source-version colleague-build-2026-09-24 \
  --output data/eaggl/captures/another-build
```

Requirements:

- Metadata is a TSV with `factor_id`, `trait`, `factor`, and nonblank `label`. Additional fields are retained verbatim. Metadata is joined by exact factor ID, so its row order may differ from the matrix.
- NPZ must be canonical SciPy CSR. `factor_ids.tsv` has a `factor_id` column; `genes.tsv` has a `gene` column. Their row orders define the matrix axes.
- Alternatively, `--loadings` accepts a wide `.tsv` or `.tsv.gz`: the first header is `factor_id`, the remaining headers are gene IDs, and every row has all numeric values. Separate axis files are optional; if supplied, they must match exactly.
- IDs are case-sensitive. Missing/duplicate identifiers, mismatched scopes or shapes, nonfinite values, and values outside `[0,1]` are errors. The importer does not silently clip, normalize, threshold, or select only top genes.
- `--graph` is optional for individual files. It accepts the supplied explorer HTML or its `DATA` JSON object. JSON is extracted without executing JavaScript. Leaves must cover all factors exactly once; the graph must be a consistent single-root DAG with valid reciprocal parent/child references. Group nodes remain group nodes; only factor labels are embedded.
- If the bundle has `SHA256SUMS.txt`, consumed files listed there must match. Source hashes are captured even without a checksum manifest. Cap-audit files and the analysis summary are copied into the local capture when supplied. Unused scripts and preview images are not executed or imported.

Use a new output directory for a new capture. Its import ID includes source namespace, explicit source version, input hashes and adapter format, so two builds cannot collide just because both have `Factor1`. Factor identity is `(import_id, exact factor_id)`; equal labels can share an embedding but remain distinct factors. Moving a frozen capture preserves its identity. Keep captures backed up if their cap-audit files are needed later.

## Search on demand

```bash
# Embeds just this query and searches all factors in the selected run.
.venv/bin/python scripts/import_eaggl_factors.py search \
  --output data/eaggl/captures/legacy-711 \
  --query 'pancreatic beta-cell dysfunction' --top-k 10

# Uses a stored factor vector; this makes no embedding-service request.
.venv/bin/python scripts/import_eaggl_factors.py search \
  --output data/eaggl/captures/legacy-711 \
  --factor-id '22q112_deletion_syndrome_Orphanet_567::Factor1' --top-k 10
```

`--trait` applies an exact trait filter. Results contain the original factor ID, trait, label, cosine similarity, import ID, and embedding-run ID. Factor-to-factor search excludes the source factor itself and retains other factors with the same label. If more than one embedding run is complete in a capture, select one with `--run-id` for `search` and `load`.

The backend can load a small reusable index **from MySQL** once and serve many queries without reloading gene weights:

```python
from reveal_backend.eaggl_database import connect
from reveal_backend.eaggl_embeddings import database_search_index

connection = connect(ca_file='/path/to/verified-rds-ca-bundle.pem')
try:
    index = database_search_index(connection, import_id, run_id)
finally:
    connection.close()

matches = index.search(query='T cell activation', top_k=10)
neighbors = index.search(factor_id=source_factor_id, top_k=10)
```

Only complete imports and complete embedding runs are searchable. This is a Python retrieval component and CLI, not a deployed REST endpoint. Gene-loading cosine and name-embedding cosine are different quantities; the original hierarchy continues to represent the colleague's loading-based analysis. Semantic proximity does not establish biological equivalence or a causal relation.

## Storage and replay

[Migration 002](../schema/migrations/002_eaggl_factors.sql) adds eight source-specific tables without modifying the existing GeneSet tables:

| Table | Contents |
| --- | --- |
| `eaggl_imports` | Immutable capture manifest, explicit source version, import status and transactional checkpoints |
| `eaggl_factors` | Exact factor IDs, labels, trait context, input hashes and all source metadata |
| `eaggl_genes` | Complete, ordered gene axis |
| `eaggl_gene_loadings` | Every nonzero capped factor/gene weight as `DOUBLE` |
| `eaggl_graph_nodes` | Source node IDs, factor links, and supplied node JSON including group summaries |
| `eaggl_graph_edges` | Original parent-to-child hierarchy edges |
| `eaggl_embedding_runs` | Service/model/revision/template configuration, dimension and completion state |
| `eaggl_name_embeddings` | Exact input text/hash and unnormalized little-endian float32 vector/checksum |

Within a complete import's factor/gene axes, an omitted loading row means zero. A gene absent from the axis is outside the source scope. Original cap audits remain in the frozen local capture, with their hashes in the database manifest; they are not separate uncapped-loading database rows. Graph group summaries are preserved as supplied; no new full group-by-gene matrix is generated.

Database imports commit bounded batches together with their progress counters. A failed batch rolls back, and rerunning resumes the last committed checkpoint. An import-specific MySQL advisory lock prevents simultaneous loaders from racing. The CLI also locks the local capture during embedding/loading/search. Replaying checks manifest compatibility and every stage count, and reads back every stored embedding exactly. It never truncates, replaces, or deletes existing source snapshots. Database batch size defaults to 5,000 rows and is independent of embedding batch size.

Source completion and embedding completion are separate: a failure during vector loading leaves the source loaded but the embedding run unavailable for search. The loader currently requires a complete local embedding run before applying the capture. A successful load writes `database-load.json` with the import/run IDs and counts.

## Verification

```bash
PYTHONPATH=services/backend/src python3 -m unittest discover \
  -s services/backend/tests -p 'test_*.py'
```

The suite covers source alignment, sparse weights (including subnormal values), malformed bundles and graph references, artifact checksums, actual embedding-client HTTP serialization with mocked responses, duplicate-label caching, interruption/resume, dimension drift, model-revision separation, query ranking, database checkpoint rollback, completed replay, and database-index retrieval. Database transaction tests use a SQLite adapter for local inserts/constraints/commit/rollback; they do not certify the MySQL engine, Aurora connectivity or a live authenticated service call.

The provided bundle was fully prepared and its NPZ/TSV matrices compared at the original float32 precision. The live run completed on **September 25, 2026**: the lab service generated **3,103 unique-label embeddings of 768 dimensions**, and Aurora loaded **4,037 factors**, **18,477 genes**, **2,553,330 nonzero loadings**, **5,682 graph nodes**, and **8,535 hierarchy edges** as `cyaka` over certificate-verified TLS. See the [embedding report](../data/eaggl/2026-09-25/embedding-generation.json), [database-load report](../data/eaggl/2026-09-25/database-load.json), and [independent read-back report](../data/eaggl/2026-09-25/database-verification.json).

The live read-back compared all factor metadata, gene-axis entries, graph node payloads, graph edges, and every embedding byte with the frozen capture. Every table count and the loading minimum, maximum and sum matched; **8,777 individual weights across 21 factors** also matched exactly, including the smallest nonzero value. On-demand semantic-search results from the database index matched the local index. The earlier GeneSet import still reports 801,934 rows and complete status. The 30 offline tests also passed in the installed project virtual environment before execution.

The completed local capture and embedding cache are available at `data/eaggl/captures/legacy-711` in this workspace. You can run `search` immediately; repeating `embed` uses the cached vectors. Captures are ignored by Git because they contain large source artifacts and derived vector caches; the compact live-run reports above are retained separately.
