#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime"]
# ///
"""Cross-record scientific-content checks and an authored-text paragraph renderer.

    uv run tools/scientific_claims.py examples/example_scientific_account.yaml

This assembles supplied text; it does not infer scientific conclusions. Use the
provenance linter for complete schema, identifier, and provenance validation.
"""
from __future__ import annotations

import argparse
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "identity"))
from dapper_identity import DOC_GROUPS, load_schema


def index_document(document: dict) -> dict[str, tuple[str, dict]]:
    return {node["id"]: (cls, node)
            for group, cls in DOC_GROUPS.items()
            for node in (document.get(group) or [])
            if isinstance(node, dict) and isinstance(node.get("id"), str)}


def _refs(node: dict, field: str) -> list[str]:
    value = node.get(field)
    return [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)]


@lru_cache(maxsize=None)
def _schema():
    return load_schema()


@lru_cache(maxsize=None)
def _accepts(expected: str, actual: str | None) -> bool:
    """A reference to `expected` accepts it or any subclass (a CausalLink is a
    Proposition; a MechanisticModel is an ExplanatoryModel)."""
    if actual is None:
        return False
    if actual == expected:
        return True
    return expected in _schema().class_ancestors(actual)


def _find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """One directed cycle as a node list (first node repeated), or None."""
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(u: str) -> list[str] | None:
        state[u] = 1
        stack.append(u)
        for v in sorted(edges.get(u, ())):
            if state.get(v) == 1:
                return stack[stack.index(v):] + [v]
            if not state.get(v):
                found = visit(v)
                if found:
                    return found
        state[u] = 2
        stack.pop()
        return None

    for u in sorted(edges):
        if not state.get(u):
            found = visit(u)
            if found:
                return found
    return None


def _graph_parents(graph: dict, nodes: dict[str, tuple[str, dict]]) -> dict[str, set[str]]:
    """Each variable's causes in a graph, from its links."""
    parents: dict[str, set[str]] = {}
    for entry in graph.get("links") or []:
        link = nodes.get(entry.get("link") if isinstance(entry, dict) else None, (None, {}))[1]
        if isinstance(link.get("cause"), str) and isinstance(link.get("effect"), str):
            parents.setdefault(link["effect"], set()).add(link["cause"])
    return parents


def check_structural_causal_model(nid: str, scm: dict,
                                  nodes: dict[str, tuple[str, dict]]) -> list[tuple[str, str]]:
    """Specifications and equations agree with the SCM's own graph."""
    problems = []
    if scm.get("has_feedback") is True:
        problems.append((nid, "a StructuralCausalModel must be acyclic; has_feedback must not be true"))
    variables = set(_refs(scm, "variables"))
    parents = _graph_parents(scm, nodes)
    specified = set()
    for i, spec in enumerate(scm.get("variable_specifications") or []):
        if not isinstance(spec, dict):
            continue
        where, var = f"{nid}.variable_specifications[{i}]", spec.get("variable")
        if var not in variables:
            problems.append((where, f"{var} is not in this model's variables"))
        if var in specified:
            problems.append((where, f"{var} is specified more than once"))
        specified.add(var)
        if spec.get("distribution") and parents.get(var):
            problems.append((where, f"{var} has causes in the model; give it an equation, not a distribution"))
    explained = set()
    for i, equation in enumerate(scm.get("equations") or []):
        if not isinstance(equation, dict):
            continue
        where, outcome = f"{nid}.equations[{i}]", equation.get("outcome")
        if outcome not in variables:
            problems.append((where, f"{outcome} is not in this model's variables"))
        if outcome in explained:
            problems.append((where, f"{outcome} has more than one equation"))
        explained.add(outcome)
        inputs, causes = set(_refs(equation, "inputs")), parents.get(outcome, set())
        if inputs != causes:
            problems.append((where, f"inputs must be exactly {outcome}'s causes in the graph: "
                             f"missing {sorted(causes - inputs)}, extra {sorted(inputs - causes)}"))
    return problems


DIRECTED = {"SUPPORTS", "DISPUTES", "MIXED"}
MEDIATED = {"CONTROLLED_DIRECT_EFFECT", "NATURAL_DIRECT_EFFECT", "NATURAL_INDIRECT_EFFECT"}


