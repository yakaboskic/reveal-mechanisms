export type ProvenanceRecord = { [key: string]: unknown };
export const recordOf = (value: unknown): ProvenanceRecord | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as ProvenanceRecord : null;
export const textOf = (value: unknown): string | null => typeof value === "string" && value.trim() ? value : typeof value === "number" ? String(value) : null;
export const recordsOf = (value: unknown): ProvenanceRecord[] => Array.isArray(value) ? value.map(recordOf).filter((item): item is ProvenanceRecord => item !== null) : [];
export const stringsOf = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : typeof value === "string" ? [value] : [];

/** Provenance can contain local or S3 locators. Render those as text, never navigable schemes. */
export function publicSourceHref(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}

export const provenanceAnchor = (id: string) => `source-${id}`;
