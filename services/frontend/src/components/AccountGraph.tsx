"use client";

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import type { Core, ElementDefinition, NodeSingular } from "cytoscape";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { accountGraphLabel, buildAccountHierarchy, flattenAccountHierarchy, graphKindLabel, mergeAccountPages, type AccountGraphNode } from "@/lib/account-graph";
import { layoutAccountHierarchy } from "@/lib/account-graph-layout";
import "./account-graph.css";

const colors = { account: "#f0f7fc", claim: "#d9eaf7", evidence: "#b8d8e9", dataset: "#d6cce9", file: "#e2eaf0", activity: "#f0e5d2", record: "#e8eaf0" };
type Props = { result: Schema<"AccountResult">; onReload: () => void; inspector: (node: AccountGraphNode, result: Schema<"AccountResult">) => ReactNode };
type IconKind = "claim" | "dataset" | "file";
type CircleMarker = { id: string; kind: IconKind; text: string; x: number; y: number; size: number };

function CircleIcon({ kind }: { kind: IconKind }) {
  return <svg className="account-circle-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    {kind === "claim" ? <path d="M5 3.5h10a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H9l-4 3v-3a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2Z" />
      : kind === "dataset" ? <><ellipse cx="10" cy="4.5" rx="6" ry="2.5" /><path d="M4 4.5v11c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5v-11M4 10c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5" /></>
      : <><path d="M11.5 2.5H4.5v15h11V6.5l-4-4Z" /><path d="M11.5 2.5v4h4M7 10h6M7 13h4" /></>}
  </svg>;
}

