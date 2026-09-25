"""Tests over schema/examples/*.yaml — the documents shipped as the model's
worked examples and rendered by the portal.

These are the files a reader is most likely to copy, and the ones the portal
embeds, so a stale identifier in one of them is a published lie rather than a
local mistake. That is exactly the failure this catches: PR #13 carried three
example ids that no longer matched their content.

Everything here is parametrized over a GLOB rather than a hardcoded list.
lint_identity.py checks four files out of eleven, and the stale
BioComputeObject id in example_graph.yaml sat outside that list for exactly
that reason. A new example is covered the moment it is added.
"""
from __future__ import annotations

import pytest
import yaml

from conftest import EXAMPLES
from dapper_identity import DOC_GROUPS, digest_of, verify

EXAMPLE_FILES = sorted(EXAMPLES.glob("*.yaml"))
# Documents that carry `id`-bearing nodes in DOC_GROUPS buckets, i.e. everything
# minting applies to. Single-class illustrations without an id are skipped.
GRAPH_FILES = [p for p in EXAMPLE_FILES
               if set(yaml.safe_load(p.read_text()) or {}) & set(DOC_GROUPS)]


def test_there_are_examples_to_check():
    """The globs actually matched something.

    Every other test in this file is parametrized over these two lists, and
    pytest reports an empty parametrization as a pass. Without this guard, a
    renamed directory or a broken glob would turn the whole file green while
    checking nothing at all.
    """
    assert EXAMPLE_FILES, "schema/examples/*.yaml is empty — did the glob break?"
    assert GRAPH_FILES


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: p.name)
def test_every_example_is_valid_yaml(path):
    """Each example parses at all.

    The cheapest possible check, run over every example including the
    single-class illustrations the later tests skip. A published example that
    does not parse is worse than no example.
    """
    assert yaml.safe_load(path.read_text()) is not None


@pytest.mark.parametrize("path", GRAPH_FILES, ids=lambda p: p.name)
def test_every_example_id_matches_its_content(path, sv):
    """Every node's identifier still hashes to the content beneath it.

    The most valuable test in the file. Editing an example without re-minting
    leaves an id that describes something else — and because the document still
    parses, still validates and still renders, nothing else notices. Caught the
    three stale ids in example_bottom_line_result.yaml and the
    BioComputeObject id in example_graph.yaml that 5ff61ad left behind.
    """
    doc = yaml.safe_load(path.read_text())
    problems = verify(doc, sv)
    assert problems == [], (
        f"{path.name} has stale identifiers — re-mint with schema/identity/mint.py:\n  "
        + "\n  ".join(problems)
    )


@pytest.mark.parametrize("path", GRAPH_FILES, ids=lambda p: p.name)
def test_every_example_node_id_is_a_well_formed_dapper_id(path):
    """Any id claiming our prefix is actually parseable as one.

    Complements the test above, which recomputes digests but says nothing about
    shape. External identifiers (orcid:, ror:) are deliberately skipped — they
    are authoritative and left alone by minting.
    """
    doc = yaml.safe_load(path.read_text())
    for group in set(doc) & set(DOC_GROUPS):
        for node in doc[group] or []:
            node_id = node.get("id")
            if node_id is None or not str(node_id).startswith("dapper:"):
                continue   # external identifiers (orcid:, ror:) are left as-is
            assert digest_of(node_id), f"{path.name}: malformed id {node_id!r}"


@pytest.mark.parametrize("path", GRAPH_FILES, ids=lambda p: p.name)
def test_no_duplicate_ids_within_an_example(path):
    """No identifier appears twice in one document.

    Two nodes sharing an id means either a copy-paste that was never re-minted,
    or genuinely duplicate content that should have been collapsed the way
    mint.py's deduplication does. Either way a reference to that id is ambiguous
    and the graph cannot be walked reliably.
    """
    doc = yaml.safe_load(path.read_text())
    seen: dict[str, str] = {}
    for group in set(doc) & set(DOC_GROUPS):
        for node in doc[group] or []:
            node_id = node.get("id")
            if node_id is None:
                continue
            assert node_id not in seen, f"{path.name}: {node_id} appears in {seen[node_id]} and {group}"
            seen[node_id] = group


@pytest.mark.parametrize("path", GRAPH_FILES, ids=lambda p: p.name)
def test_illustrative_list_points_at_nodes_that_exist(path):
    """`_illustrative:` names only nodes that are actually in the document.

    The portal reads this list to mark a node as drawn-for-shape rather than
    transcribed from a real run — the honesty distinction the examples exist to
    make. A dangling entry means some node the reader sees as real was meant to
    be labelled illustrative, and the id drifted out from under the list.
    """
    doc = yaml.safe_load(path.read_text())
    declared = doc.get("_illustrative") or []
    present = {n.get("id") for g in set(doc) & set(DOC_GROUPS) for n in doc[g] or []}
    dangling = [i for i in declared if i not in present]
    assert dangling == [], f"{path.name}: _illustrative names absent nodes {dangling}"
