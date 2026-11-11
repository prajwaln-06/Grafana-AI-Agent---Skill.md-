"""
scripts/setup_rbac_test_env.py

Creates the two isolated Grafana RBAC test environments and assigns the
matching resource scopes in the PostgreSQL User & Role Store.

* Grafana folders and dashboards are created in the *real* Grafana instance
  through the application's own Grafana MCP wrapper
  (:func:`app.mcp.session.mcp_call`) -- the same integration the HTTP API,
  the agent and the MCP server use. No second MCP server is introduced.
* The scopes are written through the application's own User & Role Store
  (:meth:`app.storage.user_store.UserStore.update_user_access`), the store
  the administrative endpoint calls. The role, the status, the denied
  dashboards and the permitted environments of a user are never touched.

The script is idempotent: a rerun re-uses the folder, the dashboard and the
scope entries that already exist. It never overwrites or deletes an existing
Grafana folder or dashboard.

Usage::

    .venv/bin/python scripts/setup_rbac_test_env.py           # set up
    .venv/bin/python scripts/setup_rbac_test_env.py --verify  # verify only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from app.mcp.session import mcp_call, run_sync  # noqa: E402
from app.storage.database import get_db_session  # noqa: E402
from app.storage.user_store import UserStore  # noqa: E402

#: The administrator that performs the scope assignment. The same record the
#: administrative endpoint uses as the acting principal.
ADMIN_USER_ID = "usr_admin_01"

#: The two isolated test environments.
TARGETS: list[dict[str, Any]] = [
    {
        "user_id": "usr_maint_01",
        "username": "alex.rivera",
        "folder_title": "maintainer-test",
        "folder_uid": "maintainer-test",
        "dashboard_title": "Maintainer RBAC Test Dashboard",
        "dashboard_uid": "dash-maintainer-test",
    },
    {
        "user_id": "usr_oper_01",
        "username": "sam.chen",
        "folder_title": "operator-test",
        "folder_uid": "operator-test",
        "dashboard_title": "Operator RBAC Test Dashboard",
        "dashboard_uid": "dash-operator-test",
    },
]


# ---------------------------------------------------------------------------
# Grafana helpers -- every Grafana call goes through the MCP wrapper
# ---------------------------------------------------------------------------

def _unwrap(raw: str) -> Any:
    """Return the payload of a Grafana MCP response.

    ``grafana_api_request`` answers with an envelope
    (``{"status": ..., "data": ...}``); the payload itself is in ``data``.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict) and "status" in payload and "data" in payload:
        status = payload.get("status")
        if isinstance(status, int) and status >= 400:
            raise RuntimeError(f"Grafana API error {status}: {payload.get('body') or payload}")
        return payload.get("data")
    return payload


async def _grafana_api(method: str, endpoint: str, body: Any = None) -> Any:
    """Perform a Grafana HTTP request through the Grafana MCP server."""
    args: dict[str, Any] = {"method": method, "endpoint": endpoint}
    if body is not None:
        args["body"] = json.dumps(body)
    raw = await mcp_call("grafana_api_request", args, raw=True, timeout=30.0)
    if not raw:
        raise RuntimeError(f"Grafana MCP returned an empty response for {method} {endpoint}")
    return _unwrap(raw)


def _dashboard_payload(target: dict[str, Any]) -> dict[str, Any]:
    """The dashboard body used for the test dashboards."""
    title = target["dashboard_title"]
    owner = target["username"]
    return {
        "dashboard": {
            "uid": target["dashboard_uid"],
            "title": title,
            "description": (
                f"RBAC test dashboard for {owner}. It lives in the '{target['folder_title']}' "
                "folder, which is assigned to this user only."
            ),
            "tags": ["rbac-test", target["folder_title"]],
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 0,
            "refresh": "",
            "time": {"from": "now-6h", "to": "now"},
            "editable": True,
            "panels": [
                {
                    "id": 1,
                    "type": "text",
                    "title": "RBAC test environment",
                    "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
                    "options": {
                        "mode": "markdown",
                        "content": (
                            f"# {title}\n\n"
                            f"Folder: `{target['folder_title']}` (UID `{target['folder_uid']}`)\n\n"
                            f"Assigned user: `{owner}`\n\n"
                            "This dashboard is used to verify that the RBAC scope assignment "
                            "reaches Grafana and the User & Role Store."
                        ),
                    },
                }
            ],
        },
        "folderUid": target["folder_uid"],
        "overwrite": False,
        "message": "Created by the RBAC test environment setup",
    }


