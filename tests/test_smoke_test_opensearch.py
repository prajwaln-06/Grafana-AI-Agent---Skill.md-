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


# ---------------------------------------------------------------------------------------------------------------------
def _clear_settings_cache():
    from app import config
    if hasattr(config.get_settings, "cache_clear"):
        config.get_settings.cache_clear()


# --rows: bounded, read-only row retrieval. Nothing here contacts a cluster; opensearch_client.search is patched.
# ---------------------------------------------------------------------------------------------------------------------
_SECRETS = ("alice.admin", "10.1.2.3", "hunter2", "SECRET-BODY-TEXT", "Traceback")


def _raw(hits, total=None):
    return {"took": 1, "timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"value": total if total is not None else len(hits), "relation": "eq"}, "hits": hits}}


def _hit(entry, *, literal_dotted=False, drop=(), secret=True):
    """A synthetic _source shaped like the server's filtered response for the family's projection."""
    src = {}
    for path in entry.output_projection:
        if path in drop:
            continue
        val = "2026-11-01T00:00:00.000Z" if path in (entry.time_field, "Created") else (
            "SECRET-BODY-TEXT user alice.admin from 10.1.2.3" if secret and path in ("Body", "Message", "fields.Value")
            else "x")
        if literal_dotted:
            src[path] = val
        else:
            node = src
            parts = path.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = val
    return {"_index": "i-2026.11.01", "_id": "1", "_source": src}


def _run(smoke, os_registry, os_ctx, settings, pid, raw=None, outcome=None, size=3, **kw):
    entry = os_registry.get(pid)
    out = outcome or opensearch_client.ExecutionOutcome(status="success", raw_body=raw)
    with patch.object(opensearch_client, "search", return_value=out) as m:
        r = smoke.run_rows_query(entry, os_ctx(pid), settings, size)
    return r, m


@pytest.mark.parametrize("pid", ["fabric_health", "controller_log", "ilo_events"])
@pytest.mark.parametrize("literal", [False, True])
def test_rows_success_verifies_projection_for_nested_and_literal_dotted_sources(smoke, os_registry, os_ctx, settings, pid, literal):
    e = os_registry.get(pid)
    r, m = _run(smoke, os_registry, os_ctx, settings, pid, raw=_raw([_hit(e, literal_dotted=literal) for _ in range(3)]))
    assert r["ok"] and r["rows_returned"] == 3 and r["not_seen"] == [] and r["detail"] is None
    assert set(r["present_in"]) == set(e.output_projection) and all(c == 3 for c in r["present_in"].values())
    # the request really carried the registered projection, sort and bounded size
    body = m.call_args.args[2]
    assert body["_source"] == list(e.output_projection) and body["size"] == 3
    assert body["sort"] == [{e.time_field: {"order": "desc"}}] and "aggs" not in body


def test_rows_controller_log_dotted_fields_are_checked(smoke, os_registry, os_ctx, settings):
    e = os_registry.get("controller_log")
    assert "Resource.host.name" in e.output_projection and "Resource.service.name" in e.output_projection
    r, _ = _run(smoke, os_registry, os_ctx, settings, "controller_log",
                raw=_raw([_hit(e, drop=("Resource.host.name",))]))
    assert r["ok"] and r["present_in"]["Resource.host.name"] == 0 and r["present_in"]["Resource.service.name"] == 1
    assert r["not_seen"] == ["Resource.host.name"]


def test_rows_missing_timestamp_is_a_failure(smoke, os_registry, os_ctx, settings):
    e = os_registry.get("ilo_events")
    r, _ = _run(smoke, os_registry, os_ctx, settings, "ilo_events", raw=_raw([_hit(e), _hit(e, drop=("@timestamp",))]))
    assert not r["ok"] and "@timestamp missing from 1 of 2 rows" in r["detail"]


def test_rows_zero_rows_is_a_failure_not_a_pass(smoke, os_registry, os_ctx, settings):
    r, _ = _run(smoke, os_registry, os_ctx, settings, "fabric_health", raw=_raw([], total=0),
                outcome=opensearch_client.ExecutionOutcome(status="empty_result", raw_body=_raw([], total=0)))
    assert not r["ok"] and r["rows_returned"] == 0 and "NOT verified" in r["detail"]


def test_rows_backend_failure_is_graceful_and_redacted(smoke, os_registry, os_ctx, settings):
    out = opensearch_client.ExecutionOutcome(status="endpoint_error", error="boom for user alice.admin at 10.1.2.3")
    r, _ = _run(smoke, os_registry, os_ctx, settings, "controller_log", outcome=out)
    assert not r["ok"] and r["stage"] == "executor"
    blob = json.dumps(r)
    assert "10.1.2.3" not in blob and "Traceback" not in blob


def test_rows_unexpected_exception_reports_type_without_traceback_or_secrets(smoke, os_registry, os_ctx, settings):
    with patch.object(opensearch_client, "search", side_effect=RuntimeError("password=hunter2 host 10.1.2.3\nTraceback (most recent call last)")):
        r = smoke.run_rows_query(os_registry.get("ilo_events"), os_ctx("ilo_events"), settings, 3)
    assert not r["ok"] and r["stage"] == "executor"
    d = r["detail"]
    assert "\n" not in d and "10.1.2.3" not in d and "hunter2" not in d and "Traceback" not in d and len(d) <= 200


def test_failure_text_scrubs_configured_credentials_pairs_tokens_and_tracebacks(smoke, settings):
    s = settings.model_copy(update={"opensearch_auth_username": "svc-reader", "opensearch_auth_password": "S3cretPw!"})
    for raw in ("401 for svc-reader / S3cretPw! at host", "failed: password=hunter2 next", "Authorization: Bearer abc123def456ghi",
                "token: xyz987 boom", "api_key=ABCDEF", "oops\nTraceback (most recent call last):\n  File \"x.py\", line 1\nValueError: v"):
        out = smoke._failure_text(RuntimeError(raw), s)
        for leak in ("svc-reader", "S3cretPw!", "hunter2", "abc123def456ghi", "xyz987", "ABCDEF", "File \"x.py\"", "Traceback"):
            assert leak not in out, (raw, out)
        assert "\n" not in out and len(out) <= 200
    assert smoke._failure_text(RuntimeError("connection refused"), s).startswith("RuntimeError: connection refused")


def test_rows_guard_rejection_is_reported(smoke, os_registry, settings):
    from app.opensearch_catalog.context import error_context
    r = smoke.run_rows_query(os_registry.get("controller_log"), error_context("down"), settings, 3)
    assert not r["ok"] and r["stage"] == "guard"


@pytest.mark.parametrize("asked,expected", [(0, 1), (-5, 1), (1, 1), (3, 3), (10, 10), (11, 10), (10**9, 10), ("x", 3), (None, 3)])
def test_rows_size_is_always_bounded(smoke, asked, expected):
    assert smoke.clamp_rows_size(asked) == expected


def test_rows_request_is_bounded_even_when_a_huge_size_is_requested(smoke, os_registry, os_ctx, settings):
    e = os_registry.get("controller_log")
    _, m = _run(smoke, os_registry, os_ctx, settings, "controller_log", raw=_raw([_hit(e)]), size=10**6)
    body = m.call_args.args[2]
    assert body["size"] == smoke.ROWS_HARD_MAX and "from" not in body and body["timeout"]
    assert body["track_total_hits"] is False


def test_rows_result_contains_no_values_only_registry_names_and_counts(smoke, os_registry, os_ctx, settings):
    for pid in ("fabric_health", "controller_log", "ilo_events"):
        e = os_registry.get(pid)
        r, _ = _run(smoke, os_registry, os_ctx, settings, pid, raw=_raw([_hit(e)]))
        blob = json.dumps(r)
        for s in _SECRETS:
            assert s not in blob, (pid, s)
        assert set(r) <= {"pattern_id", "requested_size", "ok", "stage", "execution_status", "rows_returned",
                          "over_requested", "present_in", "not_seen", "partial", "detail"}
        assert all(isinstance(c, int) for c in r["present_in"].values())


def _main_with_cluster(smoke, monkeypatch, os_registry, patterns_hits, argv, mapping_ok=True):
    """Run main() with the cluster mocked: fixture mappings + patched search. Returns (code, stdout-free)."""
    import json as _j
    from app import config
    fixtures = Path(__file__).parent / "fixtures" / "opensearch"
    by_index = {"slingshot_crayfabrichealth-*": "mapping_fabric_health.json", "controllerlog-*": "mapping_controller_log.json",
                "log_iml-*": "mapping_ilo_events.json"}
    runtime.reset()
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENSEARCH_URL", "http://localhost:9600")
    _clear_settings_cache()
    monkeypatch.setattr(runtime, "make_mapping_fetcher",
                        lambda *a, **k: (lambda ip: _j.loads((fixtures / by_index[ip]).read_text()) if mapping_ok else None))

    def fake_search(base_url, index_pattern, body, **kw):
        if body.get("size") == 0:
            return opensearch_client.ExecutionOutcome(status="success", raw_body=_raw([], total=7))
        e = next(p for p in os_registry.enabled_patterns if p.index_pattern == index_pattern)
        return opensearch_client.ExecutionOutcome(status="success", raw_body=_raw([_hit(e) for _ in range(patterns_hits)]))
    return fake_search


def test_main_rows_end_to_end_prints_names_and_counts_only(smoke, monkeypatch, capsys, os_registry):
    fake = _main_with_cluster(smoke, monkeypatch, os_registry, 2, ["--rows"])
    from app import config
    try:
        with patch.object(opensearch_client, "search", side_effect=fake):
            code = smoke.main(["--rows", "--rows-size", "2"])
    finally:
        runtime.reset(); _clear_settings_cache()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Bounded row retrieval" in out and "Resource.host.name: present in 2 of 2 rows" in out
    for s in _SECRETS:
        assert s not in out
    assert "Bounded count queries" not in out      # --queries is independent of --rows


def test_main_rows_failure_exits_nonzero_without_traceback(smoke, monkeypatch, capsys, os_registry):
    fake = _main_with_cluster(smoke, monkeypatch, os_registry, 0, ["--rows"])
    from app import config
    try:
        with patch.object(opensearch_client, "search",
                          return_value=opensearch_client.ExecutionOutcome(status="empty_result", raw_body=_raw([], 0))):
            code = smoke.main(["--rows"])
    finally:
        runtime.reset(); _clear_settings_cache()
    out = capsys.readouterr().out
    assert code == 1 and "FAILED" in out and "Traceback" not in out


def test_main_queries_behaviour_is_unchanged_and_runs_no_rows(smoke, monkeypatch, capsys, os_registry):
    fake = _main_with_cluster(smoke, monkeypatch, os_registry, 2, ["--queries"])
    from app import config
    seen = []
    def spy(*a, **k):
        r = fake(*a, **k); seen.append(a[2].get("size")); return r
    try:
        with patch.object(opensearch_client, "search", side_effect=spy):
            code = smoke.main(["--queries"])
    finally:
        runtime.reset(); _clear_settings_cache()
    out = capsys.readouterr().out
    assert code == 0 and "Bounded count queries" in out and "Bounded row retrieval" not in out
    assert seen and set(seen) == {0}


def test_main_rows_flag_defaults_do_not_change_plain_run(smoke):
    import argparse
    assert smoke.ROWS_DEFAULT == 3 and smoke.ROWS_HARD_MAX == 10
