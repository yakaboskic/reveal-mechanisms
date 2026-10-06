# Scientific object citations

This experimental contract implements the model fields in the REVEAL citation
standard dated September 24, 2026. It covers citation metadata and paragraph
occurrences. Registry persistence, durable resolvers, access control, DOI
registration, and BibTeX/CSL/APA/MLA exporters belong to the application and are
not implemented here. The example is fictional and has not been issued.

## Two records with different responsibilities

Mint the Claim, Question, or KnowledgeGap first. Its exact, case-sensitive
`dapper:<Class>.<digest>` identifies the scientific object. A KnowledgeGap keeps
its most-specific class; do not duplicate it as a second Question. A Claim
citation identifies the attributed assessment, not its Proposition.

Store a separate **citation metadata record** keyed by `(target_id,
metadata_revision)`. The [JSON Schema](../citations/citation-record.schema.json)
defines this application record. It is not a DAPPER node and does not participate
in scientific identity. Metadata revisions append snapshots; they do not rewrite
earlier revisions or change scientific digests. A hashable scientific edit still
follows the normal DAPPER identity rules.

Do not put the registry record in `has_recommended_citation`: that field is
hashable, and linking a target back to metadata containing its digest would
introduce an identity cycle. `Claim.asserted_in` remains a publication reference.

## Citation metadata fields

The machine-readable contract specifies required fields and nullable values.
Unknown information stays absent or null; an empty `byline` means no known
credited contributors. Do not fill missing dates or names with invented values.

- **Exact object:** `target_id`, `target_class`, `dapper_schema_version`,
  `dapper_identity_profile`, and `object_payload_ref`. Pin the schema by a release,
  commit, or checksum. The identity profile is `DAPPER-ID-1`.
- **Metadata snapshot:** positive `metadata_revision`, `citation_profile_version`
  (`reveal-citation-v1`), `title`, `title_derivation`, nullable `language`, and
  optional `metadata_checksum`. Title derivation records its method, version when
  known, and source. Titles must retain assessment direction and scope.
- **Ordered credit:** `byline` is an ordered array. Each entry has an `agent_id`,
  `kind` (person, software, organization), one or more per-object `roles`, and
  source provenance. Optional names, ORCID, provider, agent/model/harness versions,
  and generation activity retain the supplied snapshot. Roles are `author`,
  `ai_generator`, `agent_operator`, `publisher`, and `curator`; operator/publisher
  credit does not imply source authorship or scientific endorsement. Array order
  is authoritative and must not be inferred from `was_attributed_to`.
- **Identity provenance:** a person's optional `orcid` includes its URL, status
  (`supplied` or `verified`), and source observation. Software uses a literal
  `display_name`, never person-name fields or an ORCID. Verification status is
  supplied by the trusted identity system; the schema does not authenticate it.
  Email is not a citation field.
- **Dates:** `generated_at`, `first_minted_at`, `published_at`, and, for imported
  objects, `original_issued_date` and `imported_at`. Timestamps use UTC with a `Z`
  suffix. Every known event date has a `date_provenance` entry identifying its
  source observation. Nullable `issued_date` and `issued_basis` select the citation
  date: `first_minted_at`, `original_issued_date`, or `unknown`. Imported objects
  with a known original issue date retain it. Reimports must not reset first mint
  or original issue dates. Export time is never used as an issue date.
- **Repository and access:** `publisher`, `repository`, `canonical_url`,
  `access_level` (private, restricted, public), and `publication_state`
  (unpublished, published, withdrawn). A configured resolver URL is not evidence
  that an object has been made public. These states are distinct from a Claim's
  scientific assessment or review status.
- **Optional provenance and notices:** `generation_activity`, `responsible_agents`,
  `source_revisions`, `prior_objects` with explicit relations, and correction or
  withdrawal `notices`. Contributor roles distinguish source authors from the
  agents importing or publishing their work.
- **Optional identifiers:** `doi` only for a registered DOI for the exact cited
  object/version; `accession` for a separately allocated readable alias. A digest
  is not a DOI or a date. A source paper's DOI belongs to the source paper.

The [example record](../citations/example-record.json) targets the fictional
KnowledgeGap in the scientific-account example. Its dates and byline are unknown;
its `example.org` URLs do not assert a deployed resolver. Its schema pin is the
`0.2.0-a1` release tag, which includes both root schema modules and the citation
metadata contract.

## Paragraph occurrences

`Paragraph.citations` is an optional ordered list of inline `CitationOccurrence`
values. Each requires:

- `target_id`: exact Claim, Question, or KnowledgeGap identifier.
- `citation_metadata_revision`: positive integer pinning the registry snapshot.
- `start` and `end`: zero-based Unicode **code-point** offsets in the saved
  `Paragraph.text`, with an inclusive start and exclusive end.

Optional `exact_text` must match that span exactly. Require `0 <= start < end <=
len(text)`. Count code points, not bytes, grapheme clusters, or JavaScript UTF-16
code units; JavaScript can use `Array.from(text)` before slicing. Do not normalize
or edit text after calculating anchors without recalculating them.

Different targets can share a span, and one target can be cited multiple times.
Duplicate occurrences with the same target, revision, and span are rejected.
Question/gap citations provide framing. Claim citations point to assessments.
Neither creates an `EvidenceItem` or asserts scientific support. Paragraph
citations may refer to relevant prior claims as well as account members; they do
not silently add those claims to the account.

Citation values have no independent digest. They are hashable content of their
Paragraph, so changing an occurrence or pinned revision changes that Paragraph's
identifier while leaving its account and scientific targets unchanged. Metadata
corrections alone leave an existing paragraph pinned to its original revision.

Numbering, author-date labels, styles, and the bibliography are computed for the
whole citation set by the application. Never silently replace a missing pinned
metadata revision with the latest one.

## Validation and agent outputs

Use the provenance linter for a DAPPER graph document. It checks citation shapes,
local target classes, duplicate occurrences, and text spans, alongside scientific
content and identity. A graph submitted for local validation must include the
cited objects, just as it includes other referenced scientific content.

```sh
uv run schema/lint/lint_provenance.py schema/examples/example_scientific_account.yaml
uv run schema/citation_metadata.py schema/citations/example-record.json
```

`check_citation_metadata` adds date consistency and provenance checks to the
registry JSON Schema. `check_citation_registry_links` checks that every pinned
occurrence has a matching supplied registry revision. They do not fetch payloads,
prove DOI registration, validate the scientific faithfulness of a title, enforce
database history, or authorize access. The registry must enforce those properties.

An agent can return ScientificAccounts and Paragraphs through an application
output schema. Generate references to existing objects and revisions explicitly;
the runtime supplies trustworthy mint dates, authenticated identity snapshots,
and publication events. Do not ask the model to invent registry metadata.
`assemble_cited_text` in `schema/scientific_claims.py` accepts authored text
segments with target/revision pairs and calculates spans. `render_account` source
references are not automatically citations: they can include Propositions or
Activities and do not supply registry revisions.

## Gap context

`Question.about_entities` is an optional list of URI/CURIE entity references,
inherited by `KnowledgeGap`. It supports disease, gene, mechanism, or phenotype
context without treating those entities as answers. Prose scope remains in
`Question.scope`. Document-local prefixes follow the gene-set authoring guide.

`KnowledgeGap.gap_kind` optionally distinguishes `KNOWLEDGE_GAP` from
`HUMAN_MODEL_MISMATCH`. Omission means unclassified. These fields are scientific
content and participate in identity. Source ingestion dates, source lifecycle,
and whether an application considers a gap addressed remain separate metadata.
