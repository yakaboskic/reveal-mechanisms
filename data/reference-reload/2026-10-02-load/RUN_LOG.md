# Reference reload — additive load round

- Code: chase/reference-reload @ 89a79a9acab844e5f0c8b7b83a8448cdaf50f150 (pushed to origin)
- Host: dig-ae-dev-03; operator: cyakabos
- Load window (UTC): 2026-10-02T08:15:04Z → 2026-10-02T08:17:03Z; db_load_cmd took 119 s

| step | result | file |
|---|---|---|
| preflight (before) | ok, 0 blockers; Aurora 3.10.3 / MySQL 8.0.42 writer, TLS, all grants, lock free, no migration-008 tables | preflight.before.json |
| QA readyz (before) | 200 ready; mapping 272cfa19…, embedding run d4c03009…, DisMech 4062563d…, 1,756 mapped factors | qa_readyz.before.json |
| build | generation ec3364ae1dfd…; 711 / 4,037 / 133 / 44,399 / 278,272 | eaggl_capped__cfde_2026_09_28.db_build.json |
| embed | 44,532 vectors; 32 calibration probes, min cosine 0.99999998; space cfcd9d08a23b… = run d4c03009 config | eaggl_capped__cfde_2026_09_28.db_embed.json |
| load dry run | pinned import a548ad80… / run d4c03009…; embedding space matches; migration not applied | load.dryrun.json |
| load --apply | complete; legacy generation 16e78f6a1282… registered; readback: vectors checked, 4,037 factors' projection counts, 64+64 sampled rows | eaggl_capped__cfde_2026_09_28.db_load.json |
| preflight --compare | ok; only the 10 migration-008 tables are new; no source or source-count changes | preflight.after.json |
| status | both generations complete; prod/qa/local/compose legacy, no active pointer, no gate | status.after.json |
| QA readyz (after) | 200 ready; sources unchanged | qa_readyz.after.json |

Rollback while no capture/snapshot has run: `reference_reload abandon --generation ec3364ae1dfdeeead0110dd8129a16aa03dbd90b11400108e274f8d9ded92c8a [--drop-empty-schema] --apply`.
Next: cutover round (merge origin/main, deploy merged code in legacy mode, S3 + Upstash write token, rehearsal tables, records backups, then capture/snapshot/plan/apply per target).
