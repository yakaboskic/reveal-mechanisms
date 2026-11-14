"use client";
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { formatAdminDate as date } from "../lib/admin-time";
import {
  LIMIT_FIELDS, fallbackLimit, formatLimitValue as shown, isLimitsView, limitFieldId, limitFields, limitsForm, supportedRange,
  type AgentLimit, type AgentLimitsView, type LimitField, type LimitFields, type LimitsChange,
} from "../lib/admin-limits";

const ENDPOINT = "/api/admin/agent-limits";
const HEADINGS = ["Limit", "Default in effect", "Default set here", "Ceiling in effect", "Ceiling set here", "Supported range"];
const unset = (value: number | null | undefined) => value == null ? "not set" : shown(value);

/** The value a job gets now and where it comes from: this console, an environment variable or the code. */
function Effective({ limit, field }: { limit: AgentLimit; field: LimitField }) {
  const resolved = limit[field];
  return <td><strong className="admin-limit-value">{shown(resolved.value)}</strong> {limit.unit}
    <small>{resolved.source === "console" ? "Set in this console" : resolved.source === "environment" ? <>Environment <span className="admin-mono">{resolved.environment}</span></> : "Built-in"}</small>
    {resolved.lowered_to_ceiling && <small>Lowered to the ceiling; the {resolved.source === "console" ? "console" : resolved.source === "environment" ? "environment" : "built-in"} default is higher.</small>}
    {resolved.ignored_environment_value && <small><span className="admin-warning"><span className="admin-mono">{resolved.environment}</span> is set to an unusable value and is ignored.</span></small>}
  </td>;
}

type FieldProps = { limit: AgentLimit; field: LimitField; text: string; error?: string; changed: boolean; disabled: boolean; prefix: string; onChange: (text: string) => void };
function Field({ limit, field, text, error, changed, disabled, prefix, onChange }: FieldProps) {
  const fallback = fallbackLimit(limit, field), resolved = limit[field], saved = limit.console[field];
  const note = [
    changed && (saved == null ? "Unsaved." : `Unsaved; the saved value is ${shown(saved)}.`),
    !text.trim() && (fallback.known
      ? `Not set here: the ${resolved.source === "environment" ? `environment variable ${resolved.environment}` : "built-in value"} applies.`
      : `Not set here: ${resolved.environment} applies if it is set, otherwise the built-in ${shown(fallback.value)}.`),
  ].filter(Boolean).join(" ");
  const noteId = `${prefix}-${limit.key}-${field}-note`, errorId = `${prefix}-${limit.key}-${field}-error`;
  return <td>
    <div className="admin-limit-field">
      <input className="field" type="text" inputMode={limit.type === "integer" ? "numeric" : "decimal"} autoComplete="off" spellCheck={false}
        aria-label={`${limit.label} ${field}, in ${limit.unit}`} aria-invalid={error ? true : undefined}
        aria-describedby={[error && errorId, note && noteId].filter(Boolean).join(" ") || undefined}
        placeholder={fallback.known ? String(fallback.value) : "Not set"} value={text} disabled={disabled} onChange={e => onChange(e.target.value)} />
      <button type="button" className="text-button" disabled={disabled || !text} aria-label={`Clear the console's ${limit.label} ${field}`} onClick={() => onChange("")}>Clear</button>
    </div>
    {error && <small id={errorId} className="admin-limit-error">{error}</small>}
    {note && <small id={noteId}>{note}</small>}
  </td>;
}

