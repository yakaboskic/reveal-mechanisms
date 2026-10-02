"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

export function DraftNavigation() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);
  return <div className="draft-back" ref={root} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }} onKeyDown={event => {
    if (event.key === "Escape" && open) { event.preventDefault(); setOpen(false); trigger.current?.focus(); }
  }}>
    <button type="button" ref={trigger} className="draft-back-trigger" aria-expanded={open} aria-controls="draft-back-destinations" onClick={() => setOpen(value => !value)}>
      <span aria-hidden="true">←</span> Back <svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" aria-hidden="true"><path d="m3 4.5 3 3 3-3" /></svg>
    </button>
    {open && <div id="draft-back-destinations" className="draft-back-options">
      <Link href="/">Knowledge gaps</Link>
      <Link href="/workspace?tab=drafts">Saved drafts</Link>
    </div>}
  </div>;
}
