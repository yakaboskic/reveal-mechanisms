import { test } from "node:test";
import assert from "node:assert/strict";
import { loadingAppearance, loadingColors, loadingLevel, loadingMembershipCounts, loadingNavigation, loadingValue, type LoadingCell } from "../src/lib/loading-visual";

test("a loading keeps its color when filtering or paging changes the visible rows", () => {
  const complete = [0, 2, 4, 6, 8, 10];
  const filtered = complete.filter(value => value >= 6);
  const colors = complete.map(value => loadingLevel(value, 0, 10));
  assert.deepEqual(filtered.map(value => loadingLevel(value, 0, 10)), colors.slice(3));
  assert.equal(loadingColors[loadingLevel(0, 0, 10)!], "#f1f8f3");
  assert.equal(loadingColors[loadingLevel(10, 0, 10)!], "#116b45");
  assert.equal(loadingLevel(-1, 0, 10), 0);
  assert.equal(loadingLevel(11, 0, 10), 4);
});

test("missing values remain distinct from observed zero and invalid scales do not imply zero", () => {
  assert.equal(loadingLevel(null, 0, 1), null);
  assert.equal(loadingLevel(0, 0, 1), 0);
  assert.equal(loadingValue(null), "Not available");
  assert.equal(loadingValue(0), "0");
  for (const invalid of [NaN, Infinity, -Infinity]) {
    assert.equal(loadingLevel(invalid, 0, 1), null);
    assert.equal(loadingLevel(1, 0, invalid), null);
    assert.equal(loadingValue(invalid), "Not available");
  }
  assert.equal(loadingLevel(1, null, 2), null);
  assert.equal(loadingLevel(1, 2, 0), null);
  assert.equal(loadingLevel(0, 0, 0), 0);
  assert.equal(loadingLevel(4, 4, 4), 4);
  assert.equal(loadingValue(0.000000123456789), String(0.000000123456789));
});

test("membership overlays preserve loading scale, exact values and original ranks", () => {
  const item = Object.freeze({ id: "gene:SHANK3", label: "SHANK3", loading: 0.6890000104904175, rank: 14 });
  const original = loadingAppearance(item, 0, 0.92);
  for (const isMember of [true, false, undefined]) {
    const appearance = loadingAppearance({ ...item, isMember }, 0, 0.92, "Synaptic signaling");
    assert.equal(appearance.level, original.level, "Membership cannot change the numeric loading color");
    assert.equal(loadingValue(item.loading), "0.6890000104904175");
    assert.equal(item.rank, 14);
  }
  const member = loadingAppearance({ ...item, isMember: true }, 0, 0.92, "Synaptic signaling");
  assert.equal(member.membership, "member");
  assert.equal(member.membershipLabel, "Member of Synaptic signaling");
  assert.equal(loadingAppearance({ ...item, isMember: false }, 0, 0.92, "Synaptic signaling").membershipLabel, "Not a member of Synaptic signaling");
});

test("unresolved membership stays unknown and overlay removal clears membership annotations", () => {
  const item: LoadingCell = { id: "gene:unknown", label: "Unknown gene", loading: null, rank: 1 };
  assert.deepEqual(loadingAppearance(item, 0, 1, "Selected set"), {
    level: null, membership: "unknown", membershipLabel: "Membership in Selected set is not available",
  });
  assert.equal(loadingAppearance({ ...item, isMember: true }, 0, 1).membership, null);
  assert.equal(loadingAppearance({ ...item, isMember: false }, 0, 1, "   ").membershipLabel, null);
  assert.equal(loadingAppearance({ ...item, loading: 0, isMember: true }, 0, 1, "Selected set").level, 0);
});

test("overlay counts reflect visible rows and include members whose loading is missing", () => {
  const items: readonly LoadingCell[] = Object.freeze([
    { id: "a", label: "A", loading: null, rank: 1, isMember: true },
    { id: "b", label: "B", loading: 0, rank: 2, isMember: false },
    { id: "c", label: "C", loading: 0.9, rank: 3 },
    { id: "d", label: "D", loading: 0.5, rank: 4, isMember: true },
  ]);
  assert.deepEqual(loadingMembershipCounts(items), { members: 2, nonmembers: 1, unknown: 1, total: 4 });
  assert.deepEqual(loadingMembershipCounts(items.slice(1, 3)), { members: 0, nonmembers: 1, unknown: 1, total: 2 });
  assert.deepEqual(loadingMembershipCounts([]), { members: 0, nonmembers: 0, unknown: 0, total: 0 });
});

test("arrow navigation follows rows and stays valid at boundaries and incomplete rows", () => {
  assert.equal(loadingNavigation(0, "ArrowLeft", 17, 6), 0);
  assert.equal(loadingNavigation(0, "ArrowUp", 17, 6), 0);
  assert.equal(loadingNavigation(3, "ArrowUp", 17, 6), 3);
  assert.equal(loadingNavigation(5, "ArrowRight", 17, 6), 6);
  assert.equal(loadingNavigation(8, "ArrowDown", 17, 6), 14);
  assert.equal(loadingNavigation(11, "ArrowDown", 17, 6), 16);
  assert.equal(loadingNavigation(16, "ArrowRight", 17, 6), 16);
  assert.equal(loadingNavigation(14, "ArrowDown", 17, 6), 14);
  assert.equal(loadingNavigation(16, "ArrowUp", 17, 6), 10);
  assert.equal(loadingNavigation(0, "ArrowDown", 0, 6), null);
});

test("Home and End support row navigation and Control supports whole-grid navigation", () => {
  assert.equal(loadingNavigation(8, "Home", 17, 6), 6);
  assert.equal(loadingNavigation(8, "End", 17, 6), 11);
  assert.equal(loadingNavigation(14, "End", 17, 6), 16);
  assert.equal(loadingNavigation(8, "Home", 17, 6, true), 0);
  assert.equal(loadingNavigation(8, "End", 17, 6, true), 16);
  assert.equal(loadingNavigation(8, "Enter", 17, 6), null);
});
