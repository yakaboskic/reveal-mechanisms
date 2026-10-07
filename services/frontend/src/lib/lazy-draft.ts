/** An opened knowledge gap's temporary draft, created only when something first needs its id: an attachment, the
 * CFDE check, Save or Submit. Opening, browsing and leaving a gap write no draft and delete none.
 * One creation per editor (`epoch`), shared by every caller; a failed one can be retried. A creation that settles
 * after its editor was replaced, or after another draft took its place, is discarded rather than adopted. */
export class LazyDraft<D> {
  private pending: { epoch: number; promise: Promise<D | null> } | null = null;
  constructor(private readonly deps: {
    epoch: () => number;
    current: () => D | null;
    create: () => Promise<D>;
    /** false: the editor can no longer take a draft (it unmounted, or a submission owns its draft). */
    adopt: (draft: D) => boolean;
    discard: (draft: D) => void;
  }) {}
  /** The editor's draft, creating it if needed. null: the editor that asked is gone. */
  ensure(): Promise<D | null> {
    const existing = this.deps.current();
    if (existing) return Promise.resolve(existing);
    const epoch = this.deps.epoch();
    if (this.pending?.epoch === epoch) return this.pending.promise;
    const promise: Promise<D | null> = this.deps.create().then(created => {
      if (this.deps.epoch() !== epoch) { this.deps.discard(created); return null; }
      const current = this.deps.current();
      if (current) { this.deps.discard(created); return current; }
      if (!this.deps.adopt(created)) { this.deps.discard(created); return null; }
      return created;
    }).finally(() => { if (this.pending?.promise === promise) this.pending = null; });
    this.pending = { epoch, promise };
    return promise;
  }
  /** Settles once a creation for the current editor, if one is running, has finished either way. */
  settled(): Promise<unknown> {
    const pending = this.pending;
    return pending && pending.epoch === this.deps.epoch() ? pending.promise.catch(() => null) : Promise.resolve();
  }
}
