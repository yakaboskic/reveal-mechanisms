import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { applySuggestions, emptyComposer, factorSelection, removeAnchor } from "../src/lib/composer";
import { readEvents, type Schema } from "../src/lib/client";
import { createFixtureAdapter, type ValidatedFixtures } from "../src/lib/fixture-adapter";

const fixtures = JSON.parse(readFileSync(new URL("../src/lib/fixtures/contract.json", import.meta.url), "utf8")) as ValidatedFixtures;
test("fixture provenance pins the actual OpenAPI contract and explicit non-live notice", () => {
  assert.equal(fixtures.contractSha256, createHash("sha256").update(readFileSync(new URL("../../../api/openapi.json", import.meta.url))).digest("hex"));
  assert.match(fixtures.notice, /invented KG assertions/);
});
test("automatic anchors are unique by native identity, capped across calls, and persist dismissals", () => {
  let composer = applySuggestions(emptyComposer(), fixtures.suggestions);
  assert.ok(composer.eaggl_anchors.length <= 5);
  composer = applySuggestions(composer, fixtures.suggestions);
  assert.equal(new Set(composer.eaggl_anchors.map(a => a.reference.source_id)).size, composer.eaggl_anchors.length);
  const removed = composer.eaggl_anchors[0].reference.source_id;
  composer = removeAnchor(composer, removed);
  composer = applySuggestions(composer, fixtures.suggestions);
  assert.ok(composer.dismissed_source_ids.includes(removed));
  assert.ok(!composer.eaggl_anchors.some(a => a.reference.source_id === removed));
});
test("manual anchors do not duplicate automatic selections or consume five automatic capacity", () => {
  const composer = emptyComposer(); const first = fixtures.suggestions.automatic_anchors[0].factor;
  composer.eaggl_anchors = [factorSelection(first, "manual")];
  const result = applySuggestions(composer, fixtures.suggestions);
  assert.equal(result.eaggl_anchors.filter(a => a.reference.source_id === first.source_id).length, 1);
  assert.equal(result.eaggl_anchors[0].origin, "manual");
});
test("fixture adapter preserves exact scientific identity and clones to avoid mutable source corruption", async () => {
  const adapter = createFixtureAdapter(fixtures, "test");
  await assert.rejects(adapter.account("dapper:ScientificAccount.wrong"), /different scientific identity/);
  const result = await adapter.account(fixtures.account.root_id);
  result.document.scientific_accounts![0].closing_remarks = "Changed by presentation";
  assert.notEqual((await adapter.account(fixtures.account.root_id)).document.scientific_accounts![0].closing_remarks, "Changed by presentation");
});
test("SSE parser reassembles arbitrary UTF-8 chunks and ignores heartbeats", async () => {
  const event = { ...fixtures.events.items[0], message: "Read αβ evidence" };
  const bytes = new TextEncoder().encode(`: ping\n\nevent: activity\nid: ${event.id}\ndata: ${JSON.stringify(event)}\n\n`);
  const stream = new ReadableStream<Uint8Array>({ start(controller) { for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3)); controller.close(); } });
  const events: Schema<"JobEvent">[] = [];
  await readEvents(new Response(stream), value => events.push(value)); assert.deepEqual(events, [event]);
});
test("malformed activity and expired replay cursor fail explicitly", async () => {
  await assert.rejects(readEvents(new Response('data: {"message":"missing identity"}\n\n'), () => {}), /Malformed/);
  await assert.rejects(readEvents(Response.json({ code: "EVENT_CURSOR_EXPIRED", detail: "Replay expired" }, { status: 410 }), () => {}), /Replay expired/);
});

test("SSE parser normalizes CRLF split between chunks", async () => {
  const event = fixtures.events.items[0];
  const bytes = new TextEncoder().encode(`: ping\r\n\r\ndata: ${JSON.stringify(event)}\r\n\r\n`);
  const stream = new ReadableStream<Uint8Array>({ start(controller) { for (const byte of bytes) controller.enqueue(new Uint8Array([byte])); controller.close(); } });
  const events: Schema<"JobEvent">[] = [];
  await readEvents(new Response(stream), value => events.push(value)); assert.deepEqual(events, [event]);
});
