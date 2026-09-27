# Persistent DisMech context embeddings

EAGGL factor labels were already embedded during import. DisMech migration 003 imported source text, attachments, and provenance, but did not create vectors. Automatic suggestions therefore had to encode the selected DisMech descriptions at request time before comparing them with the existing EAGGL matrix. Migration 006 and the explicit pipeline below add the missing persistent context vectors. They do not regenerate EAGGL embeddings or change scientific identities.

The embedding target is the exact completed EAGGL run: model, provider, service URL, model revision, dimensions, and configuration are preserved. The current run is `d4c0300978778453c847c7c3447a316e55b1b0cfe469c647f148ecc1a77f57d7`, using `pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb`, 768 dimensions, and the recorded model revision `unspecified`. That revision value does not establish a pinned remote model build. Each new generation session checks three existing EAGGL label vectors against its own fresh outputs before and after embedding, requiring cosine similarity of at least 0.999999. These calibration calls do not replace the stored EAGGL vectors; the observed compatibility check does not prove an immutable remote service build. Local execution records its exact model commit separately from this compatibility target.

The prepared corpus uses exactly the current suggestion inputs:

- Every imported mechanism uses its description, falling back to its name only when the description is absent or empty.
- A gap uses its exact prompt only when none of its attachments resolves to an imported Mechanism. Other attachment kinds do not supply mechanism descriptions.
- Identical text is embedded once. Separate source bindings retain each native ID, source revision, template, and text hash. Context order and duplicate occurrences remain intact when calculating maximum cosine similarity and matched context IDs.

For the verified source commit `df3884a8bba34370ecc0b3a49d956e7f0523bb28` and import `4062563df48bbc8aa418b7add0af083115184e70c1d909e66ab70d505087e395`, the preparation contains 19,959 mechanism bindings and 629 fallback gap bindings. Their text deduplicates to 20,576 vectors: 19,950 mechanism texts and 626 prompts, with no overlap. This is 7,777,930 characters and approximately 63.21 MB of raw float32 vectors before metadata and indexes. A fully remote run with batch size 32 would require 643 embedding requests; batch size 100 would require 206, excluding calibration and retries.

The local capture completed on September 26, 2026: all 20,576 vectors and 20,588 source bindings passed `read_local_vectors` verification. It contains 3,360 earlier remote vectors attributed by an explicit retrospective operator attestation, plus 17,216 locally generated vectors with exact input/vector bindings in a generation session journal. The earlier executions did not record original per-session provenance or final calibration; the journal preserves that limitation instead of inventing it. The final local session completed in 131.887 seconds, including its generation and session calibration; this is one observed run, not a controlled benchmark. Existing EAGGL vectors remain unchanged. Aurora loading and runtime readiness require their separate verification below.

The narrower set used by all current gap suggestions contains 3,560 unique linked-mechanism texts and 626 fallback prompts. Preparing the full mechanism corpus also covers imported mechanisms that are not currently attached to a gap. None of the 4,186 current suggestion texts is already an exact text match in the existing 3,103-vector EAGGL label store.

## Explicit preparation and backfill

Run commands from the repository root with the backend dependencies installed. Database and embedding credentials are read from the ignored root `.env` or process environment, never passed on the command line. The default capture location, `.runtime/dismech-embeddings`, is ignored. Keep it for resume and independent verification; commit only appropriate compact validation reports.

Prepare by reading the completed EAGGL target and its calibration probes from Aurora over verified TLS:

```sh
.venv/bin/python scripts/import_dismech_embeddings.py prepare \
  --eaggl-run-id d4c0300978778453c847c7c3447a316e55b1b0cfe469c647f148ecc1a77f57d7
```

Preparation verifies the frozen DisMech export manifests and source hashes. It only reads Aurora. Alternatively, `--target-run-file` accepts complete metadata previously returned by the backend's `read_target_run`, including the stored calibration labels, vectors, and hashes. The older `embedding-generation.json` summary is insufficient because it lacks those probes. Database loading independently checks the live EAGGL target even when preparation used a local file.

The next command explicitly sends the prepared public DisMech text to the configured embedding service. It resumes the local capture, retaining validated vectors already completed:

```sh
.venv/bin/python scripts/import_dismech_embeddings.py embed \
  --batch-size 32 --max-workers 2 --max-retries 3 --timeout 120
```

For a bounded throughput check, add `--max-batches 1`. This limits committed
groups, each containing at most `batch-size × max-workers` texts; it does not
limit individual HTTP requests. For example, a group with batch size 100 and
four workers contains at most 400 texts across four requests. A partial run
remains unavailable to runtime search and resumes with the same `embed` command
after removing the limit. Pending texts are grouped by length to reduce padding
work, without changing their exact input strings, source bindings, or manifest.

Schema creation and data loading are separate operations. Inspect the prepared manifest and migration 006 before applying them to the configured project database:

```sh
.venv/bin/python scripts/import_dismech_embeddings.py migrate --apply
.venv/bin/python scripts/import_dismech_embeddings.py load --apply --batch-size 500
.venv/bin/python scripts/import_dismech_embeddings.py verify \
  --report .runtime/dismech-embeddings-verification.json
```

