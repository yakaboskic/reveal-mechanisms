export class IdentityServiceError extends Error {
  constructor(readonly status: number, readonly code: string | null) {
    super(`Identity service returned ${status}`);
  }
}

/** A stale anonymous cookie must not block a freshly verified OAuth login. */
export async function resolveVerifiedLogin<T>(
  hasAnonymous: boolean,
  resolve: (includeAnonymous: boolean) => Promise<T>,
  discardAnonymous: () => Promise<void>,
): Promise<T> {
  try {
    return await resolve(hasAnonymous);
  } catch (error) {
    if (!hasAnonymous || !(error instanceof IdentityServiceError)
      || error.status !== 401 || error.code !== "INVALID_IDENTITY_PROOF") throw error;
    const principal = await resolve(false);
    await discardAnonymous();
    return principal;
  }
}
