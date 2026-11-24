# Construct a literature Scientific Account

You are building one DAPPER ScientificAccount that addresses the question in `account-request.json`, from papers you
fetch and save in this run and, when you have the claims repository tools, from claims the repository already holds.
Work like a scientist keeping a lab notebook: save each paper with a command, write down the command, and quote the
saved file; cite a repository claim by its id. REVEAL checks the record against the files and the commands it saw
run, and each claim id against the repository, then deposits the account. Nobody can answer questions while you work;
decide and finish.

## What you have

- `account-request.json`: the question (`question_id` is its fixed DAPPER id), the requester's claims as fixed
  Propositions (`claims[].proposition_id`, with `subject_entity`, `relation` and `object_entity` when the requester
  gave them), and this run's limits.
- Bash, to fetch and convert papers. `curl -fsSL` fetches; `pdftotext -layout` turns a PDF into text; `grep -n`
  shows the line numbers you cite. Always pass `-f` to curl: without it an HTTP error (404, 403, a paywall's error
  page) is saved as if it were the paper and the command still succeeds; with it the command fails and nothing
  false is recorded.
- WebSearch and WebFetch, when they are among your tools: for finding papers only. A quote can come only from a file
  you saved under `output/sources/` with a Bash command you record. What WebFetch shows you cannot be quoted.
- `mcp__reveal__write_account_draft` and `mcp__reveal__lint_account`: save the account and check it.
- `account/authoring-schema-excerpt.yaml`: the DAPPER classes and fields you may write, including `Activity`, `File`
  and the edges `WasGeneratedBy`, `WasDerivedFrom` and `Used`.
