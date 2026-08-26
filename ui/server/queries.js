/**
 * Pre-defined query catalog for the Observability Lab UI.
 *
 * Queries are identified by (sourceId, metricId, queryId) and carry an
 * optional `vars` list. Each var describes a label that the user can filter
 * by (e.g. "which host?", "which container?") and is fetched dynamically from
 * Prometheus or Loki at runtime so the dropdown always reflects reality.
 *
 * Expression placeholder conventions:
 *   $VAR       → Prometheus label (type prometheus_label): appended as
 *                `,label="value"` inside existing {}. "All" → empty string.
 *   $VAR_F     → Prometheus: first/only label inside `{}`. "All" → "".
 *   $VAR       → Loki (type loki_label): regex value. "All" → `.*`.
 *   $VAR       → static / metric_name: raw string substitution of the value.
 *
 * No LLM is involved — all expressions are human-authored and read-only.
 */

/**
 * Exact CM telemetry metric names from the screenshots (without leading dot).
 * Shown in the View dropdown as `.name` to match the lab CLI list.
 */
export const CM_TELEMETRY_METRICS = [
  "opensearch_xfs_quota_soft_limit_bytes",
  "process_cpu_seconds_total",
  "process_exporter_build_info",
  "process_max_fds",
  "process_network_receive_bytes_total",
  "process_network_transmit_bytes_total",
  "process_open_fds",
  "process_resident_memory_bytes",
  "process_start_time_seconds",
  "process_virtual_memory_bytes",
  "process_virtual_memory_max_bytes",
  "promhttp_metric_handler_errors_total",
  "promhttp_metric_handler_requests_in_flight",
  "promhttp_metric_handler_requests_total",
  "scrape_duration_seconds",
  "scrape_response_size_bytes",
  "scrape_samples_post_metric_relabeling",
  "scrape_samples_scraped",
  "scrape_series_added",
  "scrape_timeout_seconds",
  "ta_cluster_cache_used_total",
  "ta_cluster_refresh_lock_seconds_bucket",
  "ta_cluster_refresh_lock_seconds_count",
  "ta_cluster_refresh_lock_seconds_sum",
  "ta_cmutil_request_seconds_bucket",
  "ta_cmutil_request_seconds_count",
  "ta_cmutil_request_seconds_sum",
  "ta_direct_proto_detect_seconds_bucket",
  "ta_direct_proto_detect_seconds_count",
  "ta_direct_proto_detect_seconds_sum",
  "ta_monitordb_restart_seconds_bucket",
  "ta_monitordb_restart_seconds_count",
  "ta_monitordb_restart_seconds_sum",
  "ta_remote_proto_detect_seconds_bucket",
  "ta_remote_proto_detect_seconds_count",
  "ta_remote_proto_detect_seconds_sum",
  "temperature",
  "up",
  "vm_fs_available_bytes",
  "vm_fs_total_size_bytes",
  "vm_fs_used_bytes",
  "vm_retention_days",
  "vm_used_bytes",
  "vm_xfs_quota_hard_limit_bytes",
  "vm_xfs_quota_soft_limit_bytes",
  // "Normal metrics" section from screenshots
  "entAliasMappingIdentifier",
  "entLastChangeTime",
  "entPhysicalAlias",
  "entPhysicalAssetID",
  "entPhysicalChildIndex",
];

/** Metrics for pattern Metric pickers (CM list + common local lab metrics). */
export const CURATED_METRICS = [
  ...CM_TELEMETRY_METRICS,
  "node_cpu_seconds_total",
  "node_memory_MemTotal_bytes",
  "node_memory_MemAvailable_bytes",
  "node_filesystem_avail_bytes",
  "node_disk_read_bytes_total",
  "node_disk_written_bytes_total",
  "container_cpu_usage_seconds_total",
  "container_memory_usage_bytes",
  "demo_requests_total",
  "demo_errors_total",
  "demo_latency_ms",
  "demo_cpu_percent",
];

const INTERVAL_OPTIONS = [
  "15s",
  "30s",
  "1m",
  "5m",
  "15m",
  "30m",
  "1h",
  "6h",
  "12h",
  "1d",
];

const DURATION_OPTIONS = ["15m", "30m", "1h", "6h", "12h", "24h", "7d"];

const TS_START_OPTIONS = [
  "",
  "now-5m",
  "now-15m",
  "now-30m",
  "now-1h",
  "now-3h",
  "now-6h",
  "now-12h",
  "now-24h",
  "now-2d",
  "now-7d",
  "now-14d",
  "now-30d",
];

const TS_END_OPTIONS = [
  "",
  "now",
  "now-5m",
  "now-15m",
  "now-30m",
  "now-1h",
  "now-3h",
  "now-6h",
  "now-12h",
  "now-24h",
  "now-2d",
  "now-7d",
];

const CHART_LIMIT_OPTIONS = ["10", "25", "50", "100", "200"];

const FILTER_OPTIONS = [
  "",
  'status=~"5.."',
  'status=~"4.."',
  'status=~"2.."',
  'job="prometheus"',
  'job="node"',
  'job="cadvisor"',
  'job=~"node|prometheus"',
  'instance=~"admin.*"',
  'instance=~"leader.*"',
  'instance=~".*:9100"',
  'instance=~".*:9256"',
  'name!=""',
  'name=~".+"',
  'container!=""',
  'device!=""',
  'fstype!="tmpfs"',
  'mode="idle"',
  'mode!="idle"',
  'level="error"',
  'level="warn"',
  'level="info"',
  'service="checkout"',
  'service="payments"',
];

const AGGREGATION_OPTIONS = [
  "avg",
  "count",
  "median",
  "min",
  "max",
  "sum",
  "counter",
  "last",
  "first",
];

const NODE_ID_OPTIONS = ["hostname", "xname", "ip", "original", "ALIAS"];

const OUTPUT_OPTIONS = ["table", "csv", "json", "chart", "prom"];

