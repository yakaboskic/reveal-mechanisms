"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { messageOf, type Schema } from "@/lib/client";
import { factorApi } from "@/lib/factor-api";
import { geneSetHref } from "@/lib/factor-links";
import { appendLoadings, overlayMembers, prefetchedLoadings } from "@/lib/factor-explorer";
import { constraintCell, constraintDefinitions, constraintLinks, constraintSelection, constraintValue, loadingSortLabels, type LoadingSort } from "@/lib/gnomad-display";
import { LoadingSurface } from "./LoadingSurface";
import { LoadingHeatmap } from "./LoadingHeatmap";

type Kind = "gene" | "gene_set";
type OverlayChoice = { id: string; label: string };
type Overlay = { label: string; symbols: Set<string>; unsupported: number };
const count = (value: number) => value.toLocaleString();
const valueLabel = (value: number | null | undefined) => value == null ? "Not reported" : String(value);

export function FactorExplorer({ detail, self, genes }: { detail: Schema<"FactorDetail">; self: string; genes?: Promise<Schema<"FactorLoadings"> | null> }) {
  const id = useId(), [tab, setTab] = useState<Kind>("gene"), [visitedSets, setVisitedSets] = useState(false);
  const [chooser, setChooser] = useState(false), [choice, setChoice] = useState<OverlayChoice | null>(null);
  const [options, setOptions] = useState<Schema<"FactorLoading">[] | null>(null), [optionsError, setOptionsError] = useState("");
  const [optionsAttempt, retryOptions] = useState(0), [memberAttempt, retryMembers] = useState(0);
  const [members, setMembers] = useState<{ key: string; overlay: Overlay | null } | null>(null);
  const [memberFailure, setMemberFailure] = useState<{ key: string; message: string } | null>(null);
  const selectionKey = `${detail.generation_id}:${choice?.id || ""}`;
  const selected = members?.key === selectionKey ? members : null;
  const memberError = memberFailure?.key === selectionKey ? memberFailure.message : "";
  const overlay = choice ? selected?.overlay || null : null;
  useEffect(() => {
    if (!chooser) return;
    const controller = new AbortController(); setOptionsError("");
    void (async () => {
      let offset = 0, items: Schema<"FactorLoading">[] = [];
      for (;;) {
        const result = await factorApi.loadings({ source_id: detail.factor.source_id, source_revision: detail.factor.source_revision,
          generation_id: detail.generation_id || undefined, kind: "gene_set", metric: "joint", sort: "alphabetical", offset, limit: 500 }, controller.signal);
        if (controller.signal.aborted) return;
        if (result.source_id !== detail.factor.source_id || result.generation_id !== detail.generation_id) throw new Error("The gene sets belong to a different reference. Reload the factor page.");
        items = appendLoadings(items, result.items);
        if (result.next_offset == null) { setOptions(items); return; }
        if (result.next_offset <= offset) throw new Error("The next gene-set page is unavailable. Please retry.");
        offset = result.next_offset;
      }
    })().catch(error => { if (!controller.signal.aborted) setOptionsError(messageOf(error)); });
    return () => controller.abort();
  }, [chooser, detail, optionsAttempt]);
  useEffect(() => {
    if (!choice) return;
    const controller = new AbortController(); setMemberFailure(null);
    void factorApi.geneSet(choice.id, detail.generation_id || undefined, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      if (result.id !== choice.id || result.generation_id !== detail.generation_id) throw new Error("The gene set belongs to a different reference. Reload the factor page.");
      const membership = overlayMembers(result.object);
      setMembers({ key: selectionKey, overlay: membership ? { label: result.name, ...membership } : null });
    }).catch(error => { if (!controller.signal.aborted) setMemberFailure({ key: selectionKey, message: messageOf(error) }); });
    return () => controller.abort();
  }, [choice, detail.generation_id, selectionKey, memberAttempt]);
  const changeTab = (value: Kind) => { if (value === "gene_set") setVisitedSets(true); setTab(value); };
  const applyOverlay = (value: OverlayChoice) => { setChoice(value); setChooser(true); changeTab("gene"); };
  const clearOverlay = () => { setChoice(null); setMembers(null); setMemberFailure(null); };
  const tabs = [{ kind: "gene" as const, label: "Gene loadings", total: detail.genes.total }, { kind: "gene_set" as const, label: "Gene-set loadings", total: detail.gene_sets.total }];
  return <div className="factor-explorer">
    <div className="reading-tabs factor-loading-tabs" role="tablist" aria-label="Factor loadings">{tabs.map((item, index) => <button type="button" key={item.kind} id={`${id}-${item.kind}-tab`} role="tab" aria-selected={tab === item.kind} aria-controls={`${id}-${item.kind}-panel`} tabIndex={tab === item.kind ? 0 : -1} onClick={() => changeTab(item.kind)} onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : 1 - index;
      changeTab(tabs[next].kind); document.getElementById(`${id}-${tabs[next].kind}-tab`)?.focus();
    }}>{item.label}<span>{count(item.total)}</span></button>)}</div>
    <div id={`${id}-gene-panel`} role="tabpanel" aria-labelledby={`${id}-gene-tab`} hidden={tab !== "gene"}>
      <LoadingPanel detail={detail} kind="gene" self={self} overlay={overlay} initial={genes}>
        {!chooser ? <button type="button" className="overlay-add" onClick={() => setChooser(true)}>＋ Overlay a gene set</button> : <div className="factor-overlay">
          <div className="overlay-tools"><label htmlFor={`${id}-overlay`}>Overlay gene set</label><select id={`${id}-overlay`} value={choice?.id || ""} disabled={!options && !choice} onChange={event => {
            const item = options?.find(row => row.gene_set_id === event.target.value);
            if (item?.gene_set_id) setChoice({ id: item.gene_set_id, label: item.label }); else clearOverlay();
          }}><option value="">{options ? "Choose a gene set…" : "Loading gene sets…"}</option>{choice && !options?.some(row => row.gene_set_id === choice.id) && <option value={choice.id}>{choice.label}</option>}{options?.filter(row => row.gene_set_id).map(row => <option key={row.id} value={row.gene_set_id!}>{row.label}</option>)}</select>
            {choice ? <button type="button" onClick={clearOverlay}>Clear overlay</button> : <button type="button" onClick={() => setChooser(false)}>Close</button>}
          </div>
          {optionsError && <p role="alert">{optionsError} <button type="button" onClick={() => retryOptions(n => n + 1)}>Retry gene sets</button></p>}
          {choice && <div className="overlay-status" role="status">{memberError ? <>{memberError} <button type="button" onClick={() => retryMembers(n => n + 1)}>Retry membership</button></> : !selected ? "Loading gene-set membership…" : !overlay ? "Member genes were not recorded for this gene set. An overlay is unavailable." : <>{count(overlay.symbols.size)} recorded gene symbols. Only members present in this factor’s stored gene loadings can be highlighted.{overlay.unsupported > 0 && <> {count(overlay.unsupported)} member identifiers cannot be matched to gene symbols; other tiles have unknown membership.</>}</>}</div>}
          {choice && <Link className="overlay-provenance" href={geneSetHref(choice.id, detail.generation_id, self)}>View gene-set provenance ↗</Link>}
        </div>}
      </LoadingPanel>
    </div>
    <div id={`${id}-gene_set-panel`} role="tabpanel" aria-labelledby={`${id}-gene_set-tab`} hidden={tab !== "gene_set"}>{visitedSets && <LoadingPanel detail={detail} kind="gene_set" self={self} onOverlay={applyOverlay} />}</div>
  </div>;
}

