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

from app.auth.environment import resolve_environment
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

    A missing dashboard UID or folder UID never grants access on its own --
    an unknown scope fails closed.
    """
    if dashboard_uid and dashboard_uid in principal.denied_dashboard_uids:
        return False

    if dashboard_uid and dashboard_uid in principal.allowed_dashboard_uids:
        return True

    if "*" in principal.allowed_folder_uids:
        return True

    if folder_uid is not None and folder_uid in principal.allowed_folder_uids:
        return True

    # If neither dashboard nor folder was specified or matched, deny by default
    return False


def is_environment_in_scope(principal: Principal, environment: str | None) -> bool:
    """Check if the target environment is permitted for this principal.

    ``environment=None`` means "no environment dimension applies to this
    deployment" and is permitted; a named environment must appear in the
    principal's permitted environments.
    """
    if not principal.is_active:
        return False
    if (principal.role or "").lower() == "admin":
        return True
    if not environment:
        return True
    if "*" in principal.allowed_environments:
        return True
    return environment.lower() in {e.lower() for e in principal.allowed_environments}


# ---------------------------------------------------------------------------
# Impact classification (Section 8.4)
# ---------------------------------------------------------------------------

#: The impact classes a proposal can carry.
ROUTINE_EDIT = "routine_edit"
DESTRUCTIVE_DELETE = "destructive_delete"

#: Operations that destroy a resource.
_DESTRUCTIVE_OPERATIONS = frozenset({"remove", "delete"})


def classify_impact(operation: str | None = None, ir: Mapping[str, Any] | None = None) -> str:
    """Classify the impact of a proposal, server-side.

    The classification is derived from the operation alone -- never from a
    client-supplied ``impact_class`` or ``requires_second_approver`` field.
    A client that downgrades a destructive operation in the payload gets the
    destructive classification, because the classification is recomputed from
    the operation every time it is needed.

    ``ir`` is accepted so a caller can pass the canonical proposal content
    instead of a separately-maintained operation string. When both are given
    the classification is the *more* severe of the two: a proposal whose
    recorded operation says "remove" but whose payload says "update" is
    treated as destructive, so a tampered payload can never downgrade a
    destructive operation into a routine one.
    """
    candidates = {str(operation or "").strip().lower()}
    if ir:
        candidates.add(str(ir.get("operation") or "").strip().lower())
    if candidates & _DESTRUCTIVE_OPERATIONS:
        return DESTRUCTIVE_DELETE
    return ROUTINE_EDIT


def requires_second_approver(impact_class: str | None) -> bool:
    """Does this impact class require an independent second approver?"""
    return str(impact_class or "") == DESTRUCTIVE_DELETE


def proposal_proposer_id(proposal: Mapping[str, Any] | None) -> str | None:
    """The server-recorded proposer of a proposal, if any."""
    if not proposal:
        return None
    proposer = proposal.get("proposer_id")
    if not proposer:
        owner = proposal.get("owner")
        if isinstance(owner, Mapping):
            proposer = owner.get("userId")
    return str(proposer) if proposer else None


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

    # Administrators keep global access (an explicit deny still wins).
    role = (principal.role or "").lower()
    if dashboard_uid and dashboard_uid in principal.denied_dashboard_uids:
        return PolicyDecision(
            allowed=False,
            reason=f"Dashboard {dashboard_uid} is explicitly denied.",
        )
    if role == "admin":
        return PolicyDecision(allowed=True, reason="Dashboard read permitted.")

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
    *,
    require_target: bool = False,
) -> PolicyDecision:
    """Check if the user is authorized to create, update, or propose dashboard/panel changes.

    ``require_target`` is set at the enforcement points (proposal building and
    execution), where the target dashboard / destination folder is always
    known. In that mode a missing identifier is a denial, never a silently
    skipped scope check.
    """
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
            # The destination folder must be known and authorized.
            if folder_uid is None:
                if require_target:
                    return PolicyDecision(
                        allowed=False,
                        reason=(
                            "Policy Denied: the destination folder for the new dashboard "
                            "could not be resolved and authorized."
                        ),
                    )
            elif not is_dashboard_in_scope(principal, None, folder_uid):
                return PolicyDecision(
                    allowed=False,
                    reason=f"Policy Denied: Destination folder '{folder_uid}' is not in allowed folders.",
                )
        else:
            # Updates, deletions and panel changes act on an existing dashboard.
            if dashboard_uid is None:
                if require_target:
                    return PolicyDecision(
                        allowed=False,
                        reason=(
                            "Policy Denied: the target dashboard could not be resolved "
                            "and authorized."
                        ),
                    )
            elif not is_dashboard_in_scope(principal, dashboard_uid, folder_uid):
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
    """Check if the user is authorized to approve a dashboard proposal.

    The separation-of-duties rule is applied here, from the *server-side*
    proposal record: the impact class is recomputed from the proposal's own
    operation rather than trusted from a stored or client-supplied field, so
    a downgraded ``impact_class`` cannot buy a proposer the right to approve
    their own destructive change.
    """
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
        ir = proposal.get("ir")
        ir = ir if isinstance(ir, Mapping) else {}
        impact = classify_impact(proposal.get("operation"), ir)
        stored_impact = proposal.get("impact_class")
        if stored_impact and str(stored_impact) != impact:
            # The recorded classification and the recomputed one disagree.
            # That means the proposal content changed underneath the
            # classification -- fail closed rather than pick a side.
            return PolicyDecision(
                allowed=False,
                reason=(
                    "Policy Denied: the proposal's impact classification no longer "
                    "matches its content; the proposal must be re-created."
                ),
                impact_class=impact,
                requires_second_approver=requires_second_approver(impact),
            )
        needs_second = requires_second_approver(impact)
        proposer_id = proposal_proposer_id(proposal)
        # Separation of duties (Section 8.4): a proposer cannot approve their
        # own destructive change -- an independent second approver must.
        if needs_second and proposer_id and proposer_id == principal.user_id:
            return PolicyDecision(
                allowed=False,
                reason="Policy Denied: Separation of duties requires a second, different approver.",
                impact_class=impact,
                requires_second_approver=True,
            )
        return PolicyDecision(
            allowed=True,
            reason="Authorized to approve dashboard proposal.",
            impact_class=impact,
            requires_second_approver=needs_second,
        )

    return PolicyDecision(allowed=True, reason="Authorized to approve dashboard proposal.")


def authorize_proposal_rejection(
    principal: Principal,
    proposal: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Check if the user is authorized to reject a proposal.

    Rejection is an administrative act on someone else's proposal, so it
    follows the same role rules as approval and the same separation-of-duties
    rule: a proposer may not reject their own proposal (that would let them
    erase a pending change without a reviewer ever seeing it).
    """
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    role = (principal.role or "").lower()
    if role not in ("maintainer", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=(
                f"Policy Denied: Role '{principal.role}' cannot manage dashboard proposals."
            ),
        )

    if proposal:
        proposer_id = proposal_proposer_id(proposal)
        if proposer_id and proposer_id == principal.user_id:
            return PolicyDecision(
                allowed=False,
                reason=(
                    "Policy Denied: a proposer cannot reject their own proposal; "
                    "rejection must come from a different reviewer."
                ),
            )

    return PolicyDecision(allowed=True, reason="Authorized to reject dashboard proposal.")


