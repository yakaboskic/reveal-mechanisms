# DAPPER object citations — REVEAL profile v1

**Status:** aligned to current DAPPER, September 24, 2026 (design v8). DAPPER implements the external metadata schema, Paragraph/CitationOccurrence fields, and metadata/span/link validators. REVEAL registry persistence, resolvers, exporters, and citation UI remain planned; no records have been publicly published or assigned DOIs by this work. See [the integration audit](dapper-integration.md).

Every saved **Claim, Question, and KnowledgeGap** should be citable as an individual scholarly object. A reader should be able to copy BibTeX, select APA or MLA, and follow a durable link to the exact object, its attribution, and its supporting evidence. ScientificAccounts and GeneSets can adopt the same profile later.

## 1. Identity, dates, and the citation record

Use the complete, case-sensitive `dapper:<Class>.<digest>` as the canonical scientific identifier. A KnowledgeGap is cited by its own most-specific class and digest, even though it inherits from Question. Citations to a Claim identify the attributed assessment, not merely its Proposition. A different assessment or changed hashable content follows DAPPER's existing identity rules.

Create a **versioned citation record keyed to the already-minted object**. Its shape is defined by DAPPER's [citation-record JSON Schema](../data/dapper/2026-09-24-v8/snapshot/schema/citations/citation-record.schema.json), with additional date/provenance checks in [citation_metadata.py](../data/dapper/2026-09-24-v8/snapshot/schema/citation_metadata.py). It is intentionally an external application record, not a DAPPER scientific node. It contains:

- `target_id`, `target_class`, pinned DAPPER schema/identity profile, and object payload reference.
- `metadata_revision`, `citation_profile_version`, `title`, `language`, and ordered byline.
- Structured contributors: agent ID, person/software/organization kind, display name or given/family name, ORCID where supplied, and per-object roles. Keep source attribution separate from platform publishing roles.
- `generated_at`, when supported by the generating activity; `first_minted_at`, the platform's first successful durable registration of this digest; and nullable `published_at`, when the platform actually made it public. Store UTC timestamps and their provenance. Imported objects also retain their original issue date when known and local import time.
- `issued_date` and `issued_basis`: the documented date selected for citation display. For newly generated records with a known first durable mint, use that date; retain publication and generation dates separately. An imported, previously issued object uses its supported original issue date. Reimports never reset the citation year.
- `publisher`/`repository`, canonical resolver URL, optional registered DOI, and optional readable accession alias.
- Generation activity, agent/model/harness versions, responsible human operator/publisher, source revisions, and relationships to prior object digests when available.
- Access/publication state and optional correction/withdrawal notices, distinct from scientific support or review status.

A missing date, author, ORCID, or model version stays missing or is explicitly marked unknown. Do not infer a date from a content digest or use the export date as the mint date. The same object reused by another account retains its original citation record; the new usage or publishing event is recorded separately.

**The digest is not a DOI.** Export `doi`/`DOI` only for a registered DOI referring to this exact cited object/version. Store the DAPPER identifier in the entry key, `eprint`, structured metadata, and resolver URL. A DOI belonging to an underlying paper belongs to that paper's reference. The [DOI Handbook](https://www.doi.org/doi-handbook/html/index.html) defines DOI prefix/suffix syntax and registration.

