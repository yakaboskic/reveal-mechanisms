import { test } from "node:test";
import assert from "node:assert/strict";
import { formatLeaderboardNumber, leaderboardHref, LeaderboardPageChangedError, mergeLeaderboardPages, publicLeaderboardHref, readLeaderboardQuery, sortsFor, verifiedOrcidHref } from "../src/lib/leaderboard";
import type { Schema } from "../src/lib/client";

// Invented local projection records; never inserted into the community database.
const entry = (id: string, rank = 1): Schema<"LeaderboardEntry"> => ({ id, rank, kind: "researcher", label: `Test contributor ${id}`, metrics: { overall_score: 35, account_count: 1, net_votes: 0, upvotes: 0, downvotes: 0, voter_count: 0, account_gap_count: 1, explored_gap_count: 1, claim_count: 1, researcher_count: 0, traced_claim_count: 0, traced_account_count: 0 }, components: { accounts: 50, votes: 0, gaps: 50 }, orcid: null, account_id: null, gap_id: null, gap: null, attribution: null, directions: { SUPPORTS: 0, DISPUTES: 0, MIXED: 0, NEUTRAL: 0, UNKNOWN: 0 } });
const page = (items: Schema<"LeaderboardEntry">[], cursor: string | null = null): Schema<"LeaderboardList"> => ({ items, view: "researchers", sort: "overall", evidence: "all", scope: "published", as_of: "2026-10-01T10:00:00Z", score_version: "public-contribution-v1", cohort_size: 3, voting_participants: 0, exclusions: { fixture_accounts: 0, uncredited_accounts: 0, conflicting_accounts: 0, invalid_public_accounts: 0, excluded_evidence_paths: 0, fixture_explorations: 0, uncredited_explorations: 0 }, definitions: [], methodology: { weights: { accounts: .3, votes: .4, gaps: .3 }, scope: "Public test projection", score: "Test definition", own_votes: "Excluded", evidence: "Explicit paths", traced: "Stored traces" }, page: { has_more: !!cursor, next_cursor: cursor, snapshot_id: "public-projection-v1" } });
const query = readLeaderboardQuery(new URLSearchParams());

test("leaderboard URLs normalize unsupported combinations without losing a valid drilldown", () => {
  assert.deepEqual(query, { view: "researchers", sort: "overall", evidence: "all", scope: "published", selected: null, metric: "accounts" });
  const researcher = readLeaderboardQuery(new URLSearchParams("view=researchers&sort=files&metric=files&evidence=supporting&selected=researcher_AbC"));
  assert.deepEqual(researcher, { ...query, selected: "researcher_AbC" });
  assert.deepEqual(readLeaderboardQuery(new URLSearchParams("view=accounts&sort=overall&metric=explored&evidence=supporting&scope=all")), { view: "accounts", sort: "votes", evidence: "all", scope: "published", selected: null, metric: "votes" });
  assert.equal(readLeaderboardQuery(new URLSearchParams("tab=datasets&sort=claims&metric=files&evidence=supporting")).metric, "files");
  assert.equal(readLeaderboardQuery(new URLSearchParams("view=datasets&metric=votes")).metric, "accounts");
});

test("leaderboard links preserve exact IDs, dataset evidence and record metric, while tab changes clear selection", () => {
  const selected = readLeaderboardQuery(new URLSearchParams("view=datasets&sort=claims&evidence=supporting&selected=dapper%3ADataset.AbC_123&metric=files"));
  const roundTrip = readLeaderboardQuery(new URLSearchParams(leaderboardHref(selected).split("?")[1]));
  assert.deepEqual(roundTrip, selected);
  assert.equal(leaderboardHref(selected, { view: "researchers", sort: "overall", selected: null }), "/leaderboard?view=researchers&sort=overall");
  assert.equal(readLeaderboardQuery(new URLSearchParams(leaderboardHref(selected, { metric: "gaps" }).split("?")[1])).selected, selected.selected);
});

