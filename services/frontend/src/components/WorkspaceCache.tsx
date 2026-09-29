"use client";
import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { RevalidationCache, workspaceFreshMs } from "@/lib/revalidation-cache";
import { loadWorkspaceData, workspaceTabs, type WorkspaceData, type WorkspaceTab } from "@/lib/workspace-data";
import { onWorkspaceChange } from "@/lib/workspace-events";
import { terminal, type Schema } from "@/lib/client";

type Store = RevalidationCache<WorkspaceTab, WorkspaceData>;
type IdentityCheck = () => Promise<Schema<"Me"> | null>;
const Context = createContext<{ cache: Store; scope: string | null; checkIdentity: IdentityCheck } | null>(null);
const serverVersion = () => 0;

export function WorkspaceCacheProvider({ scope, children, checkIdentity }: { scope: string | null; children: ReactNode; checkIdentity: IdentityCheck }) {
  const [cache] = useState(() => new RevalidationCache(loadWorkspaceData));
  useLayoutEffect(() => { cache.bind(scope); }, [cache, scope]);
  useEffect(() => onWorkspaceChange(reset => { if (reset) cache.bind(null); else cache.invalidate(); }), [cache]);
  return <Context.Provider value={{ cache, scope, checkIdentity }}>{children}</Context.Provider>;
}

export function useWorkspaceData(tab: WorkspaceTab) {
  const store = useContext(Context);
  if (!store) throw new Error("Workspace cache requires the session provider.");
  const { cache, scope, checkIdentity } = store;
  useSyncExternalStore(cache.subscribe, cache.getVersion, serverVersion);
  const snapshot = cache.read(scope, tab);
  const active = tab === "gaps" ? snapshot.data?.jobs.some(job => !terminal(job.status))
    : tab === "accounts" && snapshot.data?.accounts.some(item => ["queued", "running", "cancel_requested"].includes(item.research_statement.status));
  useEffect(() => {
    if (!snapshot.error) void cache.revalidate(scope, tab);
  }, [cache, scope, tab, snapshot.stale, snapshot.loading, snapshot.error]);
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "hidden") return;
      void checkIdentity().then(identity => { if (identity && scope?.startsWith(identity.user_id + ":")) void cache.revalidate(scope, tab); });
    };
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void cache.revalidate(scope, tab, !!active, active && tab === "gaps" ? "activity" : "refresh");
    }, active ? 10_000 : workspaceFreshMs);
    window.addEventListener("focus", refresh); window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => { clearInterval(timer); window.removeEventListener("focus", refresh); window.removeEventListener("online", refresh); document.removeEventListener("visibilitychange", refresh); };
  }, [cache, scope, tab, active, checkIdentity]);
  const observed = useRef(new Map<string, string>());
  useEffect(() => { observed.current.clear(); }, [scope]);
  useEffect(() => {
    const jobs = snapshot.data?.jobs || [];
    const completed = jobs.some(job => observed.current.has(job.id) && observed.current.get(job.id) !== job.status && terminal(job.status));
    for (const job of jobs) observed.current.set(job.id, job.status);
    if (completed) cache.invalidate();
  }, [cache, snapshot.data?.jobs]);
  const counts: Partial<Record<WorkspaceTab, string>> = {};
  for (const key of workspaceTabs) {
    const data = cache.read(scope, key).data;
    if (data) counts[key] = `${(key === "gaps" ? data.gaps : key === "accounts" ? data.accounts : data.outcomes).length}${data.cursor ? "+" : ""}`;
  }
  return { ...snapshot, counts, refresh: () => cache.revalidate(scope, tab, true), loadMore: () => cache.revalidate(scope, tab, true, "append") };
}
