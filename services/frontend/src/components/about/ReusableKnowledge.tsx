"use client";

import { useId, useState, type CSSProperties, type ReactNode } from "react";
import { useResearchStory } from "./useResearchStory";
import "./reusable-knowledge.css";

type Kind = "context" | "gap" | "agent" | "claim" | "account" | "community" | "dataset" | "file" | "reuse";
type Topic = Kind | "provenance" | "researchers" | "contributors" | "software";
type Point = readonly [number, number];
type StoryNode = { id: string; kind: Kind; label: string; note: string; enters: number; wide: Point; narrow: Point };

const stages = [
  { label: "Context", title: "Start with research context", text: "A researcher’s question, experience and hypotheses give the investigation its direction.", focus: ["context"] },
  { label: "Knowledge gap", title: "Identify what remains unexplained", text: "DisMech contributes curated knowledge gaps about disease mechanisms, grounded in the scientific record.", focus: ["gap", "dismech"] },
  { label: "Community", title: "Prioritize questions together", text: "Community votes surface gaps worth exploring. They guide attention without replacing scientific judgment.", focus: ["gap", "community"] },
  { label: "Claims", title: "Direct the agent’s investigation", text: "The researcher guides an agent to find claims and assemble evidence that supports, challenges or qualifies them.", focus: ["agent", "claims"] },
  { label: "Account", title: "Synthesize and publish an account", text: "Claims form an inspectable scientific account. Researchers choose when to publish; the community can assess and rank the work.", focus: ["claims", "account", "community"] },
  { label: "Sources", title: "Trace each claim to its sources", text: "DAPPER connects claims to evidence, analyses, datasets and files, recording how each source contributed.", focus: ["claims", "dataset", "file"] },
  { label: "Reuse", title: "Discover data through its scientific context", text: "Other researchers can find relevant data through the questions and claims it informed. Credit stays linked to researchers, data contributors and analysis.", focus: ["reuse", "dataset", "file"] },
] as const;

// Fixed positions keep the scientific relationships legible throughout playback.
const nodes: StoryNode[] = [
  { id: "context", kind: "context", label: "Research context", note: "Question & judgment", enters: 0, wide: [12, 43], narrow: [27, 9] },
  { id: "gap", kind: "gap", label: "Knowledge gap", note: "An open question", enters: 1, wide: [37, 43], narrow: [27, 29] },
  { id: "dismech", kind: "gap", label: "DisMech", note: "Curated gaps", enters: 1, wide: [37, 16], narrow: [76, 18] },
  { id: "community", kind: "community", label: "Community", note: "Prioritize & assess", enters: 2, wide: [37, 74], narrow: [76, 34] },
  { id: "agent", kind: "agent", label: "Researcher + agent", note: "Directed investigation", enters: 3, wide: [62, 16], narrow: [76, 49] },
  { id: "claims", kind: "claim", label: "Scientific claims", note: "Evidence & reasoning", enters: 3, wide: [62, 43], narrow: [27, 49] },
  { id: "account", kind: "account", label: "Scientific account", note: "Published synthesis", enters: 4, wide: [87, 43], narrow: [27, 69] },
  { id: "dataset", kind: "dataset", label: "Datasets", note: "Traceable sources", enters: 5, wide: [62, 74], narrow: [76, 69] },
  { id: "file", kind: "file", label: "Files", note: "Source material", enters: 5, wide: [87, 74], narrow: [76, 89] },
  { id: "reuse", kind: "reuse", label: "Further research", note: "New context, shared credit", enters: 6, wide: [12, 74], narrow: [27, 89] },
];