/** Group-by choices from CM default + common lab / Prometheus labels. */
const GROUP_BY_OPTIONS = [
  "location_type,index,parentalindex",
  "location_type",
  "index",
  "parentalindex",
  "location_type,index",
  "hostname",
  "instance",
  "instance,job",
  "instance,name",
  "job",
  "name",
  "device",
  "mountpoint",
  "service",
];

/** Hosts seen in the lab CM telemetry chart screenshots (plus local defaults). */
export const CURATED_HOSTS = [
  // bare hostnames (CM -n regex style)
  "admin",
  "leader1",
  "leader2",
  "leader3",
  // host:port from chart legend screenshots
  "admin:9100",
  "admin:9256",
  "leader1:9100",
  "leader1:9256",
  "leader2:9100",
  "leader2:9256",
  "leader3:9100",
  "leader3:9256",
  // local lab
  "node-exporter:9100",
  "cadvisor:8080",
  "demo-metrics:8000",
  "prometheus:9090",
];

function unitForMetric(name) {
  if (/_bytes$|_bytes_total$/.test(name)) return "bytes";
  if (/percent|ratio/i.test(name)) return "percent";
  if (/_seconds|_seconds_total|_seconds_sum|_seconds_count|_seconds_bucket/.test(name))
    return "short";
  return "short";
}

function metricVar(extra = {}) {
  return {
    id: "METRIC",
    label: "Metric",
    type: "metric_name",
    allowAll: false,
    options: CURATED_METRICS,
    ...extra,
  };
}

function instanceVar(metricHint) {
  return {
    id: "INSTANCE",
    label: "Node (-n)",
    type: "prometheus_label",
    metric: metricHint || undefined,
    labelName: "instance",
    allowAll: true,
    dependsOn: "METRIC",
    options: CURATED_HOSTS,
    hint: "Subset of nodes (hostnames), like CM `--node`. All = every instance.",
  };
}

function nodeIdVar() {
  return {
    id: "NODE_ID",
    label: "Node identifier (-i)",
    type: "static",
    allowAll: false,
    options: NODE_ID_OPTIONS,
    defaultValue: "hostname",
    hint: "How to present datapoints: hostname, xname, ip, original, or ALIAS.",
  };
}

function aggregationVar() {
  return {
    id: "AGGREGATION",
    label: "Aggregation (-a)",
    type: "static",
    allowAll: false,
    options: AGGREGATION_OPTIONS,
    defaultValue: "last",
  };
}

function outputVar() {
  return {
    id: "OUTPUT",
    label: "Output (-o)",
    type: "static",
    allowAll: false,
    options: OUTPUT_OPTIONS,
    defaultValue: "chart",
    hint: "CM CLI: table | csv | json | chart | prom. UI always charts when you Run.",
  };
}

/** Full option set from `cm telemetry query -h` screenshots. */
function cmCliVars(metricName, { hideMetric = true, metricDefault } = {}) {
  const metric = metricDefault || metricName;
  return [
    {
      id: "METRIC",
      label: "Metric",
      type: hideMetric ? "static" : "metric_name",
      allowAll: false,
      options: hideMetric ? [metric] : CURATED_METRICS,
      defaultValue: metric,
      hidden: hideMetric,
    },
    aggregationVar(),
    {
      ...instanceVar(metricName),
      dependsOn: hideMetric ? undefined : "METRIC",
      label: "Node (-n)",
      hint: "Subset of nodes (hostname / host:port), like CM --node LOCATION.",
    },
    {
      id: "GROUP_BY",
      label: "Group by (-g)",
      type: "static",
      allowAll: false,
      options: GROUP_BY_OPTIONS,
      defaultValue: "location_type,index,parentalindex",
      hint: "Labels that control uniqueness of sample windows. Same group → aggregated together.",
    },
    outputVar(),
    {
      id: "CHART_LIMIT",
      label: "Chart entries (-cl)",
      type: "static",
      allowAll: false,
      options: CHART_LIMIT_OPTIONS,
      defaultValue: "25",
      hint: "Max entries when output is chart (CM default 25).",
    },
    nodeIdVar(),
    {
      ...intervalVar(),
      label: "Step (-s)",
      hint: "Window size: (s)ecs, (m)ins, (h)ours, (d)ays. Default 1m in CM.",
      defaultValue: "1m",
    },
    {
      id: "DURATION",
      label: "Duration (-d)",
      type: "static",
      allowAll: false,
      options: DURATION_OPTIONS,
      defaultValue: "1h",
      hint: "Lookback duration ending now, when start/end are not set.",
    },
    {
      id: "TS_START",
      label: "Start time (-t)",
      type: "static",
      allowAll: false,
      options: TS_START_OPTIONS,
      defaultValue: "",
      hint: "Optional start timestamp / relative time (CM --ts-start).",
    },
    {
      id: "TS_END",
      label: "End time (-e)",
      type: "static",
      allowAll: false,
      options: TS_END_OPTIONS,
      defaultValue: "",
      hint: "Optional end timestamp (CM --ts-end). Empty = now.",
    },
    {
      id: "FILTER",
      label: "Filter (-f)",
      type: "static",
      allowAll: false,
      options: FILTER_OPTIONS,
      defaultValue: "",
      hint: "Filter expression presets (CM --filter).",
    },
    {
      id: "UTCTIME",
      label: "UTC time (-ut)",
      type: "checkbox",
      allowAll: false,
      defaultValue: "false",
      hint: "Show times in UTC.",
    },
    {
      id: "NO_COLOR",
      label: "No color (-nc)",
      type: "checkbox",
      allowAll: false,
      defaultValue: "false",
      hint: "Print table without color (CLI).",
    },
    {
      id: "NO_HEADER",
      label: "No header (-nh)",
      type: "checkbox",
      allowAll: false,
      defaultValue: "false",
      hint: "Do not display the table header (CLI).",
    },
    {
      id: "VERBOSE",
      label: "Verbose (-v)",
      type: "checkbox",
      allowAll: false,
      defaultValue: "false",
      hint: "Print debug information (CLI).",
    },
  ];
}

