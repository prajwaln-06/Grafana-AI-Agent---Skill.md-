import express from "express";
import cors from "cors";
import http from "node:http";
import https from "node:https";
import {
  catalog,
  CM_TELEMETRY_METRICS,
  CURATED_HOSTS,
  CURATED_METRICS,
  findQuery,
  interpolateVars,
} from "./queries.js";

const CURATED_CONTAINERS = [
  "cadvisor-ai-lab",
  "grafana-ai-lab",
  "log-generator-ai-lab",
  "loki-ai-lab",
  "node-exporter-ai-lab",
  "prometheus-ai-lab",
  "promtail-ai-lab",
  "admin",
  "leader1",
  "leader2",
  "leader3",
];

const app = express();
const PORT = process.env.PORT || 5050;
const PROMETHEUS_URL = process.env.PROMETHEUS_URL || "http://localhost:9090";
const LOKI_URL = process.env.LOKI_URL || "http://localhost:3100";
const OPENSEARCH_URL = process.env.OPENSEARCH_URL || "";
const OPENSEARCH_INDEX = process.env.OPENSEARCH_INDEX || "logs-*";
const OPENSEARCH_TIMESTAMP_FIELD =
  process.env.OPENSEARCH_TIMESTAMP_FIELD || "@timestamp";
const GRAFANA_URL = process.env.GRAFANA_URL || "http://localhost:3000";
const ADK_SERVICE_URL = process.env.ADK_SERVICE_URL || process.env.BACKEND_URL || "http://127.0.0.1:8008";
const ADK_PROXY_TIMEOUT_MS = readPositiveIntEnv("ADK_PROXY_TIMEOUT_MS", 120000, 120000);
const BACKEND_TIMEOUT_MS = readPositiveIntEnv("BACKEND_TIMEOUT_MS", 12000, 60000);
const MAX_ADK_PROXY_BYTES = 5 * 1024 * 1024;
const MAX_BOARD_PANELS = 12;

app.use(cors());
app.use(express.json({ limit: "32kb" }));

// ---------- Root + health ----------

app.get("/", (_req, res) => {
  res.type("html").send(`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Grafana AI API</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, sans-serif; background:#0a0a0a; color:#fafafa;
      max-width: 40rem; margin: 3rem auto; padding: 0 1.25rem; line-height: 1.5; }
    a { color: #f7931a; } code { color: #c8c8c8; } .ok { color: #3ecf8e; }
  </style>
</head>
<body>
  <h1>Grafana AI API</h1>
  <p class="ok">API is running on port ${PORT}.</p>
  <p>Open the UI here: <a href="http://localhost:5173/">http://localhost:5173/</a></p>
  <p>Health JSON: <a href="/api/health"><code>/api/health</code></a></p>
</body>
</html>`);
});

app.get("/api/health", async (_req, res) => {
  const checks = {
    prometheus: PROMETHEUS_URL,
    loki: LOKI_URL,
    opensearch: OPENSEARCH_URL || "not configured; using Loki fallback",
    grafana: GRAFANA_URL,
    adk: ADK_SERVICE_URL,
  };

  let prometheusOk = false;
  try {
    const r = await fetchBackend(`${PROMETHEUS_URL}/-/ready`);
    prometheusOk = r.ok;
  } catch {
    prometheusOk = false;
  }

  res.status(prometheusOk ? 200 : 503).json({
    ok: prometheusOk,
    ui: "http://localhost:5173/",
    services: checks,
    status: {
      api: "up",
      prometheus: prometheusOk ? "ready" : "unreachable",
    },
  });
});

app.get("/api/catalog", (_req, res) => {
  res.json(catalog);
});

