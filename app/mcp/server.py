"""Protocol-level MCP interactive proposal resource and tools.

The HTML resource is associated with proposal tools through tool metadata. An
MCP host that supports interactive UI resources can render the resource and
invoke these tools. Grafana MCP remains a separate backend server.

The proposal tools are subject to exactly the same authorization rules as
the HTTP API: the role check, the resource-scope check and the
separation-of-duties rule all run here, against the same policy functions
and the same proposal store. An MCP client cannot approve a proposal it is
not allowed to approve, and it cannot approve its own destructive proposal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:  # MCP 1.x
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - MCP 2.x renamed FastMCP
    from mcp.server.mcpserver import MCPServer as FastMCP

from app.auth.policy import (
    authorize_dashboard_approve,
    authorize_dashboard_propose,
    authorize_proposal_rejection,
)
from app.auth.principal import Principal
from app.auth.resource_scope import authorize_proposal_scope, require_active_principal
from app.grafana_tools.dashboard_writing import PROPOSALS, build_proposal, execute_approved_mutation

UI_URI = "ui://hpe-grafana/dashboard-proposal"
ASSETS = Path(__file__).with_name("assets")
UI_META = {"ui/resourceUri": UI_URI, "openai/outputTemplate": UI_URI}

mcp = FastMCP(
    "HPE Grafana Dashboard Proposal App",
    instructions="Interactive Dashboard IR proposal review. Mutations require backend approval verification.",
)


def _authenticated_principal() -> Principal:
    """Return the principal bound to the current context, or refuse.

    This MCP server shares the application's authorization model: a proposal
    may only be built or executed for an authenticated, authorized identity.
    Without one the tools refuse to act rather than running with the Grafana
    service account's full privileges.

    The identity is re-resolved from the role store, so a revoked role, a
    revoked scope or a deactivated account stops the tool as well.
    """
    return require_active_principal()


def _denied(reason: str) -> PermissionError:
    """Build the error an unauthorized MCP tool call raises."""
    return PermissionError(f"RBAC policy denied: {reason}")


@mcp.resource(UI_URI, name="dashboard-proposal-ui", mime_type="text/html")
def dashboard_proposal_ui() -> str:
    return (ASSETS / "proposal.html").read_text(encoding="utf-8")


@mcp.tool(meta=UI_META, structured_output=True)
async def create_dashboard_proposal(request: str, target: str = "", time_range: str = "1h") -> dict[str, Any]:
    """Create a READ-safe, versioned CREATE/UPDATE/REMOVE Dashboard IR proposal."""
    _authenticated_principal()
    return await build_proposal(request, target or None, time_range)


@mcp.tool(meta=UI_META, structured_output=True)
async def get_dashboard_proposal(proposal_id: str) -> dict[str, Any]:
    principal = _authenticated_principal()
    proposal = PROPOSALS.get(proposal_id)
    # Reading a proposal must not disclose a resource outside the caller's
    # scope, so the target is checked before the proposal is returned.
    decision = await authorize_proposal_scope(principal, proposal)
    if not decision.allowed:
        raise _denied(decision.reason)
    return proposal


@mcp.tool(meta=UI_META, structured_output=True)
async def modify_dashboard_proposal(proposal_id: str, ir_json: str) -> dict[str, Any]:
    """Save edited Dashboard IR as a new version and invalidate approval."""
    principal = _authenticated_principal()
    ir = json.loads(ir_json)
    existing = PROPOSALS.get(proposal_id)
    decision = await authorize_proposal_scope(principal, existing)
    if not decision.allowed:
        raise _denied(decision.reason)
    operation = str((ir or {}).get("operation") or "update")
    propose_decision = authorize_dashboard_propose(
        principal,
        operation=operation,
        dashboard_uid=(ir or {}).get("dashboardUid"),
        folder_uid=(ir or {}).get("folderUid"),
    )
    if not propose_decision.allowed:
        raise _denied(propose_decision.reason)
    return PROPOSALS.modify(proposal_id, ir)


@mcp.tool(meta=UI_META, structured_output=True)
async def approve_dashboard_proposal(proposal_id: str, version: int) -> dict[str, Any]:
    principal = _authenticated_principal()
    proposal = PROPOSALS.get(proposal_id)
    # The same policy the HTTP API applies: role, scope and the
    # separation-of-duties rule.
    decision = authorize_dashboard_approve(principal, proposal=proposal)
    if not decision.allowed:
        raise _denied(decision.reason)
    scope_decision = await authorize_proposal_scope(principal, proposal)
    if not scope_decision.allowed:
        raise _denied(scope_decision.reason)
    return PROPOSALS.approve(
        proposal_id,
        version,
        approver_id=principal.user_id,
        approver_role=(principal.role or "").lower(),
    )


@mcp.tool(meta=UI_META, structured_output=True)
async def reject_dashboard_proposal(proposal_id: str, version: int) -> dict[str, Any]:
    principal = _authenticated_principal()
    proposal = PROPOSALS.get(proposal_id)
    decision = authorize_proposal_rejection(principal, proposal=proposal)
    if not decision.allowed:
        raise _denied(decision.reason)
    scope_decision = await authorize_proposal_scope(principal, proposal)
    if not scope_decision.allowed:
        raise _denied(scope_decision.reason)
    return PROPOSALS.reject(
        proposal_id,
        version,
        approver_id=principal.user_id,
        approver_role=(principal.role or "").lower(),
    )


@mcp.tool(meta=UI_META, structured_output=True)
async def execute_dashboard_proposal(proposal_id: str, version: int, approval_token: str) -> dict[str, Any]:
    """Execute only an already approved exact proposal version."""
    _authenticated_principal()
    return await execute_approved_mutation(proposal_id, version, approval_token)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