test("pagination retains server ordering and tied ranks, deduplicates overlaps, and allows a new observation timestamp", () => {
  const first = page([entry("B", 1), entry("A", 1)], "second-page");
  const second = { ...page([entry("A", 1), entry("C", 3)]), as_of: "2026-10-01T10:02:00Z" };
  const merged = mergeLeaderboardPages(first, second, query);
  assert.deepEqual(merged.items.map(row => [row.id, row.rank]), [["B", 1], ["A", 1], ["C", 3]]);
  assert.equal(merged.as_of, second.as_of);
  assert.equal(merged.page.has_more, false);
});

test("pagination rejects changed public snapshots, repeating cursors and records from another ranking", () => {
  const first = page([entry("A")], "second-page");
  assert.throws(() => mergeLeaderboardPages(first, { ...page([entry("B")]), page: { ...first.page, snapshot_id: "changed" } }, query), LeaderboardPageChangedError);
  assert.throws(() => mergeLeaderboardPages(first, page([entry("B")], "second-page"), query), LeaderboardPageChangedError);
  assert.throws(() => mergeLeaderboardPages(null, { ...page([]), view: "datasets" }, query), /selected view/);
  assert.throws(() => mergeLeaderboardPages(null, { ...page([]), evidence: "supporting" }, query), /selected view/);
  assert.throws(() => mergeLeaderboardPages(null, page([{ ...entry("A"), kind: "dataset" }]), query), /different view/);
});

test("public record links stay on known public routes and ORCID links require the canonical verified-identity format", () => {
  assert.equal(publicLeaderboardHref("/accounts/dapper%3AScientificAccount.AbC"), "/accounts/dapper%3AScientificAccount.AbC");
  assert.equal(publicLeaderboardHref("/leaderboard?view=researchers&selected=researcher_AbC"), "/leaderboard?view=researchers&selected=researcher_AbC");
  for (const href of ["https://example.com/accounts/id", "//example.com/accounts/id", "/\\example.com/accounts/id", "/workspace?tab=accounts", "/api/backend/v1/me", "javascript:alert(1)"]) assert.equal(publicLeaderboardHref(href), null);
  assert.equal(verifiedOrcidHref("https://orcid.org/0000-0002-1825-0097"), "https://orcid.org/0000-0002-1825-0097");
  for (const href of [null, "https://example.com/0000-0002-1825-0097", "https://orcid.org.evil.test/0000-0002-1825-0097", "javascript:alert(1)"]) assert.equal(verifiedOrcidHref(href), null);
});

test("display formatting preserves negative community votes and rounds only the visible score", () => {
  assert.equal(formatLeaderboardNumber(-4), "-4");
  assert.equal(formatLeaderboardNumber(35.125, true), "35.1");
  assert.equal(formatLeaderboardNumber(0, true), "0.0");
});

test("all accounts rank datasets only by traced claims, drop the evidence filter, and round-trip through the URL", () => {
  const all = readLeaderboardQuery(new URLSearchParams("view=datasets&scope=all&sort=claims&evidence=supporting&metric=files&selected=dapper%3ADataset.AbC_123"));
  assert.deepEqual(all, { view: "datasets", sort: "traced_claims", evidence: "all", scope: "all", selected: "dapper:Dataset.AbC_123", metric: "traced_claims" });
  assert.deepEqual(readLeaderboardQuery(new URLSearchParams(leaderboardHref(all).split("?")[1])), all);
  assert.equal(readLeaderboardQuery(new URLSearchParams("view=datasets&scope=all&metric=traced_accounts")).metric, "traced_accounts");
  assert.deepEqual(sortsFor("datasets", "all").map(sort => sort.id), ["traced_claims"]);
  assert.ok(sortsFor("datasets", "published").some(sort => sort.id === "traced_claims"));
  const published = readLeaderboardQuery(new URLSearchParams("view=datasets&sort=traced_claims"));
  assert.equal(published.scope, "published"); assert.equal(published.sort, "traced_claims");
  assert.equal(leaderboardHref(all, { view: "researchers", sort: "overall", scope: "published", selected: null }), "/leaderboard?view=researchers&sort=overall");
  assert.throws(() => mergeLeaderboardPages(null, { ...page([]), view: "datasets", sort: "traced_claims" }, all), /selected view/);
});
