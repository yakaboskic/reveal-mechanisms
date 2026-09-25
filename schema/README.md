# Evidence-package schema

[evidence-package.yaml](evidence-package.yaml) is the canonical **LinkML schema** for the collector's `reveal.evidence-package/0.2-draft` output. [evidence-package.schema.json](evidence-package.schema.json) is its generated, self-contained JSON Schema for consumers that do not use Python or LinkML. Edit the LinkML source and regenerate the JSON file together.

This models the agent's input package. It does not replace the DAPPER schema, define new scientific Claims, or describe the internal `build-input.json` replay manifest. Earlier `0.1-draft` illustrative packets remain historical examples and do not validate against this version.

## Model organization

| Section | Root-associated class | Content |
|---|---|---|
| Package | `EvidencePackage` | Version, prefix declarations and required sections |
| Selection | `EPSelection` | Selected gap, mechanism anchors, origins and expansion policy |
| DisMech | `EPDismechContext` | Gap question, linked mechanisms, other attachments and related gaps |
| PIGEAN/EAGGL | `EPPigeanEvidence` | Mechanism loadings, trait observations, candidates and retained graph |
| Entities | `EPEntities` | Gene identities and imported DAPPER GeneSet mappings |
| DAPPER objects | `EPDapperContext` | Hydrated objects using the actual imported DAPPER classes |
| Sources | `EPSourceArtifact`, `EPSourceRef` | Content hashes, DAPPER File IDs and exact JSON Pointers |
| Coverage and limits | `EPCoverage`, `EPBuildPolicy` | Query outcomes, counts, omissions and retention budgets |
| Authoring and readiness | `EPAuthoring`, `EPReadiness` | Frozen instructions, selected question and remaining worker checks |
| External enrichment | `EPExternalEvidence` | Selected graphs; the initial ledger and assertions must be empty |

An EAGGL factor is a mechanism. Observed loadings, association statistics, ranking scores and semantic similarity have separate fields. A source observation is not itself an authored Claim or Proposition.

### Dictionary and source boundaries

Mechanisms, traits, entity observations and artifacts are inlined dictionaries. Their keys hold the source identifier; the abstract `_key` identifier does not need to be repeated inside each value. Prefixes and selection origins use simple key/value dictionaries. These follow [LinkML's dictionary inlining model](https://linkml.io/linkml/schemas/inlining.html).

Application classes are closed: unknown fields are rejected. DisMech mechanism records and designated raw source objects explicitly allow extra source fields. A whole-section attachment can retain an array. These boundaries preserve upstream data without pretending its entire schema is owned here. Their application bindings (`dapper_id`, `source_ref`, semantic associations) remain typed. Missing optional fields and explicit nulls are supported where indicated by the generated schema; required observations and application sections cannot be null.

The LinkML source imports `data/dapper/2026-09-24-v8/snapshot/schema/dapper.yaml` by a repository-relative path. It reuses the pinned `KnowledgeGap`, `Mechanism`, `GeneSet`, `File`, `Activity` and other supported DAPPER classes. The generated JSON Schema embeds those definitions, so consumers need no network access or external schema resolver.

## Generate and validate

Run from the repository root after installing the backend evidence dependencies:

```bash
python -m pip install -e 'services/backend[evidence]'

# Regenerate JSON Schema from LinkML.
python scripts/evidence_package_schema.py generate

# Check that the generated file is up to date, without changing it.
python scripts/evidence_package_schema.py generate --check

# Validate a collected package; JSON and YAML are both accepted.
python scripts/evidence_package_schema.py validate \
  data/evidence-captures/cad-builder-v1/package/evidence-package.yaml
```

The generation command uses LinkML 1.11.1, then materializes two explicit serialization declarations that this generator version does not fully emit: `extra_slots.allowed` for open source records, and annotated raw JSON array/Any ranges. Those small compatibility rules live in [evidence_schema.py](../services/backend/src/reveal_backend/evidence_schema.py). Use the project command to reproduce the checked-in schema; plain `gen-json-schema` does not apply these rules.

The validation command checks the package shape, types, enums, required fields, hashes' format and URI formats. It additionally verifies the pinned DAPPER snapshot, prefix bindings, DAPPER object identities and dependency closure. It reads local files and makes no API calls. It does not coerce values or change the package.

Cross-record scientific and collection invariants remain in the [deterministic builder](../docs/evidence-package-builder.md): source checksums and pointer resolution, exact source-to-projection agreement, query completeness, consistent seed sets, graph closure/deduplication, GeneSet mappings and retention counts. A standalone JSON Schema validator cannot establish these facts or biological truth. Passing schema validation also does not authorize agent dispatch.

Schema validation is a separate command so historical captures retain their builder-code pins and byte-for-byte replay. The tests require newly collected fixture output to conform to this schema; the future worker can call `validate_package_shape` at its handoff boundary.

```bash
PYTHONPATH=services/backend/src python -m unittest discover \
  -s services/backend/tests -p 'test_evidence*.py' -v
```

## Other application boundaries

- [Gateway schema](gateway.schema.json): proposed service-only principal/proof/claim input/output definitions; [contract](../docs/gateway-contract.md). It is not an authentication implementation.
- [Agent output schema](agent-output.schema.json): proposed worker-owned attempt result manifest linking individually validated DAPPER documents; [contract](../docs/agent-output-contract.md). The existing linter consumes each account document, not this manifest.
- [Prisma/SQL](prisma/README.md): source inventory storage; application job/auth/citation tables remain future migrations.
- [OpenAPI](../api/openapi.json): public research transport, bundling the actual generated evidence-package schema.