def check_causal_estimand(nid: str, estimand: dict,
                          nodes: dict[str, tuple[str, dict]]) -> list[tuple[str, str]]:
    """An estimand's variables are distinct and belong to its model."""
    problems = []
    treatment, outcome, mediator = (estimand.get(k) for k in ("treatment", "outcome", "mediator"))
    if treatment is not None and treatment == outcome:
        problems.append((nid, "treatment and outcome must differ"))
    if estimand.get("estimand_kind") in MEDIATED and not mediator:
        problems.append((nid, f"{estimand['estimand_kind']} needs a mediator"))
    directed = sorted(ref for ref, (cls, node) in nodes.items() if cls == "Claim"
                      and node.get("proposition") == nid and node.get("direction") in DIRECTED)
    if directed and not estimand.get("predicted_change"):
        problems.append((nid, f"claim {directed[0]} gives a direction on this estimand; state the change "
                         "its model predicts in predicted_change, so the direction reads against it"))
    model_cls, model = nodes.get(estimand.get("explanatory_model"), (None, {}))
    if _accepts("CausalGraph", model_cls):
        variables = set(_refs(model, "variables"))
        named = [("treatment", treatment), ("outcome", outcome), ("mediator", mediator)]
        named += [(f"interventions[{i}]", e.get("variable"))
                  for i, e in enumerate(estimand.get("interventions") or []) if isinstance(e, dict)]
        for field, var in named:
            if isinstance(var, str) and var not in variables:
                problems.append((f"{nid}.{field}", f"{var} is not in the model's variables"))
    return problems


def check_discriminating_experiment(nid: str, experiment: dict,
                                    nodes: dict[str, tuple[str, dict]]) -> list[tuple[str, str]]:
    """Predictions stay within the experiment's estimands and tested
    propositions, and at least two of them on one estimand differ."""
    problems = []
    estimands, tests = set(_refs(experiment, "estimands")), set(_refs(experiment, "tests"))
    by_estimand: dict[str, set[tuple]] = {}
    for i, prediction in enumerate(experiment.get("predictions") or []):
        if not isinstance(prediction, dict):
            continue
        where = f"{nid}.predictions[{i}]"
        estimand, proposition = prediction.get("estimand"), prediction.get("proposition")
        if estimand not in estimands:
            problems.append((where, f"{estimand} is not one of the experiment's estimands"))
        if proposition not in tests:
            problems.append((where, f"{proposition} is not one of the propositions the experiment tests"))
        if not _accepts("CausalEstimand", nodes.get(estimand, (None, {}))[0]):
            problems.append((where, f"{estimand} must resolve to CausalEstimand"))
        holds = prediction.get("proposition_holds", True) is not False
        by_estimand.setdefault(estimand, set()).add((proposition, holds, prediction.get("predicted_change")))
    if not any(len({(p, h) for p, h, _ in group}) > 1 and len({c for _, _, c in group}) > 1
               for group in by_estimand.values()):
        problems.append((nid, "no estimand has differing predictions from two propositions (or one "
                         "proposition holding and failing), so the experiment does not discriminate"))
    treated = set()
    for ref in estimands:
        estimand = nodes.get(ref, (None, {}))[1]
        treated |= {estimand.get("treatment"), estimand.get("outcome")}
    for var in _refs(experiment, "instruments"):
        if var in treated:
            problems.append((f"{nid}.instruments", f"{var} is a treatment or outcome of an estimand, "
                             "not an instrument for one"))
    return problems


def target_variables(cls: str | None, proposition: dict) -> set[str] | None:
    """The variables a causal proposition is about: a link's cause and effect, an
    estimand's treatment and outcome. None for a proposition with no variables."""
    if _accepts("CausalLink", cls):
        return {proposition.get("cause"), proposition.get("effect")} - {None}
    if _accepts("CausalEstimand", cls):
        return {proposition.get("treatment"), proposition.get("outcome")} - {None}
    return None


DIRECT_BASES = {"IN_CONTEXT", "BY_ANALOGY"}


