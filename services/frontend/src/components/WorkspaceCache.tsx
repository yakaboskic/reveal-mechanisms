"use client";
import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { RevalidationCache } from "@/lib/revalidation-cache";
import { loadWorkspaceData, workspaceTabs, type WorkspaceData, type WorkspaceTab } from "@/lib/workspace-data";
import { onWorkspaceChange, invalidateWorkspace, affectedWorkspaceTabs, connectWorkspaceEvents, type WorkspaceConnection } from "@/lib/workspace-events";
import { terminal, type Schema } from "@/lib/client";

type Store = RevalidationCache<WorkspaceTab, WorkspaceData>;
type IdentityCheck = () => Promise<Schema<"Me"> | null>;
const Context = createContext<{ cache: Store; scope: string | null; checkIdentity: IdentityCheck; connection: WorkspaceConnection } | null>(null);
const serverVersion = () => 0;

export function WorkspaceCacheProvider({ scope, children, checkIdentity }: { scope: string | null; children: ReactNode; checkIdentity: IdentityCheck }) {
  const [cache] = useState(() => new RevalidationCache(loadWorkspaceData));
  const [connection, setConnection] = useState<WorkspaceConnection>("connecting");
  const identityCheck = useRef(checkIdentity); identityCheck.current = checkIdentity;
  useLayoutEffect(() => { cache.bind(scope); }, [cache, scope]);
  useEffect(() => {
    const keys = new Set<WorkspaceTab>(); let queued = false, active = true;
    const remove = onWorkspaceChange((reset, event) => {
      if (reset) { cache.bind(null); keys.clear(); return; }
      for (const key of affectedWorkspaceTabs(event)) keys.add(key);
      if (queued) return;
      queued = true;
      queueMicrotask(() => { queued = false; if (active && keys.size) cache.invalidate([...keys]); keys.clear(); });
    });
    return () => { active = false; remove(); };
  }, [cache]);
  useEffect(() => {
    if (!scope) return;
    const controller = new AbortController();
    void connectWorkspaceEvents(controller.signal, {
      status: setConnection,
      change: event => {
        invalidateWorkspace(event);
        if (event?.collections.includes("identity")) void identityCheck.current();
      },
      revoked: () => { cache.bind(null); void identityCheck.current(); },
    });
    return () => controller.abort();
  }, [cache, scope]);
  return <Context.Provider value={{ cache, scope, checkIdentity, connection }}>{children}</Context.Provider>;
}

export function useWorkspaceData(tab: WorkspaceTab) {
  const store = useContext(Context);
  if (!store) throw new Error("Workspace cache requires the session provider.");
  const { cache, scope, checkIdentity, connection } = store;
  useSyncExternalStore(cache.subscribe, cache.getVersion, serverVersion);
  const snapshot = cache.read(scope, tab);
  useEffect(() => {
    if (!snapshot.error) void cache.revalidate(scope, tab);
  }, [cache, scope, tab, snapshot.stale, snapshot.loading, snapshot.error]);
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "hidden") return;
      void checkIdentity().then(identity => { if (identity && scope?.startsWith(identity.user_id + ":")) void cache.revalidate(scope, tab); });
    };
    window.addEventListener("focus", refresh); window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => { window.removeEventListener("focus", refresh); window.removeEventListener("online", refresh); document.removeEventListener("visibilitychange", refresh); };
  }, [cache, scope, tab, checkIdentity]);
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
  return { ...snapshot, counts, connection, refresh: () => cache.revalidate(scope, tab, true), loadMore: () => cache.revalidate(scope, tab, true, "append") };
}
