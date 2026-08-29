import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { adkChat, confirmAlert, fetchAdkBoard } from "../api";
import type { AdkChatResponse, AlertRuleProposal, ChatMessage, GlancePanelResult } from "../types";
import ProposalCard from "./ProposalCard";
import TimeSeriesChart from "./TimeSeriesChart";

const DEFAULT_PANEL_IDS = [
  "demo_errors",
  "demo_latency",
  "cpu_busy",
  "error_logs",
];

function uid() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

type Props = {
  externalPrompt?: string | null;
  onExternalPromptConsumed?: () => void;
};

export default function AdkAssistantTab({
  externalPrompt = null,
  onExternalPromptConsumed,
}: Props) {
  const [board, setBoard] = useState<GlancePanelResult[]>([]);
  const [boardLoading, setBoardLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "welcome",
      role: "assistant",
      content: "Ask about metrics, logs, or dashboards.",
    },
  ]);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void loadBoard();
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  useEffect(() => {
    if (!externalPrompt) return;
    const prompt = externalPrompt.trim();
    onExternalPromptConsumed?.();
    if (prompt) void send(prompt);
  }, [externalPrompt]); // eslint-disable-line react-hooks/exhaustive-deps

  async function loadBoard() {
    setBoardLoading(true);
    try {
      const result = await fetchAdkBoard(DEFAULT_PANEL_IDS);
      setBoard(result.panels || []);
    } catch {
      setBoard([]);
    } finally {
      setBoardLoading(false);
    }
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || loading) return;
    setMessages((prev) => [
      ...prev,
      { id: uid(), role: "user", content: message },
    ]);
    setInput("");
    setLoading(true);
    try {
      const response = await adkChat(message, sessionId);
      if (response.sessionId) {
        setSessionId(response.sessionId);
      } else {
        setSessionId(null);
      }
      appendResponse(normalizeResponse(response));
    } catch (error) {
      appendResponse({
        framework: "FastAPI + SKILL.md",
        intent: "error",
        agents: ["Router", "Generator", "Validator", "Executor"],
        steps: [],
        answer: error instanceof Error ? error.message : String(error),
        queryUsed: null,
        series: null,
        dashboardLink: null,
        usedLlm: false,
      });
    } finally {
      setLoading(false);
    }
  }

  function appendResponse(response: AdkChatResponse) {
    setMessages((prev) => [
      ...prev,
      {
        id: uid(),
        role: "assistant",
        content: response.answer,
        meta: response,
      },
    ]);
  }

  function onSelectCandidate(candidateName: string) {
    void send(candidateName);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void send(input);
  }

  const boardStats = useMemo(() => {
    const live = board.filter((p) => p.status === "success").length;
    const total = board.length;
    return { live, offline: Math.max(total - live, 0), total };
  }, [board]);

  return (
    <div className="glance-shell">
      <div className="glance-workspace">
        <aside className="glance-metrics" aria-label="Live metrics">
          <header className="glance-panel-head">
            <div>
              <h3>Metrics</h3>
              <p className="glance-sub rail">
                {boardLoading
                  ? "Loading…"
                  : boardStats.total
                    ? `${boardStats.live} live · ${boardStats.offline} offline`
                    : "No panels"}
              </p>
            </div>
            <button
              type="button"
              className="btn quiet"
              onClick={() => void loadBoard()}
              disabled={boardLoading}
            >
              {boardLoading ? "…" : "Refresh"}
            </button>
          </header>

          <div className="metrics-rail">
            {boardLoading &&
              [0, 1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="glance-card skeleton"
                  aria-hidden="true"
                >
                  <div className="skel-line w40" />
                  <div className="skel-line w70" />
                  <div className="skel-chart" />
                </div>
              ))}

            {!boardLoading &&
              board.map((panel) => (
                <GlanceCard
                  key={panel.panel_id}
                  panel={panel}
                  expanded={expandedId === panel.panel_id}
                  onToggle={() =>
                    setExpandedId((id) =>
                      id === panel.panel_id ? null : panel.panel_id
                    )
                  }
                />
              ))}

            {!boardLoading && !board.length && (
              <div className="metrics-rail-empty">
                <p className="query-empty-title">No metrics</p>
                <p className="muted small">
                  Start local services, then refresh.
                </p>
              </div>
            )}
          </div>
        </aside>

        <section className="glance-chat">
          <header className="glance-panel-head chat">
            <div>
              <h3>Chat</h3>
              <p className="glance-sub rail">Ask anything about your system</p>
            </div>
            {loading && <span className="chat-status-live">Working…</span>}
          </header>

          <div className="chat-log">
            {messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                onSelectCandidate={onSelectCandidate}
              />
            ))}
            {loading && (
              <div className="bubble assistant pending">
                <div className="bubble-role">Assistant</div>
                <div className="bubble-body muted">
                  <span className="typing-dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  Reading data
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          <div
            className="sample-prompt-chips"
            style={{
              display: "flex",
              gap: "6px",
              flexWrap: "wrap",
              padding: "6px 16px",
              background: "rgba(0,0,0,0.15)",
            }}
          >
            {[
              { label: "📋 Create Dashboard", prompt: "Create a dashboard on Grafana." },
              { label: "📈 CPU Utilization", prompt: "What is the CPU utilization on node-exporter:9100?" },
              { label: "📊 Memory Usage", prompt: "Show memory utilization on my instances" },
              { label: "🔍 List Dashboards", prompt: "List all available dashboards" },
              { label: "📜 Error Logs", prompt: "Show recent error logs" },
            ].map((chip) => (
              <button
                key={chip.label}
                type="button"
                className="chip"
                onClick={() => {
                  setInput(chip.prompt);
                }}
                style={{
                  fontSize: "11px",
                  padding: "3px 8px",
                  borderRadius: "12px",
                  border: "1px solid rgba(255,255,255,0.1)",
                  background: "rgba(255,255,255,0.04)",
                  color: "var(--muted, #999)",
                  cursor: "pointer",
                }}
              >
                {chip.label}
              </button>
            ))}
          </div>

          <form className="chat-input" onSubmit={onSubmit}>
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Ask about metrics, logs, or dashboards"
              disabled={loading}
              aria-label="Question"
            />
            <button
              type="submit"
              className="btn primary"
              disabled={loading || !input.trim()}
            >
              Ask
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}

function GlanceCard({
  panel,
  expanded,
  onToggle,
}: {
  panel: GlancePanelResult;
  expanded: boolean;
  onToggle: () => void;
}) {
  const ok = panel.status === "success";
  const hasSeries = ok && panel.series?.length > 0;
  const kind = sourceKind(panel.source);
  const errorCopy = friendlyPanelError(panel.error);
  const chartHeight = expanded ? 180 : 72;

  return (
    <article
      className={[
        "glance-card",
        ok ? "" : "is-offline",
        expanded ? "is-expanded" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <button
        type="button"
        className="glance-card-toggle"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <div className="glance-card-head">
          <div className="glance-card-id">
            <span className={`source-mark ${kind}`}>
              {kind === "logs" ? "L" : "M"}
            </span>
            <div className="glance-card-titles">
              <span className="glance-source">
                {friendlySource(panel.source)}
              </span>
              <h4 title={panel.title}>{friendlyPanelTitle(panel)}</h4>
            </div>
          </div>
          <div className="glance-card-right">
            <span className={ok ? "card-status live" : "card-status"}>
              {ok ? "Live" : "Offline"}
            </span>
            <span className="expand-hint" aria-hidden="true">
              {expanded ? "▾" : "▸"}
            </span>
          </div>
        </div>

        <div className="glance-value-row">
          <span className="glance-value-label">Latest</span>
          <strong className="glance-value">
            {formatLatest(panel.latest_value, panel.unit)}
          </strong>
        </div>
      </button>

      <div
        className={
          expanded ? "glance-chart-slot is-expanded" : "glance-chart-slot"
        }
      >
        {hasSeries ? (
          panel.image ? (
            <img
              className="glance-image"
              src={panel.image}
              alt={`${panel.title} trend`}
              style={{ height: chartHeight }}
            />
          ) : (
            <TimeSeriesChart
              series={panel.series}
              unit={panel.unit}
              height={chartHeight}
              compact
            />
          )
        ) : (
          <div className="mini-empty">
            <span className="mini-empty-title">
              {ok ? "No series" : "Unavailable"}
            </span>
            <span className="mini-empty-copy">{errorCopy}</span>
          </div>
        )}
      </div>
    </article>
  );
}

function sourceKind(source?: string): "metrics" | "logs" {
  if (source === "loki" || source === "opensearch") return "logs";
  return "metrics";
}

function friendlySource(source?: string) {
  if (source === "prometheus") return "Metrics";
  if (source === "loki" || source === "opensearch") return "Logs";
  return source || "Local";
}

function friendlyPanelTitle(panel: GlancePanelResult) {
  const map: Record<string, string> = {
    demo_errors: "Errors",
    demo_latency: "Latency",
    cpu_busy: "CPU busy",
    error_logs: "Error log rate",
  };
  return map[panel.panel_id] || panel.title;
}

function friendlyPanelError(error?: string) {
  if (!error) return "Start services, then refresh";
  const lower = error.toLowerCase();
  if (lower.includes("fetch failed") || lower.includes("network")) {
    return "Can't reach data source";
  }
  if (lower.includes("timeout")) return "Request timed out";
  if (lower.includes("econnrefused") || lower.includes("connect")) {
    return "Service not running";
  }
  return error.length > 48 ? `${error.slice(0, 48)}…` : error;
}

function friendlyIntent(intent: string) {
  if (intent === "glance") return "Overview";
  if (intent === "metrics") return "Metrics";
  if (intent === "logs") return "Logs";
  if (intent === "dashboard") return "Dashboards";
  if (intent === "error") return "Error";
  return intent;
}

function friendlyAgent(agent: string) {
  return agent
    .replace("Orchestrator", "Coordinator")
    .replace("MetricsAgent", "Metrics")
    .replace("LogsAgent", "Logs")
    .replace("SummaryAgent", "Summary");
}

function friendlyAction(action: string) {
  return action
    .replace("classify_intent", "classified intent")
    .replace("select_tools", "selected tools")
    .replace("query_prometheus", "queried metrics")
    .replace("query_loki", "queried logs")
    .replace("summarize", "summarized")
    .replace("build_glance", "built overview");
}

function MessageBubble({
  message,
  onSelectCandidate,
}: {
  message: ChatMessage;
  onSelectCandidate?: (name: string) => void;
}) {
  const response = message.meta;
  const chartSeries = response?.series?.length
    ? response.series
    : response?.charts?.[0]?.series;
  const isError = response?.intent === "error";

  return (
    <div
      className={
        message.role === "user" ? "bubble user" : "bubble assistant"
      }
    >
      <div className="bubble-role">
        {message.role === "user" ? "You" : "Assistant"}
      </div>
      <div
        className={isError ? "bubble-body error-copy" : "bubble-body"}
        dangerouslySetInnerHTML={{
          __html: formatMarkdownLite(message.content),
        }}
      />
      {response && (
        <div className="adk-meta">
          <div className="meta-row">
            <span className={isError ? "badge badge-error" : "badge"}>
              {friendlyIntent(response.intent)}
            </span>
            {response.agents?.length > 0 && (
              <span className="muted small meta-agents">
                {response.agents.map(friendlyAgent).join(" → ")}
              </span>
            )}
          </div>

          {response.candidates && response.candidates.length > 0 && (
            <div
              className="candidate-options"
              style={{
                marginTop: "10px",
                display: "flex",
                gap: "8px",
                flexWrap: "wrap",
              }}
            >
              {response.candidates.map((c) => (
                <button
                  key={c.name}
                  type="button"
                  className="btn quiet"
                  style={{
                    fontSize: "12px",
                    padding: "4px 10px",
                    cursor: "pointer",
                  }}
                  onClick={() => onSelectCandidate?.(c.name)}
                >
                  👉 {c.name} {c.purpose ? `— ${c.purpose}` : ""}
                </button>
              ))}
            </div>
          )}

          {response.steps?.length > 0 && (
            <details className="trace">
              <summary>Steps</summary>
              <ol>
                {response.steps.map((step) => (
                  <li key={step.step}>
                    <strong>{friendlyAgent(step.agent)}</strong>
                    {" · "}
                    {friendlyAction(step.action)}
                    <code className="inline-result">
                      {formatStepResult(step.result)}
                    </code>
                  </li>
                ))}
              </ol>
            </details>
          )}

          {response.queryUsed && (
            <details className="technical-details">
              <summary>Expression</summary>
              <div className="expr-box tight">
                <code>{response.queryUsed}</code>
              </div>
            </details>
          )}

          {response.dashboardLink && (
            <a
              className="dash-link"
              href={response.dashboardLink}
              target="_blank"
              rel="noreferrer"
            >
              Open dashboard ↗
            </a>
          )}

          {chartSeries && chartSeries.length > 0 && (
            <div className="meta-chart">
              <TimeSeriesChart
                series={chartSeries}
                chartType={response.chartType}
                height={220}
              />
            </div>
          )}

          {response.proposal && (
            <ProposalCard proposal={response.proposal} />
          )}

          {response.alertRule && (
            <AlertProposalCard
              alertRule={response.alertRule}
              sessionId={response.sessionId}
            />
          )}
        </div>
      )}
    </div>
  );
}

function AlertProposalCard({
  alertRule,
  sessionId,
}: {
  alertRule: AlertRuleProposal;
  sessionId?: string | null;
}) {
  const [status, setStatus] = useState<"idle" | "creating" | "created" | "discarded" | "error">("idle");
  const [deeplink, setDeeplink] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  if (status === "created") {
    return (
      <div className="alert-card-created">
        <div className="alert-card-created-title">✓ Alert Rule Created in Grafana!</div>
        {deeplink && (
          <a
            href={deeplink}
            target="_blank"
            rel="noreferrer"
            className="alert-card-link"
          >
            View Alert in Grafana ↗
          </a>
        )}
      </div>
    );
  }

  if (status === "discarded") {
    return (
      <div style={{ marginTop: "10px", padding: "8px", background: "var(--bg-soft)", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", fontSize: "12px", color: "var(--muted)" }}>
        Alert proposal discarded.
      </div>
    );
  }

  return (
    <div className="alert-proposal-card">
      <div className="alert-proposal-header">🔔 Proposed Grafana Alert Rule</div>
      <div className="alert-proposal-title">{alertRule.title}</div>
      <div className="alert-proposal-code">
        {alertRule.condition_query} {alertRule.comparison ? `> ${alertRule.comparison.threshold}` : ""} (for {alertRule.for_duration})
      </div>
      {errorMsg && <div style={{ fontSize: "12px", color: "var(--danger)", marginTop: "4px" }}>{errorMsg}</div>}
      <div style={{ marginTop: "8px", display: "flex", gap: "8px" }}>
        <button
          type="button"
          disabled={status === "creating" || !sessionId}
          onClick={async () => {
            setStatus("creating");
            try {
              const res = await confirmAlert(sessionId!, true);
              if (res.status === "created") {
                setDeeplink(res.deeplink || null);
                setStatus("created");
              } else {
                setStatus("error");
                setErrorMsg(res.status);
              }
            } catch (err: any) {
              setStatus("error");
              setErrorMsg(err.message || String(err));
            }
          }}
          className="btn primary"
          style={{ fontSize: "12px", padding: "5px 14px", cursor: "pointer" }}
        >
          {status === "creating" ? "Creating..." : "✓ Create in Grafana"}
        </button>
        <button
          type="button"
          disabled={status === "creating" || !sessionId}
          onClick={async () => {
            try {
              await confirmAlert(sessionId!, false);
            } catch {
              // ignore
            }
            setStatus("discarded");
          }}
          className="btn quiet"
          style={{ fontSize: "12px", padding: "5px 12px", cursor: "pointer" }}
        >
          ✕ Discard
        </button>
      </div>
    </div>
  );
}

function normalizeResponse(response: AdkChatResponse): AdkChatResponse {
  const firstChart = response.charts?.[0];
  return {
    ...response,
    intent: response.intent || response.flow?.intent || "metrics",
    agents: response.agents?.length
      ? response.agents
      : response.flow?.agents || ["Orchestrator"],
    steps: response.steps || [],
    answer: polishAnswer(response.answer || "Done."),
    queryUsed: response.queryUsed ?? firstChart?.expr ?? null,
    series: response.series ?? firstChart?.series ?? null,
    dashboardLink:
      response.dashboardLink ?? response.dashboards?.[0]?.url ?? null,
    usedLlm: response.usedLlm ?? false,
  };
}

function polishAnswer(answer: string): string {
  return answer
    .replace(
      /\*\*Highest error service\*\* completed via ADK tools\./,
      "**Error overview** is ready."
    )
    .replace(
      /\*\*CPU quick glance\*\* completed via ADK tools\./,
      "**CPU overview** is ready."
    )
    .replace(
      /\*\*Latency trend\*\* completed via ADK tools\./,
      "**Latency overview** is ready."
    )
    .replace(
      /\*\*Recent error logs\*\* completed via ADK tools\./,
      "**Recent error activity** is ready."
    )
    .replace(
      /\*\*Find dataset dashboard\*\* completed via ADK tools\./,
      "**Dataset dashboard** is ready."
    )
    .replace(
      /\*\*Full quick-glance board\*\* completed via ADK tools\./,
      "**System overview** is ready."
    )
    .replace(/\*\*Demo requests\*\*/g, "**Request activity**");
}

function formatLatest(value: number | null | undefined, unit?: string) {
  if (value == null || Number.isNaN(value)) return "—";
  if (unit === "percent") return `${value.toFixed(1)}%`;
  if (unit === "ms") return `${value.toFixed(1)} ms`;
  if (unit === "bytes") {
    return value > 1e9
      ? `${(value / 1e9).toFixed(1)} GB`
      : `${(value / 1e6).toFixed(1)} MB`;
  }
  return value.toFixed(value >= 100 ? 0 : 2);
}

function formatStepResult(result: unknown): string {
  if (Array.isArray(result)) return result.join(", ");
  if (typeof result === "string") return result;
  return JSON.stringify(result);
}

function formatMarkdownLite(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(
      /\[([^\]]+)\]\(((?:https?:\/\/)[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1 ↗</a>'
    )
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br/>");
}
