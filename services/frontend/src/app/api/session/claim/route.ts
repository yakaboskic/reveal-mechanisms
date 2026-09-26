import { getToken } from "next-auth/jwt";
import { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { anonymousCookie, assertion, checkOrigin, gatewayProblem, readAnonymous, serviceRequest } from "@/lib/gateway";
export async function POST(request: NextRequest) {
  if (!checkOrigin(request)) return gatewayProblem(403, "CSRF_REJECTED", "Invalid request origin");
  const token = await getToken({ req: request, secret: process.env.AUTH_SECRET });
  const source = await readAnonymous();
  if (!source || !token?.principal || !token.verifiedIdentity || !token.loginObservedAt || Date.now() - token.loginObservedAt > 10 * 60 * 1000) return gatewayProblem(401, "INVALID_IDENTITY_PROOF", "Sign in again to move this anonymous workspace.");
  if ((await request.json()).consent !== true) return gatewayProblem(403, "CLAIM_CONSENT_REQUIRED", "Consent is required to move your anonymous work.");
  try {
    const result = await serviceRequest("principals/claim", {
      anonymous_session_assertion: await assertion(source, { purpose: "anonymous_session" }),
      verified_login_assertion: await assertion(token.principal, { purpose: "verified_identity", verified_identity: token.verifiedIdentity }), consent: true,
    }, request.headers.get("Idempotency-Key") || crypto.randomUUID());
    (await cookies()).delete(anonymousCookie);
    return Response.json(result);
  } catch { return gatewayProblem(409, "CLAIM_FAILED", "The workspace could not be moved. Your original records are retained."); }
}