type TableProps = { view: AgentLimitsView; fields: LimitFields; errors: Record<string, string>; changed: string[]; disabled: boolean; onChange: (key: string, field: LimitField, text: string) => void };
/** One row per limit. A text input, not type="number": that reports unparseable text as empty, which here means "clear". */
export function LimitsTable({ view, fields, errors, changed, disabled, onChange }: TableProps) {
  const prefix = useId();
  return <div className="admin-table-scroll" tabIndex={0}><table><thead><tr>{HEADINGS.map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>
    {view.limits.map(limit => <tr key={limit.key}>
      <td><strong>{limit.label}</strong><small>{limit.description}</small><small>Counted in {limit.unit}</small></td>
      {LIMIT_FIELDS.map(field => { const id = limitFieldId(limit.key, field); return <React.Fragment key={field}>
        <Effective limit={limit} field={field} />
        <Field limit={limit} field={field} text={fields[limit.key]?.[field] ?? ""} error={errors[id]} changed={changed.includes(id)} disabled={disabled} prefix={prefix} onChange={text => onChange(limit.key, field, text)} />
      </React.Fragment>; })}
      <td>{limit.type === "integer" ? "Whole number" : "Number"}<small>{supportedRange(limit)}</small></td>
    </tr>)}
  </tbody></table></div>;
}

export function LimitsHistory({ view }: { view: AgentLimitsView }) {
  const labels = new Map(view.limits.map(limit => [limit.key, limit.label]));
  if (!view.history.length) return <p className="admin-empty">No change has been saved from this console yet.</p>;
  return <div className="admin-table-scroll" tabIndex={0}><table><thead><tr><th>When</th><th>Who</th><th>What changed</th></tr></thead><tbody>
    {view.history.map((entry, index) => <tr key={`${entry.at}-${index}`}><td className="admin-date">{date(entry.at)}</td><td>{entry.by}</td>
      <td className="admin-limits-history">{Object.entries(entry.changes).flatMap(([key, change]) => LIMIT_FIELDS.filter(field => change[field]).map(field =>
        <div key={limitFieldId(key, field)}>{labels.get(key) || key} {field}: {unset(change[field]![0])} → {unset(change[field]![1])}</div>))}</td></tr>)}
  </tbody></table></div>;
}

/** Stays mounted once opened (hidden on other tabs) and owns its own request, so neither a tab switch nor the
 *  console's telemetry refresh touches unsaved edits. Reopening the tab reloads only when nothing is unsaved. */
export function AdminLimits({ active }: { active: boolean }) {
  const [view, setView] = useState<AgentLimitsView | null>(null);
  const [fields, setFields] = useState<LimitFields>({});
  const [busy, setBusy] = useState<"" | "loading" | "saving">("");
  const [problem, setProblem] = useState<{ message: string; conflict: boolean } | null>(null);
  const [saved, setSaved] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const form = useMemo(() => view ? limitsForm(view, fields) : null, [view, fields]);
  const edits = form?.changed.length || 0, invalid = Object.keys(form?.errors || {}).length;
  const unsaved = useRef(false); unsaved.current = edits > 0;

  const request = useCallback(async (change?: LimitsChange) => {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(change ? "saving" : "loading"); setSaved(false);
    try {
      const response = await fetch(ENDPOINT, change
        ? { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(change), cache: "no-store", signal: controller.signal }
        : { cache: "no-store", signal: controller.signal });
      const value = await response.json().catch(() => null);
      if (controller.signal.aborted) return;
      if (response.ok && isLimitsView(value)) { setView(value); setFields(limitFields(value)); setProblem(null); setSaved(!!change); return; }
      // The server's sentence is shown as sent: a 422 names the field and a 409 says to reload.
      const detail = typeof value?.error === "string" ? value.error : typeof value?.detail === "string" ? value.detail : "";
      const message = response.ok ? "The backend sent limits this console cannot read."
        : response.status === 404 ? "This backend does not offer editable agent job limits yet."
        : detail || (change ? "The limits could not be saved." : "The limits are unavailable.");
      setProblem({ message: change && response.status >= 500 ? `${message} Your edits are still here; save again.` : message, conflict: response.status === 409 });
    } catch {
      if (!controller.signal.aborted) setProblem({ message: change ? "The limits could not be saved. Your edits are still here; check the connection, then save again." : "The limits are unavailable. Check the connection, then reload.", conflict: false });
    } finally { if (pending.current === controller) { pending.current = null; setBusy(""); } }
  }, []);
  useEffect(() => () => { pending.current?.abort(); pending.current = null; }, []);
  useEffect(() => { if (active && !unsaved.current) void request(); }, [active, request]);

  function edit(key: string, field: LimitField, text: string) {
    setFields(current => ({ ...current, [key]: { ...(current[key] || { default: "", ceiling: "" }), [field]: text } }));
    setSaved(false); setProblem(current => current?.conflict ? current : null);
  }
  function save(event: React.FormEvent) {
    event.preventDefault();
    if (view && form && edits && !invalid) void request({ expected_version: view.version, values: form.values });
  }
  const status = busy === "saving" ? "Saving…" : saved ? "Saved. Jobs queued from now on use these limits."
    : invalid ? `Correct ${invalid} marked ${invalid === 1 ? "field" : "fields"} to save.`
    : edits ? `${edits} unsaved ${edits === 1 ? "change" : "changes"}.` : "";
  return <section className="admin-limits" hidden={!active} aria-label="General agent job limits">
    <div className="admin-section-heading"><div><h2>General agent job limits</h2><p>These limits apply to general agent jobs only. A change applies to jobs queued after it is saved. A request can lower any limit, or raise it up to the ceiling.</p></div>
      <button className="submit" type="button" disabled={!!busy || edits > 0} title={edits ? "Save or discard your edits first." : undefined} onClick={() => void request()}>{busy === "loading" ? "Loading…" : "Reload limits"}</button></div>
    {!view && !problem && <p role="status">Loading limits…</p>}
    {problem && <p role="alert" className="error">{problem.message}{problem.conflict && <button type="button" disabled={!!busy} onClick={() => void request()}>Reload limits and discard my edits</button>}</p>}
    {view && form && <>
      <p>{view.updated_at ? <>Last changed {date(view.updated_at)} by {view.updated_by || "an unrecorded admin"}.</> : "Nothing has been set from this console yet."} The default is what a request gets when it names no limit; the ceiling is the most a request may ask for. An empty field is not set here, so the environment variable or the built-in value applies.</p>
      <form noValidate onSubmit={save}>
        <LimitsTable view={view} fields={fields} errors={form.errors} changed={form.changed} disabled={!!busy} onChange={edit} />
        <div className="admin-limits-actions">
          <button className="submit" type="submit" disabled={!edits || !!invalid || !!busy}>{busy === "saving" ? "Saving…" : "Save changes"}</button>
          <button className="text-button" type="button" disabled={!edits || !!busy} onClick={() => { setFields(limitFields(view)); setSaved(false); setProblem(null); }}>Discard changes</button>
          <span role="status">{status}</span>
        </div>
      </form>
      <h3>Change history</h3>
      <p>Changes saved from this console, newest first; the latest 50 are kept. “not set” means the environment or built-in value applied.</p>
      <LimitsHistory view={view} />
    </>}
  </section>;
}
