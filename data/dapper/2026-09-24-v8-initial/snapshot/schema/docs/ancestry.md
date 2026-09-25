# Ancestry context for genetic analyses

`AncestryEnum` provides nine source-compatible DIG codes with readable labels
and ontology mappings. Dataset and Activity gain an optional, single-valued
`ancestry` field through the `AncestryContext` mixin. The field records the
source-reported population grouping represented by a Dataset or targeted by an
Activity. It does not establish participant classification methods or
individual genetic ancestry proportions.

The field is hashable: changing ancestry changes the record's DAPPER ID.
Records without ancestry remain valid and retain their existing IDs. Omit the
field when unknown; do not use `Mixed` as a missing-value code. Values are
case-sensitive. Importers must explicitly map other systems' codes and verify
their definitions before translating them (for example, `AFR` is not an
additional accepted spelling of `AF`).

This feature targets the current development schema; pin a schema commit or
release that includes it. Sources below were checked on 2026-09-21.

## Evidence from the bucket and producer

Anonymous listing of `s3://dig-open-bottom-line-analysis/` returned
`bottom-line/` and `input-sumstats/`. Under `bottom-line/` the folders were:

```text
AA/  AF/  EA/  EU/  GME/  HS/  Mixed/  SA/  SSAF/
```

The object `bottom-line/AA/AF.sumstats.tsv.gz` exists in this production
bucket. Only its key was checked; no content or checksum comparison with
the supplied staging-bucket object was performed.

