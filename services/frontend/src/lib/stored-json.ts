// Format JSON tokens without parsing numbers into JavaScript floats. This keeps
// stored 64-bit integers and scientific numeric literals exact in the inspector.
export function formatStoredJson(text: string, transformValue?: (token: string, key?: string) => string, pretty = true): string {
  const tokens = text.match(/"(?:\\.|[^"\\])*"|[{}\[\],:]|[^\s{}\[\],:]+/g) || [];
  let depth = 0;
  const output: string[] = [];
  const line = () => { if (pretty) output.push("\n", "  ".repeat(Math.max(0, depth))); };
  tokens.forEach((token, i) => {
    if (token === "{" || token === "[") {
      output.push(token); depth++;
      if (tokens[i + 1] !== "}" && tokens[i + 1] !== "]") line();
    } else if (token === "}" || token === "]") {
      depth--;
      if (tokens[i - 1] !== "{" && tokens[i - 1] !== "[") line();
      output.push(token);
    } else if (token === ",") { output.push(token); line(); }
    else if (token === ":") output.push(pretty ? ": " : ":");
    else {
      let key: string | undefined;
      if (tokens[i - 1] === ":") {
        try { key = JSON.parse(tokens[i - 2]); } catch { /* Partial preview. */ }
      }
      output.push(transformValue && tokens[i + 1] !== ":" ? transformValue(token, key) : token);
    }
  });
  return output.join("");
}
