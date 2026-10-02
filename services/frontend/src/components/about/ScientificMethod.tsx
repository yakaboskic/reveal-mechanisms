"use client";

import { useId, useState } from "react";
import "./scientific-method.css";

type Layer = "science" | "data" | "context";

const explanations: Record<Layer, { title: string; text: string }> = {
  science: {
    title: "Connect biological concepts",
    text: "Link biological concepts and mechanisms to frame a knowledge gap and identify predictions that existing data can test.",
  },
  data: {
    title: "Put predictions to the test",
    text: "Use existing data for analyses that test predictions. Agents help find relevant datasets, run analyses, and assemble evidence for and against scientific claims.",
  },
  context: {
    title: "Scientific judgment stays with you",
    text: "You pose a knowledge gap and judge whether the evidence actually warrants closing it. Consider supporting and conflicting findings, their limitations, and what remains unexplained.",
  },
};

function ScienceIllustration() {
  return <svg className="method-diagram-illustration" viewBox="0 0 240 142" aria-hidden="true" focusable="false">
    <g fill="none" stroke="#4a79aa" strokeWidth="2.4" strokeLinecap="round">
      <path d="M26 17c50 29-35 64 15 101M46 17C-4 46 81 81 31 118" />
      <path d="m29 24 14 5m-18 9 22 9m-25 5 28 10m-27 4 26 10m-23 5 19 8m-17 8 15 6" strokeWidth="1.5" opacity=".7" />
    </g>
    <path d="M93 35c18-13 42-5 51 14 9 21 0 48-18 58-20 10-46-2-48-26-2-20 0-37 15-46Z" fill="#dce9f8" stroke="#86a8cd" strokeWidth="1.5" />
    <ellipse cx="112" cy="72" rx="18" ry="21" fill="#b7cdec" stroke="#7799c2" strokeWidth="1.5" />
    <circle cx="116" cy="73" r="8" fill="#638bbd" />
    <g fill="none" stroke="#7599c6" strokeWidth="3" strokeLinecap="round">
      <path d="m91 50 6-4m-10 37 3 7m41-42 4 6m-3 35-6 6m-24 5 6 2" />
    </g>
    <g fill="none" stroke="#567da4" strokeWidth="1.5">
      <path d="m170 39 42 14-11 38-31 14-8-35 8-31m0 0 31 52m-39-21 50-17m-11 38 23 22" />
    </g>
    <g stroke="#648bae" strokeWidth="1.5">
      <circle cx="170" cy="39" r="8" fill="#ccddf1" /><circle cx="212" cy="53" r="9" fill="#bad4e5" />
      <circle cx="162" cy="70" r="6" fill="#eef5fc" /><circle cx="201" cy="91" r="9" fill="#a9cace" />
      <circle cx="170" cy="105" r="7" fill="#d1d9f2" /><circle cx="224" cy="113" r="6" fill="#e4eaf6" />
    </g>
    <path d="M19 134h207" stroke="#b6cce2" strokeWidth="1.3" />
    <path d="M62 130v8m18-8v8m18-8v8" stroke="#6d95be" strokeWidth="4" />
    <path d="M152 130h19v8h-19zm30 0h23v8h-23z" fill="#91b3d4" />
  </svg>;
}

function DataIllustration() {
  const cells = [2, 3, 1, 0, 4, 5, 4, 3, 1, 0, 2, 4, 5, 4, 1, 2, 3, 4, 3, 5, 2, 1, 2, 3, 5, 4, 3, 2];
  const colors = ["#d8e7ef", "#a8cadc", "#72a5c4", "#efcfb7", "#dea77b", "#c78054"];
  return <svg className="method-diagram-illustration" viewBox="0 0 240 142" aria-hidden="true" focusable="false">
    <rect x="15" y="13" width="210" height="12" rx="6" fill="#fff" stroke="#c3aaa0" />
    {[25, 36, 54, 63, 83, 98, 114, 137, 150, 177, 186, 210].map((x, index) => <path key={x} d={`M${x} 14v10`} stroke={index % 3 === 0 ? "#a99690" : "#53657a"} strokeWidth={index % 2 === 0 ? 5 : 3} />)}
    <path d="M154 8v22" stroke="#ba7448" strokeWidth="2" />
    <path d="M15 38v39h210" fill="none" stroke="#c6cbd1" strokeWidth="1" />
    <path d="m16 76 5-3 5 1 5-13 5 8 5-1 5-20 5 18 5-4 5 9 5-2 5 5 5-8 5 4 5-9 5 10 5-26 5 11 5-5 5 17 5-1 5 5 5-9 5 6 5-15 5 9 5-18 5 8 5 15 5-6 5 2 5-7 5 8 5-5 5 4 5-15 5 3 5-7 5 17 5-6 5 10 5-2v2H16Z" fill="#78a6c5" />
    {cells.map((value, index) => <rect key={index} x={15 + (index % 7) * 15} y={92 + Math.floor(index / 7) * 11} width="13" height="9" rx="1" fill={colors[value]} />)}
    <g stroke="#ac7657" strokeWidth="1.2" strokeLinecap="round">
      <path d="M145 99h79M145 117h79" />
      <path d="M158 95v8m16-8v8m34-8v8M152 113v8m37-8v8m23-8v8" strokeWidth="6" />
    </g>
    <path d="M145 133h18m8 0h11m8 0h16m8 0h10" stroke="#9eaebb" strokeWidth="2" strokeLinecap="round" />
  </svg>;
}

