import test from "node:test";
import assert from "node:assert/strict";
import { initialStoryState, researchStoryReducer as reduce, storyPlaying, STORY_STEPS, type StoryState } from "../src/lib/research-story";

function ready(reduced = false): StoryState {
  let state = reduce(initialStoryState, { type: "foreground", value: true });
  state = reduce(state, { type: "visible", value: true });
  return reduce(state, { type: "motion", reduced });
}

test("autoplay waits for motion preference, visible diagram and foreground tab", () => {
  let state = reduce(initialStoryState, { type: "visible", value: true });
  assert.equal(storyPlaying(state), false);
  state = reduce(state, { type: "motion", reduced: false });
  assert.equal(storyPlaying(state), false);
  state = reduce(state, { type: "foreground", value: true });
  assert.equal(storyPlaying(state), true);
  assert.equal(state.step, 0);
});

test("offscreen and hidden tabs cannot advance, returning resumes the same stage", () => {
  for (const type of ["visible", "foreground"] as const) {
    let state = reduce(ready(), { type: "tick" });
    state = reduce(state, { type, value: false });
    assert.equal(storyPlaying(state), false);
    assert.equal(reduce(state, { type: "tick" }).step, 1);
    state = reduce(state, { type, value: true });
    assert.equal(state.step, 1);
    assert.equal(storyPlaying(state), true);
  }
});

test("manual navigation and inspection prevent automatic restart on reentry", () => {
  for (const action of [{ type: "seek", step: 4 }, { type: "pause" }] as const) {
    let state = reduce(ready(), action);
    state = reduce(state, { type: "visible", value: false });
    state = reduce(state, { type: "visible", value: true });
    assert.equal(storyPlaying(state), false);
    assert.equal(reduce(state, { type: "tick" }).step, state.step);
    assert.equal(storyPlaying(reduce(state, { type: "toggle" })), true);
  }
});

test("the story ends on the complete overview and replay starts intentionally", () => {
  let state = ready();
  for (let i = 0; i < STORY_STEPS + 2; i++) state = reduce(state, { type: "tick" });
  assert.equal(state.step, STORY_STEPS - 1);
  assert.equal(storyPlaying(state), false);
  state = reduce(state, { type: "visible", value: false });
  state = reduce(state, { type: "visible", value: true });
  assert.equal(state.step, STORY_STEPS - 1);
  assert.equal(storyPlaying(state), false);
  state = reduce(state, { type: "replay" });
  assert.equal(state.step, 0);
  assert.equal(storyPlaying(state), true);
});

test("reduced motion opens the complete overview with manual navigation", () => {
  let state = ready(true);
  assert.equal(state.step, STORY_STEPS - 1);
  assert.equal(storyPlaying(state), false);
  state = reduce(state, { type: "seek", step: 2 });
  assert.equal(state.step, 2);
  assert.equal(storyPlaying(state), false);
  // An explicit play/replay is allowed, with CSS motion still disabled.
  assert.equal(storyPlaying(reduce(state, { type: "toggle" })), true);
});

test("enabling reduced motion during playback stops without changing the current stage", () => {
  let state = reduce(ready(), { type: "tick" });
  state = reduce(state, { type: "motion", reduced: true });
  assert.equal(state.step, 1);
  assert.equal(storyPlaying(state), false);
  state = reduce(state, { type: "motion", reduced: false });
  assert.equal(storyPlaying(state), false);
});

test("manual stage selection stays within the story", () => {
  assert.equal(reduce(ready(), { type: "seek", step: -4 }).step, 0);
  assert.equal(reduce(ready(), { type: "seek", step: 99 }).step, STORY_STEPS - 1);
  assert.equal(reduce(ready(), { type: "seek", step: 2.8 }).step, 2);
  assert.deepEqual(reduce(ready(), { type: "seek", step: NaN }), ready());
});

test("replay renews the first stage dwell even while that stage is already playing", () => {
  const first = ready();
  const replay = reduce(first, { type: "replay" });
  assert.equal(replay.step, 0);
  assert.equal(storyPlaying(replay), true);
  assert.equal(replay.replayVersion, first.replayVersion + 1);
});
