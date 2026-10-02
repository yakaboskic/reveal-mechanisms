/** Resolve retained source links through this app's authorized artifact gateway. */
export function sourceDownloadPath(value: string | null | undefined, origin: string): string | null {
  if (!value) return null;
  try {
    const current = new URL(origin), target = new URL(value, current.origin);
    if (target.username || target.password || target.search || target.hash || !["http:", "https:"].includes(target.protocol)) return null;
    const loopback = (hostname: string) => ["localhost", "127.0.0.1", "[::1]"].includes(hostname);
    const localAlias = loopback(current.hostname) && loopback(target.hostname) && target.protocol === current.protocol && target.port === current.port;
    // Saved records retain their original frontend URL. Recognize only the
    // known local ports and canonical deployment, then return a checksum path
    // through the current gateway, which independently authorizes access.
    const retainedLocal = loopback(target.hostname) && target.protocol === "http:" && ["3000", "3100"].includes(target.port);
    const retainedCanonical = target.origin === "https://reveal-mechanisms.vercel.app";
    if (target.origin !== current.origin && !localAlias && !retainedLocal && !retainedCanonical) return null;
    return /^\/api\/backend\/v1\/artifacts\/[a-f0-9]{64}$/.test(target.pathname) ? target.pathname : null;
  } catch { return null; }
}
