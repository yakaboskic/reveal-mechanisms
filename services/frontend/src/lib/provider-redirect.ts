import { withRequestDeadline } from "./request-deadline";

// Use NextAuth's CSRF-protected sign-in endpoints while keeping navigation
// outside the request deadline, so a late response cannot redirect after Back.
export async function providerRedirect(provider: "google" | "orcid", callbackUrl: string): Promise<string> {
  return withRequestDeadline(async signal => {
    const csrf = await fetch("/api/auth/csrf", { cache: "no-store", signal });
    const token = await csrf.json();
    if (!csrf.ok || typeof token.csrfToken !== "string") throw new Error("Sign-in could not be started. Please try again.");
    const response = await fetch(`/api/auth/signin/${provider}`, {
      method: "POST", signal, headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ csrfToken: token.csrfToken, callbackUrl, json: "true" }),
    });
    const value = await response.json();
    if (!response.ok || typeof value.url !== "string") throw new Error("Sign-in could not be started. Please try again.");
    const target = new URL(value.url, callbackUrl);
    if (target.protocol !== "https:" && target.origin !== new URL(callbackUrl).origin) throw new Error("Sign-in returned an invalid destination. Please try again.");
    return target.href;
  }, "Sign-in is taking longer than expected. Please retry; your question and anchors are saved.");
}
