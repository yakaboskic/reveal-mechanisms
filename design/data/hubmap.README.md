# Corrected HuBMAP HZ1 provenance and matching GMT

Send `geneset.provenance.corrected.yaml` together with `genesets.dapper-ids.gmt`.
Every one of the 358 GeneSets has `gmt_entry` equal to its `id` and to the exact
first-column row name in that GMT. `in_gmt_file` points to the renamed GMT's
File record, and the collection's `has_gmt_file` points to the same record.

`genesets.gmt` preserves the original supplied GMT bytes. The renamed export
preserves columns 2 onward, row order, and line endings byte-for-byte. Readable
set names remain in `name`; original GMT labels remain in `alternate_identifier`.
Neither original input in Downloads was modified.

## Files and provenance

- One GeneSetCollection contains all 358 GeneSets through `members`. Each set
  links back through `in_gene_set_collection` and lists its source gene symbols.
- The original GMT remains the sole C2M2File, retaining its `persistent_id`,
  `c2m2_uuid`, supplied MD5, and byte size. Its SHA-256 was calculated from the
  supplied bytes. The 74 other source/intermediate files use File.
- The renamed GMT is a new File, with its own MD5, SHA-256, and byte size.
  An export Activity consumes the original GMT and generates the renamed file.
  C2M2 registration identifiers were not copied onto these changed bytes.
- The original 77 reified edges are retained with updated node references;
  two edges describe this additional export. The bundle has 438 nodes and 79 edges.
- Paths moved from `local_id` to `location`. Incorrect example `dcc_url` and
  `drc_url` values were removed; the schema still supports those fields.

Original GMT SHA-256: `42a9f83defef91894d50937a2f8f0bf275d5f8a26183734d7a8954b2fa382700`

DAPPER-ID GMT SHA-256: `498996ebd67791c0a296d03f718b613c52876669176a1405b124778375983bda`

## Identity and row selectors

Use the current DAPPER development schema. `GeneSet.in_gmt_file` and
`GeneSet.gmt_entry` are now unhashable representation locators. The GeneSets
were minted first, their IDs written into the GMT, then its checksums and File
ID computed. Updating the locators leaves the GeneSet IDs stable, avoiding a
circular dependency through the file checksum. Earlier GeneSet IDs were
re-minted under this revised policy, and all dependent references updated.

`GeneSetCollection.members` and `has_gmt_file` remain hashable. The inverse
`GeneSet.in_gene_set_collection` remains unhashable. Both membership directions
are present and consistent.

`geneset.provenance.expanded.yaml` is the full-URI form. Expanding hashable gene
references can change GeneSet IDs. Its `gmt_entry` values intentionally retain
the literal first-column names in the supplied `genesets.dapper-ids.gmt`.
Locate rows using `in_gmt_file` plus `gmt_entry`; only the compact corrected
YAML promises that `gmt_entry` also equals the owning GeneSet's ID.

## Counts and gene identifiers

The supplied GMT matches the original export's 37,069-byte size and base64 MD5
`S9JxYl+5WUf+XBaE06+v+w==`. It contains 358 distinct sets, 964 distinct gene-symbol
strings, and 3,232 gene occurrences, with no repeated symbol within a row.
The collection therefore has `n_sets: 358`, `n_members: 358`, and `n_genes: 964`.
The original reported `n_genes: 1533` has an unspecified scope and does not
match this GMT union; that discrepancy remains in the collection description.

`HGNC.SYMBOL` expands to `https://identifiers.org/hgnc.symbol:`. Source symbols
are preserved without alias normalization or a numeric HGNC-ID crosswalk;
current approval status has not been checked for every symbol. The namespace
is documented at https://bioregistry.io/registry/hgnc.symbol .

`humgen: file:///humgen/` describes the original source filesystem. `export:`
points to the local directory containing this bundle. When moving the bundle,
update the compact YAML's `export:` prefix to its new directory and regenerate
the expanded YAML, or update the expanded File's `location` directly. File
locations are excluded from identity. These locations are not public downloads.

## Validation

From the DAPPER repository:

    uv run schema/lint/lint_provenance.py /path/to/geneset.provenance.corrected.yaml --strict
    uv run schema/lint/lint_provenance.py /path/to/geneset.provenance.expanded.yaml --strict

Both files pass strict linting with zero errors and warnings. The matched GMT
checksums and every row selector and gene list are also checked directly in the
example's regression tests. The generic linter does not open referenced GMT
files. Other source file bytes and execution records remain source-reported.

Original YAML SHA-256: `9caa469abf7eeb09d1bb9188c82a542ed6ce9fb84d65333a59cfd9bcc678b32f`
