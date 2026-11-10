"""tests/test_dashboard_metadata_scope.py

Regression tests for the dashboard scope decision and for what the decision
is allowed to reveal.

Deciding whether a caller may read a dashboard requires the dashboard's
*actual* folder, which only Grafana knows. The lookup must therefore:

* use metadata rather than the dashboard's content wherever Grafana supports
  it, so an out-of-scope read does not download the dashboard at all; and
* never let the dashboard's title, panel definitions, queries, variables or
  annotations reach the caller or the agent -- not in a return value, not in
  an exception, and not through the search fallback.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from app.auth.principal import (
    CURRENT_PRINCIPAL,
    Principal,
    set_current_principal,
)
from app.auth.resource_scope import AuthorizationDenied

FOLDER_A = "fld-metadata-team-a"
FOLDER_B = "fld-metadata-team-b"
DASH_A = "dash-metadata-a"
DASH_B = "dash-metadata-b"
#: A dashboard that Grafana knows but that the search listing does not
#: report -- the case where the metadata lookup has to fall back.
DASH_UNLISTED = "dash-metadata-unlisted"

#: Distinctive strings that only exist inside a dashboard's *content*. If any
#: of them appears in a result or an error, the content leaked.
SECRET_TITLE = "Confidential Billing Overview"
SECRET_PANEL = "Secret Revenue Panel"
SECRET_QUERY = "secret_revenue_total"
SECRET_VARIABLE = "secret_billing_token"
SECRET_ANNOTATION = "secret_annotation_query"


def make_principal(
    role: str,
    *,
    user_id: str | None = None,
    folders: tuple[str, ...] = (),
    dashboards: tuple[str, ...] = (),
) -> Principal:
    return Principal(
        user_id=user_id or f"usr_{role}",
        issuer="https://authentik.test",
        subject=f"sub_{user_id or role}",
        authenticated=True,
        status="active",
        role=role,
        allowed_folder_uids=frozenset(folders),
        allowed_dashboard_uids=frozenset(dashboards),
        allowed_environments=frozenset({"env-metadata"}),
        username=role,
    )


ADMIN = make_principal("admin", user_id="usr_metadata_admin", folders=("*",))
MAINTAINER = make_principal(
    "maintainer", user_id="usr_metadata_maintainer", folders=(FOLDER_A,)
)


@contextmanager
def acting_as(principal: Principal | None) -> Iterator[None]:
    token = set_current_principal(principal)
    try:
        yield
    finally:
        try:
            CURRENT_PRINCIPAL.reset(token)
        except (ValueError, LookupError):  # pragma: no cover - defensive
            set_current_principal(None)


# ---------------------------------------------------------------------------
# The fake upstream Grafana MCP client.
#
# The dashboard bodies carry distinctive "secret" strings so that a leak is
# detectable. The *listing* is metadata only -- exactly what Grafana's
# search endpoint returns -- and it never contains a body.
# ---------------------------------------------------------------------------

def _dashboard_body(uid: str, folder_uid: str) -> dict[str, Any]:
    return {
        "dashboard": {
            "uid": uid,
            "title": SECRET_TITLE if uid == DASH_B else f"Dashboard {uid}",
            "panels": [
                {
                    "id": 1,
                    "title": SECRET_PANEL if uid == DASH_B else "CPU",
                    "type": "timeseries",
                    "targets": [
                        {
                            "expr": SECRET_QUERY if uid == DASH_B else "up",
                            "legendFormat": "{{instance}}",
                        }
                    ],
                }
            ],
            "templating": {
                "list": [
                    {
                        "name": SECRET_VARIABLE if uid == DASH_B else "instance",
                        "type": "query",
                    }
                ]
            },
            "annotations": {
                "list": [
                    {
                        "name": SECRET_ANNOTATION if uid == DASH_B else "Annotations",
                        "datasource": {"type": "grafana"},
                    }
                ]
            },
        },
        "meta": {"folderUid": folder_uid},
    }


class RecordingMcp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def tools(self) -> list[str]:
        with self._lock:
            return [call["tool"] for call in self.calls]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)

    def body_fetches(self) -> list[str]:
        """The calls that carry a dashboard's *content*."""
        with self._lock:
            return [
                call["tool"]
                for call in self.calls
                if call["tool"]
                in {"get_dashboard_by_uid", "get_dashboard_summary", "get_dashboard_panel_queries"}
            ]

    async def mcp_call(self, tool: str, args: dict[str, Any] | None = None, **kwargs) -> str:
        args = dict(args or {})
        with self._lock:
            self.calls.append({"tool": tool, "args": args})

        if tool == "grafana_api_request":
            endpoint = str(args.get("endpoint") or "")
            if endpoint.startswith("/api/search"):
                # The search listing is metadata only: uid, title, tags and
                # the folder -- never a panel, a query or a variable.
                return json.dumps(
                    [
                        {
                            "uid": DASH_A,
                            "title": "Team A dashboard",
                            "tags": [],
                            "folderUid": FOLDER_A,
                        },
                        {
                            "uid": DASH_B,
                            "title": "Team B dashboard",
                            "tags": [],
                            "folderUid": FOLDER_B,
                        },
                    ]
                )
            if endpoint.startswith("/api/dashboards/uid/"):
                uid = endpoint.rsplit("/", 1)[-1]
                if uid == DASH_UNLISTED:
                    return json.dumps(_dashboard_body(uid, FOLDER_A))
                if uid not in (DASH_A, DASH_B):
                    return ""
                folder = FOLDER_A if uid == DASH_A else FOLDER_B
                return json.dumps(_dashboard_body(uid, folder))
            return "{}"

        if tool == "get_dashboard_by_uid":
            uid = str(args.get("uid") or "")
            if uid == DASH_UNLISTED:
                return json.dumps(_dashboard_body(uid, FOLDER_A))
            if uid not in (DASH_A, DASH_B):
                return ""
            folder = FOLDER_A if uid == DASH_A else FOLDER_B
            return json.dumps(_dashboard_body(uid, folder))

        if tool == "get_dashboard_summary":
            uid = str(args.get("uid") or "")
            title = SECRET_TITLE if uid == DASH_B else f"Dashboard {uid}"
            panel = SECRET_PANEL if uid == DASH_B else "CPU"
            return json.dumps(
                {
                    "uid": uid,
                    "title": title,
                    "panels": [
                        {
                            "title": panel,
                            "type": "timeseries",
                            "description": "CPU usage",
                            "queryCount": 1,
                        }
                    ],
                    "variables": [{"name": SECRET_VARIABLE if uid == DASH_B else "instance"}],
                    "timeRange": {"from": "now-1h", "to": "now"},
                }
            )

        if tool == "search_dashboards":
            return json.dumps(
                {
                    "dashboards": [
                        {
                            "uid": DASH_A,
                            "title": "Team A dashboard",
                            "tags": [],
                            "folderUid": FOLDER_A,
                        },
                        {
                            "uid": DASH_B,
                            "title": "Team B dashboard",
                            "tags": [],
                            "folderUid": FOLDER_B,
                        },
                    ],
                    "total": 2,
                }
            )

        return "{}"


