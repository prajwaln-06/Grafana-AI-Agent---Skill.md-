import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Series } from "../types";

const COLORS = [
  "#f7931a",
  "#3ecf8e",
  "#60a5fa",
  "#e6b450",
  "#a78bfa",
  "#f07178",
  "#38bdf8",
  "#c084fc",
];

type Props = {
  series: Series[];
  unit?: string;
  chartType?: string;
  height?: number;
  /** Dense sparkline-style chart for cards */
  compact?: boolean;
};

type ChartTheme = {
  text: string;
  muted: string;
  mutedDim: string;
  elev: string;
  border: string;
  grid: string;
  cursor: string;
  shadow: string;
};

function readChartTheme(): ChartTheme {
  const styles = getComputedStyle(document.documentElement);
  const get = (name: string, fallback: string) =>
    styles.getPropertyValue(name).trim() || fallback;
  return {
    text: get("--text", "#fafafa"),
    muted: get("--muted", "#8a8a8a"),
    mutedDim: get("--muted-dim", "#5c5c5c"),
    elev: get("--bg-elev", "#0a0a0a"),
    border: get("--border-strong", "rgba(255,255,255,0.12)"),
    grid: get("--border", "rgba(255,255,255,0.08)"),
    cursor: get("--primary-border", "rgba(247,147,26,0.35)"),
    shadow: get("--shadow", "0 12px 32px rgba(0,0,0,0.55)"),
  };
}

function useChartTheme() {
  const [theme, setTheme] = useState<ChartTheme>(() => readChartTheme());
  useEffect(() => {
    const sync = () => setTheme(readChartTheme());
    sync();
    const obs = new MutationObserver(sync);
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => obs.disconnect();
  }, []);
  return theme;
}

