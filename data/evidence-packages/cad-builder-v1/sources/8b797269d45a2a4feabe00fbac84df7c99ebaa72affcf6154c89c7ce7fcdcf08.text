# PIGEAN/EAGGL claim templates — biological propositions and result evidence

**Working design, September 25, 2026.** The four templates connect PIGEAN/EAGGL results to biological propositions. The **Proposition** states the biological involvement being assessed. **EvidenceItems** connect the observed results to that proposition. The **Claim** records the attributed assessment and summarizes its evidence in natural language. [ScientificAccount construction](scientific-account-construction.md) explains how several such claims form an account and a final synthesis addressing a selected KnowledgeGap.

These templates use existing DAPPER classes. They are design examples, not executed or minted scientific assessments. The numerical source observations remain inspectable in the appendices. No graph-path object or evidence-field extension is introduced.

The [evidence-package design](evidence-package.md) organizes the captured inputs for these templates, with a complete CAD/DisMech example and a separately namespaced legacy EAGGL fragment. It preserves mechanism loadings, trait association statistics, semantic retrieval metadata and exact source locators before the agent authors target-specific EvidenceItems.

**Factor-centered use:** one PIGEAN/EAGGL result can inform a family of gene and gene-set Propositions around a shared mechanism. Apply the templates repeatedly to the relevant genes and annotations, assess each Proposition through its evidence, and bring those assessments together in the ScientificAccount synthesis. The mapping is not one response, one Proposition or one account Claim.

**Domain definition: an EAGGL factor is a mechanism.** “Factor” is the source/API terminology for that same mechanism. There is no separate factor-to-mechanism mapping, equivalence Claim or interpretive promotion step. `Factor1` and `Factor12` are interim labels; better names will describe the mechanisms already identified by those records.

## 1. Proposition, claim and evidence have different jobs

- **Proposition — what is being assessed:** “Gene G is involved in mechanism M relevant to trait T.” This is the scientific content. Its wording does not, by itself, say how strongly it is supported or who accepts it.
- **Evidence — what was observed and how it bears on that content:** for example, a gene loading on the trait-scoped EAGGL mechanism, its interpretation as evidence of involvement, and any relevant independent biological assertions.
- **Claim — the assessment:** “The evidence supports G's involvement in M relevant to T because [the observed results and their interpreted relevance].” It records the proposition, evidence uses, direction, responsible agent and generating Activity.
- **ScientificAccount — the synthesis:** how these assessed propositions work together to address the selected gap, including qualifications, differences of scope and remaining uncertainty.

A Claim is **an assertion or assessment of** a Proposition; they are linked objects, not identical objects. Several Claims can assess the same Proposition using different methods, evidence and directions. `Claim.statement` is the human-readable summary of that assessment across its evidence lines. It should account for conflicting or inconclusive evidence rather than merely repeat the Proposition or list every raw number.

A stronger Proposition does not force a stronger assessment. Keep the biological content stable while the Claim records whether the evidence supports, disputes, mixes or leaves uncertainty about it. The same Proposition can therefore be reused when another method or source assesses it.

### Mechanism identity and labels

Use **G** for a gene, **S** for a gene set, **M** for a mechanism, **T** for a trait and **V** for the computational model/version. Where the source calls the mechanism **F** (a factor), **F and M identify the same object**. Refer to that mechanism in the biological Proposition; retain the full CFDE factor identity, model and fit provenance in its source mapping and evidence. Include species, population, tissue or disease stage on the Proposition when they are essential to its biological meaning.

Use the complete CFDE identity, such as `factor:portal:CADinT2D:cfde-inc-v2:Factor1`, to resolve the mechanism. A bare `Factor1` label is not globally unique. A missing descriptive label does not prevent mechanism-level Propositions. Improved labels name the same source mechanism; preserve source aliases and apply normal DAPPER revision/minting rules when hashable fields change. Other methods can contribute evidence about this same mechanism. Equating it with a different source record, including a DisMech mechanism, still requires an explicit identity crosswalk; similar labels or embeddings alone do not establish that equivalence.

Although these claims originate in computational results, their **biological involvement** propositions use `proposition_kind: BIOLOGICAL_INTERPRETATION` in the current DAPPER vocabulary. Observed model outputs remain in the source artifacts and EvidenceItems. If the agent chooses to record an observation as a separate source Claim, that observation's Proposition uses `RESULT`. This classifies the content being assessed, not its confidence or how it was produced.

## 2. The four templates