**`YYMM.NNNNN` is an accession, not a timestamp.** arXiv combines year/month with a sequence number and uses a version suffix for a specific version. REVEAL can add a separately allocated readable accession later, but it is not required for citation v1 and never replaces the DAPPER digest. Dates go in date fields. See [arXiv's identifier definition](https://info.arxiv.org/help/arxiv_identifier.html).

## 2. Attribution and the byline

The platform-native byline should credit the **human who authored or managed/published the work and the AI agent that generated it**, as requested. Preserve the actual contributions explicitly:

NextAuth login through ORCID or Google supplies the initiating human's identity under the [authentication contract](authentication.md). Save the trusted principal and attribution snapshot with the research activity. ORCID sign-in authenticates the ORCID account; Google-only sign-in supplies no authenticated ORCID. Missing provider fields stay nullable, typed profile additions retain their source/verification status, and email is private contact metadata rather than a public citation field. Returning logins/profile changes do not rewrite historical bylines. A valid login is operator provenance, not evidence of authorship of imported material or scientific endorsement.

The platform principal is a permanent internal application user ID resolved through verified external identity mappings. Citation attribution snapshots and DAPPER Person references are pinned separately from current login/profile data. A replacement auth implementation maps to the same application user; it does not remint historical scientific objects or rewrite their bylines. Draft autosave does not mint citable scientific content on every edit.

- People have stable agent references, supplied display/given/family names, and optional ORCID URLs. Record whether an ORCID was merely supplied or verified through the platform's identity flow; a name match does not verify it.
- AI credit has an explicit software-agent kind, provider, recorded model ID/version, Claude Code harness/version, and generating activity/run reference. Format it as a literal name, such as `Claude [model ID] via Claude Code (AI generator)`, without parsing it as a person's surname. Never give an AI agent an ORCID.
- Ordered role-bearing entries distinguish `author`, `ai_generator`, `agent_operator`, `publisher`, and `curator`. These are the implemented registry contract's roles; they need not be forced into unrelated DAPPER Person enums. A person may hold several roles. Publication/operator status alone does not assert scientific endorsement.
- REVEAL Mechanisms is the repository/publisher; Anthropic is model provenance. Neither automatically replaces the responsible human or source author in the byline.
- Imported DisMech questions/gaps retain their available source authorship. The importing user/agent is credited for import or publication, without being represented as the original author of the source question.
- Order is stored explicitly at issuance. Do not infer author order from an unordered `was_attributed_to` relation or change it during formatting. Unknown source authors remain unknown; do not substitute the importing user.

For the default platform export, map the ordered credited byline into bibliography names, with humans as given/family names and software agents as literal names. Preserve full role and ORCID data in native metadata and the landing page. Conventional bibliography formats can lose these details; they are projections, not the authoritative attribution store. A future destination-specific export profile can change how AI credit is displayed without deleting its provenance. Formatting in an APA or MLA style does not establish a journal's acceptance of AI authorship.

## 3. Titles and exact versions

Use a faithful, readable title for each object:

- Claim: an existing supported title/name, otherwise a deterministic rendering of the Claim's assessment statement, falling back to its Proposition statement with the assessment's direction/scope clearly retained. A disputing Claim must not acquire a title asserting support.
- Question: its inquiry text, or a faithful short title linked to the full text.
- KnowledgeGap: its inquiry text as the title; preserve the separate `gap_description` on the landing page and native record.

Store title derivation/version; never silently strengthen certainty, imply a gap is resolved, or invent a result to make a nicer citation. A presentation-only correction creates a citation metadata revision. A change to hashable scientific text or attribution creates whatever new DAPPER object the pinned schema requires. Cite the exact digest and retain historical citation metadata revisions for reproducible exports.

## 4. BibTeX and BibLaTeX

Use `@misc` as the initial portable export type. This **template** uses `EXAMPLE_DIGEST`, a fictional author, and a reserved example URL; it is not an issued record or valid minted DAPPER ID.

```bibtex
@misc{dapper:Question.EXAMPLE_DIGEST,
  title         = {How does mechanism X contribute to disease Y?},
  author        = {Researcher, Example and {Claude [model ID] via Claude Code (AI generator)}},
  year          = {2026},
  month         = sep,
  archivePrefix = {DAPPER},
  eprint        = {Question.EXAMPLE_DIGEST},
  howpublished  = {REVEAL Mechanisms: DAPPER Question},
  url           = {https://example.org/id/dapper:Question.EXAMPLE_DIGEST},
  note          = {First minted 2026-09-24. DAPPER ID: dapper:Question.EXAMPLE\_DIGEST. Human role: agent operator and publisher.}
}
```

For Claims and KnowledgeGaps, substitute the actual class, digest, title, contributors, and dates. `archivePrefix + eprint` reconstructs the namespace and class-qualified digest; the full native identifier is always retained. No `doi` field is emitted when no registered DOI exists.

BibLaTeX export additionally uses a full `date` and an `eprinttype`/`eprintclass` mapping appropriate to the selected profile. DAPPER is a custom archive, so provide the resolver URL explicitly; do not assume a processor knows its URL pattern. Classic BibTeX styles differ in which additional fields they display. Retain essential identifier/repository information in supported fallback fields such as `howpublished`/`note`, and test the selected exporters/styles.

Protect literal software-agent names from person-name parsing, escape TeX special characters in text fields, preserve meaningful capitalization, and treat identifier/URL fields with the chosen serializer's rules. ORCID and detailed roles are native metadata; do not insert ORCID strings into surnames. Optional custom export fields are supplementary and must not be required for basic citation resolution.

## 5. APA/MLA rendering through CSL

Use the authoritative citation record to produce **both BibTeX and CSL-JSON**, then render APA/MLA through a CSL processor. Avoid a BibTeX parse-and-convert round trip that could lose structured attribution. CSL separates item metadata, citation context, and formatting styles. See the [CSL primer](https://docs.citationstyles.org/en/stable/primer.html).

Initial rendering choice: represent a DAPPER registry landing page as CSL `type: "webpage"`, with `genre` identifying Claim, Question, or KnowledgeGap and `container-title: "REVEAL Mechanisms"`. This is an application mapping, not a claim that these are journal articles. Validate the exported fields against the pinned [CSL input schema](https://github.com/citation-style-language/schema/blob/master/schemas/input/csl-data.json).

Companion CSL-JSON for the same illustrative template:

```json
{
  "id": "dapper:Question.EXAMPLE_DIGEST",
  "type": "webpage",
  "title": "How does mechanism X contribute to disease Y?",
  "author": [
    {"family": "Researcher", "given": "Example"},
    {"literal": "Claude [model ID] via Claude Code (AI generator)"}
  ],
  "issued": {"date-parts": [[2026, 9, 24]]},
  "genre": "DAPPER Question",
  "container-title": "REVEAL Mechanisms",
  "publisher": "REVEAL Mechanisms",
  "archive": "DAPPER",
  "archive_location": "Question.EXAMPLE_DIGEST",
  "URL": "https://example.org/id/dapper:Question.EXAMPLE_DIGEST",
  "note": "First minted 2026-09-24. DAPPER ID: dapper:Question.EXAMPLE_DIGEST."
}
```

Use the equivalent record for Claim and KnowledgeGap. Keep ORCIDs and contribution roles in native metadata rather than adding unsupported fields to standard CSL name objects. Style behavior varies: a standard APA/MLA style may omit `note`, `genre`, or the archive identifier, so the canonical URL must always resolve the exact digest and display complete metadata. If a visible DAPPER identifier is required beyond a standard style, label the output as a REVEAL extension rather than claiming it is unmodified APA/MLA.

Pin processor version, CSL style content/version, locale, citation profile, and metadata revision. Format the **whole paragraph/document citation set together**, so same-author/same-year disambiguation, repeated citations, order, and numbering work correctly. A per-item “Copy citation” is a standalone bibliography entry; it cannot determine every in-text label for an unseen document. Test APA and MLA bibliographies and in-text citations with the actual processor; no hand-written formatter or claimed style-validation result is included in this design phase.

## 6. Resolver, UI, and paragraph contract

Proposed user interaction:

1. Every Claim/Question/KnowledgeGap inspector has **Cite**: APA, MLA, BibTeX, BibLaTeX, CSL-JSON, and permanent link.
2. Show the exact object type/ID, title, first-mint/issue date and date basis, ordered human/AI credits, roles/ORCIDs, generation provenance, and source/evidence links.
3. DAPPER `Paragraph.citations` stores ordered CitationOccurrence values: `target_id`, `citation_metadata_revision`, `start`, `end`, and optional `exact_text`. Offsets count Unicode code points with inclusive start/exclusive end. Use `assemble_cited_text` to derive them from authored segments; JavaScript uses `Array.from(text)` for equivalent slicing. Numbering/author-date markers are separate rendered artifacts, outside the saved Paragraph text.
4. Questions and gaps are allowed framing citations; Claims are assessment citations. Citation relations are distinct from scientific support edges. A cited Question/KnowledgeGap does not itself support an answer.
5. Citing a DAPPER Claim lets the reader inspect its CFDE/Proto-OKN evidence and original references. Preserve those references; citing the derived Claim does not assert it replaces source literature or independently verifies it.

Proposed API:

- `GET /v1/citations/{dapper_id}` — native citation metadata, selected revision, object and resolver links.
- `GET /v1/citations/{dapper_id}?format=bibtex|biblatex|csl-json|apa|mla&revision=...&locale=en-US` — one-item export; URL-encode opaque IDs.
- `POST /v1/citations/render` — saved `paragraph_id`, style and locale; the server loads its ordered occurrences and exact target/metadata revisions; returns in-text citations, bibliography, and rendering manifest. Rendering is a read: it uses one read-only snapshot and persists nothing. Each API process keeps a warm citeproc engine per style and resets its processor state before every render, so output matches a freshly built engine.
- A stable configured public URL such as `/id/{dapper_id}` — HTML landing page for that exact version. The deployment domain is still to be selected; `example.org` above is only documentation.

Minting and citation export do not require a new human approval gate. Minted objects remain inspectable under existing access controls; publication follows the application's actual visibility policy. A public-looking URL or a citation request must not silently publish a private gap or agent result. Corrections/withdrawals retain the original identifier and a durable notice where allowed, while enforcing applicable access controls.

## 7. Database and DAPPER coordination

Proposed additional records, not applied migrations:

- `citation_records`: exact target digest, metadata revision, title, date fields/basis, repository/resolver, publication state, optional registered DOI, source/identity/profile versions, and metadata checksum. Unique `(target_id, metadata_revision)`; updates append revisions.
- `citation_contributors`: ordered credit records with agent identity, type, supplied/verified ORCID status, per-object roles, and source provenance. Pin the display metadata used by each citation revision.
- `citation_events`: generation/mint/publication/import/correction/withdrawal observations and responsible agents. Reuse existing run/activity records by reference.
- `citation_renderings`: target/metadata revisions, ordered occurrences, style/processor/locale versions and hashes, and output artifact/checksum. This is a rebuildable cache; citation metadata is authoritative.

The [current DAPPER audit](dapper-integration.md) verifies the implemented external registry schema and inline Paragraph citations. Adopt those contracts directly; the earlier [citation audit](../data/dapper/citation-audit-2026-09-24.json) is historical. There is no pending requirement to invent a native bibliographic node or a citation-span extension.

**Acyclic identity is now the implemented design.** Mint the scientific object first, then register `(target_id, metadata_revision)` outside its hashable payload. Do not add the registry record through `has_recommended_citation`, which remains hashable. `Claim.asserted_in` remains a publication back-reference, not a citation registry. This separation is intentional, not a temporary workaround awaiting another schema.

Paragraph citations are hashable inline values with no independent digest. Editing an occurrence or its pinned metadata revision changes the Paragraph ID, while a registry correction alone leaves the existing Paragraph unchanged. Never silently use latest metadata if the requested revision is absent. The provenance linter checks occurrence shape, local target classes, duplicates and spans; `check_citation_metadata` validates each supplied record and `check_citation_registry_links` checks exact target/revision availability. REVEAL still verifies authorization, durable object/payload resolution, first-mint history, title/prose faithfulness, and any claimed DOI/ORCID provenance.

The agent supplies text and known citation targets. The trusted backend supplies authenticated identity snapshots, actual mint/publication observations and dates, and persisted revisions. `render_account` source references are not automatically valid citations because some refer to Propositions or Activities and lack metadata revisions. Build explicit citation segments and calculate spans only after text is final. Prior Claims may be cited from the saved authorized context without becoming account members.

## 8. Acceptance for implementation

- Each persisted Claim, Question, and KnowledgeGap resolves to its exact object and a reproducible citation record, including records without a DOI.
- A repeated import/generation of the same digest preserves the first issue/mint date and byline snapshot; new uses remain separate events.
- Two objects with identical titles but different digests never merge. Claim and Proposition are not confused, and a KnowledgeGap is not duplicated as an additional Question citation.
- Contributor order, software literal names, Unicode/TeX escaping, source authorship, and supplied versus verified ORCIDs survive export as specified.
- No registered DOI means no `doi`/`DOI` field. Source-paper DOIs are never attached as the derived Claim's DOI.
- Claim titles retain direction/scope; gap titles do not imply resolution. The full source text remains accessible.
- APA/MLA are tested with pinned processors/styles, including repeated citations, same-author/year collisions, unknown authors/dates, and combined Question/Gap/Claim citation sets.
- Metadata-only corrections preserve scientific digests and previous citation revisions; scientific/attribution changes follow DAPPER hashability rules. Citation metadata introduces no digest cycle.
- Paragraph references pin targets and metadata revisions; private records retain access control. Citation availability does not imply scientific support or public publication.