The [DIG portal formatter](https://github.com/broadinstitute/dig-dug-portal/blob/859ebdf777d498dab583bb5d49b952ccf167e946/src/utils/formatters.js#L27-L48)
defines the codes, and the [DIG registry enum](https://github.com/broadinstitute/data-registry-api/blob/3da2b0ee5fb43bd810ba6de9947fd74f41b941a1/dataregistry/api/model.py#L64-L83)
contains these and additional values. The
[export implementation](https://github.com/broadinstitute/dig-aggregator-methods/blob/42fdbc8a8d60ef4b3f3042d4bc22d2fe50a176ea/bottom-line/src/main/resources/openDataTransfer.py#L23-L47)
writes `bottom-line/{ancestry}/{phenotype}.sumstats.tsv.gz`. For `Mixed` it
selects the pipeline's `trans-ethnic` outputs instead of an ancestry partition.
Thus the folder code and filename describe different dimensions: `AA` is
ancestry context; `AF` in this filename is the phenotype label.

## Initial vocabulary

Use [HANCESTRO](https://ebispot.github.io/hancestro/) as the primary external
vocabulary. The identifiers below were checked against its
[2025-10-14 release content](https://github.com/EBISPOT/hancestro/blob/main/hancestro.obo),
which was the content served by the default branch at review time. All listed
terms were active. These mappings are based on the producer's labels;
they do not independently validate the cohort assignments.

| Code | DAPPER display label | Mapping |
| --- | --- | --- |
| `AA` | African American / Afro-Caribbean | `meaning`: [HANCESTRO:0016](http://purl.obolibrary.org/obo/HANCESTRO_0016) |
| `AF` | African, unspecified | `meaning`: [HANCESTRO:0010](http://purl.obolibrary.org/obo/HANCESTRO_0010) |
| `EA` | East Asian | `meaning`: [HANCESTRO:0009](http://purl.obolibrary.org/obo/HANCESTRO_0009) |
| `EU` | European | `meaning`: [HANCESTRO:0005](http://purl.obolibrary.org/obo/HANCESTRO_0005) |
| `GME` | Middle Eastern / North African / Persian | `meaning`: [HANCESTRO:0015](http://purl.obolibrary.org/obo/HANCESTRO_0015) |
| `HS` | Hispanic / Latin American, as reported | `close_mappings`: [HANCESTRO:0014](http://purl.obolibrary.org/obo/HANCESTRO_0014) |
| `Mixed` | Multiple ancestry groups | No equivalent ancestry term asserted |
| `SA` | South Asian | `meaning`: [HANCESTRO:0006](http://purl.obolibrary.org/obo/HANCESTRO_0006) |
| `SSAF` | Sub-Saharan African | `meaning`: [HANCESTRO:0011](http://purl.obolibrary.org/obo/HANCESTRO_0011) |

`AA` includes Afro-Caribbean groups in the producer's definition. Restricting
it to African American would lose information. `AF` is deliberately
unspecified; it must not silently replace `AA` or `SSAF`.

`HS` needs a qualified mapping. HANCESTRO:0014 describes Latin or Admixed
American ancestry, while HANCESTRO:0612 describes Hispanic or Latin ethnicity.
The source label alone does not establish the participant classification
method. Retain `HS` as the reported analysis grouping and use `close_mappings`
to 0014; do not equate ethnicity with genetically inferred ancestry.

`Mixed` is an analysis grouping, not a biological ancestry category. In this
export it means results across ancestry groups; it must not be mapped to
HANCESTRO:0306 (admixed ancestry). The contributing groups remain discoverable
through the input Datasets when supplied. Do not invent their composition or
assume every ancestry folder contributed. Unknown ancestry is omitted, not
encoded as `Mixed`.

## The AF / AA example

The [complete example](../examples/example_bottom_line_af_aa.yaml) has
`ancestry: AA` on seven Datasets and five Activities. This includes the final
Dataset, the two partitions, the ancestry-specific staging and three result
collections, and the five explicitly AA-scoped analysis/publication stages.
The staging reconstruction remains labeled as inferred. Other inputs and
Activities retain no ancestry assertion: a suffix in a dataset name does not
establish scope, and an Activity can filter or combine its inputs.

These excerpts show the fields on the corresponding records; the complete
example includes their minted IDs and lineage:

```yaml
datasets:
  - name: Published bottom-line summary statistics for AF / AA
    resource_type: dataset
    ancestry: AA
    trait: KPN.TRAIT:0000096

activities:
  - name: Bottom-line ancestry-specific METAL for AF / AA
    activity_type: AncestrySpecificStage
    ancestry: AA
    trait: KPN.TRAIT:0000096
```

The final Dataset connects to its File distribution through `has_file`.
Scientific context can be queried through that link; File and DrsObject have
no ancestry field. The example retains the supplied staging-bucket location.
The production listing above does not demonstrate identical file content or
verify a publication execution.

The separate `trait` field identifies atrial fibrillation using the KPN
catalog; see [trait identity in the bottom-line guide](bottom-line-results.md#trait-identity).
Ancestry and trait are independent dimensions of the analysis scope.

## Validate a document

Generic DAPPER validation checks the enum values, cardinality, graph shape,
lineage, and computed identifiers:

```bash
uv run schema/lint/lint_provenance.py schema/examples/example_bottom_line_af_aa.yaml
```

For DIG exports, an opt-in command runs the same validation and then checks
storage-path consistency without accessing S3:

```bash
uv run schema/lint/lint_dig_ancestry.py schema/examples/example_bottom_line_af_aa.yaml
```

The additional check recognizes only the production and staging
`dig-open-bottom-line-analysis` buckets and their
`bottom-line/{ancestry}/[phenotype.sumstats.tsv.gz]` layout. It rejects unknown
folder codes and conflicts with a Dataset's declared ancestry, checking both
its own `location` and its linked File locations. Inline `has_file` and reified
`HasFile` edges are equivalent. It does not infer missing metadata, change
values, inspect filenames for ancestry, or apply the convention to other
buckets or internal pipeline layouts.

For example, a Dataset with `ancestry: EU` linked to the AA export is an error
under this check. An ancestry-omitted Dataset remains valid. An unknown folder
such as `AFR` is reported even if ancestry was omitted, so new source categories
require a reviewed vocabulary update.

After editing ancestry, re-mint the complete graph and update references:

```bash
uv run schema/identity/dapper_identity.py assign path/to/result.yaml
```

## Ontology links and extension

The model navigator exposes the enum, slot, and mixin through
`dapper_enum:AncestryEnum`, `dapper_slot:ancestry`, and
`dapper_class:AncestryContext`. Its enum page links meanings to HANCESTRO and
shows qualified mappings separately, including the HS close mapping.
Documentation CURIEs identify DAPPER definitions; HANCESTRO CURIEs identify
the external concepts. A `close_mappings` link does not assert equivalence.

For finer African population descriptions, use
[AfPO through HANCESTRO's imported population hierarchy](https://ebispot.github.io/hancestro/#african-populations-in-hancestro).
A later extension can carry specific population CURIEs and classification
methods/reference panels. These details should not be inferred from the broad
labels or from storage paths. Reference-panel populations, such as individual
1000 Genomes groups, are distinct from the categories in this enum.

The nine values cover the observed output bucket, not every human population
or all DIG registry values. Add other categories and ontology crosswalks after
reviewing their definitions. Reporting status, individual admixture, and
ancestry proportions require separate fields if those use cases arise.
