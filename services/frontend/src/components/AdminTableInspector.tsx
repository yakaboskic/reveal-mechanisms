"use client";
import { useEffect, useRef, useState } from "react";
import { adminDate, formatAdminDate, formatAdminJson, localizeAdminValue } from "@/lib/admin-time";

type Column = { name: string; type: string; nullable: boolean };
type Cell = { text: string | null; length: number | null; loaded: number; truncated: boolean; binary: boolean; json: boolean; digest?: string | null };
type Key = Record<string, string>;
type Row = { key: Key; cells: Record<string, Cell>; timestamps: Record<string, string> };
type Page = { table: string; model: string; columns: Column[]; primary_key: string[]; rows: Row[]; next_cursor: string | null; limit: number };
type Detail = Row & { columns: Column[] };
type Filter = { column: string; operator: string; q: string };

async function read<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { cache: "no-store", signal });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Unable to read this table.");
  return value as T;
}
function formatted(value: Cell, field: string, pretty = true) {
  if (value.text === null) return "NULL";
  if (value.binary) return value.text.match(/.{1,64}/g)?.join("\n") || "(empty bytes)";
  if (value.json) return formatAdminJson(value.text, pretty);
  return localizeAdminValue(value.text, field) || '""';
}
function Timestamps({ values }: { values: Record<string, string> }) {
  const entries = Object.entries(values || {}).filter(([field, value]) => adminDate(value, field));
  return entries.length ? <dl className="admin-object-times">{entries.map(([field, value]) => <div key={field}><dt>{field}</dt><dd>{formatAdminDate(value, undefined, field)}</dd></div>)}</dl> : <span className="admin-no-times">Not recorded</span>;
}
function ValueInspector({ table, rowKey, column, initial }: { table: string; rowKey: Key; column: Column; initial: Cell }) {
  const [value, setValue] = useState(initial);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  async function more() {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setLoading(true); setError("");
    try {
      const params = new URLSearchParams({ key: JSON.stringify(rowKey), column: column.name });
      const endpoint = `/api/admin/tables/${table}/cell?`;
      // Start with a hashed chunk so an updated value cannot get spliced into
      // the preview captured by the earlier row request.
      let current = value;
      if (!current.digest) current = await read<Cell>(endpoint + params, controller.signal);
      if (current.truncated) {
        params.set("offset", String(current.loaded)); params.set("digest", current.digest || "");
        const next = await read<Cell>(endpoint + params, controller.signal);
        current = { ...next, text: (current.text || "") + (next.text || "") };
      }
      if (!controller.signal.aborted) { setValue(current); setCopied(false); }
    } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Unable to load the value."); }
    finally { pending.current = null; if (!controller.signal.aborted) setLoading(false); }
  }
  return <details className="admin-field-detail" open={!value.json && !value.binary && !value.truncated && (value.text?.length || 0) < 300}>
    <summary><strong>{column.name}</strong><span>{column.type}{column.nullable ? " · nullable" : ""}{value.length !== null ? ` · ${value.length.toLocaleString()} ${value.binary ? "bytes, hex" : "characters"}` : " · NULL"}</span></summary>
    <pre>{formatted(value, column.name)}</pre>
    {value.truncated && <p>Showing {value.loaded.toLocaleString()} of {value.length?.toLocaleString()} {value.binary ? "bytes" : "characters"}.<button className="text-button" disabled={loading} onClick={() => void more()}>{loading ? "Loading…" : "Load more"}</button></p>}
    {!value.truncated && value.text !== null && <button className="text-button" onClick={async () => { try { await navigator.clipboard.writeText(value.text!); setCopied(true); } catch { setError("Copy is unavailable. Select the displayed value above to copy it."); } }}>{copied ? "Copied" : value.binary ? "Copy hex" : "Copy stored value"}</button>}
    {error && <p className="error" role="alert">{error}</p>}
  </details>;
}

