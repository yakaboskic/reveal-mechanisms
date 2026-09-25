# Graph Report - /Users/cyakaboski/src/research/reveal-mechanisms  (2026-09-25)

## Corpus Check
- Large corpus: 193 files · ~508,860 words. Semantic extraction will be expensive (many Claude tokens). Consider running on a subfolder.

## Summary
- 1718 nodes · 4583 edges · 94 communities
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 93 edges (avg confidence: 0.76)
- Token cost: unavailable; host-agent tools do not expose per-agent usage (zero placeholders are not measured usage).

## Community Hubs (Navigation)
- API Contract Definitions
- OpenAPI Generation and Examples
- DAPPER API Payloads
- Application Workflow Contracts
- Importer Integration Tests
- Evidence JSON Schema
- Architecture and Design History
- Embedded DAPPER Package Models
- Prisma and Database Models
- EAGGL Import Pipeline
- EAGGL Bundle Tests
- Research Application Flow
- Account Interface Components
- LinkML Evidence Schema
- DisMech and Factor Mapping
- Command Line Entry Points
- Evidence Collection and Assembly
- Scientific Evidence Entities
- Package Provenance References
- Evidence Package API Contracts
- Evidence Builder Tests
- Scientific Account Validation
- Schema Validation Tests
- Embedding Service Client
- Prototype and Snapshot Builders
- CFDE GeneSet Import
- Evidence Context References
- CFDE Capture and Viewers
- Prototype Design Review
- Workspace Identity Interface
- Agent Evidence Boundary
- GeneSet Import Tests
- PIGEAN Scientific Claims
- Results and Citation Metadata
- DAPPER Integration and Provenance
- Pinned Agent Runtime
- DisMech Import Tests
- Evidence Package Build Policy
- Typed Evidence Records
- Package Coverage Metadata
- Evidence Interpretation Rules
- Identity Gateway Contract
- Scientific Account Construction
- Evidence Example Generation
- EAGGL CFDE Crosswalk
- DisMech Context Schema
- Evidence Coverage and Queries
- Trait Association Evidence
- Deterministic Evidence Builder
- Implementation Acceptance Gates
- Mechanism Search Contracts
- Paragraph and Citation Interface
- Graph Evidence Schema
- Authentication and Workspace Ownership
- Graph Evidence Definitions
- Backend Dependency Extras
- GeneSet Crosswalk Tables
- DisMech Source Extraction
- Importer Python Dependencies
- Package Trait Observations
- Current API Amendments
- Prisma Connection Configuration
- Factor Selection Metadata
- OpenAPI Python Dependencies
- Revision Inventory Validation
- People and Identity API
- Dataset Provenance Types
- Semantic Retrieval Schema
- Historical CAD Evidence Example
- Selected Gap Framing
- Historical BioIndex Recipes
- Scientific Attribution Types
- Source Artifact Formats

## God Nodes (most connected - your core abstractions)
1. `DapperDocument` - 71 edges
2. `PackageEPDapperContext` - 42 edges
3. `EPDapperContext` - 41 edges

## Surprising Connections (you probably didn't know these)
- `Mapped candidate retrieval` --references--> `lookup_factor()`  [EXTRACTED]
  docs/implementation-handoff.md → services/backend/src/reveal_backend/eaggl_cfde_links.py

