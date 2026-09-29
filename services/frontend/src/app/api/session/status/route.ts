import { currentPrincipal, readAnonymous } from "@/lib/gateway";
import { adminAccess } from "@/lib/admin";
export async function GET() {
  const principal = await currentPrincipal();
  const anonymous = await readAnonymous();
  const admin = await adminAccess();
  return Response.json({ principal, canAdmin: admin.allowed, canClaim: principal?.principal_kind === "registered" && !!anonymous && anonymous.user_id !== principal.user_id,
    providers: { google: !!(process.env.AUTH_GOOGLE_ID && process.env.AUTH_GOOGLE_SECRET), orcid: !!(process.env.AUTH_ORCID_ID && process.env.AUTH_ORCID_SECRET) } }, { headers: { "cache-control": "no-store" } });
}
