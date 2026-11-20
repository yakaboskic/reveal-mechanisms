import { adminProxy } from "@/lib/admin-proxy";
import { readLimitsChange } from "@/lib/admin-limits";
import { checkOrigin, gatewayProblem } from "@/lib/gateway";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  return adminProxy("agent-limits", "", { purpose: "admin_settings" });
}
// The console's only write. The admin gate and the signed, purpose-bound assertion are applied by adminProxy.
export async function PUT(request: Request) {
  if (!checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "Invalid request origin");
  const read = await readLimitsChange(request);
  if (!read.ok) return Response.json({ error: read.error }, { status: read.status, headers: { "Cache-Control": "private, no-store" } });
  return adminProxy("agent-limits", "", { method: "PUT", body: read.change, purpose: "admin_settings" });
}
