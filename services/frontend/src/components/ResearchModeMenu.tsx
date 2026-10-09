"use client";

import { useEffect, useId, useRef, useState } from "react";
import { CfdeEstimateRing } from "./CfdeAssessmentView";
import { assessmentSupportLabel, type CfdeAssessment } from "../lib/cfde-assessment";
import { lightningEnabled, type ResearchMode } from "../lib/lightning-audit";
import "./research-mode-menu.css";

const modeChoices: { mode: ResearchMode; label: string; description: string }[] = [
  { mode: "online", label: "Run online", description: "Let Reveal run the investigation." },
  { mode: "local", label: "Use my local agent", description: "Work with an agent on your computer." },
  ...(lightningEnabled ? [{ mode: "lightning" as const, label: "Lightning audit", description: "Assess this evidence and suggest a direction." }] : []),
];

export function ResearchModeMenu({ disabled, onSelect, assessment, assessing = false }: {
  disabled: boolean;
  onSelect: (mode: ResearchMode) => void;
  assessment?: CfdeAssessment | null; assessing?: boolean;
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
      aria-describedby={assessment?.result || assessing ? `${id}-estimate` : undefined}
      onClick={() => { initialFocus.current = 0; setOpen(!visible); }}
      onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault(); initialFocus.current = event.key === "ArrowUp" ? modeChoices.length - 1 : 0; setOpen(true);
        }
      }}>
      <span>Let’s close this gap</span><span className="send cfde-estimate-arrow" aria-hidden="true"><span>↑</span>{(assessing || assessment?.result) && <CfdeEstimateRing loading={assessing} probability={assessment?.result?.probability_yes} />}</span>
    </button>
    {(assessing || assessment?.result) && <span className="sr-only" id={`${id}-estimate`}>{assessing ? "Assessing likely CFDE support" : assessmentSupportLabel(assessment!.result!.probability_yes)}</span>}
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
      {modeChoices.map(choice => <button key={choice.mode} type="button" role="menuitem" tabIndex={-1} onClick={() => { close(true); onSelect(choice.mode); }}>
        <span>{choice.label}</span><small>{choice.description}</small>
      </button>)}
    </div>}
  </div>;
}
