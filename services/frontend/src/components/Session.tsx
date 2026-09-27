"use client";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { signIn, signOut } from "next-auth/react";
import Link from "next/link";
import { selectedGap } from "@/lib/composer";
import { api, messageOf, type Schema } from "@/lib/client";
import { withRequestDeadline } from "@/lib/request-deadline";

type SessionStatus = { canClaim: boolean; providers: { google: boolean; orcid: boolean } };
type SessionContextType = { me: Schema<"Me"> | null; ready: boolean; status: SessionStatus; refresh: () => Promise<Schema<"Me"> | null> };
const SessionContext = createContext<SessionContextType>({ me: null, ready: false, status: { canClaim: false, providers: { google: false, orcid: false } }, refresh: async () => null });
export const useIdentity = () => useContext(SessionContext);

export function Session({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Schema<"Me"> | null>(null);
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState<SessionStatus>({ canClaim: false, providers: { google: false, orcid: false } });
  const [menu, setMenu] = useState(false);
  const menuArea = useRef<HTMLDivElement>(null);
  const [claimMessage, setClaimMessage] = useState("");
  const refresh = async () => {
    try {
      const state = await withRequestDeadline(signal => fetch("/api/session/status", { cache: "no-store", signal }).then(r => {
        if (!r.ok) throw new Error("Your session could not be checked. Please retry.");
        return r.json();
      })); setStatus(state);
      const identity = state.principal ? await api.me() : null; setMe(identity); return identity;
    } catch { setMe(null); return null; } finally { setReady(true); }
  };
  useEffect(() => { void refresh(); }, []);
  useEffect(() => {
    if (!menu) return;
    const closeOutside = (event: PointerEvent) => { if (!menuArea.current?.contains(event.target as Node)) setMenu(false); };
    const closeEscape = (event: KeyboardEvent) => { if (event.key === "Escape") { setMenu(false); menuArea.current?.querySelector("button")?.focus(); } };
    document.addEventListener("pointerdown", closeOutside); document.addEventListener("keydown", closeEscape);
    return () => { document.removeEventListener("pointerdown", closeOutside); document.removeEventListener("keydown", closeEscape); };
  }, [menu]);
  useEffect(() => {
    if (!me) return;
    try {
      const visits = JSON.parse(sessionStorage.getItem("reveal:local-explorations") || "[]") as Schema<"GapRecord">[];
      if (visits.length) void Promise.all(visits.map(gap => api.explore({ source_gap: selectedGap(gap) }))).then(() => sessionStorage.removeItem("reveal:local-explorations")).catch(() => {});
    } catch { /* retain local selections when storage is unavailable */ }
  }, [me?.user_id]);
  const logout = async () => {
    await fetch("/api/session/logout", { method: "POST" });
    for (const key of Object.keys(sessionStorage)) if (key.startsWith("reveal:")) sessionStorage.removeItem(key);
    await signOut({ callbackUrl: "/" });
  };
  const claim = async () => {
    try {
      const response = await fetch("/api/session/claim", { method: "POST", headers: { "content-type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ consent: true }) });
      const result = await response.json(); if (!response.ok) throw new Error(result.detail);
      setClaimMessage("Your anonymous work is now in this workspace. Scientific attribution is unchanged."); await refresh();
    } catch (error) { setClaimMessage(messageOf(error)); }
  };
  return <SessionContext.Provider value={{ me, ready, status, refresh }}>
    <a className="skip" href="#main">Skip to content</a>
    <header className="site-nav workspace-chrome">
      <div className="avatar-area" ref={menuArea}><button className="avatar" aria-label="Your workspace" aria-expanded={menu} onClick={() => setMenu(!menu)}>{me?.display_name?.slice(0, 1).toUpperCase() || <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2" aria-hidden="true"><circle cx="12" cy="8" r="3.2" /><path d="M5 20v-2a7 7 0 0 1 14 0v2" /></svg>}</button>
        {menu && <div className="avatar-menu" onKeyDown={e => { if (e.key === "Escape") setMenu(false); }}>
          <p>{me?.display_name || (me ? "Anonymous workspace" : "Your workspace")}</p>
          <nav><Link onClick={() => setMenu(false)} href="/workspace?tab=gaps">Your knowledge gaps</Link><Link onClick={() => setMenu(false)} href="/workspace?tab=accounts">Your scientific accounts</Link></nav>
          {me?.principal_kind === "anonymous" && <small>Anonymous access ends {me.workspace_expires_at ? new Date(me.workspace_expires_at).toLocaleDateString() : "with this session"}.</small>}
          {me?.principal_kind !== "registered" && <><small>Sign in to keep your work across devices.</small><ProviderButtons disabled={!ready} /></>}
          {me && <button className="text-button" onClick={logout}>Sign out</button>}
        </div>}
      </div>
    </header>
    {status.canClaim && <div className="continuity"><p>Keep the work from your anonymous session in this signed-in workspace?</p><button onClick={claim}>Move my anonymous work</button><small>Existing scientific identities and attribution stay unchanged.</small></div>}
    {claimMessage && <p role="status" className="notice">{claimMessage}</p>}
    {children}
  </SessionContext.Provider>;
}
export function ProviderButtons({ disabled = false, onLogin }: { disabled?: boolean; onLogin?: (provider: "google" | "orcid") => void }) {
  const { status } = useIdentity();
  return <>{(["orcid", "google"] as const).map(provider => <div key={provider}>
    <button className="provider" disabled={disabled || !status.providers[provider]} onClick={() => { if (onLogin) onLogin(provider); else void signIn(provider, { callbackUrl: window.location.origin + "/" }); }}>
      <span aria-hidden="true">{provider === "orcid" ? "iD" : "G"}</span>Continue with {provider === "orcid" ? "ORCID" : "Google"}
    </button>{!status.providers[provider] && <small>{provider === "orcid" ? "ORCID" : "Google"} sign-in is not configured.</small>}
  </div>)}</>;
}
