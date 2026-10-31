"""Offline tests for scripts/smoke_test_opensearch.py (its logic only). The script itself is the LIVE check and
is not run by the suite; nothing here contacts a cluster."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app import opensearch_client
from app.opensearch_catalog import runtime

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_test_opensearch.py"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_test_opensearch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("pid", ["fabric_health", "controller_log", "ilo_events"])
def test_check_family_reports_usable_for_a_complete_mapping(smoke, os_registry, pid):
    from app.opensearch_catalog.field_catalog import FieldCatalogCache
    m = json.loads((Path(__file__).parent / "fixtures" / "opensearch" / f"mapping_{pid}.json").read_text())
    cache = FieldCatalogCache(lambda ip: m, ttl_seconds=60, stale_ttl_seconds=60, max_indices=10, failure_backoff_seconds=1)
    r = smoke.check_family(os_registry.get(pid), cache)
    assert r["usable"] and r["discovery_status"] == "fresh"
    assert r["registered_fields_missing_live"] == [] and r["role_bindings_dropped"] == []


def test_check_family_reports_registered_fields_missing_from_the_live_mapping(smoke, os_registry):
    from app.opensearch_catalog.field_catalog import FieldCatalogCache
    m = json.loads((Path(__file__).parent / "fixtures" / "opensearch" / "mapping_controller_log.json").read_text())
    for idx in m.values():
        del idx["mappings"]["properties"]["Resource"]["properties"]["service"]
    cache = FieldCatalogCache(lambda ip: m, ttl_seconds=60, stale_ttl_seconds=60, max_indices=10, failure_backoff_seconds=1)
    r = smoke.check_family(os_registry.get("controller_log"), cache)
    assert "Resource.service.name" in r["registered_fields_missing_live"]
    assert any(x.startswith("service:") for x in r["role_bindings_dropped"])


def test_check_family_reports_unusable_when_cluster_unreachable(smoke, os_registry):
    from app.opensearch_catalog.field_catalog import FieldCatalogCache
    cache = FieldCatalogCache(lambda ip: None, ttl_seconds=60, stale_ttl_seconds=60, max_indices=10, failure_backoff_seconds=1)
    r = smoke.check_family(os_registry.get("ilo_events"), cache)
    assert not r["usable"] and r["discovery_status"] == "unavailable"
    # no mapping was obtained, so nothing may be claimed about the fields
    assert r["registered_fields_missing_live"] == [] and r["role_bindings_dropped"] == [] and r["warnings"] == []


def test_count_entry_is_bounded_and_guard_approved(smoke, os_ctx, os_registry):
    from app.opensearch_catalog import guard
    for pid in ("fabric_health", "controller_log", "ilo_events"):
        e = os_registry.get(pid)
        g = guard.check_entry(smoke.count_entry(e), os_ctx(pid))
        assert g.ok, (pid, g.reason)
        assert g.query["size"] == 0 and g.window["seconds"] == 86400


def test_run_count_query_reports_metadata_only(smoke, os_ctx, os_registry, settings):
    raw = {"took": 1, "timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
           "hits": {"total": {"value": 5, "relation": "eq"}, "hits": []}}
    out = opensearch_client.ExecutionOutcome(status="success", raw_body=raw)
    with patch.object(opensearch_client, "search", return_value=out):
        r = smoke.run_count_query(os_registry.get("controller_log"), os_ctx("controller_log"), settings)
    assert r["ok"] and r["count"]["value"] == 5 and r["partial"] is False
    assert "hits" not in r and "_source" not in json.dumps(r)


def test_run_count_query_surfaces_a_guard_rejection(smoke, os_registry, settings):
    from app.opensearch_catalog.context import error_context
    r = smoke.run_count_query(os_registry.get("controller_log"), error_context("down"), settings)
    assert not r["ok"] and r["stage"] == "guard"


def test_save_mapping_writes_only_the_mapping(smoke, os_registry, tmp_path):
    m = {"controllerlog-2026.10.28": {"mappings": {"properties": {"Body": {"type": "text"}}}}}
    path = smoke.save_mapping(os_registry.get("controller_log"), lambda ip: m, tmp_path)
    assert json.loads(path.read_text()) == m
    assert smoke.save_mapping(os_registry.get("controller_log"), lambda ip: None, tmp_path) is None


def test_main_exits_nonzero_and_does_not_traceback_when_the_cluster_is_unreachable(smoke, monkeypatch, capsys):
    runtime.reset()
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    from app import config
    config.get_settings.cache_clear() if hasattr(config.get_settings, "cache_clear") else None
    monkeypatch.setenv("OPENSEARCH_URL", "http://127.0.0.1:9")   # discard port: connection refused
    try:
        code = smoke.main([])
    finally:
        runtime.reset()
        config.get_settings.cache_clear() if hasattr(config.get_settings, "cache_clear") else None
    out = capsys.readouterr().out
    assert code == 1 and "usable=False" in out and "FAILED" in out
