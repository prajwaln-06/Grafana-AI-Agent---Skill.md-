import type {
  AdkChatResponse,
  AdkFlow,
  Catalog,
  GlancePanelResult,
  QueryResult,
  SearchResponse,
} from "./types";

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      ...init,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("The request timed out. Check that the local observability services are running, then retry.");
    }
    throw new Error(
      "The UI API is unreachable. Start the app with `cd ui && npm run dev`, then reload the page."
    );
  }

  const contentType = res.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await res.json().catch(() => ({}))
    : {};
  if (!res.ok) {
    const errorMsg = (data as any)?.detail || (data as any)?.error || `Request failed (${res.status})`;
    throw new Error(typeof errorMsg === "string" ? errorMsg : JSON.stringify(errorMsg));
  }
  return data as T;
}

export function fetchCatalog() {
  return json<Catalog>("/api/catalog");
}

export function runQuery(body: {
  sourceId: string;
  metricId: string;
  queryId: string;
  range: string;
  vars?: Record<string, string>;
}) {
  return json<QueryResult>("/api/query", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function fetchLabels(params: {
  source?: string;
  metric?: string;
  labelName: string;
}): Promise<{ values: string[] }> {
  const qs = new URLSearchParams();
  if (params.source) qs.set("source", params.source);
  if (params.metric) qs.set("metric", params.metric);
  qs.set("labelName", params.labelName);
  return json<{ values: string[] }>(`/api/labels?${qs.toString()}`);
}

export async function adkChat(
  message: string,
  sessionId?: string | null
): Promise<AdkChatResponse> {
  const isDashboardQuery = /\b(dashboard|dashboards|propose|panel|panels|create|remove|delete|list)\b/i.test(message);

  if (isDashboardQuery) {
    try {
      const adkRaw = await json<{
        kind?: string;
        text?: string;
        agent_response?: string;
        proposalId?: string;
        proposal?: any;
        raw_json?: any[];
        timings?: any;
        errors?: any[];
      }>("/api/adk/chat", {
        method: "POST",
        body: JSON.stringify({
          message,
          sessionId: sessionId || null,
        }),
      });

      if (adkRaw.kind === "proposal" || adkRaw.proposal || adkRaw.proposalId) {
        return {
          status: "ok",
          framework: "Google ADK + FastMCP",
          intent: "dashboard_proposal",
          agents: ["ADK Agent", "Proposal Engine", "MCP-Grafana"],
          steps: [
            { step: 1, agent: "ADK Agent", action: "classified intent", result: "CREATE/UPDATE" },
            { step: 2, agent: "Proposal Engine", action: "generated Dashboard IR", result: adkRaw.proposal?.ir?.name || "Proposal" },
          ],
          answer: adkRaw.text || adkRaw.agent_response || "Here is the proposed dashboard.",
          queryUsed: null,
          series: null,
          dashboardLink: null,
          usedLlm: true,
          sessionId: sessionId || null,
          proposalId: adkRaw.proposalId,
          proposal: adkRaw.proposal,
        };
      }

      if (adkRaw.text || adkRaw.agent_response) {
        return {
          status: "ok",
          framework: "Google ADK + FastMCP",
          intent: "dashboard",
          agents: ["ADK Agent", "MCP-Grafana"],
          steps: [
            { step: 1, agent: "ADK Agent", action: "queried Grafana MCP", result: "completed" },
          ],
          answer: adkRaw.text || adkRaw.agent_response || "Done.",
          queryUsed: null,
          series: null,
          dashboardLink: null,
          usedLlm: true,
          sessionId: sessionId || null,
        };
      }
    } catch {
      // Fall through to query pipeline if adk chat errors
    }
  }

    type QueryEntry = {
      mode?: string;
      status?: string;
      explanation?: string;
      clarification?: string;
      candidates?: Array<{ name: string; purpose?: string }>;
      caveat?: string;
      data_source?: string;
      query?: string | Record<string, unknown>;
      reference_used?: string;
      execution?: {
        execution_status?: string;
        chart_type?: string;
        result_type?: string;
        series?: Array<{
          labels?: Record<string, string>;
          legend_label?: string;
          points?: Array<{ timestamp: string | number; value: number | null }>;
        }>;
        buckets?: Array<{ key: string; doc_count: number }>;
        hits?: Array<{ timestamp: string; severity: string; body: string }>;
        error?: string;
      };
    };

  try {
    const raw = await json<{
      result: QueryEntry & {
        results?: QueryEntry[];
        synthesis?: string | null;
      };
      session_id?: string | null;
    }>("/api/v1/query", {
      method: "POST",
      body: JSON.stringify({
        question: message,
        session_id: sessionId || null,
      }),
    });

    const res = raw.result;
    const mode = res.mode || "single";
    const entries: QueryEntry[] = mode === "multi" ? res.results || [] : [res];

    let answer = "";
    if (mode === "multi" && res.synthesis) {
      answer = res.synthesis;
    } else {
      const explanations = entries
        .map((e) => e.explanation || e.clarification || "")
        .filter(Boolean);
      answer = explanations.join("\n\n") || "Query executed.";
    }

    // If query was unsupported by metrics pipeline, try ADK agent
    if (res.status === "unsupported_metric" || answer.includes("outside the scope")) {
      try {
        const adkFallback = await json<any>("/api/adk/chat", {
          method: "POST",
          body: JSON.stringify({ message, sessionId: sessionId || null }),
        });
        if (adkFallback.text || adkFallback.agent_response) {
          return {
            status: "ok",
            framework: "Google ADK + FastMCP",
            intent: "general",
            agents: ["ADK Agent", "MCP-Grafana"],
            steps: [{ step: 1, agent: "ADK Agent", action: "queried tools", result: "success" }],
            answer: adkFallback.text || adkFallback.agent_response,
            queryUsed: null,
            series: null,
            dashboardLink: null,
            usedLlm: true,
            sessionId: sessionId || null,
            proposalId: adkFallback.proposalId,
            proposal: adkFallback.proposal,
          };
        }
      } catch {
        // use original answer
      }
    }

    const allSeries: Array<{
      name: string;
      labels: Record<string, string>;
      points: Array<{ t: number; v: number | null }>;
    }> = [];

    const queriesUsed: string[] = [];

    for (const entry of entries) {
      if (entry.query) {
        queriesUsed.push(
          typeof entry.query === "string"
            ? entry.query
            : JSON.stringify(entry.query)
        );
      }

      if (entry.execution?.series) {
        for (const s of entry.execution.series) {
          const name =
            s.legend_label ||
            (s.labels
              ? Object.entries(s.labels)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(", ")
              : "metric");
          const points = (s.points || []).map((p) => {
            const rawTs = p.timestamp;
            const ts = typeof rawTs === "string" ? new Date(rawTs).getTime() : Number(rawTs);
            return {
              t: ts > 1e12 ? Math.floor(ts / 1000) : ts,
              v: p.value,
            };
          });
          allSeries.push({ name, labels: s.labels || {}, points });
        }
      }
    }

    const steps = [
      { step: 1, agent: "Router", action: "matched domain skill", result: entries[0]?.reference_used || "skills" },
      { step: 2, agent: "Generator", action: "constructed query", result: queriesUsed[0] || "PromQL" },
      { step: 3, agent: "Validator", action: "mechanically audited rules", result: "passed" },
      { step: 4, agent: "Executor", action: "executed query", result: entries[0]?.execution?.execution_status || "success" },
    ];

    const firstEntry = entries[0];
    const caveat = firstEntry?.caveat ? `\n\n*Note: ${firstEntry.caveat}*` : "";

    return {
      status: firstEntry?.status || "ok",
      framework: "FastAPI + SKILL.md v3",
      intent: (firstEntry?.data_source as string) || "metrics",
      agents: ["Router", "Generator", "Validator", "Executor"],
      steps,
      answer: answer + caveat,
      queryUsed: queriesUsed.join("\n") || null,
      series: allSeries.length > 0 ? allSeries : null,
      dashboardLink: null,
      usedLlm: true,
      chartType: (firstEntry?.execution as Record<string, unknown> | undefined)?.chart_type as string || "line",
      sessionId: raw.session_id || null,
      candidates: firstEntry?.candidates || undefined,
      alertRule: (firstEntry as any)?.alert_rule || (raw.result as any)?.alert_rule || null,
      backendResult: raw,
    };
  } catch (err) {
    return json<AdkChatResponse>("/api/adk/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    }).catch(() => {
      throw err;
    });
  }
}

export function confirmAlert(
  sessionId: string,
  confirm: boolean = true
): Promise<{ status: string; rule_uid?: string; deeplink?: string }> {
  return json<{ status: string; rule_uid?: string; deeplink?: string }>(
    "/api/v1/alerts/confirm",
    {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, confirm }),
    }
  );
}

