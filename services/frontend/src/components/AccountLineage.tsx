"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, ApiError, messageOf } from "@/lib/client";
import { factorApi } from "@/lib/factor-api";
import { traitPageHref } from "@/lib/factor-links";
import { appendLineagePage, claimDatasets, gapLabel, issueCounts, issueLabel, lineageIndex, LineageChangedError, routeCounts, routeLabels, type Lineage } from "@/lib/account-lineage";
import { mechanismHref } from "@/lib/trait-view";
import { LoadingSurface } from "./LoadingSurface";
import { useIdentity } from "./Session";

export type AccountLineageState = { value: Lineage | null; error: string; busy: boolean; request: () => void; retry: () => void; more: () => void };
const count = (value: number) => value.toLocaleString();
const plural = (value: number, one: string, many = `${one}s`) => `${count(value)} ${value === 1 ? one : many}`;
const objectHref = (id: string) => `/id/${encodeURIComponent(id)}`;

/** The account's dataset lineage, traced once on first use (the trace may read the provenance portal) and shared by the
 * Data lineage tab and the claim inspector. Later pages add only witness paths; reuse and per-claim datasets come complete. */
export function useAccountLineage(id: string): AccountLineageState {
  const { me, ready } = useIdentity();
  const binding = `${id}:${me?.user_id || "visitor"}`;
  const [wanted, setWanted] = useState(""), [cursor, setCursor] = useState<{ binding: string; cursor: string } | null>(null), [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState<{ binding: string; value: Lineage } | null>(null);
  const [failure, setFailure] = useState<{ binding: string; message: string; expired: boolean } | null>(null), [settled, setSettled] = useState("");
  const latest = useRef(loaded); latest.current = loaded;
  const next = cursor?.binding === binding ? cursor.cursor : undefined, requestKey = `${binding}:${next || ""}:${attempt}`;
  useEffect(() => {
    if (!ready || wanted !== binding) return;
    const controller = new AbortController(); setFailure(null);
    void api.accountProvenance(id, next, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      if (value.account.record.id !== id) throw new Error("The lineage belongs to a different scientific account. Reload the page.");
      const previous = latest.current?.binding === binding ? latest.current.value : null;
      setLoaded({ binding, value: next && previous ? appendLineagePage(previous, value) : value });
    }).catch(error => { if (!controller.signal.aborted) setFailure({ binding, message: messageOf(error), expired: !!next && (error instanceof LineageChangedError || error instanceof ApiError && error.code === "CURSOR_EXPIRED") }); })
      .finally(() => { if (!controller.signal.aborted) setSettled(requestKey); });
    return () => controller.abort();
  }, [ready, wanted, binding, id, next, requestKey]);
  const value = ready && loaded?.binding === binding ? loaded.value : null;
  const error = ready && failure?.binding === binding ? failure.message : "";
  const request = useCallback(() => setWanted(binding), [binding]);
  return {
    value, error, busy: wanted === binding && settled !== requestKey, request,
    // An expired cursor starts again from the first page; any other failure retries the same page.
    retry: () => { if (failure?.expired) { setCursor(null); setLoaded(null); } setWanted(binding); setAttempt(n => n + 1); },
    more: () => { const page = value?.page.next_cursor; if (page) setCursor({ binding, cursor: page }); },
  };
}

/** A Mechanism id opens its factor page: current, an archived snapshot (410), or the raw record (404, as before). It is
 * resolved on first hover, focus or click, so listing Mechanisms costs no request; a failed lookup opens the raw record. */
export function MechanismLink({ id, from, className, title, children }: { id: string; from?: string | null; className?: string; title?: string; children: ReactNode }) {
  const router = useRouter(), mechanism = id.startsWith("dapper:Mechanism.");
  const [target, setTarget] = useState<{ id: string; href: string | null } | null>(null), pending = useRef<{ id: string; href: Promise<string | null> } | null>(null);
  const known = target?.id === id ? target : null;
  const resolve = () => {
    if (!mechanism || known) return Promise.resolve(known?.href || null);
    if (pending.current?.id !== id) pending.current = { id, href: factorApi.mechanismObject(id).then(value => value, error => error).then(outcome => {
      const href = mechanismHref(outcome, from);
      // Only a definitive answer is kept: a timeout or outage is tried again on the next hover or click.
      if (href || outcome instanceof ApiError && [404, 410].includes(outcome.status)) setTarget({ id, href });
      if (pending.current?.id === id) pending.current = null;
      return href;
    }) };
    return pending.current.href;
  };
  return <Link className={className} href={known?.href || objectHref(id)} title={title} onPointerEnter={() => void resolve()} onFocus={() => void resolve()} onClick={event => {
    if (!mechanism || known || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); void resolve().then(href => router.push(href || objectHref(id)));
  }}>{children}</Link>;
}