## Import Cycles
- 1-file cycle: `design/build_account_example.py -> design/build_account_example.py`
- 1-file cycle: `scripts/import_cfde_genesets.py -> scripts/import_cfde_genesets.py`
- 1-file cycle: `design/build_evidence_package_example.py -> design/build_evidence_package_example.py`
- 1-file cycle: `schema/prisma/prisma.config.ts -> schema/prisma/prisma.config.ts`
- 1-file cycle: `scripts/audit_dapper_integration.py -> scripts/audit_dapper_integration.py`
- 1-file cycle: `scripts/audit_mysql.py -> scripts/audit_mysql.py`
- 1-file cycle: `scripts/build_api_flow.py -> scripts/build_api_flow.py`
- 1-file cycle: `scripts/build_openapi.py -> scripts/build_openapi.py`
- 1-file cycle: `scripts/import_dismech.py -> scripts/import_dismech.py`
- 1-file cycle: `services/backend/src/reveal_backend/dismech_import.py -> services/backend/src/reveal_backend/dismech_import.py`
- 1-file cycle: `scripts/import_eaggl_factors.py -> scripts/import_eaggl_factors.py`
- 1-file cycle: `services/backend/src/reveal_backend/eaggl_bundle.py -> services/backend/src/reveal_backend/eaggl_bundle.py`
- 1-file cycle: `scripts/probe_interactive_api.py -> scripts/probe_interactive_api.py`
- 1-file cycle: `scripts/test_geneset_import.py -> scripts/test_geneset_import.py`
- 1-file cycle: `scripts/validate_genesets.py -> scripts/validate_genesets.py`
- 1-file cycle: `scripts/validate_openapi.py -> scripts/validate_openapi.py`
- 1-file cycle: `services/backend/src/reveal_backend/embedding_client.py -> services/backend/src/reveal_backend/embedding_client.py`
- 1-file cycle: `services/backend/src/reveal_backend/evidence_schema.py -> services/backend/src/reveal_backend/evidence_schema.py`
- 2-file cycle: `design/build_account_example.py -> scripts/import_cfde_genesets.py -> design/build_account_example.py`
- 3-file cycle: `scripts/import_cfde_genesets.py -> scripts/pull_cfde.py -> scripts/probe_interactive_api.py -> scripts/import_cfde_genesets.py`

## Hyperedges (group relationships)
- **Selected-gap research and automatic expression flow** — readme_selected_dismech_gap, readme_mechanism_anchors, api_readme_composer, api_readme_shared_jobs, api_readme_paragraph_outbox, docs_citation_standard_citation_registry [EXTRACTED 1.00]
- **Trusted input → per-account output → final acceptance boundary** — api_examples_evidence_package_evidence_package_evidence_package, api_examples_evidence_package_evidence_package_dispatch_readiness, docs_agent_evidence_integration_complete_mcp_ledger, docs_agent_output_contract_one_account_per_document, docs_agent_output_contract_trusted_acceptance, docs_dapper_integration_scientific_grounding [EXTRACTED 1.00]
- **Reproducible scientific identity, citation metadata and rendered expression** — docs_citation_standard_citation_registry, docs_citation_standard_acyclic_citation_identity, docs_citation_standard_citation_occurrence, docs_citation_standard_csl_rendering, docs_citation_standard_ordered_attribution [EXTRACTED 1.00]
- **Initial retrieval: embedded source labels, versioned route, native collector inputs** — docs_eaggl_factor_import_database_search_index, docs_eaggl_cfde_links_exact_trait_factor_mapping, docs_eaggl_cfde_links_mapping_run, docs_evidence_package_builder_collector [EXTRACTED 1.00]
- **Frozen observations and append-only enrichment feed proposition assessments** — docs_evidence_package_evidence_package, docs_evidence_package_enrichment_ledger, docs_evidence_package_biological_propositions, docs_evidence_package_cfde_ancestry [EXTRACTED 1.00]
- **Verified identity and anonymous ownership converge through workspace claim while preserving authorship** — docs_gateway_contract_anonymous_principal, docs_gateway_contract_verified_identity, docs_gateway_contract_workspace_claim, docs_gateway_contract_ownership_authorship_separation [EXTRACTED 1.00]
- **Four PIGEAN/EAGGL biological involvement templates** — docs_pigean_claim_model_gene_mechanism_template, docs_pigean_claim_model_gene_set_mechanism_template, docs_pigean_claim_model_gene_trait_template, docs_pigean_claim_model_gene_set_trait_template, docs_scientific_account_construction_proposition, docs_scientific_account_construction_evidenceitem [EXTRACTED 1.00]
- **Pinned startup and independently validated scientific-account output** — scripts_start_research_agent, services_backend_agent_runtime_dapper_release, scripts_lint_scientific_account, services_backend_src_reveal_backend_scientific_account_lint, services_backend_src_reveal_backend_scientific_account_lint_validate_scientific_account, docs_scientific_account_linting_independent_final_validation [EXTRACTED 1.00]
- **EAGGL search to native CFDE anchor and DAPPER GeneSet routing** — services_backend_src_reveal_backend_eaggl_database_database_search_index, services_backend_src_reveal_backend_eaggl_cfde_links_lookup_factor, schema_prisma_schema_eagglcfdelinkrun, schema_prisma_schema_eagglcfdefactorlink, schema_prisma_schema_eagglcfdegenesetlink, schema_prisma_schema_cfdegenesetalias, schema_prisma_schema_dapperobject [EXTRACTED 1.00]

