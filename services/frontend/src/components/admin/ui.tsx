"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React, { useState, type ReactNode } from "react";
import Link from "next/link";
import { adminDate, formatAdminDate } from "@/lib/admin-time";

// The admin console's building blocks, ported from the claims console's components/admin/shared.tsx into plain CSS
// (app/admin/admin.css, scoped under .admin-root). Colour is used only for status.

export function PageHeading({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return <div className="admin-page-heading">
    <div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>
    {actions && <div className="admin-page-actions">{actions}</div>}
  </div>;
}

/** A titled card. The description says in plain words what the content is and where it comes from. */
export function Panel({ title, description, action, footer, padded = false, children, label }: {
  title: string; description?: ReactNode; action?: ReactNode; footer?: ReactNode; padded?: boolean; children: ReactNode; label?: string;
}) {
  return <section className="admin-panel" aria-label={label ?? title}>
    <header><div><h2>{title}</h2>{description && <p>{description}</p>}</div>{action && <div className="admin-panel-action">{action}</div>}</header>
    <div className={padded ? "admin-panel-body padded" : "admin-panel-body"}>{children}</div>
    {footer && <footer>{footer}</footer>}
  </section>;
}

/** A headline number; with `href` the whole card links to the list it counts. */
export function Metric({ title, value, hint, href }: { title: string; value: ReactNode; hint?: ReactNode; href?: string }) {
  const content = <><span className="admin-metric-title">{title}</span><strong>{value}</strong>{hint && <span className="admin-metric-hint">{hint}</span>}</>;
  return href ? <Link className="admin-metric" href={href}>{content}</Link> : <div className="admin-metric">{content}</div>;
}
export const MetricGrid = ({ children, label }: { children: ReactNode; label: string }) => <div className="admin-metrics" role="group" aria-label={label}>{children}</div>;

export type Tone = "neutral" | "good" | "warn" | "bad" | "info";
const TONES = new Set<string>(["neutral", "good", "warn", "bad", "info"]);
/** A tone by name; anything else (such as depositOutcome's "") is neutral. */
export const asTone = (value: string | null | undefined): Tone => value && TONES.has(value) ? value as Tone : "neutral";

/** The tone of a job, import or event state: finished well, finished badly, needs a look, or still running. */
export function statusTone(value: string | null | undefined): Tone {
  switch (value) {
    case "succeeded": case "ready": case "complete": case "completed": case "deposited": case "passed": return "good";
    case "failed": case "error": case "withheld": case "withheld_unsafe": return "bad";
    case "insufficient_evidence": case "cancelled": case "expired": return "warn";
    case "queued": case "running": case "pending": case "claimed": case "started": case "starting": case "in_progress": case "loading": return "info";
    default: return "neutral";
  }
}

export function Status({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`admin-status ${tone}`} title={title}>{children}</span>;
}
/** A recorded state in words ("insufficient evidence"), toned by what it means. */
export const StateStatus = ({ value }: { value: string }) => <Status tone={statusTone(value)}>{value.replaceAll("_", " ")}</Status>;

/** A block-level notice in one of the status tones. */
export function Callout({ tone = "neutral", role, children }: { tone?: Tone; role?: "alert" | "status"; children: ReactNode }) {
  return <div className={`admin-callout ${tone}`} role={role}>{children}</div>;
}

/** A definition list as a two-column grid. */
export function KeyValues({ rows }: { rows: [string, ReactNode][] }) {
  return <dl className="admin-key-values">{rows.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value ?? "—"}</dd></div>)}</dl>;
}

/** Preformatted text that wraps and scrolls inside a capped height. */
export const Pre = ({ children }: { children: ReactNode }) => <pre className="admin-pre">{children}</pre>;
/** Raw JSON goes last, folded away: the readable fields come first. */
export function Json({ value, label = "Raw JSON", open = false }: { value: unknown; label?: string; open?: boolean }) {
  return <details className="admin-json" open={open}><summary>{label}</summary><Pre>{JSON.stringify(value, null, 2)}</Pre></details>;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [state, setState] = useState("");
  return <button type="button" className="admin-copy" aria-label={`${label}: ${text}`} onClick={async () => {
    try { await navigator.clipboard.writeText(text); setState("Copied"); } catch { setState("Copy failed"); }
    setTimeout(() => setState(""), 1200);
  }}>{state || label}</button>;
}

