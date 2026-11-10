"""
app/auth/resource_scope.py

The single place where dashboard and folder scope decisions are made.

Every dashboard/folder read, proposal, approval and write funnels through the
helpers in this module so that scope logic is not duplicated across the API
routes, the AI Agent tool wrappers and the Grafana mutation boundary.

Two rules hold everywhere:

* Identifiers (dashboard UID, folder UID) are never trusted on their own --
  the folder a dashboard lives in is re-fetched from Grafana before it is
  used for a decision.
* Missing or ambiguous scope information fails closed: an unknown folder is
  never treated as "unrestricted", and an administrator keeps global access
  explicitly rather than by accident.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Sequence

from app.auth.policy import PolicyDecision, authorize_dashboard_read, is_dashboard_in_scope
from app.auth.principal import Principal, get_current_principal, set_current_principal

logger = logging.getLogger(__name__)

#: Grafana's "General" folder -- the pseudo-folder that holds dashboards which
#: belong to no folder at all. Grafana reports it as an empty folder UID.
GENERAL_FOLDER_UID = ""


class AuthorizationDenied(Exception):
    """A resource-scope check failed.

    ``status_code`` is 403 for an out-of-scope resource and 401 when no
    authenticated identity was available at all.
    """

    def __init__(self, reason: str, *, status_code: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Principal helpers
# ---------------------------------------------------------------------------

def is_admin(principal: Principal | None) -> bool:
    return bool(principal) and (principal.role or "").lower() == "admin"


def current_principal() -> Principal | None:
    """The principal bound to the current request/context, if any."""
    return get_current_principal()


def require_principal_context() -> Principal:
    """Return the current principal or fail closed with HTTP 401."""
    principal = get_current_principal()
    if principal is None:
        raise AuthorizationDenied(
            "An authenticated identity is required for this operation.",
            status_code=401,
        )
    return principal


def require_active_principal() -> Principal:
    """Return the current principal with its *current* permissions.

    This is the entry point every Grafana-facing wrapper uses, so it is
    where a stale snapshot is caught:

    * the caller's row is re-read from the User and Role Store using the
      already authenticated identity key (issuer, subject) -- the identity
      is never re-established, only the authorization data is refreshed;
    * an account that has been deactivated, deleted, or had its role or
      scopes changed is refused or authorized according to its *current*
      state, not the state it had when the request started; and
    * the refreshed principal replaces the bound one, so every check that
      follows in the same operation -- and every check in the same agent
      turn -- sees the current permissions.

    A deactivated or otherwise inactive account is *authenticated* but not
    *authorized*: it fails closed with HTTP 403 and never reaches the
    Grafana MCP client.
    """
    principal = require_principal_context()

    from app.auth.refresh import current_principal

    return current_principal(
        principal,
        error=lambda reason: AuthorizationDenied(reason, status_code=403),
    )


# ---------------------------------------------------------------------------
# Folder / dashboard scope predicates
# ---------------------------------------------------------------------------

def folder_in_scope(principal: Principal | None, folder_uid: str | None) -> bool:
    """Is this Grafana folder inside the principal's permitted folders?"""
    if principal is None or not principal.is_active:
        return False
    if is_admin(principal):
        return True
    folders = {str(uid) for uid in principal.allowed_folder_uids}
    if "*" in folders:
        return True
    if folder_uid is None:
        return False
    # The General folder is addressed by the empty UID and is only in scope
    # when it was granted explicitly.
    return str(folder_uid) in folders


def dashboard_in_scope(
    principal: Principal | None,
    dashboard_uid: str | None,
    folder_uid: str | None,
) -> bool:
    """Is this dashboard inside the principal's permitted resource scope?"""
    if principal is None or not principal.is_active:
        return False
    # An explicit deny always wins -- including for administrators.
    if dashboard_uid and str(dashboard_uid) in principal.denied_dashboard_uids:
        return False
    if is_admin(principal):
        return True
    return is_dashboard_in_scope(principal, dashboard_uid, folder_uid)


def _granted_without_folder(principal: Principal, dashboard_uid: str) -> bool:
    """True when the dashboard is allowed even if its folder is unknown."""
    if is_admin(principal):
        return True
    if dashboard_uid and str(dashboard_uid) in principal.denied_dashboard_uids:
        return False
    if dashboard_uid and str(dashboard_uid) in principal.allowed_dashboard_uids:
        return True
    return "*" in principal.allowed_folder_uids


# ---------------------------------------------------------------------------
# Authoritative Grafana metadata
# ---------------------------------------------------------------------------

async def resolve_dashboard_scope(dashboard_uid: str) -> tuple[bool, str | None]:
    """Resolve ``(exists, folder_uid)`` for a dashboard from Grafana itself.

    The folder UID is taken from the authoritative dashboard metadata, never
    from the request. A dashboard whose folder cannot be determined is
    reported as ``(True, None)`` so the caller can fail closed.
    """
    from app.grafana_tools.dashboard import fetch_dashboard_folder_uid

    try:
        return await fetch_dashboard_folder_uid(dashboard_uid)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not resolve the scope of dashboard %r: %s", dashboard_uid, exc)
        return False, None


