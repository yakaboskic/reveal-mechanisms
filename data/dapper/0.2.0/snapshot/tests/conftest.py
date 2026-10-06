"""Shared fixtures.

`schema/converter/` and `schema/identity/` are standalone PEP-723 scripts, not
an installed package — the same reason portal/build.py reads DOC_GROUPS out of
the source text instead of importing it. Tests do import them, so their
directories go on sys.path here rather than in every test module.

The SchemaView is session-scoped: building it parses the whole of dapper.yaml
and dominates the runtime of anything that touches identity.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "schema" / "dapper.yaml"
EXAMPLES = REPO_ROOT / "schema" / "examples"
FIXTURES = Path(__file__).parent / "fixtures"
HZ2 = FIXTURES / "geneset-hubmap-hz2"

sys.path.insert(0, str(REPO_ROOT / "schema" / "identity"))
sys.path.insert(0, str(REPO_ROOT / "schema" / "converter"))
sys.path.insert(0, str(REPO_ROOT / "schema" / "lint"))
sys.path.insert(0, str(REPO_ROOT / "schema"))

from dapper_identity import load_schema

@pytest.fixture(scope="session")
def sv():
    """SchemaView over schema/dapper.yaml."""

    return load_schema(SCHEMA)


@pytest.fixture(scope="session")
def hz2_payload() -> dict:
    """The real dig.geneset provenance payload for HuBMAP gene set HZ2."""
    return json.loads((HZ2 / "geneset.provenance.json").read_text())


@pytest.fixture(scope="session")
def hz2_graph(hz2_payload) -> dict:
    """The single {nodes, edges} ProvenanceGraph inside that payload."""
    graphs = [v for v in hz2_payload.values() if isinstance(v, dict) and "nodes" in v]
    assert len(graphs) == 1, f"fixture shape changed: {len(graphs)} graphs"
    return graphs[0]


@pytest.fixture(scope="session")
def hz2_meta() -> dict:
    return json.loads((HZ2 / "geneset.meta.json").read_text())


@pytest.fixture(scope="session")
def example_docs() -> dict[str, dict]:
    """Every schema/examples/*.yaml, parsed, keyed by filename."""
    return {p.name: yaml.safe_load(p.read_text()) for p in sorted(EXAMPLES.glob("*.yaml"))}
