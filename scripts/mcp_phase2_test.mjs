#!/usr/bin/env node
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

function loadDotEnv(filePath) {
  if (!existsSync(filePath)) return;

  const contents = readFileSync(filePath, "utf8");
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;

    const equalsIndex = line.indexOf("=");
    if (equalsIndex === -1) continue;

    const key = line.slice(0, equalsIndex).trim();
    const value = line.slice(equalsIndex + 1).trim().replace(/^['\"]|['\"]$/g, "");
    if (key && process.env[key] == null) {
      process.env[key] = value;
    }
  }
}

loadDotEnv(resolve(process.cwd(), ".env"));

const token = process.env.GRAFANA_MCP_TOKEN;
const grafanaUrl = process.env.GRAFANA_URL || "http://grafana:3000";
const dockerNetwork = process.env.DOCKER_NETWORK || "grafanaai_default";
const context = {
  prometheusUid: "Prometheus",
  lokiUid: "Loki",
  dashboardUid: "demo-dataset-overview",
};

if (!token) {
  console.error("Set GRAFANA_MCP_TOKEN before running this script.");
  process.exit(1);
}

const child = spawn(
  "docker",
  [
    "run",
    "--rm",
    "-i",
    "--network",
    dockerNetwork,
    "-e",
    `GRAFANA_URL=${grafanaUrl}`,
    "-e",
    `GRAFANA_SERVICE_ACCOUNT_TOKEN=${token}`,
    "--entrypoint",
    "/app/mcp-grafana",
    "mcp/grafana",
    "--transport",
    "stdio",
  ],
  { stdio: ["pipe", "pipe", "pipe"] },
);

let nextId = 1;
let buffer = "";
const pending = new Map();

child.stderr.on("data", (chunk) => {
  const text = chunk.toString();
  if (!text.includes("level=debug")) {
    process.stderr.write(text);
  }
});

child.stdout.on("data", (chunk) => {
  buffer += chunk.toString();
  parseMessages();
});

child.on("exit", (code) => {
  for (const { reject } of pending.values()) {
    reject(new Error(`mcp-grafana exited with code ${code}`));
  }
});

function parseMessages() {
  while (true) {
    const newline = buffer.indexOf("\n");
    if (newline === -1) return;

    const body = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    if (!body) continue;
    const message = JSON.parse(body);

    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else resolve(message.result);
    }
  }
}

function send(method, params) {
  const id = nextId++;
  const message = JSON.stringify({ jsonrpc: "2.0", id, method, params });
  child.stdin.write(`${message}\n`);
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`Timed out waiting for ${method}`));
      }
    }, 20000);
  });
}

function notify(method, params) {
  const message = JSON.stringify({ jsonrpc: "2.0", method, params });
  child.stdin.write(`${message}\n`);
}

function summarizeContent(result) {
  const text = JSON.stringify(result);
  return text.length > 1000 ? `${text.slice(0, 1000)}...` : text;
}

function argsFor(toolName, tool) {
  const props = tool.inputSchema?.properties || {};
  const keys = Object.keys(props);
  const args = {};

  const setFirst = (candidates, value) => {
    const key = candidates.find((candidate) => keys.includes(candidate));
    if (key) args[key] = value;
  };

  if (toolName === "search_dashboards") {
    setFirst(["query", "search", "title"], "Dataset");
  }

  if (toolName === "get_dashboard_summary") {
    setFirst(["uid", "dashboardUid", "dashboard_uid"], context.dashboardUid);
  }

  if (toolName === "get_dashboard_by_uid") {
    setFirst(["uid", "dashboardUid", "dashboard_uid"], context.dashboardUid);
  }

  if (toolName === "get_dashboard_panel_queries") {
    setFirst(["uid", "dashboardUid", "dashboard_uid"], context.dashboardUid);
  }

  if (toolName === "get_dashboard_property") {
    setFirst(["uid", "dashboardUid", "dashboard_uid"], context.dashboardUid);
    setFirst(["jsonPath", "jsonpath", "path", "property"], "$.title");
  }

  if (toolName === "get_datasource") {
    setFirst(["uid", "datasourceUid", "datasource_uid", "name"], context.prometheusUid);
  }

  if (toolName === "get_query_examples") {
    setFirst(["datasourceUid", "datasource_uid", "uid"], context.prometheusUid);
  }

  if (toolName.startsWith("list_prometheus_")) {
    setFirst(["datasourceUid", "datasource_uid", "uid"], context.prometheusUid);
    setFirst(["labelName", "label", "name"], "service");
    setFirst(["match", "selector", "query"], "demo_requests_total");
  }

  if (toolName === "query_prometheus") {
    setFirst(["datasourceUid", "datasource_uid", "uid"], context.prometheusUid);
    setFirst(["query", "expr", "expression"], "demo_requests_total");
    setFirst(["queryType"], "instant");
    setFirst(["endTime"], "now");
  }

  if (toolName === "query_prometheus_histogram") {
    setFirst(["datasourceUid", "datasource_uid", "uid"], context.prometheusUid);
    setFirst(["query", "expr", "expression"], "demo_latency_ms");
    setFirst(["queryType"], "instant");
    setFirst(["endTime"], "now");
  }

  if (toolName.startsWith("list_loki_")) {
    setFirst(["datasourceUid", "datasource_uid", "uid"], context.lokiUid);
    setFirst(["labelName", "label", "name"], "container");
  }

  if (toolName === "query_loki_logs") {
    setFirst(["datasourceUid", "datasource_uid", "uid"], context.lokiUid);
    setFirst(["query", "expr", "expression", "logql"], '{container="log-generator-ai-lab"} |= "level=error"');
    setFirst(["limit"], 5);
    setFirst(["direction"], "backward");
    setFirst(["startRfc3339"], "now-15m");
    setFirst(["endRfc3339"], "now");
  }

  if (toolName === "query_loki_stats" || toolName === "query_loki_patterns") {
    setFirst(["datasourceUid", "datasource_uid", "uid"], context.lokiUid);
    setFirst(["query", "expr", "expression", "logql"], '{container="log-generator-ai-lab"}');
    setFirst(["startRfc3339"], "now-15m");
    setFirst(["endRfc3339"], "now");
  }

  if (toolName === "search_folders") {
    setFirst(["query", "search", "title"], "AI Observability");
  }

  return args;
}

