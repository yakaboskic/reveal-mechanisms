# CAD-in-T2D ScientificAccount design packet

Open [the account design](http://127.0.0.1:8767/#example), or inspect [scientific-account.yaml](scientific-account.yaml).

This is an authored HTML design fixture. The CFDE observations and DisMech gap come from frozen source records; the biological assessments are proposed example interpretations. No live agent, authentication, database write, publication or external KG query runs here.

## Contents

- One ScientificAccount framed by the exact imported DisMech CAD gap about exposure-driven genetic-risk amplification versus downstream readouts.
- Three genes: SHH, GLI3 and TCF7L2.
- One mechanism: `factor:portal:CADinT2D:cfde-inc-v2:Factor1`.
- Two catalog GeneSets: the retained AMP AD / GTEx composite annotation and PsychENCODE geneM16.
- Twelve biological Propositions and twelve Claims: three gene–mechanism, two gene-set–mechanism, three gene–trait, two gene-set–trait, and two illustrative membership relationships.
- Sixteen EvidenceItems linked directly to retained Files. There are no redundant source-result Claims.
- A synthesis that explains what the relationships contribute and why they do not resolve the gap's causal direction.

## Captured versus illustrative

The two PIGEAN JSON responses retain their original bytes. Each numerical EvidenceItem points to its source row and includes a verbatim excerpt. Model scores retain their own meanings; they are not confidence probabilities.

`sources/illustrative-kg-assertions.json` contains **invented** ProKN/BiomarkerKG-labeled assertions for reviewing KG and membership interactions. They are not returned records from either graph. Every use is labeled in the YAML and UI. Two membership Claims use them as illustrative support; uses against gene–mechanism Propositions are neutral context. Shared loadings do not establish membership. The real imported GeneSet payloads have not acquired invented members.

## Packet files

- `scientific-account.yaml` / `.json`: equivalent DAPPER documents with computed identities.
- `presentation.json`: application-only grouping, source-origin labels, metric display and artifact access, keyed by DAPPER ID. These are not new DAPPER fields.
- `paragraph.json` / `citation-registry.json`: a coherent, separately authored paragraph preview and its pinned citation metadata.
- `sources/`: original CFDE responses, the captured DisMech discussion, and the explicitly illustrative KG source.
- `validation.json`: identity, scientific reference, schema/profile and evidence checks. Validation checks representation, not biological truth. The catalog encoding Activity retains its preexisting missing-input warning.

## Rebuild

From the repository root, with PyYAML and the project's DAPPER dependencies:

```bash
python3 design/build_prototype.py
python3 -m http.server 8767 --bind 127.0.0.1 --directory design
```

The builder regenerates this packet with `design/build_account_example.py`, then embeds it in the standalone HTML. The current OpenAPI fixtures reuse this scientific document and its citation packet. The v9 API is archived for history.

## Review questions

Does the synthesis explain how the propositions address the gap? Can a reader distinguish claim assessment from observed scores? Is illustrative membership unmistakable? Can they inspect each EvidenceItem's exact source? Is the boundary between catalog provenance and original construction provenance clear?

## Cited research statement

The account view switches between the existing closing remarks and a DAPPER Paragraph. `paragraph-object.json` is the standalone Paragraph; `paragraph.json` is its full scientific document; `paragraph-packet.json` bundles the Paragraph and 13 exact citation metadata records for rendering. There are 19 occurrences, with shared spans and repeated citations. The registry is also available separately in `citation-registry.json`.

`research-statement.md` and `references.bib` are generated from these same objects. No DOI is invented. The account and claim identities stay unchanged; changing the paragraph text and citations remints only the affected Paragraph. Its explanatory KG membership statements remain illustrative. The HTML's automatic paragraph preparation is local timed playback, not a generation request.