function Named({ id, index, from }: { id: string; index: ReturnType<typeof lineageIndex>; from: string }) {
  const href = index.href(id), trait = traitPageHref(id, from), name = index.name(id);
  if (trait) return <><Link href={trait}>{name}</Link>{href && <a className="link-secondary" href={href} target="_blank" rel="noopener noreferrer">{href.includes("/bottom_line/") ? "GWAS" : "KPN catalog"} ↗<span className="sr-only"> (opens in a new tab)</span></a>}</>;
  return href ? <a href={href} target="_blank" rel="noopener noreferrer" title={id}>{name} ↗<span className="sr-only"> (opens in a new tab)</span></a> : <span title={id}>{name}</span>;
}

export function AccountLineage({ lineage, accountId, claimHref }: { lineage: AccountLineageState; accountId: string; claimHref: (id: string) => string }) {
  const { value, error, request, retry } = lineage;
  useEffect(request, [request]);
  if (!value) return <LoadingSurface title={error ? "Data lineage is unavailable" : "Tracing data lineage"} description="Following each claim to the datasets and organizations behind it, including the published GWAS of the traits it reaches. This can take several seconds." error={error} onRetry={retry} rows={3} />;
  const index = lineageIndex(value), coverage = value.coverage, self = `/accounts/${encodeURIComponent(accountId)}?view=lineage`;
  const issues = issueCounts(coverage.issues), factors = coverage.retained_projection_coverage.flatMap(row => typeof row.mechanism_id === "string" ? [{ id: row.mechanism_id, retained: typeof row.retained_projection_count === "number" ? row.retained_projection_count : null }] : []);
  const claims = value.claims.filter(claim => claim.provenance.dataset_ids.length || claim.provenance.resolution !== "not_applicable");
  return <section className="account-lineage" aria-label="Data lineage">
    <p className="lineage-facts"><span><b>{count(coverage.resolved_claim_count)}</b> of {plural(coverage.claim_count, "claim")} traced</span><span><b>{count(value.dataset_reuse.length)}</b> {value.dataset_reuse.length === 1 ? "dataset" : "datasets"}</span><span><b>{count(value.organization_reuse.length)}</b> {value.organization_reuse.length === 1 ? "organization" : "organizations"}</span><span>{plural(coverage.trace_count, "lineage path")}</span></p>
    <p className={coverage.status === "complete" ? "small-note" : "notice"}>{coverage.status === "complete" ? "Every recorded reference was followed to its sources." : "Some lineage could not be resolved."}{coverage.counts_are_lower_bounds && " Counts are lower bounds: a dataset missing here is not evidence that it was not used."} Reuse measures lineage, not evidentiary strength.</p>
    {!!issues.length && <details className="technical-disclosure"><summary>Unresolved lineage <span>{plural(coverage.lineage_resolution.issue_count, "issue")}</span></summary><ul className="lineage-list">{issues.map(item => <li key={item.code}>{issueLabel(item.code)} <small>{plural(item.count, "occurrence")} · {item.code}</small></li>)}</ul></details>}
    {!!coverage.upstream_gaps?.length && <div className="lineage-gaps"><h3>Gaps in the upstream sources</h3><p className="small-note">Recorded absences in the sources themselves. They do not make the trace incomplete.</p><ul className="lineage-list">{coverage.upstream_gaps.map(gap => <li key={`${gap.route}:${gap.code}`}>{routeLabels[gap.route]}: {gapLabel(gap.code)}<small>{gap.object_ids.map((id, i) => <span key={id}>{i > 0 && ", "}<Named id={id} index={index} from={self} /></span>)}</small></li>)}</ul></div>}
    <h3>Datasets</h3>
    {value.dataset_reuse.length ? <div className="lineage-table-scroll" tabIndex={0} role="region" aria-label="Datasets reached"><table className="lineage-table"><thead><tr><th scope="col">Dataset</th><th scope="col">Claims</th><th scope="col" title="One claim can reach a dataset by several routes; route counts overlap and are not added.">Routes</th><th scope="col">Input roles</th></tr></thead><tbody>{value.dataset_reuse.map(row => <tr key={row.dataset_id}><th scope="row"><Named id={row.dataset_id} index={index} from={self} /></th><td>{count(row.claim_count)}</td><td>{routeCounts(row).map(route => <span className="lineage-route" key={route.route}>{route.label} · {count(route.count)}</span>)}</td><td>{row.input_roles.join(", ") || "Not recorded"}</td></tr>)}</tbody></table></div> : <p className="small-note">No claim reached a dataset{coverage.counts_are_lower_bounds ? " through the lineage that could be resolved" : ""}.</p>}
    <h3>Organizations</h3>
    {value.organization_reuse.length ? <div className="lineage-table-scroll" tabIndex={0} role="region" aria-label="Organizations reached"><table className="lineage-table"><thead><tr><th scope="col">Organization</th><th scope="col">Claims</th><th scope="col">Datasets</th><th scope="col">Claim–dataset pairs</th><th scope="col">Recorded roles</th></tr></thead><tbody>{value.organization_reuse.map(row => <tr key={row.organization_id}><th scope="row"><Named id={row.organization_id} index={index} from={self} /></th><td>{count(row.claim_count)}</td><td>{count(row.dataset_count)}</td><td>{count(row.claim_dataset_count)}</td><td>{Object.entries(row.roles).map(([role, counts]) => <span className="lineage-route" key={role}>{role.replaceAll("_", " ")} · {plural(counts.dataset_count, "dataset")}</span>)}</td></tr>)}</tbody></table></div> : <p className="small-note">The reached datasets name no organization.</p>}
    <h3>Datasets by claim</h3>
    {claims.length ? <ul className="lineage-claims">{claims.map(claim => <li key={claim.record.id}><Link href={claimHref(claim.record.id)}>{claim.record.statement || claim.record.id}</Link><ClaimDatasetList ids={claim.provenance.dataset_ids} resolution={claim.provenance.resolution} index={index} from={self} /></li>)}</ul> : <p className="small-note">No claim of this account cites traceable sources.</p>}
    {!!factors.length && <><h3>Factors</h3><p className="small-note">Factor lineage follows the retained gene-set projections and the fitted trait’s published GWAS; it is not the complete model construction history.</p><ul className="lineage-list">{factors.map(factor => <li key={factor.id}><MechanismLink id={factor.id} from={self}>{index.name(factor.id)}</MechanismLink>{factor.retained != null && <small>{plural(factor.retained, "retained projection")}</small>}</li>)}</ul></>}
    <LineagePaths lineage={lineage} index={index} />
    <details className="technical-disclosure"><summary>Trace snapshot</summary><dl className="scientific-keyval"><dt>Policy</dt><dd>{value.snapshot.policy_version}</dd><dt>Snapshot</dt><dd>{value.snapshot.id}</dd><dt>Releases</dt><dd>{value.snapshot.reference_generation_ids.join(", ") || "None"}</dd><dt>Portal reads</dt><dd>{value.snapshot.source_observations.filter(item => item.kind === "portal_bottom_line").length}</dd></dl></details>
  </section>;
}