async function callTool(name, args) {
  return send("tools/call", { name, arguments: args });
}

function textPayload(result) {
  return result?.content?.find((item) => item.type === "text")?.text || "";
}

function parseJsonPayload(result) {
  const text = textPayload(result);
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function assertToolResult(name, result) {
  if (result?.isError) {
    throw new Error(textPayload(result) || `${name} returned isError=true`);
  }
}

async function main() {
  await send("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "grafana-ai-phase2-test", version: "1.0.0" },
  });
  notify("notifications/initialized", {});

  const toolList = await send("tools/list", {});
  const tools = toolList.tools || [];
  const byName = Object.fromEntries(tools.map((tool) => [tool.name, tool]));
  const safeToExecute = new Set([
    "search_dashboards",
    "get_dashboard_summary",
    "get_dashboard_by_uid",
    "get_dashboard_panel_queries",
    "get_dashboard_property",
    "list_datasources",
    "get_datasource",
    "get_query_examples",
    "query_prometheus",
    "list_prometheus_metric_names",
    "list_prometheus_label_values",
    "query_prometheus_histogram",
    "query_loki_logs",
    "query_loki_stats",
    "query_loki_patterns",
    "list_loki_label_values",
    "list_incidents",
    "search_folders",
    "list_teams",
    "list_users_by_org",
    "list_all_roles",
    "get_resource_permissions",
    "query_influxdb",
    "list_clickhouse_tables",
    "describe_clickhouse_table",
  ]);
  const skipByDefault = new Set([
    "update_dashboard",
    "create_incident",
    "add_activity_to_incident",
    "create_folder",
  ]);
  const sheetTools = [
    "search_dashboards",
    "get_dashboard_summary",
    "get_dashboard_by_uid",
    "update_dashboard",
    "get_dashboard_panel_queries",
    "get_dashboard_property",
    "list_datasources",
    "get_datasource",
    "get_query_examples",
    "query_prometheus",
    "list_prometheus_metric_names",
    "list_prometheus_label_values",
    "query_prometheus_histogram",
    "query_loki_logs",
    "query_loki_stats",
    "query_loki_patterns",
    "list_loki_label_values",
    "list_incidents",
    "create_incident",
    "get_incident",
    "add_activity_to_incident",
    "search_folders",
    "create_folder",
    "list_teams",
    "list_users_by_org",
    "list_all_roles",
    "get_resource_permissions",
    "query_influxdb",
    "list_clickhouse_tables",
    "describe_clickhouse_table",
  ];

  console.log(`MCP tools discovered: ${tools.length}`);
  console.log(
    tools
      .map((tool) => tool.name)
      .filter((name) =>
        [
          "search_dashboards",
          "get_dashboard_summary",
          "get_dashboard_panel_queries",
          "get_dashboard_property",
          "list_datasources",
          "get_datasource",
          "query_prometheus",
          "list_prometheus_metric_names",
          "list_prometheus_label_values",
          "query_loki_logs",
          "list_loki_label_values",
        ].includes(name),
      )
      .join("\n"),
  );

  const datasourceResult = await callTool("list_datasources", {});
  assertToolResult("list_datasources", datasourceResult);
  const datasourcePayload = parseJsonPayload(datasourceResult);
  const datasources = datasourcePayload?.datasources || [];
  context.prometheusUid = datasources.find((ds) => ds.name === "Prometheus")?.uid || context.prometheusUid;
  context.lokiUid = datasources.find((ds) => ds.name === "Loki")?.uid || context.lokiUid;
  console.log("PASS list_datasources");
  console.log(summarizeContent(datasourceResult));
  console.log(`Using Prometheus UID: ${context.prometheusUid}`);
  console.log(`Using Loki UID: ${context.lokiUid}`);

  const tests = sheetTools.filter((name) => name !== "list_datasources");

  for (const name of tests) {
    const tool = byName[name];
    if (!tool) {
      console.log(`SKIP ${name}: tool not exposed`);
      continue;
    }

    if (skipByDefault.has(name) && process.env.MCP_ALLOW_WRITES !== "1") {
      console.log(`SKIP ${name}: write/destructive tool, set MCP_ALLOW_WRITES=1 to test intentionally`);
      continue;
    }

    if (!safeToExecute.has(name)) {
      console.log(`SKIP ${name}: no safe local test arguments configured`);
      continue;
    }

    const args = argsFor(name, tool);
    try {
      const result = await callTool(name, args);
      assertToolResult(name, result);
      console.log(`PASS ${name}`);
      console.log(summarizeContent(result));
    } catch (error) {
      console.log(`FAIL ${name}`);
      console.log(`args=${JSON.stringify(args)}`);
      console.log(`schema=${JSON.stringify(tool.inputSchema)}`);
      console.log(error.message);
    }
  }

  child.stdin.end();
}

main().catch((error) => {
  console.error(error);
  child.kill();
  process.exit(1);
});
