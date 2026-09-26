import { assertion, backendUrl, checkOrigin, currentPrincipal, gatewayProblem, serviceRequest, writeAnonymous, type Principal } from "@/lib/gateway";
export async function POST(request: Request) {
  if (!checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "Invalid request origin");
  const key = request.headers.get("Idempotency-Key");
  if (!key) return gatewayProblem(400, "IDEMPOTENCY_KEY_REQUIRED", "A retry key is required");
  try {
    let principal = await currentPrincipal();
    if (!principal) { principal = await serviceRequest<Principal>("principals/anonymous", {}, key); await writeAnonymous(principal); }
    const me = await fetch(`${backendUrl()}/v1/me`, { headers: { Authorization: `Bearer ${await assertion(principal)}` }, cache: "no-store" });
    return new Response(me.body, { status: me.ok ? 201 : me.status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  } catch { return gatewayProblem(503, "SESSION_UNAVAILABLE", "Anonymous continuation is temporarily unavailable. Please retry."); }
}
