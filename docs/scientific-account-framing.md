# ScientificAccount framing and example-data audit

**Historical framing audit; consolidated September 25 in [v12](design-plan.md).** The current OpenAPI now reuses the exact CAD gap/account/Paragraph from the HTML example. The inquiry-generation history below describes the archived v9 fixture, not a current input contract.

September 25, 2026. This audit explains the question shown in the HTML account card and specifies the next coherent workflow fixture. It does not regenerate the reviewed OpenAPI or remint existing scientific objects.

**Current HTML update:** the replacement [CAD account packet](../design/data/cad-account/README.md) now uses the exact imported DisMech `cad_pgsxc_reverse_causation` KnowledgeGap. It includes twelve biological Claims and sixteen direct artifact EvidenceItems. CFDE observations are captured; KG/membership assertions are labeled illustrative. The synthesis scopes the CAD-in-T2D evidence to a candidate investigation of the broader CAD gap and does not claim to resolve causal direction. New objects have new DAPPER identities. The old API fixture described below is preserved but is no longer the primary HTML account. A job for a different selected gap shows the new packet only as an explicitly labeled reference example.

## Where the displayed question comes from

The text **“Which gene sets connect to Factor1 for CADinT2D in the captured CFDE graph?”** is authored directly in `make_fixtures()` in [the API generator](../scripts/build_openapi.py). It is neither an imported DisMech question nor the output of an executed research agent.

The generator:

1. Loads the captured CADinT2D factor-to-gene-set response.
2. Creates that narrow graph-retrieval Question and an illustrative inquiry-recording Activity/File.
3. Builds a source Claim about an actual captured edge and one account Claim interpreting that edge.
4. Sets `ScientificAccount.question` to the Question ID and assigns DAPPER IDs.
5. Exports the [archived v9 contract fixture](../api/history/v9/openapi.json), its paragraph, citation records and API exchanges.

The fixture inquiry is `dapper:Question.V63PQwfAIYJX1l2miYxzG2NVFolSa9Xt`; the account is `dapper:ScientificAccount.Mugr1fnwb9398BkbBMZ2v7vr38fBE9PL`. The agent workspace explicitly records `illustrative-model-not-executed`.

[The HTML builder](../design/build_prototype.py) bundles that existing document. The card uses the resolved inquiry text as its heading. Until this audit, the renderer also assumed `doc.questions[0]`, so a document framed directly by a KnowledgeGap would not have rendered correctly. It now resolves the exact `account.question` ID across both inquiry collections and fails on a missing reference.

This older fixture was useful for tracing a captured edge into a Claim, EvidenceItem and account. It does not demonstrate addressing the selected AIP knowledge gap. The explicit permission to display unrelated example results supported visual iteration; it did not make the old inquiry the intended product framing.

## The framing rule

Both the current sibling DAPPER schema and the pinned schema already support this:

- `KnowledgeGap` inherits `Question`.
- `ScientificAccount.question` references a Question **or KnowledgeGap**.
- `hypothesis` optionally references a Proposition under investigation.
- The DAPPER renderer resolves the question reference, then renders its `text` and, when present, `gap_description`.

Sources: [pinned claims schema](../data/dapper/2026-09-24-v8/snapshot/schema/claims.yaml), [DAPPER account documentation](../../dapper/schema/docs/claims.md), [DAPPER renderer](../../dapper/schema/scientific_claims.py).

For this application the invariant is:

```text
ScientificAccount.question == frozen_request.selected_gap.dapper_id
```

`frozen_request.selected_gap.dapper_id` is conceptual notation for the resolved, trusted source-gap identity, not a new wire field. All accounts from that request share the exact gap ID and payload observation. The backend supplies/verifies it; the agent cannot replace it with a search subquery, a paraphrase, or an unrelated inquiry. Do not mint a duplicate Question containing the gap's text.

For the existing AIP gap, the account's framing reference would be:

```json
{
  "question": "dapper:KnowledgeGap.RwCR-lUqGE0e7zgF463AFxKj7IdOGrAU"
}
```

