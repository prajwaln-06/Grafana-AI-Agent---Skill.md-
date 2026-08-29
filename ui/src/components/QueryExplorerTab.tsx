import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchCatalog, fetchLabels, runQuery } from "../api";
import type { Catalog, QueryDef, QueryResult, VarDef } from "../types";
import TimeSeriesChart from "./TimeSeriesChart";

const RANGES = [
  { id: "15m", label: "15m" },
  { id: "1h", label: "1h" },
  { id: "6h", label: "6h" },
  { id: "24h", label: "24h" },
];

const ALL_VALUE = "__all__";

type ExplorerTarget = {
  sourceId: string;
  metricId: string;
  queryId: string;
};

type Props = {
  initialTarget?: ExplorerTarget | null;
  onInitialTargetConsumed?: () => void;
};

export default function QueryExplorerTab({
  initialTarget = null,
  onInitialTargetConsumed,
}: Props) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [sourceId, setSourceId] = useState("prometheus");
  const [metricId, setMetricId] = useState("");
  const [queryId, setQueryId] = useState("");
  const [range, setRange] = useState("1h");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [autoRunToken, setAutoRunToken] = useState(0);

  // Variable filter state: id → selected value (or ALL_VALUE)
  const [varValues, setVarValues] = useState<Record<string, string>>({});
  // Available options per variable: id → string[]
  const [varOptions, setVarOptions] = useState<Record<string, string[]>>({});
  // Loading state per variable
  const [varLoading, setVarLoading] = useState<Record<string, boolean>>({});

  // Track in-flight label fetches so we can cancel stale ones
  const labelFetchRef = useRef<AbortController | null>(null);
  const skipSourceResetRef = useRef(false);

  useEffect(() => {
    fetchCatalog()
      .then((c) => {
        setCatalog(c);
        const firstSource = c.prometheus ? "prometheus" : Object.keys(c)[0];
        setSourceId(firstSource);
        const m = c[firstSource]?.metrics[0];
        if (m) {
          setMetricId(m.id);
          setQueryId(m.queries[0]?.id || "");
        }
      })
      .catch((e) => setError(e.message));
  }, []);

  const source = catalog?.[sourceId];
  const metrics = source?.metrics || [];
  const metric = metrics.find((m) => m.id === metricId) || metrics[0];
  const queries = metric?.queries || [];
  const query = queries.find((q) => q.id === queryId) || queries[0];

  useEffect(() => {
    if (!source) return;
    if (skipSourceResetRef.current) {
      skipSourceResetRef.current = false;
      return;
    }
    const m = source.metrics[0];
    if (!m) return;
    setMetricId(m.id);
    setQueryId(m.queries[0]?.id || "");
    setResult(null);
  }, [sourceId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!metric) return;
    if (!metric.queries.find((q) => q.id === queryId)) {
      setQueryId(metric.queries[0]?.id || "");
    }
    setResult(null);
  }, [metricId]); // eslint-disable-line react-hooks/exhaustive-deps

  const defaultVarValue = useCallback((v: VarDef, opts: string[]) => {
    if (
      v.defaultValue !== undefined &&
      (v.allowAll || opts.includes(v.defaultValue))
    ) {
      return v.defaultValue;
    }
    if (!v.allowAll && opts[0] !== undefined) return opts[0];
    return ALL_VALUE;
  }, []);

  // Load options for one variable; returns the value that should be selected
  const loadOneVar = useCallback(
    async (
      v: VarDef,
      currentValues: Record<string, string>
    ): Promise<string> => {
      if (v.type === "static" || v.type === "text" || v.type === "checkbox") {
        const opts = v.options || [];
        const next =
          currentValues[v.id] !== undefined
            ? currentValues[v.id]
            : v.defaultValue !== undefined
              ? v.defaultValue
              : v.type === "checkbox"
                ? "false"
                : defaultVarValue(v, opts);
        setVarOptions((prev) => ({ ...prev, [v.id]: opts }));
        setVarLoading((prev) => ({ ...prev, [v.id]: false }));
        return next;
      }

      setVarLoading((prev) => ({ ...prev, [v.id]: true }));
      try {
        if (v.type === "metric_name") {
          const data = await fetchLabels({
            source: "prometheus",
            labelName: "__name__",
          });
          const live = data.values || [];
          const opts = Array.from(
            new Set([...(v.options || []), ...live])
          ).sort();
          setVarOptions((prev) => ({ ...prev, [v.id]: opts }));
          const existing = currentValues[v.id];
          if (existing && existing !== ALL_VALUE && opts.includes(existing)) {
            return existing;
          }
          return defaultVarValue(v, opts);
        }

        const source = v.type === "loki_label" ? "loki" : "prometheus";
        let metricMatch = v.metric;
        if (v.dependsOn && currentValues[v.dependsOn]) {
          const dep = currentValues[v.dependsOn];
          if (dep && dep !== ALL_VALUE) metricMatch = dep;
        }
        const data = await fetchLabels({
          source,
          metric: metricMatch,
          labelName: v.labelName || v.id.toLowerCase(),
        });
        const live = data.values || [];
        // Merge curated options (e.g. lab hosts) so the dropdown is never a single entry
        const opts = Array.from(
          new Set([...(v.options || []), ...live])
        ).sort();
        setVarOptions((prev) => ({ ...prev, [v.id]: opts }));
        const existing = currentValues[v.id];
        if (existing && (existing === ALL_VALUE || opts.includes(existing))) {
          return existing;
        }
        return defaultVarValue(v, opts);
      } catch {
        const fallback = [...(v.options || [])].sort();
        setVarOptions((prev) => ({ ...prev, [v.id]: fallback }));
        return defaultVarValue(v, fallback);
      } finally {
        setVarLoading((prev) => ({ ...prev, [v.id]: false }));
      }
    },
    [defaultVarValue]
  );

  // Fetch variable options whenever the active query changes
  const fetchVarOptions = useCallback(
    async (q: QueryDef | undefined) => {
      if (labelFetchRef.current) {
        labelFetchRef.current.abort();
      }
      labelFetchRef.current = new AbortController();

      if (!q?.vars?.length) {
        setVarValues({});
        setVarOptions({});
        setVarLoading({});
        return;
      }

      setVarValues({});
      setVarOptions({});
      const loadingMap: Record<string, boolean> = {};
      q.vars.forEach((v) => {
        loadingMap[v.id] =
          v.type !== "static" && v.type !== "text" && v.type !== "checkbox";
      });
      setVarLoading(loadingMap);

      const roots = q.vars.filter((v) => !v.dependsOn);
      const dependents = q.vars.filter((v) => v.dependsOn);
      const resolved: Record<string, string> = {};

      await Promise.all(
        roots.map(async (v) => {
          resolved[v.id] = await loadOneVar(v, resolved);
        })
      );
      setVarValues({ ...resolved });

      await Promise.all(
        dependents.map(async (v) => {
          resolved[v.id] = await loadOneVar(v, resolved);
        })
      );
      setVarValues({ ...resolved });
    },
    [loadOneVar]
  );

  useEffect(() => {
    fetchVarOptions(query);
  }, [query?.id, fetchVarOptions]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!initialTarget || !catalog) return;
    const src = catalog[initialTarget.sourceId];
    if (!src) {
      onInitialTargetConsumed?.();
      return;
    }
    const metric = src.metrics.find((m) => m.id === initialTarget.metricId);
    const q = metric?.queries.find((item) => item.id === initialTarget.queryId);
    if (!metric || !q) {
      onInitialTargetConsumed?.();
      return;
    }
    skipSourceResetRef.current = true;
    setSourceId(initialTarget.sourceId);
    setMetricId(initialTarget.metricId);
    setQueryId(initialTarget.queryId);
    setResult(null);
    setAutoRunToken((n) => n + 1);
    onInitialTargetConsumed?.();
  }, [initialTarget, catalog]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!autoRunToken) return;
    if (!sourceId || !metricId || !queryId) return;
    void onRun();
  }, [autoRunToken]); // eslint-disable-line react-hooks/exhaustive-deps

  // When a dependency (e.g. METRIC) changes, refresh dependent vars (INSTANCE)
  async function onVarChange(v: VarDef, next: string) {
    const updated = { ...varValues, [v.id]: next };
    setVarValues(updated);
    if (v.id === "DURATION" && next && next !== ALL_VALUE) {
      setRange(next);
    }
    const dependents = (query?.vars || []).filter((d) => d.dependsOn === v.id);
    if (!dependents.length) return;
    const resolved = { ...updated };
    for (const dep of dependents) {
      resolved[dep.id] = await loadOneVar(dep, resolved);
    }
    setVarValues(resolved);
  }

  const canRun = Boolean(sourceId && metricId && queryId);

  async function onRun() {
    if (!canRun) return;
    setLoading(true);
    setError(null);
    try {
      // Merge catalog defaults so search auto-run works before live label fetches finish
      const mergedVars: Record<string, string> = {};
      for (const v of query?.vars || []) {
        const selected = varValues[v.id];
        if (selected !== undefined && selected !== "") {
          mergedVars[v.id] = selected;
        } else if (v.defaultValue !== undefined) {
          mergedVars[v.id] = v.defaultValue;
        } else if (v.allowAll === false && v.options?.[0] !== undefined) {
          mergedVars[v.id] = v.options[0];
        } else {
          mergedVars[v.id] = ALL_VALUE;
        }
      }
      // Prefer CM Duration (-d) when present so Range stays in sync with screenshots
      const effectiveRange =
        mergedVars.DURATION && mergedVars.DURATION !== ALL_VALUE
          ? mergedVars.DURATION
          : range;
      if (effectiveRange !== range) setRange(effectiveRange);
      const data = await runQuery({
        sourceId,
        metricId: metric?.id || metricId,
        queryId: query?.id || queryId,
        range: effectiveRange,
        vars: query?.vars?.length ? mergedVars : undefined,
      });
      setResult(data);
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  const seriesCount = result?.series?.length ?? 0;
  const pointCount = useMemo(
    () =>
      result?.series?.reduce((acc, s) => acc + (s.points?.length || 0), 0) ?? 0,
    [result]
  );

  // Human-readable expression preview (frontend approximation for display)
  const displayExpr = useMemo(() => {
    if (!query?.expr) return "";
    if (query.expr === "__CM_STYLE__") {
      const metric = varValues.METRIC || query.vars?.find((v) => v.id === "METRIC")?.defaultValue || "up";
      const agg = varValues.AGGREGATION || "avg";
      const interval = varValues.INTERVAL || "1m";
      const instance = varValues.INSTANCE;
      const by = promGroupByPreview(varValues.GROUP_BY);
      let instanceFilter = "";
      if (instance && instance !== ALL_VALUE) {
        instanceFilter =
          instance.includes(":") && !/[.*+|?]/.test(instance)
            ? `,instance="${instance}"`
            : instance.includes(":")
              ? `,instance=~"${instance}"`
              : `,instance=~"${instance}.*"`;
      }
      const selector = `{__name__="${metric}"${instanceFilter}}`;
      if (agg === "counter") {
        return `sum by (${by}) (rate(${selector}[${interval}]))`;
      }
      if (agg === "median") {
        return `quantile by (${by}) (0.5, ${selector})`;
      }
      if (["avg", "sum", "min", "max", "count"].includes(agg)) {
        return `${agg} by (${by}) (${selector})`;
      }
      return `sum by (${by}) (${selector})`;
    }
    let expr = query.expr;
    for (const v of query.vars || []) {
      const val = varValues[v.id] ?? v.defaultValue;
      const isAll = !val || val === ALL_VALUE;
      const labelName = v.labelName || v.id.toLowerCase();
      if (v.type === "loki_label") {
        expr = expr.replaceAll(`$${v.id}`, isAll ? ".*" : val);
      } else if (
        v.type === "static" ||
        v.type === "metric_name" ||
        v.type === "text" ||
        v.type === "checkbox"
      ) {
        expr = expr.replaceAll(`$${v.id}_bucket`, isAll ? "" : `${val}_bucket`);
        expr = expr.replaceAll(`$${v.id}`, isAll ? "" : val);
      } else if (v.id === "INSTANCE" || labelName === "instance") {
        let filter = "";
        if (!isAll && val) {
          filter =
            val.includes(":") && !/[.*+|?]/.test(val)
              ? `,instance="${val}"`
              : val.includes(":")
                ? `,instance=~"${val}"`
                : `,instance=~"${val}.*"`;
        }
        const bare = filter.startsWith(",") ? filter.slice(1) : filter;
        expr = expr.replaceAll(`$${v.id}_F`, isAll ? "" : bare);
        expr = expr.replaceAll(`$${v.id}`, filter);
      } else {
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
    return expr.replaceAll(/\{(\s*)\}/g, "");
  }, [query, varValues]);

  if (!catalog) {
    return (
      <div className="panel query-loading">
        <p className="muted">{error || "Loading catalog…"}</p>
      </div>
    );
  }

  const sources = Object.values(catalog);

  return (
    <div className="query-explorer">
      <aside className="query-composer">
        <header className="query-composer-head">
          <h2>Query explorer</h2>
        </header>

        <div className="query-composer-body">
          {/* ── Source ────────────────────────────────────────── */}
          <section className="query-block">
            <div className="query-block-label">Source</div>
            <div
              className="source-tiles"
              role="radiogroup"
              aria-label="Data source"
            >
              {sources.map((s) => {
                const active = sourceId === s.id;
                const kind = sourceKind(s.id);
                return (
                  <button
                    key={s.id}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    className={active ? "source-tile active" : "source-tile"}
                    onClick={() => setSourceId(s.id)}
                  >
                    <span className={`source-mark ${kind}`}>{kindMark(kind)}</span>
                    <span className="source-tile-copy">
                      <span className="source-tile-title">{shortSourceLabel(s)}</span>
                      <span className="source-tile-kind">
                        {kind === "logs" ? "Logs" : "Metrics"}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </section>

          {/* ── Stream ────────────────────────────────────────── */}
          <section className="query-block">
            <div className="query-block-label">Stream</div>
            <div className="stream-list" role="listbox" aria-label="Metric or log stream">
              {metrics.map((m) => {
                const active = (metric?.id || metricId) === m.id;
                return (
                  <button
                    key={m.id}
                    type="button"
                    role="option"
                    aria-selected={active}
                    className={active ? "stream-item active" : "stream-item"}
                    onClick={() => setMetricId(m.id)}
                    title={m.description}
                  >
                    <span className="stream-item-title">{m.label}</span>
                    <span className="stream-item-meta">
                      {m.queries.length} view{m.queries.length === 1 ? "" : "s"}
                    </span>
                  </button>
                );
              })}
            </div>
          </section>

          {/* ── View (query pattern / ready-made query) ───────── */}
          <section className="query-block">
            <label className="query-block-label" htmlFor="query-view">
              {metric?.id === "query_patterns"
                ? "Pattern"
                : metric?.id === "cm_telemetry"
                  ? "Metric / query"
                  : "View"}
            </label>
            <div className="query-select-wrap">
              <select
                id="query-view"
                className={
                  metric?.id === "cm_telemetry"
                    ? "query-select query-select-metrics"
                    : "query-select"
                }
                value={query?.id || ""}
                onChange={(e) => setQueryId(e.target.value)}
              >
                {metric?.id === "cm_telemetry" ? (
                  <>
                    <optgroup label="Cluster metrics">
                      {queries
                        .filter((q) => !q.id.startsWith("cm_ent"))
                        .map((q) => (
                          <option key={q.id} value={q.id} title={q.description}>
                            {q.label}
                          </option>
                        ))}
                    </optgroup>
                    <optgroup label="Normal metrics">
                      {queries
                        .filter((q) => q.id.startsWith("cm_ent"))
                        .map((q) => (
                          <option key={q.id} value={q.id} title={q.description}>
                            {q.label}
                          </option>
                        ))}
                    </optgroup>
                  </>
                ) : (
                  queries.map((q) => (
                    <option key={q.id} value={q.id} title={q.description}>
                      {q.label}
                    </option>
                  ))
                )}
              </select>
            </div>
            {query?.description && (
              <p className="query-view-description muted small">
                {query.description}
              </p>
            )}
          </section>

          {/* ── Variables / CM CLI options ─────────────────────── */}
          {(query?.vars?.length ?? 0) > 0 && (
            <section className="query-block">
              <div className="query-block-label">
                {metric?.id === "cm_telemetry"
                  ? "cm telemetry query options"
                  : sourceId === "opensearch"
                    ? "OpenSearch / log query options"
                    : "Variables"}
              </div>
              <p className="query-vars-help muted small">
                {metric?.id === "cm_telemetry" || query?.id === "pattern_cm_chart"
                  ? "Cluster query options: aggregation, node, group-by, output, step, time, filter, and flags."
                  : sourceId === "opensearch"
                    ? "OpenSearch / lab log filters: container, host (admin/leader:port), port, level, index/snippet, group-by, window, and keyword/grep."
                    : "Apply these to the pattern. Hosts load live from Prometheus when available."}
              </p>
              <div className="query-vars">
                {query!.vars!.filter((v) => !v.hidden).map((v) => {
                  const opts = varOptions[v.id] || v.options || [];
                  const isLoading = varLoading[v.id];
                  const selected =
                    varValues[v.id] ??
                    v.defaultValue ??
                    (v.allowAll === false ? opts[0] : ALL_VALUE) ??
                    ALL_VALUE;
                  const showAll = v.allowAll !== false && v.type !== "text";

                  if (v.type === "checkbox") {
                    const checked = selected === "true" || selected === "1";
                    return (
                      <div key={v.id} className="query-var-row query-var-check">
                        <label className="query-check-label" htmlFor={`var-${v.id}`}>
                          <input
                            id={`var-${v.id}`}
                            type="checkbox"
                            checked={checked}
                            onChange={(e) =>
                              onVarChange(v, e.target.checked ? "true" : "false")
                            }
                          />
                          <span>{v.label}</span>
                        </label>
                        {v.hint && (
                          <p className="query-var-hint muted small">{v.hint}</p>
                        )}
                      </div>
                    );
                  }

                  if (v.type === "text") {
                    return (
                      <div key={v.id} className="query-var-row">
                        <label
                          className="query-var-label"
                          htmlFor={`var-${v.id}`}
                        >
                          {v.label}
                        </label>
                        <input
                          id={`var-${v.id}`}
                          className="query-text"
                          type="text"
                          value={selected === ALL_VALUE ? "" : selected}
                          placeholder={v.placeholder || ""}
                          onChange={(e) => onVarChange(v, e.target.value)}
                        />
                        {v.hint && (
                          <p className="query-var-hint muted small">{v.hint}</p>
                        )}
                      </div>
                    );
                  }

                  return (
                    <div key={v.id} className="query-var-row">
                      <label
                        className="query-var-label"
                        htmlFor={`var-${v.id}`}
                      >
                        {v.label}
                      </label>
                      <div className="query-select-wrap">
                        <select
                          id={`var-${v.id}`}
                          className="query-select"
                          value={selected}
                          disabled={isLoading}
                          onChange={(e) => onVarChange(v, e.target.value)}
                        >
                          {showAll && (
                            <option value={ALL_VALUE}>
                              {isLoading
                                ? "Loading…"
                                : v.id === "INSTANCE"
                                  ? "All nodes"
                                  : "All"}
                            </option>
                          )}
                          {!showAll && isLoading && (
                            <option value={selected}>Loading…</option>
                          )}
                          {opts.map((opt) => (
                            <option key={opt} value={opt}>
                              {formatVarOption(v, opt)}
                            </option>
                          ))}
                        </select>
                      </div>
                      {v.hint && (
                        <p className="query-var-hint muted small">{v.hint}</p>
                      )}
                    </div>
                  );
                })}
              </div>
              {(metric?.id === "cm_telemetry" ||
                query?.id === "pattern_cm_chart") &&
                query && (
                <details className="query-expr cm-cmd-preview" open>
                  <summary>Equivalent CM command</summary>
                  <code>
                    {buildCmCommand(
                      query.id === "pattern_cm_chart"
                        ? `.${varValues.METRIC || "metric"}`
                        : query.label,
                      varValues,
                      range
                    )}
                  </code>
                </details>
              )}
            </section>
          )}

          {/* ── Range ─────────────────────────────────────────── */}
          <section className="query-block">
            <div className="query-block-label">Range</div>
            <div className="range-chips" role="radiogroup" aria-label="Time range">
              {RANGES.map((r) => (
                <button
                  key={r.id}
                  type="button"
                  role="radio"
                  aria-checked={range === r.id}
                  className={range === r.id ? "range-chip active" : "range-chip"}
                  onClick={() => setRange(r.id)}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </section>

          {/* ── Expression (shows resolved query) ─────────────── */}
          {query && (
            <details className="query-expr">
              <summary>Expression</summary>
              <code>{displayExpr || query.expr}</code>
            </details>
          )}
        </div>

        <footer className="query-composer-foot">
          <button
            type="button"
            className="btn primary query-run"
            disabled={!canRun || loading}
            onClick={onRun}
          >
            {loading ? "Running…" : "Run query"}
          </button>
        </footer>
      </aside>

      {/* ── Result panel ──────────────────────────────────────── */}
      <section className="query-stage">
        <header className="query-stage-head">
          <div className="query-stage-title">
            <h2>{result ? result.query.label : "Result"}</h2>
            {result ? (
              <p className="query-stage-meta">
                <span>{result.source.id === "opensearch" ? "OpenSearch logs" : result.source.label}</span>
                <span className="meta-sep" aria-hidden="true" />
                <span>{result.range.label}</span>
                <span className="meta-sep" aria-hidden="true" />
                <span>
                  {seriesCount} series · {pointCount} pts
                </span>
              </p>
            ) : (
              <p className="query-stage-meta">Configure a query, then run it</p>
            )}
          </div>
          {result?.plotImage && <span className="render-badge">Matplotlib</span>}
        </header>

        {error && <div className="alert error">{error}</div>}

        {!result && !error && (
          <div className="query-empty">
            <div className="query-empty-inner">
              <p className="query-empty-title">No chart yet</p>
              <p className="muted small">
                Pick a source, stream, pattern/view, variables, and range — then
                run the query.
              </p>
              <ol className="query-empty-steps">
                <li className={sourceId ? "done" : undefined}>Source</li>
                <li className={metricId ? "done" : undefined}>Stream</li>
                <li className={queryId ? "done" : undefined}>
                  {metric?.id === "query_patterns" ? "Pattern" : "View"}
                </li>
                <li
                  className={
                    !query?.vars?.length || Object.keys(varValues).length
                      ? "done"
                      : undefined
                  }
                >
                  Variables
                </li>
                <li className={range ? "done" : undefined}>Range</li>
              </ol>
            </div>
          </div>
        )}

        {result && (
          <div className="query-stage-body">
            {result.plotImage ? (
              <div className="query-image-frame">
                <img
                  className="query-image"
                  src={result.plotImage}
                  alt={`${result.query.label} trend chart`}
                />
              </div>
            ) : (
              <div className="query-chart-frame">
                <TimeSeriesChart
                  series={result.series}
                  unit={result.query.unit}
                />
              </div>
            )}
            <details className="raw-details">
              <summary>Series</summary>
              <ul className="series-list">
                {result.series.map((s, i) => (
                  <li key={i}>
                    <code>{s.name}</code>
                    <span className="muted small"> ({s.points.length} pts)</span>
                  </li>
                ))}
              </ul>
            </details>
          </div>
        )}
      </section>
    </div>
  );
}

function sourceKind(id: string): "metrics" | "logs" {
  return id === "opensearch" || id === "loki" ? "logs" : "metrics";
}

function kindMark(kind: "metrics" | "logs") {
  return kind === "logs" ? "L" : "M";
}

function shortSourceLabel(source: { id: string; label: string }) {
  if (source.id === "opensearch") return "OpenSearch";
  if (source.id === "prometheus") return "Prometheus";
  return source.label;
}

function promGroupByPreview(groupBy?: string) {
  const raw = (groupBy || "instance").trim();
  const mapped = raw
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => {
      if (p === "location_type" || p === "hostname") return "instance";
      if (p === "index" || p === "parentalindex") return "job";
      return p;
    });
  return [...new Set(mapped)].join(", ") || "instance";
}

function buildCmCommand(
  metricLabel: string,
  values: Record<string, string>,
  rangeLabel: string
) {
  const metric = metricLabel.startsWith(".")
    ? metricLabel
    : `.${metricLabel.replace(/^\./, "")}`;
  const parts = [`cm telemetry query ${metric}`];
  const agg = values.AGGREGATION || "last";
  parts.push(`-a ${agg}`);
  const node = values.INSTANCE;
  if (node && node !== ALL_VALUE) parts.push(`-n ${node}`);
  const groupBy = values.GROUP_BY?.trim();
  if (groupBy) parts.push(`-g ${groupBy}`);
  const output = values.OUTPUT || "chart";
  parts.push(`-o ${output}`);
  const cl = values.CHART_LIMIT?.trim();
  if (cl) parts.push(`-cl ${cl}`);
  const nodeId = values.NODE_ID || "hostname";
  parts.push(`-i ${nodeId}`);
  const step = values.INTERVAL || "1m";
  parts.push(`-s ${step}`);
  const tsStart = values.TS_START?.trim();
  if (tsStart && tsStart !== ALL_VALUE) parts.push(`-t ${tsStart}`);
  const tsEnd = values.TS_END?.trim();
  if (tsEnd && tsEnd !== ALL_VALUE) parts.push(`-e ${tsEnd}`);
  const duration = values.DURATION?.trim() || rangeLabel;
  if (duration && !tsStart && !tsEnd) parts.push(`-d ${duration}`);
  const filter = values.FILTER?.trim();
  if (filter && filter !== ALL_VALUE) parts.push(`-f ${JSON.stringify(filter)}`);
  if (values.UTCTIME === "true") parts.push("-ut");
  if (values.NO_COLOR === "true") parts.push("-nc");
  if (values.NO_HEADER === "true") parts.push("-nh");
  if (values.VERBOSE === "true") parts.push("-v");
  return parts.join(" ");
}

function formatVarOption(v: VarDef, opt: string) {
  if (v.type === "metric_name") {
    // Screenshots show leading dots in CM telemetry; keep PromQL-safe names in value
    return opt.startsWith(".") ? opt : opt;
  }
  if (v.id === "AGGREGATION") {
    const hints: Record<string, string> = {
      avg: "avg — mean in each window",
      count: "count — sample count",
      median: "median — middle value",
      min: "min — lowest sample",
      max: "max — highest sample",
      sum: "sum — total of samples",
      counter: "counter — rate of change",
      last: "last — last sample (default)",
      first: "first — first sample",
    };
    return hints[opt] || opt;
  }
  if (v.id === "NODE_ID") {
    const hints: Record<string, string> = {
      hostname: "hostname",
      xname: "xname",
      ip: "ip",
      original: "original",
      ALIAS: "ALIAS",
    };
    return hints[opt] || opt;
  }
  if (v.id === "OUTPUT") {
    const hints: Record<string, string> = {
      table: "table",
      csv: "csv",
      json: "json",
      chart: "chart (UI default)",
      prom: "prom",
    };
    return hints[opt] || opt;
  }
  if (v.id === "GROUP_BY") {
    const hints: Record<string, string> = {
      "location_type,index,parentalindex":
        "location_type, index, parentalindex (CM default)",
      location_type: "location_type",
      index: "index",
      parentalindex: "parentalindex",
      "location_type,index": "location_type, index",
      hostname: "hostname",
      instance: "instance",
      "instance,job": "instance, job",
      "instance,name": "instance, name",
      job: "job",
      name: "name (container)",
      device: "device",
      mountpoint: "mountpoint",
      service: "service",
    };
    return hints[opt] || opt;
  }
  if (v.id === "TS_START") {
    if (!opt) return "(none — use duration)";
    const labels: Record<string, string> = {
      "now-5m": "now-5m — 5 minutes ago",
      "now-15m": "now-15m — 15 minutes ago",
      "now-30m": "now-30m — 30 minutes ago",
      "now-1h": "now-1h — 1 hour ago",
      "now-3h": "now-3h — 3 hours ago",
      "now-6h": "now-6h — 6 hours ago",
      "now-12h": "now-12h — 12 hours ago",
      "now-24h": "now-24h — 1 day ago",
      "now-2d": "now-2d — 2 days ago",
      "now-7d": "now-7d — 1 week ago",
      "now-14d": "now-14d — 2 weeks ago",
      "now-30d": "now-30d — 30 days ago",
    };
    return labels[opt] || opt;
  }
  if (v.id === "TS_END") {
    if (!opt) return "(none — now)";
    const labels: Record<string, string> = {
      now: "now — current time",
      "now-5m": "now-5m — end 5 minutes ago",
      "now-15m": "now-15m — end 15 minutes ago",
      "now-30m": "now-30m — end 30 minutes ago",
      "now-1h": "now-1h — end 1 hour ago",
      "now-3h": "now-3h — end 3 hours ago",
      "now-6h": "now-6h — end 6 hours ago",
      "now-12h": "now-12h — end 12 hours ago",
      "now-24h": "now-24h — end 1 day ago",
      "now-2d": "now-2d — end 2 days ago",
      "now-7d": "now-7d — end 1 week ago",
    };
    return labels[opt] || opt;
  }
  if (v.id === "FILTER") {
    if (!opt) return "(none)";
    const labels: Record<string, string> = {
      'status=~"5.."': 'status 5xx errors',
      'status=~"4.."': 'status 4xx client errors',
      'status=~"2.."': 'status 2xx success',
      'job="prometheus"': 'job = prometheus',
      'job="node"': 'job = node',
      'job="cadvisor"': 'job = cadvisor',
      'job=~"node|prometheus"': 'job = node or prometheus',
      'instance=~"admin.*"': 'instance admin*',
      'instance=~"leader.*"': 'instance leader*',
      'instance=~".*:9100"': 'port 9100',
      'instance=~".*:9256"': 'port 9256',
      'name!=""': 'named containers only',
      'name=~".+"': 'any container name',
      'container!=""': 'has container label',
      'device!=""': 'has device label',
      'fstype!="tmpfs"': 'exclude tmpfs',
      'mode="idle"': 'CPU idle mode',
      'mode!="idle"': 'CPU non-idle modes',
      'level="error"': 'log level error',
      'level="warn"': 'log level warn',
      'level="info"': 'log level info',
      'service="checkout"': 'service checkout',
      'service="payments"': 'service payments',
    };
    return labels[opt] || opt;
  }
  if (v.id === "CHART_LIMIT") return `${opt} entries`;
  if (v.id === "DURATION") return opt;
  if (v.id === "LEVEL") {
    if (!opt) return "(all levels)";
    const labels: Record<string, string> = {
      error: "error — failures needing attention",
      warn: "warn — warnings",
      warning: "warning — alias of warn",
      info: "info — normal events",
      debug: "debug — verbose diagnostics",
      trace: "trace — deepest detail",
      critical: "critical — urgent",
      fatal: "fatal — process-stopping",
    };
    return labels[opt] || opt;
  }
  if (v.id === "INDEX") {
    const labels: Record<string, string> = {
      "logs-*": "logs-* — default log index pattern",
      slingshot_switchstate: "slingshot_switchstate — lab snippet",
      switch_state: "switch_state — lab snippet",
      "switch_state-grep": "switch_state-grep — grep switch snippet",
      check_os_indices: "check_os_indices — lab snippet",
      "os-indices-*": "os-indices-* — OpenSearch index pattern",
      "application-*": "application-* — app logs",
      "cluster-logs-*": "cluster-logs-* — cluster / host lines",
      "node-exporter-*": "node-exporter-* — exporter-related",
      "promtail-*": "promtail-* — shipper logs",
    };
    return labels[opt] || opt;
  }
  if (v.id === "LOG_FILTER") {
    if (!opt) return "(no keyword — match any text)";
    const labels: Record<string, string> = {
      error: "error",
      warn: "warn",
      warning: "warning",
      timeout: "timeout",
      exception: "exception",
      failed: "failed",
      OOM: "OOM — out of memory",
      panic: "panic",
      switch: "switch — grep switch",
      switch_state: "switch_state",
      switchstate: "switchstate",
      slingshot: "slingshot",
      slingshot_switchstate: "slingshot_switchstate",
      indices: "indices — check os indices",
      cluster: "cluster",
      leader: "leader",
      admin: "admin",
      "9100": "9100 — exporter port",
      "9256": "9256 — secondary port",
      curl: "curl",
      GET: "GET",
    };
    return labels[opt] || opt;
  }
  if (v.id === "HOST") {
    if (!opt) return "(all hosts)";
    if (opt.includes(":")) return `${opt} — host:port from lab charts`;
    return `${opt} — cluster node`;
  }
  if (v.id === "PORT") {
    if (!opt) return "(any port)";
    const labels: Record<string, string> = {
      "9100": "9100 — node exporter style",
      "9256": "9256 — secondary exporter",
      "9090": "9090 — Prometheus",
      "3100": "3100 — Loki",
      "3000": "3000 — Grafana",
      "9200": "9200 — OpenSearch",
    };
    return labels[opt] || opt;
  }
  if (v.id === "CONTAINER") {
    if (!opt) return "(all containers)";
    return opt;
  }
  if (v.id === "GROUP_BY_LOG") {
    const labels: Record<string, string> = {
      container: "container — per container",
      level: "level — per severity",
      host: "host — per node",
      index: "index — per index/snippet",
      service: "service — per service",
    };
    return labels[opt] || opt;
  }
  return opt;
}
