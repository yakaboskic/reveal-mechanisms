import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { jobHref, pageNumber, pageOf, urlWith } from "../src/lib/admin-url";
import { Age, asTone, Empty, Metric, Pagination, ShortId, Status, StateStatus, shortAge, statusTone } from "../src/components/admin/ui";

const render = <P extends object>(component: React.ComponentType<P>, props: P) => renderToStaticMarkup(React.createElement(component, props));
const params = (query = "") => new URLSearchParams(query);

test("view state goes in the URL: updates set keys, empty values remove them, others stay", () => {
  assert.equal(urlWith("/admin/jobs", "", { status: "failed" }), "/admin/jobs?status=failed");
  assert.equal(urlWith("/admin/jobs", params("status=failed&owner=o1&page=3"), { page: null, kind: "agent" }), "/admin/jobs?status=failed&owner=o1&kind=agent");
  assert.equal(urlWith("/admin/jobs", "q=x", { q: "", job: undefined }), "/admin/jobs");
  assert.equal(urlWith("/admin/activity", "", { records: 2 }), "/admin/activity?records=2");
  assert.equal(urlWith("/admin/jobs", "", { q: "a b&c" }), "/admin/jobs?q=a+b%26c");
  // One job opens under the Jobs list until slice 3b; the old /admin?job= link is gone.
  assert.equal(jobHref("95d0bc18/x"), "/admin/jobs?job=95d0bc18%2Fx");
});

test("pages: a stale or invalid page number never shows nothing", () => {
  for (const value of [null, "", "0", "-2", "abc", "1.5"]) assert.equal(pageNumber(value), 1, String(value));
  assert.equal(pageNumber("4"), 4);
  const items = Array.from({ length: 60 }, (_, i) => i);
  assert.deepEqual(pageOf(items, 2, 25), { items: items.slice(25, 50), page: 2, pages: 3, total: 60 });
  assert.deepEqual(pageOf(items, 9, 25).page, 3);
  assert.deepEqual(pageOf([], 3, 25), { items: [], page: 1, pages: 1, total: 0 });
});

test("pagination links each page and disables the ends", () => {
  const middle = render(Pagination, { page: 2, pages: 3, total: 60, noun: "job", href: page => `/admin/jobs?page=${page}` });
  assert.ok(middle.includes("60 jobs · Page 2 of 3"));
  assert.ok(middle.includes('href="/admin/jobs?page=1"') && middle.includes('href="/admin/jobs?page=3"'));
  const first = render(Pagination, { page: 1, pages: 2, total: 26, href: page => `?page=${page}` });
  assert.ok(first.includes('<span class="submit" aria-disabled="true">Previous</span>') && first.includes('href="?page=2"'));
  const one = render(Pagination, { page: 1, pages: 1, total: 1, noun: "event", href: page => `?page=${page}` });
  assert.ok(one.includes("1 event · Page 1 of 1") && !one.includes("href="));
});

test("status tones: good, bad, warn and info by meaning, neutral otherwise", () => {
  assert.deepEqual(["succeeded", "ready", "deposited", "failed", "withheld_unsafe", "insufficient_evidence", "cancelled", "running", "queued", "unknown", ""].map(statusTone),
    ["good", "good", "good", "bad", "bad", "warn", "warn", "info", "info", "neutral", "neutral"]);
  // depositOutcome's empty tone is neutral; a known tone passes through.
  assert.deepEqual([asTone(""), asTone(null), asTone("bad"), asTone("loud")], ["neutral", "neutral", "bad", "neutral"]);
  assert.equal(render(StateStatus, { value: "insufficient_evidence" }), '<span class="admin-status warn">insufficient evidence</span>');
  assert.equal(render(Status, { tone: "info", children: "queued" }), '<span class="admin-status info">queued</span>');
});

test("a metric with a link opens the list it counts; without one it is a card", () => {
  assert.equal(render(Metric, { title: "Failed jobs", value: "6", hint: "All time", href: "/admin/jobs?status=failed" }),
    '<a class="admin-metric" href="/admin/jobs?status=failed"><span class="admin-metric-title">Failed jobs</span><strong>6</strong><span class="admin-metric-hint">All time</span></a>');
  assert.ok(render(Metric, { title: "Last 7 days", value: "$4.00" }).startsWith('<div class="admin-metric">'));
});

test("short ids and ages show the whole value on hover and copy it", () => {
  const id = "95d0bc18-6f2a-4b6c-9c1e-3a1d2e4f5a6b";
  const html = render(ShortId, { value: id, href: jobHref(id) });
  assert.ok(html.includes(`<a title="${id}" href="/admin/jobs?job=${id}">95d0bc18</a>`) && html.includes(`aria-label="Copy: ${id}"`));
  assert.ok(render(ShortId, { value: null }).includes("—"));
  const now = Date.parse("2026-11-19T10:00:00Z");
  assert.deepEqual([shortAge(new Date(now - 20_000), now), shortAge(new Date(now - 5 * 60_000), now), shortAge(new Date(now - 3 * 3600_000), now), shortAge(new Date(now - 2 * 86400_000), now)], ["now", "5m", "3h", "2d"]);
  assert.equal(shortAge(new Date("2026-10-03T12:00:00Z"), now), "Oct 3");
  assert.equal(shortAge(new Date("2025-10-03T12:00:00Z"), now), "Oct 3, 2025");
  const age = render(Age, { value: "2026-11-19 09:55:00", now });
  assert.match(age, /^<time class="admin-age" dateTime="2026-11-19T09:55:00.000Z" title="[^"]*GMT[^"]*">5m<\/time>$/);
  assert.equal(render(Age, { value: null, empty: "Not started" }), '<span class="admin-muted">Not started</span>');
});

test("an empty list is said in words", () => {
  assert.equal(render(Empty, { children: "Nothing is running or queued." }), '<p class="admin-empty">Nothing is running or queued.</p>');
});
