import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LazyDraft } from "../src/lib/lazy-draft";

type Draft = { id: string };
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness() {
  const state = { epoch: 1, current: null as Draft | null, adoptable: true, created: [] as ReturnType<typeof deferred<Draft>>[], adopted: [] as string[], discarded: [] as string[] };
  const lazy = new LazyDraft<Draft>({
    epoch: () => state.epoch, current: () => state.current,
    create: () => { const next = deferred<Draft>(); state.created.push(next); return next.promise; },
    adopt: draft => { if (!state.adoptable) return false; state.current = draft; state.adopted.push(draft.id); return true; },
    discard: draft => { state.discarded.push(draft.id); },
  });
  return { state, lazy };
}

test("opening a gap writes nothing; the first need creates one draft that every caller shares", async () => {
  const { state, lazy } = harness();
  assert.equal(state.created.length, 0);
  const attachment = lazy.ensure(), check = lazy.ensure();
  assert.equal(state.created.length, 1, "An attachment and the CFDE check share one creation");
  state.created[0].resolve({ id: "temporary" });
  assert.deepEqual(await Promise.all([attachment, check]), [{ id: "temporary" }, { id: "temporary" }]);
  assert.deepEqual(state.adopted, ["temporary"]);
  assert.deepEqual(await lazy.ensure(), { id: "temporary" }); assert.equal(state.created.length, 1);
});

test("an existing draft is used as is", async () => {
  const { state, lazy } = harness(); state.current = { id: "saved" };
  assert.deepEqual(await lazy.ensure(), { id: "saved" }); assert.equal(state.created.length, 0);
});

test("a creation that settles after its editor was replaced is discarded, never adopted", async () => {
  const { state, lazy } = harness();
  const old = lazy.ensure(); state.epoch++;
  const fresh = lazy.ensure();
  assert.equal(state.created.length, 2, "The new editor does not wait on the old editor's creation");
  state.created[0].resolve({ id: "old" }); state.created[1].resolve({ id: "new" });
  assert.equal(await old, null); assert.deepEqual(await fresh, { id: "new" });
  assert.deepEqual(state.discarded, ["old"]); assert.deepEqual(state.adopted, ["new"]);
});

test("a creation that loses to another draft or to a submission is discarded", async () => {
  const saved = harness();
  const pending = saved.lazy.ensure(); saved.state.current = { id: "saved-by-save" };
  saved.state.created[0].resolve({ id: "temporary" });
  assert.deepEqual(await pending, { id: "saved-by-save" }); assert.deepEqual(saved.state.discarded, ["temporary"]);
  const submitting = harness(); submitting.state.adoptable = false;
  const lost = submitting.lazy.ensure(); submitting.state.created[0].resolve({ id: "temporary" });
  assert.equal(await lost, null); assert.deepEqual(submitting.state.discarded, ["temporary"]); assert.deepEqual(submitting.state.adopted, []);
});

test("a failed creation reports its error and the next need tries again; settled() never rejects", async () => {
  const { state, lazy } = harness();
  const first = lazy.ensure(), settled = lazy.settled();
  state.created[0].reject(new Error("unavailable"));
  await assert.rejects(first, /unavailable/); assert.equal(await settled, null);
  const second = lazy.ensure(); assert.equal(state.created.length, 2);
  state.created[1].resolve({ id: "temporary" }); assert.deepEqual(await second, { id: "temporary" });
});

test("Save waits for its editor's creation in flight, and only its own editor's", async () => {
  const { state, lazy } = harness();
  void lazy.ensure(); let waited = false;
  const save = lazy.settled().then(() => { waited = true; });
  await Promise.resolve(); assert.equal(waited, false);
  state.created[0].resolve({ id: "temporary" }); await save; assert.equal(waited, true);
  void lazy.ensure().catch(() => {}); state.current = null; state.epoch++;
  assert.equal(await Promise.race([lazy.settled().then(() => "settled"), new Promise(done => setTimeout(() => done("waiting"), 20))]), "settled");
});

test("the Composer opens a gap without a draft and creates one only for attachments, the CFDE check, Save or Submit", () => {
  const composer = readFileSync(new URL("../src/components/Composer.tsx", import.meta.url), "utf8");
  const opening = composer.slice(composer.indexOf("async function selectGap("), composer.indexOf("const submissionFailed"));
  assert.doesNotMatch(opening, /createWorkingDraft/);
  assert.ok(opening.indexOf("const suggested = suggest(") < opening.indexOf("await openIdentity()"), "Suggestions do not wait for a session");
  assert.match(opening, /navigateSelection\(target\); setDraftView\(true\)/);
  assert.match(composer, /draft=\{ensureDraftId\}/);
  assert.match(composer, /if \(assessable\) void lazyDraft\.current!\.ensure\(\)/);
  assert.match(composer, /if \(!resumed\) \{ await lazyDraft\.current!\.settled\(\);/);
  assert.doesNotMatch(composer, /newInputsValid: [^\n]*!!draft/, "Submit creates its own draft");
  const inputs = readFileSync(new URL("../src/components/ResearchInputs.tsx", import.meta.url), "utf8");
  assert.ok(inputs.indexOf("const draftId = await draft();") < inputs.indexOf('uploadRequest<UploadTicket>("/v1/uploads", "POST"'));
});
