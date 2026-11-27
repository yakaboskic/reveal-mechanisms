import { adminProxy } from "@/lib/admin-proxy";
import { checkOrigin, gatewayProblem } from "@/lib/gateway";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request: Request, { params }: { params: Promise<{ keyId: string }> }) {
  if (!checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "Invalid request origin");
  const { keyId } = await params;
  if (!/^[0-9a-f-]{36}$/.test(keyId)) return Response.json({ error: "No key has that id." }, { status: 404, headers: { "Cache-Control": "private, no-store" } });
  return adminProxy(`api-keys/${keyId}/revoke`, "", { method: "POST", purpose: "admin_api_keys" });
}
