# Construct a literature Scientific Account

You are building one DAPPER ScientificAccount that addresses the question in `account-request.json`, from papers you
read in this run. REVEAL checks the account, records what made it, and deposits it. Nobody can answer
questions while you work; decide and finish.

## What you have

- `account-request.json`: the question (`question_id` is its fixed DAPPER id), the requester's claims as fixed
  Propositions (`claims[].proposition_id`, with `subject_entity`, `relation` and `object_entity` when the requester
  gave them), and this run's limits.
- `mcp__reveal__search_papers` and `mcp__reveal__read_paper`: Europe PMC. A read returns the abstract unless you pass
  `source: "PMC"`, the paper's PMC id and `section: "full_text"` (open-access papers only). Page with `offset` /
  `next_offset`. Each result ends with a capture descriptor and a `run_budget`; watch the budget.
- `mcp__reveal__write_account_draft` and `mcp__reveal__lint_account`: save the account and check it. They do not use
  up your paper budget.
- `account/authoring-schema-excerpt.yaml`: the DAPPER classes and fields you may write.
- `mcp__reveal__query_claims`, when it is among your tools: what the claims repository already holds on your
  question. Look once before drafting. If a Proposition you would write already exists, copy its statement exactly:
  your Claim is then about the same Proposition. A repository record is an earlier claim, not evidence. Do not cite
  it, and do not treat its absence as a finding.

You have no shell and no other network. Only text returned by `read_paper` in this run can be cited.

Quotes are short on purpose. A snippet is 20 to 400 characters from one paragraph and must occur only once in the
paper; all your snippets from one paper text together may not exceed 4,000 characters, or 40% of a short text such
as an abstract. Quote the sentence that carries the finding and explain the rest in your own words.

## Work in this order

1. Read `account-request.json`. Plan a few focused searches.
2. Search, then read. Prefer primary studies. Read the full text when it is open access; an abstract supports only what
   the abstract says.
3. As soon as you have two or three usable passages, write a first draft and lint it. Repair what the linter reports,
   then extend. A run stopped at its time or turn limit returns nothing, so keep a linted draft saved at all times.
4. Finish with a short final message: what the account concludes, which requester claims you assessed and which you
   could not, and what you did not read.

## The document

Call `write_account_draft` with `filename: "account-1.json"` and a `document` of plural arrays. Use ids of your own
for new objects (for example `urn:draft:claim-1`); REVEAL assigns the real ids. Do not write `was_generated_by`,
`was_attributed_to`, people or activities: REVEAL supplies them.

```json
{
  "files": [ { "id": "<file.id from the capture descriptor>", "filename": "…", "mime_type": "application/json",
               "sha256": "…", "size_in_bytes": 0 } ],
  "propositions": [ { "id": "urn:draft:prop-1", "statement": "One checkable statement.",
                      "proposition_kind": "BIOLOGICAL_INTERPRETATION",
                      "subject_entity": "HGNC.SYMBOL:LEPR", "relation": "biolink:affects",
                      "object_entity": "<an identifier you were given or read>" } ],
  "evidence_items": [ { "id": "urn:draft:evidence-1", "target_proposition": "urn:draft:prop-1",
                        "direction": "SUPPORTS",
                        "snippet": "A verbatim quote from the text read_paper returned.",
                        "explanation": "In your words: what the passage shows and why it bears on the proposition.",
                        "context": "Source locator: <source_locator from the capture descriptor>. Species, tissue, design.",
                        "evidence_source": "Europe PMC open-access full text (PMC…)",
                        "reference": "PMID:…",
                        "was_derived_from": [ "<the same file.id>" ] } ],
  "claims": [ { "id": "urn:draft:claim-1", "proposition": "urn:draft:prop-1", "direction": "SUPPORTS",
                "status": "proposed", "statement": "What the evidence, taken together, supports.",
                "has_evidence": [ "urn:draft:evidence-1" ] } ],
  "scientific_accounts": [ { "id": "urn:draft:account-1", "name": "Short title", "question": "<question_id>",
                             "context": "Scope of the account and how the papers were found.",
                             "component_claims": [ "urn:draft:claim-1" ],
                             "closing_remarks": "What the account does and does not establish, and what was not read." } ]
}
```

Rules the linter and REVEAL enforce:

- **Files.** Copy the `file` object from the capture descriptor of the `read_paper` result you are quoting, unchanged.
  Search results and failed reads are not evidence.
- **Snippet.** A verbatim quote from one paragraph of the text that read returned, 20 to 400 characters, that occurs
  once in what you read of that paper. Quote the sentence that carries the finding. Only whitespace may differ. Put
  everything else in `explanation`, in your own words. For an abstract the text includes any markup shown.
- **One paper per EvidenceItem.** Use several EvidenceItems for several passages or papers.
- **Every Claim needs evidence**, and each EvidenceItem's `target_proposition` must be its Claim's `proposition`.
  `direction` is `SUPPORTS`, `DISPUTES`, `MIXED` or `NEUTRAL`. Report contradicting evidence as `DISPUTES`; an empty
  search is a coverage limit, not evidence of absence.
- **Give each Proposition its triple when you can.** `subject_entity`, `relation` and `object_entity` say the same
  thing as the statement in a form others can search and join: all three or none, each a CURIE or an absolute URI.
  A human gene is `HGNC.SYMBOL:<symbol>`. A relation is a biolink predicate, for example `biolink:associated_with`,
  `biolink:genetically_associated_with`, `biolink:affects`, `biolink:regulates`, `biolink:contributes_to` or
  `biolink:causes`. For a disease, trait, process, cell type or anything else, use an identifier you were given: in
  `account-request.json`, in the requester's prompt, in a claims-repository record, or printed in a paper you read
  (`KPN.TRAIT:…`, `MONDO:…`, `CL:…`, `obo:GO_…`). Do not recall an ontology number from memory: a wrong identifier is
  worse than none. If you cannot name both ends this way, leave the triple out and keep the Proposition as text.
  `lint_account` lists each Proposition without a triple as a `proposition-triple` advisory; it is a reminder, not
  an error.
- **The requester's claims.** To assess one, write a Claim whose `proposition` is that claim's `proposition_id` from
  `account-request.json`, with evidence from what you read. Do not reword it. When the requester's claim has no
  triple and you can state one by the rule above, write a Proposition with exactly the same `statement` plus the
  triple, and make your Claim and its evidence about that Proposition instead; REVEAL reports the requester's claim
  as assessed either way. If you found nothing that bears on a claim, leave it out and say so in your final message.
- **The question.** `question` is exactly `question_id`. Do not write a KnowledgeGap or Question object.
- **Do not invent** bibliographic details, numbers or passages. If the papers do not answer the question, say so in
  `closing_remarks` and keep the claims you can support.

When `lint_account` reports findings, edit the saved `output/account-1.json` in place for the fields it names and lint
again. A `literature-quote` finding tells you exactly what to change in a snippet.