@pytest.fixture()
def mcp(monkeypatch) -> Iterator[RecordingMcp]:
    import importlib

    recorder = RecordingMcp()
    for module_name in (
        "app.grafana_tools.dashboard",
        "app.grafana_tools.prometheus",
        "app.grafana_tools.opensearch",
        "app.grafana_tools.datasource",
        "app.grafana_tools.dashboard_writing",
        "app.grafana_tools.alerting",
    ):
        module = importlib.import_module(module_name)
        if hasattr(module, "mcp_call"):
            monkeypatch.setattr(module, "mcp_call", recorder.mcp_call)
    return recorder


@pytest.fixture(autouse=True)
def _reset_caches():
    import app.grafana_tools.dashboard as dashboard_module
    import app.grafana_tools.datasource as datasource_module
    import app.grafana_tools.discovery as discovery_module

    dashboard_module._dashboard_list = None
    dashboard_module._dashboard_details = {}
    datasource_module._datasource_registry = None
    discovery_module._panel_index = None
    yield
    dashboard_module._dashboard_list = None
    dashboard_module._dashboard_details = {}
    datasource_module._datasource_registry = None
    discovery_module._panel_index = None


SECRETS = (SECRET_TITLE, SECRET_PANEL, SECRET_QUERY, SECRET_VARIABLE, SECRET_ANNOTATION)


