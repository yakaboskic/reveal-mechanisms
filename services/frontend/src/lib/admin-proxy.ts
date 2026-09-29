import "server-only";
import { adminAccess } from "./admin";
import { assertion, backendUrl } from "./gateway";

export async function adminProxy(path: string, search = "") {
  const headers = { "Cache-Control": "private, no-store", Vary: "Cookie" };
  try {
    const access = await adminAccess();
    if (!access.allowed) return Response.json({ error: "Admin access required." }, { status: access.signedIn ? 403 : 401, headers });
    const proof = await assertion({ user_id: "admin-console", principal_kind: "registered" }, { purpose: "admin_telemetry" });
    const upstream = await fetch(`${backendUrl()}/internal/v1/admin/${path}${search}`, {
      headers: { Authorization: `Bearer ${process.env.REVEAL_GATEWAY_SERVICE_TOKEN || ""}`, "X-Reveal-Admin-Assertion": proof },
      cache: "no-store", signal: AbortSignal.timeout(20000),
    });
    const body = await upstream.json();
    if (!upstream.ok) {
      const detail = (upstream.status < 500 || body.code === "INSPECTION_TIMEOUT") && typeof body.detail === "string" ? body.detail : "The database is unavailable. Retry shortly.";
      return Response.json({ error: detail }, { status: upstream.status, headers });
    }
    return Response.json(body, { headers });
  } catch { return Response.json({ error: "The database is unavailable. Check the API connection, then retry." }, { status: 503, headers }); }
}
