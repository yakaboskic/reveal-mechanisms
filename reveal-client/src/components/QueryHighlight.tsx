import React, { type ReactNode } from "react";

/** Mark literal query words without changing source text or interpreting HTML. */
export function QueryHighlight({ text, query }: { text: string; query: string }) {
  const terms = [...new Set(query.trim().split(/\s+/u).filter(Boolean))];
  const ranges: [number, number][] = [];
  for (const term of terms) {
    const literal = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    // Unicode word boundaries keep abbreviations like RNA out of "alternative".
    // Lookahead retains overlaps between punctuation-bearing scientific terms.
    for (const match of text.matchAll(new RegExp(`(?<![\\p{L}\\p{M}\\p{N}_])(?=(${literal})(?![\\p{L}\\p{M}\\p{N}_]))`, "giu"))) {
      ranges.push([match.index, match.index + match[1].length]);
    }
  }
  if (!ranges.length) return <>{text}</>;
  ranges.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  const merged: [number, number][] = [];
  for (const range of ranges) {
    const previous = merged[merged.length - 1];
    if (previous && range[0] <= previous[1]) previous[1] = Math.max(previous[1], range[1]);
    else merged.push([...range]);
  }
  const parts: ReactNode[] = [];
  let offset = 0;
  for (const [start, end] of merged) {
    parts.push(text.slice(offset, start), <mark className="query-highlight" key={start}>{text.slice(start, end)}</mark>);
    offset = end;
  }
  return <>{parts}{text.slice(offset)}</>;
}