## Communities (94 total, 0 thin omitted)

### Community 0 - "API Contract Definitions"
Cohesion: 0.03
Nodes (83): AnalysisJobInput, CitationRendering, CitationRenderInput, CitationTarget, DapperAgent, DapperAward, DapperCausalLinkTypeEnum, DapperCausalStep (+75 more)

### Community 1 - "OpenAPI Generation and Examples"
Cohesion: 0.05
Nodes (31): CslItem, exportParagraph, getCitation, add(), application_schemas(), array(), canonical(), citation() (+23 more)

### Community 2 - "DAPPER API Payloads"
Cohesion: 0.04
Nodes (67): AccountSummary, DapperAccessLevelEnum, DapperActivity, DapperAgenticWorkspace, DapperAncestryContext, DapperAncestryEnum, DapperAssertedIn, DapperBioComputeObject (+59 more)

### Community 3 - "Application Workflow Contracts"
Cohesion: 0.06
Nodes (56): AccountCount, AccountList, ActivityDetail, AnalysisResult, Attachment, AttributionSnapshot, Composer, Draft (+48 more)

### Community 4 - "Importer Integration Tests"
Cohesion: 0.07
Nodes (17): io, numpy, BioIndexPaginationTests, Offline regressions for source-boundary and completeness behavior., REVEAL backend components., DismechMySQLTests, Opt-in real MySQL tests, restricted to a disposable localhost test database.  RE, MappingMySQLTests (+9 more)

### Community 5 - "Evidence JSON Schema"
Cohesion: 0.04
Nodes (53): Agent, AssertedIn, Award, CausalLinkTypeEnum, CausalStep, ClaimScore, ClaimScoreKindEnum, CorrespondsToDismech (+45 more)

### Community 6 - "Architecture and Design History"
Cohesion: 0.15
Nodes (40): index.html (reference only), citation-registry.json (reference only), paragraph-object.json (reference only), design/data/cad-account/README.md, hubmap.provenance.yaml (reference only), design-plan-2026-09-15.md (historical; superseded by consolidated plan), Historical AgentRunner and PostgreSQL proposal, design-plan-2026-09-24-v2.md (historical; superseded by consolidated plan) (+32 more)

### Community 7 - "Embedded DAPPER Package Models"
Cohesion: 0.05
Nodes (50): PackageAccessLevelEnum, PackageActivity, PackageAgenticWorkspace, PackageAncestryContext, PackageAncestryEnum, PackageAward, PackageBioComputeObject, PackageC2M2File (+42 more)

### Community 8 - "Prisma and Database Models"
Cohesion: 0.09
Nodes (45): DisMech gap attachment preservation, Paired DisMech snapshot validation, Prepared DisMech tables (not loaded into Aurora), Resumable DisMech imports, DisMech source-specific database import, CFDE GeneSet catalog import, Immutable DAPPER GeneSet identity, Resumable GeneSet import (+37 more)

### Community 9 - "EAGGL Import Pipeline"
Cohesion: 0.12
Nodes (41): eaggl_embedding_runs, eaggl_factors, eaggl_gene_loadings, eaggl_genes, eaggl_graph_edges, eaggl_graph_nodes, eaggl_imports, eaggl_name_embeddings (+33 more)

