import { adminProxy } from "@/lib/admin-proxy";
import { readMintRequest } from "@/lib/admin-api-keys";
import { checkOrigin, gatewayProblem } from "@/lib/gateway";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  return adminProxy("api-keys", "", { purpose: "admin_api_keys" });
}
// Mints one key. The response holds the raw key once and is never cached or logged; the backend stores only its hash.
export async function POST(request: Request) {
  if (!checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "Invalid request origin");
  const read = await readMintRequest(request);
  if (!read.ok) return Response.json({ error: read.error }, { status: read.status, headers: { "Cache-Control": "private, no-store" } });
  return adminProxy("api-keys", "", { method: "POST", body: read.request, purpose: "admin_api_keys" });
}