`migrate` applies only migration 006. `load` does not create tables or regenerate embeddings. `verify` is read-only against Aurora. A failed embedding or loading command can be rerun against the same capture; incompatible inputs, target configuration, vector dimensions, or hashes must fail instead of silently replacing an existing run. The CLI locks each capture while it is in use, so two local commands cannot modify it concurrently.

Use `--output` consistently to keep captures for separate imports or model configurations apart. `--report` writes a compact JSON result outside the capture if desired. Database connection overrides match the existing import commands: `--host`, `--port`, `--user`, `--database`, and `--ca-file`; TLS remains required.

## Optional local inference

The reusable CLI can generate prepared public context inputs entirely offline from an already cached Hugging Face snapshot. Install Torch and SentenceTransformers only in a separate tool environment; the application and ordinary import commands do not require them. The CLI additionally needs the usual lightweight backend import dependencies, including NumPy and python-dotenv. It never downloads a model automatically and disables implicit Hugging Face credentials. Remote and local execution retain the same immutable compatibility target, source texts, vector format, and ranking rules.

Local inference requires an explicitly selected device, an exact 40-character model commit, its hub cache, and a fixed reference bundle with an independently pinned SHA256. That bundle binds the capture manifest, EAGGL target, at least 32 remote context texts (or the full smaller inventory), and their stored vector hashes. Its remote origin is explicitly retrospective operator attribution. Preflight verifies each unchanged text against the source inventory and every vector against the preserved checkpoint, then compares fresh local outputs with both the original EAGGL probes and the fixed remote contexts. A later mixed resume continues using those same remote references. Any mismatch fails before the embedding writer is called.

For the validated current capture, the opt-in command is:

```sh
.runtime/dismech-local-benchmark/venv/bin/python scripts/import_dismech_embeddings.py embed \
  --batch-size 32 --max-workers 1 --max-retries 0 \
  --local-device mps \
  --local-revision 82d44689be9cf3c6c6a6f77cc3171c93282873a1 \
  --local-cache-dir .runtime/dismech-local-benchmark/huggingface/hub \
  --local-calibration-reference .runtime/dismech-local-benchmark/remote-calibration.json \
  --local-reference-sha256 66a3a8e8b5a7cb1c505649869a993eb0d2e62d02a41e438887f77a132b02d4c7 \
  --legacy-generation-attestation .runtime/anchor-performance/legacy-generation-attestation.json
```

The CLI holds the existing capture lock throughout validation and generation. Inference uses one local worker, float32, and no normalization. Its generation metadata records the actual cached file hashes, resolved commit, device, package versions, tokenizer settings, pooling, calibration evidence, and fixed-reference digest. The current model truncates to **100 tokens**; this matches the calibrated remote encoder and is a material property of the embedding, not a change to the source text. Source strings remain complete and unchanged in the capture.

The September 26 benchmark compared three original EAGGL probes and 32 remote DisMech contexts spanning character-length quantiles, including the longest checkpointed input (2,038 characters). A separate 64-context comparison passed at both batch sizes: minimum cosine was 0.9999999999989528. The measured inference time was 0.549 seconds for 64 texts at batch size 32 and 0.925 seconds at batch size 64, after model loading. The resolved model commit was `82d44689be9cf3c6c6a6f77cc3171c93282873a1`, with Torch 2.14.0 and SentenceTransformers 6.1.0 on MPS.

The actual 17,216-vector continuation used an explicit isolated runner in `.runtime/dismech-local-benchmark/complete_capture.py`, while the reusable CLI was being implemented. That runner used the benchmarked offline model, the fixed remote references, and the same backend session journal with pre/post EAGGL calibration. Its record includes the runner hash, model commit, tokenizer settings, device, packages, and reference digest. The reusable CLI received focused offline tests and independent code review; it was not the command that performed this backfill. The detailed benchmark, continuation result, fixed references, and retrospective attribution remain in the ignored `.runtime` evidence directories.

## Runtime boundaries

Imported context vectors belong to their source import and compatible EAGGL embedding run. Set `REVEAL_DISMECH_EMBEDDING_RUN_ID` to select a completed run explicitly when needed. Source text changes, model/configuration changes, or a new target run require a new explicit preparation and backfill. A missing or incomplete stored run must not be represented as completed scientific coverage.

Ad hoc semantic search can still need an embedding when its exact text is absent from the preloaded source-context and EAGGL-label vectors. The bounded process cache avoids repeat requests for the same text and target configuration. Ordinary application startup does not backfill the corpus, call the embedding service to rebuild it, or mutate existing EAGGL vectors.

Embedding similarity is retrieval metadata, not biological evidence. Persistent vectors change where matching inputs are obtained, not the scientific interpretation of a suggestion or the requirement to inspect its evidence.

The complete run was loaded into Aurora and independently verified on September 27: 20,576 vectors and 20,588 source bindings. The API was reloaded and the live T2D example verified against those stored contexts; see [anchor retrieval validation](anchor-retrieval-validation.md) for measured timings, binding parity, and test limitations.
