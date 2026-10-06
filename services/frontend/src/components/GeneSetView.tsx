"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ApiError, messageOf, type Schema } from "@/lib/client";
import { factorApi } from "@/lib/factor-api";
import { geneSetHref } from "@/lib/factor-links";
import { publicSourceHref, provenanceAnchor, recordOf, recordsOf, stringsOf, textOf, type ProvenanceRecord } from "@/lib/provenance-view";
import { useIdentity } from "./Session";
import { ReferenceNavigation } from "./FactorView";
import { LoadingSurface } from "./LoadingSurface";
import { Record } from "./Scientific";
import "./factor-pages.css";

type GeneSetData = { catalog: Schema<"CatalogGeneSet">; saved?: never } | { saved: Schema<"GeneSetResult">; catalog?: never };

export function GeneSetView({ id, generation, from }: { id: string; generation?: string; from?: string }) {
  const { me, ready } = useIdentity();
  const binding = JSON.stringify([id, generation, me?.user_id || "visitor"]);
  const [loaded, setLoaded] = useState<{ binding: string; value: GeneSetData } | null>(null);
  const [failure, setFailure] = useState<{ binding: string; message: string } | null>(null);
  const [attempt, retry] = useState(0);
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController(); setFailure(null);
    const load = async (): Promise<GeneSetData> => {
      try { return { catalog: await factorApi.geneSet(id, generation, controller.signal) }; }
      catch (error) {
        // A pinned catalog lookup must never fall through to a different generation or account.
        if (generation || !(error instanceof ApiError) || error.status !== 404) throw error;
        return { saved: await factorApi.savedGeneSet(id, controller.signal) };
      }
    };
    void load().then(value => { if (!controller.signal.aborted) setLoaded({ binding, value }); }).catch(error => { if (!controller.signal.aborted) setFailure({ binding, message: messageOf(error) }); });
    return () => controller.abort();
  }, [id, generation, binding, ready, attempt]);
  const data = ready && loaded?.binding === binding ? loaded.value : null;
  const error = failure?.binding === binding ? failure.message : "";
  return <main id="main" className="reference-page gene-set-page"><ReferenceNavigation from={from} />
    {data ? <GeneSetContent key={binding} id={id} data={data} /> : <LoadingSurface title={error ? "Couldn’t open this gene set" : "Opening gene set"} description="Retrieving its membership, collection and original source records." error={error} onRetry={() => retry(n => n + 1)} skeleton="record" />}
  </main>;
}