function formatTick(ts: number) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatTooltipTime(ts: number) {
  const d = new Date(ts * 1000);
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatValue(v: number | null | undefined, unit?: string): string {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const u = (unit || "").toLowerCase();
  if (u === "percent" || u === "%") return `${v.toFixed(1)}%`;
  if (u === "bytes" || u === "b") {
    const abs = Math.abs(v);
    if (abs >= 1024 ** 4) return `${(v / 1024 ** 4).toFixed(2)} TB`;
    if (abs >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)} GB`;
    if (abs >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(2)} MB`;
    if (abs >= 1024) return `${(v / 1024).toFixed(1)} KB`;
    return `${v.toFixed(0)} B`;
  }
  if (u === "seconds" || u === "s") {
    if (Math.abs(v) < 0.001) return `${(v * 1000000).toFixed(0)} µs`;
    if (Math.abs(v) < 1) return `${(v * 1000).toFixed(1)} ms`;
    return `${v.toFixed(2)} s`;
  }
  if (u === "celsius" || u === "°c") return `${v.toFixed(1)} °C`;
  if (Math.abs(v) >= 1000000) return `${(v / 1000000).toFixed(2)}M`;
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(1)}k`;
  if (Number.isInteger(v)) return v.toString();
  return v.toFixed(2);
}

// Circular SVG Radial Gauge Component
function SvgGauge({
  value,
  min = 0,
  max = 100,
  unit,
  title,
  theme,
}: {
  value: number;
  min?: number;
  max?: number;
  unit?: string;
  title?: string;
  theme: ChartTheme;
}) {
  const safeMax = max > min ? max : 100;
  const clamped = Math.max(min, Math.min(safeMax, value));
  const ratio = (clamped - min) / (safeMax - min);

  // 240 degree gauge arc
  const radius = 70;
  const circumference = 2 * Math.PI * radius * (240 / 360);
  const strokeDashoffset = circumference - ratio * circumference;

  const color =
    ratio > 0.85 ? "#ef4444" : ratio > 0.7 ? "#f59e0b" : "#3ecf8e";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
        background: theme.elev,
        border: `1px solid ${theme.border}`,
        borderRadius: "8px",
      }}
    >
      {title && (
        <span
          style={{
            fontSize: "12px",
            color: theme.muted,
            marginBottom: "8px",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
          }}
        >
          {title}
        </span>
      )}
      <div style={{ position: "relative", width: "180px", height: "140px" }}>
        <svg
          viewBox="0 0 180 150"
          style={{ width: "100%", height: "100%", overflow: "visible" }}
        >
          {/* Background Track */}
          <path
            d="M 30 130 A 70 70 0 1 1 150 130"
            fill="none"
            stroke={theme.grid}
            strokeWidth="14"
            strokeLinecap="round"
          />
          {/* Active Colored Arc */}
          <path
            d="M 30 130 A 70 70 0 1 1 150 130"
            fill="none"
            stroke={color}
            strokeWidth="14"
            strokeLinecap="round"
            strokeDasharray={`${circumference} ${circumference}`}
            strokeDashoffset={strokeDashoffset}
            style={{ transition: "stroke-dashoffset 0.6s ease" }}
          />
        </svg>
        <div
          style={{
            position: "absolute",
            bottom: "18px",
            left: 0,
            right: 0,
            textAlign: "center",
          }}
        >
          <div
            style={{
              fontSize: "26px",
              fontWeight: "700",
              color: theme.text,
              fontFamily: "IBM Plex Mono, monospace",
            }}
          >
            {formatValue(value, unit)}
          </div>
          <span style={{ fontSize: "11px", color: theme.mutedDim }}>
            {min} {unit} → {safeMax} {unit}
          </span>
        </div>
      </div>
    </div>
  );
}

export default function TimeSeriesChart({
  series,
  unit,
  chartType: initialChartType = "line",
  height = 240,
  compact = false,
}: Props) {
  const theme = useChartTheme();
  const normalizedInitial = (initialChartType || "line").toLowerCase();
  const [selectedType, setSelectedType] = useState<string>(normalizedInitial);

  useEffect(() => {
    if (initialChartType) setSelectedType(initialChartType.toLowerCase());
  }, [initialChartType]);

  const { data, visibleKeys, stats, categoryData } = useMemo(() => {
    if (!series || series.length === 0) {
      return {
        data: [],
        visibleKeys: [],
        stats: { latest: 0, min: 0, max: 0, avg: 0 },
        categoryData: [],
      };
    }

    const timeMap = new Map<number, Record<string, number | null>>();
    const keys: string[] = [];
    let allVals: number[] = [];
    const catList: Array<{ name: string; value: number; unit?: string }> = [];

    series.forEach((s, sIdx) => {
      const key = s.name || `series_${sIdx}`;
      keys.push(key);
      const points = s.points || [];
      points.forEach((pt) => {
        if (!timeMap.has(pt.t)) timeMap.set(pt.t, { t: pt.t });
        const row = timeMap.get(pt.t)!;
        row[key] = pt.v;
        if (pt.v !== null && pt.v !== undefined && !isNaN(pt.v)) {
          allVals.push(pt.v);
        }
      });

      // Compute latest value for categorical distribution
      const latestPt = points.length > 0 ? points[points.length - 1] : null;
      if (latestPt && latestPt.v !== null && !isNaN(latestPt.v)) {
        let label = key;
        if (s.labels) {
          if (s.labels.cpu !== undefined) label = `Core ${s.labels.cpu}`;
          else if (s.labels.service) label = s.labels.service;
          else if (s.labels.instance) label = s.labels.instance;
          else if (s.labels.gpu !== undefined) label = `GPU ${s.labels.gpu}`;
        }
        catList.push({
          name: label,
          value: latestPt.v,
          unit,
        });
      }
    });

    const rows = Array.from(timeMap.values()).sort(
      (a, b) => (a.t as number) - (b.t as number)
    );
    const latest = allVals.length > 0 ? allVals[allVals.length - 1] : 0;
    const min = allVals.length > 0 ? Math.min(...allVals) : 0;
    const max = allVals.length > 0 ? Math.max(...allVals) : 0;
    const avg =
      allVals.length > 0
        ? allVals.reduce((a, b) => a + b, 0) / allVals.length
        : 0;

    return {
      data: rows,
      visibleKeys: keys,
      stats: { latest, min, max, avg },
      categoryData: catList.sort((a, b) => {
        const numA = parseInt(a.name.replace(/\D/g, ""), 10);
        const numB = parseInt(b.name.replace(/\D/g, ""), 10);
        if (!isNaN(numA) && !isNaN(numB)) return numA - numB;
        return b.value - a.value;
      }),
    };
  }, [series, unit]);

  if (!series || series.length === 0 || data.length === 0) {
    return (
      <div className="chart-empty" style={{ height }}>
        <span className="muted small">No chartable data points</span>
      </div>
    );
  }

  const tooltipStyle = {
    background: theme.elev,
    border: `1px solid ${theme.border}`,
    borderRadius: 6,
    boxShadow: theme.shadow,
    fontSize: 12,
    padding: "8px 12px",
  };

  // If only 1 time point (instant query), auto-render gauge instead of empty line
  const effectiveType = data.length <= 1 && (selectedType === "line" || selectedType === "area") ? "gauge" : selectedType;

  return (
    <div className="chart-container" style={{ position: "relative" }}>
      {/* Render View Based on selectedType chosen autonomously by chart_selection */}
      {effectiveType === "gauge" ? (
        <SvgGauge
          value={stats.latest}
          min={0}
          max={unit === "percent" || unit === "%" || stats.latest <= 100 ? 100 : Math.ceil(stats.max * 1.2) || 100}
          unit={unit}
          title={series[0]?.name || "Telemetry Gauge"}
          theme={theme}
        />
      ) : selectedType === "bar" ? (
        <div
          style={{
            height,
            background: theme.elev,
            border: `1px solid ${theme.border}`,
            borderRadius: "8px",
            padding: "12px",
          }}
        >
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={categoryData.length > 0 ? categoryData : [{ name: "Current", value: stats.latest }]}
              layout="vertical"
              margin={{ top: 8, right: 24, left: 40, bottom: 4 }}
            >
              <CartesianGrid stroke={theme.grid} strokeDasharray="2 8" horizontal={false} />
              <XAxis
                type="number"
                tick={{ fill: theme.mutedDim, fontSize: 11, fontFamily: "monospace" }}
                tickFormatter={(v) => formatValue(Number(v), unit)}
              />
              <YAxis
                type="category"
                dataKey="name"
                tick={{ fill: theme.text, fontSize: 11, fontFamily: "monospace" }}
                width={80}
              />
              <Tooltip
                cursor={{ fill: "rgba(255,255,255,0.04)" }}
                contentStyle={tooltipStyle}
                formatter={(val: number) => [formatValue(val, unit), "Value"]}
              />
              <Bar dataKey="value" radius={[0, 4, 4, 0]} maxBarSize={22}>
                {categoryData.map((_, i) => (
                  <Cell key={`cell-${i}`} fill={COLORS[i % COLORS.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      ) : selectedType === "table" ? (
        <div
          className="chart-table-wrap"
          style={{
            maxHeight: height,
            overflowY: "auto",
            background: theme.elev,
            border: `1px solid ${theme.border}`,
            borderRadius: "8px",
            padding: "8px",
          }}
        >
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${theme.border}`, textAlign: "left", color: theme.muted }}>
                <th style={{ padding: "8px" }}>Time</th>
                {visibleKeys.map((k) => (
                  <th key={k} style={{ padding: "8px" }}>{k}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.slice(-15).map((row, idx) => (
                <tr key={idx} style={{ borderBottom: `1px solid ${theme.grid}` }}>
                  <td style={{ padding: "6px 8px", color: theme.mutedDim, fontFamily: "monospace" }}>
                    {formatTick(row.t as number)}
                  </td>
                  {visibleKeys.map((k) => (
                    <td key={k} style={{ padding: "6px 8px", color: theme.text, fontFamily: "monospace" }}>
                      {formatValue(row[k], unit)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : selectedType === "area" ? (
        <div className="chart-wrap" style={{ height }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={compact ? { top: 4, right: 4, left: 4, bottom: 2 } : { top: 8, right: 12, left: 0, bottom: 4 }}>
              <defs>
                {visibleKeys.map((key, i) => (
                  <linearGradient key={key} id={`area-grad-${i}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={COLORS[i % COLORS.length]} stopOpacity={0.4} />
                    <stop offset="95%" stopColor={COLORS[i % COLORS.length]} stopOpacity={0.0} />
                  </linearGradient>
                ))}
              </defs>
              {!compact && <CartesianGrid stroke={theme.grid} strokeDasharray="2 8" vertical={false} />}
              <XAxis hide={compact} dataKey="t" tickFormatter={formatTick} axisLine={false} tickLine={false} tick={{ fill: theme.mutedDim, fontSize: 11, fontFamily: "monospace" }} minTickGap={48} />
              <YAxis hide={compact} axisLine={false} tickLine={false} tick={{ fill: theme.mutedDim, fontSize: 11, fontFamily: "monospace" }} tickFormatter={(v) => formatValue(Number(v), unit)} width={compact ? 0 : 56} />
              <Tooltip contentStyle={tooltipStyle} labelFormatter={(ts) => formatTooltipTime(Number(ts))} formatter={(val: number, name: string) => [formatValue(val, unit), name]} />
              {!compact && <Legend align="left" verticalAlign="top" height={24} iconSize={8} iconType="circle" wrapperStyle={{ fontSize: 11, color: theme.muted }} />}
              {visibleKeys.map((key, i) => (
                <Area key={key} type="monotone" dataKey={key} stroke={COLORS[i % COLORS.length]} strokeWidth={compact ? 1.6 : 2} fill={`url(#area-grad-${i})`} connectNulls />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="chart-wrap" style={{ height }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={compact ? { top: 4, right: 4, left: 4, bottom: 2 } : { top: 8, right: 12, left: 0, bottom: 4 }}>
              {!compact && <CartesianGrid stroke={theme.grid} strokeDasharray="2 8" vertical={false} />}
              <XAxis hide={compact} dataKey="t" tickFormatter={formatTick} axisLine={false} tickLine={false} tick={{ fill: theme.mutedDim, fontSize: 11, fontFamily: "monospace" }} minTickGap={48} />
              <YAxis hide={compact} axisLine={false} tickLine={false} tick={{ fill: theme.mutedDim, fontSize: 11, fontFamily: "monospace" }} tickFormatter={(v) => formatValue(Number(v), unit)} width={compact ? 0 : 56} />
              <Tooltip cursor={{ stroke: theme.cursor, strokeWidth: 1 }} contentStyle={tooltipStyle} labelFormatter={(ts) => formatTooltipTime(Number(ts))} formatter={(val: number, name: string) => [formatValue(val, unit), name]} />
              {!compact && <Legend align="left" verticalAlign="top" height={24} iconSize={8} iconType="circle" wrapperStyle={{ fontSize: 11, color: theme.muted }} />}
              {visibleKeys.map((key, i) => (
                <Line key={key} type="monotone" dataKey={key} stroke={COLORS[i % COLORS.length]} strokeWidth={compact ? 1.6 : 2.1} dot={false} activeDot={{ r: 3.5 }} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