| Evidence source | Biological Proposition template | Core observed evidence |
|---|---|---|
| Gene → factor (mechanism) | Gene G is involved in mechanism M relevant to trait T. | G's loading on M in the T fit. |
| Gene set → factor (mechanism) | Gene set S represents a gene program involved in mechanism M relevant to trait T. | S's loading on M and the gene-set meaning/provenance. |
| Gene → trait | Gene G is involved in the biology of trait T. | PIGEAN's gene–trait relevance result for G and T. |
| Gene set → trait | Gene set S represents a gene program involved in the biology of trait T. | PIGEAN's gene-set/trait result, with the interpretation of the represented gene program. |

All four target propositions are biological content rather than statements about an API response. In the gene-set templates, the program represented by the set is the biological subject of interpretation; the template does not assert that every member individually contributes or that the set's experimental label defines the disease mechanism.

Claim statements below illustrate a **supporting** assessment. Generate the corresponding disputed, mixed or uncertain statement when warranted. Do not assign a support direction solely by applying an undocumented cutoff to a source score.

### One factor result → several assessed propositions

For a result containing genes A, B and C and gene sets S1 and S2 around mechanism M (source factor F), the candidate Proposition set can include:

- A is involved in mechanism M in the specified biological context.
- B is involved in M in that context.
- C is involved in M in that context.
- S1 describes a gene program associated with process X within M.
- S2 provides a readout of process Y within M.

Each is separately assessed by a Claim. The evidence identifies which original gene/factor or gene-set/factor observation contributes and explains the biological interpretation. A single retained source result can be reused where relevant; it does not automatically support every Proposition generated from the broader response.

Gene-set descriptions and readouts are two useful roles within the **gene-set → factor** template. Use the role warranted by the set's definition, original experiment and provenance. A set can describe a program without being a validated activity readout, and a readout does not establish that its members regulate the process. A gene's loading and a set's loading on F do not establish that the gene belongs to that set.

