# Evidence claim structure

Recommended structure for ScientificAccount Claims built from PIGEAN/EAGGL and CFDE evidence. It uses DAPPER `0.2.0` as released: no schema change, no new authored field. It is guidance, not a gate. Lint reports how closely an account follows it as advisory `claim_structure` suggestions (see [Scientific-account linting](scientific-account-linting.md)); validity, errors and warnings never depend on it. Knowledge-graph, literature and other independently evidenced Claims remain welcome alongside this structure.

An account has two layers:

- **Atomic Claims** restate one retrieved observation each. Each is a `RESULT` Proposition with a single subject–relation–object triple, plus a Claim with the exact `ClaimScore` from the cited row and an EvidenceItem pointing at that row.
- **Synthesis Claims** interpret the atomic Claims. Each is a `BIOLOGICAL_INTERPRETATION` Claim whose EvidenceItem cites atomic Claims through `source_claims`. At least one synthesis connects the evidence to the selected KnowledgeGap.

The complete synthetic example in `input/package-sections/authoring-examples.json` (and the authored-only skeleton in `input/package-sections/authoring-skeleton.json`) shows one atomic Claim per family and one gap-relevance synthesis. Both pass lint with zero structure suggestions.

## 1. Entities

| Kind | Identifier | Source |
|---|---|---|
| Factor | the trusted `dapper:Mechanism.*` | `get_factor` (an EAGGL factor **is** the mechanism) |
| Gene | `HGNC.SYMBOL:<symbol>`, as captured | `get_factor_loadings`, `get_gene_factors`, `get_gene_set_members`, `get_pigean_gene_phenotype` |
| Gene set | the trusted `dapper:GeneSet.*` | `get_gene_set` |
| Trait | `KPN.TRAIT:NNNNNNN` | the factor's fit or `kpn_traits` |
| Knowledge gap | the selected `dapper:KnowledgeGap.*` | the seed selection |

Keep the captured symbol spelling. It matches `GeneSet.members` exactly, so membership triples and synthesis paths line up. Symbols are compared case-insensitively when paths are checked (`C10orf71` and `C10ORF71` are one gene); identity is never inferred from case alone. A verified `HGNC:<id>` from `resolve_gene` may be used where it is the captured identity.

## 2. Predicates

The ontology terms below were checked in the EBI Ontology Lookup Service (OLS); `candidate-gap-connection` is a REVEAL relation.

| Term | IRI | Label | Use |
|---|---|---|---|
| `prov:hadMember` | `http://www.w3.org/ns/prov#hadMember` | hadMember | gene set → member gene; the DAPPER-native `slot_uri` of `GeneSet.members` |
| `obo:RO_0002610` | `http://purl.obolibrary.org/obo/RO_0002610` | correlated with | statistical dependence: factor loadings, set projections, set–trait effects |
| `biolink:genetically_associated_with` | `https://w3id.org/biolink/vocab/genetically_associated_with` | genetically associated with | gene → trait (PIGEAN gene–phenotype) |
| `obo:RO_0002331` | `http://purl.obolibrary.org/obo/RO_0002331` | involved in | synthesis: gene or set involved in a mechanism |
| `urn:reveal:relation:candidate-gap-connection` | (REVEAL relation) | candidate gap connection | synthesis: gene or set → selected KnowledgeGap |

Related terms, verified but not recommended for these families:

- RO:0002350 *member of* and RO:0002351 *has member* are the RO membership pair. `prov:hadMember` is used instead because DAPPER already declares it for `GeneSet.members`, so an atomic membership triple restates the stored membership exactly.
- RO:0000056 *participates in* relates a continuant to a process. An EAGGL factor is a fitted mechanism, not a process instance, so involvement uses RO:0002331.
- `biolink:gene_associated_with_condition` relates a gene to a disease condition. PIGEAN traits include quantitative measurements in a fitted model, so the broader `genetically_associated_with` is used.

**Prefixes.** DAPPER declares `obo:`, `prov:`, `HGNC:` and `KPN.TRAIT:`. New seeds also declare `HGNC.SYMBOL: https://identifiers.org/hgnc.symbol:` and `biolink: https://w3id.org/biolink/vocab/` in the trusted package `prefixes`. `write_account_draft` and trusted acceptance add any of these that a draft uses to its `prefixes`, so the CURIEs resolve in draft lint, final lint and the public projection. An older seed without them can use the full IRIs, which are equally conformant.

