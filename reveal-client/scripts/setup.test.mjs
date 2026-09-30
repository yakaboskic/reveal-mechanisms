import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { parseEnv } from "node:util";
import { configure } from "./setup.mjs";

const input = { REVEAL_API_URL: "https://example.test/api/reveal", REVEAL_GATEWAY_SECRET: "s".repeat(40), REVEAL_GATEWAY_SERVICE_TOKEN: "t".repeat(40),
  REVEAL_GATEWAY_ISSUER: "reveal-nextjs", REVEAL_GATEWAY_AUDIENCE: "reveal-api", REVEAL_ARTIFACT_DOWNLOAD_BASE_URLS: "https://bucket.test/qa/", UNRELATED_PROVIDER_KEY: "never-copy" };
test("setup copies only gateway settings, writes mode600, and preserves identity on explicit replacement", async () => {
  const directory = await mkdtemp(join(tmpdir(), "reveal-setup-"));
  try {
    const source = join(directory, "source.env"), destination = join(directory, ".env.local");
    await writeFile(source, Object.entries(input).map(([key, value]) => `${key}=${value}`).join("\n"));
    await configure({ credentialsPath: source, destination });
    const original = await readFile(destination, "utf8"), values = parseEnv(original);
    assert.equal((await stat(destination)).mode & 0o777, 0o600);
    assert.equal(values.UNRELATED_PROVIDER_KEY, undefined); assert.ok(values.AUTH_SECRET.length >= 43);
    assert.equal(values.APP_ORIGIN, "http://localhost:3200"); assert.equal(values.REVEAL_DEMO_MODE, "true");
    await assert.rejects(configure({ credentialsPath: source, destination }), /existing .env.local was preserved/);
    assert.equal(await readFile(destination, "utf8"), original);
    await writeFile(destination, original.replace('"developer"', '"established-subject"'));
    await configure({ credentialsPath: source, destination, force: true });
    const updated = parseEnv(await readFile(destination, "utf8"));
    assert.equal(updated.AUTH_SECRET, values.AUTH_SECRET); assert.equal(updated.DEMO_IDENTITY_SUBJECT, "established-subject");
    assert.equal(updated.REVEAL_GATEWAY_SECRET, input.REVEAL_GATEWAY_SECRET);
  } finally { await rm(directory, { recursive: true, force: true }); }
});
test("missing credentials or invalid settings produce no output file", async () => {
  const directory = await mkdtemp(join(tmpdir(), "reveal-setup-"));
  try {
    const source = join(directory, "source.env"), destination = join(directory, ".env.local");
    await assert.rejects(configure({ credentialsPath: source, destination }), /Could not read/);
    await writeFile(source, "REVEAL_GATEWAY_SECRET=secret-that-must-not-appear\n");
    await assert.rejects(configure({ credentialsPath: source, destination }), error => !error.message.includes("secret-that-must-not-appear"));
    await assert.rejects(stat(destination), { code: "ENOENT" });
  } finally { await rm(directory, { recursive: true, force: true }); }
});
