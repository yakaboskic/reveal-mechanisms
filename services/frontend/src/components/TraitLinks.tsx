"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { messageOf, type Schema } from "@/lib/client";
import { factorApi } from "@/lib/factor-api";
import { factorHref, traitPageHref } from "@/lib/factor-links";
import { appendRows, linkKey, linkSortLabels, linkThreshold, metricDefinitions, metricValue, ownTraitStatus, phenotypeTarget, releaseLacks, type LinkScope, type LinkSort } from "@/lib/trait-view";
import { LoadingSurface } from "./LoadingSurface";

const count = (value: number) => value.toLocaleString();
const Metric = ({ value }: { value: number }) => <td title={String(value)}>{metricValue(value)}</td>;

/** A factor's linked phenotypes (sourceId), or the factors linked to a trait (traitId), from the release's eaggl trait linkage. */
export function TraitLinks({ sourceId, traitId, release, self, summary }: { sourceId?: string; traitId?: string; release?: string | null; self: string; summary?: Schema<"FactorDetail">["trait_links"] }) {
  const id = useId(), byFactor = sourceId !== undefined;
  const [scope, setScope] = useState<LinkScope>("all"), [sort, setSort] = useState<LinkSort>("phewas_q");
  const [offset, setOffset] = useState(0), [attempt, retry] = useState(0);
  const binding = JSON.stringify([sourceId, traitId, scope, sort]);
  const requestKey = `${binding}:${offset}:${attempt}`;
  const [loaded, setLoaded] = useState<{ binding: string; result: Schema<"FactorTraitLinks"> } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string; absent: boolean } | null>(null), [settled, setSettled] = useState("");
  const busy = settled !== requestKey, latest = useRef(loaded);
  latest.current = loaded;
  useEffect(() => {
    const controller = new AbortController(); setFailure(null);
    void factorApi.traitLinks({ ...(byFactor ? { source_id: sourceId, scope } : { trait_id: traitId }), sort, offset, limit: 50 }, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      if (result.sort !== sort || result.source_id !== (sourceId ?? null) || result.trait_id !== (traitId ?? null) || (byFactor && result.scope !== scope)) throw new Error("These links belong to a different factor, trait or order. Reload the page.");
      const previous = latest.current, append = offset > 0 && previous?.binding === binding;
      if ((release && result.release_id !== release) || (append && previous.result.release_id !== result.release_id)) throw new Error("The reference release changed while loading. Reload the page.");
      setLoaded({ binding, result: { ...result, items: append ? appendRows(previous.result.items, result.items, linkKey) : result.items } });
    }).catch(error => { if (!controller.signal.aborted) setFailure({ key: requestKey, message: messageOf(error), absent: offset === 0 && releaseLacks(error) }); })
      .finally(() => { if (!controller.signal.aborted) setSettled(requestKey); });
    return () => controller.abort();
  }, [binding, requestKey, byFactor, sourceId, traitId, release, scope, sort, offset]);
  const result = loaded?.binding === binding ? loaded.result : null;
  const error = failure?.key === requestKey ? failure.message : "", absent = failure?.key === requestKey && failure.absent;
  const facts = byFactor ? summary || result?.summary : null;
  const own = ownTraitStatus(facts);
  const reset = (change: () => void) => { change(); setOffset(0); };
  if (absent && !result) return <section className="factor-loadings trait-links" aria-labelledby={`${id}-title`}><h2 id={`${id}-title`}>{byFactor ? "Linked traits" : "Linked factors"}</h2><p className="loading-empty">The published reference release does not include factor-trait links{byFactor ? " for this factor" : ""}.</p></section>;
  return <section className="factor-loadings trait-links" aria-labelledby={`${id}-title`}>
    <div className="loading-heading"><div><h2 id={`${id}-title`}>{byFactor ? "Linked traits" : "Linked factors"}</h2><p>{result?.coverage || `Factor–phenotype links from eaggl trait linkage and factor-PheWAS, kept at Benjamini–Hochberg q ≤ ${linkThreshold}. An absent phenotype is unlinked at this threshold, not a negative result.`}</p></div>
      {byFactor && <div className="loading-view" role="group" aria-label="Phenotypes shown"><button type="button" aria-pressed={scope === "all"} onClick={() => reset(() => setScope("all"))}>All phenotypes</button><button type="button" aria-pressed={scope === "atlas"} onClick={() => reset(() => setScope("atlas"))}>Atlas traits</button></div>}</div>
    {facts && <p className="trait-link-summary">Linked to <b>{count(facts.n_linked)}</b> of {count(facts.n_phenotypes)} phenotypes tested: {count(facts.n_linked_kpn_traits)} other KPN traits, {count(facts.n_linked_atlas_traits)} of them atlas traits.{own && <span className={facts.own_trait_q != null && facts.own_trait_q <= linkThreshold ? "link-flag" : "link-flag is-muted"}>{own}</span>}</p>}
    {!byFactor && <p className="loading-explanation">Factors fitted on other traits whose factor-PheWAS links them to this trait. Factors fitted on this trait are flagged.</p>}
    <div className="loading-tools"><label className="loading-sort">Order<select value={sort} onChange={event => reset(() => setSort(event.target.value as LinkSort))}>{(Object.keys(linkSortLabels) as LinkSort[]).map(value => <option key={value} value={value}>{linkSortLabels[value]}</option>)}</select></label></div>
    {!result ? <LoadingSurface compact title={error ? `Couldn’t load linked ${byFactor ? "traits" : "factors"}` : `Loading linked ${byFactor ? "traits" : "factors"}`} error={error} onRetry={() => retry(n => n + 1)} rows={2} /> : <>
      <p className="loading-result-count" role="status">{result.total ? `${count(result.items.length)} of ${count(result.total)} links` : "No links"}{result.total > 0 && <span>{linkSortLabels[sort]}</span>}</p>
      {!result.items.length ? <div className="loading-empty"><p>{byFactor ? scope === "atlas" ? `No atlas trait is linked to this factor at q ≤ ${linkThreshold}.` : `This factor is not linked to any phenotype at q ≤ ${linkThreshold}. That is not evidence that it is unrelated to them.` : `No factor is linked to this trait at q ≤ ${linkThreshold}.`}</p>{byFactor && scope === "atlas" && <button type="button" onClick={() => reset(() => setScope("all"))}>Show all phenotypes</button>}</div>
        : <div className="loading-table-scroll" tabIndex={0} role="region" aria-label={byFactor ? "Linked traits table" : "Linked factors table"}><table className="loading-table"><thead><tr>
          {byFactor ? <th scope="col">Phenotype</th> : <><th scope="col">Factor</th><th scope="col">Fitted on</th></>}
          <th scope="col" title={metricDefinitions.phewas_q}>PheWAS q</th><th scope="col" title={metricDefinitions.phewas_beta}>PheWAS beta</th><th scope="col" title={metricDefinitions.phewas_p_onesided}>One-sided p</th><th scope="col" title={metricDefinitions.nnls_loading}>NNLS loading</th>
        </tr></thead><tbody>{result.items.map(row => <tr key={linkKey(row)}>
          {byFactor ? <th scope="row"><Phenotype row={row} self={self} /></th> : <><th scope="row"><Link href={factorHref(row.source_id, null, { from: self })}>{row.factor_label}</Link>{row.is_own_trait && <span className="link-flag">Fitted on this trait</span>}</th><td><TraitName id={row.fitted_kpn_trait_id} name={row.fitted_trait_name} self={self} /></td></>}
          <Metric value={row.phewas_q} /><Metric value={row.phewas_beta} /><Metric value={row.phewas_p_onesided} /><Metric value={row.nnls_loading} />
        </tr>)}</tbody></table></div>}
      {error && <LoadingSurface compact title="Couldn’t load more" error={error} onRetry={() => retry(n => n + 1)} skeleton="none" />}
      {result.next_offset !== null && <div className="loading-append"><button type="button" disabled={busy} onClick={() => setOffset(result.next_offset!)}>{busy ? "Loading more…" : "Load more links"}</button></div>}
    </>}
  </section>;
}

function Phenotype({ row, self }: { row: Schema<"FactorTraitLink">; self: string }) {
  const target = phenotypeTarget(row, self);
  return <>{target.page ? <Link href={target.page}>{row.phenotype_name}</Link> : <span title={row.phenotype}>{row.phenotype_name}</span>}
    {row.is_own_trait && <span className="link-flag">Own trait</span>}
    {!target.page && target.catalog && <a className="link-secondary" href={target.catalog} target="_blank" rel="noopener noreferrer">{row.phenotype_kpn_trait_id} ↗<span className="sr-only"> (KPN catalog, opens in a new tab)</span></a>}</>;
}

function TraitName({ id, name, self }: { id: string; name: string; self: string }) {
  const href = traitPageHref(id, self);
  return href ? <Link href={href}>{name}</Link> : <>{name}</>;
}