**Legacy synonyms.** Earlier `urn:reveal:relation:*` relations (`member-of`, `has-loading-on`, `has-joint-loading-on`, `observed-projection`, `involved-in` and others), `RO:` CURIEs and RO:0002350/0002351 are accepted as synonyms of the families above. Lint suggests the recommended term; it never rejects a synonym.

## 3. Atomic families

Each family is recognized from its endpoint kinds and predicate, falling back to its ClaimScore metric. Several families share RO:0002610; the endpoints tell them apart.

| Family | Triple (subject → predicate → object) | Statement template | ClaimScore(s) | Retrieval |
|---|---|---|---|---|
| Gene–gene set | GeneSet → `prov:hadMember` → gene | "Gene set S has member G." | none | `get_gene_gene_sets`, then `get_gene_set`, `get_gene_set_members` |
| Factor–gene | gene → `obo:RO_0002610` → Mechanism | "Gene G has loading L on factor F (label)." | `LOADING` `loading` | `get_factor_loadings` (kind gene), `get_gene_factors` |
| Factor–gene set | GeneSet → `obo:RO_0002610` → Mechanism | "Gene set S has joint loading L on factor F." | `LOADING` `joint_loading` or `marginal_loading` | `get_factor_loadings` (kind gene_set), `get_gene_set_factors`, `get_gene_gene_sets` with `factor_id` |
| Phenotype–gene | gene → `biolink:genetically_associated_with` → KPN.TRAIT | "Gene G is associated with P in PIGEAN (combined C)." | `SCORE` `combined` (and `log_bf`, `prior`) | `get_pigean_gene_phenotype` |
| Gene set–trait | GeneSet → `obo:RO_0002610` → KPN.TRAIT | "Gene set S is associated with P (beta_uncorrected B)." | `EFFECT_ESTIMATE` `beta_uncorrected` (and `beta`) | `get_pigean_gene_set_phenotype` |

Rules for every atomic Claim:

1. `proposition_kind: RESULT`, with `subject_entity`, `relation` and `object_entity` all set, in the orientation above.
2. One observation per Claim: one gene or set, one factor or trait, one family. Several metrics of the same row (for example `combined` and `log_bf`) may share one Claim.
3. `has_score` references a ClaimScore whose `metric` is the exact captured column, whose `value` is the exact captured value, and whose `score_kind` is the kind in the table. Lint checks metric, value and kind against the cited row. Membership has no score.
4. The statement states the value exactly, or rounded to at least three significant digits.
5. The EvidenceItem's `was_derived_from` lists the captured File; set families also list the trusted GeneSet. The exact row locator (for example `` `/result/items/3` ``) goes in `context`, a verbatim excerpt of that row in `snippet`, and the bearing on this Proposition in `explanation`.
6. Direction is normally `SUPPORTS`: the row is the observation. Missing data is a coverage limitation, never a contradiction.

## 4. Synthesis Claims

Synthesis Claims are `BIOLOGICAL_INTERPRETATION` Claims whose EvidenceItems cite atomic Claims with `source_claims` (no `was_derived_from` is needed). The cited atomic Claims should come from at least two families and form one connected path: the same gene, gene set, factor and trait across them. The synthesis subject must lie on that path.

Number new draft IDs at one width once a kind reaches ten (for example `claim-01` … `claim-30`). Trusted minting reads an ID embedded in a longer ID as a reference, so a synthesis `claim-1` citing `claim-10` cannot be minted; lint reports every such ID as a `draft-id-collision` error.

| Type | Proposition | Cites |
|---|---|---|
| **Gap relevance** (primary, at least one) | text-only, or S (or G) → `urn:reveal:relation:candidate-gap-connection` → the selected KnowledgeGap. "Gene G and gene set S provide a candidate connection between factor F, phenotype P and knowledge gap D." | factor–gene + gene–gene set + (phenotype–gene or gene set–trait), and factor–gene set where available |
| Convergent involvement | S (or G) → `obo:RO_0002331` → Mechanism. "S is involved in F relevant to P." | factor–gene set + gene set–trait, or factor–gene + phenotype–gene |
| Factor–phenotype bridge | text-only, or a triple to the other trait. For a phenotype other than F's own trait. | two or more shared genes or sets |

