/** Browser-session notifications; no private workspace data is stored here. */
const listeners = new Set<(reset: boolean) => void>();
export function onWorkspaceChange(listener: (reset: boolean) => void) {
  listeners.add(listener); return () => { listeners.delete(listener); };
}
export function invalidateWorkspace() { for (const listener of listeners) listener(false); }
export function resetWorkspaceCache() { for (const listener of listeners) listener(true); }

export function changesWorkspace(method: string, path: string) {
  return ["POST", "PATCH", "DELETE"].includes(method) && /\/v1\/(?:drafts(?:\/[^/]+)?|jobs(?:\/[^/]+\/(?:cancel|retry-review))?|me\/explorations|accounts\/[^/]+\/publication|analysis-outcomes\/[^/]+\/publication)$/.test(path);
}
