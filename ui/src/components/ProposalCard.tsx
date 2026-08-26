import { useState } from "react";
import type { DashboardProposal, PanelIR } from "../types";
import { approveProposal, executeProposal, rejectProposal } from "../api";

type Props = {
  proposal: DashboardProposal;
};

export default function ProposalCard({ proposal: initialProposal }: Props) {
  const [proposal, setProposal] = useState<DashboardProposal>(initialProposal);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const ir = proposal.ir || { name: "Untitled Dashboard", panels: [] };
  const panels: PanelIR[] = ir.panels || [];

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
    } catch (err: any) {
      setError(err?.message || "Failed to approve proposal");
    } finally {
      setLoading(false);
    }
  };

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

  const handleExecute = async () => {
    if (!proposal.approvalToken) {
      setError("No approval token found.");
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
        const dashUrl = (res as any)?.grafanaResult?.url;
        setSuccessMessage(
          `Dashboard "${ir.name}" successfully created in Grafana! ${dashUrl ? `(http://localhost:3000${dashUrl})` : ""}`
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
        border: "1px solid var(--primary-border, rgba(247, 147, 26, 0.3))",
        borderRadius: "10px",
        padding: "16px",
        marginTop: "12px",
        display: "flex",
        flexDirection: "column",
        gap: "12px",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          borderBottom: "1px solid var(--border, rgba(255,255,255,0.08))",
          paddingBottom: "10px",
        }}
      >
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <span style={{ fontSize: "16px" }}>📊</span>
            <span
              style={{
                fontSize: "15px",
                fontWeight: 600,
                color: "var(--text, #fff)",
              }}
            >
              {ir.name}
            </span>
          </div>
          {ir.description && (
            <p
              style={{
                fontSize: "12px",
                color: "var(--muted, #8a8a8a)",
                margin: "4px 0 0 0",
              }}
            >
              {ir.description}
            </p>
          )}
        </div>

        <span
          className="badge"
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
            padding: "3px 8px",
            borderRadius: "4px",
          }}
        >
          {proposal.status}
        </span>
      </div>

      {/* Panels List */}
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
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

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
            gap: "8px",
          }}
        >
          {panels.map((panel, idx) => (
            <div
              key={panel.id || idx}
              style={{
                background: "rgba(255,255,255,0.02)",
                border: "1px solid var(--border, rgba(255,255,255,0.06))",
                borderRadius: "6px",
                padding: "10px",
                display: "flex",
                flexDirection: "column",
                gap: "4px",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    fontSize: "12px",
                    fontWeight: 500,
                    color: "var(--text, #fff)",
                  }}
                >
                  {panel.title}
                </span>
                <span
                  style={{
                    fontSize: "10px",
                    padding: "1px 5px",
                    borderRadius: "3px",
                    background: "rgba(255,255,255,0.06)",
                    color: "var(--muted, #8a8a8a)",
                    textTransform: "capitalize",
                  }}
                >
                  {panel.visualizationType || "timeseries"}
                </span>
              </div>
              {panel.query && (
                <code
                  style={{
                    fontSize: "10px",
                    color: "var(--muted-dim, #666)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={panel.query}
                >
                  {panel.query}
                </code>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Messages */}
      {error && (
        <div
          style={{
            fontSize: "12px",
            color: "#ef4444",
            background: "rgba(239, 68, 68, 0.1)",
            padding: "6px 10px",
            borderRadius: "4px",
            border: "1px solid rgba(239, 68, 68, 0.2)",
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
            padding: "6px 10px",
            borderRadius: "4px",
            border: "1px solid rgba(62, 207, 142, 0.2)",
          }}
        >
          ✅ {successMessage}
        </div>
      )}

      {/* Action Buttons */}
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          gap: "8px",
          marginTop: "4px",
        }}
      >
        {proposal.status === "proposed" && (
          <>
            <button
              type="button"
              disabled={loading}
              onClick={handleReject}
              style={{
                fontSize: "12px",
                padding: "6px 12px",
                borderRadius: "5px",
                border: "1px solid var(--border, rgba(255,255,255,0.1))",
                background: "transparent",
                color: "var(--muted, #8a8a8a)",
                cursor: "pointer",
              }}
            >
              ✕ Reject
            </button>
            <button
              type="button"
              disabled={loading}
              onClick={handleApprove}
              style={{
                fontSize: "12px",
                padding: "6px 14px",
                borderRadius: "5px",
                border: "none",
                background: "var(--primary, #f7931a)",
                color: "#000",
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              {loading ? "Approving..." : "✓ Approve Proposal"}
            </button>
          </>
        )}

        {proposal.status === "approved" && (
          <button
            type="button"
            disabled={loading}
            onClick={handleExecute}
            style={{
              fontSize: "12px",
              padding: "6px 14px",
              borderRadius: "5px",
              border: "none",
              background: "#3ecf8e",
              color: "#000",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {loading ? "Applying to Grafana..." : "🚀 Apply to Grafana"}
          </button>
        )}

        {proposal.status === "executed" && (
          <span
            style={{
              fontSize: "12px",
              color: "#3ecf8e",
              fontWeight: 500,
            }}
          >
            ✓ Applied to Grafana
          </span>
        )}
      </div>
    </div>
  );
}
