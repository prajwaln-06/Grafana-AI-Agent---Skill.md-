#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";

const defaultModels = [
  "gemma3:4b",
  "qwen2.5-coder:3b",
  "llama3.1:8b",
  "deepseek-r1:7b",
  "phi4-mini:latest",
];

const models = (process.env.MODELS || defaultModels.join(","))
  .split(",")
  .map((name) => name.trim())
  .filter(Boolean);
const outputPath = process.env.OUTPUT || "docs/local-model-evaluation-results.md";

const prompts = [
  {
    id: "metrics_tool_plan",
    title: "Prometheus MCP Tool Plan",
    prompt: `You are designing a Grafana observability assistant.
User question: Which service has the highest request count right now?

Return a concise MCP tool plan. Use only these tools:
- list_datasources
- list_prometheus_label_values
- query_prometheus

Known metric: demo_requests_total
Important rule: discover datasource UID before querying.
Return JSON only.`,
    expected: [
      "list_datasources",
      "list_prometheus_label_values",
      "query_prometheus",
      "demo_requests_total",
      "datasource",
    ],
  },
  {
    id: "logs_tool_plan",
    title: "Loki MCP Tool Plan",
    prompt: `You are designing a Grafana observability assistant.
User question: Show the latest error logs from the demo app.

Return a concise MCP tool plan. Use only these tools:
- list_datasources
- list_loki_label_values
- query_loki_logs

Known container: log-generator-ai-lab
Known LogQL filter: level=error
Important rule: discover datasource UID before querying.
Return JSON only.`,
    expected: [
      "list_datasources",
      "list_loki_label_values",
      "query_loki_logs",
      "log-generator-ai-lab",
      "level=error",
    ],
  },
  {
    id: "workflow_mapping",
    title: "Model To Workflow Mapping",
    prompt: `Map a local open-source model into this Grafana assistant workflow:
1. user asks an observability question
2. model drafts intent/tool plan
3. MCP client validates tool schema and datasource UID
4. MCP tools query Grafana, Prometheus, or Loki
5. model summarizes only from tool output

Return 5 concise bullet points. Mention why raw model output must be validated.`,
    expected: ["intent", "MCP", "schema", "datasource", "tool output"],
  },
];

function stripAnsi(text) {
  return text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").trim();
}

function runModel(model, prompt) {
  try {
    const output = execFileSync("ollama", ["run", model, prompt], {
      encoding: "utf8",
      timeout: 120000,
      maxBuffer: 1024 * 1024 * 8,
    });
    return { ok: true, text: stripAnsi(output) };
  } catch (error) {
    return {
      ok: false,
      text: stripAnsi(error.stdout?.toString() || error.stderr?.toString() || error.message),
    };
  }
}

function score(text, expected) {
  const lower = text.toLowerCase();
  const hits = expected.filter((item) => lower.includes(item.toLowerCase()));
  return {
    hits,
    score: hits.length,
    max: expected.length,
  };
}

function recommendation(model, total, max) {
  const ratio = total / max;
  if (ratio >= 0.85) return "Strong fit for MCP planning";
  if (ratio >= 0.65) return "Usable with validation";
  if (ratio >= 0.45) return "Use only for summaries or fallback";
  return "Not recommended for tool planning";
}

const rows = [];
const details = [];

for (const model of models) {
  let total = 0;
  let max = 0;
  details.push(`## ${model}\n`);

  for (const test of prompts) {
    const result = runModel(model, test.prompt);
    const scored = result.ok ? score(result.text, test.expected) : { hits: [], score: 0, max: test.expected.length };
    total += scored.score;
    max += scored.max;

    details.push(`### ${test.title}`);
    details.push(`Score: ${scored.score}/${scored.max}`);
    details.push(`Matched: ${scored.hits.join(", ") || "none"}`);
    details.push("");
    details.push("```text");
    details.push(result.text.slice(0, 2500));
    details.push("```");
    details.push("");
  }

  rows.push({ model, total, max, recommendation: recommendation(model, total, max) });
}

rows.sort((a, b) => b.total - a.total);

const report = [
  "# Local Model Evaluation For Grafana MCP Assistant",
  "",
  "## Purpose",
  "",
  "This is the physical model-selection work for the assigned task. Each local open-source model was asked to produce Grafana MCP tool plans for Prometheus and Loki questions, then mapped into the assistant workflow.",
  "",
  "## Models Tested",
  "",
  ...models.map((model) => `- ${model}`),
  "",
  "## Score Summary",
  "",
  "| Rank | Model | Score | Recommendation |",
  "| --- | --- | --- | --- |",
  ...rows.map((row, index) => `| ${index + 1} | ${row.model} | ${row.total}/${row.max} | ${row.recommendation} |`),
  "",
  "## Final Recommendation",
  "",
  `Use **${rows[0]?.model || "qwen"}** first for MCP/PromQL/LogQL planning because it scored highest in this local test. Use **llama3.1:8b** or **phi4-mini** for concise user-facing summaries if they perform well in your local run. Keep **DeepSeek R1** for reasoning-heavy analysis, but avoid exposing its raw chain-of-thought in the UI.`,
  "",
  "Critical design rule: the model drafts a plan, but deterministic MCP client code validates tool schemas, discovers datasource UIDs, and executes calls. The model should summarize only from returned tool output.",
  "",
  "## Detailed Outputs",
  "",
  ...details,
].join("\n");

mkdirSync("docs", { recursive: true });
writeFileSync(outputPath, report);
console.log(report);
