# Prisma schema for REVEAL

`schema.prisma` has 23 models: the 11 original Aurora tables for DAPPER GeneSets
and EAGGL, three application crosswalk tables from `../migrations/004_eaggl_cfde_links.sql`,
plus nine DisMech tables from `../migrations/003_dismech.sql`. Existing
tables were read with `SHOW CREATE TABLE` over verified TLS; those exact definitions
and migrations 003/004 were recreated and checked in disposable MySQL 8.0.42.
Prisma validation, client generation, and a schema diff including all 23 table
definitions against that database pass. The crosswalk is described in
[EAGGL → CFDE links](../../docs/eaggl-cfde-links.md).

**September 25 state:** the original consolidation audit mapped [11 live tables](../../data/audit/2026-09-25-consolidation/database.json); its [column/nullability comparison and Prisma validation](../../data/audit/2026-09-25-consolidation/prisma.json) passed without DDL. The later [crosswalk load verification](../../data/eaggl-cfde-mapping/2026-09-25/database-verification.json) records **14 applied tables** and successful validation/client generation/schema comparison for the updated 23-model schema. Nine DisMech models remain pending.

DisMech is defined here but has not yet been applied to Aurora. Use the
[Python importer](../../docs/dismech-import.md) to apply its migration and load data.

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
