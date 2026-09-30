import assert from "node:assert/strict";
import test from "node:test";
import mutations from "../src/lib/mutations.ts";
const { createMutationKeys } = mutations;

test("identical new drafts receive distinct keys after a confirmed save", async () => {
  let sequence = 0;
  const mutations = createMutationKeys(() => `key-${++sequence}`);
  const create = ["save", undefined, undefined, { source_gap: { id: "gap-1" } }, "Same name"];
  const keys = [];
  const save = key => { keys.push(key); return Promise.resolve({ id: `draft-${keys.length}` }); };
  assert.equal((await mutations.run(create, save)).id, "draft-1");
  assert.equal((await mutations.run(create, save)).id, "draft-2");
  assert.deepEqual(keys, ["key-1", "key-2"]);
});

test("lost response and later auth refusal retain the original write key", async () => {
  let sequence = 0;
  const mutations = createMutationKeys(() => `key-${++sequence}`), keys = [];
  for (const reason of [new TypeError("Response lost"), new Error("401 session expired")]) {
    await assert.rejects(mutations.run(["save", "draft-1", 2], async key => { keys.push(key); throw reason; }), reason);
  }
  await mutations.run(["save", "draft-1", 2], async key => { keys.push(key); return { version: 3 }; });
  assert.deepEqual(keys, ["key-1", "key-1", "key-1"]);
});

test("a failed list refresh after successful mutation cannot resurrect its retired key", async () => {
  let sequence = 0;
  const mutations = createMutationKeys(() => `key-${++sequence}`), keys = [];
  await assert.rejects((async () => {
    await mutations.run(["save", null, null, "same draft"], async key => { keys.push(key); return { id: "saved" }; });
    throw new Error("List refresh failed");
  })(), /List refresh failed/);
  await mutations.run(["save", null, null, "same draft"], async key => { keys.push(key); return { id: "new" }; });
  assert.deepEqual(keys, ["key-1", "key-2"]);
});
