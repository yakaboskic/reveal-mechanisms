import { formatStoredJson } from "./stored-json";

const timestamp = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$/;
const timeField = /(?:_at(?:_time)?|_until|timestamp)$/;

export function adminDate(value: unknown, field = ""): Date | null {
  if (typeof value === "string") {
    const parts = timestamp.exec(value);
    if (parts) {
      const [, year, month, day, hour, minute, second, fraction, zone] = parts;
      const days = new Date(Date.UTC(Number(year), Number(month), 0)).getUTCDate();
      if (+month < 1 || +month > 12 || +day < 1 || +day > days || +hour > 23 || +minute > 59 || +second > 59) return null;
      // Native SQL TIMESTAMP values are read in UTC by the admin API. Date-only
      // scientific values deliberately stay unchanged: they are not instants.
      const date = new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}.${(fraction || "").padEnd(3, "0").slice(0, 3)}${zone || "Z"}`);
      return Number.isFinite(date.getTime()) ? date : null;
    }
  }
  // Box phase markers use epoch seconds. Only convert numbers explicitly
  // identified as time fields; counters and scientific numbers stay exact.
  if (timeField.test(field) && (typeof value === "number" || typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value))) {
    const epoch = Number(value);
    const date = new Date(epoch < 100_000_000_000 ? epoch * 1000 : epoch);
    if (Number.isFinite(date.getTime())) return date;
  }
  return null;
}

export const formatAdminDuration = (seconds: number | null) => seconds == null ? "—" : seconds < 60 ? `${seconds.toFixed(1)}s` : seconds < 3600 ? `${(seconds / 60).toFixed(1)}m` : `${(seconds / 3600).toFixed(1)}h`;

const formatters = new Map<string, Intl.DateTimeFormat>();
export function formatAdminDate(value: unknown, timeZone?: string, field = ""): string {
  const date = adminDate(value, field);
  if (!date) return "—";
  const zone = timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  let formatter = formatters.get(zone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(undefined, { timeZone: zone, year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "shortOffset" });
    formatters.set(zone, formatter);
  }
  return formatter.format(date);
}

export function localizeAdminValue(value: string, field = "", timeZone?: string): string {
  return adminDate(value, field) ? formatAdminDate(value, timeZone, field) : value;
}

export function formatAdminJson(text: string, pretty = true, timeZone?: string): string {
  return formatStoredJson(text, (token, key) => {
    if (token.startsWith('"')) {
      try { return JSON.stringify(localizeAdminValue(JSON.parse(token), key, timeZone)); }
      catch { return token; } // A bounded preview may end mid-string.
    }
    return adminDate(token, key) ? JSON.stringify(formatAdminDate(token, timeZone, key)) : token;
  }, pretty);
}
