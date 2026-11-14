// Shared pure policy; values are supplied by server code only.
export function adminBypass(env: Record<string, string | undefined>) {
  return env.DISABLE_ADMIN_LOGIN === "true" && env.NODE_ENV === "development" && env.REVEAL_ENVIRONMENT !== "production";
}
export function allowedAdmin(email: unknown, verified: unknown, emails: string | undefined) {
  return verified === true && typeof email === "string" && !!email.trim() &&
    (emails || "").split(",").map(value => value.trim().toLowerCase()).filter(Boolean).includes(email.trim().toLowerCase());
}
/** The email an allowed admin's changes are recorded under: the verified email, trimmed and lower-cased. */
export function adminEmail(email: unknown, verified: unknown, emails: string | undefined) {
  return allowedAdmin(email, verified, emails) ? (email as string).trim().toLowerCase() : null;
}
export const BYPASS_ADMIN = "local-development-bypass";
/** Who a console change is attributed to in the signed assertion; null when access was not granted. */
export function adminActor(access: { allowed: boolean; bypass: boolean; email: string | null }) {
  if (!access.allowed) return null;
  return access.bypass ? BYPASS_ADMIN : access.email;
}