export function AccountGraph({ result: initial, onReload, inspector }: Props) {
  const [result, setResult] = useState(initial), [selectedId, setSelectedId] = useState("account");
  const [loading, setLoading] = useState(false), [error, setError] = useState(""), [expired, setExpired] = useState(false), [rendererError, setRendererError] = useState("");
  const container = useRef<HTMLDivElement>(null), cy = useRef<Core | null>(null), activeRequest = useRef<AbortController | null>(null), source = useRef(initial);
  const selectionHeading = useRef<HTMLHeadingElement>(null), instructionsId = useId(), tooltipId = useId(), tooltip = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(0), [circleMarkers, setCircleMarkers] = useState<CircleMarker[]>([]);
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null), [tooltipHeight, setTooltipHeight] = useState(100);
  useEffect(() => {
    source.current = initial; activeRequest.current?.abort(); activeRequest.current = null;
    setResult(initial); setSelectedId("account"); setHover(null); setError(""); setExpired(false); setLoading(false);
  }, [initial]);
  useEffect(() => () => activeRequest.current?.abort(), []);
  const root = useMemo(() => buildAccountHierarchy(result), [result]);
  const nodes = useMemo(() => flattenAccountHierarchy(root), [root]);
  const byId = useMemo(() => new Map(nodes.map(node => [node.id, node])), [nodes]);
  const selected = byId.get(selectedId) || root;
  const selectedNodeId = useRef(selected.id); selectedNodeId.current = selected.id;
  const path = nodes.filter(node => selected.id === node.id || selected.id.startsWith(`${node.id}/`));
  const up = path.at(-2);
  const showTooltip = (id: string, clientX?: number, clientY?: number) => {
    const rect = container.current?.getBoundingClientRect(), position = cy.current?.getElementById(id).renderedPosition();
    if (rect) setHover({ id, x: clientX === undefined ? position?.x || rect.width / 2 : clientX - rect.left, y: clientY === undefined ? position?.y || rect.height / 2 : clientY - rect.top });
  };
  const tooltipNode = hover && byId.get(hover.id);
  const tooltipWidth = Math.min(280, Math.max(160, (container.current?.clientWidth || 304) - 24));
  useEffect(() => { if (tooltip.current) setTooltipHeight(tooltip.current.offsetHeight); }, [tooltipNode?.label, tooltipWidth]);
  const layout = useMemo(() => layoutAccountHierarchy(root), [root]);
  useEffect(() => {
    if (!container.current) return;
    let active = true, frame = 0;
    let instance: Core | null = null;
    let observer: ResizeObserver | null = null;
    setRendererError(""); setCircleMarkers([]);
    void import("cytoscape").then(({ default: cytoscape }) => {
      if (!active || !container.current) return;
      const claimLabels = new Map(root.children.map((claim, index) => [claim.objectId, `C${index + 1}`]));
      const elements: ElementDefinition[] = layout.map(node => ({ group: "nodes", position: { x: node.x, y: node.y },
        data: { id: node.data.id, objectId: node.data.objectId, diameter: node.r * 2, depth: node.depth, color: colors[node.data.kind], label: node.depth === 0 ? "Scientific account" : node.data.kind === "claim" ? claimLabels.get(node.data.objectId) || "" : "", labelOffset: -node.r + (node.depth === 0 ? 30 : 20) },
        classes: [node.data.kind, node.data.missing ? "missing" : ""].join(" "), locked: true }));
      instance = cytoscape({ container: container.current, elements, layout: { name: "preset", fit: true, padding: 50 },
        minZoom: 0.15, maxZoom: 5, wheelSensitivity: 0.2, autoungrabify: true, boxSelectionEnabled: false, selectionType: "single",
        style: [{ selector: "node", style: { shape: "ellipse", width: "data(diameter)", height: "data(diameter)", "background-color": "data(color)", "background-opacity": 1, "border-color": "#8daec5", "border-width": 1,
          "z-index-compare": "manual", "z-index": (node: NodeSingular) => node.data("depth"), label: "data(label)", "font-family": "Arial, sans-serif", "font-size": 24, color: "#354e65", "text-valign": "center", "text-halign": "center", "text-margin-y": (node: NodeSingular) => node.data("labelOffset") } },
        { selector: ".missing", style: { "border-style": "dashed", "background-color": "#fafbfd" } },
        // Claim numbers sit inside their circles above nested dataset nodes.
        // Their small hit targets always inspect the claim named by the number.
        { selector: ".claim", style: { label: "" } },
        { selector: ".outside-focus", style: { label: "", "border-opacity": 0.15, "background-opacity": 0.12 } },
        { selector: ".highlight", style: { "border-color": "#325f93", "border-width": 1.2 } },
        { selector: ":selected", style: { "border-color": "#254f83", "border-width": 1.5 } }],
      });
      cy.current = instance;
      const updateViewport = () => {
        if (frame) return;
        frame = requestAnimationFrame(() => {
          frame = 0;
          if (!active || !instance) return;
          const markers: CircleMarker[] = [];
          instance.nodes(".claim, .dataset, .file").forEach(node => {
            const kind: IconKind = node.hasClass("claim") ? "claim" : node.hasClass("dataset") ? "dataset" : "file";
            const diameter = node.renderedHeight(), position = node.renderedPosition();
            // Keep small source circles quiet; zoom reveals their line symbols.
            if (kind !== "claim" && diameter < (kind === "dataset" ? 40 : 24)) return;
            const size = kind === "claim" ? diameter < 42 ? 0 : 13 : Math.max(11, Math.min(17, diameter * 0.2));
            markers.push({ id: node.id(), kind, text: node.data("label"), x: position.x, size,
              y: kind === "file" ? position.y - size / 2 : position.y - diameter / 2 + (kind === "claim" ? Math.max(4, Math.min(10, diameter * 0.07)) : 3) });
          });
          setCircleMarkers(markers);
        });
      };
      instance.on("tap", "node", event => { setSelectedId(event.target.id()); container.current?.focus({ preventScroll: true }); });
      instance.on("mouseover mousemove", "node", event => {
        const pointer = event.originalEvent as { clientX?: number; clientY?: number } | undefined;
        showTooltip(event.target.id(), pointer?.clientX, pointer?.clientY);
      });
      instance.on("mouseout", "node", event => setHover(previous => previous?.id === event.target.id() ? null : previous));
      instance.on("pan zoom resize", updateViewport);
      let observedWidth = container.current.clientWidth, observedHeight = container.current.clientHeight;
      observer = new ResizeObserver(([entry]) => {
        if (!instance) return;
        const { width, height } = entry.contentRect;
        const changed = Math.abs(width - observedWidth) > 1 || Math.abs(height - observedHeight) > 1;
        observedWidth = width; observedHeight = height;
        instance.resize();
        if (changed) {
          const target = instance.getElementById(selectedNodeId.current);
          instance.stop(); if (target.length) instance.fit(target, 65);
        }
        updateViewport();
      }); observer.observe(container.current);
      updateViewport(); setReady(value => value + 1);
    }).catch(() => { if (active) setRendererError("The diagram could not load. Choose a scientific record below to inspect it."); });
    return () => { active = false; cancelAnimationFrame(frame); observer?.disconnect(); instance?.destroy(); if (cy.current === instance) cy.current = null; };
  }, [layout]);
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;
    instance.nodes().removeClass("highlight outside-focus").unselect();
    instance.nodes().filter(node => node.id() !== selected.id && !node.id().startsWith(`${selected.id}/`)).addClass("outside-focus");
    instance.nodes().filter(node => node.data("objectId") === selected.objectId).addClass("highlight");
    const target = instance.getElementById(selected.id); target.select();
    if (target.length) {
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      instance.stop();
      if (reduced) instance.fit(target, 65);
      else instance.animate({ fit: { eles: target, padding: 65 }, duration: 240 });
      if (document.activeElement === container.current) showTooltip(selected.id);
    }
  }, [selected.id, selected.objectId, ready]);
  const zoom = (factor: number) => {
    const instance = cy.current;
    if (instance) instance.zoom({ level: Math.max(0.15, Math.min(5, instance.zoom() * factor)), renderedPosition: { x: instance.width() / 2, y: instance.height() / 2 } });
  };
  const navigateGraph = (event: KeyboardEvent<HTMLDivElement>) => {
    const siblings = up?.children || root.children, index = siblings.findIndex(node => node.id === selected.id);
    const next = event.key === "ArrowRight" ? selected.children[0] : event.key === "ArrowLeft" || event.key === "Escape" ? up
      : event.key === "ArrowDown" ? siblings[(index + 1) % siblings.length] : event.key === "ArrowUp" ? siblings[(index - 1 + siblings.length) % siblings.length] : event.key === "Home" ? root : undefined;
    if (next) { event.preventDefault(); setSelectedId(next.id); }
    else if (event.key === "Enter") { event.preventDefault(); selectionHeading.current?.focus({ preventScroll: true }); }
    else if (event.key === "+" || event.key === "=" || event.key === "-") { event.preventDefault(); zoom(event.key === "-" ? 1 / 1.4 : 1.4); }
  };
  const loadMore = async () => {
    const cursor = result.coverage.next_cursor;
    if (!cursor || activeRequest.current) return;
    const controller = new AbortController(); activeRequest.current = controller;
    const binding = source.current;
    setLoading(true); setError("");
    try {
      const page = await api.accountPage(result.root_id, cursor, controller.signal);
      if (controller.signal.aborted || source.current !== binding) return;
      const merged = mergeAccountPages(result, page); setResult(merged);
    } catch (failure) {
      if (!controller.signal.aborted && source.current === binding) { setError(messageOf(failure)); setExpired((failure instanceof ApiError && failure.status === 409) || messageOf(failure).includes("account source changed")); }
    } finally { if (activeRequest.current === controller) { activeRequest.current = null; setLoading(false); } }
  };
  return <section className="account-explorer" aria-label="Scientific account diagram">
    <nav className="account-explorer-breadcrumbs" aria-label="Source path">{path.map((node, index) => <span key={node.id}>{index > 0 && <span aria-hidden="true"> / </span>}<button onClick={() => setSelectedId(node.id)} aria-current={node.id === selected.id ? "location" : undefined} title={node.label}>{index === 0 ? "Account" : accountGraphLabel(node, 40)}</button></span>)}</nav>
    <div className="account-explorer-layout"><div className="account-explorer-main">
      <div className="account-explorer-controls"><button disabled={!up} onClick={() => up && setSelectedId(up.id)}>← Back</button><button onClick={() => { setSelectedId("account"); cy.current?.fit(cy.current.getElementById("account"), 65); }}>Reset view</button><div><button aria-label="Zoom out" onClick={() => zoom(1 / 1.4)}>−</button><button aria-label="Zoom in" onClick={() => zoom(1.4)}>+</button></div></div>
      <p className="sr-only" id={instructionsId}>Hover a circle to read its full text. Claim numbers match Associated claims in Conclusions. Use right arrow to enter a circle, left arrow to go back, up and down arrows to move between siblings, Home to reset, plus and minus to zoom, and Enter to read the inspector. Escape dismisses hover text. Drag to pan.</p>
      <div className="account-bubbles-frame" onMouseLeave={() => setHover(null)} onKeyDownCapture={event => { if (event.key === "Escape" && hover) { event.preventDefault(); event.stopPropagation(); setHover(null); } }}>
        <div className="account-bubbles" ref={container} role="group" aria-roledescription="interactive circle diagram" tabIndex={0} onKeyDown={navigateGraph} onFocus={() => showTooltip(selected.id)} onBlur={() => setHover(null)} aria-label={`${graphKindLabel(selected.kind)}: ${selected.label}`} aria-describedby={`${instructionsId}${tooltipNode ? ` ${tooltipId}` : ""}`} />
        <div className="account-bubble-labels">{circleMarkers.filter(marker => marker.id === selected.id || marker.id.startsWith(`${selected.id}/`)).map(marker => <button key={marker.id} className={marker.kind === "claim" ? "account-bubble-label" : `account-bubble-source-icon account-bubble-source-icon--${marker.kind}`} style={{ left: marker.x, top: marker.y }} tabIndex={marker.kind === "claim" ? undefined : -1} aria-label={`${marker.text || graphKindLabel(marker.kind)}: ${byId.get(marker.id)?.label}`} aria-describedby={hover?.id === marker.id ? tooltipId : undefined} onMouseEnter={event => showTooltip(marker.id, event.clientX, event.clientY)} onMouseLeave={() => setHover(null)} onFocus={() => showTooltip(marker.id)} onBlur={() => setHover(null)} onClick={() => { setSelectedId(marker.id); container.current?.focus({ preventScroll: true }); }}>{marker.size > 0 && <span className="account-bubble-symbol" style={{ width: marker.size, height: marker.size }}><CircleIcon kind={marker.kind} /></span>}{marker.text}</button>)}</div>
        {tooltipNode && hover && <div ref={tooltip} id={tooltipId} role="tooltip" className="account-bubble-tooltip" style={{ width: tooltipWidth, left: Math.max(8, Math.min(hover.x + 14, (container.current?.clientWidth || 304) - tooltipWidth - 8)), top: Math.max(8, Math.min(hover.y + 14, (container.current?.clientHeight || 620) - tooltipHeight - 8)) }}><small>{graphKindLabel(tooltipNode.kind)}</small><p>{tooltipNode.label}</p>{tooltipNode.missing && <small>Referenced record not loaded</small>}</div>}
      </div>
      <p className="sr-only" role="status">{graphKindLabel(selected.kind)}: {selected.label}. {selected.children.length} contained records.{selected.missing ? " Record not loaded." : ""}</p>
      {rendererError && <div className="notice" role="status"><p>{rendererError}</p><label>Scientific record<select aria-label="Choose a scientific record" value={selected.id} onChange={event => setSelectedId(event.target.value)}>{nodes.map(node => <option key={node.id} value={node.id}>{graphKindLabel(node.kind)}: {node.label}</option>)}</select></label></div>}
      <div className="account-explorer-legend" aria-label="Circle types">{(["claim", "dataset", "file"] as const).map(kind => <span key={kind}><i style={{ background: colors[kind] }}><CircleIcon kind={kind} /></i>{graphKindLabel(kind)}</span>)}</div><p className="account-explorer-scale">Circle sizes vary for visual clarity and do not indicate evidence strength. Shared sources appear within each use.</p>
      {(selected.limited || selected.cycle || selected.missingReferences.length > 0) && <p className="notice">{selected.limited && "Some source paths reach the display limit. "}{selected.cycle && "Repeated provenance paths are shown once. "}{selected.missingReferences.length > 0 && "Some referenced source records have not been loaded. "}Inspect the scientific record for its full references.</p>}
      {!result.coverage.complete && <div className="account-explorer-coverage"><p>This source view is bounded. Missing records are not evidence of absence.</p>{result.coverage.next_cursor && !expired ? <button disabled={loading} onClick={() => void loadMore()}>{loading ? "Loading source records…" : "Load more source records"}</button> : <p>Further references may require opening their individual scientific records.</p>}</div>}
      {error && <div role="alert" className="error"><span>{error}</span>{expired && <button onClick={onReload}>Reload account</button>}</div>}
    </div><aside className="account-explorer-inspector" aria-label="Selected scientific record"><p className="account-explorer-kind">{graphKindLabel(selected.kind)}{selected.shared > 1 ? " · Shared source" : ""}</p><h3 ref={selectionHeading} tabIndex={-1}>{selected.label}</h3><p className="account-explorer-relation">{selected.relation}{selected.direction ? ` · ${selected.direction.toLowerCase().replaceAll("_", " ")}` : ""}</p>{inspector(selected, result)}</aside></div>
  </section>;
}
