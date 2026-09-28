/** Display projections only: never change the recorded tool name or arguments. */
const toolNames: Record<string, string> = {
  get_schema: "GetGraphSchema", query_graph: "QueryGraph",
  get_scientific_schema: "GetScientificSchema", get_scientific_account_schema: "GetScientificAccountSchema",
  get_scope: "GetGraphScope", describe_kg: "DescribeGraph", list_graphs: "ListGraphs",
  write_account_draft: "WriteAccountDraft", lint_account: "LintAccount",
};

export function prettyToolName(name: string) {
  if (!name.startsWith("mcp__")) return name;
  const native = name.split("__").slice(2).join("__");
  return toolNames[native] || native.split(/[_\s-]+/).filter(Boolean).map(part => part[0].toUpperCase() + part.slice(1)).join("") || "MCPTool";
}

function shorten(value: string, length = 88) {
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}

function compactPath(path: string) {
  const relative = path.replace(/^\/reveal\/workspace\/(?:reveal\/)?/, "");
  const segments = relative.split("/");
  const file = segments.pop() || "";
  const shortFile = file.replace(/[a-f\d]{24,}/gi, hash => `${hash.slice(0, 8)}…${hash.slice(-8)}`);
  const joined = [...segments, shortFile].join("/");
  if (joined.length <= 80) return joined;
  const base = shortFile.length > 58 ? `${shortFile.slice(0, 28)}…${shortFile.slice(-25)}` : shortFile;
  return `${segments.filter(Boolean)[0] || ""}/…/${base}`;
}

/** Indent recorded JSON without round-tripping scientific numbers through JS. */
export function prettyRecordedValue(raw: string) {
  try { JSON.parse(raw); } catch { return raw; }
  let output = "", depth = 0, quoted = false, escaped = false;
  for (let i = 0; i < raw.length; i++) {
    const char = raw[i];
    if (quoted) {
      output += char;
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
    } else if (char === '"') { quoted = true; output += char; }
    else if (/\s/.test(char)) continue;
    else if (char === "{" || char === "[") {
      output += char; depth++;
      if (!/^[\s]*[}\]]/.test(raw.slice(i + 1))) output += "\n" + "  ".repeat(depth);
    } else if (char === "}" || char === "]") {
      depth--;
      if (!/[{\[]$/.test(output)) output += "\n" + "  ".repeat(depth);
      output += char;
    } else if (char === ",") output += ",\n" + "  ".repeat(depth);
    else if (char === ":") output += ": ";
    else output += char;
  }
  return output;
}

/** Keep each JSON value's exact text, including large integers and decimals. */
function argumentFields(raw: string): [string, string][] | null {
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  } catch { return null; }
  const source = raw.trim().slice(1, -1);
  if (!source.trim()) return [];
  const fields: [string, string][] = [];
  let depth = 0, quoted = false, escaped = false, start = 0, colon = -1;
  for (let i = 0; i <= source.length; i++) {
    const char = source[i];
    if (quoted) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
    } else if (char === '"') quoted = true;
    else if (char === "{" || char === "[") depth++;
    else if (char === "}" || char === "]") depth--;
    else if (char === ":" && depth === 0 && colon === -1) colon = i;
    else if ((char === "," && depth === 0) || i === source.length) {
      fields.push([JSON.parse(source.slice(start, colon)), source.slice(colon + 1, i).trim()]);
      start = i + 1; colon = -1;
    }
  }
  return fields;
}

export function toolInvocation(name: string, argumentsText: string | null | undefined, compact = true) {
  const title = prettyToolName(name);
  if (!argumentsText) return `${title}(…)`;
  const fields = argumentFields(argumentsText);
  if (!fields) return `${title}(\n  ${compact ? shorten(argumentsText.replace(/\s+/g, " ")) : argumentsText}\n)`;
  if (!fields.length) return `${title}()`;
  const rows = (compact ? fields.slice(0, 3) : fields).map(([key, raw]) => {
    let value = raw;
    if (compact) {
      if (raw.startsWith('"')) {
        const text = JSON.parse(raw) as string;
        value = JSON.stringify(["file_path", "path", "filename"].includes(key) ? compactPath(text) : shorten(text));
      } else value = shorten(raw.replace(/\s+/g, " "));
    } else value = prettyRecordedValue(raw).replace(/\n/g, "\n  ");
    const label = compact && key === "file_path" ? "filename" : key;
    return `  ${label} = ${value}`;
  });
  if (compact && fields.length > 3) rows.push(`  … ${fields.length - 3} more ${fields.length === 4 ? "argument" : "arguments"}`);
  return `${title}(\n${rows.join("\n")}\n)`;
}
