# Gene-set collections, GMT rows, and file locations

The [corrected HuBMAP example](../examples/example_geneset_hubmap.yaml) adapts
the supplied `geneset.provenance.dapper-0.2.0-a0.yaml` export into 75 generic
Files, one original GMT C2M2File, three Activities, one GeneSetCollection, 358
individual GeneSets, and 79 reified edges (77 original and two for the GMT
export). The supplied `ryan-hubmap-example.gmt`
matches the source GMT's MD5 and byte size; its rows populate the individual
sets and reciprocal membership links. Other files' checksums and execution
records remain source-reported. Use the current development schema.

## File, collection, and individual set

- **File / C2M2File** describes the physical GMT: filename, location, checksums,
  and generation provenance. Use C2M2File when retaining C2M2 metadata.
- **GeneSetCollection** describes the library of named sets. Its `has_gmt_file`
  points to that file record. Its optional `members` lists GeneSet identifiers.
- **GeneSet** describes one named set of genes. `in_gmt_file` points to the file,
  and `gmt_entry` gives its exact first-column row name. These two fields must
  be supplied together. A readable `name` can differ from the row key;
  `alternate_identifier` can preserve source-system labels.

Collections and sets also link directly, independently of the GMT:

- `GeneSetCollection.members` lists the GeneSet IDs in the collection.
- `GeneSet.in_gene_set_collection` lists the GeneSetCollection IDs containing
  that set. It is multivalued because one set can belong to several collections.

Both directions are shown in the row-level example. These links are separate
from `has_gmt_file`, `in_gmt_file`, and `gmt_entry`, which locate representations.
Do not infer collection membership merely from two nodes pointing at one GMT.

Either direction may be omitted; existing forward-only documents remain valid.
A supplied collection `members` list must be complete and distinct. If both
directions are supplied, the linter checks that they agree, and rejects duplicate,
mistyped, or dangling local collection references. If only some individual sets
are described, omit the collection's complete `members` list and use their
backlinks; this does not claim that those sets enumerate the whole library.
Contained sets are reachable through either direction and are not counted as
independent terminal results when their collection is present in the document.

The collection's `members` contributes to its content-derived identity.
`in_gene_set_collection` is an **unhashable inverse link**: adding it leaves
the GeneSet ID unchanged and avoids circular collection/member hashes. Minting
and URI expansion still rewrite these pointers when their targets change.

`GeneSet.n_genes` counts distinct genes in that one set. For a collection,
`n_sets` (and `n_members`, if supplied) counts named sets, while `n_genes`
counts distinct genes across their union. A gene appearing in several rows
counts once. The old `GeneSet.n_sets` is deprecated and accepts only 1;
move library-level records to `gene_set_collections`.

The [small row-level example](../examples/example_geneset_collection_rows.yaml)
and its [GMT file](../examples/data/example_gene_sets.gmt) demonstrate two sets:
Gene1/Gene2 and Gene2/Gene3. Each set has `n_genes: 2`; the collection has
`n_sets: 2` and `n_genes: 3`. All records in this example are illustrative.
It is also available in the portal as **GMT collection and individual sets**.

