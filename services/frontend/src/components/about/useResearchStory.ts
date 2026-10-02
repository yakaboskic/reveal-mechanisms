"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import { initialStoryState, researchStoryReducer, storyPlaying, STORY_DWELL_MS } from "@/lib/research-story";

export function useResearchStory() {
  const [state, dispatch] = useReducer(researchStoryReducer, initialStoryState);
  const stageRef = useRef<HTMLDivElement>(null);
  const playing = storyPlaying(state);

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const motion = () => dispatch({ type: "motion", reduced: media.matches });
    const visibility = () => dispatch({ type: "foreground", value: document.visibilityState === "visible" });
    motion(); visibility();
    media.addEventListener("change", motion);
    document.addEventListener("visibilitychange", visibility);
    const observer = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(entries => {
      dispatch({ type: "visible", value: entries.some(entry => entry.isIntersecting && entry.intersectionRatio >= 0.15) });
    }, { threshold: [0, 0.15] });
    if (stageRef.current && observer) observer.observe(stageRef.current);
    else dispatch({ type: "visible", value: true });
    return () => {
      observer?.disconnect();
      media.removeEventListener("change", motion);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => dispatch({ type: "tick" }), STORY_DWELL_MS);
    return () => window.clearTimeout(timer);
  }, [playing, state.step, state.replayVersion]);

  const goTo = useCallback((step: number) => dispatch({ type: "seek", step }), []);
  const toggle = useCallback(() => dispatch({ type: "toggle" }), []);
  const replay = useCallback(() => dispatch({ type: "replay" }), []);
  const pause = useCallback(() => dispatch({ type: "pause" }), []);
  return { step: state.step, playing, reducedMotion: state.motion === "reduced", stageRef, goTo, toggle, replay, pause };
}
