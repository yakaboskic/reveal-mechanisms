# Durable Vector retrieval

The catalog uses Upstash Vector for semantic candidate retrieval. The vectors and the reference tables come from one reference release (see [reference-release.md](reference-release.md)). The publisher writes both, and the app only reads them. No Vector code uses Redis.

Use `upstash-vector==0.8.0`. The adapter sets its HTTP timeout to 20 seconds with one retry, because this SDK otherwise defaults to a 600-second read timeout.

## Configuration

- `REVEAL_VECTOR_ENVIRONMENT=local`, `qa` or `prod`: names the environment's namespaces.
- `UPSTASH_VECTOR_REST_URL`: the HTTPS dense cosine index, with the release's embedding dimensions.
- `UPSTASH_VECTOR_REST_TOKEN`: the serving credential. Prefer a read-only token.
- `UPSTASH_VECTOR_WRITE_TOKEN`: the publisher's credential. The app doesn't need it.
- `EMBEDDING_SERVICE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_PROVIDER`: these embed query text, and DisMech context text the release lacks.

Credentials stay in backend secrets. Namespaces separate environments; credentials, application authorization and deployment configuration set the access boundaries.

## Namespaces

Each environment has two fixed namespaces, filled by `python -m reveal_backend.reference_release publish`:

| Namespace | Ids | Read by the app |
|---|---|---|
| `<env>-factors` | factor key `KPN.TRAIT:NNNNNNN::FactorN`, one vector of its label | yes: queried and fetched |
| `<env>-contexts` | sha256 of the exact DisMech context text | yes: fetched |

Each vector's metadata carries its `vector_sha256`. A publish:
- upserts every vector that is missing or changed;
- swaps the reference tables;
- then deletes the vectors the release doesn't hold.

The served factor table decides which factors can be suggested. A provider hit whose id isn't in `ref_factors` is skipped.

## Serving and audit semantics

**Context vectors.** A semantic request embeds each context once:
- a DisMech context text is fetched from `<env>-contexts` by its sha256, and embedded live if it is missing;
- researcher text is always embedded live.

Query vectors are cached in the process.

**Candidates.** The provider returns ANN candidates, and the adapter computes exact cosines over the fetched factor vectors. This keeps the real similarity of every selected factor/context pair, including the contexts where the factor didn't win. The hybrid mode keeps reciprocal-rank fusion of the semantic and lexical legs. Its bounded ANN candidate depth can change the ranking relative to an exact search, and the depth is recorded in the retrieval provenance.

**Candidate depth.**
- It starts at `max(32, 4 × requested_count)` candidates per context and doubles as needed, up to 1,000.
- Fetches are bounded at 4,096 ids.
- Batch queries are split so their combined top-k reads never exceed the provider's limit of 1,000 reads per request.
- Provider scores are converted from `(1 + cosine) / 2` back to cosine. The saved retrieval record keeps the raw provider scores.
- A provider error, invalid score, incomplete fetch or wrong metric or dimensions fails semantic retrieval explicitly. There is no fallback.

**What a saved suggestion records:**
- the release id and namespaces;
- the policy version and query embedding model;
- the candidate hits with their raw scores;
- the normalized query vectors and their checksums;
- each selected factor's `vector_sha256`.

Accepted requests freeze the saved suggestion record, so a later release can't rewrite the chosen anchors or the retrieval evidence.

## Provider references

The implementation supplies raw vectors through:
- the [Python query and batch query API](https://upstash.com/docs/vector/sdks/py/example_calls/query);
- the [fetch API](https://upstash.com/docs/vector/sdks/py/example_calls/fetch);
- explicit [namespaces](https://upstash.com/docs/vector/features/namespaces).

The score conversion follows the documented [cosine similarity function](https://upstash.com/docs/vector/features/similarityfunctions).