def check_evidence_basis_variables(nid: str, claim: dict,
                                   nodes: dict[str, tuple[str, dict]]) -> list[tuple[str, str]]:
    """IN_CONTEXT and BY_ANALOGY say the evidence is on the proposition's own
    variables. When the evidence carrying the claim's direction names the
    variables it measured, at least one item must name all of them."""
    if claim.get("evidence_basis") not in DIRECT_BASES:
        return []
    cls, proposition = nodes.get(claim.get("proposition"), (None, {}))
    wanted = target_variables(cls, proposition)
    if not wanted:
        return []
    carrying = [nodes.get(ref, (None, {}))[1] for ref in _refs(claim, "has_evidence")]
    carrying = [e for e in carrying if e.get("direction") == claim.get("direction")]
    named = [set(_refs(e, "measured_variables")) for e in carrying if e.get("measured_variables")]
    if named and not any(wanted <= v for v in named):
        return [(nid, f"evidence_basis {claim['evidence_basis']} says the evidence is on the proposition's own "
                      f"variables, but no evidence carrying its direction measured all of {sorted(wanted)}; "
                      "evidence on other variables makes the basis INDIRECT")]
    return []


GENOTYPE_PROPOSITIONS = {"GeneticAssociation": "genotype", "CausalLink": "cause"}


def check_genetic_estimates(nid: str, claim: dict,
                            nodes: dict[str, tuple[str, dict]]) -> list[tuple[str, str]]:
    """A GeneticEffectEstimate's sign follows an effect allele, so the claim's
    proposition must be about a GenotypeVariable."""
    estimates = [ref for ref in _refs(claim, "has_score")
                 if nodes.get(ref, (None, {}))[0] == "GeneticEffectEstimate"]
    if not estimates:
        return []
    cls, proposition = nodes.get(claim.get("proposition"), (None, {}))
    field = next((f for c, f in GENOTYPE_PROPOSITIONS.items() if _accepts(c, cls)), None)
    variable = nodes.get(proposition.get(field) if field else None, (None, {}))[0]
    if variable != "GenotypeVariable":
        return [(nid, f"GeneticEffectEstimate {ref} needs a proposition about a GenotypeVariable "
                 "(a GeneticAssociation, or a CausalLink caused by one), whose effect allele "
                 "gives the estimate its sign") for ref in estimates]
    return []


def check_genetic_effect_estimate(nid: str, score: dict) -> list[tuple[str, str]]:
    """Study counts agree with each other."""
    problems = []
    studies = score.get("number_of_studies")
    if isinstance(studies, int) and studies > 1 and score.get("meta_analysis") is not True:
        problems.append((nid, f"{studies} studies combined is a meta-analysis; set meta_analysis: true"))
    if score.get("meta_analysis") is False and isinstance(studies, int) and studies != 1:
        problems.append((nid, "meta_analysis is false but number_of_studies is not 1"))
    counts = [score.get(k) for k in ("number_of_cases", "number_of_controls")]
    total = score.get("sample_size")
    if all(isinstance(c, int) for c in counts) and isinstance(total, int) and sum(counts) > total:
        problems.append((nid, f"cases and controls ({sum(counts)}) exceed sample_size ({total})"))
    return problems


def check_causal_graph(nid: str, graph: dict,
                       nodes: dict[str, tuple[str, dict]]) -> list[tuple[str, str]]:
    """A graph's links stay inside its variable set, appear once, and form a
    DAG unless the graph declares feedback."""
    problems = []
    variables = set(_refs(graph, "variables"))
    edges: dict[str, set[str]] = {}
    seen = set()
    for i, entry in enumerate(graph.get("links") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("link"), str):
            continue
        where, ref = f"{nid}.links[{i}]", entry["link"]
        if ref in seen:
            problems.append((where, f"{ref} appears more than once in this graph"))
        seen.add(ref)
        cls, link = nodes.get(ref, (None, {}))
        if not _accepts("CausalLink", cls):
            problems.append((where, f"{ref} must resolve to CausalLink; found {cls or 'no local record'}"))
            continue
        for mechanism in _refs(entry, "via_mechanism"):
            if not _accepts("Mechanism", nodes.get(mechanism, (None, {}))[0]):
                problems.append((where, f"via_mechanism {mechanism} must resolve to Mechanism"))
        cause, effect = link.get("cause"), link.get("effect")
        for end in (cause, effect):
            if isinstance(end, str) and end not in variables:
                problems.append((where, f"{end} is not in this graph's variables"))
        if isinstance(cause, str) and isinstance(effect, str):
            edges.setdefault(cause, set()).add(effect)
    cycle = _find_cycle(edges)
    if cycle and graph.get("has_feedback") is not True:
        problems.append((nid, "directed cycle without has_feedback: " + " -> ".join(cycle)))
    if not cycle and graph.get("has_feedback") is True:
        problems.append((nid, "has_feedback is true but the graph has no directed cycle"))
    return problems


