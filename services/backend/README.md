# REVEAL backend components

Implemented components include the embedding client supplied for this project, packaged in [embedding_client.py](src/reveal_backend/embedding_client.py), and the [EAGGL bundle importer and retrieval helpers](../../docs/eaggl-factor-import.md). The REST API/EC2 worker and Box runner are planned in [the design](../../docs/design-plan.md); they are not running yet.

The [evidence collector and deterministic builder](../../docs/evidence-package-builder.md) accept a DisMech gap ID and EAGGL factor IDs, call the CFDE interactive/BioIndex APIs, and produce a frozen DAPPER-aware evidence package. Install with `python -m pip install -e 'services/backend[evidence]'`, then use `scripts/build_evidence_package.py collect` or `replay` from the repository root. No database credentials or agent keys are required.

The [agent startup and scientific-account linter](../../docs/scientific-account-linting.md) clone a locked DAPPER release per agent start, install the skill and lint script, and return structured findings. `validate_scientific_account` reuses this linter in final mode for backend acceptance checks. The local helpers are implemented; Box provisioning and worker deployment remain planned.

## Install and configure

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e services/backend
```

Set `EMBEDDING_SERVICE_API_KEY` in the backend's environment when available. The client does not load `.env` files automatically. [Configuration names](.env.example) include empty secret fields, never live credentials.

The EAGGL CLI automatically loads the repository-root `.env`; its [template](../../.env.example) includes the embedding key and Aurora settings. Existing shell variables and explicit CLI options take precedence. The direct Python client above continues to read only its process environment and arguments.

Defaults:

- URL: `https://embedding-service-27386110942.us-east1.run.app` (`EMBEDDING_SERVICE_URL` override).
- Model: `pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb` (`EMBEDDING_MODEL` override).
- Provider: `huggingface`.
- Batches: at most 100 texts; one concurrent request by default. Configure `max_workers` explicitly for import jobs.

```python
from reveal_backend.embedding_client import get_embeddings, cosine_similarity, DEFAULT_MODEL

# Reads the service key from EMBEDDING_SERVICE_API_KEY.
vectors = get_embeddings(["Insulin secretion", "Pancreatic beta-cell dysfunction"])
similarity = cosine_similarity(vectors[0], vectors[1])

# Existing explicit calls remain supported.
vectors = get_embeddings(
    texts=["AIP-AHR transcriptional signaling"],
    model=DEFAULT_MODEL,
    service_url="https://embedding-service-27386110942.us-east1.run.app",
    api_key=service_key,  # supplied by the caller's secret configuration
    max_workers=1,
    max_retries=2,
    timeout=30,
)
```

## Supplied service contract

`POST /embed`, JSON content type, `X-API-Key` authentication:

```json
{"texts":["mechanism text"],"model":"pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb","provider":"huggingface"}
```

Expected response: `embeddings` as a numeric matrix, `dimensions` as a positive integer, and `count` equal to the number of texts. This contract was verified against the authenticated live service on September 25, 2026: [3,103 EAGGL labels returned valid 768-dimensional vectors](../../data/eaggl/2026-09-25/embedding-generation.json). The client returns a float32 matrix in input order and rejects missing rows, ragged/mismatched dimensions, nonnumeric/nonfinite values, and dimensions that change between batches. Empty input returns shape `(0, 0)` without a request.

Adaptations from the supplied code:

- Added the requested default model/URL and environment-key configuration while preserving explicit argument usage.
- Retained adaptive concurrency and transient retries; fixed the throttle so it respects worker counts below four and can back off to one.
- Validated batch/response contracts to prevent misalignment between source records and vector rows.
- Preserved input order when concurrent batches finish out of order.
- Kept TLS certificate verification and omitted HTTP error bodies from logs.
- Made cosine result shapes follow the input ranks: two matrices produce a matrix, including a `(1, 1)` result; two vectors produce a scalar. Zero vectors produce similarity zero.

`timeout` is **per HTTP attempt**, not a total job deadline. The future worker must enforce the overall budget/cancellation policy. Import defaults retain the supplied retry count; use smaller explicit values for interactive suggestions. The model card reports 768 dimensions, but the client validates the actual returned dimension rather than hardcoding it; ingestion must additionally enforce one model/index dimension. [Model card](https://huggingface.co/pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb).

## Offline tests

```bash
.venv/bin/python -m unittest discover -s services/backend/tests -p 'test_*.py'
```

Tests use mocked HTTP responses, including transient/auth failures, invalid matrices, and out-of-order parallel completion. They do not consume service quota or require a key.

## EAGGL factor bundles

The [EAGGL importer](../../docs/eaggl-factor-import.md) adds bundle validation,
resumable factor-name embeddings, Aurora persistence of all nonzero gene loadings
and the supplied hierarchy, and on-demand cosine search. Install its dependencies
with `python -m pip install -e 'services/backend[eaggl]'` from the repository root.
Use `scripts/import_eaggl_factors.py` for `prepare`, `embed`, `load`, and `search`.
The Python `database_search_index` helper loads a reusable index directly from
completed MySQL imports. The supplied legacy bundle has been loaded into Aurora,
and [read-back plus database-backed semantic search passed](../../data/eaggl/2026-09-25/database-verification.json).
The REST API remains undeployed.

The [CFDE routing crosswalk](../../docs/eaggl-cfde-links.md) links an imported EAGGL
factor to a CFDE factor by exact trait and factor number, ignoring gene/label
differences. `eaggl_cfde_links.lookup_factor` returns the interactive CFDE node ID
and ranked summary gene-set references with resolved DAPPER GeneSet IDs where
available. The CLI is `scripts/link_eaggl_cfde.py`.

## DisMech source imports

The [DisMech importer](../../docs/dismech-import.md) loads the paired mechanism and
knowledge-gap exports, preserving source payloads, statuses, attachment resolution,
file hashes, and source commit. Install with `python -m pip install -e
'services/backend[dismech]'`. Run `scripts/import_dismech.py load` for offline
validation, add `--apply` to load MySQL, or use `verify` for complete read-back.
It shares the EAGGL importer's verified-TLS connection helper and root `.env`.
It does not call the embedding service. The [Prisma schema](../../schema/prisma/README.md)
covers both existing tables and the new DisMech migration.
