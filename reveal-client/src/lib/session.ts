/** Loopback-only demonstration adapter. Hosted applications supply their own login. */
import { SignJWT, jwtVerify } from "jose";
import { configuration, errorResponse, GatewayError, loopback, readMe, resolvePrincipal } from "./gateway";
import type { Environment, Fetcher, GatewayConfiguration, Principal } from "./gateway";

export const sessionCookie = "reveal-client-session";
export type DemoConfiguration = GatewayConfiguration & { authSecret: string; identity: { issuer: string; subject: string } };
export function demoConfiguration(request: Request, env: Environment = process.env): DemoConfiguration {
  const config = configuration(env);
  const expected = new URL(config.appOrigin), actual = new URL(request.url);
  if (env.REVEAL_DEMO_MODE !== "true" || !loopback(expected.hostname) || actual.origin !== expected.origin ||
      (request.headers.get("host") && request.headers.get("host") !== expected.host)) {
    throw new GatewayError(503, "DEMO_DISABLED", "This fixed-identity demo runs only on its configured loopback origin. A hosted app must provide its own session adapter.");
  }
  if (!env.AUTH_SECRET || env.AUTH_SECRET.length < 32 || !env.DEMO_IDENTITY_ISSUER?.trim() || !env.DEMO_IDENTITY_SUBJECT?.trim()) {
    throw new GatewayError(503, "SETUP_REQUIRED", "Configure this client with npm run setup before connecting.");
  }
  try { new URL(env.DEMO_IDENTITY_ISSUER); } catch { throw new GatewayError(503, "SETUP_REQUIRED", "The configured demo identity issuer must be a URI."); }
  return { ...config, authSecret: env.AUTH_SECRET, identity: { issuer: env.DEMO_IDENTITY_ISSUER, subject: env.DEMO_IDENTITY_SUBJECT } };
}
const audience = (config: DemoConfiguration) => JSON.stringify([config.appOrigin, config.backendUrl, config.identity.issuer, config.identity.subject]);
export async function readSession(request: Request, config: DemoConfiguration): Promise<Principal | null> {
  const cookie = request.headers.get("cookie")?.split(";").map(part => part.trim()).find(part => part.startsWith(sessionCookie + "="));
  if (!cookie) return null;
  try {
    const { payload } = await jwtVerify(cookie.slice(sessionCookie.length + 1), new TextEncoder().encode(config.authSecret), {
      algorithms: ["HS256"], issuer: "reveal-client-demo", audience: audience(config), requiredClaims: ["exp", "iat", "sub"],
    });
    if (payload.principal_kind !== "registered" || !/^[a-f0-9-]{36}$/.test(payload.sub || "") || Number(payload.exp) - Number(payload.iat) > 43200) return null;
    return { user_id: payload.sub!, principal_kind: "registered" };
  } catch { return null; }
}
async function cookieValue(principal: Principal, config: DemoConfiguration): Promise<string> {
  return new SignJWT({ principal_kind: principal.principal_kind }).setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(principal.user_id).setIssuer("reveal-client-demo").setAudience(audience(config))
    .setIssuedAt().setExpirationTime("12h").sign(new TextEncoder().encode(config.authSecret));
}
function cookieHeader(value: string, config: DemoConfiguration, clear = false): string {
  return `${sessionCookie}=${value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${clear ? 0 : 43200}${config.appOrigin.startsWith("https:") ? "; Secure" : ""}`;
}
export async function handleSession(request: Request, env: Environment = process.env, fetcher: Fetcher = fetch): Promise<Response> {
  try {
    const config = demoConfiguration(request, env);
    if (!["GET", "POST", "DELETE"].includes(request.method)) throw new GatewayError(405, "METHOD_NOT_ALLOWED", "Use GET, POST or DELETE.");
    if (request.method !== "GET" && request.headers.get("origin") !== config.appOrigin) throw new GatewayError(403, "CSRF_REJECTED", "The request must come from this application.");
    const headers = new Headers({ "Cache-Control": "private, no-store" });
    if (request.method === "DELETE") {
      headers.set("Set-Cookie", cookieHeader("", config, true));
      return Response.json({ principal: null }, { headers });
    }
    if (request.method === "POST") {
      const raw = await request.text(); let body;
      try { body = raw ? JSON.parse(raw) : {}; } catch { throw new GatewayError(422, "INVALID_REQUEST", "Connect accepts only an empty JSON object."); }
      if (!body || Array.isArray(body) || typeof body !== "object" || Object.keys(body).length) throw new GatewayError(422, "INVALID_REQUEST", "The demo identity is configured on the server; connect accepts only an empty JSON object.");
      const principal = await resolvePrincipal(config.identity, config, fetcher);
      headers.set("Set-Cookie", cookieHeader(await cookieValue(principal, config), config));
      return Response.json({ principal }, { headers });
    }
    const session = await readSession(request, config);
    if (!session) return Response.json({ principal: null }, { headers });
    try { return Response.json({ principal: await readMe(session, config, fetcher) }, { headers }); }
    catch (error) {
      if (!(error instanceof GatewayError) || error.status !== 401) throw error;
      headers.set("Set-Cookie", cookieHeader("", config, true));
      return Response.json({ principal: null }, { headers });
    }
  } catch (error) { return errorResponse(error); }
}