def check_scientific_content(nodes: dict[str, tuple[str, dict]]) -> list[tuple[str, str]]:
    """Return (location, problem) pairs; schema validation checks record shapes."""
    problems = []
    targets = {
        "Proposition": {"explanatory_model": "ExplanatoryModel"},
        "CausalLink": {"explanatory_model": "ExplanatoryModel",
                       "cause": "CausalVariable", "effect": "CausalVariable"},
        "CausalGraph": {"variables": "CausalVariable"},
        "StructuralCausalModel": {"variables": "CausalVariable"},
        "CausalEstimand": {"explanatory_model": "ExplanatoryModel", "treatment": "CausalVariable",
                           "outcome": "CausalVariable", "mediator": "CausalVariable"},
        "MechanisticModel": {"variables": "CausalVariable"},
        "GeneticAssociation": {"explanatory_model": "ExplanatoryModel", "genotype": "GenotypeVariable",
                               "associated_variable": "CausalVariable"},
        "DiscriminatingExperiment": {"tests": "Proposition", "addresses_gaps": "KnowledgeGap",
                                     "estimands": "CausalEstimand", "instruments": "CausalVariable"},
        "Claim": {"proposition": "Proposition", "has_score": "ClaimScore", "has_evidence": "EvidenceItem"},
        "ScientificAccount": {"framing": "Framing", "component_claims": "Claim",
                              "conclusion_claims": "Claim", "explanatory_model": "ExplanatoryModel",
                              "identified_gaps": "KnowledgeGap",
                              "proposed_experiments": "ProposedExperiment"},
        "KnowledgeGap": {"concerns": "Proposition"},
        "Hypothesis": {"proposition": "Proposition", "alternative_to": "Hypothesis"},
        "EvidenceItem": {"source_claims": "Claim", "target_proposition": "Proposition",
                         "explanatory_model": "ExplanatoryModel", "measured_variables": "CausalVariable"},
        "Paragraph": {"scientific_account": "ScientificAccount"},
    }
    for nid, (cls, node) in nodes.items():
        for field, expected in targets.get(cls, {}).items():
            refs = _refs(node, field)
            if len(refs) != len(set(refs)):
                problems.append((f"{nid}.{field}", "references must be distinct"))
            for ref in refs:
                actual = nodes.get(ref, (None, {}))[0]
                if not _accepts(expected, actual):
                    problems.append((f"{nid}.{field}", f"{ref} must resolve to {expected}; found {actual or 'no local record'}"))
        if cls == "ScientificAccount":
            if not _refs(node, "framing"):
                problems.append((nid, "framing needs at least one Question, KnowledgeGap or Hypothesis"))
            members = set(_refs(node, "component_claims"))
            conclusions = set(_refs(node, "conclusion_claims"))
            if not conclusions <= members:
                problems.append((nid, "conclusion_claims must be a subset of component_claims"))
            if not members - conclusions:
                problems.append((nid, "an account needs at least one claim outside its closing conclusions"))
        if cls == "Claim":
            for ref in _refs(node, "has_evidence"):
                evidence = nodes.get(ref, (None, {}))[1]
                target = evidence.get("target_proposition")
                if target and target != node.get("proposition"):
                    problems.append((nid, f"evidence {ref} targets a different proposition"))
            if node.get("evidence_basis") == "UNTESTED" and node.get("direction") not in (None, "UNKNOWN"):
                problems.append((nid, "an UNTESTED claim has no evidence to give it a direction; "
                                 f"omit direction or use UNKNOWN, not {node['direction']}"))
            problems.extend(check_genetic_estimates(nid, node, nodes))
            problems.extend(check_evidence_basis_variables(nid, node, nodes))
        if cls == "Hypothesis" and nid in _refs(node, "alternative_to"):
            problems.append((f"{nid}.alternative_to", "a hypothesis is not an alternative to itself"))
        if cls == "DiscriminatingExperiment":
            problems.extend(check_discriminating_experiment(nid, node, nodes))
        if cls == "GeneticEffectEstimate":
            problems.extend(check_genetic_effect_estimate(nid, node))
        if cls == "Paragraph":
            problems.extend(check_citation_occurrences(nid, node, nodes))
        if _accepts("CausalGraph", cls):
            problems.extend(check_causal_graph(nid, node, nodes))
        if _accepts("StructuralCausalModel", cls):
            problems.extend(check_structural_causal_model(nid, node, nodes))
        if cls in {"CausalLink", "CausalEstimand", "GeneticAssociation"}:
            for field in ("relation", "subject_entity", "object_entity"):
                if field in node:
                    problems.append((f"{nid}.{field}", f"a {cls} states its structure with its own "
                                     f"variables, not {field}"))
        if cls == "CausalEstimand":
            problems.extend(check_causal_estimand(nid, node, nodes))

    # Evidence uses must not make a Claim depend on itself, directly or indirectly.
    dependencies = {nid: {source for e in _refs(node, "has_evidence")
                         for source in _refs(nodes.get(e, (None, {}))[1], "source_claims")}
                    for nid, (cls, node) in nodes.items() if cls == "Claim"}
    state = {}

    def visit(nid):
        if state.get(nid) == 1:
            problems.append((nid, "circular evidential support between claims"))
            return
        if state.get(nid) == 2:
            return
        state[nid] = 1
        for source in sorted(dependencies.get(nid, set())):
            visit(source)
        state[nid] = 2

    for nid in dependencies:
        visit(nid)
    return problems


