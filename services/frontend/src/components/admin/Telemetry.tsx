"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React, { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { Snapshot } from "@/lib/admin-telemetry";
import { formatAdminDate as date } from "@/lib/admin-time";
import { Callout } from "./ui";

type Telemetry = {
  data: Snapshot | null; error: string; loading: boolean; auto: boolean; timeZone: string;
  setAuto: (value: boolean) => void; refresh: () => Promise<void>;
};
const TelemetryContext = createContext<Telemetry>({ data: null, error: "", loading: false, auto: true, timeZone: "", setAuto() {}, async refresh() {} });
export const useTelemetry = () => useContext(TelemetryContext);

/** One telemetry snapshot shared by every admin page, refreshed every 15 seconds while the tab is visible. It sits in
 * the admin layout, so moving between pages keeps the snapshot instead of reloading it. */
export function TelemetryProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [timeZone, setTimeZone] = useState("");
  useEffect(() => setTimeZone(Intl.DateTimeFormat().resolvedOptions().timeZone), []);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const controller = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    if (controller.current) return;
    const request = new AbortController(); controller.current = request; setLoading(true);
    try {
      const response = await fetch("/api/admin/telemetry", { cache: "no-store", signal: request.signal });
      const value = await response.json();
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) setData(null);
        throw new Error(value.error || "Telemetry is unavailable.");
      }
      setData(value); setError("");
    } catch (e) { if (!request.signal.aborted) setError(e instanceof Error ? e.message : "Telemetry is unavailable."); }
    finally { if (controller.current === request) { controller.current = null; setLoading(false); } }
  }, []);
  useEffect(() => { void refresh(); return () => { controller.current?.abort(); controller.current = null; }; }, [refresh]);
  useEffect(() => { if (!auto) return; const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 15000); return () => clearInterval(timer); }, [auto, refresh]);
  return <TelemetryContext.Provider value={{ data, error, loading, auto, timeZone, setAuto, refresh }}>{children}</TelemetryContext.Provider>;
}

/** When the snapshot was read, the 15-second switch and a manual refresh: the top bar's right side. */
export function TelemetryControls({ telemetry }: { telemetry: Telemetry }) {
  const { data, loading, auto, setAuto, refresh } = telemetry;
  return <div className="admin-telemetry-controls">
    <span role="status">{data ? <>Updated {date(data.generated_at)} <span>· {data.query_ms} ms database read</span></> : "Connecting to telemetry…"}</span>
    <label><input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} /> Refresh every 15s</label>
    <button type="button" className="submit" disabled={loading} onClick={() => void refresh()}>{loading ? "Refreshing…" : "Refresh now"}</button>
  </div>;
}

/** Shown on every page: the development-bypass notice, the stale-data banner after a failed refresh, the time-zone note. */
export function ConsoleNotices({ bypass, telemetry }: { bypass: boolean; telemetry: Pick<Telemetry, "data" | "error" | "timeZone"> }) {
  const { data, error, timeZone } = telemetry;
  return <>
    {bypass && <Callout tone="warn">Development access: admin sign-in is bypassed on this server.</Callout>}
    {error && <Callout tone="bad" role="alert">{error}{data && " Showing the last successful snapshot; data is stale."}</Callout>}
    <p className="admin-timezone">Times shown in {timeZone || "your browser’s local timezone"}. Each timestamp includes its UTC offset; short ages show the full time on hover; “—” means not recorded.</p>
  </>;
}

/** Renders `children` with the snapshot once one has arrived; until then says it is connecting, or why it cannot. */
export function WithSnapshot({ children }: { children: (data: Snapshot) => ReactNode }) {
  const { data, error } = useTelemetry();
  if (data) return <>{children(data)}</>;
  return error ? null : <p className="admin-loading" role="status">Connecting to telemetry…</p>;
}