const details: Record<Topic, { title: string; text: string }> = {
  context: { title: "Find data in the context of a question", text: "A dataset’s usefulness depends on the research question. Your context, hypotheses and scientific judgment guide what to investigate and how to interpret the evidence." },
  gap: { title: "A curated starting point", text: "DisMech records knowledge gaps about disease mechanisms. Researchers can choose a gap, bring their own context and explore different explanations of the same question." },
  agent: { title: "Researcher direction, agent assistance", text: "An agent helps search for mechanisms, retrieve scientific claims and assemble their evidence. The researcher sets the direction and weighs the resulting explanations and limitations." },
  community: { title: "Votes guide discovery", text: "Upvotes and downvotes help surface knowledge gaps and reflect the community’s reception of published accounts. They do not establish scientific truth or replace scrutiny of the evidence." },
  account: { title: "A synthesis open to scrutiny", text: "A scientific account brings claims together around one knowledge gap. Its researcher decides when to publish a snapshot, with the reasoning, source relationships and attribution available to inspect." },
  claim: { title: "Follow the reasoning", text: "A claim records an attributed assessment of a scientific proposition. Its evidence can support, challenge or qualify the proposition; provenance connects that assessment to the underlying sources and analyses." },
  dataset: { title: "Shared sources, distinct interpretations", text: "Several claims can draw on one dataset, and a claim can draw on several datasets. A shared source identity keeps these connections visible without implying that the data supports every claim." },
  file: { title: "Reach the research material", text: "Files are the concrete artifacts associated with a dataset or analysis. Recorded locations connect you to those materials; access depends on the source and its permissions." },
  provenance: { title: "Inspect evidence and provenance", text: "Evidence explains how a source bears on a proposition. DAPPER structures that relationship alongside provenance: source inputs, analysis activities, software and contributors, so you can trace how the work was produced." },
  reuse: { title: "New context, preserved source credit", text: "Other researchers can discover data through the claims it informed, judge its relevance and reuse it for a different question. The original source relationships and attribution remain part of that work." },
  researchers: { title: "Recognize the scientific contribution", text: "Accounts retain researcher attribution alongside their claims and sources. Agents and software can assist the work without being mistaken for the researcher who contributed the scientific account." },
  contributors: { title: "Keep data contributors in view", text: "Dataset and file provenance can identify the people and organizations behind the source material. Those connections preserve the original contribution when data is used again." },
  software: { title: "Make analysis contributions traceable", text: "Recorded activities connect analyses, software and agents to their outputs. Their contributions can remain visible as other researchers build on the work." },
};

function KnowledgeIcon({ kind }: { kind: Kind }) {
  const paths: Record<Kind, ReactNode> = {
    context: <><path d="M4 5h16v14H4zM8 9h8M8 13h5" /></>,
    gap: <><path d="M9 8a3 3 0 1 1 5 2c-2 1-2 2-2 4M12 17h.01" /><circle cx="12" cy="12" r="9" /></>,
    agent: <><path d="M4 7h6v6H4zM14 11h6v6h-6zM10 10h7v1M7 13v4h7" /></>,
    claim: <><path d="M4 4h16v12H9l-5 4V4ZM8 8h8M8 12h5" /></>,
    account: <><path d="M4 4h12v16H4zM8 8h5M8 12h5M8 16h3M16 7h4v13h-4" /></>,
    community: <><path d="M7 19V5M3 9l4-4 4 4M17 5v14M13 15l4 4 4-4" /></>,
    dataset: <><ellipse cx="12" cy="5" rx="7" ry="3" /><path d="M5 5v14c0 4 14 4 14 0V5M5 12c0 4 14 4 14 0" /></>,
    file: <><path d="M5 3h9l5 5v13H5zM14 3v5h5M9 12h6M9 16h6" /></>,
    reuse: <><path d="M4 9a8 8 0 0 1 14-3l2 3M20 3v6h-6M20 15a8 8 0 0 1-14 3l-2-3M4 21v-6h6" /></>,
  };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.35" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{paths[kind]}</svg>;
}

type Connection = { from: string; to: string; enters: number; source?: boolean; widePath?: string; narrowPath?: string };
const connections: Connection[] = [
  { from: "context", to: "gap", enters: 1 },
  { from: "dismech", to: "gap", enters: 1 },
  { from: "community", to: "gap", enters: 2 },
  { from: "gap", to: "claims", enters: 3 },
  { from: "agent", to: "claims", enters: 3 },
  { from: "claims", to: "account", enters: 4 },
  { from: "community", to: "account", enters: 4, widePath: "M370 326V253H870V189", narrowPath: "M243 238H306V434H86V483" },
  { from: "claims", to: "dataset", enters: 5, source: true },
  { from: "dataset", to: "file", enters: 5, source: true },
  { from: "dataset", to: "reuse", enters: 6, widePath: "M620 326V407H120V326" },
];

function Connections({ step, compact = false }: { step: number; compact?: boolean }) {
  const width = compact ? 320 : 1000, height = compact ? 700 : 440;
  const position = (key: string) => {
    const node = nodes.find(item => item.id === key)!;
    const [x, y] = compact ? node.narrow : node.wide;
    return `${x * width / 100} ${y * height / 100}`;
  };
  return <svg className={`reuse-story-lines reuse-story-lines--${compact ? "compact" : "wide"}`} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
    {connections.map(edge => <path key={`${edge.from}-${edge.to}`} className="reuse-story-line" data-reached={step >= edge.enters} data-active={step === edge.enters} data-source={edge.source} d={(compact ? edge.narrowPath : edge.widePath) ?? `M${position(edge.from)}L${position(edge.to)}`} />)}
  </svg>;
}