This is a field illustration, not a newly minted ScientificAccount or a claim that the CAD account answers AIP.

## What the account should organize

| Part | Meaning in this workflow |
|---|---|
| Framing gap | The user's selected, versioned DisMech KnowledgeGap. The question and missing-knowledge description already belong to this object. |
| Optional hypothesis | A Proposition describing a possible explanation being investigated for that gap. Referencing it is not a support assessment. |
| Context | Selected mechanism anchors, evidence coverage, model, biological scope and analytical approach relevant to this account. |
| Component Claims | Assessments of scoped Propositions, retaining the distinction between graph associations and biological interpretations. |
| EvidenceItems | Explicit explanations of how source Claims support, dispute or contextualize the target Proposition. |
| Closing synthesis | How the scoped assessments work together to address, partially answer or propose an explanation for the original gap, including remaining uncertainty. Substantive new conclusions require Claims. |

The knowledge gap supplies the inquiry; it is not evidence supporting its own answer. A display label such as “Scientific account 1” does not require another Question object. The gap can remain visible once above several accounts, which differ in hypotheses, interpretations or findings. An agent's working search questions belong in its activity/evidence ledger and do not replace account framing.

The account can combine multiple Propositions through its Claims and closing synthesis without having one overarching Proposition. The active construction instructions are in [ScientificAccount construction](scientific-account-construction.md); the four source result templates are in [the PIGEAN/EAGGL claim model](pigean-claim-model.md).

## Design of the replacement example

Use one selected source gap consistently through the entire example. The existing AIP gap is a concrete candidate because its source prompt, rationale and mechanism attachments are already captured.

1. Reuse the exact imported KnowledgeGap object and retain its DisMech source mapping.
2. Supply a relevant frozen CFDE graph and record the selected factor identities and model. The current five-factor defaults are illustrative rankings; the CADinT2D capture is not an established AIP evidence package.
3. Record source Claims only for assertions present in the selected artifacts, with exact file/row/edge locators. DisMech source assertions and any external KG assertions retain their own sources and scopes.
4. Author scoped interpretation Claims and EvidenceItems explaining their relevance to the AIP gap. Do not turn cell-model observations into established human tumor causality or label an unrelated graph edge as support.
5. Frame every example account with the same AIP KnowledgeGap ID. A hypothesis is optional; it should reflect the actual proposed explanation.
6. Generate the Paragraph from that account and cite its gap and relevant durable Claims. The old paragraph and Question citation cannot be carried over unchanged.
7. Mint/verify the new objects with one explicit DAPPER schema/identity pin and validate the complete provenance document. Preserve the old fixture as a separate artifact; do not overwrite its content under old IDs.

If the immediate aim is purely visual, an internally consistent synthetic scenario is possible, with synthetic evidence labeled throughout and newly computed IDs. It must remain distinct from captured source evidence. Repointing the existing CAD account to whichever gap is clicked is not a coherent replacement scenario.

## Checks needed beyond schema validation

The current fixture validation checks DAPPER shapes, identity, scientific references, provenance profiles and citations. A valid generic Question-framed account can pass those checks. Add REVEAL scenario checks for:

- Exact account-to-selected-gap identity and trusted payload equality, including multiple accounts from one job.
- KnowledgeGap-only framing with no `questions` array; unrelated inquiry ordering must not affect rendering.
- Rejection of an invented Question, a paraphrased duplicate or a different gap, even if its DAPPER document is structurally valid.
- Relevant evidence and an explicit interpretation connecting the findings to the original gap; structural validity does not establish scientific relevance.
- Paragraph framing and citation targets matching the same saved gap/account/claim set.
- Re-minting affected objects after semantic edits; an account's `question` is hashable and cannot be changed under the same account digest.

The current API is 0.2.0-draft and uses the gap-centered CAD fixture. The earlier mismatch is retained here as history; the current validator asserts request/account/paragraph framing consistency.