function intervalVar() {
  return {
    id: "INTERVAL",
    label: "Step / window",
    type: "static",
    allowAll: false,
    options: INTERVAL_OPTIONS,
    defaultValue: "5m",
  };
}

/** Curated containers from the lab UI screenshots + local stack names. */
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

/** Hosts / instances seen in lab charts and log lines (host:port). */
const LOG_HOST_OPTIONS = [
  "admin",
  "leader1",
  "leader2",
  "leader3",
  "admin:9100",
  "admin:9256",
  "leader1:9100",
  "leader1:9256",
  "leader2:9100",
  "leader2:9256",
  "leader3:9100",
  "leader3:9256",
];

const LOG_PORT_OPTIONS = ["9100", "9256", "9090", "3100", "3000", "9200"];

const LOG_LEVEL_OPTIONS = [
  "error",
  "warn",
  "warning",
  "info",
  "debug",
  "trace",
  "critical",
  "fatal",
];

/** Index / snippet names from the lab Snippets sidebar. */
const LOG_INDEX_OPTIONS = [
  "logs-*",
  "slingshot_switchstate",
  "switch_state",
  "switch_state-grep",
  "check_os_indices",
  "os-indices-*",
  "application-*",
  "cluster-logs-*",
  "node-exporter-*",
  "promtail-*",
];

/** Keyword presets inspired by lab snippets + common ops filters. */
const LOG_FILTER_OPTIONS = [
  "",
  "error",
  "warn",
  "warning",
  "timeout",
  "exception",
  "failed",
  "OOM",
  "panic",
  "switch",
  "switch_state",
  "switchstate",
  "slingshot",
  "slingshot_switchstate",
  "indices",
  "cluster",
  "leader",
  "admin",
  "9100",
  "9256",
  "curl",
  "GET",
];

const LOG_WINDOW_OPTIONS = [
  "1m",
  "2m",
  "5m",
  "10m",
  "15m",
  "30m",
  "1h",
  "3h",
  "6h",
  "12h",
  "24h",
];

const LOG_GROUP_BY_OPTIONS = [
  "container",
  "level",
  "host",
  "index",
  "service",
];

function logQueryVars({
  levelDefault = "",
  indexDefault = "logs-*",
  filterDefault = "error",
  includeKeyword = true,
} = {}) {
  const vars = [
    {
      id: "CONTAINER",
      label: "Container",
      type: "loki_label",
      labelName: "container",
      allowAll: true,
      options: CURATED_CONTAINERS,
      hint: "Container or service emitting logs (lab stack + admin/leader nodes).",
    },
    {
      id: "HOST",
      label: "Host / instance",
      type: "static",
      allowAll: true,
      options: LOG_HOST_OPTIONS,
      defaultValue: "",
      hint: "Cluster host from lab charts (admin, leaderN, or host:port).",
    },
    {
      id: "PORT",
      label: "Port",
      type: "static",
      allowAll: true,
      options: LOG_PORT_OPTIONS,
      defaultValue: "",
      hint: "Exporter / service port seen in telemetry lines (9100, 9256, …).",
    },
    {
      id: "LEVEL",
      label: "Log level",
      type: "static",
      allowAll: false,
      options: LOG_LEVEL_OPTIONS,
      defaultValue: levelDefault || "error",
      hint: "Severity filter: error, warn, info, debug, trace, critical, fatal.",
    },
    {
      id: "INDEX",
      label: "Index / snippet",
      type: "static",
      allowAll: false,
      options: LOG_INDEX_OPTIONS,
      defaultValue: indexDefault,
      hint:
        "OpenSearch index or lab snippet target (slingshot_switchstate, switch_state, check os indices).",
    },
    {
      id: "GROUP_BY_LOG",
      label: "Group by",
      type: "static",
      allowAll: false,
      options: LOG_GROUP_BY_OPTIONS,
      defaultValue: "container",
      hint: "How to break down log volume in grouped views.",
    },
    {
      id: "INTERVAL",
      label: "Window",
      type: "static",
      allowAll: false,
      options: LOG_WINDOW_OPTIONS,
      defaultValue: "5m",
      hint: "Lookback / rate window for count_over_time style queries.",
    },
  ];

  if (includeKeyword) {
    vars.push({
      id: "LOG_FILTER",
      label: "Keyword / grep",
      type: "static",
      allowAll: false,
      options: LOG_FILTER_OPTIONS,
      defaultValue: filterDefault,
      hint:
        "Text filter (snippet-style grep): switch, slingshot, indices, error, host names, ports.",
    });
  }

  return vars;
}

/** One View-dropdown entry per screenshot metric. */
function buildCmTelemetryQueries() {
  return CM_TELEMETRY_METRICS.map((name) => ({
    id: `cm_${name}`,
    label: `.${name}`,
    description: `From cm telemetry query list. Mirrors \`cm telemetry query .${name} -a <agg> -o chart\`.`,
    expr: "__CM_STYLE__",
    unit: unitForMetric(name),
    legend: "{{instance}}",
    vars: cmCliVars(name, { hideMetric: true }),
  }));
}

