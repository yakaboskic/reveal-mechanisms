import assert from "node:assert/strict";
import test from "node:test";
import { nextVote } from "../src/lib/votes";
import { changesWorkspace } from "../src/lib/workspace-events";

test("vote arrows set, switch, and remove a vote without accumulating votes", () => {
  for (const direction of [-1, 1] as const) {
    assert.equal(nextVote(null, direction), direction);
    assert.equal(nextVote(0, direction), direction);
    assert.equal(nextVote(direction, direction), 0);
    assert.equal(nextVote(direction === 1 ? -1 : 1, direction), direction);
  }
});

test("committed votes invalidate event-driven views but reading vote totals does not", () => {
  for (const path of ["/v1/knowledge-gaps/gap/vote", "/v1/accounts/account/vote"]) {
    assert.equal(changesWorkspace("POST", "/api/backend" + path), true);
    assert.equal(changesWorkspace("GET", "/api/backend" + path), false);
  }
});