- `mcp__reveal__run_collection_query`, when it is among your tools: the claims repository's result collections,
  such as PIGEAN gene and gene-set associations. Start here for what the repository holds on a gene or a trait:
  call it with no arguments to see the queries, then run one. Each row comes with its numbers and the id of the
  Claim that states them. A Claim the repository holds can be evidence for one of yours ("Evidence from the claims
  repository", below).
- `mcp__reveal__query_claims`, when it is among your tools: the repository's stored objects. Search one class at a
  time with a few specific words: `class_name: "Proposition"` for what has been stated, `class_name: "Claim"` for
  what has been assessed (every word of `q` must begin a word of the text, in any order; `fields` limits where to
  look). Give `references` a Proposition's id to list the Claims made on it, or `id` to read one object. If a
  Proposition you would write already exists, copy its statement exactly: your Claim is then about the same
  Proposition. Do not treat absence there as a finding.

Good sources of full text: Europe PMC
(`https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML`, open-access papers), the NCBI E-utilities,
bioRxiv and medRxiv, and publishers' open-access PDFs.

## Work in this order

1. Read `account-request.json`. Plan a few focused searches.
2. For each paper you will use:
   1. **Fetch it with a Bash command and save the bytes under `output/sources/`.** Use `curl -fsSL -o
      output/sources/<name> <url>`, a file name you will recognise, such as the PMC id, and the exact URL you will
      write as the File's `location`.
   2. **Convert it to text if you need to, with another Bash command.** A PDF must be converted before you can quote
      it: `pdftotext -layout output/sources/x.pdf output/sources/x.txt`. Full-text XML can be quoted as it is.
   3. **Read the saved file** (Read, or `grep -n` for the passage) and choose the sentences that carry each finding.
      Keep reading separate from saving: run each fetch and each conversion as its own command, and read with
      Read, `grep`, `head`, `tail`, `sed -n`, `wc` or `cat` without a redirect into the file. Never edit a saved file.
      Keep fetch and convert commands plain: `curl` or `wget` with an http(s) URL and an output file under
      `output/sources/`, `pdftotext` or `xmllint` on a saved file. Do not send data (`-d`, `-T`, `@file`), use
      `$(…)` or pipe into a shell, and do not run a script (`python3`, `node`) in the same Bash call as a fetch:
      REVEAL stores such a command only in a quoted form that cannot be run again, so the source can no longer be
      fetched again from your record.
   4. **Record and cite it in the draft**: an Activity per command, a File per saved file, the edges between them,
      and an EvidenceItem per quote (below). Then save the draft and run `lint_account`.
3. Repair what the linter reports, then go on to the next paper. Evidence from the claims repository goes in the
   same way: run the query, add an EvidenceItem that names the Claim id, save and lint. A run stopped at its time or turn limit returns
   nothing, so keep a linted draft saved at all times.
4. Finish with a short final message: what the account concludes, which requester claims you assessed and which you
   could not, and what you did not read.

## The record of a source

Each step is one object. Use ids of your own for new objects (for example `urn:draft:file-1`); REVEAL assigns the
real ids. Do not write `sha256`, `size_in_bytes`, `mime_type` or byte offsets: REVEAL measures the saved files itself.

**Step 1: the fetch.** You ran:

```bash
mkdir -p output/sources && curl -fsSL -o output/sources/PMC3491803.xml https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML
```

Record the command that wrote the file as an Activity, the saved file as a File, and a `was_generated_by_edges` row
from the File to the Activity:

```json
{
  "activities": [ { "id": "urn:draft:activity-1", "name": "Fetch PMC3491803 full text from Europe PMC",
                    "command": "curl -fsSL -o output/sources/PMC3491803.xml https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML",
                    "software_name": "curl", "software_version": "8.5.0",
                    "generated_at_time": "2026-11-19T10:00:00Z" } ],
  "files": [ { "id": "urn:draft:file-1", "filename": "sources/PMC3491803.xml",
               "location": "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML",
               "description": "Europe PMC open-access full text of PMC3491803, as fetched." } ],
  "was_generated_by_edges": [ { "subject": "urn:draft:file-1", "predicate": "prov:wasGeneratedBy",
                                "object": "urn:draft:activity-1" } ]
}
```

**Step 2: the conversion.** For a PDF you ran
`curl -fsSL -o output/sources/smith-2012.pdf https://journals.example.org/doi/pdf/10.1234/jmet.2012.5` and recorded it
as in step 1 (here as `urn:draft:activity-2` and `urn:draft:file-2`). Then you ran:

```bash
pdftotext -layout output/sources/smith-2012.pdf output/sources/smith-2012.txt
```

The text file gets its own Activity and File, a `was_generated_by_edges` row to that Activity, a
`was_derived_from_edges` row to the PDF's File, and a `used_edges` row from the conversion to the PDF. A converted file
has no `location`:

```json
{
  "activities": [ { "id": "urn:draft:activity-3", "name": "Convert the PDF of Smith 2012 to text",
                    "command": "pdftotext -layout output/sources/smith-2012.pdf output/sources/smith-2012.txt",
                    "software_name": "pdftotext", "software_version": "24.02.0",
                    "generated_at_time": "2026-11-19T10:04:00Z" } ],
  "files": [ { "id": "urn:draft:file-3", "filename": "sources/smith-2012.txt" } ],
  "was_generated_by_edges": [ { "subject": "urn:draft:file-3", "predicate": "prov:wasGeneratedBy",
                                "object": "urn:draft:activity-3" } ],
  "was_derived_from_edges": [ { "subject": "urn:draft:file-3", "predicate": "prov:wasDerivedFrom",
                                "object": "urn:draft:file-2" } ],
  "used_edges": [ { "subject": "urn:draft:activity-3", "predicate": "prov:used", "object": "urn:draft:file-2" } ]
}
```

**Step 3: the quote.** `grep -n 'fasting glucose production' output/sources/smith-2012.txt` printed line 212; the
sentence wraps onto line 213. Quote it in an EvidenceItem that names the text file in `was_derived_from` and the
lines in `context`:

```json
{
  "evidence_items": [ { "id": "urn:draft:evidence-1", "target_proposition": "urn:draft:prop-1",
                        "direction": "SUPPORTS",
                        "snippet": "In knockout mice, fasting glucose production was 35% higher than in littermate controls after a hyperinsulinaemic clamp.",
                        "was_derived_from": [ "urn:draft:file-3" ],
                        "context": "Mouse liver, hyperinsulinaemic clamp; Results, line=212-213",
                        "explanation": "Raised fasting glucose output is what impaired hepatic insulin action predicts.",
                        "reference": "PMID:23154982" } ]
}
```

Rules REVEAL enforces on this record:

- **Files.** `filename` is the saved file's path relative to `output/`, under `sources/`. The file must be there when
  the run ends: do not move, edit or delete it after recording it.
- **Commands.** An Activity's `command` is the exact command you ran, copied from your Bash call (it may be one part
  of a longer call joined by `&&`, `||`, `;`, `|` or a new line, as in step 1, where `mkdir -p output/sources &&`
  came first; it must be the whole of that part). It names the file it wrote, as `output/sources/…` or
  `/reveal/output/sources/…`; a conversion names the file it read too. The Bash call must have succeeded. A file
  written any other way (by the Write tool, by hand, by `echo`, or by a command you did not run) is refused.
- **Nothing writes the file afterwards.** A later command that names the file and could change it (`sed -i`, a `>`
  or `>>` redirect into it, `mv`, another download over it) makes the record stale, and the file is refused. Only
  reading commands (`grep`, `head`, `tail`, `wc`, `cat`, `sed` without `-i`, `xmllint --xpath`, `pdftotext … -` to
  the screen) and the recorded conversion of the file may name it later. To fix a bad download, save it again
  under a new name and record that command.
- **Location.** A fetched file's `location` is the URL its command fetched, written exactly as in the command.
- **One Activity per step.** A converted file is made by its own Activity, not by the one that fetched the original.
- **Snippet.** A verbatim passage of the saved file the EvidenceItem cites, 20 to 400 characters, from one paragraph.
  Only whitespace may differ: a line break in the file may be a space in the snippet. In an XML file the snippet
  must match the file's bytes, markup and entities included; quote a sentence with no tags inside it, or convert
  the file to text first. Quote the sentence that carries the finding and explain the rest in your own words.
