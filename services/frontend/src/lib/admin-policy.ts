// Shared pure policy; values are supplied by server code only.
export function adminBypass(env: Record<string, string | undefined>) {
  return env.DISABLE_ADMIN_LOGIN === "true" && env.NODE_ENV === "development" && env.REVEAL_ENVIRONMENT !== "production";
}
export function allowedAdmin(email: unknown, verified: unknown, emails: string | undefined) {
  return verified === true && typeof email === "string" && !!email.trim() &&
    (emails || "").split(",").map(value => value.trim().toLowerCase()).filter(Boolean).includes(email.trim().toLowerCase());
}
