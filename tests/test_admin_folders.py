"""
tests/test_admin_folders.py

Tests for the folder inventory behind ``GET /api/v1/admin/folders``.

The endpoint reads the folders from Grafana through the Grafana MCP server,
and that tool answers with a response *envelope*::

    {"status": 200, "headers": {...}, "data": [{"uid": ..., "title": ...}]}

The folder list therefore lives in ``data``, not at the top level of the
answer. Reading it from the wrong level leaves the administrator with an
empty picker and no way to assign a folder scope -- so the unwrapping, and
the handling of everything that is not a well-formed envelope, are what
these tests pin down.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.auth.dependencies import require_principal

#: A folder of the connected Grafana instance.
FOLDER_UID = "maintainer-test"
FOLDER_TITLE = "maintainer-test"
OTHER_FOLDER_UID = "operator-test"
OTHER_FOLDER_TITLE = "operator-test"


def _install_grafana(monkeypatch, answer: Any) -> None:
    """Make the Grafana MCP answer the folder listing with ``answer``."""

    async def fake_mcp_call(tool: str, args: dict, **kwargs: Any) -> str:
        assert tool == "grafana_api_request"
        assert args.get("endpoint") == "/api/folders"
        return answer

    monkeypatch.setattr("app.mcp.session.mcp_call", fake_mcp_call)


def _uids(response: Any) -> list[str]:
    return [folder["uid"] for folder in response.json()["folders"]]


class TestFolderEnvelope:
    """The folder list is read out of the MCP response envelope."""

    def test_a_wrapped_response_lists_the_folders(self, monkeypatch, api_principal):
        _install_grafana(monkeypatch, json.dumps({
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "data": [
                {"id": 31, "title": FOLDER_TITLE, "uid": FOLDER_UID},
                {"id": 33, "title": OTHER_FOLDER_TITLE, "uid": OTHER_FOLDER_UID},
            ],
        }))

        with TestClient(app) as client:
            response = client.get("/api/v1/admin/folders")

        assert response.status_code == 200
        uids = _uids(response)
        assert FOLDER_UID in uids
        assert OTHER_FOLDER_UID in uids
        titles = {f["uid"]: f["title"] for f in response.json()["folders"]}
        assert titles[FOLDER_UID] == FOLDER_TITLE
        assert titles[OTHER_FOLDER_UID] == OTHER_FOLDER_TITLE

    def test_a_payload_without_an_envelope_is_accepted(self, monkeypatch, api_principal):
        """A build that answers with the payload directly still works."""
        _install_grafana(monkeypatch, json.dumps([
            {"id": 31, "title": FOLDER_TITLE, "uid": FOLDER_UID},
        ]))

        with TestClient(app) as client:
            response = client.get("/api/v1/admin/folders")

        assert response.status_code == 200
        assert FOLDER_UID in _uids(response)

    def test_a_folder_without_a_title_falls_back_to_its_uid(self, monkeypatch, api_principal):
        _install_grafana(monkeypatch, json.dumps({
            "status": 200,
            "headers": {},
            "data": [{"id": 31, "uid": FOLDER_UID}],
        }))

        with TestClient(app) as client:
            response = client.get("/api/v1/admin/folders")

        titles = {f["uid"]: f["title"] for f in response.json()["folders"]}
        assert titles[FOLDER_UID] == FOLDER_UID

    def test_a_folder_is_listed_once(self, monkeypatch, api_principal):
        _install_grafana(monkeypatch, json.dumps({
            "status": 200,
            "headers": {},
            "data": [
                {"title": FOLDER_TITLE, "uid": FOLDER_UID},
                {"title": FOLDER_TITLE, "uid": FOLDER_UID},
            ],
        }))

        with TestClient(app) as client:
            response = client.get("/api/v1/admin/folders")

        assert _uids(response).count(FOLDER_UID) == 1


class TestFolderAnswersThatCarryNoFolder:
    """An answer without a usable folder list must not fail the request."""

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param("", id="empty-answer"),
            pytest.param("[]", id="empty-list"),
            pytest.param("not json at all", id="not-json"),
            pytest.param('{"status": 200, "headers": {}}', id="envelope-without-data"),
            pytest.param('{"status": 200, "data": {"folders": []}}', id="data-is-not-a-list"),
            pytest.param("null", id="null-payload"),
        ],
    )
    def test_the_endpoint_still_reports_the_folders_it_knows(
        self, monkeypatch, api_principal, answer
    ):
        _install_grafana(monkeypatch, answer)

        with TestClient(app) as client:
            response = client.get("/api/v1/admin/folders")

        assert response.status_code == 200
        uids = _uids(response)
        assert "*" in uids
        assert FOLDER_UID not in uids

    def test_a_grafana_error_is_reported_as_an_empty_inventory(
        self, monkeypatch, api_principal
    ):
        _install_grafana(monkeypatch, json.dumps({
            "status": 500,
            "headers": {},
            "data": {"message": "Folder listing failed"},
        }))

        with TestClient(app) as client:
            response = client.get("/api/v1/admin/folders")

        assert response.status_code == 200
        uids = _uids(response)
        assert "*" in uids
        assert FOLDER_UID not in uids


class TestFolderEndpointAuthorization:
    """The folder inventory stays administrator-only."""

    def test_an_unauthenticated_caller_is_refused(self, monkeypatch):
        _install_grafana(monkeypatch, json.dumps({"status": 200, "data": []}))

        with TestClient(app) as client:
            assert client.get("/api/v1/admin/folders").status_code == 401

    def test_a_non_administrator_is_refused(self, monkeypatch, make_test_principal):
        _install_grafana(monkeypatch, json.dumps({"status": 200, "data": []}))

        principal = make_test_principal("maintainer", folders=(FOLDER_UID,))
        app.dependency_overrides[require_principal] = lambda: principal
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/admin/folders")
        finally:
            app.dependency_overrides.pop(require_principal, None)

        assert response.status_code == 403
