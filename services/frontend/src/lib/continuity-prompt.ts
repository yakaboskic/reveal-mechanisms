type PromptStorage = Pick<Storage, "getItem" | "setItem">;
const dismissalKey = (userId: string) => `reveal:continuity-dismissed:${userId}`;

/** UI preference only: dismissing never claims, deletes or changes workspace data. */
export function continuityPromptDismissed(userId: string, storage?: PromptStorage): boolean {
  if (!userId) return false;
  try { return (storage ?? window.sessionStorage).getItem(dismissalKey(userId)) === "1"; }
  catch { return false; }
}

export function dismissContinuityPrompt(userId: string, storage?: PromptStorage): void {
  if (!userId) return;
  try { (storage ?? window.sessionStorage).setItem(dismissalKey(userId), "1"); }
  catch { /* The mounted session still remembers the choice when storage is blocked. */ }
}
