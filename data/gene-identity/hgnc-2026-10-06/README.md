# Pinned HGNC identity source

These are the complete approved and withdrawn HGNC downloads captured on 2026-10-06 from the HGNC public download links. The gzip containers are deterministic; decompressing restores the original TSV bytes. `manifest.json` pins both compressed and original hashes, source URLs, source modification dates and the mapping revision.

Official documentation: https://www.genenames.org/download/statistics-and-files/

HGNC is the human authority. This crosswalk covers `NCBITaxon:9606` only. It does not establish the organism of an EAGGL import. Callers must provide explicit taxon scope; aliases, previous symbols, withdrawn entries, ambiguous and unmapped inputs are reported separately. Only a unique approved exact symbol or identifier has `verified_identity`; aliases are candidates for explicit review. Withdrawn replacement assertions are preserved, never promoted automatically.

No source calls occur during resolution. Replaying a result requires the exact `mapping_revision`; unavailable revisions fail instead of silently substituting newer mappings. Each candidate includes the source TSV hash and record number. To update, acquire both official files as a new snapshot, preserve their original bytes and update the module revision in a separately reviewed change. Old captures retain their original mapping revision and result bytes.

HGNC makes its data freely available without access or use restrictions; attribution is requested in its download documentation. The source release here is a checksum-pinned download snapshot, not an invented upstream release number.