The account can subsequently include more specific Propositions such as “A amplifies M through X” or “B inhibits M through Y.” These refine the biological relationship being assessed; they are not additional PIGEAN source types. Use source results plus relevant functional or curated evidence and an explicit explanation to assess them. See the [worked factor-centered account](scientific-account-construction.md#6-worked-factor-centered-account).

## 3. Template 1 — gene → factor evidence

**Proposition**

> Gene {G} is involved in mechanism {M} relevant to trait {T}.

**Claim statement**

> The evidence supports {G}'s involvement in mechanism {M} relevant to {T}: EAGGL assigns {G} a loading of {v} on this mechanism in the {T} fit under {V}.

The claim can summarize additional evidence lines from other methods or knowledge graphs and any limitations affecting the overall assessment. It is not restricted to a single score or single source.

**Evidence content**

- Exact gene identity and trait scope.
- Full factor identity, model/version and available fit/build provenance.
- Source `factor_value`, its meaning as a loading, and the retained row/edge locator.
- The explanation of how the observed loading bears on G's involvement in this mechanism; no separate assertion that F is M is required.
- Other relevant evidence uses and their target-specific directions, when present.

**Captured CAD example:** SHH has `factor_value=0.5042` for `factor:portal:CADinT2D:cfde-inc-v2:Factor1` in `cfde-inc-v2`. A candidate Proposition is “SHH is involved in mechanism Factor1 relevant to coronary artery disease in people with type 2 diabetes.” The mechanism resolves to that full source ID. The loading belongs in the evidence supporting the Claim's assessment. A better descriptive name can replace the interim display label without requiring a factor-to-mechanism inference.

## 4. Template 2 — gene set → factor evidence

**Proposition**

> Gene set {S} represents a gene program involved in mechanism {M} relevant to trait {T}.

**Claim statement**

> The evidence supports the gene program represented by {S} being involved in mechanism {M} relevant to {T}: {S} has a loading of {v} on this EAGGL mechanism under {V}, interpreted in the context of {the gene-set definition and provenance}.

**Evidence content**

- Exact source gene-set key, resolved DAPPER GeneSet and available collection/construction provenance.
- Full factor identity, trait/model context and source `factor_value`.
- The source semantics of the gene set and the explanation of how its loading bears on the represented program's involvement in M.
- A statement of what any tissue, comparison or perturbation label contributes to the interpretation.

**Captured CAD example:** G1, the retained AMP_AD/GTEx set, has `factor_value=1.0` on the same factor. G1 resolves to `dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD`; its complete source name is in Appendix A. The loading is an observation. The biological meaning of the composite annotation and its relationship to M must be explained in the evidence use. A loading of one is not certainty that M is true.

**Process/readout variants:** where supported, the target can be “S describes a gene program associated with process X within M” or “S provides a readout of process X within M.” Its Claim explains how the factor result and the gene set's source definition support that particular role. The account synthesis can then connect genes' assessed roles in M to the processes described or read out by those sets.

## 5. Template 3 — gene → trait evidence

**Proposition**

> Gene {G} is involved in the biology of trait {T}.

**Claim statement**

> The PIGEAN evidence supports {G}'s involvement in the biology of {T}, based on a combined gene–trait relevance score of {v} under {V}, interpreted as {the source-defined relevance assessment}, together with {any other relevant evidence and qualifications}.

“Involved” must have an explicit intended meaning in the biological scope. Do not silently turn involvement into necessity, sufficiency or direct causation. A narrower gene–trait association Proposition can be used when that is the actual scientific target, rather than changing the meaning of an existing involvement Proposition.

**Evidence content**

- Gene and trait identities, relevant biological scope and source analysis provenance.
- `combined` with its original method-specific interpretation.
- Source `log_bf` and `prior` retained with their own meanings; no invented probability transformation or assumed log base.
- The reasoning connecting model-inferred relevance to the biological involvement being assessed.

**Captured CAD example:** the SHH row has `combined=3.86`, `log_bf=2.14` and `prior=1.72`. The candidate biological Proposition is “SHH is involved in the biology of coronary artery disease in people with type 2 diabetes.” The model, numbers and their interpretation belong in its evidence and Claim assessment. This is distinct from assessing SHH's involvement in a particular mechanism M.

## 6. Template 4 — gene set → trait evidence

**Proposition**

> Gene set {S} represents a gene program involved in the biology of trait {T}.

**Claim statement**

> The PIGEAN evidence supports the gene program represented by {S} being involved in the biology of {T}, based on the reported gene-set effects beta={b} and beta_uncorrected={u} under {V}, interpreted in the context of {the gene-set definition and relevant biology}.

**Evidence content**

- Exact DAPPER GeneSet/source identity, trait and biological scope.
- Source `beta` and `beta_uncorrected` as separately identified effect estimates.
- The annotation's definition and provenance supporting its interpretation as a gene program.
- The explanation connecting the estimated relationship to trait-relevant genes with the biological Proposition.

**Captured CAD example:** G1 has `beta=0.115` and `beta_uncorrected=0.119`. These are evidence for assessing the candidate gene-program/trait Proposition. They do not themselves establish that the source experiment reproduces the disease or that its tissue label determines where the trait mechanism acts. Preserve `rs_score` in the source record until its interpretation is established.

## 7. Represent this with current DAPPER

For these small examples, use **one biological Proposition and one Claim assessing it**, with an EvidenceItem containing the observed result and its interpretation. Link the EvidenceItem directly to the retained DAPPER File through `was_derived_from`; put the exact named-artifact/row locator in `context` and a verbatim source excerpt in `snippet`. The original JSON retains the structured metric names and values. No extra source-result Proposition, Claim or ClaimScore is needed.

```text
Biological Proposition ← assessed by Claim
                            │
                            └─ has_evidence → EvidenceItem
                                               ├─ target_proposition → biological Proposition
                                               ├─ was_derived_from → retained source File
                                               ├─ snippet + context → observation and exact row locator
                                               └─ explanation → how they bear on this Proposition
```

- The biological Claim's `statement` summarizes its assessment across the evidence uses.
- Each EvidenceItem retains its own target, direction, explanation, context and provenance.
- Numeric loadings/effects retain their original source meanings in the artifact and evidence explanation. A loading is not a confidence score for the biological target. Current EvidenceItem has no `has_score` or `source_locator` slot: these examples use its existing `snippet`, `context` and provenance fields, without inventing additional fields.
- Several EvidenceItems can target the same Proposition. Several Claims can assess that Proposition differently. Reuse source records across different targets through separate interpreted EvidenceItems.
- Resolve each EAGGL factor directly to its mechanism record. Factor/mechanism identity is part of the domain model; it does not require a supporting Proposition or Claim. Claims assess the gene, gene-set, process and regulatory relationships involving that mechanism.

**Optional source-Claim pattern:** the agent may separately record a result as a `RESULT` Proposition and source Claim when that result merits its own durable citation, assessment, or typed ClaimScores. In that case, an EvidenceItem references it through `source_claims`. This is a modeling choice, not a required wrapper around every numeric observation. Artifact-based and source-Claim-based EvidenceItems can coexist in one assessment; neither duplicates the underlying observation. Appendix B deliberately uses the simpler artifact-based pattern for all four examples.

### Template 1: pre-mint DAPPER fragment

This is a **schematic supporting-assessment template** with symbolic G/M/T/V; F is the source identifier for M. The source File and row locator must resolve to the actual result before it can be instantiated. Mechanism identity is supplied by the source mapping, not another evidence Claim.

```yaml
propositions:
  - id: urn:example:biological-proposition
    statement: Gene G is involved in mechanism M relevant to trait T.
    proposition_kind: BIOLOGICAL_INTERPRETATION
    scope: >-
      Specify the gene and biological mechanism identities, trait,
      organism and any material population, tissue or disease-stage scope.
claims:
  - id: urn:example:biological-claim
    proposition: urn:example:biological-proposition
    statement: >-
      The evidence supports G's involvement in M relevant to T:
      its reported loading on this EAGGL mechanism supports this
      assessment within the given trait and biological scope.
    direction: SUPPORTS
    status: proposed
    has_evidence: [urn:example:interpreted-factor-evidence]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
evidence_items:
  - id: urn:example:interpreted-factor-evidence
    target_proposition: urn:example:biological-proposition
    direction: SUPPORTS
    evidence_source: CFDE PIGEAN/EAGGL
    explanation: >-
      Explain what the loading observes and how it supports G's involvement
      in mechanism M for T. F is the source identity of that same mechanism.
      State the inferential assumptions and material counterevidence.
    context: >-
      Name the exact factor F, trait fit, model V, source artifact and
      JSON pointer/row locator, and the biological setting of this use.
    was_derived_from: [urn:example:source-response-file]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
```

All fields above exist in DAPPER. Temporary identifiers, symbolic content and omitted dependency records make this a design fragment rather than a complete validated document. Appendix B instantiates all four templates with the captured CAD-in-T2D results, showing the biological assessments, their EvidenceItems and the supporting numerical observations together.

## 8. Linking assessments across methods

### Biological direction in the account synthesis

The intended synthesis can explain **which genes amplify or inhibit a mechanism, through which processes, and which gene sets describe or read out those processes**. These relationships must already be represented in assessed Propositions before being expressed together in the closing.

Keep three meanings separate:

- **Biological relationship:** “A inhibits M” or “A amplifies M” is part of the Proposition's scientific content, including material intervention and biological context. An optional structured predicate can express the same relation when resolved.
- **Evidence assessment:** `Claim.direction` / `EvidenceItem.direction` describes support or dispute of that content. Evidence supporting inhibition has `direction: SUPPORTS`; it is not assigned DISPUTES merely because the biological effect is inhibitory.
- **Source quantity/contrast:** a factor loading or an experimental `up`/`dn` label is interpreted in its original source context. A positive loading does not by itself mean amplification; a `dn` label does not by itself mean inhibition of the trait-related mechanism.

Involvement-only evidence supports an involvement assessment. Inhibition, amplification, a route through a named process, or a validated process readout each needs its own explicit evidential argument. A directional hypothesis can still be proposed with uncertain assessment; the synthesis should describe it as proposed rather than present a stronger assessment than its Claims record.

### Reusing the proposition across methods

1. Establish a shared biological Proposition with matching entity identities, intended relation and scientific scope. Reuse its exact DAPPER identity when it is genuinely the same content; do not rely on matching labels or paraphrase similarity.
2. Record each method's results and interpreted evidence separately, retaining original metrics and source dependence.
3. Create separate Claims assessing the shared Proposition when the assessments are method-specific, or create an attributed integrative Claim whose EvidenceItems combine those sources.
4. The integrative Claim's statement summarizes the whole evidential assessment, including contradictions and limitations. An observation used in multiple assessments remains one underlying observation.
5. The ScientificAccount synthesizes those Claims with other relevant Propositions to address the selected gap. It does not require all of its Claims to target one Proposition.

Different model versions change evidence/assessment provenance. For factor-defined mechanisms, do not assume that matching factor numbers across versions identify the same mechanism; retain full source identities and explicit cross-version mappings. A biological Proposition can be reused when the assessed mechanism, content and scope remain the same. Changes to biological meaning or scope require a different Proposition. DAPPER digests identify representations rather than performing semantic equivalence reasoning.

## 9. Source adapter and review rules

The available source families remain the same:

- Gene–factor: BioIndex `pigean-gene-factor`; interactive `factor_gene_direct` where returned.
- Gene-set–factor: BioIndex `pigean-gene-set-factor`; interactive `factor_gene_set_direct` where returned.
- Gene–trait: BioIndex `pigean-gene-phenotype` and gene-centered results; captured factor rows can also contain these metrics.
- Gene-set–trait: BioIndex `pigean-gene-set-phenotype` and gene-set-centered results; captured factor rows can also contain these metrics.

Record each source relationship faithfully, without interpreting unavailable values as zero or repeated retrievals as independent evidence. `factor_value` is a LOADING; `combined` is a method-specific SCORE; `beta` and `beta_uncorrected` are separately named EFFECT_ESTIMATE values. Retain unknown metric semantics in raw artifacts. Pin any normalization, score interpretation or threshold used by an assessment, and retain the mechanism's exact factor source identity and label revision.

Review must separately check (a) the faithful recording of each source observation, (b) the explanation of its relevance to the biological target, and (c) whether the Claim statement accurately summarizes all its evidence. Mechanism identity is supplied directly by the EAGGL factor. Missing descriptive labels do not block involvement claims; scope mismatches or missing evidence for more specific process/readout/regulatory relationships remain explicit.

No schema change is required. The remaining work is to populate and review a coherent biological interpretation for the captured CAD example, then connect the assessed propositions through the account synthesis to a relevant selected knowledge gap.

## Appendix A — captured CAD-in-T2D evidence



The anchor is **Factor1 for coronary artery disease in type 2 diabetes**, model `cfde-inc-v2`:

```text
factor:portal:CADinT2D:cfde-inc-v2:Factor1
```

In this model, this EAGGL factor is the trait-scoped mechanism for the CAD-in-T2D result. Its scope is coronary artery disease in people with T2D, not generic T2D. The current source catalog uses this gene-set key as an interim mechanism label:

```text
AMP_AD__all_brain__AMP_AD_MAYO_TCX_Diagnosis.AOD_AD-CONTROL_ALL_dn___GTEx__brain__GTEx_aging_Brain_20-29_60-69_dn
```

Call this **G1** below. That shorthand is a display alias, not a new entity. The existing catalog import maps it to `dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD`. Its original generation history and members are not established by that catalog record.

### Captured graph

- September 24 direct expansion: eight genes and eight gene sets, each query limited to eight; no trait candidates returned. These are bounded results, not the complete factor neighborhood. A factor-target query was not captured for this CAD anchor.
- September 25 contextual query: supplied the same 17 nodes (factor + eight genes + eight gene sets), received 16 edges. All 16 edge objects exactly match the saved direct-expansion edges. It added **zero distinct edges** and no gene–gene or gene–gene-set membership relationships.
- The older `contextual-edges-user-example.json` concerns language-measurement/presubiculum factors, not this CAD factor. Do not mix it into this account.
- These are separate retrieval times under the same requested model; no upstream immutable graph-build identifier was exposed. Matching observations do not establish a globally frozen build.

Sources: [gene expansion](../data/interactive/2026-09-24/cad-in-t2d-factor-gene.json), [gene-set expansion](../data/interactive/2026-09-24/cad-in-t2d-factor-gene_set.json), [CAD contextual capture](../data/interactive/2026-09-25/cad-in-t2d-contextual-edges.json).

### Gene observations

Two additional, bounded BioIndex queries on September 25 preserve the original method fields. The gene identities match the interactive results, and `factor_value` matches their `raw_score` to floating-point precision. Do not generalize this eight-row cross-check to all families or builds.

| Gene | BioIndex `factor_value` | Interactive `normalized_score` | BioIndex `combined` |
|---|---:|---:|---:|
| SHH | 0.5042 | 3.6776304513 | 3.86 |
| TCF7L2 | 0.3813 | 2.6035690826 | 2.42 |
| SIX2 | 0.3377 | 2.2225351036 | 2.07 |
| GLI3 | 0.3245 | 2.1071760443 | 1.98 |
| FGF13 | 0.2899 | 1.8047959102 | 1.73 |
| EYA2 | 0.2801 | 1.7191504967 | 1.66 |
| CTNNB1 | 0.2787 | 1.7069155121 | 1.65 |
| WNT4 | 0.2787 | 1.7069155121 | 1.65 |

The source rows also retain `log_bf` and `prior`. For SHH these are 2.14 and 1.72. For TCF7L2 they are 2.33 and 0.0873. Preserve the exact field names and values; no transformation into a probability is established here.

The [source graph documentation](https://github.com/broadinstitute/dig-knowledge-graph#edge-types) distinguishes PIGEAN gene/trait scores from EAGGL factor loadings and gene-set/trait effect estimates. Its static-export description does not document the interactive API's normalization formula.

Source: [gene-factor capture](../data/cfde/cad-in-t2d/pigean-gene-factor.json), with [original response bytes](../data/cfde/cad-in-t2d/pigean-gene-factor.response.json).

### Gene-set observations

The eight retained gene sets, in returned order:

1. G1: AMP_AD MAYO TCX `Diagnosis.AOD_AD-CONTROL_ALL_dn` / GTEx brain aging `20-29_60-69_dn`; loading **1.0**.
2. AMP_AD MAYO TCX `Diagnosis_AD-OTHER_ALL_dn` / the same GTEx brain-aging label; loading **0.759**.
3. PsychENCODE `geneM16`; loading **0.4518**.
4. IGVF `IGVFDS4003HZAB`, perturbation `chr5:173234812-173235812_dn`; loading **0.4065**.
5. AMP_AD MAYO TCX `SourceDiagnosis_AD-PATH_AGE_ALL_dn` / the same GTEx label; loading **0.3238**.
6. IGVF `IGVFDS6332VCTO`, perturbation `ZFP64_up`; loading **0.246**.
7. GTEx small-intestine aging `20-29_50-59_up`; loading **0.2186**.
8. IGVF `IGVFDS2266YDVM`, perturbation `chr5:88882966-88883966_dn`; loading **0.1849**.

These descriptions abbreviate source names; they do not establish how the composite sets were constructed. In particular, the separators do not prove union/intersection, `dn` does not establish a protective effect on CAD, and a brain-derived annotation does not establish a brain-specific CAD mechanism.

G1 has `factor_value=1.0`, `beta=0.115`, `beta_uncorrected=0.119`, `rs_score=0.9840045398876323`; its interactive normalized score is `4.400714758178104`. The second set illustrates why metrics need separate interpretation: loading `0.759`, `beta=0.00121`, `beta_uncorrected=0.121`. Do not collapse these into one confidence number. The meaning/calibration of `rs_score` is not established by this capture.

Source: [gene-set-factor capture](../data/cfde/cad-in-t2d/pigean-gene-set-factor.json), with [original response bytes](../data/cfde/cad-in-t2d/pigean-gene-set-factor.response.json). Both BioIndex calls returned eight rows with `limit=8` and no continuation, but their progress metadata shows fewer bytes read than total; a missing cursor is not evidence of completeness.


## Appendix B — worked claims and supporting PIGEAN/EAGGL observations

A complete multi-claim [ScientificAccount YAML packet](../design/data/cad-account/scientific-account.yaml) now instantiates this approach for the HTML design. See its [source boundaries and reproduction guide](../design/data/cad-account/README.md). The fragments below explain the four core templates individually; the full packet also includes explicitly illustrative gene-set membership and KG evidence.

Each example below contains **one biological Proposition, one Claim, and one EvidenceItem**. The EvidenceItem retains the observation, explains its bearing on the Proposition, and points directly to the captured source File. The first two assess involvement in the EAGGL mechanism itself. The other two assess involvement in the trait's biology. None introduces a second Claim merely to restate the observed score.

The numbers and row locators come from the captures in Appendix A. The biological Claim statements, `SUPPORTS` directions and evidence explanations are **illustrative authored assessments**, not fields returned by CFDE or output from an executed agent. They show the intended modeling without inventing a score threshold or probability. These are unminted design fragments: File, Activity, agent and imported GeneSet dependencies must be supplied before validation and minting.

### Shared mechanism identity

All references to mechanism **Factor1** below resolve to `factor:portal:CADinT2D:cfde-inc-v2:Factor1`. The temporary DAPPER Mechanism reference is `urn:example:mechanism-cadint2d-factor1`. The factor and the mechanism are the same object; there is no intervening identity Claim.

```yaml
mechanisms:
  - id: urn:example:mechanism-cadint2d-factor1
    name: CAD-in-T2D mechanism Factor1
    description: >-
      EAGGL mechanism identified by
      factor:portal:CADinT2D:cfde-inc-v2:Factor1, relevant to coronary
      artery disease in people with type 2 diabetes. Factor1 is its
      interim display label pending a descriptive mechanism name.
```

The source-to-digest mapping retains that full CFDE identity and source revision alongside the catalog File. The original catalog label remains available in the source artifact; this display label does not replace its captured bytes. G1 below is the existing `dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD`, whose full source key is recorded in Appendix A.

### B1. Gene → mechanism — SHH and Factor1

**Proposition:** SHH is involved in mechanism Factor1 relevant to coronary artery disease in people with type 2 diabetes.

The Claim assesses that involvement using SHH's loading of **0.5042** on the mechanism. The EvidenceItem quotes that observed value, explains its use, and identifies the original response row.

```yaml
propositions:
  - id: urn:example:p-gene-mechanism-involvement
    statement: >-
      SHH is involved in mechanism Factor1 relevant to coronary artery
      disease in people with type 2 diabetes.
    proposition_kind: BIOLOGICAL_INTERPRETATION
    subject_entity: urn:cfde:gene:SHH
    relation: urn:reveal:relation:involved-in
    object_entity: urn:example:mechanism-cadint2d-factor1
    scope: >-
      Source gene SHH; coronary artery disease in people with type 2 diabetes
      (portal CADinT2D); mechanism factor:portal:CADinT2D:cfde-inc-v2:Factor1.
claims:
  - id: urn:example:c-gene-mechanism-involvement
    proposition: urn:example:p-gene-mechanism-involvement
    statement: >-
      The EAGGL evidence supports SHH's involvement in mechanism Factor1
      relevant to coronary artery disease in people with type 2 diabetes:
      SHH has a reported loading of 0.5042 on this mechanism under cfde-inc-v2.
    direction: SUPPORTS
    status: proposed
    has_evidence: [urn:example:e-gene-mechanism-involvement]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
evidence_items:
  - id: urn:example:e-gene-mechanism-involvement
    target_proposition: urn:example:p-gene-mechanism-involvement
    direction: SUPPORTS
    evidence_source: CFDE EAGGL gene-mechanism result
    snippet: '"factor_value":0.5042'
    explanation: >-
      The retained result assigns SHH a loading of 0.5042 on this mechanism.
      The loading measures its association with the EAGGL mechanism and
      is used here to support the scoped involvement Proposition. Factor1
      is the mechanism itself. This assessment does not assign an
      inhibitory or amplifying role to SHH.
    context: >-
      Gene SHH; mechanism factor:portal:CADinT2D:cfde-inc-v2:Factor1;
      portal CADinT2D; cfde-inc-v2.
      Source locator: pigean-gene-factor.response.json#/data/0 in
      urn:example:gene-factor-response-file; factor_value is a loading.
      The same direct edge in the contextual response is the same
      observation, not independent corroboration.
    was_derived_from: [urn:example:gene-factor-response-file]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
```

### B2. Gene set → mechanism — G1 and Factor1

**Proposition:** The gene program represented by G1 is involved in mechanism Factor1 relevant to coronary artery disease in people with type 2 diabetes.

The Claim uses G1's mechanism loading of **1.0**. G1's provenance supplies the identity and meaning of the annotation; an additional process-description or readout Proposition can be assessed when those source details are available.

```yaml
propositions:
  - id: urn:example:p-geneset-mechanism-involvement
    statement: >-
      The gene program represented by G1 is involved in mechanism Factor1
      relevant to coronary artery disease in people with type 2 diabetes.
    proposition_kind: BIOLOGICAL_INTERPRETATION
    subject_entity: dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD
    relation: urn:reveal:relation:involved-in
    object_entity: urn:example:mechanism-cadint2d-factor1
    scope: >-
      G1 is the retained AMP_AD MAYO TCX / GTEx brain-aging annotation,
      dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD;
      portal CADinT2D; mechanism factor:portal:CADinT2D:cfde-inc-v2:Factor1.
claims:
  - id: urn:example:c-geneset-mechanism-involvement
    proposition: urn:example:p-geneset-mechanism-involvement
    statement: >-
      The EAGGL evidence supports the gene program represented by G1 being
      involved in mechanism Factor1 relevant to coronary artery disease
      in people with type 2 diabetes: G1 has a reported loading of 1.0
      on this mechanism under cfde-inc-v2.
    direction: SUPPORTS
    status: proposed
    has_evidence: [urn:example:e-geneset-mechanism-involvement]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
evidence_items:
  - id: urn:example:e-geneset-mechanism-involvement
    target_proposition: urn:example:p-geneset-mechanism-involvement
    direction: SUPPORTS
    evidence_source: CFDE EAGGL gene-set-mechanism result
    snippet: '"factor_value":1.0'
    explanation: >-
      The retained result assigns G1 a loading of 1.0 on this mechanism.
      Its association with the mechanism supports involvement of the
      represented gene program at the annotation level. The assessment
      does not assert that every member participates or that G1 measures
      a particular process's activity. The loading is not a probability.
    context: >-
      G1 is dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD;
      mechanism factor:portal:CADinT2D:cfde-inc-v2:Factor1;
      portal CADinT2D; cfde-inc-v2.
      Source locator: pigean-gene-set-factor.response.json#/data/0 in
      urn:example:geneset-factor-response-file; factor_value is a loading.
      The mechanism's current catalog label repeats G1's source name;
      that repeated label is not an additional observation.
    was_derived_from: [urn:example:geneset-factor-response-file]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
```

### B3. Gene → trait — SHH and CAD-in-T2D

**Proposition:** SHH is involved in the biology of coronary artery disease in people with type 2 diabetes.

This uses the gene–trait `combined` score of **3.86**, distinct from the mechanism loading in B1. Both observations occur in the same captured row; they assess different Propositions and are not presented as independent studies. The row's `log_bf=2.14` and `prior=1.72` remain available as source context.

```yaml
propositions:
  - id: urn:example:p-gene-trait-involvement
    statement: >-
      SHH is involved in the biology of coronary artery disease
      in people with type 2 diabetes.
    proposition_kind: BIOLOGICAL_INTERPRETATION
    scope: >-
      Source gene SHH; coronary artery disease in people with type 2
      diabetes (portal CADinT2D); trait-level biological involvement.
claims:
  - id: urn:example:c-gene-trait-involvement
    proposition: urn:example:p-gene-trait-involvement
    statement: >-
      The PIGEAN evidence supports SHH's involvement in the biology of
      coronary artery disease in people with type 2 diabetes, based on
      its reported combined gene-trait relevance score of 3.86 under
      cfde-inc-v2.
    direction: SUPPORTS
    status: proposed
    has_evidence: [urn:example:e-gene-trait-involvement]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
evidence_items:
  - id: urn:example:e-gene-trait-involvement
    target_proposition: urn:example:p-gene-trait-involvement
    direction: SUPPORTS
    evidence_source: CFDE PIGEAN gene-trait result
    snippet: '"combined":3.86'
    explanation: >-
      PIGEAN reports combined=3.86 for SHH and CADinT2D. This method-specific
      gene-trait relevance result is used here to support SHH's biological
      involvement in the trait. It assesses trait relevance rather than
      loading on a specific mechanism and supplies no calibrated
      probability or signed regulatory effect.
    context: >-
      Gene SHH; portal CADinT2D; cfde-inc-v2.
      Source locator: pigean-gene-factor.response.json#/data/0 in
      urn:example:gene-factor-response-file.
      The row also reports log_bf=2.14 and prior=1.72. These source
      quantities are retained without an invented transformation or
      treatment as independent corroborating observations.
    was_derived_from: [urn:example:gene-factor-response-file]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
```

### B4. Gene set → trait — G1 and CAD-in-T2D

**Proposition:** The gene program represented by G1 is involved in the biology of coronary artery disease in people with type 2 diabetes.

The Claim uses the separately reported effects **beta=0.115** and **beta_uncorrected=0.119**, with G1's annotation context. These are trait-level evidence; the mechanism loading in B2 remains a different observation from the same source analysis.

```yaml
propositions:
  - id: urn:example:p-geneset-trait-involvement
    statement: >-
      The gene program represented by G1 is involved in the biology
      of coronary artery disease in people with type 2 diabetes.
    proposition_kind: BIOLOGICAL_INTERPRETATION
    subject_entity: dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD
    relation: urn:reveal:relation:involved-in
    object_entity: urn:cfde:trait:portal:CADinT2D
    scope: >-
      G1 is dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD, the retained
      AMP_AD MAYO TCX / GTEx brain-aging annotation; portal CADinT2D;
      trait-level involvement of the represented program.
claims:
  - id: urn:example:c-geneset-trait-involvement
    proposition: urn:example:p-geneset-trait-involvement
    statement: >-
      The PIGEAN evidence supports the gene program represented by G1
      being involved in the biology of coronary artery disease in people
      with type 2 diabetes, based on the reported gene-set effects
      beta=0.115 and beta_uncorrected=0.119 under cfde-inc-v2.
    direction: SUPPORTS
    status: proposed
    has_evidence: [urn:example:e-geneset-trait-involvement]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
evidence_items:
  - id: urn:example:e-geneset-trait-involvement
    target_proposition: urn:example:p-geneset-trait-involvement
    direction: SUPPORTS
    evidence_source: CFDE PIGEAN gene-set-trait result
    snippet: '"beta":0.115,"beta_uncorrected":0.119'
    explanation: >-
      The retained result reports beta=0.115 and beta_uncorrected=0.119
      for G1's association with trait-relevant genes. These effect
      estimates support the represented program's trait-level involvement
      in this example assessment. They retain distinct source meanings
      and are not independent replications, confidence probabilities or
      evidence that the source tissue is where the CAD mechanism acts.
    context: >-
      G1 is dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD;
      portal CADinT2D; cfde-inc-v2.
      Source locator: pigean-gene-set-factor.response.json#/data/0 in
      urn:example:geneset-factor-response-file.
      GeneSet identity currently establishes catalog/import provenance;
      membership and original construction history are not supplied here.
      The source rs_score is retained without an unverified interpretation.
    was_derived_from: [urn:example:geneset-factor-response-file]
    was_generated_by: urn:example:interpretation-activity
    was_attributed_to: [urn:example:responsible-agent]
```

### Using these examples in a ScientificAccount

The four `*-involvement` Claims are the biological assessments available for `component_claims`. Their numerical observations remain inspectable through EvidenceItems and retained source Files, with no additional source-result Claims in these examples. B1/B2 describe involvement in the same mechanism; B3/B4 assess the gene and gene program at trait level. The account synthesis explains how the selected assessments address the exact selected KnowledgeGap, as described in [ScientificAccount construction](scientific-account-construction.md). These captures alone do not supply a matching gap or the more specific inhibition/amplification and process/readout assessments from the schematic account.

The same source artifact/row can inform other Propositions through separate target-specific EvidenceItems. The agent can choose the optional source-Claim pattern when separately addressable source assessments are useful, including for selected Proto-OKN assertions. Either form can supply additional evidence targeting an existing biological Proposition; the owning Claim's statement then summarizes the combined assessment. No external-KG result is fabricated in these captured-data examples.
