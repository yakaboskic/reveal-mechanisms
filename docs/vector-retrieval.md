# Durable Vector retrieval

The production catalog uses Upstash Vector for semantic candidate retrieval. Aurora retains canonical source records and the immutable snapshot registry. The old matrix implementation is available only through the explicit operator setting `REVEAL_RETRIEVAL_BACKEND=legacy`; provider errors never trigger automatic fallback.

Use `upstash-vector==0.8.0`. The adapter sets its HTTP timeout to 20 seconds with one retry because this SDK otherwise defaults to a 600-second read timeout. No Vector code uses Redis.

## Configuration

- `REVEAL_RETRIEVAL_BACKEND=upstash` (default)
- `REVEAL_VECTOR_ENVIRONMENT=local`, `qa`, or `prod`
- `UPSTASH_VECTOR_REST_URL`: HTTPS dense cosine index, with the selected embedding run's dimensions
- `UPSTASH_VECTOR_REST_TOKEN`: serving credential; prefer a read-only token
- `UPSTASH_VECTOR_WRITE_TOKEN`: separate explicit ingestion credential
- `REVEAL_APPLICATION_TABLE_PREFIX`: environment-specific application/registry tables
- Standard versioned S3 artifact configuration, including a distinct write prefix per environment

Credentials stay in backend secrets. Namespaces separate snapshots, while credentials, application authorization, and deployment configuration determine access boundaries. The local migration supplied one write-capable token; configuring a distinct read-only serving token is an operational hardening step.

## Source coverage and import

The current source import has 4,037 EAGGL factor bindings. Of these, 1,756 have an eligible binding in the selected completed CFDE mapping. Those 1,756 aliases enter the serving namespace. The remaining 2,281 unmapped bindings are retained in an immutable S3 archive and are not scientific retrieval candidates. There are 20,588 DisMech context bindings representing 20,576 unique context texts. Context vectors remain distinct by exact source ID, source revision, template, and input hash.

An explicit import exports original float32 vectors, verifies source bindings with the existing EAGGL/DisMech validators, writes compressed immutable S3 batches, and registers a loading snapshot. It never re-embeds the corpus. A deterministic ID, original vector checksum, model/calibration space, source/mapping runs, and explicit namespace accompany each binding. The snapshot remains inactive until every batch, ID inventory, numeric readback, and exact-baseline quality probe passes.

```sh
python -m reveal_backend.vector_ingestion --env-file /absolute/path/backend.env --workflow --activate
```

This command exports the current selected source runs and durably enrolls the import. `--snapshot SNAPSHOT_ID --workflow` resumes enrollment for an already exported snapshot. A registered dispatch intent survives a failed QStash trigger and is repaired by the managed reconciler. `--previous-snapshot ID` supplies the expected active snapshot for compare-and-swap activation. With `--workflow` omitted, the same import operations can be run explicitly by an operator for the initial local migration.

The signed endpoint is `/internal/workflows/vector-import-v1`, with the same service prefix and signature configuration as research workflows. Workflow steps import one bounded batch, verify one 200-ID inventory page, or compare one representative query to its frozen exact baseline. Workflow payloads and results contain only identities, cursors, and small reports. Initial source export is an explicit offline operation; application startup does not export, index, or generate corpus embeddings.

Batch checkpoints use independent `vector_batch` records. Immutable manifests are cached within a bounded cache; they are not repeatedly transferred for every batch. Source exports and manifests are versioned and checksum-verified. Retrying an upload uses the same namespace and IDs. Failed or partially indexed snapshots never become active. Loading snapshots do not emit catalog events; activation writes `vector_active` and produces the committed public catalog invalidation through the normal outbox.

`clone_export()` copies a verified export to another environment's namespaces and S3 write prefix without rereading MySQL vectors or invoking an embedder. It retains the source export and unmapped archive provenance. Each environment then independently imports, verifies, and activates its new snapshot.

## Serving and audit semantics

A semantic request reads the small active-registry pointer and pins one immutable snapshot for all context queries, alias fetches, and ranking. Cached source metadata must match the selected source and embedding runs. Changing those runs requires loading the corresponding catalog; incompatible snapshots are rejected explicitly.

The provider returns ANN candidates. The adapter expands a bounded candidate window before native-factor deduplication/exclusions and fetches every alias of candidate native factors. Exact cosine calculations over these bounded returned vectors retain maximum-over-aliases and real similarities for every selected factor/context pair, including non-winning contexts. The hybrid mode retains reciprocal-rank fusion of the semantic and lexical legs. Its bounded ANN candidate depth can change ranking relative to the offline exact baseline; it is recorded in retrieval provenance.

The current policy starts at `max(32, 4 × requested_count)` candidates per context and doubles as necessary, with a 1,000-candidate ceiling. Alias fetches have a separate 4,096-record bound. Provider scores are converted from `(1 + cosine) / 2` to cosine; raw provider scores remain in the saved retrieval record. A provider error, unknown binding, empty response for a known nonempty snapshot, wrong metric/space, or changed readback fails semantic retrieval explicitly.

Saved suggestion hits contain snapshot/namespaces, policy version, source/embedding/mapping runs, actual candidate hits and raw scores, normalized query vectors/checksums, selected alias IDs/original checksums, and exact per-context cosines. Accepted requests already freeze the saved suggestion record, so later activation cannot rewrite the chosen anchors or historical retrieval evidence.

The import gate checks original vector bytes independently of provider numeric roundtrip precision (`atol=2e-6`, `rtol=2e-5`). It records a separate checksum of the verified provider values; serving fetches must match that readback. Deterministic factor/context probes require minimum recall at 10 of 0.9, exact top-1 score agreement within `1e-5`, and maximum provider-converted cosine error below `5e-4`. Gate results are stored with the snapshot; these are measured import gates, not a promise of identical ANN ordering on every future query.

Old namespaces and immutable exports are retained. There is no automatic namespace deletion or garbage collection until reference retention and rollback policy are established.

## Provider references

The implementation uses supplied raw vectors through the [Python query and batch query API](https://upstash.com/docs/vector/sdks/py/example_calls/query), [fetch API](https://upstash.com/docs/vector/sdks/py/example_calls/fetch), and explicit [namespaces](https://upstash.com/docs/vector/features/namespaces). The score conversion follows the documented [cosine similarity function](https://upstash.com/docs/vector/features/similarityfunctions).
