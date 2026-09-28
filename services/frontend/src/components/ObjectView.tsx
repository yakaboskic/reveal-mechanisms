"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api, messageOf } from "@/lib/client";
import { Record } from "./Scientific";
import { LoadingSurface } from "./LoadingSurface";
export function ObjectView({ id }: { id: string }) {
  const [snapshot, setSnapshot] = useState<{ id: string; value: Awaited<ReturnType<typeof api.object>> } | null>(null);
  const [failure, setFailure] = useState<{ id: string; message: string } | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true; setSnapshot(null); setFailure(null);
    void api.object(id).then(value => { if (active) setSnapshot({ id, value }); }).catch(error => { if (active) setFailure({ id, message: messageOf(error) }); });
    return () => { active = false; };
  }, [id, retry]);
  const record = snapshot?.id === id ? snapshot.value : null;
  const error = failure?.id === id ? failure.message : "";
  return <main id="main" className="reading-page"><nav className="page-topbar" aria-label="Scientific record navigation"><Link href="/">Explore knowledge gaps</Link><Link href="/workspace?tab=accounts">Your scientific accounts</Link></nav><h1>Scientific record</h1><p className="idline">{id}</p>{record ? <Record value={record} /> : <LoadingSurface key={id} title={error ? "Couldn’t open this record" : "Opening scientific record"} description="Retrieving its saved content, identity and source provenance." error={error} onRetry={() => setRetry(value => value + 1)} skeleton="record" />}</main>;
}