/** An id shortened on screen to its first `length` characters; hover shows it whole, and the button copies it. */
export function ShortId({ value, length = 8, href, copy = true }: { value: string | null | undefined; length?: number; href?: string; copy?: boolean }) {
  if (!value) return <span className="admin-muted">—</span>;
  const short = value.length > length ? value.slice(0, length) : value;
  return <span className="admin-short-id">
    {href ? <Link href={href} title={value}>{short}</Link> : <code title={value}>{short}</code>}
    {copy && <CopyButton text={value} />}
  </span>;
}

/** Twitter-style age: "now", "10m", "5h", "3d", then "Oct 3" (with the year when it is not this year). */
export function shortAge(value: Date, now = Date.now()): string {
  const minutes = Math.floor((now - value.getTime()) / 60_000);
  if (minutes < 1) return minutes < -1 ? "soon" : "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)}h`;
  if (minutes < 60 * 24 * 7) return `${Math.floor(minutes / (60 * 24))}d`;
  return value.toLocaleDateString("en-US", { month: "short", day: "numeric", ...(value.getFullYear() === new Date(now).getFullYear() ? {} : { year: "numeric" }) });
}

/** A timestamp as a short age; hover shows the full local time with its UTC offset. */
export function Age({ value, empty = "—", now }: { value: string | null | undefined; empty?: string; now?: number }) {
  const date = adminDate(value);
  if (!value || !date) return <span className="admin-muted">{empty}</span>;
  return <time className="admin-age" dateTime={date.toISOString()} title={formatAdminDate(value)}>{shortAge(date, now)}</time>;
}

/** An empty list, said in words. */
export const Empty = ({ children }: { children: ReactNode }) => <p className="admin-empty">{children}</p>;

/** Previous and next as links, so each page has its own URL. */
export function Pagination({ page, pages, total, noun = "result", href }: { page: number; pages: number; total: number; noun?: string; href: (page: number) => string }) {
  return <nav className="admin-pagination" aria-label="Pages">
    <span>{total.toLocaleString()} {total === 1 ? noun : `${noun}s`} · Page {page.toLocaleString()} of {pages.toLocaleString()}</span>
    <span>
      {page > 1 ? <Link className="submit" href={href(page - 1)} scroll={false} rel="prev">Previous</Link> : <span className="submit" aria-disabled="true">Previous</span>}
      {page < pages ? <Link className="submit" href={href(page + 1)} scroll={false} rel="next">Next</Link> : <span className="submit" aria-disabled="true">Next</span>}
    </span>
  </nav>;
}

/** Filters in one row. */
export const FilterRow = ({ children, label = "Filters" }: { children: ReactNode; label?: string }) => <div className="admin-filter-row" role="search" aria-label={label}>{children}</div>;

export function SelectFilter({ label, value, options, onChange, all = "All" }: {
  label: string; value: string; options: ([string, string] | string)[]; onChange: (value: string) => void; all?: string;
}) {
  return <label className="admin-select"><span>{label}</span>
    <select aria-label={label} value={value} onChange={e => onChange(e.target.value)}>
      <option value="">{all}</option>
      {options.map(option => { const [v, text] = typeof option === "string" ? [option, option] : option; return <option key={v} value={v}>{text}</option>; })}
    </select>
  </label>;
}

export function SearchFilter({ label, value, placeholder, onChange }: { label: string; value: string; placeholder: string; onChange: (value: string) => void }) {
  return <label className="admin-search"><span className="sr-only">{label}</span>
    <input className="field" type="search" aria-label={label} placeholder={placeholder} value={value} onChange={e => onChange(e.target.value)} />
  </label>;
}

/** A table with hairline rows and muted headers; it scrolls sideways rather than squeezing its columns. */
export function Table({ label, headings, children, className = "" }: { label: string; headings: string[]; children: ReactNode; className?: string }) {
  return <div className={`admin-table-scroll ${className}`.trim()} tabIndex={0}>
    <table aria-label={label}><thead><tr>{headings.map(h => <th key={h} scope="col">{h}</th>)}</tr></thead><tbody>{children}</tbody></table>
  </div>;
}
