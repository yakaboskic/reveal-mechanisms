"use client";
import { changedWorkspace, sessionReloadKey } from "@/lib/browser-session";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { version as clientVersion } from "../../package.json";
import { api, ApiError, backend, errorMessage, request, type CfdeAssessment, type FactorLoading, type LightningAudit, type LightningAuditProgress, type LightningAuditResult } from "../lib/api";
import { followJob, followWorkspace } from "../lib/events";
import { createMutationKeys } from "../lib/mutations";
import { emptyComposer, terminal, withFactors, type AnalysisInput, type Composer, type Draft, type Factor, type Gap, type Job, type JobEvent, type Me, type Schema } from "../lib/types";

type PendingSubmission = { body: AnalysisInput; key: string };
type FeasibilityRun = { draft: Draft; cfde: "yes" | "no"; audit: LightningAudit | null };
type ParagraphProgress = { job?: Job; text?: string; error?: string };
const readable = (value: string) => value.replaceAll("_", " ");
function outcomeLabel(status: string) {
  if (status === "succeeded") return "Success";
  if (status === "failed") return "Failed";
  if (status === "cancelled") return "Cancelled";
  if (status === "insufficient_evidence") return "Insufficient evidence";
  return readable(status);
}
const date = (value: string) => new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const setupStages = new Set(["queued", "freezing_inputs", "retrieving_cfde", "preparing_evidence", "starting_agent"]);
function analysisHasStarted(job: Job) {
  return job.kind !== "analysis" || terminal(job.status) || !setupStages.has(job.stage);
}
function selectionSignature(value: Pick<Composer, "source_gap" | "eaggl_anchors">) {
  return [value.source_gap?.source_id || "", ...value.eaggl_anchors.map(anchor => anchor.reference.source_id).sort()].join("\n");
}
function investigationDraftName(search: string, gap: Gap | null) {
  const stamp = date(new Date().toISOString());
  const source = search.trim() || gap?.object.text || gap?.source.disease_label || "Investigation";
  const label = source.replace(/\s+/g, " ").trim();
  const suffix = ` · ${stamp}`;
  const room = 120 - suffix.length;
  const head = label.length > room ? label.slice(0, Math.max(1, room - 1)).trimEnd() + "…" : label;
  return head + suffix;
}
function splitDraftTitle(name: string) {
  const mark = name.lastIndexOf(" · ");
  if (mark < 1) return null;
  const saved = name.slice(mark + 3).trim();
  if (!/^[A-Za-z]{3,9}\.?\s+\d{1,2},\s+\d{1,2}:\d{2}\s*[AP]M$/i.test(saved)) return null;
  return { query: name.slice(0, mark).trim(), saved };
}
const gapTitle = (gap: Gap) => gap.object.name || gap.object.text || "Knowledge gap";
const gapBody = (gap: Gap) => gap.object.gap_description || gap.object.text || gap.source.source_id;
function accountLevel(count: number, max: number) {
  if (max <= 0 || count <= 0) return 0;
  const ratio = count / max;
  return ratio >= 0.66 ? 2 : ratio >= 0.33 ? 1 : 0;
}
function GapOption({ gap, selected, disabled, maxAccounts, onSelect, onInspect }: { gap: Gap; selected: boolean; disabled: boolean; maxAccounts: number; onSelect: () => void; onInspect: () => void }) {
  const bodyRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const body = gapBody(gap);
  const accounts = gap.scientific_accounts?.count ?? 0;
  const upvotes = gap.votes?.upvotes ?? 0;
  const downvotes = gap.votes?.downvotes ?? 0;
  const voteTone = (gap.votes?.score ?? upvotes - downvotes) > 0 ? "up" : (gap.votes?.score ?? upvotes - downvotes) < 0 ? "down" : "even";
  useEffect(() => {
    const node = bodyRef.current;
    if (!node || open) return;
    const measure = () => setOverflows(node.scrollHeight > node.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [body, open]);
  return <div className={selected ? "gap-option selected" : "gap-option"} aria-disabled={disabled || undefined}>
    <div className="gap-main">
      <strong>{gapTitle(gap)}</strong>
      <span ref={bodyRef} className={open ? "expanded" : undefined}>{body}</span>
      <div className="gap-meta">
        {(overflows || open) && <button type="button" className="read-more" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? "Show less" : "Read more"}</button>}
        <div className="gap-bubbles">
          <span className={"gap-bubble accounts level-" + accountLevel(accounts, maxAccounts)} aria-label={accounts + " connected accounts"}>{accounts}</span>
          <span className={"gap-bubble votes tone-" + voteTone} aria-label={upvotes + " upvotes, " + downvotes + " downvotes"}><span className="vote-up">{upvotes}</span><span className="vote-bar" aria-hidden="true">|</span><span className="vote-down">{downvotes}</span></span>
          <button type="button" className="gap-bubble inspect" onClick={onInspect}>Inspect gap</button>
        </div>
      </div>
    </div>
    <div className="gap-select">
      <button type="button" disabled={disabled} aria-pressed={selected} onClick={() => { if (!disabled) onSelect(); }}>Select</button>
    </div>
  </div>;
}
const factorTitle = (factor: Factor) => factor.cfde_anchor.label || factor.object.name || factor.source_id;
function gapMechanisms(gap: Gap | null) {
  if (!gap) return [];
  return attachedMechanisms(gap).flatMap(item => {
    const id = item.target?.source_id;
    return id ? [{ id, label: item.label || id }] : [];
  });
}
function matchedMechanisms(gap: Gap | null, ids: string[]) {
  const catalog = gapMechanisms(gap);
  const bySource = new Map(catalog.map(item => [item.id, item]));
  const resolved = ids.flatMap(id => {
    const item = bySource.get(id);
    return item ? [item] : [];
  });
  if (resolved.length) return resolved;
  // A research-context search is one context. The linked mechanisms stay inside that node.
  if (ids.includes("mechanism_subquery")) return [{ id: "mechanism_subquery", label: "Research context" }];
  return ids.map(id => ({ id, label: id }));
}
type FactorNetRow = { sourceId: string; title: string; subtitle: string; mechanisms: { id: string; label: string }[]; cosine: number | null; selected: boolean; selectDisabled: boolean; onToggle: () => void; onInspect: (() => void) | null };
function edgePaint(value: number | null) {
  if (value == null || !Number.isFinite(value)) return { stroke: "#b7b9be", width: 1 };
  const clamped = Math.max(-1, Math.min(1, value));
  if (clamped === 0) return { stroke: "#b7b9be", width: 1 };
  return { stroke: clamped < 0 ? "#f3ccc8" : "#c5daf0", width: 1.25 + Math.abs(clamped) * 5 };
}
function curvePath(x1: number, y1: number, x2: number, y2: number) {
  const bend = Math.max(28, Math.abs(x2 - x1) * 0.46);
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
}
function FactorNetwork({ gapLabel, rows, guide, contextNode }: { gapLabel: string; rows: FactorNetRow[]; guide?: string; contextNode?: { text: string; mechanisms: { id: string; label: string }[] } | null }) {
  const root = useRef<HTMLDivElement>(null);
  const [graph, setGraph] = useState<{ width: number; height: number; links: { x1: number; y1: number; x2: number; y2: number; stroke: string; width: number; key: string; curve: boolean; label: string }[] }>({ width: 0, height: 0, links: [] });
  const mechanisms = useMemo(() => {
    const seen = new Map<string, string>();
    for (const row of rows) for (const item of row.mechanisms) if (!seen.has(item.id)) seen.set(item.id, item.label);
    return [...seen].map(([id, label]) => ({ id, label }));
  }, [rows]);
  const layoutKey = gapLabel + "|" + (contextNode ? contextNode.text + "|" + contextNode.mechanisms.map(item => item.id).join(",") : "") + "|" + mechanisms.map(item => item.id).join(",") + "|" + rows.map(row => row.sourceId + ":" + row.cosine + ":" + row.selected + ":" + row.mechanisms.map(item => item.id).join(",")).join(";");
  useLayoutEffect(() => {
    const rootEl = root.current;
    if (!rootEl) return;
    const draw = () => {
      const origin = rootEl.getBoundingClientRect();
      const point = (id: string, side: "left" | "right") => {
        const el = rootEl.querySelector(`[data-net-id="${CSS.escape(id)}"]`);
        if (!(el instanceof HTMLElement)) return null;
        const box = el.getBoundingClientRect();
        return { x: (side === "left" ? box.left : box.right) - origin.left, y: box.top - origin.top + box.height / 2 };
      };
      const next: { x1: number; y1: number; x2: number; y2: number; stroke: string; width: number; key: string; curve: boolean; label: string }[] = [];
      for (const item of mechanisms) {
        const from = point("gap", "right");
        const to = point("mech:" + item.id, "left");
        if (from && to) next.push({ x1: from.x, y1: from.y, x2: to.x, y2: to.y, stroke: "#b7b9be", width: 1, key: "gap-" + item.id, curve: true, label: "" });
      }
      for (const row of rows) {
        const paint = edgePaint(row.cosine);
        const label = row.cosine == null ? "" : row.cosine.toLocaleString(undefined, { maximumFractionDigits: 3 });
        for (const item of row.mechanisms) {
          const from = point("mech:" + item.id, "right");
          const to = point("factor:" + row.sourceId, "left");
          if (from && to) next.push({ x1: from.x, y1: from.y, x2: to.x, y2: to.y, ...paint, key: item.id + "-" + row.sourceId, curve: true, label });
        }
      }
      setGraph({ width: origin.width, height: origin.height, links: next });
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(rootEl);
    return () => observer.disconnect();
  }, [layoutKey, mechanisms, rows]);
  return <figure className="factor-net-figure">
    <figcaption className="factor-net-legend">Cosine similarity: <span>-1</span><svg viewBox="0 0 28 14" aria-hidden="true"><polygon points="0,1 0,13 28,7" fill="#f3ccc8" /></svg><span>0</span><svg viewBox="0 0 28 14" aria-hidden="true"><polygon points="0,7 28,1 28,13" fill="#c5daf0" /></svg><span>1</span></figcaption>
    {guide && <p className="factor-net-guide">{guide}</p>}
    <div className="factor-net" ref={root}>
      <svg className="factor-net-edges" width={graph.width} height={graph.height} aria-hidden="true">{graph.links.map(link => link.curve ? <path key={link.key} d={curvePath(link.x1, link.y1, link.x2, link.y2)} fill="none" stroke={link.stroke} strokeWidth={link.width} strokeLinecap="round" /> : <line key={link.key} x1={link.x1} y1={link.y1} x2={link.x2} y2={link.y2} stroke={link.stroke} strokeWidth={link.width} strokeLinecap="round" />)}</svg>
      {graph.links.map(link => link.label ? <span className="net-edge-score" key={link.key} style={{ left: (link.x1 + link.x2) / 2, top: (link.y1 + link.y2) / 2 }}>{link.label}</span> : null)}
      <div className="factor-net-col"><h3>Knowledge gap</h3><article className="net-gap" data-net-id="gap" title={gapLabel}><span>{gapLabel}</span></article></div>
      <div className="factor-net-col factor-net-mechanisms"><h3>DisMech mechanism</h3>{contextNode ? <article className="net-context-node" data-net-id="mech:mechanism_subquery"><p className="net-context-lead">{contextNode.text}</p>{!!contextNode.mechanisms.length && <ul className="net-context-bubbles" aria-label="DisMech mechanisms">{contextNode.mechanisms.map(item => <li key={item.id}>{item.label}</li>)}</ul>}</article> : mechanisms.map(item => <article className="net-mechanism" data-net-id={"mech:" + item.id} key={item.id}>{item.label}</article>)}</div>
      <div className="factor-net-col factor-net-factors"><h3><span className="factor-net-brand">CFDE REVEAL KG</span> mechanism factors</h3>{rows.map(row => <article className={"net-factor" + (row.selected ? " selected" : "")} data-net-id={"factor:" + row.sourceId} key={row.sourceId}>
        <div className="net-factor-row"><input type="checkbox" checked={row.selected} disabled={row.selectDisabled} aria-label={"Select " + row.title} onChange={row.onToggle} /><div className="net-factor-copy"><strong>{row.title}</strong>{row.subtitle && <small>{row.subtitle}</small>}{row.onInspect && <button type="button" className="gap-bubble inspect" onClick={row.onInspect}>Inspect factor</button>}</div></div>
      </article>)}</div>
    </div>
  </figure>;
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function evidenceHref(reference: string): string | null {
  if (/^PMID:\d+$/i.test(reference)) return `https://pubmed.ncbi.nlm.nih.gov/${reference.split(":")[1]}/`;
  if (/^PMC(?:ID)?:PMC\d+$/i.test(reference)) return `https://pmc.ncbi.nlm.nih.gov/articles/${reference.split(":")[1]}/`;
  if (/^DOI:10\.\d{4,9}\/.+/i.test(reference)) return `https://doi.org/${reference.slice(4)}`;
  try {
    const url = new URL(reference);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch { return null; }
}
function textField(item: Record<string, unknown>, key: string) {
  const value = item[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
}
function recordList(value: unknown) {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}
function idList(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
}
function directionPaint(direction: string) {
  if (direction === "SUPPORTS") return { stroke: "#c5daf0", width: 4 };
  if (direction === "DISPUTES") return { stroke: "#f3ccc8", width: 4 };
  return { stroke: "#b7b9be", width: 1 };
}
type AccountNode = { id: string; label: string; title: string; source: string };
function graphSource(source: string) {
  const value = source.toLowerCase();
  if (value.includes("biomarker")) return "BiomarkerKG";
  if (value.includes("prokn")) return "ProKN";
  if (value.includes("cfde") || value.includes("eaggl") || value.includes("pigean")) return "CFDE";
  return source;
}
type AccountEdge = { from: string; to: string; tone: "support" | "dispute" | "plain"; curve: boolean; key: string };
function accountGraph(data: unknown) {
  if (!isRecord(data) || !isRecord(data.document)) return null;
  const document = data.document;
  const account = recordList(document.scientific_accounts)[0];
  if (!account) return null;
  const byId = (items: Record<string, unknown>[]) => new Map(items.flatMap(item => { const id = textField(item, "id"); return id ? [[id, item] as const] : []; }));
  const gaps = byId(recordList(document.knowledge_gaps));
  const claims = byId(recordList(document.claims));
  const evidence = byId(recordList(document.evidence_items));
  const files = byId([...recordList(document.files), ...recordList(document.c2m2_files)]);
  const gapId = textField(account, "question");
  const gapRecord = gaps.get(gapId);
  const gap = gapId ? { id: "gap", label: textField(gapRecord || {}, "text") || textField(gapRecord || {}, "name") || "Knowledge gap", title: textField(gapRecord || {}, "text") || gapId, source: "" } : null;
  const claimNodes: AccountNode[] = [];
  const evidenceNodes: AccountNode[] = [];
  const graphNodes: AccountNode[] = [];
  const fileNodes: AccountNode[] = [];
  const edges: AccountEdge[] = [];
  const seenEvidence = new Set<string>();
  const seenGraphs = new Set<string>();
  const seenFiles = new Set<string>();
  for (const claimId of idList(account.component_claims)) {
    const claim = claims.get(claimId);
    if (!claim) continue;
    claimNodes.push({ id: "claim:" + claimId, label: textField(claim, "statement") || textField(claim, "name") || "Claim", title: textField(claim, "statement") || claimId, source: "" });
    if (gap) edges.push({ from: "gap", to: "claim:" + claimId, tone: "plain", curve: true, key: "gap-" + claimId });
    for (const evidenceId of idList(claim.has_evidence)) {
      const item = evidence.get(evidenceId);
      if (!item) continue;
      if (!seenEvidence.has(evidenceId)) {
        seenEvidence.add(evidenceId);
        const source = textField(item, "evidence_source");
        const label = textField(item, "snippet") || source || textField(item, "name") || "Evidence";
        evidenceNodes.push({ id: "evidence:" + evidenceId, label, title: textField(item, "explanation") || source || textField(item, "snippet") || evidenceId, source: "" });
        if (source) {
          const name = graphSource(source);
          const graphId = "graph:" + name;
          if (!seenGraphs.has(graphId)) {
            seenGraphs.add(graphId);
            graphNodes.push({ id: graphId, label: name, title: source, source: "" });
          }
          edges.push({ from: "evidence:" + evidenceId, to: graphId, tone: "plain", curve: true, key: evidenceId + "-" + graphId });
        }
      }
      const direction = textField(item, "direction") || textField(claim, "direction");
      edges.push({ from: "claim:" + claimId, to: "evidence:" + evidenceId, tone: direction === "SUPPORTS" ? "support" : direction === "DISPUTES" ? "dispute" : "plain", curve: true, key: claimId + "-" + evidenceId });
      for (const fileId of idList(item.was_derived_from)) {
        const file = files.get(fileId);
        if (!seenFiles.has(fileId)) {
          seenFiles.add(fileId);
          fileNodes.push({ id: "file:" + fileId, label: file ? textField(file, "filename") || textField(file, "name") || "Source" : "Source", title: file ? textField(file, "description") || textField(file, "filename") || fileId : fileId, source: "" });
        }
        edges.push({ from: "evidence:" + evidenceId, to: "file:" + fileId, tone: "plain", curve: true, key: evidenceId + "-" + fileId });
      }
    }
  }
  if (!gap && !claimNodes.length) return null;
  return { gap, claims: claimNodes, evidence: evidenceNodes, files: [...graphNodes, ...fileNodes], edges };
}
function AccountNodeView({ node }: { node: AccountNode }) {
  const [popup, setPopup] = useState<{ top: number; left: number } | null>(null);
  const text = node.title && node.title.length >= node.label.length ? node.title : node.label;
  function enter(event: { currentTarget: HTMLElement }) {
    const box = event.currentTarget.getBoundingClientRect();
    const width = Math.min(352, window.innerWidth - 24);
    setPopup({ top: Math.min(box.bottom + 8, window.innerHeight - 16), left: Math.max(12, Math.min(box.left, window.innerWidth - width - 12)) });
  }
  return <article className="account-node" data-net-id={node.id} onMouseEnter={enter} onMouseLeave={() => setPopup(null)}><span>{node.label}</span>{node.source && <small>{node.source}</small>}{popup && <div className="account-node-pop" style={{ top: popup.top, left: popup.left }} role="tooltip">{text}</div>}</article>;
}
function AccountGraph({ data }: { data: unknown }) {
  const model = useMemo(() => accountGraph(data), [data]);
  const root = useRef<HTMLDivElement>(null);
  const [drawn, setDrawn] = useState<{ width: number; height: number; links: { x1: number; y1: number; x2: number; y2: number; stroke: string; width: number; key: string; curve: boolean }[] }>({ width: 0, height: 0, links: [] });
  const layoutKey = model ? [model.gap?.id || "", ...model.claims.map(item => item.id), ...model.evidence.map(item => item.id), ...model.files.map(item => item.id), ...model.edges.map(item => item.key)].join("|") : "";
  useLayoutEffect(() => {
    const rootEl = root.current;
    if (!rootEl || !model) return;
    const draw = () => {
      const origin = rootEl.getBoundingClientRect();
      const point = (id: string, side: "left" | "right") => {
        const el = rootEl.querySelector(`[data-net-id="${CSS.escape(id)}"]`);
        if (!(el instanceof HTMLElement)) return null;
        const box = el.getBoundingClientRect();
        return { x: (side === "left" ? box.left : box.right) - origin.left, y: box.top - origin.top + box.height / 2 };
      };
      const links = model.edges.flatMap(edge => {
        const from = point(edge.from, "right");
        const to = point(edge.to, "left");
        if (!from || !to) return [];
        const paint = edge.tone === "plain" ? { stroke: "#b7b9be", width: 1 } : directionPaint(edge.tone === "support" ? "SUPPORTS" : "DISPUTES");
        return [{ x1: from.x, y1: from.y, x2: to.x, y2: to.y, ...paint, key: edge.key, curve: edge.curve }];
      });
      setDrawn({ width: origin.width, height: origin.height, links });
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(rootEl);
    return () => observer.disconnect();
  }, [layoutKey, model]);
  if (!model) return <p className="settings-note">This record has no scientific account graph.</p>;
  const column = (title: string, nodes: AccountNode[], className = "") => <div className={"account-net-col " + className}><h3>{title}</h3>{nodes.map(node => <AccountNodeView node={node} key={node.id} />)}</div>;
  return <figure className="account-net-figure">
    <figcaption className="account-net-legend"><span className="account-swatch support" />Supports<span className="account-swatch dispute" />Disputes<span className="account-swatch plain" />Other</figcaption>
    <div className="account-net-scroll"><div className="account-net" ref={root}>
      <svg className="factor-net-edges" width={drawn.width} height={drawn.height} aria-hidden="true">{drawn.links.map(link => link.curve ? <path key={link.key} d={curvePath(link.x1, link.y1, link.x2, link.y2)} fill="none" stroke={link.stroke} strokeWidth={link.width} strokeLinecap="round" /> : <line key={link.key} x1={link.x1} y1={link.y1} x2={link.x2} y2={link.y2} stroke={link.stroke} strokeWidth={link.width} strokeLinecap="round" />)}</svg>
      {model.gap ? column("Knowledge gap", [model.gap]) : <div className="account-net-col" />}
      {column("Claim", model.claims, "account-net-pad-right")}
      {column("Evidence", model.evidence, "account-net-pad")}
      {column("Source", model.files, "account-net-pad-left")}
    </div></div>
  </figure>;
}
function ResultInspectBody({ result }: { result: ResultInspect }) {
  return <>
    <div className="result-links">{result.links.map(link => <a key={link.href + link.label} href={link.href} target="_blank" rel="noreferrer">{link.label}</a>)}</div>
    <JsonValue value={result.data} />
  </>;
}
function gapSnippets(gap: Gap) {
  const raw = isRecord(gap.source_detail?.raw) ? gap.source_detail.raw : {};
  const evidence = Array.isArray(raw.evidence) ? raw.evidence : [];
  return evidence.flatMap(item => {
    if (!isRecord(item) || typeof item.snippet !== "string" || !item.snippet.trim()) return [];
    const reference = textField(item, "reference");
    return [{ text: item.snippet.trim(), explanation: textField(item, "explanation"), title: textField(item, "reference_title"), source: textField(item, "evidence_source"), href: reference ? evidenceHref(reference) : null, support: textField(item, "supports") }];
  });
}
function attachedMechanisms(gap: Gap) {
  return gap.attachments.filter(item => item.resolution === "resolved" && item.target?.source === "dismech" && (item.label || item.target.source_id));
}
function GapFacts({ gap }: { gap: Gap }) {
  const snippets = gapSnippets(gap);
  const mechanisms = attachedMechanisms(gap);
  return <dl className="inspect-fields">
    {gap.object.gap_description && <div><dt>Rationale</dt><dd>{gap.object.gap_description}</dd></div>}
    {!!mechanisms.length && <div><dt>DisMech mechanisms</dt><dd><ul className="mechanism-list">{mechanisms.map(item => <li key={item.target?.source_id || item.label}>{item.label || item.target?.source_id}</li>)}</ul></dd></div>}
    {!!snippets.length && <div><dt>Snippets</dt><dd><div className="snippet-list">{snippets.map((item, index) => <article className="snippet-card" key={index}><p className="snippet-text">“<span>{item.text}</span>”</p>{item.explanation && <p className="snippet-note">{item.explanation}</p>}{(item.title || item.source) && <div className="snippet-cite">{item.title && <p className="snippet-title">{item.href ? <a href={item.href} target="_blank" rel="noreferrer">{item.title}</a> : item.title}</p>}{item.source && <p className="snippet-source">{item.source}</p>}</div>}{item.support && <p><span>Supports</span>{item.support}</p>}</article>)}</div></dd></div>}
  </dl>;
}
function diseaseBubble(gap: Gap) {
  const disease = gap.source.disease_label || "";
  const scope = gap.object.scope && gap.object.scope !== disease ? gap.object.scope : "";
  const diseaseScope = [disease, scope].filter(Boolean).join(" / ");
  if (!diseaseScope) return null;
  const entity = (gap.object.about_entities || []).find(value => /^https?:\/\//i.test(value));
  return entity ? <a className="disease-bubble" href={entity} target="_blank" rel="noreferrer">{diseaseScope}</a> : <span className="disease-bubble">{diseaseScope}</span>;
}
const LOADING_PAGE_SIZE = 10;
function formatLoading(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  return abs !== 0 && abs < 0.0001 ? value.toExponential(2) : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
}
function LoadingDot({ value, tone }: { value: number | null | undefined; tone: "gene" | "joint" | "marginal" }) {
  const size = dotSize(value);
  if (!size) return null;
  return <span className={"loading-dot " + tone} style={{ width: size, height: size }} aria-hidden="true" />;
}
function FactorSummary({ factor }: { factor: Factor }) {
  return factor.cfde_anchor.subtitle ? <p>{factor.cfde_anchor.subtitle}</p> : null;
}
function wrapAtUnderscore(value: string) {
  return value.split("_").map((part, index) => index === 0 ? part : <span key={index}>_<wbr />{part}</span>);
}
function loadingCut(raw: string) {
  const value = Number(raw);
  return raw.trim() && Number.isFinite(value) ? value : null;
}
function scoreValue(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? -Infinity : value;
}
function symbolKeys(value: string) {
  const text = value.trim().toUpperCase();
  if (!text) return [];
  const cut = text.lastIndexOf(":");
  return cut >= 0 ? [text, text.slice(cut + 1)] : [text];
}
function geneKeys(item: FactorLoading) {
  return [...new Set([...symbolKeys(item.label), ...symbolKeys(item.id)])];
}
const CROSSING_LIMIT = 10;
type CrossingSet = { item: FactorLoading; members: Set<string> | null };
function dotSize(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value) || value <= 0) return 0;
  return 3 + Math.min(1, value) * 8;
}
function ScoreDot({ value, tone }: { value: number | null | undefined; tone: "gene" | "joint" | "marginal" }) {
  const size = dotSize(value);
  if (!size) return null;
  return <span className={"crossing-dot " + tone} style={{ width: size, height: size }} />;
}
function CrossingMap({ genes, sets }: { genes: FactorLoading[]; sets: CrossingSet[] }) {
  return <div className="crossing">
    <h3>Top 10 gene and gene set crossings</h3>
    <ul className="crossing-legend"><li><span className="crossing-dot gene" />Gene loading</li><li><span className="crossing-dot joint" />Joint</li><li><span className="crossing-dot marginal" />Marginal</li><li className="crossing-scale"><span>Scores:</span> 0 <span className="crossing-scale-dots" aria-hidden="true"><span style={{ width: dotSize(0.2), height: dotSize(0.2) }} /><span style={{ width: dotSize(0.55), height: dotSize(0.55) }} /><span style={{ width: dotSize(1), height: dotSize(1) }} /></span> 1</li></ul>
    <div className="crossing-scroll">
      <table className="crossing-map" aria-label="Crossings of the top genes and gene sets">
        <thead><tr><th className="set" />{genes.map(gene => <th className="gene" key={gene.id} scope="col"><span>{gene.label}</span></th>)}</tr></thead>
        <tbody>{sets.map(set => {
          const joint = set.item.joint_loading ?? set.item.loading;
          return <tr key={set.item.id}>
            <th className="set" scope="row">{wrapAtUnderscore(set.item.label)}</th>
            {set.members == null
              ? <td className="crossing-missing" colSpan={genes.length}>Membership unavailable</td>
              : genes.map(gene => {
                const member = geneKeys(gene).some(key => set.members?.has(key));
                const detail = member
                  ? `${gene.label} belongs to this gene set. Gene loading ${formatLoading(gene.loading)}, joint ${formatLoading(joint)}, marginal ${formatLoading(set.item.marginal_loading)}.`
                  : `${gene.label} is not in this gene set.`;
                return <td key={gene.id} className={member ? "member" : undefined} title={detail}>{member && <span className="crossing-dots">
                  <span className="crossing-slot"><ScoreDot value={gene.loading} tone="gene" /></span>
                  <span className="crossing-slot"><ScoreDot value={joint} tone="joint" /></span>
                  <span className="crossing-slot"><ScoreDot value={set.item.marginal_loading} tone="marginal" /></span>
                </span>}</td>;
              })}
          </tr>;
        })}</tbody>
      </table>
    </div>
  </div>;
}
function LoadingTable({ rows, kind, empty }: { rows: FactorLoading[]; kind: "gene" | "gene_set"; empty: string }) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(rows.length / LOADING_PAGE_SIZE));
  const visible = rows.slice(page * LOADING_PAGE_SIZE, page * LOADING_PAGE_SIZE + LOADING_PAGE_SIZE);
  const title = kind === "gene" ? "Genes" : "Gene sets";
  return <section className="loading-block">
    {!rows.length && <p className="settings-note">{empty}</p>}
    {!!visible.length && <table className="loading-table">
      <thead>{kind === "gene" ? <tr><th>Gene</th><th className="num">Loading</th></tr> : <tr><th>Gene set</th><th className="source">Source</th><th className="num">Joint</th><th className="num">Marginal</th></tr>}</thead>
      <tbody>{visible.map(item => kind === "gene"
        ? <tr key={item.id}><td>{item.label}</td><td className="num"><LoadingDot value={item.loading} tone="gene" />{formatLoading(item.loading)}</td></tr>
        : <tr key={item.id}><td className="set-id">{wrapAtUnderscore(item.label)}</td><td className="source">{item.library || "—"}</td><td className="num"><LoadingDot value={item.joint_loading ?? item.loading} tone="joint" />{formatLoading(item.joint_loading ?? item.loading)}</td><td className="num"><LoadingDot value={item.marginal_loading} tone="marginal" />{formatLoading(item.marginal_loading)}</td></tr>)}</tbody>
    </table>}
    {rows.length > LOADING_PAGE_SIZE && <nav className="inspect-pages" aria-label={title + " pages"}><button type="button" className="step" aria-label="First page" disabled={page === 0} onClick={() => setPage(0)}>«</button><button type="button" className="step" aria-label="Previous page" disabled={page === 0} onClick={() => setPage(value => value - 1)}>‹</button>{page > 0 && <button type="button" className="page-num" aria-label={"Page " + page} onClick={() => setPage(page - 1)}>{page}</button>}<button type="button" className="current" aria-current="page">{page + 1}</button>{page < pages - 1 && <button type="button" className="page-num" aria-label={"Page " + (page + 2)} onClick={() => setPage(page + 1)}>{page + 2}</button>}<button type="button" className="step" aria-label="Next page" disabled={page >= pages - 1} onClick={() => setPage(value => value + 1)}>›</button><button type="button" className="step" aria-label="Last page" disabled={page >= pages - 1} onClick={() => setPage(pages - 1)}>»</button></nav>}
  </section>;
}
function FactorLoadings({ sourceId }: { sourceId: string }) {
  const [tab, setTab] = useState<"gene" | "gene_set">("gene");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [geneQuery, setGeneQuery] = useState("");
  const [geneMin, setGeneMin] = useState("");
  const [jointMin, setJointMin] = useState("");
  const [marginalMin, setMarginalMin] = useState("");
  const [rows, setRows] = useState<{ gene: FactorLoading[] | null; gene_set: FactorLoading[] | null }>({ gene: null, gene_set: null });
  const [note, setNote] = useState("");
  const [crossing, setCrossing] = useState<{ genes: FactorLoading[]; sets: CrossingSet[] } | null>(null);
  const [crossingNote, setCrossingNote] = useState("");
  const [crossingBusy, setCrossingBusy] = useState(false);
  const crossingAbort = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    crossingAbort.current?.abort();
    setRows({ gene: null, gene_set: null }); setNote(""); setCrossing(null); setCrossingNote(""); setCrossingBusy(false);
    void Promise.all((["gene", "gene_set"] as const).map(kind => api.factorLoadingsAll(sourceId, kind, controller.signal))).then(([gene, geneSet]) => {
      setRows({ gene, gene_set: geneSet });
    }).catch(error => {
      if (controller.signal.aborted) return;
      setRows({ gene: [], gene_set: [] }); setNote(errorMessage(error));
    });
    return () => { controller.abort(); crossingAbort.current?.abort(); };
  }, [sourceId]);
  useEffect(() => {
    if (!rows.gene || !rows.gene_set) return;
    const genes = [...rows.gene].sort((a, b) => scoreValue(b.loading) - scoreValue(a.loading)).slice(0, CROSSING_LIMIT);
    const sets = [...rows.gene_set].sort((a, b) => scoreValue(b.joint_loading ?? b.loading) - scoreValue(a.joint_loading ?? a.loading)).slice(0, CROSSING_LIMIT);
    if (!genes.length || !sets.length) { setCrossing(null); setCrossingNote("This factor does not have genes and gene sets to compare."); return; }
    const controller = new AbortController();
    crossingAbort.current = controller;
    setCrossingBusy(true); setCrossingNote(""); setCrossing(null);
    void Promise.all(sets.map(async item => {
      if (!item.gene_set_id) return { item, members: null };
      try {
        const record = await api.catalogGeneSet(item.gene_set_id, controller.signal);
        const raw = record.object?.members;
        if (!Array.isArray(raw)) return { item, members: null };
        return { item, members: new Set(raw.flatMap(value => typeof value === "string" ? symbolKeys(value) : [])) };
      } catch (error) {
        if (controller.signal.aborted) throw error;
        return { item, members: null };
      }
    })).then(mapped => {
      if (!controller.signal.aborted) setCrossing({ genes, sets: mapped });
    }).catch(error => {
      if (!controller.signal.aborted) setCrossingNote(errorMessage(error));
    }).finally(() => {
      if (!controller.signal.aborted) setCrossingBusy(false);
    });
    return () => controller.abort();
  }, [rows]);
  const filtered = useMemo(() => {
    const query = geneQuery.trim().toLowerCase();
    const geneCut = loadingCut(geneMin);
    const jointCut = loadingCut(jointMin);
    const marginalCut = loadingCut(marginalMin);
    return {
      gene: rows.gene?.filter(item => {
        if (query && !`${item.label} ${item.id}`.toLowerCase().includes(query)) return false;
        return geneCut == null || item.loading >= geneCut;
      }) ?? null,
      gene_set: rows.gene_set?.filter(item => {
        const joint = item.joint_loading ?? item.loading;
        if (jointCut != null && (joint == null || joint < jointCut)) return false;
        return marginalCut == null || (item.marginal_loading != null && item.marginal_loading >= marginalCut);
      }) ?? null,
    };
  }, [rows, geneQuery, geneMin, jointMin, marginalMin]);
  const current = filtered[tab];
  const filtersOn = [geneQuery, geneMin, jointMin, marginalMin].some(value => value.trim());
  const empty = rows[tab]?.length ? "No loadings match these filters." : "No loadings for this factor.";
  return <div className="loading-tabs">
    <div className="loading-tools"><button type="button" className="loading-filter" aria-label="Filters" aria-expanded={filtersOpen} aria-pressed={filtersOn} onClick={() => setFiltersOpen(open => !open)}><svg viewBox="0 0 72 54" aria-hidden="true"><g fill="none" stroke="currentColor" strokeWidth="4" strokeLinecap="round"><line x1="6" y1="10" x2="11.5" y2="10" /><line x1="28.5" y1="10" x2="66" y2="10" /><line x1="6" y1="27" x2="37.5" y2="27" /><line x1="54.5" y1="27" x2="66" y2="27" /><line x1="6" y1="44" x2="17.5" y2="44" /><line x1="34.5" y1="44" x2="66" y2="44" /></g><g fill="#fff" stroke="currentColor" strokeWidth="4"><circle cx="20" cy="10" r="6.5" /><circle cx="46" cy="27" r="6.5" /><circle cx="26" cy="44" r="6.5" /></g></svg></button></div>
    {filtersOpen && <div className="loading-filters">
      <label>Gene<input value={geneQuery} onChange={event => setGeneQuery(event.target.value)} placeholder="Search a gene" autoComplete="off" /></label>
      <div className="loading-filter-scores">
        <label>Gene loading<input type="number" inputMode="decimal" min={0} max={1} step="any" value={geneMin} onChange={event => setGeneMin(event.target.value)} placeholder="Any" /></label>
        <label>Joint<input type="number" inputMode="decimal" min={0} max={1} step="any" value={jointMin} onChange={event => setJointMin(event.target.value)} placeholder="Any" /></label>
        <label>Marginal<input type="number" inputMode="decimal" min={0} max={1} step="any" value={marginalMin} onChange={event => setMarginalMin(event.target.value)} placeholder="Any" /></label>
      </div>
    </div>}
    {crossingBusy && <p className="settings-note">Checking crossings…</p>}
    {crossingNote && <p className="settings-note">{crossingNote}</p>}
    {crossing && <CrossingMap genes={crossing.genes} sets={crossing.sets} />}
    <div role="tablist" aria-label="Factor loadings"><button type="button" role="tab" aria-selected={tab === "gene"} onClick={() => setTab("gene")}>Genes</button><button type="button" role="tab" aria-selected={tab === "gene_set"} onClick={() => setTab("gene_set")}>Gene sets</button></div>
    {note && <p className="settings-note">{note}</p>}
    {current ? <LoadingTable key={`${tab}|${geneQuery}|${geneMin}|${jointMin}|${marginalMin}`} rows={current} kind={tab} empty={empty} /> : !note && <p className="settings-note">Loading…</p>}
  </div>;
}
function jsonLabel(key: string) {
  const words = key.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
function JsonValue({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value == null) return <span className="json-empty">None</span>;
  if (typeof value === "string") return /^https?:\/\//.test(value) ? <a href={value} target="_blank" rel="noreferrer">{value}</a> : <span>{value}</span>;
  if (typeof value === "number" || typeof value === "boolean") return <span>{String(value)}</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="json-empty">None</span>;
    return <ol className="json-list">{value.map((item, index) => <li key={index}><JsonValue value={item} depth={depth + 1} /></li>)}</ol>;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return <span className="json-empty">None</span>;
    return <dl className="json-view">{entries.map(([key, item]) => {
      const nested = item != null && typeof item === "object";
      const count = Array.isArray(item) ? item.length : nested ? Object.keys(item as object).length : 0;
      return <div className="json-row" key={key}><dt>{jsonLabel(key)}{Array.isArray(item) ? ` (${count})` : ""}</dt><dd>{nested ? <details className="json-fold" open={depth === 0}><summary>{Array.isArray(item) ? `${count} item${count === 1 ? "" : "s"}` : `${count} field${count === 1 ? "" : "s"}`}</summary><JsonValue value={item} depth={depth + 1} /></details> : <JsonValue value={item} depth={depth + 1} />}</dd></div>;
    })}</dl>;
  }
  return <span>{String(value)}</span>;
}
function InspectCard({ title, open, summary = false, closable = true, closeLabel, badge, onOpen, onClose, children }: { title: string; open: boolean; summary?: boolean; closable?: boolean; closeLabel: string; badge?: ReactNode; onOpen: () => void; onClose: () => void; children: ReactNode }) {
  return <section className={"inspect-card" + (open ? " open" : summary ? " summary" : "")}><div className="inspect-card-head"><button type="button" className="inspect-toggle" aria-expanded={open || summary} onClick={onOpen}><span>{title}</span></button>{closable && <button type="button" className="inspect-close" aria-label={closeLabel} onClick={onClose}>×</button>}{open && badge && <div className="inspect-badge-row">{badge}</div>}</div>{(open || summary) && <div className="inspect-body">{children}</div>}</section>;
}
function activityNote(message: string, eventType: JobEvent["event_type"] = "progress"): JobEvent {
  return { id: crypto.randomUUID(), job_id: "cfde-assessment", occurred_at: new Date().toISOString(), event_type: eventType, status: "running", stage: "retrieving_cfde", message, result: null, detail: null };
}
function isMessageDelta(event: JobEvent) {
  return event.detail?.kind === "agent_message" && event.detail.message_delta === true;
}
function displayActivity(events: JobEvent[]) {
  const shown: JobEvent[] = [];
  for (const event of events) {
    const previous = shown[shown.length - 1];
    if (previous && isMessageDelta(previous) && isMessageDelta(event)) {
      shown[shown.length - 1] = { ...previous, message: previous.message + event.message, occurred_at: event.occurred_at };
      continue;
    }
    shown.push(event);
  }
  return shown;
}
function FeasibilityReport({ result }: { result: LightningAuditResult }) {
  const summary = result.summary.split(/\n\n+/).map(paragraph => paragraph.trim()).filter(Boolean);
  return <section className="feasibility-report"><h3>Feasibility assessment report</h3><h4>Test summary</h4>{summary.map(paragraph => <p key={paragraph}>{paragraph}</p>)}{!!result.observations.length && <><h4>Observations</h4><ul>{result.observations.map(item => <li key={item.text}>{item.text}</li>)}</ul></>}{!!result.limitations.length && <><h4>Limitations</h4><ul>{result.limitations.map(item => <li key={item}>{item}</li>)}</ul></>}{!!result.missing_evidence.length && <><h4>Missing evidence</h4><ul>{result.missing_evidence.map(item => <li key={item}>{item}</li>)}</ul></>}{result.recommended_direction && <><h4>Recommended direction</h4><p>{result.recommended_direction}</p></>}{!!result.next_steps.length && <><h4>Next steps</h4><ul>{result.next_steps.map(item => <li key={item}>{item}</li>)}</ul></>}</section>;
}
function ActivityRecord({ events, logRef }: { events: JobEvent[]; logRef: RefObject<HTMLDivElement | null> }) {
  const shown = displayActivity(events);
  return <div className="event-log" ref={logRef} aria-label="Job activity events">{!shown.length && <p className="empty">Loading saved activity…</p>}{shown.map(event => <article className={"event " + event.event_type} key={event.id}><div className="event-header"><span>{event.detail?.tool_name || readable(event.event_type)}</span><time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleTimeString()}</time></div><p>{event.message}</p>{event.detail?.output_excerpt && <details><summary>Captured output</summary><pre>{event.detail.output_excerpt}</pre></details>}{event.detail?.artifact_sha256 && <a href={backend("artifacts/" + event.detail.artifact_sha256)} target="_blank" rel="noreferrer">Open captured artifact</a>}</article>)}</div>;
}
function InspectColumn({ gap, factor, factorPending, result, activity, cfdePassed, focus, gapNote, factorNote, onFocus, onCloseGap, onCloseFactor, onCloseResult, onCloseActivity }: {
  gap: Gap | null; factor: Factor | null; factorPending: boolean; result: ResultInspect | null; activity: ReactNode; cfdePassed: boolean; focus: "gap" | "factor" | "result" | "activity"; gapNote: string; factorNote: string;
  onFocus: (focus: "gap" | "factor" | "result" | "activity") => void; onCloseGap: () => void; onCloseFactor: () => void; onCloseResult: () => void; onCloseActivity: () => void;
}) {
  return <div className="inspect-frame">
    {gap && <InspectCard title={gap.object.text || gapTitle(gap)} open={focus === "gap"} closeLabel="Close gap inspection" badge={diseaseBubble(gap)} onOpen={() => onFocus("gap")} onClose={onCloseGap}><GapFacts gap={gap} />{gapNote && <p className="settings-note">{gapNote}</p>}</InspectCard>}
    {(factor || factorPending) && <InspectCard title={factor ? factorTitle(factor) : "Loading factor…"} open={focus === "factor"} closeLabel="Close factor inspection" onOpen={() => onFocus("factor")} onClose={onCloseFactor}>{factor ? <><FactorSummary factor={factor} /><FactorLoadings key={factor.source_id} sourceId={factor.source_id} /></> : <p className="settings-note">Loading the factor…</p>}{factorNote && <p className="settings-note">{factorNote}</p>}</InspectCard>}
    {result && <InspectCard title={result.title} open={focus === "result"} closeLabel="Close result inspection" onOpen={() => onFocus("result")} onClose={onCloseResult}><ResultInspectBody result={result} /></InspectCard>}
    {activity && <InspectCard title="Activity" open={focus === "activity"} closeLabel="Close activity" onOpen={() => onFocus("activity")} onClose={onCloseActivity}>{cfdePassed && <p className="cfde-passed">CFDE evidence assessment passed.</p>}{activity}</InspectCard>}
  </div>;
}
function setLocation(kind: "draft" | "job", id: string | null) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set(kind, id); else url.searchParams.delete(kind);
  window.history.replaceState(null, "", url);
}
const DRAFT_PAGE_SIZE = 10;
const API_VERSION = "0.2.0-draft";
const SETTINGS_KEY = "reveal-client-settings";
const LAST_DRAFT_KEY = "reveal-client-last-draft";
type SettingsTab = "settings" | "apis" | "information";
function readOpenLastDraft() {
  try { return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null")?.openLastDraft === true; }
  catch { return false; }
}
const GAP_SEARCH_KEY = "reveal-client-gap-search";
type GapSearchMap = { drafts: Record<string, string>; gaps: Record<string, string> };
function readGapSearches(): GapSearchMap {
  try {
    const value = JSON.parse(localStorage.getItem(GAP_SEARCH_KEY) || "null");
    return {
      drafts: value && typeof value.drafts === "object" && value.drafts ? value.drafts : {},
      gaps: value && typeof value.gaps === "object" && value.gaps ? value.gaps : {},
    };
  } catch { return { drafts: {}, gaps: {} }; }
}
function rememberGapSearch(query: string, draftId?: string | null, gapSourceId?: string | null) {
  const map = readGapSearches();
  if (draftId) map.drafts[draftId] = query;
  if (gapSourceId) map.gaps[gapSourceId] = query;
  if (!draftId && !gapSourceId) return;
  localStorage.setItem(GAP_SEARCH_KEY, JSON.stringify(map));
}
function recallGapSearch(draftId: string, gapSourceId?: string | null) {
  const map = readGapSearches();
  if (Object.prototype.hasOwnProperty.call(map.drafts, draftId)) return map.drafts[draftId];
  if (gapSourceId && Object.prototype.hasOwnProperty.call(map.gaps, gapSourceId)) return map.gaps[gapSourceId];
  return null;
}
function withSelectedGap(values: Gap[], selected: Gap | null) {
  if (!selected || values.some(item => item.source.source_id === selected.source.source_id)) return values;
  return [selected, ...values];
}
const CFDE_PASSED_KEY = "reveal-client-cfde-passed";
function readCfdePassed() {
  try {
    const value = JSON.parse(localStorage.getItem(CFDE_PASSED_KEY) || "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  } catch { return []; }
}
function rememberCfdePassed(draftId: string) {
  localStorage.setItem(CFDE_PASSED_KEY, JSON.stringify([...new Set([...readCfdePassed(), draftId])]));
}
function forgetCfdePassed(draftId: string) {
  localStorage.setItem(CFDE_PASSED_KEY, JSON.stringify(readCfdePassed().filter(id => id !== draftId)));
}
const productApis: { method: string; path: string; detail: string }[] = [
  { method: "GET", path: "/api/session", detail: "Check whether this browser already has a workspace session." },
  { method: "POST", path: "/api/session", detail: "Connect to the workspace." },
  { method: "DELETE", path: "/api/session", detail: "Disconnect the workspace. Guests lose access to saved work; running jobs continue." },
  { method: "GET", path: "/v1/drafts", detail: "List saved drafts." },
  { method: "GET", path: "/v1/drafts/{id}", detail: "Open one saved draft." },
  { method: "POST", path: "/v1/drafts", detail: "Save a new draft." },
  { method: "PATCH", path: "/v1/drafts/{id}", detail: "Save updates to the open draft." },
  { method: "DELETE", path: "/v1/drafts/{id}", detail: "Delete a draft. A submitted investigation from that draft stays available." },
  { method: "GET", path: "/v1/knowledge-gaps", detail: "Browse knowledge gaps, ranked by how many investigations use them." },
  { method: "GET", path: "/v1/knowledge-gaps/search", detail: "Search knowledge gaps by a disease or research question." },
  { method: "GET", path: "/v1/knowledge-gaps/{id}", detail: "Load the knowledge gap selected for an investigation." },
  { method: "POST", path: "/v1/mechanisms/suggest", detail: "Suggest mechanism anchors for the selected gap." },
  { method: "GET", path: "/v1/mechanisms/{id}", detail: "Load a mechanism factor after it is selected." },
  { method: "GET", path: "/v1/factor-loadings", detail: "Page the gene and gene-set loadings for a factor." },
  { method: "GET", path: "/v1/catalog/gene-sets/{gene_set_id}", detail: "Load the genes that belong to a gene set." },
  { method: "GET", path: "/v1/jobs", detail: "List workspace jobs." },
  { method: "GET", path: "/v1/jobs/{id}", detail: "Open one job and its saved result." },
  { method: "POST", path: "/v1/jobs", detail: "Start an analysis from a saved draft." },
  { method: "POST", path: "/v1/jobs/{id}/cancel", detail: "Stop a running job." },
  { method: "POST", path: "/v1/jobs/{id}/retry-review", detail: "Retry a saved review that can be run again." },
  { method: "GET", path: "/v1/jobs/{id}/events", detail: "Follow a job’s live activity." },
  { method: "GET", path: "/v1/jobs/{id}/evidence-package", detail: "Open the frozen evidence package for a finished job." },
  { method: "GET", path: "/v1/artifacts/{sha256}", detail: "Open a captured artifact from a job event." },
  { method: "GET", path: "/v1/research-requests", detail: "Match saved drafts to the investigations they started." },
  { method: "GET", path: "/v1/me/workspace/events", detail: "Follow workspace changes while the session is connected." },
];
function SettingsPanel({ tab, openLastDraft, onTab, onOpenLastDraft, onClose }: {
  tab: SettingsTab; openLastDraft: boolean; onTab: (tab: SettingsTab) => void; onOpenLastDraft: (value: boolean) => void; onClose: () => void;
}) {
  return <div className="warning-stage" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-heading">
      <button type="button" className="panel-back" aria-label="Close" onClick={onClose}>×</button>
      <h2 id="settings-heading" className="sr-only">Settings</h2>
      <div className="settings-tabs" role="tablist" aria-label="Settings sections">
        {([["settings", "Settings"], ["apis", "APIs"], ["information", "Information"]] as const).map(([id, label]) => <button type="button" key={id} role="tab" id={"settings-tab-" + id} aria-selected={tab === id} aria-controls={"settings-panel-" + id} onClick={() => onTab(id)}>{label}</button>)}
      </div>
      {tab === "settings" && <div className="settings-body" role="tabpanel" id="settings-panel-settings" aria-labelledby="settings-tab-settings">
        <label className="settings-option"><input type="checkbox" checked={openLastDraft} onChange={event => onOpenLastDraft(event.target.checked)} /><span><strong>Open last draft when connecting to the workspace</strong><span>Connecting opens the draft you last worked on. Leave this off to start from the welcome panel.</span></span></label>
      </div>}
      {tab === "apis" && <div className="settings-body" role="tabpanel" id="settings-panel-apis" aria-labelledby="settings-tab-apis">
        <ul className="settings-api-list">{productApis.map(item => <li key={item.method + item.path}><code>{item.method} {item.path}</code><p>{item.detail}</p></li>)}</ul>
      </div>}
      {tab === "information" && <div className="settings-body" role="tabpanel" id="settings-panel-information" aria-labelledby="settings-tab-information">
        <dl className="settings-facts"><dt>Product</dt><dd>REVEAL Close the gap</dd><dt>Client version</dt><dd>{clientVersion}</dd><dt>API</dt><dd>REVEAL Mechanisms API</dd><dt>API version</dt><dd>{API_VERSION}</dd><dt>Description</dt><dd>Choose the gap. Ground the claim.</dd></dl>
        <p className="settings-note">The client reaches the API through the workspace gateway. Requests are signed on the server.</p>
      </div>}
    </section>
  </div>;
}
const evidenceName = (id: "biomarkerkg" | "prokn") => id === "biomarkerkg" ? "BiomarkerKG" : "ProKN";
function SavedDrafts({ drafts, jobs, requests, gapLabels, factorLabels, ready, requestsReady, page, deletingId, onPage, onOpen, onDelete, onClose }: {
  drafts: Draft[]; jobs: Job[]; requests: Schema<"ResearchRequest">[]; gapLabels: Record<string, string>; factorLabels: Record<string, string>;
  ready: boolean; requestsReady: boolean; page: number; deletingId: string; onPage: (page: number) => void; onOpen: (id: string) => void; onDelete: (draft: Draft) => void; onClose: () => void;
}) {
  const pages = Math.max(1, Math.ceil(drafts.length / DRAFT_PAGE_SIZE));
  const rows = drafts.slice(page * DRAFT_PAGE_SIZE, page * DRAFT_PAGE_SIZE + DRAFT_PAGE_SIZE);
  return <div className="warning-stage" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="draft-picker" role="dialog" aria-modal="true" aria-labelledby="draft-picker-heading">
      <button type="button" className="panel-back" aria-label="Close" onClick={onClose}>×</button>
      <div className="draft-picker-heading"><h2 id="draft-picker-heading">Saved drafts</h2></div>
      <div className="draft-table-wrap"><table className="draft-table">
        <thead><tr><th scope="col">Query</th><th scope="col">Saved time</th><th scope="col">Knowledge gap</th><th scope="col">Factors</th><th scope="col">Evidence</th><th scope="col">Investigation</th><th scope="col">Select</th><th scope="col">Delete</th></tr></thead>
        <tbody>{rows.length === 0 ? <tr><td colSpan={8}>No saved drafts.</td></tr> : rows.map(draft => {
          const titled = splitDraftTitle(draft.name || "");
          const gapId = draft.composer.source_gap?.id;
          const factorIds = draft.composer.eaggl_anchors.map(anchor => anchor.reference.source_id);
          const linked = requests.filter(request => request.source_draft_id === draft.id).sort((a, b) => b.submitted_at.localeCompare(a.submitted_at));
          const investigations = linked.flatMap(request => {
            const matches = jobs.filter(job => job.research_request_id === request.id);
            return matches.length ? matches.map(job => `${job.kind === "analysis" ? "Analysis" : "Cited claim"} — ${readable(job.status)} — ${date(job.created_at)}`) : [`Analysis — ${date(request.submitted_at)}`];
          });
          return <tr key={draft.id}>
            <td>{titled?.query || draft.name || "—"}</td>
            <td>{titled?.saved || "—"}</td>
            <td>{gapId ? gapLabels[gapId] || (ready ? "—" : "Loading…") : "—"}</td>
            <td>{factorIds.length ? factorIds.map(id => factorLabels[id] || (ready ? "—" : "Loading…")).join(", ") : "—"}</td>
            <td>{draft.composer.selected_kgs.map(evidenceName).join(", ") || "—"}</td>
            <td>{!requestsReady ? "Loading…" : investigations.length ? <span className="draft-investigations">{investigations.map((line, index) => <span key={index}>{line}</span>)}</span> : "—"}</td>
            <td><button type="button" onClick={() => onOpen(draft.id)}>Open</button></td>
            <td><button type="button" className="draft-delete" aria-label={`Delete ${draft.name || "Untitled draft"}`} disabled={!!deletingId} onClick={() => onDelete(draft)}>{deletingId === draft.id ? "Deleting…" : "Delete"}</button></td>
          </tr>;
        })}</tbody>
      </table></div>
      {drafts.length > DRAFT_PAGE_SIZE && <nav className="draft-pages" aria-label="Saved draft pages"><button type="button" className="secondary" disabled={page === 0} onClick={() => onPage(page - 1)}>Previous</button>{Array.from({ length: pages }, (_, index) => <button type="button" className={index === page ? undefined : "secondary"} aria-current={index === page ? "page" : undefined} key={index} onClick={() => onPage(index)}>{index + 1}</button>)}<button type="button" className="secondary" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>Next</button></nav>}
    </section>
  </div>;
}
function storedIntent(user: string): PendingSubmission | null {
  try {
    const value = JSON.parse(sessionStorage.getItem("reveal-submit:" + user) || "null");
    return value?.body?.kind === "analysis" && typeof value.body.draft_id === "string" && Number.isInteger(value.body.draft_version) && typeof value.key === "string" ? value : null;
  } catch { return null; }
}

export default function Home() {
  const [principal, setPrincipal] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [guestNote, setGuestNote] = useState(true);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [moreRecords, setMoreRecords] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [name, setName] = useState("");
  const [composer, setComposer] = useState<Composer>(emptyComposer);
  const [gap, setGap] = useState<Gap | null>(null);
  const [query, setQuery] = useState("");
  const [gaps, setGaps] = useState<Gap[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [gapListMode, setGapListMode] = useState<"search" | "trending" | "selected" | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestion, setSuggestion] = useState<Schema<"Suggestions"> | null>(null);
  const [factors, setFactors] = useState<Record<string, Factor>>({});
  const [factorMisses, setFactorMisses] = useState<Record<string, true>>({});
  const [job, setJob] = useState<Job | null>(null);
  const [paragraphs, setParagraphs] = useState<Record<string, ParagraphProgress>>({});
  const [publication, setPublication] = useState<"public" | "private" | "unknown" | null>(null);
  const [startedDraftId, setStartedDraftId] = useState<string | null>(null);
  const [activity, setActivity] = useState<JobEvent[]>([]);
  const [activityDetail, setActivityDetail] = useState(true);
  const [activityLogOpen, setActivityLogOpen] = useState(false);
  const [streamState, setStreamState] = useState("");
  const [, setWorkspaceState] = useState("");
  const [streamAttempt, setStreamAttempt] = useState(0);
  const [pending, setPending] = useState<PendingSubmission | null>(null);
  const [welcomeOpen, setWelcomeOpen] = useState(true);
  const [panelView, setPanelView] = useState<"menu" | "draft" | "copy">("menu");
  const [step, setStep] = useState<"gap" | "anchors" | "investigation" | null>("gap");
  const [activityOpen, setActivityOpen] = useState(false);
  const [draftPicker, setDraftPicker] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const [pendingDelete, setPendingDelete] = useState<Draft | null>(null);
  const [cfdeGate, setCfdeGate] = useState<Draft | null>(null);
  const [cfdePassed, setCfdePassed] = useState(false);
  const [feasibility, setFeasibility] = useState<FeasibilityRun | null>(null);
  const [fullInvestigation, setFullInvestigation] = useState(false);
  const [investigationTab, setInvestigationTab] = useState<"assessment" | "result">("assessment");
  const [inspectGap, setInspectGap] = useState<Gap | null>(null);
  const [inspectGapNote, setInspectGapNote] = useState("");
  const [inspectFactor, setInspectFactor] = useState<Factor | null>(null);
  const [inspectFactorId, setInspectFactorId] = useState<string | null>(null);
  const [inspectFactorNote, setInspectFactorNote] = useState("");
  const [inspectFocus, setInspectFocus] = useState<"gap" | "factor" | "result" | "activity">("gap");
  const [trackedStep, setTrackedStep] = useState<"gap" | "anchors" | "investigation">("gap");
  const [inspectorMemory, setInspectorMemory] = useState<{ gap: "gap" | null; anchors: "factor" | null; investigation: "result" | "activity" | null }>({ gap: null, anchors: null, investigation: null });
  const [inspectResult, setInspectResult] = useState<ResultInspect | null>(null);
  const [resultActions, setResultActions] = useState<ResultAction[]>([]);
  const factorInspectId = useRef("");
  const [draftPage, setDraftPage] = useState(0);
  const [gapLabels, setGapLabels] = useState<Record<string, string>>({});
  const [factorLabels, setFactorLabels] = useState<Record<string, string>>({});
  const [draftRequests, setDraftRequests] = useState<Schema<"ResearchRequest">[]>([]);
  const [catalogReady, setCatalogReady] = useState(false);
  const [requestsReady, setRequestsReady] = useState(false);
  const [sessionOpen, setSessionOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("settings");
  const [openLastDraft, setOpenLastDraft] = useState(false);
  const [focusNonce, setFocusNonce] = useState(0);
  const editorGeneration = useRef(0), jobGeneration = useRef(0);
  const collections = useRef<{ drafts: boolean; jobs: boolean; flight: Promise<void> | null }>({ drafts: false, jobs: false, flight: null });
  const identity = useRef<string | null>(null);
  const searchAbort = useRef<AbortController | null>(null), suggestionAbort = useRef<AbortController | null>(null);
  const mutationKeys = useRef(createMutationKeys());
  const currentJob = useRef<Job | null>(null);
  const submittedJob = useRef<string | null>(null);
  const activityPinned = useRef<string | null>(null);
  const activityCollapsed = useRef<string | null>(null);
  const activityLogRef = useRef<HTMLDivElement>(null);
  const lightningSeen = useRef<LightningAuditProgress | null>(null);
  const lightningStatus = useRef("");
  const runLightningRef = useRef<(saved: Draft, cfde: "yes" | "no", existing?: LightningAudit) => Promise<void>>(async () => undefined);
  const assessmentActive = useRef(false);
  const draftIdRef = useRef<string | null>(null);
  const submittedDraft = useRef<string | null>(null);
  const completionSaved = useRef(new Set<string>());
  const saveRef = useRef<(asNew?: boolean, options?: { name?: string; quiet?: boolean; updateOnly?: boolean; draftId?: string; composer?: Composer }) => Promise<Draft | null>>(async () => null);
  const startFocus = useRef<string | null>(null);
  const sessionMenu = useRef<HTMLDivElement>(null);
  const helpMenu = useRef<HTMLDivElement>(null);
  const keptName = useRef("");
  const searchedQuery = useRef<string | null>(null);
  const suggestedSubquery = useRef<string | null>(null);
  draftIdRef.current = draft?.id ?? null;
  const dirty = !draft || JSON.stringify(draft.composer) !== JSON.stringify(composer) || (draft.name || "") !== name;
  const selectionForked = !!draft && selectionSignature(composer) !== selectionSignature(draft.composer);
  const mutable = Boolean(principal) && !busy;
  const gapChosen = Boolean(composer.source_gap);
  const anchorChosen = composer.eaggl_anchors.length > 0;
  const suggestedIds = suggestion?.automatic_anchors.map(anchor => anchor.factor.source_id) ?? [];
  const selectedIds = composer.eaggl_anchors.map(anchor => anchor.reference.source_id);
  const anchorIds = suggestedIds.length ? Array.from(new Set([...suggestedIds, ...selectedIds])) : selectedIds;
  const missingFactorKey = composer.eaggl_anchors.map(anchor => anchor.reference.source_id).filter(id => !factors[id] && !factorMisses[id]).join("\n");

  const refresh = useCallback((kinds: string[] = ["drafts", "jobs"]): Promise<void> => {
    if (!identity.current) return Promise.resolve();
    const state = collections.current;
    state.drafts ||= kinds.includes("drafts"); state.jobs ||= kinds.includes("jobs");
    if (state.flight) return state.flight;
    state.flight = (async () => {
      try {
        // Mutations during an in-flight read schedule one further read, never overlap it.
        while (identity.current && (state.drafts || state.jobs)) {
          const owner = identity.current, draftsNeeded = state.drafts, jobsNeeded = state.jobs;
          state.drafts = false; state.jobs = false;
          const [saved, recent] = await Promise.all([draftsNeeded ? api.drafts() : null, jobsNeeded ? api.jobs() : null]);
          if (identity.current !== owner) continue;
          if (saved) setDrafts(saved.items);
          if (recent) setJobs(recent.items);
          if (saved?.page.has_more || recent?.page.has_more) setMoreRecords(true);
        }
      } finally { state.flight = null; }
    })();
    return state.flight;
  }, []);
  const openJob = useCallback(async (id: string) => {
    const generation = ++jobGeneration.current, owner = identity.current;
    try {
      const value = await api.job(id);
      if (generation !== jobGeneration.current || owner !== identity.current) return;
      currentJob.current = value; setJob(value); setActivity([]); setLocation("job", id); setError("");
    } catch (error) { if (generation === jobGeneration.current && owner === identity.current) setError(errorMessage(error)); }
  }, []);
  const openDraft = useCallback(async (id: string) => {
    if (assessmentActive.current && draftIdRef.current === id) return;
    assessmentActive.current = false;
    const generation = ++editorGeneration.current, owner = identity.current;
    suggestionAbort.current?.abort(); setSuggesting(false);
    try {
      const value = await api.draft(id);
      if (generation !== editorGeneration.current || owner !== identity.current) return;
      setDraft(value); setComposer(value.composer); setName(value.name || ""); setSuggestion(null); setGap(null); suggestedSubquery.current = value.composer.mechanism_subquery || ""; setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false; setCfdePassed(readCfdePassed().includes(id));
      setQuery(""); setGaps([]); setSearched(false); setGapListMode(null); searchedQuery.current = null;
      setLocation("draft", id); setNotice("Saved draft loaded."); setError(""); setWelcomeOpen(false);
      const suggestionController = new AbortController();
      suggestionAbort.current = suggestionController;
      const suggestionTask = value.composer.source_gap ? api.suggest(value.composer, suggestionController.signal).then(suggestion => {
        if (suggestionController.signal.aborted || generation !== editorGeneration.current || owner !== identity.current) return;
        setSuggestion(suggestion);
        setFactors(values => ({ ...values, ...Object.fromEntries(suggestion.automatic_anchors.map(anchor => [anchor.factor.source_id, anchor.factor])) }));
      }).catch(error => {
        if (!suggestionController.signal.aborted && generation === editorGeneration.current && owner === identity.current) setError(errorMessage(error));
      }).finally(() => { if (!suggestionController.signal.aborted && generation === editorGeneration.current) setSuggesting(false); }) : Promise.resolve();
      if (value.composer.source_gap) setSuggesting(true);
      let selectedGap: Gap | null = null;
      if (value.composer.source_gap) {
        try {
          selectedGap = await api.gap(value.composer.source_gap.id);
          if (generation === editorGeneration.current) setGap(selectedGap);
        } catch (error) { if (generation === editorGeneration.current && owner === identity.current) setError(errorMessage(error)); }
      }
      if (generation !== editorGeneration.current || owner !== identity.current) return;
      const remembered = recallGapSearch(id, value.composer.source_gap?.source_id);
      if (selectedGap) { setGaps([selectedGap]); setSearched(true); setGapListMode("selected"); }
      if (remembered !== null) {
        searchedQuery.current = remembered;
        setQuery(remembered);
        setGapListMode(remembered.trim() ? "search" : "trending");
        searchAbort.current?.abort();
        const controller = new AbortController();
        searchAbort.current = controller;
        setSearching(true);
        try {
          const found = await api.gaps(remembered, controller.signal);
          if (!controller.signal.aborted && generation === editorGeneration.current) { setGaps(withSelectedGap(found, selectedGap)); setSearched(true); }
        } catch (error) { if (!controller.signal.aborted && generation === editorGeneration.current && owner === identity.current) setError(errorMessage(error)); }
        finally { if (!controller.signal.aborted && generation === editorGeneration.current) setSearching(false); }
      }
      await suggestionTask;
      const [requestPage, jobPage] = await Promise.all([api.requests(), api.jobs()]);
      if (generation !== editorGeneration.current || owner !== identity.current) return;
      const requestIds = new Set(requestPage.items.filter(item => item.source_draft_id === id).map(item => item.id));
      const linked = jobPage.items.filter(item => item.kind === "analysis" && !!item.research_request_id && requestIds.has(item.research_request_id))
        .sort((a, b) => Number(b.status === "succeeded") - Number(a.status === "succeeded") || b.created_at.localeCompare(a.created_at))[0];
      let audit: LightningAudit | undefined;
      try {
        const page = await api.lightningAudits();
        if (generation !== editorGeneration.current || owner !== identity.current) return;
        audit = page.items.filter(item => item.source_draft_id === id).sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
      } catch { /* A draft still opens when the feasibility list is unavailable. */ }
      if (generation !== editorGeneration.current || owner !== identity.current) return;
      const support = readCfdePassed().includes(id) ? "yes" as const : "no" as const;
      if (audit?.status === "succeeded" && audit.result) {
        setFeasibility({ draft: value, cfde: support, audit });
        if (linked) { setFullInvestigation(true); setInvestigationTab("result"); }
      } else if (audit && (audit.status === "preparing" || audit.status === "assessing")) {
        void runLightningRef.current(value, support, audit);
      }
      if (!linked) {
        setStartedDraftId(null);
        if ((audit?.status === "succeeded" && audit.result) || audit?.status === "preparing" || audit?.status === "assessing" || assessmentActive.current) setStep("investigation");
        else setStep(value.composer.source_gap ? "anchors" : "gap");
        return;
      }
      setStartedDraftId(id); submittedDraft.current = id; submittedJob.current = linked.id;
      const finished = terminal(linked.status);
      if (finished) activityCollapsed.current = linked.id;
      setActivityDetail(true); setActivityLogOpen(!finished); if (!finished) setInspectFocus("activity"); setStep("investigation"); setActivityOpen(true);
      await openJob(linked.id);
    } catch (error) { if (generation === editorGeneration.current && owner === identity.current) setError(errorMessage(error)); }
  }, [openJob]);

  useEffect(() => {
    let active = true;
    const changedInAnotherTab = sessionStorage.getItem(sessionReloadKey) === "true";
    sessionStorage.removeItem(sessionReloadKey);
    api.session().then(value => {
      if (!active) return;
      if (value.principal) { setPrincipal(value.principal); setChecking(false); return; }
      setChecking(false);
      if (changedInAnotherTab) { setNotice("The workspace changed in another tab. Connect to open a new guest workspace."); return; }
      void connect();
    }).catch(error => { if (active) { setError(errorMessage(error)); setChecking(false); } });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    function onSessionChange(event: StorageEvent) {
      if (!changedWorkspace(event, identity.current)) return;
      sessionStorage.setItem(sessionReloadKey, "true");
      window.location.reload();
    }
    window.addEventListener("storage", onSessionChange);
    return () => window.removeEventListener("storage", onSessionChange);
  }, []);
  useEffect(() => { setOpenLastDraft(readOpenLastDraft()); }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 3000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (!error) return;
    const timer = window.setTimeout(() => setError(""), 3000);
    return () => window.clearTimeout(timer);
  }, [error]);
  useEffect(() => {
    if (principal?.principal_kind !== "anonymous") { setGuestNote(false); return; }
    setGuestNote(true);
    const timer = window.setTimeout(() => setGuestNote(false), 3000);
    return () => window.clearTimeout(timer);
  }, [principal?.user_id, principal?.principal_kind]);
  useEffect(() => { if (draft?.id) localStorage.setItem(LAST_DRAFT_KEY, draft.id); }, [draft?.id]);
  useEffect(() => {
    if (!settingsOpen) return;
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") setSettingsOpen(false); }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [settingsOpen]);
  useEffect(() => {
    identity.current = principal?.user_id || null;
    if (!principal) return;
    setPending(storedIntent(principal.user_id));
    void refresh().catch(error => setError(errorMessage(error)));
    const params = new URLSearchParams(window.location.search);
    if (params.get("draft") || params.get("job")) setWelcomeOpen(false);
    if (params.get("draft")) void openDraft(params.get("draft")!);
    if (params.get("job")) { setStep("investigation"); setActivityOpen(true); void openJob(params.get("job")!); }
    return () => { identity.current = null; ++editorGeneration.current; ++jobGeneration.current; };
  }, [principal?.user_id, refresh, openDraft, openJob]);
  useEffect(() => {
    if (!principal) return;
    const controller = new AbortController(); let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const invalidated = new Set<string>();
    const scheduleRefresh = (kinds: string[]) => {
      for (const kind of kinds) invalidated.add(kind);
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => {
        const kinds = [...invalidated]; invalidated.clear();
        void refresh(kinds).catch(error => { if (!controller.signal.aborted) setError(errorMessage(error)); });
      }, 120);
    };
    void followWorkspace({ signal: controller.signal, onState: setWorkspaceState, onChange: change => {
      const kinds = change ? change.collections.filter(value => ["drafts", "jobs"].includes(value)) : ["drafts", "jobs"];
      if (kinds.length) scheduleRefresh(kinds);
    } }).catch(error => { if (!controller.signal.aborted) setWorkspaceState(errorMessage(error)); });
    return () => { controller.abort(); if (refreshTimer) clearTimeout(refreshTimer); };
  }, [principal?.user_id, refresh]);
  useEffect(() => {
    if (!principal || !job) return;
    const id = job.id, controller = new AbortController();
    if (!submittedJob.current && job.kind === "analysis" && !terminal(job.status)) {
      const params = new URLSearchParams(window.location.search);
      const draftId = params.get("draft");
      if (draftId && params.get("job") === id) { submittedJob.current = id; submittedDraft.current = draftId; }
    }
    setStreamState("Connecting to live activity…");
    async function readLatest() {
      const value = await api.job(id);
      if (!controller.signal.aborted && currentJob.current?.id === id) { currentJob.current = value; setJob(value); }
      return value;
    }
    void followJob(id, { signal: controller.signal, onState: setStreamState,
      onResync: async () => { const value = await readLatest(); setNotice("Older activity is unavailable. The current saved job was reloaded."); return { cursor: value.last_event_id, terminal: terminal(value.status) }; },
      onEvent: event => {
        if (controller.signal.aborted) return;
        setActivity(items => items.some(item => item.id === event.id) ? items : [...items, event].slice(-1000));
        setJob(value => value?.id === id ? { ...value, status: event.status, stage: event.stage, result: event.result || value.result,
          updated_at: event.occurred_at, completed_at: terminal(event.status) ? event.occurred_at : value.completed_at, last_event_id: event.id } : value);
        if (terminal(event.status)) {
          setStreamState("Activity complete");
          setStep("investigation"); setActivityDetail(true); setActivityLogOpen(false);
          if (job.kind === "analysis" && submittedJob.current === id && !completionSaved.current.has(id)) {
            completionSaved.current.add(id);
            void saveRef.current(false, { quiet: true, updateOnly: true, draftId: submittedDraft.current || undefined }).then(saved => { if (!saved) completionSaved.current.delete(id); });
          }
          void readLatest().then(() => refresh(["jobs"])).catch(error => setError(errorMessage(error)));
        }
      },
    }).then(() => { if (!controller.signal.aborted) setStreamState("Activity complete"); })
      .catch(error => { if (!controller.signal.aborted) setStreamState(errorMessage(error)); });
    return () => controller.abort();
  // The stream owns status changes; only selecting a different job reconnects it.
  }, [principal?.user_id, job?.id, streamAttempt, refresh]);
  useEffect(() => () => { searchAbort.current?.abort(); suggestionAbort.current?.abort(); }, []);
  const paragraphKey = job?.result?.kind === "analysis" ? job.result.paragraph_job_ids.join(",") : "";
  useEffect(() => {
    const ids = paragraphKey ? paragraphKey.split(",") : [];
    if (!ids.length) { setParagraphs({}); return; }
    let active = true;
    const timers = new Map<string, ReturnType<typeof setTimeout>>();
    setParagraphs({});
    async function load(id: string) {
      try {
        const value = await api.job(id);
        if (!active) return;
        if (value.result?.kind === "paragraph" && value.status === "succeeded") {
          const record = await request<Record<string, unknown>>(backend("paragraphs/" + encodeURIComponent(value.result.paragraph_id)));
          if (!active) return;
          setParagraphs(current => ({ ...current, [id]: { job: value, text: paragraphText(record) } }));
          return;
        }
        setParagraphs(current => ({ ...current, [id]: { ...current[id], job: value } }));
        if (!terminal(value.status)) timers.set(id, setTimeout(() => void load(id), 3000));
      } catch (err) {
        if (active) setParagraphs(current => ({ ...current, [id]: { ...current[id], error: errorMessage(err) } }));
      }
    }
    for (const id of ids) void load(id);
    return () => { active = false; for (const timer of timers.values()) clearTimeout(timer); };
  }, [paragraphKey]);
  const publicationKey = job && terminal(job.status)
    ? job.result?.kind === "analysis" && job.result.account_ids[0] ? "account:" + job.result.account_ids[0]
      : job.result?.kind === "analysis_outcome" ? "outcome:" + job.result.outcome_id : ""
    : "";
  useEffect(() => {
    if (!publicationKey) { setPublication(null); return; }
    let active = true;
    setPublication(null);
    const path = publicationKey.startsWith("outcome:")
      ? "analysis-outcomes/" + encodeURIComponent(publicationKey.slice("outcome:".length)) + "/publication"
      : "accounts/" + encodeURIComponent(publicationKey.slice("account:".length));
    function load() {
      void request<{ visibility?: string; publication?: { visibility?: string } }>(backend(path))
        .then(record => {
          if (!active) return;
          const visibility = record.publication?.visibility || record.visibility;
          setPublication(visibility === "public" ? "public" : "private");
        })
        .catch(() => { if (active) setPublication("unknown"); });
    }
    load();
    function onVisible() { if (document.visibilityState === "visible") load(); }
    document.addEventListener("visibilitychange", onVisible);
    return () => { active = false; document.removeEventListener("visibilitychange", onVisible); };
  }, [publicationKey]);
  useLayoutEffect(() => {
    if (!activityLogOpen) return;
    const node = activityLogRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [activity, activityLogOpen, step]);
  useEffect(() => {
    if (!job || !terminal(job.status) || activityCollapsed.current === job.id) return;
    activityCollapsed.current = job.id;
    setStep("investigation"); setActivityDetail(true); setActivityLogOpen(false);
  }, [job]);
  useEffect(() => {
    if (welcomeOpen || !startFocus.current) return;
    const id = startFocus.current;
    if (id === "job-select" && !activityOpen) return;
    if (id === "gap-search" && step !== "gap") return;
    startFocus.current = null;
    const element = document.getElementById(id);
    element?.scrollIntoView({ block: "center" });
    element?.focus();
  }, [welcomeOpen, activityOpen, step, focusNonce]);
  useEffect(() => {
    if (step === "anchors" && !gapChosen) setStep("gap");
  }, [step, gapChosen]);
  useEffect(() => {
    if (!sessionOpen && !helpOpen) return;
    function close(event: MouseEvent) {
      const target = event.target as Node;
      if (!sessionMenu.current?.contains(target)) setSessionOpen(false);
      if (!helpMenu.current?.contains(target)) setHelpOpen(false);
    }
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") { setSessionOpen(false); setHelpOpen(false); } }
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", onKey); };
  }, [sessionOpen, helpOpen]);
  useEffect(() => {
    if (!cfdeGate) return;
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") setCfdeGate(null); }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [cfdeGate]);
  useEffect(() => {
    if (!draftPicker) return;
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") { if (pendingDelete) setPendingDelete(null); else setDraftPicker(false); } }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [draftPicker, pendingDelete]);
  useEffect(() => {
    if (!draftPicker) return;
    const last = Math.max(0, Math.ceil(drafts.length / DRAFT_PAGE_SIZE) - 1);
    if (draftPage > last) setDraftPage(last);
  }, [drafts.length, draftPage, draftPicker]);
  useEffect(() => {
    if (!draftPicker) return;
    const controller = new AbortController();
    setCatalogReady(false); setRequestsReady(false);
    const gapIds = [...new Set(drafts.flatMap(item => item.composer.source_gap ? [item.composer.source_gap.id] : []))];
    const factorIds = [...new Set(drafts.flatMap(item => item.composer.eaggl_anchors.map(anchor => anchor.reference.source_id)))];
    async function labelsFor(ids: string[], read: (id: string) => Promise<string>, apply: (labels: Record<string, string>) => void) {
      const labels: Record<string, string> = {};
      for (let index = 0; index < ids.length; index += 8) {
        if (controller.signal.aborted) return;
        const batch = await Promise.all(ids.slice(index, index + 8).map(async id => {
          try { return [id, await read(id)] as const; }
          catch (error) { if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error; return [id, "Unavailable"] as const; }
        }));
        for (const [id, label] of batch) if (label) labels[id] = label;
        if (!controller.signal.aborted) apply({ ...labels });
      }
    }
    void (async () => {
      try {
        await Promise.all([
          labelsFor(gapIds, async id => { const gap = await api.gap(id, controller.signal); return gap.object.text || gapTitle(gap); }, setGapLabels),
          labelsFor(factorIds, async id => factorTitle(await api.factor(id, controller.signal)), setFactorLabels),
          api.requests(controller.signal).then(page => { if (!controller.signal.aborted) setDraftRequests(page.items); }).catch(error => {
            if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error;
          }).finally(() => { if (!controller.signal.aborted) setRequestsReady(true); }),
        ]);
        if (!controller.signal.aborted) setCatalogReady(true);
      } catch { /* A newer list replaced this lookup. */ }
    })();
    return () => controller.abort();
  }, [draftPicker, drafts]);
  useEffect(() => {
    if (!missingFactorKey) return;
    const ids = missingFactorKey.split("\n");
    const controller = new AbortController();
    void (async () => {
      const loaded: Record<string, Factor> = {};
      const missed: string[] = [];
      try {
        for (let index = 0; index < ids.length; index += 8) {
          if (controller.signal.aborted) return;
          const batch = await Promise.all(ids.slice(index, index + 8).map(async id => {
            try { return [id, await api.factor(id, controller.signal)] as const; }
            catch (error) { if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error; return [id, null] as const; }
          }));
          for (const [id, factor] of batch) { if (factor) loaded[id] = factor; else missed.push(id); }
        }
        if (controller.signal.aborted) return;
        if (Object.keys(loaded).length) setFactors(current => ({ ...current, ...loaded }));
        if (missed.length) setFactorMisses(current => ({ ...current, ...Object.fromEntries(missed.map(id => [id, true])) }));
      } catch { /* A newer selection replaced this lookup. */ }
    })();
    return () => controller.abort();
  }, [missingFactorKey]);

  async function connect() {
    setBusy("connect"); setError("");
    try {
      setLocation("draft", null); setLocation("job", null);
      const value = await api.connect();
      let restore: string | null = null;
      if (readOpenLastDraft()) {
        const id = localStorage.getItem(LAST_DRAFT_KEY);
        if (id) {
          try { await api.draft(id); restore = id; }
          catch (error) { if (error instanceof ApiError && error.status === 404) localStorage.removeItem(LAST_DRAFT_KEY); }
        }
      }
      if (restore) setLocation("draft", restore);
      setPrincipal(value.principal); setNotice(""); setPanelView("menu"); setWelcomeOpen(!restore); setActivityOpen(false); setSettingsOpen(false);
      setDraft(null); setComposer(emptyComposer()); setName(""); setGap(null); setSuggestion(null); setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false; setStep("gap"); currentJob.current = null; setJob(null);
    }
    catch (error) { setError(errorMessage(error)); }
    finally { setBusy(""); }
  }
  async function disconnect() {
    const guest = principal?.principal_kind === "anonymous";
    if (guest && !window.confirm("Disconnect this guest workspace? You will lose access to its saved work. Running jobs continue, and reconnecting creates a new workspace.")) return;
    setBusy("disconnect");
    try {
      await api.disconnect(); setSessionOpen(false); setDraftPicker(false); setHelpOpen(false); setSettingsOpen(false); setWelcomeOpen(false); setPanelView("menu"); setStep("gap"); setActivityOpen(false); setPrincipal(null); currentJob.current = null; setJob(null); setDraft(null); setJobs([]); setDrafts([]);
      setComposer(emptyComposer()); setName(""); setGap(null); setSuggestion(null); setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false; setActivity([]); setPending(null);
      setNotice(guest ? "Disconnected. Access to this guest workspace is lost. Running jobs remain on the server." : "Disconnected. Saved work and running jobs remain on the server.");
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(""); }
  }
  async function search() {
    searchAbort.current?.abort(); const controller = new AbortController(); searchAbort.current = controller;
    setSearching(true); setError("");
    try {
      const values = await api.gaps(query, controller.signal);
      if (!controller.signal.aborted) {
        searchedQuery.current = query;
        rememberGapSearch(query, draft?.id, composer.source_gap?.source_id);
        setGaps(values); setSearched(true); setGapListMode(query.trim() ? "search" : "trending");
      }
    }
    catch (error) { if (!controller.signal.aborted) setError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setSearching(false); }
  }
  async function suggest(next: Composer, replaceSelection: boolean) {
    suggestionAbort.current?.abort(); const controller = new AbortController(); suggestionAbort.current = controller;
    const generation = editorGeneration.current; setSuggesting(true); setError("");
    try {
      const value = await api.suggest(next, controller.signal);
      if (controller.signal.aborted || generation !== editorGeneration.current) return;
      suggestedSubquery.current = next.mechanism_subquery || "";
      setSuggestion(value); setFactors(values => ({ ...values, ...Object.fromEntries(value.automatic_anchors.map(anchor => [anchor.factor.source_id, anchor.factor])) }));
      const suggested = value.automatic_anchors.map(anchor => anchor.factor);
      setComposer(current => {
        if (current.source_gap?.source_id !== next.source_gap?.source_id) return current;
        if (!replaceSelection && current.eaggl_anchors.length > 0) return current;
        return withFactors(current, suggested, value.suggestion_id, true);
      });
    } catch (error) { if (!controller.signal.aborted && generation === editorGeneration.current) setError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setSuggesting(false); }
  }
  function selectGap(value: Gap) {
    if (composer.source_gap?.source_id === value.source.source_id) return;
    ++editorGeneration.current;
    const next: Composer = { ...composer, source_gap: { id: value.object.id, source_id: value.source.source_id, source_revision: value.source.source_revision }, eaggl_anchors: [], dismissed_source_ids: [] };
    if (searchedQuery.current !== null) rememberGapSearch(searchedQuery.current, draft?.id, value.source.source_id);
    setGap(value); setComposer(next); setSuggestion(null); setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false; setNotice(""); setStep("anchors");
    void suggest(next, false);
  }
  function newDraft() {
    if (dirty && (composer.source_gap || name) && !window.confirm("Start a new draft? Unsaved changes will be discarded.")) return false;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false; setLocation("draft", null); setNotice(""); setError(""); setStep("gap"); setActivityOpen(false);
    return true;
  }
  function beginWithoutDraft() {
    if (dirty && (draft || composer.source_gap || name) && !window.confirm("Leave this draft and choose a knowledge gap? Unsaved changes will be discarded.")) return;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false;
    searchedQuery.current = null;
    setQuery(""); setGaps([]); setSearched(false); setGapListMode(null); setLocation("draft", null); setNotice(""); setError("");
    setStep("gap"); setActivityOpen(false); setWelcomeOpen(false); setPanelView("menu");
    startFocus.current = "gap-search"; setFocusNonce(value => value + 1);
  }
  function startSession() {
    setSessionOpen(false);
    if (dirty && (draft || composer.source_gap || name) && !window.confirm("Start a new session? Unsaved changes will be discarded.")) return;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false;
    searchedQuery.current = null;
    setQuery(""); setGaps([]); setSearched(false); setGapListMode(null); setLocation("draft", null); setLocation("job", null);
    setNotice(""); setError(""); setStep("gap"); setActivityOpen(false); setPanelView("menu");
    setWelcomeOpen(true);
    factorInspectId.current = "";
    setInspectGap(null); setInspectGapNote(""); setInspectFactor(null); setInspectFactorId(null); setInspectFactorNote(""); setInspectResult(null); setActivityLogOpen(false);
    currentJob.current = null; setJob(null);
  }
  function startSearch() {
    beginWithoutDraft();
  }
  function startFrom(action: "draft" | "gap" | "jobs") {
    setSessionOpen(false);
    if (action === "draft") { if (newDraft()) { setPanelView("draft"); setWelcomeOpen(true); } return; }
    if (action === "gap") { beginWithoutDraft(); return; }
    if (panelView === "copy") setName(keptName.current);
    if (action === "jobs") { setStep("investigation"); setActivityOpen(true); }
    else { setStep("gap"); startFocus.current = "gap-search"; }
    setWelcomeOpen(false); setPanelView("menu");
  }
  function openCopy() {
    setSessionOpen(false);
    if (panelView !== "copy") keptName.current = name;
    setName(""); setPanelView("copy"); setWelcomeOpen(true);
  }
  async function browseTrending() {
    if (dirty && (draft || composer.source_gap || name) && !window.confirm("Leave this draft and browse trending knowledge gaps? Unsaved changes will be discarded.")) return;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false;
    searchedQuery.current = null;
    setQuery(""); setGaps([]); setSearched(false); setGapListMode(null); setLocation("draft", null); setNotice(""); setError("");
    setStep("gap"); setActivityOpen(false); setWelcomeOpen(false); setPanelView("menu");
    searchAbort.current?.abort(); const controller = new AbortController(); searchAbort.current = controller;
    setSearching(true);
    try { const values = await api.gaps("", controller.signal); if (!controller.signal.aborted) { searchedQuery.current = ""; setGaps(values); setSearched(true); setGapListMode("trending"); } }
    catch (error) { if (!controller.signal.aborted) setError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setSearching(false); }
  }
  function showDrafts() {
    setSessionOpen(false); setHelpOpen(false); setDraftPage(0); setDraftPicker(true);
    void refresh(["drafts", "jobs"]);
  }
  function chooseDraft(id: string) {
    if (dirty && (composer.source_gap || name) && !window.confirm("Load the saved draft and discard unsaved changes?")) return;
    setDraftPicker(false); setWelcomeOpen(false); setPanelView("menu");
    void openDraft(id);
  }
  async function removeDraft(value: Draft) {
    setPendingDelete(null); setDeletingId(value.id); setError("");
    try {
      await mutationKeys.current.run(["delete", value.id, value.version], key => api.deleteDraft(value, key));
      if (draft?.id === value.id) {
        ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
        setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setFeasibility(null); setFullInvestigation(false); assessmentActive.current = false; setLocation("draft", null); setStep("gap"); setActivityOpen(false);
      }
      await refresh(["drafts"]);
    } catch (error) { setError(errorMessage(error)); }
    finally { setDeletingId(""); }
  }
  async function save(asNew = false, options?: { name?: string; quiet?: boolean; updateOnly?: boolean; draftId?: string; composer?: Composer }): Promise<Draft | null> {
    if (options?.updateOnly && (!draft || (options.draftId && draft.id !== options.draftId))) return null;
    const fromStart = welcomeOpen && panelView === "draft";
    const savedName = (options?.name ?? name).trim();
    const savedComposer = options?.composer ?? composer;
    const selectionChanged = !!draft && selectionSignature(savedComposer) !== selectionSignature(draft.composer);
    if (options?.updateOnly && selectionChanged) return null;
    const fromCopy = asNew || selectionChanged || (welcomeOpen && panelView === "copy");
    if (!options?.quiet) setBusy("save");
    setError("");
    try {
      const value = await mutationKeys.current.run(["save", fromCopy ? null : draft?.id, fromCopy ? null : draft?.version, savedComposer, savedName], key => api.save(fromCopy ? null : draft, savedComposer, savedName, key));
      setDraft(value); setComposer(value.composer); setName(value.name || ""); setLocation("draft", value.id);
      if (!fromStart && searchedQuery.current !== null) rememberGapSearch(searchedQuery.current, value.id, value.composer.source_gap?.source_id);
      if (fromStart) { searchedQuery.current = null; setGaps([]); setSearched(false); setGapListMode(null); setNotice(""); setStep("gap"); setActivityOpen(false); setWelcomeOpen(false); setPanelView("menu"); }
      else if (fromCopy && !options?.quiet) { setNotice(""); setWelcomeOpen(false); setPanelView("menu"); setStep(value.composer.source_gap ? "anchors" : "gap"); }
      await refresh();
      return value;
    } catch (error) { setError(error instanceof ApiError && error.status === 409 && error.code !== "REFERENCE_GENERATION_SUPERSEDED" ? "This draft changed on the server. Open the saved draft again to review it before saving. Your local selections remain visible." : errorMessage(error)); return null; }
    finally { if (!options?.quiet) setBusy(""); }
  }
  saveRef.current = save;
  function noteActivity(message: string, eventType: JobEvent["event_type"] = "progress") {
    setActivity(items => [...items, activityNote(message, eventType)].slice(-1000));
  }
  function noteLightningProgress(progress: LightningAuditProgress) {
    const phase = progress.phase === "validating" ? "Checking the feasibility assessment." : "Writing the feasibility assessment.";
    if (lightningStatus.current !== phase) { lightningStatus.current = phase; noteActivity(phase); }
    const previous = lightningSeen.current;
    lightningSeen.current = progress;
    if (previous?.revision === progress.revision) return;
    const addedText = (label: string, before: string, after: string) => {
      const added = after.startsWith(before) ? after.slice(before.length).trim() : after.trim();
      if (added && added !== before.trim()) noteActivity(`${label}: ${added}`);
    };
    const addedItems = (label: string, before: string[], after: string[]) => {
      for (const item of after) if (item.trim() && !before.includes(item)) noteActivity(`${label}: ${item.trim()}`);
    };
    addedText("Test summary", previous?.summary || "", progress.summary);
    addedItems("Observations", previous?.observations || [], progress.observations);
    addedItems("Limitations", previous?.limitations || [], progress.limitations);
    addedItems("Missing evidence", previous?.missing_evidence || [], progress.missing_evidence);
    addedText("Recommended direction", previous?.recommended_direction || "", progress.recommended_direction);
    addedItems("Next steps", previous?.next_steps || [], progress.next_steps);
  }
  async function runLightningAudit(saved: Draft, cfde: "yes" | "no", existing?: LightningAudit) {
    const generation = editorGeneration.current;
    assessmentActive.current = true;
    lightningSeen.current = null; lightningStatus.current = "";
    setFeasibility({ draft: saved, cfde, audit: existing?.result ? existing : null });
    setFullInvestigation(false); setInvestigationTab("assessment"); setStep("investigation"); setActivityLogOpen(true); setInspectFocus("activity");
    try {
      let current = existing;
      if (!current) {
        noteActivity("Starting the feasibility assessment.");
        current = await mutationKeys.current.run(["lightning-audit", saved.id, saved.version], key => api.createLightningAudit({ draft_id: saved.id, draft_version: saved.version }, key));
      }
      const deadline = Date.now() + 150_000;
      while (current.status === "preparing" || current.status === "assessing") {
        if (generation !== editorGeneration.current) return;
        if (current.status === "preparing" && !lightningStatus.current) { lightningStatus.current = "preparing"; noteActivity("Preparing the feasibility assessment."); }
        if (current.progress) noteLightningProgress(current.progress);
        if (Date.now() > deadline) throw new Error("The feasibility assessment did not finish. Start the investigation again to retry.");
        await new Promise(resolve => setTimeout(resolve, 3000));
        current = await api.lightningAudit(current.id);
      }
      if (generation !== editorGeneration.current) return;
      if (current.status !== "succeeded" || !current.result) throw new Error(current.error?.detail || "The feasibility assessment did not finish. Start the investigation again to retry.");
      noteActivity("Feasibility assessment finished.");
      setFeasibility({ draft: saved, cfde, audit: current });
    } catch (error) {
      if (generation !== editorGeneration.current) return;
      const message = error instanceof Error && error.name === "Error" ? error.message : errorMessage(error);
      noteActivity(message, "failure");
      setError(message);
      setFeasibility(null);
    }
  }
  runLightningRef.current = runLightningAudit;
  async function assessCfdeSupport(saved: Draft): Promise<"yes" | "no"> {
    const created = await mutationKeys.current.run(["cfde-assessment", saved.id, saved.version, saved.composer], key => api.createCfdeAssessment(saved.id, { draft_version: saved.version, composer: saved.composer }, key));
    let current: CfdeAssessment = created;
    let shown = "";
    const deadline = Date.now() + 120_000;
    while (current.status === "preparing" || current.status === "assessing") {
      const message = current.status === "preparing" ? "Preparing the CFDE assessment." : "Assessing CFDE support.";
      if (message !== shown) { shown = message; noteActivity(message); }
      if (Date.now() > deadline) throw new Error("The CFDE assessment did not finish. Start the investigation again to retry.");
      await new Promise(resolve => setTimeout(resolve, 1000));
      current = await api.cfdeAssessment(saved.id, current.id);
    }
    if (current.draft_id !== saved.id || current.draft_version !== saved.version || current.stale !== false) throw new Error("The CFDE assessment does not match this draft. Start the investigation again to retry.");
    if (current.status === "failed" || current.status === "interrupted") throw new Error(current.error?.detail || "The CFDE assessment did not finish. Start the investigation again to retry.");
    const support = current.status === "succeeded" ? current.result?.probability_yes : undefined;
    if (typeof support !== "number" || !Number.isFinite(support)) throw new Error("The CFDE assessment did not confirm support. The investigation was not started.");
    return support >= 0.54 ? "yes" : "no";
  }
  async function startInvestigation() {
    if (pending) { await beginAnalysis(); return; }
    if (!composer.source_gap || !composer.eaggl_anchors.length) return;
    ++editorGeneration.current;
    assessmentActive.current = true;
    setCfdeGate(null);
    setCfdePassed(false);
    setActivity([activityNote("Checking CFDE support for this investigation.")]);
    setActivityLogOpen(true); setInspectFocus("activity"); setStep("investigation");
    setFeasibility(null); setFullInvestigation(false); currentJob.current = null; setJob(null); setLocation("job", null);
    setBusy("assess"); setError("");
    let saved: Draft | null = null;
    let verdict: "yes" | "no" | null = null;
    try {
      const next = { ...composer, selected_kgs: ["biomarkerkg", "prokn"] as Composer["selected_kgs"] };
      saved = await save(false, { quiet: true, composer: next, name: investigationDraftName(query, gap) });
      if (!saved) { noteActivity("The draft could not be saved, so the CFDE assessment did not start.", "failure"); return; }
      verdict = await assessCfdeSupport(saved);
    } catch (error) {
      const message = error instanceof Error && error.name === "Error" ? error.message : errorMessage(error);
      noteActivity(message, "failure");
      setError(message);
      return;
    } finally { setBusy(""); }
    if (!saved || !verdict) return;
    if (verdict === "yes") {
      rememberCfdePassed(saved.id);
      setCfdePassed(true);
      noteActivity("CFDE evidence can support this investigation.");
      setBusy("lightning");
      try { await runLightningAudit(saved, "yes"); }
      finally { setBusy(""); }
      return;
    }
    forgetCfdePassed(saved.id);
    noteActivity("No CFDE evidence was found for this investigation.", "warning");
    setCfdePassed(false);
    setCfdeGate(saved);
  }
  async function continueAfterCfde(saved: Draft) {
    setBusy("lightning");
    try { await runLightningAudit(saved, "no"); }
    finally { setBusy(""); }
  }
  async function launchFullInvestigation(saved: Draft) {
    const previousJob = currentJob.current?.id || null;
    setFullInvestigation(true); setInvestigationTab("result");
    await beginAnalysis(saved);
    if (currentJob.current?.id === previousJob) setFullInvestigation(false);
  }
  async function beginAnalysis(saved?: Draft) {
    if (!principal) return;
    const source = saved || draft;
    if (!pending && (!source || (!saved && dirty))) return;
    if (!pending && (!source?.composer.source_gap || !source.composer.eaggl_anchors.length)) return;
    const intent = pending || { body: { kind: "analysis" as const, draft_id: source!.id, draft_version: source!.version, budgets: { max_accounts: 1 } }, key: crypto.randomUUID() };
    setPending(intent); sessionStorage.setItem("reveal-submit:" + principal.user_id, JSON.stringify(intent));
    setBusy("submit"); setError("");
    try {
      const value = await api.submit(intent.body, intent.key);
      sessionStorage.removeItem("reveal-submit:" + principal.user_id); setPending(null);
      submittedJob.current = value.id; submittedDraft.current = intent.body.draft_id; setStartedDraftId(intent.body.draft_id); activityPinned.current = null; activityCollapsed.current = null; currentJob.current = value; setJob(value); setActivity([]); setLocation("job", value.id); setActivityDetail(true); setActivityLogOpen(true); setInspectFocus("activity"); setStep("investigation"); setActivityOpen(true); setNotice("Analysis submitted. Live activity will appear alongside your draft."); await refresh();
    } catch (error) {
      // A later refusal cannot disprove an earlier committed request whose response was lost.
      setError(errorMessage(error));
    } finally { setBusy(""); }
  }
  function discardSubmission() {
    if (!principal || !window.confirm("The original request may already have created a job. Check Workspace jobs first. Discard this recovery key and allow a new submission?")) return;
    sessionStorage.removeItem("reveal-submit:" + principal.user_id); setPending(null);
  }
  async function cancel() {
    if (!job) return; const id = job.id; setBusy("cancel"); setError("");
    try { const value = await api.cancel(id); if (currentJob.current?.id === id) { currentJob.current = value; setJob(value); } setNotice("Stop requested. The service will preserve captured work and clean up the research sandbox."); await refresh(); }
    catch (error) { setError(errorMessage(error)); } finally { setBusy(""); }
  }
  async function retryReview() {
    if (!job) return; const id = job.id; setBusy("review"); setError("");
    try {
      const value = await mutationKeys.current.run(["review", job.id, job.last_event_id], key => api.retryReview(job, key)); if (currentJob.current?.id === id) { currentJob.current = value; setJob(value); setActivity([]); setStreamAttempt(value => value + 1); } await refresh();
    } catch (error) { setError(errorMessage(error)); } finally { setBusy(""); }
  }

  async function openInspect(value: Gap) {
    setInspectGap(value); setInspectGapNote(""); setInspectFocus("gap");
    try {
      const fresh = await api.gap(value.object.id);
      setInspectGap(current => current?.object.id === value.object.id ? fresh : current);
    } catch (error) { setInspectGapNote(errorMessage(error)); }
  }
  async function openFactorInspect(sourceId: string) {
    factorInspectId.current = sourceId; setInspectFactorId(sourceId); setInspectFocus("factor"); setInspectFactorNote("");
    setInspectFactor(factors[sourceId] ?? null);
    try {
      const fresh = await api.factor(sourceId);
      if (factorInspectId.current !== sourceId) return;
      setFactors(values => ({ ...values, [fresh.source_id]: fresh })); setInspectFactor(fresh);
    } catch (error) { if (factorInspectId.current === sourceId) setInspectFactorNote(errorMessage(error)); }
  }
  function closeGapInspect() {
    setInspectGap(null); setInspectGapNote("");
    if (inspectFactorId) setInspectFocus("factor");
    else if (inspectResult) setInspectFocus("result");
    else if (activityLogOpen) setInspectFocus("activity");
  }
  function closeFactorInspect() {
    factorInspectId.current = ""; setInspectFactorId(null); setInspectFactor(null); setInspectFactorNote("");
    if (inspectGap) setInspectFocus("gap");
    else if (inspectResult) setInspectFocus("result");
    else if (activityLogOpen) setInspectFocus("activity");
  }
  function openResultInspect(value: ResultInspect) {
    setInspectResult(value); setInspectFocus("result");
  }
  function closeResultInspect() {
    setInspectResult(null);
    if (inspectFactorId) setInspectFocus("factor");
    else if (inspectGap) setInspectFocus("gap");
    else if (activityLogOpen) setInspectFocus("activity");
  }
  function openActivityInspect() {
    setActivityLogOpen(true); setInspectFocus("activity");
  }
  function closeActivityInspect() {
    setActivityLogOpen(false);
    if (inspectResult) setInspectFocus("result");
    else if (inspectFactorId) setInspectFocus("factor");
    else if (inspectGap) setInspectFocus("gap");
  }
  const maxGapAccounts = Math.max(0, ...gaps.map(item => item.scientific_accounts?.count ?? 0));
  const inWorkspace = Boolean(principal) && !welcomeOpen;
  const errorNotice = error ? <div className="notice error" role="alert"><span>{error}</span><button className="quiet" onClick={() => setError("")} aria-label="Dismiss error">Dismiss</button></div> : null;
  const paragraphWriting = Object.values(paragraphs).some(item => item.job && !terminal(item.job.status));
  const factorBubbles = (limit?: number) => {
    const shown = limit == null ? composer.eaggl_anchors : composer.eaggl_anchors.slice(0, limit);
    const extra = composer.eaggl_anchors.length - shown.length;
    const bubbles = shown.map(anchor => { const factor = factors[anchor.reference.source_id]; const title = factor ? factorTitle(factor) : anchor.reference.source_id; return <span className="factor-bubble" key={anchor.reference.source_id}>{title}</span>; });
    if (!extra) return bubbles;
    return <>{bubbles}<span className="factor-more">+ {extra} {extra === 1 ? "factor" : "factors"}</span></>;
  };
  const factorList = composer.eaggl_anchors.map(anchor => { const factor = factors[anchor.reference.source_id]; return factor ? factorTitle(factor) : anchor.reference.source_id; }).join(", ");
  const feasibilityResult = feasibility?.audit?.result;
  const feasibilityReport = feasibilityResult ? <FeasibilityReport result={feasibilityResult} /> : feasibility ? <p role="status" className="loading">Running the feasibility assessment…</p> : busy === "assess" ? <p role="status" className="loading">Checking CFDE support…</p> : null;
  const showTabs = fullInvestigation && !!feasibilityResult;
  const showStartFull = !!feasibilityResult && !fullInvestigation && !job && !selectionForked;
  const jobReport = <>
    {!job && showTabs && <p role="status" className="loading">Submitting the investigation…</p>}
    {job && <div className="activity-status-row"><strong>Status:</strong><p>Started {date(job.created_at)}</p>{job.completed_at && <p>Completed {date(job.completed_at)}</p>}<span className={"status " + job.status}>{readable(job.status)}</span>{paragraphWriting && <span className="status running">Writing the cited claim</span>}{!terminal(job.status) && <button className="danger small" onClick={cancel} disabled={!!busy || job.status === "cancel_requested"}>{job.status === "cancel_requested" ? "Stopping…" : "Stop"}</button>}</div>}
    {!!job?.warnings.length && <div className="notice"><ul>{job.warnings.map(value => <li key={value}>{value}</li>)}</ul></div>}
    {job?.failure && <div className="notice error" role="alert"><div><strong>Research could not complete</strong><p>{job.failure.message}</p><span className="small">{job.failure.code}</span>{job.failure.code.startsWith("REVIEW_") && job.failure.retryable && <p><button className="secondary" onClick={retryReview} disabled={!!busy}>{busy === "review" ? "Requesting review…" : "Retry saved review"}</button></p>}</div></div>}
    {job?.status === "cancelled" && <p className="notice">This job was stopped. No successful result is implied.</p>}
    {(job?.status === "failed" || job?.status === "cancelled") && <p className="session-restart"><button type="button" onClick={startSession}>Start a new session?</button></p>}
    {job?.result && <ResultView job={job} principalKind={principal?.principal_kind} onActions={setResultActions} />}
    {job?.kind === "analysis" && job.status === "succeeded" && <section className="paragraph-result"><h3>Cited claim</h3>{job.result?.kind === "analysis" && job.result.paragraph_job_ids.map(id => <ParagraphView key={id} progress={paragraphs[id]} />)}</section>}
  </>;
  const investigationPanel = showTabs ? (investigationTab === "assessment" ? feasibilityReport : jobReport) : <>{feasibilityReport}{jobReport}</>;
  const showActivityLink = !!job || !!feasibility || activity.length > 0;
  const showEvidenceProvenance = job?.kind === "analysis" && job.status === "succeeded";
  const headingAside = showStartFull || showEvidenceProvenance;
  const activeStep = step === "anchors" || step === "investigation" ? step : "gap";
  if (trackedStep !== activeStep) {
    const previous = trackedStep;
    const snapshot = previous === "gap" ? (inspectGap ? "gap" as const : null) : previous === "anchors" ? (inspectFactorId ? "factor" as const : null) : inspectFocus === "result" && inspectResult ? "result" as const : inspectFocus === "activity" && activityLogOpen ? "activity" as const : activityLogOpen ? "activity" as const : inspectResult ? "result" as const : null;
    const saved = inspectorMemory[activeStep];
    const restored = saved === "gap" && inspectGap ? "gap" as const : saved === "factor" && inspectFactorId ? "factor" as const : saved === "result" && inspectResult ? "result" as const : saved === "activity" && activityLogOpen ? "activity" as const : null;
    setTrackedStep(activeStep);
    setInspectorMemory(current => ({ ...current, [previous]: snapshot }));
    if (restored && restored !== inspectFocus) setInspectFocus(restored);
  }
  const showGapInspect = activeStep === "gap" && !!inspectGap;
  const showFactorInspect = activeStep === "anchors" && !!inspectFactorId;
  const showResultInspect = activeStep === "investigation" && !!inspectResult;
  const showActivityInspect = activeStep === "investigation" && activityLogOpen;
  const inspectColumnOpen = showGapInspect || showFactorInspect || showResultInspect || showActivityInspect;
  const inspectColumnFocus = activeStep === "gap" ? "gap" as const : activeStep === "anchors" ? "factor" as const : inspectFocus === "activity" && showActivityInspect ? "activity" as const : inspectFocus === "result" && showResultInspect ? "result" as const : showActivityInspect ? "activity" as const : "result" as const;
  const analysisRunning = !!job && !terminal(job.status);
  const step3Ready = !!job || !!feasibility || busy === "assess" || busy === "lightning";
  const lastActivity = displayActivity(activity).at(-1) ?? null;
  const analysisComplete = !!job && terminal(job.status);
  const publicationLabel = publication === "public" ? "Published" : "";
  const investigationRail = job && analysisRunning ? <span className="step-rail-facts"><span>Started {date(job.created_at)}</span><span className={"status " + job.status}>{readable(job.status)}</span>{lastActivity && <span>{lastActivity.message}</span>}</span>
    : job && analysisComplete ? <span className="step-rail-line">Completed {date(job.completed_at || job.updated_at)} | {outcomeLabel(job.status)}{publicationLabel ? ` | ${publicationLabel}` : ""}</span>
    : null;
  return <>
    <header className="site-header"><div className="brand"><div className="brand-logos"><img src="/brand/cfde-knowledge-center.svg" alt="CFDE Knowledge Center" /><span className="brand-rule" aria-hidden="true" /><img src="/brand/cfde-ecosystem.png" alt="Common Fund Data Ecosystem" /></div><span className="brand-rule" aria-hidden="true" /><div className="brand-copy"><h1><span className="brand-reveal">REVEAL</span><span className="brand-product">Close the gap</span></h1><p>Choose the gap. Ground the claim.</p></div></div>
      <div className="connection">
        {principal && <div className="session-menu" ref={sessionMenu}><button className="secondary" aria-expanded={sessionOpen} aria-haspopup="menu" aria-controls="session-menu" onClick={() => { setHelpOpen(false); setSessionOpen(open => !open); }}>Session</button>
          {sessionOpen && <div id="session-menu" className="session-menu-list" role="menu"><button role="menuitem" onClick={startSession}>Start session</button><button role="menuitem" onClick={showDrafts} disabled={!drafts.length}>Saved drafts</button><button role="menuitem" onClick={() => { setSessionOpen(false); void browseTrending(); }}>Trending gaps</button></div>}
        </div>}
        {principal && <div className="session-menu" ref={helpMenu}><button className="secondary" aria-expanded={helpOpen} aria-haspopup="menu" aria-controls="help-menu" onClick={() => { setSessionOpen(false); setHelpOpen(open => !open); }}>Help</button>
          {helpOpen && <div id="help-menu" className="session-menu-list" role="menu"><button role="menuitem" onClick={() => setHelpOpen(false)}>Learn REVEAL Close the gap</button><button role="menuitem" onClick={() => setHelpOpen(false)}>Quick start tutorial</button></div>}
        </div>}
        {principal ? <span className="connection-status" role="status">Workspace connected</span> : <button onClick={() => void connect()} disabled={checking || busy === "connect"}>{checking || busy === "connect" ? "Connecting…" : "Connect workspace"}</button>}
        <button type="button" className="settings-button" aria-label="Settings" aria-expanded={settingsOpen} onClick={() => { setSessionOpen(false); setHelpOpen(false); setSettingsTab("settings"); setSettingsOpen(true); }}><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.49.49 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 0 0-.48-.41h-3.84a.48.48 0 0 0-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 0 0-.59.22L2.74 8.87a.48.48 0 0 0 .12.61l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.49.49 0 0 0-.12-.61l-2.03-1.58zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z" /></svg></button>
      </div></header>
    {inWorkspace && <div className="workspace-bar"><div className="step-rail" role="tablist" aria-label="Investigation steps"><button type="button" role="tab" aria-selected={activeStep === "gap"} className={"step-rail-card" + (activeStep === "gap" ? " active" : "")} onClick={() => setStep("gap")}><span className="step-rail-title"><span className="step-number">1</span>Choose a knowledge gap</span>{gap && <span className="step-rail-summary">{gap.object.text || gapTitle(gap)}</span>}</button><button type="button" role="tab" aria-selected={activeStep === "anchors"} className={"step-rail-card" + (activeStep === "anchors" ? " active" : "") + (gapChosen ? "" : " inactive")} disabled={!gapChosen && activeStep !== "anchors"} onClick={() => { if (gapChosen) setStep("anchors"); }}><span className="step-rail-title"><span className="step-number">2</span>Select mechanism anchors</span>{anchorChosen && <span className="step-rail-summary bubbles">{factorBubbles(2)}</span>}</button><button type="button" role="tab" aria-selected={activeStep === "investigation"} className={"step-rail-card" + (activeStep === "investigation" ? " active" : "") + (step3Ready ? "" : " inactive")} disabled={!step3Ready && activeStep !== "investigation"} onClick={() => { if (step3Ready) setStep("investigation"); }}><span className="step-rail-title"><span className="step-number">3</span>Investigation</span>{investigationRail}</button></div>{errorNotice}</div>}
    <main className={inWorkspace ? "workspace" : undefined}>
      {guestNote && principal?.principal_kind === "anonymous" && <p className="notice">Guest workspace: available in this browser until {date(principal.workspace_expires_at || "")}. Clearing cookies or disconnecting loses access to saved work.</p>}
      {!inWorkspace && errorNotice}
      {(!principal || (welcomeOpen && panelView === "menu")) ? <section className="welcome"><div className="welcome-lead"><h2>Choose the gap. Ground the claim.</h2><svg className="welcome-mark" viewBox="0 0 132 18" aria-hidden="true"><line x1="16" y1="9" x2="116" y2="9" stroke="#a7a9ad" strokeWidth="1.7" /><circle cx="9" cy="9" r="6.1" fill="#fff" stroke="#e07b39" strokeWidth="1.8" /><circle cx="123" cy="9" r="7" fill="#e07b39" /></svg><div className="welcome-choices"><button type="button" onClick={startSearch} disabled={!principal || checking || busy === "connect"}>Search knowledge gaps</button><button type="button" onClick={() => void browseTrending()} disabled={!principal || checking || busy === "connect" || searching}>Trending knowledge gaps</button></div></div><img className="welcome-flow" src="/workflow_updated.svg" alt="Choose the gap. Ground the claim. Search or browse trending DisMech knowledge gaps and select one. The system suggests CFDE REVEAL KG mechanism factors matched to that gap's DisMech mechanisms, and you select them. Starting the investigation collects BiomarkerKG and ProKN evidence and writes a scientific account and cited claim." /><div className="welcome-cards"><a className="welcome-card" href="#learn-reveal-client" onClick={event => event.preventDefault()}><svg viewBox="0 0 72 56" aria-hidden="true"><path d="M36 12v32M14 16c7 5 15 5 22-2 7 7 15 7 22 2v28c-7 5-15 5-22-2-7 7-15 7-22 2V16z" /></svg><strong>Learn REVEAL Close the gap</strong><span>A guide to the workspace, from a knowledge gap to a grounded claim.</span></a><a className="welcome-card" href="#quick-start-demo" onClick={event => event.preventDefault()}><svg viewBox="0 0 72 56" aria-hidden="true"><rect x="14" y="12" width="44" height="32" rx="3" /><path className="card-icon-fill" d="M33 22l12 6-12 6z" /></svg><strong>Watch quick start demo</strong><span>A short walkthrough of connecting and starting an investigation.</span></a></div></section> : welcomeOpen ? <div className="welcome-panel-stage"><section className="welcome-panel" role="dialog" aria-modal="true" aria-labelledby="welcome-heading"><button type="button" className="panel-back" aria-label="Close" onClick={() => { if (panelView === "copy") setName(keptName.current); else { setName(""); setNotice(""); } setPanelView("menu"); }}>×</button><div className="draft-start"><h2 id="welcome-heading">{panelView === "copy" ? "Save as a new draft" : "Start a draft"}</h2><label htmlFor="draft-name">Draft name</label><input id="draft-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} disabled={!mutable} placeholder="Name this investigation" autoFocus />{notice && <p className="notice" role="status">{notice}</p>}<div className="actions"><button onClick={() => save(panelView === "copy")} disabled={!mutable || !name.trim() || (panelView !== "copy" && !dirty)}>{busy === "save" ? "Saving…" : "Save draft"}</button></div></div></section></div> : <>
        <div className={inspectColumnOpen ? "workspace-frames" : "workspace-frames steps-only"}>
        <div className="steps">
          {activeStep === "gap" && <section className="step open">
            <h2 className="step-heading"><span className="step-number">1</span>Choose a knowledge gap</h2>
            <p className="step-guide">Find an existing scientific question that evidence still leaves unexplained. The gap you select becomes the question this investigation will try to ground.</p>
            <div className="step-body"><form className="search-control" autoComplete="off" onSubmit={event => { event.preventDefault(); void search(); }}><label className="sr-only" htmlFor="gap-search">Search knowledge gaps</label><input id="gap-search" name="reveal-gap-search" type="text" inputMode="search" autoComplete="off" autoCorrect="off" spellCheck={false} value={query} onChange={event => setQuery(event.target.value)} placeholder="Search a disease or research question" /><button type="submit" className="secondary" disabled={searching}>{searching ? "Searching…" : "Search"}</button></form>
              <label className="research-context" htmlFor="research-context"><span className="research-context-heading"><span>Research context (optional)</span><span className="research-context-note">This context is included in the mechanism search and the investigation.</span></span><input id="research-context" type="text" maxLength={2000} value={composer.mechanism_subquery || ""} onChange={event => setComposer(current => ({ ...current, mechanism_subquery: event.target.value }))} onBlur={() => { if (composer.source_gap && (composer.mechanism_subquery || "") !== (suggestedSubquery.current ?? "")) void suggest(composer, true); }} disabled={!mutable} autoComplete="off" /></label>
              {gaps.length > 0 && <div className="gap-results"><p className="gap-guide">{gapListMode === "selected" ? "The knowledge gap selected for this draft." : `${query.trim() ? `${gaps.length} knowledge gap${gaps.length === 1 ? "" : "s"} found.` : `${gaps.length} trending knowledge gaps.`} Use Select to choose one for the next step.`}</p><div className="gap-list" aria-label="Knowledge gap search results">{gaps.map(value => <GapOption key={value.source.source_id} gap={value} maxAccounts={maxGapAccounts} selected={composer.source_gap?.source_id === value.source.source_id} disabled={!mutable} onSelect={() => selectGap(value)} onInspect={() => void openInspect(value)} />)}</div><ul className="gap-legend"><li><span className="gap-swatch accounts" aria-hidden="true" />Accounts</li><li><span className="gap-swatch up" aria-hidden="true" />Upvotes</li><li><span className="gap-swatch down" aria-hidden="true" />Downvotes</li></ul></div>}
              {searched && !searching && !gaps.length && <p className="empty">No matching gaps. Try a broader disease name.</p>}
            </div>
          </section>}
          {activeStep === "anchors" && <section className="step open">
            <div className="step-heading-row"><div className="step-heading-copy"><h2 className="step-heading"><span className="step-number">2</span>Select mechanism anchors</h2><p className="step-guide">Choose genetic factors that may help explain the selected gap. Suggested factors start selected, and at least one is required to start an investigation.{suggestion?.limitations.length ? ` ${suggestion.limitations.join(" ")}` : ""}</p></div>{(anchorChosen || pending) && (!(startedDraftId && startedDraftId === draft?.id) || selectionForked) && !(feasibility && feasibility.draft.id === draft?.id && !selectionForked) && <button type="button" className="step-next" onClick={() => void startInvestigation()} disabled={!mutable || suggesting || !!cfdeGate || (!pending && (!composer.source_gap || !composer.eaggl_anchors.length))}>{busy === "assess" ? "Checking CFDE support…" : busy === "lightning" ? "Assessing feasibility…" : busy === "submit" ? "Submitting…" : busy === "save" ? "Saving draft…" : pending ? "Recover submission" : "Start investigation"}</button>}</div>
            <div className="step-body">{!composer.source_gap ? <p className="empty">Select a question to find related genetic mechanisms.</p> : <>
                {!!anchorIds.length && <FactorNetwork guide={anchorChosen ? undefined : "At least one factor has to be selected to initiate investigation."} gapLabel={gap?.object.text || (gap ? gapTitle(gap) : "Knowledge gap")} contextNode={(composer.mechanism_subquery || "").trim() && suggestion?.automatic_anchors.some(anchor => anchor.matched_context_ids.includes("mechanism_subquery")) ? { text: composer.mechanism_subquery.trim(), mechanisms: gapMechanisms(gap) } : null} rows={anchorIds.map(sourceId => {
                  const factor = factors[sourceId], selected = composer.eaggl_anchors.some(value => value.reference.source_id === sourceId);
                  const title = factor ? factorTitle(factor) : factorMisses[sourceId] ? sourceId : "Loading…";
                  const suggested = suggestion?.automatic_anchors.find(anchor => anchor.factor.source_id === sourceId);
                  const cosine = suggested?.ranking.metric === "cosine_similarity" ? suggested.ranking.value : null;
                  return { sourceId, title, subtitle: factor?.cfde_anchor.subtitle || "", mechanisms: matchedMechanisms(gap, suggested?.matched_context_ids || []), cosine, selected, selectDisabled: !mutable || (!selected && composer.eaggl_anchors.length >= 10), onToggle: () => setComposer(current => selected ? { ...current, eaggl_anchors: current.eaggl_anchors.filter(value => value.reference.source_id !== sourceId) } : factor && suggestion ? withFactors(current, [factor], suggestion.suggestion_id) : current), onInspect: factor ? () => void openFactorInspect(sourceId) : null };
                })} />}
                {!suggestedIds.length && (suggesting ? <p role="status" className="loading">Finding relevant mechanisms…</p> : <button type="button" className="quiet suggestion-refresh" disabled={!mutable} onClick={() => void suggest(composer, false)}>Show suggestions</button>)}
                {pending && <div className="notice"><span>A submission needs confirmation. Recover it with the original request key before starting another.</span><button className="quiet small" onClick={discardSubmission} disabled={!!busy}>Discard recovery</button></div>}
              </>}
            </div>
          </section>}
          {activeStep === "investigation" && <section className="step open">
            <div className={"step-heading-row investigation-heading" + (headingAside ? " with-aside" : "")}><div className="step-heading-copy"><h2 className="step-heading"><span className="step-number">3</span>Investigation</h2><p className="investigation-field">{gap ? <><strong>Knowledge gap:</strong> {gap.object.text || gapTitle(gap)}</> : <><strong>Knowledge gap:</strong> <span className="muted">Select a knowledge gap to investigate.</span></>}</p>{!!factorList && <p className="investigation-field"><strong>Factors:</strong> {factorList}</p>}{(showActivityLink || resultActions.length > 0) && <p className="investigation-nav">{resultActions.map((action, index) => <span className="status-link-item" key={action.key}>{index > 0 && <span className="status-rule" aria-hidden="true">|</span>}<button type="button" className="status-link" onClick={() => openResultInspect(action.inspect)}>{action.label}</button></span>)}{showActivityLink && <span className="status-link-item">{resultActions.length > 0 && <span className="status-rule" aria-hidden="true">|</span>}<button type="button" className="status-link" onClick={openActivityInspect}>Activity</button></span>}</p>}{showTabs && <div className="investigation-tabs" role="tablist" aria-label="Investigation contents"><button type="button" role="tab" aria-selected={investigationTab === "assessment"} onClick={() => setInvestigationTab("assessment")}>Feasibility assessment</button><button type="button" role="tab" aria-selected={investigationTab === "result"} onClick={() => setInvestigationTab("result")}>Investigation result</button></div>}</div>{headingAside && <div className="investigation-heading-actions">{showStartFull && <button type="button" className="step-next" onClick={() => { if (!feasibility) return; void launchFullInvestigation(feasibility.draft); }} disabled={!mutable || !!cfdeGate}>Start full investigation</button>}{showEvidenceProvenance && <p className="evidence-provenance"><button type="button">Evidence provenance</button></p>}</div>}</div>
            <div className="step-body">{investigationPanel}</div>
          </section>}
        </div>
        {inspectColumnOpen && <InspectColumn gap={showGapInspect ? inspectGap : null} factor={showFactorInspect ? inspectFactor : null} factorPending={showFactorInspect && !inspectFactor} result={showResultInspect ? inspectResult : null} activity={showActivityInspect ? <ActivityRecord events={activity} logRef={activityLogRef} /> : null} cfdePassed={cfdePassed} focus={inspectColumnFocus} gapNote={inspectGapNote} factorNote={inspectFactorNote} onFocus={setInspectFocus} onCloseGap={closeGapInspect} onCloseFactor={closeFactorInspect} onCloseResult={closeResultInspect} onCloseActivity={closeActivityInspect} />}
        </div>
      </>}
    </main>
    {draftPicker && <SavedDrafts drafts={drafts} jobs={jobs} requests={draftRequests} gapLabels={gapLabels} factorLabels={factorLabels} ready={catalogReady} requestsReady={requestsReady} page={draftPage} deletingId={deletingId} onPage={setDraftPage} onOpen={chooseDraft} onDelete={setPendingDelete} onClose={() => { setPendingDelete(null); setDraftPicker(false); }} />}
    {settingsOpen && <SettingsPanel tab={settingsTab} openLastDraft={openLastDraft} onTab={setSettingsTab} onOpenLastDraft={value => { localStorage.setItem(SETTINGS_KEY, JSON.stringify({ openLastDraft: value })); setOpenLastDraft(value); }} onClose={() => setSettingsOpen(false)} />}
    {pendingDelete && <div className="warning-stage"><section className="warning-panel" role="alertdialog" aria-modal="true" aria-labelledby="delete-draft-heading" aria-describedby="delete-draft-copy"><h2 id="delete-draft-heading">Delete this draft</h2><p id="delete-draft-copy">{`Delete "${pendingDelete.name || "Untitled draft"}"? A submitted investigation from this draft stays available.`}</p><div className="actions"><button type="button" className="secondary" autoFocus onClick={() => setPendingDelete(null)}>Cancel</button><button type="button" className="confirm-delete" disabled={!!deletingId} onClick={() => void removeDraft(pendingDelete)}>{deletingId ? "Deleting…" : "Delete"}</button></div></section></div>}
    {cfdeGate && <div className="warning-stage"><section className="warning-panel" role="alertdialog" aria-modal="true" aria-labelledby="cfde-gate-heading" aria-describedby="cfde-gate-copy"><h2 id="cfde-gate-heading">No CFDE evidence</h2><p id="cfde-gate-copy">No CFDE evidence was found for this investigation. Continue with evidence from other sources?</p><div className="actions"><button type="button" className="secondary" autoFocus onClick={() => setCfdeGate(null)}>Cancel</button><button type="button" className="confirm-continue" onClick={() => { const saved = cfdeGate; forgetCfdePassed(saved.id); setCfdePassed(false); setCfdeGate(null); void continueAfterCfde(saved); }}>Continue</button></div></section></div>}
  </>;
}

type ResultInspect = { title: string; data: unknown; links: { href: string; label: string }[] };
type ResultAction = { key: string; label: string; inspect: ResultInspect };
type ResultRecord = { path: string; title: string; data?: Record<string, unknown>; error?: string };
function textValue(value: unknown): string { return typeof value === "string" ? value : ""; }
function paragraphText(data: Record<string, unknown>) {
  const document = data.document as { paragraphs?: { text?: unknown }[] } | undefined;
  return (document?.paragraphs || []).map(item => textValue(item.text)).filter(Boolean).join("\n\n");
}
function ParagraphView({ progress }: { progress?: ParagraphProgress }) {
  const paragraphJob = progress?.job;
  const writing = !!paragraphJob && !terminal(paragraphJob.status);
  const text = progress?.text || "";
  if (!writing && !text && !progress?.error && !paragraphJob?.failure && paragraphJob?.status !== "cancelled") return null;
  return <>
    {writing && <p className="paragraph-status" role="status">Writing the cited claim…</p>}
    {progress?.error && <p role="alert" className="error-text">{progress.error}</p>}
    {paragraphJob?.failure && <p role="alert" className="error-text">{paragraphJob.failure.message}</p>}
    {paragraphJob?.status === "cancelled" && <p className="paragraph-status">Cited claim writing was stopped.</p>}
    {text && <p className="research-text">{text}</p>}
  </>;
}
function ResultView({ job, principalKind, onActions }: { job: Job; principalKind?: Me["principal_kind"]; onActions: (actions: ResultAction[]) => void }) {
  const [records, setRecords] = useState<ResultRecord[]>([]);
  const result = job.result;
  useEffect(() => {
    if (!result) return;
    let active = true;
    const paths = result.kind === "analysis" ? result.account_ids.map(id => ({ path: "accounts/" + encodeURIComponent(id), title: "Scientific account" }))
      : result.kind === "paragraph" ? [{ path: "paragraphs/" + encodeURIComponent(result.paragraph_id), title: "Cited claim" }]
      : [{ path: "analysis-outcomes/" + encodeURIComponent(result.outcome_id), title: "Insufficient evidence" }];
    setRecords(paths);
    void Promise.all(paths.map(async record => {
      try { return { ...record, data: await request<Record<string, unknown>>(backend(record.path)) }; }
      catch (error) { return { ...record, error: errorMessage(error) }; }
    })).then(values => { if (active) setRecords(values); });
    return () => { active = false; };
  }, [job.id, JSON.stringify(result)]);
  const actions = useMemo(() => {
    if (!result) return [];
    const items: ResultAction[] = [];
    records.forEach((record, index) => {
      if (!record.data) return;
      const document = record.data.document as Record<string, Record<string, unknown>[]> | undefined;
      const object = document?.scientific_accounts?.[0];
      const artifacts = (record.data.artifacts || []) as Schema<"ArtifactAccess">[];
      items.push({ key: "inspect-" + record.path, label: "Inspect result", inspect: { title: textValue(object?.name) || record.title, data: record.data, links: [{ href: backend(record.path), label: "Open saved JSON" }, ...(result.kind === "paragraph" ? [{ href: backend("paragraphs/" + encodeURIComponent(result.paragraph_id) + "/export?format=markdown"), label: "Export cited claim Markdown" }] : []), ...artifacts.filter(artifact => artifact.availability === "available" && /^[a-f0-9]{64}$/.test(artifact.file.sha256 || "")).map(artifact => ({ href: backend("artifacts/" + artifact.file.sha256), label: artifact.file.filename || artifact.file.name || "Evidence artifact" }))] } });
      if (index === records.findIndex(item => item.data)) items.push({ key: "job-record", label: "Job record and evidence", inspect: { title: "Job record and evidence", data: job, links: [{ href: backend("jobs/" + job.id), label: "Job JSON" }, { href: backend("jobs/" + job.id + "/evidence-package"), label: "Frozen evidence package" }] } });
    });
    return items;
  }, [records, job, result]);
  useEffect(() => { onActions(actions); }, [actions, onActions]);
  useEffect(() => () => onActions([]), [onActions]);
  if (!result) return null;
  return <section className="results">{result.kind === "analysis" && records.map(record => record.data ? <AccountGraph data={record.data} key={record.path} /> : null)}<h3>{result.kind === "analysis_outcome" ? "Investigation outcome" : "Scientific account"}</h3>
    {principalKind === "anonymous" && result.kind === "analysis" && <p className="muted">Guest results are private to this workspace. Publishing requires a registered account.</p>}
    {records.map(record => {
      const document = record.data?.document as Record<string, Record<string, unknown>[]> | undefined;
      const object = document?.scientific_accounts?.[0];
      const paragraphs = document?.paragraphs || [];
      return <article className="result-record" key={record.path}><h4>{textValue(object?.name) || record.title}</h4>{record.error ? <p role="alert" className="error-text">{record.error} <a href={backend(record.path)} target="_blank" rel="noreferrer">Open result</a></p> : !record.data ? <p className="muted">Loading saved result…</p> : <>
        {result.kind === "analysis_outcome" && <><p>{textValue(record.data.summary)}</p><p>{textValue(record.data.reason)}</p>
          {([['missing_evidence', 'Missing evidence'], ['limitations', 'Limitations'], ['next_steps', 'Possible next steps']] as const).map(([field, title]) => Array.isArray(record.data![field]) && (record.data![field] as unknown[]).length > 0 ? <div className="outcome-section" key={field}><strong>{title}</strong><ul>{(record.data![field] as unknown[]).map((value, index) => <li key={index}>{textValue(value)}</li>)}</ul></div> : null)}
          {textValue(record.data.scope_note) && <p className="scope-note">{textValue(record.data.scope_note)}</p>}
        </>}
        {object?.closing_remarks && <p>{textValue(object.closing_remarks)}</p>}
        {paragraphs.map((paragraph, index) => <p className="research-text" key={textValue(paragraph.id) || index}>{textValue(paragraph.text)}</p>)}
      </>}</article>;
    })}
  </section>;
}
