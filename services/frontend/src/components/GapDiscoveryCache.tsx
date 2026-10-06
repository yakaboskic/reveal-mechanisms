"use client";
import { createContext, useContext, useEffect, useLayoutEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { GapDiscoveryCache, gapListKey, type GapScope, type GapSort } from "@/lib/gap-discovery";
import { onWorkspaceChange } from "@/lib/workspace-events";

const Context = createContext<{ cache: GapDiscoveryCache; viewer: string | null; sort: GapSort; setSort: (sort: GapSort) => void } | null>(null);
const serverVersion = () => 0;

export function GapDiscoveryProvider({ viewer, children }: { viewer: string | null; children: ReactNode }) {
  const [cache] = useState(() => new GapDiscoveryCache());
  const [sort, setSort] = useState<GapSort>("accounts");
  useLayoutEffect(() => { cache.bind(viewer); }, [cache, viewer]);
  useEffect(() => onWorkspaceChange((reset, event) => {
    if (reset) {
      cache.bind(null); cache.bind(viewer);
    } else if (!event || event.collections.some(value => ["catalog", "accounts", "gaps", "identity"].includes(value))) {
      // Show that updates are available without reordering a list being read.
      // This listener stays mounted when the user visits another page.
      cache.invalidate();
    }
  }), [cache, viewer]);
  return <Context.Provider value={{ cache, viewer, sort, setSort }}>{children}</Context.Provider>;
}

export function useGapDiscovery() {
  const context = useContext(Context);
  if (!context) throw new Error("Gap discovery requires the session provider.");
  return context;
}

export function useGapCollection(scope: GapScope, sort: GapSort) {
  const { cache, viewer } = useGapDiscovery();
  useSyncExternalStore(cache.subscribe, cache.getVersion, serverVersion);
  const key = gapListKey(scope, sort), snapshot = cache.read(viewer, key);
  useEffect(() => { void cache.ensure(viewer, key); }, [cache, viewer, key, snapshot.data, snapshot.loading, snapshot.error]);
  return { cache, viewer, key, snapshot };
}