export function ReusableKnowledge() {
  const id = useId();
  const { step, playing, reducedMotion, stageRef, goTo, toggle, pause } = useResearchStory();
  const [selection, setSelection] = useState<{ id: string; topic: Topic } | null>(null);
  const [announce, setAnnounce] = useState(false);
  const detailId = `${id}-detail`, stageId = `${id}-stage`;
  const selectStage = (value: number) => { setAnnounce(true); setSelection(null); goTo(value); };
  const choose = (nodeId: string, topic: Topic) => {
    pause();
    setAnnounce(true);
    setSelection(current => current?.id === nodeId ? null : { id: nodeId, topic });
  };
  const detail = selection ? details[selection.topic] : stages[step];
  return <div className="reusable-knowledge" data-reduced-motion={reducedMotion}>
    <div className="reuse-story-topbar">
      <span>DAPPER <span className="reuse-story-topbar-description">Connecting questions to reusable data</span></span>
      <button type="button" onClick={() => selectStage(stages.length - 1)}>View full map</button>
    </div>
    <div ref={stageRef} id={stageId} className="reuse-story-stage" role="group" aria-label="From research context to reusable scientific knowledge">
      <p className="sr-only">Select any item for its explanation. The walkthrough follows research context through a knowledge gap, community priorities, agent-assisted claims, a published account, source provenance and reuse. The full text follows the diagram.</p>
      <Connections step={step} /><Connections step={step} compact />
      {nodes.map(node => {
        const active = !selection && (stages[step].focus as readonly string[]).includes(node.id);
        const [x, y] = node.wide, [mobileX, mobileY] = node.narrow;
        return <button type="button" key={node.id} className={`reuse-story-node reuse-story-node--${node.kind}`} style={{ "--node-x": `${x}%`, "--node-y": `${y}%`, "--mobile-x": `${mobileX}%`, "--mobile-y": `${mobileY}%` } as CSSProperties} data-reached={step >= node.enters} data-active={active} aria-controls={detailId} aria-expanded={selection?.id === node.id} onFocus={pause} onClick={() => choose(node.id, node.kind)}>
          <KnowledgeIcon kind={node.kind} /><span className="reuse-story-node-label">{node.label}</span><span className="reuse-story-node-meta">{node.note}</span>
        </button>;
      })}
    </div>
    <div className="reuse-story-controls" aria-label="Research story controls">
      <div className="reuse-story-control-row">
        <span className="reuse-story-count">{step + 1} / {stages.length}</span>
        <div className="reuse-story-playback">
          <button type="button" onClick={() => selectStage(step - 1)} disabled={step === 0} aria-label="Previous stage">←</button>
          <button type="button" onClick={() => { setAnnounce(false); setSelection(null); toggle(); }} aria-label={playing ? "Pause research story" : "Play research story"}>{playing ? "Pause" : step === stages.length - 1 ? "Replay" : "Play"}</button>
          <button type="button" onClick={() => selectStage(step + 1)} disabled={step === stages.length - 1} aria-label="Next stage">→</button>
        </div>
      </div>
      <div className="reuse-story-caption" id={detailId} role="region" aria-label="Research story explanation" aria-live={announce ? "polite" : "off"} aria-atomic="true"><h3>{detail.title}</h3><p>{detail.text}</p></div>
      <div className="reuse-story-progress" aria-hidden="true">{stages.map((stage, index) => <span key={stage.label} data-reached={index <= step} />)}</div>
      {reducedMotion && <p className="reuse-story-motion-note">Reduced motion is on. Explore at your own pace.</p>}
    </div>
    <details className="reuse-story-transcript" onToggle={event => { if (event.currentTarget.open) pause(); }}><summary>How the connections work</summary><ol>{stages.map(stage => <li key={stage.label}><h4>{stage.title}</h4><p>{stage.text}</p></li>)}</ol><h4>{details.provenance.title}</h4><p>{details.provenance.text}</p><h4>Credit stays connected</h4><p>{details.researchers.text} {details.contributors.text} {details.software.text}</p></details>
  </div>;
}
