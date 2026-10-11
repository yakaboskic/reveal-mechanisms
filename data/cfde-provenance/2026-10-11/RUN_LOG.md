# Run log: cfde-collection-lineage-2026-10-11

Database `cyaka_reveal_mechanisms` (shared reference tables). Generation `ec3364ae1dfdeeead0110dd8129a16aa03dbd90b11400108e274f8d9ded92c8a` (KPN `eaggl-capped-v1`). Active on local and QA; production serves no KPN generation.

## Plan (read-only), 2026-10-11

`plan_sha256` = `e1ae6daec714a61f5b6ca7a0a493ddff62ccb836064bca13ba3ad8ebdbc06034`

| Library | Collections | Edges |
|---|---|---|
| GTEx | 80 add | 160 |
| HuBMAP | 1 add | 2 |
| LINCS_L1000 | 2 add | 4 |
| MoTrPAC | 13 add | 14 |
| LIGER | 37 skip (unresolved) | 0 |

Verification:
- 96 collections validated.
- 96 representative gene sets stayed exact in evidence packages and gained their inputs.
- 111 shared nodes were identical across collections.

## Preview (read-only, `--overlay`) on QA accounts citing EAGGL Mechanisms

All Mechanisms bound by `document_identity`.

| Account | Mechanisms | Traces before → after | Datasets | Organizations |
|---|---|---|---|---|
| `…DBlEf4HAEEiEdzju59LMJK_iuOqo2O16` | 3 (74/76/63 projections) | 0 → 51 | 6 | 8 |
| `…EmL6BtOpVEuAipovYs7IBQI3XVF7vQIj` | 1 (50) | 0 → 18 | 3 (LINCS, Harmonizome, MoTrPAC) | 3 |
| `…H7W2s1LBHlj-pqYxBSzDP_XGOfUBZ7a3` | 5 (51–83) | 0 → 55 | 6 | 8 |

- **Responses:** 185–842 KB.
- **Coverage stays `incomplete`:** evidence items cite captured Files, which have no lineage.

## Apply, 2026-10-11T21:09:11Z

```
apply --apply --expect-plan e1ae6daec714a61f5b6ca7a0a493ddff62ccb836064bca13ba3ad8ebdbc06034
```

- **Rows:** 96 collections updated and read back.
- **Backup:** `.runtime/cfde-lineage/20261011T210911648069Z/payloads.jsonl.gz`, sha256 `875a050b40b2f1110b498db67a14070039a6d57e3b2832ba4721a1aa665c50fe`.
- **Post-apply:**
  - the preview without overlay equals the overlay preview for all three accounts, snapshot ids included;
  - a re-plan reports every patched collection `unchanged`.