export const catalog = {
  prometheus: {
    id: "prometheus",
    label: "Prometheus",
    description: "Time-series metrics from Prometheus",
    metrics: [
      // ── Cluster metrics (CM telemetry list from screenshots) ──────────
      {
        id: "cm_telemetry",
        label: "Cluster metrics",
        description:
          "Common cluster/node metrics — pick one, then choose host and aggregation",
        queries: buildCmTelemetryQueries(),
      },

      // ── Query patterns (from dropdown-label screenshots) ────────────────
      {
        id: "query_patterns",
        label: "Query patterns",
        description:
          "Pick a ready-made PromQL shape, then choose the metric and host to apply it to",
        queries: [
          {
            id: "pattern_idle_pct",
            label: "Idle percentage (inverse)",
            description:
              "Treats the metric as idle time and shows busy %: 100 − avg(rate). Good for CPU-like counters.",
            expr: '100 - (avg by (instance) (rate({__name__="$METRIC"$INSTANCE}[$INTERVAL])) * 100)',
            unit: "percent",
            legend: "{{instance}}",
            vars: [metricVar(), instanceVar(), intervalVar()],
          },
          {
            id: "pattern_usage_ratio",
            label: "Usage ratio (percentage)",
            description:
              "used ÷ total as a percentage. Pick the ‘used’ metric and the matching ‘total’ metric.",
            expr: '100 * ({__name__="$USED_METRIC"$INSTANCE} / {__name__="$TOTAL_METRIC"$INSTANCE})',
            unit: "percent",
            legend: "{{instance}}",
            vars: [
              {
                id: "USED_METRIC",
                label: "Used metric",
                type: "metric_name",
                allowAll: false,
                options: CURATED_METRICS,
                defaultValue: "vm_fs_used_bytes",
              },
              {
                id: "TOTAL_METRIC",
                label: "Total metric",
                type: "metric_name",
                allowAll: false,
                options: CURATED_METRICS,
                defaultValue: "vm_fs_total_size_bytes",
              },
              instanceVar(),
            ],
          },
          {
            id: "pattern_error_pct",
            label: "Error percentage rate",
            description:
              "Share of samples with status 5xx vs all requests for the selected metric (HTTP-style counters).",
            expr: '100 * sum(rate({__name__="$METRIC",status=~"5.."$INSTANCE}[$INTERVAL])) / sum(rate({__name__="$METRIC"$INSTANCE}[$INTERVAL]))',
            unit: "percent",
            legend: "error %",
            vars: [metricVar({ defaultValue: "promhttp_metric_handler_requests_total" }), instanceVar(), intervalVar()],
          },
          {
            id: "pattern_per_second",
            label: "Per-second rate",
            description:
              "How fast the counter is increasing right now (sum of rate). Use for requests, bytes, errors.",
            expr: 'sum(rate({__name__="$METRIC"$INSTANCE}[$INTERVAL]))',
            unit: "short",
            legend: "rate",
            vars: [
              metricVar({ defaultValue: "promhttp_metric_handler_requests_total" }),
              instanceVar(),
              intervalVar(),
            ],
          },
          {
            id: "pattern_increase",
            label: "Total increase",
            description:
              "How much the counter grew over the window (sum of increase). Useful for totals in a time range.",
            expr: 'sum(increase({__name__="$METRIC"$INSTANCE}[$INTERVAL]))',
            unit: "short",
            legend: "increase",
            vars: [metricVar(), instanceVar(), intervalVar()],
          },
          {
            id: "pattern_p95",
            label: "95th percentile latency",
            description:
              "histogram_quantile on a *_bucket metric. Pick the base metric name; _bucket is appended.",
            expr: 'histogram_quantile(0.95, sum(rate({__name__="$METRIC_bucket"$INSTANCE}[$INTERVAL])) by (le))',
            unit: "short",
            legend: "p95",
            vars: [
              metricVar({ defaultValue: "ta_cmutil_request_seconds" }),
              instanceVar(),
              intervalVar(),
            ],
          },
          {
            id: "pattern_top5",
            label: "Top 5 consumers",
            description:
              "The 5 busiest instances for the selected metric rate — good for spotting hot hosts.",
            expr: 'topk(5, sum by (instance) (rate({__name__="$METRIC"$INSTANCE}[$INTERVAL])))',
            unit: "short",
            legend: "{{instance}}",
            vars: [metricVar(), instanceVar(), intervalVar()],
          },
          {
            id: "pattern_current",
            label: "Current value",
            description:
              "Latest sample sum for the metric (gauge-friendly). Closest to CM telemetry “last” aggregation.",
            expr: 'sum by (instance) ({__name__="$METRIC"$INSTANCE})',
            unit: "short",
            legend: "{{instance}}",
            vars: [
              metricVar({ defaultValue: "promhttp_metric_handler_requests_in_flight" }),
              instanceVar(),
            ],
          },
          {
            id: "pattern_cm_chart",
            label: "CM-style chart",
            description:
              "Full CM telemetry option set: metric + -a/-n/-g/-o/-cl/-i/-s/-d/-t/-e/-f and flags.",
            expr: "__CM_STYLE__",
            unit: "short",
            legend: "{{instance}}",
            vars: cmCliVars("promhttp_metric_handler_requests_in_flight", {
              hideMetric: false,
              metricDefault: "promhttp_metric_handler_requests_in_flight",
            }),
          },
        ],
      },

      // ── CPU ──────────────────────────────────────────────────────────────
      {
        id: "cpu",
        label: "CPU usage",
        description: "Host processor utilization over time",
        queries: [
          {
            id: "cpu_busy",
            label: "CPU usage %",
            description:
              "Percentage of CPU time NOT spent idle. High values mean the host is under load.",
            expr: '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"$INSTANCE}[5m])) * 100)',
            unit: "percent",
            legend: "{{instance}}",
            vars: [
              {
                id: "INSTANCE",
                label: "Host / Instance",
                type: "prometheus_label",
                metric: "node_cpu_seconds_total",
                labelName: "instance",
                allowAll: true,
              },
            ],
          },
          {
            id: "cpu_idle",
            label: "CPU idle rate",
            description:
              "Rate of time the CPU is doing nothing. Lower = more load on the host.",
            expr: 'avg by (instance) (rate(node_cpu_seconds_total{mode="idle"$INSTANCE}[5m]))',
            unit: "short",
            legend: "idle {{instance}}",
            vars: [
              {
                id: "INSTANCE",
                label: "Host / Instance",
                type: "prometheus_label",
                metric: "node_cpu_seconds_total",
                labelName: "instance",
                allowAll: true,
              },
            ],
          },
          {
            id: "cpu_top5",
            label: "Top 5 busiest instances",
            description:
              "The 5 hosts with the highest CPU usage right now — useful to spot which machine is hot.",
            expr: 'topk(5, 100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))',
            unit: "percent",
            legend: "{{instance}}",
          },
        ],
      },

      // ── Memory ───────────────────────────────────────────────────────────
      {
        id: "memory",
        label: "Memory usage",
        description: "Host RAM consumption",
        queries: [
          {
            id: "mem_used",
            label: "Memory used (bytes)",
            description: "Bytes of RAM currently in use. Total − Available.",
            expr: "node_memory_MemTotal_bytes{$INSTANCE_F} - node_memory_MemAvailable_bytes{$INSTANCE_F}",
            unit: "bytes",
            legend: "used {{instance}}",
            vars: [
              {
                id: "INSTANCE",
                label: "Host / Instance",
                type: "prometheus_label",
                metric: "node_memory_MemTotal_bytes",
                labelName: "instance",
                allowAll: true,
              },
            ],
          },
          {
            id: "mem_available",
            label: "Memory available (bytes)",
            description: "Bytes of RAM free for new processes right now.",
            expr: "node_memory_MemAvailable_bytes{$INSTANCE_F}",
            unit: "bytes",
            legend: "available {{instance}}",
            vars: [
              {
                id: "INSTANCE",
                label: "Host / Instance",
                type: "prometheus_label",
                metric: "node_memory_MemAvailable_bytes",
                labelName: "instance",
                allowAll: true,
              },
            ],
          },
          {
            id: "mem_used_pct",
            label: "Memory usage %",
            description:
              "What fraction of total RAM is in use. High values risk OOM kills.",
            expr: "100 * (1 - node_memory_MemAvailable_bytes{$INSTANCE_F} / node_memory_MemTotal_bytes{$INSTANCE_F})",
            unit: "percent",
            legend: "used% {{instance}}",
            vars: [
              {
                id: "INSTANCE",
                label: "Host / Instance",
                type: "prometheus_label",
                metric: "node_memory_MemTotal_bytes",
                labelName: "instance",
                allowAll: true,
              },
            ],
          },
        ],
      },

      // ── Disk ─────────────────────────────────────────────────────────────
      {
        id: "disk",
        label: "Disk usage",
        description: "Filesystem capacity and I/O throughput",
        queries: [
          {
            id: "disk_used_pct",
            label: "Disk usage %",
            description:
              "Percentage of disk space used across all mounted filesystems. Excludes tmpfs and overlay.",
            expr: '100 - ((node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"$INSTANCE} * 100) / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"$INSTANCE})',
            unit: "percent",
            legend: "{{mountpoint}}",
            vars: [
              {
                id: "INSTANCE",
                label: "Host / Instance",
                type: "prometheus_label",
                metric: "node_filesystem_avail_bytes",
                labelName: "instance",
                allowAll: true,
              },
            ],
          },
          {
            id: "disk_read_rate",
            label: "Disk read rate (bytes/s)",
            description:
              "Rate of data being read from disk per second, broken down by device.",
            expr: "sum by (device, instance) (rate(node_disk_read_bytes_total{$INSTANCE_F}[5m]))",
            unit: "bytes",
            legend: "{{instance}} {{device}} read",
            vars: [
              {
                id: "INSTANCE",
                label: "Host / Instance",
                type: "prometheus_label",
                metric: "node_disk_read_bytes_total",
                labelName: "instance",
                allowAll: true,
              },
            ],
          },
          {
            id: "disk_write_rate",
            label: "Disk write rate (bytes/s)",
            description:
              "Rate of data being written to disk per second, broken down by device.",
            expr: "sum by (device, instance) (rate(node_disk_written_bytes_total{$INSTANCE_F}[5m]))",
            unit: "bytes",
            legend: "{{instance}} {{device}} write",
            vars: [
              {
                id: "INSTANCE",
                label: "Host / Instance",
                type: "prometheus_label",
                metric: "node_disk_written_bytes_total",
                labelName: "instance",
                allowAll: true,
              },
            ],
          },
        ],
      },

      // ── Containers ───────────────────────────────────────────────────────
      {
        id: "containers",
        label: "Containers",
        description: "Resource usage broken down by Docker container",
        queries: [
          {
            id: "container_cpu",
            label: "CPU usage per container",
            description:
              "CPU usage rate for each running container. Pick a specific container to isolate it.",
            expr: 'sum(rate(container_cpu_usage_seconds_total{name!=""$NAME}[5m])) by (name)',
            unit: "short",
            legend: "{{name}}",
            vars: [
              {
                id: "NAME",
                label: "Container",
                type: "prometheus_label",
                metric: "container_cpu_usage_seconds_total",
                labelName: "name",
                allowAll: true,
              },
            ],
          },
          {
            id: "container_mem",
            label: "Memory usage per container",
            description:
              "Current memory consumption for each container. Pick a specific container to isolate it.",
            expr: 'sum(container_memory_usage_bytes{name!=""$NAME}) by (name)',
            unit: "bytes",
            legend: "{{name}}",
            vars: [
              {
                id: "NAME",
                label: "Container",
                type: "prometheus_label",
                metric: "container_memory_usage_bytes",
                labelName: "name",
                allowAll: true,
              },
            ],
          },
          {
            id: "container_cpu_top5",
            label: "Top 5 containers by CPU",
            description:
              "Shows only the 5 most CPU-intensive containers — useful during incidents.",
            expr: 'topk(5, sum by (name) (rate(container_cpu_usage_seconds_total{name!=""}[5m])))',
            unit: "short",
            legend: "{{name}}",
          },
        ],
      },

      // ── Demo dataset ─────────────────────────────────────────────────────
      {
        id: "demo_dataset",
        label: "Demo dataset",
        description: "Business metrics from the sample CSV dataset",
        queries: [
          {
            id: "demo_requests",
            label: "Total requests",
            description: "Cumulative request count reported by each demo service.",
            expr: "demo_requests_total",
            unit: "short",
            legend: "{{service}}",
          },
          {
            id: "demo_errors",
            label: "Total errors",
            description: "Cumulative error count reported by each demo service.",
            expr: "demo_errors_total",
            unit: "short",
            legend: "{{service}}",
          },
          {
            id: "demo_latency",
            label: "Request latency (ms)",
            description: "Average request latency in milliseconds per service.",
            expr: "demo_latency_ms",
            unit: "ms",
            legend: "{{service}}",
          },
          {
            id: "demo_cpu",
            label: "Service CPU usage (%)",
            description: "CPU utilization percentage reported by each demo service.",
            expr: "demo_cpu_percent",
            unit: "percent",
            legend: "{{service}}",
          },
          {
            id: "demo_error_rate",
            label: "Error rate %",
            description:
              "Percentage of requests that resulted in an error per service. High = bad.",
            expr: "100 * rate(demo_errors_total[5m]) / rate(demo_requests_total[5m])",
            unit: "percent",
            legend: "{{service}} error%",
          },
          {
            id: "demo_req_rate",
            label: "Requests per second",
            description: "Incoming request rate per second per service (throughput).",
            expr: "rate(demo_requests_total[5m])",
            unit: "short",
            legend: "{{service}} req/s",
          },
        ],
      },
    ],
  },

  // ── OpenSearch / Loki logs ───────────────────────────────────────────────
  opensearch: {
    id: "opensearch",
    label: "OpenSearch logs",
    description:
      "Read-only log explorer inspired by the lab Snippets sidebar (slingshot_switchstate, switch_state, check os indices). Uses OpenSearch when OPENSEARCH_URL is set; otherwise Loki.",
    metrics: [
      {
        id: "app_logs",
        label: "Application logs",
        description:
          "Event rates by severity — filter by container, host, port, index, and keyword",
        queries: [
          {
            id: "all_logs_rate",
            label: "All events (rate)",
            description:
              "Total log lines of any level. Narrow with Container, Host/instance, Port, Index, or Keyword.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$HOST" |= "$PORT" |= "$LOG_FILTER" [1m]))',
            unit: "short",
            legend: "all events",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ filterDefault: "" }),
          },
          {
            id: "error_logs_rate",
            label: "Errors",
            description:
              'Lines with level=error (or keyword "error"). Use Host to focus on admin/leader nodes.',
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "level=error" |= "$HOST" |= "$PORT" [5m]))',
            unit: "short",
            legend: "errors",
            engine: "loki",
            opensearch: { filters: { level: "error" } },
            vars: logQueryVars({
              levelDefault: "error",
              filterDefault: "error",
              includeKeyword: false,
            }),
          },
          {
            id: "warn_logs_rate",
            label: "Warnings",
            description: 'Lines with level=warn / warning in the selected window.',
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "level=warn" |= "$HOST" [5m]))',
            unit: "short",
            legend: "warnings",
            engine: "loki",
            opensearch: { filters: { level: "warn" } },
            vars: logQueryVars({
              levelDefault: "warn",
              filterDefault: "warn",
              includeKeyword: false,
            }),
          },
          {
            id: "info_logs_rate",
            label: "Info",
            description: "Informational log volume for the selected container/host.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "level=info" |= "$HOST" [5m]))',
            unit: "short",
            legend: "info",
            engine: "loki",
            opensearch: { filters: { level: "info" } },
            vars: logQueryVars({
              levelDefault: "info",
              filterDefault: "info",
              includeKeyword: false,
            }),
          },
          {
            id: "debug_logs_rate",
            label: "Debug / trace",
            description: "Debug and trace chatter — useful when diagnosing exporters.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "level=debug" |= "$HOST" [5m]))',
            unit: "short",
            legend: "debug",
            engine: "loki",
            opensearch: { filters: { level: "debug" } },
            vars: logQueryVars({
              levelDefault: "debug",
              filterDefault: "debug",
              includeKeyword: false,
            }),
          },
          {
            id: "critical_logs_rate",
            label: "Critical / fatal",
            description: "Highest-severity lines (critical or fatal).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |~ "level=(critical|fatal)" |= "$HOST" [5m]))',
            unit: "short",
            legend: "critical",
            engine: "loki",
            opensearch: { filters: { level: "critical" } },
            vars: logQueryVars({
              levelDefault: "critical",
              filterDefault: "critical",
              includeKeyword: false,
            }),
          },
          {
            id: "level_picker_rate",
            label: "By selected level",
            description:
              "Pick any Log level from the dropdown and chart its rate for container/host.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "level=$LEVEL" |= "$HOST" |= "$PORT" [5m]))',
            unit: "short",
            legend: "level=$LEVEL",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ levelDefault: "error", includeKeyword: false }),
          },
        ],
      },
      {
        id: "log_volume",
        label: "Log volume",
        description: "Volume breakdowns — by container, level, or top noisy sources",
        queries: [
          {
            id: "by_container",
            label: "By container",
            description:
              "Compare how many log lines each container produces per minute.",
            expr: 'sum by (container) (count_over_time({container=~"$CONTAINER"} |= "$HOST" [1m]))',
            unit: "short",
            legend: "{{container}}",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ includeKeyword: false }),
          },
          {
            id: "by_level",
            label: "By log level",
            description: "Volume for the selected severity (error/warn/info/…).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "level=$LEVEL" |= "$HOST" [1m]))',
            unit: "short",
            legend: "level=$LEVEL",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              levelDefault: "error",
              includeKeyword: false,
            }),
          },
          {
            id: "by_host",
            label: "By host / instance",
            description:
              "Focus on a lab host (admin, leader2:9100, leader3:9256) like the telemetry table.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$HOST" |= "$PORT" [1m]))',
            unit: "short",
            legend: "host=$HOST",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              includeKeyword: false,
            }),
          },
          {
            id: "top_containers",
            label: "Top 5 noisy containers",
            description: "Containers producing the most log lines right now.",
            expr: 'topk(5, sum by (container) (count_over_time({container=~".+"}[1m])))',
            unit: "short",
            legend: "{{container}}",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ includeKeyword: false }),
          },
          {
            id: "windowed_volume",
            label: "Windowed volume",
            description:
              "Total volume over the Window dropdown (1m–24h), with optional keyword.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$HOST" |= "$LOG_FILTER" [$INTERVAL]))',
            unit: "short",
            legend: "volume",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ filterDefault: "" }),
          },
        ],
      },
      {
        id: "os_indices",
        label: "OpenSearch indices / snippets",
        description:
          "Lab Snippets sidebar: slingshot_switchstate, switch_state-grep, check os indices",
        queries: [
          {
            id: "check_os_indices",
            label: "check os indices",
            description:
              "Index / cluster activity — mirrors the ‘check os indices’ curl snippet in the lab UI.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |~ "(?i)indic|opensearch|cluster" |= "$HOST" [1m]))',
            unit: "short",
            legend: "os indices",
            engine: "loki",
            opensearch: { filters: { index: "check_os_indices" } },
            vars: logQueryVars({
              indexDefault: "check_os_indices",
              filterDefault: "indices",
            }),
          },
          {
            id: "slingshot_switchstate",
            label: "slingshot_switchstate",
            description:
              "Slingshot switch-state events (lab snippet: curl GET … slingshot_switchstate).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "slingshot" |= "switch" |= "$HOST" [5m]))',
            unit: "short",
            legend: "slingshot_switchstate",
            engine: "loki",
            opensearch: { filters: { index: "slingshot_switchstate" } },
            vars: logQueryVars({
              indexDefault: "slingshot_switchstate",
              filterDefault: "slingshot",
            }),
          },
          {
            id: "switch_state",
            label: "switch_state",
            description: "Switch state log events (lab snippet switch_state).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "switch_state" |= "$HOST" [5m]))',
            unit: "short",
            legend: "switch_state",
            engine: "loki",
            opensearch: { filters: { index: "switch_state" } },
            vars: logQueryVars({
              indexDefault: "switch_state",
              filterDefault: "switch_state",
            }),
          },
          {
            id: "switch_state_grep",
            label: "switch_state-grep switch",
            description:
              "Grep-style switch filter — mirrors ‘switch_state-grep switch’ in the Snippets list.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "switch" |= "$LOG_FILTER" |= "$HOST" [5m]))',
            unit: "short",
            legend: "switch grep",
            engine: "loki",
            opensearch: { filters: { index: "switch_state-grep" } },
            vars: logQueryVars({
              indexDefault: "switch_state-grep",
              filterDefault: "switch",
            }),
          },
          {
            id: "os_indices_wildcard",
            label: "logs-* / os-indices-* activity",
            description:
              "Broad index pattern activity using the Index / snippet dropdown (logs-*, os-indices-*, …).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$LOG_FILTER" |= "$HOST" [1m]))',
            unit: "short",
            legend: "index=$INDEX",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              indexDefault: "logs-*",
              filterDefault: "",
            }),
          },
          {
            id: "cluster_host_lines",
            label: "Cluster host telemetry lines",
            description:
              "Lines mentioning lab hosts/ports (leader2:9100, leader3:9256) like the central console feed.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$HOST" |= "$PORT" [5m]))',
            unit: "short",
            legend: "host lines",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              indexDefault: "cluster-logs-*",
              filterDefault: "leader",
              includeKeyword: false,
            }),
          },
        ],
      },
      {
        id: "log_search",
        label: "Log search / grep",
        description:
          "Search-style views — keyword, host, port, and level (snippet + CM -f style)",
        queries: [
          {
            id: "level_and_container",
            label: "Level + container + host",
            description:
              "Count lines for a chosen level, container, and optional host/port.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "level=$LEVEL" |= "$HOST" |= "$PORT" [5m]))',
            unit: "short",
            legend: "level=$LEVEL",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              levelDefault: "error",
              includeKeyword: false,
            }),
          },
          {
            id: "keyword_filter",
            label: "Keyword / grep filter",
            description:
              "Count lines matching Keyword / grep (error, slingshot, switch, indices, ports, …).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$LOG_FILTER" |= "$HOST" |= "$PORT" [5m]))',
            unit: "short",
            legend: "matches",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ filterDefault: "error" }),
          },
          {
            id: "host_port_search",
            label: "Host + port search",
            description:
              "Search the console-style host:port feed (e.g. leader2 + 9100).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$HOST" |= "$PORT" [5m]))',
            unit: "short",
            legend: "$HOST:$PORT",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              includeKeyword: false,
            }),
          },
          {
            id: "snippet_keyword",
            label: "Snippet keyword sweep",
            description:
              "Combine Index / snippet + Keyword to mimic running a saved lab snippet against logs.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$LOG_FILTER" |= "$HOST" [$INTERVAL]))',
            unit: "short",
            legend: "$INDEX · $LOG_FILTER",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              indexDefault: "slingshot_switchstate",
              filterDefault: "slingshot",
            }),
          },
          {
            id: "failed_timeout",
            label: "Failures & timeouts",
            description: "Lines mentioning failed, timeout, exception, or OOM.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |~ "(?i)(failed|timeout|exception|OOM|panic)" |= "$HOST" [5m]))',
            unit: "short",
            legend: "failures",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              filterDefault: "failed",
            }),
          },
        ],
      },
      {
        id: "cluster_nodes",
        label: "Cluster nodes (admin / leaders)",
        description:
          "Host-centric views for admin and leader1–3 (ports 9100 / 9256 from lab charts)",
        queries: [
          {
            id: "admin_node",
            label: "admin node",
            description: "Log activity mentioning the admin host.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "admin" |= "$PORT" |= "$LOG_FILTER" [5m]))',
            unit: "short",
            legend: "admin",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ filterDefault: "" }),
          },
          {
            id: "leader_nodes",
            label: "leader nodes",
            description: "Activity across leader1 / leader2 / leader3.",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |~ "leader[123]" |= "$PORT" |= "$LOG_FILTER" [5m]))',
            unit: "short",
            legend: "leaders",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ filterDefault: "" }),
          },
          {
            id: "port_9100",
            label: "Port 9100 exporters",
            description: "Lines mentioning :9100 (node-exporter style endpoints).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "9100" |= "$HOST" [5m]))',
            unit: "short",
            legend: ":9100",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              filterDefault: "9100",
              includeKeyword: false,
            }),
          },
          {
            id: "port_9256",
            label: "Port 9256 exporters",
            description: "Lines mentioning :9256 (secondary exporter endpoints in lab).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "9256" |= "$HOST" [5m]))',
            unit: "short",
            legend: ":9256",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({
              filterDefault: "9256",
              includeKeyword: false,
            }),
          },
          {
            id: "picked_host_port",
            label: "Selected host + port",
            description:
              "Use the Host / instance and Port dropdowns together (e.g. leader2 + 9256).",
            expr: 'sum(count_over_time({container=~"$CONTAINER"} |= "$HOST" |= "$PORT" |= "$LOG_FILTER" [5m]))',
            unit: "short",
            legend: "selected host",
            engine: "loki",
            opensearch: { filters: {} },
            vars: logQueryVars({ filterDefault: "" }),
          },
        ],
      },
    ],
  },
};

