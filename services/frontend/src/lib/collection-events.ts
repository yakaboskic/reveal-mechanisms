import { onWorkspaceChange, type WorkspaceEvent } from "./workspace-events";

/** Coalesce one notification delivery batch; idle views issue no requests. */
export function onCollectionInvalidation(collections: readonly WorkspaceEvent["collections"][number][], refresh: (reset: boolean) => void) {
  let active = true, queued = false, resetPending = false;
  const remove = onWorkspaceChange((reset, event) => {
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
