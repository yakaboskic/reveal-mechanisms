"use client";

import { useEffect, useRef, useState } from "react";
import { api, messageOf, terminal, type Schema } from "@/lib/client";
import { paragraphStateFromJob } from "@/lib/paragraph-state";
import { Activity } from "./Activity";
import { LoadingSurface } from "./LoadingSurface";
import "./paragraph-activity.css";

function Elapsed({ job }: { job: Schema<"Job"> }) {
  const [now, setNow] = useState(Date.now);
  const active = !terminal(job.status);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [active]);
  const seconds = Math.max(0, Math.floor(((job.completed_at ? Date.parse(job.completed_at) : now) - Date.parse(job.created_at)) / 1_000));
  if (!Number.isFinite(seconds)) return null;
  return <span>Elapsed {Math.floor(seconds / 60)}m {seconds % 60}s</span>;
}

export function ParagraphActivity({ jobId, accountId, onJob, initial, deferred = false }: {
  jobId: string;
  accountId: string;
  onJob: (job: Schema<"Job">) => void;
  initial?: Schema<"Job"> | null;
  deferred?: boolean;
}) {
  const [opened, setOpened] = useState(!deferred);
  const [job, setJob] = useState<Schema<"Job"> | null>(initial?.id === jobId ? initial : null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const callback = useRef(onJob); callback.current = onJob;
  useEffect(() => {
    if (!opened) return;
    let active = true;
    setError("");
    void api.job(jobId).then(value => {
      if (!active) return;
      if (!paragraphStateFromJob(value, accountId)) throw new Error("This activity does not belong to the requested research statement.");
      setJob(value); callback.current(value);
    }).catch(failure => { if (active) setError(messageOf(failure)); });
    return () => { active = false; };
  }, [jobId, accountId, opened, retry]);
  const update = (value: Schema<"Job">) => {
    if (value.id !== jobId || !paragraphStateFromJob(value, accountId)) return;
    setJob(value); callback.current(value);
  };
  return <section className="paragraph-activity" aria-label="Research statement activity">
    {!opened ? <button className="paragraph-activity-open" onClick={() => setOpened(true)}>View statement activity <span aria-hidden="true">›</span></button> : <>
      {job ? <><div className="paragraph-activity-meta"><span>Research statement</span><Elapsed job={job} /></div><Activity key={job.id} initial={job} onJob={update} /></> : <LoadingSurface compact title={error ? "Statement activity is unavailable" : "Connecting to statement activity"} description="Retrieving the writing agent’s progress and tool calls." error={error} onRetry={() => setRetry(n => n + 1)} skeleton="none" />}
      {job && error && <div className="error" role="alert">{error}<button onClick={() => setRetry(n => n + 1)}>Retry activity</button></div>}
    </>}
  </section>;
}
