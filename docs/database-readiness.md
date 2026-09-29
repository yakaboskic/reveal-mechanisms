# Aurora MySQL readiness and persistence

**Current runtime update:** the API now uses the populated database and preserves the original `reveal_*` records. Colleague setup runs no imports or table migrations. Catalog readiness and in-memory embedding search are explained in [local deployment](local-deployment.md); the audit details below retain their observation dates.

**Current status (September 25, v12.1):** the original [read-only audit](../data/audit/2026-09-25-consolidation/database.json) covered 11 GeneSet/EAGGL tables and 3,103 live 768-dimensional embeddings. The later [crosswalk verification](../data/eaggl-cfde-mapping/2026-09-25/database-verification.json) records **14 applied tables**, **1,756 EAGGL→CFDE mappings** and **4,056 resolved GeneSet links**. Prisma maps these plus nine DisMech tables, still unapplied to Aurora. Application users/jobs/citations/general provenance storage remain planned. Use the [crosswalk-backed retrieval contract](design-plan.md#initial-crosswalk-backed-retrieval-contract) and [implementation sequence](implementation-handoff.md).

**Initial audit:** 2026-09-24, read-only connectivity, authentication, grants, and capability inspection. [Machine-readable audit](../data/infrastructure/mysql-audit-2026-09-24.json). **Subsequent implementation:** the GeneSet importer created `cyaka_reveal_mechanisms` and its first three tables; see [the load report](../data/cfde-genesets/2026-09-24/database-load.json) and [import guide](geneset-import.md).

## 1. Can this account create the project database?

**Yes, the observed grants permit it.** The supplied account authenticated successfully to `aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com:3306` as `cyaka@%`, using certificate-verified TLS with cipher `TLS_AES_256_GCM_SHA384`.

The relevant database grant is:

```sql
GRANT ALL PRIVILEGES ON `cyaka\_%`.* TO ...
```

The escaped underscore makes `cyaka_` the literal prefix; `%` permits a suffix. Database-level `ALL PRIVILEGES` includes creation within this scope. **`cyaka_reveal_mechanisms`** was absent during the initial audit and has since been created for the authorized GeneSet import.

The initial audit performed no writes. The subsequent [migration](../schema/migrations/001_gene_set_inventory.sql) creates `dapper_objects`, `gene_set_imports`, and `cfde_gene_set_aliases`. The loader writes actual DAPPER GeneSet/Activity objects and model-scoped aliases, with transactionally resumable checkpoints. Migration 002 subsequently added eight legacy EAGGL/embedding tables; migration 004 added three [routing tables](eaggl-cfde-links.md). DisMech migration 003 remains unapplied.

The completed import contains **801,934 GeneSets**, **801,934 CFDE aliases**, and **one encoding Activity**. It is the first database foundation for this project, with immutable DAPPER payloads and an explicit distinction between catalog/import provenance and still-unavailable original scientific construction history.

Independent [read-back verification](../data/cfde-genesets/2026-09-24/database-verification.json) passed for every alias invariant and 18 exact payload samples. Replaying the completed loader resumed at 801,934 rows and retained the same final count.

Observed engine: **MySQL 8.0.42, Aurora 3.10.3**. Credentials were entered through a non-echoing prompt and are absent from the saved scripts, fixtures, and documentation.

Database definition used by the importer:

```sql
CREATE DATABASE IF NOT EXISTS cyaka_reveal_mechanisms
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_bin;
```

Use binary/case-sensitive columns for opaque source IDs and DAPPER digests even if human-facing text has case-insensitive collation. The earlier source capitalization anomaly makes this distinction concrete.

## 2. Embeddings in this MySQL deployment

Store textual embeddings in MySQL as requested. Separate their durable storage from the engine that searches them.

Recommended first implementation:

Use the existing `eaggl_name_embeddings`/`eaggl_embedding_runs` storage and database search index, joined to a completed `eaggl_cfde_link_runs`/`eaggl_cfde_factor_links` run. It already supplies 1,756 routable anchors; no new complete factor corpus is needed. The generalized search records below can support DisMech query caching and later corpus expansion without replacing those imports.

- `search_documents` holds the exact text, entity/version reference, source, scope, and text hash used for embedding.
- `text_embeddings` holds a float32 vector in a BLOB, embedding provider/model/version, dimension, normalization, distance metric, input hash, and creation activity. Validate byte length against dimension. JSON vectors are a simpler debugging alternative with greater storage/parse cost.
- A backend retrieval worker loads a versioned vector matrix/index from these rows and performs global semantic search over the selected source/model corpus. Begin with exact cosine search at the present corpus size; benchmark memory/latency before selecting an approximate index. The index is a rebuildable projection; MySQL owns the records.
- Use MySQL full-text search for lexical candidates and explicit application fuzzy/alias matching for short gene symbols, abbreviations, and misspellings. Full-text and semantic rankings are separate signals. See [MySQL 8.0 full-text documentation](https://dev.mysql.com/doc/refman/8.0/en/fulltext-search.html).
- Hybrid search combines independently retrieved lexical and semantic candidates, with the retrieval methods and ranks recorded. Do not perform “global semantic search” only over a lexical shortlist and silently lose paraphrase matches.

**Native vector search is not verified.** The read-only vector-function experiment was inconclusive: a call without a default database needed a database selection, and `USE information_schema` was denied by this account. The engine/version and grants do not demonstrate a native vector index. Do not assume PostgreSQL/pgvector, MySQL HeatWave, or a different MySQL version's vector features exist on this server. AWS also notes that Aurora MySQL's ML extension does not support vector interfaces in [its ML documentation](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/mysql-ml.html).

The service and default model are selected: `https://embedding-service-27386110942.us-east1.run.app`, provider `huggingface`, model `pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb`. The [embedding client module](../services/backend/README.md) is implemented and a live legacy-label run verified 768 dimensions. Use those vectors for initial retrieval, resolving source hits through the loaded exact-trait/factor crosswalk before calling CFDE. Embed DisMech query text on demand with the same configuration and record it; a bulk DisMech cache/full current-model factor corpus can follow later. Estimated embedding volume should use actual dimensions: `number_of_documents × dimensions × 4 bytes` for float32 values, plus metadata/index overhead. Record effective revision/truncation and benchmark the chosen index.

## 3. Proposed logical records

The 14 applied inventory/routing tables and nine prepared DisMech tables are mapped in Prisma. The additional application records below are a persistence specification, not existing tables.

### Source and search records

- **Source snapshots:** repository commit or API capture/build version; request metadata; content hashes; ingestion activity; source license where available.
- **Source entities and revisions:** stable application identity, source ID aliases, immutable revision payload, entity kind, source document/pointer, source version. Distinguish disease, phenotype, factor, and mechanism occurrence.
- **Mechanisms:** contextual DisMech occurrences and EAGGL factors, with disease/trait/model context and searchable descriptors. Preserve `trait_group` in CFDE identity and the exact interactive `node_id` alias.
- **Knowledge gaps and source mappings:** DAPPER KnowledgeGap content/digest, plus source repository/document/discussion ID and snapshot/hash mapped to that digest. Preserve raw prompt, kind, nullable status, rationale, context, evidence, attachments, and source pointer. The current DAPPER contract includes `gap_kind` and `about_entities`; implement the exact source mapping and four rationale fallbacks in the [integration contract](dapper-integration.md#2-question-and-dismech-gap-adapter). Question remains supported for historical records/interoperability; this application accepts selected imported gaps only. Keep raw source observations separate from the hashable semantic projection, so status-only or unrelated source updates do not remint every gap. Source status/retrieval observations are separate from semantic identity. Retain historical mappings rather than overwriting them.
- **Gap attachments:** exact original reference, resolved target ID/version or whole-section reference, target kind, resolution status, and ambiguity details. Never silently assign an ambiguous reference to the first name match.
- **Search documents and embeddings:** versioned derived text and vectors as described above. Separate record kind/source filters, and exclude outdated embeddings from the current index.

### User and job records

- **Authentication:** NextAuth JWT sessions with no database adapter require no NextAuth User/Account/Session tables. Credentials, OAuth tokens, session tokens, and signing/provider secrets stay outside this research database. See [the authentication design](authentication.md).
- **Application users and login mappings:** `app_users` gives each researcher a permanent UUID; `login_identities` uniquely maps verified external issuer/subject identities to it with source/verification provenance. These application-owned records make the database portable to another auth implementation. Email/name changes do not change ownership; cross-provider links need proof of both identities. A replacement system with different subject IDs requires a verified migration mapping.
- **Drafts:** `research_drafts` stores owner user ID, version, composer payload (selected gap/source revision, chips, selection origins/dismissals, subquery, model, selected KGs), and timestamps. Autosave uses expected-version updates and exposes conflicts. Anonymous/pending edits are local until the server confirms an authenticated save.
- **Research requests:** immutable submitted gap/question/context snapshot, source draft ID/version, internal `owner_user_id`, trusted attribution snapshot (available name/email/ORCID with source/verification state), submission time and idempotency key. Freeze submission and queue the job transactionally. Later draft edits do not change prior queries. Email is private contact metadata and is not an ownership key.
- **Job anchors:** selected source mechanism versions and frozen graph seed bindings, including automatic top-five EAGGL selections from DisMech, manually selected factors, user dismissals and embedding match provenance. Pin source EAGGL import/factor/index, embedding run/input/score, mapping run/method, CFDE native ID/model/catalog hash and GeneSet import. Copy confirmed draft bindings into the immutable request; never re-resolve old jobs through a newer active mapping. The accepted matching rule is exact trait + factor number, ignoring gene/label differences. CFDE anchors must contain at least one resolved factor; DisMech records are context only. Selection/routing provenance is separate from scientific support.
- **Jobs and activities:** common jobs table with `kind=analysis|paragraph`, parent request or input account, state, stage attempts, retries, cancellation, model/harness/skill versions, Upstash Box/run identifiers, and input/output artifact references. Persist EC2 worker leases, heartbeats, sequenced progress events, and Box execution IDs for restart recovery. Repeated executions retain separate operational history even when DAPPER content IDs deduplicate identical objects.
- **API captures, evidence bundles, and MCP ledger:** exact CFDE requests/responses, scores/paths, clipping decisions and frozen bundle hash; append-only Proto-OKN tool requests/results, selected graph/version, assertion IDs/qualifiers, source references and no-match/error status. Freeze a final evidence manifest without modifying the initial package.

### Scientific content and communication

- **DAPPER objects and payload observations:** validated scientific nodes with pinned schema, identity implementation/profile, payload checksum and validation status. Preserve the existing table/import payload. Add append-only `dapper_object_snapshots(object_id, payload_sha256, schema_pin, payload, validation)` so permitted unhashable changes (File location, activity timestamp, collection backlink) do not conflict with identity. Different hashable content under an existing ID fails verification. The original importer’s whole-payload equality rule remains specific to its frozen export.
- **ScientificAccounts and revisions:** required framing/context, ordered component claim references, interpretations, optional closing, attribution and assembly activity. Use the [audited DAPPER contract](dapper-integration.md) and retain exact dependency pins. The scientific-account linter accepts one terminal account per complete document; store and validate multiple accounts separately while reusing shared scientific IDs.
- **Account–claim references:** claim ID/version, order, and role. One claim may be reused by several accounts; one account may have one finding. Membership does not mean logical conjunction.
- **Question/KnowledgeGap digests:** use DAPPER-ID-1 with the implemented inquiry classes and typed `ScientificAccount.question` reference. Implement and validate the source adapter before minting imported gap objects; preserve the raw source captures.
- **Paragraphs and citations:** use `scientific_account`, `text`, generation provenance, and `citations` containing exact target IDs, positive metadata revision pins and Unicode code-point `start`/`end` offsets, optionally `exact_text`. Keep focus claim and audience/style in run inputs/render artifacts. Targets must exist in the saved authorized context, including explicitly selected prior Claims; hydrate their records for local validation. Changing a citation pin changes Paragraph identity; changing only the renderer does not.
- **Bibliographic registry:** versioned citation records keyed to exact DAPPER digests, faithful titles, ordered human/AI credits, roles/ORCID provenance, mint/issue/publication dates, resolver links, and optional registered DOIs. Keep citation metadata outside the target's hashable payload to prevent a digest cycle. DAPPER supplies `citation-record.schema.json`, `check_citation_metadata`, and `check_citation_registry_links`. The [citation profile](citation-standard.md) maps these to proposed `citation_records`, `citation_contributors`, `citation_events`, and rendering-cache records; these tables are not yet created.

- **Immutable provenance documents/edges:** capture each validated account package, prefix map, source/evidence manifest, exact object payload snapshots, and reified relationships such as `Used`. Node IDs alone do not hash a whole graph. Preserve a document checksum and explicit run-to-document association.
- **Object ownership/access grants:** link globally deduplicated scientific objects to owned requests/runs and explicit access grants. Authentication/user ownership is independent of DAPPER attribution. A matching digest or a historic bibliographic access field does not authorize access to another user's private content.

Saving a validated result should be transactional: required objects, account membership/interpretations, and provenance are committed together. Paragraph output is committed only after validating the account version and citation referential integrity. Failed agent output is retained as an attempt artifact, not exposed as a valid ScientificAccount. Digest minting does not require a separate human acceptance gate.

## 4. Data ready for import

- September 24 GeneSet export: **801,934** schema-validated DAPPER GeneSets for `cfde-inc-v2`, plus their encoding Activity. Its database status is recorded separately in the [load report](../data/cfde-genesets/2026-09-24/database-load.json).
- September 15 DisMech export: **19,959 mechanism occurrences**, plus source entities, evidence, vocabularies, and ontology bindings.
- September 15 CFDE export: **18,419 factors** for `cfde-inc-v2` from **3,778** phenotype queries.
- September 24 discussion export: **4,051 discussions**; **2,604 `KNOWLEDGE_GAP`** records and **763 `HUMAN_MODEL_MISMATCH`** records. Store the explicit kinds; expose the latter as a related gap category rather than relabeling it.
- Gap attachments: **6,422**, comprising **6,329 uniquely resolved targets**, **79 whole sections**, **4 whole documents**, and **10 ambiguous targets**.
- Gap source statuses: **2,526 `OPEN`**, **3 `RESOLVED`**, **838 unspecified**. Preserve null; choose a UI policy explicitly instead of rewriting it to OPEN. Every extracted gap has a source `discussion_id`.

See [gap extraction manifest](../data/dismech-gaps/2026-09-24/manifest.json) and [the exact AIP example with resolved attachments](../data/dismech-gaps/2026-09-24/aip-gap-example.json). The source repository remains at the same commit used by the original mechanism export, so its resolved mechanism pointers can join to those exported records. Cross-snapshot imports must check this condition instead of assuming pointer stability.

## 5. Import sequence and acceptance

1. Retain the completed GeneSet foundation, pinned DAPPER dependency, immutable objects, and versioned aliases. Adopt the current gap/citation contracts; add general payload/provenance storage and exact dependency pins. The existing import keeps its frozen schema and IDs.
2. Import snapshot metadata, source documents/entities, mechanisms/factors, gaps, and attachment resolutions. Use checksums/upserts for replay; preserve older revisions and source statuses.
3. Validate counts, unique source keys, model separation, case-sensitive IDs, and dangling references. Quarantine ambiguous targets; they must not become seeds automatically.
4. Join the existing embedding index to the completed EAGGL→CFDE run; wire mapped-only ranking and GeneSet alias lookups. Embed selected DisMech query text with the same provider/model and persist input hashes. Full factor re-embedding and improved mappings can be loaded later as new runs.
5. Verify exact source-attached suggestions, fuzzy search, true semantic search, and bounded one-round expansion using the live API fixtures.
6. Add account/claim/provenance, bibliographic metadata, and paragraph storage; implement BibTeX/CSL export and APA/MLA rendering. Exercise idempotent retry, rollback, version pinning, stable issue dates/bylines, acyclic citation identity, and durable resolution.

## 6. Re-run the account audit

```bash
python3 -m pip install -r scripts/requirements-audit.txt
python3 scripts/audit_mysql.py \
  --host aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com \
  --user cyaka \
  --ca-file /path/to/verified-rds-ca-bundle.pem
```

The script prompts for the password without echoing it and never performs DDL or DML. Obtain the [AWS RDS CA bundle](https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem) for certificate verification. Production credentials belong in the independently deployed EC2 REST API/worker secret configuration. Next.js and other frontends call that API rather than connecting to RDS directly. Upstash Box receives frozen evidence inputs and scoped tool access; the application backend persists its validated results.
