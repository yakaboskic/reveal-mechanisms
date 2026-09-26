import { assertion, backendUrl, checkOrigin, currentPrincipal, gatewayProblem } from "@/lib/gateway";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
async function proxy(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  if (path[0] !== "v1" || path.some(p => p === "." || p === ".." || p.includes("/"))) return gatewayProblem(404, "NOT_FOUND", "Unknown API route");
  if (!["GET", "HEAD"].includes(request.method) && !checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "The request must come from this application.");
  try {
    const principal = await currentPrincipal();
    const headers = new Headers();
    for (const name of ["accept", "content-type", "idempotency-key", "last-event-id"]) {
      const value = request.headers.get(name); if (value) headers.set(name, value);
    }
    if (principal) headers.set("Authorization", `Bearer ${await assertion(principal)}`);
    const target = `${backendUrl()}/${path.map(encodeURIComponent).join("/")}${new URL(request.url).search}`;
    const upstream = await fetch(target, { method: request.method, headers, body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.text(), cache: "no-store", signal: request.signal });
    const output = new Headers({ "Cache-Control": "no-store", "X-Accel-Buffering": "no" });
    for (const name of ["content-type", "content-disposition", "x-content-type-options", "retry-after"]) { const value = upstream.headers.get(name); if (value) output.set(name, value); }
    return new Response(upstream.body, { status: upstream.status, headers: output });
  } catch { return gatewayProblem(502, "API_UNAVAILABLE", "The research API is unavailable. Your local edits are preserved; retry shortly."); }
}
export { proxy as GET, proxy as POST, proxy as PATCH };
