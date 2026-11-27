"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React from "react";
import Link from "next/link";
import { signOut } from "next-auth/react";

export const ADMIN_NAV = [
  { href: "/admin", label: "Overview" },
  { href: "/admin/jobs", label: "Jobs" },
  { href: "/admin/agent-jobs", label: "Agent jobs" },
  { href: "/admin/costs", label: "Costs" },
  { href: "/admin/database", label: "Database" },
  { href: "/admin/activity", label: "Activity" },
  { href: "/admin/performance", label: "Performance" },
  { href: "/admin/limits", label: "Limits" },
  { href: "/admin/api-keys", label: "API keys" },
] as const;

/** The section a path belongs to: Overview only on /admin itself, the others on their own path and anything below. */
export function activeSection(pathname: string) {
  const path = pathname.replace(/\/+$/, "") || "/";
  return ADMIN_NAV.find(item => item.href !== "/admin" && (path === item.href || path.startsWith(`${item.href}/`))) ?? ADMIN_NAV[0];
}

/** The 224px sidebar: where you are, the sections, and who is signed in. */
export function AdminSidebar({ pathname, email, bypass }: { pathname: string; email: string | null; bypass: boolean }) {
  const current = activeSection(pathname);
  return <aside className="admin-sidebar">
    <div className="admin-brand"><Link href="/">REVEAL Mechanisms</Link><span>Admin console · Application observatory</span></div>
    <nav aria-label="Admin">{ADMIN_NAV.map(item => <Link key={item.href} href={item.href} aria-current={current.href === item.href ? "page" : undefined}>{item.label}</Link>)}</nav>
    <div className="admin-account">
      {bypass ? <><strong>Local development</strong><span>Sign-in is bypassed on this server.</span></>
        : <><strong>{email || "Signed-in admin"}</strong><button type="button" className="text-button" onClick={() => void signOut({ callbackUrl: "/admin" })}>Sign out</button></>}
    </div>
  </aside>;
}
