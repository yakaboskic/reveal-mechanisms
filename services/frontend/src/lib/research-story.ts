/** Presentation state only: this story never votes, publishes, or launches research. */
export const STORY_STEPS = 7;
export const STORY_DWELL_MS = 6500;

export type StoryState = {
  step: number;
  wantsPlayback: boolean;
  started: boolean;
  visible: boolean;
  foreground: boolean;
  motion: "unknown" | "full" | "reduced";
  replayVersion: number;
};
export const initialStoryState: StoryState = {
  step: 0, wantsPlayback: false, started: false, visible: false, foreground: false, motion: "unknown", replayVersion: 0,
};
export type StoryAction =
  | { type: "motion"; reduced: boolean }
  | { type: "visible" | "foreground"; value: boolean }
  | { type: "seek"; step: number }
  | { type: "tick" | "pause" | "toggle" | "replay" };

export function storyPlaying(state: StoryState): boolean {
  return state.wantsPlayback && state.visible && state.foreground && state.motion !== "unknown";
}

export function researchStoryReducer(state: StoryState, action: StoryAction): StoryState {
  let next = state;
  switch (action.type) {
    case "motion":
      next = { ...state, motion: action.reduced ? "reduced" : "full" };
      if (action.reduced) next = { ...next, step: state.started ? state.step : STORY_STEPS - 1, wantsPlayback: false, started: true };
      break;
    case "visible": case "foreground":
      next = { ...state, [action.type]: action.value };
      break;
    case "seek":
      if (!Number.isFinite(action.step)) return state;
      return { ...state, step: Math.max(0, Math.min(STORY_STEPS - 1, Math.floor(action.step))), wantsPlayback: false, started: true };
    case "pause":
      return { ...state, wantsPlayback: false, started: true };
    case "toggle":
      return { ...state, step: state.step === STORY_STEPS - 1 ? 0 : state.step, wantsPlayback: !storyPlaying(state), started: true };
    case "replay":
      return { ...state, step: 0, wantsPlayback: true, started: true, replayVersion: state.replayVersion + 1 };
    case "tick": {
      if (!storyPlaying(state)) return state;
      const step = Math.min(STORY_STEPS - 1, state.step + 1);
      return { ...state, step, wantsPlayback: step < STORY_STEPS - 1 };
    }
  }
  // Begin once, only after both user preference and viewport visibility are known.
  if (!next.started && next.motion === "full" && next.visible && next.foreground) {
    return { ...next, started: true, wantsPlayback: true };
  }
  return next;
}
