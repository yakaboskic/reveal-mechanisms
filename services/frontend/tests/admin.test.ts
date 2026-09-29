import assert from "node:assert/strict";
import test from "node:test";
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
