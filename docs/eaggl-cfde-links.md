# EAGGL → CFDE application links

The application mapping uses **exact trait + factor number**. Labels, top genes,
scores, loadings, and biological agreement are deliberately ignored. An EAGGL
`AD::Factor1` is routed to the CFDE `cfde-inc-v2` record for `(AD, Factor1)`.

This is the **initial application retrieval path** in the [current plan](design-plan.md#initial-crosswalk-backed-retrieval-contract). Reuse the loaded EAGGL label embeddings, join mapped candidates before selecting five suggestions, and dispatch the returned native CFDE IDs. Full current-model embeddings and improved correspondence can follow later. The [implementation handoff](implementation-handoff.md) covers the API adapter and frozen request bindings still to build.

The source capture and CFDE catalog are pinned in each mapping run. The current
catalog has 18,419 CFDE factors; the source EAGGL import has 4,037. This mapping
finds 1,756 exact keys. The remaining 2,281 source factors are recorded as unmatched:
832 are on traits absent from the CFDE catalog and 1,449 have an unavailable factor
number. No missing CFDE identifier is fabricated.

## Use the populated mappings

Loaded into `cyaka_reveal_mechanisms` on September 25, 2026:
[load report](../data/eaggl-cfde-mapping/2026-09-25/database-load.json).
The run ID is `272cfa19d093257863d7e7134776229dbc9b9b018d974e6b311285686833ec31`.
The [post-commit verification](../data/eaggl-cfde-mapping/2026-09-25/database-verification.json)
confirmed all 4,056 resolved references reach existing GeneSet objects, covering
1,052 mapped factors. Existing EAGGL and DAPPER GeneSet counts were unchanged.

The CLI shares the existing root `.env` and Python import dependencies:

```bash
# Read-only plan using the already imported EAGGL factors and GeneSet aliases.
.venv/bin/python scripts/link_eaggl_cfde.py load

# Create migration 004's tables and atomically load the mapping.
# Rerunning verifies existing rows without duplicating them.
.venv/bin/python scripts/link_eaggl_cfde.py load --apply \
  --report data/eaggl-cfde-mapping/2026-09-25/database-load.json

# Read a factor's CFDE node ID and immediately available GeneSet links.
.venv/bin/python scripts/link_eaggl_cfde.py lookup --factor-id 'AD::Factor1'
```

The default CFDE snapshot is `data/cfde/manifest.json` plus `factors.jsonl.gz`.
Its checksum, model scope, completeness, unique keys, and factor-number syntax
are validated. Set `--cfde-catalog` to use a new snapshot. The EAGGL factors and
GeneSet aliases are read from the selected database. Use `--eaggl-import-id` and
`--gene-set-import-id` if more than one eligible import exists, and `--run-id`
for lookups when multiple mapping runs exist.

## Application lookup

```python
from reveal_backend.eaggl_cfde_links import lookup_factor

mapped = lookup_factor(connection, "AD::Factor1", run_id=mapping_run_id)
if mapped["status"] == "matched":
    cfde_node_id = mapped["cfde_node_id"]
    gene_sets = [g for g in mapped["gene_sets"] if g["dapper_id"] is not None]
```

`cfde_node_id` has the interactive API's form
`factor:<trait_group>:<trait>:cfde-inc-v2:FactorN`. It can be passed directly to the
existing CFDE evidence collector/interactive graph flow for live expansion.
The lookup returns both the EAGGL display label and CFDE metadata, so the app can
keep the colleague's labels while routing through CFDE IDs.

For application use, explicitly configure a completed mapping run and its source/embedding imports. Persist that run and the resolved IDs with each saved selection/request. New mapping runs apply to new drafts; historical jobs retain their original bindings. Existing search/suggestion endpoints can return the native CFDE `EagglFactor` representation; the worker keeps the original EAGGL hit and routing provenance separately. The first selectable corpus is the mapped subset. Unmatched source factors remain stored and available to source tooling.

An unmatched result has `status="unmatched"`, a reason, and an empty `gene_sets`
list. A matched factor can still have gene-set references that are absent from
the imported GeneSet catalog; these have `dapper_id=null` and
`status="not_in_gene_set_catalog"`. This does not prevent the factor mapping.

## Gene-set coverage

The initial gene-set links use the **CFDE factor metadata's ranked `top_gene_sets`**
field, not the EAGGL top-gene-set list. There are 8,780 such references (five per
matched factor), of which 4,056 resolve to 3,733 distinct existing DAPPER GeneSet
objects. The other 4,724 references retain their exact source keys for future
resolution. No placeholder DAPPER objects are created.

These are summary associations; the complete factor/gene-set loading matrix is
not bulk-imported by this command. Retrieve additional relationships through the
existing CFDE API using the mapped factor ID. Source top-list order is stored as
`gene_set_rank`; it is not a loading or confidence score.

## Tables and Prisma

Migration `schema/migrations/004_eaggl_cfde_links.sql` adds:

- `eaggl_cfde_link_runs`: source import IDs, target model, matching rule, pinned
  catalog manifest, counts, and explicit unmatched IDs.
- `eaggl_cfde_factor_links`: EAGGL factor foreign key, CFDE node ID, trait,
  factor number/group, and complete CFDE catalog payload.
- `eaggl_cfde_gene_set_links`: ranked CFDE summary references, with foreign keys
  to existing `cfde_gene_set_aliases` when resolved.

The migration requires the existing GeneSet/EAGGL tables from 001 and 002. It does
not apply the separate pending DisMech migration. All mapping rows commit in one
transaction, protected by an advisory lock. A failed transaction can be retried;
a completed run is read back and verified on replay.

The Prisma models are `EagglCfdeLinkRun`, `EagglCfdeFactorLink`, and
`EagglCfdeGeneSetLink`. For example:

```ts
const factor = await prisma.eagglCfdeFactorLink.findUnique({
  where: { run_id_factor_index: { run_id: runId, factor_index: factorIndex } },
  include: {
    run: true,
    gene_set_links: {
      orderBy: { gene_set_rank: "asc" },
      include: { alias: { include: { dapper_objects: true } } },
    },
  },
});
if (factor?.run.status !== "complete") throw new Error("Mapping unavailable");
```

This creates database routing and retrieval helpers. It does not deploy a REST
endpoint or change the existing frontend study.

## Verification

The new tests cover matching despite entirely different genes/labels, unmatched
traits/numbers, model scope, duplicate keys, catalog integrity, real MySQL alias
resolution, atomic rollback/retry, idempotence, and read-back corruption detection.
The updated Prisma schema validates, generates a client, and has no differences
from the corresponding MySQL table definitions.

```bash
.venv/bin/python -m unittest discover -s services/backend/tests \
  -p 'test_eaggl_cfde_links.py'
```

The three MySQL tests are opt-in using `REVEAL_TEST_MYSQL_PORT`, a disposable
localhost database named `cyaka_dismech_test`, and
`REVEAL_TEST_MYSQL_PASSWORD` (default `local-test-only`).
