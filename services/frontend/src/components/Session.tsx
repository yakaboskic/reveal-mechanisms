"use client";
import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { signIn, signOut } from "next-auth/react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { selectedGap } from "@/lib/composer";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { withRequestDeadline } from "@/lib/request-deadline";
import { continuityPromptDismissed, dismissContinuityPrompt } from "@/lib/continuity-prompt";
import { WorkspaceCacheProvider } from "./WorkspaceCache";
import { GapDiscoveryProvider } from "./GapDiscoveryCache";
import { invalidateWorkspace, resetWorkspaceCache } from "@/lib/workspace-events";
import "./session-menu.css";

type SessionStatus = { canClaim: boolean; canAdmin?: boolean; providers: { google: boolean; orcid: boolean } };
type SessionContextType = { me: Schema<"Me"> | null; ready: boolean; status: SessionStatus; refresh: () => Promise<Schema<"Me"> | null> };
const SessionContext = createContext<SessionContextType>({ me: null, ready: false, status: { canClaim: false, providers: { google: false, orcid: false } }, refresh: async () => null });
export const useIdentity = () => useContext(SessionContext);

export function Session({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  useEffect(() => {
    // Native selects can match :focus-visible after a mouse click in Chromium.
    // Track input modality without blurring controls or interrupting navigation.
    const pointer = () => { document.documentElement.dataset.inputModality = "pointer"; };
    const keyboard = (event: KeyboardEvent) => {
      if (!["Shift", "Control", "Alt", "Meta"].includes(event.key)) document.documentElement.dataset.inputModality = "keyboard";
    };
    document.addEventListener("pointerdown", pointer, true);
    document.addEventListener("keydown", keyboard, true);
    return () => { document.removeEventListener("pointerdown", pointer, true); document.removeEventListener("keydown", keyboard, true); };
  }, []);
  const [me, setMe] = useState<Schema<"Me"> | null>(null);
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState<SessionStatus>({ canClaim: false, providers: { google: false, orcid: false } });
  const [menu, setMenu] = useState(false);
  const menuArea = useRef<HTMLDivElement>(null);
  const [claimMessage, setClaimMessage] = useState("");
  const [claimPrompt, setClaimPrompt] = useState<{ owner: string; dismissed: boolean } | null>(null);
  const claimOwner = ready && me?.principal_kind === "registered" ? me.user_id : null;
  const identityRef = useRef<Schema<"Me"> | null>(null);
  const refreshSequence = useRef(0);
  const pendingRefresh = useRef<Promise<Schema<"Me"> | null> | null>(null);
  const refresh = useCallback((): Promise<Schema<"Me"> | null> => {
    if (pendingRefresh.current) return pendingRefresh.current;
    const sequence = ++refreshSequence.current;
    const request = (async () => {
    try {
      const state = await withRequestDeadline(signal => fetch("/api/session/status", { cache: "no-store", signal }).then(r => {
        if (!r.ok) throw new ApiError(r.status, "SESSION_CHECK_FAILED", "Your session could not be checked. Please retry.");
        return r.json();
      }));
      if (sequence !== refreshSequence.current) return null;
      setStatus(state);
      if (state.principal?.user_id !== identityRef.current?.user_id) {
        resetWorkspaceCache(); identityRef.current = null; setMe(null); setReady(false);
      }
      const identity = state.principal ? await api.me() : null;
      if (sequence !== refreshSequence.current) return null;
      identityRef.current = identity; setMe(identity); return identity;
    } catch (error) {
      if (sequence !== refreshSequence.current) return null;
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        resetWorkspaceCache(); identityRef.current = null; setMe(null);
      }
      // A transport outage does not change an already verified identity.
      // Retain its cached rows; explicit logout/denial still purges them.
      return identityRef.current;
    } finally { if (sequence === refreshSequence.current) setReady(true); }
    })();
    pendingRefresh.current = request;
    void request.finally(() => { if (pendingRefresh.current === request) pendingRefresh.current = null; });
    return request;
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    setClaimPrompt(claimOwner ? { owner: claimOwner, dismissed: continuityPromptDismissed(claimOwner) } : null);
    setClaimMessage("");
  }, [claimOwner]);
  useEffect(() => {
    const check = () => { if (document.visibilityState !== "hidden") void refresh(); };
    window.addEventListener("focus", check); window.addEventListener("online", check);
    return () => { window.removeEventListener("focus", check); window.removeEventListener("online", check); };
  }, [refresh]);
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
    refreshSequence.current++; pendingRefresh.current = null; identityRef.current = null;
    resetWorkspaceCache(); setMe(null); setStatus(current => ({ ...current, canClaim: false, canAdmin: false }));
    await fetch("/api/session/logout", { method: "POST" });
    for (const key of Object.keys(sessionStorage)) if (key.startsWith("reveal:")) sessionStorage.removeItem(key);
    await signOut({ callbackUrl: "/" });
  };
  const claim = async () => {
    try {
      const response = await fetch("/api/session/claim", { method: "POST", headers: { "content-type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ consent: true }) });
      const result = await response.json(); if (!response.ok) throw new Error(result.detail);
      invalidateWorkspace(); setClaimMessage("Your anonymous work is now in this workspace. Scientific attribution is unchanged."); await refresh();
    } catch (error) { setClaimMessage(messageOf(error)); }
  };
  const dismissClaim = () => {
    if (!claimOwner) return;
    dismissContinuityPrompt(claimOwner); setClaimPrompt({ owner: claimOwner, dismissed: true });
    menuArea.current?.querySelector<HTMLButtonElement>(".avatar")?.focus();
  };
  const canClaim = !!claimOwner && status.canClaim;
  const claimPromptKnown = claimPrompt?.owner === claimOwner;
  const claimDismissed = claimPromptKnown && claimPrompt?.dismissed;
  const workspaceScope = ready && me ? `${me.user_id}:${me.principal_kind}:${me.workspace_expires_at || ""}:${status.canClaim}` : null;
  return <SessionContext.Provider value={{ me, ready, status, refresh }}><WorkspaceCacheProvider scope={workspaceScope} checkIdentity={refresh}><GapDiscoveryProvider viewer={ready ? workspaceScope || "visitor" : null}>
    <a className="skip" href="#main">Skip to content</a>
    <header className="site-nav workspace-chrome">
      <nav className="site-information" aria-label="Community"><Link href="/about" aria-current={pathname === "/about" ? "page" : undefined}>About</Link><span className="site-information-divider" aria-hidden="true">|</span><Link href="/leaderboard" aria-current={pathname === "/leaderboard" ? "page" : undefined}>Leaderboard</Link></nav>
      <div className="avatar-area" ref={menuArea}><button className="avatar" aria-label="Your workspace" aria-expanded={menu} onClick={() => setMenu(!menu)}>{me?.display_name?.slice(0, 1).toUpperCase() || <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2" aria-hidden="true"><circle cx="12" cy="8" r="3.2" /><path d="M5 20v-2a7 7 0 0 1 14 0v2" /></svg>}</button>
        {menu && <div className="avatar-menu workspace-menu" onKeyDown={e => { if (e.key === "Escape") setMenu(false); }}>
          <p className="workspace-menu-title">{me?.display_name || (me ? "Anonymous workspace" : "Your workspace")}</p>
          <nav><Link onClick={() => setMenu(false)} href="/workspace?tab=gaps">Knowledge gaps</Link><Link onClick={() => setMenu(false)} href="/workspace?tab=accounts">Scientific accounts</Link><Link onClick={() => setMenu(false)} href="/workspace?tab=drafts">Saved drafts</Link><Link onClick={() => setMenu(false)} href="/workspace?tab=explorations">Explorations</Link><Link onClick={() => setMenu(false)} href="/workspace?tab=runs">Research runs</Link></nav>
          {status.canAdmin && <nav><Link onClick={() => setMenu(false)} href="/admin">Admin telemetry</Link></nav>}
          {me?.principal_kind === "anonymous" && <small className="workspace-session-note">Anonymous access ends {me.workspace_expires_at ? new Date(me.workspace_expires_at).toLocaleDateString() : "with this session"}.</small>}
          {me?.principal_kind !== "registered" && <div className="workspace-signin"><small>Keep your work across devices.</small><ProviderButtons disabled={!ready} compact /></div>}
          {canClaim && claimDismissed && <button type="button" className="text-button workspace-claim" onClick={() => { setMenu(false); void claim(); }}>Move my anonymous work</button>}
          {me && <button className="text-button workspace-signout" onClick={logout}>Sign out</button>}
        </div>}
      </div>
    </header>
    {pathname !== "/research/connect" && canClaim && claimPromptKnown && !claimDismissed && <div className="continuity continuity-prompt"><button type="button" className="continuity-dismiss" aria-label="Dismiss anonymous work prompt" title="Dismiss for this session" onClick={dismissClaim}><span aria-hidden="true">×</span></button><p>Keep the work from your anonymous session in this signed-in workspace?</p><button type="button" onClick={claim}>Move my anonymous work</button><small>Existing scientific identities and attribution stay unchanged. You can also move this work later from your workspace menu.</small></div>}
    {claimMessage && <p role="status" className="notice">{claimMessage}</p>}
    {children}
  </GapDiscoveryProvider></WorkspaceCacheProvider></SessionContext.Provider>;
}
export function ProviderButtons({ disabled = false, onLogin, compact = false }: { disabled?: boolean; onLogin?: (provider: "google" | "orcid") => void; compact?: boolean }) {
  const { status, ready } = useIdentity(); const noteId = useId();
  const unavailable = (["orcid", "google"] as const).filter(provider => !status.providers[provider]).map(provider => provider === "orcid" ? "ORCID" : "Google");
  const note = !ready ? "Checking sign-in options…" : unavailable.length ? `${unavailable.join(" and ")} sign-in unavailable.` : null;
  return <div className={`provider-options${compact ? " compact" : ""}`}>{(["orcid", "google"] as const).map(provider => <div key={provider}>
    <button className="provider" aria-label={`Continue with ${provider === "orcid" ? "ORCID" : "Google"}`} aria-describedby={compact && note && (disabled || !status.providers[provider]) ? noteId : undefined} disabled={disabled || !status.providers[provider]} onClick={() => { if (onLogin) onLogin(provider); else void signIn(provider, { callbackUrl: window.location.origin + "/" }); }}>
      <span aria-hidden="true">{provider === "orcid" ? "iD" : "G"}</span>{!compact && "Continue with "}{provider === "orcid" ? "ORCID" : "Google"}
    </button>{!compact && !status.providers[provider] && <small>{provider === "orcid" ? "ORCID" : "Google"} sign-in is not configured.</small>}
  </div>)}{compact && note && <small className="provider-note" id={noteId}>{note}</small>}</div>;
}
