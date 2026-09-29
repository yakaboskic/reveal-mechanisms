import { adminProxy } from "@/lib/admin-proxy";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export async function GET(request: Request, context: { params: Promise<{ table: string; view?: string[] }> }) {
  const { table, view = [] } = await context.params;
  if (!/^[a-z_]+$/.test(table) || view.length > 1 || (view.length && !["row", "cell"].includes(view[0]))) {
    return Response.json({ error: "Unknown table route." }, { status: 404, headers: { "Cache-Control": "private, no-store" } });
  }
  const allowed = view[0] === "cell" ? ["key", "column", "offset", "digest"] : view[0] === "row" ? ["key"] : ["limit", "cursor", "column", "operator", "q"];
  const search = new URLSearchParams();
  const input = new URL(request.url).searchParams;
  for (const name of allowed) { const value = input.get(name); if (value !== null) search.set(name, value); }
  return adminProxy(`tables/${table}${view.length ? "/" + view[0] : ""}`, `?${search}`);
}
