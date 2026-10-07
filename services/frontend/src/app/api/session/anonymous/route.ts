import { assertion, backendUrl, checkOrigin, currentPrincipal, gatewayProblem, serviceRequest, writeAnonymous, type Principal } from "@/lib/gateway";
import { provisionalMe } from "@/lib/session-identity";
export async function POST(request: Request) {
  if (!checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "Invalid request origin");
  const key = request.headers.get("Idempotency-Key");
  if (!key) return gatewayProblem(400, "IDEMPOTENCY_KEY_REQUIRED", "A retry key is required");
  try {
    const principal = await currentPrincipal();
    if (!principal) {
      const created = await serviceRequest<Principal>("principals/anonymous", {}, key); await writeAnonymous(created);
      // A just-provisioned anonymous principal has no profile, so this is exactly its Me without another GET /v1/me.
      return Response.json(provisionalMe(created), { status: 201, headers: { "cache-control": "no-store" } });
    }
    const me = await fetch(`${backendUrl()}/v1/me`, { headers: { Authorization: `Bearer ${await assertion(principal)}` }, cache: "no-store" });
    return new Response(me.body, { status: me.ok ? 201 : me.status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  } catch { return gatewayProblem(503, "SESSION_UNAVAILABLE", "Anonymous continuation is temporarily unavailable. Please retry."); }
}
