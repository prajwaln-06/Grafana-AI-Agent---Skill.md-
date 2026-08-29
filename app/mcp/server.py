"""Protocol-level MCP interactive proposal resource and tools.

The HTML resource is associated with proposal tools through tool metadata. An
MCP host that supports interactive UI resources can render the resource and
invoke these tools. Grafana MCP remains a separate backend server.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.grafana_tools.dashboard_writing import PROPOSALS, build_proposal, execute_approved_mutation

UI_URI = "ui://hpe-grafana/dashboard-proposal"
ASSETS = Path(__file__).with_name("assets")
UI_META = {"ui/resourceUri": UI_URI, "openai/outputTemplate": UI_URI}

mcp = FastMCP(
    "HPE Grafana Dashboard Proposal App",
    instructions="Interactive Dashboard IR proposal review. Mutations require backend approval verification.",
)


@mcp.resource(UI_URI, name="dashboard-proposal-ui", mime_type="text/html")
def dashboard_proposal_ui() -> str:
    return (ASSETS / "proposal.html").read_text(encoding="utf-8")


@mcp.tool(meta=UI_META, structured_output=True)
async def create_dashboard_proposal(request: str, target: str = "", time_range: str = "1h") -> dict[str, Any]:
    """Create a READ-safe, versioned CREATE/UPDATE/REMOVE Dashboard IR proposal."""
    return await build_proposal(request, target or None, time_range)


@mcp.tool(meta=UI_META, structured_output=True)
def get_dashboard_proposal(proposal_id: str) -> dict[str, Any]:
    return PROPOSALS.get(proposal_id)


@mcp.tool(meta=UI_META, structured_output=True)
def modify_dashboard_proposal(proposal_id: str, ir_json: str) -> dict[str, Any]:
    """Save edited Dashboard IR as a new version and invalidate approval."""
    return PROPOSALS.modify(proposal_id, json.loads(ir_json))


@mcp.tool(meta=UI_META, structured_output=True)
def approve_dashboard_proposal(proposal_id: str, version: int) -> dict[str, Any]:
    return PROPOSALS.approve(proposal_id, version)


@mcp.tool(meta=UI_META, structured_output=True)
def reject_dashboard_proposal(proposal_id: str, version: int) -> dict[str, Any]:
    return PROPOSALS.reject(proposal_id, version)


@mcp.tool(meta=UI_META, structured_output=True)
async def execute_dashboard_proposal(proposal_id: str, version: int, approval_token: str) -> dict[str, Any]:
    """Execute only an already approved exact proposal version."""
    return await execute_approved_mutation(proposal_id, version, approval_token)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
