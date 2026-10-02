import React from "react";
import Link from "next/link";
import { citationTitle } from "../lib/citation-title";

/** The complete reference remains available in its tooltip and metadata view. */
export function CitationReference({ title, href, targetId }: { title: string; href: string; targetId: string }) {
  const display = citationTitle(title);
  return <><Link className="reference-title" href={href} title={title} aria-label={`Inspect cited record: ${title}`}>{display.text}</Link>{display.truncated && "…"}{" "}<span className="reference-id">[{targetId}]</span></>;
}
