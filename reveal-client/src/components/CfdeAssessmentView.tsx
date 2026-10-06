import React from "react";
import { assessmentIsRunning, assessmentResult, assessmentSupportLabel, type AssessmentState } from "../lib/cfde-assessment";

export function CfdeEstimateRing({ probability, loading = false }: { probability?: number; loading?: boolean }) {
  return <svg className={`cfde-estimate-ring${loading ? " is-assessing" : ""}`} viewBox="0 0 48 48" aria-hidden="true" focusable="false">
    <circle className="cfde-ring-track" cx="24" cy="24" r="22" />
    <circle className="cfde-ring-value" cx="24" cy="24" r="22" pathLength="100" strokeDasharray={loading ? "24 100" : `${(probability ?? 0) * 100} 100`} />
  </svg>;
}

export function CfdeAssessmentFeedback({ state, disabled, onCheck }: { state: AssessmentState; disabled: boolean; onCheck: () => void }) {
  const result = assessmentResult(state);
  const assessing = state.phase === "starting" || state.phase === "polling";
  const resume = !!state.resource && assessmentIsRunning(state.resource) && !state.resource.stale;
  const recovery = state.phase === "error";
  if (!result && !recovery && !assessing) return null;
  return <div className="cfde-assessment-feedback">
    <div className="cfde-assessment-status" role="status" aria-live="polite">
      {result?.result && <span className="cfde-support-label">{assessmentSupportLabel(result.result.probability_yes)}</span>}
      {assessing && <span className="cfde-assessing-label">Assessing likely CFDE support</span>}
      {recovery && <span>{state.message}</span>}
    </div>
    {recovery && <button type="button" className="cfde-assessment-check" disabled={disabled} onClick={onCheck}>{resume ? "Check status" : "Retry support check"}</button>}
  </div>;
}