def assert_no_leak(text: str) -> None:
    """No part of a dashboard's content may appear in ``text``."""
    for secret in SECRETS:
        assert secret not in text, f"dashboard content leaked: {secret!r}"


# ---------------------------------------------------------------------------
# 1. The metadata lookup.
# ---------------------------------------------------------------------------

class TestMetadataOnlyLookup:
    def test_the_scope_lookup_uses_metadata_not_the_dashboard_body(self, mcp):
        """Resolving a dashboard's folder must not download the dashboard."""
        from app.grafana_tools.dashboard import fetch_dashboard_folder_uid

        exists, folder_uid = __import__("asyncio").run(
            fetch_dashboard_folder_uid(DASH_A, refresh=True)
        )

        assert exists is True
        assert folder_uid == FOLDER_A
        # Only the metadata listing was fetched; the body was not.
        assert mcp.body_fetches() == []
        assert "grafana_api_request" in mcp.tools

    def test_the_folder_comes_from_grafana_not_from_the_caller(self, mcp):
        """The dashboard really lives in the folder Grafana reports."""
        from app.grafana_tools.dashboard import fetch_dashboard_folder_uid

        exists, folder_uid = __import__("asyncio").run(
            fetch_dashboard_folder_uid(DASH_B, refresh=True)
        )

        assert exists is True
        assert folder_uid == FOLDER_B

    def test_a_dashboard_outside_the_listing_falls_back_to_its_own_metadata(self, mcp):
        """The fallback reads the folder out of the body and keeps it internal."""
        from app.grafana_tools.dashboard import fetch_dashboard_folder_uid

        # The listing knows nothing about this dashboard, so the folder can
        # only come from the dashboard's own metadata.
        exists, folder_uid = __import__("asyncio").run(
            fetch_dashboard_folder_uid(DASH_UNLISTED, refresh=True)
        )

        assert exists is True
        assert folder_uid == FOLDER_A
        # The body was fetched, but only the folder was taken out of it.
        assert mcp.body_fetches() == ["get_dashboard_by_uid"]

    def test_an_unknown_dashboard_is_reported_as_missing(self, mcp):
        from app.grafana_tools.dashboard import fetch_dashboard_folder_uid

        exists, folder_uid = __import__("asyncio").run(
            fetch_dashboard_folder_uid("dash-metadata-unknown", refresh=True)
        )

        assert exists is False
        assert folder_uid is None

    def test_the_search_listing_never_carries_dashboard_content(self, mcp):
        """The metadata endpoint reports titles and folders, not panels."""
        from app.grafana_tools.dashboard import fetch_dashboard_list

        dashboards = __import__("asyncio").run(fetch_dashboard_list(refresh=True))

        assert dashboards
        for entry in dashboards:
            assert set(entry) <= {
                "uid", "title", "tags", "url", "folderUid", "folderTitle"
            }


# ---------------------------------------------------------------------------
# 2. An unauthorized read reveals nothing.
# ---------------------------------------------------------------------------

