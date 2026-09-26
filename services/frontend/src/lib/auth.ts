import { cookies } from "next/headers";
import type { NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import type { OAuthConfig } from "next-auth/providers/oauth";
import { anonymousCookie, assertion, readAnonymous, serviceRequest, type Principal, type VerifiedIdentity } from "./gateway";

const orcidIssuer = process.env.AUTH_ORCID_ISSUER || "https://orcid.org";
const providers: NextAuthOptions["providers"] = [];
if (process.env.AUTH_GOOGLE_ID && process.env.AUTH_GOOGLE_SECRET) providers.push(GoogleProvider({ clientId: process.env.AUTH_GOOGLE_ID, clientSecret: process.env.AUTH_GOOGLE_SECRET }));
if (process.env.AUTH_ORCID_ID && process.env.AUTH_ORCID_SECRET) providers.push({
  id: "orcid", name: "ORCID", type: "oauth", wellKnown: `${orcidIssuer}/.well-known/openid-configuration`,
  clientId: process.env.AUTH_ORCID_ID, clientSecret: process.env.AUTH_ORCID_SECRET,
  authorization: { params: { scope: "openid" } }, idToken: true, checks: ["state", "nonce"],
  // ORCID discovery advertises client_secret_post and does not advertise PKCE.
  client: { token_endpoint_auth_method: "client_secret_post" },
  profile(profile) { return { id: profile.sub, name: profile.name || null, email: null, image: null }; },
} satisfies OAuthConfig<{ sub: string; name?: string }>);

export const authOptions: NextAuthOptions = {
  providers, secret: process.env.AUTH_SECRET, session: { strategy: "jwt", maxAge: 30 * 24 * 60 * 60 },
  callbacks: {
    async jwt({ token, account, profile }) {
      // Only a verified OAuth callback may change application identity. Ignore client session updates.
      if (account && profile) {
        const p = profile as Record<string, unknown>;
        const issuer = account.provider === "google" ? "https://accounts.google.com" : orcidIssuer;
        const identity: VerifiedIdentity = {
          issuer, subject: account.providerAccountId, display_name: typeof p.name === "string" ? p.name : null,
          email: typeof p.email === "string" ? p.email : null, email_verified: p.email_verified === true,
          orcid: account.provider === "orcid" ? `${orcidIssuer}/${account.providerAccountId}` : null,
          orcid_authenticated: account.provider === "orcid",
        };
        const anonymous = await readAnonymous();
        const headers: Record<string, string> = anonymous ? { "X-Reveal-Anonymous-Session": await assertion(anonymous, { purpose: "anonymous_session" }) } : {};
        token.principal = await serviceRequest<Principal>("principals/resolve", identity, crypto.randomUUID(), headers);
        if (anonymous && token.principal.user_id === anonymous.user_id) (await cookies()).delete(anonymousCookie);
        token.verifiedIdentity = identity;
        token.loginObservedAt = Date.now();
      }
      return token;
    },
    async session({ session, token }) {
      session.principal = token.principal as Principal | undefined;
      // No subject, OAuth token or private verified-login proof is returned to browser code.
      return session;
    },
  },
};

declare module "next-auth" { interface Session { principal?: Principal } }
declare module "next-auth/jwt" { interface JWT { principal?: Principal; verifiedIdentity?: VerifiedIdentity; loginObservedAt?: number } }
