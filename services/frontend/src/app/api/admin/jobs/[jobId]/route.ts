import { adminProxy } from "@/lib/admin-proxy";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request, context: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await context.params;
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(jobId)) {
    return Response.json({ error: "Unknown job." }, { status: 404, headers: { "Cache-Control": "private, no-store" } });
  }
  const input = new URL(request.url).searchParams;
  const search = new URLSearchParams();
  for (const name of ["before", "limit"]) { const value = input.get(name); if (value !== null) search.set(name, value); }
  return adminProxy(`jobs/${jobId}`, `?${search}`);
}