export function fetchAdkExamples() {
  return json<{ examples: string[] }>("/api/adk/examples");
}

export function fetchAdkFlows() {
  return json<{ flows: AdkFlow[] }>("/api/adk/flows");
}

export function runAdkFlow(flowId: string) {
  return json<AdkChatResponse & { charts?: GlancePanelResult[] }>(
    "/api/adk/flows/run",
    {
      method: "POST",
      body: JSON.stringify({ flow_id: flowId }),
    }
  );
}

export function fetchAdkBoard(panelIds?: string[], range = "1h") {
  return json<{ panels: GlancePanelResult[]; range: string }>(
    "/api/adk/glance/board",
    {
      method: "POST",
      body: JSON.stringify({ panel_ids: panelIds, range_label: range }),
    }
  );
}

export async function fetchHealth() {
  try {
    const data = await json<{ ok: boolean; services: Record<string, string> }>("/api/health");
    return data;
  } catch {
    try {
      const data = await json<{ status: string; skill_name?: string }>("/readyz");
      return {
        ok: data.status === "ready",
        services: {
          backend: data.status,
          skill: data.skill_name || "observability-query-builder",
        },
      };
    } catch {
      try {
        const data = await json<{ status: string }>("/healthz");
        return { ok: data.status === "ok", services: { backend: data.status } };
      } catch {
        return { ok: false, services: { backend: "offline" } };
      }
    }
  }
}

