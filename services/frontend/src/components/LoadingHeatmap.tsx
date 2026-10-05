"use client";

import { useEffect, useId, useRef, useState, type CSSProperties } from "react";
import Link from "next/link";
import { loadingAppearance, loadingColors, loadingLevel, loadingMembershipCounts, loadingNavigation, loadingValue, type LoadingCell } from "@/lib/loading-visual";
import "./loading-heatmap.css";

export type { LoadingCell } from "@/lib/loading-visual";
export type LoadingHeatmapProps = {
  items: readonly LoadingCell[];
  min: number | null;
  max: number | null;
  label: string;
  orderingLabel?: string;
  overlayLabel?: string;
  onAction?: (item: LoadingCell) => void;
  actionLabel?: string;
};
type Pointer = { id: string; left: number; top: number };

export function LoadingHeatmap({ items, min, max, label, orderingLabel = "rank", overlayLabel, onAction, actionLabel }: LoadingHeatmapProps) {
  const identity = useId(), root = useRef<HTMLDivElement>(null), grid = useRef<HTMLDivElement>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const [columns, setColumns] = useState(14), [focused, setFocused] = useState<string | null>(null);
  const [hover, setHover] = useState<Pointer | null>(null), [focus, setFocus] = useState<Pointer | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const tabStop = items.some(item => item.id === focused) ? focused : items[0]?.id;
  const active = hover || focus;
  const inspected = items.find(item => item.id === active?.id);
  const selected = items.find(item => item.id === pinned);
  const rows = Array.from({ length: Math.ceil(items.length / columns) }, (_, row) => items.slice(row * columns, (row + 1) * columns));
  const scaleAvailable = min !== null && max !== null && Number.isFinite(min) && Number.isFinite(max) && max >= min;
  const overlay = overlayLabel?.trim() || undefined, membershipCounts = loadingMembershipCounts(items);
  const inspectedMembership = inspected ? loadingAppearance(inspected, min, max, overlay).membershipLabel : null;
  const selectedMembership = selected ? loadingAppearance(selected, min, max, overlay).membershipLabel : null;

  useEffect(() => {
    const target = grid.current;
    if (!target) return;
    const coarse = window.matchMedia("(pointer: coarse)");
    const measure = () => setColumns(Math.max(1, Math.min(40, Math.floor((target.clientWidth + 4) / (coarse.matches ? 48 : 24)))));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(target);
    coarse.addEventListener("change", measure);
    return () => { observer.disconnect(); coarse.removeEventListener("change", measure); };
  }, []);

  const point = (id: string, element: HTMLButtonElement): Pointer => {
    const tile = element.getBoundingClientRect(), container = root.current!.getBoundingClientRect();
    const width = Math.min(300, container.width);
    return { id, left: Math.max(0, Math.min(container.width - width, tile.left - container.left + tile.width / 2 - width / 2)), top: tile.bottom - container.top + 8 };
  };
  const keyboard = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === "Escape") { setPinned(null); setHover(null); setFocus(null); return; }
    const next = loadingNavigation(index, event.key, items.length, columns, event.ctrlKey || event.metaKey);
    if (next === null) return;
    event.preventDefault(); setHover(null); setFocused(items[next].id); buttons.current.get(items[next].id)?.focus();
  };

  return <section className="loading-heatmap" aria-label={`${label} heatmap`}>
    <p className="loading-heatmap-instructions" id={`${identity}-instructions`}>Each square is one loading. Order: {orderingLabel}. Hover or select a square to inspect it.<span className="sr-only"> Use arrow keys to move between squares. Enter or Space keeps a loading open; Escape clears it.</span></p>
    <div className="loading-heatmap-body" ref={root}>
      <div ref={grid} className="loading-heatmap-grid" role="grid" aria-label={label} aria-describedby={`${identity}-instructions${overlay ? ` ${identity}-overlay` : ""}`} aria-rowcount={rows.length} aria-colcount={columns} style={{ "--loading-columns": columns } as CSSProperties}>
        {rows.map((row, rowIndex) => <div className="loading-heatmap-row" role="row" aria-rowindex={rowIndex + 1} key={row[0].id}>
          {row.map((item, column) => {
            const { level, membership, membershipLabel } = loadingAppearance(item, min, max, overlay);
            return <div role="gridcell" aria-colindex={column + 1} className="loading-heatmap-cell" key={item.id}>
              <button type="button" ref={element => { if (element) buttons.current.set(item.id, element); else buttons.current.delete(item.id); }}
                className={`loading-heatmap-tile${level === null ? " loading-heatmap-tile-unknown" : ""}${membership ? ` loading-heatmap-tile-${membership === "unknown" ? "membership-unknown" : membership}` : ""}`}
                style={level === null ? undefined : { backgroundColor: loadingColors[level] }} tabIndex={item.id === tabStop ? 0 : -1}
                aria-label={`${item.label}; loading ${loadingValue(item.loading)}; rank ${item.rank}${membershipLabel ? `; ${membershipLabel}` : ""}`}
                aria-pressed={item.id === pinned} aria-describedby={inspected?.id === item.id ? `${identity}-tooltip` : undefined}
                onPointerEnter={event => { if (event.pointerType !== "touch") setHover(point(item.id, event.currentTarget)); }} onPointerLeave={() => setHover(null)}
                onFocus={event => { setFocused(item.id); setFocus(point(item.id, event.currentTarget)); }} onBlur={() => setFocus(null)}
                onClick={() => { setPinned(previous => previous === item.id ? null : item.id); setHover(null); setFocus(null); }}
                onKeyDown={event => keyboard(event, rowIndex * columns + column)} />
            </div>;
          })}
        </div>)}
      </div>
      {inspected && active && <div className="loading-heatmap-tooltip" role="tooltip" id={`${identity}-tooltip`} style={{ left: active.left, top: active.top }}>
        <strong>{inspected.label}</strong><span>Loading {loadingValue(inspected.loading)} <span aria-hidden="true">·</span> Rank {inspected.rank}</span>
        {inspectedMembership && <span className="loading-heatmap-membership">{inspectedMembership}</span>}
        {inspected.description && <small>{inspected.description}</small>}
      </div>}
    </div>
    <div className="loading-heatmap-legend" aria-label={scaleAvailable ? `Loading scale ${loadingValue(min)} to ${loadingValue(max)}` : "Loading scale unavailable"}>
      {scaleAvailable ? <><span>{loadingValue(min)}</span><span className="loading-heatmap-swatches" aria-hidden="true">{loadingColors.map(color => <i key={color} style={{ backgroundColor: color }} />)}</span><span>{loadingValue(max)}</span><span className="loading-heatmap-scale-note">Loading</span></> : <span>Loading scale unavailable</span>}
      {items.some(item => loadingLevel(item.loading, min, max) === null) && <span className="loading-heatmap-unknown-key"><i className="loading-heatmap-tile-unknown" aria-hidden="true" />{scaleAvailable ? "Not available" : "Unscaled"}</span>}
    </div>
    {overlay && <div className="loading-heatmap-overlay" id={`${identity}-overlay`} aria-live="polite">
      <p><strong>{membershipCounts.members} of {membershipCounts.total} shown loadings</strong> are members of {overlay}.{membershipCounts.unknown > 0 && <> Membership is unknown for {membershipCounts.unknown}.</>}</p>
      <span><i className="loading-heatmap-member-key" aria-hidden="true" />Members</span><span><i className="loading-heatmap-nonmember-key" aria-hidden="true" />Nonmembers use smaller squares</span>{membershipCounts.unknown > 0 && <span><i className="loading-heatmap-membership-unknown-key" aria-hidden="true" />Membership unknown</span>}<span className="loading-heatmap-overlay-note">Colors still show loading on the same scale.</span>
    </div>}
    {selected && <div className="loading-heatmap-selection" aria-live="polite">
      <div><strong>{selected.label}</strong><span>Loading <b>{loadingValue(selected.loading)}</b> <span aria-hidden="true">·</span> Rank {selected.rank}</span>{selectedMembership && <span className="loading-heatmap-membership">{selectedMembership}</span>}{selected.description && <small>{selected.description}</small>}
        {(selected.href || (onAction && actionLabel)) && <div className="loading-heatmap-selection-actions">
          {selected.href && <Link href={selected.href}>View gene set and provenance <span aria-hidden="true">↗</span></Link>}
          {onAction && actionLabel && <button type="button" onClick={() => onAction(selected)}>{actionLabel}</button>}
        </div>}
      </div>
      <button type="button" aria-label="Clear selected loading" onClick={() => { setPinned(null); buttons.current.get(selected.id)?.focus(); }}>×</button>
    </div>}
  </section>;
}
