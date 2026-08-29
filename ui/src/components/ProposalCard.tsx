import { useState } from "react";
import type { DashboardProposal, PanelIR } from "../types";
import {
  approveProposal,
  executeProposal,
  modifyProposal,
  previewProposal,
  rejectProposal,
} from "../api";

type Props = {
  proposal: DashboardProposal;
};

const VIZ_OPTIONS = [
  { value: "gauge", label: "Gauge" },
  { value: "timeseries", label: "Time Series" },
  { value: "stat", label: "Stat" },
  { value: "barchart", label: "Bar Chart" },
  { value: "table", label: "Table" },
  { value: "logs", label: "Logs" },
  { value: "piechart", label: "Pie Chart" },
  { value: "histogram", label: "Histogram" },
  { value: "heatmap", label: "Heatmap" },
];

const TIME_RANGES = ["15m", "1h", "6h", "12h", "24h", "7d"];

export default function ProposalCard({ proposal: initialProposal }: Props) {
  const [proposal, setProposal] = useState<DashboardProposal>(initialProposal);
  const [ir, setIr] = useState<any>(
    initialProposal.ir || { name: "Observability Dashboard", panels: [] }
  );
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [dashboardUrl, setDashboardUrl] = useState<string | null>(null);

  const panels: PanelIR[] = ir.panels || [];
  const availableTargets: string[] = ir.availableTargets || [];
  const currentTimeRange =
    ir.timeConfig?.from?.replace(/^now-/, "") ||
    ir.timeConfig?.range ||
    "24h";
  const currentTarget =
    panels[0]?.target?.value ||
    (availableTargets.length > 0 ? availableTargets[0] : "");

  // Update dashboard name
  const handleNameChange = (name: string) => {
    setIr((prev: any) => ({ ...prev, name }));
  };

  // Change time range across all panels and refresh previews
  const handleTimeRangeChange = async (timeRange: string) => {
    const updatedIr = {
      ...ir,
      timeConfig: {
        ...ir.timeConfig,
        from: `now-${timeRange}`,
        to: "now",
      },
    };
    setIr(updatedIr);
    await triggerPreviewRefresh(updatedIr);
  };

  // Change target host across all panels and refresh previews
  const handleTargetChange = async (target: string) => {
    const updatedPanels = (ir.panels || []).map((panel: PanelIR) => {
      const p = { ...panel };
      if (p.target?.label) {
        const label = p.target.label;
        p.target = { ...p.target, value: target };
        if (p.query) {
          p.query = p.query.replace(
            new RegExp(`${label}="[^"]+"`, "g"),
            `${label}="${target}"`
          );
        }
      }
      return p;
    });
    const updatedIr = { ...ir, panels: updatedPanels };
    setIr(updatedIr);
    await triggerPreviewRefresh(updatedIr);
  };

  // Update panel title
  const handlePanelTitleChange = (index: number, title: string) => {
    const updatedPanels = [...panels];
    updatedPanels[index] = { ...updatedPanels[index], title };
    setIr({ ...ir, panels: updatedPanels });
  };

  // Change visualization type on a panel
  const handleVisualizationChange = (index: number, vizType: string) => {
    const updatedPanels = [...panels];
    updatedPanels[index] = {
      ...updatedPanels[index],
      visualizationType: vizType,
    };
    setIr({ ...ir, panels: updatedPanels });
  };

  // Remove a panel from the proposal
  const handleRemovePanel = (index: number) => {
    const updatedPanels = panels.filter((_, i) => i !== index);
    setIr({ ...ir, panels: updatedPanels });
  };

  // Keep only one specific panel (select 1 out of 3)
  const handleKeepOnlyPanel = (index: number) => {
    const updatedPanels = [panels[index]];
    setIr({ ...ir, panels: updatedPanels });
  };

  // Toggle panel width between half (12) and full (24)
  const handleToggleWidth = (index: number) => {
    const updatedPanels = [...panels];
    const currentW = updatedPanels[index].size?.w || 12;
    const newW = currentW === 24 ? 12 : 24;
    updatedPanels[index] = {
      ...updatedPanels[index],
      size: { ...updatedPanels[index].size, w: newW },
    };
    setIr({ ...ir, panels: updatedPanels });
  };

  // Refresh live preview for all panels
  const triggerPreviewRefresh = async (currentIr: any) => {
    try {
      const panelIds = (currentIr.panels || []).map(
        (p: PanelIR, i: number) => p.id || `panel-${i + 1}`
      );
      const res = await previewProposal(proposal.proposalId, currentIr, panelIds);
      if (res?.ir) {
        setIr(res.ir);
      } else if (res?.panels) {
        const mergedPanels = (currentIr.panels || []).map((panel: PanelIR) => {
          const fresh = res.panels.find(
            (item: any) => String(item.id) === String(panel.id)
          );
          return fresh || panel;
        });
        setIr({ ...currentIr, panels: mergedPanels });
      }
    } catch {
      // Best-effort live preview refresh
    }
  };

  // Save modified IR back to server
  const handleSaveModifications = async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await modifyProposal(proposal.proposalId, ir);
      setProposal((prev) => ({
        ...prev,
        status: res?.status || "proposed",
        version: res?.version || prev.version + 1,
        approvalToken: undefined,
      }));
      setSuccessMessage("Modifications saved as new proposal version!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: any) {
      setError(err?.message || "Failed to save modifications");
    } finally {
      setSaving(false);
    }
  };

  // Approve proposal
  const handleApprove = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await approveProposal(proposal.proposalId, proposal.version);
      setProposal((prev) => ({
        ...prev,
        status: "approved",
        approvalToken: res.approvalToken,
        version: res.version,
      }));
      setSuccessMessage("Proposal approved! Click 'Execute / Apply' to build it.");
    } catch (err: any) {
      setError(err?.message || "Failed to approve proposal");
    } finally {
      setLoading(false);
    }
  };

  // Reject proposal
  const handleReject = async () => {
    setLoading(true);
    setError(null);
    try {
      await rejectProposal(proposal.proposalId, proposal.version);
      setProposal((prev) => ({
        ...prev,
        status: "rejected",
      }));
    } catch (err: any) {
      setError(err?.message || "Failed to reject proposal");
    } finally {
      setLoading(false);
    }
  };

  // Execute and apply to Grafana
  const handleExecute = async () => {
    if (!proposal.approvalToken) {
      setError("Proposal must be approved first.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await executeProposal(
        proposal.proposalId,
        proposal.version,
        proposal.approvalToken
      );
      const isSuccess =
        res.status === "success" ||
        res.status === "executed" ||
        (res as any)?.grafanaResult?.status === "success" ||
        (res as any)?.proposal?.status === "built";

      if (isSuccess) {
        setProposal((prev) => ({ ...prev, status: "executed" }));
        const rawUrl =
          (res as any)?.dashboardUrl ||
          (res as any)?.url ||
          (res as any)?.grafanaResult?.url;
        const fullUrl = rawUrl
          ? rawUrl.startsWith("http")
            ? rawUrl
            : `http://localhost:3000${rawUrl}`
          : null;
        setDashboardUrl(fullUrl);
        setSuccessMessage(
          `Dashboard "${ir.name}" successfully created in Grafana!`
        );
      } else {
        setError(res.error || "Execution failed. Check Grafana MCP connection.");
      }
    } catch (err: any) {
      setError(err?.message || "Failed to apply dashboard to Grafana");
    } finally {
      setLoading(false);
    }
  };

  return (
    <section
      className="proposal-card"
      data-proposal={proposal.proposalId}
    >
      {/* Top Header Bar */}
      <div className="proposal-top">
        <input
          className="proposal-name-input"
          type="text"
          value={ir.name}
          onChange={(e) => handleNameChange(e.target.value)}
          title="Edit Dashboard Name"
        />

        <label style={{ display: "grid", gap: "2px", color: "var(--muted)", fontSize: "0.72rem", fontWeight: 600 }}>
          Time range
          <select
            value={currentTimeRange}
            onChange={(e) => handleTimeRangeChange(e.target.value)}
            style={{ padding: "5px 8px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--text)" }}
          >
            {TIME_RANGES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>

        {availableTargets.length > 0 && (
          <label style={{ display: "grid", gap: "2px", color: "var(--muted)", fontSize: "0.72rem", fontWeight: 600 }}>
            Target
            <select
              value={currentTarget}
              onChange={(e) => handleTargetChange(e.target.value)}
              style={{ padding: "5px 8px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--text)" }}
            >
              {availableTargets.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </label>
        )}

        <span
          className={`badge ${
            proposal.status === "executed"
              ? "badge-green"
              : proposal.status === "rejected"
              ? ""
              : "badge-violet"
          }`}
          style={{ marginLeft: "auto" }}
        >
          {proposal.status} · v{proposal.version}
        </span>
      </div>

      {/* Multiple Options Chooser */}
      {panels.length > 1 && (
        <div className="proposal-filter-bar">
          <span style={{ fontWeight: 600, color: "var(--text)" }}>Quick Filter:</span>
          <span style={{ color: "var(--muted)" }}>
            Select one to focus, or keep all:
          </span>
          {panels.map((p, i) => (
            <button
              key={p.id || i}
              type="button"
              className="btn quiet"
              style={{ fontSize: "11px", padding: "2px 8px", color: "var(--primary)" }}
              onClick={() => handleKeepOnlyPanel(i)}
              title={`Keep only ${p.title} panel`}
            >
              Only {p.title}
            </button>
          ))}
        </div>
      )}

      {/* Panels Grid */}
      <div className="proposal-grid">
        {panels.map((panel, idx) => {
          const viz = (panel.visualizationType || "gauge").toLowerCase();
          return (
            <article key={panel.id || idx} className="proposal-panel">
              {/* Panel Header */}
              <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
                <input
                  type="text"
                  value={panel.title}
                  onChange={(e) => handlePanelTitleChange(idx, e.target.value)}
                  style={{ flex: 1, fontWeight: 700, fontSize: "0.9rem", padding: "5px 8px", background: "var(--bg-soft)", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", color: "var(--text)" }}
                  title="Edit Panel Title"
                />

                <button
                  type="button"
                  className="btn quiet"
                  style={{ color: "var(--danger)", fontSize: "11px", padding: "4px 8px" }}
                  onClick={() => handleRemovePanel(idx)}
                >
                  Remove
                </button>

                <label style={{ display: "grid", gap: "2px", color: "var(--muted)", fontSize: "0.72rem", fontWeight: 600 }}>
                  Viz
                  <select
                    value={viz}
                    onChange={(e) =>
                      handleVisualizationChange(idx, e.target.value)
                    }
                    style={{ padding: "4px 8px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border)", background: "var(--bg-soft)", color: "var(--text)", fontSize: "0.8rem" }}
                  >
                    {VIZ_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              {/* Graphical Preview Box */}
              <div className="proposal-preview">
                <RenderExactPreview panel={panel} />
              </div>

              {/* Metric and Target info footer */}
              <div style={{ display: "grid", gap: "4px", color: "var(--muted)", fontSize: "0.78rem" }}>
                {panel.metric && (
                  <div>
                    <span style={{ color: "var(--muted)" }}>Metric</span>
                    <b style={{ color: "var(--text)", display: "block", marginTop: "2px" }}>
                      {panel.metric}
                    </b>
                  </div>
                )}
                {panel.query && (
                  <details>
                    <summary style={{ cursor: "pointer", color: "var(--primary)", fontWeight: 600 }}>Query</summary>
                    <code style={{ display: "block", whiteSpace: "pre-wrap", color: "var(--code)", padding: "6px 8px", background: "var(--bg-soft)", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", marginTop: "4px", overflowWrap: "anywhere" }}>
                      {panel.query}
                    </code>
                  </details>
                )}
              </div>
            </article>
          );
        })}
      </div>

      {/* Feedback Messages */}
      {error && (
        <div style={{ color: "var(--danger)", background: "var(--danger-soft)", border: "1px solid rgba(240, 113, 120, 0.3)", padding: "8px 12px", borderRadius: "var(--radius-sm)", fontSize: "0.84rem" }}>
          ⚠️ {error}
        </div>
      )}

      {successMessage && (
        <div style={{ color: "var(--green)", background: "var(--green-soft)", border: "1px solid rgba(62, 207, 142, 0.3)", padding: "8px 12px", borderRadius: "var(--radius-sm)", fontSize: "0.84rem", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>✅ {successMessage}</span>
          {dashboardUrl && (
            <a
              href={dashboardUrl}
              target="_blank"
              rel="noreferrer"
              style={{
                color: "var(--green)",
                fontWeight: 700,
                textDecoration: "underline",
              }}
            >
              Open dashboard ↗
            </a>
          )}
        </div>
      )}

      {/* Action Buttons */}
      <div className="proposal-actions">
        <button
          type="button"
          className="btn"
          disabled={saving || loading || proposal.status === "executed"}
          onClick={handleSaveModifications}
        >
          {saving ? "Saving…" : "Save modifications"}
        </button>

        <button
          type="button"
          className="btn"
          disabled={loading || proposal.status !== "proposed"}
          onClick={handleApprove}
          style={{
            background: "var(--green-soft)",
            color: "var(--green)",
            borderColor: "rgba(62, 207, 142, 0.35)",
            opacity: proposal.status !== "proposed" ? 0.45 : 1,
          }}
        >
          {loading ? "Approving…" : "Approve"}
        </button>

        <button
          type="button"
          className="btn primary"
          disabled={loading || proposal.status !== "approved"}
          onClick={handleExecute}
          style={{
            opacity: proposal.status !== "approved" ? 0.45 : 1,
          }}
        >
          {loading ? "Executing…" : "Execute / Apply"}
        </button>

        {proposal.status !== "executed" && (
          <button
            type="button"
            className="btn quiet"
            disabled={loading}
            onClick={handleReject}
          >
            Reject
          </button>
        )}

        {proposal.status === "executed" && dashboardUrl && (
          <a
            href={dashboardUrl}
            target="_blank"
            rel="noreferrer"
            className="btn quiet"
            style={{ color: "var(--green)", background: "var(--green-soft)", border: "1px solid rgba(62, 207, 142, 0.4)" }}
          >
            Open dashboard ↗
          </a>
        )}
      </div>
    </section>
  );
}

/**
 * Renders SVG visualization themed with the Grafana application palette
 */
function RenderExactPreview({ panel }: { panel: PanelIR }) {
  const viz = (panel.visualizationType || "gauge").toLowerCase();
  const cfg = (panel as any).visualizationConfig || {};
  const queryResult = (panel as any).queryResult;

  // 1. Radial SVG Gauge
  if (viz === "gauge") {
    let rawVal = 0;
    if (typeof queryResult?.value === "number") {
      rawVal = queryResult.value;
    } else if (queryResult?.value) {
      rawVal = parseFloat(queryResult.value) || 0;
    } else if (queryResult?.series?.[0]?.points?.length) {
      const pts = queryResult.series[0].points;
      rawVal = pts[pts.length - 1]?.value ?? 0;
    }

    const min = typeof cfg.min === "number" ? cfg.min : 0;
    const max = typeof cfg.max === "number" ? cfg.max : 100;
    const ratio = Math.max(0, Math.min(1, (rawVal - min) / (max - min || 1)));
    const angle = -90 + ratio * 180;
    const rad = (angle * Math.PI) / 180;
    const x = 150 + 105 * Math.cos(rad);
    const y = 135 + 105 * Math.sin(rad);

    const display = `${Math.round(rawVal)}${cfg.unit === "percent" || !cfg.unit ? "%" : ` ${cfg.unit}`}`;

    return (
      <svg
        className="gauge"
        viewBox="0 0 300 175"
        style={{ width: "100%", height: "100%" }}
      >
        {/* Title above gauge */}
        <text
          x="150"
          y="42"
          textAnchor="middle"
          fill="var(--muted)"
          fontSize="13"
          fontWeight="600"
        >
          {panel.title}
        </text>

        {/* Background Track Arc */}
        <path
          d="M45 135 A105 105 0 0 1 255 135"
          fill="none"
          stroke="var(--border-strong)"
          strokeWidth="18"
          strokeLinecap="round"
        />

        {/* Active Amount Arc */}
        <path
          d="M45 135 A105 105 0 0 1 255 135"
          fill="none"
          stroke="var(--primary)"
          strokeWidth="18"
          strokeLinecap="round"
          pathLength="100"
          strokeDasharray={`${ratio * 100} 100`}
        />

        {/* Needle */}
        <line
          x1="150"
          y1="135"
          x2={x}
          y2={y}
          stroke="var(--text)"
          strokeWidth="3"
          strokeLinecap="round"
        />
        <circle cx="150" cy="135" r="4" fill="var(--primary)" />

        {/* Big centered numeric readout */}
        <text
          x="150"
          y="165"
          textAnchor="middle"
          fill="var(--text)"
          fontSize="22"
          fontWeight="bold"
        >
          {display}
        </text>
      </svg>
    );
  }

  // 2. Time Series SVG Chart
  if (viz === "timeseries") {
    return (
      <svg
        className="chart"
        viewBox="0 0 600 210"
        style={{ width: "100%", height: "100%" }}
      >
        <line x1="34" y1="178" x2="566" y2="178" stroke="var(--border)" />
        <line x1="34" y1="38" x2="34" y2="178" stroke="var(--border)" />
        <line x1="34" y1="108" x2="566" y2="108" stroke="var(--border)" strokeDasharray="4" />
        {/* Wave path */}
        <path
          d="M 34 140 Q 150 70, 250 120 T 420 90 T 566 115"
          fill="none"
          stroke="var(--primary)"
          strokeWidth="3"
        />
        <path
          d="M 34 140 Q 150 70, 250 120 T 420 90 T 566 115 L 566 178 L 34 178 Z"
          fill="var(--primary-soft)"
        />
        {/* Sample points */}
        <circle cx="34" cy="140" r="5" fill="var(--primary)" />
        <circle cx="250" cy="120" r="5" fill="var(--primary)" />
        <circle cx="420" cy="90" r="5" fill="var(--primary)" />
        <circle cx="566" cy="115" r="5" fill="var(--primary)" />
        <text x="38" y="25" fill="var(--text)" fontSize="13" fontWeight="bold">
          Latest {panel.title}
        </text>
      </svg>
    );
  }

  // 3. Stat KPI
  if (viz === "stat") {
    return (
      <div
        className="stat"
        style={{ textAlign: "center", display: "grid", gap: "4px" }}
      >
        <strong style={{ fontSize: "3.8rem", color: "var(--primary)" }}>
          {panel.title.includes("CPU") ? "12.4%" : "48.2%"}
        </strong>
        <span style={{ color: "var(--muted)", fontSize: "0.9rem" }}>
          Current live result
        </span>
      </div>
    );
  }

  // 4. Bar Chart
  if (viz === "barchart") {
    return (
      <div
        className="bars"
        style={{
          width: "92%",
          height: "80%",
          display: "flex",
          alignItems: "flex-end",
          gap: "8px",
          borderBottom: "1px solid var(--border)",
          paddingBottom: "4px",
        }}
      >
        {[40, 65, 30, 85, 50, 75, 45, 90].map((h, i) => (
          <i
            key={i}
            style={{
              flex: 1,
              background: i % 2 === 0 ? "var(--primary)" : "var(--warn)",
              height: `${h}%`,
              borderRadius: "4px 4px 0 0",
              minWidth: "8px",
            }}
          />
        ))}
      </div>
    );
  }

  // 5. Table / Logs
  return (
    <div
      className="table"
      style={{
        width: "100%",
        height: "100%",
        overflow: "auto",
        padding: "10px",
      }}
    >
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.78rem",
          color: "var(--text)",
        }}
      >
        <thead>
          <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--muted)" }}>
            <th style={{ padding: "8px", textAlign: "left" }}>Timestamp</th>
            <th style={{ padding: "8px", textAlign: "left" }}>Instance</th>
            <th style={{ padding: "8px", textAlign: "left" }}>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr style={{ borderBottom: "1px solid var(--border)" }}>
            <td style={{ padding: "8px" }}>now-5m</td>
            <td style={{ padding: "8px" }}>node-exporter:9100</td>
            <td style={{ padding: "8px", color: "var(--green)" }}>healthy</td>
          </tr>
          <tr>
            <td style={{ padding: "8px" }}>now-1m</td>
            <td style={{ padding: "8px" }}>cadvisor:8080</td>
            <td style={{ padding: "8px", color: "var(--green)" }}>healthy</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
