# Anchor retrieval validation

Validation performed September 26–27, 2026. The complete DisMech vector import passed independent Aurora verification, the updated API is ready, and the live T2D example returns the same five native anchors with mechanism-label chips. Network conditions changed between measurements, so the timings below are observations rather than a controlled estimate of speedup.

## Interface and component checks

Mechanism chips and picker results now lead with the imported factor label and retain identifying details in tooltips. Automatic suggestions abort superseded requests and ignore stale results. The manual picker displays lexical matches while its debounced hybrid search runs, with visible loading feedback.

- Frontend: nine unit tests passed, TypeScript checks passed, and an isolated production build completed with ten static pages.
- Browser checks used a real catalog record and intercepted suggestion responses. They verified lexical results before a delayed hybrid response, chip/picker labels and tooltips, no overflow at 390 px, clearing a selection aborting its request, and autosave occurring once after suggestions settled rather than while pending. No page errors occurred. These checks made no embedding calls and started no research jobs. Evidence: `.runtime/anchor-label-audit/checks.json` and `check.mjs`.
- Backend full suite: 243 tests, comprising 234 passed, eight optional/disposable-database skips, and one worker-journey failure. Diagnostics showed the host wall clock advancing beyond a valid attempt lease; a test-only UTC clock derived from monotonic elapsed time passed that same journey. Production lease behavior was unchanged. The full suite is therefore not reported as entirely passing.
- A subsequent 31-test loader/cleanup regression run passed. The local runner and CLI checks passed 13 tests, including reference corruption, vector drift, capture locking, explicit operation gates, and refusal to enter the writer after failed calibration. Independent review found no blocker in the local runner.

## Imported vectors and provenance

EAGGL already contained 3,103 unique label vectors across 4,037 factor records. The original DisMech import contained source text and provenance but no compatible context vectors. Migration 006 and the explicit prepare/embed/load/verify pipeline add those missing vectors without regenerating EAGGL embeddings or altering scientific identities.

The prepared DisMech corpus contains 19,959 mechanism bindings plus 629 fallback gap prompts. Exact text deduplication produces 20,576 vectors for 20,588 source bindings. Bindings retain native source IDs, revisions, text templates, and input hashes. The target is EAGGL run `d4c0300978778453c847c7c3447a316e55b1b0cfe469c647f148ecc1a77f57d7`, using the BioBERT sentence-transformer model at 768 dimensions.

The complete local capture passed integrity verification. Its 3,360 earlier remote vectors have explicit retrospective operator attribution; original per-session provenance and final calibration were not recorded for those executions. The remaining 17,216 vectors were generated locally in 131.887 seconds, with exact input/vector assignments and pre/post EAGGL calibration recorded in a generation journal. This observed generation time excludes model download and database loading.

Local execution used cached Hugging Face commit `82d44689be9cf3c6c6a6f77cc3171c93282873a1`, float32 MPS inference, no normalization, and the model's existing 100-token truncation limit. Calibration covered original EAGGL probes and a fixed set of 32 remote DisMech contexts spanning length quantiles, including the longest checkpointed context. A separate 64-context benchmark had minimum cosine similarity 0.9999999999989528 against remote vectors, above the required 0.999999 threshold.

The actual continuation used the isolated `complete_capture.py` runner while the reusable CLI was being implemented. Both use the backend's generation journal; the reusable CLI received offline tests and review but did not perform this backfill. It additionally records cached snapshot file hashes and requires an explicitly pinned reference-bundle digest. Detailed commands, limitations, and evidence locations are in [Persistent DisMech context embeddings](dismech-embeddings.md).

Automatic gap requests reuse imported mechanism descriptions or fallback prompts, and exact EAGGL label queries reuse their imported label vectors. Free-form semantic searches use a bounded runtime cache and may still call the embedding service. Tests cover ranking parity, source revisions, context order, exclusions, concurrent cache misses, expiry, and fail-closed behavior for stale or missing imported bindings.

## Transfer reduction and live validation

The backend's local projection estimate reduces each full vector readback from approximately 73.6 MB to 5.35 MB by returning hashes computed from the database text/vector bytes, byte lengths, and recorded checksums. Source text verification also compares exact server-computed hashes instead of transferring the prose. This is an estimate from the local imported data, not measured Aurora traffic or an observed end-to-end latency improvement.

A prior actual proxied suggestion request for the T2D example took 6.2799 seconds on September 26. It is a single observation, and network conditions subsequently changed; any later timing comparison must preserve that qualification.

| Final acceptance item | Verified result |
| --- | --- |
| Aurora import | 20,576 vectors, 20,588 source bindings, 768 dimensions; separate read-only verification passed |
| Selected DisMech embedding run | `dd922e3b7b405f68626bd1679129764de605fd9e278244cca8de54b304d47824` |
| Deployed API | Ready over verified Aurora TLS; catalog warmed in a 32.134-second API-only reload; existing worker identity/image/start time unchanged |
| Live browser | Five mechanism-label chips; first is “Beta Cell Dysfunction and Diabetes”; no page errors or horizontal overflow at 390 px; no intercepted responses or research job submission |
| Live proxied requests | Three full-response observations: 1.8339, 1.8627, and 1.5809 seconds; separate browser request reached response headers in 2.161 seconds |
| Scientific bindings | Same five native IDs, source revisions, matched contexts, and ranking order as the earlier request; maximum similarity delta below 0.00000008 |
| Stored-vector provenance | The live suggestion audit binds the verified DisMech run and all three exact context IDs, revisions, templates, and input hashes; checked against the local capture |

Evidence is retained locally in `.runtime/anchor-performance/dismech-verify.json`, `api-reload.json`, `retrieval-comparison.json`, and `live-provenance.json`, and `.runtime/anchor-label-audit/live-checks.json` with desktop/mobile screenshots. The browser test confirms the rendered chips; unit tests explicitly forbid embedding calls on automatic semantic/hybrid requests. No live network-call counter was used, and similarity scores remain retrieval metadata rather than biological confidence.