function LoadingPanel({ detail, kind, self, overlay, onOverlay, initial, children }: { detail: Schema<"FactorDetail">; kind: Kind; self: string; overlay?: Overlay | null; onOverlay?: (choice: OverlayChoice) => void; initial?: Promise<Schema<"FactorLoadings"> | null>; children?: React.ReactNode }) {
  const id = useId(), isGene = kind === "gene", title = isGene ? "Gene loadings" : "Gene-set loadings";
  const [search, setSearch] = useState(""), [q, setQuery] = useState("");
  const [metric, setMetric] = useState<"joint" | "marginal">("joint"), [sort, setSort] = useState<LoadingSort>("alphabetical");
  const [offset, setOffset] = useState(0), [view, setView] = useState("heatmap"), [attempt, retry] = useState(0);
  const gnomadImportId = isGene ? detail.gnomad?.import_id || "none" : undefined;
  const binding = JSON.stringify([detail.factor.source_id, detail.factor.source_revision, detail.generation_id, gnomadImportId, kind, q, metric, sort]);
  const requestKey = `${binding}:${offset}:${attempt}`;
  const [loaded, setLoaded] = useState<{ binding: string; result: Schema<"FactorLoadings"> } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null), [settled, setSettled] = useState("");
  const busy = settled !== requestKey, first = useRef(requestKey);
  useEffect(() => { const timer = setTimeout(() => { setQuery(search.trim()); setOffset(0); }, 250); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    const controller = new AbortController(); setFailure(null);
    const limit = isGene ? 200 : 50;
    const pinned = () => factorApi.loadings({ source_id: detail.factor.source_id, source_revision: detail.factor.source_revision, generation_id: detail.generation_id || undefined, gnomad_import_id: gnomadImportId, kind, metric, sort, q, offset, limit }, controller.signal);
    void (initial && requestKey === first.current
      ? initial.then(value => prefetchedLoadings(value, { source_id: detail.factor.source_id, generation_id: detail.generation_id, gnomad_import_id: gnomadImportId, kind, metric, sort, q, offset, limit }) || pinned())
      : pinned())
      .then(result => {
        if (controller.signal.aborted) return;
        if (result.source_id !== detail.factor.source_id || result.generation_id !== detail.generation_id || result.kind !== kind || result.metric !== metric || result.sort !== sort) throw new Error("These loadings belong to a different reference or order. Reload the factor page.");
        if (isGene && (result.gnomad?.import_id || "none") !== gnomadImportId) throw new Error("The gnomAD annotation import changed. Reload the factor page to use the new annotations.");
        setLoaded(previous => ({ binding, result: { ...result, items: offset && previous?.binding === binding ? appendLoadings(previous.result.items, result.items) : result.items } }));
      }).catch(error => { if (!controller.signal.aborted) setFailure({ key: requestKey, message: messageOf(error) }); })
      .finally(() => { if (!controller.signal.aborted) setSettled(requestKey); });
    return () => controller.abort();
  }, [binding, requestKey, detail, kind, isGene, gnomadImportId, metric, sort, q, offset, initial]);
  const result = loaded?.binding === binding ? loaded.result : null;
  const error = failure?.key === requestKey ? failure.message : "";
  const summary = result?.summary || (isGene ? detail.genes : detail.gene_sets);
  const cellHref = (row: Schema<"FactorLoading">) => row.gene_set_id ? geneSetHref(row.gene_set_id, detail.generation_id, self) : undefined;
  const membership = (row: Schema<"FactorLoading">) => !isGene || !overlay ? undefined : overlay.symbols.has(row.label) ? true : overlay.unsupported ? undefined : false;
  const selectOverlay = (row: Schema<"FactorLoading">) => { if (row.gene_set_id) onOverlay?.({ id: row.gene_set_id, label: row.label }); };
  return <section id={isGene ? "gene-loadings" : "gene-set-loadings"} className="factor-loadings" aria-labelledby={`${id}-title`}>
    <div className="loading-heading"><div><h2 id={`${id}-title`}>{title}</h2><p>{summary.coverage}</p></div><div className="loading-view" role="group" aria-label={`${title} view`}><button type="button" aria-pressed={view === "heatmap"} onClick={() => setView("heatmap")}>Heatmap</button><button type="button" aria-pressed={view === "table"} onClick={() => setView("table")}>Table</button></div></div>
    <div className="loading-tools"><label className="loading-search" htmlFor={`${id}-search`}><svg width="17" height="17" viewBox="0 0 20 20" fill="none" stroke="currentColor" aria-hidden="true"><circle cx="8" cy="8" r="5.5" /><path d="m12 12 5 5" /></svg><span className="sr-only">Search {isGene ? "gene" : "gene-set"} loadings</span><input id={`${id}-search`} type="search" placeholder={isGene ? "Search gene symbols…" : "Search gene sets or libraries…"} maxLength={200} value={search} onChange={event => setSearch(event.target.value)} /></label>
      {!isGene && <label className="loading-metric">Loading<select value={metric} onChange={event => { setMetric(event.target.value as "joint" | "marginal"); setOffset(0); }}><option value="joint">Joint</option><option value="marginal">Marginal</option></select></label>}
      <label className="loading-sort">Order<select value={sort} onChange={event => { setSort(event.target.value as LoadingSort); setOffset(0); }}>{(Object.keys(loadingSortLabels) as LoadingSort[]).filter(value => isGene || !value.startsWith("gnomad_")).map(value => <option key={value} value={value} disabled={value.startsWith("gnomad_") && !detail.gnomad}>{loadingSortLabels[value]}</option>)}</select></label>
    </div>
    {!isGene && <p className="loading-explanation">{metric === "joint" ? "Joint loadings fit this trait’s factors together to explain each gene set." : "Marginal loadings fit each factor separately to each gene set."} Values are specific to this factor and metric.</p>}
    {children}
    {!result ? <LoadingSurface compact title={error ? `Couldn’t load ${isGene ? "genes" : "gene sets"}` : `Loading ${isGene ? "genes" : "gene sets"}`} error={error} onRetry={() => retry(n => n + 1)} rows={2} /> : <>
      <p className="loading-result-count" role="status">{result.total ? `${count(result.items.length)} of ${count(result.total)}${q ? " matches" : ` ${isGene ? "genes" : "gene sets"}`}` : q ? "No matching loadings" : "No loadings available"}{result.total > 0 && <span>{loadingSortLabels[sort]}</span>}</p>
      {!result.items.length ? <div className="loading-empty"><p>{q ? `No ${isGene ? "gene symbols" : "gene sets or libraries"} match “${q}”.` : "This reference does not include loadings for this factor."}</p>{q && <button type="button" onClick={() => setSearch("")}>Clear search</button>}</div> : view === "heatmap" ? <LoadingHeatmap label={title} min={summary.min} max={summary.max} orderingLabel={loadingSortLabels[sort]} overlayLabel={overlay?.label} items={result.items.map(row => ({ id: row.id, label: row.label, loading: row.loading, rank: row.rank, description: row.library || undefined, href: cellHref(row), isMember: membership(row), ...(isGene ? constraintCell(row.gnomad, detail.gnomad) : {}) }))} actionLabel={!isGene && onOverlay ? "Overlay on genes" : undefined} onAction={!isGene && onOverlay ? item => { const row = result.items.find(value => value.id === item.id); if (row) selectOverlay(row); } : undefined} /> : <div className="loading-table-scroll" tabIndex={0} role="region" aria-label={`${title} table`}><table className="loading-table"><thead><tr><th scope="col">Rank</th><th scope="col">{isGene ? "Gene" : "Gene set"}</th>{!isGene && <th scope="col">Library</th>}<th scope="col">{isGene ? "Loading" : `${metric === "joint" ? "Joint" : "Marginal"} loading`}</th>{isGene && detail.gnomad && <><th scope="col" title={constraintDefinitions.pli}>pLI</th><th scope="col" title={constraintDefinitions.loeuf}>LOEUF</th><th scope="col" title={constraintDefinitions.mis_z}>Missense Z</th><th scope="col">gnomAD provenance</th></>}{(overlay || !isGene) && <th scope="col">{isGene ? "Gene-set member" : "Explore"}</th>}</tr></thead><tbody>{result.items.map(row => <tr key={row.id} className={membership(row) ? "loading-table-member" : undefined}><td>{row.rank}</td><th scope="row">{cellHref(row) ? <Link href={cellHref(row)!}>{row.label}</Link> : row.label}</th>{!isGene && <td>{row.library || "Not reported"}</td>}<td>{valueLabel(row.loading)}</td>{isGene && detail.gnomad && <><td>{constraintValue(row.gnomad, "pli")}</td><td>{constraintValue(row.gnomad, "loeuf")}</td><td>{constraintValue(row.gnomad, "mis_z")}</td><td className="gnomad-provenance-cell"><ConstraintProvenance annotation={row.gnomad} source={detail.gnomad} /></td></>}{isGene && overlay && <td>{membership(row) === true ? "Member" : membership(row) === false ? "Not a member" : "Unknown"}</td>}{!isGene && <td>{row.gene_set_id && <button type="button" className="loading-overlay-action" onClick={() => selectOverlay(row)}>Overlay on genes</button>}</td>}</tr>)}</tbody></table></div>}
      {error && <LoadingSurface compact title="Couldn’t load more" error={error} onRetry={() => retry(n => n + 1)} skeleton="none" />}
      {result.next_offset !== null && <div className="loading-append"><button type="button" disabled={busy} onClick={() => setOffset(result.next_offset!)}>{busy ? "Loading more…" : `Load more ${isGene ? "genes" : "gene sets"}`}</button></div>}
    </>}
  </section>;
}

function ConstraintProvenance({ annotation, source }: { annotation: Schema<"GnomadGeneConstraint"> | null | undefined; source: Schema<"GnomadImport"> }) {
  if (!annotation) return <span>No matching gene in this import</span>;
  return <details className="gnomad-gene-provenance"><summary>{constraintSelection(annotation)}{annotation.flags.length > 0 && " · flagged"}</summary>
    <p>gnomAD {source.version}</p>{annotation.transcript && <p>{annotation.transcript}</p>}
    <p>{annotation.selection_reason}</p><p>LoF observed/expected: {constraintValue(annotation, "lof_oe")}</p>
    {annotation.flags.length > 0 && <p>Source flags: {annotation.flags.join(", ")}</p>}
    {constraintLinks(annotation, source.version).map(link => <a key={link.href} href={link.href} target="_blank" rel="noreferrer">{link.label} ↗</a>)}
  </details>;
}
