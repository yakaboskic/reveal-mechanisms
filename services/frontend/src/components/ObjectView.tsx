"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api, messageOf } from "@/lib/client";
import { Record } from "./Scientific";
export function ObjectView({ id }: { id: string }) {
  const [record, setRecord] = useState<Awaited<ReturnType<typeof api.object>> | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { void api.object(id).then(setRecord).catch(e => setError(messageOf(e))); }, [id]);
  return <main id="main" className="reading-page"><div className="page-topbar"><Link href="/">Explore knowledge gaps</Link><Link href="/workspace?tab=accounts">Your scientific accounts</Link></div><h1>Scientific record</h1><p className="idline">{id}</p>{record ? <Record value={record} /> : <p role={error ? "alert" : "status"}>{error || "Loading the authorized scientific record…"}</p>}</main>;
}
