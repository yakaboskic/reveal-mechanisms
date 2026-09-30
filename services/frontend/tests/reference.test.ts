import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { api, ApiError, supersededReference, unwrap, type Schema } from "../src/lib/client";
import { applySuggestions, currentAnalysisComposer, dropOutdatedAnchors, emptyComposer, factorSelection, persistDraft } from "../src/lib/composer";
import { anchorKey, currentComposer, currentReferenceModel, isArchived, isReferenceReload, legacyReferenceModel, modelOfSourceId, observeFactors, observedReferenceModel, outdatedFromAnchor, outdatedFromFactor, parseReferenceState, referenceProblemMessage, referenceQuery, referenceRechecker, referenceRecheckDelaysMs, referenceReloaded } from "../src/lib/reference";
import { rememberSubmission, restoreSubmission, type SubmissionAttempt } from "../src/lib/submission";
import { loadWorkspaceData, workspaceKey, workspaceKeys } from "../src/lib/workspace-data";
import { affectedWorkspaceTabs } from "../src/lib/workspace-events";

// Every archived shape below is a contract example built by the backend's own stamp helpers.
const spec = JSON.parse(readFileSync(new URL("../../../api/openapi.json", import.meta.url), "utf8"));
const example = <T>(path: string, method: string, name: string, status = "200") => (Object.values(spec.paths[path][method].responses[status].content)[0] as { examples: Record<string, { value: T }> }).examples[name].value;
const kpnFactor = example<Schema<"EagglFactor">>("/v1/mechanisms/{source_id}", "get", "kpn_factor");
const legacyFactor = example<Schema<"EagglFactor">>("/v1/mechanisms/{source_id}", "get", "eaggl_factor");
const archivedAccounts = example<{ items: Schema<"AccountSummary">[] }>("/v1/accounts", "get", "archived");
const archivedOutcome = example<Schema<"AnalysisOutcome">>("/v1/analysis-outcomes/{outcome_id}", "get", "archived_public_reader");
const superseded = example<Schema<"Problem">>("/v1/mechanisms/{source_id}", "get", "reference_generation_superseded", "410");
const suggestions = example<Schema<"Suggestions">>("/v1/mechanisms/suggest", "post", "illustrative_five_total");
const archive = archivedAccounts.items.find(isArchived)!.archive!;

function withStorage(t: { after: (fn: () => void) => void }) {
  const stores = ["localStorage", "sessionStorage"].map(name => {
    const values = new Map<string, string>();
    const store = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, String(value)); }, removeItem: (key: string) => { values.delete(key); } };
    Object.defineProperty(globalThis, name, { value: store, configurable: true, writable: true });
    return name;
  });
  t.after(() => { for (const name of stores) delete (globalThis as Record<string, unknown>)[name]; });
}
const composerWith = (factor: Schema<"EagglFactor">, model = factor.model): Schema<"Composer"> => ({ ...emptyComposer(), model, source_gap: archivedOutcome.source_gap, eaggl_anchors: [factorSelection(factor, "manual")] });

test("public factor ids expose their reference model, including KPN ids", () => {
  assert.equal(modelOfSourceId("factor:portal:CAD:cfde-inc-v2:Factor1"), "cfde-inc-v2");
  assert.equal(modelOfSourceId(kpnFactor.source_id), "eaggl-capped-v1");
  assert.equal(modelOfSourceId("factor:kpn:0000398:unknown-model:Factor1"), null);
  assert.equal(modelOfSourceId("dismech:disorders/x"), null);
});

test("without an observed reload the legacy model and requests are unchanged", t => {
  withStorage(t);
  assert.equal(observedReferenceModel(), null);
  assert.equal(emptyComposer().model, legacyReferenceModel);
  assert.equal(referenceReloaded(), false);
  observeFactors([legacyFactor]);
  assert.equal(currentReferenceModel(), "cfde-inc-v2"); assert.equal(referenceReloaded(), false);
  // Legacy composers survive; the `all` listing omits reference_state entirely.
  const legacy = composerWith(legacyFactor);
  assert.equal(currentComposer(legacy), legacy);
  assert.equal(referenceQuery("all"), undefined); assert.equal(referenceQuery(undefined), undefined);
  assert.equal(referenceQuery("archived"), "archived"); assert.equal(parseReferenceState("bogus"), "all");
});

