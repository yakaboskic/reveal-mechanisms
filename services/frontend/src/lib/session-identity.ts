import type { Schema } from "./client";

/**
 * Page data waits only for the cookie principal that the gateway signs (the DB-free status hop).
 * GET /v1/me verifies it in parallel and is throttled; it never gates data requests.
 */
type Me = Schema<"Me">;
export type SessionPrincipal = { user_id: string; principal_kind: Me["principal_kind"]; workspace_expires_at?: string | null };
export type SessionStatus = { principal?: SessionPrincipal | null; canClaim: boolean; canAdmin?: boolean; providers: { google: boolean; orcid: boolean } };
export type IdentitySnapshot = { ready: boolean; principal: SessionPrincipal | null; verified: Me | null; rejected: boolean };
/** `adopt`: a Me the gateway just obtained (anonymous provisioning), so no second GET /v1/me is needed. */
export type RefreshOptions = { force?: boolean; adopt?: Me };
export const identityFreshMs = 30_000;
export const emptyIdentity: IdentitySnapshot = { ready: false, principal: null, verified: null, rejected: false };

export const samePrincipal = (a?: SessionPrincipal | null, b?: SessionPrincipal | null) => a?.user_id === b?.user_id && a?.principal_kind === b?.principal_kind;
/** The known principal before GET /v1/me answers; also the exact Me of a just-provisioned anonymous principal (no profile). */
export const provisionalMe = (principal: SessionPrincipal): Me => ({ user_id: principal.user_id, principal_kind: principal.principal_kind,
  workspace_expires_at: principal.workspace_expires_at ?? null, display_name: null, email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null });
/** Consumers bind to user_id/principal_kind, which verification never changes. */
export const sessionMe = (state: IdentitySnapshot): Me | null => state.rejected || !state.principal ? null
  : state.verified && samePrincipal(state.verified, state.principal) ? state.verified : provisionalMe(state.principal);
/** Cache scopes come from the same principal the gateway signs, so verification never re-keys (and clears) them. */
export const sessionScope = (state: IdentitySnapshot, canClaim: boolean) => state.ready && state.principal && !state.rejected
  ? `${state.principal.user_id}:${state.principal.principal_kind}:${state.principal.workspace_expires_at || ""}:${canClaim}` : null;

export function createSessionIdentity(deps: {
  status: () => Promise<SessionStatus>; me: () => Promise<Me>; onStatus: (status: SessionStatus) => void;
  onChange: (state: IdentitySnapshot) => void; reset: () => void; denied: (error: unknown) => boolean; now?: () => number;
}) {
  const now = deps.now || Date.now;
  let state = emptyIdentity, sequence = 0, verifiedAt = 0, epoch = 0;
  let pending: Promise<Me | null> | null = null, verifying: { principal: SessionPrincipal; request: Promise<Me | null> } | null = null;
  const publish = (next: Partial<IdentitySnapshot>) => {
    if ((Object.keys(next) as (keyof IdentitySnapshot)[]).every(key => state[key] === next[key])) return;
    state = { ...state, ...next }; deps.onChange(state);
  };
  function verify(principal: SessionPrincipal, force: boolean): Promise<Me | null> {
    if (!force && verifying && samePrincipal(verifying.principal, principal)) return verifying.request;
    const mine = ++epoch, entry = { principal, request: Promise.resolve<Me | null>(null) };
    verifying = entry;
    entry.request = (async () => {
      try {
        const identity = await deps.me();
        // A late or superseded answer, or one for a cookie changed in another tab, never labels this principal.
        if (mine !== epoch || !samePrincipal(state.principal, principal) || !samePrincipal(identity, principal)) return sessionMe(state);
        verifiedAt = now(); publish({ verified: identity, rejected: false }); return identity;
      } catch (error) {
        if (mine !== epoch || !samePrincipal(state.principal, principal)) return sessionMe(state);
        if (deps.denied(error)) { deps.reset(); verifiedAt = 0; publish({ verified: null, rejected: true }); return null; }
        // A transport outage does not change a known identity; denial purges it.
        return sessionMe(state);
      } finally { if (verifying === entry) verifying = null; }
    })();
    return entry.request;
  }
  function refresh({ force = false, adopt }: RefreshOptions = {}): Promise<Me | null> {
    if (pending && !force && !adopt) return pending;
    const current = ++sequence;
    // A superseded check answers with the newer one (logout clears it), never with a stale identity.
    const superseded = () => pending || sessionMe(state);
    const request: Promise<Me | null> = (async () => {
      let status: SessionStatus;
      try { status = await deps.status(); }
      catch (error) {
        if (current !== sequence) return superseded();
        if (deps.denied(error)) { deps.reset(); verifiedAt = 0; epoch++; publish({ ready: true, principal: null, verified: null, rejected: false }); return null; }
        publish({ ready: true }); return sessionMe(state);
      }
      if (current !== sequence) return superseded();
      deps.onStatus(status);
      const known = state.principal, next = status.principal || null, changed = !samePrincipal(next, known);
      const principal = known && next && !changed && (known.workspace_expires_at ?? null) === (next.workspace_expires_at ?? null) ? known : next;
      if (changed) { deps.reset(); verifiedAt = 0; epoch++; verifying = null; }
      publish(changed ? { ready: true, principal, verified: null, rejected: false } : { ready: true, principal });
      if (!principal) return null;
      if (adopt && samePrincipal(adopt, principal)) { verifiedAt = now(); epoch++; publish({ verified: adopt, rejected: false }); return adopt; }
      if (!changed && !force && state.verified && !state.rejected && now() - verifiedAt < identityFreshMs) return state.verified;
      return verify(principal, force);
    })();
    pending = request;
    void request.finally(() => { if (pending === request) pending = null; }).catch(() => {});
    return request;
  }
  function clear() { sequence++; epoch++; pending = null; verifying = null; verifiedAt = 0; publish({ principal: null, verified: null, rejected: false }); }
  return { refresh, clear, snapshot: () => state };
}
