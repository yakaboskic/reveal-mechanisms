import React from "react";
import type { Schema } from "@/lib/client";
import { gapContext, readableTerm, type GapEvidence } from "./gap-context";

function Metadata({ items }: { items: { label: string; value: string }[] }) {
  return <dl className="gap-detail-metadata">{items.map(item => <div key={item.label}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl>;
}

function Evidence({ items }: { items: GapEvidence[] }) {
  return <ol className="gap-detail-evidence">{items.map((item, index) => <li key={index}>
    <p className="gap-detail-reference">{item.href ? <a href={item.href} target="_blank" rel="noopener noreferrer">{item.title || item.reference}</a> : item.title || item.reference || "Source evidence"}
      {item.title && item.reference && <span>{item.reference}</span>}</p>
    {!!item.badges.length && <p className="gap-detail-evidence-tags">{item.badges.join(" · ")}</p>}
    {item.snippet && <blockquote>{item.snippet}</blockquote>}
    {item.explanation && <p>{item.explanation}</p>}
  </li>)}</ol>;
}

export function GapContext({ gap }: { gap: Schema<"GapRecord"> }) {
  const context = gapContext(gap);
  return <div className="gap-detail-disclosures">
    <details className="gap-detail-disclosure"><summary>About this knowledge gap</summary><div className="gap-detail-disclosure-body"><Metadata items={context.metadata} /></div></details>
    <details className="gap-detail-disclosure"><summary>Context and rationale</summary><div className="gap-detail-disclosure-body">
      <p>{context.context}</p>
      {context.resolution && <><h3>Resolution</h3><p>{context.resolution}</p></>}
      {context.notes && <><h3>Curation notes</h3><p>{context.notes}</p></>}
    </div></details>
    {!!gap.attachments.length && <details className="gap-detail-disclosure"><summary>Attached nodes and mechanisms <span>{gap.attachments.length}</span></summary><div className="gap-detail-disclosure-body">
      <ul className="gap-detail-attachments">{gap.attachments.map((item, index) => <li key={`${item.source_reference}:${index}`}>
        <p>{item.label || item.source_reference.split("#").slice(1).join("#") || readableTerm(item.target_kind)}</p>
        <small>{readableTerm(item.target_kind)} · {readableTerm(item.resolution)}</small>
        <small className="gap-detail-source-reference">{item.source_reference}</small>
      </li>)}</ul>
    </div></details>}
    {!!context.experiments.length && <details className="gap-detail-disclosure"><summary>Proposed experiments <span>{context.experiments.length}</span></summary><div className="gap-detail-disclosure-body">
      {context.experiments.map((experiment, index) => <article className="gap-detail-experiment" key={index}>
        <h3>{experiment.name}</h3>{experiment.type && <small>{experiment.type}</small>}{experiment.description && <p>{experiment.description}</p>}
        {(!!experiment.groups.length || experiment.decision) && <details className="gap-detail-design"><summary>Experimental design</summary>
          {experiment.groups.map(group => <section key={group.label}><h4>{group.label}</h4><ul>{group.items.map((item, index) => <li key={index}>
            {item.name && <p className="gap-detail-item-name">{item.name}</p>}{item.description && <p>{item.description}</p>}{!!item.metadata.length && <Metadata items={item.metadata} />}
          </li>)}</ul></section>)}
          {experiment.decision && <section><h4>Decision criterion</h4><p>{experiment.decision}</p></section>}
        </details>}
        {!!experiment.evidence.length && <details className="gap-detail-design"><summary>Experiment evidence <span>{experiment.evidence.length}</span></summary><Evidence items={experiment.evidence} /></details>}
        {experiment.notes && <p>{experiment.notes}</p>}
      </article>)}
    </div></details>}
    {!!context.evidence.length && <details className="gap-detail-disclosure"><summary>Source evidence <span>{context.evidence.length}</span></summary><div className="gap-detail-disclosure-body"><Evidence items={context.evidence} /></div></details>}
    <details className="gap-detail-disclosure"><summary>DisMech source and provenance</summary><div className="gap-detail-disclosure-body">
      <p className="gap-detail-source-links">{context.sourcePage && <a href={context.sourcePage} target="_blank" rel="noopener noreferrer">View discussion in DisMech ↗</a>}{context.sourceUrl && <a href={context.sourceUrl} target="_blank" rel="noopener noreferrer">View current source YAML ↗</a>}</p>
      <Metadata items={[{ label: "Source record", value: gap.source.source_id }, { label: "Source revision", value: gap.source.source_revision }, { label: "Source file", value: gap.source_detail.source_file }, { label: "Source location", value: gap.source_detail.source_pointer }]} />
    </div></details>
  </div>;
}
