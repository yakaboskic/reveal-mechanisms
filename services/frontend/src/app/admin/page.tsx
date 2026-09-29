import { adminAccess } from "@/lib/admin";
import { AdminConsole, AdminLogin } from "@/components/AdminConsole";
import "./admin.css";
export const dynamic = "force-dynamic";
export const metadata = { title: "Admin telemetry | REVEAL", robots: { index: false, follow: false } };
export default async function AdminPage() {
  const access = await adminAccess();
  return <main id="main" className="admin-console">
    <header className="admin-heading"><div><a href="/">REVEAL Mechanisms</a><h1>Application observatory</h1><p>Database activity, scientific data, and agent execution.</p></div><span className="admin-label">Admin console</span></header>
    {access.allowed ? <AdminConsole bypass={access.bypass} /> : <AdminLogin signedIn={access.signedIn} />}
  </main>;
}