function ClaimDatasetList({ ids, resolution, index, from }: { ids: string[]; resolution: string; index: ReturnType<typeof lineageIndex>; from: string }) {
  return <>{ids.length ? <ul className="lineage-list">{ids.map(id => <li key={id}><Named id={id} index={index} from={from} /></li>)}</ul> : <small>No dataset reached.</small>}{resolution === "incomplete" && <small>Some of this claim’s lineage could not be resolved; the list is a lower bound.</small>}</>;
}

/** The selected claim's datasets in the account inspector. */
export function ClaimDatasets({ lineage, claimId, accountId }: { lineage: AccountLineageState; claimId: string; accountId: string }) {
  const { value, error, request, retry } = lineage;
  useEffect(request, [request]);
  if (!value) return <LoadingSurface compact title={error ? "Data lineage is unavailable" : "Tracing data lineage"} description="Following this claim to its datasets." error={error} onRetry={retry} rows={2} />;
  const ids = claimDatasets(value, claimId), wrapper = value.claims.find(claim => claim.record.id === claimId);
  return <><h2>Datasets behind this claim</h2>{ids && wrapper ? <ClaimDatasetList ids={ids} resolution={wrapper.provenance.resolution} index={lineageIndex(value)} from={`/accounts/${encodeURIComponent(accountId)}?view=lineage`} /> : <p className="small-note">This claim is not part of the traced account.</p>}</>;
}

function LineagePaths({ lineage, index }: { lineage: AccountLineageState; index: ReturnType<typeof lineageIndex> }) {
  const { value, busy, error, more, retry } = lineage;
  if (!value?.traces.length) return null;
  return <details className="technical-disclosure"><summary>Lineage paths <span>{count(value.traces.length)} of {count(value.page.total)}</span></summary>
    <ul className="lineage-list">{value.traces.map(trace => <li key={trace.id}>{index.name(trace.origin_id)} → {index.name(trace.dataset_id)}<small>{trace.route_classifications.map(route => routeLabels[route]).join(" · ") || "Context reference"}{trace.direction ? ` · ${trace.direction.toLowerCase()}` : ""} · {plural(trace.path_steps.length, "step")}</small></li>)}</ul>
    {error && value.page.next_cursor && <p role="alert" className="small-note">{error} <button type="button" className="text-button" onClick={retry}>Retry</button></p>}
    {value.page.next_cursor && <button type="button" className="text-button" disabled={busy} onClick={more}>{busy ? "Loading more paths…" : "Load more paths"}</button>}
  </details>;
}