function ResearcherIllustration() {
  return <svg className="method-diagram-person" viewBox="0 0 90 78" aria-hidden="true" focusable="false">
    <circle cx="45" cy="38" r="35" fill="#edf6f3" />
    <path d="M29 68v-9c0-10 7-18 16-18s16 8 16 18v9" fill="#b6d5cf" stroke="#397d78" strokeWidth="1.6" />
    <path d="M34 29c0-8 4-13 11-13s11 5 11 13v4c0 7-5 12-11 12s-11-5-11-12Z" fill="#fff" stroke="#397d78" strokeWidth="1.6" />
    <path d="M34 27c3-1 6-4 7-8 3 5 8 7 15 7" fill="none" stroke="#397d78" strokeWidth="1.6" />
    <path d="m28 58 17 4 17-4v15l-17 3-17-3Z" fill="#fff" stroke="#397d78" strokeWidth="1.5" strokeLinejoin="round" />
    <path d="M45 63v12" stroke="#397d78" strokeWidth="1.2" />
    <path d="M13 22h7m-3-3v6M71 40h6" stroke="#88b6ad" strokeWidth="1.5" strokeLinecap="round" />
  </svg>;
}

function Connections({ id, compact = false }: { id: string; compact?: boolean }) {
  const marker = `${id}-${compact ? "compact" : "wide"}`;
  return <svg className={`method-diagram-connections ${compact ? "method-diagram-connections--compact" : "method-diagram-connections--wide"}`} viewBox={compact ? "0 0 350 460" : "0 0 1000 488"} preserveAspectRatio="none" aria-hidden="true" focusable="false">
    <defs>
      <marker id={`${marker}-blue`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="m1 1 8 4-8 4 2-4Z" fill="#4a79aa" /></marker>
      <marker id={`${marker}-warm`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="m1 1 8 4-8 4 2-4Z" fill="#a45d34" /></marker>
    </defs>
    <path className="method-diagram-arrow" d={compact ? "M85 66C126 46 223 46 264 66" : "M352 136C438 107 561 107 648 136"} markerEnd={`url(#${marker}-blue)`} />
    <path className="method-diagram-arrow" d={compact ? "M90 350C20 333 8 290 28 255" : "M364 385C278 367 198 317 162 257"} markerEnd={`url(#${marker}-blue)`} />
    <path className="method-diagram-arrow method-diagram-arrow--evidence" d={compact ? "M322 255C342 290 330 333 260 350" : "M838 257C802 317 722 367 636 385"} markerEnd={`url(#${marker}-warm)`} />
  </svg>;
}

export function ScientificMethod() {
  const [selected, setSelected] = useState<Layer | null>(null);
  const id = useId().replaceAll(":", "");
  const detailId = `method-detail-${id}`, titleId = `${detailId}-title`;
  const select = (layer: Layer) => setSelected(previous => previous === layer ? null : layer);
  return <div className="method-diagram">
    <div className="method-diagram-stage" role="group" aria-label="Scientific method: the researcher poses a knowledge gap, uses existing data to test predictions about linked biological concepts, and judges if the evidence actually warrants closing the gap">
      <Connections id={id} /><Connections id={id} compact />
      <button type="button" className="method-diagram-layer method-diagram-layer--science" aria-expanded={selected === "science"} aria-controls={detailId} onClick={() => select("science")}>
        <span className="method-diagram-layer-title">Science layer</span><span className="method-diagram-layer-subtitle">Linked biological concepts</span>
        <ScienceIllustration />
      </button>
      <button type="button" className="method-diagram-layer method-diagram-layer--data" aria-expanded={selected === "data"} aria-controls={detailId} onClick={() => select("data")}>
        <span className="method-diagram-layer-title">Data layer</span><span className="method-diagram-layer-subtitle">Existing data for analysis</span>
        <DataIllustration />
      </button>
      <button type="button" className="method-diagram-layer method-diagram-layer--context" aria-expanded={selected === "context"} aria-controls={detailId} onClick={() => select("context")}>
        <ResearcherIllustration /><span className="method-diagram-layer-title">Researcher context</span><span className="method-diagram-layer-subtitle">Scientific judgment</span>
      </button>
      <span className="method-diagram-action method-diagram-action--test">Uses data to test<br />predictions</span>
      <span className="method-diagram-action method-diagram-action--pose">Poses a<br />knowledge gap</span>
      <span className="method-diagram-action method-diagram-action--weigh">Judges if the evidence<br />actually warrants<br />closing the gap</span>
    </div>
    <div className="method-diagram-details" aria-live="polite" aria-atomic="true">
      {!selected && <p className="method-diagram-hint">Select a layer to explore.</p>}
      <section id={detailId} className={`method-diagram-detail${selected ? ` method-diagram-detail--${selected}` : ""}`} hidden={!selected} aria-labelledby={titleId}>
        <h3 id={titleId}>{selected ? explanations[selected].title : "Explore the scientific method"}</h3>
        <p>{selected ? explanations[selected].text : ""}</p>
      </section>
    </div>
  </div>;
}