function RowInspector({ table, rowKey, onClose }: { table: string; rowKey: Key; onClose: () => void }) {
  const [row, setRow] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const element = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const controller = new AbortController(); setRow(null); setError("");
    const params = new URLSearchParams({ key: JSON.stringify(rowKey) });
    void read<Detail>(`/api/admin/tables/${table}/row?${params}`, controller.signal)
      .then(value => { if (!controller.signal.aborted) setRow(value); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); });
    element.current?.scrollIntoView({ block: "start" });
    return () => controller.abort();
  }, [table, rowKey, revision]);
  return <section ref={element} className="admin-row-detail" aria-label="Row contents">
    <div className="admin-section-heading"><h3>Row contents</h3><div><button className="text-button" onClick={() => setRevision(v => v + 1)}>Reload row</button><button className="text-button" onClick={onClose}>Close row</button></div></div>
    <dl className="admin-row-key">{Object.entries(rowKey).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
    {!row && !error && <p role="status">Loading row contents…</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {row && <div className="admin-row-timestamps"><h4>Timestamps</h4><Timestamps values={row.timestamps} /></div>}
    {row && row.columns.map(column => <ValueInspector key={`${column.name}-${revision}`} table={table} rowKey={rowKey} column={column} initial={row.cells[column.name]} />)}
  </section>;
}

export function AdminTableInspector({ table, onClose }: { table: string; onClose: () => void }) {
  const [data, setData] = useState<Page | null>(null);
  const [columns, setColumns] = useState<Column[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [limit, setLimit] = useState(25);
  const [history, setHistory] = useState<(string | null)[]>([null]);
  const [filter, setFilter] = useState<Filter>({ column: "", operator: "contains", q: "" });
  const [form, setForm] = useState<Filter>(filter);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<Key | null>(null);
  const cursor = history[history.length - 1];
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(""); setSelected(null);
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    if (filter.q) { params.set("column", filter.column); params.set("operator", filter.operator); params.set("q", filter.q); }
    void read<Page>(`/api/admin/tables/${table}?${params}`, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setData(value); setColumns(value.columns); } })
      .catch(e => { if (!controller.signal.aborted) { setData(null); setError(e.message); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [table, cursor, limit, filter, revision]);
  const searchable = columns.filter(c => !["Json", "Bytes"].includes(c.type));
  return <section className="admin-inspector" aria-label="Table contents">
    <button className="text-button" onClick={onClose}>Back to database tables</button>
    <div className="admin-section-heading"><div><h2>{table}</h2><p>{data?.model || "Database table"} · Read only</p></div><button className="submit" disabled={loading} onClick={() => setRevision(v => v + 1)}>Refresh rows</button></div>
    <p>Inspect a row to expand JSON and long values. Times, including those in JSON, use your browser’s local timezone. Copy stored value preserves the original data. Rows are ordered by primary key and update when you refresh.</p>
    {table === "reveal_records" && <p className="admin-inspector-note">Application payloads are visible here. Worker lease tokens are omitted.</p>}
    <form className="admin-filter" onSubmit={e => { e.preventDefault(); setHistory([null]); setFilter({ ...form, column: form.column || searchable[0]?.name || "" }); }}>
      <label>Column<select aria-label="Column" value={form.column || searchable[0]?.name || ""} onChange={e => setForm({ ...form, column: e.target.value })} disabled={loading || !searchable.length}>{searchable.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}</select></label>
      <label>Match<select aria-label="Match" value={form.operator} onChange={e => setForm({ ...form, operator: e.target.value })}><option value="contains">Contains</option><option value="equals">Equals</option></select></label>
      <label className="admin-filter-value">Value<input className="field" maxLength={256} placeholder="Filter stored rows…" value={form.q} onChange={e => setForm({ ...form, q: e.target.value })} /></label>
      <button className="submit" type="submit" disabled={loading || !searchable.length}>Apply filter</button>
      <button className="text-button" type="button" disabled={loading || !filter.q} onClick={() => { const empty = { column: "", operator: "contains", q: "" }; setForm(empty); setFilter(empty); setHistory([null]); }}>Clear</button>
    </form>
    <div className="admin-row-pagination"><span role="status">{loading ? "Loading rows…" : data ? `${data.rows.length} rows · page ${history.length}${filter.q ? " · filtered" : ""}` : "Rows unavailable"}</span><label>Rows per page<select aria-label="Rows per page" value={limit} disabled={loading} onChange={e => { setLimit(Number(e.target.value)); setHistory([null]); }}>{[10,25,50].map(n => <option key={n}>{n}</option>)}</select></label><button className="submit" disabled={loading || history.length === 1} onClick={() => setHistory(h => h.slice(0, -1))}>Previous</button><button className="submit" disabled={loading || !data?.next_cursor} onClick={() => setHistory(h => [...h, data!.next_cursor])}>Next</button></div>
    {error && <p role="alert" className="error">{error}<button onClick={() => { setHistory([null]); setRevision(v => v + 1); }}>Reload first page</button></p>}
    {data && !loading && <div className="admin-table-scroll admin-content-table" tabIndex={0}><table><thead><tr><th>Row</th><th>Timestamps<small>Where recorded</small></th>{data.columns.map(c => <th key={c.name}>{c.name}<small>{c.type}{data.primary_key.includes(c.name) ? " · primary key" : ""}</small></th>)}</tr></thead><tbody>{data.rows.map((row, index) => <tr key={JSON.stringify(row.key)}><td><button className="admin-job-link" onClick={() => setSelected(row.key)} aria-label={`Inspect row ${index + 1}`}>Inspect</button></td><td className="admin-timestamp-cell"><Timestamps values={row.timestamps} /></td>{data.columns.map(c => { const value = row.cells[c.name]; return <td key={c.name}><span className="admin-cell-preview">{value.text === null ? <em>NULL</em> : value.binary ? `${value.length?.toLocaleString()} bytes · ${value.text.slice(0, 32)}…` : (value.text.length ? formatted(value, c.name, false).slice(0, 180) : '""')}{!value.binary && (value.truncated || (value.text?.length || 0) > 150) ? "…" : ""}</span></td>; })}</tr>)}</tbody></table></div>}
    {data && !loading && !data.rows.length && <p className="admin-empty">{filter.q ? "No rows match this filter. Clear it or choose another column." : "This table has no rows on this page."}</p>}
    {selected && <RowInspector key={JSON.stringify(selected)} table={table} rowKey={selected} onClose={() => setSelected(null)} />}
  </section>;
}
