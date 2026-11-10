"""
app/storage/seed_audit.py

Diagnostics for the demo RBAC seed data.

The seeded demo accounts reference Grafana folders and dashboards by UID.
Those UIDs belong to one particular Grafana instance, so on a deployment
whose Grafana holds different resources the references are stale. A stale
reference is a *data-consistency* problem, not an authorization problem: the
right answer is never to widen a demo user's scope so that they can see
something, and never to invent a replacement resource.

This module therefore only *reports*:

* which demo accounts exist and which of them are still configured exactly
  as the seeder created them (an account an administrator reconfigured is
  reported as configured, not as stale);
* which folder and dashboard UIDs those accounts reference;
* which of those UIDs do not exist in the connected Grafana instance;
* what the operator has to do about it.

Nothing here writes to Grafana and nothing here changes a user's scopes.
The report is exposed to administrators through
``GET /api/v1/admin/seed-audit``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from app.storage.database import get_db_session
from app.storage.seed import DEMO_SEEDED_SCOPES, is_demo_user
from app.storage.user_store import UserStore

logger = logging.getLogger(__name__)

#: Scope lists that name Grafana resources.
FOLDER_SCOPE_KEYS = ("allowed_folder_uids",)
DASHBOARD_SCOPE_KEYS = ("allowed_dashboard_uids", "denied_dashboard_uids")


def _unwrap_mcp_payload(raw: Any) -> Any:
    """Extract the payload from the Grafana MCP tool's response envelope.

    ``grafana_api_request`` wraps the Grafana response as
    ``{"status": 200, "headers": {...}, "data": <payload>}``. Older or
    different builds return the payload directly, so both shapes are
    accepted.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if isinstance(raw, dict) and "data" in raw and "status" in raw:
        return raw.get("data")
    return raw


async def fetch_grafana_inventory() -> dict[str, Any]:
    """The folder and dashboard UIDs the connected Grafana instance holds.

    Returns a mapping with ``available`` (whether Grafana answered at all),
    ``folders``, ``dashboards`` and, when Grafana could not be reached, an
    ``error``. A Grafana that is unreachable is not an error for the caller:
    the report simply says the inventory is unknown.
    """
    inventory: dict[str, Any] = {
        "available": False,
        "folders": [],
        "dashboards": [],
        "error": None,
    }
    try:
        from app.mcp.session import mcp_call

        raw_folders = await mcp_call(
            "grafana_api_request",
            {"method": "GET", "endpoint": "/api/folders"},
            raw=True,
            timeout=10.0,
        )
        raw_search = await mcp_call(
            "grafana_api_request",
            {"method": "GET", "endpoint": "/api/search?type=dash-db&limit=1000"},
            raw=True,
            timeout=10.0,
        )
    except Exception as exc:  # pragma: no cover - the MCP bridge may be down
        inventory["error"] = f"Grafana inventory could not be retrieved: {exc}"
        return inventory

    if not raw_folders or not raw_search:
        inventory["error"] = "Grafana inventory could not be retrieved (the Grafana MCP bridge returned nothing)."
        return inventory

    folders_raw = _unwrap_mcp_payload(raw_folders)
    search_raw = _unwrap_mcp_payload(raw_search)

    folder_uids: set[str] = set()
    if isinstance(folders_raw, list):
        for folder in folders_raw:
            if isinstance(folder, dict):
                uid = folder.get("uid")
                if uid:
                    folder_uids.add(str(uid))
    # Grafana's General folder is addressed by the empty UID.
    folder_uids.add("")

    dashboard_uids: set[str] = set()
    entries = search_raw if isinstance(search_raw, list) else []
    for entry in entries:
        if isinstance(entry, dict):
            uid = entry.get("uid")
            if uid:
                dashboard_uids.add(str(uid))

    inventory["available"] = True
    inventory["folders"] = sorted(folder_uids)
    inventory["dashboards"] = sorted(dashboard_uids)
    return inventory


def _demo_accounts() -> list[dict[str, Any]]:
    """The demo accounts currently in the store, with their scopes."""
    accounts: list[dict[str, Any]] = []
    with get_db_session() as session:
        for user in UserStore.list_users(session):
            if not is_demo_user(user):
                continue
            scopes = user.scopes
            seeded = DEMO_SEEDED_SCOPES.get(user.user_id)
            configured = {
                "allowed_folder_uids": list(scopes.allowed_folder_uids if scopes else []),
                "allowed_dashboard_uids": list(scopes.allowed_dashboard_uids if scopes else []),
                "denied_dashboard_uids": list(scopes.denied_dashboard_uids if scopes else []),
                "allowed_environments": list(scopes.allowed_environments if scopes else []),
            }
            accounts.append(
                {
                    "user_id": user.user_id,
                    "username": user.username,
                    "role": user.role,
                    "status": user.status,
                    "authz_version": user.authz_version,
                    "scopes": configured,
                    # A demo account whose scopes still match the seeded ones
                    # is "unmodified"; one an administrator has reconfigured is
                    # "configured" and is never reported as stale.
                    "seed_state": "unmodified" if seeded == configured else "configured",
                }
            )
    return accounts


