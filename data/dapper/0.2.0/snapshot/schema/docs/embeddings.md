# Embeddings: vectors for any node, stored as rows of a shared matrix

An `Embedding` is a fixed-length numeric vector that a model computed from text
derived from another DAPPER node: a `GeneSet`'s name and metadata, a
`GeneSetCollection`'s description, a `Dataset` abstract, a `Claim`, and so on.
The class records **what** was embedded, **how**, and the **resulting bytes**,
while the vector itself lives outside the document in a shared matrix file.
The [illustrative example](../examples/example_embedding_graph.yaml) embeds the
two-row gene-set collection and the collection itself into a 3 x 4 float16 matrix.

## Records

- **Embedding** (`embeddings:` group). `embedding_of` names the represented
  node. `embedding_model`, `embedding_model_version`, `embedding_provider`,
  `dimensions`, `dtype` (`EmbeddingDtypeEnum`), `normalization`
  (`EmbeddingNormalizationEnum`), `embedded_text`, `text_template` and
  `vector_sha256` describe the computation and its result. `was_generated_by`
  points at the Activity that ran the model.
- **File** for the matrix. Its `description` states the array layout (for
  example "NumPy `.npy`, N rows x D dimensions, float16, C order"); `filename`,
  checksums and `size_in_bytes` are hashable as for any file, `location` is not.
  A second File can describe a row index (`rows.tsv`) that maps rows back to
  node ids and texts; it is provenance, not something an Embedding references.
- **Activity** for the run: `activity_type: text_embedding`, software name and
  version, the replay `command` (model, template, dtype), `code_version`, and an
  unhashable `generated_at_time`. Under `--strict` the linter expects every
  Activity to have used something; point a `used_edges` entry at the data the
  texts were built from (a GMT File, a Dataset), **not** at the embedded nodes.

## Identity

Hashable: `embedding_of`, model fields, `dimensions`, `dtype`, `normalization`,
`embedded_text`, `text_template`, `vector_sha256`, `was_generated_by`, `name`.
Two runs that produce different floats are different embeddings; the same text
embedded on behalf of a different node is a different embedding.

Unhashable: `has_vector_file` and `vector_row` (where the row lives), and the
target node's `has_embedding` list. Moving the matrix, re-packing rows, or
attaching an embedding to a node changes no identifier. `has_vector_file` and
`vector_row` must be supplied together (schema rule), like
`GeneSet.in_gmt_file` and `gmt_entry`.

`vector_sha256` is the hex SHA-256 of the row's bytes in `dtype`, little-endian,
C order — with NumPy, `hashlib.sha256(matrix[row].tobytes()).hexdigest()` for a
matrix loaded in that dtype.

## Links in both directions

`Embedding.embedding_of` is the hashable forward link; `has_embedding` is the
unhashable inverse, available on every `Node`, and may be reified as a
`HasEmbedding` edge (`has_embedding_edges`). Supplying both directions is
recommended so that the linter's reachability walk finds the Embedding, its
matrix File and its Activity from the embedded node. Embedding a node does not
make it upstream of anything: `dapper:embeddingOf` is not a derivation
predicate, so a `GeneSet` with embeddings remains a terminal result under the
`geneset` profile.

## Minting order

1. Mint the embedded nodes as usual (GeneSets, collection).
2. Compute the vectors, write the matrix file, checksum it, mint its File.
3. Mint the embedding Activity, then each Embedding (`vector_sha256` from the
   stored row bytes, `has_vector_file` + `vector_row` set).
4. Fill in `has_embedding` on the embedded nodes. Their ids do not change.

Or write everything with temporary ids and run
`uv run schema/identity/dapper_identity.py assign document.yaml`, which orders
the work by hashable references.

## Matrix file conventions

A single matrix across many collections is the intended use: one `.npy`
(rows x dimensions, one dtype), one `rows.tsv` with at least `row`, `node_id`,
`text_template` and `text`, and one matrix File record shared by every
document whose Embeddings point into it. Keep the matrix immutable once
referenced; a re-embedding produces a new file, new File record and new
Embedding records (their `vector_sha256` values change), while the embedded
nodes keep their ids.