### Community 10 - "EAGGL Bundle Tests"
Cohesion: 0.12
Nodes (8): BundleTests, CaptureFixture, DatabaseTests, EmbeddingTests, Bundle alignment, provenance, resumability, service wiring and retrieval invaria, Exercise loader transactions/constraints locally, not MySQL engine compatibility, SQLiteCursor, SQLiteMySQLAdapter

### Community 11 - "Research Application Flow"
Cohesion: 0.09
Nodes (41): Gap → anchors → draft → analysis → account → paragraph flow, POST /v1/jobs/{job_id}/cancel (cancelJob), POST /v1/drafts (createDraft), POST /v1/jobs (createJob), GET /v1/accounts/{dapper_id} (getAccount), GET /v1/claims/{dapper_id} (getClaim), GET /v1/drafts/{draft_id} (getDraft), GET /v1/jobs/{job_id}/evidence-package (getEvidencePackage) (+33 more)

### Community 12 - "Account Interface Components"
Cohesion: 0.11
Nodes (37): accountClaims(), accountPage(), accountPreview(), alignClaimSelection(), artifactLink(), citationModel(), citedParagraphText(), claimDetail() (+29 more)

### Community 13 - "LinkML Evidence Schema"
Cohesion: 0.10
Nodes (39): DAPPER Activity, DAPPER AgenticWorkspace, DAPPER Award, DAPPER BioComputeObject, DAPPER C2M2File, DAPPER CausalStep, DAPPER CellState, DAPPER Claim (+31 more)

### Community 14 - "DisMech and Factor Mapping"
Cohesion: 0.15
Nodes (34): main(), main(), canonical(), digest(), Export, load_export(), locator(), open_export() (+26 more)

### Community 15 - "Command Line Entry Points"
Cohesion: 0.12
Nodes (20): argparse, collections, contextlib, datetime, dotenv, fcntl, getpass, itertools (+12 more)

### Community 16 - "Evidence Collection and Assembly"
Cohesion: 0.14
Nodes (26): certifi, math, re, main(), CaptureStore, collect_package(), gz_records(), parse_factor() (+18 more)

### Community 17 - "Scientific Evidence Entities"
Cohesion: 0.07
Nodes (36): AccessLevelEnum, AgenticWorkspace, BioComputeObject, C2M2File, CellState, CitationOccurrence, Claim, ClaimStatusEnum (+28 more)

### Community 18 - "Package Provenance References"
Cohesion: 0.08
Nodes (35): PackageEPAttachedContext, PackageEPAttachment, PackageEPAttachmentResolution, PackageEPCandidateEvidence, PackageEPCandidateEvidence__identifier_optional, PackageEPDismechMechanism, PackageEPDismechMechanism__identifier_optional, PackageEPFit (+27 more)

### Community 19 - "Evidence Package API Contracts"
Cohesion: 0.09
Nodes (33): EvidencePackage, EvidencePackageResult, PackageEPArtifactFormat, PackageEPAuthoring, PackageEPBioIndexCoverage, PackageEPBioIndexProgress, PackageEPBuilderPin, PackageEPBuildPolicy (+25 more)

### Community 20 - "Evidence Builder Tests"
Cohesion: 0.17
Nodes (9): HttpCaptureClient, Bounded public read requests; every attempt and exact response body is saved., EvidenceBuildError, ValueError, Frozen inputs are inconsistent, incomplete, or outside the declared policy., EvidencePackageTests, FixtureClient, Source-based collection fixtures and deterministic replay/validation invariants. (+1 more)

### Community 21 - "Scientific Account Validation"
Cohesion: 0.16
Nodes (13): dataclasses, main(), AccountValidationError, lint_scientific_account(), ValueError, One scientific-account linter shared by agent feedback and backend validation., Return a machine-readable report. Files are read, never minted or edited., Backend gate: rerun the same linter on actual output, in final mode.      Passin (+5 more)