def _referenced_uids(account: dict[str, Any]) -> tuple[list[str], list[str]]:
    scopes = account.get("scopes") or {}
    folders: list[str] = []
    dashboards: list[str] = []
    for key in FOLDER_SCOPE_KEYS:
        for uid in scopes.get(key) or []:
            if str(uid) and str(uid) != "*" and str(uid) not in folders:
                folders.append(str(uid))
    for key in DASHBOARD_SCOPE_KEYS:
        for uid in scopes.get(key) or []:
            if str(uid) and str(uid) not in dashboards:
                dashboards.append(str(uid))
    return folders, dashboards


async def audit_seed_references(
    *,
    accounts: Iterable[dict[str, Any]] | None = None,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report stale Grafana references in the demo seed data.

    The report never changes anything. It says which demo accounts point at
    Grafana resources that do not exist, and it spells out the remediation:
    an administrator has to assign real folder and dashboard UIDs (through
    ``PUT /api/v1/admin/users/{user_id}/access``), or the demo accounts have
    to be left without Grafana scopes.
    """
    demo_accounts = list(accounts) if accounts is not None else _demo_accounts()
    grafana = inventory if inventory is not None else await fetch_grafana_inventory()

    known_folders = set(grafana.get("folders") or [])
    known_dashboards = set(grafana.get("dashboards") or [])

    stale_folders: dict[str, list[str]] = {}
    stale_dashboards: dict[str, list[str]] = {}

    for account in demo_accounts:
        folders, dashboards = _referenced_uids(account)
        missing_folders = [uid for uid in folders if uid not in known_folders]
        missing_dashboards = [uid for uid in dashboards if uid not in known_dashboards]
        if missing_folders:
            stale_folders[account["user_id"]] = missing_folders
        if missing_dashboards:
            stale_dashboards[account["user_id"]] = missing_dashboards

    has_stale = bool(stale_folders or stale_dashboards)
    remediation = None
    if not grafana.get("available"):
        remediation = (
            "Grafana could not be reached, so the seed references could not be "
            "validated. Retry once Grafana and the Grafana MCP bridge are up."
        )
    elif has_stale:
        remediation = (
            "The demo accounts reference Grafana resources that do not exist in this "
            "instance. Assign the real folder and dashboard UIDs with "
            "PUT /api/v1/admin/users/{user_id}/access, or clear the demo scopes. "
            "The references are deliberately not repaired automatically: granting a "
            "demo account access to an arbitrary existing folder would hand it "
            "access to resources it was never meant to see."
        )

    return {
        "ok": not has_stale and bool(grafana.get("available")),
        "grafana_available": bool(grafana.get("available")),
        "grafana_error": grafana.get("error"),
        "grafana_folder_count": len(known_folders),
        "grafana_dashboard_count": len(known_dashboards),
        "demo_accounts": demo_accounts,
        "demo_account_count": len(demo_accounts),
        "stale_folder_references": stale_folders,
        "stale_dashboard_references": stale_dashboards,
        "stale_reference_count": sum(len(v) for v in stale_folders.values())
        + sum(len(v) for v in stale_dashboards.values()),
        "remediation": remediation,
    }


async def log_seed_warnings() -> dict[str, Any]:
    """Log the seed audit at WARNING level when there is something to say.

    Called during start-up so that an operator sees the stale references in
    the log even without opening the admin endpoint.
    """
    report = await audit_seed_references()
    if report["stale_reference_count"]:
        logger.warning(
            "%d demo RBAC account(s) reference Grafana resources that do not exist "
            "in this instance. Affected accounts: folder refs=%s, dashboard refs=%s. %s",
            report["stale_reference_count"],
            report["stale_folder_references"],
            report["stale_dashboard_references"],
            report["remediation"],
        )
    elif not report["grafana_available"]:
        logger.warning("Could not validate the demo RBAC seed references: %s", report["grafana_error"])
    else:
        logger.info("Demo RBAC seed references are consistent with the connected Grafana instance.")
    return report


__all__ = [
    "DASHBOARD_SCOPE_KEYS",
    "FOLDER_SCOPE_KEYS",
    "audit_seed_references",
    "fetch_grafana_inventory",
    "log_seed_warnings",
]
