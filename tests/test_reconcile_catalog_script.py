"""
tests/test_reconcile_catalog_script.py

scripts/reconcile_catalog.py: orchestration, report content, exit codes and
read-only behavior, with Prometheus mocked (no live server).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import requests

from app.catalog import reconciler as rc
from app.catalog.schema import Catalog, CatalogEntry, Priority
from app.config import Settings
from scripts import reconcile_catalog as script


def _entry(name):
    return CatalogEntry(
        name=name, type="gauge", category="CPU", priority=Priority.REVIEW.value,
        exporter="node-exporter", status="approved", help="h",
        reference_path="references/node-exporter/cpu.md",
    )


def _write_catalog(path, names):
    cat = Catalog(catalog_version="1.0", generated_at="t", metrics=tuple(_entry(n) for n in names))
    path.write_text(json.dumps(cat.to_dict(), indent=2) + "\n", encoding="utf-8")


def _settings(tmp_path, **kw):
    return Settings(
        gemini_api_key="x", catalog_path=tmp_path / "catalog.json",
        catalog_reviews_path=tmp_path / "reviews.json",
        prometheus_url="http://prom.test:9090", **kw,
    )


def _resp(body):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = body
    return r


def _session(names, metadata):
    def _get(url, timeout=None):
        payload = names if url.endswith("/api/v1/label/__name__/values") else metadata
        if isinstance(payload, Exception):
            raise payload
        return _resp({"status": "success", "data": payload})
    s = MagicMock()
    s.get.side_effect = _get
    return s


def _run(settings, session, **kw):
    out: list[str] = []
    with patch.object(rc, "_get_session", return_value=session):
        code = script.run(settings, emit=out.append, **kw)
    return code, "\n".join(out)


def test_report_lists_pending_unavailable_undetermined_and_scope(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1", "node_load5"])
    settings = _settings(tmp_path, catalog_discovery_exclude_patterns=["go_*"])
    session = _session(
        ["node_load1", "acme_new", "acme_untyped", "go_goroutines"],
        {"acme_new": [{"type": "counter", "help": "n"}]},
    )
    code, text = _run(settings, session)
    assert code == script.EXIT_OK
    assert "4 metric names at runtime" in text
    assert "Approved metrics available at runtime: 1" in text
    assert "Approved metrics currently unavailable: 1" in text and "node_load5" in text
    assert "New discoveries pending review: 1" in text and "acme_new (counter)" in text
    assert "Type undetermined (skipped, never guessed): 1" in text and "acme_untyped" in text
    assert "Out of configured discovery scope: 1" in text


def test_empty_successful_discovery_is_reported_as_success_not_failure(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    code, text = _run(_settings(tmp_path), _session([], {}))
    assert code == script.EXIT_OK
    assert "zero metric names" in text
    assert "DISCOVERY FAILED" not in text
    assert "Approved metrics currently unavailable: 1" in text


def test_name_discovery_failure_exits_1_and_draws_no_conclusions(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    code, text = _run(
        _settings(tmp_path), _session(requests.exceptions.ConnectionError("refused"), {})
    )
    assert code == script.EXIT_DISCOVERY_FAILED
    assert "DISCOVERY FAILED" in text and "__name__/values" in text
    assert "currently unavailable" not in text


def test_metadata_discovery_failure_exits_1(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    code, text = _run(_settings(tmp_path), _session(["node_load1"], requests.exceptions.Timeout()))
    assert code == script.EXIT_DISCOVERY_FAILED
    assert "api/v1/metadata" in text


def test_missing_or_invalid_vendor_catalog_exits_2(tmp_path):
    code, text = _run(_settings(tmp_path), _session([], {}))  # no catalog.json written
    assert code == script.EXIT_CONFIG_ERROR and "CONFIGURATION ERROR" in text
    (tmp_path / "catalog.json").write_text("{not json", encoding="utf-8")
    code, _ = _run(_settings(tmp_path), _session([], {}))
    assert code == script.EXIT_CONFIG_ERROR


def test_invalid_scope_pattern_exits_2(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    code, text = _run(_settings(tmp_path, catalog_discovery_exclude_patterns=[" "]), _session([], {}))
    assert code == script.EXIT_CONFIG_ERROR and "CONFIGURATION ERROR" in text


def test_script_is_read_only_and_repeatable(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    before = (tmp_path / "catalog.json").read_bytes()
    files_before = sorted(p.name for p in tmp_path.iterdir())
    session = _session(["node_load1", "acme_new"], {"acme_new": [{"type": "gauge", "help": "x"}]})
    first = _run(_settings(tmp_path), session)
    second = _run(_settings(tmp_path), session)
    assert first == second
    assert (tmp_path / "catalog.json").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before


def test_report_truncates_long_lists_unless_all_requested(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    names = ["node_load1"] + [f"acme_{i:03d}" for i in range(40)]
    s = _session(names, {})
    _, short = _run(_settings(tmp_path), s)
    _, full = _run(_settings(tmp_path), s, show_all=True)
    assert "and 15 more" in short and "acme_039" not in short
    assert "acme_039" in full


def test_report_reflects_persisted_rejections_and_approved_discoveries(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    (tmp_path / "reviews.json").write_text(json.dumps({"schema_version": 1, "decisions": [
        {"name": "node_load1", "decision": "rejected", "decided_at": "2026-10-27T05:00:00Z"},
        {"name": "acme_ok", "decision": "approved", "decided_at": "2026-10-27T05:00:00Z",
         "type": "gauge", "category": "Q", "exporter": "node-exporter",
         "reference_path": "references/node-exporter/cpu.md"},
    ]}))
    session = _session(["node_load1", "acme_ok", "acme_new"],
                       {"acme_new": [{"type": "gauge", "help": "n"}]})
    code, text = _run(_settings(tmp_path), session)
    assert code == script.EXIT_OK
    assert "Review decisions: 1 approved, 1 rejected" in text
    assert "Preserved rejections: 1" in text and "node_load1" in text
    assert "Approved metrics available at runtime: 1" in text  # acme_ok, not the rejected vendor metric
    assert "New discoveries pending review: 1" in text and "acme_new" in text


def test_malformed_review_store_exits_2_instead_of_ignoring_decisions(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    (tmp_path / "reviews.json").write_text("{broken")
    code, text = _run(_settings(tmp_path), _session(["node_load1"], {}))
    assert code == script.EXIT_CONFIG_ERROR and "CONFIGURATION ERROR" in text


def test_superseded_approval_record_is_warned_about_not_silently_dropped(tmp_path):
    _write_catalog(tmp_path / "catalog.json", ["node_load1"])
    (tmp_path / "reviews.json").write_text(json.dumps({"schema_version": 1, "decisions": [
        {"name": "node_load1", "decision": "approved", "decided_at": "2026-10-27T05:00:00Z",
         "type": "gauge", "category": "Q", "exporter": "node-exporter",
         "reference_path": "references/node-exporter/cpu.md"}]}))
    code, text = _run(_settings(tmp_path), _session(["node_load1"], {}))
    assert code == script.EXIT_OK and "superseded by a vendor entry" in text
