"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { factorApi } from "@/lib/factor-api";
import { factorHref, traitHref, referenceReturnPath, returnLabel } from "@/lib/factor-links";
import { mechanismName, mechanismTrait } from "@/lib/mechanism-display";
import { LoadingSurface } from "./LoadingSurface";
import { LoadingHeatmap } from "./LoadingHeatmap";
import { Record } from "./Scientific";
import { FactorExplorer } from "./FactorExplorer";
import "./factor-pages.css";

export function ReferenceNavigation({ from }: { from?: string | null }) {
  const back = referenceReturnPath(from);
  return <nav className="page-topbar" aria-label="Reference navigation"><Link href={back || "/"}>← {returnLabel(back)}</Link><span>Reference catalog</span></nav>;
}

export function FactorView({ sourceId, revision, archiveId, from }: { sourceId: string; revision?: string; archiveId?: string; from?: string }) {
  const binding = JSON.stringify([sourceId, revision, archiveId]);
  const [loaded, setLoaded] = useState<{ binding: string; detail?: Schema<"FactorDetail">; archived?: Schema<"ArchivedReferenceFactor"> } | null>(null);
  const [failure, setFailure] = useState<{ binding: string; message: string } | null>(null);
  const [attempt, retry] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setFailure(null);
    const load = async () => {
      if (archiveId) {
        const archived = await api.referenceFactor(archiveId);
        if (archived.source_id !== sourceId) throw new Error("This archived record belongs to a different factor.");
        return { binding, archived };
      }
      try { return { binding, detail: await factorApi.detail(sourceId, revision, controller.signal) }; }
      catch (error) {
        const archived = error instanceof ApiError ? error.problem?.archived_reference_factor : null;
        if (archived?.source_id === sourceId) return { binding, archived };
        throw error;
      }
    };
    void load().then(value => { if (!controller.signal.aborted) setLoaded(value); }).catch(error => { if (!controller.signal.aborted) setFailure({ binding, message: messageOf(error) }); });
    return () => controller.abort();
  }, [binding, sourceId, revision, archiveId, attempt]);
  const result = loaded?.binding === binding ? loaded : null;
  const error = failure?.binding === binding ? failure.message : "";
  const detail = result?.detail;
  const self = factorHref(sourceId, detail?.factor.source_revision || revision, { archiveId, from });
  return <main id="main" className="reference-page factor-page"><ReferenceNavigation from={from} />
    {!result ? <LoadingSurface title={error ? "Couldn’t open this factor" : "Opening factor"} description="Retrieving its gene loadings, gene-set projections and source record." error={error} onRetry={() => retry(n => n + 1)} skeleton="record" /> : result.archived ? <ArchivedFactorView snapshot={result.archived} /> : detail && <>
      <header className="reference-heading"><p className="reference-kind">EAGGL factor</p><h1>{mechanismName(detail.factor)}</h1><p className="factor-trait">{mechanismTrait(detail.factor)}</p>
      </header>
      <details id="factor-source" className="factor-metadata"><summary>Factor details</summary>
        <div className="reference-meta"><span>{detail.provenance.factor_id}</span><span>{detail.factor.model}</span>{detail.factor.kpn_trait && (traitHref(detail.factor.kpn_trait.id) ? <a href={traitHref(detail.factor.kpn_trait.id)!} target="_blank" rel="noopener noreferrer">{detail.factor.kpn_trait.id} ↗</a> : <span>{detail.factor.kpn_trait.id}</span>)}</div>
        <p>These loadings describe this factor in the selected EAGGL model. A high loading is an association with the factor, not a causal conclusion.</p>
        <dl className="reference-properties"><div><dt>Source identity</dt><dd>{detail.factor.source_id}</dd></div><div><dt>Source revision</dt><dd>{detail.factor.source_revision}</dd></div>{detail.generation_id && <div><dt>Reference generation</dt><dd>{detail.generation_id}</dd></div>}</dl>
        <details className="reference-raw"><summary>Factor and provenance records</summary><Record value={detail.factor.object} /><Record value={detail.provenance} /><Record value={detail.factor.catalog_file} /></details>
      </details>
      <FactorExplorer key={binding} detail={detail} self={self} />
    </>}
  </main>;
}

function ArchivedFactorView({ snapshot }: { snapshot: Schema<"ArchivedReferenceFactor"> }) {
  const [q, setQuery] = useState("");
  const query = q.trim().toLocaleLowerCase();
  const genes = snapshot.top_genes.filter(gene => gene.symbol.toLocaleLowerCase().includes(query));
  const sets = snapshot.top_gene_sets.filter(set => `${set.name} ${set.library || ""}`.toLocaleLowerCase().includes(query));
  const setValues = snapshot.top_gene_sets.flatMap(set => set.joint_loading == null ? [] : [set.joint_loading]);
  return <><header className="reference-heading"><p className="reference-kind">Archived EAGGL factor</p><h1>{snapshot.label || snapshot.mechanism.name}</h1><p className="factor-trait">{snapshot.trait}</p></header>
    <p className="reference-notice">This reference has been replaced. These are the saved top loadings captured on {new Date(snapshot.captured_at).toLocaleDateString()}; they are not the current catalog.</p>
    <label className="archive-loading-search">Search saved loadings<input className="field" type="search" value={q} onChange={event => setQuery(event.target.value)} /></label>
    <section className="factor-loadings"><h2>Saved gene loadings</h2>{genes.length ? <LoadingHeatmap label="Saved gene loadings" min={0} max={Math.max(0, ...snapshot.top_genes.map(gene => gene.loading))} items={genes.map(gene => ({ id: gene.symbol, label: gene.symbol, loading: gene.loading, rank: snapshot.top_genes.indexOf(gene) + 1 }))} /> : <p className="loading-empty">{query ? "No saved gene loadings match your search." : "No gene loadings were captured."}</p>}</section>
    <section className="factor-loadings"><h2>Saved gene-set loadings</h2><p className="loading-explanation">Joint loadings where recorded. Missing values are not zero. Gene-set provenance from a retired generation may no longer be available.</p>{sets.length ? <LoadingHeatmap label="Saved gene-set loadings" min={setValues.length ? Math.min(...setValues) : null} max={setValues.length ? Math.max(...setValues) : null} items={sets.map(set => ({ id: `${set.rank}:${set.name}`, label: set.name, loading: set.joint_loading, rank: set.rank, description: set.library || undefined }))} /> : <p className="loading-empty">{query ? "No saved gene-set loadings match your search." : "No gene-set loadings were captured."}</p>}</section>
    <details className="reference-raw"><summary>Archived factor record</summary><Record value={snapshot} /></details>
  </>;
}