test("stored composers from a superseded model are discarded unless they only hold a gap", t => {
  withStorage(t);
  observeFactors([{ source: "dismech" }, kpnFactor]);
  assert.equal(observedReferenceModel(), "eaggl-capped-v1"); assert.equal(referenceReloaded(), true);
  assert.equal(emptyComposer().model, "eaggl-capped-v1");
  assert.equal(currentComposer(composerWith(legacyFactor)), null);
  // A composer whose model field was updated but still holds an old anchor is also stale.
  assert.equal(currentComposer(composerWith(legacyFactor, "eaggl-capped-v1")), null);
  const gapOnly = { ...composerWith(legacyFactor), eaggl_anchors: [] };
  assert.deepEqual(currentComposer(gapOnly), { ...gapOnly, model: "eaggl-capped-v1" });
  const current = composerWith(kpnFactor);
  assert.equal(currentComposer(current), current);
});

test("a pending submission kept across a reload is dropped instead of resubmitted", t => {
  withStorage(t);
  const attempt = (composer: Schema<"Composer">): SubmissionAttempt => ({ method: "session", question: "q", gap: null, composer, draft: null, owner: "user", anonymousKey: "key", requestKeys: [], submitKey: null });
  observeFactors([legacyFactor]);
  rememberSubmission(attempt(composerWith(legacyFactor)));
  assert.equal(restoreSubmission()?.composer.eaggl_anchors[0].reference.source_id, legacyFactor.source_id);
  observeFactors([kpnFactor]);
  assert.equal(restoreSubmission(), null);
  assert.equal(sessionStorage.getItem("reveal:submission"), null);
  rememberSubmission(attempt(composerWith(kpnFactor)));
  assert.ok(restoreSubmission());
});

test("suggestions and manual selections carry the model the API served", () => {
  const legacy = applySuggestions(emptyComposer(), suggestions);
  assert.equal(legacy.model, "cfde-inc-v2");
  const kpn = applySuggestions({ ...emptyComposer(), model: "cfde-inc-v2" }, { ...suggestions, automatic_anchors: [{ ...suggestions.automatic_anchors[0], factor: kpnFactor }] });
  assert.equal(kpn.model, "eaggl-capped-v1"); assert.equal(kpn.eaggl_anchors[0].reference.source_id, kpnFactor.source_id);
  // Key order is unchanged, so the autosave comparison still detects no-op snapshots.
  assert.deepEqual(Object.keys(kpn), Object.keys(emptyComposer()));
  assert.deepEqual(applySuggestions(legacy, { ...suggestions, automatic_anchors: [] }).model, legacy.model);
});

test("replacing outdated anchors keeps current ones and does not dismiss the old ids", () => {
  const composer = { ...composerWith(legacyFactor), eaggl_anchors: [factorSelection(legacyFactor, "automatic"), factorSelection(kpnFactor, "manual")], dismissed_source_ids: [legacyFactor.source_id, "factor:portal:X:cfde-inc-v2:Factor2"] };
  const next = dropOutdatedAnchors(composer, new Set([legacyFactor.source_id]), "eaggl-capped-v1");
  assert.deepEqual(next.eaggl_anchors.map(anchor => anchor.reference.source_id), [kpnFactor.source_id]);
  assert.deepEqual(next.dismissed_source_ids, ["factor:portal:X:cfde-inc-v2:Factor2"]);
  assert.equal(next.model, "eaggl-capped-v1");
  assert.deepEqual(Object.keys(next), Object.keys(composer));
});

test("a new analysis copies the gap, inquiry and knowledge graphs with no anchors", t => {
  withStorage(t); observeFactors([kpnFactor]);
  const composer = currentAnalysisComposer(archivedOutcome.source_gap, { selected_kgs: ["prokn"], mechanism_subquery: "insulin secretion" });
  assert.deepEqual(composer, { source_gap: archivedOutcome.source_gap, eaggl_anchors: [], dismissed_source_ids: [], mechanism_subquery: "insulin secretion", model: "eaggl-capped-v1", selected_kgs: ["prokn"] });
  assert.notEqual(composer.source_gap, archivedOutcome.source_gap);
  assert.deepEqual(currentAnalysisComposer(archivedOutcome.source_gap).selected_kgs, ["biomarkerkg", "prokn"]);
});

