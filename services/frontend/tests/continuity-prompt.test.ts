import test from "node:test";
import assert from "node:assert/strict";
import { continuityPromptDismissed, dismissContinuityPrompt } from "../src/lib/continuity-prompt";

function session() {
  const values = new Map<string, string>();
  return { values, getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
}

test("dismissal survives repeated reads in the same browser session and is scoped to its signed-in user", () => {
  const storage = session(); storage.setItem("reveal:submission", "existing work");
  assert.equal(continuityPromptDismissed("user-a", storage), false);
  dismissContinuityPrompt("user-a", storage);
  assert.equal(continuityPromptDismissed("user-a", storage), true);
  assert.equal(continuityPromptDismissed("user-b", storage), false);
  assert.equal(continuityPromptDismissed("user-a", storage), true);
  assert.equal(storage.getItem("reveal:submission"), "existing work");
  assert.equal(storage.values.size, 2, "Only one UI preference was added; existing work is untouched");
});

test("a new browser session and missing identity do not inherit another dismissal", () => {
  const first = session(), next = session();
  dismissContinuityPrompt("user-a", first); dismissContinuityPrompt("", next);
  assert.equal(continuityPromptDismissed("user-a", next), false);
  assert.equal(continuityPromptDismissed("", first), false);
  assert.equal(next.values.size, 0);
});

test("restricted browser storage does not break the continuity choice", () => {
  const storage = { getItem: () => { throw new Error("Storage blocked"); }, setItem: () => { throw new Error("Storage blocked"); } };
  assert.equal(continuityPromptDismissed("user-a", storage), false);
  assert.doesNotThrow(() => dismissContinuityPrompt("user-a", storage));
});
