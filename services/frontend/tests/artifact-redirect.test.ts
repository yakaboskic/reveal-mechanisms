import assert from "node:assert/strict";
import test from "node:test";
import { allowedArtifactRedirect } from "../src/lib/artifact-redirect";

const path = ["v1", "artifacts", "a".repeat(64)];
const base = "https://assets.example.com/reveal/";
test("only artifact requests redirect to the configured storage origin and prefix", () => {
  assert.equal(allowedArtifactRedirect(path, base + "artifacts/sha256/aa/a?versionId=1", base), true);
  for (const target of ["https://evil.example/reveal/a", "https://assets.example.com/other/a",
    "https://assets.example.com/reveal/../private", "https://user:pass@assets.example.com/reveal/a",
    "http://assets.example.com/reveal/a", base + "a#fragment"]) {
    assert.equal(allowedArtifactRedirect(path, target, base), false, target);
  }
  assert.equal(allowedArtifactRedirect(["v1", "me"], base + "a", base), false);
  assert.equal(allowedArtifactRedirect(path, base + "a", undefined), false);
});
test("plain HTTP is limited to explicitly configured local storage", () => {
  const local = "http://127.0.0.1:19000/bucket/";
  assert.equal(allowedArtifactRedirect(path, local + "a", local), true);
  assert.equal(allowedArtifactRedirect(path, "http://remote.example/bucket/a", "http://remote.example/bucket/"), false);
});
test("cloud cutover allows only explicitly retained storage prefixes", () => {
  const allowed = "https://assets.example.com/prod/, https://assets.example.com/local/";
  assert.equal(allowedArtifactRedirect(path, "https://assets.example.com/local/artifacts/old", allowed), true);
  assert.equal(allowedArtifactRedirect(path, "https://assets.example.com/prod/artifacts/new", allowed), true);
  assert.equal(allowedArtifactRedirect(path, "https://assets.example.com/other/artifacts/private", allowed), false);
  assert.equal(allowedArtifactRedirect(path, "https://evil.example/local/artifacts/private", allowed), false);
});