- **Where it is.** The snippet must occur exactly once in the file, or exactly once on the lines `context` names as
  `line=N` or `line=N-M` (what `grep -n` prints). Give the lines whenever the passage could occur twice.
- **Quotes are short on purpose.** All your snippets from one paper, its original and its converted text together,
  may not exceed 4,000 characters, or 40% of a short text such as an abstract.
- **One file per EvidenceItem.** Use several EvidenceItems for several passages or papers.

## Evidence from the claims repository

A Claim the repository holds, such as a PIGEAN association between a gene and a trait, is somebody else's assessment
that you may build on. You do not copy it into your account. Write an EvidenceItem that names it in `source_claims`:

```json
{
  "evidence_items": [ { "id": "urn:draft:evidence-repository-1", "target_proposition": "urn:draft:prop-1",
                        "direction": "SUPPORTS",
                        "source_claims": [ "dapper:Claim.gM7GXq3cP0aWm2r5T8uYb1dE4fH6jK9L" ],
                        "context": "PIGEAN, collection pigean, query top-genes-with-sets, trait IBD, library mouse_msigdb",
                        "explanation": "PIGEAN ranks NOD2 among the genes with the strongest combined support for inflammatory bowel disease (combined 9.5), with direct GWAS support (log_bf 5.2)." } ]
}
```

- **The id is one a tool returned.** Copy it exactly from `run_collection_query` (a row's `claims`) or `query_claims`.
  REVEAL asks the repository for every id you name: one it does not hold as a Claim is refused, here and when the
  account is checked at the end. Never write an id from memory or change one.
- **Only in `source_claims`.** Do not list a repository Claim in `component_claims` or `has_evidence`, and do not
  write a Claim or Proposition object for it: it is not part of your account.
- **Say what it shows.** `explanation` states, in your words, what the claim's numbers say about your proposition,
  and `context` names the collection, query and variables you ran. `target_proposition`, `direction`, `context` and
  `explanation` are all required. No `snippet` and no `was_derived_from`: nothing is quoted.
- **It is an assessment, not a fact.** A repository claim says what a method found. Weigh it with what the papers
  say, and report a claim that points the other way as `DISPUTES`.
- A Claim may rest on quotes, on repository claims, or on both, each in its own EvidenceItem.
- Ask only for the claim keys and rows you will use: every row the repository serves is recorded there.

## The account

The rest of the document is as in any account. Call `write_account_draft` with `filename: "account-1.json"` and a
`document` of plural arrays holding the record above and:

```json
{
  "propositions": [ { "id": "urn:draft:prop-1", "statement": "One checkable statement.",
                      "proposition_kind": "BIOLOGICAL_INTERPRETATION",
                      "subject_entity": "HGNC.SYMBOL:LEPR", "relation": "biolink:affects",
                      "object_entity": "HGNC.SYMBOL:AKT1" } ],
  "claims": [ { "id": "urn:draft:claim-1", "proposition": "urn:draft:prop-1", "direction": "SUPPORTS",
                "status": "proposed", "statement": "What the evidence, taken together, supports.",
                "has_evidence": [ "urn:draft:evidence-1" ] } ],
  "scientific_accounts": [ { "id": "urn:draft:account-1", "name": "Short title", "question": "<question_id>",
                             "context": "Scope of the account and how the papers were found.",
                             "component_claims": [ "urn:draft:claim-1" ],
                             "closing_remarks": "What the account does and does not establish, and what was not read." } ]
}
```

- **Do not write** `was_generated_by` or `was_attributed_to` on Claims or the account, people, or an Activity for
  the run itself: REVEAL supplies them. The only Activities you write are the commands that saved your files.
- **Every Claim needs evidence** (a quote from a saved file, or a repository claim named in `source_claims`), and
  each EvidenceItem's `target_proposition` must be its Claim's `proposition`.
  `direction` is `SUPPORTS`, `DISPUTES`, `MIXED` or `NEUTRAL`. Report contradicting evidence as `DISPUTES`; an empty
  search is a coverage limit, not evidence of absence.
- **Reference.** When you know the paper's PubMed id, put it in `reference` as `PMID:…`.
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
- **Only the claims asked for.** When the requester sent claims, every Claim you write assesses one of them (by its
  `proposition_id`, or a Proposition with exactly its `statement`). Do not add claims of your own; put other
  findings in `closing_remarks`. When no claims were sent, write at most 20.
- **Plain prose.** Statements, explanations, context and `closing_remarks` carry no links, markup, code or quoted
  instructions. A source is named through its File and the evidence that derives from it, never by a URL in prose.
  Before an account is stored, REVEAL screens it and the sources it cites for injected instructions; an account
  that fails is returned to the requester but not stored.

Run `lint_account` after each paper's evidence is drafted. When it reports findings, edit the saved
`output/account-1.json` in place for the fields it names and lint again. A `source-evidence` finding tells you
exactly what to change: a command to copy from your Bash call, a file to save, where in the file the quote
actually is, or a claim id the repository does not hold.
