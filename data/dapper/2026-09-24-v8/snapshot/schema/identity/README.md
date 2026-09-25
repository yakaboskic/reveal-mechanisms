# `schema/identity` — DAPPER-ID-1 computed identifiers

Every DAPPER Node gets a content address:

```text
dapper:Activity.b4IAIg_sR6RfE5ZFf0zU_BNbfpz5A_2g
       ^^^^^^^^ ^-------------------------------
       class    32-char sha512t24u digest
```

The digest covers what the object **is** and nothing else. Re-running a pipeline, re-signing a
nanopublication, mirroring an artifact, or moving a workspace does not change its identifier.
Changing the analysis, the content, or the author order does.

```bash
uv run schema/identity/dapper_identity.py assign  schema/examples/example_geneset_graph.yaml
uv run schema/identity/dapper_identity.py verify  schema/examples/example_geneset_graph.yaml
uv run schema/identity/dapper_identity.py digest  schema/examples/example_dataset.yaml --class Dataset
uv run schema/identity/dapper_identity.py verify-vectors
uv run schema/identity/lint_identity.py
```

Importable too: `from dapper_identity import compute_id, compute_digest, assign_ids, verify`.

For a graph using document-local prefixes, `schema/expand_prefixes.py` exports
absolute URI references and re-mints affected DAPPER IDs. The full
`https://broadinstitute.github.io/dapper/ns#Class.digest` form is recognized
alongside `dapper:Class.digest`; existing compact-form test vectors are unchanged.
External CURIE expansion can change hashable literal values, so it must update
IDs and dependent references together. See the
[gene-set authoring guide](../docs/geneset-authoring.md#validate-and-export).

---

# Start here: you have a pile of gene sets and want IDs

One command. Point it at your runs.

```bash
uv run schema/identity/mint.py /path/to/your/genesets -o collection.yaml
```

That walks the folder for every `geneset.provenance.json`, converts them, merges them into one
collection, mints an ID for every object, and writes the YAML. It also works on an `s3://` prefix, a
single `.json`, or an existing collection you want to re-mint.

Then check it:

```bash
uv run schema/identity/dapper_identity.py verify collection.yaml
```

**`24 node(s) checked, 0 mismatch(es)` means every ID matches the content it names.**

Read the node count, not just the mismatch count. A document with no grouped node
lists — a single instance such as `example_nanopub.yaml` — has nothing to check, and
saying `0 mismatch(es)` on its own would make "I found no problems" and "I looked at
nothing" identical. That case now reports `0 node(s) checked` instead.

### The one rule: never write an ID by hand

Leave `id` out of your source data. An identifier is *computed from* the content it names, so a
hand-written one can drift out of sync with what it points at and start lying. `mint.py` fills them
in, and tells you if it had to replace hand-written ones.

This is why you can throw away IDs and regenerate them at any time without losing anything. They
aren't data; they're a function of the data.

### What you can rely on

**Re-running changes nothing.** Same input, byte-identical output. So if an ID *does* change, the
content changed — that's a signal worth reading, not noise. Diff the YAML to see what moved.

**Timestamps don't count.** Re-run the same analysis tomorrow and every ID is the same, because
`generated_at_time` is excluded from the digest. Change a parameter and the ID changes, because
`command` isn't.

**Duplicates collapse for free.** Twenty gene sets that all used `human_gene_info` produce the same
digest for it, so the merged collection carries one copy, not twenty. `mint.py` reports how many
collapsed. Verified: minting the same run twice collapsed 12 of 24 nodes.

**Two labs get the same answer.** Anyone with this repo and the same content computes the same
identifier. Nothing is registered, allocated, or looked up.

### When something looks wrong

| symptom | what it means |
|---|---|
| `verify` reports a mismatch | the content was edited after minting — re-run `mint.py` |
| an ID changed and you didn't expect it | some hashable field changed; diff the YAML |
| two objects got the same ID | their content is genuinely identical — that's dedup, not a bug |
| `No geneset.provenance.json found` | point it at the folder your runs were written to, not the repo |
| `N node(s) unminted` / `unrecognised node group(s)` | a node list is under a group key the minter doesn't know — usually a typo (`gene_set:` for `gene_sets:`). Check it against `DOC_GROUPS`; minting refuses rather than half-minting the document |

---

## This is not a Trusty URI

`schema/trusty-identifiers.md` defines two deliberately separate paths. This package is **path 2**
("a new, versioned content-ID profile"). Path 1 — real nanopublication `RA` Trusty URIs — is a
shell-out to `nanopub-java` at publication time and lands in `Nanopublication.trusty_uri`.

That document is emphatic and we follow it: *"Do not reimplement or approximate `RA`."* Nothing here
is called `RA`, and `dapper:` digests are not interchangeable with Trusty URIs. A Trusty URI proves
content integrity for a *published nanopublication package*; a `dapper:` id names *one DAPPER Node*.

## The profile

| | |
|---|---|
| **Profile name** | `DAPPER-ID-1` (recorded on `HashableNode` as `dapper:id_profile`) |
| **Identifier form** | `dapper:{ClassName}.{digest}` — the full LinkML class name |
| **Semantic scope** | one DAPPER Node instance — not a publication package, graph, or file's bytes |
| **Input model** | `schema/dapper.yaml` (pre-release, unversioned) |
| **Included** | slots marked `mixins: [hashable]`, plus the class name |
| **Excluded** | slots marked `mixins: [unhashable]`, `id`, and nulls |
| **Canonicalization** | RDF → `rdflib.compare.to_canonical_graph` → N-Triples → **sorted lines** → `\n`-joined → UTF-8 |
| **Hash** | SHA-512, leftmost 24 bytes, base64url — GA4GH `sha512t24u`, 32 chars, unpadded |
| **Self-reference** | subject fixed to `urn:dapper:self`; `id` is never an input |
| **Nested references** | a `dapper:` reference contributes its **bare digest**, as GA4GH VRS requires |
| **List order** | preserved — the index is part of the predicate |
| **Blank nodes** | none emitted; the canonicalizer is a safety net |
| **Complexity limit** | 10,000 triples |
| **Verification** | `verify` recomputes and compares; `verify-vectors` replays permanent vectors |
| **Successors** | a changed artifact gets a new id; link the old one with `Supersedes` |

### Why the class name and not a two-letter code

GA4GH VRS uses 2–4 character prefixes (`ga4gh:VA.…`) because it mints identifiers at genomic scale
and runs a central registry through TASC. Neither applies to 27 classes. Full names cost ~20
characters and buy: uniqueness for free (LinkML already enforces unique class names — derived
two-letter codes collide for 8 of our 27, e.g. `Award`/`AgenticWorkspace`), self-describing
identifiers, and no registry to maintain or drift.

### Why predicates come from slot names, not `slot_uri`

Measured against the real schema: 133 of 243 hashable slots declare no `slot_uri` at all, and
`dcc_url` and `drc_url` **both** map to `schema:url` in four classes. Keying the digest on `slot_uri`
would make those two fields indistinguishable, so swapping their values would not change the
identifier. Slot names are unique within a class by construction, and decoupling identity from
ontology mappings means re-mapping a slot to a better URI later does not churn the whole corpus.

## What the identifier actually names

A `dapper:` id names **the metadata record**, not the underlying bytes. This is the "identity scope"
question `trusty-identifiers.md` insists be settled up front: *"Does an ID name a claim, a
publication package, an entire provenance graph, a file? These should not share one undifferentiated
identifier type."*

The practical consequence is worth stating plainly, because it looks like a bug the first time you
see it: the curated `schema/examples/example_geneset_graph.yaml` and the raw output of
`schema/converter/geneset_to_dapper.py` describe the **same real gene set** and produce **different
`dapper:` ids**. That is correct. The example gives the gene set a readable title
(`HuBMAP ASCT+B augmented marker gene-set library (model HZ2)`) where the converter emits the
machine name (`unsigned_term_gene:402cf4a1…`), and the converter carries machine fields the curated
example omits. Different descriptions of the same thing are different records, so they get different
addresses.

If you need to know whether two records describe the same *bytes*, compare `File.md5` /
`File.sha256` (also inherited by `C2M2File`) — those address the file. The `dapper:` id
addresses what DAPPER says about it. Generic files use the `files` document group.

Moving a file (`File.location`) or adding DRS access (`File.drs_representation`) does
not change its ID. These fields are unhashable. `C2M2File.local_id` remains hashable
because it is the source system's identifier. Changing a record's class from `File`
to `C2M2File` changes its ID, even when it describes the same bytes.

## Three traps, all found the hard way

**1. `to_canonical_graph` does not sort its output.** It canonicalizes blank-node labels, but the
serializer emits lines in arbitrary order. The same instance built in two insertion orders produced
two different digests until the sort was added. Without it, identifiers are silently
non-reproducible across machines. `canonical_bytes()` carries the sort and a comment saying so.

**2. A class with no hashable slots collides with itself.** `NanopubSignature` was initially marked
unhashable wholesale, which left its digest covering only the class name — so every signature in the
corpus got the same identifier. The rule that fixes it: **`unhashable` belongs on the reference from
a stable object to a volatile one, not on the volatile object's own content.** A nanopublication does
not depend on its signature (`has_signature_element` is unhashable), but a signature *is* its bytes
(`has_signature` is hashable). Lint check 3 enforces a non-empty digest input.

**3. RDF triples are a set, so lists lose their order.** Emitting each element of `has_creator`
against the same predicate silently discards order, and DataCite author order is citation-bearing.
The element index is therefore part of the predicate (`…slot:has_creator[0]`). Reordering a creator
list **does** change the identifier — that is intended, and vector `dataset-creators-reversed` locks
it.

## What is `unhashable`, and why

| Category | Slots | Rationale |
|---|---|---|
| Temporal | `Activity.generated_at_time`, `AgenticWorkspace.last_run`, `MirrorProvenance.sync_time`, `Nanopublication.created` | Re-running something does not make it a different thing |
| Signature | `Nanopublication.has_signature_element` | A signature attests to content; it cannot be part of what it attests to |
| Mirror-observed | `ProvenancedResource.has_mirror_provenance` | A mirror's observation must not change the artifact's identity (the mirroring invariant) |
| Location | `AgenticWorkspace.workspace_url`, `platform` | Where work can be re-run is not what the work is |
| Back-reference | `Claim.asserted_in`, `NanopubPublicationInfo.pubinfo_of`, `NanopubProvenance.provenance_of`, `NanopubSignature.has_signature_target` | Carries nothing the forward edge doesn't — **and breaks the reference cycles** |

Those back-references are load-bearing. The nanopublication structure is genuinely cyclic
(`Claim → Nanopublication → NanopubAssertion → Claim`), so bottom-up digest computation is
impossible until one direction is excluded. Lint check 4 re-verifies the graph is a DAG so a future
back-reference cannot silently reintroduce the problem.

Gene-set collections use the same pattern: `GeneSetCollection.members` is
hashable, while the inverse `GeneSet.in_gene_set_collection` is unhashable.
Adding a collection backlink leaves the set ID unchanged. Editing a member's
content remints the set and dependent collection, and the minter rewrites both
directions to the new IDs. Membership consistency is checked by the provenance
linter separately from digest verification.

`GeneSet.in_gmt_file` and `GeneSet.gmt_entry` are also unhashable: they locate
a serialized row without changing the set's content. This allows a GMT to use
the GeneSet's own ID as its row name. Mint the sets, serialize the GMT, compute
the file's checksums, then mint its File and the collection; rewriting the sets'
file pointers leaves their IDs stable. `has_gmt_file` remains hashable. These
two locator exclusions revise the development schema's earlier hash policy,
so records minted under that policy need to be re-minted once.

`gmt_entry` is a literal selector, not a graph reference. URI expansion leaves
it unchanged so it still matches the physical GMT, even if expansion of gene
identifiers changes the GeneSet ID.

Assessment status and score references are hashable on Claim. Reassessment changes
the Claim and any dependent ScientificAccount; it leaves the assessed Proposition
and optional MechanisticModel unchanged. Paragraph points to its account, so edits
to wording change only the expression, not the scientific content.

## Test vectors are permanent

`test_vectors.json` is a fixture, not a snapshot. From `trusty-identifiers.md`: *"Keep test vectors
forever. Every supported profile needs fixed positive and negative vectors so future library upgrades
cannot silently change IDs."*

If a dependency bump breaks them, the identifiers already published broke too. **Fix the code, never
the vectors.** If the algorithm must genuinely change, that is `DAPPER-ID-2` under a new CURIE prefix
(`dapper2:`), never a silent rehash under `dapper:`.

The file also carries the GA4GH cross-check: `sha512t24u(b"ACGT")` must equal
`aKF498dAxcJAqme6QYQ7EZ07-fiw8Kw2`, verbatim from the VRS spec. That proves our digest primitive is
byte-compatible with theirs.

## Known gap

The identifier carries no in-band algorithm version. `trusty-identifiers.md` recommends the opposite
(*"Put the canonicalization/hash profile in the ID"* — Trusty's `RA` is exactly that), while GA4GH
VRS omits it and its own spec carries the resulting apology: *"there is no guarantee that VRS
computed identifiers will remain stable across major version releases."* We followed the agreed
`dapper:{ClassName}.{digest}` shape and pinned the profile out-of-band on `HashableNode`. A future
algorithm mints `dapper2:`. Worth revisiting before the first identifiers are published externally —
it is cheap now and expensive later.
