"use client";
import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { formatAdminDate as date } from "../lib/admin-time";
import { Callout, Empty, PageHeading, Panel, Status } from "./admin/ui";
import {
  budgetFields, isKeysView, isMinted, mintForm, usageText,
  type ApiKeySummary, type ApiKeysView, type BudgetFields, type MintFields, type Minted,
} from "../lib/admin-api-keys";

const ENDPOINT = "/api/admin/api-keys";
const EMPTY_FORM: MintFields = { label: "", workspace: "new", userId: "", agentJobs: false, period: "none", limit: "" };

function problemText(value: unknown, fallback: string) {
  const body = value as { error?: unknown; detail?: unknown } | null;
  return typeof body?.error === "string" ? body.error : typeof body?.detail === "string" ? body.detail : fallback;
}

/** The raw key, shown once. Copying never puts the key in an accessible name or a log. */
export function MintedKey({ minted, onDone }: { minted: Minted; onDone: () => void }) {
  const [copied, setCopied] = useState("");
  return <Callout tone="good" role="status">
    <strong>Key minted for “{minted.key.label}”.</strong> Copy it now: it is shown only this once, and REVEAL keeps only its hash.
    <div className="admin-key-secret">
      <code className="admin-mono" data-testid="minted-key">{minted.api_key}</code>
      <button type="button" className="submit" onClick={async () => {
        try { await navigator.clipboard.writeText(minted.api_key); setCopied("Copied"); } catch { setCopied("Copy failed; select the key and copy it"); }
      }}>Copy key</button>
      <span role="status">{copied}</span>
    </div>
    <small>Workspace <span className="admin-mono">{minted.key.user_id}</span>. Use it as <span className="admin-mono">Authorization: Bearer &lt;key&gt;</span> against this environment&apos;s API.</small>
    <button type="button" className="text-button" onClick={onDone}>I have stored the key; hide it</button>
  </Callout>;
}

/** Period and amount. "none" leaves the key unlimited; the amount is in US dollars. */
export function BudgetInputs({ fields, error, disabled, prefix, onChange }: {
  fields: BudgetFields; error?: string; disabled: boolean; prefix: string; onChange: (fields: BudgetFields) => void;
}) {
  return <div className="admin-key-budget">
    <select className="field" aria-label="Budget period" value={fields.period} disabled={disabled}
      onChange={e => onChange({ ...fields, period: e.target.value as BudgetFields["period"] })}>
      <option value="none">No budget</option>
      <option value="month">Per calendar month (UTC)</option>
      <option value="total">In total</option>
    </select>
    {fields.period !== "none" && <input className="field" type="text" inputMode="decimal" autoComplete="off" spellCheck={false}
      aria-label="Budget in US dollars" placeholder="100.00" value={fields.limit} disabled={disabled}
      aria-invalid={error ? true : undefined} aria-describedby={error ? `${prefix}-budget-error` : undefined}
      onChange={e => onChange({ ...fields, limit: e.target.value })} />}
    {error && <small id={`${prefix}-budget-error`} className="admin-limit-error">{error}</small>}
  </div>;
}

function budgetOf(key: ApiKeySummary): BudgetFields {
  return key.budget ? { period: key.budget.period, limit: String(key.budget.limit_usd) } : { period: "none", limit: "" };
}