/** Search dashboards, glance panels, and catalog metrics/views for the top search bar. */
app.get("/api/search", async (req, res) => {
  const q = typeof req.query.q === "string" ? req.query.q.trim() : "";
  if (!q) {
    return res.json({
      query: "",
      dashboards: [],
      panels: [],
      metrics: [],
      best: null,
    });
  }

  try {
    const normalized = normalizeSearchText(q);
    const stopWords = new Set([
      "a",
      "an",
      "the",
      "of",
      "for",
      "to",
      "in",
      "on",
      "is",
      "are",
      "was",
      "what",
      "which",
      "who",
      "how",
      "show",
      "me",
      "my",
      "please",
      "current",
      "value",
      "values",
      "with",
      "from",
      "and",
      "or",
      "datasource",
      "data",
      "source",
    ]);
    const tokens = normalized
      .split(" ")
      .filter((token) => token && !stopWords.has(token));
    // Keep original tokens as a fallback when the query is only stop words
    const scoreTokens = tokens.length
      ? tokens
      : normalized.split(" ").filter(Boolean);

    const dashboards = LOCAL_DASHBOARDS.map((dashboard) => {
      const searchable = normalizeSearchText(
        `${dashboard.title} ${dashboard.terms.join(" ")} dashboard`
      );
      const score = scoreSearchTokens(scoreTokens, searchable);
      return {
        type: "dashboard",
        id: dashboard.uid,
        title: dashboard.title,
        description: `Grafana dashboard · ${dashboard.uid}`,
        url: dashboard.url,
        score,
      };
    })
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 8);

    const panelDefs = [
      {
        id: "cpu_busy",
        title: "CPU Busy %",
        terms: ["cpu", "usage", "busy", "processor", "node", "panel", "metric", "health"],
      },
      {
        id: "memory_avail",
        title: "Memory Available",
        terms: ["memory", "ram", "available", "node", "panel", "metric"],
      },
      {
        id: "gpu_temp",
        title: "GPU Temperature",
        terms: ["gpu", "temp", "temperature", "celsius", "thermal", "panel", "metric"],
      },
      {
        id: "gpu_util",
        title: "GPU Utilization",
        terms: ["gpu", "utilization", "usage", "compute", "load", "panel", "metric"],
      },
    ];

    const panelHits = panelDefs
      .map((panel) => {
        const searchable = normalizeSearchText(
          `${panel.title} ${panel.terms.join(" ")} panel`
        );
        return {
          type: "panel",
          id: panel.id,
          title: panel.title,
          description: `Live panel · ${panel.id}`,
          score: scoreSearchTokens(scoreTokens, searchable),
        };
      })
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 8);

    const metrics = [];
    for (const source of Object.values(catalog)) {
      for (const metric of source.metrics || []) {
        for (const query of metric.queries || []) {
          const searchable = normalizeSearchText(
            [
              source.label,
              source.id,
              metric.label,
              metric.id,
              metric.description,
              query.label,
              query.id,
              query.description,
              query.expr,
              "metric",
              "metrics",
              "stream",
              "view",
            ].join(" ")
          );
          const score = scoreSearchTokens(scoreTokens, searchable);
          if (score <= 0) continue;
          metrics.push({
            type: "metric",
            id: `${source.id}/${metric.id}/${query.id}`,
            title: query.label,
            description: `${source.label} · ${metric.label}`,
            sourceId: source.id,
            metricId: metric.id,
            queryId: query.id,
            unit: query.unit,
            score,
          });
        }
      }
    }
    metrics.sort((a, b) => b.score - a.score);
    const topMetrics = metrics.slice(0, 12);

    const candidates = [
      ...dashboards.map((d) => ({ ...d, kind: "dashboard" })),
      ...panelHits.map((p) => ({ ...p, kind: "panel" })),
      ...topMetrics.map((m) => ({ ...m, kind: "metric" })),
    ].sort((a, b) => b.score - a.score);

    // Prefer live panels slightly when the query is about cpu/memory health
    if (
      hasAnyTerm(normalized, ["cpu", "memory", "mem", "latency", "error", "errors"]) &&
      panelHits[0]
    ) {
      const panelBest = candidates.find((c) => c.kind === "panel");
      const metricBest = candidates.find((c) => c.kind === "metric");
      if (
        panelBest &&
        metricBest &&
        panelBest.score >= metricBest.score - 2
      ) {
        candidates.sort((a, b) => {
          if (a.id === panelBest.id) return -1;
          if (b.id === panelBest.id) return 1;
          return b.score - a.score;
        });
      }
    }

    let best = candidates[0] || null;
    let showcase = null;

    if (best?.kind === "panel") {
      const panel = await runLocalGlancePanel(best.id, "1h");
      showcase = {
        kind: "panel",
        item: best,
        panel,
      };
    } else if (best?.kind === "metric") {
      try {
        const found = findQuery(best.sourceId, best.metricId, best.queryId);
        if (found) {
          const end = Math.floor(Date.now() / 1000);
          const start = end - parseRangeSeconds("1h");
          const stepSec = pickStep(start, end);
          const defaultVars = Object.fromEntries(
            (found.query.vars || []).map((v) => [
              v.id,
              v.defaultValue !== undefined ? v.defaultValue : "__all__",
            ])
          );
          const resolvedExpr = interpolateVars(
            found.query.expr,
            found.query.vars,
            defaultVars
          );
          let series;
          let backend;
          if (best.sourceId === "opensearch") {
            if (OPENSEARCH_URL) {
              backend = "opensearch";
              series = await queryOpenSearchRange(
                found.query,
                start,
                end,
                stepSec,
                {}
              );
            } else {
              backend = "loki-fallback";
              series = await queryLokiRange(
                resolvedExpr,
                start,
                end,
                stepSec
              );
            }
          } else {
            backend = "prometheus";
            series = await queryPrometheusRange(
              resolvedExpr,
              start,
              end,
              stepSec
            );
          }
          showcase = {
            kind: "metric",
            item: best,
            result: {
              source: { id: found.source.id, label: found.source.label },
              metric: { id: found.metric.id, label: found.metric.label },
              query: {
                id: found.query.id,
                label: found.query.label,
                expr: found.query.expr,
                resolvedExpr,
                unit: found.query.unit,
                legend: found.query.legend,
              },
              backend,
              range: { start, end, step: stepSec, label: "1h" },
              series,
            },
          };
        }
      } catch (err) {
        showcase = {
          kind: "metric",
          item: best,
          error: err.message || "Query failed",
        };
      }
    } else if (best?.kind === "dashboard") {
      // Attach a related panel chart when useful (e.g. logs dashboard → error_logs)
      const relatedPanelId = hasAnyTerm(normalized, ["log", "logs", "loki", "opensearch"])
        ? "error_logs"
        : hasAnyTerm(normalized, ["error", "errors", "latency", "demo"])
          ? "demo_errors"
          : "cpu_busy";
      const panel = await runLocalGlancePanel(relatedPanelId, "1h");
      showcase = {
        kind: "dashboard",
        item: best,
        panel,
      };
    }

    // Enrich top panels with live series for the hit list previews (best only already loaded)
    const panels = await Promise.all(
      panelHits.slice(0, 4).map(async (hit) => {
        if (showcase?.kind === "panel" && showcase.item.id === hit.id) {
          return { ...hit, panel: showcase.panel };
        }
        const panel = await runLocalGlancePanel(hit.id, "1h");
        return { ...hit, panel };
      })
    );

    res.json({
      query: q,
      dashboards,
      panels,
      metrics: topMetrics,
      best,
      showcase,
    });
  } catch (err) {
    console.error("search error:", err);
    res.status(502).json({ error: err.message || "Search failed" });
  }
});

function scoreSearchTokens(tokens, searchable) {
  if (!tokens.length) return 0;
  let score = 0;
  for (const token of tokens) {
    if (!token) continue;
    if (searchable.includes(token)) score += token.length >= 4 ? 3 : 2;
    else if (token.length >= 3 && searchable.split(" ").some((w) => w.startsWith(token))) {
      score += 1;
    }
  }
  // Prefer exact phrase containment
  const phrase = tokens.join(" ");
  if (phrase && searchable.includes(phrase)) score += 4;
  return score;
}