def authorize_alert_rejection(
    principal: Principal,
    proposal: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Check if the user is authorized to reject an alert proposal.

    Rejection is an administrative act on somebody else's proposal, so it
    follows the same role rules as alert approval -- only the roles that
    manage alert rules may reject one -- and the same separation-of-duties
    rule: a proposer may not reject their own proposal.
    """
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    role = (principal.role or "").lower()
    if role not in ("operator", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=(
                f"Policy Denied: Role '{principal.role}' cannot reject alert proposals."
            ),
        )

    if proposal:
        proposer_id = proposal_proposer_id(proposal)
        if proposer_id and proposer_id == principal.user_id:
            return PolicyDecision(
                allowed=False,
                reason=(
                    "Policy Denied: a proposer cannot reject their own proposal; "
                    "rejection must come from a different reviewer."
                ),
            )

    return PolicyDecision(allowed=True, reason="Authorized to reject alert proposals.")


def revalidate_proposal_for_execution(
    principal: Principal,
    proposal: Mapping[str, Any] | None = None,
    *,
    environment: str | None = None,
) -> PolicyDecision:
    """Re-check a proposal's approval state immediately before execution.

    The approval that was granted earlier is never trusted on its own. This
    function re-derives everything that could have changed since the
    approval was recorded:

    * the proposal's state must still be ``approved``;
    * the impact classification is recomputed from the proposal content and
      must still match the classification that was recorded;
    * a proposal that requires a second approver must have been approved by
      somebody other than the proposer;
    * the recorded approver must still be an active account.
    """
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    if not proposal:
        return PolicyDecision(allowed=False, reason="Proposal not found.")

    status = str(proposal.get("status") or proposal.get("state") or "").lower()
    if status and status not in ("approved", "executing", "built"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: the proposal is in state '{status}', not 'approved'.",
        )

    ir = proposal.get("ir")
    ir = ir if isinstance(ir, Mapping) else {}
    impact = classify_impact(proposal.get("operation"), ir)
    stored_impact = proposal.get("impact_class")
    if stored_impact and str(stored_impact) != impact:
        return PolicyDecision(
            allowed=False,
            reason=(
                "Policy Denied: the proposal's impact classification no longer matches "
                "its content; the approval no longer applies."
            ),
            impact_class=impact,
            requires_second_approver=requires_second_approver(impact),
        )

    needs_second = requires_second_approver(impact)
    proposer_id = proposal_proposer_id(proposal)
    # The approver is recorded by the proposal store under ``approvedBy``.
    # The snake-case spellings are accepted as well so that a proposal
    # record loaded from a different persistence layer still validates.
    approver_id = (
        proposal.get("approvedBy")
        or proposal.get("approved_by")
        or proposal.get("approver_id")
    )
    if needs_second:
        if not approver_id:
            return PolicyDecision(
                allowed=False,
                reason=(
                    "Policy Denied: this operation requires an independent second "
                    "approver, but no approver was recorded."
                ),
                impact_class=impact,
                requires_second_approver=True,
            )
        if proposer_id and str(approver_id) == str(proposer_id):
            return PolicyDecision(
                allowed=False,
                reason=(
                    "Policy Denied: Separation of duties requires a second, different approver."
                ),
                impact_class=impact,
                requires_second_approver=True,
            )

    if environment is not None:
        resolved, environment_error = _resolve_environment_for_policy(principal, environment)
        if environment_error:
            return PolicyDecision(allowed=False, reason=f"Policy Denied: {environment_error}")
        if not is_environment_in_scope(principal, resolved):
            return PolicyDecision(
                allowed=False,
                reason=f"Policy Denied: Environment '{resolved}' is outside allowed environments.",
            )

    return PolicyDecision(
        allowed=True,
        reason="Proposal approval revalidated.",
        impact_class=impact,
        requires_second_approver=needs_second,
    )


def authorize_dashboard_execute(
    principal: Principal,
    proposal: Mapping[str, Any] | None = None,
    *,
    dashboard_uid: str | None = None,
    folder_uid: str | None = None,
    environment: str | None = None,
    require_target: bool = False,
) -> PolicyDecision:
    """Check if the user is authorized to execute and apply an approved dashboard proposal.

    This is the last gate before a Grafana write, so it re-validates the
    caller's role, the target dashboard's scope and the environment -- the
    approval that was granted earlier is never trusted on its own.
    """
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

    if role == "maintainer":
        if dashboard_uid is None:
            # A creation: the destination folder must itself be authorized.
            if folder_uid is None:
                if require_target:
                    return PolicyDecision(
                        allowed=False,
                        reason=(
                            "Policy Denied: neither the target dashboard nor an "
                            "authorized destination folder could be resolved."
                        ),
                    )
            elif not is_dashboard_in_scope(principal, None, folder_uid):
                return PolicyDecision(
                    allowed=False,
                    reason=f"Policy Denied: Destination folder '{folder_uid}' is not in allowed folders.",
                )
        elif not is_dashboard_in_scope(principal, dashboard_uid, folder_uid):
            return PolicyDecision(
                allowed=False,
                reason=f"Policy Denied: Dashboard '{dashboard_uid}' is outside permitted scope.",
            )

    resolved_environment, environment_error = _resolve_environment_for_policy(principal, environment)
    if environment_error:
        return PolicyDecision(allowed=False, reason=f"Policy Denied: {environment_error}")
    if not is_environment_in_scope(principal, resolved_environment):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Environment '{resolved_environment}' is outside allowed environments.",
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

    resolved, environment_error = _resolve_environment_for_policy(principal, environment)
    if environment_error:
        return PolicyDecision(allowed=False, reason=f"Policy Denied: {environment_error}")
    if resolved is not None and not is_environment_in_scope(principal, resolved):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Target environment '{resolved}' is outside allowed environments.",
        )

    return PolicyDecision(allowed=True, reason="Authorized to propose alert rule.")


def authorize_alert_approve(
    principal: Principal,
    environment: str | None = None,
) -> PolicyDecision:
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")
    role = (principal.role or "").lower()
    if role not in ("operator", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{principal.role}' cannot approve alert rules.",
        )
    resolved, environment_error = _resolve_environment_for_policy(principal, environment)
    if environment_error:
        return PolicyDecision(allowed=False, reason=f"Policy Denied: {environment_error}")
    if resolved is not None and not is_environment_in_scope(principal, resolved):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Target environment '{resolved}' is outside allowed environments.",
        )
    return PolicyDecision(allowed=True, reason="Authorized to approve alert rules.")


def authorize_alert_execute(
    principal: Principal,
    environment: str | None = None,
) -> PolicyDecision:
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")
    role = (principal.role or "").lower()
    if role not in ("operator", "admin"):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Role '{principal.role}' cannot execute alert rules.",
        )
    resolved, environment_error = _resolve_environment_for_policy(principal, environment)
    if environment_error:
        return PolicyDecision(allowed=False, reason=f"Policy Denied: {environment_error}")
    if resolved is not None and not is_environment_in_scope(principal, resolved):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Target environment '{resolved}' is outside allowed environments.",
        )
    return PolicyDecision(allowed=True, reason="Authorized to execute alert rules.")


# ---------------------------------------------------------------------------
# Prometheus Query Gateway Authorization (Section 12)
# ---------------------------------------------------------------------------

def _resolve_environment_for_policy(
    principal: Principal,
    environment: str | None,
) -> tuple[str | None, str | None]:
    """Resolve the environment a policy decision applies to.

    An explicitly supplied environment is validated against the deployment
    catalogue and the principal's permitted environments. When no
    environment is supplied the deployment default is resolved, so the
    environment dimension is never silently inert. ``(None, None)`` is only
    returned when the deployment has no environment dimension configured at
    all; ``(None, reason)`` reports a denial.
    """
    from app.auth.environment import EnvironmentDenied, EnvironmentRequired

    try:
        return resolve_environment(principal, environment), None
    except (EnvironmentDenied, EnvironmentRequired) as exc:
        logger.info("Environment scope denied for %s: %s", principal.user_id, exc)
        return None, exc.reason


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

    resolved, environment_error = _resolve_environment_for_policy(principal, environment)
    if environment_error:
        return PolicyDecision(allowed=False, reason=f"Policy Denied: {environment_error}")

    if not is_environment_in_scope(principal, resolved):
        return PolicyDecision(
            allowed=False,
            reason=f"Policy Denied: Environment '{resolved}' is outside allowed environments {list(principal.allowed_environments)}.",
        )

    return PolicyDecision(allowed=True, reason="Prometheus query execution authorized.")


__all__ = [
    "DESTRUCTIVE_DELETE",
    "PolicyDecision",
    "ROUTINE_EDIT",
    "authorize_alert_approve",
    "authorize_alert_execute",
    "authorize_alert_propose",
    "authorize_alert_rejection",
    "authorize_dashboard_approve",
    "authorize_dashboard_execute",
    "authorize_dashboard_propose",
    "authorize_dashboard_read",
    "authorize_promql",
    "authorize_proposal_rejection",
    "classify_impact",
    "is_dashboard_in_scope",
    "is_environment_in_scope",
    "proposal_proposer_id",
    "requires_second_approver",
    "revalidate_proposal_for_execution",
]