### Community 22 - "Schema Validation Tests"
Cohesion: 0.14
Nodes (14): functools, jsonschema, jsonschema_validators, linkml_generators_jsonschemagen, linkml_runtime_utils_schemaview, main(), generate_schema(), LinkML-derived structural validation for the evidence-package wire format.  This (+6 more)

### Community 23 - "Embedding Service Client"
Cohesion: 0.15
Nodes (20): ndarray, random, Embedding service contract, _backoff(), cosine_similarity(), _embed_batch(), get_embeddings(), _make_ssl_context() (+12 more)

### Community 24 - "Prototype and Snapshot Builders"
Cohesion: 0.15
Nodes (19): citation_metadata, dapper_identity, main(), Number exact target/revision pairs once, including repeated citation spans., write_json(), write_paragraph_exports(), main(), read() (+11 more)

### Community 25 - "CFDE GeneSet Import"
Cohesion: 0.22
Nodes (22): importlib_util, bulk_stage(), bulk_store(), canonical(), check_export(), compressed_rows(), digest(), encode() (+14 more)

### Community 26 - "Evidence Context References"
Cohesion: 0.12
Nodes (24): EPAttachedContext, EPAttachment, EPAttachmentResolution, EPDismechContext, EPDismechMechanism, EPDismechMechanism__identifier_optional, EPFit, EPGapKind (+16 more)

### Community 27 - "CFDE Capture and Viewers"
Cohesion: 0.15
Nodes (17): concurrent_futures, hashlib, main(), main(), collect(), capture(), main(), fetch() (+9 more)

### Community 28 - "Prototype Design Review"
Cohesion: 0.16
Nodes (17): Illustrative KG membership assertions, Captured PIGEAN/EAGGL assessment examples, Authored CAD-in-T2D research statement fixture, Historical UI contract amendments, Observable public agent transcript, Source-owned mechanism links, canSubmit, previewJobEvents (+9 more)

### Community 29 - "Workspace Identity Interface"
Cohesion: 0.17
Nodes (19): chooseWorkspaceIdentity(), closeWorkspaceMenu(), historyUpsert(), mergeHistory(), openWorkspace(), persistWorkspace(), readWorkspaceStorage(), rememberAccount() (+11 more)

### Community 30 - "Agent Evidence Boundary"
Cohesion: 0.19
Nodes (16): CAD PGS×context reverse-causation gap, CADinT2D cfde-inc-v2 Factor1, Explicit input capture and coverage, Dispatch readiness gates, Captured EvidencePackage 0.2-draft, Unqueried selected external evidence, BiomarkerKG / BiomarkerKB KG, Planned Claude Code in Upstash Box (+8 more)

### Community 31 - "GeneSet Import Tests"
Cohesion: 0.17
Nodes (5): BulkStagingTests, CatalogTests, PinnedIdentityTests, GeneSet import invariants: exact scope, faithful metadata, identity and storage., zlib

### Community 32 - "PIGEAN Scientific Claims"
Cohesion: 0.22
Nodes (17): design/data/cad-account/scientific-account.yaml, beta and beta_uncorrected, Biological direction versus evidence direction, combined, log_bf and prior, factor_value loading, Gene → mechanism involvement template, Gene-set → mechanism involvement template, Gene-set → trait involvement template (+9 more)

### Community 33 - "Results and Citation Metadata"
Cohesion: 0.22
Nodes (18): AccountResult, ArtifactAccess, CitationContributor, CitationMetadata, CitationProvenance, ClaimResult, DapperFile, GeneSetResult (+10 more)

### Community 34 - "DAPPER Integration and Provenance"
Cohesion: 0.18
Nodes (17): GET /v1/paragraphs/{dapper_id}/export (exportParagraph), GET /v1/citations/{dapper_id} (getCitation), POST /v1/citations/render (renderCitations), Acyclic citation identity, Versioned external citation registry, Pinned whole-document CSL rendering, DAPPER scientific model integration, Target-specific EvidenceItems and shared sources (+9 more)

