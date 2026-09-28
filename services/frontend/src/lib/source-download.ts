/** Keep artifact requests on the current origin, including equivalent local dev hosts. */
export function sourceDownloadPath(value: string | null | undefined, origin: string): string | null {
  if (!value) return null;
  try {
    const current = new URL(origin), target = new URL(value, current.origin);
    if (target.username || target.password || !["http:", "https:"].includes(target.protocol)) return null;
    const loopback = (hostname: string) => ["localhost", "127.0.0.1", "[::1]"].includes(hostname);
    const localAlias = loopback(current.hostname) && loopback(target.hostname) && target.protocol === current.protocol && target.port === current.port;
    if (target.origin !== current.origin && !localAlias) return null;
    return /^\/api\/backend\/v1\/artifacts\/[a-f0-9]{64}$/.test(target.pathname) ? target.pathname : null;
  } catch { return null; }
}
