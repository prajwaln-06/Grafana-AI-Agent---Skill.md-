"""
app/auth/policy.py

Central Policy Module enforcing Section 8 (Resource-scope enforcement,
Section 8.3 Policy Decisions, and Section 8.4 Impact Classification) and
Section 12 (Prometheus Query Gateway RBAC controls).

Enforces strict role boundaries:
- admin: Full administrative, mutation, query, and approval privileges.
- maintainer: Dashboard & panel proposals, approvals, executions, and custom PromQL.
- operator: Alert rule proposals, approvals, executions, and custom PromQL.
- observer: Strictly read-only. Cannot execute Prometheus queries or custom PromQL,
  cannot create, propose, modify, approve, or execute dashboards, panels, or alert rules.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Mapping

from app.auth.principal import Principal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of an authorization evaluation by the Policy Module."""
    allowed: bool
    reason: str
    impact_class: str = "routine_edit"
    requires_second_approver: bool = False

    def __bool__(self) -> bool:
        return self.allowed


# ---------------------------------------------------------------------------
# Scope Evaluation (Section 8.2)
# ---------------------------------------------------------------------------

def is_dashboard_in_scope(
    principal: Principal,
    dashboard_uid: str | None = None,
    folder_uid: str | None = None,
) -> bool:
    """Evaluate resource scope for a dashboard per Section 8.2 formula.
    
    Precedence:
    1. Explicit Deny: denied_dashboard_uids wins over every allowance.
    2. Allowed Dashboard: allowed_dashboard_uids grants access to specific dashboard.
    3. Wildcard Folder: '*' in allowed_folder_uids grants access across all folders.
    4. Allowed Folder: folder_uid in allowed_folder_uids grants access.
    """
    if dashboard_uid and dashboard_uid in principal.denied_dashboard_uids:
        return False

    if dashboard_uid and dashboard_uid in principal.allowed_dashboard_uids:
        return True

    if "*" in principal.allowed_folder_uids:
        return True

    if folder_uid and folder_uid in principal.allowed_folder_uids:
        return True

    # If neither dashboard nor folder was specified or matched, deny by default
    return False


def is_environment_in_scope(principal: Principal, environment: str | None) -> bool:
    """Check if the target environment is permitted for this principal."""
    if not environment:
        return True
    if "*" in principal.allowed_environments:
        return True
    return environment.lower() in {e.lower() for e in principal.allowed_environments}


# ---------------------------------------------------------------------------
# Dashboard & Panel Mutation Authorization (Section 8.3 & Section 9)
# ---------------------------------------------------------------------------

def authorize_dashboard_read(
    principal: Principal,
    dashboard_uid: str | None = None,
    folder_uid: str | None = None,
) -> PolicyDecision:
    """Check if the user may view a dashboard or its panel definitions."""
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Principal is not active.")

    # All active roles may read dashboards within their permitted scope
    if not is_dashboard_in_scope(principal, dashboard_uid, folder_uid):
        return PolicyDecision(
            allowed=False,
            reason=f"Dashboard {dashboard_uid or ''} is outside permitted resource scope.",
        )

    return PolicyDecision(allowed=True, reason="Dashboard read permitted.")


def authorize_dashboard_propose(
    principal: Principal,
    operation: str = "create",
    dashboard_uid: str | None = None,
    folder_uid: str | None = None,
) -> PolicyDecision:
    """Check if the user is authorized to create, update, or propose dashboard/panel changes."""
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    role = (principal.role or "").lower()

    # Observers and Operators are strictly forbidden from creating/modifying dashboards & panels
    if role == "observer":
        return PolicyDecision(
            allowed=False,
            reason=(
                "Policy Denied: Role 'observer' lacks permission 'dashboard:propose'. "
                "Observers have read-only access and cannot create dashboards or add/modify panels."
            ),
        )

    if role == "operator":
        return PolicyDecision(
            allowed=False,
            reason=(
                "Policy Denied: Role 'operator' lacks permission 'dashboard:propose'. "
                "Operators manage alert rules. Dashboard creation and editing requires Maintainer or Admin."
            ),
        )

    if role not in ("maintainer", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{principal.role}' cannot create or modify dashboards.",
        )

    # Scoping check for Maintainers (Admins have global scope unless explicitly denied)
    if role == "maintainer":
        if operation == "create":
            # Folder allow is required for dashboard creation
            if folder_uid and not is_dashboard_in_scope(principal, None, folder_uid):
                return PolicyDecision(
                    allowed=False,
                    reason=f"Policy Denied: Destination folder '{folder_uid}' is not in allowed folders.",
                )
        elif dashboard_uid:
            if not is_dashboard_in_scope(principal, dashboard_uid, folder_uid):
                return PolicyDecision(
                    allowed=False,
                    reason=f"Policy Denied: Dashboard '{dashboard_uid}' is outside permitted scope.",
                )

    impact_class = "destructive_delete" if operation in ("remove", "delete") else "routine_edit"
    requires_second_approver = (impact_class == "destructive_delete")

    return PolicyDecision(
        allowed=True,
        reason=f"Authorized to propose dashboard operation '{operation}'.",
        impact_class=impact_class,
        requires_second_approver=requires_second_approver,
    )


