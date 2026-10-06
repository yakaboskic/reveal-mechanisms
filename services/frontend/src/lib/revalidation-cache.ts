/** One session's memory cache. Refreshes retain data, and identity changes purge it. */
export const workspaceFreshMs = 30_000;
export type LoadMode = "refresh" | "append" | "activity";
export type CacheSnapshot<T> = { data?: T; loading: boolean; loadingMore: boolean; error: unknown; updatedAt: number; stale: boolean };
const empty = { loading: false, loadingMore: false, error: null, updatedAt: 0, stale: true };
type Entry<T> = { snapshot: CacheSnapshot<T>; revision: number; pending?: Promise<void>; controller?: AbortController };

export class RevalidationCache<K extends string, T> {
  private scope: string | null = null;
  private entries = new Map<K, Entry<T>>();
  private listeners = new Set<() => void>();
  private version = 0;
  constructor(private loader: (key: K, previous: T | undefined, mode: LoadMode, signal: AbortSignal) => Promise<T>, private now = Date.now) {}
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getVersion = () => this.version;
  private emit() { this.version++; for (const listener of this.listeners) listener(); }
  bind(scope: string | null) {
    if (this.scope === scope) return;
    for (const entry of this.entries.values()) entry.controller?.abort();
    this.scope = scope; this.entries.clear(); this.emit();
  }
  read(scope: string | null, key: K): CacheSnapshot<T> {
    return scope && scope === this.scope ? this.entries.get(key)?.snapshot || empty : empty;
  }
  invalidate(keys: readonly K[] = [...this.entries.keys()]) {
    for (const key of keys) {
      const entry = this.entries.get(key); if (!entry) continue;
      entry.revision++; entry.snapshot = { ...entry.snapshot, stale: true, error: null };
    }
    this.emit();
  }
  invalidateWhere(matches: (key: K) => boolean) {
    this.invalidate([...this.entries.keys()].filter(matches));
  }
  update(scope: string | null, key: K, change: (data: T) => T) {
    if (!scope || scope !== this.scope) return;
    const entry = this.entries.get(key);
    if (entry?.snapshot.data === undefined) return;
    const data = change(entry.snapshot.data);
    if (data === entry.snapshot.data) return;
    // A committed local edit wins over reads already in flight. Ranking may
    // have changed, but retained rows stay in place until an explicit refresh.
    entry.revision++; entry.snapshot = { ...entry.snapshot, data, stale: true };
    this.emit();
  }
  revalidate(scope: string | null, key: K, force = false, mode: LoadMode = "refresh"): Promise<void> {
    if (!scope || scope !== this.scope) return Promise.resolve();
    let entry = this.entries.get(key);
    if (!entry) { entry = { snapshot: empty, revision: 0 }; this.entries.set(key, entry); }
    if (entry.pending) return entry.pending;
    if (!force && entry.snapshot.data !== undefined && !entry.snapshot.stale && this.now() - entry.snapshot.updatedAt < workspaceFreshMs) return Promise.resolve();
    const current = entry, revision = entry.revision, controller = new AbortController();
    current.controller = controller;
    const valid = () => this.scope === scope && this.entries.get(key) === current;
    const unchanged = () => valid() && current.revision === revision;
    const previous = current.snapshot;
    current.snapshot = { ...previous, loading: true, loadingMore: mode === "append", error: null };
    current.pending = Promise.resolve().then(() => this.loader(key, previous.data, mode, controller.signal)).then(data => {
      if (unchanged()) current.snapshot = { ...current.snapshot, data, stale: mode === "activity" ? previous.stale : false,
        updatedAt: mode === "activity" ? previous.updatedAt : this.now(), error: null };
    }).catch(error => {
      if (!unchanged()) return;
      const status = (error as { status?: number } | null)?.status;
      if (status === 401 || status === 403) {
        // Expired/revoked access must not leave private results on screen.
        for (const [otherKey, other] of this.entries) if (otherKey !== key) other.controller?.abort();
        this.entries.clear(); this.entries.set(key, current);
        current.snapshot = { ...empty, error };
      } else current.snapshot = { ...current.snapshot, error, stale: true };
    }).finally(() => {
      if (!valid()) return;
      current.pending = undefined; current.controller = undefined;
      current.snapshot = { ...current.snapshot, loading: false, loadingMore: false }; this.emit();
    });
    this.emit(); return current.pending;
  }
}
