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
  { value: "timeseries", label: "Time Series" },
  { value: "gauge", label: "Gauge" },
  { value: "stat", label: "Stat KPI" },
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
    initialProposal.ir || { name: "Untitled Dashboard", panels: [] }
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
    "1h";
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

  // Remove a panel from the proposal
  const handleRemovePanel = (index: number) => {
    const updatedPanels = panels.filter((_, i) => i !== index);
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
      setSuccessMessage("Proposal approved! Click 'Apply to Grafana' to build it.");
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
    <div
      className="proposal-card-container"
      style={{
        background: "var(--bg-elev, #0d0d0d)",
        border: "1px solid var(--primary-border, rgba(247, 147, 26, 0.35))",
        borderRadius: "12px",
        padding: "18px",
        marginTop: "14px",
        display: "flex",
        flexDirection: "column",
        gap: "14px",
        boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
      }}
    >
      {/* Top Header Bar with editable controls */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: "10px",
          borderBottom: "1px solid var(--border, rgba(255,255,255,0.08))",
          paddingBottom: "12px",
        }}
      >
        <div style={{ flex: 1, minWidth: "240px", display: "flex", alignItems: "center", gap: "8px" }}>
          <span style={{ fontSize: "20px" }}>📊</span>
          <input
            type="text"
            value={ir.name}
            onChange={(e) => handleNameChange(e.target.value)}
            style={{
              background: "rgba(255,255,255,0.05)",
              border: "1px solid rgba(255,255,255,0.12)",
              borderRadius: "6px",
              padding: "6px 10px",
              fontSize: "15px",
              fontWeight: 600,
              color: "#fff",
              flex: 1,
            }}
            title="Edit Dashboard Name"
          />
        </div>

        {/* Global Controls: Time range & Target selector */}
        <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
          <label style={{ display: "flex", alignItems: "center", gap: "5px", fontSize: "11px", color: "var(--muted, #8a8a8a)" }}>
            Time:
            <select
              value={currentTimeRange}
              onChange={(e) => handleTimeRangeChange(e.target.value)}
              style={{
                background: "#161b22",
                border: "1px solid rgba(255,255,255,0.15)",
                color: "#fff",
                borderRadius: "5px",
                padding: "4px 8px",
                fontSize: "12px",
                cursor: "pointer",
              }}
            >
              {TIME_RANGES.map((t) => (
                <option key={t} value={t}>
                  Last {t}
                </option>
              ))}
            </select>
          </label>

          {availableTargets.length > 0 && (
            <label style={{ display: "flex", alignItems: "center", gap: "5px", fontSize: "11px", color: "var(--muted, #8a8a8a)" }}>
              Target:
              <select
                value={currentTarget}
                onChange={(e) => handleTargetChange(e.target.value)}
                style={{
                  background: "#161b22",
                  border: "1px solid rgba(255,255,255,0.15)",
                  color: "#fff",
                  borderRadius: "5px",
                  padding: "4px 8px",
                  fontSize: "12px",
                  cursor: "pointer",
                }}
              >
                {availableTargets.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
          )}

          <span
            style={{
              textTransform: "uppercase",
              fontSize: "10px",
              letterSpacing: "0.06em",
              background:
                proposal.status === "executed"
                  ? "rgba(62, 207, 142, 0.15)"
                  : proposal.status === "approved"
                  ? "rgba(96, 165, 250, 0.15)"
                  : proposal.status === "rejected"
                  ? "rgba(239, 68, 68, 0.15)"
                  : "rgba(247, 147, 26, 0.15)",
              color:
                proposal.status === "executed"
                  ? "#3ecf8e"
                  : proposal.status === "approved"
                  ? "#60a5fa"
                  : proposal.status === "rejected"
                  ? "#ef4444"
                  : "var(--primary, #f7931a)",
              border: "1px solid currentColor",
              padding: "4px 8px",
              borderRadius: "4px",
              fontWeight: 600,
            }}
          >
            {proposal.status} · v{proposal.version}
          </span>
        </div>
      </div>

      {/* Panels Grid */}
      <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span
            style={{
              fontSize: "11px",
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: "0.05em",
              color: "var(--muted, #8a8a8a)",
            }}
          >
            Proposed Panels ({panels.length})
          </span>
          <span style={{ fontSize: "11px", color: "var(--muted, #8a8a8a)" }}>
            Edit titles, visualization type, or resize/remove panels below
          </span>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(12, 1fr)",
            gap: "12px",
          }}
        >
          {panels.map((panel, idx) => {
            const isFullWidth = panel.size?.w === 24;
            const viz = panel.visualizationType || "timeseries";
            return (
              <div
                key={panel.id || idx}
                style={{
                  gridColumn: isFullWidth ? "span 12" : "span 6",
                  background: "rgba(255,255,255,0.02)",
                  border: "1px solid var(--border, rgba(255,255,255,0.08))",
                  borderRadius: "8px",
                  padding: "12px",
                  display: "flex",
                  flexDirection: "column",
                  gap: "8px",
                }}
              >
                {/* Panel Controls: Title, Viz Selector, Resize, Remove */}
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: "8px",
                  }}
                >
                  <input
                    type="text"
                    value={panel.title}
                    onChange={(e) => handlePanelTitleChange(idx, e.target.value)}
                    style={{
                      background: "transparent",
                      border: "none",
                      borderBottom: "1px solid rgba(255,255,255,0.15)",
                      color: "#fff",
                      fontSize: "13px",
                      fontWeight: 600,
                      flex: 1,
                      padding: "2px 4px",
                    }}
                    title="Edit Panel Title"
                  />

                  <select
                    value={viz}
                    onChange={(e) => handleVisualizationChange(idx, e.target.value)}
                    style={{
                      background: "#161b22",
                      border: "1px solid rgba(255,255,255,0.15)",
                      color: "var(--primary, #f7931a)",
                      borderRadius: "4px",
                      padding: "3px 6px",
                      fontSize: "11px",
                      cursor: "pointer",
                    }}
                  >
                    {VIZ_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>

                  <button
                    type="button"
                    onClick={() => handleToggleWidth(idx)}
                    title={isFullWidth ? "Collapse to 50% width" : "Expand to 100% width"}
                    style={{
                      background: "rgba(255,255,255,0.06)",
                      border: "none",
                      color: "#aaa",
                      borderRadius: "4px",
                      padding: "3px 7px",
                      fontSize: "10px",
                      cursor: "pointer",
                    }}
                  >
                    {isFullWidth ? "½" : "Full"}
                  </button>

                  <button
                    type="button"
                    onClick={() => handleRemovePanel(idx)}
                    title="Remove panel"
                    style={{
                      background: "rgba(239, 68, 68, 0.15)",
                      border: "none",
                      color: "#ef4444",
                      borderRadius: "4px",
                      padding: "3px 7px",
                      fontSize: "10px",
                      cursor: "pointer",
                    }}
                  >
                    ✕
                  </button>
                </div>

                {/* Panel Graphical Preview */}
                <div
                  style={{
                    height: "120px",
                    background: "rgba(0,0,0,0.25)",
                    borderRadius: "6px",
                    border: "1px solid rgba(255,255,255,0.04)",
                    overflow: "hidden",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    padding: "8px",
                  }}
                >
                  <PanelLivePreview panel={panel} />
                </div>

                {/* Query and Target metadata */}
                <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
                  {panel.query && (
                    <code
                      style={{
                        fontSize: "10px",
                        color: "var(--muted-dim, #777)",
                        background: "rgba(0,0,0,0.3)",
                        padding: "3px 6px",
                        borderRadius: "3px",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                      title={panel.query}
                    >
                      {panel.query}
                    </code>
                  )}
                  {panel.target?.value && (
                    <span style={{ fontSize: "10px", color: "var(--muted, #666)" }}>
                      Target: {panel.target.label || "instance"} = {panel.target.value}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Messages */}
      {error && (
        <div
          style={{
            fontSize: "12px",
            color: "#ef4444",
            background: "rgba(239, 68, 68, 0.1)",
            padding: "8px 12px",
            borderRadius: "6px",
            border: "1px solid rgba(239, 68, 68, 0.25)",
          }}
        >
          ⚠️ {error}
        </div>
      )}

      {successMessage && (
        <div
          style={{
            fontSize: "12px",
            color: "#3ecf8e",
            background: "rgba(62, 207, 142, 0.1)",
            padding: "8px 12px",
            borderRadius: "6px",
            border: "1px solid rgba(62, 207, 142, 0.25)",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span>✅ {successMessage}</span>
          {dashboardUrl && (
            <a
              href={dashboardUrl}
              target="_blank"
              rel="noreferrer"
              style={{
                color: "#3ecf8e",
                fontWeight: 600,
                textDecoration: "underline",
              }}
            >
              Open Dashboard in Grafana ↗
            </a>
          )}
        </div>
      )}

      {/* Action Buttons */}
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          alignItems: "center",
          gap: "10px",
          marginTop: "6px",
          flexWrap: "wrap",
        }}
      >
        <button
          type="button"
          disabled={saving || loading || proposal.status === "executed"}
          onClick={handleSaveModifications}
          style={{
            fontSize: "12px",
            padding: "6px 14px",
            borderRadius: "6px",
            border: "1px solid rgba(255,255,255,0.15)",
            background: "rgba(255,255,255,0.06)",
            color: "#fff",
            cursor: saving ? "not-allowed" : "pointer",
          }}
        >
          {saving ? "Saving…" : "💾 Save Modifications"}
        </button>

        {proposal.status !== "executed" && proposal.status !== "rejected" && (
          <button
            type="button"
            disabled={loading}
            onClick={handleReject}
            style={{
              fontSize: "12px",
              padding: "6px 12px",
              borderRadius: "6px",
              border: "1px solid rgba(239, 68, 68, 0.3)",
              background: "rgba(239, 68, 68, 0.1)",
              color: "#ef4444",
              cursor: loading ? "not-allowed" : "pointer",
            }}
          >
            Reject
          </button>
        )}

        {proposal.status === "proposed" && (
          <button
            type="button"
            disabled={loading}
            onClick={handleApprove}
            style={{
              fontSize: "12px",
              padding: "6px 16px",
              borderRadius: "6px",
              border: "1px solid rgba(96, 165, 250, 0.4)",
              background: "rgba(96, 165, 250, 0.15)",
              color: "#60a5fa",
              fontWeight: 600,
              cursor: loading ? "not-allowed" : "pointer",
            }}
          >
            {loading ? "Approving…" : "✓ Approve"}
          </button>
        )}

        {(proposal.status === "approved" || proposal.approvalToken) &&
          proposal.status !== "executed" && (
            <button
              type="button"
              disabled={loading}
              onClick={handleExecute}
              style={{
                fontSize: "12px",
                padding: "7px 18px",
                borderRadius: "6px",
                border: "none",
                background: "linear-gradient(135deg, #f7931a, #ea580c)",
                color: "#fff",
                fontWeight: 600,
                cursor: loading ? "not-allowed" : "pointer",
                boxShadow: "0 2px 8px rgba(247, 147, 26, 0.35)",
              }}
            >
              {loading ? "Applying to Grafana…" : "🚀 Apply to Grafana"}
            </button>
          )}

        {proposal.status === "executed" && dashboardUrl && (
          <a
            href={dashboardUrl}
            target="_blank"
            rel="noreferrer"
            style={{
              fontSize: "12px",
              padding: "6px 16px",
              borderRadius: "6px",
              background: "#3ecf8e",
              color: "#0a0a0a",
              fontWeight: 600,
              textDecoration: "none",
              display: "inline-flex",
              alignItems: "center",
              gap: "4px",
            }}
          >
            Open in Grafana ↗
          </a>
        )}
      </div>
    </div>
  );
}

/**
 * Renders an inline SVG preview corresponding to the panel's visualizationType
 */
function PanelLivePreview({ panel }: { panel: PanelIR }) {
  const viz = (panel.visualizationType || "timeseries").toLowerCase();
  const cfg = (panel as any).visualizationConfig || {};
  const queryResult = (panel as any).queryResult;

  // 1. Gauge Preview
  if (viz === "gauge") {
    const rawVal =
      typeof queryResult?.value === "number"
        ? queryResult.value
        : parseFloat(queryResult?.value) || 42.5;
    const min = typeof cfg.min === "number" ? cfg.min : 0;
    const max = typeof cfg.max === "number" ? cfg.max : 100;
    const ratio = Math.max(0, Math.min(1, (rawVal - min) / (max - min || 1)));
    const angle = -90 + ratio * 180;
    const rad = (angle * Math.PI) / 180;
    const nx = 150 + 80 * Math.cos(rad);
    const ny = 115 + 80 * Math.sin(rad);

    return (
      <svg viewBox="0 0 300 140" style={{ width: "100%", height: "100%" }}>
        {/* Background Track */}
        <path
          d="M 50 115 A 100 100 0 0 1 250 115"
          fill="none"
          stroke="#233b55"
          strokeWidth="16"
          strokeLinecap="round"
        />
        {/* Active Value Arc */}
        <path
          d="M 50 115 A 100 100 0 0 1 250 115"
          fill="none"
          stroke="#f7931a"
          strokeWidth="16"
          strokeLinecap="round"
          strokeDasharray={`${ratio * 314} 314`}
        />
        {/* Needle */}
        <line
          x1="150"
          y1="115"
          x2={nx}
          y2={ny}
          stroke="#ffffff"
          strokeWidth="3"
        />
        <circle cx="150" cy="115" r="5" fill="#f7931a" />
        {/* Readout */}
        <text
          x="150"
          y="135"
          textAnchor="middle"
          fill="#ffffff"
          fontSize="18"
          fontWeight="bold"
        >
          {rawVal.toFixed(1)}
          {cfg.unit === "percent" ? "%" : cfg.unit ? ` ${cfg.unit}` : ""}
        </text>
      </svg>
    );
  }

  // 2. Stat KPI Preview
  if (viz === "stat") {
    const rawVal =
      typeof queryResult?.value === "number"
        ? queryResult.value
        : parseFloat(queryResult?.value) || 84.2;
    return (
      <div style={{ textAlign: "center", display: "flex", flexDirection: "column", gap: "2px" }}>
        <span style={{ fontSize: "28px", fontWeight: "bold", color: "#f7931a" }}>
          {rawVal.toFixed(1)}
          {cfg.unit === "percent" ? "%" : cfg.unit ? ` ${cfg.unit}` : ""}
        </span>
        <span style={{ fontSize: "11px", color: "var(--muted, #888)" }}>
          Current Metric Value
        </span>
      </div>
    );
  }

  // 3. Bar Chart Preview
  if (viz === "barchart") {
    return (
      <svg viewBox="0 0 200 80" style={{ width: "90%", height: "90%" }}>
        <line x1="10" y1="75" x2="190" y2="75" stroke="#334155" strokeWidth="1" />
        {[
          { x: 25, h: 45, val: "A" },
          { x: 65, h: 60, val: "B" },
          { x: 105, h: 30, val: "C" },
          { x: 145, h: 50, val: "D" },
        ].map((bar, i) => (
          <rect
            key={i}
            x={bar.x}
            y={75 - bar.h}
            width="22"
            height={bar.h}
            rx="3"
            fill={i % 2 === 0 ? "#f7931a" : "#38bdf8"}
          />
        ))}
      </svg>
    );
  }

  // 4. Table / Logs Preview
  if (viz === "table" || viz === "logs") {
    return (
      <div style={{ width: "100%", fontSize: "10px", color: "#ccc", overflow: "hidden" }}>
        <div style={{ display: "flex", borderBottom: "1px solid #334155", paddingBottom: "3px", fontWeight: 600 }}>
          <span style={{ width: "40%" }}>Time</span>
          <span style={{ width: "60%" }}>Message</span>
        </div>
        <div style={{ display: "flex", paddingTop: "4px", color: "#888" }}>
          <span style={{ width: "40%" }}>17:30:12</span>
          <span style={{ width: "60%" }}>service healthy</span>
        </div>
        <div style={{ display: "flex", paddingTop: "2px", color: "#888" }}>
          <span style={{ width: "40%" }}>17:31:00</span>
          <span style={{ width: "60%" }}>heartbeat ok</span>
        </div>
      </div>
    );
  }

  // 5. Default Time Series (Sparkline / Line chart)
  return (
    <svg viewBox="0 0 240 80" style={{ width: "100%", height: "100%" }}>
      {/* Grid lines */}
      <line x1="10" y1="20" x2="230" y2="20" stroke="#1e293b" strokeWidth="1" />
      <line x1="10" y1="45" x2="230" y2="45" stroke="#1e293b" strokeWidth="1" />
      <line x1="10" y1="70" x2="230" y2="70" stroke="#334155" strokeWidth="1" />
      {/* Wave Path */}
      <path
        d="M 10 55 Q 40 25, 70 48 T 130 30 T 180 50 T 230 25"
        fill="none"
        stroke="#38bdf8"
        strokeWidth="2.5"
      />
      {/* Glow fill */}
      <path
        d="M 10 55 Q 40 25, 70 48 T 130 30 T 180 50 T 230 25 L 230 70 L 10 70 Z"
        fill="rgba(56, 189, 248, 0.08)"
      />
    </svg>
  );
}