async def ensure_folder(target: dict[str, Any]) -> tuple[str, str]:
    """Make sure the target folder exists. Returns ``(uid, action)``."""
    folders = await _grafana_api("GET", "/api/folders")
    for folder in folders or []:
        if not isinstance(folder, dict):
            continue
        if folder.get("title") == target["folder_title"]:
            return str(folder.get("uid")), "reused"
        if folder.get("uid") == target["folder_uid"]:
            return str(folder.get("uid")), "reused"

    created = await _grafana_api(
        "POST",
        "/api/folders",
        {"uid": target["folder_uid"], "title": target["folder_title"]},
    )
    if isinstance(created, dict) and created.get("uid"):
        return str(created["uid"]), "created"
    raise RuntimeError(f"Could not create folder '{target['folder_title']}': {created!r}")


async def ensure_dashboard(target: dict[str, Any], folder_uid: str) -> tuple[str, str]:
    """Make sure the target dashboard exists in its folder.

    Returns ``(uid, action)``. An existing dashboard is never overwritten.
    """
    listing = await _grafana_api(
        "GET", f"/api/search?type=dash-db&folderUIDs={folder_uid}"
    )
    for entry in listing or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("title") == target["dashboard_title"]:
            return str(entry.get("uid")), "reused"
        if entry.get("uid") == target["dashboard_uid"]:
            return str(entry.get("uid")), "reused"

    raw = await mcp_call(
        "update_dashboard",
        _dashboard_payload(target),
        raw=True,
        timeout=30.0,
    )
    if not raw:
        raise RuntimeError(f"Grafana MCP did not create '{target['dashboard_title']}'")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}
    uid = parsed.get("uid") if isinstance(parsed, dict) else None
    if not uid and isinstance(parsed, dict) and isinstance(parsed.get("dashboard"), dict):
        uid = parsed["dashboard"].get("uid")
    if not uid:
        uid = target["dashboard_uid"]
    return str(uid), "created"


async def setup_grafana() -> list[dict[str, Any]]:
    """Create the folders and dashboards in the Grafana instance."""
    report: list[dict[str, Any]] = []
    for target in TARGETS:
        folder_uid, folder_action = await ensure_folder(target)
        dashboard_uid, dashboard_action = await ensure_dashboard(target, folder_uid)
        report.append(
            {
                "username": target["username"],
                "user_id": target["user_id"],
                "folder_title": target["folder_title"],
                "folder_uid": folder_uid,
                "folder_action": folder_action,
                "dashboard_title": target["dashboard_title"],
                "dashboard_uid": dashboard_uid,
                "dashboard_action": dashboard_action,
            }
        )
    return report


# ---------------------------------------------------------------------------
# PostgreSQL User & Role Store
# ---------------------------------------------------------------------------

