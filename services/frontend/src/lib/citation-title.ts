/** Display-only shortening: never use this value for citation metadata or exports. */
export function citationTitle(title: string, limit = 60): { text: string; truncated: boolean } {
  if (!Number.isInteger(limit) || limit < 1) throw new RangeError("Citation title limit must be positive");
  // Keep combining marks and joined emoji intact when a single token is long.
  const characters = Array.from(new Intl.Segmenter("en", { granularity: "grapheme" }).segment(title), part => part.segment);
  if (characters.length <= limit) return { text: title, truncated: false };
  const prefix = characters.slice(0, limit).join("");
  const boundary = prefix.search(/\s+\S*$/u);
  const text = !/\s/u.test(characters[limit]) && boundary > 0 ? prefix.slice(0, boundary) : prefix;
  return { text: text.trimEnd(), truncated: true };
}
