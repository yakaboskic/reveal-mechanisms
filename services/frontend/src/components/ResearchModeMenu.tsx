"use client";

import { useEffect, useId, useRef, useState } from "react";
import "./research-mode-menu.css";

export function ResearchModeMenu({ disabled, onSelect }: {
  disabled: boolean;
  onSelect: (mode: "online" | "local") => void;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const initialFocus = useRef(0);
  const visible = open && !disabled;

  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus();
  }

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!visible) return;
    menu.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]")[initialFocus.current]?.focus();
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [visible]);

  return <div className="research-mode-menu" ref={root} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) close();
  }}>
    <button type="button" className="gap-submit" ref={trigger} id={`${id}-trigger`}
      disabled={disabled} aria-haspopup="menu" aria-expanded={visible} aria-controls={visible ? id : undefined}
      onClick={() => { initialFocus.current = 0; setOpen(!visible); }}
      onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault(); initialFocus.current = event.key === "ArrowUp" ? 1 : 0; setOpen(true);
        }
      }}>
      <span>Let’s close this gap</span><span className="send" aria-hidden="true"><span>↑</span></span>
    </button>
    {visible && <div className="research-mode-options" id={id} ref={menu} role="menu" aria-labelledby={`${id}-trigger`}
      onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); return; }
        const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("[role=menuitem]"));
        const current = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "ArrowDown" ? (current + 1) % items.length
          : event.key === "ArrowUp" ? (current + items.length - 1) % items.length
          : event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : null;
        if (next !== null) { event.preventDefault(); items[next]?.focus(); }
      }}>
      <button type="button" role="menuitem" tabIndex={-1} onClick={() => { close(true); onSelect("online"); }}>
        <span>Run online</span><small>Let Reveal run the investigation.</small>
      </button>
      <button type="button" role="menuitem" tabIndex={-1} onClick={() => { close(true); onSelect("local"); }}>
        <span>Use my local agent</span><small>Work with an agent on your computer.</small>
      </button>
    </div>}
  </div>;
}