### Community 35 - "Pinned Agent Runtime"
Cohesion: 0.26
Nodes (14): importlib_metadata, clone_release(), git(), prepare_agent_workspace(), Clone and verify the DAPPER release selected by the trusted worker., Mandatory startup preparation, reusable by the future Box worker.      Dependenc, verify_release(), deepcopy_record() (+6 more)

### Community 36 - "DisMech Import Tests"
Cohesion: 0.29
Nodes (5): Small export pair with cross-document and ambiguous gap attachments., records(), rewrite(), write_fixture(), DismechValidationTests

### Community 37 - "Evidence Package Build Policy"
Cohesion: 0.17
Nodes (16): EPArtifactFormat, EPAuthoring, EPBuilderPin, EPBuildPolicy, EPDapperPin, EPFileID, EPIdentifierPolicy, EPInstruction (+8 more)

### Community 38 - "Typed Evidence Records"
Cohesion: 0.16
Nodes (16): EPDismechMechanism, EPEntities, EPFit, EPGeneBinding, EPGeneSetBinding, EPGeneSetID, EPInteractiveObservation, EPKeyedRecord (+8 more)

### Community 39 - "Package Coverage Metadata"
Cohesion: 0.13
Nodes (16): EPAuthoring, EPBioIndexCoverage, EPBioIndexProgress, EPBuilderPin, EPBuildPolicy, EPCoverage, EPDapperPin, EPEntities (+8 more)

### Community 40 - "Evidence Interpretation Rules"
Cohesion: 0.18
Nodes (12): Trusted scientific account acceptance, Evidence observations to biological propositions, CFDE ancestry for account findings, Append-only Proto-OKN enrichment ledger, Distinct loading, association and similarity quantities, Overlapping source observation accounting, Field-aware CURIE resolution, Semantic suggestion provenance (+4 more)

### Community 41 - "Identity Gateway Contract"
Cohesion: 0.17
Nodes (13): Owned gap/account dashboard, Scientific identity and application ownership boundary, Server-provisioned anonymous principal, Private ownership versus scientific authorship, Trusted Next.js-to-EC2 identity gateway, Verified issuer/subject identity mapping, Atomic anonymous workspace claim, AnonymousProvisionInput (+5 more)

### Community 42 - "Scientific Account Construction"
Cohesion: 0.22
Nodes (10): Post-acceptance Paragraph generation, Closing synthesis, EAGGL factor is a mechanism, File, KnowledgeGap, Mechanism, Paragraph, ScientificAccount (+2 more)

### Community 43 - "Evidence Example Generation"
Cohesion: 0.29
Nodes (13): csv, build_legacy(), main(), package_prefixes(), pointer(), Small native-source fragment, deliberately not a cfde-inc-v2 alias., Validate declared identifier slots/map keys, without rewriting source text or mi, read() (+5 more)

### Community 44 - "EAGGL CFDE Crosswalk"
Cohesion: 0.22
Nodes (12): Initial crosswalk-backed retrieval, Atomic crosswalk load, Exact trait + factor-number routing, Ranked CFDE GeneSet summary links, CFDE native-ID lookup, Pinned completed mapping run, Capped factor-gene weights, Database-backed cosine search index (+4 more)

### Community 45 - "DisMech Context Schema"
Cohesion: 0.20
Nodes (14): EPAttachedContext, EPAttachment, EPAttachmentResolution, EPDismechContext, EPGapKind, EPJSONValue, EPKnowledgeGapContext, EPProvidedSubset (+6 more)

### Community 46 - "Evidence Coverage and Queries"
Cohesion: 0.22
Nodes (13): Bounded query coverage, EPBioIndexCoverage, EPBioIndexProgress, EPContextualRelationships, EPCount, EPCoverage, EPGraph, EPPigeanEvidence (+5 more)

