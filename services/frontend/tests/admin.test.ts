import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { adminBypass, allowedAdmin } from "../src/lib/admin-policy";
test("admin email matching is normalized, exact and verified", () => {
  const emails = " chase@broadinstitute.org, cyakabos@broadinstitute.org, ";
  assert.equal(allowedAdmin("Chase@BroadInstitute.org", true, emails), true);
  assert.equal(allowedAdmin("cyakabos@broadinstitute.org", true, emails), true);
  for (const email of [null, undefined, "", "attacker@broadinstitute.org", "chase@broadinstitute.org.evil", "ase@broadinstitute.org"]) assert.equal(allowedAdmin(email, true, emails), false);
  for (const verified of [false, undefined, "true", 1]) assert.equal(allowedAdmin("chase@broadinstitute.org", verified, emails), false);
  assert.equal(allowedAdmin("chase@broadinstitute.org", true, undefined), false);
});
test("login bypass requires literal true and development; production always denies", () => {
  assert.equal(adminBypass({ DISABLE_ADMIN_LOGIN: "true", NODE_ENV: "development" }), true);
  for (const NODE_ENV of ["production", "test", undefined]) assert.equal(adminBypass({ DISABLE_ADMIN_LOGIN: "true", NODE_ENV }), false);
  for (const DISABLE_ADMIN_LOGIN of [undefined, "false", "TRUE", "1"]) assert.equal(adminBypass({ DISABLE_ADMIN_LOGIN, NODE_ENV: "development" }), false);
  assert.equal(adminBypass({ DISABLE_ADMIN_LOGIN: "true", NODE_ENV: "development", REVEAL_ENVIRONMENT: "production" }), false);
});

test("stored JSON display preserves large integers, decimal literals and escaped strings", async () => {
  const { formatStoredJson } = await import("../src/lib/stored-json");
  const source = '{"version":9007199254740993,"decimal":1.234567890123456789,"nested":[{},[],{"text":"a, { \\\" b"}]}';
  const formatted = formatStoredJson(source);
  assert.ok(formatted.includes("9007199254740993"));
  assert.ok(formatted.includes("1.234567890123456789"));
  assert.deepEqual(JSON.parse(formatted), JSON.parse(source));
});

test("performance rows show per-request database cost on http rows and a dash where it is not recorded", async () => {
  const React = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const { AdminPerformance, LOCK_NOTE } = await import("../src/components/AdminPerformance");
  const scope = "This API process since startup. database rows: FENCE is the global-lock wait plus one round trip; LOCK_HOLD runs from fence grant to COMMIT/ROLLBACK.";
  const http = { category: "http", name: "GET /v1/local-work/{work_id}", count: 120, errors: 2, mean_ms: 31.4, p95_ms: 88.1,
    mean_statements: 4.5, mean_db_ms: 12.25, mean_pool_wait_ms: 0.4, mean_lock_wait_ms: 0, mean_connects: 0.01, mean_resets: 0.33 };
  const fence = { category: "database", name: "FENCE", count: 7, errors: 0, mean_ms: 142.6, p95_ms: 301.2 };
  const html = renderToStaticMarkup(React.createElement(AdminPerformance, { runtime: { pid: 41, uptime_seconds: 5400, scope, rows: [http, fence] } }));
  const headings = [...html.matchAll(/<th>([^<]*)<\/th>/g)].map(m => m[1]);
  assert.deepEqual(headings, ["Operation", "Requests", "Errors", "Mean", "Recent p95", "Statements / req", "DB ms / req", "Pool wait", "Lock wait", "Connects / req", "Resets / req"]);
  const rows = [...html.matchAll(/<tr><td><small>.*?<\/tr>/g)].map(m => [...m[0].matchAll(/<td>(.*?)<\/td>/g)].map(c => c[1]));
  assert.deepEqual(rows[0].slice(1), ["120", "2", "31.4 ms", "88.1 ms", "4.5", "12.25 ms", "0.4 ms", "0 ms", "0.01", "0.33"]);
  assert.equal(rows[0][0], "<small>http</small>GET /v1/local-work/{work_id}");
  assert.deepEqual(rows[1].slice(5), ["—", "—", "—", "—", "—", "—"]);
  assert.ok(html.includes(`<p>${scope}</p>`));
  assert.ok(!html.includes(LOCK_NOTE));
  assert.match(html, /Process 41 · uptime 1\.5h/);
  const older = renderToStaticMarkup(React.createElement(AdminPerformance, { runtime: { pid: 41, uptime_seconds: 30, scope: "This API process since startup.", rows: [fence] } }));
  assert.ok(older.includes(`<p>${LOCK_NOTE}</p>`));
  assert.ok(older.includes("<p>This API process since startup.</p>"));
  const backend = readFileSync(new URL("../../backend/src/reveal_backend/runtime_metrics.py", import.meta.url), "utf8");
  assert.match(backend, /FENCE is the global-lock wait plus one round trip; '\s*'LOCK_HOLD runs from fence grant to COMMIT\/ROLLBACK/);
  const unscoped = renderToStaticMarkup(React.createElement(AdminPerformance, { runtime: { pid: 41, uptime_seconds: 30, scope: "", rows: [] } }));
  assert.equal([...unscoped.matchAll(/<p>/g)].length, 2);
  assert.ok(unscoped.includes(LOCK_NOTE));
});
