# Nanopublication stable identifiers: what to reuse

**Status:** DAPPER research recommendation  
**Date:** 2026-08-05  
**Scope:** Trusty URIs, nanopublication identity, and application to DAPPER

## Executive recommendation

Nanopublications use **Trusty URIs**. The identifier is not a random stable ID
and not a hash of a particular TriG file. It is a **versioned content address**
calculated from a canonical representation of the nanopublication's RDF dataset.
A typical identifier is:

```text
https://w3id.org/np/RAwUp0SmZZwQNOY1zbSPhR21aQoImiUyQrDlyXj5QYXmQ
                        ^^-------------------------------------------
                        RA       43-character SHA-256 value
```

For DAPPER, use one of two deliberately separate paths:

1. **Nanopublication interoperability:** generate the four named RDF graphs and
   use `nanopub-java` to sign, mint, and verify a real `RA` Trusty URI. Do not
   reimplement or approximate `RA`.
2. **General DAPPER artifact identity:** define a new, versioned content-ID
   profile over canonical JSON or canonical RDF. Reuse the Trusty URI design
   principles, but do not call the result `RA` unless it implements the Trusty
   URI RA specification byte for byte.

The first path is the recommendation for the existing DAPPER
`Nanopublication` class. The second is appropriate if datasets, hypotheses,
provenance bundles, or MCP responses need content addresses without becoming
nanopublications.

## What the identifier guarantees

A verified Trusty URI establishes:

- **Content integrity:** changing any hashed quad changes the identifier.
- **Serialization independence:** equivalent RDF in TriG, N-Quads, JSON-LD,
  and other RDF syntaxes can produce the same identifier.
- **Self-verification:** a copy obtained from an untrusted mirror can be checked
  against the identifier without trusting that mirror.
- **Immutable citation:** corrections receive new identifiers and link to the
  previous nanopublication with supersession or retraction statements.

It does not by itself establish:

- who created the content;
- whether a scientific claim is true;
- whether the identifier will resolve on the Web; or
- whether two differently scoped publication packages express the same claim.

Nanopublications address the first item with a separate digital signature and
the third with `w3id.org` redirects plus replicated registries. The **Trusty URI
proves content integrity; the RSA signature proves control of a signing key**.
These are complementary guarantees, not one mechanism.

## The exact `RA` algorithm

The Trusty URI version-1 specification defines module `RA` for an RDF dataset,
possibly containing multiple named graphs:

1. Represent the content as RDF quads: graph, subject, predicate, object.
2. Do not retain blank nodes. The `RA` specification requires them to be
   skolemized consistently before hashing.
3. Support self-reference by replacing occurrences of the artifact code in
   IRIs with one space during hash preprocessing. This breaks the otherwise
   circular dependency between content and its own content-derived URI.
4. Sort statements deterministically by graph, subject, predicate, and object,
   using the specification's Unicode and literal ordering rules.
5. Build the digest input by appending graph, subject, predicate, and object,
   each followed by LF. IRIs are emitted directly. Literals have specified
   datatype/language prefixes and escaping rules.
6. UTF-8 encode the resulting character sequence and hash it with SHA-256.
7. Encode the 256-bit digest as URL-safe Base64 without padding. This produces
   43 characters.
8. Prefix the digest with the module/version code `RA`, producing a
   45-character artifact code.
9. Append the artifact code to a URI after a non-Base64 delimiter, commonly
   `https://w3id.org/np/` for nanopublications.

The module code is essential. It commits the identifier to a canonicalization
and hashing contract, allowing future algorithms to use a different module
identifier without silently changing the meaning of existing IDs.

### Why ordinary file hashing is insufficient

Hashing a TriG, YAML, or JSON file directly makes whitespace, prefix choices,
key order, and formatting part of identity. Two files carrying the same RDF
statements would then have different IDs. `RA` hashes the RDF dataset's meaning
at the quad level instead of one serialization's bytes.

### Scope matters

A nanopublication consists of the head, assertion, provenance, and publication
information named graphs. Its Trusty URI identifies that whole publication
package, not only the assertion. A changed creator, creation time, license,
provenance statement, or signature-related statement can therefore produce a
new nanopublication ID even when the scientific assertion is unchanged. This
is intentional: the identifier names a publication act with its context.

## How nanopublications remain usable over time

The hash is only one layer of stability:

| Layer | Mechanism | Result |
|---|---|---|
| Identity | `RA` Trusty URI | Exact immutable content is self-verifying |
| Resolution | `https://w3id.org/np/...` | Redirect target can move without changing citations |
| Availability | Independent registries and mirrors | No single content host is authoritative |
| Authorship | Digital signature in publication info | A key holder can be verified separately from content integrity |
| Evolution | New nanopub plus supersession/retraction links | History remains addressable; old content is not overwritten |

This distinction is important for DAPPER's mirror invariant. A mirror cannot
substitute modified authoritative provenance under the same verified Trusty
URI. It may append information only by publishing a separate object with a new
identifier and an explicit relationship to the original.

## Applying this to DAPPER

The current model already has the right conceptual boundary:

