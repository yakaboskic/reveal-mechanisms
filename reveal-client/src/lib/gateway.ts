/** Server-side Web API adapter; no Next dependency or browser session policy. */
import { SignJWT } from "jose";
import type { components } from "./api.generated";

export type Me = components["schemas"]["Me"];
export type Principal = Pick<Me, "user_id" | "principal_kind">;
export type Environment = Record<string, string | undefined>;
export type Fetcher = typeof fetch;
export type GatewayConfiguration = {
  backendUrl: string; secret: string; serviceToken: string; issuer: string; audience: string;
  appOrigin: string; artifactBaseUrls: string | undefined;
};
export class GatewayError extends Error {
  constructor(readonly status: number, readonly code: string, detail: string) { super(detail); }
}
export const loopback = (hostname: string) => ["localhost", "127.0.0.1", "[::1]"].includes(hostname);

export function configuration(env: Environment = process.env): GatewayConfiguration {
  try {
    const required = ["REVEAL_API_URL", "REVEAL_GATEWAY_SECRET", "REVEAL_GATEWAY_SERVICE_TOKEN", "REVEAL_GATEWAY_ISSUER", "REVEAL_GATEWAY_AUDIENCE", "APP_ORIGIN"];
    if (required.some(name => !env[name]?.trim()) || env.REVEAL_GATEWAY_SECRET!.length < 32 || env.REVEAL_GATEWAY_SERVICE_TOKEN!.length < 32) throw new Error();
    const backend = new URL(env.REVEAL_API_URL!); const origin = new URL(env.APP_ORIGIN!);
    for (const url of [backend, origin]) {
      if ((url.protocol !== "https:" && !(url.protocol === "http:" && loopback(url.hostname))) || url.username || url.password || url.search || url.hash) throw new Error();
    }
    if (origin.pathname !== "/") throw new Error();
    return { backendUrl: backend.href.replace(/\/$/, ""), secret: env.REVEAL_GATEWAY_SECRET!, serviceToken: env.REVEAL_GATEWAY_SERVICE_TOKEN!,
      issuer: env.REVEAL_GATEWAY_ISSUER!, audience: env.REVEAL_GATEWAY_AUDIENCE!, appOrigin: origin.origin,
      artifactBaseUrls: env.REVEAL_ARTIFACT_DOWNLOAD_BASE_URLS || env.REVEAL_ARTIFACT_DOWNLOAD_BASE_URL };
  } catch { throw new GatewayError(503, "SETUP_REQUIRED", "Configure this client with npm run setup before connecting."); }
}

export async function assertion(principal: Principal, config: GatewayConfiguration): Promise<string> {
  return new SignJWT({ principal_kind: principal.principal_kind }).setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(principal.user_id).setIssuer(config.issuer).setAudience(config.audience)
    .setIssuedAt().setExpirationTime("5m").setJti(crypto.randomUUID()).sign(new TextEncoder().encode(config.secret));
}

