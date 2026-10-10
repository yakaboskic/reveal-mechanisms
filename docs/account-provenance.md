# Scientific account provenance and reuse

`GET /v1/accounts/{account_id}/provenance` expands one accepted scientific account into dataset and organization lineage. It follows structured scientific references in propositions and evidence, including inherited source claims and retained factor–GeneSet projections. Existing leaderboard definitions are unchanged.

Use the stored, minted `dapper:ScientificAccount.…` ID returned by account acceptance. A local authoring URN or a JSON filename is not an accepted account ID. Private accounts require their owner's application bearer credential; published accounts are readable anonymously. Withdrawal and revoked dependencies take effect on subsequent reads. Responses use `Cache-Control: private, no-store` and vary by authorization. Configure `REVEAL_GATEWAY_SECRET` with at least 32 bytes for signed provenance cursors.

```http
GET /v1/accounts/{account_id}/provenance?limit=100
Authorization: Bearer <application credential>
```

`payload_sha256` optionally selects an exact account payload. `limit` defaults to 100 and accepts 1–250. Follow `page.next_cursor` on the same endpoint until it is null; omit `limit` to retain the cursor's page size. Changed source observations, supplements, publication state, permissions, or an explicitly changed limit invalidate a cursor with `409 CURSOR_EXPIRED`. An inaccessible account or unavailable requested payload returns 404.

## Response

- `account`, `claims`, `propositions`, and `evidence_items` contain `{record, provenance}` wrappers. `record` preserves the original scientific object. `provenance` adds the total trace count, current-page trace IDs/count, reached dataset and organization IDs, and resolution status.
- `traces` identify the originating claim, proposition/evidence item and source field, dataset reached, route classifications, input roles and organization attributions. `path_steps` describe an auditable witness path; reverse dataset membership steps are explicit.
- `nodes` and `edges` share the traversed graph across traces. Alternative recorded branches remain in the graph, without enumerating every path. Nodes are compact summaries rather than complete GeneSet member lists. The bounded shared graph repeats on each page.
- `dataset_reuse` and `organization_reuse` are full-account totals, computed before pagination. Do not sum totals from successive pages.
- `snapshot` pins the account payload, authorized document, publication version, reference generations, supplements and tracing policy. Source observations contain hashes and public scientific coordinates, not private storage locations.
- `coverage` separates lineage resolution, retained-projection coverage and recovery status. Missing lineage makes counts explicit lower bounds. Processing limits and storage failures do not produce zero rankings.

The generated [OpenAPI contract](../api/openapi.json) provides exact types and a complete fixture response.

## Counting rules

A dataset contributes once to each distinct component/conclusion claim that reaches it, regardless of how many propositions, evidence items or inherited paths reach the same dataset. Component and conclusion membership is a set. An outside source claim supplies lineage but does not become another counted claim in this account.

Route classifications overlap: `proposition_reference`, `evidence_derivation`, `inherited_source_claim`, and `factor_projection`. Their subtotals must not be added. Evidence direction belongs to the originating evidence item; it is not multiplied through source claims. Referencing a factor does not mean every associated dataset directly supports the proposition.

Organization summaries count distinct datasets, claims and claim–dataset pairs, with breakdowns by recorded role. Publishers, creators, contributors and generation agents remain distinct. Award/funder text does not create an Organization identity. Mapping/reference datasets are included, and their recorded input roles are retained; absent roles stay `unknown`.

Traversal uses derivation, generation and activity-input relationships in fields and edge records. A reached File may be associated with its recorded Dataset, but this inverse membership does not expand sibling files or inherit unrelated lineage of the containing dataset. Free-text names, retrieval candidates, KnowledgeGap selection links, embeddings and account-authoring activity inputs do not establish claim reuse.

Factor expansion is pinned to trusted captured generation bindings. KPN projections are the retained top-50 joint **or** marginal union; legacy links are ranked summaries. Neither establishes the complete model construction history. An unavailable or ambiguous binding stays unresolved rather than using the active catalog.

The processing bounds are 10,000 nodes, 50,000 edges and depth 128; a 100,000-state processing limit also bounds repeated work. Serialized responses are limited to 8 MiB. Exceeding a bound returns `422 PROVENANCE_LIMIT_EXCEEDED` without rankings. Retained storage outages return a service error. A missing full account source returns `409 PROVENANCE_SOURCE_UNAVAILABLE`; conflicting root payload observations return `409 SCIENTIFIC_SOURCE_CONFLICT` (or 404 when an exact payload was requested).

## Import preservation and historical recovery

New version-3 reference bundles retain provenance nodes and relationship groups before and after `gene_sets` in collection YAML. Relevant sections are streamed and validated. Captures and evidence packages preserve anonymous provenance edges alongside exact scientific nodes. Dataset and organization records reachable only through inverse membership remain in immutable retained captures, because canonical DAPPER reachability is directed; the endpoint expands that verified context without rewriting accepted records, even when the original catalog rows have retired.

Older imports may have retained a generating Activity but dropped its input edges. The offline recovery command can register an immutable supplement from the exact original collection YAML. It checks the complete file checksum against both the stored collection and generation manifest, validates extracted relationships, and refuses conflicts. It never rewrites accounts, historical packages, collection payloads or generation manifests.

With the normal application database/artifact-store configuration, inspect the dry run first:

```sh
python scripts/recover_collection_provenance.py --generation-id <generation-sha256> /path/to/original.GeneSetCollection.yaml
```

Register verified supplements explicitly:

```sh
python scripts/recover_collection_provenance.py --generation-id <generation-sha256> --apply /path/to/original.GeneSetCollection.yaml
```

The command accepts multiple collection files. It stores content-addressed graph artifacts and a catalog-owned `provenance_supplement` record, identified by generation, collection, original source checksum and extractor version. Repeating the same recovery is idempotent. Registry records/artifacts are retained across reference retirement; unavailable retired source bindings are still reported as gaps. The GET endpoint only reads registered supplements and performs no remote recovery or source-file fetching.

Recovery of the supplied MoTrPAC example requires its checksum-matching original collection file. A copied dataset name, collection membership, or co-location in a provenance document is insufficient to reconstruct a missing derivation relationship.
