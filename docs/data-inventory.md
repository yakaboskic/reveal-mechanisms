# Extracted data inventory

Generated from the source manifests. Mechanism/factor snapshot: 2026-09-15; knowledge-gap and integration inventory: 2026-09-24.

## CFDE: cfde-inc-v2

- **18,419 factors** from **3,778/3,778** advertised phenotype queries.
- Complete: `true`; failed queries: 0; restricted queries: 0.
- Query retrieval range: `2026-09-15T16:46:22.602139+00:00` through `2026-09-15T16:54:55.910829+00:00`.
- [Factors](../data/cfde/factors.jsonl.gz): one JSON record per line, gzip compressed. Each record has a composite ID, query phenotype key, original source row, source URL, and retrieval time.
- [Manifest](../data/cfde/manifest.json): scope, success counts, checksum, and source anomalies.
- [Factor keys](../data/cfde/factor-keys.json): complete source key catalog, including other model keys for discovery. Only v2 factors were downloaded.
- [Index catalog](../data/cfde/indexes.json) and [OpenAPI](../data/cfde/openapi.json): source build timestamps, positional keys, and transport contract.
- One source capitalization anomaly is retained explicitly; details are in the manifest and API discovery document.

### T2D response fixtures

These are complete available responses for each recorded query, not a full biomedical graph or a claim of comprehensive source-model coverage.

- [pigean-factor](../data/cfde/t2d/pigean-factor.json): 2 rows.
- [pigean-gene-phenotype](../data/cfde/t2d/pigean-gene-phenotype.json): 18,321 rows.
- [pigean-gene-set-phenotype](../data/cfde/t2d/pigean-gene-set-phenotype.json): 5,000 rows.
- [pigean-gene-factor-Factor1](../data/cfde/t2d/pigean-gene-factor-Factor1.json): 541 rows.
- [pigean-gene-set-factor-Factor1](../data/cfde/t2d/pigean-gene-set-factor-Factor1.json): 597 rows.
- [pigean-gene-factor-Factor2](../data/cfde/t2d/pigean-gene-factor-Factor2.json): 502 rows.
- [pigean-gene-set-factor-Factor2](../data/cfde/t2d/pigean-gene-set-factor-Factor2.json): 467 rows.
- [pigean-gene](../data/cfde/t2d/pigean-gene.json): 6,682 rows.
- [pigean-gene-gene-sets](../data/cfde/t2d/pigean-gene-gene-sets.json): 0 rows.
- [pigean-gene-set-genes](../data/cfde/t2d/pigean-gene-set-genes.json): 0 rows.
- [pigean-gene-set](../data/cfde/t2d/pigean-gene-set.json): 82 rows.
- [pigean-joined-gene-set](../data/cfde/t2d/pigean-joined-gene-set.json): 262 rows.
- [pigean-joined-gene](../data/cfde/t2d/pigean-joined-gene.json): 167 rows.

These BioIndex membership queries return zero rows because their model catalogs advertise only `cfde`. The September 24 [interactive API inventory](interactive-api-inventory.md) separately verifies membership edges under a `cfde-inc-v2` request. Exact BioIndex gene-set and joined-context queries are recorded separately. See [query recipes and limitations](api-discovery.md).

## DisMech

Source commit: `df3884a8bba34370ecc0b3a49d956e7f0523bb28`. Source KB/schema had local changes: `false`.

- **3,361 KB YAML files** parsed; complete: `true`; errors: 0.
- **19,959 mechanism occurrences**, **18,387 distinct normalized mechanism labels**, and **37,149 downstream edges**.
- **21,382 observed ontology IDs**, **54,690 lexical entries**, and **138 enum definitions / 1,192 declared values**.

### Files

- [Entities](../data/dismech/entities.jsonl.gz): disorder/module/grouping and other KB document identities, mappings, and classifications.
- [Mechanisms](../data/dismech/mechanisms.jsonl.gz): source pathophysiology records, their original fields/evidence, and document/pointer provenance.
- [Causal edges](../data/dismech/causal-edges.jsonl.gz): downstream records and edge-specific evidence; target references are retained, not globally resolved.
- [Mechanistic hypotheses](../data/dismech/hypotheses.jsonl.gz): hypothesis-group metadata associated with KB documents.
- [Ontology terms](../data/dismech/ontology-terms.jsonl.gz): observed identifiers, labels, and source occurrences.
- [Lexical vocabulary](../data/dismech/vocabulary.jsonl.gz): mechanism names, descriptors, references, and source roles for chip search.
- [Schema vocabularies](../data/dismech/schema-vocabularies.json): main-schema/local-import enums, declared values, dynamic constraints, and prefixes.
- [T2D mechanisms](../data/dismech/t2d-mechanisms.json): readable fixture with nine mechanism occurrences and their evidence.
- [Source file hashes](../data/dismech/source-files.json), [manifest](../data/dismech/manifest.json), and [source license](../data/dismech/SOURCE-LICENSE.txt).

### Scope distinctions

DisMech's disorder/module records, grouping navigation, and hypothesis-assessment documents are retained as separate source kinds. The picker should use mechanism occurrences and their relevant descriptors; the broader vocabulary export is available for later crosswalk work. A lexical occurrence count is not a count of biologically distinct mechanisms.

The extraction preserves assertions from an AI-curated, human-reviewed source; it does not independently revalidate scientific claims, ontology membership, or quoted literature. Observed terms are not complete external ontologies. Evidence snippets and opposing evidence are retained as source data. The source repository was read without modifying it.

## Reproduce and inspect

