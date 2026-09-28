"use client";

import { useEffect, useState, type ReactNode } from "react";
import "./loading-surface.css";

/** Shared waiting vocabulary for saved records, discovery and workspace reads. */
export function LoadingPulse() {
  return <span className="loading-surface-pulse" aria-hidden="true"><i /><i /><i /></span>;
}

export function LoadingStatus({ children }: { children: ReactNode }) {
  return <span className="loading-inline-status" role="status" aria-live="polite"><LoadingPulse /><span>{children}</span></span>;
}

export function LoadingSurface({ title, description, error, onRetry, skeleton = "rows", compact = false, rows = 3 }: {
  title: string;
  description?: string;
  error?: string;
  onRetry?: () => void;
  skeleton?: "rows" | "record" | "none";
  compact?: boolean;
  rows?: number;
}) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    setSlow(false);
    if (error) return;
    const timer = setTimeout(() => setSlow(true), 8_000);
    return () => clearTimeout(timer);
  }, [title, error]);
  return <div className={`loading-surface${compact ? " is-compact" : ""}${error ? " has-error" : ""}`}>
    <div className="loading-surface-panel">
      {!compact && <span className="loading-surface-symbol" aria-hidden="true"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8Z" /><path d="M14 3v5h5M9 12h6M9 16h4" /></svg></span>}
      <div className="loading-surface-copy">
        <div role={error ? "alert" : "status"} aria-live={error ? "assertive" : "polite"} aria-atomic="true">
          <p className="loading-surface-title">{title}</p>
          {(error || description) && <p className="loading-surface-description">{error || description}</p>}
          {slow && !error && <p className="loading-surface-delay">This is taking a little longer. We’re still waiting for a response.</p>}
        </div>
        {error && onRetry && <button type="button" className="loading-surface-retry" onClick={onRetry}>Retry <span aria-hidden="true">↻</span></button>}
      </div>
      {!error && <LoadingPulse />}
    </div>
    {!error && skeleton !== "none" && <div className={`loading-surface-skeleton is-${skeleton}`} aria-hidden="true">
      {Array.from({ length: Math.max(1, Math.min(rows, 5)) }, (_, index) => <div className="loading-surface-row" key={index}><span className="loading-surface-line is-title" /><span className="loading-surface-line" /><span className="loading-surface-line is-short" /></div>)}
    </div>}
  </div>;
}