test("archived anchors display from the stamp and superseded reads from the frozen factor", () => {
  const [anchor] = archive.reference.anchors;
  assert.deepEqual(outdatedFromAnchor(anchor), { source_id: anchor.source_id, name: anchor.label, trait: "Coronary artery disease in type 2 diabetes (CAD in T2D)", archive_id: anchor.archived_reference_factor_id });
  assert.equal(outdatedFromAnchor({ ...anchor, label: null, trait: null }).name, anchor.name);
  assert.equal(outdatedFromAnchor({ ...anchor, label: null, name: null, trait: null }).trait, "CADinT2D");
  const frozen = superseded.archived_reference_factor!;
  const display = outdatedFromFactor(frozen.source_id, frozen);
  assert.equal(display.name, frozen.label); assert.equal(display.trait, "Coronary artery disease in type 2 diabetes (CAD in T2D)");
  // Nothing captured: keep what the browser already knew, else the native identity.
  assert.deepEqual(outdatedFromFactor(kpnFactor.source_id, null, { name: "Cached label", trait: "Type 2 diabetes (T2D)" }), { source_id: kpnFactor.source_id, name: "Cached label", trait: "Type 2 diabetes (T2D)", archive_id: null });
  assert.deepEqual(outdatedFromFactor(kpnFactor.source_id, null), { source_id: kpnFactor.source_id, name: "Mechanism anchor", trait: "KPN.TRAIT:0000398", archive_id: null });
  // A KPN snapshot's `trait` is the EAGGL/portal code: show its KPN phenotype name, else what the browser showed.
  const kpnFrozen = { ...frozen, source_id: kpnFactor.source_id, model: "eaggl-capped-v1" as const, trait: "2hrG", kpn_trait_id: "KPN.TRAIT:0000007",
    metadata: { label: frozen.label, kpn: { phenotype_name: "2-hour glucose" } } };
  assert.equal(outdatedFromFactor(kpnFrozen.source_id, kpnFrozen).trait, "2-hour glucose");
  assert.equal(outdatedFromFactor(kpnFrozen.source_id, { ...kpnFrozen, metadata: {} }, { trait: "2-hour glucose (cached)" }).trait, "2-hour glucose (cached)");
  assert.equal(outdatedFromFactor(kpnFrozen.source_id, { ...kpnFrozen, metadata: {} }).trait, "2hrG");
  // A later KPN generation can reuse a public id with a new revision.
  assert.notEqual(anchorKey({ source_id: kpnFactor.source_id, source_revision: "a" }), anchorKey({ source_id: kpnFactor.source_id, source_revision: "b" }));
});

test("reference problems keep their body and read as clear user-facing copy", () => {
  const response = (status: number) => new Response(null, { status });
  let failure: unknown;
  try { unwrap({ error: superseded, response: response(410) }); } catch (error) { failure = error; }
  assert.ok(failure instanceof ApiError && supersededReference(failure));
  assert.equal(failure.status, 410);
  assert.equal(failure.problem?.archived_reference_factor?.source_id, "factor:portal:CADinT2D:cfde-inc-v2:Factor1");
  assert.match(failure.message, /outdated EAGGL reference/);
  assert.throws(() => unwrap({ error: { ...superseded, status: 409, archived_reference_factor: undefined }, response: response(409) }), /Start a new analysis on this knowledge gap with current factors/);
  assert.throws(() => unwrap({ error: { code: "REFERENCE_RELOAD_IN_PROGRESS", detail: "Reference data is being reloaded. Retry shortly." }, response: response(503) }), /being updated.*try again in a few minutes/);
  // Other problems keep the server detail, and a version conflict is not a superseded reference.
  assert.throws(() => unwrap({ error: { code: "VERSION_CONFLICT", detail: "This draft changed in another tab." }, response: response(409) }), (error: unknown) => error instanceof ApiError && error.message === "This draft changed in another tab." && !supersededReference(error));
  assert.equal(referenceProblemMessage(404, "NOT_FOUND"), null);
});

test("listing summaries are archived only when stamped", () => {
  assert.deepEqual(archivedAccounts.items.map(isArchived), archivedAccounts.items.map(item => !!item.archive));
  assert.ok(archivedAccounts.items.some(isArchived));
  assert.equal(isArchived(archivedOutcome), true); assert.equal(isArchived({}), false); assert.equal(isArchived(null), false);
});

