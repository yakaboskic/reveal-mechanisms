"use client";
import { LocalWorkView } from "./LocalWork";
import { useIdentity } from "./Session";
export function LocalResearchPage({ id }: { id: string }) {
  const { me, ready } = useIdentity();
  if (!ready) return <main className="local-work-page"><p role="status">Checking your workspace…</p></main>;
  return <LocalWorkView key={`${me?.user_id || "visitor"}:${id}`} id={id} />;
}
