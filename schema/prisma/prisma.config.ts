import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { config } from "dotenv";
import { defineConfig } from "prisma/config";

const root = fileURLToPath(new URL("../../", import.meta.url));
// Same .env as the Python importers. No shell execution or variable expansion.
config({ path: resolve(root, ".env"), override: false, quiet: true });

function databaseUrl(): string {
  // An explicit override is useful for isolated local tests.
  if (process.env.DATABASE_URL) return process.env.DATABASE_URL;
  const env = process.env;
  const database = env.REVEAL_MYSQL_DATABASE || "cyaka_reveal_mechanisms";
  if (!/^cyaka_[a-z0-9_]+$/.test(database)) {
    throw new Error("REVEAL_MYSQL_DATABASE must have a literal cyaka_ prefix");
  }
  const host = env.REVEAL_MYSQL_HOST || "aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com";
  const port = env.REVEAL_MYSQL_PORT || "3306";
  const user = encodeURIComponent(env.REVEAL_MYSQL_USER || "cyaka");
  const password = encodeURIComponent(env.REVEAL_MYSQL_PASSWORD || "");
  const query = new URLSearchParams({ sslaccept: "strict" });
  if (env.REVEAL_MYSQL_CA_FILE) {
    query.set("sslcert", resolve(root, env.REVEAL_MYSQL_CA_FILE));
  }
  return `mysql://${user}:${password}@${host}:${port}/${database}?${query}`;
}

export default defineConfig({
  schema: "schema.prisma",
  datasource: { url: databaseUrl() },
});