```bash
python3 -m pip install -r scripts/requirements.txt
python3 scripts/extract_dismech.py --source ~/src/research/dismech
python3 scripts/pull_cfde.py --scope all-factors --model cfde-inc-v2
python3 scripts/pull_cfde.py --scope t2d --model cfde-inc-v2
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/validate_snapshots.py
python3 scripts/write_inventory.py
```

The source downloader needs network access. Complete responses are cached in `data/cfde/raw/` (ignored by Git); rerunning in the same output directory resumes that capture. For a genuinely fresh source snapshot, use a new `--output` directory so previously cached queries are not mistaken for refreshed data. Derived compressed exports and manifests are retained in the repository workspace.

Read a compressed export without a database:

```python
import gzip, json
with gzip.open("data/cfde/factors.jsonl.gz", "rt") as stream:
    t2d_factors = [json.loads(line) for line in stream
                   if '"phenotype_key": "T2D"' in line]
```

No REVEAL REST service, agent job, or embedding corpus is running yet. The supplied embedding client is now a backend module, and the GeneSet importer creates/populates the project database as recorded below. The [design plan](design-plan.md) describes the next implementation stages.


## September 24: knowledge gaps and integration inventory

**Website count reconciliation:** the public Discussions Browser currently serves **149 discussions**, including **127 `KNOWLEDGE_GAP`** and **17 `HUMAN_MODEL_MISMATCH`** records (144 in its combined gap facet). Its `data.js` is byte-identical to the checked-in browser asset last updated June 13, 2026. Our extraction reads the September 15 repository's YAML directly. Restricting that YAML to the browser exporter's disorder/module scope gives **4,033 discussions and 2,593 `KNOWLEDGE_GAP` records**; including groupings adds 18 discussions, 11 of them knowledge gaps. Thus the 2,604 count below is an all-KB YAML snapshot count, not the public website count. See [captured comparison](../data/dismech-gaps/2026-09-24/browser-reconciliation.json) and [public browser data](https://dismech.monarchinitiative.org/app/discussions/data.js). HTTP deployment modification time does not establish when this static dataset was regenerated.

- **4,051 discussions** extracted from **3,361 YAML files** at commit `df3884a8bba34370ecc0b3a49d956e7f0523bb28`.
- **3,367 gap records**: 2,604 `KNOWLEDGE_GAP` and 763 `HUMAN_MODEL_MISMATCH`, preserving source kinds.
- Status: 2,526 OPEN, 3 RESOLVED, 838 unspecified. Missing status remains null.
- **6,422 attachment references**: 6,329 resolved, 79 whole sections, 4 whole documents, 10 ambiguous targets. Ambiguities are retained for review.
- [Gaps](../data/dismech-gaps/2026-09-24/knowledge-gaps.jsonl.gz), [all discussions](../data/dismech-gaps/2026-09-24/discussions.jsonl.gz), [attachments](../data/dismech-gaps/2026-09-24/gap-attachments.jsonl.gz), and [manifest](../data/dismech-gaps/2026-09-24/manifest.json).
- [AIP example](../data/dismech-gaps/2026-09-24/aip-gap-example.json) preserves the supplied gap and resolves its three mechanisms and one phenotype.
- [Interactive API fixtures](../data/interactive/2026-09-24/) preserve exact requests, response payloads, timestamps, and raw-body hashes. See [the API inventory](interactive-api-inventory.md) for tested coverage and limitations.
- [MySQL readiness](database-readiness.md) records the initial read-only audit and subsequent GeneSet database import. The [earlier DAPPER working-tree audit](../data/dapper/schema-audit-2026-09-24-v3.json) is retained as history; the GeneSet dependency snapshot below captures the newer schema files.

The [Proto-OKN inventory](agent-evidence-integration.md) and [embedding module](../services/backend/README.md) record the selected integrations and implementation status; [design v8](design-plan.md) adopts the current DAPPER contracts.

Reproduce gaps and validate local integration fixtures:

```bash
python3 scripts/extract_dismech_gaps.py --source ~/src/research/dismech --output data/dismech-gaps/NEW_CAPTURE
python3 scripts/validate_revision_inventory.py
```

The validator checks the delivered September 24 capture, including attachment integrity, catalog-to-factor matches, and graph endpoint closure after anchor merging. It makes no external requests.


## September 24: CFDE → DAPPER GeneSets

- **801,934 GeneSet objects**, covering every advertised `pigean-gene-set/2` key for `cfde-inc-v2`; export complete: `true`.
- Verified **801,934 persisted aliases** in `cyaka_reveal_mechanisms`.
- [Encoding manifest](../data/cfde-genesets/2026-09-24/manifest.json), [encoded records](../data/cfde-genesets/2026-09-24/records.jsonl.gz), [database load report](../data/cfde-genesets/2026-09-24/database-load.json), and [HuBMAP example](../data/cfde-genesets/2026-09-24/example-hubmap.dapper.json).
- [DAPPER dependency snapshot](../data/cfde-genesets/2026-09-24/dapper/snapshot.json) pins the actual schema and identity implementation used. This is the dependency for the original catalog import. The newer [DAPPER integration audit](dapper-integration.md) captures gap context/classification, Paragraph citation spans, and citation metadata validators now implemented upstream. Source conversion and citation services remain application work; historical GeneSet payloads and pins stay unchanged.
- All records have actual DAPPER digests, exact model/source aliases, readable names, and catalog-encoding provenance. Membership, original scientific construction history, and unverified assay/species/build/count metadata are not loaded.
- [Import script and reproduction guide](geneset-import.md) describe collection, validation, resumable database writes, immutable objects, and alias revisions for future enrichment.

The database's GeneSet tables are implemented. The existing mechanism/factor/gap exports still await their own database migrations and imports; GeneSet completion does not imply those collections or embeddings are loaded.
