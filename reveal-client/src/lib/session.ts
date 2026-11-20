/** Separate server-owned guest workspaces for hosting, plus the loopback-only demo. */
import { SignJWT, jwtVerify } from "jose";
import { configuration, createAnonymousPrincipal, errorResponse, GatewayError, loopback, readMe, resolvePrincipal } from "./gateway";
import type { Environment, Fetcher, GatewayConfiguration, Me, Principal } from "./gateway";

export const sessionCookie = "reveal-client-session";
const guestLifetime = 30 * 24 * 60 * 60;
export type DemoConfiguration = GatewayConfiguration & { mode: "demo"; authSecret: string; identity: { issuer: string; subject: string } };
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
  return { ...config, mode: "demo", authSecret: env.AUTH_SECRET, identity: { issuer: env.DEMO_IDENTITY_ISSUER, subject: env.DEMO_IDENTITY_SUBJECT } };
}
export type GuestConfiguration = GatewayConfiguration & { mode: "guest"; authSecret: string };
export type SessionConfiguration = DemoConfiguration | GuestConfiguration;
export function sessionConfiguration(request: Request, env: Environment = process.env): SessionConfiguration {
  if (!env.REVEAL_SESSION_MODE || env.REVEAL_SESSION_MODE === "demo") return demoConfiguration(request, env);
  if (env.REVEAL_SESSION_MODE !== "guest") throw new GatewayError(503, "SETUP_REQUIRED", "The configured session mode is unsupported.");
  const config = configuration(env);
  const expected = new URL(config.appOrigin), actual = new URL(request.url);
  if (expected.protocol !== "https:" || actual.origin !== expected.origin ||
      (request.headers.get("host") && request.headers.get("host") !== expected.host)) {
    throw new GatewayError(503, "SESSION_DISABLED", "Guest sessions require this application's configured HTTPS origin.");
  }
  if (!env.AUTH_SECRET || env.AUTH_SECRET.length < 32) throw new GatewayError(503, "SETUP_REQUIRED", "A private session signing key is required.");
  return { ...config, mode: "guest", authSecret: env.AUTH_SECRET };
}
const audience = (config: SessionConfiguration) => config.mode === "demo"
  ? JSON.stringify([config.appOrigin, config.backendUrl, config.identity.issuer, config.identity.subject])
  : JSON.stringify([config.appOrigin, config.backendUrl, "guest"]);
const issuer = (config: SessionConfiguration) => config.mode === "guest" ? "reveal-client-guest" : "reveal-client-demo";
export async function readSession(request: Request, config: SessionConfiguration): Promise<Principal | null> {
  const cookie = request.headers.get("cookie")?.split(";").map(part => part.trim()).find(part => part.startsWith(sessionCookie + "="));
  if (!cookie) return null;
  try {
    const { payload } = await jwtVerify(cookie.slice(sessionCookie.length + 1), new TextEncoder().encode(config.authSecret), {
      algorithms: ["HS256"], issuer: issuer(config), audience: audience(config), requiredClaims: ["exp", "iat", "sub"],
    });
    const kind = config.mode === "guest" ? "anonymous" : "registered";
    const lifetime = Number(payload.exp) - Number(payload.iat);
    if (payload.principal_kind !== kind || !/^[a-f0-9-]{36}$/.test(payload.sub || "") ||
        lifetime <= 0 || lifetime > (config.mode === "guest" ? guestLifetime : 43200)) return null;
    return { user_id: payload.sub!, principal_kind: kind };
  } catch { return null; }
}
function sessionExpiry(principal: Me, config: SessionConfiguration): number {
  const now = Math.floor(Date.now() / 1000);
  if (config.mode === "demo") return now + 43200;
  const expiry = Math.floor(Date.parse(principal.workspace_expires_at || "") / 1000);
  if (principal.principal_kind !== "anonymous" || !Number.isFinite(expiry) || expiry <= now) {
    throw new GatewayError(502, "IDENTITY_UNAVAILABLE", "The research API returned an invalid guest workspace expiry.");
  }
  return Math.min(expiry, now + guestLifetime);
}
async function cookieValue(principal: Principal, config: SessionConfiguration, expiry: number): Promise<string> {
  return new SignJWT({ principal_kind: principal.principal_kind }).setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(principal.user_id).setIssuer(issuer(config)).setAudience(audience(config))
    .setIssuedAt().setExpirationTime(expiry).sign(new TextEncoder().encode(config.authSecret));
}
function cookieHeader(value: string, config: SessionConfiguration, expiry = 0): string {
  const maxAge = Math.max(0, expiry - Math.floor(Date.now() / 1000));
  return `${sessionCookie}=${value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${maxAge}${config.appOrigin.startsWith("https:") ? "; Secure" : ""}`;
}
export async function handleSession(request: Request, env: Environment = process.env, fetcher: Fetcher = fetch): Promise<Response> {
  try {
    const config = sessionConfiguration(request, env);
    if (!["GET", "POST", "DELETE"].includes(request.method)) throw new GatewayError(405, "METHOD_NOT_ALLOWED", "Use GET, POST or DELETE.");
    if (request.method !== "GET" && request.headers.get("origin") !== config.appOrigin) throw new GatewayError(403, "CSRF_REJECTED", "The request must come from this application.");
    const headers = new Headers({ "Cache-Control": "private, no-store" });
    if (request.method === "DELETE") {
      headers.set("Set-Cookie", cookieHeader("", config));
      return Response.json({ principal: null }, { headers });
    }
    if (request.method === "POST") {
      const raw = await request.text(); let body;
      try { body = raw ? JSON.parse(raw) : {}; } catch { throw new GatewayError(422, "INVALID_REQUEST", "Connect accepts only an empty JSON object."); }
      if (!body || Array.isArray(body) || typeof body !== "object" || Object.keys(body).length) throw new GatewayError(422, "INVALID_REQUEST", "Identity is managed on the server; connect accepts only an empty JSON object.");
      let principal: Me | null = null;
      if (config.mode === "guest") {
        const session = await readSession(request, config);
        if (session) {
          try { principal = await readMe(session, config, fetcher); }
          catch (error) { if (!(error instanceof GatewayError) || error.status !== 401) throw error; }
        }
        principal ??= await createAnonymousPrincipal(config, fetcher);
      } else principal = await resolvePrincipal(config.identity, config, fetcher);
      const expiry = sessionExpiry(principal, config);
      headers.set("Set-Cookie", cookieHeader(await cookieValue(principal, config, expiry), config, expiry));
      return Response.json({ principal }, { headers });
    }
    const session = await readSession(request, config);
    if (!session) return Response.json({ principal: null }, { headers });
    try { return Response.json({ principal: await readMe(session, config, fetcher) }, { headers }); }
    catch (error) {
      if (!(error instanceof GatewayError) || error.status !== 401) throw error;
      headers.set("Set-Cookie", cookieHeader("", config));
      return Response.json({ principal: null }, { headers });
    }
  } catch (error) { return errorResponse(error); }
}
