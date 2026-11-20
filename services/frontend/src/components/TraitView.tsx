"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { messageOf, type Schema } from "@/lib/client";
import { factorApi } from "@/lib/factor-api";
import { factorHref, geneSetHref, traitPageHref } from "@/lib/factor-links";
import { publicSourceHref } from "@/lib/provenance-view";
import { appendRows, betaKey, betaSortLabels, gwasLinks, gwasReasons, metricDefinitions, metricValue, releaseLacks, type BetaSort } from "@/lib/trait-view";
import { ReferenceNavigation } from "./FactorView";
import { LoadingSurface } from "./LoadingSurface";
import { Record } from "./Scientific";
import { TraitLinks } from "./TraitLinks";
import "./factor-pages.css";

const count = (value: number) => value.toLocaleString();
const Metric = ({ value }: { value: number }) => <td title={String(value)}>{metricValue(value)}</td>;

export function TraitView({ id, from }: { id: string; from?: string }) {
  const [loaded, setLoaded] = useState<{ id: string; detail: Schema<"TraitDetail"> } | null>(null);
  const [failure, setFailure] = useState<{ id: string; message: string } | null>(null);
  const [attempt, retry] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setFailure(null);
    void factorApi.trait(id, controller.signal).then(detail => {
      if (controller.signal.aborted) return;
      if (detail.kpn_trait_id !== id) throw new Error("The trait record belongs to a different identifier. Reload the page.");
      setLoaded({ id, detail });
    }).catch(error => { if (!controller.signal.aborted) setFailure({ id, message: messageOf(error) }); });
    return () => controller.abort();
  }, [id, attempt]);
  const detail = loaded?.id === id ? loaded.detail : null;
  const error = failure?.id === id ? failure.message : "";
  return <main id="main" className="reference-page trait-page"><ReferenceNavigation from={from} />
    {detail ? <TraitContent key={id} detail={detail} self={traitPageHref(id, from) || `/traits/${encodeURIComponent(id)}`} /> : <LoadingSurface title={error ? "Couldn’t open this trait" : "Opening trait"} description="Retrieving its gene-set betas, fitted factors and GWAS source." error={error} onRetry={() => retry(n => n + 1)} skeleton="record" />}
  </main>;
}

function TraitContent({ detail, self }: { detail: Schema<"TraitDetail">; self: string }) {
  const kpn = publicSourceHref(detail.kpn_url), sets = detail.gene_set_libraries.reduce((total, library) => total + library.n_gene_sets, 0);
  const context = [detail.category, detail.trait_group, detail.trait_type].filter((value): value is string => !!value);
  return <>
    <header className="reference-heading"><p className="reference-kind">{detail.is_atlas ? "KPN atlas trait" : "KPN phenotype"}</p><h1>{detail.name}</h1>{detail.description && <p className="reference-description">{detail.description}</p>}
      <div className="reference-meta"><span>Phenotype {detail.phenotype}</span>{context.map(item => <span key={item}>{item}</span>)}{kpn && <a href={kpn} target="_blank" rel="noopener noreferrer">{detail.kpn_trait_id} ↗<span className="sr-only"> (KPN catalog, opens in a new tab)</span></a>}</div>
    </header>
    <GwasSource gwas={detail.gwas} />
    <nav className="reference-sections" aria-label="Trait sections"><a href="#trait-gene-sets">Gene-set betas<span>{count(sets)}</span></a><a href="#trait-factors">Fitted factors<span>{count(detail.factors.length)}</span></a><a href="#trait-linked-factors">Linked factors<span>{count(detail.linked_factors.n_factors)}</span></a><a href="#trait-record">Source record</a></nav>
    <div id="trait-gene-sets">{detail.gene_set_libraries.length ? <TraitGeneSets traitId={detail.kpn_trait_id} libraries={detail.gene_set_libraries} release={detail.release_id} self={self} /> : <section className="factor-loadings"><h2>Gene-set betas</h2><p className="loading-empty">No gene-set betas were fitted for this trait in this release. Betas are fitted for atlas traits only.</p></section>}</div>
    <section id="trait-factors" className="factor-loadings trait-factors"><h2>Fitted factors</h2>{detail.factors.length ? <><p className="loading-explanation">EAGGL factors fitted on this trait’s gene and gene-set evidence.</p><ul>{detail.factors.map(factor => <li key={factor.source_id}><Link href={factorHref(factor.source_id, null, { from: self })}>{factor.label}</Link><span>Factor {factor.factor_number}</span></li>)}</ul></> : <p className="loading-empty">No EAGGL factors were fitted on this trait. Factors fitted on other traits can still link to it.</p>}</section>
    <div id="trait-linked-factors"><TraitLinks traitId={detail.kpn_trait_id} release={detail.release_id} self={self} /></div>
    <section id="trait-record" className="reference-source"><h2>Source record</h2><dl className="reference-properties"><div><dt>Trait identity</dt><dd>{detail.kpn_trait_id}</dd></div>{detail.legacy_phenotype_id && <div><dt>Legacy phenotype</dt><dd>{detail.legacy_phenotype_id}</dd></div>}<div><dt>Reference release</dt><dd>{detail.release_id}</dd></div></dl>
      <details className="reference-raw"><summary>Trait record</summary><Record value={detail} /></details>
    </section>
  </>;
}

