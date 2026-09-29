import { adminAccess } from "@/lib/admin";
import { assertion, backendUrl } from "@/lib/gateway";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export async function GET() {
  const headers = { "Cache-Control": "private, no-store", "Vary": "Cookie" };
  try {
    const access = await adminAccess();
    if (!access.allowed) return Response.json({ error: "Admin access required." }, { status: access.signedIn ? 403 : 401, headers });
    const proof = await assertion({ user_id: "admin-console", principal_kind: "registered" }, { purpose: "admin_telemetry" });
    const response = await fetch(`${backendUrl()}/internal/v1/admin/telemetry`, {
      headers: { Authorization: `Bearer ${process.env.REVEAL_GATEWAY_SERVICE_TOKEN || ""}`, "X-Reveal-Admin-Assertion": proof },
      cache: "no-store", signal: AbortSignal.timeout(20000),
    });
    if (!response.ok) throw new Error("Telemetry unavailable");
    return Response.json(await response.json(), { headers });
  } catch { return Response.json({ error: "Telemetry is unavailable. Check the API and database connection, then retry." }, { status: 503, headers }); }
}