function GeneSetContent({ id, data }: { id: string; data: GeneSetData }) {
  const catalog = data.catalog, saved = data.saved;
  const object = catalog?.object || saved?.document.gene_sets?.find(set => set.id === id) || {};
  const collection = catalog?.collection;
  const provenance = catalog?.provenance || saved?.document || {};
  const datasets = recordsOf(provenance.datasets), activities = recordsOf(provenance.activities), files = recordsOf(provenance.files), organizations = recordsOf(provenance.organizations);
  const named = [...datasets, ...activities, ...files, ...organizations, ...(collection?.object ? [collection.object] : [])];
  const names = new Map(named.flatMap(record => typeof record.id === "string" ? [[record.id, textOf(record.name) || record.id] as const] : []));
  const members = stringsOf(object.members);
  const [search, setSearch] = useState(""), [showAll, setShowAll] = useState(false);
  const matches = useMemo(() => members.filter(member => member.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())), [members, search]);
  const title = catalog?.name || textOf(object.name) || "Gene set";
  const memberCount = catalog ? catalog.gene_count : typeof object.n_members === "number" ? object.n_members : Array.isArray(object.members) ? members.length : null;
  const countLabel = memberCount == null ? "Gene count not reported" : `${memberCount.toLocaleString()} genes`;
  const context = [textOf(object.organism), textOf(object.assay), textOf(object.data_type), textOf(object.genome_build)].filter(Boolean);
  return <>
    <header className="reference-heading"><p className="reference-kind">Gene set{catalog?.library ? ` / ${catalog.library}` : ""}</p><h1>{title}</h1>{textOf(object.description) && <p className="reference-description">{textOf(object.description)}</p>}<div className="reference-meta"><span>{countLabel}</span>{catalog?.genes_in_universe != null && <span>{catalog.genes_in_universe.toLocaleString()} in the EAGGL universe</span>}{context.map((item, i) => <span key={i}>{item}</span>)}</div></header>
    {!!catalog?.limitations.length && <div className="reference-notice">{catalog.limitations.map(note => <p key={note}>{note}</p>)}</div>}
    <nav className="reference-sections" aria-label="Gene-set sections"><a href="#gene-set-provenance">Provenance</a><a href="#gene-set-members">Member genes{memberCount != null && <span>{memberCount.toLocaleString()}</span>}</a><a href="#gene-set-record">Source record</a></nav>
    <section id="gene-set-provenance" className="gene-set-provenance"><h2>Provenance</h2><p className="reference-description">{catalog ? "Source records supplied with this gene set and its collection." : "Source records retained with the saved scientific document."}</p>
      <dl className="reference-properties provenance-relations">
        {collection && <div><dt>Collection</dt><dd><a href="#gene-set-collection">{textOf(collection.object?.name) || collection.label}</a></dd></div>}
        {textOf(object.was_generated_by) && <div><dt>Created by</dt><dd><ProvenanceValue value={object.was_generated_by} names={names} /></dd></div>}
        {!!stringsOf(object.was_derived_from).length && <div><dt>Derived from</dt><dd><ProvenanceValue value={object.was_derived_from} names={names} /></dd></div>}
        {textOf(object.in_gmt_file) && <div><dt>Membership file</dt><dd><ProvenanceValue value={object.in_gmt_file} names={names} /></dd></div>}
        {textOf(object.gmt_entry) && <div><dt>Named row</dt><dd>{textOf(object.gmt_entry)}</dd></div>}
      </dl>
      {collection && <section id="gene-set-collection" className="provenance-collection"><h3 id={provenanceAnchor(collection.id)}>{textOf(collection.object?.name) || collection.label}</h3>{textOf(collection.object?.description) && <p>{textOf(collection.object?.description)}</p>}<dl className="reference-properties"><div><dt>Library</dt><dd>{collection.library}</dd></div><div><dt>Gene sets</dt><dd>{collection.gene_set_count.toLocaleString()}</dd></div>{collection.object?.was_generated_by != null && <div><dt>Created by</dt><dd><ProvenanceValue value={collection.object.was_generated_by} names={names} /></dd></div>}{collection.object?.was_derived_from != null && <div><dt>Derived from</dt><dd><ProvenanceValue value={collection.object.was_derived_from} names={names} /></dd></div>}</dl><details className="reference-raw"><summary>Collection record and checksum</summary><Record value={collection} /></details></section>}
      <ProvenanceGroup title="Source datasets" records={datasets} names={names} />
      <ProvenanceGroup title="Construction activities" records={activities} names={names} collapsible />
      <ProvenanceGroup title="Source files" records={files} names={names} collapsible />
      <ProvenanceGroup title="Organizations and attribution" records={organizations} names={names} collapsible />
      {!named.length && <p className="reference-notice">No upstream source records were included. The source record below shows the provenance fields that were retained.</p>}
    </section>
    <section id="gene-set-members" className="gene-set-members"><h2>Member genes</h2>{members.length ? <><label htmlFor="member-search" className="sr-only">Search member genes</label><input id="member-search" className="field" type="search" placeholder="Search member genes…" value={search} onChange={event => { setSearch(event.target.value); setShowAll(false); }} /><p className="loading-result-count" role="status">{matches.length.toLocaleString()} {search.trim() ? "matching" : "recorded"} genes{memberCount != null && memberCount > members.length ? ` of ${memberCount.toLocaleString()} reported` : ""}</p><ul className="member-genes">{(showAll ? matches : matches.slice(0, 100)).map((member, index) => <li key={`${member}:${index}`} title={member}>{member.replace(/^HGNC\.SYMBOL:/, "")}</li>)}</ul>{matches.length > 100 && <button className="reference-action" type="button" onClick={() => setShowAll(value => !value)}>{showAll ? "Show fewer genes" : `Show all ${matches.length.toLocaleString()} genes`}</button>}{!matches.length && <p className="loading-empty">No member genes match “{search}”.</p>}</> : <p className="reference-notice">{memberCount == null ? "The source does not report a member count or an explicit membership list." : `The source reports ${memberCount.toLocaleString()} genes, but does not include an explicit membership list in this record.`}</p>}</section>
    <section id="gene-set-record" className="reference-source"><h2>Source record</h2><dl className="reference-properties"><div><dt>Gene-set identity</dt><dd>{id}</dd></div>{catalog?.generation_id && <div><dt>Reference generation</dt><dd>{catalog.generation_id}</dd></div>}</dl>
      {!!stringsOf(object.was_derived_from).filter(value => value.startsWith("dapper:GeneSet.")).length && <p>Related gene sets: {stringsOf(object.was_derived_from).filter(value => value.startsWith("dapper:GeneSet.")).map(value => <Link key={value} href={geneSetHref(value)}>{names.get(value) || value}</Link>)}</p>}
      <details className="reference-raw"><summary>Gene-set record</summary><Record value={object} /></details>
      <details className="reference-raw"><summary>Complete imported provenance and metadata</summary><Record value={catalog ? { metadata: catalog.metadata, provenance: catalog.provenance } : saved} /></details>
    </section>
  </>;
}

