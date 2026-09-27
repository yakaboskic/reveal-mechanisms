"use client";

import { useEffect, useRef, useState } from "react";
import "./submission-progress.css";

export type SubmissionProgressProps = {
  stage: "signing-in" | "saving" | "submitting";
  provider?: "anonymous" | "google" | "orcid" | "session";
  question?: string;
  error?: string;
  onRetry: () => void;
  onBack: () => void;
  retryLabel?: string;
};

const signInMessages = {
  anonymous: "Starting your anonymous session…",
  google: "Continuing with Google…",
  orcid: "Continuing with ORCID…",
  session: "Checking your session…",
};

export function SubmissionProgress({
  stage,
  provider = "session",
  question,
  error,
  onRetry,
  onBack,
  retryLabel = "Retry",
}: SubmissionProgressProps) {
  const heading = useRef<HTMLHeadingElement>(null);
  const [takingLonger, setTakingLonger] = useState(false);
  const hasError = Boolean(error?.trim());
  const stageMessage = stage === "signing-in"
    ? signInMessages[provider]
    : stage === "saving"
      ? "Saving your question and mechanism anchors…"
      : "Opening your research activity…";

  useEffect(() => { heading.current?.focus(); }, [error]);

  useEffect(() => {
    setTakingLonger(false);
    if (hasError) return;
    // Keep elapsed waiting time across stages; only a retry starts it over.
    const timer = window.setTimeout(() => setTakingLonger(true), 12_000);
    return () => window.clearTimeout(timer);
  }, [hasError]);

  return (
    <main
      id="main"
      className="submission-page"
      data-submission-stage={stage}
      data-submission-state={hasError ? "error" : "pending"}
    >
      <section className="submission-progress-content" aria-labelledby="submission-progress-heading">
        <div className="submission-progress-indicator" aria-hidden="true">
          {hasError ? (
            <svg className="submission-progress-attention" viewBox="0 0 32 32" fill="none">
              <circle cx="16" cy="16" r="12" />
              <path d="M16 9.5v8M16 22v.5" />
            </svg>
          ) : <span className="submission-progress-spinner" />}
        </div>

        <h1 id="submission-progress-heading" ref={heading} tabIndex={-1}>Preparing your research</h1>

        {hasError ? (
          <>
            <p className="submission-progress-error" role="alert">{error}</p>
            <div className="submission-progress-actions">
              <button type="button" className="submission-progress-retry" onClick={onRetry}>
                {retryLabel.trim() || "Retry"}
              </button>
              <button type="button" className="submission-progress-back" onClick={onBack}>Back to question</button>
            </div>
          </>
        ) : (
          <>
            <p className="submission-progress-stage" role="status" aria-live="polite" aria-atomic="true">
              {stageMessage}
            </p>
            <p className="submission-progress-explanation">Your research activity will open here once your request is ready.</p>
            <p className="submission-progress-delay" role="status" aria-live="polite" aria-atomic="true">
              {takingLonger ? "This is taking a little longer. A slow connection can add to the wait." : ""}
            </p>
          </>
        )}

        {question?.trim() && <p className="submission-progress-question">{question}</p>}
      </section>
    </main>
  );
}
