"use client";
import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { RevalidationCache } from "@/lib/revalidation-cache";
import { loadWorkspaceData, workspaceTabs, workspaceKey, workspaceKeyParts, type WorkspaceData, type WorkspaceTab, type WorkspaceKey } from "@/lib/workspace-data";
import type { ReferenceState } from "@/lib/reference";
import { onWorkspaceChange, invalidateWorkspace, resetWorkspaceCache, affectedWorkspaceTabs, connectWorkspaceEvents, type WorkspaceConnection } from "@/lib/workspace-events";
import { terminal, type Schema } from "@/lib/client";

type Store = RevalidationCache<WorkspaceKey, WorkspaceData>;
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
      // Every reference filter and search of an affected tab is invalidated with it.
      queueMicrotask(() => { queued = false; if (active && keys.size) cache.invalidateWhere(key => keys.has(workspaceKeyParts(key).tab)); keys.clear(); });
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
      revoked: () => { resetWorkspaceCache(); void identityCheck.current(); },
    });
    return () => controller.abort();
  }, [cache, scope]);
  return <Context.Provider value={{ cache, scope, checkIdentity, connection }}>{children}</Context.Provider>;
}

export function useWorkspaceData(tab: WorkspaceTab, query = "", reference: ReferenceState = "all") {
  const store = useContext(Context);
  if (!store) throw new Error("Workspace cache requires the session provider.");
  const { cache, scope, checkIdentity, connection } = store;
  useSyncExternalStore(cache.subscribe, cache.getVersion, serverVersion);
  const key = workspaceKey(tab, query, reference);
  const snapshot = cache.read(scope, key);
  useEffect(() => {
    if (!snapshot.error) void cache.revalidate(scope, key);
  }, [cache, scope, key, snapshot.stale, snapshot.loading, snapshot.error]);
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "hidden") return;
      void checkIdentity().then(identity => { if (identity && scope?.startsWith(identity.user_id + ":")) void cache.revalidate(scope, key); });
    };
    window.addEventListener("focus", refresh); window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => { window.removeEventListener("focus", refresh); window.removeEventListener("online", refresh); document.removeEventListener("visibilitychange", refresh); };
  }, [cache, scope, key, checkIdentity]);
  const observed = useRef(new Map<string, string>());
  useEffect(() => { observed.current.clear(); }, [scope]);
  useEffect(() => {
    const jobs = snapshot.data?.jobs || [];
    const completed = jobs.some(job => observed.current.has(job.id) && observed.current.get(job.id) !== job.status && terminal(job.status));
    for (const job of jobs) observed.current.set(job.id, job.status);
    if (completed) cache.invalidate();
  }, [cache, snapshot.data?.jobs]);
  const counts: Partial<Record<WorkspaceTab, string>> = {};
  // The selected tab counts its reference-filtered listing (a search reports its own matches); the others count everything loaded.
  const listed = workspaceKey(tab, "", reference);
  for (const value of workspaceTabs) {
    const data = cache.read(scope, value === tab ? listed : value).data;
    if (data) counts[value] = `${(value === "gaps" ? data.gaps : value === "drafts" ? data.drafts : value === "runs" ? data.jobs.filter(job => job.kind === "analysis") : value === "accounts" ? data.accounts : data.outcomes).length}${data.cursor ? "+" : ""}`;
  }
  return { ...snapshot, counts, connection, refresh: () => cache.revalidate(scope, key, true), loadMore: () => cache.revalidate(scope, key, true, "append") };
}
