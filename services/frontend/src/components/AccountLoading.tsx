"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import "./scientific.css";
import "./account-loading.css";

export function AccountNavigation() {
  return <nav className="account-topbar" aria-label="Account navigation">
    <Link href="/workspace?tab=accounts">← Your scientific accounts</Link>
    <Link href="/">Search knowledge gaps</Link>
  </nav>;
}

export function AccountLoading({ embedded = false, error, onRetry }: {
  embedded?: boolean;
  error?: string;
  onRetry?: () => void;
}) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    setSlow(false);
    if (error) return;
    const timer = setTimeout(() => setSlow(true), 8_000);
    return () => clearTimeout(timer);
  }, [error]);

  return <section className={`scientific account-study account-loading${embedded ? " embedded" : ""}`} aria-label="Scientific account">
    {!embedded && <AccountNavigation />}
    <div className={`account-loading-panel${error ? " has-error" : ""}`}>
      <div className="account-loading-symbol" aria-hidden="true">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8Z" />
          <path d="M14 3v5h5M9 12h6M9 16h4" />
        </svg>
      </div>
      <div className="account-loading-copy">
        <div role={error ? "alert" : "status"} aria-live={error ? "assertive" : "polite"} aria-atomic="true">
          <h1>{error ? "Couldn’t open this account" : slow ? "Still loading your account" : "Opening scientific account"}</h1>
          <p>{error || (slow ? "This is taking a little longer. We’re still retrieving your saved account." : "Loading conclusions, claims and supporting evidence.")}</p>
        </div>
        {error && <div className="account-loading-recovery">
          {onRetry && <button className="account-loading-retry" onClick={onRetry}>Retry <span aria-hidden="true">↻</span></button>}
          <span>Your saved account is unchanged.</span>
        </div>}
      </div>
      {!error && <span className="account-loading-pulse" aria-hidden="true"><i /><i /><i /></span>}
    </div>
    {!error && <div className="account-skeleton" aria-hidden="true">
      <div className="account-skeleton-question">
        <span className="account-skeleton-line skeleton-eyebrow" />
        <span className="account-skeleton-line skeleton-title" />
        <span className="account-skeleton-line skeleton-title-short" />
        <div className="account-skeleton-mechanisms"><span /><span /><span /></div>
      </div>
      <div className="account-skeleton-tabs"><span>Conclusions</span><span>Research statement</span></div>
      <div className="account-skeleton-prose">
        <span className="account-skeleton-line skeleton-heading" />
        <span className="account-skeleton-line" /><span className="account-skeleton-line" />
        <span className="account-skeleton-line" /><span className="account-skeleton-line skeleton-ending" />
        <span className="account-skeleton-line skeleton-attribution" />
      </div>
      <div className="account-skeleton-claims"><span>Associated claims</span><span className="account-skeleton-line" /></div>
    </div>}
  </section>;
}
