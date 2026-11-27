import { adminProxy } from "@/lib/admin-proxy";
import { readBudgetRequest } from "@/lib/admin-api-keys";
import { checkOrigin, gatewayProblem } from "@/lib/gateway";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const headers = { "Cache-Control": "private, no-store" };
// Sets, changes or removes one minted key's budget; it applies to the next job the key starts.
export async function PUT(request: Request, { params }: { params: Promise<{ keyId: string }> }) {
  if (!checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "Invalid request origin");
  const { keyId } = await params;
  if (!/^[0-9a-f-]{36}$/.test(keyId)) return Response.json({ error: "No key has that id." }, { status: 404, headers });
  const read = await readBudgetRequest(request);
  if (!read.ok) return Response.json({ error: read.error }, { status: read.status, headers });
  return adminProxy(`api-keys/${keyId}/budget`, "", { method: "PUT", body: read.request, purpose: "admin_api_keys" });
}
