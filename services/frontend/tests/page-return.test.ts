import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { onPageReturn } from "../src/lib/page-return";

test("a tab switch (visibilitychange + focus) runs one page-return check, none while hidden", () => {
  const win = new EventTarget(), doc = Object.assign(new EventTarget(), { visibilityState: "visible" as DocumentVisibilityState });
  let clock = 0, checks = 0;
  const stop = onPageReturn(() => { checks++; }, { win, doc, now: () => clock });
  doc.dispatchEvent(new Event("visibilitychange")); win.dispatchEvent(new Event("focus"));
  assert.equal(checks, 1);
  clock += 1_000; doc.visibilityState = "hidden"; doc.dispatchEvent(new Event("visibilitychange")); assert.equal(checks, 1);
  doc.visibilityState = "visible"; win.dispatchEvent(new Event("online")); assert.equal(checks, 2);
  stop(); clock += 1_000; win.dispatchEvent(new Event("focus")); assert.equal(checks, 2);
});

test("trending accounts and the leaderboard check once per page return through the shared helper", async () => {
  const browser = await readFile(new URL("../src/components/GapBrowser.tsx", import.meta.url), "utf8");
  const trending = browser.slice(browser.indexOf("export function TrendingAccounts"));
  assert.doesNotMatch(trending, /addEventListener\("(focus|visibilitychange)"/);
  assert.match(trending, /onPageReturn\(\(\) => \{ if \(!inflight\.current && \(!snapshot\.current \|\| Date\.now\(\) - loadedAt\.current >= publicFreshMs\)\) refresh\(\); \}\)/);
  const leaderboard = await readFile(new URL("../src/components/leaderboard/Leaderboard.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(leaderboard, /addEventListener\("focus"/);
});
