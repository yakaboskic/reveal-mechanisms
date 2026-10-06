"use client";

import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import "./research-mode-menu.css";

export function ResearchModeMenu({ disabled, onSelect }: { disabled: boolean; onSelect: (mode: "online" | "local") => void }) {
  const [open, setOpen] = useState(false);
  const id = useId(), root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  const initialFocus = useRef(0);
  const items = () => Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') || []);
  function close(restoreFocus = false) { setOpen(false); if (restoreFocus) trigger.current?.focus(); }
  useEffect(() => {
    if (!open || disabled) return;
    items()[initialFocus.current]?.focus();
    const outside = (event: Event) => { if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("focusin", outside); };
  }, [open, disabled]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  function navigate(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); return; }
    if (event.key === "Tab") { close(true); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const choices = items(), current = choices.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? choices.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length;
    choices[next]?.focus();
  }
  return <div className="research-mode-menu" ref={root}>
    <button type="button" ref={trigger} id={`${id}-trigger`} disabled={disabled} aria-haspopup="menu" aria-expanded={open && !disabled} aria-controls={open && !disabled ? `${id}-menu` : undefined}
      onClick={() => { initialFocus.current = 0; setOpen(value => !value); }}
      onKeyDown={event => { if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); initialFocus.current = event.key === "ArrowUp" ? 1 : 0; setOpen(true); } }}>
      Let’s close this gap <span aria-hidden="true">⌄</span>
    </button>
    {open && !disabled && <div className="research-mode-popup" id={`${id}-menu`} role="menu" aria-labelledby={`${id}-trigger`} onKeyDown={navigate}>
      <button type="button" className="research-mode-item" role="menuitem" tabIndex={-1} aria-label="Run online" aria-describedby={`${id}-online-description`} onClick={() => { close(true); onSelect("online"); }}><strong>Run online</strong><span id={`${id}-online-description`}>Run research in Reveal.</span></button>
      <button type="button" className="research-mode-item" role="menuitem" tabIndex={-1} aria-label="Use my local agent" aria-describedby={`${id}-local-description`} onClick={() => { close(true); onSelect("local"); }}><strong>Use my local agent</strong><span id={`${id}-local-description`}>Connect Codex or Claude Code.</span></button>
    </div>}
  </div>;
}