def authorize_dashboard_approve(
    principal: Principal,
    proposal: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Check if the user is authorized to approve a dashboard proposal."""
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    role = (principal.role or "").lower()

    if role == "observer":
        return PolicyDecision(
            allowed=False,
            reason="Policy Denied: Role 'observer' lacks permission 'dashboard:approve'.",
        )

    if role == "operator":
        return PolicyDecision(
            allowed=False,
            reason="Policy Denied: Role 'operator' lacks permission 'dashboard:approve'.",
        )

    if role not in ("maintainer", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{principal.role}' cannot approve dashboard proposals.",
        )

    if proposal:
        proposer_id = proposal.get("proposer_id") or (proposal.get("owner") or {}).get("userId")
        requires_second = proposal.get("requires_second_approver") or (
            proposal.get("impact_class") == "destructive_delete"
        )
        # Separation of duties (Section 8.4): A proposer cannot self-approve a destructive deletion
        if requires_second and proposer_id and proposer_id == principal.user_id:
            return PolicyDecision(
                allowed=False,
                reason="Policy Denied: Separation of duties requires a second, different approver.",
            )

    return PolicyDecision(allowed=True, reason="Authorized to approve dashboard proposal.")


def authorize_dashboard_execute(
    principal: Principal,
    proposal: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Check if the user is authorized to execute and apply an approved dashboard proposal."""
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    role = (principal.role or "").lower()

    if role in ("observer", "operator"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{role}' lacks permission 'dashboard:execute'.",
        )

    if role not in ("maintainer", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{principal.role}' cannot execute dashboard proposals.",
        )

    return PolicyDecision(allowed=True, reason="Authorized to execute dashboard proposal.")


# ---------------------------------------------------------------------------
# Alert Rule Mutation Authorization
# ---------------------------------------------------------------------------

def authorize_alert_propose(
    principal: Principal,
    environment: str | None = None,
) -> PolicyDecision:
    """Check if the user is authorized to create or propose alert rules."""
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    role = (principal.role or "").lower()

    if role == "observer":
        return PolicyDecision(
            allowed=False,
            reason="Policy Denied: Role 'observer' lacks permission 'alert:propose'.",
        )

    if role == "maintainer":
        return PolicyDecision(
            allowed=False,
            reason=(
                "Policy Denied: Role 'maintainer' lacks permission 'alert:propose'. "
                "Alert rules can only be managed by Operators or Administrators."
            ),
        )

    if role not in ("operator", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{principal.role}' cannot propose alert rules.",
        )

    if not is_environment_in_scope(principal, environment):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Target environment '{environment}' is outside allowed environments.",
        )

    return PolicyDecision(allowed=True, reason="Authorized to propose alert rule.")


def authorize_alert_approve(principal: Principal) -> PolicyDecision:
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")
    role = (principal.role or "").lower()
    if role not in ("operator", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{principal.role}' cannot approve alert rules.",
        )
    return PolicyDecision(allowed=True, reason="Authorized to approve alert rules.")


def authorize_alert_execute(principal: Principal) -> PolicyDecision:
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")
    role = (principal.role or "").lower()
    if role not in ("operator", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{principal.role}' cannot execute alert rules.",
        )
    return PolicyDecision(allowed=True, reason="Authorized to execute alert rules.")


# ---------------------------------------------------------------------------
# Prometheus Query Gateway Authorization (Section 12)
# ---------------------------------------------------------------------------

def authorize_promql(
    principal: Principal,
    is_custom: bool = True,
    expr: str = "",
    environment: str | None = None,
) -> PolicyDecision:
    """Check if the user may execute a Prometheus query or custom PromQL expression.
    
    Per Section 6.2 and Section 12.4:
    - observer: Strictly denied from executing Prometheus queries and custom PromQL.
    - maintainer / operator: Extended catalogue and custom PromQL, scoped to environment.
    - admin: Bounded custom PromQL across environments.
    """
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    role = (principal.role or "").lower()

    if role == "observer":
        return PolicyDecision(
            allowed=False,
            reason=(
                "Policy Denied: Role 'observer' is not permitted to execute Prometheus queries. "
                "Observers have read-only access to existing dashboards and catalog metadata only."
            ),
        )

    if not is_environment_in_scope(principal, environment):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Environment '{environment}' is outside allowed environments {list(principal.allowed_environments)}.",
        )

    return PolicyDecision(allowed=True, reason="Prometheus query execution authorized.")


__all__ = [
    "PolicyDecision",
    "authorize_alert_approve",
    "authorize_alert_execute",
    "authorize_alert_propose",
    "authorize_dashboard_approve",
    "authorize_dashboard_execute",
    "authorize_dashboard_propose",
    "authorize_dashboard_read",
    "authorize_promql",
    "is_dashboard_in_scope",
    "is_environment_in_scope",
]