When the phenotype is the factor's own fitted trait, say the link is **within the factor's fit**; it is not independent support. A different phenotype is an independent bridge. A synthesis states a scoped candidate interpretation and leaves causal direction unresolved unless the cited evidence distinguishes it.

Other Claims stay welcome: knowledge-graph observations (biomarker KG, Proto-OKN), exact literature reads and contradicting evidence (`DISPUTES` or `MIXED`). They follow the ordinary evidence rules.

## 5. Soft targets

These are targets, not quotas, and are never enforced:

- about one to three atomic Claims per family the gap actually uses;
- at most about thirty atomic Claims per account;
- at least one gap-relevance synthesis.

Families without relevant evidence are left out; lint names them only as hints. Do not pad with paraphrases or duplicate observations. Snippet and locator fidelity applies to every atomic Claim, so write the first draft early and lint it.

## 6. Template

Draft IDs, symbolic values and omitted trusted bodies make this a pattern, not a document. The trusted draft writer hydrates the Mechanism, GeneSet, File and gap, and declares the prefixes.

```yaml
propositions:
  - id: urn:reveal:local:W:D:proposition-1
    proposition_kind: RESULT
    subject_entity: HGNC.SYMBOL:G
    relation: obo:RO_0002610
    object_entity: dapper:Mechanism.F
    statement: Gene G has loading 0.2502 on factor F (label).
  - id: urn:reveal:local:W:D:proposition-2
    proposition_kind: BIOLOGICAL_INTERPRETATION
    subject_entity: dapper:GeneSet.S
    relation: urn:reveal:relation:candidate-gap-connection
    object_entity: dapper:KnowledgeGap.D
    statement: Gene G and gene set S provide a candidate connection between factor F and phenotype P (within the factor's fit) and knowledge gap D.
claim_scores:
  - {id: urn:reveal:local:W:D:score-1, metric: loading, score_kind: LOADING, value: 0.2502, interpretation: Exact stored EAGGL gene loading.}
claims:
  - {id: urn:reveal:local:W:D:claim-1, proposition: urn:reveal:local:W:D:proposition-1, has_score: [urn:reveal:local:W:D:score-1],
     has_evidence: [urn:reveal:local:W:D:evidence-1], direction: SUPPORTS, status: proposed, statement: ...}
  - {id: urn:reveal:local:W:D:claim-2, proposition: urn:reveal:local:W:D:proposition-2,
     has_evidence: [urn:reveal:local:W:D:evidence-2], direction: SUPPORTS, status: proposed, statement: ...}
evidence_items:
  - {id: urn:reveal:local:W:D:evidence-1, target_proposition: urn:reveal:local:W:D:proposition-1, direction: SUPPORTS,
     was_derived_from: [dapper:File.CAPTURE], context: 'get_factor_loadings capture `/result/items/0`',
     snippet: '"loading":0.2502', explanation: ...}
  - {id: urn:reveal:local:W:D:evidence-2, target_proposition: urn:reveal:local:W:D:proposition-2, direction: SUPPORTS,
     source_claims: [urn:reveal:local:W:D:claim-1, ...], context: The atomic Claims on the G/S path, explanation: ...}
```

## 7. Alignment notes

- `prov:hadMember` is the `slot_uri` of DAPPER `GeneSet.members`, so a membership triple is the RDF statement the trusted GeneSet already makes. RO:0002351 *has member* is its closest RO equivalent.
- RO:0002610 *correlated with* asserts statistical dependence only. A loading or projection is a fitted model weight, not a probability, a biological effect size or a causal claim.
- Biolink `genetically_associated_with` aligns with PIGEAN's genetic-support scores. The scores keep their own meaning: `combined` and `log_bf` are not probabilities.
- HYCL, which DAPPER imports, relates statements to statements, not entities to gaps, so gap relevance keeps the REVEAL `candidate-gap-connection` relation (or a text-only Proposition).
- `docs/pigean-claim-model.md` explains the meaning of each metric and the older single-Claim templates, which remain valid for legacy accounts.
