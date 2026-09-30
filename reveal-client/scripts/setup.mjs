import { randomBytes } from "node:crypto";
import { chmod, readFile, rename, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseEnv } from "node:util";

const credentialFields = ["REVEAL_API_URL", "REVEAL_GATEWAY_SECRET", "REVEAL_GATEWAY_SERVICE_TOKEN", "REVEAL_GATEWAY_ISSUER", "REVEAL_GATEWAY_AUDIENCE"];
const artifactFields = ["REVEAL_ARTIFACT_DOWNLOAD_BASE_URLS", "REVEAL_ARTIFACT_DOWNLOAD_BASE_URL"];
export async function configure({ credentialsPath, destination, force = false }) {
  let input;
  try { input = parseEnv(await readFile(credentialsPath, "utf8")); } catch { throw new Error("Could not read the credentials env file."); }
  if (credentialFields.some(name => !input[name]?.trim()) || input.REVEAL_GATEWAY_SECRET.length < 32 || input.REVEAL_GATEWAY_SERVICE_TOKEN.length < 32) {
    throw new Error("The credentials file must contain the five REVEAL gateway settings; both secrets must be at least 32 characters.");
  }
  try {
    const url = new URL(input.REVEAL_API_URL);
    if ((url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) || url.username || url.password || url.search || url.hash) throw new Error();
  } catch { throw new Error("REVEAL_API_URL must be an HTTPS API URL or a loopback HTTP URL."); }
  let previous = {};
  try {
    previous = parseEnv(await readFile(destination, "utf8"));
    if (!force) throw new Error("An existing .env.local was preserved. Use --force to replace gateway settings while preserving its local identity and AUTH_SECRET.");
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  const values = Object.fromEntries([...credentialFields, ...artifactFields].filter(name => input[name]).map(name => [name, input[name]]));
  Object.assign(values, { AUTH_SECRET: previous.AUTH_SECRET || randomBytes(32).toString("base64url"), APP_ORIGIN: previous.APP_ORIGIN || "http://localhost:3200",
    REVEAL_DEMO_MODE: "true", DEMO_IDENTITY_ISSUER: previous.DEMO_IDENTITY_ISSUER || "urn:reveal:client:dk", DEMO_IDENTITY_SUBJECT: previous.DEMO_IDENTITY_SUBJECT || "developer" });
  if (Object.values(values).some(value => /[\r\n\0]/.test(value))) throw new Error("Settings must be single-line values.");
  const output = "# Private local setup. Do not commit or share this file.\n" + Object.entries(values).map(([name, value]) => `${name}=${JSON.stringify(value).replace(/\$/g, "\\$")}`).join("\n") + "\n";
  const temporary = `${destination}.${randomBytes(8).toString("hex")}.tmp`;
  await writeFile(temporary, output, { mode: 0o600, flag: "wx" });
  await chmod(temporary, 0o600);
  await rename(temporary, destination);
  return { destination };
}
async function main() {
  const args = process.argv.slice(2); let credentialsPath, force = false;
  for (let index = 0; index < args.length; index++) {
    if (args[index] === "--credentials" && args[index + 1]) credentialsPath = args[++index];
    else if (args[index] === "--force") force = true;
    else throw new Error("Usage: npm run setup -- --credentials /absolute/path/to/dk-qa.env [--force]");
  }
  if (!credentialsPath) throw new Error("Usage: npm run setup -- --credentials /absolute/path/to/dk-qa.env [--force]");
  await configure({ credentialsPath: resolve(credentialsPath), destination: resolve(".env.local"), force });
  process.stdout.write("Private .env.local saved with mode 600. Run npm run dev and open http://localhost:3200.\n");
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { process.stderr.write(`Setup failed: ${error.message}\n`); process.exitCode = 1; });
}
