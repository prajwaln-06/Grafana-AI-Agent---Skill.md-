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
  const normalized = message.trim();
  const raw = await json<{
    status: string;
    sessionId: string;
    intent: string;
    framework: string;
    agents?: string[];
    steps?: Array<{ step: number; agent: string; action: string; result: string }>;
    answer: string;
    queryUsed?: string | null;
    chartType?: string | null;
    series?: Array<{
      name: string;
      labels: Record<string, string>;
      points: Array<{ t: number; v: number | null }>;
    }> | null;
    dashboardLink?: string | null;
    proposalId?: string | null;
    proposal?: any | null;
    alertRule?: any | null;
    candidates?: Array<{ name: string; purpose?: string }>;
  }>("/api/chat", {
    method: "POST",
    body: JSON.stringify({
      message: normalized,
      sessionId: sessionId || null,
    }),
  });

  return {
    status: raw.status || "ok",
    framework: raw.framework || "Google ADK + FastMCP + SKILL.md v3",
    intent: raw.intent || "general",
    agents: raw.agents || [],
    steps: raw.steps || [],
    answer: raw.answer || "",
    queryUsed: raw.queryUsed || null,
    chartType: raw.chartType || "line",
    series: raw.series || null,
    dashboardLink: raw.dashboardLink || null,
    usedLlm: true,
    sessionId: raw.sessionId || sessionId || null,
    proposalId: raw.proposalId || null,
    proposal: raw.proposal || null,
    alertRule: raw.alertRule || null,
    candidates: raw.candidates || undefined,
  };
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


