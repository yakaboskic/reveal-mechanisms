/** Serialize cookie creation across tabs; localStorage is notification only, never a lock or credential. */
export class BrowserSessionError extends Error {}
export const sessionLock = "reveal-client-session";
export const sessionChangedKey = "reveal-client-session-changed";
export const sessionReloadKey = "reveal-client-session-reload";
export type SessionValue<P> = { principal: P | null };
export type SessionLocks = { request<T>(name: string, callback: () => Promise<T>): Promise<T> };
export function browserSessions<P extends { user_id: string }>(options: {
  read: () => Promise<SessionValue<P>>;
  create: () => Promise<{ principal: P }>;
  remove: () => Promise<{ principal: null }>;
  locks: () => SessionLocks | undefined;
  remember: (principal: P | null) => void;
  announce: (principal: P | null) => void;
}) {
  const remember = <T extends SessionValue<P>>(value: T): T => { options.remember(value.principal); return value; };
  return {
    session: async () => remember(await options.read()),
    connect: async (): Promise<{ principal: P }> => {
      const connect = async () => {
        const existing = await options.read();
        if (existing.principal) return remember({ principal: existing.principal });
        const value = await options.create();
        remember(value); options.announce(value.principal); return value;
      };
      const locks = options.locks();
      if (locks) return locks.request(sessionLock, connect);
      const existing = await options.read();
      if (existing.principal) return remember({ principal: existing.principal });
      throw new BrowserSessionError("Opening a new workspace requires a browser with Web Locks support. Please use an up-to-date browser.");
    },
    disconnect: async () => {
      const disconnect = async () => {
        const value = remember(await options.remove()); options.announce(null); return value;
      };
      const locks = options.locks();
      if (!locks) throw new BrowserSessionError("Changing workspaces requires a browser with Web Locks support. Please use an up-to-date browser.");
      return locks.request(sessionLock, disconnect);
    },
  };
}
export function announceSessionChange(principal: { user_id: string } | null): void {
  try { localStorage.setItem(sessionChangedKey, JSON.stringify({ principal: principal?.user_id || null, nonce: crypto.randomUUID() })); }
  catch { /* The request's expected-workspace fence remains effective when storage is unavailable. */ }
}
export function changedWorkspace(event: Pick<StorageEvent, "key" | "newValue">, expected: string | null): boolean {
  if (event.key !== sessionChangedKey || !event.newValue || !expected) return false;
  try {
    const value = JSON.parse(event.newValue);
    return (value.principal === null || typeof value.principal === "string") && value.principal !== expected;
  } catch { return false; }
}