/** A key's spend and budget, and the editor that changes the budget. */
function BudgetCell({ apiKey, busy, onSave }: { apiKey: ApiKeySummary; busy: boolean; onSave: (key: ApiKeySummary, fields: BudgetFields) => Promise<boolean> }) {
  const [editing, setEditing] = useState<BudgetFields | null>(null);
  const [error, setError] = useState("");
  const prefix = useId();
  const usage = apiKey.usage;
  const over = !!apiKey.budget && usage?.remaining_usd != null && usage.remaining_usd <= 0;
  if (!editing) return <td>
    <span className={over ? "admin-warning" : undefined}>{usageText(apiKey)}</span>
    {usage && usage.in_progress_usd > 0 && <small>Includes up to ${usage.in_progress_usd.toFixed(2)} held for jobs in progress.</small>}
    {apiKey.budget_changed_at && <small>Budget changed {date(apiKey.budget_changed_at)} by {apiKey.budget_changed_by}</small>}
    {!apiKey.revoked_at && <button type="button" className="text-button" disabled={busy} onClick={() => { setEditing(budgetOf(apiKey)); setError(""); }}>
      {apiKey.budget ? "Change budget" : "Set a budget"}</button>}
  </td>;
  return <td>
    <form noValidate onSubmit={async event => {
      event.preventDefault();
      const checked = budgetFields(editing);
      if ("error" in checked) { setError(checked.error); return; }
      if (await onSave(apiKey, editing)) setEditing(null);
    }}>
      <BudgetInputs fields={editing} error={error} disabled={busy} prefix={prefix} onChange={next => { setEditing(next); setError(""); }} />
      <span className="admin-key-confirm">
        <button type="submit" className="submit" disabled={busy}>Save budget</button>
        <button type="button" className="text-button" disabled={busy} onClick={() => setEditing(null)}>Cancel</button>
      </span>
    </form>
  </td>;
}

function workspaceText(key: ApiKeySummary) {
  const w = key.workspace;
  if (!w) return "Workspace not found";
  if (w.retired) return "Retired workspace";
  const name = w.display_name ? `${w.display_name} · ` : "";
  return `${name}${w.principal_kind || "unknown"}${w.workspace_expires_at ? `, expires ${date(w.workspace_expires_at)}` : ""}`;
}

export function KeysTable({ keys, busy, onRevoke, onBudget }: {
  keys: ApiKeySummary[]; busy: boolean; onRevoke: (key: ApiKeySummary) => void; onBudget: (key: ApiKeySummary, fields: BudgetFields) => Promise<boolean>;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);
  if (!keys.length) return <Empty>No key has been minted in this console yet.</Empty>;
  return <div className="admin-table-scroll" tabIndex={0}><table><thead><tr>
    <th>Label</th><th>Key</th><th>Workspace</th><th>Agent jobs</th><th>Spend and budget</th><th>Created</th><th>Status</th><th><span className="sr-only">Actions</span></th>
  </tr></thead><tbody>
    {keys.map(key => <tr key={key.key_id}>
      <td><strong>{key.label}</strong></td>
      <td className="admin-mono">{key.prefix}…</td>
      <td><span className="admin-mono">{key.user_id}</span><small>{workspaceText(key)}</small></td>
      <td>{key.agent_jobs ? "Allowed" : "No"}</td>
      <BudgetCell apiKey={key} busy={busy} onSave={onBudget} />
      <td className="admin-date">{date(key.created_at)}<small>{key.created_by}</small></td>
      <td>{key.revoked_at ? <><Status tone="bad">revoked</Status><small>{date(key.revoked_at)} by {key.revoked_by}</small></> : <Status tone="good">active</Status>}</td>
      <td>{!key.revoked_at && (confirming === key.key_id
        ? <span className="admin-key-confirm">
            <button type="button" className="submit danger" disabled={busy} onClick={() => { setConfirming(null); onRevoke(key); }}>Revoke “{key.label}”</button>
            <button type="button" className="text-button" disabled={busy} onClick={() => setConfirming(null)}>Keep</button>
          </span>
        : <button type="button" className="text-button" disabled={busy} onClick={() => setConfirming(key.key_id)}>Revoke…</button>)}</td>
    </tr>)}
  </tbody></table></div>;
}

export function ConfiguredKeys({ view }: { view: ApiKeysView }) {
  const { keys, problem } = view.environment;
  if (problem) return <Callout tone="bad">{problem}</Callout>;
  if (!keys.length) return <Empty>No key is configured in this deployment&apos;s environment.</Empty>;
  return <div className="admin-table-scroll" tabIndex={0}><table><thead><tr><th>Hash prefix</th><th>Workspace</th><th>Agent jobs</th></tr></thead><tbody>
    {keys.map(key => <tr key={key.fingerprint}><td className="admin-mono">{key.fingerprint}…</td><td className="admin-mono">{key.user_id}</td>
      <td>{key.agent_jobs ? "Allowed (REVEAL_AGENT_JOB_USERS)" : "No"}</td></tr>)}
  </tbody></table></div>;
}

