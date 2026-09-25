# Portable evidence-package fixture

`cad-builder-v1/` preserves the exact collected `reveal.evidence-package/0.2-draft` output used by the consolidated API handoff, including source artifacts and the original manifest. No identifiers, capture times or source bytes have been rewritten. The API generator copies this immutable fixture into its portable examples; generated API output is not its own source of truth.

This is a bounded live CFDE/DisMech capture: 11 successful requests, 17 retained nodes and 16 edges at limit 8. It has no external KG evidence and is not a recorded agent execution. The 12-claim HTML account is a separate authored fixture with illustrative KG assertions and an older Mechanism projection; it is not the accepted output of this package.

Validate with `python scripts/evidence_package_schema.py validate data/evidence-packages/cad-builder-v1/evidence-package.yaml`. The schema validates 45 embedded DAPPER objects. `manifest.json` records the exact package/file hashes. The earlier full collector/replay input tree lives in the ignored local `data/evidence-captures/cad-builder-v1` directory; this portable output alone is not a replay input. For a new capture/replay, follow `docs/evidence-package-builder.md` from the repository root.