export function findQuery(sourceId, metricId, queryId) {
  if (
    typeof sourceId !== "string" ||
    typeof metricId !== "string" ||
    typeof queryId !== "string" ||
    !Object.hasOwn(catalog, sourceId)
  ) {
    return null;
  }
  const source = catalog[sourceId];
  if (!source) return null;
  const metric = source.metrics.find((m) => m.id === metricId);
  if (!metric) return null;
  const query = metric.queries.find((q) => q.id === queryId);
  if (!query) return null;
  return { source, metric, query };
}

/**
 * Build a CM-telemetry-style PromQL expression from metric + aggregation.
 * NODE_ID is display-only metadata from the CM CLI (-i); PromQL still groups
 * by the Prometheus `instance` label.
 */
function promGroupByClause(groupBy) {
  const raw = (groupBy || "instance").trim();
  // CM default labels often don't exist locally — map to Prometheus-safe labels.
  const mapped = raw
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => {
      if (p === "location_type" || p === "hostname") return "instance";
      if (p === "index" || p === "parentalindex") return "job";
      return p;
    });
  const unique = [...new Set(mapped)];
  return unique.length ? unique.join(", ") : "instance";
}

/** Bare hostname (admin) → regex; host:port → exact instance match. */
function instanceMatcher(instance) {
  if (!instance || instance === "__all__") return "";
  if (instance.includes(":") || instance.includes(".*") || instance.includes("|")) {
    return instance.includes(":") && !/[.*+|?]/.test(instance)
      ? `,instance="${instance}"`
      : `,instance=~"${instance}"`;
  }
  return `,instance=~"${instance}.*"`;
}