function ProvenanceValue({ value, names }: { value: unknown; names: Map<string, string> }) {
  if (Array.isArray(value)) return <ul className="provenance-values">{value.map((item, index) => <li key={index}><ProvenanceValue value={item} names={names} /></li>)}</ul>;
  const text = textOf(value);
  if (!text) return <>{recordOf(value) ? <Record value={value} /> : "Not reported"}</>;
  if (names.has(text)) return <a href={`#${encodeURIComponent(provenanceAnchor(text))}`} onClick={() => { const target = document.getElementById(provenanceAnchor(text)); if (target instanceof HTMLDetailsElement) target.open = true; }}>{names.get(text)}</a>;
  const href = publicSourceHref(text);
  return href ? <a href={href} target="_blank" rel="noopener noreferrer">{text}<span className="sr-only"> (opens in a new tab)</span></a> : <>{text}</>;
}

const provenanceFields: [string, string][] = [
  ["location", "Source location"], ["url", "Website"], ["version", "Version"], ["publisher", "Publisher"], ["was_attributed_to", "Attributed to"],
  ["was_derived_from", "Derived from"], ["was_generated_by", "Created by"], ["software_name", "Software"], ["software_version", "Software version"],
  ["repo_url", "Source code"], ["code_version", "Code revision"], ["generated_at_time", "Created"], ["license", "License"], ["sha256", "SHA-256"], ["md5", "MD5"],
];

function ProvenanceGroup({ title, records, names, collapsible = false }: { title: string; records: ProvenanceRecord[]; names: Map<string, string>; collapsible?: boolean }) {
  if (!records.length) return null;
  return <section className="provenance-group"><h3>{title} <span>{records.length}</span></h3>{records.map((record, index) => {
    const identity = textOf(record.id) || `${title}-${index}`, name = textOf(record.name) || textOf(record.filename) || identity;
    const content = <>{textOf(record.description) && <p>{textOf(record.description)}</p>}<dl className="reference-properties">{provenanceFields.filter(([key]) => record[key] != null).map(([key, label]) => <div key={key}><dt>{label}</dt><dd><ProvenanceValue value={record[key]} names={names} /></dd></div>)}</dl>{textOf(record.command) && <details className="provenance-command"><summary>Recorded command</summary><pre>{textOf(record.command)}</pre></details>}<details className="reference-raw"><summary>Full source record</summary><Record value={record} /></details></>;
    return collapsible ? <details key={identity} id={provenanceAnchor(identity)} className="provenance-entry"><summary>{name}</summary>{content}</details> : <article key={identity} id={provenanceAnchor(identity)} className="provenance-entry"><h4>{name}</h4>{content}</article>;
  })}</section>;
}
