/** Resolve retained local links through this app's authorized artifact gateway. */
export function sourceDownloadPath(value: string | null | undefined, origin: string): string | null {
  if (!value) return null;
  try {
    const current = new URL(origin), target = new URL(value, current.origin);
    if (target.username || target.password || target.search || target.hash || !["http:", "https:"].includes(target.protocol)) return null;
    const loopback = (hostname: string) => ["localhost", "127.0.0.1", "[::1]"].includes(hostname);
    const localAlias = loopback(current.hostname) && loopback(target.hostname) && target.protocol === current.protocol && target.port === current.port;
    // These are the two origins used by saved local deployment records. Return
    // only the checksum route, never the old origin or an external redirect.
    const retainedLocal = loopback(target.hostname) && target.protocol === "http:" && ["3000", "3100"].includes(target.port);
    if (target.origin !== current.origin && !localAlias && !retainedLocal) return null;
    return /^\/api\/backend\/v1\/artifacts\/[a-f0-9]{64}$/.test(target.pathname) ? target.pathname : null;
  } catch { return null; }
}