class TestUnauthorizedReadLeaksNothing:
    def test_an_out_of_scope_read_is_denied_with_zero_content(self, mcp):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied) as excinfo:
                get_dashboard_panels(DASH_B)

        message = str(excinfo.value)
        assert excinfo.value.status_code == 403
        assert_no_leak(message)
        # The dashboard's content was never fetched.
        assert mcp.body_fetches() == []

    def test_the_denial_reason_does_not_name_the_dashboard(self, mcp):
        """The refusal does not confirm that the dashboard exists."""
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied) as excinfo:
                get_dashboard_panels(DASH_B)

        message = str(excinfo.value)
        assert "Confidential" not in message
        assert "Team B" not in message

    def test_a_proposal_for_an_out_of_scope_dashboard_leaks_nothing(self, mcp):
        """The proposal path denies before the dashboard body is read."""
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied) as excinfo:
                __import__("asyncio").run(
                    build_proposal(f"Add a panel to dashboard {DASH_B}")
                )

        assert_no_leak(str(excinfo.value))
        assert mcp.body_fetches() == []

    def test_the_search_fallback_never_returns_the_unfiltered_payload(self, mcp, monkeypatch):
        """A failure in the scope filter must not leak the raw search result."""
        from app.grafana_tools import wrapper

        async def _boom(*args, **kwargs):
            raise RuntimeError("the scope filter exploded")

        monkeypatch.setattr(
            wrapper, "filter_dashboards_in_scope", _boom
        )

        with acting_as(MAINTAINER):
            result = wrapper.search_dashboards("overview")

        assert_no_leak(result)
        assert "Team B dashboard" not in result
        assert "could not be authorized" in result


# ---------------------------------------------------------------------------
# 3. A client-supplied folder cannot widen the scope.
# ---------------------------------------------------------------------------

class TestFolderCannotBeClaimed:
    def test_a_claimed_folder_does_not_unlock_a_foreign_dashboard(self, mcp):
        """The caller's own folder does not decide the outcome."""
        from app.auth.resource_scope import authorize_dashboard_access

        # The maintainer may write to Team A; the dashboard lives in Team B.
        decision = __import__("asyncio").run(
            authorize_dashboard_access(MAINTAINER, DASH_B)
        )
        assert decision.allowed is False
        assert_no_leak(decision.reason)

    def test_the_real_folder_decides_the_outcome(self, mcp):
        from app.auth.resource_scope import authorize_dashboard_access

        decision = __import__("asyncio").run(
            authorize_dashboard_access(MAINTAINER, DASH_A)
        )
        assert decision.allowed is True

    def test_the_folder_is_never_taken_from_the_request(self, mcp):
        """The scope decision only ever sees Grafana's folder."""
        from app.auth.resource_scope import authorize_dashboard_access

        # A dashboard that Grafana reports in Team B stays in Team B, no
        # matter what a caller claims.
        decision = __import__("asyncio").run(
            authorize_dashboard_access(MAINTAINER, DASH_B)
        )
        assert decision.allowed is False
        assert mcp.body_fetches() == []


# ---------------------------------------------------------------------------
# 4. Authorized reads keep working.
# ---------------------------------------------------------------------------

class TestAuthorizedReadsStillWork:
    def test_an_in_scope_dashboard_is_read(self, mcp):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(MAINTAINER):
            output = get_dashboard_panels(DASH_A)

        assert "CPU" in output
        assert "get_dashboard_summary" in mcp.tools

    def test_an_admin_reads_any_dashboard(self, mcp):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(ADMIN):
            output = get_dashboard_panels(DASH_B)

        assert SECRET_PANEL in output

    def test_the_listing_still_works(self, mcp):
        from app.grafana_tools.wrapper import list_dashboards

        with acting_as(MAINTAINER):
            output = list_dashboards()

        assert "Team A dashboard" in output
        assert "Team B dashboard" not in output

    def test_the_search_still_works(self, mcp):
        from app.grafana_tools.wrapper import search_dashboards

        with acting_as(MAINTAINER):
            output = search_dashboards("dashboard")

        assert "Team A dashboard" in output
        assert "Team B dashboard" not in output