### Community 47 - "Trait Association Evidence"
Cohesion: 0.20
Nodes (12): EPContextualRelationships, EPGraph, EPPigeanEvidence, EPQueryStatus, EPReportedMetrics, EPRetainedEdge, EPTraitAssociations, EPTraitEntityObservations (+4 more)

### Community 48 - "Deterministic Evidence Builder"
Cohesion: 0.25
Nodes (10): Frozen-seed one-round CFDE expansion, Evidence collector, Pure deterministic evidence builder, Pinned GeneSet alias hydration, Portable offline replay, Input capture versus dispatch readiness, Deterministic retained graph, Eleven-request single-factor capture (+2 more)

### Community 49 - "Implementation Acceptance Gates"
Cohesion: 0.33
Nodes (7): Job attempts, outbox and leases, DAPPER 0.2.0-a1, Independent final validation, Pinned agent startup, Scientific acceptance gates, Schema and builder validation, main()

### Community 50 - "Mechanism Search Contracts"
Cohesion: 0.24
Nodes (10): CfdeAnchor, DapperMechanism, DismechMechanism, EagglFactor, GapHit, MechanismHit, MechanismRecord, Rank (+2 more)

### Community 51 - "Paragraph and Citation Interface"
Cohesion: 0.31
Nodes (9): reveal-paragraph terminal profile, Transactional Paragraph outbox, chooseWorkspaceIdentity, citationModel, citedParagraphText, mergeHistory, persistWorkspace, rememberAccount (+1 more)

### Community 52 - "Graph Evidence Schema"
Cohesion: 0.27
Nodes (10): EPCandidateEvidence, EPCandidateEvidence__identifier_optional, EPExternalEvidence, EPGraphEdge, EPGraphNode, EPInteractiveCandidate, EPNodeType, EPNotQueried (+2 more)

### Community 53 - "Authentication and Workspace Ownership"
Cohesion: 0.44
Nodes (8): Anonymous upgrade and explicit account claim, Versioned draft autosave, GatewayAssertion trust boundary, ORCID and Google identity providers, Portable application user UUID, Planned registered and anonymous sessions, Ordered human and AI attribution, Proposed application persistence

### Community 54 - "Graph Evidence Definitions"
Cohesion: 0.28
Nodes (9): EPCandidateEvidence, EPExternalEvidence, EPGraphEdge, EPGraphNode, EPInteractiveCandidate, EPNodeType, EPNotQueried, EPRawObject (+1 more)

### Community 55 - "Backend Dependency Extras"
Cohesion: 0.32
Nodes (5): pkg_certifi, pkg_numpy, reveal-backend, Backend dismech extra, Backend eaggl extra

### Community 56 - "GeneSet Crosswalk Tables"
Cohesion: 0.46
Nodes (6): cfde_gene_set_aliases, dapper_objects, gene_set_imports, eaggl_cfde_factor_links, eaggl_cfde_gene_set_links, eaggl_cfde_link_runs

### Community 57 - "DisMech Source Extraction"
Cohesion: 0.39
Nodes (6): extract(), json_file(), jsonl(), normalize(), walk(), unicodedata

### Community 58 - "Importer Python Dependencies"
Cohesion: 0.25
Nodes (5): PyMySQL==1.1.2, linkml==1.11.1, rdflib==7.6.0, ruamel.yaml==0.18.17, PyYAML==6.0.2

### Community 59 - "Package Trait Observations"
Cohesion: 0.33
Nodes (7): PackageEPReportedMetrics, PackageEPTraitAssociations, PackageEPTraitEntityObservations, PackageEPTraitEntityObservations__identifier_optional, PackageEPTraitEvidence, PackageEPTraitEvidence__identifier_optional, PackageEPTraitObservation

### Community 60 - "Current API Amendments"
Cohesion: 0.33
Nodes (5): copy, fixtures(), Current design-contract amendments. Generates documentation only, no server rout, Use the same multi-claim CAD fixture as the approved HTML design., shutil

### Community 61 - "Prisma Connection Configuration"
Cohesion: 0.29
Nodes (5): dotenv, node_path, node_url, prisma_config, root