// ---------- Dynamic label values (for variable dropdowns) ----------

app.get("/api/labels", async (req, res) => {
  const { source = "prometheus", metric, labelName } = req.query;

  if (typeof labelName !== "string" || !labelName.trim()) {
    return res.status(400).json({ error: "labelName query param is required" });
  }

  try {
    let values = [];

    if (source === "loki") {
      const url = `${LOKI_URL}/loki/api/v1/label/${encodeURIComponent(labelName)}/values`;
      try {
        const resp = await fetchBackend(url);
        const data = resp.ok ? await resp.json() : {};
        values = Array.isArray(data?.data) ? data.data : [];
      } catch {
        values = [];
      }
      if (labelName === "container") {
        values = Array.from(new Set([...values, ...CURATED_CONTAINERS]));
      }
    } else {
      // Prometheus label values API — optionally scoped to a specific metric
      const url = new URL(
        `${PROMETHEUS_URL}/api/v1/label/${encodeURIComponent(labelName)}/values`
      );
      if (metric) url.searchParams.append("match[]", String(metric));
      const resp = await fetchBackend(url.toString());
      const data = resp.ok ? await resp.json() : {};
      values = Array.isArray(data?.data) ? data.data : [];

      // CM / lab metrics often aren't scraped locally — if the scoped lookup
      // is empty, fall back to every value of that label (e.g. all instances).
      if (metric && values.length === 0 && labelName !== "__name__") {
        const fallbackUrl = `${PROMETHEUS_URL}/api/v1/label/${encodeURIComponent(labelName)}/values`;
        const fallbackResp = await fetchBackend(fallbackUrl);
        const fallbackData = fallbackResp.ok ? await fallbackResp.json() : {};
        values = Array.isArray(fallbackData?.data) ? fallbackData.data : [];
      }

      // Metric-name picker: merge live Prometheus names with curated screenshot list
      if (labelName === "__name__") {
        values = Array.from(
          new Set([...values, ...CURATED_METRICS, ...CM_TELEMETRY_METRICS])
        );
      }

      // Node (-n) picker: always include live instances + lab hosts from screenshots
      if (labelName === "instance") {
        values = Array.from(new Set([...values, ...CURATED_HOSTS]));
      }
    }

    res.json({ values: values.filter(Boolean).sort() });
  } catch (err) {
    console.error("labels error:", err);
    // Still return curated metrics so the Metric dropdown works offline
    if (labelName === "__name__") {
      return res.json({
        values: [...new Set([...CURATED_METRICS, ...CM_TELEMETRY_METRICS])].sort(),
      });
    }
    res.status(502).json({ error: err.message || "Failed to fetch label values" });
  }
});

// ---------- Direct query execution (no LLM) ----------

app.post("/api/query", async (req, res) => {
  try {
    const { sourceId, metricId, queryId, range = "1h", step, vars } = req.body || {};
    const found = findQuery(sourceId, metricId, queryId);
    if (!found) {
      return res.status(404).json({ error: "Unknown source / metric / query" });
    }

    const { source, metric, query } = found;

    // Interpolate user-selected variable values into the expression template
    const resolvedExpr = interpolateVars(query.expr, query.vars, vars);

    const rangeLabel = normalizeRangeLabel(range);
    const end = Math.floor(Date.now() / 1000);
    const start = end - parseRangeSeconds(rangeLabel);
    const requestedStep = Number(step);
    const stepSec = Number.isFinite(requestedStep) && requestedStep > 0
      ? Math.max(1, Math.min(Math.floor(requestedStep), 3600))
      : pickStep(start, end);

    let series;
    let backend;

    if (sourceId === "prometheus") {
      backend = "prometheus";
      series = await queryPrometheusRange(resolvedExpr, start, end, stepSec);
    } else if (sourceId === "opensearch") {
      if (OPENSEARCH_URL) {
        backend = "opensearch";
        series = await queryOpenSearchRange(query, start, end, stepSec, vars);
      } else {
        backend = "loki-fallback";
        series = await queryLokiRange(resolvedExpr, start, end, stepSec);
      }
    } else {
      return res.status(400).json({ error: `Unsupported source: ${sourceId}` });
    }

    const rendered = await proxyAdk("/api/adk/plot", {
      method: "POST",
      timeoutMs: 2500,
      body: JSON.stringify({
        title: query.label,
        unit: query.unit,
        series,
      }),
    });

    res.json({
      source: { id: source.id, label: source.label },
      metric: { id: metric.id, label: metric.label },
      query: {
        id: query.id,
        label: query.label,
        expr: query.expr,
        resolvedExpr,
        unit: query.unit,
        legend: query.legend,
      },
      backend,
      range: { start, end, step: stepSec, label: rangeLabel },
      series,
      plotImage: rendered?.image || null,
      chartType: rendered?.chart_type || null,
      usedLlm: false,
    });
  } catch (err) {
    console.error("query error:", err);
    res.status(502).json({
      error: err.message || "Query failed",
      detail: String(err.cause || err),
    });
  }
});

// ---------- Google ADK assistant (orchestrated path) ----------

/**
 * Simulated ADK orchestrator endpoint.
 * Classifies the question, picks a specialist path, runs a pre-mapped
 * tool query (no free-form LLM generation yet — deterministic demo path
 * that mirrors the ADK design in docs/next-phase-adk-mcp-design.md).
 *
 * When a real Google ADK agent is wired in, replace `runAdkFlow` with
 * the ADK runner while keeping this HTTP contract.
 */
