/** Only an authorized artifact response may redirect outside the gateway. */
export function allowedArtifactRedirect(path: string[], location: string, base: string | undefined): boolean {
  if (path.length !== 3 || path[0] !== "v1" || path[1] !== "artifacts" || !/^[a-f0-9]{64}$/.test(path[2]) || !base) return false;
  return base.split(",").some(value => allowedStorageLocation(location, value.trim()));
}
function allowedStorageLocation(location: string, base: string): boolean {
  try {
    const target = new URL(location);
    const allowed = new URL(base);
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(allowed.hostname);
    if (allowed.protocol !== "https:" && !(local && allowed.protocol === "http:")) return false;
    const prefix = allowed.pathname.endsWith("/") ? allowed.pathname : allowed.pathname + "/";
    return target.origin === allowed.origin && target.pathname.startsWith(prefix) &&
      !target.username && !target.password && !target.hash && !allowed.username && !allowed.password;
  } catch { return false; }
}
