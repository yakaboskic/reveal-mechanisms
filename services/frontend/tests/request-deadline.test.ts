import { test } from "node:test";
import assert from "node:assert/strict";
import { RequestTimeoutError, withRequestDeadline } from "../src/lib/request-deadline";

test("a stalled request times out and aborts, even if it ignores the signal", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let signal!: AbortSignal;
  let finish!: (value: string) => void;
  const result = withRequestDeadline(s => {
    signal = s;
    return new Promise<string>(resolve => { finish = resolve; });
  });
  const rejected = assert.rejects(result, RequestTimeoutError);
  t.mock.timers.tick(30_000);
  await rejected;
  assert.equal(signal.aborted, true);
  finish("late response");
  await assert.rejects(result, RequestTimeoutError);
});

test("a response with headers but a stalled JSON body still times out", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const response = new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('{"partial":')); } }));
  const result = withRequestDeadline(async () => response.json(), "Confirmation timed out.");
  const rejected = assert.rejects(result, { name: "RequestTimeoutError", message: "Confirmation timed out." });
  t.mock.timers.tick(30_000);
  await rejected;
});

test("completed requests clear their deadline and preserve normal errors", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let signal!: AbortSignal;
  assert.equal(await withRequestDeadline(async s => { signal = s; return "ready"; }), "ready");
  t.mock.timers.tick(30_000);
  assert.equal(signal.aborted, false);
  const failure = new Error("Rejected by server");
  await assert.rejects(withRequestDeadline(async () => { throw failure; }), error => error === failure);
});