export function searchObservability(q: string) {
  const qs = new URLSearchParams({ q });
  return json<SearchResponse>(`/api/search?${qs.toString()}`);
}

export function fetchGlancePanel(panelId: string, range = "1h") {
  return json<{ panels: GlancePanelResult[]; range: string }>(
    "/api/adk/glance/board",
    {
      method: "POST",
      body: JSON.stringify({ panel_ids: [panelId], range_label: range }),
    }
  );
}

export function approveProposal(proposalId: string, version?: number) {
  return json<{ status: string; approvalToken?: string; version: number }>(
    `/api/proposals/${proposalId}/approve`,
    {
      method: "POST",
      body: JSON.stringify({ version }),
    }
  );
}

export function rejectProposal(proposalId: string, version?: number) {
  return json<{ status: string; version: number }>(
    `/api/proposals/${proposalId}/reject`,
    {
      method: "POST",
      body: JSON.stringify({ version }),
    }
  );
}

export function executeProposal(proposalId: string, version: number, approvalToken: string) {
  return json<{ status: string; result?: any; error?: string }>(
    `/api/proposals/${proposalId}/execute`,
    {
      method: "POST",
      body: JSON.stringify({ version, approvalToken }),
    }
  );
}

export function previewProposal(proposalId: string, ir: any, panelIds?: string[]) {
  return json<{ status: string; ir: any }>(
    `/api/proposals/${proposalId}/preview`,
    {
      method: "POST",
      body: JSON.stringify({ ir, panelIds }),
    }
  );
}

export function modifyProposal(proposalId: string, ir: any) {
  return json<any>(
    `/api/proposals/${proposalId}`,
    {
      method: "PUT",
      body: JSON.stringify({ ir }),
    }
  );
}

export function fetchAlerts() {
  return json<{ alerts: string }>("/api/alerts");
}

export function fetchAlertProposals() {
  return json<{ proposals: any[] }>("/api/alert-proposals");
}

export function approveAlertProposal(proposalId: string, version: number) {
  return json<any>(`/api/alert-proposals/${proposalId}/approve`, {
    method: "POST",
    body: JSON.stringify({ version }),
  });
}

export function rejectAlertProposal(proposalId: string, version: number) {
  return json<any>(`/api/alert-proposals/${proposalId}/reject`, {
    method: "POST",
    body: JSON.stringify({ version }),
  });
}

export function executeAlertProposal(proposalId: string, version: number, approvalToken: string) {
  return json<any>(`/api/alert-proposals/${proposalId}/execute`, {
    method: "POST",
    body: JSON.stringify({ version, approvalToken }),
  });
}