export function buildCmStyleExpr(varValues = {}) {
  const metric = varValues.METRIC || "up";
  const agg = varValues.AGGREGATION || "avg";
  const interval = varValues.INTERVAL || "5m";
  const instance = varValues.INSTANCE;
  const by = promGroupByClause(varValues.GROUP_BY);
  const instanceFilter = instanceMatcher(instance);
  const selector = `{__name__="${metric}"${instanceFilter}}`;

  switch (agg) {
    case "avg":
      return `avg by (${by}) (${selector})`;
    case "sum":
      return `sum by (${by}) (${selector})`;
    case "min":
      return `min by (${by}) (${selector})`;
    case "max":
      return `max by (${by}) (${selector})`;
    case "count":
      return `count by (${by}) (${selector})`;
    case "median":
      return `quantile by (${by}) (0.5, ${selector})`;
    case "counter":
      return `sum by (${by}) (rate(${selector}[${interval}]))`;
    case "first":
    case "last":
    default:
      return `sum by (${by}) (${selector})`;
  }
}

/**
 * Interpolate user-selected variable values into an expression template.
 *
 * Placeholder rules (by var.type):
 *   prometheus_label  →  $VARID / $VARID_F label filters
 *   loki_label        →  $VARID regex value (`.*` when All)
 *   static|metric_name→  raw value substitution (supports $VAR_bucket suffix)
 *   __CM_STYLE__      →  built from AGGREGATION + METRIC + INSTANCE
 */
