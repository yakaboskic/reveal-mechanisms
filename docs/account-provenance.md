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

Factor expansion is pinned to a reference generation in one of two ways. A trusted research capture binds the IDs it observed. Otherwise, a GeneSet or Mechanism the account references, whose lineage the document does not already carry, binds by content-addressed identity: a GeneSet ID stored in exactly one retained KPN generation, or a Mechanism whose node that generation's `reference_factors` and `kpn_traits` rows recompute exactly (the same minting as the catalog). Hosted (Box) accounts carry no captures, so their Mechanisms bind this way. Identity bindings are reported as `binding_origin: document_identity` in `coverage.retained_projection_coverage` and in the projection edges' `provenance_source`, and as a `document_identity` source observation. Captures keep precedence; a capture and an identity binding that disagree are ambiguous and blocked, and a match in more than one generation stays unbound (`ambiguous_identity_binding`). The active catalog and legacy generations are never used. KPN projections are the retained top-50 joint **or** marginal union; legacy links are ranked summaries. Neither establishes the complete model construction history.

### Curated CFDE lineage

The served KPN generation `ec3364ae…` stored each collection's Datasets and Organizations but not the edges after `gene_sets:` in its source documents, so a GeneSet's generating Activity reached no Dataset. [`data/cfde-provenance/2026-10-11/collection-lineage.json`](../data/cfde-provenance/2026-10-11/collection-lineage.json) curates, per library, which stored upstream Datasets that Activity used (`data_input` for the measured source, `metadata_input` for a gene-identifier mapping), and `scripts/apply_collection_lineage.py` adds them as `payload.provenance.used_edges` with a `payload.lineage_curation` marker. This is a collapsed, hand-curated lineage: the filtered-library step is not represented, and the edges are labelled `provenance_source.kind: curated_lineage` (with the curation id and checksum) wherever they appear, and in `recovery_status[].curated_lineage`. LIGER collections record no upstream source and stay unresolved. See [the curation README](../data/cfde-provenance/README.md).

The processing bounds are 10,000 nodes, 50,000 edges and depth 128; a 100,000-state processing limit also bounds repeated work. Serialized responses are limited to 8 MiB. Exceeding a bound returns `422 PROVENANCE_LIMIT_EXCEEDED` without rankings. Retained storage outages return a service error. A missing full account source returns `409 PROVENANCE_SOURCE_UNAVAILABLE`; conflicting root payload observations return `409 SCIENTIFIC_SOURCE_CONFLICT` (or 404 when an exact payload was requested).

## Stored data sources

Every new account is traced once when it is saved: by the agent run's final save (`Worker.accept_accounts`, both job engines) and by a local submission (`research_execution.validate_submission` traces, `commit_accounts` stores). The trace runs before the acceptance write fence, over the exact capture records the commit writes, and is stored in the same commit as one `account_data_sources` record per owner and account (`account_data_sources.py`). `GET /v1/accounts/{id}` serves it as `data_sources`: per claim, each Dataset it reaches with its input roles, routes, the cited gene set, mechanism, file or source claim it was reached through (`via_ids`), the credited Organizations and roles, and any curated lineage step (`curation_ids`), plus names for the bubble chart. The account view draws these datasets inside each claim circle.

- **Never blocking:** a trace failure (storage, reference database, processing bound, or a 90 s deadline) is stored as `status: unavailable` with an error code; the account is accepted regardless.
- **Exact source:** a record is served only for the document and account payload it traced, under the current format and `TRACING_POLICY_VERSION`; otherwise `data_sources` is `{status: not_traced}`. Re-accepting an account keeps its first trace, matching the first-document-wins object index. A private read that withholds revoked borrowed objects drops the connections reached only through them and reports `incomplete`.
- **Publication:** publishing copies the owner's matching record beside the new snapshot (`digest(['publication', snapshot_id])`); a later re-trace never changes what a published snapshot serves.
- **Leaderboard:** the Datasets view ranks by "Most claims traced" (`sort=traced_claims`): the distinct claims, and accounts, whose stored trace reaches each dataset. This is lineage reach, not evidence, so the evidence-based rankings are unchanged. `scope=published` (default) reads the records frozen with publications. `scope=all` also adds every other saved account's own record, as anonymous totals: private accounts and claims are never listed, and their records report only a `withheld` count.
- **Point in time:** the record observes the reference tables when the account was saved (`reference_generation_ids`, `source_observations_sha256`). After a lineage curation or policy change, re-trace with the backfill, which uses the endpoint's exact sources (dry run first):

```sh
REVEAL_ARTIFACT_STORE=s3 REVEAL_S3_BUCKET=cyaka-reveal-data REVEAL_S3_PREFIX=qa/ AWS_PROFILE=broad \
  .venv/bin/python scripts/backfill_account_data_sources.py --prefix reveal_workflow_qa [--force] [--apply]
```

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
