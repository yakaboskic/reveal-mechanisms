"use client";
import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { RevalidationCache } from "@/lib/revalidation-cache";
import { loadWorkspaceData, workspaceTabs, workspaceKey, workspaceKeyParts, workspaceSize, type WorkspaceData, type WorkspaceTab, type WorkspaceKey } from "@/lib/workspace-data";
import type { ReferenceState } from "@/lib/reference";
import { onWorkspaceChange, invalidateWorkspace, resetWorkspaceCache, affectedWorkspaceTabs, shareWorkspaceEvents, type WorkspaceConnection } from "@/lib/workspace-events";
import { terminal, type Schema } from "@/lib/client";
import { onPageReturn } from "@/lib/page-return";
import type { RefreshOptions } from "@/lib/session-identity";

type Store = RevalidationCache<WorkspaceKey, WorkspaceData>;
type IdentityCheck = (options?: RefreshOptions) => Promise<Schema<"Me"> | null>;
const Context = createContext<{ cache: Store; scope: string | null; checkIdentity: IdentityCheck; connection: WorkspaceConnection } | null>(null);
const serverVersion = () => 0;

/** `owner`: the scope's user; every tab of that user in this browser shares one workspace event stream. */
export function WorkspaceCacheProvider({ scope, owner, live = true, children, checkIdentity }: { scope: string | null; owner?: string | null; live?: boolean; children: ReactNode; checkIdentity: IdentityCheck }) {
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
    if (!scope || !live) return;
    const controller = new AbortController();
    void shareWorkspaceEvents(controller.signal, owner || scope, {
      status: setConnection,
      change: event => {
        invalidateWorkspace(event);
        // Replay re-sends the principal's creation on every connect; only a resync forces a fresh /v1/me.
        if (event?.collections.includes("identity")) void identityCheck.current({ force: event.operation === "resync" });
      },
      revoked: () => { resetWorkspaceCache(); void identityCheck.current({ force: true }); },
    });
    return () => controller.abort();
  }, [cache, scope, owner, live]);
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
  useEffect(() => onPageReturn(() => {
    void checkIdentity().then(identity => { if (identity && scope?.startsWith(identity.user_id + ":")) void cache.revalidate(scope, key); });
  }), [cache, scope, key, checkIdentity]);
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
    if (data) counts[value] = `${workspaceSize(value, data)}${data.cursor ? "+" : ""}`;
  }
  return { ...snapshot, counts, connection, refresh: () => cache.revalidate(scope, key, true), loadMore: () => cache.revalidate(scope, key, true, "append") };
}
