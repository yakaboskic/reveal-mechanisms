import { test } from "node:test";
import assert from "node:assert/strict";
import { prettyRecordedValue, prettyToolName, toolInvocation } from "../src/lib/tool-display";
import { groupedWarnings } from "../src/lib/activity";
import type { Schema } from "../src/lib/client";

test("compact Read shortens recorded paths; expanded invocation keeps the exact full path and argument names", () => {
  const path = `/reveal/workspace/reveal/input/evidence-records/${"a".repeat(64)}.json`;
  const args = JSON.stringify({file_path:path, offset:10, limit:100});
  const compact = toolInvocation("Read", args);
  assert.match(compact, /^Read\(\n  filename = "input\/evidence-records\/aaaaaaaa…aaaaaaaa.json"/);
  assert.match(compact, /offset = 10\n  limit = 100\n\)$/);
  const full = toolInvocation("Read", args, false);
  assert.ok(full.includes(`file_path = "${path}"`));
  assert.ok(!full.includes("…"));
});

test("MCP tools get readable names while builtin and unknown tools remain identifiable", () => {
  assert.equal(prettyToolName("mcp__reveal__query_graph"), "QueryGraph");
  assert.equal(prettyToolName("mcp__reveal__get_schema"), "GetGraphSchema");
  assert.equal(prettyToolName("mcp__reveal__describe_kg"), "DescribeGraph");
  assert.equal(prettyToolName("mcp__reveal__write_account_draft"), "WriteAccountDraft");
  assert.equal(prettyToolName("mcp__other__future_tool"), "FutureTool");
  assert.equal(prettyToolName("Read"), "Read");
  assert.equal(toolInvocation("mcp__reveal__get_schema", "{}"), "GetGraphSchema()");
});

test("argument display handles nesting and escaped separators without changing numeric precision", () => {
  const args = '{"subject":"text,with:separators\\\"end", "limit":9007199254740993, "nested":{"score":0.123456789012345678901,"values":[null,true,{},[]]}, "extra":"fourth"}';
  const compact = toolInvocation("Query", args);
  assert.ok(compact.includes("limit = 9007199254740993"));
  assert.ok(compact.includes("… 1 more argument"));
  const full = toolInvocation("Query", args, false);
  assert.ok(full.includes('subject = "text,with:separators\\\"end"'));
  assert.ok(full.includes('"score": 0.123456789012345678901'));
  assert.ok(full.includes('extra = "fourth"'));
  assert.equal(prettyRecordedValue(args).replace(/\s/g, ""), args.replace(/\s/g, ""));
});

test("truncated legacy argument and result text is retained verbatim on expansion", () => {
  const raw = '{"file_path": "partial\n[truncated]';
  assert.ok(toolInvocation("Read", raw, false).includes(raw));
  assert.equal(prettyRecordedValue(raw), raw);
  assert.equal(toolInvocation("Read", null), "Read(…)");
});

test("repeated snapshot and streamed warnings have unique identities and are counted once", () => {
  const saved = ["first", "same", "same", "same"];
  const events = ["same", "same", "live"].map((message, i) => ({id:String(i), message, event_type:"warning"}) as Schema<"JobEvent">);
  assert.deepEqual(groupedWarnings(saved, events), [{message:"first",count:1}, {message:"same",count:3}, {message:"live",count:1}]);
  assert.deepEqual(saved, ["first", "same", "same", "same"]);
});