Standard [GMT format](https://docs.gsea-msigdb.org/GSEA/Data_Formats/#gmt-gene-matrix-transposed-file-format-gmt)
is tab-delimited: **name, description, gene, gene, …**. Use `na` for an absent
description; the second column is not a gene. Row names must be unique within
the representation. The linter checks metadata and supplied membership counts;
it does not open GMT files or verify that a row actually exists. Collection
union counts are checked only when every member's gene list is supplied.

## GMT exports named by GeneSet ID

The [DAPPER-ID GMT export](../examples/data/hubmap_hz1.dapper-ids.gmt) replaces
the first column of every real HuBMAP HZ1 row with the corresponding
`dapper:GeneSet.<digest>` from the corrected provenance YAML. Its 358 row names
join directly to `GeneSet.id`; human-readable labels and original row names
remain in `name` and `alternate_identifier`. The description column, gene
symbols, row order, and line endings are unchanged from the source GMT.

The corrected YAML and this GMT form a matched pair: `gmt_entry` equals the
GeneSet's ID, while `in_gmt_file` and the collection's `has_gmt_file` point to a
new File record for the renamed bytes. This file has its own checksums and an
export Activity linked to the original GMT. Only the original GMT retains its
C2M2 registration metadata.

`GeneSet.in_gmt_file` and `GeneSet.gmt_entry` are **unhashable representation
locators**. Mint the GeneSets first, write their IDs into the GMT, then calculate
the GMT checksums and mint its File and the collection. Updating the GeneSets'
file pointers and selectors leaves their IDs stable, avoiding a checksum/row-ID
dependency cycle. Gene membership and other hashable content still determine
their IDs. This revises the identity policy for these two development-schema
slots; records previously minted with hashable GMT locators must be re-minted.

In the **compact corrected YAML**, row names join directly to `GeneSet.id`.
Expanding external references to full URIs can re-mint records under DAPPER-ID-1.
The expanded YAML retains `gmt_entry` literally: it must still select the row
actually present in the same GMT, even when the owning GeneSet's ID changes.
Always use `in_gmt_file` plus `gmt_entry` to locate a row. Generate a separate
GMT from the final expanded IDs if that serialization also needs ID-named rows.

## Changes to the HuBMAP example

- The collection is named **HuBMAP ASCT+B gene-set library (HZ1)**. Its original
  `unsigned_term_gene:2b85b11457a52cc7e0d6950a` label is retained in
  `alternate_identifier` (the schema's spelling).
- Only `genesets.gmt` retains C2M2File and its source `persistent_id` and
  `c2m2_uuid`. Other files use File, with C2M2-only identifiers removed.
- Filesystem paths moved from `local_id` into `location`, using the declared
  `humgen:` prefix. A location is mutable and excluded from file identity.
  The renamed GMT uses an `export:` prefix for the repository fixture at tag
  `0.2.0-a1`. To use a local copy instead, set `location` to that file's path or URI.
- `has_gmt_file` references the GMT record through its DAPPER content-derived
  ID. The GMT has **358 named sets and 964 distinct gene symbols**, with
  3,232 gene occurrences across rows. The original export reported 1,533 genes;
  that value does not match the GMT union, and its original scope is unspecified.
  The collection uses the verified union count, with this discrepancy documented
  in its description. The [GMT fixture](../examples/data/hubmap_hz1.genesets.gmt)
  preserves the supplied bytes; the original GMT record also includes their SHA-256.
- All 358 GeneSets have readable names, DAPPER row keys in `gmt_entry`, original
  source labels in `alternate_identifier`, gene members and counts, a pointer
  to the renamed GMT, and
  `in_gene_set_collection`. The collection enumerates those sets in `members`.
  The generating activity is inherited from the library's recorded production.
- Gene symbols use a document-local `HGNC.SYMBOL` prefix expanding to
  `https://identifiers.org/hgnc.symbol:` (the
  [HGNC Symbol namespace](https://bioregistry.io/registry/hgnc.symbol)). Symbols
  are retained exactly: no alias normalization or stable numeric HGNC-ID mapping
  is asserted, and current approval status has not been checked for every symbol.
- File and Activity names are readable; filenames and commands remain intact.
  The preparation description now matches the recorded HuBMAP ASCT+B command.
- Incorrect `dcc_url` and `drc_url` values were removed from this example.
  Those slots remain available; the linter does not ban them or enforce naming style.
- Changed metadata records and references were re-minted. The original input
  file is unchanged.

## Document-local prefixes

Put reusable namespace bases in a top-level `prefixes` mapping:

```yaml
prefixes:
  humgen: file:///humgen/
```

For example, `humgen:diabetes2/users/ryank/data/input.csv` expands to
`file:///humgen/diabetes2/users/ryank/data/input.csv`. This preserves the source
filesystem path; it is not a public HTTPS download link. A producer with a
real public namespace can instead declare its absolute URL base. Expansion
concatenates base and suffix exactly, so include the intended slash or hash.

The document's declarations supplement the prefixes in the selected schema
and its imports. Existing model prefixes cannot be rebound; repeating the
same binding is allowed. Prefix names are case-sensitive. Prefix bases must
be absolute URIs; prefix chains, relative paths, malformed declarations, and
unknown prefixes fail validation, including unused invalid declarations.

The linter checks node IDs, edge endpoints and predicates, URI-valued fields,
and class-valued references, including inlined records. It resolves mixed
CURIE/URI spellings before checking duplicate IDs, graph connectivity, and
endpoint classes. It does not reinterpret arbitrary strings: names, prose,
commands, filenames, enum codes, and opaque `alternate_identifier` values
retain their literal meaning. A filesystem path in an `uriorcurie` slot such
as `C2M2File.local_id` must be expressed as a URI or declared CURIE.
`File.location` and `Dataset.location` accept plain filesystem paths, but a
URI/CURIE value in those fields is resolved and checked too. URI export expands
`humgen:` locations to `file:///humgen/`; it leaves ordinary paths as paths.
Reserve `local_id` for an identifier assigned by a source system, rather than
using it as a storage-location field.

Recognized absolute URI schemes are `http`, `https`, `ftp`, `ftps`, `s3`,
`gs`, `drs`, `ipfs`, `ipns`, `file`, `urn`, `mailto`, and `tag`. Other
CURIE namespaces require a model or document declaration. Resolution is
syntactic and offline; it does not test URL accessibility or ontology membership.

## GMT reference identifiers

`GeneSetCollection.has_gmt_file` and `GeneSet.in_gmt_file` reference a local
File or subclass (including C2M2File) with a minted DAPPER identifier. Use
`dapper:File.<digest>` or the corresponding full URI, not a bare MD5/SHA-256
checksum or download URL. Location and checksums belong on the file record.
`has_gmt_file` is identity-bearing on its owner; the GeneSet representation
locators `in_gmt_file` and `gmt_entry` are excluded from identity.
For compatibility, `GeneSet.has_gmt_file` remains available for a whole-file
export of a single set; use `in_gmt_file` with `gmt_entry` for a shared GMT.

## Validate and export

```bash
uv run schema/lint/lint_provenance.py schema/examples/example_geneset_hubmap.yaml --strict
uv run schema/expand_prefixes.py schema/examples/example_geneset_hubmap.yaml \
  --output geneset-hubmap.expanded.yaml
uv run schema/lint/lint_provenance.py geneset-hubmap.expanded.yaml --strict
```

The reusable Python function is `document_prefixes.expand_document(document,
schema_view)`, with `schema/` on the Python path. It returns a new document;
the CLI requires a separate output path, validates the result, and writes
nothing if resolution or validation fails. `--schema` pins the model version,
and `--profile` overrides end-modality detection.

The output uses absolute URIs in identifier/reference fields and omits the
now-unneeded `prefixes` map. External node IDs retain their expanded external
identity. Existing DAPPER records are re-minted and their references rewritten:
DAPPER-ID-1 hashes literal content, so expanding an external CURIE in a
hashable slot can change its digest. Full DAPPER record URIs are recognized
as content-addressed references, with the same scalar digest substitution as
their `dapper:` forms. Original compact-document hashes remain checked against
their original serialized values, not a silently normalized copy.

Use a consistent prefix map when publishing compact documents: changing a
document namespace binding changes its interpretation, even when the literal
CURIE is unchanged. For a self-contained identity representation, publish the
expanded export, whose hashable external references include their absolute URIs.