test("workspace listings cache each reference filter and send it only when filtered", async t => {
  assert.equal(workspaceKey("accounts"), "accounts"); assert.equal(workspaceKey("accounts", "all"), "accounts");
  assert.equal(workspaceKey("explorations", "archived"), "explorations:archived"); assert.equal(workspaceKey("gaps", "archived"), "gaps");
  assert.deepEqual(workspaceKeys(affectedWorkspaceTabs({ collections: ["accounts"] } as never)), ["accounts", "accounts:current", "accounts:archived"]);
  assert.deepEqual(workspaceKeys(["gaps"]), ["gaps"]);
  const seen: unknown[] = [];
  const page = { next_cursor: null, has_more: false, snapshot_id: "test" };
  t.mock.method(api, "accounts", async (_cursor?: string, _signal?: AbortSignal, reference?: string) => { seen.push(reference); return { items: archivedAccounts.items, page }; });
  t.mock.method(api, "outcomes", async (_cursor?: string, _signal?: AbortSignal, reference?: string) => { seen.push(reference); return { items: [], page }; });
  const signal = new AbortController().signal;
  const archivedOnly = await loadWorkspaceData("accounts:archived", undefined, "refresh", signal);
  await loadWorkspaceData("accounts", undefined, "refresh", signal);
  await loadWorkspaceData("explorations:current", undefined, "refresh", signal);
  assert.deepEqual(seen, ["archived", "all", "current"]);
  assert.equal(archivedOnly.accounts.length, archivedAccounts.items.length);
});

test("only a reference cutover event rechecks anchors, now and after the API swaps catalogs", t => {
  const event = (entity_id: string, collections = ["catalog", "accounts", "gaps", "explorations"]) => ({ event_type: "catalog.updated", entity_id, entity_revision: 1, operation: "invalidate", collections });
  assert.equal(isReferenceReload(event("reference")), true);
  // Publishing or unpublishing emits a public catalog event too: no anchor reads.
  assert.equal(isReferenceReload(event("catalog")), false);
  assert.equal(isReferenceReload(event("reference", ["drafts"])), false);
  assert.equal(isReferenceReload(undefined), false);
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let checks = 0;
  const rechecks = referenceRechecker(() => { checks++; });
  rechecks.schedule();
  t.mock.timers.tick(0); assert.equal(checks, 1);
  t.mock.timers.tick(7_000); assert.equal(checks, 2);  // after the 5 s generation poll and the reload
  rechecks.schedule();  // a new cutover event restarts the schedule
  t.mock.timers.tick(0); assert.equal(checks, 3);
  t.mock.timers.tick(30_000); assert.equal(checks, 5);
  rechecks.cancel();  // unmounted: nothing further
  t.mock.timers.tick(200_000); assert.equal(checks, 5);
  assert.deepEqual([...referenceRecheckDelaysMs], [0, 7_000, 30_000, 120_000]);
});

test("a saved draft dropped at a cutover (404) keeps the edits as a new draft", async () => {
  const snapshot = { ...emptyComposer(), source_gap: archivedOutcome.source_gap };
  const saved = { id: "dropped-draft", version: 3, composer: emptyComposer() } as unknown as Schema<"Draft">;
  const created = { id: "new-draft", version: 1, composer: snapshot } as unknown as Schema<"Draft">;
  const calls: string[] = [];
  const keys: string[] = [];
  const client = (failure: unknown) => ({
    saveDraft: async (draft: Schema<"Draft">) => { calls.push("patch:" + draft.id); throw failure; },
    createDraft: async () => { calls.push("create"); return created; },
  });
  const key = (body: string) => { keys.push(body); return "key-" + keys.length; };
  let gone = 0;
  const result = await persistDraft(client(new ApiError(404, "NOT_FOUND", "The requested resource is unavailable.")), saved, snapshot, key, { gone: () => { gone++; } });
  assert.deepEqual(result, { draft: created, replaced: "dropped-draft" });
  assert.deepEqual(calls, ["patch:dropped-draft", "create"]); assert.equal(gone, 1);
  assert.deepEqual(JSON.parse(keys[1]), { composer: snapshot });  // the same key as a first save of this snapshot
  // Other failures, and a save superseded meanwhile, are not replaced.
  calls.length = 0;
  await assert.rejects(persistDraft(client(new ApiError(409, "VERSION_CONFLICT", "changed")), saved, snapshot, key), (error: ApiError) => error.status === 409);
  await assert.rejects(persistDraft(client(new ApiError(404, "NOT_FOUND", "gone")), saved, snapshot, key, { current: () => false }), (error: ApiError) => error.status === 404);
  assert.deepEqual(calls, ["patch:dropped-draft", "patch:dropped-draft"]);
  assert.deepEqual(await persistDraft(client(new Error("unused")), null, snapshot, key), { draft: created, replaced: null });
});