async def authorize_dashboard_access(
    principal: Principal | None,
    dashboard_uid: str | None,
) -> PolicyDecision:
    """Authorize a read of a dashboard and its panel definitions.

    The dashboard's folder is re-fetched from Grafana before the decision is
    made, so a caller cannot widen its own scope by supplying a folder UID.
    """
    if principal is None:
        return PolicyDecision(allowed=False, reason="An authenticated identity is required.")
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    uid = str(dashboard_uid or "").strip()
    if not uid:
        return PolicyDecision(
            allowed=False,
            reason="A dashboard identifier is required for this operation.",
        )

    exists, folder_uid = await resolve_dashboard_scope(uid)
    if not exists:
        # Do not distinguish "does not exist" from "not permitted" to the caller.
        return PolicyDecision(
            allowed=False,
            reason="Dashboard is not available or outside your permitted scope.",
        )

    decision = authorize_dashboard_read(principal, uid, folder_uid)
    if not decision.allowed:
        logger.info(
            "Denied dashboard read: principal=%s dashboard=%s folder=%s reason=%s",
            principal.user_id, uid, folder_uid, decision.reason,
        )
        return PolicyDecision(
            allowed=False,
            reason="Dashboard is not available or outside your permitted scope.",
        )
    return decision


async def filter_dashboards_in_scope(
    principal: Principal | None,
    dashboards: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return only the dashboards the principal is allowed to see.

    Dashboards whose folder cannot be established are dropped for everyone
    except administrators (who keep global access by definition).
    """
    entries: list[dict[str, Any]] = []
    for dashboard in dashboards or ():
        if not isinstance(dashboard, Mapping):
            continue
        uid = str(dashboard.get("uid") or "").strip()
        if not uid:
            continue
        entry = dict(dashboard)
        entry["uid"] = uid
        entries.append(entry)

    if principal is None or not principal.is_active:
        return []

    unresolved = [
        entry for entry in entries
        if entry.get("folderUid") is None
        and not _granted_without_folder(principal, str(entry["uid"]))
    ]
    if unresolved:
        outcomes = await asyncio.gather(
            *(resolve_dashboard_scope(str(entry["uid"])) for entry in unresolved),
            return_exceptions=True,
        )
        for entry, outcome in zip(unresolved, outcomes):
            if isinstance(outcome, BaseException):
                continue
            exists, folder_uid = outcome
            if exists and folder_uid is not None:
                entry["folderUid"] = folder_uid

    return [
        entry for entry in entries
        if dashboard_in_scope(principal, str(entry["uid"]), entry.get("folderUid"))
    ]


# ---------------------------------------------------------------------------
# Destination folder resolution (dashboard creation / moves)
# ---------------------------------------------------------------------------

def resolve_destination_folder(
    principal: Principal | None,
    requested_folder_uid: str | None = None,
    *,
    default_folder_uid: str | None = None,
) -> str:
    """Resolve the folder a dashboard must be created in (or moved to).

    An explicitly requested folder is always validated against the
    principal's permitted folders. When no folder is requested, the
    deployment default is used if the principal may write there, otherwise the
    principal's single permitted folder. A principal who has no unambiguous,
    authorized destination gets a rejection -- never a silent fallback to
    the General folder.
    """
    if principal is None:
        raise AuthorizationDenied(
            "An authenticated identity is required for this operation.",
            status_code=401,
        )
    if not principal.is_active:
        raise AuthorizationDenied("Account is not active.")

    requested = str(requested_folder_uid or "").strip()
    if requested:
        if not folder_in_scope(principal, requested):
            raise AuthorizationDenied(
                f"Folder '{requested}' is outside your permitted folders."
            )
        return requested

    default = str(default_folder_uid or "").strip()
    if default and folder_in_scope(principal, default):
        return default

    explicit = sorted(
        str(uid) for uid in principal.allowed_folder_uids if str(uid) and str(uid) != "*"
    )
    if len(explicit) == 1:
        return explicit[0]

    if is_admin(principal):
        # Administrators keep global access; the deployment default (or the
        # General folder) remains a valid destination for them.
        return default

    raise AuthorizationDenied(
        "No destination folder was specified and no single authorized folder "
        "could be resolved. Name the target folder explicitly."
    )


async def authorize_proposal_scope(
    principal: Principal | None,
    proposal: Mapping[str, Any] | None,
    *,
    ir: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Check that the caller may see the resources a proposal targets.

    A proposal carries the dashboard and folder it will touch; reading,
    approving, rejecting or modifying it must not disclose a resource outside
    the caller's scope. The dashboard's folder is re-fetched from Grafana, so
    a proposal cannot widen the caller's scope by naming a folder the
    dashboard does not actually live in.

    This lives here -- and not in a route -- so that the HTTP API, the ADK
    agent and the MCP server all apply exactly the same scope rule.
    """
    if principal is None:
        return PolicyDecision(allowed=False, reason="An authenticated identity is required.")
    if not principal.is_active:
        return PolicyDecision(allowed=False, reason="Account is not active.")

    content = ir if ir is not None else (proposal or {}).get("ir")
    content = content if isinstance(content, Mapping) else {}
    if not content and not proposal:
        return PolicyDecision(allowed=False, reason="Proposal not found.")

    dashboard_uid = content.get("dashboardUid")
    if dashboard_uid:
        return await authorize_dashboard_access(principal, dashboard_uid)

    folder_uid = content.get("folderUid")
    if folder_uid is None:
        folder_uid = content.get("folderUID")
    if folder_uid is not None and not folder_in_scope(principal, folder_uid):
        return PolicyDecision(
            allowed=False,
            reason="This proposal targets a folder outside your permitted scope.",
        )
    return PolicyDecision(allowed=True, reason="Proposal target is within scope.")


__all__ = [
    "AuthorizationDenied",
    "GENERAL_FOLDER_UID",
    "authorize_dashboard_access",
    "authorize_proposal_scope",
    "current_principal",
    "dashboard_in_scope",
    "filter_dashboards_in_scope",
    "folder_in_scope",
    "is_admin",
    "require_active_principal",
    "require_principal_context",
    "resolve_dashboard_scope",
    "resolve_destination_folder",
]