def assign_scopes(report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign the folder and dashboard UIDs to the users in the role store.

    Only ``allowed_folder_uids`` and ``allowed_dashboard_uids`` are written.
    The role, the status, the denied dashboards and the permitted
    environments are left exactly as they are.
    """
    results: list[dict[str, Any]] = []
    with get_db_session() as session:
        for entry in report:
            user = UserStore.get_user_by_id(session, entry["user_id"])
            if user is None:
                raise RuntimeError(f"User {entry['user_id']} not found in the role store.")
            scopes = user.scopes
            current_folders = [str(u) for u in (scopes.allowed_folder_uids if scopes else [])]
            current_dashboards = [str(u) for u in (scopes.allowed_dashboard_uids if scopes else [])]

            # The wildcard folder grant is replaced by explicit folder UIDs:
            # '*' would also cover the other user's test folder, which would
            # defeat the isolation of the two test environments.
            folders = [uid for uid in current_folders if uid != "*"]
            for uid in (entry["folder_uid"],):
                if uid not in folders:
                    folders.append(uid)
            dashboards = list(current_dashboards)
            if entry["dashboard_uid"] not in dashboards:
                dashboards.append(entry["dashboard_uid"])

            UserStore.update_user_access(
                session,
                admin_user_id=ADMIN_USER_ID,
                target_user_id=entry["user_id"],
                scopes={
                    "allowed_folder_uids": folders,
                    "allowed_dashboard_uids": dashboards,
                },
                admin_role="admin",
            )
            results.append(
                {
                    "username": entry["username"],
                    "user_id": entry["user_id"],
                    "role": user.role,
                    "status": user.status,
                    "authz_version": user.authz_version,
                    "allowed_folder_uids": folders,
                    "allowed_dashboard_uids": dashboards,
                    "denied_dashboard_uids": list(scopes.denied_dashboard_uids if scopes else []),
                    "allowed_environments": list(scopes.allowed_environments if scopes else []),
                }
            )
        session.commit()
    return results


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

async def verify_grafana(report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read the folders and dashboards back from Grafana."""
    checks: list[dict[str, Any]] = []
    folders = await _grafana_api("GET", "/api/folders")
    for entry in report:
        folder = next(
            (
                f
                for f in folders or []
                if isinstance(f, dict) and f.get("uid") == entry["folder_uid"]
            ),
            None,
        )
        listing = await _grafana_api(
            "GET", f"/api/search?type=dash-db&folderUIDs={entry['folder_uid']}"
        )
        dashboard = next(
            (
                d
                for d in listing or []
                if isinstance(d, dict) and d.get("uid") == entry["dashboard_uid"]
            ),
            None,
        )
        checks.append(
            {
                "username": entry["username"],
                "folder_uid": entry["folder_uid"],
                "folder_title_in_grafana": (folder or {}).get("title"),
                "folder_found": folder is not None,
                "dashboard_uid": entry["dashboard_uid"],
                "dashboard_title_in_grafana": (dashboard or {}).get("title"),
                "dashboard_folder_uid_in_grafana": (dashboard or {}).get("folderUid"),
                "dashboard_found": dashboard is not None,
                "dashboard_in_expected_folder": (
                    dashboard is not None
                    and str((dashboard or {}).get("folderUid")) == entry["folder_uid"]
                ),
            }
        )
    return checks


def verify_store() -> list[dict[str, Any]]:
    """Read the scopes back from the PostgreSQL role store."""
    checks: list[dict[str, Any]] = []
    with get_db_session() as session:
        for target in TARGETS:
            user = UserStore.get_user_by_id(session, target["user_id"])
            scopes = user.scopes if user else None
            checks.append(
                {
                    "username": target["username"],
                    "user_id": target["user_id"],
                    "role": user.role if user else None,
                    "status": user.status if user else None,
                    "allowed_folder_uids": list(scopes.allowed_folder_uids) if scopes else [],
                    "allowed_dashboard_uids": list(scopes.allowed_dashboard_uids) if scopes else [],
                    "denied_dashboard_uids": list(scopes.denied_dashboard_uids) if scopes else [],
                    "allowed_environments": list(scopes.allowed_environments) if scopes else [],
                }
            )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Only verify the current state; do not create or modify anything.",
    )
    args = parser.parse_args()

    if args.verify:
        report = [
            {
                "username": t["username"],
                "user_id": t["user_id"],
                "folder_title": t["folder_title"],
                "folder_uid": t["folder_uid"],
                "dashboard_title": t["dashboard_title"],
                "dashboard_uid": t["dashboard_uid"],
            }
            for t in TARGETS
        ]
    else:
        report = run_sync(setup_grafana())
        assign_scopes(report)

    grafana_checks = run_sync(verify_grafana(report))
    store_checks = verify_store()

    print(json.dumps(
        {
            "grafana": grafana_checks,
            "role_store": store_checks,
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