def check_citation_occurrences(nid: str, paragraph: dict,
                               nodes: dict[str, tuple[str, dict]]) -> list[tuple[str, str]]:
    """Check local targets and Unicode code-point spans, independently of style.

    Schema validation checks field types. Registry availability, authorization,
    and whether the prose faithfully represents the cited object need separate
    application checks; a local graph cannot establish them.
    """
    problems = []
    text = paragraph.get("text")
    seen = set()
    for i, occurrence in enumerate(paragraph.get("citations") or []):
        where = f"{nid}.citations[{i}]"
        if not isinstance(occurrence, dict):
            continue
        target = occurrence.get("target_id")
        if not isinstance(target, str):
            continue
        cls = nodes.get(target, (None, {}))[0]
        if cls not in {"Claim", "Question", "KnowledgeGap"}:
            problems.append((where, "citation target must resolve to Claim, Question, or KnowledgeGap; "
                             f"found {cls or 'no local record'}"))
        start, end = occurrence.get("start"), occurrence.get("end")
        if type(start) is not int or type(end) is not int or not isinstance(text, str):
            continue
        if not 0 <= start < end <= len(text):
            problems.append((where, "citation span must satisfy 0 <= start < end <= text length (Unicode code points)"))
        elif "exact_text" in occurrence and occurrence["exact_text"] != text[start:end]:
            problems.append((where, "exact_text does not match the cited span"))
        revision = occurrence.get("citation_metadata_revision")
        if type(revision) is int:
            key = (target, revision, start, end)
            if key in seen:
                problems.append((where, "duplicate citation occurrence"))
            seen.add(key)
    return problems


