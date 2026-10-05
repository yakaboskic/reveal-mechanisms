# Prisma schema for REVEAL

`schema.prisma` has 47 models: the 11 original Aurora tables for DAPPER GeneSets
and EAGGL, three crosswalk tables from migration 004, nine DisMech source tables
from migration 003, two application tables from migration 005, three
persistent DisMech embedding tables from migration 006, ten
reference-generation tables from migration 008, and the nine reference release
tables (`reveal_ref_*`). The crosswalk is described in
[EAGGL → CFDE links](../../docs/eaggl-cfde-links.md); reference generations in
[Reference reload](../../docs/reference-reload.md). The reference release tables
are not a migration: the publisher creates them per environment
([Reference release](../../docs/reference-release.md)). Migration 008's tables,
except `archived_reference_factors`, are dropped by the one-time cleanup.

The original tables were read with `SHOW CREATE TABLE` over verified TLS. Those
definitions and migrations 003/004 were recreated and checked in disposable
MySQL 8.0.42; the historical 23-model schema passed validation, generation, and
database comparison. The 28-model schema before migration 008 passed Prisma
validation; that check alone does not apply a migration or establish a new live
schema diff. The ten migration-008 models were added by hand from
`schema/migrations/008_reference_generation.sql`, and the nine reference release
models from the publisher's DDL template in
`services/backend/src/reveal_backend/reference_release.py`; neither set has been
through `prisma validate` yet.
The [DisMech readback report](../../data/validation/local-stack/dismech-verification.json)
records the completed source import, including all 19,959 mechanisms and 3,367
knowledge gaps.

Source import and vector backfill are explicit operations. Use the
[source importer](../../docs/dismech-import.md) for a new DisMech snapshot and the
[persistent embedding pipeline](../../docs/dismech-embeddings.md) for migration
006, its calibrated context vectors, and independent database verification.

## Install and generate

Use Node 24 LTS, Node 22.12+, or Node 20.19+ (Node 23 is not supported by this
pinned Prisma release):

```bash
cd schema/prisma
npm ci
npm run validate
npm run generate
```

Prisma 7.10.0 and its client are pinned in `package-lock.json`. Generated client
code goes to `generated/client` and is ignored by Git. Prisma's
[v7 configuration](https://www.prisma.io/docs/orm/v7/reference/prisma-config-reference)
keeps the datasource URL in `prisma.config.ts`. It reads the repository-root `.env`
and constructs the URL from `REVEAL_MYSQL_*`, percent-encoding credentials.
An explicit `DATABASE_URL` overrides those settings. No password is stored here.

CLI URLs set `sslaccept=strict` and pass the configured CA as `sslcert`, following
the [MySQL connector](https://www.prisma.io/docs/orm/v7/core-concepts/supported-databases/mysql).
On this Mac, Prisma's native CLI currently rejects the RDS certificate chain with
P1011 even though Python verifies it using the configured bundle. Direct Prisma
Aurora introspection therefore remains unverified; the schema was verified using
the equivalent local DDL. Do not disable certificate checking to work around it.

`npm run inspect` is read-only (`prisma db pull --print`), so it prints the connected
database's models without overwriting this schema. Pointing it at current Aurora
will show only applied tables once the native TLS issue is resolved.

## Mapping and ownership

Model names are singular PascalCase with `@@map` to the actual table names. SQL
column names are retained so Prisma code and importer queries use the same names.
The schema includes composite keys, exact FK/index names, unsigned integers,
timestamp precision, JSON fields, and EAGGL's `MEDIUMBLOB` float32 vectors.

SQL migrations remain authoritative. Prisma cannot fully represent column
collations, the EAGGL loading CHECK constraint, or database-managed `ON UPDATE`
timestamps. No Prisma migration history has been baselined for this existing
database. Use the reviewed SQL migrations/importers for changes; do not run
`prisma db push`, `migrate dev`, or `migrate reset` against Aurora.

## Reading knowledge gaps

`DismechDiscussion.is_gap` distinguishes source gaps from other discussion kinds.
The gap kind, nullable source status, question prompt, full source payload, and
attachment relations are available without conversion to DAPPER identities.
After applying the DisMech import, use a specific completed import:

```ts
const snapshot = await prisma.dismechImport.findUnique({
  where: { import_id: importId },
});
if (snapshot?.status !== "complete") throw new Error("DisMech import is not complete");

const gaps = await prisma.dismechDiscussion.findMany({
  where: { import_id: importId, is_gap: true },
  include: {
    dismech_documents: true,
    dismech_gap_attachments: {
      include: { dismech_mechanisms: true },
    },
  },
  orderBy: { id_sha256: "asc" },
  take: 50,
});
```

Prisma 7 applications instantiate the generated client with a MySQL driver
adapter, such as `@prisma/adapter-mariadb`. Driver connection/TLS options must be
configured by the application; `prisma.config.ts` configures the CLI only. This
schema package does not deploy an API or add a running Node database service.