### Community 62 - "Factor Selection Metadata"
Cohesion: 0.33
Nodes (7): EPExpansionPolicy, EPFactorID, EPReportedMetrics, EPSelection, EPSelectionOrigin, EPTraitEntityObservations, EPTraitObservation

### Community 63 - "OpenAPI Python Dependencies"
Cohesion: 0.29
Nodes (6): jsonschema==4.26.0, linkml==1.11.1, linkml-runtime==1.11.1, openapi-spec-validator==0.7.2, PyYAML==6.0.2, rdflib==7.6.0

### Community 64 - "Revision Inventory Validation"
Cohesion: 0.67
Nodes (6): main(), read(), rows(), validate_api(), validate_database_capture(), validate_gaps()

### Community 65 - "People and Identity API"
Cohesion: 0.40
Nodes (6): DapperContributorRoleEnum, DapperCreatorRoleEnum, DapperOrganization, DapperPerson, Me, getMe

### Community 66 - "Dataset Provenance Types"
Cohesion: 0.33
Nodes (6): Activity, AncestryContext, AncestryEnum, Dataset, IdentifierTypeEnum, ResourceTypeEnum

### Community 67 - "Semantic Retrieval Schema"
Cohesion: 0.33
Nodes (6): EPExpansionPolicy, EPSelection, EPSelectionOrigin__identifier_optional, EPSemanticAssociation, EPSemanticRetrieval, EPSimilarityStatus

### Community 68 - "Historical CAD Evidence Example"
Cohesion: 0.60
Nodes (4): CAD illustrative observation coverage, Historical CAD evidence package fixture, CAD PGS×context reverse-causation knowledge gap, CAD-in-T2D cfde-inc-v2 Factor1

### Community 69 - "Selected Gap Framing"
Cohesion: 0.67
Nodes (3): api/history/v9/openapi.json, CAD account fixture, Exact selected-gap invariant

### Community 70 - "Historical BioIndex Recipes"
Cohesion: 0.50
Nodes (4): Historical September 15 CFDE BioIndex recipes, Model-scoped factor identity, Historical BioIndex membership limitation, BioIndex continuation and byte-progress completeness

### Community 71 - "Scientific Attribution Types"
Cohesion: 0.67
Nodes (4): ContributorRoleEnum, CreatorRoleEnum, Organization, Person

### Community 72 - "Source Artifact Formats"
Cohesion: 0.67
Nodes (4): EPArtifactFormat, EPRepositoryOrigin, EPSourceArtifact, EPSourceArtifact__identifier_optional

## Knowledge Gaps
- **128 isolated node(s):** `paragraphJobs`, `workspace`, `root`, `GatewayAssertion`, `DapperAgent` (+123 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **How do the EAGGL import scripts connect to Prisma models and CFDE GeneSets?**
  _The graph links importer modules, SQL tables, Prisma mappings, and the trait/factor crosswalk._

## Build coverage and integrity

- 194 unique scoped source files, including 51 Python files, all four SQL migrations, and all 23 Prisma models.
- Secrets, bulk data captures, vendored code, dependency installations, and redundant generated UI bundles were excluded by `.graphifyignore`. Explicit links to excluded artifacts are reference-only nodes.
- SQL parsing uses Graphify’s SQL extra. The local build adapter adds JSON pointer references, external dependency nodes, canonical cross-migration FK targets, and literal code-to-table references. Six spurious Python-to-JSON symbol matches were removed after source inspection.
- No dangling or missing endpoints and no self-loops. The default undirected graph combines 304 parallel/opposite-direction relationships; `extraction.json` preserves all 4,887 source-oriented edge records and nine hyperedges.
- Historical/planned documentation is descriptive; this graph does not verify deployments or live database state.
- Full inventory: `SOURCE_COVERAGE.json`; health diagnostic: `GRAPH_HEALTH.json`; adapter audit: `BUILD_ADAPTER_AUDIT.json`.