function GwasSource({ gwas }: { gwas: Schema<"TraitDetail">["gwas"] }) {
  const { note, listing, artifacts } = gwasLinks(gwas);
  if (!listing && !artifacts.length) return <p className="reference-notice trait-gwas">{note}</p>;
  return <section className="reference-notice trait-gwas" aria-label="GWAS source">{note && <p>{note}</p>}
    {listing && <p><a href={listing} target="_blank" rel="noopener noreferrer">{gwasReasons.available} ↗<span className="sr-only"> (opens in a new tab)</span></a></p>}
    {!!artifacts.length && <ul>{artifacts.map(artifact => <li key={artifact.id}>{artifact.href ? <a href={artifact.href} target="_blank" rel="noopener noreferrer">{artifact.name} ↗<span className="sr-only"> GWAS (opens in a new tab)</span></a> : artifact.name}{artifact.traced && <span className="link-flag">Traced in account lineage</span>}</li>)}</ul>}
  </section>;
}

/** A trait's gene-set betas (traitId), or a gene set's betas across traits (geneSetId), from the release's PIGEAN betas mode. */
export function TraitGeneSets({ traitId, geneSetId, libraries, release, self }: { traitId?: string; geneSetId?: string; libraries?: Schema<"TraitDetail">["gene_set_libraries"]; release?: string | null; self: string }) {
  const id = useId(), byTrait = traitId !== undefined;
  const [library, setLibrary] = useState(""), [sort, setSort] = useState<BetaSort>("beta_uncorrected");
  const [offset, setOffset] = useState(0), [attempt, retry] = useState(0);
  const binding = JSON.stringify([traitId, geneSetId, library, sort]);
  const requestKey = `${binding}:${offset}:${attempt}`;
  const [loaded, setLoaded] = useState<{ binding: string; result: Schema<"TraitGeneSets"> } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string; absent: boolean } | null>(null), [settled, setSettled] = useState("");
  const busy = settled !== requestKey, latest = useRef(loaded);
  latest.current = loaded;
  useEffect(() => {
    const controller = new AbortController(); setFailure(null);
    void factorApi.traitGeneSets({ ...(byTrait ? { trait_id: traitId } : { gene_set_id: geneSetId }), library: library || undefined, sort, offset, limit: 50 }, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      if (result.sort !== sort || result.trait_id !== (traitId ?? null) || result.gene_set_id !== (geneSetId ?? null) || result.library !== (library || null)) throw new Error("These betas belong to a different trait, gene set or order. Reload the page.");
      const previous = latest.current, append = offset > 0 && previous?.binding === binding;
      if ((release && result.release_id !== release) || (append && previous.result.release_id !== result.release_id)) throw new Error("The reference release changed while loading. Reload the page.");
      setLoaded({ binding, result: { ...result, items: append ? appendRows(previous.result.items, result.items, betaKey) : result.items } });
    }).catch(error => { if (!controller.signal.aborted) setFailure({ key: requestKey, message: messageOf(error), absent: offset === 0 && releaseLacks(error) }); })
      .finally(() => { if (!controller.signal.aborted) setSettled(requestKey); });
    return () => controller.abort();
  }, [binding, requestKey, byTrait, traitId, geneSetId, release, library, sort, offset]);
  const result = loaded?.binding === binding ? loaded.result : null;
  const error = failure?.key === requestKey ? failure.message : "", absent = failure?.key === requestKey && failure.absent;
  const title = byTrait ? "Gene-set betas" : "Associated traits";
  if (absent && !result) return <section className="factor-loadings trait-gene-sets" aria-labelledby={`${id}-title`}><h2 id={`${id}-title`}>{title}</h2><p className="loading-empty">The published reference release does not include gene-set betas.</p></section>;
  return <section className="factor-loadings trait-gene-sets" aria-labelledby={`${id}-title`}>
    <div className="loading-heading"><div><h2 id={`${id}-title`}>{title}</h2><p>{result?.coverage || "Gene-set betas from the release’s PIGEAN betas mode, for the gene sets PIGEAN kept. An absent pair is not a zero beta."}</p></div></div>
    <div className="loading-tools">
      {byTrait && <label className="loading-metric">Library<select value={library} onChange={event => { setLibrary(event.target.value); setOffset(0); }}><option value="">All libraries</option>{libraries?.map(item => <option key={item.library} value={item.library}>{item.library} · {count(item.n_gene_sets)} gene sets</option>)}</select></label>}
      <label className="loading-sort">Order<select value={sort} onChange={event => { setSort(event.target.value as BetaSort); setOffset(0); }}>{(Object.keys(betaSortLabels) as BetaSort[]).map(value => <option key={value} value={value}>{betaSortLabels[value]}</option>)}</select></label>
    </div>
    <p className="loading-explanation">{metricDefinitions[sort]} Highest first; exact zeros are listed last. These are not the BioIndex small-model betas.</p>
    {!result ? <LoadingSurface compact title={error ? `Couldn’t load ${byTrait ? "gene-set betas" : "associated traits"}` : `Loading ${byTrait ? "gene-set betas" : "associated traits"}`} error={error} onRetry={() => retry(n => n + 1)} rows={2} /> : <>
      <p className="loading-result-count" role="status">{result.total ? `${count(result.items.length)} of ${count(result.total)} ${byTrait ? "gene sets" : "traits"}` : byTrait ? "No gene sets" : "No traits"}{result.total > 0 && <span>{betaSortLabels[sort]}</span>}</p>
      {!result.items.length ? <div className="loading-empty"><p>{byTrait ? library ? `PIGEAN kept no ${library} gene sets for this trait.` : "PIGEAN kept no gene sets for this trait." : "No trait has a PIGEAN beta for this gene set in this release. An absent pair is not a zero beta."}</p>{library && <button type="button" onClick={() => { setLibrary(""); setOffset(0); }}>Show all libraries</button>}</div>
        : <div className="loading-table-scroll" tabIndex={0} role="region" aria-label={`${title} table`}><table className="loading-table"><thead><tr>
          <th scope="col">{byTrait ? "Gene set" : "Trait"}</th><th scope="col" title={metricDefinitions.library_rank}>Rank in library</th><th scope="col" title={metricDefinitions.beta_uncorrected}>Uncorrected beta</th><th scope="col" title={metricDefinitions.beta}>Corrected beta</th><th scope="col" title={metricDefinitions.avg_postp}>Avg. posterior</th>{byTrait && <th scope="col">Genes</th>}
        </tr></thead><tbody>{result.items.map(row => { const trait = traitPageHref(row.kpn_trait_id, self); return <tr key={betaKey(row)}>
          <th scope="row">{byTrait ? <Link href={geneSetHref(row.gene_set_id, result.release_id, self)}>{row.gene_set_name}</Link> : trait ? <Link href={trait}>{row.trait_name}</Link> : row.trait_name}</th>
          <td>{row.library_rank} in {row.library}</td><Metric value={row.beta_uncorrected} /><Metric value={row.beta} /><Metric value={row.avg_postp} />{byTrait && <td>{count(row.n_genes)}</td>}
        </tr>; })}</tbody></table></div>}
      {error && <LoadingSurface compact title="Couldn’t load more" error={error} onRetry={() => retry(n => n + 1)} skeleton="none" />}
      {result.next_offset !== null && <div className="loading-append"><button type="button" disabled={busy} onClick={() => setOffset(result.next_offset!)}>{busy ? "Loading more…" : `Load more ${byTrait ? "gene sets" : "traits"}`}</button></div>}
    </>}
  </section>;
}
