"use client";
import { LocalWorkView } from "./LocalWork";
import { useIdentity } from "./Session";
export function LocalResearchPage({ id }: { id: string }) {
  const { me, ready } = useIdentity();
  if (!ready) return <main id="main" className="local-work-page local-work-download-page"><div className="local-workspace-opening" role="status"><span className="local-workspace-spinner" aria-hidden="true" />Opening your workspace…</div></main>;
  return <LocalWorkView key={`${me?.user_id || "visitor"}:${id}`} id={id} />;
}