async function jsonResult<T>(response: Response): Promise<T> {
  if (!response.ok) throw new GatewayError(response.status >= 500 ? 502 : response.status, "IDENTITY_UNAVAILABLE", "The research API could not resolve this session.");
  return response.json() as Promise<T>;
}
export async function readMe(principal: Principal, config: GatewayConfiguration, fetcher: Fetcher = fetch): Promise<Me> {
  const response = await fetcher(`${config.backendUrl}/v1/me`, { headers: { Authorization: `Bearer ${await assertion(principal, config)}` },
    cache: "no-store", redirect: "error", signal: AbortSignal.timeout(20000) });
  const me = await jsonResult<Me>(response);
  if (me.user_id !== principal.user_id || me.principal_kind !== principal.principal_kind) throw new GatewayError(502, "IDENTITY_UNAVAILABLE", "The research API returned an inconsistent session.");
  return me;
}
export async function resolvePrincipal(identity: { issuer: string; subject: string }, config: GatewayConfiguration, fetcher: Fetcher = fetch): Promise<Me> {
  const response = await fetcher(`${config.backendUrl}/internal/v1/principals/resolve`, {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.serviceToken}` },
    body: JSON.stringify({ ...identity, display_name: null, email: null, email_verified: false, orcid: null, orcid_authenticated: false }),
    cache: "no-store", redirect: "error", signal: AbortSignal.timeout(20000),
  });
  const principal = await jsonResult<Principal>(response);
  if (principal.principal_kind !== "registered" || !/^[a-f0-9-]{36}$/.test(principal.user_id)) throw new GatewayError(502, "IDENTITY_UNAVAILABLE", "The research API returned an invalid identity.");
  return readMe(principal, config, fetcher);
}

export function allowedArtifactRedirect(path: string[], location: string, bases?: string): boolean {
  if (path.length !== 3 || path[0] !== "v1" || path[1] !== "artifacts" || !/^[a-f0-9]{64}$/.test(path[2]) || !bases) return false;
  return bases.split(",").some(value => {
    try {
      const target = new URL(location), base = new URL(value.trim());
      if (base.protocol !== "https:" && !(base.protocol === "http:" && loopback(base.hostname))) return false;
      const prefix = base.pathname.endsWith("/") ? base.pathname : base.pathname + "/";
      return target.origin === base.origin && target.pathname.startsWith(prefix) && !target.username && !target.password && !target.hash && !base.username && !base.password;
    } catch { return false; }
  });
}
export function gatewayProblem(status: number, code: string, detail: string): Response {
  return Response.json({ type: "about:blank", title: code, status, code, detail }, { status, headers: { "Cache-Control": "no-store" } });
}
export function errorResponse(error: unknown): Response {
  return error instanceof GatewayError ? gatewayProblem(error.status, error.code, error.message)
    : gatewayProblem(502, "API_UNAVAILABLE", "The research API is unavailable. Retry shortly.");
}
export async function proxyRequest(request: Request, path: string[], principal: Principal | null, config: GatewayConfiguration, fetcher: Fetcher = fetch): Promise<Response> {
  if (path[0] !== "v1" || path.some(part => !part || [".", ".."].includes(part) || /[/\\]/.test(part))) return gatewayProblem(404, "NOT_FOUND", "Unknown API route.");
  if (!["GET", "HEAD"].includes(request.method) && request.headers.get("origin") !== config.appOrigin) return gatewayProblem(403, "CSRF_REJECTED", "The request must come from this application.");
  try {
    const headers = new Headers();
    for (const name of ["accept", "content-type", "idempotency-key", "last-event-id"]) {
      const value = request.headers.get(name); if (value) headers.set(name, value);
    }
    if (principal) headers.set("Authorization", `Bearer ${await assertion(principal, config)}`);
    const target = `${config.backendUrl}/${path.map(encodeURIComponent).join("/")}${new URL(request.url).search}`;
    const upstream = await fetcher(target, { method: request.method, headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.text(), cache: "no-store", redirect: "manual", signal: request.signal });
    if (upstream.status >= 300 && upstream.status < 400) {
      const location = upstream.headers.get("location") || "";
      if (request.method !== "GET" || upstream.status !== 307 || !allowedArtifactRedirect(path, location, config.artifactBaseUrls)) return gatewayProblem(502, "INVALID_ARTIFACT_REDIRECT", "The artifact download is unavailable.");
      return new Response(null, { status: 307, headers: { Location: location, "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer" } });
    }
    const output = new Headers({ "Cache-Control": "no-store", "X-Accel-Buffering": "no" });
    for (const name of ["content-type", "content-disposition", "x-content-type-options", "retry-after"]) { const value = upstream.headers.get(name); if (value) output.set(name, value); }
    return new Response(upstream.body, { status: upstream.status, headers: output });
  } catch (error) { return errorResponse(error); }
}
