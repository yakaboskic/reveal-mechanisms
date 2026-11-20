import type { Metadata } from "next";
import { adminAccess } from "@/lib/admin";
import { AdminLogin, AdminShell } from "@/components/admin/AdminShell";
import "./admin.css";

// Every /admin/* page is behind this gate; the admin API proxies check again.
export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Admin telemetry | REVEAL", robots: { index: false, follow: false } };

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const access = await adminAccess();
  return <div className="admin-root">
    {access.allowed ? <AdminShell email={access.email} bypass={access.bypass}>{children}</AdminShell> : <AdminLogin signedIn={access.signedIn} />}
  </div>;
}