def assemble_cited_text(segments: list[dict]) -> dict:
    """Join authored segments with spaces and calculate citation offsets.

    Each segment has text and an optional citations list containing target_id and
    citation_metadata_revision. References from render_account are not citations:
    callers must explicitly select citable targets and known registry revisions.
    No normalization is performed after offsets are calculated.
    """
    texts, citations, offset = [], [], 0
    for segment in segments:
        text = segment["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("each segment needs nonblank text")
        if texts:
            offset += 1
        for citation in segment.get("citations") or []:
            citations.append({"target_id": citation["target_id"],
                              "citation_metadata_revision": citation["citation_metadata_revision"],
                              "start": offset, "end": offset + len(text), "exact_text": text})
        texts.append(text)
        offset += len(text)
    return {"text": " ".join(texts), "citations": citations}


def render_account(document: dict, account_id: str | None = None) -> list[dict]:
    """Return ordered text segments with source references for review.

    Only authored text and explicitly labelled assessment metadata are rendered.
    No hypothesis is silently asserted and no component scores are aggregated.
    """
    nodes = index_document(document)
    problems = check_scientific_content(nodes)
    if problems:
        raise ValueError("; ".join(f"{where}: {problem}" for where, problem in problems))
    accounts = [nid for nid, (cls, _) in nodes.items() if cls == "ScientificAccount"]
    if account_id is None:
        if len(accounts) != 1:
            raise ValueError("select an account id when the document does not contain exactly one account")
        account_id = accounts[0]
    if account_id not in accounts:
        raise ValueError("account id must identify a ScientificAccount")
    account = nodes[account_id][1]
    segments = []
    included_scopes = set()

    def add(role, text, *refs):
        if isinstance(text, str) and text.strip():
            text = " ".join(text.split())
            if text[-1] not in ".?!":
                text += "."
            segments.append({"role": role, "text": text, "references": list(refs)})

    for ref in _refs(account, "framing"):
        cls, framing = nodes[ref]
        if cls == "Hypothesis":
            proposition_ref = framing["proposition"]
            proposition = nodes[proposition_ref][1]
            add("hypothesis", "Hypothesis under investigation: "
                + (framing.get("text") or proposition["statement"]), ref, proposition_ref)
            if framing.get("scope"):
                add("framing_scope", "Hypothesis scope: " + framing["scope"], ref)
            scope = proposition.get("scope")
            if scope:
                add("scope", "Scope: " + scope, proposition_ref)
                included_scopes.add(scope)
        else:
            add("framing", framing["text"], ref)
            add("knowledge_gap", framing.get("gap_description"), ref)
            if framing.get("scope"):
                add("question_scope", "Question scope: " + framing["scope"], ref)
    add("context", account.get("context"), account_id)
    for assumption in account.get("assumptions") or []:
        add("assumption", "Assumption: " + assumption, account_id)

    def claim_segments(cid, role):
        claim = nodes[cid][1]
        proposition = nodes[claim["proposition"]][1]
        # A directional assessment must never fall back to asserting its target.
        text = claim.get("statement") or ("Assessment of the proposition: " + proposition["statement"])
        add(role, text, cid, claim["proposition"])
        scope = proposition.get("scope")
        if scope and scope not in included_scopes:
            add("scope", "Scope: " + scope, claim["proposition"])
            included_scopes.add(scope)
        if claim.get("direction"):
            add("assessment", "Assessment direction: " + claim["direction"].lower(), cid)
        for ref in claim.get("has_score") or []:
            score = nodes[ref][1]
            add("score", f"Reported {score['metric']}: {score['value']} ({score['score_kind'].lower()}). "
                + score["interpretation"], cid, ref)
        for ref in claim.get("has_evidence") or []:
            evidence = nodes[ref][1]
            if evidence.get("direction"):
                add("evidence_direction", "Evidence contribution: " + evidence["direction"].lower(), ref)
            add("interpretation", evidence.get("explanation"), cid, ref, *_refs(evidence, "source_claims"))
            add("evidence_context", evidence.get("context"), ref)
            for assumption in evidence.get("assumptions") or []:
                add("assumption", "Interpretation assumes: " + assumption, ref)

    conclusions = account.get("conclusion_claims") or []
    for cid in account["component_claims"]:
        if cid not in conclusions:
            claim_segments(cid, "claim")
    for cid in conclusions:
        claim_segments(cid, "conclusion")
    add("closing", account.get("closing_remarks"), account_id)
    return segments


def main():
    import json
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--account")
    parser.add_argument("--segments", action="store_true", help="show roles and source references as JSON")
    args = parser.parse_args()
    try:
        segments = render_account(yaml.safe_load(args.document.read_text()), args.account)
    except (ValueError, KeyError) as exc:
        parser.exit(1, f"Cannot render account: {exc}\n")
    print(json.dumps(segments, indent=2) if args.segments else " ".join(s["text"] for s in segments))


if __name__ == "__main__":
    main()
