import "server-only";
import { adminAccess } from "./admin";
import { adminActor } from "./admin-policy";
import { assertion, backendUrl } from "./gateway";

/** purpose "admin_settings" is the backend's separate authority for console settings; its assertion also names the admin. */
type AdminRequest = { method?: "GET" | "PUT"; body?: unknown; purpose?: "admin_telemetry" | "admin_settings" };

export async function adminProxy(path: string, search = "", { method = "GET", body, purpose = "admin_telemetry" }: AdminRequest = {}) {
  const headers = { "Cache-Control": "private, no-store", Vary: "Cookie" };
  try {
    const access = await adminAccess();
    if (!access.allowed) return Response.json({ error: "Admin access required." }, { status: access.signedIn ? 403 : 401, headers });
    const claims: Record<string, unknown> = { purpose };
    if (purpose === "admin_settings") claims.admin_email = adminActor(access);
    const proof = await assertion({ user_id: "admin-console", principal_kind: "registered" }, claims);
    const upstream = await fetch(`${backendUrl()}/internal/v1/admin/${path}${search}`, {
      method, body: body === undefined ? undefined : JSON.stringify(body),
      headers: { Authorization: `Bearer ${process.env.REVEAL_GATEWAY_SERVICE_TOKEN || ""}`, "X-Reveal-Admin-Assertion": proof, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      cache: "no-store", signal: AbortSignal.timeout(20000),
    });
    const result = await upstream.json();
    if (!upstream.ok) {
      const detail = (upstream.status < 500 || result.code === "INSPECTION_TIMEOUT") && typeof result.detail === "string" ? result.detail : "The database is unavailable. Retry shortly.";
      return Response.json({ error: detail }, { status: upstream.status, headers });
    }
    return Response.json(result, { headers });
  } catch { return Response.json({ error: "The database is unavailable. Check the API connection, then retry." }, { status: 503, headers }); }
}
