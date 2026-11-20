import { adminProxy } from "@/lib/admin-proxy";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// One agent job's prompt, limits and response. Its own purpose-bound assertion names the admin; the backend records
// every read and refuses one it cannot record.
export async function GET(_request: Request, context: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await context.params;
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(jobId)) {
    return Response.json({ error: "Unknown job." }, { status: 404, headers: { "Cache-Control": "private, no-store" } });
  }
  return adminProxy(`jobs/${jobId}/run`, "", { purpose: "admin_content" });
}
