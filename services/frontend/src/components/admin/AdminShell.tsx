"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React, { Suspense, useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { signIn, signOut } from "next-auth/react";
import { useIdentity } from "../Session";
import { AdminLimits } from "../AdminLimits";
import { activeSection, AdminSidebar } from "./AdminNav";
import { ConsoleNotices, TelemetryControls, TelemetryProvider, useTelemetry } from "./Telemetry";

function Frame({ email, bypass, children }: { email: string | null; bypass: boolean; children: ReactNode }) {
  const pathname = usePathname();
  const telemetry = useTelemetry();
  const current = activeSection(pathname);
  const limits = current.href === "/admin/limits";
  // Limits is mounted on first visit and then kept: it owns its request and form, so moving to another page and back,
  // or a telemetry refresh, leaves unsaved edits alone.
  const [limitsOpened, setLimitsOpened] = useState(limits);
  useEffect(() => { if (limits) setLimitsOpened(true); }, [limits]);
  return <div className="admin-frame">
    <AdminSidebar pathname={pathname} email={email} bypass={bypass} />
    <div className="admin-content">
      <div className="admin-topbar"><span>{current.label}</span><TelemetryControls telemetry={telemetry} /></div>
      <main id="main" className="admin-main">
        <ConsoleNotices bypass={bypass} telemetry={telemetry} />
        <Suspense fallback={<p className="admin-loading" role="status">Loading…</p>}>{children}</Suspense>
        {limitsOpened && <AdminLimits active={limits} />}
      </main>
    </div>
  </div>;
}

/** Sidebar, top bar naming the section, and the shared telemetry snapshot. */
export function AdminShell({ email, bypass, children }: { email: string | null; bypass: boolean; children: ReactNode }) {
  return <TelemetryProvider><Frame email={email} bypass={bypass}>{children}</Frame></TelemetryProvider>;
}

export function AdminLogin({ signedIn }: { signedIn: boolean }) {
  const { status, ready } = useIdentity();
  return <main id="main" className="admin-gate">
    <Link href="/">REVEAL Mechanisms</Link>
    <h1>{signedIn ? "This account does not have admin access" : "Sign in to view telemetry"}</h1>
    <p>Access requires a verified Google or ORCID email listed in ADMIN_EMAILS. ORCID sign-in may not supply a verified email; use Google in that case.</p>
    <div className="admin-gate-actions">{(["google", "orcid"] as const).map(provider => <button className="submit" key={provider} disabled={!ready || !status.providers[provider]} onClick={() => void signIn(provider, { callbackUrl: "/admin" })}>Continue with {provider === "google" ? "Google" : "ORCID"}</button>)}</div>
    {ready && !status.providers.google && !status.providers.orcid && <p>No sign-in providers are configured.</p>}
    {signedIn && <button className="text-button" onClick={() => void signOut({ callbackUrl: "/admin" })}>Sign out</button>}
  </main>;
}
