import { onWorkspaceChange, type WorkspaceEvent } from "./workspace-events";

/** Coalesce one notification delivery batch; idle views issue no requests. `local: false` ignores this tab's own
 * mutations, which carry no event, when their stream echo names the collections that matter; stream resyncs still refresh. */
export function onCollectionInvalidation(collections: readonly WorkspaceEvent["collections"][number][], refresh: (reset: boolean) => void, { local = true }: { local?: boolean } = {}) {
  let active = true, queued = false, resetPending = false;
  const remove = onWorkspaceChange((reset, event, own) => {
    if (!reset && own && !local) return;
    if (!reset && event && !event.collections.some(value => value === "identity" || collections.includes(value))) return;
    resetPending ||= reset;
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      if (!active) return;
      const reset = resetPending; resetPending = false;
      refresh(reset);
    });
  });
  return () => { active = false; remove(); };
}