app.post("/api/adk/chat", async (req, res) => {
  try {
    const { message } = req.body || {};
    if (typeof message !== "string" || !message.trim()) {
      return res.status(400).json({ error: "message is required" });
    }
    if (message.length > 2000) {
      return res.status(413).json({ error: "message must be 2000 characters or fewer" });
    }
    const normalizedMessage = message.trim();

    const remote = await proxyAdk("/api/adk/chat", {
      method: "POST",
      timeoutMs: 120000,
      body: JSON.stringify({
        message: normalizedMessage,
        use_llm: true,
        sessionId: req.body?.sessionId || req.body?.conversationId || null,
        conversation_id: req.body?.sessionId || req.body?.conversationId || null,
      }),
    });
    if (remote && remote.statusCode >= 200 && remote.statusCode < 300 && remote.data?.status !== "error") {
      return res.json(remote.data);
    }
    if (remote && remote.data && (remote.data.kind || remote.data.text || remote.data.proposal || remote.data.proposalId)) {
      return res.json(remote.data);
    }

    const result = await runAdkFlow(normalizedMessage);
    res.json(result);
  } catch (err) {
    console.error("adk error:", err);
    res.status(502).json({
      error: err.message || "ADK flow failed",
    });
  }
});

app.all("/api/proposals*", async (req, res) => {
  try {
    const remote = await proxyAdk(req.originalUrl, {
      method: req.method,
      timeoutMs: 30000,
      body: ["POST", "PUT", "PATCH"].includes(req.method) ? JSON.stringify(req.body) : undefined,
    });
    if (remote) return res.status(remote.statusCode).json(remote.data);
    res.status(502).json({ error: "Backend proposal service unreachable" });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.all("/api/conversations*", async (req, res) => {
  try {
    const remote = await proxyAdk(req.originalUrl, {
      method: req.method,
      timeoutMs: ADK_PROXY_TIMEOUT_MS,
      body: ["POST", "PUT", "PATCH"].includes(req.method) ? JSON.stringify(req.body) : undefined,
    });
    if (remote) return res.status(remote.statusCode).json(remote.data);
    res.status(502).json({ error: "Backend conversation service unreachable" });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.all("/api/v1/query*", async (req, res) => {
  try {
    const remote = await proxyAdk(req.originalUrl, {
      method: req.method,
      timeoutMs: ADK_PROXY_TIMEOUT_MS,
      body: ["POST", "PUT", "PATCH"].includes(req.method) ? JSON.stringify(req.body) : undefined,
    });
    if (remote) return res.status(remote.statusCode).json(remote.data);
    res.status(502).json({ error: "Backend query service unreachable" });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.all("/api/alert-proposals*", async (req, res) => {
  try {
    const remote = await proxyAdk(req.originalUrl, {
      method: req.method,
      timeoutMs: 30000,
      body: ["POST", "PUT", "PATCH"].includes(req.method) ? JSON.stringify(req.body) : undefined,
    });
    if (remote) return res.status(remote.statusCode).json(remote.data);
    res.status(502).json({ error: "Backend alert proposal service unreachable" });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get("/api/alerts", async (req, res) => {
  try {
    const remote = await proxyAdk(req.originalUrl, { method: "GET" });
    if (remote) return res.json(remote);
    res.status(502).json({ error: "Backend alerts service unreachable" });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get("/api/adk/examples", (_req, res) => {
  res.json({
    examples: [
      "Which service has the highest error count?",
      "Is checkout latency increasing?",
      "Show recent error logs",
      "What is the current CPU busy percentage?",
      "Which dashboard shows my custom dataset?",
      "Show container memory usage",
    ],
  });
});

// ---------- ADK core flow bridge ----------

const ADK_FLOW_CATALOG = [
  {
    id: "full_glance_board",
    title: "System health board",
    description: "Run a small metrics + logs board through ADK tools.",
    intent: "glance",
    prompt: "Show me a quick glance of system health",
    agents: ["Orchestrator", "MetricsAgent", "LogsAgent", "SummaryAgent"],
  },
  {
    id: "errors_by_service",
    title: "Highest error service",
    description: "Rank demo services by their latest error count.",
    intent: "metrics",
    prompt: "Which service has the highest error count?",
    agents: ["Orchestrator", "MetricsAgent", "SummaryAgent"],
  },
  {
    id: "cpu_glance",
    title: "CPU quick glance",
    description: "Plot current host CPU busy percentage.",
    intent: "metrics",
    prompt: "What is the current CPU busy percentage?",
    agents: ["Orchestrator", "MetricsAgent", "SummaryAgent"],
  },
  {
    id: "latency_trend",
    title: "Latency trend",
    description: "Plot the demo latency trend for the last hour.",
    intent: "metrics",
    prompt: "Is checkout latency increasing?",
    agents: ["Orchestrator", "MetricsAgent", "SummaryAgent"],
  },
  {
    id: "error_logs",
    title: "Recent error logs",
    description: "Plot the recent error log rate from the log source.",
    intent: "logs",
    prompt: "Show recent error logs",
    agents: ["Orchestrator", "LogsAgent", "SummaryAgent"],
  },
  {
    id: "dataset_dashboard",
    title: "Find dataset dashboard",
    description: "Find the saved dashboard for the demo request dataset.",
    intent: "dashboard",
    prompt: "Which dashboard shows my custom dataset?",
    agents: ["Orchestrator", "DashboardAgent", "SummaryAgent"],
  },
];

app.get("/api/adk/flows", async (_req, res) => {
  try {
    const remote = await proxyAdk("/api/adk/flows", { timeoutMs: 1500 });
    res.json(remote || { flows: ADK_FLOW_CATALOG, framework: "Google ADK core" });
  } catch (error) {
    console.error("adk flow catalog error:", error);
    res.json({ flows: ADK_FLOW_CATALOG, framework: "Google ADK core" });
  }
});

app.post("/api/adk/flows/run", async (req, res) => {
  try {
    const flowId = typeof req.body?.flow_id === "string" ? req.body.flow_id : "";
    const flow = ADK_FLOW_CATALOG.find((item) => item.id === flowId);
    if (!flow) return res.status(404).json({ error: "Unknown ADK flow" });

    const remote = await proxyAdk("/api/adk/flows/run", {
      method: "POST",
      timeoutMs: 5000,
      body: JSON.stringify({ flow_id: flowId }),
    });
    if (remote && remote.status !== "error") return res.json(remote);

    const result = await runAdkFlow(flow.prompt);
    res.json({
      ...result,
      framework: "Google ADK core tools (local fallback)",
      flow: { ...flow },
      charts: result.charts || (result.series
        ? [
            {
              title: flow.title,
              unit: inferUnitFromQuery(result.queryUsed),
              expr: result.queryUsed,
              series: result.series,
              status: "success",
            },
          ]
        : []),
    });
  } catch (error) {
    console.error("adk flow error:", error);
    res.status(502).json({ error: error.message || "ADK flow failed" });
  }
});

app.post("/api/adk/glance/board", async (req, res) => {
  try {
    const defaultIds = ["cpu_busy", "memory_avail", "gpu_temp", "gpu_util"];
    const requestedIds = req.body?.panel_ids;
    if (requestedIds != null && !Array.isArray(requestedIds)) {
      return res.status(400).json({ error: "panel_ids must be an array" });
    }
    const ids = [...new Set(
      (requestedIds || defaultIds)
        .filter((id) => typeof id === "string" && /^[a-z0-9_]{1,80}$/.test(id))
    )].slice(0, MAX_BOARD_PANELS);
    if (!ids.length) {
      return res.status(400).json({ error: "At least one valid panel_id is required" });
    }
    const rangeLabel = normalizeRangeLabel(req.body?.range_label || "1h");
    const remote = await proxyAdk("/api/adk/glance/board", {
      method: "POST",
      timeoutMs: 5000,
      body: JSON.stringify({ panel_ids: ids, range_label: rangeLabel }),
    });
    if (remote && Array.isArray(remote.panels)) return res.json(remote);

    const panels = await Promise.all(ids.map((id) => runLocalGlancePanel(id, rangeLabel)));
    res.json({ status: "success", range: rangeLabel, panels });
  } catch (error) {
    console.error("adk glance board error:", error);
    res.status(502).json({ error: error.message || "Glance board failed" });
  }
});

// ---------- Helpers ----------

function readPositiveIntEnv(name, fallback, maximum) {
  const value = Number(process.env[name]);
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.min(Math.floor(value), maximum);
}

function normalizeTimeout(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0
    ? Math.min(Math.floor(parsed), 120000)
    : fallback;
}

function inferUnitFromQuery(expr) {
  if (!expr) return "short";
  if (expr.includes("_bytes")) return "bytes";
  if (expr.includes("latency") || expr.includes("_ms")) return "ms";
  if (expr.includes("percent") || expr.includes("CPU") || expr.includes("node_cpu")) return "percent";
  return "short";
}

function parseRangeSeconds(range) {
  const m = String(range).match(/^(\d{1,7})([smhd])$/);
  if (!m) return 3600;
  const n = Number(m[1]);
  const unit = { s: 1, m: 60, h: 3600, d: 86400 }[m[2]];
  if (!Number.isFinite(n) || n <= 0) return 3600;
  return Math.min(n * unit, 7 * 86400);
}

function normalizeRangeLabel(range) {
  const value = String(range || "1h").trim().toLowerCase();
  const match = value.match(/^(\d{1,7})([smhd])$/);
  if (!match || Number(match[1]) <= 0) return "1h";
  const seconds = parseRangeSeconds(value);
  return seconds >= 7 * 86400 ? "7d" : `${Number(match[1])}${match[2]}`;
}

function pickStep(start, end) {
  const span = end - start;
  if (span <= 900) return 15;
  if (span <= 3600) return 30;
  if (span <= 21600) return 60;
  if (span <= 86400) return 300;
  return 600;
}

async function queryPrometheusRange(expr, start, end, step) {
  const url = new URL("/api/v1/query_range", PROMETHEUS_URL);
  url.searchParams.set("query", expr);
  url.searchParams.set("start", String(start));
  url.searchParams.set("end", String(end));
  url.searchParams.set("step", String(step));

  const resp = await fetchBackend(url);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Prometheus ${resp.status}: ${text.slice(0, 200)}`);
  }
  const data = await resp.json();
  if (data.status !== "success") {
    throw new Error(`Prometheus error: ${JSON.stringify(data.error || data)}`);
  }
  return (data.data?.result || []).map(normalizePromSeries);
}

async function queryLokiRange(expr, start, end, step) {
  // Loki metric queries use nanosecond timestamps
  const url = new URL("/loki/api/v1/query_range", LOKI_URL);
  url.searchParams.set("query", expr);
  url.searchParams.set("start", String(BigInt(start) * 1_000_000_000n));
  url.searchParams.set("end", String(BigInt(end) * 1_000_000_000n));
  url.searchParams.set("step", String(step));

  const resp = await fetchBackend(url);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Loki ${resp.status}: ${text.slice(0, 200)}`);
  }
  const data = await resp.json();
  if (data.status !== "success") {
    throw new Error(`Loki error: ${JSON.stringify(data.error || data)}`);
  }
  return (data.data?.result || []).map(normalizePromSeries);
}

async function queryOpenSearchRange(query, start, end, step, vars = {}) {
  if (!OPENSEARCH_URL) throw new Error("OPENSEARCH_URL is not configured");
  const index =
    (vars.INDEX && vars.INDEX !== "__all__" && vars.INDEX) ||
    query.opensearch?.filters?.index ||
    OPENSEARCH_INDEX;
  const url = new URL(
    `${index}/_search`,
    `${OPENSEARCH_URL.replace(/\/$/, "")}/`
  );
  const filters = { ...(query.opensearch?.filters || {}) };
  delete filters.index;
  if (vars.LEVEL && vars.LEVEL !== "__all__") filters.level = vars.LEVEL;
  if (vars.CONTAINER && vars.CONTAINER !== "__all__") {
    filters.container = vars.CONTAINER;
  }
  if (vars.HOST && vars.HOST !== "__all__") {
    filters.keyword = filters.keyword
      ? `${filters.keyword} AND ${vars.HOST}`
      : vars.HOST;
  }
  if (vars.PORT && vars.PORT !== "__all__") {
    filters.keyword = filters.keyword
      ? `${filters.keyword} AND ${vars.PORT}`
      : vars.PORT;
  }
  if (vars.LOG_FILTER && vars.LOG_FILTER !== "__all__") {
    filters.keyword = filters.keyword
      ? `${filters.keyword} AND ${vars.LOG_FILTER}`
      : vars.LOG_FILTER;
  }
  const filterClauses = Object.entries(filters).flatMap(([field, value]) => {
    if (field === "keyword") {
      return [{ query_string: { query: String(value) } }];
    }
    return [{ term: { [field]: value } }];
  });
  const body = {
    size: 0,
    query: {
      bool: {
        filter: [
          {
            range: {
              [OPENSEARCH_TIMESTAMP_FIELD]: {
                gte: new Date(start * 1000).toISOString(),
                lte: new Date(end * 1000).toISOString(),
              },
            },
          },
          ...filterClauses,
        ],
      },
    },
    aggs: {
      events_over_time: {
        date_histogram: {
          field: OPENSEARCH_TIMESTAMP_FIELD,
          fixed_interval: `${step}s`,
          min_doc_count: 0,
          extended_bounds: {
            min: new Date(start * 1000).toISOString(),
            max: new Date(end * 1000).toISOString(),
          },
        },
      },
    },
  };
  const resp = await fetchBackend(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`OpenSearch ${resp.status}: ${text.slice(0, 240)}`);
  }
  const data = await resp.json();
  const buckets = data.aggregations?.events_over_time?.buckets || [];
  return [
    {
      name: query.legend || "log events",
      labels: { source: "opensearch" },
      points: buckets.map((bucket) => ({
        t: Math.floor(Number(bucket.key) / 1000),
        v: Number(bucket.doc_count || 0),
      })).filter((point) => Number.isFinite(point.t) && Number.isFinite(point.v)),
    },
  ];
}

function fetchBackend(url, init = {}) {
  return fetch(url, {
    ...init,
    signal: AbortSignal.timeout(BACKEND_TIMEOUT_MS),
  });
}

async function runLocalGlancePanel(panelId, rangeLabel = "1h") {
  const panels = {
    cpu_busy: {
      title: "CPU Busy %",
      expr: '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
      unit: "percent",
      source: "prometheus",
    },
    memory_avail: {
      title: "Memory Available",
      expr: "node_memory_MemAvailable_bytes",
      unit: "bytes",
      source: "prometheus",
    },
    gpu_temp: {
      title: "GPU Temperature",
      expr: "DCGM_FI_DEV_GPU_TEMP",
      unit: "celsius",
      source: "prometheus",
    },
    gpu_util: {
      title: "GPU Utilization",
      expr: "DCGM_FI_DEV_GPU_UTIL",
      unit: "percent",
      source: "prometheus",
    },
    demo_errors: { title: "GPU Temperature", expr: "DCGM_FI_DEV_GPU_TEMP", unit: "celsius", source: "prometheus" },
    demo_latency: { title: "Memory Available", expr: "node_memory_MemAvailable_bytes", unit: "bytes", source: "prometheus" },
    error_logs: { title: "GPU Utilization", expr: "DCGM_FI_DEV_GPU_UTIL", unit: "percent", source: "prometheus" },
  };
  const panel = panels[panelId];
  if (!panel) return { panel_id: panelId, title: panelId, status: "error", error: "Unknown panel", series: [] };
  const end = Math.floor(Date.now() / 1000);
  const normalizedRange = normalizeRangeLabel(rangeLabel);
  const start = end - parseRangeSeconds(normalizedRange);
  const step = pickStep(start, end);
  try {
    const series = panel.source === "prometheus"
      ? await queryPrometheusRange(panel.expr, start, end, step)
      : await queryLokiRange(panel.expr, start, end, step);
    const latestPoint = series
      .flatMap((item) => item.points)
      .filter((point) => point.v != null && Number.isFinite(point.t))
      .sort((a, b) => a.t - b.t)
      .at(-1);
    return {
      panel_id: panelId,
      title: panel.title,
      unit: panel.unit,
      expr: panel.expr,
      source: panel.source,
      status: "success",
      latest_value: latestPoint?.v ?? null,
      series,
    };
  } catch (error) {
    return { panel_id: panelId, title: panel.title, unit: panel.unit, expr: panel.expr, source: panel.source, status: "error", error: error.message, series: [] };
  }
}

async function proxyAdk(path, init = {}) {
  let target;
  try {
    target = new URL(path, `${ADK_SERVICE_URL.replace(/\/$/, "")}/`);
  } catch {
    return null;
  }
  const body = init.body ? String(init.body) : "";
  return new Promise((resolve) => {
    const transport = target.protocol === "https:" ? https : http;
    const request = transport.request(
      {
        hostname: target.hostname,
        port: target.port || (target.protocol === "https:" ? 443 : 80),
        path: `${target.pathname}${target.search}`,
        method: init.method || "GET",
        headers: {
          "content-type": "application/json",
          ...(body ? { "content-length": Buffer.byteLength(body) } : {}),
        },
      },
      (response) => {
        let payload = "";
        let payloadBytes = 0;
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          payloadBytes += Buffer.byteLength(chunk);
          if (payloadBytes > MAX_ADK_PROXY_BYTES) {
            response.destroy();
            resolve(null);
            return;
          }
          payload += chunk;
        });
        response.on("end", () => {
          let parsed;
          try {
            parsed = payload ? JSON.parse(payload) : {};
          } catch {
            parsed = { raw: payload };
          }
          resolve({ statusCode: response.statusCode || 200, data: parsed });
        });
      }
    );
    request.setTimeout(normalizeTimeout(init.timeoutMs, ADK_PROXY_TIMEOUT_MS), () => {
      console.error("proxyAdk timeout on:", target.href);
      request.destroy();
      resolve(null);
    });
    request.on("error", (err) => {
      console.error("proxyAdk error on:", target.href, err.message);
      resolve(null);
    });
    if (body) request.write(body);
    request.end();
  });
}

function normalizePromSeries(item) {
  const labels = item.metric || {};
  const name =
    labels.service ||
    labels.name ||
    labels.container ||
    labels.instance ||
    labels.mountpoint ||
    Object.entries(labels)
      .filter(([k]) => k !== "__name__")
      .map(([k, v]) => `${k}=${v}`)
      .join(", ") ||
    "series";

  const points = (Array.isArray(item.values) ? item.values : [])
    .map(([ts, val]) => {
      const timestamp = Number(ts);
      const value = Number(val);
      return {
        t: timestamp,
        v:
          val === "NaN" ||
          val === "+Inf" ||
          val === "-Inf" ||
          !Number.isFinite(value)
            ? null
            : value,
      };
    })
    .filter((point) => Number.isFinite(point.t));

  return { name, labels, points };
}

function normalizeSearchText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function hasAnyTerm(normalizedText, terms) {
  const padded = ` ${normalizedText} `;
  return terms.some((term) => padded.includes(` ${normalizeSearchText(term)} `));
}

const LOCAL_DASHBOARDS = [
  {
    title: "Observability Overview",
    uid: "observability-overview",
    url: `${GRAFANA_URL}/d/observability-overview/observability-overview`,
    terms: ["observability", "overview", "cpu", "memory", "disk", "container", "containers", "metric", "metrics", "health"],
  },
  {
    title: "Logs Overview",
    uid: "logs-overview",
    url: `${GRAFANA_URL}/d/logs-overview/logs-overview`,
    terms: ["log", "logs", "logging", "error log", "warning", "loki", "opensearch"],
  },
  {
    title: "Demo Dataset Overview",
    uid: "demo-dataset-overview",
    url: `${GRAFANA_URL}/d/demo-dataset-overview/demo-dataset-overview`,
    terms: ["dataset", "csv", "demo", "request", "requests", "error", "errors", "latency", "service", "services"],
  },
];

function searchLocalDashboards(message) {
  const normalized = normalizeSearchText(message);
  const tokens = normalized.split(" ").filter(Boolean);
  const scored = LOCAL_DASHBOARDS.map((dashboard) => {
    const searchable = normalizeSearchText(`${dashboard.title} ${dashboard.terms.join(" ")}`);
    const score = tokens.reduce(
      (total, token) => total + (hasAnyTerm(searchable, [token]) ? 1 : 0),
      0
    );
    return { dashboard, score };
  }).sort((a, b) => b.score - a.score);
  return scored[0]?.score > 0 ? scored[0].dashboard : LOCAL_DASHBOARDS[0];
}

function classifyIntent(message) {
  const q = normalizeSearchText(message);
  if (hasAnyTerm(q, ["dashboard", "dashboards", "open dashboard", "where do i find"])) {
    return "dashboard";
  }
  if (hasAnyTerm(q, ["glance", "overview", "health", "system health", "board"])) {
    return "glance";
  }
  if (hasAnyTerm(q, ["log", "logs", "logging", "error log", "warn", "warning", "opensearch"])) {
    return "logs";
  }
  if (hasAnyTerm(q, ["error", "errors", "latency", "cpu", "memory", "request", "requests", "metric", "metrics", "container", "containers", "service", "services"])) {
    return "metrics";
  }
  return "metrics";
}

const ADK_ROUTES = {
  glance: {
    agent: "MetricsAgent",
    tools: ["query_prometheus", "query_loki_logs"],
  },
  metrics: {
    agent: "MetricsAgent",
    tools: ["list_prometheus_metric_names", "query_prometheus"],
  },
  logs: {
    agent: "LogsAgent",
    tools: ["list_loki_label_names", "query_loki_logs"],
  },
  dashboard: {
    agent: "DashboardAgent",
    tools: ["search_dashboards", "get_dashboard_by_uid"],
  },
};

async function runAdkFlow(message) {
  const intent = classifyIntent(message);
  const route = ADK_ROUTES[intent];
  const steps = [
    {
      step: 1,
      agent: "Orchestrator",
      action: "classify_intent",
      result: intent,
    },
    {
      step: 2,
      agent: route.agent,
      action: "select_tools",
      result: route.tools,
    },
  ];

  let answer = "";
  let queryUsed = null;
  let series = null;
  let charts = null;
  let dashboardLink = null;
  let toolResult = null;

  if (intent === "glance") {
    const panelIds = ["cpu_busy", "memory_avail", "gpu_temp", "gpu_util"];
    const panels = await Promise.all(panelIds.map((panelId) => runLocalGlancePanel(panelId, "1h")));
    charts = panels.filter((panel) => panel.status === "success");
    const failures = panels.filter((panel) => panel.status !== "success");
    answer = failures.length
      ? `System overview loaded with ${charts.length} available charts and ${failures.length} unavailable data source${failures.length === 1 ? "" : "s"}.`
      : `System overview loaded with **${charts.length} charts** from local metrics and logs.`;
    steps.push({
      step: 3,
      agent: "MetricsAgent",
      action: "build_glance",
      result: panels.map((panel) => ({ panel_id: panel.panel_id, status: panel.status })),
    });
    steps.push({ step: 4, agent: "SummaryAgent", action: "summarize", result: "ok" });
    dashboardLink = `${GRAFANA_URL}/d/observability-overview/observability-overview`;
    toolResult = { panels };
  } else if (intent === "dashboard") {
    const dashboards = LOCAL_DASHBOARDS.map(({ terms: _terms, ...dashboard }) => dashboard);
    const selected = searchLocalDashboards(message);
    const pick = dashboards.find((dashboard) => dashboard.uid === selected.uid) || dashboards[0];

    steps.push({
      step: 3,
      agent: "DashboardAgent",
      action: "search_dashboards",
      result: dashboards.map((d) => d.title),
    });
    steps.push({
      step: 4,
      agent: "SummaryAgent",
      action: "summarize",
      result: "ok",
    });

    answer = `I would open **${pick.title}**. Direct link: ${pick.url}`;
    dashboardLink = pick.url;
    toolResult = { dashboards, selected: pick };
  } else if (intent === "logs") {
    const expr =
      'sum(count_over_time({container="log-generator-ai-lab"} |= "level=error" [5m]))';
    queryUsed = expr;
    const end = Math.floor(Date.now() / 1000);
    const start = end - 3600;
    try {
      series = await queryLokiRange(expr, start, end, 60);
      const last = series?.[0]?.points?.filter((p) => p.v != null).at(-1)?.v;
      answer =
        last != null
          ? `Recent error log rate is about **${Number(last).toFixed(2)}** events over the 5m window (container \`log-generator-ai-lab\`).`
          : "No error log samples returned for the last hour. Is the log-generator container running?";
    } catch (e) {
      answer = `Logs agent could not reach Loki: ${e.message}. Start the stack with \`docker compose up -d\`.`;
    }
    steps.push({
      step: 3,
      agent: "LogsAgent",
      action: "query_loki_logs",
      result: expr,
    });
    steps.push({
      step: 4,
      agent: "SummaryAgent",
      action: "summarize",
      result: "ok",
    });
    dashboardLink = `${GRAFANA_URL}/d/logs-overview/logs-overview`;
  } else {
    // metrics
    const q = normalizeSearchText(message);
    let expr = "demo_errors_total";
    let label = "Errors by service";
    let unit = "short";

    if (hasAnyTerm(q, ["latency", "checkout", "response time"])) {
      expr = "demo_latency_ms";
      label = "Latency (ms)";
      unit = "ms";
    } else if (hasAnyTerm(q, ["cpu", "processor"])) {
      expr =
        '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)';
      label = "CPU busy %";
      unit = "percent";
    } else if (hasAnyTerm(q, ["memory", "ram"])) {
      if (hasAnyTerm(q, ["container", "containers"])) {
        expr = 'sum(container_memory_usage_bytes{name!=""}) by (name)';
        label = "container memory";
        unit = "bytes";
      } else {
        expr =
          "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes";
        label = "memory used";
        unit = "bytes";
      }
    } else if (hasAnyTerm(q, ["request", "requests", "traffic", "throughput"])) {
      expr = "demo_requests_total";
      label = "demo requests";
    } else if (hasAnyTerm(q, ["error", "errors", "service", "services"])) {
      expr = "demo_errors_total";
      label = "Errors by service";
    }

    queryUsed = expr;
    const end = Math.floor(Date.now() / 1000);
    const start = end - 3600;
    try {
      series = await queryPrometheusRange(expr, start, end, 30);
      if (!series.length) {
        answer = `Metrics agent ran \`${expr}\` but got no series. Check that Prometheus and exporters are up.`;
      } else if (expr === "demo_errors_total") {
        const ranked = series
          .map((s) => ({
            name: s.labels.service || s.name,
            last: s.points.filter((p) => p.v != null).at(-1)?.v ?? 0,
          }))
          .sort((a, b) => b.last - a.last);
        const top = ranked[0];
        answer = top
          ? `**${top.name}** currently has the highest error count (**${top.last}**). Query used: \`${expr}\`.`
          : `Ran \`${expr}\` successfully.`;
      } else {
        const last = series[0].points.filter((p) => p.v != null).at(-1)?.v;
        answer =
          last != null
            ? `Latest **${label}** ≈ **${formatValue(last, unit)}**. Query used: \`${expr}\`.`
            : `Ran \`${expr}\`; series present but no numeric samples.`;
      }
    } catch (e) {
      answer = `Metrics agent could not reach Prometheus: ${e.message}. Start the stack with \`docker compose up -d\`.`;
    }

    steps.push({
      step: 3,
      agent: "MetricsAgent",
      action: "query_prometheus",
      result: expr,
    });
    steps.push({
      step: 4,
      agent: "SummaryAgent",
      action: "summarize",
      result: "ok",
    });
    dashboardLink =
      expr.startsWith("demo_")
        ? `${GRAFANA_URL}/d/demo-dataset-overview/demo-dataset-overview`
        : `${GRAFANA_URL}/d/observability-overview/observability-overview`;
  }

  return {
    framework: "Google ADK (demo orchestrator)",
    intent,
    agents: ["Orchestrator", route.agent, "SummaryAgent"],
    steps,
    answer,
    queryUsed,
    series,
    charts,
    dashboardLink,
    usedLlm: false,
    note: "Deterministic ADK-style flow for the lab demo. Swap runAdkFlow() for a real Google ADK runner when ready.",
  };
}

function formatValue(v, unit) {
  if (unit === "bytes") {
    if (v > 1e9) return `${(v / 1e9).toFixed(2)} GB`;
    if (v > 1e6) return `${(v / 1e6).toFixed(2)} MB`;
    if (v > 1e3) return `${(v / 1e3).toFixed(2)} KB`;
    return `${v.toFixed(0)} B`;
  }
  if (unit === "percent") return `${Number(v).toFixed(2)}%`;
  if (unit === "ms") return `${Number(v).toFixed(1)} ms`;
  return Number(v).toFixed(3);
}

app.use((error, _req, res, _next) => {
  if (error?.type === "entity.too.large") {
    return res.status(413).json({ error: "Request body is too large" });
  }
  if (error instanceof SyntaxError && "body" in error) {
    return res.status(400).json({ error: "Request body must be valid JSON" });
  }
  console.error("unhandled API error:", error);
  return res.status(500).json({ error: "Internal server error" });
});

app.listen(PORT, () => {
  console.log(`Grafana AI API listening on http://localhost:${PORT}`);
  console.log(`  Prometheus: ${PROMETHEUS_URL}`);
  console.log(`  Loki:       ${LOKI_URL}`);
  console.log(`  ADK service: ${ADK_SERVICE_URL}`);
});
