import assert from "node:assert/strict";
import test from "node:test";
import { adminDate, formatAdminDate, formatAdminJson, localizeAdminValue } from "../src/lib/admin-time";

test("SQL timestamps are UTC and explicit offsets describe the same instant", () => {
  const utc = "2026-09-26T21:15:45Z";
  for (const stored of ["2026-09-26 21:15:45", "2026-09-26 21:15:45.000000", "2026-09-26T23:15:45+02:00"]) {
    assert.equal(adminDate(stored)?.toISOString(), utc.replace("Z", ".000Z"));
    assert.equal(formatAdminDate(stored, "Europe/Rome"), formatAdminDate(utc, "Europe/Rome"));
  }
  assert.match(formatAdminDate(utc, "Europe/Rome"), /23:15:45 GMT\+2/);
});

test("local times use the offset for the timestamp, including DST and day rollover", () => {
  assert.match(formatAdminDate("2026-01-01T00:30:00Z", "Europe/Rome"), /01:30:00 GMT\+1/);
  assert.match(formatAdminDate("2026-07-01T00:30:00Z", "Europe/Rome"), /02:30:00 GMT\+2/);
  const losAngeles = formatAdminDate("2026-01-01T00:30:00Z", "America/Los_Angeles");
  assert.match(losAngeles, /2025/);
  assert.match(losAngeles, /16:30:00 GMT-8/);
});

test("unknown, invalid and date-only scientific values remain unchanged", () => {
  for (const value of [null, undefined, "", "2026-01-01", "2026-02-30T00:00:00Z", "2026-01-01T24:00:00Z", "2026-00-01T00:00:00Z", "2026-01-01T00:00:00+99:00", "invalid"]) {
    assert.equal(adminDate(value), null);
    assert.equal(formatAdminDate(value), "—");
  }
  assert.equal(localizeAdminValue("2026-01-01"), "2026-01-01");
  assert.equal(adminDate(1790457345), null);
  assert.equal(adminDate("9007199254740993", "version"), null);
  assert.equal(adminDate(1790457345, "running_at")?.getTime(), 1790457345000);
  assert.equal(adminDate(1790457345000, "running_at")?.getTime(), 1790457345000);
});

test("JSON timestamps localize without rounding scientific values or rewriting keys", () => {
  const raw = '{"version":9007199254740993,"decimal":1.234567890123456789,"2026-01-01T00:00:00Z":"key","dates":["2026-01-01T00:30:00Z","2026-01-01"],"timings":{"running_at":1790457345}}';
  for (const pretty of [true, false]) {
    const formatted = formatAdminJson(raw, pretty, "Europe/Rome");
    assert.ok(formatted.includes("9007199254740993"));
    assert.ok(formatted.includes("1.234567890123456789"));
    const parsed = JSON.parse(formatted);
    assert.equal(parsed["2026-01-01T00:00:00Z"], "key");
    assert.equal(parsed.dates[0], formatAdminDate("2026-01-01T00:30:00Z", "Europe/Rome"));
    assert.equal(parsed.dates[1], "2026-01-01");
    assert.equal(parsed.timings.running_at, formatAdminDate(1790457345, "Europe/Rome", "running_at"));
  }
  assert.doesNotThrow(() => formatAdminJson('{"created_at":"2026-01-01T00:30'));
});
