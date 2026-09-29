import "server-only";
import { SignJWT, jwtVerify } from "jose";
import { cookies } from "next/headers";
import { getServerSession } from "next-auth";
import { authOptions } from "./auth";
import { IdentityServiceError } from "./identity-recovery";

export type Principal = { user_id: string; principal_kind: "anonymous" | "registered"; workspace_expires_at?: string };
export type VerifiedIdentity = { issuer: string; subject: string; display_name: string | null; email: string | null; email_verified: boolean; orcid: string | null; orcid_authenticated: boolean };
export const anonymousCookie = "reveal-anonymous";
export const backendUrl = () => process.env.REVEAL_API_URL || "http://127.0.0.1:8000";
const secret = (name: string) => {
  const value = process.env[name];
  if (!value || value.length < 32) throw new Error(`${name} must contain at least 32 characters`);
  return new TextEncoder().encode(value);
};
export async function assertion(principal: Principal, extra: Record<string, unknown> = {}) {
  return new SignJWT({ principal_kind: principal.principal_kind, ...extra })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" }).setSubject(principal.user_id)
    .setIssuer(process.env.REVEAL_GATEWAY_ISSUER || "reveal-nextjs")
    .setAudience(process.env.REVEAL_GATEWAY_AUDIENCE || "reveal-api")
    .setIssuedAt().setExpirationTime("5m").setJti(crypto.randomUUID())
    .sign(secret("REVEAL_GATEWAY_SECRET"));
}
export async function serviceRequest<T>(path: string, body: unknown, key = crypto.randomUUID(), extraHeaders: Record<string, string> = {}): Promise<T> {
  if (!process.env.REVEAL_GATEWAY_SERVICE_TOKEN) throw new Error("REVEAL_GATEWAY_SERVICE_TOKEN is required");
  const response = await fetch(`${backendUrl()}/internal/v1/${path}`, {
    method: "POST", headers: { "content-type": "application/json", Authorization: `Bearer ${process.env.REVEAL_GATEWAY_SERVICE_TOKEN}`, "Idempotency-Key": key, ...extraHeaders },
    body: JSON.stringify(body), cache: "no-store", signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    throw new IdentityServiceError(response.status, typeof problem?.code === "string" ? problem.code : null);
  }
  return response.json();
}
export async function readAnonymous(): Promise<Principal | null> {
  const token = (await cookies()).get(anonymousCookie)?.value;
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, secret("AUTH_SECRET"), { algorithms: ["HS256"], issuer: "reveal-browser", audience: "reveal-anonymous" });
    if (typeof payload.sub !== "string" || payload.principal_kind !== "anonymous") return null;
    return { user_id: payload.sub, principal_kind: "anonymous", workspace_expires_at: new Date(payload.exp! * 1000).toISOString() };
  } catch { return null; }
}
export async function writeAnonymous(principal: Principal) {
  const expiry = Math.floor(new Date(principal.workspace_expires_at!).getTime() / 1000);
  const token = await new SignJWT({ principal_kind: "anonymous" }).setProtectedHeader({ alg: "HS256" })
    .setSubject(principal.user_id).setIssuer("reveal-browser").setAudience("reveal-anonymous")
    .setIssuedAt().setExpirationTime(expiry).sign(secret("AUTH_SECRET"));
  (await cookies()).set(anonymousCookie, token, { httpOnly: true, sameSite: "lax", secure: process.env.NODE_ENV === "production", path: "/", expires: new Date(expiry * 1000) });
}
export async function currentPrincipal(): Promise<Principal | null> {
  const session = await getServerSession(authOptions);
  if (session?.principal) return session.principal;
  return readAnonymous();
}
export function checkOrigin(request: Request) {
  const origin = request.headers.get("origin");
  // Browsers send Origin for every same-origin POST/PATCH; never accept a missing value.
  const expected = new URL(process.env.NEXTAUTH_URL || request.url).origin;
  return origin === expected || (origin === new URL(request.url).origin && process.env.NODE_ENV !== "production");
}
export function gatewayProblem(status: number, code: string, detail: string) {
  return Response.json({ type: "about:blank", title: code, status, code, detail }, { status });
}
