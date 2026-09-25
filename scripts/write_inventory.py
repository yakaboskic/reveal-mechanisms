#!/usr/bin/env python3
"""Generate a compact inventory from the validated snapshot manifests."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    dismech = json.loads((ROOT / "data/dismech/manifest.json").read_text())
    cfde = json.loads((ROOT / "data/cfde/manifest.json").read_text())
    t2d = json.loads((ROOT / "data/cfde/t2d/manifest.json").read_text())
    content = f"""# Extracted data inventory

Generated from the source manifests. Mechanism/factor snapshot: 2026-09-15; knowledge-gap and integration inventory: 2026-09-24.

## CFDE: {cfde['model']}

- **{cfde['factor_records']:,} factors** from **{cfde['successful_queries']:,}/{cfde['phenotype_model_keys']:,}** advertised phenotype queries.
- Complete: `{str(cfde['complete']).lower()}`; failed queries: {len(cfde['errors'])}; restricted queries: {cfde['restricted_queries']}.
- Query retrieval range: `{cfde.get('query_retrieved_at_min')}` through `{cfde.get('query_retrieved_at_max')}`.
- [Factors](../data/cfde/factors.jsonl.gz): one JSON record per line, gzip compressed. Each record has a composite ID, query phenotype key, original source row, source URL, and retrieval time.
- [Manifest](../data/cfde/manifest.json): scope, success counts, checksum, and source anomalies.
- [Factor keys](../data/cfde/factor-keys.json): complete source key catalog, including other model keys for discovery. Only v2 factors were downloaded.
- [Index catalog](../data/cfde/indexes.json) and [OpenAPI](../data/cfde/openapi.json): source build timestamps, positional keys, and transport contract.
- One source capitalization anomaly is retained explicitly; details are in the manifest and API discovery document.

### T2D response fixtures

These are complete available responses for each recorded query, not a full biomedical graph or a claim of comprehensive source-model coverage.

"""
    for q in t2d["queries"]:
        label = q["file"].removesuffix(".json")
        content += f"- [{label}](../data/cfde/t2d/{q['file']}): {q['row_count']:,} rows.\n"
    content += f"""
These BioIndex membership queries return zero rows because their model catalogs advertise only `cfde`. The September 24 [interactive API inventory](interactive-api-inventory.md) separately verifies membership edges under a `cfde-inc-v2` request. Exact BioIndex gene-set and joined-context queries are recorded separately. See [query recipes and limitations](api-discovery.md).

## DisMech

Source commit: `{dismech['source_commit']}`. Source KB/schema had local changes: `{str(dismech['source_has_local_changes']).lower()}`.

- **{dismech['yaml_files']:,} KB YAML files** parsed; complete: `{str(dismech['complete']).lower()}`; errors: {len(dismech['errors'])}.
- **{dismech['mechanism_records']:,} mechanism occurrences**, **{dismech['unique_mechanism_labels']:,} distinct normalized mechanism labels**, and **{dismech['causal_edges']:,} downstream edges**.
- **{dismech['ontology_terms']:,} observed ontology IDs**, **{dismech['vocabulary_entries']:,} lexical entries**, and **{dismech['enum_definitions']:,} enum definitions / {dismech['enum_values']:,} declared values**.

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
"""
    gap_root = ROOT / "data/dismech-gaps/2026-09-24"
    gaps = json.loads((gap_root / "manifest.json").read_text())
    content += f"""

## September 24: knowledge gaps and integration inventory

**Website count reconciliation:** the public Discussions Browser currently serves **149 discussions**, including **127 `KNOWLEDGE_GAP`** and **17 `HUMAN_MODEL_MISMATCH`** records (144 in its combined gap facet). Its `data.js` is byte-identical to the checked-in browser asset last updated June 13, 2026. Our extraction reads the September 15 repository's YAML directly. Restricting that YAML to the browser exporter's disorder/module scope gives **4,033 discussions and 2,593 `KNOWLEDGE_GAP` records**; including groupings adds 18 discussions, 11 of them knowledge gaps. Thus the 2,604 count below is an all-KB YAML snapshot count, not the public website count. See [captured comparison](../data/dismech-gaps/2026-09-24/browser-reconciliation.json) and [public browser data](https://dismech.monarchinitiative.org/app/discussions/data.js). HTTP deployment modification time does not establish when this static dataset was regenerated.

- **{gaps['discussions']:,} discussions** extracted from **{gaps['yaml_files']:,} YAML files** at commit `{gaps['source_commit']}`.
- **{gaps['knowledge_gaps']:,} gap records**: {gaps['gaps_by_kind']['KNOWLEDGE_GAP']:,} `KNOWLEDGE_GAP` and {gaps['gaps_by_kind']['HUMAN_MODEL_MISMATCH']:,} `HUMAN_MODEL_MISMATCH`, preserving source kinds.
- Status: {gaps['gaps_by_status']['OPEN']:,} OPEN, {gaps['gaps_by_status']['RESOLVED']:,} RESOLVED, {gaps['gaps_by_status']['null']:,} unspecified. Missing status remains null.
- **{gaps['attachments']:,} attachment references**: {gaps['attachment_resolution']['resolved']:,} resolved, {gaps['attachment_resolution']['whole_section']:,} whole sections, {gaps['attachment_resolution']['whole_document']:,} whole documents, {gaps['attachment_resolution']['ambiguous_target']:,} ambiguous targets. Ambiguities are retained for review.
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
"""
    gene_root = ROOT / 'data/cfde-genesets/2026-09-24'
    genes = json.loads((gene_root / 'manifest.json').read_text())
    load_path = gene_root / 'database-load.json'
    db = json.loads(load_path.read_text()) if load_path.exists() else {}
    db_status = (f"Verified **{db['loaded_rows']:,} persisted aliases** in `{db['database']}`."
                 if db.get('complete') else 'Database import is not yet recorded as complete; consult the load report.')
    content += f"""

## September 24: CFDE → DAPPER GeneSets

- **{genes['encoded_rows']:,} GeneSet objects**, covering every advertised `pigean-gene-set/2` key for `{genes['model']}`; export complete: `{str(genes['complete']).lower()}`.
- {db_status}
- [Encoding manifest](../data/cfde-genesets/2026-09-24/manifest.json), [encoded records](../data/cfde-genesets/2026-09-24/records.jsonl.gz), [database load report](../data/cfde-genesets/2026-09-24/database-load.json), and [HuBMAP example](../data/cfde-genesets/2026-09-24/example-hubmap.dapper.json).
- [DAPPER dependency snapshot](../data/cfde-genesets/2026-09-24/dapper/snapshot.json) pins the actual schema and identity implementation used. This is the dependency for the original catalog import. The newer [DAPPER integration audit](dapper-integration.md) captures gap context/classification, Paragraph citation spans, and citation metadata validators now implemented upstream. Source conversion and citation services remain application work; historical GeneSet payloads and pins stay unchanged.
- All records have actual DAPPER digests, exact model/source aliases, readable names, and catalog-encoding provenance. Membership, original scientific construction history, and unverified assay/species/build/count metadata are not loaded.
- [Import script and reproduction guide](geneset-import.md) describe collection, validation, resumable database writes, immutable objects, and alias revisions for future enrichment.

The database's GeneSet tables are implemented. The existing mechanism/factor/gap exports still await their own database migrations and imports; GeneSet completion does not imply those collections or embeddings are loaded.
"""
    (ROOT / "docs/data-inventory.md").write_text(content)
    print("Wrote docs/data-inventory.md")


if __name__ == "__main__":
    main()