export function AdminApiKeys() {
  const [view, setView] = useState<ApiKeysView | null>(null);
  const [fields, setFields] = useState<MintFields>(EMPTY_FORM);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [minted, setMinted] = useState<Minted | null>(null);
  const [busy, setBusy] = useState<"" | "loading" | "minting" | "revoking" | "saving">("");
  const [problem, setProblem] = useState<string | null>(null);
  const pending = useRef(false);
  const id = useId();

  const load = useCallback(async () => {
    if (pending.current) return;
    pending.current = true; setBusy("loading");
    try {
      const response = await fetch(ENDPOINT, { cache: "no-store" });
      const value = await response.json().catch(() => null);
      if (response.ok && isKeysView(value)) { setView(value); setProblem(null); }
      else setProblem(response.status === 404 ? "This backend does not offer API keys from the console yet." : problemText(value, "The keys are unavailable."));
    } catch { setProblem("The keys are unavailable. Check the connection, then reload."); }
    finally { pending.current = false; setBusy(""); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function mint(event: React.FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    const checked = mintForm(fields);
    if ("errors" in checked) { setErrors(checked.errors); return; }
    setErrors({}); pending.current = true; setBusy("minting"); setProblem(null); setMinted(null);
    try {
      const response = await fetch(ENDPOINT, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(checked.request), cache: "no-store" });
      const value = await response.json().catch(() => null);
      if (response.ok && isMinted(value)) {
        setMinted(value); setFields(EMPTY_FORM);
        setView(current => current ? { ...current, keys: [value.key, ...current.keys] } : current);
      } else setProblem(problemText(value, "The key could not be minted."));
    } catch { setProblem("The key could not be minted. Check the connection; reload to see whether it was, then try again."); }
    finally { pending.current = false; setBusy(""); }
  }

  async function revoke(key: ApiKeySummary) {
    if (pending.current) return;
    pending.current = true; setBusy("revoking"); setProblem(null);
    try {
      const response = await fetch(`${ENDPOINT}/${encodeURIComponent(key.key_id)}/revoke`, { method: "POST", cache: "no-store" });
      const value = await response.json().catch(() => null) as { key?: ApiKeySummary } | null;
      if (response.ok && value?.key) setView(current => current ? { ...current, keys: current.keys.map(k => k.key_id === key.key_id ? { ...k, ...value.key! } : k) } : current);
      else setProblem(problemText(value, "The key could not be revoked."));
    } catch { setProblem("The key could not be revoked. Check the connection, then reload."); }
    finally { pending.current = false; setBusy(""); }
  }

  async function changeBudget(key: ApiKeySummary, fields: BudgetFields): Promise<boolean> {
    const checked = budgetFields(fields);
    if ("error" in checked || pending.current) return false;
    pending.current = true; setBusy("saving"); setProblem(null);
    try {
      const response = await fetch(`${ENDPOINT}/${encodeURIComponent(key.key_id)}/budget`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ budget: checked.budget }), cache: "no-store" });
      const value = await response.json().catch(() => null) as { key?: ApiKeySummary } | null;
      if (response.ok && value?.key) {
        setView(current => current ? { ...current, keys: current.keys.map(k => k.key_id === key.key_id ? value.key! : k) } : current);
        return true;
      }
      setProblem(problemText(value, "The budget could not be saved."));
    } catch { setProblem("The budget could not be saved. Check the connection, then try again."); }
    finally { pending.current = false; setBusy(""); }
    return false;
  }

  const set = <K extends keyof MintFields>(name: K, value: MintFields[K]) => { setFields(current => ({ ...current, [name]: value })); setErrors(current => ({ ...current, [name]: "" })); };
  return <section className="admin-keys" aria-label="API keys">
    <PageHeading title="API keys" subtitle="Mint and revoke rvl_ keys for scripts and partner apps. A key works as soon as it is minted, with no deploy."
      actions={<button className="submit" type="button" disabled={!!busy} onClick={() => void load()}>{busy === "loading" ? "Loading…" : "Reload keys"}</button>} />
    {problem && <Callout tone="bad" role="alert">{problem}</Callout>}
    {minted && <MintedKey minted={minted} onDone={() => setMinted(null)} />}
    <Panel title="Mint a key" description="A key acts as one workspace: it can save drafts, submit jobs and read their results, and nothing an admin can. Only its hash is stored, so a lost key cannot be shown again; revoke it and mint another.">
      <form className="admin-key-form" noValidate onSubmit={mint}>
        <label htmlFor={`${id}-label`}>Label</label>
        <div>
          <input id={`${id}-label`} className="field" type="text" autoComplete="off" spellCheck={false} placeholder="reveal-claims demo"
            value={fields.label} aria-invalid={errors.label ? true : undefined} aria-describedby={errors.label ? `${id}-label-error` : undefined}
            disabled={!!busy} onChange={e => set("label", e.target.value)} />
          {errors.label && <small id={`${id}-label-error`} className="admin-limit-error">{errors.label}</small>}
        </div>
        <span>Workspace</span>
        <fieldset disabled={!!busy}>
          <legend className="sr-only">Workspace</legend>
          <label><input type="radio" name={`${id}-workspace`} checked={fields.workspace === "new"} onChange={() => set("workspace", "new")} /> A new service workspace, named by the label (registered, no expiry)</label>
          <label><input type="radio" name={`${id}-workspace`} checked={fields.workspace === "existing"} onChange={() => set("workspace", "existing")} /> An existing workspace</label>
          {fields.workspace === "existing" && <div>
            <input className="field admin-mono" type="text" autoComplete="off" spellCheck={false} aria-label="Existing workspace id" placeholder="Workspace id (UUID)"
              value={fields.userId} aria-invalid={errors.userId ? true : undefined} aria-describedby={errors.userId ? `${id}-user-error` : undefined}
              onChange={e => set("userId", e.target.value)} />
            {errors.userId && <small id={`${id}-user-error`} className="admin-limit-error">{errors.userId}</small>}
          </div>}
        </fieldset>
        <span>Budget</span>
        <div>
          <BudgetInputs fields={fields} error={errors.limit} disabled={!!busy} prefix={`${id}-mint`}
            onChange={next => { setFields(current => ({ ...current, ...next })); setErrors(current => ({ ...current, limit: "" })); }} />
          <small>Spend is what the jobs this key starts cost, retries included. A job is refused when it could take the key past its budget.</small>
        </div>
        <span>Agent jobs</span>
        <label><input type="checkbox" checked={fields.agentJobs} disabled={!!busy} onChange={e => set("agentJobs", e.target.checked)} /> Allow this workspace to start general agent jobs while the key is active</label>
        <div className="admin-limits-actions">
          <button className="submit" type="submit" disabled={!!busy}>{busy === "minting" ? "Minting…" : "Mint key"}</button>
        </div>
      </form>
    </Panel>
    <Panel title="Keys minted here" description="Newest first. Spend counts each job's recorded cost and, while a job runs, the most its next run may cost. A budget change applies to the next job. Revoking takes effect on the next request; an open event stream ends within four minutes.">
      {view ? <KeysTable keys={view.keys} busy={!!busy} onRevoke={key => void revoke(key)} onBudget={changeBudget} /> : problem ? <Empty>The keys could not be read.</Empty> : <p role="status">Loading keys…</p>}
    </Panel>
    {view && <Panel title="Keys configured in the environment" description="Set through REVEAL_API_KEY_SHA256 and REVEAL_API_KEYS. These change only with a deploy and cannot be revoked here.">
      <ConfiguredKeys view={view} />
    </Panel>}
  </section>;
}