export function interpolateVars(exprTemplate, varDefs, varValues) {
  const values = varValues || {};
  if (exprTemplate === "__CM_STYLE__") {
    return buildCmStyleExpr(values);
  }
  if (!varDefs?.length) return exprTemplate;

  let expr = exprTemplate;
  for (const v of varDefs) {
    const val = values[v.id] ?? v.defaultValue;
    const labelName = v.labelName || v.id.toLowerCase();
    const isAll = !val || val === "__all__";

    if (v.type === "loki_label") {
      expr = expr.replaceAll(`$${v.id}`, isAll ? ".*" : val);
    } else if (
      v.type === "static" ||
      v.type === "metric_name" ||
      v.type === "text" ||
      v.type === "checkbox"
    ) {
      // Prefer longer suffixes first so $METRIC_bucket wins over $METRIC
      expr = expr.replaceAll(`$${v.id}_bucket`, isAll ? "" : `${val}_bucket`);
      expr = expr.replaceAll(`$${v.id}`, isAll ? "" : val);
    } else if (v.id === "INSTANCE" || labelName === "instance") {
      const filter = instanceMatcher(isAll ? "__all__" : val);
      const bare = filter.startsWith(",") ? filter.slice(1) : filter;
      // Replace longer placeholders first so $INSTANCE_F is not mangled by $INSTANCE.
      expr = expr.replaceAll(`$${v.id}_F`, isAll ? "" : bare);
      expr = expr.replaceAll(`$${v.id}`, filter);
    } else {
      // Longer `_F` form first (same pitfall as INSTANCE).
      expr = expr.replaceAll(
        `$${v.id}_F`,
        isAll ? "" : `${labelName}="${val}"`
      );
      expr = expr.replaceAll(
        `$${v.id}`,
        isAll ? "" : `,${labelName}="${val}"`
      );
    }
  }
  // Drop empty matcher braces left when optional filters are "All"
  // e.g. node_memory_MemTotal_bytes{} → node_memory_MemTotal_bytes
  expr = expr.replaceAll(/\{(\s*)\}/g, "");
  return expr;
}
