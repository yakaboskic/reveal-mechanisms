import { test } from "node:test";
import assert from "node:assert/strict";
import { sourceDownloadPath } from "../src/lib/source-download";

const path = "/api/backend/v1/artifacts/" + "a".repeat(64);
test("captured sources stay on the current origin while equivalent loopback hosts work", () => {
  assert.equal(sourceDownloadPath(path, "https://reveal.example"), path);
  assert.equal(sourceDownloadPath("https://reveal.example" + path, "https://reveal.example"), path);
  assert.equal(sourceDownloadPath("http://localhost:3000" + path, "http://127.0.0.1:3000"), path);
  assert.equal(sourceDownloadPath("http://[::1]:3000" + path, "http://localhost:3000"), path);
});

test("other hosts, schemes, ports, credentials and nonartifact paths never become downloads", () => {
  for (const value of ["https://localhost:3000" + path, "http://localhost:8000" + path,
    "http://localhost.evil.example:3000" + path, "http://evil.example:3000" + path,
    "http://user:secret@localhost:3000" + path, "javascript:alert(1)", "/api/backend/v1/jobs/private",
    "/api/backend/v1/artifacts/not-a-sha", null, ""]) {
    assert.equal(sourceDownloadPath(value, "http://127.0.0.1:3000"), null, String(value));
  }
  assert.equal(sourceDownloadPath("http://localhost:3000" + path, "https://reveal.example"), null);
});