- `Nanopublication` is the immutable publication act.
- `Proposition` and optional `MechanisticModel` describe scientific content.
- `Claim` records an attributed assessment; `ScientificAccount` organizes an account.
- successor/support/retraction relationships connect immutable publications.
- the four nanopublication graph classes provide the RDF dataset to hash.

The example at `schema/examples/example_nanopub.yaml` correctly uses a
local pre-publication URI. Publication should be a transformation step, not a
schema-validation side effect.

### Recommended nanopublication pipeline

1. Validate the DAPPER source instance.
2. Serialize it to the required four named RDF graphs.
3. Start with temporary/local nanopublication IRIs, including local graph and
   signature-element IRIs.
4. Use the maintained nanopublication tooling to sign and transform it to a
   Trusty URI. `nanopub-java` supports temporary URI replacement and handles
   self-references.
5. Verify the resulting nanopublication with an independent check before
   accepting or publishing it.
6. Store the final artifact code and final nanopublication URI as output, while
   retaining the source instance and transformation metadata for replay.
7. Publish corrections as successors; never update content in place.

Use the official CLI/library operations (`sign`, `mktrusty`, `check`) rather
than duplicating the canonicalizer. Conformance should be tested against known
published nanopublications and official implementation outputs.

### Recommended general content-ID profile

For non-nanopublication DAPPER objects, define a small profile before writing
code. It should state:

- the exact semantic scope of the ID and excluded transport fields;
- the input model/version;
- the canonicalization algorithm and version;
- the hash algorithm;
- the digest encoding;
- self-reference handling;
- blank-node handling;
- maximum input/complexity limits;
- verification procedure; and
- successor/version relationships.

For RDF output in 2026, **RDFC-1.0** is the current W3C Recommendation for RDF
dataset canonicalization and supports blank nodes. A profile using RDFC-1.0 plus
SHA-256 is easier to justify for new generic RDF identifiers than inventing a
canonicalizer. It is not the same algorithm as Trusty URI `RA`, so it needs its
own profile/module label.

For JSON/YAML-native content, parse both formats into one data model and apply a
standard canonical JSON representation before hashing. Never hash YAML text,
and never rely on a language runtime's default object serialization.

## Design decisions to settle before implementation

1. **Identity scope:** Does an ID name a claim, a publication package, an
   entire provenance graph, a file, or an MCP response? These should not share
   one undifferentiated identifier type.
2. **Time-varying fields:** Timestamps and signatures intentionally make a
   publication ID unique, but request IDs, retrieval times, cache headers, and
   mirror observations usually should not affect a semantic artifact ID.
3. **Canonical source:** Decide whether RDF is authoritative or generated from
   LinkML/JSON. Round-trip stability must be tested if identifiers cross these
   representations.
4. **Blank nodes:** Continue DAPPER's use of explicit node IDs where possible.
   If blank nodes are permitted, use RDFC-1.0 for a new profile or deterministic
   skolemization for strict `RA` compatibility.
5. **Algorithm agility:** Put the canonicalization/hash profile in the ID. A
   naked SHA-256 value does not say what was hashed.
6. **Resolver independence:** Treat the resolver as an index to content, never
   as the authority for whether retrieved bytes/quads are correct.

## Risks and cautions

- **Do not use `RA` as a decorative prefix.** Existing software interprets it
  as an exact algorithm/version contract.
- **Do not hash LinkML YAML bytes.** Key order, comments, quoting, and emitter
  versions would become identity-bearing.
- **Do not confuse persistence with availability.** A content ID remains valid
  even if no resolver retains a copy; replication and archival policy are still
  required.
- **Do not confuse hashing with signing.** Anyone can reproduce a content hash.
  A signature binds content to a key; identity-to-key trust needs its own policy.
- **Set canonicalization complexity limits.** RDFC-1.0 documents pathological
  blank-node graphs that can cause denial-of-service behavior.
- **Keep test vectors forever.** Every supported profile needs fixed positive
  and negative vectors so future library upgrades cannot silently change IDs.

## Sources

1. Nanopublications FAQ, especially "What measures have been taken towards
   long-term stability of the nanopublication identifiers?" and "How should
   nanopublications be cited?":
   <https://nanodash.knowledgepixels.com/resource?1&id=https://w3id.org/spaces/nanopub/r/faq&context=https://w3id.org/spaces/knowledgepixels/nanodash/r/home>
2. Trusty URI specification, version 1:
   <https://github.com/trustyuri/trustyuri-spec>
3. Trusty URI overview and implementations: <https://trustyuri.net/>
4. Nanopublication Guidelines, including well-formedness and integrity keys:
   <https://nanopub.net/guidelines/working_draft/>
5. `trustyuri-java`, including `RdfPreprocessor`, `RdfHasher`, and module `RA`:
   <https://github.com/trustyuri/trustyuri-java>
6. `nanopub-java`, including temporary URI replacement and CLI operations:
   <https://github.com/Nanopublication/nanopub-java>
7. W3C RDF Dataset Canonicalization (RDFC-1.0), Recommendation 21 May 2024:
   <https://www.w3.org/TR/rdf-canon/>
8. Kuhn and Dumontier, "Trusty URIs: Verifiable, Immutable, and Permanent
   Digital Artifacts for Linked Data" (2014): <https://arxiv.org/abs/1401.5775>
