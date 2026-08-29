export type VarDef = {
  id: string;
  label: string;
  /** How the variable is populated and substituted into the expression. */
  type:
    | "prometheus_label"
    | "loki_label"
    | "static"
    | "metric_name"
    | "text"
    | "checkbox";
  metric?: string;
  labelName?: string;
  /** Fallback / fixed choices for static and metric_name vars. */
  options?: string[];
  allowAll?: boolean;
  defaultValue?: string;
  /** When set, re-fetch this var after the dependency changes (e.g. INSTANCE after METRIC). */
  dependsOn?: string;
  hint?: string;
  /** Hide from the Variables UI (e.g. metric already chosen in View). */
  hidden?: boolean;
  placeholder?: string;
};

export type QueryDef = {
  id: string;
  label: string;
  description?: string;
  expr: string;
  resolvedExpr?: string;
  unit: string;
  legend: string;
  engine?: string;
  vars?: VarDef[];
};

export type MetricDef = {
  id: string;
  label: string;
  description: string;
  queries: QueryDef[];
};

export type SourceDef = {
  id: string;
  label: string;
  description: string;
  metrics: MetricDef[];
};

export type Catalog = Record<string, SourceDef>;

export type SeriesPoint = { t: number; v: number | null };

export type Series = {
  name: string;
  labels: Record<string, string>;
  points: SeriesPoint[];
};

export type QueryResult = {
  source: { id: string; label: string };
  metric: { id: string; label: string };
  query: QueryDef;
  backend: string;
  range: { start: number; end: number; step: number; label: string };
  series: Series[];
  usedLlm: boolean;
  plotImage?: string | null;
  chartType?: string | null;
  error?: string;
};

export type PanelIR = {
  id: string;
  title: string;
  metric?: string;
  query?: string;
  visualizationType?: string;
  unit?: string;
  datasource?: string;
  queryResult?: {
    kind?: string;
    points?: Array<{ t: number; v: number }>;
    columns?: string[];
    rows?: any[][];
  };
};

export type DashboardIR = {
  name: string;
  description?: string;
  datasource?: string;
  timeConfig?: {
    range?: string;
    refresh?: string;
  };
  panels: PanelIR[];
};

export type DashboardProposal = {
  proposalId: string;
  status: "proposed" | "approved" | "rejected" | "executed" | string;
  version: number;
  approvalToken?: string;
  ir: DashboardIR;
  errors?: any[];
};

export type AdkStep = {
  step: number;
  agent: string;
  action: string;
  result: unknown;
};

export type AlertRuleProposal = {
  title: string;
  condition_query: string;
  comparison?: {
    operator: string;
    threshold: number;
  };
  for_duration: string;
  folder?: string;
  datasource_uid?: string;
};

export type AdkChatResponse = {
  status?: string;
  framework: string;
  intent: string;
  agents: string[];
  steps: AdkStep[];
  answer: string;
  queryUsed: string | null;
  series: Series[] | null;
  dashboardLink: string | null;
  usedLlm: boolean;
  sessionId?: string | null;
  candidates?: Array<{ name: string; purpose?: string }>;
  chartType?: string;
  proposalId?: string;
  proposal?: DashboardProposal;
  alertRule?: AlertRuleProposal | null;
  kind?: "proposal" | "text" | string;
  agent_response?: string;
  raw_json?: any[];
  note?: string;
  error?: string;
  flow?: {
    id: string;
    title: string;
    description?: string;
    prompt?: string;
    agents?: string[];
    intent?: string;
  };
  charts?: AdkChart[];
  dashboards?: Array<{ title: string; url: string }>;
  backendResult?: unknown;
};

export type AdkChart = {
  title: string;
  unit?: string;
  expr?: string;
  panel_id?: string;
  latest_value?: number | null;
  status?: string;
  error?: string;
  chart_type?: string;
  image?: string | null;
  series: Series[];
};

export type AdkFlow = {
  id: string;
  title: string;
  description: string;
  intent: string;
  prompt: string;
  agents: string[];
};

export type GlancePanelResult = AdkChart & {
  panel_id: string;
  description?: string;
  source?: string;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  meta?: AdkChatResponse;
};

export type SearchHit = {
  type: "dashboard" | "panel" | "metric";
  id: string;
  title: string;
  description?: string;
  url?: string;
  sourceId?: string;
  metricId?: string;
  queryId?: string;
  unit?: string;
  score: number;
};

export type SearchShowcase =
  | {
      kind: "dashboard";
      item: SearchHit;
      panel?: GlancePanelResult;
    }
  | {
      kind: "panel";
      item: SearchHit;
      panel?: GlancePanelResult;
    }
  | {
      kind: "metric";
      item: SearchHit;
      result?: QueryResult;
      error?: string;
    };

export type SearchResponse = {
  query: string;
  dashboards: SearchHit[];
  panels: Array<SearchHit & { panel?: GlancePanelResult }>;
  metrics: SearchHit[];
  best: SearchHit | null;
  showcase?: SearchShowcase | null;
  error?: string;
};
