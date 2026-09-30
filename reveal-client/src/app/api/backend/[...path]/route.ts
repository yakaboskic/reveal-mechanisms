import { errorResponse, proxyRequest } from "@/lib/gateway";
import { demoConfiguration, readSession } from "@/lib/session";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 300;
async function proxy(request: Request, context: { params: Promise<{ path: string[] }> }) {
  try {
    const config = demoConfiguration(request);
    return proxyRequest(request, (await context.params).path, await readSession(request, config), config);
  } catch (error) { return errorResponse(error); }
}
export { proxy as GET, proxy as HEAD, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
